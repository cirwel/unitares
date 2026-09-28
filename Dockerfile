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
# The server imports unitares_sdk (src/lease_plane re-exports its lease-plane
# client). It ships in agents/sdk, a standalone package with no path
# dependency on its agents/ siblings; install it so the import resolves.
# Its dependencies (httpx, mcp, pydantic) are already installed above.
#
# Only these three agents/ subpackages are copied, not the whole tree:
#   - agents/sdk: unitares_sdk, installed below.
#   - agents/common: src/http_routes/sentinel.py imports
#     agents.common.resolution_outcome to serve POST /v1/sentinel/adjudicate,
#     mounted when UNITARES_ROUTE_PACKS includes "reference-residents".
#   - agents/dialectic_reviewer: the spawn target when
#     UNITARES_DIALECTIC_ORCHESTRATED_REVIEW is enabled (see
#     src/mcp_handlers/dialectic/orchestrator_dispatch.py, which runs it via
#     this image's own interpreter at this same repo root).
# All three are self-contained (no import of a chronicler/vigil/sentinel/
# watcher/... sibling), so the reference residents' deployment-specific
# defaults (GITHUB_SCRAPE_ORG, VIGIL_STALLED_PR_OWNER, ...) stay out of the
# image while both opt-in features keep working.
COPY agents/sdk/ agents/sdk/
COPY agents/common/ agents/common/
COPY agents/dialectic_reviewer/ agents/dialectic_reviewer/
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
