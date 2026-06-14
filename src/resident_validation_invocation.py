"""Supervised local invocation layer for resident validation canaries.

This module is the first runtime wrapper above the stateful canary runner. It
adds a small file lease, per-run tick bounds, and a local invocation audit
stream. It deliberately does not submit UNITARES process updates, open GitHub
issues, request dialectic, deploy, merge, or roll back anything.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from src.resident_validation import ResidentProfile
from src.resident_validation_runner import build_canary_ticks

LOCAL_OUTPUTS = frozenset({"local_state_jsonl", "local_invocation_audit_jsonl"})
DEFAULT_ALLOWED_OUTPUTS = ("local_state_jsonl", "local_invocation_audit_jsonl")
FORBIDDEN_OUTPUTS = frozenset(
    {
        "unitares_process_update",
        "kg_note",
        "dialectic_request",
        "github_issue",
        "deploy",
        "merge",
        "force_push",
        "rollback",
    }
)
INVOCATION_EVENT_TYPE = "resident_validation_supervised_invocation"


class InvocationLockHeld(RuntimeError):
    """Raised when a live supervised invocation lock already exists."""


@dataclass(frozen=True)
class SupervisedInvocationPlan:
    """Local-only supervision settings for one resident canary invocation."""

    profile: ResidentProfile
    state_path: Path
    lock_path: Path
    audit_path: Path
    max_ticks_per_run: int = 1
    lock_ttl_seconds: int = 300
    allowed_outputs: tuple[str, ...] = field(default=DEFAULT_ALLOWED_OUTPUTS)

    def __post_init__(self) -> None:
        """Validate that this layer remains local-only and bounded."""
        if self.max_ticks_per_run <= 0:
            raise ValueError("max_ticks_per_run must be positive")
        if self.lock_ttl_seconds <= 0:
            raise ValueError("lock_ttl_seconds must be positive")
        outputs = set(self.allowed_outputs)
        if outputs != LOCAL_OUTPUTS or outputs & FORBIDDEN_OUTPUTS:
            raise ValueError(
                "resident validation supervised invocation is local-only; "
                "allowed_outputs must be local_state_jsonl and "
                "local_invocation_audit_jsonl"
            )


def _ensure_utc(dt: datetime | None) -> datetime:
    """Normalize optional datetimes to timezone-aware UTC."""
    if dt is None:
        return datetime.now(timezone.utc)
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _parse_timestamp(value: str) -> datetime:
    """Parse an ISO timestamp and normalize it to UTC."""
    return _ensure_utc(datetime.fromisoformat(value.replace("Z", "+00:00")))


def _read_lock(lock_path: Path) -> dict[str, Any] | None:
    """Read an invocation lock file if one exists."""
    if not lock_path.exists():
        return None
    return json.loads(lock_path.read_text(encoding="utf-8"))


def _lock_is_live(record: dict[str, Any], now: datetime) -> bool:
    """Return true when a lock record has not reached its expiry."""
    expires_at = record.get("expires_at")
    if not isinstance(expires_at, str):
        return True
    return _parse_timestamp(expires_at) > now


def acquire_invocation_lock(
    lock_path: Path,
    *,
    owner: str,
    now: datetime | None = None,
    ttl_seconds: int = 300,
) -> dict[str, Any]:
    """Acquire or refresh an expired local invocation lock."""
    if ttl_seconds <= 0:
        raise ValueError("ttl_seconds must be positive")
    if not owner.strip():
        raise ValueError("owner is required")
    observed_at = _ensure_utc(now)
    existing = _read_lock(lock_path)
    if existing is not None and _lock_is_live(existing, observed_at):
        raise InvocationLockHeld("resident validation invocation lock is held")

    record = {
        "event_type": "resident_validation_invocation_lock",
        "owner": owner,
        "acquired_at": observed_at.isoformat(),
        "expires_at": (observed_at + timedelta(seconds=ttl_seconds)).isoformat(),
    }
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    lock_path.write_text(json.dumps(record, sort_keys=True) + "\n", encoding="utf-8")
    return record


def release_invocation_lock(lock_path: Path, *, owner: str) -> None:
    """Release a local invocation lock when it is still owned by ``owner``."""
    record = _read_lock(lock_path)
    if record is None:
        return
    if record.get("owner") == owner:
        lock_path.unlink(missing_ok=True)


def _append_audit(audit_path: Path, event: dict[str, Any]) -> None:
    """Append one invocation event to a local JSONL audit stream."""
    audit_path.parent.mkdir(parents=True, exist_ok=True)
    with audit_path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(event, sort_keys=True) + "\n")


def _safe_summary(status: str) -> dict[str, str]:
    """Return the public non-sensitive invocation summary shape."""
    return {"event_type": INVOCATION_EVENT_TYPE, "status": status}


def run_supervised_canary_invocation(
    plan: SupervisedInvocationPlan,
    *,
    count: int,
    observation: str,
    prediction: str,
    confidence: float,
    now: datetime | None = None,
) -> dict[str, str]:
    """Run one bounded resident canary invocation under a local lock.

    Raw observations and predictions are written only to the configured local
    state stream by ``build_canary_ticks``. The returned summary is intentionally
    constant so cron/launchd logs do not become a private data surface.
    """
    if count <= 0:
        raise ValueError("count must be positive")
    if count > plan.max_ticks_per_run:
        raise ValueError("count exceeds max_ticks_per_run")

    observed_at = _ensure_utc(now)
    owner = f"{plan.profile.cohort_id}:{plan.profile.resident_id}"
    acquire_invocation_lock(
        plan.lock_path,
        owner=owner,
        now=observed_at,
        ttl_seconds=plan.lock_ttl_seconds,
    )
    try:
        ticks = build_canary_ticks(
            plan.profile,
            state_path=plan.state_path,
            count=count,
            observation=observation,
            prediction=prediction,
            confidence=confidence,
            now=observed_at,
        )
        audit_event = {
            "event_type": INVOCATION_EVENT_TYPE,
            "status": "state_appended",
            "cohort_id": plan.profile.cohort_id,
            "resident": {
                "id": plan.profile.resident_id,
                "role": plan.profile.role,
            },
            "observed_at": observed_at.isoformat(),
            "tick_count": len(ticks),
            "first_tick_index": ticks[0]["tick_index"],
            "last_tick_index": ticks[-1]["tick_index"],
            "authority": {
                "allowed_outputs": list(plan.allowed_outputs),
                "write_authority": False,
            },
        }
        _append_audit(plan.audit_path, audit_event)
        return _safe_summary("state_appended")
    finally:
        release_invocation_lock(plan.lock_path, owner=owner)
