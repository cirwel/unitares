#!/usr/bin/env bash
# Deploy the BEAM agent orchestrator (com.unitares.agent-orchestrator, :8789)
# from its DEDICATED worktree ~/projects/unitares-orchestrator.
#
# Why this service was outside the sweep until now: it is the one BEAM service
# NOT pinned to unitares-deploy. That is deliberate — pinning it there would
# couple every orchestrator restart to gov-mcp's migration cadence — but the
# cost was that deploy-status.sh never listed it and deploy-apply.sh could not
# reach it, so its drift was invisible and every deploy was hand-rolled from
# memory. This script brings it under the same lock / plist-preflight / ff
# contract as the other five.
#
# THREE THINGS THIS SERVICE DOES DIFFERENTLY — all load-bearing:
#
# 1. RESTART ONLY WHEN ITS OWN CODE CHANGED. The worktree tracks all of master,
#    but the orchestrator only runs elixir/agent_orchestrator, and that subdir
#    has not changed since 2026-06-29 while the tree drifted 200+ commits. A
#    restart is genuine risk here (see 3) and "N commits behind" is NOT evidence
#    the orchestrator needs one. So the ff always runs — keeping `behind` a real
#    signal instead of permanent noise — but the restart is gated on the subdir
#    actually differing. --force overrides.
#
# 2. bootout + bootstrap, NOT `kickstart -k`. The plist carries the worktree
#    path in ProgramArguments/WorkingDirectory and env in EnvironmentVariables;
#    kickstart reuses the loaded service definition, so a plist edit silently
#    does not take. Documented footgun: the FIRST bootstrap often fails with an
#    I/O error leaving the service DOWN — retrying returns 0. This script
#    retries rather than leaving a dead orchestrator behind.
#
# 3. /health is bearer-gated, so an unauthenticated 401 IS the success signal.
#    Verifying with `curl -f` would report a healthy service as broken. The
#    check stays unauthenticated on purpose — a deploy script should not need a
#    secret to confirm a process is serving.
#
# No lease-plane nudge: that exists because ff-ing the SHARED unitares-deploy
# tree moves source under other running BEAM nodes. This worktree is this
# service's alone, so there is nothing else to disturb.
set -euo pipefail

REPO="${UNITARES_REPO:-$HOME/projects/unitares}"
DEPLOY="${UNITARES_ORCHESTRATOR_DEPLOY:-$HOME/projects/unitares-orchestrator}"
APP_SUBDIR="elixir/agent_orchestrator"
LABEL="com.unitares.agent-orchestrator"
PLIST="${UNITARES_ORCHESTRATOR_PLIST:-$HOME/Library/LaunchAgents/$LABEL.plist}"
PORT="${AGENT_ORCHESTRATOR_HTTP_PORT:-8789}"
UID_NUM="$(id -u)"
TAG="deploy-orch"

FORCE=0
SKIP_TESTS=0
# --apply-migrations: apply pending DB migrations as part of the deploy (or set
# UNITARES_DEPLOY_APPLY_MIGRATIONS=1). Default is detect-and-refuse on a gap,
# matching deploy-mcp.sh — DDL stays a deliberate, opt-in action.
APPLY_MIGRATIONS="${UNITARES_DEPLOY_APPLY_MIGRATIONS:-0}"
for a in "$@"; do
  case "$a" in
    --force) FORCE=1 ;;
    --skip-tests) SKIP_TESTS=1 ;;
    --apply-migrations) APPLY_MIGRATIONS=1 ;;
    -h|--help) sed -n '2,40p' "$0"; exit 0 ;;
    *) echo "unknown arg: $a" >&2; exit 2 ;;
  esac
done

export PATH="/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:$PATH"

# shellcheck source=deploy-lib.sh
. "$(cd "$(dirname "$0")" && pwd)/deploy-lib.sh"

deploy_lib_acquire_lock "$TAG" "$DEPLOY"

# The plist must already point at the dedicated worktree. Refuse loudly rather
# than deploy a tree the running service does not load from — restarting the
# orchestrator while it loads from somewhere else is exactly the false-success
# ("deployed!" but the old code is still serving) these preflights exist for.
deploy_lib_require_plist_target "$TAG" "$PLIST" "$DEPLOY/$APP_SUBDIR" \
  --require-exists \
  --allow-env UNITARES_ORCHESTRATOR_ALLOW_DEV \
  --recipe "[$TAG]   /usr/libexec/PlistBuddy -c \"Set :WorkingDirectory $DEPLOY/$APP_SUBDIR\" \"$PLIST\"
[$TAG]   (then launchctl bootout + bootstrap — kickstart will NOT re-read it)"

# --detach: `master` is checked out in unitares-deploy, and git permits one
# branch checkout per repo, so this tree must be detached at origin/master.
deploy_lib_ff_worktree "$TAG" "$REPO" "$DEPLOY" --detach
PREV="$DEPLOY_LIB_PREV"
NOW="$(git -C "$DEPLOY" rev-parse HEAD)"

# Did the orchestrator's OWN code move? Empty diff => the running node is
# already on this code and a restart buys nothing but risk. The SDK path dep
# (elixir/unitares_sdk, mix.exs) compiles INTO this node — an SDK-only
# classifier fix without it here would leave the live service on stale code.
CODE_CHANGED=0
if [ "$PREV" != "$NOW" ] && [ -n "$(git -C "$DEPLOY" diff --name-only "$PREV" "$NOW" -- "$APP_SUBDIR" elixir/unitares_sdk 2>/dev/null)" ]; then
  CODE_CHANGED=1
fi

if [ "$CODE_CHANGED" = 0 ] && [ "$FORCE" = 0 ]; then
  echo "[$TAG] $APP_SUBDIR unchanged ${PREV:0:8}..${NOW:0:8} — worktree is current, NOT restarting."
  echo "[$TAG] (the orchestrator's code has been frozen since 2026-06-29; commit distance on the"
  echo "[$TAG]  shared history is not evidence it is stale. Use --force to restart anyway.)"
  # The lock releases via deploy-lib's EXIT trap; do not release it by hand.
  exit 0
fi

# ── Migration preflight (gate, not silent) ───────────────────────────────────
# This service is deliberately NOT pinned to the shared unitares-deploy worktree
# (see the header) — but being outside that worktree removed it from
# deploy-mcp.sh's migration gate WITHOUT removing its dependency on the
# governance DB schema. On 2026-08-28 a sweep restarted the orchestrator on code
# expecting orchestration.spawn_idempotency (migration 068, #1942) while that
# table did not exist; deploy-mcp.sh refused for the same gap in the same run,
# so gov-mcp was protected and this service was not. Migration 068 exists
# specifically to serve orchestrator code, so orchestrator changes and
# governance-DB migrations will keep arriving together.
#
# The check runs from THIS worktree's own manifest against the live DB, so the
# decoupling is preserved: it verifies the database it actually talks to, not
# gov-mcp's release cadence.
MIGRATE="$DEPLOY/scripts/dev/apply_migrations.py"
MIGRATE_DBURL=()
[[ -n "${UNITARES_DEPLOY_DB_URL:-}" ]] && MIGRATE_DBURL=(--db-url "$UNITARES_DEPLOY_DB_URL")
# Expand with ${arr[@]+"${arr[@]}"}: on macOS bash 3.2 a bare "${empty[@]}"
# trips `set -u` and aborts the deploy (the #951/#960 footgun).
if [[ -f "$MIGRATE" ]]; then
  echo "[$TAG] migration preflight: is the live DB in sync with this worktree's manifest?"
  if ! python3 "$MIGRATE" --check "${MIGRATE_DBURL[@]+"${MIGRATE_DBURL[@]}"}"; then
    if [[ "$APPLY_MIGRATIONS" == 1 ]]; then
      echo "[$TAG] applying pending migrations (operator opt-in) BEFORE restart"
      if ! python3 "$MIGRATE" --apply "${MIGRATE_DBURL[@]+"${MIGRATE_DBURL[@]}"}" \
         || ! python3 "$MIGRATE" --check "${MIGRATE_DBURL[@]+"${MIGRATE_DBURL[@]}"}"; then
        echo "[$TAG] FAILED — migrations did not reach sync; NOT restarting." >&2
        exit 1
      fi
    else
      echo "[$TAG] REFUSING: the live DB is not in sync with this worktree's migration manifest." >&2
      echo "[$TAG] Restarting now would bring up code expecting an unapplied schema (the 068 case above)." >&2
      echo "[$TAG] The orchestrator keeps running its current code — a consistent pair — and this deploy did NOT complete." >&2
      echo "[$TAG] Then either apply the gap and re-deploy:" >&2
      echo "[$TAG]     python3 $MIGRATE --apply" >&2
      echo "[$TAG]   or re-run this deploy with migrations applied automatically:" >&2
      echo "[$TAG]     $0 --apply-migrations" >&2
      exit 1
    fi
  fi
fi

echo "[$TAG] compiling (surfaces a build error while the OLD node is still serving)"
( cd "$DEPLOY/$APP_SUBDIR" && mix deps.get && mix compile )
if [ "$SKIP_TESTS" = 0 ]; then
  echo "[$TAG] running the orchestrator test suite before touching the live node"
  ( cd "$DEPLOY/$APP_SUBDIR" && mix test )
fi

# bootout+bootstrap, with the documented retry. A first-attempt I/O error leaves
# the service DOWN, so a single unguarded bootstrap can end a "successful"
# deploy with no orchestrator running at all.
echo "[$TAG] restarting $LABEL (bootout + bootstrap — kickstart would reuse the old service definition)"
launchctl bootout "gui/$UID_NUM/$LABEL" 2>/dev/null || true
if ! launchctl bootstrap "gui/$UID_NUM" "$PLIST" 2>/dev/null; then
  echo "[$TAG] first bootstrap failed (the documented I/O footgun) — retrying"
  sleep 2
  launchctl bootstrap "gui/$UID_NUM" "$PLIST" 2>/dev/null \
    || launchctl load -w "$PLIST" 2>/dev/null \
    || { echo "[$TAG] FAILED — orchestrator did not bootstrap; it is DOWN. Recover with:" >&2
         echo "[$TAG]     launchctl bootstrap gui/$UID_NUM $PLIST" >&2
         exit 1; }
fi

# Verify: registered with launchd AND answering on the port. 401 is the healthy
# answer for this bearer-gated endpoint (see header note 3).
verify_orchestrator() {
  launchctl list 2>/dev/null | grep -q "$LABEL" || return 1
  local code
  code="$(curl -s -m 2 -o /dev/null -w '%{http_code}' "http://127.0.0.1:$PORT/health" 2>/dev/null)" || return 1
  case "$code" in 200|401|403) return 0 ;; *) return 1 ;; esac
}

if deploy_lib_poll 20 3 verify_orchestrator; then
  echo "[$TAG] OK — orchestrator serving on :$PORT from $DEPLOY @ $(git -C "$DEPLOY" rev-parse --short HEAD)"
else
  echo "[$TAG] FAILED — orchestrator did not answer on :$PORT after restart." >&2
  echo "[$TAG] Check: launchctl list | grep $LABEL ; tail ~/Library/Logs/unitares-agent-orchestrator.log" >&2
  exit 1
fi
