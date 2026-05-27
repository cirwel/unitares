#!/usr/bin/env python3
"""Claim BEAM lease-plane file surfaces before mutating code.

Examples:

    python3 scripts/dev/file_lease.py acquire src/foo.py --enforce
    python3 scripts/dev/file_lease.py guard --path src/foo.py -- pytest tests/test_foo.py
    python3 scripts/dev/file_lease.py guard --changed -- ./scripts/dev/test-cache.sh
    python3 scripts/dev/file_lease.py hold --changed
    python3 scripts/dev/file_lease.py changed
    python3 scripts/dev/file_lease.py status src/foo.py

The helper maps paths to canonical ``file://`` surface IDs and talks to the
Elixir lease plane on ``127.0.0.1:8788`` by default. ``guard`` is intended for
multi-agent codebase edits: it acquires every requested path, runs the command
only if all required leases were acquired, and releases acquired leases on exit.
``hold --changed`` refreshes the changed-path set on each heartbeat so new
worktree edits are claimed during the session.
"""

from __future__ import annotations

import argparse
import json
import os
import signal
import subprocess
import sys
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

_PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from src.lease_plane import (  # noqa: E402
    AcquireHeldByOther,
    AcquireOk,
    AcquirePermissionDenied,
    AcquireRequest,
    AcquireSchemaInvalid,
    AcquireServiceUnavailable,
    HeartbeatRequest,
    LeasePlaneClient,
    LeasePlaneClientConfig,
    LeasePlaneDisabledClient,
    ReleaseRequest,
    SimpleOk,
    StatusOk,
    StatusSchemaInvalid,
    StatusServiceUnavailable,
)


DEFAULT_TTL_S = 900


@dataclass
class LeaseAttempt:
    path: str
    surface_id: str
    outcome: str
    lease_id: str | None = None
    blocking_lease_id: str | None = None
    held_by_uuid: str | None = None
    expires_at: str | None = None
    retry_after_hint_ms: int | None = None
    detail: Any = None

    @property
    def acquired(self) -> bool:
        return self.outcome in {"acquired_new", "acquired_idempotent"}


def path_to_surface_id(path: str, *, cwd: Path | None = None) -> str:
    """Return a canonicalizable file surface ID for a workspace path."""
    base = cwd or Path.cwd()
    p = Path(path).expanduser()
    if not p.is_absolute():
        p = base / p
    return f"file://{p}"


def changed_paths(*, cwd: Path | None = None, include_untracked: bool = True) -> list[str]:
    """Return staged, unstaged, and untracked git paths for this worktree."""
    base = cwd or Path.cwd()
    paths: list[str] = []

    for args in (
        ["git", "diff", "--name-only"],
        ["git", "diff", "--cached", "--name-only"],
    ):
        paths.extend(_git_output_lines(args, cwd=base))

    if include_untracked:
        paths.extend(_git_output_lines(["git", "ls-files", "--others", "--exclude-standard"], cwd=base))

    seen: set[str] = set()
    ordered: list[str] = []
    for path in paths:
        if path and path not in seen:
            seen.add(path)
            ordered.append(path)
    return ordered


def _git_output_lines(args: list[str], *, cwd: Path) -> list[str]:
    result = subprocess.run(
        args,
        cwd=str(cwd),
        check=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    return [line for line in result.stdout.splitlines() if line]


def make_client(
    *,
    base_url: str | None = None,
    bearer_token: str | None = None,
    timeout_s: float = 2.0,
) -> LeasePlaneClient:
    token = (bearer_token if bearer_token is not None else os.environ.get("LEASE_PLANE_BEARER_TOKEN", "")).strip()
    if not token:
        return LeasePlaneDisabledClient()
    return LeasePlaneClient(
        LeasePlaneClientConfig(
            base_url=base_url or os.environ.get("LEASE_PLANE_BASE_URL") or "http://127.0.0.1:8788",
            bearer_token=token,
            timeout_s=timeout_s,
        )
    )


def acquire_paths(
    paths: list[str],
    *,
    client: LeasePlaneClient,
    holder_uuid: UUID,
    ttl_s: int = DEFAULT_TTL_S,
    intent: str | None = None,
    audit_session: str | None = None,
    cwd: Path | None = None,
) -> list[LeaseAttempt]:
    attempts: list[LeaseAttempt] = []
    for path in paths:
        surface_id = path_to_surface_id(path, cwd=cwd)
        try:
            request = AcquireRequest(
                surface_id=surface_id,
                holder_agent_uuid=holder_uuid,
                holder_class="process_instance",
                holder_kind="remote_heartbeat",
                ttl_s=ttl_s,
                intent=intent,
                audit_session=audit_session,
            )
            result = client.acquire(request)
        except Exception as exc:  # noqa: BLE001 - CLI must classify, not crash.
            attempts.append(
                LeaseAttempt(
                    path=path,
                    surface_id=surface_id,
                    outcome="client_error",
                    detail=f"{type(exc).__name__}: {exc}",
                )
            )
            continue

        attempts.append(_attempt_from_result(path, surface_id, result))
    return attempts


def _attempt_from_result(path: str, surface_id: str, result: object) -> LeaseAttempt:
    if isinstance(result, AcquireOk):
        return LeaseAttempt(
            path=path,
            surface_id=result.lease.surface_id,
            outcome="acquired_idempotent" if result.idempotent else "acquired_new",
            lease_id=str(result.lease.lease_id),
            expires_at=result.lease.expires_at.isoformat(),
        )
    if isinstance(result, AcquireHeldByOther):
        return LeaseAttempt(
            path=path,
            surface_id=result.surface_id,
            outcome="held_by_other",
            blocking_lease_id=str(result.blocking_lease_id),
            held_by_uuid=str(result.held_by_uuid),
            expires_at=result.expires_at.isoformat(),
            retry_after_hint_ms=result.retry_after_hint_ms,
        )
    if isinstance(result, AcquireServiceUnavailable):
        return LeaseAttempt(
            path=path,
            surface_id=surface_id,
            outcome="service_unavailable",
            detail=result.reason,
        )
    if isinstance(result, AcquirePermissionDenied):
        return LeaseAttempt(
            path=path,
            surface_id=surface_id,
            outcome="permission_denied",
            detail=result.reason,
        )
    if isinstance(result, AcquireSchemaInvalid):
        return LeaseAttempt(
            path=path,
            surface_id=surface_id,
            outcome="schema_invalid",
            detail=result.detail,
        )
    return LeaseAttempt(
        path=path,
        surface_id=surface_id,
        outcome="client_error",
        detail=f"unrecognized result {type(result).__name__}",
    )


def release_leases(client: LeasePlaneClient, lease_ids: list[str]) -> list[dict[str, Any]]:
    releases: list[dict[str, Any]] = []
    for raw in lease_ids:
        try:
            lease_id = UUID(raw)
            result = client.release(ReleaseRequest(lease_id=lease_id, release_reason="normal"))
            releases.append({"lease_id": raw, "ok": isinstance(result, SimpleOk)})
        except Exception as exc:  # noqa: BLE001 - best-effort cleanup.
            releases.append({"lease_id": raw, "ok": False, "error": f"{type(exc).__name__}: {exc}"})
    return releases


def heartbeat_leases(client: LeasePlaneClient, lease_ids: list[str]) -> list[dict[str, Any]]:
    heartbeats: list[dict[str, Any]] = []
    for raw in lease_ids:
        try:
            lease_id = UUID(raw)
            result = client.heartbeat(HeartbeatRequest(lease_id=lease_id))
            heartbeats.append({"lease_id": raw, "ok": isinstance(result, SimpleOk)})
        except Exception as exc:  # noqa: BLE001 - keep the hold loop observable.
            heartbeats.append({"lease_id": raw, "ok": False, "error": f"{type(exc).__name__}: {exc}"})
    return heartbeats


def status_paths(paths: list[str], *, client: LeasePlaneClient, cwd: Path | None = None) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for path in paths:
        surface_id = path_to_surface_id(path, cwd=cwd)
        try:
            result = client.status(surface_id)
        except Exception as exc:  # noqa: BLE001
            rows.append(
                {
                    "path": path,
                    "surface_id": surface_id,
                    "status": "client_error",
                    "detail": f"{type(exc).__name__}: {exc}",
                }
            )
            continue

        if isinstance(result, StatusOk):
            if result.lease is None:
                rows.append({"path": path, "surface_id": surface_id, "status": "free"})
            else:
                rows.append(
                    {
                        "path": path,
                        "surface_id": result.lease.surface_id,
                        "status": "held",
                        "lease_id": str(result.lease.lease_id),
                        "held_by_uuid": str(result.lease.holder_agent_uuid),
                        "expires_at": result.lease.expires_at.isoformat(),
                    }
                )
        elif isinstance(result, StatusSchemaInvalid):
            rows.append({"path": path, "surface_id": surface_id, "status": "schema_invalid", "detail": result.detail})
        elif isinstance(result, StatusServiceUnavailable):
            rows.append(
                {"path": path, "surface_id": surface_id, "status": "service_unavailable", "detail": result.reason}
            )
        else:
            rows.append(
                {
                    "path": path,
                    "surface_id": surface_id,
                    "status": "client_error",
                    "detail": f"unrecognized result {type(result).__name__}",
                }
            )
    return rows


def _acquire_report(attempts: list[LeaseAttempt], holder_uuid: UUID, *, enforce: bool) -> dict[str, Any]:
    blocked = any(not attempt.acquired for attempt in attempts)
    return {
        "ok": not blocked,
        "blocked": blocked and enforce,
        "enforced": enforce,
        "holder_uuid": str(holder_uuid),
        "leases": [asdict(attempt) for attempt in attempts],
    }


def _cmd_acquire(args: argparse.Namespace) -> int:
    holder_uuid = UUID(args.holder_uuid) if args.holder_uuid else uuid4()
    client = make_client(base_url=args.base_url, bearer_token=args.bearer_token, timeout_s=args.timeout_s)
    attempts = acquire_paths(
        args.paths,
        client=client,
        holder_uuid=holder_uuid,
        ttl_s=args.ttl_s,
        intent=args.intent,
        audit_session=args.audit_session,
    )
    report = _acquire_report(attempts, holder_uuid, enforce=args.enforce)
    print(json.dumps(report, sort_keys=True))
    return 1 if report["blocked"] else 0


def _cmd_release(args: argparse.Namespace) -> int:
    client = make_client(base_url=args.base_url, bearer_token=args.bearer_token, timeout_s=args.timeout_s)
    print(json.dumps({"releases": release_leases(client, args.lease_id)}, sort_keys=True))
    return 0


def _cmd_status(args: argparse.Namespace) -> int:
    client = make_client(base_url=args.base_url, bearer_token=args.bearer_token, timeout_s=args.timeout_s)
    print(json.dumps({"surfaces": status_paths(args.paths, client=client)}, sort_keys=True))
    return 0


def _cmd_guard(args: argparse.Namespace) -> int:
    command = list(args.command)
    if command and command[0] == "--":
        command = command[1:]
    if not command:
        print("guard requires a command after --", file=sys.stderr)
        return 2

    paths = changed_paths(include_untracked=not args.no_untracked) if args.changed else args.paths
    holder_uuid = UUID(args.holder_uuid) if args.holder_uuid else uuid4()
    client = make_client(base_url=args.base_url, bearer_token=args.bearer_token, timeout_s=args.timeout_s)
    attempts = acquire_paths(
        paths,
        client=client,
        holder_uuid=holder_uuid,
        ttl_s=args.ttl_s,
        intent=args.intent or "file_lease guarded command",
        audit_session=args.audit_session,
    )
    report = _acquire_report(attempts, holder_uuid, enforce=not args.advisory)
    print(json.dumps(report, sort_keys=True), file=sys.stderr)

    lease_ids = [attempt.lease_id for attempt in attempts if attempt.lease_id]
    if report["blocked"]:
        if lease_ids:
            print(json.dumps({"releases": release_leases(client, lease_ids)}, sort_keys=True), file=sys.stderr)
        return 1

    try:
        completed = subprocess.run(command, check=False)
        return completed.returncode
    finally:
        if lease_ids:
            print(json.dumps({"releases": release_leases(client, lease_ids)}, sort_keys=True), file=sys.stderr)


def _cmd_hold(args: argparse.Namespace) -> int:
    paths = changed_paths(include_untracked=not args.no_untracked) if args.changed else args.paths
    holder_uuid = UUID(args.holder_uuid) if args.holder_uuid else uuid4()
    client = make_client(base_url=args.base_url, bearer_token=args.bearer_token, timeout_s=args.timeout_s)
    seen_paths = set(paths)
    attempts = acquire_paths(
        paths,
        client=client,
        holder_uuid=holder_uuid,
        ttl_s=args.ttl_s,
        intent=args.intent or "file_lease hold",
        audit_session=args.audit_session,
    )
    report = _acquire_report(attempts, holder_uuid, enforce=True)
    print(json.dumps(report, sort_keys=True), flush=True)

    lease_ids = [attempt.lease_id for attempt in attempts if attempt.lease_id]
    if report["blocked"]:
        if lease_ids:
            print(json.dumps({"releases": release_leases(client, lease_ids)}, sort_keys=True), file=sys.stderr)
        return 1

    refresh_changed = args.changed and not args.no_refresh_changed
    if not lease_ids and not refresh_changed:
        return 0

    stop = _StopFlag()
    exit_code = 0

    def _handle_signal(_signum: int, _frame: object) -> None:
        stop.stop = True

    old_int = signal.signal(signal.SIGINT, _handle_signal)
    old_term = signal.signal(signal.SIGTERM, _handle_signal)
    try:
        while not stop.stop:
            try:
                args.sleep_fn(args.heartbeat_interval_s)
            except KeyboardInterrupt:
                stop.stop = True
                break
            if refresh_changed:
                new_paths = [
                    path
                    for path in changed_paths(include_untracked=not args.no_untracked)
                    if path not in seen_paths
                ]
                if new_paths:
                    seen_paths.update(new_paths)
                    refresh_attempts = acquire_paths(
                        new_paths,
                        client=client,
                        holder_uuid=holder_uuid,
                        ttl_s=args.ttl_s,
                        intent=args.intent or "file_lease hold refresh",
                        audit_session=args.audit_session,
                    )
                    refresh_report = _acquire_report(refresh_attempts, holder_uuid, enforce=True)
                    print(json.dumps({"refresh": refresh_report}, sort_keys=True), file=sys.stderr, flush=True)
                    attempts.extend(refresh_attempts)
                    lease_ids = [attempt.lease_id for attempt in attempts if attempt.lease_id]
                    if refresh_report["blocked"]:
                        exit_code = 1
                        break
            if lease_ids:
                heartbeats = heartbeat_leases(client, lease_ids)
                print(json.dumps({"heartbeats": heartbeats}, sort_keys=True), file=sys.stderr, flush=True)
            if args.once:
                break
        return exit_code
    finally:
        signal.signal(signal.SIGINT, old_int)
        signal.signal(signal.SIGTERM, old_term)
        if lease_ids:
            print(json.dumps({"releases": release_leases(client, lease_ids)}, sort_keys=True), file=sys.stderr)


@dataclass
class _StopFlag:
    stop: bool = False


def _cmd_changed(args: argparse.Namespace) -> int:
    print(json.dumps({"paths": changed_paths(include_untracked=not args.no_untracked)}, sort_keys=True))
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="cmd", required=True)

    def add_client_args(p: argparse.ArgumentParser) -> None:
        p.add_argument("--base-url")
        p.add_argument("--bearer-token")
        p.add_argument("--timeout-s", type=float, default=2.0)

    acquire = sub.add_parser("acquire", help="Acquire leases for one or more paths.")
    add_client_args(acquire)
    acquire.add_argument("paths", nargs="+")
    acquire.add_argument("--ttl-s", type=int, default=DEFAULT_TTL_S)
    acquire.add_argument("--intent")
    acquire.add_argument("--audit-session")
    acquire.add_argument("--holder-uuid")
    acquire.add_argument("--enforce", action="store_true", help="Exit non-zero if any path is not acquired.")
    acquire.set_defaults(func=_cmd_acquire)

    guard = sub.add_parser("guard", help="Acquire paths, run a command, then release.")
    add_client_args(guard)
    guard_paths = guard.add_mutually_exclusive_group(required=True)
    guard_paths.add_argument("--path", dest="paths", action="append")
    guard_paths.add_argument("--changed", action="store_true", help="Guard all staged, unstaged, and untracked paths.")
    guard.add_argument("--no-untracked", action="store_true", help="With --changed, ignore untracked files.")
    guard.add_argument("--ttl-s", type=int, default=DEFAULT_TTL_S)
    guard.add_argument("--intent")
    guard.add_argument("--audit-session")
    guard.add_argument("--holder-uuid")
    guard.add_argument("--advisory", action="store_true", help="Run the command even when a lease is not acquired.")
    guard.add_argument("command", nargs=argparse.REMAINDER)
    guard.set_defaults(func=_cmd_guard)

    hold = sub.add_parser("hold", help="Acquire paths and heartbeat until interrupted.")
    add_client_args(hold)
    hold_paths = hold.add_mutually_exclusive_group(required=True)
    hold_paths.add_argument("--path", dest="paths", action="append")
    hold_paths.add_argument("--changed", action="store_true", help="Hold all staged, unstaged, and untracked paths.")
    hold.add_argument("--no-untracked", action="store_true", help="With --changed, ignore untracked files.")
    hold.add_argument("--no-refresh-changed", action="store_true", help="With --changed, snapshot paths once.")
    hold.add_argument("--ttl-s", type=int, default=DEFAULT_TTL_S)
    hold.add_argument("--heartbeat-interval-s", type=float, default=60.0)
    hold.add_argument("--intent")
    hold.add_argument("--audit-session")
    hold.add_argument("--holder-uuid")
    hold.add_argument("--once", action="store_true", help="Heartbeat once, release, and exit.")
    hold.set_defaults(func=_cmd_hold, sleep_fn=time.sleep)

    status = sub.add_parser("status", help="Show active lease status for paths.")
    add_client_args(status)
    status.add_argument("paths", nargs="+")
    status.set_defaults(func=_cmd_status)

    release = sub.add_parser("release", help="Release one or more lease IDs.")
    add_client_args(release)
    release.add_argument("--lease-id", action="append", required=True)
    release.set_defaults(func=_cmd_release)

    changed = sub.add_parser("changed", help="List paths that --changed would guard.")
    changed.add_argument("--no-untracked", action="store_true")
    changed.set_defaults(func=_cmd_changed)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
