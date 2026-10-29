# Independent-operator cohort

UNITARES is looking for one operator who runs at least one genuine agent
workload and wants a durable record across its sessions, restarts, and
handoffs. This is a bounded usability study, not a sales program and not a
request to generate synthetic traffic.

The study's protocol was registered before recruitment began. Negative,
inconclusive, and abandoned runs are reported as readily as successful ones.
The complete protocol is
[`independent-operator-cohort-preregistration-v0.md`](../proposals/registered/independent-operator-cohort-preregistration-v0.md),
tracked by [issue #1607](https://github.com/cirwel/unitares/issues/1607).

## Who fits

The study is intended for an operator who:

- runs one or more coding, research, background, or resident agents or
  recurring agent jobs on real work;
- is comfortable running a Docker Compose stack on infrastructure they
  control;
- can keep at least one genuine agent workload connected for four weeks;
- can name at least one real outcome source, such as CI, a test harness, or a
  review tool; and
- is willing to record setup friction and permit publication of the result,
  including failure or abandonment.

The operator must not share the maintainer's production database. The study
uses the operator's own release-tagged installation and keeps its traffic
separate from the maintainer deployment. This guide promises no compensation;
any consideration would be agreed and disclosed before enrollment.

## What participation involves

1. **Short fit call.** Confirm the workload, infrastructure, outcome source,
   relationship to the maintainer, and publication expectations.
2. **Install and connect.** Start a release-tagged Docker Compose stack, run
   the quickstart check, record the demo agent's `identity_id`, delete that
   identity's rows, note the deletion timestamp, and connect a real agent.
3. **Plumbing check.** Emit at least three outcomes through each declared
   producer and confirm that the recorded rows are attributable and visible
   to the registered analysis.
4. **Enrollment.** Add the release, fleet shape, producers, harness hashes,
   demo identity and deletion timestamp, plumbing counts, and publication
   consent to the append-only
   [enrollment ledger](../proposals/active/independent-operator-cohort-enrollments.md).
5. **Operate for 28 UTC days.** Use the agents for their ordinary work. Keep a
   timestamped friction log and record maintainer-support time.
6. **Review the report.** The operator reviews and approves every public post
   about their deployment. The primary result is operational usability; the
   study does not claim that governance prevented an incident.

The participation window begins only after a valid enrollment has merged and
the first non-demo agent completes a qualifying check-in.

### Record and remove the quickstart identity

`make demo` prints the demo agent's `agent_uuid`; the enrollment ledger instead
records the matching internal `identity_id`. Copy the printed UUID exactly,
then look up and record the numeric ID before deleting anything:

```bash
DEMO_AGENT_UUID='<agent_uuid printed by make demo>'
docker compose exec -T postgres-age psql \
  -U "${POSTGRES_USER:-postgres}" -d "${POSTGRES_DB:-governance}" \
  -Atc "SELECT identity_id FROM core.identities WHERE agent_id = '$DEMO_AGENT_UUID';"
```

An empty result means stop and diagnose the install. After recording the ID,
remove the demo's direct audit rows and its core record; the schema's foreign-
key cascades remove the associated identity, session, state, and baseline rows:

```bash
docker compose exec -T postgres-age psql \
  -U "${POSTGRES_USER:-postgres}" -d "${POSTGRES_DB:-governance}" \
  -v ON_ERROR_STOP=1 -c "
BEGIN;
DELETE FROM audit.outcome_prediction_bindings WHERE agent_id = '$DEMO_AGENT_UUID';
DELETE FROM audit.outcome_events WHERE agent_id = '$DEMO_AGENT_UUID';
DELETE FROM audit.coordination_events WHERE agent_id = '$DEMO_AGENT_UUID';
DELETE FROM audit.tool_usage WHERE agent_id = '$DEMO_AGENT_UUID';
DELETE FROM audit.events WHERE agent_id = '$DEMO_AGENT_UUID';
DELETE FROM audit.rate_limits WHERE agent_id = '$DEMO_AGENT_UUID';
DELETE FROM core.onboard_pins WHERE agent_uuid = '$DEMO_AGENT_UUID';
DELETE FROM core.session_bindings WHERE agent_uuid = '$DEMO_AGENT_UUID';
DELETE FROM core.agents WHERE id = '$DEMO_AGENT_UUID';
COMMIT;"
```

The final delete must report `DELETE 1`. Record the UTC completion time in the
enrollment entry as the demo-row deletion timestamp.

## What the maintainer provides

- a release-tagged install path and a guided first setup within the study's
  disclosed support budget;
- help connecting Claude Code, Codex, Hermes Agent, or another MCP client;
- diagnosis of setup and integration failures;
- a public record of every maintainer-support interaction and its approximate
  duration; and
- preparation of the enrollment and result artifacts for the operator to
  review.

Questions are allowed and logged. The study may claim that the tested release
was independently deployable only if all four registered conditions hold:

1. no repository change was made in response to a blocker the participant
   raised during setup or the window;
2. at least 14 of the 28 window days contain a qualifying check-in;
3. the day-28 capture has a behavioral primary rate of at least 0.5 and zero
   contract violations; and
4. no `blocked` friction item remains unresolved at window end, with no more
   than 120 total minutes of logged maintainer support.

Anything less is still published as a usability result, but the independent-
deployment claim is not made.

## Data and claims

UNITARES is self-hosted. The participant keeps their database. Shared study
artifacts are limited to the registered harness outputs, telemetry-health JSON
bodies, the friction log, support time, and enrollment metadata. A participant
may opt in to sharing a de-identified analytic slice; otherwise the secondary
analysis is described as attested rather than independently reproducible.

The study can establish whether one independent operator deployed and operated
the tested release under the registered conditions. It cannot establish broad
adoption, predictive validity across fleets, or causal incident prevention.

## Express interest

Supply these details either in a comment on
[issue #1607](https://github.com/cirwel/unitares/issues/1607) or by email at
`founder@cirwel.org`:

- the agent clients or runtimes you operate;
- roughly how many agents or recurring jobs are active;
- the real work they perform;
- the outcome source you could connect; and
- the host on which you would run Docker Compose.

If those details should not be public, comment only that you are interested
and send the five-item intake by email. The issue comment leaves a visible
recruitment trace without exposing deployment details.

Recruitment closes on **2026-11-30**. If no valid operator enrolls by that
date, the protocol requires recording recruitment as the unresolved dependency
rather than substituting maintainer or synthetic traffic.
