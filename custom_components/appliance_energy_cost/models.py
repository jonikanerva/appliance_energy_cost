"""Decoded configuration shapes for the Appliance Energy Cost integration.

Pure domain module: stdlib only, no Home Assistant imports, no I/O.
The config flow is the write boundary; this module is the read boundary
that narrows a stored mapping into a typed value for ``entry.runtime_data``.
"""

from __future__ import annotations

import asyncio
from collections.abc import Mapping
from dataclasses import dataclass, field

from .const import CONF_CURRENCY, CONF_ENERGY_SENSOR, CONF_PRICE_SENSOR


@dataclass(frozen=True, slots=True)
class EntryRuntimeData:
    """Decoded per-entry configuration held in ``entry.runtime_data``.

    Also carries the entry's runtime concurrency primitive: ``import_lock``
    serialises confirmed backfill imports per entry (issue #42) — held from
    period resolution through the post-commit calibration pass, so two
    concurrent imports can never interleave their read/compute/write
    windows. Excluded from equality: the lock is entry infrastructure, not
    decoded configuration — two decodes of the same data compare equal.
    """

    price_sensor: str
    currency: str
    import_lock: asyncio.Lock = field(default_factory=asyncio.Lock, compare=False)


def decode_entry_config(data: Mapping[str, object]) -> EntryRuntimeData:
    """Decode and narrow a config entry's raw data mapping.

    Structure-only validation: raises ``ValueError`` when a key is missing
    or not a non-empty string. Availability of the referenced entities is
    deliberately not checked here — sources may be down at HA start.
    """
    price_sensor = data.get(CONF_PRICE_SENSOR)
    if not isinstance(price_sensor, str) or not price_sensor:
        raise ValueError(f"missing or invalid '{CONF_PRICE_SENSOR}' in config entry data")
    currency = data.get(CONF_CURRENCY)
    if not isinstance(currency, str) or not currency:
        raise ValueError(f"missing or invalid '{CONF_CURRENCY}' in config entry data")
    return EntryRuntimeData(price_sensor=price_sensor, currency=currency)


@dataclass(frozen=True, slots=True)
class ApplianceConfig:
    """Decoded per-appliance configuration from a config subentry."""

    name: str
    energy_sensor: str


def decode_appliance_config(title: str, data: Mapping[str, object]) -> ApplianceConfig:
    """Decode and narrow an appliance subentry's title and raw data mapping.

    Structure-only validation (mirrors ``decode_entry_config``): raises
    ``ValueError`` when the title is empty or the energy sensor is missing
    or not a non-empty string. Availability of the referenced entity is
    deliberately not checked — sources may be down at HA start.
    """
    if not title:
        raise ValueError("missing appliance subentry title")
    energy_sensor = data.get(CONF_ENERGY_SENSOR)
    if not isinstance(energy_sensor, str) or not energy_sensor:
        raise ValueError(f"missing or invalid '{CONF_ENERGY_SENSOR}' in appliance subentry data")
    return ApplianceConfig(name=title, energy_sensor=energy_sensor)
