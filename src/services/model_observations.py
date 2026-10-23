"""Read-only model attribution for responses, separate from caller identity.

The latest recorded observation is not a claim about the current inference.
Neither labels nor request arguments are used to fill gaps in durable evidence.
"""
from __future__ import annotations

import asyncio
from collections import OrderedDict
from copy import deepcopy
from datetime import datetime, timezone
import json
import time
from uuid import UUID

from mcp.types import TextContent

from src.model_harness_provenance import normalize_persisted_runtime_provenance


LOOKUP_TIMEOUT_SECONDS = 0.25
CACHE_SECONDS = 15.0
CACHE_LIMIT = 256
CANDIDATE_LIMIT = 32
_cache: OrderedDict[str, tuple[float, dict | None]] = OrderedDict()


async def read_model_observation(agent_uuid: str, *, db=None) -> dict | None:
    """Latest usable versioned observation among bounded model-bearing rows."""
    # Never reinterpret a cosmetic handle as an identity key.
    agent_uuid = str(UUID(agent_uuid))
    if db is None:
        from src.db import get_db
        db = get_db()
    from src.runtime_observations import VALID_KINDS, RUNTIME_EVENT_PREFIX

    # Each component reads only its own tables. Filter model-less rows before
    # bounding candidates, then combine sources by observation (not arrival) time.
    observed_before = datetime.now(timezone.utc)
    async with asyncio.TaskGroup() as reads:
        audit_read = reads.create_task(db.read_audit_model_observation_candidates(
            agent_uuid,
            event_types=sorted(RUNTIME_EVENT_PREFIX + kind for kind in VALID_KINDS),
            observed_before=observed_before, limit=CANDIDATE_LIMIT,
        ))
        state_read = reads.create_task(db.read_state_model_observation_candidates(
            agent_uuid, observed_before=observed_before, limit=CANDIDATE_LIMIT,
        ))
    rows = sorted(
        [*audit_read.result(), *state_read.result()],
        key=lambda row: (row["observed_at"], row["record_kind"], row["record_id"]),
        reverse=True,
    )
    for row in rows:
        raw = row["provenance"]
        if isinstance(raw, str):
            try:
                raw = json.loads(raw)
            except (TypeError, ValueError):
                continue
        provenance = normalize_persisted_runtime_provenance(raw)
        model = provenance["model"]
        if not model["identifier"]:
            continue
        return {
            "agent_uuid": agent_uuid,
            "model": model,
            "harness": provenance["harness"],
            "observed_at": row["observed_at"].isoformat(),
            "record": {"kind": row["record_kind"], "id": row["record_id"]},
            "scope": "latest_recorded_observation",
        }
    return None


async def _cached_observation(agent_uuid: str) -> dict | None:
    now = time.monotonic()
    cached = _cache.get(agent_uuid)
    if cached is not None and now - cached[0] < CACHE_SECONDS:
        _cache.move_to_end(agent_uuid)
        return deepcopy(cached[1])
    # Includes pool acquisition. An unavailable optional read must not hold up
    # the successful tool indefinitely. Failed reads are not cached as facts.
    result = await asyncio.wait_for(
        read_model_observation(agent_uuid), timeout=LOOKUP_TIMEOUT_SECONDS
    )
    _cache[agent_uuid] = (time.monotonic(), deepcopy(result))
    _cache.move_to_end(agent_uuid)
    while len(_cache) > CACHE_LIMIT:
        _cache.popitem(last=False)
    return result


async def attach_model_observation(result):
    """Add caller attribution after response shaping, without changing identity."""
    from src.mcp_handlers.context import get_context_resolved_agent_id, get_session_proof_origin
    from src.mcp_handlers.identity_bootstrap import identity_refusal_status
    from src.logging_utils import get_logger

    if get_session_proof_origin() != "caller_asserted":
        return result
    caller_uuid = get_context_resolved_agent_id()
    if not caller_uuid or not (
        isinstance(result, (list, tuple)) and result and hasattr(result[0], "text")
    ):
        return result
    try:
        payload = json.loads(result[0].text)
        if (
            not isinstance(payload, dict) or payload.get("success") is False
            or "error" in payload or payload.get("refused")
            or payload.get("status") == "identity_required"
            or identity_refusal_status(payload)
        ):
            return result
        # use_tool may wrap a response that was already enriched. Preserve its
        # original observation rather than changing attribution on the way out.
        if "caller_model_observation" in payload:
            return result
        observation = await _cached_observation(caller_uuid)
        if observation is None:
            return result
        # A cross-agent read's agent_uuid can name its subject. This field
        # always names the caller and carries its own UUID to avoid ambiguity.
        payload["caller_model_observation"] = observation
        size = payload.get("_response_size")
        if isinstance(size, dict):
            measured = len(json.dumps(
                {k: v for k, v in payload.items() if k != "_response_size"},
                ensure_ascii=False,
            ).encode())
            size.update(
                approx_bytes=measured, approx_kb=round(measured / 1000, 1),
                size_class="small" if measured < 4000 else "medium" if measured < 12000 else "large",
            )
        return [TextContent(type="text", text=json.dumps(payload, ensure_ascii=False)), *result[1:]]
    except Exception:
        get_logger(__name__).debug("Model observation unavailable", exc_info=True)
        return result
