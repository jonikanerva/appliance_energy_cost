"""Constants for the Appliance Energy Cost integration."""

from __future__ import annotations

from typing import Final

DOMAIN: Final = "appliance_energy_cost"

CONF_PRICE_SENSOR: Final = "price_sensor"
CONF_ENERGY_SENSOR: Final = "energy_sensor"
# Same key value as homeassistant.const.CONF_CURRENCY, defined here so pure
# domain modules (models.py) never import homeassistant.*; equality with the
# core constant is pinned by an integration test.
CONF_CURRENCY: Final = "currency"

SUBENTRY_TYPE_APPLIANCE: Final = "appliance"

ATTR_PRICE_GAP_ACTIVE: Final = "price_gap_active"

SERVICE_PREVIEW_BACKFILL: Final = "preview_backfill"

# preview_backfill request fields.
ATTR_CONFIG_ENTRY: Final = "config_entry"
ATTR_START: Final = "start"
ATTR_END: Final = "end"
ATTR_APPLIANCES: Final = "appliances"
ATTR_STRICT: Final = "strict"

# preview_backfill response keys (request keys above are echoed).
ATTR_EXPECTED_HOURS: Final = "expected_hours"
ATTR_OK: Final = "ok"
ATTR_APPLIANCE: Final = "appliance"
ATTR_STATISTIC_ID: Final = "statistic_id"
ATTR_VALID: Final = "valid"
ATTR_HOURLY_POINTS: Final = "hourly_points"
ATTR_FIRST_POINT: Final = "first_point"
ATTR_LAST_POINT: Final = "last_point"
ATTR_TOTAL_ENERGY_KWH: Final = "total_energy_kwh"
ATTR_TOTAL_COST: Final = "total_cost"
ATTR_END_ENERGY_KWH: Final = "end_energy_kwh"
ATTR_MISSING_PRICE_HOURS: Final = "missing_price_hours"
ATTR_MISSING_PRICE_RANGES: Final = "missing_price_ranges"
ATTR_INVALID_ENERGY_HOURS: Final = "invalid_energy_hours"
ATTR_INVALID_ENERGY_RANGES: Final = "invalid_energy_ranges"
ATTR_ENERGY_GAP_HOURS: Final = "energy_gap_hours"

RANGE_CAP: Final = 10
"""Maximum contiguous ranges listed per finding; the count field is exact."""
