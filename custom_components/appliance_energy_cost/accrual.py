"""Live cost accrual state machine: price at the moment of consumption.

Pure domain module: stdlib only, no Home Assistant imports, no I/O, no
clock access — every instant and reading is an input. All money and
energy arithmetic uses ``Decimal`` to avoid float drift on long-running
cumulative sums. Display rounding is deliberately absent; presentation
precision belongs to the entity layer.

Binding semantics come from issue #2 and its 2026-07-27 amendment: a
decrease of a ``total_increasing`` source is a *reset* only when the new
reading drops below 90 % of the last raw reading (Home Assistant core's
``reset_detected`` predicate); a smaller decrease is a *dip* that charges
nothing and holds the priced baseline at its high-water mark.

A negative cumulative reading is out of contract and fails closed with
``INVALID_READING``: it never settles, never becomes a baseline, and never
enters the reset predicate (mirrors ``backfill``'s invalid-energy rule).
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from decimal import Decimal
from enum import Enum, StrEnum, auto
from typing import Final, NamedTuple


@dataclass(frozen=True, slots=True)
class AccrualState:
    """One appliance's accrual state.

    ``last_energy_kwh`` is the priced baseline (high-water mark): energy up
    to it has been settled. ``last_reading_kwh`` is the last raw meter
    reading, used only for reset/dip classification; it is not persisted —
    after a restore it initialises to the restored baseline. ``None``
    baseline means awaiting the first reading; ``None`` price means a
    price gap is active.
    """

    cost: Decimal
    last_energy_kwh: Decimal | None
    last_reading_kwh: Decimal | None
    price_per_kwh: Decimal | None


INITIAL_STATE: Final = AccrualState(
    cost=Decimal("0"),
    last_energy_kwh=None,
    last_reading_kwh=None,
    price_per_kwh=None,
)


class AccrualEvent(StrEnum):
    """What a transition did, for visibility at the caller (never silent)."""

    BASELINE_INITIALISED = "baseline_initialised"
    ACCRUED = "accrued"
    METER_RESET = "meter_reset"
    METER_DIP = "meter_dip"
    HELD_PRICE_GAP = "held_price_gap"
    GAP_STARTED = "gap_started"
    GAP_ENDED = "gap_ended"
    CALIBRATED = "calibrated"
    INVALID_READING = "invalid_reading"


class Transition(NamedTuple):
    """A transition result: the next state and what happened (``()`` = nothing)."""

    state: AccrualState
    events: tuple[AccrualEvent, ...]


# Home Assistant core's total_increasing reset predicate
# (sensor/recorder.py::reset_detected): a reset is a drop below 90 % of the
# previous reading; a smaller decrease is a warned dip.
_RESET_FRACTION: Final = Decimal("0.9")


class _ReadingClass(Enum):
    """Classification of a new raw reading against the last raw reading."""

    RESET = auto()
    DIP = auto()
    UNCHANGED = auto()
    ADVANCE = auto()


def _classify(last_reading_kwh: Decimal, reading_kwh: Decimal) -> _ReadingClass:
    """The single reset/dip predicate, shared by every consumer.

    The issue #2 amendment mandates one shared classify helper so a reset
    during a price gap is classified identically to one outside it.
    """
    if reading_kwh < last_reading_kwh:
        if reading_kwh < _RESET_FRACTION * last_reading_kwh:
            return _ReadingClass.RESET
        return _ReadingClass.DIP
    if reading_kwh == last_reading_kwh:
        return _ReadingClass.UNCHANGED
    return _ReadingClass.ADVANCE


class _Settlement(NamedTuple):
    cost: Decimal
    baseline_kwh: Decimal
    last_reading_kwh: Decimal
    events: tuple[AccrualEvent, ...]


def _settle(
    cost: Decimal,
    baseline_kwh: Decimal | None,
    last_reading_kwh: Decimal | None,
    reading_kwh: Decimal,
    price_per_kwh: Decimal,
) -> _Settlement:
    """Classify one reading (initialise / accrue / reset / dip) and settle it.

    Every settlement path — live energy updates, price-change settlement,
    and gap-end settlement in both its variants — goes through this single
    helper so a meter reset or dip is classified identically everywhere.
    """
    if baseline_kwh is None or last_reading_kwh is None:
        # First reading: baseline to it so pre-existing consumption is
        # never priced at today's price.
        return _Settlement(cost, reading_kwh, reading_kwh, (AccrualEvent.BASELINE_INITIALISED,))
    match _classify(last_reading_kwh, reading_kwh):
        case _ReadingClass.RESET:
            # Reset: the new reading is consumption since the reset
            # (issue #2, as amended by the 2026-07-27 comment).
            return _Settlement(
                cost + reading_kwh * price_per_kwh,
                reading_kwh,
                reading_kwh,
                (AccrualEvent.METER_RESET,),
            )
        case _ReadingClass.DIP:
            # Dip (decrease within 10 %): charge nothing and hold the priced
            # baseline at its high-water mark so the recovery leg is not
            # double-charged and float-noise oscillation fabricates zero cost.
            return _Settlement(cost, baseline_kwh, reading_kwh, (AccrualEvent.METER_DIP,))
        case _ReadingClass.UNCHANGED | _ReadingClass.ADVANCE:
            delta = reading_kwh - baseline_kwh
            if delta <= 0:
                # At or below the held high-water mark (post-dip recovery, or
                # an unchanged reading): charge nothing, track the reading.
                return _Settlement(cost, baseline_kwh, reading_kwh, ())
            return _Settlement(
                cost + delta * price_per_kwh,
                reading_kwh,
                reading_kwh,
                (AccrualEvent.ACCRUED,),
            )


def _track_gap_energy(
    state: AccrualState,
    last_reading_kwh: Decimal,
    reading_kwh: Decimal,
) -> Transition:
    """Track meter movement during a price gap without charging anything.

    The priced baseline is held so the whole gap delta is settled when a
    price returns (v1 returning-price policy, see ``_end_gap``). The reading
    is classified by the shared ``_classify`` helper so a reset during a gap
    is classified identically to one outside it.
    """
    match _classify(last_reading_kwh, reading_kwh):
        case _ReadingClass.RESET:
            # A reset destroys the meter reference for any unsettled
            # pre-reset gap energy: that energy is dropped (undercharge,
            # never fabricate), while re-baselining to zero makes the full
            # post-reset reading — consumption since the reset — priced
            # when the price returns.
            return Transition(
                replace(state, last_energy_kwh=Decimal("0"), last_reading_kwh=reading_kwh),
                (AccrualEvent.METER_RESET,),
            )
        case _ReadingClass.DIP:
            return Transition(
                replace(state, last_reading_kwh=reading_kwh),
                (AccrualEvent.METER_DIP,),
            )
        case _ReadingClass.UNCHANGED:
            return Transition(state, ())
        case _ReadingClass.ADVANCE:
            return Transition(
                replace(state, last_reading_kwh=reading_kwh),
                (AccrualEvent.HELD_PRICE_GAP,),
            )


def apply_energy(state: AccrualState, energy_kwh: Decimal) -> Transition:
    """Apply a new cumulative energy reading (kWh) to the state.

    Without a baseline the reading only initialises it; during a price gap
    the reading is tracked but nothing is charged; otherwise the reading is
    settled at the active price. A negative reading is out of contract for
    a cumulative meter and is rejected visibly with ``INVALID_READING``.
    """
    if energy_kwh < 0:
        # Fail closed (mirrors backfill's invalid_energy_hours): charging a
        # negative reading would decrease cost, and letting it become the
        # baseline or the raw reading would fabricate cost or corrupt the
        # reset predicate later.
        return Transition(state, (AccrualEvent.INVALID_READING,))
    if state.last_energy_kwh is None or state.last_reading_kwh is None:
        return Transition(
            replace(state, last_energy_kwh=energy_kwh, last_reading_kwh=energy_kwh),
            (AccrualEvent.BASELINE_INITIALISED,),
        )
    if state.price_per_kwh is None:
        return _track_gap_energy(state, state.last_reading_kwh, energy_kwh)
    settled = _settle(
        state.cost,
        state.last_energy_kwh,
        state.last_reading_kwh,
        energy_kwh,
        state.price_per_kwh,
    )
    return Transition(
        AccrualState(
            cost=settled.cost,
            last_energy_kwh=settled.baseline_kwh,
            last_reading_kwh=settled.last_reading_kwh,
            price_per_kwh=state.price_per_kwh,
        ),
        settled.events,
    )


def _end_gap(
    state: AccrualState,
    new_price_per_kwh: Decimal,
    current_energy_kwh: Decimal | None,
) -> Transition:
    """End a price gap at the returning price.

    v1 policy (deliberate trade-off recorded in VISION.md → Open
    Questions): energy accumulated while the price was unknown is priced
    at the price in force when the price returns.
    """
    if current_energy_kwh is None:
        # Without a reading the held baseline realises the same policy
        # later: the next energy event prices the whole gap delta at the
        # returning price.
        return Transition(
            replace(state, price_per_kwh=new_price_per_kwh),
            (AccrualEvent.GAP_ENDED,),
        )
    settled = _settle(
        state.cost,
        state.last_energy_kwh,
        state.last_reading_kwh,
        current_energy_kwh,
        new_price_per_kwh,
    )
    return Transition(
        AccrualState(
            cost=settled.cost,
            last_energy_kwh=settled.baseline_kwh,
            last_reading_kwh=settled.last_reading_kwh,
            price_per_kwh=new_price_per_kwh,
        ),
        (AccrualEvent.GAP_ENDED, *settled.events),
    )


def apply_price(
    state: AccrualState,
    new_price_per_kwh: Decimal | None,
    current_energy_kwh: Decimal | None,
) -> Transition:
    """Apply a price change, settling pending energy at the outgoing price first.

    Settle-first is binding: energy accumulated during the old price period
    is priced at the old price before the new price takes effect, so a
    slow-updating energy sensor never shifts consumption onto the next
    hour's price. ``new_price_per_kwh=None`` starts a price gap;
    a price arriving while one is active ends it (see ``_end_gap``).
    A negative reading is discarded visibly (``INVALID_READING``) and the
    transition falls back to the documented no-reading degraded path.
    """
    reading = current_energy_kwh
    invalid_events: tuple[AccrualEvent, ...] = ()
    if reading is not None and reading < 0:
        # Fail closed (mirrors backfill's invalid_energy_hours): a negative
        # cumulative reading must never settle, become a baseline, or enter
        # the reset predicate.
        reading = None
        invalid_events = (AccrualEvent.INVALID_READING,)
    old_price = state.price_per_kwh
    if old_price is None:
        if new_price_per_kwh is None:
            if reading is None:
                return Transition(state, invalid_events)
            # A still-unavailable price event can carry a reading; consume
            # it exactly like an energy event during the gap so the raw
            # reading never goes stale for reset/dip classification.
            return apply_energy(state, reading)
        ended = _end_gap(state, new_price_per_kwh, reading)
        return Transition(ended.state, (*invalid_events, *ended.events))
    if reading is None:
        # Degraded but documented: with no (valid) reading at switch time
        # there is nothing to settle here — the old-price share cannot be
        # measured, so energy accumulated since the last event is
        # unavoidably priced at the NEW price at the next energy event.
        next_state = replace(state, price_per_kwh=new_price_per_kwh)
        if new_price_per_kwh is None:
            return Transition(next_state, (*invalid_events, AccrualEvent.GAP_STARTED))
        return Transition(next_state, invalid_events)
    settled = _settle(
        state.cost,
        state.last_energy_kwh,
        state.last_reading_kwh,
        reading,
        old_price,
    )
    next_state = AccrualState(
        cost=settled.cost,
        last_energy_kwh=settled.baseline_kwh,
        last_reading_kwh=settled.last_reading_kwh,
        price_per_kwh=new_price_per_kwh,
    )
    if new_price_per_kwh is None:
        return Transition(next_state, (*settled.events, AccrualEvent.GAP_STARTED))
    return Transition(next_state, settled.events)


def calibrate(
    state: AccrualState,
    cost: Decimal,
    current_energy_kwh: Decimal | None,
) -> Transition:
    """Set the cumulative cost and re-baseline to the given reading.

    Reset-to-zero is ``calibrate(state, Decimal("0"), reading)``; there is
    no separate reset function. A ``None`` reading returns the state to
    awaiting-first-reading. A negative reading rejects the whole
    calibration (``INVALID_READING``): it must never become a baseline,
    and a partial calibration would be worse than none.
    """
    if current_energy_kwh is not None and current_energy_kwh < 0:
        return Transition(state, (AccrualEvent.INVALID_READING,))
    return Transition(
        AccrualState(
            cost=cost,
            last_energy_kwh=current_energy_kwh,
            last_reading_kwh=current_energy_kwh,
            price_per_kwh=state.price_per_kwh,
        ),
        (AccrualEvent.CALIBRATED,),
    )
