#!/usr/bin/env python3
"""Name the model UNITARES uses for consult and the in-process dialectic reviewer.

Run from a Docker Compose install (``make setup-model``). It asks the local
Ollama which models are pulled, lets you pick one, writes the two settings to
``.env`` and rebuilds the server, then checks that the server can reach the
model. Without a model, consult answers "Standard advisory consultation is
unavailable" and a review waits for a peer or the operator.

    python3 scripts/install/choose_model.py                  # interactive
    python3 scripts/install/choose_model.py --model qwen3:8b --yes
    python3 scripts/install/choose_model.py --no-docker      # print settings for a source install
    python3 scripts/install/choose_model.py --clear          # remove the settings from .env

Stdlib only, so it runs before anything is installed. It edits one file
(``.env``, only the two lines it owns) and, unless ``--no-rebuild``, runs
``docker compose up -d --build governance-mcp``.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import urllib.error
import urllib.request
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
BASE_KEY = "UNITARES_OLLAMA_BASE"
MODEL_KEY = "UNITARES_LLM_MODEL"
ALIAS_KEY = "UNITARES_OLLAMA_BASE_URL"
# How this script reaches Ollama by default (it runs on the host). The server,
# in the container, reaches the same endpoint through container_base().
HOST_OLLAMA = "http://localhost:11434"
PREFERRED_MODEL = "gemma4:latest"


def ollama_root(url: str) -> str:
    """An Ollama URL reduced to its root: no surrounding space, trailing ``/`` or ``/v1``."""
    url = url.strip().rstrip("/")
    if url.endswith("/v1"):
        url = url[: -len("/v1")].rstrip("/")
    return url


def list_ollama_models(base: str, timeout: float = 3.0) -> list[str] | None:
    """Model names pulled into the Ollama at ``base``, or None if it did not answer."""
    try:
        with urllib.request.urlopen(ollama_root(base) + "/api/tags", timeout=timeout) as resp:
            payload = json.load(resp)
    except (urllib.error.URLError, OSError, ValueError):
        return None
    models = payload.get("models") if isinstance(payload, dict) else None
    if not isinstance(models, list):
        return None
    names = [m.get("name") for m in models if isinstance(m, dict) and isinstance(m.get("name"), str)]
    return sorted(set(names))


def container_base(host_url: str) -> str:
    """The same Ollama as the server in the container sees it.

    Inside the container, localhost is the container itself, so a host-local
    address becomes host.docker.internal on the same scheme and port. Any other
    host is already reachable by name and is kept as given. A trailing ``/`` or
    ``/v1`` is dropped: the setting is the root URL.
    """
    parts = urlsplit(ollama_root(host_url))
    if parts.hostname in ("localhost", "127.0.0.1", "::1"):
        port = f":{parts.port}" if parts.port else ""
        parts = parts._replace(netloc=f"host.docker.internal{port}")
    return urlunsplit(parts)


def default_choice(models: list[str]) -> str:
    """The model offered first: the documented default if pulled, else the first listed."""
    return PREFERRED_MODEL if PREFERRED_MODEL in models else models[0]


def update_env_text(text: str, settings: dict[str, str | None]) -> str:
    """Set or remove KEY=value lines in .env text, keeping every other line.

    A value of None removes the key. Only uncommented ``KEY=`` lines are touched;
    a commented example (``# KEY=...``) is left as it is. A key that is not
    present is appended.
    """
    lines = text.splitlines()
    out: list[str] = []
    seen: set[str] = set()
    for line in lines:
        key = line.split("=", 1)[0].strip() if "=" in line and not line.lstrip().startswith("#") else None
        if key in settings:
            if key in seen or settings[key] is None:
                continue  # drop duplicates, and removed keys
            out.append(f"{key}={settings[key]}")
            seen.add(key)
        else:
            out.append(line)
    missing = [k for k, v in settings.items() if v is not None and k not in seen]
    if missing:
        if out and out[-1].strip():
            out.append("")
        out.append("# Model for consult and dialectic reviews (scripts/install/choose_model.py)")
        out.extend(f"{k}={settings[k]}" for k in missing)
    return "\n".join(out) + "\n"


def read_env_value(text: str, key: str) -> str | None:
    for line in text.splitlines():
        if line.lstrip().startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        if k.strip() == key:
            return v.strip()
    return None


def ask(prompt: str, default: str, on_eof: str | None = None) -> str:
    """Prompt; Enter takes ``default``. End of input (Ctrl-D) returns ``on_eof``,
    which defaults to ``default`` but should be the safe answer for a yes/no."""
    try:
        answer = input(prompt).strip()
    except EOFError:
        print()
        return default if on_eof is None else on_eof
    return answer or default


def pick_model(models: list[str], requested: str | None, assume_yes: bool) -> str | None:
    if requested:
        if requested not in models:
            print(f"✗ {requested} is not pulled. Pull it first: ollama pull {requested}")
            return None
        return requested
    fallback = default_choice(models)
    if assume_yes or not sys.stdin.isatty():
        return fallback
    print("Models pulled into Ollama:")
    for i, name in enumerate(models, 1):
        print(f"  {i}. {name}{'   (default)' if name == fallback else ''}")
    answer = ask(f"Which model should UNITARES use? [1-{len(models)}, Enter for {fallback}] ", "")
    if not answer:
        return fallback
    if answer.isdigit() and 1 <= int(answer) <= len(models):
        return models[int(answer) - 1]
    if answer in models:
        return answer
    print(f"✗ {answer!r} is not one of the listed models.")
    return None


def compose(args: list[str], cwd: Path) -> subprocess.CompletedProcess:
    return subprocess.run(["docker", "compose", *args], cwd=cwd, text=True, capture_output=True)


def server_reaches_model(cwd: Path) -> bool:
    probe = (
        "import os,urllib.request;"
        f"urllib.request.urlopen(os.environ['{BASE_KEY}'].rstrip('/')+'/api/tags',timeout=5).read()"
    )
    return compose(["exec", "-T", "governance-mcp", "python", "-c", probe], cwd).returncode == 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--model", help="model to use (must already be pulled)")
    parser.add_argument("--yes", "-y", action="store_true", help="no prompts: take the default model and rebuild")
    parser.add_argument("--no-rebuild", action="store_true", help="write .env but do not rebuild the server")
    parser.add_argument("--no-docker", action="store_true", help="source install: print the settings instead of writing .env")
    parser.add_argument("--clear", action="store_true", help="remove the model settings from .env")
    parser.add_argument("--ollama", default=os.environ.get("OLLAMA_HOST_URL", HOST_OLLAMA), help=f"where this script finds Ollama (default {HOST_OLLAMA})")
    parser.add_argument("--env-file", type=Path, default=REPO_ROOT / ".env")
    args = parser.parse_args(argv)
    env_file: Path = args.env_file

    if args.clear:
        if env_file.exists():
            env_file.write_text(update_env_text(env_file.read_text(), {BASE_KEY: None, MODEL_KEY: None}))
        print(f"✓ Removed {BASE_KEY} and {MODEL_KEY} from {env_file}. Rebuild to apply: docker compose up -d --build governance-mcp")
        return 0

    models = list_ollama_models(args.ollama)
    if models is None:
        print(f"✗ No Ollama answered at {args.ollama}.")
        print("  Install it from https://ollama.com, start it, pull a model (for example: ollama pull gemma4:latest),")
        print("  then run this again. Without a model, consult is unavailable and reviews wait for a peer.")
        return 1
    if not models:
        print(f"✗ Ollama at {args.ollama} has no models. Pull one (for example: ollama pull {PREFERRED_MODEL}) and run this again.")
        return 1

    model = pick_model(models, args.model, args.yes)
    if model is None:
        return 1

    if args.no_docker:
        print("✓ Source install: set these in the server's environment (shell, or the launchd plist), then restart it:")
        print(f"  {BASE_KEY}={ollama_root(args.ollama)}")
        print(f"  {MODEL_KEY}={model}")
        return 0

    server_base = container_base(args.ollama)
    before = env_file.read_text() if env_file.exists() else ""
    env_file.write_text(update_env_text(before, {BASE_KEY: server_base, MODEL_KEY: model}))
    print(f"✓ Wrote {BASE_KEY}={server_base} and {MODEL_KEY}={model} to {env_file}")
    alias = read_env_value(before, ALIAS_KEY)
    if alias:
        print(f"  Note: {env_file.name} also sets {ALIAS_KEY}={alias}. {BASE_KEY} takes precedence; remove the other line to avoid confusion.")

    if args.no_rebuild:
        print("  Rebuild to apply: docker compose up -d --build governance-mcp")
        return 0
    if not (args.yes or not sys.stdin.isatty()):
        if ask("Rebuild and restart the server now? [Y/n] ", "y", on_eof="n").lower() not in ("y", "yes"):
            print("  Rebuild to apply: docker compose up -d --build governance-mcp")
            return 0

    print("… Rebuilding the server (docker compose up -d --build --wait governance-mcp)")
    result = compose(["up", "-d", "--build", "--wait", "governance-mcp"], env_file.parent)
    if result.returncode != 0:
        print("✗ The rebuild failed:")
        print((result.stderr or result.stdout).strip()[-2000:])
        return result.returncode or 1

    if server_reaches_model(env_file.parent):
        print(f"✓ The server reaches {model}. consult and dialectic reviews will use it.")
        return 0
    print(f"✗ The server cannot reach Ollama at {server_base}.")
    if sys.platform.startswith("linux"):
        print("  On Linux, Ollama listens only on 127.0.0.1 by default. Set OLLAMA_HOST=0.0.0.0 for the Ollama service,")
        print("  allow port 11434 from the Docker bridge, and do not expose it beyond this machine (Ollama has no authentication).")
    else:
        print("  Check that Ollama is running on this machine, then run: make setup-model")
    return 1


if __name__ == "__main__":
    sys.exit(main())
