"""The Appliance Energy Cost integration."""

from __future__ import annotations

from typing import Final

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryError

from .models import EntryRuntimeData, decode_entry_config

type ApplianceEnergyCostConfigEntry = ConfigEntry[EntryRuntimeData]

PLATFORMS: Final = [Platform.SENSOR]


async def async_setup_entry(hass: HomeAssistant, entry: ApplianceEnergyCostConfigEntry) -> bool:
    """Set up a price-sensor entry: decode its config and wire the reload listener.

    Structure-only re-validation: the referenced source entities may
    legitimately be unavailable while Home Assistant is starting, so their
    availability is deliberately NOT re-checked here — the sensor platform
    owns the runtime degraded states.
    """
    try:
        entry.runtime_data = decode_entry_config(entry.data)
    except ValueError as err:
        raise ConfigEntryError(f"Config entry data is not usable: {err}") from err
    entry.async_on_unload(entry.add_update_listener(_async_update_listener))
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    return True


async def _async_update_listener(
    hass: HomeAssistant, entry: ApplianceEnergyCostConfigEntry
) -> None:
    """Reload the entry on any main-entry or subentry change.

    The sole reload mechanism by design: core fires this listener on
    main-entry updates and on every subentry create/update/delete, and the
    flows use the non-reloading ``async_update_and_abort`` variants — one
    reload owner, never a double reload.
    """
    await hass.config_entries.async_reload(entry.entry_id)


async def async_unload_entry(hass: HomeAssistant, entry: ApplianceEnergyCostConfigEntry) -> bool:
    """Unload a config entry, tearing down every platform it forwarded."""
    return await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
