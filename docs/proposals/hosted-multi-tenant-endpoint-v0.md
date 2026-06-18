# Hosted Governance Endpoint — Scope & Decision (v0)

Status: **scoping / not committed** — decision doc, no implementation.
Author: Claude (session 2026-06-18), at operator request.

## Why this exists

External adoption of UNITARES is gated by setup friction. The client side is
now nearly seamless (gov-plugin PR #73 bundles `.mcp.json` so install
auto-registers the MCP server — no hand-edited JSON). The remaining wall is the
**server**: an adopter still has to stand up Postgres+AGE+pgvector+Redis
themselves. A "just works like a hosted agent" experience requires UNITARES to
*offer a server they point at*, not one they operate.

This doc scopes what that takes — honestly — given the current architecture. It
deliberately does **not** pick the destination; it lays out the models, their
real cost, and the one product decision that dominates all of them.

## Current reality (single-tenant, localhost)

Grounded by read-through 2026-06-18 (file:line cited). The load-bearing fact:
**UNITARES today assumes one trusted operator running one fleet with shared
visibility.** There is no tenant boundary anywhere.

| Surface | State today | Citation |
|---|---|---|
| Tenant/org concept | **Absent.** Flat global agent namespace; no `tenant_id` on any table | `db/postgres/schema.sql:29-106` (`core.agents`, `core.identities`); `db/postgres/knowledge_schema.sql:23-67` |
| Knowledge graph scope | **Globally shared.** KG search has no tenant filter; an unfiltered search returns every discovery in the fleet | `src/storage/knowledge_graph_postgres.py:61-96`; `src/mcp_handlers/knowledge/handlers.py:1017-1170` (agent filter is post-query, opt-in, not a boundary) |
| Remote auth on `/mcp` | **No global gate.** Default bind `127.0.0.1`; LAN exposure is opt-in env; the `/mcp` endpoint itself accepts unauthenticated POSTs. HTTP REST auth is optional and skipped on trusted networks | `src/mcp_listen_config.py:33-74`; `src/http_api.py:160-200`; `src/mcp_server.py:919-1050` |
| Operator token | Per-handler gate for operator-class actions only (redaction, dashboard writes) — **not** a tenant boundary and **not** on the endpoint | `src/mcp_handlers/identity/operator.py:55-102` |
| Identity isolation | Fleet-global. IP:UA fingerprint binding is not org-scoped; lineage DAG is one global forest; presence/leases keyed by bare `agent_id` | `src/mcp_handlers/middleware/identity_step.py:46-159`; `schema.sql:41-42,77-78` |
| Data sent at check-in | Rich + verbatim: `response_text`, tool names, complexity/confidence, drift vectors, provenance — stored unredacted | `src/mcp_handlers/core.py:340-353`; `src/mcp_handlers/updates/context.py:11-83`; `schema.sql:146-183` |

**Consequence:** strict-identity (#425) gates *writes within a fleet*, but
provides **no tenant isolation**. It is necessary-but-not-sufficient for hosting
untrusted tenants. Pointing two organizations at one current deployment would
let them collide identities, entangle lineage, and read each other's KG.

## The two hosting models

### Model 0 — Hosted, isolated-per-adopter (recommended first)

Each adopter gets their **own** deployment instance (container + DB), provisioned
behind a per-adopter URL (`https://<org>.gov.cirwel.org/mcp/`). Isolation is at
the **infrastructure boundary**, not the schema. This sidesteps *all five* gaps
above because there is no cross-tenant surface — the global namespace is fine
when the fleet *is* the tenant.

What it minimally needs (small, well-bounded):
- **An auth gate on `/mcp`** — even a single shared bearer per instance — so a
  provisioned URL isn't open to the internet. (Today: absent.)
- **TLS + a provisioning story** (spin up instance, mint its bearer, hand back
  the URL). The gov-plugin already reads `UNITARES_SERVER_URL` as a base, so the
  client side is *done* — an adopter sets one env var and is governed.
- A baseline ops posture (backups, resource caps) per instance.

Cost: mostly ops/provisioning, not core rewrites. This is the fastest honest
path to "install → governed against a server you didn't build."

### Model 1 — True multi-tenant SaaS (large; defer)

One shared deployment, many orgs. Requires changes across **all five** surfaces:
1. `tenant_id NOT NULL` FK on `core.agents`, `core.identities`,
   `knowledge.discoveries`, `core.agent_state` + Postgres RLS or app-layer
   enforcement.
2. Tenant-scoped bearer required on every `/mcp` request; unbound callers
   resolve to a tenant or are rejected.
3. Lineage boundary: reject cross-tenant `parent_agent_id`; partition the DAG.
4. KG row-level tenant isolation.
5. Privacy classification + redaction of org-specific fields in any cross-tenant
   context.

This touches the identity/onboarding **single-writer surface** deeply and is a
multi-week project, not an afternoon. Don't start it until adoption justifies
the shared-infrastructure economics.

## The product crux: the shared knowledge graph

This is the decision that dominates, and it is a **product** call, not a security
one. UNITARES's value proposition includes a *shared* KG — cross-agent network
effects. Multi-tenancy (Model 1) directly contradicts that: tenant A must not
read tenant B's discoveries. So hosting forces a choice:

- **Per-tenant KG** (isolated) — safe, but loses the cross-org network effect
  that makes a *shared* graph valuable. The graph is only as rich as one org.
- **Opt-in shared/public KG layer** — a deliberately public stratum adopters can
  publish to and read from, atop their private per-tenant stratum. Preserves
  network effects without leaking proprietary work — but is net-new design.

Model 0 dodges this entirely (each adopter's KG is their own fleet's, by
construction). It only becomes forcing under Model 1.

## Privacy surface (true under every model)

A hosted endpoint *receives the raw operational text of an adopter's work* —
tool names, response summaries, drift vectors (`core.py:340-353`,
`context.py:11-83`), stored verbatim with no field redaction. Regardless of
isolation model this demands an explicit data-handling posture (retention,
operator-access boundary, a stated privacy contract). **Some adopters will only
ever accept self-hosting** — which is why the one-command self-host path
(Docker quickstart, now green + CI-guarded) must remain a first-class option
*alongside* any hosted offering, never replaced by it.

## Recommendation

1. **Ship the client seam now** (PR #73, done) and **publish the plugin to the
   Claude Code marketplace** so install is `/plugin install`, no clone. Lowest
   risk, serves every model.
2. **Hosted = Model 0 first.** Add a single auth gate on `/mcp` + TLS +
   per-adopter provisioning. This is the "hosted like an agent" experience at a
   fraction of Model 1's cost, and it needs no schema surgery.
3. **Treat Model 1 (multi-tenant SaaS) as deferred**, contingent on adoption,
   and **resolve the shared-KG product question before** starting it — that
   choice, not the schema work, is the real gate.
4. **Keep self-host first-class** for privacy-sensitive adopters.

## Smallest next concrete step (if approved)

The one reusable primitive both Model 0 and Model 1 need, and which is absent
today, is **an authentication gate on the `/mcp` endpoint**. A bearer-token gate
(env-configured, default-off to preserve localhost dev) is a contained,
testable change that unblocks any hosted posture without committing to tenancy.
That — not a schema migration — is where implementation should start.
