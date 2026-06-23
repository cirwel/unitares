"""Orchestrated dialectic reviewer — a standalone, independently-accountable
reviewer process.

The agent-orchestrator spawns this as a supervised, lease-bound child when a
dialectic session needs a reviewer. Unlike the in-process synthetic path
(`handle_llm_assisted_dialectic`, which hardcodes ``agrees=True`` and borrows the
paused agent's api_key), this process:

  * onboards as its OWN governance identity (strict-identity compliant),
  * runs a heterogeneous LOCAL model (gemma4 via Ollama — no paid API) IN its own
    process to form a *genuine* verdict that may DISAGREE,
  * submits that verdict through the ordinary dialectic protocol tools, and
  * exits (the orchestrator reaps it and releases its lease).

Design: docs/proposals/orchestrated-dialectic-reviewer-v0.md

The verdict-derivation (`parse_reviewer_verdict`) and prompt-construction
(`build_review_prompt`) are PURE functions so the independence-critical behavior
— that a disagreeing model produces ``agrees=False`` — is unit-tested without a
network or a model.
"""
from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field
from typing import Any, Optional

# gemma4 hides its answer behind a <think> block under thinking mode; strip it
# before JSON extraction (mirrors llm_delegation._wants_reasoning_effort_none).
_THINK_BLOCK = re.compile(r"<think>.*?</think>", re.DOTALL | re.IGNORECASE)
# The model is asked for strict JSON, but local models fence it or add prose.
_JSON_OBJECT = re.compile(r"\{.*\}", re.DOTALL)

DEFAULT_MODEL = os.getenv("UNITARES_LLM_MODEL", "gemma4:latest")
OLLAMA_BASE_URL = os.getenv("UNITARES_OLLAMA_BASE_URL", "http://localhost:11434/v1")
SPAWN_REASON = "dialectic_reviewer"


@dataclass
class Thesis:
    """What the paused agent claimed — passed in the spawn payload, not read over
    MCP (get_dialectic_session is register=False)."""

    session_id: str
    root_cause: str = ""
    proposed_conditions: list[str] = field(default_factory=list)
    reasoning: str = ""
    situation: str = ""  # free-text context about why the agent paused

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
        return cls(
            session_id=sid,
            root_cause=env.get("DIALECTIC_THESIS_ROOT_CAUSE", ""),
            proposed_conditions=conditions,
            reasoning=env.get("DIALECTIC_THESIS_REASONING", ""),
            situation=env.get("DIALECTIC_THESIS_SITUATION", ""),
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
    return (
        "You are an INDEPENDENT reviewer in a dialectic governance process. A paused "
        "AI agent has proposed conditions under which it should resume work. Your job "
        "is to genuinely evaluate the proposal — NOT to rubber-stamp it. Disagreeing is "
        "a valid, expected outcome when the root-cause analysis is shallow, the "
        "conditions don't address the root cause, or the agent is rationalizing.\n\n"
        f"PAUSED AGENT'S SITUATION:\n{thesis.situation or '(not provided)'}\n\n"
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


# --------------------------------------------------------------------------- #
# Async wiring (the impure shell). Kept thin; the testable logic is above.
# --------------------------------------------------------------------------- #
async def call_reviewer_model(prompt: str, model: str = DEFAULT_MODEL) -> str:
    """Run the local heterogeneous model in THIS process (not via the server's
    call_model tool, whose 30s timeout is shorter than gemma4's 43–70s budget).
    Localhost Ollama, OpenAI-compat — no paid API."""
    from openai import OpenAI  # local import: only the runner process needs it

    client = OpenAI(base_url=OLLAMA_BASE_URL, api_key="ollama")
    kwargs: dict[str, Any] = {
        "model": model,
        "messages": [{"role": "user", "content": prompt}],
        "max_tokens": int(os.getenv("UNITARES_DIALECTIC_REVIEW_MAX_TOKENS", "1024")),
        "temperature": 0.2,
    }
    resp = client.chat.completions.create(**kwargs)
    return resp.choices[0].message.content or ""


async def run(thesis: Thesis, governance_url: str, parent_agent_id: Optional[str]) -> Verdict:
    """Onboard → model → claim slot + submit. Returns the Verdict for logging/tests."""
    from unitares_sdk.client import GovernanceClient  # type: ignore

    verdict = parse_reviewer_verdict(await call_reviewer_model(build_review_prompt(thesis)))

    client = GovernanceClient(governance_url)
    await client.connect()
    try:
        await client.onboard(
            force_new=True,
            parent_agent_id=parent_agent_id,
            spawn_reason=SPAWN_REASON,
        )
        # Claim the open reviewer slot as first-responder (handlers.py:677/691).
        await client.call_tool(
            "submit_antithesis",
            {"session_id": thesis.session_id, "reasoning": verdict.reasoning},
        )
        # Submit the model-derived verdict — agrees may be False (the whole point).
        await client.call_tool(
            "submit_synthesis",
            {
                "session_id": thesis.session_id,
                "agrees": verdict.agrees,
                "proposed_conditions": verdict.proposed_conditions,
                "root_cause": verdict.root_cause,
                "reasoning": verdict.reasoning,
            },
        )
        # A real check-in before exit (subagent-onboarding discipline).
        # SDK checkin() maps to the server's process_agent_update.
        await client.checkin(
            response_text=f"dialectic review complete: agrees={verdict.agrees}"
            + (" (degraded fallback)" if verdict.degraded else ""),
            complexity=0.4,
            confidence=0.6 if not verdict.degraded else 0.3,
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
