"""Tests for the principal (octopus) rollup added to the list_agents summary.

A principal is a connected component over agent-declared edges only — shared
thread_id and declared lineage. The rollup is additive to the summary (it never
changes `total`/`participated`); see docs/proposals/principal-rollup-v0.md.
"""
from __future__ import annotations

from types import SimpleNamespace

from src.mcp_handlers.lifecycle.query import _principal_rollup


def _meta(thread=None, parent=None):
    return SimpleNamespace(thread_id=thread, parent_agent_id=parent)


def _agent(uid, updates=1):
    return {"_agent_uuid": uid, "total_updates": updates}


def _rollup(agents, metas):
    return _principal_rollup(agents, meta_lookup=metas.get)


def test_singletons_stay_singular():
    agents = [_agent("a"), _agent("b"), _agent("c")]
    metas = {"a": _meta(), "b": _meta(), "c": _meta()}
    r = _rollup(agents, metas)
    assert r["principals"] == 3
    assert r["multi_instance_principals"] == 0
    assert r["participated_principals"] == 3


def test_shared_thread_merges():
    agents = [_agent("a"), _agent("b"), _agent("c")]
    metas = {"a": _meta(thread="t1"), "b": _meta(thread="t1"), "c": _meta(thread="t1")}
    r = _rollup(agents, metas)
    assert r["principals"] == 1
    assert r["multi_instance_principals"] == 1


def test_declared_lineage_merges_across_threads():
    # b -> a, c -> b on different threads: one principal via lineage
    agents = [_agent("a"), _agent("b"), _agent("c")]
    metas = {
        "a": _meta(thread="t1"),
        "b": _meta(thread="t2", parent="a"),
        "c": _meta(thread="t3", parent="b"),
    }
    assert _rollup(agents, metas)["principals"] == 1


def test_lineage_and_thread_combine():
    # {a,b} share a thread; c chains to b -> all one principal
    agents = [_agent("a"), _agent("b"), _agent("c")]
    metas = {"a": _meta(thread="t1"), "b": _meta(thread="t1"), "c": _meta(parent="b")}
    assert _rollup(agents, metas)["principals"] == 1


def test_unknown_parent_does_not_merge():
    # parent outside the listed population (e.g. archived ancestor) -> no merge
    agents = [_agent("a"), _agent("b")]
    metas = {"a": _meta(parent="ghost-not-listed"), "b": _meta()}
    assert _rollup(agents, metas)["principals"] == 2


def test_principal_participated_if_any_instance_did():
    # one thread, two instances: one ghost (0 updates), one checked in ->
    # the principal counts as participated.
    agents = [_agent("a", updates=0), _agent("b", updates=3)]
    metas = {"a": _meta(thread="t1"), "b": _meta(thread="t1")}
    r = _rollup(agents, metas)
    assert r["principals"] == 1
    assert r["participated_principals"] == 1


def test_all_ghost_principal_not_participated():
    agents = [_agent("a", updates=0), _agent("b", updates=0)]
    metas = {"a": _meta(thread="t1"), "b": _meta(thread="t1")}
    r = _rollup(agents, metas)
    assert r["principals"] == 1
    assert r["participated_principals"] == 0


def test_falls_back_to_id_when_no_agent_uuid():
    # lite-shaped dicts may not carry _agent_uuid; the rollup uses `id`.
    agents = [{"id": "a", "total_updates": 1}, {"id": "b", "total_updates": 1}]
    metas = {"a": _meta(thread="t1"), "b": _meta(thread="t1")}
    assert _rollup(agents, metas)["principals"] == 1
