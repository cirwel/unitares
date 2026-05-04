"""Async governance client — typed MCP tool calls with session management."""

from __future__ import annotations

import asyncio

import anyio
import json
import logging
import os
from typing import Any

import httpx
from mcp.client.session import ClientSession
from mcp.client.streamable_http import streamable_http_client

from unitares_sdk.errors import (
    GovernanceConnectionError,
    GovernanceTimeoutError,
    IdentityDriftError,
    VerdictError,
)
from unitares_sdk.models import (
    ArchiveResult,
    AuditResult,
    CheckinResult,
    CleanupResult,
    IdentityResult,
    MetricsResult,
    ModelResult,
    NoteResult,
    OnboardResult,
    RecoveryResult,
    SearchResult,
)

logger = logging.getLogger(__name__)

# Tools that must NOT get automatic session injection
_IDENTITY_TOOLS = frozenset({"onboard", "identity"})


class GovernanceClient:
    """Async client for UNITARES governance MCP server.

    Opens a fresh MCP connection per connect()/disconnect() cycle.
    Use as a context manager for convenience::

        async with GovernanceClient() as client:
            result = await client.onboard("MyAgent")
            await client.checkin("did some work")
    """

    def __init__(
        self,
        mcp_url: str = "http://127.0.0.1:8767/mcp/",
        timeout: float = 30.0,
        retry_delay: float = 3.0,
        uds_path: str | None = None,
    ):
        # S19 substrate-anchored residents (Vigil, Sentinel, Chronicler)
        # connect over Unix-domain socket so the kernel attests their PID
        # to the governance MCP. Set ``uds_path`` explicitly OR set the
        # ``UNITARES_UDS_SOCKET`` env var (the launchd plist is the
        # documented place to set it). Passing ``uds_path`` overrides the
        # env var so tests can pin a value without polluting the
        # environment.
        if uds_path is None:
            uds_path = os.environ.get("UNITARES_UDS_SOCKET") or None

        self.mcp_url = mcp_url
        self.timeout = timeout
        self.retry_delay = retry_delay
        self.uds_path = uds_path

        # Session state — updated after identity/onboard responses
        self.client_session_id: str | None = None
        self.continuity_token: str | None = None
        self.agent_uuid: str | None = None
        # RFC §7.13: resident name captured on onboard so post-checkin
        # substrate emission can build surface_id = "resident:/<name>".
        # Stays None for non-resident callers; substrate emission skips
        # silently in that case.
        self.resident_name: str | None = None
        # Cached lease handle for substrate emission. Per-client-instance
        # so concurrent residents don't share state.
        from unitares_sdk._substrate import _LeaseCache
        self._substrate_lease_cache = _LeaseCache()

        # MCP transport state
        self._session: ClientSession | None = None
        self._http_client: httpx.AsyncClient | None = None
        self._cm_stack: list = []

    # --- Connection lifecycle ---

    async def connect(self) -> None:
        """Open MCP transport. Call disconnect() when done.

        If any step fails (e.g. httpx.ConnectError during initialize), unwinds
        whatever was already entered before re-raising. Python does NOT call
        __aexit__ when __aenter__ raises, so without this cleanup the MCP
        streamable_http_client's anyio task group is leaked and later unwound
        on a different task at GC — producing the "Attempted to exit cancel
        scope in a different task than it was entered in" crash that killed
        the sentinel repeatedly (KG 2026-04-19T00:51:46).
        """
        # S19: when uds_path is set, route the underlying HTTP requests over
        # a Unix-domain socket via httpx.AsyncHTTPTransport(uds=...). The MCP
        # client still speaks HTTP semantically; only the network boundary
        # changes. The Host header in mcp_url is informational under UDS
        # (the kernel resolves the connection via the socket file path).
        if self.uds_path:
            transport = httpx.AsyncHTTPTransport(uds=self.uds_path)
            self._http_client = httpx.AsyncClient(
                http2=False, timeout=self.timeout, transport=transport,
            )
            logger.info(
                "[SDK] connecting via UDS at %s (substrate-attestation transport)",
                self.uds_path,
            )
        else:
            self._http_client = httpx.AsyncClient(http2=False, timeout=self.timeout)
        try:
            cm = streamable_http_client(self.mcp_url, http_client=self._http_client)
            read, write, _ = await cm.__aenter__()
            self._cm_stack.append(cm)

            session_cm = ClientSession(read, write)
            self._session = await session_cm.__aenter__()
            self._cm_stack.append(session_cm)

            await self._session.initialize()
        except Exception as e:
            logger.warning("connect() failed (%s: %s); unwinding partial state", type(e).__name__, e)
            await self.disconnect()
            raise

    async def disconnect(self) -> None:
        """Close MCP transport."""
        for cm in reversed(self._cm_stack):
            try:
                await cm.__aexit__(None, None, None)
            except Exception:
                pass
        self._cm_stack.clear()
        self._session = None
        if self._http_client:
            try:
                await self._http_client.aclose()
            except Exception:
                pass
            self._http_client = None

    async def __aenter__(self) -> GovernanceClient:
        await self.connect()
        return self

    async def __aexit__(self, *exc: Any) -> None:
        await self.disconnect()

    # --- Raw tool call ---

    async def call_tool(
        self, tool_name: str, arguments: dict, *, timeout: float | None = None
    ) -> dict:
        """Call an MCP tool and return the parsed JSON response.

        Handles session injection, timeout, one retry on transient errors,
        and MCP content block parsing.
        """
        if self._session is None:
            raise GovernanceConnectionError("Not connected — call connect() first")

        effective_timeout = timeout or self.timeout
        injected_args = self._inject_session(tool_name, arguments)
        last_error: Exception | None = None

        for attempt in range(2):
            try:
                with anyio.fail_after(effective_timeout):
                    result = await self._session.call_tool(tool_name, injected_args)
                return self._parse_mcp_result(result)

            except TimeoutError:
                last_error = GovernanceTimeoutError(
                    f"{tool_name} timed out after {effective_timeout}s"
                )
                if attempt == 0:
                    logger.warning("Timeout on %s, retrying in %.1fs", tool_name, self.retry_delay)
                    await asyncio.sleep(self.retry_delay)
                    continue
            except (httpx.ConnectError, httpx.TimeoutException, ConnectionError, OSError) as e:
                last_error = GovernanceConnectionError(str(e))
                if attempt == 0:
                    logger.warning(
                        "Transient error on %s, retrying in %.1fs: %s",
                        tool_name, self.retry_delay, e,
                    )
                    await asyncio.sleep(self.retry_delay)
                    continue

        raise last_error  # type: ignore[misc]

    # --- Identity ---

    async def onboard(
        self,
        name: str,
        model_type: str = "resident_agent",
        client_hint: str = "resident",
        force_new: bool = False,
        parent_agent_id: str | None = None,
        spawn_reason: str | None = None,
        **kwargs: Any,
    ) -> OnboardResult:
        """Register with governance. Maps to server tool: onboard.

        parent_agent_id/spawn_reason: opt-in lineage tracking. When provided,
        the server persists a parent link in core.identities so spawned agents
        are distinguishable from root agents with otherwise-similar signals.
        """
        args: dict[str, Any] = {
            "name": name,
            "model_type": model_type,
            "client_hint": client_hint,
        }
        if force_new:
            args["force_new"] = True
        if parent_agent_id is not None:
            args["parent_agent_id"] = parent_agent_id
        if spawn_reason is not None:
            args["spawn_reason"] = spawn_reason
        args.update(kwargs)

        raw = await self.call_tool("onboard", args)
        self._capture_identity(raw)
        # RFC §7.13: capture resident name so subsequent checkins can emit
        # substrate observations to lease_plane.surface_leases. Non-resident
        # names are stored too but substrate emission filters them out.
        self.resident_name = name
        return OnboardResult.model_validate(raw)

    async def identity(
        self,
        name: str | None = None,
        resume: bool = True,
        continuity_token: str | None = None,
        parent_agent_id: str | None = None,
        spawn_reason: str | None = None,
        agent_uuid: str | None = None,
        **kwargs: Any,
    ) -> IdentityResult:
        """Resume or query identity. Maps to server tool: identity.

        agent_uuid: pass a known UUID for direct server-side lookup.
        Skips session/name resolution entirely. Requires resume=True.

        parent_agent_id/spawn_reason: applied only when this call results in
        identity creation (e.g. name-resume miss falling through to create).
        Ignored on successful resume.
        """
        args: dict[str, Any] = {"resume": resume}
        if name is not None:
            args["name"] = name
        if continuity_token is not None:
            args["continuity_token"] = continuity_token
        if parent_agent_id is not None:
            args["parent_agent_id"] = parent_agent_id
        if spawn_reason is not None:
            args["spawn_reason"] = spawn_reason
        if agent_uuid is not None:
            args["agent_uuid"] = agent_uuid
        args.update(kwargs)

        raw = await self.call_tool("identity", args)
        self._capture_identity(raw)
        # RFC §7.13: capture resident name from identity() too — substrate-
        # anchored residents (Vigil/Sentinel/Watcher/Chronicler) resume via
        # identity() across restarts and never call onboard(), so without
        # this fixup self.resident_name stayed None and the post-checkin
        # substrate emission silently skipped. Caught 2026-05-04 on the
        # canary multi-resident probe (only Steward had a substrate row
        # because Steward is in-process and skipped this code path).
        # Resolution order: explicit name kwarg → raw response 'label' field
        # if the server includes it → leave None (caller may set explicitly).
        if name:
            self.resident_name = name
        elif isinstance(raw, dict) and raw.get("label"):
            self.resident_name = raw["label"]
        return IdentityResult.model_validate(raw)

    # --- Check-in ---

    async def checkin(
        self,
        response_text: str,
        complexity: float = 0.3,
        confidence: float = 0.7,
        response_mode: str = "compact",
        **kwargs: Any,
    ) -> CheckinResult:
        """Check in with governance. Maps to server tool: process_agent_update."""
        args: dict[str, Any] = {
            "response_text": response_text,
            "complexity": complexity,
            "confidence": confidence,
            "response_mode": response_mode,
        }
        args.update(kwargs)

        raw = await self.call_tool("process_agent_update", args)
        self._raise_for_tool_failure("process_agent_update", raw)

        # Extract verdict for potential error raising
        decision = raw.get("decision", {})
        verdict = decision.get("action", raw.get("verdict", "proceed"))
        guidance = decision.get("guidance") or raw.get("guidance")

        # Build result with flattened verdict
        result_data = dict(raw)
        result_data["verdict"] = verdict
        if guidance:
            result_data["guidance"] = guidance

        # Extract coherence/risk from metrics if present
        metrics = raw.get("metrics", {})
        if metrics:
            result_data.setdefault("coherence", metrics.get("coherence"))
            result_data.setdefault("risk", metrics.get("risk"))

        # RFC §7.13: emit substrate observation to lease_plane.surface_leases
        # alongside the existing process_agent_update path. Failure does NOT
        # fail the checkin — RFC §7.13.4 dual-run-authority assignment makes
        # audit.events authoritative until each resident's individual canary
        # completes (PR 8 env-var gate). Skipped silently for non-resident
        # callers (matched against KNOWN_RESIDENT_NAMES in _substrate.py).
        try:
            from unitares_sdk._substrate import emit_substrate_observation
            if self.resident_name and self.agent_uuid and metrics:
                emit_substrate_observation(
                    resident_name=self.resident_name,
                    holder_uuid=self.agent_uuid,
                    metrics=metrics,
                    cache=self._substrate_lease_cache,
                )
        except Exception as exc:  # noqa: BLE001 — observational-only by contract
            logger.debug("[SDK] substrate emit failed (observational-only): %r", exc)

        return CheckinResult.model_validate(result_data)

    # --- Knowledge graph ---

    async def leave_note(
        self,
        summary: str,
        tags: list[str] | None = None,
        **kwargs: Any,
    ) -> NoteResult:
        """Leave a knowledge graph note. Maps to server tool: leave_note."""
        args: dict[str, Any] = {"summary": summary}
        if tags is not None:
            args["tags"] = tags
        args.update(kwargs)
        raw = await self.call_tool("leave_note", args)
        return NoteResult.model_validate(raw)

    async def search_knowledge(self, query: str, **kwargs: Any) -> SearchResult:
        """Search knowledge graph. Maps to server tool: knowledge(action=search)."""
        args: dict[str, Any] = {"action": "search", "query": query}
        args.update(kwargs)
        raw = await self.call_tool("knowledge", args)
        self._raise_for_tool_failure("knowledge", raw)
        return SearchResult.model_validate(raw)

    async def store_discovery(
        self,
        summary: str,
        discovery_type: str,
        severity: str,
        tags: list[str] | None = None,
        details: str | None = None,
        **kwargs: Any,
    ) -> NoteResult:
        """Store a discovery. Maps to server tool: knowledge(action=store)."""
        args: dict[str, Any] = {
            "action": "store",
            "discovery_type": discovery_type,
            "severity": severity,
            "summary": summary,
        }
        if tags is not None:
            args["tags"] = tags
        if details is not None:
            args["details"] = details
        args.update(kwargs)
        raw = await self.call_tool("knowledge", args)
        return NoteResult.model_validate(raw)

    async def audit_knowledge(
        self, scope: str = "open", top_n: int = 10, **kwargs: Any
    ) -> AuditResult:
        """Audit knowledge graph. Maps to server tool: knowledge(action=audit)."""
        args: dict[str, Any] = {
            "action": "audit",
            "scope": scope,
            "top_n": str(top_n),
            "use_model": "true",
        }
        args.update(kwargs)
        raw = await self.call_tool("knowledge", args)
        return AuditResult.model_validate(raw)

    async def cleanup_knowledge(
        self, dry_run: bool = False, **kwargs: Any
    ) -> CleanupResult:
        """Clean up knowledge graph. Maps to server tool: knowledge(action=cleanup)."""
        args: dict[str, Any] = {"action": "cleanup", "dry_run": str(dry_run).lower()}
        args.update(kwargs)
        raw = await self.call_tool("knowledge", args)
        return CleanupResult.model_validate(raw)

    # --- Lifecycle ---

    async def archive_orphan_agents(self, **kwargs: Any) -> ArchiveResult:
        """Archive orphan agents. Maps to server tool: archive_orphan_agents."""
        raw = await self.call_tool("archive_orphan_agents", kwargs)
        return ArchiveResult.model_validate(raw)

    async def self_recovery(
        self, action: str = "quick", **kwargs: Any
    ) -> RecoveryResult:
        """Trigger self-recovery. Maps to server tool: self_recovery."""
        args: dict[str, Any] = {"action": action}
        args.update(kwargs)
        raw = await self.call_tool("self_recovery", args)
        return RecoveryResult.model_validate(raw)

    # --- Metrics ---

    async def get_metrics(self, **kwargs: Any) -> MetricsResult:
        """Get governance metrics. Maps to server tool: get_governance_metrics."""
        raw = await self.call_tool("get_governance_metrics", kwargs)
        return MetricsResult.model_validate(raw)

    # --- Model inference ---

    async def call_model(
        self,
        prompt: str,
        provider: str | None = None,
        model: str | None = None,
        max_tokens: int = 1024,
        temperature: float = 0.0,
        **kwargs: Any,
    ) -> ModelResult:
        """Call a model via governance. Maps to server tool: call_model."""
        args: dict[str, Any] = {
            "prompt": prompt,
            "max_tokens": max_tokens,
            "temperature": temperature,
        }
        if provider is not None:
            args["provider"] = provider
        if model is not None:
            args["model"] = model
        args.update(kwargs)
        raw = await self.call_tool("call_model", args)
        return ModelResult.model_validate(raw)

    # --- Internal helpers ---

    def _inject_session(self, tool_name: str, arguments: dict) -> dict:
        """Auto-append session/continuity IDs. Skip for identity tools."""
        if tool_name in _IDENTITY_TOOLS:
            return arguments
        args = dict(arguments)
        if self.client_session_id and "client_session_id" not in args:
            args["client_session_id"] = self.client_session_id
        if self.continuity_token and "continuity_token" not in args:
            args["continuity_token"] = self.continuity_token
        return args

    def _capture_identity(self, raw: dict) -> None:
        """Extract and store session ID, continuity token, and UUID from a response."""
        sid = self._extract_session_id(raw)
        token = self._extract_continuity_token(raw)
        uuid = self._extract_uuid(raw)

        if uuid:
            if self.agent_uuid and uuid != self.agent_uuid:
                raise IdentityDriftError(self.agent_uuid, uuid)
            self.agent_uuid = uuid

        if sid:
            self.client_session_id = sid
        if token:
            self.continuity_token = token

    @staticmethod
    def _extract_session_id(raw: dict) -> str | None:
        return (
            raw.get("client_session_id")
            or raw.get("session_continuity", {}).get("client_session_id")
            or raw.get("identity_summary", {}).get("client_session_id", {}).get("value")
        )

    @staticmethod
    def _extract_continuity_token(raw: dict) -> str | None:
        return (
            raw.get("continuity_token")
            or raw.get("session_continuity", {}).get("continuity_token")
            or raw.get("identity_summary", {}).get("continuity_token", {}).get("value")
            or raw.get("quick_reference", {}).get("for_strong_resume")
        )

    @staticmethod
    def _extract_uuid(raw: dict) -> str | None:
        return (
            raw.get("uuid")
            or raw.get("agent_uuid")
            or raw.get("bound_identity", {}).get("uuid")
        )

    @staticmethod
    def _raise_for_tool_failure(tool_name: str, raw: dict) -> None:
        if raw.get("success") is False:
            error = raw.get("error", "Unknown error")
            raise GovernanceConnectionError(f"Tool {tool_name} failed: {error}")

    @staticmethod
    def _parse_mcp_result(result: Any) -> dict:
        """Parse MCP tool result content blocks into a merged dict."""
        final: dict = {}
        raw_texts: list[str] = []
        json_parsed = False

        for content in result.content:
            if hasattr(content, "text"):
                text = content.text
                raw_texts.append(text)
                try:
                    data = json.loads(text)
                    if isinstance(data, dict):
                        final.update(data)
                        json_parsed = True
                except json.JSONDecodeError:
                    continue

        if json_parsed:
            return final
        if raw_texts:
            return {"text": "\n".join(raw_texts), "raw": True}
        return {"success": False, "error": "No content in response"}
