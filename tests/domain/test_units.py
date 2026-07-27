"""Tests for energy and price unit parsing and conversion."""

from __future__ import annotations

from decimal import Decimal

import pytest

from custom_components.appliance_energy_cost.units import (
    EnergyUnit,
    PriceUnit,
    currency_matches,
    parse_energy_unit,
    parse_finite_decimal,
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


@pytest.mark.parametrize(
    ("numerator", "currency"),
    [
        ("EUR", "EUR"),
        ("eur", "EUR"),
        ("Eur", "EUR"),
        ("  EUR  ", "EUR"),
        ("EUR", " eur "),
    ],
)
def test_currency_matches_is_case_and_whitespace_insensitive(numerator: str, currency: str) -> None:
    assert currency_matches(numerator, currency)


@pytest.mark.parametrize(
    ("numerator", "currency"),
    [
        ("SEK", "EUR"),
        # v1 strictness (binding, see issue #14): a subunit numerator never
        # matches — silently accepting snt/kWh against EUR would make every
        # cost figure 100x off, and the integration never rescales prices.
        ("snt", "EUR"),
        ("c", "EUR"),
        # A currency symbol never matches either; issue #14 owns any future
        # symbol-equivalence map.
        ("€", "EUR"),
        ("", "EUR"),
    ],
)
def test_currency_mismatches_fail_closed(numerator: str, currency: str) -> None:
    assert not currency_matches(numerator, currency)


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("0", Decimal("0")),
        ("12.345", Decimal("12.345")),
        ("-0.5", Decimal("-0.5")),
        (" 7.25 ", Decimal("7.25")),
        ("1E+2", Decimal("100")),
    ],
)
def test_parse_finite_decimal_parses_finite_numbers(raw: str, expected: Decimal) -> None:
    assert parse_finite_decimal(raw) == expected


@pytest.mark.parametrize(
    "raw",
    [
        "",
        "abc",
        "unknown",
        "unavailable",
        "nan",
        "NaN",
        "sNaN",
        "inf",
        "Infinity",
        "-Infinity",
        "12,5",
    ],
)
def test_parse_finite_decimal_fails_closed_on_non_finite_input(raw: str) -> None:
    assert parse_finite_decimal(raw) is None
