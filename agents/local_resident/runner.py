"""Identity lifecycle + in-process local model call for a fleet-member resident.

The contract a resident gets:

    onboard as its OWN identity -> do its job with the governance tools ->
    land one real check-in -> exit and be reaped

and the contract it must honour: the job is a coroutine that receives a
connected client, and whatever it returns becomes the check-in summary. A
resident that cannot describe what it did in one line has probably not done one
job.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Optional

# Localhost Ollama's OpenAI-compatible endpoint. Not the server's `call_model`
# tool: that carries a 30s timeout, and a small local model routinely needs
# 40-70s for a real answer. The reviewer learned this the expensive way; a
# resident that routes its own thinking through call_model will look flaky
# rather than slow.
OLLAMA_BASE_URL = os.getenv("UNITARES_OLLAMA_BASE_URL", "http://localhost:11434/v1")
DEFAULT_MODEL = os.getenv("UNITARES_LLM_MODEL", "gemma4:latest")


@dataclass(frozen=True)
class ResidentSpec:
    """What governance needs to know about a resident before it acts."""

    name: str
    # LINEAGE_SPAWN_REASONS is classification-only server-side, but the value is
    # what a later reader uses to tell a real spawn from co-location, so it is
    # required rather than defaulted.
    spawn_reason: str
    max_tokens: int = 1024
    temperature: float = 0.2


# A thinking-style local model (gemma4, deepseek-r1) wraps its reasoning in a
# <think> block and, under Ollama's OpenAI-compat adapter, may put the whole
# response in a non-standard `reasoning` field with `content` left empty. Both
# the reviewer and the server's call_model already handle this; the first draft
# of this extraction did not, and the resident's first live run returned "model
# returned nothing" against a model that had in fact answered.
_THINK_BLOCK = re.compile(r"<think>.*?</think>", re.DOTALL | re.IGNORECASE)


async def call_local_model(
    prompt: str,
    *,
    model: Optional[str] = None,
    max_tokens: int = 1024,
    temperature: float = 0.2,
) -> str:
    """Run a local model IN this process. No paid API, no server round-trip.

    Async client with ``await`` so a 40-70s generation does not block the event
    loop the rest of the resident runs on.

    Returns the model's answer with any ``<think>`` block stripped, falling back
    to the ``reasoning`` trace when the model spent its whole budget thinking and
    emitted no final answer. Returning "" in that case would report silence from
    a model that produced plenty — a resident should not mistake a truncated
    thought for no thought.
    """
    from openai import AsyncOpenAI  # local import: only a runner process needs it

    client = AsyncOpenAI(base_url=OLLAMA_BASE_URL, api_key="ollama")
    resp = await client.chat.completions.create(
        model=model or DEFAULT_MODEL,
        messages=[{"role": "user", "content": prompt}],
        max_tokens=max_tokens,
        temperature=temperature,
    )
    return extract_model_text(resp.choices[0].message)


def extract_model_text(message: Any) -> str:
    """Pull usable text off a completion message. Pure — testable without Ollama."""
    text = _THINK_BLOCK.sub("", getattr(message, "content", None) or "").strip()
    if text:
        return text

    reasoning = _THINK_BLOCK.sub(
        "", getattr(message, "reasoning", None) or ""
    ).strip()
    if not reasoning:
        return ""
    # Flagged, not laundered: a truncated thought is weaker evidence than an
    # answer, and a caller storing it should be able to see which it got.
    return (
        "[Model hit its token limit before emitting a final answer; this is the "
        "thinking trace it produced.]\n\n" + reasoning
    )


async def run_local_resident(
    spec: ResidentSpec,
    job: Callable[[Any], Awaitable[str]],
    *,
    governance_url: Optional[str] = None,
    parent_agent_id: Optional[str] = None,
    complexity: float = 0.4,
    confidence: float = 0.6,
) -> str:
    """Onboard, run ``job``, check in with its summary, exit. Returns the summary.

    ``parent_agent_id`` is a CANDIDATE declaration: the orchestrator provisions
    ``UNITARES_PARENT_AGENT_ID`` into a spawned child's environment, and the
    child declares it. Nothing here resumes an identity — co-location is not
    lineage, and a resident that inherited no work should pass None rather than
    claim a parent because one happened to be in the environment.
    """
    from unitares_sdk.client import GovernanceClient  # type: ignore

    url = governance_url or os.getenv("UNITARES_GOVERNANCE_URL") or os.getenv(
        "GOVERNANCE_URL", ""
    )
    parent = parent_agent_id or os.getenv("UNITARES_PARENT_AGENT_ID") or None

    client = GovernanceClient(url)
    await client.connect()
    try:
        await client.onboard(
            name=spec.name,
            force_new=True,
            parent_agent_id=parent,
            spawn_reason=spec.spawn_reason,
        )
        summary = await job(client)
        # A real check-in before exit, per the subagent-onboarding discipline —
        # an identity that never reports is indistinguishable from a leak.
        await client.checkin(
            response_text=summary,
            complexity=complexity,
            confidence=confidence,
        )
        return summary
    finally:
        await client.disconnect()
