"""A non-owner may ANNOTATE a high-severity discovery without closing it.

The old rule attached `resolution_notes` to the closing transition: a non-owner
could append a note only if the same call also moved status to resolved /
closed / wont_fix. So anyone reconciling someone else's entry had two options,
close it or leave it wrong, and there was no third.

That is a bad trade exactly when an entry is still true as a class record and
only its instance lines have gone stale. Observed 2026-08-19: two
constraint-drift instances sat marked "STILL OPEN" and "NOT yet applied to
prod" for days after both were repaired, because correcting them would have
required closing a pattern entry that remains correct.

Widening is safe because appends are additive and now carry the writer's id.
The fields that can rewrite an author's meaning stay owner-only.
"""

import pytest

from src.mcp_handlers.knowledge.handlers import (  # type: ignore
    _apply_update_text_fields,
    _requested_non_owner_edits,
    _KnowledgeUpdateRequest,
)

ALLOWED = {"resolved", "closed", "wont_fix"}


def _request(**overrides):
    fields = {
        "arguments": {"discovery_id": "d-1"},
        "discovery_id": "d-1",
        "status": None,
        "details": None,
        "resolution_note": None,
        "summary": None,
        "severity": None,
        "discovery_type": None,
        "tags": None,
        "superseded_by": None,
    }
    fields.update(overrides)
    return _KnowledgeUpdateRequest(**fields)


class _Discovery:
    def __init__(self, agent_id="owner-uuid", details="prior body"):
        self.agent_id = agent_id
        self.details = details


class TestNonOwnerMayAnnotateWithoutClosing:
    def test_note_without_status_is_no_longer_refused(self):
        """The whole point: annotate and leave the entry open."""
        assert _requested_non_owner_edits(
            _request(resolution_note="both instances are repaired in prod"),
            ALLOWED,
        ) == []

    def test_note_while_leaving_status_open_is_allowed(self):
        """status='open' is not in ALLOWED, and must no longer trip the gate."""
        assert _requested_non_owner_edits(
            _request(resolution_note="still true as a class record", status="open"),
            ALLOWED,
        ) == []

    def test_note_alongside_a_close_still_works(self):
        """The path that always worked must keep working."""
        assert _requested_non_owner_edits(
            _request(resolution_note="verified fixed", status="resolved"),
            ALLOWED,
        ) == []

    @pytest.mark.parametrize(
        "field,value",
        [
            ("summary", "rewritten"),
            ("details", "replaced wholesale"),
            ("severity", "low"),
            ("discovery_type", "note"),
            ("tags", ["retagged"]),
        ],
    )
    def test_fields_that_rewrite_the_authors_meaning_stay_owner_only(self, field, value):
        """Appending is additive; these are destructive. They stay refused."""
        assert _requested_non_owner_edits(_request(**{field: value}), ALLOWED) == [
            "details/content" if field == "details" else field
        ]


class TestAppendedNotesCarryTheirWriter:
    """Attribution is what makes the widening safe — without it a non-owner's
    annotation is indistinguishable from the author's own."""

    def test_non_owner_note_carries_a_byline(self):
        updates: dict = {}
        _apply_update_text_fields(
            _request(resolution_note="verified against prod"),
            _Discovery(agent_id="owner-uuid"),
            updates,
            writer_agent_id="other-uuid",
            writer_is_owner=False,
        )
        assert ", by other-uuid" in updates["details"]
        assert "verified against prod" in updates["details"]

    def test_owner_note_is_unmarked(self):
        """The common case reads exactly as it did before this change."""
        updates: dict = {}
        _apply_update_text_fields(
            _request(resolution_note="closing my own finding"),
            _Discovery(agent_id="owner-uuid"),
            updates,
            writer_agent_id="owner-uuid",
            writer_is_owner=True,
        )
        assert ", by " not in updates["details"]
        assert "Resolution notes (" in updates["details"]

    def test_append_never_destroys_the_prior_body(self):
        updates: dict = {}
        _apply_update_text_fields(
            _request(resolution_note="an annotation"),
            _Discovery(details="the original finding text"),
            updates,
            writer_agent_id="other-uuid",
            writer_is_owner=False,
        )
        assert updates["details"].startswith("the original finding text")
        assert "an annotation" in updates["details"]

    def test_unknown_writer_defaults_to_unmarked_rather_than_lying(self):
        """No writer resolved => no byline, rather than a fabricated one."""
        updates: dict = {}
        _apply_update_text_fields(
            _request(resolution_note="n"), _Discovery(), updates
        )
        assert ", by " not in updates["details"]
