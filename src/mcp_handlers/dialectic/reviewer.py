"""
Dialectic Reviewer Selection

Handles selecting appropriate reviewer agents for dialectic sessions.
Implements collusion prevention and expertise matching.

NOTE: Cross-process visibility is now provided by PostgreSQL (dialectic_db.py).
The in-memory ACTIVE_SESSIONS dict is kept for backward compat but PostgreSQL
is the source of truth for queries that need cross-process visibility.
"""

from typing import Dict, Any, List, Optional
from datetime import datetime, timedelta
import os
import random
import json
import asyncio
import contextvars

from src.dialectic_protocol import calculate_authority_score, DialecticPhase, DialecticSession
from src.logging_utils import get_logger

# Reentrancy guard for the auto-resolve pre-check inside is_agent_in_active_session.
# select_reviewer() calls is_agent_in_active_session() once per candidate, and
# auto_resolve_stuck_sessions() calls select_reviewer() — so without this guard a
# single submit_antithesis first-responder check fans out to O(fleet_size) stuck-
# session PG scans once UNITARES_AUTOSELECT_REVIEWER is enabled. ContextVar scopes
# the guard per asyncio task-tree, so concurrent requests don't suppress each other.
_AUTO_RESOLVE_IN_PROGRESS = contextvars.ContextVar(
    "_dialectic_auto_resolve_in_progress", default=False
)
from .session import (
    SESSION_STORAGE_DIR,
    ACTIVE_SESSIONS,
    _SESSION_METADATA_CACHE,
    _CACHE_TTL
)
from src.mcp_handlers.shared import lazy_mcp_server as mcp_server
# Import PostgreSQL async functions for cross-process visibility
from src.dialectic_db import (
    is_agent_in_active_session_async as pg_is_agent_in_active_session,
    has_recently_reviewed_async as pg_has_recently_reviewed,
)

logger = get_logger(__name__)


def _autoselect_enabled() -> bool:
    """Gate for reviewer auto-selection.

    Off by default. The candidate pool is dominated by ephemeral sessions that
    are still flagged active in metadata long after their process exited, plus
    resident agents that are deterministic scripts (pytest, threshold checks,
    regex matching) and cannot perform dialectic reasoning. Assigning either
    kind as a reviewer is dishonest. Callers receiving ``None`` fall through
    to self-review, awaiting-facilitation, or an explicit NO_REVIEWER error.

    Set ``UNITARES_AUTOSELECT_REVIEWER=1`` once a real reasoner is wired in
    (e.g. call_model with a capable backend, or a live pool of summonable
    agents).
    """
    return os.environ.get("UNITARES_AUTOSELECT_REVIEWER", "").lower() in (
        "1", "true", "yes", "on",
    )

async def _has_recently_reviewed(reviewer_id: str, paused_agent_id: str, hours: int = 24) -> bool:
    """
    Check if reviewer has recently reviewed the paused agent - ASYNC to prevent blocking.

    Prevents collusion by ensuring reviewers don't repeatedly review the same agent.

    Uses PostgreSQL for cross-process visibility (CLI and SSE can see each other's sessions).

    Args:
        reviewer_id: Potential reviewer agent ID
        paused_agent_id: Paused agent ID
        hours: Time window in hours (default: 24)

    Returns:
        True if reviewer reviewed paused agent within the time window, False otherwise
    """
    # PRIMARY: Use PostgreSQL for cross-process visibility
    try:
        return await pg_has_recently_reviewed(reviewer_id, paused_agent_id, hours)
    except Exception as e:
        logger.warning(f"PostgreSQL check failed for _has_recently_reviewed, falling back to disk: {e}")

    # FALLBACK: Check JSON files on disk (backward compat)
    cutoff_time = datetime.now() - timedelta(hours=hours)

    try:
        loop = asyncio.get_running_loop()

        def _check_sessions_sync():
            """Synchronous session check - runs in executor"""
            SESSION_STORAGE_DIR.mkdir(parents=True, exist_ok=True)

            if not SESSION_STORAGE_DIR.exists():
                return False

            session_files = sorted(SESSION_STORAGE_DIR.glob("*.json"),
                                     key=lambda p: p.stat().st_mtime,
                                     reverse=True)[:100]

            for session_file in session_files:
                try:
                    with open(session_file, 'r') as f:
                        session_data = json.load(f)

                    if (session_data.get('reviewer_agent_id') == reviewer_id and
                        session_data.get('paused_agent_id') == paused_agent_id):
                        # Count ALL session outcomes — not just resolved — to prevent
                        # a reviewer from bypassing cooldown by deliberately failing sessions.
                        created_at_str = session_data.get('created_at')
                        if created_at_str:
                            try:
                                created_at = datetime.fromisoformat(created_at_str.replace('Z', '+00:00'))
                                if created_at >= cutoff_time:
                                    return True
                            except (ValueError, AttributeError):
                                continue
                except (json.JSONDecodeError, KeyError, IOError, OSError) as e:
                    logger.debug(f"Skipping unreadable session file: {e}")
                    continue
            return False

        return await loop.run_in_executor(None, _check_sessions_sync)
    except Exception as e:
        logger.warning(f"Fallback disk check failed for _has_recently_reviewed: {e}")
        return False

async def is_agent_in_active_session(agent_id: str) -> bool:
    """
    Check if agent is already participating in an active dialectic session - ASYNC to prevent blocking.

    Prevents recursive assignment where an agent reviewing someone else
    gets assigned as a reviewer in another session.

    Uses PostgreSQL for cross-process visibility (CLI and SSE can see each other's sessions).
    Falls back to in-memory + disk for backward compat.

    QUICK WIN A: Auto-resolves stuck sessions before checking to prevent false positives.

    Args:
        agent_id: Agent ID to check

    Returns:
        True if agent is in an active session (as paused agent or reviewer), False otherwise
    """
    import time
    
    # QUICK WIN A: Auto-resolve stuck sessions before checking
    # This prevents "session conflict" errors when sessions are actually stuck.
    # Reentrancy-guarded: auto_resolve -> select_reviewer -> is_agent_in_active_session
    # would otherwise recurse back here once per candidate. Run the pre-check at
    # most once per asyncio task-tree.
    if not _AUTO_RESOLVE_IN_PROGRESS.get():
        token = _AUTO_RESOLVE_IN_PROGRESS.set(True)
        try:
            from src.mcp_handlers.dialectic.auto_resolve import check_and_resolve_stuck_sessions
            resolution_result = await check_and_resolve_stuck_sessions()
            if resolution_result.get("resolved_count", 0) > 0:
                logger.info(f"Auto-resolved {resolution_result['resolved_count']} stuck session(s) before checking active sessions")
        except Exception as e:
            # Best-effort: don't block reviewer selection if auto-resolve fails
            logger.warning(f"Auto-resolve pre-check failed in is_agent_in_active_session: {e}")
        finally:
            _AUTO_RESOLVE_IN_PROGRESS.reset(token)

    # PRIMARY: Use PostgreSQL for cross-process visibility
    # This is the key fix - CLI and SSE processes now share session state
    try:
        result = await pg_is_agent_in_active_session(agent_id)
        if result:
            # Update local cache for faster repeated lookups
            _SESSION_METADATA_CACHE[agent_id] = {
                'in_session': True,
                'timestamp': time.time(),
                'session_ids': []  # Could query for IDs if needed
            }
            return True
        # CRITICAL: If PostgreSQL says "not active", override any stale local cache.
        # Otherwise, we can incorrectly treat RESOLVED sessions as active for _CACHE_TTL.
        _SESSION_METADATA_CACHE[agent_id] = {
            'in_session': False,
            'timestamp': time.time(),
            'session_ids': []
        }
    except Exception as e:
        logger.warning(f"PostgreSQL check failed for is_agent_in_active_session, falling back: {e}")

    # FALLBACK Step 1: Check in-memory sessions (process-local cache)
    for session in ACTIVE_SESSIONS.values():
        if (session.paused_agent_id == agent_id or
            session.reviewer_agent_id == agent_id):
            if session.phase not in [DialecticPhase.RESOLVED, DialecticPhase.FAILED]:
                _SESSION_METADATA_CACHE[agent_id] = {
                    'in_session': True,
                    'timestamp': time.time(),
                    'session_ids': [session.session_id]
                }
                return True

    # FALLBACK Step 2: Check cache
    cache_key = agent_id
    if cache_key in _SESSION_METADATA_CACHE:
        cached = _SESSION_METADATA_CACHE[cache_key]
        cache_age = time.time() - cached['timestamp']

        if cache_age < _CACHE_TTL:
            return cached['in_session']
        else:
            del _SESSION_METADATA_CACHE[cache_key]

    # FALLBACK Step 3: Check disk sessions (JSON files)
    try:
        loop = asyncio.get_running_loop()

        def _check_disk_sessions_sync():
            """Synchronous disk check - runs in executor"""
            SESSION_STORAGE_DIR.mkdir(parents=True, exist_ok=True)

            if not SESSION_STORAGE_DIR.exists():
                return False, []

            session_files = sorted(SESSION_STORAGE_DIR.glob("*.json"),
                                     key=lambda p: p.stat().st_mtime,
                                     reverse=True)[:50]

            found_sessions = []
            for session_file in session_files:
                try:
                    with open(session_file, 'r') as f:
                        session_data = json.load(f)

                    if (session_data.get('paused_agent_id') == agent_id or
                        session_data.get('reviewer_agent_id') == agent_id):
                        phase = session_data.get('phase')
                        if phase not in ['resolved', 'failed', 'escalated']:
                            found_sessions.append(session_data.get('session_id', session_file.stem))
                            return True, found_sessions
                except (json.JSONDecodeError, KeyError, IOError, OSError) as e:
                    logger.debug(f"Skipping unreadable session file: {e}")
                    continue

            return False, []

        found, found_sessions = await loop.run_in_executor(None, _check_disk_sessions_sync)

        if found:
            _SESSION_METADATA_CACHE[agent_id] = {
                'in_session': True,
                'timestamp': time.time(),
                'session_ids': found_sessions
            }
            return True

        _SESSION_METADATA_CACHE[agent_id] = {
            'in_session': False,
            'timestamp': time.time(),
            'session_ids': []
        }
    except Exception as e:
        logger.warning(f"Fallback disk check failed for is_agent_in_active_session: {e}")
        _SESSION_METADATA_CACHE[agent_id] = {
            'in_session': False,
            'timestamp': time.time(),
            'session_ids': []
        }

    return False

async def select_reviewer(paused_agent_id: str,
                   metadata: Dict[str, Any] = None,
                   paused_agent_state: Dict[str, Any] = None,
                   paused_agent_tags: List[str] = None,
                   exclude_agent_ids: List[str] = None) -> Optional[str]:
    """
    Select a reviewer agent for dialectic session using authority scoring.

    Eligibility filters:
    - Not the paused agent
    - Not in exclude_agent_ids list
    - Not already in another active session
    - Not recently reviewed this agent (prevent collusion)
    - Status is 'active'
    - Active within last 24 hours (prevents assigning stale agents)

    Ranking: authority_score (health 40%, track record 30%, domain 20%, freshness 10%)
    with weighted random selection from top candidates.

    Returns:
        Selected reviewer agent_id, or None if no reviewer available
    """
    if not _autoselect_enabled():
        logger.info(
            "[DIALECTIC] Reviewer auto-select disabled "
            "(UNITARES_AUTOSELECT_REVIEWER unset). Returning None — caller "
            "should self-review, await facilitation, or accept manual assignment."
        )
        return None

    if not metadata or not isinstance(metadata, dict):
        return None

    candidates = []
    recency_cutoff = datetime.now() - timedelta(hours=24)

    for agent_id, agent_meta in metadata.items():
        if not isinstance(agent_id, str):
            continue

        if agent_id == paused_agent_id:
            continue

        if exclude_agent_ids and agent_id in exclude_agent_ids:
            continue

        if isinstance(agent_meta, str) or agent_meta is None:
            continue

        status = agent_meta.get('status') if isinstance(agent_meta, dict) else getattr(agent_meta, 'status', None)
        if status and status != 'active':
            continue

        # Skip stale agents — must have checked in within 24h to be a viable reviewer
        last_update = agent_meta.get('last_update') if isinstance(agent_meta, dict) else getattr(agent_meta, 'last_update', None)
        if last_update:
            try:
                last_dt = datetime.fromisoformat(str(last_update).replace('Z', '+00:00'))
                if last_dt.tzinfo:
                    from datetime import timezone
                    recency_cutoff_tz = recency_cutoff.replace(tzinfo=timezone.utc)
                    if last_dt < recency_cutoff_tz:
                        continue
                elif last_dt < recency_cutoff:
                    continue
            except (ValueError, TypeError):
                pass  # Unparseable timestamp — allow but deprioritize via authority score

        if await is_agent_in_active_session(agent_id):
            continue

        if await _has_recently_reviewed(agent_id, paused_agent_id, hours=24):
            continue

        # Calculate authority score for ranking
        meta_dict = agent_meta if isinstance(agent_meta, dict) else {}
        if not isinstance(agent_meta, dict):
            meta_dict = {}
            for attr in ('tags', 'total_reviews', 'successful_reviews', 'last_update'):
                val = getattr(agent_meta, attr, None)
                if val is not None:
                    meta_dict[attr] = val
        if paused_agent_tags:
            meta_dict['paused_agent_tags'] = paused_agent_tags

        try:
            score = calculate_authority_score(meta_dict, paused_agent_state)
        except Exception:
            score = 0.5  # Neutral fallback

        candidates.append((agent_id, score))

    if not candidates:
        return None

    # Weighted random selection: higher authority = higher chance
    # Sort by score descending, take top 5, then weighted random
    candidates.sort(key=lambda x: x[1], reverse=True)
    top = candidates[:5]

    total_score = sum(s for _, s in top)
    if total_score <= 0:
        return random.choice(top)[0]

    # Weighted random pick
    r = random.random() * total_score
    cumulative = 0.0
    for agent_id, score in top:
        cumulative += score
        if r <= cumulative:
            return agent_id

    return top[0][0]


