"""Tests for the octopus rollup logic (scripts/dev/octopus_rollup.py).

Covers the pure connected-component rollup: thread-reuse merges, lineage merges,
the two combining transitively, and singletons staying singular.
"""
import importlib.util
from pathlib import Path

_SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "dev" / "octopus_rollup.py"
_spec = importlib.util.spec_from_file_location("octopus_rollup", _SCRIPT)
octopus_rollup = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(octopus_rollup)

rollup = octopus_rollup.rollup


def _sizes(rows):
    return sorted((len(v) for v in rollup(rows).values()), reverse=True)


def test_singletons_stay_singular():
    # three unrelated one-shots: no thread, no lineage -> three octopi
    rows = [("a", "", ""), ("b", "", ""), ("c", "", "")]
    assert _sizes(rows) == [1, 1, 1]


def test_shared_thread_merges():
    # same thread = same logical worker (the Hermes / Claude-session-chain shape)
    rows = [("a", "t1", ""), ("b", "t1", ""), ("c", "t1", "")]
    assert _sizes(rows) == [3]


def test_declared_lineage_merges():
    # b -> a, c -> b across DIFFERENT threads: still one octopus via lineage
    rows = [("a", "t1", ""), ("b", "t2", "a"), ("c", "t3", "b")]
    assert _sizes(rows) == [3]


def test_lineage_and_thread_combine_transitively():
    # {a,b} share a thread; c chains to b by lineage -> all three are one octopus
    rows = [("a", "t1", ""), ("b", "t1", ""), ("c", "t9", "b")]
    assert _sizes(rows) == [3]


def test_unknown_parent_does_not_merge():
    # parent points outside the set (e.g. an archived ancestor not in scope):
    # the child must not be merged into a phantom — it stands alone.
    rows = [("a", "", "ghost-not-in-set"), ("b", "", "")]
    assert _sizes(rows) == [1, 1]


def test_two_distinct_workers_stay_distinct():
    rows = [("a", "t1", ""), ("b", "t1", ""), ("c", "t2", ""), ("d", "t2", "")]
    assert _sizes(rows) == [2, 2]
