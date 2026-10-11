"""Dispatch an independent dialectic reviewer through the agent-orchestrator.

This is the orchestrator's first live consumer (Decision A of the 2026-06-24
Wave-3 gate). Design (b), escalation tier: the in-process synthetic reviewer
stays the default and the fallback — orchestrated dispatch is opt-in via
``UNITARES_DIALECTIC_ORCHESTRATED_REVIEW`` and ANY failure here returns None so
``handle_submit_thesis`` degrades to the in-process path. The orchestrator being
down therefore never breaks dialectic; it is an enhancement (a governed,
own-identity, lease-capable reviewer process), not a dependency.

The spawned reviewer (``python3 -m agents.dialectic_reviewer``) onboards with its
OWN identity, claims the still-open reviewer slot via the multi-agent path
(submit_antithesis), and submits its verdict. After a disagreement it remains
alive for a bounded response/reconsideration window under that same identity,
then exits — so on successful dispatch the handler must NOT also run the
in-process review.
"""
from __future__ import annotations

import hashlib
import json
import math
import os
import sys
from pathlib import Path
from typing import Any, Dict, Optional

from src.local_inference_env import (
    MODEL_BASE_URL_ENV,
    MODEL_ENV,
    default_local_model,
    model_base_url,
)
from src.logging_utils import get_logger

logger = get_logger(__name__)

# Classifier inputs the reviewer child must see exactly as this server does
# (literal names, listed again in reviewer_config for the flag catalog).
_CLASSIFIER_SETTINGS = (
    "UNITARES_MODEL_LOCAL_HOSTS",
    "UNITARES_MODEL_PRIVACY",
    "UNITARES_TRUSTED_NETWORKS",
)

# Settings that decide which reviewer hosts the child may call and where the
# external one points: forwarded even when empty, so the daemon's own values
# never stand in for this server's (literal names, also in reviewer_config).
_HOST_SELECTION_SETTINGS = (
    "UNITARES_DIALECTIC_REVIEWER_HOSTS",
    "UNITARES_DIALECTIC_REVIEWER_HOST",
    "UNITARES_DIALECTIC_EXTERNAL_BASE_URL",
)

# Repo root: src/mcp_handlers/dialectic/orchestrator_dispatch.py -> repo
_REPO_ROOT = Path(__file__).resolve().parents[3]

# Must match agents.dialectic_reviewer.reviewer.DEFAULT_CONTINUATION_WAIT_S.
# A contract test pins the two values together without importing the runner into
# the governance server. The extra grace covers the initial model call (host
# backends allow up to seven minutes), onboarding, and protocol writes before
# the continuation clock starts.
DEFAULT_CONTINUATION_WAIT_S = 3600.0
# Must match agents.dialectic_reviewer.reviewer.CONTINUATION_TOTAL_WAIT_FACTOR:
# the reviewer restarts its wait after each filed synthesis, so its lifetime is
# up to this many waits and the reaper cap must outlast it.
CONTINUATION_TOTAL_WAIT_FACTOR = 3
REVIEWER_RUNTIME_GRACE_S = 900.0


def orchestrated_review_enabled() -> bool:
    """Opt-in gate for routing reviews through the orchestrator (default OFF).

    OFF preserves today's behaviour exactly (in-process synthetic reviewer). ON
    makes the orchestrator the first-choice reviewer with in-process as fallback.
    """
    return os.environ.get(
        "UNITARES_DIALECTIC_ORCHESTRATED_REVIEW", "0"
    ).strip().lower() in ("1", "true", "yes", "on")


def _orchestrator_url() -> str:
    return os.environ.get("AGENT_ORCHESTRATOR_URL", "http://127.0.0.1:8789").rstrip("/")


def _governance_url() -> str:
    # The reviewer passes this straight to GovernanceClient(mcp_url=...), whose
    # streamable-http transport needs the /mcp/ path — a bare base URL makes
    # session.initialize() hang then cancel (live-found 2026-06-23). Normalize a
    # pathless URL so either form (base or full mcp_url) works.
    raw = (
        os.environ.get("UNITARES_GOVERNANCE_URL")
        or os.environ.get("GOVERNANCE_URL")
        or "http://127.0.0.1:8767/mcp/"
    )
    from urllib.parse import urlparse

    if urlparse(raw).path in ("", "/"):
        raw = raw.rstrip("/") + "/mcp/"
    return raw


def _reviewer_max_runtime_ms() -> int:
    """Lifetime cap for one spawned reviewer.

    The agent-orchestrator defaults to thirty minutes, which is shorter than
    the dialectic protocol's one-hour synthesis window. Size this spawn's cap
    from the same operator override the reviewer consumes, plus bounded setup
    and model-call grace. Invalid/negative overrides fail back to the default
    window rather than disabling the orchestrator's reaper.
    """
    raw = os.environ.get(
        "UNITARES_DIALECTIC_CONTINUATION_WAIT_S",
        "3600",
    )
    wait_s = _seconds(raw, DEFAULT_CONTINUATION_WAIT_S)
    if wait_s < 0:
        wait_s = DEFAULT_CONTINUATION_WAIT_S
    total_s = wait_s * CONTINUATION_TOTAL_WAIT_FACTOR
    return max(1, int((total_s + REVIEWER_RUNTIME_GRACE_S + _extra_host_budget_s()) * 1000))


# The largest seconds override honoured; anything above it counts as unset.
# Seven days is far past any real reviewer timeout or continuation window, and
# keeps the summed cap (three hosts plus the wait, in milliseconds) finite.
MAX_SECONDS_OVERRIDE = 7 * 24 * 3600.0


def _seconds(raw: Any, default: float) -> float:
    """A seconds override, or ``default`` when it is not a usable number.

    ``float()`` accepts ``inf``, ``nan`` and ``1e309`` (which overflows to
    infinity), and a finite ``1e308`` still overflows once the cap is summed
    and scaled to milliseconds; ``int()`` of that cap raises. That raise
    happens in ``_build_spec``, before the dispatcher's exception handler, so
    it would escape instead of degrading to the in-process reviewer.
    """
    return float(raw) if _is_usable_seconds(raw) else default


def _is_usable_seconds(raw: Any) -> bool:
    """Finite and no larger than MAX_SECONDS_OVERRIDE (negatives pass: each
    reader gives a negative its own meaning)."""
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return False
    return math.isfinite(value) and value <= MAX_SECONDS_OVERRIDE


# Per-call timeout settings and defaults of the hosts a reviewer list may name,
# by every name the list accepts. Must match agents.dialectic_reviewer.host_list
# (HOSTS and its aliases); a contract test pins the two together without
# importing the runner into the governance server.
_HOST_TIMEOUTS = {
    "codex": ("UNITARES_DIALECTIC_CODEX_TIMEOUT_S", 420.0),
    "claude": ("UNITARES_DIALECTIC_CLAUDE_TIMEOUT_S", 420.0),
    "antigravity": ("UNITARES_DIALECTIC_ANTIGRAVITY_TIMEOUT_S", 420.0),
    "external": ("UNITARES_DIALECTIC_EXTERNAL_TIMEOUT_S", 180.0),
}
_HOST_TIMEOUT_ALIASES = {
    "codex:host-adapter": "codex",
    "claude:host-adapter": "claude",
    "agy": "antigravity",
    "antigravity:host-adapter": "antigravity",
    "openai_compat": "external",
    "openai-compatible": "external",
    "gemini": "external",
}


def _extra_host_budget_s() -> float:
    """Time for the listed hosts after the first.

    REVIEWER_RUNTIME_GRACE_S covers one model call. When the first listed host
    fails, each later one may take its full timeout before the verdict, so the
    lifetime cap grows by those timeouts (design: docs/proposals/active/
    dialectic-reviewer-hosts-v0.md 2.7). Unknown names add nothing: the
    reviewer refuses such a list and calls no host.
    """
    raw = os.environ.get("UNITARES_DIALECTIC_REVIEWER_HOSTS", "")
    names = [part.strip().lower() for part in raw.split(",") if part.strip()]
    extra = 0.0
    for name in names[1:3]:
        key = _HOST_TIMEOUT_ALIASES.get(name, name)
        if key not in _HOST_TIMEOUTS:
            continue
        env_name, default = _HOST_TIMEOUTS[key]
        extra += max(0.0, _seconds(os.environ.get(env_name, default), default))
    return extra


def _build_spec(session_id: str, thesis: Dict[str, Any], parent_agent_id: Optional[str]) -> Dict[str, Any]:
    """Translate the thesis into an orchestrator POST /v1/agents spec.

    The reviewer reads everything from env (see Thesis.from_env + reviewer.main).
    PYTHONPATH must include the repo root and the SDK src so the spawned process
    can import ``agents.dialectic_reviewer`` and ``unitares_sdk``.
    """
    conditions = thesis.get("proposed_conditions") or []
    if not isinstance(conditions, list):
        conditions = [str(conditions)]

    pythonpath = os.pathsep.join(
        [str(_REPO_ROOT), str(_REPO_ROOT / "agents" / "sdk" / "src")]
    )
    existing_pp = os.environ.get("PYTHONPATH")
    if existing_pp:
        pythonpath = pythonpath + os.pathsep + existing_pp

    env: Dict[str, str] = {
        "DIALECTIC_SESSION_ID": session_id,
        "DIALECTIC_THESIS_ROOT_CAUSE": thesis.get("root_cause") or "",
        "DIALECTIC_THESIS_CONDITIONS": json.dumps(conditions),
        "DIALECTIC_THESIS_REASONING": thesis.get("reasoning") or "",
        "DIALECTIC_THESIS_SITUATION": thesis.get("situation") or "",
        "DIALECTIC_PAUSED_AGENT_STATE": json.dumps(
            thesis.get("paused_agent_state") or {}, sort_keys=True, default=str
        ),
        "UNITARES_GOVERNANCE_URL": _governance_url(),
        "PYTHONPATH": pythonpath,
    }
    if parent_agent_id:
        env["UNITARES_PARENT_AGENT_ID"] = parent_agent_id

    # The backend choice belongs to the dispatching governance process, not to
    # whichever defaults happen to be present in the orchestrator daemon. Pass
    # only the bounded reviewer configuration — never auth tokens. The Claude
    # CLI itself inherits operator subscription auth from the child runtime.
    reviewer_config = (
        # The ordered host list (design: dialectic-reviewer-hosts-v0); the
        # single-host name is read as a one-item list when it is unset.
        "UNITARES_DIALECTIC_REVIEWER_HOSTS",
        "UNITARES_DIALECTIC_REVIEWER_HOST",
        "UNITARES_DIALECTIC_CLAUDE_MODEL",
        "UNITARES_DIALECTIC_CLAUDE_TIMEOUT_S",
        "UNITARES_DIALECTIC_CODEX_TIMEOUT_S",
        # External (OpenAI-compatible) host: endpoint, model, and the NAME of
        # the key variable. Without these a HOST=external/gemini selection
        # reached the child alone, reported "not configured", and fell back to
        # the local model. The key VALUE is deliberately not forwarded: this
        # env becomes the governed-spawn payload sent to the lease plane, and
        # a vendor credential does not belong in an effect record. Provision
        # the key in the ORCHESTRATOR's environment (the child inherits it);
        # setting it only here yields an auth failure, which the reviewer
        # records as a fallback warning in its provenance.
        "UNITARES_DIALECTIC_EXTERNAL_BASE_URL",
        "UNITARES_DIALECTIC_EXTERNAL_MODEL",
        "UNITARES_DIALECTIC_EXTERNAL_API_KEY_ENV",
        "UNITARES_DIALECTIC_EXTERNAL_TIMEOUT_S",
        "UNITARES_DIALECTIC_EXTERNAL_MAX_TOKENS",
        "UNITARES_DIALECTIC_REVIEW_MAX_TOKENS",
        "UNITARES_DIALECTIC_CONTINUATION_WAIT_S",
        "UNITARES_DIALECTIC_CONTINUATION_POLL_S",
        "UNITARES_CLAUDE_CLI",
        "UNITARES_CODEX_CLI",
        # Antigravity (agy): subscription auth from the child runtime, like
        # claude/codex; only the CLI path and timeout are configuration.
        "UNITARES_ANTIGRAVITY_CLI",
        "UNITARES_DIALECTIC_ANTIGRAVITY_TIMEOUT_S",
        # The local model endpoint and model are forwarded below as this
        # server's RESOLVED values. The classifier settings pass through as
        # given, so the child classifies the endpoint (local or external) the
        # way this server does; UNITARES_TRUSTED_NETWORKS is one of its inputs.
        # Literal names (not the resolver's constants) so scripts/dev/
        # flag_catalog.py can read this tuple; the settings-routing test in
        # tests/test_local_inference_env.py keeps it in step with the list.
        "UNITARES_MODEL_LOCAL_HOSTS",
        "UNITARES_MODEL_PRIVACY",
        "UNITARES_TRUSTED_NETWORKS",
        # The reviewer talks to gov-mcp through GovernanceClient. If that /mcp
        # gate is configured, the child needs the bearer or every call it makes
        # 401s — and the failure would look like a broken reviewer rather than
        # a missing credential. Forwarded, not minted: this process does not
        # decide the token, it only passes on what it was given.
        "UNITARES_MCP_BEARER_TOKEN",
    )
    for name in reviewer_config:
        value = os.environ.get(name)
        if value:
            env[name] = value
    # A seconds value that is not usable (_is_usable_seconds) reaches the child
    # as its default, not as given: the reviewer passes its host timeout to
    # asyncio.wait_for, so `inf` would let a hung host hold it until the reaper,
    # never reaching the next host. Replaced rather than dropped, since the
    # child also inherits the orchestrator daemon's own environment.
    seconds_settings = dict(_HOST_TIMEOUTS.values())
    seconds_settings["UNITARES_DIALECTIC_CONTINUATION_WAIT_S"] = DEFAULT_CONTINUATION_WAIT_S
    for name, default in seconds_settings.items():
        if name in env and not _is_usable_seconds(env[name]):
            env[name] = f"{default:g}"

    # Host selection is forwarded even when empty, like the classifier
    # settings below: the orchestrator merges this env over its own, so an
    # omitted list would let the child keep the daemon's (say HOSTS=claude)
    # and approve through a host this server never selected. Empty reads as
    # unset in host_list.
    for name in _HOST_SELECTION_SETTINGS:
        env[name] = os.environ.get(name, "")

    # The classifier inputs are forwarded even when empty. The orchestrator
    # merges this env OVER its own inherited environment, so an omitted key
    # would let the child keep a stale value (say UNITARES_MODEL_PRIVACY=local)
    # and classify the endpoint differently from this server. An empty value
    # reads as unset in local_inference_env, which is what this server used.
    for name in _CLASSIFIER_SETTINGS:
        env[name] = os.environ.get(name, "")

    # Local model endpoint and model: always forward what THIS server
    # resolved (src/local_inference_env.py), defaults included, under the new
    # names. The child runs this release, so the new names outrank any older
    # or stale value it would otherwise inherit from the orchestrator daemon's
    # environment. Forwarding only explicit settings let a server on defaults
    # spawn a reviewer that used the orchestrator's own endpoint, which could
    # classify local and receive a thesis the server never classified.
    env[MODEL_BASE_URL_ENV] = model_base_url()
    env[MODEL_ENV] = default_local_model()

    # NB: we deliberately do NOT forward UNITARES_DIALECTIC_BEAM_RESOLUTION into
    # the reviewer's env. The reviewer submits its antithesis/synthesis via the
    # gov-mcp `dialectic` tool (client.call_tool), so those writes EXECUTE IN
    # gov-mcp where the flag already applies — the reviewer process never does its
    # own session-row writes. (Reverts #1185, which forwarded the flag on a wrong
    # premise; it was a dead no-op.)

    return {
        "cmd": sys.executable,  # the MCP process's own interpreter has the deps
        "args": ["-m", "agents.dialectic_reviewer"],
        "cd": str(_REPO_ROOT),
        "env": env,
        # Per-spawn override: the orchestrator's 30-minute default would reap a
        # healthy reviewer halfway through the protocol's synthesis window.
        "max_runtime_ms": _reviewer_max_runtime_ms(),
    }


def _direct_idempotency_key(session_id: str, spec: Dict[str, Any]) -> str:
    """Stable key for response-loss retries of one material reviewer spawn."""
    canonical = json.dumps(
        spec,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    material = session_id.encode("utf-8") + b"\x00" + canonical
    digest = hashlib.sha256(material).hexdigest()
    return f"dialectic-reviewer:{digest}"


async def dispatch_orchestrated_review(
    session_id: str,
    thesis: Dict[str, Any],
    parent_agent_id: Optional[str],
    *,
    timeout: float = 10.0,
) -> Optional[Dict[str, Any]]:
    """POST a reviewer-spawn spec to the orchestrator. Returns its JSON (the spawned
    execution id/status) on success, or None on ANY failure (caller falls back to the
    in-process synthetic reviewer)."""
    bearer = os.environ.get("AGENT_ORCHESTRATOR_BEARER_TOKEN")
    if not bearer:
        logger.warning(
            "[DIALECTIC] orchestrated review enabled but AGENT_ORCHESTRATOR_BEARER_TOKEN "
            "unset; falling back to in-process synthetic reviewer"
        )
        return None

    spec = _build_spec(session_id, thesis, parent_agent_id)

    # Governed-first routing (opt-in): try the lease plane's governed-effect
    # surface before the direct orchestrator POST. The outcome buckets are
    # load-bearing — a governance refusal or an ambiguous outcome must degrade
    # to the in-process synthetic reviewer (return None) and must NOT fall
    # through to a direct spawn; only availability/config conditions may.
    # Rationale per bucket: src/mcp_handlers/dialectic/governed_spawn.py.
    from src.mcp_handlers.dialectic.governed_spawn import (
        GovernedOutcome,
        governed_dispatch,
        governed_spawn_enabled,
    )

    if governed_spawn_enabled():
        governed = await governed_dispatch(session_id, spec)
        if governed.outcome is GovernedOutcome.COMMITTED:
            execution_id = governed.execution_id or governed.agent_id
            logger.info(
                "[DIALECTIC] governed reviewer spawned for session %s: execution_id=%s "
                "effect_id=%s",
                session_id[:16], execution_id, governed.effect_id,
            )
            return {
                "ok": True,
                "execution_id": execution_id,
                "agent_id": execution_id,
                "effect_id": governed.effect_id,
                "governed": True,
            }
        if governed.outcome is GovernedOutcome.REFUSED:
            logger.info(
                "[DIALECTIC] governed spawn refused (%s); degrading to in-process "
                "synthetic reviewer (no direct-spawn fallback for this bucket)",
                governed.detail,
            )
            return None
        logger.warning(
            "[DIALECTIC] governed spawn %s (%s); falling back to direct orchestrator",
            governed.outcome.value, governed.detail,
        )

    url = f"{_orchestrator_url()}/v1/agents"
    idempotency_key = _direct_idempotency_key(session_id, spec)
    try:
        import httpx

        async with httpx.AsyncClient(timeout=timeout) as client:
            resp = await client.post(
                url,
                json=spec,
                headers={
                    "Authorization": f"Bearer {bearer}",
                    "Idempotency-Key": idempotency_key,
                },
            )
        if resp.status_code not in (200, 201, 202):
            logger.warning(
                "[DIALECTIC] orchestrator spawn returned %s: %s; falling back",
                resp.status_code, resp.text[:300],
            )
            return None
        data = resp.json()
        execution_id = data.get("execution_id") or data.get("agent_id") or data.get("id")
        if not execution_id:
            logger.warning(
                "[DIALECTIC] orchestrator acknowledged spawn without an execution id; "
                "falling back"
            )
            return None
        data["execution_id"] = execution_id
        data.setdefault("agent_id", execution_id)
        logger.info(
            "[DIALECTIC] orchestrated reviewer spawned for session %s: %s",
            session_id[:16], execution_id,
        )
        return data
    except Exception as exc:  # noqa: BLE001 — any failure degrades to in-process
        logger.warning(
            "[DIALECTIC] orchestrated dispatch failed (%r); falling back to in-process", exc
        )
        return None


async def reviewer_crashed_fast(
    execution_id: str,
    *,
    await_seconds: float = 15.0,
) -> bool:
    """Briefly await the spawned reviewer to catch a FAST crash.

    The common reviewer failure modes (bad URL, import error, wrong tool name)
    exit within ~12s — well before gemma4 (~40-70s) could finish. We block on the
    orchestrator's await endpoint for a short window:

    - exited with non-zero status  → True  (crashed without claiming the slot;
      the caller should fall back to the in-process synthetic reviewer inline so
      the session resolves now instead of stranding at antithesis for the 4h reap)
    - exited 0 / still running (504) / any error → False (the reviewer owns the
      review; leave it on the async path — DO NOT also run in-process)

    The short window keeps the whole submit_thesis call inside its handler
    timeout (derived from the synthetic-review budget in handlers.py, #1442)
    even when a fast crash triggers the in-process fallback.
    A reviewer that crashes AFTER this window (mid-model, rare) still relies on
    the slower reap — acceptable; this closes the common case.
    """
    if not execution_id:
        return False
    bearer = os.environ.get("AGENT_ORCHESTRATOR_BEARER_TOKEN")
    if not bearer:
        return False
    url = f"{_orchestrator_url()}/v1/executions/{execution_id}/await"
    try:
        import httpx

        async with httpx.AsyncClient(timeout=await_seconds + 5.0) as client:
            resp = await client.post(
                url,
                json={"timeout_ms": int(await_seconds * 1000)},
                headers={"Authorization": f"Bearer {bearer}"},
            )
        if resp.status_code == 504:
            return False  # await_timeout — still running, reviewer owns it
        if resp.status_code != 200:
            return False  # not_found / unexpected — don't double-run
        result = (resp.json() or {}).get("result") or {}
        exit_status = result.get("exit_status")
        crashed = exit_status not in (0, None)
        if crashed:
            logger.warning(
                "[DIALECTIC] orchestrated reviewer %s exited %s without resolving; "
                "falling back to in-process", execution_id, exit_status,
            )
        return bool(crashed)
    except Exception as exc:  # noqa: BLE001 — can't tell ⇒ leave it to the reviewer
        logger.warning("[DIALECTIC] reviewer await check failed (%r); leaving async", exc)
        return False
