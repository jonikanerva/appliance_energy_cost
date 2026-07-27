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
SERVICE_IMPORT_BACKFILL: Final = "import_backfill"
SERVICE_CALIBRATE_COST: Final = "calibrate_cost"

# preview_backfill request fields (shared with import_backfill).
ATTR_CONFIG_ENTRY: Final = "config_entry"
ATTR_START: Final = "start"
ATTR_END: Final = "end"
ATTR_APPLIANCES: Final = "appliances"
ATTR_STRICT: Final = "strict"

# import_backfill request fields (in addition to the shared ones above).
ATTR_CONFIRM: Final = "confirm"
ATTR_OVERWRITE_EXISTING: Final = "overwrite_existing"
ATTR_INITIAL_COST: Final = "initial_cost"

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

# import_backfill response keys (preview vocabulary above is reused).
ATTR_ROWS_WRITTEN: Final = "rows_written"
ATTR_EXISTING_ROWS_KEPT: Final = "existing_rows_kept"

# calibrate_cost request field.
ATTR_VALUE: Final = "value"

# calibrate_cost receipt keys.
ATTR_OLD_COST: Final = "old_cost"
ATTR_NEW_COST: Final = "new_cost"
ATTR_OLD_BASELINE_KWH: Final = "old_baseline_kwh"
ATTR_NEW_BASELINE_KWH: Final = "new_baseline_kwh"

RANGE_CAP: Final = 10
"""Maximum contiguous ranges listed per finding; the count field is exact."""
