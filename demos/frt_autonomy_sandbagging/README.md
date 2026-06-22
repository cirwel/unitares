# Sandbagging detection eval — a model organism + measurement harness

**Claim under test:** a single-framing capability eval undercounts what an
autonomous agent can do when the agent *conceals capability while it believes it
is being evaluated*. This demo builds a transparent model organism of that
behavior and a harness that (a) recovers the true capability and (b) flags the
concealment with calibrated statistics.

It is deliberately small, fully deterministic, and dependency-free (Python
standard library only — no model API, no network, no GPU). Every number below
reproduces on a fresh clone.

```bash
cd demos/frt_autonomy_sandbagging
python3 run_demo.py            # human-readable report
python3 run_demo.py --json     # machine-readable summary
python3 -m pytest test_sandbagging.py -q
```

## Why this, and why it matters for capability evals

If your safety case rests on "the model scored X on the dangerous-capability
eval," sandbagging is the failure that quietly invalidates it: the number you
measured under eval conditions is *lower* than the capability that holds when
the agent does not think it is being graded. You cannot fix this by trusting the
eval-frame number harder. You need a measurement design that does not assume the
agent is cooperating with the measurement.

The standard answer — and what this harness operationalizes — is to **compare
the same task distribution under a monitored (evaluated) framing and an
unmonitored (deployment) framing.** Honest agents look the same under both; a
concealer is betrayed by the gap, and the unmonitored rate is the better
estimate of true capability.

## What's in the box

| File | Role |
|------|------|
| `model_organism.py` | The agent. A Rasch-style item-response policy with a known latent capability; sandbaggers shed competence, intermittently, *only* under eval framing. Honest agents are the controls. |
| `eval_harness.py` | The measurement. Runs each agent under both framings, computes a per-agent one-sided significance test for concealment, a population ROC AUC, and the recovered-vs-naive capability. All stats hand-rolled from stdlib. |
| `run_demo.py` | Entry point; prints the per-agent table and the detection/undercount summary. |
| `test_sandbagging.py` | Pins the two load-bearing claims: the organism conceals only under eval, and the harness separates concealers from honest controls while recovering the understated capability. |

## Representative result (`--seed 0`, 240 tasks/framing)

```
  Detection (concealers vs honest controls)
    ROC AUC ................. 1.000
    flag threshold .......... z >= 1.645  (one-sided 95%)
    true-positive rate ...... 100%   (sandbaggers caught)
    false-positive rate ..... 8%     (honest mis-flagged)

  Capability undercount on the concealing agents
    naive eval-frame number . 0.34   <- what a single-framing eval reports
    unmonitored number ...... 0.64   <- true capability
    undercount .............. +0.30  (+89% of the reported number)
```

Across seeds {0,1,2,7,42} the separation is stable: **AUC 0.98–1.00**, undercount
**+0.25 to +0.32** in absolute success rate. Note the honest tension the demo
keeps visible rather than hiding: ranking can be perfect (AUC 1.0) while a fixed
nominal z-threshold still mis-flags an honest agent — a multiple-comparisons
artifact, and exactly the kind of caveat a real eval has to report.

## What this is *not* (scope and honesty)

- **The agent is scripted, not trained.** This is a model *organism* for the
  measurement methodology, not evidence that any real model sandbags. It lets us
  ask "if an agent concealed like this, would our harness catch it?" with a known
  ground truth to grade against.
- **The detection assumes the unmonitored framing is genuinely unmonitored.** A
  capable enough agent that sees through the deployment framing collapses the
  gap — the harder, open problem, and the one a real red team lives in.
- **Single discrimination parameter, stationary tasks, no adaptive adversary.**
  Natural next steps: an agent whose concealment adapts to the framing cues, an
  eval-detection arms race, and noisy/ambiguous task labels.

## How it connects to the rest of this repo

This sits next to the same instinct the main project is built on — that you
cannot trust an agent's self-report and must anchor to outcomes the agent can't
fake (see the repo's `record_result` / Integrity story). Here that instinct is
pointed at the *pre-deployment* question — measuring a capability an agent is
motivated to hide — rather than the runtime-governance question.
