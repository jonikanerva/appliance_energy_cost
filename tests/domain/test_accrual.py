"""Tests for the live cost accrual state machine."""

from __future__ import annotations

from collections import Counter
from decimal import Decimal

from custom_components.appliance_energy_cost.accrual import (
    INITIAL_STATE,
    AccrualEvent,
    AccrualState,
    Transition,
    apply_energy,
    apply_price,
    calibrate,
)

D = Decimal


def _state(
    cost: str = "0",
    baseline: str | None = None,
    reading: str | None = None,
    price: str | None = None,
) -> AccrualState:
    """Build an AccrualState from string literals for exact Decimals."""
    return AccrualState(
        cost=D(cost),
        last_energy_kwh=None if baseline is None else D(baseline),
        last_reading_kwh=None if reading is None else D(reading),
        price_per_kwh=None if price is None else D(price),
    )


def test_first_reading_initialises_the_baseline_without_cost() -> None:
    result = apply_energy(INITIAL_STATE, D("100"))
    assert result.events == (AccrualEvent.BASELINE_INITIALISED,)
    assert result.state.cost == D("0")
    assert result.state.last_energy_kwh == D("100")
    assert result.state.last_reading_kwh == D("100")


def test_first_reading_initialises_even_with_a_price_active() -> None:
    result = apply_energy(_state(price="0.10"), D("100"))
    assert result.events == (AccrualEvent.BASELINE_INITIALISED,)
    assert result.state.cost == D("0")


def test_multi_step_accumulation() -> None:
    state = _state(baseline="100", reading="100", price="0.10")
    state, events = apply_energy(state, D("102"))
    assert events == (AccrualEvent.ACCRUED,)
    state, events = apply_energy(state, D("105"))
    assert events == (AccrualEvent.ACCRUED,)
    assert state.cost == D("0.50")
    assert state.last_energy_kwh == D("105")


def test_price_change_settles_pending_energy_at_the_old_price() -> None:
    # A slow-updating energy sensor: consumption happened at 0.10 but the
    # reading only arrives with the price change to 0.50.
    state = _state(baseline="100", reading="100", price="0.10")
    state, events = apply_price(state, D("0.50"), D("102"))
    assert events == (AccrualEvent.ACCRUED,)
    assert state.cost == D("0.20")
    assert state.price_per_kwh == D("0.50")
    state, _ = apply_energy(state, D("103"))
    assert state.cost == D("0.70")


def test_price_loss_settles_first_then_starts_a_gap() -> None:
    state = _state(baseline="100", reading="100", price="0.10")
    state, events = apply_price(state, None, D("102"))
    assert events == (AccrualEvent.ACCRUED, AccrualEvent.GAP_STARTED)
    assert state.cost == D("0.20")
    assert state.price_per_kwh is None
    assert state.last_energy_kwh == D("102")


def test_energy_during_a_gap_is_held_and_nothing_advances() -> None:
    state = _state(cost="1", baseline="100", reading="100")
    result = apply_energy(state, D("105"))
    assert result.events == (AccrualEvent.HELD_PRICE_GAP,)
    assert result.state.cost == D("1")
    assert result.state.last_energy_kwh == D("100")
    assert result.state.last_reading_kwh == D("105")


def test_gap_end_with_reading_prices_gap_energy_at_the_returning_price() -> None:
    state = _state(baseline="100", reading="106")
    state, events = apply_price(state, D("0.20"), D("106"))
    assert events == (AccrualEvent.GAP_ENDED, AccrualEvent.ACCRUED)
    assert state.cost == D("1.20")
    assert state.last_energy_kwh == D("106")
    assert state.price_per_kwh == D("0.20")


def test_gap_end_without_reading_prices_gap_delta_at_next_energy_event() -> None:
    state = _state(baseline="100", reading="106")
    state, events = apply_price(state, D("0.20"), None)
    assert events == (AccrualEvent.GAP_ENDED,)
    assert state.cost == D("0")
    state, events = apply_energy(state, D("106"))
    assert events == (AccrualEvent.ACCRUED,)
    assert state.cost == D("1.20")


def test_gap_paths_are_equivalent_including_a_reset_during_the_gap() -> None:
    # The with-reading and without-reading gap-end variants must produce the
    # same final state and the same events (modulo ordering) for the same
    # inputs — including a meter reset that happens while the gap is active.
    def run(gap_end_with_reading: bool) -> tuple[AccrualState, Counter[AccrualEvent]]:
        events: list[AccrualEvent] = []

        def step(transition: Transition) -> AccrualState:
            events.extend(transition.events)
            return transition.state

        state = _state(baseline="100", reading="100", price="0.10")
        state = step(apply_price(state, None, D("100")))
        state = step(apply_energy(state, D("40")))  # reset during the gap
        state = step(apply_energy(state, D("45")))
        if gap_end_with_reading:
            state = step(apply_price(state, D("0.20"), D("45")))
        else:
            state = step(apply_price(state, D("0.20"), None))
            state = step(apply_energy(state, D("45")))
        return state, Counter(events)

    with_reading = run(gap_end_with_reading=True)
    without_reading = run(gap_end_with_reading=False)
    assert with_reading == without_reading
    final_state, event_counts = with_reading
    # Full post-reset reading priced at the returning price: 45 kWh x 0.20.
    assert final_state.cost == D("9.00")
    assert final_state.last_energy_kwh == D("45")
    assert event_counts[AccrualEvent.METER_RESET] == 1
    assert event_counts[AccrualEvent.GAP_ENDED] == 1


def test_gap_continuation_price_event_tracks_the_reading() -> None:
    # A price event that stays unavailable but carries a reading must consume
    # it like any gap energy event: against a stale raw reading of 100, the
    # later true reset (95 < 0.9 x 150) would misclassify as a dip.
    state = _state(baseline="100", reading="100")
    state, events = apply_price(state, None, D("150"))
    assert events == (AccrualEvent.HELD_PRICE_GAP,)
    assert state.last_reading_kwh == D("150")
    assert state.last_energy_kwh == D("100")
    state, events = apply_energy(state, D("95"))
    assert events == (AccrualEvent.METER_RESET,)
    assert state.last_energy_kwh == D("0")
    assert state.last_reading_kwh == D("95")


def test_gap_continuation_price_event_without_reading_is_a_no_op() -> None:
    state = _state(cost="1", baseline="100", reading="100")
    result = apply_price(state, None, None)
    assert result.events == ()
    assert result.state == state


def test_price_change_without_reading_prices_later_energy_at_the_new_price() -> None:
    # Pinned degraded behaviour: with no reading at switch time, energy
    # accumulated under the old price is priced at the new price later.
    state = _state(baseline="100", reading="100", price="0.10")
    state, events = apply_price(state, D("0.50"), None)
    assert events == ()
    state, _ = apply_energy(state, D("102"))
    assert state.cost == D("1.00")


def test_reset_charges_the_full_new_reading() -> None:
    state = _state(baseline="100", reading="100", price="0.10")
    result = apply_energy(state, D("5"))
    assert result.events == (AccrualEvent.METER_RESET,)
    assert result.state.cost == D("0.50")
    assert result.state.last_energy_kwh == D("5")
    assert result.state.last_reading_kwh == D("5")


def test_decrease_at_the_reset_boundary_is_a_dip_not_a_reset() -> None:
    # HA's predicate: reset iff new < 0.9 x last reading; exactly 90 % dips.
    state = _state(baseline="100", reading="100", price="0.10")
    assert apply_energy(state, D("90")).events == (AccrualEvent.METER_DIP,)
    assert apply_energy(state, D("89.999")).events == (AccrualEvent.METER_RESET,)


def test_dip_charges_nothing_and_holds_the_baseline() -> None:
    state = _state(cost="2", baseline="100", reading="100", price="0.10")
    result = apply_energy(state, D("95"))
    assert result.events == (AccrualEvent.METER_DIP,)
    assert result.state.cost == D("2")
    assert result.state.last_energy_kwh == D("100")
    assert result.state.last_reading_kwh == D("95")


def test_dip_oscillation_fabricates_zero_cost() -> None:
    # Float-noise oscillation between adjacent readings must never accrue.
    state = _state(baseline="1000.0001", reading="1000.0001", price="1")
    for _ in range(10):
        state, events = apply_energy(state, D("1000.0000"))
        assert events == (AccrualEvent.METER_DIP,)
        state, events = apply_energy(state, D("1000.0001"))
        assert events == ()
    assert state.cost == D("0")
    assert state.last_energy_kwh == D("1000.0001")


def test_recovery_after_dip_charges_only_above_the_high_water_mark() -> None:
    state = _state(baseline="100", reading="100", price="0.10")
    state, _ = apply_energy(state, D("95"))  # dip
    state, events = apply_energy(state, D("98"))  # recovery below high water
    assert events == ()
    assert state.cost == D("0")
    state, events = apply_energy(state, D("103"))  # above high water
    assert events == (AccrualEvent.ACCRUED,)
    assert state.cost == D("0.30")
    assert state.last_energy_kwh == D("103")


def test_reset_never_decreases_cost_with_a_non_negative_price() -> None:
    state = _state(cost="7", baseline="5000", reading="5000", price="0.10")
    result = apply_energy(state, D("0"))
    assert result.events == (AccrualEvent.METER_RESET,)
    assert result.state.cost == D("7")
    assert result.state.cost >= state.cost


def test_negative_price_decreases_cost() -> None:
    state = _state(baseline="100", reading="100", price="-0.05")
    result = apply_energy(state, D("102"))
    assert result.state.cost == D("-0.10")


def test_zero_delta_changes_nothing_and_emits_no_events() -> None:
    state = _state(cost="3", baseline="100", reading="100", price="0.10")
    result = apply_energy(state, D("100"))
    assert result.events == ()
    assert result.state == state


def test_calibrate_sets_cost_and_rebaselines() -> None:
    state = _state(cost="5", baseline="100", reading="100", price="0.10")
    state, events = calibrate(state, D("2"), D("110"))
    assert events == (AccrualEvent.CALIBRATED,)
    assert state.cost == D("2")
    assert state.last_energy_kwh == D("110")
    assert state.last_reading_kwh == D("110")
    state, events = apply_energy(state, D("111"))
    assert events == (AccrualEvent.ACCRUED,)
    assert state.cost == D("2.10")


def test_reset_to_zero_is_calibrate_with_zero() -> None:
    state = _state(cost="5", baseline="100", reading="100", price="0.10")
    result = calibrate(state, D("0"), D("100"))
    assert result.state.cost == D("0")
    assert result.state.last_energy_kwh == D("100")


def test_restored_state_does_not_double_count() -> None:
    # Restore path (#4): cost and baseline come back, price is unknown and
    # the raw reading initialises to the restored baseline.
    restored = AccrualState(
        cost=D("5"),
        last_energy_kwh=D("100"),
        last_reading_kwh=D("100"),
        price_per_kwh=None,
    )
    state, events = apply_price(restored, D("0.10"), D("100"))
    assert events == (AccrualEvent.GAP_ENDED,)
    assert state.cost == D("5")
    state, _ = apply_energy(state, D("101"))
    assert state.cost == D("5.10")


def test_decimal_precision_over_ten_thousand_small_steps() -> None:
    state = _state(baseline="0", reading="0", price="0.1")
    reading = D("0")
    for _ in range(10_000):
        reading += D("0.001")
        state, _ = apply_energy(state, reading)
    assert state.cost == D("1")
    assert state.last_energy_kwh == D("10")
