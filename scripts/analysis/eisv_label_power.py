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
is a floor, not a tight bound.

Do not read a conclusion out of this note. An earlier version ended it with
"and the conclusion (massively underpowered) holds with margin" -- a fixed claim
in the docstring of a script whose whole job is to compute whether that holds,
and one the commensurate arithmetic does not support at the shipped default.

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
    """Hanley-McNeil standard error of an AUC. n_bad = positives (minority).

    AUC outside [0, 1] is not a domain error to paper over: the variance goes
    negative there, and the `max(var, 0.0)` clamp below used to turn that into
    SE = 0.0 -- an answer indistinguishable from a perfectly precise estimate.
    `n_bad_for_lift` then read that zero as "the target is already detectable"
    and returned its loop floor of 2. Return NaN instead, so an out-of-domain
    call propagates as unknown rather than as good news.
    """
    if n_bad < 1 or n_good < 1:
        return float("nan")
    if not (0.0 <= auc <= 1.0):
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


# n_bad_for_lift sentinels. Two different failures that must not share a label:
N_BAD_OUT_OF_DOMAIN = -1   # target AUC > 1.0 -- not an AUC at all
N_BAD_APPROX_INVALID = -2  # target AUC == 1.0 -- a real AUC the approximation cannot cost


def mde_vs_baseline(baseline: float, n_bad: int, n_good: int,
                    z_alpha: float = Z_ALPHA_1SIDED) -> float:
    """MDE for resolving a model's AUC against the FIXED value `baseline`.

    THIS is the estimand commensurate with the headroom (`1 - baseline`): both
    are AUC distances measured from the baseline, so "does the MDE fit in the
    headroom?" is a question about one axis.

    `mde_over_chance` is NOT commensurate with headroom. It measures from 0.5,
    so comparing it to `1 - baseline` compares a distance-from-chance to a
    distance-from-baseline. At n_bad=50, n_good=1000, baseline=0.94 the two
    differ by nearly 2x (0.108 vs 0.058) and straddle the 0.06 headroom, so the
    substitution flips the verdict. Report both; never compare them.

    Still one-sample and still optimistic: it treats `baseline` as perfectly
    known when it is estimated on the same scarce slice. See `_conclusion`.
    """
    return (z_alpha + Z_POWER_80) * auc_se(baseline, n_bad, n_good)


def n_bad_for_lift(lift: float, n_good: int, baseline: float,
                   z_alpha: float = Z_ALPHA_1SIDED, ratio_cap: int = 25) -> int:
    """Smallest n_bad so MDE (over the baseline value) <= the target lift.

    Holds the good:bad ratio at min(observed, ratio_cap) so n_good scales with
    n_bad rather than assuming an unlimited supply of negatives.

    Returns N_BAD_OUT_OF_DOMAIN or N_BAD_APPROX_INVALID rather than a count
    when the target is not costable -- see the constants above.
    """
    k = z_alpha + Z_POWER_80
    target = baseline + lift
    # Above 1.0 there is no such AUC, so no sample size buys it.
    if target > 1.0:
        return N_BAD_OUT_OF_DOMAIN
    # Exactly 1.0 is a legitimate AUC, and auc_se accepts it -- but the
    # Hanley-McNeil variance collapses to exactly 0 there for EVERY n, so the
    # loop would return its floor of 2 and report perfect separation as the
    # cheapest thing to demonstrate. That is a degenerate approximation, not a
    # reachable target and not an out-of-domain one; it gets its own status
    # rather than being folded into either.
    if target == 1.0:
        return N_BAD_APPROX_INVALID
    ratio = min(ratio_cap, max(1, n_good))
    for nb in range(2, 200001):
        ng = nb * ratio
        if k * auc_se(target, nb, ng) <= lift:
            return nb
    return N_BAD_OUT_OF_DOMAIN


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
    mde_vs_base = mde_vs_baseline(base, nb, ng)
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
        if need == N_BAD_OUT_OF_DOMAIN:
            answer = f"NOT REACHABLE — target AUC {base + lift:.2f} is above 1.0"
        elif need == N_BAD_APPROX_INVALID:
            answer = ("NOT COSTABLE — target AUC is exactly 1.0, where the "
                      "Hanley-McNeil variance is 0 for every n")
        else:
            answer = f"~{need} bad labels (have {nb}) — optimistic; ignores baseline CI"
        a.append(f"- +{lift:.2f} lift over a {base:.2f} baseline at 80% power: {answer}")
    a.append("These counts are a LOWER bound. They assume a fixed, perfectly-known "
             "baseline near the ceiling; the real (paired, baseline-uncertain) "
             "requirement is materially larger.")

    a.append("\n## Reading (decision-relevant)")
    a.append(
        "Gate-3 is a minority-class problem; power is set by the scarce BAD-label "
        "count. Two facts from the numbers above:\n"
        f"  - Against CHANCE, at the realistic SCORED slice (~21 bad), a lift below "
        f"**+{mde_21:.3f}** is not distinguishable from a coin. (Distance from 0.5 — "
        "not comparable to the headroom below.)\n"
        f"  - Against the BASELINE, the commensurate quantity is **+{mde_vs_base:.3f}**, "
        f"to be read against the **{headroom:.3f}** of headroom above {base:.2f}. The "
        f"baseline is itself known only to +/-{base_ci_now:.3f} here and swings across "
        "slices, so this comparison is optimistic in a direction the arithmetic below "
        "accounts for.\n"
        f"{_conclusion(mde_vs_base, headroom, base_ci_21)}"
    )
    return "\n".join(a) + "\n"


def _conclusion(mde_vs_base: float, headroom: float, baseline_ci: float) -> str:
    """Derive the verdict from the numbers above it.

    This paragraph used to be a constant: `build_report` contained no branch on
    any computed quantity, so it printed "NOT validatable" for every input --
    including a label budget large enough to make it false.

    It then briefly branched on the WRONG quantity. `mde_over_chance` measures
    from 0.5 and `headroom` measures from the baseline, so the comparison mixed
    two axes. At n_bad=50, n_good=1000, baseline=0.94 the chance-MDE (0.108)
    exceeds the 0.06 headroom while the commensurate baseline-MDE (0.058) fits
    inside it -- the substitution flipped the verdict. `mde_vs_baseline` is now
    the input, which is what the surrounding prose was always describing.

    Two branches, because "fits" and "established" are not the same claim:
      - wider than the headroom -> NOT validatable, and robustly so: this is
        the OPTIMISTIC approximation, and every correction (paired design,
        baseline uncertainty) makes the requirement larger, never smaller.
      - fits inside the headroom -> NOT ESTABLISHED, not "resolvable". The
        one-sample form treats the baseline as a fixed known constant while it
        is estimated on the same scarce slice; a categorical powered verdict
        needs a paired estimand carrying that covariance, and no such design is
        registered. Saying "resolvable" on the optimistic number would be the
        overclaim this script exists to prevent.
    """
    if mde_vs_base > headroom:
        return (
            "Conclusion: Stage-B / GROUNDING_APPLY is NOT validatable on outcomes at "
            f"this label supply — the MDE against the baseline (**+{mde_vs_base:.3f}**) "
            f"is wider than the entire headroom above it (**{headroom:.3f}**). This is "
            "the optimistic one-sample form, so the honest paired requirement is "
            "strictly larger: the verdict does not depend on pinning the baseline. The "
            "decision then hinges on the *other* killer experiment, the latent-supply "
            "count: if the fleet cannot emit clean bad labels at a rate that reaches "
            "the hundreds on a reasonable horizon, EISV is plausibly "
            "unfalsifiable-on-outcomes and the grounding program — not just Stage B — "
            "deserves reconsideration."
        )
    ci_note = ""
    if baseline_ci > headroom:
        # Stated as a fact, not applied as a rule. eisv-grounding-next-move-v0.md
        # treats "the baseline's own CI is wider than the headroom" as decisive.
        # That may well be right — but promoting it to a verdict here would be
        # this printout picking the deciding standard, which is the operator's.
        ci_note = (
            f" Note the specific obstacle: the baseline's own CI "
            f"(+/-{baseline_ci:.3f}) is WIDER than the entire headroom "
            f"({headroom:.3f}), so there is no stable target at this slice. "
            "eisv-grounding-next-move-v0.md treats that as decisive; whether it is "
            "remains a registered choice, and not one this script makes."
        )
    return (
        f"Conclusion: NOT ESTABLISHED. The optimistic one-sample MDE "
        f"(**+{mde_vs_base:.3f}**) does fit inside the headroom above the baseline "
        f"(**{headroom:.3f}**), so this label supply is no longer ruled out by "
        "arithmetic alone — but that is not the same as powered. The one-sample form "
        "treats the baseline as a perfectly-known constant, while it is estimated on "
        f"the same scarce slice (+/-{baseline_ci:.3f} at the ~21-bad scored slice) and "
        "moves across slices. Resolving a model against it is a PAIRED comparison whose "
        f"power depends on the covariance between the two AUCs, and no such design is "
        f"registered here.{ci_note} What is established: the arithmetic kill no longer "
        "applies. What is not: that the comparison is powered. Specifying the paired "
        "estimand is the next step, and it is an operator call — this printout must not "
        "make it by reporting the optimistic number as a verdict."
    )


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
