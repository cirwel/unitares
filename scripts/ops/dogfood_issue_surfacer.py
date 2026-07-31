#!/usr/bin/env python3
"""Actuate the ``issue_surface`` route on dogfood friction findings.

WHY THIS EXISTS
---------------
``agents/common/dogfood_friction.py`` emits a rich, well-evidenced envelope and
tags it with route hints (``issue_surface``, ``kg_note``) — but says plainly
that it "does not perform those downstream actions". Nothing did.

As of 2026-07-29 there were 19 friction findings spanning 2026-06-17..07-28,
and the only dogfood-derived issues (#710, #945, #752) were all closed and all
from June. Meanwhile ``recurrence_count`` climbed on repeat surfaces — that
counter was measuring how long nobody looked, not how bad the bug was.

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
import json
import os
import subprocess
import sys
from typing import Any, Callable, Dict, Iterable, List, Optional

DEFAULT_REPO = os.environ.get("DOGFOOD_ISSUE_REPO", "CIRWEL/unitares")
DEFAULT_BASE = os.environ.get("UNITARES_HTTP_BASE", "http://127.0.0.1:8767")
EVENT_TYPE = "dogfood_friction_finding"
ROUTE = "issue_surface"
# Machine-readable markers so dedup survives humans editing the issue body.
FP_MARKER = "<!-- dogfood-fingerprint: {} -->"
CT_MARKER = "<!-- dogfood-change-token: {} -->"

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


def io_fetch_findings(base: str, limit: int) -> List[Dict[str, Any]]:
    """Read full friction envelopes from the durable store (see module docstring)."""
    sql = (
        "SELECT payload FROM audit.events "
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
    findings: List[Dict[str, Any]] = []
    for line in out.stdout.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            findings.append(json.loads(line))
        except json.JSONDecodeError:
            continue  # a malformed row must not sink the whole run
    return findings


def io_gh(args: List[str]) -> str:
    out = subprocess.run(["gh", *args], capture_output=True, text=True, timeout=60)
    if out.returncode != 0:
        raise RuntimeError(out.stderr.strip()[:300])
    return out.stdout.strip()


DEFAULT_IO: Dict[str, Callable[..., Any]] = {
    "fetch_findings": io_fetch_findings,
    "gh": io_gh,
}


def log(msg: str) -> None:
    print(f"[dogfood-surfacer] {msg}", flush=True)


# --------------------------------------------------------------------------
# Rendering
# --------------------------------------------------------------------------

def render_title(f: Dict[str, Any]) -> str:
    surface = (f.get("surface") or "unknown surface").strip()
    # Issue titles are scanned in lists; keep the surface readable and bounded.
    if len(surface) > 88:
        surface = surface[:85].rstrip() + "..."
    return f"dogfood friction: {surface}"


def render_body(f: Dict[str, Any]) -> str:
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
    return body


# --------------------------------------------------------------------------
# Surfacer
# --------------------------------------------------------------------------

class Surfacer:
    def __init__(
        self,
        repo: str = DEFAULT_REPO,
        base: str = DEFAULT_BASE,
        io: Optional[Dict[str, Callable[..., Any]]] = None,
        apply: bool = False,
        limit: int = 50,
        fingerprints: Optional[Iterable[str]] = None,
    ):
        self.repo = repo
        self.base = base
        self.io = {**DEFAULT_IO, **(io or {})}
        self.apply = apply
        self.limit = limit
        self.fingerprints = {
            str(fingerprint).strip()
            for fingerprint in (fingerprints or ())
            if str(fingerprint).strip()
        }

    def _existing(self) -> Dict[str, Dict[str, Any]]:
        """Map fingerprint -> issue for already-surfaced findings.

        Searches open AND closed: a closed issue means a human judged it, so
        re-filing it would relitigate a settled call.
        """
        raw = self.io["gh"]([
            "issue", "list", "-R", self.repo, "--state", "all", "--limit", "200",
            "--search", "dogfood-fingerprint in:body",
            "--json", "number,body,state,title",
        ])
        try:
            issues = json.loads(raw) if raw else []
        except json.JSONDecodeError:
            return {}
        out: Dict[str, Dict[str, Any]] = {}
        for issue in issues:
            body = issue.get("body") or ""
            for line in body.splitlines():
                line = line.strip()
                if line.startswith("<!-- dogfood-fingerprint:"):
                    fp = line.split(":", 1)[1].replace("-->", "").strip()
                    if fp:
                        out[fp] = issue
                    break
        return out

    def run(self) -> int:
        findings = self.io["fetch_findings"](self.base, self.limit)
        routed = [f for f in findings if ROUTE in (f.get("routes") or [])]
        log(f"{len(findings)} finding(s) fetched, {len(routed)} routed to {ROUTE}")
        if self.fingerprints:
            routed = [
                finding
                for finding in routed
                if finding.get("fingerprint") in self.fingerprints
            ]
            log(f"{len(routed)} matched explicit fingerprint selection")
        if not routed:
            return 0

        existing = self._existing()
        log(f"{len(existing)} already surfaced")

        created = updated = skipped = 0
        for f in routed:
            fp = f.get("fingerprint") or ""
            if not fp:
                skipped += 1
                continue
            prior = existing.get(fp)
            if prior is None:
                created += 1
                self._create(f)
            elif CT_MARKER.format(f.get("change_token", "")) not in (prior.get("body") or ""):
                updated += 1
                self._comment(prior, f)
            else:
                skipped += 1

        verb = "created" if self.apply else "would create"
        log(f"{verb}={created} updated={updated} unchanged={skipped}")
        if not self.apply:
            log("dry-run — re-run with --apply to file them")
        return 0

    def _create(self, f: Dict[str, Any]) -> None:
        title = render_title(f)
        if not self.apply:
            log(f"  NEW  {title}")
            return
        labels = ["dogfood"]
        sev = SEVERITY_LABELS.get(str(f.get("severity", "")).lower())
        if sev:
            labels.append(sev)
        args = ["issue", "create", "-R", self.repo, "--title", title,
                "--body", render_body(f)]
        for label in labels:
            args += ["--label", label]
        try:
            log(f"  created: {self.io['gh'](args)}")
        except RuntimeError as exc:
            # A missing label must not lose the finding — retry unlabelled.
            log(f"  label rejected ({exc}); retrying without labels")
            log(f"  created: {self.io['gh'](args[:7])}")

    def _comment(self, issue: Dict[str, Any], f: Dict[str, Any]) -> None:
        num = issue.get("number")
        if not self.apply:
            log(f"  UPD  #{num} recurred (change_token moved) — {render_title(f)}")
            return
        note = (
            f"Probe hit this again (recurrence_count="
            f"{f.get('recurrence_count', '?')}).\n\n"
            f"**Observed now:**\n\n{(f.get('observed') or '').strip()}\n\n"
            + CT_MARKER.format(f.get("change_token", ""))
        )
        self.io["gh"](["issue", "comment", str(num), "-R", self.repo, "--body", note])
        log(f"  commented on #{num}")


def main() -> int:
    p = argparse.ArgumentParser(
        description="File dogfood friction findings as deduped GitHub issues.",
        epilog="Dry-run by default: filing issues is outward-facing and bulk, "
               "so it requires --apply.",
    )
    p.add_argument("--apply", action="store_true", help="actually create/comment")
    p.add_argument("--repo", default=DEFAULT_REPO)
    p.add_argument("--base", default=DEFAULT_BASE)
    p.add_argument("--limit", type=int, default=50)
    p.add_argument(
        "--fingerprint",
        action="append",
        default=[],
        help="limit the run to an explicitly reviewed fingerprint; repeatable",
    )
    a = p.parse_args()
    try:
        return Surfacer(
            repo=a.repo,
            base=a.base,
            apply=a.apply,
            limit=a.limit,
            fingerprints=a.fingerprint,
        ).run()
    except Exception as exc:  # noqa: BLE001 - operator-facing tool
        print(f"[dogfood-surfacer] failed: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
