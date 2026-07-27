"""Tests for the redacted config-entry diagnostics.

The snapshot pins the full payload shape; the forbidden-substring test is
the real privacy enforcement — it scans the serialised payload for every
identifying value (titles, slugs, entity ids) regardless of which key it
would hide under. Fixture titles are deliberately distinctive ("Sauna
Heater", "Pool Pump") so they cannot overlap kept tokens like "cost" or
"sensor".
"""

from __future__ import annotations

import json

from freezegun import freeze_time
from homeassistant.components.diagnostics import REDACTED
from homeassistant.components.sensor import ATTR_STATE_CLASS
from homeassistant.config_entries import ConfigSubentryData
from homeassistant.const import (
    ATTR_DEVICE_CLASS,
    ATTR_FRIENDLY_NAME,
    ATTR_UNIT_OF_MEASUREMENT,
    STATE_UNAVAILABLE,
)
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er
from homeassistant.util import slugify
from pytest_homeassistant_custom_component.common import MockConfigEntry
from pytest_homeassistant_custom_component.components.diagnostics import (
    get_diagnostics_for_config_entry,
)
from pytest_homeassistant_custom_component.typing import ClientSessionGenerator
from syrupy.assertion import SnapshotAssertion

from custom_components.appliance_energy_cost.const import (
    CONF_CURRENCY,
    CONF_ENERGY_SENSOR,
    CONF_PRICE_SENSOR,
    DOMAIN,
    SUBENTRY_TYPE_APPLIANCE,
)

ENTRY_TITLE = "Spot Electricity Rate"
PRICE_SENSOR = "sensor.spot_electricity_rate"
SAUNA_TITLE = "Sauna Heater"
SAUNA_METER = "sensor.sauna_heater_meter"
POOL_TITLE = "Pool Pump"
POOL_METER = "sensor.pool_pump_meter"

_NULL_SOURCE = {
    "entity_id": None,
    "exists": False,
    "state": None,
    "unit_of_measurement": None,
    "state_class": None,
    "device_class": None,
}


def _price_attrs() -> dict[str, str]:
    return {
        ATTR_FRIENDLY_NAME: "Spot Electricity Rate",
        ATTR_UNIT_OF_MEASUREMENT: "EUR/kWh",
        ATTR_STATE_CLASS: "measurement",
        ATTR_DEVICE_CLASS: "monetary",
    }


def _energy_attrs(friendly_name: str) -> dict[str, str]:
    return {
        ATTR_FRIENDLY_NAME: friendly_name,
        ATTR_UNIT_OF_MEASUREMENT: "kWh",
        ATTR_STATE_CLASS: "total_increasing",
        ATTR_DEVICE_CLASS: "energy",
    }


def _appliance(title: str, energy_sensor: str) -> ConfigSubentryData:
    return ConfigSubentryData(
        data={CONF_ENERGY_SENSOR: energy_sensor},
        subentry_type=SUBENTRY_TYPE_APPLIANCE,
        title=title,
        unique_id=energy_sensor,
    )


async def _setup_entry(
    hass: HomeAssistant, subentries: list[ConfigSubentryData]
) -> MockConfigEntry:
    entry = MockConfigEntry(
        domain=DOMAIN,
        title=ENTRY_TITLE,
        data={CONF_PRICE_SENSOR: PRICE_SENSOR, CONF_CURRENCY: "EUR"},
        subentries_data=subentries,
    )
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    return entry


async def _setup_two_appliances(hass: HomeAssistant) -> MockConfigEntry:
    """Known states and units: price 0.25 EUR/kWh, two accruing appliances."""
    hass.states.async_set(PRICE_SENSOR, "0.25", _price_attrs())
    hass.states.async_set(SAUNA_METER, "100.0", _energy_attrs("Sauna Heater Meter"))
    hass.states.async_set(POOL_METER, "50.0", _energy_attrs("Pool Pump Meter"))
    entry = await _setup_entry(
        hass, [_appliance(SAUNA_TITLE, SAUNA_METER), _appliance(POOL_TITLE, POOL_METER)]
    )
    hass.states.async_set(SAUNA_METER, "102.0", _energy_attrs("Sauna Heater Meter"))
    hass.states.async_set(POOL_METER, "54.0", _energy_attrs("Pool Pump Meter"))
    await hass.async_block_till_done()
    return entry


def _cost_entity_ids(hass: HomeAssistant, entry: MockConfigEntry) -> list[str]:
    """Registry-resolved cost entity ids, in appliance (subentry) order."""
    registry = er.async_get(hass)
    entity_ids: list[str] = []
    for subentry in entry.subentries.values():
        entity_id = registry.async_get_entity_id("sensor", DOMAIN, subentry.subentry_id)
        assert entity_id is not None
        entity_ids.append(entity_id)
    return entity_ids


async def test_diagnostics_snapshot(
    hass: HomeAssistant,
    hass_client: ClientSessionGenerator,
    snapshot: SnapshotAssertion,
) -> None:
    """The full redacted payload, pinned. No ids and no wall clock leak in.

    Only the data-producing phase runs under the frozen clock: the
    ``hass_client`` access token is minted at fixture time, so freezing the
    HTTP fetch outside the token's validity window would 401.
    """
    with freeze_time("2026-07-20T10:00:00+00:00"):
        entry = await _setup_two_appliances(hass)

    result = await get_diagnostics_for_config_entry(hass, hass_client, entry)

    assert result == snapshot


async def test_no_identifying_value_survives_serialisation(
    hass: HomeAssistant, hass_client: ClientSessionGenerator
) -> None:
    """The semantic privacy guard: no title, slug, or entity id anywhere.

    Key-based redaction is structural; this scan is what actually fails if
    an identifying value ever reaches the payload under a new key — slugs
    included, because entity ids are built from them.
    """
    entry = await _setup_two_appliances(hass)
    cost_entities = _cost_entity_ids(hass, entry)

    result = await get_diagnostics_for_config_entry(hass, hass_client, entry)

    serialised = json.dumps(result).lower()
    forbidden = [
        ENTRY_TITLE,
        slugify(ENTRY_TITLE),
        PRICE_SENSOR,
        SAUNA_TITLE,
        POOL_TITLE,
        slugify(SAUNA_TITLE),
        slugify(POOL_TITLE),
        SAUNA_METER,
        POOL_METER,
        "Sauna Heater Meter",
        "Pool Pump Meter",
        *cost_entities,
    ]
    for value in forbidden:
        assert value.lower() not in serialised, f"identifying value leaked: {value!r}"


async def test_content_presence(hass: HomeAssistant, hass_client: ClientSessionGenerator) -> None:
    """The values a costing bug report needs are present and exact."""
    entry = await _setup_two_appliances(hass)

    result = await get_diagnostics_for_config_entry(hass, hass_client, entry)

    entry_block = result["entry"]
    assert isinstance(entry_block, dict)
    assert entry_block["state"] == "loaded"
    assert entry_block["appliance_count"] == 2
    assert entry_block["data"] == {"price_sensor": REDACTED, "currency": "EUR"}

    price_source = result["price_source"]
    assert isinstance(price_source, dict)
    assert price_source["exists"] is True
    assert price_source["state"] == "0.25"
    assert price_source["unit_of_measurement"] == "EUR/kWh"
    assert price_source["state_class"] == "measurement"

    appliances = result["appliances"]
    assert isinstance(appliances, list)
    sauna, pool = appliances
    assert isinstance(sauna, dict) and isinstance(pool, dict)
    # Decimals travel as strings end to end — never floats.
    assert sauna["accrual"] == {
        "cost": "0.500",
        "last_energy_kwh": "102.0",
        "energy_sensor": REDACTED,
        "source_entity_uuid": None,
    }
    assert pool["accrual"] == {
        "cost": "1.000",
        "last_energy_kwh": "54.0",
        "energy_sensor": REDACTED,
        "source_entity_uuid": None,
    }
    sauna_cost = sauna["cost_entity"]
    assert isinstance(sauna_cost, dict)
    assert sauna_cost["state"] == "0.500"
    assert sauna_cost["available"] is True
    assert sauna_cost["price_gap_active"] is False
    sauna_source = sauna["source"]
    assert isinstance(sauna_source, dict)
    assert sauna_source["state_class"] == "total_increasing"
    assert sauna_source["state"] == "102.0"


async def test_zero_appliances(hass: HomeAssistant, hass_client: ClientSessionGenerator) -> None:
    """A zero-appliance entry reports an empty list, not an error."""
    hass.states.async_set(PRICE_SENSOR, "0.25", _price_attrs())
    entry = await _setup_entry(hass, [])

    result = await get_diagnostics_for_config_entry(hass, hass_client, entry)

    entry_block = result["entry"]
    assert isinstance(entry_block, dict)
    assert entry_block["appliance_count"] == 0
    assert result["appliances"] == []


async def test_price_gap_active_is_visible(
    hass: HomeAssistant, hass_client: ClientSessionGenerator
) -> None:
    """An unavailable price source shows as a gap, never as fabricated zeros."""
    hass.states.async_set(PRICE_SENSOR, STATE_UNAVAILABLE)
    hass.states.async_set(SAUNA_METER, "100.0", _energy_attrs("Sauna Heater Meter"))
    entry = await _setup_entry(hass, [_appliance(SAUNA_TITLE, SAUNA_METER)])

    result = await get_diagnostics_for_config_entry(hass, hass_client, entry)

    price_source = result["price_source"]
    assert isinstance(price_source, dict)
    assert price_source["exists"] is True
    assert price_source["state"] == STATE_UNAVAILABLE

    appliances = result["appliances"]
    assert isinstance(appliances, list)
    (sauna,) = appliances
    assert isinstance(sauna, dict)
    cost_entity = sauna["cost_entity"]
    assert isinstance(cost_entity, dict)
    assert cost_entity["available"] is True
    assert cost_entity["price_gap_active"] is True
    # Baseline initialised from the reading, nothing charged during the gap.
    assert sauna["accrual"] == {
        "cost": "0",
        "last_energy_kwh": "100.0",
        "energy_sensor": REDACTED,
        "source_entity_uuid": None,
    }


async def test_degraded_sources_resolve_to_explicit_values(
    hass: HomeAssistant, hass_client: ClientSessionGenerator
) -> None:
    """Absent and unavailable energy sources: explicit fields, no fabrication."""
    hass.states.async_set(PRICE_SENSOR, "0.25", _price_attrs())
    # SAUNA_METER deliberately never set; POOL_METER explicitly unavailable.
    hass.states.async_set(POOL_METER, STATE_UNAVAILABLE)
    entry = await _setup_entry(
        hass, [_appliance(SAUNA_TITLE, SAUNA_METER), _appliance(POOL_TITLE, POOL_METER)]
    )

    result = await get_diagnostics_for_config_entry(hass, hass_client, entry)

    appliances = result["appliances"]
    assert isinstance(appliances, list)
    sauna, pool = appliances
    assert isinstance(sauna, dict) and isinstance(pool, dict)

    sauna_source = sauna["source"]
    assert isinstance(sauna_source, dict)
    assert sauna_source["exists"] is False
    sauna_cost = sauna["cost_entity"]
    assert isinstance(sauna_cost, dict)
    assert sauna_cost["state"] == STATE_UNAVAILABLE
    assert sauna_cost["available"] is False
    # The accrual read works even while the entity itself is unavailable:
    # the baseline is None because no reading has ever arrived.
    assert sauna["accrual"] == {
        "cost": "0",
        "last_energy_kwh": None,
        "energy_sensor": REDACTED,
        "source_entity_uuid": None,
    }

    pool_source = pool["source"]
    assert isinstance(pool_source, dict)
    assert pool_source["exists"] is True
    assert pool_source["state"] == STATE_UNAVAILABLE
    pool_cost = pool["cost_entity"]
    assert isinstance(pool_cost, dict)
    assert pool_cost["available"] is False


async def test_damaged_subentry_resolves_to_explicit_nulls(
    hass: HomeAssistant, hass_client: ClientSessionGenerator
) -> None:
    """Undecodable subentry data: explicit nulls and a 200, never a crash."""
    hass.states.async_set(PRICE_SENSOR, "0.25", _price_attrs())
    damaged = ConfigSubentryData(
        data={},
        subentry_type=SUBENTRY_TYPE_APPLIANCE,
        title="Broken Fridge",
        unique_id=None,
    )
    entry = await _setup_entry(hass, [damaged])

    result = await get_diagnostics_for_config_entry(hass, hass_client, entry)

    assert result["appliances"] == [
        {
            "title": REDACTED,
            "config_ok": False,
            "energy_sensor": None,
            "cost_entity": {
                "entity_id": None,
                "state": None,
                "available": False,
                "price_gap_active": None,
            },
            "accrual": None,
            "source": _NULL_SOURCE,
        }
    ]


async def test_unloaded_entry_degrades_accrual_to_none(
    hass: HomeAssistant, hass_client: ClientSessionGenerator
) -> None:
    """No entity platform (entry unloaded): accrual is an explicit None."""
    entry = await _setup_two_appliances(hass)
    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()

    result = await get_diagnostics_for_config_entry(hass, hass_client, entry)

    entry_block = result["entry"]
    assert isinstance(entry_block, dict)
    assert entry_block["state"] == "not_loaded"
    appliances = result["appliances"]
    assert isinstance(appliances, list)
    sauna, _pool = appliances
    assert isinstance(sauna, dict)
    assert sauna["config_ok"] is True
    assert sauna["accrual"] is None
    cost_entity = sauna["cost_entity"]
    assert isinstance(cost_entity, dict)
    # The registry still resolves the entity id (redacted), and core keeps
    # a bare "unavailable" state for the registered entity — without the
    # integration's attributes, so the gap flag degrades to None.
    assert cost_entity["entity_id"] == REDACTED
    assert cost_entity["state"] == STATE_UNAVAILABLE
    assert cost_entity["available"] is False
    assert cost_entity["price_gap_active"] is None
