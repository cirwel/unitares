"""Tests for the derived principal (octopus) reconciler.

The map is recomputed from the agent-metadata cache; identity/onboard responses
read it via lookup(). Singletons have no principal (lookup -> None); only
multi-instance components are mapped. principal_id is advisory/display-only.
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from src.services import principal_rollup


def _meta(thread=None, parent=None):
    return SimpleNamespace(thread_id=thread, parent_agent_id=parent)


@pytest.fixture(autouse=True)
def _reset_map():
    principal_rollup._MAP = {}
    yield
    principal_rollup._MAP = {}


def test_singleton_has_no_principal():
    principal_rollup.recompute({"a": _meta(), "b": _meta()})
    assert principal_rollup.lookup("a") is None
    assert principal_rollup.lookup("b") is None


def test_shared_thread_forms_one_principal():
    mapped = principal_rollup.recompute(
        {"a": _meta(thread="t1"), "b": _meta(thread="t1"), "c": _meta(thread="t1")}
    )
    assert mapped == 1
    p = principal_rollup.lookup("b")
    assert p["principal_id"] == "a"  # min member = stable anchor
    assert p["instance_count"] == 3
    assert p["source"] == "derived"


def test_lineage_merges_across_threads():
    principal_rollup.recompute(
        {"a": _meta(thread="t1"), "b": _meta(thread="t2", parent="a")}
    )
    assert principal_rollup.lookup("a")["instance_count"] == 2
    assert principal_rollup.lookup("b")["principal_id"] == "a"


def test_unknown_parent_does_not_merge():
    principal_rollup.recompute({"a": _meta(parent="ghost"), "b": _meta()})
    assert principal_rollup.lookup("a") is None  # still a singleton


def test_two_distinct_workers_each_their_own_principal():
    mapped = principal_rollup.recompute(
        {"a": _meta(thread="t1"), "b": _meta(thread="t1"),
         "c": _meta(thread="t2"), "d": _meta(thread="t2")}
    )
    assert mapped == 2
    assert principal_rollup.lookup("a")["principal_id"] == "a"
    assert principal_rollup.lookup("c")["principal_id"] == "c"


def test_lookup_fail_open():
    principal_rollup.recompute({"a": _meta(thread="t1"), "b": _meta(thread="t1")})
    assert principal_rollup.lookup("missing") is None
    assert principal_rollup.lookup(None) is None
    assert principal_rollup.lookup("") is None


def test_recompute_replaces_map_atomically():
    principal_rollup.recompute({"a": _meta(thread="t1"), "b": _meta(thread="t1")})
    assert principal_rollup.lookup("a") is not None
    # a later recompute where a/b are now singletons clears them
    principal_rollup.recompute({"a": _meta(), "b": _meta()})
    assert principal_rollup.lookup("a") is None
