"""Regression tests for src/gateway/query_engine.py.

The gateway's natural-language router classifies a question into one of five
intents (status/checkin/search/note/help) and maps it to a tool call. It tries
an LLM first, then a deterministic keyword fallback. Only the LLM path needs a
client; the keyword classifier and the intent->tool routing are pure and were
untested. These tests pin the fallback contract (the safety net when the LLM is
unavailable) and the routing table.
"""

from __future__ import annotations

import pytest

from src.gateway.query_engine import (
    _keyword_classify,
    classify_intent,
    route_query,
)


class _FakeClient:
    """Minimal GovernanceMCPClient stand-in for classify_intent.

    Either returns a canned call_model result or raises, to exercise the
    LLM-success and LLM-failure branches.
    """

    def __init__(self, result=None, raises=False):
        self._result = result
        self._raises = raises
        self.calls = []

    async def call_tool(self, name, args):
        self.calls.append((name, args))
        if self._raises:
            raise RuntimeError("model unavailable")
        return self._result


# --------------------------------------------------------------------------- #
# _keyword_classify — deterministic fallback
# --------------------------------------------------------------------------- #

class TestKeywordClassify:
    @pytest.mark.parametrize(
        "question, expected",
        [
            ("what is my eisv coherence right now", "status"),
            ("show me the current verdict and health", "status"),
            ("I finished the refactor and completed tests", "checkin"),
            ("find the knowledge graph entry", "search"),
            ("please save this as a note", "note"),
            ("what tools are available, help", "help"),
            ("What does EISV stand for?", "explain"),
            ("explain coherence", "explain"),
            ("what does my verdict mean right now", "status"),
            ("What does I stand for in EISV?", "explain"),
            ("Can you explain to me what EISV means?", "explain"),
            ("define eventual consistency", "search"),
        ],
    )
    def test_keyword_routes(self, question, expected):
        assert _keyword_classify(question) == expected

    def test_unmatched_defaults_to_search(self):
        assert _keyword_classify("zxqw plover frobnicate") == "search"

    def test_is_case_insensitive(self):
        assert _keyword_classify("EISV STATUS PLEASE") == "status"

    def test_first_pattern_wins_on_overlap(self):
        # Contains both a status word ('state') and a search word ('find');
        # status is earlier in KEYWORD_PATTERNS so it must win.
        assert _keyword_classify("find my state") == "status"


# --------------------------------------------------------------------------- #
# classify_intent — LLM-first with keyword fallback
# --------------------------------------------------------------------------- #

class TestClassifyIntent:
    @pytest.mark.asyncio
    async def test_uses_valid_llm_intent(self):
        client = _FakeClient(result={"response": "note"})
        # question keywords would say 'status', but a valid LLM answer wins.
        assert await classify_intent("what is my status", client) == "note"

    @pytest.mark.asyncio
    async def test_unknown_llm_intent_falls_back_to_keywords(self):
        client = _FakeClient(result={"response": "frobnicate"})
        assert await classify_intent("what is my status", client) == "status"

    @pytest.mark.asyncio
    async def test_llm_failure_falls_back_to_keywords(self):
        client = _FakeClient(raises=True)
        assert await classify_intent("please save this note", client) == "note"

    @pytest.mark.asyncio
    async def test_string_result_is_accepted(self):
        client = _FakeClient(result="search")
        assert await classify_intent("anything", client) == "search"


# --------------------------------------------------------------------------- #
# route_query — intent -> tool/args mapping
# --------------------------------------------------------------------------- #

class TestRouteQuery:
    @pytest.mark.asyncio
    async def test_status_route(self):
        client = _FakeClient(result={"response": "status"})
        assert await route_query("how am I doing", client) == {"tool": "status", "args": {}}

    @pytest.mark.asyncio
    async def test_checkin_carries_question_as_summary(self):
        client = _FakeClient(result={"response": "checkin"})
        out = await route_query("I shipped the parser", client)
        assert out == {"tool": "checkin", "args": {"summary": "I shipped the parser"}}

    @pytest.mark.asyncio
    async def test_search_carries_question_as_query(self):
        client = _FakeClient(result={"response": "search"})
        out = await route_query("where is the migration doc", client)
        assert out == {"tool": "search", "args": {"query": "where is the migration doc"}}

    @pytest.mark.asyncio
    async def test_note_carries_question_as_content(self):
        client = _FakeClient(result={"response": "note"})
        out = await route_query("remember to rotate keys", client)
        assert out == {"tool": "note", "args": {"content": "remember to rotate keys"}}

    @pytest.mark.asyncio
    async def test_help_route(self):
        client = _FakeClient(result={"response": "help"})
        assert await route_query("what can you do", client) == {"tool": "help", "args": {}}

    @pytest.mark.asyncio
    async def test_default_routes_to_search(self):
        # LLM raises and the question matches no keyword → default search,
        # with the question carried as the query.
        client = _FakeClient(raises=True)
        out = await route_query("zxqw plover", client)
        assert out == {"tool": "search", "args": {"query": "zxqw plover"}}


class TestClassifierSessionBinding:
    """call_model needs a bound caller, so the session id must reach it."""

    @pytest.mark.asyncio
    async def test_classify_forwards_client_session_id(self):
        client = _FakeClient(result={"response": "note"})
        assert await classify_intent("keep this observation", client, client_session_id="agent-abc") == "note"
        name, args = client.calls[0]
        assert name == "call_model"
        assert args["client_session_id"] == "agent-abc"

    @pytest.mark.asyncio
    async def test_classify_omits_client_session_id_when_absent(self):
        client = _FakeClient(result={"response": "note"})
        await classify_intent("keep this observation", client)
        assert "client_session_id" not in client.calls[0][1]

    @pytest.mark.asyncio
    async def test_route_query_forwards_client_session_id(self):
        client = _FakeClient(result={"response": "note"})
        route = await route_query("keep this observation", client, client_session_id="agent-abc")
        assert route["tool"] == "note"
        assert client.calls[0][1]["client_session_id"] == "agent-abc"


class TestExplainRoute:
    @pytest.mark.asyncio
    async def test_explain_intent_routes_to_help(self):
        client = _FakeClient(result={"response": "explain"})
        assert await route_query("what does EISV stand for", client) == {"tool": "help", "args": {}}

    @pytest.mark.asyncio
    async def test_explain_outside_glossary_keeps_search(self):
        client = _FakeClient(result={"response": "explain"})
        out = await route_query("define eventual consistency", client)
        assert out == {"tool": "search", "args": {"query": "define eventual consistency"}}

    @pytest.mark.asyncio
    async def test_definitional_question_survives_llm_failure(self):
        # LLM down: the keyword fallback must not send this to status.
        client = _FakeClient(raises=True)
        assert await route_query("What does EISV stand for?", client) == {"tool": "help", "args": {}}
