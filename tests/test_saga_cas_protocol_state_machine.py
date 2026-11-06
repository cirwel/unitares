"""
Lean 4 + Hypothesis State-Machine Refinement Verification for the UNITARES
Dual-Writer Compare-And-Set (CAS), Saga Exclusion, and Sweeper Accounting Protocol.

Pairs with `formal/SagaCasProtocol.lean` to verify:
1. When `lean` is installed (including in the GitHub Actions `smoke` job), the
   Lean 4 kernel type-checks `formal/SagaCasProtocol.lean` with zero `sorry`s.
2. The compile-time `#guard` trace scenarios (`sagaScenario1..3` and
   `sagaScenario4Counts`) in `formal/SagaCasProtocol.lean` are parsed directly
   from the `.lean` file and checked against both the Python spec mirror and
   the live `DialecticDB` / `resolve_session_async` / `add_message_async` /
   `_attempt_guarded_write` / `_probe_write_overlap` implementation.
3. A Hypothesis `RuleBasedStateMachine` drives arbitrary sequences of:
   - `add_message_async` (with CAS expectations and `reopen_failed_facilitation`)
   - Concurrent message bump invalidating a stale `resolve_session_async` CAS
   - Saga creation and state transitions (`reserved`, `paused_agent_applied`,
     `both_agents_applied`, `reverting`, `pg_committed`, `reverted`)
   - `resolve_session_async` (both raw caller and sweeper `winner`-bound caller)
   - `_attempt_guarded_write` + `_probe_write_overlap` (healthy, overlap-detected,
     and probe-outage paths)
   In `@invariant`, the machine checks bisimulation between `SpecSessionRow` /
   `SpecCycleCounts` and the live `DialecticDB` + `_CycleCounts` state; in each
   `@rule`, it asserts the corresponding theorem invariants (Theorems 1–8).
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
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from hypothesis import settings, strategies as st
from hypothesis.stateful import RuleBasedStateMachine, initialize, invariant, rule

import src.dialectic_db as dialectic_db_module
import src.dialectic_session_writes as sw
from src.dialectic_db import (
    DialecticDB,
    _UNSET,
    add_message_async,
    has_inflight_saga_async,
    probe_saga_since_async,
    resolve_session_async,
)
from src.dialectic_reviewed_work import (
    REVIEWED_WORK_AUTHORSHIP_STATE_KEY,
    declaration_for_request,
    embed_reviewed_work_authorship,
)
from src.mcp_handlers.dialectic.auto_resolve import (
    _CycleCounts,
    _attempt_guarded_write,
    _probe_write_overlap,
)


REPO_ROOT = Path(__file__).resolve().parent.parent
LEAN_SPEC_PATH = REPO_ROOT / "formal" / "SagaCasProtocol.lean"
LEAN_TOOLCHAIN_PATH = REPO_ROOT / "formal" / "lean-toolchain"

AGENT_UUIDS: Dict[int, str] = {
    i: f"00000000-0000-4000-8000-{i:012d}" for i in range(6)
}

INFLIGHT_SAGA_STATES = frozenset(
    {"reserved", "paused_agent_applied", "both_agents_applied", "reverting"}
)
ALL_SAGA_STATES = (
    "reserved",
    "paused_agent_applied",
    "both_agents_applied",
    "reverting",
    "pg_committed",
    "reverted",
)

EXPECTED_THEOREMS = (
    "terminal_write_immutability",
    "sweeper_idempotent_replay_not_counted_as_written",
    "terminal_write_always_clears_facilitation",
    "inflight_saga_blocks_excluded_resolve",
    "stale_transcript_revision_blocks_resolve",
    "addMessage_bumps_transcript_revision",
    "concurrent_message_bump_invalidates_stale_cas",
    "pause_bound_self_review_blocks_resolved_not_failed",
    "unhealthy_probe_returns_probe_failed",
    "time_correlated_probe_catches_window_saga",
    "cycle_counts_conservation",
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


def test_lean4_saga_cas_spec_structure_and_zero_sorry():
    """Verify `formal/SagaCasProtocol.lean` exists, has zero `sorry`/`admit`/`axiom`,
    and declares all expected theorems."""
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


def test_lean4_kernel_typechecks_saga_cas_protocol():
    """Run the Lean 4 kernel on `formal/SagaCasProtocol.lean` when `lean` is installed."""
    lean_bin = _find_lean_binary()
    if not lean_bin:
        pytest.skip("Lean 4 binary not installed on this runner")

    proc = subprocess.run(
        [lean_bin, str(LEAN_SPEC_PATH)],
        cwd=str(REPO_ROOT),
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert proc.returncode == 0, (
        f"`lean {LEAN_SPEC_PATH.name}` failed (exit {proc.returncode}):\n"
        f"STDOUT:\n{proc.stdout}\nSTDERR:\n{proc.stderr}"
    )


# ---------------------------------------------------------------------------
# Python Reference Mirror of `formal/SagaCasProtocol.lean`
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class SpecSagaRecord:
    saga_id: int
    state: str
    created_at: int

    @property
    def is_inflight(self) -> bool:
        return self.state in INFLIGHT_SAGA_STATES


@dataclass(frozen=True)
class SpecSessionRow:
    status: str = "active"
    phase: str = "synthesis"
    paused_agent: int = 0
    reviewer: Optional[int] = 1
    transcript_revision: int = 0
    resolution_saga_revision: int = 0
    awaiting_facilitation: bool = False
    updated_at: int = 0
    pause_effect_required: bool = True
    declared_authors: Tuple[int, ...] = ()
    authorship_valid: bool = True
    sagas: Tuple[SpecSagaRecord, ...] = ()

    @property
    def is_terminal(self) -> bool:
        return self.status in ("resolved", "failed")


@dataclass(frozen=True)
class SpecCasExpectations:
    expected_phase: Optional[str] = None
    expected_updated_at: Optional[int] = None
    expected_reviewer_set: bool = False
    expected_reviewer: Optional[int] = None
    expected_transcript_revision: Optional[int] = None
    expected_awaiting_facilitation: Optional[bool] = None
    expected_resolution_saga_revision: Optional[int] = None
    exclude_inflight_saga: bool = False
    clear_facilitation: bool = False


@dataclass(frozen=True)
class SpecResolveOutput:
    state: SpecSessionRow
    written: bool
    idempotent_replay: bool
    raw_result: bool
    sweeper_written: bool
    winner_status: Optional[str]
    refused_reason: Optional[str]


@dataclass(frozen=True)
class SpecAddMessageOutput:
    state: SpecSessionRow
    written: bool
    new_revision: Optional[int]


@dataclass(frozen=True)
class SpecCycleCounts:
    write_attempt_count: int = 0
    write_succeeded_count: int = 0
    skipped_count: int = 0
    write_error_count: int = 0
    overlap_clean_count: int = 0
    overlap_detected_count: int = 0
    overlap_probe_failed_count: int = 0

    def is_balanced(self) -> bool:
        return (
            self.write_attempt_count
            == self.write_succeeded_count + self.skipped_count + self.write_error_count
            and self.write_succeeded_count
            == self.overlap_clean_count
            + self.overlap_detected_count
            + self.overlap_probe_failed_count
        )


def spec_has_inflight_saga(sagas: Tuple[SpecSagaRecord, ...]) -> bool:
    return any(sg.is_inflight for sg in sagas)


def spec_probe_saga_since(
    sagas: Tuple[SpecSagaRecord, ...],
    since: Optional[int],
    probe_healthy: bool,
) -> Optional[Dict[str, Any]]:
    if not probe_healthy:
        return None
    matched = [
        sg
        for sg in sagas
        if sg.is_inflight or (since is not None and sg.created_at >= since)
    ]
    if not matched:
        return {}
    newest = max(matched, key=lambda sg: sg.created_at)
    return {
        "saga_id": newest.saga_id,
        "state": newest.state,
        "created_at": newest.created_at,
    }


def spec_probe_write_overlap(probe_result: Optional[Dict[str, Any]]) -> str:
    if not isinstance(probe_result, dict):
        return "probe_failed"
    if not probe_result:
        return "clean"
    return "detected"


def spec_cas_matches(s: SpecSessionRow, cas: SpecCasExpectations) -> bool:
    if cas.expected_phase is not None and s.phase != cas.expected_phase:
        return False
    if cas.expected_updated_at is not None and s.updated_at != cas.expected_updated_at:
        return False
    if cas.expected_reviewer_set and s.reviewer != cas.expected_reviewer:
        return False
    if (
        cas.expected_transcript_revision is not None
        and s.transcript_revision != cas.expected_transcript_revision
    ):
        return False
    if (
        cas.expected_awaiting_facilitation is not None
        and s.awaiting_facilitation != cas.expected_awaiting_facilitation
    ):
        return False
    if (
        cas.expected_resolution_saga_revision is not None
        and s.resolution_saga_revision != cas.expected_resolution_saga_revision
    ):
        return False
    return True


def spec_resolve_authority_ok(s: SpecSessionRow) -> bool:
    not_self_pause_bound = not (
        s.pause_effect_required and s.reviewer == s.paused_agent
    )
    if s.reviewer is None:
        author_ok = s.authorship_valid and len(s.declared_authors) == 0
    else:
        author_ok = s.authorship_valid and (
            s.reviewer == s.paused_agent or s.reviewer not in s.declared_authors
        )
    return not_self_pause_bound and author_ok


def spec_resolve_guard_ok(
    s: SpecSessionRow, target_status: str, cas: SpecCasExpectations
) -> bool:
    return (
        not s.is_terminal
        and spec_cas_matches(s, cas)
        and not (cas.exclude_inflight_saga and spec_has_inflight_saga(s.sagas))
        and (target_status != "resolved" or spec_resolve_authority_ok(s))
    )


def spec_compute_refusal_reason(
    s: SpecSessionRow, target_status: str
) -> Optional[str]:
    if s.is_terminal or target_status != "resolved":
        return None
    if s.pause_effect_required and s.reviewer == s.paused_agent:
        return "self_review_not_authorizing"
    if not s.authorship_valid:
        return "invalid_review_authorship"
    if (
        s.reviewer is not None
        and s.reviewer != s.paused_agent
        and s.reviewer in s.declared_authors
    ):
        return "reviewer_recused"
    return None


def spec_resolve_session(
    s: SpecSessionRow,
    target_status: str,
    cas: SpecCasExpectations,
    now: int,
) -> SpecResolveOutput:
    if spec_resolve_guard_ok(s, target_status, cas):
        next_phase = "resolved" if target_status == "resolved" else target_status
        next_state = replace(
            s,
            status=target_status,
            phase=next_phase,
            awaiting_facilitation=False,
            updated_at=now,
        )
        return SpecResolveOutput(
            state=next_state,
            written=True,
            idempotent_replay=False,
            raw_result=True,
            sweeper_written=True,
            winner_status=None,
            refused_reason=None,
        )
    if s.status == target_status:
        return SpecResolveOutput(
            state=s,
            written=False,
            idempotent_replay=True,
            raw_result=True,
            sweeper_written=False,
            winner_status=s.status,
            refused_reason=None,
        )
    return SpecResolveOutput(
        state=s,
        written=False,
        idempotent_replay=False,
        raw_result=False,
        sweeper_written=False,
        winner_status=s.status,
        refused_reason=spec_compute_refusal_reason(s, target_status),
    )


def spec_add_message(
    s: SpecSessionRow,
    cas: SpecCasExpectations,
    next_phase: Optional[str],
    next_reviewer: Optional[int],
    next_awaiting_facilitation: Optional[bool],
    next_status: Optional[str],
    reopen_failed_facilitation: bool,
    now: int,
) -> SpecAddMessageOutput:
    if reopen_failed_facilitation:
        status_ok = s.status == "failed" and s.awaiting_facilitation is True
    else:
        status_ok = s.status == "active"
    cas_ok = spec_cas_matches(s, cas)
    next_rev_auth_ok = (
        True
        if next_reviewer is None
        else (
            s.authorship_valid
            and (
                next_reviewer == s.paused_agent
                or next_reviewer not in s.declared_authors
            )
        )
    )
    if status_ok and cas_ok and next_rev_auth_ok:
        new_rev = s.transcript_revision + 1
        s_next = replace(
            s,
            updated_at=now,
            transcript_revision=new_rev,
            phase=next_phase if next_phase is not None else s.phase,
            reviewer=next_reviewer if next_reviewer is not None else s.reviewer,
            awaiting_facilitation=(
                next_awaiting_facilitation
                if next_awaiting_facilitation is not None
                else s.awaiting_facilitation
            ),
            status=next_status if next_status is not None else s.status,
        )
        return SpecAddMessageOutput(state=s_next, written=True, new_revision=new_rev)
    return SpecAddMessageOutput(state=s, written=False, new_revision=None)


def spec_attempt_guarded_write(
    c: SpecCycleCounts,
    kind: str,
    probe_result: Optional[Dict[str, Any]] = None,
) -> SpecCycleCounts:
    c1 = replace(c, write_attempt_count=c.write_attempt_count + 1)
    if kind == "error":
        return replace(c1, write_error_count=c1.write_error_count + 1)
    if kind == "refused":
        return replace(c1, skipped_count=c1.skipped_count + 1)
    assert kind == "succeeded"
    c2 = replace(c1, write_succeeded_count=c1.write_succeeded_count + 1)
    ov = spec_probe_write_overlap(probe_result)
    if ov == "clean":
        return replace(c2, overlap_clean_count=c2.overlap_clean_count + 1)
    if ov == "detected":
        return replace(c2, overlap_detected_count=c2.overlap_detected_count + 1)
    return replace(c2, overlap_probe_failed_count=c2.overlap_probe_failed_count + 1)


# ---------------------------------------------------------------------------
# Stateful SQL-Evaluating Backend Driving Real `DialecticDB` Methods
# ---------------------------------------------------------------------------


BASE_TIME = datetime(2026, 11, 5, 12, 0, 0, tzinfo=timezone.utc)


def _ts_from_ticks(ticks: int) -> datetime:
    return BASE_TIME + timedelta(seconds=ticks)


def _ticks_from_ts(dt: datetime) -> int:
    return int(round((dt - BASE_TIME).total_seconds()))


class _LiveSessionSqlBackend:
    """In-memory SQL backend that backs a real `DialecticDB` instance.

    Instead of mocking `DialecticDB.resolve_session`, `DialecticDB.add_message`,
    `DialecticDB.probe_saga_since`, or `DialecticDB.probe_inflight_saga`, this
    backend intercepts `conn.fetchrow(sql, *args)`, parses the CAS/saga SQL
    predicates and positional `$N` parameters produced by `DialecticDB`, and
    models the JSONB reviewer-authority `WHERE` clause in `_eval_reviewer_authority`
    after asserting the expected SQL authority clause fragments on `sql`.
    """

    def __init__(self, init_spec: SpecSessionRow):
        self.session_id = "sess-saga-cas-001"
        self.clock_ticks = init_spec.updated_at
        self.probe_healthy = True
        self._msg_seq = 0
        self.sagas: List[Dict[str, Any]] = [
            {
                "saga_id": f"sg-{sg.saga_id}",
                "state": sg.state,
                "created_at": _ts_from_ticks(sg.created_at),
                "pg_committed_at": (
                    _ts_from_ticks(sg.created_at)
                    if sg.state == "pg_committed"
                    else None
                ),
                "reverted_at": (
                    _ts_from_ticks(sg.created_at)
                    if sg.state == "reverted"
                    else None
                ),
            }
            for sg in init_spec.sagas
        ]

        paused_uuid = AGENT_UUIDS[init_spec.paused_agent]
        rev_uuid = (
            AGENT_UUIDS[init_spec.reviewer]
            if init_spec.reviewer is not None
            else None
        )
        base_state: Dict[str, Any] = {
            "pause_effect_required": init_spec.pause_effect_required,
            "pause_token": "tok-001",
        }
        if init_spec.authorship_valid:
            decl = declaration_for_request(
                [AGENT_UUIDS[i] for i in init_spec.declared_authors],
                declared_by_agent_id=paused_uuid,
            )
            paused_state = embed_reviewed_work_authorship(base_state, decl)
        else:
            paused_state = {
                **base_state,
                REVIEWED_WORK_AUTHORSHIP_STATE_KEY: {"schema": "broken"},
            }

        self.row: Dict[str, Any] = {
            "session_id": self.session_id,
            "status": init_spec.status,
            "phase": init_spec.phase,
            "paused_agent_id": paused_uuid,
            "reviewer_agent_id": rev_uuid,
            "transcript_revision": init_spec.transcript_revision,
            "resolution_saga_revision": init_spec.resolution_saga_revision,
            "awaiting_facilitation": init_spec.awaiting_facilitation,
            "updated_at": _ts_from_ticks(init_spec.updated_at),
            "paused_agent_state_json": paused_state,
            "resolution_json": None,
        }

        self.pool = MagicMock()
        self.pool._closed = False
        self.conn = AsyncMock()
        self.conn.fetchrow = AsyncMock(side_effect=self._fetchrow)

        @asynccontextmanager
        async def _tx():
            yield

        self.conn.transaction = _tx
        acm = AsyncMock()
        acm.__aenter__ = AsyncMock(return_value=self.conn)
        acm.__aexit__ = AsyncMock(return_value=False)
        self.pool.acquire.return_value = acm

        self.db = DialecticDB(pool=self.pool)
        self.db._initialized = True

    def sync_sagas_from_spec(self, sagas: Tuple[SpecSagaRecord, ...]) -> None:
        self.sagas = [
            {
                "saga_id": f"sg-{sg.saga_id}",
                "state": sg.state,
                "created_at": _ts_from_ticks(sg.created_at),
                "pg_committed_at": (
                    _ts_from_ticks(sg.created_at)
                    if sg.state == "pg_committed"
                    else None
                ),
                "reverted_at": (
                    _ts_from_ticks(sg.created_at)
                    if sg.state == "reverted"
                    else None
                ),
            }
            for sg in sagas
        ]

    def _has_inflight_saga(self) -> bool:
        return any(sg["state"] in INFLIGHT_SAGA_STATES for sg in self.sagas)

    def _eval_reviewer_authority(self, candidate_reviewer: Optional[str]) -> bool:
        paused_state = self.row.get("paused_agent_state_json")
        if (
            paused_state is None
            or REVIEWED_WORK_AUTHORSHIP_STATE_KEY not in paused_state
        ):
            return True
        auth = dialectic_db_module.read_reviewed_work_authorship(self.row)
        if auth.is_invalid:
            return False
        if candidate_reviewer is None:
            # In PostgreSQL (`src/dialectic_db.py:901-906`),
            # `reviewer_agent_id = paused_agent_id OR NOT (authors ? reviewer_agent_id)`
            # evaluates to NULL (falsy in WHERE) when reviewer_agent_id is NULL
            # and `_dialectic_reviewed_work_authorship` is present.
            return False
        return (
            candidate_reviewer == self.row["paused_agent_id"]
            or candidate_reviewer not in auth.author_agent_ids
        )

    async def _fetchrow(self, sql: str, *args: Any) -> Optional[Dict[str, Any]]:
        norm = " ".join(sql.split())

        # 1. `DialecticDB.probe_inflight_saga`
        if norm.startswith("SELECT 1 FROM coordination.session_resolution_sagas"):
            if not self.probe_healthy:
                raise RuntimeError("simulated saga table read failure")
            assert args[0] == self.session_id
            return {"?column?": 1} if self._has_inflight_saga() else None

        # 2. `DialecticDB.probe_saga_since`
        if norm.startswith("SELECT saga_id, state, created_at, pg_committed_at, reverted_at"):
            assert (
                "OR ($2::timestamptz IS NOT NULL AND created_at >= $2::timestamptz)"
                in norm
            )
            if not self.probe_healthy:
                raise RuntimeError("simulated probe_saga_since outage")
            session_id, since = args[0], args[1]
            assert session_id == self.session_id
            matched = [
                sg
                for sg in self.sagas
                if sg["state"] in INFLIGHT_SAGA_STATES
                or (since is not None and sg["created_at"] >= since)
            ]
            if not matched:
                return None
            matched.sort(key=lambda sg: sg["created_at"], reverse=True)
            return dict(matched[0])

        # 3. `DialecticDB.resolve_session` (`WITH terminal AS (UPDATE core.dialectic_sessions ...)`)
        if "WITH terminal AS ( UPDATE core.dialectic_sessions" in norm:
            assert "awaiting_facilitation = false" in norm
            target_status, target_phase, res_json, session_id = (
                args[0],
                args[1],
                args[2],
                args[3],
            )
            assert session_id == self.session_id

            if self.row["status"] in ("resolved", "failed"):
                return None

            # Evaluate CAS predicates bound in the SQL query
            m_phase = re.search(r"phase IS NOT DISTINCT FROM \$(\d+)", norm)
            if m_phase and self.row["phase"] != args[int(m_phase.group(1)) - 1]:
                return None

            m_upd = re.search(r"updated_at IS NOT DISTINCT FROM \$(\d+)", norm)
            if m_upd and self.row["updated_at"] != args[int(m_upd.group(1)) - 1]:
                return None

            m_rev = re.search(
                r"reviewer_agent_id IS NOT DISTINCT FROM \$(\d+)", norm
            )
            if m_rev and self.row["reviewer_agent_id"] != args[int(m_rev.group(1)) - 1]:
                return None

            m_tr = re.search(r"transcript_revision = \$(\d+)", norm)
            if (
                m_tr
                and self.row["transcript_revision"] != args[int(m_tr.group(1)) - 1]
            ):
                return None

            m_fac = re.search(
                r"awaiting_facilitation IS NOT DISTINCT FROM \$(\d+)", norm
            )
            if (
                m_fac
                and self.row["awaiting_facilitation"]
                != args[int(m_fac.group(1)) - 1]
            ):
                return None

            m_srev = re.search(r"resolution_saga_revision = \$(\d+)", norm)
            if (
                m_srev
                and self.row["resolution_saga_revision"]
                != args[int(m_srev.group(1)) - 1]
            ):
                return None

            if (
                "NOT EXISTS ( SELECT 1 FROM coordination.session_resolution_sagas"
                in norm
                and self._has_inflight_saga()
            ):
                return None

            if target_status == "resolved":
                assert "pause_effect_required" in norm
                assert (
                    f"NOT (paused_agent_state_json ? '{REVIEWED_WORK_AUTHORSHIP_STATE_KEY}')"
                    in norm
                )
                assert (
                    "AND ( reviewer_agent_id = paused_agent_id OR NOT ("
                    in norm
                )
                assert (
                    f"'{{"
                    f"{REVIEWED_WORK_AUTHORSHIP_STATE_KEY},reviewed_work_author_agent_ids}}' ? reviewer_agent_id"
                    in norm
                )
                paused_state = self.row.get("paused_agent_state_json") or {}
                if (
                    paused_state.get("pause_effect_required") is True
                    and self.row["reviewer_agent_id"] == self.row["paused_agent_id"]
                ):
                    return None
                if not self._eval_reviewer_authority(self.row["reviewer_agent_id"]):
                    return None

            effect_ts = _ts_from_ticks(self.clock_ticks)
            self.row["status"] = target_status
            self.row["phase"] = target_phase
            self.row["resolution_json"] = json.loads(res_json)
            self.row["awaiting_facilitation"] = False
            self.row["updated_at"] = effect_ts
            return {"session_id": self.session_id, "effect_ts": effect_ts}

        # 4. `DialecticDB.resolve_session` follow-up SELECT on refusal / idempotent replay
        if (
            "SELECT status, phase, paused_agent_id, reviewer_agent_id," in norm
            and "FROM core.dialectic_sessions WHERE session_id = $1" in norm
        ):
            return dict(self.row)

        # 5. `DialecticDB.add_message` session bump UPDATE
        if (
            "UPDATE core.dialectic_sessions SET updated_at = now(), transcript_revision = transcript_revision + 1"
            in norm
        ):
            if "status = 'failed' AND awaiting_facilitation = true" in norm:
                if not (
                    self.row["status"] == "failed"
                    and self.row["awaiting_facilitation"] is True
                ):
                    return None
            else:
                if self.row["status"] in ("resolved", "failed", "escalated"):
                    return None

            where_part = norm.split("WHERE", 1)[1]
            set_part = norm.split("WHERE", 1)[0]

            m_phase = re.search(r"phase IS NOT DISTINCT FROM \$(\d+)", where_part)
            if m_phase and self.row["phase"] != args[int(m_phase.group(1)) - 1]:
                return None

            m_rev = re.search(
                r"reviewer_agent_id IS NOT DISTINCT FROM \$(\d+)", where_part
            )
            if m_rev and self.row["reviewer_agent_id"] != args[int(m_rev.group(1)) - 1]:
                return None

            m_tr = re.search(r"transcript_revision = \$(\d+)", where_part)
            if (
                m_tr
                and self.row["transcript_revision"] != args[int(m_tr.group(1)) - 1]
            ):
                return None

            m_fac = re.search(
                r"awaiting_facilitation IS NOT DISTINCT FROM \$(\d+)", where_part
            )
            if (
                m_fac
                and self.row["awaiting_facilitation"]
                != args[int(m_fac.group(1)) - 1]
            ):
                return None

            m_set_rev = re.search(r"reviewer_agent_id = \$(\d+)", set_part)
            if m_set_rev:
                rev_idx_sql = int(m_set_rev.group(1))
                assert (
                    f"NOT (paused_agent_state_json ? '{REVIEWED_WORK_AUTHORSHIP_STATE_KEY}')"
                    in where_part
                )
                assert (
                    f"AND ( ${rev_idx_sql} = paused_agent_id OR NOT ("
                    in where_part
                )
                assert (
                    f"'{{"
                    f"{REVIEWED_WORK_AUTHORSHIP_STATE_KEY},reviewed_work_author_agent_ids}}' ? ${rev_idx_sql}"
                    in where_part
                )
                cand_rev = args[rev_idx_sql - 1]
                if not self._eval_reviewer_authority(cand_rev):
                    return None
                self.row["reviewer_agent_id"] = cand_rev

            m_set_phase = re.search(r"phase = \$(\d+)", set_part)
            if m_set_phase:
                self.row["phase"] = args[int(m_set_phase.group(1)) - 1]

            m_set_fac = re.search(r"awaiting_facilitation = \$(\d+)", set_part)
            if m_set_fac:
                self.row["awaiting_facilitation"] = bool(
                    args[int(m_set_fac.group(1)) - 1]
                )

            m_set_status = re.search(r"status = \$(\d+)", set_part)
            if m_set_status:
                self.row["status"] = args[int(m_set_status.group(1)) - 1]

            effect_ts = _ts_from_ticks(self.clock_ticks)
            self.row["transcript_revision"] += 1
            self.row["updated_at"] = effect_ts
            return {
                "transcript_revision": self.row["transcript_revision"],
                "effect_ts": effect_ts,
            }

        # 6. `DialecticDB.add_message` message INSERT
        if "INSERT INTO core.dialectic_messages" in norm:
            self._msg_seq += 1
            effect_ts = _ts_from_ticks(self.clock_ticks)
            return {"message_id": self._msg_seq, "timestamp": effect_ts}

        raise AssertionError(f"Unexpected SQL in _LiveSessionSqlBackend: {norm}")


def _cas_kwargs_for_live(cas: SpecCasExpectations) -> Dict[str, Any]:
    kwargs: Dict[str, Any] = {}
    if cas.expected_phase is not None:
        kwargs["expected_phase"] = cas.expected_phase
    if cas.expected_updated_at is not None:
        kwargs["expected_updated_at"] = _ts_from_ticks(cas.expected_updated_at)
    if cas.expected_reviewer_set:
        kwargs["expected_reviewer"] = (
            AGENT_UUIDS[cas.expected_reviewer]
            if cas.expected_reviewer is not None
            else None
        )
    if cas.expected_transcript_revision is not None:
        kwargs["expected_transcript_revision"] = cas.expected_transcript_revision
    if cas.expected_awaiting_facilitation is not None:
        kwargs["expected_awaiting_facilitation"] = cas.expected_awaiting_facilitation
    if cas.expected_resolution_saga_revision is not None:
        kwargs["expected_resolution_saga_revision"] = (
            cas.expected_resolution_saga_revision
        )
    if cas.exclude_inflight_saga:
        kwargs["exclude_inflight_saga"] = True
    if cas.clear_facilitation:
        kwargs["clear_facilitation"] = True
    return kwargs


# ---------------------------------------------------------------------------
# Parsed `#guard` Trace Conformance Between `formal/SagaCasProtocol.lean`,
# Python Mirror, and Live `DialecticDB` / `_attempt_guarded_write`
# ---------------------------------------------------------------------------


def _parse_lean_cas_record(expr: str) -> SpecCasExpectations:
    phase_m = re.search(r"expectedPhase\s*:=\s*some\s+\.(\w+)", expr)
    upd_m = re.search(r"expectedUpdatedAt\s*:=\s*some\s+(\d+)", expr)
    rev_none_m = re.search(r"expectedReviewer\s*:=\s*some\s+none", expr)
    rev_some_m = re.search(r"expectedReviewer\s*:=\s*some\s+\(some\s+(\d+)\)", expr)
    tr_m = re.search(r"expectedTranscriptRevision\s*:=\s*some\s+(\d+)", expr)
    fac_m = re.search(r"expectedAwaitingFacilitation\s*:=\s*some\s+(true|false)", expr)
    excl_m = re.search(r"excludeInflightSaga\s*:=\s*(true|false)", expr)
    clear_m = re.search(r"clearFacilitation\s*:=\s*(true|false)", expr)

    return SpecCasExpectations(
        expected_phase=phase_m.group(1) if phase_m else None,
        expected_updated_at=int(upd_m.group(1)) if upd_m else None,
        expected_reviewer_set=bool(rev_none_m or rev_some_m),
        expected_reviewer=int(rev_some_m.group(1)) if rev_some_m else None,
        expected_transcript_revision=int(tr_m.group(1)) if tr_m else None,
        expected_awaiting_facilitation=(
            fac_m.group(1) == "true" if fac_m else None
        ),
        exclude_inflight_saga=(excl_m.group(1) == "true") if excl_m else False,
        clear_facilitation=(clear_m.group(1) == "true") if clear_m else False,
    )


def _camel_to_snake_saga_state(token: str) -> str:
    return re.sub(r"([a-z0-9])([A-Z])", r"\1_\2", token).lower()


def test_lean4_guard_scenarios_match_python_mirror_and_live_code():
    """Parse `sagaScenario1..3` and `sagaScenario4Counts` and their `#guard`
    assertions from `formal/SagaCasProtocol.lean` and verify parity across the
    Python mirror and the live `DialecticDB` / `_attempt_guarded_write` code."""
    code_only = _strip_lean_comments(LEAN_SPEC_PATH.read_text(encoding="utf-8"))

    scenario_blocks = re.findall(
        r"def\s+(sagaScenario\d+)\s*:\s*SessionRow\s*:=\s*(.*?)(?=\n#guard|\ndef\s+sagaScenario|\nend\s+Unitares)",
        code_only,
        flags=re.DOTALL,
    )
    assert len(scenario_blocks) == 3

    scenarios_spec: Dict[str, SpecSessionRow] = {}
    scenarios_live: Dict[str, _LiveSessionSqlBackend] = {}
    step_outputs_spec: Dict[str, SpecResolveOutput] = {}
    step_outputs_live: Dict[str, Dict[str, Any]] = {}
    let_to_guard_name = {
        ("sagaScenario1", "rStale"): "sagaScenario1Stale",
        ("sagaScenario1", "rReplay"): "sagaScenario1Replay",
        ("sagaScenario2", "rBlocked"): "sagaScenario2Blocked",
        ("sagaScenario3", "rSelfResolve"): "sagaScenario3SelfResolve",
    }

    with patch.object(sw, "_emit", new=AsyncMock()):
        for name, body in scenario_blocks:
            init_m = re.search(
                r"mkInitSession\s+(\d+)\s+(\(some\s+\d+\)|none)\s+\[([^\]]*)\]\s+(true|false)",
                body,
            )
            assert init_m, f"Could not parse mkInitSession in {name}"
            paused_idx = int(init_m.group(1))
            rev_tok = init_m.group(2).strip()
            rev_idx = (
                None
                if rev_tok == "none"
                else int(re.search(r"\d+", rev_tok).group(0))
            )
            authors_raw = init_m.group(3).strip()
            authors = (
                tuple(int(x.strip()) for x in authors_raw.split(",") if x.strip())
                if authors_raw
                else ()
            )
            pause_req = init_m.group(4) == "true"

            spec = SpecSessionRow(
                status="active",
                phase="synthesis",
                paused_agent=paused_idx,
                reviewer=rev_idx,
                transcript_revision=0,
                resolution_saga_revision=0,
                awaiting_facilitation=True,
                updated_at=100,
                pause_effect_required=pause_req,
                declared_authors=authors,
                authorship_valid=True,
                sagas=(),
            )
            backend = _LiveSessionSqlBackend(spec)
            dialectic_db_module._db_instance = backend.db

            for line in body.splitlines():
                line = line.strip()
                if not line.startswith("let ") or "mkInitSession" in line:
                    continue

                let_m = re.match(r"let\s+(\w+)\s*:=", line)
                let_var = let_m.group(1) if let_m else ""

                if "sagas :=" in line:
                    sg_m = re.search(
                        r"sagaId\s*:=\s*(\d+),\s*state\s*:=\s*\.(\w+),\s*createdAt\s*:=\s*(\d+)",
                        line,
                    )
                    assert sg_m
                    sg = SpecSagaRecord(
                        saga_id=int(sg_m.group(1)),
                        state=_camel_to_snake_saga_state(sg_m.group(2)),
                        created_at=int(sg_m.group(3)),
                    )
                    spec = replace(spec, sagas=(sg,))
                    backend.sync_sagas_from_spec(spec.sagas)
                elif "addMessage" in line:
                    cas_m = re.search(r"\{([^}]*)\}", line)
                    cas = _parse_lean_cas_record(cas_m.group(1) if cas_m else "")
                    now_m = re.search(r"(\d+)\s*$", line)
                    assert now_m
                    now_ticks = int(now_m.group(1))
                    phase_m = re.search(r"\(some\s+\.(\w+)\)", line)
                    next_phase = phase_m.group(1) if phase_m else None

                    msg_out = spec_add_message(
                        spec, cas, next_phase, None, None, None, False, now_ticks
                    )
                    spec = msg_out.state

                    backend.clock_ticks = now_ticks
                    live_kw = _cas_kwargs_for_live(cas)
                    live_kw.pop("exclude_inflight_saga", None)
                    live_kw.pop("clear_facilitation", None)
                    if next_phase is not None:
                        live_kw["next_phase"] = next_phase
                    msg_id = asyncio.run(
                        add_message_async(
                            session_id=backend.session_id,
                            agent_id=AGENT_UUIDS[spec.paused_agent],
                            message_type="synthesis",
                            reasoning="step",
                            **live_kw,
                        )
                    )
                    assert (msg_id > 0) == msg_out.written
                elif "resolveSession" in line:
                    st_m = re.search(r"resolveSession\s+[\w.]+\s+\.(\w+)", line)
                    assert st_m
                    target_status = st_m.group(1)
                    cas_m = re.search(r"\{([^}]*)\}", line)
                    cas = _parse_lean_cas_record(cas_m.group(1) if cas_m else "")
                    now_m = re.search(r"(\d+)\s*$", line)
                    assert now_m
                    now_ticks = int(now_m.group(1))

                    res_out = spec_resolve_session(
                        spec, target_status, cas, now_ticks
                    )
                    spec = res_out.state

                    backend.clock_ticks = now_ticks
                    live_kw = _cas_kwargs_for_live(cas)
                    detail: Dict[str, Any] = {}
                    raw_ok = asyncio.run(
                        backend.db.resolve_session(
                            backend.session_id,
                            {"action": target_status},
                            target_status,
                            detail=detail,
                            **live_kw,
                        )
                    )
                    sweeper_ok = bool(detail.get("written") is True)
                    assert raw_ok == res_out.raw_result
                    assert sweeper_ok == res_out.sweeper_written

                    guard_step_name = let_to_guard_name.get((name, let_var))
                    if guard_step_name is not None:
                        step_outputs_spec[guard_step_name] = res_out
                        step_outputs_live[guard_step_name] = {
                            "written": sweeper_ok,
                            "idempotent_replay": bool(not sweeper_ok and raw_ok),
                            "raw_result": raw_ok,
                            "sweeper_written": sweeper_ok,
                            "winner_status": detail.get("existing_status"),
                            "refused_reason": detail.get("refused"),
                        }

            scenarios_spec[name] = spec
            scenarios_live[name] = backend

        # Verify all `#guard` lines on `sagaScenario1..3` and intermediate step outputs
        guard_lines = re.findall(
            r"^#guard\s+(.*?sagaScenario[123].*?)$", code_only, flags=re.MULTILINE
        )
        assert len(guard_lines) >= 20
        for expr in guard_lines:
            expr = expr.strip()
            matched_step = False
            for step_name, sp_out in step_outputs_spec.items():
                if not expr.startswith(f"{step_name}."):
                    continue
                matched_step = True
                lv_out = step_outputs_live[step_name]
                if f"{step_name}.written ==" in expr:
                    expected = re.search(r"==\s*(true|false)", expr).group(1) == "true"
                    assert sp_out.written is expected
                    assert lv_out["written"] is expected
                elif f"{step_name}.idempotentReplay ==" in expr:
                    expected = re.search(r"==\s*(true|false)", expr).group(1) == "true"
                    assert sp_out.idempotent_replay is expected
                    assert lv_out["idempotent_replay"] is expected
                elif f"{step_name}.rawResult ==" in expr:
                    expected = re.search(r"==\s*(true|false)", expr).group(1) == "true"
                    assert sp_out.raw_result is expected
                    assert lv_out["raw_result"] is expected
                elif f"{step_name}.sweeperWritten ==" in expr:
                    expected = re.search(r"==\s*(true|false)", expr).group(1) == "true"
                    assert sp_out.sweeper_written is expected
                    assert lv_out["sweeper_written"] is expected
                elif f"{step_name}.winnerStatus ==" in expr:
                    if "== none" in expr:
                        assert sp_out.winner_status is None
                        assert lv_out["winner_status"] is None
                    else:
                        expected = re.search(r"some\s+\.(\w+)", expr).group(1)
                        assert sp_out.winner_status == expected
                        assert lv_out["winner_status"] == expected
                elif f"{step_name}.refusedReason ==" in expr:
                    if "== none" in expr:
                        assert sp_out.refused_reason is None
                        assert lv_out["refused_reason"] is None
                    else:
                        expected = re.search(r'some\s+"([^"]+)"', expr).group(1)
                        assert sp_out.refused_reason == expected
                        assert lv_out["refused_reason"] == expected
                else:
                    raise AssertionError(f"Unrecognized step #guard expression: {expr}")
            if matched_step:
                continue

            for name in ("sagaScenario1", "sagaScenario2", "sagaScenario3"):
                if name not in expr:
                    continue
                sp = scenarios_spec[name]
                bk = scenarios_live[name]
                dialectic_db_module._db_instance = bk.db
                if f"{name}.status ==" in expr:
                    expected = re.search(r"==\s*\.(\w+)", expr).group(1)
                    assert sp.status == expected
                    assert bk.row["status"] == expected
                elif f"{name}.phase ==" in expr:
                    expected = re.search(r"==\s*\.(\w+)", expr).group(1)
                    assert sp.phase == expected
                    assert bk.row["phase"] == expected
                elif f"{name}.awaitingFacilitation ==" in expr:
                    expected = re.search(r"==\s*(true|false)", expr).group(1) == "true"
                    assert sp.awaiting_facilitation is expected
                    assert bk.row["awaiting_facilitation"] is expected
                elif f"{name}.transcriptRevision ==" in expr:
                    expected = int(re.search(r"==\s*(\d+)", expr).group(1))
                    assert sp.transcript_revision == expected
                    assert bk.row["transcript_revision"] == expected
                elif f"{name}.updatedAt ==" in expr:
                    expected = int(re.search(r"==\s*(\d+)", expr).group(1))
                    assert sp.updated_at == expected
                    assert _ticks_from_ts(bk.row["updated_at"]) == expected
                elif f"hasInflightSaga {name}.sagas ==" in expr:
                    expected = re.search(r"==\s*(true|false)", expr).group(1) == "true"
                    assert spec_has_inflight_saga(sp.sagas) is expected
                    assert (
                        asyncio.run(has_inflight_saga_async(bk.session_id))
                        is expected
                    )
                elif f"probeWriteOverlap (probeSagaSince {name}.sagas" in expr:
                    m_pr = re.search(
                        r"\(some\s+(\d+)\)\s+(true|false)\)\s*==\s*\.(\w+)", expr
                    )
                    assert m_pr
                    since_ticks = int(m_pr.group(1))
                    healthy = m_pr.group(2) == "true"
                    expected_ov = _camel_to_snake_saga_state(m_pr.group(3))
                    assert (
                        spec_probe_write_overlap(
                            spec_probe_saga_since(sp.sagas, since_ticks, healthy)
                        )
                        == expected_ov
                    )
                    bk.probe_healthy = healthy
                    with patch(
                        "src.mcp_handlers.dialectic.auto_resolve.emit_write_overlap",
                        new=AsyncMock(),
                    ):
                        live_ov = asyncio.run(
                            _probe_write_overlap(
                                bk.session_id,
                                "failed",
                                bk.row["paused_agent_id"],
                                early_check_ts=_ts_from_ticks(since_ticks),
                            )
                        )
                    assert live_ov == expected_ov
                else:
                    raise AssertionError(f"Unrecognized scenario #guard expression: {expr}")

    # Also verify `sagaScenario4Counts` against real `_attempt_guarded_write`
    spec_counts = SpecCycleCounts()
    live_counts = _CycleCounts()
    session_dict = {"session_id": "s-4", "paused_agent_id": AGENT_UUIDS[0]}

    steps = [
        ("succeeded", {}),
        (
            "succeeded",
            {
                "saga_id": "sg-1",
                "state": "pg_committed",
                "created_at": _ts_from_ticks(105),
            },
        ),
        ("succeeded", None),
        ("refused", {}),
        ("error", {}),
    ]
    for kind, probe_val in steps:
        spec_counts = spec_attempt_guarded_write(spec_counts, kind, probe_val)

        async def _write_cb(winner: Dict[str, Any], _k=kind) -> bool:
            if _k == "error":
                raise RuntimeError("simulated db error")
            if _k == "refused":
                winner["winner_status"] = "failed"
                return False
            return True

        with patch(
            "src.mcp_handlers.dialectic.auto_resolve.probe_saga_since_async",
            new=AsyncMock(return_value=probe_val),
        ), patch(
            "src.mcp_handlers.dialectic.auto_resolve.emit_guarded_write",
            new=AsyncMock(),
        ), patch(
            "src.mcp_handlers.dialectic.auto_resolve.emit_write_refused",
            new=AsyncMock(),
        ), patch(
            "src.mcp_handlers.dialectic.auto_resolve.emit_write_overlap",
            new=AsyncMock(),
        ):
            if kind == "error":
                with pytest.raises(RuntimeError):
                    asyncio.run(
                        _attempt_guarded_write(
                            live_counts,
                            [],
                            attempted="failed",
                            session=session_dict,
                            write=_write_cb,
                            decision_read_ts=BASE_TIME,
                            early_check_ts=BASE_TIME,
                        )
                    )
            else:
                asyncio.run(
                    _attempt_guarded_write(
                        live_counts,
                        [],
                        attempted="failed",
                        session=session_dict,
                        write=_write_cb,
                        decision_read_ts=BASE_TIME,
                        early_check_ts=BASE_TIME,
                    )
                )

    assert spec_counts.is_balanced()
    assert live_counts.write_attempt_count == spec_counts.write_attempt_count == 5
    assert (
        live_counts.write_succeeded_count == spec_counts.write_succeeded_count == 3
    )
    assert live_counts.skipped_count == spec_counts.skipped_count == 1
    assert live_counts.write_error_count == spec_counts.write_error_count == 1
    assert live_counts.overlap_clean_count == spec_counts.overlap_clean_count == 1
    assert (
        live_counts.overlap_detected_count
        == spec_counts.overlap_detected_count
        == 1
    )
    assert (
        live_counts.overlap_probe_failed_count
        == spec_counts.overlap_probe_failed_count
        == 1
    )


# ---------------------------------------------------------------------------
# Hypothesis RuleBasedStateMachine: Refinement & Theorem Invariants (1–8)
# ---------------------------------------------------------------------------


class SagaCasProtocolStateMachine(RuleBasedStateMachine):
    """Stateful refinement test driving `SpecSessionRow` + `SpecCycleCounts`
    and the real `DialecticDB` + `_attempt_guarded_write` in lockstep.

    In `@invariant`, the machine checks bisimulation between the formal state
    and the live SQL-backed row plus `_CycleCounts` conservation (Theorem 8).
    In each `@rule`, it asserts the transition's theorem invariants (Theorems 1–8).
    """

    def __init__(self):
        super().__init__()
        self.spec: Optional[SpecSessionRow] = None
        self.backend: Optional[_LiveSessionSqlBackend] = None
        self.spec_counts = SpecCycleCounts()
        self.live_counts = _CycleCounts()
        self.clock = 100
        self.next_saga_id = 1
        self.emitted_writes: List[Dict[str, Any]] = []

    def _tick(self) -> int:
        self.clock += 10
        assert self.backend is not None
        self.backend.clock_ticks = self.clock
        return self.clock

    async def _capture_emit(self, details: Dict[str, Any]) -> None:
        self.emitted_writes.append(details)

    @initialize(
        paused_agent=st.integers(min_value=0, max_value=1),
        reviewer=st.one_of(st.none(), st.integers(min_value=0, max_value=3)),
        declared_authors=st.lists(
            st.integers(min_value=2, max_value=4), max_size=2, unique=True
        ),
        pause_effect_required=st.booleans(),
        authorship_valid=st.booleans(),
        awaiting_facilitation=st.booleans(),
    )
    def init_session(
        self,
        paused_agent: int,
        reviewer: Optional[int],
        declared_authors: List[int],
        pause_effect_required: bool,
        authorship_valid: bool,
        awaiting_facilitation: bool,
    ):
        self.clock = 100
        self.next_saga_id = 1
        self.spec_counts = SpecCycleCounts()
        self.live_counts = _CycleCounts()
        self.spec = SpecSessionRow(
            status="active",
            phase="synthesis",
            paused_agent=paused_agent,
            reviewer=reviewer,
            transcript_revision=0,
            resolution_saga_revision=0,
            awaiting_facilitation=awaiting_facilitation,
            updated_at=self.clock,
            pause_effect_required=pause_effect_required,
            declared_authors=tuple(declared_authors),
            authorship_valid=authorship_valid,
            sagas=(),
        )
        self.backend = _LiveSessionSqlBackend(self.spec)
        dialectic_db_module._db_instance = self.backend.db

    @rule(
        pin_phase=st.booleans(),
        wrong_phase=st.booleans(),
        pin_rev=st.booleans(),
        wrong_rev=st.booleans(),
        reopen_failed_facilitation=st.booleans(),
        next_phase=st.sampled_from([None, "antithesis", "synthesis"]),
        next_reviewer=st.one_of(st.none(), st.integers(min_value=0, max_value=4)),
    )
    def step_add_message(
        self,
        pin_phase: bool,
        wrong_phase: bool,
        pin_rev: bool,
        wrong_rev: bool,
        reopen_failed_facilitation: bool,
        next_phase: Optional[str],
        next_reviewer: Optional[int],
    ):
        assert self.spec is not None and self.backend is not None
        dialectic_db_module._db_instance = self.backend.db
        now = self._tick()

        exp_phase = None
        if pin_phase:
            exp_phase = (
                "thesis"
                if (wrong_phase and self.spec.phase != "thesis")
                else self.spec.phase
            )
        exp_rev = None
        if pin_rev:
            exp_rev = (
                self.spec.transcript_revision + 5
                if wrong_rev
                else self.spec.transcript_revision
            )

        cas = SpecCasExpectations(
            expected_phase=exp_phase,
            expected_transcript_revision=exp_rev,
        )
        next_fac = False if reopen_failed_facilitation else None
        next_status = "active" if reopen_failed_facilitation else None

        prev_rev = self.spec.transcript_revision
        spec_out = spec_add_message(
            self.spec,
            cas,
            next_phase,
            next_reviewer,
            next_fac,
            next_status,
            reopen_failed_facilitation,
            now,
        )
        self.spec = spec_out.state

        live_kw = _cas_kwargs_for_live(cas)
        if next_phase is not None:
            live_kw["next_phase"] = next_phase
        if next_reviewer is not None:
            live_kw["next_reviewer"] = AGENT_UUIDS[next_reviewer]
        if next_fac is not None:
            live_kw["next_awaiting_facilitation"] = next_fac
        if next_status is not None:
            live_kw["next_status"] = next_status
        if reopen_failed_facilitation:
            live_kw["reopen_failed_facilitation"] = True

        with patch.object(sw, "_emit", side_effect=self._capture_emit):
            msg_id = asyncio.run(
                add_message_async(
                    session_id=self.backend.session_id,
                    agent_id=AGENT_UUIDS[self.spec.paused_agent],
                    message_type="synthesis",
                    reasoning="msg",
                    **live_kw,
                )
            )
        assert (msg_id > 0) == spec_out.written
        if spec_out.written:
            assert self.spec.transcript_revision == prev_rev + 1
            assert self.backend.row["transcript_revision"] == prev_rev + 1

    @rule(target_status=st.sampled_from(["resolved", "failed"]))
    def step_concurrent_message_bump_invalidates_stale_cas(
        self, target_status: str
    ):
        """Directly exercise Theorem 5 (`concurrent_message_bump_invalidates_stale_cas`):
        when a message append lands after a caller snapshots `transcript_revision`,
        the caller's subsequent `resolve_session_async` with that snapshot revision
        is refused."""
        assert self.spec is not None and self.backend is not None
        if self.spec.status != "active":
            return
        dialectic_db_module._db_instance = self.backend.db

        pinned_rev = self.spec.transcript_revision
        now1 = self._tick()
        msg_cas = SpecCasExpectations(
            expected_phase=self.spec.phase,
            expected_transcript_revision=pinned_rev,
        )
        msg_out = spec_add_message(
            self.spec, msg_cas, None, None, None, None, False, now1
        )
        assert msg_out.written is True
        self.spec = msg_out.state

        with patch.object(sw, "_emit", side_effect=self._capture_emit):
            msg_id = asyncio.run(
                add_message_async(
                    session_id=self.backend.session_id,
                    agent_id=AGENT_UUIDS[self.spec.paused_agent],
                    message_type="synthesis",
                    reasoning="concurrent bump",
                    expected_phase=msg_cas.expected_phase,
                    expected_transcript_revision=pinned_rev,
                )
            )
        assert msg_id > 0

        now2 = self._tick()
        stale_cas = SpecCasExpectations(expected_transcript_revision=pinned_rev)
        res_out = spec_resolve_session(self.spec, target_status, stale_cas, now2)
        # Theorem 5 assertion
        assert res_out.written is False
        assert res_out.state == self.spec

        winner: Dict[str, Any] = {}
        with patch.object(sw, "_emit", side_effect=self._capture_emit):
            live_ok = asyncio.run(
                resolve_session_async(
                    self.backend.session_id,
                    {"action": target_status},
                    target_status,
                    winner=winner,
                    expected_transcript_revision=pinned_rev,
                )
            )
        assert live_ok is False
        assert winner.get("winner_status") == self.spec.status

    @rule(
        new_state=st.sampled_from(ALL_SAGA_STATES),
        update_existing=st.booleans(),
        probe_healthy=st.booleans(),
    )
    def step_spawn_or_advance_saga(
        self, new_state: str, update_existing: bool, probe_healthy: bool
    ):
        assert self.spec is not None and self.backend is not None
        dialectic_db_module._db_instance = self.backend.db
        now = self._tick()

        if update_existing and self.spec.sagas:
            last = self.spec.sagas[-1]
            updated = SpecSagaRecord(
                saga_id=last.saga_id,
                state=new_state,
                created_at=last.created_at,
            )
            new_sagas = self.spec.sagas[:-1] + (updated,)
            window_start = last.created_at
        else:
            sg = SpecSagaRecord(
                saga_id=self.next_saga_id,
                state=new_state,
                created_at=now,
            )
            self.next_saga_id += 1
            new_sagas = self.spec.sagas + (sg,)
            window_start = now

        self.spec = replace(self.spec, sagas=new_sagas)
        self.backend.sync_sagas_from_spec(self.spec.sagas)
        self.backend.probe_healthy = probe_healthy

        # Check Theorems 7a & 7b against live `_probe_write_overlap`
        spec_probe = spec_probe_saga_since(
            self.spec.sagas, window_start, probe_healthy
        )
        spec_ov = spec_probe_write_overlap(spec_probe)
        if not probe_healthy:
            assert spec_ov == "probe_failed"
        else:
            assert spec_ov == "detected"

        with patch(
            "src.mcp_handlers.dialectic.auto_resolve.emit_write_overlap",
            new=AsyncMock(),
        ):
            live_ov = asyncio.run(
                _probe_write_overlap(
                    self.backend.session_id,
                    "failed",
                    self.backend.row["paused_agent_id"],
                    early_check_ts=_ts_from_ticks(window_start),
                )
            )
        assert live_ov == spec_ov
        self.backend.probe_healthy = True

    @rule(
        target_status=st.sampled_from(["resolved", "failed"]),
        pin_rev=st.booleans(),
        wrong_rev=st.booleans(),
        pin_fac=st.booleans(),
        wrong_fac=st.booleans(),
        exclude_inflight_saga=st.booleans(),
        use_sweeper_winner=st.booleans(),
    )
    def step_resolve_session(
        self,
        target_status: str,
        pin_rev: bool,
        wrong_rev: bool,
        pin_fac: bool,
        wrong_fac: bool,
        exclude_inflight_saga: bool,
        use_sweeper_winner: bool,
    ):
        assert self.spec is not None and self.backend is not None
        dialectic_db_module._db_instance = self.backend.db
        now = self._tick()

        exp_rev = None
        if pin_rev:
            exp_rev = (
                self.spec.transcript_revision + 9
                if wrong_rev
                else self.spec.transcript_revision
            )
        exp_fac = None
        if pin_fac:
            exp_fac = (
                (not self.spec.awaiting_facilitation)
                if wrong_fac
                else self.spec.awaiting_facilitation
            )

        cas = SpecCasExpectations(
            expected_transcript_revision=exp_rev,
            expected_awaiting_facilitation=exp_fac,
            exclude_inflight_saga=exclude_inflight_saga,
            clear_facilitation=True,
        )

        prev_spec = self.spec
        res_out = spec_resolve_session(prev_spec, target_status, cas, now)
        self.spec = res_out.state

        live_kw = _cas_kwargs_for_live(cas)
        winner: Optional[Dict[str, Any]] = {} if use_sweeper_winner else None
        self.emitted_writes.clear()

        with patch.object(sw, "_emit", side_effect=self._capture_emit):
            live_res = asyncio.run(
                resolve_session_async(
                    self.backend.session_id,
                    {"action": target_status, "reason": "test"},
                    target_status,
                    winner=winner,
                    **live_kw,
                )
            )

        expected_live_res = (
            res_out.sweeper_written if use_sweeper_winner else res_out.raw_result
        )
        assert live_res == expected_live_res

        # Theorem 1: Terminal Write Immutability
        if prev_spec.is_terminal:
            assert res_out.written is False
            assert self.spec == prev_spec

        # Theorem 2: Idempotent Replay Is Not Counted As A Sweeper Write
        if prev_spec.is_terminal and prev_spec.status == target_status:
            assert res_out.idempotent_replay is True
            assert res_out.raw_result is True
            assert res_out.sweeper_written is False
            assert self.emitted_writes[-1]["outcome"] == "no_op"

        # Theorem 3: Terminal Write Always Clears Facilitation
        if res_out.written:
            assert self.spec.awaiting_facilitation is False
            assert self.spec.status == target_status
            assert self.backend.row["awaiting_facilitation"] is False

        # Theorem 4: In-Flight Saga Blocks `excludeInflightSaga` Writes
        if exclude_inflight_saga and spec_has_inflight_saga(prev_spec.sagas):
            assert res_out.written is False
            assert self.spec == prev_spec

        # Theorem 6: Pause-Bound Self-Review Blocks `.resolved` Terminal Writes
        if (
            target_status == "resolved"
            and prev_spec.pause_effect_required
            and prev_spec.reviewer == prev_spec.paused_agent
        ):
            assert res_out.written is False
            assert self.spec == prev_spec

    @rule(
        inject_error=st.booleans(),
        probe_healthy=st.booleans(),
    )
    def step_sweeper_guarded_write(
        self, inject_error: bool, probe_healthy: bool
    ):
        """Drive the real `_attempt_guarded_write` with a live `resolve_session_async`
        callback and live `_probe_write_overlap`, asserting Theorem 8
        (`cycle_counts_conservation`)."""
        assert self.spec is not None and self.backend is not None
        dialectic_db_module._db_instance = self.backend.db
        early_ticks = self.clock
        now = self._tick()
        self.backend.probe_healthy = probe_healthy

        cas = SpecCasExpectations(
            expected_phase=self.spec.phase,
            expected_transcript_revision=self.spec.transcript_revision,
            exclude_inflight_saga=True,
            clear_facilitation=True,
        )

        if inject_error:
            self.spec_counts = spec_attempt_guarded_write(
                self.spec_counts, "error"
            )
        else:
            res_out = spec_resolve_session(self.spec, "failed", cas, now)
            self.spec = res_out.state
            if res_out.sweeper_written:
                probe_res = spec_probe_saga_since(
                    self.spec.sagas, early_ticks, probe_healthy
                )
                self.spec_counts = spec_attempt_guarded_write(
                    self.spec_counts, "succeeded", probe_res
                )
            else:
                self.spec_counts = spec_attempt_guarded_write(
                    self.spec_counts, "refused"
                )

        live_kw = _cas_kwargs_for_live(cas)

        async def _live_write(winner: Dict[str, Any]) -> bool:
            if inject_error:
                raise RuntimeError("injected write error")
            return await resolve_session_async(
                self.backend.session_id,
                {"action": "failed", "reason": "sweeper"},
                "failed",
                winner=winner,
                **live_kw,
            )

        session_snapshot = {
            "session_id": self.backend.session_id,
            "paused_agent_id": self.backend.row["paused_agent_id"],
            "phase": self.backend.row["phase"],
        }

        with patch.object(
            sw, "_emit", side_effect=self._capture_emit
        ), patch(
            "src.mcp_handlers.dialectic.auto_resolve.emit_guarded_write",
            new=AsyncMock(),
        ), patch(
            "src.mcp_handlers.dialectic.auto_resolve.emit_write_refused",
            new=AsyncMock(),
        ), patch(
            "src.mcp_handlers.dialectic.auto_resolve.emit_write_overlap",
            new=AsyncMock(),
        ):
            if inject_error:
                with pytest.raises(RuntimeError):
                    asyncio.run(
                        _attempt_guarded_write(
                            self.live_counts,
                            [],
                            attempted="failed",
                            session=session_snapshot,
                            write=_live_write,
                            decision_read_ts=_ts_from_ticks(early_ticks),
                            early_check_ts=_ts_from_ticks(early_ticks),
                        )
                    )
            else:
                asyncio.run(
                    _attempt_guarded_write(
                        self.live_counts,
                        [],
                        attempted="failed",
                        session=session_snapshot,
                        write=_live_write,
                        decision_read_ts=_ts_from_ticks(early_ticks),
                        early_check_ts=_ts_from_ticks(early_ticks),
                    )
                )

        self.backend.probe_healthy = True
        assert self.spec_counts.is_balanced()

    @invariant()
    def check_bisimulation_and_cycle_conservation(self):
        if self.spec is None or self.backend is None:
            return
        dialectic_db_module._db_instance = self.backend.db

        # 1. Session row bisimulation
        assert self.backend.row["status"] == self.spec.status
        assert self.backend.row["phase"] == self.spec.phase
        assert (
            self.backend.row["transcript_revision"]
            == self.spec.transcript_revision
        )
        assert (
            self.backend.row["awaiting_facilitation"]
            == self.spec.awaiting_facilitation
        )
        assert (
            _ticks_from_ts(self.backend.row["updated_at"]) == self.spec.updated_at
        )
        expected_rev = (
            AGENT_UUIDS[self.spec.reviewer]
            if self.spec.reviewer is not None
            else None
        )
        assert self.backend.row["reviewer_agent_id"] == expected_rev

        # 2. In-flight saga predicate bisimulation
        live_inflight = asyncio.run(
            has_inflight_saga_async(self.backend.session_id)
        )
        assert live_inflight == spec_has_inflight_saga(self.spec.sagas)

        # 3. Theorem 8: CycleCounts conservation and exact bisimulation
        assert self.spec_counts.is_balanced()
        assert (
            self.live_counts.write_attempt_count
            == self.spec_counts.write_attempt_count
        )
        assert (
            self.live_counts.write_succeeded_count
            == self.spec_counts.write_succeeded_count
        )
        assert self.live_counts.skipped_count == self.spec_counts.skipped_count
        assert (
            self.live_counts.write_error_count
            == self.spec_counts.write_error_count
        )
        assert (
            self.live_counts.overlap_clean_count
            == self.spec_counts.overlap_clean_count
        )
        assert (
            self.live_counts.overlap_detected_count
            == self.spec_counts.overlap_detected_count
        )
        assert (
            self.live_counts.overlap_probe_failed_count
            == self.spec_counts.overlap_probe_failed_count
        )
        assert (
            self.live_counts.write_attempt_count
            == self.live_counts.write_succeeded_count
            + self.live_counts.skipped_count
            + self.live_counts.write_error_count
        )
        assert (
            self.live_counts.write_succeeded_count
            == self.live_counts.overlap_clean_count
            + self.live_counts.overlap_detected_count
            + self.live_counts.overlap_probe_failed_count
        )


TestSagaCasProtocolStateMachine = SagaCasProtocolStateMachine.TestCase
TestSagaCasProtocolStateMachine.settings = settings(
    max_examples=75,
    stateful_step_count=30,
    deadline=None,
)
