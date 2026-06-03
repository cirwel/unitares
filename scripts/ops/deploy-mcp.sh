#!/usr/bin/env bash
# Deploy the governance MCP from a DEDICATED clean worktree pinned to
# origin/master — never from a developer working tree.
#
# Why: the running MCP starts via `python src/mcp_server.py` against a checkout
# on disk. If that checkout is the dev tree (as it is by default), it drifts —
# the live fleet brain runs whatever branch/WIP happens to be checked out, so a
# merged fix is NOT actually live until the dev tree happens to be on master and
# restarted. This is the running-process-vs-master-commit drift class
# (feedback_running-process-vs-master-commit.md). deploy-lease-plane.sh (#569)
# closed it for the Elixir lease plane; this is the same move for the Python
# governance MCP. Both services share the one deploy worktree (same repo, master).
#
# What it does, idempotently:
#   1. fast-forward the deploy worktree to origin/master (never destructive)
#   2. repoint the governance-mcp LaunchAgent at the deploy worktree, preserving
#      EVERY env var / secret from the currently-installed plist (paths only are
#      rewritten — secrets are never templated into the repo)
#   3. restart the LaunchAgent and verify /health/ready
#   4. AUTO-ROLLBACK to the previous plist + restart if the new one is not
#      healthy within the budget (model load can take ~30-60s)
#
# Requires the LaunchAgent to have been installed once already.
set -euo pipefail

REPO="${UNITARES_REPO:-$HOME/projects/unitares}"
DEPLOY="${UNITARES_MCP_DEPLOY:-$HOME/projects/unitares-deploy}"
LABEL="com.unitares.governance-mcp"
PLIST="$HOME/Library/LaunchAgents/${LABEL}.plist"
UID_NUM="$(id -u)"
PORT="${UNITARES_MCP_PORT:-8767}"

export PATH="/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:$PATH"

echo "[deploy-mcp] fetching origin/master"
git -C "$REPO" fetch origin master --quiet

if ! git -C "$REPO" worktree list --porcelain | grep -qx "worktree $DEPLOY"; then
  echo "[deploy-mcp] creating dedicated deploy worktree at $DEPLOY (on master)"
  git -C "$REPO" worktree add "$DEPLOY" master
fi

echo "[deploy-mcp] fast-forwarding $DEPLOY to origin/master (ff-only; refuses if it would lose work)"
git -C "$DEPLOY" merge --ff-only origin/master

# The MCP writes logs relative to its WorkingDirectory; ensure the dir exists in
# the deploy worktree (data/ is gitignored).
mkdir -p "$DEPLOY/data/logs"

if [[ ! -f "$PLIST" ]]; then
  echo "[deploy-mcp] $PLIST is not installed — install the LaunchAgent once (see CLAUDE.md setup) before deploying" >&2
  exit 1
fi

# Repoint the plist at the deploy worktree, preserving all env/secrets from the
# currently-installed plist. Idempotent: skip if it already points at $DEPLOY.
# Only the dev-checkout path prefix is rewritten (PYTHONPATH, the script path,
# the log paths, WorkingDirectory); secrets/env values are byte-identical.
if grep -q "$DEPLOY/src/mcp_server.py" "$PLIST"; then
  echo "[deploy-mcp] plist already points at the deploy worktree — no repoint needed"
else
  echo "[deploy-mcp] repointing plist $REPO -> $DEPLOY (backup at ${PLIST}.bak)"
  cp "$PLIST" "${PLIST}.bak"
  sed "s|${REPO}|${DEPLOY}|g" "${PLIST}.bak" > "$PLIST"
fi

echo "[deploy-mcp] restarting $LABEL (gui domain — LaunchAgent, not a system daemon)"
launchctl kickstart -k "gui/$UID_NUM/$LABEL"

echo "[deploy-mcp] verifying /health/ready (bge-m3 model load can take ~30-60s)"
ok=""
for _ in $(seq 1 30); do
  sleep 3
  if curl -fsS -m 4 "http://127.0.0.1:${PORT}/health/ready" 2>/dev/null | grep -q '"status":"ready"'; then
    ok=yes
    break
  fi
done

if [[ "$ok" == yes ]]; then
  echo "[deploy-mcp] OK — governance MCP healthy, serving from $DEPLOY @ $(git -C "$DEPLOY" rev-parse --short HEAD)"
else
  echo "[deploy-mcp] FAILED — MCP did not return /health/ready in budget." >&2
  if [[ -f "${PLIST}.bak" ]] && ! grep -q "$DEPLOY/src/mcp_server.py" "${PLIST}.bak"; then
    echo "[deploy-mcp] rolling back to previous plist (dev checkout) and restarting" >&2
    cp "${PLIST}.bak" "$PLIST"
    launchctl kickstart -k "gui/$UID_NUM/$LABEL"
    echo "[deploy-mcp] rolled back. Investigate ${DEPLOY}/data/logs/mcp_server_error.log" >&2
  fi
  exit 1
fi
