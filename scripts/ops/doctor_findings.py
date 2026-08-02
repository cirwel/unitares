#!/usr/bin/env python3
"""Escalate unitares_doctor's live checks into the governance finding stream.

The doctor's operator-mode checks are good and they run nowhere. No launchd job
invokes it, and CI imports it only to call ``check_dockerfile_pinned_tags`` -- a
static check. So every runtime check in it (``immortal_lease``,
``resident_checkin_stale``, ``signal_degeneracy``, ``finding_producer_live``,
``checkin_stream_live``, ...) fires only when a human remembers to look. That is
the dormant-capability anti-pattern the registry exists to prevent, and it is
self-defeating here specifically: these are detectors for *silent* failure, so
leaving them unwired reproduces the exact condition they were built to break.

Two outages make the cost concrete. Sentinel was governance-dark for 24h from
2026-07-29 while every aggregate signal read healthy; Watcher was dead for a
month behind an unpulled model tag. Checks that would have caught both existed.
Nothing ran them.

This runs the operator checks on a schedule and posts one finding per failing
check, reusing ``agents/common/findings.post_finding`` -- the same fingerprinted,
deduped path the Sentinel/Watcher/deploy-drift producers already use, so these
land where findings already land rather than inventing a surface.

Deliberately a separate script rather than an extension of
``deploy_drift_doctor.py``: that doctor works, is one of the live producers, and
is structured around Surface/Diagnosis for deploy drift specifically. Bolting a
second concern into it would mean refactoring a working detector to add another.
An isolated script costs one launchd entry and cannot break the existing one.

Diagnose only. This never heals, never restarts, never mutates governance state.

Usage:
    python3 scripts/ops/doctor_findings.py            # run + escalate
    python3 scripts/ops/doctor_findings.py --dry-run  # print, post nothing
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import time
from pathlib import Path
from typing import Any, Callable, Dict

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "scripts" / "dev"))
sys.path.insert(0, str(REPO_ROOT))

try:  # pragma: no cover - exercised by the very failure it guards against
    from agents.common.findings import (
        DEDUPED, DELIVERED, FAILED, REACHED_GOVERNANCE,
    )
except Exception:
    # Guarded for the same reason as in deploy_drift_doctor: the escalation
    # module is precisely what is missing when this runs under an interpreter
    # lacking the project's deps. Keep in sync with agents/common/findings.py.
    DELIVERED, DEDUPED, FAILED = "delivered", "deduped", "failed"
    REACHED_GOVERNANCE = frozenset({DELIVERED, DEDUPED})

FINDING_KIND = "doctor_check_finding"
PRODUCER = "doctor-findings"

# Re-alert cooldown. A condition that stays true should not re-notify hourly --
# the finding is already open. Long enough to stay quiet across a working day,
# short enough that a genuinely stuck condition resurfaces.
COOLDOWN_SECONDS = int(os.environ.get("DOCTOR_FINDINGS_COOLDOWN", 6 * 3600))

STATE_FILE = os.path.expanduser(
    os.environ.get("DOCTOR_FINDINGS_STATE", "~/.unitares/doctor-findings.state.json")
)

DB_URL = os.environ.get(
    "GOVERNANCE_DATABASE_URL",
    "postgresql://postgres:postgres@localhost:5432/governance",
)

# Checks owned by a dedicated watchdog already. Escalating them here would
# double-notify for the same condition on a different fingerprint.
SKIP_CHECKS = {
    c.strip() for c in os.environ.get(
        "DOCTOR_FINDINGS_SKIP", "http_listening,http_health,pid_file"
    ).split(",") if c.strip()
}


class FakeBlind:
    """A synthetic FAIL result for the sweep's own blindness.

    Shaped like a doctor CheckResult (name/message/detail + a .status carrying
    .value) so it flows through the same escalation path as a real check
    instead of needing a special case.
    """

    class _S:
        value = "fail"

    def __init__(self, name: str, message: str, detail: str = ""):
        self.name = name
        self.message = message
        self.detail = detail
        self.status = self._S()


def log(msg: str) -> None:
    print(f"[doctor-findings] {msg}", flush=True)


def io_post_finding(payload: Dict[str, Any]) -> str:
    """Best-effort escalation returning DELIVERED / DEDUPED / FAILED.

    Must not raise. "The log line always lands" was the old justification for
    ignoring the outcome; it lands in a file nobody reads, so the caller needs
    the outcome to know whether it may record having alerted.
    """
    try:
        from agents.common.findings import post_finding_result
        return post_finding_result(**payload)
    except Exception as exc:  # noqa: BLE001 - escalation is advisory
        log(f"  post_finding unavailable ({exc.__class__.__name__}: {exc}) — "
            f"escalation is DOWN, not quiet")
        return FAILED


DEFAULT_IO: Dict[str, Callable[..., Any]] = {"post_finding": io_post_finding}


def fingerprint(name: str, status: str) -> str:
    """Stable per (check, status).

    Deliberately NOT keyed on the message: messages carry volatile counts
    ("2 lease(s)...", "308 check-ins..."), so including them would mint a fresh
    fingerprint every time a count moved and re-alert on noise. One open finding
    per failing check is the unit an operator actually acts on.
    """
    return hashlib.sha256(f"{name}:{status}".encode()).hexdigest()[:16]


def severity_for(status: str) -> str:
    return "critical" if status == "fail" else "warning"


class DoctorFindings:
    def __init__(self, io: Dict[str, Callable[..., Any]] | None = None,
                 dry_run: bool = False):
        self.io = {**DEFAULT_IO, **(io or {})}
        self.dry_run = dry_run
        self.state = self._load_state()

    def _load_state(self) -> Dict[str, Any]:
        try:
            return json.loads(Path(STATE_FILE).read_text())
        except Exception:
            return {}

    def _save_state(self) -> None:
        if self.dry_run:
            return
        try:
            Path(STATE_FILE).parent.mkdir(parents=True, exist_ok=True)
            Path(STATE_FILE).write_text(json.dumps(self.state, indent=2))
        except Exception as exc:  # noqa: BLE001
            log(f"  state write failed ({exc.__class__.__name__})")

    def collect(self) -> list[Any]:
        """Run operator-mode checks. A check that raises is reported, not fatal."""
        import unitares_doctor as d

        results = []
        for check in d.build_checks(REPO_ROOT, DB_URL):
            if check.mode != "operator" or check.name in SKIP_CHECKS:
                continue
            try:
                results.append(check.fn())
            except Exception as exc:  # noqa: BLE001
                log(f"  check {check.name} raised {exc.__class__.__name__}")
        return results

    def run(self) -> int:
        import unitares_doctor as d

        open_findings: Dict[str, Any] = self.state.setdefault("open", {})
        seen: set[str] = set()
        bad = 0

        results = self.collect()

        # A blind watchdog must not report health. Nearly every operator check
        # is DB-backed, and _psql_row returns None -- SKIP, not FAIL -- when
        # `psql` is not on PATH. Under launchd's minimal PATH it is not, so
        # without this guard the whole sweep SKIPs and prints "all operator
        # checks pass" forever. That is the precise failure this script exists
        # to catch, so it has to catch it in itself first.
        skipped = [r for r in results if r.status == d.Status.SKIP]
        if results and len(skipped) >= max(2, len(results) // 2):
            names = ", ".join(r.name for r in skipped[:5])
            blind = FakeBlind(
                "doctor_sweep_blind",
                f"{len(skipped)} of {len(results)} operator checks SKIPPED "
                f"({names}) — the sweep cannot see the system it is watching; "
                "a skipped check is not a passing one",
                "usually psql or the DB is unreachable from this environment "
                "(launchd's minimal PATH omits /opt/homebrew/bin)",
            )
            fp = fingerprint(blind.name, "fail")
            seen.add(fp)
            log(f"FAIL {blind.name}: {blind.message}")
            self._escalate(blind, fp, open_findings)
            bad += 1

        for r in results:
            if r.status not in (d.Status.WARN, d.Status.FAIL):
                continue
            bad += 1
            fp = fingerprint(r.name, r.status.value)
            seen.add(fp)
            log(f"{r.status.value.upper()} {r.name}: {r.message}")
            self._escalate(r, fp, open_findings)

        # Close anything that has gone back to PASS. An open finding nobody
        # closes is how a detector decays into noise.
        for fp in list(open_findings):
            if fp in seen:
                continue
            rec = open_findings.pop(fp)
            log(f"RESOLVED {rec.get('check')} — back to pass")

        self._save_state()
        if bad == 0:
            log("all operator checks pass")
        return 0

    def _escalate(self, r: Any, fp: str, open_findings: Dict[str, Any]) -> None:
        now = time.time()
        prev = open_findings.get(fp)
        if prev and (now - prev.get("last_alert", 0)) < COOLDOWN_SECONDS:
            log(f"  finding suppressed (cooldown active for {r.name})")
            return
        if self.dry_run:
            log(f"  dry-run: would post finding {fp}")
            return
        message = r.message
        if getattr(r, "detail", ""):
            message = f"{message} — {r.detail}"
        outcome = self.io["post_finding"]({
            "event_type": FINDING_KIND,
            "severity": severity_for(r.status.value),
            "message": f"{r.name}: {message}",
            "fingerprint": fp,
            "agent_id": PRODUCER,
            "agent_name": PRODUCER,
        })
        # Only claim the alert if governance actually holds it. DEDUPED counts
        # (the finding is on file); FAILED does not. Recording a failed post
        # would leave the cooldown suppressing every retry, so one transient
        # outage would bury the finding until an operator cleared the state by
        # hand — the failure mode being silent is the whole reason this exists.
        if outcome not in REACHED_GOVERNANCE:
            log(f"  ESCALATION FAILED for {r.name} — not recorded, retrying next cycle")
            return
        open_findings[fp] = {
            "check": r.name,
            "status": r.status.value,
            "first_seen": (prev or {}).get("first_seen", now),
            "last_alert": now,
        }


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Run unitares_doctor's operator checks and escalate failures "
                    "as governance findings. Diagnoses only; never heals.",
    )
    parser.add_argument("--dry-run", action="store_true",
                        help="run checks and print; post no findings, write no state")
    args = parser.parse_args()
    return DoctorFindings(dry_run=args.dry_run).run()


if __name__ == "__main__":
    raise SystemExit(main())
