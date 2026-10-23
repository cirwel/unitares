# Live Timeline pane on BEAM, v0 (portal stage 2)

**Created:** 2026-10-23
**Status:** Draft, design only. Nothing is built. Written in response to the operator direction of 2026-10-23 (KG `2026-10-23T11:39:51.328628+00:00`): the BEAM work aims at a live, harness-agnostic portal over every vendor's agents, with EISV and similar instruments as subscribing plugins. The portal is a control plane above harnesses, not a new harness. This document covers **stage 2 only** (put the timeline live on Phoenix LiveView). Merging it authorises M1 and nothing else (see §10). M1 carries no auth-boundary change, so nothing in it is reserved to the operator. All boundary work is in M4.
**Basis:** the drafting session wrote three candidate designs: grow inside `elixir/dialectic_live`, start a new `elixir/portal` app, or push-patch the Python dashboard tab. Three design-selection judges scored them, and all three picked the first. A four-seat council then reviewed the draft. This revision applies its findings (see Review record).
**Inputs:** PR #3045 (`GET /v1/timeline`, branch `claude/agent-timeline-endpoint`), PR #3047 (dashboard Timeline tab, `claude/agent-timeline-tab`, open and not a draft), PR #3049 (`state_json.report_excerpt`, `claude/checkin-report-excerpt`, draft); `elixir/dialectic_live/`; `src/broadcaster.py`; `wave-3-reduced-scope-gate-v0.md` and amendment-2; `eisv-core-boundary-v0.md`; `federation-component-boundaries-v0.md`.

## 1. Problem and goal

The operator's portal has four stages: (1) a Timeline tab in the Python dashboard (#3045, #3047, #3049); (2) a live timeline through BEAM/Phoenix LiveView; (3) steering actions, which are operator-reserved new actions; (4) a plugin interface. Stage 1 is in review. This document proposes stage 2.

Stage 2 is a cheap test of the **platform bet** that the `dialectic_live` README names: is LiveView a better host for live portal panes than the buildless JS dashboard? Fidelity, request cost, flicker and development cost decide it. The test is deliberately not "does push beat polling". After M0, the JS tab is also push-triggered (§2), so that question has no BEAM-specific answer. A "no" at M1.5 retires this pane. It does **not** decide where stages 3–4 run. That depends on control-plane needs (supervised per-viewer processes, fan-in, later steering), which a read view cannot exercise (§9.2).

The pane shows whatever agents already report through the existing adapters. It adds no vendor reach. Stage 2 does not build the portal's identity, plugin contract, steering, or exposure beyond loopback.

## 2. What exists

**`elixir/dialectic_live`** (Phoenix 1.8 / LiveView, `127.0.0.1:8790`, launchd `com.unitares.dialectic-live`, KeepAlive, no `RunAtLoad`):

- Owns no data and has no Ecto. It reads through two channels only: the `/ws/eisv` firehose and `POST /v1/tools/call` (`README.md`, `mix.exs`).
- `firehose.ex` opens one `Mint.WebSocket` per node. It fans every frame onto PubSub `"governance:events"` and `dialectic*` frames onto `"dialectic:events"`. It reconnects after a fixed 10 s, with no backfill. The upgrade sends no headers, so it is accepted only through the local-posture trusted-network bypass (`src/http_routes/access.py` `_check_ws_auth`). The log holds 1355 "Firehose config failed" lines, all `econnrefused`. There are no tests.
- `governance.ex` is a `Req.post` client (`receive_timeout: 8_000`, Req's default retries) with the bearer held server-side. It exposes only `dialectic` list/get.
- `dialectic_live.ex` subscribes to `"dialectic:events"` and handles only `:refresh` and `{:governance_event, _}`, with no catch-all clause. It does not use `<Layouts.app>`. Nothing subscribes to `"governance:events"`.
- **The LiveView socket has never connected in production.** Prod leaves `check_origin` at its default, which pins the socket to the endpoint url host (`localhost`: `config.exs`, `runtime.exs` `PHX_HOST || "localhost"`). A council probe on 2026-10-23 got 403 for an upgrade with Origin `http://127.0.0.1:8790` and 101 for Origin `http://localhost:8790`. Before that day's probes the log held no "CONNECTED TO Phoenix.LiveView.Socket" line. That is evidence about the bet itself: the beachhead pane has served page GETs but no recorded live session.
- **The current browser boundary is two generator defaults, both load-bearing.** (a) `config/prod.exs` `force_ssl: [exclude: [hosts: ["localhost", "127.0.0.1"]]]` redirects any other Host to https. A probe with `Host: rebind.example:8790` got a 301, so this works as a Host allowlist against DNS rebinding. (b) The default `check_origin` refuses foreign socket origins. Neither is documented as a security control.
- `application.ex` still starts the generator's `{DNSCluster, query: ... || :ignore}`, and `mix.exs` lists `:dns_cluster`. That is a clustering hook, inert unless `DNS_CLUSTER_QUERY` is set. The node runs as `nonode@nohost`, and `epmd -names` does not list it.
- `scripts/start.sh` sources the whole secrets file with `set -a`, so the process holds every key in it, not only the ones the pane uses.
- `config/test.exs` does not turn the firehose off. `start_firehose` defaults to `true` from `runtime.exs`, so the CI `dialectic_live` job already starts a Firehose against `127.0.0.1:8767`.
- Deploy: `cirwel update` → `deploy-apply.sh` → `deploy-dialectic-live.sh` (compile, strict asset build, kickstart, `/health` poll, reset-to-PREV rollback). `/health` is decoupled from the LiveViews.
- `mix.lock` pins Req 0.6.2, which ships `Req.Test`.
- The README and moduledocs are stale. They say the server emits no `dialectic_*` events, but it has emitted them since 2026-06-29 (#1251).

**`GET /v1/timeline`** (#3045, open, not on master, so a 404 live today) serves six kinds: `checkin`, `outcome`, `lifecycle`, `knowledge`, `lineage` and `dialectic`. `limit` is per kind (default 500, max 2000), and truncation drops the oldest rows. Unknown params return a 400. Summaries are cut to 280 code points (`summary[:279] + "…"`). Check-in events carry `state_json->>'action'`, but no state-row id and no `report_excerpt`. Lifecycle reads 7 audit types. #3045 also adds a 20-line read-only `SELECT` on `core.dialectic_sessions` to `src/dialectic_db.py` (§5).

**`/ws/eisv`** (`src/broadcaster.py`) is an in-process singleton with a 2000-entry in-memory ring. The ring is lost on restart and never replayed, and every client gets every frame. The stream is unversioned, unfiltered and carries raw sensor payloads, so it is not a declared subscribe contract. Average total rate is about 3.5 frames/min and bursty. Over 24 h, audit.events held 3333 `knowledge_read`, 13 `knowledge_write` and 20 `lineage_declared`.

| Timeline kind | On the firehose? |
|---|---|
| checkin | as a by-product: `eisv_update`, emitted by the EISV websocket enrichment (`enrichments.py`, `@enrichment(order=220)`, failure logged at debug); no `epistemic_class`, row id or excerpt |
| lifecycle | partly: `lifecycle_*` frames, an overlapping but different set from the 7 types history reads (`_LIFECYCLE_EVENT_TYPES`, which also includes the two `circuit_breaker_*` types) |
| knowledge | yes: `knowledge_write` with `discovery_id` (summary cut at 497 chars + "...") |
| dialectic | transitions only (`dialectic_*`) |
| outcome | **no** (`outcome_events.py` never broadcasts) |
| lineage | **no** (`lineage_declared` is audit-only, `src/mcp_handlers/identity/handlers.py`) |

**Load**, from read-only DB counts on 2026-10-23 over 24 h: 1525 check-in rows (about 63/h; 552 in the last 6 h, already over the default `limit=500`); 363 non-trajectory outcomes (about 15/h visible by default) and 1322 `trajectory_validated`; 0 rows in the 7 timeline lifecycle types. Over 30 days the populated lifecycle types are `lifecycle_archived`, `lifecycle_silent` and `lifecycle_silent_critical`. The live share is therefore essentially check-ins.

**Verdict field.** The persisted check-in `action` is `decision.sub_action || decision.action` (`phases.py:2063-2064` and `2148-2149`, `broadcaster.verdict_of`). On the wire, `decision.action` is "proceed" for both approve and guide.

**#3047 defect, independent of this proposal.** `timeline` is in `RELOAD` but has no `APPLY` entry (`app.html`), so every frame falls through to the 1.5 s debounced full refetch. That means a refetch on every frame: bursts down to the debounce, about every 15–20 s on average. The tab defaults to 6 h with substrate on. `AUTO_MAX_HOURS = 6`, so the 1 h and 6 h windows auto-refresh and only 24 h is on demand. Its default 6 h check-in kind is truncated today.

## 3. Design

### 3.1 Where it lives

The design adds a second LiveView inside `elixir/dialectic_live`. It needs no new BEAM service, port, plist, deploy script or CI job. Placement is an agent decision taken here (rationale in §9.1), and the operator may veto it. Renaming the app is deferred to M5.

New files under `elixir/dialectic_live/`:

- `lib/dialectic_live/timeline_feed.ex`: the feed GenServer (§3.2).
- `lib/dialectic_live/timeline_adapter.ex`: pure frame→mark functions. This is an **anti-corruption adapter over an undeclared stream**, not a plugin seam (§3.7).
- `lib/dialectic_live_web/live/timeline_live.ex`.
- Tests: `timeline_adapter_test.exs`, `timeline_feed_test.exs`, `firehose_test.exs` (the first Firehose tests), `timeline_live_test.exs`, `endpoint_boundary_test.exs`.
- A shared parity fixture: `tests/fixtures/timeline_parity/*.json`, with frame→mark pairs and history-row cases (280-code-point cut, lifecycle type set, verdict rule). Both the Elixir tests and a new Python test in `tests/` assert it, so the two normalisers cannot drift. Nothing under `src/` changes.

Edited files:

- `governance.ex`: add `timeline/2` (`Req.get`, same bearer, `retry: false`, 8 s timeout, JSON decode with no `Envelope.classify`). Add `:req_options` injection to both calls for `Req.Test`.
- `firehose.ex`: back off from 1 s to 10 s; log `econnrefused` as "governance unreachable" and a refused upgrade (401/403) as "governance refused credentials"; publish connection state on a **new** topic, `"firehose:status"`, never on `"dialectic:events"`; accept `:name` and `:pubsub` options for tests. It still sends **no** auth header. Credentials are an M4 item.
- `application.ex`: start `Task.Supervisor` and `TimelineFeed` outside tests (gated on `start_timeline_feed`, default true); remove the `DNSCluster` child.
- `mix.exs` / `mix.lock`: drop `:dns_cluster`.
- `config/runtime.exs`: remove `DNS_CLUSTER_QUERY`; add `GOVERNANCE_TIMELINE_URL`, `TIMELINE_WINDOW_HOURS` (default 1) and `TIMELINE_REREAD_MS` (default 60000); fix the stale `start_firehose` comment.
- `config/runtime.exs` and `config/test.exs`: `runtime.exs` sets `start_firehose` outside its prod guard and loads after `test.exs`, so a `test.exs` value alone would be overridden. Guard both flags in `runtime.exs` with `config_env() != :test` (and set `start_firehose: false`, `start_timeline_feed: false` in `test.exs`), so tests start their own instances with `start_supervised`.
- `router.ex`: add `live "/timeline", TimelineLive` in the existing browser scope, under the same posture as `/` and `/dialectic`.
- `components/layouts/root.html.heex`: a two-link nav (Dialectic, Timeline). Because it sits in the root layout, the dialectic LiveView needs no edit.
- `README.md`, `AGENTS.md`: correct the `dialectic_*` claim; record the trial URL; replace the generator AGENTS.md with UNITARES rules (read-only toward dialectic, fleet neutrality, no distribution, deploy via `cirwel update`, which credentials the process holds, and that `force_ssl`'s host list and `check_origin` are load-bearing).
- `scripts/ops/deploy-dialectic-live.sh`: a post-deploy smoke check that a `/live/websocket` upgrade with Origin `http://localhost:8790` returns 101.
- Separately, a one-line PR adds `elixir/unitares_sdk` to the dialectic-live drift scope in `scripts/ops/deploy-status.sh`.

The PR is classified **capability**. It adds no tool, action, alias, instrument or model channel.

### 3.2 History (authoritative)

`TimelineFeed` holds the window. It is always started (except in tests). It re-reads only while at least one `TimelineLive` is subscribed: it monitors subscriber pids, re-reads on the first mount, and stops on the last unmount. With zero viewers it makes zero GETs. While viewers are present, it re-reads on every `{:connected}` on `"firehose:status"`, on a 60 s floor, and on a debounced doorbell (5 s, coalesced). Without the firehose it degrades to re-read only, and the header says so.

Each fetch runs in `Task.Supervisor.async_nolink` with `retry: false`. The feed handles `{ref, result}` and `:DOWN`, which also enforces at most one fetch in flight per window. A generation counter discards stale results. A non-200 or undecodable body becomes `{:error, reason}`: the feed keeps the last window and sets `unavailable` or `refused`. It never raises.

A successful response (`events`, `agents`, `truncated`, `counts`, `window`) replaces the held window, subject to §3.3's survivors. The windows live in a protected ETS table with the feed as owner and a supervisor-held heir. LiveViews read through `TimelineFeed.snapshot/1`, which returns `:unavailable` when the table is missing.

Two windows are offered: 1 h (default) and 6 h. The 6 h window passes `limit=2000` so check-ins are not truncated at today's volume. Both are held in the feed and refresh on the same cycle while viewed, so load stays flat in the number of tabs. 24 h is not offered. The broadcaster ring is never used as history. After a governance restart, the pane repopulates on the reconnect re-read.

### 3.3 Live deltas (provisional)

`TimelineFeed` subscribes to `"governance:events"`. `TimelineAdapter` maps only these frames:

- `eisv_update` → two parts. (a) A **core** `checkin` mark: `agent_id`, `ts` = frame timestamp, `authored_by: "unknown"`. This is a *stated stand-in*. The live check-in signal currently comes from an EISV enrichment's output, which inverts the rule that subscribers depend on the core (federation §3 rule 4). If EISV becomes optional, or its enrichment fails, live check-ins stop, and the pane falls back to re-read only (§4 item 0). (b) An **optional EISV decoration**, `verdict = decision["sub_action"] || decision["action"]`, which mirrors `verdict_of` and is rendered and labelled as an assessment. A render without it must work.
- The 7 types history reads (`lifecycle_paused`, `lifecycle_resumed`, `lifecycle_archived`, `lifecycle_loop_detected`, `lifecycle_stuck_detected`, `circuit_breaker_trip`, `circuit_breaker_reset`) → `lifecycle`, plus a doorbell. Other `lifecycle_*` names are ignored until §4 item 3 aligns the sets.
- `knowledge_write` → `knowledge`, with the summary re-cut to Python's rule: if the code-point length is over 280, keep the first 279 code points plus U+2026 (`String.codepoints/1`, not graphemes).
- `dialectic_*` → doorbell only.

Everything else is dropped before ETS: `knowledge_read`, `identity_*`, `dashboard_*` (which carry `operator_label`), `*_finding`, and any frame without an `agent_id`. Mapped rows are broadcast on `"timeline:window"`. There is no per-agent topic until a per-agent view exists.

**Merge rule.** A re-read replaces the window. A provisional row survives until the re-read contains its key, or until it has aged past two re-reads:

- knowledge: exact, on `discovery_id`;
- lifecycle: exact, on (`agent_id`, `event_type`, parsed `ts` instant). `_persist_event` writes the frame's own timestamp. Persist is fire-and-forget after send, so a frame can precede its row;
- check-in: match a history row with the same `agent_id` and `recorded_at` in [frame_ts − 2 s, frame_ts]. The frame timestamp is taken after the state row is written, in the same process, so the lag is one-directional (70–115 ms in a 9-frame sample). On a match, the row takes its `epistemic_class` and `action` from history. Exact matching moves to `state_id` once §4 item 1 ships.

### 3.4 Rendering and bounding

`TimelineLive` reads `snapshot/1` on mount, subscribes to `"timeline:window"` and `"firehose:status"`, and renders with streams:

- A reverse-chronological feed capped at 200 rows, with provisional rows styled distinctly.
- An "agents now" strip: one row per agent from `agents[]`, capped at 25 with show-all, a text filter, and a ticking "last seen". Order is recomputed only on re-read.
- A substrate toggle (default **on**, matching the tab). With it off, provisional check-ins are hidden, because the frame cannot tell substrate from agent. The header then says check-ins arrive on re-read only.

The header shows firehose state (connected / unreachable / refused credentials), LiveView socket state, seconds since the last frame, time since the last re-read, the truncation banner, and which kinds are live (checkin, lifecycle, knowledge) versus re-read only (outcome, lineage, dialectic). It also shows **event-to-push latency** (frame timestamp to stream insert, server side) as trial telemetry. Grouping and styling use record fields only. Nothing branches on resident or vendor names.

### 3.5 Auth and boundary

**M1 changes no auth or boundary code.** The boundary stays what it is today and is written down: the `127.0.0.1` bind, `force_ssl`'s host exclude list (the HTTP Host allowlist), and the default `check_origin` (the socket origin pin). M1 adds `endpoint_boundary_test.exs`, which pins the current behaviour: a foreign Host on HTTP is redirected; a foreign Origin on `/live/websocket` is refused; Origin `http://localhost:8790` connects. The trial URL is therefore **`http://localhost:8790/timeline`**, which connects today. The `127.0.0.1` origin stays refused until M4.

Strict posture, verified in `access.py`: under `rest_strict_required()`, both `_check_ws_auth` and `_check_http_auth` accept only the MCP bearer (`check_mcp_bearer`, when `mcp_bearer_required()`) or a dashboard session. `UNITARES_HTTP_API_TOKEN` is not consulted. Under strict posture, then, the firehose, `/v1/timeline` and the existing `/v1/tools/call` client would all be refused. The live server runs in local posture, where loopback passes through the trusted-network bypass and any header is a no-op. M1 only names the refusal in the log and the header. The fix is client-side credential resolution like `lease_plane`'s `configure_governance_api_token` (MCP bearer first, then the HTTP token), and it is an M4 item.

### 3.6 Server broadcast events

**None are required for M1.** Outcome and lineage freshness is bounded by the 60 s floor, and the UI says so.

### 3.7 Toward stage 4 without declaring it

`TimelineAdapter` is a private adapter over `/ws/eisv` and is expected to be thrown away. The stage-4 plugin contract belongs **server-side** on the core's subscribe contract (`eisv-core-boundary-v0`: `checkpoint.recorded.v0`, `assessment.produced.v0`, `outcome.bound.v0`; federation §3), and the BEAM portal will consume it as one more subscriber. M1 includes an "EISV-absent" render test (no `eisv_update` frames) and a zero-optional-subscribers render test.

## 4. Server follow-ups (separate PRs, not M1)

Each item needs its own PR, checked against the Wave 3 writer inventory and the 2026-10-15 EISV instrument rule (`REGISTERED_READ_MANIFEST`, `scripts/analysis/eisv_ablation_matrix.py`) before it is written.

0. **Core check-in broadcast** (`checkin_recorded`: `state_id`, `agent_id`, `recorded_at`, `epistemic_class`, `action`), independent of EISV. This is the long-term live source for check-ins. It sits on the check-in path, so instrument-change risk is likely, and it may wait for the matching eisv-core-boundary stage.
1. **Check-in id and excerpt** in `fetch_timeline_checkins` / `timeline.py`: `state_id` and `state_json->>'report_excerpt'`. Read-only. Without it, excerpts are dead in live mode in both UIs.
2. **`since` cursor** on `/v1/timeline` (allow-list change). Read-only.
3. **Lifecycle alignment**: a timeline-local type set covering emitted names, chiefly `lifecycle_silent` and `lifecycle_silent_critical`, leaving the shared `_LIFECYCLE_EVENT_TYPES` alone. The live mapping and the parity fixture widen with it.
4. **Server-side derivations** (spawn_reason→relation, pause-band pairing) in `/v1/timeline` before any swimlane port.
5. **Ephemeral broadcast helper**: a new `broadcast_ephemeral(...)` that sends without `_persist_event`, used only by new emitters. `broadcast_event` and `_persist_event` stay untouched, because gated dialectic code calls them (§5).
6. **Outcome broadcast** via item 5, after `db.record_outcome_event`, skipping `trajectory_validated`. This is on the path the registered EISV read consumes. If it counts as an instrument change, drop it and keep outcomes on re-read.
7. **Deferred:** a `lineage_declared` broadcast (identity handler path, an identity-enforcement area reserved to the operator); `epistemic_class` on `eisv_update`; any dialectic additions to the stream (gated area).

## 5. Non-goals and what this stays out of

- **Wave 3 reduced-scope gate.** The gate covers writers of `status`, `phase`, `reviewer_agent_id`, `awaiting_facilitation`, and anything that moves `updated_at` on dialectic sessions (as `scripts/ops/wave3_writer_inventory.py` defines them). It also covers the seven symbols named in gate §1 (`auto_resolve_stuck_sessions`, `_parse_timestamp`, `check_reviewer_stuck`, `_apply_reviewer_reassignment`, `check_timeout`, `select_reviewer`, `is_agent_in_active_session`); the writer inventory's sites (`add_message`, `src/mcp_handlers/dialectic/{session,beam_resolve_client,events}.py`, the `lease_plane` `dialectic_saga` / `dialectic_liveness` / `http_router` modules, the `dialectic_session_write` emitter); and, from gate §10 (A5/A6), the `dialectic_sweep_cycle` emitter and `instrument_version = "wave3-instrument-v2"`. Gated paths call `src/broadcaster.py` `broadcast_event` / `_persist_event` on their emit path (`dialectic/events.py:256`, `dialectic/handlers.py:241`), so those two functions are out of bounds as well. #3045's addition to `src/dialectic_db.py` is a read-only `SELECT`, outside the writer set. #3045's PR body should record that `tests/test_wave3_writer_inventory.py` stays green on it. The pane never calls dialectic `resolve`, `reassign`, `takeover`, `submit` or `add_message`.
- **No steering.** No POST from the timeline. No new tool, action, alias or model channel.
- **No Erlang distribution.** No `-sname`/`-name`, no cookie, no clustering hook (DNSCluster is removed), no clustering with lease_plane, agent_orchestrator, wave3a_handlers, sentinel or dispatch_beam.
- **No data ownership.** No Ecto or Postgres reads.
- **No dashboard change** beyond the separate M0 fix.
- **No auth or boundary change in M1** (§3.5), and no auth-path files in core (`src/dashboard_auth.py`, `src/services/http_tool_service.py`, `src/http_routes/access.py`).
- **No rename, no hand deploys, no `RunAtLoad`, no `DIALECTIC_LIVE_BIND_ALL`, no tunnel.**
- **The dialectic pane's rendering and calls are unchanged.** Its input reconnects faster, because of the shared Firehose backoff.

## 6. Milestones

**M0, prerequisites.**
- #3045 merges and deploys through `cirwel update`.
- #3047's `APPLY.timeline` fix: done on #3047 (commit `f7614cbdd`). Irrelevant frames are swallowed, relevant ones coalesce to at most one refetch per 15 s, and a vitest replays a `knowledge_read`-heavy sequence and counts `DATA.timeline` calls. The tab already shows a truncation banner (`sections/timeline.js`); passing a larger `limit` for its 6 h window remains open there.
- This document lands with a row in `active/README.md` (BEAM footprint, **Active**) and the Active count in `docs/proposals/README.md` bumped by one, after claiming the BEAM proposal surface.

**M1, the smallest live slice** (one PR: the app plus the parity fixture and the deploy smoke check). It contains §3.1–3.4 and §3.7, with no auth change. Tests are DB-free and governance-free, use injected `:name`, `:table`, `:pubsub` and `:req_options`, and use `Req.Test.allow` with `start_supervised`:
- each mapped and dropped frame; the guided-verdict case ({action: proceed, sub_action: guide} → "guide"); the code-point re-cut with emoji and combining marks; the parity fixture;
- merge: a key match per kind, the check-in lag window, and a frame that precedes its persisted row;
- feed: zero viewers gives zero GETs; one GET for N LiveViews; debounce coalescing; reconnect re-read; slow, timeout and 500 stubs keep the last window and set `unavailable`; a 401 stub gives `refused`; a missing ETS table renders `:unavailable`;
- Firehose fan-out, backoff and refused-upgrade labelling against a fake WS;
- renders: empty window, unavailable source, a delta with no refetch, foreign roster, empty roster, EISV-absent, zero optional subscribers;
- boundary pins (§3.5); no clustering child in the supervision tree; `node() == :nonode@nohost`.

A second-family review is requested voluntarily, because the PR touches the endpoint test surface and the Firehose. M1 ships through `cirwel update`.

**M1.5, operator trial** (one to two weeks, nothing sent to core). Open `http://localhost:8790/timeline` beside the tab, **both set to 1 h with substrate on**. The visual forms differ: the tab is a swimlane, the pane is a feed plus a strip. The record is therefore kept **per feature**: is the live feed or the agents-now strip more useful than the tab's equivalent, and what would make it matter? A local append-only trial log outside core (the share of marks first seen live versus at re-read, and the latency distribution) records trial telemetry with no authority. If the answer is no, the pane stays as a dev view or is removed, and §1's limits on what a "no" decides apply.

**M2, only if M1.5 says yes:** port the swimlane after §4 item 4.

**M3:** §4 items 1–3 and 5, then possibly 0 and 6, each gate-checked. With `state_id`, check-in merge becomes exact.

**M4, operator-reserved draft PR(s), before any exposure beyond loopback, any steering, or a move to strict posture:** an explicit Host-allowlist plug in `endpoint.ex` (127.0.0.1, localhost, [::1]) that replaces reliance on `force_ssl`; explicit `check_origin` for the loopback origins; all routes in one guarded `live_session` (`:peer_data` in both transports' `connect_info`, fail closed on nil, documented as defence in depth); a `BIND_ALL` boot refusal; client credential resolution (MCP bearer first, then the HTTP token) for Firehose and Governance; `start.sh` narrowed to named keys; real browser auth (passkey session or portal token) with two-family review.

**M5:** reframe as "the portal" only when a third pane arrives or the operator calls the bet proven.

## 7. Success criteria

- **The documented URL is live:** a browser at `http://localhost:8790/timeline` connects the LiveView socket. The CI test and the post-deploy smoke check both pass.
- **Live without reload:** `eisv_update`, the 7 lifecycle types and `knowledge_write` appear without a page reload or a per-event refetch. Latency is shown. Any numeric target is the operator's to set.
- **Flat and idle-safe load:** zero GETs with zero viewers; one GET per re-read cycle per window with N viewers; at most one per 5 s plus one per 60 s.
- **Survives restarts and outages:** after a governance restart the pane repopulates within one re-read. A governance outage keeps the last window, labelled, and never crashes the node.
- **Read-only and gate-clean:** `git diff --stat` shows nothing under `src/`, `elixir/lease_plane/` or `scripts/ops/wave3_*`; there are no dialectic write calls; `tests/test_wave3_writer_inventory.py` is unaffected; `instrument_version` is unchanged; `scripts/dev/check_component_boundaries.py` passes with no new baseline entries.
- **Ships the existing way, undistributed:** deployed by `cirwel update`, `/health` ok, `nonode@nohost`, no clustering child, bind `127.0.0.1`.
- **Fleet-neutral:** no resident or vendor literal in `lib/`; the foreign-roster and empty-roster renders pass.
- **Honest about coverage:** the UI names the live kinds, the re-read-only kinds, truncation, and when check-ins are re-read-only (substrate off, EISV absent).

The deciding criterion is the operator's per-feature judgement after M1.5 on the platform bet (§1). It carries no authority to retire the tab or to settle the BEAM placement of stages 3–4.

## 8. Risks and known limits

- **Unmerged dependency.** M1 cannot run until #3045 lands. Excerpts need #3049 plus §4 item 1, so they are not live in stage 2.
- **Thin live content.** In practice the live share is check-ins. Knowledge writes run at about 0.5/h, the timeline lifecycle types were silent, and outcomes and lineage are re-read only. M1.5 mostly tests check-in push.
- **EISV dependency** (§3.3). Live check-ins depend on an EISV enrichment until §4 item 0 exists, and an enrichment failure is only debug-logged. The EISV-absent render and the header make that visible. They do not fix it.
- **Strict posture is a known break, not a risk.** Every governance call from this app fails under strict posture until M4's credential work lands.
- **Interim boundary.** Two generator defaults form the boundary (§3.5). M1 pins them in tests but does not harden them. The pane shows every agent's activity through a server-held bearer, and the process holds every key in the secrets file until M4.
- **Firehose edits touch untested code** that the dialectic pane also consumes. M1 adds the first Firehose tests.
- **Truncation.** The 1 h default and the 6 h `limit=2000` cover today's volume. The banner must stay accurate after pushes.
- **Gate interpretation.** The document assumes that read-only consumers and read-only endpoint follow-ups are not instrument changes. That has not been confirmed against `REGISTERED_READ_MANIFEST`. §4 items 0, 6 and 7 carry the most risk.
- **Unverified:** `/v1/timeline` latency at `limit=2000`; frame-rate figures come from short samples.
- **Naming debt** until M5.

## 9. Open decisions for the operator

1. **Placement (decided here, open to veto).** Grow inside `dialectic_live`. It reuses the deploy, CI and plist, and avoids about 10 registration files and a new port during the runway. A new `elixir/portal` would avoid the later rename. The operator may veto before M1 merges.
2. **Which stage-3 property should the shell prove first?** For example: supervised per-viewer state, fan-in from several sources, or a dry-run steering surface. A read pane cannot test the control-plane case for BEAM.
3. **Evaluation standard.** What per-feature answer counts as "LiveView is the better host", and is a numeric latency target wanted at all?
4. **Strict posture.** Is `rest_strict` planned? If so, M4's credential work moves ahead of M2.
5. **Outcome broadcast** (§4 item 6). Is the instrument-change check the deciding test, or should outcomes stay on re-read by policy?
6. **Python tab after M1.5.** It stays the adopter-facing UI. Retiring it is not proposed.

## 10. Authorization

Merging this document authorises **M1 only**, as scoped in §3 and §6, plus three small agent-column changes it names: the M0 fixes on #3047 (the `APPLY.timeline` fix, already made, and the 6 h `limit`), and the one-line `scripts/ops/deploy-status.sh` drift-scope PR (§3.1). M1 contains no auth, identity or boundary change, so it sits wholly inside the agent column. If the operator vetoes §9.1, M1 waits. Merging does not authorise: any change in the Wave 3 gated scope, including `broadcast_event` / `_persist_event`; any new tool, action, alias, model channel or steering control; any `src/` change; any M4 boundary or credential change; Erlang distribution; `RunAtLoad`, `DIALECTIC_LIVE_BIND_ALL` or tunnel exposure; a rename; or M2 and later.

## Review record

- 2026-10-23: the drafting session's three candidate designs were scored by three design-selection judges on fit, cost/runway, risk to measured paths and reuse. All three chose "grow inside dialectic_live". The judges' model families and shared context were not recorded, so this is not review coverage.
- 2026-10-23: a four-seat council returned approve-with-changes. This revision addresses the following:
  - the socket origin blocker: trial URL changed to `localhost`, socket-connect test and smoke check added, and the absence of any recorded live session noted;
  - the EISV-sourced check-in lane (§3.3, §4 item 0);
  - the plugin-seam layer (§3.7, parity fixture);
  - the browser boundary: documented in §3.5, hardening moved to M4;
  - the experiment confound (§1, M1.5);
  - the gate wording (§5) and `broadcast_event` (§4 item 5);
  - strict posture stated as verified fact;
  - the §9.1 vs §10 conflict;
  - the verdict mapping, merge keys, test-harness singletons, feed blocking and crash isolation, the window and truncation premises, and the rate figures;
  - the minor findings: DNSCluster, nav placement, status topic, code-point cut, per-agent topic, latency naming, M0 ownership, classification, idle polling, trial log, positioning and credential scope.

  The second-family review for M1 is still requested.
- 2026-10-23: an independent fresh-context pass checked the revised text against the repo, both PR branches and the running system. It confirmed the runtime claims (socket origin 403/101, the `rebind.example` 301, `nonode@nohost`, the `/v1/timeline` 404 on master) and found seven corrections, all applied: the §5 sources (gate §1, gate §10 and the writer inventory, each named), the `runtime.exs` override of a `test.exs` flag, the `TimelineFeed` start wording, the `phases.py` line numbers, the M0 status on #3047, §10 naming every scheduled agent-column change, and the lifecycle type set listed by name.
