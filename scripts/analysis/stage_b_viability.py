#!/usr/bin/env python3
"""Stage B viability probe — can the per-agent residual be validated against
externally-anchored outcomes *at all* yet?

B's falsifiability gate (roadmap §6.3) needs the SAME agents to have (a) a
behavioral baseline (→ residual) and (b) externally-anchored outcomes (→ ground
truth label). This probe measures that overlap first, and only computes the
AUC(residual vs Φ) comparison if the overlap is non-empty.

Finding (2026-06-25): the two are **disjoint populations** — externally-anchored
outcomes carry no EISV and come from agents with no baseline, while EISV-rich
residents receive only self-referential outcomes (Invariant-4 excluded). So B is
not yet falsifiable for a data reason, not a thin-label reason. This probe is the
regression guard: when the overlap appears, it starts emitting the AUC.
"""
from __future__ import annotations

import argparse
import json
import sys

DIAG = {
    "anchored outcomes (external_signal)":
        "SELECT count(*) FROM audit.outcome_events WHERE verification_source='external_signal'",
    "  ...carrying EISV (eisv_phi not null)":
        "SELECT count(*) FROM audit.outcome_events WHERE verification_source='external_signal' AND eisv_phi IS NOT NULL",
    "distinct agents with anchored outcomes":
        "SELECT count(DISTINCT agent_id) FROM audit.outcome_events WHERE verification_source='external_signal'",
    "  ...that have ANY EISV state row":
        """SELECT count(DISTINCT i.identity_id)
           FROM (SELECT DISTINCT agent_id FROM audit.outcome_events WHERE verification_source='external_signal') x
           JOIN core.identities i ON i.agent_id=x.agent_id
           JOIN core.agent_state s ON s.identity_id=i.identity_id""",
    "  ...that are BASELINED (→ residual computable)":
        """SELECT count(DISTINCT i.identity_id)
           FROM (SELECT DISTINCT agent_id FROM audit.outcome_events WHERE verification_source='external_signal') x
           JOIN core.identities i ON i.agent_id=x.agent_id
           JOIN core.agent_state s ON s.identity_id=i.identity_id
           WHERE s.state_json->'behavioral_eisv'->'warmup'->>'is_baselined'='true'""",
    "OVERLAP: anchored outcomes joinable to a prior baselined state":
        """SELECT count(*) FROM audit.outcome_events e
           JOIN core.identities i ON i.agent_id=e.agent_id
           WHERE e.verification_source='external_signal'
             AND EXISTS (SELECT 1 FROM core.agent_state s
               WHERE s.identity_id=i.identity_id AND s.recorded_at<=e.ts
               AND s.state_json->'behavioral_eisv'->'warmup'->>'is_baselined'='true')""",
}


# One row per anchored outcome, joined to the LAST baselined state at-or-before
# the outcome timestamp (the residual's information set — no lookahead).
JOIN_SQL = """
SELECT e.is_bad, e.eisv_phi, s.beh, s.state_ts
FROM audit.outcome_events e
JOIN core.identities i ON i.agent_id = e.agent_id
JOIN LATERAL (
    SELECT s.state_json->'behavioral_eisv' AS beh, s.recorded_at AS state_ts
    FROM core.agent_state s
    WHERE s.identity_id = i.identity_id AND s.recorded_at <= e.ts
      AND s.state_json->'behavioral_eisv'->'warmup'->>'is_baselined' = 'true'
    ORDER BY s.recorded_at DESC LIMIT 1
) s ON true
WHERE e.verification_source = 'external_signal'
"""

CHANNELS = ("E", "I", "S", "V")


def residual_z_norm(beh: dict) -> float | None:
    """RMS of per-channel self-relative z-scores against the agent's own
    Welford baseline (roadmap §4/§6: residual = measurement − own reference,
    scaled by own spread). None when no channel has a usable baseline."""
    stats = beh.get("baseline_stats") or {}
    zs = []
    for ch in CHANNELS:
        x = beh.get(ch)
        st = stats.get(ch) or {}
        mean, m2, n = st.get("mean"), st.get("m2"), st.get("count") or 0
        if x is None or mean is None or m2 is None or n < 2:
            continue
        var = m2 / (n - 1)
        if var <= 0:
            continue
        zs.append((float(x) - float(mean)) / var ** 0.5)
    if not zs:
        return None
    return (sum(z * z for z in zs) / len(zs)) ** 0.5


def auc(scores: list[float], labels: list[bool]) -> float | None:
    """Mann-Whitney AUC — P(score_bad > score_good), average-rank ties.
    Direction: higher score should mean worse (is_bad=True)."""
    n_bad = sum(labels)
    n_good = len(labels) - n_bad
    if n_bad == 0 or n_good == 0:
        return None
    order = sorted(range(len(scores)), key=lambda k: scores[k])
    ranks = [0.0] * len(scores)
    i = 0
    while i < len(order):
        j = i
        while j + 1 < len(order) and scores[order[j + 1]] == scores[order[i]]:
            j += 1
        avg = (i + j) / 2 + 1  # 1-based average rank across the tie block
        for k in range(i, j + 1):
            ranks[order[k]] = avg
        i = j + 1
    rank_sum_bad = sum(r for r, is_bad in zip(ranks, labels) if is_bad)
    return (rank_sum_bad - n_bad * (n_bad + 1) / 2) / (n_bad * n_good)


def permutation_p(scores: list[float], labels: list[bool], observed: float,
                  n_perm: int = 10000, seed: int = 0) -> float:
    """One-sided permutation p: chance of an AUC >= observed under shuffled
    labels. With very few positives this is the honest significance measure."""
    import random
    rng = random.Random(seed)
    lab = list(labels)
    hits = 0
    for _ in range(n_perm):
        rng.shuffle(lab)
        a = auc(scores, lab)
        if a is not None and a >= observed:
            hits += 1
    return (hits + 1) / (n_perm + 1)


def emit_auc(conn) -> None:
    with conn.cursor() as cur:
        cur.execute(JOIN_SQL)
        rows = cur.fetchall()

    residuals, phis, labels, clusters = [], [], [], []
    skipped = 0
    for is_bad, phi, beh, state_ts in rows:
        if isinstance(beh, str):
            beh = json.loads(beh)
        r = residual_z_norm(beh or {})
        if r is None or phi is None:
            skipped += 1
            continue
        residuals.append(r)
        phis.append(float(phi))
        labels.append(bool(is_bad))
        clusters.append(state_ts)

    n, n_bad = len(labels), sum(labels)
    n_clusters = len(set(clusters))
    bad_clusters = len({c for c, is_bad in zip(clusters, labels) if is_bad})
    print()
    print("=== §6.3 falsifier — residual vs Φ on externally-anchored outcomes ===")
    print(f"  usable rows {n} ({n_bad} bad / {n - n_bad} good; {skipped} skipped: no residual/Φ)")
    print(f"  distinct prior-state snapshots joined: {n_clusters} "
          f"({bad_clusters} carrying the bad labels)")
    a_res = auc(residuals, labels)
    a_phi = auc(phis, labels)
    if a_res is None or a_phi is None:
        print("  AUC undefined — need at least one bad and one good label.")
        return
    p_res = permutation_p(residuals, labels, a_res)
    p_phi = permutation_p(phis, labels, a_phi)
    print(f"  AUC(residual z-norm) = {a_res:.3f}   (perm p={p_res:.3f})")
    print(f"  AUC(absolute Φ)      = {a_phi:.3f}   (perm p={p_phi:.3f})")
    print()
    lead = "residual > Φ" if a_res > a_phi else ("Φ > residual" if a_phi > a_res else "tie")
    print(f"  Direction on this sample: {lead}. CAVEATS: rows sharing a prior-state")
    print(f"  snapshot are NOT independent — adjudication batches join to one state,")
    print(f"  so the effective sample is ~{n_clusters} clusters (bad labels in "
          f"{bad_clusters}), and the")
    print("  permutation p above ignores that clustering (anti-conservative).")
    print("  Labels are single-channel (Watcher adjudications), effectively")
    print("  single-agent. This is an instrument check, not a verdict on B —")
    print("  the gate needs breadth (more residents, channels, and adjudication")
    print("  batches) before either direction is claimable.")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--db-url", default="postgresql://postgres:postgres@localhost:5432/governance")
    args = ap.parse_args()
    import psycopg2  # lazy: keeps the pure math importable in test envs without the driver
    conn = psycopg2.connect(args.db_url)

    print("=== Stage B viability: EISV ∩ exogenous-anchor population overlap ===")
    overlap = 0
    with conn.cursor() as cur:
        for label, sql in DIAG.items():
            cur.execute(sql)
            v = cur.fetchone()[0]
            print(f"  {label:<52} {v}")
            if label.startswith("OVERLAP"):
                overlap = v

    print()
    if overlap < 20:
        conn.close()
        print(f"VERDICT: B not yet falsifiable — overlap={overlap}. The anchored-outcome")
        print("population and the EISV/baseline population are effectively disjoint.")
        print("Prerequisite (deeper than 'wire test_failed'): EISV-bearing agents must")
        print("RECEIVE and SNAPSHOT externally-anchored outcomes. Until then B's §6.3 gate")
        print("is structurally uncomputable — the validation-gap pathology, by construction.")
        return 0
    print(f"overlap={overlap} ≥ 20 — computing the residual-vs-Φ AUC.")
    emit_auc(conn)
    conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
