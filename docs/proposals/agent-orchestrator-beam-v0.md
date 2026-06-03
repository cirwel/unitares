# Agent Orchestrator on BEAM — v0 (thin slice)

**Created:** June 3, 2026
**Status:** v0 — thin vertical slice, council-reviewed, lifecycle bugs fixed,
12 tests + live-verified. NOT merged to any running surface; it is a library +
smoke, not a service. Scope decision (which layer) made with the operator this
session: **Layer A — BEAM as orchestrator/supervisor of ephemeral agents**, pulled
by *fleet capability we lack* + *architectural coherence* (explicitly NOT a fix
for a measured failure). Lease-binding kept for v0 with a documented self-heal
caveat (operator call); the `agent:`-scheme fix and a possible
governance-lineage-injection pivot are deferred follow-ups (see findings).

## What this is — and what it is NOT

This is a **new axis** of the BEAM footprint, distinct from the two existing tracks:

- It is **NOT** the governance-server migration (Wave 1–3 handler dispatch,
  `beam-footprint-roadmap-v0.md`). That track is about where the governance MCP's
  handlers run. This is about the *agents that call governance*, not governance.
- It is **NOT** the lease plane itself (`surface-lease-plane-v0.md`). It is a
  *client* of the lease plane.

It is BEAM owning the **lifecycle of ephemeral agents** — short-lived external
runtimes (a Claude SDK process, `claude -p`, a tool worker) — as OTP-supervised
children, one process per agent, each wrapping a `Port`.

### The trap we did not walk into

Reimplementing the agent loop (call-model → parse-tools → dispatch → loop) in
Elixir was the rejected option. The harness/SDK is Anthropic-maintained and
moving fast; rebuilding it to own less is a losing trade. BEAM owns
**lifecycle**, not the loop. The Port is the boundary: the loop stays in the
runtime Anthropic maintains.

## Topology

    AgentOrchestrator.Supervisor            (one_for_one)
    ├── Registry  (AgentOrchestrator.Registry)   agent_id -> runner pid
    └── AgentSupervisor  (DynamicSupervisor)
        └── AgentRunner  (GenServer + Port)       restart: :temporary

`restart: :temporary` is deliberate. Ephemeral agents are not resurrected on
exit — a finished or crashed agent stays finished. The supervisor buys
*lifecycle ownership* (clean spawn, tracked teardown, lease release, fan-out over
a known child set), not crash-restart durability. This is the honest scope.

## Lease binding — kept in v0, with an honest caveat

On spawn, an agent optionally acquires a lease for its surface (default
`agent:<id>`) via the lease plane's RFC §5 HTTP surface; on exit it releases.
This gives the fleet plane a record of which ephemeral agents are live, and
admission control is free: `lease: %{required: true}` (default) refuses to start
the agent if the lease is denied — fail closed. The `LeasePlaneClient` is
behaviour-injected (`:lease_client` spec key) so tests and standalone runs need
no live plane.

### CAVEAT — these leases do NOT self-heal at TTL (council finding, 2026-06-03)

An earlier draft of this note claimed "orphans self-heal via the reaper's TTL
(`remote_heartbeat` = pure DB TTL row)." **That is false for the surface the
orchestrator actually uses**, and it was caught by council review + verified
empirically against the live plane:

- The plane routes `holder_kind` **by surface scheme, not by the request body**
  (`http_router.ex:457-466`). Only `file://` surfaces get the TTL-row
  `remote_heartbeat` path. Every other scheme — `resident:/`, and any future
  `agent:` — is coerced to `local_beam`, which spawns an **auto-renewing holder**
  on the plane's node. The `holder_kind: "remote_heartbeat"` this client sends is
  silently overridden.
- Proof: an orphan left by a failed smoke run had its `expires_at` *advancing*
  under active renewal (12:07:46 → 12:09:26), not decaying toward a TTL reap. It
  had to be explicitly released.

**Consequence for v0:** lease cleanup rests entirely on the runner's release
paths (`finalize/2` on exit, `terminate/2` on stop), which the lifecycle-bug
fixes below now make robust. The **residual risk** is a hard crash that skips
`terminate/2` (`:brutal_kill`, VM crash): such a lease will NOT self-heal — it is
held by the plane-side renewing holder until the plane restarts or an operator
force-releases. The proper fix (an `agent:` scheme with non-renewing TTL-row
routing) is deferred — see finding 1.

## Verification

- **Unit:** 12 tests, 0 failures (`mix test`, stable across seeds) — supervised
  spawn/capture/exit, multi-line + merged-stderr + non-zero-exit + env
  passthrough, executable-not-found refusal, over-long-line bounding, registry
  list/count/stop, `run_fleet`, lease acquire-on-spawn / release-on-exit /
  release-on-port-open-failure / required-lease-denial / best-effort (via stub).
- **Live:** `scripts/live_smoke.exs` against the running plane (127.0.0.1:8788)
  proves a real acquire→run→release round-trip — `lease_released: true` is
  asserted, not assumed. (First run reported a false success; the success check
  now requires the release to actually land. See findings.)

## Lifecycle bug fixes (council review, 2026-06-03)

The slice went through a 3-agent council (code-reviewer + architect +
live-verifier). Operator chose "fix the bugs, keep lease-binding with the caveat,
defer the architecture call." Fixed:

1. **Orphan on port-open failure.** `init/1` used a `with/else` where the acquired
   `lease_id` was out of scope on the port-open-failure branch, so a failed Port
   open after a successful acquire leaked the lease. Restructured to a `case` so
   the release path sees the lease.
2. **Skipped release retry after a transient error.** `terminate/2` skipped its
   release whenever `release_status` was non-nil — including a prior `{:error,_}`
   (plane briefly unreachable). Now retries unless the prior release was `:ok` or
   `:no_lease`. Matters precisely because these leases do not self-heal.
3. **Exit-status / port-EXIT ordering race.** The `{:exit_status}` handler
   required `state.port` to still match the reference; if the linked-port
   `{:EXIT}` cleared it first, the status was dropped, waiters hung forever, and
   the lease was never released. Both messages now route through a shared
   `finalize/2` that is order-independent.
4. **`await/2` caller crash.** A race between `whereis/0` and the call landing
   could exit the caller `:noproc`; now caught → `{:error, :not_found}`.
5. **Unbounded partial-line buffer.** A child emitting a line longer than
   `@line_max_bytes` with no newline grew `partial` without limit; now flushed at
   the cap.

## Findings surfaced by building the slice (operator/council decisions)

1. **No `agent:` surface scheme — and the missing scheme is *why* leases don't
   self-heal.** The canonical scheme list is `file dialectic resident capture td`;
   `agent:` is rejected `invalid_scheme`. Adding it is not "append a string to two
   allowlists" — it requires a lifecycle-routing decision in `acquire_for_surface`
   (`http_router.ex:457-466`): ephemeral agents want the **non-renewing TTL-row**
   path (like `file://`), NOT the auto-renewing `local_beam` holder every non-file
   scheme currently gets. That touches `Canonicalize` + acquire routing in **both**
   Elixir and Python (single-writer cross-repo coordination surface per CLAUDE.md)
   — an operator/council follow-up. Until then, the self-heal caveat above stands.
   The runner's `:surface_id` is overridable so a valid scheme works today.
2. **`release_reason` is allowlisted.** Valid: `normal | down_local | reaped_* |
   handoff` (live-verifier confirmed a 7th, `forced`, in the DB CHECK). The runner
   releases with `normal`. (The first smoke run used an invalid reason and the
   release 422'd — caught only because the live success-check was later made
   honest. The orphan it left is the same one that proved the no-self-heal finding.)
3. **`holder_kind` is coerced by the plane** (live-verifier). For non-`file://`
   surfaces the plane stores `local_beam` regardless of the requested
   `remote_heartbeat`, enforced by a DB CHECK. Callers must not infer accepted
   `remote_heartbeat` semantics from a 200.

## Deferred (not in v0)

- A control surface (HTTP/MCP) to spawn/list/stop agents from outside BEAM.
- Distributed Erlang multi-node fan-out.
- The `agent:` surface scheme (finding 1) and any governance-onboarding env
  injection contract for spawned agents (so a spawned agent declares lineage to
  its orchestrator).
- A launchd plist + deploy story (this is a library + smoke today, not a service).
