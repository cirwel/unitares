#!/usr/bin/env python3
"""Trusted stdlib-only launcher: verify exact bytes before application execution.

Usage: python -I -S -B run_kg_snapshot.py PLAN SHA256 APPROVAL APPROVAL_SHA256
The supplied plan digest and governance approval must come from trusted review.
This is a trusted-workload integrity check, not an OS security sandbox.
"""
import hashlib
import json
import os
from pathlib import Path
import stat
import sys


def main():
    if not (sys.flags.isolated and sys.flags.no_site and sys.dont_write_bytecode) or len(sys.argv) != 5:
        raise SystemExit("requires python -I -S -B launcher PLAN SHA256 APPROVAL APPROVAL_SHA256")
    plan_path, expected, approval, approval_sha = sys.argv[1:]
    fd = os.open(plan_path, os.O_RDONLY | os.O_NOFOLLOW)
    with os.fdopen(fd, "rb") as stream:
        info = os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != 0o600:
            raise SystemExit("plan must be owned/private/regular")
        data = stream.read(16 * 1024 * 1024 + 1)
    if len(data) > 16 * 1024 * 1024 or hashlib.sha256(data).hexdigest() != expected:
        raise SystemExit("reviewed plan bytes changed")
    plan = json.loads(data)
    repo = Path(plan["repo"])
    sources = {}
    for name in ("kg_snapshot_feasibility.py", "kg_canary_runtime.py"):
        path = repo / "scripts/eval" / name
        sources[name] = path.read_bytes()
        if hashlib.sha256(sources[name]).hexdigest() != plan["files"].get(str(path)):
            raise SystemExit("reviewed entrypoint/helper changed before execution")
    path = repo / "scripts/eval/kg_snapshot_feasibility.py"
    sys.argv[:] = [str(path), "execute", "--plan", plan_path, "--plan-sha256", expected,
                   "--approval", approval, "--approval-sha256", approval_sha]
    values = {"__name__": "__main__", "__file__": str(path),
              "_VERIFIED_ENTRY_BYTES": sources["kg_snapshot_feasibility.py"],
              "_VERIFIED_HELPER_BYTES": sources["kg_canary_runtime.py"]}
    exec(compile(sources["kg_snapshot_feasibility.py"], str(path), "exec"), values)


if __name__ == "__main__":
    main()
