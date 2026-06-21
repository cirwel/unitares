# Monitor-Delegated Liveness — v0 (design, ahead of demand)

**Created:** June 21, 2026
**Author session:** agent-7cbd038c-509 (claude_code-claude_7cbd038c), lineage
successor of be73f43b (the discovery's author) → 0cc34b13.
**Status:** v0 — **design only; DO NOT BUILD YET.** The reframe is verified-correct
architecture, but live verification (2026-06-21) shows the "authoritative monitor"
branch has **zero live consumers** today: nothing flows through it. Building it now
would be inert inventory (`feasible ≠ needed`; cf. the inert-build lesson of #819).
This doc records the design and the **build-trigger** so it ships *with* demand
rather than ahead of it.
**Council pass 1 (2026-06-21, parallel agents):** dialectic-knowledge-architect
(1 BLOCK, 3 CONCERN, 3 NIT), feature-dev:code-reviewer (1 BLOCK + drift
corrections), live-verifier (4/4 VERIFIED). All folded into this v0 text. The
defer decision survived review unchanged; the fixes are precision/honesty
corrections to the doc, not to the conclusion.
**Source:** KG insight `2026-06-21T16:20:42.605560+00:00` (be73f43b, the reframe) +
its live-verified scoping correction `2026-06-21T17:21:03.613863+00:00` (this
session). Both `status: open`.

## What this is — and what it is NOT

This sharpens the **ephemeral-liveness wire** (`2026-06-17T00:23`, shipped) and the
**false-archival root-cause** thread (`2026-06-17T00:12`; PRs #720/#721/#725/#726/
#779/#794–#797). It is about *how the runtime learns an agent is still alive* — the
signal the lineage/archival gate consumes.

- It is **NOT** a new fix for an active bug. The false-archival recurrence was
  already addressed by the self-heartbeat wire (below). This is the next
  refinement of the *quality* of that liveness signal.
- It is **NOT** the agent-orchestrator proposal (`agent-orchestrator-beam-v0.md`),
  though it depends on that orchestrator becoming live to have a consumer.
- It is **NOT** about *work*-liveness ("is the agent still doing useful work?").
  That is the separate, unsolved stuck-detection problem (see *The reframe*, point 2).

## The reframe

The check-in heartbeat conflates two different questions. Separate them:

1. **Process-liveness** — "is the OS instance still running?" This is
   **runtime-legible**. A runtime that *owns* the process (an OTP supervisor over a
   `Port`, a VM hosting a resident GenServer) knows the instant it dies, via a
   `:DOWN` monitor / `Process.alive?`. This signal is proof against *agent-process*
   hard-kill: it fires on SIGKILL / crash of the child, exactly where a *graceful*
   self-release never comes. It is **not** proof against *host*-loss — if the BEAM
   VM itself dies (power-cut, kernel panic) the monitor dies with the monitored and
   no release fires, degrading to TTL expiry, the same fallback as self-report.

2. **Work-liveness** — "is the agent still doing useful work?" This is
   **meaning-legible**, not runtime-legible. No liveness lease — self- or
   monitor-sourced — can answer it. "BEAM knows a port died; it cannot know the
   agent went off the rails." Keep it as a separate problem; do not let the
   liveness wire pretend to cover it.

**The claim:** process-liveness should be *delegated to the owning monitor* where
one exists, and self-report kept only as the fallback for owner-less agents.

## The current wire — self-report (live today)

Producer (check-in path): `src/mcp_handlers/identity/agent_presence_lease.py`.
On the first `process_agent_update` (sync_state) an agent **acquires its own**
`agent:/<uuid>` lease; later check-ins heartbeat it. `holder_kind="remote_heartbeat"`,
`holder_class="process_instance"`, `_PRESENCE_TTL_S = 600`. Scheduled
fire-and-forget from `core.py:_schedule_agent_presence_heartbeat`.

Consumer (archival gate): `src/mcp_handlers/identity/process_binding.py:310`
`has_live_agent_lease()` —

```sql
SELECT EXISTS(
  SELECT 1 FROM lease_plane.surface_leases
  WHERE surface_id = $1            -- 'agent:/<uuid>'
    AND released_at IS NULL
    AND expires_at > NOW()
)
```

This is **self-report**: the *agent itself* keeps its own liveness fresh. Two
failure modes:

- **(a) Hard death** (crash / SIGKILL / host-loss) produces no graceful release, so
  a dead agent reads LIVE for up to the full TTL (≤600s). The false-archival window
  shrank from "stale `agent_state`" to "stale lease" — not to zero.
- **(b) Silent-dark** — the exact case UNITARES most wants to catch (healthy →
  "proceed on your own accord" → off-task → silent; cf. `2026-06-05T16:22`,
  Sentinel `2026-06-14`): a hung agent may keep heartbeating (loop alive, reasoning
  dead) or stop without prompt archival. This is the work-liveness ceiling — not
  closable by any liveness lease.

## The layered design (build when triggered)

Layer, don't replace. The consumer is `has_live_agent_lease`, called by the
archival gate in `lifecycle/stuck.py:_archive_superseded_parents` (the
`get_live_bindings` → `has_live_agent_lease` guard pair). It reads a single
`agent:/<uuid>` row regardless of who keeps it fresh. (`lifecycle/lineage_reachability.py`
does **not** call the lease check itself — it is shadow/measure-only, gated by
`UNITARES_LINEAGE_TRANSITIVE_ARCHIVAL`, and relies on the same `stuck.py` guard.) The design adds a
**provenance distinction** so the gate can know the *confidence* of the signal:

| Source | Who keeps it fresh | Death detection | Holder marking |
|--------|--------------------|-----------------|----------------|
| **Authoritative (monitor)** | the owning runtime releases on `:DOWN` / Port-exit | instant, hard-kill-proof | a distinct `holder_kind` (e.g. `orchestrator_monitored`) so the row is legibly monitor-backed |
| **Fallback (self-report)** | the agent's own check-in heartbeat | ≤ TTL lag on hard death | existing `remote_heartbeat` |

- For **orchestrator-spawned** agents, `agent_runner.ex` *already* acquires the
  `agent:/<id>` lease on `init/1` and releases it on `{:exit_status, status}`
  (`finalize/2`) and in `terminate/2` (moduledoc §Lifecycle, lines 12–24). The
  release **is** the monitor signal — but today the acquire uses
  `holder_kind="remote_heartbeat"` (`agent_runner.ex:486`), the *same* kind as the
  Python self-heartbeat path, so a monitor-held row is **not yet distinguishable**
  from a self-reported one. The build therefore spans both sides: the Elixir acquire
  must stamp the new monitor `holder_kind`, *and* the consumer must read provenance
  and demote self-heartbeat to fallback. "Release plumbing exists" is true; the
  provenance marking does not.
- For **owner-less raw-MCP agents** (interactive Claude Code, Codex, ad-hoc
  scripts) there is no owning runtime, so self-report is irreducibly the only
  signal. The fallback is **kept by design**, not a gap to close.
- The two surfaces must be **made non-conflicting by the demote rule** — they do
  not avoid conflict for free. `agent:/<uuid>` is a single surface under an
  active-unique partial index (`(surface_id, holder_agent_uuid)` WHERE
  `released_at IS NULL`, migration 025), so only one active holder wins the row. If
  the monitor holds it, the agent's self-heartbeat re-acquire returns
  `held_by_other` and *silently fails* — that is the intended fallback suppression,
  but it must be made explicit, not assumed. The inverse race is the hazard: a late
  self-heartbeat re-acquiring after a monitor `:DOWN` release would clobber the
  authoritative death signal with stale self-report — the exact hard-kill case this
  design exists to fix. The demote rule must make monitor-release terminal.

**A regression the design must own:** delegating process-liveness to a monitor
makes it *more* trustworthy (instant, hard-kill-proof for agent-death), which
*widens* the gap to work-liveness. A monitor confidently reporting "process alive"
on a silently-dark agent (the work-liveness / failure-mode-(b) case) is a *stronger* false-reassurance
than a self-heartbeat that at least *could* lapse via TTL. The fallback's TTL is the
only **accidental** work-liveness signal the system has — a hung-but-not-heartbeating
agent self-archives via TTL today; a monitored hung agent never will. That is a real
argument for preserving TTL/fallback semantics *even where a monitor exists*, not a
reason to avoid monitor-delegation — but the design must not present it as pure
upside.

## Live-verified reality — why NOT now (the gating finding)

Verified 2026-06-21 against the governance DB and process table:

1. **`agent_orchestrator` is NOT running.** Nothing listens on its spawn surface
   (`POST /v1/agents`, default port **8789** per `agent_orchestrator/config/config.exs`,
   distinct from the lease-plane VM on 8788); `curl` to it is refused. **Zero**
   orchestrator-spawned agents exist. (Confirms the "still inert since 2026-06-12"
   memory + the dormant-capability registry entry, PR #793.)
2. **Every `agent:/<uuid>` presence lease is `remote_heartbeat` self-report**
   (`audit_session = NULL`) — a single `holder_kind` group, **zero** monitor-sourced
   rows (~1.9k total as of the 2026-06-21 snapshot, but the durable fact is the
   *shape*: 100% self-report). Leases cycle ~5–10 min as raw-MCP Claude/Codex
   sessions and Sentinel re-acquire on the check-in path.
3. **dispatch_beam is live**, but its `Dispatch.Lease` `:DOWN` monitor governs
   `resident:/dispatch/<thread>` (`local_beam`) surfaces — **disjoint** from the
   `agent:/` governance-presence surfaces. It does not source `agent:/` liveness.

**Consequence:** 100% of governance-presence liveness is self-report today; the
authoritative branch has no inputs. Shipping the monitor-delegation layer now = a
code path nothing flows through.

## A false-archival nuance (corrects an implicit framing)

Narrow claim first: for the *false-archival of the dead agent itself*, hard death
is benign — a genuinely dead agent *should* be archived, and self-report only
**delays** correct archival by ≤ TTL. The classic false-archival risk is the
inverse — a genuinely **live** agent whose self-report lapsed past TTL (a slow
inter-check-in gap); monitor-delegation does not worsen that, and *lowering* TTL
would.

**But ≤TTL of stale-live is not fully harmless — and this is the real present harm
monitor-delegation addresses.** Because the gate is *liveness-gated* (a parent is
not archivable while it reads live, #720/#779), a hard-dead agent reading LIVE for
up to 600s **suppresses legitimate lineage succession**: a real successor declaring
`parent_agent_id=<dead uuid>` is *blocked* from archiving its genuinely-dead parent
until the dead parent's lease finally expires. That is a present (small, bounded)
correctness cost of self-report that a monitor would close instantly.

So monitor-delegation's true benefit is **prompt, truthful archival of dead agents**
— unblocking real successions without waiting out the TTL — and **not depending on
the agent's own check-in cadence**. Real, but modest, and only where a monitor
exists. It is a latency/correctness win on the *dead* path, not a closure of the
*live-but-lapsed* false-archival case (which self-report owns and TTL tuning, not
monitoring, governs).

## Build-triggers (do not pre-build)

Honest state: there is really **one** trigger, currently blocked, plus a parked
maybe.

- **(A) — the real trigger: the orchestrator de-inerts** and becomes the live spawn
  path → ship monitor-delegation *with* it. Work spans both sides: the Elixir
  acquire stamps the new monitor `holder_kind`, the consumer reads provenance and
  applies the demote rule (the lease *release* plumbing in `agent_runner.ex` already
  exists; the marking and demotion do not). **Currently blocked:** de-inerting
  collides with the **Wave-3 deferral to 2026-06-24** — not a clean "now" move.
- **(B) — parked maybe: retarget to the one live monitor.** Make `resident:/`
  `local_beam` lease release `Process.monitor`-sourced (instant on resident death)
  instead of TTL-bounded. Its only named consumer — Sentinel false-archival latency
  — is **already mitigated** (#685/#686/#687), so this is not really a live trigger
  unless a *second* `resident:/` consumer (e.g. dispatch threads) has an unmitigated
  false-archival cost (see Open Questions). Treat as dubious until that is shown.

## Open questions

- **Holder-kind grammar — a DB migration with more surface than it first looks.**
  The `holder_kind` allowlist is a DB-level `CHECK` first established in **migration
  024** (`024_lease_plane.sql`: `CHECK (holder_kind IN ('local_beam',
  'remote_heartbeat'))`) — *not* migration 042, which canonicalizes the `agent:/`
  *surface scheme*. Adding `orchestrator_monitored` means amending **three** things,
  not one: the `IN (...)` allowlist, the **`heartbeat_required` pairing CHECK**
  (024 lines 29–32 currently legalize only `remote_heartbeat↔true` and
  `local_beam↔false` — a monitor kind needs a decided pairing), and the Elixir
  insert logic in `repo.ex` (~line 169) that hardcodes the pairing. A flag column on
  the existing row is the alternative to evaluate against this. Guarded contract;
  cf. the dispatch_beam client-contract gotchas (`2026-06-21T06:50`).
- **Demote rule precedence + the dead-branch provenance blind spot.** When both a
  monitor-marked and a self-heartbeat acquire target one `agent:/<uuid>`, the
  active-unique index admits only one row, so the consumer can `SELECT holder_kind
  ... LIMIT 1` instead of `EXISTS` to read provenance on the **live** branch. But on
  a **false** (no live lease) there is no `holder_kind` to read — so the gate cannot
  distinguish "monitor confirmed dead" from "self-report merely lapsed," and *those
  two have opposite archival meaning* (truthfully-dead vs. possibly-live-but-dark).
  The provenance distinction the design adds exists only on the live branch; the
  dead branch — arguably the more important one for an archival decision — stays
  provenance-blind. Carrying confidence there (a tombstone release-reason? a
  released-by-monitor marker on the released row?) is unresolved.
- **Whether (B) is worth doing at all** given Sentinel is already mitigated — i.e.
  is there a *second* `resident:/` consumer (dispatch threads) whose false-archival
  is not yet mitigated? If not, (B) has no live justification.

## References

- KG `2026-06-21T16:20:42` — the reframe (be73f43b).
- KG `2026-06-21T17:21:03` — live-verified scoping correction + build-trigger (this session).
- KG `2026-06-17T00:12` / `2026-06-17T00:23` — false-archival root-cause + the shipped self-heartbeat wire (cb954a90).
- KG `2026-06-21T06:50` — dispatch_beam lease-plane client-contract gotchas (verify-against-runtime).
- Code (consumer): `src/mcp_handlers/identity/process_binding.py:310` `has_live_agent_lease`.
- Code (producer): `src/mcp_handlers/identity/agent_presence_lease.py`; hook at `src/mcp_handlers/core.py` `_schedule_agent_presence_heartbeat`.
- Code (gate): `src/mcp_handlers/lifecycle/stuck.py` (`_live_lineage_parent_ids`, `_archive_superseded_parents`); `src/mcp_handlers/lifecycle/lineage_reachability.py` (shadow, gated by `UNITARES_LINEAGE_TRANSITIVE_ARCHIVAL`).
- Code (monitor): `unitares-deploy/elixir/agent_orchestrator/lib/agent_orchestrator/agent_runner.ex` (Port-exit lease release); `dispatch_beam/lib/dispatch/lease.ex` (`:DOWN`-sourced release of `resident:/`).
- Related proposals: `agent-orchestrator-beam-v0.md`, `surface-lease-plane-v0.md`, `beam-footprint-roadmap-v0.md`.
