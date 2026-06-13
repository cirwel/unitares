"""Curated tag-ontology tables for the knowledge graph.

Two hand-curated, deterministic layers — NOT inference, NOT an ontology
system. Per the 2026-06-13 ontology-need analysis, shared-graph tag
fragmentation is *mostly a formatting problem* (``Postgres`` / ``postgres`` /
``PostgreSQL`` filed three ways, each a future search miss). The formatting
layer is fixed by :func:`src.knowledge_graph.normalize_tags`; the small
*semantic* residue that formatting cannot reach (``db`` vs ``database``)
is fixed here, deliberately and by hand.

Layer separation (kept identical to the governance-plugin client normalizer
so server-minted and plugin-minted tags converge on the same canonical form):

- ``SPELLING_VARIANTS`` — **formatting layer**, applied on every write via
  ``normalize_tags``. Spelling/format variants of the *same* token
  (``postgresql`` → ``postgres``). The plugin's ``tag_normalize.py`` carries
  the identical map client-side; this server-side copy catches non-plugin
  clients and the direct REST/API write path.

- ``SEMANTIC_SYNONYMS`` — **semantic layer**, applied only in the KG
  lifecycle pass (``run_cleanup``), never at write time. Distinct tokens that
  mean the same thing (``db`` → ``database``, ``auth`` → ``identity``).
  Merging these at write time would be a silent lossy rewrite of caller
  intent, so it is deferred to the periodic janitorial sweep where it is
  visible and auditable.

Both tables are intentionally tiny and hand-curated. Add an entry only when a
real fragmentation has been observed — never auto-derive. Explicitly out of
scope: plural stripping (``metrics`` → ``metric`` is lossy), entity
resolution, richer typed edges, any standing ontology agent.
"""

from typing import Dict, List

# Formatting layer — spelling/format variants of the SAME token. Keys are
# post-slug (lowercase, hyphen-folded) forms; values are the canonical form.
# Applied inside normalize_tags(), so it runs on every write and on every
# tag-filtered search (keeping stored and queried tags consistent).
SPELLING_VARIANTS: Dict[str, str] = {
    "postgresql": "postgres",
}

# Semantic layer — distinct tokens that denote the same concept. Hand-curated.
# Applied ONLY by the lifecycle pass, never at write time. Keys and values are
# already in canonical (post-normalize_tags) form.
SEMANTIC_SYNONYMS: Dict[str, str] = {
    "db": "database",
    "auth": "identity",
}


def apply_semantic_synonyms(tags: List[str]) -> List[str]:
    """Map curated semantic synonyms to their canonical form, order-preserving.

    Expects ``tags`` already run through ``normalize_tags`` (lowercase,
    hyphen-folded). Deduplicates after mapping so that, e.g.,
    ``["db", "database"]`` collapses to ``["database"]``. Unknown tags pass
    through untouched. Idempotent: canonical forms are never themselves keys.
    """
    seen: set[str] = set()
    result: List[str] = []
    for tag in tags:
        canonical = SEMANTIC_SYNONYMS.get(tag, tag)
        if canonical and canonical not in seen:
            seen.add(canonical)
            result.append(canonical)
    return result
