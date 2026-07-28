"""Setup and teardown tests for the Appliance Energy Cost integration."""

from __future__ import annotations

from homeassistant.config_entries import ConfigEntryState, ConfigSubentryData
from homeassistant.core import HomeAssistant
from homeassistant.loader import async_get_integration
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.appliance_energy_cost.const import (
    CONF_CURRENCY,
    CONF_ENERGY_SENSOR,
    CONF_PRICE_SENSOR,
    DOMAIN,
    SUBENTRY_TYPE_APPLIANCE,
)
from custom_components.appliance_energy_cost.models import EntryRuntimeData


async def test_integration_is_discoverable(hass: HomeAssistant) -> None:
    """The custom integration resolves through the HA loader with its metadata."""
    integration = await async_get_integration(hass, DOMAIN)
    assert integration.domain == DOMAIN
    assert integration.version == "1.0.0"


async def test_setup_succeeds_with_sources_absent_and_unloads_cleanly(
    hass: HomeAssistant,
) -> None:
    """Sources may be down at HA start: setup must not re-check availability."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        title="Electricity price",
        data={CONF_PRICE_SENSOR: "sensor.absent_price", CONF_CURRENCY: "EUR"},
        subentries_data=[
            ConfigSubentryData(
                data={CONF_ENERGY_SENSOR: "sensor.absent_energy"},
                subentry_type=SUBENTRY_TYPE_APPLIANCE,
                title="Heat pump",
                unique_id="sensor.absent_energy",
            )
        ],
    )
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    assert entry.state is ConfigEntryState.LOADED
    assert entry.runtime_data == EntryRuntimeData(
        price_sensor="sensor.absent_price", currency="EUR"
    )
    assert await hass.config_entries.async_unload(entry.entry_id)
    assert entry.state is ConfigEntryState.NOT_LOADED


async def test_setup_with_zero_appliances_succeeds(hass: HomeAssistant) -> None:
    """A zero-appliance entry is legal (the chained add flow is optional)."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        title="Electricity price",
        data={CONF_PRICE_SENSOR: "sensor.absent_price", CONF_CURRENCY: "EUR"},
    )
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    assert entry.state is ConfigEntryState.LOADED


async def test_setup_fails_visibly_on_undecodable_entry_data(
    hass: HomeAssistant,
) -> None:
    """Structural decode failure is a visible setup error, never a silent pass."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        title="Broken",
        data={CONF_PRICE_SENSOR: "sensor.price"},
    )
    entry.add_to_hass(hass)
    assert not await hass.config_entries.async_setup(entry.entry_id)
    assert entry.state is ConfigEntryState.SETUP_ERROR
