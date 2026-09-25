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
import os
import subprocess
import time
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from agents.common.findings import post_finding
from agents.watcher._util import (
    findings_state_lock as _findings_state_lock,
    log,
    watcher_state_dir,
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

STATE_DIR = watcher_state_dir()
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


def findings_state_lock(wait_s: float = 2.0):
    """Lock the currently configured findings state directory."""
    return _findings_state_lock(STATE_DIR, wait_s=wait_s)


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
    # Snapshot of the offending source line, captured at detection. This is
    # what lets a finding outlive the file it was found in: worktrees get
    # pruned and scratch dirs vanish, but the evidence stays reviewable — and
    # therefore adjudicable. See the retention branch in _sweep_stale_quiet.
    line_content: str = ""
    # Set by the sweep when the target path disappeared but the finding was
    # retained on the strength of its snapshot. Display-only: it tells the
    # reader "this code is gone" so they judge the snippet, not the path.
    path_gone: bool = False

    def __post_init__(self) -> None:
        if not self.fingerprint:
            self.fingerprint = self.compute_fingerprint()

    def compute_fingerprint(self) -> str:
        """Stable identifier combining pattern, file, line, and (optionally)
        a content hash. Callers that want content-aware dedup should set
        ``line_content_hash`` BEFORE invoking this and then assign the
        result back to ``fingerprint``.

        Absolute file identity is retained in the fingerprint. Watcher state
        is shared across worktrees, so collapsing identical repo-relative
        paths would deduplicate away the second worktree's only actionable
        finding. Legacy relative paths remain normalized but un-attributed.
        """
        path = Path(self.file)
        try:
            normalized_path = (
                path.resolve().as_posix() if path.is_absolute() else path.as_posix()
            )
        except OSError:
            normalized_path = path.as_posix()
        key = f"{self.pattern}|{normalized_path}|{self.line}|{self.line_content_hash}"
        return hashlib.sha256(key.encode()).hexdigest()[:16]


# ---------------------------------------------------------------------------
# Dedup (data/watcher/dedup.json)
# ---------------------------------------------------------------------------


def _unique_tmp_path(target: Path) -> Path:
    return target.with_suffix(f"{target.suffix}.tmp.{os.getpid()}.{time.monotonic_ns()}")


def load_dedup() -> dict[str, str]:
    """Return mapping of fingerprint → detected_at timestamp."""
    if not DEDUP_FILE.exists():
        return {}
    try:
        return json.loads(DEDUP_FILE.read_text())
    except (json.JSONDecodeError, OSError):
        return {}


def save_dedup(dedup: dict[str, str]) -> None:
    """Atomically persist ``dedup.json``.

    The scan hook can overlap with itself across fast edit bursts, so direct
    ``write_text`` is too fragile: a crash or colliding writer can leave a
    truncated dedup gate. Use the same tmp+rename discipline as the other
    Watcher state files, with a unique temp name per writer.
    """
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    tmp = _unique_tmp_path(DEDUP_FILE)
    try:
        with tmp.open("w") as fh:
            json.dump(dedup, fh, indent=2, sort_keys=True)
        tmp.replace(DEDUP_FILE)
    except BaseException:
        try:
            tmp.unlink()
        except OSError:
            pass
        raise


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


def _absolute_provenance_key(
    file_path: str,
    pattern: str,
    line: Any,
    line_content_hash: str,
) -> tuple[str, str, int, str] | None:
    """Identity used only to bridge pre-absolute fingerprint rows."""
    try:
        normalized_line = int(line or 0)
    except (TypeError, ValueError):
        return None
    path = Path(file_path)
    if not path.is_absolute():
        return None
    try:
        canonical_path = path.resolve().as_posix()
    except OSError:
        canonical_path = path.as_posix()
    return pattern, canonical_path, normalized_line, line_content_hash


def persist_findings(new_findings: list[Finding]) -> list[Finding]:
    """Append new (non-duplicate) findings to findings.jsonl. Return the ones
    that were actually new (dedup filter applied)."""
    with findings_state_lock():
        dedup = load_dedup()
        original_dedup = dict(dedup)
        dedup = sweep_stale_dedup(dedup)
        legacy_fingerprints: dict[tuple[str, str, int, str], list[str]] = {}
        for row in _iter_findings_raw():
            old_fingerprint = row.get("fingerprint")
            key = _absolute_provenance_key(
                str(row.get("file") or ""),
                str(row.get("pattern") or ""),
                row.get("line"),
                str(row.get("line_content_hash") or ""),
            )
            if key is not None and isinstance(old_fingerprint, str):
                legacy_fingerprints.setdefault(key, []).append(old_fingerprint)
        fresh: list[Finding] = []
        now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        for f in new_findings:
            if f.fingerprint in dedup:
                continue  # already flagged this one
            key = _absolute_provenance_key(
                f.file,
                f.pattern,
                f.line,
                f.line_content_hash,
            )
            prior_timestamps = [
                dedup[old_fingerprint]
                for old_fingerprint in legacy_fingerprints.get(key, [])
                if old_fingerprint in dedup
            ]
            if prior_timestamps:
                # Rollout bridge: retain the existing row and lifecycle while
                # teaching dedup its new absolute-path fingerprint. Exact
                # canonical provenance keeps separate repos/worktrees apart.
                dedup[f.fingerprint] = max(prior_timestamps)
                continue
            dedup[f.fingerprint] = now
            fresh.append(f)

        if fresh or dedup != original_dedup:
            # Persist even if `fresh` is empty, so the sweep's pruning actually
            # lands on disk. Otherwise stale entries would rematerialize on the
            # next scan.
            STATE_DIR.mkdir(parents=True, exist_ok=True)
            for finding in fresh:
                _append_finding_row(finding)
            save_dedup(dedup)

    for finding in fresh:
        _emit_persisted_finding(finding)
    return fresh


def _watcher_agent_id() -> str:
    """Watcher's governance UUID for finding attribution, or the legacy slug.

    Why this exists: ``audit.events.agent_id`` is what
    ``http_sentinel_adjudicate`` resolves through ``_finding_producer_uuid`` to
    decide **whose** EISV an adjudicated outcome is booked against. Measured
    2026-08-20 over 30d, ``watcher_finding`` wrote the bare slug on 17 of 17
    rows while ``watcher_resolution_finding`` — the same agent, a different
    function — wrote a real UUID on 7 of 7. Slug rows are unadjudicatable: the
    endpoint returns 422 rather than book the outcome against the wrong
    resident. So they are refutable claims that can never become anchors.

    Deliberately does NOT call governance. ``persist_finding`` runs on the
    PostToolUse hook path, on every edit, and adding a network round-trip there
    is the substrate-tax bug class. Both sources are free: the in-process
    identity when the agent resolved one this cycle, else the on-disk anchor
    ``_load_session`` already maintains.

    Falls back to the slug rather than skipping the post. A finding that is
    merely unattributable is worth strictly more than one that was never
    surfaced — this path also feeds the Discord bridge and the SessionStart
    summary. Degrading keeps today's behaviour exactly when identity is
    unavailable.
    """
    # Function-local imports: agent.py imports this module, so top-level would
    # be circular. Matches the existing deferred-import pattern below.
    try:
        from agents.watcher.agent import get_watcher_identity

        identity = get_watcher_identity()
        if identity and identity.get("agent_uuid"):
            return identity["agent_uuid"]
    except Exception:
        pass
    try:
        from agents.watcher.agent import _load_session

        uuid = (_load_session() or {}).get("agent_uuid")
        if uuid:
            return uuid
    except Exception:
        pass
    return "watcher"


def persist_finding(finding: Finding) -> None:
    """Append a new finding to findings.jsonl and, for high/critical severity,
    mirror it into the governance event stream so the Discord bridge surfaces it.

    Low/medium stays local — the SessionStart hook handles surfacing those
    to the in-editor Claude session.

    The caller is responsible for the dedup gate; this function does NOT
    check dedup itself.
    """
    with findings_state_lock():
        _append_finding_row(finding)
    _emit_persisted_finding(finding)


def _append_finding_row(finding: Finding) -> None:
    """Append one local row; caller holds the findings state lock."""
    FINDINGS_FILE.parent.mkdir(parents=True, exist_ok=True)
    with FINDINGS_FILE.open("a") as f:
        f.write(json.dumps(asdict(finding)) + "\n")


def _emit_persisted_finding(finding: Finding) -> None:
    """Mirror a persisted high-severity finding after releasing the file lock."""
    if finding.severity in ("high", "critical"):
        post_finding(
            event_type="watcher_finding",
            severity=finding.severity,
            message=f"[{finding.pattern}] {finding.file}:{finding.line} — {finding.hint}",
            agent_id=_watcher_agent_id(),
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
    tmp = _unique_tmp_path(FINDINGS_FILE)
    try:
        with tmp.open("w") as fh:
            for f in findings:
                fh.write(json.dumps(f) + "\n")
        tmp.replace(FINDINGS_FILE)
    except BaseException:
        try:
            tmp.unlink()
        except OSError:
            pass
        raise


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


def _finding_target_exists(finding: dict[str, Any]) -> bool:
    path = finding.get("file", "")
    if not path:
        return False
    candidate = Path(path)
    # Legacy hook records may be relative to an edit worktree that is no
    # longer knowable. Never reinterpret them against the cwd of whichever
    # later session happens to surface the shared findings file.
    if not candidate.is_absolute():
        return True
    return candidate.exists()


def update_finding_status(
    fingerprint_prefix: str,
    new_status: str,
    resolver_agent_id: str | None = None,
    reason: str | None = None,
    *,
    emit_resolution_event: bool = True,
    updated_finding_sink: list[dict[str, Any]] | None = None,
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
    updated_target: dict[str, Any] | None = None
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
            updated_target = merged
        updated.append(f)
    _write_findings_atomic(updated)
    if updated_finding_sink is not None and updated_target is not None:
        updated_finding_sink.append(updated_target)
    log(f"update_finding_status: {target_fp[:8]} → {new_status}")
    print(
        f"ok: {target_fp[:16]} → {new_status} "
        f"({matches[0].get('pattern','?')} at {matches[0].get('file','?')}:{matches[0].get('line','?')})"
    )

    # --- Post resolution event to governance ---
    if emit_resolution_event and new_status in ("confirmed", "dismissed"):
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
    newly_marked = 0
    for f in findings:
        if _finding_target_exists(f):
            kept.append(f)
        elif f.get("line_content"):
            # The path is gone, but this finding carries its own evidence, so
            # there IS still something to evaluate. Retain it.
            #
            # This branch exists because the unconditional drop silently
            # destroyed real findings: Watcher scans worktrees and scratch
            # dirs, those paths are deleted as a matter of routine, and the
            # sweep runs from surface_pending — so the chime deleted findings
            # on its way to displaying them. Measured 2026-08-12: 12 of 12
            # recent target files no longer existed, findings.jsonl held zero
            # open entries, and both surfacing paths printed nothing while
            # every health check passed.
            #
            # Retention is not just cosmetic. An adjudicated finding yields
            # one external_signal outcome, and label breadth is the binding
            # constraint on outcome validation — so a dropped finding is a
            # discarded label, not merely a missed notification.
            if not f.get("path_gone"):
                f = {**f, "path_gone": True}
                newly_marked += 1
            kept.append(f)
        else:
            dropped += 1

    # Marking path_gone is itself a state change worth persisting, so the early
    # return has to consider both. Returning on `dropped == 0` alone would
    # leave the flag unwritten until some later sweep happened to drop
    # something — the exact class of silent staleness this function exists for.
    if dropped == 0 and newly_marked == 0:
        return 0

    _write_findings_atomic(kept)
    log(
        f"sweep_stale_findings (auto): dropped {dropped} findings for missing "
        f"files, retained {newly_marked} with a source snapshot"
    )
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


def _current_source_line(path: str, line: int) -> str | None:
    """Return the current text of ``line`` (1-based) in ``path``, or None if
    the file is missing, unreadable, or the line is out of range.

    Used to re-validate a finding's flagged construct against current disk
    content after detection — line numbers drift as files are later edited.
    """
    if not path or line < 1:
        return None
    p = Path(path)
    # A relative legacy record has no durable worktree provenance. Reading it
    # against the current hook cwd could age out another worktree's finding.
    if not p.is_absolute() or not p.exists():
        return None
    try:
        with p.open("r", encoding="utf-8", errors="replace") as fh:
            for i, text in enumerate(fh, start=1):
                if i == line:
                    return text.rstrip("\n")
    except OSError:
        return None
    return None  # line beyond EOF


def _sweep_token_drift_quiet() -> int:
    """Age out open/surfaced findings whose flagged line no longer carries the
    pattern's required token.

    Closes the line-drift staleness gap (patterns.md P016 note, 2026-05-04): a
    finding is validated by the required-token verifier at DETECTION time, but
    its stored line number drifts as the file is later edited, so the same
    finding can surface pointing at a line that no longer holds the construct
    (a return statement, a comment, a blank line). The detection gate
    (``_PATTERN_REQUIRED_TOKENS`` in agent.py) is replayed here against CURRENT
    disk content; when the required token is gone, the finding is stale and is
    aged out rather than re-shown.

    Conservative by construction:
      * only token-gated patterns are touched (others have no token to replay);
      * a line that STILL holds its required token is never aged out, so a real
        match is never silently dropped;
      * unreadable/out-of-range lines are left alone — the file-existence sweep
        (``_sweep_stale_quiet``) and a future scan cover those.

    Aged out (not deleted) so the audit trail and precision math keep the
    record; ``--compact`` reclaims it after the TTL. Quiet variant — logs when
    non-zero, never prints — safe to call from the chime path where stdout
    becomes agent context. Returns the count aged out.
    """
    # Lazy import: the token map lives in agent.py, which imports this module
    # at top level. A top-level import here would be circular (same reason as
    # the _post_resolution_event lazy import in update_finding_status).
    from agents.watcher.agent import _PATTERN_REQUIRED_TOKENS

    findings = _iter_findings_raw()
    if not findings:
        return 0

    now_iso = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    aged = 0
    out: list[dict[str, Any]] = []
    for f in findings:
        if f.get("status", "open") in ("open", "surfaced"):
            tokens = _PATTERN_REQUIRED_TOKENS.get(f.get("pattern", ""))
            if tokens:
                src_line = _current_source_line(
                    f.get("file", ""), int(f.get("line", 0) or 0)
                )
                if src_line is not None and not any(tok in src_line for tok in tokens):
                    f = {
                        **f,
                        "status": "aged_out",
                        "aged_out_at": now_iso,
                        "resolution_reason": "line_drift_token_absent",
                    }
                    aged += 1
        out.append(f)

    if aged == 0:
        return 0

    _write_findings_atomic(out)
    log(
        f"sweep_token_drift (auto): aged out {aged} finding(s) whose flagged "
        f"line no longer carries the pattern's required token"
    )
    return aged


# ---------------------------------------------------------------------------
# Surfacing — how findings reach host sessions
#
# Two hooks call the functions below:
#
#   SessionStart → --print-unresolved (read-only, shows open+surfaced so the
#     new session sees the full backlog — if it only showed open, findings
#     already "surfaced" in a previous session would silently disappear from
#     context)
#
#   UserPromptSubmit → --surface-pending (chime mode, in agent.py because
#     it also triggers a governance check-in; federated callers pass a stable
#     audience key so delivery by one host does not consume another host's
#     notification)
#
# Both print a <unitares-watcher-findings> block that a host hook injects as
# additionalContext. The formatter is shared so the block shape stays
# consistent no matter which host emitted it.
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


# ---------------------------------------------------------------------------
# Display-time grouping of copies
#
# Watcher state is shared across worktrees and the fingerprint keeps the
# absolute path and line, so the same code flagged in five worktrees, or at a
# shifted line, is five rows. The listing groups those rows into one entry at
# render time only: findings.jsonl, the fingerprint, dismissal, outcomes and
# precision are untouched, and every copy's fingerprint stays on screen so
# each row can still be resolved or dismissed on its own.
# ---------------------------------------------------------------------------

# Per-render cache of git lookups: ``{directory: (git common dir, worktree
# toplevel) or None}`` plus ``{"ignored:<path>": bool}`` from ``_git_ignores``.
GitCache = dict[str, Any]


def _git_worktree_of_dir(directory: Path, cache: GitCache) -> tuple[str, str] | None:
    """Return ``(common_dir, toplevel)`` for an existing directory, or None.

    The common dir identifies the repository across all of its worktrees; the
    toplevel identifies the worktree. Bounded to 2s like the scope lookup,
    because SessionStart latency is user-visible.
    """
    key = str(directory)
    if key in cache:
        return cache[key]
    info: tuple[str, str] | None = None
    try:
        result = subprocess.run(
            [
                "git",
                "-C",
                key,
                "rev-parse",
                "--path-format=absolute",
                "--git-common-dir",
                "--show-toplevel",
            ],
            capture_output=True,
            text=True,
            timeout=2.0,
            check=False,
        )
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
        result = None
    if result is not None and result.returncode == 0:
        lines = result.stdout.strip().splitlines()
        if len(lines) == 2 and lines[0] and lines[1]:
            try:
                info = (
                    Path(lines[0]).resolve().as_posix(),
                    Path(lines[1]).resolve().as_posix(),
                )
            except OSError:
                info = None
    cache[key] = info
    return info


def _git_location(
    file_path: str, cache: GitCache
) -> tuple[str, str, str] | None:
    """Return ``(common_dir, toplevel, repo_relative_path)`` for a finding's
    file, or None when it cannot be placed in a git worktree.

    Only absolute paths whose directory still exists are placed: a legacy
    relative path has no known origin, and walking up from a removed worktree
    would land in whatever repository happens to contain it.
    """
    if not file_path:
        return None
    path = Path(file_path)
    if not path.is_absolute():
        return None
    try:
        parent = path.parent.resolve()
    except OSError:
        return None
    if not parent.is_dir():
        return None
    info = _git_worktree_of_dir(parent, cache)
    if info is None:
        return None
    common_dir, toplevel = info
    try:
        rel = (parent / path.name).relative_to(Path(toplevel)).as_posix()
    except ValueError:
        return None
    return common_dir, toplevel, rel


# Patterns whose ``line_content_hash`` is not a hash of the source line.
# Review findings (R000) hash the hint text, so two unrelated lines with the
# same observation would otherwise look like copies of one line.
_UNGROUPED_PATTERNS = frozenset({"R000"})


def _group_copies(
    findings: list[dict[str, Any]], cache: GitCache
) -> list[list[dict[str, Any]]]:
    """Group findings that are copies of the same flagged code.

    Copies share the repository (git common dir), the repo-relative path, the
    pattern, the line content hash and the displayed severity. Findings with
    no content hash, with a pattern in ``_UNGROUPED_PATTERNS``, or that cannot
    be placed in a git worktree stay on their own. Groups keep the order of
    their first member, and members keep the input order.
    """
    groups: dict[Any, list[dict[str, Any]]] = {}
    for index, f in enumerate(findings):
        content_hash = str(f.get("line_content_hash") or "")
        pattern = f.get("pattern", "")
        location = (
            None
            if pattern in _UNGROUPED_PATTERNS
            else _git_location(str(f.get("file") or ""), cache)
        )
        if content_hash and location is not None:
            # Severity is part of the key so a group never shows, and marks
            # surfaced, a row the display rules would have hidden.
            key: Any = (
                location[0],
                location[2],
                pattern,
                content_hash,
                f.get("severity", "low"),
            )
        else:
            key = ("single", index)
        groups.setdefault(key, []).append(f)
    return list(groups.values())


def _format_finding_line(f: dict[str, Any]) -> list[str]:
    sev = str(f.get("severity", "?")).upper()
    pat = f.get("pattern", "?")
    vcls = f.get("violation_class", "")
    file = f.get("file", "?")
    line_no = f.get("line", "?")
    hint = f.get("hint", "")
    fp = str(f.get("fingerprint", ""))[:8]
    cls_tag = f"[{vcls}] " if vcls else ""
    lines = [f"  [{sev}] {cls_tag}{pat} {file}:{line_no} — {hint}  (#{fp}){_status_marker(f)}"]
    lines.extend(_retained_line(f))
    return lines


def _format_group_lines(
    group: list[dict[str, Any]],
    cache: GitCache,
    members: list[dict[str, Any]] | None = None,
) -> list[str]:
    """Render a group: one entry line, then one line per displayed member.

    ``members`` is the part of ``group`` the display cap left room for (all
    of it by default). Members the cap cut are summarised in one line and
    are not listed, so they are not marked surfaced either.
    """
    members = group if members is None else members
    first = group[0]
    sev = str(first.get("severity", "?")).upper()
    pat = first.get("pattern", "?")
    vcls = first.get("violation_class", "")
    hint = first.get("hint", "")
    cls_tag = f"[{vcls}] " if vcls else ""
    location = _git_location(str(first.get("file") or ""), cache)
    rel = location[2] if location else first.get("file", "?")
    worktrees = {
        loc[1]
        for loc in (_git_location(str(f.get("file") or ""), cache) for f in group)
        if loc is not None
    }
    # Only the flagged line is hashed, so distinct handlers that share the
    # same line text (a bare `except Exception:`) in one file land in one
    # group too. Each location stays listed, so say "locations", not copies.
    where = f"{len(group)} locations, same line text, {len(worktrees)} worktree(s)"
    lines = [f"  [{sev}] {cls_tag}{pat} {rel} — {hint}  ({where})"]
    for f in members:
        loc = _git_location(str(f.get("file") or ""), cache)
        label = Path(loc[1]).name if loc else "?"
        fp = str(f.get("fingerprint", ""))[:8]
        # Every row in the group is marked surfaced, so a hint that differs
        # from the entry's must reach the screen too.
        own_hint = f.get("hint", "")
        hint_note = f" — {own_hint}" if own_hint != hint else ""
        lines.append(
            f"    {label}: {f.get('file', '?')}:{f.get('line', '?')}{hint_note}"
            f"  (#{fp}){_status_marker(f)}"
        )
    hidden = len(group) - len(members)
    if hidden:
        lines.append(
            f"    +{hidden} more location(s) not shown (display cap); "
            "they stay open for a later listing"
        )
    snapshot_source = next(
        (f for f in members if f.get("path_gone") and f.get("line_content")), None
    )
    if snapshot_source is not None:
        lines.extend(_retained_line(snapshot_source))
    return lines


def _status_marker(f: dict[str, Any]) -> str:
    status = f.get("status", "open")
    marker = "" if status == "open" else f" ({status})"
    if f.get("path_gone"):
        marker += " (path gone; snapshot retained)"
    return marker


def _retained_line(f: dict[str, Any]) -> list[str]:
    if not (f.get("path_gone") and f.get("line_content")):
        return []
    # JSON quoting keeps a retained source line visibly data-shaped and
    # escapes control characters before it enters an agent's context.
    snapshot = str(f["line_content"]).strip()[:240]
    return [f"    retained source line: {json.dumps(snapshot)}"]


def _format_findings_block(
    findings: list[dict[str, Any]],
    *,
    header: str,
    out_of_scope_groups: dict[str, int] | None = None,
    git_cache: GitCache | None = None,
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
      - medium: shown only if there's room under the 10-row display cap
        reserved for critical+high (keeps session context from drowning in
        medium-severity noise while still surfacing some)
      - low: never shown (file-only signal)

    Copies of the same flagged code (see ``_group_copies``) render as one
    entry that lists each displayed copy's location and fingerprint. The
    display cap still counts rows, that is fingerprint lines, not entries, so
    grouping never shows more rows than the ungrouped listing did: a medium
    group that does not fit shows the copies that do plus a "+K more" line.
    ``shown`` holds exactly the copies whose fingerprint was on screen.

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

    cache: GitCache = {} if git_cache is None else git_cache
    # Findings are sorted, so a group's first member is its most severe.
    entries = _group_copies(findings, cache)
    critical_high = [
        g for g in entries if g[0].get("severity") in ("critical", "high")
    ]
    medium = [g for g in entries if g[0].get("severity") == "medium"]
    # (group, displayed members). The cap budgets rows, as it did before
    # grouping, so a large medium group cannot flood the block.
    shown_entries: list[tuple[list[dict[str, Any]], list[dict[str, Any]]]] = [
        (g, g) for g in critical_high
    ]
    budget = 10 - sum(len(g) for g in critical_high)
    for group in medium:
        if budget <= 0:
            break
        members = group[:budget]
        shown_entries.append((group, members))
        budget -= len(members)
    shown = [f for _group, members in shown_entries for f in members]

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
    for group, members in shown_entries:
        if len(group) == 1:
            lines.extend(_format_finding_line(group[0]))
        else:
            lines.extend(_format_group_lines(group, cache, members))
    lines.append("")
    if len(shown_entries) == len(shown):
        lines.append(f"Total unresolved: {len(findings)} (showing {len(shown)})")
    else:
        lines.append(
            f"Total unresolved: {len(findings)} (showing {len(shown)} as "
            f"{len(shown_entries)} entries; findings with the same pattern, "
            "repo path and line text are grouped)"
        )
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
    git_cache: GitCache | None = None,
    *,
    count_out_of_scope: bool = True,
) -> tuple[list[dict[str, Any]], dict[str, int]]:
    """Split findings into ``(in_scope, out_of_scope_groups)``.

    A finding is in-scope when its ``file`` lives under ``scope_root``.
    Out-of-scope findings are aggregated by the worktree they live in (see
    ``_worktree_label``) so the footer can summarize *where* the backlog is
    without listing every path.

    If ``scope_root`` is None, all findings are treated as in-scope —
    matches the legacy "surface everything" behavior so callers without
    a worktree (CI, ad-hoc CLI) keep the existing experience.

    Labelling can start git subprocesses. A caller that discards the counts
    passes ``count_out_of_scope=False``; the second value is then ``{}`` and
    no label is computed.
    """
    if scope_root is None:
        return list(findings), {}

    cache: GitCache = {} if git_cache is None else git_cache
    in_scope: list[dict[str, Any]] = []
    out_groups: dict[str, int] = {}
    resolved_scope = scope_root.resolve()
    for f in findings:
        file_path = f.get("file") or ""
        resolved_file: Path | None = None
        if file_path:
            candidate = Path(file_path)
            if candidate.is_absolute():
                resolved_file = candidate.resolve()
                try:
                    resolved_file.relative_to(resolved_scope)
                except ValueError:
                    pass
                else:
                    in_scope.append(f)
                    continue
        if not count_out_of_scope:
            continue
        label_path = str(resolved_file) if resolved_file is not None else file_path
        label = _worktree_label(label_path, cache)
        out_groups[label] = out_groups.get(label, 0) + 1
    return in_scope, out_groups


def _codex_worktree_label(path: Path) -> str | None:
    """``codex:<id>`` for a path under ``.codex/worktrees/<id>/``, else None.

    Every Codex worktree's toplevel is ``~/.codex/worktrees/<id>/<repo>``, so
    the directory name alone (``unitares``) cannot tell them apart."""
    parts = path.parts
    for i in range(len(parts) - 2):
        if parts[i] == ".codex" and parts[i + 1] == "worktrees":
            return f"codex:{parts[i + 2]}"
    return None


def _label_for_worktree_root(toplevel: str, common_dir: str) -> str:
    """Footer label for a worktree git placed.

    ``main`` for the main checkout (its ``.git`` is the common dir),
    ``codex:<id>`` for a Codex worktree, otherwise the worktree's own
    directory name (``~/projects/wt/<name>`` gives ``<name>``)."""
    root = Path(toplevel)
    if Path(common_dir) == root / ".git":
        return "main"
    return _codex_worktree_label(root) or root.name or "(root)"


def _worktree_label(file_path: str, cache: GitCache) -> str:
    """Name the worktree an out-of-scope finding lives in.

    When git can place the file, ``_label_for_worktree_root`` names the
    worktree: ``main`` for the main checkout, ``codex:<id>`` for a Codex
    worktree, else the worktree's directory name. Directory names alone are
    not enough: every Codex worktree and the main checkout share the name
    ``unitares``. For a path whose directory is gone, the nearest surviving
    ancestor decides: if it is inside a worktree, that worktree's label;
    otherwise the removed worktree, named ``codex:<id>`` under
    ``.codex/worktrees/`` or else by the first missing directory. Anything
    else (legacy relative paths, files outside git) falls back to
    ``_label_for_other_worktree``. Before this, a file under
    ``<worktree>/tests/`` was counted as ``tests``, so one worktree's backlog
    was split across its subdirectory names.
    """
    location = _git_location(file_path, cache)
    if location is not None:
        return _label_for_worktree_root(location[1], location[0])
    path = Path(file_path) if file_path else None
    if path is not None and path.is_absolute() and ".worktrees" not in path.parts:
        missing = path.parent
        while not missing.parent.is_dir() and missing.parent != missing:
            missing = missing.parent
        if not missing.is_dir() and missing.parent != missing:
            info = _git_worktree_of_dir(missing.parent, cache)
            if info is not None and not _git_ignores(missing, cache):
                return _label_for_worktree_root(info[1], info[0])
            # Outside git, or an ignored directory inside a checkout (a
            # worktree kept under `.claude/worktrees/`): the missing
            # directory is the removed worktree, not the enclosing checkout.
            return _codex_worktree_label(path) or missing.name
    return _label_for_other_worktree(file_path)


def _git_ignores(path: Path, cache: GitCache) -> bool:
    """True when git ignores ``path`` in the checkout around its parent.

    Nested worktree directories (``.claude/worktrees/<name>``) are ignored
    by the enclosing checkout; a deleted source directory normally is not.
    Errors count as not ignored.
    """
    key = f"ignored:{path}"
    if key in cache:
        return bool(cache[key])
    try:
        result = subprocess.run(
            [
                "git",
                "-C",
                str(path.parent),
                "check-ignore",
                "-q",
                "--no-index",
                str(path),
            ],
            capture_output=True,
            text=True,
            timeout=2.0,
            check=False,
        )
        ignored = result.returncode == 0
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
        ignored = False
    cache[key] = ignored
    return ignored


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

    findings: list[dict[str, Any]] = []
    for finding in _iter_findings_raw():
        if finding.get("status", "open") not in ("open", "surfaced"):
            continue
        if _finding_target_exists(finding):
            findings.append(finding)
            continue
        if finding.get("line_content"):
            # A retained snapshot is still adjudicable after its worktree is
            # removed. Mark only the display copy so SessionStart remains
            # strictly read-only; the next lifecycle sweep persists path_gone.
            findings.append({**finding, "path_gone": True})
    git_cache: GitCache = {}
    in_scope, out_groups = _partition_findings_by_scope(
        findings, scope_root, git_cache
    )

    block, _shown = _format_findings_block(
        in_scope,
        header=(
            "The UNITARES Watcher agent flagged the following unresolved code\n"
            "patterns in recently edited files. Watcher has a track record — these\n"
            "are not noise. Investigate or explicitly --dismiss them."
        ),
        out_of_scope_groups=out_groups or None,
        git_cache=git_cache,
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
