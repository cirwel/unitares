"""Orchestrated dialectic reviewer — a standalone, independently-accountable
reviewer process.

The agent-orchestrator spawns this as a supervised, lease-bound child when a
dialectic session needs a reviewer. Unlike the in-process synthetic path
(`handle_llm_assisted_dialectic`, which hardcodes ``agrees=True`` and borrows the
paused agent's api_key), this process:

  * onboards as its OWN governance identity (strict-identity compliant),
  * runs an operator-selected heterogeneous model (local Ollama by default,
    subscription-auth Codex or Claude when configured) IN its own process to
    form a *genuine* verdict that may DISAGREE,
  * submits that verdict through the ordinary dialectic protocol tools, and
  * after a disagreement, stays alive for a bounded window to evaluate the
    paused agent's response under the SAME reviewer identity before exiting.

Design: docs/proposals/orchestrated-dialectic-reviewer-v0.md

The verdict-derivation (`parse_reviewer_verdict`) and prompt-construction
(`build_review_prompt`) are PURE functions so the independence-critical behavior
— that a disagreeing model produces ``agrees=False`` — is unit-tested without a
network or a model.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import time
from dataclasses import dataclass, field
from typing import Any, Optional

from src.identity.lineage_semantics import LineageSpawnReason

from .host_backends import (
    HostReviewResult,
    call_claude_backend,
    call_openai_compat_backend,
    resolve_host_cli,
)

# gemma4 hides its answer behind a <think> block under thinking mode; strip it
# before JSON extraction (mirrors llm_delegation._wants_reasoning_effort_none).
_THINK_BLOCK = re.compile(r"<think>.*?</think>", re.DOTALL | re.IGNORECASE)
# The model is asked for strict JSON, but local models fence it or add prose.
_JSON_OBJECT = re.compile(r"\{.*\}", re.DOTALL)

DEFAULT_MODEL = os.getenv("UNITARES_LLM_MODEL", "gemma4:latest")
OLLAMA_BASE_URL = os.getenv("UNITARES_OLLAMA_BASE_URL", "http://localhost:11434/v1")
SPAWN_REASON = LineageSpawnReason.DIALECTIC_REVIEWER.value
REVIEWER_NAME = "DialecticReviewer"
# Keep a rejecting reviewer available for the protocol's full synthesis-response
# window. DialecticSession.MAX_SYNTHESIS_WAIT is one hour; the old ten-minute
# process window exited while the paused agent still had fifty minutes in which
# to answer, stranding the response without the reviewer identity that alone can
# reconsider it.
DEFAULT_CONTINUATION_WAIT_S = 3600.0
# A synthesis response is human/agent-paced, so sub-second visibility buys
# nothing. Fifteen seconds bounds response pickup while avoiding 1,800 reads per
# reviewer during an otherwise idle one-hour window.
DEFAULT_CONTINUATION_POLL_S = 15.0
_TERMINAL_PHASES = {"resolved", "failed", "escalated", "quorum_voting"}

logger = logging.getLogger(__name__)

_LAST_REVIEWER_PROVENANCE: dict[str, Any] = {
    "backend": "unselected",
    "host_id": None,
    "model_used": None,
    "models_used": [],
    "warnings": [],
}


def reviewer_backend_provenance() -> dict[str, Any]:
    """Return a copy of the latest backend selection/evidence for this process."""
    value = dict(_LAST_REVIEWER_PROVENANCE)
    value["models_used"] = list(value.get("models_used") or [])
    value["warnings"] = list(value.get("warnings") or [])
    return value


def _record_reviewer_provenance(value: dict[str, Any]) -> None:
    global _LAST_REVIEWER_PROVENANCE
    _LAST_REVIEWER_PROVENANCE = {
        "backend": value.get("backend"),
        "host_id": value.get("host_id"),
        "model_requested": value.get("model_requested"),
        "model_used": value.get("model_used"),
        "models_used": list(value.get("models_used") or []),
        "tokens_used": int(value.get("tokens_used") or 0),
        "cost_usd": value.get("cost_usd"),
        "latency_ms": value.get("latency_ms"),
        "finish_reason": value.get("finish_reason"),
        "fallback_from": value.get("fallback_from"),
        "warnings": list(value.get("warnings") or []),
    }


def _reviewer_model_type(provenance: dict[str, Any]) -> str:
    """Derive the identity model fingerprint without guessing an exact model."""
    models = [str(model) for model in (provenance.get("models_used") or [])]
    if models:
        return "dialectic_reviewer:" + "+".join(models)
    backend = str(provenance.get("backend") or "unknown")
    return f"dialectic_reviewer:{backend}"


def _reviewer_audit_text(provenance: dict[str, Any]) -> str:
    models = provenance.get("models_used") or ["provider_unreported"]
    parts = [
        f"backend={provenance.get('backend') or 'unknown'}",
        f"host={provenance.get('host_id') or 'unknown'}",
        f"models={','.join(str(model) for model in models)}",
    ]
    if provenance.get("fallback_from"):
        parts.append(f"fallback_from={provenance['fallback_from']}")
    if provenance.get("cost_usd") is not None:
        parts.append(f"provider_cost_usd={provenance['cost_usd']}")
    return "; ".join(parts)


# Keys worth persisting alongside the verdict. Deliberately an allowlist: the
# provenance dict is assembled from provider responses, and a denylist would
# leak any field a future backend adds.
_PERSISTED_PROVENANCE_KEYS = (
    "backend",
    "host_id",
    "model_requested",
    "model_used",
    "models_used",
    "tokens_used",
    "cost_usd",
    "latency_ms",
    "finish_reason",
    "fallback_from",
    "warnings",
)


def _provenance_for_message(provenance: dict[str, Any], *, degraded: bool) -> dict[str, Any]:
    """Non-secret reviewer provenance to store ON the verdict.

    ``_reviewer_audit_text`` already puts this in the reviewer's check-in
    ``response_text`` — but response_text is not persisted (3 of 30,063
    ``agent_state`` rows in 30 days carry it, and 0 of 4.18M audit events carry
    the reviewer's audit line), so that channel drops the evidence. The
    ``signature`` column is NOT an alternative: it is the protocol's HMAC
    attestation (``DialecticMessage.sign`` / ``verify_signatures``).

    ``observed_metrics`` is the surviving persisted slot on the antithesis row.
    Its readers address named keys (risk_score, coherence, coherence_source,
    coherence_role), so one namespaced key is inert to them.

    Why it matters: a replay of 14 real theses (2026-08-18) put local-model
    verdicts 36-50% apart from the deployed codex reviewer's. A verdict from
    the selected host and a verdict from a degraded fallback are therefore
    materially different objects, and without this they are indistinguishable
    in the ledger.
    """
    stored = {
        key: provenance[key]
        for key in _PERSISTED_PROVENANCE_KEYS
        if provenance.get(key) is not None
    }
    stored["degraded"] = bool(degraded)
    return stored


@dataclass
class Thesis:
    """Review payload passed at spawn time, including clearly separated paused-
    agent claims and the server-captured state snapshot. The child cannot read
    ``get_dialectic_session`` because that tool is ``register=False``."""

    session_id: str
    root_cause: str = ""
    proposed_conditions: list[str] = field(default_factory=list)
    reasoning: str = ""
    situation: str = ""  # free-text context about why the agent paused
    paused_agent_state: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_env(cls, env: Optional[dict[str, str]] = None) -> "Thesis":
        env = env if env is not None else os.environ
        sid = env.get("DIALECTIC_SESSION_ID", "")
        raw_conditions = env.get("DIALECTIC_THESIS_CONDITIONS", "")
        try:
            conditions = json.loads(raw_conditions) if raw_conditions else []
            if not isinstance(conditions, list):
                conditions = [str(conditions)]
        except (json.JSONDecodeError, ValueError):
            conditions = [c.strip() for c in raw_conditions.split("\n") if c.strip()]

        raw_state = env.get("DIALECTIC_PAUSED_AGENT_STATE", "")
        try:
            paused_agent_state = json.loads(raw_state) if raw_state else {}
            if not isinstance(paused_agent_state, dict):
                paused_agent_state = {}
        except (json.JSONDecodeError, ValueError, TypeError):
            paused_agent_state = {}

        return cls(
            session_id=sid,
            root_cause=env.get("DIALECTIC_THESIS_ROOT_CAUSE", ""),
            proposed_conditions=conditions,
            reasoning=env.get("DIALECTIC_THESIS_REASONING", ""),
            situation=env.get("DIALECTIC_THESIS_SITUATION", ""),
            paused_agent_state=paused_agent_state,
        )


@dataclass
class Verdict:
    agrees: bool
    root_cause: str
    proposed_conditions: list[str]
    reasoning: str
    # True when we could not extract a real model judgment and fell back to a
    # conservative default. A fallback verdict must DISAGREE — never rubber-stamp.
    degraded: bool = False


def build_review_prompt(thesis: Thesis) -> str:
    """Construct the independent-review prompt. Pure."""
    conditions = "\n".join(f"  - {c}" for c in thesis.proposed_conditions) or "  (none proposed)"
    pause_evidence = (
        json.dumps(thesis.paused_agent_state, indent=2, sort_keys=True, default=str)
        if thesis.paused_agent_state
        else "(not provided)"
    )
    return (
        "You are an INDEPENDENT reviewer in a dialectic governance process. A paused "
        "AI agent has proposed conditions under which it should resume work. Your job "
        "is to genuinely evaluate the proposal — NOT to rubber-stamp it. Disagreeing is "
        "a valid, expected outcome when the root-cause analysis is shallow, the "
        "conditions don't address the root cause, or the agent is rationalizing.\n\n"
        f"PAUSED AGENT'S SITUATION:\n{thesis.situation or '(not provided)'}\n\n"
        "SERVER-CAPTURED GOVERNANCE EVIDENCE AT SESSION OPEN "
        f"(not authored by the paused agent):\n{pause_evidence}\n"
        "Treat it as available only when evidence_status says available, and use "
        "policy_evaluation.action/enforcement—not the diagnostic ODE vector—to "
        "determine what the server actually decided.\n\n"
        f"PROPOSED ROOT CAUSE:\n{thesis.root_cause or '(none)'}\n\n"
        f"PROPOSED RESUMPTION CONDITIONS:\n{conditions}\n\n"
        f"AGENT'S REASONING:\n{thesis.reasoning or '(none)'}\n\n"
        "Respond with STRICT JSON only, no prose outside it:\n"
        "{\n"
        '  "agrees": true | false,\n'
        '  "root_cause": "your assessment of the actual root cause",\n'
        '  "proposed_conditions": ["condition 1", "condition 2"],\n'
        '  "reasoning": "why you agree or disagree"\n'
        "}\n"
        "If you disagree, set agrees=false and use proposed_conditions to state what "
        "you would require instead. If you agree, proposed_conditions must be non-empty."
    )


def build_continuation_prompt(
    thesis: Thesis,
    previous_verdict: Verdict,
    paused_response: dict[str, Any],
    synthesis_round: Optional[int] = None,
) -> str:
    """Ask the same reviewer to judge a paused-agent response. Pure.

    ``agrees=true`` is deliberately framed as independent ratification, not as
    an echo of the paused agent's requested outcome. This is the second half of
    a real synthesis round: objection, response, then reviewer reconsideration.
    """
    previous = json.dumps(
        {
            "agrees": previous_verdict.agrees,
            "root_cause": previous_verdict.root_cause,
            "proposed_conditions": previous_verdict.proposed_conditions,
            "reasoning": previous_verdict.reasoning,
        },
        indent=2,
        sort_keys=True,
        default=str,
    )
    response = json.dumps(paused_response, indent=2, sort_keys=True, default=str)
    round_label = str(synthesis_round) if synthesis_round is not None else "unknown"
    return (
        "You are the SAME independent reviewer continuing a dialectic governance "
        "session. You previously rejected the paused agent's proposal. The paused "
        "agent has now responded. Decide whether that response actually addresses "
        "your objection. Do not approve merely because the agent says it agrees. "
        "Set agrees=true only if YOU independently ratify the revised root cause "
        "and conditions; otherwise keep agrees=false and state what remains.\n\n"
        f"ORIGINAL ROOT CAUSE CLAIM:\n{thesis.root_cause or '(none)'}\n\n"
        "ORIGINAL PROPOSED CONDITIONS:\n"
        f"{json.dumps(thesis.proposed_conditions, indent=2, default=str)}\n\n"
        f"YOUR PREVIOUS VERDICT:\n{previous}\n\n"
        f"PAUSED AGENT RESPONSE (synthesis round {round_label}):\n{response}\n\n"
        "Respond with STRICT JSON only, no prose outside it:\n"
        "{\n"
        '  "agrees": true | false,\n'
        '  "root_cause": "your current assessment",\n'
        '  "proposed_conditions": ["condition 1", "condition 2"],\n'
        '  "reasoning": "why the response does or does not satisfy your objection"\n'
        "}\n"
        "If agrees=true, proposed_conditions must contain the terms you ratify."
    )


def parse_reviewer_verdict(model_text: str) -> Verdict:
    """Derive a Verdict from raw model output. Pure.

    The independence-critical property: a model that expresses disagreement yields
    ``agrees=False``. Anything we cannot parse degrades to a DISAGREE verdict — a
    reviewer that cannot form a judgment must not silently approve.
    """
    text = _THINK_BLOCK.sub("", model_text or "").strip()
    match = _JSON_OBJECT.search(text)
    if not match:
        return Verdict(
            agrees=False,
            root_cause="",
            proposed_conditions=[],
            reasoning="Reviewer model returned no parseable verdict; defaulting to "
            "disagreement (no independent approval without a real judgment).",
            degraded=True,
        )
    try:
        obj = json.loads(match.group(0))
    except json.JSONDecodeError:
        return Verdict(
            agrees=False,
            root_cause="",
            proposed_conditions=[],
            reasoning="Reviewer model emitted malformed JSON; defaulting to "
            "disagreement.",
            degraded=True,
        )

    agrees = _coerce_bool(obj.get("agrees"))
    conditions = obj.get("proposed_conditions") or obj.get("conditions") or []
    if isinstance(conditions, str):
        conditions = [conditions] if conditions else []
    conditions = [str(c).strip() for c in conditions if str(c).strip()]

    return Verdict(
        agrees=agrees,
        root_cause=str(obj.get("root_cause", "")).strip(),
        proposed_conditions=conditions,
        reasoning=str(obj.get("reasoning", "")).strip(),
        degraded=False,
    )


def _coerce_bool(value: Any) -> bool:
    """Match the server's submit_synthesis coercion (handlers.py ~1631): only an
    explicit truthy token agrees. Absent / unknown ⇒ False (don't approve by
    default)."""
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in ("true", "1", "yes")
    return False


def extract_last_json_object(text: str) -> Optional[str]:
    """Return the LAST parseable top-level JSON object in ``text`` that carries
    an ``agrees`` key, or None. Pure.

    A ``codex exec`` transcript echoes the whole prompt (whose JSON *template*
    contains ``true | false`` and is deliberately unparseable) plus banners and
    exec traces before the final verdict — so the naive first-``{``-to-last-``}``
    regex used for local models would swallow the transcript. Scan forward with
    ``raw_decode`` (which consumes nested braces correctly) and keep the last
    verdict-shaped object.
    """
    decoder = json.JSONDecoder()
    last: Optional[str] = None
    pos = 0
    while True:
        start = text.find("{", pos)
        if start == -1:
            return last
        try:
            obj, consumed = decoder.raw_decode(text[start:])
        except json.JSONDecodeError:
            pos = start + 1
            continue
        if isinstance(obj, dict) and "agrees" in obj:
            last = text[start : start + consumed]
        pos = start + consumed


def find_pending_paused_response(
    session_data: dict[str, Any],
    *,
    paused_agent_id: Optional[str] = None,
    reviewer_agent_id: Optional[str] = None,
) -> Optional[dict[str, Any]]:
    """Return the latest paused-agent synthesis after the reviewer's last one.

    Transcript order is the turn boundary. Looking only for *any* paused-agent
    synthesis would replay an old response on every poll; looking only at a
    timestamp would make correctness depend on clock formatting. This scan uses
    the append-only message order already guaranteed by the session read path.
    """
    paused_agent_id = (
        session_data.get("paused_agent_id")
        or session_data.get("paused_agent")
        or paused_agent_id
    )
    reviewer_agent_id = (
        session_data.get("reviewer_agent_id")
        or session_data.get("reviewer")
        or reviewer_agent_id
    )
    if not paused_agent_id or not reviewer_agent_id:
        return None

    transcript = session_data.get("transcript") or session_data.get("messages") or []
    last_reviewer_synthesis = -1
    for index, message in enumerate(transcript):
        phase = (
            message.get("phase") or message.get("message_type") or message.get("role")
            if isinstance(message, dict)
            else getattr(message, "phase", None)
        )
        agent_id = (
            message.get("agent_id")
            if isinstance(message, dict)
            else getattr(message, "agent_id", None)
        )
        if phase == "synthesis" and agent_id == reviewer_agent_id:
            last_reviewer_synthesis = index

    if last_reviewer_synthesis < 0:
        return None

    pending: Optional[dict[str, Any]] = None
    for message in transcript[last_reviewer_synthesis + 1 :]:
        phase = (
            message.get("phase") or message.get("message_type") or message.get("role")
            if isinstance(message, dict)
            else getattr(message, "phase", None)
        )
        agent_id = (
            message.get("agent_id")
            if isinstance(message, dict)
            else getattr(message, "agent_id", None)
        )
        if phase != "synthesis" or agent_id != paused_agent_id:
            continue
        if isinstance(message, dict):
            pending = dict(message)
        else:
            pending = {
                key: getattr(message, key, None)
                for key in (
                    "agent_id",
                    "timestamp",
                    "agrees",
                    "root_cause",
                    "proposed_conditions",
                    "reasoning",
                    "concerns",
                )
            }
    return pending


# --------------------------------------------------------------------------- #
# Async wiring (the impure shell). Kept thin; the testable logic is above.
# --------------------------------------------------------------------------- #
def _env_float(name: str, default: float, *, minimum: float = 0.0) -> float:
    try:
        return max(minimum, float(os.getenv(name, str(default))))
    except (TypeError, ValueError):
        return default


async def call_codex_reviewer(prompt: str) -> Optional[str]:
    """Run the review on Codex (``codex exec``, ChatGPT-subscription CLI) —
    the capable-heterogeneous reviewer path (2026-07-02 planted-flaw probe:
    Codex named the planted circular-auth flaw first; gemma4 one-shot affirmed
    it — the judgment gap is model class, not prompt or parse).

    Opt-in via ``UNITARES_DIALECTIC_REVIEWER_HOST=codex``; returns the final
    JSON verdict string, or None on ANY failure (CLI absent, non-zero exit,
    timeout, no parseable verdict) — the caller then falls back to the free
    local model, so the no-budget default path is never removed (execution-cost
    policy: subscription CLI is an opt-in backend, never a requirement).

    Spawn recipe mirrors the host adapter's proven one: ``sh -c 'exec
    "$DR_CLI" exec … "$DR_PROMPT" </dev/null'`` — stdin CLOSED (codex blocks
    reading a non-tty stdin pipe) and paths/prompts passed via env, never
    argv-interpolated.
    """
    cli_path = resolve_host_cli("codex:host-adapter")
    if cli_path is None:
        return None
    # Read literally (not via _env_float) so scripts/dev/flag_catalog.py's AST
    # scan still indexes this flag — the catalog only sees direct os.getenv calls.
    try:
        timeout_s = float(os.getenv("UNITARES_DIALECTIC_CODEX_TIMEOUT_S", "420"))
    except (TypeError, ValueError):
        timeout_s = 420.0
    timeout_s = max(1.0, timeout_s)
    try:
        proc = await asyncio.create_subprocess_exec(
            "/bin/sh",
            "-c",
            'exec "$DR_CLI" exec --sandbox read-only --skip-git-repo-check '
            '"$DR_PROMPT" </dev/null',
            env={**os.environ, "DR_CLI": cli_path, "DR_PROMPT": prompt},
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )
    except Exception:  # noqa: BLE001 - selected-host failure falls back locally
        return None
    try:
        stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=timeout_s)
    except asyncio.TimeoutError:
        proc.kill()
        await proc.communicate()
        return None
    except Exception:  # noqa: BLE001 - selected-host failure falls back locally
        try:
            proc.kill()
            await proc.communicate()
        except Exception:
            pass
        return None
    if proc.returncode != 0:
        return None
    return extract_last_json_object(stdout.decode(errors="replace"))


async def call_claude_reviewer(prompt: str) -> HostReviewResult:
    """Run the safe Claude subscription-CLI backend with exact provenance."""
    return await call_claude_backend(prompt)


async def call_external_reviewer(prompt: str) -> HostReviewResult:
    """Run an operator-configured OpenAI-compatible backend (base_url + model
    + key env name). This is the third-family seam: Gemini, an OpenAI endpoint,
    or any compatible host is a CONFIGURATION of this path, not a code branch.
    """
    return await call_openai_compat_backend(prompt)


async def obtain_reviewer_text(prompt: str) -> str:
    """Route to the configured reviewer backend, falling back to the free
    local model. Default (env unset) is byte-identical to the pre-existing
    gemma4 path."""
    host = os.getenv("UNITARES_DIALECTIC_REVIEWER_HOST", "").strip().lower()
    fallback_from: Optional[str] = None
    fallback_warning: Optional[str] = None
    if host in ("claude", "claude:host-adapter"):
        result = await call_claude_reviewer(prompt)
        if result.text is not None:
            _record_reviewer_provenance(result.provenance())
            return result.text
        fallback_from = result.host_id
        fallback_warning = result.error
    elif host in ("codex", "codex:host-adapter"):
        text = await call_codex_reviewer(prompt)
        if text is not None:
            _record_reviewer_provenance({
                "backend": "codex",
                "host_id": "codex:host-adapter",
                "models_used": [],
                "warnings": ["Codex CLI did not report an exact model identifier"],
            })
            return text
        fallback_from = "codex:host-adapter"
        fallback_warning = "Codex backend unavailable or returned no verdict"
    elif host in ("external", "openai_compat", "openai-compatible", "gemini"):
        # ``gemini`` is accepted as an ALIAS for the configured external host —
        # it selects no vendor logic, only this path. Misconfiguration surfaces
        # as a warning on the local fallback, never as a silent vendor default.
        result = await call_external_reviewer(prompt)
        if result.text is not None:
            _record_reviewer_provenance(result.provenance())
            return result.text
        fallback_from = result.host_id
        fallback_warning = result.error
    elif host not in ("", "local", "ollama", "ollama:local"):
        fallback_from = host
        fallback_warning = f"Unknown reviewer host '{host}'"

    # Any selected-host failure degrades to the local default, never harder
    # than the pre-existing path.
    text = await call_reviewer_model(prompt)
    warnings = [fallback_warning] if fallback_warning else []
    _record_reviewer_provenance({
        "backend": "ollama",
        "host_id": "ollama:local",
        "model_used": DEFAULT_MODEL,
        "models_used": [DEFAULT_MODEL],
        "fallback_from": fallback_from,
        "warnings": warnings,
    })
    return text


async def call_reviewer_model(prompt: str, model: str = DEFAULT_MODEL) -> str:
    """Run the local heterogeneous model in THIS process (not via the server's
    call_model tool, whose 30s timeout is shorter than gemma4's 43–70s budget).
    Localhost Ollama, OpenAI-compat — no paid API.

    Uses the ASYNC client + ``await`` so the ~40-70s model call does not block the
    event loop (this is an ``async def`` driven by ``asyncio.run``)."""
    from openai import AsyncOpenAI  # local import: only the runner process needs it

    client = AsyncOpenAI(base_url=OLLAMA_BASE_URL, api_key="ollama")
    kwargs: dict[str, Any] = {
        "model": model,
        "messages": [{"role": "user", "content": prompt}],
        "max_tokens": int(os.getenv("UNITARES_DIALECTIC_REVIEW_MAX_TOKENS", "1024")),
        "temperature": 0.2,
    }
    resp = await client.chat.completions.create(**kwargs)
    return resp.choices[0].message.content or ""


def _verdict_with_ratified_conditions(
    verdict: Verdict,
    paused_response: dict[str, Any],
    previous_verdict: Verdict,
) -> Verdict:
    """Make an approving verdict explicit about which conditions it ratifies.

    Models occasionally emit ``agrees=true`` with an empty condition list. The
    protocol correctly refuses that shape. If the paused response supplied
    terms, explicit approval ratifies those terms; otherwise inherit the prior
    reviewer's terms. With no terms anywhere, degrade to disagreement instead
    of manufacturing an empty approval.
    """
    if not verdict.agrees or verdict.proposed_conditions:
        return verdict

    inherited = paused_response.get("proposed_conditions") or previous_verdict.proposed_conditions
    if isinstance(inherited, str):
        inherited = [inherited] if inherited.strip() else []
    inherited = [str(item).strip() for item in (inherited or []) if str(item).strip()]
    if inherited:
        return Verdict(
            agrees=True,
            root_cause=verdict.root_cause,
            proposed_conditions=inherited,
            reasoning=verdict.reasoning,
            degraded=verdict.degraded,
        )
    return Verdict(
        agrees=False,
        root_cause=verdict.root_cause,
        proposed_conditions=[],
        reasoning=(
            verdict.reasoning
            + " Approval omitted the conditions being ratified; retaining the objection."
        ).strip(),
        degraded=True,
    )


async def continue_after_disagreement(
    client: Any,
    thesis: Thesis,
    initial_verdict: Verdict,
    *,
    paused_agent_id: Optional[str],
    reviewer_agent_id: Optional[str],
) -> Verdict:
    """Run bounded objection → response → reconsideration rounds.

    The wall-clock budget includes polling and every follow-up model call. This
    leaves the orchestrator's process deadline as a separate hard backstop.
    """
    wait_s = _env_float(
        "UNITARES_DIALECTIC_CONTINUATION_WAIT_S", DEFAULT_CONTINUATION_WAIT_S
    )
    if wait_s <= 0:
        return initial_verdict
    poll_s = _env_float(
        "UNITARES_DIALECTIC_CONTINUATION_POLL_S",
        DEFAULT_CONTINUATION_POLL_S,
        minimum=0.01,
    )
    deadline = time.monotonic() + wait_s
    current_verdict = initial_verdict
    read_failures = 0

    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return current_verdict

        try:
            session_data = await client.call_tool(
                "dialectic", {"action": "get", "session_id": thesis.session_id}
            )
        except Exception as exc:  # noqa: BLE001 — bounded polling tolerates a transient read
            read_failures += 1
            log = logger.warning if read_failures == 1 else logger.debug
            log("Dialectic continuation read failed: %r", exc)
            await asyncio.sleep(min(poll_s, max(0.0, deadline - time.monotonic())))
            continue

        if not isinstance(session_data, dict):
            return current_verdict
        phase = str(session_data.get("phase") or "").lower()
        if phase in _TERMINAL_PHASES:
            return current_verdict

        try:
            synthesis_round = int(session_data.get("synthesis_round"))
        except (TypeError, ValueError):
            synthesis_round = None
        try:
            max_rounds = int(session_data.get("max_synthesis_rounds"))
        except (TypeError, ValueError):
            max_rounds = None
        if (
            synthesis_round is not None
            and max_rounds is not None
            and synthesis_round > max_rounds
        ):
            return current_verdict

        paused_response = find_pending_paused_response(
            session_data,
            paused_agent_id=paused_agent_id,
            reviewer_agent_id=reviewer_agent_id,
        )
        if paused_response is None:
            await asyncio.sleep(min(poll_s, max(0.0, deadline - time.monotonic())))
            continue

        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return current_verdict
        prompt = build_continuation_prompt(
            thesis, current_verdict, paused_response, synthesis_round
        )
        try:
            model_text = await asyncio.wait_for(
                obtain_reviewer_text(prompt), timeout=remaining
            )
        except asyncio.TimeoutError:
            return current_verdict
        except Exception as exc:  # noqa: BLE001 — preserve the standing rejection
            logger.warning("Dialectic continuation model failed: %r", exc)
            return current_verdict
        next_verdict = _verdict_with_ratified_conditions(
            parse_reviewer_verdict(model_text), paused_response, current_verdict
        )
        result = await client.call_tool(
            "dialectic",
            {
                "action": "synthesis",
                "session_id": thesis.session_id,
                "agrees": next_verdict.agrees,
                "proposed_conditions": next_verdict.proposed_conditions,
                "root_cause": next_verdict.root_cause,
                # There is no second antithesis call, so the reconsideration's
                # rationale belongs on this follow-up synthesis.
                "reasoning": next_verdict.reasoning,
            },
        )
        if isinstance(result, dict) and result.get("success") is False:
            logger.warning("Dialectic continuation synthesis was refused: %s", result)
            return current_verdict
        current_verdict = next_verdict
        if next_verdict.agrees:
            return current_verdict


async def run(thesis: Thesis, governance_url: str, parent_agent_id: Optional[str]) -> Verdict:
    """Onboard, submit an independent verdict, and continue if it rejects."""
    from unitares_sdk.client import GovernanceClient  # type: ignore

    reviewer_text = await obtain_reviewer_text(build_review_prompt(thesis))
    provenance = reviewer_backend_provenance()
    verdict = parse_reviewer_verdict(reviewer_text)

    client = GovernanceClient(governance_url)
    await client.connect()
    try:
        await client.onboard(
            name=REVIEWER_NAME,  # required first arg of GovernanceClient.onboard
            force_new=True,
            parent_agent_id=parent_agent_id,
            spawn_reason=SPAWN_REASON,
            model_type=_reviewer_model_type(provenance),
        )
        # Claim the open reviewer slot as first-responder. The bare submit_*
        # handlers are register=False; the public MCP surface is the `dialectic`
        # umbrella tool (action='antithesis'/'synthesis'). (live-found 2026-06-23)
        await client.call_tool(
            "dialectic",
            {
                "action": "antithesis",
                "session_id": thesis.session_id,
                "reasoning": verdict.reasoning,
                # Attribution rides the antithesis because it is the reviewer's
                # own first message and is always written; the synthesis row
                # joins to it by session_id. See _provenance_for_message.
                "observed_metrics": {
                    "reviewer_backend": _provenance_for_message(
                        provenance, degraded=verdict.degraded
                    )
                },
            },
        )
        # Submit the model-derived verdict — agrees may be False (the whole point).
        #
        # No `reasoning` here, deliberately. The argument was made once, in the
        # antithesis above; this message is the VERDICT (agrees + conditions +
        # root_cause), which is what the synthesis row is actually for —
        # antithesis rows carry those fields in 1 of 98 rows, synthesis rows in
        # ~134 of 149. Passing verdict.reasoning to both calls is what made the
        # synthesis a byte-identical replay of the antithesis in 60 of 60
        # orchestrated sessions since 2026-06-23, so every transcript showed
        # the same paragraph twice under two different headings.
        #
        # Safe to omit only because finalize_resolution now falls back to this
        # same agent's antithesis reasoning (dialectic_protocol.reasoning_of);
        # without that fallback this would blank the rationale on every
        # approved resolution, which is why the naive version was reverted.
        synthesis_result = await client.call_tool(
            "dialectic",
            {
                "action": "synthesis",
                "session_id": thesis.session_id,
                "agrees": verdict.agrees,
                "proposed_conditions": verdict.proposed_conditions,
                "root_cause": verdict.root_cause,
            },
        )
        # A real check-in after the initial judgment (subagent-onboarding
        # discipline). On disagreement the process remains alive, but this
        # records meaningful work even if the orchestrator later reaps it.
        # SDK checkin() maps to the server's process_agent_update.
        await client.checkin(
            response_text=(
                f"dialectic review submitted: agrees={verdict.agrees}"
                + (" (degraded fallback)" if verdict.degraded else "")
                + f"; {_reviewer_audit_text(provenance)}"
            ),
            complexity=0.4,
            confidence=0.6 if not verdict.degraded else 0.3,
        )
        if not verdict.agrees and not (
            isinstance(synthesis_result, dict)
            and synthesis_result.get("success") is False
        ):
            verdict = await continue_after_disagreement(
                client,
                thesis,
                verdict,
                paused_agent_id=parent_agent_id,
                reviewer_agent_id=getattr(client, "agent_uuid", None),
            )
        return verdict
    finally:
        await client.disconnect()


def main() -> int:
    import asyncio

    thesis = Thesis.from_env()
    if not thesis.session_id:
        print("FATAL: DIALECTIC_SESSION_ID not set in spawn payload", flush=True)
        return 2
    governance_url = os.getenv("UNITARES_GOVERNANCE_URL") or os.getenv("GOVERNANCE_URL", "")
    parent = os.getenv("UNITARES_PARENT_AGENT_ID") or None
    try:
        verdict = asyncio.run(run(thesis, governance_url, parent))
    except Exception as exc:  # noqa: BLE001 — a reviewer crash must be loud, not silent
        print(f"FATAL: reviewer failed: {exc!r}", flush=True)
        return 1
    print(f"reviewer done: agrees={verdict.agrees} degraded={verdict.degraded}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
