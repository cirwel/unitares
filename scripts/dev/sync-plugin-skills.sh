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
# Usage:
#   ./scripts/dev/sync-plugin-skills.sh                         # default plugin path
#   UNITARES_PLUGIN_REPO=/path/to/plugin ./scripts/dev/sync-plugin-skills.sh
#   ./scripts/dev/sync-plugin-skills.sh --check                 # diff-only, exit 1 on mismatch
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

# Diff first — same operation either way.
DIFF_OUT=$(diff -rq "$SRC" "$DST" 2>&1 || true)

if [[ -z "$DIFF_OUT" ]]; then
    echo "[sync-plugin-skills] in sync — nothing to do"
    exit 0
fi

if [[ "$CHECK_ONLY" == 1 ]]; then
    echo "[sync-plugin-skills] plugin bundle out of sync with canonical:"
    echo "$DIFF_OUT" | sed 's/^/  /'
    echo
    echo "[sync-plugin-skills] run: ./scripts/dev/sync-plugin-skills.sh"
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
# The comparison is `>=`, not `>`, and that is the whole point. An EQUAL date
# with DIFFERENT content is the more common hazard, and the first version of
# this guard let it through: 2026-08-09, plugin #112 merged the mirror's content
# ahead of canonical while both sides still read last_verified 2026-07-28,
# because the content PR and the freshness PR were split. A `>` test sees equal
# dates and waves it past — straight into the revert it exists to prevent.
#
# Equal date + differing content means somebody edited one side without bumping,
# and the script cannot tell which side is right. Refusing is correct: the cost
# of a false refusal is one forward-port command, the cost of a false pass is
# silently deleting merged work.
REGRESSIONS=$(python3 - "$SRC" "$DST" <<'PY'
import pathlib, re, sys
src, dst = (pathlib.Path(p) for p in sys.argv[1:3])
pat = re.compile(r'^last_verified:\s*"?([\d-]+)"?', re.M)
block = []
def verified(p):
    try:
        m = pat.search(p.read_text(encoding="utf-8"))
    except OSError:
        return None
    return m.group(1) if m else None
for mirror in sorted(dst.glob("*/SKILL.md")):
    canon = src / mirror.parent.name / "SKILL.md"
    if not canon.exists():
        continue
    if canon.read_bytes() == mirror.read_bytes():
        continue
    cv, mv = verified(canon), verified(mirror)
    if cv and mv and mv >= cv:
        rel = "newer than" if mv > cv else "same date as, but differs from"
        block.append(f"{mirror.parent.name}: mirror ({mv}) is {rel} canonical ({cv})")
print("\n".join(block))
PY
)
if [[ -n "$REGRESSIONS" ]]; then
    echo "[sync-plugin-skills] REFUSING — the mirror is NEWER than canonical for:" >&2
    echo "$REGRESSIONS" | sed 's/^/  /' >&2
    echo >&2
    echo "[sync-plugin-skills] Syncing would revert a verification that already happened." >&2
    echo "[sync-plugin-skills] Forward-port into canonical first, then re-run:" >&2
    echo "[sync-plugin-skills]   cp $DST/<skill>/SKILL.md $SRC/<skill>/SKILL.md" >&2
    echo "[sync-plugin-skills]   python3 scripts/dev/skills_manifest.py" >&2
    exit 4
fi

echo "[sync-plugin-skills] mirroring $SRC → $DST"
# rsync: --delete to drop plugin-only skills (canonical is authoritative);
# preserve only file content, not perms/owners (cross-repo is a portability concern).
#
# --checksum is load-bearing, not belt-and-braces. rsync's default quick check
# is size + mtime, and SKILLS_MANIFEST.sha256 is fixed-size (same seven
# hash lines, same aggregate line length) so its size never changes when its
# contents do. When both checkouts are created close together — e.g. two
# `git worktree add` calls in the same session — git stamps identical mtimes,
# rsync concludes "unchanged", and the stale manifest survives while the
# SKILL.md files update around it. That lands the mirror in exactly the state
# the plugin #80 parity gate exists to catch, and it is timing-dependent, so
# it reproduces intermittently. Observed 2026-07-28 mirroring #1394.
rsync -a --checksum --delete "$SRC/" "$DST/"

echo "[sync-plugin-skills] done. Plugin status:"
git -C "$PLUGIN_REPO" status --short -- skills/ | sed 's/^/  /'
echo
echo "[sync-plugin-skills] next: cd $PLUGIN_REPO && commit + push the mirror update"
