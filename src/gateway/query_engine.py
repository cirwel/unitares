"""Natural language intent classification — routes questions to the right gateway tool.

Uses governance's call_model tool for LLM routing, with keyword fallback.
"""

from __future__ import annotations

import logging
import re
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .client import GovernanceMCPClient

logger = logging.getLogger("gateway.query")

# Intent → (tool_name, argument builder)
INTENTS = {
    "status": "status",
    "checkin": "checkin",
    "search": "search",
    "note": "note",
    "help": "help",
}

# Keyword patterns for fallback classification
KEYWORD_PATTERNS = [
    (r"\b(status|eisv|coherence|state|basin|verdict|health)\b", "status"),
    (r"\b(check.?in|report|update|worked on|did|finished|completed)\b", "checkin"),
    (r"\b(search|find|look.?up|query|knowledge|graph|discover)\b", "search"),
    (r"\b(note|save|record|remember|log|leave)\b", "note"),
    (r"\b(help|tools|commands|how|what can)\b", "help"),
]

ROUTING_PROMPT = """You are an intent classifier for a governance system. Given a user question, classify it into exactly one intent.

Available intents:
- status: Questions about agent state, EISV, coherence, basin, verdict, health
- checkin: Reporting work done, checking in, getting a verdict on progress
- search: Looking up knowledge, findings, discoveries in the knowledge graph
- note: Saving a note, discovery, or observation
- help: Questions about what tools are available or how to use the system

Respond with ONLY the intent name (one word). If unsure, respond "search".

User question: {question}

Intent:"""


async def classify_intent(
    question: str, client: GovernanceMCPClient, client_session_id: str | None = None
) -> str:
    """Classify a natural language question into a gateway intent.

    Tries LLM classification via call_model first, falls back to keyword matching.
    """
    # Try LLM classification
    try:
        args = {
            "prompt": ROUTING_PROMPT.format(question=question),
            "max_tokens": 10,
        }
        # call_model needs a bound caller; without the id it is refused and the
        # keyword fallback below silently takes over.
        if client_session_id:
            args["client_session_id"] = client_session_id
        result = await client.call_tool("call_model", args)
        if isinstance(result, dict):
            intent = (result.get("response") or result.get("text") or "").strip().lower()
        else:
            intent = str(result).strip().lower()

        # Validate it's a known intent
        if intent in INTENTS:
            logger.debug("LLM classified '%s' → %s", question[:50], intent)
            return intent
        logger.debug("LLM returned unknown intent '%s', falling back to keywords", intent)
    except Exception as exc:
        logger.debug("LLM classification failed (%s), using keyword fallback", exc)

    return _keyword_classify(question)


def _keyword_classify(question: str) -> str:
    """Fallback: classify by keyword matching."""
    q = question.lower()
    for pattern, intent in KEYWORD_PATTERNS:
        if re.search(pattern, q):
            logger.debug("Keyword classified '%s' → %s", question[:50], intent)
            return intent
    # Default to search
    return "search"


async def route_query(
    question: str, client: GovernanceMCPClient, client_session_id: str | None = None
) -> dict:
    """Route a natural language question to the appropriate tool call.

    Returns (tool_name, arguments) tuple for the gateway to execute.
    """
    intent = await classify_intent(question, client, client_session_id=client_session_id)

    if intent == "status":
        return {"tool": "status", "args": {}}
    elif intent == "checkin":
        # Can't auto-fill summary from a question, use call_model to generate one
        return {"tool": "checkin", "args": {"summary": question}}
    elif intent == "search":
        return {"tool": "search", "args": {"query": question}}
    elif intent == "note":
        return {"tool": "note", "args": {"content": question}}
    elif intent == "help":
        return {"tool": "help", "args": {}}
    else:
        return {"tool": "search", "args": {"query": question}}
