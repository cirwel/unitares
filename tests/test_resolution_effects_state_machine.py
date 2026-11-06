"""
Lean 4 + Hypothesis State-Machine Refinement Verification for the UNITARES
Dialectic Resolution Effect Ownership, Quarantine, and Outcome Classification Protocol.

Pairs with `formal/ResolutionEffects.lean` to verify:
1. When `lean` is installed (including in the GitHub Actions `smoke` job), the
   Lean 4 kernel type-checks `formal/ResolutionEffects.lean` with zero `sorry`s.
2. The compile-time `#guard` trace scenarios (`effectScenario1..4` and
   `outcomeScenario5`, plus their intermediate step bindings) in
   `formal/ResolutionEffects.lean` are parsed directly from the `.lean` file
   and checked against both the Python spec mirror and the live `DialecticDB` /
   `classify_outcome` / `resolution_rate` implementation.
3. A Hypothesis `RuleBasedStateMachine` drives arbitrary sequences of:
   - Session resolution enqueueing (`resolve_session` with `explicitTrue`,
     `explicitFalse`, and `legacyUnbound` `paused_agent_state_json` bindings)
   - `claim_resolution_effect_async` (valid claims, concurrent competing claims,
     mismatched participant/token/payload claims, and session-snapshot drift)
   - `complete_resolution_effect_async` (matching and fenced `owner_token`s)
   - `fail_resolution_effect_async` (with and without `execution_result` payload)
   - `quarantine_resolution_effect_async` (matching and fenced `owner_token`s)
   - `acknowledge_resolution_effect_async` (exact and stale `expected_updated_at`)
   - Session analytics & outcome classification (`classify_outcome`,
     `resolution_rate`, and `DialecticDB.get_outcome_breakdown`) across all
     6 outcome classes (`canary`, `open`, `resolved_effect_pending`, `resolved`,
     `unresolved_awaiting_facilitation`, `failed`).
   In `@invariant`, the machine checks bisimulation between `SpecResolutionEffect`
   / `SpecOutcomeCounts` and the live `DialecticDB` + `classify_outcome` state;
   in each `@rule`, it asserts the corresponding theorem invariants (Theorems 1–11).
"""

from __future__ import annotations

import asyncio
import json
import re
import shutil
import subprocess
from contextlib import asynccontextmanager
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from unittest.mock import MagicMock, patch

import pytest
from hypothesis import settings, strategies as st
from hypothesis.stateful import RuleBasedStateMachine, initialize, invariant, rule

import src.dialectic_db as dialectic_db_module
from src.dialectic_db import (
    DialecticDB,
    acknowledge_resolution_effect_async,
    claim_resolution_effect_async,
    complete_resolution_effect_async,
    fail_resolution_effect_async,
    get_resolution_effect_async,
    quarantine_resolution_effect_async,
)
from src.dialectic_outcomes import (
    CANARY,
    FAILED,
    OPEN,
    RESOLVED,
    RESOLVED_EFFECT_PENDING,
    UNRESOLVED_AWAITING_FACILITATION,
    classify_outcome,
    resolution_rate,
)


REPO_ROOT = Path(__file__).resolve().parent.parent
LEAN_SPEC_PATH = REPO_ROOT / "formal" / "ResolutionEffects.lean"
LEAN_TOOLCHAIN_PATH = REPO_ROOT / "formal" / "lean-toolchain"

AGENT_UUIDS: Dict[int, str] = {
    i: f"00000000-0000-4000-8000-{i:012d}" for i in range(6)
}

OWNER_TOKENS: Dict[int, str] = {
    101: "owner-token-101",
    202: "owner-token-202",
    303: "owner-token-303",
}

PAUSE_TOKENS: Dict[int, str] = {
    10: "pause-tok-10",
    20: "pause-tok-20",
}

RESOLUTION_PAYLOADS: Dict[int, Dict[str, Any]] = {
    1: {
        "action": "resume",
        "conditions": ["calibrate_confidence"],
        "root_cause": "transient_drift_1",
        "reasoning": "agreed_on_remediation_1",
        "signature_a": "sig-paused-1",
        "signature_b": "sig-reviewer-1",
        "timestamp": "2026-11-05T12:00:00Z",
        "signature_version": 2,
    },
    2: {
        "action": "resume",
        "conditions": ["add_smoke_check", "monitor_coherence"],
        "root_cause": "transient_drift_2",
        "reasoning": "agreed_on_remediation_2",
        "signature_a": "sig-paused-2",
        "signature_b": "sig-reviewer-2",
        "timestamp": "2026-11-05T12:05:00Z",
        "signature_version": 2,
    },
}


def _assert_migration_075_effect_schema(eff_row: Dict[str, Any]) -> None:
    """Enforce the PostgreSQL table constraints from
    `db/postgres/migrations/075_dialectic_resolution_effects.sql:25-52` on every
    `INSERT INTO coordination.dialectic_resolution_effects` row."""
    if not eff_row.get("paused_agent_id"):
        raise ValueError(
            "075_dialectic_resolution_effects: paused_agent_id NOT NULL violated"
        )
    if not eff_row.get("reviewer_agent_id"):
        raise ValueError(
            "075_dialectic_resolution_effects: reviewer_agent_id NOT NULL violated"
        )
    eff_req = eff_row.get("effect_required")
    if not isinstance(eff_req, bool):
        raise ValueError(
            "075_dialectic_resolution_effects: effect_required BOOLEAN NOT NULL violated"
        )
    pause_tok = eff_row.get("pause_token")
    token_check_ok = (
        eff_req
        and isinstance(pause_tok, str)
        and pause_tok.strip() != ""
    ) or (not eff_req and pause_tok is None)
    if not token_check_ok:
        raise ValueError(
            f"075_dialectic_resolution_effects: effect_required/pause_token CHECK violated "
            f"(effect_required={eff_req!r}, pause_token={pause_tok!r})"
        )
    raw_payload = eff_row.get("resolution_payload_json")
    payload = (
        json.loads(raw_payload)
        if isinstance(raw_payload, str)
        else raw_payload
    )
    required_keys = {
        "action",
        "conditions",
        "root_cause",
        "reasoning",
        "signature_a",
        "signature_b",
        "timestamp",
        "signature_version",
    }
    payload_check_ok = (
        isinstance(payload, dict)
        and required_keys.issubset(payload.keys())
        and payload.get("action") == "resume"
        and isinstance(payload.get("conditions"), list)
        and isinstance(payload.get("root_cause"), str)
        and isinstance(payload.get("reasoning"), str)
        and isinstance(payload.get("signature_a"), str)
        and isinstance(payload.get("signature_b"), str)
        and isinstance(payload.get("timestamp"), str)
        and isinstance(payload.get("signature_version"), (int, float))
        and not isinstance(payload.get("signature_version"), bool)
    )
    if not payload_check_ok:
        raise ValueError(
            f"075_dialectic_resolution_effects: resolution_payload_json CHECK violated: {payload!r}"
        )

EXPECTED_THEOREMS = (
    "enqueue_non_required_immediately_applied",
    "claim_requires_pending_state",
    "single_claimant_mutual_exclusion",
    "owner_token_fencing_protects_applying_row",
    "unknown_partial_blocks_claim_and_worker_writes",
    "acknowledge_requires_unknown_partial_and_exact_updated_at",
    "applied_is_absorbing",
    "canary_always_classified_canary",
    "unapplied_effect_withholds_resolved_classification",
    "standing_rejection_three_valued_precedence",
    "non_quality_outcomes_preserve_resolution_rate_denominator",
)


def _find_lean_binary() -> Optional[str]:
    """Locate the Lean 4 compiler binary, preferring the direct toolchain binary
    under `~/.elan/toolchains/.../bin/lean` so read-only sandboxes never invoke
    the `elan` proxy's settings write."""
    elan_toolchains = Path.home() / ".elan" / "toolchains"
    if LEAN_TOOLCHAIN_PATH.exists() and elan_toolchains.is_dir():
        raw_tc = LEAN_TOOLCHAIN_PATH.read_text(encoding="utf-8").strip()
        sanitized = raw_tc.replace("/", "--").replace(":", "---")
        direct = elan_toolchains / sanitized / "bin" / "lean"
        if direct.is_file():
            return str(direct)
    which_lean = shutil.which("lean")
    if which_lean:
        return which_lean
    for candidate in (
        Path.home() / ".elan" / "bin" / "lean",
        Path("/opt/homebrew/bin/lean"),
        Path("/usr/local/bin/lean"),
    ):
        if candidate.exists():
            return str(candidate)
    return None


def _strip_lean_comments(source: str) -> str:
    """Strip `/-- ... -/`, `/- ... -/`, and `-- ...` comments from Lean source."""
    without_block = re.sub(r"/-.*?-/", "", source, flags=re.DOTALL)
    return re.sub(r"--[^\n]*", "", without_block)


def test_lean4_resolution_effects_spec_structure_and_zero_sorry():
    """Verify `formal/ResolutionEffects.lean` exists, has zero `sorry`/`admit`/`axiom`,
    and declares all 11 expected theorems."""
    assert LEAN_SPEC_PATH.exists(), f"Missing Lean 4 specification at {LEAN_SPEC_PATH}"
    source = LEAN_SPEC_PATH.read_text(encoding="utf-8")
    code_only = _strip_lean_comments(source)

    for forbidden in ("sorry", "admit", "axiom"):
        matches = re.findall(rf"\b{forbidden}\b", code_only)
        assert not matches, (
            f"Found forbidden `{forbidden}` token in {LEAN_SPEC_PATH}"
        )

    for thm in EXPECTED_THEOREMS:
        assert re.search(rf"\btheorem\s+{thm}\b", code_only), (
            f"Missing expected theorem `{thm}` in {LEAN_SPEC_PATH}"
        )


def test_lean4_kernel_typechecks_resolution_effects():
    """Run the Lean 4 kernel on `formal/ResolutionEffects.lean` when `lean` is installed."""
    lean_bin = _find_lean_binary()
    if not lean_bin:
        pytest.skip("Lean 4 binary not installed on this runner")

    proc = subprocess.run(
        [lean_bin, str(LEAN_SPEC_PATH)],
        cwd=str(REPO_ROOT),
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert proc.returncode == 0, (
        f"Lean 4 kernel rejected {LEAN_SPEC_PATH}:\n"
        f"STDOUT:\n{proc.stdout}\nSTDERR:\n{proc.stderr}"
    )


# ============================================================================
# Python Reference Mirror of `formal/ResolutionEffects.lean`
# ============================================================================


def spec_pause_binding_enqueue_effect_required(pause_binding: str) -> bool:
    """Mirror `PauseBinding.enqueueEffectRequired` (`src/dialectic_db.py:926-935`)."""
    return pause_binding == "explicitTrue"


def spec_pause_binding_is_legacy_unbound(pause_binding: str) -> bool:
    """Mirror `PauseBinding.isLegacyUnbound` (`src/dialectic_db.py:936-940`)."""
    return pause_binding == "legacyUnbound"


def spec_pause_binding_session_claim_coalesce(pause_binding: str) -> bool:
    """Mirror `PauseBinding.sessionClaimCoalesce` (`src/dialectic_db.py:1137-1141`).
    `COALESCE(s.paused_agent_state_json->'pause_effect_required' = 'true'::jsonb, TRUE)`
    evaluates to `True` both when the key is `'true'` and when the key is absent."""
    return pause_binding in ("explicitTrue", "legacyUnbound")


@dataclass(frozen=True)
class SpecEffectSessionSnapshot:
    """Exact Python mirror of `Unitares.ResolutionEffects.SessionSnapshot`."""

    status_resolved: bool = True
    paused_agent: int = 0
    reviewer: Optional[int] = 1
    resolution_payload: int = 1
    pause_token: Optional[int] = 10
    pause_binding: str = "explicitTrue"


@dataclass(frozen=True)
class SpecResolutionEffectRow:
    """Exact Python mirror of `Unitares.ResolutionEffects.ResolutionEffectRow`."""

    paused_agent: int = 0
    reviewer: Optional[int] = 1
    effect_required: bool = True
    pause_token: Optional[int] = 10
    resolution_payload: int = 1
    state: str = "pending"
    owner_token: Optional[int] = None
    attempt_count: int = 0
    last_error: Optional[str] = None
    has_execution_evidence: bool = False
    no_op_reason: Optional[str] = None
    manually_reconciled: bool = False
    reconciliation_reason: Optional[str] = None
    claimed_at: Optional[int] = None
    applied_at: Optional[int] = None
    uncertain_at: Optional[int] = None
    created_at: int = 100
    updated_at: int = 100


@dataclass(frozen=True)
class SpecClaimExpectations:
    """Exact Python mirror of `Unitares.ResolutionEffects.ClaimExpectations`."""

    paused_agent: int = 0
    reviewer: Optional[int] = 1
    effect_required: bool = True
    pause_token: Optional[int] = 10
    resolution_payload: int = 1


@dataclass(frozen=True)
class SpecClaimOutput:
    """Exact Python mirror of `Unitares.ResolutionEffects.ClaimOutput`."""

    row: SpecResolutionEffectRow
    claimed: bool


@dataclass(frozen=True)
class SpecEffectStepOutput:
    """Exact Python mirror of `Unitares.ResolutionEffects.EffectStepOutput`."""

    row: SpecResolutionEffectRow
    written: bool


def spec_enqueue_resolution_effect(
    s: SpecEffectSessionSnapshot, now: int
) -> SpecResolutionEffectRow:
    """Exact Python mirror of `Unitares.ResolutionEffects.enqueueResolutionEffect`."""
    req = spec_pause_binding_enqueue_effect_required(s.pause_binding)
    legacy = spec_pause_binding_is_legacy_unbound(s.pause_binding)
    return SpecResolutionEffectRow(
        paused_agent=s.paused_agent,
        reviewer=s.reviewer,
        effect_required=req,
        pause_token=s.pause_token,
        resolution_payload=s.resolution_payload,
        state="pending" if req else "applied",
        owner_token=None,
        attempt_count=0,
        last_error=None,
        has_execution_evidence=not req,
        no_op_reason=(
            None
            if req
            else ("legacy_unbound_session" if legacy else "active_at_session_open")
        ),
        manually_reconciled=False,
        reconciliation_reason=None,
        claimed_at=None,
        applied_at=None if req else now,
        uncertain_at=None,
        created_at=now,
        updated_at=now,
    )


def spec_strict_reviewer_eq(
    row_rev: Optional[int], exp_rev: Optional[int]
) -> bool:
    """Exact Python mirror of `Unitares.ResolutionEffects.strictReviewerEq`
    (`e.reviewer_agent_id = $4` in SQL: `NULL = NULL` is falsy)."""
    if row_rev is not None and exp_rev is not None:
        return row_rev == exp_rev
    return False


def spec_claim_guard_ok(
    eff: SpecResolutionEffectRow,
    sess: SpecEffectSessionSnapshot,
    exp: SpecClaimExpectations,
) -> bool:
    """Exact Python mirror of `Unitares.ResolutionEffects.claimGuardOk`."""
    return (
        eff.state == "pending"
        and eff.paused_agent == exp.paused_agent
        and spec_strict_reviewer_eq(eff.reviewer, exp.reviewer)
        and eff.resolution_payload == exp.resolution_payload
        and eff.pause_token == exp.pause_token
        and eff.effect_required == exp.effect_required
        and sess.status_resolved
        and sess.paused_agent == exp.paused_agent
        and spec_strict_reviewer_eq(sess.reviewer, exp.reviewer)
        and sess.resolution_payload == exp.resolution_payload
        and sess.pause_token == exp.pause_token
        and spec_pause_binding_session_claim_coalesce(sess.pause_binding)
        == exp.effect_required
    )


def spec_claim_resolution_effect(
    eff: SpecResolutionEffectRow,
    sess: SpecEffectSessionSnapshot,
    owner: int,
    exp: SpecClaimExpectations,
    now: int,
) -> SpecClaimOutput:
    """Exact Python mirror of `Unitares.ResolutionEffects.claimResolutionEffect`."""
    if spec_claim_guard_ok(eff, sess, exp):
        return SpecClaimOutput(
            row=replace(
                eff,
                state="applying",
                owner_token=owner,
                attempt_count=eff.attempt_count + 1,
                claimed_at=now,
                updated_at=now,
                last_error=None,
                uncertain_at=None,
            ),
            claimed=True,
        )
    return SpecClaimOutput(row=eff, claimed=False)


def spec_complete_resolution_effect(
    eff: SpecResolutionEffectRow, owner: int, now: int
) -> SpecEffectStepOutput:
    """Exact Python mirror of `Unitares.ResolutionEffects.completeResolutionEffect`."""
    if eff.state == "applying" and eff.owner_token == owner:
        return SpecEffectStepOutput(
            row=replace(
                eff,
                state="applied",
                has_execution_evidence=True,
                applied_at=now,
                updated_at=now,
                last_error=None,
                uncertain_at=None,
            ),
            written=True,
        )
    return SpecEffectStepOutput(row=eff, written=False)


def spec_fail_resolution_effect(
    eff: SpecResolutionEffectRow,
    owner: int,
    err: str,
    provide_execution_result: bool,
    now: int,
) -> SpecEffectStepOutput:
    """Exact Python mirror of `Unitares.ResolutionEffects.failResolutionEffect`."""
    if eff.state == "applying" and eff.owner_token == owner:
        return SpecEffectStepOutput(
            row=replace(
                eff,
                state="unknown_partial",
                last_error=err,
                has_execution_evidence=(
                    True if provide_execution_result else eff.has_execution_evidence
                ),
                uncertain_at=now,
                updated_at=now,
            ),
            written=True,
        )
    return SpecEffectStepOutput(row=eff, written=False)


def spec_quarantine_resolution_effect(
    eff: SpecResolutionEffectRow,
    owner: int,
    reason: str,
    now: int,
) -> SpecEffectStepOutput:
    """Exact Python mirror of `Unitares.ResolutionEffects.quarantineResolutionEffect`."""
    if eff.state == "applying" and eff.owner_token == owner:
        return SpecEffectStepOutput(
            row=replace(
                eff,
                state="unknown_partial",
                last_error=reason,
                uncertain_at=now,
                updated_at=now,
            ),
            written=True,
        )
    return SpecEffectStepOutput(row=eff, written=False)


def spec_acknowledge_resolution_effect(
    eff: SpecResolutionEffectRow,
    reason: str,
    expected_updated_at: Optional[int],
    now: int,
) -> SpecEffectStepOutput:
    """Exact Python mirror of `Unitares.ResolutionEffects.acknowledgeResolutionEffect`."""
    if eff.state == "unknown_partial" and expected_updated_at == eff.updated_at:
        return SpecEffectStepOutput(
            row=replace(
                eff,
                state="applied",
                has_execution_evidence=True,
                manually_reconciled=True,
                reconciliation_reason=reason,
                applied_at=now,
                updated_at=now,
            ),
            written=True,
        )
    return SpecEffectStepOutput(row=eff, written=False)


def spec_classify_outcome(
    status: str,
    awaiting_facilitation: bool,
    is_canary: bool,
    standing_rejection: Optional[bool],
    effect_state: Optional[str],
) -> str:
    """Exact Python mirror of `Unitares.ResolutionEffects.classifyOutcome`."""
    if is_canary:
        return CANARY
    if status not in ("resolved", "failed", "timeout", "abandoned"):
        return OPEN
    if status == "resolved" and effect_state in (
        "pending",
        "applying",
        "unknown_partial",
    ):
        return RESOLVED_EFFECT_PENDING
    if status == "resolved":
        return RESOLVED
    if standing_rejection is True:
        return UNRESOLVED_AWAITING_FACILITATION
    if standing_rejection is False:
        return FAILED
    if awaiting_facilitation:
        return UNRESOLVED_AWAITING_FACILITATION
    return FAILED


@dataclass(frozen=True)
class SpecOutcomeCounts:
    """Exact Python mirror of `Unitares.ResolutionEffects.OutcomeCounts`."""

    resolved: int = 0
    resolved_effect_pending: int = 0
    unresolved_awaiting_facilitation: int = 0
    failed: int = 0
    canary: int = 0
    open_count: int = 0


def spec_record_outcome(
    c: SpecOutcomeCounts, outcome: str
) -> SpecOutcomeCounts:
    """Exact Python mirror of `Unitares.ResolutionEffects.recordOutcome`."""
    if outcome == RESOLVED:
        return replace(c, resolved=c.resolved + 1)
    if outcome == RESOLVED_EFFECT_PENDING:
        return replace(c, resolved_effect_pending=c.resolved_effect_pending + 1)
    if outcome == UNRESOLVED_AWAITING_FACILITATION:
        return replace(
            c,
            unresolved_awaiting_facilitation=c.unresolved_awaiting_facilitation + 1,
        )
    if outcome == FAILED:
        return replace(c, failed=c.failed + 1)
    if outcome == CANARY:
        return replace(c, canary=c.canary + 1)
    if outcome == OPEN:
        return replace(c, open_count=c.open_count + 1)
    raise ValueError(f"Unknown outcome {outcome!r}")


def spec_rate_denominator(c: SpecOutcomeCounts) -> int:
    """Exact Python mirror of `Unitares.ResolutionEffects.rateDenominator`."""
    return c.resolved + c.failed


# ============================================================================
# In-Memory SQL Backend for Live `DialecticDB` Resolution Effect & Outcome Execution
# ============================================================================


def _int_to_ts(tick: int) -> datetime:
    return datetime(2026, 11, 5, 12, 0, 0, tzinfo=timezone.utc) + timedelta(
        seconds=tick
    )


def _ts_to_int(dt: Optional[datetime]) -> Optional[int]:
    if dt is None:
        return None
    base = datetime(2026, 11, 5, 12, 0, 0, tzinfo=timezone.utc)
    return int(round((dt - base).total_seconds()))


class _LiveResolutionEffectsSqlBackend:
    """Stateful PostgreSQL emulator for `core.dialectic_sessions`,
    `coordination.dialectic_resolution_effects`, `core.agents`, and
    `core.dialectic_messages` that intercepts `conn.fetchrow` and `conn.fetch`
    on a real `DialecticDB` instance so every call executes the production
    SQL generator, parameter binding, JSON decoding, and outcome classification.

    SQL Predicate Verification vs Direct Python Emulation:
    - For `DialecticDB.resolve_session`: asserts the presence of the `terminal`,
      `prepared`, and `enqueued` CTEs (`AND status NOT IN ('resolved', 'failed')`,
      `paused_agent_state_json ? 'pause_effect_required'`,
      `CASE WHEN effect_required THEN 'pending' ELSE 'applied' END`,
      `WHERE $1 = 'resolved'`) in the emitted SQL string and evaluates the
      `prepared` / `enqueued` logic against the session row's JSONB state.
    - For `DialecticDB.get_resolution_effect`: asserts
      `FROM coordination.dialectic_resolution_effects WHERE session_id = $1`.
    - For `DialecticDB.claim_resolution_effect`: asserts the presence of all 7
      effect predicates (`WHERE e.session_id = $1 AND e.state = 'pending' ...`)
      and the correlated `EXISTS ( SELECT 1 FROM core.dialectic_sessions s ...)`
      subquery predicates (including strict `=` on `reviewer_agent_id`,
      `IS NOT DISTINCT FROM` on `pause_token`, and
      `COALESCE(s.paused_agent_state_json->'pause_effect_required' = 'true'::jsonb, TRUE) = $7`),
      as well as the fallback `SELECT e.*, false AS claimed` read.
    - For `complete_resolution_effect`, `fail_resolution_effect`, and
      `quarantine_resolution_effect`: asserts the exact
      `WHERE session_id = $1 AND state = 'applying' AND owner_token = $2` fence.
    - For `acknowledge_resolution_effect`: asserts the exact
      `WHERE session_id = $1 AND state = 'unknown_partial' AND updated_at IS NOT DISTINCT FROM $3`
      guard and `COALESCE(execution_result_json, '{}'::jsonb) || ($2::text)::jsonb`
      additive JSONB merge.
    - For `get_outcome_breakdown`: asserts the `LEFT JOIN core.agents`,
      `LEFT JOIN coordination.dialectic_resolution_effects`, and
      `LEFT JOIN LATERAL` reviewer synthesis query with the three-valued
      `CASE WHEN r.agrees IS NULL THEN NULL ELSE (r.agrees IS FALSE) END AS standing_rejection`
      projection.
    """

    def __init__(self, session_id: str = "sess-effects-001") -> None:
        self.session_id = session_id
        self.clock_tick: int = 100
        self.sessions: Dict[str, Dict[str, Any]] = {}
        self.effects: Dict[str, Dict[str, Any]] = {}
        self.agents: Dict[str, Dict[str, Any]] = {}
        self.messages: List[Dict[str, Any]] = []

    def set_clock(self, tick: int) -> None:
        self.clock_tick = tick

    def seed_session(
        self,
        session_id: str,
        *,
        status: str = "active",
        phase: str = "synthesis",
        paused_agent_id: str = AGENT_UUIDS[0],
        reviewer_agent_id: Optional[str] = AGENT_UUIDS[1],
        pause_token: Optional[str] = PAUSE_TOKENS[10],
        pause_binding: str = "explicitTrue",
        resolution_json: Optional[Dict[str, Any]] = None,
        awaiting_facilitation: bool = False,
        paused_agent_label: Optional[str] = "worker_agent",
        latest_reviewer_synthesis_agrees: Optional[bool] = None,
    ) -> None:
        paused_state: Dict[str, Any] = {}
        if pause_token is not None:
            paused_state["pause_token"] = pause_token
        if pause_binding == "explicitTrue":
            paused_state["pause_effect_required"] = True
        elif pause_binding == "explicitFalse":
            paused_state["pause_effect_required"] = False
        elif pause_binding == "legacyUnbound":
            pass
        else:
            raise ValueError(f"Unknown pause_binding {pause_binding!r}")

        self.sessions[session_id] = {
            "session_id": session_id,
            "status": status,
            "phase": phase,
            "paused_agent_id": paused_agent_id,
            "reviewer_agent_id": reviewer_agent_id,
            "paused_agent_state_json": paused_state,
            "resolution_json": resolution_json,
            "awaiting_facilitation": awaiting_facilitation,
            "transcript_revision": 0,
            "resolution_saga_id": None,
            "resolution_saga_Revision": 0,
            "created_at": _int_to_ts(self.clock_tick),
            "updated_at": _int_to_ts(self.clock_tick),
        }
        if paused_agent_label is not None:
            self.agents[paused_agent_id] = {
                "id": paused_agent_id,
                "label": paused_agent_label,
            }
        else:
            self.agents.pop(paused_agent_id, None)

        # Remove any existing messages for this session and optionally seed a synthesis message
        self.messages = [
            m for m in self.messages if m["session_id"] != session_id
        ]
        if (
            latest_reviewer_synthesis_agrees is not None
            and reviewer_agent_id is not None
        ):
            self.messages.append(
                {
                    "message_id": len(self.messages) + 1,
                    "session_id": session_id,
                    "agent_id": reviewer_agent_id,
                    "message_type": "synthesis",
                    "agrees": latest_reviewer_synthesis_agrees,
                    "timestamp": _int_to_ts(self.clock_tick),
                }
            )

    def make_db(self) -> DialecticDB:
        conn = MagicMock()
        conn.fetchrow = self._fetchrow
        conn.fetch = self._fetch

        @asynccontextmanager
        async def _tx():
            yield

        conn.transaction = _tx

        @asynccontextmanager
        async def _acquire():
            yield conn

        pool = MagicMock()
        pool._closed = False
        pool.acquire = _acquire
        db = DialecticDB(pool=pool)
        db._initialized = True
        return db

    async def _fetch(self, sql: str, *args: Any) -> List[Dict[str, Any]]:
        norm = " ".join(sql.split())
        if "FROM core.dialectic_sessions s" in norm and "standing_rejection" in norm:
            assert (
                "LEFT JOIN core.agents a ON a.id = s.paused_agent_id" in norm
            ), f"get_outcome_breakdown missing LEFT JOIN core.agents: {norm}"
            assert (
                "LEFT JOIN coordination.dialectic_resolution_effects e ON e.session_id = s.session_id"
                in norm
            ), f"get_outcome_breakdown missing LEFT JOIN dialectic_resolution_effects: {norm}"
            assert (
                "CASE WHEN r.agrees IS NULL THEN NULL ELSE (r.agrees IS FALSE) END AS standing_rejection"
                in norm
            ), f"get_outcome_breakdown missing three-valued standing_rejection CASE: {norm}"
            assert (
                "WHERE dm.session_id = s.session_id AND dm.message_type = 'synthesis' AND dm.agent_id = s.reviewer_agent_id"
                in norm
            ), f"get_outcome_breakdown missing LATERAL synthesis filter: {norm}"
            assert (
                "WHERE s.created_at >= now() - interval '1 day' * $1" in norm
            ), f"get_outcome_breakdown missing window_days filter: {norm}"

            window_days = int(args[0])
            cutoff = _int_to_ts(self.clock_tick) - timedelta(days=window_days)
            out: List[Dict[str, Any]] = []
            for sid, sess in self.sessions.items():
                if sess["created_at"] < cutoff:
                    continue
                agent_row = self.agents.get(sess["paused_agent_id"])
                eff_row = self.effects.get(sid)
                synth_msgs = [
                    m
                    for m in self.messages
                    if m["session_id"] == sid
                    and m["message_type"] == "synthesis"
                    and m["agent_id"] == sess["reviewer_agent_id"]
                ]
                synth_msgs.sort(
                    key=lambda m: (m["timestamp"], m["message_id"]),
                    reverse=True,
                )
                r_agrees = synth_msgs[0]["agrees"] if synth_msgs else None
                standing_rejection = (
                    None if r_agrees is None else (r_agrees is False)
                )
                out.append(
                    {
                        "status": sess["status"],
                        "awaiting_facilitation": bool(
                            sess.get("awaiting_facilitation", False)
                        ),
                        "paused_agent_label": (
                            agent_row["label"] if agent_row else None
                        ),
                        "resolution_effect_state": (
                            eff_row["state"] if eff_row else None
                        ),
                        "standing_rejection": standing_rejection,
                    }
                )
            return out
        raise NotImplementedError(f"Unhandled SQL in _fetch: {norm}")

    async def _fetchrow(
        self, sql: str, *args: Any
    ) -> Optional[Dict[str, Any]]:
        norm = " ".join(sql.split())

        # 1. `resolve_session` CTE (`WITH terminal AS ... prepared AS ... enqueued AS ...`)
        if (
            "WITH terminal AS (" in norm
            and "INSERT INTO coordination.dialectic_resolution_effects" in norm
        ):
            assert (
                "AND status NOT IN ('resolved', 'failed')" in norm
            ), f"resolve_session missing terminal immutability guard: {norm}"
            assert (
                "paused_agent_state_json ? 'pause_effect_required'" in norm
            ), f"resolve_session missing pause_effect_required JSONB key check: {norm}"
            assert (
                "CASE WHEN effect_required THEN 'pending' ELSE 'applied' END"
                in norm
            ), f"resolve_session missing state CASE: {norm}"
            assert (
                "WHERE $1 = 'resolved'" in norm
            ), f"resolve_session enqueued CTE missing WHERE $1 = 'resolved': {norm}"

            req_status = args[0]
            req_phase = args[1]
            res_json_raw = args[2]
            sid = args[3]
            sess = self.sessions.get(sid)
            if sess is None or sess["status"] in ("resolved", "failed"):
                return None

            res_payload = (
                json.loads(res_json_raw)
                if isinstance(res_json_raw, str)
                else res_json_raw
            )
            now_dt = _int_to_ts(self.clock_tick)
            eff_candidate: Optional[Dict[str, Any]] = None

            if req_status == "resolved":
                pstate = sess.get("paused_agent_state_json") or {}
                has_key = "pause_effect_required" in pstate
                eff_req = (
                    bool(pstate.get("pause_effect_required") is True)
                    if has_key
                    else False
                )
                legacy_unbound = not has_key
                pause_tok = pstate.get("pause_token")
                eff_candidate = {
                    "session_id": sid,
                    "paused_agent_id": sess["paused_agent_id"],
                    "reviewer_agent_id": sess["reviewer_agent_id"],
                    "effect_required": eff_req,
                    "pause_token": pause_tok,
                    "resolution_payload_json": json.dumps(res_payload),
                    "state": "pending" if eff_req else "applied",
                    "owner_token": None,
                    "attempt_count": 0,
                    "last_error": None,
                    "execution_result_json": (
                        None
                        if eff_req
                        else json.dumps(
                            {
                                "success": True,
                                "no_op": True,
                                "reason": (
                                    "legacy_unbound_session"
                                    if legacy_unbound
                                    else "active_at_session_open"
                                ),
                            }
                        )
                    ),
                    "claimed_at": None,
                    "applied_at": None if eff_req else now_dt,
                    "uncertain_at": None,
                    "created_at": now_dt,
                    "updated_at": now_dt,
                }
                # Validate migration 075 schema BEFORE mutating `sess` so a CTE
                # INSERT failure aborts the entire statement atomically.
                _assert_migration_075_effect_schema(eff_candidate)

            sess["status"] = req_status
            sess["phase"] = req_phase
            sess["resolution_json"] = res_payload
            sess["awaiting_facilitation"] = False
            sess["updated_at"] = now_dt
            if eff_candidate is not None:
                self.effects[sid] = eff_candidate
            return {
                "session_id": sid,
                "effect_ts": now_dt.isoformat(),
            }

        # 2. `get_resolution_effect`
        if (
            norm.startswith("SELECT session_id, paused_agent_id, reviewer_agent_id,")
            and "FROM coordination.dialectic_resolution_effects WHERE session_id = $1"
            in norm
        ):
            sid = args[0]
            eff = self.effects.get(sid)
            return dict(eff) if eff is not None else None

        # 3. `claim_resolution_effect` statement 1 (`UPDATE ... RETURNING *, true AS claimed`)
        if (
            norm.startswith(
                "UPDATE coordination.dialectic_resolution_effects AS e"
            )
            and "RETURNING *, true AS claimed" in norm
        ):
            assert (
                "WHERE e.session_id = $1 AND e.state = 'pending'" in norm
            ), f"claim_resolution_effect missing pending state guard: {norm}"
            assert (
                "AND e.paused_agent_id = $3 AND e.reviewer_agent_id = $4" in norm
            ), f"claim_resolution_effect missing participant guards on e: {norm}"
            assert (
                "AND e.resolution_payload_json = ($5::text)::jsonb" in norm
                and "AND e.pause_token IS NOT DISTINCT FROM $6" in norm
                and "AND e.effect_required = $7" in norm
            ), f"claim_resolution_effect missing payload/token/effect_required guards on e: {norm}"
            assert (
                "AND s.status = 'resolved' AND s.phase = 'resolved'" in norm
                and "AND s.paused_agent_id = $3 AND s.reviewer_agent_id = $4"
                in norm
                and "AND s.resolution_json = ($5::text)::jsonb" in norm
                and "AND s.paused_agent_state_json->>'pause_token' IS NOT DISTINCT FROM $6"
                in norm
                and "COALESCE( s.paused_agent_state_json->'pause_effect_required' = 'true'::jsonb, TRUE ) = $7"
                in norm
            ), f"claim_resolution_effect missing EXISTS session guards: {norm}"

            (
                sid,
                owner_token,
                exp_paused,
                exp_reviewer,
                exp_res_raw,
                exp_pause_tok,
                exp_eff_req,
            ) = args
            eff = self.effects.get(sid)
            sess = self.sessions.get(sid)
            if eff is None or sess is None:
                return None

            exp_res_obj = json.loads(exp_res_raw)
            eff_res_obj = (
                json.loads(eff["resolution_payload_json"])
                if isinstance(eff["resolution_payload_json"], str)
                else eff["resolution_payload_json"]
            )
            sess_res_obj = sess.get("resolution_json")
            sess_pstate = sess.get("paused_agent_state_json") or {}
            sess_pause_tok = sess_pstate.get("pause_token")
            if "pause_effect_required" in sess_pstate:
                sess_coalesce_req = bool(
                    sess_pstate.get("pause_effect_required") is True
                )
            else:
                sess_coalesce_req = True

            # Strict SQL `=` on reviewer_agent_id (`NULL = NULL` is falsy)
            eff_rev_ok = (
                eff["reviewer_agent_id"] is not None
                and exp_reviewer is not None
                and eff["reviewer_agent_id"] == exp_reviewer
            )
            sess_rev_ok = (
                sess["reviewer_agent_id"] is not None
                and exp_reviewer is not None
                and sess["reviewer_agent_id"] == exp_reviewer
            )

            guard_ok = (
                eff["state"] == "pending"
                and eff["paused_agent_id"] == exp_paused
                and eff_rev_ok
                and eff_res_obj == exp_res_obj
                and eff["pause_token"] == exp_pause_tok
                and eff["effect_required"] == exp_eff_req
                and sess["status"] == "resolved"
                and sess["phase"] == "resolved"
                and sess["paused_agent_id"] == exp_paused
                and sess_rev_ok
                and sess_res_obj == exp_res_obj
                and sess_pause_tok == exp_pause_tok
                and sess_coalesce_req == exp_eff_req
            )
            if not guard_ok:
                return None

            now_dt = _int_to_ts(self.clock_tick)
            eff["state"] = "applying"
            eff["owner_token"] = owner_token
            eff["attempt_count"] = eff["attempt_count"] + 1
            eff["claimed_at"] = now_dt
            eff["updated_at"] = now_dt
            eff["last_error"] = None
            eff["uncertain_at"] = None
            res = dict(eff)
            res["claimed"] = True
            return res

        # 4. `claim_resolution_effect` statement 2 (`SELECT e.*, false AS claimed`)
        if (
            norm.startswith("SELECT e.*, false AS claimed")
            and "FROM coordination.dialectic_resolution_effects e WHERE e.session_id = $1"
            in norm
        ):
            sid = args[0]
            eff = self.effects.get(sid)
            if eff is None:
                return None
            res = dict(eff)
            res["claimed"] = False
            return res

        # 5. `complete_resolution_effect`
        if (
            norm.startswith(
                "UPDATE coordination.dialectic_resolution_effects SET state = 'applied', execution_result_json = ($3::text)::jsonb"
            )
        ):
            assert (
                "WHERE session_id = $1 AND state = 'applying' AND owner_token = $2"
                in norm
            ), f"complete_resolution_effect missing applying + owner_token fence: {norm}"
            sid, owner_token, exec_res_raw = args
            eff = self.effects.get(sid)
            if (
                eff is None
                or eff["state"] != "applying"
                or eff["owner_token"] != owner_token
            ):
                return None
            now_dt = _int_to_ts(self.clock_tick)
            eff["state"] = "applied"
            eff["execution_result_json"] = exec_res_raw
            eff["applied_at"] = now_dt
            eff["updated_at"] = now_dt
            eff["last_error"] = None
            eff["uncertain_at"] = None
            return {"session_id": sid}

        # 6. `fail_resolution_effect`
        if (
            norm.startswith(
                "UPDATE coordination.dialectic_resolution_effects SET state = 'unknown_partial', last_error = $3, execution_result_json = CASE"
            )
        ):
            assert (
                "WHERE session_id = $1 AND state = 'applying' AND owner_token = $2"
                in norm
            ), f"fail_resolution_effect missing applying + owner_token fence: {norm}"
            sid, owner_token, err_str, exec_res_raw = args
            eff = self.effects.get(sid)
            if (
                eff is None
                or eff["state"] != "applying"
                or eff["owner_token"] != owner_token
            ):
                return None
            now_dt = _int_to_ts(self.clock_tick)
            eff["state"] = "unknown_partial"
            eff["last_error"] = err_str
            if exec_res_raw is not None:
                eff["execution_result_json"] = exec_res_raw
            eff["uncertain_at"] = now_dt
            eff["updated_at"] = now_dt
            return {"session_id": sid}

        # 7. `quarantine_resolution_effect`
        if (
            norm.startswith(
                "UPDATE coordination.dialectic_resolution_effects SET state = 'unknown_partial', last_error = $3, uncertain_at = clock_timestamp()"
            )
        ):
            assert (
                "WHERE session_id = $1 AND state = 'applying' AND owner_token = $2"
                in norm
            ), f"quarantine_resolution_effect missing applying + owner_token fence: {norm}"
            sid, owner_token, reason = args
            eff = self.effects.get(sid)
            if (
                eff is None
                or eff["state"] != "applying"
                or eff["owner_token"] != owner_token
            ):
                return None
            now_dt = _int_to_ts(self.clock_tick)
            eff["state"] = "unknown_partial"
            eff["last_error"] = reason
            eff["uncertain_at"] = now_dt
            eff["updated_at"] = now_dt
            return {"session_id": sid}

        # 8. `acknowledge_resolution_effect`
        if (
            norm.startswith(
                "UPDATE coordination.dialectic_resolution_effects SET state = 'applied', execution_result_json = COALESCE(execution_result_json, '{}'::jsonb) || ($2::text)::jsonb"
            )
        ):
            assert (
                "WHERE session_id = $1 AND state = 'unknown_partial' AND updated_at IS NOT DISTINCT FROM $3"
                in norm
            ), f"acknowledge_resolution_effect missing unknown_partial + updated_at CAS guard: {norm}"
            sid, addon_raw, exp_updated_at = args
            eff = self.effects.get(sid)
            if (
                eff is None
                or eff["state"] != "unknown_partial"
                or eff["updated_at"] != exp_updated_at
            ):
                return None
            now_dt = _int_to_ts(self.clock_tick)
            existing_exec = eff.get("execution_result_json")
            if isinstance(existing_exec, str):
                existing_dict = json.loads(existing_exec)
            elif isinstance(existing_exec, dict):
                existing_dict = dict(existing_exec)
            else:
                existing_dict = {}
            addon_dict = json.loads(addon_raw)
            merged_dict = {**existing_dict, **addon_dict}
            eff["state"] = "applied"
            eff["execution_result_json"] = json.dumps(merged_dict)
            eff["applied_at"] = now_dt
            eff["updated_at"] = now_dt
            return {"session_id": sid}

        raise NotImplementedError(f"Unhandled SQL in _fetchrow: {norm}")


# ============================================================================
# Dynamic Parser & Conformance Verifier for Lean `#guard` Scenarios
# ============================================================================


def _parse_lean_opt_int(token: str) -> Optional[int]:
    token = token.strip()
    if token.startswith("(") and token.endswith(")"):
        token = token[1:-1].strip()
    if token == "none":
        return None
    m = re.match(r"^some\s+(\d+)$", token)
    if m:
        return int(m.group(1))
    raise ValueError(f"Cannot parse Lean Option Nat token: {token!r}")


def _parse_lean_opt_str(token: str) -> Optional[str]:
    token = token.strip()
    if token.startswith("(") and token.endswith(")"):
        token = token[1:-1].strip()
    if token == "none":
        return None
    m = re.match(r'^some\s+"([^"]*)"$', token)
    if m:
        return m.group(1)
    raise ValueError(f"Cannot parse Lean Option String token: {token!r}")


def _parse_lean_opt_bool(token: str) -> Optional[bool]:
    token = token.strip()
    if token.startswith("(") and token.endswith(")"):
        token = token[1:-1].strip()
    if token == "none":
        return None
    if token == "some true":
        return True
    if token == "some false":
        return False
    raise ValueError(f"Cannot parse Lean Option Bool token: {token!r}")


def _parse_lean_effect_state(token: str) -> str:
    mapping = {
        ".pending": "pending",
        ".applying": "applying",
        ".applied": "applied",
        ".unknownPartial": "unknown_partial",
    }
    return mapping[token.strip()]


def _parse_lean_opt_effect_state(token: str) -> Optional[str]:
    token = token.strip()
    if token.startswith("(") and token.endswith(")"):
        token = token[1:-1].strip()
    if token == "none":
        return None
    m = re.match(r"^some\s+(\.\w+)$", token)
    if m:
        return _parse_lean_effect_state(m.group(1))
    raise ValueError(f"Cannot parse Lean Option EffectState token: {token!r}")


def _parse_lean_analytics_status(token: str) -> str:
    mapping = {
        ".resolved": "resolved",
        ".failed": "failed",
        ".timeout": "timeout",
        ".abandoned": "abandoned",
        ".active": "active",
        ".escalated": "escalated",
        ".quorumVoting": "quorum_voting",
    }
    return mapping[token.strip()]


def _parse_lean_pause_binding(token: str) -> str:
    mapping = {
        ".explicitTrue": "explicitTrue",
        ".explicitFalse": "explicitFalse",
        ".legacyUnbound": "legacyUnbound",
    }
    return mapping[token.strip()]


def _parse_lean_resolution_effects_scenarios(
    source: str,
) -> Tuple[Dict[str, Any], List[Tuple[str, Optional[str], Any]]]:
    """Parse `effectScenario*` and `outcomeScenario5` definitions and `#guard`
    assertions directly from `formal/ResolutionEffects.lean`."""
    code = _strip_lean_comments(source)

    def_pattern = re.compile(
        r"def\s+((?:effectScenario|outcomeScenario)\w*)\s*:\s*(\w+)\s*:=\s*(.*?)(?=\n\s*(?:def\s+|#guard\s+|end\s+)|\Z)",
        re.DOTALL,
    )

    evaluated_spec: Dict[str, Any] = {}
    evaluated_live: Dict[str, Any] = {}

    for match in def_pattern.finditer(code):
        name, ret_type, body = (
            match.group(1),
            match.group(2),
            match.group(3).strip(),
        )
        lines = [ln.strip() for ln in body.splitlines() if ln.strip()]

        if ret_type == "OutcomeCounts":
            env_spec: Dict[str, SpecOutcomeCounts] = {}
            live_backend = _LiveResolutionEffectsSqlBackend()
            for idx, ln in enumerate(lines):
                if ln.startswith("let "):
                    m_init = re.match(
                        r"^let\s+(\w+)\s*:\s*OutcomeCounts\s*:=\s*\{\}$", ln
                    )
                    if m_init:
                        env_spec[m_init.group(1)] = SpecOutcomeCounts()
                        continue
                    m_rec = re.match(
                        r"^let\s+(\w+)\s*:=\s*recordOutcome\s+(\w+)\s+\(classifyOutcome\s+(\.\w+)\s+(true|false)\s+(true|false)\s+(none|\(some\s+(?:true|false)\))\s+(none|\(some\s+\.\w+\))\)$",
                        ln,
                    )
                    assert m_rec, f"Unrecognized OutcomeCounts line in {name}: {ln!r}"
                    dst, src_var, st_tok, fac_tok, can_tok, stand_tok, eff_tok = (
                        m_rec.groups()
                    )
                    st_str = _parse_lean_analytics_status(st_tok)
                    fac_b = fac_tok == "true"
                    can_b = can_tok == "true"
                    stand_opt = _parse_lean_opt_bool(stand_tok)
                    eff_opt = _parse_lean_opt_effect_state(eff_tok)

                    # Spec evaluation
                    spec_cls = spec_classify_outcome(
                        st_str, fac_b, can_b, stand_opt, eff_opt
                    )
                    env_spec[dst] = spec_record_outcome(
                        env_spec[src_var], spec_cls
                    )

                    # Live classify_outcome parity check
                    live_cls = classify_outcome(
                        st_str,
                        fac_b,
                        "canary_dialectic_probe" if can_b else "worker_agent",
                        standing_rejection=stand_opt,
                        resolution_effect_state=eff_opt,
                    )
                    assert live_cls == spec_cls

                    # Also seed into live_backend for get_outcome_breakdown check
                    sid = f"sess-outcome-{idx}"
                    live_backend.seed_session(
                        sid,
                        status=st_str,
                        phase="resolved" if st_str == "resolved" else "synthesis",
                        awaiting_facilitation=fac_b,
                        paused_agent_id=f"agent-{idx}",
                        paused_agent_label=(
                            "canary_dialectic_probe" if can_b else "worker_agent"
                        ),
                        latest_reviewer_synthesis_agrees=(
                            None if stand_opt is None else (not stand_opt)
                        ),
                    )
                    if eff_opt is not None:
                        live_backend.effects[sid] = {
                            "session_id": sid,
                            "state": eff_opt,
                        }
                else:
                    evaluated_spec[name] = env_spec[ln]
                    db = live_backend.make_db()
                    breakdown = asyncio.run(db.get_outcome_breakdown())
                    c_dict = breakdown["counts"]
                    evaluated_live[name] = SpecOutcomeCounts(
                        resolved=c_dict[RESOLVED],
                        resolved_effect_pending=c_dict[RESOLVED_EFFECT_PENDING],
                        unresolved_awaiting_facilitation=c_dict[
                            UNRESOLVED_AWAITING_FACILITATION
                        ],
                        failed=c_dict[FAILED],
                        canary=c_dict[CANARY],
                        open_count=c_dict[OPEN],
                    )
            continue

        # Effect scenario evaluation (both Spec and Live SQL Backend)
        env_s: Dict[str, Any] = dict(evaluated_spec)
        env_l: Dict[str, Any] = dict(evaluated_live)
        live_backend = _LiveResolutionEffectsSqlBackend("sess-guard")
        live_db = live_backend.make_db()

        def _sync_live_from_spec_row(row: SpecResolutionEffectRow) -> None:
            """Seed live_backend when an intermediate definition references an earlier scenario binding."""
            live_backend.seed_session(
                "sess-guard",
                status="resolved",
                phase="resolved",
                paused_agent_id=AGENT_UUIDS[row.paused_agent],
                reviewer_agent_id=(
                    AGENT_UUIDS[row.reviewer] if row.reviewer is not None else None
                ),
                pause_token=(
                    PAUSE_TOKENS[row.pause_token]
                    if row.pause_token is not None
                    else None
                ),
                pause_binding="explicitTrue" if row.effect_required else "legacyUnbound",
                resolution_json=RESOLUTION_PAYLOADS[row.resolution_payload],
            )
            live_backend.effects["sess-guard"] = {
                "session_id": "sess-guard",
                "paused_agent_id": AGENT_UUIDS[row.paused_agent],
                "reviewer_agent_id": (
                    AGENT_UUIDS[row.reviewer] if row.reviewer is not None else None
                ),
                "effect_required": row.effect_required,
                "pause_token": (
                    PAUSE_TOKENS[row.pause_token]
                    if row.pause_token is not None
                    else None
                ),
                "resolution_payload_json": json.dumps(
                    RESOLUTION_PAYLOADS[row.resolution_payload]
                ),
                "state": row.state,
                "owner_token": (
                    OWNER_TOKENS[row.owner_token]
                    if row.owner_token is not None
                    else None
                ),
                "attempt_count": row.attempt_count,
                "last_error": row.last_error,
                "execution_result_json": (
                    json.dumps({"ok": True}) if row.has_execution_evidence else None
                ),
                "claimed_at": _int_to_ts(row.claimed_at)
                if row.claimed_at is not None
                else None,
                "applied_at": _int_to_ts(row.applied_at)
                if row.applied_at is not None
                else None,
                "uncertain_at": _int_to_ts(row.uncertain_at)
                if row.uncertain_at is not None
                else None,
                "created_at": _int_to_ts(row.created_at),
                "updated_at": _int_to_ts(row.updated_at),
            }

        def _live_dict_to_spec_row(d: Dict[str, Any]) -> SpecResolutionEffectRow:
            rev_id = d["reviewer_agent_id"]
            rev_int = next(
                (k for k, v in AGENT_UUIDS.items() if v == rev_id), None
            )
            paused_int = next(
                k for k, v in AGENT_UUIDS.items() if v == d["paused_agent_id"]
            )
            ptok_int = next(
                (k for k, v in PAUSE_TOKENS.items() if v == d["pause_token"]),
                None,
            )
            otok_int = next(
                (k for k, v in OWNER_TOKENS.items() if v == d["owner_token"]),
                None,
            )
            res_payload_obj = d["resolution_payload_json"]
            if isinstance(res_payload_obj, str):
                res_payload_obj = json.loads(res_payload_obj)
            res_int = next(
                k for k, v in RESOLUTION_PAYLOADS.items() if v == res_payload_obj
            )
            exec_json = d.get("execution_result_json")
            if isinstance(exec_json, str):
                exec_json = json.loads(exec_json)
            has_ev = exec_json is not None
            no_op_reason = (
                exec_json.get("reason")
                if isinstance(exec_json, dict) and exec_json.get("no_op")
                else None
            )
            man_rec = bool(
                isinstance(exec_json, dict)
                and exec_json.get("manually_reconciled") is True
            )
            rec_reason = (
                exec_json.get("reconciliation_reason")
                if isinstance(exec_json, dict)
                else None
            )
            return SpecResolutionEffectRow(
                paused_agent=paused_int,
                reviewer=rev_int,
                effect_required=bool(d["effect_required"]),
                pause_token=ptok_int,
                resolution_payload=res_int,
                state=d["state"],
                owner_token=otok_int,
                attempt_count=d["attempt_count"],
                last_error=d["last_error"],
                has_execution_evidence=has_ev,
                no_op_reason=no_op_reason,
                manually_reconciled=man_rec,
                reconciliation_reason=rec_reason,
                claimed_at=_ts_to_int(d["claimed_at"]),
                applied_at=_ts_to_int(d["applied_at"]),
                uncertain_at=_ts_to_int(d["uncertain_at"]),
                created_at=_ts_to_int(d["created_at"]) or 100,
                updated_at=_ts_to_int(d["updated_at"]) or 100,
            )

        def _resolve_ref_spec(ref: str) -> Any:
            parts = ref.split(".")
            val = env_s[parts[0]]
            for p in parts[1:]:
                val = getattr(val, p)
            return val

        def _eval_rhs(rhs: str) -> Tuple[Any, Any]:
            if rhs in env_s or ("." in rhs and rhs.split(".")[0] in env_s):
                s_val = _resolve_ref_spec(rhs)
                parts = rhs.split(".")
                l_val = env_l[parts[0]]
                for p in parts[1:]:
                    l_val = getattr(l_val, p)
                return s_val, l_val

            m_sess = re.match(r"^mkInitEffectSession\s+(\.\w+)$", rhs)
            if m_sess:
                pb = _parse_lean_pause_binding(m_sess.group(1))
                ptok_int = (
                    10
                    if spec_pause_binding_enqueue_effect_required(pb)
                    else None
                )
                snap = SpecEffectSessionSnapshot(
                    pause_token=ptok_int, pause_binding=pb
                )
                live_backend.seed_session(
                    "sess-guard",
                    status="active",
                    phase="synthesis",
                    paused_agent_id=AGENT_UUIDS[0],
                    reviewer_agent_id=AGENT_UUIDS[1],
                    pause_token=(
                        PAUSE_TOKENS[ptok_int] if ptok_int is not None else None
                    ),
                    pause_binding=pb,
                )
                return snap, snap

            m_enq = re.match(r"^enqueueResolutionEffect\s+(\w+)\s+(\d+)$", rhs)
            if m_enq:
                sess_var, now_s = m_enq.group(1), int(m_enq.group(2))
                snap = env_s[sess_var]
                spec_row = spec_enqueue_resolution_effect(snap, now_s)
                live_backend.set_clock(now_s)
                ok = asyncio.run(
                    live_db.resolve_session(
                        "sess-guard", RESOLUTION_PAYLOADS[1], "resolved"
                    )
                )
                assert ok is True
                live_d = asyncio.run(
                    live_db.get_resolution_effect("sess-guard")
                )
                assert live_d is not None
                return spec_row, _live_dict_to_spec_row(live_d)

            m_claim = re.match(
                r"^claimResolutionEffect\s+([\w.]+)\s+(\w+)\s+(\d+)\s+defaultClaimExp\s+(\d+)$",
                rhs,
            )
            if m_claim:
                eff_ref, sess_var, owner_s, now_s = (
                    m_claim.group(1),
                    m_claim.group(2),
                    int(m_claim.group(3)),
                    int(m_claim.group(4)),
                )
                eff_in = _resolve_ref_spec(eff_ref)
                if "sess-guard" not in live_backend.effects:
                    _sync_live_from_spec_row(eff_in)
                spec_out = spec_claim_resolution_effect(
                    eff_in,
                    env_s[sess_var],
                    owner_s,
                    SpecClaimExpectations(),
                    now_s,
                )
                live_backend.set_clock(now_s)
                live_d = asyncio.run(
                    live_db.claim_resolution_effect(
                        "sess-guard",
                        OWNER_TOKENS[owner_s],
                        paused_agent_id=AGENT_UUIDS[0],
                        reviewer_agent_id=AGENT_UUIDS[1],
                        effect_required=True,
                        pause_token=PAUSE_TOKENS[10],
                        resolution=RESOLUTION_PAYLOADS[1],
                    )
                )
                assert live_d is not None
                live_out = SpecClaimOutput(
                    row=_live_dict_to_spec_row(live_d),
                    claimed=bool(live_d["claimed"]),
                )
                return spec_out, live_out

            m_comp = re.match(
                r"^completeResolutionEffect\s+([\w.]+)\s+(\d+)\s+(\d+)$", rhs
            )
            if m_comp:
                eff_ref, owner_s, now_s = (
                    m_comp.group(1),
                    int(m_comp.group(2)),
                    int(m_comp.group(3)),
                )
                eff_in = _resolve_ref_spec(eff_ref)
                if "sess-guard" not in live_backend.effects:
                    _sync_live_from_spec_row(eff_in)
                spec_out = spec_complete_resolution_effect(
                    eff_in, owner_s, now_s
                )
                live_backend.set_clock(now_s)
                written = asyncio.run(
                    live_db.complete_resolution_effect(
                        "sess-guard",
                        OWNER_TOKENS[owner_s],
                        {"success": True, "resumed": True},
                    )
                )
                live_d = asyncio.run(
                    live_db.get_resolution_effect("sess-guard")
                )
                assert live_d is not None
                live_out = SpecEffectStepOutput(
                    row=_live_dict_to_spec_row(live_d),
                    written=written,
                )
                return spec_out, live_out

            m_fail = re.match(
                r'^failResolutionEffect\s+([\w.]+)\s+(\d+)\s+"([^"]*)"\s+(true|false)\s+(\d+)$',
                rhs,
            )
            if m_fail:
                eff_ref, owner_s, err_s, prov_s, now_s = (
                    m_fail.group(1),
                    int(m_fail.group(2)),
                    m_fail.group(3),
                    m_fail.group(4) == "true",
                    int(m_fail.group(5)),
                )
                eff_in = _resolve_ref_spec(eff_ref)
                if "sess-guard" not in live_backend.effects:
                    _sync_live_from_spec_row(eff_in)
                spec_out = spec_fail_resolution_effect(
                    eff_in, owner_s, err_s, prov_s, now_s
                )
                live_backend.set_clock(now_s)
                written = asyncio.run(
                    live_db.fail_resolution_effect(
                        "sess-guard",
                        OWNER_TOKENS[owner_s],
                        err_s,
                        execution_result=(
                            {"partial": True} if prov_s else None
                        ),
                    )
                )
                live_d = asyncio.run(
                    live_db.get_resolution_effect("sess-guard")
                )
                assert live_d is not None
                live_out = SpecEffectStepOutput(
                    row=_live_dict_to_spec_row(live_d),
                    written=written,
                )
                return spec_out, live_out

            m_quar = re.match(
                r'^quarantineResolutionEffect\s+([\w.]+)\s+(\d+)\s+"([^"]*)"\s+(\d+)$',
                rhs,
            )
            if m_quar:
                eff_ref, owner_s, reason_s, now_s = (
                    m_quar.group(1),
                    int(m_quar.group(2)),
                    m_quar.group(3),
                    int(m_quar.group(4)),
                )
                eff_in = _resolve_ref_spec(eff_ref)
                if "sess-guard" not in live_backend.effects:
                    _sync_live_from_spec_row(eff_in)
                spec_out = spec_quarantine_resolution_effect(
                    eff_in, owner_s, reason_s, now_s
                )
                live_backend.set_clock(now_s)
                written = asyncio.run(
                    live_db.quarantine_resolution_effect(
                        "sess-guard",
                        OWNER_TOKENS[owner_s],
                        reason_s,
                    )
                )
                live_d = asyncio.run(
                    live_db.get_resolution_effect("sess-guard")
                )
                assert live_d is not None
                live_out = SpecEffectStepOutput(
                    row=_live_dict_to_spec_row(live_d),
                    written=written,
                )
                return spec_out, live_out

            m_ack = re.match(
                r'^acknowledgeResolutionEffect\s+([\w.]+)\s+"([^"]*)"\s+(none|\(some\s+\d+\))\s+(\d+)$',
                rhs,
            )
            if m_ack:
                eff_ref, reason_s, exp_upd_s, now_s = (
                    m_ack.group(1),
                    m_ack.group(2),
                    _parse_lean_opt_int(m_ack.group(3)),
                    int(m_ack.group(4)),
                )
                eff_in = _resolve_ref_spec(eff_ref)
                if "sess-guard" not in live_backend.effects:
                    _sync_live_from_spec_row(eff_in)
                spec_out = spec_acknowledge_resolution_effect(
                    eff_in, reason_s, exp_upd_s, now_s
                )
                live_backend.set_clock(now_s)
                written = asyncio.run(
                    live_db.acknowledge_resolution_effect(
                        "sess-guard",
                        reason_s,
                        expected_updated_at=(
                            _int_to_ts(exp_upd_s)
                            if exp_upd_s is not None
                            else None
                        ),
                    )
                )
                live_d = asyncio.run(
                    live_db.get_resolution_effect("sess-guard")
                )
                assert live_d is not None
                live_out = SpecEffectStepOutput(
                    row=_live_dict_to_spec_row(live_d),
                    written=written,
                )
                return spec_out, live_out

            raise ValueError(f"Unrecognized RHS in {name}: {rhs!r}")

        for ln in lines:
            if ln.startswith("let "):
                m_let = re.match(r"^let\s+(\w+)\s*:=\s*(.+)$", ln)
                assert m_let, f"Malformed let line in {name}: {ln!r}"
                var_name, rhs = m_let.group(1), m_let.group(2).strip()
                s_val, l_val = _eval_rhs(rhs)
                env_s[var_name] = s_val
                env_l[var_name] = l_val
            else:
                s_val, l_val = _eval_rhs(ln)
                evaluated_spec[name] = s_val
                evaluated_live[name] = l_val

    # Parse all `#guard` lines
    guards: List[Tuple[str, Optional[str], Any]] = []
    field_map = {
        "claimed": "claimed",
        "written": "written",
        "state": "state",
        "ownerToken": "owner_token",
        "attemptCount": "attempt_count",
        "hasExecutionEvidence": "has_execution_evidence",
        "appliedAt": "applied_at",
        "updatedAt": "updated_at",
        "manuallyReconciled": "manually_reconciled",
        "reconciliationReason": "reconciliation_reason",
        "lastError": "last_error",
        "uncertainAt": "uncertain_at",
        "effectRequired": "effect_required",
        "pauseToken": "pause_token",
        "noOpReason": "no_op_reason",
        "canary": "canary",
        "openCount": "open_count",
        "resolvedEffectPending": "resolved_effect_pending",
        "resolved": "resolved",
        "unresolvedAwaitingFacilitation": "unresolved_awaiting_facilitation",
        "failed": "failed",
    }

    for match in re.finditer(r"#guard\s+([^\n]+)", code):
        expr = match.group(1).strip()
        m_denom = re.match(r"^rateDenominator\s+(\w+)\s*==\s*(\d+)$", expr)
        if m_denom:
            guards.append(
                (m_denom.group(1), "rate_denominator", int(m_denom.group(2)))
            )
            continue

        m_eq = re.match(r"^([\w.]+)\s*==\s*(.+)$", expr)
        assert m_eq, f"Unrecognized #guard expression: {expr!r}"
        lhs, rhs_tok = m_eq.group(1), m_eq.group(2).strip()
        parts = lhs.split(".")
        scen_name = parts[0]
        attr_path = ".".join(field_map.get(p, p) for p in parts[1:])

        if rhs_tok in ("true", "false"):
            expected: Any = rhs_tok == "true"
        elif re.match(r"^\d+$", rhs_tok):
            expected = int(rhs_tok)
        elif rhs_tok.startswith("."):
            expected = _parse_lean_effect_state(rhs_tok)
        elif rhs_tok == "none" or rhs_tok.startswith("some "):
            if '"' in rhs_tok:
                expected = _parse_lean_opt_str(rhs_tok)
            else:
                expected = _parse_lean_opt_int(rhs_tok)
        else:
            raise ValueError(f"Unrecognized #guard RHS token: {rhs_tok!r}")

        guards.append((scen_name, attr_path, expected))

    return {"spec": evaluated_spec, "live": evaluated_live}, guards


def test_lean4_guard_scenarios_match_python_mirror_and_live_code():
    """Parse `effectScenario*` and `outcomeScenario5` and all `#guard` assertions
    directly from `formal/ResolutionEffects.lean` and verify that both the Python
    spec mirror and the live `DialecticDB` / `classify_outcome` implementation
    satisfy every `#guard` assertion."""
    source = LEAN_SPEC_PATH.read_text(encoding="utf-8")
    eval_maps, guards = _parse_lean_resolution_effects_scenarios(source)
    spec_map = eval_maps["spec"]
    live_map = eval_maps["live"]

    assert len(guards) >= 35, f"Expected at least 35 #guard assertions, found {len(guards)}"

    for scen_name, attr_path, expected in guards:
        s_obj = spec_map[scen_name]
        l_obj = live_map[scen_name]
        assert s_obj == l_obj, (
            f"Spec vs live mismatch on {scen_name}: spec={s_obj} vs live={l_obj}"
        )
        if attr_path == "rate_denominator":
            actual_s = spec_rate_denominator(s_obj)
            assert actual_s == expected, (
                f"#guard {scen_name} rateDenominator expected {expected}, got {actual_s}"
            )
        else:
            val = s_obj
            for part in attr_path.split("."):
                val = getattr(val, part)
            assert val == expected, (
                f"#guard {scen_name}.{attr_path} expected {expected!r}, got {val!r}"
            )


# ============================================================================
# Hypothesis Stateful Refinement Suite (`RuleBasedStateMachine`)
# ============================================================================


def test_migration_075_schema_constraints_enforced_by_sql_emulator():
    """Verify `_LiveResolutionEffectsSqlBackend` enforces all 3 constraints from
    `db/postgres/migrations/075_dialectic_resolution_effects.sql:25-52` on enqueue."""
    backend = _LiveResolutionEffectsSqlBackend("sess-075")
    db = backend.make_db()

    # 1. reviewer_agent_id NOT NULL violation
    backend.seed_session(
        "sess-075",
        reviewer_agent_id=None,
        pause_token=PAUSE_TOKENS[10],
        pause_binding="explicitTrue",
    )
    with pytest.raises(ValueError, match="reviewer_agent_id NOT NULL violated"):
        asyncio.run(
            db.resolve_session("sess-075", RESOLUTION_PAYLOADS[1], "resolved")
        )
    assert backend.sessions["sess-075"]["status"] == "active"
    assert "sess-075" not in backend.effects

    # 2a. effect_required=True with pause_token=None violates CHECK
    backend.seed_session(
        "sess-075",
        reviewer_agent_id=AGENT_UUIDS[1],
        pause_token=None,
        pause_binding="explicitTrue",
    )
    with pytest.raises(
        ValueError, match="effect_required/pause_token CHECK violated"
    ):
        asyncio.run(
            db.resolve_session("sess-075", RESOLUTION_PAYLOADS[1], "resolved")
        )
    assert backend.sessions["sess-075"]["status"] == "active"
    assert "sess-075" not in backend.effects

    # 2b. effect_required=False (legacyUnbound) with pause_token!=None violates CHECK
    backend.seed_session(
        "sess-075",
        reviewer_agent_id=AGENT_UUIDS[1],
        pause_token=PAUSE_TOKENS[10],
        pause_binding="legacyUnbound",
    )
    with pytest.raises(
        ValueError, match="effect_required/pause_token CHECK violated"
    ):
        asyncio.run(
            db.resolve_session("sess-075", RESOLUTION_PAYLOADS[1], "resolved")
        )
    assert backend.sessions["sess-075"]["status"] == "active"
    assert "sess-075" not in backend.effects

    # 3. resolution_payload_json missing required keys violates CHECK
    backend.seed_session(
        "sess-075",
        reviewer_agent_id=AGENT_UUIDS[1],
        pause_token=PAUSE_TOKENS[10],
        pause_binding="explicitTrue",
    )
    with pytest.raises(
        ValueError, match="resolution_payload_json CHECK violated"
    ):
        asyncio.run(
            db.resolve_session(
                "sess-075", {"action": "resume", "summary": "bad"}, "resolved"
            )
        )
    assert backend.sessions["sess-075"]["status"] == "active"
    assert "sess-075" not in backend.effects


@settings(
    max_examples=60,
    stateful_step_count=25,
    deadline=None,
)
class ResolutionEffectsStateMachine(RuleBasedStateMachine):
    """Hypothesis state machine driving `DialecticDB` resolution effect methods
    and `src/dialectic_outcomes.py` in lockstep with `SpecResolutionEffectRow`
    and `SpecOutcomeCounts`."""

    def __init__(self) -> None:
        super().__init__()
        self.session_id = "sess-hyp-effects-001"
        self.backend = _LiveResolutionEffectsSqlBackend(self.session_id)
        self.db = self.backend.make_db()
        self.analytics_backend = _LiveResolutionEffectsSqlBackend("sess-analytics")
        self.analytics_db = self.analytics_backend.make_db()
        self.tick = 100
        self.spec_session: Optional[SpecEffectSessionSnapshot] = None
        self.spec_effect: Optional[SpecResolutionEffectRow] = None
        self.spec_counts = SpecOutcomeCounts()
        self.outcome_session_counter = 0

    def _advance_clock(self) -> int:
        self.tick += 5
        self.backend.set_clock(self.tick)
        self.analytics_backend.set_clock(self.tick)
        return self.tick

    def _live_effect_to_spec(self, d: Dict[str, Any]) -> SpecResolutionEffectRow:
        rev_id = d["reviewer_agent_id"]
        rev_int = next((k for k, v in AGENT_UUIDS.items() if v == rev_id), None)
        paused_int = next(
            k for k, v in AGENT_UUIDS.items() if v == d["paused_agent_id"]
        )
        ptok_int = next(
            (k for k, v in PAUSE_TOKENS.items() if v == d["pause_token"]), None
        )
        otok_int = next(
            (k for k, v in OWNER_TOKENS.items() if v == d["owner_token"]), None
        )
        res_payload_obj = d["resolution_payload_json"]
        res_int = next(
            k for k, v in RESOLUTION_PAYLOADS.items() if v == res_payload_obj
        )
        exec_json = d.get("execution_result_json")
        has_ev = exec_json is not None
        no_op_reason = (
            exec_json.get("reason")
            if isinstance(exec_json, dict) and exec_json.get("no_op")
            else None
        )
        man_rec = bool(
            isinstance(exec_json, dict)
            and exec_json.get("manually_reconciled") is True
        )
        rec_reason = (
            exec_json.get("reconciliation_reason")
            if isinstance(exec_json, dict)
            else None
        )
        return SpecResolutionEffectRow(
            paused_agent=paused_int,
            reviewer=rev_int,
            effect_required=bool(d["effect_required"]),
            pause_token=ptok_int,
            resolution_payload=res_int,
            state=d["state"],
            owner_token=otok_int,
            attempt_count=d["attempt_count"],
            last_error=d["last_error"],
            has_execution_evidence=has_ev,
            no_op_reason=no_op_reason,
            manually_reconciled=man_rec,
            reconciliation_reason=rec_reason,
            claimed_at=_ts_to_int(d["claimed_at"]),
            applied_at=_ts_to_int(d["applied_at"]),
            uncertain_at=_ts_to_int(d["uncertain_at"]),
            created_at=_ts_to_int(d["created_at"]) or 100,
            updated_at=_ts_to_int(d["updated_at"]) or 100,
        )

    def _fetch_live_spec_row(self) -> SpecResolutionEffectRow:
        live_d = asyncio.run(self.db.get_resolution_effect(self.session_id))
        assert live_d is not None
        return self._live_effect_to_spec(live_d)

    @initialize(
        pause_binding=st.sampled_from(
            ["explicitTrue", "explicitFalse", "legacyUnbound"]
        ),
        active_pause_token=st.sampled_from([10, 20]),
        resolution_payload=st.sampled_from([1, 2]),
    )
    def init_resolved_session(
        self,
        pause_binding: str,
        active_pause_token: int,
        resolution_payload: int,
    ) -> None:
        now = self._advance_clock()
        pause_token: Optional[int] = (
            active_pause_token if pause_binding == "explicitTrue" else None
        )
        self.spec_session = SpecEffectSessionSnapshot(
            status_resolved=True,
            paused_agent=0,
            reviewer=1,
            resolution_payload=resolution_payload,
            pause_token=pause_token,
            pause_binding=pause_binding,
        )
        self.spec_effect = spec_enqueue_resolution_effect(
            self.spec_session, now
        )

        self.backend.seed_session(
            self.session_id,
            status="active",
            phase="synthesis",
            paused_agent_id=AGENT_UUIDS[0],
            reviewer_agent_id=AGENT_UUIDS[1],
            pause_token=(
                PAUSE_TOKENS[pause_token] if pause_token is not None else None
            ),
            pause_binding=pause_binding,
        )
        ok = asyncio.run(
            self.db.resolve_session(
                self.session_id,
                RESOLUTION_PAYLOADS[resolution_payload],
                "resolved",
            )
        )
        assert ok is True
        # Theorem 1: Non-required pause effect enqueues directly as `.applied` on live DB
        live_row = self._fetch_live_spec_row()
        if pause_binding in ("explicitFalse", "legacyUnbound"):
            assert live_row.state == "applied"
            assert live_row.effect_required is False
            assert live_row.pause_token is None
            assert live_row.has_execution_evidence is True
            assert live_row.applied_at == now
            assert live_row.no_op_reason == (
                "legacy_unbound_session"
                if pause_binding == "legacyUnbound"
                else "active_at_session_open"
            )

    @rule(
        pause_binding=st.sampled_from(
            ["explicitTrue", "explicitFalse", "legacyUnbound"]
        ),
        active_pause_token=st.sampled_from([10, 20]),
        resolution_payload=st.sampled_from([1, 2]),
    )
    def rule_reseed_resolved_session(
        self,
        pause_binding: str,
        active_pause_token: int,
        resolution_payload: int,
    ) -> None:
        """Reset the primary effect session with a schema-valid resolved session
        to explore fresh enqueue & claim paths."""
        now = self._advance_clock()
        pause_token: Optional[int] = (
            active_pause_token if pause_binding == "explicitTrue" else None
        )
        self.spec_session = SpecEffectSessionSnapshot(
            status_resolved=True,
            paused_agent=0,
            reviewer=1,
            resolution_payload=resolution_payload,
            pause_token=pause_token,
            pause_binding=pause_binding,
        )
        self.spec_effect = spec_enqueue_resolution_effect(
            self.spec_session, now
        )

        self.backend.seed_session(
            self.session_id,
            status="active",
            phase="synthesis",
            paused_agent_id=AGENT_UUIDS[0],
            reviewer_agent_id=AGENT_UUIDS[1],
            pause_token=(
                PAUSE_TOKENS[pause_token] if pause_token is not None else None
            ),
            pause_binding=pause_binding,
        )
        ok = asyncio.run(
            self.db.resolve_session(
                self.session_id,
                RESOLUTION_PAYLOADS[resolution_payload],
                "resolved",
            )
        )
        assert ok is True
        live_row = self._fetch_live_spec_row()
        if pause_binding in ("explicitFalse", "legacyUnbound"):
            assert live_row.state == "applied"
            assert live_row.effect_required is False
            assert live_row.pause_token is None
            assert live_row.has_execution_evidence is True
            assert live_row.applied_at == now
            assert live_row.no_op_reason == (
                "legacy_unbound_session"
                if pause_binding == "legacyUnbound"
                else "active_at_session_open"
            )

    @rule(
        owner=st.sampled_from([101, 202, 303]),
        match_expectations=st.booleans(),
        wrong_paused_agent=st.booleans(),
        wrong_reviewer=st.sampled_from(["match", "other", "null"]),
        wrong_pause_token=st.booleans(),
        wrong_effect_required=st.booleans(),
        wrong_payload=st.booleans(),
    )
    def rule_claim_resolution_effect(
        self,
        owner: int,
        match_expectations: bool,
        wrong_paused_agent: bool,
        wrong_reviewer: str,
        wrong_pause_token: bool,
        wrong_effect_required: bool,
        wrong_payload: bool,
    ) -> None:
        assert self.spec_effect is not None and self.spec_session is not None
        now = self._advance_clock()
        pre_eff = self.spec_effect

        if match_expectations:
            exp = SpecClaimExpectations(
                paused_agent=pre_eff.paused_agent,
                reviewer=pre_eff.reviewer,
                effect_required=pre_eff.effect_required,
                pause_token=pre_eff.pause_token,
                resolution_payload=pre_eff.resolution_payload,
            )
        else:
            if wrong_reviewer == "null":
                exp_rev: Optional[int] = None
            elif wrong_reviewer == "other":
                exp_rev = 3
            else:
                exp_rev = pre_eff.reviewer
            exp = SpecClaimExpectations(
                paused_agent=2 if wrong_paused_agent else pre_eff.paused_agent,
                reviewer=exp_rev,
                effect_required=(
                    (not pre_eff.effect_required)
                    if wrong_effect_required
                    else pre_eff.effect_required
                ),
                pause_token=20 if wrong_pause_token else pre_eff.pause_token,
                resolution_payload=(
                    2 if wrong_payload else pre_eff.resolution_payload
                ),
            )

        spec_out = spec_claim_resolution_effect(
            pre_eff, self.spec_session, owner, exp, now
        )
        self.spec_effect = spec_out.row

        with patch.object(
            dialectic_db_module,
            "get_dialectic_db",
            return_value=self.db,
        ):
            live_res = asyncio.run(
                claim_resolution_effect_async(
                    self.session_id,
                    OWNER_TOKENS[owner],
                    paused_agent_id=AGENT_UUIDS[exp.paused_agent],
                    reviewer_agent_id=(
                        AGENT_UUIDS[exp.reviewer]
                        if exp.reviewer is not None
                        else None  # type: ignore[arg-type]
                    ),
                    effect_required=exp.effect_required,
                    pause_token=(
                        PAUSE_TOKENS[exp.pause_token]
                        if exp.pause_token is not None
                        else None
                    ),
                    resolution=RESOLUTION_PAYLOADS[exp.resolution_payload],
                )
            )

        assert live_res is not None
        assert bool(live_res["claimed"]) == spec_out.claimed
        live_row = self._live_effect_to_spec(live_res)

        # Theorem 2 & 3: Claim requires `.pending`; subsequent claim against `.applying` loses on live DB
        if pre_eff.state != "pending":
            assert live_res["claimed"] is False
            assert live_row == pre_eff
        if pre_eff.state == "applying":
            assert live_row.state == "applying"
            assert live_row.owner_token == pre_eff.owner_token
            assert live_row.attempt_count == pre_eff.attempt_count

    @rule(
        status_resolved=st.booleans(),
        drift_paused_agent=st.booleans(),
        drift_reviewer=st.sampled_from(["match", "other", "null"]),
        drift_payload=st.booleans(),
        drift_pause_token=st.booleans(),
        drift_pause_binding=st.sampled_from(
            ["explicitTrue", "explicitFalse", "legacyUnbound"]
        ),
    )
    def rule_mutate_session_snapshot(
        self,
        status_resolved: bool,
        drift_paused_agent: bool,
        drift_reviewer: str,
        drift_payload: bool,
        drift_pause_token: bool,
        drift_pause_binding: str,
    ) -> None:
        """Simulate out-of-band session drift on `core.dialectic_sessions` to verify
        that `claim_resolution_effect`'s `EXISTS` subquery catches session-level drift
        across status, phase, participants (including nullable `s.reviewer_agent_id`),
        payload, pause_token, and pause_effect_required."""
        assert self.spec_session is not None and self.spec_effect is not None
        new_paused = 4 if drift_paused_agent else self.spec_effect.paused_agent
        if drift_reviewer == "null":
            new_rev: Optional[int] = None
        elif drift_reviewer == "other":
            new_rev = 5
        else:
            new_rev = self.spec_effect.reviewer
        new_payload = 2 if drift_payload else self.spec_effect.resolution_payload
        new_tok = 20 if drift_pause_token else self.spec_effect.pause_token
        self.spec_session = replace(
            self.spec_session,
            status_resolved=status_resolved,
            paused_agent=new_paused,
            reviewer=new_rev,
            resolution_payload=new_payload,
            pause_token=new_tok,
            pause_binding=drift_pause_binding,
        )
        sess = self.backend.sessions[self.session_id]
        sess["status"] = "resolved" if status_resolved else "failed"
        sess["phase"] = "resolved" if status_resolved else "failed"
        sess["paused_agent_id"] = AGENT_UUIDS[new_paused]
        sess["reviewer_agent_id"] = (
            AGENT_UUIDS[new_rev] if new_rev is not None else None
        )
        sess["resolution_json"] = RESOLUTION_PAYLOADS[new_payload]
        pstate: Dict[str, Any] = {}
        if new_tok is not None:
            pstate["pause_token"] = PAUSE_TOKENS[new_tok]
        if drift_pause_binding == "explicitTrue":
            pstate["pause_effect_required"] = True
        elif drift_pause_binding == "explicitFalse":
            pstate["pause_effect_required"] = False
        sess["paused_agent_state_json"] = pstate

    @rule(owner=st.sampled_from([101, 202, 303]))
    def rule_complete_resolution_effect(self, owner: int) -> None:
        assert self.spec_effect is not None
        now = self._advance_clock()
        pre_eff = self.spec_effect
        spec_out = spec_complete_resolution_effect(pre_eff, owner, now)
        self.spec_effect = spec_out.row

        with patch.object(
            dialectic_db_module,
            "get_dialectic_db",
            return_value=self.db,
        ):
            written = asyncio.run(
                complete_resolution_effect_async(
                    self.session_id,
                    OWNER_TOKENS[owner],
                    {"success": True, "resumed": True},
                )
            )
        assert written == spec_out.written
        live_row = self._fetch_live_spec_row()

        # Theorem 4, 5, 7: Owner-token fencing, unknown_partial block, applied absorbing on live DB
        if pre_eff.owner_token != owner or pre_eff.state != "applying":
            assert written is False
            assert live_row == pre_eff
        else:
            assert live_row.state == "applied"
            assert live_row.has_execution_evidence is True
            assert live_row.applied_at == now

    @rule(
        owner=st.sampled_from([101, 202, 303]),
        provide_result=st.booleans(),
        err_msg=st.sampled_from(["rpc_timeout", "partial_write_error"]),
    )
    def rule_fail_resolution_effect(
        self, owner: int, provide_result: bool, err_msg: str
    ) -> None:
        assert self.spec_effect is not None
        now = self._advance_clock()
        pre_eff = self.spec_effect
        spec_out = spec_fail_resolution_effect(
            pre_eff, owner, err_msg, provide_result, now
        )
        self.spec_effect = spec_out.row

        with patch.object(
            dialectic_db_module,
            "get_dialectic_db",
            return_value=self.db,
        ):
            written = asyncio.run(
                fail_resolution_effect_async(
                    self.session_id,
                    OWNER_TOKENS[owner],
                    err_msg,
                    execution_result=(
                        {"sub_effect_landed": True} if provide_result else None
                    ),
                )
            )
        assert written == spec_out.written
        live_row = self._fetch_live_spec_row()
        if written:
            assert live_row.state == "unknown_partial"
            assert live_row.last_error == err_msg
            assert live_row.uncertain_at == now
        else:
            assert live_row == pre_eff

    @rule(
        owner=st.sampled_from([101, 202, 303]),
        reason=st.sampled_from(["worker_stalled", "lease_abandoned"]),
    )
    def rule_quarantine_resolution_effect(
        self, owner: int, reason: str
    ) -> None:
        assert self.spec_effect is not None
        now = self._advance_clock()
        pre_eff = self.spec_effect
        spec_out = spec_quarantine_resolution_effect(
            pre_eff, owner, reason, now
        )
        self.spec_effect = spec_out.row

        with patch.object(
            dialectic_db_module,
            "get_dialectic_db",
            return_value=self.db,
        ):
            written = asyncio.run(
                quarantine_resolution_effect_async(
                    self.session_id,
                    OWNER_TOKENS[owner],
                    reason,
                )
            )
        assert written == spec_out.written
        live_row = self._fetch_live_spec_row()
        if written:
            assert live_row.state == "unknown_partial"
            assert live_row.last_error == reason
            assert live_row.uncertain_at == now
        else:
            assert live_row == pre_eff

    @rule(
        use_exact_updated_at=st.booleans(),
        null_updated_at=st.booleans(),
        reason=st.sampled_from(["verified_resumed", "manual_db_repair"]),
    )
    def rule_acknowledge_resolution_effect(
        self,
        use_exact_updated_at: bool,
        null_updated_at: bool,
        reason: str,
    ) -> None:
        assert self.spec_effect is not None
        now = self._advance_clock()
        pre_eff = self.spec_effect
        if null_updated_at:
            exp_upd: Optional[int] = None
        elif use_exact_updated_at:
            exp_upd = pre_eff.updated_at
        else:
            exp_upd = pre_eff.updated_at - 5

        spec_out = spec_acknowledge_resolution_effect(
            pre_eff, reason, exp_upd, now
        )
        self.spec_effect = spec_out.row

        with patch.object(
            dialectic_db_module,
            "get_dialectic_db",
            return_value=self.db,
        ):
            written = asyncio.run(
                acknowledge_resolution_effect_async(
                    self.session_id,
                    reason,
                    expected_updated_at=(
                        _int_to_ts(exp_upd) if exp_upd is not None else None
                    ),
                )
            )
        assert written == spec_out.written
        live_row = self._fetch_live_spec_row()

        # Theorem 6 & 7: Acknowledge requires `.unknownPartial` & exact `updatedAt`, preserving evidence on live DB
        if written:
            assert pre_eff.state == "unknown_partial"
            assert exp_upd == pre_eff.updated_at
            assert live_row.state == "applied"
            assert live_row.manually_reconciled is True
            assert live_row.reconciliation_reason == reason
            assert live_row.last_error == pre_eff.last_error
            assert live_row.uncertain_at == pre_eff.uncertain_at
            assert live_row.owner_token == pre_eff.owner_token
        else:
            assert live_row == pre_eff

    @rule(
        status=st.sampled_from(
            [
                "resolved",
                "failed",
                "timeout",
                "abandoned",
                "active",
                "escalated",
                "quorum_voting",
            ]
        ),
        awaiting_facilitation=st.booleans(),
        is_canary=st.booleans(),
        standing_rejection=st.sampled_from([None, True, False]),
        effect_state=st.sampled_from(
            [None, "pending", "applying", "applied", "unknown_partial"]
        ),
    )
    def rule_classify_and_record_outcome(
        self,
        status: str,
        awaiting_facilitation: bool,
        is_canary: bool,
        standing_rejection: Optional[bool],
        effect_state: Optional[str],
    ) -> None:
        """Verify Theorems 8–11 across `classify_outcome`, `resolution_rate`,
        and `DialecticDB.get_outcome_breakdown`."""
        pre_counts = self.spec_counts
        spec_cls = spec_classify_outcome(
            status,
            awaiting_facilitation,
            is_canary,
            standing_rejection,
            effect_state,
        )
        live_cls = classify_outcome(
            status,
            awaiting_facilitation,
            "canary_dialectic_probe" if is_canary else "worker_agent",
            standing_rejection=standing_rejection,
            resolution_effect_state=effect_state,
        )
        assert live_cls == spec_cls

        # Theorem 8: Canary traffic always classifies as `canary`
        if is_canary:
            assert live_cls == CANARY

        # Theorem 9: Unapplied effect withholds `resolved` classification
        if (
            not is_canary
            and status == "resolved"
            and effect_state in ("pending", "applying", "unknown_partial")
        ):
            assert live_cls == RESOLVED_EFFECT_PENDING

        # Theorem 10: Three-valued `standing_rejection` precedence over `awaiting_facilitation`
        if not is_canary and status == "failed":
            if standing_rejection is True:
                assert live_cls == UNRESOLVED_AWAITING_FACILITATION
            elif standing_rejection is False:
                assert live_cls == FAILED

        self.spec_counts = spec_record_outcome(pre_counts, spec_cls)

        # Seed into a dedicated analytics backend to verify `get_outcome_breakdown` SQL parity
        self.outcome_session_counter += 1
        sid = f"sess-analytics-{self.outcome_session_counter}"
        self.analytics_backend.seed_session(
            sid,
            status=status,
            phase="resolved" if status == "resolved" else "synthesis",
            paused_agent_id=f"analytics-agent-{self.outcome_session_counter}",
            reviewer_agent_id=AGENT_UUIDS[1],
            awaiting_facilitation=awaiting_facilitation,
            paused_agent_label=(
                "canary_dialectic_probe" if is_canary else "worker_agent"
            ),
            latest_reviewer_synthesis_agrees=(
                None
                if standing_rejection is None
                else (not standing_rejection)
            ),
        )
        if effect_state is not None:
            self.analytics_backend.effects[sid] = {
                "session_id": sid,
                "state": effect_state,
            }

        # Theorem 11: Non-quality outcomes preserve `resolution_rate` numerator & denominator on live DB
        if live_cls in (
            CANARY,
            OPEN,
            RESOLVED_EFFECT_PENDING,
            UNRESOLVED_AWAITING_FACILITATION,
        ):
            live_bd = asyncio.run(self.analytics_db.get_outcome_breakdown())
            assert live_bd["counts"][RESOLVED] == pre_counts.resolved
            assert live_bd["counts"][FAILED] == pre_counts.failed
            assert live_bd["resolution_rate_denominator"] == spec_rate_denominator(
                pre_counts
            )

    @invariant()
    def invariant_bisimulation_spec_vs_live(self) -> None:
        """Verify bisimulation between `self.spec_effect` and the live row in
        `coordination.dialectic_resolution_effects`, plus outcome breakdown parity."""
        if self.spec_effect is None:
            return
        with patch.object(
            dialectic_db_module,
            "get_dialectic_db",
            return_value=self.db,
        ):
            live_d = asyncio.run(get_resolution_effect_async(self.session_id))
        assert live_d is not None
        live_spec_row = self._live_effect_to_spec(live_d)
        assert live_spec_row == self.spec_effect

        # Also check `classify_outcome` on the primary session's live effect state
        assert self.spec_session is not None
        primary_status = (
            "resolved" if self.spec_session.status_resolved else "failed"
        )
        assert classify_outcome(
            primary_status,
            False,
            "worker_agent",
            standing_rejection=None,
            resolution_effect_state=live_d["state"],
        ) == spec_classify_outcome(
            primary_status,
            False,
            False,
            None,
            self.spec_effect.state,
        )

        # Check `DialecticDB.get_outcome_breakdown` and `resolution_rate` parity against `self.spec_counts`
        breakdown = asyncio.run(self.analytics_db.get_outcome_breakdown())
        assert breakdown["total_sessions"] == self.outcome_session_counter
        b_counts = breakdown["counts"]
        assert b_counts[RESOLVED] == self.spec_counts.resolved
        assert (
            b_counts[RESOLVED_EFFECT_PENDING]
            == self.spec_counts.resolved_effect_pending
        )
        assert (
            b_counts[UNRESOLVED_AWAITING_FACILITATION]
            == self.spec_counts.unresolved_awaiting_facilitation
        )
        assert b_counts[FAILED] == self.spec_counts.failed
        assert b_counts[CANARY] == self.spec_counts.canary
        assert b_counts[OPEN] == self.spec_counts.open_count

        denom = spec_rate_denominator(self.spec_counts)
        assert breakdown["resolution_rate_denominator"] == denom
        live_rate = resolution_rate(b_counts)
        assert breakdown["resolution_rate"] == live_rate
        if denom == 0:
            assert live_rate is None
        else:
            assert live_rate == pytest.approx(self.spec_counts.resolved / denom)


TestResolutionEffectsStateMachine = ResolutionEffectsStateMachine.TestCase
