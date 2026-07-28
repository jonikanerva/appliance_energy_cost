"""Tests for the import_backfill ``calibrate`` flag (issue #42, one-call backfill).

These tests pin the whole calibration contract: the cutover value and its
single-mutation entity path, every gate in order (hour gate, zero-rows,
end-reading, entity, entity pre-checks), the skip constants as visible
receipt values with WARNING logs, the post-import summary line carrying the
outcome, and the two absolute rules — a calibration failure never masks the
succeeded import, and a failed verification means no calibration runs.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from unittest.mock import patch

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
    ATTR_FRIENDLY_NAME,
    ATTR_UNIT_OF_MEASUREMENT,
    STATE_UNAVAILABLE,
    STATE_UNKNOWN,
)
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from pytest_homeassistant_custom_component.common import MockConfigEntry
from pytest_homeassistant_custom_component.components.recorder.common import (
    async_wait_recording_done,
)
from pytest_homeassistant_custom_component.components.recorder.common import (
    statistics_during_period as read_statistics,
)

from custom_components.appliance_energy_cost.const import (
    ATTR_APPLIANCES,
    ATTR_CALIBRATE,
    ATTR_CALIBRATED_TO,
    ATTR_CALIBRATION_SKIPPED,
    ATTR_CONFIG_ENTRY,
    ATTR_CONFIRM,
    ATTR_END,
    ATTR_ROWS_WRITTEN,
    ATTR_START,
    CONF_CURRENCY,
    CONF_ENERGY_SENSOR,
    CONF_PRICE_SENSOR,
    DOMAIN,
    SERVICE_IMPORT_BACKFILL,
    SKIP_CALIBRATION_FAILED,
    SKIP_ENTITY_UNAVAILABLE,
    SKIP_METER_DIP,
    SKIP_NO_END_ENERGY,
    SKIP_NO_ROWS,
    SKIP_PRICE_GAP,
    SKIP_READING_NEGATIVE,
    SKIP_READING_UNUSABLE,
    SKIP_STALE_HOUR,
    SKIP_VALUE_NOT_FINITE,
    SUBENTRY_TYPE_APPLIANCE,
)
from custom_components.appliance_energy_cost.sensor import ApplianceCostSensor
from custom_components.appliance_energy_cost.services import _cost_sensor_entity

PRICE_SENSOR = "sensor.electricity_price"
ENERGY_SENSOR = "sensor.heat_pump_energy"
ENERGY_SENSOR_B = "sensor.sauna_energy"
COST_ENTITY = "sensor.heat_pump_cost"
COST_ENTITY_B = "sensor.sauna_cost"

PERIOD_START = datetime(2026, 7, 20, 0, 0, tzinfo=UTC)
# Frozen "now" for every test: 06:56, so the current hour start is 06:00 and
# an import ending at _hour(6) passes the hour gate.
NOW = "2026-07-20 06:56:00+00:00"
CURRENT_HOUR_END = PERIOD_START + timedelta(hours=6)


def _hour(index: int) -> datetime:
    return PERIOD_START + timedelta(hours=index)


def _price_attrs() -> dict[str, str]:
    return {
        ATTR_FRIENDLY_NAME: "Electricity price",
        ATTR_UNIT_OF_MEASUREMENT: "EUR/kWh",
    }


def _energy_attrs() -> dict[str, str]:
    return {
        ATTR_FRIENDLY_NAME: "Heat pump energy",
        ATTR_STATE_CLASS: "total_increasing",
        ATTR_UNIT_OF_MEASUREMENT: "kWh",
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


def _seed_energy(
    hass: HomeAssistant,
    statistic_id: str,
    rows: list[tuple[datetime, float, float | None]],
) -> None:
    """Seed hourly energy LTS rows: (start, cumulative sum, meter state or None)."""
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
        [
            StatisticData(start=start, sum=total)
            if state is None
            else StatisticData(start=start, sum=total, state=state)
            for start, total, state in rows
        ],
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


def _clean_history(hass: HomeAssistant, statistic_id: str = ENERGY_SENSOR) -> None:
    """Four clean import hours 00-03; the meter story ends at 100.0 kWh."""
    _seed_energy(hass, statistic_id, [(_hour(i), 1.0 + i, 97.0 + i) for i in range(4)])
    _seed_price(hass, [(_hour(i), (i + 1) / 10) for i in range(4)])


async def _live_sensor(
    hass: HomeAssistant,
    freezer: FrozenDateTimeFactory,
    *,
    reading: str = "101.0",
) -> MockConfigEntry:
    """Frozen at NOW, live price 0.5 EUR/kWh, meter at ``reading`` kWh, cost 0.

    The baseline initialises from the first reading, so the live cost is 0
    and any change after a calibration is the calibration's alone.
    """
    freezer.move_to(NOW)
    hass.states.async_set(PRICE_SENSOR, "0.5", _price_attrs())
    hass.states.async_set(ENERGY_SENSOR, reading, _energy_attrs())
    return await _setup_entry(hass)


def _cost(hass: HomeAssistant, entity_id: str = COST_ENTITY) -> Decimal:
    state = hass.states.get(entity_id)
    assert state is not None
    return Decimal(state.state)


async def _call_import(
    hass: HomeAssistant,
    entry: MockConfigEntry,
    **data: object,
) -> dict[str, object]:
    payload: dict[str, object] = {
        ATTR_CONFIG_ENTRY: entry.entry_id,
        ATTR_START: _hour(0).isoformat(),
        ATTR_END: CURRENT_HOUR_END.isoformat(),
        ATTR_CONFIRM: True,
        ATTR_CALIBRATE: True,
    }
    payload.update(data)
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


def _receipts(response: dict[str, object]) -> list[dict[str, object]]:
    receipts = response[ATTR_APPLIANCES]
    assert isinstance(receipts, list)
    return receipts


async def test_one_call_import_calibrates_to_the_cutover_value(
    hass: HomeAssistant, freezer: FrozenDateTimeFactory, caplog: pytest.LogCaptureFixture
) -> None:
    """The owner's one-call E2E: import + calibrate, no user math, no skew window.

    Import total 1.0 EUR ends at meter 100.0; the live meter reads 101.0 at
    0.5 EUR/kWh, so the cutover is 1.0 + (101.0 - 100.0) x 0.5 = 1.5 —
    computed from the series Decimals, applied in the same call, and the
    post-import summary line carries the outcome on the same record.
    """
    caplog.set_level(logging.INFO)
    entry = await _live_sensor(hass, freezer)
    assert _cost(hass) == Decimal("0")  # the first reading only baselines
    _clean_history(hass)
    await async_wait_recording_done(hass)

    response = await _call_import(hass, entry)
    assert response[ATTR_CALIBRATE] is True
    (receipt,) = _receipts(response)
    assert receipt[ATTR_ROWS_WRITTEN] == 4
    assert receipt[ATTR_CALIBRATED_TO] == 1.5
    assert ATTR_CALIBRATION_SKIPPED not in receipt
    assert _cost(hass) == Decimal("1.5")

    summary_lines = [
        record.message
        for record in caplog.records
        if record.levelno == logging.INFO and record.message.startswith("Imported backfill")
    ]
    assert len(summary_lines) == 1
    assert f"{COST_ENTITY}: 4 rows, calibrated to 1.50" in summary_lines[0]

    # Live accrual continues from the calibrated level and baseline.
    hass.states.async_set(ENERGY_SENSOR, "102.0", _energy_attrs())
    await hass.async_block_till_done()
    assert _cost(hass) == Decimal("2.0")


async def test_calibrate_omitted_defaults_off_and_receipt_has_no_calibration_keys(
    hass: HomeAssistant, freezer: FrozenDateTimeFactory
) -> None:
    """INVARIANT (issue #42): the vol schema default is False — services.yaml's
    default: true is a UI prefill only, so an automation omitting the flag
    never calibrates."""
    entry = await _live_sensor(hass, freezer)
    _clean_history(hass)
    await async_wait_recording_done(hass)

    response = await _call_import(hass, entry, **{ATTR_CALIBRATE: None})
    assert response[ATTR_CALIBRATE] is False
    (receipt,) = _receipts(response)
    assert receipt[ATTR_ROWS_WRITTEN] == 4
    assert ATTR_CALIBRATED_TO not in receipt
    assert ATTR_CALIBRATION_SKIPPED not in receipt
    assert _cost(hass) == Decimal("0")


async def test_stale_end_hour_skips_every_appliance(
    hass: HomeAssistant, freezer: FrozenDateTimeFactory, caplog: pytest.LogCaptureFixture
) -> None:
    """Hour gate: an end that is no longer the current hour skips ALL appliances.

    Re-resolving end to now instead was rejected — it would retro-price
    (issue #42 amendment 5). The skip names the re-run remedy verbatim.
    """
    entry = await _live_sensor(hass, freezer)
    _clean_history(hass)
    await async_wait_recording_done(hass)

    response = await _call_import(hass, entry, **{ATTR_END: _hour(4).isoformat()})
    (receipt,) = _receipts(response)
    assert receipt[ATTR_ROWS_WRITTEN] == 4
    assert receipt[ATTR_CALIBRATION_SKIPPED] == SKIP_STALE_HOUR
    assert ATTR_CALIBRATED_TO not in receipt
    assert _cost(hass) == Decimal("0")
    assert "run the same import call again" in caplog.text


async def test_zero_row_appliance_skips_while_the_other_calibrates(
    hass: HomeAssistant, freezer: FrozenDateTimeFactory
) -> None:
    """Zero-rows gate: no imported series means no level to continue.

    Calibrating the zero-row appliance to its total (the initial 0) would be
    a mass reset of a healthy live sensor — the exact hazard the gate closes.
    The healthy appliance still calibrates: skips are per appliance.
    """
    freezer.move_to(NOW)
    hass.states.async_set(PRICE_SENSOR, "0.5", _price_attrs())
    hass.states.async_set(ENERGY_SENSOR, "101.0", _energy_attrs())
    hass.states.async_set(ENERGY_SENSOR_B, "50.0", _energy_attrs())
    entry = await _setup_entry(
        hass, appliances=(("Heat pump", ENERGY_SENSOR), ("Sauna", ENERGY_SENSOR_B))
    )
    hass.states.async_set(ENERGY_SENSOR_B, "52.0", _energy_attrs())
    await hass.async_block_till_done()
    assert _cost(hass, COST_ENTITY_B) == Decimal("1.0")
    _clean_history(hass)
    # Sauna's statistics exist but only BEFORE the window: zero points inside.
    _seed_energy(hass, ENERGY_SENSOR_B, [(_hour(-4), 1.0, 51.0)])
    await async_wait_recording_done(hass)

    response = await _call_import(hass, entry)
    heat_pump, sauna = _receipts(response)
    assert heat_pump[ATTR_CALIBRATED_TO] == 1.5
    assert sauna[ATTR_ROWS_WRITTEN] == 0
    assert sauna[ATTR_CALIBRATION_SKIPPED] == SKIP_NO_ROWS
    # The mass-reset hazard did not happen: sauna's live cost is untouched.
    assert _cost(hass, COST_ENTITY_B) == Decimal("1.0")


async def test_missing_end_state_skips_with_no_end_energy(
    hass: HomeAssistant, freezer: FrozenDateTimeFactory
) -> None:
    """End-reading gate: a stateless last row means no measurable delta."""
    entry = await _live_sensor(hass, freezer)
    _seed_energy(
        hass,
        ENERGY_SENSOR,
        [(_hour(0), 1.0, 98.0), (_hour(1), 2.0, 99.0), (_hour(2), 3.0, None)],
    )
    _seed_price(hass, [(_hour(i), (i + 1) / 10) for i in range(3)])
    await async_wait_recording_done(hass)

    response = await _call_import(hass, entry)
    (receipt,) = _receipts(response)
    assert receipt[ATTR_ROWS_WRITTEN] == 3
    assert receipt[ATTR_CALIBRATION_SKIPPED] == SKIP_NO_END_ENERGY
    assert _cost(hass) == Decimal("0")


async def test_entity_gone_mid_call_degrades_to_a_skip(
    hass: HomeAssistant, freezer: FrozenDateTimeFactory
) -> None:
    """Entity gate: the unload race degrades to a visible skip, never a crash."""
    entry = await _live_sensor(hass, freezer)
    _clean_history(hass)
    await async_wait_recording_done(hass)

    # Simulate the entry unloading between the write and the calibration
    # pass: the platform lookup finds nothing.
    with patch(
        "homeassistant.helpers.entity_platform.async_get_platforms",
        return_value=[],
    ):
        response = await _call_import(hass, entry)
    (receipt,) = _receipts(response)
    assert receipt[ATTR_ROWS_WRITTEN] == 4
    assert receipt[ATTR_CALIBRATION_SKIPPED] == SKIP_ENTITY_UNAVAILABLE
    assert _cost(hass) == Decimal("0")


async def test_price_gap_at_calibration_moment_skips(
    hass: HomeAssistant, freezer: FrozenDateTimeFactory
) -> None:
    """Entity pre-check: no usable price means the delta cannot be priced."""
    freezer.move_to(NOW)
    hass.states.async_set(PRICE_SENSOR, STATE_UNAVAILABLE, _price_attrs())
    hass.states.async_set(ENERGY_SENSOR, "101.0", _energy_attrs())
    entry = await _setup_entry(hass)
    _clean_history(hass)
    await async_wait_recording_done(hass)

    response = await _call_import(hass, entry)
    (receipt,) = _receipts(response)
    assert receipt[ATTR_ROWS_WRITTEN] == 4
    assert receipt[ATTR_CALIBRATION_SKIPPED] == SKIP_PRICE_GAP
    assert _cost(hass) == Decimal("0")


async def test_unusable_reading_at_calibration_moment_skips(
    hass: HomeAssistant, freezer: FrozenDateTimeFactory
) -> None:
    """Entity pre-check: the new baseline must come from a real reading."""
    entry = await _live_sensor(hass, freezer)
    hass.states.async_set(ENERGY_SENSOR, STATE_UNKNOWN, _energy_attrs())
    await hass.async_block_till_done()
    _clean_history(hass)
    await async_wait_recording_done(hass)

    response = await _call_import(hass, entry)
    (receipt,) = _receipts(response)
    assert receipt[ATTR_CALIBRATION_SKIPPED] == SKIP_READING_UNUSABLE
    assert _cost(hass) == Decimal("0")


async def test_negative_reading_at_calibration_moment_skips(
    hass: HomeAssistant, freezer: FrozenDateTimeFactory
) -> None:
    """Entity pre-check: a negative reading must never become a baseline."""
    entry = await _live_sensor(hass, freezer)
    hass.states.async_set(ENERGY_SENSOR, "-5.0", _energy_attrs())
    await hass.async_block_till_done()
    _clean_history(hass)
    await async_wait_recording_done(hass)

    response = await _call_import(hass, entry)
    (receipt,) = _receipts(response)
    assert receipt[ATTR_CALIBRATION_SKIPPED] == SKIP_READING_NEGATIVE
    assert _cost(hass) == Decimal("0")


async def test_meter_dip_is_a_skip_not_a_calibration(
    hass: HomeAssistant, freezer: FrozenDateTimeFactory
) -> None:
    """BINDING (issue #42 amendment 1): DIP maps to a skip.

    The import's meter story ends at 100.0; the live meter reads 95.0 —
    below end but above the 90% reset line, so a dip. Calibrating it would
    re-baseline to 95.0 and charge the recovery back to 100.0 as ADVANCE —
    energy the import already costed. Nothing is calibrated.
    """
    entry = await _live_sensor(hass, freezer, reading="95.0")
    _clean_history(hass)
    await async_wait_recording_done(hass)

    response = await _call_import(hass, entry)
    (receipt,) = _receipts(response)
    assert receipt[ATTR_ROWS_WRITTEN] == 4
    assert receipt[ATTR_CALIBRATION_SKIPPED] == SKIP_METER_DIP
    assert ATTR_CALIBRATED_TO not in receipt
    assert _cost(hass) == Decimal("0")


async def test_meter_reset_calibrates_the_full_reading_as_post_reset(
    hass: HomeAssistant, freezer: FrozenDateTimeFactory
) -> None:
    """A reset after end: the full reading is consumption since the reset."""
    entry = await _live_sensor(hass, freezer, reading="50.0")
    _clean_history(hass)
    await async_wait_recording_done(hass)

    response = await _call_import(hass, entry)
    (receipt,) = _receipts(response)
    # 1.0 + 50.0 x 0.5 = 26.0
    assert receipt[ATTR_CALIBRATED_TO] == 26.0
    assert _cost(hass) == Decimal("26.0")


async def test_unchanged_reading_calibrates_to_the_import_total(
    hass: HomeAssistant, freezer: FrozenDateTimeFactory
) -> None:
    """No energy metered since end: the live sensor continues at total_cost."""
    entry = await _live_sensor(hass, freezer, reading="100.0")
    _clean_history(hass)
    await async_wait_recording_done(hass)

    response = await _call_import(hass, entry)
    (receipt,) = _receipts(response)
    assert receipt[ATTR_CALIBRATED_TO] == 1.0
    assert _cost(hass) == Decimal("1.0")


async def test_calibration_error_degrades_to_a_skip_and_keeps_the_import(
    hass: HomeAssistant, freezer: FrozenDateTimeFactory
) -> None:
    """Post-commit rule: a raising calibration NEVER masks the succeeded import."""
    entry = await _live_sensor(hass, freezer)
    _clean_history(hass)
    await async_wait_recording_done(hass)

    with patch.object(
        ApplianceCostSensor,
        "async_calibrate_from_import",
        side_effect=HomeAssistantError("simulated calibration failure"),
    ):
        response = await _call_import(hass, entry)
    (receipt,) = _receipts(response)
    assert receipt[ATTR_ROWS_WRITTEN] == 4
    assert receipt[ATTR_CALIBRATION_SKIPPED] == SKIP_CALIBRATION_FAILED
    assert _cost(hass) == Decimal("0")

    await async_wait_recording_done(hass)
    rows = read_statistics(hass, _hour(0), statistic_ids={COST_ENTITY}, types={"sum"})
    assert len(rows[COST_ENTITY]) == 4


async def test_failed_verification_runs_no_calibration(
    hass: HomeAssistant, freezer: FrozenDateTimeFactory
) -> None:
    """If _verify_written raises, NO calibration runs (issue #42 gate order)."""
    entry = await _live_sensor(hass, freezer)
    _clean_history(hass)
    await async_wait_recording_done(hass)

    with (
        patch(
            "homeassistant.components.recorder.statistics.import_statistics",
            return_value=True,  # silently write nothing: verification must fail
        ),
        pytest.raises(HomeAssistantError) as excinfo,
    ):
        await _call_import(hass, entry)
    assert excinfo.value.translation_key == "import_verification_failed"
    assert _cost(hass) == Decimal("0")


async def test_non_finite_computed_value_is_a_skip_not_an_exception(
    hass: HomeAssistant, freezer: FrozenDateTimeFactory
) -> None:
    """The computed-value finiteness pre-check (issue #42 amendment 3).

    Unreachable through the service flow (initial_cost and the ingestion
    edge are both finiteness-guarded), so the entity method is exercised
    directly: a non-finite cutover maps to the skip constant, never raises,
    and mutates nothing.
    """
    entry = await _live_sensor(hass, freezer)
    entity = _cost_sensor_entity(hass, entry.entry_id, COST_ENTITY)
    assert entity is not None

    outcome = entity.async_calibrate_from_import(Decimal("Infinity"), Decimal("100.0"))
    assert outcome == SKIP_VALUE_NOT_FINITE
    assert _cost(hass) == Decimal("0")


async def test_calibrate_narrowed_by_the_appliances_filter(
    hass: HomeAssistant, freezer: FrozenDateTimeFactory
) -> None:
    """The appliances filter narrows the calibration exactly like the import."""
    freezer.move_to(NOW)
    hass.states.async_set(PRICE_SENSOR, "0.5", _price_attrs())
    hass.states.async_set(ENERGY_SENSOR, "101.0", _energy_attrs())
    hass.states.async_set(ENERGY_SENSOR_B, "50.0", _energy_attrs())
    entry = await _setup_entry(
        hass, appliances=(("Heat pump", ENERGY_SENSOR), ("Sauna", ENERGY_SENSOR_B))
    )
    _clean_history(hass)
    await async_wait_recording_done(hass)

    response = await _call_import(hass, entry, **{ATTR_APPLIANCES: [ENERGY_SENSOR]})
    (receipt,) = _receipts(response)
    assert receipt[ATTR_CALIBRATED_TO] == 1.5
    assert _cost(hass) == Decimal("1.5")
    # The filtered-out appliance was neither imported nor calibrated.
    assert _cost(hass, COST_ENTITY_B) == Decimal("0")
