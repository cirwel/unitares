"""Reader-side resolution of the Watcher agent's local state.

The dashboard's watcher panel reads the findings.jsonl that the Watcher agent
(a reference resident under ``agents/watcher``) writes. The server must not
import resident example code — ``agents/`` is documented as non-contract
reference material — so this module owns the reader's copy of the
state-location contract instead of importing ``agents.watcher._util``:

- Shared dir: ``~/.unitares/watcher``, overridable via
  ``UNITARES_WATCHER_DATA_DIR``. Checkout-independent so the writer (pinned to
  the dev checkout by the PostToolUse hook) and the reader (whichever checkout
  serves the live MCP) always agree — see the #595 dashboard-zeroes incident.
- Legacy dir: ``<repo>/data/watcher``, relative to this checkout. Migration
  source only; nothing writes here anymore.

The writer-side counterpart is ``agents.watcher._util``. The two
implementations are deliberately independent (no shared import in either
direction); ``tests/test_http_api_watcher_summary.py``
(``TestFindingsPathMatchesWriter``) imports both and pins that they resolve
the same shared dir, so drift fails CI instead of silently zeroing the panel.
"""

from __future__ import annotations

import os
from pathlib import Path

from src.logging_utils import get_logger

logger = get_logger(__name__)

_REPO_ROOT = Path(__file__).resolve().parent.parent

# Legacy state location — relative to whichever checkout this module loads
# from, matching where pre-#595 Watcher code wrote. Migration source only.
_LEGACY_STATE_DIR = _REPO_ROOT / "data" / "watcher"

# Files that make up Watcher's local state, migrated together.
_STATE_FILES = ("findings.jsonl", "dedup.json", "pattern_floor.json")

_state_dir_cache: Path | None = None
_legacy_migration_done = False


def watcher_state_dir() -> Path:
    """Checkout-independent home for Watcher's local state (reader's view).

    Pure path resolution with no filesystem side effects — call
    :func:`migrate_legacy_watcher_state` to carry forward legacy state.
    """
    global _state_dir_cache
    if _state_dir_cache is not None:
        return _state_dir_cache

    override = os.environ.get("UNITARES_WATCHER_DATA_DIR")
    _state_dir_cache = (
        Path(override).expanduser()
        if override
        else Path.home() / ".unitares" / "watcher"
    )
    return _state_dir_cache


def migrate_legacy_watcher_state() -> None:
    """Copy legacy checkout-relative state into the shared dir if absent there.

    Idempotent and best-effort: runs its filesystem work once per process, and
    a file is only copied when it exists in the legacy dir and not yet in the
    target. The legacy copy is left untouched so an older-code Watcher still
    running mid-rollout is never disrupted.
    """
    global _legacy_migration_done
    if _legacy_migration_done:
        return
    _legacy_migration_done = True

    target = watcher_state_dir()
    legacy = _LEGACY_STATE_DIR
    try:
        if legacy.resolve() == target.resolve() or not legacy.is_dir():
            return
    except OSError:
        return
    try:
        target.mkdir(parents=True, exist_ok=True)
    except OSError:
        return

    import shutil

    for name in _STATE_FILES:
        src = legacy / name
        dst = target / name
        if src.is_file() and not dst.exists():
            try:
                shutil.copy2(src, dst)
                logger.info("migrated watcher state %s from %s to %s", name, legacy, target)
            except OSError as e:
                logger.warning("watcher state migration failed for %s: %s", name, e)


def watcher_findings_path() -> Path:
    """Resolve Watcher's findings.jsonl for the dashboard reader.

    Mid-rollout safety: if the shared findings file is still empty/absent but a
    legacy checkout-relative file has data, read the legacy file. This collapses
    the cutover window where the new reader is live but an old-code Watcher is
    still appending to the legacy dir (which would otherwise leave the panel
    frozen on the migration snapshot). The fallback can be dropped once the
    writer is confirmed on the shared dir.
    """
    migrate_legacy_watcher_state()  # one-time, idempotent
    shared = watcher_state_dir() / "findings.jsonl"
    try:
        if shared.exists() and shared.stat().st_size > 0:
            return shared
        legacy = _LEGACY_STATE_DIR / "findings.jsonl"
        if legacy.exists() and legacy.stat().st_size > 0:
            return legacy
    except OSError:
        pass
    return shared
