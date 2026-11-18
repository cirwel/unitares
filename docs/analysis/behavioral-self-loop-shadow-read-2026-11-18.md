# Behavioral self-loop shadow reads (`RK1`, `RR1`, `SN1`) — 2026-11-18

Two measurement-only shadows persisted on every server-computed behavioral check-in (`eisv_telemetry.shadow_ablations.candidates.decision_self_loop_neutralized`, `#1777`, and `regime_self_loop_neutralized`, `#2759`) had been written without an aggregation read (`docs/EVALUATION_INDEX.md`). This record reads them once against the single-operator PostgreSQL database using [`scripts/analysis/behavioral_self_loop_shadow_read.py`](../../scripts/analysis/behavioral_self_loop_shadow_read.py), reading **each EISV instrument era separately (never pooled)** and **without querying `audit.outcome_events`**.

**Scope limits (matches the shadows' `not_modeled` contract).** Both shadows compute one-step observation deltas against observed fleet stand-ins (`DECISION_NEUTRAL_SCORE = 0.84` for `decision_history`, `REGIME_NEUTRAL_INSTABILITY = 0.12` for `_regime_instability`). Neither models recursive `E`/`I`/`S` history replay, behavioral EMA replay, `V` or regime-label counterfactuals, policy effects, or future outcomes. No figure below measures closed-loop gain, and nothing here touches the closed outcome-grounding stop rule (`docs/proposals/registered/eisv-outcome-grounding-stop-rule-v0.md`).

## Summary across separate eras

Rows are split by `sensor_eisv_source` (`behavioral`, `physical`, `unrecorded`); shadows are written only on server-computed `behavioral` check-ins (`0` present on `physical` or `unrecorded` in any era).

| Era | Window (UTC) | Instrument id | `behavioral` rows (identities) | `physical` / `unrecorded` | `decision` eligible (`delta_E` `p50` / `p90`) | `regime` eligible (`delta_S` `p50` / `p90`) |
|---|---|---|---|---|---|---|
| `RK1` (pre-`SN1`) | `2026-10-16T02:59:50` .. `2026-10-22T14:27:20` | `RK1:a2579740d3bea62a` | `5155` (`48`) | `2848` (`1`) / `270` (`120`) | `n = 5155` (`-0.0381` / `-0.0381`) | `n = 5155` (`+0.0336` / `+0.0336`) |
| `RR1` (pre-`SN1`) | `2026-10-22T14:28:19` .. `2026-11-05T22:56:34` | `RK1:92a47c19ddf301d8` | `7936` (`104`) | `5183` (`1`) / `673` (`203`) | `n = 7936` (`-0.0381` / `-0.0381`) | `n = 7936` (`+0.0336` / `+0.0336`) |
| `SN1` (post-`SN1`, stamp-filtered) | `2026-11-05T22:57:49` .. `2026-11-18T08:08:59` | `SN1:fa9417b331219443` | `6882` (`113`) | `5223` (`1`) / `617` (`294`) | `0` (`self_loop_already_neutralized`: `6882`) | `0` (`self_loop_already_neutralized`: `6882`) |

In `SN1` (`UNITARES_NEUTRALIZE_SENSOR_SELF_LOOPS=1`, deployed `2026-11-05T22:56:34Z`), all `6882 / 6882` shadow-bearing behavioral rows carry `eligible: false` with `eligibility_reason: "self_loop_already_neutralized"`, confirming live neutralization of both terms.

## `decision_self_loop_neutralized` (#1777)

- **Pre-`SN1` `RK1` (`n = 5155` eligible, `48` identities):** `candidate_minus_deployed.E` has `p10 = -0.0381`, `p50 = -0.0381`, `p90 = -0.0381` (`min = -0.0560`, `max = +0.0960`); `candidate_minus_deployed.I` is `0.0000` (`p50 = 0.0000`, `p90 = 0.0000`, `n = 5155`). Among the `37` identities with `n >= 10` rows, `31` sit at `delta_E p50 = -0.0381` (reflecting uniform `approve` history scored `1.0` vs replacement `0.84`), including all three behavioral residents (`Sentinel` `n = 1831`, `Watcher` `n = 1326`, `Vigil` `n = 298`, each `p50 = -0.0381`, `p90 = -0.0381`).
- **Pre-`SN1` `RR1` (`n = 7936` eligible, `104` identities):** `candidate_minus_deployed.E` has `p10 = -0.0381`, `p50 = -0.0381`, `p90 = -0.0381` (`min = -0.0544`, `max = +0.1200`); `candidate_minus_deployed.I` is `0.0000` (`p50 = 0.0000`, `p90 = 0.0000`, `n = 7936`). Among the `53` identities with `n >= 10` rows, `38` sit at `delta_E p50 = -0.0381` (again including `Sentinel` `n = 3525`, `Watcher` `n = 1366`, `Vigil` `n = 530` at `p50 = -0.0381`, `p90 = -0.0381`).
- **What this establishes:** Across both pre-`SN1` eras, `decision_history` operated as a constant offset (`-0.0381` relative to the `0.84` neutral reference) on at least 90% of rows; nonzero variation appeared only on the minority of sessions that recorded non-`approve` (`guide`/`revise`) decisions (for example `n = 94, delta_E p50 = +0.0327` in `RK1` and `n = 61, delta_E p50 = +0.0127` in `RR1`).

## `regime_self_loop_neutralized` (#2759)

- **Pre-`SN1` `RK1` (`n = 5155` eligible, `48` identities):** `candidate_minus_deployed.S` has `p10 = -0.0908`, `p50 = +0.0336`, `p90 = +0.0336` (`min = -0.2464`, `max = +0.0420`), with `deployed_regime_instability` `p50 = 0.0000`, `p90 = 0.4444`, `max = 1.0000`.
- **Pre-`SN1` `RR1` (`n = 7936` eligible, `104` identities):** `candidate_minus_deployed.S` has `p10 = -0.1220`, `p50 = +0.0336`, `p90 = +0.0336` (`min = -0.3080`, `max = +0.0420`), with `deployed_regime_instability` `p50 = 0.0000`, `p90 = 0.5556`, `max = 1.0000`.
- **Resident vs ephemeral split (`n >= 10`):** Row-level `p50` is pulled to `+0.0336` because long-running residents account for roughly two-thirds of behavioral rows (`Sentinel` + `Watcher` + `Vigil`: `3455 / 5155` in `RK1` and `5421 / 7936` in `RR1`) and sit largely at zero regime transitions (`deployed_regime_instability p50 = 0.0000` in `RR1`, `0.0000` for `Sentinel`/`Watcher` and `0.1111` for `Vigil` in `RK1`). By contrast, `35` of `37` `n >= 10` identities in `RK1` and `49` of `53` in `RR1` have `deployed_regime_instability p50 > 0` (`p50` up to `0.6667` in `RK1` and `0.8452` in `RR1`), yielding negative `candidate_minus_deployed.S` medians (`p50` down to `-0.1531` in `RK1` and `-0.2422` in `RR1`).

## Reproduce

```bash
python3 scripts/analysis/behavioral_self_loop_shadow_read.py --era RK1
python3 scripts/analysis/behavioral_self_loop_shadow_read.py --era RR1
python3 scripts/analysis/behavioral_self_loop_shadow_read.py --era SN1
```
