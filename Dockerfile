# UNITARES Governance MCP Server
#
# This image is intended to be run via the top-level docker-compose.yml,
# which wires it up to the postgres-age and redis services.
#
# Build standalone (rare):
#   docker build -t unitares-governance .
#
# Run standalone (requires external Postgres+AGE):
#   docker run -p 8767:8767 \
#     -e DB_POSTGRES_URL=postgresql://... \
#     -e UNITARES_BIND_ALL_INTERFACES=1 \
#     unitares-governance

# Pinned by digest, not just the floating `3.14-slim` tag: the governance core
# is a numpy ODE, and a Docker Hub rebuild of the tag (new libm/BLAS) can shift
# float results enough to move a verdict near a threshold. Dependabot-docker is
# configured to bump this digest with the Docker Quickstart job validating each
# bump (see .github/dependabot.yml). This is the reproducibility *bridge* — the
# robustness fix is continuous verdict blending (docs/proposals/archive/continuous-verdict-blending-v0.md).
FROM python:3.14-slim@sha256:cad9a2c871761c413caa6fdd6441c783451e740a48aaeba60ae62a8b53525ef6

WORKDIR /app

# System deps for asyncpg, sentence-transformers, etc.
RUN apt-get update && apt-get install -y --no-install-recommends \
    curl \
    && rm -rf /var/lib/apt/lists/*

# Install Python deps first for better layer caching. constraints.txt pins
# drift-bitten deps to the production resolution so published images cannot
# silently pick up a newer release than the fleet runs.
COPY requirements-docker.txt constraints.txt ./
RUN pip install --no-cache-dir -r requirements-docker.txt -c constraints.txt

# Copy application code. governance_core/ was folded back into this repo
# in 2026-04-24 (was previously a separate compiled wheel) — no wheel install.
COPY src/ src/
COPY governance_core/ governance_core/
COPY agents/ agents/
# The server imports unitares_sdk (src/lease_plane re-exports its lease-plane
# client). It ships in agents/sdk; install it so the import resolves. Its
# dependencies (httpx, mcp, pydantic) are already installed above.
#
# Narrowing this COPY to only the subpackages src/ imports directly looked
# safe (agents/sdk, agents/common) but review caught a live regression each
# of the first two times: agents.common.resolution_outcome (the
# reference-residents route pack's POST /v1/sentinel/adjudicate) and, next
# round, agents/chronicler/metrics_catalog.json — a path .env.example
# documents mounting straight from inside this image via
# UNITARES_METRICS_CATALOG_EXTRA. Both are real, supported, opt-in
# integration points that a static import/grep sweep does not surface, and a
# third pass is not a reason for confidence there isn't a fourth. Reference
# residents genuinely aren't held to the fleet-neutrality bar (see
# AGENTS.md/CLAUDE.md), so their defaults being in the image is an accepted
# tradeoff, not a credential leak; re-attempt narrowing only with an
# end-to-end test of every documented UNITARES_ROUTE_PACKS /
# UNITARES_DIALECTIC_ORCHESTRATED_REVIEW / UNITARES_METRICS_CATALOG_EXTRA
# path, not another static sweep.
RUN pip install --no-cache-dir --no-deps ./agents/sdk
COPY config/ config/
COPY dashboard/ dashboard/
COPY skills/ skills/
COPY VERSION .

EXPOSE 8767

# Bind 0.0.0.0 inside the container so the host port mapping works. The
# server still does host/origin allowlisting via UNITARES_MCP_ALLOWED_HOSTS.
ENV UNITARES_BIND_ALL_INTERFACES=1
ENV UNITARES_MCP_ALLOWED_HOSTS=localhost,127.0.0.1
ENV UNITARES_MCP_ALLOWED_ORIGINS=http://localhost:8767,http://127.0.0.1:8767

CMD ["python", "src/mcp_server.py", "--host", "0.0.0.0", "--port", "8767", "--force"]
