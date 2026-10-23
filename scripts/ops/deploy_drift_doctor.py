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
import importlib.util
import json
import os
import plistlib
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

REPO_ROOT = Path(__file__).resolve().parents[2]
OPS_DIR = Path(__file__).resolve().parent
for _p in (str(REPO_ROOT), str(OPS_DIR)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

try:  # pragma: no cover - exercised by the very failure it guards against
    from agents.common.findings import (
        DEDUPED, DELIVERED, FAILED, REACHED_GOVERNANCE, doctor_layer_agent_id,
    )
except Exception:
    # The escalation module is exactly what goes missing when this doctor is
    # run by an interpreter without the project's deps — which is how it stayed
    # silent for its whole life. Import defensively and keep the literals in
    # sync with agents/common/findings.py, so a missing escalation path still
    # reports FAILED loudly instead of exploding at import time.
    DELIVERED, DEDUPED, FAILED = "delivered", "deduped", "failed"

    def doctor_layer_agent_id(fallback: str) -> str:  # type: ignore[misc]
        """Same contract as the real one: degrade to the caller's slug.

        Reached only when agents.common.findings is unimportable, which is the
        bare-interpreter case this whole block exists for. Attribution is lost
        in that case, exactly as it is today — never the finding.
        """
        return fallback
    REACHED_GOVERNANCE = frozenset({DELIVERED, DEDUPED})

STATE_FILE = os.path.expanduser(
    os.environ.get("DEPLOY_DRIFT_DOCTOR_STATE", "~/.unitares/deploy-drift-doctor.state.json")
)
FINDING_KIND = "deploy_drift_finding"
# A surface left un-deployed deliberately (waiting on a review, staged rollout)
# must not re-alert every cycle. One finding per surface+condition per window.
COOLDOWN_SECONDS = int(os.environ.get("DEPLOY_DRIFT_COOLDOWN_SECONDS", str(12 * 3600)))
# A checkout a few merged commits behind for a few hours is the normal state of
# a manual deploy cadence, not an emergency: it is a warning until it is either
# far behind or has stayed behind for a day. Grading every lag as critical sent
# 20 of 23 findings in one window to the operator's urgent channel, mostly the
# plugin skill mirror trailing by a commit or two (2026-10-19 Discord audit).
BEHIND_CRITICAL_COMMITS = int(os.environ.get("DEPLOY_DRIFT_BEHIND_CRITICAL_COMMITS", "20"))
BEHIND_CRITICAL_AGE_SECONDS = int(
    os.environ.get("DEPLOY_DRIFT_BEHIND_CRITICAL_AGE_SECONDS", str(24 * 3600)))
# Extensions the interpreter actually loads. A pull that changes only these is
# the one that leaves a stale module in memory; anything else (markdown, skills
# content, CI config) is live on disk the moment it lands.
RUNTIME_SUFFIXES = (".py", ".pyi", ".so", ".pth")



def _pyproject_version(pyproject_text: Optional[str]) -> Optional[str]:
    """Pull `version = "..."` from a pyproject blob. Returns None when unreadable.

    Deliberately shallow: only the first top-level `version =` before any
    `[tool.*]` section, so a dependency pin or a tool's own version field cannot
    be mistaken for the project's. Returning None on anything unexpected means a
    parse failure degrades to silence rather than to a false drift alarm — this
    check exists to be believed, so it must not cry wolf.
    """
    if not pyproject_text:
        return None
    for line in pyproject_text.splitlines():
        stripped = line.strip()
        if stripped.startswith("[tool."):
            break
        if stripped.startswith("version") and "=" in stripped:
            value = stripped.split("=", 1)[1].strip()
            return value.strip('"\'') or None
    return None


class Surface:
    """A checkout whose code is expected to be running somewhere.

    ``check_behind`` is False for the pinned deploy worktree: it lags origin by
    design, so behind-ness there is a deliberate staging decision, not drift.
    Restart-pending still applies — that one is never intentional.

    But "lagging is deliberate" stops being true at some distance. On
    2026-08-12 the deploy worktree sat 28 commits and a full minor version
    behind master — `pyproject.toml` said 2.18.0, the running server answered
    2.17.0 — while this doctor printed "all surfaces running merged code" every
    hour, because by its own definition it was. The operator reasonably believed
    the fleet was synced; nothing was watching the one gap that mattered.

    ``check_version`` closes that without reintroducing hourly commit-count
    noise: a pinned checkout may lag by any number of commits, but a lag that
    crosses a declared version is a staging decision someone should have made on
    purpose. It is a much rarer event than a commit, so it can be loud.
    """

    def __init__(self, name: str, path: str, branch: str, launchd_label: Optional[str],
                 check_behind: bool = True, check_version: bool = False):
        self.name = name
        self.path = os.path.expanduser(path)
        self.branch = branch
        self.launchd_label = launchd_label
        self.check_behind = check_behind
        self.check_version = check_version


_BUILTIN_SURFACES: List[Surface] = [
    # Adapter bundle, also live from its checkout.
    Surface("unitares-governance-plugin", "~/projects/unitares-governance-plugin", "master",
            "com.unitares.governance-mcp"),
    # Pinned deploy worktree: lagging origin is deliberate, restart lag is not.
    Surface("unitares-deploy", "~/projects/unitares-deploy", "master",
            "com.unitares.governance-mcp", check_behind=False, check_version=True),
]
DEFAULT_SURFACES: List[Surface] = _BUILTIN_SURFACES


class PreflightSpec:
    """Read-only deploy preflight for a LaunchAgent-backed service (#3020).

    Mirrors ``deploy_lib_require_plist_target`` and the per-service read-only
    checks (such as ``sentinel-plist.py check`` and ``governance-mcp``'s
    ``DB_POSTGRES_URL`` / bearer config check) so a host-configuration change
    that invalidates a deploy preflight surfaces on the doctor schedule instead
    of waiting to block ``deploy-apply.sh`` at the next deploy.
    """

    def __init__(
        self,
        service: str,
        plist: str,
        needles: List[str],
        *,
        require_exists: bool = False,
        allow_env: Optional[str] = None,
        require_env_keys: Optional[List[str]] = None,
        check_governance_bearer: bool = False,
        check_sentinel_plist: bool = False,
        governance_plist: Optional[str] = None,
        uds_socket: Optional[str] = None,
    ):
        self.service = service
        self.plist = os.path.expanduser(plist)
        self.needles = [os.path.expanduser(n) for n in needles]
        self.require_exists = require_exists
        self.allow_env = allow_env
        self.require_env_keys = list(require_env_keys or [])
        self.check_governance_bearer = check_governance_bearer
        self.check_sentinel_plist = check_sentinel_plist
        self.governance_plist = (
            os.path.expanduser(governance_plist) if governance_plist else None
        )
        self.uds_socket = os.path.expanduser(uds_socket) if uds_socket else None


DEFAULT_PREFLIGHTS: List[PreflightSpec] = [
    PreflightSpec(
        "governance-mcp",
        "~/Library/LaunchAgents/com.unitares.governance-mcp.plist",
        ["~/projects/unitares-deploy/src/mcp_server.py"],
        require_exists=True,
        require_env_keys=["DB_POSTGRES_URL"],
        check_governance_bearer=True,
    ),
    PreflightSpec(
        "sentinel-beam",
        "~/Library/LaunchAgents/com.unitares.sentinel-beam.plist",
        ["~/projects/unitares-deploy"],
        allow_env="UNITARES_SENTINEL_ALLOW_DEV",
        check_sentinel_plist=True,
        governance_plist="~/Library/LaunchAgents/com.unitares.governance-mcp.plist",
        uds_socket="~/.unitares/governance.sock",
    ),
    PreflightSpec(
        "gateway-mcp",
        "~/Library/LaunchAgents/com.unitares.gateway-mcp.plist",
        ["~/projects/unitares-deploy/src/gateway_server.py"],
        allow_env="UNITARES_GATEWAY_ALLOW_DEV",
    ),
    PreflightSpec(
        "lease-plane",
        "~/Library/LaunchAgents/com.unitares.lease-plane.plist",
        ["~/projects/unitares-deploy"],
        allow_env="UNITARES_LEASE_PLANE_ALLOW_DEV",
    ),
    PreflightSpec(
        "wave3a-handlers",
        "~/Library/LaunchAgents/com.unitares.wave3a-handlers.plist",
        ["~/projects/unitares-deploy"],
        allow_env="UNITARES_WAVE3A_ALLOW_DEV",
    ),
    PreflightSpec(
        "dialectic-live",
        "~/Library/LaunchAgents/com.unitares.dialectic-live.plist",
        ["~/projects/unitares-deploy"],
        allow_env="UNITARES_DIALECTIC_LIVE_ALLOW_DEV",
    ),
    PreflightSpec(
        "agent-orchestrator",
        "~/Library/LaunchAgents/com.unitares.agent-orchestrator.plist",
        ["~/projects/unitares-orchestrator-deploy/elixir/agent_orchestrator"],
        allow_env="UNITARES_ORCHESTRATOR_ALLOW_DEV",
    ),
    PreflightSpec(
        "discord-bridge",
        "~/Library/LaunchAgents/com.unitares.discord-bridge.plist",
        ["~/projects/unitares-discord-bridge-deploy/.venv/bin/python3"],
        allow_env="BRIDGE_ALLOW_DEV",
    ),
    PreflightSpec(
        "openai-gov-proxy",
        "~/Library/LaunchAgents/com.unitares.openai-governance-proxy.plist",
        [
            "~/projects/unitares-host-adapter-deploy/.venv/bin/python3",
            "~/projects/unitares-host-adapter-deploy/src",
        ],
    ),
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


def io_sentinel_plist_check(
    plist: str, governance_plist: str, uds_socket: str
) -> Optional[str]:
    """Run ``sentinel-plist.py``'s read-only validation, returning refusal text or None."""
    try:
        sentinel_tool = OPS_DIR / "sentinel-plist.py"
        spec = importlib.util.spec_from_file_location("sentinel_plist", sentinel_tool)
        if spec is None or spec.loader is None:
            return None
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        mod.validate_plist(
            plist=Path(plist),
            governance_plist=Path(governance_plist),
            expected_socket=Path(uds_socket),
        )
        return None
    except Exception as exc:
        return str(exc)


DEFAULT_IO: Dict[str, Callable[..., Any]] = {
    "git": io_git,
    "fetch": io_fetch,
    "process_start_epoch": io_process_start_epoch,
    "post_finding": io_post_finding,
    "post_outcome": io_post_outcome,
    "sentinel_plist_check": io_sentinel_plist_check,
}


def log(msg: str) -> None:
    print(f"[deploy-drift-doctor] {msg}", flush=True)


# ---------------------------------------------------------------------------
# Diagnosis
# ---------------------------------------------------------------------------

class Diagnosis:
    def __init__(self, surface: str, condition: str, detail: str, behind: int = 0):
        self.surface = surface
        self.condition = condition  # "branch_mismatch" | "behind_origin" | "restart_pending" | "version_lag" | "preflight_refused"
        self.detail = detail
        self.behind = behind

    @property
    def fingerprint_parts(self) -> List[str]:
        if self.condition == "preflight_refused":
            return ["deploy-drift", self.surface, self.condition, self.detail]
        return ["deploy-drift", self.surface, self.condition]


def finding_severity(d: Diagnosis, first_seen: Optional[float], now: float) -> str:
    """Grade a diagnosis by consequence.

    A pending restart is a warning. Being behind origin is a warning until the
    lag exceeds BEHIND_CRITICAL_COMMITS or the finding has stayed open for
    BEHIND_CRITICAL_AGE_SECONDS; then a merged fix has plausibly been missed and
    it is critical. Every other condition (a checkout on the wrong branch, a
    pinned checkout behind its declared version) is critical at once.
    """
    if d.condition == "restart_pending":
        return "warning"
    if d.condition == "behind_origin":
        aged = first_seen is not None and (now - first_seen) >= BEHIND_CRITICAL_AGE_SECONDS
        return "critical" if (d.behind > BEHIND_CRITICAL_COMMITS or aged) else "warning"
    return "critical"


def diagnose(surface: Surface, io: Dict[str, Callable[..., Any]]) -> List[Diagnosis]:
    """Return every drift condition true for this surface (may be empty)."""
    found: List[Diagnosis] = []
    if not os.path.isdir(surface.path):
        return found

    io["fetch"](surface.path)

    # Which branch is checked out at all. surface.branch was only ever used as
    # the comparison TARGET (origin/<branch>), never as an assertion that HEAD
    # is on it — so a live checkout parked on a feature branch compared cleanly
    # against master and reported nothing unusual. For a live-from-checkout
    # surface that is the more serious condition of the two: commits ahead of
    # the declared branch are unreviewed code already executing.
    #
    # Checked independently of check_behind. The two claims are orthogonal — a
    # pinned deploy worktree lagging origin is deliberate (that is what
    # check_behind=False encodes), but the same worktree sitting on someone
    # else's branch is not.
    on_branch = (io["git"](surface.path, "rev-parse", "--abbrev-ref", "HEAD") or "").strip()
    # Detached HEAD reports the literal string "HEAD"; it is a distinct
    # condition, not a branch whose name happens to differ from the expected one.
    detached = on_branch == "HEAD"
    mismatched = bool(on_branch) and on_branch != surface.branch

    if mismatched:
        ahead = behind = 0
        counts = io["git"](surface.path, "rev-list", "--left-right", "--count",
                           f"HEAD...origin/{surface.branch}")
        if counts:
            try:
                ahead, behind = (int(x) for x in counts.split())
            except ValueError:
                ahead = behind = 0
        where = "detached HEAD" if detached else f"branch '{on_branch}'"
        lead = (f"live checkout is on {where}, not the expected "
                f"'{surface.branch}' ({ahead} ahead, {behind} behind)")
        if ahead:
            subjects = io["git"](surface.path, "log", "--oneline",
                                 f"origin/{surface.branch}..HEAD") or ""
            first = subjects.splitlines()[:3]
            lead += (" — those " + str(ahead) + " commit(s) are running but not on "
                     f"'{surface.branch}': " + "; ".join(first))
        found.append(Diagnosis(
            surface.name, "branch_mismatch",
            lead + ". Do NOT pull: with local commits this is a merge/rebase, "
                   "not a fast-forward. Land them or switch back deliberately.",
            behind=behind,
        ))

    # behind_origin assumes HEAD is on surface.branch, so its "not pulled — go
    # pull" advice is only correct when that holds. Under a mismatch the
    # branch_mismatch diagnosis above already carries both counts, and emitting
    # this one too would restate them with the wrong remedy attached.
    if surface.check_behind and not mismatched:
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

    # Version lag on a deliberately-pinned checkout.
    #
    # Deliberately NOT gated on `check_behind`: this fires precisely for the
    # surface whose commit-lag is suppressed. A pinned worktree may sit any
    # number of commits behind without that meaning anything; a pin that has
    # fallen behind a declared version is a staging decision that should have
    # been made on purpose, and on 2026-08-12 one had not been — 2.17.0 served
    # live while master said 2.18.0, unremarked for eleven hours.
    #
    # Reads the version out of git rather than the working tree so an
    # uncommitted local edit cannot manufacture or mask the signal.
    if surface.check_version and not mismatched:
        local_v = _pyproject_version(io["git"](surface.path, "show", "HEAD:pyproject.toml"))
        origin_v = _pyproject_version(
            io["git"](surface.path, "show", f"origin/{surface.branch}:pyproject.toml")
        )
        if local_v and origin_v and local_v != origin_v:
            found.append(Diagnosis(
                surface.name, "version_lag",
                f"pinned checkout serves {local_v} while origin/{surface.branch} "
                f"declares {origin_v}. Commit-lag on a pinned worktree is "
                "expected and suppressed; a version gap is not. Deploy, or "
                "record why the pin is held back.",
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


def diagnose_preflight(
    spec: PreflightSpec, io: Dict[str, Callable[..., Any]]
) -> List[Diagnosis]:
    """Run one service's read-only deploy preflight (#3020).

    Side-effect free by construction: inspects only the installed LaunchAgent
    plist, any referenced secrets file, and (when ``sentinel-plist.py check``
    needs to verify an external-store governance bearer) the read-only
    ``GET /v1/tools`` UDS endpoint.
    """
    found: List[Diagnosis] = []
    plist_path = Path(spec.plist)
    if not plist_path.is_file():
        if spec.require_exists:
            found.append(
                Diagnosis(
                    spec.service,
                    "preflight_refused",
                    f"{spec.plist} not installed",
                )
            )
        return found

    try:
        raw_text = plist_path.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        found.append(
            Diagnosis(
                spec.service,
                "preflight_refused",
                f"cannot read {spec.plist}: {exc}",
            )
        )
        return found

    allow_dev = bool(spec.allow_env and os.environ.get(spec.allow_env) == "1")
    if not allow_dev:
        for needle in spec.needles:
            if needle not in raw_text:
                found.append(
                    Diagnosis(
                        spec.service,
                        "preflight_refused",
                        f"LaunchAgent plist {spec.plist} does not point at the deploy worktree ({needle})",
                    )
                )

    if spec.require_env_keys or spec.check_governance_bearer:
        try:
            with plist_path.open("rb") as handle:
                payload = plistlib.load(handle)
        except Exception as exc:
            found.append(
                Diagnosis(
                    spec.service,
                    "preflight_refused",
                    f"cannot parse {spec.plist}: {exc}",
                )
            )
            return found
        env = payload.get("EnvironmentVariables") if isinstance(payload, dict) else None
        for key in spec.require_env_keys:
            val = env.get(key) if isinstance(env, dict) else None
            if not isinstance(val, str) or not val.strip():
                found.append(
                    Diagnosis(
                        spec.service,
                        "preflight_refused",
                        f"{spec.plist} has no readable EnvironmentVariables/{key}",
                    )
                )
        if spec.check_governance_bearer:
            try:
                from governance_bearer import PlistConfigError, resolve_plist_bearer

                resolve_plist_bearer(plist_path)
            except PlistConfigError as exc:
                found.append(
                    Diagnosis(spec.service, "preflight_refused", str(exc))
                )

    if spec.check_sentinel_plist:
        checker = io.get("sentinel_plist_check", io_sentinel_plist_check)
        gov_plist = spec.governance_plist or os.path.expanduser(
            "~/Library/LaunchAgents/com.unitares.governance-mcp.plist"
        )
        uds = spec.uds_socket or os.path.expanduser("~/.unitares/governance.sock")
        refusal = checker(spec.plist, gov_plist, uds)
        if refusal:
            found.append(
                Diagnosis(spec.service, "preflight_refused", refusal)
            )

    return found


def diagnose_preflights(
    specs: List[PreflightSpec], io: Dict[str, Callable[..., Any]]
) -> List[Diagnosis]:
    """Run all configured read-only service deploy preflights."""
    found: List[Diagnosis] = []
    for spec in specs:
        found.extend(diagnose_preflight(spec, io))
    return found


# ---------------------------------------------------------------------------
# Doctor
# ---------------------------------------------------------------------------

class Doctor:
    def __init__(self, surfaces: Optional[List[Surface]] = None,
                 io: Optional[Dict[str, Callable[..., Any]]] = None,
                 dry_run: bool = False,
                 preflights: Optional[List[PreflightSpec]] = None):
        self.surfaces = surfaces if surfaces is not None else DEFAULT_SURFACES
        if preflights is not None:
            self.preflights = preflights
        elif (
            surfaces is None
            and DEFAULT_SURFACES is _BUILTIN_SURFACES
            and (io is None or "sentinel_plist_check" in io)
        ):
            self.preflights = DEFAULT_PREFLIGHTS
        else:
            self.preflights = []
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

        for d in diagnose_preflights(self.preflights, self.io):
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
        # Severity reflects consequence, not novelty (see finding_severity): a
        # long or old lag means a merged fix is not actually running. It is
        # graded BEFORE the cooldown so a warning that becomes critical (a lag
        # that jumps past BEHIND_CRITICAL_COMMITS, or crosses the age limit) is
        # alerted at once instead of waiting out the warning's cooldown. A
        # record from before severity was stored was graded critical.
        severity = finding_severity(d, (prev or {}).get("first_seen"), now)
        escalated = bool(prev) and prev.get("severity", "critical") == "warning" \
            and severity == "critical"
        if prev and not escalated and (now - prev.get("last_alert", 0)) < COOLDOWN_SECONDS:
            log(f"  finding suppressed (cooldown active for {fp})")
            return
        if self.dry_run:
            log(f"  dry-run: would post finding {fp}")
            return
        outcome = self.io["post_finding"]({
            "event_type": FINDING_KIND,
            "severity": severity,
            "message": (f"{d.surface}: {d.detail}. Deploy is a human action — "
                        f"pull and restart deliberately; this doctor never heals."),
            "fingerprint": fp,
            "agent_id": doctor_layer_agent_id("deploy-drift-doctor"),
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
            "severity": severity,
        }

    def _resolve(self, fp: str, rec: Dict[str, Any]) -> None:
        """Close a finding whose drift has cleared. Local state only.

        This deliberately emits NO ``outcome_event``. It used to emit one with
        ``status="confirmed"``, gated on ``DEPLOY_DRIFT_DOCTOR_UUID`` — which
        would have written a self-graded row into the exogenous-anchor channel.

        ``build_resolution_outcome_args`` stamps
        ``verification_source='external_signal'``, which
        ``src/grounding/outcome_anchors.py`` tiers as ``TRUSTED_EXTERNAL`` — a
        gold anchor for the EISV falsifiability test. Its justification is
        explicit that the outcome is ground truth because a finding was
        "adjudicated by an operator/human … not the loop validating its own
        trajectory" (roadmap Invariant 4). Nothing of the sort happens here:
        this fires on an unattended hourly tick when the doctor's own re-check
        no longer sees drift. The judge and the judged are the same process.

        It was also structurally incapable of being wrong. There is no path in
        this doctor that emits a dismissal, so every row it could ever write is
        ``_confirmed`` / ``is_bad=False`` — and an ``fp`` dismissal is the sole
        true-negative in the precision math. An all-positive label generator
        inflates the outcome and day counts that the falsifier-progress readout
        tracks while contributing nothing to the bad-rate it is measuring.

        Two accidents kept that inert: the env var is unset on every live plist,
        and ``_adjudication_progress`` currently filters to
        ``sentinel_finding_%``/``watcher_finding_%``. Neither is a safeguard —
        one ``export`` armed it — so the path is removed rather than re-gated.

        A cleared condition is a state fact, not a verdict on this doctor's
        judgment. It is logged and closed locally; if that transition is worth
        keeping centrally it belongs in a metric or an audit event, not in the
        channel whose only value is that it comes from outside the loop.
        """
        log(f"  closed locally (condition cleared on {rec.get('surface')}; "
            f"no outcome_event — see _resolve docstring)")


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
