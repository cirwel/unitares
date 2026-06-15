"""GovernanceAgent — lifecycle base class for long-running UNITARES agents.

First-time resident bootstrap:
    UNITARES_FIRST_RUN=1 python3 -m agents.vigil  # or sentinel, watcher
This is the ONLY path that mints a new UUID for a resident with
refuse_fresh_onboard=True. Every other path must resume the stored
anchor UUID.
"""

from __future__ import annotations

import asyncio
import logging
import os
import signal
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from unitares_sdk.client import GovernanceClient
from unitares_sdk.errors import (
    GovernanceConnectionError,
    GovernanceTimeoutError,
    GovernanceUnavailableError,
    IdentityDriftError,
    VerdictError,
)
from unitares_sdk.models import CheckinResult
from unitares_sdk.utils import (
    load_json_state,
    notify,
    save_json_state,
    trim_log,
)

logger = logging.getLogger(__name__)

# Tag set stamped on every resident identity when persistent=True.
#   - 'persistent':  exempts from auto_archive_orphan_agents
#   - 'autonomous':  exempts from loop-detection pattern 4 (agent_loop_detection.py:216)
# Keep in sync with server-side KNOWN_RESIDENT_LABELS in grounding/class_indicator.py.
RESIDENT_TAGS: list[str] = ["persistent", "autonomous"]


@dataclass
class CycleResult:
    """Structured return from GovernanceAgent.run_cycle().

    Carries everything the base class needs for the post-cycle check-in.
    """

    summary: str
    complexity: float = 0.3
    confidence: float = 0.7
    response_mode: str = "compact"
    notes: list[tuple[str, list[str]]] | None = None

    @classmethod
    def simple(cls, summary: str) -> CycleResult:
        """Convenience: create a CycleResult with defaults for everything except summary."""
        return cls(summary=summary)


class GovernanceAgent:
    """Base class for long-running governance agents.

    Handles:
    - MCP connection lifecycle (per-cycle connect/disconnect)
    - Identity resolution (UUID from file -> server lookup, or fresh onboard)
    - Session persistence (atomic file writes)
    - Check-in after each cycle
    - Heartbeat when idle
    - Graceful shutdown via SIGTERM/SIGINT

    Subclass and implement ``run_cycle()``::

        class MyAgent(GovernanceAgent):
            async def run_cycle(self, client: GovernanceClient) -> CycleResult | None:
                # do work
                return CycleResult.simple("processed 5 items")
    """

    def __init__(
        self,
        name: str,
        mcp_url: str = "http://127.0.0.1:8767/mcp/",
        state_dir: Path | None = None,
        state_file: Path | None = None,
        session_file: Path | None = None,
        legacy_session_file: Path | None = None,
        notify_on_error: bool = True,
        timeout: float = 30.0,
        parent_agent_id: str | None = None,
        spawn_reason: str | None = None,
        persistent: bool = False,
        refuse_fresh_onboard: bool = False,
        cycle_timeout_seconds: float | None = None,
        log_file: Path | None = None,
        max_log_lines: int = 10_000,
        connect_timeout: float | None = None,
        connect_retries: int | None = None,
    ):
        self.name = name
        self.mcp_url = mcp_url
        self.timeout = timeout
        # Connect-handshake resilience, threaded to the per-cycle
        # GovernanceClient. None lets the client resolve its own defaults
        # (UNITARES_CONNECT_TIMEOUT / UNITARES_CONNECT_RETRIES env, else
        # 10s / 1). Residents whose cycle budget differs from the default
        # (e.g. a tighter cycle_timeout_seconds) can size these explicitly so
        # worst-case connect time still fits the cycle.
        self.connect_timeout = connect_timeout
        self.connect_retries = connect_retries
        # Hard cap on a single cycle in both run_once() and run_forever()
        # (connect + run_cycle + checkin + heartbeat-check). Used by
        # residents whose cycles can stall on an MCP session that never
        # finishes initialize. None = unbounded. Vigil and Sentinel
        # previously implemented this as an asyncio.wait_for wrapper in
        # their own run_once; hoisted here so subclasses don't reinvent it.
        self.cycle_timeout_seconds = cycle_timeout_seconds
        # Optional bounded log file. When set, the base class trims it to
        # max_log_lines after each run_once / run_forever iteration (via
        # finally so it fires on error and timeout too). Leave unset when
        # an external rotator (launchd StandardOutPath, logrotate) manages
        # the file.
        self.log_file = log_file
        self.max_log_lines = max_log_lines
        self.notify_on_error = notify_on_error
        # When True, stamp the "persistent" tag after fresh onboard so
        # auto_archive_orphan_agents (is_agent_protected in agent_lifecycle.py)
        # skips this identity. Resident agents (Vigil, Sentinel, etc.) should
        # set this to True to avoid sweep false-positives.
        self.persistent = persistent
        # Residents (Vigil, Sentinel, Watcher) set this True. When True,
        # _ensure_identity refuses to fresh-onboard if the anchor is
        # missing; the operator must set UNITARES_FIRST_RUN=1 to bootstrap
        # a new identity. Prevents the 2026-04-19 rotation-wipe silent-fork
        # class. See docs/superpowers/plans/2026-04-19-anchor-resilience-series.md
        self.refuse_fresh_onboard = refuse_fresh_onboard

        # Defaults based on name
        name_lower = name.lower()
        default_root = Path(__file__).resolve().parent.parent.parent.parent.parent
        self.state_dir = state_dir or default_root / "data" / name_lower
        # state_file overrides the default state_dir/state.json when the
        # caller wants a specific path (e.g. a versioned filename or a
        # non-default data root). When None, falls back to the old default.
        self.state_file = state_file
        # Host-scoped anchor default: one identity per role per host, shared
        # across every git worktree or install path (Watcher/Vigil/Sentinel
        # previously minted a new UUID per install-path-relative session
        # file). Subclasses override session_file+legacy_session_file pair.
        self.session_file = session_file or (
            Path.home() / ".unitares" / "anchors" / f"{name_lower}.json"
        )
        self.legacy_session_file = legacy_session_file

        # Opt-in lineage: when set, forwarded to the server on fresh onboard
        # so spawned agents are distinguishable from unrelated siblings.
        self.parent_agent_id = parent_agent_id
        self.spawn_reason = spawn_reason

        # Runtime state
        self.running = True
        self.client_session_id: str | None = None
        self.continuity_token: str | None = None
        self.agent_uuid: str | None = None
        self._last_checkin_time: float = 0.0
        # Resident tag reconciliation runs at most once per process. Tags don't
        # drift mid-process, so a per-cycle re-check would just add a
        # get_agent_metadata read every cycle (substrate-tax sensitive). Set
        # True once a read succeeds; left False on a read/write failure so the
        # next cycle retries. See _reconcile_resident_tags.
        self._resident_tags_reconciled = False

    # --- Subclass interface ---

    async def run_cycle(self, client: GovernanceClient) -> CycleResult | None:
        """One unit of work. Return CycleResult for check-in, or None to skip."""
        raise NotImplementedError

    async def on_after_checkin(
        self,
        client: GovernanceClient,
        checkin_result: CheckinResult,
        cycle_result: CycleResult,
    ) -> None:
        """Post-checkin extension hook. Override to log EISV, track state,
        or do any bookkeeping that needs the server's response.

        Called after a successful checkin AND after notes are posted, but
        before a ``pause`` or ``reject`` verdict raises ``VerdictError``.
        Runs on every verdict so state trackers see paused/rejected cycles
        too. Hook exceptions are logged and swallowed — a broken hook must
        not take down the cycle. When a retry occurred via
        ``on_verdict_pause``, this hook receives the post-retry result, not
        the pre-retry pause. Default: no-op.
        """
        return None

    async def on_verdict_pause(
        self,
        client: GovernanceClient,
        checkin_result: CheckinResult,
        cycle_result: CycleResult,
    ) -> bool:
        """Pause-recovery hook. Called when checkin returns ``pause``.

        Return ``True`` to retry the checkin once (e.g. after
        ``client.self_recovery(action="quick")``); ``False`` to let the
        ``VerdictError`` propagate. Hook exceptions are logged and
        swallowed (treated as ``False``). Default: no recovery — returns
        False.
        """
        return False

    # --- Lifecycle ---

    async def run_once(self) -> None:
        """Single cycle: connect -> ensure_identity -> run_cycle -> checkin -> disconnect.

        Bounded by ``cycle_timeout_seconds`` if set. Trims ``log_file`` after
        completion (success, failure, or timeout).
        """
        async def _cycle() -> None:
            async with GovernanceClient(
                mcp_url=self.mcp_url,
                timeout=self.timeout,
                connect_timeout=self.connect_timeout,
                connect_retries=self.connect_retries,
            ) as client:
                await self._ensure_identity(client)
                result = await self.run_cycle(client)
                await self._handle_cycle_result(client, result)

        try:
            if self.cycle_timeout_seconds is None:
                await _cycle()
            else:
                await asyncio.wait_for(_cycle(), self.cycle_timeout_seconds)
        finally:
            if self.log_file is not None:
                trim_log(self.log_file, self.max_log_lines)

    async def run_forever(
        self, interval: int = 60, heartbeat_interval: int = 1800
    ) -> None:
        """Loop: run_cycle repeatedly with heartbeat when idle.

        Each iteration is bounded by ``cycle_timeout_seconds`` if set.
        """
        self._install_signal_handlers()
        self._last_checkin_time = time.monotonic()

        async def _iteration() -> None:
            async with GovernanceClient(
                mcp_url=self.mcp_url,
                timeout=self.timeout,
                connect_timeout=self.connect_timeout,
                connect_retries=self.connect_retries,
            ) as client:
                await self._ensure_identity(client)
                result = await self.run_cycle(client)
                await self._handle_cycle_result(client, result)

                # Heartbeat if idle too long
                elapsed = time.monotonic() - self._last_checkin_time
                if elapsed >= heartbeat_interval and result is None:
                    await self._send_heartbeat(client)

        while self.running:
            delay = float(interval)
            try:
                if self.cycle_timeout_seconds is None:
                    await _iteration()
                else:
                    await asyncio.wait_for(_iteration(), self.cycle_timeout_seconds)

            except GovernanceUnavailableError as e:
                # Typed 503 — an expected, server-scheduled condition, not an
                # "unexpected error". Honor the server-suggested delay (already
                # bounded by MAX_RETRY_AFTER_SECONDS in errors.py) so the next
                # cycle backs off honestly instead of hammering on `interval`.
                logger.warning("%s: governance temporarily unavailable: %s", self.name, e)
                delay = max(delay, e.retry_after_seconds)
                if self.notify_on_error:
                    notify(self.name, f"Governance unavailable: {e}")
            except (GovernanceConnectionError, GovernanceTimeoutError) as e:
                logger.warning("%s: governance unavailable: %s", self.name, e)
                if self.notify_on_error:
                    notify(self.name, f"Governance unavailable: {e}")
            except VerdictError as e:
                logger.warning("%s: verdict %s — %s", self.name, e.verdict, e.guidance)
                if self.notify_on_error:
                    notify(self.name, f"Verdict: {e.verdict}")
            except IdentityDriftError as e:
                logger.error("%s: %s", self.name, e)
                if self.notify_on_error:
                    notify(self.name, str(e))
            except Exception as e:
                logger.error("%s: unexpected error: %s", self.name, e, exc_info=True)
                if self.notify_on_error:
                    notify(self.name, f"Error: {e}")
            finally:
                if self.log_file is not None:
                    trim_log(self.log_file, self.max_log_lines)

            if self.running:
                await asyncio.sleep(delay)

    # --- Identity resolution ---

    async def _ensure_identity(self, client: GovernanceClient) -> None:
        """Identity resolution: UUID lookup (fast) or fresh onboard."""
        self._load_session()

        # Fast path: we know who we are — just tell the server
        if self.agent_uuid:
            # Identity Honesty Part C: server's PATH 0 now requires
            # continuity_token alongside agent_uuid. Pass it explicitly here;
            # generic client auto-injection is intentionally token-free.
            if self.continuity_token and not client.continuity_token:
                client.continuity_token = self.continuity_token
            try:
                # Pass name=self.name so the SDK captures resident_name even
                # when resuming via UUID (RFC §7.13 substrate emission needs
                # resident_name to build surface_id = "resident:/<name>").
                # Without this, residents that resume via UUID never set
                # resident_name and substrate emission skips.
                identity_kwargs: dict[str, Any] = {
                    "name": self.name,
                    "agent_uuid": self.agent_uuid,
                    "resume": True,
                }
                token = self.continuity_token or getattr(client, "continuity_token", None)
                if token:
                    identity_kwargs["continuity_token"] = token
                await client.identity(**identity_kwargs)
                self._sync_from_client(client)
                self._save_session()
                logger.info("%s: resumed via UUID %s", self.name, self.agent_uuid[:12])
                # Reconcile resident tags on resume too — residents that always
                # resume via UUID (refuse_fresh_onboard=True) never re-run the
                # fresh-onboard stamp, so a dropped required tag would otherwise
                # never self-heal (Sentinel 2026-06-15 resident_tag_gap finding).
                await self._reconcile_resident_tags(client)
                return
            except Exception as e:
                logger.error(
                    "%s: UUID lookup failed for %s: %s — refusing to create ghost",
                    self.name, self.agent_uuid[:12], e,
                )
                raise

        # First run — onboard, get a UUID, save it
        if self.refuse_fresh_onboard and os.environ.get("UNITARES_FIRST_RUN") != "1":
            from .errors import IdentityBootstrapRefused
            raise IdentityBootstrapRefused(
                f"{self.name}: anchor missing at {self.session_file}, and "
                "refuse_fresh_onboard=True. Either restore the anchor from a "
                "rotation backup, or run this agent once with UNITARES_FIRST_RUN=1 "
                "to explicitly bootstrap a new identity. Never silent-swap."
            )

        onboard_kwargs: dict[str, Any] = {}
        if self.parent_agent_id is not None:
            onboard_kwargs["parent_agent_id"] = self.parent_agent_id
        if self.spawn_reason is not None:
            onboard_kwargs["spawn_reason"] = self.spawn_reason
        await client.onboard(self.name, **onboard_kwargs)
        self._sync_from_client(client)
        self._save_session()
        logger.info("%s: onboarded fresh (UUID %s)", self.name, self.agent_uuid[:12] if self.agent_uuid else "?")

        # Stamp/reconcile resident tags after fresh onboard. The server already
        # stamps these in the onboard handler for KNOWN_RESIDENT_LABELS (S8a
        # Phase-1, 2026-04-23), so this is usually a no-op against a current
        # server; it still covers pre-Phase-1 deploys where the server didn't.
        await self._reconcile_resident_tags(client)

    async def _reconcile_resident_tags(self, client: GovernanceClient) -> None:
        """Ensure a persistent resident carries the full ``RESIDENT_TAGS`` set.

        Residents need BOTH tags:
          - ``persistent``:  protects from ``auto_archive_orphan_agents``
            (``is_agent_protected`` in ``src/agent_lifecycle.py``).
          - ``autonomous``:  exempts from loop-detection pattern 4
            (``agent_loop_detection.py:216``). Resident cadences can't respond
            to pause verdicts within the 30s cooldown, so pattern-4 rejection
            silently starves their state writes (Steward 2026-04-20 regression).

        Both the SDK fresh-onboard stamp and the server-side onboard stamp only
        fire at *creation*. Residents like Sentinel set
        ``refuse_fresh_onboard=True`` and ALWAYS resume via UUID, so neither
        re-runs after the identity exists. If a required tag is ever dropped —
        an identity minted before ``autonomous`` was required, an archive/resume
        cycle, or a tag-replacing metadata write — nothing re-adds it and
        Vigil's ``resident_tag_hygiene`` check flags the gap on every cycle
        until someone manually re-tags (Sentinel 2026-06-15).

        Reconcile instead: read the current tags and, if any ``RESIDENT_TAGS``
        are missing, write back the UNION. Additive on purpose —
        ``update_agent_metadata`` REPLACES the tag list, so a bare
        ``RESIDENT_TAGS`` write would clobber role/cadence tags (e.g.
        ``cadence.10min``). A no-op (no write) when the tags are already
        present, and runs at most once per process (see
        ``_resident_tags_reconciled``).

        Non-fatal throughout: the agent still runs if reconciliation can't
        complete; the gap just persists (and stays visible to Vigil) until a
        later cycle or restart succeeds.
        """
        if not (self.persistent and self.agent_uuid):
            return
        if self._resident_tags_reconciled:
            return

        try:
            raw = await client.call_tool(
                "get_agent_metadata", {"target_agent": self.agent_uuid}
            )
        except Exception as e:
            # Couldn't read current tags — do NOT blind-write (that would risk
            # clobbering cadence/role tags). Leave the flag unset so the next
            # cycle retries.
            logger.warning(
                "%s: could not read tags to reconcile resident tag set: %s "
                "(will retry next cycle)",
                self.name, e,
            )
            return

        if not isinstance(raw, dict):
            logger.debug(
                "%s: resident tag reconcile skipped — unexpected "
                "get_agent_metadata response shape",
                self.name,
            )
            return

        current = [t for t in (raw.get("tags") or []) if isinstance(t, str)]
        missing = [t for t in RESIDENT_TAGS if t not in current]
        if not missing:
            # Already healthy — record success so we don't re-read every cycle.
            self._resident_tags_reconciled = True
            return

        merged = current + missing  # additive — preserves role/cadence tags
        try:
            await client.call_tool(
                "update_agent_metadata",
                {"agent_id": self.agent_uuid, "tags": merged},
            )
            logger.info(
                "%s: reconciled resident tags — added %s (now %s)",
                self.name, missing, merged,
            )
            self._resident_tags_reconciled = True
        except Exception as e:
            # Non-fatal. The agent still runs; it's just vulnerable to
            # archive_orphan_agents AND loop-detection until tagged. Flag stays
            # unset so the next cycle retries.
            logger.warning(
                "%s: failed to write reconciled resident tags %s: %s "
                "(vulnerable to orphan-sweep / loop-detection pattern 4 "
                "until tagged)",
                self.name, merged, e,
            )

    # --- Check-in handling ---

    async def _handle_cycle_result(
        self, client: GovernanceClient, result: CycleResult | None
    ) -> None:
        """Process a cycle result: check in, post notes, run hooks, raise on unrecovered pause/reject."""
        if result is None:
            return

        checkin_result = await client.checkin(
            response_text=result.summary,
            complexity=result.complexity,
            confidence=result.confidence,
            response_mode=result.response_mode,
        )
        self._last_checkin_time = time.monotonic()

        # Post any notes
        if result.notes:
            for summary, tags in result.notes:
                try:
                    await client.leave_note(summary=summary, tags=tags)
                except Exception as e:
                    logger.warning("%s: failed to leave note: %r", self.name, e)

        # Pause-recovery hook: retry once if the subclass recovered.
        if checkin_result.verdict == "pause":
            try:
                retry = await self.on_verdict_pause(client, checkin_result, result)
            except Exception as e:
                logger.warning("%s: on_verdict_pause raised: %r", self.name, e)
                retry = False
            if retry:
                checkin_result = await client.checkin(
                    response_text=result.summary,
                    complexity=result.complexity,
                    confidence=result.confidence,
                    response_mode=result.response_mode,
                )
                self._last_checkin_time = time.monotonic()

        # State-tracking hook: runs on the FINAL checkin_result (post-retry).
        # Hook exceptions are logged and swallowed.
        try:
            await self.on_after_checkin(client, checkin_result, result)
        except Exception as e:
            logger.warning("%s: on_after_checkin raised: %r", self.name, e)

        # Surface verdict if still bad
        if checkin_result.verdict in ("pause", "reject"):
            raise VerdictError(checkin_result.verdict, checkin_result.guidance)

    async def _send_heartbeat(self, client: GovernanceClient) -> None:
        """Send a lightweight heartbeat check-in."""
        try:
            await client.checkin(
                response_text="heartbeat",
                complexity=0.05,
                confidence=0.9,
                response_mode="compact",
            )
            self._last_checkin_time = time.monotonic()
            logger.debug("%s: heartbeat sent", self.name)
        except Exception as e:
            logger.warning("%s: heartbeat failed: %s", self.name, e)

    # --- Session persistence ---

    def _load_session(self) -> None:
        """Load session state, migrating from legacy location if needed."""
        if (
            not self.session_file.exists()
            and self.legacy_session_file
            and self.legacy_session_file.exists()
        ):
            try:
                legacy_data = load_json_state(self.legacy_session_file)
                if legacy_data:
                    # save_json_state/atomic_write creates parent dirs 0o700.
                    save_json_state(self.session_file, legacy_data)
                    logger.info(
                        "%s: migrated session from %s to %s",
                        self.name, self.legacy_session_file, self.session_file,
                    )
            except Exception as e:
                logger.warning("%s: legacy session migration failed: %s", self.name, e)

        saved = load_json_state(self.session_file)
        if saved.get("client_session_id"):
            self.client_session_id = saved["client_session_id"]
        if saved.get("continuity_token"):
            self.continuity_token = saved["continuity_token"]
        if saved.get("agent_uuid"):
            self.agent_uuid = saved["agent_uuid"]

    def _save_session(self) -> None:
        """Persist session state to the anchor."""
        data: dict[str, Any] = {}
        uuid_only_anchor = self.persistent and bool(os.environ.get("UNITARES_UDS_SOCKET"))
        if self.client_session_id and not uuid_only_anchor:
            data["client_session_id"] = self.client_session_id
        if self.continuity_token and not uuid_only_anchor:
            data["continuity_token"] = self.continuity_token
        if self.agent_uuid:
            data["agent_uuid"] = self.agent_uuid
        # save_json_state/atomic_write creates parent dirs 0o700.
        save_json_state(self.session_file, data)

    def _sync_from_client(self, client: GovernanceClient) -> None:
        """Copy identity state from client after successful identity/onboard."""
        if client.client_session_id:
            self.client_session_id = client.client_session_id
        if client.continuity_token:
            self.continuity_token = client.continuity_token
        if client.agent_uuid:
            if self.agent_uuid and client.agent_uuid != self.agent_uuid:
                raise IdentityDriftError(self.agent_uuid, client.agent_uuid)
            self.agent_uuid = client.agent_uuid

    # --- State persistence ---

    def load_state(self) -> dict:
        """Load agent-specific cross-cycle state."""
        path = self.state_file or (self.state_dir / "state.json")
        return load_json_state(path)

    def save_state(self, state: dict) -> None:
        """Save agent-specific cross-cycle state."""
        path = self.state_file or (self.state_dir / "state.json")
        save_json_state(path, state)

    # --- Signal handlers ---

    def _install_signal_handlers(self) -> None:
        """Install SIGTERM/SIGINT handlers for graceful shutdown."""
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGTERM, signal.SIGINT):
            loop.add_signal_handler(sig, self._handle_signal, sig)

    def _handle_signal(self, sig: int | signal.Signals) -> None:
        sig_name = sig.name if hasattr(sig, "name") else str(sig)
        logger.info("%s: received %s, shutting down gracefully", self.name, sig_name)
        self.running = False
