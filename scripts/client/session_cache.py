#!/usr/bin/env python3
"""Transport-neutral local cache helper for UNITARES client adapters.

Stores lightweight continuity state in:

    .unitares/session.json
    .unitares/session-<slot>.json
    .unitares/last-milestone.json

This helper is intentionally small and dependency-free so Claude hooks, Codex
commands, and other thin clients can share one cache format.

Session-cache schema versions
-----------------------------

* v1: ``continuity_token`` was cached as a cross-process resume credential.
  These files may still exist on disk and are read-only legacy lineage hints.
* v2: cache writes are lineage surfaces. Store UUID/session labels, not a
  non-empty ``continuity_token``. New session writes are slot-scoped unless the
  caller explicitly opts into a shared file for a substrate-earned
  single-tenant deployment.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

CACHE_DIR = ".unitares"
CACHE_FILES = {
    "session": "session.json",
    "milestone": "last-milestone.json",
}

_SLOT_PATTERN = re.compile(r"[A-Za-z0-9_-]{1,64}")
_SESSION_IDENTITY_FIELDS = ("uuid", "client_session_id", "continuity_token")


def _workspace_path(raw: str | None) -> Path:
    base = raw or os.getcwd()
    return Path(base).expanduser().resolve()


def _slot_suffix(slot: str | None) -> str:
    if not slot:
        return ""
    safe = "".join(c if c.isalnum() or c in ("-", "_") else "_" for c in slot)
    return safe[:64]


def _cache_path(kind: str, workspace: Path, slot: str | None = None) -> Path:
    try:
        filename = CACHE_FILES[kind]
    except KeyError as exc:
        raise ValueError(f"unknown cache kind: {kind}") from exc
    safe_slot = _slot_suffix(slot) if kind == "session" else ""
    if safe_slot:
        stem, _, ext = filename.rpartition(".")
        filename = f"{stem}-{safe_slot}.{ext}"
    return workspace / CACHE_DIR / filename


def _read_json(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}
    return data if isinstance(data, dict) else {}


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    """Atomic write with mode 0600.

    Session files carry process-continuity labels such as ``client_session_id``
    and may overwrite legacy token-bearing files. A world-readable cache widens
    the local siphon surface, even when v2 writes reject non-empty tokens.
    Inlined here rather than imported from unitares_sdk because this helper is
    intentionally dependency-free.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    data = (json.dumps(payload, indent=2, sort_keys=True) + "\n").encode("utf-8")
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), suffix=".tmp")
    try:
        try:
            os.write(fd, data)
            os.fchmod(fd, 0o600)
        finally:
            os.close(fd)
        os.replace(tmp, str(path))
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def _load_payload(args: argparse.Namespace) -> dict[str, Any]:
    raw = args.json
    if raw is None and not sys.stdin.isatty():
        raw = sys.stdin.read()
    if raw is None:
        return {}
    raw = raw.strip()
    if not raw:
        return {}
    data = json.loads(raw)
    if not isinstance(data, dict):
        raise ValueError("payload must be a JSON object")
    return data


def cmd_path(args: argparse.Namespace) -> int:
    workspace = _workspace_path(args.workspace)
    print(_cache_path(args.kind, workspace, getattr(args, "slot", None)))
    return 0


def cmd_get(args: argparse.Namespace) -> int:
    workspace = _workspace_path(args.workspace)
    payload = _read_json(_cache_path(args.kind, workspace, getattr(args, "slot", None)))
    if args.key:
        value = payload.get(args.key)
        if value is None:
            return 0
        if isinstance(value, (dict, list)):
            print(json.dumps(value))
        else:
            print(value)
        return 0
    print(json.dumps(payload))
    return 0


def cmd_set(args: argparse.Namespace) -> int:
    workspace = _workspace_path(args.workspace)
    slot = getattr(args, "slot", None)
    allow_shared = bool(getattr(args, "allow_shared", False))
    if args.kind == "session" and not slot and not allow_shared:
        print(
            "session_cache.py: refusing slotless session write; pass --slot <id> "
            "or --allow-shared for substrate-earned single-tenant use",
            file=sys.stderr,
        )
        return 2

    path = _cache_path(args.kind, workspace, slot)
    payload = _load_payload(args)
    if args.merge:
        existing = _read_json(path)
        if args.kind == "session":
            stale_token = existing.get("continuity_token")
            if isinstance(stale_token, str) and stale_token.strip():
                existing.pop("continuity_token", None)
                print(
                    "session_cache.py: [V1_LEGACY_STRIP] dropped pre-existing "
                    f"continuity_token from {path} during merge",
                    file=sys.stderr,
                )
        existing.update(payload)
        payload = existing

    if args.kind == "session":
        token = payload.get("continuity_token")
        if isinstance(token, str) and token:
            print(
                "session_cache.py: refusing session payload with non-empty "
                "continuity_token; v2 cache stores lineage hints, not resume "
                "credentials",
                file=sys.stderr,
            )
            return 2
        if not any(key in payload for key in _SESSION_IDENTITY_FIELDS):
            print(
                "session_cache.py: refusing to write session cache without any "
                f"identity field (need one of {list(_SESSION_IDENTITY_FIELDS)})",
                file=sys.stderr,
            )
            return 1

    if args.stamp:
        payload["updated_at"] = datetime.now(timezone.utc).isoformat()
    _write_json(path, payload)
    if args.echo:
        print(json.dumps(payload))
    return 0


def cmd_clear(args: argparse.Namespace) -> int:
    workspace = _workspace_path(args.workspace)
    path = _cache_path(args.kind, workspace, getattr(args, "slot", None))
    try:
        path.unlink()
    except FileNotFoundError:
        pass
    return 0


def _parse_session_filename(name: str) -> str | None:
    if not name.startswith("session-") or not name.endswith(".json"):
        return None
    raw = name[len("session-") : -len(".json")]
    if not raw or not _SLOT_PATTERN.fullmatch(raw):
        return None
    return raw


def cmd_list(args: argparse.Namespace) -> int:
    workspace = _workspace_path(args.workspace)
    cache_dir = workspace / CACHE_DIR
    entries: list[dict[str, Any]] = []
    if cache_dir.is_dir():
        for path in cache_dir.iterdir():
            if not path.is_file():
                continue
            slot = _parse_session_filename(path.name)
            if path.name != "session.json" and slot is None:
                continue
            payload = _read_json(path)
            if not payload:
                continue
            uuid = payload.get("uuid")
            client_session_id = payload.get("client_session_id")
            if not uuid and not client_session_id:
                continue
            entries.append({
                "slot": slot,
                "parent_agent_id": uuid,
                "prior_client_session_id": client_session_id,
                "updated_at": payload.get("updated_at"),
                "path": str(path),
            })

    min_utc = datetime.min.replace(tzinfo=timezone.utc)

    def _sort_ts(entry: dict[str, Any]) -> datetime:
        raw = entry.get("updated_at")
        if not isinstance(raw, str) or not raw:
            return min_utc
        try:
            parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        except Exception:
            return min_utc
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed

    entries.sort(key=_sort_ts, reverse=True)
    print(json.dumps(entries))
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    p_path = sub.add_parser("path", help="Print the absolute cache path")
    p_path.add_argument("kind", choices=sorted(CACHE_FILES))
    p_path.add_argument("--workspace")
    p_path.add_argument("--slot")
    p_path.set_defaults(func=cmd_path)

    p_get = sub.add_parser("get", help="Read cached JSON")
    p_get.add_argument("kind", choices=sorted(CACHE_FILES))
    p_get.add_argument("--workspace")
    p_get.add_argument("--slot")
    p_get.add_argument("--key")
    p_get.set_defaults(func=cmd_get)

    p_set = sub.add_parser("set", help="Write cached JSON")
    p_set.add_argument("kind", choices=sorted(CACHE_FILES))
    p_set.add_argument("--workspace")
    p_set.add_argument("--slot")
    p_set.add_argument(
        "--allow-shared",
        action="store_true",
        help=(
            "Permit slotless session writes for substrate-earned single-tenant "
            "deployments. This is an operator assertion, not runtime proof."
        ),
    )
    p_set.add_argument("--json")
    p_set.add_argument("--merge", action="store_true")
    p_set.add_argument("--stamp", action="store_true")
    p_set.add_argument("--echo", action="store_true")
    p_set.set_defaults(func=cmd_set)

    p_clear = sub.add_parser("clear", help="Delete a cache file")
    p_clear.add_argument("kind", choices=sorted(CACHE_FILES))
    p_clear.add_argument("--workspace")
    p_clear.add_argument("--slot")
    p_clear.set_defaults(func=cmd_clear)

    p_list = sub.add_parser("list", help="List session slot inventory as JSON")
    p_list.add_argument("--workspace")
    p_list.set_defaults(func=cmd_list)

    return parser


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    try:
        return int(args.func(args))
    except Exception as exc:
        print(f"session_cache.py: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
