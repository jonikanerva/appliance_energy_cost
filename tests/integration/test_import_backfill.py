"""Tests for the import_backfill service.

The most dangerous surface of the product: these tests pin that nothing is
ever written without an explicit confirm, that every pre-write gate blocks
the WHOLE call before anything is queued, that the written rows equal the
previewed series exactly, and that a partial commit is reported honestly
and is safely re-runnable.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from unittest.mock import patch

import pytest
from freezegun.api import FrozenDateTimeFactory
from homeassistant.components.recorder.models import (
    StatisticData,
    StatisticMeanType,
    StatisticMetaData,
)
from homeassistant.components.recorder.statistics import (
    async_import_statistics,
    get_metadata,
)
from homeassistant.components.recorder.statistics import (
    import_statistics as real_import_statistics,
)
from homeassistant.config_entries import ConfigSubentryData
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
from homeassistant.helpers.json import json_bytes
from pytest_homeassistant_custom_component.common import MockConfigEntry
from pytest_homeassistant_custom_component.components.recorder.common import (
    async_wait_recording_done,
    do_adhoc_statistics,
    statistics_during_period,
)
from syrupy.assertion import SnapshotAssertion

from custom_components.appliance_energy_cost.const import (
    ATTR_APPLIANCE,
    ATTR_APPLIANCES,
    ATTR_CONFIG_ENTRY,
    ATTR_CONFIRM,
    ATTR_END,
    ATTR_END_ENERGY_KWH,
    ATTR_ENERGY_GAP_HOURS,
    ATTR_EXISTING_ROWS_KEPT,
    ATTR_FIRST_POINT,
    ATTR_INITIAL_COST,
    ATTR_INVALID_ENERGY_HOURS,
    ATTR_LAST_POINT,
    ATTR_MISSING_PRICE_HOURS,
    ATTR_OVERWRITE_EXISTING,
    ATTR_ROWS_WRITTEN,
    ATTR_START,
    ATTR_STATISTIC_ID,
    ATTR_STRICT,
    ATTR_TOTAL_COST,
    ATTR_TOTAL_ENERGY_KWH,
    CONF_CURRENCY,
    CONF_ENERGY_SENSOR,
    CONF_PRICE_SENSOR,
    DOMAIN,
    SERVICE_IMPORT_BACKFILL,
    SERVICE_PREVIEW_BACKFILL,
    SUBENTRY_TYPE_APPLIANCE,
)

PRICE_SENSOR = "sensor.electricity_price"
ENERGY_SENSOR = "sensor.heat_pump_energy"
ENERGY_SENSOR_B = "sensor.sauna_energy"
COST_ENTITY = "sensor.heat_pump_cost"
COST_ENTITY_B = "sensor.sauna_cost"

PERIOD_START = datetime(2026, 7, 20, 0, 0, tzinfo=UTC)


def _hour(index: int, base: datetime = PERIOD_START) -> datetime:
    return base + timedelta(hours=index)


def _seed_energy(
    hass: HomeAssistant,
    statistic_id: str,
    rows: list[tuple[datetime, float, float]],
    unit: str = "kWh",
) -> None:
    """Seed hourly energy LTS rows: (start, cumulative sum, meter state)."""
    async_import_statistics(
        hass,
        StatisticMetaData(
            has_sum=True,
            mean_type=StatisticMeanType.NONE,
            name=None,
            source="recorder",
            statistic_id=statistic_id,
            unit_class="energy",
            unit_of_measurement=unit,
        ),
        [StatisticData(start=start, sum=total, state=state) for start, total, state in rows],
    )


def _seed_price(
    hass: HomeAssistant,
    rows: list[tuple[datetime, float]],
    unit: str = "EUR/kWh",
    statistic_id: str = PRICE_SENSOR,
) -> None:
    """Seed hourly price LTS rows: (start, hourly mean price)."""
    async_import_statistics(
        hass,
        StatisticMetaData(
            has_sum=False,
            mean_type=StatisticMeanType.ARITHMETIC,
            name=None,
            source="recorder",
            statistic_id=statistic_id,
            unit_class=None,
            unit_of_measurement=unit,
        ),
        [StatisticData(start=start, mean=mean) for start, mean in rows],
    )


def _seed_cost(
    hass: HomeAssistant,
    rows: list[tuple[datetime, float]],
    statistic_id: str = COST_ENTITY,
) -> None:
    """Seed pre-existing cost rows in the integration's own metadata shape."""
    async_import_statistics(
        hass,
        StatisticMetaData(
            has_sum=True,
            mean_type=StatisticMeanType.NONE,
            name=None,
            source="recorder",
            statistic_id=statistic_id,
            unit_class=None,
            unit_of_measurement="EUR",
        ),
        [StatisticData(start=start, sum=total, state=total) for start, total in rows],
    )


def _clean_energy_rows(base: datetime = PERIOD_START) -> list[tuple[datetime, float, float]]:
    """Four hours with a 1 kWh change each; meter ends at 104.0."""
    return [(_hour(i, base), 1.0 + i, 101.0 + i) for i in range(4)]


def _clean_price_rows(base: datetime = PERIOD_START) -> list[tuple[datetime, float]]:
    """Four hourly prices 0.1..0.4 EUR/kWh."""
    return [(_hour(i, base), (i + 1) / 10) for i in range(4)]


async def _setup_entry(
    hass: HomeAssistant,
    appliances: tuple[tuple[str, str], ...] = (("Heat pump", ENERGY_SENSOR),),
) -> MockConfigEntry:
    entry = MockConfigEntry(
        domain=DOMAIN,
        title="Electricity price",
        data={CONF_PRICE_SENSOR: PRICE_SENSOR, CONF_CURRENCY: "EUR"},
        subentries_data=[
            ConfigSubentryData(
                data={CONF_ENERGY_SENSOR: energy_sensor},
                subentry_type=SUBENTRY_TYPE_APPLIANCE,
                title=name,
                unique_id=energy_sensor,
            )
            for name, energy_sensor in appliances
        ],
    )
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    return entry


async def _call_import(
    hass: HomeAssistant,
    entry: MockConfigEntry,
    **data: object,
) -> dict[str, object]:
    payload: dict[str, object] = {
        ATTR_CONFIG_ENTRY: entry.entry_id,
        ATTR_START: _hour(0).isoformat(),
        ATTR_END: _hour(4).isoformat(),
        ATTR_CONFIRM: True,
    }
    payload.update(data)
    # A None override drops the key: the service must apply its own default.
    payload = {key: value for key, value in payload.items() if value is not None}
    response = await hass.services.async_call(
        DOMAIN,
        SERVICE_IMPORT_BACKFILL,
        payload,
        blocking=True,
        return_response=True,
    )
    assert isinstance(response, dict)
    return response


async def _call_preview(
    hass: HomeAssistant,
    entry: MockConfigEntry,
) -> dict[str, object]:
    response = await hass.services.async_call(
        DOMAIN,
        SERVICE_PREVIEW_BACKFILL,
        {
            ATTR_CONFIG_ENTRY: entry.entry_id,
            ATTR_START: _hour(0).isoformat(),
            ATTR_END: _hour(4).isoformat(),
        },
        blocking=True,
        return_response=True,
    )
    assert isinstance(response, dict)
    return response


def _receipts(response: dict[str, object]) -> list[dict[str, object]]:
    receipts = response[ATTR_APPLIANCES]
    assert isinstance(receipts, list)
    return receipts


def _cost_rows(hass: HomeAssistant, statistic_id: str = COST_ENTITY) -> list[dict[str, object]]:
    """Every hourly (start epoch, sum) row on a cost id, pre-window included."""
    stats = statistics_during_period(
        hass,
        datetime(2026, 1, 1, tzinfo=UTC),
        statistic_ids={statistic_id},
        types={"sum"},
    )
    return stats.get(statistic_id, [])


def _start_sums(rows: list[dict[str, object]]) -> list[tuple[object, object]]:
    return [(row["start"], row["sum"]) for row in rows]


async def test_clean_import_writes_previewed_rows_exactly(hass: HomeAssistant) -> None:
    """BINDING determinism pin: import receipt == preview summary, floats exact."""
    entry = await _setup_entry(hass)
    _seed_energy(hass, ENERGY_SENSOR, _clean_energy_rows())
    _seed_price(hass, _clean_price_rows())
    await async_wait_recording_done(hass)

    preview = await _call_preview(hass, entry)
    response = await _call_import(hass, entry)

    assert response[ATTR_START] == "2026-07-20T00:00:00+00:00"
    assert response[ATTR_END] == "2026-07-20T04:00:00+00:00"
    assert response[ATTR_STRICT] is True
    assert response[ATTR_OVERWRITE_EXISTING] is False
    assert response[ATTR_INITIAL_COST] == 0.0
    assert response[CONF_CURRENCY] == "EUR"
    assert response[CONF_PRICE_SENSOR] == PRICE_SENSOR

    preview_appliances = preview[ATTR_APPLIANCES]
    assert isinstance(preview_appliances, list)
    (preview_summary,) = preview_appliances
    (receipt,) = _receipts(response)
    # Identical inputs (initial_cost 0) run the same shared compute: every
    # shared figure must be EXACTLY equal, no rounding anywhere.
    for key in (
        ATTR_APPLIANCE,
        CONF_ENERGY_SENSOR,
        ATTR_STATISTIC_ID,
        ATTR_FIRST_POINT,
        ATTR_LAST_POINT,
        ATTR_TOTAL_ENERGY_KWH,
        ATTR_TOTAL_COST,
        ATTR_END_ENERGY_KWH,
        ATTR_MISSING_PRICE_HOURS,
        ATTR_INVALID_ENERGY_HOURS,
        ATTR_ENERGY_GAP_HOURS,
    ):
        assert receipt[key] == preview_summary[key]
    assert receipt[ATTR_ROWS_WRITTEN] == 4
    assert ATTR_EXISTING_ROWS_KEPT not in receipt

    await async_wait_recording_done(hass)
    assert _start_sums(_cost_rows(hass)) == [
        (_hour(0).timestamp(), 0.1),
        (_hour(1).timestamp(), 0.3),
        (_hour(2).timestamp(), 0.6),
        (_hour(3).timestamp(), 1.0),
    ]
    # The metadata mirrors the live-compiled cost series field by field.
    _, meta = get_metadata(hass, statistic_ids={COST_ENTITY})[COST_ENTITY]
    assert meta["mean_type"] == StatisticMeanType.NONE
    assert meta["has_sum"] is True
    assert meta["name"] is None
    assert meta["source"] == "recorder"
    assert meta["unit_class"] is None
    assert meta["unit_of_measurement"] == "EUR"


async def test_missing_or_false_confirm_is_rejected(hass: HomeAssistant) -> None:
    """Absent confirm and confirm: false both refuse with the translated key."""
    entry = await _setup_entry(hass)
    _seed_energy(hass, ENERGY_SENSOR, _clean_energy_rows())
    _seed_price(hass, _clean_price_rows())
    await async_wait_recording_done(hass)

    for override in ({ATTR_CONFIRM: None}, {ATTR_CONFIRM: False}):
        with pytest.raises(ServiceValidationError) as excinfo:
            await _call_import(hass, entry, **override)
        assert excinfo.value.translation_domain == DOMAIN
        assert excinfo.value.translation_key == "confirm_required"

    await async_wait_recording_done(hass)
    assert get_metadata(hass, statistic_ids={COST_ENTITY}) == {}
    assert _cost_rows(hass) == []


async def test_strict_abort_leaves_database_unchanged(hass: HomeAssistant) -> None:
    """Strict findings abort the whole call; the database is untouched."""
    entry = await _setup_entry(hass)
    _seed_energy(hass, ENERGY_SENSOR, _clean_energy_rows())
    # Prices only for hours 0 and 3 — two missing-price hours with consumption.
    _seed_price(hass, [(_hour(0), 0.1), (_hour(3), 0.4)])
    await async_wait_recording_done(hass)

    with pytest.raises(ServiceValidationError) as excinfo:
        await _call_import(hass, entry)
    assert excinfo.value.translation_key == "import_strict_findings"
    placeholders = excinfo.value.translation_placeholders
    assert placeholders is not None
    assert "Heat pump: 2 missing-price hours, 0 invalid-energy hours" in placeholders["findings"]

    await async_wait_recording_done(hass)
    assert get_metadata(hass, statistic_ids={COST_ENTITY}) == {}
    assert _cost_rows(hass) == []


async def test_empty_period_refuses_with_nothing_to_import(hass: HomeAssistant) -> None:
    """A period with zero importable points anywhere refuses, writing nothing."""
    entry = await _setup_entry(hass)
    _seed_energy(hass, ENERGY_SENSOR, _clean_energy_rows())
    _seed_price(hass, _clean_price_rows())
    await async_wait_recording_done(hass)

    with pytest.raises(ServiceValidationError) as excinfo:
        await _call_import(
            hass,
            entry,
            **{ATTR_START: _hour(48).isoformat(), ATTR_END: _hour(52).isoformat()},
        )
    assert excinfo.value.translation_key == "nothing_to_import"

    await async_wait_recording_done(hass)
    assert get_metadata(hass, statistic_ids={COST_ENTITY}) == {}


async def test_overlap_blocks_by_default_and_names_the_rows(hass: HomeAssistant) -> None:
    """An existing mid-window cost row blocks the import and names its range."""
    entry = await _setup_entry(hass)
    _seed_energy(hass, ENERGY_SENSOR, _clean_energy_rows())
    _seed_price(hass, _clean_price_rows())
    _seed_cost(hass, [(_hour(2), 123.0)])
    await async_wait_recording_done(hass)

    with pytest.raises(ServiceValidationError) as excinfo:
        await _call_import(hass, entry)
    assert excinfo.value.translation_key == "import_overlap"
    placeholders = excinfo.value.translation_placeholders
    assert placeholders is not None
    overlaps = placeholders["overlaps"]
    assert COST_ENTITY in overlaps
    assert "1 rows" in overlaps
    assert "2026-07-20T02:00:00+00:00" in overlaps

    await async_wait_recording_done(hass)
    assert _start_sums(_cost_rows(hass)) == [(_hour(2).timestamp(), 123.0)]


async def test_overlap_row_exactly_at_start_blocks(hass: HomeAssistant) -> None:
    """[start, end) is start-inclusive: a row exactly at start blocks."""
    entry = await _setup_entry(hass)
    _seed_energy(hass, ENERGY_SENSOR, _clean_energy_rows())
    _seed_price(hass, _clean_price_rows())
    _seed_cost(hass, [(_hour(0), 5.0)])
    await async_wait_recording_done(hass)

    with pytest.raises(ServiceValidationError) as excinfo:
        await _call_import(hass, entry)
    assert excinfo.value.translation_key == "import_overlap"

    await async_wait_recording_done(hass)
    assert _start_sums(_cost_rows(hass)) == [(_hour(0).timestamp(), 5.0)]


async def test_overlap_row_exactly_at_end_does_not_block(hass: HomeAssistant) -> None:
    """[start, end) is end-exclusive: a row exactly at end never blocks."""
    entry = await _setup_entry(hass)
    _seed_energy(hass, ENERGY_SENSOR, _clean_energy_rows())
    _seed_price(hass, _clean_price_rows())
    _seed_cost(hass, [(_hour(4), 999.0)])
    await async_wait_recording_done(hass)

    response = await _call_import(hass, entry)
    (receipt,) = _receipts(response)
    assert receipt[ATTR_ROWS_WRITTEN] == 4

    await async_wait_recording_done(hass)
    rows = _cost_rows(hass)
    assert _start_sums(rows)[-1] == (_hour(4).timestamp(), 999.0)
    assert _start_sums(rows)[:4] == [
        (_hour(0).timestamp(), 0.1),
        (_hour(1).timestamp(), 0.3),
        (_hour(2).timestamp(), 0.6),
        (_hour(3).timestamp(), 1.0),
    ]


async def test_overwrite_updates_matching_hours_and_keeps_the_rest(hass: HomeAssistant) -> None:
    """Overwrite updates only the hours the new series has points for.

    A stale in-window row at a gap hour SURVIVES (there is no deletion
    path) and is counted in existing_rows_kept; rows outside the window are
    never touched.
    """
    entry = await _setup_entry(hass)
    # Energy hole at hour 2: the new series has points for hours 0, 1, 3.
    _seed_energy(
        hass,
        ENERGY_SENSOR,
        [(_hour(0), 1.0, 101.0), (_hour(1), 2.0, 102.0), (_hour(3), 4.0, 104.0)],
    )
    _seed_price(hass, _clean_price_rows())
    _seed_cost(
        hass,
        [
            (_hour(0), 100.0),
            (_hour(1), 200.0),
            (_hour(2), 300.0),
            (_hour(3), 400.0),
            (_hour(5), 500.0),
        ],
    )
    await async_wait_recording_done(hass)

    with pytest.raises(ServiceValidationError):
        await _call_import(hass, entry)  # blocked without the explicit exception

    response = await _call_import(hass, entry, **{ATTR_OVERWRITE_EXISTING: True})
    assert response[ATTR_OVERWRITE_EXISTING] is True
    (receipt,) = _receipts(response)
    assert receipt[ATTR_ROWS_WRITTEN] == 3
    assert receipt[ATTR_EXISTING_ROWS_KEPT] == 1
    assert receipt[ATTR_ENERGY_GAP_HOURS] == 1

    await async_wait_recording_done(hass)
    # Hour 3's change is 2 kWh (recorder sum delta across the hole) at 0.4.
    assert _start_sums(_cost_rows(hass)) == [
        (_hour(0).timestamp(), 0.1),
        (_hour(1).timestamp(), 0.3),
        (_hour(2).timestamp(), 300.0),
        (_hour(3).timestamp(), 1.1),
        (_hour(5).timestamp(), 500.0),
    ]


async def test_initial_cost_offsets_every_row(hass: HomeAssistant) -> None:
    """initial_cost offsets every state/sum; one appliance, no filter, passes."""
    entry = await _setup_entry(hass)
    _seed_energy(hass, ENERGY_SENSOR, _clean_energy_rows())
    _seed_price(hass, _clean_price_rows())
    await async_wait_recording_done(hass)

    response = await _call_import(hass, entry, **{ATTR_INITIAL_COST: 10.5})
    assert response[ATTR_INITIAL_COST] == 10.5
    (receipt,) = _receipts(response)
    # total_cost is the cumulative cost at end — the offset included.
    assert receipt[ATTR_TOTAL_COST] == 11.5

    await async_wait_recording_done(hass)
    assert _start_sums(_cost_rows(hass)) == [
        (_hour(0).timestamp(), 10.6),
        (_hour(1).timestamp(), 10.8),
        (_hour(2).timestamp(), 11.1),
        (_hour(3).timestamp(), 11.5),
    ]


async def test_negative_initial_cost_is_accepted(hass: HomeAssistant) -> None:
    """Negative prices legally yield negative cumulative cost: no positive gate."""
    entry = await _setup_entry(hass)
    _seed_energy(hass, ENERGY_SENSOR, _clean_energy_rows())
    _seed_price(hass, _clean_price_rows())
    await async_wait_recording_done(hass)

    response = await _call_import(hass, entry, **{ATTR_INITIAL_COST: -5.0})
    assert response[ATTR_INITIAL_COST] == -5.0

    await async_wait_recording_done(hass)
    assert _start_sums(_cost_rows(hass)) == [
        (_hour(0).timestamp(), -4.9),
        (_hour(1).timestamp(), -4.7),
        (_hour(2).timestamp(), -4.4),
        (_hour(3).timestamp(), -4.0),
    ]


async def test_initial_cost_requires_a_single_appliance(hass: HomeAssistant) -> None:
    """Two resolved appliances with initial_cost refuse; the filter recovers."""
    entry = await _setup_entry(
        hass, appliances=(("Heat pump", ENERGY_SENSOR), ("Sauna", ENERGY_SENSOR_B))
    )
    _seed_energy(hass, ENERGY_SENSOR, _clean_energy_rows())
    _seed_price(hass, _clean_price_rows())
    await async_wait_recording_done(hass)

    with pytest.raises(ServiceValidationError) as excinfo:
        await _call_import(hass, entry, **{ATTR_INITIAL_COST: 1.0})
    assert excinfo.value.translation_key == "initial_cost_requires_single_appliance"
    placeholders = excinfo.value.translation_placeholders
    assert placeholders is not None
    assert placeholders["count"] == "2"

    # One import call per appliance is the documented remedy.
    response = await _call_import(
        hass,
        entry,
        **{ATTR_INITIAL_COST: 1.0, ATTR_APPLIANCES: [ENERGY_SENSOR]},
    )
    (receipt,) = _receipts(response)
    assert receipt[ATTR_APPLIANCE] == "Heat pump"
    assert receipt[ATTR_ROWS_WRITTEN] == 4


async def test_pre_start_rows_require_explicit_initial_cost(hass: HomeAssistant) -> None:
    """Continuity gate: pre-start rows abort unless initial_cost is explicit.

    An explicit initial_cost: 0 is a conscious decision and passes — the
    gate distinguishes ABSENT from zero.
    """
    entry = await _setup_entry(hass)
    _seed_energy(hass, ENERGY_SENSOR, _clean_energy_rows())
    _seed_price(hass, _clean_price_rows())
    _seed_cost(hass, [(_hour(-2), 40.0), (_hour(-1), 50.0)])
    await async_wait_recording_done(hass)

    with pytest.raises(ServiceValidationError) as excinfo:
        await _call_import(hass, entry)
    assert excinfo.value.translation_key == "import_discontinuity"
    placeholders = excinfo.value.translation_placeholders
    assert placeholders is not None
    # The last pre-start sum is named so the user can continue the series.
    assert "50.0" in placeholders["details"]

    await async_wait_recording_done(hass)
    assert _start_sums(_cost_rows(hass)) == [
        (_hour(-2).timestamp(), 40.0),
        (_hour(-1).timestamp(), 50.0),
    ]

    response = await _call_import(hass, entry, **{ATTR_INITIAL_COST: 0})
    (receipt,) = _receipts(response)
    assert receipt[ATTR_ROWS_WRITTEN] == 4


async def test_previous_month_rows_trip_the_continuity_gate(hass: HomeAssistant) -> None:
    """Pre-start rows ONLY in a previous local month still trip the gate.

    The partial-month hourly read finds nothing, so the gate must fall back
    to the monthly-bucket path and name the last full-month sum.
    """
    entry = await _setup_entry(hass)
    _seed_energy(hass, ENERGY_SENSOR, _clean_energy_rows())
    _seed_price(hass, _clean_price_rows())
    june = datetime(2026, 6, 15, 10, 0, tzinfo=UTC)
    _seed_cost(hass, [(june, 7.0), (june + timedelta(hours=1), 8.0)])
    await async_wait_recording_done(hass)

    with pytest.raises(ServiceValidationError) as excinfo:
        await _call_import(hass, entry)
    assert excinfo.value.translation_key == "import_discontinuity"
    placeholders = excinfo.value.translation_placeholders
    assert placeholders is not None
    assert "8.0" in placeholders["details"]

    await async_wait_recording_done(hass)
    assert _start_sums(_cost_rows(hass)) == [
        (june.timestamp(), 7.0),
        ((june + timedelta(hours=1)).timestamp(), 8.0),
    ]


async def test_continuity_gate_at_exact_local_month_start(hass: HomeAssistant) -> None:
    """A start exactly at a local month boundary skips the empty hourly read.

    The timezone is pinned to UTC because the test harness defaults to
    US/Pacific, where 2026-07-01 00:00 UTC is NOT a local month start —
    unpinned, the hourly read would drift and satisfy the gate. Pinned,
    the partial month [month start, start) is empty by construction, so
    the previous month's last row must be found via the monthly buckets
    alone; an explicit initial_cost then continues the series across the
    boundary.
    """
    await hass.config.async_set_time_zone("UTC")
    entry = await _setup_entry(hass)
    month_start = datetime(2026, 7, 1, 0, 0, tzinfo=UTC)
    window = {
        ATTR_START: month_start.isoformat(),
        ATTR_END: (month_start + timedelta(hours=4)).isoformat(),
    }
    _seed_energy(hass, ENERGY_SENSOR, _clean_energy_rows(month_start))
    _seed_price(hass, _clean_price_rows(month_start))
    _seed_cost(hass, [(month_start - timedelta(hours=1), 12.5)])  # 2026-06-30 23:00
    await async_wait_recording_done(hass)

    with pytest.raises(ServiceValidationError) as excinfo:
        await _call_import(hass, entry, **window)
    assert excinfo.value.translation_key == "import_discontinuity"
    placeholders = excinfo.value.translation_placeholders
    assert placeholders is not None
    assert "12.5" in placeholders["details"]

    response = await _call_import(hass, entry, **window, **{ATTR_INITIAL_COST: 12.5})
    (receipt,) = _receipts(response)
    assert receipt[ATTR_ROWS_WRITTEN] == 4
    await async_wait_recording_done(hass)
    assert _start_sums(_cost_rows(hass)) == [
        ((month_start - timedelta(hours=1)).timestamp(), 12.5),
        (month_start.timestamp(), 12.6),
        ((month_start + timedelta(hours=1)).timestamp(), 12.8),
        ((month_start + timedelta(hours=2)).timestamp(), 13.1),
        ((month_start + timedelta(hours=3)).timestamp(), 13.5),
    ]


async def test_post_end_same_month_rows_do_not_trip_the_continuity_gate(
    hass: HomeAssistant,
) -> None:
    """No-leak counterpart: a post-end row in start's month never trips the gate.

    Core aligns the monthly read outward to whole local months, so the
    bucket containing start also covers this row — the gate must not
    mistake it for pre-start history.
    """
    entry = await _setup_entry(hass)
    _seed_energy(hass, ENERGY_SENSOR, _clean_energy_rows())
    _seed_price(hass, _clean_price_rows())
    _seed_cost(hass, [(_hour(30), 77.0)])  # 2026-07-21 06:00, after end, same month
    await async_wait_recording_done(hass)

    response = await _call_import(hass, entry)
    (receipt,) = _receipts(response)
    assert receipt[ATTR_ROWS_WRITTEN] == 4

    await async_wait_recording_done(hass)
    assert _start_sums(_cost_rows(hass)) == [
        (_hour(0).timestamp(), 0.1),
        (_hour(1).timestamp(), 0.3),
        (_hour(2).timestamp(), 0.6),
        (_hour(3).timestamp(), 1.0),
        (_hour(30).timestamp(), 77.0),
    ]


async def test_divergent_metadata_blocks_and_stays_untouched(hass: HomeAssistant) -> None:
    """Full-field metadata guard: a foreign-shaped series is never relabeled."""
    entry = await _setup_entry(hass)
    _seed_energy(hass, ENERGY_SENSOR, _clean_energy_rows())
    _seed_price(hass, _clean_price_rows())
    # A price-shaped series already lives on the cost id, with its rows
    # outside the window (after end) so the overlap and continuity gates
    # pass and the metadata guard is the one that must refuse.
    _seed_price(hass, [(_hour(6), 0.5)], statistic_id=COST_ENTITY)
    await async_wait_recording_done(hass)

    with pytest.raises(ServiceValidationError) as excinfo:
        await _call_import(hass, entry)
    assert excinfo.value.translation_key == "cost_statistics_metadata_mismatch"
    placeholders = excinfo.value.translation_placeholders
    assert placeholders is not None
    assert placeholders["statistic_id"] == COST_ENTITY
    differences = placeholders["differences"]
    assert "mean_type" in differences
    assert "has_sum" in differences
    assert "unit_of_measurement" in differences

    await async_wait_recording_done(hass)
    # The divergent metadata is untouched and nothing landed in the window.
    _, meta = get_metadata(hass, statistic_ids={COST_ENTITY})[COST_ENTITY]
    assert meta["mean_type"] == StatisticMeanType.ARITHMETIC
    assert meta["has_sum"] is False
    assert meta["unit_of_measurement"] == "EUR/kWh"
    in_window = [row for row in _cost_rows(hass) if row["start"] < _hour(4).timestamp()]
    assert in_window == []


async def test_one_blocked_appliance_blocks_all(hass: HomeAssistant) -> None:
    """All-or-nothing gates: nothing is written for ANY appliance on abort."""
    entry = await _setup_entry(
        hass, appliances=(("Heat pump", ENERGY_SENSOR), ("Sauna", ENERGY_SENSOR_B))
    )
    _seed_energy(hass, ENERGY_SENSOR, _clean_energy_rows())
    _seed_energy(hass, ENERGY_SENSOR_B, [(_hour(i), 2.0 + 2 * i, 51.0 + i) for i in range(4)])
    _seed_price(hass, _clean_price_rows())
    _seed_cost(hass, [(_hour(1), 9.0)], statistic_id=COST_ENTITY_B)
    await async_wait_recording_done(hass)

    with pytest.raises(ServiceValidationError) as excinfo:
        await _call_import(hass, entry)
    assert excinfo.value.translation_key == "import_overlap"

    await async_wait_recording_done(hass)
    # The healthy appliance was not written either.
    assert get_metadata(hass, statistic_ids={COST_ENTITY}) == {}
    assert _cost_rows(hass, COST_ENTITY) == []
    assert _start_sums(_cost_rows(hass, COST_ENTITY_B)) == [(_hour(1).timestamp(), 9.0)]


async def test_partial_commit_is_reported_honestly_and_recoverable(
    hass: HomeAssistant,
) -> None:
    """A permanently failing per-appliance write is named, and re-runs are safe.

    The write is per-appliance (no cross-appliance transaction): the
    verification failure names written vs unwritten counts, the appliances
    filter re-runs the unwritten one, and overlap protection blocks a plain
    re-run from double-writing the committed one.
    """
    entry = await _setup_entry(
        hass, appliances=(("Heat pump", ENERGY_SENSOR), ("Sauna", ENERGY_SENSOR_B))
    )
    _seed_energy(hass, ENERGY_SENSOR, _clean_energy_rows())
    _seed_energy(hass, ENERGY_SENSOR_B, [(_hour(i), 2.0 + 2 * i, 51.0 + i) for i in range(4)])
    _seed_price(hass, _clean_price_rows())
    await async_wait_recording_done(hass)

    def _fail_for_sauna(instance: object, metadata: StatisticMetaData, *args: object) -> bool:
        if metadata["statistic_id"] == COST_ENTITY_B:
            raise RuntimeError("simulated permanent import failure")
        return real_import_statistics(instance, metadata, *args)

    with (
        patch(
            "homeassistant.components.recorder.statistics.import_statistics",
            side_effect=_fail_for_sauna,
        ),
        pytest.raises(HomeAssistantError) as excinfo,
    ):
        await _call_import(hass, entry)
    assert excinfo.value.translation_key == "import_verification_failed"
    placeholders = excinfo.value.translation_placeholders
    assert placeholders is not None
    assert placeholders["appliance"] == "Sauna"
    assert placeholders["expected"] == "4"
    assert placeholders["actual"] == "0"
    assert "Heat pump: 4/4" in placeholders["rows_written"]
    assert "Sauna: 0/4" in placeholders["rows_written"]

    await async_wait_recording_done(hass)
    assert len(_cost_rows(hass, COST_ENTITY)) == 4
    assert _cost_rows(hass, COST_ENTITY_B) == []

    # A plain re-run is blocked by overlap on the written appliance —
    # nothing is ever double-written.
    with pytest.raises(ServiceValidationError) as overlap_info:
        await _call_import(hass, entry)
    assert overlap_info.value.translation_key == "import_overlap"

    # The appliances filter re-runs the unwritten appliance successfully.
    response = await _call_import(hass, entry, **{ATTR_APPLIANCES: [ENERGY_SENSOR_B]})
    (receipt,) = _receipts(response)
    assert receipt[ATTR_APPLIANCE] == "Sauna"
    assert receipt[ATTR_ROWS_WRITTEN] == 4
    await async_wait_recording_done(hass)
    assert _start_sums(_cost_rows(hass, COST_ENTITY_B)) == [
        (_hour(0).timestamp(), 0.2),
        (_hour(1).timestamp(), 0.6),
        (_hour(2).timestamp(), 1.2),
        (_hour(3).timestamp(), 2.0),
    ]


async def test_zero_point_appliance_is_skipped_without_a_metadata_row(
    hass: HomeAssistant,
) -> None:
    """A zero-point appliance is skipped entirely: no metadata row appears."""
    entry = await _setup_entry(
        hass, appliances=(("Heat pump", ENERGY_SENSOR), ("Sauna", ENERGY_SENSOR_B))
    )
    _seed_energy(hass, ENERGY_SENSOR, _clean_energy_rows())
    # Sauna has statistics — but only before the window: zero points inside.
    _seed_energy(hass, ENERGY_SENSOR_B, [(_hour(-4), 1.0, 51.0), (_hour(-3), 2.0, 52.0)])
    _seed_price(hass, _clean_price_rows())
    await async_wait_recording_done(hass)

    response = await _call_import(hass, entry)
    heat_pump, sauna = _receipts(response)
    assert heat_pump[ATTR_ROWS_WRITTEN] == 4
    assert sauna[ATTR_ROWS_WRITTEN] == 0
    assert sauna[ATTR_FIRST_POINT] is None
    assert sauna[ATTR_LAST_POINT] is None

    await async_wait_recording_done(hass)
    # An empty import would still have created a metadata row: it must not.
    assert get_metadata(hass, statistic_ids={COST_ENTITY_B}) == {}
    assert _cost_rows(hass, COST_ENTITY_B) == []


async def test_import_coexists_with_live_compilation(
    hass: HomeAssistant, freezer: FrozenDateTimeFactory
) -> None:
    """Import + live compile: no crash; boundary change == live - imported sum.

    Pins the issue #7 coexistence facts: the live sum lineage reads only the
    short-term table (imported LTS rows never feed it), so the first
    live-compiled hour after imported history shows
    change == live_sum - imported_sum.
    """
    freezer.move_to("2026-07-20 06:56:00+00:00")
    entry = await _setup_entry(hass)

    # The live cost sensor accrues 0.5 EUR within hour 06.
    hass.states.async_set(PRICE_SENSOR, "0.5", {"unit_of_measurement": "EUR/kWh"})
    await hass.async_block_till_done()
    hass.states.async_set(ENERGY_SENSOR, "100.0", {"unit_of_measurement": "kWh"})
    await hass.async_block_till_done()
    hass.states.async_set(ENERGY_SENSOR, "101.0", {"unit_of_measurement": "kWh"})
    await hass.async_block_till_done()
    live_state = hass.states.get(COST_ENTITY)
    assert live_state is not None
    assert float(live_state.state) == 0.5

    _seed_energy(hass, ENERGY_SENSOR, _clean_energy_rows())
    _seed_price(hass, _clean_price_rows())
    await async_wait_recording_done(hass)
    response = await _call_import(hass, entry)
    (receipt,) = _receipts(response)
    assert receipt[ATTR_ROWS_WRITTEN] == 4

    # Compile the live hour's statistics on top of the imported history.
    do_adhoc_statistics(hass, start=datetime(2026, 7, 20, 6, 55, tzinfo=UTC))
    await async_wait_recording_done(hass)

    stats = statistics_during_period(
        hass, _hour(0), statistic_ids={COST_ENTITY}, types={"change", "sum"}
    )
    rows = stats[COST_ENTITY]
    live_hour = datetime(2026, 7, 20, 6, 0, tzinfo=UTC)
    assert [row["start"] for row in rows] == [
        _hour(0).timestamp(),
        _hour(1).timestamp(),
        _hour(2).timestamp(),
        _hour(3).timestamp(),
        live_hour.timestamp(),
    ]
    live_row = rows[-1]
    imported_last = rows[-2]
    assert imported_last["sum"] == 1.0
    assert live_row["sum"] == 0.5
    # The boundary change is live_sum - imported_sum: the large negative
    # spike the calibration service (issue #7) exists to close.
    assert live_row["change"] == live_row["sum"] - imported_last["sum"]
    assert live_row["change"] == -0.5


async def test_import_writes_no_short_term_rows(hass: HomeAssistant) -> None:
    """The import touches only the hourly long-term table, never short-term."""
    entry = await _setup_entry(hass)
    _seed_energy(hass, ENERGY_SENSOR, _clean_energy_rows())
    _seed_price(hass, _clean_price_rows())
    await async_wait_recording_done(hass)

    await _call_import(hass, entry)
    await async_wait_recording_done(hass)

    assert len(_cost_rows(hass)) == 4
    assert (
        statistics_during_period(hass, _hour(0), statistic_ids={COST_ENTITY}, period="5minute")
        == {}
    )


async def test_strict_false_skips_and_never_costs_skipped_hours(hass: HomeAssistant) -> None:
    """strict: false skip-and-report: a hole causes no sum jump, ever.

    The skipped hours' consumption is never costed — the change across the
    hole equals only the next priced hour's own cost (a permanent
    under-count, deliberately different from the live gap policy).
    """
    entry = await _setup_entry(hass)
    _seed_energy(hass, ENERGY_SENSOR, _clean_energy_rows())
    _seed_price(hass, [(_hour(0), 0.1), (_hour(3), 0.4)])
    await async_wait_recording_done(hass)

    response = await _call_import(hass, entry, **{ATTR_STRICT: False})
    assert response[ATTR_STRICT] is False
    (receipt,) = _receipts(response)
    assert receipt[ATTR_ROWS_WRITTEN] == 2
    assert receipt[ATTR_MISSING_PRICE_HOURS] == 2
    assert receipt[ATTR_TOTAL_COST] == 0.5

    await async_wait_recording_done(hass)
    stats = statistics_during_period(
        hass, _hour(0), statistic_ids={COST_ENTITY}, types={"change", "sum"}
    )
    rows = stats[COST_ENTITY]
    assert [(row["start"], row["sum"]) for row in rows] == [
        (_hour(0).timestamp(), 0.1),
        (_hour(3).timestamp(), 0.5),
    ]
    # Hour 3 contributes only its own 1 kWh x 0.4: the 2 kWh consumed in
    # the skipped hours never enter the sum.
    assert rows[1]["change"] == 0.4


async def test_full_receipt_snapshot_and_json_round_trip(
    hass: HomeAssistant, snapshot: SnapshotAssertion
) -> None:
    """One full receipt, pinned; json_bytes round-trips it Decimal-free."""
    entry = await _setup_entry(hass)
    _seed_energy(hass, ENERGY_SENSOR, _clean_energy_rows())
    _seed_price(hass, [(_hour(0), 0.1), (_hour(3), 0.4)])
    await async_wait_recording_done(hass)

    response = await _call_import(hass, entry, **{ATTR_STRICT: False})

    assert response == snapshot
    assert json.loads(json_bytes(response)) == response
