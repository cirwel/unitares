"""Transform governance responses into simple uniform envelopes.

Every gateway response uses: {"ok": bool, "summary": str, "data": dict}
Errors use:                   {"ok": false, "summary": str, "error": str}
"""

from __future__ import annotations

from typing import Any


def ok(summary: str, data: Any = None) -> dict:
    """Success envelope."""
    return {"ok": True, "summary": summary, "data": data or {}}


def err(summary: str, error: str | None = None) -> dict:
    """Error envelope."""
    return {"ok": False, "summary": summary, "error": error or summary}


def simplify_status(raw: dict) -> dict:
    """Simplify get_governance_metrics response."""
    if not isinstance(raw, dict):
        return ok("Status retrieved", {"raw": raw})

    # Extract EISV
    eisv = raw.get("eisv") or raw.get("state", {}).get("eisv") or {}
    coherence = raw.get("coherence") or raw.get("state", {}).get("coherence")
    basin = raw.get("basin") or raw.get("state", {}).get("basin")
    risk = raw.get("risk") or raw.get("state", {}).get("risk")
    verdict = raw.get("verdict") or raw.get("action")
    agent_id = raw.get("agent_id") or raw.get("resolved_agent_id")

    # Build compact state
    state = {}
    if eisv:
        e = eisv.get("E") or eisv.get("energy")
        i = eisv.get("I") or eisv.get("information_integrity")
        s = eisv.get("S") or eisv.get("entropy")
        v = eisv.get("V") or eisv.get("void")
        state["eisv"] = {"E": e, "I": i, "S": s, "V": v}

    if coherence is not None:
        state["coherence"] = _round(coherence)
    if basin:
        state["basin"] = basin
    if risk is not None:
        state["risk"] = _round(risk)
    if verdict:
        state["verdict"] = verdict
    if agent_id:
        state["agent_id"] = agent_id

    # Summary line
    parts = []
    if verdict:
        parts.append(verdict)
    if coherence is not None:
        parts.append(f"coherence={_round(coherence)}")
    if basin:
        parts.append(f"basin={basin}")
    summary = " | ".join(parts) if parts else "Status retrieved"

    return ok(summary, state)


def simplify_checkin(raw: dict) -> dict:
    """Simplify process_agent_update response."""
    if not isinstance(raw, dict):
        return ok("Check-in recorded", {"raw": raw})

    verdict = raw.get("action") or raw.get("verdict", "proceed")
    margin = raw.get("margin", "")
    reason = raw.get("reason", "")
    coherence = raw.get("coherence")
    guidance = raw.get("guidance")

    data = {"verdict": verdict}
    if margin:
        data["margin"] = margin
    if reason:
        data["reason"] = reason
    if coherence is not None:
        data["coherence"] = _round(coherence)
    if guidance:
        data["guidance"] = guidance

    summary = f"{verdict}"
    if margin:
        summary += f" (margin: {margin})"

    return ok(summary, data)


def simplify_search(raw: dict) -> dict:
    """Simplify search_knowledge_graph response."""
    if not isinstance(raw, dict):
        return ok("Search complete", {"raw": raw})

    results = raw.get("results") or raw.get("entries") or []
    if isinstance(results, list):
        simplified = []
        for r in results:
            entry = {}
            if isinstance(r, dict):
                for key in ("title", "content", "summary", "tags", "type", "status", "severity", "score"):
                    if key in r and r[key] is not None:
                        entry[key] = r[key]
            else:
                entry["content"] = str(r)
            if entry:
                simplified.append(entry)

        return ok(f"{len(simplified)} result(s) found", {"results": simplified})

    return ok("Search complete", {"results": raw})


def simplify_note(raw: dict) -> dict:
    """Simplify knowledge note response."""
    if not isinstance(raw, dict):
        return ok("Note saved", {"raw": raw})

    node_id = raw.get("id") or raw.get("node_id") or raw.get("note_id") or raw.get("entry_id")
    data = {"saved": True}
    if node_id:
        data["id"] = node_id

    return ok("Note saved", data)


def simplify_query(raw: dict) -> dict:
    """Simplify call_model / routed tool response."""
    if not isinstance(raw, dict):
        return ok(str(raw)[:200], {"response": raw})

    # call_model returns {"response": "..."} or the tool result
    response = raw.get("response") or raw.get("result") or raw.get("text")
    if response:
        summary = str(response)[:200]
        return ok(summary, {"response": response})

    return ok("Query processed", raw)


def _round(val: Any) -> Any:
    """Round numeric values for display."""
    if isinstance(val, float):
        return round(val, 4)
    if isinstance(val, dict):
        v = val.get("value")
        if isinstance(v, (int, float)):
            return round(v, 4)
    return val
