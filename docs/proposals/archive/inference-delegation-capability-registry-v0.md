# Inference Delegation & Capability Registry

**Status:** v0 scoping proposal - design-first, no runtime change in this
document.
**Created:** 2026-06-29.
**Author:** Codex, at operator request.

## Why This Exists

UNITARES already has the core governance loop: process identity, check-ins,
EISV state estimation, calibration, KG, dialectic review, MCP/REST/SDK access,
tool modes, and a local-first `call_model` tool. The missing layer is not
"more intelligence." It is a small coordination surface for discovering
available inference hosts and borrowing model judgment with provenance.

The operator need is practical:

> A governed agent should be able to ask an available local model, hosted model,
> or operator-authorized subscription-backed assistant for bounded advisory
> inference, and UNITARES should record that result as evidence without treating
> the model output as an accountable agent by default.

Today `call_model` routes to Ollama by default and can use Hugging Face
Inference Providers when explicitly configured. That is a good first provider
router, but it does not yet answer these questions:

- Which inference hosts are available on this machine or fleet?
- Which are local, cloud, subscription-backed, or operator-authorized?
- What transport will be used?
- What privacy/cost/accountability class applies?
- Was this a tool answer or an accountable participant's reviewed position?
- How should Codex, Claude, local models, and future host adapters expose the
  same shape without each inventing a separate convention?

This proposal scopes that missing layer.

## Definition

**Inference Delegation & Capability Registry** is a small UNITARES capability
layer with two responsibilities:

1. Maintain a discoverable catalog of inference hosts and their capabilities.
2. Return model outputs as provenance-rich evidence artifacts through a common
   delegation interface.

It does not make model output authoritative. It does not turn every model into
an agent. It does not replace dialectic review, KG promotion, the orchestrator,
or Plexus/surface leases.

Core rule:

```text
A model answer is evidence.
An onboarded model worker is an agent.
A dialectic resolution is a decision process.
A governance verdict is runtime policy.
```

## Current Reality

The current `call_model` surface is useful but provider-centric:

- `provider="ollama"` / `privacy="local"` routes to local Ollama.
- `provider="hf"` / `privacy="cloud"` routes to Hugging Face Inference
  Providers when `HF_TOKEN` or `HUGGINGFACE_TOKEN` is configured.
- `provider="auto"` tries Ollama first, then HF when a token exists.
- Response metadata includes `model_used`, `tokens_used`, `energy_cost`,
  `routed_via`, and `task_type`.

That is enough for local-model delegation. It is not enough for a heterogeneous
fleet where the available inference sources include:

- local Ollama models;
- OpenAI-compatible local or LAN servers;
- hosted APIs;
- host adapters for tools such as Codex, Claude Code, Hermes, Goose, Cursor, or
  future subscription-backed assistants;
- orchestrated model workers that should be accountable agents rather than
  one-off inference tools.

The gap is especially visible for subscription-backed assistants. The right
framing is **operator-authorized host adapters**, not "let other AIs use a
personal subscription." The adapter must be explicit about transport, operator
authorization, privacy/cost class, and accountability.

## Boundary Map

| Layer | Owns | Does not own |
|---|---|---|
| Capability registry | What inference hosts/capabilities are available | Identity, truth, process supervision |
| `call_model` / delegation | Advisory inference request + provenance envelope | Governance verdicts, peer authority |
| Orchestrator | Running/supervising model workers or agents | Deciding truth |
| Governance | Identity, state, calibration, audit, policy action | Hosting every model/runtime |
| Dialectic | Structured disagreement and conditions | Process lifecycle |
| KG | Durable promoted conclusions | Raw answer dumping |
| Plexus / leases | Shared-surface mutation coordination | Semantic review |

## In Scope

- A read-only inference host registry:
  - `list_inference_hosts`
  - `describe_inference_host`
  - optional later `refresh_inference_hosts`
- A provenance envelope for every delegated model result.
- A provider taxonomy that can represent:
  - `ollama`
  - `hf`
  - `openai_compatible`
  - `codex_host_adapter`
  - `claude_host_adapter`
  - future `mistral`, `goose`, `hermes`, or other adapter classes
- Extension of `call_model` to route by host/capability rather than only by
  provider string.
- SDK bindings for registry reads and richer inference results.
- Stub entries for known but unconfigured adapter classes, so clients can
  distinguish "unsupported" from "supported but not configured."
- Optional later `compare_models` for multi-model evidence gathering before
  dialectic or design review.

## Out Of Scope

- Turning UNITARES into a chat app or agent framework.
- UI scraping as the normal transport for subscription-backed assistants.
- Treating Codex/Claude subscription output as an autonomous peer by default.
- Letting `call_model` lower risk, bypass pause/reject, or override a verdict.
- Replacing dialectic review with model voting.
- Writing every raw model answer into KG.
- Making the orchestrator decide semantic truth.
- Adding tenant/multi-user billing semantics.
- Creating a broad marketplace of arbitrary model credentials.

## Provider Taxonomy

Provider identity should separate four questions that are often conflated:

| Field | Question |
|---|---|
| `provider_id` | Which provider or adapter answered? |
| `transport` | How was it called? |
| `privacy_class` | Where did the prompt go? |
| `accountability_class` | Is the answer a tool artifact or an agent position? |

Illustrative host record:

```json
{
  "host_id": "ollama:local",
  "display_name": "Ollama local",
  "provider_kind": "ollama",
  "transport": "openai_compatible_http",
  "configured": true,
  "available": true,
  "privacy_class": "local",
  "cost_class": "local_free",
  "accountability_class": "tool_evidence",
  "capabilities": ["reasoning", "generation", "analysis"],
  "models": ["gemma4:latest"],
  "notes": "Model list is host-observed; absence is not a governance failure."
}
```

Illustrative subscription-backed adapter record:

```json
{
  "host_id": "claude:host-adapter",
  "display_name": "Claude host adapter",
  "provider_kind": "claude_host_adapter",
  "transport": "host_adapter",
  "configured": false,
  "available": false,
  "privacy_class": "operator_authorized_external",
  "cost_class": "subscription_backed",
  "accountability_class": "tool_evidence",
  "capabilities": ["reasoning", "review", "summarize"],
  "models": [],
  "notes": "Configured only by explicit operator adapter setup."
}
```

## Delegation Result Envelope

`call_model` should continue returning the answer in a convenient field, but the
audit-bearing shape should be explicit.

Illustrative result:

```json
{
  "success": true,
  "response": "...",
  "inference": {
    "schema": "unitares.inference_result.v0",
    "host_id": "ollama:local",
    "provider_kind": "ollama",
    "transport": "openai_compatible_http",
    "model_used": "gemma4:latest",
    "task_type": "analysis",
    "privacy_class": "local",
    "cost_class": "local_free",
    "accountability_class": "tool_evidence",
    "requesting_agent_uuid": "optional-uuid",
    "latency_ms": 1234,
    "tokens_used": 550,
    "energy_cost": 0.01,
    "prompt_hash": "sha256:...",
    "response_hash": "sha256:...",
    "finish_reason": "stop",
    "configured_by": "operator",
    "warnings": []
  }
}
```

Compatibility note: existing top-level fields such as `model_used`,
`tokens_used`, `energy_cost`, and `routed_via` can remain during migration. The
nested `inference` object is the durable shape.

## Tool Surface

### `list_inference_hosts`

Read-only. Returns configured and known-unconfigured hosts.

Primary use cases:

- let Codex/Claude/local agents discover what they can ask;
- make absence explicit (`configured=false`, `available=false`);
- avoid callers guessing model/provider names.

### `describe_inference_host`

Read-only. Returns full provider details, capability tags, privacy/cost class,
and recovery hints.

### `call_model`

Existing tool, expanded conservatively:

- preserve current `provider`, `privacy`, `model`, `task_type`, `max_tokens`,
  and `temperature` inputs;
- add optional `host_id`;
- return the provenance envelope;
- fail closed when the requested host is unconfigured or not available;
- never silently route from local to external unless `privacy` permits it.

### `compare_models` (later)

Optional higher-level tool. Runs the same prompt against multiple hosts and
returns a structured comparison artifact. This is useful for dialectic prep and
design review, but it should not be Phase 1.

## Accountability Modes

This proposal depends on keeping two modes distinct.

### Tool Evidence Mode

The model is called through `call_model`. Its answer is an evidence artifact.

Properties:

- no independent UNITARES identity;
- no check-ins by the model;
- answer can be cited in KG/dialectic only as model evidence;
- cannot satisfy "peer reviewed" by itself.

### Agent Participant Mode

The model runs behind a wrapper/worker that calls `start_session`, receives a
UUID, checks in with `sync_state`, and participates in dialectic as an
accountable agent.

Properties:

- independent process identity;
- governed state and calibration;
- can submit thesis/antithesis/synthesis when authorized;
- orchestrator may supervise its lifecycle;
- outputs are agent positions, not just tool artifacts.

The registry should expose which hosts support each mode, but `call_model`
should default to Tool Evidence Mode.

## First Vertical Slice

The first build should intentionally avoid Codex/Claude adapter complexity.

Deliverables:

1. Add a small inference host registry module, file-backed or static-code-backed
   to avoid a migration.
2. Expose `list_inference_hosts` and `describe_inference_host`.
3. Register current Ollama and HF routing in the registry.
4. Add unconfigured placeholder records for Codex and Claude host adapters.
5. Add the nested `inference` provenance envelope to `call_model` responses.
6. Add tests for provider discovery, privacy routing, unavailable host errors,
   and compatibility fields.
7. Update SDK result models for the richer envelope.

Exit criterion:

> An agent can discover available inference hosts, call the existing Ollama/HF
> paths through the old parameters, and receive a stable provenance envelope
> that distinguishes local, cloud, and unconfigured subscription-backed adapter
> classes.

## Second Vertical Slice

Add one real host-adapter provider, behind explicit operator configuration.

Recommended shape:

- `provider_kind="claude_host_adapter"` or `provider_kind="codex_host_adapter"`;
- `transport="host_adapter"`;
- `privacy_class="operator_authorized_external"`;
- `cost_class="subscription_backed"`;
- `accountability_class="tool_evidence"`;
- fail closed if the adapter is absent, unauthenticated, or ambiguous.

Adapter requirements:

- no hidden fallback to a different provider;
- no scraping-only first design;
- no storage of subscription secrets in UNITARES KG or audit payloads;
- response hashes and latency recorded;
- warnings surfaced when the adapter cannot provide model/version identity.

## Later Slices

| Phase | Name | Outcome |
|---|---|---|
| 0 | Scoping RFC | This document |
| 1 | Registry + provenance | Hosts discoverable, `call_model` envelope stable |
| 2 | One host adapter | Codex or Claude adapter wired through explicit config |
| 3 | OpenAI-compatible host class | LAN/local/vLLM/LM Studio/Mistral-compatible hosts share one adapter shape |
| 4 | Comparison evidence | `compare_models` returns multi-host evidence artifacts |
| 5 | Agent participant mode | Orchestrator can spawn model-backed agents that onboard/check in |
| 6 | Dialectic integration | Model evidence can be attached to reviews without becoming authority |

## Relationship To Existing Threads

- **Harness registry:** catalogues harness types. This proposal catalogues
  inference hosts/capabilities. A host adapter may run inside a harness, but
  host capability is not identity.
- **Hosted endpoint:** concerns UNITARES server hosting and tenancy. This
  proposal concerns outbound or local inference delegation by governed agents.
- **Operator decision packet:** packages operator choices. Model comparison may
  become evidence for a packet, but the operator remains authority for
  taste/irreversible calls.
- **Verification-weighted verdict:** adds independent one-sided risk evidence.
  Inference delegation can provide model evidence, but it must not lower risk.
- **Plexus / lease plane:** coordinates shared mutation surfaces. Inference
  hosts are not lease holders unless they become supervised agents touching
  shared surfaces.
- **Agent orchestrator:** can later spawn accountable model workers. Phase 1
  does not require orchestrator changes.

## Safety Invariants

1. **No silent privacy escalation.** A local request must not silently leave the
   machine.
2. **No authority laundering.** A model answer is not peer review unless an
   accountable agent produced it through the governance loop.
3. **No risk lowering.** Inference evidence may inform or escalate; it must not
   reduce governance risk by itself.
4. **No raw-answer KG sludge.** KG stores promoted conclusions, corrections, or
   durable lessons, not every model response.
5. **No credential echo.** Secrets, bearer tokens, and subscription artifacts do
   not appear in audit/KG payloads.
6. **No adapter ambiguity.** A configured host record must identify its
   transport and privacy/cost/accountability class.
7. **No mandatory external dependency.** Local Ollama remains the default
   low-friction path; external providers are explicit opt-ins.

## Open Questions

1. Should the registry be code-backed initially or file-backed like the proposed
   harness catalog?
2. Which adapter should be first: Codex, Claude, or a generic
   OpenAI-compatible host class?
3. What minimum metadata should a host adapter provide if the upstream host does
   not expose a model/version identifier?
4. Should prompt/response hashes be emitted to `audit.events`, returned only in
   the tool response, or both?
5. Should `compare_models` be a first-class tool or a client-side SDK helper
   until enough usage proves it belongs server-side?
6. What operator UX configures subscription-backed adapters without implying
   account-sharing or hidden credential reuse?

## Recommended Next Step

Build Phase 1 only:

> registry + `list_inference_hosts` / `describe_inference_host` + provenance
> envelope on existing Ollama/HF `call_model`.

Do not wire Codex or Claude adapters until the registry and envelope are stable.
That keeps the first PR reviewable and lets the repo prove the boundary before
adding subscription-backed transports.
