"""Regression tests for apply_param_aliases collision handling.

The original one-pass implementation rewrote canonical keys with aliased
values when both were present, depending on dict iteration order. A
2026-05-09 leave_note call with summary=... and content=... saw content
overwrite summary, dropping the actual summary text and corrupting the
KG record. These tests pin the non-destructive collision policy.
"""

import logging

from src.mcp_handlers.validators import apply_param_aliases


def test_unknown_tool_returns_arguments_unchanged():
    args = {"foo": 1, "bar": 2}
    out = apply_param_aliases("not_a_tool", args)
    assert out is args  # passthrough preserves identity


def test_alias_only_is_renamed_to_canonical():
    out = apply_param_aliases("leave_note", {"text": "hello"})
    assert out == {"summary": "hello"}


def test_canonical_only_passes_through():
    out = apply_param_aliases("leave_note", {"summary": "hello"})
    assert out == {"summary": "hello"}


def test_alias_and_canonical_both_present_canonical_wins():
    out = apply_param_aliases(
        "leave_note", {"summary": "real-summary", "content": "extended-body"}
    )
    assert out == {"summary": "real-summary"}


def test_collision_order_independent_canonical_first():
    # Insertion order: canonical first
    out = apply_param_aliases(
        "leave_note", {"summary": "real", "content": "should-drop"}
    )
    assert out["summary"] == "real"


def test_collision_order_independent_alias_first():
    # Insertion order: alias first
    out = apply_param_aliases(
        "leave_note", {"content": "should-drop", "summary": "real"}
    )
    assert out["summary"] == "real"


def test_collision_emits_warning(caplog):
    with caplog.at_level(logging.WARNING, logger="src.mcp_handlers.validators"):
        apply_param_aliases(
            "leave_note", {"summary": "kept", "content": "dropped"}
        )
    assert any(
        "alias 'content' collides with canonical 'summary'" in rec.getMessage()
        for rec in caplog.records
    )


def test_equal_values_no_warning(caplog):
    with caplog.at_level(logging.WARNING, logger="src.mcp_handlers.validators"):
        apply_param_aliases(
            "leave_note", {"summary": "same", "content": "same"}
        )
    assert not [
        r for r in caplog.records if "collides" in r.getMessage()
    ]


def test_store_knowledge_content_alias_for_details_does_not_clobber():
    # store_knowledge_graph aliases content -> details; both present means
    # details (canonical) must win.
    out = apply_param_aliases(
        "store_knowledge_graph",
        {"summary": "s", "details": "real-details", "content": "extra"},
    )
    assert out["details"] == "real-details"
    assert out["summary"] == "s"


def test_process_agent_update_summary_alias_for_response_text():
    # process_agent_update aliases summary -> response_text. Both present:
    # response_text wins.
    out = apply_param_aliases(
        "process_agent_update",
        {"response_text": "real", "summary": "should-drop"},
    )
    assert out == {"response_text": "real"}


def test_unrelated_keys_pass_through():
    out = apply_param_aliases(
        "leave_note",
        {"summary": "s", "tags": ["a", "b"], "agent_id": "x"},
    )
    assert out == {"summary": "s", "tags": ["a", "b"], "agent_id": "x"}


def test_multiple_aliases_to_same_canonical_first_wins():
    # leave_note has many aliases all mapping to summary. If two are
    # supplied without canonical, first encountered wins; later ones
    # are dropped (whichever path: collision against earlier-set canonical).
    out = apply_param_aliases(
        "leave_note", {"text": "first", "note": "second"}
    )
    assert out == {"summary": "first"}
