#!/usr/bin/env python3
"""Apply this deployment's environment overlay to a LaunchAgent plist.

The product ships neutral defaults: a setting that only makes sense on one
deployment (a tailnet range the auth bypass should trust, the markers one's
own substrate runtime writes) is read from the environment, empty by default.
This operator's deployment keeps its values in a tracked overlay file beside
this script (``governance-mcp.env``) instead of in hand edits to the live
plist, so nothing has to be set by hand and a fresh machine gets the same
values.

The overlay is ``KEY=VALUE`` lines; blank lines and ``#`` comment lines are
ignored. A comment after a value is refused, not stored as part of it. Each key is written into the plist's ``EnvironmentVariables`` when
it is missing or differs. Keys the overlay does not name are left alone, and
nothing is ever removed: the plist also holds secrets this file must never
see. The write is atomic, keeps the plist's file mode, and goes to the
symlink's target when the plist is a symlink. Rewriting through plistlib
drops the plist's XML comments, so before the first change the original bytes
are copied to a private backup (``--backup-dir``, mode 0600), written once and
never overwritten.

A changed plist is picked up by the deploy's restart: ``deploy-lib.sh``
records the plist's hash after each restart and reloads (rather than
kickstarts) the service when it differs, so a new value is loaded, not only
written.

Only key names are printed, never values: the plist carries tokens, and a
future overlay entry might too.

usage: apply_plist_env_overlay.py --plist PATH --overlay PATH [--dry-run]
exit:  0 applied or already in place; 2 on a bad overlay or plist.
"""
from __future__ import annotations

import argparse
import os
import plistlib
import re
import stat
import sys
import tempfile
from pathlib import Path
from typing import Any

_KEY = re.compile(r"[A-Z][A-Z0-9_]*")


class OverlayError(ValueError):
    """The overlay or the plist cannot be applied as written."""


def parse_overlay(text: str) -> dict[str, str]:
    """``KEY=VALUE`` lines, in order. A value is taken verbatim after the
    first ``=`` (trailing whitespace dropped); quotes are not interpreted."""
    values: dict[str, str] = {}
    for number, raw in enumerate(text.splitlines(), start=1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        key, sep, value = line.partition("=")
        key = key.strip()
        if not sep or not _KEY.fullmatch(key):
            # The line is not echoed: a mistyped key would print its value.
            raise OverlayError(f"line {number}: expected an upper-case KEY=VALUE line")
        if key in values:
            raise OverlayError(f"line {number}: {key} is set twice")
        value = value.rstrip()
        if re.search(r"\s#", value):
            raise OverlayError(f"line {number}: put a comment on its own line, not after {key}'s value")
        if value[:1].isspace():
            raise OverlayError(f"line {number}: {key}'s value starts with whitespace")
        values[key] = value
    return values


def pending_changes(payload: dict[str, Any], overlay: dict[str, str]) -> list[str]:
    """Keys whose value in the plist is missing or differs from the overlay."""
    env = payload.get("EnvironmentVariables") or {}
    if not isinstance(env, dict):
        raise OverlayError("EnvironmentVariables is not a dict")
    return [key for key, value in overlay.items() if env.get(key) != value]


def _write_plist(path: Path, payload: dict[str, Any], *, mode: int) -> None:
    """Atomically replace ``path`` with ``payload``, keeping ``mode``."""
    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temporary = Path(temporary_name)
    try:
        os.fchmod(descriptor, mode)
        with os.fdopen(descriptor, "wb") as handle:
            plistlib.dump(payload, handle, fmt=plistlib.FMT_XML, sort_keys=False)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise


def _fsync_directory(directory: Path) -> None:
    """Make a new directory entry durable: syncing the file does not."""
    descriptor = os.open(directory, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _backup(original: bytes, target: Path, backup_dir: Path) -> Path:
    """Keep the pre-overlay bytes (comments included) where launchd never
    looks, readable only by the owner: the plist carries tokens.

    Written once: a later change would otherwise replace the hand-written
    original with an already-rewritten copy. An existing backup is kept. The
    bytes go to a synced temporary file first and only a complete copy is
    linked to the final name (atomically, failing if it exists), so a full
    disk or an interrupted write never leaves a truncated "original". The
    directory is synced before returning, so the plist is never rewritten
    ahead of a backup that a power loss could still take back."""
    backup_dir.mkdir(parents=True, exist_ok=True)
    path = backup_dir / f"{target.name}.pre-overlay"
    if path.exists():
        return path
    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=backup_dir)
    temporary = Path(temporary_name)
    try:
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(original)
            handle.flush()
            os.fsync(handle.fileno())
        try:
            os.link(temporary, path)
        except FileExistsError:
            pass  # another run published one first; keep it
        _fsync_directory(backup_dir)  # the new name must outlive a crash
    finally:
        temporary.unlink(missing_ok=True)
    return path


def apply_overlay(plist: Path, overlay_path: Path, *, dry_run: bool = False,
                  backup_dir: Path | None = None) -> list[str]:
    """Write the overlay's values into ``plist``; return the keys changed."""
    overlay = parse_overlay(overlay_path.read_text())
    plist = plist.resolve()  # write the file a symlink points at, keep the link
    try:
        with plist.open("rb") as handle:
            payload = plistlib.load(handle)
    except (OSError, plistlib.InvalidFileException, ValueError) as exc:
        raise OverlayError(f"cannot read {plist}: {exc}") from exc
    if not isinstance(payload, dict):
        raise OverlayError(f"{plist} is not a dict plist")
    changed = pending_changes(payload, overlay)
    if changed and not dry_run:
        if backup_dir is not None:
            _backup(plist.read_bytes(), plist, backup_dir)
        env = payload.setdefault("EnvironmentVariables", {})
        for key in changed:
            env[key] = overlay[key]
        _write_plist(plist, payload, mode=stat.S_IMODE(plist.stat().st_mode))
    return changed


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--plist", type=Path, required=True)
    parser.add_argument("--overlay", type=Path, required=True)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--backup-dir", type=Path,
                        help="copy the plist's original bytes here before changing it")
    args = parser.parse_args(argv)
    try:
        changed = apply_overlay(args.plist, args.overlay, dry_run=args.dry_run,
                                backup_dir=args.backup_dir)
    except (OverlayError, OSError) as exc:
        print(f"[env-overlay] cannot apply {args.overlay}: {exc}", file=sys.stderr)
        return 2
    verb = "would set" if args.dry_run else "set"
    if changed:
        print(f"[env-overlay] {verb} in {args.plist.name}: {', '.join(changed)}")
    else:
        print(f"[env-overlay] {args.plist.name} already carries the overlay")
    return 0


if __name__ == "__main__":
    sys.exit(main())
