#!/usr/bin/env python3
"""Actuate the ``issue_surface`` route on dogfood friction findings.

WHY THIS EXISTS
---------------
``agents/common/dogfood_friction.py`` emits a rich, well-evidenced envelope and
tags it with route hints (``issue_surface``, ``kg_note``) — but says plainly
that it "does not perform those downstream actions". Nothing did.

The durable store had accumulated recurring friction findings while only older,
human-filed dogfood issues existed. Meanwhile ``recurrence_count`` climbed on
repeat surfaces — that counter was measuring how long nobody looked, not how
bad the bug was.

The probe was doing its job well and reporting into an empty room. This closes
that half of the loop: findings become tracked issues, deduped so a recurring
friction updates its issue instead of spawning a new one.

WHAT IT DOES NOT DO
-------------------
It does not close issues, and it does not judge whether a friction is real —
that stays human. It also does not touch the ``kg_note`` route; KG sediment is
a separate decision about durable memory, not issue tracking.

READ PATH — why Postgres and not /api/events
--------------------------------------------
The obvious source would be ``GET /api/events?type=dogfood_friction_finding``.
It is unusable here, for two independent reasons found while building this:

1. It serves an **in-memory ring buffer**, not the durable store. Immediately
   after a governance-mcp restart it returned 1 finding where ``audit.events``
   held 19 — so it can never backfill, and silently under-reports after any
   restart.
2. It **flattens the payload away**. The response carries only
   ``{type, severity, message, agent_id, agent_name, event_id, timestamp}``:
   no ``routes``, ``fingerprint``, ``change_token``, ``expected``, ``observed``,
   ``proposed_action`` or ``repro_command``. Every field an issue needs, and
   the dedup key, are gone.

So this reads ``audit.events`` directly. Related: that same endpoint silently
ignores the ``event_type`` alias and degrades to an unfiltered success — the
probe's own finding 9028fa1e (2026-07-28). Three defects on one surface.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
import unicodedata
from collections import defaultdict
from collections.abc import Callable, Iterable, Mapping, Sequence
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

DEFAULT_REPO = os.environ.get("DOGFOOD_ISSUE_REPO", "CIRWEL/unitares")
DEFAULT_BASE = os.environ.get("UNITARES_HTTP_BASE", "http://127.0.0.1:8767")
EVENT_TYPE = "dogfood_friction_finding"
ROUTE = "issue_surface"
# Machine-readable markers so dedup survives humans editing the issue body.
FP_MARKER = "<!-- dogfood-fingerprint: {} -->"
CT_MARKER = "<!-- dogfood-change-token: {} -->"
SEM_MARKER = "<!-- dogfood-semantic-key: {} -->"
SEMANTIC_FIELDS = ("surface", "attempted_action", "expected", "observed")
STABLE_SEMANTIC_FIELDS = ("surface", "attempted_action", "expected")
CHECKPOINT_SCHEMA_VERSION = 1
DEFAULT_CHECKPOINT_PATH = Path(
    os.environ.get(
        "DOGFOOD_SURFACER_CHECKPOINT",
        "~/.local/state/unitares/dogfood-issue-surfacer.json",
    )
).expanduser()

SEVERITY_LABELS = {
    "critical": "priority:high",
    "high": "priority:high",
    "medium": "priority:medium",
    "low": "priority:low",
}


# --------------------------------------------------------------------------
# IO seam — injected so dedup/rendering logic is testable without gh or a server
# --------------------------------------------------------------------------

def _resolve_psql() -> str:
    for p in ("/opt/homebrew/opt/postgresql@17/bin/psql", "/opt/homebrew/bin/psql",
              "/usr/local/bin/psql"):
        if os.path.isfile(p) and os.access(p, os.X_OK):
            return p
    from shutil import which

    found = which("psql")
    if not found:
        raise RuntimeError("psql not found; set PATH or install postgresql client")
    return found


Finding = dict[str, Any]
Issue = dict[str, Any]
IOMap = dict[str, Callable[..., Any]]


class UnsafeApplyError(RuntimeError):
    """Raised before IO when an apply run lacks an explicit safety boundary."""


def _parse_aware_timestamp(value: Any, *, label: str) -> datetime:
    """Parse an ISO-8601 timestamp and require an explicit UTC offset."""
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, str) and value.strip():
        text = value.strip()
        if text.endswith(("Z", "z")):
            text = text[:-1] + "+00:00"
        try:
            parsed = datetime.fromisoformat(text)
        except ValueError as exc:
            raise ValueError(f"{label} must be an ISO-8601 timestamp") from exc
    else:
        raise ValueError(f"{label} must be an ISO-8601 timestamp")
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError(f"{label} requires an explicit UTC offset")
    return parsed.astimezone(timezone.utc)


def _format_utc(value: datetime) -> str:
    """Render one aware timestamp in canonical UTC form."""

    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _reject_symlink_components(path: Path) -> None:
    """Reject symbolic links in all existing checkpoint path components."""

    absolute = Path(os.path.abspath(path.expanduser()))
    for component in reversed((absolute, *absolute.parents)):
        if component.is_symlink():
            raise UnsafeApplyError(
                f"checkpoint path contains a symbolic link: {component}"
            )


def _read_checkpoint(path: Path, repo: str) -> datetime | None:
    """Read and validate a durable forward-only checkpoint, if present."""

    _reject_symlink_components(path)
    if not path.exists():
        return None
    if not path.is_file():
        raise UnsafeApplyError("checkpoint path is not a regular file")
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise UnsafeApplyError("checkpoint file is unreadable or corrupt") from exc
    expected_keys = {"schema_version", "repo", "event_type", "event_timestamp"}
    if not isinstance(payload, dict) or set(payload) != expected_keys:
        raise UnsafeApplyError("checkpoint file has an unexpected schema")
    if (
        payload["schema_version"] != CHECKPOINT_SCHEMA_VERSION
        or payload["repo"] != repo
        or payload["event_type"] != EVENT_TYPE
    ):
        raise UnsafeApplyError("checkpoint file does not match this surfacer scope")
    try:
        return _parse_aware_timestamp(
            payload["event_timestamp"],
            label="checkpoint event timestamp",
        )
    except ValueError as exc:
        raise UnsafeApplyError("checkpoint event timestamp is invalid") from exc


def _write_checkpoint(path: Path, repo: str, boundary: datetime) -> None:
    """Atomically persist a monotonic successful-run checkpoint."""

    _reject_symlink_components(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    _reject_symlink_components(path)
    payload = {
        "schema_version": CHECKPOINT_SCHEMA_VERSION,
        "repo": repo,
        "event_type": EVENT_TYPE,
        "event_timestamp": _format_utc(boundary),
    }
    encoded = json.dumps(payload, indent=2, sort_keys=True) + "\n"
    temporary = path.with_name(f".{path.name}.tmp-{os.getpid()}")
    try:
        with temporary.open("x", encoding="utf-8") as handle:
            handle.write(encoded)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary, 0o600)
        _reject_symlink_components(path)
        os.replace(temporary, path)
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


def _normalize_semantic_text(value: Any) -> str:
    """Normalize casing, Unicode width, and whitespace without guessing meaning."""
    if value is None:
        return ""
    normalized = unicodedata.normalize("NFKC", str(value)).casefold()
    return " ".join(normalized.split())


def semantic_key(finding: Mapping[str, Any]) -> str | None:
    """Return a deterministic structural key, or ``None`` if evidence is incomplete.

    This is deliberately not an AI-similarity judgment. All required evidence
    fields must normalize to non-empty text, and punctuation remains significant
    so the key errs toward false negatives instead of unsafe conflation.
    """
    normalized = {
        field: _normalize_semantic_text(finding.get(field))
        for field in SEMANTIC_FIELDS
    }
    if any(not value for value in normalized.values()):
        return None
    encoded = json.dumps(
        normalized,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return f"v1:{hashlib.sha256(encoded).hexdigest()}"


def _stable_semantic_key(finding: Mapping[str, Any]) -> str | None:
    """Return a v2 key that excludes volatile observed diagnostic text."""

    normalized = {
        field: _normalize_semantic_text(finding.get(field))
        for field in STABLE_SEMANTIC_FIELDS
    }
    if any(not value for value in normalized.values()):
        return None
    encoded = json.dumps(
        normalized,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return f"v2:{hashlib.sha256(encoded).hexdigest()}"


def semantic_keys(finding: Mapping[str, Any]) -> tuple[str, ...]:
    """Return stable-v2 plus legacy-v1 keys for backward-compatible dedup."""

    keys = (_stable_semantic_key(finding), semantic_key(finding))
    return tuple(dict.fromkeys(key for key in keys if key is not None))


def io_fetch_findings(base: str, limit: int) -> list[Finding]:
    """Read full friction envelopes from the durable store (see module docstring)."""
    sql = (
        "SELECT payload || jsonb_build_object('event_timestamp', ts) "
        "FROM audit.events "
        f"WHERE event_type = '{EVENT_TYPE}' ORDER BY ts DESC LIMIT {int(limit)};"
    )
    out = subprocess.run(
        [_resolve_psql(), "-h", os.environ.get("PGHOST", "localhost"),
         "-U", os.environ.get("PGUSER", "postgres"),
         "-d", os.environ.get("PGDATABASE", "governance"), "-tAc", sql],
        capture_output=True, text=True, timeout=30,
    )
    if out.returncode != 0:
        raise RuntimeError(f"psql failed: {out.stderr.strip()[:200]}")
    findings: list[Finding] = []
    for line in out.stdout.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            findings.append(json.loads(line))
        except json.JSONDecodeError as exc:
            raise RuntimeError(
                "durable finding query returned malformed JSON; refusing partial data"
            ) from exc
    return findings


def io_gh(args: list[str]) -> str:
    """Run a GitHub CLI command and return its stripped stdout."""
    out = subprocess.run(["gh", *args], capture_output=True, text=True, timeout=60)
    if out.returncode != 0:
        raise RuntimeError(out.stderr.strip()[:300])
    return out.stdout.strip()


DEFAULT_IO: IOMap = {
    "fetch_findings": io_fetch_findings,
    "gh": io_gh,
}


def log(msg: str) -> None:
    print(f"[dogfood-surfacer] {msg}", flush=True)


# --------------------------------------------------------------------------
# Rendering
# --------------------------------------------------------------------------

def render_title(f: Mapping[str, Any]) -> str:
    """Render a bounded issue title from a finding surface."""
    surface = (f.get("surface") or "unknown surface").strip()
    # Issue titles are scanned in lists; keep the surface readable and bounded.
    if len(surface) > 88:
        surface = surface[:85].rstrip() + "..."
    return f"dogfood friction: {surface}"


def render_body(f: Mapping[str, Any]) -> str:
    """Render human evidence plus exact and structural dedup markers."""
    def block(label: str, key: str) -> str:
        v = (f.get(key) or "").strip()
        return f"### {label}\n\n{v}\n\n" if v else ""

    recur = f.get("recurrence_count")
    header = [
        f"Filed automatically from a `{EVENT_TYPE}` (route `{ROUTE}`).",
        "",
        f"- **severity**: {f.get('severity', 'unknown')}",
        f"- **reproducible**: {f.get('reproducible')}",
    ]
    if recur not in (None, "", 1, "1"):
        header.append(
            f"- **recurrence_count**: {recur} — the probe has hit this more than once"
        )
    if f.get("ambiguous"):
        header.append("- **ambiguous**: the probe was unsure this is a defect")
    if f.get("policy_question"):
        header.append("- **policy_question**: may be intended behaviour, not a bug")

    body = "\n".join(header) + "\n\n"
    body += block("Attempted", "attempted_action")
    body += block("Expected", "expected")
    body += block("Observed", "observed")
    body += block("Proposed action", "proposed_action")
    body += block("Workaround used", "workaround_used")

    repro = (f.get("repro_command") or "").strip()
    if repro:
        body += f"### Repro\n\n```\n{repro}\n```\n\n"
    if f.get("evidence_uri"):
        body += f"Evidence: `{f['evidence_uri']}`\n\n"
    if f.get("fresh_agent_context"):
        body += f"<sub>{f['fresh_agent_context']}</sub>\n\n"

    body += "---\n"
    body += (
        "<sub>Surfaced by `scripts/ops/dogfood_issue_surfacer.py`. The probe "
        "reports; triage stays human — this was not auto-triaged.</sub>\n"
    )
    body += FP_MARKER.format(f.get("fingerprint", "")) + "\n"
    body += CT_MARKER.format(f.get("change_token", "")) + "\n"
    for key in semantic_keys(f):
        body += SEM_MARKER.format(key) + "\n"
    return body


# --------------------------------------------------------------------------
# Surfacer
# --------------------------------------------------------------------------

class ExistingIndex:
    """Unique existing issues indexed by exact and structural markers."""

    def __init__(
        self,
        *,
        issue_count: int,
        by_fingerprint: dict[str, tuple[Issue, ...]],
        by_semantic: dict[str, tuple[Issue, ...]],
    ) -> None:
        """Store immutable-by-convention issue index snapshots."""
        self.issue_count = issue_count
        self.by_fingerprint = by_fingerprint
        self.by_semantic = by_semantic


def _issue_identity(issue: Mapping[str, Any]) -> str:
    """Return a stable identity for merging issue-list search results."""
    number = issue.get("number")
    if number is not None:
        return f"number:{number}"
    return json.dumps(issue, ensure_ascii=False, sort_keys=True, default=str)


def _issue_texts(issue: Mapping[str, Any]) -> Iterable[str]:
    """Yield body and comment text carrying machine-readable markers."""
    body = issue.get("body")
    if isinstance(body, str):
        yield body
    comments = issue.get("comments") or []
    if not isinstance(comments, list):
        return
    for comment in comments:
        if isinstance(comment, Mapping) and isinstance(comment.get("body"), str):
            yield comment["body"]


def _marker_values(issue: Mapping[str, Any], marker_name: str) -> set[str]:
    """Extract all exact HTML-comment marker values from an issue."""
    prefix = f"<!-- {marker_name}:"
    values: set[str] = set()
    for text in _issue_texts(issue):
        for line in text.splitlines():
            stripped = line.strip()
            if stripped.startswith(prefix) and stripped.endswith("-->"):
                value = stripped[len(prefix):-3].strip()
                if value:
                    values.add(value)
    return values


def _issue_contains(issue: Mapping[str, Any], marker: str) -> bool:
    """Return whether an issue body or comment contains an exact marker."""
    return any(marker in text for text in _issue_texts(issue))


def _is_label_rejection(exc: RuntimeError) -> bool:
    """Recognize only GitHub label-resolution errors as safe to retry."""

    text = str(exc).casefold()
    return "label" in text and any(
        phrase in text
        for phrase in (
            "not found",
            "could not resolve",
            "does not exist",
            "invalid label",
        )
    )


def _validated_issue_url(value: Any, repo: str) -> str:
    """Require a canonical issue URL for the configured repository."""

    url = str(value).strip()
    pattern = rf"https://github\.com/{re.escape(repo)}/issues/[1-9][0-9]*"
    if re.fullmatch(pattern, url, flags=re.IGNORECASE) is None:
        raise RuntimeError("GitHub issue creation returned an invalid issue URL")
    return url


def _legacy_issue_evidence(issue: Mapping[str, Any]) -> dict[str, str] | None:
    """Recover structural evidence from pre-marker dogfood issue bodies."""

    title = issue.get("title")
    body = issue.get("body")
    prefix = "dogfood friction:"
    if not isinstance(title, str) or not title.casefold().startswith(prefix):
        return None
    if not isinstance(body, str):
        return None

    def section(label: str) -> str:
        match = re.search(
            rf"(?ims)^###\s+{re.escape(label)}\s*$\n+(.*?)(?=^###\s|^---\s*$|\Z)",
            body,
        )
        return match.group(1).strip() if match else ""

    evidence = {
        "surface": title[len(prefix):].strip(),
        "attempted_action": section("Attempted"),
        "expected": section("Expected"),
        "observed": section("Observed"),
    }
    return evidence if all(evidence.values()) else None


class Surfacer:
    """Surface routed findings under explicit temporal and dedup safety gates."""

    def __init__(
        self,
        repo: str = DEFAULT_REPO,
        base: str = DEFAULT_BASE,
        io: Mapping[str, Callable[..., Any]] | None = None,
        apply: bool = False,
        limit: int = 50,
        fingerprints: Iterable[str] | None = None,
        not_before: str | datetime | None = None,
        checkpoint_path: str | Path | None = None,
    ) -> None:
        """Configure a dry run, reviewed exact apply, or checkpointed bulk apply."""
        self.repo = repo
        self.base = base
        self.io = {**DEFAULT_IO, **(io or {})}
        self.apply = apply
        self.limit = limit
        if self.limit <= 0:
            raise ValueError("limit must be positive")
        self.checkpoint_path = (
            Path(checkpoint_path).expanduser() if checkpoint_path is not None else None
        )
        self.not_before = (
            _parse_aware_timestamp(not_before, label="--not-before")
            if not_before is not None
            else None
        )
        self.fingerprints = {
            str(fingerprint).strip()
            for fingerprint in (fingerprints or ())
            if str(fingerprint).strip()
        }

    def _effective_boundary(self) -> datetime | None:
        """Combine an explicit boundary with durable state monotonically."""

        if self.fingerprints:
            return self.not_before
        stored = (
            _read_checkpoint(self.checkpoint_path, self.repo)
            if self.apply and self.checkpoint_path is not None
            else None
        )
        candidates = [value for value in (self.not_before, stored) if value is not None]
        return max(candidates) if candidates else None

    def _validate_apply_contract(self, boundary: datetime | None) -> None:
        """Reject unsafe actuation before fetching findings or querying GitHub."""
        if self.apply and not self.fingerprints and boundary is None:
            raise UnsafeApplyError(
                "unsafe --apply: provide --not-before for a forward-only bulk run "
                "or --fingerprint for each explicitly reviewed finding; a valid "
                "durable checkpoint also satisfies the bulk boundary"
            )

    def _existing(self) -> ExistingIndex:
        """Index exact and structural markers across open and closed issues.

        Searches open AND closed: a closed issue means a human judged it, so
        re-filing it would relitigate a settled call.
        """
        issues_by_identity: dict[str, Issue] = {}
        search_terms = (
            "dogfood-fingerprint in:body,comments",
            "dogfood-semantic-key in:body,comments",
            '"dogfood friction:" in:title',
        )
        for search_term in search_terms:
            raw = self.io["gh"]([
                "issue", "list", "-R", self.repo, "--state", "all",
                "--limit", "1000", "--search", search_term,
                "--json", "number,body,state,title,comments",
            ])
            try:
                issues = json.loads(raw) if raw else []
            except json.JSONDecodeError as exc:
                raise RuntimeError("could not read existing issue index") from exc
            if not isinstance(issues, list):
                raise RuntimeError("could not read existing issue index")
            if len(issues) >= 1000:
                raise RuntimeError(
                    "existing issue index reached the GitHub search cap; "
                    "refusing a potentially truncated dedup index"
                )
            for issue in issues:
                if isinstance(issue, dict):
                    issues_by_identity[_issue_identity(issue)] = issue

        fingerprints: defaultdict[str, list[Issue]] = defaultdict(list)
        semantics: defaultdict[str, list[Issue]] = defaultdict(list)
        for issue in issues_by_identity.values():
            for fingerprint in _marker_values(issue, "dogfood-fingerprint"):
                fingerprints[fingerprint].append(issue)
            for key in _marker_values(issue, "dogfood-semantic-key"):
                semantics[key].append(issue)
            legacy_evidence = _legacy_issue_evidence(issue)
            if legacy_evidence is not None:
                for key in semantic_keys(legacy_evidence):
                    semantics[key].append(issue)
        return ExistingIndex(
            issue_count=len(issues_by_identity),
            by_fingerprint={key: tuple(value) for key, value in fingerprints.items()},
            by_semantic={key: tuple(value) for key, value in semantics.items()},
        )

    def _after_checkpoint(
        self,
        finding: Mapping[str, Any],
        boundary: datetime,
    ) -> bool:
        """Fail closed unless a finding has a valid timestamp at the boundary."""
        try:
            event_timestamp = _parse_aware_timestamp(
                finding.get("event_timestamp"),
                label="event timestamp",
            )
        except ValueError as exc:
            log(f"  SKIP missing or invalid event timestamp ({exc})")
            return False
        if event_timestamp < boundary:
            log(f"  SKIP historical finding {finding.get('fingerprint') or '<missing>'}")
            return False
        return True

    def _advance_checkpoint(self, findings: Sequence[Finding]) -> None:
        """Advance only after a complete successful forward-only apply run."""

        if not self.apply or self.fingerprints or self.checkpoint_path is None:
            return
        timestamps: list[datetime] = []
        for finding in findings:
            try:
                timestamps.append(
                    _parse_aware_timestamp(
                        finding.get("event_timestamp"),
                        label="event timestamp",
                    )
                )
            except ValueError:
                log("checkpoint unchanged: at least one fetched timestamp was invalid")
                return
        if not timestamps:
            return
        current = _read_checkpoint(self.checkpoint_path, self.repo)
        candidate = max(timestamps)
        if current is not None:
            candidate = max(candidate, current)
        _write_checkpoint(self.checkpoint_path, self.repo, candidate)
        log(f"checkpoint advanced to {_format_utc(candidate)}")

    @staticmethod
    def _collapse_batch(findings: Iterable[Finding]) -> list[Finding]:
        """Choose one deterministic newest representative per connected identity."""

        rows = list(findings)
        parents: dict[str, str] = {}

        def find(token: str) -> str:
            parents.setdefault(token, token)
            while parents[token] != token:
                parents[token] = parents[parents[token]]
                token = parents[token]
            return token

        def union(left: str, right: str) -> None:
            left_root = find(left)
            right_root = find(right)
            if left_root != right_root:
                parents[max(left_root, right_root)] = min(left_root, right_root)

        identity_tokens: list[tuple[str, ...]] = []
        for index, finding in enumerate(rows):
            fingerprint = str(finding.get("fingerprint") or "").strip()
            tokens = tuple(
                token
                for token in (
                    f"fp:{fingerprint}" if fingerprint else "",
                    *(f"semantic:{key}" for key in semantic_keys(finding)),
                )
                if token
            ) or (f"row:{index}",)
            for token in tokens[1:]:
                union(tokens[0], token)
            find(tokens[0])
            identity_tokens.append(tokens)

        groups: defaultdict[str, list[Finding]] = defaultdict(list)
        for finding, tokens in zip(rows, identity_tokens, strict=True):
            groups[find(tokens[0])].append(finding)

        def rank(finding: Finding) -> tuple[float, str, str]:
            try:
                parsed = _parse_aware_timestamp(
                    finding.get("event_timestamp"),
                    label="event timestamp",
                )
                newest_first = -parsed.timestamp()
            except ValueError:
                newest_first = float("inf")
            fingerprint = str(finding.get("fingerprint") or "")
            canonical = json.dumps(
                finding,
                ensure_ascii=False,
                sort_keys=True,
                default=str,
            )
            return newest_first, fingerprint, canonical

        representatives = [min(group, key=rank) for group in groups.values()]
        representatives.sort(key=rank)
        duplicate_count = len(rows) - len(representatives)
        if duplicate_count:
            log(f"  SKIP {duplicate_count} duplicate fetched identity record(s)")
        return representatives

    @staticmethod
    def _matching_issue(
        existing: ExistingIndex,
        fingerprint: str,
        keys: Iterable[str],
    ) -> tuple[Issue | None, bool]:
        """Return one unambiguous prior issue, otherwise flag a conflict."""
        candidates: dict[str, Issue] = {}
        for issue in existing.by_fingerprint.get(fingerprint, ()):
            candidates[_issue_identity(issue)] = issue
        for key in keys:
            for issue in existing.by_semantic.get(key, ()):
                candidates[_issue_identity(issue)] = issue
        if len(candidates) > 1:
            numbers = sorted(str(issue.get("number", "?")) for issue in candidates.values())
            log(
                "  AMBIGUOUS existing dedup markers matched issues "
                + ", ".join(f"#{number}" for number in numbers)
            )
            return None, True
        return next(iter(candidates.values()), None), False

    def run(self) -> int:
        """Execute the selected dry-run or apply mode under fail-closed guards."""
        boundary = self._effective_boundary()
        self._validate_apply_contract(boundary)
        findings = self.io["fetch_findings"](self.base, self.limit)
        if self.apply and not self.fingerprints and len(findings) >= self.limit:
            raise UnsafeApplyError(
                "bulk fetch reached --limit; increase it before apply so checkpoint "
                "advancement cannot skip truncated findings"
            )
        routed = [f for f in findings if ROUTE in (f.get("routes") or [])]
        log(f"{len(findings)} finding(s) fetched, {len(routed)} routed to {ROUTE}")
        if self.fingerprints:
            routed = [
                finding
                for finding in routed
                if finding.get("fingerprint") in self.fingerprints
            ]
            log(f"{len(routed)} matched explicit fingerprint selection")
        if boundary is not None:
            routed = [
                finding
                for finding in routed
                if self._after_checkpoint(finding, boundary)
            ]
            log(f"{len(routed)} remained at or after the explicit checkpoint")
        if not routed:
            self._advance_checkpoint(findings)
            return 0

        existing = self._existing()
        log(f"{existing.issue_count} already surfaced issue(s)")

        created = updated = skipped = 0
        for f in self._collapse_batch(routed):
            fp = str(f.get("fingerprint") or "")
            if not fp:
                log("  SKIP finding without exact fingerprint")
                skipped += 1
                continue
            keys = semantic_keys(f)
            prior, ambiguous = self._matching_issue(existing, fp, keys)
            if ambiguous:
                skipped += 1
                continue
            if prior is None:
                if not keys and fp not in self.fingerprints:
                    log(f"  SKIP {fp}: incomplete structural evidence for semantic marker")
                    skipped += 1
                    continue
                created += 1
                self._create(f)
            elif not _issue_contains(
                prior,
                CT_MARKER.format(f.get("change_token", "")),
            ):
                updated += 1
                self._comment(prior, f)
            else:
                skipped += 1

        verb = "created" if self.apply else "would create"
        log(f"{verb}={created} updated={updated} unchanged={skipped}")
        if not self.apply:
            log("dry-run — re-run with --apply to file them")
        self._advance_checkpoint(findings)
        return 0

    def _create(self, f: Mapping[str, Any]) -> None:
        """Create one issue, or describe it in dry-run mode."""
        title = render_title(f)
        if not self.apply:
            log(f"  NEW  {title}")
            return
        labels = ["dogfood"]
        sev = SEVERITY_LABELS.get(str(f.get("severity", "")).lower())
        if sev:
            labels.append(sev)
        base_args = [
            "issue",
            "create",
            "-R",
            self.repo,
            "--title",
            title,
            "--body",
            render_body(f),
        ]
        args = list(base_args)
        for label in labels:
            args += ["--label", label]
        try:
            url = _validated_issue_url(self.io["gh"](args), self.repo)
        except RuntimeError as exc:
            if not _is_label_rejection(exc):
                raise
            log(f"  label rejected ({exc}); retrying without labels")
            url = _validated_issue_url(self.io["gh"](base_args), self.repo)
        log(f"  created: {url}")

    def _comment(self, issue: Mapping[str, Any], f: Mapping[str, Any]) -> None:
        """Comment on recurrence without changing human-controlled issue state."""
        num = issue.get("number")
        if not self.apply:
            log(f"  UPD  #{num} recurred (change_token moved) — {render_title(f)}")
            return
        note = (
            f"Probe hit this again (recurrence_count="
            f"{f.get('recurrence_count', '?')}).\n\n"
            f"**Observed now:**\n\n{(f.get('observed') or '').strip()}\n\n"
            + FP_MARKER.format(f.get("fingerprint", ""))
            + "\n"
            + CT_MARKER.format(f.get("change_token", ""))
        )
        for key in semantic_keys(f):
            note += "\n" + SEM_MARKER.format(key)
        self.io["gh"](["issue", "comment", str(num), "-R", self.repo, "--body", note])
        log(f"  commented on #{num}")


def main(argv: Sequence[str] | None = None) -> int:
    """Parse command-line arguments and run the surfacer."""
    p = argparse.ArgumentParser(
        description="File dogfood friction findings as deduped GitHub issues.",
        epilog=(
            "Dry-run by default. Bulk --apply also requires an explicit "
            "--not-before boundary or valid durable checkpoint; exact "
            "--fingerprint review is exempt."
        ),
    )
    p.add_argument("--apply", action="store_true", help="actually create/comment")
    p.add_argument("--repo", default=DEFAULT_REPO)
    p.add_argument("--base", default=DEFAULT_BASE)
    p.add_argument("--limit", type=int, default=50)
    p.add_argument(
        "--not-before",
        help=(
            "explicit ISO-8601 checkpoint for bulk --apply; only findings at or "
            "after it are eligible; successful bulk apply advances durable state"
        ),
    )
    p.add_argument(
        "--checkpoint-file",
        type=Path,
        default=DEFAULT_CHECKPOINT_PATH,
        help=f"durable bulk checkpoint (default: {DEFAULT_CHECKPOINT_PATH})",
    )
    p.add_argument(
        "--fingerprint",
        action="append",
        default=[],
        help="limit the run to an explicitly reviewed fingerprint; repeatable",
    )
    a = p.parse_args(argv)
    try:
        return Surfacer(
            repo=a.repo,
            base=a.base,
            apply=a.apply,
            limit=a.limit,
            fingerprints=a.fingerprint,
            not_before=a.not_before,
            checkpoint_path=a.checkpoint_file,
        ).run()
    except Exception as exc:  # noqa: BLE001 - operator-facing tool
        print(f"[dogfood-surfacer] failed: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
