"""Discovery of third-party resident-progress sources.

These tests use a fake entry-point loader rather than installing real
distributions: the contract under test is "what does discovery accept and
reject", not importlib's ability to read package metadata.
"""
from __future__ import annotations

from datetime import timedelta

import pytest

from src.resident_progress.plugins import (
    ENTRY_POINT_GROUP,
    PLUGINS_ENABLED_ENV,
    discover_progress_sources,
    plugins_enabled,
)

BUILTINS = ("kg_writes", "watcher_findings", "eisv_sync_rows",
            "metrics_series", "sentinel_pulse", "agent_checkins")


class _FakeEntryPoint:
    def __init__(self, name, target, *, raises=None):
        self.name = name
        self._target = target
        self._raises = raises

    def load(self):
        if self._raises is not None:
            raise self._raises
        return self._target


def _loader(*eps):
    def load(group):
        assert group == ENTRY_POINT_GROUP
        return list(eps)
    return load


class _GoodSource:
    """A minimal well-formed third-party source."""
    def __init__(self, db, name="my_source"):
        self._db = db
        self.name = name

    async def fetch(self, resident_uuids, window: timedelta):
        return {u: 1 for u in resident_uuids}


def _factory(name):
    return lambda db: _GoodSource(db, name=name)


# --- happy path ------------------------------------------------------------

def test_well_formed_plugin_is_registered_under_its_entry_point_name():
    res = discover_progress_sources(
        db=object(), builtin_names=BUILTINS,
        entry_point_loader=_loader(_FakeEntryPoint("my_source", _factory("my_source"))),
    )
    assert res.errors == []
    assert set(res.sources) == {"my_source"}
    assert res.sources["my_source"].name == "my_source"


@pytest.mark.asyncio
async def test_registered_plugin_is_actually_callable():
    """Guards against registering an object that only *looks* like a source."""
    res = discover_progress_sources(
        db=object(), builtin_names=BUILTINS,
        entry_point_loader=_loader(_FakeEntryPoint("my_source", _factory("my_source"))),
    )
    out = await res.sources["my_source"].fetch(["u1", "u2"], timedelta(hours=1))
    assert out == {"u1": 1, "u2": 1}


def test_no_entry_points_is_not_an_error():
    """The canonical deployment has zero plugins; that must be silent."""
    res = discover_progress_sources(
        db=object(), builtin_names=BUILTINS, entry_point_loader=_loader(),
    )
    assert res.sources == {} and res.errors == []


# --- collision with first-party names --------------------------------------

@pytest.mark.parametrize("builtin", BUILTINS)
def test_plugin_may_not_shadow_a_first_party_source(builtin):
    """A shadowing plugin could silently redefine a first-party metric."""
    res = discover_progress_sources(
        db=object(), builtin_names=BUILTINS,
        entry_point_loader=_loader(_FakeEntryPoint(builtin, _factory(builtin))),
    )
    assert res.sources == {}
    assert len(res.errors) == 1
    assert "first-party" in res.errors[0]
    assert builtin in res.errors[0]


def test_duplicate_entry_point_names_keep_the_first():
    res = discover_progress_sources(
        db=object(), builtin_names=BUILTINS,
        entry_point_loader=_loader(
            _FakeEntryPoint("dup", _factory("dup")),
            _FakeEntryPoint("dup", _factory("dup")),
        ),
    )
    assert set(res.sources) == {"dup"}
    assert any("duplicate" in e for e in res.errors)


# --- malformed plugins are rejected, not fatal -----------------------------

def test_load_failure_is_recorded_and_does_not_raise():
    res = discover_progress_sources(
        db=object(), builtin_names=BUILTINS,
        entry_point_loader=_loader(
            _FakeEntryPoint("broken", None, raises=ImportError("no module named x")),
        ),
    )
    assert res.sources == {}
    assert "load failed" in res.errors[0] and "ImportError" in res.errors[0]


def test_factory_that_raises_is_recorded_and_does_not_raise():
    def boom(db):
        raise RuntimeError("bad config")
    res = discover_progress_sources(
        db=object(), builtin_names=BUILTINS,
        entry_point_loader=_loader(_FakeEntryPoint("boom", boom)),
    )
    assert res.sources == {}
    assert "factory raised" in res.errors[0] and "RuntimeError" in res.errors[0]


def test_source_without_fetch_is_rejected():
    class NoFetch:
        name = "nofetch"
    res = discover_progress_sources(
        db=object(), builtin_names=BUILTINS,
        entry_point_loader=_loader(_FakeEntryPoint("nofetch", lambda db: NoFetch())),
    )
    assert res.sources == {}
    assert "callable 'fetch'" in res.errors[0]


def test_source_without_name_is_rejected():
    class NoName:
        async def fetch(self, resident_uuids, window):
            return {}
    res = discover_progress_sources(
        db=object(), builtin_names=BUILTINS,
        entry_point_loader=_loader(_FakeEntryPoint("noname", lambda db: NoName())),
    )
    assert res.sources == {}
    assert "'name' attribute" in res.errors[0]


def test_name_mismatch_is_rejected_rather_than_silently_rekeyed():
    """entry-point name, source.name and the manifest's `source` are one string.

    Keying by one while the operator reads the other is how a source ends up
    installed but never referenced by any manifest entry.
    """
    res = discover_progress_sources(
        db=object(), builtin_names=BUILTINS,
        entry_point_loader=_loader(
            _FakeEntryPoint("declared_name", _factory("actual_name")),
        ),
    )
    assert res.sources == {}
    assert "does not match source.name" in res.errors[0]


def test_one_bad_plugin_does_not_block_a_good_one():
    """The probe is part of the detection layer; it must still boot."""
    res = discover_progress_sources(
        db=object(), builtin_names=BUILTINS,
        entry_point_loader=_loader(
            _FakeEntryPoint("broken", None, raises=ValueError("nope")),
            _FakeEntryPoint("good", _factory("good")),
        ),
    )
    assert set(res.sources) == {"good"}
    assert len(res.errors) == 1


def test_enumeration_failure_is_contained():
    def explode(group):
        raise OSError("metadata unreadable")
    res = discover_progress_sources(
        db=object(), builtin_names=BUILTINS, entry_point_loader=explode,
    )
    assert res.sources == {}
    assert "enumeration failed" in res.errors[0]


# --- opt-out ---------------------------------------------------------------

@pytest.mark.parametrize("value", ["0", "false", "no", "off", "OFF", " 0 "])
def test_discovery_can_be_disabled(value):
    res = discover_progress_sources(
        db=object(), builtin_names=BUILTINS,
        entry_point_loader=_loader(_FakeEntryPoint("my_source", _factory("my_source"))),
        env={PLUGINS_ENABLED_ENV: value},
    )
    assert res.sources == {} and res.errors == []


@pytest.mark.parametrize("env", [{}, {PLUGINS_ENABLED_ENV: "1"}, {PLUGINS_ENABLED_ENV: "true"}])
def test_discovery_is_on_by_default(env):
    assert plugins_enabled(env) is True
