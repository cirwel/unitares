# S axis: test-retest reliability and faithfulness under intervention (v0)

**Status:** exploratory, synthetic, not pre-registered. Run 2026-10-12 against the
live build (`19fcf68db`); the estimator path is unchanged at `6d7422734`.
**Script:** [`scripts/analysis/eisv_s_faithfulness.py`](../../../scripts/analysis/eisv_s_faithfulness.py).
**Scope:** the label-free validation regime the contract assigns to S
([`eisv-proprioception-contract.md`](../../ontology/eisv-proprioception-contract.md),
*The split*): test-retest reliability (same state, same reading) and faithfulness
under intervention (inject a known change; does S respond with the predicted
sign). No outcome labels are used and no outcome claim follows.

## Summary

- S moves in the predicted direction for every injected miscalibration or
  instability, scales with the size of an overclaim, rises within about two
  check-ins and returns most of the way within ten to fifteen after the
  intervention ends (the 0.45 overclaim still sits 0.03 above baseline at the end).
- An honestly reported shift to heavier work moved S by +0.007 (se 0.020), so S
  separates "the work changed and the agent said so" from miscalibration. A
  vocabulary-only change moved it by -0.038 (se 0.016) in live code; that shift
  disappears with the regime term held constant (below).
- Most of S's resting step-to-step variation comes from a feedback loop inside S.
  `detect_regime` labels the regime from behavioral S's own last change, and 35% of
  raw S counts how often that label flipped. Holding that term constant cut the
  resting step SD by 94% (0.0273 to 0.0016). Paired response differences are
  small next to the responses themselves (+0.029, se 0.013, for thrash, whose
  response is +0.157); all are exploratory estimates.
- Identical inputs replayed from an identical initial state give identical
  readings at every step.

## Method

The script drives `UNITARESMonitor.process_update` through synthetic check-in
streams inside the hermetic harness of `tests/eisv_preservation_driver.py`:
injected clock, empty calibration, audit log and drift telemetry redirected to a
temporary directory, `load_state=False`. Nothing touches the database, Redis or a
live data directory, and no row enters the registered 2026-12-01 series. The
measured value is `metrics["S"]`, what an agent is shown. Every stream reached the
`behavioral_sensor` observation source.

Each stream has 70 check-ins at 30 to 90 second intervals: baseline 0 to 29,
intervention 30 to 44, recovery 45 to 69. Reports are templated text whose
self-reported complexity tracks the server's text-derived complexity within
0.03, unless an intervention says otherwise. Every condition runs on 8
independent seeds. Effects are the change from each stream's own baseline (steps
20 to 29) to the intervention window (32 to 44), differenced against the control
stream with the same seed index; the streams are independent, so the pairing only
fixes the order. Standard errors are across the 8 differences.

The run used the estimator flags the live plist sets. With the code defaults
instead (`--no-deployed-flags`) every value is identical: `UNITARES_S_SETPOINT` and
`UNITARES_PHI_TELEMETRY_ONLY` already default on, and the setpoint and sensor
coupling act on the diagnostic ODE, not on behavioral S.

## Results

### Faithfulness under intervention

| Condition (steps 30 to 44) | Predicted | ΔS vs control (se) | Recovery ΔS (60 to 69) |
|---|---|---|---|
| Overclaim: self-reported complexity 0.45 above derived | rise | +0.195 (0.012) | +0.030 |
| Overclaim by 0.10 / 0.20 / 0.30 | rise with dose | +0.040 / +0.087 / +0.115 (0.013 to 0.020) | -0.061 / +0.010 / +0.004 |
| Confidence collapse (0.75 to 0.3) with claimed complexity 0.9 | rise | +0.308 (0.015) | -0.023 |
| Thrash: alternating terse/confident and heavy/unsure | rise | +0.157 (0.013) | -0.047 |
| Sham: same reports, different vocabulary | none | -0.038 (0.016), see the regime self-loop | -0.073 |
| Honest shift to heavier work, reported accurately | none | +0.007 (0.020) | -0.022 |

Recovery values are relative to each stream's own baseline. S is still settling
at steps 20 to 29 (the control drifts -0.047 by steps 60 to 69), which is why
recovery rows sit slightly negative.

E and I respond where their inputs say they should and not otherwise: overclaim
lowers E by 0.012 to 0.066 with dose and I by 0.008 to 0.041; confidence collapse
lowers E by 0.106 and I by 0.118; thrash lowers I by 0.119 and leaves E unchanged;
sham and honest shift move neither by more than 0.002.

Every check-in inside an intervention window (9 conditions × 8 seeds × 15
steps) returned `safe/proceed`. Risk rose by 0.07 to 0.10 under the large positive controls and
by 0.016 under the honest shift.

### Test-retest

Strict test-retest, same state and same reading: the control stream for seed 0,
replayed with identical inputs from an identical initial state, matches the
first run in every field at every step. Under the harness's injected clock this
is what a deterministic estimator should give; it confirms that no hidden state
outside the monitor (the driver's documented process-global caches are reset per
stream) leaks between runs.

### Robustness under within-style variation

This is not test-retest: the inputs differ within each style (new report text,
complexity and confidence jitter of 0.03, randomized intervals). Steady-state S
(mean of steps 30 to 69, 8 seeds per working style): moderate 0.178 ± 0.040, terse
0.200 ± 0.026, heavy 0.179 ± 0.018; ICC(1) 0.05; mean within-stream step SD 0.028.
The low ICC is the intended invariance: S is self-relative, and three steady,
accurately reported working styles should read alike. The useful comparison is
the replicate spread against the response size: the largest seed spread (0.040)
is a fifth of the overclaim response and an eighth of the confidence-collapse
response.

### The regime self-loop

`src/monitor_regime.py` `detect_regime` uses behavioral S once behavioral
confidence reaches 0.3, and returns `DIVERGENCE` when S rose by more than
`eps_S = 0.002` since the previous check-in while |V| exceeds 0.05.
`src/behavioral_sensor.py` `_regime_instability` then counts label changes over
the last ten entries of `regime_history`, at 35% of raw S. At rest behavioral V
sits near -0.095, so the V condition always holds, and S's step-to-step variation
(SD about 0.028, fourteen times `eps_S`) keeps flipping the label: 5.4 flips per
39 resting check-ins in the control. Each flip feeds back into S.

Counterfactual run (`--ablate-regime`, `_regime_instability` held at its
documented low default of 0.1). Both arms replay identical inputs on the same
seeds, so the live-minus-held difference is paired exactly (`--paired-ablation`):

| | Live code | Regime term held at 0.1 | Paired difference (se) |
|---|---|---|---|
| Resting within-stream S step SD | 0.0273 | 0.0016 | |
| Resting seed spread (moderate) | 0.040 | 0.0012 | |
| Overclaim 0.45 | +0.195 | +0.192 | +0.003 (0.012) |
| Confidence collapse | +0.308 | +0.294 | +0.014 (0.015) |
| Thrash | +0.157 | +0.128 | +0.029 (0.013) |
| Overclaim 0.10 / 0.20 / 0.30 | +0.040 / +0.087 / +0.115 | +0.024 / +0.066 / +0.114 | +0.016 (0.012) / +0.021 (0.020) / +0.001 (0.021) |
| Sham | -0.038 | -0.0003 | -0.037 (0.015) |
| Honest shift | +0.007 | -0.0004 | +0.007 (0.019) |

No deciding standard was declared for these differences, so they are reported
as exploratory estimates rather than sorted into effects and non-effects. The
largest share is thrash's (+0.029 of +0.157, about 18%), which fits a condition
that does switch regimes; most of that response still arrives through the drift
component. The sham's live shift (-0.038) is matched by its paired difference
(-0.037): with the term held, the vocabulary-only change moves S by -0.0003. In
these streams the term contributes about 94% of S's resting step SD and a small
part of the measured responses.

The topology was already recorded (ledger row 13; the v7 observer self-loop
note). This run adds the magnitude and the direct path inside behavioral S, and
is recorded as ledger row 46.

## Limits

- Synthetic streams from templated text; real reports vary in ways the templates
  do not.
- Tool-usage statistics are pinned to zero by the harness, so the tool-error and
  tool-velocity inputs the check-in path supplies are absent. No outcome history.
- One agent class (the default) and one cadence band.
- Not pre-registered and no deciding standard was declared in advance, so this
  earns no ledger status by itself. It characterizes the mechanism.
- Faithfulness here means S tracks the quantities it is built from. Whether those
  quantities matter for anything downstream is a separate question.

## What this does not authorize

No change to S before the registered 2026-12-01 read. S is an input to that
read's models, and changing it now would contaminate the registered series (the
same reason the contract parks other EISV rescoring). A repair, dropping the term
or computing regime changes from something other than S itself, belongs after
that read or behind a shadow reading. That shadow now runs on every check-in where the
server computes the behavioral sensor
(`eisv_telemetry.shadow_ablations.regime_self_loop_neutralized`), holding the
term at its observed fleet level, 0.12, rather than the 0.1 default used here.

## Reproduce

```bash
python3 scripts/analysis/eisv_s_faithfulness.py                      # deployed flags
python3 scripts/analysis/eisv_s_faithfulness.py --no-deployed-flags  # code defaults (identical)
python3 scripts/analysis/eisv_s_faithfulness.py --ablate-regime      # counterfactual
python3 scripts/analysis/eisv_s_faithfulness.py --paired-ablation    # live vs held, paired by seed
```

Deterministic: seeds are fixed and every clock read is injected.
