#!/usr/bin/python3
"""Root-deployed, privilege-separated model-review worker.

The merge conductor invokes this file through a narrowly scoped sudoers rule as
the dedicated reviewer UID.  That UID owns only provider subscription state and
cannot read the conductor's GitHub App key, lease bearer, or secrets file.
"""

from __future__ import annotations

import argparse
import errno
import hashlib
import json
import os
import re
import stat
import sys
from dataclasses import asdict
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from scripts.ops import merge_conductor as mc  # noqa: E402

WORKER_VERSION = 1
MODEL_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:/-]{0,127}")


def _private_home_payload(
    boundary: mc.MergeServiceBoundary,
) -> dict[str, object]:
    if not (
        sys.flags.isolated
        and sys.flags.no_site
        and sys.flags.ignore_environment
        and getattr(sys.flags, "safe_path", True)
    ):
        raise RuntimeError("reviewer Python must start with -I -S")
    if Path(sys.executable).resolve(
        strict=True
    ) != boundary.reviewer_python_path.resolve(strict=True):
        raise RuntimeError("reviewer Python did not match the root manifest")
    raw_home = os.environ.get("HOME")
    if not raw_home or not Path(raw_home).is_absolute():
        raise RuntimeError("reviewer HOME must be an explicit absolute path")
    home = Path(raw_home)
    try:
        resolved = home.resolve(strict=True)
        metadata = resolved.stat()
    except OSError as exc:
        raise RuntimeError("reviewer HOME was missing or unresolved") from exc
    if not stat.S_ISDIR(metadata.st_mode):
        raise RuntimeError("reviewer HOME was not a directory")
    if metadata.st_uid != os.geteuid():
        raise RuntimeError("reviewer HOME was not owned by the reviewer UID")
    if stat.S_IMODE(metadata.st_mode) & 0o077:
        raise RuntimeError("reviewer HOME must have mode 0700")
    if os.geteuid() != boundary.reviewer_uid:
        raise RuntimeError("review worker ran as the wrong attested UID")
    if resolved != boundary.reviewer_home.resolve(strict=True):
        raise RuntimeError("review worker HOME did not match the root manifest")
    if Path(__file__).resolve(strict=True) != boundary.review_runner_path.resolve(
        strict=True
    ):
        raise RuntimeError("review worker path did not match the root manifest")
    return {
        "version": WORKER_VERSION,
        "uid": os.geteuid(),
        "home": str(resolved),
        "home_mode": stat.S_IMODE(metadata.st_mode),
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(add_help=False)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--probe", action="store_true")
    mode.add_argument("--preflight", action="store_true")
    mode.add_argument("--review", choices=("claude", "codex"))
    parser.add_argument("--model")
    parser.add_argument("--timeout", type=float)
    parser.add_argument("--deny-read", type=Path)
    return parser.parse_args()


def _validate_arguments(
    args: argparse.Namespace,
    boundary: mc.MergeServiceBoundary,
) -> None:
    """Constrain the sudo entrypoint to its documented fixed argument grammar."""
    if args.deny_read is not None:
        if not args.probe:
            raise RuntimeError("--deny-read is valid only with --probe")
        allowed_probe_paths = {
            boundary.credential_root,
            boundary.review_key_path,
            boundary.secrets_env_path,
        }
        if (
            not args.deny_read.is_absolute()
            or args.deny_read not in allowed_probe_paths
        ):
            raise RuntimeError("--deny-read path was not root-attested")
    if not args.review and (args.model is not None or args.timeout is not None):
        raise RuntimeError("--model/--timeout are valid only with --review")


def main() -> int:
    args = parse_args()
    try:
        boundary = mc.MergeServiceBoundary.from_payload(
            mc._read_root_owned_json(mc.MERGE_SERVICE_BOUNDARY_PATH)
        )
        _validate_arguments(args, boundary)
        os.environ["PATH"] = os.pathsep.join(
            str(path) for path in boundary.reviewer_path
        )
        identity = _private_home_payload(boundary)
        if args.probe:
            if args.deny_read is not None:
                descriptor = -1
                try:
                    descriptor = os.open(
                        args.deny_read,
                        os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0),
                    )
                    read_denied = False
                except OSError as exc:
                    read_denied = exc.errno in {errno.EACCES, errno.EPERM}
                finally:
                    if descriptor >= 0:
                        os.close(descriptor)
                identity["read_denied"] = read_denied
            print(json.dumps(identity, sort_keys=True))
            return 0
        if args.preflight:
            mc.ModelReviewer.assert_cli_contracts(
                {
                    "claude": boundary.claude_cli_path,
                    "codex": boundary.codex_cli_path,
                }
            )
            print(json.dumps({**identity, "contracts": "ok"}, sort_keys=True))
            return 0
        model = str(args.model or "").strip()
        if not MODEL_PATTERN.fullmatch(model):
            raise RuntimeError("review model identifier was invalid")
        timeout = 420.0 if args.timeout is None else args.timeout
        if timeout < 1 or timeout > 3600:
            raise RuntimeError("review timeout must be between 1 and 3600 seconds")
        backend = str(args.review)
        variable = (
            "UNITARES_MERGE_CLAUDE_MODEL"
            if backend == "claude"
            else "UNITARES_MERGE_CODEX_MODEL"
        )
        os.environ[variable] = model
        prompt_limit = mc.MAX_PATCH_BYTES + 500_000
        prompt = sys.stdin.read(prompt_limit + 1)
        if (
            not prompt
            or len(prompt) > prompt_limit
            or len(prompt.encode("utf-8")) > prompt_limit
        ):
            raise RuntimeError("review prompt was empty or exceeded the worker limit")
        result = mc.ModelReviewer(
            timeout_s=timeout,
            claude_binary=boundary.claude_cli_path,
            codex_binary=boundary.codex_cli_path,
        ).review(backend, prompt)
        verdict_nonce = mc._trusted_verdict_nonce(prompt)
        if verdict_nonce is None:
            raise RuntimeError("review prompt had no trusted verdict nonce")
        print(
            json.dumps(
                {
                    "version": WORKER_VERSION,
                    "uid": os.geteuid(),
                    "home": identity["home"],
                    "prompt_sha256": hashlib.sha256(prompt.encode("utf-8")).hexdigest(),
                    "verdict_nonce": verdict_nonce,
                    "result": asdict(result),
                },
                sort_keys=True,
            )
        )
        return 0
    except Exception as exc:  # noqa: BLE001 - worker returns one bounded error
        print(
            json.dumps(
                {
                    "version": WORKER_VERSION,
                    "error": f"{type(exc).__name__}: {exc}",
                },
                sort_keys=True,
            ),
            file=sys.stderr,
        )
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
