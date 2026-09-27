# Inference host daily probe v0

Status: design only, awaiting operator approval. Nothing here is built.
Date: 2026-09-26

## Problem

A broken inference option is found only when someone happens to choose it.
From 2026-09-25, every thorough `consult` from a Claude caller went to
`codex:host-adapter` first and failed on a Codex usage limit for about a day
before anyone noticed (#2486). #2486 now routes around a host at its limit,
and the Redis follow-up keeps that knowledge across restarts. Neither helps
with a failure the classifier does not recognise. In that case the host stays
in rotation and fails each caller in turn, and nothing records it.

Failures of that kind have happened before on this path. On 2026-08-29,
`codex:host-adapter` exited 0 and then lost its answer at the terminal
envelope (`adapter_status=malformed`, an orchestrator output-capture defect).
The CLI worked; gov's path to it did not. The same problem can come from a
logged-out CLI whose error wording the classifier does not match, from an
orchestrator that rejects spawns, or from a CLI upgrade that changes its
output.

## Proposal

Once a day, send one tiny `delegate_inference` call to each enabled host
adapter that has shown no other sign of life. Record a finding when a call
fails for a reason other than a known cooldown.

### Where it runs

The probe is a standalone script, `scripts/ops/inference_host_probe.py`,
scheduled by launchd (`com.unitares.inference-host-probe`,
`StartCalendarInterval` once a day) and registered in the automations census.
It is a separate script, not a new check in `doctor_findings.py`, for two
reasons. `doctor_findings.py` runs hourly and is documented as diagnosis
only, while a probe spends quota and writes an energy record through
`delegate_inference`. And an isolated script costs one launchd entry without
touching a detector that already works, the same reasoning
`doctor_findings.py` itself gives for staying out of `deploy_drift_doctor.py`.

The probe goes through gov's REST tool call (`delegate_inference` on 8767), not
straight to each CLI. Authorization and attribution are separate. The operator
bearer authorizes the REST call, as it does for the other ops scripts. Each
call also carries the `client_session_id` of the probe's own identity (see
*Governance side effects*), so the call is attributed to the probe and not to
the operator or a resident. The path that failed on
2026-09-25 and 2026-08-29 was gov to the orchestrator to the CLI. A direct CLI
call would pass while that path was broken. `model_adjudicator.py` already
calls the `claude` CLI directly every 6 h, and a successful run of it says
nothing about `claude:host-adapter` in gov.

Two placements were rejected:

- **A gov background task.** It would spend quota on every restart unless
  separately gated, and gov would be posting findings about itself.
- **A cloud routine.** A cloud routine sees a fresh clone and cannot reach the
  local orchestrator or the subscription CLIs.

### What it does, per host

Each run starts with a cleanup pass over the probe's own state file (see
*Hung calls*). The pass stops every live execution the probe left behind,
whether or not its host is still configured, so a host removed from the
configuration cannot keep a hung call running. Then, for each host that is
either in `list_inference_hosts` or has records in the probe's state, the
script takes the first matching
row below:

| Host state | Action | Quota spent |
|---|---|---|
| A previous probe's timed-out execution was still live at the cleanup pass (whether or not the stop succeeded) | While the host is enabled, this reproduces its timeout record (already high). For a host the operator has switched off, go to the next row. No new probe either way. A failed stop is reported separately, whatever the host's state (see *Hung calls*). | 0 |
| Out of scope by the operator's choice: `UNITARES_HOST_ADAPTER_ENABLED` off, the host listed in `UNITARES_HOST_ADAPTER_DISABLED_HOSTS`, the host left out of a set `UNITARES_INFERENCE_PROBE_HOSTS` allowlist, or the host no longer in `list_inference_hosts` at all | Log `skipped: not_enabled` (or `removed`), and close the host's records with that reason. Operator choice, not a fault. An enabled host whose CLI has gone missing is not skipped: it is probed, fails at preflight at no quota cost, and is reported. | 0 |
| In a cooldown (`cooldown` field set; `list_inference_hosts` fills it from `host_availability.cooldown()`, which returns nothing once `retry_after` has passed, so a lapsed window never shows) | Log `skipped: cooling until <retry_after>`, with no probe. A `quota` cooldown raises no finding: it clears by itself, and consult routes around it meanwhile. An `auth` cooldown raises the host's `auth` finding (**high**) from the cooldown alone. Real traffic can keep a logged-out host cooling indefinitely, and nobody is told to log in unless this row reports it. | 0 |
| A real call succeeded in the last 24 h (see *Passive evidence*) | Log `skipped: live <age>`. | 0 |
| Otherwise | Probe once. | one call |

The probe prompt is `Reply with exactly: OK`, with `timeout_s=120` and no
retry. Hosts are probed one after another, never in parallel.

A failure before any CLI ran (`dispatch_phase` is `preflight` or
`spawn_rejected`) may be local to one host or shared by all of them. Preflight
covers a missing CLI for that host and also an unset orchestrator bearer. A
spawn can be rejected for one host's spec or because the orchestrator is
unhealthy. The phase alone cannot tell which, so the probe does not stop
early. It checks every host, which costs no quota for pre-CLI failures because
no provider was called. When two or more hosts fail at the same pre-CLI phase
with the same error, it posts one shared finding with fingerprint
`sha("gov-dispatch", phase, error)` in place of per-host findings. Any other
pre-CLI failure stays a per-host finding.

### What counts as a failure, and what it reports

The probe reads the failure class that `delegate_inference` already returns:

| Result | Finding | Why |
|---|---|---|
| Success: a validated answer whose text, trimmed and ignoring case, is `OK` | none; log latency, tokens, model | A call that `delegate_inference` reports as successful but whose text is anything else is an unclassified failure (`unexpected_answer`). That catches a CLI that exits 0 and returns an error message as its answer. |
| Failure classified `quota` | none | `delegate_inference` records the cooldown itself, until the provider's stated reset or, when none is stated, on a backoff from 30 min doubling to 6 h. The limit resets, and failover covers it meanwhile. |
| Failure classified `auth` | **high** | `delegate_inference` puts an auth failure into a cooldown too, on the 30 min to 6 h backoff, so a login that gets fixed is noticed within hours. Unlike a quota limit, though, a logged-out CLI does not recover on its own: the operator has to log in. |
| Pre-CLI failure (preflight, spawn rejected, orchestrator down) | **medium** | Gov never reached the CLI. It is shared across hosts when two or more show it (below). |
| Unclassified failure after the CLI ran (malformed envelope, nonzero exit) | **medium** | The case nothing else records. |
| Timeout (`possibly_running`) | **high**, noted as possibly still running | A probe asks for one word, so a timeout means the host hangs. The severity stays high on later runs too, so a host that keeps hanging is re-posted only under the backoff. See *Hung calls* below. |

Findings go through `agents/common/findings.post_finding`, the same
fingerprinted and deduplicated path Sentinel, Watcher and the doctors use.
They use event type `inference_host_finding` and fingerprint
`sha(host_id, stage, failure_class)`, with the stage from the recovery table
below, so records of one class at different stages stay distinct, and take `doctor_findings.py`'s doubling
re-alert backoff so a host that stays broken does not re-post daily.
Recovery follows `doctor_findings.py`'s rule: a record is closed, and its
backoff dropped, once a run shows the failure is gone, so a later failure of
the same kind alerts at once. What a run shows depends on how far the call got.
A call passes through five stages, and each record belongs to the stage where
its failure happened:

| Stage | Failures recorded there | Observations that reach it |
|---|---|---|
| 1. gov dispatch | preflight, spawn rejected, shared `gov-dispatch` | any call gov attempted |
| 2. CLI started | timeout (the CLI never finished) | any call that got past dispatch |
| 3. CLI finished | `auth` (the CLI reports itself logged out), unclassified | a call whose CLI ran to completion (`dispatch_phase == "terminal"`), or an `auth` cooldown |
| 4. provider answered | `quota` | a quota failure, or a `quota` cooldown |
| 5. success | none | a successful probe, which closes all of the host's records. Passive evidence never appears in this table: it only skips hosts with no open record. |

An observation that reached stage N closes every record from a stage before
N, because those stages evidently worked. Two kinds of observation reflect
earlier state rather than a call made this run: a cooldown, and a hung call
from an earlier run. Those close only records opened before that state began,
since a newer failure at an earlier stage is not contradicted by them. It reproduces a record of its own
class, which stays open and is re-posted under the backoff. It leaves the
host's other records at stage N or later untouched. For example, an old
unclassified record stays open beside a new `auth` one, since an `auth`
failure says nothing about whether the earlier fault is gone. Those records
are neither proven fixed nor re-observed, so the host is probed again on the next run
that it is not cooling. A few consequences:

- A quota cooldown closes an old `auth` record, since the provider answered,
  so the host was logged in.
- A preflight failure closes nothing else. A CLI that has gone missing says
  nothing about whether an old `auth` fault was fixed.
- A hung call found by the cleanup pass reproduces its timeout record.
- A shared `gov-dispatch` record replaces the member hosts' own records of
  that same pre-CLI failure. It lists its member hosts, and each member
  counts as having an open record for every rule that asks, including the
  passive-evidence check. It is reproduced by any run in which two or more
  hosts show that failure, and it closes when fewer do. A single host still
  failing then gets its own record, since the fault no longer looks shared.
- A host the operator switches off has all its records closed, with reason
  `not_enabled`. A host with open records that no longer appears in
  `list_inference_hosts` at all is treated the same way, with reason
  `removed`. The run checks every host in its records, not only the hosts
  listed. A failed stop is not one of the host's records (see *Hung
  calls*), so it is unaffected.

A changed failure is covered the same way. A host that failed preflight
yesterday and fails `auth` today closes the preflight record and opens an
`auth` one. A shared `gov-dispatch` failure follows its own rule above.

Recovery is noticed on the next daily run, so a record can stay open up to a
day after a host recovers. That delay costs nothing more: the backoff already
holds back re-posting, and the finding was posted once. Closing a record
changes local state only. It emits no `outcome_event`, because a probe
passing is not an operator judging the finding correct (roadmap Invariant 4,
the reasoning `doctor_findings.py` records at the same step).

### Hung calls

A call that times out may still be running under the orchestrator, and a CLI
stuck on a prompt would otherwise leave one more child each day. The probe
stores the `orchestrator_execution_id` of every timed-out call in its state
file. Each run's cleanup pass reads the orchestrator's
`GET /v1/executions/<id>` snapshot for each stored id, which does not block.
If that execution is still live, the probe stops it with
`DELETE /v1/executions/<id>`, which the orchestrator documents as stopping
exactly that execution. That id is the probe's own spawn, so no other
caller's work is touched. A stopped or finished execution leaves the state
file. A host whose hung call was found live is not probed again that run, so
each host has at most one probe child alive at a time.

If the orchestrator cannot be reached for the snapshot, the pass changes
nothing: the id stays, and any finding keyed to it stays as it is. Only a
snapshot that says the execution is gone removes an id. If a stop fails, the
id stays in the state file for the next pass, and the
probe posts a **high** finding keyed to that execution id, with fingerprint
`sha("hung-execution", id)`. The finding belongs to the execution, not to the
host. It stays open, whether the host is enabled, switched off, or removed from
the configuration, until a cleanup pass sees the execution gone.

### Passive evidence

A host that served a real call yesterday does not need a probe, so on most
days the check should cost nothing for the busy hosts. The build adds one
small piece to the Redis layer from the cooldown PR: `clear_async`, which
already runs on every successful call, also sets
`unitares:host_last_ok:<host_id>` to the current time with a 48 h TTL.
`list_inference_hosts` shows it as `last_ok`.

The probe's own successes set `last_ok` as well, and they must not count as
passive evidence. If they did, a probe at 04:15:05 would make the next day's
04:15:00 run skip the host, and idle hosts would be probed only every other
day. So after each successful probe, the probe reads that host's `last_ok`
from `list_inference_hosts` and stores the exact value in its state file. It
skips a host only when all four of these hold:

- the host has no open record, so an open record is retested by the
  probe's own call (on the next run the host is not cooling) rather than
  closed on another caller's success, which may have used a different model
  or come before the fault;
- `last_ok` is under 24 h old;
- it differs from the stored value, meaning some other caller has succeeded
  since;
- it is later than the host's `last_failed`.

`last_failed` is written the same way, on every failed `delegate_inference`
call except the two refusals where gov never tried the host: a host in a
cooldown, and a host the operator has switched off. That includes preflight
and spawn failures, so an orchestrator that breaks in the afternoon is not
hidden by a morning success. Without the last condition, a success in
the morning would hide an afternoon failure whose short cooldown had lapsed
by the next run. `last_ok` is written only by gov's own success path, inside
the call. A timed-out call has already returned its failure, so if the
orchestrator finishes it later, no `last_ok` is written. This works because
`delegated_inference` awaits `clear_async` before it returns its response
(it does today), so `last_ok` is already written when the probe reads it. The
build keeps `last_ok` inside that awaited call, and a test asserts it: if the
write were ever moved to a background task, the stored value could be the
pre-probe one. An idle host is probed every day. A busy host costs
nothing.

### Cost per day

- **Calls:** at most one per enabled host per day, so at most 3 today
  (`claude`, `codex` and `antigravity` host adapters), and fewer when real
  traffic provides passive evidence.
- **Tokens per call:** not yet measured for a probe. The one measured
  host-adapter consult (Codex, 2026-09-26, a real brief) used 28,250 tokens and
  took 12.4 s. A probe prompt is much shorter, but each CLI loads its own
  system prompt and tools, so treat one probe as costing up to one real consult
  until the first dry run measures it.
- **Timing:** once a day at a fixed time the operator chooses (04:15 local is
  proposed), so the probe's draw on each provider's rolling usage window is
  predictable and can be kept out of working hours.
- **Governance side effects:** each probe charges the fixed
  `delegate_inference` energy increment (0.05) to the probe's identity and
  adds one `tool_usage` row. The probe needs its own baselined doctor-layer
  identity, as other doctors do, so its calls stay separate from agent
  traffic and never land on a resident's record.

### How it avoids tripping cooldowns

- It never probes a host in a cooldown, so it cannot extend a window or push
  up the backoff.
- It never retries. One failure is one data point, and a quota failure the
  probe discovers starts a correctly classified cooldown that it did not have
  to guess at.
- Probes run in sequence with a 120 s cap, once a day.
- A probe cannot shorten a window: it runs only when no window is live, and
  `host_availability`'s rules keep a guess from overriding a provider's stated
  reset.

### Controls

- `--dry-run` prints the plan (which hosts it would probe or skip, and why)
  without calling anything.
- `UNITARES_INFERENCE_PROBE_HOSTS` is an optional allowlist; empty means every
  enabled host.
- Unloading the plist stops it entirely. There is no in-process state to
  clean up.

## Decisions for the operator

1. **Approve the spend:** at most 3 probe calls a day, fewer with passive
   evidence.
2. **Time of day:** 04:15 local is proposed.
3. **Quota as a finding:** proposed as no finding, since it recovers on its own
   and failover covers it. The alternative is a low-severity finding when one
   host has cooled for more than N days in a row.
4. **Scope:** all three host adapters, or leave out one whose quota is tight.
5. **Stopping a hung probe call:** the probe stops only its own timed-out
   execution, by id, through the orchestrator's `DELETE`. This is proposed as
   on, since it is the only thing that bounds a hung CLI. It is also the
   probe's one act beyond its own call, so it can instead report only and
   leave the stop to the operator.
6. **Passive evidence (`last_ok`):** build it with the probe as proposed, or
   probe every enabled host every day, which is simpler but always costs about
   3 calls. The trade-off is what counts as success. Passive evidence trusts
   gov's own judgement, a validated terminal envelope with non-empty text,
   because gov cannot tell whether a real caller's answer makes sense. A host
   whose CLI returns an error message as a normal answer would keep writing
   `last_ok` under real traffic, and the probe's stricter `OK` check would
   never run. Probing daily regardless closes that gap at the cost of the
   calls. A middle option is to skip on passive evidence at most N days in a
   row.

## Build plan once approved

1. `last_ok` write-through in `host_availability.clear_async` and
   `last_failed` on every failed call gov actually attempted, both 48 h TTL, plus
   both fields in `list_inference_hosts`. Tests cover the TTL and Redis being
   down.
2. `scripts/ops/inference_host_probe.py` with injectable I/O, as in
   `model_adjudicator.py`. Tests cover each row of both tables above, check
   that a cooling host and a `last_ok` host are never probed, and check that
   only an execution id from the probe's own state file is ever stopped.
3. The launchd plist template and census registration.
4. A `--dry-run` against live gov, then a single supervised real run to
   measure `tokens_used` per host before the schedule is loaded.
