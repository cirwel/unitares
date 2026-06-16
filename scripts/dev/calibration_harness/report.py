"""The proof (v1.1). Validates the calibration MEASUREMENT by recovering a known
injected miscalibration, then shows the server's tactical channel before/after.

Primary check (self-contained, server-independent): bin on the STATED confidence
the harness controlled, compute ECE/AUC from the Bernoulli outcomes, and compare
the recovered ECE to the analytic injected ECE. If they match within sampling
noise, report.py's measurement math is sound. AUC should exceed 0.5 because the
injected curve makes success rise with confidence.

Secondary view: the server's tactical channel (global, registered/capped
confidence) before vs after — what the server actually scored, distorted by the
0.55 cap + corrector. The gap between primary and secondary IS the finding.
"""
from __future__ import annotations

from dataclasses import dataclass

from .client import GovernanceClient
from .config import BINS
from .miscalibration import expected_recovered_ece, injected_ece


@dataclass
class Pair:
    confidence: float
    success: bool


def compute_ece(pairs: list[Pair]) -> tuple[float, list[dict]]:
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
        g = abs(mean_conf - accuracy)
        ece += (len(members) / total) * g
        table.append({
            "bin": f"{lo:.1f}-{hi:.1f}", "count": len(members),
            "mean_conf": round(mean_conf, 4), "accuracy": round(accuracy, 4), "gap": round(g, 4),
        })
    return ece, table


def compute_auc(pairs: list[Pair]) -> float | None:
    """Tie-aware rank AUC. None unless both classes present."""
    pos = [p for p in pairs if p.success]
    neg = [p for p in pairs if not p.success]
    if not pos or not neg:
        return None
    scored = sorted(pairs, key=lambda p: p.confidence)
    ranks = [0.0] * len(scored)
    i = 0
    while i < len(scored):
        j = i
        while j + 1 < len(scored) and scored[j + 1].confidence == scored[i].confidence:
            j += 1
        avg_rank = (i + j) / 2.0 + 1.0
        for k in range(i, j + 1):
            ranks[k] = avg_rank
        i = j + 1
    sum_ranks_pos = sum(r for r, p in zip(ranks, scored) if p.success)
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
        "tests_bad_rate": health.get("bad_rate"),
        "server_ece": fm.get("ece"),
    }


def emit(rows, gap: float, before: dict, after: dict) -> dict:
    pairs = [Pair(confidence=r.stated_confidence, success=not r.is_bad) for r in rows]
    confidences = [p.confidence for p in pairs]
    recovered_ece, table = compute_ece(pairs)
    injected = injected_ece(confidences, gap)              # bias-free: what we injected
    expected = expected_recovered_ece(confidences, gap, BINS)  # bias-aware: what a binned estimator should report
    auc = compute_auc(pairs)
    n_bad = sum(1 for p in pairs if not p.success)
    ece_err = abs(recovered_ece - expected)
    ok_ece = ece_err <= 0.05  # vs the bias-aware target, robust at any gap

    print("\n================ CALIBRATION HARNESS v1.1 — REPORT ================")
    print("Synthetic fixture. Outcomes drawn from an INJECTED curve")
    print(f"  true_accuracy(c) = clamp(c - {gap}), so a correct ECE estimate should")
    print("  recover ~ the injected ECE, and AUC should exceed 0.5.\n")

    print(f"rows: {len(pairs)}  failures: {n_bad}  bad_rate: {n_bad/len(pairs):.3f}" if pairs else "rows: 0")
    print("\nPRIMARY — measurement validation (binned on STATED confidence):")
    print(f"  injected ECE (bias-free):       {injected:.4f}   <- the miscalibration we injected")
    print(f"  expected ECE (bias-aware floor): {expected:.4f}   <- what a binned estimator should report at this n")
    print(f"  recovered ECE (measured):       {recovered_ece:.4f}   (|err vs expected|={ece_err:.4f})")
    print(f"  AUC: {auc if auc is None else round(auc, 4)}"
          + ("  <- one class only" if auc is None else "  (>0.5 expected)"))

    print("\nreliability table (stated confidence; accuracy should track c-gap):")
    print(f"  {'bin':<10} {'n':>4} {'mean_conf':>10} {'accuracy':>9} {'gap':>7}")
    for r in table:
        mc = "-" if r["mean_conf"] is None else f"{r['mean_conf']:.3f}"
        ac = "-" if r["accuracy"] is None else f"{r['accuracy']:.3f}"
        gp = "-" if r["gap"] is None else f"{r['gap']:.3f}"
        print(f"  {r['bin']:<10} {r['count']:>4} {mc:>10} {ac:>9} {gp:>7}")

    print("\nSECONDARY — server tactical channel (registered/capped confidence, global):")
    print(f"  eligible_samples: {before['eligible_samples']} -> {after['eligible_samples']}")
    print(f"  tests bad_rate:   {before['tests_bad_rate']} -> {after['tests_bad_rate']}")
    print(f"  server tactical ECE: {before['server_ece']} -> {after['server_ece']}")

    print("\nv1.1 success criteria:")
    print(f"  [{'x' if ok_ece else ' '}] recovered ECE matches bias-aware expected within 0.05 (measurement sound)")
    print(f"  [{'x' if auc is not None and auc > 0.5 else ' '}] AUC > 0.5 (confidence now discriminates)")
    print(f"  [{'x' if (after['eligible_samples'] or 0) > (before['eligible_samples'] or 0) else ' '}] tactical channel moved")
    print("==================================================================\n")
    return {"injected_ece": injected, "expected_ece": expected, "recovered_ece": recovered_ece,
            "ece_err": ece_err, "auc": auc, "rows": len(pairs), "ok": ok_ece}
