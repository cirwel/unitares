# Forecast-bound calibration mirror (v0)

**Status:** DRAFT proposal, 2026-10-01. Documentation only. This document
changes no runtime behavior, schema, flag, default, threshold, response shape,
or registered protocol. Merging it authorizes nothing; each phase in
[Section 8](#8-phases-and-what-each-needs) needs its own operator decision.
**Scope:** how an agent can get a calibration readout on its own confidence
that rewards reporting what it actually believes, and that the agent cannot
improve by gaming its inputs.
**Inputs:** PR #2603 (claim provenance on `auto_attest`, merged 2026-10-01);
KG finding `2026-10-01T05:54:28.762573+00:00` (the external mirror can never
reach its floor); the outcome-binding assurance case
([`outcome-binding-v0.md`](../../evaluations/assurance-cases/outcome-binding-v0.md));
the fixture-conflation decision packet
([`outcome-fixture-conflation-decision-packet-v0.md`](../archive/outcome-fixture-conflation-decision-packet-v0.md));
the outcome-grounding stop rule
([`eisv-outcome-grounding-stop-rule-v0.md`](../registered/eisv-outcome-grounding-stop-rule-v0.md), #1425).

---

## 1. The question

Two ways to stop an agent gaming a self-report:

1. Make the report carry no payoff, so gaming gains nothing.
2. Make honest reporting the agent's best strategy, so it does not want to game.

UNITARES already does (1) for confidence: calibration readouts are mirror
only, with no verdict, risk, or trust-tier effect. That keeps the report
safe, but it gives the agent no reason to report carefully. This proposal is
about (2).

A proper scoring rule such as the Brier score gives (2), but only when four
conditions hold:

| # | Condition | Why it is needed |
|---|---|---|
| C1 | The forecast is committed before the outcome exists | Otherwise the "forecast" can be read off the result |
| C2 | The outcome label is produced by someone other than the forecaster | Otherwise the forecaster writes both sides of the score |
| C3 | The scored value is the agent's own report, not a server estimate | Scoring a derived or clamped value scores the server, not the agent |
| C4 | Every committed forecast stays in the denominator | Otherwise the agent resolves only the forecasts that went well |

A fifth condition sits outside the scoring rule: **nothing besides the mirror
may ride on the score**. Once a verdict, a trust tier, or a ranking depends
on it, the agent is better off optimizing the number than reporting its
belief.

## 2. What exists today

The plumbing for C1 is mostly built. Each condition below is checked against
`master` at `4e4540b82` and the live database on 2026-10-01 (read-only).

**Forecast minting (C1, C3 partly met).**
- A check-in that carries a caller `confidence` mints a `prediction_id`
  (`src/monitor_calibration.py:87-91`).
- Server-derived confidence is never registered
  (`monitor_calibration.py:26-27`; `governance_monitor.py:1210`).
- The registered value is taken after the weak-identity clamp
  (`phases.py:849-853`), and the registry does not record that a clamp
  happened (`monitor_prediction.py:28-37`).
- The id comes back in every response mode except `minimal`
  (`update_response_service.py:40-43`; `envelope_step.py:2966`).

**Forecast durability (C4 not met).**
- The registry lives in the monitor's memory and its per-agent file snapshot
  (`governance_monitor.py:449-458`, `:532-547`), with a 3600 s TTL.
- Nothing reaches the database at mint time. A forecast that never receives
  an outcome leaves no durable trace, so the denominator in C4 cannot be
  reconstructed. `scripts/analysis/prospective_prediction_cohort.py:45-47`
  records the same limit.

**Outcome binding (C2 not met).**
- `audit.outcome_prediction_bindings` (migration 070) holds at most one
  canonical outcome per `(agent_id, prediction_id)`.
- Only two paths carry a `prediction_id`, and both are self-report:
  - public `record_result`, forced to `agent_reported_tool_result`
    (`outcome_events.py:898-899`);
  - the Phase-5 evidence emitter (`phases.py:2402`), which runs in `shadow`
    on the maintainer deployment, so its rows are calibration-excluded.
- The external path, `POST /v1/harness/outcome`, ignores `prediction_id` on
  purpose (`src/http_routes/substrate.py:305-311`). It takes an
  operator-asserted `agent_uuid` with no work correlation, so binding "any
  open prediction of that agent" would launder an unrelated outcome into
  calibration. PR #1445 removed exactly that.

**The only per-agent readout shown to agents.**
- `enrich_external_grounding` (`enrichments.py:258-306`) shows a Brier score
  over `external_signal` outcomes.
- It joins each outcome to the agent's latest reported confidence in the
  previous 24 h (`tool_usage.py:579-593`). That is a time join, not a
  binding, so C1 and C4 fail by construction.
- Its floors (`MIN_N=10`, `MIN_BATCHES=3`) have never been met.

### Live counts (all time unless stated, read 2026-10-01)

| Quantity | Value |
|---|---|
| Outcomes bound through the registry | 448, across 212 agents |
| of which Phase-5 shadow rows (excluded) | 293, across 79 agents |
| of which other rows (`record_result`) | 155 |
| Bound rows per agent (all 448) | p50 1, max 20 |
| Agents with 10 or more non-excluded bound rows | 0 |
| Non-excluded bound rows graded by the forecaster itself (`agent_reported_tool_result`) | 151 of 155 |
| Bound rows from `external_signal` | 4, all dated 2026-05-30 to 2026-06-16, before #1445 |
| Non-excluded bound `task_*`/`test_*` rows, last 30 days | 21 |
| Outcome label among non-excluded bound rows | 153 good, 2 bad |

So binding a score to a registered forecast is not enough on its own. Of the
rows that would pass C1 today, 97% fail C2: the agent graded its own
forecast. And with a median of one bound outcome per agent, no per-agent
calibration curve is reachable at current volume.

## 3. Design principles

- **P1. Forecasts name what they predict.** A forecast carries a *target*: a
  concrete event, declared at mint time, that a party other than the
  forecaster can resolve. Binding is by target, not by time window and not
  by "the agent's open prediction". This answers #1445: the work correlation
  the harness route lacks is supplied by the forecast itself, before the
  outcome exists.
- **P2. Resolution comes from outside the forecaster.** Only a resolver the
  agent does not author can close a targeted forecast. The forecaster's own
  `record_result` never resolves one.
- **P3. Every forecast is durable from mint.** Registration writes a record
  at mint time. An unresolved forecast stays visible as unresolved; it is
  never scored and never dropped.
- **P4. Score the agent's own words.** The scored value is the confidence the
  agent sent, before any server clamp. The clamp is recorded beside it.
- **P5. The mirror is the only consumer.** Forecast records and their
  resolutions feed the agent-facing mirror and an offline operator table,
  nothing else: no verdict, no risk, no trust tier, no EISV input, no
  `calibration_checker` bins, no sequential e-process, and no rows in
  `audit.outcome_events`. See [Section 6](#6-interaction-with-registered-protocols).
- **P6. No pooling across identities.** The readout is per identity. Pooling
  by harness or model key was considered and rejected (PR #2603): the key is
  caller-assertable, and a pooled score is not proper for any one agent.

## 4. Mechanism

### 4.1 Targeted forecast

`sync_state` / `process_agent_update` gain one optional argument:

```text
forecast_target = {"kind": "command", "ref": "<exact command text>"}
```

It is accepted only together with a caller `confidence`, and it means: *my
confidence that the next run of this exact command in this session exits
0.* v0 has exactly one kind. Others, such as the CI conclusion for a commit
SHA, are deferred (Section 9).

`command` comes first because external resolution for it already exists.
The harness hook posts outcomes for observed test commands, carrying the
command text in `detail.command`. In the last 30 days that is 2,957
`external_signal` test outcomes across about 120 agents (p50 8 per agent). A
forecast that names its command removes the need for the hook's own
classification of what counts as a test. That matters: 1,309 of those 2,957
rows, 44%, are not test-runner commands at all (for example, inline Python
edit scripts recorded as `test_passed`). The target is matched against the
declared command, not against the hook's label.

### 4.2 Durable registration

At mint, the server writes one `audit.events` row, `event_type =
'forecast.registered'`:

| Field | Value |
|---|---|
| `agent_id`, `prediction_id` | as minted |
| `reported_confidence` | the caller's value before the weak-identity clamp |
| `registered_confidence` | the value the registry holds |
| `clamped` | whether the clamp changed it |
| `target` | `{kind, ref_digest}`; the raw command stays out of the audit row |
| `minted_at` | wall clock |
| `expires_at` | mint + TTL |

`audit.events` is used rather than a new table, so Phase 1 needs no
migration, as `mirror_signal.emit` did in the mirror-effectiveness work. A
forecast without a target is still minted as today and gets no
`forecast.registered` row. It is never scored by this mirror.

### 4.3 Resolution

When the harness route receives an outcome for `agent_uuid` with
`detail.command`, the server finds that agent's earliest open targeted
forecast whose `ref_digest` matches the command's digest. It must be minted
before the outcome's timestamp and not yet expired. If one matches, the
server writes `event_type = 'forecast.resolved'` with `prediction_id`,
`outcome` (exit 0 or not), the outcome's `outcome_id`, and the Brier term.

Each forecast resolves at most once, and only the earliest match counts. A
forecast that expires unmatched gets `forecast.expired` on the next sweep, or
lazily on read. Nothing in this path writes `audit.outcome_events`, touches
the binding ledger, or calls a calibration channel.

### 4.4 The mirror

On the forecaster's next check-in, the response carries one line per
resolution since its last check-in, then a tally:

```text
forecast: 0.85 that `<command>` exits 0 -> it failed (Brier 0.72)
forecasts this identity: 6 registered, 4 resolved, 2 unresolved; mean Brier 0.11 over 4
```

- **No verdict words.** No "well calibrated", no "overconfident". Below 10
  resolutions the tally shows counts and the mean only.
- **Coverage is always shown.** Registered, resolved and unresolved counts
  appear together, so selective forecasting is visible to the agent itself.
- **Silent when there is nothing to report.** No targeted forecasts means no
  line, the same rule `enrich_external_grounding` follows.

### 4.5 Operator table

An offline script, in the mold of `scripts/dev/mirror_effectiveness_reeval.py`,
reads the two event types and reports the following. It is operator-facing
only.

- Per identity and fleet-wide: registered, resolved, expired, mean Brier, and
  a reliability table.
- Grouping only by server-recorded fields. `model_type` is caller-supplied,
  so any grouping by it is labelled as such.

## 5. Threats and responses

| Threat | Response | Residual |
|---|---|---|
| Forecast after seeing the result (C1) | The match must be the first run of the declared command *after* mint. Runs before mint cannot resolve it | The agent can run the command, see it pass, then forecast the rerun at 0.99. That is an honest, uninformative forecast. The score is good and teaches nothing. Accepted |
| Grade your own forecast (C2) | `record_result` never resolves a targeted forecast; only the harness outcome path does | The harness hook runs in the agent's own client. Its outcome is the command's real exit code, not the agent's prose, so this is substrate observation, not self-report |
| Choose a command whose outcome you control (`true`, `exit 1`) | Nothing rides on the score (P5). Gaming yields only a nicer number the agent shows itself | Accepted, by design. The aim is that honest reporting is the cheapest path, not that gaming is impossible |
| Resolve only the forecasts that went well (C4) | Every targeted forecast is durable from mint; unresolved and expired counts are always shown | None within the TTL |
| Score a server value (C3) | The pre-clamp `reported_confidence` is scored; derived confidence never registers | None |
| Fabricated or replayed `prediction_id` | Only ids with a `forecast.registered` row resolve; `prediction_id` is server-minted and resolution never takes it from the caller | None for this mirror. The binding ledger's own gap is out of scope (Section 7, F2) |
| Spoofed harness outcome | The harness route is operator-gated (`substrate.py:314-321`) | Same trust as every `external_signal` row today |
| Abstain: never forecast | Abstention is free here, and stays visible to the operator table | Accepted. An incentive to forecast would be a payoff, which P5 forbids |
| Pooling by an assertable key | Not done (P6) | None |

## 6. Interaction with registered protocols

- **Stop rule (#1425) and the 2026-12-01 read.** The read draws on
  `audit.outcome_events` and the anchors under `--anchor-scope trusted`. This
  design writes neither: resolutions live in `audit.events` only, and the
  harness outcome row is written exactly as today. No cohort, label, or
  threshold the read uses changes. That is why P5 keeps forecast resolutions
  out of `outcome_events` and out of the calibration channels, at least until
  the operator decides otherwise after the read.
- **Independent-operator cohort preregistration.** Its condition 4 requires
  that a producer with no genuine confidence sends none. That is unchanged:
  the harness hook still sends no confidence, and the forecast is the
  agent's own.
- **`cal_I` and the EISV estimate.** Untouched. Forecast records never enter
  `calibration_checker` or the sequential tracker.

## 7. Findings recorded while scoping (not fixed here)

- **F1. Post-hoc confidence trains calibration.**
  - Bound rows whose confidence came from the `record_result` argument
    (`argument_fallback`, and `missing_prediction` or `ttl_expired_fallback`
    with an argument) train every calibration channel exactly as registry
    rows do (`outcome_events.py:403-409`, `:641-702`).
  - That confidence is stated at outcome time, possibly after the result is
    known.
  - Changing it moves `cal_I` inside the registered read window, so it is
    flagged, not fixed.
- **F2. A fabricated `prediction_id` still claims a row in the binding
  ledger** (`outcome_events.py:536-564`). The assurance case already scopes
  authorship out; the row itself is new information.
- **F3. Phase-5 `enable` mode would produce post-hoc `registry` rows that are
  not calibration-excluded** (`phases.py:246-281`). The maintainer deployment
  runs `shadow` today, so this is latent.
- **F4. A stale comment.** `governance_monitor.py:2069` says the registry is
  in-memory only; the file snapshot (`:532-547`) has persisted it since the
  restart fix. KG finding `2026-09-24T10:22:28.558342+00:00` describes the
  pre-fix state.
- **F5. 44% of harness `test_*` outcomes are not test-runner commands.** In
  the last 30 days, 1,309 of 2,957 are not test-runner commands (pattern:
  pytest, test-cache, npm, cargo, go, mix, unittest). Section 4.1 sidesteps
  this; anything that reads those labels as test results does not.
- **F6. Per-agent sequential e-process state is computed and never read.**
  `sequential_calibration.py:386-414` updates `agent_states`; every
  production caller of `compute_metrics` is fleet-only.

## 8. Phases and what each needs

Each phase is separately authorized and separately revertible. None changes
an existing response field.

| Phase | Change | Needs |
|---|---|---|
| 0 | This document | Operator read |
| 1 | `forecast_target` argument; `forecast.registered` rows; pre-clamp value carried to the registry. Flag `UNITARES_FORECAST_TARGETS`, default off | Correctness review; no migration |
| 2 | Harness-route resolver; `forecast.resolved` / `forecast.expired` rows | Correctness review; shadow first (resolutions written, nothing shown) |
| 3 | Mirror line and tally on the next check-in | Operator decision after Phase 2 data |
| 4 | Offline operator table | Any time after Phase 2 |

**Park criterion, stated before data.** If 30 days after Phase 2 is enabled
fewer than 20 targeted forecasts have resolved across all identities, stop at
Phase 2. Record the count, and do not build Phase 3. Uptake is voluntary:
plugin hooks must not send confidence, so only agents that choose to forecast
produce rows.

## 9. Deliberately not proposed

- **Other target kinds** (CI conclusion for a SHA, PR merge, a named test
  ID). CI needs a server-side GitHub observer that does not exist. Add kinds
  only after `command` shows uptake.
- **Any payoff for good calibration**: trust tier, risk, routing, ranking.
  That would break P5 and turn the score into a target.
- **Cross-identity pooling**, including through declared lineage. A declared
  parent is caller-asserted, and inheriting a record would let an agent
  choose its ancestry for a better number.
- **Repairing F1 to F6** inside this work. Each is its own decision, and F1
  touches the registered read window.
- **Changing `enrich_external_grounding`.** It stays as PR #2603 left it:
  honest and silent. If Phase 3 ships, retiring it is a separate decision.
