# Consult advisory facade v1

Status: implementation candidate

> **Status re-read 2026-09-23 (proposals audit):** implemented. PR #1887
> (merged 2026-08-24) added `src/mcp_handlers/support/consultation.py`, and
> `consult` is one of the advertised tools. This document remains its contract.
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
> whenever an inference result came back (an upstream failure records the
> lane, requested privacy and failure code but not where the attempt went),
> and HMAC hashes of the brief, prompt and advice under a key returned only
> to the caller. The advice stays advisory and off the governed record; the
> fact that it was sought is on it.
- `request_review` requests governed, on-record judgment with actual reviewer provenance.

`call_model`, `delegate_inference`, and raw `dialectic` remain compatible route or
lifecycle controls. They are not aliases for either primary verb.

## Public contract

```text
consult(
  brief,
  purpose="answer",              # answer | critique | summarize | generate
  effort="standard",             # standard | thorough
  privacy="local",               # local | cloud_allowed
  allow_degraded=false,
  response_mode="compact",       # compact | full
)
```

Provider, host, model, temperature, token limit, and timeout are deliberately
absent. Callers that require those controls use the raw inference tools.

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
policy, delivered effort, whether external processing occurred, degradation
when applicable, completion state, and any external-cost warning. Full mode
adds one `diagnostics` object with route, host, provider/model metadata,
transport, hashes, usage, latency, requester identity, and orchestrator id.

## Routing and degradation

| Effort | Privacy | `allow_degraded` | Resolution |
|---|---|---:|---|
| standard | local | either | Configured local inference service |
| standard | cloud_allowed | either | Local-first standard lane; approved cloud fallback is permitted |
| thorough | local | false | Fail: policy cannot be satisfied |
| thorough | local | true | Explicitly degrade to standard local |
| thorough | cloud_allowed | false | Reciprocal strong-model host: Claude-family caller → Codex; all other/unknown callers → Claude |
| thorough | cloud_allowed | true | Reciprocal strong-model host; standard local fallback only after proven pre-execution unavailability |

The reciprocal choice is derived from transport-captured harness provenance,
not from public `consult` arguments: callers cannot name a host through this
facade. The harness hint is descriptive rather than an authority signal. It
selects between two operator-enabled routes with the same privacy, cost, and
accountability class; an unavailable or unknown hint defaults to Claude. The
resolved host is enforced again as a postcondition before any advice is
returned, so backend routing drift fails closed.

Fallback is forbidden after execution starts or may have started. In
particular, a lost spawn acknowledgement, a successful spawn response without
an agent id, an await timeout, and an ambiguous adapter exception are reported
as possibly running. Only registry/config preflight failures and explicit spawn
HTTP rejection are fallback-eligible. Cancellation propagates; the
orchestrator's child max-runtime remains the orphan backstop.

## Implementation boundary

The facade calls typed, transport-neutral `run_model_inference` and
`run_delegated_inference` services. It does not recursively invoke MCP handlers
or parse public response envelopes. Raw handlers adapt those same outcomes back
to their existing wire contracts.

Strict public validation rejects route controls and other extras. Dispatch-owned
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
2. Compact responses omit route and identity diagnostics; full responses retain them.
3. No fallback after ambiguous or post-spawn delegated failures.
4. Tool-choice probes select `consult` for advisory intents and
   `request_review` for governed verdict/recovery intents.
5. Review-summoning telemetry shows that `request_review` either assigns,
   summons, or clearly reports an open reviewer slot; that lifecycle is not
   silently conflated with inference availability.

No runtime capability is removed by v1.
