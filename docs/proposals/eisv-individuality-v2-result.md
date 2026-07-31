# EISV individuality v2 — final read result

**Verdict: FAIL.** The kill criterion in
[`eisv-individuality-v2-preregistration.md`](eisv-individuality-v2-preregistration.md)
executed on schedule: the individuality axiom is **retired for raw behavioral
EISV as currently measured**. No v3 against this measurement process; a further
attempt requires *changing the measurement* and pre-registering before any of its
data exists.

- **Registered read date:** 2026-07-30. **Executed:** 2026-07-30T15:00:05Z.
- **Script:** `scripts/analysis/eisv_individuality_v2.py`, sha256
  `e512c01cf18110f59fbdf749dd15ca4d04bce671f2c0d541314dffa37e2240a5` — byte-identical
  to the snapshot frozen at pre-registration.
- **Eligible agents:** 7 (floor 4), so the verdict is FAIL rather than NOT EVALUABLE.
- **Leg A:** 0 of 7. **Leg B:** fail (E rho=0.86 p=0.012; I p=0.249; S p=0.118).
  **Leg C:** 0 of 7 (estimator finding; does not gate the axiom).

An interim read on 2026-07-16 also returned FAIL. Both are reported here; neither
was reworked.

---

## Machine output (verbatim)

The block below is the frozen script's own output, unedited. The 46 sub-threshold
agents in the original per-agent table are omitted for length — every one carries
`eligible=no` and contributes to no leg.

# EISV individuality v2 — pre-registered read

Registration: `2026-07-02T18:00:00+00:00` — only rows recorded after this instant are counted. Spec: `docs/proposals/eisv-individuality-v2-preregistration.md` (thresholds frozen; do not reinterpret).


## Per-agent (eligible only; 46 sub-threshold agents omitted, all `eligible=no`)

| Agent | states | moved | eligible | leg A (VR24 dims E/I/S, p) | A | leg C win-rate (p) | C |
|---|---:|---:|---|---|---|---|---|
| Lumen | 5360 | 5359 | yes | 0.26(0.001) / 0.25(0.001) / 0.80(0.002) | fail | 0.41 (1.000, n=15256) | fail |
| Sentinel | 4918 | 2122 | yes | 0.07(0.001) / 0.12(0.001) / 0.29(0.001) | fail | 0.47 (0.999, n=3481) | fail |
| Vigil | 791 | 286 | yes | 0.08(0.001) / 0.12(0.001) / 0.39(0.001) | fail | 0.39 (1.000, n=533) | fail |
| lumen-broker-ex-shadow | 555 | 554 | yes | 0.34(0.001) / 0.09(0.001) / 0.78(0.237) | fail | 0.38 (1.000, n=1546) | fail |
| Watcher | 542 | 478 | yes | 0.08(0.001) / 0.11(0.001) / 0.24(0.001) | fail | 0.31 (1.000, n=885) | fail |
| lumen-broker-ex-shadow_a00e9d21 | 495 | 494 | yes | 0.25(0.001) / 0.10(0.001) / 0.89(0.412) | fail | 0.39 (1.000, n=1353) | fail |
| lumen-broker-ex-shadow_f4eba889 | 283 | 282 | yes | 0.29(0.007) / 0.13(0.001) / 1.30(0.949) | fail | 0.40 (1.000, n=735) | fail |
| Agent | dim | VR8 | VR16 | VR48 | VR24 |
|---|---|---:|---:|---:|---:|

### Descriptive VR curve (h ∈ (8, 16, 48), no inference — trend context for the primary h)
| Agent | dim | VR8 | VR16 | VR48 | VR24 |
|---|---|---:|---:|---:|---:|
| Lumen | E | 0.33 | 0.28 | 0.19 | 0.26 |
| Lumen | I | 0.28 | 0.25 | 0.26 | 0.25 |
| Lumen | S | 1.00 | 0.89 | 0.64 | 0.80 |
| Sentinel | E | 0.27 | 0.15 | 0.05 | 0.07 |
| Sentinel | I | 0.30 | 0.19 | 0.09 | 0.12 |
| Sentinel | S | 0.67 | 0.55 | 0.18 | 0.29 |
| Vigil | E | 0.24 | 0.12 | 0.05 | 0.08 |
| Vigil | I | 0.22 | 0.14 | 0.08 | 0.12 |
| Vigil | S | 0.80 | 0.52 | 0.15 | 0.39 |
| lumen-broker-ex-shadow | E | 0.37 | 0.37 | 0.25 | 0.34 |
| lumen-broker-ex-shadow | I | 0.16 | 0.11 | 0.09 | 0.09 |
| lumen-broker-ex-shadow | S | 0.84 | 0.77 | 0.75 | 0.78 |
| Watcher | E | 0.18 | 0.10 | 0.04 | 0.08 |
| Watcher | I | 0.31 | 0.15 | 0.05 | 0.11 |
| Watcher | S | 0.81 | 0.35 | 0.15 | 0.24 |
| lumen-broker-ex-shadow_a00e9d21 | E | 0.36 | 0.27 | 0.21 | 0.25 |
| lumen-broker-ex-shadow_a00e9d21 | I | 0.17 | 0.11 | 0.11 | 0.10 |
| lumen-broker-ex-shadow_a00e9d21 | S | 1.59 | 1.31 | 0.67 | 0.89 |
| lumen-broker-ex-shadow_f4eba889 | E | 0.44 | 0.33 | 0.17 | 0.29 |
| lumen-broker-ex-shadow_f4eba889 | I | 0.25 | 0.16 | 0.08 | 0.13 |
| lumen-broker-ex-shadow_f4eba889 | S | 1.26 | 1.47 | 0.80 | 1.30 |

Eligible agents: **7** (verdict floor 4); leg A winners: **0 / 7**

Leg B (split-half home stability, 7 agents): E: rho=0.86 (p=0.012) / I: rho=0.32 (p=0.249) / S: rho=0.54 (p=0.118) → **fail**

## Verdict: **FAIL**

- AXIOM EARNED = leg A majority AND leg B pass, at >= 4 eligible agents. Earns the individuality axiom's estimator half ONLY — outcome validity remains label-blocked; no public 'self-model' framing, no Stage B, nothing new wired to the live verdict path, regardless of outcome.
- Leg C is an estimator finding (is the runtime EMA reference fit to size residuals at informative moments) — it neither rescues nor kills the axiom.
- FAIL at the final read (2026-07-30) triggers the kill criterion: the axiom is retired for raw behavioral EISV as currently measured; no v3 without changing the measurement process.


---

## Interpretation

Recorded in the tested-claims ledger, not here, so there is one authoritative
reading rather than two:
[`../ontology/eisv-proprioception-contract.md`](../ontology/eisv-proprioception-contract.md)
(added in #1400, #1402, #1408).

In short: the kill criterion is honoured, but the FAIL is **not** evidence against
the axiom — both gating legs turn out to have had no usable power (leg A is a
whiteness detector that fails hardest on series that best satisfy the axiom; leg B
ran at effective n=4 because three of the seven "agents" replicate one Raspberry Pi;
and a ~10-day fleet outage sits inside a window whose legs count steps, not
wall-clock). The honest status is **untested as deployed**. Honouring a stopping
rule and being refuted are different acts, and the public record should not
conflate them.

This file exists to publish the instrument's **verbatim output** next to the
pre-registration that specified it, so the result can be checked rather than taken
on report.
