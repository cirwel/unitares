"""Principal (octopus) rollup — derived, off the hot path.

A PRINCIPAL is the logical worker that many process-instance identities are
facets of (the octopus to the per-process tentacle). It is computed as a
connected component over only agent-DECLARED edges — shared ``thread_id`` and
declared lineage (``parent_agent_id``). Spoofable/coarse keys (IP:UA
fingerprint, the ``<harness>_<date>`` label) are excluded by construction; the
ontology names both performative. See docs/proposals/principal-rollup-v0.md.

This module holds a DERIVED rollup, recomputed periodically by a background
sweeper (``principal_rollup_sweeper_task``) and read by the identity/onboard
response builders so an agent can see "you are instance K of principal P".

DESIGN (council 2026-06-18, unanimous): principal is NEVER resolved-or-created
at mint. Onboard is the moment of least information about the component (most
lineage/thread edges arrive later). The rollup is a recomputed cache, never an
authoritative mint-time resolution.

INVARIANT — ``principal_id`` MUST NEVER authorize anything. It is advisory,
display-only. It is not accepted on any write path, never consulted by identity
resolution / rebind / tier, and is fail-open (a missing entry yields ``None``,
never an error). A copyable principal that authorized resumption would re-open
the S19 / copyable-bearer vector the ontology names performative.

A genuine singleton (no thread, no lineage, or a one-of-one component) has NO
principal — ``lookup`` returns ``None``. We never fabricate a singleton-principal
object, which would let the anon-ghost class re-inflate the very count this
exists to deflate.
"""
from __future__ import annotations

from typing import Any, Optional

# uuid -> {"principal_id": <root uuid>, "instance_count": <int>}. Replaced
# atomically on each recompute; only MULTI-instance components are present
# (singletons are intentionally absent → lookup yields None).
_MAP: dict[str, dict[str, Any]] = {}


def _components(metas: dict[str, Any]) -> dict[str, list[str]]:
    """Connected components over shared thread_id U declared lineage."""
    parent: dict[str, str] = {}

    def find(x: str) -> str:
        parent.setdefault(x, x)
        root = x
        while parent[root] != root:
            root = parent[root]
        while parent[x] != root:  # path compression
            parent[x], x = root, parent[x]
        return root

    def union(a: str, b: str) -> None:
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[ra] = rb

    uuids = set(metas)
    by_thread: dict[str, list[str]] = {}
    for uid, meta in metas.items():
        find(uid)
        thread = getattr(meta, "thread_id", None)
        if thread:
            by_thread.setdefault(thread, []).append(uid)
    for members in by_thread.values():
        for m in members[1:]:
            union(members[0], m)
    for uid, meta in metas.items():
        par = getattr(meta, "parent_agent_id", None)
        if par and par in uuids:  # only chain to a parent we actually hold
            union(uid, par)

    comps: dict[str, list[str]] = {}
    for uid in metas:
        comps.setdefault(find(uid), []).append(uid)
    return comps


def recompute(metas: dict[str, Any]) -> int:
    """Rebuild the derived map from the agent-metadata cache.

    ``principal_id`` is the lexicographically-smallest member uuid of the
    component — a stable, deterministic anchor ("the founding instance"), and a
    real per-process identity, NOT a new mintable identifier. Only multi-instance
    components are stored. Returns the number of principals mapped.
    """
    new_map: dict[str, dict[str, Any]] = {}
    mapped = 0
    for members in _components(metas).values():
        if len(members) < 2:
            continue  # singleton: no octopus → absent → lookup None
        principal_id = min(members)
        for uid in members:
            new_map[uid] = {"principal_id": principal_id, "instance_count": len(members)}
        mapped += 1
    global _MAP
    _MAP = new_map
    return mapped


def lookup(agent_uuid: Optional[str]) -> Optional[dict[str, Any]]:
    """Fail-open read: the principal block for ``agent_uuid``, or None.

    None when the agent is a singleton, not yet reconciled, or anything is off.
    A fresh mint is typically absent until the next sweep — that is correct
    (an agent learns its octopus on a later identity() call), and it is never an
    error.
    """
    if not agent_uuid:
        return None
    entry = _MAP.get(agent_uuid)
    if not entry:
        return None
    return {
        "principal_id": entry["principal_id"],
        "instance_count": entry["instance_count"],
        "source": "derived",  # advisory rollup, recomputed; not a credential
    }
