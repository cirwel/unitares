"""Small Python client for the lease-plane service.

This module is intentionally synchronous and stdlib-only. It is meant for
callers that need a stable contract before the Elixir node exists, not for MCP
handler paths that would block the anyio task group.
"""

from __future__ import annotations

import json
import logging
import random
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import deque
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from pydantic import ValidationError

logger = logging.getLogger(__name__)

from .models import (
    AcquireHeldByOther,
    AcquireOk,
    AcquirePermissionDenied,
    AcquireRequest,
    AcquireResult,
    AcquireSchemaInvalid,
    AcquireServiceUnavailable,
    ForceReleaseRequest,
    HandoffAcceptRequest,
    HandoffOfferRequest,
    HealthOk,
    HealthResult,
    HealthUnavailable,
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
    # §7.10: force-release requires a separate elevated token, sourced from
    # ~/.config/cirwel/secrets.env as LEASE_FORCE_RELEASE_TOKEN. Used only by
    # force_release(); release() rejects release_reason='forced' regardless.
    force_release_token: str | None = None
    timeout_s: float = 1.0


@dataclass(frozen=True)
class LeaseHTTPRequest:
    method: str
    url: str
    headers: dict[str, str]
    json_body: dict[str, Any] | None
    timeout_s: float


LeaseTransport = Callable[[LeaseHTTPRequest], Mapping[str, Any]]


# Wave 3 §14 prereq PR #6: per-call measurement samples for the disconfirmer
# (B) baseline. The recorder below appends; the MCP server's
# perf_monitor_persist_task drains every ~5min into
# audit.coordination_measurements as measurement.lease_plane.request rows.
# Client-perceived elapsed is the point — §0(B) budgets the FULL crossing
# cost ("full request marshalling vs lease ack"), which plane-side timing
# would understate. Processes without the persist task (plugin hooks,
# short-lived scripts) never drain: the deque is bounded, oldest samples
# drop, and the drop count is visible via measurement_samples_dropped() —
# a documented coverage note, not silent truncation. Sample tuple shape:
# (ts_utc, endpoint_path, method, outcome, elapsed_ms_int, payload_bytes).
_MEASUREMENT_SAMPLES: deque = deque(maxlen=10_000)
_MEASUREMENT_SAMPLES_DROPPED = 0


def drain_measurement_samples() -> list[tuple]:
    """Pop all pending measurement samples (deque ops are atomic)."""
    out: list[tuple] = []
    while True:
        try:
            out.append(_MEASUREMENT_SAMPLES.popleft())
        except IndexError:
            return out


def measurement_samples_dropped() -> int:
    """How many samples were dropped to the maxlen bound (coverage signal)."""
    return _MEASUREMENT_SAMPLES_DROPPED


def _record_lease_rpc_latency(
    path: str,
    start_perf: float,
    outcome: str,
    *,
    method: str = "?",
    payload_bytes: int | None = None,
) -> None:
    """Record lease RPC latency to the in-process perf_monitor + sample deque.

    Best-effort: a profile run with perf_monitor missing or import-broken must
    never break a lease call. Key shape: ``lease_plane.client.<path>.<outcome>``
    plus a coarser ``lease_plane.client.<path>`` aggregate.
    """
    global _MEASUREMENT_SAMPLES_DROPPED
    elapsed_ms = (time.perf_counter() - start_perf) * 1000.0
    try:
        # len-check-then-increment is not atomic as a pair: assumes the
        # standard GIL-enabled CPython build (free-threading is opt-in on
        # 3.13+/3.14). Under a thread switch the counter can undercount —
        # acceptable at coverage-signal precision; the deque's own maxlen
        # still bounds memory regardless.
        if len(_MEASUREMENT_SAMPLES) == _MEASUREMENT_SAMPLES.maxlen:
            _MEASUREMENT_SAMPLES_DROPPED += 1
        _MEASUREMENT_SAMPLES.append(
            (datetime.now(UTC), path, method, outcome, int(round(elapsed_ms)), payload_bytes)
        )
    except Exception:  # noqa: BLE001 — recorder must never break a lease call
        pass
    # Optional host hook: when running inside the unitares server checkout,
    # bridge timings into its perf monitor. Silently absent everywhere else —
    # the SDK package itself never requires src.* to be importable.
    try:
        from src.perf_monitor import record_ms
    except Exception:
        return
    op = path.lstrip("/").replace("/", ".")
    record_ms(f"lease_plane.client.{op}", elapsed_ms)
    record_ms(f"lease_plane.client.{op}.{outcome}", elapsed_ms)


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
        payload = self._request_json("POST", "/v1/lease/acquire", request.model_dump(mode="json", exclude_none=True))
        return _parse_acquire(payload)

    def acquire_with_retry(
        self,
        request: AcquireRequest,
        *,
        max_attempts: int = 5,
        floor_s: float = 0.1,
        ceiling_s: float = 5.0,
        sleep: Callable[[float], None] | None = None,
        rng: Callable[[], float] | None = None,
    ) -> AcquireResult:
        """Acquire with jittered exponential backoff on `held_by_other`.

        RFC v0.8 §7.3.3 contract: floor 100ms, ceiling 5s, full jitter
        (per AWS Architecture Blog convention). `retry_after_hint_ms` from
        the server (set in the §7.3.2 extended typed-absence shape) is
        honored as a per-attempt floor — the wait is at least that long.

        Terminal results (no retry):
          - AcquireOk
          - AcquireServiceUnavailable (advisory escape valve)
          - AcquirePermissionDenied / AcquireSchemaInvalid (caller bug)
        Only `held_by_other` triggers retry. After max_attempts, the final
        held_by_other is returned to the caller.

        Args:
            max_attempts: total acquire attempts including the first (default 5).
            floor_s: minimum backoff per attempt (default 0.1s = 100ms).
            ceiling_s: maximum backoff per attempt (default 5.0s).
            sleep: injectable sleep for test determinism (default time.sleep).
            rng: injectable [0,1) random for test determinism (default random.random).

        Raises:
            ValueError: max_attempts < 1, or floor_s > ceiling_s
                (PR 5 council fix — pre-PR-5 silently fired one HTTP call when max_attempts=0).
        """
        if max_attempts < 1:
            raise ValueError(f"max_attempts must be >= 1, got {max_attempts}")
        if floor_s > ceiling_s:
            raise ValueError(
                f"floor_s ({floor_s}) must be <= ceiling_s ({ceiling_s})"
            )

        # AcquireHeldByOther is the only retry-triggering result type;
        # AcquireOk no longer needed here (NIT-1 fix from review pass).
        from .models import AcquireHeldByOther

        sleep_fn = sleep or time.sleep
        rand_fn = rng or random.random

        result: AcquireResult = self.acquire(request)
        attempt = 1
        while attempt < max_attempts and isinstance(result, AcquireHeldByOther):
            # Full-jitter exponential backoff. Cap exponent to avoid overflow.
            exp_cap = min(2 ** min(attempt - 1, 10) * floor_s, ceiling_s)
            backoff = rand_fn() * exp_cap
            backoff = max(backoff, floor_s)
            # retry_after_hint_ms (server-provided) raises the floor for THIS attempt only.
            hint_floor = (result.retry_after_hint_ms or 0) / 1000.0
            backoff = max(backoff, hint_floor)
            sleep_fn(backoff)
            result = self.acquire(request)
            attempt += 1
        return result

    def status(self, surface_id: str) -> StatusResult:
        payload = self._request_json("GET", "/v1/lease/status", None, query={"surface_id": surface_id})
        return _parse_status(payload)

    def renew(self, request: RenewRequest) -> SimpleResult:
        payload = self._request_json("POST", "/v1/lease/renew", request.model_dump(mode="json", exclude_none=True))
        return _parse_simple(payload)

    def heartbeat(self, request: HeartbeatRequest) -> SimpleResult:
        payload = self._request_json("POST", "/v1/lease/heartbeat", request.model_dump(mode="json", exclude_none=True))
        return _parse_simple(payload)

    def release(self, request: ReleaseRequest) -> SimpleResult:
        # §7.10 contract-layer rejection: force-release MUST go through
        # force_release(), which uses LEASE_FORCE_RELEASE_TOKEN. The standard
        # release path uses the regular bearer (LEASE_PLANE_BEARER_TOKEN /
        # GOVERNANCE_TOKEN) and must not accept release_reason='forced'.
        if request.release_reason == "forced":
            return SimpleError(
                ok=False,
                error="permission_denied",
                reason="release_reason='forced' requires force_release(); see RFC §7.10",
            )
        payload = self._request_json("POST", "/v1/lease/release", request.model_dump(mode="json", exclude_none=True))
        return _parse_simple(payload)

    def force_release(self, request: ForceReleaseRequest) -> SimpleResult:
        """Operator-only force-release via POST /v1/lease/force-release (§7.10).

        Sends to the dedicated /v1/lease/force-release endpoint (added in PR 1)
        using LEASE_FORCE_RELEASE_TOKEN. The Elixir router enforces path-level
        mutual exclusion: the force_release_token is accepted only on this path,
        and the standard bearer is rejected here. If force_release_token is
        unset, returns permission_denied without sending — contract-layer rejection.
        """
        if not self.config.force_release_token:
            return SimpleError(
                ok=False,
                error="permission_denied",
                reason="force_release_token not configured; force-release requires LEASE_FORCE_RELEASE_TOKEN",
            )
        payload = self._request_json(
            "POST",
            "/v1/lease/force-release",
            request.model_dump(mode="json", exclude_none=True),
            authorization_token=self.config.force_release_token,
        )
        return _parse_simple(payload)

    def handoff_offer(self, request: HandoffOfferRequest) -> SimpleResult:
        payload = self._request_json("POST", "/v1/lease/handoff/offer", request.model_dump(mode="json", exclude_none=True))
        return _parse_simple(payload)

    def handoff_accept(self, request: HandoffAcceptRequest) -> SimpleResult:
        payload = self._request_json("POST", "/v1/lease/handoff/accept", request.model_dump(mode="json", exclude_none=True))
        return _parse_simple(payload)

    def health_check(self, *, timeout_s: float | None = None) -> HealthResult:
        """Liveness probe against the BEAM lease-plane (Wave 2 Phase C).

        Returns HealthOk if the boundary round-trips a 200 with a valid
        envelope, HealthUnavailable otherwise. Distinct from the typed
        operation results: a `service_unavailable` here means the boundary
        itself didn't confirm liveness, not that a specific lease op failed.

        `timeout_s` overrides the client's default for this single probe —
        health probes typically want a tighter budget than full ops (the
        whole point is fast liveness, not waiting on the slow path). When
        omitted, falls back to the client's configured timeout.
        """
        path = "/v1/health"
        url = self.config.base_url.rstrip("/") + path
        headers = {"Accept": "application/json"}
        if self.config.bearer_token:
            headers["Authorization"] = f"Bearer {self.config.bearer_token}"

        request = LeaseHTTPRequest(
            method="GET",
            url=url,
            headers=headers,
            json_body=None,
            timeout_s=timeout_s if timeout_s is not None else self.config.timeout_s,
        )
        try:
            payload = self._transport(request)
        except Exception as exc:  # noqa: BLE001 — health probe NEVER raises
            return HealthUnavailable(
                ok=False,
                error="service_unavailable",
                reason=f"transport failure: {type(exc).__name__}",
            )
        if not isinstance(payload, Mapping):
            return HealthUnavailable(
                ok=False,
                error="service_unavailable",
                reason="response was not a JSON object",
            )
        _check_protocol_version(payload, path)
        try:
            if payload.get("ok") is True:
                return HealthOk.model_validate(payload)
            # Server returned 401/503/etc as a JSON envelope — surface the
            # reason if present, else describe the shape.
            reason = payload.get("reason") or payload.get("error") or "unhealthy"
            return HealthUnavailable(
                ok=False,
                error="service_unavailable",
                reason=str(reason),
            )
        except ValidationError as exc:
            return HealthUnavailable(
                ok=False,
                error="service_unavailable",
                reason=f"response failed validation: {exc.errors()!r}",
            )

    def _request_json(
        self,
        method: str,
        path: str,
        json_body: dict[str, Any] | None,
        *,
        query: dict[str, str] | None = None,
        authorization_token: str | None = None,
    ) -> Mapping[str, Any]:
        url = self.config.base_url.rstrip("/") + path
        if query:
            url = url + "?" + urllib.parse.urlencode(query)

        headers = {"Accept": "application/json"}
        if json_body is not None:
            headers["Content-Type"] = "application/json"
        token = authorization_token if authorization_token is not None else self.config.bearer_token
        if token:
            headers["Authorization"] = f"Bearer {token}"

        request = LeaseHTTPRequest(
            method=method,
            url=url,
            headers=headers,
            json_body=json_body,
            timeout_s=self.config.timeout_s,
        )
        _body_bytes = (
            len(json.dumps(json_body, separators=(",", ":")).encode("utf-8"))
            if json_body is not None
            else None
        )
        _start = time.perf_counter()
        try:
            payload = self._transport(request)
        except Exception:
            _record_lease_rpc_latency(
                path, _start, "transport_exception", method=method, payload_bytes=_body_bytes
            )
            return {"ok": False, "error": "service_unavailable"}
        if not isinstance(payload, Mapping):
            _record_lease_rpc_latency(
                path, _start, "schema_invalid", method=method, payload_bytes=_body_bytes
            )
            return {"ok": False, "error": "schema_invalid", "detail": "response was not an object"}
        # Default-True on `ok` is wrong: a malformed response without an `ok`
        # key (custom transport, future caller) would be silently recorded
        # as success. Treat absent `ok` as an error. Use `unknown_error` as
        # the bucket for `ok:false` rows with no `error` discriminant so
        # they don't collapse into a literal `"error"` perf_monitor key.
        if payload.get("ok") is True:
            outcome = "ok"
        else:
            outcome = str(payload.get("error") or "unknown_error")
        _check_protocol_version(payload, path)
        _record_lease_rpc_latency(
            path, _start, outcome, method=method, payload_bytes=_body_bytes
        )
        return payload


class LeasePlaneDisabledClient(LeasePlaneClient):
    """Advisory-mode fallback for callers that have no lease service configured."""

    def __init__(self) -> None:
        super().__init__(transport=lambda _: {"ok": False, "error": "service_unavailable"})


# Wave 2 §"Lease-integration boundary hardening" — versioned contracts.
# Module-level dedup state so a steady mismatch (post-deploy of one side
# only) doesn't spam the log every call. The (path, server_version) key is
# small enough to bound — `path` is a closed set of route templates and
# `server_version` is whatever the BEAM is reporting today.
_logged_protocol_mismatches: set[tuple[str, str]] = set()
_logged_protocol_absences: bool = False


def _check_protocol_version(payload: Mapping[str, Any], path: str) -> None:
    """Compare the server's reported protocol_version to PROTOCOL_VERSION.

    Failure-safe: never raises, never alters the payload. On mismatch logs
    a single WARNING per (path, server_version) pair so a stuck mismatch
    is loud once but quiet thereafter. On absence (older BEAM that hasn't
    deployed the field yet) logs ONE info-level breadcrumb per process —
    the rollout grace window where some routes are versioned and others
    aren't is expected and shouldn't generate noise.
    """
    # Local import keeps tests free to monkeypatch the module-level constant.
    from unitares_sdk.lease_plane import PROTOCOL_VERSION

    server_version = payload.get("protocol_version")
    if server_version is None:
        global _logged_protocol_absences
        if not _logged_protocol_absences:
            logger.debug(
                "[lease-plane] response %s has no protocol_version field — "
                "Wave 2 rollout grace; client expecting %r once both sides "
                "have deployed the boundary version",
                path,
                PROTOCOL_VERSION,
            )
            _logged_protocol_absences = True
        return
    if server_version == PROTOCOL_VERSION:
        return
    key = (path, str(server_version))
    if key in _logged_protocol_mismatches:
        return
    _logged_protocol_mismatches.add(key)
    logger.warning(
        "[lease-plane] protocol_version mismatch on %s: client expected %r, "
        "server returned %r — responses may be parsed with stale shape "
        "assumptions; coordinate a deploy",
        path,
        PROTOCOL_VERSION,
        server_version,
    )


# Wave 2 §"Lease-integration boundary hardening" — Phase B (error translation
# table). One declarative dict per endpoint family that maps the BEAM-emitted
# `error` discriminant to the typed Pydantic result model. Adding a new error
# variant means: add the typed model in models.py, add the discriminant here,
# done. The previous if-else chain meant an undocumented variant silently
# degraded to `service_unavailable` — the new design surfaces unknowns
# explicitly via `_unknown_error_fallback` so a Python ↔ BEAM mapping drift
# becomes a visible reason string instead of a swallowed misclassification.
#
# `BEAM_EMITTED_ACQUIRE_ERRORS` etc. enumerate the discriminants the Elixir
# router actually emits today (per `elixir/lease_plane/lib/unitares_lease_plane/
# {http_router,http_auth}.ex`). The contract tests in
# `tests/test_lease_plane_error_translation.py` assert that every BEAM-side
# discriminant has a Python mapping; drift catches at test time before it
# silently degrades errors in production.
_ACQUIRE_ERROR_PARSERS: dict[str, type] = {
    "held_by_other": AcquireHeldByOther,
    "permission_denied": AcquirePermissionDenied,
    "schema_invalid": AcquireSchemaInvalid,
    "service_unavailable": AcquireServiceUnavailable,
}

_STATUS_ERROR_PARSERS: dict[str, type] = {
    "schema_invalid": StatusSchemaInvalid,
    "service_unavailable": StatusServiceUnavailable,
}

# `_parse_simple` covers renew/heartbeat/release/handoff_offer/handoff_accept/
# force_release. The BEAM router emits the first six; `not_holder` and
# `already_released` are reserved for future server-side semantics (see
# surface_registry.ex `:not_holder` return — currently caught by the generic
# `{:error, reason}` arm and surfaces as `service_unavailable`, but the
# typed model accepts them so a future router map-through doesn't require a
# coordinated client deploy).
_SIMPLE_ACCEPTED_ERRORS: frozenset[str] = frozenset({
    "not_found",
    "expired",
    "not_holder",
    "already_released",
    "permission_denied",
    "schema_invalid",
    "service_unavailable",
})

# Authoritative BEAM-emitted error sets per endpoint family. Mirrors the
# `elixir/lease_plane/lib/unitares_lease_plane/http_router.ex` discriminants
# and the `http_auth.ex` 401 paths. Synced by the contract tests.
BEAM_EMITTED_ACQUIRE_ERRORS: frozenset[str] = frozenset({
    "held_by_other",
    "permission_denied",
    "schema_invalid",
    "service_unavailable",
})

BEAM_EMITTED_STATUS_ERRORS: frozenset[str] = frozenset({
    # /v1/lease/status emits service_unavailable on internal error and
    # schema_invalid on missing/invalid surface_id. 404 returns ok:true with
    # lease=null (typed-absence shape), not an error envelope.
    "schema_invalid",
    "service_unavailable",
})

BEAM_EMITTED_SIMPLE_ERRORS: frozenset[str] = frozenset({
    # renew / heartbeat / release / handoff_offer / handoff_accept /
    # force_release. `expired` is renew-specific (409); `not_found` is the
    # 404 across all simple-shaped endpoints; `permission_denied` covers
    # both auth-layer 401 and force-release token mismatch.
    "not_found",
    "expired",
    "permission_denied",
    "schema_invalid",
    "service_unavailable",
})


def _unknown_error_fallback(payload: Mapping[str, Any]) -> str:
    """Build a `reason` string that names the unknown discriminant. Pre-Phase-B
    these were silently coerced to `service_unavailable` with no signal —
    the operator had to dig through Elixir router logs to find the actual
    discriminant. Naming it in the Python-side reason makes drift visible."""
    raw = payload.get("error")
    if raw is None:
        return "missing error discriminant in BEAM response"
    return f"unrecognized error discriminant: {raw!r} (BEAM/Python registry drift?)"


def _parse_acquire(payload: Mapping[str, Any]) -> AcquireResult:
    try:
        if payload.get("ok") is True:
            return AcquireOk.model_validate(payload)
        model_cls = _ACQUIRE_ERROR_PARSERS.get(payload.get("error"))
        if model_cls is None:
            return AcquireServiceUnavailable.model_validate({
                "ok": False,
                "error": "service_unavailable",
                "reason": _unknown_error_fallback(payload),
            })
        return model_cls.model_validate(payload)
    except ValidationError as exc:
        return AcquireSchemaInvalid(ok=False, error="schema_invalid", detail=exc.errors())


def _parse_status(payload: Mapping[str, Any]) -> StatusResult:
    try:
        if payload.get("ok") is True:
            return StatusOk.model_validate(payload)
        model_cls = _STATUS_ERROR_PARSERS.get(payload.get("error"))
        if model_cls is None:
            return StatusServiceUnavailable.model_validate({
                "ok": False,
                "error": "service_unavailable",
                "reason": _unknown_error_fallback(payload),
            })
        return model_cls.model_validate(payload)
    except ValidationError as exc:
        return StatusSchemaInvalid(ok=False, error="schema_invalid", detail=exc.errors())


def _parse_simple(payload: Mapping[str, Any]) -> SimpleResult:
    try:
        if payload.get("ok") is True:
            return SimpleOk.model_validate(payload)
        error = payload.get("error")
        if error in _SIMPLE_ACCEPTED_ERRORS:
            return SimpleError.model_validate(payload)
        return SimpleError(
            ok=False,
            error="service_unavailable",
            reason=_unknown_error_fallback(payload),
        )
    except ValidationError as exc:
        return SimpleError(ok=False, error="schema_invalid", detail=exc.errors())
