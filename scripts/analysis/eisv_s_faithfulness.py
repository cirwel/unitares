#!/usr/bin/env python3
"""S-axis test-retest reliability and faithfulness under intervention (offline).

The contract (``docs/ontology/eisv-proprioception-contract.md``, *The split*)
assigns S a label-free validation regime: test-retest reliability (same state,
same reading) and faithfulness under intervention (inject a known change; does
S respond with the predicted sign). This script runs both against the real
estimator path, ``UNITARESMonitor.process_update``, through synthetic check-in
streams, and measures ``metrics["S"]``: the value an agent is shown.

It reuses the hermetic harness of ``tests/eisv_preservation_driver.py``:
injected clock, empty calibration, audit log and drift telemetry redirected to a
temporary directory, ``load_state=False``. Nothing touches the database, Redis or
a live data directory, and no row enters the registered 2026-12-01 series.
It makes no claim about outcomes.

Streams are 70 check-ins: baseline 0-29, intervention 30-44, recovery 45-69.
Each condition runs on independent seeds. Interventions:

* positive controls: overclaimed complexity (+0.45, and a 0.10/0.20/0.30 dose
  series), confidence collapse with a claimed-complexity jump, thrash
  (alternating terse/confident and heavy/unsure reports);
* negative controls: no change, vocabulary-only change (sham), and an honestly
  reported shift to heavier work.

``--ablate-regime`` is a counterfactual, not a proposal: it holds
``_regime_instability`` (35% of raw S) at its documented low default, to measure
the loop in which ``detect_regime`` labels regimes from behavioral S's own
step-to-step change and S then counts the label flips.

Usage:
    python3 scripts/analysis/eisv_s_faithfulness.py
    python3 scripts/analysis/eisv_s_faithfulness.py --ablate-regime
    python3 scripts/analysis/eisv_s_faithfulness.py --no-deployed-flags --out /tmp/s.json

Result record: ``docs/evaluations/eisv-s-faithfulness/result-v0.md``.
"""

from __future__ import annotations

import argparse
import json
import math
import os
import random
import statistics as st
import subprocess
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]

# The estimator flags the live governance-MCP plist sets (read 2026-10-12). The
# driver strips every UNITARES_/GOVERNANCE_ variable; these are added back so the
# run measures the deployed configuration. On this path they change nothing
# (S_SETPOINT and PHI_TELEMETRY_ONLY already default on; the setpoint and sensor
# coupling act on the diagnostic ODE), which --no-deployed-flags demonstrates.
DEPLOYED_FLAGS = {
    "UNITARES_CLASS_CALIBRATION": "~/.unitares/class-calibration.json",
    "UNITARES_COHERENCE_GATE_SHADOW": "1",
    "UNITARES_GROUNDING_SHADOW": "1",
    "UNITARES_PHI_TELEMETRY_ONLY": "1",
    "UNITARES_S_SETPOINT": "1",
    "UNITARES_SENSOR_COUPLING": "behavioral_only",
}

N_STEPS = 70
INT_START, INT_END = 30, 45

VERBS_A = ["edited", "updated", "reworked", "adjusted", "cleaned up", "renamed"]
VERBS_B = ["revised", "restructured", "amended", "tidied", "refactored", "retitled"]
OBJECTS_A = ["the parser", "two handlers", "the config loader", "a migration helper",
             "the retry logic", "the cache layer", "the CLI flags"]
OBJECTS_B = ["the tokenizer", "a pair of routes", "the settings reader", "an upgrade shim",
             "the backoff code", "the memo store", "the command options"]
CHECKS = ["ran pytest on the touched module", "ran ruff and fixed two warnings",
          "checked the diff with git", "re-ran the focused tests"]


# ── worker: runs inside the hermetic subprocess ─────────────────────────────

def _worker(out_path: str, seeds: int, ablate_regime: bool) -> None:
    from contextlib import ExitStack
    from unittest.mock import patch

    sys.path.insert(0, str(REPO))
    import tests.eisv_preservation_driver as d
    from src.dual_log.continuity import derive_complexity
    from src.dual_log.operational import create_operational_entry

    def derived(text):
        return derive_complexity(create_operational_entry("exp", text, "s"))

    def clip(x):
        return max(0.0, min(1.0, x))

    def moderate(rng, verbs=VERBS_A, objects=OBJECTS_A):
        sents = [f"I {rng.choice(verbs)} {rng.choice(objects)} and {rng.choice(CHECKS)}."
                 for _ in range(rng.randint(3, 5))]
        return " ".join(sents) + f"\n\nNext I will look at {rng.choice(objects)} because it shares the same pattern."

    def terse(rng):
        return f"{rng.choice(VERBS_A).capitalize()} {rng.choice(OBJECTS_A)}, tests pass."

    def heavy(rng):
        paras = [" ".join(f"I {rng.choice(VERBS_A)} {rng.choice(OBJECTS_A)} and {rng.choice(CHECKS)}."
                          for _ in range(rng.randint(4, 7))) for _ in range(rng.randint(4, 6))]
        items = "\n".join(f"- {rng.choice(OBJECTS_A)}: {rng.choice(CHECKS)}" for _ in range(rng.randint(5, 9)))
        code = "```python\ndef f(x):\n    return x + 1\n```"
        return "\n\n".join(paras[:2] + [items, code] + paras[2:]
                           + ["Is the retry budget still right? Should the cache be bounded?"])

    texts = {"moderate": moderate, "terse": terse, "heavy": heavy}
    confs = {"moderate": 0.75, "terse": 0.85, "heavy": 0.65}

    def steady(kind):
        def gen(rng, i):
            text = texts[kind](rng)
            return text, clip(derived(text) + rng.gauss(0, 0.03)), clip(confs[kind] + rng.gauss(0, 0.03))
        return gen

    def with_intervention(during):
        base = steady("moderate")
        return lambda rng, i: during(rng, i) if INT_START <= i < INT_END else base(rng, i)

    def sham(rng, i):
        text = moderate(rng, VERBS_B, OBJECTS_B)
        return text, clip(derived(text) + rng.gauss(0, 0.03)), clip(0.75 + rng.gauss(0, 0.03))

    def overclaim_by(offset):
        def gen(rng, i):
            text = moderate(rng)
            return text, clip(derived(text) + offset), clip(0.75 + rng.gauss(0, 0.03))
        return gen

    def conf_collapse(rng, i):
        return moderate(rng), clip(0.9 + rng.gauss(0, 0.03)), clip(0.3 + rng.gauss(0, 0.03))

    def thrash(rng, i):
        if i % 2:
            text = terse(rng)
            return text, clip(derived(text) + rng.gauss(0, 0.03)), 0.9
        text = heavy(rng)
        return text, clip(derived(text) + rng.gauss(0, 0.03)), 0.35

    def honest_shift(rng, i):
        text = heavy(rng)
        return text, clip(derived(text) + rng.gauss(0, 0.03)), clip(0.75 + rng.gauss(0, 0.03))

    steady_regimes = {k: steady(k) for k in texts}
    interventions = {
        "control": steady("moderate"),
        "sham_paraphrase": with_intervention(sham),
        "overclaim_complexity": with_intervention(overclaim_by(0.45)),
        "confidence_collapse": with_intervention(conf_collapse),
        "thrash": with_intervention(thrash),
        "honest_work_shift": with_intervention(honest_shift),
        "overclaim_0.10": with_intervention(overclaim_by(0.10)),
        "overclaim_0.20": with_intervention(overclaim_by(0.20)),
        "overclaim_0.30": with_intervention(overclaim_by(0.30)),
    }

    def run_stream(name, gen, seed):
        rng = random.Random(f"{name}:{seed}")
        d._reset_process_global_state()
        d._Clock.now = d.START
        monitor = d._new_monitor(f"exp-{name}-{seed}")
        rows = []
        for i in range(N_STEPS):
            text, claim, conf = gen(rng, i)
            cap = d._step(monitor, seconds=rng.uniform(30, 90), text=text, complexity=claim, confidence=conf)
            rows.append({
                "i": i, "S": cap["S"], "E": cap["E"], "I": cap["I"], "V": cap["V"],
                "risk": cap["risk_score"], "verdict": cap["verdict"], "action": cap["action"],
                "regime": cap["regime"], "obs_source": cap["behavioral"]["obs_source"],
                "derived": cap["continuity"]["derived_complexity"],
                "divergence": cap["continuity"]["complexity_divergence"],
            })
        return rows

    results = {"steady": {}, "intervention": {}}
    with d.injected_environment(), ExitStack() as stack:
        if ablate_regime:
            stack.enter_context(patch("src.behavioral_sensor._regime_instability", lambda h: 0.1))
        for name, gen in steady_regimes.items():
            results["steady"][name] = [run_stream(f"steady-{name}", gen, s) for s in range(seeds)]
        for name, gen in interventions.items():
            results["intervention"][name] = [run_stream(f"int-{name}", gen, s) for s in range(seeds)]
    Path(out_path).write_text(json.dumps(results))


# ── analysis ────────────────────────────────────────────────────────────────

def _m(xs):
    return st.fmean(xs)


def _sd(xs):
    return st.stdev(xs) if len(xs) > 1 else 0.0


def analyze(results: dict) -> str:
    out = []
    sources = sorted({run[-1]["obs_source"] for g in results.values() for runs in g.values() for run in runs})
    out.append(f"observation source at the last step of every stream: {sources}")
    out.append("")
    out.append("Test-retest: steady-state mean over steps 30-69, per regime (mean ± SD across seeds)")
    for ax in ("S", "E", "I", "V"):
        per = {k: [_m([r[ax] for r in run[30:]]) for run in runs] for k, runs in results["steady"].items()}
        k = len(next(iter(per.values())))
        g = len(per)
        grand = _m([x for v in per.values() for x in v])
        msb = k * sum((_m(v) - grand) ** 2 for v in per.values()) / (g - 1)
        msw = sum((x - _m(v)) ** 2 for v in per.values() for x in v) / (g * (k - 1))
        icc = (msb - msw) / (msb + (k - 1) * msw) if msb + (k - 1) * msw > 0 else float("nan")
        step_sd = _m([_sd([r[ax] for r in run[30:]]) for runs in results["steady"].values() for run in runs])
        out.append(f"  {ax}: " + "  ".join(f"{n}={_m(v):.4f}±{_sd(v):.4f}" for n, v in per.items())
                   + f"  ICC(1)={icc:.3f}  within-run step SD={step_sd:.4f}")
    out.append("")
    out.append("Faithfulness: change vs own baseline (20-29); during = 32-44, recovery = 60-69; "
               "difference from control by seed index (independent streams)")
    ctrl = results["intervention"]["control"]

    def deltas(runs, ax, a, b):
        return [_m([r[ax] for r in run[a:b]]) - _m([r[ax] for r in run[20:30]]) for run in runs]

    for ax in ("S", "E", "I", "risk"):
        out.append(f"  {ax}")
        c = deltas(ctrl, ax, 32, 45)
        for name, runs in results["intervention"].items():
            dd = deltas(runs, ax, 32, 45)
            diff = [x - y for x, y in zip(dd, c)]
            se = _sd(diff) / math.sqrt(len(diff))
            out.append(f"    {name:22s} vs control {_m(diff):+.4f} (se {se:.4f})  "
                       f"recovery Δ {_m(deltas(runs, ax, 60, 70)):+.4f}")
    out.append("")
    window = {}
    for runs in results["intervention"].values():
        for run in runs:
            for r in run[INT_START:INT_END]:
                key = f"{r['verdict']}/{r['action']}"
                window[key] = window.get(key, 0) + 1
    out.append(f"verdict/action over every intervention-window check-in: {window}")
    c_sd = _m([_sd([r["S"] for r in run[30:]]) for run in ctrl])
    flips = _m([sum(1 for i in range(31, N_STEPS) if run[i]["regime"] != run[i - 1]["regime"]) for run in ctrl])
    out.append(f"control: within-run S step SD (30-69) {c_sd:.4f}; regime flips per 39 steps {flips:.1f}")
    return "\n".join(out)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--seeds", type=int, default=8)
    ap.add_argument("--ablate-regime", action="store_true",
                    help="Counterfactual: hold the regime-instability term at its low default")
    ap.add_argument("--no-deployed-flags", action="store_true",
                    help="Run with code defaults instead of the deployed estimator flags")
    ap.add_argument("--out", help="Also write the raw per-step streams as JSON")
    ap.add_argument("--worker", nargs=3, metavar=("OUT", "SEEDS", "ABLATE"), help=argparse.SUPPRESS)
    args = ap.parse_args()

    if args.worker:
        _worker(args.worker[0], int(args.worker[1]), args.worker[2] == "1")
        return 0

    sys.path.insert(0, str(REPO))
    from tests.eisv_preservation_driver import strip_config_env

    env = strip_config_env(dict(os.environ))
    if not args.no_deployed_flags:
        env.update({k: os.path.expanduser(v) for k, v in DEPLOYED_FLAGS.items()})
    env["PYTHONPATH"] = str(REPO)
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    with tempfile.TemporaryDirectory() as tmp:
        raw = args.out or str(Path(tmp) / "streams.json")
        proc = subprocess.run(
            [sys.executable, str(Path(__file__).resolve()), "--worker", raw, str(args.seeds),
             "1" if args.ablate_regime else "0"],
            env=env, cwd=str(REPO), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        if proc.returncode != 0:
            print(f"worker failed with exit {proc.returncode}", file=sys.stderr)
            return proc.returncode
        print(analyze(json.loads(Path(raw).read_text())))
    return 0


if __name__ == "__main__":
    sys.exit(main())
