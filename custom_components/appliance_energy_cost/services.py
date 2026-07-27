"""The ``preview_backfill`` service: a dry-run summary of reconstructable history.

The only module that touches ``homeassistant.components.recorder``. It reads
hourly long-term statistics through the supported statistics APIs, narrows the
rows into domain shapes at the edge, and hands the pure calculation to
``backfill.py``. It never writes statistics and never modifies live sensors —
importing is a separate, explicitly confirmed action (issue #6).

Concurrency shape (event loop never blocks): two sequential executor jobs —
one recorder-executor pass reading metadata and rows, then one
general-executor pass running the Decimal-heavy series calculation.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Final

import voluptuous as vol
from homeassistant.components.recorder.models import StatisticMetaData
from homeassistant.components.recorder.statistics import (
    StatisticsRow,
    get_metadata,
    statistics_during_period,
)
from homeassistant.components.sensor import DOMAIN as SENSOR_DOMAIN
from homeassistant.core import (
    HomeAssistant,
    ServiceCall,
    ServiceResponse,
    SupportsResponse,
    callback,
)
from homeassistant.exceptions import ServiceValidationError
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers import service
from homeassistant.helpers.recorder import get_instance
from homeassistant.helpers.selector import ConfigEntrySelector, ConfigEntrySelectorConfig
from homeassistant.util import dt as dt_util
from homeassistant.util.json import JsonValueType

from .backfill import BackfillSeries, EnergyRow, PriceRow, build_backfill_series
from .const import (
    ATTR_APPLIANCE,
    ATTR_APPLIANCES,
    ATTR_CONFIG_ENTRY,
    ATTR_END,
    ATTR_END_ENERGY_KWH,
    ATTR_ENERGY_GAP_HOURS,
    ATTR_EXPECTED_HOURS,
    ATTR_FIRST_POINT,
    ATTR_HOURLY_POINTS,
    ATTR_INVALID_ENERGY_HOURS,
    ATTR_INVALID_ENERGY_RANGES,
    ATTR_LAST_POINT,
    ATTR_MISSING_PRICE_HOURS,
    ATTR_MISSING_PRICE_RANGES,
    ATTR_OK,
    ATTR_START,
    ATTR_STATISTIC_ID,
    ATTR_STRICT,
    ATTR_TOTAL_COST,
    ATTR_TOTAL_ENERGY_KWH,
    ATTR_VALID,
    CONF_CURRENCY,
    CONF_ENERGY_SENSOR,
    CONF_PRICE_SENSOR,
    DOMAIN,
    RANGE_CAP,
    SERVICE_PREVIEW_BACKFILL,
    SUBENTRY_TYPE_APPLIANCE,
)
from .models import ApplianceConfig, EntryRuntimeData, decode_appliance_config
from .units import EnergyUnit, PriceUnit, currency_matches, parse_energy_unit, parse_price_unit

if TYPE_CHECKING:
    from . import ApplianceEnergyCostConfigEntry

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


def _compute_previews(
    previews: tuple[_AppliancePreview, ...],
    price_rows: tuple[PriceRow, ...],
    price_unit: EnergyUnit,
    expected_hours: int,
) -> tuple[list[JsonValueType], bool]:
    """Run the pure series calculation for every selected appliance.

    Runs on the general executor: the Decimal arithmetic over a long period
    times many appliances is CPU-bound and must stay off the event loop.
    Energy rows arrive in kWh (recorder-converted); price rows arrive in the
    stored metadata unit and are converted by the domain via ``price_unit``.
    Returns the per-appliance summaries and whether every appliance is valid.
    """
    summaries: list[JsonValueType] = []
    all_valid = True
    for preview in previews:
        series = build_backfill_series(
            preview.energy_rows,
            price_rows,
            energy_unit=EnergyUnit.KWH,
            price_unit=price_unit,
        )
        if series.missing_price_hours or series.invalid_energy_hours:
            all_valid = False
        summaries.append(_appliance_summary(preview, series, expected_hours))
    return summaries, all_valid


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
    summaries, all_valid = await hass.async_add_executor_job(
        _compute_previews, previews, price_rows, price_unit.denominator, expected_hours
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
