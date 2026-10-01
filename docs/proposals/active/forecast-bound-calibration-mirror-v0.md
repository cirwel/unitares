# Forecast-bound calibration mirror (v0)

**Status:** DRAFT decision record, 2026-10-01, revised the same day after
adversarial design review (Section 8). Documentation only. This document
changes no runtime behavior, schema, flag, default, threshold, response shape,
or registered protocol, and it authorizes nothing.
**Recommendation:** do **not** build the forecast mirror first drafted here.
Section 6 explains why and names the options that remain.
**Scope:** whether UNITARES can give an agent a calibration readout that makes
honest confidence reporting its best strategy, rather than only harmless.
**Inputs:** PR #2603 (claim provenance on `auto_attest`, merged 2026-10-01);
KG finding `2026-10-01T05:54:28.762573+00:00`; the outcome-binding assurance
case ([`outcome-binding-v0.md`](../../evaluations/assurance-cases/outcome-binding-v0.md));
the fixture-conflation decision packet
([`outcome-fixture-conflation-decision-packet-v0.md`](../archive/outcome-fixture-conflation-decision-packet-v0.md));
the outcome-grounding stop rule
([`eisv-outcome-grounding-stop-rule-v0.md`](../registered/eisv-outcome-grounding-stop-rule-v0.md), #1425).

---

## 1. The question

There are two ways to stop an agent from gaming a self-report:

1. **Can't gain.** The report carries no payoff, so gaming buys nothing.
2. **Doesn't want to.** Honest reporting is the agent's best strategy.

UNITARES already does (1) for confidence: calibration readouts are mirror
only, with no verdict, risk, or trust-tier effect. The first draft of this
document set out to reach (2) with a proper scoring rule (Brier) applied to
forecasts bound before their outcome.

## 2. The finding: (2) needs stakes, and stakes need a resolver the agent cannot reach

A strictly proper scoring rule makes honest reporting the unique best
response **only for a forecaster whose utility rises with the score**. Strip
every payoff, as UNITARES deliberately does, and every report is equally
good. The rule then provides (1), not (2).

If the agent does value its own number, for example because a model treats
any displayed score as something to improve, the cheapest strategies are not
honest hard forecasts:

- forecasting a rerun after already seeing it pass;
- forecasting commands whose outcome the agent controls;
- not forecasting at all;
- quietly abandoning forecasts that start to look wrong, so they expire unscored.

Proper scoring is incentive-compatible only when these conditions all hold:

| # | Condition | Status on this deployment |
|---|---|---|
| C1 | Forecast committed before the outcome, with an ordering the server can verify | **Not met.** Outcomes carry server receipt time, not execution time (`src/db/mixins/tool_usage.py:161-165`) |
| C2 | Outcome produced by a party the forecaster cannot write as | **Not met.** See below |
| C3 | The scored value is the agent's own report, with no other effect | **Not met.** Check-in `confidence` already drives λ1 gating, continuity signals, and the drift vector (`governance_monitor.py:1235-1271`, `:1397-1427`) |
| C4 | Every committed forecast is scored, including abandoned ones | **Not met.** An unresolved forecast has no label to score |
| C5 | The forecaster does not choose which questions are asked | **Not met.** The agent picks what to forecast |
| C6 | The forecaster cannot steer the outcome after forecasting | **Not met** for any target that is the agent's own code or command |
| C7 | The score has some value to the forecaster | **Deliberately not met**: nothing rides on calibration |

**C2 is the binding constraint here.** The only external producer is the
harness outcome hook (`~/scripts/hooks/outcome-tracker.sh`, operator
fleet-ops, not this repo). It reads `UNITARES_OPERATOR_TOKEN` from a file
owned by the same OS user every agent process runs as, and agents routinely
edit that directory. `POST /v1/harness/outcome` accepts a caller-chosen
`agent_uuid`, `verification_source`, and `detail`
(`src/http_routes/substrate.py:331`, `:345-361`). Any agent on the host can
therefore post the "external" grade for its own forecast. No mechanism inside
the server can fix that; it needs process isolation that a single-user host
does not have.

**C7 conflicts with the rest of UNITARES.** Adding stakes to repair C7 would
make C2 through C6 load-bearing, and reopen the gaming problem (1) solved.
The two goals pull against each other: with no stakes there is no incentive,
and with stakes there is something to game.

## 3. What exists today (verified)

Each item was checked against `master` at `4e4540b82` and the live database
on 2026-10-01, read-only.

**Forecast minting.**
- A check-in carrying caller `confidence` mints a `prediction_id`
  (`src/monitor_calibration.py:87-91`).
- Server-derived confidence is never registered (`:26-27`).
- The registered value is the post-clamp one: weak identities are capped at
  0.55 (`src/mcp_handlers/updates/phases.py:849-853`).
- The registry (`src/monitor_prediction.py:28-37`) stores no provenance.
- It lives in monitor memory plus a per-agent file snapshot
  (`governance_monitor.py:449-458`, `:532-547`), with a 3600 s TTL. Nothing
  reaches the database at mint.

**Outcome binding.**
- `audit.outcome_prediction_bindings` (migration 070) holds one canonical
  outcome per `(agent_id, prediction_id)`.
- Only self-report paths carry a `prediction_id`:
  - public `record_result`, forced to `agent_reported_tool_result`
    (`outcome_events.py:898-899`);
  - the Phase-5 emitter (`phases.py:2400`), which runs in `shadow` on the
    maintainer deployment.
- The harness route ignores `prediction_id` on purpose (`substrate.py:388`;
  rationale at `:305-311`, #1445).

**The harness hook as a resolver.**
- It fires only on commands matching `pytest|test-cache\.sh`
  (`outcome-tracker.sh:130`).
- It stores the first 400 characters of the command (`:149`).
- It sends the classifier's label and parsed counts, not an exit code
  (`:150-152`).
- It withholds failures whose command text carries "induced" markers, or
  whose command writes a test file (`classify_test_outcome.py:207-215`).
  Passes are never withheld.
- It drops `test-cache.sh` cache hits (`:182-185`).
- It posts fire-and-forget, with no retry (`outcome-tracker.sh:109-113`).

### Live counts (read 2026-10-01)

| Quantity | Value |
|---|---|
| Outcomes bound through the registry, all time | 448, across 212 agents (p50 1 per agent, max 20) |
| of which Phase-5 shadow rows (calibration-excluded) | 293, across 79 agents |
| of which other bound rows | 155, across 135 agents (p50 1, max 3) |
| Agents with 10 or more non-excluded bound rows | 0 |
| Non-excluded bound rows graded by the forecaster itself | 151 of 155 |
| Bound rows from `external_signal` | 4: 2026-06-16 (two), 2026-06-26, 2026-08-02 |
| Non-excluded bound `task_*`/`test_*` rows, last 30 days | 21 |
| Harness `test_*` outcomes, last 30 days | 2,957 across 120 agents (p50 8 per agent, max 702) |
| of which stored command is exactly 400 characters (truncated) | 2,088 (71%) |
| of which command pipes into `tail`, `head`, `grep` or `tee` | 1,294 (44%) |

Of the rows that already have a registered forecast, 151 of 155 were graded
by the agent that made the forecast. A median of one bound outcome per agent
means no per-agent calibration curve is reachable at current volume, with or
without this design.

## 4. The design that was reviewed, and why it fails

The first draft proposed the following. It is retained here so the review is
legible.

- An optional `forecast_target = {kind: "command", ref}` argument on the
  check-in, carried alongside `confidence`: "the next run of this exact
  command exits 0".
- A `forecast.registered` row in `audit.events` at mint.
- Resolution by the harness route, matching a digest of the command.
- A mirror line and per-identity tally on the next check-in.
- An offline operator table.
- A park rule: stop if fewer than 20 forecasts resolve in the first 30 days.

Adversarial review found that it fails every condition in Section 2:

- **C1.** Matching on "minted before the outcome's timestamp" uses server
  receipt time. A delayed or replayed POST can resolve a forecast minted
  after the result was known, and a server `outcome_id` does not deduplicate
  executions.
- **C2.** See Section 2. "Same trust as other `external_signal` rows" is not
  much trust: all 2,957 of those rows in 30 days are `calibration_excluded`.
- **C3.**
  - Riding on check-in `confidence` puts every forecast into λ1 gating,
    continuity signals, the drift vector, and the tactical registry. That
    last one lets a self-graded `record_result` bind the same id and train
    calibration with a different label.
  - An honest low forecast therefore changes the agent's own EISV inputs.
  - Scoring the pre-clamp value would also contradict #2603, which treats a
    clamped value as not the agent's claim.
- **C4.**
  - Mean Brier over resolved forecasts only is not a proper score of the
    forecasts made.
  - An agent can abandon a likely loser just by changing the command text.
    `pytest -q x` does not match `pytest x -q`.
  - The hook's outcome-dependent withholding makes passes more likely to
    resolve than failures.
- **The event scored is not the event forecast.**
  - 71% of stored commands are truncated, so a full-command digest can never
    match them.
  - The label is the classifier's parsed pytest summary, not the shell exit
    status, and 44% of commands are pipelines.
- **Ordering and attribution.**
  - Several open forecasts on one command resolve against different runs.
  - Concurrent sessions and subagents sharing a UUID resolve each other's
    forecasts.
  - A dropped POST shifts "the next run" to a later one.
- **Durability.**
  - `audit.events` writes are fire-and-forget by design
    (`src/audit_log.py:688-743`).
  - The table has no key on forecast or terminal state, so "resolves at most
    once" is not enforced.
  - Partitions drop at 180 days (`db/postgres/partitions.sql:568`).
  - The `confidence` column defaults to 1.0 and would leak into the
    audit-trail fallback (`src/db/mixins/audit.py:261-291`).
- **Reach.** The only resolver is a Claude Code hook on one host. Codex,
  SDK residents, and other installs have none.
- **The park rule.**
  - It would retire a capability on a usage count from a phase agents cannot
    see. That is the conflation `CLAUDE.md` *Measurement authority* forbids.
  - It could also be met by one agent rerunning a passing command twenty
    times.
- **Stop-rule interaction.** Asking agents to send more `confidence` changes
  which check-ins carry reported confidence inside the 2026-12-01 read
  window. So the claim that no input to that read changes was false.

## 5. What a sound version would need

These are prerequisites, not a design. Each is a separate decision.

- **A separate forecast namespace.** A forecast is its own verb, with its
  own id, never the check-in `confidence`, never in the tactical registry,
  and never consumable by `record_result`.
- **Execution receipts from outside the agent's write authority.**
  - Each receipt carries a full-command digest, the exit status, the start
    and end times, and a receipt nonce.
  - The producer is a process the agent's OS user cannot impersonate.
  - On a single-user host that needs sandboxing UNITARES does not provide.
- **A durable, keyed store.**
  - A table keyed on forecast id, with an atomic open-to-terminal
    transition and its own retention.
  - Registration acknowledged before the forecast id is returned.
- **Server-chosen questions, or scoring over all observed runs.** This keeps
  the agent from choosing what is asked, which C5 requires.
- **Stakes the agent values.** Without this, items 1 to 4 buy (1) again,
  not (2), and this is the one that conflicts with UNITARES' mirror-only
  posture.

## 6. Recommendation and options

**Recommendation: A.** The question this document set out to answer has a
clean answer: (2) is not reachable while UNITARES keeps calibration
payoff-free, and keeping it payoff-free is the right call for a
single-operator kernel whose agents share an OS user with their observers.

- **A. Record and stop (recommended).**
  - Keep #2603's mirror as is.
  - Treat "can't gain" as the achieved property, and say so where gaming
    resistance is described.
  - Fix nothing in the forecast path.
- **B. Build the receipts channel only,** if another consumer needs honest
  execution records: full command digest, exit status, no truncation, no
  withholding. It is independently useful (finding F5), but it does not
  deliver (2) without the rest of Section 5.
- **C. Pursue (2).**
  - Requires every Section 5 item, including stakes. That reverses a
    standing design choice.
  - It belongs in an operator decision, not a proposal.

Whatever the choice, the "doesn't want to" lever available today is not a
score. It is what the agent is told: the record is for its own use, and
nothing rides on the number. That is a framing claim about LLM agents, not a
mechanism, and it is untested here.

## 7. Findings recorded while scoping (not fixed here)

- **F1. Post-hoc confidence trains calibration.**
  - Bound rows whose confidence came from the `record_result` argument
    (`argument_fallback`, and `missing_prediction` or `ttl_expired_fallback`
    with an argument) train every calibration channel as registry rows do
    (`outcome_events.py:403-409`, `:641-702`).
  - That confidence is stated at outcome time.
  - Changing it moves `cal_I` inside the registered read window, so this is
    flagged, not fixed.
- **F2. A fabricated `prediction_id` still claims a binding-ledger row**
  (`outcome_events.py:536-564`); there is no registry check before the claim.
- **F3. Phase-5 `enable` mode would produce post-hoc `registry` rows that are
  not calibration-excluded** (`phases.py:246-281`). The maintainer
  deployment runs `shadow`, so this is latent.
- **F4. Stale comment.** `governance_monitor.py:2069` says the registry is
  in-memory only, but the file snapshot has persisted it since #2411. KG
  finding `2026-09-24T10:22:28.558342+00:00` describes the pre-fix state.
- **F5. The harness hook's outcome record is lossy.**
  - `detail.command` is cut to 400 characters (71% of rows in 30 days).
  - The label is the classifier's, not an exit status.
  - Failures with "induced" markers are withheld, while passes are not.
  - Anyone reading `detail.command` as the full command, or reading the
    labels as unbiased, should know this. (An earlier draft read the
    truncation as 44% of rows not being test runs. That reading was wrong:
    the hook gates on the full command before truncating.)
- **F6. Per-agent sequential e-process state is persisted and used only for
  class rollups** (`sequential_calibration.py:394-402`, rebucket at
  `:659-693`). It is never shown per agent; every production
  `compute_metrics` caller is fleet-only.

## 8. Review record

Three independent passes plus a second model family reviewed the first draft
(commit `fb630a00e`) on 2026-10-01. All were read-only.

| Pass | Result |
|---|---|
| Adversarial design review | 17 findings (7 P1). Established the C7 point (no stakes, no incentive), C2 reachability of the operator token, C3 side effects of check-in confidence, C4 abandonment, C5 and C6 omissions, truncation, and the park-rule flaw |
| Code-claim verification | About 35 citations confirmed. Corrected: F6, durability and retention of `audit.events`, the `confidence` column default, session enforcement, resolver label versus exit code, and citation drift |
| Live-data verification | 13 numbers confirmed. Corrected: the `external_signal` dates and the F5 interpretation (truncation, not misclassification) |
| Codex (`gpt-6-astra`) | FINDINGS(9), 4 P1. Isolation breach via check-in confidence, missing exit code and truncation, ordering and replay, and selective resolution. Agrees with the passes above |

The draft's mechanism is superseded by Sections 2, 4 and 6 of this revision.
