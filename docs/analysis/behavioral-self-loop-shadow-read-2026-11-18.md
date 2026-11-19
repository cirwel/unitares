# Behavioral self-loop shadow reads (`RK1`, `RR1`, `SN1`) — 2026-11-18

Two measurement-only shadows persisted on every server-computed behavioral check-in (`eisv_telemetry.shadow_ablations.candidates.decision_self_loop_neutralized`, `#1777`, and `regime_self_loop_neutralized`, `#2759`, computed in [`src/behavioral_sensor.py`](../../src/behavioral_sensor.py)) had been written without an aggregation read (`docs/EVALUATION_INDEX.md`). This record reads them once against the single-operator PostgreSQL database using [`scripts/analysis/behavioral_self_loop_shadow_read.py`](../../scripts/analysis/behavioral_self_loop_shadow_read.py), reading **each EISV instrument era separately (disjoint UTC windows, never pooled)** and **without querying `audit.outcome_events`**.

**Scope limits (matches the shadows' `not_modeled` contract).** Both shadows compute one-step observation deltas against fixed fleet stand-ins (`DECISION_NEUTRAL_SCORE = 0.84` for `decision_history`, `REGIME_NEUTRAL_INSTABILITY = 0.12` for `_regime_instability`). Neither models recursive `E`/`I`/`S` history replay, behavioral EMA replay, `V` or regime-label counterfactuals, policy effects, or future outcomes. No figure below measures closed-loop gain, nothing here touches the closed outcome-grounding stop rule (`docs/proposals/registered/eisv-outcome-grounding-stop-rule-v0.md`), and per contract ledger row 46 **no change to `S` is proposed before the 2026-12-01 read**.

## Summary across separate eras

Rows are split strictly by `sensor_eisv_source` (`behavioral`, `physical`, `unrecorded`); the script reads all `synthetic IS NOT TRUE` rows in each era (no unsettled-verdict exclusion). `RR1` is a config-hash population boundary (`config_hash = 92a47c19ddf301d8`) under code era `RK1`, so its rows carry stamp prefix `RK1:92a47c19ddf301d8`. Shadows are written only on server-computed `behavioral` check-ins (`0` present on `physical` or `unrecorded` in any era).

| Era (disjoint UTC window) | Window (UTC) | Instrument stamp (`id`) | `behavioral` rows (identities) | `physical` / `unrecorded` | `decision` eligible (`delta_E` `p50` / `p90`) | `regime` eligible (`delta_S` `p50` / `p90`) |
|---|---|---|---|---|---|---|
| `RK1` (pre-`SN1`) | `2026-10-16T02:59:50` .. `2026-10-22T14:27:20` | `RK1:a2579740d3bea62a` | `5155` (`48`) | `2848` (`1`) / `270` (`120`) | `n = 5155` (`-0.0381` / `-0.0381`) | `n = 5155` (`+0.0336` / `+0.0336`) |
| `RR1` (pre-`SN1`, `RK1` code) | `2026-10-22T14:28:19` .. `2026-11-05T22:56:34` | `RK1:92a47c19ddf301d8` | `7936` (`104`) | `5183` (`1`) / `673` (`203`) | `n = 7936` (`-0.0381` / `-0.0381`) | `n = 7936` (`+0.0336` / `+0.0336`) |
| `SN1` (post-`SN1`, stamp-filtered) | `2026-11-05T22:57:49` .. `2026-11-18T08:08:59` | `SN1:fa9417b331219443` | `6882` (`113`) | `5223` (`1`) / `617` (`294`) | `0` (`self_loop_already_neutralized`: `6882`) | `0` (`self_loop_already_neutralized`: `6882`) |

In `SN1` (`UNITARES_NEUTRALIZE_SENSOR_SELF_LOOPS=1`, deployed `2026-11-05T22:56:34Z`), **both** `decision_self_loop_neutralized` (`#1777`) and `regime_self_loop_neutralized` (`#2759`) record `0 / 6882` eligible behavioral rows, all with `eligibility_reason: "self_loop_already_neutralized"`, confirming live neutralization of both terms on every stamped `SN1` behavioral check-in.

## `decision_self_loop_neutralized` (#1777)

- **Pre-`SN1` `RK1` (`n = 5155` eligible behavioral rows, `48` identities):** `candidate_minus_deployed.E` has `p10 = -0.0381`, `p50 = -0.0381`, `p90 = -0.0381` (`min = -0.0560`, `max = +0.0960`); `candidate_minus_deployed.I` is identically `0.0000` (`n = 5155`, `p10 = 0.0000`, `p50 = 0.0000`, `p90 = 0.0000`, `min = 0.0000`, `max = 0.0000`). Among the `37` identities with `n >= 10` rows, `31` sit at `delta_E p50 = -0.0381` (uniform `approve` history scored `1.0` vs neutral `0.84`), including all three behavioral residents (`Sentinel` `n = 1831`, `Watcher` `n = 1326`, `Vigil` `n = 298`, each `p50 = -0.0381`, `p90 = -0.0381`).
- **Pre-`SN1` `RR1` (`n = 7936` eligible behavioral rows, `104` identities):** `candidate_minus_deployed.E` has `p10 = -0.0381`, `p50 = -0.0381`, `p90 = -0.0381` (`min = -0.0544`, `max = +0.1200`); `candidate_minus_deployed.I` is identically `0.0000` (`n = 7936`, `p10 = 0.0000`, `p50 = 0.0000`, `p90 = 0.0000`, `min = 0.0000`, `max = 0.0000`). Among the `53` identities with `n >= 10` rows, `38` sit at `delta_E p50 = -0.0381` (including `Sentinel` `n = 3525`, `Watcher` `n = 1366`, `Vigil` `n = 530` at `p50 = -0.0381`, `p90 = -0.0381`).
- **What this establishes:** Across both pre-`SN1` eras, `p10 = p50 = p90 = -0.0381` shows that at least the `[p10, p90]` bulk of row-pooled behavioral check-ins reflected a constant offset relative to the fixed `0.84` neutral stand-in (`decision_history` saturated at `1.0` for uniform `approve`), while nonzero variation appeared only on the minority of rows recording non-`approve` (`guide`/`revise`) decisions (for example `n = 94, delta_E p50 = +0.0327` in `RK1` and `n = 61, delta_E p50 = +0.0127` in `RR1`). Row-pooled percentiles are row-weighted (`3,455 / 5,155` `RK1` and `5,421 / 7,936` `RR1` rows come from the three behavioral residents), so per-identity `n >= 10` counts are reported alongside them.

## `regime_self_loop_neutralized` (#2759)

Because `S` is linear and increasing in the regime-instability term (weight `0.35` in [`src/behavioral_sensor.py`](../../src/behavioral_sensor.py)), replacing `deployed_regime_instability` with `REGIME_NEUTRAL_INSTABILITY = 0.12` produces negative `delta_S` only when `deployed_regime_instability > 0.12`, and non-negative `delta_S` up to `+0.0420` (`0.35 * 0.12` at zero instability; `+0.0336 = 0.8 * 0.0420` on continuity-blended rows where `continuity_S_input` is weighted `0.20`, and `+0.0025` continuity-blended / `+0.0031` unblended at `0.1111`) when `deployed_regime_instability <= 0.12`.

- **Pre-`SN1` `RK1` (`n = 5155` eligible behavioral rows, `48` identities):** Row-pooled `candidate_minus_deployed.S` has `p10 = -0.0908`, `p50 = +0.0336`, `p90 = +0.0336` (`min = -0.2464`, `max = +0.0420`), with `deployed_regime_instability` `p50 = 0.0000`, `p90 = 0.4444`, `max = 1.0000`.
- **Pre-`SN1` `RR1` (`n = 7936` eligible behavioral rows, `104` identities):** Row-pooled `candidate_minus_deployed.S` has `p10 = -0.1220`, `p50 = +0.0336`, `p90 = +0.0336` (`min = -0.3080`, `max = +0.0420`), with `deployed_regime_instability` `p50 = 0.0000`, `p90 = 0.5556`, `max = 1.0000`.
- **Resident vs ephemeral split (per-identity medians over `n >= 10` identities):** Row-pooled `p50` sits at `+0.0336` (the continuity-blended zero-instability value; unblended rows reach `max = +0.0420`) because heavy resident identities (`Sentinel` and `Watcher` at `deployed_regime_instability p50 = 0.0000`; `Vigil` at `p50 = 0.1111` in `RK1` [`n = 298`, `delta_S p50 = +0.0025`] and `0.0000` in `RR1` [`n = 530`, `delta_S p50 = +0.0336`]) account for roughly two-thirds of behavioral rows (`3455 / 5155` in `RK1` and `5421 / 7936` in `RR1`). Among `n >= 10` identities:
  - In `RK1` (`37` identities): `35 / 37` have `deployed_regime_instability p50 > 0`, of which `8` sit in `(0, 0.12]` (including `Vigil` at `0.1111`, positive `delta_S p50 = +0.0025`), and **`27 / 37` have `deployed_regime_instability p50 > 0.12` and negative `delta_S p50`** (minimum per-identity median `-0.1531` on `n = 15` rows, `deployed_regime_instability p50 = 0.6667`, for identity `22156`).
  - In `RR1` (`53` identities): `49 / 53` have `deployed_regime_instability p50 > 0`, of which `4` sit in `(0, 0.12]` (`delta_S p50 = +0.0025`), and **`45 / 53` have `deployed_regime_instability p50 > 0.12` and negative `delta_S p50`** (minimum per-identity median `-0.2422` on `n = 14` rows, `deployed_regime_instability p50 = 0.8452`, for identity `22674`).

## Reproduce

```bash
python3 scripts/analysis/behavioral_self_loop_shadow_read.py --era RK1
python3 scripts/analysis/behavioral_self_loop_shadow_read.py --era RR1
python3 scripts/analysis/behavioral_self_loop_shadow_read.py --era SN1
```
