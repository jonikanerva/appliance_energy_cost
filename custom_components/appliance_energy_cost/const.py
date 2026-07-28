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
# Named "calibrate", NOT "calibrate_cost": the field must not collide with
# the calibrate_cost action name (issue #42).
ATTR_CALIBRATE: Final = "calibrate"

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
# Per-appliance calibration outcome, present only when calibrate: true —
# exactly one of the two keys per appliance (calibrated_to XOR
# calibration_skipped).
ATTR_CALIBRATED_TO: Final = "calibrated_to"
ATTR_CALIBRATION_SKIPPED: Final = "calibration_skipped"

# Calibration skip reasons (issue #42): the CLOSED set of values the
# receipt's calibration_skipped field can carry, each plain English with its
# remedy. Deliberately values, not translation keys: service responses are
# never translated, and automations must be able to match them verbatim.
# Every skip is also logged at WARNING; a skip never fails the import — the
# rows are committed and verified before any calibration runs.
SKIP_STALE_HOUR: Final = (
    "the import's end is no longer the current hour, so the energy metered"
    " since end cannot be priced at the consumption-time price; run the same"
    " import call again — overwrite updates the committed rows in place and"
    " the calibration lands within the fresh hour"
)
SKIP_NO_ROWS: Final = (
    "the import wrote no rows for this appliance, so there is no imported"
    " series to continue; check the appliance's statistics or narrow the"
    " period, then calibrate manually if needed"
)
SKIP_NO_END_ENERGY: Final = (
    "the period's last energy row carries no cumulative meter state"
    " (end_energy_kwh is null), so the energy metered since end cannot be"
    " measured; re-run the import once the last hour carries a state, or"
    " calibrate manually"
)
SKIP_ENTITY_UNAVAILABLE: Final = (
    "the live cost sensor was not reachable at the calibration moment (the"
    " entry may be reloading); re-run the same import call again, or"
    " calibrate manually"
)
SKIP_PRICE_GAP: Final = (
    "a price gap is active, so the energy metered since the import's end"
    " cannot be priced; wait for a usable price, then calibrate manually or"
    " re-run the import"
)
SKIP_READING_UNUSABLE: Final = (
    "the energy sensor has no usable cumulative reading at the calibration"
    " moment; wait until it reports a numeric reading, then calibrate"
    " manually or re-run the import"
)
SKIP_READING_NEGATIVE: Final = (
    "the energy sensor reports a negative cumulative reading, which must"
    " never become a baseline; fix the source, then calibrate manually or"
    " re-run the import"
)
SKIP_METER_DIP: Final = (
    "meter reading is below the import's end reading but not a reset —"
    " likely a sensor dip; nothing calibrated; wait for recovery, then"
    " calibrate manually or re-run the import"
)
SKIP_VALUE_NOT_FINITE: Final = (
    "the computed calibration value is not a finite number; nothing"
    " calibrated — check the receipt's totals and calibrate manually"
)
SKIP_CALIBRATION_FAILED: Final = (
    "the calibration raised an error after the rows were committed (see the"
    " Home Assistant log); the imported rows are intact — calibrate manually"
    " or re-run the import"
)

# calibrate_cost request field.
ATTR_VALUE: Final = "value"

# calibrate_cost receipt keys.
ATTR_OLD_COST: Final = "old_cost"
ATTR_NEW_COST: Final = "new_cost"
ATTR_OLD_BASELINE_KWH: Final = "old_baseline_kwh"
ATTR_NEW_BASELINE_KWH: Final = "new_baseline_kwh"

RANGE_CAP: Final = 10
"""Maximum contiguous ranges listed per finding; the count field is exact."""
