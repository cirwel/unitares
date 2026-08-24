"""
Agent monitor state management.

Monitor instances, state file I/O (save/load), per-agent state persistence.
"""

from __future__ import annotations

import os
import json
import math
import tempfile
import time
import fcntl
import asyncio
from pathlib import Path

from src.logging_utils import get_logger
from src.agent_metadata_model import project_root
from src.governance_monitor import UNITARESMonitor

logger = get_logger(__name__)

# Budget for the file-write executor await. If the anyio task group stalls
# the event loop (see docs anyio-deadlock note), we'd rather degrade than
# hang the handler. hydrate_from_db_if_fresh heals the missed save on the
# next monitor load, so dropping one write is no longer catastrophic.
STATE_SAVE_TIMEOUT_SECONDS = 2.0

# Store monitors per agent (shared mutable dict)
monitors: dict[str, UNITARESMonitor] = {}


def get_state_file(agent_id: str) -> Path:
    """
    Get path to state file for an agent.

    Uses organized structure: data/agents/{agent_id}_state.json

    Provides automatic migration: if file exists in old location (data/ root),
    it will be automatically moved to new location on first access.
    """
    new_path = Path(project_root) / "data" / "agents" / f"{agent_id}_state.json"
    old_path = Path(project_root) / "data" / f"{agent_id}_state.json"

    if not new_path.exists() and old_path.exists():
        try:
            new_path.parent.mkdir(parents=True, exist_ok=True)
            old_path.rename(new_path)
            logger.info(f"Migrated {agent_id} state file to agents/ subdirectory")
        except Exception as e:
            logger.warning(f"Could not migrate {agent_id} state file: {e}", exc_info=True)
            return old_path

    return new_path


def _write_state_file(state_file: Path, state_data: dict) -> None:
    """Atomically write the state file.

    Write to a tempfile in the same directory, fsync, then os.replace onto
    the final path. Prevents zero-byte / truncated files if the process is
    killed mid-write (which would leave loaders seeing a corrupt JSON and
    silently falling back to None).
    """
    state_file.parent.mkdir(parents=True, exist_ok=True)
    tmp_fd, tmp_path = tempfile.mkstemp(
        dir=state_file.parent,
        prefix=f".{state_file.stem}.",
        suffix=".tmp",
    )
    try:
        with os.fdopen(tmp_fd, 'w') as f:
            json.dump(state_data, f, indent=2)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_path, state_file)
    except Exception:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise


def _snapshot_governor_state(monitor: UNITARESMonitor) -> None:
    """Snapshot AdaptiveGovernor state into GovernanceState for persistence."""
    gov = getattr(monitor, 'adaptive_governor', None)
    if gov is not None and hasattr(gov, 'state'):
        monitor.state._governor_state_dict = gov.state.to_dict()
    else:
        monitor.state._governor_state_dict = None


def _attach_behavioral_state(monitor: UNITARESMonitor, state_data: dict) -> None:
    """Merge the behavioral EISV EMA into the persisted snapshot.

    The load path (``GovernanceMonitor.load_persisted_state``) already restores a
    ``behavioral_eisv`` block via ``BehavioralEISV.from_dict``, but the live save
    path historically serialized only the ODE state — so behavioral confidence reset
    to ODE-fallback on every process restart (0/702 state files carried it). This
    makes save symmetric with load. ``to_dict_with_history``/``from_dict`` round-trip
    update_count, EMA values, histories, and the Welford baseline faithfully.
    Telemetry serialization must never break a state save, so it is fail-open.
    """
    beh = getattr(monitor, "_behavioral_state", None)
    if beh is None:
        return
    try:
        state_data["behavioral_eisv"] = beh.to_dict_with_history()
        # Which source fed the last behavioral observation. `load_persisted_state`
        # does not read this back into an attribute, but it is what tells an
        # operator whether a restored snapshot's vector came from a physical
        # sensor or the behavioral one.
        obs_source = getattr(monitor, "_behavioral_obs_source", None)
        if obs_source:
            state_data["behavioral_eisv"]["obs_source"] = obs_source
    except Exception:  # noqa: BLE001 — never let a snapshot serialization break the save
        logger.debug("Behavioral state serialization skipped during save", exc_info=True)


def _attach_resolved_authority(monitor: UNITARESMonitor, state_data: dict) -> None:
    """Persist the exact risk/verdict pair from the last real decision.

    ``GovernanceState.risk_history`` is Φ-derived telemetry.  Keeping the
    resolved pair at monitor level prevents a restart from promoting that
    telemetry into the headline decision risk.  Simulations restore these
    attributes before the live writer runs, so only real updates are durable.
    """
    try:
        risk = getattr(monitor, "_last_resolved_risk", None)
        if risk is not None:
            state_data["resolved_risk"] = float(risk)
        verdict = getattr(monitor, "_last_resolved_verdict", None)
        if verdict:
            state_data["resolved_verdict"] = str(verdict)
    except Exception:  # noqa: BLE001 — state persistence must remain fail-open
        logger.debug("Resolved authority serialization skipped during save", exc_info=True)


def _attach_monitor_transients(monitor: UNITARESMonitor, state_data: dict) -> None:
    """Merge the monitor-level fields ``load_persisted_state`` already restores.

    Same asymmetry `_attach_behavioral_state` fixed for the EMA, one layer up.
    `GovernanceMonitor.load_persisted_state` pops four keys off the state file —
    ``sensor_divergence``, ``sensor_divergence_history``, ``created_at_iso`` and
    ``last_update_iso`` — but the live save path never wrote any of them, so all
    four restored as absent on every process start. (`save_persisted_state` on
    the monitor does write them; nothing calls it. This is the live writer.)

    The divergence history is the one that matters. Per-source coupling
    (`UNITARES_SENSOR_COUPLING`) cuts an embodied agent's sensor out of the ODE
    spring on the stated grounds that "sustained disagreement is itself the
    signal" — but with the trend resetting on every restart, the evidence window
    was bounded by process uptime rather than by `SENSOR_DIVERGENCE_HISTORY_MAX`.

    Fail-open: this is telemetry on the mandatory check-in path, same posture as
    `_record_sensor_divergence` itself.
    """
    try:
        last_div = getattr(monitor, "_last_sensor_divergence", None)
        if last_div is not None:
            state_data["sensor_divergence"] = last_div

        div_hist = getattr(monitor, "_sensor_divergence_history", None)
        if div_hist:
            state_data["sensor_divergence_history"] = list(div_hist)

        created_at = getattr(monitor, "created_at", None)
        if created_at is not None:
            state_data["created_at_iso"] = created_at.isoformat()

        last_update = getattr(monitor, "last_update", None)
        if last_update is not None:
            state_data["last_update_iso"] = last_update.isoformat()
    except Exception:  # noqa: BLE001 — never let a snapshot serialization break the save
        logger.debug("Monitor transient serialization skipped during save", exc_info=True)


async def save_monitor_state_async(agent_id: str, monitor: UNITARESMonitor) -> None:
    """
    Async version of save_monitor_state - uses file-based storage.

    Uses async file locking to avoid blocking the event loop.
    """
    _snapshot_governor_state(monitor)
    state_data = monitor.state.to_dict_with_history()
    _attach_behavioral_state(monitor, state_data)
    _attach_resolved_authority(monitor, state_data)
    _attach_monitor_transients(monitor, state_data)

    state_file = get_state_file(agent_id)
    state_file.parent.mkdir(parents=True, exist_ok=True)

    state_lock_file = state_file.parent / f".{agent_id}_state.lock"

    lock_fd = None
    try:
        lock_fd = os.open(str(state_lock_file), os.O_CREAT | os.O_RDWR)
        lock_acquired = False
        start_time = time.time()
        timeout = 5.0

        try:
            while time.time() - start_time < timeout:
                try:
                    fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    lock_acquired = True
                    break
                except IOError:
                    await asyncio.sleep(0.1)

            if not lock_acquired:
                logger.warning(f"State lock timeout for {agent_id} ({timeout}s)")
                raise TimeoutError("State lock timeout")

            loop = asyncio.get_running_loop()
            await asyncio.wait_for(
                loop.run_in_executor(None, _write_state_file, state_file, state_data),
                timeout=STATE_SAVE_TIMEOUT_SECONDS,
            )

        finally:
            if lock_fd is not None:
                try:
                    fcntl.flock(lock_fd, fcntl.LOCK_UN)
                except (IOError, OSError):
                    pass
                try:
                    os.close(lock_fd)
                except (OSError, ValueError):
                    pass
    except asyncio.TimeoutError:
        logger.warning(
            f"State file save for {agent_id} exceeded {STATE_SAVE_TIMEOUT_SECONDS}s "
            f"(likely anyio/asyncio executor stall); dropped this save, "
            f"monitor will rehydrate from DB on next load"
        )
    except Exception as e:
        logger.warning(f"Could not acquire state lock for {agent_id}: {e}", exc_info=True)
        try:
            loop = asyncio.get_running_loop()
            await asyncio.wait_for(
                loop.run_in_executor(None, _write_state_file, state_file, state_data),
                timeout=STATE_SAVE_TIMEOUT_SECONDS,
            )
        except asyncio.TimeoutError:
            logger.warning(
                f"Unlocked fallback save for {agent_id} also exceeded "
                f"{STATE_SAVE_TIMEOUT_SECONDS}s; dropped"
            )
        except Exception as fallback_error:
            logger.error(f"Failed to save state even without lock for {agent_id}: {fallback_error}", exc_info=True)


def save_monitor_state(agent_id: str, monitor: UNITARESMonitor) -> None:
    """Save monitor state to file with locking to prevent race conditions."""
    _snapshot_governor_state(monitor)
    state_data = monitor.state.to_dict_with_history()
    _attach_behavioral_state(monitor, state_data)
    _attach_resolved_authority(monitor, state_data)
    _attach_monitor_transients(monitor, state_data)

    state_file = get_state_file(agent_id)
    state_file.parent.mkdir(parents=True, exist_ok=True)

    state_lock_file = state_file.parent / f".{agent_id}_state.lock"

    lock_fd = None
    try:
        lock_fd = os.open(str(state_lock_file), os.O_CREAT | os.O_RDWR)
        lock_acquired = False
        start_time = time.time()
        timeout = 5.0

        try:
            while time.time() - start_time < timeout:
                try:
                    fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    lock_acquired = True
                    break
                except IOError:
                    time.sleep(0.1)

            if not lock_acquired:
                logger.warning(f"State lock timeout for {agent_id} ({timeout}s)")
                raise TimeoutError("State lock timeout")

            _write_state_file(state_file, state_data)

        finally:
            if lock_fd is not None:
                try:
                    fcntl.flock(lock_fd, fcntl.LOCK_UN)
                except (IOError, OSError):
                    pass
                try:
                    os.close(lock_fd)
                except (OSError, ValueError):
                    pass
    except Exception as e:
        logger.warning(f"Could not acquire state lock for {agent_id}: {e}", exc_info=True)
        try:
            _write_state_file(state_file, state_data)
        except Exception as e2:
            logger.error(f"Could not save state for {agent_id}: {e2}", exc_info=True)


def load_monitor_state(agent_id: str) -> 'GovernanceState | None':
    """Load monitor state from file if it exists."""
    from src.governance_state import GovernanceState

    state_file = get_state_file(agent_id)

    if not state_file.exists():
        return None

    try:
        with open(state_file, 'r') as f:
            data = json.load(f)
            state = GovernanceState.from_dict(data)
            return state
    except Exception as e:
        logger.warning(f"Could not load state for {agent_id}: {e}", exc_info=True)
        return None


def _restore_resolved_authority(monitor: UNITARESMonitor, state_json: object) -> bool:
    """Restore a validated final risk/verdict pair from durable row JSON."""
    if not isinstance(state_json, dict):
        return False

    restored = False
    raw_risk = state_json.get("risk_score")
    if raw_risk is not None:
        try:
            risk = float(raw_risk)
            if math.isfinite(risk) and 0.0 <= risk <= 1.0:
                monitor._last_resolved_risk = risk
                restored = True
        except (TypeError, ValueError):
            pass

    verdict = state_json.get("verdict")
    if verdict in {"safe", "caution", "high-risk"}:
        monitor._last_resolved_verdict = verdict
        restored = True
    return restored


async def hydrate_resolved_authority_if_missing(
    monitor: UNITARESMonitor,
    agent_id: str,
) -> bool:
    """Heal a legacy file snapshot whose decision-time pair was not persisted.

    Existing snapshots already carry EISV/history, so replacing that state from
    PostgreSQL would be unnecessary and lossy.  Read only the latest durable
    row and restore its final risk/verdict pair.  Never raises.
    """
    if (
        getattr(monitor, "_last_resolved_risk", None) is not None
        and getattr(monitor, "_last_resolved_verdict", None) is not None
    ):
        return False
    try:
        from src import agent_storage

        latest = await agent_storage.get_latest_agent_state(agent_id)
        if latest is None:
            return False
        restored = _restore_resolved_authority(
            monitor, getattr(latest, "state_json", None)
        )
        if restored:
            logger.info("Hydrated resolved risk/verdict from core.agent_state")
        return restored
    except Exception as exc:
        logger.warning(
            "Resolved authority hydration failed: %s", type(exc).__name__
        )
        return False


async def hydrate_from_db_if_fresh(monitor: UNITARESMonitor, agent_id: str) -> bool:
    """Rehydrate a fresh monitor from core.agent_state when the JSON file is missing.

    The file and the DB are two independent persistence paths. If a save
    attempt is dropped (anyio executor stall, crash mid-write, unwritten
    file for any reason), every subsequent server restart loads the monitor
    as update_count=0 — displayed to the user as "uninitialized" —
    even though the DB has the full history. This function closes that gap:
    when called on a fresh monitor, it populates EISV, coherence, regime,
    and rolling histories from the last ~50 DB records.

    Safe to call unconditionally — no-ops if the monitor already has updates
    or if the DB has no history for this agent. Never raises.

    Returns True if hydration actually applied new state.
    """
    # Defensive: some test paths hand us SimpleNamespace-style state objects
    # without the full GovernanceState interface. Treat missing update_count
    # as 0 (which means "attempt hydrate") rather than crashing the handler.
    if getattr(monitor.state, "update_count", 0) > 0:
        return False
    try:
        from src.db import get_db
        db = get_db()
        identity = await db.get_identity(agent_id)
        if identity is None:
            return False
        # Most-recent-first; we'll reverse for chronological history arrays.
        #
        # exclude_synthetic=True per onboard-bootstrap-checkin §4 inclusion-
        # exception + filter-audit site #6: the in-memory monitor's
        # E/I/S/V/coherence/regime/histories MUST NEVER be seeded from a
        # bootstrap row. Every downstream consumer of monitor.state
        # (self-recovery, dialectic, trajectory ODE prior-reads) treats
        # seeded values as measured. A bootstrap-only agent stays
        # update_count=0 here so those downstream paths' existing
        # "no measured trajectory yet" guards refuse-with-explanation.
        rows = await db.get_agent_state_history(
            identity_id=identity.identity_id, limit=50,
            exclude_synthetic=True,
        )
        if not rows:
            return False
        chrono = list(reversed(rows))
        latest = chrono[-1]
        latest_sj = getattr(latest, "state_json", None) or {}

        # The DB row stores the exact final decision-time pair.  Restore it
        # alongside EISV so get_metrics() cannot fall back to Φ history after a
        # JSON-snapshot-loss restart.
        _restore_resolved_authority(monitor, latest_sj)

        # Core EISV + coherence from latest row
        monitor.state.unitaires_state.E = float(latest.energy)
        monitor.state.unitaires_state.I = float(latest.integrity)
        monitor.state.unitaires_state.S = float(latest.entropy)
        monitor.state.unitaires_state.V = float(latest.void)
        monitor.state.coherence = float(latest.coherence)
        monitor.state.regime = str(latest.regime)

        # Rolling histories (capped at what we fetched; monitor trims itself on next update)
        monitor.state.E_history = [float(r.energy) for r in chrono]
        monitor.state.I_history = [float(r.integrity) for r in chrono]
        monitor.state.S_history = [float(r.entropy) for r in chrono]
        monitor.state.V_history = [float(r.void) for r in chrono]
        monitor.state.coherence_history = [float(r.coherence) for r in chrono]
        monitor.state.regime_history = [str(r.regime) for r in chrono]

        # decision_history rebuilt from state_json.action when present.
        # Rows written before record_agent_state's `action` parameter shipped
        # carry no action key — those rows are skipped, preserving a partial
        # replay rather than padding with placeholders. Pre-action-write rows
        # leave decision_history empty until the next live process_update
        # populates it, which observe surfaces as zero counts.
        # verdict_history rebuilt from state_json.verdict — that key has been
        # persisted since long before this change, so legacy rows DO replay.
        from src.agent_storage import extract_actions_verdicts
        actions, verdicts = extract_actions_verdicts(chrono)
        monitor.state.decision_history = actions
        monitor.state.verdict_history = verdicts

        # update_count gates the "uninitialized" display. Using len(chrono) is
        # a floor (true count may be higher — we only fetched 50); that's fine
        # since the gate is >0, and downstream consumers of update_count treat
        # it as "how much history have we seen" which is exactly len(chrono).
        monitor.state.update_count = len(chrono)

        # Restore the behavioral baseline from the latest row's state_json.
        # hydrate previously healed only the ODE state, leaving _behavioral_state
        # fresh (update_count=0) on every JSON-snapshot-loss restart — which kept
        # the entire fleet permanently is_baselined=false (2026-06-03 starvation).
        # record_agent_state now persists behavioral_eisv into state_json, so the
        # DB path is symmetric with the JSON path (PR #545). Fail-open.
        try:
            beh_blob = latest_sj.get("behavioral_eisv") if isinstance(latest_sj, dict) else None
            if isinstance(beh_blob, dict) and beh_blob:
                from src.behavioral_state import BehavioralEISV
                monitor._behavioral_state = BehavioralEISV.from_dict(beh_blob)
        except Exception:
            logger.debug("Behavioral baseline restore during hydrate skipped", exc_info=True)

        logger.info(
            "Hydrated monitor from core.agent_state: "
            "%s records, coherence=%.3f, regime=%s",
            len(chrono),
            monitor.state.coherence,
            monitor.state.regime,
        )
        # Confirmation counts never cross a restart/hydration boundary.  The
        # shadow gate therefore treats this lineage as uncertain and leaves any
        # pause intact, even though measurement state itself was restored.
        monitor._cold_start_confirmation_lineage_status = "db_hydrated_restart"
        return True
    except Exception as e:
        monitor._cold_start_confirmation_lineage_status = "hydration_uncertain"
        logger.warning("DB hydration failed: %s", type(e).__name__)
        return False


async def ensure_hydrated(monitor: UNITARESMonitor, agent_id: str) -> bool:
    """Drain the `_needs_hydration` mark set by `get_or_create_monitor`.

    Idempotent: returns immediately when the flag is unset (the common hot-path
    case). A fresh monitor receives full EISV/history hydration; a legacy file
    snapshot that already has state receives only its missing resolved
    risk/verdict pair. The flag is cleared regardless of outcome.

    Call this at the top of any async handler that reads `monitor.state`.
    Without this drain, monitors created cold from a missing snapshot would
    return seed-default EISV (the "uninitialized" symptom) on every read.

    The flag-and-drain split exists because `get_or_create_monitor` is sync
    (used from background tasks and CLI), but DB hydration is async. Sync
    callers that never reach an async drain see the same seed-default behavior
    they had before this fix — no regression.

    Returns True iff hydration actually applied new state.
    """
    # Strict identity check — `is True` (not truthy). The factory sets the flag
    # explicitly to True or False; any other value (None, MagicMock from tests,
    # absent attr) means "not marked" and we no-op. Without this, MagicMock
    # monitors in test fixtures fall through to hydrate and crash on
    # `MagicMock > 0` inside hydrate_from_db_if_fresh.
    if getattr(monitor, "_needs_hydration", False) is not True:
        return False
    try:
        if getattr(monitor.state, "update_count", 0) > 0:
            return await hydrate_resolved_authority_if_missing(monitor, agent_id)
        return await hydrate_from_db_if_fresh(monitor, agent_id)
    finally:
        # Single-shot: drain the mark even on hydrate failure (DB unreachable
        # etc.) so we don't retry on every subsequent read. Behavior degrades
        # to seed defaults — same as the pre-fix state.
        monitor._needs_hydration = False
