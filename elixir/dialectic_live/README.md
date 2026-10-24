# dialectic_live

Phoenix/LiveView UI-platform beachhead for UNITARES: the place live panes grow
while the LiveView platform bet is tested against the buildless JS dashboard.
It serves two panes:

- **Dialectic** (`/`, `/dialectic`): dialectic deliberation sessions.
- **Timeline** (`/timeline`): every agent's governance events in one window,
  with live marks from the firehose between re-reads (portal stage 2,
  [`docs/proposals/active/portal-live-timeline-v0.md`](../../docs/proposals/active/portal-live-timeline-v0.md)).

**Trial URL: `http://localhost:8790/timeline`.** Use `localhost`, not
`127.0.0.1`: the LiveView socket only accepts the `localhost` origin (see
*Boundary* below), so on `127.0.0.1` the page renders but never goes live.

## Architecture

This app owns **no data** and has no Ecto. Postgres and all canonical
governance state stay in the Python governance MCP (`:8767`); this app is purely
a *consumer*, through three reads:

- **`DialecticLive.Firehose`**: a `Mint.WebSocket` client of the broadcaster
  firehose (`/ws/eisv`), re-publishing every decoded event onto
  `Phoenix.PubSub` (`governance:events`, and `dialectic:events` for
  `dialectic_*` frames) and its own connection state on `firehose:status`.
  Adapted from the Elixir Sentinel's `eisv_web_socket.ex`: no application
  heartbeat, reconnect after any failure on a 1 s → 10 s backoff. Gated on
  `GOVERNANCE_START_FIREHOSE` so the app still boots when the upstream is down.
- **`DialecticLive.Governance`**: a `Req` client. Dialectic `list`/`get` go
  through `POST /v1/tools/call`; the Timeline pane reads `GET /v1/timeline`.
  The bearer token is held server-side and never reaches the browser.
- **`DialecticLive.TimelineFeed`**: holds the Timeline pane's two windows (1 h
  and 6 h) in ETS. It re-reads `GET /v1/timeline` only while a Timeline page is
  open: on the first viewer, on each firehose reconnect, on a 60 s floor and
  on a debounced doorbell. With no viewer it makes no request.
  `DialecticLive.TimelineAdapter` turns firehose frames into provisional marks
  until the next re-read confirms them.
- **`DialecticLiveWeb.DialecticLive`** subscribes to `dialectic:events` and
  treats any event as a doorbell to refetch, with a 10 s floor. Sessions flagged
  `awaiting_facilitation` float to the top with a badge.
- **`DialecticLiveWeb.TimelineLive`** renders a feed (newest 200 rows), an
  "agents now" strip, and a header naming what is live and what is re-read
  only.

### What the Timeline shows live

| Kind | Live | Source |
|---|---|---|
| checkin | yes, as a provisional mark | `eisv_update` frames, which the EISV enrichment emits. Until one arrives the header says check-ins are re-read only; after that it shows how long ago the last one came, so a stalled enrichment is visible as a growing age |
| lifecycle | when broadcast | the seven types `/v1/timeline` reads; transitions the server records without a broadcast (automatic archives among them) arrive on re-read |
| knowledge | yes | `knowledge_write` |
| outcome, lineage, dialectic | re-read only | not broadcast (dialectic frames ring the doorbell) |

The frame-to-mark rules that copy server behaviour (the 280-code-point summary
cut, the lifecycle type set, the verdict rule) are pinned on both sides by the
shared fixture in [`tests/fixtures/timeline_parity/`](../../tests/fixtures/timeline_parity/).

## Strangler status

- **B0** (platform up) and **B1** (the dialectic pane on the data the server
  already exposes): done.
- The server has emitted `dialectic_*` broadcast events since 2026-06-29
  (#1251). The dialectic pane uses them as a doorbell; true per-turn streaming
  (B2) is not built.
- The `awaiting_facilitation` badge needs no further server work: `dialectic(list)`
  returns that field on each session in its default (non-`compact`) shape, which
  is what this pane requests (#1220 / migration 053).
- **Timeline M1**: the live pane, under an operator trial (M1.5) before anything
  further is built on it.

## Boundary

The app binds `127.0.0.1` only. Two Phoenix defaults form its browser boundary
today, and both are load-bearing:

- `config/prod.exs` `force_ssl` excludes only `localhost` and `127.0.0.1`, so a
  request with any other `Host` is redirected to https. On this plain-http
  loopback bind that acts as a Host allowlist against DNS rebinding.
- `check_origin` is left at its default, which pins the LiveView socket to the
  endpoint url host (`localhost`).

`test/dialectic_live_web/endpoint_boundary_test.exs` pins both. Replacing them
with explicit controls is an operator-reserved milestone (proposal §6 M4).

## Running

```bash
mix deps.get
mix phx.server          # http://localhost:8790
```

Config ([`./config/runtime.exs`](./config/runtime.exs), all overridable by env):
`PORT` (8790), `GOVERNANCE_WS_URL`, `GOVERNANCE_TOOLS_URL`,
`GOVERNANCE_TIMELINE_URL`, `UNITARES_HTTP_API_TOKEN`,
`GOVERNANCE_START_FIREHOSE`, `TIMELINE_START_FEED`, `TIMELINE_WINDOW_HOURS` (1
or 6, the window shown on open) and `TIMELINE_REREAD_MS` (60000). As a standing
service it runs via [`./scripts/start.sh`](./scripts/start.sh) under
`scripts/ops/com.unitares.dialectic-live.plist.template` (operator-promoted; no
`RunAtLoad`) and deploys through `cirwel update`.
