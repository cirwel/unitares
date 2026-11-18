# Live read of the 2026-10-15 basin and I-rest repairs: result v0

**Status:** completed 2026-11-18 under [`rule-v0.md`](rule-v0.md) (`schema: eisv.repair_live_read.v1`, `range_only: true`).
**Script:** [`scripts/analysis/eisv_repair_live_read.py`](../../../scripts/analysis/eisv_repair_live_read.py) (read-only `REPEATABLE READ` snapshot against PostgreSQL `:5432/governance`).

**Telemetry only (no outcome labels).** This read joins no outcome labels and makes no efficacy, harm-prevention, causal, or enforcement claim. It records one-step/per-row telemetry: whether the live `sensor_eisv_source = 'behavioral'` `verdict = 'safe'` → `action = 'approve'` mix moved relative to contract row 50's equilibrium-shift estimate.

**Range-only reference and confounds.** Per-stratum counterfactual endpoints were not committed with `#2834`, so each stratum's `RK1` level is checked against contract row 50's fixed pooled 209-identity fleet-mix reference `[44.1%, 84.5%]`. Here `ABOVE` means `RK1`'s level (`93.0%`) exceeds that fixed pooled fleet-mix reference (not a stratum-specific counterfactual). `ID` (unstamped) and `RK1` (stamped) are analyzed separately (`never pooled`), and the `ID` → `RK1` comparison spans an instrument transition (`A1` + `I1` + `RK1`) and unequal window lengths (`~9.6` vs `~6.5` days).

## Windows and exclusions (read separately, never pooled)

`UNITARES_RESIDENTS` was set to `Lumen,Vigil,Sentinel,Watcher,Chronicler,Doctor,revenue-worker-1,kg-gardener` so residents map to their own `sensor_eisv_source = 'behavioral'` strata rather than folding into generic classes (confirming none is the `physical` identity `Lumen`). Unsettled transient rows are subtracted from total `behavioral` rows in each era before computing stratum shares.

- **Post-repair era (`RK1`):** `2026-10-16T02:59:50+00:00` .. `2026-10-22T14:27:20+00:00` (clamped before `RR1` `ambiguous_from`; single stamped instrument id `RK1:a2579740d3bea62a`, `8,273` non-synthetic rows total, `0` unstamped). Composition: `5,155` `behavioral` rows (`276` unsettled transient excluded → `4,879` settled `behavioral` rows across the four strata), `2,848` `physical`, `270` `unrecorded` (`physical` and `unrecorded` reported separately, never compared).
- **Pre-repair baseline era (`ID`):** `2026-10-05T22:06:59+00:00` .. `2026-10-15T13:45:00+00:00` (clamped before `A1` `ambiguous_from`; unstamped era, `0` stamped rows; `10,950` non-synthetic rows total). Composition: `5,766` `behavioral` rows (`229` unsettled transient excluded → `5,537` settled `behavioral` rows across the four strata), `4,506` `physical`, `678` `unrecorded` (`physical` and `unrecorded` reported separately, never compared).

## Stratum outcomes

| Stratum (`sensor_eisv_source = behavioral`) | Outcome | Fleet-mix reference | RK1 level vs ID baseline (`pp`) |
|---|---|---|---|
| `behavioral_assessment/Sentinel` | `INCONCLUSIVE` | `range_only [44.1%, 84.5%]` | `+100.0 pp` |
| `behavioral_assessment/Vigil` | `INCONCLUSIVE` | `range_only [44.1%, 84.5%]` | `+100.0 pp` |
| `behavioral_assessment/Watcher` | `INCONCLUSIVE` | `range_only [44.1%, 84.5%]` | `+100.0 pp` |
| `behavioral_assessment/engaged_ephemeral` | `ABOVE, loop-consistent` (`range-only`) | `range_only [44.1%, 84.5%]` | `+66.8 pp` (`RK1` `93.0%` vs `ID` `26.2%`) |

## Per-era counts and statistics

`P` = post-repair (`RK1`), `B` = baseline (`ID`). The pooled row-level `approve` share over settled `safe` rows decides each stratum; the per-identity `p50` share and within-identity `state_json.E` last-quarter minus first-quarter trend (`E trend p50`) are computed over counted identities (`>= 10` `safe` rows; unit: delta `E` per identity).

| Stratum | Era | Pooled share | Safe rows | Identities | Counted ids (`N`) | p50 rows/id | Max id share | Id p50 share | E trend p50 (over counted ids) |
|---|---|---|---|---|---|---|---|---|---|
| `behavioral_assessment/Sentinel` | P | `100.0%` (`1782/1782`) | `1782` | `1` | `1` | `1782` | `100.0%` | `100.0%` | `-0.0074` (`N=1`) |
| `behavioral_assessment/Sentinel` | B | `0.0%` (`0/1138`) | `1138` | `1` | `1` | `1138` | `100.0%` | `0.0%` | `+0.0057` (`N=1`) |
| `behavioral_assessment/Vigil` | P | `100.0%` (`249/249`) | `249` | `1` | `1` | `249` | `100.0%` | `100.0%` | `-0.0008` (`N=1`) |
| `behavioral_assessment/Vigil` | B | `0.0%` (`0/406`) | `406` | `1` | `1` | `406` | `100.0%` | `0.0%` | `+0.0076` (`N=1`) |
| `behavioral_assessment/Watcher` | P | `100.0%` (`1277/1277`) | `1277` | `1` | `1` | `1277` | `100.0%` | `100.0%` | `+0.0003` (`N=1`) |
| `behavioral_assessment/Watcher` | B | `0.0%` (`0/1652`) | `1652` | `1` | `1` | `1652` | `100.0%` | `0.0%` | `-0.0015` (`N=1`) |
| `behavioral_assessment/engaged_ephemeral` | P | `93.0%` (`1461/1571`) | `1571` | `41` | `31` | `23` | `11.4%` | `100.0%` | `+0.0688` (`N=31`) |
| `behavioral_assessment/engaged_ephemeral` | B | `26.2%` (`614/2341`) | `2341` | `154` | `60` | `6` | `6.9%` | `20.8%` | `+0.0677` (`N=60`) |

## Interpretation

- **Resident strata (`Sentinel`, `Vigil`, `Watcher`):** `INCONCLUSIVE` by construction in both eras because each stratum is a single identity (`N = 1 < 5` counted identities and `100.0% > 50%` max single-identity share). As individual behavioral identities, all three moved from `0.0%` (`0/1138`, `0/406`, `0/1652`) in `ID` to `100.0%` (`1782/1782`, `249/249`, `1277/1277`) in `RK1`.
- **`behavioral_assessment/engaged_ephemeral`:** Meets every floor in both eras (`counted_identities` `31 >= 5` in `RK1` and `60 >= 5` in `ID`; `safe_rows` `1571 >= 100` and `2341 >= 100`; `max_identity_share` `11.4% <= 50%` and `6.9% <= 50%`). Baseline pooled share (`26.2%`) is below the deployed replay endpoints (`26.7%` / `28.9%`), so the `composition` qualifier does not trigger; post-repair pooled level (`93.0%`, `0.92998`, `+66.8 pp` over `ID`) exceeds the fixed pooled `[44.1%, 84.5%]` fleet-mix reference; and within-identity `E trend p50` is `+0.0688` (`N=31`, `> 0`), yielding **`ABOVE, loop-consistent`** (range-only, descriptive).
- **Mechanism note & limits:** Directionally compatible with the `decision_e` feedback path noted in [`rule-v0.md`](rule-v0.md) (removing the `BOUNDARY` `guide` pin shifts actions from `guide` `0.7` to `approve` `1.0`, which can feed back into `decision_e` and behavioral `E`), not causal isolation of that mechanism against the `ID` → `RK1` instrument transition. `SN1` later neutralized `decision_history` on `2026-11-05` after the `RK1` window ended (`2026-10-22`), so this read says nothing about `SN1`'s effect. Recursive replay Counterfactuals remain unmodeled (`range-only`), and no efficacy, outcome, or harm-prevention conclusion follows from this telemetry read.

## Reproduce

```bash
UNITARES_RESIDENTS="Lumen,Vigil,Sentinel,Watcher,Chronicler,Doctor,revenue-worker-1,kg-gardener" \
  python3 scripts/analysis/eisv_repair_live_read.py
UNITARES_RESIDENTS="Lumen,Vigil,Sentinel,Watcher,Chronicler,Doctor,revenue-worker-1,kg-gardener" \
  python3 scripts/analysis/eisv_repair_live_read.py --json
```
