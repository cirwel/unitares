# NVIDIA OpenShell: Provenance Check and Federation Fit (2026-10)

**Status:** Research note (external project read). Extends the market map in
[`competitive-analysis-2026-09.md`](competitive-analysis-2026-09.md); no
verdict there is changed.
**Date:** 2026-10-01.
**Prompted by:** the operator asked whether NVIDIA OpenShell
(`github.com/NVIDIA/openshell`) does what UNITARES does "better", and whether
it copied UNITARES, after meeting an NVIDIA employee in 2026-09.
**Method:** full-history clone of OpenShell at `8091f66` (2026-10-01, 1,599
commits) and a read of its `docs/`, `rfc/` and `examples/` READMEs. The
UNITARES side is read from `README.md` and `docs/PRODUCT_DEFINITION.md`. Docs
were read, not products; nothing here was run.

---

## TL;DR

- **No evidence of copying.** OpenShell's first commit is 2026-01-29;
  UNITARES's is 2025-11-20. Neither date is close to the 2026-09 meeting. Its
  code is Rust and gRPC around kernel sandboxing; it uses none of UNITARES's
  distinctive concepts (EISV, dialectic adjudication, claims bound to
  outcomes, process lineage). The overlap is shared vocabulary that this whole
  field uses ("governance", "fleets of agents", "audit").
- **Different layer.** OpenShell decides what an agent *may* do: sandbox
  isolation, network and filesystem policy, credential injection, and
  SMT-checked policy changes. UNITARES records what an agent *did*, who vouched
  for it, who challenged it, and what happened. At its own layer OpenShell is
  far ahead; at UNITARES's layer it does not compete.
- **Good fit for federation.** OpenShell scopes itself to one gateway and
  lists cross-gateway federation as a non-goal. It already exposes the seams
  an external record would attach to. Its own multi-agent example uses a
  GitHub repository as the durable shared record across sandbox restarts,
  which is the job UNITARES does with identity, review and outcomes attached.

## What OpenShell is (as read)

A runtime for fleets of autonomous agents, Apache-2.0, primarily Rust with
Python, TypeScript, Go and Rust SDKs.

| Concept | What it does | Source |
|---|---|---|
| Sandbox | Isolated workload; the kernel restricts files, syscalls and network | [`README.md`](https://github.com/NVIDIA/OpenShell/blob/8091f6687701d5fc73e2e527fc7bb7f5ffff3f9c/README.md) |
| Supervisor | In-sandbox principal that enforces policy and proxies egress; authenticates as `Principal::Sandbox` with a gateway-minted JWT bound to one sandbox UUID | [`rfc/0011`](https://github.com/NVIDIA/OpenShell/tree/8091f6687701d5fc73e2e527fc7bb7f5ffff3f9c/rfc/0011-multi-player-design) |
| Gateway | Control plane and "system of record" for sandboxes, policies and providers; deployable on Kubernetes | [gateway interceptors](https://github.com/NVIDIA/OpenShell/blob/8091f6687701d5fc73e2e527fc7bb7f5ffff3f9c/docs/extensibility/gateway-interceptors.mdx) |
| Provider | Credential usable only at approved endpoints, injected after policy evaluation; SPIFFE JWT-SVID token exchange demonstrated | [SPIFFE exchange demo](https://github.com/NVIDIA/OpenShell/tree/8091f6687701d5fc73e2e527fc7bb7f5ffff3f9c/examples/spiffe-token-exchange-demo) |
| Policy advisor and prover | Agents propose network rules; an SMT solver checks that a proposal adds no risky access and that a subagent's policy stays inside its parent's boundary | [policy prover](https://github.com/NVIDIA/OpenShell/blob/8091f6687701d5fc73e2e527fc7bb7f5ffff3f9c/docs/how-it-works/policies/prover.mdx) |
| OCSF export | OCSF 1.8.0 JSONL from gateway and supervisors for SIEMs; "best-effort, not a guarantee of complete audit history" | [OCSF export](https://github.com/NVIDIA/OpenShell/blob/8091f6687701d5fc73e2e527fc7bb7f5ffff3f9c/docs/observability/ocsf-json-export.mdx) |

## Where the layers meet

| Question | OpenShell | UNITARES |
|---|---|---|
| Is this process allowed to reach that endpoint? | Yes: kernel and proxy enforcement | No |
| Which credential does the call carry? | Yes: provider injection, SPIFFE exchange | No; its identity is "a record for attribution, not a credential" |
| Which process acted, and whose work did it inherit? | Sandbox UUID per sandbox; no lineage across sandboxes | `uuid`, `parent_agent_id`, lineage |
| What was claimed, what supports it, who challenged it? | No | Claims, evidence, dialectic review |
| What happened afterward? | Operation outcomes for its own API calls only | Outcome events bound to prior claims |
| Does the record survive the sandbox? | Sandbox-local OCSF files rotate daily and keep three; gateway JSONL is configurable; both best-effort | Durable Postgres record |
| Many runtimes, one operator | One gateway; federation is a non-goal (RFC 0011) | Single-operator federation kernel over MCP/HTTP |

The two compose without overlap: OpenShell runs and confines the agent,
UNITARES keeps its accountable record. This matches the boundary the README
already draws, in which platform workload identity remains the credential
layer.

## Candidate integration seams

These are read from OpenShell's docs. None is built and none is authorized by
this note. Any adapter would be opt-in and off by default, with no required
dependency (the execution-cost policy in `CLAUDE.md`).

1. **Gateway interceptor, `post_commit` phase.** An external gRPC service sees
   committed control-plane writes (for example `CreateSandbox` and
   `UpdateConfig`) with secrets omitted. OpenShell lists "observe committed
   operations for an audit or inventory service" as an intended use. This is
   where a sandbox's creation could be bound to a UNITARES identity and the
   creating principal. `post_commit` is fail-open by construction, so
   UNITARES would never sit on OpenShell's enforcement path.
2. **OCSF JSONL ingest.** Policy decisions, denials and middleware findings
   (`DetectionFinding`) could enter UNITARES as evidence attributed to the
   agent inside the sandbox. OpenShell calls its own collection best-effort,
   so the durable record would sit in UNITARES. Correlate on
   `container.uid` (sandbox), not `metadata.uid` (event).
3. **Agent inside the sandbox.** The agent's harness talks to UNITARES over
   MCP/HTTP like any other runtime. That needs one egress policy rule to the
   UNITARES endpoint and nothing from OpenShell itself.
4. **Subagent lineage.** OpenShell's prover checks that a parent's policy
   bounds a subagent's policy. UNITARES `parent_agent_id` records that the
   subagent inherited the parent's work. The same parent–child edge is seen
   from two sides: authority bounding in one, accountability inheritance in
   the other.

## What this note does not establish

- **Provenance.** Separate first commits and unrelated stacks make copying
  implausible, not impossible to rule out. Nothing that was read suggests it.
- **Value.** No seam has been exercised. Whether an OpenShell operator wants a
  claims-and-outcomes record is the same buyer question that
  `competitive-analysis-2026-09.md` leaves open.
- **Docs only.** OpenShell's docs were read, not its code. Field names come
  from its documentation and may drift.

## Open questions carried forward

1. Should the first seam be `post_commit`, which carries identity and
   attribution at creation time, or OCSF ingest, which carries evidence over
   the agent's life?
2. Does UNITARES's fleet workload-identity proposal
   (`docs/proposals/active/fleet-workload-identity-auth-audit-v0.md`) gain
   anything from OpenShell's gateway-minted, sandbox-scoped JWT design as
   prior art? The proposal deliberately excludes a third-party credential
   runtime; that decision is unchanged here.
3. Would an OpenShell contributor or deployer be the "operator other than the
   maintainer" that the roadmap's standing gate asks for?
