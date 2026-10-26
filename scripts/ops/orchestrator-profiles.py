#!/usr/bin/env python3
"""Write the agent orchestrator's launch profiles file.

Design: ``docs/proposals/active/inference-hosts-config-v0.md`` §2.5 and step
4b. The orchestrator (``elixir/agent_orchestrator/lib/agent_orchestrator/profiles.ex``)
reads ``AGENT_ORCHESTRATOR_PROFILES_FILE``; this script writes it, and the
orchestrator's ``scripts/start.sh`` runs it before boot when that variable is
set together with ``AGENT_ORCHESTRATOR_PROFILES_GENERATE=1``. The generator
owns the whole file, so that switch is how an operator hands it over. The
orchestrator re-reads the file when its digest changes, so running this again
needs no restart.

It reads files only, never its own process environment, because start.sh has
sourced the secrets file into that environment and a profile must not pick a
value up by accident:

* the repository root (``--repo-root``, default: the checkout holding this
  script), which should be the deployment tree, never a worktree agents edit;
* the interpreter the clients run under (``--python``, default: this one);
* the server's environment files (``--env-file``, repeatable, later files
  win), for the CLI overrides (``UNITARES_CLAUDE_CLI``, ``UNITARES_CODEX_CLI``,
  ``UNITARES_ANTIGRAVITY_CLI``) and ``UNITARES_CODEX_APP_SERVER_INSTRUMENTATION``;
* the CLI on disk. Each CLI is pinned by the path of its symlink, never its
  target: ``claude``'s auto-update replaces the target.

Profiles written, one per host adapter lane, each omitted with a note on
standard error when its CLI is not found:

* ``host.claude``: ``claude_cli_client.py``.
* ``host.codex``: ``codex_app_server_client.py``, which falls back to
  ``codex exec`` itself (``HA_CODEX_EXEC_FALLBACK``) where the raw adapter
  used a shell template, and runs its own administrator-configuration and
  sandbox-probe checks before either.
* ``host.antigravity``: ``antigravity_cli_client.py``.
* ``dialectic.reviewer``: the orchestrated dialectic reviewer
  (``python -s -m agents.dialectic_reviewer``). It takes only the session id
  and reads its thesis and evidence from the session
  (``DIALECTIC_REVIEWER_FROM_SESSION``), and runs with the server's reviewer
  settings fixed (``src/dialectic_reviewer_config.reviewer_profile_env``):
  its host list as the server resolves it, the inference hosts file
  included, so regenerate after editing that file; the CLIs found here; and
  its lifetime cap sized from those settings.

The host lanes are subscription lanes: none binds a secret, and no API key
is inherited. The reviewer binds, by name only, the MCP bearer when the
server has one and the external reviewer host's key variable when one is
configured; the orchestrator resolves them from its own environment.

The server's settings come from ``--env-file`` and ``--launchd-plist`` (the
server's LaunchAgent, whose ``EnvironmentVariables`` it reads). Only the
names this script uses are taken from either, and a credential only as
present or absent: a secrets file named by mistake contributes nothing else.

The output is deterministic (sorted keys), written atomically with mode 0600
into a directory this user owns and nobody else can write, and left untouched
when unchanged. ``--check`` exits 1 when the file on disk differs from a fresh
generation, without writing.
"""

from __future__ import annotations

import argparse
import json
import os
import pwd
import stat
import sys
import tempfile
from pathlib import Path
from typing import Any

SCHEMA = "unitares.orchestrator_profiles.v1"

#: The proposal's model pattern; the orchestrator anchors it.
MODEL_PATTERN = r"[A-Za-z0-9][A-Za-z0-9._:/\[\]-]{0,127}"

#: The prompt travels to the client in its environment. Linux caps one
#: environment string at MAX_ARG_STRLEN, 131072 bytes including the name,
#: the "=" and the terminator, so a larger prompt would pass the profile and
#: then fail process creation with E2BIG. Under the orchestrator's own
#: per-variable ceiling (profiles.ex @max_var_bytes, 262144).
PROMPT_MAX_BYTES = 131_072 - len("HA_PROMPT=") - 1

#: delegate_inference accepts timeout_s up to 420, and the host adapter adds
#: a 30 s backstop (host_adapter.ORCHESTRATOR_BACKSTOP_GRACE_S).
HOST_MAX_RUNTIME_MS = 450_000

#: agy takes the prompt as one argument, which its client caps at 120000
#: bytes after adding a 38-byte guard line and a 145-byte text-only note
#: (antigravity_cli_client.PROMPT_BYTES_LIMIT, guard_prompt, TEXT_ONLY).
AGY_PROMPT_MAX_BYTES = 120_000 - 38 - 145

CLAUDE_CLIENT = "src/mcp_handlers/support/claude_cli_client.py"
CODEX_CLIENT = "src/mcp_handlers/support/codex_app_server_client.py"
AGY_CLIENT = "src/mcp_handlers/support/antigravity_cli_client.py"
CLIENTS = (CLAUDE_CLIENT, CODEX_CLIENT, AGY_CLIENT)

REVIEWER_MODULE = "agents/dialectic_reviewer/reviewer.py"
REVIEWER_SDK = "agents/sdk/src"
#: Dialectic session ids are 16 hex characters (DialecticSession); the
#: pattern leaves room without admitting anything but an id, and never a
#: leading "-".
SESSION_ID_PATTERN = "[A-Za-z0-9][A-Za-z0-9_-]{0,63}"

#: host id -> (CLI name, its override in the server's environment file).
CLIS = {
    "claude": ("claude", "UNITARES_CLAUDE_CLI"),
    "codex": ("codex", "UNITARES_CODEX_CLI"),
    "antigravity": ("agy", "UNITARES_ANTIGRAVITY_CLI"),
}


def read_env_files(paths: list[str]) -> dict[str, str]:
    """``KEY=VALUE`` lines (``export`` and matching quotes allowed)."""
    values: dict[str, str] = {}
    for path in paths:
        try:
            text = Path(path).read_text(encoding="utf-8")
        except OSError as exc:
            raise SystemExit(f"cannot read env file {path}: {exc.strerror}") from None
        for line in text.splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            if line.startswith("export "):
                line = line[len("export "):].lstrip()
            key, sep, value = line.partition("=")
            if not sep:
                continue
            value = value.strip()
            if len(value) >= 2 and value[0] == value[-1] and value[0] in "'\"":
                value = value[1:-1]
            values[key.strip()] = value
    return values


def read_plist_env(path: str) -> dict[str, str]:
    """A LaunchAgent's ``EnvironmentVariables``."""
    import plistlib

    try:
        with open(path, "rb") as handle:
            document = plistlib.load(handle)
    except (OSError, plistlib.InvalidFileException, ValueError) as exc:
        raise SystemExit(f"cannot read launchd plist {path}: {type(exc).__name__}") from None
    env = document.get("EnvironmentVariables") if isinstance(document, dict) else None
    return {str(k): str(v) for k, v in env.items()} if isinstance(env, dict) else {}


def server_settings(sources: list[dict[str, str]], names: tuple[str, ...],
                    presence_only: tuple[str, ...]) -> dict[str, str]:
    """The settings this script uses, later sources winning. A name in
    ``presence_only`` keeps only whether it is set ("1"), never its value;
    every other name is dropped."""
    settings: dict[str, str] = {}
    for source in sources:
        for name in names:
            if name in source:
                settings[name] = source[name]
        for name in presence_only:
            if name in source:
                settings[name] = "1" if source[name] else ""
    return settings


def _executable(path: Path) -> bool:
    return path.is_file() and os.access(path, os.X_OK)


def find_cli(name: str, override: str, home: str) -> str | None:
    """The CLI's own path (a symlink stays a symlink), or None."""
    if override:
        candidate = Path(os.path.expanduser(override))
        if not candidate.is_absolute():
            raise SystemExit(f"the {name} CLI override must be an absolute path: {override}")
        return os.path.abspath(candidate) if _executable(candidate) else None
    for directory in (Path(home) / ".local" / "bin", Path("/opt/homebrew/bin"),
                      Path("/usr/local/bin"), Path("/usr/bin")):
        candidate = directory / name
        if _executable(candidate):
            return os.path.abspath(candidate)
    return None


def host_profile(*, repo_root: str, python: str, client: str, cli: str, user: str,
                 inherit: list[str], fixed: dict[str, str], prompt_max: int,
                 timeout_var: bool) -> dict[str, Any]:
    """A host adapter lane: the caller chooses the prompt and the model only.

    ``USER`` is fixed because a subscription CLI finds its keychain login by
    username, and a service manager may not pass it."""
    variables: dict[str, Any] = {
        "HA_PROMPT": {"max_bytes": prompt_max, "required": True},
        "HA_MODEL": {"max_bytes": 128, "pattern": MODEL_PATTERN},
    }
    if timeout_var:
        variables["HA_TIMEOUT_S"] = {"max_bytes": 3, "pattern": "[1-9][0-9]{0,2}"}
    return {
        "cmd": python,
        "args": ["-I", os.path.join(repo_root, client)],
        "env": {"inherit": inherit, "set": {"HA_CLI": cli, "USER": user, **fixed}},
        "vars": variables,
        "lineage": "none",
        "lease": False,
        "max_runtime_ms": HOST_MAX_RUNTIME_MS,
        "max_output_lines": 1000,
        "max_concurrent": 3,
        "max_per_hour": 60,
    }


def claude_profile(*, repo_root: str, python: str, cli: str, user: str) -> dict[str, Any]:
    return host_profile(repo_root=repo_root, python=python, client=CLAUDE_CLIENT, cli=cli,
                        user=user, inherit=["HOME", "PATH", "LANG", "TMPDIR"], fixed={},
                        prompt_max=PROMPT_MAX_BYTES, timeout_var=True)


def codex_profile(*, repo_root: str, python: str, cli: str, user: str,
                  app_server: bool) -> dict[str, Any]:
    # CODEX_HOME, where set, is where the subscription login lives. No
    # deadline variable: the orchestrator's max_runtime_ms is the backstop,
    # as on the raw path.
    return host_profile(
        repo_root=repo_root, python=python, client=CODEX_CLIENT, cli=cli, user=user,
        inherit=["HOME", "PATH", "LANG", "TMPDIR", "CODEX_HOME"],
        fixed={"HA_SANDBOX": "read-only", "HA_CODEX_EXEC_FALLBACK": "1",
               "HA_CODEX_APP_SERVER": "1" if app_server else "0"},
        prompt_max=PROMPT_MAX_BYTES, timeout_var=False)


def antigravity_profile(*, repo_root: str, python: str, cli: str, user: str) -> dict[str, Any]:
    # XDG_RUNTIME_DIR is where a Linux session bus, and so a keyring login,
    # is found; the client gives agy an isolated HOME of its own.
    return host_profile(
        repo_root=repo_root, python=python, client=AGY_CLIENT, cli=cli, user=user,
        inherit=["HOME", "PATH", "LANG", "TMPDIR", "XDG_RUNTIME_DIR"], fixed={},
        prompt_max=AGY_PROMPT_MAX_BYTES, timeout_var=True)


def reviewer_profile(*, repo_root: str, python: str, user: str, fixed: dict[str, str],
                     secrets: dict[str, str], max_runtime_ms: int,
                     clis: dict[str, str]) -> dict[str, Any]:
    """The dialectic reviewer: a session id in, everything else fixed.

    ``fixed`` and ``secrets`` come from reviewer_profile_env; ``clis`` pins the
    CLI each host backend runs, by its override variable."""
    env_set = {
        **fixed,
        **clis,
        "DIALECTIC_REVIEWER_FROM_SESSION": "1",
        "PYTHONPATH": os.pathsep.join([repo_root, os.path.join(repo_root, REVIEWER_SDK)]),
        "USER": user,
    }
    profile: dict[str, Any] = {
        # -s: no user site-packages (.pth files). Not -I: the reviewer imports
        # this checkout and its SDK through PYTHONPATH, which the profile fixes.
        "cmd": python,
        "args": ["-s", "-m", "agents.dialectic_reviewer"],
        "cd": repo_root,
        "env": {
            "inherit": ["HOME", "PATH", "LANG", "TMPDIR", "XDG_RUNTIME_DIR", "CODEX_HOME"],
            "set": env_set,
        },
        "vars": {
            "DIALECTIC_SESSION_ID": {"max_bytes": 64, "pattern": SESSION_ID_PATTERN,
                                     "required": True},
        },
        # The reviewer onboards with the session's paused agent as its parent,
        # read from the session record, never from the caller.
        "lineage": "none",
        "max_runtime_ms": max_runtime_ms,
        "max_output_lines": 2000,
        "max_concurrent": 3,
        "max_per_hour": 20,
    }
    if secrets:
        profile["env"]["secrets"] = secrets
    return profile


def build_profiles(*, repo_root: str, python: str, user: str, claude_cli: str | None,
                   codex_cli: str | None = None, antigravity_cli: str | None = None,
                   codex_app_server: bool = True,
                   reviewer: dict[str, Any] | None = None) -> dict[str, Any]:
    """``reviewer``: keyword arguments for reviewer_profile beyond the common
    ones, or None to omit it."""
    common = {"repo_root": repo_root, "python": python, "user": user}
    profiles: dict[str, Any] = {}
    if reviewer is not None:
        profiles["dialectic.reviewer"] = reviewer_profile(**common, **reviewer)
    if claude_cli:
        profiles["host.claude"] = claude_profile(cli=claude_cli, **common)
    if codex_cli:
        profiles["host.codex"] = codex_profile(cli=codex_cli, app_server=codex_app_server,
                                               **common)
    if antigravity_cli:
        profiles["host.antigravity"] = antigravity_profile(cli=antigravity_cli, **common)
    return {"schema": SCHEMA, "profiles": profiles}


def flag_on(value: str | None) -> bool:
    """host_adapter.codex_app_server_instrumentation_enabled's reading."""
    return (value or "1").strip().lower() not in ("0", "false", "no", "off")


def render(document: dict[str, Any]) -> str:
    return json.dumps(document, indent=2, sort_keys=True) + "\n"


def _private_dir(directory: Path) -> None:
    """Create it 0700 if missing; refuse one another user owns or can write."""
    if not directory.exists():
        directory.mkdir(mode=0o700, parents=True)
    info = directory.stat()
    if info.st_uid != os.getuid():
        raise SystemExit(f"{directory} is owned by another user")
    if info.st_mode & (stat.S_IWGRP | stat.S_IWOTH):
        raise SystemExit(f"{directory} is writable by group or others")


def _private_file(path: Path) -> bool:
    """A regular file (not a link) this user owns that nobody else can write:
    what the orchestrator accepts (profiles.ex owned_and_private/3)."""
    try:
        info = path.lstat()
    except FileNotFoundError:
        return False
    return (stat.S_ISREG(info.st_mode) and info.st_uid == os.getuid()
            and not info.st_mode & (stat.S_IWGRP | stat.S_IWOTH))


def write_atomically(path: Path, text: str) -> bool:
    """Write ``text`` to ``path`` (0600) unless it is already there, private.

    A file with the right text but the wrong owner or mode is rewritten:
    the orchestrator would refuse it.
    """
    _private_dir(path.parent)
    if _private_file(path) and path.read_text(encoding="utf-8") == text:
        return False
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(tmp, 0o600)
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise
    return True


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--out", required=True, help="the profiles file to write")
    parser.add_argument("--repo-root", default=str(Path(__file__).resolve().parents[2]))
    parser.add_argument("--python", default=sys.executable)
    parser.add_argument("--env-file", action="append", default=[])
    parser.add_argument("--launchd-plist", action="append", default=[],
                        help="the server's LaunchAgent plist, for its EnvironmentVariables")
    parser.add_argument("--check", action="store_true",
                        help="exit 1 if the file differs from a fresh generation; write nothing")
    args = parser.parse_args(argv)

    repo_root = os.path.abspath(args.repo_root)
    for client in (*CLIENTS, REVIEWER_MODULE):
        if not os.path.isfile(os.path.join(repo_root, client)):
            raise SystemExit(f"{repo_root} is not a UNITARES checkout (no {client})")
    # The deployment tree's own resolution, so the profile matches the server
    # that runs from it. Light: it imports only src.inference_config.
    sys.path.insert(0, repo_root)
    from src import dialectic_reviewer_config as reviewer_config
    python = os.path.abspath(args.python)
    if not _executable(Path(python)):
        raise SystemExit(f"{python} is not an executable interpreter")

    account = pwd.getpwuid(os.getuid())
    sources = [read_env_files([path]) for path in args.env_file]
    sources += [read_plist_env(path) for path in args.launchd_plist]
    names = (*(override for _, override in CLIS.values()),
             "UNITARES_CODEX_APP_SERVER_INSTRUMENTATION", "UNITARES_GOVERNANCE_URL",
             "GOVERNANCE_URL", "UNITARES_INFERENCE_HOSTS_FILE",
             *reviewer_config.REVIEWER_PROFILE_SETTINGS)
    settings = server_settings(sources, names, (reviewer_config.MCP_BEARER_ENV,))
    found: dict[str, str | None] = {}
    for host, (name, override) in CLIS.items():
        found[host] = find_cli(name, settings.get(override, "").strip(), account.pw_dir)
        if found[host] is None:
            print(f"orchestrator-profiles: no {name} CLI found; host.{host} omitted",
                  file=sys.stderr)

    try:
        fixed, secrets = reviewer_config.reviewer_profile_env(settings)
    except ValueError as exc:
        raise SystemExit(f"orchestrator-profiles: {exc}") from None
    reviewer = {
        "fixed": fixed,
        "secrets": secrets,
        "max_runtime_ms": reviewer_config.reviewer_max_runtime_ms(settings),
        "clis": {override: found[host] for host, (_, override) in CLIS.items() if found[host]},
    }

    text = render(build_profiles(
        repo_root=repo_root, python=python, user=account.pw_name,
        claude_cli=found["claude"], codex_cli=found["codex"],
        antigravity_cli=found["antigravity"],
        codex_app_server=flag_on(settings.get("UNITARES_CODEX_APP_SERVER_INSTRUMENTATION")),
        reviewer=reviewer))
    out = Path(os.path.abspath(args.out))
    if args.check:
        try:
            same = out.read_text(encoding="utf-8") == text
        except FileNotFoundError:
            same = False
        print(f"orchestrator-profiles: {out} is {'current' if same else 'stale'}")
        return 0 if same else 1
    changed = write_atomically(out, text)
    print(f"orchestrator-profiles: {out} {'written' if changed else 'unchanged'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
