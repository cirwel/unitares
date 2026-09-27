# 2 · Installation

[← Overview](01-overview.md) · [Manual index](README.md) · [Next: Running the server →](03-running-the-server.md)

Choose one path:

- **Docker** is the Tier-1 install contract and brings up PostgreSQL, Redis, the
  coordination lease plane, and the server together from a named release.
- **Bare metal** is the advanced macOS operator path. The canonical, maintained
  instructions live in the [install playbook](../install/PLAYBOOK.md); this
  chapter does not duplicate them or present it as an equivalent default.

## 2.1 Docker quickstart

The clone pin below names the latest verified public release, which can lag
the source version while a release is being prepared.

```bash
git clone --branch v2.22.1 --depth 1 https://github.com/cirwel/unitares.git
cd unitares
docker compose up -d --wait
make coordination-demo
```

After cloning, `docker compose up -d --wait` is the one-command install/start;
there is no separate schema bootstrap. `make coordination-demo` verifies the
live coordination boundary by onboarding two participants, rejecting A's
request-bound attestation when it claims B's UUID, refusing replay of a captured
attestation, acquiring one `maintenance:/` surface, refusing a second holder,
handing ownership over through identity-checked mutations, and releasing it.
The local proof uses one operator and a deployment-specific audience. This
version intentionally permits one trusted issuer because lease rows do not yet
persist issuer-qualified principals; it does not establish cross-operator trust
or outcome benefit.

Run `make demo` next to send six warmup check-ins and print the real governance
API response shape. It verifies identity and telemetry wiring; it does not
exercise self-relative scoring or establish predictive value.

When it completes:

- MCP: `http://localhost:8767/mcp/`
- Dashboard: `http://localhost:8767/dashboard`
- Liveness: `http://localhost:8767/health/live`
- Lease plane: `http://127.0.0.1:8788/v1/health` (bearer-authenticated)

If the default ports are occupied:

```bash
POSTGRES_HOST_PORT=15432 REDIS_HOST_PORT=16379 GOVERNANCE_HOST_PORT=18767 \
  LEASE_PLANE_HOST_PORT=18788 \
  docker compose up -d --wait
UNITARES_DEMO_PORT=18767 make demo
GOVERNANCE_HOST_PORT=18767 LEASE_PLANE_HOST_PORT=18788 make coordination-demo
```

The coordination demo talks to both the governance server and the lease plane,
so give it both ports. With only the lease-plane port it falls back to the
default governance port, 8767, and registers its demo agents on whatever server
answers there.

### Choose a model (optional)

UNITARES bundles no model. Naming one turns on two things: `consult` answers
from it, and a `request_review` that no peer has claimed can be taken by the
in-process local reviewer, which drafts the antithesis and a proposed
synthesis. Without a reachable model, `consult` answers "Standard advisory
consultation is unavailable" (`MODEL_PROVIDER_UNAVAILABLE`), and
`request_review` records the thesis and leaves the review
waiting for a peer or the operator. That also happens with a model when the
thesis is about the review system itself (recusal), when the model runs past
the review time budget, or when its synthesis does not approve.

This model is the only one a default install uses. The tools also describe a
stronger lane, `consult(effort='thorough')` and `delegate_inference`, which hand
a brief to a Codex, Claude or Antigravity CLI. That lane is an operator
extension: it runs through the agent orchestrator, which no Compose service
starts, so on a default install those calls fail and say the extension is not
configured. `list_inference_hosts` reports it under
`extensions.agent_orchestrator`. Nothing in this manual needs it.

The quickest way is to run, from the checkout, once Ollama is installed and has
a model pulled:

```bash
make setup-model
```

It lists the models your Ollama has, writes your choice to `.env`, rebuilds the
server, and checks that the server can reach the model. `--help` shows the
non-interactive flags; `--no-docker` prints the two settings for a source
install instead.

To do the same by hand, install Ollama on the Docker host, pull a model, and
name both in `.env`:

```bash
ollama pull gemma4:latest
cat >> .env <<'EOF'
UNITARES_OLLAMA_BASE=http://host.docker.internal:11434
UNITARES_LLM_MODEL=gemma4:latest
EOF
docker compose up -d --build governance-mcp
```

`UNITARES_OLLAMA_BASE` is the Ollama root URL, without `/v1`; the older name
`UNITARES_OLLAMA_BASE_URL` is still read when it is unset. On Docker Desktop the
container reaches the host's Ollama as is. On Linux, Ollama listens only on
127.0.0.1 by default: set `OLLAMA_HOST=0.0.0.0` for the Ollama service and allow
port 11434 from the Docker bridge, without exposing it beyond the machine,
because Ollama has no authentication. `--build` matters on an existing
install: the model client is installed when the image is built, and `up -d`
alone keeps an image built before this step. If your `docker-compose.yml` has
no `UNITARES_OLLAMA_BASE` line, the checkout predates this step. If it has one
but `consult` reports a missing dependency, the image predates it; rebuild with
`--build`.

### Tool discovery (interface 1.13.0 and later)

The current source negotiates one complete catalog, including installed plugin
tools, while advertising a small progressive surface initially. Use
`list_tools(lite=true)` for every capability name, `describe_tool(tool_name=...,
action=...)` for its parameters, and `use_tool(tool_name=..., arguments={...})`
to invoke an omitted capability through its normal gates. Set
`UNITARES_TOOL_ADVERTISEMENT=full` in `.env` to advertise every schema up front,
then recreate the service and reconnect the client. Old `GOVERNANCE_TOOL_MODE`
values are ignored. Action authorization and identity gates are unchanged.

Earlier releases used `minimal`, `lite`, and `full` discovery profiles. The
`standard` profile existed only on unreleased master between #2102 and #2137
and never shipped in a release; v2.22.1 is the latest release and defaults to
`minimal` as described below. Those releases still require their own
configuration instructions; changing an old server's environment does not
install the unified catalog.

v2.22.0 defaults to the five-tool `minimal` profile and has no `standard`. Its
published Compose file does not forward `GOVERNANCE_TOOL_MODE` either, so
selecting a profile there needs the explicit override in the [release
errata](../releases/2.22.0-errata.md). v2.21.0 has an older six-tool `minimal`
profile and registration-time filtering on the HTTP MCP mount. Selecting a
profile on either does not reproduce this surface or its dispatch
compatibility; upgrade the server for those changes.

## 2.2 Bare-metal installation

Follow [`../install/PLAYBOOK.md`](../install/PLAYBOOK.md). It owns the exact
PostgreSQL/AGE/pgvector versions, schema sequence, Python environment, expected
outputs, and failure recovery. Do not copy commands from historical proposals.
The `scripts/install/setup.py` helper only diagnoses and scaffolds this
advanced path; it is not a replacement for the Docker quickstart or playbook.

The production posture uses Redis as the de-facto session and identity store.
The server can boot without it in degraded local-only mode, which is adequate
for the demo but does not preserve production continuity.

## 2.3 Verify and continue

An install is ready when `/health/live` reports alive, the coordination demo
completes its refusal and handoff, the telemetry demo returns six well-formed
decisions, and the dashboard loads. Then continue to
[Running the server](03-running-the-server.md) and
[Integrating agents](04-integrating-agents.md).

Production hardening, bearer rotation, and remote exposure belong in
[Operating](06-operating.md) and the
[operator runbook](../operations/OPERATOR_RUNBOOK.md).

For a shared deployment, replace the Compose-only development Ed25519 seed in
`UNITARES_LEASE_ATTESTATION_SIGNING_KEY`, set a stable
`UNITARES_LEASE_ATTESTATION_ISSUER`, and configure each trusted peer as an
issuer-to-HTTPS `/v1/lease-holder/keys` entry in
`UNITARES_LEASE_TRUSTED_ISSUERS`. Never copy a private seed or continuity token
into the lease plane.

---

[← Overview](01-overview.md) · [Manual index](README.md) · [Next: Running the server →](03-running-the-server.md)
