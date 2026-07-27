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
