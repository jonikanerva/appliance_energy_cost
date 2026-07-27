"""Diagnostics support for the Appliance Energy Cost integration.

The payload is hand-built with an explicit field allowlist — never
``entry.as_dict()`` and never a full state-attribute dump (spot-price
sensors commonly carry raw upstream payloads, e.g. per-hour price tables,
in their attributes). Deny-by-omission is the primary redaction; the
``TO_REDACT`` key set is the second layer for the identifying values the
payload deliberately carries. Everything is a synchronous in-memory read
(config entry, state machine, entity registry, entity platform objects):
no I/O, nothing awaited but the platform contract, nothing written.

Identifying values (entity ids, titles) always live under ``TO_REDACT``
keys, never as dict keys. ``subentry_id`` / ``unique_id`` / ``entry_id``
are omitted entirely: pairing an appliance's config, sources, and accrual
is intrinsic — each lives in one element of the insertion-ordered
``appliances`` list — so the ids would buy nothing a list position does
not, and identifiers a bug report does not need are identifiers not
shipped.
"""

from __future__ import annotations

from typing import Final

from homeassistant.components.diagnostics import async_redact_data
from homeassistant.components.sensor import ATTR_STATE_CLASS
from homeassistant.components.sensor import DOMAIN as SENSOR_DOMAIN
from homeassistant.config_entries import ConfigSubentry
from homeassistant.const import (
    ATTR_DEVICE_CLASS,
    ATTR_ENTITY_ID,
    ATTR_FRIENDLY_NAME,
    ATTR_UNIT_OF_MEASUREMENT,
    CONF_NAME,
    STATE_UNAVAILABLE,
)
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_platform
from homeassistant.helpers import entity_registry as er

from . import ApplianceEnergyCostConfigEntry
from .const import (
    ATTR_PRICE_GAP_ACTIVE,
    CONF_CURRENCY,
    CONF_ENERGY_SENSOR,
    CONF_PRICE_SENSOR,
    DOMAIN,
    SUBENTRY_TYPE_APPLIANCE,
)
from .models import decode_appliance_config
from .sensor import ApplianceCostSensor

TO_REDACT: Final[frozenset[str]] = frozenset(
    {
        CONF_PRICE_SENSOR,
        CONF_ENERGY_SENSOR,
        "title",
        CONF_NAME,
        ATTR_FRIENDLY_NAME,
        ATTR_ENTITY_ID,
    }
)


def _str_or_none(value: object) -> str | None:
    """Coerce an attribute value to a plain string; ``None`` stays ``None``.

    Attribute values may be ``StrEnum`` members in memory (e.g.
    ``SensorStateClass``); ``str()`` yields their wire value.
    """
    return None if value is None else str(value)


def _describe_source(hass: HomeAssistant, entity_id: str | None) -> dict[str, object]:
    """Describe a source sensor through an explicit field allowlist.

    Never a full attribute dump — only the fields a costing bug report
    needs: existence, the raw state string, and the unit/class trio that
    drives parsing. A ``None`` entity id (damaged config) resolves to
    explicit nulls.
    """
    state = None if entity_id is None else hass.states.get(entity_id)
    return {
        ATTR_ENTITY_ID: entity_id,
        "exists": state is not None,
        "state": None if state is None else state.state,
        ATTR_UNIT_OF_MEASUREMENT: (
            None if state is None else _str_or_none(state.attributes.get(ATTR_UNIT_OF_MEASUREMENT))
        ),
        ATTR_STATE_CLASS: (
            None if state is None else _str_or_none(state.attributes.get(ATTR_STATE_CLASS))
        ),
        ATTR_DEVICE_CLASS: (
            None if state is None else _str_or_none(state.attributes.get(ATTR_DEVICE_CLASS))
        ),
    }


def _accrual_snapshot(
    hass: HomeAssistant, entry_id: str, cost_entity_id: str | None
) -> dict[str, str | None] | None:
    """Read one cost sensor's live accrual via its public restore payload.

    The isolated entity-object read: the last-priced-energy baseline is
    deliberately absent from state attributes (recorder churn), so the
    entity object behind the platform is the only place it lives. The
    payload's ``energy_sensor`` key is redacted by the caller's
    ``async_redact_data`` recursion. A missing platform or entity object
    (entry not loaded, subentry skipped as damaged) degrades to an
    explicit ``None`` — a documented degraded value, never a crash.
    """
    if cost_entity_id is None:
        return None
    for platform in entity_platform.async_get_platforms(hass, DOMAIN):
        if platform.domain != SENSOR_DOMAIN:
            continue
        if platform.config_entry is None or platform.config_entry.entry_id != entry_id:
            continue
        entity = platform.entities.get(cost_entity_id)
        if isinstance(entity, ApplianceCostSensor):
            return entity.extra_restore_state_data.as_dict()
    return None


def _describe_appliance(
    hass: HomeAssistant,
    entry: ApplianceEnergyCostConfigEntry,
    subentry: ConfigSubentry,
) -> dict[str, object]:
    """Describe one appliance: config, cost entity, accrual, and source.

    A structurally damaged subentry (undecodable data) resolves to explicit
    nulls with ``config_ok: false`` — the diagnostics download must never
    crash on the exact damage it exists to reveal.
    """
    try:
        config = decode_appliance_config(subentry.title, subentry.data)
    except ValueError:
        config = None
    energy_sensor = None if config is None else config.energy_sensor
    cost_entity_id = er.async_get(hass).async_get_entity_id(
        SENSOR_DOMAIN, DOMAIN, subentry.subentry_id
    )
    cost_state = None if cost_entity_id is None else hass.states.get(cost_entity_id)
    gap_raw = None if cost_state is None else cost_state.attributes.get(ATTR_PRICE_GAP_ACTIVE)
    return {
        "title": subentry.title,
        "config_ok": config is not None,
        CONF_ENERGY_SENSOR: energy_sensor,
        "cost_entity": {
            ATTR_ENTITY_ID: cost_entity_id,
            "state": None if cost_state is None else cost_state.state,
            "available": cost_state is not None and cost_state.state != STATE_UNAVAILABLE,
            ATTR_PRICE_GAP_ACTIVE: gap_raw if isinstance(gap_raw, bool) else None,
        },
        "accrual": _accrual_snapshot(hass, entry.entry_id, cost_entity_id),
        "source": _describe_source(hass, energy_sensor),
    }


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant, entry: ApplianceEnergyCostConfigEntry
) -> dict[str, object]:
    """Return redacted diagnostics for a config entry.

    Reads ``entry.data`` (never ``entry.runtime_data``, which is unset when
    setup failed — diagnostics must work exactly when things are broken)
    and narrows every field explicitly. One ``async_redact_data`` pass at
    the top covers the whole payload, nested blocks included.
    """
    raw_price_sensor = entry.data.get(CONF_PRICE_SENSOR)
    price_sensor = raw_price_sensor if isinstance(raw_price_sensor, str) else None
    raw_currency = entry.data.get(CONF_CURRENCY)
    currency = raw_currency if isinstance(raw_currency, str) else None
    appliance_subentries = [
        subentry
        for subentry in entry.subentries.values()
        if subentry.subentry_type == SUBENTRY_TYPE_APPLIANCE
    ]
    payload: dict[str, object] = {
        "entry": {
            "title": entry.title,
            "state": entry.state.value,
            "version": entry.version,
            "minor_version": entry.minor_version,
            "data": {CONF_PRICE_SENSOR: price_sensor, CONF_CURRENCY: currency},
            "options": dict(entry.options),
            "appliance_count": len(appliance_subentries),
        },
        "price_source": _describe_source(hass, price_sensor),
        "appliances": [
            _describe_appliance(hass, entry, subentry) for subentry in appliance_subentries
        ],
    }
    return async_redact_data(payload, TO_REDACT)
