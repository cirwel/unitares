"""Tests for the StalledDraftPR Vigil check.

The check fires on draft + red-past-the-window, where "red for how long" comes
from the newest failing check-run's ``completedAt`` — never from the PR's
``updatedAt``, which this fleet's doc-validation bot resets on every workflow
run. The pure helpers carry the logic, so most tests drive them with synthetic
rows and a fixed ``now``: no real clock, no network, no ``gh``.

The paths that previously shipped bugs get explicit regression coverage:
degradation must not flip the transition edge, bound-hits are counted by cause,
and no exception may escape to runner.py (which would turn it into a `critical`
page — the inverse of this module's contract).
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

    return dt.datetime.fromtimestamp(epoch, dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _pr(number: int, updated_h: float = 1.0, repo: str = "cirwel/unitares") -> dict:
    return {
        "number": number,
        "repository": {"nameWithOwner": repo},
        "title": f"pr {number}",
        "updatedAt": _iso(NOW - updated_h * HOUR),
        "url": f"https://github.com/{repo}/pull/{number}",
    }


def _run(name: str, conclusion: str, completed_h: float = 0.0) -> dict:
    return {
        "__typename": "CheckRun",
        "name": name,
        "conclusion": conclusion,
        "status": "COMPLETED",
        "completedAt": _iso(NOW - completed_h * HOUR),
    }


def _stalled(number: int, red_hours: float, failing=("smoke",)) -> dict:
    return {
        "repo": "cirwel/unitares",
        "number": number,
        "red_hours": red_hours,
        "failing": list(failing),
        "url": "",
    }


def _assess(stalled, **kw):
    from agents.vigil.checks.stalled_draft_pr import assess

    params = dict(
        stale_hours=12.0,
        drafts_read=len(stalled),
        cap_skipped=0,
        unreadable=0,
        deadline_skipped=0,
        search_truncated=False,
        search_limit=60,
        max_inspect=15,
    )
    params.update(kw)
    return assess(stalled, **params)


async def _gather(monkeypatch, fake_gh, **kw):
    from agents.vigil.checks import stalled_draft_pr as mod

    monkeypatch.setattr(mod, "_gh_json", fake_gh)
    params = dict(
        now=NOW, stale_hours=12.0, search_limit=60, max_inspect=15,
        timeout=5.0, budget=25.0, concurrency=4,
    )
    params.update(kw)
    return await mod.gather("cirwel", **params)


# --- identity / registration -------------------------------------------------


def test_identity():
    from agents.vigil.checks.stalled_draft_pr import StalledDraftPR

    c = StalledDraftPR()
    assert c.name == "stalled_draft_pr"
    # Exact value, not merely "novel": _collect_health_state keys persisted
    # state off it and agent.CONDITION_SERVICE_KEYS names it literally, so a
    # typo would fragment state and re-enable the false-outage notes.
    assert c.service_key == "github"


def test_registered_by_default():
    from agents.vigil.checks import registry

    registry.load_plugins()
    assert "stalled_draft_pr" in {c.name for c in registry.all_checks()}


def test_service_key_is_exempt_from_outage_notes():
    """Regression: detect_changes must not write "Github is down" to the KG."""
    from agents.vigil import agent as vigil_agent
    from agents.vigil.checks.stalled_draft_pr import StalledDraftPR

    assert StalledDraftPR.service_key in vigil_agent.CONDITION_SERVICE_KEYS

    notes = vigil_agent.detect_changes(
        {"github_healthy": True, "github_down_streak": 0},
        {"github_healthy": False, "github_detail": "stalled", "github_down_streak": 3},
    )
    assert not [n for n in notes if "github" in n.get("tags", [])]


def test_real_services_still_get_outage_notes():
    """The exemption must be surgical — governance/lumen still report outages."""
    from agents.vigil import agent as vigil_agent

    notes = vigil_agent.detect_changes(
        {"lumen_healthy": True}, {"lumen_healthy": False, "lumen_detail": "timeout"}
    )
    assert any("lumen" in n.get("tags", []) for n in notes)


# --- the stall clock ---------------------------------------------------------


def test_red_since_uses_newest_failing_run():
    from agents.vigil.checks.stalled_draft_pr import red_since

    rollup = [
        _run("a", "FAILURE", 30.0),
        _run("b", "FAILURE", 5.0),
        _run("c", "SUCCESS", 1.0),  # green runs must not anchor the clock
    ]
    assert red_since(rollup) == pytest.approx(NOW - 5.0 * HOUR, abs=1)


def test_red_since_ignores_green_only_rollup():
    from agents.vigil.checks.stalled_draft_pr import red_since

    assert red_since([_run("a", "SUCCESS", 1.0)]) is None


def test_clock_is_not_updatedat(monkeypatch):
    """The load-bearing regression: a bot comment bumping updatedAt to *now*
    must not reset the stall clock. CI went red 30h ago and nothing has run
    since, so the PR is stalled regardless of chatter on the thread."""

    async def fake_gh(args, timeout):
        if args[0] == "search":
            return [_pr(1, updated_h=0.01)]  # touched seconds ago by a bot
        return {"statusCheckRollup": [_run("smoke", "FAILURE", 30.0)]}

    res = asyncio.run(_gather(monkeypatch, fake_gh))
    assert len(res["stalled"]) == 1
    assert res["stalled"][0]["red_hours"] == pytest.approx(30.0, abs=0.1)


def test_recent_red_is_not_stalled(monkeypatch):
    async def fake_gh(args, timeout):
        if args[0] == "search":
            return [_pr(1, updated_h=40.0)]  # stale by updatedAt...
        return {"statusCheckRollup": [_run("smoke", "FAILURE", 2.0)]}  # ...but just went red

    res = asyncio.run(_gather(monkeypatch, fake_gh))
    assert res["stalled"] == []
    assert res["drafts_read"] == 1


def test_green_drafts_are_not_stalled(monkeypatch):
    """Green-and-waiting is the designed state, not a fault."""

    async def fake_gh(args, timeout):
        if args[0] == "search":
            return [_pr(1, updated_h=99.0)]
        return {"statusCheckRollup": [_run("smoke", "SUCCESS", 50.0)]}

    res = asyncio.run(_gather(monkeypatch, fake_gh))
    assert res["stalled"] == []


def test_search_is_ordered_oldest_first():
    """Without --sort updated, gh returns best-match order and the --limit cut
    would drop an arbitrary subset — possibly the very PR being hunted."""
    from agents.vigil.checks.stalled_draft_pr import _search_args

    args = _search_args("cirwel", 60)
    assert "--sort" in args and args[args.index("--sort") + 1] == "updated"
    assert "--order" in args and args[args.index("--order") + 1] == "asc"


# --- timestamp parsing -------------------------------------------------------


def test_naive_timestamp_is_pinned_to_utc():
    """Left to datetime.timestamp(), a naive string reads as LOCAL time — a
    silent hours-scale error on this fleet's Denver clock."""
    from agents.vigil.checks.stalled_draft_pr import _parse_iso8601_utc

    assert _parse_iso8601_utc("2026-08-06T12:00:00") == _parse_iso8601_utc(
        "2026-08-06T12:00:00Z"
    )


@pytest.mark.parametrize("bad", [None, "", "not-a-date", 12345, [], {}, True])
def test_parse_rejects_junk_without_raising(bad):
    from agents.vigil.checks.stalled_draft_pr import _parse_iso8601_utc

    assert _parse_iso8601_utc(bad) is None


def test_future_timestamp_does_not_fabricate_a_stall(monkeypatch):
    """Clock skew must fail closed, never invent a finding."""

    async def fake_gh(args, timeout):
        if args[0] == "search":
            return [_pr(1)]
        return {"statusCheckRollup": [_run("smoke", "FAILURE", -50.0)]}

    res = asyncio.run(_gather(monkeypatch, fake_gh))
    assert res["stalled"] == []


# --- failing_check_names -----------------------------------------------------


@pytest.mark.parametrize(
    "node,expected",
    [
        ({"name": "s", "conclusion": "FAILURE"}, ["s"]),
        ({"name": "s", "conclusion": "TIMED_OUT"}, ["s"]),
        ({"name": "s", "conclusion": "CANCELLED"}, ["s"]),
        ({"name": "s", "conclusion": "STARTUP_FAILURE"}, ["s"]),
        ({"name": "s", "conclusion": "ACTION_REQUIRED"}, ["s"]),
        ({"name": "s", "conclusion": "SUCCESS"}, []),
        ({"name": "s", "conclusion": "NEUTRAL"}, []),
        ({"name": "s", "conclusion": "SKIPPED"}, []),
        ({"name": "s", "conclusion": "STALE"}, []),
        ({"name": "s", "conclusion": None}, []),  # still running
        ({"context": "legacy", "state": "FAILURE"}, ["legacy"]),
        ({"context": "legacy", "state": "ERROR"}, ["legacy"]),
        ({"context": "legacy", "state": "PENDING"}, []),
        ({"context": "legacy", "state": "EXPECTED"}, []),
        ({"name": "s"}, []),  # neither key present
    ],
)
def test_failing_check_names_shapes(node, expected):
    from agents.vigil.checks.stalled_draft_pr import failing_check_names

    assert failing_check_names([node]) == expected


def test_failing_check_names_rejects_non_list():
    """A dict rollup iterated as a mapping would read a red PR as green."""
    from agents.vigil.checks.stalled_draft_pr import failing_check_names

    assert failing_check_names({"a": {"conclusion": "FAILURE"}}) == []
    assert failing_check_names(None) == []
    assert failing_check_names("nope") == []


def test_failing_check_names_tolerates_junk_nodes():
    from agents.vigil.checks.stalled_draft_pr import failing_check_names

    assert failing_check_names(["str", 7, {"conclusion": "FAILURE"}]) == ["?"]


# --- assess ------------------------------------------------------------------


def test_assess_clean_is_ok():
    r = _assess([], drafts_read=3)
    assert r.ok is True
    assert r.detail["stalled_pr_count"] == 0
    assert not r.fingerprint_key


def test_assess_flags_stalled_pr():
    r = _assess([_stalled(1511, 23.0, failing=("smoke", "Analyze (python)"))])
    assert r.ok is False
    assert r.severity == "warning"
    assert r.fingerprint_key == "stalled_draft_pr"
    assert "cirwel/unitares#1511" in r.summary
    assert "23h" in r.summary and "smoke" in r.summary
    assert r.detail["stalled_pr_count"] == 1


def test_assess_names_overflow_failing_checks_instead_of_hiding_them():
    r = _assess([_stalled(1, 30.0, failing=("a", "b", "c", "d", "e"))])
    assert "a, b, c, +2 more" in r.summary
    assert r.detail["stalled_prs"][0]["failing"] == ["a", "b", "c", "d", "e"]


def test_assess_reports_worst_first_and_counts_the_rest():
    r = _assess([_stalled(1, 40.0), _stalled(2, 20.0), _stalled(3, 15.0)])
    assert "#1" in r.summary and "+2 more" in r.summary
    assert r.detail["stalled_pr_count"] == 3


@pytest.mark.parametrize(
    "kw,needle",
    [
        ({"search_truncated": True}, "60-PR limit"),
        ({"cap_skipped": 4}, "4 draft(s) beyond the 15-PR inspection cap"),
        ({"unreadable": 2}, "2 draft(s) could not be status-checked"),
        ({"deadline_skipped": 3}, "3 draft(s) dropped at the wall-clock budget"),
    ],
)
def test_assess_reports_each_bound_by_its_own_cause(kw, needle):
    """Each bound needs a distinct message — telling an operator to raise the
    inspection cap when the real cause was a gh auth failure sends them
    chasing the wrong knob."""
    clean = _assess([], **kw)
    firing = _assess([_stalled(1, 30.0)], **kw)
    assert needle in clean.summary
    assert needle in firing.summary


def test_assess_unexamined_total_sums_every_cause():
    r = _assess([], cap_skipped=1, unreadable=2, deadline_skipped=3)
    assert r.detail["stalled_pr_unexamined"] == 6


def test_every_branch_emits_the_same_detail_keys():
    """_collect_health_state merges detail with dict.update(), so any key a
    branch omits silently retains another branch's value."""
    branches = [
        _assess([]),
        _assess([_stalled(1, 30.0)]),
        _assess([], indeterminate=True, indeterminate_reason="x", prev_ok=False),
    ]
    key_sets = [set(b.detail) for b in branches]
    assert key_sets[0] == key_sets[1] == key_sets[2]
    assert branches[0].detail["stalled_prs"] == []


# --- degradation must not flip the transition edge ---------------------------


def test_indeterminate_holds_the_previous_verdict():
    """Regression: returning ok=True on a gh flake fabricated a "Github
    recovered" note and re-paged the same unchanged PR the next cycle."""
    unhealthy = _assess([], indeterminate=True, indeterminate_reason="flake", prev_ok=False)
    healthy = _assess([], indeterminate=True, indeterminate_reason="flake", prev_ok=True)
    assert unhealthy.ok is False
    assert healthy.ok is True
    # ...but it must never *page* on an indeterminate cycle.
    assert unhealthy.fingerprint_key == ""
    assert unhealthy.severity == "info"
    assert unhealthy.detail["stalled_pr_indeterminate"] is True


def test_run_is_indeterminate_when_gh_missing(monkeypatch):
    from agents.vigil.checks import stalled_draft_pr as mod

    monkeypatch.setattr(mod, "GH_BIN", "/nonexistent/gh-binary-for-tests")
    result = asyncio.run(mod.StalledDraftPR().run())
    assert result.ok is True and result.severity == "info"
    assert "indeterminate" in result.summary


def test_run_preserves_unhealthy_across_a_gh_outage(monkeypatch):
    from agents.vigil.checks import stalled_draft_pr as mod

    monkeypatch.setattr(mod, "GH_BIN", "/nonexistent/gh-binary-for-tests")
    result = asyncio.run(mod.StalledDraftPR().run(prev_state={"github_healthy": False}))
    assert result.ok is False  # holds the edge; no false recovery
    assert result.severity == "info"
    assert result.fingerprint_key == ""


@pytest.mark.parametrize(
    "payload",
    [42, "a string", ["not", "an", "object"], True, {"statusCheckRollup": "junk"}],
)
def test_no_malformed_payload_escapes_as_a_critical_page(monkeypatch, payload):
    """runner.py turns a bare exception into severity='critical'. This module
    promises the opposite: unreadable input must stay quiet."""
    from agents.vigil.checks import stalled_draft_pr as mod

    async def fake_gh(args, timeout):
        return [_pr(1)] if args[0] == "search" else payload

    monkeypatch.setattr(mod, "_gh_json", fake_gh)
    result = asyncio.run(mod.StalledDraftPR().run())
    assert result.severity in ("info", "warning")
    assert result.severity != "critical"


def test_run_via_runner_never_reports_crashed(monkeypatch):
    """End-to-end through the real runner, which is what actually pages."""
    from agents.vigil.checks import registry, runner
    from agents.vigil.checks import stalled_draft_pr as mod

    async def hostile(args, timeout):
        return [_pr(1)] if args[0] == "search" else 42

    monkeypatch.setattr(mod, "_gh_json", hostile)
    registry.register(mod.StalledDraftPR())
    results = asyncio.run(runner.run_health_checks({}))
    _, res = results[0]
    assert res.fingerprint_key != "github_crashed"
    assert res.severity != "critical"


# --- bound accounting --------------------------------------------------------


def test_unreadable_pr_is_not_counted_as_read(monkeypatch):
    """Regression: the previous version counted a PR as both examined and
    skipped, so 2 candidates could report "2 checked" and "2 not checked"."""
    calls = {"n": 0}

    async def fake_gh(args, timeout):
        if args[0] == "search":
            return [_pr(1), _pr(2)]
        calls["n"] += 1
        if calls["n"] == 1:
            raise __import__(
                "agents.vigil.checks.stalled_draft_pr", fromlist=["x"]
            ).GhUnavailable("no access")
        return {"statusCheckRollup": [_run("smoke", "FAILURE", 30.0)]}

    res = asyncio.run(_gather(monkeypatch, fake_gh))
    assert res["drafts_read"] == 1
    assert res["unreadable"] == 1
    assert res["drafts_read"] + res["unreadable"] == 2  # counters partition
    assert len(res["stalled"]) == 1


def test_malformed_search_row_is_counted_not_dropped(monkeypatch):
    async def fake_gh(args, timeout):
        if args[0] == "search":
            return [_pr(1), {"number": None}, "junk", {"repository": "str", "number": 3}]
        return {"statusCheckRollup": []}

    res = asyncio.run(_gather(monkeypatch, fake_gh))
    assert res["unreadable"] == 3  # three malformed rows, none silently vanished
    assert res["drafts_read"] == 1


def test_cap_skipped_is_separate_from_unreadable(monkeypatch):
    async def fake_gh(args, timeout):
        if args[0] == "search":
            return [_pr(i) for i in range(5)]
        return {"statusCheckRollup": []}

    res = asyncio.run(_gather(monkeypatch, fake_gh, max_inspect=2))
    assert res["cap_skipped"] == 3
    assert res["unreadable"] == 0
    assert res["drafts_read"] == 2


def test_search_truncation_flagged_at_limit(monkeypatch):
    async def fake_gh(args, timeout):
        if args[0] == "search":
            return [_pr(i) for i in range(3)]
        return {"statusCheckRollup": []}

    res = asyncio.run(_gather(monkeypatch, fake_gh, search_limit=3, max_inspect=0))
    assert res["search_truncated"] is True


def test_search_returning_non_list_is_unavailable(monkeypatch):
    from agents.vigil.checks import stalled_draft_pr as mod

    async def fake_gh(args, timeout):
        return {"message": "Not Found"}

    with pytest.raises(mod.GhUnavailable):
        asyncio.run(_gather(monkeypatch, fake_gh))


# --- the cycle budget --------------------------------------------------------


def test_sweep_respects_the_wall_clock_budget(monkeypatch):
    """The load-bearing bound. Vigil's whole cycle is 120s and a sibling step
    already reserves 60s; an unbounded sweep would abort the cycle, losing the
    state write, the check-in, and every other check's result."""

    async def slow_gh(args, timeout):
        if args[0] == "search":
            return [_pr(i) for i in range(12)]
        await asyncio.sleep(5)
        return {"statusCheckRollup": []}

    import time as _time

    t0 = _time.monotonic()
    res = asyncio.run(_gather(monkeypatch, slow_gh, budget=1.0, timeout=5.0, concurrency=4))
    elapsed = _time.monotonic() - t0

    assert elapsed < 3.0, f"sweep ran {elapsed:.1f}s against a 1s budget"
    assert res["deadline_skipped"] > 0
    assert res["drafts_read"] < 12


def test_deadline_drops_are_reported_not_hidden(monkeypatch):
    async def slow_gh(args, timeout):
        if args[0] == "search":
            return [_pr(i) for i in range(6)]
        await asyncio.sleep(5)
        return {"statusCheckRollup": []}

    res = asyncio.run(_gather(monkeypatch, slow_gh, budget=0.5, timeout=5.0))
    r = _assess([], deadline_skipped=res["deadline_skipped"])
    assert "dropped at the wall-clock budget" in r.summary


def test_timeout_kills_the_process_group(monkeypatch):
    """A cancelled or timed-out gh must not leave a live process behind."""
    from agents.vigil.checks import stalled_draft_pr as mod

    monkeypatch.setattr(mod, "GH_BIN", "/bin/sleep")
    with pytest.raises(mod.GhUnavailable):
        asyncio.run(mod._gh_json(["987654"], 0.5))

    import subprocess

    survivors = subprocess.run(
        ["pgrep", "-f", "sleep 987654"], capture_output=True, text=True
    ).stdout.strip()
    assert not survivors, f"orphaned process(es): {survivors}"


def test_empty_gh_output_is_unavailable_not_empty_list(monkeypatch):
    """rc=0 with no stdout is gh not answering; reading it as [] would report
    "no drafts exist" and silently suppress every finding."""
    from agents.vigil.checks import stalled_draft_pr as mod

    monkeypatch.setattr(mod, "GH_BIN", "/usr/bin/true")
    with pytest.raises(mod.GhUnavailable):
        asyncio.run(mod._gh_json(["anything"], 5.0))
