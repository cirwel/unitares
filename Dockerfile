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
# robustness fix is continuous verdict blending (docs/proposals/continuous-verdict-blending-v0.md).
FROM python:3.14-slim@sha256:63a4c7f612a00f92042cbdcc7cdc6a306f38485af0a200b9c89de7d9b1607d15

WORKDIR /app

# System deps for asyncpg, sentence-transformers, etc.
RUN apt-get update && apt-get install -y --no-install-recommends \
    curl \
    && rm -rf /var/lib/apt/lists/*

# Install Python deps first for better layer caching.
COPY requirements-docker.txt .
RUN pip install --no-cache-dir -r requirements-docker.txt

# Copy application code. governance_core/ was folded back into this repo
# in 2026-04-24 (was previously a separate compiled wheel) — no wheel install.
COPY src/ src/
COPY governance_core/ governance_core/
COPY agents/ agents/
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
