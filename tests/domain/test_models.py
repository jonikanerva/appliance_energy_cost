"""Tests for the decoded configuration shapes."""

from __future__ import annotations

import pytest

from custom_components.appliance_energy_cost.models import (
    ApplianceConfig,
    EntryRuntimeData,
    decode_appliance_config,
    decode_entry_config,
)


def test_decode_narrows_a_valid_mapping() -> None:
    decoded = decode_entry_config({"price_sensor": "sensor.electricity_price", "currency": "EUR"})
    assert decoded == EntryRuntimeData(price_sensor="sensor.electricity_price", currency="EUR")


def test_decode_ignores_extra_keys() -> None:
    decoded = decode_entry_config({"price_sensor": "sensor.p", "currency": "EUR", "future_key": 1})
    assert decoded.price_sensor == "sensor.p"


@pytest.mark.parametrize(
    "data",
    [
        {},
        {"currency": "EUR"},
        {"price_sensor": "sensor.p"},
        {"price_sensor": "", "currency": "EUR"},
        {"price_sensor": "sensor.p", "currency": ""},
        {"price_sensor": 42, "currency": "EUR"},
        {"price_sensor": "sensor.p", "currency": None},
    ],
)
def test_decode_fails_closed_on_structural_damage(data: dict[str, object]) -> None:
    with pytest.raises(ValueError):
        decode_entry_config(data)


def test_decode_appliance_narrows_a_valid_subentry() -> None:
    decoded = decode_appliance_config("Heat pump", {"energy_sensor": "sensor.heat_pump_energy"})
    assert decoded == ApplianceConfig(name="Heat pump", energy_sensor="sensor.heat_pump_energy")


def test_decode_appliance_ignores_extra_keys() -> None:
    decoded = decode_appliance_config("Sauna", {"energy_sensor": "sensor.e", "future_key": 1})
    assert decoded.energy_sensor == "sensor.e"


@pytest.mark.parametrize(
    ("title", "data"),
    [
        ("", {"energy_sensor": "sensor.e"}),
        ("Heat pump", {}),
        ("Heat pump", {"energy_sensor": ""}),
        ("Heat pump", {"energy_sensor": 42}),
        ("Heat pump", {"energy_sensor": None}),
    ],
)
def test_decode_appliance_fails_closed_on_structural_damage(
    title: str, data: dict[str, object]
) -> None:
    with pytest.raises(ValueError):
        decode_appliance_config(title, data)
