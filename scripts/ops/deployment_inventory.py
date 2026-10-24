#!/usr/bin/env python3
"""Validated deployment inventory boundary; no host or service roster defaults.

Set UNITARES_DEPLOY_INVENTORY to a JSON file, or
UNITARES_DEPLOY_INVENTORY_PROVIDER to a trusted executable that prints the same
JSON. Providers are deployment code and must be installed from reviewed source.
"""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import re
import subprocess
import sys

FIELDS = (
    "name",
    "label",
    "checkout",
    "paths",
    "pickup",
    "port",
    "deploy_script",
    "status_adapter",
)
PICKUPS = {
    "restart",
    "restart-DEV",
    "hot-reload",
    "live-from-checkout",
    "library",
    "external",
    "unregistered",
}


def safe_text(value, name):
    if not isinstance(value, str) or any(c in value for c in "|\t\r\n\0"):
        raise ValueError("%s must be a string without field delimiters" % name)
    return value


def canonical_path(value, key):
    """Identity follows the physical target, including Git worktree aliases.

    A deployment worktree may not exist yet, so strict resolution is unsuitable.
    When it exists, a path inside it must not create a second identity for the
    same checkout. Different worktrees retain their distinct top-level paths.
    """
    resolved = str(Path(value).resolve(strict=False))
    if key == "checkout" and Path(resolved).is_dir():
        probe = subprocess.run(
            ["git", "-C", resolved, "rev-parse", "--show-toplevel"],
            capture_output=True,
            text=True,
            timeout=10,
            env=dict(os.environ, GIT_OPTIONAL_LOCKS="0"),
        )
        if probe.returncode == 0:
            resolved = str(Path(probe.stdout.rstrip("\n")).resolve(strict=False))
    # A safe alias can point at a target containing a framing delimiter.
    return safe_text(resolved, key)


def inventory():
    path = os.environ.get("UNITARES_DEPLOY_INVENTORY", "")
    provider = os.environ.get("UNITARES_DEPLOY_INVENTORY_PROVIDER", "")
    if bool(path) == bool(provider):
        raise ValueError(
            "configure exactly one of UNITARES_DEPLOY_INVENTORY or UNITARES_DEPLOY_INVENTORY_PROVIDER"
        )
    if provider:
        if not Path(provider).is_absolute():
            raise ValueError("inventory provider must be an absolute executable path")
        raw = subprocess.run(
            [provider], check=True, text=True, capture_output=True, timeout=30
        ).stdout
    else:
        raw = Path(path).read_text()
    data = json.loads(raw)
    if (
        not isinstance(data, dict)
        or data.get("version") != 1
        or not isinstance(data.get("services"), list)
    ):
        raise ValueError("inventory requires version=1 and a services array")
    seen = set()
    for row in data["services"]:
        if not isinstance(row, dict) or set(row) - set(FIELDS):
            raise ValueError("service fields must use the documented inventory schema")
        for key in FIELDS:
            row[key] = safe_text(row.get(key, ""), key)
        name = row["name"]
        if not re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9._-]*", name) or name in seen:
            raise ValueError("service names must be unique safe identifiers")
        seen.add(name)
        if row["pickup"] not in PICKUPS:
            raise ValueError("%s has an unsupported pickup" % name)
        for key in ("checkout", "deploy_script", "status_adapter"):
            if key == "checkout" or row[key]:
                if not Path(row[key]).is_absolute():
                    raise ValueError("%s.%s must be absolute" % (name, key))
                row[key] = canonical_path(row[key], key)
        if row["port"] and (
            not row["port"].isdigit() or not 1 <= int(row["port"]) <= 65535
        ):
            raise ValueError("%s.port must be a valid TCP port" % name)
        if row["pickup"] == "external" and not row["status_adapter"]:
            raise ValueError("%s requires a status_adapter" % name)
        if row["paths"] and any(
            not re.fullmatch(r"[a-zA-Z0-9_./-]+", p)
            or p.startswith("/")
            or ".." in p.split("/")
            for p in row["paths"].split()
        ):
            raise ValueError("%s.paths must be relative literal pathspecs" % name)
    return data["services"]


def adapter_status(command, repo, base):
    data = json.loads(
        subprocess.run(
            [command, "--repo", repo, "--base", base],
            check=True,
            text=True,
            capture_output=True,
            timeout=30,
        ).stdout
    )
    keys = ("verdict", "branch", "commit", "behind", "health")
    if not isinstance(data, dict) or set(data) != set(keys):
        raise ValueError(
            "status adapter must return verdict, branch, commit, behind, health"
        )
    values = [safe_text(data[k], k) for k in keys]
    if not values[0]:
        raise ValueError("adapter verdict must not be empty")
    return "|".join(values)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--list-checkouts", action="store_true")
    parser.add_argument(
        "--adapter-status", nargs=3, metavar=("EXECUTABLE", "REPO", "BASE")
    )
    args = parser.parse_args()
    try:
        if args.adapter_status:
            print(adapter_status(*args.adapter_status))
        else:
            rows = inventory()
            if args.list_checkouts:
                print("\n".join(dict.fromkeys(row["checkout"] for row in rows)))
            else:
                print("\n".join("|".join(row[k] for k in FIELDS) for row in rows))
    except (ValueError, OSError, subprocess.SubprocessError) as exc:
        print("[inventory] %s" % exc, file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
