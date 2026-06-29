# dialectic_live

Phoenix/LiveView UI-platform beachhead for UNITARES — the first pane of a
deliberate one-pane strangler. It renders a live-ish view of dialectic
deliberation sessions and is the standing place new live panes will grow if the
LiveView platform bet proves out against the buildless JS dashboard.

## Architecture

This app owns **no data**. Postgres and all canonical governance state stay in
the Python governance MCP (`:8767`); this app is purely a *consumer*:

- **`DialecticLive.Firehose`** — a `Mint.WebSocket` client of the broadcaster
  firehose (`/ws/eisv`), re-publishing every decoded event onto
  `Phoenix.PubSub`. Adapted from the Elixir Sentinel's `eisv_web_socket.ex`
  (same reconnect-after-any-failure contract, no application heartbeat). Gated on
  `GOVERNANCE_START_FIREHOSE` so the app still boots when the upstream is down.
- **`DialecticLive.Governance`** — a `Req` client that reaches dialectic
  `list`/`get` through `POST /v1/tools/call` (`{name: "dialectic", arguments:
  ...}`). The bearer token is held server-side and never reaches the browser.
- **`DialecticLiveWeb.DialecticLive`** — the root LiveView. It subscribes to the
  `dialectic:events` topic and treats any event as a doorbell to refetch
  authoritative state, with a 10s refresh floor. Sessions flagged
  `awaiting_facilitation` float to the top with a badge.

## Strangler status

- **B0** (platform up) and **B1** (pane on the data the server already exposes):
  done.
- **B2** (true per-turn streaming) is blocked upstream: the engine does not yet
  emit `dialectic_*` broadcast events (#1167 Ask 1). The PubSub topic and
  doorbell are wired and ready for them.
- The `awaiting_facilitation` badge activates once that field is exposed in
  `dialectic(list)` (#1220 / migration 053).

## Running

```bash
mix deps.get
mix phx.server          # http://127.0.0.1:8790
```

Config (`config/runtime.exs`, all overridable by env): `PORT` (8790),
`GOVERNANCE_WS_URL`, `GOVERNANCE_TOOLS_URL`, `UNITARES_HTTP_API_TOKEN`,
`GOVERNANCE_START_FIREHOSE`. As a standing service it runs via
`scripts/start.sh` under `scripts/ops/com.unitares.dialectic-live.plist.template`
(operator-promoted; no `RunAtLoad`).
