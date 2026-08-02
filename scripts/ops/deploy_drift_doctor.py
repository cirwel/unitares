#!/usr/bin/env python3
"""Self-doctor for "the running code is not the merged code" — diagnose, escalate.

WHY THIS EXISTS
---------------
On 2026-07-28 a merged fix (unitares-pi-plugin #8, Steward's missing
``core.agent_state`` write) sat dark in production for a day. The PR was
merged, CI was green, and nothing was wrong with the code — the live checkout
had simply never been pulled and the server never restarted. It was found by a
human asking "merged?", not by any detector.

Nothing in the fleet watches for this. ``unitares_doctor.py`` has 23 checks
covering migrations, column drift, health, PID files and LaunchAgents, and none
of them ask whether the process is running the code that was merged.

WHAT IT DETECTS
---------------
Two distinct ways the running code diverges from the intended code:

  A. BEHIND-ORIGIN — merged commits the checkout has not pulled. Only
     meaningful for live-from-checkout surfaces, where merge is intended to be
     deploy (the plugin is an editable install; the governance plugin runs
     live from its checkout). NOT applied to the pinned deploy worktree, where
     lagging origin is deliberate ("NO deploy everything").

  B. RESTART-PENDING — the checkout has commits newer than the running
     process start time. The bytes are on disk but the interpreter still holds
     the old module. This is the state a `git pull` alone leaves you in, and it
     applies to every surface including the pinned deploy worktree.

DESIGN CONTRACT (see memory: self-healing-doctor-layer)
------------------------------------------------------
1. Diagnose FIRST — the two conditions above are distinguished, never merged
   into a generic "out of date", because their fixes differ (pull vs restart).
2. Bounded heal — THIS DOCTOR NEVER HEALS. Pulling a live checkout and
   restarting governance-mcp is a production deploy on Lumen's check-in path;
   that is squarely in the never-automate category. Diagnose and escalate only.
3. Verify after — n/a (no heal), but drift clearing IS verified, and clearing
   is what closes the finding.
4. Escalate honestly — one fingerprinted finding per (surface, condition),
   cooldown-limited so a surface left un-deployed for a week does not spam.

LIFECYCLE
---------
Findings are posted to /api/findings with a stable fingerprint so they dedupe,
surface in Discord/dashboard, and can be adjudicated like any resident finding.
When drift clears, the doctor emits a *resolution* — closing the loop rather
than leaving a stale open finding, and (once this doctor has a baselined
governance identity) producing an ``external_signal`` outcome_event, which is
the scarce exogenous label the EISV residual test is starved of.

See ``--help`` for the identity caveat.
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

try:  # pragma: no cover - exercised by the very failure it guards against
    from agents.common.findings import (
        DEDUPED, DELIVERED, FAILED, REACHED_GOVERNANCE,
    )
except Exception:
    # The escalation module is exactly what goes missing when this doctor is
    # run by an interpreter without the project's deps — which is how it stayed
    # silent for its whole life. Import defensively and keep the literals in
    # sync with agents/common/findings.py, so a missing escalation path still
    # reports FAILED loudly instead of exploding at import time.
    DELIVERED, DEDUPED, FAILED = "delivered", "deduped", "failed"
    REACHED_GOVERNANCE = frozenset({DELIVERED, DEDUPED})

STATE_FILE = os.path.expanduser(
    os.environ.get("DEPLOY_DRIFT_DOCTOR_STATE", "~/.unitares/deploy-drift-doctor.state.json")
)
FINDING_KIND = "deploy_drift_finding"
# A surface left un-deployed deliberately (waiting on a review, staged rollout)
# must not re-alert every cycle. One finding per surface+condition per window.
COOLDOWN_SECONDS = int(os.environ.get("DEPLOY_DRIFT_COOLDOWN_SECONDS", str(12 * 3600)))
# Extensions the interpreter actually loads. A pull that changes only these is
# the one that leaves a stale module in memory; anything else (markdown, skills
# content, CI config) is live on disk the moment it lands.
RUNTIME_SUFFIXES = (".py", ".pyi", ".so", ".pth")


class Surface:
    """A checkout whose code is expected to be running somewhere.

    ``check_behind`` is False for the pinned deploy worktree: it lags origin by
    design, so behind-ness there is a deliberate staging decision, not drift.
    Restart-pending still applies — that one is never intentional.
    """

    def __init__(self, name: str, path: str, branch: str, launchd_label: Optional[str],
                 check_behind: bool = True):
        self.name = name
        self.path = os.path.expanduser(path)
        self.branch = branch
        self.launchd_label = launchd_label
        self.check_behind = check_behind


DEFAULT_SURFACES: List[Surface] = [
    # Editable install imported live by governance-mcp — merge IS deploy here.
    Surface("unitares-pi-plugin", "~/projects/unitares-pi-plugin", "main",
            "com.unitares.governance-mcp"),
    # Adapter bundle, also live from its checkout.
    Surface("unitares-governance-plugin", "~/projects/unitares-governance-plugin", "master",
            "com.unitares.governance-mcp"),
    # Pinned deploy worktree: lagging origin is deliberate, restart lag is not.
    Surface("unitares-deploy", "~/projects/unitares-deploy", "master",
            "com.unitares.governance-mcp", check_behind=False),
]


# ---------------------------------------------------------------------------
# IO seam — injected so the diagnosis logic is testable without a live fleet.
# ---------------------------------------------------------------------------

def io_git(path: str, *args: str) -> str:
    try:
        out = subprocess.run(["git", "-C", path, *args], capture_output=True,
                             text=True, timeout=30)
        return out.stdout.strip() if out.returncode == 0 else ""
    except (subprocess.SubprocessError, OSError):
        return ""


def io_fetch(path: str) -> None:
    try:
        subprocess.run(["git", "-C", path, "fetch", "origin", "--quiet"],
                       capture_output=True, timeout=60)
    except (subprocess.SubprocessError, OSError):
        pass


def io_process_start_epoch(label: str) -> Optional[float]:
    """Unix start time of the launchd-managed process, or None if not running."""
    try:
        listing = subprocess.run(["launchctl", "list"], capture_output=True,
                                 text=True, timeout=15).stdout
        pid = None
        for line in listing.splitlines():
            parts = line.split()
            if len(parts) >= 3 and parts[2] == label and parts[0].isdigit():
                pid = parts[0]
                break
        if pid is None:
            return None
        # `ps -o lstart=` is human text; -o etime= is elapsed, easier to parse
        # into an absolute start without locale dependence.
        etime = subprocess.run(["ps", "-o", "etime=", "-p", pid], capture_output=True,
                               text=True, timeout=15).stdout.strip()
        if not etime:
            return None
        return time.time() - _parse_etime(etime)
    except (subprocess.SubprocessError, OSError, ValueError):
        return None


def _parse_etime(etime: str) -> float:
    """Parse ps elapsed time ([[dd-]hh:]mm:ss) into seconds."""
    days = 0
    if "-" in etime:
        d, etime = etime.split("-", 1)
        days = int(d)
    bits = [int(x) for x in etime.split(":")]
    while len(bits) < 3:
        bits.insert(0, 0)
    h, m, s = bits[-3], bits[-2], bits[-1]
    return days * 86400 + h * 3600 + m * 60 + s


def io_post_finding(payload: Dict[str, Any]) -> str:
    """Escalate, returning DELIVERED / DEDUPED / FAILED.

    Formerly this swallowed every exception with a bare ``pass`` and the
    comment "escalation is best-effort; the log line always lands." The log
    line does land — in a file nobody reads — so an import error here was
    indistinguishable from a healthy hourly run for this doctor's entire
    life. Best-effort is fine; silent best-effort is not. The caller needs the
    outcome to decide whether it may record having alerted.
    """
    try:
        from agents.common.findings import post_finding_result
        return post_finding_result(**payload)
    except Exception as exc:
        log(f"  post_finding unavailable ({type(exc).__name__}: {exc}) — "
            f"escalation is DOWN, not quiet")
        return FAILED


def io_post_outcome(args: Dict[str, Any]) -> None:
    """Emit the resolution outcome_event. No-op without a baselined identity."""
    try:
        import httpx
        url = os.environ.get("UNITARES_HTTP_BASE", "http://localhost:8767")
        httpx.post(f"{url}/v1/tools/call",
                   json={"name": "outcome_event", "arguments": args}, timeout=5.0)
    except Exception:
        pass


DEFAULT_IO: Dict[str, Callable[..., Any]] = {
    "git": io_git,
    "fetch": io_fetch,
    "process_start_epoch": io_process_start_epoch,
    "post_finding": io_post_finding,
    "post_outcome": io_post_outcome,
}


def log(msg: str) -> None:
    print(f"[deploy-drift-doctor] {msg}", flush=True)


# ---------------------------------------------------------------------------
# Diagnosis
# ---------------------------------------------------------------------------

class Diagnosis:
    def __init__(self, surface: str, condition: str, detail: str, behind: int = 0):
        self.surface = surface
        self.condition = condition  # "behind_origin" | "restart_pending"
        self.detail = detail
        self.behind = behind

    @property
    def fingerprint_parts(self) -> List[str]:
        return ["deploy-drift", self.surface, self.condition]


def diagnose(surface: Surface, io: Dict[str, Callable[..., Any]]) -> List[Diagnosis]:
    """Return every drift condition true for this surface (may be empty)."""
    found: List[Diagnosis] = []
    if not os.path.isdir(surface.path):
        return found

    io["fetch"](surface.path)

    if surface.check_behind:
        counts = io["git"](surface.path, "rev-list", "--left-right", "--count",
                           f"HEAD...origin/{surface.branch}")
        if counts:
            try:
                _ahead, behind = (int(x) for x in counts.split())
            except ValueError:
                behind = 0
            if behind > 0:
                subjects = io["git"](surface.path, "log", "--oneline",
                                     f"HEAD..origin/{surface.branch}") or ""
                first = subjects.splitlines()[:3]
                found.append(Diagnosis(
                    surface.name, "behind_origin",
                    f"{behind} merged commit(s) not pulled into the live checkout: "
                    + "; ".join(first),
                    behind=behind,
                ))

    # Restart-pending: commits on disk that the running process predates.
    #
    # Only commits touching code the interpreter actually loads count. A pull
    # carrying markdown, skills content or CI config changes nothing in memory,
    # so flagging it would tell the operator to restart a production server for
    # a docs change — the wrong-class advice contract item 1 exists to prevent.
    # Caught by dogfooding: the 2026-07-28 governance-plugin pull was 9 files,
    # zero Python, and an earlier draft raised restart_pending on it.
    if surface.launchd_label:
        started = io["process_start_epoch"](surface.launchd_label)
        head_ts = io["git"](surface.path, "log", "-1", "--format=%ct")
        if started and head_ts:
            try:
                head_epoch = float(head_ts)
            except ValueError:
                head_epoch = 0.0
            if head_epoch > started:
                changed = io["git"](surface.path, "log", f"--since=@{int(started)}",
                                    "--name-only", "--format=") or ""
                runtime = sorted({f for f in changed.split()
                                  if f.endswith(RUNTIME_SUFFIXES)})
                if runtime:
                    age_min = int((head_epoch - started) / 60)
                    found.append(Diagnosis(
                        surface.name, "restart_pending",
                        f"checkout HEAD is {age_min}m newer than the running "
                        f"{surface.launchd_label} process and touches loaded code "
                        f"({', '.join(runtime[:3])}) — bytes on disk, old module in memory",
                    ))
    return found


# ---------------------------------------------------------------------------
# Doctor
# ---------------------------------------------------------------------------

class Doctor:
    def __init__(self, surfaces: Optional[List[Surface]] = None,
                 io: Optional[Dict[str, Callable[..., Any]]] = None,
                 dry_run: bool = False):
        self.surfaces = surfaces if surfaces is not None else DEFAULT_SURFACES
        self.io = {**DEFAULT_IO, **(io or {})}
        self.dry_run = dry_run
        self.state = self._load_state()

    def _load_state(self) -> Dict[str, Any]:
        try:
            return json.loads(Path(STATE_FILE).read_text())
        except (OSError, ValueError):
            return {}

    def _save_state(self) -> None:
        if self.dry_run:
            return
        try:
            Path(STATE_FILE).parent.mkdir(parents=True, exist_ok=True)
            Path(STATE_FILE).write_text(json.dumps(self.state, indent=2))
        except OSError:
            pass

    def _fingerprint(self, d: Diagnosis) -> str:
        try:
            from agents.common.findings import compute_fingerprint
            return compute_fingerprint(d.fingerprint_parts)
        except Exception:
            import hashlib
            return hashlib.sha256("|".join(d.fingerprint_parts).encode()).hexdigest()[:16]

    def run(self) -> int:
        open_findings: Dict[str, Any] = self.state.setdefault("open", {})
        seen: set[str] = set()
        drift_count = 0

        for surface in self.surfaces:
            for d in diagnose(surface, self.io):
                drift_count += 1
                fp = self._fingerprint(d)
                seen.add(fp)
                log(f"DRIFT {d.surface} [{d.condition}] {d.detail}")
                self._escalate(d, fp, open_findings)

        # Resolution: anything previously open that is no longer drifting has
        # been deployed. Close it — an open finding nobody closes is how a
        # detector decays into noise.
        for fp in list(open_findings):
            if fp in seen:
                continue
            rec = open_findings.pop(fp)
            log(f"RESOLVED {rec.get('surface')} [{rec.get('condition')}] — deployed")
            self._resolve(fp, rec)

        self._save_state()
        if drift_count == 0:
            log("all surfaces running merged code")
        return 0

    def _escalate(self, d: Diagnosis, fp: str, open_findings: Dict[str, Any]) -> None:
        now = time.time()
        prev = open_findings.get(fp)
        if prev and (now - prev.get("last_alert", 0)) < COOLDOWN_SECONDS:
            log(f"  finding suppressed (cooldown active for {fp})")
            return
        if self.dry_run:
            log(f"  dry-run: would post finding {fp}")
            return
        # Severity reflects consequence, not novelty: a live-from-checkout
        # surface behind origin means a merged fix is not actually running.
        severity = "warning" if d.condition == "restart_pending" else "critical"
        outcome = self.io["post_finding"]({
            "event_type": FINDING_KIND,
            "severity": severity,
            "message": (f"{d.surface}: {d.detail}. Deploy is a human action — "
                        f"pull and restart deliberately; this doctor never heals."),
            "fingerprint": fp,
            "agent_id": "deploy-drift-doctor",
            "agent_name": "deploy-drift-doctor",
        })
        # Record the alert ONLY if governance actually holds the finding.
        # Writing last_alert after a failed post manufactures a delivery that
        # never happened, and the cooldown above then suppresses the retry that
        # would have fixed it — the finding is buried by its own bookkeeping.
        # This doctor spent its entire life in exactly that state: escalation
        # raised ModuleNotFoundError every cycle, the exception was swallowed,
        # and last_alert was written anyway (verified 2026-08-01, zero
        # deploy_drift rows in audit.events, ever).
        if outcome not in REACHED_GOVERNANCE:
            log(f"  ESCALATION FAILED for {fp} — finding NOT recorded, will retry next cycle")
            return
        open_findings[fp] = {
            "surface": d.surface, "condition": d.condition,
            "first_seen": (prev or {}).get("first_seen", now), "last_alert": now,
        }

    def _resolve(self, fp: str, rec: Dict[str, Any]) -> None:
        """Close a finding whose drift has cleared.

        The finding was real (the surface WAS behind) and got actioned, so it
        resolves as ``confirmed`` — a correct call, not a false positive.

        IDENTITY CAVEAT: outcome_event snapshots EISV by ``agent_id``, so this
        only becomes a usable exogenous label once this doctor onboards a
        baselined governance identity. Until DEPLOY_DRIFT_DOCTOR_UUID is set,
        the resolution is logged and the finding closed locally, but no
        outcome_event is emitted — an outcome row with no EISV would add noise
        to the label-breadth problem rather than helping it.
        """
        uuid = os.environ.get("DEPLOY_DRIFT_DOCTOR_UUID", "").strip()
        if not uuid:
            log("  (no DEPLOY_DRIFT_DOCTOR_UUID — closed locally, no outcome_event)")
            return
        if self.dry_run:
            log(f"  dry-run: would emit resolution outcome for {fp}")
            return
        try:
            from agents.common.resolution_outcome import build_resolution_outcome_args
            args = build_resolution_outcome_args(
                finding_kind=FINDING_KIND, status="confirmed",
                fingerprint=fp, agent_uuid=uuid,
                reason=f"drift cleared on {rec.get('surface')}",
            )
        except Exception:
            return
        self.io["post_outcome"](args)


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Detect running-code vs merged-code drift. Diagnoses and "
                    "escalates; never pulls, never restarts.",
        epilog="Identity: set DEPLOY_DRIFT_DOCTOR_UUID to a baselined governance "
               "identity for resolutions to emit external_signal outcome_events.",
    )
    parser.add_argument("--dry-run", action="store_true",
                        help="diagnose and print; post no findings, write no state")
    args = parser.parse_args()
    return Doctor(dry_run=args.dry_run).run()


if __name__ == "__main__":
    raise SystemExit(main())
