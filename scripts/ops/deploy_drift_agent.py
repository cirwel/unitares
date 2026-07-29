#!/usr/bin/env python3
"""Governed runner for the deploy-drift doctor — gives it a baselined identity.

WHY A SECOND ENTRYPOINT
-----------------------
``deploy_drift_doctor.py`` is deliberately a plain, dependency-light script: it
diagnoses drift and posts findings, and it runs fine with no governance
identity at all. What it *cannot* do in that form is close the lifecycle,
because ``outcome_event`` snapshots EISV **by agent_id** — a resolution emitted
by an agent with no baselined ``core.agent_state`` history produces an outcome
row carrying no EISV, which adds noise to the label-breadth problem it is meant
to help rather than helping it.

This runner wraps the same ``Doctor`` in the SDK's ``GovernanceAgent``, which
already owns identity resolution (anchor file -> server lookup -> fresh
onboard), session persistence, and the post-cycle check-in. Reusing it rather
than hand-rolling identity is deliberate: the hand-rolled path is where the
2026-04-20 Steward regression came from (a resident stamped ``persistent`` but
not ``autonomous``, so loop-detection silently starved every write for three
days). ``persistent=True`` here stamps the full ``RESIDENT_TAGS`` set.

WHAT THIS DOES NOT DO
---------------------
It does NOT add the doctor to ``KNOWN_RESIDENT_LABELS`` or the residents env
list. Baselined identity and resident-roster membership are separate things:
the doctor needs the former (so its outcomes carry EISV) and has no business in
the latter (it is not part of the fleet the dashboard reports on, and adding it
would silently change "N of 6 residents reporting").

CHECK-IN HONESTY
----------------
The per-cycle check-in reports what was actually examined and found — surfaces
checked, drift conditions raised, findings closed. It is not a heartbeat
manufactured to look governed; a cycle that finds nothing says so, and
confidence reflects whether every surface could actually be read (a checkout
that has vanished or a process that could not be inspected lowers it).
"""
from __future__ import annotations

import argparse
import asyncio
import os
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
for p in (str(REPO_ROOT), str(REPO_ROOT / "agents" / "sdk" / "src")):
    if p not in sys.path:
        sys.path.insert(0, p)

from scripts.ops.deploy_drift_doctor import (  # noqa: E402
    DEFAULT_SURFACES,
    Doctor,
    diagnose,
)


def _build_agent_cls():
    """Import the SDK lazily so the plain doctor stays dependency-light."""
    from unitares_sdk.agent import CycleResult, GovernanceAgent

    class DeployDriftAgent(GovernanceAgent):
        """Runs one drift diagnosis per cycle under a persistent identity."""

        def __init__(self, dry_run: bool = False, **kwargs):
            super().__init__(
                name="deploy-drift-doctor",
                persistent=True,          # stamps RESIDENT_TAGS; avoids orphan sweep
                spawn_reason="new_session",
                **kwargs,
            )
            self.dry_run = dry_run

        async def run_cycle(self, client):  # noqa: ANN001 - SDK-typed
            # The doctor is synchronous and does subprocess git calls; keep it
            # off the event loop so a slow fetch can't stall the SDK's cycle.
            def _work():
                doctor = Doctor(dry_run=self.dry_run)
                # Identity flows in from the SDK so resolutions carry EISV.
                if getattr(self, "agent_uuid", None):
                    os.environ["DEPLOY_DRIFT_DOCTOR_UUID"] = str(self.agent_uuid)
                readable = 0
                drifts = []
                for surface in doctor.surfaces:
                    if not os.path.isdir(surface.path):
                        continue
                    readable += 1
                    drifts.extend(diagnose(surface, doctor.io))
                doctor.run()
                return readable, drifts

            readable, drifts = await asyncio.get_running_loop().run_in_executor(
                None, _work
            )

            total = len(DEFAULT_SURFACES)
            if not readable:
                # Nothing could be inspected — say so plainly rather than
                # reporting a clean fleet we did not actually observe.
                return CycleResult(
                    summary="deploy-drift: no surfaces readable this cycle",
                    complexity=0.2,
                    confidence=0.2,
                )
            if drifts:
                detail = "; ".join(f"{d.surface}:{d.condition}" for d in drifts)
                return CycleResult(
                    summary=f"deploy-drift: {len(drifts)} drift condition(s) — {detail}",
                    complexity=0.4,
                    # Full confidence only when every surface was readable.
                    confidence=0.85 if readable == total else 0.6,
                )
            return CycleResult(
                summary=f"deploy-drift: {readable}/{total} surfaces running merged code",
                complexity=0.2,
                confidence=0.85 if readable == total else 0.6,
            )

    return DeployDriftAgent


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Run the deploy-drift doctor under a governed identity so "
                    "its finding resolutions carry EISV.",
    )
    parser.add_argument("--dry-run", action="store_true",
                        help="diagnose and check in; post no findings, write no state")
    parser.add_argument("--once", action="store_true", default=True,
                        help="single cycle (default; launchd owns the schedule)")
    args = parser.parse_args()

    try:
        agent_cls = _build_agent_cls()
    except ImportError as exc:
        # The plain doctor still works without the SDK — say which one to run.
        print(f"[deploy-drift-agent] SDK unavailable ({exc}); "
              f"run scripts/ops/deploy_drift_doctor.py for ungoverned diagnosis",
              file=sys.stderr)
        return 2

    agent = agent_cls(dry_run=args.dry_run)
    asyncio.run(agent.run_once())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
