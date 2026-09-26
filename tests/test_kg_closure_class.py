"""A closure must be able to say by WHAT STANDARD it closed.

`status` is two-valued over a three-valued world. A discovery is open or it is
closed, and the state a reconciler keeps meeting is neither — "not currently
observed, cause unknown". Forced to pick, everyone picks the one that shortens
the queue, and `status='resolved'` then records no standard at all.

The cost is not local. A reader cannot distinguish a closure resting on a
deployed fix with positively observed effect from one resting on a correlation
with a date, so a weak closure dilutes what "resolved" means graph-wide,
retroactively, including entries other agents closed rigorously.

The two required-evidence rules below are not generic diligence prompts. Each
encodes a specific way a closure went wrong on 2026-08-19:

  fix_verified requires `observed` because a closure claimed a verified fix on
  evidence that the old symptom was gone. The subject-keying half of the repair
  had never been exercised live.

  unobserved requires `instrument_check` because a closure concluded a condition
  had ceased from a sibling signal still arriving. Siblings share the sink, not
  the emitter.
"""

import pytest

from src.mcp_handlers.knowledge.handlers import (  # type: ignore
    CLOSURE_CLASSES,
    _KnowledgeUpdateRequest,
    _UpdateResponseError,
    _validate_closure_class,
)


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
        "closure_class": None,
        "closure_evidence": None,
    }
    fields.update(overrides)
    return _KnowledgeUpdateRequest(**fields)


class TestNonBreaking:
    """A required field would break the KG gardener's mechanical auto-resolve."""

    def test_absent_class_is_accepted(self):
        _validate_closure_class(_request(), "resolved")

    def test_absent_class_is_accepted_on_every_closing_status(self):
        for status in ("resolved", "closed", "wont_fix", "superseded"):
            _validate_closure_class(_request(), status)


class TestVocabulary:
    def test_unknown_class_is_refused(self):
        with pytest.raises(_UpdateResponseError):
            _validate_closure_class(_request(closure_class="probably_fine"), "resolved")

    @pytest.mark.parametrize(
        "cls", sorted(CLOSURE_CLASSES - {"fix_verified", "unobserved"})
    )
    def test_classes_without_required_evidence_pass_bare(self, cls):
        _validate_closure_class(_request(closure_class=cls), "resolved")

    def test_a_standard_cannot_be_declared_for_a_closure_that_is_not_happening(self):
        """closure_class on status='open' is a contradiction, not a hint."""
        with pytest.raises(_UpdateResponseError):
            _validate_closure_class(_request(closure_class="obsolete"), "open")


class TestFixVerifiedRequiresPositiveObservation:
    """The failure: claiming a verified fix because the old symptom is gone."""

    def test_bare_fix_verified_is_refused(self):
        with pytest.raises(_UpdateResponseError):
            _validate_closure_class(_request(closure_class="fix_verified"), "resolved")

    def test_deployed_without_observed_is_refused(self):
        """A merged-and-deployed PR is half the claim. It is not the effect."""
        with pytest.raises(_UpdateResponseError):
            _validate_closure_class(
                _request(
                    closure_class="fix_verified",
                    closure_evidence={"deployed": "dd8c0d74 is in build 377c8687"},
                ),
                "resolved",
            )

    def test_both_keys_pass(self):
        _validate_closure_class(
            _request(
                closure_class="fix_verified",
                closure_evidence={
                    "deployed": "dd8c0d74 ancestor of running build_sha 377c8687",
                    "observed": "new fingerprint tracks the event-type set across repeats",
                },
            ),
            "resolved",
        )

    def test_whitespace_is_not_evidence(self):
        with pytest.raises(_UpdateResponseError):
            _validate_closure_class(
                _request(
                    closure_class="fix_verified",
                    closure_evidence={"deployed": "x", "observed": "   "},
                ),
                "resolved",
            )

    def test_evidence_must_be_an_object(self):
        with pytest.raises(_UpdateResponseError):
            _validate_closure_class(
                _request(
                    closure_class="fix_verified",
                    closure_evidence="deployed and observed, trust me",
                ),
                "resolved",
            )


class TestUnobservedRequiresAnInstrumentCheck:
    """The failure: concluding a condition ceased from a sibling still arriving."""

    def test_bare_unobserved_is_refused(self):
        with pytest.raises(_UpdateResponseError):
            _validate_closure_class(_request(closure_class="unobserved"), "resolved")

    def test_window_alone_is_refused(self):
        """A date range with no liveness check is the exact 2026-08-19 error."""
        with pytest.raises(_UpdateResponseError):
            _validate_closure_class(
                _request(
                    closure_class="unobserved",
                    closure_evidence={"window": "zero rows since week of 2026-07-06"},
                ),
                "resolved",
            )

    def test_both_keys_pass(self):
        _validate_closure_class(
            _request(
                closure_class="unobserved",
                closure_evidence={
                    "window": "zero rows since week of 2026-07-06",
                    "instrument_check": (
                        "total failing-row volume dropped with it rather than "
                        "staying flat, so the rows were not relabelled"
                    ),
                },
            ),
            "resolved",
        )


class TestTheHintNamesTheTrap:
    """The recovery text has to teach the distinction, not just list keys."""

    def _hint(self, cls, status="resolved"):
        try:
            _validate_closure_class(_request(closure_class=cls), status)
        except _UpdateResponseError as exc:
            return str(exc.response.text)
        pytest.fail("expected a refusal")

    def test_fix_verified_hint_rejects_absence_as_observation(self):
        assert "not an observation" in self._hint("fix_verified")

    def test_unobserved_hint_names_the_sibling_fallacy(self):
        hint = self._hint("unobserved")
        assert "sink" in hint and "emitter" in hint


# ---------------------------------------------------------------------------
# Where a class may sit, now that it is stored (migration 071)
# ---------------------------------------------------------------------------

from src.mcp_handlers.knowledge import handlers as kg_handlers  # noqa: E402
from src.mcp_handlers.knowledge.handlers import (  # noqa: E402
    _build_discovery_updates,
    _parse_knowledge_update_request,
)
from src.knowledge_graph import DiscoveryNode  # noqa: E402


def _stored(**overrides):
    fields = dict(id="d-1", agent_id="a-1", type="bug_found", summary="s", status="open")
    fields.update(overrides)
    return DiscoveryNode(**fields)


def _refusal(exc) -> str:
    import json

    return json.loads(exc.value.response.text)["error"]


class TestTheClassSurvivesRetention:
    """archived and cold are retention tiers, not reopenings."""

    @pytest.mark.parametrize("status", ["archived", "cold"])
    def test_a_class_is_admitted_on_a_retention_status(self, status):
        _validate_closure_class(_request(closure_class="obsolete"), status)

    @pytest.mark.parametrize("status", ["open", "disputed"])
    def test_a_class_is_refused_on_a_reopening_status(self, status):
        with pytest.raises(_UpdateResponseError) as refused:
            _validate_closure_class(_request(closure_class="obsolete"), status)
        assert f"status='{status}'" in _refusal(refused)


class TestAClassWithoutAStatusIsJudgedAgainstTheStoredOne:
    """Without this check, a status-less class sent to an open row would reach
    storage now that storage writes it; the constraint would refuse it there,
    and the AGE backend would report the refusal as "Discovery not found"."""

    @pytest.mark.parametrize("stored", ["resolved", "closed", "wont_fix", "superseded", "archived", "cold"])
    def test_a_closed_row_takes_a_class_alone(self, stored):
        _validate_closure_class(_request(closure_class="duplicate"), None, stored)

    @pytest.mark.parametrize("stored", ["open", "disputed"])
    def test_an_open_row_refuses_a_class_alone(self, stored):
        with pytest.raises(_UpdateResponseError) as refused:
            _validate_closure_class(_request(closure_class="duplicate"), None, stored)
        message = _refusal(refused)
        assert f"is '{stored}'" in message and "sets no status" in message

    def test_the_status_being_set_wins_over_the_stored_one(self):
        # Closing an open row and classifying it in one call is the normal path.
        _validate_closure_class(_request(closure_class="duplicate"), "resolved", "open")
        with pytest.raises(_UpdateResponseError):
            _validate_closure_class(_request(closure_class="duplicate"), "open", "resolved")

    def test_a_class_alone_is_an_updatable_field(self):
        request = _parse_knowledge_update_request(
            {"discovery_id": "d-1", "closure_class": "duplicate"}
        )
        updates, status = _build_discovery_updates(request, _stored(status="resolved"))
        assert status is None and "status" not in updates
        assert updates["closure_class"] == "duplicate"


class TestEvidenceTravelsWithItsClass:
    def test_evidence_without_a_class_is_refused_not_dropped(self):
        with pytest.raises(_UpdateResponseError) as refused:
            _validate_closure_class(
                _request(closure_evidence={"deployed": "x", "observed": "y"}), "resolved"
            )
        assert "closure_class" in _refusal(refused)

    def test_evidence_must_be_an_object_for_every_class(self):
        with pytest.raises(_UpdateResponseError):
            _validate_closure_class(
                _request(closure_class="duplicate", closure_evidence="see d-0"), "resolved"
            )

    def test_changing_the_class_replaces_the_evidence(self):
        """A class that needs none must not inherit the last class's evidence."""
        request = _parse_knowledge_update_request(
            {"discovery_id": "d-1", "status": "resolved", "closure_class": "duplicate"}
        )
        updates, _ = _build_discovery_updates(
            request,
            _stored(
                status="resolved",
                closure_class="fix_verified",
                closure_evidence={"deployed": "x", "observed": "y"},
            ),
        )
        assert updates["closure_class"] == "duplicate"
        assert "closure_evidence" in updates and updates["closure_evidence"] is None


class TestReopeningClearsTheClass:
    @pytest.mark.parametrize("status", ["open", "disputed"])
    def test_a_reopening_update_clears_both_fields(self, status):
        request = _parse_knowledge_update_request({"discovery_id": "d-1", "status": status})
        updates, _ = _build_discovery_updates(
            request, _stored(status="resolved", closure_class="duplicate")
        )
        assert updates["closure_class"] is None
        assert updates["closure_evidence"] is None

    @pytest.mark.parametrize("status", ["resolved", "closed", "archived"])
    def test_a_non_reopening_update_leaves_the_class_alone(self, status):
        request = _parse_knowledge_update_request({"discovery_id": "d-1", "status": status})
        updates, _ = _build_discovery_updates(
            request, _stored(status="resolved", closure_class="duplicate")
        )
        assert "closure_class" not in updates and "closure_evidence" not in updates


def test_the_handler_admits_a_class_exactly_where_the_schema_does():
    """_CLASS_ADMITTING_STATUSES and migration 071's CHECK name one set."""
    import re
    from pathlib import Path

    sql = (
        Path(__file__).resolve().parent.parent
        / "db/postgres/migrations/071_knowledge_closure_class_survives_tiering.sql"
    ).read_text()
    check = sql.split("ADD CONSTRAINT discoveries_closure_class_requires_closed", 1)[1]
    check = check.split(";", 1)[0]
    in_list = set(re.findall(r"'(\w+)'", check))
    assert in_list == set(kg_handlers._CLASS_ADMITTING_STATUSES)


# ---------------------------------------------------------------------------
# Through the real handler
# ---------------------------------------------------------------------------

from unittest.mock import AsyncMock, MagicMock, patch  # noqa: E402

from tests.helpers import parse_result  # noqa: E402


@pytest.fixture
def graph():
    server = MagicMock()
    server.agent_metadata = {}
    graph = AsyncMock()
    graph.update_discovery = AsyncMock(return_value=True)
    with (
        patch("src.mcp_handlers.context.get_context_agent_id", return_value=None),
        patch("src.mcp_handlers.shared.get_mcp_server", return_value=server),
        patch("src.mcp_handlers.knowledge.handlers.mcp_server", server),
        patch(
            "src.mcp_handlers.knowledge.handlers.get_knowledge_graph",
            new_callable=AsyncMock,
            return_value=graph,
        ),
    ):
        yield graph


@pytest.mark.asyncio
async def test_a_class_sent_to_an_open_row_is_refused_before_storage(graph):
    graph.get_discovery = AsyncMock(return_value=_stored(status="open"))
    data = parse_result(
        await kg_handlers.handle_update_discovery_status_graph(
            {"agent_id": "a-1", "discovery_id": "d-1", "closure_class": "duplicate"}
        )
    )
    assert data["success"] is False
    assert data["error_code"] == "INVALID_PARAM"
    assert "not found" not in data["error"].lower()
    graph.update_discovery.assert_not_awaited()


@pytest.mark.asyncio
async def test_a_non_owner_classifies_only_in_the_closing_call(graph):
    """Same rule as resolution_notes on a high-severity finding."""
    graph.get_discovery = AsyncMock(
        return_value=_stored(agent_id="owner-a", severity="high", status="resolved")
    )
    with (
        patch(
            "src.mcp_handlers.knowledge.handlers.require_registered_agent",
            return_value=("closer-b", None),
        ),
        patch("src.mcp_handlers.utils.verify_agent_ownership", return_value=True),
    ):
        alone = parse_result(
            await kg_handlers.handle_update_discovery_status_graph(
                {"discovery_id": "d-1", "closure_class": "duplicate"}
            )
        )
        with_close = parse_result(
            await kg_handlers.handle_update_discovery_status_graph(
                {"discovery_id": "d-1", "status": "closed", "closure_class": "duplicate"}
            )
        )
    assert alone["success"] is False
    assert "closure_class" in alone["error"]
    assert with_close["success"] is True, with_close
    assert graph.update_discovery.await_args.args[1]["closure_class"] == "duplicate"
