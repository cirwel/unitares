"""knowledge(action=search) can return the newest entries for a query.

The failure this guards, from a 2026-09-26 session: an agent searched the KG,
saw only August rows, and concluded nothing had been written for three days.
A matching entry from that morning existed; it ranked below the page. Relevance
order was the only order on offer, because `sort_by` and `created_after` were
declared on the legacy search schema, absent from the unified `knowledge`
schema (which strips undeclared fields), and read by no handler.

The contract under test: with sort_by=created_at the query's full-text matches
come back newest first, however weakly the newest one ranks. That has to be
true in SQL, not by re-sorting a relevance page, so the integration tests below
seed a match that ranks LAST and ask for one result.

Integration tests use the real governance_test database (the
`live_postgres_backend` fixture) and skip when it is unavailable.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import pytest_asyncio

from src.knowledge_graph import DiscoveryNode
from src.mcp_handlers.knowledge import handlers
from src.mcp_handlers.knowledge.handlers import (
    _SearchParameterError,
    _parse_knowledge_search_request,
    _within_window,
)

NOW = datetime.now(timezone.utc)


def _node(id_suffix: str, *, age: timedelta, summary: str, details: str = "", tags=None):
    created = NOW - age
    return DiscoveryNode(
        id=f"{created.isoformat()}-{id_suffix}",
        agent_id="recency-test-agent",
        type="note",
        summary=summary,
        details=details,
        tags=tags or ["recency-test"],
        timestamp=created.isoformat(),
    )


# The old row saturates the query terms and wins on rank; the fresh row
# mentions them once, deep in a long body, and ranks last. A row newer still
# does not match at all and must never appear.
OLD_STRONG = _node(
    "old",
    age=timedelta(days=40),
    summary="coherence gate: coherence gate soak, coherence gate verdict",
    details="coherence gate " * 20,
)
MID = _node(
    "mid",
    age=timedelta(days=10),
    summary="coherence gate check after the restart",
)
NEW_WEAK = _node(
    "new",
    age=timedelta(minutes=2),
    summary="Orchestrator notes from this morning's restart window",
    details=("unrelated operational narrative " * 30) + "the coherence of the gate held",
)
NEWEST_UNRELATED = _node(
    "unrelated",
    age=timedelta(seconds=10),
    summary="Dashboard overview redesign landed",
)
SEEDED = (OLD_STRONG, MID, NEW_WEAK, NEWEST_UNRELATED)


@pytest_asyncio.fixture
async def seeded_db(live_postgres_backend):
    for node in SEEDED:
        await live_postgres_backend.kg_add_discovery(node)
    return live_postgres_backend


def _pg_graph(db):
    from src.storage.knowledge_graph_postgres import KnowledgeGraphPostgres

    graph = KnowledgeGraphPostgres()
    graph._get_db = AsyncMock(return_value=db)
    return graph


def _display_by_agent(agent_id):
    return {"display_name": agent_id}


async def _search(db, **arguments):
    graph = _pg_graph(db)
    with patch.object(handlers, "get_knowledge_graph", AsyncMock(return_value=graph)), \
         patch.object(handlers, "_broadcast_knowledge_read", AsyncMock()), \
         patch.object(handlers, "_resolve_agent_display", MagicMock(side_effect=_display_by_agent)):
        result = await handlers.handle_search_knowledge_graph(arguments)
    return json.loads(result[0].text)


def _ids(payload):
    return [d["id"] for d in payload["discoveries"]]


# ---------------------------------------------------------------------------
# SQL layer
# ---------------------------------------------------------------------------


class TestFullTextSearchOrder:
    @pytest.mark.asyncio
    async def test_rank_order_buries_the_fresh_match(self, seeded_db):
        # The premise. If this fails the fixture no longer builds the contrast
        # and the newest-first tests below prove nothing.
        rows = await seeded_db.kg_full_text_search("coherence gate", limit=3)
        assert [r["id"] for r in rows] == [OLD_STRONG.id, MID.id, NEW_WEAK.id]
        rows = await seeded_db.kg_full_text_search("coherence gate", limit=1)
        assert [r["id"] for r in rows] == [OLD_STRONG.id]

    @pytest.mark.asyncio
    async def test_created_at_order_returns_newest_match_at_limit_one(self, seeded_db):
        rows = await seeded_db.kg_full_text_search(
            "coherence gate", limit=1, order_by="created_at"
        )
        assert [r["id"] for r in rows] == [NEW_WEAK.id]

    @pytest.mark.asyncio
    async def test_created_at_order_keeps_the_query_as_the_filter(self, seeded_db):
        rows = await seeded_db.kg_full_text_search(
            "coherence gate", limit=10, order_by="created_at"
        )
        assert [r["id"] for r in rows] == [NEW_WEAK.id, MID.id, OLD_STRONG.id]
        assert NEWEST_UNRELATED.id not in [r["id"] for r in rows]

    @pytest.mark.asyncio
    async def test_window_is_applied_inside_the_query(self, seeded_db):
        rows = await seeded_db.kg_full_text_search(
            "coherence gate", limit=1, created_after=NOW - timedelta(days=20)
        )
        # Rank order, but the 40-day row is outside the window, so the next
        # best match is returned rather than an empty page.
        assert [r["id"] for r in rows] == [MID.id]
        rows = await seeded_db.kg_full_text_search(
            "coherence gate", limit=10, created_before=NOW - timedelta(days=20)
        )
        assert [r["id"] for r in rows] == [OLD_STRONG.id]

    @pytest.mark.asyncio
    async def test_unknown_order_is_refused(self, seeded_db):
        with pytest.raises(ValueError, match="order_by"):
            await seeded_db.kg_full_text_search("coherence", order_by="score")


class TestKgQueryWindow:
    @pytest.mark.asyncio
    async def test_created_after_is_newest_first(self, seeded_db):
        rows = await seeded_db.kg_query(created_after=NOW - timedelta(days=1))
        assert [r["id"] for r in rows] == [NEWEST_UNRELATED.id, NEW_WEAK.id]

    @pytest.mark.asyncio
    async def test_created_before(self, seeded_db):
        rows = await seeded_db.kg_query(created_before=NOW - timedelta(days=1))
        assert [r["id"] for r in rows] == [MID.id, OLD_STRONG.id]

    @pytest.mark.asyncio
    async def test_an_iso_string_bound_is_refused_by_the_driver(self, seeded_db):
        # Why the temporal narrator must pass a datetime: this is the error it
        # swallowed at debug level for as long as it passed .isoformat().
        import asyncpg

        with pytest.raises(asyncpg.DataError):
            await seeded_db.kg_query(created_after=(NOW - timedelta(days=1)).isoformat())


# ---------------------------------------------------------------------------
# Handler, end to end over the Postgres backend
# ---------------------------------------------------------------------------


class TestSearchHandlerNewestFirst:
    @pytest.mark.asyncio
    async def test_default_order_is_still_relevance(self, seeded_db):
        payload = await _search(seeded_db, query="coherence gate", limit=1)
        assert _ids(payload) == [OLD_STRONG.id]
        assert "sort_by" not in payload

    @pytest.mark.asyncio
    async def test_entry_written_now_comes_first(self, seeded_db):
        payload = await _search(seeded_db, query="coherence gate", limit=1, sort_by="created_at")
        assert _ids(payload) == [NEW_WEAK.id]
        assert payload["sort_by"] == "created_at"
        assert payload["search_mode_used"] == "fts_newest_first"

    @pytest.mark.asyncio
    async def test_newest_first_list_is_ordered_and_matching_only(self, seeded_db):
        payload = await _search(seeded_db, query="coherence gate", limit=10, sort_by="created_at")
        assert _ids(payload) == [NEW_WEAK.id, MID.id, OLD_STRONG.id]

    @pytest.mark.asyncio
    async def test_newer_ineligible_matches_do_not_empty_the_page(self, seeded_db):
        # Review finding (#2517): filters applied after the SQL LIMIT let a
        # burst of newer archived matches fill the candidate page, so
        # limit=1 came back empty although an active match existed.
        for i in range(8):
            node = _node(
                f"archived-{i}",
                age=timedelta(seconds=i + 1),
                summary=f"coherence gate archived note {i}",
            )
            node.status = "archived"
            await seeded_db.kg_add_discovery(node)
        payload = await _search(seeded_db, query="coherence gate", limit=1, sort_by="created_at")
        assert _ids(payload) == [NEW_WEAK.id]

    @pytest.mark.asyncio
    async def test_type_filter_applies_before_the_limit(self, seeded_db):
        for i in range(8):
            node = _node(f"bug-{i}", age=timedelta(seconds=i + 1), summary=f"coherence gate bug {i}")
            node.type = "bug_found"
            await seeded_db.kg_add_discovery(node)
        payload = await _search(
            seeded_db, query="coherence gate", limit=1, sort_by="created_at", discovery_type="note"
        )
        assert _ids(payload) == [NEW_WEAK.id]

    @pytest.mark.asyncio
    async def test_excluded_writer_does_not_take_the_only_slot(self, seeded_db):
        # Review finding (#2517, round 2): label exclusion ran after the page
        # was cut, so limit=1 came back empty when the newest match was
        # written by an excluded label.
        node = _node("excluded", age=timedelta(seconds=1), summary="coherence gate excluded writer")
        node.agent_id = "excluded-writer"
        await seeded_db.kg_add_discovery(node)
        payload = await _search(
            seeded_db,
            query="coherence gate",
            limit=1,
            sort_by="created_at",
            exclude_agent_labels=["excluded-writer"],
        )
        assert _ids(payload) == [NEW_WEAK.id]

    @pytest.mark.asyncio
    async def test_a_run_of_excluded_writers_longer_than_a_page(self, seeded_db):
        # Review finding (#2517, round 3): a label cannot be filtered in SQL,
        # so more newer excluded rows than one page (limit * 5) used to
        # exhaust the read. Keyset continuation reads the next older page.
        # 30 > the old 5 + 4 * 5 fixed budget (review round 4 on #2517).
        for i in range(30):
            node = _node(f"excluded-{i}", age=timedelta(seconds=i + 1), summary=f"coherence gate excluded {i}")
            node.agent_id = "excluded-writer"
            await seeded_db.kg_add_discovery(node)
        payload = await _search(
            seeded_db,
            query="coherence gate",
            limit=1,
            sort_by="created_at",
            exclude_agent_labels=["excluded-writer"],
        )
        assert _ids(payload) == [NEW_WEAK.id]

    @pytest.mark.asyncio
    async def test_continuation_does_not_skip_rows_sharing_a_timestamp(self, seeded_db):
        # Review on #2517: an exclusive created_at bound skipped unread rows
        # at the page boundary's timestamp. The cursor is (created_at, id).
        stamp = (NOW - timedelta(seconds=1)).isoformat()
        for i in range(6):
            node = _node(f"z-excluded-{i}", age=timedelta(0), summary=f"coherence gate tied {i}")
            node.id, node.timestamp, node.agent_id = f"{stamp}-z{i}", stamp, "excluded-writer"
            await seeded_db.kg_add_discovery(node)
        eligible = _node("a-eligible", age=timedelta(0), summary="coherence gate tied eligible")
        eligible.id, eligible.timestamp = f"{stamp}-a", stamp
        await seeded_db.kg_add_discovery(eligible)
        payload = await _search(
            seeded_db,
            query="coherence gate",
            limit=1,
            sort_by="created_at",
            exclude_agent_labels=["excluded-writer"],
        )
        assert _ids(payload) == [eligible.id]

    @pytest.mark.asyncio
    async def test_continued_rows_count_as_fts_anchored(self, seeded_db):
        # Review on #2517: rows from a continuation page were missing from
        # fts_anchor_ids, so a real lexical hit could read as unanchored.
        for i in range(8):
            node = _node(f"excl-{i}", age=timedelta(seconds=i + 1), summary=f"coherence gate excluded {i}")
            node.agent_id = "excluded-writer"
            await seeded_db.kg_add_discovery(node)
        request = _parse_knowledge_search_request({
            "query": "coherence gate", "limit": 1, "sort_by": "created_at",
            "exclude_agent_labels": ["excluded-writer"],
        })
        state = handlers._KnowledgeSearchState(request=request, graph=_pg_graph(seeded_db))
        with patch.object(handlers, "_resolve_agent_display", MagicMock(side_effect=_display_by_agent)):
            await handlers._run_text_search(state)
        assert [d.id for d in state.results] == [NEW_WEAK.id]
        assert NEW_WEAK.id in state.fts_anchor_ids

    @pytest.mark.asyncio
    async def test_queryless_listing_skips_excluded_writers_before_the_limit(self, seeded_db):
        # Review round 4 on #2517: the queryless read fetched exactly `limit`
        # rows and then dropped excluded writers, so limit=1 came back empty.
        node = _node("excluded-q", age=timedelta(seconds=1), summary="dashboard note by excluded")
        node.agent_id = "excluded-writer"
        await seeded_db.kg_add_discovery(node)
        payload = await _search(
            seeded_db,
            created_after=(NOW - timedelta(days=1)).isoformat(),
            limit=1,
            exclude_agent_labels=["excluded-writer"],
        )
        assert _ids(payload) == [NEWEST_UNRELATED.id]

    @pytest.mark.asyncio
    async def test_queryless_window_is_time_ordered_despite_authority(self, seeded_db):
        # Review finding (#2517, round 3): without an explicit sort_by the
        # queryless listing got the authority nudge, which moves an imported
        # row behind an older native one.
        imported = _node(
            "imported",
            age=timedelta(seconds=1),
            summary="imported memory note",
            tags=["source-claude-memory"],
        )
        await seeded_db.kg_add_discovery(imported)
        payload = await _search(
            seeded_db, created_after=(NOW - timedelta(days=1)).isoformat(), limit=10
        )
        assert _ids(payload)[0] == imported.id
        assert payload["sort_by"] == "created_at"

    @pytest.mark.asyncio
    async def test_what_is_new_since_without_a_query(self, seeded_db):
        since = NOW - timedelta(days=1)
        payload = await _search(seeded_db, created_after=since.isoformat(), limit=10)
        assert _ids(payload) == [NEWEST_UNRELATED.id, NEW_WEAK.id]
        assert payload["created_after"] == since.isoformat()

    @pytest.mark.asyncio
    async def test_window_with_a_query(self, seeded_db):
        payload = await _search(
            seeded_db,
            query="coherence gate",
            limit=10,
            created_after=(NOW - timedelta(days=20)).isoformat(),
        )
        assert OLD_STRONG.id not in _ids(payload)
        assert set(_ids(payload)) == {MID.id, NEW_WEAK.id}

    @pytest.mark.asyncio
    async def test_empty_window_is_an_empty_answer_not_the_whole_graph(self, seeded_db):
        payload = await _search(
            seeded_db, created_after=(NOW + timedelta(days=1)).isoformat(), limit=10
        )
        assert payload["discoveries"] == []


# ---------------------------------------------------------------------------
# The unified router must not strip the new parameters
# ---------------------------------------------------------------------------


class TestUnifiedSchemaCarriesRecencyParams:
    def test_knowledge_params_accept_them(self):
        from src.mcp_handlers.schemas.knowledge import KnowledgeParams

        params = KnowledgeParams(
            action="search",
            query="x",
            sort_by="created_at",
            created_after="2026-09-26T00:00:00Z",
            created_before="2026-09-27",
        )
        dumped = params.model_dump(exclude_none=True)
        assert dumped["sort_by"] == "created_at"
        assert dumped["created_after"] == "2026-09-26T00:00:00Z"
        assert dumped["created_before"] == "2026-09-27"

    def test_declared_as_search_fields(self):
        from src.mcp_handlers.schemas.knowledge import KnowledgeParams

        for name in ("sort_by", "created_after", "created_before"):
            assert name in KnowledgeParams.ACTION_FIELDS["search"]

    def test_legacy_schema_defaults_to_relevance(self):
        # The legacy default was created_at while the field was dead; wiring
        # it with that default would have flipped every legacy search to time
        # order.
        from src.mcp_handlers.schemas.knowledge import SearchKnowledgeGraphParams

        # Unset, which the handler reads as relevance. A materialized
        # "relevance" default would also defeat the queryless-window time
        # order after validation (review on #2517).
        assert SearchKnowledgeGraphParams(query="x").sort_by is None
        dumped = SearchKnowledgeGraphParams(created_after="2026-09-26").model_dump(exclude_none=True)
        assert "sort_by" not in dumped


# ---------------------------------------------------------------------------
# Request parsing
# ---------------------------------------------------------------------------


def test_legacy_validation_dump_keeps_a_queryless_window_time_ordered():
    """The legacy tool's arguments reach the handler as params_step's plain
    model_dump(), defaults included. An omitted sort_by must survive that as
    "unset" so the queryless window still reads as time order."""
    from src.mcp_handlers.schemas.knowledge import SearchKnowledgeGraphParams

    dumped = SearchKnowledgeGraphParams.model_validate(
        {"created_after": "2026-09-26T00:00:00Z"}
    ).model_dump()
    assert dumped["sort_by"] is None
    assert _parse_knowledge_search_request(dumped).sort_by == "created_at"
    explicit = SearchKnowledgeGraphParams.model_validate(
        {"created_after": "2026-09-26T00:00:00Z", "sort_by": "relevance"}
    ).model_dump()
    assert _parse_knowledge_search_request(explicit).sort_by == "relevance"


class TestParseRecencyArguments:
    def test_defaults(self):
        request = _parse_knowledge_search_request({"query": "x"})
        assert request.sort_by == "relevance"
        assert request.created_after is None and request.created_before is None

    def test_z_suffix_and_naive_timestamps_are_utc(self):
        request = _parse_knowledge_search_request(
            {"created_after": "2026-09-26T00:00:00Z", "created_before": "2026-09-27T00:00:00"}
        )
        assert request.created_after == datetime(2026, 9, 26, tzinfo=timezone.utc)
        assert request.created_before == datetime(2026, 9, 27, tzinfo=timezone.utc)

    def test_offset_is_normalized_to_utc(self):
        request = _parse_knowledge_search_request({"created_after": "2026-09-26T00:00:00-06:00"})
        assert request.created_after == datetime(2026, 9, 26, 6, tzinfo=timezone.utc)

    @pytest.mark.parametrize("bad", ["yesterday", "2026-13-01", ""])
    def test_unparseable_timestamp_is_refused(self, bad):
        with pytest.raises(_SearchParameterError, match="created_after"):
            _parse_knowledge_search_request({"created_after": bad})

    def test_inverted_window_is_refused(self):
        with pytest.raises(_SearchParameterError, match="earlier than"):
            _parse_knowledge_search_request(
                {"created_after": "2026-09-27", "created_before": "2026-09-26"}
            )

    def test_unknown_sort_is_refused(self):
        with pytest.raises(_SearchParameterError, match="sort_by"):
            _parse_knowledge_search_request({"query": "x", "sort_by": "score"})

    @pytest.mark.parametrize("flag", [True, "true"])
    def test_newest_first_refuses_the_legacy_semantic_toggle(self, flag):
        # Review finding (#2517): semantic=true was accepted and silently ran FTS.
        with pytest.raises(_SearchParameterError, match="semantic=true"):
            _parse_knowledge_search_request({"query": "x", "sort_by": "created_at", "semantic": flag})

    def test_semantic_false_is_compatible_with_newest_first(self):
        request = _parse_knowledge_search_request(
            {"query": "x", "sort_by": "created_at", "semantic": False}
        )
        assert request.sort_by == "created_at"

    @pytest.mark.parametrize("mode", ["semantic", "hybrid"])
    def test_newest_first_refuses_similarity_modes(self, mode):
        with pytest.raises(_SearchParameterError, match="full-text matches"):
            _parse_knowledge_search_request(
                {"query": "x", "sort_by": "created_at", "search_mode": mode}
            )


class TestWindowOnCandidatesInHand:
    """Semantic retrieval cannot filter by date in the query; its candidates
    are held to the window after retrieval."""

    def _request(self, **arguments):
        return _parse_knowledge_search_request({"query": "x", **arguments})

    def test_no_window_admits_everything(self):
        assert _within_window(OLD_STRONG, self._request())

    def test_bounds_are_exclusive_and_applied(self):
        request = self._request(created_after=(NOW - timedelta(days=20)).isoformat())
        assert not _within_window(OLD_STRONG, request)
        assert _within_window(MID, request)
        at_bound = self._request(created_after=OLD_STRONG.timestamp)
        assert not _within_window(OLD_STRONG, at_bound)

    def test_unreadable_timestamp_is_outside_any_window(self):
        node = DiscoveryNode(id="x", agent_id="a", type="note", summary="s", timestamp="not-a-time")
        assert _within_window(node, self._request())
        assert not _within_window(node, self._request(created_after="2026-01-01"))

    @pytest.mark.asyncio
    async def test_hybrid_candidates_outside_the_window_are_dropped(self, monkeypatch):
        graph = MagicMock()
        graph.semantic_search = AsyncMock(return_value=[(OLD_STRONG, 0.9), (MID, 0.8)])
        graph.full_text_search = AsyncMock(return_value=[])
        monkeypatch.setenv("UNITARES_ENABLE_HYBRID", "1")
        request = _parse_knowledge_search_request(
            {"query": "coherence gate", "created_after": (NOW - timedelta(days=20)).isoformat()}
        )
        state = handlers._KnowledgeSearchState(request=request, graph=graph)
        await handlers._run_text_search(state)
        assert [d.id for d in state.results] == [MID.id]
        # The FTS leg was asked to window inside its query.
        assert graph.full_text_search.await_args.kwargs["created_after"] == request.created_after


class TestPgvectorWindow:
    """Review finding (#2517, round 2): a window applied after the semantic
    top-k could leave semantic mode empty while in-window rows existed below
    the cut. The bounds now ride inside the ranked pgvector query."""

    @pytest.mark.asyncio
    async def test_window_is_a_predicate_inside_the_ranked_query(self):
        from src.storage.knowledge_graph_age import KnowledgeGraphAGE

        conn = MagicMock()
        conn.fetch = AsyncMock(return_value=[])
        conn.execute = AsyncMock()
        tx = MagicMock()
        tx.__aenter__ = AsyncMock(return_value=None)
        tx.__aexit__ = AsyncMock(return_value=False)
        conn.transaction = MagicMock(return_value=tx)
        acquire = MagicMock()
        acquire.__aenter__ = AsyncMock(return_value=conn)
        acquire.__aexit__ = AsyncMock(return_value=False)
        db = MagicMock()
        db.acquire = MagicMock(return_value=acquire)

        graph = KnowledgeGraphAGE.__new__(KnowledgeGraphAGE)
        graph._get_db = AsyncMock(return_value=db)
        after = NOW - timedelta(days=1)
        with patch("src.embeddings.get_active_table_name", return_value="core.discovery_embeddings"):
            await graph._pgvector_search([0.1, 0.2], limit=5, min_similarity=0.3, created_after=after)

        sql, *params = conn.fetch.await_args.args
        assert "d.created_at > $3" in sql
        assert "LIMIT $4" in sql
        assert params[2] == after
        # The relaxed iterative scan keeps a narrow window from stopping
        # the filtered HNSW walk at ef_search candidates.
        conn.execute.assert_awaited_with("SET LOCAL hnsw.iterative_scan = relaxed_order")

    @pytest.mark.asyncio
    async def test_semantic_leg_is_asked_for_the_window(self, monkeypatch):
        graph = MagicMock()
        graph.semantic_search = AsyncMock(return_value=[(MID, 0.8)])
        graph.full_text_search = AsyncMock(return_value=[])
        monkeypatch.setenv("UNITARES_ENABLE_HYBRID", "1")
        request = _parse_knowledge_search_request(
            {"query": "coherence gate", "created_after": (NOW - timedelta(days=20)).isoformat()}
        )
        await handlers._run_text_search(handlers._KnowledgeSearchState(request=request, graph=graph))
        assert graph.semantic_search.await_args.kwargs["created_after"] == request.created_after


# ---------------------------------------------------------------------------
# Temporal narrator: "N entries added since last session"
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_temporal_narrator_passes_a_datetime_bound():
    from src.temporal import build_temporal_context

    db = AsyncMock()
    db.get_identity.return_value = MagicMock(identity_id=1)
    db.get_active_sessions_for_identity.return_value = [
        MagicMock(created_at=NOW - timedelta(minutes=5))
    ]
    db.get_last_inactive_session.return_value = MagicMock(last_active=NOW - timedelta(days=2))
    db.get_latest_agent_state.return_value = MagicMock(recorded_at=NOW - timedelta(minutes=1))
    db.get_recent_cross_agent_activity.return_value = []
    db.get_agent_state_history.return_value = []
    db.kg_query.return_value = [{"id": "d1"}]

    await build_temporal_context("test-uuid", db, now=NOW)

    bound = db.kg_query.await_args.kwargs["created_after"]
    assert isinstance(bound, datetime) and bound.tzinfo is not None
