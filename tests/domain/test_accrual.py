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
    cutover_value,
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
        state = step(apply_energy(state, D("105")))  # 5 kWh held during the gap
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
    # Pinned deviation: the 5 kWh held before the reset (100 -> 105) is
    # dropped, not priced at the returning price - a reset destroys the
    # meter reference for unsettled gap energy (undercharge, never
    # fabricate). Cost is exactly 45 x 0.20, not (5 + 45) x 0.20.
    assert final_state.cost == D("9.00")
    assert final_state.cost == D("45") * D("0.20")
    assert final_state.last_energy_kwh == D("45")
    assert event_counts[AccrualEvent.HELD_PRICE_GAP] == 2
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


def test_price_change_with_reading_initialises_a_missing_baseline() -> None:
    # Autonomy deviation #2: a price change carrying a reading before any
    # energy event initialises the baseline - the same outcome as the first
    # energy event, realised earlier; nothing is ever charged for it.
    state = _state(price="0.10")
    state, events = apply_price(state, D("0.50"), D("100"))
    assert events == (AccrualEvent.BASELINE_INITIALISED,)
    assert state.cost == D("0")
    assert state.last_energy_kwh == D("100")
    assert state.last_reading_kwh == D("100")
    assert state.price_per_kwh == D("0.50")


def test_first_price_with_reading_initialises_the_baseline_from_initial_state() -> None:
    state, events = apply_price(INITIAL_STATE, D("0.10"), D("100"))
    assert events == (AccrualEvent.GAP_ENDED, AccrualEvent.BASELINE_INITIALISED)
    assert state.cost == D("0")
    assert state.last_energy_kwh == D("100")
    assert state.price_per_kwh == D("0.10")


def test_unchanged_reading_during_a_gap_is_a_no_op() -> None:
    # Autonomy deviation #3: no energy moved, so there is nothing to hold.
    state = _state(cost="1", baseline="100", reading="105")
    result = apply_energy(state, D("105"))
    assert result.events == ()
    assert result.state == state


def test_dip_during_a_gap_holds_the_baseline_and_tracks_the_reading() -> None:
    state = _state(cost="1", baseline="100", reading="100")
    result = apply_energy(state, D("95"))
    assert result.events == (AccrualEvent.METER_DIP,)
    assert result.state.cost == D("1")
    assert result.state.last_energy_kwh == D("100")
    assert result.state.last_reading_kwh == D("95")


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


def test_negative_reading_is_invalid_and_changes_nothing() -> None:
    # Without the guard this would classify as a reset and charge -5 x 0.10,
    # silently decreasing the cumulative cost.
    state = _state(cost="7", baseline="5000", reading="5000", price="0.10")
    result = apply_energy(state, D("-5"))
    assert result.events == (AccrualEvent.INVALID_READING,)
    assert result.state == state


def test_negative_first_reading_never_becomes_the_baseline() -> None:
    # A -5 baseline would fabricate 0.50 of cost on the next real reading.
    state = _state(price="0.10")
    state, events = apply_energy(state, D("-5"))
    assert events == (AccrualEvent.INVALID_READING,)
    assert state.last_energy_kwh is None
    assert state.last_reading_kwh is None
    state, events = apply_energy(state, D("100"))
    assert events == (AccrualEvent.BASELINE_INITIALISED,)
    assert state.cost == D("0")


def test_negative_reading_during_a_gap_is_invalid() -> None:
    state = _state(cost="1", baseline="100", reading="100")
    result = apply_energy(state, D("-5"))
    assert result.events == (AccrualEvent.INVALID_READING,)
    assert result.state == state


def test_negative_reading_on_price_change_is_discarded_visibly() -> None:
    # The price change itself stays valid; the settlement falls back to the
    # documented no-reading degraded path.
    state = _state(baseline="100", reading="100", price="0.10")
    state, events = apply_price(state, D("0.50"), D("-5"))
    assert events == (AccrualEvent.INVALID_READING,)
    assert state.price_per_kwh == D("0.50")
    assert state.last_energy_kwh == D("100")
    assert state.cost == D("0")


def test_negative_reading_on_price_loss_still_starts_the_gap() -> None:
    state = _state(baseline="100", reading="100", price="0.10")
    state, events = apply_price(state, None, D("-5"))
    assert events == (AccrualEvent.INVALID_READING, AccrualEvent.GAP_STARTED)
    assert state.price_per_kwh is None
    assert state.last_reading_kwh == D("100")


def test_negative_reading_on_gap_end_defers_settlement() -> None:
    state = _state(baseline="100", reading="106")
    state, events = apply_price(state, D("0.20"), D("-5"))
    assert events == (AccrualEvent.INVALID_READING, AccrualEvent.GAP_ENDED)
    assert state.cost == D("0")
    assert state.last_energy_kwh == D("100")
    # The held baseline still prices the gap delta at the next valid reading.
    state, events = apply_energy(state, D("106"))
    assert events == (AccrualEvent.ACCRUED,)
    assert state.cost == D("1.20")


def test_negative_reading_in_gap_continuation_price_event_is_invalid() -> None:
    state = _state(cost="1", baseline="100", reading="100")
    result = apply_price(state, None, D("-5"))
    assert result.events == (AccrualEvent.INVALID_READING,)
    assert result.state == state


def test_calibrate_with_negative_reading_is_rejected_whole() -> None:
    state = _state(cost="5", baseline="100", reading="100", price="0.10")
    result = calibrate(state, D("0"), D("-5"))
    assert result.events == (AccrualEvent.INVALID_READING,)
    assert result.state == state


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


def test_total_class_legitimate_decrease_is_charged_as_a_reset() -> None:
    """Recorded trade-off (issue #3): state_class ``total`` sources are admitted.

    The domain has exactly one decrease rule, tuned for cumulative
    consumption meters: a drop below 90% of the last raw reading is a
    METER_RESET that charges the whole new reading as consumption since
    the reset. A genuinely decreasing ``total`` source (e.g. net metering
    that subtracts exported energy) therefore gets its post-decrease
    reading charged rather than credited — VISION.md scopes the product
    to costing consumed energy, never crediting exports.
    """
    state = _state(baseline="100", reading="100", price="0.10")
    state, events = apply_energy(state, D("50"))
    assert events == (AccrualEvent.METER_RESET,)
    assert state.cost == D("5.00")
    assert state.last_energy_kwh == D("50")
    assert state.last_reading_kwh == D("50")


def test_cutover_advance_prices_the_metered_since_end_delta() -> None:
    value = cutover_value(
        total_cost=D("145.27"),
        end_energy_kwh=D("4321.0"),
        reading_kwh=D("4322.4"),
        price_per_kwh=D("0.12"),
    )
    # The documented worked example: 145.27 + 1.4 x 0.12, exactly.
    assert value == D("145.438")


def test_cutover_unchanged_reading_is_the_import_total() -> None:
    value = cutover_value(
        total_cost=D("145.27"),
        end_energy_kwh=D("4321.0"),
        reading_kwh=D("4321.0"),
        price_per_kwh=D("0.12"),
    )
    assert value == D("145.27")


def test_cutover_reset_charges_the_full_reading_as_post_reset_consumption() -> None:
    # 50 < 90% of 4321: a reset — the reading IS consumption since the reset.
    value = cutover_value(
        total_cost=D("145.27"),
        end_energy_kwh=D("4321.0"),
        reading_kwh=D("50"),
        price_per_kwh=D("0.10"),
    )
    assert value == D("150.27")


def test_cutover_dip_is_not_a_calibration() -> None:
    """BINDING (issue #42 stress-test amendment 1): DIP maps to None, never a value.

    calibrate() re-baselines to the current reading, so calibrating a dipped
    reading to total_cost would charge the recovery leg back to the true
    meter level as ADVANCE — energy the import already costed.
    """
    # 4000 is between 90% of 4321 (3888.9) and 4321: a dip, not a reset.
    value = cutover_value(
        total_cost=D("145.27"),
        end_energy_kwh=D("4321.0"),
        reading_kwh=D("4000"),
        price_per_kwh=D("0.12"),
    )
    assert value is None


def test_cutover_dip_reset_boundary_uses_the_shared_predicate() -> None:
    # Exactly 90% of the end reading is still a dip (the reset predicate is
    # strictly-below), one step under it is a reset — _classify semantics.
    at_boundary = cutover_value(
        total_cost=D("10"),
        end_energy_kwh=D("100"),
        reading_kwh=D("90"),
        price_per_kwh=D("1"),
    )
    assert at_boundary is None
    below_boundary = cutover_value(
        total_cost=D("10"),
        end_energy_kwh=D("100"),
        reading_kwh=D("89.999"),
        price_per_kwh=D("1"),
    )
    assert below_boundary == D("99.999")


def test_cutover_negative_price_is_legal() -> None:
    value = cutover_value(
        total_cost=D("1.00"),
        end_energy_kwh=D("100"),
        reading_kwh=D("102"),
        price_per_kwh=D("-0.05"),
    )
    assert value == D("0.90")
