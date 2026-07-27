"""Shared fixtures for the Appliance Energy Cost tests."""

from __future__ import annotations

import pytest
from homeassistant.components.recorder import Recorder


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(
    recorder_mock: Recorder, enable_custom_integrations: None
) -> None:
    """Enable custom integrations with a fake recorder in all integration tests.

    The manifest declares a hard dependency on recorder (the backfill reads
    and writes long-term statistics), so loading the integration needs the
    recorder component set up. ``recorder_mock`` must be listed before
    ``enable_custom_integrations`` (and anything else that boots ``hass``):
    the recorder database fixture asserts it initialises first (see
    pytest-homeassistant-custom-component issue #132).
    """
