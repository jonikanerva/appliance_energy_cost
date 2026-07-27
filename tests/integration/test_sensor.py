"""Tests for the appliance cost sensor platform."""

from __future__ import annotations

import logging
from datetime import datetime, timedelta
from decimal import Decimal

import pytest
from freezegun.api import FrozenDateTimeFactory
from homeassistant.components.sensor import ATTR_STATE_CLASS
from homeassistant.config_entries import ConfigSubentryData
from homeassistant.const import (
    ATTR_FRIENDLY_NAME,
    ATTR_UNIT_OF_MEASUREMENT,
    STATE_UNAVAILABLE,
    STATE_UNKNOWN,
)
from homeassistant.core import HomeAssistant, State
from homeassistant.helpers import entity_registry as er
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

from custom_components.appliance_energy_cost.const import (
    ATTR_PRICE_GAP_ACTIVE,
    CONF_CURRENCY,
    CONF_ENERGY_SENSOR,
    CONF_PRICE_SENSOR,
    DOMAIN,
    SUBENTRY_TYPE_APPLIANCE,
)

PRICE_SENSOR = "sensor.electricity_price"
ENERGY_SENSOR = "sensor.heat_pump_energy"
ENERGY_SENSOR_B = "sensor.replacement_meter_energy"
COST_ENTITY = "sensor.heat_pump_cost"


def _price_attrs(unit: str | None = "EUR/kWh") -> dict[str, str]:
    attributes = {ATTR_FRIENDLY_NAME: "Electricity price"}
    if unit is not None:
        attributes[ATTR_UNIT_OF_MEASUREMENT] = unit
    return attributes


def _energy_attrs(unit: str | None = "kWh") -> dict[str, str]:
    attributes = {
        ATTR_FRIENDLY_NAME: "Heat pump energy",
        ATTR_STATE_CLASS: "total_increasing",
    }
    if unit is not None:
        attributes[ATTR_UNIT_OF_MEASUREMENT] = unit
    return attributes


def _make_entry(hass: HomeAssistant) -> MockConfigEntry:
    entry = MockConfigEntry(
        domain=DOMAIN,
        title="Electricity price",
        data={CONF_PRICE_SENSOR: PRICE_SENSOR, CONF_CURRENCY: "EUR"},
        subentries_data=[
            ConfigSubentryData(
                data={CONF_ENERGY_SENSOR: ENERGY_SENSOR},
                subentry_type=SUBENTRY_TYPE_APPLIANCE,
                title="Heat pump",
                unique_id=ENERGY_SENSOR,
            )
        ],
    )
    entry.add_to_hass(hass)
    return entry


async def _setup_entry(hass: HomeAssistant) -> MockConfigEntry:
    entry = _make_entry(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    return entry


def _cost(hass: HomeAssistant) -> Decimal:
    state = hass.states.get(COST_ENTITY)
    assert state is not None
    return Decimal(state.state)


def _integration_records(caplog: pytest.LogCaptureFixture, level: int) -> list[logging.LogRecord]:
    """This integration's log records at exactly the given level."""
    return [
        record
        for record in caplog.records
        if record.levelno == level
        and record.name.startswith("custom_components.appliance_energy_cost")
    ]


async def test_accrues_at_the_price_in_force(hass: HomeAssistant) -> None:
    """The core loop: delta kWh times the active price, exact in Decimal."""
    hass.states.async_set(PRICE_SENSOR, "0.50", _price_attrs())
    hass.states.async_set(ENERGY_SENSOR, "100.0", _energy_attrs())
    await _setup_entry(hass)
    assert _cost(hass) == Decimal("0")

    hass.states.async_set(ENERGY_SENSOR, "102.0", _energy_attrs())
    await hass.async_block_till_done()
    assert _cost(hass) == Decimal("1.00")

    state = hass.states.get(COST_ENTITY)
    assert state is not None
    assert state.attributes[ATTR_UNIT_OF_MEASUREMENT] == "EUR"
    assert state.attributes[CONF_ENERGY_SENSOR] == ENERGY_SENSOR
    assert state.attributes[CONF_PRICE_SENSOR] == PRICE_SENSOR
    assert state.attributes[ATTR_PRICE_GAP_ACTIVE] is False


async def test_rename_reload_round_trip_keeps_cost_to_the_cent(hass: HomeAssistant) -> None:
    """BINDING: a subentry rename reloads the entry without losing a cent."""
    hass.states.async_set(PRICE_SENSOR, "0.50", _price_attrs())
    hass.states.async_set(ENERGY_SENSOR, "100.0", _energy_attrs())
    entry = await _setup_entry(hass)
    hass.states.async_set(ENERGY_SENSOR, "102.0", _energy_attrs())
    await hass.async_block_till_done()
    assert _cost(hass) == Decimal("1.00")

    subentry = next(iter(entry.subentries.values()))
    hass.config_entries.async_update_subentry(entry, subentry, title="Sauna")
    await hass.async_block_till_done()

    state = hass.states.get(COST_ENTITY)
    assert state is not None
    assert state.attributes[ATTR_FRIENDLY_NAME] == "Sauna cost"
    assert Decimal(state.state) == Decimal("1.00")

    # Only the post-reload delta charges — the reload never re-prices
    # already-settled energy.
    hass.states.async_set(ENERGY_SENSOR, "103.0", _energy_attrs())
    await hass.async_block_till_done()
    assert _cost(hass) == Decimal("1.50")


async def test_energy_sensor_swap_keeps_cost_and_rebaselines(
    hass: HomeAssistant, caplog: pytest.LogCaptureFixture
) -> None:
    """Reconfigure swap: cost survives to the cent, nothing charged at swap.

    The restored baseline belongs to the old meter; replaying it against
    the new meter's (much higher) reading would fabricate a bogus accrual.
    """
    caplog.set_level(logging.INFO)
    hass.states.async_set(PRICE_SENSOR, "0.50", _price_attrs())
    hass.states.async_set(ENERGY_SENSOR, "100.0", _energy_attrs())
    entry = await _setup_entry(hass)
    hass.states.async_set(ENERGY_SENSOR, "102.0", _energy_attrs())
    await hass.async_block_till_done()
    assert _cost(hass) == Decimal("1.00")

    hass.states.async_set(ENERGY_SENSOR_B, "5000.0", _energy_attrs())
    subentry = next(iter(entry.subentries.values()))
    hass.config_entries.async_update_subentry(
        entry,
        subentry,
        data={CONF_ENERGY_SENSOR: ENERGY_SENSOR_B},
        unique_id=ENERGY_SENSOR_B,
    )
    await hass.async_block_till_done()

    # Cost survives exactly; the new sensor's 5000 kWh charges nothing.
    state = hass.states.get(COST_ENTITY)
    assert state is not None
    assert Decimal(state.state) == Decimal("1.00")
    assert state.attributes[CONF_ENERGY_SENSOR] == ENERGY_SENSOR_B
    swap_logs = [
        r
        for r in _integration_records(caplog, logging.INFO)
        if "energy sensor changed" in r.message
    ]
    assert len(swap_logs) == 1

    # The next delta on the new sensor charges from the new baseline.
    hass.states.async_set(ENERGY_SENSOR_B, "5002.0", _energy_attrs())
    await hass.async_block_till_done()
    assert _cost(hass) == Decimal("2.00")


async def test_energy_sensor_swap_to_lower_reading_never_fabricates_reset(
    hass: HomeAssistant, caplog: pytest.LogCaptureFixture
) -> None:
    """A swap to a >10%-lower reading must not trip METER_RESET fabrication."""
    hass.states.async_set(PRICE_SENSOR, "0.50", _price_attrs())
    hass.states.async_set(ENERGY_SENSOR, "100.0", _energy_attrs())
    entry = await _setup_entry(hass)
    hass.states.async_set(ENERGY_SENSOR, "102.0", _energy_attrs())
    await hass.async_block_till_done()
    assert _cost(hass) == Decimal("1.00")

    # 5.0 < 0.9 * 102.0: a leaked baseline would classify this as a reset
    # and charge the whole 5 kWh reading as fabricated consumption.
    hass.states.async_set(ENERGY_SENSOR_B, "5.0", _energy_attrs())
    subentry = next(iter(entry.subentries.values()))
    hass.config_entries.async_update_subentry(
        entry,
        subentry,
        data={CONF_ENERGY_SENSOR: ENERGY_SENSOR_B},
        unique_id=ENERGY_SENSOR_B,
    )
    await hass.async_block_till_done()

    assert _cost(hass) == Decimal("1.00")
    assert not any("meter reset detected" in r.message for r in caplog.records)

    hass.states.async_set(ENERGY_SENSOR_B, "6.0", _energy_attrs())
    await hass.async_block_till_done()
    assert _cost(hass) == Decimal("1.50")


async def test_restart_restore_prices_downtime_delta_at_returning_price(
    hass: HomeAssistant,
) -> None:
    """Restore brings back cost and baseline; downtime energy prices on arrival."""
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
    hass.states.async_set(PRICE_SENSOR, "0.30", _price_attrs())
    hass.states.async_set(ENERGY_SENSOR, "104.0", _energy_attrs())
    await _setup_entry(hass)

    # 4 kWh consumed across the restart, priced at the price in force now.
    assert _cost(hass) == Decimal("5.000") + Decimal("4.0") * Decimal("0.30")

    # The same reading again must not double count.
    hass.states.async_set(ENERGY_SENSOR, "104.0", _energy_attrs())
    await hass.async_block_till_done()
    assert _cost(hass) == Decimal("6.200")


async def test_price_change_settles_at_the_old_price_first(hass: HomeAssistant) -> None:
    """Settled energy stays at its period's price; later reports use the new price."""
    hass.states.async_set(PRICE_SENSOR, "0.50", _price_attrs())
    hass.states.async_set(ENERGY_SENSOR, "100.0", _energy_attrs())
    await _setup_entry(hass)

    hass.states.async_set(ENERGY_SENSOR, "102.0", _energy_attrs())
    await hass.async_block_till_done()
    assert _cost(hass) == Decimal("1.00")

    # The price drops; the already-metered energy is never re-priced.
    hass.states.async_set(PRICE_SENSOR, "0.10", _price_attrs())
    await hass.async_block_till_done()
    assert _cost(hass) == Decimal("1.00")

    # A slow meter reporting after the switch prices at the new price
    # (documented event-driven semantics).
    hass.states.async_set(ENERGY_SENSOR, "104.0", _energy_attrs())
    await hass.async_block_till_done()
    assert _cost(hass) == Decimal("1.20")


async def test_meter_reset_charges_reading_and_dip_charges_nothing(
    hass: HomeAssistant,
) -> None:
    """Reset >10% charges the new reading; a dip holds; recovery never double-charges."""
    hass.states.async_set(PRICE_SENSOR, "0.50", _price_attrs())
    hass.states.async_set(ENERGY_SENSOR, "100.0", _energy_attrs())
    await _setup_entry(hass)
    hass.states.async_set(ENERGY_SENSOR, "102.0", _energy_attrs())
    await hass.async_block_till_done()
    assert _cost(hass) == Decimal("1.00")

    # Dip within 10% (100.0 >= 0.9 * 102.0): nothing charged.
    hass.states.async_set(ENERGY_SENSOR, "100.0", _energy_attrs())
    await hass.async_block_till_done()
    assert _cost(hass) == Decimal("1.00")

    # Recovery to the held high-water mark: still nothing (no double charge).
    hass.states.async_set(ENERGY_SENSOR, "102.0", _energy_attrs())
    await hass.async_block_till_done()
    assert _cost(hass) == Decimal("1.00")

    hass.states.async_set(ENERGY_SENSOR, "103.0", _energy_attrs())
    await hass.async_block_till_done()
    assert _cost(hass) == Decimal("1.50")

    # Reset (10.0 < 0.9 * 103.0): the new reading is consumption since the
    # reset — charged, never negative.
    hass.states.async_set(ENERGY_SENSOR, "10.0", _energy_attrs())
    await hass.async_block_till_done()
    assert _cost(hass) == Decimal("1.50") + Decimal("10.0") * Decimal("0.50")
    assert _cost(hass) > 0


async def test_price_gap_holds_accrual_and_prices_at_returning_price(
    hass: HomeAssistant,
) -> None:
    """A price outage holds accrual visibly; the gap delta prices on return."""
    hass.states.async_set(PRICE_SENSOR, "0.50", _price_attrs())
    hass.states.async_set(ENERGY_SENSOR, "100.0", _energy_attrs())
    await _setup_entry(hass)
    hass.states.async_set(ENERGY_SENSOR, "102.0", _energy_attrs())
    await hass.async_block_till_done()
    assert _cost(hass) == Decimal("1.00")

    hass.states.async_set(PRICE_SENSOR, STATE_UNAVAILABLE, _price_attrs())
    await hass.async_block_till_done()
    state = hass.states.get(COST_ENTITY)
    assert state is not None
    assert state.attributes[ATTR_PRICE_GAP_ACTIVE] is True
    # The entity stays available at the settled (true) value.
    assert Decimal(state.state) == Decimal("1.00")

    hass.states.async_set(ENERGY_SENSOR, "104.0", _energy_attrs())
    await hass.async_block_till_done()
    assert _cost(hass) == Decimal("1.00")

    hass.states.async_set(PRICE_SENSOR, "0.20", _price_attrs())
    await hass.async_block_till_done()
    state = hass.states.get(COST_ENTITY)
    assert state is not None
    assert state.attributes[ATTR_PRICE_GAP_ACTIVE] is False
    # 2 kWh accumulated during the gap, priced at the returning 0.20.
    assert Decimal(state.state) == Decimal("1.00") + Decimal("2.0") * Decimal("0.20")


async def test_energy_unavailability_flap_degrades_and_recovers_visibly(
    hass: HomeAssistant, caplog: pytest.LogCaptureFixture
) -> None:
    """Energy source flap: unavailable entity, no fabricated cost, edge logs once."""
    caplog.set_level(logging.INFO)
    hass.states.async_set(PRICE_SENSOR, "0.50", _price_attrs())
    hass.states.async_set(ENERGY_SENSOR, "100.0", _energy_attrs())
    await _setup_entry(hass)

    hass.states.async_set(ENERGY_SENSOR, STATE_UNAVAILABLE, _energy_attrs())
    await hass.async_block_till_done()
    state = hass.states.get(COST_ENTITY)
    assert state is not None
    assert state.state == STATE_UNAVAILABLE

    hass.states.async_set(ENERGY_SENSOR, "100.0", _energy_attrs())
    await hass.async_block_till_done()
    assert _cost(hass) == Decimal("0")

    info_records = _integration_records(caplog, logging.INFO)
    assert len([r for r in info_records if "became unavailable" in r.message]) == 1
    assert len([r for r in info_records if "recovered" in r.message]) == 1


async def test_unknown_energy_state_does_not_flip_availability(
    hass: HomeAssistant,
) -> None:
    """unknown carries nothing to settle but is not unavailability."""
    hass.states.async_set(PRICE_SENSOR, "0.50", _price_attrs())
    hass.states.async_set(ENERGY_SENSOR, "100.0", _energy_attrs())
    await _setup_entry(hass)

    hass.states.async_set(ENERGY_SENSOR, STATE_UNKNOWN, _energy_attrs())
    await hass.async_block_till_done()
    state = hass.states.get(COST_ENTITY)
    assert state is not None
    assert state.state != STATE_UNAVAILABLE
    assert Decimal(state.state) == Decimal("0")


@pytest.mark.parametrize("bad_state", ["nan", "inf", "-Infinity", "not-a-number"])
async def test_non_finite_energy_states_charge_nothing(hass: HomeAssistant, bad_state: str) -> None:
    """NaN/Infinity/non-numeric energy states are skipped, never priced."""
    hass.states.async_set(PRICE_SENSOR, "0.50", _price_attrs())
    hass.states.async_set(ENERGY_SENSOR, "100.0", _energy_attrs())
    await _setup_entry(hass)

    hass.states.async_set(ENERGY_SENSOR, bad_state, _energy_attrs())
    await hass.async_block_till_done()
    assert _cost(hass) == Decimal("0")

    # A later valid reading settles against the untouched baseline.
    hass.states.async_set(ENERGY_SENSOR, "102.0", _energy_attrs())
    await hass.async_block_till_done()
    assert _cost(hass) == Decimal("1.00")


@pytest.mark.parametrize("bad_state", ["nan", "Infinity", "garbage"])
async def test_non_finite_price_states_start_a_gap(hass: HomeAssistant, bad_state: str) -> None:
    """NaN/Infinity/non-numeric price states are a gap, never a price."""
    hass.states.async_set(PRICE_SENSOR, "0.50", _price_attrs())
    hass.states.async_set(ENERGY_SENSOR, "100.0", _energy_attrs())
    await _setup_entry(hass)

    hass.states.async_set(PRICE_SENSOR, bad_state, _price_attrs())
    await hass.async_block_till_done()
    state = hass.states.get(COST_ENTITY)
    assert state is not None
    assert state.attributes[ATTR_PRICE_GAP_ACTIVE] is True
    assert Decimal(state.state) == Decimal("0")


async def test_price_handler_survives_unusable_energy_fetch(hass: HomeAssistant) -> None:
    """The price handler's energy fetch fails closed on an unusable state."""
    hass.states.async_set(PRICE_SENSOR, "0.50", _price_attrs())
    hass.states.async_set(ENERGY_SENSOR, "100.0", _energy_attrs())
    await _setup_entry(hass)
    hass.states.async_set(ENERGY_SENSOR, "102.0", _energy_attrs())
    await hass.async_block_till_done()
    assert _cost(hass) == Decimal("1.00")

    # The energy state goes non-numeric, then the price changes: the fetch
    # returns nothing usable and the switch happens without settlement.
    hass.states.async_set(ENERGY_SENSOR, "nan", _energy_attrs())
    hass.states.async_set(PRICE_SENSOR, "0.10", _price_attrs())
    await hass.async_block_till_done()
    assert _cost(hass) == Decimal("1.00")

    hass.states.async_set(ENERGY_SENSOR, "104.0", _energy_attrs())
    await hass.async_block_till_done()
    # The unsettleable leg prices at the new price (documented degraded path).
    assert _cost(hass) == Decimal("1.20")


async def test_runtime_price_unit_drift_is_a_gap_never_a_price(
    hass: HomeAssistant, caplog: pytest.LogCaptureFixture
) -> None:
    """A numerator drift (snt/kWh) becomes a warned gap — never a 100x price."""
    hass.states.async_set(PRICE_SENSOR, "0.50", _price_attrs())
    hass.states.async_set(ENERGY_SENSOR, "100.0", _energy_attrs())
    await _setup_entry(hass)
    hass.states.async_set(ENERGY_SENSOR, "102.0", _energy_attrs())
    await hass.async_block_till_done()
    assert _cost(hass) == Decimal("1.00")

    hass.states.async_set(PRICE_SENSOR, "50", _price_attrs(unit="snt/kWh"))
    await hass.async_block_till_done()
    state = hass.states.get(COST_ENTITY)
    assert state is not None
    assert state.attributes[ATTR_PRICE_GAP_ACTIVE] is True
    assert Decimal(state.state) == Decimal("1.00")
    assert any(
        "does not match currency" in record.message
        for record in caplog.records
        if record.levelno == logging.WARNING
    )

    hass.states.async_set(ENERGY_SENSOR, "104.0", _energy_attrs())
    hass.states.async_set(PRICE_SENSOR, "0.10", _price_attrs())
    await hass.async_block_till_done()
    # The gap delta prices at the returning EUR price, never at 50.
    assert _cost(hass) == Decimal("1.20")


async def test_mid_life_energy_unit_change_stays_correct(hass: HomeAssistant) -> None:
    """Each reading converts via its own unit attribute — no unit memory."""
    hass.states.async_set(PRICE_SENSOR, "0.50", _price_attrs())
    hass.states.async_set(ENERGY_SENSOR, "100000", _energy_attrs(unit="Wh"))
    await _setup_entry(hass)
    assert _cost(hass) == Decimal("0")

    # The source flips to kWh mid-life: 102 kWh - 100 kWh = 2 kWh.
    hass.states.async_set(ENERGY_SENSOR, "102", _energy_attrs(unit="kWh"))
    await hass.async_block_till_done()
    assert _cost(hass) == Decimal("1.00")


async def test_registry_identity_and_subentry_removal(hass: HomeAssistant) -> None:
    """unique_id is the subentry_id; removing the subentry removes the entity."""
    hass.states.async_set(PRICE_SENSOR, "0.50", _price_attrs())
    hass.states.async_set(ENERGY_SENSOR, "100.0", _energy_attrs())
    entry = await _setup_entry(hass)
    subentry_id = next(iter(entry.subentries))

    registry = er.async_get(hass)
    entity_entry = registry.async_get(COST_ENTITY)
    assert entity_entry is not None
    assert entity_entry.unique_id == subentry_id
    assert entity_entry.config_subentry_id == subentry_id

    state = hass.states.get(COST_ENTITY)
    assert state is not None
    assert state.attributes[ATTR_FRIENDLY_NAME] == "Heat pump cost"

    hass.config_entries.async_remove_subentry(entry, subentry_id)
    await hass.async_block_till_done()
    assert registry.async_get(COST_ENTITY) is None
    assert hass.states.get(COST_ENTITY) is None


async def test_zero_appliance_entry_loads_with_no_entities(hass: HomeAssistant) -> None:
    """A zero-appliance entry is legal and creates nothing."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        title="Electricity price",
        data={CONF_PRICE_SENSOR: PRICE_SENSOR, CONF_CURRENCY: "EUR"},
    )
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    assert not hass.states.async_entity_ids("sensor")


async def test_restore_falls_back_to_the_state_string_on_corrupt_extra_data(
    hass: HomeAssistant, caplog: pytest.LogCaptureFixture
) -> None:
    """Corrupt extra data restores cost from the state string — no LTS crater."""
    mock_restore_cache_with_extra_data(
        hass,
        [
            (
                State(COST_ENTITY, "7.25"),
                {"cost": "garbage", "last_energy_kwh": "100.0"},
            )
        ],
    )
    hass.states.async_set(PRICE_SENSOR, "0.50", _price_attrs())
    hass.states.async_set(ENERGY_SENSOR, "104.0", _energy_attrs())
    await _setup_entry(hass)

    # Cost preserved from the state string; the baseline was lost so the
    # current reading re-baselines without charging.
    assert _cost(hass) == Decimal("7.25")
    assert any(
        "baseline was lost" in record.message
        for record in caplog.records
        if record.levelno == logging.WARNING
    )

    hass.states.async_set(ENERGY_SENSOR, "106.0", _energy_attrs())
    await hass.async_block_till_done()
    assert _cost(hass) == Decimal("8.25")


async def test_restore_restarts_at_zero_when_nothing_is_usable(
    hass: HomeAssistant, caplog: pytest.LogCaptureFixture
) -> None:
    """Both restore layers corrupt: fresh start with the consequence named."""
    mock_restore_cache_with_extra_data(
        hass,
        [
            (
                State(COST_ENTITY, STATE_UNKNOWN),
                {"cost": "nan", "last_energy_kwh": None},
            )
        ],
    )
    hass.states.async_set(PRICE_SENSOR, "0.50", _price_attrs())
    hass.states.async_set(ENERGY_SENSOR, "100.0", _energy_attrs())
    await _setup_entry(hass)

    assert _cost(hass) == Decimal("0")
    assert any(
        "long-term statistics will record a negative step" in record.message
        for record in caplog.records
        if record.levelno == logging.WARNING
    )


async def test_unloaded_entry_ignores_source_events(
    hass: HomeAssistant, caplog: pytest.LogCaptureFixture
) -> None:
    """After unload every subscription is gone: no state write, no error."""
    hass.states.async_set(PRICE_SENSOR, "0.50", _price_attrs())
    hass.states.async_set(ENERGY_SENSOR, "100.0", _energy_attrs())
    entry = await _setup_entry(hass)
    assert hass.states.get(COST_ENTITY) is not None

    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()
    # Unload leaves a restored-unavailable placeholder state, not a removal.
    state = hass.states.get(COST_ENTITY)
    assert state is not None
    assert state.state == STATE_UNAVAILABLE

    caplog.clear()
    hass.states.async_set(ENERGY_SENSOR, "200.0", _energy_attrs())
    hass.states.async_set(PRICE_SENSOR, "9.99", _price_attrs())
    await hass.async_block_till_done()
    # Every subscription died with the entity: no write, no resurrection.
    state = hass.states.get(COST_ENTITY)
    assert state is not None
    assert state.state == STATE_UNAVAILABLE
    assert not [r for r in caplog.records if r.levelno >= logging.ERROR]


async def test_gap_from_birth_warns_exactly_once(
    hass: HomeAssistant, caplog: pytest.LogCaptureFixture
) -> None:
    """A dead-at-startup price source is visible once, with no duplicate."""
    caplog.set_level(logging.INFO)
    hass.states.async_set(ENERGY_SENSOR, "100.0", _energy_attrs())
    await _setup_entry(hass)

    state = hass.states.get(COST_ENTITY)
    assert state is not None
    assert state.state != STATE_UNAVAILABLE
    assert state.attributes[ATTR_PRICE_GAP_ACTIVE] is True

    hass.states.async_set(ENERGY_SENSOR, "102.0", _energy_attrs())
    await hass.async_block_till_done()
    assert _cost(hass) == Decimal("0")

    warnings = _integration_records(caplog, logging.WARNING)
    assert len(warnings) == 1
    assert "no usable price" in warnings[0].message
    assert not any("price gap started" in r.message for r in caplog.records)

    # The gap ends at the returning price; the whole delta prices then.
    hass.states.async_set(PRICE_SENSOR, "0.50", _price_attrs())
    await hass.async_block_till_done()
    assert _cost(hass) == Decimal("1.00")


async def test_invalid_readings_warn_once_per_streak(
    hass: HomeAssistant, caplog: pytest.LogCaptureFixture
) -> None:
    """Negative readings reject visibly, edge-guarded per streak."""
    hass.states.async_set(PRICE_SENSOR, "0.50", _price_attrs())
    hass.states.async_set(ENERGY_SENSOR, "100.0", _energy_attrs())
    await _setup_entry(hass)

    for bad in ("-5", "-6", "-7"):
        hass.states.async_set(ENERGY_SENSOR, bad, _energy_attrs())
        await hass.async_block_till_done()
    assert _cost(hass) == Decimal("0")

    hass.states.async_set(ENERGY_SENSOR, "102.0", _energy_attrs())
    await hass.async_block_till_done()
    assert _cost(hass) == Decimal("1.00")

    hass.states.async_set(ENERGY_SENSOR, "-8", _energy_attrs())
    await hass.async_block_till_done()
    assert _cost(hass) == Decimal("1.00")

    negative_warnings = [
        r
        for r in caplog.records
        if r.levelno == logging.WARNING and "negative cumulative reading" in r.message
    ]
    # One per streak: the -5/-6/-7 streak and the -8 streak.
    assert len(negative_warnings) == 2


def _next_hour_boundary() -> datetime:
    """The next UTC hour boundary strictly after now (never moves time back)."""
    now = dt_util.utcnow()
    return now.replace(minute=0, second=0, microsecond=0) + timedelta(hours=1)


async def _compile_hour(hass: HomeAssistant, hour_start: datetime) -> None:
    """Compile the 5-minute slot at the hour start plus the hourly rollup.

    The recorder rolls short-term statistics up into an hourly row when the
    5-minute compile at minute 55 runs (recorder/statistics.py).
    """
    do_adhoc_statistics(hass, start=hour_start)
    do_adhoc_statistics(hass, start=hour_start + timedelta(minutes=55))
    await async_wait_recording_done(hass)


async def test_hourly_statistics_sum_accumulates(
    hass: HomeAssistant, freezer: FrozenDateTimeFactory
) -> None:
    """The cost sensor feeds hourly long-term statistics sum rows."""
    hour_one = _next_hour_boundary()
    hour_two = hour_one + timedelta(hours=1)
    freezer.move_to(hour_one)
    hass.states.async_set(PRICE_SENSOR, "0.50", _price_attrs())
    hass.states.async_set(ENERGY_SENSOR, "100.0", _energy_attrs())
    await _setup_entry(hass)
    freezer.move_to(hour_one + timedelta(minutes=1))
    hass.states.async_set(ENERGY_SENSOR, "102.0", _energy_attrs())
    await hass.async_block_till_done()
    await async_wait_recording_done(hass)

    freezer.move_to(hour_two + timedelta(minutes=1))
    hass.states.async_set(ENERGY_SENSOR, "104.0", _energy_attrs())
    await hass.async_block_till_done()
    await async_wait_recording_done(hass)

    freezer.move_to(hour_two + timedelta(hours=1))
    await _compile_hour(hass, hour_one)
    await _compile_hour(hass, hour_two)

    stats = statistics_during_period(hass, hour_one, statistic_ids={COST_ENTITY}, period="hour")
    rows = stats[COST_ENTITY]
    assert len(rows) == 2
    assert rows[1]["sum"] > rows[0]["sum"]
    # LTS sums are recorder-inherent floats; the state stays Decimal-exact.
    assert rows[1]["sum"] - rows[0]["sum"] == pytest.approx(1.0)


async def test_negative_price_decreases_total_and_hourly_sum(
    hass: HomeAssistant, freezer: FrozenDateTimeFactory
) -> None:
    """A negative price legally decreases the total and the LTS sum."""
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
    hass.states.async_set(PRICE_SENSOR, "-0.50", _price_attrs())
    hass.states.async_set(ENERGY_SENSOR, "103.5", _energy_attrs())
    await hass.async_block_till_done()
    # 1.5 kWh at -0.50: the cumulative total legally decreases.
    assert _cost(hass) == Decimal("0.25")
    await async_wait_recording_done(hass)

    freezer.move_to(hour_two + timedelta(hours=1))
    await _compile_hour(hass, hour_one)
    await _compile_hour(hass, hour_two)

    stats = statistics_during_period(hass, hour_one, statistic_ids={COST_ENTITY}, period="hour")
    rows = stats[COST_ENTITY]
    assert len(rows) == 2
    assert rows[1]["sum"] < rows[0]["sum"]
    assert rows[1]["sum"] - rows[0]["sum"] == pytest.approx(-0.75)


async def test_damaged_subentry_is_skipped_without_crashing_the_entry(
    hass: HomeAssistant, caplog: pytest.LogCaptureFixture
) -> None:
    """A structurally damaged subentry is skipped; the others still cost."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        title="Electricity price",
        data={CONF_PRICE_SENSOR: PRICE_SENSOR, CONF_CURRENCY: "EUR"},
        subentries_data=[
            ConfigSubentryData(
                data={},
                subentry_type=SUBENTRY_TYPE_APPLIANCE,
                title="Broken",
                unique_id="broken",
            ),
            ConfigSubentryData(
                data={CONF_ENERGY_SENSOR: ENERGY_SENSOR},
                subentry_type=SUBENTRY_TYPE_APPLIANCE,
                title="Heat pump",
                unique_id=ENERGY_SENSOR,
            ),
        ],
    )
    entry.add_to_hass(hass)
    hass.states.async_set(PRICE_SENSOR, "0.50", _price_attrs())
    hass.states.async_set(ENERGY_SENSOR, "100.0", _energy_attrs())
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    assert hass.states.get(COST_ENTITY) is not None
    assert len(hass.states.async_entity_ids("sensor")) == 3  # 2 sources + 1 cost
    assert any(
        "Skipping appliance subentry" in record.message
        for record in caplog.records
        if record.levelno == logging.ERROR
    )
