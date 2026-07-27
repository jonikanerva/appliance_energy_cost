"""Tests for the preview_backfill service.

Statistics are seeded through the supported import API (source="recorder")
and read back through the service; the preview must summarise them without
writing anything — the proof-of-no-writes test pins that.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

import pytest
from freezegun.api import FrozenDateTimeFactory
from homeassistant.components.recorder.models import (
    StatisticData,
    StatisticMeanType,
    StatisticMetaData,
)
from homeassistant.components.recorder.statistics import (
    async_import_statistics,
    get_metadata,
)
from homeassistant.config_entries import ConfigSubentryData
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ServiceValidationError
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.json import json_bytes
from pytest_homeassistant_custom_component.common import MockConfigEntry
from pytest_homeassistant_custom_component.components.recorder.common import (
    async_wait_recording_done,
    statistics_during_period,
)
from syrupy.assertion import SnapshotAssertion

from custom_components.appliance_energy_cost.const import (
    ATTR_APPLIANCE,
    ATTR_APPLIANCES,
    ATTR_CONFIG_ENTRY,
    ATTR_END,
    ATTR_END_ENERGY_KWH,
    ATTR_ENERGY_GAP_HOURS,
    ATTR_EXPECTED_HOURS,
    ATTR_FIRST_POINT,
    ATTR_HOURLY_POINTS,
    ATTR_INVALID_ENERGY_HOURS,
    ATTR_INVALID_ENERGY_RANGES,
    ATTR_LAST_POINT,
    ATTR_MISSING_PRICE_HOURS,
    ATTR_MISSING_PRICE_RANGES,
    ATTR_OK,
    ATTR_START,
    ATTR_STATISTIC_ID,
    ATTR_STRICT,
    ATTR_TOTAL_COST,
    ATTR_TOTAL_ENERGY_KWH,
    ATTR_VALID,
    CONF_CURRENCY,
    CONF_ENERGY_SENSOR,
    CONF_PRICE_SENSOR,
    DOMAIN,
    RANGE_CAP,
    SERVICE_PREVIEW_BACKFILL,
    SUBENTRY_TYPE_APPLIANCE,
)
from custom_components.appliance_energy_cost.services import (
    _ranges_payload,
    hours_to_contiguous_ranges,
)

PRICE_SENSOR = "sensor.electricity_price"
ENERGY_SENSOR = "sensor.heat_pump_energy"
ENERGY_SENSOR_B = "sensor.sauna_energy"
COST_ENTITY = "sensor.heat_pump_cost"

PERIOD_START = datetime(2026, 7, 20, 0, 0, tzinfo=UTC)


def _hour(index: int, base: datetime = PERIOD_START) -> datetime:
    return base + timedelta(hours=index)


def _seed_energy(
    hass: HomeAssistant,
    statistic_id: str,
    rows: list[tuple[datetime, float, float]],
    unit: str = "kWh",
) -> None:
    """Seed hourly energy LTS rows: (start, cumulative sum, meter state)."""
    async_import_statistics(
        hass,
        StatisticMetaData(
            has_sum=True,
            mean_type=StatisticMeanType.NONE,
            name=None,
            source="recorder",
            statistic_id=statistic_id,
            unit_class="energy",
            unit_of_measurement=unit,
        ),
        [StatisticData(start=start, sum=total, state=state) for start, total, state in rows],
    )


def _seed_price(
    hass: HomeAssistant,
    rows: list[tuple[datetime, float]],
    unit: str = "EUR/kWh",
) -> None:
    """Seed hourly price LTS rows: (start, hourly mean price)."""
    async_import_statistics(
        hass,
        StatisticMetaData(
            has_sum=False,
            mean_type=StatisticMeanType.ARITHMETIC,
            name=None,
            source="recorder",
            statistic_id=PRICE_SENSOR,
            unit_class=None,
            unit_of_measurement=unit,
        ),
        [StatisticData(start=start, mean=mean) for start, mean in rows],
    )


def _clean_energy_rows(base: datetime = PERIOD_START) -> list[tuple[datetime, float, float]]:
    """Four hours with a 1 kWh change each; meter ends at 104.0."""
    return [(_hour(i, base), 1.0 + i, 101.0 + i) for i in range(4)]


def _clean_price_rows(base: datetime = PERIOD_START) -> list[tuple[datetime, float]]:
    """Four hourly prices 0.1..0.4 EUR/kWh."""
    return [(_hour(i, base), (i + 1) / 10) for i in range(4)]


async def _setup_entry(
    hass: HomeAssistant,
    appliances: tuple[tuple[str, str], ...] = (("Heat pump", ENERGY_SENSOR),),
) -> MockConfigEntry:
    entry = MockConfigEntry(
        domain=DOMAIN,
        title="Electricity price",
        data={CONF_PRICE_SENSOR: PRICE_SENSOR, CONF_CURRENCY: "EUR"},
        subentries_data=[
            ConfigSubentryData(
                data={CONF_ENERGY_SENSOR: energy_sensor},
                subentry_type=SUBENTRY_TYPE_APPLIANCE,
                title=name,
                unique_id=energy_sensor,
            )
            for name, energy_sensor in appliances
        ],
    )
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    return entry


async def _call_preview(
    hass: HomeAssistant,
    entry: MockConfigEntry,
    **data: object,
) -> dict[str, object]:
    payload: dict[str, object] = {
        ATTR_CONFIG_ENTRY: entry.entry_id,
        ATTR_START: _hour(0).isoformat(),
        ATTR_END: _hour(4).isoformat(),
    }
    payload.update(data)
    # A None override drops the key: the service must apply its own default.
    payload = {key: value for key, value in payload.items() if value is not None}
    response = await hass.services.async_call(
        DOMAIN,
        SERVICE_PREVIEW_BACKFILL,
        payload,
        blocking=True,
        return_response=True,
    )
    assert isinstance(response, dict)
    return response


def _appliance_summaries(response: dict[str, object]) -> list[dict[str, object]]:
    summaries = response[ATTR_APPLIANCES]
    assert isinstance(summaries, list)
    return summaries


async def test_clean_period_full_summary(hass: HomeAssistant) -> None:
    """A gap-free period: exact totals, ok true, all gap counts zero."""
    entry = await _setup_entry(hass)
    _seed_energy(hass, ENERGY_SENSOR, _clean_energy_rows())
    _seed_price(hass, _clean_price_rows())
    await async_wait_recording_done(hass)

    response = await _call_preview(hass, entry)

    assert response[ATTR_START] == "2026-07-20T00:00:00+00:00"
    assert response[ATTR_END] == "2026-07-20T04:00:00+00:00"
    assert response[ATTR_EXPECTED_HOURS] == 4
    assert response[ATTR_STRICT] is True
    assert response[ATTR_OK] is True
    assert response[CONF_CURRENCY] == "EUR"
    assert response[CONF_PRICE_SENSOR] == PRICE_SENSOR
    (summary,) = _appliance_summaries(response)
    assert summary == {
        ATTR_APPLIANCE: "Heat pump",
        CONF_ENERGY_SENSOR: ENERGY_SENSOR,
        ATTR_STATISTIC_ID: COST_ENTITY,
        ATTR_VALID: True,
        ATTR_HOURLY_POINTS: 4,
        ATTR_FIRST_POINT: "2026-07-20T00:00:00+00:00",
        ATTR_LAST_POINT: "2026-07-20T03:00:00+00:00",
        ATTR_TOTAL_ENERGY_KWH: 4.0,
        # 1 kWh at each of 0.1 + 0.2 + 0.3 + 0.4 EUR/kWh.
        ATTR_TOTAL_COST: 1.0,
        ATTR_END_ENERGY_KWH: 104.0,
        ATTR_MISSING_PRICE_HOURS: 0,
        ATTR_MISSING_PRICE_RANGES: [],
        ATTR_INVALID_ENERGY_HOURS: 0,
        ATTR_INVALID_ENERGY_RANGES: [],
        ATTR_ENERGY_GAP_HOURS: 0,
    }


async def test_price_gap_counts_ranges_and_strict_flip(hass: HomeAssistant) -> None:
    """Hours with consumption but no price: counted, ranged, and strict-gated."""
    entry = await _setup_entry(hass)
    _seed_energy(hass, ENERGY_SENSOR, _clean_energy_rows())
    # Prices only for hours 0 and 3 — hours 1 and 2 are a contiguous gap.
    _seed_price(hass, [(_hour(0), 0.1), (_hour(3), 0.4)])
    await async_wait_recording_done(hass)

    response = await _call_preview(hass, entry)

    assert response[ATTR_OK] is False
    (summary,) = _appliance_summaries(response)
    assert summary[ATTR_VALID] is False
    assert summary[ATTR_HOURLY_POINTS] == 2
    assert summary[ATTR_MISSING_PRICE_HOURS] == 2
    assert summary[ATTR_MISSING_PRICE_RANGES] == [
        {"start": "2026-07-20T01:00:00+00:00", "end": "2026-07-20T03:00:00+00:00"}
    ]
    assert summary[ATTR_ENERGY_GAP_HOURS] == 0
    assert summary[ATTR_TOTAL_ENERGY_KWH] == 2.0
    assert summary[ATTR_TOTAL_COST] == 0.5

    relaxed = await _call_preview(hass, entry, **{ATTR_STRICT: False})
    assert relaxed[ATTR_OK] is True
    (relaxed_summary,) = _appliance_summaries(relaxed)
    assert relaxed_summary[ATTR_VALID] is False
    assert relaxed_summary[ATTR_MISSING_PRICE_HOURS] == 2


async def test_meter_reset_flags_invalid_energy(hass: HomeAssistant) -> None:
    """A decreasing seeded sum yields a negative change: flagged, not priced."""
    entry = await _setup_entry(hass)
    _seed_energy(hass, ENERGY_SENSOR, [(_hour(0), 5.0, 100.0), (_hour(1), 3.0, 50.0)])
    _seed_price(hass, [(_hour(0), 0.1), (_hour(1), 0.1)])
    await async_wait_recording_done(hass)

    response = await _call_preview(hass, entry, **{ATTR_END: _hour(2).isoformat()})

    assert response[ATTR_OK] is False
    (summary,) = _appliance_summaries(response)
    assert summary[ATTR_VALID] is False
    assert summary[ATTR_HOURLY_POINTS] == 1
    assert summary[ATTR_INVALID_ENERGY_HOURS] == 1
    assert summary[ATTR_INVALID_ENERGY_RANGES] == [
        {"start": "2026-07-20T01:00:00+00:00", "end": "2026-07-20T02:00:00+00:00"}
    ]
    assert summary[ATTR_ENERGY_GAP_HOURS] == 0
    assert summary[ATTR_TOTAL_ENERGY_KWH] == 5.0
    assert summary[ATTR_TOTAL_COST] == 0.5


async def test_energy_hole_reported_as_gap_count(hass: HomeAssistant) -> None:
    """BINDING: omitted energy hours == expected - points - flagged hours."""
    entry = await _setup_entry(hass)
    # Hours 2 and 3 have no energy row at all (a recorder hole).
    _seed_energy(
        hass,
        ENERGY_SENSOR,
        [
            (_hour(0), 1.0, 101.0),
            (_hour(1), 2.0, 102.0),
            (_hour(4), 3.0, 103.0),
            (_hour(5), 4.0, 104.0),
        ],
    )
    _seed_price(hass, [(_hour(i), 0.1) for i in range(6)])
    await async_wait_recording_done(hass)

    response = await _call_preview(hass, entry, **{ATTR_END: _hour(6).isoformat()})

    assert response[ATTR_EXPECTED_HOURS] == 6
    # The hole does not fail validation: it is reported, not treated as
    # source-data corruption.
    assert response[ATTR_OK] is True
    (summary,) = _appliance_summaries(response)
    assert summary[ATTR_VALID] is True
    assert summary[ATTR_HOURLY_POINTS] == 4
    assert summary[ATTR_MISSING_PRICE_HOURS] == 0
    assert summary[ATTR_INVALID_ENERGY_HOURS] == 0
    assert summary[ATTR_ENERGY_GAP_HOURS] == 2


async def test_empty_period_is_a_wellformed_summary(hass: HomeAssistant) -> None:
    """A period with no rows at all is an all-zeros summary, not an error."""
    entry = await _setup_entry(hass)
    _seed_energy(hass, ENERGY_SENSOR, _clean_energy_rows())
    _seed_price(hass, _clean_price_rows())
    await async_wait_recording_done(hass)

    empty_start = _hour(48)
    response = await _call_preview(
        hass,
        entry,
        **{ATTR_START: empty_start.isoformat(), ATTR_END: (_hour(52)).isoformat()},
    )

    assert response[ATTR_EXPECTED_HOURS] == 4
    assert response[ATTR_OK] is True
    (summary,) = _appliance_summaries(response)
    assert summary[ATTR_VALID] is True
    assert summary[ATTR_HOURLY_POINTS] == 0
    assert summary[ATTR_FIRST_POINT] is None
    assert summary[ATTR_LAST_POINT] is None
    assert summary[ATTR_TOTAL_ENERGY_KWH] == 0.0
    assert summary[ATTR_TOTAL_COST] == 0.0
    assert summary[ATTR_END_ENERGY_KWH] is None
    assert summary[ATTR_ENERGY_GAP_HOURS] == 4


async def test_multi_appliance_breakdown_and_filter(hass: HomeAssistant) -> None:
    """Every appliance is summarised; the filter narrows to a subset."""
    entry = await _setup_entry(
        hass, appliances=(("Heat pump", ENERGY_SENSOR), ("Sauna", ENERGY_SENSOR_B))
    )
    _seed_energy(hass, ENERGY_SENSOR, _clean_energy_rows())
    _seed_energy(hass, ENERGY_SENSOR_B, [(_hour(i), 2.0 + 2 * i, 51.0 + i) for i in range(4)])
    _seed_price(hass, _clean_price_rows())
    await async_wait_recording_done(hass)

    response = await _call_preview(hass, entry)
    summaries = _appliance_summaries(response)
    assert [summary[ATTR_APPLIANCE] for summary in summaries] == ["Heat pump", "Sauna"]
    assert summaries[0][ATTR_TOTAL_ENERGY_KWH] == 4.0
    assert summaries[1][ATTR_TOTAL_ENERGY_KWH] == 8.0

    filtered = await _call_preview(hass, entry, **{ATTR_APPLIANCES: [ENERGY_SENSOR_B]})
    (sauna,) = _appliance_summaries(filtered)
    assert sauna[ATTR_APPLIANCE] == "Sauna"


async def test_unknown_appliance_filter_is_rejected(hass: HomeAssistant) -> None:
    """A filter id no appliance uses is a translated validation error."""
    entry = await _setup_entry(hass)
    with pytest.raises(ServiceValidationError) as excinfo:
        await _call_preview(hass, entry, **{ATTR_APPLIANCES: ["sensor.no_such_energy"]})
    assert excinfo.value.translation_domain == DOMAIN
    assert excinfo.value.translation_key == "unknown_appliances"
    assert excinfo.value.translation_placeholders == {"entity_ids": "sensor.no_such_energy"}


async def test_unaligned_start_is_rejected(hass: HomeAssistant) -> None:
    """A start off the top of the UTC hour names the field and the UTC value."""
    entry = await _setup_entry(hass)
    with pytest.raises(ServiceValidationError) as excinfo:
        await _call_preview(hass, entry, **{ATTR_START: "2026-07-20T00:30:00+00:00"})
    assert excinfo.value.translation_key == "period_not_top_of_hour"
    assert excinfo.value.translation_placeholders == {
        "field": ATTR_START,
        "value": "2026-07-20T00:30:00+00:00",
    }


async def test_offset_aligned_start_is_rejected_in_utc(hass: HomeAssistant) -> None:
    """Top-of-hour in a half-hour-offset zone is not top-of-hour in UTC."""
    entry = await _setup_entry(hass)
    with pytest.raises(ServiceValidationError) as excinfo:
        await _call_preview(hass, entry, **{ATTR_START: "2026-07-20T05:00:00+05:30"})
    assert excinfo.value.translation_key == "period_not_top_of_hour"
    assert excinfo.value.translation_placeholders == {
        "field": ATTR_START,
        "value": "2026-07-19T23:30:00+00:00",
    }


async def test_end_not_after_start_is_rejected(hass: HomeAssistant) -> None:
    """end == start (and anything earlier) is rejected."""
    entry = await _setup_entry(hass)
    with pytest.raises(ServiceValidationError) as excinfo:
        await _call_preview(hass, entry, **{ATTR_END: _hour(0).isoformat()})
    assert excinfo.value.translation_key == "end_not_after_start"


async def test_future_end_is_rejected(hass: HomeAssistant, freezer: FrozenDateTimeFactory) -> None:
    """An end beyond now is rejected — statistics cannot exist there yet."""
    entry = await _setup_entry(hass)
    freezer.move_to("2026-08-01 06:30:00+00:00")
    with pytest.raises(ServiceValidationError) as excinfo:
        await _call_preview(hass, entry, **{ATTR_END: "2026-08-02T00:00:00+00:00"})
    assert excinfo.value.translation_key == "end_in_future"


async def test_default_end_is_start_of_current_utc_hour(
    hass: HomeAssistant, freezer: FrozenDateTimeFactory
) -> None:
    """Omitted end defaults to the current UTC hour start: no partial hour."""
    entry = await _setup_entry(hass)
    base = datetime(2026, 8, 1, 0, 0, tzinfo=UTC)
    freezer.move_to("2026-08-01 06:30:00+00:00")
    _seed_energy(hass, ENERGY_SENSOR, _clean_energy_rows(base))
    _seed_price(hass, _clean_price_rows(base))
    await async_wait_recording_done(hass)

    response = await _call_preview(hass, entry, **{ATTR_START: base.isoformat(), ATTR_END: None})
    assert response[ATTR_END] == "2026-08-01T06:00:00+00:00"
    assert response[ATTR_EXPECTED_HOURS] == 6
    (summary,) = _appliance_summaries(response)
    assert summary[ATTR_HOURLY_POINTS] == 4
    assert summary[ATTR_ENERGY_GAP_HOURS] == 2


async def test_naive_datetimes_are_normalised_from_ha_timezone(hass: HomeAssistant) -> None:
    """A naive boundary is interpreted in HA's timezone and converted to UTC."""
    await hass.config.async_set_time_zone("Europe/Helsinki")
    entry = await _setup_entry(hass)
    _seed_energy(hass, ENERGY_SENSOR, _clean_energy_rows())
    _seed_price(hass, _clean_price_rows())
    await async_wait_recording_done(hass)

    # Helsinki is UTC+3 in July: naive 03:00 local == 00:00 UTC.
    response = await _call_preview(
        hass,
        entry,
        **{ATTR_START: "2026-07-20T03:00:00", ATTR_END: "2026-07-20T07:00:00"},
    )
    assert response[ATTR_START] == "2026-07-20T00:00:00+00:00"
    assert response[ATTR_END] == "2026-07-20T04:00:00+00:00"
    (summary,) = _appliance_summaries(response)
    assert summary[ATTR_HOURLY_POINTS] == 4


async def test_unknown_and_unloaded_entries_are_rejected(hass: HomeAssistant) -> None:
    """A missing or not-loaded config entry is a service validation error."""
    entry = await _setup_entry(hass)
    with pytest.raises(ServiceValidationError):
        await _call_preview(hass, entry, **{ATTR_CONFIG_ENTRY: "0123456789abcdef"})

    other = MockConfigEntry(
        domain=DOMAIN,
        title="Other price",
        data={CONF_PRICE_SENSOR: "sensor.other_price", CONF_CURRENCY: "EUR"},
    )
    other.add_to_hass(hass)
    with pytest.raises(ServiceValidationError):
        await _call_preview(hass, entry, **{ATTR_CONFIG_ENTRY: other.entry_id})


async def test_units_come_from_metadata_not_live_state(hass: HomeAssistant) -> None:
    """Wh energy and EUR/MWh price convert via stored METADATA, never live state."""
    entry = await _setup_entry(hass)
    # Live entities contradict the stored history on purpose: the preview
    # must not read them. No attributes at all — a live-state reader would
    # fail or misprice.
    hass.states.async_set(ENERGY_SENSOR, "999999")
    hass.states.async_set(PRICE_SENSOR, "999999")
    _seed_energy(
        hass,
        ENERGY_SENSOR,
        [(_hour(0), 1000.0, 500000.0), (_hour(1), 2000.0, 501000.0)],
        unit="Wh",
    )
    _seed_price(hass, [(_hour(0), 100.0), (_hour(1), 100.0)], unit="EUR/MWh")
    await async_wait_recording_done(hass)

    response = await _call_preview(hass, entry, **{ATTR_END: _hour(2).isoformat()})

    assert response[ATTR_OK] is True
    (summary,) = _appliance_summaries(response)
    # 1000 Wh/h == 1 kWh/h; 100 EUR/MWh == 0.1 EUR/kWh.
    assert summary[ATTR_TOTAL_ENERGY_KWH] == 2.0
    assert summary[ATTR_TOTAL_COST] == 0.2
    assert summary[ATTR_END_ENERGY_KWH] == 501.0


async def test_price_currency_mismatch_in_metadata_is_rejected(hass: HomeAssistant) -> None:
    """A stored price unit in another currency fails closed — never rescaled."""
    entry = await _setup_entry(hass)
    _seed_energy(hass, ENERGY_SENSOR, _clean_energy_rows())
    _seed_price(hass, _clean_price_rows(), unit="NOK/kWh")
    await async_wait_recording_done(hass)

    with pytest.raises(ServiceValidationError) as excinfo:
        await _call_preview(hass, entry)
    assert excinfo.value.translation_key == "price_statistics_currency_mismatch"
    assert excinfo.value.translation_placeholders == {
        "numerator": "NOK",
        "currency": "EUR",
        "price_sensor": PRICE_SENSOR,
    }


async def test_price_sensor_without_statistics_names_the_remedy(hass: HomeAssistant) -> None:
    """No price statistics at all: the error names the state_class remedy."""
    entry = await _setup_entry(hass)
    _seed_energy(hass, ENERGY_SENSOR, _clean_energy_rows())
    await async_wait_recording_done(hass)

    with pytest.raises(ServiceValidationError) as excinfo:
        await _call_preview(hass, entry)
    assert excinfo.value.translation_domain == DOMAIN
    assert excinfo.value.translation_key == "price_sensor_no_statistics"
    assert excinfo.value.translation_placeholders == {"price_sensor": PRICE_SENSOR}


async def test_appliance_without_statistics_fails_whole_call_filter_recovers(
    hass: HomeAssistant,
) -> None:
    """One statistics-less appliance fails the call; the filter recovers."""
    entry = await _setup_entry(
        hass, appliances=(("Heat pump", ENERGY_SENSOR), ("Sauna", ENERGY_SENSOR_B))
    )
    _seed_energy(hass, ENERGY_SENSOR, _clean_energy_rows())
    _seed_price(hass, _clean_price_rows())
    await async_wait_recording_done(hass)

    with pytest.raises(ServiceValidationError) as excinfo:
        await _call_preview(hass, entry)
    assert excinfo.value.translation_key == "appliance_no_statistics"
    assert excinfo.value.translation_placeholders == {
        "appliance": "Sauna",
        "energy_sensor": ENERGY_SENSOR_B,
    }

    healthy = await _call_preview(hass, entry, **{ATTR_APPLIANCES: [ENERGY_SENSOR]})
    (summary,) = _appliance_summaries(healthy)
    assert summary[ATTR_APPLIANCE] == "Heat pump"
    assert healthy[ATTR_OK] is True


async def test_energy_statistics_unit_outside_domain_is_rejected(hass: HomeAssistant) -> None:
    """A recorder-convertible but domain-unsupported energy unit fails closed."""
    entry = await _setup_entry(hass)
    _seed_energy(hass, ENERGY_SENSOR, _clean_energy_rows(), unit="GJ")
    _seed_price(hass, _clean_price_rows())
    await async_wait_recording_done(hass)

    with pytest.raises(ServiceValidationError) as excinfo:
        await _call_preview(hass, entry)
    assert excinfo.value.translation_key == "energy_statistics_unit_unsupported"
    assert excinfo.value.translation_placeholders == {
        "unit": "GJ",
        "appliance": "Heat pump",
        "energy_sensor": ENERGY_SENSOR,
    }


async def test_damaged_subentry_fails_whole_call(hass: HomeAssistant) -> None:
    """A cost sensor missing from the entity registry fails the whole call."""
    entry = await _setup_entry(hass)
    registry = er.async_get(hass)
    registry.async_remove(COST_ENTITY)
    await hass.async_block_till_done()

    with pytest.raises(ServiceValidationError) as excinfo:
        await _call_preview(hass, entry)
    assert excinfo.value.translation_key == "appliance_subentry_damaged"
    assert excinfo.value.translation_placeholders == {"subentry": "Heat pump"}


def test_hours_to_contiguous_ranges() -> None:
    """Range derivation: empty, single, adjacent-merge and disjoint inputs."""
    h0, h1, h2, h4 = _hour(0), _hour(1), _hour(2), _hour(4)
    one_hour = timedelta(hours=1)
    assert hours_to_contiguous_ranges(()) == ()
    assert hours_to_contiguous_ranges((h0,)) == ((h0, h1),)
    assert hours_to_contiguous_ranges((h0, h1, h2)) == ((h0, _hour(3)),)
    assert hours_to_contiguous_ranges((h0, h2, h4)) == (
        (h0, h1),
        (h2, _hour(3)),
        (h4, h4 + one_hour),
    )


def test_ranges_payload_caps_at_range_cap() -> None:
    """Twelve disjoint gaps serialise as RANGE_CAP ranges; counts stay exact."""
    hours = tuple(_hour(2 * i) for i in range(12))
    payload = _ranges_payload(hours)
    assert len(payload) == RANGE_CAP
    assert payload[0] == {
        "start": "2026-07-20T00:00:00+00:00",
        "end": "2026-07-20T01:00:00+00:00",
    }


async def test_full_response_snapshot_and_json_round_trip(
    hass: HomeAssistant, snapshot: SnapshotAssertion
) -> None:
    """One full response, pinned; json_bytes round-trips it Decimal-free."""
    entry = await _setup_entry(hass)
    _seed_energy(hass, ENERGY_SENSOR, _clean_energy_rows())
    _seed_price(hass, [(_hour(0), 0.1), (_hour(3), 0.4)])
    await async_wait_recording_done(hass)

    response = await _call_preview(hass, entry)

    assert response == snapshot
    assert json.loads(json_bytes(response)) == response


async def test_preview_writes_nothing(hass: HomeAssistant) -> None:
    """Proof of no writes: no cost series appears; sources re-read identical."""
    entry = await _setup_entry(hass)
    _seed_energy(hass, ENERGY_SENSOR, _clean_energy_rows())
    _seed_price(hass, _clean_price_rows())
    await async_wait_recording_done(hass)

    before_energy = statistics_during_period(hass, _hour(0), statistic_ids={ENERGY_SENSOR})
    before_price = statistics_during_period(hass, _hour(0), statistic_ids={PRICE_SENSOR})
    assert before_energy[ENERGY_SENSOR]
    assert before_price[PRICE_SENSOR]

    response = await _call_preview(hass, entry)
    assert response[ATTR_OK] is True
    await async_wait_recording_done(hass)

    assert get_metadata(hass, statistic_ids={COST_ENTITY}) == {}
    assert statistics_during_period(hass, _hour(0), statistic_ids={COST_ENTITY}) == {}
    assert statistics_during_period(hass, _hour(0), statistic_ids={ENERGY_SENSOR}) == before_energy
    assert statistics_during_period(hass, _hour(0), statistic_ids={PRICE_SENSOR}) == before_price
