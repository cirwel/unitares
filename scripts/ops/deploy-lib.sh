# deploy-lib.sh — shared building blocks for the per-service deploy scripts.
#
# SOURCED, never executed. Each deploy-*.sh keeps its own linear story (compile
# step, verify probe, rollback policy — the genuinely service-specific parts)
# and sources this file for the blocks that used to be pasted five times and
# had already drifted between copies:
#
#   deploy_lib_acquire_lock        serialize deploys of the shared worktree
#   deploy_lib_require_plist_target  refuse when the LaunchAgent still loads
#                                    from the dev checkout (false-success guard)
#   deploy_lib_ff_worktree         fetch + create-if-missing + ff-only merge
#   deploy_lib_nudge_lease_plane   #1277 fix 1: restart the plane when the
#                                  shared worktree moves under it
#   deploy_lib_poll                bounded retry loop for verify probes
#
# CONTRACT (enforced by scripts/dev/check-deploy-lib.sh):
#   - The lock-key derivation below must stay byte-identical to the historical
#     per-script copies. During any window where an old-copy script and a
#     lib-based script coexist on an operator machine (partial pull, deploy
#     worktree mid-ff), mutual exclusion only holds if both derive the SAME
#     lock path from the same DEPLOY value.
#   - macOS stock bash is 3.2: no associative arrays, no ${var,,}, and empty
#     arrays trip `set -u` without the ${arr[@]+"${arr[@]}"} guard.
#   - Every function is safe under the callers' `set -euo pipefail`.
#
# Globals published (read-only for callers):
#   DEPLOY_LIB_LOCK_DIR   lock path held by this process
#   DEPLOY_LIB_PREV       worktree HEAD before the ff (rollback target)
#   DEPLOY_LIB_FRESH      1 if ff_worktree had to CREATE the worktree
#                         (gitignored deps/_build are gone — see #1277)

if [[ "${BASH_SOURCE[0]}" == "$0" ]]; then
  echo "deploy-lib.sh is a library — source it from a deploy script, do not run it" >&2
  exit 2
fi

# ── Serialize deploys (shared worktree) ──────────────────────────────────────
# All per-service scripts fast-forward the SAME deploy worktree, and rollback
# paths run `git reset --hard $PREV` — two deploys at once race the git index,
# and a rollback can revert a parallel deploy to a stale commit (silent
# regression + a false "OK"). macOS has no flock(1), so guard with an atomic
# mkdir lock keyed to the worktree path (shared key ON PURPOSE across every
# service that shares the worktree), reclaiming it only if the holder process
# is dead. Override via UNITARES_DEPLOY_LOCK.
#
# usage: deploy_lib_acquire_lock TAG DEPLOY_PATH
# Installs an EXIT trap to release the lock; callers must not overwrite the
# EXIT trap afterwards (append to deploy_lib_release_lock instead if needed).
deploy_lib_acquire_lock() {
  local tag="$1" deploy="$2" holder
  DEPLOY_LIB_LOCK_DIR="${UNITARES_DEPLOY_LOCK:-${TMPDIR:-/tmp}/unitares-deploy$(printf '%s' "$deploy" | tr -c 'A-Za-z0-9' '_').lock}"
  if ! mkdir "$DEPLOY_LIB_LOCK_DIR" 2>/dev/null; then
    holder="$(cat "$DEPLOY_LIB_LOCK_DIR/pid" 2>/dev/null || echo '?')"
    if [[ "$holder" != '?' ]] && ! kill -0 "$holder" 2>/dev/null; then
      echo "[$tag] reclaiming stale deploy lock (holder PID $holder is dead): $DEPLOY_LIB_LOCK_DIR" >&2
      rm -rf "$DEPLOY_LIB_LOCK_DIR"
      mkdir "$DEPLOY_LIB_LOCK_DIR" 2>/dev/null || { echo "[$tag] lost a lock race — another deploy just started; refusing" >&2; exit 1; }
    else
      echo "[$tag] another deploy is in progress (lock: $DEPLOY_LIB_LOCK_DIR, holder PID $holder) — refusing to run concurrently" >&2
      exit 1
    fi
  fi
  printf '%s' "$$" > "$DEPLOY_LIB_LOCK_DIR/pid"
  trap deploy_lib_release_lock EXIT
}

deploy_lib_release_lock() {
  [[ -n "${DEPLOY_LIB_LOCK_DIR:-}" ]] && rm -rf "$DEPLOY_LIB_LOCK_DIR"
}

# ── Pre-flight: the LaunchAgent must load from the deploy worktree ───────────
# `launchctl kickstart` restarts the process but does NOT re-read the plist, so
# if the plist still points at the dev checkout, a kickstart restarts the OLD
# code and the health probe passes against it — the false-success failure mode
# this preflight exists to prevent. Changing WHERE a service runs from is a
# one-time operator-interactive RELOAD (unload + load from a login shell).
#
# usage: deploy_lib_require_plist_target TAG PLIST NEEDLE [opts]
#   --require-exists   a missing plist is a hard error (default: missing passes,
#                      for services whose install is optional on a machine)
#   --allow-env VAR    escape hatch: VAR=1 downgrades the refusal to a warning
#                      (default: no escape hatch — hard refuse on mismatch)
#   --recipe TEXT      extra stderr guidance printed on refusal (the service's
#                      one-time migration recipe)
deploy_lib_require_plist_target() {
  local tag="$1" plist="$2" needle="$3"; shift 3
  local require_exists=0 allow_env="" recipe=""
  while [[ $# -gt 0 ]]; do
    case "$1" in
      --require-exists) require_exists=1; shift ;;
      --allow-env) allow_env="$2"; shift 2 ;;
      --recipe) recipe="$2"; shift 2 ;;
      *) echo "[$tag] deploy_lib_require_plist_target: unknown option $1" >&2; exit 2 ;;
    esac
  done

  if [[ ! -f "$plist" ]]; then
    if [[ "$require_exists" == 1 ]]; then
      echo "[$tag] $plist not installed — install the LaunchAgent first (see CLAUDE.md setup)" >&2
      exit 1
    fi
    return 0
  fi

  grep -q "$needle" "$plist" && return 0

  echo "[$tag] REFUSING: the LaunchAgent plist does not point at the deploy worktree ($needle)." >&2
  echo "[$tag] kickstart would restart the OLD code and this script would lie about success." >&2
  echo "[$tag] One-time migration (interactive login shell — a RELOAD, kickstart won't re-read the plist):" >&2
  if [[ -n "$recipe" ]]; then
    printf '%s\n' "$recipe" >&2
  fi
  echo "[$tag]   launchctl unload \"$plist\" && launchctl load \"$plist\"" >&2
  if [[ -n "$allow_env" ]]; then
    if [[ "${!allow_env:-0}" == "1" ]]; then
      echo "[$tag] WARNING: $allow_env=1 set — restarting the dev checkout anyway (this deploy will NOT take effect on the deploy worktree)." >&2
      return 0
    fi
    echo "[$tag] Refusing (set $allow_env=1 to restart the dev checkout anyway)." >&2
    exit 2
  fi
  exit 1
}

# ── Fetch + worktree-create-if-missing + ff-only ─────────────────────────────
# Never a destructive reset: ff-only refuses if it would lose work. Publishes
# DEPLOY_LIB_PREV (the rollback target) and DEPLOY_LIB_FRESH (worktree was just
# created, so gitignored deps/ + _build/ are GONE while a running BEAM keeps
# serving in-RAM modules — the 06-27 ~5.4h fail-open, #1277).
#
# usage: deploy_lib_ff_worktree TAG REPO DEPLOY_PATH
deploy_lib_ff_worktree() {
  local tag="$1" repo="$2" deploy="$3"
  echo "[$tag] fetching origin/master"
  git -C "$repo" fetch origin master --quiet

  DEPLOY_LIB_FRESH=0
  if ! git -C "$repo" worktree list --porcelain | grep -qx "worktree $deploy"; then
    echo "[$tag] creating dedicated deploy worktree at $deploy (on master)"
    git -C "$repo" worktree add "$deploy" master
    DEPLOY_LIB_FRESH=1
  fi

  DEPLOY_LIB_PREV="$(git -C "$deploy" rev-parse HEAD)"
  echo "[$tag] fast-forwarding $deploy to origin/master (ff-only; was ${DEPLOY_LIB_PREV:0:8})"
  git -C "$deploy" merge --ff-only origin/master
}

# ── #1277 fix 1: restart the lease plane at disturbance time ─────────────────
# The ff above may have moved the SHARED worktree under the running BEAM
# (RAM-vs-disk drift). Nudge the plane now, not at first failure. Best-effort
# (`|| true` semantics): a failed nudge is a loud warning, never a deploy
# abort — the acquire-healthcheck auto-restart (#1284) remains the backstop.
# Must be called after deploy_lib_ff_worktree (uses its published globals).
#
# usage: deploy_lib_nudge_lease_plane TAG SCRIPT_NAME DEPLOY_PATH
deploy_lib_nudge_lease_plane() {
  local tag="$1" script_name="$2" deploy="$3" ops_dir
  ops_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
  if [[ "${DEPLOY_LIB_FRESH:-0}" == 1 ]]; then
    "$ops_dir/nudge-lease-plane.sh" --reason "$script_name: deploy worktree re-created (deps/_build gone)" || true
  else
    "$ops_dir/nudge-lease-plane.sh" --reason "$script_name: shared-worktree ff" \
      --if-changed "$DEPLOY_LIB_PREV" "$(git -C "$deploy" rev-parse HEAD)" || true
  fi
}

# ── Bounded verify poll ──────────────────────────────────────────────────────
# usage: deploy_lib_poll ATTEMPTS INTERVAL_SECONDS CMD [ARGS...]
# Runs CMD every INTERVAL seconds up to ATTEMPTS times; returns 0 on the first
# success, 1 if it never succeeds. CMD is typically a verify function defined
# in the calling script (the probes are genuinely service-specific).
deploy_lib_poll() {
  local attempts="$1" interval="$2" i
  shift 2
  i=0
  while [[ "$i" -lt "$attempts" ]]; do
    sleep "$interval"
    if "$@"; then
      return 0
    fi
    i=$((i + 1))
  done
  return 1
}
