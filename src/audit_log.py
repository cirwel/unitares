"""
Audit Log for Governance System
Records all skipped lambda1 updates and auto-attestations for analysis.

JSONL is the raw truth log. PostgreSQL provides queryable indexing.
"""

import json
from pathlib import Path
from datetime import datetime
from typing import Dict, Iterator, List, Optional
from dataclasses import dataclass, asdict
import fcntl
import os

# Import structured logging
from src.logging_utils import get_logger
logger = get_logger(__name__)


# Module-local strong-ref set for in-flight fire-and-forget PG audit-write
# tasks. Without this, `loop.create_task(coro)` returns a Task that CPython
# GC can collect mid-await — `_write_to_postgres` awaits asyncpg, so a
# collection between yields drops the write. Mirrors the canonical pattern
# at `src/coordination_failure_emit.py:_inflight_dedicated_writes` and
# `src/mcp_handlers/support/pause_ttl.py:_inflight_persistence_tasks`.
_inflight_pg_audit_tasks: "set" = set()


def _spawn_pg_audit_task(loop, coro) -> None:
    """Spawn coro on `loop` and pin a strong ref until it completes."""
    task = loop.create_task(coro, name="pg_audit_write")
    _inflight_pg_audit_tasks.add(task)
    task.add_done_callback(_inflight_pg_audit_tasks.discard)


def _parse_ts_naive(ts: str) -> datetime:
    """Parse an ISO timestamp and strip tzinfo for naive-naive comparison.

    Audit entries written across the lifetime of the system mix tz-naive and
    tz-aware timestamps; comparing one against ``datetime.now()`` (naive) raises
    ``TypeError``. Normalising both sides to naive lets rotation / window
    queries work uniformly. The wall-clock semantic is preserved: callers that
    care about UTC vs local already established that convention upstream when
    they wrote the entry.
    """
    parsed = datetime.fromisoformat(ts)
    if parsed.tzinfo is not None:
        parsed = parsed.replace(tzinfo=None)
    return parsed


def _iter_jsonl_reverse(path: Path, chunk_size: int = 65536) -> Iterator[str]:
    """Yield non-empty lines from a JSONL file in reverse order, lazily.

    Append-only JSONL files have monotonic-ish timestamps: callers that want a
    recent time window can read from the tail and `break` when they cross the
    cutoff, turning O(file) scans into O(window). Empty lines are skipped so a
    trailing newline is harmless.
    """
    with open(path, "rb") as f:
        f.seek(0, 2)
        pos = f.tell()
        if pos == 0:
            return
        leftover = b""
        while pos > 0:
            read_size = min(chunk_size, pos)
            pos -= read_size
            f.seek(pos)
            chunk = f.read(read_size)
            data = chunk + leftover
            lines = data.split(b"\n")
            if pos > 0:
                leftover = lines[0]
                lines = lines[1:]
            else:
                leftover = b""
            for line in reversed(lines):
                if line:
                    yield line.decode("utf-8", errors="replace")
        if leftover:
            yield leftover.decode("utf-8", errors="replace")


@dataclass
class AuditEntry:
    """Single audit log entry"""
    timestamp: str
    agent_id: Optional[str]
    event_type: str  # "lambda1_skip", "auto_attest", "calibration_check", "complexity_derivation"
    confidence: float
    details: Dict
    metadata: Optional[Dict] = None
    session_id: Optional[str] = None


class AuditLogger:
    """Manages audit logging for governance system"""
    _event_loop = None  # Set by server at startup for executor-thread writes

    def __init__(self, log_file: Optional[Path] = None):
        if log_file is None:
            project_root = Path(__file__).parent.parent
            log_file = project_root / "data" / "audit_log.jsonl"

        self.log_file = log_file
        self.log_file.parent.mkdir(parents=True, exist_ok=True)

        self._jsonl_enabled = os.getenv("UNITARES_AUDIT_WRITE_JSONL", "1").strip().lower() not in ("0", "false", "no")
    
    def log_lambda1_skip(self, agent_id: str, confidence: float, threshold: float, 
                         update_count: int, reason: str = None):
        """Log a skipped lambda1 update"""
        entry = AuditEntry(
            timestamp=datetime.now().isoformat(),
            agent_id=agent_id,
            event_type="lambda1_skip",
            confidence=confidence,
            details={
                "threshold": threshold,
                "update_count": update_count,
                "reason": reason or f"confidence {confidence:.3f} < threshold {threshold:.3f}"
            }
        )
        self._write_entry(entry)

    def log_grounding_shadow(self, agent_id: str, ungrounded: dict, grounded: dict,
                             sources: dict, applied: bool):
        """Shadow-compare grounded vs ungrounded canonical metrics (E/I/S/coherence).

        Records what enrich_grounding would change each dimension to, and which
        tier produced it (s_source/coherence_source/...), before the persist and
        response stages. `applied` says whether the grounded values were actually
        kept this check-in (grounding_apply_enabled) or reverted (shadow only).
        Lets the fleet-wide metric shift from activating grounding be measured.
        """
        dims = ("E", "I", "S", "coherence")
        details = {"applied": bool(applied), "sources": sources or {}}
        for d in dims:
            u, g = ungrounded.get(d), grounded.get(d)
            details[d] = {"ungrounded": u, "grounded": g,
                          "delta": (g - u) if isinstance(u, (int, float)) and isinstance(g, (int, float)) else None}
        entry = AuditEntry(
            timestamp=datetime.now().isoformat(),
            agent_id=agent_id,
            event_type="grounding_shadow",
            confidence=0.0,
            details=details,
        )
        self._write_entry(entry)

    def log_pause_auto_expired(self, agent_id: str,
                                original_paused_at: Optional[str],
                                elapsed_seconds: float):
        """Log a stale-pause auto-expiration.

        Emitted when a paused agent's status is auto-cleared at a gate
        because the pause is older than `PAUSE_AUTO_EXPIRE_SECONDS`.
        The categorizer is allowed to re-evaluate on the same call;
        if the agent is genuinely degraded, the normal circuit-breaker
        path re-pauses on the next cycle. See
        `src/mcp_handlers/support/pause_ttl.py` and
 ``.
        """
        entry = AuditEntry(
            timestamp=datetime.now().isoformat(),
            agent_id=agent_id,
            event_type="pause_auto_expired",
            confidence=0.0,
            details={
                "original_paused_at": original_paused_at,
                "elapsed_seconds": round(elapsed_seconds, 1),
            }
        )
        self._write_entry(entry)

    def log_attest_gap_suppressed(self, agent_id: str, elapsed_seconds: float,
                                   risk_score: float, original_reason: str,
                                   cycles_remaining: int,
                                   confidence: float = None):
        """Log a 'pause' decision suppressed by the gap-recovery window.

        Emitted when an attestation runs in the N cycles immediately after
        a DT_MAX-saturating wall-clock gap. The original 'pause' decision is
        downgraded to 'proceed' on the assumption that the inputs reflect a
        sleep-wake transient rather than real drift. Cycle counter decrements
        until the recovery window closes and normal pause semantics resume.
        """
        entry = AuditEntry(
            timestamp=datetime.now().isoformat(),
            agent_id=agent_id,
            event_type="attest_gap_suppressed",
            confidence=float(confidence) if confidence is not None else 0.0,
            details={
                "elapsed_seconds": round(elapsed_seconds, 1),
                "risk_score": risk_score,
                "original_reason": original_reason,
                "cycles_remaining": cycles_remaining,
            }
        )
        self._write_entry(entry)

    def log_warmup_structural_suppressed(self, agent_id: str, sub_action: str,
                                         original_reason: str, process_cycle: int,
                                         coherence: float = None, void: float = None):
        """Log a STRUCTURAL pause suppressed by the warmup grace window.

        Emitted when a void/coherence/basin (cold-ODE-transient) pause fires in
        the first few process-LOCAL cycles after a restart, but the restored
        behavioral baseline is established and judges the agent 'safe'. The pause
        is downgraded to 'proceed' on the assumption the structural metric is a
        cold-start artifact, not real degradation. Every suppression is recorded
        so an operator can audit that no genuine safety signal was masked.
        """
        entry = AuditEntry(
            timestamp=datetime.now().isoformat(),
            agent_id=agent_id,
            event_type="warmup_structural_suppressed",
            confidence=0.0,
            details={
                "sub_action": sub_action,
                "original_reason": original_reason,
                "process_cycle": process_cycle,
                "coherence": round(coherence, 4) if coherence is not None else None,
                "void": round(void, 4) if void is not None else None,
            }
        )
        self._write_entry(entry)

    def log_auto_attest(self, agent_id: str, confidence: float, ci_passed: bool,
                       risk_score: float, decision: str, details: Dict = None):
        """Log an auto-attestation event"""
        entry = AuditEntry(
            timestamp=datetime.now().isoformat(),
            agent_id=agent_id,
            event_type="auto_attest",
            confidence=confidence,
            details={
                "ci_passed": ci_passed,
                "risk_score": risk_score,
                "decision": decision,
                **(details or {})
            }
        )
        self._write_entry(entry)
    
    # NOTE: log_knowledge_visibility_warning removed (knowledge layer archived November 28, 2025)

    # ============================================================
    # Cross-Device Audit Events (Mac↔Pi Orchestration)
    # ============================================================

    def log_cross_device_call(self, agent_id: str, source_device: str, target_device: str,
                              tool_name: str, arguments: Dict, status: str = "initiated",
                              latency_ms: Optional[float] = None, error: Optional[str] = None,
                              details: Optional[Dict] = None):
        """
        Log a cross-device MCP tool call (Mac↔Pi orchestration).

        Args:
            agent_id: Agent making the call
            source_device: Device initiating call ("mac" or "pi")
            target_device: Device receiving call ("mac" or "pi")
            tool_name: Name of the tool being called
            arguments: Tool arguments (sanitized - no secrets)
            status: "initiated", "success", "error", "timeout"
            latency_ms: Round-trip latency in milliseconds
            error: Error message if status is "error"
            details: Additional context
        """
        entry = AuditEntry(
            timestamp=datetime.now().isoformat(),
            agent_id=agent_id,
            event_type="cross_device_call",
            confidence=1.0,
            details={
                "source_device": source_device,
                "target_device": target_device,
                "tool_name": tool_name,
                "arguments": arguments,
                "status": status,
                "latency_ms": latency_ms,
                "error": error,
                **(details or {})
            }
        )
        self._write_entry(entry)

    def log_orchestration_request(self, agent_id: str, workflow: str, target_device: str,
                                  tools_planned: List[str], context: Optional[Dict] = None,
                                  details: Optional[Dict] = None):
        """
        Log an orchestration request (Mac planning multi-step Pi coordination).

        Args:
            agent_id: Agent initiating orchestration
            workflow: Name of the workflow being executed
            target_device: Target device for orchestration
            tools_planned: List of tools to be called
            context: Workflow context (e.g., trigger, goals)
            details: Additional metadata
        """
        entry = AuditEntry(
            timestamp=datetime.now().isoformat(),
            agent_id=agent_id,
            event_type="orchestration_request",
            confidence=1.0,
            details={
                "workflow": workflow,
                "target_device": target_device,
                "tools_planned": tools_planned,
                "context": context,
                **(details or {})
            }
        )
        self._write_entry(entry)

    def log_orchestration_complete(self, agent_id: str, workflow: str, target_device: str,
                                   tools_executed: List[str], success: bool,
                                   total_latency_ms: float, errors: Optional[List[str]] = None,
                                   results_summary: Optional[Dict] = None,
                                   details: Optional[Dict] = None):
        """
        Log orchestration completion with summary metrics.

        Args:
            agent_id: Agent that ran orchestration
            workflow: Name of the completed workflow
            target_device: Target device
            tools_executed: List of tools that were executed
            success: Whether all tools completed successfully
            total_latency_ms: Total workflow latency
            errors: List of any errors encountered
            results_summary: High-level summary of results
            details: Additional metadata
        """
        entry = AuditEntry(
            timestamp=datetime.now().isoformat(),
            agent_id=agent_id,
            event_type="orchestration_complete",
            confidence=1.0,
            details={
                "workflow": workflow,
                "target_device": target_device,
                "tools_executed": tools_executed,
                "success": success,
                "total_latency_ms": total_latency_ms,
                "errors": errors or [],
                "results_summary": results_summary,
                **(details or {})
            }
        )
        self._write_entry(entry)

    def log_device_health_check(self, agent_id: str, device: str, status: str,
                                latency_ms: Optional[float] = None,
                                components: Optional[Dict[str, str]] = None,
                                details: Optional[Dict] = None):
        """
        Log a device health check (connectivity, service status).

        Args:
            agent_id: Agent performing health check
            device: Device being checked ("mac" or "pi")
            status: "healthy", "degraded", "unreachable", "error"
            latency_ms: Health check latency
            components: Component-level status (e.g., {"sensors": "ok", "display": "ok"})
            details: Additional context
        """
        entry = AuditEntry(
            timestamp=datetime.now().isoformat(),
            agent_id=agent_id,
            event_type="device_health_check",
            confidence=1.0,
            details={
                "device": device,
                "status": status,
                "latency_ms": latency_ms,
                "components": components or {},
                **(details or {})
            }
        )
        self._write_entry(entry)

    def log_eisv_sync(self, agent_id: str, source_device: str, target_device: str,
                      anima_state: Dict, eisv_mapped: Dict, sync_direction: str = "pi_to_mac",
                      details: Optional[Dict] = None):
        """
        Log EISV state synchronization between devices.

        Maps Anima state (Pi) to EISV governance state (Mac):
        - Warmth → Energy (E)
        - Clarity → Integrity (I)
        - 1 - Stability → Entropy (S)
        - (1 - Presence) × 0.3 → Void (V)  [observation-layer seed; ODE evolves independently]

        Args:
            agent_id: Agent performing sync
            source_device: Device providing state
            target_device: Device receiving state
            anima_state: Raw anima values from Pi
            eisv_mapped: Mapped EISV values
            sync_direction: "pi_to_mac" or "mac_to_pi"
            details: Additional context
        """
        entry = AuditEntry(
            timestamp=datetime.now().isoformat(),
            agent_id=agent_id,
            event_type="eisv_sync",
            confidence=1.0,
            details={
                "source_device": source_device,
                "target_device": target_device,
                "anima_state": anima_state,
                "eisv_mapped": eisv_mapped,
                "sync_direction": sync_direction,
                **(details or {})
            }
        )
        self._write_entry(entry)


    def log_concurrent_session_binding_observed(
        self,
        *,
        session_key_prefix: str,
        candidate_fingerprint_prefix: str,
        client_hint: Optional[str],
        model_type: Optional[str],
    ) -> None:
        """S13 (2026-04-25) — passive observation when derive_session_key step 7
        IP:UA pin lookup matches. Distinct from the active-alert
        identity_hijack_suspected event: this fires whenever a pin match
        happens (even with proof signal), to build the dataset of
        multi-agent-on-same-machine concurrency. Hijack alerts remain the
        active-attack signal; this event is the observation channel.
 S13.
        """
        entry = AuditEntry(
            timestamp=datetime.now().isoformat(),
            agent_id=None,
            event_type="concurrent_session_binding_observed",
            confidence=1.0,
            details={
                "session_key_prefix": session_key_prefix,
                "candidate_fingerprint_prefix": candidate_fingerprint_prefix,
                "client_hint": client_hint,
                "model_type": model_type,
            },
        )
        self._write_entry(entry)

    def log_identity_resolution_observed(
        self,
        *,
        agent_uuid: Optional[str],
        resolution_source: Optional[str],
        pin_match_scope: Optional[str] = None,
        pin_entry_present: Optional[bool] = None,
        pin_fingerprint_match: Optional[bool] = None,
        pin_entry_age_seconds: Optional[int] = None,
        token_iat: Optional[int] = None,
        token_exp: Optional[int] = None,
        token_age_seconds: Optional[int] = None,
    ) -> None:
        """Record one observation per onboard/resume identity resolution.

        Fields answer "what won, and what would the pin path have done if
        we'd checked it?" — needed to discriminate the masking hypothesis
        for the 30-min sliding pin TTL from the alternative explanations
        (pin expired absolutely / fingerprint legitimately changed).

        ``pin_*`` fields populated when a shadow lookup ran (winning path was
        not the pin itself). ``token_*`` fields populated when a continuity
        token was presented at resume. ``agent_uuid`` may be None when the
        resolution did not bind to a known agent (e.g., onboard that minted
        a fresh identity has it set; pre-bind diagnostics may not).
        """
        entry = AuditEntry(
            timestamp=datetime.now().isoformat(),
            agent_id=agent_uuid,
            event_type="identity_resolution_observed",
            confidence=1.0,
            details={
                "resolution_source": resolution_source,
                "pin_match_scope": pin_match_scope,
                "pin_entry_present": pin_entry_present,
                "pin_fingerprint_match": pin_fingerprint_match,
                "pin_entry_age_seconds": pin_entry_age_seconds,
                "token_iat": token_iat,
                "token_exp": token_exp,
                "token_age_seconds": token_age_seconds,
            },
        )
        self._write_entry(entry)

    def log_session_resolve_miss_observed(
        self,
        *,
        session_key: str,
        resolution_source: Optional[str],
        reason: str,
        resume: bool,
        force_new: bool,
        token_agent_uuid_present: bool,
        client_hint: Optional[str] = None,
        model_type: Optional[str] = None,
    ) -> None:
        """Record PATH 2 fail-closed misses as structured audit telemetry.

        S21-b/M7: the log line is useful for operators watching one process,
        but audit reconstruction needs a queryable event keyed to the rejected
        session. This event is intentionally separate from
        concurrent_session_binding_observed: a missing session row is not, by
        itself, evidence of a concurrent binding.
        """
        entry = AuditEntry(
            timestamp=datetime.now().isoformat(),
            agent_id=None,
            event_type="session_resolve_miss_observed",
            confidence=1.0,
            session_id=session_key,
            details={
                "session_key_prefix": session_key[:20],
                "resolution_source": resolution_source,
                "reason": reason,
                "resume": resume,
                "force_new": force_new,
                "token_agent_uuid_present": token_agent_uuid_present,
                "client_hint": client_hint,
                "model_type": model_type,
            },
        )
        self._write_entry(entry)

    def log_continuity_token_deprecated_accept(
        self,
        *,
        agent_id: str,
        caller_channel: Optional[str],
        caller_model_type: Optional[str],
        issued_at: int,
        accepted_at: int,
        agent_uuid: str,
    ) -> None:
        """S1-a (2026-04-24) — grace-period telemetry for cross-instance token resume.

        Fires when a caller invokes onboard() with a continuity_token and
        without force_new=true. The retired cross-process-instance resume path.
        """
        lifetime_seconds = max(0, int(accepted_at) - int(issued_at))
        entry = AuditEntry(
            timestamp=datetime.now().isoformat(),
            agent_id=agent_id,
            event_type="continuity_token_deprecated_accept",
            confidence=1.0,
            details={
                "caller_channel": caller_channel,
                "caller_model_type": caller_model_type,
                "issued_at": int(issued_at),
                "accepted_at": int(accepted_at),
                "lifetime_seconds": lifetime_seconds,
                "agent_uuid": agent_uuid,
            },
        )
        self._write_entry(entry)

    def log_mirror_signal_emit(
        self,
        *,
        agent_id: Optional[str],
        update_index: Optional[int],
        response_mode: Optional[str],
        surfaced: bool,
        signals: List[Dict],
    ) -> None:
        """Phase 0 mirror-effectiveness instrumentation (mirror-effectiveness-measurement-v0).

        Records, per check-in that fired at least one structured mirror signal,
        what fired and whether the agent actually saw it. ``surfaced`` is the
        lever: it is True only when the resolved ``response_mode`` was ``mirror``.
        Agents on minimal/compact/standard/full compute the same signals (the
        enrichment runs before mode filtering) but never see them — they are the
        natural shadow control the Phase 1 analysis joins against.

        Each entry in ``signals`` is a structured trigger record:
        ``{"signal_type", "metric", "value", "threshold", ...}``.
        """
        entry = AuditEntry(
            timestamp=datetime.now().isoformat(),
            agent_id=agent_id,
            event_type="mirror_signal.emit",
            confidence=1.0,
            details={
                "update_index": update_index,
                "response_mode": response_mode,
                "surfaced": bool(surfaced),
                "signals": signals,
            },
        )
        self._write_entry(entry)

    def _write_entry(self, entry: AuditEntry):
        """Write audit entry to JSONL log file with locking.

        Postgres persistence is intentionally fire-and-forget: JSONL is the
        durable local truth, and DB write loss is accepted in exchange for
        keeping audit logging off latency-sensitive handler paths.
        """
        entry_dict = asdict(entry)
        try:
            # Raw truth: JSONL append
            if self._jsonl_enabled:
                with open(self.log_file, 'a') as f:
                    # Acquire exclusive lock
                    fcntl.flock(f.fileno(), fcntl.LOCK_EX)
                    try:
                        json.dump(entry_dict, f)
                        f.write('\n')
                        f.flush()
                        os.fsync(f.fileno())
                    finally:
                        fcntl.flock(f.fileno(), fcntl.LOCK_UN)
        except Exception as e:
            # Don't crash on audit log failures
            logger.warning(f"Could not write audit log: {e}", exc_info=True)

        # Fire-and-forget Postgres write; see docstring for the loss/latency
        # tradeoff. The task is pinned in `_inflight_pg_audit_tasks` until
        # done so CPython GC can't collect it mid-await — bare create_task
        # is a documented P001 hazard (Watcher #69f2ccbc, 2026-05-18).
        try:
            import asyncio
            try:
                loop = asyncio.get_running_loop()
                # In event loop thread — schedule directly
                _spawn_pg_audit_task(loop, self._write_to_postgres(entry_dict))
            except RuntimeError:
                # In executor thread — schedule back to main loop
                loop = self.__class__._event_loop
                if loop is not None and loop.is_running():
                    coro = self._write_to_postgres(entry_dict)
                    loop.call_soon_threadsafe(_spawn_pg_audit_task, loop, coro)
        except Exception:
            pass  # No event loop at all (tests, CLI)

    async def _write_to_postgres(self, entry_dict: dict):
        """Fire-and-forget Postgres audit write."""
        try:
            from src.audit_db import append_audit_event_async
            await append_audit_event_async(entry_dict)
        except Exception as e:
            logger.warning(f"Postgres audit write failed (non-fatal): {e}")

    def rotate_log(self, max_age_days: int = 30):
        """
        Rotate audit log: archive old entries, keep recent ones.
        
        Args:
            max_age_days: Keep entries newer than this many days
        """
        if not self.log_file.exists():
            return
        
        from datetime import timedelta
        cutoff_time = datetime.now() - timedelta(days=max_age_days)
        
        # Create archive directory
        archive_dir = self.log_file.parent / "audit_log_archive"
        archive_dir.mkdir(exist_ok=True)
        
        # Archive old entries
        archived_file = archive_dir / f"audit_log_{datetime.now().strftime('%Y%m%d')}.jsonl"
        recent_entries = []
        
        try:
            with open(self.log_file, 'r') as f:
                for line in f:
                    try:
                        entry_dict = json.loads(line.strip())
                        entry_time = _parse_ts_naive(entry_dict['timestamp'])

                        if entry_time < cutoff_time:
                            # Archive old entry
                            with open(archived_file, 'a') as af:
                                af.write(line)
                        else:
                            # Keep recent entry
                            recent_entries.append(line)
                    except (json.JSONDecodeError, KeyError, ValueError):
                        continue
            
            # Rewrite log file with only recent entries
            with open(self.log_file, 'w') as f:
                f.writelines(recent_entries)
            
            return len(recent_entries), archived_file
        except Exception as e:
            logger.warning(f"Could not rotate log: {e}", exc_info=True)
            return None, None
    
    def query_audit_log(self, agent_id: Optional[str] = None,
                       event_type: Optional[str] = None,
                       start_time: Optional[str] = None,
                       end_time: Optional[str] = None,
                       limit: int = 1000) -> List[Dict]:
        """
        Query audit log with filters.
        
        Args:
            agent_id: Filter by agent ID
            event_type: Filter by event type ("lambda1_skip", "auto_attest", "calibration_check")
            start_time: ISO format timestamp (inclusive)
            end_time: ISO format timestamp (inclusive)
            limit: Maximum number of entries to return
        """
        if not self.log_file.exists():
            return []
        
        results = []

        try:
            start_dt = _parse_ts_naive(start_time) if start_time else None
            end_dt = _parse_ts_naive(end_time) if end_time else None

            for line in _iter_jsonl_reverse(self.log_file):
                if len(results) >= limit:
                    break
                try:
                    entry_dict = json.loads(line)
                    entry_ts = entry_dict.get('timestamp')
                    entry_time = _parse_ts_naive(entry_ts) if entry_ts else None

                    if start_dt and entry_time is not None and entry_time < start_dt:
                        break

                    if agent_id and entry_dict.get('agent_id') != agent_id:
                        continue
                    if event_type and entry_dict.get('event_type') != event_type:
                        continue
                    if end_dt and entry_time is not None and entry_time > end_dt:
                        continue

                    results.append(entry_dict)
                except (json.JSONDecodeError, KeyError):
                    continue
        except Exception as e:
            logger.warning(f"Could not query audit log: {e}", exc_info=True)
            return []

        return results
    
    def get_skip_rate_metrics(self, agent_id: Optional[str] = None, 
                             window_hours: int = 24) -> Dict:
        """Calculate skip rate metrics from audit log"""
        from datetime import timedelta
        cutoff_time = datetime.now() - timedelta(hours=window_hours)

        if not self.log_file.exists():
            return {
                "total_skips": 0,
                "total_updates": 0,
                "skip_rate": 0.0,
                "avg_confidence": 0.0,
                "suspicious": False
            }
        
        total_skips = 0
        total_updates = 0
        confidence_sum = 0.0
        confidence_count = 0
        
        try:
            for line in _iter_jsonl_reverse(self.log_file):
                try:
                    entry_dict = json.loads(line)
                    entry_time = _parse_ts_naive(entry_dict['timestamp'])

                    if entry_time < cutoff_time:
                        break

                    if agent_id and entry_dict['agent_id'] != agent_id:
                        continue

                    if entry_dict['event_type'] == 'lambda1_skip':
                        total_skips += 1
                        confidence_sum += entry_dict['confidence']
                        confidence_count += 1
                    elif entry_dict['event_type'] == 'auto_attest':
                        total_updates += 1
                except (json.JSONDecodeError, KeyError):
                    continue
        except Exception as e:
            logger.warning(f"Could not read audit log: {e}", exc_info=True)
            return {"error": str(e)}
        
        avg_confidence = confidence_sum / confidence_count if confidence_count > 0 else 0.0
        skip_rate = total_skips / (total_skips + total_updates) if (total_skips + total_updates) > 0 else 0.0
        
        # Suspicious pattern: low skip rate but low average confidence (configurable thresholds)
        from config.governance_config import config
        suspicious = (skip_rate < config.SUSPICIOUS_LOW_SKIP_RATE and 
                     avg_confidence < config.SUSPICIOUS_LOW_CONFIDENCE and 
                     total_skips + total_updates > 10)
        
        return {
            "total_skips": total_skips,
            "total_updates": total_updates,
            "skip_rate": skip_rate,
            "avg_confidence": avg_confidence,
            "suspicious": suspicious,
            "window_hours": window_hours
        }


# Global audit logger instance
audit_logger = AuditLogger()


def get_audit_log() -> AuditLogger:
    """Return the process-global :class:`AuditLogger` instance."""
    return audit_logger
