# Portal (the UNITARES hub)

The local app from which an operator will see, start and resume agent
sessions across vendors (`docs/proposals/active/portal-hub-v0.md`). So far it
has the boundary and the sign-in; the timeline moves in next.

- **Loopback only.** It binds `127.0.0.1` and refuses to boot on any other
  address (`Portal.Boundary.check_bind!/1`). No setting widens it.
- **One Host and port.** It answers only for `PORTAL_HOST:PORTAL_PORT`
  (default `127.0.0.1:8791`), sockets included (`PortalWeb.EndpointHostGuard`).
  Every other name or port gets `421`.
- **One Origin.** The LiveView socket accepts only `http://PORTAL_HOST:PORTAL_PORT`.
- **Signed-in operator only.** Every page and every socket mount sits behind
  `Portal.Auth`. Unconfigured, it refuses everyone.

## Sign-in

The hub has no passkeys of its own. It borrows a sign-in from the governance
dashboard, whose passkeys are bound to the dashboard's domain:

1. `/signin/start` keeps a PKCE verifier and a state in a five-minute
   encrypted cookie, and sends the browser to the dashboard's approval page.
2. There the operator uses the passkey if needed, and confirms.
3. The dashboard returns the browser to `/auth/callback` with a one-time code.
   The hub checks the state and exchanges the code, with the verifier, over
   the governance server's owner-only socket. The new session holds only the
   resulting handle.
4. Every request asks the server, over that socket, whether the handle is
   still live. Signing out of the dashboard, or revoking its passkey, signs
   the hub out on its next request. A page left open is asked again every 30
   seconds (`config :portal, :reverify_ms`) and ends when the answer is no.
   `/signout` ends the handle on the server.

The LiveView client is served straight from the pinned dependencies
(`/assets/vendor/phoenix.min.js`, `/assets/vendor/phoenix_live_view.min.js`),
so the hub needs no JavaScript build step.

The server half is `src/dashboard_auth.py`. It needs migration 079, and its
`UNITARES_HUB_CALLBACK_URL` must be this hub's `/auth/callback` exactly.

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
| `PORTAL_GOV_ORIGIN` | none | The dashboard's origin, where the browser approves a sign-in. `https`, or `http` on loopback only. |
| `PORTAL_GOV_SOCKET` | none | The governance server's owner-only socket (its `UNITARES_UDS_SOCKET`). |
| `PORTAL_HUB_TOKEN` | none | The hub credential, the same value as the server's `UNITARES_HUB_TOKEN`. |

Sign-in is on only when all three `PORTAL_GOV_*`/`PORTAL_HUB_TOKEN` settings are set.

## What comes next

- **H2d-2:** the timeline as the home screen.
- **H2d-3:** registration with `cirwel update`.
- **H3 onward:** resume and start sessions through the orchestrator's launch profiles.
