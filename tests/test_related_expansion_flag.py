"""UNITARES_ENABLE_RELATED_EXPANSION, and its deprecated name.

The expansion path adds the findings each top result lists in its stored
`related_to` column; it never read the AGE graph edges, so the flag was renamed
from UNITARES_ENABLE_GRAPH_EXPANSION (operator delegation, 2026-10-20). The old
name is still read when the new one is unset, with one deprecation warning.
"""

import logging

import pytest

from src import retrieval


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    monkeypatch.delenv(retrieval.RELATED_EXPANSION_ENV, raising=False)
    monkeypatch.delenv(retrieval.DEPRECATED_RELATED_EXPANSION_ENV, raising=False)
    monkeypatch.setattr(retrieval, "_warned_deprecated_related_expansion", False)


def test_off_by_default():
    assert retrieval.related_expansion_enabled() is False
    assert retrieval.graph_expansion_enabled() is False


def test_new_name_turns_it_on(monkeypatch):
    monkeypatch.setenv(retrieval.RELATED_EXPANSION_ENV, "1")
    assert retrieval.related_expansion_enabled() is True


def test_old_name_still_works_and_warns_once(monkeypatch, caplog):
    monkeypatch.setenv(retrieval.DEPRECATED_RELATED_EXPANSION_ENV, "1")
    with caplog.at_level(logging.WARNING):
        assert retrieval.related_expansion_enabled() is True
        assert retrieval.related_expansion_enabled() is True
    warnings = [r for r in caplog.records if "deprecated" in r.getMessage()]
    assert len(warnings) == 1
    assert retrieval.RELATED_EXPANSION_ENV in warnings[0].getMessage()


def test_new_name_wins_when_both_are_set(monkeypatch, caplog):
    monkeypatch.setenv(retrieval.RELATED_EXPANSION_ENV, "0")
    monkeypatch.setenv(retrieval.DEPRECATED_RELATED_EXPANSION_ENV, "1")
    with caplog.at_level(logging.WARNING):
        assert retrieval.related_expansion_enabled() is False
    assert not [r for r in caplog.records if "deprecated" in r.getMessage()]
