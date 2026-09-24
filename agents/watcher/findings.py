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
    hash_line_content,
    log,
    repo_identity,
    repo_relative_path,
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

# ``resolved_by`` stamped on a finding that persist_findings recorded as a
# duplicate of an existing unresolved finding (same pattern, same code, same
# repo-relative path, another worktree or a shifted line). Such a row is
# dismissed with reason ``dup`` and a ``duplicate_of`` pointer. Nobody
# adjudicated it, so it is never a resolution: no watcher_resolution event, no
# external_signal outcome, and it does not count as a dismissal in Watcher's
# self-reported confidence. See ``is_auto_duplicate``.
AUTO_DEDUP_RESOLVER = "watcher_auto_dedup"

_UNRESOLVED_STATUSES = ("open", "surfaced")


# Verdicts on a canonical finding that speak for the code itself, so they
# also settle its automatic duplicates. Any other closure (confirmed = fixed
# in that checkout, aged_out, or a dismissal that says nothing about the code)
# releases the duplicates: the same code may still be live in their worktrees.
_VERDICT_COVERS_DUPLICATES = frozenset({"fp", "wont_fix", "out_of_scope"})


def is_auto_duplicate(row: dict[str, Any]) -> bool:
    """True for a row persist_findings recorded as an automatic duplicate
    and that nobody has adjudicated since."""
    return (
        row.get("status") == "dismissed"
        and row.get("resolution_reason") == "dup"
        and row.get("resolved_by") == AUTO_DEDUP_RESOLVER
        and bool(row.get("duplicate_of"))
    )


def live_auto_duplicate_fps(rows: list[dict[str, Any]]) -> set[str]:
    """Fingerprints of auto-duplicates whose canonical finding is unresolved.

    Such a copy still stands for live code in its own worktree, so the
    consumers that act per worktree (scoped delivery, the ship trailer, the
    commit scanner, compaction) treat it like an unresolved finding.
    """
    unresolved = {
        row.get("fingerprint")
        for row in rows
        if row.get("status", "open") in _UNRESOLVED_STATUSES
    }
    return {
        str(row.get("fingerprint"))
        for row in rows
        if is_auto_duplicate(row) and row.get("duplicate_of") in unresolved
    }


def _same_file(a: str, b: str) -> bool:
    if not a or not b:
        return False
    try:
        return Path(a).resolve() == Path(b).resolve()
    except OSError:
        return a == b


def release_orphaned_duplicates(
    rows: list[dict[str, Any]], now: str | None = None
) -> tuple[list[dict[str, Any]], int]:
    """Reopen automatic duplicates whose canonical finding closed.

    Folding a copy into a canonical finding is only sound while that finding
    is unresolved. Once it is confirmed (fixed in its own checkout), aged out,
    or dismissed for a reason that says nothing about the code, the copies in
    other worktrees may still hold the bug, so the first copy is reopened as
    the new canonical finding and any others are re-pointed at it. A verdict
    about the code (``fp``/``wont_fix``/``out_of_scope``) settles the copies
    too, as does any closure for a copy in the canonical's own file (that is
    the same site after a line shift), and a canonical row that no longer
    exists leaves them as they are. Returns the new rows and how many copies were reopened.
    """
    by_fp = {row.get("fingerprint"): row for row in rows if row.get("fingerprint")}
    promoted: dict[str, str] = {}
    stamp = now or datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    released = 0
    out: list[dict[str, Any]] = []
    for row in rows:
        if not is_auto_duplicate(row):
            out.append(row)
            continue
        canonical_fp = str(row["duplicate_of"])
        canonical = by_fp.get(canonical_fp)
        if canonical is None or canonical.get("status", "open") in _UNRESOLVED_STATUSES:
            out.append(row)
            continue
        if (
            canonical.get("status") == "dismissed"
            and canonical.get("resolution_reason") in _VERDICT_COVERS_DUPLICATES
        ) or _same_file(str(row.get("file") or ""), str(canonical.get("file") or "")):
            # A verdict about the code, or a copy in the canonical's own file
            # (a line shift): the closure already speaks for this copy.
            out.append(row)
            continue
        if canonical_fp in promoted:
            out.append({**row, "duplicate_of": promoted[canonical_fp]})
            continue
        reopened = {
            key: value
            for key, value in row.items()
            if key not in ("status", "dismissed_at", "resolved_by", "resolution_reason", "duplicate_of")
        }
        reopened.update(
            status="open",
            released_from_duplicate_of=canonical_fp,
            released_at=stamp,
        )
        promoted[canonical_fp] = str(row.get("fingerprint") or "")
        released += 1
        out.append(reopened)
    if released:
        log(f"released {released} auto-duplicate finding(s) whose canonical finding closed")
    return out, released


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
    # Hash of the flagged line plus its neighbours, captured at detection.
    # NOT part of the fingerprint. Insert-time dedupe uses it to recognise the
    # same code at a shifted line number without mistaking a second identical
    # one-liner (`pass`, `except Exception:`) elsewhere in the file for it.
    context_hash: str = ""
    # Path relative to the git worktree root, captured at persist time so a
    # finding can still be matched after its worktree is removed. NOT part of
    # the fingerprint either.
    repo_relpath: str = ""
    # The repository's shared git dir (see ``repo_identity``), captured with
    # ``repo_relpath``: identical relative paths in two different repos are
    # not duplicates. NOT part of the fingerprint.
    repo_id: str = ""

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


def _row_code_location(row: dict[str, Any]) -> tuple[str, str]:
    """``(repo_id, repo_relpath)`` of a stored finding, for duplicate matching.

    Prefers the values captured at persist time. Older rows predate them; for
    those they are derived from ``file`` while the file still exists. An
    absolute path that cannot be resolved is compared verbatim, which can only
    ever match the same file. A legacy relative path has no knowable worktree
    or repo, so it matches nothing (empty relpath).
    """
    stored = row.get("repo_relpath")
    if isinstance(stored, str) and stored:
        return str(row.get("repo_id") or ""), stored
    file_path = str(row.get("file") or "")
    if not file_path or not Path(file_path).is_absolute():
        return "", ""
    if Path(file_path).exists():
        return repo_identity(file_path), repo_relative_path(file_path)
    return "", file_path


def _find_duplicate_target(
    finding: Finding,
    relpath: str,
    candidates: list[dict[str, Any]],
) -> dict[str, Any] | None:
    """The unresolved finding ``finding`` duplicates, or None.

    A duplicate is the same pattern on the same code (``line_content_hash``)
    at the same repo-relative path of the same repository (``repo_id``), in
    another worktree or at a shifted line.
    ``candidates`` are the unresolved rows already sharing pattern and line
    hash. The line hash alone is too weak for a shifted line: one-liners like
    ``pass`` or ``except Exception:`` repeat within a file. So a line shift is
    accepted only when both rows carry an equal ``context_hash`` (the flagged
    line and its neighbours); rows without one match only at the same line.
    Finally, when this finding's own file still holds the same code at the
    candidate's line, the candidate's site is still in place here and this
    finding is a second site, not a moved one.
    """
    for row in candidates:
        if row.get("fingerprint") == finding.fingerprint:
            continue
        row_repo, row_relpath = _row_code_location(row)
        if not row_relpath or (row_repo, row_relpath) != (finding.repo_id, relpath):
            continue
        try:
            row_line = int(row.get("line") or 0)
        except (TypeError, ValueError):
            continue
        row_context = str(row.get("context_hash") or "")
        if row_context and finding.context_hash:
            if row_context != finding.context_hash:
                continue
        elif row_line != finding.line:
            continue
        if row_line != finding.line:
            current = _current_source_line(finding.file, row_line)
            if current is not None and hash_line_content(current) == finding.line_content_hash:
                continue
        return row
    return None


def _auto_duplicate_row(finding: Finding, canonical: dict[str, Any], now: str) -> dict[str, Any]:
    """The findings.jsonl row recording ``finding`` as a duplicate.

    Kept, never deleted: the row carries its own path, line and snapshot, and
    ``duplicate_of`` names the unresolved finding it folds into.
    """
    return {
        **asdict(finding),
        "status": "dismissed",
        "dismissed_at": now,
        "resolved_by": AUTO_DEDUP_RESOLVER,
        "resolution_reason": "dup",
        "duplicate_of": canonical.get("fingerprint", ""),
    }


def persist_findings(new_findings: list[Finding]) -> list[Finding]:
    """Append new (non-duplicate) findings to findings.jsonl. Return the ones
    that were actually new (dedup filter applied).

    A finding that duplicates an existing unresolved one (see
    ``_find_duplicate_target``) is appended as an auto-dismissed ``dup`` row
    pointing at it, and is not returned: it is neither surfaced, escalated nor
    mirrored to the event stream. Identical code in N worktrees, or the same
    block after an edit above it, stays one unresolved item.
    """
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    with findings_state_lock():
        dedup = load_dedup()
        original_dedup = dict(dedup)
        dedup = sweep_stale_dedup(dedup)
        legacy_fingerprints: dict[tuple[str, str, int, str], list[str]] = {}
        existing_rows, released = release_orphaned_duplicates(_iter_findings_raw(), now)
        rewrite = bool(released)
        unresolved_fps: set[str] = set()
        dup_candidates: dict[tuple[str, str], list[dict[str, Any]]] = {}
        for row in existing_rows:
            if row.get("status", "open") in _UNRESOLVED_STATUSES:
                fp = row.get("fingerprint")
                if isinstance(fp, str) and fp:
                    unresolved_fps.add(fp)
                line_hash = str(row.get("line_content_hash") or "")
                if line_hash:
                    dup_candidates.setdefault(
                        (str(row.get("pattern") or ""), line_hash), []
                    ).append(row)
        # A re-detection after the dedup TTL must not append a second row with
        # the fingerprint of an existing auto-duplicate: either it is still
        # folded into a live finding, or a verdict about the code settled it
        # (a released copy is open again and already in unresolved_fps). A
        # second row would also make --dismiss/--resolve on it ambiguous.
        auto_dup_fps = {row.get("fingerprint") for row in existing_rows if is_auto_duplicate(row)}
        auto_dups: list[dict[str, Any]] = []
        for row in existing_rows:
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
            if f.fingerprint in unresolved_fps or f.fingerprint in auto_dup_fps:
                # Already on record and still live; the dedup TTL lapsed.
                continue
            canonical = None
            if Path(f.file).is_absolute():
                if not f.repo_relpath:
                    f.repo_relpath = repo_relative_path(f.file)
                    f.repo_id = repo_identity(f.file)
                if f.line_content_hash:
                    canonical = _find_duplicate_target(
                        f,
                        f.repo_relpath,
                        dup_candidates.get((f.pattern, f.line_content_hash), []),
                    )
            if canonical is not None:
                if _same_file(f.file, str(canonical.get("file") or "")):
                    # Same site after an edit above it: keep the canonical
                    # finding pointing at where the code is now. Its
                    # fingerprint stays its lifecycle id; the first line is
                    # kept for the record.
                    canonical.setdefault("first_line", canonical.get("line"))
                    canonical["line"] = f.line
                    canonical["line_content"] = f.line_content
                    if f.context_hash:
                        canonical["context_hash"] = f.context_hash
                    rewrite = True
                auto_dups.append(_auto_duplicate_row(f, canonical, now))
                log(
                    f"auto-dup {f.pattern} {f.file}:{f.line} ({f.fingerprint}) "
                    f"→ duplicate_of {canonical.get('fingerprint', '?')} "
                    f"at {canonical.get('file', '?')}:{canonical.get('line', '?')}"
                )
                continue
            fresh.append(f)

        if rewrite:
            # A release or a relocation changed existing rows: rewrite the
            # whole file once, new rows included.
            _write_findings_atomic(
                existing_rows + [asdict(finding) for finding in fresh] + auto_dups
            )
        if fresh or auto_dups or rewrite or dedup != original_dedup:
            # Persist even if `fresh` is empty, so the sweep's pruning actually
            # lands on disk. Otherwise stale entries would rematerialize on the
            # next scan.
            STATE_DIR.mkdir(parents=True, exist_ok=True)
            if not rewrite:
                for finding in fresh:
                    _append_finding_row(finding)
                if auto_dups:
                    with FINDINGS_FILE.open("a") as fh:
                        for row in auto_dups:
                            fh.write(json.dumps(row) + "\n")
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
            base = f
            if is_auto_duplicate(f):
                # Someone adjudicated this copy directly. It stops being an
                # automatic duplicate: the verdict is theirs, not the fold's.
                base = {
                    key: value
                    for key, value in f.items()
                    if key not in ("dismissed_at", "resolved_by", "resolution_reason", "duplicate_of")
                }
                base["was_duplicate_of"] = f["duplicate_of"]
            merged = {**base, "status": new_status}
            if timestamp_field:
                merged[timestamp_field] = now_iso
            if resolver_agent_id:
                merged["resolved_by"] = resolver_agent_id
            if reason:
                merged["resolution_reason"] = reason
            f = merged
            updated_target = merged
        updated.append(f)
    updated, _released = release_orphaned_duplicates(updated)
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

    out, _released = release_orphaned_duplicates(out)
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
        if f.get("path_gone"):
            marker += " (path gone; snapshot retained)"
        cls_tag = f"[{vcls}] " if vcls else ""
        lines.append(f"  [{sev}] {cls_tag}{pat} {file}:{line_no} — {hint}  (#{fp}){marker}")
        if f.get("path_gone") and f.get("line_content"):
            # JSON quoting keeps a retained source line visibly data-shaped and
            # escapes control characters before it enters an agent's context.
            snapshot = str(f["line_content"]).strip()[:240]
            lines.append(f"    retained source line: {json.dumps(snapshot)}")
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


def auto_duplicate_aliases(rows: list[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    """Map each canonical fingerprint to its automatic-duplicate rows.

    Scoped delivery uses this so folding a worktree's copy into another
    worktree's finding does not hide it from the first worktree: that
    worktree is shown its own copy (own path, line and fingerprint), so a
    ``--resolve``/``--dismiss`` there adjudicates that copy only.
    """
    out: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        if is_auto_duplicate(row) and row.get("file"):
            out.setdefault(str(row["duplicate_of"]), []).append(row)
    return out


def _path_under(file_path: str, resolved_scope: Path) -> bool:
    candidate = Path(file_path)
    if not file_path or not candidate.is_absolute():
        return False
    try:
        candidate.resolve().relative_to(resolved_scope)
    except ValueError:
        return False
    return True


def _partition_findings_by_scope(
    findings: list[dict[str, Any]],
    scope_root: Path | None,
    aliases: dict[str, list[dict[str, Any]]] | None = None,
) -> tuple[list[dict[str, Any]], dict[str, int]]:
    """Split findings into ``(in_scope, out_of_scope_groups)``.

    A finding is in-scope when its ``file`` lives under ``scope_root``, or
    when one of its automatic duplicates does (``aliases``, keyed by
    fingerprint; see ``auto_duplicate_aliases``). In the second case the
    in-scope entry is that duplicate row, shown with the canonical finding's
    status, so the reader sees their own checkout and acts on their own copy.
    Out-of-scope findings are
    aggregated by their nearest ``.worktrees`` sibling label so the footer
    can summarize *where* the backlog is without listing every path.

    If ``scope_root`` is None, all findings are treated as in-scope —
    matches the legacy "surface everything" behavior so callers without
    a worktree (CI, ad-hoc CLI) keep the existing experience.
    """
    if scope_root is None:
        return list(findings), {}

    in_scope: list[dict[str, Any]] = []
    out_groups: dict[str, int] = {}
    resolved_scope = scope_root.resolve()
    aliases = aliases or {}
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
        local = next(
            (
                alias
                for alias in aliases.get(str(f.get("fingerprint") or ""), ())
                if _path_under(alias["file"], resolved_scope)
            ),
            None,
        )
        if local is not None:
            in_scope.append(
                {
                    **local,
                    "status": f.get("status", "open"),
                    "path_gone": not Path(local["file"]).exists(),
                }
            )
            continue
        label_path = str(resolved_file) if resolved_file is not None else file_path
        label = _label_for_other_worktree(label_path)
        out_groups[label] = out_groups.get(label, 0) + 1
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

    findings: list[dict[str, Any]] = []
    all_rows = _iter_findings_raw()
    for finding in all_rows:
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
    in_scope, out_groups = _partition_findings_by_scope(
        findings, scope_root, auto_duplicate_aliases(all_rows)
    )

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
    live_copies = live_auto_duplicate_fps(findings)
    for f in findings:
        status = f.get("status", "open")
        if status not in resolved_states or f.get("fingerprint") in live_copies:
            # open/surfaced — always keep. So is an auto-duplicate folded into
            # a still-open finding: nobody resolved it, and it is its
            # worktree's only record of the code.
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
