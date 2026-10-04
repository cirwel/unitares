"""Orchestrated dialectic reviewer — a standalone, independently-accountable
reviewer process.

The agent-orchestrator spawns this as a supervised, lease-bound child when a
dialectic session needs a reviewer. Unlike the in-process synthetic path
(`handle_llm_assisted_dialectic`, which runs in the caller's process under the
synthetic id ``llm-synthetic-reviewer`` and signs with the paused agent's
api_key; it originally hardcoded ``agrees=True`` and now binds its verdict via
``_synthetic_review_approves``), this process:

  * onboards as its OWN governance identity (strict-identity compliant),
  * runs an operator-selected heterogeneous model (local Ollama by default,
    subscription-auth Codex or Claude when configured) IN its own process to
    form a *genuine* verdict that may DISAGREE,
  * submits that verdict through the ordinary dialectic protocol tools, and
  * after a disagreement, stays alive for a bounded window to evaluate the
    paused agent's response under the SAME reviewer identity before exiting.

Design: docs/proposals/active/orchestrated-dialectic-reviewer-v0.md

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
from dataclasses import dataclass, field, replace
from typing import Any, Optional

from src.identity.lineage_semantics import LineageSpawnReason
from src.local_inference_env import (
    no_redirect_http_client,
    EndpointNotLocalError,
    default_local_model,
    model_base_url,
    require_local_endpoint,
)

from .host_list import ListedHost, reviewer_host_plan
from .host_backends import (
    HostReviewResult,
    call_antigravity_backend,
    call_claude_backend,
    call_openai_compat_backend,
    resolve_host_cli,
)

# gemma4 hides its answer behind a <think> block under thinking mode; strip it
# before JSON extraction (mirrors llm_delegation._wants_reasoning_effort_none).
_THINK_BLOCK = re.compile(r"<think>.*?</think>", re.DOTALL | re.IGNORECASE)
# The model is asked for strict JSON, but local models fence it or add prose.
_JSON_OBJECT = re.compile(r"\{.*\}", re.DOTALL)

# Same resolution as the server's local lane (src/local_inference_env.py):
# UNITARES_MODEL_BASE_URL (or an older name from its alias table), the
# OpenAI-compatible base including /v1.
DEFAULT_MODEL = default_local_model()
OLLAMA_BASE_URL = model_base_url()
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
        **{key: value.get(key) for key in _HOST_LIST_PROVENANCE_KEYS},
    }


# The host-list fields of a verdict's provenance (design 2.2 and 2.6): which
# list it was produced under, every host tried, and whether the answering host
# may approve. ``vouched`` is the one approval reads; absent means withheld.
_HOST_LIST_PROVENANCE_KEYS = (
    "host_list",
    "host_config_digest",
    "attempts",
    "vouched",
    "vouched_by",
    "authorized_by",
    "answered_family",
)


class ReviewerText(str):
    """A reviewer reply that carries the provenance of the call that produced
    it, so a later call that raises before recording cannot leave an earlier
    call's provenance (and its ``vouched``) attached to this verdict.

    ``host_key`` is the listed host that answered, or ``FLOOR`` for the local
    endpoint; a continuation pins to it (design 2.2)."""

    provenance: dict[str, Any]
    host_key: str

    def __new__(cls, text: str, provenance: dict[str, Any], host_key: str) -> "ReviewerText":
        value = super().__new__(cls, text)
        value.provenance = dict(provenance)
        value.host_key = host_key
        return value


FLOOR = "__floor__"


def _provenance_of(text: Any) -> dict[str, Any]:
    """The provenance of the call that produced ``text``. A plain string (a
    test double) falls back to the process record."""
    own = getattr(text, "provenance", None)
    return dict(own) if isinstance(own, dict) else reviewer_backend_provenance()


def _listed_host_of(text: Any) -> Optional[str]:
    key = getattr(text, "host_key", None)
    return key if isinstance(key, str) and key != FLOOR else None


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
    *_HOST_LIST_PROVENANCE_KEYS,
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
    # The server files this dict as-is (an orchestrated reviewer's stamp is
    # passed through, not rebuilt), so a kind left off here is never added:
    # every one of 120 orchestrated antitheses to 2026-09-26 carried none, and
    # a count of reviewer_kind='orchestrated' read zero while this process
    # wrote them all. The kinds are _REVIEWER_KINDS in the dialectic handlers.
    stored["reviewer_kind"] = "orchestrated"
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
        except (json.JSONDecodeError, ValueError, RecursionError):
            conditions = [c.strip() for c in raw_conditions.split("\n") if c.strip()]

        raw_state = env.get("DIALECTIC_PAUSED_AGENT_STATE", "")
        try:
            paused_agent_state = json.loads(raw_state) if raw_state else {}
            if not isinstance(paused_agent_state, dict):
                paused_agent_state = {}
        except (json.JSONDecodeError, ValueError, TypeError, RecursionError):
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
    # False ONLY when no judgment was formed at all (the model returned nothing
    # we could parse). This is NOT the same as `degraded`: a model that judged
    # and was then conservatively downgraded — approving without naming the
    # terms it ratifies — is degraded but HAS judged, and its objection is real
    # and must still be filed. A verdict with judgment_formed=False is not a
    # verdict; see run(), which abstains on it rather than filing it.
    judgment_formed: bool = True
    # Filled from the server's abstention response so operators know whether
    # this attempt found an actually open slot or an existing assignment.
    reviewer_slot_open: bool | None = None


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
        "SERVER-CAPTURED GOVERNANCE RECORD AT SESSION OPEN "
        "(bounded to decision-relevant fields; primary measurements may derive "
        f"from caller-published sensor inputs):\n{pause_evidence}\n"
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


#: One repair attempt, then abstain. A weaker local model routinely forms a
#: perfectly good judgment and then fails the JSON envelope; a single re-ask
#: separates that formatting slip from a model that genuinely cannot judge, and
#: only the second deserves an abstention. More attempts would just be waiting:
#: the paused agent is blocked throughout and this reviewer holds no slot while
#: it retries. Raised from zero on 2026-09-19 review: one-shot abstention
#: treated a transient parse failure as proof that no judgment could be formed.
_VERDICT_REPAIR_ATTEMPTS = 1


def build_repair_prompt(thesis: Thesis, unparseable: str) -> str:
    """Re-ask for the judgment already made, in the shape the protocol needs.

    Deliberately NOT a fresh review: re-running the original prompt would
    invite a different verdict and turn a formatting retry into quiet
    reviewer-shopping. The model's own unusable reply is quoted back so it can
    restate the same position as JSON.
    """
    return (
        build_review_prompt(thesis)
        + "\n\n---\nYOUR PREVIOUS REPLY COULD NOT BE PARSED. It is quoted below.\n"
        "Do NOT reconsider the thesis and do NOT change your position — restate "
        "the SAME judgment you already reached, as STRICT JSON and nothing "
        "else, with no prose before or after it and no markdown fence:\n"
        '{"agrees": true | false, "root_cause": "...", '
        '"proposed_conditions": ["..."], "reasoning": "..."}\n\n'
        f"YOUR UNPARSEABLE REPLY:\n{unparseable[:4000]}"
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
            judgment_formed=False,
        )
    try:
        obj = json.loads(match.group(0))
    except (json.JSONDecodeError, RecursionError):
        return Verdict(
            agrees=False,
            root_cause="",
            proposed_conditions=[],
            reasoning="Reviewer model emitted malformed JSON; defaulting to "
            "disagreement.",
            degraded=True,
            judgment_formed=False,
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
        judgment_formed=True,
    )


def withhold_fallback_approval(verdict: Verdict, provenance: dict[str, Any]) -> Verdict:
    """AN APPROVAL NEEDS AN AFFIRMATIVE MARK. Pure.

    A verdict may approve only when its provenance says ``vouched: true``,
    which ``obtain_reviewer_text`` sets in two cases: a host the operator
    listed, with ``may_approve``, answered this call; or no list is set and
    the local model is the reviewer, as it was before lists existed. Anything
    else, a missing field included, withholds. A rule keyed on a field that
    marks a fallback would fail open on every path that forgot to set it; this
    one fails closed (design: docs/proposals/active/dialectic-reviewer-hosts-v0.md
    section 2.2).

    The case it exists for: the operator listed hosts, none answered, and the
    local model answered in their place. That substitute may object, and its
    objection is still filed: it names terms the paused agent can answer, and
    the session stays open while it does. A substitute APPROVAL can release the
    paused agent, so it is withheld and the reviewer abstains, leaving the slot
    open for a reviewer that can judge. ``degraded`` keeps its existing
    meaning (could a judgment be extracted) and is not changed here.

    Live instance, #2379: session 17ca66285f91e61e selected gemini-3.8-flash,
    which was truncated before its verdict; gemma4 restated the thesis's own
    conditions as commitments and the session resolved ``agrees: true`` 34
    seconds after the request, with none of the thesis's four questions
    addressed.
    """
    if (
        provenance.get("vouched") is True
        or not verdict.judgment_formed
        or not verdict.agrees
    ):
        return verdict
    fallback_from = provenance.get("fallback_from")
    if fallback_from:
        reasoning = (
            f"The selected reviewer host ({fallback_from}) returned no verdict, "
            "and the local fallback model approved. A fallback approval is "
            "withheld rather than filed: the selected host did not review this "
            "thesis."
        )
    else:
        reasoning = (
            "The verdict's provenance does not show a reviewer with approval "
            "authority, so its approval is withheld rather than filed."
        )
    return replace(
        verdict,
        agrees=False,
        proposed_conditions=[],
        reasoning=reasoning,
        # Not a claim that the answering model failed to judge. The judgment
        # the operator selected was not formed, and this one is not accepted
        # in its place, so the protocol records an abstention.
        judgment_formed=False,
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
        except RecursionError:
            # Too deeply nested to decode. Skipping one brace and rescanning is
            # quadratic in the depth, so the whole transcript counts as no verdict.
            return None
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
    # Read literally (not via _env_float) because this timeout has bespoke clamp
    # behavior; the flag catalog supports both direct reads and selected wrappers.
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


async def call_antigravity_reviewer(prompt: str) -> HostReviewResult:
    """Run the Antigravity CLI (agy) subscription backend from an empty workspace."""
    return await call_antigravity_backend(prompt)


async def call_external_reviewer(prompt: str) -> HostReviewResult:
    """Run an operator-configured OpenAI-compatible backend (base_url + model
    + key env name). This is the third-family seam: Gemini, an OpenAI endpoint,
    or any compatible host is a CONFIGURATION of this path, not a code branch.
    """
    return await call_openai_compat_backend(prompt)


async def _call_listed_host(
    host: ListedHost, prompt: str
) -> tuple[Optional[str], dict[str, Any], Optional[str]]:
    """One listed host's reply, its provenance, and why it gave none."""
    if host.key == "codex":
        text = await call_codex_reviewer(prompt)
        provenance = {
            "backend": "codex",
            "host_id": host.host_id,
            "models_used": [],
            "warnings": ["Codex CLI did not report an exact model identifier"],
        }
        return text, provenance, None if text is not None else (
            "Codex backend unavailable or returned no verdict"
        )
    if host.key == "claude":
        result = await call_claude_reviewer(prompt)
    elif host.key == "antigravity":
        result = await call_antigravity_reviewer(prompt)
    else:
        # ``external``: the operator-configured OpenAI-compatible host. Its
        # misconfiguration surfaces as an attempt's reason, never as a silent
        # vendor default.
        result = await call_external_reviewer(prompt)
    return result.text, result.provenance(), result.error


async def obtain_reviewer_text(prompt: str, *, pinned: Optional[str] = None) -> ReviewerText:
    """Ask the operator's listed hosts in order, then the free local model.

    A host that returns no reply (an error, a timeout, a nonzero exit, or a
    reply holding no verdict object, which each backend already reports as no
    text) is skipped for the next one. A host that returns a reply answers:
    its reply goes to the caller's parse and repair, and is never traded for
    another host's, because a reply that does not parse may still be an
    objection in prose. The order is the operator's and is only ever
    shortened (design: docs/proposals/active/dialectic-reviewer-hosts-v0.md).

    ``pinned``: a listed host's key, to ask only that host before the floor,
    or ``FLOOR`` for the local model alone. With no list set, the local model
    is the reviewer and the reply is byte-identical to the pre-list path.
    """
    plan = reviewer_host_plan(local_base_url=OLLAMA_BASE_URL)
    if pinned == FLOOR:
        hosts: tuple[ListedHost, ...] = ()
    elif pinned is not None:
        hosts = tuple(host for host in plan.hosts if host.key == pinned)
    else:
        hosts = plan.hosts
    listing: dict[str, Any] = {
        "host_list": plan.keys or None,
        "host_config_digest": plan.digest or None,
        # One operator today. Recorded so a later federation can tell whose
        # configuration released an agent.
        "authorized_by": "deployment_config" if plan.listed else None,
    }
    warnings: list[str] = [plan.error] if plan.error else []
    attempts: list[dict[str, Any]] = []
    for host in hosts:
        text, provenance, error = await _call_listed_host(host, prompt)
        # The backend's own host id where it reports one (the external host
        # names its endpoint), else the listed id.
        host_id = provenance.get("host_id") or host.host_id
        if text is not None:
            attempts.append({"host": host_id, "outcome": "reply"})
            answered = {
                **provenance,
                **listing,
                "attempts": attempts,
                "warnings": [*warnings, *(provenance.get("warnings") or [])],
                "fallback_from": attempts[0]["host"] if len(attempts) > 1 else None,
                "vouched": host.may_approve,
                "vouched_by": "listed_host" if host.may_approve else None,
                "answered_family": host.family,
            }
            _record_reviewer_provenance(answered)
            return ReviewerText(text, answered, host.key)
        reason = str(error or "no reply")[:200]
        attempts.append({"host": host_id, "outcome": "no_reply", "reason": reason})
        warnings.append(reason)

    # The floor. With a list it may object but not approve: the operator asked
    # for a listed host's judgment. With no list it is the reviewer and keeps
    # the approval authority it always had.
    if attempts:
        fallback_from: Optional[str] = attempts[0]["host"]
    elif plan.error:
        fallback_from = plan.raw or "invalid_host_list"
    else:
        fallback_from = None
    vouched = not plan.listed
    floor_listing = {
        **listing,
        "attempts": attempts or None,
        "vouched": vouched,
        "vouched_by": "no_list_default" if vouched else None,
    }
    try:
        text = await call_reviewer_model(prompt)
    except EndpointNotLocalError as exc:
        # The local fallback is the operator's own model by definition. When
        # the endpoint does not classify local, nothing is sent: the empty text
        # parses to no judgment, so the reviewer abstains, and the reason
        # travels in the provenance warnings recorded with the abstention.
        logger.warning("Dialectic reviewer local backend refused: %s", exc)
        refused = {
            "backend": "ollama",
            "host_id": "ollama:local",
            "model_requested": DEFAULT_MODEL,
            "models_used": [],
            "fallback_from": fallback_from,
            "warnings": [*warnings, f"{exc.code}: {exc}"],
            **floor_listing,
            "vouched": False,
            "vouched_by": None,
        }
        _record_reviewer_provenance(refused)
        return ReviewerText("", refused, FLOOR)
    floor = {
        "backend": "ollama",
        "host_id": "ollama:local",
        "model_used": DEFAULT_MODEL,
        "models_used": [DEFAULT_MODEL],
        "fallback_from": fallback_from,
        "warnings": warnings,
        **floor_listing,
    }
    _record_reviewer_provenance(floor)
    return ReviewerText(text, floor, FLOOR)


async def call_reviewer_model(prompt: str, model: str = DEFAULT_MODEL) -> str:
    """Run the local heterogeneous model in THIS process (not via the server's
    call_model tool, whose 30s timeout is shorter than gemma4's 43–70s budget).
    Localhost Ollama, OpenAI-compat — no paid API.

    Uses the ASYNC client + ``await`` so the ~40-70s model call does not block the
    event loop (this is an ``async def`` driven by ``asyncio.run``)."""
    from openai import AsyncOpenAI  # local import: only the runner process needs it

    # privacy='local' by nature: raises EndpointNotLocalError, before any
    # request, when the endpoint does not classify local.
    require_local_endpoint(OLLAMA_BASE_URL)
    client = AsyncOpenAI(
        base_url=OLLAMA_BASE_URL,
        api_key="ollama",
        # A redirect would re-send the thesis to an unclassified host.
        http_client=no_redirect_http_client(),
    )
    kwargs: dict[str, Any] = {
        "model": model,
        "messages": [{"role": "user", "content": prompt}],
        "max_tokens": int(os.getenv("UNITARES_DIALECTIC_REVIEW_MAX_TOKENS", "1024")),
        "temperature": 0.2,
    }
    # The supplied HTTP client has no destructor to close it; release its
    # connections on every exit, cancellation included.
    async with client:
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

    # Unreachable-by-construction, so state it as a check rather than trust it.
    # The early return above excludes `not verdict.agrees`, and every parse
    # failure yields agrees=False, so a verdict reaching here has judged. That
    # invariant is implicit and would break silently if the parser or the guard
    # above changed — and breaking it would DROP A REAL OBJECTION, the exact
    # failure class this module now exists to prevent. Fail loudly instead.
    assert verdict.judgment_formed, (
        "a verdict with no judgment reached the approval-downgrade path; "
        "the early return above should have made this impossible"
    )
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
            judgment_formed=verdict.judgment_formed,
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
        # The model DID judge here — it approved, just without naming terms.
        # That objection is real and must still be filed, so this path never
        # abstains. A literal True, not verdict.judgment_formed: this branch
        # DERIVES a protocol objection, so the judgment is formed here whatever
        # the input carried. The assert above is what keeps that honest.
        judgment_formed=True,
    )


class GovernanceLinkLost(ConnectionError):
    """The link's connection ended before it answered this call."""


class _GovernanceLink:
    """The reviewer's governance connection, built to outlive a governance
    restart.

    A reviewer that rejects stays alive for up to an hour to answer the paused
    agent, and a deploy restarts gov-mcp inside that window. mcp 2.x's
    streamable-HTTP transport runs in an anyio task group entered by whichever
    task called ``connect()``, and when the server goes away that group
    cancels its host task. The reviewer saw ``CancelledError``, a
    BaseException that no ``except Exception`` in the poll loop catches, and
    the client stayed dead after the server returned. Live, the reviewer
    exited status 1 and the paused agent's answer arrived minutes later with
    nobody left to read it (490c7cf515b89a6e, 85bd219ebb5b9ec9). Closing the
    client does not free its host task either: after ``disconnect()`` the
    group's scope still cancelled that task at its next await, so a
    connection must never be opened in the reviewer's own task at all.

    So each connection lives in a worker task of its own and every call runs
    there. A lost transport ends the worker, not the reviewer: the pending
    call fails with ``GovernanceLinkLost``, an ordinary Exception that the
    poll loop already treats as a transient read, and the next call opens a
    fresh connection. That connection proves it is the same reviewer process
    with the same-process continuity token (``identity(agent_uuid,
    continuity_token, resume=True)``). It never onboards, so a restart cannot
    mint a second reviewer. A cancellation of the reviewer itself still
    propagates.
    """

    def __init__(
        self,
        governance_url: str,
        client_factory: Any,
        *,
        agent_uuid: Optional[str] = None,
        client_session_id: Optional[str] = None,
        continuity_token: Optional[str] = None,
    ) -> None:
        self._url = governance_url
        self._factory = client_factory
        self._identity: dict[str, Optional[str]] = {
            "agent_uuid": agent_uuid,
            "client_session_id": client_session_id,
            "continuity_token": continuity_token,
        }
        self._worker: Optional[asyncio.Task] = None
        self._requests: Optional[asyncio.Queue] = None
        self._connections = 0

    @property
    def agent_uuid(self) -> Optional[str]:
        return self._identity["agent_uuid"]

    def _remember(self, client: Any) -> None:
        for key in self._identity:
            value = getattr(client, key, None)
            if isinstance(value, str) and value:
                self._identity[key] = value

    async def _serve(self, requests: asyncio.Queue) -> None:
        client = self._factory(self._url)
        try:
            await client.connect()
            for key, value in self._identity.items():
                if value:
                    setattr(client, key, value)
            if self._identity["agent_uuid"] and self._identity["continuity_token"]:
                try:
                    await client.identity(
                        agent_uuid=self._identity["agent_uuid"],
                        continuity_token=self._identity["continuity_token"],
                        resume=True,
                    )
                except Exception as exc:  # noqa: BLE001 — the session binding may still carry
                    logger.warning(
                        "Dialectic reviewer rebind failed; continuing on the "
                        "existing session binding: %r",
                        exc,
                    )
                self._remember(client)
            while True:
                fn, reply = await requests.get()
                if reply.done():
                    continue
                try:
                    result = await fn(client)
                except Exception as exc:  # noqa: BLE001 — returned to the caller
                    if not reply.done():
                        reply.set_exception(exc)
                    continue
                self._remember(client)
                if not reply.done():
                    reply.set_result(result)
        finally:
            await client.disconnect()

    async def run(self, fn: Any) -> Any:
        """Await ``fn(client)`` on the link's live connection."""
        if self._worker is None or self._worker.done():
            if self._worker is not None:
                ended = self._worker
                cause = None if ended.cancelled() else ended.exception()
                logger.warning(
                    "Dialectic reviewer reconnecting to governance after a "
                    "lost connection (connection %d; previous ended by %s)",
                    self._connections + 1,
                    repr(cause) if cause else "transport cancellation",
                )
            self._connections += 1
            self._requests = asyncio.Queue()
            self._worker = asyncio.create_task(self._serve(self._requests))
        worker = self._worker
        reply = asyncio.get_running_loop().create_future()
        await self._requests.put((fn, reply))
        try:
            await asyncio.wait({reply, worker}, return_when=asyncio.FIRST_COMPLETED)
        except asyncio.CancelledError:
            reply.cancel()
            raise
        if reply.done():
            return reply.result()
        reply.cancel()
        cause: Optional[BaseException] = None
        if not worker.cancelled():
            cause = worker.exception()
        raise GovernanceLinkLost(
            "governance connection ended before the call completed"
            + (f": {cause!r}" if cause else " (transport cancelled)")
        )

    async def call_tool(self, name: str, args: dict) -> Any:
        return await self.run(lambda client: client.call_tool(name, args))

    async def close(self) -> None:
        worker, self._worker = self._worker, None
        if worker is None:
            return
        if not worker.done():
            worker.cancel()
        await asyncio.gather(worker, return_exceptions=True)


def _is_transport_loss(exc: BaseException) -> bool:
    """True only for errors that say the call may never have arrived.

    A tool that answered ``success: false`` raises GovernanceToolRefused: an
    answer, never retried. Every other SDK connection error, a connection the
    link lost, a timeout and a 503 mean the filing may not have landed.
    """
    if isinstance(exc, (GovernanceLinkLost, TimeoutError, asyncio.TimeoutError,
                        ConnectionError, OSError)):
        return True
    try:
        from unitares_sdk.errors import (  # type: ignore
            GovernanceConnectionError,
            GovernanceTimeoutError,
            GovernanceToolRefused,
            GovernanceUnavailableError,
        )
    except ImportError:
        return False
    if isinstance(exc, GovernanceToolRefused):
        return False
    return isinstance(
        exc,
        (GovernanceConnectionError, GovernanceTimeoutError, GovernanceUnavailableError),
    )


async def continue_after_disagreement(
    client: Any,
    thesis: Thesis,
    initial_verdict: Verdict,
    *,
    paused_agent_id: Optional[str],
    reviewer_agent_id: Optional[str],
    pinned_host: Optional[str] = None,
) -> Verdict:
    """Run bounded objection → response → reconsideration rounds.

    The wall-clock budget includes polling and every follow-up model call. This
    leaves the orchestrator's process deadline as a separate hard backstop.

    ``pinned_host``: the listed host whose judgment is standing. Every
    reconsideration goes to it, and if it fails the floor answers without
    approval authority; the list never restarts. Otherwise one host could
    object, time out on the reconsideration, and the next host could approve a
    thesis the first never accepted (design 2.2). With no listed host pinned
    yet, the first listed host to answer is pinned from then on.
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
    # A formed verdict whose filing was cut off, with the paused response it
    # answers. It is re-filed as formed, never re-judged: a second model call
    # can reach a different verdict, and a dropped connection must not be able
    # to change a governance outcome.
    unfiled: Optional[tuple[dict[str, Any], Verdict]] = None

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
        paused_response = find_pending_paused_response(
            session_data,
            paused_agent_id=paused_agent_id,
            reviewer_agent_id=reviewer_agent_id,
        )
        if unfiled is not None and paused_response != unfiled[0]:
            # The response it answered is no longer the pending one, so the
            # cut-off filing landed after all: it is the standing verdict. This
            # runs before the terminal check because a landed approval is
            # exactly what makes the session terminal.
            current_verdict, unfiled = unfiled[1], None
            if current_verdict.agrees:
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

        if paused_response is None:
            await asyncio.sleep(min(poll_s, max(0.0, deadline - time.monotonic())))
            continue

        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return current_verdict
        if unfiled is not None:
            next_verdict = unfiled[1]
        else:
            prompt = build_continuation_prompt(
                thesis, current_verdict, paused_response, synthesis_round
            )
            try:
                model_text = await asyncio.wait_for(
                    obtain_reviewer_text(prompt, pinned=pinned_host), timeout=remaining
                )
            except asyncio.TimeoutError:
                return current_verdict
            except Exception as exc:  # noqa: BLE001 — preserve the standing rejection
                logger.warning("Dialectic continuation model failed: %r", exc)
                return current_verdict
            next_verdict = withhold_fallback_approval(
                _verdict_with_ratified_conditions(
                    parse_reviewer_verdict(model_text), paused_response, current_verdict
                ),
                _provenance_of(model_text),
            )
            if pinned_host is None:
                pinned_host = _listed_host_of(model_text)
        if not next_verdict.judgment_formed:
            # Same rule as the initial verdict. Filing this would burn a
            # synthesis round and overwrite a REASONED standing rejection with
            # an empty one — the paused agent would lose the objection it was
            # answering. Preserve it, exactly as the timeout and model-failure
            # branches above already do. A withheld fallback approval lands
            # here too: the standing rejection outranks a substitute approval.
            logger.warning(
                "Dialectic continuation produced no accepted judgment (%s); "
                "preserving the standing rejection rather than filing over it",
                next_verdict.reasoning,
            )
            return current_verdict
        try:
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
        except Exception as exc:  # noqa: BLE001 — classified below
            if not _is_transport_loss(exc):
                # A refusal (the SDK raises for success=false) is an answer,
                # not an outage; retrying it would re-file until the deadline.
                logger.warning("Dialectic continuation synthesis was refused: %r", exc)
                return current_verdict
            # Whether the write landed is unknown, and the next read settles
            # it: a synthesis that landed now follows the paused response, so
            # nothing is pending; one that did not leaves the same response
            # pending, and this same verdict is filed again. Neither case
            # files twice or judges twice.
            logger.warning("Dialectic continuation synthesis did not complete: %r", exc)
            unfiled = (paused_response, next_verdict)
            await asyncio.sleep(min(poll_s, max(0.0, deadline - time.monotonic())))
            continue
        unfiled = None
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
    verdict = parse_reviewer_verdict(reviewer_text)
    # The reply whose verdict is filed; its own provenance decides approval.
    verdict_text = reviewer_text
    for _ in range(_VERDICT_REPAIR_ATTEMPTS):
        if verdict.judgment_formed:
            break
        logger.warning(
            "Dialectic reviewer got no parseable judgment on session %s; "
            "re-asking once for the same verdict in the required shape before "
            "abstaining.",
            thesis.session_id,
        )
        try:
            # The repair restates a reply, so it goes to the host that wrote
            # it, never down the list.
            reviewer_text = await obtain_reviewer_text(
                build_repair_prompt(thesis, reviewer_text),
                pinned=getattr(reviewer_text, "host_key", None),
            )
        except Exception as exc:  # noqa: BLE001 — a failed repair just abstains
            logger.warning("Dialectic reviewer repair attempt failed: %r", exc)
            break
        repaired = parse_reviewer_verdict(reviewer_text)
        # A REPAIR MAY CONFIRM AN OBJECTION. IT MAY NEVER MANUFACTURE AN
        # APPROVAL.
        #
        # build_repair_prompt asks the model not to change its position, but
        # asking is not enforcing, and the reply being restated was UNPARSEABLE
        # by construction -- so there is no way to check the position held. A
        # first reply that rejected the thesis in prose, followed by a
        # parseable `agrees: true`, would file an approval that no one can
        # verify was ever the model's judgment, and an approval can resolve the
        # session and release the paused agent.
        #
        # Approval is the one direction that must never rest on an
        # unverifiable restatement, which is the same rule
        # parse_reviewer_verdict already applies to a failed parse: "a reviewer
        # that cannot form a judgment must not silently approve". Keep the
        # original non-judgment and abstain; the slot stays open for a reviewer
        # that can judge. The cost is a genuine approval that merely botched
        # its format, and that is the correct direction to lose one.
        #
        # Found by independent review of this PR (codex, 2026-09-19): the
        # docstring claimed a property only the prompt provided.
        if repaired.judgment_formed and repaired.agrees:
            logger.warning(
                "Dialectic reviewer repair returned an APPROVAL on session %s; "
                "discarding it and abstaining. The reply it restates was "
                "unparseable, so the position cannot be confirmed unchanged.",
                thesis.session_id,
            )
            break
        verdict = repaired
        verdict_text = reviewer_text
    # The provenance of the reply whose verdict is filed, carried by that reply,
    # so a later call that raised cannot leave its own record attached here.
    provenance = _provenance_of(verdict_text)
    verdict = withhold_fallback_approval(verdict, provenance)

    # ABSTAIN rather than file a non-judgment.
    #
    # A reviewer that could not form a judgment has not reviewed anything.
    # Filing here would claim the open reviewer slot and record a BINDING
    # rejection whose reasoning is empty: it blocks the paused agent, tells it
    # nothing it can act on, is indistinguishable on the record from a reasoned
    # rejection, and — because the slot is now taken — locks out a reviewer that
    # COULD judge. Fail-closed must mean "no approval", never "silent
    # rejection"; those are different verdicts and only one of them is honest
    # about what happened.
    #
    # This is the posture the IN-PROCESS synthetic reviewer already takes
    # ("the fully-degraded case ... the session stays open rather than
    # fabricating one", _synthetic_review_approves in
    # src/mcp_handlers/dialectic/handlers.py). The orchestrated path simply
    # never had it.
    #
    # Live instance, 2026-09-19, session 99ff6f25a310d23e on PR #2316: the
    # codex backend was unavailable, the fallback gemma4 returned no parseable
    # verdict, and the PR acquired a reasonless standing rejection — while an
    # independent reviewer holding a reproduced counterexample was refused the
    # slot four minutes later. The operator had asked for a non-evasive review
    # and got a blocking non-answer.
    if not verdict.judgment_formed:
        logger.warning(
            "Dialectic reviewer ABSTAINING on session %s: %s produced no "
            "accepted judgment (%s). The server will record the abstention "
            "without assuming anything about reviewer-slot ownership.",
            thesis.session_id,
            _reviewer_audit_text(provenance),
            verdict.reasoning,
        )

    # Every governance call runs on the link, never on a client opened in
    # this task: see _GovernanceLink for why a restart would otherwise kill
    # the reviewer mid-review.
    client = _GovernanceLink(governance_url, GovernanceClient)
    try:
        await client.run(
            lambda c: c.onboard(
                name=REVIEWER_NAME,  # required first arg of GovernanceClient.onboard
                force_new=True,
                parent_agent_id=parent_agent_id,
                spawn_reason=SPAWN_REASON,
                model_type=_reviewer_model_type(provenance),
            )
        )
        # Claim the open reviewer slot as first-responder. The bare submit_*
        # handlers are register=False; the public MCP surface is the `dialectic`
        # umbrella tool (action='antithesis'/'synthesis'). (live-found 2026-06-23)
        antithesis_result = await client.call_tool(
            "dialectic",
            {
                "action": "antithesis",
                "session_id": thesis.session_id,
                "reasoning": verdict.reasoning,
                "judgment_formed": verdict.judgment_formed,
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
        if not verdict.judgment_formed:
            if isinstance(antithesis_result, dict):
                if antithesis_result.get("abstained") is True:
                    slot_state = antithesis_result.get("reviewer_slot_open")
                    verdict.reviewer_slot_open = (
                        slot_state if isinstance(slot_state, bool) else None
                    )
                else:
                    # A legacy/partial server may ignore judgment_formed and
                    # file a normal verdict. Do not report a fabricated open
                    # slot when the server did not acknowledge abstention.
                    verdict.reviewer_slot_open = None
            slot_state = (
                "the reviewer slot remains OPEN"
                if verdict.reviewer_slot_open is True
                else (
                    "the existing reviewer assignment remains unchanged"
                    if verdict.reviewer_slot_open is False
                    else "the server did not provide a reliable reviewer-slot state"
                )
            )
            logger.warning(
                "Dialectic reviewer abstention recorded for session %s; %s",
                thesis.session_id,
                slot_state,
            )
            return verdict
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
        try:
            await client.run(
                lambda c: c.checkin(
                    response_text=(
                        f"dialectic review submitted: agrees={verdict.agrees}"
                        + (" (degraded fallback)" if verdict.degraded else "")
                        + f"; {_reviewer_audit_text(provenance)}"
                    ),
                    complexity=0.4,
                    confidence=0.6 if not verdict.degraded else 0.3,
                    # The VERDICT is model-produced, but this check-in text is an
                    # f-string over verdict fields, so the substrate composed the row.
                    epistemic_class="substrate_interpretation",
                )
            )
        except Exception as exc:  # noqa: BLE001 — verdict is already durable
            # A check-in is diagnostic evidence, not part of the dialectic
            # authority path.  One live reviewer exited non-zero here after its
            # rejection was committed, 30 minutes before the paused response;
            # the promised continuation therefore died and the operator had to
            # facilitate.  Preserve the durable verdict and keep the reviewer
            # alive to reconsider instead of making telemetry a lifecycle gate.
            logger.warning(
                "Dialectic reviewer check-in failed after verdict persistence; "
                "continuing the review: %r",
                exc,
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
                reviewer_agent_id=client.agent_uuid,
                pinned_host=_listed_host_of(verdict_text),
            )
        return verdict
    finally:
        await client.close()


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
    if not verdict.judgment_formed:
        # Distinct from 0 (reviewed) and from 1 (crashed): the reviewer ran,
        # reached the model, and could not form a judgment. "The producer ran
        # and found nothing" and "the producer never ran" are different
        # findings and must not share an exit code.
        print(
            "reviewer ABSTAINED: no accepted judgment; no verdict filed; "
            + (
                "the reviewer slot remains OPEN"
                if verdict.reviewer_slot_open is True
                else (
                    "the existing reviewer assignment remains unchanged"
                    if verdict.reviewer_slot_open is False
                    else "the server did not provide a reliable reviewer-slot state"
                )
            ),
            flush=True,
        )
        return 3
    print(f"reviewer done: agrees={verdict.agrees} degraded={verdict.degraded}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
