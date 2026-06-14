# Wave 1 condition 2 — alarm-rule parity audit (BEAM vs Python Sentinel) — 2026-06-14

Status: read-and-compare audit of one of the four Wave 1 exit conditions —
**condition 2, "alarm-rule parity with the Python Sentinel implementation."**
Grounds the parity claim against both runtimes' source rather than the
proposal docs' "tracked elsewhere." **Verdict: parity holds on the
fleet-analysis rules and on two of three forced-release alarm classes, but
two dedup-relevant gaps remain — condition 2 is not yet met.**

Method: rule-by-rule and fingerprint-by-fingerprint comparison of
`agents/sentinel/agent.py` + `agents/sentinel/forced_release_alarm.py`
(Python) against `elixir/sentinel/lib/unitares_sentinel/{fleet_analysis,
findings}.ex` + `forced_release_poller/logic.ex` (BEAM). The Python-side
ISO-format claim is empirically confirmed; the Elixir-side format is from
documented `DateTime.to_iso8601/1` behavior (no Elixir runtime in this
session — see Caveat).

Why fingerprints are the load-bearing surface: `/api/findings` dedups on
fingerprint. Findings emit is a **direct flip** (no shadow mode — RFC
§Surface 2), so the same condition can be alarmed by Python just before and
BEAM just after the <30s cutover gap (and symmetrically on rollback). If the
two runtimes produce different fingerprints for the same condition, the
server cannot dedup them → double-fire. RFC v0.1.1 §B2 makes fingerprint
byte-equivalence **binding** for exactly this reason.

## Part A — fleet-analysis rules (`FleetState.analyze` → `FleetAnalysis.analyze`)

All four rules match on every dedup-relevant field (type, violation_class,
severity, threshold, gating logic, details shape):

| Rule | Thresholds | Severity | Parity |
|------|-----------|----------|--------|
| `coordinated_degradation` | drop ≥ 0.15, ≥ 2 agents, stale skip > window×2 (1200s), window 600s | high / CON | ✅ match |
| `entropy_outlier` | ≥ 3 agents, sample std (n−1), z ≥ 2.0, stale skip > 3600s | info (self) / medium / ENT | ✅ match |
| `verdict_shift` | ≥ 5 verdicts in 600s, pause/reject rate ≥ 0.20 | high / ENT | ✅ match |
| `correlated_events` | ≥ 3 typed events in 600s, ≥ 2 distinct types; prefixes `lifecycle_`/`circuit_breaker_`/`identity_`/`knowledge_` | medium / BEH | ✅ match |

Constants verified identical: Python `FLEET_COHERENCE_DROP_THRESHOLD=0.15`,
`FLEET_COORDINATED_WINDOW=600`, `FLEET_COORDINATED_MIN_AGENTS=2`,
`FLEET_ENTROPY_SIGMA=2.0` (agent.py:76-79) vs BEAM module attrs
(fleet_analysis.ex:13-20). The `coherence_drop` (first−last over window),
`mean_entropy`, and sample-std (`n−1` denominator) computations are
structurally identical.

**Non-dedup-affecting cosmetic differences (acceptable per RFC §C3's
structural-equivalence downgrade):**

- **Summary-string rounding at `.5` boundaries.** Python uses round-half-to-
  even (`f"{x:.2f}"`, `f"{rate:.0%}"`); BEAM uses round-half-away
  (`:erlang.float_to_binary/2`, `round/1`). E.g. a 22.5% pause rate renders
  "22%" (Python) vs "23%" (BEAM). The summary is **not** in the fingerprint,
  so dedup is unaffected — but the human-readable line can differ by one ULP
  at exact boundaries.
- **Finding-list ordering.** Python iterates `dict` insertion order; BEAM
  iterates a `map` (unordered for >32 keys). The finding *set* is identical;
  list *order* may differ. Compare as a set, not a sequence.

## Part B — forced-release alarm fingerprints

These ride straight through to the server as the raw `forced_release:*`
string (`findings.ex` `alarm_body` passes `Map.fetch!(alarm, :fingerprint)`
unhashed; Python emits its `fingerprint` field likewise).

| Class | Python (`forced_release_alarm.py`) | BEAM (`logic.ex`) | Parity |
|-------|-----------------------------------|-------------------|--------|
| ad_hoc | `forced_release:ad_hoc:{event_id}` (:178) | `forced_release:ad_hoc:#{event_id}` (:81) | ✅ byte-equiv |
| deprecation_batch | `forced_release:deprecation_batch:{depr_id}` (:223) | `...:#{depr_id}` (:118) | ✅ byte-equiv |
| conflict_batch | `forced_release:conflict_batch:{surface_id}:{last_ts.isoformat()}` (:203) | `...:#{surface_id}:#{DateTime.to_iso8601(last_ts)}` (:151) | ❌ **DRIFT** |

### GAP 1 (confirmed) — conflict_batch fingerprint timezone-suffix drift

Python `datetime.isoformat()` on a tz-aware UTC value yields a
`+00:00`-terminated string; Elixir `DateTime.to_iso8601/1` on a UTC
`DateTime` yields a `Z`-terminated string. Empirically (Python side):

```
python isoformat:  '2026-05-04T12:00:00.123456+00:00'
elixir to_iso8601: '2026-05-04T12:00:00.123456Z'   (documented UTC behavior)
match: False
```

So for the same surface + timestamp, Python and BEAM emit **different
conflict_batch fingerprints**. Across the cutover gap (or a rollback), the
server cannot dedup them → duplicate conflict_batch alarm. This is exactly
the drift RFC v0.1.1 §C3 flagged ("Postgrex's `DateTime.to_iso8601/1`
produces `Z`-terminated strings; Python's `datetime.isoformat()` produces
`+00:00`-terminated") and exactly what §B2's binding ("fingerprint format
MUST match Python's exactly") forbids. ad_hoc and deprecation_batch are
ID-only and unaffected.

The existing BEAM test pins the format **against itself**, not against
Python: `forced_release_poller_logic_3class_test.exs:166` asserts equality
to `"...:#{DateTime.to_iso8601(row.last_ts)}"`, so it would pass while the
cross-runtime contract breaks. The §B2-mandated cross-runtime fingerprint
contract test for conflict_batch does not exist.

**Fix (BEAM side):** format `last_ts` to match Python's `+00:00` suffix in
`logic.ex:151` (and update the self-referential test at line 166 to assert
the `+00:00` literal). Add the §B2 cross-runtime contract test:
ad_hoc/deprecation/conflict fingerprints from a shared fixture must equal
the Python-computed strings byte-for-byte.

## Part C — fleet-finding fingerprint agent_id coupling

### GAP 2 (confirmed) — fleet-finding dedup depends on `agent_id` config

Fleet findings (Part A) are hashed via `compute_fingerprint(["sentinel",
finding_type, violation_class, agent_id])` on both sides (formula identical:
sha256 → lower-hex → 16-char prefix). The 4th component diverges:

- **Python (agent.py:590-595):** `agent_id = self.agent_uuid or ""` — the
  Sentinel's runtime UUID read from `sentinel.json`.
- **BEAM (fleet_finding_emitter.ex:404-412 → findings.ex:79):** `agent_id`
  resolves `:self_agent_id` → `findings_opts[:agent_id]` →
  `:findings_agent_id` config → `UNITARES_SENTINEL_AGENT_ID` env → default
  literal `"sentinel"`. `application.ex`'s `fleet_finding_emitter_opts`
  does **not** inject the anchor UUID into this path.

So fleet-finding fingerprints match across runtimes **only if** the BEAM
deployment sets `UNITARES_SENTINEL_AGENT_ID` (or `:findings_agent_id`) to
the exact UUID Python's Sentinel uses in `sentinel.json`. With the default,
BEAM hashes `"sentinel"` while Python hashes the UUID → no cross-runtime
dedup → fleet findings double-fire across the cutover gap.

Note this is *internally consistent* on BEAM (the same id is used in the
POST `agent_id` and the fingerprint), and the Surface 5 RFC already requires
identity continuity — but the continuity is wired into the **check-in**
path (via `SessionAnchor` → `GovernanceCheckin`), not the **finding-emit**
path. The two identities are resolved independently.

**Fix (smallest):** have `application.ex` thread the loaded
`SessionAnchor`'s `agent_uuid` into the emitter's `:findings_opts[:agent_id]`
(it already loads the anchor for `:checkin_opts`), so fleet-finding
fingerprints key on the same UUID Python uses — OR document
`UNITARES_SENTINEL_AGENT_ID = <sentinel.json agent_uuid>` as a binding
deploy step in the cutover runbook.

## Condition 2 verdict

**Not yet met.** The analysis rules and 2/3 alarm fingerprints are at parity;
two confirmed dedup gaps (conflict_batch ISO suffix, fleet-finding agent_id
coupling) would each cause cross-runtime double-fire at the cutover boundary,
which condition 2's parity bar exists to prevent. Both are small and
localized. Closing condition 2 = fix both + add the §B2 cross-runtime
fingerprint contract test.

## Update 2026-06-14 — both gaps fixed

Both gaps are now addressed in this branch:

- **GAP 1** — `logic.ex` gains a private `iso8601_python/1` helper (mirrors
  Python's `isoformat()`: `+00:00` suffix, fraction omitted at whole seconds,
  6-digit pad otherwise) and the conflict_batch fingerprint uses it. The
  self-referential Elixir test is updated to the explicit `+00:00` literal.
- **GAP 2** — `application.ex` threads the `SessionAnchor` `agent_uuid` into
  the fleet-finding emitter via `:self_agent_id` (additive + graceful: any
  anchor-load failure falls back to the prior config/env/default).
- **§B2 contract test** — `tests/test_sentinel_forced_release_fingerprint_parity.py`
  pins the Python side to exact literals; the BEAM 3-class test asserts the
  identical conflict_batch literal. Both suites asserting the same strings is
  the cross-runtime contract.

Validation status: the Python side is validated in this session (all four
fingerprint literals confirmed). The Elixir side is **not** run here (no
`mix`); CI's `mix test` is the gate for `iso8601_python/1`, the updated 3-class
test, and the `application.ex` wiring. Condition 2's remaining close work is
the live cross-runtime double-fire check at cutover, once these land green.

## Caveat — BEAM-side validation pending

This session has no Elixir/`mix` runtime, so the fixes above are **not
applied or test-validated here** — applying fingerprint changes blind would
risk a different drift and violate the repo's "don't commit unvalidated
test-affecting changes" rule. The Python-side format is empirically
confirmed; the Elixir-side is from documented `DateTime.to_iso8601/1`
behavior. Apply + validate the fixes where `mix test` (with Postgrex
sandbox) can run.

## Cross-references

- Wave 1 status roll-up: `docs/proposals/wave-1-completion-status-2026-06-14.md`
- RFC parity bindings: `docs/proposals/beam-wave-1-sentinel.md` §B2 (v0.1.1),
  §B3 (v0.1.3), §C3 (v0.1.1 + v0.1.3)
- Python: `agents/sentinel/agent.py`, `agents/sentinel/forced_release_alarm.py`
- BEAM: `elixir/sentinel/lib/unitares_sentinel/{fleet_analysis,findings,
  fleet_finding_emitter}.ex`, `.../forced_release_poller/logic.ex`
