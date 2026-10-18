# Consult advisory facade v1

Status: implemented (re-read 2026-09-23, proposals audit). PR #1887
(merged 2026-08-24) added `src/mcp_handlers/support/consultation.py`, and
`consult` is one of the advertised tools. This document remains its contract.

Earlier status (2026-08-24): implementation candidate; superseded by the 2026-09-23 re-read.
Date: 2026-08-24

## Decision

Expose two primary verbs for model-mediated help:

- `consult` returns advisory model evidence and never creates a governance record.

> **Amended 2026-09-26.** Two qualifications. First, "no governance record"
> was already inexact before this amendment: the inference layers' Energy
> accounting runs a synthetic `process_update` on every successful call,
> which writes an `auto_attest` decision row that does not say a
> consultation produced it (the same holds for `call_model` and
> `delegate_inference`). Second, every consultation that reaches the router
> now also writes an `audit.events` row, `event_type='consultation'`
> (`unitares.consultation_record.v1`): requester, policy, outcome, the route
> whenever a well-formed inference result came back, and HMAC hashes of the
> brief, prompt and advice under a key returned only to the caller. A failed
> upstream attempt records no route block, but when the brief was actually
> sent its failure entry (`failure.upstream`, or `failure.primary` /
> `failure.fallback` on a fallback path) carries a `route` naming the
> registry host, provider kind and privacy class it went to
> (under `privacy='cloud_allowed'` a thorough call also records its target
> host; under `privacy='local'` none is contacted and none is named). The
> advice stays advisory and off the governed record; the fact that it was
> sought is on it.
>
> Retention: consultation rows live in `audit.events` partitions, dropped at
> 180 days, while `audit.outcome_events` keeps 365. Nothing references a
> `consultation_id` today; if an outcome, finding or review record starts to,
> consultation rows need a store that lives at least as long as it.
>
> The synthetic Energy-accounting update is scheduled for removal, together
> with a route-level record for `call_model` and `delegate_inference`, after
> the pre-registered 2026-12-01 read, so that read's inputs do not change
> mid-window.
- `request_review` requests governed, on-record judgment with actual reviewer provenance.

`call_model`, `delegate_inference`, and raw `dialectic` remain compatible route or
lifecycle controls. They are not aliases for either primary verb.

## Public contract

```text
consult(
  brief,
  purpose="answer",              # answer | critique | summarize | generate
  effort=<inferred>,             # standard | thorough; omit to follow host, else standard
  privacy="local",               # local | cloud_allowed
  allow_degraded=false,
  response_mode="compact",       # compact | full
  host_id=null,                   # omitted: automatic; supplied: pin existing host
  host_ids=null,                  # 2-3 CLI hosts: a council, each member pinned
)
```

Provider, model, temperature, token limit, and timeout controls remain on the
raw inference tools. Amended 2026-10-13: optional `host_id` accepts the five
existing hosts. `ollama:local` and `hf:router` require standard effort;
`claude:host-adapter`, `codex:host-adapter` and `antigravity:host-adapter` require
thorough effort. Amended 2026-10-14: when effort is omitted, the selected
host supplies it; with no host, it remains standard. Explicit effort must
match the host. Null is outside the advertised effort schema. External hosts
require `cloud_allowed`. A pinned call requires
`allow_degraded=false` and never tries another host. Selection grants no
configuration or identity authority; own-family selection is advisory only.

Amended 2026-10-18 (#2900, corrected in its follow-up): optional `host_ids` asks a council of 2 or 3
distinct thorough hosts in parallel. Each member is a pinned call with the
rules above (no failover, route enforced, own record and hash key), and the
records share a `council_id`. A council needs `cloud_allowed` and
`allow_degraded=false`, cannot be combined with `host_id`, and an omitted
effort becomes thorough. The result, schema
`unitares.consultation_council.v1`, has no top-level `advice`: it lists each
member's result under `members`, with `answered` of `asked`, and status
`completed` or `partial`; none answering fails as `CONSULT_COUNCIL_FAILED`.
Nothing combines, ranks or votes on the answers. Records are written once
the whole council returns. A cancelled council keeps no hashes in any
member's record: a member that answered or was still running is recorded as
cancelled, and one already refused keeps its refusal.

Amended 2026-10-16: the bounded surface above stays bounded. `call_model` is
deprecated in favour of `consult(effort='standard')` (#2847), and its model,
temperature and token-limit controls for HTTP hosts retire with it rather than
moving onto `consult`. Local model choice is operator configuration
(`UNITARES_MODEL_ID`, which the standard lane resolves through
`default_local_model()`), not a per-call knob. Removal is not before 2026-12-02,
so the pre-registered 2026-12-01 read does not see `call_model`'s route change
mid-window; #2484 changes that route on the same date. `delegate_inference`
keeps its model and timeout controls for the CLI hosts.

The lower-overhead effort is named `standard`, not `fast`: neither its HTTP
provider nor the 480-second public tool boundary is a latency SLA. The internal
standard request uses cancellable async I/O under an aggregate 450-second
wall-clock cap, leaving 30 seconds of facade budget for cleanup and response
normalization. It does not launch an executor worker that can outlive the
facade deadline.

`privacy="local"` means the operator-configured local inference service and is
enforced by an exact route/privacy postcondition. It trusts that operator
configuration; deployments requiring literal on-box transport must configure
that service accordingly. `cloud_allowed` grants permission for external
processing but does not promise that external processing will occur.

## Authority and output

Every result carries one atomic authority object:

```json
{
  "authority": {
    "class": "tool_evidence",
    "advisory": true,
    "on_record": false,
    "can_satisfy_peer_review": false,
    "governed_review_tool": "request_review"
  }
}
```

The authority fields stay nested so clients cannot accidentally combine a
top-level class from one envelope with an `on_record` flag from another. This
is the v1 decision; duplicate top-level `authority_class` and `on_record`
fields are intentionally not part of the contract.

Successful internal inference must use schema
`unitares.inference_result.v0` and accountability class `tool_evidence`.
Anything else fails closed without returning the advisory text. The facade
recomputes the response hash from the text it actually returns.

Compact mode returns the consultation id, status, advice, authority, requested
policy (including `host_id` when selected), delivered effort, whether external processing occurred, degradation
when applicable, completion state, and any external-cost warning. Full mode
adds one `diagnostics` object with route, host, provider/model metadata,
transport, hashes, usage, latency, requester identity, and orchestrator id.
Alongside `tokens_used`, full diagnostics add `tokens_used_note`: the backend
aggregate can include input/cache tokens and is not answer length or a
normalized billing measure. Zero can mean missing usage. See the intent map
for the per-adapter counting rules. No counters or accounting change. Full
failure diagnostics retain the backend message (at most 500 characters),
alongside the existing unvalidated output excerpt. Both are caller-only and
excluded from compact responses and the durable audit.

## Routing and degradation

The table describes calls without `host_id` or `host_ids`. Explicit selection bypasses
automatic family selection and peer failover, with the constraints above.

| Effort | Privacy | `allow_degraded` | Resolution |
|---|---|---:|---|
| standard | local | either | Configured local inference service |
| standard | cloud_allowed | either | Local-first standard lane; approved cloud fallback is permitted |
| thorough | local | false | Fail: policy cannot be satisfied |
| thorough | local | true | Explicitly degrade to standard local |
| thorough | cloud_allowed | false | First available strong-model peer excluding the inferred caller family |
| thorough | cloud_allowed | true | Automatic strong-model peer; standard local fallback after proven unavailability with nothing possibly running |

Automatic choice is derived from transport-captured harness provenance.
The harness hint is descriptive, not an authority signal. Available hosts in
`_THOROUGH_PEERS` are tried in order, excluding the inferred caller family;
unknown callers try Claude first. Automatic calls may try another peer after
proven provider unavailability, within the remaining call budget. Explicit
`host_id` instead pins the named host, with no family exclusion or failover.
The resolved route is enforced again before advice is returned, so backend
routing drift fails closed. Both requested selection and actual route are
retained in the text-free consultation audit.

Fallback is forbidden when execution may still be running. A terminal quota
or authentication refusal can be eligible for automatic failover even if a
process started; malformed output is not proof of provider unavailability.
Lost spawn acknowledgements, missing agent ids, await timeouts and ambiguous
adapter exceptions are reported as possibly running. Cancellation propagates;
the orchestrator's child max-runtime remains the orphan backstop.

## Implementation boundary

The facade calls typed, transport-neutral `run_model_inference` and
`run_delegated_inference` services. It does not recursively invoke MCP handlers
or parse public response envelopes. Raw handlers adapt those same outcomes back
to their existing wire contracts.

Strict public validation accepts only the named host enum and existing inputs,
and rejects raw controls and other extras. Dispatch-owned
identity and normalization metadata are stripped at ingress and reconstructed
from trusted middleware context, so `extra="forbid"` does not reject genuine
identity proof or accept caller-forged private fields.

## Discovery and rollout

Discovery teaches `consult` first for answers, critique, summaries, and
generation, and `request_review` for verdicts or governed recovery. The latter's
described schema is its narrow one-call wire schema, not the full `dialectic`
router. Raw tools remain visible in the common tier during evaluation so this
slice does not hide working controls before selection data is collected.

Promotion or further simplification requires:

1. No raw-tool schema or response compatibility regressions.
2. Compact responses include requested host selection but omit actual route and
   identity diagnostics; full responses retain them.
3. No fallback for pinned hosts or ambiguous delegated failures; automatic
   failover requires proven unavailability and nothing possibly running.
4. Tool-choice probes select `consult` for advisory intents and
   `request_review` for governed verdict/recovery intents.
5. Review-summoning telemetry shows that `request_review` either assigns,
   summons, or clearly reports an open reviewer slot; that lifecycle is not
   silently conflated with inference availability.

No runtime capability is removed by v1.
