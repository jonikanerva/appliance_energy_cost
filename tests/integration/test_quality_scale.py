"""Enforce the quality_scale.yaml contract.

This test is the file's only enforcement: hassfest validates
quality_scale.yaml for core integrations only, not custom ones (verified
in issue #1), so a malformed status or an unjustified exemption would
otherwise go unnoticed.
"""

from __future__ import annotations

from pathlib import Path

import yaml

_QUALITY_SCALE = (
    Path(__file__).parents[2] / "custom_components" / "appliance_energy_cost" / "quality_scale.yaml"
)

_STATUSES = ("done", "todo", "exempt")


def test_statuses_are_valid_and_every_exemption_is_justified() -> None:
    """Every rule is done/todo/exempt; every exempt carries a non-empty comment."""
    document = yaml.safe_load(_QUALITY_SCALE.read_text())
    rules = document["rules"]
    assert isinstance(rules, dict)
    assert rules
    for rule, status in rules.items():
        if isinstance(status, str):
            # Bare-string form is reserved for done/todo; exempt always
            # needs the mapping form to carry its reason.
            assert status in ("done", "todo"), rule
            continue
        assert isinstance(status, dict), rule
        assert status["status"] in _STATUSES, rule
        if status["status"] == "exempt":
            comment = status.get("comment")
            assert isinstance(comment, str), rule
            assert comment.strip(), rule
