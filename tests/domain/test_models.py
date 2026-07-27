"""Tests for the decoded configuration shapes."""

from __future__ import annotations

import pytest

from custom_components.appliance_energy_cost.models import (
    EntryRuntimeData,
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
