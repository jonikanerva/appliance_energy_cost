"""Tests for energy and price unit parsing and conversion."""

from __future__ import annotations

from decimal import Decimal

import pytest

from custom_components.appliance_energy_cost.units import (
    EnergyUnit,
    PriceUnit,
    parse_energy_unit,
    parse_price_unit,
    to_kwh,
    to_price_per_kwh,
)


@pytest.mark.parametrize(
    ("unit", "expected"),
    [
        (EnergyUnit.WH, Decimal("0.001")),
        (EnergyUnit.KWH, Decimal("1")),
        (EnergyUnit.MWH, Decimal("1000")),
    ],
)
def test_energy_conversion_factors_are_exact(unit: EnergyUnit, expected: Decimal) -> None:
    assert to_kwh(Decimal("1"), unit) == expected


@pytest.mark.parametrize(
    ("denominator", "expected"),
    [
        (EnergyUnit.WH, Decimal("1000")),
        (EnergyUnit.KWH, Decimal("1")),
        (EnergyUnit.MWH, Decimal("0.001")),
    ],
)
def test_price_conversion_factors_are_exact(denominator: EnergyUnit, expected: Decimal) -> None:
    assert to_price_per_kwh(Decimal("1"), denominator) == expected


def test_conversion_stays_in_decimal() -> None:
    assert to_kwh(Decimal("2500"), EnergyUnit.WH) == Decimal("2.5")
    assert to_price_per_kwh(Decimal("200"), EnergyUnit.MWH) == Decimal("0.2")


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("Wh", EnergyUnit.WH),
        ("kWh", EnergyUnit.KWH),
        ("MWh", EnergyUnit.MWH),
        ("KWH", EnergyUnit.KWH),
        ("mwh", EnergyUnit.MWH),
        ("  wh  ", EnergyUnit.WH),
    ],
)
def test_energy_unit_parsing_is_case_and_whitespace_insensitive(
    raw: str, expected: EnergyUnit
) -> None:
    assert parse_energy_unit(raw) is expected


@pytest.mark.parametrize("raw", ["J", "GJ", "kW", "", "   ", None])
def test_unsupported_energy_units_fail_closed(raw: str | None) -> None:
    assert parse_energy_unit(raw) is None


@pytest.mark.parametrize(
    ("raw", "numerator", "denominator"),
    [
        ("EUR/kWh", "EUR", EnergyUnit.KWH),
        ("€/kWh", "€", EnergyUnit.KWH),
        ("snt/kWh", "snt", EnergyUnit.KWH),
        ("EUR/MWh", "EUR", EnergyUnit.MWH),
        ("EUR/Wh", "EUR", EnergyUnit.WH),
        (" EUR / kwh ", "EUR", EnergyUnit.KWH),
    ],
)
def test_price_unit_parsing_preserves_the_numerator(
    raw: str, numerator: str, denominator: EnergyUnit
) -> None:
    assert parse_price_unit(raw) == PriceUnit(numerator=numerator, denominator=denominator)


@pytest.mark.parametrize("raw", ["EUR/GJ", "EUR", "/kWh", " / kWh", "", None])
def test_unsupported_price_units_fail_closed(raw: str | None) -> None:
    assert parse_price_unit(raw) is None
