"""Governed-effect endpoints: /v1/effect-grant and /v1/effect-veto.

Split out of src/http_api.py (see that module for route registration).
"""

from __future__ import annotations

import os

from starlette.responses import JSONResponse


from src.logging_utils import get_logger

from src.http_routes import access

logger = get_logger(__name__)


async def http_effect_grant(request):
    """POST /v1/effect-grant — mint a single-use, content-bound effect grant.

    Phase 1 of effect-binding (governed-effect-effect-binding-v0 §4-B / #1075).
    A proposer calls this BEFORE submitting an effect to the lease plane: it
    presents its continuity_token plus the effect's content fields; gov-mcp
    re-certifies the token to ``strong`` (the §7 path) and, if strong, mints a
    ``gnt.v1`` grant bound to (aid, payload_sha256, surface, custody_mode,
    idempotency_key, nonce, exp). The proposer then carries the grant in the
    effect envelope's ``proposer`` object; the lease plane forwards it to
    ``/v1/effect-veto``, which verifies it covers THIS exact effect (a later
    slice).

    Closes T1 (retarget) + the grant-only slice of T2 (replay) — NOT T3 (a
    bearer+token holder can still mint grants for effects it authors). See the
    design's threat model.

    INERT by default: gated on ``UNITARES_GOVERNED_EFFECT_BINDING``; off → 501.
    Nothing forwards or verifies the grant yet (wiring is a later slice).

    Request:  ``{"proposer_agent_uuid", "proposer_continuity_token",
                 "payload_sha256", "surface", "custody_mode",
                 "idempotency_key", "ttl_seconds"?}``
    Response: ``{"ok": true, "grant": "gnt.v1...."}`` — the grant is issued TO
              the caller (like onboard's continuity_token); never logged.
    """
    http_api_token = os.getenv("UNITARES_HTTP_API_TOKEN")
    if not access._check_http_auth(request, http_api_token=http_api_token):
        return JSONResponse({"ok": False, "error": "unauthorized"}, status_code=401)

    # Inert unless explicitly enabled. Fail-closed: off → not implemented, so a
    # premature caller is refused rather than silently issued a grant nothing
    # verifies yet. Minting opens when the global flag OR any per-type flag is
    # set (#1252 item 2): during a staged rollout producers must be able to
    # mint before every type enforces.
    if not _binding_mint_enabled():
        return JSONResponse(
            {"ok": False, "error": "binding_not_enabled",
             "detail": "effect-binding grant minting is disabled (Phase 1, inert)"},
            status_code=501,
        )

    try:
        body = await request.json()
    except Exception:
        body = {}
    if not isinstance(body, dict):
        body = {}

    proposer = body.get("proposer_agent_uuid") or body.get("agent_id")
    fields = {
        "proposer_agent_uuid": proposer,
        "payload_sha256": body.get("payload_sha256"),
        "surface": body.get("surface"),
        "custody_mode": body.get("custody_mode"),
        "idempotency_key": body.get("idempotency_key"),
    }
    missing = [k for k, v in fields.items() if not (isinstance(v, str) and v)]
    if missing:
        return JSONResponse(
            {"ok": False, "error": "schema_invalid",
             "detail": f"required string fields missing: {', '.join(missing)}"},
            status_code=422,
        )

    # A grant is only minted for a proposer that re-certifies as a STRONG
    # identity — the same §7 gate the veto enforces (the grant asserts
    # aid=proposer). Fail-closed: no/invalid/expired/mismatched token → no
    # grant. The token is consumed for verification only, never logged.
    from src.mcp_handlers.identity.session import recertify_strong_tier
    if not recertify_strong_tier(body.get("proposer_continuity_token"), proposer):
        return JSONResponse(
            {"ok": False, "error": "tier_recert_failed",
             "detail": "proposer did not re-certify as a strong identity"},
            status_code=403,
        )

    # Optional caller TTL; the primitive clamps to its floor. Default applies if
    # absent or unparseable.
    mint_kwargs = {}
    ttl_raw = body.get("ttl_seconds")
    if ttl_raw is not None:
        try:
            mint_kwargs["ttl_seconds"] = int(ttl_raw)
        except (TypeError, ValueError):
            pass

    from src.effect_grant import mint_effect_grant
    grant = mint_effect_grant(
        aid=proposer,
        payload_sha256=fields["payload_sha256"],
        surface=fields["surface"],
        custody_mode=fields["custody_mode"],
        idempotency_key=fields["idempotency_key"],
        **mint_kwargs,
    )
    if not grant:
        # No HMAC secret configured — cannot mint. Fail closed.
        return JSONResponse(
            {"ok": False, "error": "grant_mint_unavailable",
             "detail": "no signing secret configured"},
            status_code=503,
        )

    return JSONResponse({"ok": True, "grant": grant})


_BINDING_FLAG = "UNITARES_GOVERNED_EFFECT_BINDING"


def _binding_enforced(effect_type) -> bool:
    """Is effect-binding enforced for this effect type? (#1252 item 2)

    The global ``UNITARES_GOVERNED_EFFECT_BINDING`` enforces every type at
    once — flipping it requires every producer to be minting simultaneously.
    Per-type flags (``UNITARES_GOVERNED_EFFECT_BINDING_FILE_WRITE``,
    ``.._AGENT_SPAWN``, derived generically from the forwarded effect_type)
    stage the rollout: enforce+prove file_write first while agent_spawn's
    ad-hoc producers stay unbound. The effect_type here is the one the lease
    plane forwards in the veto body (trusted loopback forwarder, always set
    by its validated envelope) — a missing type under per-type staging is
    simply not yet enforced; the global flag remains the blanket lockdown.
    """
    if access._http_bool(os.getenv(_BINDING_FLAG)):
        return True
    if not isinstance(effect_type, str) or not effect_type:
        return False
    suffix = "".join(c if c.isalnum() else "_" for c in effect_type).upper()
    return access._http_bool(os.getenv(f"{_BINDING_FLAG}_{suffix}"))


def _binding_mint_enabled() -> bool:
    """Grant minting opens when the global OR any per-type flag is set."""
    if access._http_bool(os.getenv(_BINDING_FLAG)):
        return True
    prefix = _BINDING_FLAG + "_"
    return any(k.startswith(prefix) and access._http_bool(v) for k, v in os.environ.items())


async def _check_effect_binding(body: dict, proposer: str):
    """§8 effect-binding: verify the forwarded grant covers THIS effect, then
    consume its nonce exactly once (#1075 Phase 1). Returns (binding_ok, reason).

    Called only when UNITARES_GOVERNED_EFFECT_BINDING is on. Fail-closed: a
    missing/invalid/expired/mismatched grant, a replayed nonce, or a store error
    → (False, reason). On a valid grant the nonce is consumed atomically
    (INSERT ... ON CONFLICT DO NOTHING in one statement — no SELECT-then-INSERT
    TOCTOU); a second presentation of the same grant finds the row present →
    replay → vetoed. The grant is a credential: verified transiently, never
    logged.
    """
    grant = body.get("proposer_effect_grant")
    if not grant or not isinstance(grant, str):
        return False, "binding_absent"
    from src.effect_grant import verify_effect_grant
    v = verify_effect_grant(
        grant,
        aid=proposer,
        payload_sha256=str(body.get("payload_sha256") or ""),
        surface=str(body.get("surface") or ""),
        custody_mode=str(body.get("custody_mode") or ""),
        idempotency_key=str(body.get("idempotency_key") or ""),
    )
    if not v.ok:
        return False, f"binding_{v.reason}"
    try:
        from src.db import get_db
        db = get_db()
        async with db.acquire() as conn:
            result = await conn.execute(
                """
                INSERT INTO effects.consumed_nonces (nonce, grant_exp)
                VALUES ($1, to_timestamp($2))
                ON CONFLICT (nonce) DO NOTHING
                """,
                v.nonce, int(v.exp),
            )
    except Exception as e:  # noqa: BLE001 — store failure → fail closed
        logger.warning("effect-binding nonce consume failed: %s", e)
        return False, "binding_store_unavailable"
    # asyncpg returns a command tag like "INSERT 0 1" (1 row) or "INSERT 0 0".
    if str(result).split()[-1] != "1":
        return False, "binding_replayed"
    return True, None


async def http_effect_veto(request):
    """POST /v1/effect-veto — governance veto for a proposed governed effect.

    The lease plane calls this BEFORE committing an ``execute`` agent_spawn
    (governed-effect-plane-v0 §6 + §7). Two independent gates compose; either
    trips the veto:

      §6 (verdict) — reads the proposer's DURABLE last-decided governance
      posture (``verdict``/``action`` from the latest
      ``core.agent_state.state_json``) and vetoes a flagged proposer:
      verdict ``high-risk`` OR a pause/block action (∉ {approve, guide}).

      §7 (tier) — re-certifies the proposer's IDENTITY tier as ``strong`` from
      a forwarded ``proposer_continuity_token`` (HMAC + expiry, aid==proposer),
      WITHOUT a fresh transport resolution. A proposer that does not
      re-certify as strong is vetoed. This gate applies on EVERY path,
      including the unknown-proposer branch §6 alone fails open.

    Policy:
      * VETO   §6 flagged  OR  §7 not strong (no/invalid/expired/mismatched token)
      * ALLOW  safe/caution + approve/guide  AND  strong-tier re-cert
      * UNKNOWN proposer (no governance state) → §6 fails OPEN, but §7 still
        applies: allowed only if the token re-certifies strong (a strong,
        never-flagged identity may spawn; a weak/unverified one may not).
      * DB error → 503 so the CALLER fails closed (can't confirm safety).

    The ``proposer_continuity_token`` is a credential: verified transiently,
    never read into the DB query, the response body, or any log line
    (Invariant 7).

    Request:  ``{"proposer_agent_uuid": "...", "proposer_continuity_token": "v1...",
                 "surface": "...", "effect_type": "..."}``
    Response: ``{"ok": true, "vetoed": bool, "verdict", "action",
                 "risk_score", "tier", "tier_ok", "reason"}``
    """
    http_api_token = os.getenv("UNITARES_HTTP_API_TOKEN")
    if not access._check_http_auth(request, http_api_token=http_api_token):
        return JSONResponse({"ok": False, "error": "unauthorized"}, status_code=401)

    try:
        body = await request.json()
    except Exception:
        body = {}
    if not isinstance(body, dict):
        body = {}
    proposer = body.get("proposer_agent_uuid") or body.get("agent_id")
    if not proposer or not isinstance(proposer, str):
        return JSONResponse(
            {"ok": False, "error": "schema_invalid",
             "detail": "proposer_agent_uuid required"},
            status_code=422,
        )

    # §7 strong-tier re-certification (governed-effect-plane-v0 §2/§7). The
    # proposer forwards its continuity_token (transport-robust HMAC proof); we
    # re-verify it server-side to the `strong` tier WITHOUT a fresh transport
    # resolution (that would stamp server_inferred → weak — the trap that would
    # block every effect). This gate DOMINATES every allow path below: a
    # proposer that does not re-certify as strong is vetoed regardless of its
    # governance verdict — including the unknown-proposer branch that §6 alone
    # fails OPEN. Fail-closed: a missing, malformed, expired, or aid-mismatched
    # token → tier_ok False → vetoed. The token is consumed for verification
    # only — never read into the DB query, the response, or any log line.
    from src.mcp_handlers.identity.session import recertify_strong_tier
    tier_ok = recertify_strong_tier(body.get("proposer_continuity_token"), proposer)
    tier = "strong" if tier_ok else "unverified"
    tier_reason = None if tier_ok else "tier_recert_failed:not_strong_identity"

    # §8 effect-binding (governed-effect-effect-binding-v0 §5 / #1075). ADDITIVE,
    # flag-gated: with no binding flag set (default), this is a no-op
    # (binding_ok=True) and the live §6/§7 veto is byte-identical. Enforcement
    # is per effect type (#1252 item 2): the global flag covers every type,
    # per-type flags (e.g. UNITARES_GOVERNED_EFFECT_BINDING_FILE_WRITE) stage
    # the rollout one type at a time. When enforced, the forwarded grant must
    # cover THIS exact effect and its nonce must be unconsumed — otherwise
    # binding_ok is False and the effect is vetoed. Like §7, this gate applies
    # on EVERY exit path below.
    binding_ok, binding_reason = True, None
    if _binding_enforced(body.get("effect_type")):
        binding_ok, binding_reason = await _check_effect_binding(body, proposer)

    try:
        from src.db import get_db
        db = get_db()
        async with db.acquire() as conn:
            row = await conn.fetchrow(
                """
                SELECT s.state_json->>'verdict' AS verdict,
                       s.state_json->>'action'  AS action,
                       s.risk_score             AS risk_score
                FROM core.identities i
                JOIN core.agent_state s ON s.identity_id = i.identity_id
                WHERE i.agent_id = $1 AND s.synthetic = false
                ORDER BY s.recorded_at DESC
                LIMIT 1
                """,
                proposer,
            )
    except Exception as e:  # noqa: BLE001 — governance read failed; let caller fail closed
        logger.warning("effect-veto governance read failed for %s: %s", proposer, e)
        return JSONResponse(
            {"ok": False, "error": "governance_unavailable",
             "detail": "could not read proposer governance state"},
            status_code=503,
        )

    if row is None:
        # Unknown proposer. §6 (verdict) fails OPEN for newcomers — the two
        # bearers already gate access and the veto's job is to stop a KNOWN-
        # flagged agent. But §7 (tier) still applies: a newcomer must STILL
        # re-certify as a strong identity to commit an RCE-class spawn. So an
        # unknown proposer is allowed only when its token re-certifies strong;
        # no/invalid token → vetoed. This is the intended composition —
        # "a strong, never-flagged identity may spawn; a weak/unverified one
        # may not" — not an emergent property of the old early return.
        unknown_reasons = [r for r in (tier_reason, binding_reason) if r]
        return JSONResponse({
            "ok": True,
            "vetoed": not (tier_ok and binding_ok),
            "verdict": None, "action": None, "risk_score": None,
            "tier": tier, "tier_ok": tier_ok,
            "binding_ok": binding_ok,
            "reason": (" ; ".join(unknown_reasons) if unknown_reasons else "no_governance_state"),
        })

    verdict = row["verdict"]
    action = row["action"]
    risk_score = row["risk_score"]
    block_verdict = verdict == "high-risk"
    block_action = action is not None and action not in ("approve", "guide")
    # §6 verdict/action OR §7 tier OR §8 effect-binding — any trips the veto.
    vetoed = bool(block_verdict or block_action or (not tier_ok) or (not binding_ok))

    if vetoed:
        reasons = []
        if block_verdict or block_action:
            reasons.append(f"verdict={verdict} action={action}")
        if not tier_ok:
            reasons.append(tier_reason)
        if not binding_ok:
            reasons.append(binding_reason)
        reason = " ; ".join(r for r in reasons if r)
    else:
        reason = None

    return JSONResponse({
        "ok": True,
        "vetoed": vetoed,
        "verdict": verdict,
        "action": action,
        "risk_score": risk_score,
        "tier": tier,
        "tier_ok": tier_ok,
        "binding_ok": binding_ok,
        "reason": reason,
    })
