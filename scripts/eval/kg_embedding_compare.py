#!/usr/bin/env python3
"""Offline known-item retrieval comparison; never changes the serving model or KG.

Keep snapshots, vectors and caches outside version control: they contain KG text.
Requires numpy; encode additionally needs torch/sentence-transformers, and fts
uses asyncpg with a READ ONLY transaction (no tables are created).
"""
from __future__ import annotations

import argparse
import asyncio
from datetime import datetime, timezone
import hashlib
import importlib.metadata
import json
import math
from pathlib import Path
import resource
import statistics
import subprocess
import sys
import time

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

MODELS = {
    "bge": {
        "repo": "BAAI/bge-m3",
        "revision": "5617a9f61b028005a4858fdac845db406aefb181",
        "query_prompt": None, "document_prompt": None,
    },
    "qwen": {
        "repo": "Qwen/Qwen3-Embedding-0.6B",
        "revision": "97b0c614be4d77ee51c0cef4e5f07c00f9eb65b3",
        "query_prompt": "query", "document_prompt": None,
    },
    "gemma": {
        "repo": "google/embeddinggemma-2",
        "revision": "914f7f89142e33e77833254d9c9b90c3cef7303b",
        "query_prompt": "SearchQuery", "document_prompt": "Document",
    },
}


def digest(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def write_json(path, value):
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    Path(path).write_text(json.dumps(value, indent=2, allow_nan=False) + "\n")


def load_inputs(corpus_path, labels_path):
    rows = [json.loads(line) for line in Path(corpus_path).read_text().splitlines()]
    rows = [row for row in rows if row["status"] not in {"archived", "cold"}]
    ids = [row["id"] for row in rows]
    if len(ids) != len(set(ids)):
        raise ValueError("duplicate corpus IDs")
    labels = json.loads(Path(labels_path).read_text())["pairs"]
    if not rows or not labels:
        raise ValueError("empty corpus or labels")
    for pair in labels:
        if not pair.get("relevant_ids") or not set(pair["relevant_ids"]) <= set(ids):
            raise ValueError(f"missing/out-of-scope label: {pair['query']}")
    return rows, labels


def snapshot(args):
    # PostgreSQL serializes rows itself. No SQL writes or schema changes.
    sql = """BEGIN ISOLATION LEVEL REPEATABLE READ READ ONLY;
    SELECT json_build_object('id',d.id,'summary',d.summary,'details',d.details,
      'tags',d.tags,'status',d.status,'type',d.type,'created_at',d.created_at,
      'updated_at',d.updated_at,'search_vector',d.search_vector::text,
      'embedding_updated_at',e.updated_at,'bge_vector',e.embedding::text)
    FROM knowledge.discoveries d LEFT JOIN core.discovery_embeddings_bge_m3 e
      ON e.discovery_id=d.id ORDER BY d.id;
    COMMIT;"""
    result = subprocess.run(
        ["psql", "-X", "-qAt", "-v", "ON_ERROR_STOP=1", "-d", args.database, "-c", sql],
        capture_output=True, text=True, check=True,
    )
    destination = Path(args.output)
    if destination.exists():
        raise ValueError("refusing to overwrite frozen snapshot")
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(result.stdout)
    rows = [json.loads(line) for line in result.stdout.splitlines()]
    write_json(destination.with_suffix(".meta.json"), {
        "captured_at": datetime.now(timezone.utc).isoformat(),
        "sha256": digest(destination), "rows": len(rows),
        "active_rows": sum(d["status"] not in {"archived", "cold"} for d in rows),
        "git_sha": subprocess.check_output(["git", "rev-parse", "HEAD"], text=True).strip(),
    })


def embed_text(row):
    # Same document text as the serving embedder; hold chunking constant.
    return f"{row['summary']}\n{(row.get('details') or '')[:6000]}"


def synchronize(torch, device):
    if device == "mps":
        torch.mps.synchronize()
    elif device.startswith("cuda"):
        torch.cuda.synchronize()


def encode(args):
    import numpy as np
    import torch
    from sentence_transformers import SentenceTransformer

    rows, labels = load_inputs(args.corpus, args.labels)
    spec = MODELS[args.model]
    dtype = torch.bfloat16 if args.device != "cpu" else torch.float32
    kwargs = {"model_kwargs": {"dtype": dtype}}
    if args.model == "gemma":
        kwargs["config_kwargs"] = {"vision_config": None, "audio_config": None}
    started = time.perf_counter()
    model = SentenceTransformer(
        spec["repo"], revision=spec["revision"], device=args.device,
        local_files_only=True, trust_remote_code=False, **kwargs,
    )
    model.max_seq_length = 8192
    synchronize(torch, args.device)
    load_seconds = time.perf_counter() - started

    def run(texts, prompt, batch_size):
        options = {"prompt_name": prompt} if prompt else {"prompt": ""}
        result = model.encode(
            texts, batch_size=batch_size, normalize_embeddings=True,
            convert_to_tensor=True, show_progress_bar=False, **options,
        )
        return result.float().cpu().numpy()

    print(f"loaded {args.model} in {load_seconds:.1f}s", flush=True)
    documents = []
    started = time.perf_counter()
    for offset in range(0, len(rows), args.batch_size):
        batch = rows[offset:offset + args.batch_size]
        documents.append(run([embed_text(d) for d in batch], spec["document_prompt"], args.batch_size))
        if offset % (args.batch_size * 10) == 0:
            print(f"{args.model} documents {offset + len(batch)}/{len(rows)}", flush=True)
    synchronize(torch, args.device)
    document_seconds = time.perf_counter() - started
    document_vectors = np.concatenate(documents)
    run([labels[0]["query"]], spec["query_prompt"], 1)  # query warmup
    timings, query_vectors = [], []
    for repeat in range(args.repeats):
        for pair in labels:
            synchronize(torch, args.device)
            started = time.perf_counter()
            vector = run([pair["query"]], spec["query_prompt"], 1)
            synchronize(torch, args.device)
            timings.append((time.perf_counter() - started) * 1000)
            if repeat == 0:
                query_vectors.append(vector[0])
    query_vectors = np.asarray(query_vectors)
    for vectors in [document_vectors, query_vectors]:
        if not np.isfinite(vectors).all() or not np.allclose(np.linalg.norm(vectors, axis=1), 1, atol=.025):
            raise ValueError("invalid or non-normalized embeddings")
        # Accumulation/normalization in bf16 need not produce exact unit norms.
        vectors /= np.linalg.norm(vectors, axis=1, keepdims=True)
    np.savez_compressed(args.output, documents=document_vectors, queries=query_vectors)
    meta = {
        "model": args.model, **spec, "corpus_sha256": digest(args.corpus),
        "labels_sha256": digest(args.labels), "ids": [d["id"] for d in rows],
        "device": args.device, "dtype": str(dtype), "max_seq_length": 8192,
        "batch_size": args.batch_size, "query_repeats": args.repeats,
        "load_seconds": load_seconds, "document_seconds": document_seconds,
        "query_ms": timings, "peak_rss_bytes": resource.getrusage(resource.RUSAGE_SELF).ru_maxrss * (1 if sys.platform == "darwin" else 1024),
        "versions": {p: importlib.metadata.version(p) for p in ["torch", "transformers", "sentence-transformers", "numpy"]},
    }
    if args.device == "mps":
        meta["mps_driver_allocated_bytes_at_end"] = torch.mps.driver_allocated_memory()
    write_json(Path(args.output).with_suffix(".json"), meta)
    print(f"completed {args.model}: {document_seconds:.1f}s documents; query median {statistics.median(timings):.1f}ms", flush=True)


async def fts(args):
    import asyncpg
    from src.db.mixins.knowledge_graph import _apply_operator

    rows, labels = load_inputs(args.corpus, args.labels)
    frozen = json.dumps([{"id": d["id"], "tsv": d["search_vector"]} for d in rows])
    conn = await asyncpg.connect(database=args.database, command_timeout=120)
    output = {"corpus_sha256": digest(args.corpus), "labels_sha256": digest(args.labels), "queries": []}
    try:
        async with conn.transaction(readonly=True):
            for pair in labels:
                result = {}
                for operator in ["AND", "OR"]:
                    matches = await conn.fetch("""
                      SELECT id, ts_rank_cd(tsv::tsvector, websearch_to_tsquery('english',$2),32) AS score
                      FROM jsonb_to_recordset($1::jsonb) AS d(id text, tsv text)
                      WHERE tsv::tsvector @@ websearch_to_tsquery('english',$2)
                      ORDER BY score DESC,id
                    """, frozen, _apply_operator(pair["query"], operator))
                    result[operator] = [dict(m) for m in matches]
                output["queries"].append(result)
    finally:
        await conn.close()
    write_json(args.output, output)


def fuse(dense, lexical, pool=50):
    scores = {}
    for ranking in [dense[:pool], lexical[:pool]]:
        for rank, item in enumerate(ranking, 1):
            scores[item] = scores.get(item, 0) + 1 / (60 + rank)
    return sorted(scores, key=lambda item: (-scores[item], item))


def known_item_metrics(ranking, relevant):
    # Labels identify known answers, not exhaustive relevance judgments.
    ranks = [i for i, item in enumerate(ranking, 1) if item in relevant]
    first = min(ranks, default=None)
    return {
        "rank": first, "hit@1": int(first is not None and first <= 1),
        "hit@5": int(first is not None and first <= 5),
        "hit@10": int(first is not None and first <= 10),
        "recall@20": sum(rank <= 20 for rank in ranks) / len(relevant),
        "mrr@10": 1 / first if first is not None and first <= 10 else 0,
        "ndcg@10": sum(1 / math.log2(rank + 1) for rank in ranks if rank <= 10)
        / sum(1 / math.log2(i + 2) for i in range(min(10, len(relevant)))),
    }


def score(args):
    import numpy as np

    rows, labels = load_inputs(args.corpus, args.labels)
    ids = [row["id"] for row in rows]
    expected = {"corpus_sha256": digest(args.corpus), "labels_sha256": digest(args.labels)}
    lexical = json.loads(Path(args.fts).read_text())
    if any(lexical[k] != v for k, v in expected.items()):
        raise ValueError("FTS snapshot/labels mismatch")
    methods = {"fts_and_then_or": [], "fts_or": []}
    for result in lexical["queries"]:
        methods["fts_and_then_or"].append([r["id"] for r in result["AND"] or result["OR"]])
        methods["fts_or"].append([r["id"] for r in result["OR"]])
    encoders = {}
    for filename in args.vectors:
        meta = json.loads(Path(filename).with_suffix(".json").read_text())
        if any(meta[k] != v for k, v in expected.items()) or meta["ids"] != ids:
            raise ValueError("vector snapshot/labels/order mismatch")
        vectors = np.load(filename, allow_pickle=False)
        if vectors["documents"].shape[0] != len(ids) or vectors["queries"].shape[0] != len(labels):
            raise ValueError("wrong embedding row count")
        similarities = vectors["queries"] @ vectors["documents"].T
        if not np.isfinite(similarities).all():
            raise ValueError("nonfinite similarities")
        name = meta["model"]
        for suffix in ["dense", "hybrid_and", "hybrid_or", "hybrid_and_floor03"]:
            methods[f"{name}_{suffix}"] = []
        for qi, scores in enumerate(similarities):
            order = sorted(range(len(ids)), key=lambda i: (-float(scores[i]), ids[i]))
            dense = [ids[i] for i in order]
            lexical_and = [r["id"] for r in lexical["queries"][qi]["AND"]]
            lexical_or = methods["fts_or"][qi]
            methods[f"{name}_dense"].append(dense)
            methods[f"{name}_hybrid_and"].append(fuse(dense, lexical_and))
            methods[f"{name}_hybrid_or"].append(fuse(dense, lexical_or))
            methods[f"{name}_hybrid_and_floor03"].append(fuse([ids[i] for i in order if scores[i] >= .3], lexical_and))
        encoders[name] = {k: v for k, v in meta.items() if k not in {"ids", "query_ms"}}
        encoders[name]["query_p50_ms"] = statistics.median(meta["query_ms"])
        encoders[name]["query_p95_ms"] = float(np.percentile(meta["query_ms"], 95))
    result = {**expected, "corpus_rows": len(rows), "query_count": len(labels),
              "topic_count": len({p["topic"] for p in labels}), "encoders": encoders,
              "fts_and_empty": sum(not q["AND"] for q in lexical["queries"]), "methods": {}}
    for method, rankings in methods.items():
        per_query = []
        for pair, ranking in zip(labels, rankings, strict=True):
            per_query.append({"query_id": pair["query_id"], "topic": pair["topic"],
                              "origin": pair["origin"], **known_item_metrics(ranking, set(pair["relevant_ids"])),
                              "top10": ranking[:10]})
        groups = {}
        for origin in ["all"] + sorted({p["origin"] for p in per_query}):
            group = [p for p in per_query if origin == "all" or p["origin"] == origin]
            groups[origin] = {"n": len(group), **{
                metric: statistics.mean(p[metric] for p in group)
                for metric in ["hit@1", "hit@5", "hit@10", "recall@20", "mrr@10", "ndcg@10"]}}
        result["methods"][method] = {"aggregate": groups, "per_query": per_query}
    write_json(args.output, result)
    for method, values in result["methods"].items():
        print(method, json.dumps(values["aggregate"]["all"]))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    for name in ["snapshot", "fts", "encode", "score"]:
        p = sub.add_parser(name)
        p.add_argument("--output", required=True)
        if name != "snapshot":
            p.add_argument("--corpus", required=True)
            p.add_argument("--labels", required=True)
        if name in {"snapshot", "fts"}:
            p.add_argument("--database", default="governance")
        if name == "encode":
            p.add_argument("--model", choices=MODELS, required=True)
            p.add_argument("--device", default="cpu")
            p.add_argument("--batch-size", type=int, default=8)
            p.add_argument("--repeats", type=int, default=2)
        if name == "score":
            p.add_argument("--fts", required=True)
            p.add_argument("--vectors", nargs="+", required=True)
    args = parser.parse_args()
    if args.command == "fts":
        asyncio.run(fts(args))
    else:
        globals()[args.command](args)


if __name__ == "__main__":
    main()
