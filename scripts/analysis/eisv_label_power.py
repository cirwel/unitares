#!/usr/bin/env python3
"""Label power / MDE analysis — how many exogenous bad labels does gate-3 need?

The EISV design tournament's "pooled killer experiment": convert the
unvalidatable Stage-B falsifiability gate into a NUMBER. Gate-3 asks whether an
EISV/residual model's AUC (predicting bad outcomes) beats a baseline. Whether
that is even *detectable* is a power question set by the count of exogenous
**bad** labels (the minority class), which is what we are starved of.

This computes, with NO new data:
  - Hanley-McNeil SE(AUC) as a function of (n_bad, n_good).
  - MDE: the minimum AUC lift over chance (0.5) detectable at the given power.
  - Whether the operationally-relevant target — beating the *previous-outcome*
    (autocorrelation) baseline, whose decontaminated AUC is ~0.94 — is reachable
    at all (headroom 1-0.94 = 0.06 vs the MDE).
  - The n_bad required to detect a meaningful lift (default 0.05).

Defaults reflect the live decontaminated pooled set (joinable, trusted+soft):
n_bad=114, n_good=2287; the skeptic eval's *scored test slice* is ~21 bad.

Method note: Hanley-McNeil gives the SE of a single AUC. For "beat baseline B"
we treat detectability conservatively as resolving the model's AUC against the
fixed value B (one-sample), MDE = (z_alpha + z_beta) * SE(AUC=B). A paired
DeLong test with positive correlation would need somewhat fewer labels, so this
is a conservative-but-honest floor, not a tight bound — and the conclusion
(massively underpowered) holds with margin.

Usage:
    PYTHONPATH=. python3 scripts/analysis/eisv_label_power.py
    PYTHONPATH=. python3 scripts/analysis/eisv_label_power.py --n-bad 21 --n-good 563
    PYTHONPATH=. python3 scripts/analysis/eisv_label_power.py --baseline-auc 0.94 --target-lift 0.05
"""
from __future__ import annotations

import argparse
import math

# z(0.95)=1.6449 one-sided alpha=0.05 ; z(0.80)=0.8416 power=80%
Z_ALPHA_1SIDED = 1.6449
Z_ALPHA_2SIDED = 1.9600
Z_POWER_80 = 0.8416


def auc_se(auc: float, n_bad: int, n_good: int) -> float:
    """Hanley-McNeil standard error of an AUC. n_bad = positives (minority)."""
    if n_bad < 1 or n_good < 1:
        return float("nan")
    q1 = auc / (2 - auc)
    q2 = 2 * auc * auc / (1 + auc)
    var = (
        auc * (1 - auc)
        + (n_bad - 1) * (q1 - auc * auc)
        + (n_good - 1) * (q2 - auc * auc)
    ) / (n_bad * n_good)
    return math.sqrt(max(var, 0.0))


def mde_over_chance(n_bad: int, n_good: int, z_alpha: float = Z_ALPHA_1SIDED) -> float:
    """Smallest true AUC above 0.5 detectable at 80% power. Solve A-0.5=k*SE(A)."""
    k = z_alpha + Z_POWER_80
    a = 0.5
    for _ in range(200):  # fixed-point: A = 0.5 + k*SE(A)
        a_next = 0.5 + k * auc_se(a, n_bad, n_good)
        if abs(a_next - a) < 1e-9:
            break
        a = a_next
    return a - 0.5


def n_bad_for_lift(lift: float, n_good: int, baseline: float,
                   z_alpha: float = Z_ALPHA_1SIDED, ratio_cap: int = 25) -> int:
    """Smallest n_bad so MDE (over the baseline value) <= the target lift.

    Holds the good:bad ratio at min(observed, ratio_cap) so n_good scales with
    n_bad rather than assuming an unlimited supply of negatives.
    """
    k = z_alpha + Z_POWER_80
    ratio = min(ratio_cap, max(1, n_good))
    for nb in range(2, 200001):
        ng = nb * ratio
        if k * auc_se(baseline + lift, nb, ng) <= lift:
            return nb
    return -1


def build_report(args) -> str:
    nb, ng = args.n_bad, args.n_good
    a: list[str] = []
    a.append("# EISV label power / MDE — can gate-3 even be tested?\n")
    a.append(f"Pooled exogenous labels (joinable, trusted+soft): **n_bad={nb}, n_good={ng}** "
             f"(skeptic scored-test slice ~21 bad).  Power=80%, alpha=0.05 one-sided.\n")

    a.append("## SE(AUC) at the current label budget")
    a.append("| assumed true AUC | SE (n_bad={}) | SE (n_bad=21) | SE (n_bad=500) |".format(nb))
    a.append("|---:|---:|---:|---:|")
    for A in (0.55, 0.70, 0.85, 0.94):
        a.append(f"| {A:.2f} | {auc_se(A, nb, ng):.3f} | {auc_se(A, 21, 21*20):.3f} | "
                 f"{auc_se(A, 500, 500*20):.3f} |")

    mde_now = mde_over_chance(nb, ng)
    mde_21 = mde_over_chance(21, 21 * 20)
    a.append("\n## Minimum detectable AUC lift over chance (0.5)")
    a.append(f"- at the full pooled budget (n_bad={nb}): **+{mde_now:.3f}**")
    a.append(f"- at the skeptic scored slice (n_bad=21): **+{mde_21:.3f}**")
    a.append("\nMeaning: an EISV model must clear roughly these margins over 0.5 just "
             "to be distinguishable from a coin — before any comparison to a real baseline.")

    base = args.baseline_auc
    headroom = 1.0 - base
    mde_vs_base = (Z_ALPHA_1SIDED + Z_POWER_80) * auc_se(base, nb, ng)
    base_ci_now = Z_ALPHA_2SIDED * auc_se(base, nb, ng)
    base_ci_21 = Z_ALPHA_2SIDED * auc_se(base, 21, 21 * 20)
    a.append("\n## The operationally-relevant bar: beat the autocorrelation baseline")
    a.append(f"The decontaminated previous-outcome (autocorrelation) baseline AUC is "
             f"**~{base:.2f}** — entire headroom above it is **{headroom:.2f}**.")
    a.append(f"- the baseline AUC is itself only known to **+/-{base_ci_now:.3f}** at "
             f"n_bad={nb} (and +/-{base_ci_21:.3f} at the n_bad=21 scored slice).")
    a.append(f"- naive one-sample MDE to resolve a model against {base:.2f} (n_bad={nb}): "
             f"+{mde_vs_base:.3f}")
    a.append(
        "\n**Do NOT read the small MDE here as 'reachable'.** It is small for two "
        "misleading reasons, not because the test is feasible:\n"
        "  1. AUC variance COLLAPSES toward the 1.0 ceiling (Hanley-McNeil), so any "
        "comparison pinned near 0.94 looks cheap — an artifact of being near the "
        "ceiling, not of having signal.\n"
        f"  2. The baseline is not a fixed target: it is itself estimated on the same "
        f"~21 bad labels (+/-{base_ci_21:.3f}) AND swings from ~0.61 (contaminated "
        "slice) to ~0.94 (clean slice). You cannot 'beat by +0.05' a target whose own "
        "CI is wider than 0.05. The honest paired comparison (DeLong) is dominated by "
        "this baseline uncertainty, which the one-sample MDE ignores."
    )

    a.append("\n## How many bad labels would a meaningful lift need? (one-sample, optimistic)")
    for lift in (0.10, 0.05, 0.03):
        need = n_bad_for_lift(lift, ng, base)
        a.append(f"- +{lift:.2f} lift over a {base:.2f} baseline at 80% power: "
                 f"~{need} bad labels (have {nb}) — optimistic; ignores baseline CI")
    a.append("These counts are a LOWER bound. They assume a fixed, perfectly-known "
             "baseline near the ceiling; the real (paired, baseline-uncertain) "
             "requirement is materially larger.")

    a.append("\n## Reading (decision-relevant)")
    a.append(
        "Gate-3 is a minority-class problem; power is set by the scarce BAD-label "
        "count. Two robust facts survive the caveats:\n"
        f"  - At the realistic SCORED slice (~21 bad), you cannot even establish that "
        f"an EISV model beats a COIN unless its lift exceeds **+{mde_21:.3f}** — and "
        "the skeptic eval already found no feature beats the baseline at all.\n"
        "  - The comparison target (autocorrelation AUC) is both very high (~0.94) and "
        "unpinnable (0.61–0.94 across slices, CI wider than any plausible EISV lift). "
        "There is no stable thing to beat.\n"
        "Conclusion: Stage-B / GROUNDING_APPLY is NOT validatable on outcomes at this "
        "label supply — by arithmetic, independent of the maths. The decision now "
        "hinges on the *other* killer experiment, the latent-supply count: if the "
        "fleet cannot emit clean bad labels at a rate that reaches the hundreds on a "
        "reasonable horizon, EISV is plausibly unfalsifiable-on-outcomes and the "
        "grounding program — not just Stage B — deserves reconsideration."
    )
    return "\n".join(a) + "\n"


def parse_args(argv=None):
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--n-bad", type=int, default=114, help="exogenous bad labels (minority class)")
    p.add_argument("--n-good", type=int, default=2287, help="exogenous good labels")
    p.add_argument("--baseline-auc", type=float, default=0.94,
                   help="AUC of the previous-outcome/autocorrelation baseline to beat")
    p.add_argument("--target-lift", type=float, default=0.05)
    return p.parse_args(argv)


def main(argv=None) -> int:
    print(build_report(parse_args(argv)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
