"""Tests for the config and subentry flows."""

from __future__ import annotations

from unittest.mock import patch

import pytest
from homeassistant.components.sensor import ATTR_STATE_CLASS
from homeassistant.config_entries import (
    SOURCE_USER,
    ConfigEntryState,
    ConfigSubentryData,
    FlowType,
)
from homeassistant.const import (
    ATTR_FRIENDLY_NAME,
    ATTR_UNIT_OF_MEASUREMENT,
    CONF_NAME,
)
from homeassistant.const import CONF_CURRENCY as HA_CONF_CURRENCY
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.appliance_energy_cost import async_setup_entry
from custom_components.appliance_energy_cost.const import (
    CONF_CURRENCY,
    CONF_ENERGY_SENSOR,
    CONF_PRICE_SENSOR,
    DOMAIN,
    SUBENTRY_TYPE_APPLIANCE,
)

PRICE_SENSOR = "sensor.electricity_price"
ENERGY_SENSOR = "sensor.heat_pump_energy"


def test_conf_currency_matches_the_core_key() -> None:
    """Pin our HA-free key literal (models.py purity) to core's CONF_CURRENCY."""
    assert CONF_CURRENCY == HA_CONF_CURRENCY


def _set_price_sensor(
    hass: HomeAssistant,
    entity_id: str = PRICE_SENSOR,
    *,
    state: str = "0.25",
    unit: str | None = "EUR/kWh",
    state_class: str | None = "measurement",
    name: str = "Electricity price",
) -> None:
    attributes: dict[str, str] = {ATTR_FRIENDLY_NAME: name}
    if unit is not None:
        attributes[ATTR_UNIT_OF_MEASUREMENT] = unit
    if state_class is not None:
        attributes[ATTR_STATE_CLASS] = state_class
    hass.states.async_set(entity_id, state, attributes)


def _set_energy_sensor(
    hass: HomeAssistant,
    entity_id: str = ENERGY_SENSOR,
    *,
    state: str = "1234.5",
    unit: str | None = "kWh",
    state_class: str | None = "total_increasing",
) -> None:
    attributes: dict[str, str] = {ATTR_FRIENDLY_NAME: "Heat pump energy"}
    if unit is not None:
        attributes[ATTR_UNIT_OF_MEASUREMENT] = unit
    if state_class is not None:
        attributes[ATTR_STATE_CLASS] = state_class
    hass.states.async_set(entity_id, state, attributes)


async def _submit_user_flow(
    hass: HomeAssistant,
    *,
    price_sensor: str = PRICE_SENSOR,
    currency: str = "EUR",
):
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": SOURCE_USER})
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "user"
    return await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {CONF_PRICE_SENSOR: price_sensor, CONF_CURRENCY: currency},
    )


async def _setup_entry(
    hass: HomeAssistant,
    *,
    price_sensor: str = PRICE_SENSOR,
    currency: str = "EUR",
    subentries_data: list[ConfigSubentryData] | None = None,
) -> MockConfigEntry:
    entry = MockConfigEntry(
        domain=DOMAIN,
        title="Electricity price",
        data={CONF_PRICE_SENSOR: price_sensor, CONF_CURRENCY: currency},
        subentries_data=subentries_data,
    )
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    return entry


async def _start_add_appliance(hass: HomeAssistant, entry: MockConfigEntry):
    return await hass.config_entries.subentries.async_init(
        (entry.entry_id, SUBENTRY_TYPE_APPLIANCE), context={"source": SOURCE_USER}
    )


async def _add_appliance(
    hass: HomeAssistant,
    entry: MockConfigEntry,
    *,
    name: str = "Heat pump",
    energy_sensor: str = ENERGY_SENSOR,
):
    result = await _start_add_appliance(hass, entry)
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "user"
    return await hass.config_entries.subentries.async_configure(
        result["flow_id"], {CONF_NAME: name, CONF_ENERGY_SENSOR: energy_sensor}
    )


async def test_user_flow_creates_entry_and_chains_into_appliance_flow(
    hass: HomeAssistant,
) -> None:
    _set_price_sensor(hass)
    _set_energy_sensor(hass)
    result = await _submit_user_flow(hass)
    assert result["type"] is FlowResultType.CREATE_ENTRY
    entry = result["result"]
    assert entry.title == "Electricity price"
    assert dict(entry.data) == {CONF_PRICE_SENSOR: PRICE_SENSOR, CONF_CURRENCY: "EUR"}
    assert dict(entry.options) == {}
    # The chained "Add appliance" subentry flow is already in progress.
    flow_type, flow_id = result["next_flow"]
    assert flow_type is FlowType.CONFIG_SUBENTRIES_FLOW
    progress = hass.config_entries.subentries.async_get(flow_id)
    assert progress["step_id"] == "user"
    sub_result = await hass.config_entries.subentries.async_configure(
        flow_id, {CONF_NAME: "Heat pump", CONF_ENERGY_SENSOR: ENERGY_SENSOR}
    )
    assert sub_result["type"] is FlowResultType.CREATE_ENTRY
    await hass.async_block_till_done()
    subentry = next(iter(entry.subentries.values()))
    assert subentry.subentry_type == SUBENTRY_TYPE_APPLIANCE
    assert subentry.title == "Heat pump"
    assert subentry.unique_id == ENERGY_SENSOR
    assert dict(subentry.data) == {CONF_ENERGY_SENSOR: ENERGY_SENSOR}
    assert entry.state is ConfigEntryState.LOADED


async def test_missing_price_sensor_entity_is_an_error(hass: HomeAssistant) -> None:
    result = await _submit_user_flow(hass, price_sensor="sensor.does_not_exist")
    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {CONF_PRICE_SENSOR: "price_sensor_unavailable"}


@pytest.mark.parametrize(
    ("state", "unit", "expected_error"),
    [
        ("unavailable", "EUR/kWh", "price_sensor_unavailable"),
        ("unknown", "EUR/kWh", "price_sensor_unavailable"),
        ("soon", "EUR/kWh", "price_not_numeric"),
        ("0.25", "EUR", "price_unit_unsupported"),
        ("0.25", "EUR/l", "price_unit_unsupported"),
        ("0.25", None, "price_unit_unsupported"),
    ],
)
async def test_user_flow_rejects_a_bad_price_sensor(
    hass: HomeAssistant, state: str, unit: str | None, expected_error: str
) -> None:
    _set_price_sensor(hass, state=state, unit=unit)
    result = await _submit_user_flow(hass)
    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {CONF_PRICE_SENSOR: expected_error}
    assert hass.config_entries.async_entries(DOMAIN) == []


async def test_subunit_price_is_rejected_with_remediation_and_recovers(
    hass: HomeAssistant,
) -> None:
    """Binding case: snt/kWh against EUR must fail — accepting it would be 100x off."""
    _set_price_sensor(hass, unit="snt/kWh")
    result = await _submit_user_flow(hass)
    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {CONF_PRICE_SENSOR: "currency_mismatch"}
    assert result["description_placeholders"] == {
        "numerator": "snt",
        "currency": "EUR",
    }
    # The flow recovers once the source is fixed.
    _set_price_sensor(hass, unit="EUR/kWh")
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_PRICE_SENSOR: PRICE_SENSOR, CONF_CURRENCY: "EUR"}
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY


async def test_duplicate_price_sensor_aborts(hass: HomeAssistant) -> None:
    _set_price_sensor(hass)
    first = await _submit_user_flow(hass)
    assert first["type"] is FlowResultType.CREATE_ENTRY
    await hass.async_block_till_done()
    result = await _submit_user_flow(hass)
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "already_configured"


async def test_non_measurement_price_sensor_warns_then_creates(
    hass: HomeAssistant,
) -> None:
    _set_price_sensor(hass, state_class="total")
    result = await _submit_user_flow(hass)
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "confirm_no_statistics"
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {})
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["result"].data[CONF_PRICE_SENSOR] == PRICE_SENSOR


async def test_abandoning_the_no_statistics_confirm_creates_nothing(
    hass: HomeAssistant,
) -> None:
    _set_price_sensor(hass, state_class=None)
    result = await _submit_user_flow(hass)
    assert result["step_id"] == "confirm_no_statistics"
    hass.config_entries.flow.async_abort(result["flow_id"])
    await hass.async_block_till_done()
    assert hass.config_entries.async_entries(DOMAIN) == []


async def test_aborted_appliance_chain_leaves_a_working_zero_appliance_entry(
    hass: HomeAssistant,
) -> None:
    """A zero-appliance entry is legal: the chained flow is optional."""
    _set_price_sensor(hass)
    result = await _submit_user_flow(hass)
    assert result["type"] is FlowResultType.CREATE_ENTRY
    entry = result["result"]
    _, flow_id = result["next_flow"]
    hass.config_entries.subentries.async_abort(flow_id)
    await hass.async_block_till_done()
    assert entry.subentries == {}
    assert entry.state is ConfigEntryState.LOADED


async def test_add_appliance_subentry(hass: HomeAssistant) -> None:
    _set_price_sensor(hass)
    _set_energy_sensor(hass)
    entry = await _setup_entry(hass)
    result = await _add_appliance(hass, entry)
    assert result["type"] is FlowResultType.CREATE_ENTRY
    await hass.async_block_till_done()
    subentry = next(iter(entry.subentries.values()))
    assert subentry.subentry_type == SUBENTRY_TYPE_APPLIANCE
    assert subentry.title == "Heat pump"
    assert subentry.unique_id == ENERGY_SENSOR
    assert dict(subentry.data) == {CONF_ENERGY_SENSOR: ENERGY_SENSOR}


@pytest.mark.parametrize(
    ("state", "unit", "state_class", "expected_error"),
    [
        ("unavailable", "kWh", "total_increasing", "energy_sensor_unavailable"),
        ("1234.5", "W", "measurement", "energy_unit_unsupported"),
        ("1234.5", None, "total_increasing", "energy_unit_unsupported"),
        # The measurement-class foot-gun: per-period/power-style kWh sensor.
        ("1234.5", "kWh", "measurement", "energy_not_cumulative"),
        ("1234.5", "kWh", None, "energy_not_cumulative"),
        ("12,5", "kWh", "total_increasing", "energy_not_numeric"),
    ],
)
async def test_add_appliance_rejects_a_bad_energy_sensor(
    hass: HomeAssistant,
    state: str,
    unit: str | None,
    state_class: str | None,
    expected_error: str,
) -> None:
    _set_price_sensor(hass)
    _set_energy_sensor(hass, state=state, unit=unit, state_class=state_class)
    entry = await _setup_entry(hass)
    result = await _add_appliance(hass, entry)
    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {CONF_ENERGY_SENSOR: expected_error}
    assert entry.subentries == {}


async def test_duplicate_energy_sensor_within_the_entry_names_the_other_appliance(
    hass: HomeAssistant,
) -> None:
    _set_price_sensor(hass)
    _set_energy_sensor(hass)
    entry = await _setup_entry(hass)
    result = await _add_appliance(hass, entry, name="Heat pump")
    assert result["type"] is FlowResultType.CREATE_ENTRY
    await hass.async_block_till_done()
    result = await _add_appliance(hass, entry, name="Sauna")
    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {CONF_ENERGY_SENSOR: "duplicate_energy_sensor"}
    assert result["description_placeholders"] == {"other_appliance": "Heat pump"}
    assert len(entry.subentries) == 1


async def test_core_unique_id_collision_aborts_as_backstop(
    hass: HomeAssistant,
) -> None:
    """A6: a collision the duplicate scan cannot see still aborts via core."""
    _set_price_sensor(hass)
    _set_energy_sensor(hass)
    # Degenerate subentry: unique_id taken but no energy_sensor key, so the
    # A5 data scan cannot match it — only core's unique_id backstop can.
    entry = await _setup_entry(
        hass,
        subentries_data=[
            ConfigSubentryData(
                data={},
                subentry_type=SUBENTRY_TYPE_APPLIANCE,
                title="Ghost",
                unique_id=ENERGY_SENSOR,
            )
        ],
    )
    result = await _add_appliance(hass, entry)
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "already_configured"


async def test_reconfigure_appliance_renames_and_swaps_the_sensor(
    hass: HomeAssistant,
) -> None:
    _set_price_sensor(hass)
    _set_energy_sensor(hass)
    _set_energy_sensor(hass, "sensor.sauna_energy")
    entry = await _setup_entry(hass)
    await _add_appliance(hass, entry)
    await hass.async_block_till_done()
    subentry_id = next(iter(entry.subentries))
    result = await entry.start_subentry_reconfigure_flow(hass, subentry_id)
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "reconfigure"
    result = await hass.config_entries.subentries.async_configure(
        result["flow_id"],
        {CONF_NAME: "Sauna", CONF_ENERGY_SENSOR: "sensor.sauna_energy"},
    )
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "reconfigure_successful"
    await hass.async_block_till_done()
    subentry = entry.subentries[subentry_id]
    assert subentry.title == "Sauna"
    assert subentry.unique_id == "sensor.sauna_energy"
    assert dict(subentry.data) == {CONF_ENERGY_SENSOR: "sensor.sauna_energy"}


async def test_reconfigure_appliance_excludes_itself_from_the_duplicate_scan(
    hass: HomeAssistant,
) -> None:
    _set_price_sensor(hass)
    _set_energy_sensor(hass)
    entry = await _setup_entry(hass)
    await _add_appliance(hass, entry)
    await hass.async_block_till_done()
    subentry_id = next(iter(entry.subentries))
    result = await entry.start_subentry_reconfigure_flow(hass, subentry_id)
    result = await hass.config_entries.subentries.async_configure(
        result["flow_id"],
        {CONF_NAME: "Renamed", CONF_ENERGY_SENSOR: ENERGY_SENSOR},
    )
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "reconfigure_successful"
    await hass.async_block_till_done()
    assert entry.subentries[subentry_id].title == "Renamed"


async def test_reconfigure_appliance_rejects_another_appliances_sensor(
    hass: HomeAssistant,
) -> None:
    _set_price_sensor(hass)
    _set_energy_sensor(hass)
    _set_energy_sensor(hass, "sensor.sauna_energy")
    entry = await _setup_entry(hass)
    await _add_appliance(hass, entry, name="Heat pump")
    await hass.async_block_till_done()
    await _add_appliance(hass, entry, name="Sauna", energy_sensor="sensor.sauna_energy")
    await hass.async_block_till_done()
    sauna_id = next(
        subentry_id
        for subentry_id, subentry in entry.subentries.items()
        if subentry.title == "Sauna"
    )
    result = await entry.start_subentry_reconfigure_flow(hass, sauna_id)
    result = await hass.config_entries.subentries.async_configure(
        result["flow_id"],
        {CONF_NAME: "Sauna", CONF_ENERGY_SENSOR: ENERGY_SENSOR},
    )
    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {CONF_ENERGY_SENSOR: "duplicate_energy_sensor"}
    assert result["description_placeholders"] == {"other_appliance": "Heat pump"}


async def test_every_change_reloads_the_entry_exactly_once(
    hass: HomeAssistant,
) -> None:
    """The entry update listener is the sole reload owner — never a double reload."""
    _set_price_sensor(hass)
    _set_price_sensor(hass, "sensor.new_price", name="New price")
    _set_energy_sensor(hass)
    entry = await _setup_entry(hass)
    with patch(
        "custom_components.appliance_energy_cost.async_setup_entry",
        wraps=async_setup_entry,
    ) as setup_mock:
        # Subentry add → exactly one reload.
        result = await _add_appliance(hass, entry)
        assert result["type"] is FlowResultType.CREATE_ENTRY
        await hass.async_block_till_done()
        assert setup_mock.call_count == 1
        # Subentry rename via reconfigure → exactly one reload.
        subentry_id = next(iter(entry.subentries))
        result = await entry.start_subentry_reconfigure_flow(hass, subentry_id)
        result = await hass.config_entries.subentries.async_configure(
            result["flow_id"],
            {CONF_NAME: "Renamed", CONF_ENERGY_SENSOR: ENERGY_SENSOR},
        )
        assert result["type"] is FlowResultType.ABORT
        await hass.async_block_till_done()
        assert setup_mock.call_count == 2
        # Main-entry reconfigure → exactly one reload.
        result = await entry.start_reconfigure_flow(hass)
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {CONF_PRICE_SENSOR: "sensor.new_price"}
        )
        assert result["type"] is FlowResultType.ABORT
        await hass.async_block_till_done()
        assert setup_mock.call_count == 3


async def test_reconfigure_changes_the_price_sensor_in_place(
    hass: HomeAssistant,
) -> None:
    _set_price_sensor(hass)
    _set_price_sensor(hass, "sensor.new_price", name="New price")
    entry = await _setup_entry(hass)
    result = await entry.start_reconfigure_flow(hass)
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "reconfigure"
    schema_keys = {str(key): key for key in result["data_schema"].schema}
    # The currency is immutable: it is not on the reconfigure form.
    assert CONF_CURRENCY not in schema_keys
    assert schema_keys[CONF_PRICE_SENSOR].description["suggested_value"] == PRICE_SENSOR
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_PRICE_SENSOR: "sensor.new_price"}
    )
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "reconfigure_successful"
    await hass.async_block_till_done()
    assert dict(entry.data) == {
        CONF_PRICE_SENSOR: "sensor.new_price",
        CONF_CURRENCY: "EUR",
    }
    # The title is left untouched — the user may have renamed the entry.
    assert entry.title == "Electricity price"
    assert entry.state is ConfigEntryState.LOADED


async def test_reconfigure_revalidates_the_price_matrix(hass: HomeAssistant) -> None:
    _set_price_sensor(hass)
    _set_price_sensor(hass, "sensor.subunit_price", unit="snt/kWh", name="Subunit")
    entry = await _setup_entry(hass)
    result = await entry.start_reconfigure_flow(hass)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_PRICE_SENSOR: "sensor.subunit_price"}
    )
    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {CONF_PRICE_SENSOR: "currency_mismatch"}
    assert entry.data[CONF_PRICE_SENSOR] == PRICE_SENSOR


async def test_reconfigure_to_a_non_measurement_price_sensor_warns_then_updates(
    hass: HomeAssistant,
) -> None:
    _set_price_sensor(hass)
    _set_price_sensor(hass, "sensor.total_price", state_class="total", name="Total")
    entry = await _setup_entry(hass)
    result = await entry.start_reconfigure_flow(hass)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_PRICE_SENSOR: "sensor.total_price"}
    )
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "confirm_no_statistics"
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {})
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "reconfigure_successful"
    await hass.async_block_till_done()
    assert entry.data[CONF_PRICE_SENSOR] == "sensor.total_price"


async def test_reconfigure_to_another_entries_price_sensor_aborts(
    hass: HomeAssistant,
) -> None:
    _set_price_sensor(hass)
    _set_price_sensor(hass, "sensor.other_price", name="Other price")
    await _setup_entry(hass)
    entry_b = await _setup_entry(hass, price_sensor="sensor.other_price")
    result = await entry_b.start_reconfigure_flow(hass)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_PRICE_SENSOR: PRICE_SENSOR}
    )
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "already_configured"
    assert entry_b.data[CONF_PRICE_SENSOR] == "sensor.other_price"


async def test_reconfigure_keeping_the_own_price_sensor_is_not_a_duplicate(
    hass: HomeAssistant,
) -> None:
    _set_price_sensor(hass)
    entry = await _setup_entry(hass)
    result = await entry.start_reconfigure_flow(hass)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_PRICE_SENSOR: PRICE_SENSOR}
    )
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "reconfigure_successful"
