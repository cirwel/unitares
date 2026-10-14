# Local KG embedding comparison

This is a **known-item retrieval pilot**, not a representative benchmark or a
production deployment. It addresses the operator's request to test Qwen and
Gemma after the October 13 search critique. No model, threshold, lifecycle or
search setting in the running service is changed.

## Fixed design (before scores)

- Freeze the corpus in one PostgreSQL repeatable-read, read-only transaction.
  Restrict this pilot to the default-visible statuses (exclude archived/cold).
- Select 24 previously logged, untagged searches with identifiable, in-scope
  answers, and write one natural-language paraphrase per topic: 48 queries.
  Verify the target summaries before scoring. The selection is purposive, not
  random; the paraphrases are agent-authored, not recorded user traffic. An
  initially considered lease incident was excluded before scoring because its
  target was archived. This set does not measure lifecycle recall.
- One known target per query is **not exhaustive relevance labeling**. A
  different correct answer can be unfairly penalized. Query pairs share a
  target and are correlated; report the two origins separately.
- Compare BGE-M3, Qwen3-Embedding-0.6B and EmbeddingGemma 2 (text-only), with
  pinned model revisions. Re-embed every document for every model, including
  BGE; do not give BGE stale stored vectors while competitors get fresh ones.
- Hold document text fixed at summary + first 6,000 details characters, as in
  production. Use each model's documented query/document prompts. Normalize
  vectors; use an 8,192-token maximum, MPS bfloat16, batch size 8, and sequential
  model runs. No fine-tuning or query-prompt tuning on this set.
- Primary diagnostic: known-target MRR@10. Also report target hit@1/5/10,
  recall@20, and nDCG@10. Record warm single-query encode p50/p95, document
  encoding duration and process peak RSS. RSS is not total system or GPU memory.
  Query latency includes tokenization, encoding and vector transfer, not MCP,
  PostgreSQL, fusion or response serialization.
- Retrieve dense candidates by exact cosine, eliminating approximate-index
  differences. Compare the models alone and with fixed RRF (k=60, pool=50).
  Preserve the frozen PostgreSQL tsvectors; compare lexical AND with fallback
  to OR, unconditional OR, hybrid AND and hybrid OR. The hybrid AND path does
  not automatically use OR when lexical matching is empty, matching that
  mechanism in the serving handler. Include a fixed 0.3-floor diagnostic,
  explicitly **not** a calibrated threshold transferable between models.
- These are isolated first-stage comparisons: no AGE connectivity, temporal or
  status score multipliers, authority reranking, graph expansion, cross-encoder
  or digest serialization. They **do not reproduce the production ranking**.
  They answer which encoder finds known targets and how lexical fusion affects
  that comparison. Any production change needs a subsequent serving-path eval.
- A possible longer-note/chunking improvement remains a separate experiment.
  Keeping it fixed here prevents attributing extra visible text to the model.

## Reproduce locally

The tool requires Python, numpy, PostgreSQL's `psql`, and asyncpg; encoding also
requires torch and sentence-transformers. Gemma 2 requires current libraries
with `embedding_gemma2` support (the recorded run uses an isolated environment).
Its processor also imports Pillow and torchvision even for text-only encoding;
install those and sentencepiece in that environment.
Download the pinned revisions in `MODELS` with `hf download` before encoding;
encoding is local-only and refuses remote custom code. Install dependencies in
an isolated environment, not the production interpreter.

```sh
python scripts/eval/kg_embedding_compare.py snapshot \
  --output .scratch/embedding-eval/frozen.jsonl
python scripts/eval/kg_embedding_compare.py fts \
  --corpus .scratch/embedding-eval/frozen.jsonl \
  --labels tests/retrieval_eval/embedding_compare/labels.json \
  --output .scratch/embedding-eval/fts.json
# Run once each for bge, qwen and gemma, sequentially:
python scripts/eval/kg_embedding_compare.py encode --model qwen --device mps \
  --corpus .scratch/embedding-eval/frozen.jsonl \
  --labels tests/retrieval_eval/embedding_compare/labels.json \
  --output .scratch/embedding-eval/qwen.npz
python scripts/eval/kg_embedding_compare.py score \
  --corpus .scratch/embedding-eval/frozen.jsonl \
  --labels tests/retrieval_eval/embedding_compare/labels.json \
  --fts .scratch/embedding-eval/fts.json \
  --vectors .scratch/embedding-eval/bge.npz .scratch/embedding-eval/qwen.npz .scratch/embedding-eval/gemma.npz \
  --output .scratch/embedding-eval/results.json
```

CPU uses float32; report it as a different latency environment. Snapshots and
vectors contain private KG content and stay in ignored `.scratch/`. Publish
only reviewed query labels, aggregate metrics, IDs/ranks and environment
metadata. Hash checks reject changed labels, snapshots and vector row order.
New snapshots may have decayed labels; the tool refuses those rather than
silently dropping them or changing the denominator.

The related critique IDs are `2026-10-13T06:48:37.234783+00:00` and
`2026-10-13T21:50:39.123530+00:00`. Their claims and subsequent corrections
must be read together; this experiment tests encoders and lexical matching,
not every reported response-size or maintenance problem.
