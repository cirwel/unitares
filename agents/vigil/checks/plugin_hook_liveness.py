"""Detect divergence between a configured client's activity and hook dispatch.

Both the independent activity file and the hook artifact paths are explicit
configuration. No client directory is inspected by default. With recent
activity, stale or absent artifacts produce an advisory; with old or missing
activity, the result is indeterminate. A healthy gated skip can count as a
hook dispatch by including its artifact in the configured set.

The activity source must be independent of the hooks it checks. The newest
artifact mtime represents last dispatch, rather than last submitted check-in.
A long active session without any dispatch can still produce an advisory;
thresholds describe the observation window, not proof of a broken client.

Provenance: the 2026-06-02 Claude Code adapter incident exposed a quoted
${CLAUDE_PLUGIN_ROOT} that prevented hooks from dispatching for weeks. Comparing
its independent history file against hook artifacts detected that failure.
The same divergence mechanism accepts any explicitly configured client.
"""

from __future__ import annotations

import os
from typing import Iterable, Optional

from .base import CheckResult

# Optional activity source and hook artifacts. No default client directories
# are inspected. The older VIGIL_CC_HISTORY_PATH remains an explicit adapter
# alias for existing deployments; VIGIL_ACTIVITY_PATH takes precedence.
HISTORY_PATH = os.path.expanduser(
    os.environ.get("VIGIL_ACTIVITY_PATH", os.environ.get("VIGIL_CC_HISTORY_PATH", ""))
)
HOOK_ARTIFACT_PATHS = [
    os.path.expanduser(p.strip())
    for p in os.environ.get("VIGIL_HOOK_ARTIFACT_PATHS", "").split(":")
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
    the genuine divergence: recent client activity with a stale/absent hook artifact.
    """
    artifact_paths = list(artifact_paths)
    if not history_path or not artifact_paths:
        return CheckResult(
            ok=True,
            summary="Plugin-hook liveness: not configured (set activity and artifact paths)",
            severity="info",
            fingerprint_key="plugin_hook_liveness_indeterminate",
        )
    history_mtime = _newest_mtime([history_path])

    # No ground truth → cannot assert. Don't false-alarm.
    if history_mtime is None:
        return CheckResult(
            ok=True,
            summary="Plugin-hook liveness: indeterminate (no activity log found)",
            severity="info",
            fingerprint_key="plugin_hook_liveness_indeterminate",
        )

    activity_age_h = (now - history_mtime) / 3600.0
    if activity_age_h > activity_hours:
        # Operator idle — silence is expected, not a dark chain.
        return CheckResult(
            ok=True,
            summary=(
                f"Plugin-hook liveness: indeterminate (no client activity in "
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
                f"Plugin hook chain DARK — client active {activity_age_h:.1f}h ago "
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
                f"Plugin hook chain DARK — client active {activity_age_h:.1f}h ago but "
                f"last hook dispatch was {hook_age_h:.1f}h ago (threshold {stale_hours:.0f}h). "
                f"Check the configured hook dispatcher and artifact paths."
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
            f"client active {activity_age_h:.1f}h ago"
        ),
    )


class PluginHookLiveness:
    name = "plugin_hook_liveness"
    service_key = "plugin_hooks"

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
