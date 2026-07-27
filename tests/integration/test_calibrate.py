"""Tests for the calibrate_cost entity service.

The explicit user act that joins the live cost series to imported history.
These tests pin the LEVEL AND LOCATION of the calibration jump in long-term
statistics (the cutover E2E), the single-target enforcement that closes the
mass-reset hole, the mid-gap supersede semantics, and the documented
consequences (reset-to-zero's one negative change, restore staleness).
"""

from __future__ import annotations

import json
import logging
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from freezegun.api import FrozenDateTimeFactory
from homeassistant.components.recorder.models import (
    StatisticData,
    StatisticMeanType,
    StatisticMetaData,
)
from homeassistant.components.recorder.statistics import async_import_statistics
from homeassistant.components.sensor import ATTR_STATE_CLASS
from homeassistant.config_entries import ConfigSubentryData
from homeassistant.const import (
    ATTR_ENTITY_ID,
    ATTR_FRIENDLY_NAME,
    ATTR_UNIT_OF_MEASUREMENT,
    STATE_UNAVAILABLE,
    STATE_UNKNOWN,
)
from homeassistant.core import HomeAssistant, State
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
from homeassistant.helpers.json import json_bytes
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    mock_restore_cache_with_extra_data,
)
from pytest_homeassistant_custom_component.components.recorder.common import (
    async_wait_recording_done,
    do_adhoc_statistics,
    statistics_during_period,
)
from syrupy.assertion import SnapshotAssertion

from custom_components.appliance_energy_cost.const import (
    ATTR_APPLIANCES,
    ATTR_CONFIG_ENTRY,
    ATTR_CONFIRM,
    ATTR_END,
    ATTR_END_ENERGY_KWH,
    ATTR_NEW_BASELINE_KWH,
    ATTR_NEW_COST,
    ATTR_OLD_BASELINE_KWH,
    ATTR_OLD_COST,
    ATTR_PRICE_GAP_ACTIVE,
    ATTR_START,
    ATTR_TOTAL_COST,
    ATTR_VALUE,
    CONF_CURRENCY,
    CONF_ENERGY_SENSOR,
    CONF_PRICE_SENSOR,
    DOMAIN,
    SERVICE_CALIBRATE_COST,
    SERVICE_IMPORT_BACKFILL,
    SUBENTRY_TYPE_APPLIANCE,
)

PRICE_SENSOR = "sensor.electricity_price"
ENERGY_SENSOR = "sensor.heat_pump_energy"
ENERGY_SENSOR_B = "sensor.sauna_energy"
COST_ENTITY = "sensor.heat_pump_cost"
COST_ENTITY_B = "sensor.sauna_cost"

PERIOD_START = datetime(2026, 7, 20, 0, 0, tzinfo=UTC)


def _hour(index: int) -> datetime:
    return PERIOD_START + timedelta(hours=index)


def _price_attrs() -> dict[str, str]:
    return {
        ATTR_FRIENDLY_NAME: "Electricity price",
        ATTR_UNIT_OF_MEASUREMENT: "EUR/kWh",
    }


def _energy_attrs(unit: str = "kWh") -> dict[str, str]:
    return {
        ATTR_FRIENDLY_NAME: "Heat pump energy",
        ATTR_STATE_CLASS: "total_increasing",
        ATTR_UNIT_OF_MEASUREMENT: unit,
    }


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


def _cost(hass: HomeAssistant, entity_id: str = COST_ENTITY) -> Decimal:
    state = hass.states.get(entity_id)
    assert state is not None
    return Decimal(state.state)


async def _call_calibrate(
    hass: HomeAssistant,
    target: str | list[str],
    value: object,
    *,
    return_response: bool = True,
) -> dict[str, dict[str, object]]:
    response = await hass.services.async_call(
        DOMAIN,
        SERVICE_CALIBRATE_COST,
        {ATTR_ENTITY_ID: target, ATTR_VALUE: value},
        blocking=True,
        return_response=return_response,
    )
    if not return_response:
        assert response is None
        return {}
    assert isinstance(response, dict)
    return response


def _seed_energy(
    hass: HomeAssistant,
    statistic_id: str,
    rows: list[tuple[datetime, float, float]],
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
            unit_of_measurement="kWh",
        ),
        [StatisticData(start=start, sum=total, state=state) for start, total, state in rows],
    )


def _seed_price(hass: HomeAssistant, rows: list[tuple[datetime, float]]) -> None:
    """Seed hourly price LTS rows: (start, hourly mean price)."""
    async_import_statistics(
        hass,
        StatisticMetaData(
            has_sum=False,
            mean_type=StatisticMeanType.ARITHMETIC,
            name=None,
            source="recorder",
            statistic_id=PRICE_SENSOR,
            unit_class=None,
            unit_of_measurement="EUR/kWh",
        ),
        [StatisticData(start=start, mean=mean) for start, mean in rows],
    )


async def _accrue_one_euro(hass: HomeAssistant) -> MockConfigEntry:
    """Set up one appliance and accrue exactly 1.00 EUR (baseline 102 kWh)."""
    hass.states.async_set(PRICE_SENSOR, "0.50", _price_attrs())
    hass.states.async_set(ENERGY_SENSOR, "100.0", _energy_attrs())
    entry = await _setup_entry(hass)
    hass.states.async_set(ENERGY_SENSOR, "102.0", _energy_attrs())
    await hass.async_block_till_done()
    assert _cost(hass) == Decimal("1.00")
    return entry


async def test_cutover_calibration_compiles_at_the_calibration_hour(
    hass: HomeAssistant, freezer: FrozenDateTimeFactory
) -> None:
    """BINDING level-and-location pin for the cutover recipe.

    The calibration jump enters long-term statistics as ONE change at the
    CALIBRATION hour, and the spike already recorded at the imported-to-live
    boundary stays exactly where and what it was: calibration changes the
    level from the next compiled hour onward, it cannot move a mismatch
    already recorded at the boundary (issue #7 coexistence facts, made
    executable the way test_import_coexists_with_live_compilation did for
    the #6 facts).
    """
    freezer.move_to("2026-07-20 06:56:00+00:00")
    entry = await _setup_entry(hass)

    # Live: 1 kWh at 0.5 EUR/kWh within hour 06 — live cost 0.5.
    hass.states.async_set(PRICE_SENSOR, "0.5", _price_attrs())
    await hass.async_block_till_done()
    hass.states.async_set(ENERGY_SENSOR, "100.0", _energy_attrs())
    await hass.async_block_till_done()
    hass.states.async_set(ENERGY_SENSOR, "101.0", _energy_attrs())
    await hass.async_block_till_done()
    assert _cost(hass) == Decimal("0.5")

    # Compile the live hour BEFORE importing: the boundary spike will exist
    # in the database when the calibration happens.
    do_adhoc_statistics(hass, start=datetime(2026, 7, 20, 6, 55, tzinfo=UTC))
    await async_wait_recording_done(hass)

    # Import hours 00-04; the seeded meter story ends at 100.0 kWh, so the
    # live meter (101.0) is 1 kWh past the import's end.
    _seed_energy(hass, ENERGY_SENSOR, [(_hour(i), 1.0 + i, 97.0 + i) for i in range(4)])
    _seed_price(hass, [(_hour(i), (i + 1) / 10) for i in range(4)])
    await async_wait_recording_done(hass)
    response = await hass.services.async_call(
        DOMAIN,
        SERVICE_IMPORT_BACKFILL,
        {
            ATTR_CONFIG_ENTRY: entry.entry_id,
            ATTR_START: _hour(0).isoformat(),
            ATTR_END: _hour(4).isoformat(),
            ATTR_CONFIRM: True,
        },
        blocking=True,
        return_response=True,
    )
    assert isinstance(response, dict)
    receipts = response[ATTR_APPLIANCES]
    assert isinstance(receipts, list)
    (import_receipt,) = receipts
    assert import_receipt[ATTR_TOTAL_COST] == 1.0
    assert import_receipt[ATTR_END_ENERGY_KWH] == 100.0

    # Hour 07: the cutover formula from the import receipt —
    # value = total_cost + (current reading - end_energy_kwh) x current price.
    freezer.move_to("2026-07-20 07:56:00+00:00")
    value = import_receipt[ATTR_TOTAL_COST] + (101.0 - import_receipt[ATTR_END_ENERGY_KWH]) * 0.5
    assert value == 1.5
    calibrate_response = await _call_calibrate(hass, COST_ENTITY, value)
    receipt = calibrate_response[COST_ENTITY]
    assert receipt[ATTR_OLD_COST] == 0.5
    assert receipt[ATTR_NEW_COST] == 1.5
    assert receipt[ATTR_NEW_BASELINE_KWH] == 101.0
    assert _cost(hass) == Decimal("1.5")
    await async_wait_recording_done(hass)

    do_adhoc_statistics(hass, start=datetime(2026, 7, 20, 7, 55, tzinfo=UTC))
    await async_wait_recording_done(hass)

    stats = statistics_during_period(
        hass, _hour(0), statistic_ids={COST_ENTITY}, types={"change", "sum"}
    )
    rows = stats[COST_ENTITY]
    live_hour = datetime(2026, 7, 20, 6, 0, tzinfo=UTC)
    calibration_hour = datetime(2026, 7, 20, 7, 0, tzinfo=UTC)
    assert [row["start"] for row in rows] == [
        _hour(0).timestamp(),
        _hour(1).timestamp(),
        _hour(2).timestamp(),
        _hour(3).timestamp(),
        live_hour.timestamp(),
        calibration_hour.timestamp(),
    ]
    # LOCATION: the imported-to-live boundary spike is untouched — the
    # calibration could not and did not move it.
    boundary_row = rows[-2]
    assert boundary_row["sum"] == 0.5
    assert boundary_row["change"] == -0.5
    # LEVEL AND LOCATION: the jump compiles as one change at the
    # calibration hour, joining the live level onto the imported history.
    calibration_row = rows[-1]
    assert calibration_row["sum"] == 1.5
    assert calibration_row["change"] == 1.0


async def test_calibrate_during_price_gap_supersedes_unpriced_energy(
    hass: HomeAssistant,
) -> None:
    """Mid-gap calibration: the value supersedes gap-tracked unpriced energy.

    The gap persists across the calibration (visible in the receipt), and
    the superseded energy is never charged when the price returns — the
    gap-end settlement runs from the calibrated baseline.
    """
    await _accrue_one_euro(hass)

    hass.states.async_set(PRICE_SENSOR, STATE_UNAVAILABLE, _price_attrs())
    await hass.async_block_till_done()
    hass.states.async_set(ENERGY_SENSOR, "104.0", _energy_attrs())
    await hass.async_block_till_done()
    # 2 kWh tracked during the gap, nothing charged yet.
    assert _cost(hass) == Decimal("1.00")

    response = await _call_calibrate(hass, COST_ENTITY, 10.0)
    receipt = response[COST_ENTITY]
    assert receipt[ATTR_PRICE_GAP_ACTIVE] is True
    assert receipt[ATTR_OLD_COST] == 1.0
    assert receipt[ATTR_NEW_COST] == 10.0
    assert receipt[ATTR_OLD_BASELINE_KWH] == 102.0
    assert receipt[ATTR_NEW_BASELINE_KWH] == 104.0
    assert _cost(hass) == Decimal("10")
    state = hass.states.get(COST_ENTITY)
    assert state is not None
    assert state.attributes[ATTR_PRICE_GAP_ACTIVE] is True

    # The gap ends: the superseded 2 kWh settles to NOTHING from the new
    # baseline; only post-calibration consumption is charged.
    hass.states.async_set(PRICE_SENSOR, "0.20", _price_attrs())
    await hass.async_block_till_done()
    assert _cost(hass) == Decimal("10")
    hass.states.async_set(ENERGY_SENSOR, "105.0", _energy_attrs())
    await hass.async_block_till_done()
    assert _cost(hass) == Decimal("10.2")


async def test_unknown_source_state_refuses_with_calibration_source_unusable(
    hass: HomeAssistant,
) -> None:
    """An unknown energy state refuses the calibration, naming the recovery."""
    await _accrue_one_euro(hass)
    hass.states.async_set(ENERGY_SENSOR, STATE_UNKNOWN, _energy_attrs())
    await hass.async_block_till_done()

    with pytest.raises(ServiceValidationError) as excinfo:
        await _call_calibrate(hass, COST_ENTITY, 5.0)
    assert excinfo.value.translation_domain == DOMAIN
    assert excinfo.value.translation_key == "calibration_source_unusable"
    placeholders = excinfo.value.translation_placeholders
    assert placeholders is not None
    assert placeholders["sensor"] == ENERGY_SENSOR
    assert placeholders["state"] == STATE_UNKNOWN
    assert _cost(hass) == Decimal("1.00")


@pytest.mark.parametrize(
    ("bad_state", "unit"),
    [("nan", "kWh"), ("not-a-number", "kWh"), ("104.0", "MW")],
)
async def test_non_numeric_or_bad_unit_source_refuses(
    hass: HomeAssistant, bad_state: str, unit: str
) -> None:
    """Non-numeric states and unparseable units are the same refusal."""
    await _accrue_one_euro(hass)
    hass.states.async_set(ENERGY_SENSOR, bad_state, _energy_attrs(unit))
    await hass.async_block_till_done()

    with pytest.raises(ServiceValidationError) as excinfo:
        await _call_calibrate(hass, COST_ENTITY, 5.0)
    assert excinfo.value.translation_key == "calibration_source_unusable"
    assert _cost(hass) == Decimal("1.00")


async def test_unavailable_source_is_filtered_by_core(hass: HomeAssistant) -> None:
    """Core filters unavailable targets before the handler (pinned behaviour).

    The cost sensor mirrors its energy source's unavailability, so with
    return_response the call errors on the empty match, and without it the
    call is a silent no-op — either way nothing changes.
    """
    await _accrue_one_euro(hass)
    hass.states.async_set(ENERGY_SENSOR, STATE_UNAVAILABLE, _energy_attrs())
    await hass.async_block_till_done()
    state = hass.states.get(COST_ENTITY)
    assert state is not None
    assert state.state == STATE_UNAVAILABLE

    with pytest.raises(HomeAssistantError, match="did not match any entities"):
        await _call_calibrate(hass, COST_ENTITY, 5.0)
    await _call_calibrate(hass, COST_ENTITY, 5.0, return_response=False)

    hass.states.async_set(ENERGY_SENSOR, "102.0", _energy_attrs())
    await hass.async_block_till_done()
    # Neither call calibrated anything.
    assert _cost(hass) == Decimal("1.00")


async def test_multi_target_refuses_and_changes_neither(hass: HomeAssistant) -> None:
    """More than one targeted cost sensor refuses as a whole.

    One calibration value belongs to one meter: fanning a single value (here
    the reset value 0) over several cost sensors would be a one-call mass
    reset — the exact hole the batched single-target gate closes.
    """
    hass.states.async_set(PRICE_SENSOR, "0.50", _price_attrs())
    hass.states.async_set(ENERGY_SENSOR, "100.0", _energy_attrs())
    hass.states.async_set(ENERGY_SENSOR_B, "50.0", _energy_attrs())
    await _setup_entry(hass, appliances=(("Heat pump", ENERGY_SENSOR), ("Sauna", ENERGY_SENSOR_B)))
    hass.states.async_set(ENERGY_SENSOR, "102.0", _energy_attrs())
    hass.states.async_set(ENERGY_SENSOR_B, "51.0", _energy_attrs())
    await hass.async_block_till_done()
    assert _cost(hass) == Decimal("1.00")
    assert _cost(hass, COST_ENTITY_B) == Decimal("0.50")

    with pytest.raises(ServiceValidationError) as excinfo:
        await _call_calibrate(hass, [COST_ENTITY, COST_ENTITY_B], 0.0)
    assert excinfo.value.translation_domain == DOMAIN
    assert excinfo.value.translation_key == "calibration_single_target"
    placeholders = excinfo.value.translation_placeholders
    assert placeholders is not None
    assert placeholders["count"] == "2"

    assert _cost(hass) == Decimal("1.00")
    assert _cost(hass, COST_ENTITY_B) == Decimal("0.50")


async def test_negative_value_is_accepted_and_accrual_continues(
    hass: HomeAssistant,
) -> None:
    """Negative values are legal (negative prices yield negative totals)."""
    await _accrue_one_euro(hass)

    response = await _call_calibrate(hass, COST_ENTITY, -2.5)
    receipt = response[COST_ENTITY]
    assert receipt[ATTR_NEW_COST] == -2.5
    assert _cost(hass) == Decimal("-2.5")

    # Subsequent accrual continues from the calibrated value and baseline.
    hass.states.async_set(ENERGY_SENSOR, "104.0", _energy_attrs())
    await hass.async_block_till_done()
    assert _cost(hass) == Decimal("-1.5")


@pytest.mark.parametrize("bad_value", ["inf", "nan", "-Infinity"])
async def test_non_finite_value_refuses(hass: HomeAssistant, bad_value: str) -> None:
    """A non-finite value can never be a cumulative cost; nothing changes."""
    await _accrue_one_euro(hass)

    with pytest.raises(ServiceValidationError) as excinfo:
        await _call_calibrate(hass, COST_ENTITY, bad_value)
    assert excinfo.value.translation_domain == DOMAIN
    assert excinfo.value.translation_key == "calibration_value_not_finite"
    assert _cost(hass) == Decimal("1.00")


async def test_negative_source_reading_refuses_with_state_unchanged(
    hass: HomeAssistant,
) -> None:
    """A negative reading is broken system state: HomeAssistantError, no change."""
    await _accrue_one_euro(hass)
    hass.states.async_set(ENERGY_SENSOR, "-5.0", _energy_attrs())
    await hass.async_block_till_done()
    assert _cost(hass) == Decimal("1.00")

    with pytest.raises(HomeAssistantError) as excinfo:
        await _call_calibrate(hass, COST_ENTITY, 9.0)
    # System state, not caller input: deliberately NOT a validation error.
    assert not isinstance(excinfo.value, ServiceValidationError)
    assert excinfo.value.translation_domain == DOMAIN
    assert excinfo.value.translation_key == "calibration_source_reading_negative"
    assert _cost(hass) == Decimal("1.00")

    # Cost and baseline both untouched: the recovery leg charges from the
    # held 102 kWh baseline, exactly as if no calibration was attempted.
    hass.states.async_set(ENERGY_SENSOR, "103.0", _energy_attrs())
    await hass.async_block_till_done()
    assert _cost(hass) == Decimal("1.50")


def _next_hour_boundary() -> datetime:
    """The next UTC hour boundary strictly after now (never moves time back)."""
    now = dt_util.utcnow()
    return now.replace(minute=0, second=0, microsecond=0) + timedelta(hours=1)


async def _compile_hour(hass: HomeAssistant, hour_start: datetime) -> None:
    """Compile the 5-minute slot at the hour start plus the hourly rollup."""
    do_adhoc_statistics(hass, start=hour_start)
    do_adhoc_statistics(hass, start=hour_start + timedelta(minutes=55))
    await async_wait_recording_done(hass)


async def test_calibrate_to_zero_records_one_negative_change_in_statistics(
    hass: HomeAssistant, freezer: FrozenDateTimeFactory
) -> None:
    """The documented reset consequence, executable.

    Calibrating to 0 on a sensor with a prior compiled sum records the whole
    accumulated cost as ONE negative change in long-term statistics — the
    sentence the service description carries, pinned.
    """
    hour_one = _next_hour_boundary()
    hour_two = hour_one + timedelta(hours=1)
    freezer.move_to(hour_one)
    hass.states.async_set(PRICE_SENSOR, "0.50", _price_attrs())
    hass.states.async_set(ENERGY_SENSOR, "100.0", _energy_attrs())
    await _setup_entry(hass)
    freezer.move_to(hour_one + timedelta(minutes=1))
    hass.states.async_set(ENERGY_SENSOR, "102.0", _energy_attrs())
    await hass.async_block_till_done()
    assert _cost(hass) == Decimal("1.00")
    await async_wait_recording_done(hass)

    freezer.move_to(hour_two + timedelta(minutes=1))
    response = await _call_calibrate(hass, COST_ENTITY, 0)
    assert response[COST_ENTITY][ATTR_NEW_COST] == 0.0
    assert _cost(hass) == Decimal("0")
    await async_wait_recording_done(hass)

    freezer.move_to(hour_two + timedelta(hours=1))
    await _compile_hour(hass, hour_one)
    await _compile_hour(hass, hour_two)

    stats = statistics_during_period(
        hass, hour_one, statistic_ids={COST_ENTITY}, types={"change", "sum"}
    )
    rows = stats[COST_ENTITY]
    assert len(rows) == 2
    assert rows[0]["sum"] == pytest.approx(1.0)
    assert rows[1]["sum"] == pytest.approx(0.0)
    assert rows[1]["change"] == pytest.approx(-1.0)


async def test_restore_round_trip_keeps_calibrated_cost_and_baseline(
    hass: HomeAssistant,
) -> None:
    """A reload after calibrating restores the calibrated cost AND baseline."""
    entry = await _accrue_one_euro(hass)
    await _call_calibrate(hass, COST_ENTITY, 7.5)
    assert _cost(hass) == Decimal("7.5")

    await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done()
    assert _cost(hass) == Decimal("7.5")

    # The calibrated 102 kWh baseline survived: the same reading charges
    # nothing, the next delta charges from it.
    hass.states.async_set(ENERGY_SENSOR, "102.0", _energy_attrs())
    await hass.async_block_till_done()
    assert _cost(hass) == Decimal("7.5")
    hass.states.async_set(ENERGY_SENSOR, "103.0", _energy_attrs())
    await hass.async_block_till_done()
    assert _cost(hass) == Decimal("8.0")


async def test_crash_staleness_restores_the_pre_calibration_payload(
    hass: HomeAssistant,
) -> None:
    """Documented-honest crash window: a stale restore payload wins.

    RestoreEntity saves periodically (up to ~15 minutes apart): a crash
    between a calibration and the next save restores the pre-calibration
    payload — the old cost returns, honestly, rather than a fabricated one.
    """
    mock_restore_cache_with_extra_data(
        hass,
        [
            (
                State(COST_ENTITY, "5.000"),
                {
                    "cost": "5.000",
                    "last_energy_kwh": "100.0",
                    "energy_sensor": ENERGY_SENSOR,
                },
            )
        ],
    )
    hass.states.async_set(PRICE_SENSOR, "0.50", _price_attrs())
    hass.states.async_set(ENERGY_SENSOR, "100.0", _energy_attrs())
    await _setup_entry(hass)
    assert _cost(hass) == Decimal("5.000")


async def test_targeting_foreign_or_bogus_entities_matches_nothing(
    hass: HomeAssistant,
) -> None:
    """Another integration's sensor or a bogus id never matches this service."""
    await _accrue_one_euro(hass)
    hass.states.async_set("sensor.foreign_cost", "9.9", {ATTR_UNIT_OF_MEASUREMENT: "EUR"})

    for target in ("sensor.foreign_cost", "sensor.does_not_exist"):
        with pytest.raises(HomeAssistantError, match="did not match any entities"):
            await _call_calibrate(hass, target, 5.0)
        await _call_calibrate(hass, target, 5.0, return_response=False)

    assert _cost(hass) == Decimal("1.00")
    foreign = hass.states.get("sensor.foreign_cost")
    assert foreign is not None
    assert foreign.state == "9.9"


async def test_receipt_snapshot_and_info_log(
    hass: HomeAssistant, snapshot: SnapshotAssertion, caplog: pytest.LogCaptureFixture
) -> None:
    """The full receipt, pinned and JSON round-trippable; the act logs at INFO."""
    caplog.set_level(logging.INFO)
    hass.states.async_set(PRICE_SENSOR, "0.50", _price_attrs())
    hass.states.async_set(ENERGY_SENSOR, "100.0", _energy_attrs())
    await _setup_entry(hass)
    hass.states.async_set(ENERGY_SENSOR, "103.0", _energy_attrs())
    await hass.async_block_till_done()
    assert _cost(hass) == Decimal("1.50")

    response = await _call_calibrate(hass, COST_ENTITY, 2.25)

    assert response == snapshot
    assert json.loads(json_bytes(response)) == response

    calibrated_logs = [
        record
        for record in caplog.records
        if record.levelno == logging.INFO and record.message.startswith("Calibrated")
    ]
    assert len(calibrated_logs) == 1
    message = calibrated_logs[0].message
    assert COST_ENTITY in message
    assert "1.50" in message  # the old cost
    assert "2.25" in message  # the new cost
    assert "103.0" in message  # the new baseline
