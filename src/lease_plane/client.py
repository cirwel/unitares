"""Small Python client for the lease-plane service.

This module is intentionally synchronous and stdlib-only. It is meant for
callers that need a stable contract before the Elixir node exists, not for MCP
handler paths that would block the anyio task group.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any

from pydantic import ValidationError

from .models import (
    AcquireHeldByOther,
    AcquireOk,
    AcquirePermissionDenied,
    AcquireRequest,
    AcquireResult,
    AcquireSchemaInvalid,
    AcquireServiceUnavailable,
    HandoffAcceptRequest,
    HandoffOfferRequest,
    HeartbeatRequest,
    ReleaseRequest,
    RenewRequest,
    SimpleError,
    SimpleOk,
    SimpleResult,
    StatusOk,
    StatusResult,
    StatusSchemaInvalid,
    StatusServiceUnavailable,
)


@dataclass(frozen=True)
class LeasePlaneClientConfig:
    base_url: str = "http://127.0.0.1:8788"
    bearer_token: str | None = None
    timeout_s: float = 1.0


@dataclass(frozen=True)
class LeaseHTTPRequest:
    method: str
    url: str
    headers: dict[str, str]
    json_body: dict[str, Any] | None
    timeout_s: float


LeaseTransport = Callable[[LeaseHTTPRequest], Mapping[str, Any]]


def _urllib_transport(request: LeaseHTTPRequest) -> Mapping[str, Any]:
    body = None
    if request.json_body is not None:
        body = json.dumps(request.json_body, separators=(",", ":")).encode("utf-8")

    req = urllib.request.Request(
        request.url,
        data=body,
        headers=request.headers,
        method=request.method,
    )
    try:
        with urllib.request.urlopen(req, timeout=request.timeout_s) as response:
            raw = response.read()
    except urllib.error.HTTPError as exc:
        raw = exc.read()
        if raw:
            try:
                payload = json.loads(raw.decode("utf-8"))
                if isinstance(payload, Mapping):
                    return payload
            except json.JSONDecodeError:
                pass
        if exc.code in (401, 403):
            return {"ok": False, "error": "permission_denied", "reason": str(exc.reason)}
        return {"ok": False, "error": "service_unavailable"}

    if not raw:
        return {"ok": False, "error": "schema_invalid", "detail": "empty response body"}
    try:
        payload = json.loads(raw.decode("utf-8"))
    except json.JSONDecodeError:
        return {"ok": False, "error": "schema_invalid", "detail": "response was not JSON"}
    if not isinstance(payload, Mapping):
        return {"ok": False, "error": "schema_invalid", "detail": "response was not an object"}
    return payload


class LeasePlaneClient:
    """Contract client for lease acquire/status/renew/release calls."""

    def __init__(
        self,
        config: LeasePlaneClientConfig | None = None,
        *,
        transport: LeaseTransport | None = None,
    ) -> None:
        self.config = config or LeasePlaneClientConfig()
        self._transport = transport or _urllib_transport

    def acquire(self, request: AcquireRequest) -> AcquireResult:
        payload = self._request_json("POST", "/v1/lease/acquire", request.model_dump(mode="json"))
        return _parse_acquire(payload)

    def status(self, surface_id: str) -> StatusResult:
        payload = self._request_json("GET", "/v1/lease/status", None, query={"surface_id": surface_id})
        return _parse_status(payload)

    def renew(self, request: RenewRequest) -> SimpleResult:
        payload = self._request_json("POST", "/v1/lease/renew", request.model_dump(mode="json"))
        return _parse_simple(payload)

    def heartbeat(self, request: HeartbeatRequest) -> SimpleResult:
        payload = self._request_json("POST", "/v1/lease/heartbeat", request.model_dump(mode="json"))
        return _parse_simple(payload)

    def release(self, request: ReleaseRequest) -> SimpleResult:
        payload = self._request_json("POST", "/v1/lease/release", request.model_dump(mode="json"))
        return _parse_simple(payload)

    def handoff_offer(self, request: HandoffOfferRequest) -> SimpleResult:
        payload = self._request_json("POST", "/v1/lease/handoff/offer", request.model_dump(mode="json"))
        return _parse_simple(payload)

    def handoff_accept(self, request: HandoffAcceptRequest) -> SimpleResult:
        payload = self._request_json("POST", "/v1/lease/handoff/accept", request.model_dump(mode="json"))
        return _parse_simple(payload)

    def _request_json(
        self,
        method: str,
        path: str,
        json_body: dict[str, Any] | None,
        *,
        query: dict[str, str] | None = None,
    ) -> Mapping[str, Any]:
        url = self.config.base_url.rstrip("/") + path
        if query:
            url = url + "?" + urllib.parse.urlencode(query)

        headers = {"Accept": "application/json"}
        if json_body is not None:
            headers["Content-Type"] = "application/json"
        if self.config.bearer_token:
            headers["Authorization"] = f"Bearer {self.config.bearer_token}"

        request = LeaseHTTPRequest(
            method=method,
            url=url,
            headers=headers,
            json_body=json_body,
            timeout_s=self.config.timeout_s,
        )
        try:
            payload = self._transport(request)
        except Exception:
            return {"ok": False, "error": "service_unavailable"}
        if not isinstance(payload, Mapping):
            return {"ok": False, "error": "schema_invalid", "detail": "response was not an object"}
        return payload


class LeasePlaneDisabledClient(LeasePlaneClient):
    """Advisory-mode fallback for callers that have no lease service configured."""

    def __init__(self) -> None:
        super().__init__(transport=lambda _: {"ok": False, "error": "service_unavailable"})


def _parse_acquire(payload: Mapping[str, Any]) -> AcquireResult:
    try:
        if payload.get("ok") is True:
            return AcquireOk.model_validate(payload)
        error = payload.get("error")
        if error == "held_by_other":
            return AcquireHeldByOther.model_validate(payload)
        if error == "permission_denied":
            return AcquirePermissionDenied.model_validate(payload)
        if error == "schema_invalid":
            return AcquireSchemaInvalid.model_validate(payload)
        return AcquireServiceUnavailable.model_validate({"ok": False, "error": "service_unavailable"})
    except ValidationError as exc:
        return AcquireSchemaInvalid(ok=False, error="schema_invalid", detail=exc.errors())


def _parse_status(payload: Mapping[str, Any]) -> StatusResult:
    try:
        if payload.get("ok") is True:
            return StatusOk.model_validate(payload)
        if payload.get("error") == "schema_invalid":
            return StatusSchemaInvalid.model_validate(payload)
        return StatusServiceUnavailable.model_validate({"ok": False, "error": "service_unavailable"})
    except ValidationError as exc:
        return StatusSchemaInvalid(ok=False, error="schema_invalid", detail=exc.errors())


def _parse_simple(payload: Mapping[str, Any]) -> SimpleResult:
    try:
        if payload.get("ok") is True:
            return SimpleOk.model_validate(payload)
        error = payload.get("error")
        if error in {
            "not_found",
            "expired",
            "not_holder",
            "already_released",
            "permission_denied",
            "schema_invalid",
            "service_unavailable",
        }:
            return SimpleError.model_validate(payload)
        return SimpleError(
            ok=False,
            error="service_unavailable",
            reason=f"unrecognized error: {error!r}" if error is not None else "missing error discriminant",
        )
    except ValidationError as exc:
        return SimpleError(ok=False, error="schema_invalid", detail=exc.errors())
