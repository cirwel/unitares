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

For each host in `list_inference_hosts`, the script takes the first matching
row below:

| Host state | Action | Quota spent |
|---|---|---|
| Not enabled (flag off, CLI missing, or in `UNITARES_HOST_ADAPTER_DISABLED_HOSTS`) | Log `skipped: not_enabled`. Operator choice, not a fault. | 0 |
| In a cooldown (`cooldown` field set; `list_inference_hosts` fills it from `host_availability.cooldown()`, which returns nothing once `retry_after` has passed, so a lapsed window never shows) | Log `skipped: cooling until <retry_after>`. No finding: the failure is already known and consult already routes around it. | 0 |
| A previous probe's timed-out execution is still live (see *Hung calls*) | No probe; raise the host's finding to **high**. | 0 |
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
| Success | none; log latency, tokens, model | |
| Failure classified `quota` | none | `delegate_inference` records the cooldown itself, until the provider's stated reset or, when none is stated, on a backoff from 30 min doubling to 6 h. The limit resets, and failover covers it meanwhile. |
| Failure classified `auth` | **high** | A logged-out CLI does not recover on its own; the operator has to log in. |
| Unclassified failure (malformed envelope, nonzero exit, spawn rejected, orchestrator down) | **medium** | The case nothing else records. |
| Timeout (`possibly_running`) | **medium**, noted as possibly still running | See *Hung calls* below. |

Findings go through `agents/common/findings.post_finding`, the same
fingerprinted and deduplicated path Sentinel, Watcher and the doctors use.
They use event type `inference_host_finding` and fingerprint
`sha(host_id, failure_class)`, and take `doctor_findings.py`'s doubling
re-alert backoff so a host that stays broken does not re-post daily.
Recovery uses `doctor_findings.py`'s rule: at the end of each run, an open
record that this run did not reproduce is closed and its backoff dropped, so
a later failure of the same kind alerts at once. That one rule covers a
success, a changed failure (a host that failed preflight yesterday and fails
`auth` today closes the preflight record and opens an `auth` one), and a
shared `gov-dispatch` failure that is no longer shared (its record closes, and
any host still failing gets its own). The one exception is a host the run
could not assess, because it was cooling or had a live hung execution. Its
records stay as they are, since not looking is not evidence of recovery. A
host the operator has switched off has its records closed with the reason
`not_enabled`: the operator has taken it out of rotation.
Recovery is noticed on the next daily run, so a record can stay open up to a
day after a host recovers. That delay costs nothing more: the backoff already
holds back re-posting, and the finding was posted once. That closure is local state
only. It emits no `outcome_event`, because a probe passing is not an operator
judging the finding correct (roadmap Invariant 4, the reasoning
`doctor_findings.py` records at the same step).

### Hung calls

A call that times out may still be running under the orchestrator, and a CLI
stuck on a prompt would otherwise leave one more child each day. The probe
stores the `orchestrator_execution_id` of every timed-out call in its state
file. On the next run it polls the orchestrator's
`/v1/executions/<id>/await` with a short wait, the same poll the host
adapter's timeout hint names, to see whether that execution is still live. If it is, the probe does not start another call to that host. Instead
it raises the host's finding to **high** with the age of the stuck execution,
and leaves termination to the operator. The probe's only effect is its own one
call per host. Killing an orchestrator child could end work the probe cannot
see, such as another caller's execution on the same host, so it is not the
probe's to do. So each host has at most one probe child alive
at a time, however long the hang lasts. By
existing routing, a finding goes to `#residents`, and a high one also goes to
`#alerts`.

Each run also appends one JSON line per host to
`data/logs/inference-host-probe.log`, with the host's state, action, latency,
`tokens_used` and failure class. That line is the record of what the probe
cost.

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
skips a host only when `last_ok` is under 24 h old **and** differs from the
stored value, meaning some other caller has succeeded since. Comparing values
rather than clocks does not depend on the order in which gov and the script
record their times. An idle host is probed every day. A busy host costs
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
5. **Passive evidence (`last_ok`):** build it with the probe as proposed, or
   probe every enabled host every day, which is simpler but always costs about
   3 calls.

## Build plan once approved

1. `last_ok` write-through in `host_availability.clear_async`, plus the field
   in `list_inference_hosts`. Tests cover the 48 h TTL and Redis being down.
2. `scripts/ops/inference_host_probe.py` with injectable I/O, as in
   `model_adjudicator.py`. Tests cover each row of both tables above, and
   check that a cooling host and a `last_ok` host are never probed.
3. The launchd plist template and census registration.
4. A `--dry-run` against live gov, then a single supervised real run to
   measure `tokens_used` per host before the schedule is loaded.
