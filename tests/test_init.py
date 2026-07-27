"""Scaffolding smoke tests for the Appliance Energy Cost integration."""

from __future__ import annotations

from homeassistant.core import HomeAssistant
from homeassistant.loader import async_get_integration

from custom_components.appliance_energy_cost.const import DOMAIN


async def test_integration_is_discoverable(hass: HomeAssistant) -> None:
    """The custom integration resolves through the HA loader with its metadata."""
    integration = await async_get_integration(hass, DOMAIN)
    assert integration.domain == DOMAIN
    assert integration.version == "0.1.0"
