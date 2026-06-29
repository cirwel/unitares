#!/usr/bin/env python3
"""Latent exogenous bad-label supply — honest-floor Milestone 1 (read-only).

The design tournament (docs/proposals/eisv-grounding-next-move-v0.md) made this
the first decision-gate: before building any label-capture plumbing, measure how
many genuinely-exogenous, attributable BAD labels the fleet could supply per
quarter. Gate-3 power is set by the bad (minority) class; this counts its supply.

Three read-only sources, no new instrumentation, no writes:
  A. REALIZED outcome_events bad labels, split by trust tier (external_signal =
     TRUSTED vs agent_reported_tool_result = SOFT/self-attested), synthetic
     (BEAM smoke) excluded — the labels already flowing.
  B. Uncaptured operator-correction proxies that WOULD be trusted if wired:
     git reverts (a revert is an exogenous "that was bad" signal) and
     closed-unmerged PRs (operator rejection).
  C. The deeper bottlenecks volume cannot fix: per-agent balanced coverage and
     the autocorrelation baseline (cross-referenced from the power analysis).

Verdict logic: passing a raw >=30/quarter volume floor is necessary but NOT
sufficient — the script reports whether the TRUSTED + per-agent-balanced supply
(not raw volume) clears what gate-3 actually needs.

Usage:
    PYTHONPATH=. python3 scripts/analysis/eisv_latent_label_supply.py
    PYTHONPATH=. python3 scripts/analysis/eisv_latent_label_supply.py --repo /path/to/repo --gh-repo cirwel/unitares
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PROJECT_ROOT))

DEFAULT_DB_URL = "postgresql://postgres:postgres@localhost:5432/governance"
FLOOR_PER_QTR = 30          # tournament Milestone-1 volume gate
POWER_NEED_BAD = 99         # n_bad for a +0.03 lift vs baseline (eisv_label_power.py, optimistic)


WINDOW_DAYS = 90  # fixed lookback so a count IS ~per-quarter (no per-subset extrapolation)


def _q(cur, sql):
    cur.execute(sql)
    return cur.fetchall()


def _to_qtr(count: int, window_days: int) -> float:
    return count * 90.0 / max(window_days, 1)


def db_realized(db_url: str, window_days: int = WINDOW_DAYS) -> dict:
    import psycopg2
    conn = psycopg2.connect(db_url)
    try:
        cur = conn.cursor()
        clean = ("is_bad AND eisv_e IS NOT NULL "
                 "AND coalesce((detail->>'snapshot_missing')::bool,false)=false "
                 "AND coalesce(detail->>'harness','')<>'beam' "
                 "AND coalesce(detail->>'kind','') NOT ILIKE '%smoke%'")
        # Count over a FIXED recent window so the rate is robust to bursts.
        rows = _q(cur, f"""
            SELECT verification_source, count(*)
            FROM audit.outcome_events
            WHERE {clean} AND ts > now() - interval '{window_days} days'
              AND verification_source IN ('external_signal','agent_reported_tool_result')
            GROUP BY 1""")
        # per-agent balanced coverage on pooled trusted+soft joinable (all-time fact)
        bal = _q(cur, """
            WITH a AS (SELECT agent_id,
                              sum((NOT is_bad)::int) good, sum((is_bad)::int) bad
                       FROM audit.outcome_events
                       WHERE verification_source IN ('external_signal','agent_reported_tool_result')
                         AND eisv_e IS NOT NULL
                         AND coalesce((detail->>'snapshot_missing')::bool,false)=false
                       GROUP BY agent_id)
            SELECT count(*) FILTER (WHERE bad>=5 AND good>=5),
                   count(*) FILTER (WHERE bad>=1) FROM a""")[0]
        return {"rows": rows, "window_days": window_days,
                "balanced_agents": int(bal[0]), "agents_with_bad": int(bal[1])}
    finally:
        conn.close()


def git_reverts(repo: str, window_days: int = WINDOW_DAYS) -> int | None:
    try:
        out = subprocess.run(
            ["git", "-C", repo, "log", f"--since={window_days} days ago", "-i",
             "--grep=^revert", "--oneline"],
            capture_output=True, text=True, timeout=30)
        if out.returncode != 0:
            return None
        return len([ln for ln in out.stdout.splitlines() if ln.strip()])
    except Exception:
        return None


def gh_rejected_prs(gh_repo: str, window_days: int = WINDOW_DAYS) -> tuple[int, bool] | None:
    """(rejected_count_in_window, truncated) over a FIXED recent window."""
    try:
        from datetime import date, timedelta
        cutoff = (date.today() - timedelta(days=window_days)).isoformat()
        limit = 2000
        out = subprocess.run(
            ["gh", "pr", "list", "-R", gh_repo, "--state", "closed",
             "--search", f"closed:>={cutoff}", "--limit", str(limit), "--json", "mergedAt"],
            capture_output=True, text=True, timeout=90)
        if out.returncode != 0:
            return None
        data = json.loads(out.stdout)
        rejected = sum(1 for p in data if not p.get("mergedAt"))
        return rejected, len(data) >= limit
    except Exception:
        return None


def build_report(db: dict, reverts, prs, repo: str, gh_repo: str) -> str:
    a: list[str] = []
    a.append("# Latent exogenous bad-label supply (honest-floor Milestone 1)\n")
    a.append("Read-only. Estimates clean exogenous BAD labels per quarter — gate-3's "
             "scarce minority class — across realized + uncaptured-proxy sources.\n")

    win = db["window_days"]
    tier = {r[0]: r[1] for r in db["rows"]}
    soft_q = _to_qtr(tier.get("agent_reported_tool_result", 0), win)
    trust_q = _to_qtr(tier.get("external_signal", 0), win)

    a.append(f"## A. Realized (already flowing), by trust tier — last {win}d")
    a.append("| tier | ~per quarter | usable for gate-3? |")
    a.append("|---|---:|---|")
    a.append(f"| SOFT (agent self-attested) | {soft_q:.0f} | only opt-in; gameable |")
    a.append(f"| TRUSTED (external_signal) | {trust_q:.0f} | yes, but vanishingly small |")

    rev_q = _to_qtr(reverts, win) if reverts is not None else None
    pr_rej, pr_trunc = (prs if prs else (None, None))
    pr_q = _to_qtr(pr_rej, win) if pr_rej is not None else None
    plus = "+" if pr_trunc else ""
    rev_cell = f"{rev_q:.0f}" if rev_q is not None else "n/a"
    pr_cell = f"{pr_q:.0f}{plus}" if pr_q is not None else "n/a"
    a.append("\n## B. Uncaptured operator-correction proxies (TRUSTED-eligible if wired)")
    a.append(f"| source | last {win}d | ~per quarter |")
    a.append("|---|---:|---:|")
    a.append(f"| git reverts ({Path(repo).name}) | {reverts if reverts is not None else 'n/a'} | {rev_cell} |")
    a.append(f"| closed-unmerged PRs ({gh_repo}) | {pr_rej if pr_rej is not None else 'n/a'}{plus} | {pr_cell} |")

    trusted_eligible_q = trust_q + (rev_q or 0) + (pr_q or 0)
    a.append(f"\n**TRUSTED-eligible supply ≈ {trusted_eligible_q:.0f}/quarter** "
             f"(realized trusted {trust_q:.0f} + reverts {rev_q or 0:.0f} + rejected-PRs {pr_q or 0:.0f}). "
             f"SOFT adds ~{soft_q:.0f}/quarter but is self-attested.")

    a.append("\n## C. The bottlenecks volume cannot fix")
    a.append(f"- per-agent BALANCED coverage (good≥5 AND bad≥5): **{db['balanced_agents']} agents** "
             f"(of {db['agents_with_bad']} with any bad). Gate-3 is per-agent; more volume on "
             "1–3 committing agents does not create balanced agents.")
    a.append("- the uncaptured proxies need a commit→agent attribution map that does not exist "
             "(CI/git authorship ≠ governance agent_id).")
    a.append("- the autocorrelation baseline (~0.94, unpinnable) is unbeatable on outcomes — "
             "see eisv_label_power.py. More labels do not lower that bar.")

    a.append("\n## Verdict")
    vol_pass = trusted_eligible_q >= FLOOR_PER_QTR
    a.append(f"- raw volume floor (≥{FLOOR_PER_QTR}/qtr TRUSTED-eligible): "
             f"**{'PASS' if vol_pass else 'FAIL'}** ({trusted_eligible_q:.0f}/qtr)")
    a.append(f"- but the gate-3-relevant supply (TRUSTED **and** per-agent-balanced): "
             f"**FAIL** — {db['balanced_agents']} balanced agents.")
    a.append(
        "\n**Honest read: supply is not the binding constraint — distribution and tier are.** "
        f"Raw bad-event supply clears the {FLOOR_PER_QTR}/quarter floor (SOFT ~{soft_q:.0f}, "
        f"TRUSTED-eligible ~{trusted_eligible_q:.0f}), so 'capture more labels' is the WRONG "
        "lever: the trustworthy slice is tiny, it concentrates on a handful of committing "
        "agents (0 balanced), the proxies lack agent attribution, and the autocorrelation "
        "baseline is unbeatable regardless. This refines Milestone 1: the gate is not volume "
        "but TRUSTED + per-agent-balanced volume — which is not met and is not closed by "
        "building capture plumbing. It pushes toward the preserved dissent: EISV may be "
        "unfalsifiable-on-outcomes for structural reasons, and the grounding program — not "
        "just Stage B — deserves reconsideration. The one capture leg still worth building "
        "(operator-correction → trusted, with attribution) is justified for FUTURE optionality, "
        "not as a near-term unblock."
    )
    return "\n".join(a) + "\n"


def parse_args(argv=None):
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--db-url", default=DEFAULT_DB_URL)
    p.add_argument("--repo", default=str(PROJECT_ROOT), help="git repo for revert counting")
    p.add_argument("--gh-repo", default="cirwel/unitares")
    p.add_argument("--window-days", type=int, default=WINDOW_DAYS,
                   help="fixed lookback window for all rates (count scaled to /quarter)")
    return p.parse_args(argv)


def main(argv=None) -> int:
    args = parse_args(argv)
    db = db_realized(args.db_url, args.window_days)
    reverts = git_reverts(args.repo, args.window_days)
    prs = gh_rejected_prs(args.gh_repo, args.window_days)
    print(build_report(db, reverts, prs, args.repo, args.gh_repo))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
