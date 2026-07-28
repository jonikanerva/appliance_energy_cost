"""Tests for the pure backfill cost-series calculation."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

from custom_components.appliance_energy_cost.backfill import (
    BackfillSeries,
    EnergyRow,
    PriceRow,
    build_backfill_series,
)
from custom_components.appliance_energy_cost.units import EnergyUnit

D = Decimal

# 2023-11-14 23:00:00 UTC - an exact top-of-hour epoch timestamp.
HOUR_0 = 1_700_002_800
HOUR = 3600


def _hour(index: int) -> float:
    return float(HOUR_0 + index * HOUR)


def _hour_dt(index: int) -> datetime:
    return datetime.fromtimestamp(HOUR_0 + index * HOUR, tz=UTC)


def _build(
    energy_rows: list[EnergyRow],
    price_rows: list[PriceRow],
    *,
    energy_unit: EnergyUnit = EnergyUnit.KWH,
    price_unit: EnergyUnit = EnergyUnit.KWH,
    initial_cost: Decimal = Decimal("0"),
) -> BackfillSeries:
    return build_backfill_series(
        energy_rows,
        price_rows,
        energy_unit=energy_unit,
        price_unit=price_unit,
        initial_cost=initial_cost,
    )


def test_hourly_prices_are_matched_to_their_energy_hours() -> None:
    result = _build(
        [
            {"start": _hour(0), "change": 2.0},
            {"start": _hour(1), "change": 1.0},
        ],
        [
            {"start": _hour(0), "mean": 0.10},
            {"start": _hour(1), "mean": 0.40},
        ],
    )
    assert result.total_energy_kwh == D("3")
    assert result.total_cost == D("0.6")
    assert [point.state for point in result.points] == [0.2, 0.6]
    assert [point.sum for point in result.points] == [0.2, 0.6]
    assert result.missing_price_hours == ()
    assert result.invalid_energy_hours == ()


def test_wh_energy_and_eur_per_mwh_price_are_converted() -> None:
    result = _build(
        [{"start": _hour(0), "change": 2_000.0}],
        [{"start": _hour(0), "mean": 200.0}],
        energy_unit=EnergyUnit.WH,
        price_unit=EnergyUnit.MWH,
    )
    assert result.total_energy_kwh == D("2")
    assert result.total_cost == D("0.4")


def test_missing_price_is_reported_only_when_energy_was_consumed() -> None:
    result = _build(
        [
            {"start": _hour(0), "change": 0.0},
            {"start": _hour(1), "change": 1.0},
        ],
        [],
    )
    # The zero-consumption hour still emits a flat point: its cost delta is
    # zero with certainty even without a price.
    assert len(result.points) == 1
    assert result.points[0].start == _hour_dt(0)
    assert result.missing_price_hours == (_hour_dt(1),)
    assert result.total_cost == D("0")


def test_negative_change_is_invalid_and_skipped() -> None:
    result = _build(
        [{"start": _hour(0), "change": -1.0}],
        [{"start": _hour(0), "mean": 0.2}],
    )
    assert result.points == ()
    assert result.invalid_energy_hours == (_hour_dt(0),)
    assert result.total_energy_kwh == D("0")


def test_absent_change_is_invalid_and_skipped() -> None:
    result = _build(
        [{"start": _hour(0)}, {"start": _hour(1), "change": None}],
        [{"start": _hour(0), "mean": 0.2}, {"start": _hour(1), "mean": 0.2}],
    )
    assert result.points == ()
    assert result.invalid_energy_hours == (_hour_dt(0), _hour_dt(1))


def test_dip_hour_overshoot_equals_the_dip_amplitude() -> None:
    # Pinned known asymmetry: a meter dip makes Recorder emit a negative
    # change for the dip hour (reported invalid, skipped) and the recovery
    # hour's change re-climbs the dip, so the series overshoots true
    # consumption by exactly the dip amplitude (0.5 kWh here, 0.05 in cost).
    result = _build(
        [
            {"start": _hour(0), "change": 2.0},
            {"start": _hour(1), "change": -0.5},
            {"start": _hour(2), "change": 1.5},
        ],
        [
            {"start": _hour(0), "mean": 0.10},
            {"start": _hour(1), "mean": 0.10},
            {"start": _hour(2), "mean": 0.10},
        ],
    )
    assert result.invalid_energy_hours == (_hour_dt(1),)
    true_consumption = D("3.0")
    assert result.total_energy_kwh - true_consumption == D("0.5")
    assert result.total_cost == D("0.35")


def test_tiny_negative_change_is_clamped_to_zero() -> None:
    result = _build(
        [{"start": _hour(0), "change": -1e-10}],
        [{"start": _hour(0), "mean": 0.2}],
    )
    assert result.invalid_energy_hours == ()
    assert len(result.points) == 1
    assert result.total_energy_kwh == D("0")
    assert result.total_cost == D("0")


def test_initial_cost_offsets_state_sum_and_total() -> None:
    result = _build(
        [{"start": _hour(0), "change": 1.0}],
        [{"start": _hour(0), "mean": 0.25}],
        initial_cost=D("10"),
    )
    assert result.total_cost == D("10.25")
    assert result.points[0].state == 10.25
    assert result.points[0].sum == 10.25


def test_end_energy_comes_from_the_last_state() -> None:
    result = _build(
        [
            {"start": _hour(0), "change": 1.0, "state": 122.5},
            {"start": _hour(1), "change": 1.0, "state": 123.5},
        ],
        [
            {"start": _hour(0), "mean": 0.1},
            {"start": _hour(1), "mean": 0.1},
        ],
    )
    assert result.end_energy_kwh == D("123.5")


def test_end_energy_is_unit_converted_and_tracked_through_invalid_rows() -> None:
    # The LAST row's state counts REGARDLESS of the row's validity: a
    # skipped row's reading is still the meter's level (issue #42 pin).
    result = _build(
        [
            {"start": _hour(0), "change": 1_000.0, "state": 122_500.0},
            {"start": _hour(1), "change": None, "state": 123_500.0},
        ],
        [{"start": _hour(0), "mean": 0.1}],
        energy_unit=EnergyUnit.WH,
    )
    assert result.end_energy_kwh == D("123.5")
    assert result.invalid_energy_hours == (_hour_dt(1),)


def test_end_energy_is_none_when_the_last_row_carries_no_state() -> None:
    """Stale-last regression (issue #42 audit fold, the PR #40 doc contract).

    An earlier row's state must never leak into end_energy_kwh when the
    period's LAST row carries none: a stale reading fed into the cutover
    formula would double-charge the tail.
    """
    result = _build(
        [
            {"start": _hour(0), "change": 1.0, "state": 122.5},
            {"start": _hour(1), "change": 1.0},
        ],
        [
            {"start": _hour(0), "mean": 0.1},
            {"start": _hour(1), "mean": 0.1},
        ],
    )
    assert result.end_energy_kwh is None
    # Both hours still cost normally: the state field never affects pricing.
    assert result.total_cost == D("0.2")


def test_end_energy_is_none_when_the_last_row_state_is_none() -> None:
    result = _build(
        [
            {"start": _hour(0), "change": 1.0, "state": 122.5},
            {"start": _hour(1), "change": 1.0, "state": None},
        ],
        [
            {"start": _hour(0), "mean": 0.1},
            {"start": _hour(1), "mean": 0.1},
        ],
    )
    assert result.end_energy_kwh is None


def test_end_energy_uses_the_last_row_by_start_on_unsorted_input() -> None:
    result = _build(
        [
            {"start": _hour(1), "change": 1.0, "state": 123.5},
            {"start": _hour(0), "change": 1.0, "state": 122.5},
        ],
        [
            {"start": _hour(0), "mean": 0.1},
            {"start": _hour(1), "mean": 0.1},
        ],
    )
    assert result.end_energy_kwh == D("123.5")


def test_non_finite_last_state_is_treated_as_absent() -> None:
    """Ingestion-edge finiteness on the state field (issue #42 amendment 3).

    Infinity survives SQLite (NaN does not — coerced to NULL), so both are
    guarded at our edge: a non-finite state means end_energy_kwh is None and
    the one-call calibration skips visibly instead of computing from it.
    """
    for bad_state in (float("inf"), float("-inf"), float("nan")):
        result = _build(
            [{"start": _hour(0), "change": 1.0, "state": bad_state}],
            [{"start": _hour(0), "mean": 0.1}],
        )
        assert result.end_energy_kwh is None
        assert result.total_cost == D("0.1")


def test_non_finite_change_is_invalid_and_skipped() -> None:
    for bad_change in (float("inf"), float("-inf"), float("nan")):
        result = _build(
            [{"start": _hour(0), "change": bad_change}],
            [{"start": _hour(0), "mean": 0.1}],
        )
        assert result.points == ()
        assert result.invalid_energy_hours == (_hour_dt(0),)
        assert result.total_energy_kwh == D("0")
        assert result.total_cost == D("0")


def test_non_finite_price_is_treated_as_a_missing_price() -> None:
    for bad_price in (float("inf"), float("-inf"), float("nan")):
        result = _build(
            [{"start": _hour(0), "change": 1.0}],
            [{"start": _hour(0), "mean": bad_price}],
        )
        assert result.points == ()
        assert result.missing_price_hours == (_hour_dt(0),)
        assert result.total_cost == D("0")


def test_non_finite_price_mean_does_not_fall_back_to_state() -> None:
    # The mean-over-state preference applies before the finiteness guard:
    # a non-finite mean makes the HOUR unpriced rather than silently
    # repricing it from the state field.
    result = _build(
        [{"start": _hour(0), "change": 1.0}],
        [{"start": _hour(0), "mean": float("inf"), "state": 0.25}],
    )
    assert result.missing_price_hours == (_hour_dt(0),)


def test_negative_price_decreases_the_cumulative_sum() -> None:
    result = _build(
        [
            {"start": _hour(0), "change": 1.0},
            {"start": _hour(1), "change": 1.0},
        ],
        [
            {"start": _hour(0), "mean": 0.5},
            {"start": _hour(1), "mean": -0.2},
        ],
    )
    assert [point.sum for point in result.points] == [0.5, 0.3]
    assert result.total_cost == D("0.3")


def test_zero_consumption_hour_with_price_emits_a_flat_point() -> None:
    result = _build(
        [
            {"start": _hour(0), "change": 2.0},
            {"start": _hour(1), "change": 0.0},
        ],
        [
            {"start": _hour(0), "mean": 0.1},
            {"start": _hour(1), "mean": 0.4},
        ],
    )
    assert [point.state for point in result.points] == [0.2, 0.2]


def test_price_mean_is_preferred_over_state() -> None:
    result = _build(
        [{"start": _hour(0), "change": 1.0}],
        [{"start": _hour(0), "mean": 0.2, "state": 0.9}],
    )
    assert result.total_cost == D("0.2")


def test_price_state_is_the_fallback_when_mean_is_absent() -> None:
    result = _build(
        [{"start": _hour(0), "change": 1.0}],
        [{"start": _hour(0), "state": 0.25}],
    )
    assert result.total_cost == D("0.25")


def test_unsorted_energy_rows_are_ordered_by_start() -> None:
    result = _build(
        [
            {"start": _hour(1), "change": 1.0},
            {"start": _hour(0), "change": 1.0},
        ],
        [
            {"start": _hour(0), "mean": 0.1},
            {"start": _hour(1), "mean": 0.3},
        ],
    )
    assert [point.start for point in result.points] == [_hour_dt(0), _hour_dt(1)]
    assert [point.sum for point in result.points] == [0.1, 0.4]


def test_extra_price_rows_are_ignored() -> None:
    result = _build(
        [{"start": _hour(1), "change": 1.0}],
        [
            {"start": _hour(0), "mean": 9.9},
            {"start": _hour(1), "mean": 0.3},
            {"start": _hour(2), "mean": 9.9},
        ],
    )
    assert result.total_cost == D("0.3")


def test_timestamps_align_on_the_rounded_second() -> None:
    result = _build(
        [{"start": _hour(0) + 0.2, "change": 1.0}],
        [{"start": _hour(0), "mean": 0.3}],
    )
    assert result.total_cost == D("0.3")
    assert result.points[0].start == _hour_dt(0)


def test_empty_inputs_produce_an_empty_series() -> None:
    result = _build([], [])
    assert result.points == ()
    assert result.total_energy_kwh == D("0")
    assert result.total_cost == D("0")
    assert result.end_energy_kwh is None
    assert result.missing_price_hours == ()
    assert result.invalid_energy_hours == ()


def test_cost_points_are_aware_utc_top_of_hour() -> None:
    result = _build(
        [{"start": _hour(0), "change": 1.0}],
        [{"start": _hour(0), "mean": 0.1}],
    )
    start = result.points[0].start
    assert start.tzinfo is UTC
    assert start == datetime(2023, 11, 14, 23, 0, tzinfo=UTC)
    assert (start.minute, start.second, start.microsecond) == (0, 0, 0)


def test_a_full_year_of_hours_accumulates_exactly() -> None:
    # 8760 x (0.1 kWh x 0.1 EUR/kWh) drifts in float arithmetic; the
    # Decimal pipeline must produce the exact total.
    energy: list[EnergyRow] = [{"start": _hour(i), "change": 0.1} for i in range(8760)]
    prices: list[PriceRow] = [{"start": _hour(i), "mean": 0.1} for i in range(8760)]
    result = _build(energy, prices)
    assert len(result.points) == 8760
    assert result.total_energy_kwh == D("876")
    assert result.total_cost == D("87.6")
    assert result.points[-1].sum == 87.6
