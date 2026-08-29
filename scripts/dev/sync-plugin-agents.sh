#!/usr/bin/env bash
# sync-plugin-agents.sh — propagate canonical Claude Code subagent definitions
# to the plugin bundle.
#
# Top-level Markdown files under unitares/agents/ (currently
# governance-reviewer.md; README.md is excluded) are the canonical subagent
# definitions — same source-of-truth posture as skills (S15-b, see
# sync-plugin-skills.sh). Claude Code auto-discovers <plugin>/agents/, so
# mirroring the files is all that shipping takes: no plugin.json change.
#
# Differences from the skills sync, on purpose:
#   - No --delete: the plugin may carry adapter-only agents of its own; this
#     script only asserts that the canonical definitions are present and
#     current, not that the directory is an exact mirror.
#   - No last_verified direction guard (agent definitions carry no declared
#     verification date). Instead, a diverged COMMITTED plugin copy is refused
#     with a diff — direction cannot be proven, so the operator decides:
#     fold the plugin-side edit into canonical first, or pass --force.
#
# Usage:
#   ./scripts/dev/sync-plugin-agents.sh            # mirror canonical -> plugin
#   ./scripts/dev/sync-plugin-agents.sh --check    # diff-only, exit 1 on drift
#   ./scripts/dev/sync-plugin-agents.sh --force    # overwrite a diverged committed copy
#
# Environment:
#   UNITARES_PLUGIN_REPO  — path to unitares-governance-plugin checkout.
#                           Default: $(git rev-parse --show-toplevel)/../unitares-governance-plugin

set -euo pipefail

UNITARES_ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
DEFAULT_PLUGIN="${UNITARES_ROOT}/../unitares-governance-plugin"
PLUGIN_REPO="${UNITARES_PLUGIN_REPO:-$DEFAULT_PLUGIN}"
SRC_DIR="${UNITARES_ROOT}/agents"
DST_DIR="${PLUGIN_REPO}/agents"

CHECK_ONLY=0
FORCE=0
case "${1:-}" in
    --check) CHECK_ONLY=1 ;;
    --force) FORCE=1 ;;
    "") ;;
    *) echo "[sync-plugin-agents] unknown flag: $1 (use --check or --force)" >&2; exit 2 ;;
esac

if [[ ! -d "$PLUGIN_REPO" ]]; then
    if [[ "$CHECK_ONLY" == 1 ]]; then
        echo "[sync-plugin-agents] plugin repo not found at $PLUGIN_REPO — skipping parity check"
        echo "[sync-plugin-agents] (set UNITARES_PLUGIN_REPO to enable)"
        exit 0
    fi
    echo "[sync-plugin-agents] plugin repo not found at $PLUGIN_REPO" >&2
    echo "[sync-plugin-agents] set UNITARES_PLUGIN_REPO to point at your unitares-governance-plugin checkout" >&2
    exit 2
fi

# Canonical set: top-level *.md under agents/, minus README.md. The Python
# resident packages beneath agents/ are reference implementations, not part
# of the adapter bundle, and are never mirrored.
SRC_FILES=()
for f in "$SRC_DIR"/*.md; do
    [[ -f "$f" ]] || continue
    [[ "$(basename "$f")" == "README.md" ]] && continue
    SRC_FILES+=("$f")
done

if [[ "${#SRC_FILES[@]}" -eq 0 ]]; then
    echo "[sync-plugin-agents] no canonical agent definitions under $SRC_DIR — nothing to do"
    exit 0
fi

DRIFT=0
for src in "${SRC_FILES[@]}"; do
    name="$(basename "$src")"
    dst="$DST_DIR/$name"

    if [[ -f "$dst" ]] && cmp -s "$src" "$dst"; then
        continue
    fi
    DRIFT=1

    if [[ "$CHECK_ONLY" == 1 ]]; then
        if [[ -f "$dst" ]]; then
            echo "[sync-plugin-agents] out of sync: agents/$name"
        else
            echo "[sync-plugin-agents] missing from plugin: agents/$name"
        fi
        continue
    fi

    # Refuse to clobber uncommitted plugin-side edits — those belong on the
    # plugin first or folded into canonical, never silently overwritten.
    if ! git -C "$PLUGIN_REPO" diff --quiet -- "agents/$name" 2>/dev/null || \
       ! git -C "$PLUGIN_REPO" diff --quiet --cached -- "agents/$name" 2>/dev/null; then
        echo "[sync-plugin-agents] plugin agents/$name has uncommitted changes — refusing to clobber" >&2
        echo "[sync-plugin-agents] resolve plugin-side changes first (commit, stash, or fold into unitares canonical)" >&2
        exit 3
    fi

    # A diverged COMMITTED copy is ambiguous: it may be newer than canonical,
    # and overwriting would silently revert it (the failure mode the skills
    # sync guards with last_verified). Show the divergence and let the
    # operator pick a direction.
    if [[ -f "$dst" && "$FORCE" != 1 ]]; then
        echo "[sync-plugin-agents] REFUSING — plugin agents/$name diverges from canonical:" >&2
        diff -u "$dst" "$src" | sed 's/^/  /' >&2 || true
        echo >&2
        echo "[sync-plugin-agents] If the plugin copy is newer, fold it into canonical first:" >&2
        echo "[sync-plugin-agents]   cp $dst $src" >&2
        echo "[sync-plugin-agents] If canonical is newer, re-run with --force." >&2
        exit 4
    fi

    mkdir -p "$DST_DIR"
    cp "$src" "$dst"
    echo "[sync-plugin-agents] mirrored agents/$name"
done

if [[ "$DRIFT" == 0 ]]; then
    echo "[sync-plugin-agents] in sync — nothing to do"
    exit 0
fi

if [[ "$CHECK_ONLY" == 1 ]]; then
    echo
    echo "[sync-plugin-agents] run: ./scripts/dev/sync-plugin-agents.sh"
    exit 1
fi

echo "[sync-plugin-agents] done. Plugin status:"
git -C "$PLUGIN_REPO" status --short -- agents/ | sed 's/^/  /'
echo
echo "[sync-plugin-agents] next: cd $PLUGIN_REPO && commit + push the mirror update"
