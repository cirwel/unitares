#!/bin/bash
# Start UNITARES MCP Server
# Cloudflare tunnel is managed separately via launchd (com.cloudflare.tunnel.governance)

set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"

cd "$PROJECT_ROOT"

# Host header allowlists for LAN/Cloudflare access (see src/mcp_listen_config.py).
# Defaults are empty — loopback-only. To expose on LAN or via tunnel, set these
# in the caller's environment (e.g. via your LaunchAgent plist) BEFORE invoking
# this script:
#   UNITARES_MCP_ALLOWED_HOSTS="<lan-ip>:*,<hostname>.local,<tunnel-host>"
#   UNITARES_MCP_ALLOWED_ORIGINS="http://<lan-ip>:*,https://<tunnel-host>"
export UNITARES_BIND_ALL_INTERFACES="${UNITARES_BIND_ALL_INTERFACES:-1}"
export UNITARES_MCP_ALLOWED_HOSTS="${UNITARES_MCP_ALLOWED_HOSTS:-}"
export UNITARES_MCP_ALLOWED_ORIGINS="${UNITARES_MCP_ALLOWED_ORIGINS:-}"

# Colors for output
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
RED='\033[0;31m'
NC='\033[0m' # No Color

echo "🚀 Starting UNITARES Governance MCP Server..."

# Check if virtual environment exists and is usable
if [ ! -d ".venv" ]; then
    echo -e "${YELLOW}⚠️  Virtual environment not found. Creating one...${NC}"
    if ! python3 -m venv .venv 2>/dev/null; then
        echo -e "${RED}❌ python3 -m venv failed (ensurepip/venv may be missing).${NC}"
        echo ""
        echo "  Options:"
        echo "    1. Install venv support:  apt install python3-venv  (Debian/Ubuntu)"
        echo "                              dnf install python3-pip    (Fedora/RHEL)"
        echo "                              brew install python@3.14   (macOS)"
        echo "    2. Skip local setup and use remote mode instead:"
        echo "         export UNITARES_HTTP_API_TOKEN=<token>"
        echo "         curl -H 'Authorization: Bearer <token>' https://your-host.example/v1/tools"
        echo ""
        exit 1
    fi
fi

# Verify venv has pip (handles partial/broken venvs)
if [ ! -f ".venv/bin/pip" ] && [ ! -f ".venv/bin/pip3" ]; then
    echo -e "${YELLOW}⚠️  venv exists but pip is missing. Bootstrapping...${NC}"
    if ! .venv/bin/python3 -m ensurepip --upgrade 2>/dev/null; then
        echo -e "${RED}❌ ensurepip failed. Recreate the venv or install pip manually.${NC}"
        echo "    rm -rf .venv && python3 -m venv .venv"
        exit 1
    fi
fi

# Activate virtual environment
source .venv/bin/activate

wait_for_exit() {
    local pattern="$1"
    local attempts="${2:-20}"
    local sleep_seconds="${3:-0.5}"
    local i
    for ((i=0; i<attempts; i++)); do
        if ! pgrep -f "$pattern" > /dev/null; then
            return 0
        fi
        sleep "$sleep_seconds"
    done
    return 1
}

cleanup_stale_markers() {
    if pgrep -f "mcp_server.py" > /dev/null; then
        echo -e "${YELLOW}⚠️  Server process still running; preserving marker files${NC}"
        return
    fi
    rm -f data/.mcp_server.pid data/.mcp_server.lock 2>/dev/null || true
}

# Check if server is already running
if pgrep -f "mcp_server.py" > /dev/null; then
    echo -e "${YELLOW}⚠️  Server appears to be running. Stopping existing instance...${NC}"
    pkill -f "mcp_server.py" || true
    if ! wait_for_exit "mcp_server.py" 10 0.5; then
        echo -e "${YELLOW}⚠️  Force killing existing server...${NC}"
        pkill -9 -f "mcp_server.py" || true
        wait_for_exit "mcp_server.py" 10 0.5 || true
    fi
fi

echo "🧹 Cleaning up stale lock files..."
cleanup_stale_markers

# Start MCP server
echo "📡 Starting MCP server on port 8767..."
nohup python3 src/mcp_server.py --port 8767 --host 0.0.0.0 --force > /tmp/unitares.log 2>&1 &
SERVER_PID=$!

# Wait for server to start
echo "⏳ Waiting for server to start..."
sleep 3

# Check if server started successfully
if ! ps -p $SERVER_PID > /dev/null; then
    echo -e "${RED}❌ Server failed to start. Check logs: tail -f /tmp/unitares.log${NC}"
    exit 1
fi

# Test server connectivity
if curl -s http://localhost:8767/health > /dev/null 2>&1; then
    echo -e "${GREEN}✅ Server is running (PID: $SERVER_PID)${NC}"
else
    echo -e "${YELLOW}⚠️  Server started but health check failed. It may still be warming up.${NC}"
fi

# Display status
echo ""
echo "════════════════════════════════════════════════════════════"
echo "  UNITARES Status"
echo "════════════════════════════════════════════════════════════"
echo "  MCP Server:  http://localhost:8767/mcp/"
echo "  Health:      http://localhost:8767/health"
if [ -n "${CLOUDFLARE_TUNNEL_HOSTNAME:-}" ]; then
    echo "  Tunnel:      https://${CLOUDFLARE_TUNNEL_HOSTNAME}/mcp/"
fi
echo ""
echo "  Logs:"
echo "    Server:    tail -f /tmp/unitares.log"
echo "    Tunnel:    tail -f /tmp/cloudflared-gov.log"
echo ""
echo "  Stop:        ./scripts/stop_unitares.sh"
echo "════════════════════════════════════════════════════════════"
