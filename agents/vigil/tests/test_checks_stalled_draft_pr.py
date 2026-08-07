"""Tests for the StalledDraftPR Vigil check.

The check fires only on the intersection draft + stale + red. The pure helpers
(``select_stale``, ``failing_check_names``, ``assess``) carry the logic, so the
tests drive them with synthetic rows and a fixed ``now`` — no real clock, no
network, no ``gh``. The one I/O-shaped path (``run`` degrading when ``gh`` is
unavailable) is exercised by pointing VIGIL_GH_BIN at a binary that cannot exist.
"""

from __future__ import annotations

import asyncio

import pytest

HOUR = 3600.0
NOW = 1_000_000.0  # fixed synthetic clock


def _reset_registry():
    from agents.vigil.checks import registry
    registry._CHECKS.clear()
    registry._LOADED = False


@pytest.fixture(autouse=True)
def clean_registry():
    _reset_registry()
    yield
    _reset_registry()


def _iso(epoch: float) -> str:
    import datetime as dt

    return (
        dt.datetime.fromtimestamp(epoch, dt.timezone.utc)
        .strftime("%Y-%m-%dT%H:%M:%SZ")
    )


def _pr(number: int, age_hours: float, repo: str = "cirwel/unitares") -> dict:
    return {
        "number": number,
        "repository": {"nameWithOwner": repo},
        "title": f"pr {number}",
        "updatedAt": _iso(NOW - age_hours * HOUR),
        "url": f"https://github.com/{repo}/pull/{number}",
    }


def _stalled(number: int, age_hours: float, failing=("smoke",)) -> dict:
    return {
        "repo": "cirwel/unitares",
        "number": number,
        "age_hours": age_hours,
        "failing": list(failing),
        "url": "",
    }


def _assess(stalled, **kw):
    from agents.vigil.checks.stalled_draft_pr import assess

    params = dict(
        stale_hours=12.0,
        candidates_examined=len(stalled),
        candidates_skipped=0,
        search_truncated=False,
        search_limit=60,
        max_inspect=15,
    )
    params.update(kw)
    return assess(stalled, **params)


# --- identity / registration -------------------------------------------------


def test_identity():
    from agents.vigil.checks.stalled_draft_pr import StalledDraftPR

    c = StalledDraftPR()
    assert c.name == "stalled_draft_pr"
    # A novel service_key is load-bearing: _collect_health_state only gives
    # per-service {svc}_healthy bookkeeping to keys that aren't governance/lumen,
    # and that bookkeeping is what makes the finding page once per transition.
    assert c.service_key not in ("governance", "lumen")


def test_registered_by_default():
    from agents.vigil.checks import registry

    registry.load_plugins()
    assert "stalled_draft_pr" in {c.name for c in registry.all_checks()}


# --- select_stale ------------------------------------------------------------


def test_select_stale_keeps_only_older_than_window():
    from agents.vigil.checks.stalled_draft_pr import select_stale

    rows = select_stale([_pr(1, 2), _pr(2, 20)], NOW, 12.0)
    assert [r["number"] for r in rows] == [2]
    assert rows[0]["age_hours"] == pytest.approx(20.0, abs=0.05)


def test_select_stale_sorts_oldest_first():
    from agents.vigil.checks.stalled_draft_pr import select_stale

    rows = select_stale([_pr(1, 15), _pr(2, 40), _pr(3, 25)], NOW, 12.0)
    assert [r["number"] for r in rows] == [2, 3, 1]


def test_select_stale_skips_unparseable_timestamps():
    """A parse failure must not be treated as infinitely old and invent a page."""
    from agents.vigil.checks.stalled_draft_pr import select_stale

    bad = _pr(1, 40)
    bad["updatedAt"] = "not-a-date"
    missing = _pr(2, 40)
    del missing["updatedAt"]
    assert select_stale([bad, missing], NOW, 12.0) == []


# --- failing_check_names -----------------------------------------------------


@pytest.mark.parametrize(
    "node,expected",
    [
        ({"name": "smoke", "conclusion": "FAILURE"}, ["smoke"]),
        ({"name": "smoke", "conclusion": "TIMED_OUT"}, ["smoke"]),
        ({"name": "smoke", "conclusion": "CANCELLED"}, ["smoke"]),
        ({"name": "smoke", "conclusion": "SUCCESS"}, []),
        ({"name": "smoke", "conclusion": None}, []),  # still running
        ({"context": "legacy", "state": "FAILURE"}, ["legacy"]),
        ({"context": "legacy", "state": "SUCCESS"}, []),
    ],
)
def test_failing_check_names_shapes(node, expected):
    from agents.vigil.checks.stalled_draft_pr import failing_check_names

    assert failing_check_names([node]) == expected


def test_failing_check_names_tolerates_junk():
    from agents.vigil.checks.stalled_draft_pr import failing_check_names

    assert failing_check_names(None) == []
    assert failing_check_names(["not-a-dict", {"conclusion": "FAILURE"}]) == ["?"]


# --- assess ------------------------------------------------------------------


def test_assess_clean_is_ok():
    r = _assess([], candidates_examined=3)
    assert r.ok is True
    assert r.detail["stalled_pr_count"] == 0
    assert not r.fingerprint_key


def test_assess_clean_clears_every_persisted_key():
    """_collect_health_state merges detail with dict.update(), so a recovered
    cycle must overwrite every key it ever sets — otherwise the state file keeps
    advertising last cycle's stalled PRs forever."""
    dirty = _assess([_stalled(1, 30.0)], candidates_skipped=2, search_truncated=True)
    clean = _assess([], candidates_examined=1)
    assert set(dirty.detail) == set(clean.detail)
    assert clean.detail["stalled_prs"] == []


def test_assess_flags_stalled_pr():
    r = _assess([_stalled(1511, 23.0, failing=("smoke", "Analyze (python)"))])
    assert r.ok is False
    assert r.severity == "warning"
    assert r.fingerprint_key == "stalled_draft_pr"
    assert "cirwel/unitares#1511" in r.summary
    assert "23h" in r.summary
    assert "smoke" in r.summary
    assert r.detail["stalled_pr_count"] == 1


def test_assess_names_overflow_failing_checks_instead_of_hiding_them():
    r = _assess([_stalled(1, 30.0, failing=("a", "b", "c", "d", "e"))])
    assert "a, b, c, +2 more" in r.summary
    assert r.detail["stalled_prs"][0]["failing"] == ["a", "b", "c", "d", "e"]


def test_assess_reports_worst_first_and_counts_the_rest():
    r = _assess([_stalled(1, 40.0), _stalled(2, 20.0), _stalled(3, 15.0)])
    assert "#1" in r.summary
    assert "+2 more" in r.summary
    assert r.detail["stalled_pr_count"] == 3


def test_assess_surfaces_search_truncation_when_clean():
    """A truncated sweep must never read as an exhaustive clean one."""
    r = _assess([], search_truncated=True, search_limit=60)
    assert r.ok is True
    assert "60-PR limit" in r.summary


def test_assess_surfaces_inspection_cap_when_flagging():
    r = _assess([_stalled(1, 30.0)], candidates_skipped=4, max_inspect=15)
    assert r.ok is False
    assert "4 stale draft(s) not status-checked" in r.summary
    assert r.detail["stalled_pr_unexamined"] == 4


# --- degradation -------------------------------------------------------------


def test_run_is_indeterminate_when_gh_missing(monkeypatch):
    """No gh -> ok=True at info. A blind check must not manufacture a page."""
    from agents.vigil.checks import stalled_draft_pr as mod

    monkeypatch.setattr(mod, "GH_BIN", "/nonexistent/gh-binary-for-tests")
    result = asyncio.run(mod.StalledDraftPR().run())
    assert result.ok is True
    assert result.severity == "info"
    assert "indeterminate" in result.summary


def test_gather_propagates_gh_failure_as_unavailable(monkeypatch):
    from agents.vigil.checks import stalled_draft_pr as mod

    async def boom(*a, **kw):
        raise mod.GhUnavailable("gh auth required")

    monkeypatch.setattr(mod, "_gh_json", boom)
    with pytest.raises(mod.GhUnavailable):
        asyncio.run(
            mod.gather(
                "cirwel",
                now=NOW,
                stale_hours=12.0,
                search_limit=60,
                max_inspect=15,
                timeout=5.0,
            )
        )


def test_gather_counts_unreadable_pr_as_unexamined(monkeypatch):
    """One unreadable PR must not blind the sweep, and must not vanish silently."""
    from agents.vigil.checks import stalled_draft_pr as mod

    calls = {"n": 0}

    async def fake_gh(args, timeout):
        calls["n"] += 1
        if args[0] == "search":
            return [_pr(1, 30), _pr(2, 30)]
        if calls["n"] == 2:
            raise mod.GhUnavailable("no access")
        return {"statusCheckRollup": [{"name": "smoke", "conclusion": "FAILURE"}]}

    monkeypatch.setattr(mod, "_gh_json", fake_gh)
    stalled, examined, skipped, truncated = asyncio.run(
        mod.gather(
            "cirwel",
            now=NOW,
            stale_hours=12.0,
            search_limit=60,
            max_inspect=15,
            timeout=5.0,
        )
    )
    assert len(stalled) == 1
    assert examined == 2
    assert skipped == 1
    assert truncated is False


def test_gather_skips_green_drafts(monkeypatch):
    """Green-and-waiting is the designed state, not a fault."""
    from agents.vigil.checks import stalled_draft_pr as mod

    async def fake_gh(args, timeout):
        if args[0] == "search":
            return [_pr(1, 30)]
        return {"statusCheckRollup": [{"name": "smoke", "conclusion": "SUCCESS"}]}

    monkeypatch.setattr(mod, "_gh_json", fake_gh)
    stalled, examined, skipped, _ = asyncio.run(
        mod.gather(
            "cirwel",
            now=NOW,
            stale_hours=12.0,
            search_limit=60,
            max_inspect=15,
            timeout=5.0,
        )
    )
    assert stalled == []
    assert examined == 1
    assert skipped == 0


def test_gather_flags_search_truncation_at_limit(monkeypatch):
    from agents.vigil.checks import stalled_draft_pr as mod

    async def fake_gh(args, timeout):
        if args[0] == "search":
            return [_pr(i, 1) for i in range(3)]  # fresh, so none inspected
        return {}

    monkeypatch.setattr(mod, "_gh_json", fake_gh)
    _, _, _, truncated = asyncio.run(
        mod.gather(
            "cirwel",
            now=NOW,
            stale_hours=12.0,
            search_limit=3,
            max_inspect=15,
            timeout=5.0,
        )
    )
    assert truncated is True
