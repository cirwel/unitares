# Portal (the UNITARES hub)

The local app from which an operator will see, start and resume agent
sessions across vendors (`docs/proposals/active/portal-hub-v0.md`). This is
step H2's shell: the boundary, with no sign-in and nothing behind it yet.

- **Loopback only.** It binds `127.0.0.1` and refuses to boot on any other
  address (`Portal.Boundary.check_bind!/1`). No setting widens it.
- **One Host and port.** It answers only for `PORTAL_HOST:PORTAL_PORT`
  (default `127.0.0.1:8791`), sockets included (`PortalWeb.EndpointHostGuard`).
  Every other name or port gets `421`.
- **One Origin.** The LiveView socket accepts only `http://PORTAL_HOST:PORTAL_PORT`.
- **Signed-in operator only.** Every page and every socket mount sits behind
  `Portal.Auth`. Its default backend refuses everyone until the sign-in
  handoff to the governance server's passkey login lands in its own pull
  request.

```sh
mix deps.get
mix test
PORTAL_SECRET_KEY_BASE=$(mix phx.gen.secret) MIX_ENV=prod PHX_SERVER=1 mix run --no-halt
```

`/health` answers `{"ok": true}` and nothing else.

## Configuration

| Variable | Default | Meaning |
|---|---|---|
| `PORTAL_HOST` | `127.0.0.1` | The one Host the hub answers for, and the socket Origin's host. |
| `PORTAL_PORT` | `8791` | Its port. |
| `PORTAL_SECRET_KEY_BASE` | none (prod) | Signs and encrypts the session cookie; the prod boot refuses without it. |

## What comes next

- **H2b:** the governance server's sign-in handoff routes (authorization code with PKCE, on a listener the tunnel never reaches).
- **H2c:** the hub's sign-in client, the timeline as the home screen, and registration with `cirwel update`.
- **H3 onward:** resume and start sessions through the orchestrator's launch profiles.
