"""Governed dispatch of the dialectic reviewer spawn.

Routes the orchestrated-reviewer spawn through the lease plane's governed-effect
surface (``POST /v1/effects``, ``custody_mode=execute``,
``effect_type=agent_spawn``) instead of POSTing the orchestrator directly. This
makes the dialectic dispatch the fleet's first *standing* agent_spawn producer:
every reviewer spawn leaves a durable ``audit.events`` record (effect_id,
proposer, dialectic session), is idempotency-deduplicated against concurrent
same-session dispatches, and carries the §7/§8 proof material so the per-type
binding flag (``UNITARES_GOVERNED_EFFECT_BINDING_AGENT_SPAWN``) becomes
flippable without further producer code.

Honest scope (recorded in docs/proposals/governed-reviewer-spawn-v0.md): the
proposer here is a standing dispatcher identity whose continuity token is
minted in-process by gov-mcp itself. For THIS producer the §6 behavioral veto
and §7 tier recert are therefore structurally weak — gov-mcp is issuer, prover
and verifier of the same credential. What the governed path buys is durable
attribution, idempotent spawn dedup, and the architectural seam (spawns flow
through /v1/effects); it is not, for this producer, an adversarial enforcement
gain. Do not cite it as one.

Failure routing (load-bearing — each bucket chosen deliberately):

- ``COMMITTED``  — 202 with ``status=="committed"`` and a non-empty
  ``agent_id``. The reviewer is running, governed.
- ``REFUSED``    — the governed surface answered and the answer is either a
  refusal or an ambiguous outcome. Callers must degrade to the IN-PROCESS
  synthetic reviewer and must NOT fall back to a direct orchestrator spawn:
    * 403 governance_blocked — a direct spawn would bypass the veto;
    * timeout / error after the request was sent — the plane may have spawned
      (agent_spawn is irreversible; a direct retry risks a double spawn);
    * idempotent replay or 409 — a prior dispatch for this spec already ran;
      the synthetic path resolves the session now and dialectic resolution is
      idempotent, so a still-running prior reviewer cannot corrupt it;
    * canonicalization/mint failure — the payload embeds thesis text authored
      by the agent under review; routing this bucket to the direct path would
      put "does the governance check run" under the subject's control.
- ``UNAVAILABLE`` — the governed surface is not serving (connection refused,
  501 flag-off, 5xx before anything could spawn). Plane-down is an
  availability condition, not a governance refusal: callers fall back to the
  existing direct orchestrator path.
- ``CONFIG_ERROR`` — producer misconfiguration (malformed dispatcher UUID,
  missing bearer/secret). Logged loudly; callers behave as if the governed
  flag were off (direct path), because a typo must not silently degrade every
  dialectic to synthetic review.
"""
from __future__ import annotations

import os
import re
from dataclasses import dataclass
from enum import Enum
from typing import Any, Dict, Optional

from src.logging_utils import get_logger

logger = get_logger(__name__)

# C0 controls the canonical payload form refuses (everything below 0x20 except
# \b \t \n \f \r — see unitares_sdk.lease_plane.canonical._SHORT_ESCAPE_OK).
# Thesis text is agent-authored free text and routinely picks up ANSI escapes
# (U+001B) from pasted terminal output; strip the refused range so the mint
# cannot be forced to fail by payload content. NB: `canonical` (payload-hash
# form) is a DIFFERENT module from `canonicalize` (surface-id normalization);
# import the former.
_REFUSED_C0_RE = re.compile(r"[\x00-\x07\x0b\x0e-\x1f]")

_UUID_RE = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$", re.IGNORECASE
)

_GRANT_TTL_SECONDS = 120  # default 30s is shorter than the plane's worst-case
# pre-veto latency (idempotency scan + veto round-trip); 120s keeps a slow
# plane from converting into binding_expired vetoes once the per-type flag flips.


class GovernedOutcome(Enum):
    COMMITTED = "committed"
    REFUSED = "refused"
    UNAVAILABLE = "unavailable"
    CONFIG_ERROR = "config_error"


@dataclass
class GovernedSpawnResult:
    outcome: GovernedOutcome
    agent_id: Optional[str] = None
    effect_id: Optional[str] = None
    detail: str = ""


def governed_spawn_enabled() -> bool:
    """Opt-in gate (default OFF). OFF preserves today's behaviour exactly."""
    return os.environ.get(
        "UNITARES_DIALECTIC_GOVERNED_SPAWN", "0"
    ).strip().lower() in ("1", "true", "yes", "on")


def dispatcher_uuid() -> Optional[str]:
    """The standing dispatcher identity's UUID (operator-provisioned).

    Validated here because the plane's agent_spawn path forwards the proposer
    verbatim: a malformed value would surface downstream as a misleading
    ``governance_blocked`` on every dialectic. Malformed → None → CONFIG_ERROR.
    """
    raw = (os.environ.get("UNITARES_DIALECTIC_DISPATCHER_UUID") or "").strip()
    if not raw:
        return None
    if not _UUID_RE.match(raw):
        logger.error(
            "[DIALECTIC] UNITARES_DIALECTIC_DISPATCHER_UUID is not a UUID (%r); "
            "governed spawn disabled this call — fix the plist value",
            raw[:40],
        )
        return None
    return raw.lower()


def _lease_plane_url() -> str:
    return os.environ.get(
        "UNITARES_LEASE_PLANE_URL", "http://127.0.0.1:8788"
    ).rstrip("/")


def sanitize_spec_env(env: Dict[str, str]) -> Dict[str, str]:
    """Strip canonical-form-refused C0 controls from env values (see module doc)."""
    return {k: _REFUSED_C0_RE.sub("", v) if isinstance(v, str) else v for k, v in env.items()}


def build_envelope(
    session_id: str, spec: Dict[str, Any], proposer_uuid: str
) -> Optional[Dict[str, Any]]:
    """Build the /v1/effects execute envelope for a reviewer-spawn spec.

    Returns None when the payload cannot be canonicalized even after
    sanitization (callers treat that as REFUSED, not as a reason to bypass
    governance — see module doc).

    The idempotency key embeds the canonical payload hash so the key and the
    plane's digest can never disagree (the digest covers the whole payload,
    including inherited env like PYTHONPATH; a key derived from session+thesis
    alone would 409 after any env change).
    """
    from unitares_sdk.lease_plane.canonical import (  # noqa: PLC0415 — SDK on pythonpath
        CanonicalizationError,
        canonical_payload_sha256,
    )

    payload: Dict[str, Any] = {
        "cmd": spec.get("cmd"),
        "args": spec.get("args") or [],
        "env": sanitize_spec_env(spec.get("env") or {}),
    }
    if spec.get("cd"):
        payload["cd"] = spec["cd"]

    try:
        psha = canonical_payload_sha256(payload)
    except CanonicalizationError as exc:
        logger.warning(
            "[DIALECTIC] governed spawn payload not canonicalizable after "
            "sanitization (%s); degrading to in-process review",
            exc,
        )
        return None

    # `dialectic:/` is a real canonical lease-plane scheme naming the thing
    # actually under contention. The top-level effect surface is stored (and
    # grant-bound) verbatim, so use the canonical single-slash spelling.
    surface = f"dialectic:/{session_id}"
    idempotency_key = f"dialectic-reviewer:{session_id}:{psha[:16]}"

    proposer: Dict[str, str] = {"agent_uuid": proposer_uuid}

    token = _mint_dispatcher_token(proposer_uuid, session_id)
    if not token:
        # No continuity secret configured — the §7 gate at the veto would fail
        # closed and every dispatch would read as vetoed. Config problem.
        return {"__config_error__": "no continuity secret configured"}
    proposer["continuity_token"] = token

    grant = _mint_dispatcher_grant(
        proposer_uuid, psha, surface, idempotency_key
    )
    if grant:
        proposer["effect_grant"] = grant
    else:
        # Grantless is acceptable while the per-type binding flag is off; once
        # it flips, the veto fails closed on the missing grant (the safe
        # direction). Warn so the standing condition is visible.
        logger.warning(
            "[DIALECTIC] effect-grant mint returned none; sending grantless envelope"
        )

    return {
        "idempotency_key": idempotency_key,
        "custody_mode": "execute",
        "effect_type": "agent_spawn",
        "surface": surface,
        "payload": payload,
        "proposer": proposer,
        "provenance": {"session_id": session_id},
    }


def _mint_dispatcher_token(proposer_uuid: str, session_id: str) -> Optional[str]:
    """Mint the §7 proof in-process.

    The ``sid`` claim is deliberately a non-resolvable synthetic value
    (``dialectic-dispatcher:<session>``) — it must never be mistakable for a
    real client session. `recertify_strong_tier` verifies HMAC/exp/aid only;
    the sid is decoded and discarded there.
    """
    from src.mcp_handlers.identity.session import create_continuity_token  # noqa: PLC0415

    return create_continuity_token(
        proposer_uuid, f"dialectic-dispatcher:{session_id}"
    )


def _mint_dispatcher_grant(
    proposer_uuid: str, psha: str, surface: str, idempotency_key: str
) -> Optional[str]:
    from src.effect_grant import mint_effect_grant  # noqa: PLC0415

    return mint_effect_grant(
        aid=proposer_uuid,
        payload_sha256=psha,
        surface=surface,
        custody_mode="execute",
        idempotency_key=idempotency_key,
        ttl_seconds=_GRANT_TTL_SECONDS,
    )


def classify_response(status_code: int, body: Dict[str, Any]) -> GovernedSpawnResult:
    """Map a /v1/effects response onto the outcome buckets (pure; unit-tested)."""
    if status_code == 202:
        status = body.get("status")
        agent_id = body.get("agent_id")
        if status == "committed" and agent_id and not body.get("idempotent"):
            return GovernedSpawnResult(
                GovernedOutcome.COMMITTED,
                agent_id=str(agent_id),
                effect_id=body.get("effect_id"),
            )
        # Idempotent replay (a prior dispatch for this exact spec already ran)
        # or a 202 whose body does not carry a fresh committed spawn. The
        # prior reviewer is either running or dead; synthetic review resolves
        # the session now and resolution is idempotent either way.
        return GovernedSpawnResult(
            GovernedOutcome.REFUSED,
            detail=f"replay_or_uncommitted:{status}",
        )
    if status_code == 403:
        return GovernedSpawnResult(GovernedOutcome.REFUSED, detail="governance_blocked")
    if status_code == 409:
        return GovernedSpawnResult(GovernedOutcome.REFUSED, detail="idempotency_conflict")
    if status_code == 502:
        # spawn_failed — the plane's orchestrator POST errored. That leg has
        # its own timeout, so the spawn state is ambiguous; treat as refusal.
        return GovernedSpawnResult(GovernedOutcome.REFUSED, detail="spawn_failed")
    if status_code == 401:
        return GovernedSpawnResult(
            GovernedOutcome.CONFIG_ERROR, detail="lease plane bearer rejected"
        )
    if status_code == 422:
        # Envelope malformed = producer bug. Fall back to the direct path so a
        # code defect here cannot take orchestrated review down with it.
        logger.error(
            "[DIALECTIC] governed spawn envelope rejected 422: %s", str(body)[:300]
        )
        return GovernedSpawnResult(GovernedOutcome.UNAVAILABLE, detail="schema_invalid")
    # 501 execute-not-enabled, 503 pre-spawn plane errors, anything else the
    # plane refused before an orchestrator spawn could have happened.
    return GovernedSpawnResult(
        GovernedOutcome.UNAVAILABLE, detail=f"http_{status_code}"
    )


def classify_exception(exc: Exception) -> GovernedSpawnResult:
    """Transport-error mapping: refused-to-connect is availability; anything
    after the request may have been sent is ambiguous (irreversible effect →
    never retry on another path)."""
    try:
        import httpx  # noqa: PLC0415

        if isinstance(exc, (httpx.ConnectError, httpx.ConnectTimeout)):
            return GovernedSpawnResult(
                GovernedOutcome.UNAVAILABLE, detail=f"connect:{exc!r}"
            )
    except ImportError:  # pragma: no cover
        pass
    return GovernedSpawnResult(GovernedOutcome.REFUSED, detail=f"ambiguous:{exc!r}")


async def governed_dispatch(
    session_id: str,
    spec: Dict[str, Any],
    *,
    timeout: float = 5.0,
) -> GovernedSpawnResult:
    """Try to spawn the reviewer through the governed-effect surface.

    The 5s budget is deliberate: the governed leg sits in front of the direct
    path inside submit_thesis's 90s tool budget, so its worst case must stay
    small (see docs/proposals/governed-reviewer-spawn-v0.md §latency).
    """
    uuid = dispatcher_uuid()
    if not uuid:
        return GovernedSpawnResult(
            GovernedOutcome.CONFIG_ERROR, detail="dispatcher uuid unset or malformed"
        )
    bearer = os.environ.get("LEASE_PLANE_BEARER_TOKEN")
    if not bearer:
        return GovernedSpawnResult(
            GovernedOutcome.CONFIG_ERROR, detail="LEASE_PLANE_BEARER_TOKEN unset"
        )

    envelope = build_envelope(session_id, spec, uuid)
    if envelope is None:
        return GovernedSpawnResult(GovernedOutcome.REFUSED, detail="canonicalization")
    if "__config_error__" in envelope:
        return GovernedSpawnResult(
            GovernedOutcome.CONFIG_ERROR, detail=envelope["__config_error__"]
        )

    url = f"{_lease_plane_url()}/v1/effects"
    try:
        import httpx  # noqa: PLC0415

        async with httpx.AsyncClient(timeout=timeout) as client:
            resp = await client.post(
                url, json=envelope, headers={"Authorization": f"Bearer {bearer}"}
            )
        try:
            body = resp.json() or {}
        except ValueError:
            body = {}
        return classify_response(resp.status_code, body)
    except Exception as exc:  # noqa: BLE001 — mapped, never raised to the handler
        return classify_exception(exc)
