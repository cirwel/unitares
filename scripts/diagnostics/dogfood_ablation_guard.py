#!/usr/bin/env python3
"""Silent guard for UNITARES dogfood identity and ablation-lane invariants.

This script is deliberately small and CI-testable: pure helper functions hold the
invariants, while the CLI wires them to the live REST endpoint and repo-local
analysis scripts. Empty stdout means no regression detected.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Sequence

DEFAULT_HTTP_URL = "http://127.0.0.1:8767"
DEFAULT_REPO = Path(__file__).resolve().parents[2]
DEFAULT_PYTHON = "/usr/local/bin/python3"
DEFAULT_TIMEOUT_SECONDS = 120
IDENTITY_ALERT = "no-session get_governance_metrics is not identity-neutral"
INVENTORY_ALERT = "outcome inventory no longer exposes BEAM/substrate eprocess lanes"
MATRIX_ALERT = "ablation matrix default no longer excludes BEAM harness lane"


def identity_neutrality_alert(metrics: dict[str, Any]) -> str | None:
    """Return an alert when a no-session read exposes a caller identity."""

    signature = metrics.get("agent_signature")
    signature_uuid = signature.get("uuid") if isinstance(signature, dict) else None
    identity_values = (
        metrics.get("agent_id"),
        metrics.get("display_name"),
        metrics.get("agent_uuid"),
        signature_uuid,
    )
    if metrics.get("status") != "⚪ unbound" or any(identity_values):
        return IDENTITY_ALERT
    return None


def parse_inventory_counts(text: str) -> dict[str, int]:
    """Parse top-level integer counters from an outcome-inventory report."""

    counts: dict[str, int] = {}
    for key in (
        "strict_bad",
        "strict_outcomes",
        "eprocess_eligible",
        "eprocess_eligible_beam",
        "eprocess_eligible_substrate",
        "prediction_id_present",
    ):
        match = re.search(rf"^{re.escape(key)}:\s*(\d+)\s*$", text, re.MULTILINE)
        if match:
            counts[key] = int(match.group(1))
    return counts


def inventory_lane_alert(counts: dict[str, int]) -> str | None:
    """Return an alert if inventory output stopped exposing harness lanes."""

    if "eprocess_eligible_beam" not in counts or "eprocess_eligible_substrate" not in counts:
        return INVENTORY_ALERT
    return None


def matrix_exclusion_alert(text: str) -> str | None:
    """Return an alert if default matrix output no longer excludes BEAM."""

    return None if "Excluded harness lanes: `beam`" in text else MATRIX_ALERT


def render_alert_report(alerts: Sequence[str], evidence: Sequence[str]) -> str:
    """Render a Hermes no-agent cron alert; return empty string when healthy."""

    if not alerts:
        return ""
    lines = [
        "UNITARES dogfood/ablation guard",
        "Signal: " + "; ".join(alerts),
        "Evidence:",
    ]
    lines.extend(f"- {item}" for item in evidence)
    lines.append(
        "Next: inspect identity proof-origin and harness-lane code before interpreting dogfood or ablation signals."
    )
    return "\n".join(lines)


def call_tool_no_session(http_url: str, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
    """Call the REST tool endpoint without session or identity headers."""

    payload = {"name": name, "arguments": arguments}
    req = urllib.request.Request(
        f"{http_url.rstrip('/')}/v1/tools/call",
        data=json.dumps(payload).encode("utf-8"),
        headers={"Accept": "application/json", "Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=10) as response:  # noqa: S310 - localhost ops guard
        outer = json.loads(response.read().decode("utf-8"))
    result = outer.get("result") if isinstance(outer, dict) else outer
    if isinstance(result, str):
        try:
            result = json.loads(result)
        except json.JSONDecodeError:
            return {"raw_result": result}
    return result if isinstance(result, dict) else {"raw_result": result}


def run_repo_script(
    repo: Path,
    python: str,
    script: str,
    args: Sequence[str],
    *,
    timeout_seconds: int = DEFAULT_TIMEOUT_SECONDS,
) -> str:
    """Run a repo-local analysis script under the intended project Python."""

    env = os.environ.copy()
    env["PATH"] = f"/usr/local/bin:{env.get('PATH', '')}"
    env["UNITARES_PYTHON"] = python
    proc = subprocess.run(
        [python, script, *args],
        cwd=repo,
        env=env,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        timeout=timeout_seconds,
        check=False,
    )
    if proc.returncode != 0:
        raise RuntimeError(f"{script} exited {proc.returncode}: {proc.stdout[-1200:]}")
    return proc.stdout


def collect_alerts(
    *,
    http_url: str,
    repo: Path,
    python: str,
    timeout_seconds: int,
) -> tuple[list[str], list[str]]:
    """Collect guard alerts and compact evidence from live/local checks."""

    alerts: list[str] = []
    evidence: list[str] = []

    try:
        metrics = call_tool_no_session(http_url, "get_governance_metrics", {"lite": True})
        identity_summary = {
            "status": metrics.get("status"),
            "agent_id": metrics.get("agent_id"),
            "display_name": metrics.get("display_name"),
            "agent_uuid": metrics.get("agent_uuid"),
            "signature_uuid": (metrics.get("agent_signature") or {}).get("uuid")
            if isinstance(metrics.get("agent_signature"), dict)
            else None,
        }
        evidence.append(
            "no_session_metrics="
            + json.dumps(identity_summary, ensure_ascii=False, sort_keys=True)
        )
        if alert := identity_neutrality_alert(metrics):
            alerts.append(alert)
    except (OSError, urllib.error.URLError, RuntimeError, json.JSONDecodeError) as exc:
        alerts.append("no-session identity neutrality check failed to run")
        evidence.append(f"identity_check_error={type(exc).__name__}: {exc}")

    try:
        inventory = run_repo_script(
            repo,
            python,
            "scripts/analysis/outcome_inventory.py",
            ("--window-days", "90", "--leads", "0,5,30"),
            timeout_seconds=timeout_seconds,
        )
        counts = parse_inventory_counts(inventory)
        evidence.append(
            "inventory="
            f"eprocess_eligible={counts.get('eprocess_eligible')}, "
            f"lanes=beam={counts.get('eprocess_eligible_beam')},"
            f"substrate={counts.get('eprocess_eligible_substrate')}, "
            f"strict_bad={counts.get('strict_bad')}"
        )
        if alert := inventory_lane_alert(counts):
            alerts.append(alert)
    except (OSError, RuntimeError, subprocess.TimeoutExpired) as exc:
        alerts.append("outcome inventory lane check failed to run")
        evidence.append(f"inventory_error={type(exc).__name__}: {exc}")

    try:
        matrix = run_repo_script(
            repo,
            python,
            "scripts/analysis/eisv_ablation_matrix.py",
            ("--scopes", "strict,task", "--windows", "30,90", "--leads", "0,5,30"),
            timeout_seconds=timeout_seconds,
        )
        excluded = matrix_exclusion_alert(matrix) is None
        evidence.append(f"matrix_excludes_beam={excluded}")
        if not excluded:
            alerts.append(MATRIX_ALERT)
    except (OSError, RuntimeError, subprocess.TimeoutExpired) as exc:
        alerts.append("ablation matrix lane-exclusion check failed to run")
        evidence.append(f"matrix_error={type(exc).__name__}: {exc}")

    return alerts, evidence


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    """Parse CLI options for the silent guard."""

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--http-url", default=os.environ.get("UNITARES_HTTP_URL", DEFAULT_HTTP_URL))
    parser.add_argument("--repo", type=Path, default=Path(os.environ.get("UNITARES_REPO", DEFAULT_REPO)))
    parser.add_argument("--python", default=os.environ.get("UNITARES_PYTHON", DEFAULT_PYTHON))
    parser.add_argument(
        "--timeout-seconds",
        type=int,
        default=int(os.environ.get("UNITARES_GUARD_TIMEOUT", str(DEFAULT_TIMEOUT_SECONDS))),
    )
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    """Run the guard and print only actionable alerts."""

    args = parse_args(argv)
    alerts, evidence = collect_alerts(
        http_url=args.http_url,
        repo=args.repo,
        python=args.python,
        timeout_seconds=args.timeout_seconds,
    )
    report = render_alert_report(alerts, evidence)
    if report:
        print(report)
    return 0


if __name__ == "__main__":
    sys.exit(main())
