#!/usr/bin/env python3
"""Minimal PlistBuddy-compatible command used by portable deploy tests."""

from __future__ import annotations

import plistlib
import shlex
import sys
from pathlib import Path
from typing import Any


def _lookup(payload: dict[str, Any], keys: list[str]) -> Any:
    value: Any = payload
    for key in keys:
        if not isinstance(value, dict) or key not in value:
            raise KeyError(key)
        value = value[key]
    return value


def _parent(payload: dict[str, Any], keys: list[str]) -> tuple[dict[str, Any], str]:
    if not keys:
        raise KeyError("empty key path")
    parent = _lookup(payload, keys[:-1]) if len(keys) > 1 else payload
    if not isinstance(parent, dict):
        raise KeyError(keys[-1])
    return parent, keys[-1]


def main() -> int:
    if len(sys.argv) != 4 or sys.argv[1] != "-c":
        return 2

    try:
        parts = shlex.split(sys.argv[2])
        verb, key_path = parts[:2]
        keys = [part for part in key_path.split(":") if part]
        path = Path(sys.argv[3])
        payload = plistlib.loads(path.read_bytes())

        if verb == "Print" and len(parts) == 2:
            print(_lookup(payload, keys))
            return 0

        parent, key = _parent(payload, keys)
        if verb == "Add" and len(parts) == 4 and parts[2] == "string":
            if key in parent:
                return 1
            parent[key] = parts[3]
        elif verb == "Set" and len(parts) == 3:
            if key not in parent:
                return 1
            parent[key] = parts[2]
        elif verb == "Delete" and len(parts) == 2:
            del parent[key]
        else:
            return 2

        path.write_bytes(plistlib.dumps(payload))
        return 0
    except (IndexError, KeyError, OSError, plistlib.InvalidFileException, ValueError):
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
