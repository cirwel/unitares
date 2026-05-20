#!/usr/bin/env python3
"""Run controlled Phase B lease-plane contention drills.

The drill creates safe synthetic surfaces, acquires each once, then attempts
to acquire it from a second holder. The second acquire should return
``held_by_other`` and emit a normal ``conflict_held_by_other`` audit event.

This is rehearsal evidence, not a production incident: the blocked caller's
``audit_session`` is prefixed with ``phase-b-drill:`` so the promotion
evaluator can accept it only when explicitly invoked with
``--accept-drill-evidence``.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from uuid import UUID, uuid4

_PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from src.lease_plane import (  # noqa: E402
    AcquireHeldByOther,
    AcquireOk,
    AcquireRequest,
    LeasePlaneClient,
    LeasePlaneClientConfig,
    ReleaseRequest,
    SimpleOk,
)

KNOWN_SURFACE_KINDS = ("dialectic", "resident", "file", "capture", "td")
DRILL_AUDIT_SESSION_PREFIX = "phase-b-drill:"


class DrillError(RuntimeError):
    """Raised when a drill cannot be run safely."""


@dataclass
class DrillAttempt:
    surface_id: str
    blocker_holder_uuid: str
    challenger_holder_uuid: str
    blocker_lease_id: str | None = None
    challenger_outcome: str | None = None
    release_ok: bool = False
    error: str | None = None


@dataclass
class DrillReport:
    surface_kind: str
    run_id: str
    audit_session: str
    attempts: list[DrillAttempt] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return bool(self.attempts) and all(a.challenger_outcome == "held_by_other" for a in self.attempts)


def make_client(
    *, base_url: str | None = None, bearer_token: str | None = None, timeout_s: float = 2.0
) -> LeasePlaneClient:
    token = (bearer_token if bearer_token is not None else os.environ.get("LEASE_PLANE_BEARER_TOKEN", "")).strip()
    if not token:
        raise DrillError("LEASE_PLANE_BEARER_TOKEN is required for a live drill")
    return LeasePlaneClient(
        LeasePlaneClientConfig(
            base_url=(base_url or os.environ.get("LEASE_PLANE_BASE_URL") or "http://127.0.0.1:8788"),
            bearer_token=token,
            timeout_s=timeout_s,
        )
    )


def run_drill(
    surface_kind: str,
    *,
    count: int = 3,
    ttl_s: int = 60,
    run_id: str | None = None,
    audit_session: str | None = None,
    client: LeasePlaneClient | None = None,
) -> DrillReport:
    if surface_kind not in KNOWN_SURFACE_KINDS:
        raise DrillError(f"unknown surface_kind {surface_kind!r}; expected one of {KNOWN_SURFACE_KINDS}")
    if count < 1:
        raise DrillError("count must be >= 1")

    run_id = run_id or datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ") + "-" + uuid4().hex[:8]
    audit_session = audit_session or f"{DRILL_AUDIT_SESSION_PREFIX}{surface_kind}:{run_id}"
    client = client or make_client()
    report = DrillReport(surface_kind=surface_kind, run_id=run_id, audit_session=audit_session)

    for index in range(1, count + 1):
        surface_id = drill_surface_id(surface_kind, run_id, index)
        blocker_holder_uuid = uuid4()
        challenger_holder_uuid = uuid4()
        attempt = DrillAttempt(
            surface_id=surface_id,
            blocker_holder_uuid=str(blocker_holder_uuid),
            challenger_holder_uuid=str(challenger_holder_uuid),
        )
        report.attempts.append(attempt)

        try:
            blocker = client.acquire(
                AcquireRequest(
                    surface_id=surface_id,
                    holder_agent_uuid=blocker_holder_uuid,
                    holder_class="process_instance",
                    holder_kind="remote_heartbeat",
                    ttl_s=ttl_s,
                    intent=f"phase-b-drill blocker {run_id} #{index}",
                    audit_session=audit_session,
                )
            )
            if not isinstance(blocker, AcquireOk):
                attempt.error = f"blocker acquire returned {type(blocker).__name__}"
                continue

            attempt.blocker_lease_id = str(blocker.lease.lease_id)
            challenger = client.acquire(
                AcquireRequest(
                    surface_id=surface_id,
                    holder_agent_uuid=challenger_holder_uuid,
                    holder_class="process_instance",
                    holder_kind="remote_heartbeat",
                    ttl_s=ttl_s,
                    intent=f"phase-b-drill challenger {run_id} #{index}",
                    audit_session=audit_session,
                )
            )
            if isinstance(challenger, AcquireHeldByOther):
                attempt.challenger_outcome = "held_by_other"
            elif isinstance(challenger, AcquireOk):
                attempt.challenger_outcome = "acquired"
                attempt.error = "challenger unexpectedly acquired the held surface"
            else:
                attempt.challenger_outcome = type(challenger).__name__
                attempt.error = f"challenger acquire returned {type(challenger).__name__}"
        finally:
            if attempt.blocker_lease_id:
                attempt.release_ok = release(client, UUID(attempt.blocker_lease_id))

    return report


def release(client: LeasePlaneClient, lease_id: UUID) -> bool:
    try:
        return isinstance(client.release(ReleaseRequest(lease_id=lease_id, release_reason="normal")), SimpleOk)
    except Exception:
        return False


def drill_surface_id(surface_kind: str, run_id: str, index: int) -> str:
    suffix = f"phase_b_drill/{run_id}/{index}"
    if surface_kind == "file":
        root = Path(tempfile.gettempdir()) / "unitares_lease_plane_phase_b_drill"
        return f"file://{root / run_id / str(index)}"
    if surface_kind == "capture":
        return f"capture:/phase_b_drill_{run_id}_{index}"
    return f"{surface_kind}:/{suffix}"


def _format_text(report: DrillReport) -> str:
    lines = [
        f"Phase B controlled drill — surface_kind={report.surface_kind}",
        f"  run_id: {report.run_id}",
        f"  audit_session: {report.audit_session}",
        f"  ok: {report.ok}",
        "",
    ]
    for attempt in report.attempts:
        marker = "✓" if attempt.challenger_outcome == "held_by_other" else "✗"
        lines.append(f"  {marker} {attempt.surface_id}")
        lines.append(f"      challenger_outcome: {attempt.challenger_outcome}")
        lines.append(f"      blocker_lease_id: {attempt.blocker_lease_id}")
        lines.append(f"      release_ok: {attempt.release_ok}")
        if attempt.error:
            lines.append(f"      error: {attempt.error}")
        lines.append("")
    lines.append(
        "Next: python3 scripts/lease_plane/evaluate_phase_b_promotion.py "
        f"{report.surface_kind} --accept-drill-evidence"
    )
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("surface_kind", choices=KNOWN_SURFACE_KINDS)
    parser.add_argument("--count", type=int, default=3)
    parser.add_argument("--ttl-s", type=int, default=60)
    parser.add_argument("--run-id")
    parser.add_argument("--audit-session")
    parser.add_argument("--base-url")
    parser.add_argument("--bearer-token")
    parser.add_argument("--timeout-s", type=float, default=2.0)
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)

    try:
        client = make_client(
            base_url=args.base_url,
            bearer_token=args.bearer_token,
            timeout_s=args.timeout_s,
        )
        report = run_drill(
            args.surface_kind,
            count=args.count,
            ttl_s=args.ttl_s,
            run_id=args.run_id,
            audit_session=args.audit_session,
            client=client,
        )
    except DrillError as exc:
        print(str(exc), file=sys.stderr)
        return 2

    print(json.dumps(asdict(report), indent=2) if args.json else _format_text(report))
    return 0 if report.ok else 1


if __name__ == "__main__":
    sys.exit(main())
