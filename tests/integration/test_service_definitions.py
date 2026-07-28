"""Pin the service-definition invariants of issue #42, mechanically.

Two of these are verdict-load-bearing (the stress-test's amendment 9):
(a) services.yaml's ``default: true`` on overwrite_existing / calibrate is a
UI prefill only — the vol schema defaults stay False (pinned behaviourally
in the import tests); (b) ``confirm`` keeps ``default: false`` and stays
LAST in the field order. YAML field order is the UI field order, and
strings.json must equal translations/en.json byte for byte.
"""

from __future__ import annotations

import json
from pathlib import Path

import yaml

_COMPONENT = Path(__file__).parents[2] / "custom_components" / "appliance_energy_cost"

_IMPORT_FIELD_ORDER = [
    "config_entry",
    "start",
    "end",
    "appliances",
    "overwrite_existing",
    "calibrate",
    "initial_cost",
    "strict",
    "confirm",
]


def _services_yaml() -> dict[str, dict[str, dict[str, dict[str, object]]]]:
    document = yaml.safe_load((_COMPONENT / "services.yaml").read_text())
    assert isinstance(document, dict)
    return document


def test_import_backfill_field_order_and_ui_defaults() -> None:
    """The owner-dictated field order, prefill-true flags, confirm last+false."""
    fields = _services_yaml()["import_backfill"]["fields"]
    assert list(fields) == _IMPORT_FIELD_ORDER
    assert fields["overwrite_existing"]["default"] is True
    assert fields["calibrate"]["default"] is True
    # INVARIANT: collapsing either of these guts the confirm gate.
    assert fields["confirm"]["default"] is False
    assert list(fields)[-1] == "confirm"


def test_preview_backfill_field_order_and_optional_start() -> None:
    """Preview keeps the shared prefix of the field order; start is optional."""
    fields = _services_yaml()["preview_backfill"]["fields"]
    assert list(fields) == ["config_entry", "start", "end", "appliances", "strict"]
    assert fields["start"]["required"] is False


def test_import_start_is_optional_in_yaml_too() -> None:
    fields = _services_yaml()["import_backfill"]["fields"]
    assert fields["start"]["required"] is False


def test_strings_json_equals_en_translations_byte_for_byte() -> None:
    """hassfest requires the pair to agree; byte equality leaves no room."""
    strings = (_COMPONENT / "strings.json").read_bytes()
    en = (_COMPONENT / "translations" / "en.json").read_bytes()
    assert strings == en


def test_action_names_and_new_translation_keys_in_both_languages() -> None:
    """The issue #42 action names, and the new keys present in en AND fi."""
    en = json.loads((_COMPONENT / "strings.json").read_text())
    fi = json.loads((_COMPONENT / "translations" / "fi.json").read_text())
    assert en["services"]["preview_backfill"]["name"] == "Preview energy cost backfill"
    assert en["services"]["import_backfill"]["name"] == "Import energy cost backfill"
    assert en["services"]["calibrate_cost"]["name"] == "Calibrate cost sensor"
    for language in (en, fi):
        assert "calibrate" in language["services"]["import_backfill"]["fields"]
        assert "price_history_begins_now" in language["exceptions"]
        assert "initial_cost_not_finite" in language["exceptions"]
