"""Finding model, persistence, dedup, lifecycle, surfacing, compaction,
escalation.

Split out of agent.py so the file stayed navigable. Identity, scanning, and
CLI orchestration remain in agent.py. ``surface_pending`` also stays there
because it calls ``_do_checkin`` from the identity block; everything else
that touches findings.jsonl / dedup.json lives here.
"""

from __future__ import annotations

import hashlib
import json
import subprocess
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from agents.common.findings import post_finding
from agents.watcher._util import (
    PROJECT_ROOT,
    hash_line_content,
    log,
    repo_relative_path,
)
from agents.watcher.calibration import (
    classify_file,
    probe_rate_for_n,
    should_probe,
)
from agents.watcher.floor_state import FloorState, load_floor

# ---------------------------------------------------------------------------
# Paths & Config
# ---------------------------------------------------------------------------

STATE_DIR = PROJECT_ROOT / "data" / "watcher"
FINDINGS_FILE = STATE_DIR / "findings.jsonl"
DEDUP_FILE = STATE_DIR / "dedup.json"

GOV_REST_URL = "http://localhost:8767/v1/tools/call"

# Age findings out after this many days
FINDINGS_TTL_DAYS = 14

VALID_FINDING_STATUSES = ("open", "surfaced", "confirmed", "dismissed", "aged_out")
MIN_FINGERPRINT_PREFIX = 4  # users can type the first N chars instead of all 16

# Allowed --reason values for --dismiss. Only 'fp' counts as a true
# negative in precision math (see PRECISION_REASONS_TRUE_NEGATIVE in
# calibration.py). The others document operator intent without claiming
# the finding was wrong.
DISMISSAL_REASONS = frozenset({"fp", "wont_fix", "out_of_scope", "dup", "unclear", "stale"})


# ---------------------------------------------------------------------------
# Data types
# ---------------------------------------------------------------------------


@dataclass
class Finding:
    pattern: str
    file: str
    line: int
    hint: str
    severity: str  # critical | high | medium | low
    detected_at: str
    model_used: str
    # Hash of the normalized source line at `line` at the time of detection.
    # Included in the fingerprint so the same pattern flagged at the same
    # line number but against DIFFERENT code (e.g. you fixed bug A at line 47
    # and a new bug B arrived at the same line) does not get silently
    # dedup'd as a rerun of the old finding.
    line_content_hash: str = ""
    fingerprint: str = ""
    status: str = "open"  # open | surfaced | confirmed | dismissed | aged_out
    violation_class: str = ""  # CON | INT | ENT | REC | BEH | VOI

    def __post_init__(self) -> None:
        if not self.fingerprint:
            self.fingerprint = self.compute_fingerprint()

    def compute_fingerprint(self) -> str:
        """Stable identifier combining pattern, file, line, and (optionally)
        a content hash. Callers that want content-aware dedup should set
        ``line_content_hash`` BEFORE invoking this and then assign the
        result back to ``fingerprint``.

        The file path is normalized to its repo-relative form (relative to
        the git worktree root containing it) so the same line in identical
        code checked out across multiple git worktrees produces ONE
        fingerprint, not N. The displayed ``file`` field is left absolute so
        the user can navigate to the right copy.
        """
        normalized_path = repo_relative_path(self.file)
        key = f"{self.pattern}|{normalized_path}|{self.line}|{self.line_content_hash}"
        return hashlib.sha256(key.encode()).hexdigest()[:16]


# ---------------------------------------------------------------------------
# Dedup (data/watcher/dedup.json)
# ---------------------------------------------------------------------------


def load_dedup() -> dict[str, str]:
    """Return mapping of fingerprint → detected_at timestamp."""
    if not DEDUP_FILE.exists():
        return {}
    try:
        return json.loads(DEDUP_FILE.read_text())
    except (json.JSONDecodeError, OSError):
        return {}


def save_dedup(dedup: dict[str, str]) -> None:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    DEDUP_FILE.write_text(json.dumps(dedup, indent=2))


def sweep_stale_dedup(
    dedup: dict[str, str],
    ttl_days: int = FINDINGS_TTL_DAYS,
    now: datetime | None = None,
) -> dict[str, str]:
    """Drop dedup entries older than ``ttl_days``.

    Prevents the dedup dict from growing unboundedly over months — a P002
    pattern match against the Watcher's own code that Ogler correctly
    flagged at :78 / :127 / :496 on 2026-04-10. ``FINDINGS_TTL_DAYS`` was
    defined but never enforced in the first cut of this module; this
    function is the enforcement point.

    Entries with an unparseable timestamp are kept (fail-open), so a
    corrupted dedup file never silently empties itself.
    """
    if not dedup:
        return dedup
    reference = now or datetime.now(timezone.utc)
    cutoff = reference - timedelta(days=ttl_days)
    pruned: dict[str, str] = {}
    dropped = 0
    for fingerprint, ts in dedup.items():
        try:
            detected = datetime.strptime(ts, "%Y-%m-%dT%H:%M:%SZ").replace(
                tzinfo=timezone.utc
            )
        except (TypeError, ValueError):
            # Unparseable timestamp — keep the entry rather than drop it
            # blindly. We'd rather leak a few entries than lose findings.
            pruned[fingerprint] = ts
            continue
        if detected >= cutoff:
            pruned[fingerprint] = ts
        else:
            dropped += 1
    if dropped:
        log(
            f"dedup sweep: dropped {dropped} stale entries older than {ttl_days}d "
            f"({len(pruned)} remain)"
        )
    return pruned


# ---------------------------------------------------------------------------
# Persistence (data/watcher/findings.jsonl)
# ---------------------------------------------------------------------------


def persist_findings(new_findings: list[Finding]) -> list[Finding]:
    """Append new (non-duplicate) findings to findings.jsonl. Return the ones
    that were actually new (dedup filter applied)."""
    dedup = load_dedup()
    dedup = sweep_stale_dedup(dedup)
    fresh: list[Finding] = []
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    for f in new_findings:
        if f.fingerprint in dedup:
            continue  # already flagged this one
        dedup[f.fingerprint] = now
        fresh.append(f)

    if fresh or dedup != load_dedup():
        # Persist even if `fresh` is empty, so the sweep's pruning actually
        # lands on disk. Otherwise stale entries would rematerialize on the
        # next scan.
        STATE_DIR.mkdir(parents=True, exist_ok=True)
        if fresh:
            for f in fresh:
                persist_finding(f)
        save_dedup(dedup)

    return fresh


def persist_finding(finding: Finding) -> None:
    """Append a new finding to findings.jsonl and, for high/critical severity,
    mirror it into the governance event stream so the Discord bridge surfaces it.

    Low/medium stays local — the SessionStart hook handles surfacing those
    to the in-editor Claude session.

    The caller is responsible for the dedup gate; this function does NOT
    check dedup itself.
    """
    FINDINGS_FILE.parent.mkdir(parents=True, exist_ok=True)
    with FINDINGS_FILE.open("a") as f:
        f.write(json.dumps(asdict(finding)) + "\n")

    if finding.severity in ("high", "critical"):
        post_finding(
            event_type="watcher_finding",
            severity=finding.severity,
            message=f"[{finding.pattern}] {finding.file}:{finding.line} — {finding.hint}",
            agent_id="watcher",
            agent_name="Watcher",
            fingerprint=finding.fingerprint,
            extra={
                "pattern": finding.pattern,
                "file": finding.file,
                "line": finding.line,
                "violation_class": finding.violation_class,
            },
        )


def _iter_findings_raw() -> list[dict[str, Any]]:
    """Load all findings from findings.jsonl as raw dicts. Silently skips
    malformed lines. Returns [] if the file doesn't exist."""
    if not FINDINGS_FILE.exists():
        return []
    out: list[dict[str, Any]] = []
    with FINDINGS_FILE.open() as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                out.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return out


def _write_findings_atomic(findings: list[dict[str, Any]]) -> None:
    """Atomically replace findings.jsonl with the given list. Writes to a
    sibling temp file and renames, so a crash mid-write cannot corrupt the
    findings feed."""
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    tmp = FINDINGS_FILE.with_suffix(FINDINGS_FILE.suffix + ".tmp")
    with tmp.open("w") as fh:
        for f in findings:
            fh.write(json.dumps(f) + "\n")
    tmp.replace(FINDINGS_FILE)


# ---------------------------------------------------------------------------
# Lifecycle commands
#
# Without these, findings.jsonl is append-only with no way to mark a finding
# as confirmed, dismissed, or stale. Governance has no calibration signal and
# the surface hook just accumulates noise. Ogler's critique of the rollup
# daemon was specifically "build the bottom before the top" — this is the
# bottom.
# ---------------------------------------------------------------------------


def match_fingerprint(prefix: str, findings: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], str | None]:
    """Return (matches, error) for a fingerprint prefix lookup.

    - Exact 16-char match returns at most one finding.
    - Shorter prefixes match all findings whose fingerprint starts with it.
    - Prefix shorter than ``MIN_FINGERPRINT_PREFIX`` is rejected to guard
      against accidental nukes from a 1-2 char typo.
    """
    if not prefix:
        return [], "empty fingerprint"
    if len(prefix) < MIN_FINGERPRINT_PREFIX:
        return [], f"fingerprint too short (min {MIN_FINGERPRINT_PREFIX} chars)"
    matches = [f for f in findings if f.get("fingerprint", "").startswith(prefix)]
    return matches, None


_STATUS_TIMESTAMP_FIELD = {
    "confirmed": "confirmed_at",
    "dismissed": "dismissed_at",
    "aged_out": "aged_out_at",
}


def update_finding_status(
    fingerprint_prefix: str,
    new_status: str,
    resolver_agent_id: str | None = None,
    reason: str | None = None,
) -> int:
    """Mark a finding as ``new_status`` by fingerprint prefix.

    Writes a status-transition timestamp (``confirmed_at``/``dismissed_at``/
    ``aged_out_at``) and, when supplied, ``resolved_by`` + ``resolution_reason``
    so the dashboard timeline and audit trail have the data they need — the
    prior implementation only mutated ``status`` and the timeline series were
    always zero as a result.

    Returns exit code:
      0 — updated exactly one finding
      1 — no match or ambiguous prefix
      2 — invalid status
    """
    if new_status not in VALID_FINDING_STATUSES:
        log(f"update_finding_status: invalid status {new_status!r}", "error")
        print(f"error: invalid status {new_status!r}; must be one of {VALID_FINDING_STATUSES}")
        return 2

    # Soft taxonomy: a non-enum reason is persisted (operators often pass
    # free-text rationale, and pre-2026-04-27 rows already do). Precision
    # math in calibration.py excludes non-enum reasons from the TN count
    # automatically, so the calibration loop is correct without rejecting
    # the operator's input here.
    if new_status == "dismissed" and reason is not None and reason not in DISMISSAL_REASONS:
        log(
            f"update_finding_status: non-enum reason {reason!r} accepted but "
            f"will be excluded from precision math (use one of "
            f"{sorted(DISMISSAL_REASONS)} for the bucket to count)",
            "warning",
        )

    findings = _iter_findings_raw()
    if not findings:
        print("error: findings.jsonl is empty or absent")
        return 1

    matches, err = match_fingerprint(fingerprint_prefix, findings)
    if err:
        print(f"error: {err}")
        return 1
    if not matches:
        print(f"error: no finding matches fingerprint prefix {fingerprint_prefix!r}")
        return 1
    if len(matches) > 1:
        print(f"error: fingerprint prefix {fingerprint_prefix!r} is ambiguous ({len(matches)} matches):")
        for m in matches:
            print(
                f"  {m.get('fingerprint','?')[:16]} {m.get('severity','?')} "
                f"{m.get('pattern','?')} {m.get('file','?')}:{m.get('line','?')}"
            )
        return 1

    target_fp = matches[0].get("fingerprint", "")
    now_iso = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    timestamp_field = _STATUS_TIMESTAMP_FIELD.get(new_status)

    updated: list[dict[str, Any]] = []
    for f in findings:
        if f.get("fingerprint") == target_fp:
            merged = {**f, "status": new_status}
            if timestamp_field:
                merged[timestamp_field] = now_iso
            if resolver_agent_id:
                merged["resolved_by"] = resolver_agent_id
            if reason:
                merged["resolution_reason"] = reason
            f = merged
        updated.append(f)
    _write_findings_atomic(updated)
    log(f"update_finding_status: {target_fp[:8]} → {new_status}")
    print(
        f"ok: {target_fp[:16]} → {new_status} "
        f"({matches[0].get('pattern','?')} at {matches[0].get('file','?')}:{matches[0].get('line','?')})"
    )

    # --- Post resolution event to governance ---
    if new_status in ("confirmed", "dismissed"):
        # Lazy import: _post_resolution_event needs get_watcher_identity
        # from agent.py's identity block. Top-level import would be circular.
        from agents.watcher.agent import _post_resolution_event
        _post_resolution_event(matches[0], new_status, resolver_agent_id, reason=reason)

    return 0


def _sweep_stale_quiet() -> int:
    """Drop findings whose target file no longer exists. Quiet variant.

    Returns the count dropped. Logs to the watcher log when non-zero but
    never prints — safe to call from chime/SessionStart paths where
    stdout becomes part of the agent context. Called by ``surface_pending``
    (chime) so the chime never shows findings against deleted paths
    without the operator having to remember to run --sweep-stale.

    Same logic as ``sweep_stale_findings``, factored so the CLI variant
    can stay print-y while the auto-call-site stays silent.
    """
    findings = _iter_findings_raw()
    if not findings:
        return 0

    kept: list[dict[str, Any]] = []
    dropped = 0
    for f in findings:
        path = f.get("file", "")
        if path and Path(path).exists():
            kept.append(f)
        else:
            dropped += 1

    if dropped == 0:
        return 0

    _write_findings_atomic(kept)
    log(f"sweep_stale_findings (auto): dropped {dropped} findings for missing files")
    return dropped


def sweep_stale_findings() -> int:
    """Drop findings whose target file no longer exists on disk.

    CLI variant: prints a human-readable summary. The chime/auto-call
    path uses ``_sweep_stale_quiet`` instead — same drop logic, no stdout.

    This is the "the file got deleted or renamed" cleanup — we don't want
    the surface hook to keep nagging you about a file that isn't there
    anymore. Open/surfaced findings get aged_out via this path too because
    there's no code to evaluate.
    """
    findings = _iter_findings_raw()
    if not findings:
        print("(no findings to sweep)")
        return 0

    total = len(findings)
    dropped = _sweep_stale_quiet()
    if dropped == 0:
        print(f"(nothing to sweep: {total} findings, all target files present)")
        return 0
    print(f"ok: dropped {dropped} finding(s) with missing target files, kept {total - dropped}")
    return 0


# ---------------------------------------------------------------------------
# Surfacing — how findings reach the main Claude session
#
# Two hooks call the functions below:
#
#   SessionStart → --print-unresolved (read-only, shows open+surfaced so the
#     new session sees the full backlog — if it only showed open, findings
#     already "surfaced" in a previous session would silently disappear from
#     context)
#
#   UserPromptSubmit → --surface-pending (chime mode, in agent.py because
#     it also triggers a governance check-in; it reuses _format_findings_block
#     and _write_findings_atomic from here)
#
# Both print a <unitares-watcher-findings> block that the Claude Code hook
# system injects as additionalContext. The formatter is shared between the
# two commands so the block shape stays consistent no matter which hook
# emitted it.
# ---------------------------------------------------------------------------


_SEVERITY_DEMOTION_LADDER = {
    "critical": "high",
    "high": "medium",
    "medium": "low",
    "low": "low",
}

# In-memory de-dup for the 'calibration: demoted' log line. Without this,
# the surface hook (UserPromptSubmit, fires on every prompt) would emit one
# line per demoted finding per render — a stable demoted pattern with 8
# findings produces 8 lines per prompt forever.
#
# Keyed on fingerprint only; the day component is enforced structurally by
# ``_DEMOTION_LOG_SEEN_DAY`` — a different ``today`` resets the set. This
# keeps the in-memory state bounded by O(N_fingerprints_today), which is
# the operator's working-set size, not by O(N_fingerprints × N_days_alive)
# which would be unbounded over a long-running process. Watcher itself
# flagged the prior unbounded version as P002 (#925bfbe9).
#
# Tests reset via ``.clear()`` and may set ``_DEMOTION_LOG_SEEN_DAY`` to
# pin a specific day.
_DEMOTION_LOG_SEEN: set[str] = set()
_DEMOTION_LOG_SEEN_DAY: str | None = None


def _demotion_log_should_emit(fingerprint: str, today: str) -> bool:
    """Return True if this (fingerprint, today) pair has not been logged
    yet. Caller is responsible for calling exactly once per render so the
    side effect (set add + day reset) only happens when emission proceeds.
    """
    global _DEMOTION_LOG_SEEN_DAY
    if _DEMOTION_LOG_SEEN_DAY != today:
        _DEMOTION_LOG_SEEN.clear()
        _DEMOTION_LOG_SEEN_DAY = today
    if fingerprint in _DEMOTION_LOG_SEEN:
        return False
    _DEMOTION_LOG_SEEN.add(fingerprint)
    return True


def _apply_floor_to_finding(
    finding: dict[str, Any],
    *,
    floor: FloorState,
    today: str,
) -> dict[str, Any]:
    """Return a copy of ``finding`` with severity demoted if the
    pattern's calibration floor has fallen below 0.3 and the finding
    isn't selected for an ε-greedy exploration probe.

    Adds two diagnostic fields when a decision fires:
      ``calibration_demoted_from`` — original severity (set on demote)
      ``calibration_probe`` — True (set when bucket below floor but probe carved out)

    These exist for downstream audit / future dashboard panels; they
    aren't rendered into the user-visible findings block.
    """
    pattern = finding.get("pattern", "")
    file_path = finding.get("file", "")
    severity = finding.get("severity", "low")

    file_class = classify_file(file_path)
    bucket = floor.get(pattern, file_class)
    if bucket is None or bucket.ci_lower is None or bucket.ci_lower >= 0.3:
        return finding

    # Probe seed is the calibration unit — (pattern, file_class) — NOT the
    # fingerprint. A bucket is the thing being calibrated; probes should
    # apply at that granularity so the operator sees coherent batches
    # ("today the P1/app bucket is on probe duty") rather than a
    # stochastic mix of demoted-and-not within a single render.
    # Council Q3 (dialectic).
    rate = probe_rate_for_n(bucket.weighted_n)
    probe_seed = f"{pattern}|{file_class}"
    if should_probe(probe_seed, date_iso=today, probe_rate=rate):
        out = dict(finding)
        out["calibration_probe"] = True
        return out

    new_severity = _SEVERITY_DEMOTION_LADDER.get(severity, severity)
    if new_severity == severity:
        return finding
    out = dict(finding)
    out["severity"] = new_severity
    out["calibration_demoted_from"] = severity
    return out


def _format_findings_block(
    findings: list[dict[str, Any]],
    *,
    header: str,
    out_of_scope_groups: dict[str, int] | None = None,
) -> tuple[str | None, list[dict[str, Any]]]:
    """Render the <unitares-watcher-findings> block.

    Returns ``(block, shown)`` where:
      - ``block`` is the formatted string to print, or None if nothing
        should be surfaced (empty list / all-low-severity / no out-of-scope).
      - ``shown`` is the ordered list of findings that actually made it
        into the displayed block. Callers use this to decide which
        findings to transition to ``surfaced`` status — we only want to
        mark findings the user actually saw, never the ones dropped by
        the display cap.

    The (block, shown) tuple shape replaces an earlier bug where
    surface_pending marked ALL open findings as surfaced regardless of
    whether the display cap had hidden them. Medium-severity findings
    behind a wall of criticals would transition silently and then get
    dedup'd on re-detection — effectively a silent drop of real signal.
    Ogler caught it on 2026-04-11.

    Severity rules for the displayed subset:
      - critical/high: always shown
      - medium: shown only if there's room under the 10-item display cap
        reserved for critical+high (keeps session context from drowning in
        medium-severity noise while still surfacing some)
      - low: never shown (file-only signal)

    ``out_of_scope_groups`` is an optional ``{worktree_label: count}`` map
    of findings the caller is *not* surfacing in the body (typically:
    findings whose file lives in a different worktree than the current
    session). Their aggregate count is rendered as a single footer line
    so the agent knows the backlog exists without drowning the chime
    block in findings it cannot act on from this workspace.
    """
    if not findings and not out_of_scope_groups:
        return None, []

    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    floor = load_floor()
    findings = [
        _apply_floor_to_finding(f, floor=floor, today=today)
        for f in findings
    ]
    for f in findings:
        if "calibration_demoted_from" not in f:
            continue
        if not _demotion_log_should_emit(f.get("fingerprint", ""), today):
            continue  # already logged today; skip the spam
        log(
            f"calibration: demoted {f.get('pattern','?')} on "
            f"{f.get('file','?')} from {f['calibration_demoted_from']} "
            f"to {f.get('severity','?')} (ci_lower below floor)",
            "info",
        )

    severity_order = {"critical": 0, "high": 1, "medium": 2, "low": 3}
    findings = sorted(
        findings,
        key=lambda f: (
            severity_order.get(f.get("severity", "low"), 9),
            f.get("detected_at", ""),
        ),
    )

    critical_high = [f for f in findings if f.get("severity") in ("critical", "high")]
    medium = [f for f in findings if f.get("severity") == "medium"]
    shown = critical_high[:]
    if len(shown) < 10:
        shown += medium[: 10 - len(shown)]

    out_of_scope_total = (
        sum(out_of_scope_groups.values()) if out_of_scope_groups else 0
    )

    # Nothing to render: no shown findings AND no out-of-scope summary.
    # An empty in-scope set is OK if there are still other-worktree
    # findings worth flagging — agent should know the backlog exists.
    if not shown and out_of_scope_total == 0:
        return None, []

    lines: list[str] = []
    lines.append("<unitares-watcher-findings>")
    lines.append(header)
    lines.append("")
    for f in shown:
        sev = str(f.get("severity", "?")).upper()
        pat = f.get("pattern", "?")
        vcls = f.get("violation_class", "")
        file = f.get("file", "?")
        line_no = f.get("line", "?")
        hint = f.get("hint", "")
        fp = str(f.get("fingerprint", ""))[:8]
        status = f.get("status", "open")
        marker = "" if status == "open" else f" ({status})"
        cls_tag = f"[{vcls}] " if vcls else ""
        lines.append(f"  [{sev}] {cls_tag}{pat} {file}:{line_no} — {hint}  (#{fp}){marker}")
    lines.append("")
    lines.append(f"Total unresolved: {len(findings)} (showing {len(shown)})")
    if out_of_scope_total:
        # Render groups in deterministic order (sorted by label) so the
        # footer stays stable across runs — easier to spot a real change
        # than chasing dict-iteration ordering churn.
        groups_str = ", ".join(
            f"{label}={count}"
            for label, count in sorted((out_of_scope_groups or {}).items())
        )
        lines.append(
            f"Plus {out_of_scope_total} finding(s) in other worktrees ({groups_str}); "
            "list with: python3 agents/watcher/agent.py --list-findings --only-open"
        )
    lines.append(
        "Resolve: python3 agents/watcher/agent.py --resolve <fingerprint> --agent-id <your-uuid>"
    )
    lines.append(
        "Dismiss: python3 agents/watcher/agent.py --dismiss <fingerprint> --agent-id <your-uuid>"
    )
    lines.append("</unitares-watcher-findings>")
    return "\n".join(lines), shown


def _resolve_session_scope_root(cwd: Path | None = None) -> Path | None:
    """Return the path that anchors the current session's scope — typically
    the git toplevel of the cwd. None if cwd isn't inside a git worktree;
    callers fall back to "no scoping" (surface everything).

    Bounded to a 2s subprocess timeout because session-start latency is
    user-visible. A slow git call must not hold up the chime block.
    """
    base = cwd or Path.cwd()
    try:
        result = subprocess.run(
            ["git", "-C", str(base), "rev-parse", "--show-toplevel"],
            capture_output=True,
            text=True,
            timeout=2.0,
            check=False,
        )
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
        return None
    if result.returncode != 0:
        return None
    out = result.stdout.strip()
    if not out:
        return None
    try:
        return Path(out).resolve()
    except OSError:
        return None


def _partition_findings_by_scope(
    findings: list[dict[str, Any]],
    scope_root: Path | None,
) -> tuple[list[dict[str, Any]], dict[str, int]]:
    """Split findings into ``(in_scope, out_of_scope_groups)``.

    A finding is in-scope when its ``file`` lives under ``scope_root``.
    Out-of-scope findings are aggregated by their nearest ``.worktrees``
    sibling label so the footer can summarize *where* the backlog is
    without listing every path.

    If ``scope_root`` is None, all findings are treated as in-scope —
    matches the legacy "surface everything" behavior so callers without
    a worktree (CI, ad-hoc CLI) keep the existing experience.
    """
    if scope_root is None:
        return list(findings), {}

    in_scope: list[dict[str, Any]] = []
    out_groups: dict[str, int] = {}
    scope_str = str(scope_root)
    for f in findings:
        file_path = f.get("file") or ""
        if file_path and (file_path == scope_str or file_path.startswith(scope_str + "/")):
            in_scope.append(f)
            continue
        out_groups[_label_for_other_worktree(file_path)] = (
            out_groups.get(_label_for_other_worktree(file_path), 0) + 1
        )
    return in_scope, out_groups


def _label_for_other_worktree(file_path: str) -> str:
    """Heuristic short label for an out-of-scope finding's worktree.

    Uses the segment after ``.worktrees/`` when present (which is how
    superpowers/git-worktree wires the layout in this repo); otherwise
    falls back to the parent directory name. Goal is just to give the
    operator a stable, recognizable handle in the footer — exactness is
    nice-to-have, not load-bearing.
    """
    if not file_path:
        return "(unknown)"
    parts = Path(file_path).parts
    if ".worktrees" in parts:
        idx = parts.index(".worktrees")
        if idx + 1 < len(parts):
            return parts[idx + 1]
    # Fall back to the deepest dir name above the file
    parent = Path(file_path).parent
    return parent.name or "(root)"


def print_unresolved(scope_root: Path | None = None) -> int:
    """Print the unresolved-findings block (open + surfaced) without mutating
    state. Called by the SessionStart hook — it's read-only so session starts
    never accidentally reshape the findings state.

    ``scope_root`` is the worktree root used to filter findings to the
    current session. When ``None`` (the production default), it's
    auto-discovered from cwd via ``git rev-parse --show-toplevel``. Tests
    pass an explicit value. When discovery fails, scoping is disabled
    and the legacy behavior (surface everything) is preserved.
    """
    if scope_root is None:
        scope_root = _resolve_session_scope_root()

    findings = [
        f
        for f in _iter_findings_raw()
        if f.get("status", "open") in ("open", "surfaced")
    ]
    in_scope, out_groups = _partition_findings_by_scope(findings, scope_root)

    block, _shown = _format_findings_block(
        in_scope,
        header=(
            "The UNITARES Watcher agent flagged the following unresolved code\n"
            "patterns in recently edited files. Watcher has a track record — these\n"
            "are not noise. Investigate or explicitly --dismiss them."
        ),
        out_of_scope_groups=out_groups or None,
    )
    if block is None:
        return 0
    print(block)
    return 0


def compact_findings(max_age_days: int = 7, now: datetime | None = None) -> int:
    """Rewrite findings.jsonl dropping confirmed/dismissed/aged_out entries
    older than ``max_age_days``.

    Active findings (``open`` / ``surfaced``) are always kept regardless of
    age — they still need your attention. Only already-resolved entries get
    compacted away. This is the fix for Ogler's P002-round-two: the findings
    file itself was growing unboundedly even after the dedup dict got its
    TTL sweep.
    """
    findings = _iter_findings_raw()
    if not findings:
        print("(no findings to compact)")
        return 0

    reference = now or datetime.now(timezone.utc)
    cutoff = reference - timedelta(days=max_age_days)
    resolved_states = {"confirmed", "dismissed", "aged_out"}

    kept: list[dict[str, Any]] = []
    dropped = 0
    for f in findings:
        status = f.get("status", "open")
        if status not in resolved_states:
            # open/surfaced — always keep
            kept.append(f)
            continue
        ts = f.get("detected_at", "")
        try:
            detected = datetime.strptime(ts, "%Y-%m-%dT%H:%M:%SZ").replace(
                tzinfo=timezone.utc
            )
        except (TypeError, ValueError):
            # Unparseable timestamp — keep it, fail-open
            kept.append(f)
            continue
        if detected >= cutoff:
            kept.append(f)
        else:
            dropped += 1

    if dropped == 0:
        print(
            f"(nothing to compact: {len(findings)} findings, "
            f"none resolved >{max_age_days}d ago)"
        )
        return 0

    _write_findings_atomic(kept)
    log(
        f"compact_findings: dropped {dropped} resolved findings older than {max_age_days}d"
    )
    print(
        f"ok: compacted {dropped} finding(s) older than {max_age_days}d, "
        f"kept {len(kept)}"
    )
    return 0


# ---------------------------------------------------------------------------
# Severity routing
# ---------------------------------------------------------------------------


def escalate(finding: Finding) -> None:
    """Route high/critical findings beyond the findings.jsonl file.

    High findings: logged + surfaced via SessionStart hook (findings.jsonl).
    Critical findings: also stored in governance KG for visibility across agents.
    """
    log(f"ESCALATE {finding.severity.upper()} {finding.fingerprint} {finding.pattern} {finding.file}:{finding.line} — {finding.hint}", "warning")

    if finding.severity != "critical":
        return

    # --- Governance KG discovery ---
    _escalate_to_kg(finding)


def _escalate_to_kg(finding: Finding) -> None:
    """Store a critical finding as a discovery in the governance knowledge graph."""
    from unitares_sdk import SyncGovernanceClient

    summary = f"[Watcher] {finding.pattern}: {finding.hint} ({Path(finding.file).name}:{finding.line})"
    details = (
        f"Pattern: {finding.pattern}\n"
        f"File: {finding.file}:{finding.line}\n"
        f"Hint: {finding.hint}\n"
        f"Fingerprint: {finding.fingerprint}"
    )
    try:
        client = SyncGovernanceClient(rest_url=GOV_REST_URL, transport="rest", timeout=30)
        client.store_discovery(
            summary=summary,
            discovery_type="bug_found",
            severity="critical",
            tags=["watcher", finding.pattern, "critical"],
            details=details,
        )
        log(f"KG discovery stored for {finding.fingerprint}", "info")
    except Exception as e:
        log(f"KG discovery write failed: {e}", "warning")
