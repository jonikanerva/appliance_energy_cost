"""The Appliance Energy Cost integration."""

from __future__ import annotations

import logging
from functools import partial
from typing import Final

from homeassistant.config_entries import ConfigEntry, ConfigSubentry
from homeassistant.const import Platform
from homeassistant.core import Event, HomeAssistant, callback
from homeassistant.exceptions import ConfigEntryError
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers.entity_registry import EventEntityRegistryUpdatedData
from homeassistant.helpers.event import async_track_entity_registry_updated_event
from homeassistant.helpers.typing import ConfigType

from .const import CONF_ENERGY_SENSOR, CONF_PRICE_SENSOR, DOMAIN, SUBENTRY_TYPE_APPLIANCE
from .models import EntryRuntimeData, decode_entry_config
from .services import async_setup_services

type ApplianceEnergyCostConfigEntry = ConfigEntry[EntryRuntimeData]

_LOGGER = logging.getLogger(__name__)

PLATFORMS: Final = [Platform.SENSOR]

CONFIG_SCHEMA = cv.config_entry_only_config_schema(DOMAIN)


async def async_setup(hass: HomeAssistant, config: ConfigType) -> bool:
    """Register the domain services; per-entry wiring lives in async_setup_entry."""
    async_setup_services(hass)
    return True


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
    entry.async_on_unload(
        async_track_entity_registry_updated_event(
            hass,
            _tracked_source_entity_ids(entry),
            partial(_async_source_registry_updated, hass, entry),
        )
    )
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    return True


def _tracked_source_entity_ids(entry: ApplianceEnergyCostConfigEntry) -> list[str]:
    """The source entity ids whose registry entries this entry follows.

    The list is registration-time state: every acted-on registry change ends
    in a reload (via the entry update listener or an explicit reload below),
    and setup re-registers the tracker with the then-current ids.
    """
    tracked = [entry.runtime_data.price_sensor]
    for subentry in entry.subentries.values():
        if subentry.subentry_type != SUBENTRY_TYPE_APPLIANCE:
            continue
        energy_sensor = subentry.data.get(CONF_ENERGY_SENSOR)
        if isinstance(energy_sensor, str) and energy_sensor:
            tracked.append(energy_sensor)
    return tracked


async def _async_source_registry_updated(
    hass: HomeAssistant,
    entry: ApplianceEnergyCostConfigEntry,
    event: Event[EventEntityRegistryUpdatedData],
) -> None:
    """React to a tracked source entity being renamed, moved, or removed.

    BINDING ignore rule (issue #28): act only on an ``entity_id`` change, a
    ``device_id`` change, or ``action == "remove"``; every other registry
    update (name, icon, area, label, disabled, ...) returns untouched — a
    cosmetic edit must never reload the entry.
    """
    data = event.data
    if data["action"] == "remove":
        _LOGGER.warning(
            "Source entity %s was removed from the entity registry: a removed"
            " energy source leaves its cost sensor unavailable, a removed"
            " price source is a price gap. Reconfigure the affected appliance"
            " or entry to point at a replacement sensor",
            data["entity_id"],
        )
        # Registry-driven second reload initiator (alongside the entry update
        # listener): a removal changes no config data, so no config-entry
        # update ever fires the listener — without this reload the sensor
        # would keep a stale device link and subscriptions to a dead source.
        await hass.config_entries.async_reload(entry.entry_id)
        return
    if data["action"] != "update":
        return
    changes = data["changes"]
    if "entity_id" in changes:
        old_entity_id = changes["entity_id"]
        if isinstance(old_entity_id, str):
            _async_follow_source_rename(hass, entry, old_entity_id, data["entity_id"])
        return
    if "device_id" in changes:
        _LOGGER.debug(
            "Source entity %s moved to another device; reloading entry %s so"
            " the cost sensor's device link re-derives at registration",
            data["entity_id"],
            entry.title,
        )
        # Reload instead of patching the link in place: the running sensor's
        # device link is registration-time state, and a device_entry updated
        # in place would go stale against what the entity registry recorded
        # at registration. Re-deriving at setup keeps one link owner.
        await hass.config_entries.async_reload(entry.entry_id)


@callback
def _async_follow_source_rename(
    hass: HomeAssistant,
    entry: ApplianceEnergyCostConfigEntry,
    old_entity_id: str,
    new_entity_id: str,
) -> None:
    """Follow a source entity_id rename into the entry or subentry config.

    Synchronous on the event loop by design: the unique_id collision
    pre-check and the update it guards must run in one block with no await
    between them, or a concurrent subentry change could invalidate the
    check. The entry update listener owns the reload — never reload here.
    """
    if entry.data.get(CONF_PRICE_SENSOR) == old_entity_id:
        _LOGGER.debug(
            "Price sensor %s was renamed to %s; following in entry %s",
            old_entity_id,
            new_entity_id,
            entry.title,
        )
        hass.config_entries.async_update_entry(
            entry, data={**entry.data, CONF_PRICE_SENSOR: new_entity_id}
        )
        return
    for subentry in entry.subentries.values():
        if subentry.subentry_type != SUBENTRY_TYPE_APPLIANCE:
            continue
        if subentry.data.get(CONF_ENERGY_SENSOR) != old_entity_id:
            continue
        _async_follow_energy_sensor_rename(hass, entry, subentry, old_entity_id, new_entity_id)
        return


@callback
def _async_follow_energy_sensor_rename(
    hass: HomeAssistant,
    entry: ApplianceEnergyCostConfigEntry,
    subentry: ConfigSubentry,
    old_entity_id: str,
    new_entity_id: str,
) -> None:
    """Update one appliance subentry after its energy sensor was renamed."""
    new_data = {**subentry.data, CONF_ENERGY_SENSOR: new_entity_id}
    collision = next(
        (
            other
            for other in entry.subentries.values()
            if other.subentry_id != subentry.subentry_id and other.unique_id == new_entity_id
        ),
        None,
    )
    if collision is None:
        hass.config_entries.async_update_subentry(
            entry, subentry, data=new_data, unique_id=new_entity_id
        )
        return
    # Core enforces per-entry subentry unique_id uniqueness, so following the
    # rename into the unique_id would raise; update the data only and state
    # the consequence loudly — two appliances now cost the same meter.
    hass.config_entries.async_update_subentry(entry, subentry, data=new_data)
    _LOGGER.error(
        "Energy sensor %s was renamed to %s, which appliance %s already uses:"
        " appliances %s and %s now both track %s and their summed cost figures"
        " double-count. To repair, reconfigure %s to a different energy sensor"
        " first, then reconfigure %s",
        old_entity_id,
        new_entity_id,
        collision.title,
        subentry.title,
        collision.title,
        new_entity_id,
        collision.title,
        subentry.title,
    )


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
