"""A second local-model fleet member, on a job that is not dialectic.

The point of this resident is to answer one question: does the pattern proved by
``agents/dialectic_reviewer`` generalise, or does it work because dialectic
happens to be an unusually well-shaped task for a small model? So the job shares
as little as possible with review — no thesis, no verdict, no adversarial
framing, no structured JSON contract to parse.

The job: ask governance which agents look anomalous, have a local model say what
the pattern is in one paragraph, and record that where other agents can find it.

**The wire is the constraint, and it bit this resident twice.** The first draft
called ``get_tool_usage_stats``; the second called ``admin(action='tool_usage')``.
Both came back ``Unknown tool`` from the live server, because neither is in the
advertised lite set and the ``/mcp/`` transport dispatches only what it
advertises. A resident reaches the fleet over the same wire an agent does, so it
can only do jobs the advertised surface supports — which today does not include
reading the fleet's own tool telemetry. The job here was chosen to fit the wire,
not the other way round.

Two things this resident does NOT demonstrate, stated because the distinction is
easy to lose:

  * It does not show a local model navigating the tool surface. The Python here
    chooses every tool; the model only produces prose. That is equally true of
    the reviewer. "Local model as a reasoner inside a scripted lifecycle" and
    "local model as an agent choosing its own tools" are different capabilities,
    and only the first is exercised by this shape.
  * It does not prove usefulness. It proves the lifecycle carries a second job.
    Whether the paragraph is worth reading is a separate question, which is why
    writing is opt-in rather than the default.
"""

from __future__ import annotations

import json
import os
from typing import Any

from agents.local_resident import ResidentSpec, call_local_model, run_local_resident

SCRIBE_NAME = "TriageScribe"
SPAWN_REASON = "resident_cycle"

# A reading taken at a moment, so `ephemeral` is the honest tag: it carries a
# timestamp rather than a resolution condition, and without the tag every later
# KG sweep re-reads it as unfinished work. This is a claim about the content's
# shelf life, never about the writer's.
FINDING_TAGS = ["ephemeral", "triage", "anomalies"]

PROMPT = """You are triaging a fleet of AI agents governed by a behavioural state model.

Governance reports these anomalous agents:

{anomalies}

In ONE paragraph of plain prose, say what the pattern is: whether these look
like related failures or unrelated ones, and what an operator should check
first. Do not use bullet points. Do not invent agents or numbers that are not
listed above. If the data does not support a conclusion, say that plainly
instead of guessing."""


def format_anomalies(payload: dict[str, Any]) -> str:
    """Render the anomaly payload for the prompt. Pure — testable without a model."""
    anomalies = payload.get("anomalies") or payload.get("agents") or []
    if not anomalies:
        return "(governance reports no anomalous agents in this window)"

    lines = []
    for item in anomalies[:15]:
        if not isinstance(item, dict):
            lines.append(str(item))
            continue
        label = (
            item.get("agent_name")
            or item.get("agent_id")
            or item.get("name")
            or "unknown agent"
        )
        # `observe(action='anomalies')` returns type / severity / description —
        # a populated, specific explanation like "Risk increased from 0.49 to
        # 0.81". The first version of this function looked for `reasons` /
        # `anomaly_types` / `reason`, none of which the payload has, so every
        # row rendered "flagged, no reason given" and the model correctly
        # concluded the fleet's own flags were uninformative. They are not; the
        # formatter was throwing the information away before the model saw it.
        detail = " ".join(
            part
            for part in (
                item.get("type"),
                f"({item.get('severity')})" if item.get("severity") else None,
                item.get("description"),
            )
            if part
        ).strip()
        if item.get("stale"):
            # Worth carrying: a stale anomaly says the agent stopped reporting,
            # which changes what an operator should do about it.
            detail = f"{detail} [stale]" if detail else "[stale]"
        lines.append(f"{label}: {detail or 'flagged, no detail in payload'}")
    return "\n".join(lines)


async def _job(client: Any) -> str:
    dry_run = os.getenv("UNITARES_SCRIBE_DRY_RUN", "1").strip().lower() not in (
        "0", "false", "no",
    )

    raw = await client.call_tool("observe", {"action": "anomalies"})
    payload = raw if isinstance(raw, dict) else json.loads(str(raw))

    # Nothing to triage is not a finding. Without this gate a healthy fleet
    # produces a daily KG entry saying there is nothing to report, forever —
    # the mirror image of the Chronicler problem that motivated scheduling
    # this resident at all, and squarely against the repo's own write
    # discipline ("store when a future agent would search for this and not
    # already find it"). Returning here also skips the model call entirely,
    # so a quiet day costs no Ollama load.
    if not anomaly_items(payload):
        return "triage scribe: no anomalies in window; recorded no finding"

    paragraph = (
        await call_local_model(
            PROMPT.format(anomalies=format_anomalies(payload)),
            # A thinking model spends most of its budget before the answer;
            # 600 truncated gemma4 mid-thought on the first live run.
            max_tokens=int(os.getenv("UNITARES_SCRIBE_MAX_TOKENS", "1600")),
        )
    ).strip()

    if not paragraph:
        return "triage scribe: model returned nothing; recorded no finding"

    if dry_run:
        print("--- DRY RUN, would store ---", flush=True)
        print(paragraph, flush=True)
        return "triage scribe: characterised fleet anomalies (dry run, nothing written)"

    # `knowledge(action='store')` is the live write path and is advertised.
    # Once the workflow aliases for the knowledge write actions land,
    # `store_finding` is the friendlier name for exactly this call.
    await client.call_tool(
        "knowledge",
        {
            "action": "store",
            "summary": "Fleet anomaly triage",
            "details": paragraph,
            "discovery_type": "observation",
            "tags": FINDING_TAGS,
        },
    )
    return "triage scribe: stored a fleet anomaly triage finding"


def main() -> int:
    import asyncio

    spec = ResidentSpec(name=SCRIBE_NAME, spawn_reason=SPAWN_REASON)
    try:
        summary = asyncio.run(run_local_resident(spec, _job))
    except Exception as exc:  # noqa: BLE001 — a resident crash must be loud
        print(f"FATAL: triage scribe failed: {exc!r}", flush=True)
        return 1
    print(summary, flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
