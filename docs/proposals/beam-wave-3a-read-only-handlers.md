# BEAM Wave 3a — Read-Only Handler Port

**Status:** v0.2 — council-fold pass complete; operator review pending.
**Created:** 2026-05-28.
**Parent:** `docs/proposals/beam-footprint-roadmap-v0.md` (V0.3.1 AMENDMENT 2026-05-28).
**Sibling, in flight:** `docs/proposals/beam-wave-3-handler-dispatch.md` (v0.3.2, re-litigation gate open).
**Boundary precedent (outbound, lease plane):** `docs/proposals/surface-lease-plane-v0.md` Phase A — bearer-auth, fail-closed, typed REST envelope.
**Implementation precedent (BEAM listener supervision, outbound-only):** `docs/proposals/beam-wave-1-sentinel.md` — `com.unitares.sentinel-beam` PID 1782, no inbound HTTP surface.

v0.1 framed this RFC as a small first port that would falsify or confirm the architectural-ceiling premise the V0.3.1 AMENDMENT carries. The 2026-05-28 council pass (dialectic-knowledge-architect / feature-dev:code-reviewer / live-verifier) rejected that framing on evidence: the three first-ship handlers do not cross the BEAM↔Python boundary in any way that touches the bug class the architectural argument is about, and key plumbing v0.1 treated as precedent does not exist yet.

v0.2 keeps the same handler scope but reframes the wave honestly: **Wave 3a builds and proves the Python-side proxy infrastructure and the first BEAM inbound HTTP listener that all later Wave 3 ports depend on.** It is load-bearing for Wave 3b/3c regardless of which way the architectural argument resolves. It is not, by itself, evidence for or against that argument.

---

## §1 Scope

### 1.1 In scope — concrete handler shortlist

Four handlers, all currently registered in Python `src/mcp_handlers/`, none of which mutate agent state. The first three are the smallest first-ship cut; the fourth is a stretch goal for the same wave.

**First-ship cut (smallest viable port, three handlers):**

1. **`health_check`** — `src/mcp_handlers/admin/handlers.py:285-354`. Reads the in-process cached health snapshot from `src/services/health_snapshot.py::get_snapshot()`. No DB or Redis at request time. Identity posture: `requires_identity="pre_onboard"`.

2. **`get_server_info`** — `src/mcp_handlers/admin/handlers.py:20-128`. Reads process state via `psutil`, the `TOOL_HANDLERS` registry count, and the on-disk PID-file existence check. No DB, no Redis. Identity posture: `requires_identity="pre_onboard"`. The `current_pid` semantics need explicit care under the proxy — see §6 Q2.

3. **`list_tools`** — `src/mcp_handlers/introspection/tool_introspection.py:100-1065`. Reads `TOOL_HANDLERS` plus static tier/relationship data and the alias map from `tool_stability.list_all_aliases()`. No DB, no Redis. Identity posture: `requires_identity="pre_onboard"`, `rate_limit_exempt=True`.

**Stretch within Wave 3a (port if the first three land clean):**

4. **`describe_tool`** — `src/mcp_handlers/introspection/tool_introspection.py:1067+`. Same registry-only read path as `list_tools`, scoped to a single tool name.

`get_thresholds` was on v0.1's stretch list and is dropped from v0.2: at `src/mcp_handlers/admin/config.py:18` it carries `register=False`, so it is not in `TOOL_HANDLERS` at all and porting it would require flipping the decorator first — a separate operator decision out of scope here.

### 1.2 Explicitly NOT in scope

Same exclusions as v0.1, retained for clarity:

- `dashboard` (DB-backed at `src/mcp_handlers/admin/dashboard.py:24`) — Wave 3b.
- `get_telemetry_metrics` (`handlers.py:356-413`) — uses `run_in_executor`; substrate-tax site, DB-backed. Wave 3b.
- `get_workspace_health` (`handlers.py:459`) — DB + identity-bound. Wave 3b.
- `check_continuity_health` (`handlers.py:130-244`) — calls `mcp_server.load_metadata_async(force=True)`. Not a clean read.
- Anything in `src/mcp_handlers/updates/` — write path; Wave 3 proper.
- Anything in `src/mcp_handlers/dialectic/` — Wave 3c.
- Anything in `src/mcp_handlers/identity/` — Wave 3b.
- `search_knowledge_graph` / `get_knowledge_graph` / `get_discovery_details` — read-only at the agent-state layer, internals run hybrid_rrf fan-out muddied by PR #361. Defer.

The cut is **single-call-per-request, no DB, no Redis, no agent state**. Every in-scope handler satisfies all three.

### 1.3 What Wave 3a does NOT prove

Stated up front so the wave's measurements don't get cited beyond what they support.

- **Wave 3a does not falsify (or confirm) the per-agent-process architectural argument.** That argument is about handlers that contend on the agent lock and serialize under anyio task-group scheduling — exactly the handlers excluded from §1.1 by construction. Evidence on this question belongs to Wave 3b (identity middleware) and Wave 3c (dialectic resolution), not here.

- **Wave 3a does not establish that the boundary topology is cheap on contended traffic.** The PR #533 contention shape (sync I/O blocking the event loop, single ExecutorPool thread starved by `enrich_learning_context`) does not apply to Wave 3a probe traffic, which lands on the MCP server's anyio task group, dispatches to module-level Python attrs, and never touches asyncpg or ExecutorPool. v0.2 does not cite PR #533's 4-way load shape as the relevant comparator.

- **Wave 3a does not scale an existing inbound-HTTP BEAM pattern; it establishes the pattern.** Sentinel (`com.unitares.sentinel-beam`) has zero inbound HTTP surface — its supervisor wires only outbound Finch clients to MCP (8767) and the lease plane (8788). The lease plane's listener is the only inbound-HTTP BEAM precedent in the fleet, and its surface is operator/coordination traffic, not MCP. Wave 3a PR #4 (the Elixir listener + supervisor) is the first instance of BEAM serving inbound MCP HTTP. That is a structurally new risk surface, not a scaling of Wave 1.

What Wave 3a DOES produce is load-bearing for everything after it: a working Python-side internal probe endpoint with the boundary contract; a working BEAM-side HTTP listener with bearer-auth, supervisor restart, and Finch outbound to Python; a per-tool routing table on the Python transport that flips BEAM-vs-Python dispatch; the measurement channel Wave 3b/3c will read against; and a golden-parity test harness for handler ports.

---

## §2 Boundary contract

### 2.1 Topology

```
MCP request (HTTP, port 8767)
    ↓
Python transport (existing)
    ↓ [per-tool routing table — PR #3 — decides BEAM-vs-Python]
    ├── tool routes to Python → existing in-process dispatch (default for everything not in §1.1)
    └── tool routes to BEAM → outbound HTTP to BEAM listener
                                    ↓
                            BEAM listener (Elixir, elixir/wave3a_handlers/, NEW)
                                    ↓ [for §1.1 handlers: per-handler choice — BEAM-internal OR call Python probe]
                            Python probe endpoint (127.0.0.1, internal, NEW)
                                    ↓
                            Response composed BEAM-side, returned to Python transport, returned to MCP client
```

Two NEW components: the Python-side internal probe endpoint and the BEAM-side inbound listener. One MODIFIED component: the Python transport gains a per-tool routing table that introduces a new outbound HTTP call in the hot path for tools flipped to BEAM. The routing table is the actual risky greenfield in this wave; see §5 PR #3 and the reviewer FIND-R4 fold.

### 2.2 Envelope shape

Top-level keys only, matching the lease plane Phase A contract pinned by `test/unitares_lease_plane_test.exs:221`. Verified live against the running lease plane on 2026-05-28: unauthed `GET` returns `{"error":"permission_denied","ok":false,"reason":"bearer token missing or invalid","protocol_version":"v1.0"}`; authed-but-bad-input returns `{"error":"schema_invalid","ok":false,"protocol_version":"v1.0","detail":"surface_id required"}`. v0.1's nested-`data` shape would have failed the lease-plane analogue test before merge. v0.2 abandons it.

**Success:**

```json
{
  "ok": true,
  "protocol_version": "wave3a.v1",
  "<handler-specific-keys>": "..."
}
```

**Auth failure (missing/invalid bearer):**

```json
{
  "ok": false,
  "protocol_version": "wave3a.v1",
  "error": "permission_denied",
  "reason": "bearer token missing or invalid"
}
```

**Other failures:**

```json
{
  "ok": false,
  "protocol_version": "wave3a.v1",
  "error": "<machine_readable_class>",
  "detail": "<operator_readable_string>"
}
```

The Elixir handler module ships with a test pinning this contract verbatim — same shape as `test/unitares_lease_plane_test.exs:221` — and that test gates PR #4 merge.

### 2.3 Python probe endpoint

For handlers where the BEAM listener needs data Python currently owns in process memory (`TOOL_HANDLERS`, `runtime_config`, psutil enumeration of the Python process), Wave 3a ships a minimal Python-side internal HTTP surface at `127.0.0.1:<probe-port>/v1/probe/*`:

```
GET  /v1/probe/server_info       → top-level keys matching get_server_info response body
GET  /v1/probe/tool_registry     → top-level keys: tools, aliases, tiers, deprecated_tools
GET  /v1/probe/health_snapshot   → top-level keys matching health_check response body (full, no lite filter)
```

Bearer-auth-gated; see §2.5. NOT registered as an MCP tool — only the BEAM listener calls it.

### 2.4 Identity posture — what actually gates the in-scope handlers

All three first-ship handlers carry `requires_identity="pre_onboard"`. The operative mechanism that lets requests reach those handlers without an onboarded identity is **attribute lookup in the middleware**, not the decorator attribute being "informational" (the comment at `src/mcp_handlers/decorators.py:38-42` is stale relative to current behavior).

Concretely: `src/mcp_handlers/middleware/identity_step.py:608` reads `get_tool_identity_requirement(canonical_name) == "pre_onboard"` and, if so, lets the request proceed unbound. The historical hardcoded allowlist (`{health_check, get_server_info, list_tools, describe_tool, get_governance_metrics, skills, identity, onboard, bind_session}`) is named only in a comment at lines 603-606 documenting what the attribute lookup replaced.

Pre-cutover gate: each Wave 3a handler must be verified to read `pre_onboard` via `get_tool_identity_requirement` immediately before its BEAM cutover. The verification is one-line and lives in PR #5's pre-merge script.

The BEAM listener does not implement identity middleware in Wave 3a. It accepts requests, dispatches to either BEAM-internal handlers or the Python probe, and shapes the response. Wave 3b is the middleware port; Wave 3a does not commit to a delegation pattern.

### 2.5 Token discipline

Two intentionally separate token surfaces:

- **Public MCP bearer auth** — unchanged. The Python transport continues to validate `UNITARES_HTTP_API_TOKEN` before any routing decision. The BEAM listener never sees public traffic directly; it sees inbound HTTP from the Python transport, internal-only.
- **Python transport ↔ BEAM listener token** — `WAVE_3A_BEAM_TOKEN` in `~/.config/cirwel/secrets.env`, validated on the BEAM listener's inbound surface; missing/unset returns 503 (fail-closed, mirroring lease plane).
- **BEAM listener ↔ Python probe endpoint token** — `WAVE_3A_PROBE_TOKEN` in `~/.config/cirwel/secrets.env`, validated on the Python probe; missing/unset returns 503.

Three tokens look like over-engineering until rollback: each can rotate independently and each scopes a distinct trust boundary.

### 2.6 Response shape contract

Wave 3a handlers ported to BEAM MUST produce responses byte-equivalent to the current Python responses for the same input, modulo timestamp masking. Same rule pattern from Wave 3 v0.3 §7.2: keys matching `(.*_at|.*_time.*|.*_ms|server_time|processing_time_ms|uptime.*)` are masked before comparison. Capture script at `scripts/dev/wave3a-capture-goldens.sh`; fixtures at `tests/fixtures/wave3a_response_golden/`; comparison test at `tests/integration/test_wave_3a_response_parity.py`. Pre-cutover gate: 100% golden parity on every in-scope handler.

---

## §3 Rollback path

Read-only handlers, no agent-state mutation; worst case under rollback is a brief window of 5xx on the ported tools, no data corruption possible.

### 3.1 Cutover and rollback shape

- **Cutover:** BEAM listener launched via `com.unitares.wave3a-handlers.plist`. Python transport's per-tool routing table flips `health_check`, `get_server_info`, `list_tools` from Python-dispatched to BEAM-routed.
- **Rollback:** routing table flips back to Python; `launchctl unload com.unitares.wave3a-handlers.plist`. Python implementation never stopped — it just wasn't being called for the ported handlers during the cutover window.

Single-command rollback: `bash scripts/ops/wave-3a-rollback.sh`. Emits `coordination_failure.wave_3a.rollback`.

### 3.2 Failure modes covered

1. **Probe-endpoint token misconfiguration.** Probe returns 503 → BEAM observes → BEAM returns `governance_temporarily_unavailable`. Detected via §4.2 stop sign.
2. **BEAM listener crash / supervisor restart latency.** Python transport's BEAM-proxy path has a 500ms hard timeout; on timeout it falls back to in-process Python dispatch (this fallback is itself the §4.2 surveillance target — see FIND-R4 fold below). Fallback logged as `coordination_failure.wave_3a.fallback_to_python`.
3. **BEAM unhealthy under load.** Every routing-table-flipped tool accumulates the 500ms timeout before falling back; fallback is NOT near-zero cost. If §4.2 fires, rollback is mandatory.

### 3.3 What rollback does NOT cover

- A handler ported that turns out to be hiding a write side-effect — §6 Q1.
- A response-shape divergence the golden test missed — §2.6 masking regex.
- A bug in the per-tool routing table itself affecting handlers NOT in Wave 3a scope (i.e., the table corrupting routing for an unrelated tool). PR #3 carries its own integration test pass with a local BEAM stub before any handler cutover lands.

---

## §4 Stop signs

Two falsifying criteria. Each is narrowly scoped to what Wave 3a actually measures.

### 4.1 Boundary HTTP transport cost under contention

After cutover, the BEAM-proxied handler's p99 latency exceeds the Python-in-process p99 from the trailing 7-day baseline by more than **5×** on any of the three first-ship handlers, measured over a 14-day window.

What this measures: end-to-end HTTP transport cost across the new BEAM-proxy path under live traffic. It is NOT a test of the per-agent-process architectural argument — the in-scope handlers don't touch the agent lock, so anyio-task-group serialization is not on the critical path. The 5× anchor reflects the expectation that an HTTP-proxy crossing should be cheap relative to handler work; if it isn't, the topology is wrong for handlers this light, independently of any architectural-ceiling question.

**Measurement source:** see §4.3 prereq.

### 4.2 503 rate or fallback rate above operator-tolerable

The Wave 3 v0.3 §3.2 sliding-window 503-rate aggregator ports to Wave 3a's surface. Two thresholds:

- BEAM listener returns 503 to the Python transport at a rate above **1%** of accepted requests over any 60s window.
- Python transport falls back from BEAM to in-process dispatch at a rate above **5%** of attempts over any 60s window.

Either breach emits `coordination_failure.wave_3a.cutover_breach` and triggers the §3.1 rollback. Denominator: `measurement.wave_3a.request` rows. Numerator: corresponding failure events.

### 4.3 Measurement-channel prerequisite

Live verification on 2026-05-28: `audit.coordination_measurements` does not exist (`psql -d governance -c "\d audit.coordination_measurements"` returns "Did not find any relation named ..."). The closest existing table is `audit.coordination_events`, event-only — no latency or byte-count columns. Wave 3 RFC §6's measurement channel never landed.

**Wave 3a adopts option (a) from the council pass:** the measurement channel ships as **PR #2 of this wave**, before §4.1 or §4.2 can be evaluated.

PR #2 adds migration `audit.coordination_measurements` with columns: `id BIGSERIAL`, `recorded_at TIMESTAMPTZ DEFAULT NOW()`, `measurement_type TEXT NOT NULL`, `endpoint TEXT NOT NULL`, `elapsed_ms INTEGER NOT NULL`, `status TEXT NOT NULL`, `payload_bytes INTEGER`, `meta JSONB`. Wave 3a writes `measurement_type = 'measurement.wave_3a.request'`. The schema is shared infrastructure: Wave 3b/3c can write distinct `measurement_type` values into the same table.

The decision to land the channel inside Wave 3a (rather than depend on Wave 3 proper to land it first) reflects that the channel is unblocked here — it doesn't require resolving the re-litigation gate on Wave 3 — and is load-bearing for the stop signs above. The cost of the choice is that Wave 3a inherits the schema-design responsibility for a channel later waves also consume; v0.2 addresses that by keeping the schema deliberately minimal and `measurement_type`-keyed so later waves can extend `meta` without migrating.

The architectural-ceiling-falsification stop sign from v0.1 §4.3 is **removed**. It was structurally false on the in-scope handlers (no boundary crossing on the first three; the stretch case did not exercise the bug class either). Re-introducing it requires either porting handlers that hit the agent lock — which by definition belongs to a later wave — or reframing what it claims.

---

## §5 Sequencing

Each PR is small and independently reviewable. PR #3 is the load-bearing greenfield; it carries its own council pass before downstream PRs proceed.

### PR #1 — Python probe endpoint scaffolding

Adds the Python-side `127.0.0.1:<probe-port>/v1/probe/*` surface; implements all endpoints from §2.3; bearer-auth via `WAVE_3A_PROBE_TOKEN` per §2.5, fail-closed → 503 if unset. Envelope shape per §2.2 (top-level keys).

Exercised by a pytest suite at `tests/integration/test_wave_3a_probe.py` driving each endpoint and asserting envelope shape + auth behavior. Includes a probe-side test that verifies the §2.6 timestamp-masking regex masks every non-deterministic field actually returned (catches a `list_tools`-style "non-determinism added later" regression at probe scope, not just at handler scope).

Gate: must merge before PR #2 opens.

### PR #2 — Measurement channel migration

Adds the `audit.coordination_measurements` migration described in §4.3 and wires `coordination_failure.wave_3a.*` event types into `src/coordination_events.py` so the event-type CHECK constraint accepts them. Adds the Python-side write path: the probe endpoint records one `measurement.wave_3a.request` row per call with `endpoint`, `elapsed_ms`, `status`, `payload_bytes`.

Synthetic loadgen at `/tmp/loadgen_wave3a.py` drives the probe endpoint at 1, 4, and 16-way concurrency for 7 days; baseline p50/p99 land in `metrics.series` via `perf_monitor_persist_task` (PR #481).

Gate: ≥7 days of probe-only baseline data before PR #4's go-decision.

### PR #3 — Python transport per-tool routing table and rollback script

This is the load-bearing greenfield in the wave. No prior wave built a Python-side BEAM-vs-Python routing table; v0.1's framing of this as "scaffolding before the real work" understated it.

Scope: adds the routing table data structure to the Python transport, wires the BEAM-outbound HTTP path with the §3.2 500ms hard timeout and Python-fallback, adds the cutover/rollback control surface (`scripts/ops/wave-3a-rollback.sh`), and ships an integration test against a local Elixir BEAM stub binary (built and run by the test fixture, not the production BEAM listener — that's PR #4) covering: routing-table-hit success, routing-table-hit timeout-to-fallback, routing-table-miss passthrough, and rollback-script-empties-table.

Required gate: **independent council pass on PR #3 alone** before PR #4 opens. The architect lane reads the routing-table-flip logic for hot-path regressions on tools NOT in Wave 3a scope. The reviewer lane reads the fallback semantics and the timeout-budget math. The verifier lane runs the local-BEAM-stub integration tests and confirms the table is empty on master before merge.

Smoke test that the rollback script runs cleanly with zero ported handlers (exercises the rollback contract before there's anything to roll back).

### PR #4 — Elixir-side BEAM listener skeleton and supervisor

Creates `elixir/wave3a_handlers/` as a sibling Elixir OTP app. Per FIND-A5 / X2, this is the first inbound-HTTP MCP listener on BEAM in the fleet — sibling-app topology to `elixir/lease_plane/` and `elixir/sentinel/`, but functionally a new pattern.

Skeleton:
- `Wave3aHandlers.Application` + `Wave3aHandlers.Supervisor` (`one_for_one`).
- HTTP listener (Plug + Bandit, matching lease plane) on a Wave-3a-specific port; bearer-auth via `WAVE_3A_BEAM_TOKEN`, fail-closed → 503 if unset.
- `Wave3aHandlers.ProbeClient` Finch-based client to Python probe; bearer-auth via `WAVE_3A_PROBE_TOKEN`.
- Empty handler dispatch table (no handlers wired yet).
- launchd plist `scripts/ops/com.unitares.wave3a-handlers.plist`, NOT loaded by default.
- ExUnit test pinning the envelope shape per §2.2 verbatim (top-level keys, including `reason` on the 401 path), analogous to `test/unitares_lease_plane_test.exs:221`.
- ExUnit coverage for startup, supervisor restart on listener crash, the 500ms fallback timeout (PR #3 owns the Python side of that contract; PR #4 verifies the BEAM side honors it).

### PR #5 — First handler ported end-to-end (`health_check`)

Implements `health_check` BEAM-side. BEAM handler calls the Python probe endpoint `/v1/probe/health_snapshot`, applies the lite-filter logic, returns the §2.2 envelope.

Golden-response parity fixture at `tests/fixtures/wave3a_response_golden/health_check.json`; test at `tests/integration/test_wave_3a_response_parity.py::test_health_check_parity`.

Pre-cutover script: verifies `health_check` reads `pre_onboard` via `get_tool_identity_requirement` (§2.4 gate). Feature-flag-gated on `WAVE_3A_HEALTH_CHECK_ON_BEAM=true`; operator flips manually after PR #4's listener is stable ≥24h.

### PR #6 — Second handler (`get_server_info`)

Same pattern as PR #5. Q2 (§6) decides whether psutil enumeration ports to Elixir or stays Python-side. v0.2 default: delegate to Python via probe — keeps BEAM thin and the response shape provably identical.

**Reviewer FIND-R3 fold:** `os.getpid()` at `handlers.py:35` and the `is_current: true` flag at line 70 identify the Python PID, not the BEAM listener PID. Under the proxy, "current PID" from the client's perspective is ambiguous. Two choices:

- **Accept Python-PID semantics with a golden-fixture comment** noting that `current_pid` and the `is_current` flag refer to the Python backend process, not the inbound listener. Cheapest, structurally honest.
- **BEAM-side field injection** that overrides `current_pid` and the `transport: "HTTP"` field (which inspects Python's `sys.argv`) before returning. More work, hides the topology from the client.

v0.2 default: option 1. Surface as Q2 for council.

### PR #7 — Third handler (`list_tools`)

Largest of the first three. Response is rich (tiers, relationships, aliases). Golden parity test catches divergence.

**Reviewer FIND-R2 fold:** `list_all_aliases()` at `tool_introspection.py:130-131` is in the transitive closure of this handler. If it builds a module-level cache on first call, the probe-endpoint Python path may see a different snapshot than cold-start Python. PR #1's transitive-closure audit (the §6 Q1 deliverable) is gated on this handler: a mechanical audit script lists every callable reached by `health_check`, `get_server_info`, `list_tools`, and `describe_tool` and flags any module-level mutation or lazy-init on first call. PR #7 doesn't merge until the audit clears, including `list_all_aliases`.

### PR #8 — Stretch handler (`describe_tool`)

Single PR, after PR #7 lands. `get_thresholds` is no longer in scope (§1.1).

### PR #9 — Wave 3a sunset decision

After ≥14 days of post-cutover operation with all three first-ship handlers on BEAM, operator writes `docs/handoffs/wave-3a-postmortem-<date>.md`: stop-sign measurements, boundary p50/p99, fallback rate, rollback drills (at least one operator-led drill required, mirroring lease plane Phase B). The postmortem is the input to Wave 3b's go-decision.

---

## §6 Open questions for council

### Q1 — Transitive-closure mutation audit on the in-scope handlers

Survey: read every line of every handler in §1.1 including the helper-function transitive closure. Specifically flag any module-level cache built on first call (`list_all_aliases()` is the named example per FIND-R2), any in-process state mutation (`mcp_server` module attrs, `runtime_config` writes, cache invalidation), and any side-effecting fall-through.

The audit ships as a mechanical script as part of PR #1 and re-runs as a gate on each handler PR (#5, #6, #7, #8). v0.1's prose-level "did you check?" is replaced by a script that lists every reached callable and refuses to clear if any of them touch global state on the first-call path.

### Q2 — `get_server_info` PID and transport semantics under the proxy

See PR #6 FIND-R3 fold above. Choose: accept Python-PID semantics with a fixture comment, OR inject BEAM-side overrides.

### Q3 — Wave 3a sunset vs. merge-into-Wave-3b

After Wave 3b lands, identity middleware lives BEAM-side and Wave 3a's probe endpoints overlap with Wave 3b's surface. Default: keep `elixir/wave3a_handlers/` as a separate Elixir app; multi-app on one BEAM VM is cheap. Council: any reason this is wrong?

### Q4 — Synthetic-loadgen-only vs. live-resident-traffic on §4.1

PR #2's baseline is loadgen-driven; loadgen-only numbers don't reflect live traffic. Wiring Watcher/Sentinel (both currently call `health_check` periodically) through the Wave 3a BEAM listener post-cutover gives live data — at the cost of those residents catching every stop-sign breach.

Default: cut over `health_check` for ALL callers, residents included. Council: is there a less-coupled path?

---

## §7 Memory and references

**Parent and siblings:**
- `docs/proposals/beam-footprint-roadmap-v0.md` — V0.3 RESOLUTION 2026-05-05, V0.3.1 AMENDMENT 2026-05-28, V0.3.2 AMENDMENT 2026-05-09 (Wave 3 re-litigation gate, open).
- `docs/proposals/beam-wave-3-handler-dispatch.md` — Wave 3 full-port RFC at v0.3.2.
- `docs/proposals/beam-wave-1-sentinel.md` — sibling Elixir app pattern; outbound-only.
- `docs/proposals/surface-lease-plane-v0.md` — Phase A inbound-HTTP boundary precedent (bearer-auth, fail-closed, top-level-keys envelope).

**Live-verified anchors (2026-05-28):**
- `com.unitares.sentinel-beam` PID 1782 has no inbound HTTP listener — Sentinel is outbound-Finch only.
- `audit.coordination_measurements` does not exist; PR #2 lands it.
- Lease plane envelope is top-level keys with `reason` on the 401 path: `{"error":"permission_denied","ok":false,"reason":"bearer token missing or invalid","protocol_version":"v1.0"}`.
- `get_thresholds` at `src/mcp_handlers/admin/config.py:18` carries `register=False` — not in `TOOL_HANDLERS`.
- Identity-middleware `pre_onboard` exemption operates via `get_tool_identity_requirement` attribute lookup at `src/mcp_handlers/middleware/identity_step.py:608`, not the "informational" comment at `src/mcp_handlers/decorators.py:38-42`.

**Stop-sign anchors:**
- Wave 3 v0.3 §3.2 — the 503-rate halt mechanism §4.2 ports.
- V0.3.1 AMENDMENT's surviving premise (per-agent-process architectural argument) — explicitly NOT tested here per §1.3.

**Memory entries:**
- `feedback_substrate-migration-status-quo-bias.md` — bias pole this RFC operates in; §1.3's explicit non-claim is the falsification handle.
- `feedback_redraft-cycle-bias-trap.md` — three-redraft caution. v0.2's framing shift is council-evidence-driven (file:line, DB schema, decorator readings), not pressure-driven.
- `feedback_design-doc-council-review.md` — three-lane pattern that produced the v0.2 fold.
- `project_plexus-coordination-layer.md` — lease plane state (Phase A shipped; Phase B opened 2026-05-20).
- `feedback_decisive-defaults-over-tooling-forks.md` — §6 questions carry defaults; council closes on taste, not by surfacing to operator.
