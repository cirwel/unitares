#!/bin/bash
# Start the Governance MCP Server (multi-client support)
#
# Usage:
#   ./scripts/start_server.sh [--port PORT] [--host HOST]
#
# Default: http://127.0.0.1:8767/mcp
#
# Key Environment Variables (set in .env):
#   DB_BACKEND=postgres           - Database backend
#   UNITARES_I_DYNAMICS=linear    - v4.2-P: Prevents I-channel boundary saturation
#   UNITARES_KNOWLEDGE_BACKEND=postgres - Canonical KG backend

set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"

cd "$PROJECT_ROOT"

# Load environment from .env if it exists
# Uses set -a to auto-export and handles comments safely
if [ -f ".env" ]; then
    set -a
    while IFS='=' read -r key value; do
        # Skip comments and empty lines
        [[ "$key" =~ ^#.*$ ]] && continue
        [[ -z "$key" ]] && continue
        # Remove leading/trailing whitespace from key
        key=$(echo "$key" | xargs)
        [[ -z "$key" ]] && continue
        # Export the variable
        export "$key=$value"
    done < .env
    set +a
fi

# Check for Python
PYTHON="${PYTHON:-python3}"
if ! command -v "$PYTHON" &> /dev/null; then
    echo "Error: Python not found. Set PYTHON env var or install Python 3."
    exit 1
fi

# Dependencies: do NOT auto-install at runtime (avoids surprise failures / permission issues)
if ! "$PYTHON" -c "import mcp, uvicorn, starlette" 2>/dev/null; then
    echo "Error: missing dependencies for HTTP server."
    echo ""
    echo "Install minimal (stdio only):"
    echo "  pip install -r requirements-core.txt"
    echo ""
    echo "Install full (HTTP):"
    echo "  pip install -r requirements-full.txt"
    echo ""
    # Check if the specific blocker is the private package
    if ! "$PYTHON" -c "import governance_core" 2>/dev/null; then
        echo "Note: unitares-core (governance_core) is a private package not on PyPI."
        echo "  Install from wheel:  pip install /path/to/unitares_core-*.whl"
        echo "  Or use remote mode:  export UNITARES_HTTP_API_TOKEN=<token>"
        echo "                       curl -H 'Authorization: Bearer <token>' https://your-host.example/v1/tools"
        echo ""
    fi
    exit 1
fi

echo "Starting Governance MCP Server from $PROJECT_ROOT..."
exec "$PYTHON" src/mcp_server.py "$@"
