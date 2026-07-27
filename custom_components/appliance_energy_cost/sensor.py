"""Cost sensor platform: accrue money at the price in force at consumption time.

This platform file renders state and wires events; every business rule
lives in the pure domain core (``accrual.py``). One cost sensor per
appliance subentry, driven by state-change events from the appliance's
cumulative energy sensor and the entry's single price sensor.

Event-driven by design, not polling: ``DataUpdateCoordinator`` is the seam
for *polled* data (STACK.md §2, "Polling / data seam"); this platform
consumes push events exactly like core's ``utility_meter`` and
``derivative`` helpers do, so no coordinator applies here.

All money and energy values are ``Decimal`` end to end — parsing, domain
arithmetic, and restore round-trips never transit through ``float``.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from dataclasses import dataclass
from decimal import Decimal
from typing import NamedTuple, override

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorStateClass,
)
from homeassistant.const import (
    ATTR_UNIT_OF_MEASUREMENT,
    STATE_UNAVAILABLE,
    STATE_UNKNOWN,
)
from homeassistant.core import Event, EventStateChangedData, HomeAssistant, State, callback
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.helpers.event import async_track_state_change_event
from homeassistant.helpers.restore_state import ExtraStoredData, RestoreEntity

from . import ApplianceEnergyCostConfigEntry
from .accrual import (
    INITIAL_STATE,
    AccrualEvent,
    AccrualState,
    Transition,
    apply_energy,
    apply_price,
)
from .const import (
    ATTR_PRICE_GAP_ACTIVE,
    CONF_ENERGY_SENSOR,
    CONF_PRICE_SENSOR,
    SUBENTRY_TYPE_APPLIANCE,
)
from .models import ApplianceConfig, EntryRuntimeData, decode_appliance_config
from .units import (
    currency_matches,
    parse_energy_unit,
    parse_finite_decimal,
    parse_price_unit,
    to_kwh,
    to_price_per_kwh,
)

_LOGGER = logging.getLogger(__name__)

PARALLEL_UPDATES = 0


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ApplianceEnergyCostConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up one cost sensor per appliance subentry."""
    for subentry in entry.subentries.values():
        if subentry.subentry_type != SUBENTRY_TYPE_APPLIANCE:
            continue
        try:
            config = decode_appliance_config(subentry.title, subentry.data)
        except ValueError as err:
            # Fail visibly but never crash the entry over one structurally
            # damaged subentry: the remaining appliances must still cost.
            _LOGGER.error("Skipping appliance subentry %s: %s", subentry.subentry_id, err)
            continue
        async_add_entities(
            [ApplianceCostSensor(entry.runtime_data, subentry.subentry_id, config)],
            config_subentry_id=subentry.subentry_id,
        )


def _parse_energy_state(state: State) -> Decimal | None:
    """Parse a cumulative energy state into kWh via its own unit attribute.

    Parsed per event with no unit memory: a mid-life Wh→kWh unit change on
    the source stays correct because every reading converts through the
    unit it was reported with.
    """
    value = parse_finite_decimal(state.state)
    if value is None:
        return None
    unit_raw = state.attributes.get(ATTR_UNIT_OF_MEASUREMENT)
    unit = parse_energy_unit(unit_raw if isinstance(unit_raw, str) else None)
    if unit is None:
        return None
    return to_kwh(value, unit)


def _parse_energy_reading(state: State | None) -> Decimal | None:
    """Parse a possibly-absent energy state; ``None`` on anything unusable."""
    if state is None or state.state in (STATE_UNKNOWN, STATE_UNAVAILABLE):
        return None
    return _parse_energy_state(state)


class _ParsedPrice(NamedTuple):
    """A parsed price state: a price per kWh, or a gap with a named reason."""

    price_per_kwh: Decimal | None
    gap_reason: str | None


def _parse_price_state(state: State | None, currency: str) -> _ParsedPrice:
    """Parse a price state; any failure is a price gap with a named reason."""
    if state is None or state.state in (STATE_UNKNOWN, STATE_UNAVAILABLE):
        return _ParsedPrice(None, "the price sensor has no usable state")
    value = parse_finite_decimal(state.state)
    if value is None:
        return _ParsedPrice(None, f"price state {state.state!r} is not a finite number")
    unit_raw = state.attributes.get(ATTR_UNIT_OF_MEASUREMENT)
    unit = parse_price_unit(unit_raw if isinstance(unit_raw, str) else None)
    if unit is None:
        return _ParsedPrice(None, f"price unit {unit_raw!r} is not a supported per-energy unit")
    if not currency_matches(unit.numerator, currency):
        # Never accept a numerator drift as a price: a subunit like snt/kWh
        # silently accepted would make every cost figure 100x off.
        return _ParsedPrice(
            None,
            f"price unit numerator {unit.numerator!r} does not match currency"
            f" {currency!r}; prices are never rescaled",
        )
    return _ParsedPrice(to_price_per_kwh(value, unit.denominator), None)


@dataclass(frozen=True, slots=True)
class CostSensorExtraStoredData(ExtraStoredData):
    """Restore payload: cost, the baseline, and the baseline's energy sensor.

    Decimals round-trip as strings — zero float transit — because a float
    detour would drift the very cents this integration exists to get right.
    The energy sensor id pins WHICH meter the baseline belongs to: the
    unique_id survives a reconfigure that swaps the energy sensor, and a
    baseline replayed against a different meter would fabricate cost.
    """

    cost: Decimal
    last_energy_kwh: Decimal | None
    energy_sensor: str

    @override
    def as_dict(self) -> dict[str, str | None]:
        """Serialise for the restore-state store, Decimals as strings."""
        return {
            "cost": str(self.cost),
            "last_energy_kwh": (
                None if self.last_energy_kwh is None else str(self.last_energy_kwh)
            ),
            "energy_sensor": self.energy_sensor,
        }

    @classmethod
    def from_dict(cls, restored: Mapping[str, object]) -> CostSensorExtraStoredData | None:
        """Decode a stored dict, all-or-nothing.

        A missing key, a non-string value, an unparseable Decimal, or a
        non-finite value rejects the whole payload — a partially restored
        state would be worse than the documented fallback chain.
        """
        if (
            "cost" not in restored
            or "last_energy_kwh" not in restored
            or "energy_sensor" not in restored
        ):
            return None
        raw_cost = restored["cost"]
        if not isinstance(raw_cost, str):
            return None
        cost = parse_finite_decimal(raw_cost)
        if cost is None:
            return None
        raw_energy_sensor = restored["energy_sensor"]
        if not isinstance(raw_energy_sensor, str) or not raw_energy_sensor:
            return None
        raw_baseline = restored["last_energy_kwh"]
        if raw_baseline is None:
            return cls(cost=cost, last_energy_kwh=None, energy_sensor=raw_energy_sensor)
        if not isinstance(raw_baseline, str):
            return None
        baseline = parse_finite_decimal(raw_baseline)
        if baseline is None:
            return None
        return cls(cost=cost, last_energy_kwh=baseline, energy_sensor=raw_energy_sensor)


# RestoreSensor is deliberately NOT used: its async_get_last_sensor_data
# hard-codes SensorExtraStoredData.from_dict, which round-trips only
# native_value + unit and would silently drop the last-priced-energy
# baseline this sensor must persist to avoid double counting.
class ApplianceCostSensor(RestoreEntity, SensorEntity):
    """One appliance's cumulative money cost, priced at consumption time."""

    _attr_device_class = SensorDeviceClass.MONETARY
    # TOTAL is the only state class valid for MONETARY, and last_reset is
    # deliberately never set: negative prices legally decrease the total.
    _attr_state_class = SensorStateClass.TOTAL
    _attr_has_entity_name = True
    _attr_translation_key = "cost"
    _attr_should_poll = False
    _attr_suggested_display_precision = 2

    def __init__(
        self,
        runtime: EntryRuntimeData,
        subentry_id: str,
        config: ApplianceConfig,
    ) -> None:
        """Initialise the sensor from decoded entry and subentry config."""
        # BINDING (issue #4): the unique_id is the core-generated
        # subentry_id — immutable across rename and reconfigure — so
        # RestoreEntity state and long-term statistics survive both.
        self._attr_unique_id = subentry_id
        self._attr_translation_placeholders = {"appliance": config.name}
        self._attr_native_unit_of_measurement = runtime.currency
        self._currency = runtime.currency
        self._price_sensor = runtime.price_sensor
        self._energy_sensor = config.energy_sensor
        self._accrual = INITIAL_STATE
        # Edge guards — INVARIANT: every repeating degraded condition logs
        # on the transition into it only, never per event. Gap start/end are
        # edges by construction in the domain; these flags guard the rest.
        self._invalid_reading_logged = False
        self._unparseable_energy_logged = False

    @property
    @override
    def native_value(self) -> Decimal:
        """Full-precision cumulative cost.

        Core stringifies the Decimal exactly; ``suggested_display_precision``
        is presentation-only and never rounds the recorded state.
        """
        return self._accrual.cost

    @property
    @override
    def extra_state_attributes(self) -> dict[str, str | bool]:
        """Source entity ids and the price-gap flag.

        Deliberately nothing per-event (no baseline, no active price, no
        unpriced energy): attribute churn on every source event would defeat
        the recorder's shared-attribute deduplication.
        """
        return {
            CONF_ENERGY_SENSOR: self._energy_sensor,
            CONF_PRICE_SENSOR: self._price_sensor,
            ATTR_PRICE_GAP_ACTIVE: self._accrual.price_per_kwh is None,
        }

    @property
    @override
    def extra_restore_state_data(self) -> CostSensorExtraStoredData:
        """Persist the cost, the baseline, and the baseline's energy sensor."""
        return CostSensorExtraStoredData(
            cost=self._accrual.cost,
            last_energy_kwh=self._accrual.last_energy_kwh,
            energy_sensor=self._energy_sensor,
        )

    async def async_added_to_hass(self) -> None:
        """Restore state, take one initial reading, and subscribe to sources."""
        await super().async_added_to_hass()
        extra_data = await self.async_get_last_extra_data()
        last_state = await self.async_get_last_state()
        # From here to the end of the method the block is synchronous on the
        # event loop: no await may separate reading the current source states
        # from subscribing, or a source event landing in that window would be
        # missed or applied twice.
        self._accrual = self._restore_accrual_state(extra_data, last_state)
        parsed_price = _parse_price_state(self.hass.states.get(self._price_sensor), self._currency)
        energy_state = self.hass.states.get(self._energy_sensor)
        reading = _parse_energy_reading(energy_state)
        transition = apply_price(self._accrual, parsed_price.price_per_kwh, reading)
        self._apply_transition(
            transition,
            reading_supplied=reading is not None,
            gap_reason=parsed_price.gap_reason,
        )
        self.async_on_remove(
            async_track_state_change_event(
                self.hass, [self._energy_sensor], self._handle_energy_event
            )
        )
        self.async_on_remove(
            async_track_state_change_event(
                self.hass, [self._price_sensor], self._handle_price_event
            )
        )
        # Availability derives solely from the energy source; a price
        # failure is a gap, not unavailability (the settled cost is true).
        self._attr_available = energy_state is not None and energy_state.state != STATE_UNAVAILABLE
        if self._accrual.price_per_kwh is None:
            # The domain emits no event for a None→None price transition, so
            # a price source dead at startup gets its visibility here — once,
            # at setup, never per event.
            _LOGGER.warning(
                "%s has no usable price from %s at setup (%s):"
                " accrual holds until a usable price arrives",
                self.entity_id,
                self._price_sensor,
                parsed_price.gap_reason,
            )
        self.async_write_ha_state()

    def _restore_accrual_state(
        self, extra_data: ExtraStoredData | None, last_state: State | None
    ) -> AccrualState:
        """Rebuild the accrual state from the previous run (fallback chain).

        (1) Intact extra data restores cost and baseline — unless the
        baseline belongs to a different energy sensor (a reconfigure swap),
        in which case the cost is kept and the baseline dropped; (2)
        unusable extra data falls back to the last state string for the
        cost with the baseline dropped — the next reading re-baselines
        without double counting; (3) nothing usable restarts at 0 with the
        statistics consequence named. The restored price is always
        ``None``: the current price is re-read at setup.
        ``last_reading_kwh`` initialises to the restored baseline (it is
        deliberately not persisted).
        """
        if extra_data is not None:
            restored = CostSensorExtraStoredData.from_dict(extra_data.as_dict())
            if restored is not None:
                if restored.energy_sensor != self._energy_sensor:
                    # A reconfigure swapped the energy sensor: the baseline
                    # belongs to the old meter, and replaying it against the
                    # new meter's reading would fabricate cost (a bogus
                    # accrual on a higher reading, a bogus METER_RESET
                    # charge on a >10% lower one). Keep the cost, drop the
                    # baseline: the next reading re-baselines without
                    # charging (BASELINE_INITIALISED). Logged once, at setup.
                    _LOGGER.info(
                        "%s: energy sensor changed from %s to %s — cumulative cost %s"
                        " kept; the baseline re-initialises from the new sensor's"
                        " next reading without charging",
                        self.entity_id,
                        restored.energy_sensor,
                        self._energy_sensor,
                        restored.cost,
                    )
                    return AccrualState(
                        cost=restored.cost,
                        last_energy_kwh=None,
                        last_reading_kwh=None,
                        price_per_kwh=None,
                    )
                return AccrualState(
                    cost=restored.cost,
                    last_energy_kwh=restored.last_energy_kwh,
                    last_reading_kwh=restored.last_energy_kwh,
                    price_per_kwh=None,
                )
        if last_state is not None and (cost := parse_finite_decimal(last_state.state)) is not None:
            _LOGGER.warning(
                "%s: restore data unusable; cumulative cost %s restored from the last"
                " state but the last-priced-energy baseline was lost — the next"
                " reading re-baselines without charging",
                self.entity_id,
                cost,
            )
            return AccrualState(
                cost=cost,
                last_energy_kwh=None,
                last_reading_kwh=None,
                price_per_kwh=None,
            )
        if extra_data is not None or last_state is not None:
            _LOGGER.warning(
                "%s: restore data unusable and the last state was not numeric;"
                " cumulative cost restarts at 0 — long-term statistics will record"
                " a negative step",
                self.entity_id,
            )
        return INITIAL_STATE

    @callback
    def _handle_energy_event(self, event: Event[EventStateChangedData]) -> None:
        """Apply one energy state change through the domain core."""
        new_state = event.data["new_state"]
        if new_state is None or new_state.state == STATE_UNAVAILABLE:
            if self._attr_available:
                _LOGGER.info(
                    "%s: energy source %s became unavailable",
                    self.entity_id,
                    self._energy_sensor,
                )
                self._attr_available = False
                self.async_write_ha_state()
            return
        if not self._attr_available:
            _LOGGER.info("%s: energy source %s recovered", self.entity_id, self._energy_sensor)
            self._attr_available = True
        if new_state.state == STATE_UNKNOWN:
            # unknown does not flip availability (utility_meter precedent)
            # and carries nothing to settle.
            self._log_unparseable_energy_once(f"state is {STATE_UNKNOWN}")
            self.async_write_ha_state()
            return
        reading = _parse_energy_state(new_state)
        if reading is None:
            self._log_unparseable_energy_once(
                f"state {new_state.state!r} with unit"
                f" {new_state.attributes.get(ATTR_UNIT_OF_MEASUREMENT)!r}"
                " is not a usable cumulative energy reading"
            )
            self.async_write_ha_state()
            return
        self._unparseable_energy_logged = False
        transition = apply_energy(self._accrual, reading)
        self._apply_transition(transition, reading_supplied=True)
        self.async_write_ha_state()

    @callback
    def _handle_price_event(self, event: Event[EventStateChangedData]) -> None:
        """Apply one price change; settle-first at the old price is domain behaviour."""
        parsed = _parse_price_state(event.data["new_state"], self._currency)
        reading = _parse_energy_reading(self.hass.states.get(self._energy_sensor))
        transition = apply_price(self._accrual, parsed.price_per_kwh, reading)
        self._apply_transition(
            transition,
            reading_supplied=reading is not None,
            gap_reason=parsed.gap_reason,
        )
        self.async_write_ha_state()

    def _log_unparseable_energy_once(self, reason: str) -> None:
        """Warn on the transition into an unparseable-energy streak only."""
        if not self._unparseable_energy_logged:
            _LOGGER.warning(
                "%s: skipping energy update from %s: %s",
                self.entity_id,
                self._energy_sensor,
                reason,
            )
        self._unparseable_energy_logged = True

    @callback
    def _apply_transition(
        self,
        transition: Transition,
        *,
        reading_supplied: bool,
        gap_reason: str | None = None,
    ) -> None:
        """Adopt the next accrual state and map domain events to logs.

        GAP_STARTED fires once per live gap by construction in the domain;
        INVALID_READING is edge-guarded here per streak of supplied readings.
        """
        self._accrual = transition.state
        for domain_event in transition.events:
            match domain_event:
                case AccrualEvent.ACCRUED:
                    pass
                case AccrualEvent.BASELINE_INITIALISED:
                    _LOGGER.debug(
                        "%s: baseline initialised at %s kWh — pre-existing consumption"
                        " is never priced",
                        self.entity_id,
                        self._accrual.last_energy_kwh,
                    )
                case AccrualEvent.METER_RESET:
                    _LOGGER.warning(
                        "%s: meter reset detected on %s — the new reading is charged"
                        " as consumption since the reset; cost never decreases",
                        self.entity_id,
                        self._energy_sensor,
                    )
                case AccrualEvent.METER_DIP:
                    _LOGGER.debug(
                        "%s: small meter dip on %s — nothing charged, baseline held",
                        self.entity_id,
                        self._energy_sensor,
                    )
                case AccrualEvent.HELD_PRICE_GAP:
                    _LOGGER.debug(
                        "%s: energy tracked during a price gap — nothing charged yet",
                        self.entity_id,
                    )
                case AccrualEvent.GAP_STARTED:
                    _LOGGER.warning(
                        "%s: price gap started (%s) — accrual holds; energy metered"
                        " during the gap is priced at the price in force when the"
                        " price returns",
                        self.entity_id,
                        gap_reason if gap_reason is not None else "no usable price",
                    )
                case AccrualEvent.GAP_ENDED:
                    _LOGGER.info(
                        "%s: usable price in force — energy accumulated without a"
                        " price is settled at the returning price",
                        self.entity_id,
                    )
                case AccrualEvent.INVALID_READING:
                    if not self._invalid_reading_logged:
                        _LOGGER.warning(
                            "%s: negative cumulative reading from %s rejected —"
                            " nothing charged, baseline held",
                            self.entity_id,
                            self._energy_sensor,
                        )
                    self._invalid_reading_logged = True
                case AccrualEvent.CALIBRATED:
                    _LOGGER.debug("%s: calibrated", self.entity_id)
        if reading_supplied and AccrualEvent.INVALID_READING not in transition.events:
            self._invalid_reading_logged = False
