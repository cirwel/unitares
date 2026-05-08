"""Regression tests for the Watcher agent's dedup and fingerprinting.

Background: on 2026-04-11, immediately after shipping the Watcher agent
(commit 98a7ae2), Ogler flagged two latent bugs in the watcher itself:

1. ``FINDINGS_TTL_DAYS = 14`` was defined at watcher_agent.py:78 but never
   enforced by ``persist_findings`` at :496 — the dedup dict would grow
   unboundedly over months. This is the exact P002 pattern the watcher's
   own library warns about.

2. ``_compute_fingerprint`` at :127 hashed only ``pattern|file|line`` with
   no content component. If a bug at line 47 was fixed and a DIFFERENT bug
   arrived at the same line 47 later, the watcher would silently dedup it
   as a rerun and never surface it — a false negative.

Both fixes shipped in the same commit as these tests, per the project
standing rule "every behavioral change ships with tests covering the new
behavior" (see ~/.claude memory feedback_tests-with-fixes.md).
"""

from __future__ import annotations

import importlib.util
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest


# ---------------------------------------------------------------------------
# Module loading — mirrors tests/test_sentinel_cycle_timeout.py
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def watcher_module():
    """Load ``agents/watcher/agent.py`` as a module without executing
    its ``__main__`` block."""
    project_root = Path(__file__).resolve().parent.parent.parent.parent
    module_path = project_root / "agents" / "watcher" / "agent.py"
    spec = importlib.util.spec_from_file_location("watcher_agent", module_path)
    assert spec and spec.loader, "could not load watcher_agent module"
    module = importlib.util.module_from_spec(spec)
    sys.modules["watcher_agent"] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture(autouse=True)
def _isolate_watcher_state(tmp_path, monkeypatch, watcher_module):
    """Redirect all Watcher state paths into a tmp dir so tests never touch
    the production findings.jsonl / dedup.json / log file.

    Constants live in the split-out modules (findings, _util) as of the
    watcher-findings-split refactor, so we patch them at the source. The
    watcher_module re-exports are also patched for tests that read
    ``watcher_module.FINDINGS_FILE`` etc. directly.
    """
    from agents.watcher import _util as watcher_util
    from agents.watcher import agent as real_watcher
    from agents.watcher import findings as watcher_findings

    tmp_state = tmp_path / "watcher-state"
    tmp_state.mkdir()
    tmp_log = tmp_path / "watcher.log"

    # Source modules (where the constants now live) — the canonical patch
    # points; findings.py's own functions read these, not the re-exports.
    monkeypatch.setattr(watcher_findings, "STATE_DIR", tmp_state)
    monkeypatch.setattr(watcher_findings, "FINDINGS_FILE", tmp_state / "findings.jsonl")
    monkeypatch.setattr(watcher_findings, "DEDUP_FILE", tmp_state / "dedup.json")
    monkeypatch.setattr(watcher_util, "LOG_FILE", tmp_log)

    # Re-exported names on the agent module — patched so any caller that
    # reads them through `watcher_module.FINDINGS_FILE` / the real agent
    # module still sees the tmp paths.
    for target in (watcher_module, real_watcher):
        monkeypatch.setattr(target, "STATE_DIR", tmp_state)
        monkeypatch.setattr(target, "FINDINGS_FILE", tmp_state / "findings.jsonl")
        monkeypatch.setattr(target, "DEDUP_FILE", tmp_state / "dedup.json")
        monkeypatch.setattr(target, "LOG_FILE", tmp_log)

    # Disable session-scope auto-discovery in tests by default so seeded
    # findings (whose file paths point outside the tmp tree) keep their
    # legacy "always in-scope" behavior. Tests that exercise the scoping
    # logic itself pass `scope_root=...` explicitly.
    monkeypatch.setattr(watcher_findings, "_resolve_session_scope_root", lambda *a, **kw: None)
    yield


@pytest.fixture(autouse=True)
def _mock_post_finding_by_default(monkeypatch, watcher_module):
    """Default to no-op post_finding so tests don't hit the network.

    Patches all three namespaces that hold a ``post_finding`` binding:
      - ``watcher_module`` — the importlib-loaded copy used by most tests
      - ``agents.watcher.agent`` — re-exported name used by TestWatcherPostsFindings
      - ``agents.watcher.findings`` — where the binding actually lives post-refactor
        (persist_finding and _post_resolution_event call it from inside findings.py)

    Tests that need to assert on the call should use ``_mock_post_finding``
    below rather than monkeypatching directly — it handles all three
    namespaces so the spy fires regardless of which code path invokes it.
    """
    from agents.watcher import agent as watcher
    from agents.watcher import findings as watcher_findings
    monkeypatch.setattr(watcher_module, "post_finding", lambda **kw: True)
    monkeypatch.setattr(watcher, "post_finding", lambda **kw: True)
    monkeypatch.setattr(watcher_findings, "post_finding", lambda **kw: True)


def _mock_post_finding(monkeypatch, watcher_module, spy):
    """Install ``spy`` as post_finding on all three namespaces."""
    from agents.watcher import agent as watcher
    from agents.watcher import findings as watcher_findings
    monkeypatch.setattr(watcher_module, "post_finding", spy)
    monkeypatch.setattr(watcher, "post_finding", spy)
    monkeypatch.setattr(watcher_findings, "post_finding", spy)


def _mock_escalate_to_kg(monkeypatch, watcher_module, spy):
    """Install ``spy`` as _escalate_to_kg on all namespaces that hold the binding."""
    from agents.watcher import agent as watcher
    from agents.watcher import findings as watcher_findings
    monkeypatch.setattr(watcher_module, "_escalate_to_kg", spy)
    monkeypatch.setattr(watcher, "_escalate_to_kg", spy)
    monkeypatch.setattr(watcher_findings, "_escalate_to_kg", spy)


def _mock_watcher_identity(monkeypatch, watcher_module, identity):
    """Set ``_watcher_identity`` on both the importlib copy and the real agent
    module. Needed because findings.update_finding_status does a lazy import
    from ``agents.watcher.agent`` — patches on the watcher_module copy alone
    miss the real module's binding."""
    from agents.watcher import agent as watcher
    monkeypatch.setattr(watcher_module, "_watcher_identity", identity)
    monkeypatch.setattr(watcher, "_watcher_identity", identity)


# ---------------------------------------------------------------------------
# hash_line_content
# ---------------------------------------------------------------------------


def test_hash_line_content_is_stable_across_leading_whitespace(watcher_module):
    """Indent-only differences must not change the content hash, so
    reformatting (e.g. a linter adjusting indentation) doesn't re-fire
    every finding in the touched region."""
    h_indented = watcher_module.hash_line_content("    asyncio.create_task(x.run())")
    h_tight = watcher_module.hash_line_content("asyncio.create_task(x.run())")
    h_trailing = watcher_module.hash_line_content("asyncio.create_task(x.run())   ")
    assert h_indented == h_tight == h_trailing


def test_hash_line_content_differs_for_different_code(watcher_module):
    """Different code at the same line must hash differently."""
    h_a = watcher_module.hash_line_content("asyncio.create_task(x.run())")
    h_b = watcher_module.hash_line_content("task = asyncio.create_task(x.run())")
    assert h_a != h_b


def test_hash_line_content_handles_empty(watcher_module):
    """Empty / missing source lines must produce a stable, non-crashing
    hash (callers rely on it as a fingerprint component)."""
    assert watcher_module.hash_line_content("") == watcher_module.hash_line_content(
        "   "
    )
    assert watcher_module.hash_line_content(None) == watcher_module.hash_line_content(
        ""
    )


# ---------------------------------------------------------------------------
# Finding.compute_fingerprint
# ---------------------------------------------------------------------------


def _finding(watcher_module, **overrides):
    """Build a Finding with sensible defaults for fingerprint tests."""
    defaults = dict(
        pattern="P001",
        file="/tmp/foo.py",
        line=47,
        hint="fire-and-forget",
        severity="high",
        detected_at="2026-04-11T00:00:00Z",
        model_used="gemma4:latest",
    )
    defaults.update(overrides)
    return watcher_module.Finding(**defaults)


def test_fingerprint_differs_when_content_hash_changes(watcher_module):
    """The critical regression: same pattern at the same line, but the code
    on that line changed — must produce a different fingerprint so the new
    bug is not silently dedup'd as a rerun of the old one."""
    f_old = _finding(watcher_module, line_content_hash="aaaaaaaaaaaa")
    f_new = _finding(watcher_module, line_content_hash="bbbbbbbbbbbb")
    assert f_old.fingerprint != f_new.fingerprint


def test_fingerprint_stable_for_identical_content(watcher_module):
    """Same pattern, same line, same content → same fingerprint. The
    dedup layer must recognize an identical re-detection and skip it."""
    f_a = _finding(watcher_module, line_content_hash="cafebabe1234")
    f_b = _finding(watcher_module, line_content_hash="cafebabe1234")
    assert f_a.fingerprint == f_b.fingerprint


def test_fingerprint_ignores_non_identifying_fields(watcher_module):
    """detected_at, hint, severity, model_used should not affect
    fingerprint identity — only pattern/file/line/content_hash do."""
    f_a = _finding(
        watcher_module,
        line_content_hash="deadbeefcafe",
        detected_at="2026-04-11T00:00:00Z",
        hint="first hint",
        model_used="gemma4:latest",
    )
    f_b = _finding(
        watcher_module,
        line_content_hash="deadbeefcafe",
        detected_at="2026-04-11T99:99:99Z",
        hint="a different hint entirely",
        model_used="gemma4:26b",
    )
    assert f_a.fingerprint == f_b.fingerprint


# ---------------------------------------------------------------------------
# sweep_stale_dedup — the TTL enforcer
# ---------------------------------------------------------------------------


def _iso(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def test_sweep_drops_entries_older_than_ttl(watcher_module):
    now = datetime(2026, 4, 11, tzinfo=timezone.utc)
    ttl_days = watcher_module.FINDINGS_TTL_DAYS  # 14
    dedup = {
        "fresh1": _iso(now - timedelta(days=1)),
        "fresh2": _iso(now - timedelta(days=ttl_days - 1)),
        "stale1": _iso(now - timedelta(days=ttl_days + 1)),
        "stale2": _iso(now - timedelta(days=90)),
    }
    pruned = watcher_module.sweep_stale_dedup(dedup, ttl_days=ttl_days, now=now)
    assert "fresh1" in pruned
    assert "fresh2" in pruned
    assert "stale1" not in pruned
    assert "stale2" not in pruned
    assert len(pruned) == 2


def test_sweep_empty_dedup_is_a_noop(watcher_module):
    assert watcher_module.sweep_stale_dedup({}) == {}


def test_sweep_preserves_unparseable_timestamps(watcher_module):
    """Fail-open: a corrupted timestamp string should not cause the sweep
    to silently empty the dedup. We'd rather leak a few entries than lose
    real findings."""
    dedup = {
        "fresh": _iso(datetime.now(timezone.utc)),
        "garbage1": "not a timestamp",
        "garbage2": "",
    }
    pruned = watcher_module.sweep_stale_dedup(dedup)
    assert "fresh" in pruned
    assert "garbage1" in pruned
    assert "garbage2" in pruned


def test_sweep_boundary_exactly_at_ttl_is_kept(watcher_module):
    """An entry exactly at the TTL boundary is kept, not dropped. We use
    ``>= cutoff`` in the implementation, so the boundary is inclusive."""
    now = datetime(2026, 4, 11, 12, 0, 0, tzinfo=timezone.utc)
    ttl_days = 14
    boundary = now - timedelta(days=ttl_days)
    dedup = {"boundary": _iso(boundary)}
    pruned = watcher_module.sweep_stale_dedup(dedup, ttl_days=ttl_days, now=now)
    assert "boundary" in pruned


# ---------------------------------------------------------------------------
# persist_findings — end-to-end dedup with TTL enforcement
# ---------------------------------------------------------------------------


def test_persist_findings_invokes_ttl_sweep(watcher_module):
    """persist_findings must sweep the dedup dict on every call so stale
    entries are pruned continuously — not just when the user remembers to
    run a cleanup. The unbounded-growth bug was that this function never
    invoked any sweep at all."""
    now = datetime.now(timezone.utc)
    stale_ts = _iso(now - timedelta(days=watcher_module.FINDINGS_TTL_DAYS + 5))

    # Seed dedup with a stale entry
    watcher_module.save_dedup({"ancient_fingerprint": stale_ts})
    assert "ancient_fingerprint" in watcher_module.load_dedup()

    new_finding = watcher_module.Finding(
        pattern="P001",
        file="/tmp/foo.py",
        line=10,
        hint="fire-and-forget",
        severity="high",
        detected_at=_iso(now),
        model_used="gemma4:latest",
        line_content_hash="1234567890ab",
    )

    fresh = watcher_module.persist_findings([new_finding])
    assert len(fresh) == 1

    dedup_after = watcher_module.load_dedup()
    assert "ancient_fingerprint" not in dedup_after, "TTL sweep did not run"
    assert new_finding.fingerprint in dedup_after, "new finding was not recorded"


def test_persist_findings_dedup_hides_repeat_but_not_content_change(watcher_module):
    """The core regression: two findings at the same (pattern, file, line)
    but DIFFERENT line_content_hash must both get persisted. A third
    finding identical to the first must be dedup'd."""
    base = dict(
        pattern="P001",
        file="/tmp/foo.py",
        line=47,
        hint="fire-and-forget",
        severity="high",
        detected_at="2026-04-11T00:00:00Z",
        model_used="gemma4:latest",
    )
    f_first = watcher_module.Finding(**base, line_content_hash="aaaaaaaaaaaa")
    f_content_change = watcher_module.Finding(
        **base, line_content_hash="bbbbbbbbbbbb"
    )
    f_duplicate = watcher_module.Finding(**base, line_content_hash="aaaaaaaaaaaa")

    # First flight: both distinct findings land; the duplicate is dropped
    fresh = watcher_module.persist_findings([f_first, f_content_change, f_duplicate])
    assert len(fresh) == 2
    fingerprints = {f.fingerprint for f in fresh}
    assert f_first.fingerprint in fingerprints
    assert f_content_change.fingerprint in fingerprints
    assert f_first.fingerprint != f_content_change.fingerprint

    # Second flight: re-submitting all three produces nothing new
    second = watcher_module.persist_findings(
        [f_first, f_content_change, f_duplicate]
    )
    assert second == []


def test_persist_empty_batch_still_lets_sweep_reach_disk(watcher_module):
    """Even when no new findings land, the TTL sweep must write the pruned
    dedup back to disk — otherwise stale entries would resurrect on the
    next scan that DID have findings."""
    now = datetime.now(timezone.utc)
    stale_ts = _iso(now - timedelta(days=watcher_module.FINDINGS_TTL_DAYS + 5))
    watcher_module.save_dedup({"stale": stale_ts})

    fresh = watcher_module.persist_findings([])
    assert fresh == []

    dedup_after = watcher_module.load_dedup()
    assert "stale" not in dedup_after, "sweep result was not persisted to disk"


# ---------------------------------------------------------------------------
# scan_file(persist=...) — self-test isolation
#
# Background: repeatedly running ``watcher_agent.py --self-test`` poured
# synthetic P001 "selftest.py" findings into the real findings.jsonl, which
# the SessionStart hook then surfaced at the top of every new Claude Code
# session. The fix was a ``persist=False`` path through scan_file(), used
# exclusively by the self-test harness so synthetic bug samples never pollute
# the live findings feed.
# ---------------------------------------------------------------------------


def _install_scan_stubs(watcher_module, monkeypatch, findings_to_return):
    """Bypass parse_findings entirely and short-circuit scan_file's model
    pipeline so the test deterministically reaches the persist gate with the
    exact findings list it wants to exercise.

    We patch the internals scan_file calls BEFORE the persist branch:
      - should_skip → never skip
      - read_file_region → canned snippet
      - load_patterns / build_prompt → stubbed
      - call_model → stub response (parse_findings output is discarded)
      - parse_findings → returns our exact fixture list, skipping verification
      - _verify_finding_against_source → always accept

    This eliminates the previous conditional-assertion weakness where a real
    parse failure would silently make the test pass without asserting
    anything.
    """
    monkeypatch.setattr(watcher_module, "should_skip", lambda _p: (False, ""))
    monkeypatch.setattr(
        watcher_module,
        "read_file_region",
        lambda _p, _r=None: ("6:    asyncio.create_task(x.run())", 1, 10),
    )
    monkeypatch.setattr(watcher_module, "load_patterns", lambda: "P001")
    monkeypatch.setattr(watcher_module, "build_prompt", lambda *a, **k: "stub")
    monkeypatch.setattr(
        watcher_module,
        "call_model",
        lambda _p: {"text": "stub", "tokens_used": 0, "model_used": "stub"},
    )
    monkeypatch.setattr(
        watcher_module,
        "parse_findings",
        lambda _text, _fp, _model, _rs: [(f, "stub-evidence") for f in findings_to_return],
    )
    monkeypatch.setattr(
        watcher_module,
        "_verify_finding_against_source",
        lambda _f, _ev, _lines: True,
    )


def _make_fake_finding(watcher_module, pattern="P001", line=6):
    return watcher_module.Finding(
        pattern=pattern,
        file="/tmp/fake.py",
        line=line,
        hint="synthetic fixture",
        severity="high",
        detected_at="2026-04-10T00:00:00Z",
        model_used="stub",
    )


def test_scan_file_persist_false_leaves_findings_file_alone(
    watcher_module, tmp_path, monkeypatch
):
    """persist=False must neither create findings.jsonl nor call persist_findings."""
    fake = _make_fake_finding(watcher_module)
    _install_scan_stubs(watcher_module, monkeypatch, [fake])

    # Spy on persist_findings so we can assert it was NOT invoked.
    persist_calls: list = []
    orig_persist = watcher_module.persist_findings

    def _spy(batch):
        persist_calls.append(list(batch))
        return orig_persist(batch)

    monkeypatch.setattr(watcher_module, "persist_findings", _spy)

    assert not watcher_module.FINDINGS_FILE.exists()

    findings = watcher_module.scan_file("/tmp/does-not-exist.py", persist=False)

    # Hard assertions — no conditional guards.
    assert findings, "stubbed scan_file should have returned the fixture finding"
    assert findings[0].pattern == "P001"
    assert persist_calls == [], "persist=False must not call persist_findings"
    assert not watcher_module.FINDINGS_FILE.exists(), (
        "persist=False must NOT create findings.jsonl"
    )


def test_scan_file_persist_true_still_writes_findings(
    watcher_module, tmp_path, monkeypatch
):
    """persist=True (default) must call persist_findings and append to disk."""
    fake = _make_fake_finding(watcher_module)
    _install_scan_stubs(watcher_module, monkeypatch, [fake])

    assert not watcher_module.FINDINGS_FILE.exists()

    findings = watcher_module.scan_file("/tmp/fake.py", persist=True)

    # Hard assertions — no `if findings:` escape hatch.
    assert findings, "stubbed scan_file should have returned the fixture finding"
    assert watcher_module.FINDINGS_FILE.exists(), (
        "persist=True must create findings.jsonl when findings exist"
    )
    lines = watcher_module.FINDINGS_FILE.read_text().splitlines()
    assert lines, "findings.jsonl should contain at least one entry"
    decoded = [json.loads(l) for l in lines]
    assert all(e["pattern"] == "P001" for e in decoded)
    assert all(e["file"] == "/tmp/fake.py" for e in decoded)


def test_scan_file_persist_default_is_true(watcher_module, tmp_path, monkeypatch):
    """Regression guard: the default must remain persist=True so existing
    callers (the live watcher loop) don't silently lose their feed if someone
    later flips the default."""
    fake = _make_fake_finding(watcher_module)
    _install_scan_stubs(watcher_module, monkeypatch, [fake])

    # Call without the kwarg at all.
    findings = watcher_module.scan_file("/tmp/fake.py")
    assert findings
    assert watcher_module.FINDINGS_FILE.exists()


# ---------------------------------------------------------------------------
# review_file(persist=...) — reasoning-mode persistence
#
# Background: `--review` mode was added 2026-04-12 (commit 327b88cb) to let
# the local model reason freely about bugs instead of pattern-matching. But
# the initial implementation printed findings to stdout and did NOT persist
# them — meaning when the PostToolUse hook piped stdout to /dev/null, every
# review observation vanished. These tests pin the persistence path and its
# hint-aware fingerprint so the watcher stops throwing away evidence.
# ---------------------------------------------------------------------------


def _install_review_stubs(watcher_module, monkeypatch, review_json_text):
    """Short-circuit review_file's pipeline so tests reach the persist gate
    deterministically with a canned model response."""
    monkeypatch.setattr(watcher_module, "should_skip", lambda _p: (False, ""))
    monkeypatch.setattr(
        watcher_module,
        "read_file_region",
        lambda _p, _r=None: ("10:    do_thing()", 1, 20),
    )
    monkeypatch.setattr(
        watcher_module,
        "call_model",
        lambda _p: {"text": review_json_text, "tokens_used": 0, "model_used": "stub-review"},
    )


def test_review_file_persists_findings_to_jsonl(watcher_module, tmp_path, monkeypatch):
    """review_file(persist=True) must append R000 findings to findings.jsonl
    so the hook's piped stdout doesn't silently discard reasoning observations."""
    review_json = json.dumps(
        {"findings": [{"line": 10, "hint": "swallowed exception", "severity": "high"}]}
    )
    _install_review_stubs(watcher_module, monkeypatch, review_json)

    assert not watcher_module.FINDINGS_FILE.exists()

    findings = watcher_module.review_file("/tmp/fake.py")

    assert findings, "review_file should have returned the stubbed finding"
    assert findings[0].pattern == "R000"
    assert watcher_module.FINDINGS_FILE.exists(), (
        "review_file must persist findings — hook redirects stdout to /dev/null"
    )
    decoded = [json.loads(l) for l in watcher_module.FINDINGS_FILE.read_text().splitlines()]
    assert len(decoded) == 1
    assert decoded[0]["pattern"] == "R000"
    assert decoded[0]["hint"] == "swallowed exception"


def test_review_file_persist_false_does_not_write(watcher_module, tmp_path, monkeypatch):
    """persist=False path exists for the same reason scan_file has one —
    self-tests and dry runs must not pollute the live feed."""
    review_json = json.dumps(
        {"findings": [{"line": 10, "hint": "swallowed exception", "severity": "high"}]}
    )
    _install_review_stubs(watcher_module, monkeypatch, review_json)

    findings = watcher_module.review_file("/tmp/fake.py", persist=False)

    assert findings
    assert not watcher_module.FINDINGS_FILE.exists()


def test_review_file_dedup_suppresses_identical_repeat(watcher_module, tmp_path, monkeypatch):
    """Running review twice on the same unchanged observation must only
    surface the finding once. Without hint-aware fingerprinting, review-mode
    findings collapse to a single R000|file|line key and lose content
    awareness — so the fingerprint must include the hint text."""
    review_json = json.dumps(
        {"findings": [{"line": 10, "hint": "unchecked None deref", "severity": "medium"}]}
    )
    _install_review_stubs(watcher_module, monkeypatch, review_json)

    first = watcher_module.review_file("/tmp/fake.py")
    second = watcher_module.review_file("/tmp/fake.py")

    assert len(first) == 1
    assert second == [], "identical review observation must dedup on second run"


def test_review_file_different_hints_at_same_line_do_not_collide(
    watcher_module, tmp_path, monkeypatch
):
    """Review mode reports free-form hints — the model can flag two
    different issues on the same line (e.g. 'unchecked None' on one run,
    'possible race' on the next). Without hint-aware fingerprints these
    would silently dedup to a single R000|file|line key. This is the
    exact false-negative shape that motivated content-aware fingerprinting
    for pattern mode in the 2026-04-11 regression set."""
    review_json_a = json.dumps(
        {"findings": [{"line": 10, "hint": "unchecked None deref", "severity": "medium"}]}
    )
    _install_review_stubs(watcher_module, monkeypatch, review_json_a)
    first = watcher_module.review_file("/tmp/fake.py")

    review_json_b = json.dumps(
        {"findings": [{"line": 10, "hint": "possible race on shared dict", "severity": "medium"}]}
    )
    monkeypatch.setattr(
        watcher_module,
        "call_model",
        lambda _p: {"text": review_json_b, "tokens_used": 0, "model_used": "stub-review"},
    )
    second = watcher_module.review_file("/tmp/fake.py")

    assert len(first) == 1
    assert len(second) == 1, (
        "different hint at same line must produce a distinct fingerprint"
    )
    assert first[0].fingerprint != second[0].fingerprint


def test_review_file_escalates_high_severity(watcher_module, tmp_path, monkeypatch):
    """High/critical review findings must route through post_finding just
    like pattern findings — otherwise the Discord bridge never sees them."""
    review_json = json.dumps(
        {"findings": [{"line": 10, "hint": "unauthenticated admin route", "severity": "critical"}]}
    )
    _install_review_stubs(watcher_module, monkeypatch, review_json)

    calls: list[dict] = []
    _mock_post_finding(monkeypatch, watcher_module, lambda **kw: calls.append(kw) or True)

    watcher_module.review_file("/tmp/fake.py")

    assert len(calls) == 1
    assert calls[0]["severity"] == "critical"
    assert calls[0]["agent_id"] == "watcher"
    assert "R000" in calls[0]["message"]


# ---------------------------------------------------------------------------
# Severity routing — critical escalation
# ---------------------------------------------------------------------------


def test_escalate_high_does_not_call_external_targets(watcher_module, monkeypatch):
    finding = _finding(watcher_module, severity="high")
    kg_calls = []

    _mock_escalate_to_kg(monkeypatch, watcher_module, lambda f: kg_calls.append(f))

    watcher_module.escalate(finding)

    assert kg_calls == []


def test_escalate_critical_calls_kg(watcher_module, monkeypatch):
    finding = _finding(watcher_module, severity="critical")
    calls = []

    _mock_escalate_to_kg(monkeypatch, watcher_module, lambda f: calls.append(f))

    watcher_module.escalate(finding)

    assert calls == [finding]


def test_escalate_to_kg_writes_critical_discovery(watcher_module, monkeypatch):
    finding = _finding(watcher_module, severity="critical")
    captured = {}

    class _Resp:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def read(self):
            return b"{}"

    def fake_urlopen(req, timeout=0):
        captured["url"] = req.full_url
        captured["timeout"] = timeout
        captured["payload"] = json.loads(req.data.decode())
        return _Resp()

    monkeypatch.setattr(watcher_module.urllib.request, "urlopen", fake_urlopen)

    watcher_module._escalate_to_kg(finding)

    assert captured["url"] == watcher_module.GOV_REST_URL
    assert captured["timeout"] == 30
    assert captured["payload"]["name"] == "knowledge"
    args = captured["payload"]["arguments"]
    assert args["action"] == "store"
    assert args["discovery_type"] == "bug_found"
    assert args["severity"] == "critical"
    assert "watcher" in args["tags"]
    assert finding.fingerprint in args["details"]


# ---------------------------------------------------------------------------
# Lifecycle commands — Stage 1
#
# These cover the three operations that make findings.jsonl more than an
# append-only log: marking a finding as confirmed/dismissed, sweeping
# findings whose target file vanished, and compacting resolved entries that
# have aged out. Without these, Watcher has no differential signal to report
# to governance and findings.jsonl grows unboundedly (Ogler's P002
# round two).
# ---------------------------------------------------------------------------


def _seed_findings(watcher_module, entries: list[dict]) -> None:
    """Write a raw findings.jsonl directly for tests that want explicit
    control over what's in the file (status, timestamp, file path)."""
    watcher_module.STATE_DIR.mkdir(parents=True, exist_ok=True)
    with watcher_module.FINDINGS_FILE.open("w") as fh:
        for e in entries:
            fh.write(json.dumps(e) + "\n")


def _make_raw_entry(
    fingerprint: str,
    *,
    pattern: str = "P001",
    file: str = str(Path(__file__).resolve()),
    line: int = 10,
    status: str = "open",
    detected_at: str = "2026-04-11T00:00:00Z",
    severity: str = "high",
    hint: str = "fire-and-forget task",
) -> dict:
    return {
        "pattern": pattern,
        "file": file,
        "line": line,
        "hint": hint,
        "severity": severity,
        "detected_at": detected_at,
        "model_used": "gemma4:latest",
        "line_content_hash": "0123456789ab",
        "fingerprint": fingerprint,
        "status": status,
    }


# --- match_fingerprint ------------------------------------------------------


def test_match_fingerprint_rejects_empty_and_too_short(watcher_module):
    findings = [_make_raw_entry("aaaaaaaaaaaaaaaa")]
    for bad in ("", "a", "ab", "abc"):
        matches, err = watcher_module.match_fingerprint(bad, findings)
        assert matches == []
        assert err is not None


def test_match_fingerprint_exact_match(watcher_module):
    findings = [
        _make_raw_entry("aaaaaaaaaaaaaaaa"),
        _make_raw_entry("bbbbbbbbbbbbbbbb"),
    ]
    matches, err = watcher_module.match_fingerprint("aaaaaaaaaaaaaaaa", findings)
    assert err is None
    assert len(matches) == 1
    assert matches[0]["fingerprint"] == "aaaaaaaaaaaaaaaa"


def test_match_fingerprint_unique_prefix(watcher_module):
    findings = [
        _make_raw_entry("aaaaaaaaaaaaaaaa"),
        _make_raw_entry("bbbbbbbbbbbbbbbb"),
    ]
    matches, err = watcher_module.match_fingerprint("aaaa", findings)
    assert err is None
    assert len(matches) == 1
    assert matches[0]["fingerprint"] == "aaaaaaaaaaaaaaaa"


def test_match_fingerprint_ambiguous_prefix(watcher_module):
    findings = [
        _make_raw_entry("aaaa11111111"),
        _make_raw_entry("aaaa22222222"),
    ]
    matches, err = watcher_module.match_fingerprint("aaaa", findings)
    assert err is None
    assert len(matches) == 2


def test_match_fingerprint_no_match(watcher_module):
    findings = [_make_raw_entry("aaaaaaaaaaaaaaaa")]
    matches, err = watcher_module.match_fingerprint("zzzz", findings)
    assert err is None
    assert matches == []


# --- update_finding_status --------------------------------------------------


def test_update_finding_status_marks_by_exact_fingerprint(watcher_module):
    _seed_findings(
        watcher_module,
        [
            _make_raw_entry("aaaaaaaaaaaaaaaa", line=10),
            _make_raw_entry("bbbbbbbbbbbbbbbb", line=20),
        ],
    )
    rc = watcher_module.update_finding_status("aaaaaaaaaaaaaaaa", "confirmed")
    assert rc == 0

    after = watcher_module._iter_findings_raw()
    by_fp = {f["fingerprint"]: f for f in after}
    assert by_fp["aaaaaaaaaaaaaaaa"]["status"] == "confirmed"
    # Untouched finding must keep its status
    assert by_fp["bbbbbbbbbbbbbbbb"]["status"] == "open"


def test_update_finding_status_accepts_unique_prefix(watcher_module):
    _seed_findings(
        watcher_module,
        [
            _make_raw_entry("deadbeef11112222"),
            _make_raw_entry("cafebabe33334444"),
        ],
    )
    rc = watcher_module.update_finding_status("deadbeef", "dismissed")
    assert rc == 0

    after = watcher_module._iter_findings_raw()
    by_fp = {f["fingerprint"]: f for f in after}
    assert by_fp["deadbeef11112222"]["status"] == "dismissed"
    assert by_fp["cafebabe33334444"]["status"] == "open"


def test_update_finding_status_rejects_ambiguous_prefix(watcher_module, capsys):
    _seed_findings(
        watcher_module,
        [
            _make_raw_entry("aaaa11111111"),
            _make_raw_entry("aaaa22222222"),
        ],
    )
    rc = watcher_module.update_finding_status("aaaa", "confirmed")
    assert rc == 1

    # Neither finding mutated
    after = watcher_module._iter_findings_raw()
    assert all(f["status"] == "open" for f in after)
    captured = capsys.readouterr()
    assert "ambiguous" in captured.out


def test_update_finding_status_rejects_unknown_fingerprint(watcher_module, capsys):
    _seed_findings(watcher_module, [_make_raw_entry("aaaaaaaaaaaaaaaa")])
    rc = watcher_module.update_finding_status("zzzzzzzz", "dismissed")
    assert rc == 1
    captured = capsys.readouterr()
    assert "no finding matches" in captured.out


def test_update_finding_status_rejects_too_short_prefix(watcher_module, capsys):
    _seed_findings(watcher_module, [_make_raw_entry("aaaaaaaaaaaaaaaa")])
    rc = watcher_module.update_finding_status("a", "dismissed")
    assert rc == 1
    captured = capsys.readouterr()
    assert "too short" in captured.out
    # Finding untouched
    after = watcher_module._iter_findings_raw()
    assert after[0]["status"] == "open"


def test_update_finding_status_rejects_invalid_status(watcher_module, capsys):
    _seed_findings(watcher_module, [_make_raw_entry("aaaaaaaaaaaaaaaa")])
    rc = watcher_module.update_finding_status("aaaaaaaaaaaaaaaa", "bogus")
    assert rc == 2
    captured = capsys.readouterr()
    assert "invalid status" in captured.out


def test_update_finding_status_handles_missing_file(watcher_module, capsys):
    # No findings.jsonl at all
    assert not watcher_module.FINDINGS_FILE.exists()
    rc = watcher_module.update_finding_status("aaaaaaaaaaaaaaaa", "confirmed")
    assert rc == 1
    captured = capsys.readouterr()
    assert "empty" in captured.out or "absent" in captured.out


def test_update_finding_status_writes_confirmed_at_timestamp(watcher_module):
    """confirmed_at must be set when transitioning to confirmed — the dashboard
    timeline series reads this field; before this fix it was never written and
    the resolved/confirmed line was a flat zero across the whole window."""
    _seed_findings(watcher_module, [_make_raw_entry("aaaaaaaaaaaaaaaa")])
    rc = watcher_module.update_finding_status("aaaaaaaaaaaaaaaa", "confirmed")
    assert rc == 0
    row = watcher_module._iter_findings_raw()[0]
    assert row["status"] == "confirmed"
    assert "confirmed_at" in row
    # ISO 8601 with trailing Z (matches detected_at convention in this file)
    assert row["confirmed_at"].endswith("Z")
    # Sibling timestamp not written for the wrong transition
    assert "dismissed_at" not in row


def test_update_finding_status_writes_dismissed_at_timestamp(watcher_module):
    _seed_findings(watcher_module, [_make_raw_entry("bbbbbbbbbbbbbbbb")])
    rc = watcher_module.update_finding_status("bbbbbbbbbbbbbbbb", "dismissed")
    assert rc == 0
    row = watcher_module._iter_findings_raw()[0]
    assert row["status"] == "dismissed"
    assert row["dismissed_at"].endswith("Z")
    assert "confirmed_at" not in row


def test_update_finding_status_persists_resolver_and_reason(watcher_module):
    """--reason rationale is stored on the finding so future agents can see why
    a prior agent dismissed it instead of re-deriving the judgment from
    nothing."""
    _seed_findings(watcher_module, [_make_raw_entry("ccccccccccccccccc"[:16])])
    rc = watcher_module.update_finding_status(
        "cccccccccccccccc",
        "dismissed",
        resolver_agent_id="uuid-resolver-1",
        reason="false positive: file is a Starlette REST handler, not an MCP tool",
    )
    assert rc == 0
    row = watcher_module._iter_findings_raw()[0]
    assert row["resolved_by"] == "uuid-resolver-1"
    assert "Starlette REST handler" in row["resolution_reason"]


def test_update_finding_status_omits_reason_when_not_provided(watcher_module):
    """No --reason → no resolution_reason key on the finding (we don't want
    'null'/'' littering the schema)."""
    _seed_findings(watcher_module, [_make_raw_entry("dddddddddddddddd")])
    rc = watcher_module.update_finding_status("dddddddddddddddd", "confirmed")
    assert rc == 0
    row = watcher_module._iter_findings_raw()[0]
    assert "resolution_reason" not in row
    assert "resolved_by" not in row


# --- sweep_stale_findings ---------------------------------------------------


def test_sweep_stale_drops_findings_for_missing_files(
    watcher_module, tmp_path, capsys
):
    real = tmp_path / "real.py"
    real.write_text("print('hi')\n")
    missing = tmp_path / "missing.py"  # never created

    _seed_findings(
        watcher_module,
        [
            _make_raw_entry("aaaaaaaa11111111", file=str(real)),
            _make_raw_entry("bbbbbbbb22222222", file=str(missing)),
        ],
    )
    rc = watcher_module.sweep_stale_findings()
    assert rc == 0

    after = watcher_module._iter_findings_raw()
    assert len(after) == 1
    assert after[0]["fingerprint"] == "aaaaaaaa11111111"
    captured = capsys.readouterr()
    assert "dropped 1" in captured.out


def test_sweep_stale_keeps_all_when_all_files_exist(
    watcher_module, tmp_path, capsys
):
    real_a = tmp_path / "a.py"
    real_b = tmp_path / "b.py"
    real_a.write_text("")
    real_b.write_text("")
    _seed_findings(
        watcher_module,
        [
            _make_raw_entry("aaaa11111111aaaa", file=str(real_a)),
            _make_raw_entry("bbbb22222222bbbb", file=str(real_b)),
        ],
    )
    rc = watcher_module.sweep_stale_findings()
    assert rc == 0

    after = watcher_module._iter_findings_raw()
    assert len(after) == 2
    captured = capsys.readouterr()
    assert "nothing to sweep" in captured.out


def test_sweep_stale_handles_empty_findings(watcher_module, capsys):
    rc = watcher_module.sweep_stale_findings()
    assert rc == 0
    captured = capsys.readouterr()
    assert "no findings to sweep" in captured.out


# --- compact_findings -------------------------------------------------------


def test_compact_drops_old_resolved_entries(watcher_module):
    now = datetime(2026, 4, 11, tzinfo=timezone.utc)
    old = _iso(now - timedelta(days=30))
    recent = _iso(now - timedelta(days=1))
    _seed_findings(
        watcher_module,
        [
            _make_raw_entry(
                "old_confirmed__", status="confirmed", detected_at=old
            ),
            _make_raw_entry(
                "old_dismissed__", status="dismissed", detected_at=old
            ),
            _make_raw_entry(
                "old_aged_out___", status="aged_out", detected_at=old
            ),
            _make_raw_entry(
                "recent_confirm_", status="confirmed", detected_at=recent
            ),
        ],
    )
    rc = watcher_module.compact_findings(max_age_days=7, now=now)
    assert rc == 0

    after = watcher_module._iter_findings_raw()
    fps = {f["fingerprint"] for f in after}
    assert "recent_confirm_" in fps
    assert "old_confirmed__" not in fps
    assert "old_dismissed__" not in fps
    assert "old_aged_out___" not in fps


def test_compact_keeps_open_and_surfaced_regardless_of_age(watcher_module):
    now = datetime(2026, 4, 11, tzinfo=timezone.utc)
    ancient = _iso(now - timedelta(days=365))
    _seed_findings(
        watcher_module,
        [
            _make_raw_entry("open_ancient___", status="open", detected_at=ancient),
            _make_raw_entry(
                "surface_ancient", status="surfaced", detected_at=ancient
            ),
        ],
    )
    rc = watcher_module.compact_findings(max_age_days=7, now=now)
    assert rc == 0

    after = watcher_module._iter_findings_raw()
    assert len(after) == 2
    fps = {f["fingerprint"] for f in after}
    assert "open_ancient___" in fps
    assert "surface_ancient" in fps


def test_compact_preserves_entries_with_unparseable_timestamp(watcher_module):
    """Fail-open: garbage timestamps are kept rather than silently dropped."""
    _seed_findings(
        watcher_module,
        [
            _make_raw_entry(
                "bad_timestamp__",
                status="confirmed",
                detected_at="not a date",
            ),
        ],
    )
    rc = watcher_module.compact_findings(max_age_days=1)
    assert rc == 0
    after = watcher_module._iter_findings_raw()
    assert len(after) == 1


def test_compact_noop_on_empty(watcher_module, capsys):
    rc = watcher_module.compact_findings()
    assert rc == 0
    captured = capsys.readouterr()
    assert "no findings" in captured.out


# --- atomic write -----------------------------------------------------------


def test_write_findings_atomic_round_trip(watcher_module):
    entries = [
        _make_raw_entry("aaaa1111aaaa1111", line=1),
        _make_raw_entry("bbbb2222bbbb2222", line=2),
        _make_raw_entry("cccc3333cccc3333", line=3),
    ]
    watcher_module._write_findings_atomic(entries)
    round_trip = watcher_module._iter_findings_raw()
    assert len(round_trip) == 3
    assert [e["fingerprint"] for e in round_trip] == [
        "aaaa1111aaaa1111",
        "bbbb2222bbbb2222",
        "cccc3333cccc3333",
    ]


def test_write_findings_atomic_leaves_no_temp_file(watcher_module):
    watcher_module._write_findings_atomic([_make_raw_entry("aaaa1111aaaa1111")])
    tmp = watcher_module.FINDINGS_FILE.with_suffix(
        watcher_module.FINDINGS_FILE.suffix + ".tmp"
    )
    assert not tmp.exists(), "atomic write must rename, not leave a .tmp sibling"


# ---------------------------------------------------------------------------
# Surfacing — the chime-in path
#
# Two commands back the two hooks that inject findings into the main Claude
# session: --print-unresolved (SessionStart, read-only, shows open+surfaced)
# and --surface-pending (UserPromptSubmit, chime mode, shows only open and
# transitions them to surfaced so the next prompt doesn't repeat them).
# ---------------------------------------------------------------------------


def test_format_findings_block_returns_none_on_empty(watcher_module):
    block, shown = watcher_module._format_findings_block([], header="x")
    assert block is None
    assert shown == []


def test_format_findings_block_suppresses_low_severity(watcher_module):
    """Low-severity findings are file-only signal; they must never show up
    in an injected block because the display cap is already tight and
    session context is precious."""
    findings = [
        _make_raw_entry("low_____00000000", severity="low"),
        _make_raw_entry("low_____11111111", severity="low"),
    ]
    block, shown = watcher_module._format_findings_block(findings, header="x")
    assert block is None
    assert shown == []


def test_format_findings_block_never_hides_critical_or_high(watcher_module):
    """Critical and high findings are never capped — hiding them to save
    context would be more dangerous than a long chime. The display cap
    (10 items) only applies to medium-severity, which is rationed
    against the room left after all critical+high are shown."""
    findings = [
        _make_raw_entry(f"fp_{i:013d}", severity="high")
        for i in range(15)
    ]
    block, shown = watcher_module._format_findings_block(findings, header="x")
    assert block is not None
    shown_count = sum(1 for line in block.splitlines() if line.startswith("  ["))
    assert shown_count == 15, f"all 15 high findings must show; got {shown_count}"
    assert "Total unresolved: 15" in block
    assert len(shown) == 15


def test_format_findings_block_caps_medium_when_criticals_leave_no_room(
    watcher_module,
):
    """With 10+ critical/high findings, medium-severity is fully suppressed
    (no slots left under the 10-item budget reserved for critical+high)."""
    findings = [
        _make_raw_entry(f"hi_{i:013d}", severity="high") for i in range(10)
    ] + [
        _make_raw_entry(f"md_{i:013d}", severity="medium") for i in range(5)
    ]
    block, shown = watcher_module._format_findings_block(findings, header="x")
    assert block is not None
    assert "[HIGH]" in block
    assert "[MEDIUM]" not in block  # no budget left after 10 highs
    # Exactly the 10 highs were shown; none of the 5 mediums
    assert len(shown) == 10
    assert all(f.get("severity") == "high" for f in shown)


def test_format_findings_block_rations_medium_alongside_criticals(
    watcher_module,
):
    """With 3 critical findings, 7 medium slots remain under the 10-item
    budget. A 15-medium queue must be capped to exactly 7."""
    findings = [
        _make_raw_entry(f"crit_{i:011d}", severity="critical") for i in range(3)
    ] + [
        _make_raw_entry(f"med__{i:011d}", severity="medium") for i in range(15)
    ]
    block, shown = watcher_module._format_findings_block(findings, header="x")
    assert block is not None
    med_lines = [l for l in block.splitlines() if "[MEDIUM]" in l]
    crit_lines = [l for l in block.splitlines() if "[CRITICAL]" in l]
    assert len(crit_lines) == 3
    assert len(med_lines) == 7
    assert len(shown) == 10  # 3 criticals + 7 mediums


def test_format_findings_block_prioritizes_critical_over_medium(watcher_module):
    findings = [
        _make_raw_entry("med_____00000000", severity="medium"),
        _make_raw_entry("crit____00000000", severity="critical"),
        _make_raw_entry("high____00000000", severity="high"),
    ]
    block, shown = watcher_module._format_findings_block(findings, header="x")
    assert block is not None
    # Critical should appear before high, which should appear before medium
    crit_pos = block.find("[CRITICAL]")
    high_pos = block.find("[HIGH]")
    med_pos = block.find("[MEDIUM]")
    assert 0 <= crit_pos < high_pos < med_pos


# --- print_unresolved (SessionStart hook, read-only) -----------------------


def test_print_unresolved_shows_both_open_and_surfaced(
    watcher_module, capsys
):
    """Regression: session-start must include findings that were already
    surfaced in a prior session. If it only showed status=='open', the
    chime-transitioned findings would disappear across restarts and the
    new session would start with stale context."""
    _seed_findings(
        watcher_module,
        [
            _make_raw_entry("open____00000000", status="open"),
            _make_raw_entry("surfac__00000000", status="surfaced"),
            _make_raw_entry("confir__00000000", status="confirmed"),
            _make_raw_entry("dismis__00000000", status="dismissed"),
            _make_raw_entry("aged____00000000", status="aged_out"),
        ],
    )
    rc = watcher_module.print_unresolved()
    assert rc == 0
    captured = capsys.readouterr()
    assert "open____" in captured.out
    assert "surfac__" in captured.out
    assert "confir__" not in captured.out
    assert "dismis__" not in captured.out
    assert "aged____" not in captured.out


def test_print_unresolved_does_not_mutate_status(watcher_module):
    _seed_findings(
        watcher_module,
        [_make_raw_entry("open____00000000", status="open")],
    )
    watcher_module.print_unresolved()
    after = watcher_module._iter_findings_raw()
    # Critical: calling --print-unresolved at SessionStart must NEVER change
    # state, otherwise the chime-mode would never have anything new to show.
    assert after[0]["status"] == "open"


def test_print_unresolved_silent_on_empty(watcher_module, capsys):
    rc = watcher_module.print_unresolved()
    assert rc == 0
    captured = capsys.readouterr()
    assert captured.out == ""


def test_print_unresolved_silent_when_only_resolved(watcher_module, capsys):
    _seed_findings(
        watcher_module,
        [
            _make_raw_entry("confir__00000000", status="confirmed"),
            _make_raw_entry("dismis__00000000", status="dismissed"),
        ],
    )
    rc = watcher_module.print_unresolved()
    assert rc == 0
    captured = capsys.readouterr()
    assert captured.out == ""


# --- Worktree scoping (in-scope vs other-worktree footer) -----------------


def test_print_unresolved_partitions_findings_by_scope(watcher_module, tmp_path, capsys):
    """In-scope findings render in the body; out-of-scope ones collapse to a
    single footer line keyed by their `.worktrees/<name>` segment."""
    in_scope_file = str(tmp_path / "in_scope.py")
    out_a = "/some/other/.worktrees/branch-a/src/foo.py"
    out_b = "/some/other/.worktrees/branch-b/src/bar.py"
    _seed_findings(
        watcher_module,
        [
            _make_raw_entry("inside__00000000", status="open", file=in_scope_file),
            _make_raw_entry("brancha_00000000", status="open", file=out_a),
            _make_raw_entry("branchb_00000000", status="open", file=out_b),
            _make_raw_entry("brancha2_0000000", status="surfaced", file=out_a),
        ],
    )
    rc = watcher_module.print_unresolved(scope_root=tmp_path)
    assert rc == 0
    out = capsys.readouterr().out
    assert "inside__" in out
    assert "brancha_" not in out  # out-of-scope, must not appear in body
    assert "branchb_" not in out
    assert "Plus 3 finding(s) in other worktrees" in out
    assert "branch-a=2" in out
    assert "branch-b=1" in out


def test_print_unresolved_emits_footer_only_when_in_scope_empty(
    watcher_module, tmp_path, capsys
):
    """When every finding is out-of-scope, still emit a minimal block so the
    agent knows the backlog exists rather than appearing clean."""
    out_path = "/some/other/.worktrees/branch-x/src/foo.py"
    _seed_findings(
        watcher_module,
        [_make_raw_entry("x_______00000000", status="open", file=out_path)],
    )
    rc = watcher_module.print_unresolved(scope_root=tmp_path)
    assert rc == 0
    out = capsys.readouterr().out
    assert "<unitares-watcher-findings>" in out
    assert "x_______" not in out  # body is empty
    assert "Plus 1 finding(s) in other worktrees" in out
    assert "branch-x=1" in out


def test_print_unresolved_silent_when_no_findings_anywhere(
    watcher_module, tmp_path, capsys
):
    """Empty findings + scope set → no block, same as the unscoped case."""
    rc = watcher_module.print_unresolved(scope_root=tmp_path)
    assert rc == 0
    assert capsys.readouterr().out == ""


def test_partition_with_no_scope_treats_all_as_in_scope(watcher_module):
    """``scope_root=None`` is the documented escape hatch — preserves the
    legacy 'surface everything' behavior for callers without a worktree."""
    findings = [
        _make_raw_entry("a_______00000000", file="/anywhere/foo.py"),
        _make_raw_entry("b_______00000000", file="/elsewhere/bar.py"),
    ]
    in_scope, out_groups = watcher_module._partition_findings_by_scope(findings, None)
    assert len(in_scope) == 2
    assert out_groups == {}


def test_label_for_other_worktree_uses_worktrees_segment(watcher_module):
    assert watcher_module._label_for_other_worktree(
        "/projects/repo/.worktrees/feat-x/src/foo.py"
    ) == "feat-x"


def test_label_for_other_worktree_falls_back_to_parent(watcher_module):
    # No `.worktrees` segment — fall back to deepest dir name.
    assert watcher_module._label_for_other_worktree("/var/log/app/main.py") == "app"
    assert watcher_module._label_for_other_worktree("") == "(unknown)"


# --- surface_pending (UserPromptSubmit hook, chime mode) -------------------


def test_surface_pending_transitions_open_to_surfaced(watcher_module):
    _seed_findings(
        watcher_module,
        [
            _make_raw_entry("open____00000000", status="open"),
            _make_raw_entry("surfac__00000000", status="surfaced"),
        ],
    )
    watcher_module.surface_pending()
    after = {f["fingerprint"]: f["status"] for f in watcher_module._iter_findings_raw()}
    assert after["open____00000000"] == "surfaced"
    # Already-surfaced findings stay surfaced (no-op on them)
    assert after["surfac__00000000"] == "surfaced"


def test_surface_pending_auto_sweeps_stale_before_chime(watcher_module, tmp_path, capsys):
    """surface_pending must drop findings whose target file has vanished
    *before* computing the chime — closes the 2026-05-07 dogfood failure
    mode where 36% of open findings were dangling against deleted worktree
    paths and inflating chime severity rankings.
    """
    real = tmp_path / "real.py"
    real.write_text("print('hi')\n")
    missing = tmp_path / "deleted-worktree" / "ghost.py"  # never created

    _seed_findings(
        watcher_module,
        [
            _make_raw_entry("real____00000000", file=str(real), status="open"),
            _make_raw_entry("ghost___00000000", file=str(missing), status="open"),
        ],
    )

    rc = watcher_module.surface_pending()
    assert rc == 0

    after = {f["fingerprint"]: f["status"] for f in watcher_module._iter_findings_raw()}
    # Real-file finding survives and gets surfaced
    assert after["real____00000000"] == "surfaced"
    # Stale finding is gone — auto-sweep dropped it before the chime ran
    assert "ghost___00000000" not in after

    # Sweep must be silent: chime stdout contains the real finding only,
    # not "dropped N findings" CLI text.
    captured = capsys.readouterr()
    assert "ghost.py" not in captured.out
    assert "dropped" not in captured.out
    assert str(real) in captured.out


def test_surface_pending_only_prints_when_there_are_new_open_findings(
    watcher_module, capsys
):
    _seed_findings(
        watcher_module,
        [
            _make_raw_entry("surfac__00000000", status="surfaced"),
            _make_raw_entry("confir__00000000", status="confirmed"),
        ],
    )
    rc = watcher_module.surface_pending()
    assert rc == 0
    captured = capsys.readouterr()
    # No 'open' findings → nothing to chime → empty stdout. The
    # UserPromptSubmit hook must stay silent when there's nothing new.
    assert captured.out == ""


def test_surface_pending_leaves_confirmed_dismissed_untouched(watcher_module):
    _seed_findings(
        watcher_module,
        [
            _make_raw_entry("open____00000000", status="open"),
            _make_raw_entry("confir__00000000", status="confirmed"),
            _make_raw_entry("dismis__00000000", status="dismissed"),
        ],
    )
    watcher_module.surface_pending()
    after = {f["fingerprint"]: f["status"] for f in watcher_module._iter_findings_raw()}
    assert after["open____00000000"] == "surfaced"
    assert after["confir__00000000"] == "confirmed"
    assert after["dismis__00000000"] == "dismissed"


def test_surface_pending_chimes_then_goes_silent_on_second_call(
    watcher_module, capsys
):
    """This is the core chime contract: a fresh open finding chimes once,
    then the next call produces nothing until new open findings arrive.
    Without this, every prompt would re-chime the same findings until the
    user resolved them manually."""
    _seed_findings(
        watcher_module,
        [_make_raw_entry("open____00000000", status="open")],
    )

    # First call — chime fires
    watcher_module.surface_pending()
    first = capsys.readouterr().out
    assert "open____" in first
    assert "unitares-watcher-findings" in first

    # Second call — nothing to chime
    watcher_module.surface_pending()
    second = capsys.readouterr().out
    assert second == ""


def test_surface_pending_new_finding_after_chime_still_fires(
    watcher_module, capsys
):
    _seed_findings(
        watcher_module,
        [_make_raw_entry("first___00000000", status="open")],
    )
    watcher_module.surface_pending()
    capsys.readouterr()  # discard

    # Simulate a background scan adding a new open finding
    existing = watcher_module._iter_findings_raw()
    existing.append(_make_raw_entry("second__00000000", status="open"))
    watcher_module._write_findings_atomic(existing)

    watcher_module.surface_pending()
    captured = capsys.readouterr().out
    assert "second__" in captured
    # First finding was already surfaced and must NOT re-chime
    assert "first___" not in captured


# ---------------------------------------------------------------------------
# Ogler round-3 self-review fixes — 2026-04-11
#
# After shipping the Qwen3-Coder-Next model upgrade, Ogler flagged five
# latent issues in the Watcher code itself:
#
#   1. DEFAULT_CONTEXT_LINES=200 was a gemma4-era truncation that hid
#      anything past line 200 of the file from the scan. Qwen3 has a
#      256K context window — scanning whole files is cheap.
#
#   3. ~/Library/Logs/unitares-watcher.log was unbounded append — same
#      P002 pattern (round three) the Watcher's own library warns about.
#
#   4. surface_pending marked ALL open findings as surfaced, including
#      medium-severity findings the display cap had hidden. Combined with
#      content-hash dedup, those findings became silent drops — the user
#      never saw them but they'd never re-chime either.
#
# Fixes #2 (temperature 0.1 → 0.0) and #5 (max_tokens 2048 → 1024) are
# config constants and aren't exercised by unit tests directly; they're
# asserted by inspection below as a regression guard against drift.
# ---------------------------------------------------------------------------


def test_read_file_region_scans_whole_file_by_default(
    watcher_module, tmp_path
):
    """Regression for #1: DEFAULT_CONTEXT_LINES must be large enough that
    a typical source file is scanned end-to-end, not truncated at 200
    lines. A file that passes should_skip's 256KB cap should be scanned
    in full."""
    f = tmp_path / "longfile.py"
    f.write_text("\n".join(f"line_{i}" for i in range(500)))
    text, start, end = watcher_module.read_file_region(str(f))
    assert start == 1
    assert end == 500, (
        f"whole file should be scanned (500 lines); got end={end} — "
        "is DEFAULT_CONTEXT_LINES still at 200?"
    )
    assert "line_0" in text
    assert "line_499" in text


def test_default_context_lines_is_large_enough_for_typical_files(
    watcher_module,
):
    """Guard against re-regression to the gemma4-era 200-line cap. Must
    be at least 2000 to cover typical source files end-to-end; 10000 is
    the current target."""
    assert watcher_module.DEFAULT_CONTEXT_LINES >= 2000, (
        "DEFAULT_CONTEXT_LINES regressed to a small value — Qwen3 has "
        "a 256K context window and should_skip caps at 256KB; scanning "
        "whole files is the point."
    )


def test_model_call_uses_deterministic_temperature(watcher_module):
    """Regression for #2: detector workload wants temperature=0.0, not
    0.1. Asserts the constant hasn't drifted back to the creative-writing
    value."""
    import inspect

    src = inspect.getsource(watcher_module.call_ollama)
    assert '"temperature": 0.0' in src, (
        "call_ollama lost temperature=0.0 — detector must be deterministic"
    )
    assert "temperature=0.1" not in src and '"temperature": 0.1' not in src, (
        "call_ollama regressed to temperature=0.1 — "
        "Ogler caught this once, do not re-ship"
    )


def test_model_call_max_tokens_is_not_wasteful(watcher_module):
    """Regression for #5: max_tokens should be right-sized for Qwen3's
    ~40-tokens-per-finding economy, not gemma4's 2048-era budget."""
    import inspect

    src = inspect.getsource(watcher_module.call_ollama)
    assert '"max_tokens": 2048' not in src, (
        "call_ollama still has 2048 — trim to the Qwen3 economy"
    )


# --- Log rotation (#3) ------------------------------------------------------


def test_rotate_log_trims_to_max_lines(
    watcher_module, tmp_path, monkeypatch
):
    """The log file must be trimmed to the last MAX_LOG_LINES entries
    when it exceeds the cap. Without this, the Watcher's own log file
    was an unbounded P002 self-match — round three of the same pattern
    Ogler has caught in this codebase."""
    log = tmp_path / "rot.log"
    log.write_text("\n".join(f"line_{i}" for i in range(50)) + "\n")

    watcher_module._common_trim_log(log, 10)

    remaining = log.read_text().splitlines()
    assert len(remaining) == 10, f"expected 10 lines after rotation, got {len(remaining)}"
    # Must keep the TAIL (most recent entries), not the head
    assert remaining[0] == "line_40"
    assert remaining[-1] == "line_49"


def test_rotate_log_noop_when_under_limit(
    watcher_module, tmp_path, monkeypatch
):
    """A log file smaller than MAX_LOG_LINES should not be rewritten."""
    log = tmp_path / "small.log"
    content = "line_0\nline_1\nline_2\n"
    log.write_text(content)
    mtime_before = log.stat().st_mtime_ns

    watcher_module._common_trim_log(log, 100)

    assert log.read_text() == content
    # Additionally assert the file wasn't rewritten (mtime unchanged).
    # On some filesystems this may not be reliable, but it's a useful guard.
    assert log.stat().st_mtime_ns == mtime_before


def test_rotate_log_missing_file_is_safe(
    watcher_module, tmp_path, monkeypatch
):
    """A missing log file must not raise — this runs on every scan_file
    entry and fire-and-forget hooks shouldn't crash on a fresh install."""
    log = tmp_path / "nonexistent.log"
    # Must not raise
    watcher_module._common_trim_log(log, 5000)
    assert not log.exists()


def test_max_log_lines_has_a_sane_upper_bound(watcher_module):
    """Guard against MAX_LOG_LINES being removed or set to an absurd
    value that defeats the rotation."""
    assert 100 <= watcher_module.MAX_LOG_LINES <= 100000, (
        f"MAX_LOG_LINES={watcher_module.MAX_LOG_LINES} is outside "
        "the sane operational range"
    )


# --- surface_pending silent-drop fix (#4) ----------------------------------


def test_surface_pending_does_not_silently_drop_hidden_mediums(
    watcher_module,
):
    """Regression for the silent-drop bug: when the display cap (10
    items, reserved first for critical/high) hides medium-severity
    findings, those mediums must stay `open` so they appear on a later
    chime once the queue drains.

    The old behavior was to mark ALL open findings as surfaced regardless
    of whether the display cap had shown them. Combined with the content-
    hash dedup, that silently dropped real findings — the user never saw
    them AND they'd never re-appear.
    """
    # Fill the 10-slot display cap with highs, plus 5 extra mediums
    # that should NOT be shown
    entries = [
        _make_raw_entry(f"hi_{i:013d}", severity="high", status="open")
        for i in range(10)
    ]
    entries += [
        _make_raw_entry(f"md_{i:013d}", severity="medium", status="open")
        for i in range(5)
    ]
    _seed_findings(watcher_module, entries)

    watcher_module.surface_pending()

    by_fp = {f["fingerprint"]: f for f in watcher_module._iter_findings_raw()}

    # All 10 highs were displayed → transitioned to surfaced
    for i in range(10):
        assert by_fp[f"hi_{i:013d}"]["status"] == "surfaced", (
            f"high finding {i} should have been surfaced (displayed)"
        )

    # None of the 5 mediums were displayed → must remain open
    for i in range(5):
        assert by_fp[f"md_{i:013d}"]["status"] == "open", (
            f"medium finding {i} was silently dropped — it wasn't "
            "displayed but got marked surfaced anyway"
        )


# --- call_model / call_ollama: direct path + env-var config ---


def test_call_model_delegates_to_call_ollama(watcher_module, monkeypatch):
    """call_model is now a thin wrapper over call_ollama (governance path
    was removed — it had a 30s server-side ceiling and dropped token
    counts, providing no Watcher-relevant signal)."""

    captured: dict[str, Any] = {}

    def _fake_ollama(prompt, model, timeout):
        captured["prompt"] = prompt
        captured["model"] = model
        captured["timeout"] = timeout
        return {"text": "ok", "model_used": model, "tokens_used": 7}

    monkeypatch.setattr(watcher_module, "call_ollama", _fake_ollama)

    result = watcher_module.call_model("scan me", "m", timeout=5)

    assert result == {"text": "ok", "model_used": "m", "tokens_used": 7}
    assert captured == {"prompt": "scan me", "model": "m", "timeout": 5}


def test_call_ollama_posts_to_configured_url(watcher_module, monkeypatch):
    """call_ollama POSTs to module-level OLLAMA_URL (env-driven) with the
    prompt, model, and timeout it was given."""

    captured: dict[str, Any] = {}
    fake_payload = {
        "choices": [{"message": {"content": "resp"}}],
        "model": "stub-model",
        "usage": {"total_tokens": 99},
    }

    class _FakeResp:
        def __init__(self, payload):
            self._body = json.dumps(payload).encode()

        def read(self):
            return self._body

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    def _fake_urlopen(req, timeout=None):
        captured["url"] = req.full_url
        captured["body"] = json.loads(req.data.decode())
        captured["timeout"] = timeout
        return _FakeResp(fake_payload)

    monkeypatch.setattr(watcher_module.urllib.request, "urlopen", _fake_urlopen)

    result = watcher_module.call_ollama("prompt text", "m", timeout=11)

    assert result["text"] == "resp"
    assert result["tokens_used"] == 99
    assert captured["url"] == watcher_module.OLLAMA_URL
    assert captured["timeout"] == 11
    assert captured["body"]["model"] == "m"
    assert captured["body"]["temperature"] == 0.0


def test_env_vars_override_defaults(monkeypatch, tmp_path):
    """WATCHER_MODEL / WATCHER_TIMEOUT / WATCHER_OLLAMA_URL are read at
    module load. Load the file under a fresh module name to avoid
    disturbing the cached watcher_module / agents.watcher.agent imports."""
    import importlib.util

    monkeypatch.setenv("WATCHER_MODEL", "gemma4:latest")
    monkeypatch.setenv("WATCHER_TIMEOUT", "123")
    monkeypatch.setenv(
        "WATCHER_OLLAMA_URL", "http://ollama.example:11434/v1/chat/completions"
    )

    project_root = Path(__file__).resolve().parent.parent.parent.parent
    module_path = project_root / "agents" / "watcher" / "agent.py"
    spec = importlib.util.spec_from_file_location(
        "watcher_agent_env_test", module_path
    )
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    # dataclass resolution at module load looks the module up in sys.modules,
    # so we have to register it before exec_module and clean up after.
    sys.modules["watcher_agent_env_test"] = mod
    try:
        spec.loader.exec_module(mod)

        assert mod.DEFAULT_MODEL == "gemma4:latest"
        assert mod.DEFAULT_TIMEOUT == 123
        assert mod.OLLAMA_URL == "http://ollama.example:11434/v1/chat/completions"
    finally:
        sys.modules.pop("watcher_agent_env_test", None)


def test_default_timeout_is_at_least_90(watcher_module):
    """45s was too tight for qwen3-coder-next:79B and caused systematic
    timeouts; 90s is the new floor."""
    assert watcher_module.DEFAULT_TIMEOUT >= 90, (
        "DEFAULT_TIMEOUT regressed below 90s — 79B scans need headroom"
    )


def test_surface_pending_second_chime_picks_up_previously_hidden_mediums(
    watcher_module, capsys
):
    """With the silent-drop fix: if a first chime hides mediums behind a
    wall of highs, and those highs later get resolved/dismissed, the
    mediums should re-surface on the next chime."""
    # First chime: 10 highs + 3 mediums (mediums hidden)
    entries = [
        _make_raw_entry(f"hi_{i:013d}", severity="high", status="open")
        for i in range(10)
    ]
    entries += [
        _make_raw_entry(f"md_{i:013d}", severity="medium", status="open")
        for i in range(3)
    ]
    _seed_findings(watcher_module, entries)

    watcher_module.surface_pending()
    first = capsys.readouterr().out
    assert "[HIGH]" in first
    assert "[MEDIUM]" not in first

    # Resolve all 10 highs (user acted on them)
    for i in range(10):
        watcher_module.update_finding_status(f"hi_{i:013d}", "confirmed")
    capsys.readouterr()  # discard resolve output

    # Second chime: highs are resolved, mediums should now appear
    watcher_module.surface_pending()
    second = capsys.readouterr().out
    assert "[MEDIUM]" in second, (
        "previously-hidden mediums should resurface once the "
        "critical/high queue drains"
    )
    assert "[HIGH]" not in second  # all resolved


# ---------------------------------------------------------------------------
# Escalation: critical findings → KG discovery
# ---------------------------------------------------------------------------


class TestEscalation:
    """Verify that escalate() stores KG discoveries for critical findings
    and is a no-op for non-critical severities."""

    def _make_finding(self, watcher_module, severity="critical"):
        return watcher_module.Finding(
            pattern="P099",
            file="/tmp/test.py",
            line=42,
            hint="test finding",
            severity=severity,
            detected_at="2026-04-11T12:00:00",
            model_used="test",
        )

    def test_escalate_critical_stores_kg_discovery(self, watcher_module, monkeypatch):
        """Critical findings are stored as KG discoveries."""
        kg_calls = []

        def fake_urlopen(req, timeout=None):
            body = json.loads(req.data.decode())

            class FakeResp:
                def read(self):
                    return b'{"success": true}'
                def __enter__(self):
                    return self
                def __exit__(self, *a):
                    pass

            kg_calls.append(body)
            return FakeResp()

        import urllib.request
        monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)

        finding = self._make_finding(watcher_module, severity="critical")
        watcher_module.escalate(finding)

        assert len(kg_calls) == 1, "should store one KG discovery"
        kg_args = kg_calls[0]["arguments"]
        assert kg_args["action"] == "store"
        assert kg_args["severity"] == "critical"
        assert "P099" in kg_args["summary"]

    def test_escalate_high_skips_kg(self, watcher_module, monkeypatch):
        """Non-critical findings should NOT call KG."""
        calls = []

        def fake_urlopen(req, timeout=None):
            calls.append(req)
            raise AssertionError("should not be called for high severity")

        import urllib.request
        monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)

        finding = self._make_finding(watcher_module, severity="high")
        watcher_module.escalate(finding)  # should return early
        assert len(calls) == 0

    def test_escalate_kg_failure_is_graceful(self, watcher_module, monkeypatch):
        """KG write failure should not crash the watcher."""
        def fake_urlopen(req, timeout=None):
            raise ConnectionError("governance down")

        import urllib.request
        monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)

        finding = self._make_finding(watcher_module, severity="critical")
        watcher_module.escalate(finding)  # should not raise


# ---------------------------------------------------------------------------
# _verify_finding_against_source — regression tests for false-positive drops
#
# Background: on 2026-04-14 the watcher flagged P004 (asyncpg-in-MCP-handler)
# on five lines of src/http_api.py, including an `async def` line and
# unrelated arithmetic. Root cause: P004 had no required-token entry and no
# file-path constraint, so any line in any file could survive verification.
# Separately, P001 (fire-and-forget task leak) could still flag
# `task = loop.create_task(...)` even though the pattern library explicitly
# calls assigned-to-a-name tasks "NOT fire-and-forget".
# ---------------------------------------------------------------------------


def _verify_finding(watcher_module, pattern, line, src_line, file="/repo/src/mcp_handlers/x.py", evidence=""):
    """Helper: run _verify_finding_against_source with a 1-line snippet."""
    f = watcher_module.Finding(
        pattern=pattern, file=file, line=line, hint="t",
        severity="high", detected_at="2026-04-14T00:00:00Z", model_used="t",
    )
    return watcher_module._verify_finding_against_source(
        f, evidence, {line: src_line}
    )


def test_p004_dropped_outside_mcp_handlers_directory(watcher_module):
    """P004 is the asyncpg-in-MCP-handler deadlock pattern. It MUST NOT fire
    on files outside src/mcp_handlers/ (e.g. Starlette REST handlers in
    src/http_api.py), which is exactly what happened on 2026-04-14."""
    assert not _verify_finding(
        watcher_module,
        pattern="P004",
        line=736,
        src_line="async def http_dashboard(request):",
        file="/repo/src/http_api.py",
    )


def test_p004_dropped_when_flagged_line_has_no_asyncpg_marker(watcher_module):
    """Even inside src/mcp_handlers/, P004 requires a literal asyncpg/Redis
    call on the flagged line. `bucket = max(1, min(bucket, 30))` cannot be
    an asyncpg deadlock source and must be dropped."""
    assert not _verify_finding(
        watcher_module,
        pattern="P004",
        line=907,
        src_line="bucket = max(1, min(bucket, 30))",
    )


def test_p004_kept_for_real_asyncpg_call_inside_mcp_handler(watcher_module):
    """Positive case: a real `await conn.fetch(...)` inside src/mcp_handlers/
    MUST survive verification — that's the whole point of P004."""
    assert _verify_finding(
        watcher_module,
        pattern="P004",
        line=42,
        src_line="rows = await conn.fetchrow(query)",
        file="/repo/src/mcp_handlers/identity/handlers.py",
    )


def test_p004_dropped_for_wait_for_guarded_onboard_pin_helper(watcher_module):
    """Redis pin helpers are safe when the public wrapper bounds them with wait_for."""
    f = watcher_module.Finding(
        pattern="P004",
        file="/repo/src/mcp_handlers/identity/session.py",
        line=801,
        hint="t",
        severity="high",
        detected_at="2026-04-29T00:00:00Z",
        model_used="t",
    )
    snippet = {
        779: "        return await asyncio.wait_for(",
        780: "            _lookup_onboard_pin_inner(base_fingerprint, refresh_ttl=refresh_ttl),",
        781: "            timeout=_PIN_REDIS_TIMEOUT,",
        794: "async def _lookup_onboard_pin_inner(base_fingerprint: str, *, refresh_ttl: bool) -> Optional[str]:",
        801: "    pin_data = await raw_redis.get(pin_key)",
    }
    assert not watcher_module._verify_finding_against_source(f, "", snippet)


def test_p004_kept_when_wait_for_does_not_wrap_onboard_pin_helper(watcher_module):
    """An unrelated wait_for near a Redis pin helper must not hide P004."""
    f = watcher_module.Finding(
        pattern="P004",
        file="/repo/src/mcp_handlers/identity/session.py",
        line=801,
        hint="t",
        severity="high",
        detected_at="2026-04-29T00:00:00Z",
        model_used="t",
    )
    snippet = {
        779: "        return await asyncio.wait_for(other_work(), timeout=0.5)",
        794: "async def _lookup_onboard_pin_inner(base_fingerprint: str, *, refresh_ttl: bool) -> Optional[str]:",
        801: "    pin_data = await raw_redis.get(pin_key)",
    }
    assert watcher_module._verify_finding_against_source(f, "", snippet)


def test_p001_dropped_when_task_reference_is_assigned(watcher_module):
    """The pattern library explicitly says that assigning create_task() to a
    named variable means the task is stored — NOT fire-and-forget. The
    verifier must honor that, otherwise legitimate
    `task = loop.create_task(...); _background_tasks.add(task)` patterns
    get flagged."""
    assert not _verify_finding(
        watcher_module,
        pattern="P001",
        line=10,
        src_line="task = loop.create_task(broadcaster.broadcast_event())",
    )


def test_p001_kept_for_true_fire_and_forget(watcher_module):
    """Positive case: a bare `asyncio.create_task(bad())` with no assignment
    is the real fire-and-forget pattern and must survive."""
    assert _verify_finding(
        watcher_module,
        pattern="P001",
        line=10,
        src_line="asyncio.create_task(bad())",
    )


def test_p001_dropped_on_comment_line(watcher_module):
    """A comment mentioning create_task must never be flagged."""
    assert not _verify_finding(
        watcher_module,
        pattern="P001",
        line=10,
        src_line="# avoid create_task(x) here — it leaks",
    )


def test_p005_dropped_for_acquire_then_try_with_unconditional_close(watcher_module):
    """P005 must not fire on the canonical asyncpg `acquire-then-try` idiom:

        conn = await asyncpg.connect(db_url)
        try:
            ...
        finally:
            await conn.close()

    Release is unconditional. Caught when qwen3-coder-next flagged four
    sites in scripts/dev/lease_plane_deprecate.py on 2026-05-01 (issue #268,
    fingerprints ab83f5e0, f67aebf6, 0f4ceac4, 4ba2a281).
    """
    f = watcher_module.Finding(
        pattern="P005",
        file="/repo/scripts/dev/lease_plane_deprecate.py",
        line=98,
        hint="t",
        severity="high",
        detected_at="2026-05-01T00:00:00Z",
        model_used="t",
    )
    snippet = {
        97: "    \"\"\"Phase 0: mark scheme deprecated.\"\"\"",
        98: "    conn = await asyncpg.connect(db_url)",
        99: "    try:",
        100: "        catalog = await _list_catalog_kinds(conn)",
        101: "        ...",
        102: "    finally:",
        103: "        await conn.close()",
    }
    assert not watcher_module._verify_finding_against_source(f, "", snippet)


def test_p005_dropped_for_acquire_then_try_with_blank_line_between(watcher_module):
    """A blank line between the acquire and the try header doesn't change
    the operator's intent — same idiom, just spaced out."""
    f = watcher_module.Finding(
        pattern="P005",
        file="/repo/scripts/dev/x.py",
        line=10,
        hint="t",
        severity="high",
        detected_at="2026-05-01T00:00:00Z",
        model_used="t",
    )
    snippet = {
        10: "    conn = await asyncpg.connect(db_url)",
        11: "",
        12: "    try:",
        13: "        await conn.execute('SELECT 1')",
        14: "    finally:",
        15: "        await conn.close()",
    }
    assert not watcher_module._verify_finding_against_source(f, "", snippet)


def test_p005_dropped_for_acquire_then_try_acquire_variant(watcher_module):
    """The `.acquire(` variant (e.g. pool.acquire()) inside the same shape
    is equally safe — same try/finally guarantees release."""
    f = watcher_module.Finding(
        pattern="P005",
        file="/repo/src/x.py",
        line=20,
        hint="t",
        severity="high",
        detected_at="2026-05-01T00:00:00Z",
        model_used="t",
    )
    snippet = {
        20: "    conn = await pool.acquire()",
        21: "    try:",
        22: "        await conn.fetch('SELECT 1')",
        23: "    finally:",
        24: "        await pool.release(conn)",
    }
    assert not watcher_module._verify_finding_against_source(f, "", snippet)


def test_p005_kept_when_acquire_not_followed_by_try(watcher_module):
    """Negative case: a bare acquire with no try wrapper at all is still a
    real leak risk and must survive verification."""
    f = watcher_module.Finding(
        pattern="P005",
        file="/repo/src/x.py",
        line=10,
        hint="t",
        severity="high",
        detected_at="2026-05-01T00:00:00Z",
        model_used="t",
    )
    snippet = {
        10: "    conn = await asyncpg.connect(db_url)",
        11: "    rows = await conn.fetch('SELECT 1')",
        12: "    return rows",
    }
    assert watcher_module._verify_finding_against_source(f, "", snippet)


def test_p005_kept_when_intervening_statement_between_acquire_and_try(watcher_module):
    """Negative case: if real code sits between the acquire and the try, the
    cancel-between-acquire-and-try window is wider AND the operator's intent
    is no longer the canonical idiom. Keep the finding."""
    f = watcher_module.Finding(
        pattern="P005",
        file="/repo/src/x.py",
        line=10,
        hint="t",
        severity="high",
        detected_at="2026-05-01T00:00:00Z",
        model_used="t",
    )
    snippet = {
        10: "    conn = await asyncpg.connect(db_url)",
        11: "    log.info('connected')",
        12: "    try:",
        13: "        await conn.fetch('SELECT 1')",
        14: "    finally:",
        15: "        await conn.close()",
    }
    assert watcher_module._verify_finding_against_source(f, "", snippet)


def test_p016_dropped_on_typed_attribute_access(watcher_module):
    """P016 is the double-envelope dict-parsing bug. Pure attribute access
    on a typed pydantic model — `if audit_result.success:` — is by
    construction flat and cannot have a hidden nested success. Caught when
    qwen3-coder-next flagged 4 SDK-typed call sites in
    agents/vigil/agent.py:292,308,318,324 on 2026-04-14."""
    for src_line in (
        "if audit_result.success:",
        "if cleanup_result.success:",
        "if not getattr(result, 'success', False):",  # `getattr` not subscript
    ):
        assert not _verify_finding(
            watcher_module,
            pattern="P016",
            line=42,
            src_line=src_line,
            file="/repo/agents/vigil/agent.py",
        ), f"P016 should be dropped for typed attribute access: {src_line!r}"


def test_p016_kept_for_dict_envelope_parse(watcher_module):
    """Positive case: the original incident shape — dict subscript or
    `.get("success")` on a raw response — must survive verification.
    This is the parse_onboard bug from scripts/unitares (commit 718ccd3)."""
    for src_line in (
        'if data.get("success"):',
        'if response["success"]:',
        "if data.get('success') and not data.get('result', {}).get('success'):",
    ):
        assert _verify_finding(
            watcher_module,
            pattern="P016",
            line=42,
            src_line=src_line,
            file="/repo/scripts/unitares",
        ), f"P016 should fire on dict-envelope parse: {src_line!r}"


# ---------------------------------------------------------------------------
# violation_class loading
# ---------------------------------------------------------------------------


def test_load_pattern_violation_classes():
    from agents.watcher.agent import load_pattern_violation_classes
    classes = load_pattern_violation_classes()
    assert classes["P001"] == "ENT"
    assert classes["P004"] == "REC"
    assert classes["P011"] == "INT"
    assert classes["P006"] == "VOI"
    # Every pattern with a severity should also have a violation class
    from agents.watcher.agent import load_pattern_severities
    sevs = load_pattern_severities()
    for pid in sevs:
        assert pid in classes, f"Pattern {pid} has severity but no violation_class"


# ---------------------------------------------------------------------------
# Task 6: Watcher mirrors high/critical findings to the governance event stream
# ---------------------------------------------------------------------------


class TestWatcherPostsFindings:
    def test_high_severity_finding_posts_to_event_stream(self, tmp_path, monkeypatch):
        """After persisting a new high-severity finding to jsonl, Watcher posts to /api/findings."""
        from agents.watcher import agent as watcher
        from agents.watcher import findings as watcher_findings_local

        monkeypatch.setattr(watcher, "FINDINGS_FILE", tmp_path / "findings.jsonl")
        monkeypatch.setattr(watcher, "DEDUP_FILE", tmp_path / "dedup.json")
        monkeypatch.setattr(watcher_findings_local, "FINDINGS_FILE", tmp_path / "findings.jsonl")
        monkeypatch.setattr(watcher_findings_local, "DEDUP_FILE", tmp_path / "dedup.json")
        monkeypatch.setattr(watcher_findings_local, "STATE_DIR", tmp_path)

        calls = []

        def fake_post(**kwargs):
            calls.append(kwargs)
            return True

        _mock_post_finding(monkeypatch, watcher, fake_post)

        finding = watcher.Finding(
            pattern="P011",
            file="/tmp/foo.py",
            line=42,
            hint="mutation before persistence",
            severity="high",
            detected_at="2026-04-15T12:00:00Z",
            model_used="qwen3-coder-next:latest",
            line_content_hash="deadbeef",
            violation_class="INT",
        )
        watcher.persist_finding(finding)

        assert len(calls) == 1
        kwargs = calls[0]
        assert kwargs["event_type"] == "watcher_finding"
        assert kwargs["severity"] == "high"
        assert "P011" in kwargs["message"]
        assert kwargs["fingerprint"] == finding.fingerprint
        assert kwargs["extra"]["file"] == "/tmp/foo.py"
        assert kwargs["extra"]["line"] == 42
        assert (tmp_path / "findings.jsonl").read_text().count("\n") == 1

    def test_low_severity_finding_does_not_post(self, tmp_path, monkeypatch):
        """Low/medium stay local to jsonl — only high/critical hit the stream."""
        from agents.watcher import agent as watcher
        from agents.watcher import findings as watcher_findings_local

        monkeypatch.setattr(watcher, "FINDINGS_FILE", tmp_path / "findings.jsonl")
        monkeypatch.setattr(watcher, "DEDUP_FILE", tmp_path / "dedup.json")
        monkeypatch.setattr(watcher_findings_local, "FINDINGS_FILE", tmp_path / "findings.jsonl")
        monkeypatch.setattr(watcher_findings_local, "DEDUP_FILE", tmp_path / "dedup.json")
        monkeypatch.setattr(watcher_findings_local, "STATE_DIR", tmp_path)

        calls = []
        _mock_post_finding(monkeypatch, watcher,
                            lambda **kw: calls.append(kw) or True)

        finding = watcher.Finding(
            pattern="P002", file="/tmp/foo.py", line=10, hint="unbounded append",
            severity="medium", detected_at="2026-04-15T12:00:00Z",
            model_used="qwen3-coder-next:latest",
            line_content_hash="cafebabe", violation_class="ENT",
        )
        watcher.persist_finding(finding)
        assert calls == []
        assert (tmp_path / "findings.jsonl").read_text().count("\n") == 1

    def test_persist_findings_delegates_posting_per_high_severity_finding(self, tmp_path, monkeypatch):
        """Batch persist_findings calls post_finding for each new high-severity finding."""
        from agents.watcher import agent as watcher
        from agents.watcher import findings as watcher_findings_local

        monkeypatch.setattr(watcher, "FINDINGS_FILE", tmp_path / "findings.jsonl")
        monkeypatch.setattr(watcher, "DEDUP_FILE", tmp_path / "dedup.json")
        monkeypatch.setattr(watcher_findings_local, "FINDINGS_FILE", tmp_path / "findings.jsonl")
        monkeypatch.setattr(watcher_findings_local, "DEDUP_FILE", tmp_path / "dedup.json")
        monkeypatch.setattr(watcher_findings_local, "STATE_DIR", tmp_path)

        calls = []
        _mock_post_finding(monkeypatch, watcher,
                            lambda **kw: calls.append(kw) or True)

        f1 = watcher.Finding(
            pattern="P011", file="/tmp/a.py", line=1, hint="h1",
            severity="high", detected_at="2026-04-15T12:00:00Z",
            model_used="m", line_content_hash="aaaa", violation_class="INT",
        )
        f2 = watcher.Finding(
            pattern="P017", file="/tmp/b.py", line=2, hint="h2",
            severity="critical", detected_at="2026-04-15T12:00:00Z",
            model_used="m", line_content_hash="bbbb", violation_class="BEH",
        )
        watcher.persist_findings([f1, f2])

        assert len(calls) == 2
        assert {c["severity"] for c in calls} == {"high", "critical"}
        assert {c["extra"]["file"] for c in calls} == {"/tmp/a.py", "/tmp/b.py"}


# ---------------------------------------------------------------------------
# Identity resolution
# ---------------------------------------------------------------------------


SESSION_FILE_NAME = ".watcher_session"


class TestWatcherIdentity:
    """Watcher identity resolution: uuid-direct → token → fresh onboard.

    Name-resume was removed 2026-04-17 alongside the server-side name-claim
    deletion. Without name-claim, identity(name="Watcher") forked a fresh
    UUID on every call. PATH 0 (UUID-direct) replaces it as the primary
    resume path.
    """

    def test_fresh_onboard_when_no_session_file(self, watcher_module, tmp_path, monkeypatch):
        """First-ever invocation: no .watcher_session → fresh onboard.

        refuse_fresh_onboard guard (Phase 3, 2026-04-19) requires
        UNITARES_FIRST_RUN=1 to authorize a fresh mint.
        """
        session_file = tmp_path / SESSION_FILE_NAME
        monkeypatch.setattr(watcher_module, "SESSION_FILE", session_file)
        monkeypatch.setenv("UNITARES_FIRST_RUN", "1")

        onboard_called = {}

        class FakeClient:
            client_session_id = "sess-123"
            continuity_token = "tok-abc"
            agent_uuid = "uuid-watcher-001"

            def onboard(self, name, **kwargs):
                onboard_called["name"] = name
                onboard_called["kwargs"] = kwargs
                return type("R", (), {"success": True})()

        watcher_module.resolve_identity(FakeClient())
        identity = watcher_module.get_watcher_identity()

        assert onboard_called["name"] == "Watcher"
        assert onboard_called["kwargs"].get("spawn_reason") == "resident_observer"
        assert identity["agent_uuid"] == "uuid-watcher-001"
        assert session_file.exists()

    def test_uuid_direct_resume_when_agent_uuid_saved(self, watcher_module, tmp_path, monkeypatch):
        """Session file with agent_uuid → PATH 0 UUID-direct. Token/name untouched."""
        session_file = tmp_path / SESSION_FILE_NAME
        session_file.write_text(json.dumps({
            "client_session_id": "old-sess",
            "continuity_token": "old-tok",
            "agent_uuid": "uuid-watcher-001",
        }))
        monkeypatch.setattr(watcher_module, "SESSION_FILE", session_file)

        calls = []

        class FakeClient:
            client_session_id = "new-sess"
            continuity_token = "new-tok"
            agent_uuid = "uuid-watcher-001"

            def identity(self, **kwargs):
                calls.append(("identity", kwargs))
                return type("R", (), {"success": True})()

            def onboard(self, name, **kwargs):
                calls.append(("onboard", name))

        watcher_module.resolve_identity(FakeClient())
        assert calls == [("identity", {"agent_uuid": "uuid-watcher-001", "resume": True})]
        # No onboard, no token fallback — PATH 0 succeeded on first try.

    def test_token_resume_when_uuid_missing(self, watcher_module, tmp_path, monkeypatch):
        """Session file with continuity_token but no agent_uuid → token resume.

        Covers the transition window where an older session file was written
        before Step 0 existed.
        """
        session_file = tmp_path / SESSION_FILE_NAME
        session_file.write_text(json.dumps({
            "client_session_id": "old-sess",
            "continuity_token": "old-tok",
        }))
        monkeypatch.setattr(watcher_module, "SESSION_FILE", session_file)

        identity_called = {}
        onboard_called = {}

        class FakeClient:
            client_session_id = "new-sess"
            continuity_token = "new-tok"
            agent_uuid = "uuid-watcher-001"

            def identity(self, **kwargs):
                identity_called.update(kwargs)
                return type("R", (), {"success": True})()

            def onboard(self, name, **kwargs):
                onboard_called["name"] = name

        watcher_module.resolve_identity(FakeClient())
        assert identity_called.get("continuity_token") == "old-tok"
        assert identity_called.get("resume") is True
        assert not onboard_called

    def test_token_fallback_when_uuid_direct_fails(self, watcher_module, tmp_path, monkeypatch):
        """UUID-direct raises (e.g. agent archived) → fall back to token resume."""
        session_file = tmp_path / SESSION_FILE_NAME
        session_file.write_text(json.dumps({
            "continuity_token": "stale-tok",
            "agent_uuid": "uuid-old",
        }))
        monkeypatch.setattr(watcher_module, "SESSION_FILE", session_file)

        calls = []

        class FakeClient:
            client_session_id = "new-sess"
            continuity_token = "new-tok"
            agent_uuid = "uuid-watcher-001"

            def identity(self, **kwargs):
                calls.append(("identity", kwargs))
                if kwargs.get("agent_uuid"):
                    raise RuntimeError("uuid not found")
                return type("R", (), {"success": True})()

            def onboard(self, name, **kwargs):
                calls.append(("onboard", name))

        watcher_module.resolve_identity(FakeClient())
        assert calls[0] == ("identity", {"agent_uuid": "uuid-old", "resume": True})
        assert calls[1] == ("identity", {"continuity_token": "stale-tok", "resume": True})
        assert len(calls) == 2  # no onboard needed

    def test_fresh_onboard_when_both_uuid_direct_and_token_fail(self, watcher_module, tmp_path, monkeypatch):
        """Both UUID-direct AND token fail → fresh onboard (Step 2).

        refuse_fresh_onboard guard (Phase 3, 2026-04-19) requires
        UNITARES_FIRST_RUN=1 to authorize a fresh mint.
        """
        session_file = tmp_path / SESSION_FILE_NAME
        session_file.write_text(json.dumps({
            "continuity_token": "stale-tok",
            "agent_uuid": "uuid-archived",
        }))
        monkeypatch.setattr(watcher_module, "SESSION_FILE", session_file)
        monkeypatch.setenv("UNITARES_FIRST_RUN", "1")

        calls = []

        class FakeClient:
            client_session_id = "new-sess"
            continuity_token = "new-tok"
            agent_uuid = "uuid-watcher-001"

            def identity(self, **kwargs):
                calls.append(("identity", kwargs))
                raise RuntimeError("both paths dead")

            def onboard(self, name, **kwargs):
                calls.append(("onboard", name))
                return type("R", (), {"success": True})()

        watcher_module.resolve_identity(FakeClient())
        assert calls[0][0] == "identity" and calls[0][1].get("agent_uuid") == "uuid-archived"
        assert calls[1][0] == "identity" and calls[1][1].get("continuity_token") == "stale-tok"
        assert calls[2] == ("onboard", "Watcher")

    def test_governance_down_leaves_identity_none(self, watcher_module, tmp_path, monkeypatch):
        """If governance is unreachable, identity is None — scanning still works.

        Test sets UNITARES_FIRST_RUN=1 to reach the onboard path; the point
        is to verify ConnectionError from onboard is caught cleanly.
        """
        session_file = tmp_path / SESSION_FILE_NAME
        monkeypatch.setattr(watcher_module, "SESSION_FILE", session_file)
        monkeypatch.setenv("UNITARES_FIRST_RUN", "1")

        class FakeClient:
            def onboard(self, *a, **kw):
                raise ConnectionError("governance down")

            def identity(self, **kw):
                raise ConnectionError("governance down")

        watcher_module.resolve_identity(FakeClient())
        assert watcher_module.get_watcher_identity() is None


class TestSessionAnchor:
    """SESSION_FILE is a host-scoped anchor, not a per-worktree file.

    Before 2026-04-17 the session file lived at PROJECT_ROOT/.watcher_session.
    Every worktree had its own copy, so each new worktree's first edit minted
    a fresh UUID — one Watcher per worktree instead of one per host. The
    anchor at ~/.unitares/anchors/watcher.json fixes that by being shared
    across all worktrees.
    """

    def test_default_session_file_is_home_anchor(self, watcher_module):
        """Default SESSION_FILE is ~/.unitares/anchors/watcher.json, not PROJECT_ROOT-scoped."""
        expected = Path.home() / ".unitares" / "anchors" / "watcher.json"
        assert watcher_module.SESSION_FILE == expected

    def test_legacy_session_file_is_project_root(self, watcher_module):
        """LEGACY_SESSION_FILE still resolves to the old per-worktree path for migration."""
        assert watcher_module.LEGACY_SESSION_FILE.name == ".watcher_session"

    def test_save_session_creates_anchor_parent_dir(self, watcher_module, tmp_path, monkeypatch):
        """_save_session mkdirs the anchor parent if missing."""
        anchor = tmp_path / "nonexistent" / "anchors" / "watcher.json"
        monkeypatch.setattr(watcher_module, "SESSION_FILE", anchor)

        watcher_module._save_session("sess", "tok", "uuid-x")
        assert anchor.exists()
        data = json.loads(anchor.read_text())
        assert data["agent_uuid"] == "uuid-x"

    def test_save_session_writes_anchor_mode_0600(self, watcher_module, tmp_path, monkeypatch):
        """Anchor carries continuity_token — must not be world-readable to
        same-UID siblings. Regression guard against reintroducing
        Path.write_text (which inherits umask 022 → 0644)."""
        import os
        import stat as _stat

        anchor = tmp_path / "anchors" / "watcher.json"
        monkeypatch.setattr(watcher_module, "SESSION_FILE", anchor)

        watcher_module._save_session("sess", "tok", "uuid-perm")
        assert _stat.S_IMODE(os.stat(anchor).st_mode) == 0o600

    def test_load_session_migrates_from_legacy_when_anchor_missing(
        self, watcher_module, tmp_path, monkeypatch
    ):
        """If anchor is missing but legacy file exists, migrate it and return its contents."""
        anchor = tmp_path / "anchor.json"
        legacy = tmp_path / "legacy" / ".watcher_session"
        legacy.parent.mkdir()
        legacy.write_text(json.dumps({"agent_uuid": "uuid-legacy", "continuity_token": "t-legacy"}))

        monkeypatch.setattr(watcher_module, "SESSION_FILE", anchor)
        monkeypatch.setattr(watcher_module, "LEGACY_SESSION_FILE", legacy)

        data = watcher_module._load_session()
        assert data["agent_uuid"] == "uuid-legacy"
        assert anchor.exists(), "migration should have written the anchor"
        assert json.loads(anchor.read_text())["agent_uuid"] == "uuid-legacy"

    def test_load_session_prefers_anchor_over_legacy(
        self, watcher_module, tmp_path, monkeypatch
    ):
        """When both exist, the anchor wins — legacy is ignored."""
        anchor = tmp_path / "anchor.json"
        anchor.write_text(json.dumps({"agent_uuid": "uuid-anchor"}))
        legacy = tmp_path / "legacy.watcher_session"
        legacy.write_text(json.dumps({"agent_uuid": "uuid-legacy"}))

        monkeypatch.setattr(watcher_module, "SESSION_FILE", anchor)
        monkeypatch.setattr(watcher_module, "LEGACY_SESSION_FILE", legacy)

        data = watcher_module._load_session()
        assert data["agent_uuid"] == "uuid-anchor"

    def test_load_session_returns_empty_when_neither_exists(
        self, watcher_module, tmp_path, monkeypatch
    ):
        """No anchor, no legacy → empty dict (caller will Fresh Onboard)."""
        monkeypatch.setattr(watcher_module, "SESSION_FILE", tmp_path / "missing.json")
        monkeypatch.setattr(watcher_module, "LEGACY_SESSION_FILE", tmp_path / "also-missing")

        assert watcher_module._load_session() == {}


class TestMainIdentityWiring:
    """main() calls resolve_identity before dispatching subcommands."""

    def test_main_resolves_identity_before_scan(self, watcher_module, tmp_path, monkeypatch):
        """--file path triggers identity resolution before scanning."""
        resolved = {"called": False}

        def mock_resolve(client):
            resolved["called"] = True

        monkeypatch.setattr(watcher_module, "resolve_identity", mock_resolve)
        # Make scan_file a no-op so we don't need a real file
        monkeypatch.setattr(watcher_module, "scan_file", lambda *a, **kw: [])
        # Prevent SyncGovernanceClient from connecting
        monkeypatch.setattr(
            watcher_module, "_make_identity_client",
            lambda: type("C", (), {
                "onboard": lambda *a, **kw: None,
                "identity": lambda **kw: None,
                "client_session_id": None,
                "continuity_token": None,
                "agent_uuid": None,
            })(),
        )

        monkeypatch.setattr("sys.argv", ["watcher", "--file", "/dev/null"])
        watcher_module.main()
        assert resolved["called"]

    def test_main_proceeds_when_governance_down(self, watcher_module, tmp_path, monkeypatch):
        """Governance failure during identity doesn't prevent scan."""
        scan_called = {"called": False}

        def mock_scan(*a, **kw):
            scan_called["called"] = True
            return []

        monkeypatch.setattr(watcher_module, "scan_file", mock_scan)
        monkeypatch.setattr(
            watcher_module, "_make_identity_client",
            lambda: (_ for _ in ()).throw(ConnectionError("down")),
        )

        monkeypatch.setattr("sys.argv", ["watcher", "--file", "/dev/null"])
        watcher_module.main()
        assert scan_called["called"]


class TestMainAllMode:
    """--all runs pattern scan AND reasoning review in one process.

    Background: the PostToolUse hook calls agent.py once per edit. Spinning
    up twice (once for scan, once for review) doubles process startup and
    Ollama warmup cost. A single --all entrypoint runs both sequentially in
    one process and is what the hook should invoke.
    """

    def _stub_identity(self, watcher_module, monkeypatch):
        monkeypatch.setattr(watcher_module, "resolve_identity", lambda _c: None)
        monkeypatch.setattr(
            watcher_module, "_make_identity_client",
            lambda: type("C", (), {
                "onboard": lambda *a, **kw: None,
                "identity": lambda **kw: None,
                "client_session_id": None,
                "continuity_token": None,
                "agent_uuid": None,
            })(),
        )

    def test_all_mode_invokes_both_scan_and_review(
        self, watcher_module, tmp_path, monkeypatch
    ):
        """--all --file X must call scan_file AND review_file — missing
        either means the hook is silently dropping a detection path."""
        self._stub_identity(watcher_module, monkeypatch)

        calls: list[str] = []
        monkeypatch.setattr(
            watcher_module, "scan_file",
            lambda *a, **kw: calls.append("scan") or [],
        )
        monkeypatch.setattr(
            watcher_module, "review_file",
            lambda *a, **kw: calls.append("review") or [],
        )

        monkeypatch.setattr("sys.argv", ["watcher", "--all", "--file", "/dev/null"])
        watcher_module.main()

        assert calls == ["scan", "review"], (
            f"--all must invoke scan then review, got: {calls}"
        )


class TestWatcherCheckin:
    """Check-in appended to surface_pending()."""

    def _write_findings(self, watcher_module, findings: list[dict]):
        """Helper: write findings to the isolated findings.jsonl.

        Rewrites the ``file`` field on each entry to point at this test
        file so the auto-sweep on ``surface_pending`` doesn't drop them.
        Tests that exercise the sweep itself use ``_seed_findings`` +
        ``_make_raw_entry(file=...)`` instead.
        """
        real_path = str(Path(__file__).resolve())
        watcher_module.FINDINGS_FILE.parent.mkdir(parents=True, exist_ok=True)
        with watcher_module.FINDINGS_FILE.open("w") as f:
            for finding in findings:
                finding = {**finding, "file": real_path}
                f.write(json.dumps(finding) + "\n")

    def test_checkin_posts_summary_after_surface(self, watcher_module, monkeypatch):
        """surface_pending() calls checkin with a summary of current findings."""
        self._write_findings(watcher_module, [
            {"fingerprint": "aaa1", "status": "open", "severity": "high",
             "pattern": "P001", "file": "/tmp/x.py", "line": 10, "hint": "bad",
             "timestamp": datetime.now(timezone.utc).isoformat()},
            {"fingerprint": "bbb2", "status": "confirmed", "severity": "medium",
             "pattern": "P002", "file": "/tmp/y.py", "line": 20, "hint": "ok",
             "timestamp": datetime.now(timezone.utc).isoformat()},
        ])

        # Set up identity so check-in proceeds
        _mock_watcher_identity(monkeypatch, watcher_module, {
            "agent_uuid": "uuid-w", "client_session_id": "s1", "continuity_token": "t1",
        })

        checkin_args = {}

        class FakeClient:
            client_session_id = "s1"
            continuity_token = "t1"
            agent_uuid = "uuid-w"

            def checkin(self, **kwargs):
                checkin_args.update(kwargs)
                return type("R", (), {"success": True, "verdict": "proceed",
                                      "guidance": None, "coherence": 0.5,
                                      "metrics": {}})()

        monkeypatch.setattr(watcher_module, "_make_identity_client", lambda: FakeClient())

        watcher_module.surface_pending()

        assert "response_text" in checkin_args
        assert "1 confirmed" in checkin_args["response_text"]
        assert checkin_args["complexity"] > 0  # has open findings
        assert checkin_args["response_mode"] == "compact"

    def test_checkin_skipped_when_no_identity(self, watcher_module, monkeypatch):
        """No identity → surface works, check-in silently skipped."""
        _mock_watcher_identity(monkeypatch, watcher_module, None)

        self._write_findings(watcher_module, [
            {"fingerprint": "ccc3", "status": "open", "severity": "low",
             "pattern": "P003", "file": "/tmp/z.py", "line": 5, "hint": "meh",
             "timestamp": datetime.now(timezone.utc).isoformat()},
        ])

        # surface_pending should complete without error
        result = watcher_module.surface_pending()
        assert result == 0

    def test_checkin_idle_heartbeat(self, watcher_module, monkeypatch):
        """No active findings → idle heartbeat with low complexity."""
        _mock_watcher_identity(monkeypatch, watcher_module, {
            "agent_uuid": "uuid-w", "client_session_id": "s1", "continuity_token": "t1",
        })

        checkin_args = {}

        class FakeClient:
            client_session_id = "s1"
            continuity_token = "t1"
            agent_uuid = "uuid-w"

            def checkin(self, **kwargs):
                checkin_args.update(kwargs)
                return type("R", (), {"success": True, "verdict": "proceed",
                                      "guidance": None, "coherence": 0.5,
                                      "metrics": {}})()

        monkeypatch.setattr(watcher_module, "_make_identity_client", lambda: FakeClient())

        watcher_module._do_checkin()

        assert "idle" in checkin_args["response_text"].lower()
        assert checkin_args["complexity"] <= 0.1

    def test_complexity_scales_with_open_findings(self, watcher_module):
        """complexity = 0.1 at 0 findings, 0.6 at 10+, linear between."""
        assert watcher_module.compute_checkin_complexity(0) == pytest.approx(0.1)
        assert watcher_module.compute_checkin_complexity(5) == pytest.approx(0.35)
        assert watcher_module.compute_checkin_complexity(10) == pytest.approx(0.6)
        assert watcher_module.compute_checkin_complexity(20) == pytest.approx(0.6)  # capped

    def test_confidence_from_resolution_ratio(self, watcher_module):
        """confidence = posterior mean of Beta(0.5+confirmed, 0.5+dismissed).

        Replaces the previous hardcoded 0.7 warmup, which was overconfidence
        shipped to governance. Beta(0.5, 0.5) at N=0 has mean 0.5 (true
        neutrality), and the value tracks the data smoothly thereafter.
        """
        # No data → exactly 0.5 (Beta(0.5, 0.5) mean)
        assert watcher_module.compute_checkin_confidence(0, 0) == pytest.approx(0.5)
        # 3 confirmed, 1 dismissed → Beta(3.5, 1.5) mean = 3.5/5.0 = 0.7
        assert watcher_module.compute_checkin_confidence(3, 1) == pytest.approx(0.7)
        # 4 confirmed, 1 dismissed → Beta(4.5, 1.5) mean = 4.5/6.0 = 0.75
        assert watcher_module.compute_checkin_confidence(4, 1) == pytest.approx(0.75)
        # 0 confirmed, 5 dismissed → Beta(0.5, 5.5) mean = 0.5/6.0 ≈ 0.083
        assert watcher_module.compute_checkin_confidence(0, 5) == pytest.approx(0.5 / 6.0)
        # 5 confirmed, 0 dismissed → Beta(5.5, 0.5) mean = 5.5/6.0 ≈ 0.917
        assert watcher_module.compute_checkin_confidence(5, 0) == pytest.approx(5.5 / 6.0)
        # Negative input clamps to neutral
        assert watcher_module.compute_checkin_confidence(-1, 0) == pytest.approx(0.5)

    def test_surface_pending_checks_in_even_with_no_open_findings(
        self, watcher_module, monkeypatch
    ):
        """surface_pending() must call _do_checkin even when there are no open
        findings to surface. Previously the early return skipped the check-in,
        causing Watcher to go silent between finding bursts."""
        _mock_watcher_identity(monkeypatch, watcher_module, {
            "agent_uuid": "uuid-w", "client_session_id": "s1", "continuity_token": "t1",
        })

        checkin_called = []

        class FakeClient:
            client_session_id = "s1"
            continuity_token = "t1"
            agent_uuid = "uuid-w"

            def checkin(self, **kwargs):
                checkin_called.append(kwargs)
                return type("R", (), {"success": True, "verdict": "proceed",
                                      "guidance": None, "coherence": 0.5,
                                      "metrics": {}})()

        monkeypatch.setattr(watcher_module, "_make_identity_client", lambda: FakeClient())

        # No findings seeded → no open findings
        rc = watcher_module.surface_pending()
        assert rc == 0
        assert len(checkin_called) == 1


class TestResolutionAuditTrail:
    """--resolve/--dismiss posts watcher_resolution_finding governance events."""

    def _write_findings(self, watcher_module, findings: list[dict]):
        watcher_module.FINDINGS_FILE.parent.mkdir(parents=True, exist_ok=True)
        with watcher_module.FINDINGS_FILE.open("w") as f:
            for finding in findings:
                f.write(json.dumps(finding) + "\n")

    def test_resolve_posts_governance_event(self, watcher_module, monkeypatch):
        """--resolve posts a watcher_resolution_finding event with action=confirmed."""
        self._write_findings(watcher_module, [
            {"fingerprint": "ff27c1b200000000", "status": "open", "severity": "high",
             "pattern": "P004", "file": "/tmp/x.py", "line": 97,
             "hint": "asyncpg deadlock", "violation_class": "REC",
             "timestamp": datetime.now(timezone.utc).isoformat()},
        ])
        _mock_watcher_identity(monkeypatch, watcher_module, {
            "agent_uuid": "uuid-watcher",
            "client_session_id": "s1",
            "continuity_token": "t1",
        })

        posted = {}
        _mock_post_finding(monkeypatch, watcher_module, lambda **kw: posted.update(kw) or True)

        watcher_module.update_finding_status("ff27c1b2", "confirmed", resolver_agent_id="uuid-agent-X")

        assert posted["event_type"] == "watcher_resolution_finding"
        assert posted["extra"]["action"] == "confirmed"
        assert posted["extra"]["resolved_by"] == "uuid-agent-X"
        assert posted["extra"]["pattern"] == "P004"
        assert posted["agent_id"] == "uuid-watcher"

    def test_dismiss_posts_governance_event(self, watcher_module, monkeypatch):
        """--dismiss posts a watcher_resolution_finding event with action=dismissed."""
        self._write_findings(watcher_module, [
            {"fingerprint": "8266dfb800000000", "status": "surfaced", "severity": "high",
             "pattern": "P004", "file": "/tmp/y.py", "line": 114,
             "hint": "asyncpg deadlock", "violation_class": "REC",
             "timestamp": datetime.now(timezone.utc).isoformat()},
        ])
        _mock_watcher_identity(monkeypatch, watcher_module, {
            "agent_uuid": "uuid-watcher",
            "client_session_id": "s1",
            "continuity_token": "t1",
        })

        posted = {}
        _mock_post_finding(monkeypatch, watcher_module, lambda **kw: posted.update(kw) or True)

        watcher_module.update_finding_status("8266dfb8", "dismissed", resolver_agent_id="uuid-agent-Y")

        assert posted["event_type"] == "watcher_resolution_finding"
        assert posted["extra"]["action"] == "dismissed"
        assert posted["extra"]["resolved_by"] == "uuid-agent-Y"

    def test_resolve_without_agent_id(self, watcher_module, monkeypatch):
        """--resolve without --agent-id sets resolved_by to None."""
        self._write_findings(watcher_module, [
            {"fingerprint": "abcd123400000000", "status": "open", "severity": "medium",
             "pattern": "P002", "file": "/tmp/z.py", "line": 50,
             "hint": "unbounded growth", "violation_class": "ENT",
             "timestamp": datetime.now(timezone.utc).isoformat()},
        ])
        _mock_watcher_identity(monkeypatch, watcher_module, {
            "agent_uuid": "uuid-watcher",
            "client_session_id": "s1",
            "continuity_token": "t1",
        })

        posted = {}
        _mock_post_finding(monkeypatch, watcher_module, lambda **kw: posted.update(kw) or True)

        watcher_module.update_finding_status("abcd1234", "confirmed")

        assert posted["extra"]["resolved_by"] is None

    def test_resolve_skips_event_when_no_identity(self, watcher_module, monkeypatch):
        """No identity → local status update works, governance event skipped."""
        self._write_findings(watcher_module, [
            {"fingerprint": "dead000000000000", "status": "open", "severity": "low",
             "pattern": "P001", "file": "/tmp/a.py", "line": 1,
             "hint": "test", "violation_class": "CON",
             "timestamp": datetime.now(timezone.utc).isoformat()},
        ])
        _mock_watcher_identity(monkeypatch, watcher_module, None)

        posted = {}
        _mock_post_finding(monkeypatch, watcher_module, lambda **kw: posted.update(kw) or True)

        result = watcher_module.update_finding_status("dead0000", "confirmed")

        assert result == 0  # local update succeeded
        assert not posted  # no governance event

    def test_governance_event_failure_doesnt_break_local_update(self, watcher_module, monkeypatch):
        """post_finding failure doesn't prevent the local status update."""
        self._write_findings(watcher_module, [
            {"fingerprint": "beef000000000000", "status": "open", "severity": "high",
             "pattern": "P004", "file": "/tmp/b.py", "line": 10,
             "hint": "test", "violation_class": "REC",
             "timestamp": datetime.now(timezone.utc).isoformat()},
        ])
        _mock_watcher_identity(monkeypatch, watcher_module, {
            "agent_uuid": "uuid-watcher",
            "client_session_id": "s1",
            "continuity_token": "t1",
        })

        def exploding_post(**kw):
            raise RuntimeError("governance exploded")

        _mock_post_finding(monkeypatch, watcher_module, exploding_post)

        result = watcher_module.update_finding_status("beef0000", "confirmed")
        assert result == 0  # local update still worked

        # Verify the local status was actually updated
        findings = watcher_module._iter_findings_raw()
        assert findings[0]["status"] == "confirmed"


# ---------------------------------------------------------------------------
# Task 6: Full lifecycle integration test
# ---------------------------------------------------------------------------


class TestWatcherLifecycleIntegration:
    """End-to-end: identity → scan → surface + check-in → resolve with audit."""

    def test_full_lifecycle(self, watcher_module, tmp_path, monkeypatch):
        """Identity → persist finding → surface (triggers check-in) → resolve (posts event)."""
        session_file = tmp_path / ".watcher_session"
        monkeypatch.setattr(watcher_module, "SESSION_FILE", session_file)
        # Phase 3 silent-fork guard requires explicit bootstrap env var.
        monkeypatch.setenv("UNITARES_FIRST_RUN", "1")

        # Track all governance interactions
        gov_calls = []

        class FakeClient:
            client_session_id = "sess-int"
            continuity_token = "tok-int"
            agent_uuid = "uuid-watcher-int"

            def onboard(self, name, **kwargs):
                gov_calls.append(("onboard", name))
                return type("R", (), {"success": True})()

            def identity(self, **kwargs):
                raise RuntimeError("no prior session")

            def checkin(self, **kwargs):
                gov_calls.append(("checkin", kwargs.get("response_text", "")))
                return type("R", (), {
                    "success": True, "verdict": "proceed",
                    "guidance": None, "coherence": 0.5, "metrics": {},
                })()

        monkeypatch.setattr(watcher_module, "_make_identity_client", lambda: FakeClient())

        # 1. Resolve identity (fresh onboard)
        watcher_module.resolve_identity(FakeClient())
        assert watcher_module.get_watcher_identity()["agent_uuid"] == "uuid-watcher-int"
        assert ("onboard", "Watcher") in gov_calls

        # resolve_identity only sets _watcher_identity on the importlib-loaded
        # watcher_module copy. findings.update_finding_status does a lazy
        # import from the real agents.watcher.agent module, so mirror the
        # identity there for _post_resolution_event to see it.
        from agents.watcher import agent as real_watcher
        monkeypatch.setattr(real_watcher, "_watcher_identity", watcher_module.get_watcher_identity())

        # 2. Simulate a finding being persisted. File must actually exist
        # on disk so the auto-sweep on surface_pending doesn't drop it.
        test_code_path = tmp_path / "test_code.py"
        test_code_path.write_text("# placeholder\n")
        watcher_module.FINDINGS_FILE.parent.mkdir(parents=True, exist_ok=True)
        finding = {
            "fingerprint": "integ00000000000",
            "status": "open",
            "severity": "high",
            "pattern": "P004",
            "file": str(test_code_path),
            "line": 42,
            "hint": "asyncpg in handler",
            "violation_class": "REC",
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }
        with watcher_module.FINDINGS_FILE.open("w") as f:
            f.write(json.dumps(finding) + "\n")

        # 3. Surface pending (triggers check-in)
        watcher_module.surface_pending()
        checkin_calls = [c for c in gov_calls if c[0] == "checkin"]
        assert len(checkin_calls) == 1
        assert "1 unresolved" in checkin_calls[0][1]

        # 4. Resolve finding (posts audit event)
        posted_events = []
        _mock_post_finding(monkeypatch, watcher_module, lambda **kw: posted_events.append(kw) or True)

        result = watcher_module.update_finding_status("integ000", "confirmed", resolver_agent_id="uuid-agent-resolver")
        assert result == 0
        assert len(posted_events) == 1
        assert posted_events[0]["event_type"] == "watcher_resolution_finding"
        assert posted_events[0]["extra"]["resolved_by"] == "uuid-agent-resolver"

        # Verify local status also updated
        findings = watcher_module._iter_findings_raw()
        assert findings[0]["status"] == "confirmed"
