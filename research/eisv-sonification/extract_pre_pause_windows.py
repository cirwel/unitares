"""
Extract REAL pre-pause EISV windows from the governance DB.

The sonification prototype (sonify.py) shipped with a *synthetic* pause episode
because, at the time, no real pre-pause trajectory was thought to be available.
It turns out the trajectory is already persisted: every check-in writes a row to
`core.agent_state` with E, I/S/V (under `behavioral_eisv`), phi, risk_score,
verdict, and the decision `action`. Pauses are just rows whose action is one of
risk_pause / cirs_block / void_pause / coherence_pause. So pre-pause windows do
not need new *retention* — they need *retrieval*. This script is that retrieval.

It does NOT touch the running server and writes nothing to the DB: read-only.
(Deliberately not a check-in-path snapshot — adding an unguarded write to the
mandatory update path is the exact pattern that took the fleet down in #780/#800.)

What it does:
  1. Find pauses that are *transitions into pause* — the preceding check-in was a
     proceed (approve/guide). A sustained-pause plateau has nothing to "hear
     coming"; a transition does.
  2. For each, pull the preceding N check-ins + the pause row(s) + a short tail.
  3. Tier each episode by field completeness:
       TIER_A  full 4-D EISV across the runway (behavioral_eisv present) — renders
               as sonify.py was designed to.
       TIER_B  only E + risk persisted in the runway — degraded 2-D render.
  4. Emit pre_pause_windows.json (episodes + an honesty manifest), and with
     --render, write one WAV per TIER_A episode via sonify.py.

Honesty notes baked into the manifest:
  - `coherence` is NOT persisted in modern agent_state rows (only 227 legacy ODE
    rows have it). We do NOT fabricate it: states omit coherence, so sonify.py
    falls back to its neutral 0.5 default. `phi` is carried as a diagnostic only,
    never silently substituted for coherence.
  - behavioral_eisv became 100%-always-on the week of 2026-06-08; before that it
    is sparse. So historical TIER_A fuel is thin and clusters on whichever agents
    paused after that date. New pauses are auto-TIER_A — fuel grows by accumulation.

Usage:
  python3 extract_pre_pause_windows.py                 # write pre_pause_windows.json + manifest
  python3 extract_pre_pause_windows.py --render        # also render TIER_A episodes to WAV
  python3 extract_pre_pause_windows.py --pre 12 --tail 2 --min-runway 4
"""
import argparse, json, subprocess, sys

PAUSE_ACTIONS = ("risk_pause", "cirs_block", "void_pause", "coherence_pause")
HEALTHY_ACTIONS = ("approve", "guide")


def psql_json(db, sql):
    """Run a query that returns a single JSON value; parse and return it."""
    wrapped = f"SELECT coalesce(json_agg(_r), '[]'::json) FROM ({sql}) _r;"
    out = subprocess.run(
        ["psql", "-d", db, "-At", "-c", wrapped],
        capture_output=True, text=True,
    )
    if out.returncode != 0:
        sys.exit(f"psql failed:\n{out.stderr}")
    return json.loads(out.stdout.strip() or "[]")


def find_transition_pauses(db, min_runway, pre):
    """Pause events whose preceding check-in was healthy, ranked by 4-D coverage."""
    pause_in = ",".join(f"'{a}'" for a in PAUSE_ACTIONS)
    healthy_in = ",".join(f"'{a}'" for a in HEALTHY_ACTIONS)
    sql = f"""
      WITH seq AS (
        SELECT identity_id, recorded_at,
               state_json->>'action' AS act,
               (state_json ? 'behavioral_eisv') AS has_beisv,
               CASE WHEN state_json->>'action' IN ({healthy_in}) THEN 1 ELSE 0 END AS healthy,
               row_number() OVER (PARTITION BY identity_id ORDER BY recorded_at) AS rn
        FROM core.agent_state WHERE state_json ? 'action'
      ),
      ev AS (SELECT *, lag(act) OVER (PARTITION BY identity_id ORDER BY recorded_at) AS prev_act FROM seq)
      SELECT e.identity_id, e.recorded_at::text AS pause_at, e.act AS pause_action,
        (SELECT count(*) FROM ev p
           WHERE p.identity_id=e.identity_id AND p.rn < e.rn AND p.rn >= e.rn-{pre} AND p.healthy=1) AS runway,
        (SELECT count(*) FROM ev p
           WHERE p.identity_id=e.identity_id AND p.rn <= e.rn AND p.rn >= e.rn-{pre} AND p.has_beisv) AS beisv_rows
      FROM ev e
      WHERE e.act IN ({pause_in}) AND e.prev_act IN ({healthy_in})
    """
    rows = psql_json(db, sql)
    rows = [r for r in rows if r["runway"] >= min_runway]
    rows.sort(key=lambda r: (r["beisv_rows"], r["runway"]), reverse=True)
    return rows


def fetch_window(db, identity_id, pause_at, pre, tail):
    """Pull the pre-pause runway + pause row + a short tail for one episode."""
    sql = f"""
      WITH win AS (
        SELECT recorded_at, state_json,
               (recorded_at <= timestamptz '{pause_at}') AS at_or_before
        FROM core.agent_state
        WHERE identity_id={identity_id}
          AND recorded_at BETWEEN timestamptz '{pause_at}' - interval '3 hours'
                              AND timestamptz '{pause_at}' + interval '1 hour'
          AND state_json ? 'E'
      ),
      before AS (
        SELECT * FROM win WHERE at_or_before ORDER BY recorded_at DESC LIMIT {pre + 1}
      ),
      after AS (
        SELECT * FROM win WHERE NOT at_or_before ORDER BY recorded_at ASC LIMIT {tail}
      )
      SELECT recorded_at::text AS t,
             (state_json->>'E')::float AS "E",
             coalesce(state_json->'behavioral_eisv'->>'I', state_json->>'I')::float AS "I",
             coalesce(state_json->'behavioral_eisv'->>'S', state_json->>'S')::float AS "S",
             coalesce(state_json->'behavioral_eisv'->>'V', state_json->>'V')::float AS "V",
             (state_json->>'phi')::float AS phi,
             (state_json->>'risk_score')::float AS risk,
             state_json->>'verdict' AS verdict,
             coalesce(state_json->>'action','-') AS action
      FROM (SELECT * FROM before UNION ALL SELECT * FROM after) u
      ORDER BY recorded_at
    """
    return psql_json(db, sql)


def to_states(rows):
    """Map DB rows -> sonify.py state dicts. Omits coherence (not persisted)."""
    states = []
    for r in rows:
        s = {"E": r["E"], "risk": r["risk"]}
        for k in ("I", "S", "V"):
            if r[k] is not None:
                s[k] = r[k]
        # coherence intentionally omitted -> sonify falls back to neutral 0.5.
        states.append(s)
    return states


def tier(rows):
    """TIER_A iff every runway row carries full I/S/V; else TIER_B."""
    full = all(r["I"] is not None and r["S"] is not None and r["V"] is not None for r in rows)
    return "TIER_A" if full else "TIER_B"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default="governance")
    ap.add_argument("--pre", type=int, default=12, help="check-ins before the pause")
    ap.add_argument("--tail", type=int, default=2, help="check-ins after the pause")
    ap.add_argument("--min-runway", type=int, default=4, help="min healthy runway to keep an episode")
    ap.add_argument("--limit", type=int, default=20, help="max episodes")
    ap.add_argument("--render", action="store_true", help="render TIER_A episodes to WAV via sonify.py")
    args = ap.parse_args()

    cands = find_transition_pauses(args.db, args.min_runway, args.pre)[: args.limit]
    print(f"Found {len(cands)} transition-into-pause episodes (runway >= {args.min_runway}):")

    episodes = []
    for c in cands:
        rows = fetch_window(args.db, c["identity_id"], c["pause_at"], args.pre, args.tail)
        if not rows:
            continue
        t = tier([r for r in rows if r["action"] in HEALTHY_ACTIONS] or rows)
        ep = {
            "identity_id": c["identity_id"],
            "pause_at": c["pause_at"],
            "pause_action": c["pause_action"],
            "runway": c["runway"],
            "tier": t,
            "n_rows": len(rows),
            "rows": rows,
            "states": to_states(rows),
        }
        episodes.append(ep)
        print(f"  id={c['identity_id']:<6} {c['pause_at'][:19]}  {c['pause_action']:<15} "
              f"runway={c['runway']:<2} {t}  ({len(rows)} rows)")

    tier_a = [e for e in episodes if e["tier"] == "TIER_A"]
    tier_b = [e for e in episodes if e["tier"] == "TIER_B"]
    agents_a = sorted({e["identity_id"] for e in tier_a})

    manifest = {
        "generated_against": args.db,
        "params": {"pre": args.pre, "tail": args.tail, "min_runway": args.min_runway},
        "counts": {"episodes": len(episodes), "tier_a": len(tier_a), "tier_b": len(tier_b)},
        "tier_a_agents": agents_a,
        "honesty": {
            "coherence": "NOT persisted in modern rows; omitted from states (sonify uses neutral 0.5). phi carried as diagnostic only, never substituted.",
            "tier_a_meaning": "full 4-D EISV across the runway — renders as sonify.py was designed.",
            "tier_b_meaning": "only E + risk persisted in the runway — degraded 2-D render; I/S/V absent.",
            "fuel_caveat": ("TIER_A fuel is thin/narrow: behavioral_eisv went always-on the week of "
                            "2026-06-08, so historical 4-D pauses cluster on whichever agents paused "
                            "after that date. New pauses are auto-TIER_A — the study's fuel grows by "
                            "accumulation, not by adding instrumentation."),
        },
    }

    with open("pre_pause_windows.json", "w") as f:
        json.dump({"manifest": manifest, "episodes": episodes}, f, indent=2)
    print(f"\nwrote pre_pause_windows.json  "
          f"({len(tier_a)} TIER_A across agents {agents_a or '—'}, {len(tier_b)} TIER_B)")

    if args.render:
        import sonify
        print("\nRendering TIER_A real pre-pause episodes:")
        for e in tier_a:
            stamp = e["pause_at"][:16].replace(" ", "T").replace(":", "")
            name = f"real_prepause_id{e['identity_id']}_{e['pause_action']}_{stamp}.wav"
            sonify.sonify(e["states"], name)
        if not tier_a:
            print("  (no TIER_A episodes to render)")


if __name__ == "__main__":
    main()
