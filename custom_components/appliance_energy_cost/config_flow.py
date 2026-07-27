"""Config and subentry flows for the Appliance Energy Cost integration.

The flow is the boundary against the foot-guns that would corrupt cost
figures: non-per-energy price units, currency/numerator mismatches (a
subunit price like snt/kWh would be silently 100x off), non-cumulative
energy sensors, and duplicate sources. All checks reuse the pure domain
parsers in ``units.py`` — no duplicate parsing logic lives here.
"""

from __future__ import annotations

from decimal import Decimal, InvalidOperation
from typing import NamedTuple, override

import voluptuous as vol
from homeassistant.components.sensor import (
    ATTR_STATE_CLASS,
    SensorDeviceClass,
    SensorStateClass,
)
from homeassistant.components.sensor import (
    DOMAIN as SENSOR_DOMAIN,
)
from homeassistant.config_entries import (
    SOURCE_RECONFIGURE,
    SOURCE_USER,
    ConfigFlow,
    ConfigFlowResult,
    ConfigSubentryFlow,
    FlowType,
    SubentryFlowContext,
    SubentryFlowResult,
)
from homeassistant.const import (
    ATTR_UNIT_OF_MEASUREMENT,
    CONF_NAME,
    STATE_UNAVAILABLE,
    STATE_UNKNOWN,
)
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.generated.currencies import ACTIVE_CURRENCIES
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers.selector import (
    EntityFilterSelectorConfig,
    EntitySelector,
    EntitySelectorConfig,
    SelectSelector,
    SelectSelectorConfig,
    SelectSelectorMode,
    TextSelector,
)

from . import ApplianceEnergyCostConfigEntry
from .const import (
    CONF_CURRENCY,
    CONF_ENERGY_SENSOR,
    CONF_PRICE_SENSOR,
    DOMAIN,
    SUBENTRY_TYPE_APPLIANCE,
)
from .units import currency_matches, parse_energy_unit, parse_price_unit

_PRICE_SENSOR_SELECTOR = EntitySelector(
    # Domain filter only: no state_class filter exists in selectors, and a
    # device_class filter would hide the template/spot-price sensors this
    # integration targets. All authoritative checks run post-selection.
    EntitySelectorConfig(filter=EntityFilterSelectorConfig(domain=SENSOR_DOMAIN))
)

_ENERGY_SENSOR_SELECTOR = EntitySelector(
    # The device_class filter is picker convenience only; the authoritative
    # unit and state_class checks run post-selection in _check_energy_sensor.
    EntitySelectorConfig(
        filter=EntityFilterSelectorConfig(
            domain=SENSOR_DOMAIN, device_class=SensorDeviceClass.ENERGY
        )
    )
)

_RECONFIGURE_SCHEMA = vol.Schema({vol.Required(CONF_PRICE_SENSOR): _PRICE_SENSOR_SELECTOR})

_APPLIANCE_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_NAME): TextSelector(),
        vol.Required(CONF_ENERGY_SENSOR): _ENERGY_SENSOR_SELECTOR,
    }
)


def _user_schema(hass: HomeAssistant) -> vol.Schema:
    """Build the user-step schema; the currency defaults to HA's configured one."""
    return vol.Schema(
        {
            vol.Required(CONF_PRICE_SENSOR): _PRICE_SENSOR_SELECTOR,
            vol.Required(CONF_CURRENCY, default=hass.config.currency): vol.All(
                SelectSelector(
                    SelectSelectorConfig(
                        options=sorted(ACTIVE_CURRENCIES),
                        mode=SelectSelectorMode.DROPDOWN,
                    )
                ),
                # The selector constrains the UI; this re-validates server-side.
                cv.currency,
            ),
        }
    )


class _PriceCheck(NamedTuple):
    """Outcome of the price-sensor validation matrix."""

    errors: dict[str, str]
    placeholders: dict[str, str]
    records_statistics: bool


def _check_price_sensor(hass: HomeAssistant, entity_id: str, currency: str) -> _PriceCheck:
    """Run the price-sensor validation matrix (P1-P4, P6).

    P1 available, P2 numeric, P3 supported per-energy unit, P4 numerator
    matches the configured currency. P6 (``state_class: measurement``,
    required for the backfill's hourly mean statistics) is not an error —
    it routes to an explicit warn-confirm step.
    """
    errors: dict[str, str] = {}
    placeholders: dict[str, str] = {}
    state = hass.states.get(entity_id)
    if state is None or state.state in (STATE_UNKNOWN, STATE_UNAVAILABLE):
        errors[CONF_PRICE_SENSOR] = "price_sensor_unavailable"
        return _PriceCheck(errors, placeholders, records_statistics=False)
    try:
        Decimal(state.state)
    except InvalidOperation:
        errors[CONF_PRICE_SENSOR] = "price_not_numeric"
        placeholders["state"] = state.state
        return _PriceCheck(errors, placeholders, records_statistics=False)
    unit_raw = state.attributes.get(ATTR_UNIT_OF_MEASUREMENT)
    price_unit = parse_price_unit(unit_raw if isinstance(unit_raw, str) else None)
    if price_unit is None:
        errors[CONF_PRICE_SENSOR] = "price_unit_unsupported"
        placeholders["unit"] = str(unit_raw or "")
        return _PriceCheck(errors, placeholders, records_statistics=False)
    if not currency_matches(price_unit.numerator, currency):
        errors[CONF_PRICE_SENSOR] = "currency_mismatch"
        placeholders["numerator"] = price_unit.numerator
        placeholders["currency"] = currency
        return _PriceCheck(errors, placeholders, records_statistics=False)
    records_statistics = state.attributes.get(ATTR_STATE_CLASS) == SensorStateClass.MEASUREMENT
    return _PriceCheck(errors, placeholders, records_statistics)


def _check_energy_sensor(
    hass: HomeAssistant, entity_id: str
) -> tuple[dict[str, str], dict[str, str]]:
    """Run the appliance energy-sensor validation matrix (A1-A4).

    A1 available, A2 supported energy unit, A3 cumulative state_class
    (``total`` or ``total_increasing`` — a power or per-period sensor would
    corrupt the figures), A4 numeric.
    """
    errors: dict[str, str] = {}
    placeholders: dict[str, str] = {}
    state = hass.states.get(entity_id)
    if state is None or state.state in (STATE_UNKNOWN, STATE_UNAVAILABLE):
        errors[CONF_ENERGY_SENSOR] = "energy_sensor_unavailable"
        return errors, placeholders
    unit_raw = state.attributes.get(ATTR_UNIT_OF_MEASUREMENT)
    if parse_energy_unit(unit_raw if isinstance(unit_raw, str) else None) is None:
        errors[CONF_ENERGY_SENSOR] = "energy_unit_unsupported"
        placeholders["unit"] = str(unit_raw or "")
        return errors, placeholders
    state_class = state.attributes.get(ATTR_STATE_CLASS)
    if state_class not in (SensorStateClass.TOTAL, SensorStateClass.TOTAL_INCREASING):
        errors[CONF_ENERGY_SENSOR] = "energy_not_cumulative"
        return errors, placeholders
    try:
        Decimal(state.state)
    except InvalidOperation:
        errors[CONF_ENERGY_SENSOR] = "energy_not_numeric"
        placeholders["state"] = state.state
    return errors, placeholders


def _duplicate_appliance_title(
    entry: ApplianceEnergyCostConfigEntry,
    energy_sensor: str,
    exclude_subentry_id: str | None,
) -> str | None:
    """Return the title of another appliance already using this energy sensor.

    Duplicates are checked within one entry only; the same energy sensor
    under two different price-sensor entries is allowed (documented v1
    policy, revisit tracked in issue #14).
    """
    for subentry in entry.subentries.values():
        if subentry.subentry_type != SUBENTRY_TYPE_APPLIANCE:
            continue
        if subentry.subentry_id == exclude_subentry_id:
            continue
        if subentry.data.get(CONF_ENERGY_SENSOR) == energy_sensor:
            return subentry.title
    return None


class ApplianceEnergyCostConfigFlow(ConfigFlow, domain=DOMAIN):
    """Pair one all-inclusive price sensor with a currency.

    One config entry per price sensor; appliances are config subentries.
    The currency is immutable after creation — a mixed-currency cumulative
    cost series would corrupt statistics — so the reconfigure step edits
    the price sensor only.
    """

    VERSION = 1

    def __init__(self) -> None:
        """Initialise the pending validated selection."""
        self._pending_price_sensor: str | None = None
        self._pending_currency: str | None = None

    @classmethod
    @callback
    @override
    def async_get_supported_subentry_types(
        cls, config_entry: ApplianceEnergyCostConfigEntry
    ) -> dict[str, type[ConfigSubentryFlow]]:
        """Declare the appliance subentry flow."""
        return {SUBENTRY_TYPE_APPLIANCE: ApplianceSubentryFlowHandler}

    async def async_step_user(self, user_input: dict[str, str] | None = None) -> ConfigFlowResult:
        """Select the price sensor and the currency."""
        errors: dict[str, str] = {}
        placeholders: dict[str, str] = {}
        if user_input is not None:
            price_sensor = user_input[CONF_PRICE_SENSOR]
            currency = user_input[CONF_CURRENCY]
            check = _check_price_sensor(self.hass, price_sensor, currency)
            errors, placeholders = check.errors, check.placeholders
            if not errors:
                self._async_abort_entries_match({CONF_PRICE_SENSOR: price_sensor})
                self._pending_price_sensor = price_sensor
                self._pending_currency = currency
                if not check.records_statistics:
                    return await self.async_step_confirm_no_statistics()
                return self._create_pending_entry()
        return self.async_show_form(
            step_id="user",
            data_schema=self.add_suggested_values_to_schema(_user_schema(self.hass), user_input),
            errors=errors,
            description_placeholders=placeholders,
        )

    async def async_step_confirm_no_statistics(
        self, user_input: dict[str, str] | None = None
    ) -> ConfigFlowResult:
        """Warn that the backfill needs ``state_class: measurement``.

        Live costing works from the sensor's current state regardless; the
        historical backfill reads the price sensor's hourly mean long-term
        statistics, which Home Assistant records only for
        ``state_class: measurement`` sensors. Explicit confirm required —
        fail visibly, degrade explicitly.
        """
        price_sensor = self._pending_price_sensor
        if price_sensor is None or self._pending_currency is None:
            # Only reachable from a validated user/reconfigure submission.
            raise HomeAssistantError("confirm_no_statistics entered without pending data")
        if user_input is not None:
            if self.source == SOURCE_RECONFIGURE:
                return self._finish_reconfigure()
            return self._create_pending_entry()
        state = self.hass.states.get(price_sensor)
        return self.async_show_form(
            step_id="confirm_no_statistics",
            data_schema=vol.Schema({}),
            description_placeholders={"price_sensor": state.name if state else price_sensor},
        )

    async def async_step_reconfigure(
        self, user_input: dict[str, str] | None = None
    ) -> ConfigFlowResult:
        """Change the price sensor. The currency is immutable by design.

        Changing the currency of an existing cumulative cost series would
        mix currencies within one statistic — the documented remediation is
        to remove the entry and add it again.
        """
        entry = self._get_reconfigure_entry()
        errors: dict[str, str] = {}
        placeholders: dict[str, str] = {}
        if user_input is not None:
            price_sensor = user_input[CONF_PRICE_SENSOR]
            currency = entry.data[CONF_CURRENCY]
            check = _check_price_sensor(self.hass, price_sensor, currency)
            errors, placeholders = check.errors, check.placeholders
            if not errors:
                # Self-excluding: entries-match skips the entry under reconfigure.
                self._async_abort_entries_match({CONF_PRICE_SENSOR: price_sensor})
                self._pending_price_sensor = price_sensor
                self._pending_currency = currency
                if not check.records_statistics:
                    return await self.async_step_confirm_no_statistics()
                return self._finish_reconfigure()
        suggested = user_input or {CONF_PRICE_SENSOR: entry.data[CONF_PRICE_SENSOR]}
        return self.async_show_form(
            step_id="reconfigure",
            data_schema=self.add_suggested_values_to_schema(_RECONFIGURE_SCHEMA, suggested),
            errors=errors,
            description_placeholders=placeholders,
        )

    @callback
    def _create_pending_entry(self) -> ConfigFlowResult:
        """Create the entry from the validated pending selection."""
        price_sensor = self._pending_price_sensor
        currency = self._pending_currency
        if price_sensor is None or currency is None:
            raise HomeAssistantError("entry creation requested without pending data")
        state = self.hass.states.get(price_sensor)
        return self.async_create_entry(
            title=state.name if state else price_sensor,
            data={CONF_PRICE_SENSOR: price_sensor, CONF_CURRENCY: currency},
        )

    @callback
    def _finish_reconfigure(self) -> ConfigFlowResult:
        """Update the entry; the entry update listener owns the reload.

        Deliberate divergence from the docs' ``async_update_reload_and_abort``
        example: this integration registers an entry update listener as the
        sole reload mechanism (it must also cover subentry changes), so the
        flow uses the non-reloading variant to avoid a double reload. The
        entry title is left untouched — the user may have renamed it.
        """
        price_sensor = self._pending_price_sensor
        if price_sensor is None:
            raise HomeAssistantError("reconfigure finish requested without pending data")
        return self.async_update_and_abort(
            self._get_reconfigure_entry(),
            data_updates={CONF_PRICE_SENSOR: price_sensor},
        )

    @override
    async def async_on_create_entry(self, result: ConfigFlowResult) -> ConfigFlowResult:
        """Chain straight into the first "Add appliance" subentry flow.

        An entry with zero appliances is legal — the user can abort the
        chained flow; the manual "Add appliance" button on the entry page
        is the fallback surface.
        """
        subentry_flow = await self.hass.config_entries.subentries.async_init(
            (result["result"].entry_id, SUBENTRY_TYPE_APPLIANCE),
            context=SubentryFlowContext(source=SOURCE_USER),
        )
        result["next_flow"] = (
            FlowType.CONFIG_SUBENTRIES_FLOW,
            subentry_flow["flow_id"],
        )
        return result


class ApplianceSubentryFlowHandler(ConfigSubentryFlow):
    """Add or reconfigure one appliance (display name + cumulative energy sensor)."""

    async def async_step_user(self, user_input: dict[str, str] | None = None) -> SubentryFlowResult:
        """Add an appliance."""
        errors: dict[str, str] = {}
        placeholders: dict[str, str] = {}
        if user_input is not None:
            energy_sensor = user_input[CONF_ENERGY_SENSOR]
            errors, placeholders = _check_energy_sensor(self.hass, energy_sensor)
            if not errors and (
                duplicate := _duplicate_appliance_title(
                    self._get_entry(), energy_sensor, exclude_subentry_id=None
                )
            ):
                errors[CONF_ENERGY_SENSOR] = "duplicate_energy_sensor"
                placeholders["other_appliance"] = duplicate
            if not errors:
                # Core's per-entry subentry unique_id uniqueness is the
                # backstop: a collision this scan missed aborts the flow.
                return self.async_create_entry(
                    title=user_input[CONF_NAME],
                    data={CONF_ENERGY_SENSOR: energy_sensor},
                    unique_id=energy_sensor,
                )
        return self.async_show_form(
            step_id="user",
            data_schema=self.add_suggested_values_to_schema(_APPLIANCE_SCHEMA, user_input),
            errors=errors,
            description_placeholders=placeholders,
        )

    async def async_step_reconfigure(
        self, user_input: dict[str, str] | None = None
    ) -> SubentryFlowResult:
        """Rename an appliance or swap its energy sensor."""
        entry = self._get_entry()
        subentry = self._get_reconfigure_subentry()
        errors: dict[str, str] = {}
        placeholders: dict[str, str] = {}
        if user_input is not None:
            energy_sensor = user_input[CONF_ENERGY_SENSOR]
            errors, placeholders = _check_energy_sensor(self.hass, energy_sensor)
            if not errors and (
                duplicate := _duplicate_appliance_title(
                    entry, energy_sensor, exclude_subentry_id=subentry.subentry_id
                )
            ):
                errors[CONF_ENERGY_SENSOR] = "duplicate_energy_sensor"
                placeholders["other_appliance"] = duplicate
            if not errors:
                # Non-reloading variant: the entry update listener owns the
                # reload (core forbids the reloading variant with listeners).
                return self.async_update_and_abort(
                    entry,
                    subentry,
                    title=user_input[CONF_NAME],
                    data={CONF_ENERGY_SENSOR: energy_sensor},
                    unique_id=energy_sensor,
                )
        suggested = user_input or {
            CONF_NAME: subentry.title,
            CONF_ENERGY_SENSOR: subentry.data[CONF_ENERGY_SENSOR],
        }
        return self.async_show_form(
            step_id="reconfigure",
            data_schema=self.add_suggested_values_to_schema(_APPLIANCE_SCHEMA, suggested),
            errors=errors,
            description_placeholders=placeholders,
        )
