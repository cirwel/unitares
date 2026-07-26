#!/usr/bin/env python3
"""Self-doctor for "Lumen not checking in" — diagnose, bounded heal, verify, escalate.

Encodes the four field-diagnosed check-in failure classes as a decision tree
instead of operator runbook prose. Design contract (in order, non-negotiable):

  1. DIAGNOSE FIRST — never act on the symptom. The four classes share the
     symptom (central record stops advancing) but have four different fixes,
     and applying the wrong one makes things worse (e.g. resume on a DNS
     freeze, or a service restart on a real governance pause).
  2. BOUNDED HEAL — only classes with a mechanical, field-verified fix are
     auto-healed (C1 resume, C2 service restart). C3 (dropped wire tool name)
     needs a code change; C4 (both-store binding loss) is operator-only BY
     DESIGN (fail-closed identity gates — see the binding-durability ontology;
     the doctor names the runbook, it must NEVER run it).
  3. VERIFY AFTER — a heal is only reported as done when the central record
     actually advances afterward. Never claim without evidence.
  4. ESCALATE HONESTLY — unknown diagnosis, failed verify, or the flap-cap
     (same class healed too often = a root cause being masked) posts a
     governance finding (-> Discord #alerts) saying what was tried and what
     the operator should do. Repeat findings per class are cooldown-limited.

Classes (fingerprints from the 2026-06/07 incidents):
  C1 false-pause      central paused moments after a governance-mcp restart,
                      Pi healthy -> agent(action=resume). Root cause fixed
                      (#575+#577) so recurrence should be rare; the resume is
                      gated on restart-proximity + Pi health, otherwise the
                      pause is treated as real and escalated (pause is a
                      hypothesis, but not this doctor's to overrule).
  C2 dns-freeze       central active-but-frozen; Pi journal shows
                      "Cannot connect to host ... Name or service not known"
                      -> restart tailscaled then anima via the anima MCP
                      admin-gated system_service tool.
  C3 unknown-tool     journal shows "UNITARES rejected check-in: Unknown tool"
                      -> escalate with the exact line (a wire client missed a
                      tool-surface consolidation; code fix, not a restart).
  C4 binding-loss     PATH2_RESUME_MISS in the server error log + no live
                      core.sessions row + Redis restarted since the freeze
                      -> escalate naming scripts/ops/rebind-resident-session.sh.

Post-Elixir-cutover note: the Pi's `unitares_stale` / `unitares_last_success_age_s`
diagnostics are fed by the Python-era client and are unreliable now that the
broker (unitares_ex) owns check-ins — central `agent(get).last_update` age is
the ONLY primary trigger. Pi diagnostics are secondary evidence.

Runs from launchd (com.unitares.lumen-doctor.plist.template) every 10 min.
Stubbable for tests: all I/O goes through the Doctor.io callable table.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

LUMEN_UUID = os.environ.get(
    "LUMEN_GOVERNANCE_UUID", "69a1a4f7-a30f-4f4a-bcf9-2de8606fb819"
)
LUMEN_IDENTITY_ID = int(os.environ.get("LUMEN_IDENTITY_ID", "2522"))
GOV_URL = os.environ.get("UNITARES_GOVERNANCE_HTTP_URL", "http://127.0.0.1:8767")
ANIMA_URL = os.environ.get("ANIMA_HTTP_URL", "http://lumen:8766")
SECRETS_FILE = os.path.expanduser(
    os.environ.get("UNITARES_SECRETS_ENV", "~/.config/cirwel/secrets.env")
)
STATE_FILE = os.path.expanduser(
    os.environ.get("LUMEN_DOCTOR_STATE", "~/.unitares/lumen-doctor.state.json")
)
SERVER_ERROR_LOG = os.path.expanduser(
    os.environ.get(
        "UNITARES_SERVER_ERROR_LOG",
        "~/projects/unitares-deploy/data/logs/mcp_server_error.log",
    )
)
PI_SSH_HOST = os.environ.get("LUMEN_SSH_HOST", "pi-anima")

STALE_S = int(os.environ.get("LUMEN_DOCTOR_STALE_S", "900"))  # 3 missed 5-min beats
RESTART_PROXIMITY_S = int(os.environ.get("LUMEN_DOCTOR_RESTART_PROXIMITY_S", "900"))
VERIFY_TIMEOUT_S = int(os.environ.get("LUMEN_DOCTOR_VERIFY_TIMEOUT_S", "420"))
VERIFY_POLL_S = int(os.environ.get("LUMEN_DOCTOR_VERIFY_POLL_S", "60"))
HEAL_CAP_PER_24H = int(os.environ.get("LUMEN_DOCTOR_HEAL_CAP", "2"))
ALERT_COOLDOWN_S = int(os.environ.get("LUMEN_DOCTOR_ALERT_COOLDOWN_S", "21600"))
HTTP_TIMEOUT_S = int(os.environ.get("LUMEN_DOCTOR_HTTP_TIMEOUT_S", "10"))

HEALTHY = "healthy"
C1_FALSE_PAUSE = "false_pause"
PAUSED_REAL = "paused_unexplained"
C2_DNS_FREEZE = "dns_freeze"
C3_UNKNOWN_TOOL = "unknown_tool"
C4_BINDING_LOSS = "binding_loss"
UNKNOWN = "unknown"
UNREACHABLE = "central_unreachable"

AUTO_HEALABLE = {C1_FALSE_PAUSE, C2_DNS_FREEZE}

RUNBOOK_C4 = (
    "operator runbook (BY DESIGN, do not automate): "
    "unitares scripts/ops/rebind-resident-session.sh "
    f"{LUMEN_UUID} agent-{LUMEN_UUID[:12]}"
)


def _load_secret(name: str) -> str:
    if os.environ.get(name):
        return os.environ[name]
    try:
        for line in Path(SECRETS_FILE).read_text().splitlines():
            line = line.strip()
            if line.startswith(f"{name}=") or line.startswith(f"export {name}="):
                return line.split("=", 1)[1].strip().strip('"').strip("'")
    except OSError:
        pass
    return ""


def _http_json(url: str, payload: dict | None = None, headers: dict | None = None) -> dict:
    req = urllib.request.Request(
        url,
        data=json.dumps(payload).encode() if payload is not None else None,
        headers={"Content-Type": "application/json", **(headers or {})},
        method="POST" if payload is not None else "GET",
    )
    with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT_S) as resp:
        return json.loads(resp.read().decode())


def _run(cmd: list[str], timeout: int = 20) -> str:
    try:
        return subprocess.run(
            cmd, capture_output=True, text=True, timeout=timeout
        ).stdout
    except (subprocess.SubprocessError, OSError):
        return ""


# ---------------------------------------------------------------- signal I/O
# Every external read/action lives here so tests can stub the whole table.

def io_central_agent() -> dict:
    out = _http_json(
        f"{GOV_URL}/v1/tools/call",
        {"name": "agent", "arguments": {"action": "get", "agent_id": LUMEN_UUID}},
    )
    return out.get("result", {})


def io_pi_diagnostics() -> dict:
    try:
        out = _http_json(
            f"{ANIMA_URL}/v1/tools/call", {"name": "diagnostics", "arguments": {}}
        )
        return out.get("result", {}) or {}
    except (urllib.error.URLError, OSError, json.JSONDecodeError, TimeoutError):
        return {}


def io_gov_process_start_epoch() -> float | None:
    out = _run(["pgrep", "-f", "mcp_server.py"])
    pids = [p for p in out.split() if p.isdigit()]
    if not pids:
        return None
    out = _run(["ps", "-o", "lstart=", "-p", pids[0]])
    try:
        return datetime.strptime(out.strip(), "%a %b %d %H:%M:%S %Y").timestamp()
    except ValueError:
        return None


def io_pi_journal_tail() -> str:
    return _run(
        ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=10", PI_SSH_HOST,
         "sudo journalctl -u anima --since '-45 min' --no-pager | "
         "grep -iE 'unitares|circuit' | tail -40"],
        timeout=30,
    )


def io_live_session_row_count() -> int | None:
    out = _run(
        ["psql", "-h", "localhost", "-U", "postgres", "-d", "governance", "-tAc",
         f"select count(*) from core.sessions where identity_id={LUMEN_IDENTITY_ID}"
         " and expires_at > now()"]
    ).strip()
    return int(out) if out.isdigit() else None


def io_redis_uptime_s() -> int | None:
    out = _run(["redis-cli", "INFO", "server"])
    m = re.search(r"uptime_in_seconds:(\d+)", out)
    return int(m.group(1)) if m else None


def io_path2_miss_recent() -> bool:
    try:
        with open(SERVER_ERROR_LOG, "rb") as fh:
            fh.seek(0, os.SEEK_END)
            fh.seek(max(0, fh.tell() - 262144))
            return b"PATH2_RESUME_MISS" in fh.read()
    except OSError:
        return False


def io_resume(token: str) -> dict:
    out = _http_json(
        f"{GOV_URL}/v1/tools/call",
        {"name": "agent", "arguments": {"action": "resume", "agent_id": LUMEN_UUID}},
        headers={"Authorization": f"Bearer {token}"} if token else {},
    )
    return out.get("result", {})


def io_pi_restart_services(admin_secret: str) -> list[str]:
    results = []
    for svc in ("tailscaled", "anima"):
        try:
            out = _http_json(
                f"{ANIMA_URL}/v1/tools/call",
                {"name": "system_service",
                 "arguments": {"action": "restart", "service": svc}},
                headers={"X-Anima-Admin": admin_secret} if admin_secret else {},
            )
            results.append(f"{svc}: {json.dumps(out.get('result', out))[:120]}")
        except (urllib.error.URLError, OSError, TimeoutError) as exc:
            results.append(f"{svc}: request failed ({exc})")
    return results


def io_post_finding(severity: str, fingerprint: str, message: str, token: str) -> None:
    try:
        _http_json(
            f"{GOV_URL}/api/findings",
            {"type": "lumen_checkin_finding", "severity": severity,
             "message": message, "agent_id": "lumen-checkin-doctor",
             "agent_name": "lumen-checkin-doctor", "fingerprint": fingerprint},
            headers={"Authorization": f"Bearer {token}"} if token else {},
        )
    except (urllib.error.URLError, OSError, TimeoutError):
        pass  # escalation is best-effort; the log line below always lands


DEFAULT_IO: dict[str, Callable[..., Any]] = {
    "central_agent": io_central_agent,
    "pi_diagnostics": io_pi_diagnostics,
    "gov_process_start_epoch": io_gov_process_start_epoch,
    "pi_journal_tail": io_pi_journal_tail,
    "live_session_row_count": io_live_session_row_count,
    "redis_uptime_s": io_redis_uptime_s,
    "path2_miss_recent": io_path2_miss_recent,
    "resume": io_resume,
    "pi_restart_services": io_pi_restart_services,
    "post_finding": io_post_finding,
    "now": time.time,
    "sleep": time.sleep,
}


# -------------------------------------------------------------- pure classify

def _parse_ts(value: str | None) -> float | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


@dataclass
class Signals:
    central: dict = field(default_factory=dict)
    central_error: str | None = None
    now: float = 0.0
    pi_diag: dict = field(default_factory=dict)
    gov_start_epoch: float | None = None
    journal: str = ""
    session_rows: int | None = None
    redis_uptime_s: int | None = None
    path2_miss: bool = False


def classify(s: Signals) -> tuple[str, str]:
    """Map gathered signals to (class, evidence). Pure — no I/O."""
    if s.central_error:
        return UNREACHABLE, f"central agent(get) failed: {s.central_error}"

    last_update = _parse_ts(s.central.get("last_update"))
    age = (s.now - last_update) if last_update else None
    status = s.central.get("status", "")

    if status == "paused":
        paused_at = None
        for ev in reversed(s.central.get("lifecycle_events") or []):
            if ev.get("event") == "paused":
                paused_at = _parse_ts(ev.get("timestamp") or ev.get("at"))
                break
        near_restart = (
            paused_at is not None
            and s.gov_start_epoch is not None
            and 0 <= paused_at - s.gov_start_epoch <= RESTART_PROXIMITY_S
        )
        pi_gov = (s.pi_diag or {}).get("governance") or {}
        pi_healthy = bool(s.pi_diag) and pi_gov.get("last_decision_source") not in (None, "")
        if near_restart and pi_healthy:
            return C1_FALSE_PAUSE, (
                f"paused {int(paused_at - s.gov_start_epoch)}s after governance-mcp "
                "start, Pi reachable and reporting — restart false-pause fingerprint"
            )
        return PAUSED_REAL, (
            "central paused and the false-pause fingerprint does NOT match "
            f"(near_restart={near_restart}, pi_reachable={bool(s.pi_diag)}) — "
            "treating the pause as a real governance verdict; not auto-resuming"
        )

    if age is None:
        return UNKNOWN, "central record has no parseable last_update"
    if age <= STALE_S:
        return HEALTHY, f"central last_update {int(age)}s ago"

    # Active but frozen — differentiate via the Pi journal (the tell for C2/C3).
    if re.search(r"Unknown tool", s.journal, re.IGNORECASE):
        line = next(
            (ln for ln in s.journal.splitlines() if re.search(r"Unknown tool", ln, re.I)),
            "",
        )
        return C3_UNKNOWN_TOOL, f"check-ins rejected at API level: {line.strip()[:200]}"
    if re.search(r"Cannot connect to host|Name or service not known", s.journal, re.I):
        line = next(
            (ln for ln in s.journal.splitlines()
             if re.search(r"Cannot connect to host|Name or service", ln, re.I)),
            "",
        )
        return C2_DNS_FREEZE, f"Pi cannot reach central: {line.strip()[:200]}"
    if (
        s.path2_miss
        and s.session_rows == 0
        and s.redis_uptime_s is not None
        and s.redis_uptime_s < age
    ):
        return C4_BINDING_LOSS, (
            f"PATH2_RESUME_MISS in server log, no live core.sessions row for "
            f"identity_id={LUMEN_IDENTITY_ID}, Redis uptime {s.redis_uptime_s}s < "
            f"freeze age {int(age)}s — both-store binding loss"
        )
    return UNKNOWN, (
        f"central frozen {int(age)}s but no known fingerprint matches "
        f"(journal_lines={len(s.journal.splitlines())}, "
        f"session_rows={s.session_rows}, redis_uptime_s={s.redis_uptime_s}) — "
        "check the anima-broker-ex (unitares_ex) path; it owns check-ins post-cutover"
    )


# ------------------------------------------------------------------- doctor

class Doctor:
    def __init__(self, io: dict[str, Callable[..., Any]] | None = None,
                 dry_run: bool = False):
        self.io = {**DEFAULT_IO, **(io or {})}
        self.dry_run = dry_run
        self.state = self._load_state()

    # -- state (flap-cap + alert cooldown), same pattern as the bridge watchdog
    def _load_state(self) -> dict:
        try:
            return json.loads(Path(STATE_FILE).read_text())
        except (OSError, json.JSONDecodeError):
            return {}

    def _save_state(self) -> None:
        try:
            Path(STATE_FILE).parent.mkdir(parents=True, exist_ok=True)
            Path(STATE_FILE).write_text(json.dumps(self.state))
        except OSError:
            pass

    def _heal_capped(self, cls: str) -> bool:
        cutoff = self.io["now"]() - 86400
        heals = [t for t in self.state.get("heals", {}).get(cls, []) if t > cutoff]
        return len(heals) >= HEAL_CAP_PER_24H

    def _record_heal(self, cls: str) -> None:
        cutoff = self.io["now"]() - 86400
        heals = self.state.setdefault("heals", {})
        heals[cls] = [t for t in heals.get(cls, []) if t > cutoff] + [self.io["now"]()]

    def _alert_allowed(self, cls: str) -> bool:
        last = self.state.get("alerts", {}).get(cls, 0)
        return self.io["now"]() - last >= ALERT_COOLDOWN_S

    def _record_alert(self, cls: str) -> None:
        self.state.setdefault("alerts", {})[cls] = self.io["now"]()

    # -- gather
    def gather(self, deep: bool = False) -> Signals:
        s = Signals(now=self.io["now"]())
        try:
            s.central = self.io["central_agent"]()
        except (urllib.error.URLError, OSError, TimeoutError, json.JSONDecodeError) as exc:
            s.central_error = str(exc)
            return s
        s.pi_diag = self.io["pi_diagnostics"]()
        s.gov_start_epoch = self.io["gov_process_start_epoch"]()
        if deep:
            s.journal = self.io["pi_journal_tail"]()
            s.session_rows = self.io["live_session_row_count"]()
            s.redis_uptime_s = self.io["redis_uptime_s"]()
            s.path2_miss = self.io["path2_miss_recent"]()
        return s

    # -- heal + verify
    def _verify_recovered(self) -> bool:
        deadline = self.io["now"]() + VERIFY_TIMEOUT_S
        while self.io["now"]() < deadline:
            self.io["sleep"](VERIFY_POLL_S)
            try:
                central = self.io["central_agent"]()
            except (urllib.error.URLError, OSError, TimeoutError, json.JSONDecodeError):
                continue
            last = _parse_ts(central.get("last_update"))
            if (
                central.get("status") == "active"
                and last is not None
                and self.io["now"]() - last < STALE_S
            ):
                return True
        return False

    def heal(self, cls: str) -> tuple[bool, str]:
        token = _load_secret("UNITARES_HTTP_API_TOKEN")
        if cls == C1_FALSE_PAUSE:
            result = self.io["resume"](token)
            if not result.get("success"):
                return False, f"resume call failed: {json.dumps(result)[:200]}"
            action = "resumed via agent(action=resume)"
        elif cls == C2_DNS_FREEZE:
            admin = _load_secret("ANIMA_ADMIN_SECRET")
            outcomes = self.io["pi_restart_services"](admin)
            action = "restarted tailscaled+anima via system_service: " + "; ".join(outcomes)
        else:
            return False, f"no auto-heal defined for {cls}"
        if self._verify_recovered():
            return True, f"{action}; VERIFIED central check-ins resumed"
        return False, f"{action}; verify FAILED — central still not advancing"

    # -- one pass
    def run_once(self) -> str:
        signals = self.gather(deep=False)
        cls, evidence = classify(signals)
        if cls == HEALTHY:
            self.state.pop("unreachable_since", None)
            self._save_state()
            log(f"OK — {evidence}")
            return cls

        # Central down is its own case: can't diagnose Lumen through a dead server,
        # and the governance-mcp KeepAlive + backup monitors own that surface.
        if cls == UNREACHABLE:
            first = self.state.setdefault("unreachable_since", self.io["now"]())
            self._save_state()
            log(f"central unreachable since {int(self.io['now']() - first)}s — {evidence}")
            return cls

        # Re-gather with the expensive probes before acting on a freeze.
        if cls not in (C1_FALSE_PAUSE, PAUSED_REAL):
            signals = self.gather(deep=True)
            cls, evidence = classify(signals)
            if cls == HEALTHY:
                self._save_state()
                log(f"OK on deep re-check — {evidence}")
                return cls

        log(f"diagnosis: {cls} — {evidence}")
        if self.dry_run:
            log("dry-run: no heal, no finding")
            return cls

        if cls in AUTO_HEALABLE and not self._heal_capped(cls):
            self._record_heal(cls)
            self._save_state()
            healed, report = self.heal(cls)
            log(f"heal {'succeeded' if healed else 'FAILED'}: {report}")
            if healed:
                self.escalate(
                    "info", cls,
                    f"[self-healed] {cls}: {evidence}. {report}.", always_log=False,
                )
                return cls
            self.escalate(
                "critical", f"{cls}-heal-failed",
                f"Lumen check-in {cls}: auto-heal did not restore check-ins. "
                f"{evidence}. {report}. Operator attention needed.",
            )
            return cls

        if cls in AUTO_HEALABLE:  # capped
            self.escalate(
                "critical", f"{cls}-flapping",
                f"Lumen check-in {cls} healed {HEAL_CAP_PER_24H}x in 24h and recurred "
                f"— a root cause is being masked; NOT healing again. {evidence}",
            )
            return cls

        severity = "high" if cls in (C3_UNKNOWN_TOOL, C4_BINDING_LOSS) else "critical"
        remedy = {
            C3_UNKNOWN_TOOL: "a wire client is sending a dropped tool name — code fix "
                             "(see the unknown-tool-twin runbook); no restart will help",
            C4_BINDING_LOSS: RUNBOOK_C4,
            PAUSED_REAL: "review the pause verdict (dashboard/dialectic); resume only "
                         "after judging it — this doctor never overrules a real pause",
            UNKNOWN: "no known fingerprint — investigate broker (unitares_ex) and Pi",
        }.get(cls, "investigate")
        self.escalate(severity, cls, f"Lumen not checking in — {cls}. {evidence}. Next: {remedy}")
        return cls

    def escalate(self, severity: str, fingerprint_cls: str, message: str,
                 always_log: bool = True) -> None:
        if always_log:
            log(f"ESCALATE[{severity}] {message}")
        if self._alert_allowed(fingerprint_cls):
            self.io["post_finding"](
                severity, f"lumen-checkin-{fingerprint_cls}", message,
                _load_secret("UNITARES_HTTP_API_TOKEN"),
            )
            self._record_alert(fingerprint_cls)
            self._save_state()
        else:
            log(f"finding suppressed (cooldown active for {fingerprint_cls})")


def log(msg: str) -> None:
    print(f"[{datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S')}Z] {msg}",
          flush=True)


def main() -> int:
    dry = "--dry-run" in sys.argv
    doctor = Doctor(dry_run=dry)
    cls = doctor.run_once()
    return 0 if cls == HEALTHY else 1


if __name__ == "__main__":
    sys.exit(main())
