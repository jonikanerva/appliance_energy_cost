"""Tests for source registry tracking and device attachment (issue #28).

Covers the entity-registry listener in ``__init__.py`` (rename follow,
removal, device move, the binding ignore rule, the unique_id collision
path) and the device-link + naming behaviour of the cost sensor.
"""

from __future__ import annotations

import logging
from decimal import Decimal
from unittest.mock import patch

import pytest
from homeassistant.components.sensor import ATTR_STATE_CLASS
from homeassistant.config_entries import ConfigEntryState, ConfigSubentryData
from homeassistant.const import (
    ATTR_FRIENDLY_NAME,
    ATTR_UNIT_OF_MEASUREMENT,
    STATE_UNAVAILABLE,
)
from homeassistant.core import HomeAssistant, State
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    mock_restore_cache_with_extra_data,
)

from custom_components.appliance_energy_cost.const import (
    CONF_CURRENCY,
    CONF_ENERGY_SENSOR,
    CONF_PRICE_SENSOR,
    DOMAIN,
    SUBENTRY_TYPE_APPLIANCE,
)

PRICE_SENSOR = "sensor.electricity_price"
ENERGY_SENSOR = "sensor.heat_pump_energy"
RENAMED_ENERGY_SENSOR = "sensor.garage_heat_pump_energy"
RENAMED_PRICE_SENSOR = "sensor.spot_price"
COST_ENTITY = "sensor.heat_pump_cost"


def _price_attrs() -> dict[str, str]:
    return {
        ATTR_FRIENDLY_NAME: "Electricity price",
        ATTR_UNIT_OF_MEASUREMENT: "EUR/kWh",
    }


def _energy_attrs() -> dict[str, str]:
    return {
        ATTR_FRIENDLY_NAME: "Heat pump energy",
        ATTR_STATE_CLASS: "total_increasing",
        ATTR_UNIT_OF_MEASUREMENT: "kWh",
    }


def _make_entry(
    hass: HomeAssistant,
    *,
    energy_sensor: str = ENERGY_SENSOR,
    title: str = "Heat pump",
) -> MockConfigEntry:
    entry = MockConfigEntry(
        domain=DOMAIN,
        title="Electricity price",
        data={CONF_PRICE_SENSOR: PRICE_SENSOR, CONF_CURRENCY: "EUR"},
        subentries_data=[
            ConfigSubentryData(
                data={CONF_ENERGY_SENSOR: energy_sensor},
                subentry_type=SUBENTRY_TYPE_APPLIANCE,
                title=title,
                unique_id=energy_sensor,
            )
        ],
    )
    entry.add_to_hass(hass)
    return entry


async def _setup(hass: HomeAssistant, entry: MockConfigEntry) -> None:
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()


@pytest.fixture
def source_entry(hass: HomeAssistant) -> MockConfigEntry:
    """A foreign config entry owning the mock source devices and entities."""
    entry = MockConfigEntry(domain="test")
    entry.add_to_hass(hass)
    return entry


def _register_source(
    hass: HomeAssistant,
    source_entry: MockConfigEntry,
    *,
    object_id: str,
    unique_id: str,
    device_id: str | None = None,
) -> er.RegistryEntry:
    """Register a mock source entity, optionally attached to a device."""
    return er.async_get(hass).async_get_or_create(
        "sensor",
        "test",
        unique_id,
        suggested_object_id=object_id,
        config_entry=source_entry,
        device_id=device_id,
    )


def _create_device(
    hass: HomeAssistant,
    source_entry: MockConfigEntry,
    *,
    identifier: str,
    name: str,
) -> dr.DeviceEntry:
    return dr.async_get(hass).async_get_or_create(
        config_entry_id=source_entry.entry_id,
        identifiers={("test", identifier)},
        name=name,
    )


def _integration_records(caplog: pytest.LogCaptureFixture, level: int) -> list[logging.LogRecord]:
    return [
        record
        for record in caplog.records
        if record.levelno == level
        and record.name.startswith("custom_components.appliance_energy_cost")
    ]


def _cost(hass: HomeAssistant, entity_id: str = COST_ENTITY) -> Decimal:
    state = hass.states.get(entity_id)
    assert state is not None
    return Decimal(state.state)


async def test_energy_source_rename_follows_with_cost_and_baseline_intact(
    hass: HomeAssistant, source_entry: MockConfigEntry
) -> None:
    """MONEY-CRITICAL: a source rename keeps cost AND baseline to the cent.

    The subentry data and unique_id follow the rename, exactly one reload
    happens, and the post-rename delta charges from the preserved baseline —
    no uncharged interval, no re-baseline.
    """
    _register_source(hass, source_entry, object_id="heat_pump_energy", unique_id="meter-a")
    hass.states.async_set(PRICE_SENSOR, "0.50", _price_attrs())
    hass.states.async_set(ENERGY_SENSOR, "100.0", _energy_attrs())
    entry = _make_entry(hass)
    await _setup(hass, entry)
    hass.states.async_set(ENERGY_SENSOR, "102.0", _energy_attrs())
    await hass.async_block_till_done()
    assert _cost(hass) == Decimal("1.00")

    with patch.object(
        hass.config_entries, "async_reload", wraps=hass.config_entries.async_reload
    ) as reload_spy:
        # A real platform rename moves the state with the registry entry;
        # 1 kWh is consumed during the rename window (102.0 → 103.0). Only
        # a PRESERVED baseline charges it — a dropped baseline would
        # silently re-baseline at 103.0 and never charge that kWh.
        hass.states.async_remove(ENERGY_SENSOR)
        er.async_get(hass).async_update_entity(ENERGY_SENSOR, new_entity_id=RENAMED_ENERGY_SENSOR)
        hass.states.async_set(RENAMED_ENERGY_SENSOR, "103.0", _energy_attrs())
        await hass.async_block_till_done()
        assert reload_spy.await_count == 1  # the entry update listener, once

    subentry = next(iter(entry.subentries.values()))
    assert subentry.data[CONF_ENERGY_SENSOR] == RENAMED_ENERGY_SENSOR
    assert subentry.unique_id == RENAMED_ENERGY_SENSOR

    state = hass.states.get(COST_ENTITY)
    assert state is not None
    # Baseline kept via the uuid: the rename-window kWh charged (at the
    # post-reload price — restart-class semantics), no uncharged interval.
    assert Decimal(state.state) == Decimal("1.50")
    assert state.attributes[CONF_ENERGY_SENSOR] == RENAMED_ENERGY_SENSOR

    hass.states.async_set(RENAMED_ENERGY_SENSOR, "104.0", _energy_attrs())
    await hass.async_block_till_done()
    assert _cost(hass) == Decimal("2.00")


async def test_price_source_rename_follows_into_entry_data(
    hass: HomeAssistant, source_entry: MockConfigEntry
) -> None:
    """A price sensor rename updates the entry data; the listener reloads."""
    _register_source(hass, source_entry, object_id="electricity_price", unique_id="price-a")
    hass.states.async_set(PRICE_SENSOR, "0.50", _price_attrs())
    hass.states.async_set(ENERGY_SENSOR, "100.0", _energy_attrs())
    entry = _make_entry(hass)
    await _setup(hass, entry)
    hass.states.async_set(ENERGY_SENSOR, "102.0", _energy_attrs())
    await hass.async_block_till_done()
    assert _cost(hass) == Decimal("1.00")

    with patch.object(
        hass.config_entries, "async_reload", wraps=hass.config_entries.async_reload
    ) as reload_spy:
        hass.states.async_remove(PRICE_SENSOR)
        er.async_get(hass).async_update_entity(PRICE_SENSOR, new_entity_id=RENAMED_PRICE_SENSOR)
        hass.states.async_set(RENAMED_PRICE_SENSOR, "0.50", _price_attrs())
        await hass.async_block_till_done()
        assert reload_spy.await_count == 1

    assert entry.data[CONF_PRICE_SENSOR] == RENAMED_PRICE_SENSOR
    state = hass.states.get(COST_ENTITY)
    assert state is not None
    assert state.attributes[CONF_PRICE_SENSOR] == RENAMED_PRICE_SENSOR
    assert Decimal(state.state) == Decimal("1.00")

    hass.states.async_set(ENERGY_SENSOR, "104.0", _energy_attrs())
    await hass.async_block_till_done()
    assert _cost(hass) == Decimal("2.00")


async def test_source_removal_warns_once_goes_unavailable_and_clears_link(
    hass: HomeAssistant, source_entry: MockConfigEntry, caplog: pytest.LogCaptureFixture
) -> None:
    """A removed source: one warning, unavailable sensor, link cleared, value kept."""
    device = _create_device(hass, source_entry, identifier="meter-1", name="Heat pump")
    _register_source(
        hass,
        source_entry,
        object_id="heat_pump_energy",
        unique_id="meter-a",
        device_id=device.id,
    )
    hass.states.async_set(PRICE_SENSOR, "0.50", _price_attrs())
    hass.states.async_set(ENERGY_SENSOR, "100.0", _energy_attrs())
    entry = _make_entry(hass)
    await _setup(hass, entry)
    hass.states.async_set(ENERGY_SENSOR, "102.0", _energy_attrs())
    await hass.async_block_till_done()

    registry = er.async_get(hass)
    cost_entity_id = registry.async_get_entity_id("sensor", DOMAIN, next(iter(entry.subentries)))
    assert cost_entity_id is not None
    cost_entry = registry.async_get(cost_entity_id)
    assert cost_entry is not None
    assert cost_entry.device_id == device.id
    assert _cost(hass, cost_entity_id) == Decimal("1.00")

    registry.async_remove(ENERGY_SENSOR)
    hass.states.async_remove(ENERGY_SENSOR)
    await hass.async_block_till_done()

    removal_warnings = [
        r
        for r in _integration_records(caplog, logging.WARNING)
        if "was removed from the entity registry" in r.message
    ]
    assert len(removal_warnings) == 1
    assert "Reconfigure" in removal_warnings[0].message

    state = hass.states.get(cost_entity_id)
    assert state is not None
    assert state.state == STATE_UNAVAILABLE
    cost_entry = registry.async_get(cost_entity_id)
    assert cost_entry is not None
    assert cost_entry.device_id is None  # link cleared post-reload

    # The value was retained: when a source reports again under the same id,
    # accrual continues from the preserved cost and baseline.
    hass.states.async_set(ENERGY_SENSOR, "104.0", _energy_attrs())
    await hass.async_block_till_done()
    assert _cost(hass, cost_entity_id) == Decimal("2.00")


async def test_device_linked_cost_sensor_attaches_and_names_plainly(
    hass: HomeAssistant, source_entry: MockConfigEntry
) -> None:
    """A device-backed source links the cost sensor and names it "Cost"."""
    device = _create_device(hass, source_entry, identifier="meter-1", name="Heat pump")
    _register_source(
        hass,
        source_entry,
        object_id="heat_pump_energy",
        unique_id="meter-a",
        device_id=device.id,
    )
    hass.states.async_set(PRICE_SENSOR, "0.50", _price_attrs())
    hass.states.async_set(ENERGY_SENSOR, "100.0", _energy_attrs())
    entry = _make_entry(hass)
    await _setup(hass, entry)

    registry = er.async_get(hass)
    cost_entry = registry.async_get(COST_ENTITY)
    assert cost_entry is not None
    assert cost_entry.unique_id == next(iter(entry.subentries))
    assert cost_entry.device_id == device.id

    state = hass.states.get(COST_ENTITY)
    assert state is not None
    # Device name prefix + plain "Cost": never "Heat pump Heat pump cost".
    assert state.attributes[ATTR_FRIENDLY_NAME] == "Heat pump Cost"


async def test_device_less_source_keeps_standalone_naming(
    hass: HomeAssistant, source_entry: MockConfigEntry
) -> None:
    """A registered but device-less source stays device-less, named by template."""
    _register_source(hass, source_entry, object_id="heat_pump_energy", unique_id="meter-a")
    hass.states.async_set(PRICE_SENSOR, "0.50", _price_attrs())
    hass.states.async_set(ENERGY_SENSOR, "100.0", _energy_attrs())
    entry = _make_entry(hass)
    await _setup(hass, entry)

    cost_entry = er.async_get(hass).async_get(COST_ENTITY)
    assert cost_entry is not None
    assert cost_entry.unique_id == next(iter(entry.subentries))
    assert cost_entry.device_id is None

    state = hass.states.get(COST_ENTITY)
    assert state is not None
    assert state.attributes[ATTR_FRIENDLY_NAME] == "Heat pump cost"


async def test_shared_device_rename_of_one_source_leaves_the_other_alone(
    hass: HomeAssistant, source_entry: MockConfigEntry
) -> None:
    """Two appliances on one device: both link; one rename touches one subentry."""
    device = _create_device(hass, source_entry, identifier="meter-box", name="Meter box")
    _register_source(
        hass,
        source_entry,
        object_id="washer_energy",
        unique_id="meter-w",
        device_id=device.id,
    )
    _register_source(
        hass,
        source_entry,
        object_id="dryer_energy",
        unique_id="meter-d",
        device_id=device.id,
    )
    entry = MockConfigEntry(
        domain=DOMAIN,
        title="Electricity price",
        data={CONF_PRICE_SENSOR: PRICE_SENSOR, CONF_CURRENCY: "EUR"},
        subentries_data=[
            ConfigSubentryData(
                data={CONF_ENERGY_SENSOR: "sensor.washer_energy"},
                subentry_type=SUBENTRY_TYPE_APPLIANCE,
                title="Washer",
                unique_id="sensor.washer_energy",
            ),
            ConfigSubentryData(
                data={CONF_ENERGY_SENSOR: "sensor.dryer_energy"},
                subentry_type=SUBENTRY_TYPE_APPLIANCE,
                title="Dryer",
                unique_id="sensor.dryer_energy",
            ),
        ],
    )
    entry.add_to_hass(hass)
    hass.states.async_set(PRICE_SENSOR, "0.50", _price_attrs())
    hass.states.async_set("sensor.washer_energy", "10.0", _energy_attrs())
    hass.states.async_set("sensor.dryer_energy", "20.0", _energy_attrs())
    await _setup(hass, entry)
    hass.states.async_set("sensor.washer_energy", "12.0", _energy_attrs())
    await hass.async_block_till_done()

    registry = er.async_get(hass)
    subentries = {subentry.title: subentry for subentry in entry.subentries.values()}
    washer_cost_id = registry.async_get_entity_id(
        "sensor", DOMAIN, subentries["Washer"].subentry_id
    )
    dryer_cost_id = registry.async_get_entity_id("sensor", DOMAIN, subentries["Dryer"].subentry_id)
    assert washer_cost_id is not None
    assert dryer_cost_id is not None
    for cost_id in (washer_cost_id, dryer_cost_id):
        cost_entry = registry.async_get(cost_id)
        assert cost_entry is not None
        assert cost_entry.device_id == device.id
    assert _cost(hass, washer_cost_id) == Decimal("1.00")

    # Rename the SECOND appliance's source: the follow loop must pass over
    # the first, non-matching subentry and touch only the dryer's config.
    hass.states.async_remove("sensor.dryer_energy")
    registry.async_update_entity("sensor.dryer_energy", new_entity_id="sensor.big_dryer_energy")
    hass.states.async_set("sensor.big_dryer_energy", "20.0", _energy_attrs())
    await hass.async_block_till_done()

    subentries = {subentry.title: subentry for subentry in entry.subentries.values()}
    assert subentries["Dryer"].data[CONF_ENERGY_SENSOR] == "sensor.big_dryer_energy"
    assert subentries["Dryer"].unique_id == "sensor.big_dryer_energy"
    # The sibling appliance is untouched by the rename.
    assert subentries["Washer"].data[CONF_ENERGY_SENSOR] == "sensor.washer_energy"
    assert subentries["Washer"].unique_id == "sensor.washer_energy"
    assert _cost(hass, washer_cost_id) == Decimal("1.00")
    for cost_id in (washer_cost_id, dryer_cost_id):
        cost_entry = registry.async_get(cost_id)
        assert cost_entry is not None
        assert cost_entry.device_id == device.id


async def test_device_move_follows_and_detach_clears_the_link(
    hass: HomeAssistant, source_entry: MockConfigEntry
) -> None:
    """A source moved between devices re-links; a detach clears the link."""
    device_one = _create_device(hass, source_entry, identifier="meter-1", name="Heat pump")
    device_two = _create_device(hass, source_entry, identifier="meter-2", name="Basement meter")
    _register_source(
        hass,
        source_entry,
        object_id="heat_pump_energy",
        unique_id="meter-a",
        device_id=device_one.id,
    )
    hass.states.async_set(PRICE_SENSOR, "0.50", _price_attrs())
    hass.states.async_set(ENERGY_SENSOR, "100.0", _energy_attrs())
    entry = _make_entry(hass)
    await _setup(hass, entry)

    registry = er.async_get(hass)
    cost_entry = registry.async_get(COST_ENTITY)
    assert cost_entry is not None
    assert cost_entry.device_id == device_one.id

    registry.async_update_entity(ENERGY_SENSOR, device_id=device_two.id)
    await hass.async_block_till_done()
    cost_entry = registry.async_get(COST_ENTITY)
    assert cost_entry is not None
    assert cost_entry.device_id == device_two.id

    registry.async_update_entity(ENERGY_SENSOR, device_id=None)
    await hass.async_block_till_done()
    cost_entry = registry.async_get(COST_ENTITY)
    assert cost_entry is not None
    assert cost_entry.device_id is None
    state = hass.states.get(COST_ENTITY)
    assert state is not None
    # Device-less again: the standalone name template takes over.
    assert state.attributes[ATTR_FRIENDLY_NAME] == "Heat pump cost"


async def test_subentry_removal_removes_the_sensor_but_not_the_device(
    hass: HomeAssistant, source_entry: MockConfigEntry
) -> None:
    """Removing an appliance removes its cost sensor; the source device stays."""
    device = _create_device(hass, source_entry, identifier="meter-1", name="Heat pump")
    _register_source(
        hass,
        source_entry,
        object_id="heat_pump_energy",
        unique_id="meter-a",
        device_id=device.id,
    )
    hass.states.async_set(PRICE_SENSOR, "0.50", _price_attrs())
    hass.states.async_set(ENERGY_SENSOR, "100.0", _energy_attrs())
    entry = _make_entry(hass)
    await _setup(hass, entry)
    assert er.async_get(hass).async_get(COST_ENTITY) is not None

    hass.config_entries.async_remove_subentry(entry, next(iter(entry.subentries)))
    await hass.async_block_till_done()

    assert er.async_get(hass).async_get(COST_ENTITY) is None
    # Link-not-create: the device belongs to the source's integration.
    assert dr.async_get(hass).async_get(device.id) is not None


async def test_upgrade_keeps_entity_id_and_restore_while_gaining_the_device(
    hass: HomeAssistant, source_entry: MockConfigEntry
) -> None:
    """UPGRADE REGRESSION PIN: pre-#28 installs keep their identity exactly.

    The cost sensor was registered by today's code (old naming, no device);
    the device name would generate a different object id, so an id
    regeneration would be visible. After upgrade: entity_id unchanged,
    unique_id unchanged, device gained, pre-upgrade restore payload (no
    uuid key) restores cost and baseline.
    """
    device = _create_device(hass, source_entry, identifier="meter-1", name="Garage heat pump")
    _register_source(
        hass,
        source_entry,
        object_id="heat_pump_energy",
        unique_id="meter-a",
        device_id=device.id,
    )
    entry = _make_entry(hass)
    subentry_id = next(iter(entry.subentries))
    registry = er.async_get(hass)
    # Exactly what today's code registered: subentry_id unique_id, the
    # "{appliance} cost" naming, no device link.
    registry.async_get_or_create(
        "sensor",
        DOMAIN,
        subentry_id,
        suggested_object_id="heat_pump_cost",
        config_entry=entry,
        config_subentry_id=subentry_id,
        original_name="Heat pump cost",
        has_entity_name=True,
    )
    mock_restore_cache_with_extra_data(
        hass,
        [
            (
                State(COST_ENTITY, "5.000"),
                {
                    "cost": "5.000",
                    "last_energy_kwh": "100.0",
                    "energy_sensor": ENERGY_SENSOR,
                },
            )
        ],
    )
    hass.states.async_set(PRICE_SENSOR, "0.30", _price_attrs())
    hass.states.async_set(ENERGY_SENSOR, "104.0", _energy_attrs())
    await _setup(hass, entry)

    cost_entry = registry.async_get(COST_ENTITY)
    assert cost_entry is not None  # entity_id unchanged — statistics continue
    assert cost_entry.unique_id == subentry_id
    assert cost_entry.device_id == device.id
    assert registry.async_get("sensor.garage_heat_pump_cost") is None
    # Cost and baseline restored: the 4 kWh across the upgrade charges.
    assert _cost(hass) == Decimal("5.000") + Decimal("4.0") * Decimal("0.30")


async def test_cosmetic_registry_edits_are_ignored(
    hass: HomeAssistant, source_entry: MockConfigEntry
) -> None:
    """BINDING ignore rule: a friendly-name or icon edit never reloads."""
    _register_source(hass, source_entry, object_id="heat_pump_energy", unique_id="meter-a")
    hass.states.async_set(PRICE_SENSOR, "0.50", _price_attrs())
    hass.states.async_set(ENERGY_SENSOR, "100.0", _energy_attrs())
    entry = _make_entry(hass)
    await _setup(hass, entry)
    subentry_before = next(iter(entry.subentries.values()))

    with patch.object(
        hass.config_entries, "async_reload", wraps=hass.config_entries.async_reload
    ) as reload_spy:
        registry = er.async_get(hass)
        registry.async_update_entity(ENERGY_SENSOR, name="Fancy meter name")
        registry.async_update_entity(ENERGY_SENSOR, icon="mdi:flash")
        await hass.async_block_till_done()
        assert reload_spy.await_count == 0

    assert entry.state is ConfigEntryState.LOADED
    subentry_after = next(iter(entry.subentries.values()))
    assert subentry_after.data == subentry_before.data
    assert subentry_after.unique_id == subentry_before.unique_id


async def test_rename_collision_updates_data_only_and_says_so(
    hass: HomeAssistant, source_entry: MockConfigEntry, caplog: pytest.LogCaptureFixture
) -> None:
    """A rename onto another appliance's sensor keeps the unique_id and errors.

    Both appliances end up tracking the same sensor; the error names both
    titles and the two-step remediation (reconfigure the other first).
    """
    _register_source(hass, source_entry, object_id="old_meter", unique_id="meter-old")
    entry = MockConfigEntry(
        domain=DOMAIN,
        title="Electricity price",
        data={CONF_PRICE_SENSOR: PRICE_SENSOR, CONF_CURRENCY: "EUR"},
        subentries_data=[
            ConfigSubentryData(
                data={CONF_ENERGY_SENSOR: "sensor.old_meter"},
                subentry_type=SUBENTRY_TYPE_APPLIANCE,
                title="Washer",
                unique_id="sensor.old_meter",
            ),
            ConfigSubentryData(
                data={CONF_ENERGY_SENSOR: "sensor.new_meter"},
                subentry_type=SUBENTRY_TYPE_APPLIANCE,
                title="Dryer",
                unique_id="sensor.new_meter",
            ),
        ],
    )
    entry.add_to_hass(hass)
    hass.states.async_set(PRICE_SENSOR, "0.50", _price_attrs())
    hass.states.async_set("sensor.old_meter", "10.0", _energy_attrs())
    # sensor.new_meter deliberately has no state and no registry entry: the
    # rename target must be free in the registry for core to allow it.
    await _setup(hass, entry)

    er.async_get(hass).async_update_entity("sensor.old_meter", new_entity_id="sensor.new_meter")
    await hass.async_block_till_done()

    subentries = {subentry.title: subentry for subentry in entry.subentries.values()}
    assert subentries["Washer"].data[CONF_ENERGY_SENSOR] == "sensor.new_meter"
    assert subentries["Washer"].unique_id == "sensor.old_meter"  # kept, no collision
    assert subentries["Dryer"].unique_id == "sensor.new_meter"

    errors = _integration_records(caplog, logging.ERROR)
    collision_errors = [r for r in errors if "double-count" in r.message]
    assert len(collision_errors) == 1
    message = collision_errors[0].message
    assert "Washer" in message
    assert "Dryer" in message
    assert "reconfigure Dryer to a different energy sensor first" in message
    assert "then reconfigure Washer" in message
    assert entry.state is ConfigEntryState.LOADED


async def test_stale_source_id_at_setup_is_unavailable_with_a_warning(
    hass: HomeAssistant, caplog: pytest.LogCaptureFixture
) -> None:
    """A never-existing source id: no crash, unavailable sensor, one warning."""
    hass.states.async_set(PRICE_SENSOR, "0.50", _price_attrs())
    entry = _make_entry(hass, energy_sensor="sensor.never_existed")
    await _setup(hass, entry)

    assert entry.state is ConfigEntryState.LOADED
    state = hass.states.get(COST_ENTITY)
    assert state is not None
    assert state.state == STATE_UNAVAILABLE
    setup_warnings = [
        r
        for r in _integration_records(caplog, logging.WARNING)
        if "has no usable state at setup" in r.message
    ]
    assert len(setup_warnings) == 1
    assert "reconfigure" in setup_warnings[0].message

    # A later registry CREATE for the tracked id is ignored (the ignore
    # rule covers non-update actions): no reload, config untouched.
    with patch.object(
        hass.config_entries, "async_reload", wraps=hass.config_entries.async_reload
    ) as reload_spy:
        er.async_get(hass).async_get_or_create(
            "sensor", "test", "late-meter", suggested_object_id="never_existed"
        )
        await hass.async_block_till_done()
        assert reload_spy.await_count == 0
    subentry = next(iter(entry.subentries.values()))
    assert subentry.data[CONF_ENERGY_SENSOR] == "sensor.never_existed"
