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
import sys
import psycopg2

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


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--db-url", default="postgresql://postgres:postgres@localhost:5432/governance")
    args = ap.parse_args()
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
    conn.close()

    print()
    if overlap < 20:
        print(f"VERDICT: B not yet falsifiable — overlap={overlap}. The anchored-outcome")
        print("population and the EISV/baseline population are effectively disjoint.")
        print("Prerequisite (deeper than 'wire test_failed'): EISV-bearing agents must")
        print("RECEIVE and SNAPSHOT externally-anchored outcomes. Until then B's §6.3 gate")
        print("is structurally uncomputable — the validation-gap pathology, by construction.")
        return 0
    print(f"overlap={overlap} ≥ 20 — residual-vs-Φ AUC is now computable; wire it here.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
