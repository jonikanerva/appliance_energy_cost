"""Decoded configuration shapes for the Appliance Energy Cost integration.

Pure domain module: stdlib only, no Home Assistant imports, no I/O.
The config flow is the write boundary; this module is the read boundary
that narrows a stored mapping into a typed value for ``entry.runtime_data``.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass

from .const import CONF_CURRENCY, CONF_ENERGY_SENSOR, CONF_PRICE_SENSOR


@dataclass(frozen=True, slots=True)
class EntryRuntimeData:
    """Decoded per-entry configuration held in ``entry.runtime_data``."""

    price_sensor: str
    currency: str


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
