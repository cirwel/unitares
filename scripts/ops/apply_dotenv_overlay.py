#!/usr/bin/env python3
"""Apply a reviewed deployment environment overlay to a Docker Compose .env file.

Companion to ``scripts/ops/apply_plist_env_overlay.py`` for Linux / Docker
Compose hosts where ``governance-mcp`` reads configuration from ``.env`` instead
of a macOS LaunchAgent plist.

The overlay uses the same strict ``KEY=VALUE`` grammar and optional
``--expected-sha256`` byte-level verification as ``apply_plist_env_overlay.py``.
Keys not named in the overlay (such as database passwords and bearer tokens in
``.env``) and comment/blank lines are preserved in place; missing overlay keys
are appended at the end. When every managed key already has its target value and
``.env`` already has mode ``0600``, ``.env`` is not rewritten.

Optional ``--rewrite-prefix FROM=TO`` flags translate host-specific directory
prefixes in overlay values (for example, rewriting a workstation config
directory to a container bind-mount path) AFTER verifying ``--expected-sha256``
against the untouched reviewed overlay bytes. Optional ``--omit-key KEY`` flags
skip named keys when staging a partial rollout.

Writes are atomic, preserve symlinks by writing the resolved target, enforce
mode ``0600`` because Compose ``.env`` files hold secrets, and optionally save
the pre-overlay bytes once under ``--backup-dir`` (mode ``0600``). Only key
names are printed, never values.

usage: apply_dotenv_overlay.py --dotenv PATH --overlay PATH [--dry-run]
       apply_dotenv_overlay.py --overlay PATH --expected-sha256 HASH --validate-overlay
exit:  0 applied or already in place; 2 on a bad overlay or .env; 3 with --check when changes or mode tightening are pending.
"""
from __future__ import annotations

import argparse
import errno
import importlib.util
import os
import re
import stat
import sys
import tempfile
from pathlib import Path

_PLIST_OVERLAY_PATH = Path(__file__).resolve().with_name("apply_plist_env_overlay.py")
_spec = importlib.util.spec_from_file_location("_apply_plist_env_overlay", _PLIST_OVERLAY_PATH)
if _spec is None or _spec.loader is None:  # pragma: no cover - defensive import guard
    raise RuntimeError(f"cannot load {_PLIST_OVERLAY_PATH}")
_plist_overlay = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_plist_overlay)

OverlayError = _plist_overlay.OverlayError
parse_overlay = _plist_overlay.parse_overlay
read_overlay = _plist_overlay.read_overlay

_KEY = re.compile(r"[A-Z][A-Z0-9_]*")
_DOTENV_KEY = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
_NEEDS_SINGLE_QUOTES = re.compile(r"[\s#$\"\\]")
_TOLERATED_DIR_FSYNC_ERRNOS = {
    errno.EACCES,
    errno.EPERM,
    errno.EINVAL,
    errno.EROFS,
    errno.EBADF,
}


def _has_forbidden_controls(text: str) -> bool:
    return any(
        (ord(ch) < 32 and ch not in "\t\r\n") or ord(ch) in {0xFFFE, 0xFFFF}
        for ch in text
    )


def parse_rewrite_prefixes(raw_pairs: list[str] | None) -> list[tuple[str, str]]:
    """Validate ``FROM=TO`` absolute path prefix rewrites, ordered longest-FROM first."""
    pairs: list[tuple[str, str]] = []
    seen_sources: set[str] = set()
    for item in raw_pairs or []:
        src, sep, dst = item.partition("=")
        if not sep or not src or not dst:
            raise OverlayError("--rewrite-prefix requires non-empty FROM=TO")
        if not src.startswith("/") or not dst.startswith("/"):
            raise OverlayError("--rewrite-prefix paths must be absolute")
        src_norm = src.rstrip("/") or "/"
        dst_norm = dst.rstrip("/") or "/"
        if src_norm == "/":
            raise OverlayError("--rewrite-prefix FROM cannot be '/'")
        if src_norm in seen_sources:
            raise OverlayError(f"--rewrite-prefix FROM '{src_norm}' is specified more than once")
        seen_sources.add(src_norm)
        pairs.append((src_norm, dst_norm))
    pairs.sort(key=lambda pair: len(pair[0]), reverse=True)
    return pairs


def parse_omit_keys(raw_keys: list[str] | None) -> set[str]:
    omitted: set[str] = set()
    for key in raw_keys or []:
        cleaned = key.strip()
        if not _KEY.fullmatch(cleaned):
            raise OverlayError("--omit-key requires an upper-case environment variable name")
        omitted.add(cleaned)
    return omitted


def transform_overlay(
    overlay: dict[str, str],
    *,
    rewrite_prefixes: list[tuple[str, str]] | None = None,
    omit_keys: set[str] | None = None,
) -> dict[str, str]:
    """Apply key omissions and path-boundary prefix rewrites to parsed overlay values."""
    omitted = omit_keys or set()
    prefixes = sorted(rewrite_prefixes or [], key=lambda pair: len(pair[0]), reverse=True)
    transformed: dict[str, str] = {}
    for key, value in overlay.items():
        if key in omitted:
            continue
        rewritten = value
        for src, dst in prefixes:
            if rewritten == src:
                rewritten = dst
                break
            if rewritten.startswith(src + "/"):
                prefix = "" if dst == "/" else dst
                rewritten = prefix + rewritten[len(src):]
                break
        if _has_forbidden_controls(rewritten) or "\n" in rewritten or "\r" in rewritten:
            raise OverlayError(f"{key}'s value contains a control character")
        if "'" in rewritten:
            raise OverlayError(f"{key}'s value contains a single quote, unsupported in Compose .env")
        transformed[key] = rewritten
    return transformed


def format_dotenv_assignment(key: str, value: str) -> str:
    """Serialize ``KEY=VALUE`` safely for Docker Compose ``.env`` without ``$`` expansion."""
    if not _KEY.fullmatch(key):
        raise OverlayError("invalid overlay key name")
    if "'" in value or "\n" in value or "\r" in value or _has_forbidden_controls(value):
        raise OverlayError(f"{key}'s value cannot be safely encoded in .env")
    if _NEEDS_SINGLE_QUOTES.search(value):
        return f"{key}='{value}'"
    return f"{key}={value}"


def _decode_dotenv_value(raw_value: str, *, location: str) -> tuple[str, bool]:
    """Return ``(decoded_value, canonical_encoding_safe)``.

    Explicitly parses single- and double-quoted spans (including trailing inline
    ``# comments``) and rejects unterminated quotes or stray trailing text.
    """
    lstripped = raw_value.lstrip()
    if lstripped.startswith("'"):
        end = lstripped.find("'", 1)
        if end == -1:
            raise OverlayError(f"{location}: unterminated single-quoted value")
        inner = lstripped[1:end]
        trailing = lstripped[end + 1:].strip()
        if trailing and not trailing.startswith("#"):
            raise OverlayError(f"{location}: unexpected characters after single-quoted value")
        return inner, True

    if lstripped.startswith('"'):
        idx = 1
        has_escape = False
        end = -1
        while idx < len(lstripped):
            ch = lstripped[idx]
            if ch == "\\":
                has_escape = True
                idx += 2
                continue
            if ch == '"':
                end = idx
                break
            idx += 1
        if end == -1:
            raise OverlayError(f"{location}: unterminated double-quoted value")
        inner = lstripped[1:end]
        trailing = lstripped[end + 1:].strip()
        if trailing and not trailing.startswith("#"):
            raise OverlayError(f"{location}: unexpected characters after double-quoted value")
        canonical = not has_escape and not any(ch in inner for ch in ("$", "`", '"', "'"))
        return inner, canonical

    uncommented = re.split(r"\s+#", raw_value, maxsplit=1)[0].strip()
    if any(ch in uncommented for ch in ("$", "'", '"')):
        return uncommented, False
    return uncommented, True


def parse_dotenv_structure(text: str, source: Path) -> tuple[list[str], dict[str, tuple[int, str, bool]]]:
    """Parse ``.env`` lines while keeping comments and unmanaged lines intact."""
    if _has_forbidden_controls(text):
        raise OverlayError(f"{source} contains a control character")
    lines = text.splitlines()
    entries: dict[str, tuple[int, str, bool]] = {}
    for idx, raw in enumerate(lines):
        stripped = raw.strip()
        if not stripped or stripped.startswith("#"):
            continue
        key, sep, raw_value = raw.partition("=")
        key_clean = key.strip()
        location = f"{source}:{idx + 1}"
        if key_clean.startswith("export "):
            raise OverlayError(f"{location}: 'export ' prefix is not supported in Compose .env")
        if not sep or not _DOTENV_KEY.fullmatch(key_clean):
            raise OverlayError(f"{location}: expected KEY=VALUE or # comment")
        if key_clean in entries:
            raise OverlayError(f"{location}: {key_clean} is set more than once")
        decoded, canonical = _decode_dotenv_value(raw_value, location=location)
        entries[key_clean] = (idx, decoded, canonical)
    return lines, entries


def pending_changes(
    entries: dict[str, tuple[int, str, bool]],
    overlay: dict[str, str],
) -> list[str]:
    """Keys whose value in ``.env`` is missing, non-canonical, or differs from ``overlay``."""
    changed: list[str] = []
    for key, target_val in overlay.items():
        current = entries.get(key)
        if current is None:
            changed.append(key)
            continue
        _, current_val, canonical = current
        if not canonical or current_val != target_val:
            changed.append(key)
    return changed


def _fsync_directory(directory: Path, *, best_effort: bool = False) -> None:
    try:
        descriptor = os.open(directory, os.O_RDONLY)
    except OSError as exc:
        if best_effort and exc.errno in _TOLERATED_DIR_FSYNC_ERRNOS:
            return
        raise
    try:
        os.fsync(descriptor)
    except OSError as exc:
        if not (best_effort and exc.errno in _TOLERATED_DIR_FSYNC_ERRNOS):
            raise
    finally:
        os.close(descriptor)


def _make_directory_durably(directory: Path) -> None:
    missing: list[Path] = []
    probe = directory
    while not probe.exists():
        missing.append(probe)
        probe = probe.parent
    for created in reversed(missing):
        created.mkdir(mode=0o700, exist_ok=True)
        _fsync_directory(created.parent, best_effort=True)


def _validate_backup(path: Path) -> None:
    info = path.lstat()
    if not stat.S_ISREG(info.st_mode) or stat.S_IMODE(info.st_mode) & 0o077:
        raise OverlayError("existing backup must be a private regular file")
    try:
        text = path.read_bytes().decode("utf-8")
    except UnicodeDecodeError:
        raise OverlayError("existing backup is not UTF-8") from None
    parse_dotenv_structure(text, path)


def _backup(original: bytes, target: Path, backup_dir: Path) -> Path:
    """Save pre-overlay ``.env`` bytes once with mode ``0600`` and directory fsync."""
    _make_directory_durably(backup_dir)
    path = backup_dir / f"{target.name}.pre-overlay"
    if path.exists() or path.is_symlink():
        _validate_backup(path)
        _fsync_directory(backup_dir, best_effort=True)
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
            _validate_backup(path)
        _fsync_directory(backup_dir, best_effort=True)
    finally:
        temporary.unlink(missing_ok=True)
    return path


def _write_dotenv(path: Path, rendered: str) -> None:
    """Atomically replace ``path`` with UTF-8 ``rendered`` at mode ``0600``."""
    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temporary = Path(temporary_name)
    try:
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(rendered.encode("utf-8"))
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        _fsync_directory(path.parent, best_effort=True)
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise


def apply_overlay(
    dotenv: Path,
    overlay_path: Path,
    *,
    dry_run: bool = False,
    backup_dir: Path | None = None,
    expected_sha256: str | None = None,
    rewrite_prefixes: list[tuple[str, str]] | None = None,
    omit_keys: set[str] | None = None,
) -> list[str]:
    """Write ``overlay_path`` into ``dotenv`` and enforce ``0600``; return keys changed."""
    raw_overlay = read_overlay(overlay_path, expected_sha256=expected_sha256)
    overlay = transform_overlay(
        raw_overlay,
        rewrite_prefixes=rewrite_prefixes,
        omit_keys=omit_keys,
    )
    resolved = dotenv.resolve()
    try:
        info = resolved.stat()
    except OSError as exc:
        raise OverlayError(f"cannot read {resolved}: {type(exc).__name__}") from None
    if not stat.S_ISREG(info.st_mode):
        raise OverlayError(f"{resolved} is not a regular file")
    try:
        original_bytes = resolved.read_bytes()
    except OSError as exc:
        raise OverlayError(f"cannot read {resolved}: {type(exc).__name__}") from None
    try:
        text = original_bytes.decode("utf-8")
    except UnicodeDecodeError:
        raise OverlayError(f"{resolved} is not UTF-8") from None

    lines, entries = parse_dotenv_structure(text, resolved)
    changed = pending_changes(entries, overlay)
    mode_needs_tighten = stat.S_IMODE(info.st_mode) != 0o600

    if not dry_run:
        if changed:
            if backup_dir is not None:
                _backup(original_bytes, resolved, backup_dir)
            updated_lines = list(lines)
            appended: list[str] = []
            for key in changed:
                formatted = format_dotenv_assignment(key, overlay[key])
                if key in entries:
                    line_idx = entries[key][0]
                    updated_lines[line_idx] = formatted
                else:
                    appended.append(formatted)
            if appended:
                if updated_lines and updated_lines[-1].strip():
                    updated_lines.append("")
                updated_lines.extend(appended)
            rendered = "\n".join(updated_lines) + "\n"
            _write_dotenv(resolved, rendered)
        elif mode_needs_tighten:
            os.chmod(resolved, 0o600)
    return changed


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--dotenv", type=Path, help="target Docker Compose .env file")
    parser.add_argument("--overlay", type=Path, required=True, help="reviewed KEY=VALUE overlay file")
    parser.add_argument("--expected-sha256", help="reviewed digest of the exact overlay bytes")
    parser.add_argument(
        "--rewrite-prefix",
        action="append",
        default=[],
        metavar="FROM=TO",
        help="rewrite absolute path prefix FROM to TO in overlay values (repeatable)",
    )
    parser.add_argument(
        "--omit-key",
        action="append",
        default=[],
        metavar="KEY",
        help="skip applying KEY from the overlay (repeatable)",
    )
    parser.add_argument(
        "--validate-overlay",
        action="store_true",
        help="validate overlay bytes and transforms only, without reading or changing a .env file",
    )
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--check", action="store_true", help="do not write; exit 3 when changes are pending")
    parser.add_argument(
        "--backup-dir",
        type=Path,
        help="copy the .env file's original bytes here (mode 0600) before changing it",
    )
    args = parser.parse_args(argv)
    if args.validate_overlay:
        if args.dotenv or args.dry_run or args.check or args.backup_dir:
            parser.error("--validate-overlay cannot be combined with .env file options")
    elif args.dotenv is None:
        parser.error("--dotenv is required unless --validate-overlay is selected")

    try:
        rewrites = parse_rewrite_prefixes(args.rewrite_prefix)
        omitted = parse_omit_keys(args.omit_key)
        if args.validate_overlay:
            parsed = read_overlay(args.overlay, expected_sha256=args.expected_sha256)
            transform_overlay(parsed, rewrite_prefixes=rewrites, omit_keys=omitted)
            print("[env-overlay] overlay validated")
            return 0
        before_mode = stat.S_IMODE(args.dotenv.resolve().stat().st_mode) if args.dotenv.exists() else 0o600
        changed = apply_overlay(
            args.dotenv,
            args.overlay,
            dry_run=args.dry_run or args.check,
            backup_dir=args.backup_dir,
            expected_sha256=args.expected_sha256,
            rewrite_prefixes=rewrites,
            omit_keys=omitted,
        )
    except (OverlayError, OSError) as exc:
        print(f"[env-overlay] cannot apply {args.overlay}: {exc}", file=sys.stderr)
        return 2

    mode_pending = before_mode != 0o600
    verb = "would set" if args.dry_run or args.check else "set"
    if changed:
        print(f"[env-overlay] {verb} in {args.dotenv.name}: {', '.join(changed)}")
    elif mode_pending:
        mode_verb = "would tighten mode to 0600 on" if args.dry_run or args.check else "tightened mode to 0600 on"
        print(f"[env-overlay] {mode_verb} {args.dotenv.name}")
    else:
        print(f"[env-overlay] {args.dotenv.name} already carries the overlay")
    return 3 if args.check and (changed or mode_pending) else 0


if __name__ == "__main__":
    sys.exit(main())
