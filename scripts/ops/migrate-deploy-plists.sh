#!/usr/bin/env bash
# One-time migration: repoint the LaunchAgents that still load from the SHARED
# dev checkout (~/projects/unitares) at the dedicated deploy worktree
# (~/projects/unitares-deploy), so deploy-sentinel.sh / deploy-gateway.sh /
# deploy-wave3a.sh / deploy-apply.sh can deploy them safely. After this, those
# services stop being the running-process-vs-master-commit footgun.
#
# This is the one step that MUST run on the Mac: `launchctl` reload needs a
# login/GUI context (an automated/remote shell cannot do it — see deploy-mcp.sh).
# Everything else around it is automated here. Run it once:
#
#   scripts/ops/migrate-deploy-plists.sh            # migrate
#   scripts/ops/migrate-deploy-plists.sh --dry-run  # preview, change nothing
#
# Safe + idempotent:
#   - ensures the deploy worktree exists (on master)
#   - already-migrated services are detected and skipped
#   - each plist is backed up, the path is swapped, and the result is validated
#     with `plutil` BEFORE reload; any failure restores the backup
#   - a service that was running is restarted; one that was stopped (e.g. the
#     operator-gated wave3a-handlers) stays stopped — running state is preserved
set -uo pipefail

REPO="${UNITARES_REPO:-$HOME/projects/unitares}"
DEPLOY="${UNITARES_DEPLOY:-$HOME/projects/unitares-deploy}"
LA="${UNITARES_LAUNCHAGENTS:-$HOME/Library/LaunchAgents}"
UID_NUM="$(id -u)"

DRY_RUN=0
for a in "$@"; do
  case "$a" in
    --dry-run) DRY_RUN=1 ;;
    -h|--help) sed -n '2,20p' "$0"; exit 0 ;;
    *) echo "unknown arg: $a" >&2; exit 2 ;;
  esac
done

export PATH="/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:$PATH"

# Services that load from the dev checkout today (deploy-status.sh restart-DEV).
SERVICES="com.unitares.sentinel-beam com.unitares.gateway-mcp com.unitares.wave3a-handlers"

is_running() {  # label -> 0 if a launchd PID is present
  launchctl list 2>/dev/null | awk -v l="$1" '$3==l && $1!="-"{f=1} END{exit !f}'
}

# Ensure the deploy worktree exists before any plist points at it.
if ! git -C "$REPO" worktree list --porcelain 2>/dev/null | grep -qx "worktree $DEPLOY"; then
  echo "[migrate] deploy worktree $DEPLOY missing — creating it on master"
  if [ "$DRY_RUN" = 1 ]; then
    echo "[migrate] DRY would: git -C $REPO fetch origin master && git -C $REPO worktree add $DEPLOY master"
  else
    git -C "$REPO" fetch origin master --quiet
    git -C "$REPO" worktree add "$DEPLOY" master
  fi
fi

migrated=""; skipped=""; failed=""
for label in $SERVICES; do
  plist="$LA/$label.plist"

  if [ ! -f "$plist" ]; then
    echo "[migrate] SKIP $label — not installed ($plist)"
    skipped="$skipped $label"
    continue
  fi
  if grep -q "$DEPLOY" "$plist"; then
    echo "[migrate] OK   $label — already loads from $DEPLOY"
    skipped="$skipped $label"
    continue
  fi
  if ! grep -q "$REPO" "$plist"; then
    echo "[migrate] SKIP $label — references neither $REPO nor $DEPLOY; inspect manually" >&2
    skipped="$skipped $label"
    continue
  fi

  if [ "$DRY_RUN" = 1 ]; then
    echo "[migrate] DRY  would repoint $label: $REPO -> $DEPLOY, then reload (running=$(is_running "$label" && echo yes || echo no))"
    migrated="$migrated $label(dry)"
    continue
  fi

  was_running=no; is_running "$label" && was_running=yes
  bak="$plist.bak.$(date +%Y%m%d%H%M%S)"
  cp "$plist" "$bak"

  # Swap the dev-checkout path for the deploy worktree. Two forms cover every
  # occurrence: "$REPO/..." (program/working/log paths) and "$REPO<" (a bare
  # path right before the closing </string>). Skip-if-already-migrated above
  # makes this safe to never double-apply (REPO has no "-deploy" suffix).
  sed -e "s|$REPO/|$DEPLOY/|g" -e "s|$REPO<|$DEPLOY<|g" "$bak" > "$plist"

  if ! plutil -lint "$plist" >/dev/null 2>&1; then
    echo "[migrate] FAIL $label — edited plist failed plutil; restoring backup" >&2
    cp "$bak" "$plist"
    failed="$failed $label"
    continue
  fi

  echo "[migrate] reloading $label (unload + load — kickstart won't re-read the plist)"
  launchctl unload "$plist" 2>/dev/null || true
  if ! launchctl load "$plist" 2>/dev/null; then
    echo "[migrate] FAIL $label — launchctl load failed; restoring + reloading the backup" >&2
    cp "$bak" "$plist"
    launchctl unload "$plist" 2>/dev/null || true
    launchctl load "$plist" 2>/dev/null || true
    failed="$failed $label"
    continue
  fi

  # Preserve running state: restart only what was running (respects wave3a's
  # operator-gated "stopped by default" posture).
  if [ "$was_running" = yes ]; then
    launchctl kickstart -k "gui/$UID_NUM/$label" 2>/dev/null || true
  fi

  echo "[migrate] DONE $label — now loads from $DEPLOY (backup: $bak)"
  migrated="$migrated $label"
done

echo
echo "[migrate] summary:"
echo "  migrated:${migrated:-  none}"
echo "  skipped: ${skipped:-  none}"
echo "  failed:  ${failed:-  none}"
echo
echo "[migrate] next: scripts/ops/deploy-apply.sh   # one-command sweep, now total"
[ -z "$failed" ]
