"""Descriptive harness census — what harnesses have actually written, from S22 provenance.

Read-only aggregation over S22 write-context
(``core.agent_state.state_json.provenance_context`` +
``knowledge.discoveries.provenance.s22_context``). It reuses the existing
collection/normalization in ``s22_h5_comparison`` and rolls the entries up *by
harness* into an inventory: which harnesses appear, how often, since when, with
which transports/models, and — crucially — how much situating metadata each
carries.

Two things this is **not**:

* **Not an authoritative registry.** ``harness_id`` / ``harness_type`` are
  self-declared *labels*, not identity (``docs/ontology/harness-substrate-plurality.md``;
  the field is a "convenience label", explicitly NOT promoted). This census only
  *describes* what has been observed; it confers no authority and gates nothing.
* **Not the H5 comparison gate.** ``s22_h5_comparable_entries.py`` asks whether one
  shared comparison key spans the required harnesses (a promotion-evidence gate).
  This asks the broader question "what harnesses exist at all, and how well are they
  situated?" — which is exactly the evidence ``docs/ontology/plan.md`` Track D
  (D3/D4) wants before promoting ``harness_id`` to a first-class field. Building the
  authoritative registry is the deferred, council-gated decision; this census feeds it.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any, Optional

from src.identity.s22_h5_comparison import (
    S22H5Entry,
    normalize_s22_h5_entry,
)


def _ensure_entry(entry: S22H5Entry | Mapping[str, Any]) -> Optional[S22H5Entry]:
    if isinstance(entry, S22H5Entry):
        return entry
    if isinstance(entry, Mapping):
        return normalize_s22_h5_entry(entry)
    return None


def _minmax(current_min, current_max, value):
    """ISO-8601 timestamps compare correctly lexicographically; skip Nones."""
    if not value:
        return current_min, current_max
    new_min = value if current_min is None or value < current_min else current_min
    new_max = value if current_max is None or value > current_max else current_max
    return new_min, new_max


def build_harness_census(
    entries: Sequence[S22H5Entry | Mapping[str, Any]],
) -> dict[str, Any]:
    """Roll S22 write-context entries up by canonical harness.

    Pure and deterministic — no DB, no I/O. Entries with no harness label are
    counted as ``unattributed_entries`` (the size of the labelling gap is itself a
    finding). The per-harness ``situating_metadata_ratio`` is the promotion-
    readiness signal Track D keys on.
    """
    normalized = [e for e in (_ensure_entry(x) for x in entries) if e is not None]

    acc: dict[str, dict[str, Any]] = {}
    unattributed = 0
    for e in normalized:
        canon = e.canonical_harness
        if not canon:
            unattributed += 1
            continue
        rec = acc.setdefault(canon, {
            "canonical_harness": canon,
            "raw_harness_types": set(),
            "entry_count": 0,
            "_agents": set(),
            "_harness_ids": set(),
            "_labelled_ids": 0,
            "first_seen": None,
            "last_seen": None,
            "sources": {},
            "transports": set(),
            "models": set(),
            "model_providers": set(),
            "_comparison_keys": set(),
            "_situated": 0,
        })
        rec["entry_count"] += 1
        if e.harness_type:
            rec["raw_harness_types"].add(e.harness_type)
        if e.harness_id:
            rec["_harness_ids"].add(e.harness_id)
            rec["_labelled_ids"] += 1
        if e.agent_id:
            rec["_agents"].add(e.agent_id)
        rec["first_seen"], rec["last_seen"] = _minmax(
            rec["first_seen"], rec["last_seen"], e.recorded_at
        )
        if e.source:
            rec["sources"][e.source] = rec["sources"].get(e.source, 0) + 1
        if e.transport:
            rec["transports"].add(e.transport)
        if e.model:
            rec["models"].add(e.model)
        if e.model_provider:
            rec["model_providers"].add(e.model_provider)
        if e.comparison_key:
            rec["_comparison_keys"].add(e.comparison_key)
        if e.has_situating_metadata:
            rec["_situated"] += 1

    harnesses = []
    for rec in acc.values():
        count = rec["entry_count"]
        harnesses.append({
            "canonical_harness": rec["canonical_harness"],
            "raw_harness_types": sorted(rec["raw_harness_types"]),
            "entry_count": count,
            "distinct_agents": len(rec["_agents"]),
            # Type-vs-instance: how many concrete harness instances under this type,
            # and how many entries carried an instance label at all (the Track D
            # promotion question). instance_label_ratio < 1.0 means harness_id is
            # sparse — not yet safe to promote to first-class.
            "distinct_harness_ids": len(rec["_harness_ids"]),
            "instance_label_ratio": round(rec["_labelled_ids"] / count, 3) if count else 0.0,
            "first_seen": rec["first_seen"],
            "last_seen": rec["last_seen"],
            "sources": dict(sorted(rec["sources"].items())),
            "transports": sorted(rec["transports"]),
            "models": sorted(rec["models"]),
            "model_providers": sorted(rec["model_providers"]),
            "distinct_comparison_keys": len(rec["_comparison_keys"]),
            "situating_metadata_ratio": round(rec["_situated"] / count, 3) if count else 0.0,
        })
    # Most-active harness first; ties broken by name for determinism.
    harnesses.sort(key=lambda h: (-h["entry_count"], h["canonical_harness"]))

    attributed = len(normalized) - unattributed
    return {
        "total_entries": len(normalized),
        "attributed_entries": attributed,
        "unattributed_entries": unattributed,
        "distinct_harnesses": len(acc),
        "harnesses": harnesses,
    }
