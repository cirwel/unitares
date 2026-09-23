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

## Presence (default-on, best-effort) — self-heal caveat RESOLVED

By default an agent registers an `agent:/<id>` **presence** row on the lease
plane on spawn and releases it on exit. The `agent:/` scheme (migration 042,
PR #588) routes to the **self-healing `remote_heartbeat` TTL-row** path, so the
caveat below is now resolved. Presence is **best-effort** (`required: false`):
a plane failure does not block the spawn. `lease: false` opts out; a `:lease`
map overrides (e.g. `required: true` for a genuinely gating lease). The
`LeasePlaneClient` is behaviour-injected (`:lease_client`) so tests need no plane.

The result's `:presence` field is the distinguishable signal: `:registered`
(plane row exists), `:unregistered` (best-effort acquire failed — agent running
but NOT on the plane, so plane-absence ≠ not-running), or `:disabled`.

### ~~CAVEAT — these leases do NOT self-heal at TTL~~ (RESOLVED by #588)

> **RESOLVED 2026-06-03 via the `agent:/` scheme (PR #588).** `agent:/` surfaces
> now route to `remote_heartbeat` (pure TTL row), so an orphaned presence row
> reaps itself at TTL. The original finding is preserved below for the record.

An earlier draft of this note claimed "orphans self-heal via the reaper's TTL."
That was **false for the surface the orchestrator used at the time**, caught by
council + verified empirically:

- The plane routes `holder_kind` **by surface scheme, not the request body**
  (`http_router.ex`). At the time, only `file://` got the TTL-row path; every
  other scheme (incl. the old `agent:<id>`) was coerced to the auto-renewing
  `local_beam` holder. **#588 added `agent:/` to the `remote_heartbeat` branch**,
  fixing exactly this.
- Proof of the original bug: an orphan's `expires_at` *advanced* under active
  renewal (12:07:46 → 12:09:26) instead of decaying to a TTL reap.

**Residual:** a hard crash that skips `terminate/2` (`:brutal_kill`, VM crash) no
longer leaks indefinitely — the `agent:/` presence row now reaps itself at TTL
(≤ `default_lease_ttl_s`). Explicit release on exit remains the fast path.

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
4. **`await/2` caller crash, and the result lost behind it.** A race between
   `whereis/0` and the call landing could exit the caller `:noproc`; that exit is
   caught. But a fast agent (`echo`, a sub-second worker) could be *gone* before
   the await even looked, and the documented "await earlier / snapshot during the
   run" workaround does not help a fan-out caller collecting results after exit —
   the final result was simply lost to `{:error, :not_found}`. Now `finalize/2`
   writes the terminal result to `AgentOrchestrator.ResultStore` (a TTL-bounded
   ETS table owned by the app, not by any runner) *before* the runner stops, so a
   late `await`/`snapshot` that observes the runner as dead falls back to the
   retained result. The write happens-before process death, so the race is closed,
   not merely narrowed.
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

## v0.1 — HTTP control surface shipped (2026-06-17)

The "spawn/list/stop agents from outside BEAM" item below is now built — the
orchestrator is no longer drive-only-from-inside-the-VM.

- **Routes** (`lib/agent_orchestrator/http_router.ex`, Plug + Bandit):
  `GET /v1/health`, `POST /v1/agents` (spawn), `GET /v1/agents` (list),
  `GET /v1/agents/:id` (snapshot), `POST /v1/agents/:id/await`,
  `DELETE /v1/agents/:id` (stop). Typed JSON envelopes
  (`schema_invalid`/`permission_denied`/`not_found`/`lease_denied`/
  `await_timeout`/`service_unavailable`) + a `protocol_version` field, same
  typed-absence discipline as the lease-plane router.
- **Trust boundary = the lease plane's, and stricter in spirit.** Localhost-only
  IPv4 bind + bearer auth (`AgentOrchestrator.HTTPAuth`,
  `AGENT_ORCHESTRATOR_BEARER_TOKEN`) that **fails closed** (503 when no token is
  configured). `POST /v1/agents` spawns an OS process, so it is an authenticated
  RCE surface by design — the same capability the in-VM `run/1` already had,
  exposed over one trust boundary. An optional `:cmd_allowlist` (default `nil` =
  parity with the in-VM API) constrains which executables a caller may spawn.
- **Spec translation is whitelist-only.** The JSON body → `AgentRunner` spec
  mapping never `String.to_atom`s caller input (atom-exhaustion guard); lease /
  lineage / server-url all route through the runner's existing validation, so a
  bad parent UUID or non-http server URL refuses the spawn as a typed 422.
- **`present/1` is JSON-safe.** `exit_status` is stringified on the abnormal
  `{:port_closed, reason}` close path so a crashed agent's status cannot crash
  the response encoder.
- **Tests:** `test/http_router_test.exs` drives the router in-process via
  `Plug.Test` (no socket, no live plane) — auth (503/401/case-insensitive
  scheme), spawn→await round-trip, the 422/403/404/415/504 typed paths.
- **Deploy:** `scripts/start.sh` (launchd entrypoint, sources secrets,
  fail-closed). A `com.unitares.*` plist template is the remaining deploy step.

Not yet covered by CI (the Elixir apps run `mix test` on the deploy host, not in
GitHub Actions), so the operator's `mix deps.get && mix test` is the gate.

## Deferred (not in v0)

- ~~A control surface (HTTP/MCP) to spawn/list/stop agents from outside BEAM.~~
  **HTTP surface shipped in v0.1 (above).** An MCP-shaped surface is still
  deferred.
- Distributed Erlang multi-node fan-out.
- The `agent:` surface scheme (finding 1) and any governance-onboarding env
  injection contract for spawned agents (so a spawned agent declares lineage to
  its orchestrator).
- ~~A launchd plist + deploy story~~ — `scripts/start.sh` shipped in v0.1; the
  `com.unitares.agent-orchestrator.plist` template is the remaining piece.
