"""
Thread-based identity with honest forking.

Pure logic module — no I/O, no database calls.
Imported by identity_v2.py for thread context construction.

A thread is the user's conversation — the true identity anchor.
A fork is each new agent instance, with a numbered position.
Discontinuities are made legible, not hidden (kintsugi model).
"""

from __future__ import annotations

import hashlib
from typing import Optional

from src.identity.lineage_semantics import NON_SUCCESSION_SPAWN_REASONS

LINEAGE_SPAWN_REASONS = frozenset(NON_SUCCESSION_SPAWN_REASONS)


def generate_thread_id(session_key: str) -> str:
    """
    Derive a stable thread ID from a session key.

    For MCP sessions (stable per connection): use the session ID portion.
    For IP:UA fingerprints: use the UA hash (stable across IP rotation).
    For stdio:{pid}: use pid-based key.

    Returns a short opaque ID prefixed with "t-".
    """
    if session_key.startswith("mcp:"):
        raw = session_key[4:]
    elif ":" in session_key:
        # IP:UA or model-suffixed key — use the stable portion
        parts = session_key.split(":")
        # Skip IP-like first parts, use the rest
        raw = ":".join(parts[1:]) if len(parts) >= 2 else session_key
    else:
        raw = session_key

    return "t-" + hashlib.sha256(raw.encode()).hexdigest()[:16]


def infer_spawn_reason(
    arguments: dict,
    existing_nodes: list[dict],
) -> str:
    """
    Infer why this fork was created from available signals.

    Priority:
    1. Explicit caller-provided spawn_reason
    2. No parent_agent_id declared → "new_session"
    3. Claude Code client + existing thread nodes → "compaction"
    4. parent_agent_id present + existing thread nodes → "subagent"
    5. Declared parent, no prior thread nodes → "new_session"

    Inference never manufactures a lineage reason for an undeclared parent.
    A lineage reason (``compaction``, ``subagent``) classifies the fork as
    ``identity_lineage`` and is persisted as ``spawn_reason``, so returning
    one here would report and record a declaration the caller never made.
    Thread co-location alone does not establish it: a thread derived from an
    IP:UA fingerprint is shared by every unrelated session behind one egress
    address and client (cloud-hosted harnesses, 2026-09-23).
    """
    explicit = arguments.get("spawn_reason")
    if explicit:
        return explicit

    if not arguments.get("parent_agent_id"):
        return "new_session"

    if existing_nodes:
        client_hint = arguments.get("client_hint", "")
        if "claude_code" in client_hint or "claude-code" in client_hint:
            return "compaction"
        return "subagent"

    return "new_session"


def classify_episode_fork(
    position: int,
    agent_uuid: Optional[str],
    parent_uuid: Optional[str],
    spawn_reason: Optional[str],
) -> tuple[str, bool]:
    """Return (episode_fork_kind, identity_lineage_fork) per R6 v2."""
    has_child_uuid = bool(parent_uuid and agent_uuid and agent_uuid != parent_uuid)
    if has_child_uuid:
        return ("identity_lineage", True)

    if spawn_reason in LINEAGE_SPAWN_REASONS and not parent_uuid:
        return ("identity_lineage", True)

    if position > 1:
        return ("sibling_locus", False)

    return ("none", False)


def fork_honest_message(
    episode_fork_kind: str,
    parent_uuid: Optional[str],
    spawn_reason: Optional[str],
    *,
    minted_fresh: bool = False,
) -> str:
    """Build the R6 honest-message text shared by thin and rich contexts.

    ``sibling_locus`` covers two cases the classification does not separate:
    a resumed UUID reoccupying its thread (the case R6 was written for), and
    a freshly minted UUID landing on a thread earlier, unrelated
    process-instances already occupy (a shared IP:UA fingerprint). Only the
    onboard caller knows which it is, so it passes ``minted_fresh``.
    """
    if episode_fork_kind == "sibling_locus" and minted_fresh:
        return (
            "You are a distinct subject - a fresh UUID on a thread that earlier "
            "process-instances also occupy. Sharing a thread declares no "
            "lineage: they are not your predecessors. Memory access (KG, "
            "project files, harness-side caches) may be available; whether "
            "you have integrated it is yours to demonstrate, not asserted."
        )

    if episode_fork_kind == "sibling_locus":
        return (
            "You share a registry UUID with prior process-instances under this "
            "thread, but you are a distinct subject - fresh process-instance, "
            "no child UUID minted. Memory access (KG, project files, "
            "harness-side caches) may be available; whether you have integrated "
            "it is yours to demonstrate, not asserted."
        )

    if episode_fork_kind == "identity_lineage":
        parent_display = parent_uuid or "unknown"
        spawn_display = spawn_reason or "unknown"
        return (
            "You are a distinct subject (a fresh UUID under declared parent "
            f"{parent_display}, spawn_reason {spawn_display}). Lineage was "
            "declared at this fork event; whether it becomes confirmed is "
            "governed by R2's protocol (see provisional_lineage flag and "
            "downstream R1 evaluation)."
        )

    return "You are the first observation under this thread. No fork."


def _agent_uuid_at_position(position: int, all_nodes: list[dict]) -> Optional[str]:
    for node in all_nodes:
        if node.get("thread_position") == position:
            return node.get("agent_id")
    return None


def build_fork_context(
    thread_id: str,
    position: int,
    parent_uuid: Optional[str],
    spawn_reason: Optional[str],
    all_nodes: list[dict],
    *,
    agent_uuid: Optional[str] = None,
    minted_fresh: bool = False,
) -> dict:
    """
    Build the fork context dict that the onboard response embeds.

    This is the kintsugi structure — the legible discontinuity map.

    Returns dict with: thread_id, position, spawn_reason, predecessor,
    thread_size, is_root, is_fork, episode_fork_kind,
    identity_lineage_fork, honest_message.
    """
    is_root = position == 1
    is_fork = position > 1
    current_uuid = agent_uuid or _agent_uuid_at_position(position, all_nodes)
    episode_fork_kind, identity_lineage_fork = classify_episode_fork(
        position,
        current_uuid,
        parent_uuid,
        spawn_reason,
    )

    # Find predecessor
    predecessor = None
    if parent_uuid:
        parent_node = next(
            (n for n in all_nodes if n.get("agent_id") == parent_uuid),
            None,
        )
        if parent_node:
            predecessor = {
                "uuid": parent_uuid,
                "position": parent_node.get("thread_position"),
                "label": parent_node.get("label"),
            }

    # If no explicit parent but we're a fork, use the previous position as predecessor
    if not predecessor and is_fork and all_nodes:
        prev_nodes = [
            n for n in all_nodes
            if n.get("thread_position") is not None
            and n["thread_position"] < position
        ]
        if prev_nodes:
            prev_node = max(prev_nodes, key=lambda n: n["thread_position"])
            predecessor = {
                "uuid": prev_node.get("agent_id"),
                "position": prev_node.get("thread_position"),
                "label": prev_node.get("label"),
            }

    thread_size = len(all_nodes)
    context = {
        "thread_id": thread_id,
        "position": position,
        "spawn_reason": spawn_reason,
        "predecessor": predecessor,
        "thread_size": thread_size,
        "is_root": is_root,
        "is_fork": is_fork,
        "episode_fork_kind": episode_fork_kind,
        "identity_lineage_fork": identity_lineage_fork,
        "honest_message": fork_honest_message(
            episode_fork_kind,
            parent_uuid,
            spawn_reason,
            minted_fresh=minted_fresh,
        ),
    }

    # position and thread_size measure different things and can legitimately
    # diverge — position is a monotonic claim counter (the Nth node ever
    # claimed in this thread, never reused), while thread_size is the count of
    # currently-live nodes. Pruned/archived forks lower the count without
    # rewinding the counter (dogfood 2026-06-13 saw position 24 vs thread_size
    # 19). Label them so the gap isn't read as a contradiction.
    if position > thread_size:
        context["position_note"] = (
            "position is the node's monotonic claim sequence (Nth ever claimed "
            "in this thread); thread_size is the count of currently-live nodes. "
            f"position ({position}) > thread_size ({thread_size}) means "
            f"{position - thread_size} earlier node(s) were pruned or archived — "
            "expected, not a contradiction."
        )

    return context
