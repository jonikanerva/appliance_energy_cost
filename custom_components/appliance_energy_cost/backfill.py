"""Pure backfill cost-series calculation over hourly statistics rows.

Pure domain module: stdlib only, no Home Assistant imports, no I/O. The
row shapes mirror Recorder's ``StatisticsRow`` wire shape verbatim
(``start`` is epoch seconds, UTC); service wiring lives elsewhere.

Floats from the wire enter ``Decimal`` exactly once via ``Decimal(str(v))``;
the running cumulative sum stays in ``Decimal`` and is converted to float
only at the two documented exit points (``CostPoint.state`` / ``.sum``).
"""

from __future__ import annotations

import math
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from typing import Final, NotRequired, TypedDict

from .units import EnergyUnit, to_kwh, to_price_per_kwh


class EnergyRow(TypedDict):
    """One hourly energy statistics row (Recorder ``StatisticsRow`` shape)."""

    start: float
    change: NotRequired[float | None]
    state: NotRequired[float | None]


class PriceRow(TypedDict):
    """One hourly price statistics row (Recorder ``StatisticsRow`` shape)."""

    start: float
    mean: NotRequired[float | None]
    state: NotRequired[float | None]


@dataclass(frozen=True, slots=True)
class CostPoint:
    """One hourly cumulative cost point ready for the statistics import API.

    ``start`` is an aware-UTC top-of-hour instant (the import API rejects
    naive or non-top-of-hour values). ``state`` and ``sum`` are the
    documented float exit points of the Decimal pipeline.
    """

    start: datetime
    state: float
    sum: float


@dataclass(frozen=True, slots=True)
class BackfillSeries:
    """Calculated backfill series with its validation report.

    The first/last point are derivable from ``points`` and deliberately
    not duplicated here. ``end_energy_kwh`` is ``None`` unless the period's
    LAST energy row — last by ``start``, regardless of validity, because a
    skipped row's reading is still the meter's level — carries a finite
    cumulative ``state``.
    """

    points: tuple[CostPoint, ...]
    total_energy_kwh: Decimal
    total_cost: Decimal
    end_energy_kwh: Decimal | None
    missing_price_hours: tuple[datetime, ...]
    invalid_energy_hours: tuple[datetime, ...]


ZERO_TOLERANCE_KWH: Final = Decimal("1E-9")
"""Below this magnitude an energy change is treated as zero (float noise)."""


def _timestamp_key(start: float) -> int:
    """Normalise an epoch-seconds timestamp to the integer alignment key."""
    return round(start)


def _finite_or_none(value: float | None) -> float | None:
    """Treat a non-finite wire value as absent — the ingestion finiteness edge.

    SQLite coerces NaN to NULL but lets Infinity through, and PostgreSQL
    preserves NaN — so both are guarded here at our edge regardless of the
    backing database: a non-finite price, change, or state can never price,
    measure, or baseline anything (issue #42 audit fold).
    """
    if value is None or not math.isfinite(value):
        return None
    return value


def _row_start(row: EnergyRow) -> float:
    return row["start"]


def _price_value(row: PriceRow) -> float | None:
    """Prefer the hourly mean price; fall back to the hourly state."""
    mean = row.get("mean")
    if mean is not None:
        return mean
    return row.get("state")


def build_backfill_series(
    energy_rows: Iterable[EnergyRow],
    price_rows: Iterable[PriceRow],
    *,
    energy_unit: EnergyUnit,
    price_unit: EnergyUnit,
    initial_cost: Decimal = Decimal("0"),
) -> BackfillSeries:
    """Build the hourly cumulative cost series for one appliance.

    Each hour's cost is its energy ``change`` (converted to kWh) times that
    hour's mean price (fallback: the hourly price ``state``), accumulated on
    top of ``initial_cost``. Hours with consumption but no price are
    reported in ``missing_price_hours`` and skipped; hours with a negative
    ``change`` beyond tolerance are reported in ``invalid_energy_hours`` and
    skipped (Recorder already reset-compensates ``change``, so a negative
    value is abnormal). A negative *price* is valid and may decrease the
    cumulative sum. ``price_unit`` is the energy denominator of the price.

    Non-finite wire values fail closed at this edge (``_finite_or_none``):
    a non-finite price is an absent price, a non-finite ``change`` is an
    invalid-energy hour, and a non-finite ``state`` is an absent state.
    ``end_energy_kwh`` follows the ``BackfillSeries`` contract: ``None``
    unless the period's last energy row carries a finite ``state``.
    """
    prices: dict[int, Decimal] = {}
    for price_row in price_rows:
        raw_price = _finite_or_none(_price_value(price_row))
        if raw_price is not None:
            prices[_timestamp_key(price_row["start"])] = to_price_per_kwh(
                Decimal(str(raw_price)), price_unit
            )

    running_cost = initial_cost
    total_energy = Decimal("0")
    points: list[CostPoint] = []
    missing_price_hours: list[datetime] = []
    invalid_energy_hours: list[datetime] = []

    sorted_rows = sorted(energy_rows, key=_row_start)
    for row in sorted_rows:
        key = _timestamp_key(row["start"])
        start = datetime.fromtimestamp(key, tz=UTC)

        raw_change = _finite_or_none(row.get("change"))
        if raw_change is None:
            invalid_energy_hours.append(start)
            continue
        change = to_kwh(Decimal(str(raw_change)), energy_unit)
        if change < -ZERO_TOLERANCE_KWH:
            invalid_energy_hours.append(start)
            continue
        if change < 0:
            change = Decimal("0")

        price = prices.get(key)
        if price is None:
            if change > ZERO_TOLERANCE_KWH:
                missing_price_hours.append(start)
                continue
            # A zero-consumption hour needs no price: its cost delta is
            # zero with certainty, so the series still emits a flat point.
            price = Decimal("0")

        total_energy += change
        running_cost += change * price
        points.append(CostPoint(start=start, state=float(running_cost), sum=float(running_cost)))

    # The LAST row's state — regardless of the row's validity — is the
    # meter's level at end; an earlier row's state would be stale and, fed
    # into the cutover formula, would double-charge the tail (issue #42).
    end_energy: Decimal | None = None
    if sorted_rows:
        raw_end_state = _finite_or_none(sorted_rows[-1].get("state"))
        if raw_end_state is not None:
            end_energy = to_kwh(Decimal(str(raw_end_state)), energy_unit)

    return BackfillSeries(
        points=tuple(points),
        total_energy_kwh=total_energy,
        total_cost=running_cost,
        end_energy_kwh=end_energy,
        missing_price_hours=tuple(missing_price_hours),
        invalid_energy_hours=tuple(invalid_energy_hours),
    )
