# KG retrieval rebuild (2026-04-20): pointer

This path is cited by the v6 paper and by source docstrings. The original
working plan is not kept in this repository; this page points to the material
that documents the rebuild and holds its baselines.

The rebuild ran in phases, shipped in PRs #55 to #63 on 2026-04-20:

- **Phase 2, re-embedding:** `scripts/migration/reembed_corpus.py` (model
  selected by `UNITARES_EMBEDDING_MODEL`, for example `bge-m3`).
- **Phase 3, cross-encoder reranking:** `src/reranker.py`
  (`UNITARES_ENABLE_RERANKER`).
- **Phase 4, hybrid fusion (BM25 plus dense, reciprocal rank fusion):**
  `src/retrieval.py` (`UNITARES_ENABLE_HYBRID`).
- **Phase 5, one-hop typed-edge graph expansion:** behind
  `UNITARES_ENABLE_GRAPH_EXPANSION`.

Evaluation harness and baselines:

- [`tests/retrieval_eval/README.md`](../../tests/retrieval_eval/README.md):
  how to run the harness, and the metrics (nDCG@10, Recall@20, MRR, flat-miss
  rate).
- [`scripts/eval/retrieval_eval.py`](../../scripts/eval/retrieval_eval.py):
  the harness itself.
- `tests/retrieval_eval/labels.json`: the labeled query corpus.
- `tests/retrieval_eval/baseline_2026-04-20*.json`: the baselines recorded on
  2026-04-20 (dense, `bge-m3`, `bge-m3` reranked, hybrid RRF, hybrid plus
  graph).
