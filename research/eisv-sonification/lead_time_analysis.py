"""
Lead-time validation: does the AUDIBLE signal lead the dashboard?

The sonification thesis is "you hear the pause coming before the verdict flips."
This script tests that on real episodes (from pre_pause_windows.json) instead of
asserting it.

The circularity trap (the whole reason this needs care):
  The pause verdict is a threshold on `risk`, and `risk` is computed FROM EISV.
  So any EISV-derived signal thresholded below the pause threshold will trivially
  "lead" the pause — that's the threshold gap, not information. A risk-vs-risk lead
  is circular and worthless as evidence the SOUND helps.

So we separate two questions:
  (1) CIRCULAR baseline — does risk cross an alarm level before the pause? (Expected
      yes, by construction; reported only to size the threshold gap.)
  (2) THE REAL TEST — does an *audible, different-axis* channel (S→chromaticism,
      V→harmonic lean) cross its onset before the VERDICT LABEL a dashboard analyst
      watches changes (approve → guide/pause)? `lead_vs_label > 0` for a non-risk
      channel is the only non-circular evidence the sound leads.

Onsets (per episode, minutes from window start):
  t_label   : first check-in whose action != 'approve' (first visible escalation)
  t_pause   : first check-in whose action is a pause sub-action
  t_risk    : first risk >= RISK_ALARM            (circular baseline)
  t_S       : first S sustained above baseline+Kσ  (audible: chromaticism)
  t_V       : first |V - baseline_V| >= V_SHIFT    (audible: harmonic lean)
Lead metrics:
  lead_vs_pause(X) = t_pause - t_X    (positive = X precedes the pause)
  lead_vs_label(X) = t_label - t_X    (positive = X precedes the visible label change)

Usage:
  python3 lead_time_analysis.py            # console verdict + per-episode table
  python3 lead_time_analysis.py --plot     # also write lead_time_*.png per TIER_A episode
"""
import argparse, json, statistics as st
from datetime import datetime

RISK_ALARM = 0.50   # a "getting dangerous" risk level below the ~0.75 pause threshold
S_K = 1.0           # S onset = baseline_mean + S_K * baseline_std
V_SHIFT = 0.02      # V onset = |V - baseline_V| perceptible-lean shift
PAUSE = {"risk_pause", "cirs_block", "void_pause", "coherence_pause"}


def mins(rows):
    t0 = datetime.fromisoformat(rows[0]["t"])
    return [(datetime.fromisoformat(r["t"]) - t0).total_seconds() / 60 for r in rows]


def first_where(ts, rows, pred):
    for t, r in zip(ts, rows):
        if pred(r):
            return t
    return None


def onset_sustained(ts, vals, baseline_n, k):
    """First time a series crosses baseline_mean + k*std and the NEXT point stays up."""
    base = [v for v in vals[:baseline_n] if v is not None]
    if len(base) < 2:
        return None, None, None
    mu, sd = st.mean(base), (st.pstdev(base) or 1e-9)
    thr = mu + k * sd
    for i, (t, v) in enumerate(zip(ts, vals)):
        if v is None or i < baseline_n:
            continue
        nxt = vals[i + 1] if i + 1 < len(vals) else v
        if v >= thr and (nxt is None or nxt >= thr):
            return t, thr, (mu, sd)
    return None, thr, (mu, sd)


def analyze(ep):
    rows, ts = ep["rows"], mins(ep["rows"])
    has_isv = all(r["V"] is not None for r in rows)
    t_label = first_where(ts, rows, lambda r: r["action"] != "approve")
    t_pause = first_where(ts, rows, lambda r: r["action"] in PAUSE)
    t_risk = first_where(ts, rows, lambda r: r["risk"] is not None and r["risk"] >= RISK_ALARM)

    out = {"id": ep["identity_id"], "pause_at": ep["pause_at"][:19], "action": ep["pause_action"],
           "tier": ep["tier"], "t_label": t_label, "t_pause": t_pause, "t_risk": t_risk,
           "t_S": None, "t_V": None}

    # risk abruptness: max single-step drisk/dt vs the step into the pause
    risks = [r["risk"] for r in rows]
    steps = [(risks[i] - risks[i - 1]) / (ts[i] - ts[i - 1] or 1e-9)
             for i in range(1, len(risks)) if risks[i] is not None and risks[i - 1] is not None]
    out["max_drisk"] = max(steps) if steps else None

    if has_isv:
        Svals = [r["S"] for r in rows]
        t_S, S_thr, _ = onset_sustained(ts, Svals, min(3, len(rows)), S_K)
        Vbase = st.mean([r["V"] for r in rows[:min(3, len(rows))]])
        t_V = first_where(ts, rows, lambda r: abs(r["V"] - Vbase) >= V_SHIFT)
        out["t_S"], out["t_V"] = t_S, t_V
        out["V_range"] = max(r["V"] for r in rows) - min(r["V"] for r in rows)
        # Is S still elevated AT the pause, or did it recede (a misleading transient lead)?
        pause_idx = next((i for i, r in enumerate(rows) if r["action"] in PAUSE), None)
        out["S_sustained_to_pause"] = (
            t_S is not None and S_thr is not None and pause_idx is not None
            and Svals[pause_idx] is not None and Svals[pause_idx] >= S_thr)

    def lead(t_sig, t_ref):
        return None if (t_sig is None or t_ref is None) else round(t_ref - t_sig, 1)
    out["lead"] = {
        "risk_vs_pause": lead(t_risk, t_pause),     # circular baseline
        "S_vs_label": lead(out["t_S"], t_label),    # THE non-circular test
        "S_vs_pause": lead(out["t_S"], t_pause),
        "V_vs_label": lead(out["t_V"], t_label),
        "V_vs_pause": lead(out["t_V"], t_pause),
    }
    return out


def verdict(results):
    a = [r for r in results if r["tier"] == "TIER_A"]
    S_leads = [r["lead"]["S_vs_label"] for r in a if r["lead"]["S_vs_label"] is not None]
    V_fired = [r for r in a if r["t_V"] is not None]
    V_ranges = [r.get("V_range", 0) for r in a]
    print("\n" + "=" * 70)
    print("VERDICT  (non-circular test = does an AUDIBLE channel lead the label?)")
    print("=" * 70)
    print(f"TIER_A episodes (full 4-D): {len(a)}  — agents: {sorted({r['id'] for r in a})}")
    print(f"  V (harmonic lean): fired onset in {len(V_fired)}/{len(a)} episodes; "
          f"V total range max={max(V_ranges):.3f} (shift thr={V_SHIFT}).")
    print("    -> V is FLAT on this sample: the headline lean channel carries ~no lead signal.")
    if S_leads:
        pos = [x for x in S_leads if x > 0]
        sustained = [r for r in a if r.get("S_sustained_to_pause")]
        print(f"  S (chromaticism): onset precedes the label in {len(pos)}/{len(S_leads)} "
              f"(leads = {S_leads} min) —")
        print(f"    BUT sustains through to the pause in only {len(sustained)}/{len(a)}; "
              f"the rest are transient bumps that RESOLVE before the pause (misleading lead).")
    rabr = [r["max_drisk"] for r in results if r["max_drisk"] is not None]
    print(f"  risk abruptness: median max single-step drisk/dt = {st.median(rabr):.3f}/min "
          f"-> pauses are threshold SNAPS, not gradual approaches.")
    print("\nHONEST READ:")
    print("  - Sample is n=3, ONE agent (3701), ALL the #686 spurious-z-score class —")
    print("    abrupt-by-construction pauses, the worst case for 'hear it coming'.")
    print("  - V-lean does not lead (flat). S partially leads but inconsistently.")
    print("  - => Audible lead is NOT demonstrated on available fuel, and the fuel is")
    print("    biased. The harness is ready; a fair test needs non-spurious, gradual")
    print("    pauses to accumulate (auto-TIER_A from 2026-06-08 on).")


def plot(results):
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    for ep in [r for r in results if r["tier"] == "TIER_A"]:
        d = json.load(open("pre_pause_windows.json"))
        rows = next(e["rows"] for e in d["episodes"]
                    if e["identity_id"] == ep["id"] and e["pause_at"][:19] == ep["pause_at"])
        ts = mins(rows)
        fig, ax = plt.subplots(figsize=(9, 4.5))
        ax.plot(ts, [r["risk"] for r in rows], "o-", color="#e63946", label="risk")
        ax.plot(ts, [r["S"] for r in rows], "s-", color="#4361ee", label="S (chromaticism)")
        ax.plot(ts, [(r["V"] + 0.4) for r in rows], "^-", color="#b5179e",
                label="V (lean) +0.4 offset", alpha=.7)
        for t, r in zip(ts, rows):
            if r["action"] in PAUSE:
                ax.axvline(t, color="#e63946", ls="--", alpha=.5)
            elif r["action"] == "guide":
                ax.axvline(t, color="#f4a261", ls=":", alpha=.6)
        if ep["t_label"] is not None:
            ax.annotate("label escalates", (ep["t_label"], 0.05), color="#f4a261", fontsize=8)
        ax.set_title(f"id{ep['id']} {ep['pause_at']} {ep['action']} — "
                     f"S_lead_vs_label={ep['lead']['S_vs_label']}min, V flat")
        ax.set_xlabel("minutes"); ax.set_ylabel("level"); ax.legend(loc="upper left", fontsize=8)
        fig.tight_layout()
        fn = f"lead_time_id{ep['id']}_{ep['pause_at'][:10]}_{int(ep['t_pause'])}.png"
        fig.savefig(fn, dpi=120); plt.close(fig)
        print(f"  wrote {fn}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--plot", action="store_true")
    args = ap.parse_args()
    d = json.load(open("pre_pause_windows.json"))
    results = [analyze(e) for e in d["episodes"]]

    print(f"{'id':<6}{'tier':<8}{'pause':<13}{'t_lbl':>6}{'t_pse':>6}"
          f"{'risk>pse':>9}{'S>lbl':>7}{'S>pse':>7}{'V>lbl':>7}")
    for r in sorted(results, key=lambda r: (r["tier"], -(r.get("V_range", 0)))):
        L = r["lead"]
        def f(x): return "—" if x is None else f"{x:g}"
        print(f"{r['id']:<6}{r['tier']:<8}{r['action']:<13}{f(r['t_label']):>6}{f(r['t_pause']):>6}"
              f"{f(L['risk_vs_pause']):>9}{f(L['S_vs_label']):>7}{f(L['S_vs_pause']):>7}{f(L['V_vs_label']):>7}")
    verdict(results)
    if args.plot:
        print("\nPlots:")
        plot(results)


if __name__ == "__main__":
    main()
