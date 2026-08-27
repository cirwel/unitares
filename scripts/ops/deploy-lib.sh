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
#   deploy_lib_restart_service     kickstart when the plist is unchanged since
#                                  the last deploy-driven restart; full RELOAD
#                                  (bootout + bootstrap) when it changed —
#                                  kickstart reuses the cached service
#                                  definition, so plist env edits silently
#                                  never load (bit live 2026-08-27)
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
#   --recipe-handles-reload
#                      the recipe is a self-contained migration command; do not
#                      append the generic unload/load command after it
deploy_lib_require_plist_target() {
  local tag="$1" plist="$2" needle="$3"; shift 3
  local require_exists=0 allow_env="" recipe="" recipe_handles_reload=0
  while [[ $# -gt 0 ]]; do
    case "$1" in
      --require-exists) require_exists=1; shift ;;
      --allow-env) allow_env="$2"; shift 2 ;;
      --recipe) recipe="$2"; shift 2 ;;
      --recipe-handles-reload) recipe_handles_reload=1; shift ;;
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
  if [[ "$recipe_handles_reload" != 1 ]]; then
    echo "[$tag]   launchctl unload \"$plist\" && launchctl load \"$plist\"" >&2
  fi
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
# Does $repo already have a worktree at $deploy?
#
# `git worktree list --porcelain` prints RESOLVED paths, so a literal string
# compare against the caller's argument reports "missing" whenever any parent is
# a symlink — on macOS /tmp -> /private/tmp does exactly this. The old
# `grep -qx "worktree $deploy"` then fell through to `git worktree add` on a
# directory that already existed, and the deploy died on
# `fatal: '<path>' already exists` instead of fast-forwarding. Production dodged
# it only because $HOME/projects has no symlink in it; the lib's own test suite
# runs in /tmp and has been red on this.
#
# Both sides are resolved with `pwd -P` before comparing, so symlinked and
# case-folded paths (Projects vs projects on APFS) match the same worktree.
_deploy_lib_worktree_exists() {
  local repo="$1" deploy="$2" want w found=1
  want="$(cd "$deploy" 2>/dev/null && pwd -P)" || want="$deploy"
  [ -n "$want" ] || want="$deploy"
  # Process substitution keeps the loop in THIS shell, so `found` survives it
  # (a `... | while` pipeline would assign in a subshell and always report 1).
  while IFS= read -r w; do
    w="${w#worktree }"
    [ "$(cd "$w" 2>/dev/null && pwd -P || printf '%s' "$w")" = "$want" ] && { found=0; break; }
  done < <(git -C "$repo" worktree list --porcelain 2>/dev/null | grep '^worktree ')
  return "$found"
}

# usage: deploy_lib_ff_worktree TAG REPO DEPLOY [--detach] [--branch NAME]
#
# --detach creates the worktree on a DETACHED origin/<trunk> instead of the
# `<trunk>` branch. Required for any second dedicated worktree: git allows one
# checkout of a branch across a repo, and `master` is already held by
# unitares-deploy, so a branch-mode `worktree add` fails with "already used by
# worktree at ...". The orchestrator's tree is detached for exactly this reason.
# Fast-forwarding works identically either way (`merge --ff-only` advances a
# detached HEAD), so this only affects the create-if-missing path.
#
# --branch NAME names the trunk when it is not `master` (the default, which is
# every unitares-repo caller). The fleet is not single-repo: the discord-bridge
# lives in cirwel/unitares-discord-bridge, whose trunk is `main`. Before this
# flag the ref was hardcoded three ways in this function, so a `main` repo could
# not use the lib at all and its deploy script would have had to re-inline the
# whole ff block — the exact drift this library exists to prevent. Nothing else
# about the flow changes; this only chooses which ref is fetched, checked out on
# create, and fast-forwarded to.
deploy_lib_ff_worktree() {
  local tag="$1" repo="$2" deploy="$3" detach=0 branch="master"
  shift 3
  while [ $# -gt 0 ]; do
    case "$1" in
      --detach) detach=1 ;;
      --branch)
        shift
        branch="${1:-}"
        [ -n "$branch" ] || { echo "[$tag] deploy_lib_ff_worktree: --branch needs a value" >&2; return 2; }
        ;;
      *) echo "[$tag] deploy_lib_ff_worktree: unknown arg $1" >&2; return 2 ;;
    esac
    shift
  done
  echo "[$tag] fetching origin/$branch"
  git -C "$repo" fetch origin "$branch" --quiet

  DEPLOY_LIB_FRESH=0
  if ! _deploy_lib_worktree_exists "$repo" "$deploy"; then
    if [ "$detach" = 1 ]; then
      echo "[$tag] creating dedicated deploy worktree at $deploy (detached at origin/$branch)"
      git -C "$repo" worktree add --detach "$deploy" "origin/$branch"
    else
      echo "[$tag] creating dedicated deploy worktree at $deploy (on $branch)"
      git -C "$repo" worktree add "$deploy" "$branch"
    fi
    DEPLOY_LIB_FRESH=1
  fi

  DEPLOY_LIB_PREV="$(git -C "$deploy" rev-parse HEAD)"
  echo "[$tag] fast-forwarding $deploy to origin/$branch (ff-only; was ${DEPLOY_LIB_PREV:0:8})"
  git -C "$deploy" merge --ff-only "origin/$branch"
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

# ── Restart with plist-drift detection ───────────────────────────────────────
# `launchctl kickstart -k` restarts the PROCESS but reuses the CACHED service
# definition — a plist edit (EnvironmentVariables above all) silently never
# loads. That bit live on 2026-08-27: a governance env flag was added to the
# plist, the deploy tooling kickstarted, and the flag did not exist in the
# running service. Only a RELOAD (bootout + bootstrap) re-reads the plist.
#
# Detection is a content-hash sidecar, not launchctl-output parsing: after
# every successful deploy-driven restart the plist's sha256 is recorded; a
# mismatch (or no sidecar yet) means the file changed since the service
# definition was last known-loaded, so the restart must be a reload. This is
# uniform for added, changed, AND removed keys, and immune to the launchd-
# injected entries (`OSLogRateLimit` etc.) that make print-output diffing
# false-positive. Known residual: an operator who edits the plist and reloads
# BY HAND leaves the sidecar stale, so the next deploy does one unnecessary
# reload and self-heals — the safe direction.
#
# A MISSING sidecar is not evidence the plist changed — it is evidence there
# is no baseline (fresh machine, first run after rollout, state dir cleaned).
# That case ADOPTS the current hash and kickstarts: absence of state is the
# safe case, never a trigger for the heavy restart. The one-time cost is that
# drift which predates the very first baseline is not caught; any edit after
# it is.
#
# Failure policy — REFUSE, never false-success (the same doctrine as
# deploy_lib_require_plist_target and deploy-mcp.sh's migration gate):
#   - after bootout, WAIT for the label to actually leave the domain
#     (launchd tears down asynchronously; `print` still reports the job,
#     state = SIGTERMed, for seconds — an immediate bootstrap races that
#     teardown and can leave the job unloaded entirely; observed and fixed
#     once already in migrate-openai-gov-proxy.sh). Budget 30s, past the
#     default 20s SIGTERM→SIGKILL grace;
#   - still loaded after the wait (bootout refused / teardown wedged): the
#     reload is impossible — return nonzero WITHOUT touching the service,
#     print the by-hand recipe. The old process keeps running the old code
#     and old definition, a consistent pair; the caller rolls its worktree
#     back so disk does not sit ahead of the running process;
#   - bootstrap is retried (the documented first-bootstrap I/O race); if it
#     never succeeds the service is DOWN — hard error with the recovery
#     command. The caller must also roll back;
#   - after a successful reload, best-effort spot check that every
#     EnvironmentVariables KEY from the plist is present in the loaded
#     definition; a miss WARNS and withholds the sidecar so the next deploy
#     retries — the motivating bug (flag in plist, absent from the running
#     service) stays detectable instead of being latched as applied.
#
# usage: deploy_lib_restart_service TAG DOMAIN LABEL PLIST
#   DOMAIN like "gui/501" (no label); LABEL the service label; PLIST its file.
#   Callers under `set -e` must invoke it in a conditional and roll back their
#   worktree on failure (see deploy-mcp.sh).
# State dir override: UNITARES_DEPLOY_STATE_DIR (default ~/.unitares/deploy-state).

_deploy_lib_sha256() {
  # Portable content hash: macOS ships shasum, Linux CI sha256sum.
  if command -v shasum >/dev/null 2>&1; then
    shasum -a 256 "$1" | awk '{print $1}'
  elif command -v sha256sum >/dev/null 2>&1; then
    sha256sum "$1" | awk '{print $1}'
  else
    # No hash tool: print nothing; caller treats as "cannot determine" and
    # takes the reload path (reload is always-correct, just heavier).
    return 1
  fi
}

# Sidecar write is best-effort BY CONTRACT: at the points it is called the
# restart has already succeeded, so a write failure (unwritable state dir,
# disk full) must demote to "next deploy reloads unnecessarily" — never abort
# a deploy that already worked (a nonzero exit here would also HOLD every
# sibling service in a deploy-apply sweep). Always returns 0.
_deploy_lib_write_sidecar() {
  local tag="$1" state_dir="$2" sidecar="$3" sha="$4"
  [[ -n "$sha" ]] || return 0
  if ! mkdir -p "$state_dir" 2>/dev/null || ! printf '%s' "$sha" > "$sidecar" 2>/dev/null; then
    echo "[$tag] WARNING: could not record the plist-hash sidecar ($sidecar) — the next deploy will do an unnecessary reload." >&2
  fi
  return 0
}

_deploy_lib_label_gone() {
  ! launchctl print "$1" >/dev/null 2>&1
}

# Best-effort post-reload check that the loaded definition carries every
# EnvironmentVariables KEY the plist declares (keys only — one-directional, so
# launchd-injected entries like OSLogRateLimit can never false-positive it).
# Returns 1 only on a confirmed miss; any inability to determine (no python3,
# unparseable plist, empty print output) returns 0 — detection, not a gate.
_deploy_lib_env_keys_loaded() {
  local plist="$1" target="$2" keys out k missing=""
  command -v python3 >/dev/null 2>&1 || return 0
  keys="$(python3 -c 'import plistlib,sys;d=plistlib.load(open(sys.argv[1],"rb"));print("\n".join((d.get("EnvironmentVariables") or {}).keys()))' "$plist" 2>/dev/null || true)"
  [[ -n "$keys" ]] || return 0
  out="$(launchctl print "$target" 2>/dev/null || true)"
  [[ -n "$out" ]] || return 0
  while IFS= read -r k; do
    [[ -n "$k" ]] || continue
    printf '%s' "$out" | grep -F -q -- "$k" || missing="$missing $k"
  done <<< "$keys"
  if [[ -n "$missing" ]]; then
    echo "$missing"
    return 1
  fi
  return 0
}

deploy_lib_restart_service() {
  local tag="$1" domain="$2" label="$3" plist="$4"
  local state_dir sidecar cur_sha old_sha attempt err missing
  state_dir="${UNITARES_DEPLOY_STATE_DIR:-$HOME/.unitares/deploy-state}"
  sidecar="$state_dir/${label}.plist.sha256"
  cur_sha="$(_deploy_lib_sha256 "$plist" 2>/dev/null || true)"
  old_sha="$(cat "$sidecar" 2>/dev/null || true)"

  # No hash tool: drift is undetectable — keep the old, reliable behavior and
  # say so, rather than reloading blind on every deploy.
  if [[ -z "$cur_sha" ]]; then
    echo "[$tag] WARNING: no sha256 tool found — plist drift is NOT being detected; kickstarting." >&2
    launchctl kickstart -k "$domain/$label"
    return $?
  fi

  if [[ -n "$old_sha" && "$cur_sha" == "$old_sha" ]]; then
    echo "[$tag] restarting $label (plist unchanged since last deploy restart — kickstart)"
    launchctl kickstart -k "$domain/$label"
    return $?
  fi

  # A missing baseline is not evidence the plist changed — adopt and kickstart
  # (see the policy comment above). Only a MISMATCH triggers the reload.
  if [[ -z "$old_sha" ]]; then
    echo "[$tag] restarting $label (no plist baseline recorded — adopting the current hash; kickstart)"
    _deploy_lib_write_sidecar "$tag" "$state_dir" "$sidecar" "$cur_sha"
    launchctl kickstart -k "$domain/$label"
    return $?
  fi

  echo "[$tag] restarting $label via RELOAD (plist CHANGED since last deploy restart — kickstart would silently keep the old definition)"

  # A bootout refusal (service not loaded, or launchd declining) is not fatal
  # by itself — the observed loaded-state below decides which path we are on.
  launchctl bootout "$domain/$label" 2>/dev/null || true

  # launchd tears the job down ASYNCHRONOUSLY: `print` keeps reporting it
  # (state = SIGTERMed) for seconds, and a bootstrap issued into that window
  # races the teardown and can leave the job unloaded entirely (observed and
  # first fixed in migrate-openai-gov-proxy.sh). Wait for the label to
  # actually leave the domain: 15 × 2s = 30s, past the default 20s
  # SIGTERM→SIGKILL grace of a plist with no ExitTimeOut.
  if ! deploy_lib_poll 15 2 _deploy_lib_label_gone "$domain/$label"; then
    # Still loaded after the full wait: bootout was refused or teardown is
    # wedged. The reload is impossible right now. REFUSE — do not kickstart a
    # half-torn-down job, do not report success for a deploy whose plist
    # change did not apply (this script's charter is exactly "never report a
    # false success"). The old process keeps running the old code and old
    # definition — a consistent pair — and the caller rolls its worktree back.
    echo "[$tag] REFUSING: $label is still loaded 30s after bootout — the reload cannot proceed." >&2
    echo "[$tag] The plist change did NOT take effect and the deploy did NOT complete. To apply it by hand:" >&2
    echo "[$tag]   launchctl bootout $domain/$label && sleep 2 && launchctl bootstrap $domain \"$plist\"" >&2
    return 1
  fi

  # Known-unloaded: bootstrap re-reads the plist. Retried because the first
  # bootstrap after a bootout has a documented I/O race. Diagnostics are
  # captured, not discarded — a malformed plist or permission error must
  # surface as itself, not as a generic outage.
  err=""
  for attempt in 1 2 3; do
    if err="$(launchctl bootstrap "$domain" "$plist" 2>&1)"; then
      if missing="$(_deploy_lib_env_keys_loaded "$plist" "$domain/$label")"; then
        _deploy_lib_write_sidecar "$tag" "$state_dir" "$sidecar" "$cur_sha"
      else
        echo "[$tag] WARNING: reload succeeded but these plist env keys are absent from the loaded definition:$missing" >&2
        echo "[$tag] WARNING: withholding the plist-hash sidecar so the next deploy retries the reload." >&2
      fi
      return 0
    fi
    if [[ "$attempt" -lt 3 ]]; then
      echo "[$tag] bootstrap attempt $attempt failed (first-bootstrap I/O race is documented — retrying)" >&2
      sleep 2
    fi
  done

  echo "[$tag] FAILED: $label is NOT loaded after bootout and bootstrap keeps failing — the service is DOWN." >&2
  echo "[$tag] last bootstrap error: ${err:-<none captured>}" >&2
  echo "[$tag] Recover with:" >&2
  echo "[$tag]   launchctl bootstrap $domain \"$plist\"" >&2
  return 1
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
