# Harness Event Safety Policy — v0

- **Created:** 2026-06-20
- **Last Updated:** 2026-06-20
- **Status:** Draft for multi-model review
- **Scope:** Cross-harness policy contract; no runtime enforcement changes in this PR
- **Companions:** [`docs/ontology/harness-substrate-plurality.md`](../ontology/harness-substrate-plurality.md), [`discord-thread-identity-resume-v0.md`](discord-thread-identity-resume-v0.md), [`behavioral-running-hot-detector-v0.md`](behavioral-running-hot-detector-v0.md), [`docs/dev/CIRCUIT_BREAKER_DIALECTIC.md`](../dev/CIRCUIT_BREAKER_DIALECTIC.md)

---

## 0. Operator decision being tested

UNITARES should carry a **global, harness-neutral event-safety policy** before
individual adapters patch their own failures. The immediate trigger was a Hermes
Discord gateway restart / auto-resume loop, but the policy must generalize across
Hermes gateway, CLI/TUI, cron, webhooks, Kanban workers, delegated subagents,
Claude/Codex/OpenCode adapters, BEAM residents, and Lumen/Anima body loops.

This document is intentionally policy-first. It should be reviewed by multiple
models/harnesses before implementation PRs start landing.

## 1. Normative core

A model turn or side-effectful tool path may begin only from a **valid event
envelope** whose safety-relevant fields have been derived or verified at the
correct trust boundary.

Invalid, ambiguous, replayed, duplicated, or untrusted synthetic control events
may be observed and recorded as diagnostics, but they must not trigger
unconstrained model execution, durable governance writes, or external-world
effects.

Short form:

    invalid event                 -> no model turn
    untrusted synthetic control   -> no model turn by default
    duplicate/replayed event      -> dedupe or block before model/tool execution
    ambiguous resume              -> pause, quarantine, or require operator intent
    side effect                   -> require fresh valid intent + verification
    authorized automation         -> require explicit automation policy + budget

### Decision precedence

When more than one row applies, the stricter decision wins:

1. `reject` / `block` / `reject_or_require_review` / `reject_identity_required` /
   `reject_or_require_lineage`
2. `quarantine_session` / `pause_harness` / `pause_or_defer`
3. `dedupe`
4. `warn`
5. `allow_read_only`
6. `allow`

The compound decisions in the Section 7 table are normalized into this ladder at
the tier shown above: every `reject_*` variant ranks at tier 1 and carries its
nuance in the `reason_code` of the Section 13 result, and `pause_or_defer` ranks
at tier 2. There are no decision values outside this ladder; an evaluator that
cannot map a row's decision into one of these six tiers must treat it as tier 1
(`reject`).

An `allow` decision must never override a contradictory invalidity, replay,
identity, or effect-authority failure.

### Unknown values fail closed

An unrecognized value in any safety-relevant field (`event_origin`,
`requested_effect`, `max_effect_class`, `proof_origin`,
`identity_assurance.tier`, or any `classification.*` flag an evaluator does not
understand) is treated as invalid, not ignored: it falls to
`reject_or_require_review` for model turns and side effects rather than falling
through to `allow`. Producers must preserve unknown envelope fields rather than
strip them, so a newer evaluator can still see them. A `v0` evaluator must not
silently allow a `v1` field it cannot interpret on a safety-relevant path.

## 2. Normative versus example content

Sections 1–10 define the policy contract. Sections 11–14 are non-normative
examples and review scaffolding. They show how the contract maps to Hermes,
BEAM, Lumen, cron, webhooks, and other harnesses, but no example is required to
be implemented by this PR.

A later upstream Hermes PR should be a narrow gateway/session bugfix, not a
UNITARES policy import.

## 3. Measurement, diagnosis, policy, enforcement

This policy follows the UNITARES separation between measurement, diagnosis,
policy, and enforcement.

| Layer | Owns | Must not pretend to own |
|---|---|---|
| Measurement | Event facts, EISV, restart counts, duplicate keys, identity assurance, provenance | The final pause/quarantine decision |
| Diagnosis | Labels such as `restart_storm`, `poisoned_resume`, `duplicate_delivery`, `weak_identity_write` | The actuator itself |
| Policy | Rule evaluation: allow, warn, dedupe, pause, quarantine, reject, require review | Raw signal collection |
| Enforcement | Circuit breaker, session quarantine, lease release, blocked write, operator escalation | The underlying measurement truth |

For **new harness-event guards**, EISV and other telemetry may inform the policy,
but the pause authority should live in the policy/enforcement layer. In target
architecture, a thermometer does not pause the agent by itself; a governed
circuit breaker can.

**Known deployed exception.** This is target architecture, not a description of
the whole current runtime. The deployed governance monitor already emits
`void_pause`, `coherence_pause`, `basin_pause`, and `cirs_block` directly from
EISV / coherence / behavioral z-scores on the check-in path (see
`src/governance_monitor.py` and [`docs/dev/CIRCUIT_BREAKER_DIALECTIC.md`](../dev/CIRCUIT_BREAKER_DIALECTIC.md)).
Those existing EISV-driven pauses pre-date this policy and are *grandfathered*:
they are not yet routed through the layer separation or the Section 10 bounds.
This policy governs **harness-event-driven** pause/quarantine decisions; folding
the legacy EISV pauses into the same layer discipline is later work, not a claim
this PR makes about today's monitor.

## 4. Trust boundary and derived fields

Envelope fields received from a platform message, child agent, webhook body,
model output, or weak adapter are **untrusted claims** until verified or derived
by a trusted substrate.

The policy evaluator must derive or re-verify safety-relevant fields from one of
these sources:

- platform signatures, delivery IDs, message IDs, author IDs, and timestamps;
- harness-local session state, retry counters, restart counters, and leases;
- durable idempotency/dedupe state;
- UNITARES identity verification and continuity-token validation; future
  verified orchestrator vouching only after that Wave-3 path is wired;
- trusted dispatcher/tool registry effect classifications;
- server-stamped timestamps and audit state.

The following fields must not be trusted merely because an adapter supplied
them: `synthetic_control_event`, `diagnostic_probe`, `auto_resume`, `replay`,
`duplicate`, `identity_assurance`, `proof_origin`, `source_trust`,
`requested_effect`, `max_effect_class`, and the `attempts.*` counters
(`resume_attempt`, `delivery_attempt`, `restart_count_window`).

Caller-supplied lower-risk labels never reduce the decision. Missing,
contradictory, or unverified safety-relevant fields make the event invalid or
ambiguous for model turns and side effects.

## 5. Event envelope v0

Every harness that wants UNITARES-aware governance should be able to describe an
incoming event with this envelope or a lossless equivalent. Some fields are
optional before routing; requiredness is defined by effect class in Section 6.

    {
      "schema": "unitares.harness_event.v0",
      "harness_type": "hermes_gateway",
      "harness_id": "profile-or-runtime-instance",
      "process_instance_id": "optional: this concrete process/restart incarnation",
      "logical_principal_id": "optional: stable principal across process restarts",
      "transport": "discord",
      "event_origin": "platform_user_message",
      "received_at": "server-stamped RFC3339 timestamp",
      "emitted_at": "optional platform/adaptor timestamp",
      "ids": {
        "event_id": "platform-or-harness-event-id",
        "event_id_source": "platform | harness_generated | content_hash | none",
        "invocation_id": "concrete run/hook/process id",
        "conversation_id": "discord-thread-or-cli-session-or-cron-job",
        "parent_event_id": "for tool/subagent/child effects",
        "causal_chain_id": "optional correlation id"
      },
      "actor": {
        "kind": "human | agent | scheduler | system | unknown",
        "id": "stable-actor-id-if-known"
      },
      "idempotency": {
        "dedupe_key": "stable idempotency key",
        "dedupe_key_source": "platform | constructed | content_hash | none",
        "dedupe_scope": "conversation | webhook_provider | card | lease | harness_instance",
        "dedupe_ttl_seconds": 86400,
        "payload_hash": "optional canonical hash",
        "prior_state_version": "optional: monotonic state token (e.g. board/card revision, lease epoch)"
      },
      "ingress_verification": {
        "auth_status": "none | present | verified | failed",
        "signature_status": "not_applicable | verified | failed | missing",
        "operator_token_verified": false,
        "orchestrator_vouch_verified": false,
        "content_status": "non_empty | blank | structured_only | redacted | missing",
        "source_trust": "untrusted | trusted_adapter | server_observed"
      },
      "identity": {
        "agent_uuid": null,
        "client_session_id": null,
        "session_resolution_source": null,
        "identity_assurance": {
          "tier": "weak | medium | strong",
          "caller_proven": false,
          "proof_origin": "caller_asserted | server_inferred | unknown"
        }
      },
      "authority": {
        "requested_effect": "read_only | sensitive_read | model_turn | tool_call | file_write | shell | network | publish | governance_write",
        "max_effect_class": "computed upper bound for this event",
        "automation_policy_id": "required for authorized scheduled/synthetic automation"
      },
      "classification": {
        "synthetic_control_event": false,
        "authorized_scheduled_event": false,
        "replay": false,
        "duplicate": false,
        "diagnostic_probe": false,
        "auto_resume": false
      },
      "attempts": {
        "resume_attempt": 0,
        "delivery_attempt": 1,
        "restart_count_window": 0
      },
      "harness_local_phase": "optional opaque-to-core phase token (e.g. lumen body/drawing phase)",
      "adapter_context": {}
    }

### Field semantics

- `event_origin` is the semantic source of the event, not merely the transport.
  Examples: `platform_user_message`, `auto_resume`, `cron_tick`,
  `webhook_delivery`, `child_agent_result`, `operator_command`,
  `diagnostic_probe`, `resident_heartbeat`.
- `event_id` may be absent for CLI/TUI hooks, local cron ticks, BEAM internal
  events, or Lumen body loops. Absence must be explicit via
  `event_id_source=none`; side effects then require a deterministic dedupe key
  or an explicitly reviewed harness policy.
- `dedupe_ttl_seconds` is only the duplicate-suppression window for a seen
  idempotency key. It is not a replay-freshness bound; replay protection is
  handled separately in Section 8 through timestamp/signature/causal freshness.
- `identity.identity_assurance` is server-computed. Adapters may report what
  they saw, but they do not grant themselves `strong` authority. The tier and
  proof-origin enums above match the live UNITARES identity write path:
  `tier ∈ {weak, medium, strong}` (no `none` — an unresolved caller resolves to
  `weak`, and "no identity" is `agent_uuid: null`), and
  `proof_origin ∈ {caller_asserted, server_inferred, unknown}`. `continuity_token`
  is **not** a `proof_origin`; it is a `session_resolution_source` value that
  resolves to `proof_origin=caller_asserted`. `orchestrator_vouched` is a
  defined-but-inert future proof origin (Wave-3, not yet emitted by the live
  server); adapters must not produce it until that path is wired.
- `authority.requested_effect` is the caller/harness request.
  `authority.max_effect_class` is computed by the trusted dispatcher/tool
  registry and rechecked before each escalation.
- `classification.duplicate`, `classification.replay`,
  `classification.synthetic_control_event`, `classification.diagnostic_probe`,
  and the `attempts.*` counters (`resume_attempt`, `restart_count_window`) are
  **measurement outputs computed by the evaluator/trusted substrate, not trusted
  adapter inputs.** The adapter provides raw idempotency material and may report
  what it observed, but the evaluator computes the authoritative values. In
  particular, `event_origin=auto_resume` forces `synthetic_control_event=true`
  regardless of what the adapter claimed — an adapter cannot launder a resume
  into a user turn by setting the flag to `false`. Where the trusted source for
  a counter does not yet exist (see `restart_count_window` in Sections 7 and 10),
  the decisions that read it are non-normative until that source is named.
- `process_instance_id` versus `logical_principal_id` keep a process restart
  distinct from the principal whose work survives it: a supervisor restart mints
  a new `process_instance_id` under the same `logical_principal_id`, so restart
  storms do not read as identity churn. `harness_id` remains a convenience label;
  these two are the load-bearing distinction for BEAM/supervisor harnesses.
- `prior_state_version` is a monotonic state token (board/card revision, lease
  epoch). It lets a re-dispatch after a real state change be distinguished from a
  duplicate: the same `dedupe_key` with an *advanced* `prior_state_version` is a
  legitimate new event, not a replay.
- `harness_local_phase` is an opaque-to-core token a harness sets for its own
  protected phases (e.g. a Lumen body/drawing phase). The normative core does not
  interpret its value; harness-local policy decides defer/reject. It exists at a
  known path so harness-local rules can reference it without overloading
  `adapter_context`.

## 6. Minimum required fields by effect class

| Effect class | Minimum envelope evidence |
|---|---|
| `read_only` | `harness_type`, `event_origin`, `received_at`, source trust, and explicit allowlist for the read surface |
| `sensitive_read` | `read_only` fields plus verified auth/operator scope and redaction/privacy handling |
| `model_turn` | `read_only` fields plus fresh intent evidence, non-replayed/deduped idempotency, actor/conversation scope, and valid content or structured interaction |
| `tool_call` | `model_turn` parent event plus `parent_event_id`, computed effect class, and tool-specific authority gate |
| `file_write` / `shell` / `network` / `publish` | fresh non-synthetic/non-replayed parent event, computed effect class, explicit authorization, and post-action verification requirement |
| `governance_write` | fresh non-replayed parent event with `parent_event_id` causal linkage, plus UNITARES identity assurance required by the target operation. Identity assurance answers *who*; the fresh-parent requirement answers *was this a current intent* — both are required, so a stale-but-valid event cannot be replayed into a durable write. Note: the live strict gate refuses `proof_origin=server_inferred` writes under strict mode; it does not, by itself, refuse a `weak`-tier `caller_asserted` write — operations needing strong assurance must assert that requirement explicitly (the `reject_identity_required` row in Section 7) |
| authorized cron/orchestrator automation | explicit `automation_policy_id`, deterministic tick/run id, retry budget, and maximum effect class for that automation |

A valid inbound user event can authorize a model turn without authorizing later
shell, network, file-write, publish, or governance-write effects. Every stronger
effect requires its own policy check with causal linkage to the original event.

## 7. Default decision table

| Condition | Default decision | Notes |
|---|---|---|
| Missing event envelope | `reject` for `model_turn` and side effects; `warn` only for allowlisted read-only diagnostics | Legacy mode cannot call models or tools |
| Safety-relevant field is unverified or contradictory | `reject_or_require_review` | Caller claims cannot lower risk |
| `synthetic_control_event=true` and no explicit automation policy | `block` | Auto-resume/control events are not user intent |
| `authorized_scheduled_event=true` without `automation_policy_id` | `block` | Scheduled automation must be named and budgeted |
| `diagnostic_probe=true` (evaluator-derived, not claimed) | `allow_read_only`, exclude from live validation | A self-asserted `diagnostic_probe` flag must be ignored. The probe admission must be derived from a verified probe credential or the sandboxed diagnostic policy of Section 9; otherwise an attacker claims probe to bypass the `sensitive_read` auth ladder or to launder risky traffic out of EISV/calibration |
| Duplicate or replayed event requests `model_turn` or stronger effect | `dedupe` or `block` | Do not call model/tool again |
| Duplicate prior result requested from a different authz/actor/conversation scope | `block` | Avoid leaking prior result across users/tenants |
| `auto_resume=true` and `resume_attempt > 1` | `quarantine_session` | Avoid restart/resume loops. The quarantine *target* (`conversation_id`/`client_session_id`) must come from the enforcement layer's own connection→session map, never from the envelope, or a forged event quarantines a victim session (see Section 10) |
| `restart_count_window >= policy.thresholds.restart_storm` | `pause_harness` | **Non-normative until a trusted source is named.** No `restart_count_window` substrate exists yet; this counter must be derived from the supervisor's own restart-intensity (Section 10), not the adapter-supplied field, before this row can be enforced |
| Webhook/event lacks idempotency key and requests `model_turn` or side effect | `reject` | Retry semantics require dedupe |
| `identity_assurance.tier != strong` and target operation requires strong governance write | `reject_identity_required` | No silent weak durable writes |
| `proof_origin=server_inferred` used as mint/resume proof | `reject_or_require_lineage` | Do not launder server-inferred context into caller proof |
| Unrecognized value in a safety-relevant field | `reject_or_require_review` | Unknown enums fail closed (Section 1), never fall through to `allow` |
| Valid event, all safety-relevant fields verified, no row above applies | `allow` | The terminal/default case: a clean valid event is authorized only up to its Section 6 effect-class evidence, not unconditionally |

Lumen/Anima body-loop protection (interrupting a protected drawing phase) is
**harness-local enforcement policy, not part of this normative table** — it keys
on `harness_local_phase`, which the envelope carries but the normative core does
not interpret. See the non-normative example in Section 11.

The default table is conservative. Deployments may relax read-only
observability, but should not relax synthetic/replayed side effects without an
explicit policy record.

## 8. Idempotency and dedupe semantics

`dedupe_key` is not enough by itself. Dedupe must include:

- `dedupe_scope`: the boundary in which the key is unique. The enum in Section 5
  is a base set; adapters may extend it with harness-local scopes, but the scope
  defines the namespace in which `dedupe_key` must be unique. A `conversation`-
  scoped dedupe is **not** satisfied by a `harness_instance`-scoped prior record;
  a narrower scope must never be treated as covered by a broader one.
- `dedupe_ttl_seconds`: how long a duplicate is recognized;
- actor/conversation/authorization context;
- canonical payload hash when the platform event id is absent or reused;
- in-flight versus completed versus failed prior event state.

**Dedupe is not replay protection.** A `dedupe_ttl_seconds` window (e.g. the
example `86400`) only suppresses *duplicates seen within the window*. A
replay of an old-but-otherwise-valid event delayed past the TTL is a distinct
threat and must be caught by freshness — server-stamped `received_at` versus
`emitted_at` skew bounds, platform signature/nonce validation, and a
`parent_event_id` causal-freshness check for side effects — independent of and
not bounded by the dedupe TTL. Treat "duplicate within TTL" (dedupe-key match)
and "replay of a stale valid event" (freshness failure) as separate decisions.

**Payload-hash canonicalization.** Because `payload_hash` is the fallback dedupe
key when the platform event id is absent or reused, two adapters that hash
different things will silently fail to deduplicate against each other. A
conformant `payload_hash` is therefore defined as: SHA-256 over a UTF-8,
key-sorted, minimal-whitespace JSON serialization of the canonical payload —
the actor/content/conversation-identifying fields (`actor.id`,
`conversation_id`, message/body content) — **excluding** all server-stamped
timestamps (`received_at`, `emitted_at`), `delivery_attempt`, and the
`classification.*`/`attempts.*` measurement fields. An adapter that cannot
produce this hash must set `dedupe_key_source=none` / `event_id_source=none` and
follow the no-idempotency path (Section 7) rather than emitting a hash over an
ad-hoc field set.

Duplicate delivery should produce exactly one model/tool/governance execution.
Returning a prior result is permitted only when the prior result belongs to the
same authorization context. Otherwise the safe behavior is no-op/acknowledge or
block.

The dedupe check must happen atomically before model/tool execution. A concurrent
webhook retry or gateway duplicate must not win a race that starts a second
model turn.

## 9. Diagnostic probes and validation handling

Diagnostic probes may perform only allowlisted read-only observations and
rate-limited append-only diagnostic/audit logging.

By default they must not:

- mint or resume identity;
- count as live agent behavior;
- create outcome evidence or calibration rows;
- enqueue work;
- restart services;
- quarantine or pause harnesses from untrusted probe claims alone;
- publish messages into the same failing channel;
- call models/tools except through an explicitly sandboxed read-only diagnostic
  policy.

Diagnostic probe observations should be labeled so dogfood/ablation reports can
exclude probe-induced noise from live validation claims.

The probe classification itself must be **evaluator-derived, never caller-
asserted**. A self-set `diagnostic_probe=true` is exactly the two attacks this
section must prevent: (1) a read-escalation path — claiming probe to reach
`allow_read_only` while sidestepping the `sensitive_read` auth ladder; and
(2) measurement laundering — a real agent self-labeling probe to keep its
behavior out of EISV/calibration/outcome rows. Admit the probe path only when
the connection carries a verified probe credential or originates from the
sandboxed diagnostic policy above; the exclusion-from-live-validation effect in
particular must be gated on a trusted probe origin.

## 10. Quarantine, pause, and denial-of-service bounds

Pause/quarantine decisions may be triggered only by trusted substrate telemetry
or verified policy state, not by untrusted event fields alone. Each
pause-triggering counter must name its trusted source: restart storms from the
supervisor's own restart-intensity (e.g. OTP supervisor stats), duplicate rate
from the dedupe store's own write-conflict count — never from the envelope's
`attempts.*` fields. A counter with no named trusted source (today,
`restart_count_window`) cannot drive enforcement and its Section 7 row stays
non-normative until the source exists.

Fail-closed must not become a self-denial-of-service primitive. Because
`conversation_id` and `client_session_id` are adapter-supplied claims (Section
4), a quarantine/pause that scopes itself from envelope fields lets a forged
event quarantine any victim session it can name. So the **target identity** of
any session/conversation-scoped enforcement must be resolved by the enforcement
layer's own connection→session state, not read from the event. Distinguish two
decisions explicitly: `reject_this_event` (always safe on forged input) versus
`quarantine_the_session`/`pause_harness` (requires a substrate-attested target).

Enforcement must be:

- minimally scoped: event, session, conversation, harness instance, or specific
  resident child before global fleet;
- TTL-bounded or operator-visible when indefinite;
- rate-limited in alerting;
- auditable with measurement, diagnosis, policy, and enforcement separated;
- resilient to invalid-event floods becoming a global pause primitive — which
  holds only if the pause-triggering counters are substrate-derived per the
  paragraph above, not adapter-populated.

## 11. Non-normative harness examples

| Harness/locus | Loop risk | Required adapter evidence | Default guard |
|---|---|---|---|
| Hermes Discord/Telegram/Slack gateway | Blank inbound, restart auto-resume, permission churn | platform event/interaction id, channel/thread id, author id, fresh user intent, resume attempt | Reject blank synthetic events; quarantine after resume budget |
| Hermes CLI/TUI | Accidental `--continue`, stale compressed context, same-session confusion | local session id, explicit resume command, context lineage | Show/record resume boundary; no silent post-crash continuation for side effects |
| Hermes cron | Recursive scheduling, retry storms, noisy watchdogs | job id, tick id, run attempt, no-agent vs agentic mode, automation policy id | No recursive cron creation; empty-output silence; bounded retries |
| Webhooks/API gateway | Duplicate deliveries, provider retries, spoofed event ids | delivery id, signature status, idempotency key, source | Dedupe before model/tool call; reject side effects without idempotency |
| Kanban workers | Re-dispatch same card, stale lease, board self-churn | card id, lease id, attempt count, board revision | Lease/attempt budget; no re-dispatch without state change |
| Delegated subagents | Parent cancellation, unverifiable side effects, child result replay | parent session id, delegation id, child status, verifiable handles | Treat child summaries as claims; verify handles before declaring success |
| Claude/Codex/OpenCode adapters | Hook spam, ACP/sidecar mismatch, leaked session anchors | hook phase, headless/interactive discriminator, session proof source | Fail closed on weak/leaked anchors; one lifecycle check-in per turn |
| BEAM residents/supervisors | Supervisor restart storm, per-turn identity minting | supervisor child id, restart intensity, logical conversation anchor | Supervisor cooldown; distinguish process restart from principal identity |
| Lumen/Anima | Interrupting live drawings/body loops, over-dashboarding creature state | body phase, display/drawing state, visitor/request origin, disruption level | Defer/reject disruptive actions during protected embodied phases |

### Fields backing the harder guards

Several guards above need state beyond the basic envelope; v0 carries generic,
optional fields for them (defined in Section 5) rather than harness-specific
ones, so each maps to a contract field instead of `adapter_context` improvisation:

- **Kanban "no re-dispatch without state change"** → `idempotency.prior_state_version`
  (board/card revision, lease epoch): same `dedupe_key` with an advanced version
  is a legitimate re-dispatch, not a duplicate.
- **BEAM "distinguish process restart from principal identity"** →
  `process_instance_id` (this incarnation) versus `logical_principal_id` (the
  surviving principal), so a supervisor restart does not read as identity churn.
- **Lumen "protected drawing phase"** → `harness_local_phase`, an opaque-to-core
  token the harness-local policy interprets; the normative core does not read its
  value, which is why that guard stays harness-local rather than in the Section 7
  table.

These fields are optional and uninterpreted-by-core except where a harness's own
policy reads them; they bind no adapter that does not need them. What v0 still
does **not** specify (left to per-harness implementation PRs) is the concrete
semantics each harness attaches — e.g. how a BEAM adapter sources restart
intensity, or the exact Lumen phase vocabulary.

## 12. Model neutrality and model-aware budgets

The event policy is harness-first. Models are interchangeable behind the same
valid-event gate.

Universal rule:

    No model/provider receives an invalid, untrusted synthetic, duplicate,
    replayed, or ambiguous event as an unconstrained user turn.

Model/provider metadata is diagnostic and may be absent before routing. It is not
part of the validity proof.

Model-specific differences should tune budgets and review depth, not the safety
invariant:

| Model class | Budget bias |
|---|---|
| Expensive frontier model | Stricter retry/tool budget; earlier pause on ambiguity |
| Local/cheap model | More diagnostics allowed, still read-only unless event is valid |
| Coding ACP model | Require diff/test/handle verification before commit/push claims |
| Smaller/weaker model | More tool grounding required before factual assertions |
| Long-context model | Still needs explicit resume provenance; context length is not proof |
| Fast summarizer model | Useful for watchdog summaries; must not actuate restarts blindly |

## 13. Policy evaluation response shape

Adapters and policy evaluators should expose the decision in a shape that keeps
measurement, policy, and enforcement separate.

The example below uses base decision/enforcement values that are defined by the
closed enum list immediately after it; harness-local display labels must map back
to those base values before audit or alert routing consumes the result.

    {
      "schema": "unitares.harness_event_policy_result.v0",
      "event_id": "...",
      "measurement": {
        "synthetic_control_event": true,
        "duplicate": false,
        "restart_count_window": 4,
        "identity_assurance_tier": "weak"
      },
      "diagnosis": ["poisoned_resume", "restart_storm"],
      "policy_evaluation": {
        "policy_name": "harness_event_safety_default",
        "policy_version": "v0",
        "decision": "quarantine_session",
        "reason_code": "auto_resume_attempt_budget_exceeded",
        "reason": "auto-resume event exceeded attempt budget after restart storm",
        "scope": "session",
        "prior_event_id": null
      },
      "enforcement": {
        "mode": "circuit_breaker",
        "applied": true,
        "actor": "harness_adapter",
        "scope": "session",
        "ttl_seconds": null
      },
      "validation_handling": {
        "count_as_live_agent_behavior": false,
        "count_as_diagnostic_probe": true
      }
    }

### Closed enums in the result shape

So heterogeneous adapters produce a routable, auditable record, the following
fields are closed base sets (adapters may add harness-local values only if they
also map to one of these base values; consumers route on the base value):

- `policy_evaluation.decision`: one of the closed base decisions
  (`reject`, `block`, `quarantine_session`, `pause_harness`, `dedupe`, `warn`,
  `allow_read_only`, `allow`) — the Section 7 compound decisions are normalized
  into these per the Section 1 precedence ladder, with their nuance carried in
  `reason_code`.
- `enforcement.mode`: `circuit_breaker | session_quarantine | blocked_write |
  lease_release | operator_escalation | no_op`.
- `reason_code` is a **closed, machine-routable** set keyed to the Section 7
  rows (e.g. `auto_resume_attempt_budget_exceeded`, `identity_tier_insufficient`,
  `synthetic_no_automation_policy`, `missing_idempotency_key`,
  `unverified_safety_field`, `unknown_enum_value`); `reason` is the free-text
  human-readable companion and is not routed on. `diagnosis` carries measurement
  labels and is distinct from `reason_code`.

## 14. Non-normative incident mapping: Hermes Discord auto-resume loop

The incident that motivated this policy maps to the envelope as:

    {
      "harness_type": "hermes_gateway",
      "transport": "discord",
      "event_origin": "auto_resume",
      "classification": {
        "synthetic_control_event": true,
        "auto_resume": true,
        "diagnostic_probe": false
      },
      "attempts": {
        "resume_attempt": 2,
        "restart_count_window": 3
      },
      "authority": {
        "requested_effect": "model_turn"
      }
    }

Default result — this is the ordered enforcement *pipeline* for the matched
event, not the Section 1 precedence ladder (precedence selects the single
strictest decision; this is what enforcement then does, in order):

    block model turn -> quarantine session -> alert operator once -> recover transcript read-only from a fresh locus

The same pattern should not be described as an EISV failure or a model failure.
It is a harness/session-resume failure whose telemetry may inform governance.

## 15. Implementation split after review

This policy PR should land or be revised before implementation PRs. Expected
follow-ups:

1. **Hermes gateway PR** — reject blank/synthetic auto-resume events before the
   model/plugin path; add resume attempt budget and poisoned-session quarantine.
2. **UNITARES host-adapter PR** — emit/consume event envelopes; gate durable
   governance writes on event provenance and identity assurance.
3. **Webhook/cron/Kanban PRs** — add dedupe keys, retry budgets, and diagnostic
   labeling per adapter.
4. **BEAM resident PR** — map supervisor restart intensity and logical-principal
   anchors into the envelope without collapsing process restarts into identity.
5. **Lumen/Anima PR** — define body-loop protected phases and defer/reject
   disruptive effects while preserving read-only observation.

## 16. Review questions for other models/harnesses

Use this review packet when passing the policy around to different models.

### Safety/adversarial review

- How could a synthetic, replayed, or duplicate event still reach a model turn?
- Which fields can be forged by an untrusted harness?
- Where does the policy accidentally allow side effects from diagnostic probes?
- Are there denial-of-service risks in quarantine/pause behavior?

### Harness-integrator review

- Can your harness reliably populate the minimum fields for each effect class?
- Which fields are expensive or impossible without deeper host changes?
- What is the smallest adapter change that would make the policy enforceable?
- Does the policy distinguish platform retry from operator intent clearly enough?

### Upstream-maintainer review

- Which parts are generic enough for Hermes or other upstream harnesses?
- Which parts are UNITARES-specific and should remain local policy?
- Is the policy too strict for normal recovery paths?
- What test would convince you the implementation prevents the bug class without
  breaking ordinary resume/retry behavior?

### Model-behavior review

- Does this preserve model interchangeability?
- Are model-specific thresholds framed as cost/uncertainty budgets rather than
  hardcoded provider assumptions?
- Would a smaller/local model need additional grounding before policy decisions?
- Could a summarizer/watchdog model accidentally become an actuator?

## 17. Acceptance criteria for this PR

This PR is ready when reviewers agree that:

- the core invariant is harness-neutral;
- measurement, policy, and enforcement are not collapsed;
- trusted versus caller-supplied envelope fields are separated;
- synthetic/replayed/duplicate event behavior is fail-closed for side effects;
- diagnostic probes are excluded from live dogfood/ablation claims by default;
- identity assurance and proof origin are visible and not treated as equivalent;
- implementation follow-ups are split by harness rather than merged into one
  monolithic patch.

## 18. Explicit non-goals

- No EISV math changes.
- No live gateway/runtime changes.
- No automatic quarantine implementation in this PR.
- No claim that Hermes Discord is the only affected harness.
- No claim that any model caused the incident.
- No new durable identity semantics beyond the existing identity ontology.
