# Live read of the 2026-10-15 basin and I-rest repairs: result v0

**Status:** completed 2026-11-18 under [`rule-v0.md`](rule-v0.md) (`schema: eisv.repair_live_read.v1`, `range_only: true`).
**Script:** [`scripts/analysis/eisv_repair_live_read.py`](../../../scripts/analysis/eisv_repair_live_read.py) (read-only `REPEATABLE READ` snapshot against PostgreSQL `:5432/governance`).

**Telemetry only.** This read joins no outcome labels and makes no efficacy, harm-prevention, or enforcement claim. It records whether the live behavioral `verdict = 'safe'` → `action = 'approve'` mix moved as contract row 50's equilibrium-shift estimate predicted.

**Range-only scope.** Per-stratum counterfactual endpoints were not committed with `#2834`, so every stratum is checked against contract row 50's pooled 209-identity fleet-mix range `[44.1%, 84.5%]`. An `ABOVE` outcome here means outside the pooled fleet-mix estimate, not off a stratum-specific estimate.

## Windows and exclusions (read separately, never pooled)

`UNITARES_RESIDENTS` was set to `Lumen,Vigil,Sentinel,Watcher,Chronicler,Doctor,revenue-worker-1,kg-gardener` so residents map to their own strata rather than folding into generic classes.

- **Post-repair era (`RK1`):** `2026-10-16T02:59:50+00:00` .. `2026-10-22T14:27:20+00:00` (clamped before `RR1` `ambiguous_from`; single stamped instrument id `RK1:a2579740d3bea62a`, 8,273 rows, unstamped rows `0`). Unsettled transient rows excluded: `276`. Non-behavioral rows (reported, never compared): `physical` `2,848`, `unrecorded` `270`.
- **Pre-repair baseline era (`ID`):** `2026-10-05T22:06:59+00:00` .. `2026-10-15T13:45:00+00:00` (clamped before `A1` `ambiguous_from`; unstamped era, `0` stamped rows). Unsettled transient rows excluded: `229`. Non-behavioral rows (reported, never compared): `physical` `4,506`, `unrecorded` `678`.

## Stratum outcomes

| Stratum | Outcome | Endpoints | Shift |
|---|---|---|---|
| `behavioral_assessment/Sentinel` | `INCONCLUSIVE` | `range_only [44.1%, 84.5%]` | `+100.0%` |
| `behavioral_assessment/Vigil` | `INCONCLUSIVE` | `range_only [44.1%, 84.5%]` | `+100.0%` |
| `behavioral_assessment/Watcher` | `INCONCLUSIVE` | `range_only [44.1%, 84.5%]` | `+100.0%` |
| `behavioral_assessment/engaged_ephemeral` | `ABOVE, loop-consistent` | `range_only [44.1%, 84.5%]` | `+66.8%` |

## Per-era counts and statistics

`P` = post-repair (`RK1`), `B` = baseline (`ID`). The pooled row-level `approve` share over `safe` rows decides each stratum; the per-identity `p50` share (over identities with at least 10 `safe` rows) and within-identity `state_json.E` last-quarter minus first-quarter trend (`E trend p50`) are reported beside it.

| Stratum | Era | Pooled share | Safe rows | Identities | Counted ids | p50 rows/id | Max id share | Id p50 share | E trend p50 |
|---|---|---|---|---|---|---|---|---|---|
| `behavioral_assessment/Sentinel` | P | `100.0%` (`1782/1782`) | `1782` | `1` | `1` | `1782` | `100.0%` | `100.0%` | `-0.0074` |
| `behavioral_assessment/Sentinel` | B | `0.0%` (`0/1138`) | `1138` | `1` | `1` | `1138` | `100.0%` | `0.0%` | `+0.0057` |
| `behavioral_assessment/Vigil` | P | `100.0%` (`249/249`) | `249` | `1` | `1` | `249` | `100.0%` | `100.0%` | `-0.0008` |
| `behavioral_assessment/Vigil` | B | `0.0%` (`0/406`) | `406` | `1` | `1` | `406` | `100.0%` | `0.0%` | `+0.0076` |
| `behavioral_assessment/Watcher` | P | `100.0%` (`1277/1277`) | `1277` | `1` | `1` | `1277` | `100.0%` | `100.0%` | `+0.0003` |
| `behavioral_assessment/Watcher` | B | `0.0%` (`0/1652`) | `1652` | `1` | `1` | `1652` | `100.0%` | `0.0%` | `-0.0015` |
| `behavioral_assessment/engaged_ephemeral` | P | `93.0%` (`1461/1571`) | `1571` | `41` | `31` | `23` | `11.4%` | `100.0%` | `+0.0688` |
| `behavioral_assessment/engaged_ephemeral` | B | `26.2%` (`614/2341`) | `2341` | `154` | `60` | `6` | `6.9%` | `20.8%` | `+0.0677` |

## Interpretation

- **Resident strata (`Sentinel`, `Vigil`, `Watcher`):** `INCONCLUSIVE` by construction in both eras because each stratum is a single identity (`N = 1 < 5` counted identities and `100.0% > 50%` max single-identity share). As individual identities, all three moved from `0.0%` (`0/1138`, `0/406`, `0/1652`) in `ID` to `100.0%` (`1782/1782`, `249/249`, `1277/1277`) in `RK1`.
- **`behavioral_assessment/engaged_ephemeral`:** Meets every floor in both eras (`counted_identities` `31 >= 5` in `RK1` and `60 >= 5` in `ID`; `safe_rows` `1571 >= 100` and `2341 >= 100`; `max_identity_share` `11.4% <= 50%` and `6.9% <= 50%`). Baseline pooled share (`26.2%`) is below the deployed replay endpoints (`26.7%` / `28.9%`), so the `composition` qualifier does not trigger; post-repair pooled share (`93.0%`, `0.92998`, `+66.8%` shift) exceeds the pooled `[44.1%, 84.5%]` range; and `E trend p50` is `+0.0688` (`> 0`), yielding **`ABOVE, loop-consistent`** (range-only).
- This matches the `decision_e` feedback path noted before the read: removing the BOUNDARY `guide` pin shifts actions from `guide` (`0.7`) to `approve` (`1.0`), which raises `decision_e` and behavioral `E` across subsequent check-ins (a mechanism that `SN1` later neutralized on `2026-11-05`). No efficacy, outcome, or harm-prevention conclusion follows from this telemetry read.

## Reproduce

```bash
UNITARES_RESIDENTS="Lumen,Vigil,Sentinel,Watcher,Chronicler,Doctor,revenue-worker-1,kg-gardener" \
  python3 scripts/analysis/eisv_repair_live_read.py
UNITARES_RESIDENTS="Lumen,Vigil,Sentinel,Watcher,Chronicler,Doctor,revenue-worker-1,kg-gardener" \
  python3 scripts/analysis/eisv_repair_live_read.py --json
```
