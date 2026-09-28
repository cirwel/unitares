#!/usr/bin/env python3
"""Prove the governance plugin's edit hooks against a live lease plane.

The companion plugin (cirwel/unitares-governance-plugin) takes a ``file://``
lease in its PreToolUse hook before an agent edits a file and releases it in
PostToolUse. Its own tests use a fake HTTP server. This script drives the real
plugin hooks, through the same ``hooks/run-hook.cmd`` entry point that the
plugin's ``hooks/claude-hooks.json`` wires, against the lease plane of a
running Compose stack:

1. Session A's ``pre-edit`` on file F acquires (exit 0), and the lease plane
   reports an active lease on F's surface.
2. Session B's ``pre-edit`` on F, from a second git worktree of the same
   repository, is refused (exit 2) and names A's lease as the blocker.
3. After A's ``post-edit-release``, the surface is free and B's ``pre-edit``
   succeeds with a different holder.

The hooks run with ``UNITARES_FILE_LEASES_REQUIRED=1`` because they fail open
by default: without it, an unreachable lease plane would also exit 0 and step 1
would prove nothing. Each step is also checked against ``/v1/lease/status``, so
the proof does not rest on the hook's exit code alone.

Usage (Compose stack already up; lease-plane settings come from the env):

    python3 scripts/ci/plugin_file_lease_proof.py \\
        --plugin-dir _plugin --work-dir "$RUNNER_TEMP/lease-proof"

What this does not claim: it covers the Claude host payload only, one file
per edit, inside one operator trust boundary. It does not prove that a client
without the plugin is stopped; the lease is advisory to anything that does not
ask for it.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

DEFAULT_BASE_URL = "http://127.0.0.1:8788"
# The Compose default bearer. Used only for this script's own status reads.
DEFAULT_BEARER = "unitares-local-lease-plane"
REFUSAL_TEXT = "file lease held by another agent"
FILE_NAME = "shared.txt"


class ProofError(RuntimeError):
    """The live stack did not behave as the lease contract requires."""


def _log(message: str) -> None:
    print(f"[plugin-lease-proof] {message}", flush=True)


def _git(*args: str, cwd: Path) -> None:
    subprocess.run(
        [
            "git",
            "-c", "user.name=lease-proof",
            "-c", "user.email=lease-proof@example.invalid",
            "-c", "init.defaultBranch=main",
            *args,
        ],
        cwd=cwd,
        check=True,
        capture_output=True,
        text=True,
    )


def build_workspace(work_dir: Path) -> tuple[Path, Path]:
    """Create one repository with two worktrees; return (main, linked)."""
    work_dir.mkdir(parents=True, exist_ok=True)
    main = work_dir / "repo"
    linked = work_dir / "repo-b"
    if main.exists() or linked.exists():
        raise ProofError(f"work dir {work_dir} is not empty; pass a fresh path")
    main.mkdir()
    _git("init", "-q", cwd=main)
    (main / FILE_NAME).write_text("one file, two agents\n", encoding="utf-8")
    _git("add", FILE_NAME, cwd=main)
    _git("commit", "-q", "-m", "seed", cwd=main)
    _git("worktree", "add", "-q", "-b", "session-b", str(linked), cwd=main)
    return main.resolve(), linked.resolve()


def edit_payload(session_id: str, tool_use_id: str, file_path: Path, event: str) -> str:
    """A Claude Code Edit hook payload, the shape the plugin normalizes."""
    return json.dumps(
        {
            "hook_event_name": event,
            "session_id": session_id,
            "tool_name": "Edit",
            "tool_use_id": tool_use_id,
            "tool_input": {
                "file_path": str(file_path),
                "old_string": "one",
                "new_string": "1",
            },
        }
    )


def run_hook(
    plugin_dir: Path,
    hook: str,
    *,
    cwd: Path,
    payload: str,
    env: dict[str, str],
) -> subprocess.CompletedProcess[str]:
    # The command string the plugin's hooks/claude-hooks.json wires, run by a
    # shell from the session's working directory with the event on stdin.
    # run-hook.cmd is a cmd/sh polyglot with no shebang, so it has to go
    # through a shell (as the host does) rather than a direct exec.
    return subprocess.run(
        ["bash", "-c", f'"${{CLAUDE_PLUGIN_ROOT}}/hooks/run-hook.cmd" {hook} --host claude'],
        cwd=cwd,
        input=payload,
        env={**env, "CLAUDE_PLUGIN_ROOT": str(plugin_dir)},
        capture_output=True,
        text=True,
        timeout=30,
    )


def lease_status(base_url: str, bearer: str, surface_id: str) -> dict[str, Any] | None:
    query = urllib.parse.urlencode({"surface_id": surface_id})
    request = urllib.request.Request(
        f"{base_url}/v1/lease/status?{query}",
        headers={"Accept": "application/json", "Authorization": f"Bearer {bearer}"},
    )
    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            body = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        raise ProofError(
            f"/v1/lease/status answered HTTP {exc.code}: {exc.read()[:300]!r}"
        ) from exc
    except (urllib.error.URLError, OSError, ValueError) as exc:
        raise ProofError(f"/v1/lease/status at {base_url} unreadable: {exc}") from exc
    if body.get("ok") is not True:
        raise ProofError(f"/v1/lease/status did not answer ok: {body}")
    lease = body.get("lease")
    return lease if isinstance(lease, dict) else None


def _describe(result: subprocess.CompletedProcess[str]) -> str:
    return (
        f"exit={result.returncode} stdout={result.stdout.strip()!r} "
        f"stderr={result.stderr.strip()!r}"
    )


def hook_env(base_env: dict[str, str]) -> dict[str, str]:
    env = dict(base_env)
    # Fail closed, so a missing or unreachable lease plane cannot pass as an
    # acquire. An inherited opt-out must not switch the proof off either.
    env["UNITARES_FILE_LEASES_REQUIRED"] = "1"
    env["UNITARES_FILE_LEASES_ENABLED"] = "1"
    env.pop("EVAL_UNITARES_OFFLINE", None)
    # A slow shared runner should not turn into a false refusal. These widen
    # the per-request and per-batch deadlines; they do not change the contract.
    env.setdefault("UNITARES_FILE_LEASE_TIMEOUT_S", "3")
    env.setdefault("UNITARES_FILE_LEASE_BATCH_TIMEOUT_S", "4")
    # Long enough that the lease cannot expire between steps on its own, so
    # step 3 proves the release rather than the TTL.
    env.setdefault("UNITARES_FILE_LEASE_TTL_S", "120")
    return env


def prove(plugin_dir: Path, work_dir: Path, base_url: str, bearer: str) -> None:
    for hook in ("run-hook.cmd", "pre-edit", "post-edit-release"):
        if not (plugin_dir / "hooks" / hook).is_file():
            raise ProofError(f"plugin checkout has no hooks/{hook}: {plugin_dir}")

    main, linked = build_workspace(work_dir)
    file_a = main / FILE_NAME
    file_b = linked / FILE_NAME
    # The plugin maps every worktree of a repository onto the main checkout,
    # so both sessions contend on the main checkout's path.
    surface_id = f"file://{file_a}"
    env = hook_env(dict(os.environ))
    _log(f"lease plane {base_url}; surface {surface_id}")
    _log(f"session A edits {file_a}; session B edits {file_b} (linked worktree)")

    if lease_status(base_url, bearer, surface_id) is not None:
        raise ProofError(f"surface already leased before the proof began: {surface_id}")

    # 1. A acquires.
    a_pre = edit_payload("proof-session-a", "toolu_proof_a1", file_a, "PreToolUse")
    result = run_hook(plugin_dir, "pre-edit", cwd=main, payload=a_pre, env=env)
    if result.returncode != 0:
        raise ProofError(f"step 1: session A pre-edit did not acquire: {_describe(result)}")
    lease_a = lease_status(base_url, bearer, surface_id)
    if lease_a is None:
        raise ProofError("step 1: A's pre-edit exited 0 but the lease plane holds no lease")
    holder_a = lease_a.get("holder_agent_uuid")
    _log(
        f"PASS 1: A pre-edit exit 0; lease {lease_a.get('lease_id')} "
        f"held by {holder_a}, intent {lease_a.get('intent')!r}"
    )

    # 2. B is refused on the same file, from another worktree.
    b_pre1 = edit_payload("proof-session-b", "toolu_proof_b1", file_b, "PreToolUse")
    result = run_hook(plugin_dir, "pre-edit", cwd=linked, payload=b_pre1, env=env)
    if result.returncode != 2 or REFUSAL_TEXT not in result.stderr:
        raise ProofError(
            f"step 2: session B was not refused as held_by_other: {_describe(result)}"
        )
    if str(lease_a.get("lease_id")) not in result.stderr:
        raise ProofError(f"step 2: B's refusal does not name A's lease: {_describe(result)}")
    still_a = lease_status(base_url, bearer, surface_id)
    if still_a is None or still_a.get("lease_id") != lease_a.get("lease_id"):
        raise ProofError(f"step 2: A's lease did not survive B's attempt: {still_a}")
    first_line = result.stderr.strip().splitlines()[0]
    _log(f"PASS 2: B pre-edit exit 2 (held_by_other): {first_line!r}, blocker is A's lease")

    # 3. A releases; B then acquires.
    a_post = edit_payload("proof-session-a", "toolu_proof_a1", file_a, "PostToolUse")
    result = run_hook(plugin_dir, "post-edit-release", cwd=main, payload=a_post, env=env)
    if result.returncode != 0:
        raise ProofError(f"step 3: A post-edit-release failed: {_describe(result)}")
    if lease_status(base_url, bearer, surface_id) is not None:
        raise ProofError("step 3: A's post-edit-release exited 0 but the surface is still leased")
    _log("A post-edit-release exit 0; surface is free")

    b_pre2 = edit_payload("proof-session-b", "toolu_proof_b2", file_b, "PreToolUse")
    result = run_hook(plugin_dir, "pre-edit", cwd=linked, payload=b_pre2, env=env)
    if result.returncode != 0:
        raise ProofError(f"step 3: B's retry after release did not acquire: {_describe(result)}")
    lease_b = lease_status(base_url, bearer, surface_id)
    if lease_b is None:
        raise ProofError("step 3: B's pre-edit exited 0 but the lease plane holds no lease")
    if lease_b.get("holder_agent_uuid") == holder_a:
        raise ProofError(f"step 3: B's lease has A's holder {holder_a}")
    _log(
        f"PASS 3: B pre-edit exit 0 after A released; lease {lease_b.get('lease_id')} "
        f"held by {lease_b.get('holder_agent_uuid')}"
    )

    # Clean up, and check the release path once more from the other worktree.
    b_post = edit_payload("proof-session-b", "toolu_proof_b2", file_b, "PostToolUse")
    run_hook(plugin_dir, "post-edit-release", cwd=linked, payload=b_post, env=env)
    if lease_status(base_url, bearer, surface_id) is not None:
        raise ProofError("cleanup: B's post-edit-release left the surface leased")
    _log("B post-edit-release exit 0; surface is free. Plugin file-lease proof passed.")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--plugin-dir", required=True, type=Path)
    parser.add_argument("--work-dir", required=True, type=Path)
    args = parser.parse_args(argv)

    base_url = (os.environ.get("LEASE_PLANE_BASE_URL") or DEFAULT_BASE_URL).rstrip("/")
    bearer = os.environ.get("LEASE_PLANE_BEARER_TOKEN") or DEFAULT_BEARER
    try:
        prove(args.plugin_dir.resolve(), args.work_dir.resolve(), base_url, bearer)
    except (ProofError, subprocess.CalledProcessError, OSError) as exc:
        print(f"[plugin-lease-proof] FAIL: {exc}", file=sys.stderr, flush=True)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
