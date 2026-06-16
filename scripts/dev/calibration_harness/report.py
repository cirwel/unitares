"""The proof. Computes ECE + AUC from the harness's own DB rows and shows the
server's tactical channel before/after.

Why compute our own ECE/AUC instead of trusting `calibration check`?
  * `calibration check` is GLOBAL and agent-unscoped (tactical_evidence.scope
    == "global"); on a shared server it folds in every agent. The harness runs
    against an isolated test instance precisely so the global pool == harness
    rows, but we still compute independently from per-agent rows as a
    cross-check that should agree with the server's per_channel numbers.
  * We bin on the REGISTERED confidence (detail.reported_confidence), which is
    the transformed value the server actually scored — not our stated input.
"""
from __future__ import annotations

import os
from dataclasses import dataclass

import psycopg2  # type: ignore
import psycopg2.extras  # type: ignore

from .client import GovernanceClient
from .config import BINS

TEST_DB_URL = "postgresql://postgres:postgres@localhost:5432/governance_test"


@dataclass
class Pair:
    confidence: float  # registered/transformed confidence
    success: bool      # not is_bad


def _connect():
    return psycopg2.connect(os.environ.get("DB_POSTGRES_URL", TEST_DB_URL))


def fetch_pairs(agent_uuids: list[str]) -> list[Pair]:
    """Per-agent corroborated test outcomes with their registered confidence."""
    # Read verification_source from detail jsonb, not the column: the column
    # (migration 039) is absent on databases built from base DDL (e.g.
    # governance_test), but the value is always mirrored into detail.
    sql = """
        SELECT (detail->>'reported_confidence')::float8 AS conf, is_bad
        FROM audit.outcome_events
        WHERE agent_id = ANY(%(ids)s)
          AND detail->>'verification_source' = 'external_signal'
          AND outcome_type IN ('test_passed','test_failed')
          AND detail->>'reported_confidence' IS NOT NULL
    """
    with _connect() as conn, conn.cursor() as cur:
        cur.execute(sql, {"ids": agent_uuids})
        return [Pair(confidence=float(c), success=(not b)) for c, b in cur.fetchall()]


def compute_ece(pairs: list[Pair]) -> tuple[float, list[dict]]:
    """Expected Calibration Error over the configured bins + reliability table."""
    table: list[dict] = []
    total = len(pairs)
    ece = 0.0
    for lo, hi in BINS:
        members = [p for p in pairs if (lo <= p.confidence < hi) or (hi == 1.0 and p.confidence == 1.0)]
        if not members:
            table.append({"bin": f"{lo:.1f}-{hi:.1f}", "count": 0, "mean_conf": None, "accuracy": None, "gap": None})
            continue
        mean_conf = sum(p.confidence for p in members) / len(members)
        accuracy = sum(1 for p in members if p.success) / len(members)
        gap = abs(mean_conf - accuracy)
        ece += (len(members) / total) * gap
        table.append({
            "bin": f"{lo:.1f}-{hi:.1f}", "count": len(members),
            "mean_conf": round(mean_conf, 4), "accuracy": round(accuracy, 4), "gap": round(gap, 4),
        })
    return ece, table


def compute_auc(pairs: list[Pair]) -> float | None:
    """Rank-based AUC: can confidence discriminate success from failure?

    Needs both classes present (bad_rate > 0 and < 1). Returns None otherwise.
    """
    pos = [p.confidence for p in pairs if p.success]
    neg = [p.confidence for p in pairs if not p.success]
    if not pos or not neg:
        return None
    # Mann-Whitney U via average ranks, tie-aware.
    scored = sorted(((p.confidence, p.success) for p in pairs), key=lambda x: x[0])
    ranks: list[float] = [0.0] * len(scored)
    i = 0
    while i < len(scored):
        j = i
        while j + 1 < len(scored) and scored[j + 1][0] == scored[i][0]:
            j += 1
        avg_rank = (i + j) / 2.0 + 1.0  # 1-based average rank for the tie group
        for k in range(i, j + 1):
            ranks[k] = avg_rank
        i = j + 1
    sum_ranks_pos = sum(r for r, (_, succ) in zip(ranks, scored) if succ)
    n_pos, n_neg = len(pos), len(neg)
    u = sum_ranks_pos - n_pos * (n_pos + 1) / 2.0
    return u / (n_pos * n_neg)


def snapshot_tactical(client: GovernanceClient) -> dict:
    res = client.call("calibration", {"action": "check"})
    te = res.get("tactical_evidence", {}) or {}
    health = (res.get("per_channel_health") or {}).get("tests", {}) or {}
    fm = (((res.get("calibration_guidance") or {}).get("failure_modes") or {}).get("tactical") or {})
    return {
        "eligible_samples": te.get("eligible_samples"),
        "signal_sources": te.get("signal_sources"),
        "tests_bad_rate": health.get("bad_rate"),
        "tests_samples": health.get("samples"),
        "server_ece": fm.get("ece"),
        "server_total_samples": fm.get("total_samples"),
    }


def emit(agent_uuids: list[str], before: dict, after: dict) -> dict:
    pairs = fetch_pairs(agent_uuids)
    ece, table = compute_ece(pairs)
    auc = compute_auc(pairs)
    n_bad = sum(1 for p in pairs if not p.success)

    print("\n================ CALIBRATION HARNESS v1 — REPORT ================")
    print("NOTE: synthetic fixture. ECE/AUC describe the harness's measurement,")
    print("      NOT any agent's real calibration. Confidences are drawn to land")
    print("      in target bins behind known outcomes.\n")

    print(f"harness rows: {len(pairs)}  (failures={n_bad}, bad_rate={n_bad/len(pairs):.3f})" if pairs else "harness rows: 0")
    print(f"harness-computed ECE (registered confidence): {ece:.4f}")
    print(f"harness-computed AUC (discrimination):        {auc if auc is None else round(auc, 4)}"
          + ("  <- bad_rate=0, not computable" if auc is None else ""))

    print("\nreliability table (expected vs actual per bin):")
    print(f"  {'bin':<10} {'n':>4} {'mean_conf':>10} {'accuracy':>9} {'gap':>7}")
    for r in table:
        mc = "-" if r["mean_conf"] is None else f"{r['mean_conf']:.3f}"
        ac = "-" if r["accuracy"] is None else f"{r['accuracy']:.3f}"
        gp = "-" if r["gap"] is None else f"{r['gap']:.3f}"
        print(f"  {r['bin']:<10} {r['count']:>4} {mc:>10} {ac:>9} {gp:>7}")

    print("\nserver tactical channel (isolated instance, before -> after):")
    print(f"  eligible_samples: {before['eligible_samples']} -> {after['eligible_samples']}")
    print(f"  tests bad_rate:   {before['tests_bad_rate']} -> {after['tests_bad_rate']}")
    print(f"  server tactical ECE: {before['server_ece']} -> {after['server_ece']}")

    print("\nv1 success criteria:")
    print(f"  [{'x' if pairs and n_bad > 0 else ' '}] bad_rate > 0 -> AUC computable")
    print(f"  [{'x' if auc is not None else ' '}] AUC computed")
    print(f"  [{'x' if (after['eligible_samples'] or 0) > (before['eligible_samples'] or 0) else ' '}] tactical channel moved")
    print("================================================================\n")
    return {"ece": ece, "auc": auc, "rows": len(pairs), "bad": n_bad, "table": table}
