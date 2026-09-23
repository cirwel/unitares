#!/usr/bin/env python3
"""Install the opt-in macOS PR review fallback; never reload a running job.

python3 scripts/ops/install-review-sweep.py --repo ~/projects/unitares
Uses existing gh/codex/claude authentication. The installed bootstrap refreshes
its review driver from origin/master on each run, never from a PR branch.
"""
from __future__ import annotations

import argparse
import os
from pathlib import Path
import plistlib
import shutil
import subprocess
import sys

LABEL = "com.unitares.review-sweep"


def install(repo: Path, *, write_only: bool = False) -> Path:
    home = Path.home()
    service = f"gui/{os.getuid()}/{LABEL}"
    if not write_only and subprocess.run(
        ["launchctl", "print", service], capture_output=True,
    ).returncode == 0:
        raise SystemExit(f"{service} is already loaded; leave its active review alone")
    subprocess.run(["git", "-C", str(repo), "rev-parse", "--git-common-dir"],
                   check=True, capture_output=True)
    wrapper = home / ".local/libexec/unitares/review-sweep.sh"
    wrapper.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(Path(__file__).with_name("review-sweep.sh"), wrapper)
    logs = home / "Library/Logs"
    logs.mkdir(parents=True, exist_ok=True)
    arguments = ["/bin/bash", str(wrapper)]
    recorder = shutil.which("unitares-automation-run")
    if recorder:
        arguments = [recorder, "--id", f"launchd:{LABEL}", "--name", LABEL,
                     "--source", "launchd", "--kind", "automation", "--", *arguments]
    # launchd's default PATH omits Homebrew and the user's model CLIs.
    paths = [str(Path(sys.executable).parent), str(home / ".local/bin"),
             "/opt/homebrew/bin", "/usr/local/bin", "/usr/bin", "/bin"]
    for command in ("gh", "codex", "claude"):
        found = shutil.which(command)
        if found:
            paths.insert(0, str(Path(found).parent))
    config = {
        "Label": LABEL,
        "ProgramArguments": arguments,
        "WorkingDirectory": str(home),
        "StartInterval": 1800,
        "RunAtLoad": True,
        "StandardOutPath": str(logs / "unitares-review-sweep.log"),
        "StandardErrorPath": str(logs / "unitares-review-sweep.log"),
        "EnvironmentVariables": {"PATH": ":".join(dict.fromkeys(paths)),
                                 "HOME": str(home), "UNITARES_REVIEW_REPO": str(repo)},
    }
    plist = home / "Library/LaunchAgents" / f"{LABEL}.plist"
    plist.parent.mkdir(parents=True, exist_ok=True)
    plist.write_bytes(plistlib.dumps(config))
    if not write_only:
        subprocess.run(["launchctl", "bootstrap", f"gui/{os.getuid()}", str(plist)], check=True)
    print(f"[review-sweep] {'wrote' if write_only else 'loaded'} {plist}")
    print(f"[review-sweep] verify: launchctl print {service}")
    return plist


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", type=Path, default=Path.home() / "projects/unitares")
    parser.add_argument("--write-only", action="store_true", help="write config without loading launchd")
    args = parser.parse_args()
    install(args.repo.expanduser().resolve(), write_only=args.write_only)
