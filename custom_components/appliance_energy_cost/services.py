"""The ``preview_backfill``, ``import_backfill`` and ``calibrate_cost`` services.

The only module that touches ``homeassistant.components.recorder``. It reads
hourly long-term statistics through the supported statistics APIs, narrows the
rows into domain shapes at the edge, and hands the pure calculation to
``backfill.py``. The preview never writes; the import writes only through the
supported ``async_import_statistics`` API, only to the integration's own cost
statistic IDs, and only after an explicit ``confirm`` and every pre-write gate
has passed for every selected appliance. Neither backfill service modifies
live sensors — joining the live series to imported history is
``calibrate_cost``, the batched entity service registered here (issue #7):
validation lives in this module, the state mutation on the targeted
``ApplianceCostSensor``, and recorded statistics are never touched.

Concurrency shape (event loop never blocks): sequential executor jobs — a
recorder-executor pass reading metadata and rows, a general-executor pass
running the Decimal-heavy series calculation, and (import only) a
recorder-executor read-back after the write is committed.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import TYPE_CHECKING, Final

import voluptuous as vol
from homeassistant.components.recorder.const import DOMAIN as RECORDER_DOMAIN
from homeassistant.components.recorder.models import (
    StatisticData,
    StatisticMeanType,
    StatisticMetaData,
)
from homeassistant.components.recorder.statistics import (
    StatisticsRow,
    async_import_statistics,
    get_metadata,
    statistics_during_period,
)
from homeassistant.components.sensor import DOMAIN as SENSOR_DOMAIN
from homeassistant.core import (
    EntityServiceResponse,
    HomeAssistant,
    ServiceCall,
    ServiceResponse,
    SupportsResponse,
    callback,
)
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers import service
from homeassistant.helpers.recorder import get_instance
from homeassistant.helpers.selector import ConfigEntrySelector, ConfigEntrySelectorConfig
from homeassistant.helpers.typing import VolDictType
from homeassistant.util import dt as dt_util
from homeassistant.util.json import JsonValueType

from .backfill import BackfillSeries, EnergyRow, PriceRow, build_backfill_series
from .const import (
    ATTR_APPLIANCE,
    ATTR_APPLIANCES,
    ATTR_CONFIG_ENTRY,
    ATTR_CONFIRM,
    ATTR_END,
    ATTR_END_ENERGY_KWH,
    ATTR_ENERGY_GAP_HOURS,
    ATTR_EXISTING_ROWS_KEPT,
    ATTR_EXPECTED_HOURS,
    ATTR_FIRST_POINT,
    ATTR_HOURLY_POINTS,
    ATTR_INITIAL_COST,
    ATTR_INVALID_ENERGY_HOURS,
    ATTR_INVALID_ENERGY_RANGES,
    ATTR_LAST_POINT,
    ATTR_MISSING_PRICE_HOURS,
    ATTR_MISSING_PRICE_RANGES,
    ATTR_OK,
    ATTR_OVERWRITE_EXISTING,
    ATTR_ROWS_WRITTEN,
    ATTR_START,
    ATTR_STATISTIC_ID,
    ATTR_STRICT,
    ATTR_TOTAL_COST,
    ATTR_TOTAL_ENERGY_KWH,
    ATTR_VALID,
    ATTR_VALUE,
    CONF_CURRENCY,
    CONF_ENERGY_SENSOR,
    CONF_PRICE_SENSOR,
    DOMAIN,
    RANGE_CAP,
    SERVICE_CALIBRATE_COST,
    SERVICE_IMPORT_BACKFILL,
    SERVICE_PREVIEW_BACKFILL,
    SUBENTRY_TYPE_APPLIANCE,
)
from .models import ApplianceConfig, EntryRuntimeData, decode_appliance_config
from .units import (
    EnergyUnit,
    PriceUnit,
    currency_matches,
    parse_energy_unit,
    parse_finite_decimal,
    parse_price_unit,
)

if TYPE_CHECKING:
    from . import ApplianceEnergyCostConfigEntry
    from .sensor import ApplianceCostSensor

_LOGGER = logging.getLogger(__name__)

PREVIEW_SCHEMA: Final = vol.Schema(
    {
        vol.Required(ATTR_CONFIG_ENTRY): ConfigEntrySelector(
            ConfigEntrySelectorConfig(integration=DOMAIN)
        ),
        vol.Required(ATTR_START): cv.datetime,
        vol.Optional(ATTR_END): cv.datetime,
        vol.Optional(ATTR_APPLIANCES): vol.All(cv.ensure_list, [cv.entity_id], vol.Length(min=1)),
        vol.Optional(ATTR_STRICT, default=True): cv.boolean,
    }
)

# Field parity with PREVIEW_SCHEMA is structural: the import schema EXTENDS
# the preview schema, so a pasted preview call plus ``confirm: true`` is a
# valid import call by construction.
IMPORT_SCHEMA: Final = PREVIEW_SCHEMA.extend(
    {
        # The value check lives in the handler, not the schema: a schema
        # failure renders as a generic voluptuous error, the handler raises
        # the translated explanation of what confirm authorises.
        vol.Optional(ATTR_CONFIRM, default=False): cv.boolean,
        vol.Optional(ATTR_OVERWRITE_EXISTING, default=False): cv.boolean,
        # Deliberately no schema default: the continuity gate must
        # distinguish an ABSENT initial_cost from an explicit 0. Negative
        # values are legal — negative prices yield negative cumulative cost.
        vol.Optional(ATTR_INITIAL_COST): vol.Coerce(float),
    }
)

# A plain dict on purpose: the platform-entity-service helper wraps it with
# ``cv.make_entity_service_schema`` (target fields included). ``value`` is
# required with NO default — a defaulted 0 would turn a bare call into an
# accidental full reset. Negative values are legal (negative prices legally
# yield a negative cumulative cost); finiteness is the handler's translated
# check, exactly like ``initial_cost``.
CALIBRATE_SCHEMA: Final[VolDictType] = {vol.Required(ATTR_VALUE): vol.Coerce(float)}

_EPOCH: Final = datetime.fromtimestamp(0, tz=UTC)

_MIRRORED_METADATA_FIELDS: Final = (
    "mean_type",
    "has_sum",
    "name",
    "unit_class",
    "unit_of_measurement",
)


@dataclass(frozen=True, slots=True)
class _ApplianceSelection:
    """One appliance selected for the preview, resolved before any I/O."""

    config: ApplianceConfig
    statistic_id: str


@dataclass(frozen=True, slots=True)
class _AppliancePreview:
    """One selected appliance with its narrowed rows, ready to compute."""

    selection: _ApplianceSelection
    energy_rows: tuple[EnergyRow, ...]


@dataclass(frozen=True, slots=True)
class _ApplianceImport:
    """One appliance's computed import: summary, series, and write payload.

    ``payload`` is a CONCRETE list, never a generator: the recorder requeues
    a failed import task with the same iterable, and an exhausted generator
    would silently retry an empty write.
    """

    selection: _ApplianceSelection
    summary: dict[str, JsonValueType]
    series: BackfillSeries
    payload: list[StatisticData]


def hours_to_contiguous_ranges(
    hours: tuple[datetime, ...],
) -> tuple[tuple[datetime, datetime], ...]:
    """Collapse sorted top-of-hour instants into contiguous ``[start, end)`` ranges.

    ``end`` is exclusive: the last hour of a run contributes ``hour + 1h``.
    Pure function; the input order is preserved as produced by the domain
    (ascending by construction in ``build_backfill_series``).
    """
    ranges: list[tuple[datetime, datetime]] = []
    for hour in hours:
        if ranges and ranges[-1][1] == hour:
            ranges[-1] = (ranges[-1][0], hour + timedelta(hours=1))
        else:
            ranges.append((hour, hour + timedelta(hours=1)))
    return tuple(ranges)


def _ranges_payload(hours: tuple[datetime, ...]) -> list[JsonValueType]:
    """Serialise flagged hours as contiguous UTC ranges, capped at ``RANGE_CAP``.

    The sibling count field self-describes truncation: when the count exceeds
    the hours covered by the ranges shown, the list was capped.
    """
    return [
        {"start": range_start.isoformat(), "end": range_end.isoformat()}
        for range_start, range_end in hours_to_contiguous_ranges(hours)[:RANGE_CAP]
    ]


def _require_top_of_hour(field: str, value: datetime) -> None:
    """Reject a period boundary that is not a top-of-hour UTC instant."""
    if value.minute != 0 or value.second != 0 or value.microsecond != 0:
        raise ServiceValidationError(
            translation_domain=DOMAIN,
            translation_key="period_not_top_of_hour",
            translation_placeholders={"field": field, "value": value.isoformat()},
        )


def _resolve_period(call: ServiceCall) -> tuple[datetime, datetime]:
    """Normalise the requested period to aware-UTC top-of-hour boundaries.

    ``dt_util.as_utc`` is the single conversion point: an aware value is
    converted, a naive value is interpreted in Home Assistant's configured
    timezone. The default end is the start of the current UTC hour, so a
    partial hour is never included.
    """
    now = dt_util.utcnow()
    start = dt_util.as_utc(call.data[ATTR_START])
    _require_top_of_hour(ATTR_START, start)
    raw_end: datetime | None = call.data.get(ATTR_END)
    if raw_end is None:
        end = now.replace(minute=0, second=0, microsecond=0)
    else:
        end = dt_util.as_utc(raw_end)
    _require_top_of_hour(ATTR_END, end)
    if end <= start:
        raise ServiceValidationError(
            translation_domain=DOMAIN,
            translation_key="end_not_after_start",
            translation_placeholders={"start": start.isoformat(), "end": end.isoformat()},
        )
    if end > now:
        raise ServiceValidationError(
            translation_domain=DOMAIN,
            translation_key="end_in_future",
            translation_placeholders={"end": end.isoformat(), "now": now.isoformat()},
        )
    return start, end


def _selected_appliances(
    hass: HomeAssistant,
    entry: ApplianceEnergyCostConfigEntry,
    call: ServiceCall,
) -> tuple[_ApplianceSelection, ...]:
    """Resolve the entry's appliances and apply the optional filter.

    Fail-closed by design: a structurally damaged subentry or a missing cost
    entity fails the whole call, and the ``appliances`` filter is the
    documented recovery path for previewing the healthy appliances.
    """
    registry = er.async_get(hass)
    selections: list[_ApplianceSelection] = []
    for subentry_id, subentry in entry.subentries.items():
        if subentry.subentry_type != SUBENTRY_TYPE_APPLIANCE:
            continue
        try:
            config = decode_appliance_config(subentry.title, subentry.data)
        except ValueError as err:
            raise ServiceValidationError(
                translation_domain=DOMAIN,
                translation_key="appliance_subentry_damaged",
                translation_placeholders={"subentry": subentry.title or subentry_id},
            ) from err
        statistic_id = registry.async_get_entity_id(SENSOR_DOMAIN, DOMAIN, subentry_id)
        if statistic_id is None:
            raise ServiceValidationError(
                translation_domain=DOMAIN,
                translation_key="appliance_subentry_damaged",
                translation_placeholders={"subentry": subentry.title or subentry_id},
            )
        selections.append(_ApplianceSelection(config=config, statistic_id=statistic_id))
    requested: list[str] | None = call.data.get(ATTR_APPLIANCES)
    if requested is None:
        return tuple(selections)
    known = {selection.config.energy_sensor for selection in selections}
    unknown = [entity_id for entity_id in requested if entity_id not in known]
    if unknown:
        raise ServiceValidationError(
            translation_domain=DOMAIN,
            translation_key="unknown_appliances",
            translation_placeholders={"entity_ids": ", ".join(unknown)},
        )
    requested_set = set(requested)
    return tuple(s for s in selections if s.config.energy_sensor in requested_set)


def _fetch_statistics(
    hass: HomeAssistant,
    start: datetime,
    end: datetime,
    statistic_ids: set[str],
) -> tuple[dict[str, tuple[int, StatisticMetaData]], dict[str, list[StatisticsRow]]]:
    """Read metadata and hourly rows in one pass. Runs on the recorder executor.

    Energy-class series are converted to kWh by the recorder via the units
    parameter; a compound price unit has no converter, so price rows arrive
    in the stored metadata unit and are converted by the domain.
    """
    metadata = get_metadata(hass, statistic_ids=statistic_ids)
    stats = statistics_during_period(
        hass,
        start,
        end,
        statistic_ids,
        "hour",
        {"energy": EnergyUnit.KWH.value},
        {"change", "mean", "state"},
    )
    return metadata, stats


def _validated_price_unit(
    metadata: dict[str, tuple[int, StatisticMetaData]],
    runtime: EntryRuntimeData,
) -> PriceUnit:
    """Resolve the price unit from statistics METADATA, never the live state.

    The stored series is priced in the unit its metadata declares; the live
    entity may have been renamed, re-unitted, or deleted since the history
    was recorded.
    """
    price_metadata = metadata.get(runtime.price_sensor)
    if price_metadata is None:
        raise ServiceValidationError(
            translation_domain=DOMAIN,
            translation_key="price_sensor_no_statistics",
            translation_placeholders={"price_sensor": runtime.price_sensor},
        )
    unit_raw = price_metadata[1]["unit_of_measurement"]
    price_unit = parse_price_unit(unit_raw)
    if price_unit is None:
        raise ServiceValidationError(
            translation_domain=DOMAIN,
            translation_key="price_statistics_unit_unsupported",
            translation_placeholders={
                "unit": str(unit_raw or ""),
                "price_sensor": runtime.price_sensor,
            },
        )
    if not currency_matches(price_unit.numerator, runtime.currency):
        raise ServiceValidationError(
            translation_domain=DOMAIN,
            translation_key="price_statistics_currency_mismatch",
            translation_placeholders={
                "numerator": price_unit.numerator,
                "currency": runtime.currency,
                "price_sensor": runtime.price_sensor,
            },
        )
    return price_unit


def _validate_energy_metadata(
    metadata: dict[str, tuple[int, StatisticMetaData]],
    selections: tuple[_ApplianceSelection, ...],
) -> None:
    """Require statistics metadata with a supported unit for every appliance.

    Fail-closed whole-call errors; the ``appliances`` filter is the
    documented recovery path (see ``_selected_appliances``).
    """
    for selection in selections:
        energy_metadata = metadata.get(selection.config.energy_sensor)
        if energy_metadata is None:
            raise ServiceValidationError(
                translation_domain=DOMAIN,
                translation_key="appliance_no_statistics",
                translation_placeholders={
                    "appliance": selection.config.name,
                    "energy_sensor": selection.config.energy_sensor,
                },
            )
        unit_raw = energy_metadata[1]["unit_of_measurement"]
        if parse_energy_unit(unit_raw) is None:
            raise ServiceValidationError(
                translation_domain=DOMAIN,
                translation_key="energy_statistics_unit_unsupported",
                translation_placeholders={
                    "unit": str(unit_raw or ""),
                    "appliance": selection.config.name,
                    "energy_sensor": selection.config.energy_sensor,
                },
            )


def _narrow_energy_rows(rows: list[StatisticsRow]) -> tuple[EnergyRow, ...]:
    """Rebuild recorder rows as domain energy rows — decode at the edge."""
    return tuple(
        EnergyRow(start=row["start"], change=row.get("change"), state=row.get("state"))
        for row in rows
    )


def _narrow_price_rows(rows: list[StatisticsRow]) -> tuple[PriceRow, ...]:
    """Rebuild recorder rows as domain price rows — decode at the edge."""
    return tuple(
        PriceRow(start=row["start"], mean=row.get("mean"), state=row.get("state")) for row in rows
    )


def _appliance_summary(
    preview: _AppliancePreview,
    series: BackfillSeries,
    expected_hours: int,
) -> dict[str, JsonValueType]:
    """Build one appliance's response entry; Decimal→float happens here, once.

    No rounding: the floats are the exact values the import would write.
    """
    points = len(series.points)
    missing_price = len(series.missing_price_hours)
    invalid_energy = len(series.invalid_energy_hours)
    # BINDING (issue #5, from issue #2 design review): hours with no energy
    # row produce no point and no flag in the domain output by design, so the
    # gap count is derived here as expected - emitted - flagged.
    energy_gap_hours = expected_hours - points - missing_price - invalid_energy
    return {
        ATTR_APPLIANCE: preview.selection.config.name,
        CONF_ENERGY_SENSOR: preview.selection.config.energy_sensor,
        ATTR_STATISTIC_ID: preview.selection.statistic_id,
        # The energy gap deliberately does not affect validity: absent hours
        # are reported, not treated as source-data corruption.
        ATTR_VALID: missing_price == 0 and invalid_energy == 0,
        ATTR_HOURLY_POINTS: points,
        ATTR_FIRST_POINT: series.points[0].start.isoformat() if series.points else None,
        ATTR_LAST_POINT: series.points[-1].start.isoformat() if series.points else None,
        ATTR_TOTAL_ENERGY_KWH: float(series.total_energy_kwh),
        ATTR_TOTAL_COST: float(series.total_cost),
        ATTR_END_ENERGY_KWH: (
            None if series.end_energy_kwh is None else float(series.end_energy_kwh)
        ),
        ATTR_MISSING_PRICE_HOURS: missing_price,
        ATTR_MISSING_PRICE_RANGES: _ranges_payload(series.missing_price_hours),
        ATTR_INVALID_ENERGY_HOURS: invalid_energy,
        ATTR_INVALID_ENERGY_RANGES: _ranges_payload(series.invalid_energy_hours),
        ATTR_ENERGY_GAP_HOURS: energy_gap_hours,
    }


def _compute_series_summaries(
    previews: tuple[_AppliancePreview, ...],
    price_rows: tuple[PriceRow, ...],
    price_unit: EnergyUnit,
    expected_hours: int,
    initial_cost: Decimal,
) -> tuple[tuple[dict[str, JsonValueType], BackfillSeries], ...]:
    """Run the pure series calculation for every selected appliance.

    The single compute shared by preview and import — identical inputs yield
    an identical series, so a confirmed import writes exactly what the
    preview showed. Runs on the general executor: the Decimal arithmetic
    over a long period times many appliances is CPU-bound and must stay off
    the event loop. Energy rows arrive in kWh (recorder-converted); price
    rows arrive in the stored metadata unit and are converted by the domain
    via ``price_unit``. Returns one (summary, series) pair per appliance, in
    ``previews`` order.
    """
    results: list[tuple[dict[str, JsonValueType], BackfillSeries]] = []
    for preview in previews:
        series = build_backfill_series(
            preview.energy_rows,
            price_rows,
            energy_unit=EnergyUnit.KWH,
            price_unit=price_unit,
            initial_cost=initial_cost,
        )
        results.append((_appliance_summary(preview, series, expected_hours), series))
    return tuple(results)


async def _async_handle_preview(call: ServiceCall) -> ServiceResponse:
    """Handle one ``preview_backfill`` call.

    The preview never raises on validation findings in the data itself
    (missing prices, invalid energy, gaps) — those are reported in the
    response; ``ok`` reflects them only when ``strict`` is on.
    """
    hass = call.hass
    entry: ApplianceEnergyCostConfigEntry = service.async_get_config_entry(
        hass, DOMAIN, call.data[ATTR_CONFIG_ENTRY]
    )
    runtime = entry.runtime_data
    start, end = _resolve_period(call)
    expected_hours = (end - start) // timedelta(hours=1)
    strict = bool(call.data[ATTR_STRICT])
    selections = _selected_appliances(hass, entry, call)

    statistic_ids = {runtime.price_sensor} | {
        selection.config.energy_sensor for selection in selections
    }
    metadata, stats = await get_instance(hass).async_add_executor_job(
        _fetch_statistics, hass, start, end, statistic_ids
    )

    price_unit = _validated_price_unit(metadata, runtime)
    _validate_energy_metadata(metadata, selections)

    previews = tuple(
        _AppliancePreview(
            selection=selection,
            energy_rows=_narrow_energy_rows(stats.get(selection.config.energy_sensor, [])),
        )
        for selection in selections
    )
    price_rows = _narrow_price_rows(stats.get(runtime.price_sensor, []))
    pairs = await hass.async_add_executor_job(
        _compute_series_summaries,
        previews,
        price_rows,
        price_unit.denominator,
        expected_hours,
        Decimal("0"),
    )
    summaries: list[JsonValueType] = [summary for summary, _ in pairs]
    all_valid = all(
        not series.missing_price_hours and not series.invalid_energy_hours for _, series in pairs
    )

    return {
        ATTR_START: start.isoformat(),
        ATTR_END: end.isoformat(),
        ATTR_EXPECTED_HOURS: expected_hours,
        ATTR_STRICT: strict,
        # ok means the source data passed validation AT PREVIEW TIME; the
        # import (issue #6) separately enforces overlap protection and
        # re-reads history at import time.
        ATTR_OK: all_valid if strict else True,
        CONF_CURRENCY: runtime.currency,
        CONF_PRICE_SENSOR: runtime.price_sensor,
        ATTR_APPLIANCES: summaries,
    }


def _compute_import_payloads(
    previews: tuple[_AppliancePreview, ...],
    price_rows: tuple[PriceRow, ...],
    price_unit: EnergyUnit,
    expected_hours: int,
    initial_cost: Decimal,
) -> tuple[_ApplianceImport, ...]:
    """Run the shared series calculation and build the write payloads.

    Runs on the general executor: payload construction iterates potentially
    years of hourly points on top of the Decimal arithmetic.
    """
    pairs = _compute_series_summaries(
        previews, price_rows, price_unit, expected_hours, initial_cost
    )
    return tuple(
        _ApplianceImport(
            selection=preview.selection,
            summary=summary,
            series=series,
            payload=[
                StatisticData(start=point.start, state=point.state, sum=point.sum)
                for point in series.points
            ],
        )
        for preview, (summary, series) in zip(previews, pairs, strict=True)
    )


def _last_pre_start_sums(
    hass: HomeAssistant,
    start: datetime,
    cost_ids: set[str],
) -> dict[str, float | None]:
    """Resolve each cost id's last statistics row before ``start``, bounded.

    Two bounded reads instead of an unbounded hourly scan from the epoch:
    monthly buckets for the full local-calendar months strictly before the
    month containing ``start`` (the monthly reduce carries each bucket's
    last row's sum), plus hourly rows for the partial month ``[month start,
    start)``. The monthly buckets MUST be filtered: core aligns a monthly
    read's boundaries outward to whole local months, so the bucket
    containing ``start`` can include rows at or after ``start`` and cannot
    be trusted. ``get_last_statistics`` cannot answer this either — the live
    series' last row may sit after the import window.

    A key is present iff the cost id has any row before ``start``; the
    value is that last row's sum.
    """
    month_start_local = dt_util.as_local(start).replace(
        day=1, hour=0, minute=0, second=0, microsecond=0
    )
    month_start = dt_util.as_utc(month_start_local)
    month_start_ts = month_start.timestamp()
    monthly = statistics_during_period(hass, _EPOCH, start, cost_ids, "month", None, {"sum"})
    hourly = (
        statistics_during_period(hass, month_start, start, cost_ids, "hour", None, {"sum"})
        if month_start < start
        else {}
    )
    last_sums: dict[str, float | None] = {}
    for statistic_id in cost_ids:
        partial_month_rows = hourly.get(statistic_id, [])
        full_month_rows = [
            row for row in monthly.get(statistic_id, []) if row["start"] < month_start_ts
        ]
        if partial_month_rows:
            last_sums[statistic_id] = partial_month_rows[-1].get("sum")
        elif full_month_rows:
            last_sums[statistic_id] = full_month_rows[-1].get("sum")
    return last_sums


def _fetch_import_statistics(
    hass: HomeAssistant,
    start: datetime,
    end: datetime,
    source_ids: set[str],
    cost_ids: set[str],
) -> tuple[
    dict[str, tuple[int, StatisticMetaData]],
    dict[str, list[StatisticsRow]],
    dict[str, list[StatisticsRow]],
    dict[str, float | None],
]:
    """Read everything the import gates need in one recorder-executor pass.

    Returns metadata over sources and cost ids, the sources' hourly rows in
    ``[start, end)`` (as the preview reads them), the cost ids' existing
    hourly rows in ``[start, end)`` (overlap gate; overwrite receipt), and
    each cost id's last pre-start sum (continuity gate).
    """
    metadata = get_metadata(hass, statistic_ids=source_ids | cost_ids)
    source_stats = statistics_during_period(
        hass,
        start,
        end,
        source_ids,
        "hour",
        {"energy": EnergyUnit.KWH.value},
        {"change", "mean", "state"},
    )
    cost_stats = statistics_during_period(hass, start, end, cost_ids, "hour", None, {"sum"})
    pre_start_sums = _last_pre_start_sums(hass, start, cost_ids)
    return metadata, source_stats, cost_stats, pre_start_sums


def _read_back_cost_stats(
    hass: HomeAssistant,
    start: datetime,
    end: datetime,
    cost_ids: set[str],
) -> dict[str, list[StatisticsRow]]:
    """Re-read the cost ids' committed rows. Runs on the recorder executor."""
    return statistics_during_period(hass, start, end, cost_ids, "hour", None, {"sum"})


def _cost_metadata(statistic_id: str, currency: str) -> StatisticMetaData:
    """The cost series' metadata, mirroring the live-compiled series exactly.

    Every field explicit: omitting ``mean_type`` or ``unit_class`` triggers
    a 2026.11 deprecation report in core, and any drift from the live
    sensor's compiled metadata would split one series in two (the full-field
    guard in ``_require_metadata_mirror`` enforces the same shape on read).
    """
    return StatisticMetaData(
        mean_type=StatisticMeanType.NONE,
        has_sum=True,
        name=None,
        source=RECORDER_DOMAIN,
        statistic_id=statistic_id,
        unit_class=None,
        unit_of_measurement=currency,
    )


def _row_start_iso(row: StatisticsRow) -> str:
    """Render a statistics row's start (epoch seconds, UTC) as ISO UTC."""
    return datetime.fromtimestamp(round(row["start"]), tz=UTC).isoformat()


def _point_keys(series: BackfillSeries) -> set[int]:
    """The series' point starts as integer epoch keys (row alignment keys)."""
    return {round(point.start.timestamp()) for point in series.points}


def _require_single_appliance_for_initial_cost(
    selections: tuple[_ApplianceSelection, ...],
) -> None:
    """``initial_cost`` continues exactly one series; more selected is an error."""
    if len(selections) != 1:
        raise ServiceValidationError(
            translation_domain=DOMAIN,
            translation_key="initial_cost_requires_single_appliance",
            translation_placeholders={
                "count": str(len(selections)),
                "appliances": ", ".join(selection.config.energy_sensor for selection in selections),
            },
        )


def _require_strict_clean(imports: tuple[_ApplianceImport, ...]) -> None:
    """Gate: with ``strict`` on, any validation finding aborts the whole call."""
    findings = [
        f"{item.selection.config.name}:"
        f" {len(item.series.missing_price_hours)} missing-price hours,"
        f" {len(item.series.invalid_energy_hours)} invalid-energy hours"
        for item in imports
        if item.series.missing_price_hours or item.series.invalid_energy_hours
    ]
    if findings:
        raise ServiceValidationError(
            translation_domain=DOMAIN,
            translation_key="import_strict_findings",
            translation_placeholders={"findings": "; ".join(findings)},
        )


def _require_points(imports: tuple[_ApplianceImport, ...], start: datetime, end: datetime) -> None:
    """Gate: a period with no importable points anywhere is refused, not a no-op."""
    if not any(item.series.points for item in imports):
        raise ServiceValidationError(
            translation_domain=DOMAIN,
            translation_key="nothing_to_import",
            translation_placeholders={"start": start.isoformat(), "end": end.isoformat()},
        )


def _require_no_overlap(
    imports: tuple[_ApplianceImport, ...],
    cost_stats: dict[str, list[StatisticsRow]],
) -> None:
    """Gate: any existing cost row in ``[start, end)`` blocks the whole import."""
    overlaps = [
        f"{item.selection.config.name} ({item.selection.statistic_id}):"
        f" {len(rows)} rows from {_row_start_iso(rows[0])} to {_row_start_iso(rows[-1])}"
        for item in imports
        if (rows := cost_stats.get(item.selection.statistic_id))
    ]
    if overlaps:
        raise ServiceValidationError(
            translation_domain=DOMAIN,
            translation_key="import_overlap",
            translation_placeholders={"overlaps": "; ".join(overlaps)},
        )


def _require_continuity(
    imports: tuple[_ApplianceImport, ...],
    pre_start_sums: dict[str, float | None],
    start: datetime,
) -> None:
    """Gate: rows before ``start`` need an explicit ``initial_cost`` to continue.

    Called only when ``initial_cost`` is absent — importing on top of an
    existing series with the default 0 would step the cumulative sum
    backwards at ``start``. An explicit ``initial_cost: 0`` passes.
    """
    details = [
        f"{item.selection.config.name} ({item.selection.statistic_id}):"
        f" last pre-start sum {pre_start_sums[item.selection.statistic_id]}"
        for item in imports
        if item.selection.statistic_id in pre_start_sums
    ]
    if details:
        raise ServiceValidationError(
            translation_domain=DOMAIN,
            translation_key="import_discontinuity",
            translation_placeholders={"start": start.isoformat(), "details": "; ".join(details)},
        )


def _require_metadata_mirror(
    imports: tuple[_ApplianceImport, ...],
    metadata: dict[str, tuple[int, StatisticMetaData]],
    currency: str,
) -> None:
    """Gate: existing cost-id metadata must match the mirror in every field.

    Prevents silently relabeling an existing series: a differing
    ``unit_of_measurement``, ``mean_type``, ``has_sum``, ``name`` or
    ``unit_class`` means the statistic id currently holds a series of a
    different shape, and the import's metadata upsert would rewrite it.
    """
    for item in imports:
        existing = metadata.get(item.selection.statistic_id)
        if existing is None:
            continue
        existing_map: Mapping[str, object] = existing[1]
        mirror_map: Mapping[str, object] = _cost_metadata(item.selection.statistic_id, currency)
        differences = [
            f"{field} {existing_map.get(field)!r} != {mirror_map[field]!r}"
            for field in _MIRRORED_METADATA_FIELDS
            if existing_map.get(field) != mirror_map[field]
        ]
        if differences:
            raise ServiceValidationError(
                translation_domain=DOMAIN,
                translation_key="cost_statistics_metadata_mismatch",
                translation_placeholders={
                    "statistic_id": item.selection.statistic_id,
                    "differences": "; ".join(differences),
                },
            )


def _verify_written(
    imports: tuple[_ApplianceImport, ...],
    read_back: dict[str, list[StatisticsRow]],
) -> dict[str, int]:
    """Count committed rows per statistic id from the re-read; raise on any gap.

    A confirmed row is a re-read row whose start matches a written point's
    start — on overwrite, pre-existing rows the new series did not touch
    never inflate the count.
    """
    confirmed: dict[str, int] = {}
    failed: _ApplianceImport | None = None
    for item in imports:
        read_keys = {round(row["start"]) for row in read_back.get(item.selection.statistic_id, [])}
        confirmed[item.selection.statistic_id] = len(_point_keys(item.series) & read_keys)
        if failed is None and confirmed[item.selection.statistic_id] != len(item.series.points):
            failed = item
    if failed is not None:
        rows_written = ", ".join(
            f"{item.selection.config.name}:"
            f" {confirmed[item.selection.statistic_id]}/{len(item.series.points)}"
            for item in imports
        )
        raise HomeAssistantError(
            translation_domain=DOMAIN,
            translation_key="import_verification_failed",
            translation_placeholders={
                "appliance": failed.selection.config.name,
                "expected": str(len(failed.series.points)),
                "actual": str(confirmed[failed.selection.statistic_id]),
                "rows_written": rows_written,
            },
        )
    return confirmed


def _existing_rows_kept(item: _ApplianceImport, cost_stats: dict[str, list[StatisticsRow]]) -> int:
    """Pre-existing in-window rows the new series has no point for (kept as-is).

    Derived from the reads already in hand — the overlap read's starts minus
    the new points' starts — with zero extra I/O.
    """
    point_keys = _point_keys(item.series)
    return sum(
        1
        for row in cost_stats.get(item.selection.statistic_id, [])
        if round(row["start"]) not in point_keys
    )


def _appliance_receipt(
    item: _ApplianceImport,
    rows_written: int,
    existing_rows_kept: int | None,
) -> dict[str, JsonValueType]:
    """Build one appliance's import receipt from its preview-shaped summary.

    Preview vocabulary, no rounding anywhere: every figure is projected from
    the same summary the preview would return for identical inputs, with
    ``rows_written`` coming from the post-commit re-read.
    """
    receipt: dict[str, JsonValueType] = {
        ATTR_APPLIANCE: item.summary[ATTR_APPLIANCE],
        CONF_ENERGY_SENSOR: item.summary[CONF_ENERGY_SENSOR],
        ATTR_STATISTIC_ID: item.summary[ATTR_STATISTIC_ID],
        ATTR_ROWS_WRITTEN: rows_written,
        ATTR_FIRST_POINT: item.summary[ATTR_FIRST_POINT],
        ATTR_LAST_POINT: item.summary[ATTR_LAST_POINT],
        ATTR_TOTAL_ENERGY_KWH: item.summary[ATTR_TOTAL_ENERGY_KWH],
        ATTR_TOTAL_COST: item.summary[ATTR_TOTAL_COST],
        ATTR_END_ENERGY_KWH: item.summary[ATTR_END_ENERGY_KWH],
        ATTR_MISSING_PRICE_HOURS: item.summary[ATTR_MISSING_PRICE_HOURS],
        ATTR_INVALID_ENERGY_HOURS: item.summary[ATTR_INVALID_ENERGY_HOURS],
        ATTR_ENERGY_GAP_HOURS: item.summary[ATTR_ENERGY_GAP_HOURS],
    }
    if existing_rows_kept is not None:
        receipt[ATTR_EXISTING_ROWS_KEPT] = existing_rows_kept
    return receipt


async def _async_handle_import(call: ServiceCall) -> ServiceResponse:
    """Handle one ``import_backfill`` call.

    Every pre-write gate is evaluated across every selected appliance BEFORE
    anything is queued: any failure aborts the whole call with nothing
    written. The write itself is per-appliance — the recorder has no
    cross-appliance transaction — so a partial outcome is possible and is
    reported honestly by the post-commit verification; overlap protection
    makes any re-run safe (committed rows refuse, missing rows import).
    """
    hass = call.hass
    if not call.data[ATTR_CONFIRM]:
        raise ServiceValidationError(
            translation_domain=DOMAIN,
            translation_key="confirm_required",
        )
    entry: ApplianceEnergyCostConfigEntry = service.async_get_config_entry(
        hass, DOMAIN, call.data[ATTR_CONFIG_ENTRY]
    )
    runtime = entry.runtime_data
    start, end = _resolve_period(call)
    expected_hours = (end - start) // timedelta(hours=1)
    strict = bool(call.data[ATTR_STRICT])
    overwrite_existing = bool(call.data[ATTR_OVERWRITE_EXISTING])
    raw_initial_cost: float | None = call.data.get(ATTR_INITIAL_COST)
    selections = _selected_appliances(hass, entry, call)
    if raw_initial_cost is not None:
        _require_single_appliance_for_initial_cost(selections)
    # Decimal(str(...)) once at the boundary, exactly like every other float.
    initial_cost = Decimal("0") if raw_initial_cost is None else Decimal(str(raw_initial_cost))

    source_ids = {runtime.price_sensor} | {
        selection.config.energy_sensor for selection in selections
    }
    cost_ids = {selection.statistic_id for selection in selections}
    metadata, source_stats, cost_stats, pre_start_sums = await get_instance(
        hass
    ).async_add_executor_job(_fetch_import_statistics, hass, start, end, source_ids, cost_ids)

    price_unit = _validated_price_unit(metadata, runtime)
    _validate_energy_metadata(metadata, selections)

    previews = tuple(
        _AppliancePreview(
            selection=selection,
            energy_rows=_narrow_energy_rows(source_stats.get(selection.config.energy_sensor, [])),
        )
        for selection in selections
    )
    price_rows = _narrow_price_rows(source_stats.get(runtime.price_sensor, []))
    imports = await hass.async_add_executor_job(
        _compute_import_payloads,
        previews,
        price_rows,
        price_unit.denominator,
        expected_hours,
        initial_cost,
    )

    # Pre-write gates: all of them, across all selected appliances, before
    # anything is queued. Any failure means nothing was written.
    if strict:
        _require_strict_clean(imports)
    _require_points(imports, start, end)
    if not overwrite_existing:
        _require_no_overlap(imports, cost_stats)
    if raw_initial_cost is None:
        _require_continuity(imports, pre_start_sums, start)
    _require_metadata_mirror(imports, metadata, runtime.currency)

    # One synchronous no-await block: nothing can interleave between the
    # gates above and the queueing below. Zero-point appliances are skipped
    # entirely — even an empty import would still write a metadata row.
    for item in imports:
        if item.series.points:
            async_import_statistics(
                hass,
                _cost_metadata(item.selection.statistic_id, runtime.currency),
                item.payload,
            )

    # Fence before the read-back: the sync block_till_done queues a wait
    # task BEHIND the import tasks and blocks (off-loop, on an executor
    # thread) until it runs — every queued import has then committed its
    # own session. The async variant cannot be used here: it returns
    # immediately when the queue looks empty, which races an import task
    # the recorder thread has already dequeued but not yet committed.
    instance = get_instance(hass)
    await hass.async_add_executor_job(instance.block_till_done)
    read_back = await instance.async_add_executor_job(
        _read_back_cost_stats, hass, start, end, cost_ids
    )
    confirmed = _verify_written(imports, read_back)
    _LOGGER.info(
        "Imported backfill %s - %s: %s",
        start.isoformat(),
        end.isoformat(),
        "; ".join(
            f"{item.selection.statistic_id}: {confirmed[item.selection.statistic_id]} rows"
            for item in imports
        ),
    )

    if not call.return_response:
        return None
    receipts: list[JsonValueType] = [
        _appliance_receipt(
            item,
            confirmed[item.selection.statistic_id],
            _existing_rows_kept(item, cost_stats) if overwrite_existing else None,
        )
        for item in imports
    ]
    return {
        ATTR_START: start.isoformat(),
        ATTR_END: end.isoformat(),
        ATTR_STRICT: strict,
        ATTR_OVERWRITE_EXISTING: overwrite_existing,
        ATTR_INITIAL_COST: float(initial_cost),
        CONF_CURRENCY: runtime.currency,
        CONF_PRICE_SENSOR: runtime.price_sensor,
        ATTR_APPLIANCES: receipts,
    }


async def _async_handle_calibrate(
    entities: list[ApplianceCostSensor], call: ServiceCall
) -> EntityServiceResponse:
    """Handle one ``calibrate_cost`` call.

    Batched registration is the single-target enforcement mechanism: the
    handler sees the WHOLE resolved target set at once, so an area, label or
    multi-entity target can never fan one value out over many cost sensors
    (with 0, a one-call mass reset). A per-entity handler could only see one
    entity at a time and could not refuse the set. The entities are
    ``ApplianceCostSensor`` by construction — the platform-entity filter
    resolves against this integration's sensor platform only. Validation
    lives here; the state mutation is the entity's ``async_calibrate``.
    """
    if len(entities) != 1:
        raise ServiceValidationError(
            translation_domain=DOMAIN,
            translation_key="calibration_single_target",
            translation_placeholders={
                "count": str(len(entities)),
                "entity_ids": ", ".join(entity.entity_id for entity in entities),
            },
        )
    raw_value: float = call.data[ATTR_VALUE]
    # Decimal(str(...)) once at the boundary, exactly like initial_cost.
    # vol.Coerce(float) happily coerces "inf"/"nan" strings a YAML call can
    # carry, and a non-finite value can never be a cumulative cost.
    value = parse_finite_decimal(str(raw_value))
    if value is None:
        raise ServiceValidationError(
            translation_domain=DOMAIN,
            translation_key="calibration_value_not_finite",
            translation_placeholders={"value": str(raw_value)},
        )
    (entity,) = entities
    return {entity.entity_id: entity.async_calibrate(value)}


@callback
def async_setup_services(hass: HomeAssistant) -> None:
    """Register the integration's services. Called once, from ``async_setup``."""
    hass.services.async_register(
        DOMAIN,
        SERVICE_PREVIEW_BACKFILL,
        _async_handle_preview,
        schema=PREVIEW_SCHEMA,
        supports_response=SupportsResponse.ONLY,
    )
    hass.services.async_register(
        DOMAIN,
        SERVICE_IMPORT_BACKFILL,
        _async_handle_import,
        schema=IMPORT_SCHEMA,
        supports_response=SupportsResponse.OPTIONAL,
    )
    # Batched, not per-entity, so the handler can refuse a multi-entity
    # target as a whole (see _async_handle_calibrate). Verified against HA
    # 2026.7.4: helpers/service.py::async_register_batched_platform_entity_service
    # resolves targets against this integration's sensor-platform entities,
    # filters unavailable ones, and calls the handler once with the list.
    service.async_register_batched_platform_entity_service(
        hass,
        DOMAIN,
        SERVICE_CALIBRATE_COST,
        entity_domain=SENSOR_DOMAIN,
        func=_async_handle_calibrate,
        schema=CALIBRATE_SCHEMA,
        supports_response=SupportsResponse.OPTIONAL,
    )
