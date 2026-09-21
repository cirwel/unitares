"""Authority classification and ranking for shared knowledge.

The knowledge graph intentionally accepts context from many sources.  Source
provenance makes those rows inspectable, but provenance alone does not stop an
imported memory from outranking a directly authored or corroborated finding.
This module keeps retrieval relevance and epistemic authority separate while
letting the search handler combine them deliberately.

An imported memory remains searchable and may still rank first when it is the
only strong match.  Its default multiplier merely prevents it from winning a
close relevance contest against native knowledge.  A governed claim is not a
truth oracle: it means the server recorded a promotion receipt with a source
memory and non-memory evidence references.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterable, Mapping, Sequence


IMPORTED_CONTEXT = "imported_context"
NATIVE_FINDING = "native_finding"
GOVERNED_CLAIM = "governed_claim"

PROMOTION_SCHEMA = "unitares.knowledge_promotion.v1"
PROMOTION_TAG = "authority-governed"

AUTHORITY_MULTIPLIERS = {
    IMPORTED_CONTEXT: 0.55,
    NATIVE_FINDING: 1.0,
    GOVERNED_CLAIM: 1.15,
}

_IMPORTED_TAGS = frozenset({
    "imported-memory",
    "memory-import",
    "memory-sync",
    "source-memory",
})


@dataclass(frozen=True)
class AuthorityAssessment:
    tier: str
    trust: str
    basis: str
    multiplier: float
    source_id: str | None = None
    evidence_ids: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "tier": self.tier,
            "trust": self.trust,
            "basis": self.basis,
            "ranking_multiplier": self.multiplier,
        }
        if self.source_id:
            payload["source_id"] = self.source_id
        if self.evidence_ids:
            payload["evidence_ids"] = list(self.evidence_ids)
        return payload


def is_imported_memory_tag(tag: object) -> bool:
    """Return whether a normalized tag denotes an imported memory lane.

    The pattern is harness-neutral: ``source-<provider>-memory`` works without
    hard-coding Claude, Codex, or any resident name.
    """
    value = str(tag or "").strip().lower()
    return value in _IMPORTED_TAGS or (
        value.startswith("source-") and value.endswith("-memory")
    )


def has_imported_memory_marker(tags: Iterable[object] | None) -> bool:
    return any(is_imported_memory_tag(tag) for tag in (tags or ()))


def _promotion_receipt(document: Any) -> Mapping[str, Any] | None:
    provenance = getattr(document, "provenance", None)
    if not isinstance(provenance, Mapping):
        return None
    receipt = provenance.get("knowledge_authority")
    if not isinstance(receipt, Mapping):
        return None
    if receipt.get("schema") != PROMOTION_SCHEMA:
        return None
    if not receipt.get("source_id"):
        return None
    evidence_ids = receipt.get("evidence_ids")
    if not isinstance(evidence_ids, Sequence) or isinstance(evidence_ids, (str, bytes)):
        return None
    if not [value for value in evidence_ids if str(value).strip()]:
        return None
    return receipt


def assess_authority(document: Any) -> AuthorityAssessment:
    """Classify one discovery without treating author confidence as authority."""
    tags = tuple(getattr(document, "tags", None) or ())
    receipt = _promotion_receipt(document)
    if PROMOTION_TAG in tags and receipt is not None:
        evidence_ids = tuple(
            str(value) for value in receipt.get("evidence_ids", ()) if str(value).strip()
        )
        return AuthorityAssessment(
            tier=GOVERNED_CLAIM,
            trust="corroborated",
            basis="server-recorded promotion receipt",
            multiplier=AUTHORITY_MULTIPLIERS[GOVERNED_CLAIM],
            source_id=str(receipt["source_id"]),
            evidence_ids=evidence_ids,
        )
    if has_imported_memory_marker(tags):
        return AuthorityAssessment(
            tier=IMPORTED_CONTEXT,
            trust="unreviewed",
            basis="imported memory source tag",
            multiplier=AUTHORITY_MULTIPLIERS[IMPORTED_CONTEXT],
        )
    return AuthorityAssessment(
        tier=NATIVE_FINDING,
        trust="authored",
        basis="native knowledge-graph write",
        multiplier=AUTHORITY_MULTIPLIERS[NATIVE_FINDING],
    )


def rank_by_authority(
    discoveries: Sequence[Any],
    *,
    relevance_scores: Mapping[str, float] | None = None,
    enabled: bool = True,
) -> tuple[list[Any], bool]:
    """Return a stable relevance×authority order and whether it changed.

    When a retrieval backend exposes no comparable score, a shallow positional
    score preserves its order while still breaking close contests by authority.
    """
    ordered = list(discoveries)
    if not enabled or len(ordered) < 2:
        return ordered, False

    assessments = [assess_authority(document) for document in ordered]
    if len({assessment.tier for assessment in assessments}) < 2:
        return ordered, False

    score_map = relevance_scores or {}
    scored: list[tuple[float, int, Any]] = []
    for index, (document, assessment) in enumerate(zip(ordered, assessments)):
        document_id = str(getattr(document, "id", ""))
        raw_score = score_map.get(document_id)
        if not isinstance(raw_score, (int, float)):
            raw_score = getattr(document, "relevance", None)
        if not isinstance(raw_score, (int, float)):
            raw_score = 1.0 / (1.0 + (index * 0.05))
        scored.append((float(raw_score) * assessment.multiplier, index, document))

    ranked = [item[2] for item in sorted(scored, key=lambda item: (-item[0], item[1]))]
    changed = [getattr(item, "id", None) for item in ranked] != [
        getattr(item, "id", None) for item in ordered
    ]
    return ranked, changed
