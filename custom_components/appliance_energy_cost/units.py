"""Energy and price unit parsing and exact conversion to kWh-based values.

Pure domain module: stdlib only, no Home Assistant imports, no I/O.
Conversion factors are exact ``Decimal`` constants built from string
literals — no float ever touches a factor.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from enum import StrEnum
from typing import Final


class EnergyUnit(StrEnum):
    """Supported cumulative-energy units."""

    WH = "Wh"
    KWH = "kWh"
    MWH = "MWh"


@dataclass(frozen=True, slots=True)
class PriceUnit:
    """A parsed ``<currency>/<energy>`` price unit.

    The numerator text is preserved verbatim (stripped); validating it
    against the configured currency is the config boundary's job (issue #3).
    Only the energy denominator is validated here.
    """

    numerator: str
    denominator: EnergyUnit


_ENERGY_TO_KWH: Final[dict[EnergyUnit, Decimal]] = {
    EnergyUnit.WH: Decimal("0.001"),
    EnergyUnit.KWH: Decimal("1"),
    EnergyUnit.MWH: Decimal("1000"),
}

_PRICE_TO_PER_KWH: Final[dict[EnergyUnit, Decimal]] = {
    EnergyUnit.WH: Decimal("1000"),
    EnergyUnit.KWH: Decimal("1"),
    EnergyUnit.MWH: Decimal("0.001"),
}

_ENERGY_BY_NORMALISED_NAME: Final[dict[str, EnergyUnit]] = {
    unit.value.lower(): unit for unit in EnergyUnit
}


def parse_finite_decimal(raw: str) -> Decimal | None:
    """Parse a state string into a finite ``Decimal``; ``None`` when unusable.

    The single definition of finite-numeric: ``Decimal`` happily constructs
    NaN/Infinity/sNaN from their string forms (states HA sensors genuinely
    emit when a float NaN is stringified), so numericness alone is not
    enough — a non-finite value can never price or measure anything and
    fails closed to ``None``.
    """
    try:
        value = Decimal(raw)
    except InvalidOperation:
        return None
    if not value.is_finite():
        return None
    return value


def parse_energy_unit(raw: str | None) -> EnergyUnit | None:
    """Parse an energy unit case-insensitively; ``None`` when unsupported."""
    if raw is None:
        return None
    return _ENERGY_BY_NORMALISED_NAME.get(raw.strip().lower())


def parse_price_unit(raw: str | None) -> PriceUnit | None:
    """Parse a ``<currency>/<energy>`` price unit; ``None`` when unsupported.

    A missing slash, an empty numerator, or an unsupported energy
    denominator all fail closed to ``None`` — never a silent assumption.
    """
    if raw is None or "/" not in raw:
        return None
    numerator_raw, denominator_raw = raw.rsplit("/", 1)
    numerator = numerator_raw.strip()
    if not numerator:
        return None
    denominator = parse_energy_unit(denominator_raw)
    if denominator is None:
        return None
    return PriceUnit(numerator=numerator, denominator=denominator)


def currency_matches(numerator: str, currency: str) -> bool:
    """Whether a price-unit numerator names the configured currency.

    v1 is deliberately strict: case-insensitive equality after stripping
    whitespace, nothing more. A currency symbol ("€") or a subunit ("snt",
    "c") never matches — a subunit price silently accepted would make every
    cost figure 100x off, and the integration never rescales prices.
    Issue #14 owns any future symbol-equivalence map.
    """
    return numerator.strip().casefold() == currency.strip().casefold()


def to_kwh(value: Decimal, unit: EnergyUnit) -> Decimal:
    """Convert an energy value expressed in ``unit`` to kWh."""
    return value * _ENERGY_TO_KWH[unit]


def to_price_per_kwh(value: Decimal, denominator: EnergyUnit) -> Decimal:
    """Convert a price expressed per ``denominator`` to a price per kWh."""
    return value * _PRICE_TO_PER_KWH[denominator]
