"""Synchronous governance client — REST or sync-over-async transport."""

from __future__ import annotations

import asyncio
import json
import logging
import urllib.error
import urllib.request
from typing import Any

from unitares_sdk.errors import (
    GovernanceConnectionError,
    GovernanceTimeoutError,
    IdentityDriftError,
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


class SyncGovernanceClient:
    """Synchronous client for UNITARES governance.

    Supports two transports:
    - ``transport="rest"`` (default): Uses ``urllib.request`` to POST to
      ``/v1/tools/call``. Safe in any context (no event loop needed).
    - ``transport="mcp"``: Wraps the async ``GovernanceClient`` via
      ``asyncio.run()``. Only safe in standalone processes — raises
      ``RuntimeError`` if a loop is already running.

    Usage::

        client = SyncGovernanceClient(transport="rest")
        result = client.onboard("MyAgent")
        result = client.checkin("did some work")
    """

    def __init__(
        self,
        mcp_url: str = "http://127.0.0.1:8767/mcp/",
        rest_url: str = "http://127.0.0.1:8767/v1/tools/call",
        timeout: float = 30.0,
        transport: str = "rest",
    ):
        self.mcp_url = mcp_url
        self.rest_url = rest_url
        self.timeout = timeout
        self.transport = transport

        # Session state
        self.client_session_id: str | None = None
        self.continuity_token: str | None = None
        self.agent_uuid: str | None = None
        # RFC §7.13: see UnitaresClient docstring. Same shape, sync mirror.
        self.resident_name: str | None = None
        from unitares_sdk._substrate import _LeaseCache
        self._substrate_lease_cache = _LeaseCache()

        # Lazy async client for mcp transport
        self._async_client = None

    def __enter__(self) -> SyncGovernanceClient:
        if self.transport == "mcp":
            self._ensure_async_client()
        return self

    def __exit__(self, *exc: Any) -> None:
        if self._async_client is not None:
            try:
                asyncio.run(self._async_client.disconnect())
            except Exception:
                pass
            self._async_client = None

    # --- Raw tool call ---

    def call_tool(
        self, tool_name: str, arguments: dict, *, timeout: float | None = None
    ) -> dict:
        """Call a governance tool synchronously."""
        injected_args = self._inject_session(tool_name, arguments)

        if self.transport == "rest":
            result = self._rest_call(tool_name, injected_args, timeout=timeout)
        else:
            result = self._mcp_call(tool_name, injected_args, timeout=timeout)
        self._raise_for_tool_failure(tool_name, result)
        return result

    # --- Identity ---

    def onboard(
        self,
        name: str,
        model_type: str = "resident_agent",
        client_hint: str = "resident",
        force_new: bool = False,
        **kwargs: Any,
    ) -> OnboardResult:
        args: dict[str, Any] = {
            "name": name,
            "model_type": model_type,
            "client_hint": client_hint,
        }
        if force_new:
            args["force_new"] = True
        args.update(kwargs)
        raw = self.call_tool("onboard", args)
        self._capture_identity(raw)
        # RFC §7.13: capture resident name for substrate emission (mirrors UnitaresClient).
        self.resident_name = name
        return OnboardResult.model_validate(raw)

    def identity(
        self,
        name: str | None = None,
        resume: bool = True,
        continuity_token: str | None = None,
        **kwargs: Any,
    ) -> IdentityResult:
        args: dict[str, Any] = {"resume": resume}
        if name is not None:
            args["name"] = name
        if continuity_token is not None:
            args["continuity_token"] = continuity_token
        args.update(kwargs)
        raw = self.call_tool("identity", args)
        self._capture_identity(raw)
        # RFC §7.13: capture resident name from identity() too (mirrors UnitaresClient).
        # Substrate-anchored residents resume via identity() across restarts and
        # never call onboard(); without this the post-checkin substrate emission
        # silently skipped because resident_name stayed None.
        if name:
            self.resident_name = name
        elif isinstance(raw, dict) and raw.get("label"):
            self.resident_name = raw["label"]
        return IdentityResult.model_validate(raw)

    # --- Check-in ---

    def checkin(
        self,
        response_text: str,
        complexity: float = 0.3,
        confidence: float = 0.7,
        response_mode: str = "compact",
        **kwargs: Any,
    ) -> CheckinResult:
        args: dict[str, Any] = {
            "response_text": response_text,
            "complexity": complexity,
            "confidence": confidence,
            "response_mode": response_mode,
        }
        args.update(kwargs)
        raw = self.call_tool("process_agent_update", args)
        self._raise_for_tool_failure("process_agent_update", raw)

        decision = raw.get("decision", {})
        verdict = decision.get("action", raw.get("verdict", "proceed"))
        guidance = decision.get("guidance") or raw.get("guidance")

        result_data = dict(raw)
        result_data["verdict"] = verdict
        if guidance:
            result_data["guidance"] = guidance

        metrics = raw.get("metrics", {})
        if metrics:
            result_data.setdefault("coherence", metrics.get("coherence"))
            result_data.setdefault("risk", metrics.get("risk"))

        # RFC §7.13: emit substrate observation alongside process_agent_update
        # (mirrors UnitaresClient.checkin). Failure observational-only.
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

    def leave_note(
        self, summary: str, tags: list[str] | None = None, **kwargs: Any
    ) -> NoteResult:
        """Routes through `knowledge(action='note')` — the `leave_note` MCP tool
        is deprecated (issue #429) but the SDK method name is retained for
        backward compatibility.
        """
        args: dict[str, Any] = {"action": "note", "summary": summary}
        if tags is not None:
            args["tags"] = tags
        args.update(kwargs)
        raw = self.call_tool("knowledge", args)
        return NoteResult.model_validate(raw)

    def search_knowledge(self, query: str, **kwargs: Any) -> SearchResult:
        args: dict[str, Any] = {"action": "search", "query": query}
        args.update(kwargs)
        raw = self.call_tool("knowledge", args)
        self._raise_for_tool_failure("knowledge", raw)
        return SearchResult.model_validate(raw)

    def store_discovery(
        self,
        summary: str,
        discovery_type: str,
        severity: str,
        tags: list[str] | None = None,
        details: str | None = None,
        **kwargs: Any,
    ) -> NoteResult:
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
        raw = self.call_tool("knowledge", args)
        return NoteResult.model_validate(raw)

    def audit_knowledge(
        self, scope: str = "open", top_n: int = 10, **kwargs: Any
    ) -> AuditResult:
        args: dict[str, Any] = {
            "action": "audit",
            "scope": scope,
            "top_n": str(top_n),
            "use_model": "true",
        }
        args.update(kwargs)
        raw = self.call_tool("knowledge", args)
        return AuditResult.model_validate(raw)

    def cleanup_knowledge(
        self, dry_run: bool = False, **kwargs: Any
    ) -> CleanupResult:
        args: dict[str, Any] = {"action": "cleanup", "dry_run": str(dry_run).lower()}
        args.update(kwargs)
        raw = self.call_tool("knowledge", args)
        return CleanupResult.model_validate(raw)

    # --- Lifecycle ---

    def archive_orphan_agents(self, **kwargs: Any) -> ArchiveResult:
        raw = self.call_tool("archive_orphan_agents", kwargs)
        return ArchiveResult.model_validate(raw)

    def self_recovery(self, action: str = "quick", **kwargs: Any) -> RecoveryResult:
        args: dict[str, Any] = {"action": action}
        args.update(kwargs)
        raw = self.call_tool("self_recovery", args)
        return RecoveryResult.model_validate(raw)

    # --- Metrics ---

    def get_metrics(self, **kwargs: Any) -> MetricsResult:
        raw = self.call_tool("get_governance_metrics", kwargs)
        return MetricsResult.model_validate(raw)

    # --- Model inference ---

    def call_model(
        self,
        prompt: str,
        provider: str | None = None,
        model: str | None = None,
        max_tokens: int = 1024,
        temperature: float = 0.0,
        **kwargs: Any,
    ) -> ModelResult:
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
        raw = self.call_tool("call_model", args)
        return ModelResult.model_validate(raw)

    # --- REST transport ---

    def _rest_call(
        self, tool_name: str, arguments: dict, *, timeout: float | None = None
    ) -> dict:
        """POST to /v1/tools/call and parse the response envelope."""
        effective_timeout = timeout or self.timeout
        payload = json.dumps({"name": tool_name, "arguments": arguments}).encode()
        req = urllib.request.Request(
            self.rest_url,
            data=payload,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=effective_timeout) as resp:
                data = json.loads(resp.read().decode())
        except TimeoutError as e:
            raise GovernanceTimeoutError(
                f"{tool_name} timed out after {effective_timeout}s"
            ) from e
        except urllib.error.URLError as e:
            if isinstance(getattr(e, "reason", None), TimeoutError):
                raise GovernanceTimeoutError(
                    f"{tool_name} timed out after {effective_timeout}s"
                ) from e
            raise GovernanceConnectionError(f"REST call to {self.rest_url} failed: {e}") from e

        if not data.get("success", False):
            error = data.get("error", "Unknown error")
            raise GovernanceConnectionError(f"Tool {tool_name} failed: {error}")

        result = data.get("result")
        if result is None:
            raise GovernanceConnectionError(f"No result from {tool_name}")

        # Check MCP-level isError flag (outer envelope success doesn't
        # guarantee the tool itself succeeded).
        if isinstance(result, dict) and result.get("isError"):
            content = result.get("content", [])
            error_text = " ".join(
                item["text"]
                for item in content
                if isinstance(item, dict) and "text" in item
            )
            raise GovernanceConnectionError(
                f"Tool {tool_name} returned error: {error_text or 'unknown'}"
            )

        # _build_http_tool_response normalizes most core tools to a plain dict.
        # For single-content-block results, the result may still be a string.
        if isinstance(result, str):
            try:
                return json.loads(result)
            except json.JSONDecodeError:
                return {"text": result, "raw": True}

        if isinstance(result, dict):
            # Multi-content block: {content: [{type, text}, ...]}
            content = result.get("content")
            if content and isinstance(content, list):
                merged: dict = {}
                for item in content:
                    if isinstance(item, dict) and "text" in item:
                        try:
                            parsed = json.loads(item["text"])
                            if isinstance(parsed, dict):
                                merged.update(parsed)
                        except json.JSONDecodeError:
                            pass
                return merged if merged else result
            return result

        raise GovernanceConnectionError(
            f"Unexpected result type from {tool_name}: {type(result)}"
        )

    # --- MCP transport (sync wrapper) ---

    def _ensure_async_client(self) -> None:
        if self._async_client is None:
            try:
                asyncio.get_running_loop()
                raise RuntimeError(
                    "SyncGovernanceClient(transport='mcp') cannot be used inside "
                    "a running event loop. Use transport='rest' or the async "
                    "GovernanceClient instead."
                )
            except RuntimeError as e:
                if "no running event loop" not in str(e).lower():
                    raise
            from unitares_sdk.client import GovernanceClient

            self._async_client = GovernanceClient(
                mcp_url=self.mcp_url, timeout=self.timeout
            )
            asyncio.run(self._async_client.connect())

    def _mcp_call(
        self, tool_name: str, arguments: dict, *, timeout: float | None = None
    ) -> dict:
        """Route through async client via asyncio.run()."""
        self._ensure_async_client()
        assert self._async_client is not None
        return asyncio.run(
            self._async_client.call_tool(tool_name, arguments, timeout=timeout)
        )

    # --- Session helpers (shared with async client) ---

    def _inject_session(self, tool_name: str, arguments: dict) -> dict:
        if tool_name in _IDENTITY_TOOLS:
            return arguments
        args = dict(arguments)
        if self.client_session_id and "client_session_id" not in args:
            args["client_session_id"] = self.client_session_id
        return args

    def _capture_identity(self, raw: dict) -> None:
        # See GovernanceClient._capture_identity for why this short-circuits
        # on declared failure. call_tool() raises in that case, but external
        # callers may pass arbitrary dicts.
        if raw.get("success") is False:
            return

        from unitares_sdk.client import GovernanceClient

        sid = GovernanceClient._extract_session_id(raw)
        token = GovernanceClient._extract_continuity_token(raw)
        uuid = GovernanceClient._extract_uuid(raw)

        if uuid:
            if self.agent_uuid and uuid != self.agent_uuid:
                raise IdentityDriftError(self.agent_uuid, uuid)
            self.agent_uuid = uuid
        if sid:
            self.client_session_id = sid
        if token:
            self.continuity_token = token

    @staticmethod
    def _raise_for_tool_failure(tool_name: str, raw: dict) -> None:
        if raw.get("success") is False:
            error = raw.get("error", "Unknown error")
            raise GovernanceConnectionError(f"Tool {tool_name} failed: {error}")
