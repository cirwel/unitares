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
    GovernanceUnavailableError,
    IdentityDriftError,
    extract_identity_refusal,
    extract_retry_after_seconds,
    parse_retry_after_header,
)
from unitares_sdk.models import (
    ArchiveResult,
    AuditResult,
    CheckinResult,
    CleanupResult,
    IdentityResult,
    InferenceHostResult,
    InferenceHostsResult,
    MetricsResult,
    ModelResult,
    NoteResult,
    OnboardResult,
    RecoveryResult,
    SearchResult,
)

logger = logging.getLogger(__name__)

# Connect-handshake failures worth retrying: timeout (incl. the bounded
# initialize()) and connection-level transients. Auth/protocol errors are NOT
# here — they are deterministic and must surface immediately, not burn retries.
# asyncio.wait_for raises asyncio.TimeoutError (== builtin TimeoutError on 3.11+).
_CONNECT_RETRYABLE = (
    asyncio.TimeoutError,
    httpx.ConnectError,
    httpx.ConnectTimeout,
    httpx.ReadTimeout,
    httpx.RemoteProtocolError,
    ConnectionError,
)

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
        connect_timeout: float | None = None,
        connect_retries: int | None = None,
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

        # Connect-handshake resilience. The MCP `initialize()` handshake was
        # historically unbounded — under the anyio-asyncio connect tax it can
        # hang on the stream `receive()`, which is NOT covered by httpx's
        # `timeout` (that bounds HTTP reads, not the MCP session's internal
        # anyio stream). An unbounded hang silently consumes the caller's whole
        # cycle budget (Vigil cron 2026-06-02: every check skipped that cycle).
        # We bound `initialize()` with `connect_timeout` and retry the cold
        # connect `connect_retries` times — sized so worst case
        # (connect_timeout * (retries+1) + retry_delay * retries) fits the
        # tightest resident cycle budget (Sentinel = 45s). Env-overridable.
        if connect_timeout is None:
            connect_timeout = float(os.environ.get("UNITARES_CONNECT_TIMEOUT", "10"))
        if connect_retries is None:
            connect_retries = int(os.environ.get("UNITARES_CONNECT_RETRIES", "1"))
        self.connect_timeout = connect_timeout
        self.connect_retries = connect_retries

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
        """Open MCP transport with a bounded, retried handshake.

        On each attempt, if any step fails (e.g. httpx.ConnectError, or a
        ``TimeoutError`` from the bounded ``initialize()``), ``disconnect()``
        unwinds whatever was already entered before retry/re-raise. Python does
        NOT call __aexit__ when __aenter__ raises, so without this cleanup the
        MCP streamable_http_client's anyio task group is leaked and later
        unwound on a different task at GC — producing the "Attempted to exit
        cancel scope in a different task than it was entered in" crash that
        killed the sentinel repeatedly (KG 2026-04-19T00:51:46).

        Transient failures (timeout/connection-level) are retried up to
        ``connect_retries`` times; non-transient ones (auth, protocol) surface
        immediately without burning the retry budget. See __init__ for why the
        handshake is bounded (an unbounded ``initialize()`` hang is invisible
        to httpx's timeout and silently consumes the caller's cycle budget).
        """
        attempts = self.connect_retries + 1
        for attempt in range(1, attempts + 1):
            try:
                await self._open_session()
                return
            except _CONNECT_RETRYABLE as e:
                await self.disconnect()  # unwind partial state before retrying
                if attempt < attempts:
                    logger.warning(
                        "connect() attempt %d/%d failed (%s: %s); retrying in %.1fs",
                        attempt, attempts, type(e).__name__, e, self.retry_delay,
                    )
                    await asyncio.sleep(self.retry_delay)
                    continue
                logger.warning(
                    "connect() failed after %d attempt(s) (%s: %s); giving up",
                    attempts, type(e).__name__, e,
                )
                raise
            except BaseException as e:
                # Non-transient (auth, protocol) AND cancellation: unwind and
                # surface at once. Catching BaseException is deliberate — a
                # CancelledError from the caller's outer cycle timeout would
                # otherwise skip disconnect() entirely (it is not in
                # _CONNECT_RETRYABLE), leaking the partially-entered anyio task
                # group. disconnect() shields its own unwind, so cleanup
                # completes before the CancelledError propagates.
                logger.warning(
                    "connect() failed (%s: %s); unwinding partial state",
                    type(e).__name__, e,
                )
                await self.disconnect()
                raise

    async def _open_session(self) -> None:
        """Open transport + session and run the bounded MCP initialize handshake.

        Factored out of connect() so the retry loop can re-enter cleanly and so
        the bounded-initialize behavior is unit-testable. Raises on any failure;
        connect() owns unwind/retry. Never swallows — a partially-opened stack
        is left for connect()'s disconnect() to unwind.
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

        cm = streamable_http_client(self.mcp_url, http_client=self._http_client)
        read, write, _ = await cm.__aenter__()
        self._cm_stack.append(cm)

        session_cm = ClientSession(read, write)
        self._session = await session_cm.__aenter__()
        self._cm_stack.append(session_cm)

        # Bound the handshake: an anyio-stream hang inside initialize() is not
        # covered by httpx's timeout, so without this it blocks until the
        # caller's outer cycle timeout cancels the whole cycle.
        await asyncio.wait_for(self._session.initialize(), self.connect_timeout)

    async def disconnect(self) -> None:
        """Close MCP transport.

        Cleanup is shielded from cancellation: when the caller's outer cycle
        timeout cancels connect() mid-handshake, the unwind of the anyio task
        group inside streamable_http_client MUST still complete in THIS task —
        otherwise its post_writer leaks and later crashes at GC with "exit
        cancel scope in a different task" (the sentinel crash, KG
        2026-04-19T00:51:46). A bare ``except Exception`` does not catch the
        CancelledError that would otherwise interrupt an unshielded __aexit__
        await partway through, so each unwind step runs inside a shielded
        scope and the CancelledError is left to propagate afterwards.
        """
        for cm in reversed(self._cm_stack):
            try:
                with anyio.CancelScope(shield=True):
                    await cm.__aexit__(None, None, None)
            except Exception:
                pass
        self._cm_stack.clear()
        self._session = None
        if self._http_client:
            try:
                with anyio.CancelScope(shield=True):
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
                raw = self._parse_mcp_result(result)
                # Wave 3 §3.2: the cutover proxy / fail-fast writer surfaces a
                # typed-unavailable payload. Honor retry_after_seconds with one
                # retry (prereq PR #10 consumer contract), then raise typed so
                # resident cycle loops can back off with the server's delay.
                retry_after = extract_retry_after_seconds(raw)
                if retry_after is not None:
                    last_error = GovernanceUnavailableError(
                        f"{tool_name}: governance temporarily unavailable "
                        f"(retry_after_seconds={retry_after})",
                        retry_after_seconds=retry_after,
                    )
                    if attempt == 0:
                        logger.warning(
                            "%s unavailable (503-equivalent), retrying in %.1fs",
                            tool_name, retry_after,
                        )
                        await asyncio.sleep(retry_after)
                        continue
                    raise last_error
                # Centralized failure check. The MCP transport can succeed at
                # the HTTP layer but carry a structured tool failure inside
                # the JSON payload. Without this check, callers like onboard()
                # and identity() would silently extract empty identity fields
                # from a failure response (dogfood pulse 2026-05-03 regression).
                self._raise_for_tool_failure(tool_name, raw)
                return raw

            except TimeoutError:
                last_error = GovernanceTimeoutError(
                    f"{tool_name} timed out after {effective_timeout}s"
                )
                if attempt == 0:
                    logger.warning("Timeout on %s, retrying in %.1fs", tool_name, self.retry_delay)
                    await asyncio.sleep(self.retry_delay)
                    continue
            except httpx.HTTPStatusError as e:
                # §3.2 cutover 503 surfacing at the HTTP layer instead of as a
                # parsed tool payload. Honor Retry-After; other statuses keep
                # the pre-existing connection-error treatment.
                status = e.response.status_code if e.response is not None else None
                if status == 503:
                    retry_after = (
                        parse_retry_after_header(
                            e.response.headers.get("Retry-After")
                        )
                        if e.response is not None
                        else None
                    ) or 5.0
                    last_error = GovernanceUnavailableError(
                        f"{tool_name}: HTTP 503 from governance transport "
                        f"(retry_after_seconds={retry_after})",
                        retry_after_seconds=retry_after,
                    )
                    if attempt == 0:
                        logger.warning(
                            "HTTP 503 on %s, retrying in %.1fs", tool_name, retry_after
                        )
                        await asyncio.sleep(retry_after)
                        continue
                else:
                    last_error = GovernanceConnectionError(str(e))
                    if attempt == 0:
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

        # #425: a strict-identity refusal comes back as a structured SUCCESS
        # shape (no isError, no "error" key), so _raise_for_tool_failure passes
        # it through. Left undetected, the verdict extraction below defaults to
        # "proceed" and we report a successful check-in while the server
        # recorded NOTHING — the resident goes silently dark (Chronicler
        # 2026-06-14). Detect the refusal marker and fail loud instead.
        refusal = extract_identity_refusal(raw)
        if refusal is not None:
            # Recovery for the resume-binding cliff: a long-cadence resident
            # (e.g. Chronicler, daily) can wake with its PG session binding
            # expired AND the anchor's continuity token stale (1h TTL << 24h
            # cadence). The identity() resume reissues a fresh in-memory token
            # but its agent_uuid passthrough (PATH 0) does not mint a session,
            # so this first check-in resolves to a PATH2_RESUME_MISS and the
            # strict gate refuses it. #513 deliberately keeps the continuity
            # token OFF the happy path (S1-c narrowing); here we re-prove
            # ownership EXPLICITLY by presenting the fresh in-memory token on a
            # single recovery retry, which the server honors via PATH 2.8
            # token-rebind (mints a fresh caller-proven session). The token
            # rides the wire only on this recovery — frequent residents whose
            # session row is still live never reach this branch.
            if self.continuity_token and not args.get("continuity_token"):
                retry_args = dict(args)
                retry_args["continuity_token"] = self.continuity_token
                raw = await self.call_tool("process_agent_update", retry_args)
                self._raise_for_tool_failure("process_agent_update", raw)
                # Capture the rebound session so later calls this run ride it
                # without re-presenting the token.
                self._capture_identity(raw)
                refusal = extract_identity_refusal(raw)
            if refusal is not None:
                raise refusal

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
        # The lease-plane client is synchronous (2s timeout); run it in a
        # worker thread so it never blocks this event loop mid-checkin.
        try:
            from unitares_sdk._substrate import emit_substrate_observation
            if self.resident_name and self.agent_uuid and metrics:
                await asyncio.to_thread(
                    emit_substrate_observation,
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
        """Leave a knowledge graph note. Routes through `knowledge(action='note')` —
        the `leave_note` MCP tool is deprecated (issue #429) but the SDK method
        name is retained for backward compatibility.
        """
        args: dict[str, Any] = {"action": "note", "summary": summary}
        if tags is not None:
            args["tags"] = tags
        args.update(kwargs)
        raw = await self.call_tool("knowledge", args)
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
        """Preview orphan archival candidates. Maps to archive_orphan_agents."""
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

    async def list_inference_hosts(
        self,
        include_unconfigured: bool = True,
        provider_kind: str | None = None,
        **kwargs: Any,
    ) -> InferenceHostsResult:
        """List registered inference hosts. Maps to server tool: list_inference_hosts."""
        args: dict[str, Any] = {"include_unconfigured": include_unconfigured}
        if provider_kind is not None:
            args["provider_kind"] = provider_kind
        args.update(kwargs)
        raw = await self.call_tool("list_inference_hosts", args)
        return InferenceHostsResult.model_validate(raw)

    async def describe_inference_host(
        self,
        host_id: str,
        **kwargs: Any,
    ) -> InferenceHostResult:
        """Describe one inference host. Maps to server tool: describe_inference_host."""
        args: dict[str, Any] = {"host_id": host_id}
        args.update(kwargs)
        raw = await self.call_tool("describe_inference_host", args)
        return InferenceHostResult.model_validate(raw)

    async def call_model(
        self,
        prompt: str,
        provider: str | None = None,
        model: str | None = None,
        host_id: str | None = None,
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
        if host_id is not None:
            args["host_id"] = host_id
        args.update(kwargs)
        raw = await self.call_tool("call_model", args)
        return ModelResult.model_validate(raw)

    # --- Internal helpers ---

    def _inject_session(self, tool_name: str, arguments: dict) -> dict:
        """Auto-append in-process session ID. Skip for identity tools."""
        if tool_name in _IDENTITY_TOOLS:
            return arguments
        args = dict(arguments)
        if self.client_session_id and "client_session_id" not in args:
            args["client_session_id"] = self.client_session_id
        return args

    def _capture_identity(self, raw: dict) -> None:
        """Extract and store session ID, continuity token, and UUID from a response.

        Skips extraction when the response declares failure — extracted
        fields would be None and would silently overwrite nothing while
        hiding the upstream error. call_tool() already raises in that case;
        this is a defensive guard for callers that pass arbitrary dicts.
        """
        if raw.get("success") is False:
            return

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
            or raw.get("quick_reference", {}).get("for_path0_ownership_proof")
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
        """Parse MCP tool result content blocks into a merged dict.

        Raises GovernanceConnectionError when the MCP layer marked the
        call as an error (result.isError). HTTP-level success does not
        imply tool-level success: the streamable_http transport can wrap
        a structured failure in an otherwise-200 response.
        """
        if getattr(result, "isError", False):
            error_text = ""
            for content in getattr(result, "content", []) or []:
                if hasattr(content, "text"):
                    error_text = content.text
                    break
            raise GovernanceConnectionError(
                f"MCP tool returned isError=true: {error_text or 'no detail'}"
            )

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
