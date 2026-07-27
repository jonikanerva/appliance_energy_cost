"""Enforce the quality_scale.yaml contract.

This test is the file's only enforcement: hassfest validates
quality_scale.yaml for core integrations only, not custom ones (verified
in issue #1), so a malformed status, an unjustified exemption, or a
deferred item without a tracking issue would otherwise go unnoticed.

All assertions run against the PARSED document, never the raw text: a
plain YAML scalar silently swallows a trailing ` #N` as a comment, so
only the parsed values prove what a machine consumer actually reads
(PR #24 round-1 finding).
"""

from __future__ import annotations

import re
from pathlib import Path

import yaml

_QUALITY_SCALE = (
    Path(__file__).parents[2] / "custom_components" / "appliance_energy_cost" / "quality_scale.yaml"
)

_STATUSES = ("done", "todo", "exempt")

_ISSUE_REFERENCE = re.compile(r"#\d+")


def test_statuses_are_valid_and_deferrals_are_justified() -> None:
    """Every rule is done/todo/exempt; every deferral carries its evidence.

    An exempt row needs a non-empty reason, and a todo row's comment must
    reference its tracking GitHub issue (``#<N>``) — deferred work must
    not die in an untracked half-sentence (CLAUDE.md → Audit trail).
    """
    document = yaml.safe_load(_QUALITY_SCALE.read_text())
    rules = document["rules"]
    assert isinstance(rules, dict)
    assert rules
    for rule, status in rules.items():
        if isinstance(status, str):
            # Bare-string form is reserved for done: todo needs a tracking
            # issue and exempt needs a reason, both in mapping form.
            assert status == "done", rule
            continue
        assert isinstance(status, dict), rule
        assert status["status"] in _STATUSES, rule
        if status["status"] == "exempt":
            comment = status.get("comment")
            assert isinstance(comment, str), rule
            assert comment.strip(), rule
        if status["status"] == "todo":
            comment = status.get("comment")
            assert isinstance(comment, str), rule
            assert _ISSUE_REFERENCE.search(comment), (
                f"{rule}: todo comment lacks a #<issue> reference in the parsed value"
            )
