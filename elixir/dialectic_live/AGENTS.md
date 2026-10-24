# dialectic_live: rules for agents

A Phoenix 1.8 / LiveView app inside the UNITARES repo. The repo-root
`AGENTS.md` / `CLAUDE.md` apply in full; these are the rules specific to this
app. The design record for the Timeline pane is
`docs/proposals/active/portal-live-timeline-v0.md`.

## What this app may do

- **Read only.** It owns no data: no Ecto, no Postgres. It reads governance
  through three channels: the `/ws/eisv` firehose, `POST /v1/tools/call`
  (dialectic `list`/`get`), and `GET /v1/timeline`. Never call dialectic
  `resolve`, `reassign`, `takeover`, `submit` or `add_message`, and add no POST
  from the Timeline pane. Steering is a later, operator-reserved stage.
- **Fleet-neutral.** Group and style by record fields only. Never branch on an
  agent's label, a resident name or a vendor name, and never hardcode a roster.
  Labels are for display.
- **No distribution.** No `-sname`/`-name`, no cookie, no clustering hook
  (DNSCluster was removed on purpose), no clustering with the other BEAM apps.
  The node runs as `nonode@nohost`; a test pins it.
- **Deploy only via `cirwel update`.** Never hand-deploy, restart, kickstart or
  bootstrap the service, and never set `RunAtLoad` or `DIALECTIC_LIVE_BIND_ALL`.

## The boundary is two defaults; keep both

The app binds `127.0.0.1`. Its browser boundary is, today:

- `config/prod.exs` `force_ssl` with `exclude: [hosts: ["localhost",
  "127.0.0.1"]]`. Any other `Host` is redirected, which is what stops DNS
  rebinding on this plain-http bind. Do not widen the list or drop `force_ssl`.
- The default `check_origin`, which pins the LiveView socket to the endpoint
  url host, `localhost`. Do not set `check_origin: false` outside `dev.exs`.
  This is why the trial URL is `http://localhost:8790/timeline` and why
  `127.0.0.1` pages never go live.

`test/dialectic_live_web/endpoint_boundary_test.exs` pins both. Any change to
auth or the boundary (an explicit Host plug, an explicit origin list, a guarded
`live_session`, browser auth) is operator-reserved (proposal §6 M4).

## Credentials the process holds

Credentials come from the service environment. When `UNITARES_SECRETS_ENV`
names a secrets file, `scripts/start.sh` sources the whole file with `set -a`,
so the process environment holds every key in it, not only the two it uses:
`SECRET_KEY_BASE` (from `DIALECTIC_LIVE_SECRET_KEY_BASE`) and, optionally,
`UNITARES_HTTP_API_TOKEN`, which `DialecticLive.Governance` sends as a bearer.
The firehose upgrade sends no credential. On the live server both reads pass
through the local-posture loopback bypass; under strict posture every
governance read from this app is refused, and the pane says so. Narrowing the
keys and resolving credentials properly is M4 work.

## Working on it

- `mix deps.get && mix compile` in a fresh worktree before anything else; then
  `mix test` (DB-free and governance-free; CI runs the same).
- Tests start their own processes with `start_supervised` and injected names
  (`:name`, `:table`, `:pubsub`, `:req_options`); `config/test.exs` turns off
  the application's Firehose and TimelineFeed. HTTP goes through `Req.Test`.
- The frame→mark rules shared with the Python server live in
  `tests/fixtures/timeline_parity/` at the repo root, asserted by
  `test/dialectic_live/timeline_parity_test.exs` and
  `tests/test_timeline_parity_fixture.py`. Change a rule on both sides and in
  the fixture together.
- Nothing in this app may touch the Wave 3 measured paths, including the
  server's `broadcast_event` / `_persist_event`.

## Phoenix conventions used here

- LiveView collections use streams (`phx-update="stream"`, a DOM id per item);
  a reset re-streams the whole collection.
- Use the `<.input>` and `<.form>` components from `core_components.ex`; no
  inline `<script>` tags in templates.
- Add a DOM id to each element a test selects, and test with `has_element?/3`
  rather than raw HTML.
- Use `Req` for HTTP; no `:httpoison`, `:tesla` or `:httpc`.
