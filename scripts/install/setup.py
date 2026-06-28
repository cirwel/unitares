#!/usr/bin/env python3
"""Guided UNITARES install wizard.

Runs scripts/dev/unitares_doctor.py for diagnosis, prints remediation commands
for any failing checks, scaffolds ~/.unitares/ and ~/.config/cirwel/secrets.env
under --apply, and generates copy-pasteable stdio MCP snippets for detected
clients (Claude Code, Codex, Gemini CLI, Copilot CLI).

Setup PRINTS commands. It does NOT install postgres, run SQL, invoke brew, or
modify MCP client config files. The two filesystem mutations under --apply are
bounded exceptions: scaffolding ~/.unitares/ and ~/.config/cirwel/secrets.env.

Usage:
    python3 scripts/install/setup.py            # interactive, dry-run
    python3 scripts/install/setup.py --apply    # mutate the two paths
    python3 scripts/install/setup.py --json     # machine-readable plan
    python3 scripts/install/setup.py --apply --non-interactive --json
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from dataclasses import dataclass, asdict
from pathlib import Path

SCHEMA_VERSION = 1

# REPO_ROOT derivation. The script lives at scripts/install/setup.py, so
# Path(__file__).resolve().parent.parent.parent is the repo root. This pattern
# matches scripts/dev/unitares_doctor.py and is robust against `cd`, symlinks,
# and worktrees. Do not "simplify" to os.getcwd() — that breaks when the user
# runs the script from any cwd other than the repo root.
REPO_ROOT = Path(__file__).resolve().parent.parent.parent
DOCTOR_SCRIPT = REPO_ROOT / "scripts" / "dev" / "unitares_doctor.py"


class DoctorError(RuntimeError):
    """Raised when the doctor subprocess emits something we cannot parse.
    Nonzero exit codes from doctor are NOT errors — failed checks are normal.
    Only invalid JSON or a missing executable raise this.
    """


@dataclass
class PlanItem:
    """One actionable item in the wizard's plan. Phase 1 items are
    remediation commands the user copy-pastes; phase 2 items are filesystem
    operations setup will perform under --apply; phase 3 items are
    MCP-client snippets the user pastes into their own config files.
    """
    phase: int  # 1 = remediation, 2 = mkdir/file, 3 = snippet
    kind: str   # "remediation" | "mkdir" | "file" | "snippet"
    finding: str = ""        # phase 1: doctor finding name
    command: str = ""        # phase 1: shell command to run
    path: str = ""           # phase 2: filesystem target
    mode: str = ""           # phase 2: octal mode as string
    client: str = ""         # phase 3: client name (claude_code, codex, etc.)
    config_path: str = ""    # phase 3: target config file
    snippet: str = ""        # phase 3: copy-paste payload
    applied: bool = False
    note: str = ""           # human-readable note (e.g., superuser caveat)


# Remediation commands keyed by doctor finding name. Lookup misses fall through
# to a generic "see doctor output" message. The schema-migrations entry is
# computed at runtime because it depends on which migration files exist.
_REMEDIATIONS = {
    "postgres_running":
        "brew install postgresql@17 && brew services start postgresql@17",
    "governance_database":
        "createdb -h localhost -U postgres governance",
    "pg_extensions":
        "psql -U postgres -d governance -f db/postgres/init-extensions.sql",
    "secrets_file":  # mode-fail variant; the missing-file variant is phase 2
        "chmod 600 ~/.config/cirwel/secrets.env",
    "anchor_directory":
        "mkdir -m 700 ~/.unitares",
}

_REMEDIATION_NOTES = {
    "pg_extensions":
        "AGE + pgvector require superuser. The -U postgres is intentional; "
        "do not substitute -U $USER on a typical local install.",
}


def build_remediation(doctor_payload: dict) -> list[PlanItem]:
    """For each fail/warn in the doctor payload, emit a PlanItem with a
    remediation command. pass results are skipped (no action needed).
    """
    items: list[PlanItem] = []
    for r in doctor_payload.get("results", []):
        if r["status"] not in ("fail", "warn"):
            continue
        name = r["name"]
        if name == "schema_migrations":
            command = _build_migrations_command()
        else:
            command = _REMEDIATIONS.get(
                name,
                f"# No automated remediation for '{name}'. See doctor output: {r['message']}",
            )
        items.append(PlanItem(
            phase=1,
            kind="remediation",
            finding=name,
            command=command,
            note=_REMEDIATION_NOTES.get(name, ""),
        ))
    return items


def _build_migrations_command() -> str:
    """List the SQL files in db/postgres/migrations/ in lexical order, plus
    the canonical schema files. The user runs them in order with psql.
    """
    migrations_dir = REPO_ROOT / "db" / "postgres" / "migrations"
    pieces = [
        "psql -U postgres -d governance -f db/postgres/schema.sql",
        "psql -U postgres -d governance -f db/postgres/knowledge_schema.sql",
    ]
    if migrations_dir.is_dir():
        for sql in sorted(migrations_dir.glob("*.sql")):
            rel = sql.relative_to(REPO_ROOT)
            pieces.append(f"psql -U postgres -d governance -f {rel}")
    return " && \\\n    ".join(pieces)


SECRETS_TEMPLATE = """\
# UNITARES external secrets — mode 0600, never commit.
# Used by handlers that call out to LLM providers.
# ANTHROPIC_API_KEY=
# OPENAI_API_KEY=
"""


def ensure_anchor_dir(path: Path, apply: bool) -> PlanItem:
    """Plan/apply creation of the agent anchor directory.

    Default mkdir mode is umask-dependent and on a typical Mac dev machine
    (umask 022) yields 0o755 — world-readable. Anchor dir holds session state;
    explicit 0o700 is correct.
    """
    item = PlanItem(
        phase=2,
        kind="mkdir",
        path=str(path),
        mode="0o700",
        applied=False,
    )
    if path.is_dir():
        return item  # already exists; nothing to do
    if not apply:
        return item  # dry run; report only
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    item.applied = True
    return item


def ensure_secrets_file(path: Path, apply: bool) -> PlanItem:
    """Plan/apply scaffolding of ~/.config/cirwel/secrets.env.

    Creates parent directories if needed (e.g., ~/.config/cirwel/). Writes
    a commented template at mode 0o600. Never overwrites an existing file —
    the doctor flags wrong-mode separately, and we do not want to lose
    the user's keys.
    """
    item = PlanItem(
        phase=2,
        kind="file",
        path=str(path),
        mode="0o600",
        applied=False,
    )
    if path.exists():
        return item  # do not overwrite
    if not apply:
        return item  # dry run
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(SECRETS_TEMPLATE)
    os.chmod(path, 0o600)
    item.applied = True
    return item


# Client detection table. Detection path = directory whose existence implies
# the client is installed. config_path = where the user pastes the snippet.
# format = "json" or "toml" — controls render shape in build_snippet().
# These paths were sourced from each client's documented config locations as
# of 2026-04-25; the copilot path is speculative and prints a TODO instead
# of a real snippet (see build_snippet() handling).
_CLIENT_TABLE = {
    "claude_code": {
        "detect_subpath": ".claude",
        "config_subpath": ".claude/settings.json",
        "format": "json",
    },
    "codex": {
        "detect_subpath": ".codex",
        "config_subpath": ".codex/config.toml",
        "format": "toml",
    },
    "gemini": {
        "detect_subpath": ".config/gemini",
        "config_subpath": ".config/gemini/settings.json",
        "format": "json",
    },
    "copilot": {
        "detect_subpath": ".config/github-copilot-cli",
        "config_subpath": ".config/github-copilot-cli/config.json",
        "format": "todo",  # speculative — emits a note, not a snippet
    },
}


def detect_clients(home: Path) -> dict[str, dict]:
    """Probe the user's home directory for installed MCP clients.
    Returns {client_name: {"config_path": "...", "format": "..."}}.
    Clients whose detect path is missing are silently skipped.
    """
    out: dict[str, dict] = {}
    for client, entry in _CLIENT_TABLE.items():
        detect_path = home / entry["detect_subpath"]
        if detect_path.is_dir():
            out[client] = {
                "config_path": str(home / entry["config_subpath"]),
                "format": entry["format"],
            }
    return out


DEFAULT_DB_URL = "postgresql://postgres:postgres@localhost:5432/governance"


def build_snippet(
    client: str,
    config_path: str,
    fmt: str,
    repo_root: Path,
    proxy_url: str | None,
) -> PlanItem:
    """Render a copy-pasteable MCP server entry for one client.

    The snippet points at src/mcp_server_std.py (stdio transport, local mode).
    If proxy_url is set, an additional UNITARES_STDIO_PROXY_HTTP_URL env entry
    is added so the stdio process forwards to a remote HTTP governance server.
    """
    server_path = str(repo_root / "src" / "mcp_server_std.py")
    env: dict[str, str] = {"DB_POSTGRES_URL": DEFAULT_DB_URL}
    if proxy_url:
        env["UNITARES_STDIO_PROXY_HTTP_URL"] = proxy_url

    if fmt == "todo":
        snippet = (
            f"# TODO: Copilot CLI MCP config format is speculative as of 2026-04-25.\n"
            f"# Verify the actual config schema before pasting. Equivalent payload:\n"
            f"#   command: python3\n"
            f"#   args:    [{server_path}]\n"
            f"#   env:     {dict(env)}"
        )
    elif fmt == "json":
        snippet = json.dumps(
            {
                "unitares-governance": {
                    "command": "python3",
                    "args": [server_path],
                    "env": env,
                }
            },
            indent=2,
        )
    elif fmt == "toml":
        # TOML strings: escape backslashes and double quotes. Mac paths rarely
        # contain either, but a path with a literal quote would otherwise break
        # the user's config silently when they paste.
        def _toml_str(s: str) -> str:
            return s.replace("\\", "\\\\").replace('"', '\\"')

        env_lines = "\n".join(
            f'{k} = "{_toml_str(v)}"' for k, v in env.items()
        )
        snippet = (
            f"[mcp_servers.unitares-governance]\n"
            f'command = "python3"\n'
            f'args = ["{_toml_str(server_path)}"]\n\n'
            f"[mcp_servers.unitares-governance.env]\n"
            f"{env_lines}\n"
        )
    else:
        raise ValueError(f"unknown snippet format: {fmt!r}")

    return PlanItem(
        phase=3,
        kind="snippet",
        client=client,
        config_path=config_path,
        snippet=snippet,
    )


def run_pipeline(
    *,
    apply: bool,
    home: Path,
    proxy_url: str | None,
) -> dict:
    """Execute all five phases. Returns a dict shaped like the --json schema.

    Phase 1 always runs (read-only doctor + remediation generation).
    Phase 2 emits items for the two filesystem targets; mutates only with
    apply=True and only when targets are missing.
    Phase 3 detects clients and emits snippets (always print-only).
    Phase 4 re-runs doctor IFF apply=True (no point re-running in dry mode).
    """
    initial = run_doctor()
    plan: list[PlanItem] = []

    # Phase 1: remediation for fail/warn doctor results.
    plan.extend(build_remediation(initial))

    # Phase 2: filesystem scaffolding.
    plan.append(ensure_anchor_dir(home / ".unitares", apply=apply))
    # Secrets file location is overridable via UNITARES_SECRETS_ENV so a fresh
    # operator can scaffold outside the default ~/.config/cirwel/ path.
    _secrets_override = os.environ.get("UNITARES_SECRETS_ENV")
    secrets_path = (
        Path(_secrets_override).expanduser()
        if _secrets_override
        else home / ".config" / "cirwel" / "secrets.env"
    )
    plan.append(ensure_secrets_file(secrets_path, apply=apply))

    # Phase 3: client detection + snippet generation.
    detected = detect_clients(home)
    repo_root = REPO_ROOT
    for client, info in detected.items():
        plan.append(build_snippet(
            client=client,
            config_path=info["config_path"],
            fmt=info["format"],
            repo_root=repo_root,
            proxy_url=proxy_url,
        ))

    # Phase 4: re-run doctor only when we actually mutated something.
    final = run_doctor() if apply else None

    # exit_code: 0 only if there are no fails. On --apply, gate on the post-
    # mutation doctor. On dry-run, gate on the initial doctor — this makes
    # `setup.py --json --non-interactive` usable as a CI gate that catches
    # failing checks even when no remediation was attempted.
    gate_payload = final if final is not None else initial
    has_fail = any(r["status"] == "fail" for r in gate_payload.get("results", []))
    exit_code = 1 if has_fail else 0

    return {
        "schema_version": SCHEMA_VERSION,
        "doctor_initial": initial,
        "plan": plan,
        "doctor_final": final,
        "exit_code": exit_code,
    }


def bootstrap_check() -> None:
    """Verify the MCP SDK is importable. Setup is not stdlib-only — it shares
    the server's runtime deps. If mcp is missing, exit early with the canonical
    install command before doing any work.
    """
    try:
        import mcp  # noqa: F401
    except ImportError:
        print(
            "MCP SDK not found. Run:\n"
            "    pip install -r requirements-full.txt",
            file=sys.stdout,
        )
        sys.exit(2)


def run_doctor() -> dict:
    """Spawn unitares_doctor.py --json --mode=local and return the parsed
    payload. A nonzero exit code is normal (any local check failed); the
    payload is still complete. Only invalid JSON raises DoctorError.
    """
    proc = subprocess.run(
        [sys.executable, str(DOCTOR_SCRIPT), "--json", "--mode=local"],
        capture_output=True,
        text=True,
        timeout=30,
    )
    try:
        return json.loads(proc.stdout)
    except json.JSONDecodeError as e:
        raise DoctorError(
            f"doctor emitted invalid JSON (rc={proc.returncode}); "
            f"stdout[:200]={proc.stdout[:200]!r}; stderr={proc.stderr[:200]!r}"
        ) from e


def render_text(result: dict, use_color: bool) -> str:
    """Human-readable rendering of the wizard plan + final doctor pass.
    Color codes match doctor's so the eye can scan both outputs the same way.
    """
    fail_color, warn_color, reset = (
        ("\033[31m", "\033[33m", "\033[0m") if use_color
        else ("", "", "")
    )
    lines: list[str] = []
    lines.append("=== UNITARES setup ===")

    initial = result["doctor_initial"]
    fails = sum(1 for r in initial["results"] if r["status"] == "fail")
    warns = sum(1 for r in initial["results"] if r["status"] == "warn")
    passes = sum(1 for r in initial["results"] if r["status"] == "pass")
    lines.append(f"\nInitial doctor: {passes} pass · {fails} fail · {warns} warn")

    by_phase: dict[int, list[PlanItem]] = {}
    for item in result["plan"]:
        by_phase.setdefault(item.phase, []).append(item)

    if 1 in by_phase:
        lines.append(f"\n--- Phase 1: remediation ({fail_color}commands you need to run{reset}) ---")
        for item in by_phase[1]:
            lines.append(f"\n# {item.finding}:")
            lines.append(item.command)
            if item.note:
                lines.append(f"# Note: {item.note}")

    if 2 in by_phase:
        lines.append(f"\n--- Phase 2: filesystem scaffolding ---")
        for item in by_phase[2]:
            tag = "applied" if item.applied else ("would create" if not Path(item.path).exists() else "ok (exists)")
            lines.append(f"  {item.kind} {item.path} (mode {item.mode}) — {tag}")

    if 3 in by_phase:
        lines.append(f"\n--- Phase 3: MCP client snippets ({warn_color}paste these manually{reset}) ---")
        for item in by_phase[3]:
            lines.append(f"\n# Client: {item.client}")
            lines.append(f"# Paste into: {item.config_path}")
            lines.append(item.snippet)

    if result["doctor_final"]:
        f_fails = sum(1 for r in result["doctor_final"]["results"] if r["status"] == "fail")
        f_passes = sum(1 for r in result["doctor_final"]["results"] if r["status"] == "pass")
        lines.append(f"\nFinal doctor: {f_passes} pass · {f_fails} fail")

    lines.append("\n--- Next steps ---")
    lines.append("1. Restart your MCP client(s) to pick up the new mcpServers entry.")
    lines.append("2. (Optional, operator path) Run `python src/mcp_server.py --port 8767` to start the HTTP server.")
    lines.append("3. Verify: in Claude Code run a quick onboard(). Logs at ~/Library/Logs/Claude/mcp*.log if it errors.")
    lines.append("4. Read docs/guides/START_HERE.md for the agent-side workflow.")

    return "\n".join(lines)


def _plan_to_json_safe(plan: list[PlanItem]) -> list[dict]:
    """Convert dataclass items to dicts; only include non-empty fields so the
    JSON envelope stays readable.

    PlanItem fields default to "" (str) or False (bool); they are never None,
    0, or []. The filter `v not in ("", False)` is therefore safe — phase and
    kind are always included regardless because they're load-bearing for
    consumers regardless of value.
    """
    out: list[dict] = []
    for item in plan:
        d = asdict(item)
        out.append({
            k: v
            for k, v in d.items()
            if v not in ("", False) or k in ("phase", "kind", "applied")
        })
    return out


def main(argv: list[str] | None = None) -> int:
    bootstrap_check()

    parser = argparse.ArgumentParser(
        description="Guided UNITARES install wizard.",
    )
    parser.add_argument("--apply", action="store_true",
                        help="Mutate ~/.unitares/ and ~/.config/cirwel/secrets.env if missing.")
    parser.add_argument("--json", dest="json_out", action="store_true",
                        help="Emit machine-readable JSON; suppresses interactive prompts.")
    parser.add_argument("--non-interactive", action="store_true",
                        help="Skip the apply confirmation prompt (for CI).")
    parser.add_argument("--proxy-url", default=None,
                        help="Generate snippets that forward to a remote HTTP governance server.")
    parser.add_argument("--no-color", action="store_true")
    args = parser.parse_args(argv)

    if args.apply and not args.json_out and not args.non_interactive:
        # Show the dry-run plan first, then confirm.
        dry = run_pipeline(apply=False, home=Path.home(), proxy_url=args.proxy_url)
        print(render_text(dry, use_color=not args.no_color and sys.stdout.isatty()))
        ans = input("\nApply the filesystem mutations above? [y/N] ").strip().lower()
        if ans not in ("y", "yes"):
            print("Aborted.")
            return 1

    result = run_pipeline(
        apply=args.apply,
        home=Path.home(),
        proxy_url=args.proxy_url,
    )

    if args.json_out:
        payload = {
            "schema_version": result["schema_version"],
            "doctor_initial": result["doctor_initial"],
            "plan": _plan_to_json_safe(result["plan"]),
            "doctor_final": result["doctor_final"],
            "exit_code": result["exit_code"],
        }
        print(json.dumps(payload, indent=2))
    else:
        use_color = not args.no_color and sys.stdout.isatty()
        print(render_text(result, use_color=use_color))

    return result["exit_code"]


if __name__ == "__main__":
    sys.exit(main())
