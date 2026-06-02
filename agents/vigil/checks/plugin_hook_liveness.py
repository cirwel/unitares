"""Plugin-hook liveness check — an external canary for a dark hook chain.

Background: on 2026-06-02 the ``unitares-governance-plugin`` hook chain was
found to have been silently dark for ~2.5 weeks. Every command in the plugin's
``hooks.json`` wrapped ``${CLAUDE_PLUGIN_ROOT}`` in single quotes, which
``/bin/sh`` will not expand, so every hook resolved to a literal path and died
"No such file". ``~/.unitares/checkins.log`` had frozen at 2026-05-16 and
nothing surfaced it — in a project whose whole thesis is observability of agent
behavior, there was no canary asking "are my own hooks even firing?".

This check is that canary. The load-bearing design constraint: it must be
**external to the hook chain**. A heartbeat written *by* the hooks cannot
detect the hooks being un-dispatchable — the exact 2026-06-02 failure was that
the wrapper never ran, so anything it was supposed to touch never got touched.
So this check (running inside Vigil, a separate launchd process) compares two
independent signals:

  * ground truth — Claude Code's own activity record (``~/.claude/history.jsonl``
    advances on every prompt). The plugin cannot suppress this.
  * hook artifact — files the governance hook chain writes when it *dispatches*
    (``~/.unitares/checkins.log`` on send, ``hook-skips.log`` on a gated skip).
    We take the newest mtime across the set, so a healthy-but-skipping chain
    (dispatch OK, check-in gated) still reads as alive — the signal is
    "did a hook run at all", not "did a check-in get recorded".

The check is a **divergence test**, deliberately not a raw staleness threshold:
it only fires when there is recent CC activity *and* every hook artifact is
stale. That distinguishes "hooks are dark" from "operator is simply idle" —
a quiet weekend must not page anyone.

Honest scope notes (no silent caps):
  * The artifact set spans plugin AND user-level (~/.claude/hooks) governance
    hooks, so this answers "is the governance hook layer alive", which is
    slightly broader than "is the plugin chain alive". That is the right
    default — a dark user-level chain is just as worth surfacing.
  * Long single sessions are the known soft spot: an operator who keeps one
    session open for >stale_hours without any hook dispatch could read as dark.
    The default stale window (24h) is wide enough that this is rare; the
    severity is ``warning``, an advisory to verify, not a critical page.
"""

from __future__ import annotations

import os
from typing import Iterable, Optional

from .base import CheckResult

# Ground truth: Claude Code's own per-prompt activity log.
HISTORY_PATH = os.path.expanduser(
    os.environ.get("VIGIL_CC_HISTORY_PATH", "~/.claude/history.jsonl")
)
# Hook artifacts: files the governance hook chain writes when it dispatches.
# Colon-separated; newest mtime across the set is "last hook dispatch".
HOOK_ARTIFACT_PATHS = [
    os.path.expanduser(p)
    for p in os.environ.get(
        "VIGIL_HOOK_ARTIFACT_PATHS",
        "~/.unitares/checkins.log:~/.unitares/hook-skips.log",
    ).split(":")
    if p.strip()
]
# Operator must have been active within this window for the check to assert at
# all — older than this and we cannot distinguish dark hooks from idle.
ACTIVITY_HOURS = float(os.environ.get("VIGIL_HOOK_ACTIVITY_HOURS", "12"))
# A hook artifact older than this (with recent activity) is the divergence.
STALE_HOURS = float(os.environ.get("VIGIL_HOOK_STALE_HOURS", "24"))


def _newest_mtime(paths: Iterable[str]) -> Optional[float]:
    """Newest mtime across the existing paths, or None if none exist."""
    mtimes = []
    for p in paths:
        try:
            mtimes.append(os.path.getmtime(p))
        except OSError:
            continue
    return max(mtimes) if mtimes else None


def assess(
    history_path: str,
    artifact_paths: Iterable[str],
    now: float,
    *,
    activity_hours: float,
    stale_hours: float,
) -> CheckResult:
    """Pure divergence logic — no clock, no real filesystem assumptions.

    Returns ok=True when the chain looks live OR when there is not enough recent
    activity to judge (indeterminate is not a failure). Returns ok=False only on
    the genuine divergence: recent CC activity with a stale/absent hook artifact.
    """
    artifact_paths = list(artifact_paths)
    history_mtime = _newest_mtime([history_path])

    # No ground truth → cannot assert. Don't false-alarm.
    if history_mtime is None:
        return CheckResult(
            ok=True,
            summary="Plugin-hook liveness: indeterminate (no Claude Code activity log found)",
            severity="info",
            fingerprint_key="plugin_hook_liveness_indeterminate",
        )

    activity_age_h = (now - history_mtime) / 3600.0
    if activity_age_h > activity_hours:
        # Operator idle — silence is expected, not a dark chain.
        return CheckResult(
            ok=True,
            summary=(
                f"Plugin-hook liveness: indeterminate (no CC activity in "
                f"{activity_age_h:.1f}h, threshold {activity_hours:.0f}h)"
            ),
            severity="info",
            fingerprint_key="plugin_hook_liveness_indeterminate",
        )

    # There IS recent activity — now the hook chain should have left a trace.
    hook_mtime = _newest_mtime(artifact_paths)

    if hook_mtime is None:
        return CheckResult(
            ok=False,
            summary=(
                f"Plugin hook chain DARK — CC active {activity_age_h:.1f}h ago "
                f"but no hook artifact exists at any of {len(artifact_paths)} path(s). "
                f"Hooks have likely never dispatched."
            ),
            detail={
                "activity_age_hours": round(activity_age_h, 2),
                "artifact_paths": artifact_paths,
                "hook_artifact_mtime": None,
            },
            severity="warning",
            fingerprint_key="plugin_hook_chain_dark",
        )

    hook_age_h = (now - hook_mtime) / 3600.0
    if hook_age_h > stale_hours:
        return CheckResult(
            ok=False,
            summary=(
                f"Plugin hook chain DARK — CC active {activity_age_h:.1f}h ago but "
                f"last hook dispatch was {hook_age_h:.1f}h ago (threshold {stale_hours:.0f}h). "
                f"Check ${{CLAUDE_PLUGIN_ROOT}} expansion / hooks.json quoting."
            ),
            detail={
                "activity_age_hours": round(activity_age_h, 2),
                "hook_age_hours": round(hook_age_h, 2),
                "stale_hours": stale_hours,
            },
            severity="warning",
            fingerprint_key="plugin_hook_chain_dark",
        )

    return CheckResult(
        ok=True,
        summary=(
            f"Plugin hook chain live — last dispatch {hook_age_h:.1f}h ago, "
            f"CC active {activity_age_h:.1f}h ago"
        ),
    )


class PluginHookLiveness:
    name = "plugin_hook_liveness"
    service_key = "governance"

    async def run(self) -> CheckResult:
        # Re-read module-level config so test monkeypatching of paths/thresholds
        # takes effect, matching the pattern in resident_tag_hygiene.
        from . import plugin_hook_liveness as _this
        import time

        return _this.assess(
            _this.HISTORY_PATH,
            _this.HOOK_ARTIFACT_PATHS,
            time.time(),
            activity_hours=_this.ACTIVITY_HOURS,
            stale_hours=_this.STALE_HOURS,
        )
