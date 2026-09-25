#!/usr/bin/env bash
# sync-plugin-skills.sh — propagate canonical skill content to the plugin bundle.
#
# unitares/skills/ is the source of truth (S15-b, 2026-04-25). The plugin
# bundle at unitares-governance-plugin/skills/ must be a byte-identical
# mirror so Codex consumers see the same content as Claude Code consumers
# (until S15-c lands, at which point Claude Code adapter fetches from the
# server-side `skills` MCP tool directly).
#
# This script clobbers plugin/skills/ with unitares/skills/ contents.
# Refuses to run if the plugin working tree is dirty inside skills/ — those
# changes belong on plugin first or to be folded into unitares canonical.
#
# It also WRITES plugin/skills/SKILLS_MANIFEST.sha256, the canonical
# fingerprint the plugin's CI parity gate checks the mirror against. unitares
# does not commit that file (it is derived data, and committing it made every
# pair of PRs touching any two skills conflict on its aggregate line); it is
# computed here, from canonical, by scripts/dev/skills_manifest.py.
#
# Usage:
#   ./scripts/dev/sync-plugin-skills.sh                         # default plugin path
#   UNITARES_PLUGIN_REPO=/path/to/plugin ./scripts/dev/sync-plugin-skills.sh
#   ./scripts/dev/sync-plugin-skills.sh --check                 # diff-only, exit 1 on mismatch
#
# Both modes also compare the plugin's freshness checker with canonical's
# attestation rule (scripts/dev/check_plugin_attestation_rule.py). The plugin
# keeps its own copy of that rule because it cannot import unitares, and a
# copy drifts silently. Disagreement fails --check (exit 1) and, in apply
# mode, is reported after the mirror is written (exit 5): the skills still
# sync, but the checker needs a port that this script cannot make.
#
# Environment:
#   UNITARES_PLUGIN_REPO  — path to unitares-governance-plugin checkout.
#                           Default: $(git rev-parse --show-toplevel)/../unitares-governance-plugin

set -euo pipefail

UNITARES_ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
DEFAULT_PLUGIN="${UNITARES_ROOT}/../unitares-governance-plugin"
PLUGIN_REPO="${UNITARES_PLUGIN_REPO:-$DEFAULT_PLUGIN}"
SRC="${UNITARES_ROOT}/skills"
DST="${PLUGIN_REPO}/skills"

CHECK_ONLY=0
if [[ "${1:-}" == "--check" ]]; then
    CHECK_ONLY=1
fi

if [[ ! -d "$PLUGIN_REPO" ]]; then
    if [[ "$CHECK_ONLY" == 1 ]]; then
        echo "[sync-plugin-skills] plugin repo not found at $PLUGIN_REPO — skipping parity check"
        echo "[sync-plugin-skills] (set UNITARES_PLUGIN_REPO to enable)"
        exit 0
    fi
    echo "[sync-plugin-skills] plugin repo not found at $PLUGIN_REPO" >&2
    echo "[sync-plugin-skills] set UNITARES_PLUGIN_REPO to point at your unitares-governance-plugin checkout" >&2
    exit 2
fi

if [[ ! -d "$SRC" ]]; then
    echo "[sync-plugin-skills] source missing: $SRC" >&2
    exit 2
fi

MANIFEST_NAME="SKILLS_MANIFEST.sha256"
MANIFEST_TOOL="${UNITARES_ROOT}/scripts/dev/skills_manifest.py"

# Attestation-rule parity. Computed once, reported at every exit below that
# would otherwise claim success. `set -e` would abort on the check's non-zero
# exit before the status could be read, so the capture is unguarded.
set +e
RULE_OUT=$(python3 "${UNITARES_ROOT}/scripts/dev/check_plugin_attestation_rule.py" "$PLUGIN_REPO" 2>&1)
RULE_STATUS=$?
set -e

# Prints the parity result; returns 1 only on a real disagreement. A checker
# that cannot be loaded is a warning: this is a drift detector, not a guard
# against data loss, so it does not block the skills sync.
report_rule_parity() {
    case "$RULE_STATUS" in
        0) return 0 ;;
        1)
            echo "[sync-plugin-skills] plugin freshness checker disagrees with canonical's attestation rule:" >&2
            echo "$RULE_OUT" | sed 's/^/  /' >&2
            echo "[sync-plugin-skills] port src/skill_attestations.py THE RULE into $PLUGIN_REPO/scripts/_check_freshness.py" >&2
            return 1 ;;
        *)
            echo "[sync-plugin-skills] warning: could not compare the plugin's attestation rule (exit $RULE_STATUS):" >&2
            echo "$RULE_OUT" | sed 's/^/  /' >&2
            return 0 ;;
    esac
}

# Diff first — same operation either way. The manifest is compared separately:
# it exists only on the mirror side, and what matters is that it matches the
# fingerprint computed from canonical now.
DIFF_OUT=$(diff -rq -x "$MANIFEST_NAME" "$SRC" "$DST" 2>&1 || true)
if ! python3 "$MANIFEST_TOOL" --skills-dir "$SRC" --verify "$DST/$MANIFEST_NAME" >/dev/null 2>&1; then
    MANIFEST_DRIFT="$DST/$MANIFEST_NAME is missing or does not match canonical's fingerprint"
    DIFF_OUT="${DIFF_OUT:+$DIFF_OUT
}$MANIFEST_DRIFT"
fi

if [[ -z "$DIFF_OUT" ]]; then
    echo "[sync-plugin-skills] in sync — nothing to do"
    if ! report_rule_parity; then
        [[ "$CHECK_ONLY" == 1 ]] && exit 1
        exit 5
    fi
    exit 0
fi

if [[ "$CHECK_ONLY" == 1 ]]; then
    echo "[sync-plugin-skills] plugin bundle out of sync with canonical:"
    echo "$DIFF_OUT" | sed 's/^/  /'
    echo
    echo "[sync-plugin-skills] run: ./scripts/dev/sync-plugin-skills.sh"
    report_rule_parity || true
    exit 1
fi

# Apply mode — refuse if plugin/skills/ has uncommitted changes
if git -C "$PLUGIN_REPO" diff --quiet -- skills/ && git -C "$PLUGIN_REPO" diff --quiet --cached -- skills/; then
    : # clean — proceed
else
    echo "[sync-plugin-skills] plugin/skills/ has uncommitted changes — refusing to clobber" >&2
    git -C "$PLUGIN_REPO" status --short -- skills/ | sed 's/^/  /' >&2
    echo "[sync-plugin-skills] resolve plugin-side changes first (commit, stash, or fold into unitares canonical)" >&2
    exit 3
fi

# Direction guard — the uncommitted-changes check above only catches a DIRTY
# mirror. A mirror that was edited and COMMITTED is indistinguishable from a
# stale one: rsync overwrites it, the script prints "done", and a later run
# reports "in sync — nothing to do". The revert leaves no signal anywhere.
#
# Observed 2026-08-09: plugin/skills/discord-bridge carried last_verified
# 2026-08-02 with two extra source_files; canonical was still at 2026-07-28.
# A plain sync would have silently rolled that back. Caught by eye, which is
# not a control.
#
# `last_verified` is the right signal because it is a DECLARED verification
# date, not a filesystem timestamp — it survives checkout, rsync and worktree
# creation, all of which destroy mtime (see the --checksum note below for how
# badly mtime behaves here).
#
# A date alone cannot order an EQUAL-date difference, and that case is common:
# a mirror produced by this script inherits canonical's date verbatim, so any
# canonical edit later the same day lands both sides on one date with different
# content. The guard settles those against canonical's git history, by ORDER:
# it proceeds only when canonical's current content entered history strictly
# after the mirror's content last appeared there. Mirror-side work canonical
# never had, and content canonical reverted away from, both still refuse.
#
# The rule and its two corrected bugs (`>` vs `>=`, and failing open on a
# missing date) live in scripts/dev/skills_direction_guard.py, which is a
# module rather than a heredoc precisely so the rule can be tested — see
# tests/test_skills_direction_guard.py. A guard that has already shipped one
# off-by-one is not one to leave uncovered.
# `set -e` would abort on the guard's non-zero refusal exit before the status
# could be read, so the capture is deliberately unguarded and re-armed after.
set +e
REGRESSIONS=$(python3 "${UNITARES_ROOT}/scripts/dev/skills_direction_guard.py" "$SRC" "$DST")
GUARD_STATUS=$?
set -e
if [[ "$GUARD_STATUS" != 0 && "$GUARD_STATUS" != 4 ]]; then
    echo "[sync-plugin-skills] direction guard failed to run (exit $GUARD_STATUS) — refusing" >&2
    exit "$GUARD_STATUS"
fi
if [[ "$GUARD_STATUS" == 4 ]]; then
    echo "[sync-plugin-skills] REFUSING — cannot show canonical is newer for:" >&2
    echo "$REGRESSIONS" | sed 's/^/  /' >&2
    echo >&2
    echo "[sync-plugin-skills] Syncing would revert a verification that already happened." >&2
    echo "[sync-plugin-skills] Forward-port into canonical first, then re-run:" >&2
    echo "[sync-plugin-skills]   cp $DST/<skill>/SKILL.md $SRC/<skill>/SKILL.md" >&2
    echo "[sync-plugin-skills]   cp $DST/.attestations/<skill>/<file>.json $SRC/.attestations/<skill>/   # for an attestation" >&2
    exit 4
fi

echo "[sync-plugin-skills] mirroring $SRC → $DST"
# rsync: --delete to drop plugin-only skills (canonical is authoritative);
# preserve only file content, not perms/owners (cross-repo is a portability concern).
#
# --checksum is load-bearing, not belt-and-braces. rsync's default quick check
# is size + mtime. When both checkouts are created close together — e.g. two
# `git worktree add` calls in the same session — git stamps identical mtimes,
# so a same-size edit is concluded "unchanged" and survives stale. Observed
# 2026-07-28 mirroring #1394, on the then-committed fixed-size manifest.
#
# The manifest is excluded from the copy (anchored to the top level, so a
# stray local regeneration in canonical is never mirrored and the mirror's
# copy is not deleted by --delete) and written fresh from canonical below.
rsync -a --checksum --delete --exclude="/$MANIFEST_NAME" "$SRC/" "$DST/"
python3 "$MANIFEST_TOOL" --skills-dir "$SRC" --output "$DST/$MANIFEST_NAME"

echo "[sync-plugin-skills] done. Plugin status:"
git -C "$PLUGIN_REPO" status --short -- skills/ | sed 's/^/  /'
echo
echo "[sync-plugin-skills] next: cd $PLUGIN_REPO && commit + push the mirror update"
if ! report_rule_parity; then
    exit 5
fi
