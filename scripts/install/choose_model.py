#!/usr/bin/env python3
"""Name the model UNITARES uses for consult and the in-process dialectic reviewer.

Run from a Docker Compose install (``unitares model``). It asks the model
server which models it serves (``GET {base}/models``, which Ollama, vLLM, LM
Studio and llama.cpp's server all answer), lets you pick one, writes the two
settings to ``.env`` and rebuilds the server, then checks that the server can
reach the model. Ollama-only hints (``ollama pull``) appear only when the
server answers like Ollama. Without a model, consult answers "Standard advisory
consultation is unavailable" and a review waits for a peer or the operator.

    python3 scripts/install/choose_model.py                  # interactive
    python3 scripts/install/choose_model.py --model qwen3:8b --yes
    python3 scripts/install/choose_model.py --no-docker      # print settings for a source install
    python3 scripts/install/choose_model.py --clear          # remove the settings from .env
    python3 scripts/install/choose_model.py --base-url http://localhost:8000/v1   # another server

Stdlib only, so it runs before anything is installed. It edits one file
(``.env``, only the lines it owns: the two settings, and the older names it
wrote before, which it replaces) and, unless ``--no-rebuild``, runs
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
BASE_KEY = "UNITARES_MODEL_BASE_URL"
MODEL_KEY = "UNITARES_MODEL_ID"
# Names this script wrote before the endpoint became any OpenAI-compatible
# server. They are aliases now (src/local_inference_env.py), so writing the new
# names removes them rather than leaving two answers in .env.
OLD_KEYS = ("UNITARES_OLLAMA_BASE", "UNITARES_LLM_MODEL")
# An older name this script never wrote: reported, not removed.
ALIAS_KEY = "UNITARES_OLLAMA_BASE_URL"
# How this script reaches the model server by default (it runs on the host).
# The server, in the container, reaches the same endpoint through
# container_base().
HOST_OLLAMA = "http://localhost:11434"
PREFERRED_MODEL = "gemma4:latest"


def ollama_root(url: str) -> str:
    """An Ollama URL reduced to its root: no surrounding space, trailing ``/`` or ``/v1``."""
    url = url.strip().rstrip("/")
    if url.endswith("/v1"):
        url = url[: -len("/v1")].rstrip("/")
    return url


def openai_base(url: str) -> str:
    """An OpenAI-compatible base URL: no surrounding space or trailing ``/``, and
    ``/v1`` added only when the URL has no path (an Ollama root such as
    ``http://localhost:11434``). Mirrors src/local_inference_env.py."""
    url = url.strip().rstrip("/")
    if url and not urlsplit(url).path:
        url += "/v1"
    return url


def list_models(base: str, timeout: float = 3.0) -> list[str] | None:
    """Model ids the server at ``base`` lists on ``GET {base}/models``, or None
    if it did not answer in the OpenAI-compatible shape."""
    try:
        with urllib.request.urlopen(openai_base(base) + "/models", timeout=timeout) as resp:
            payload = json.load(resp)
    except (urllib.error.URLError, OSError, ValueError):
        return None
    data = payload.get("data") if isinstance(payload, dict) else None
    if not isinstance(data, list):
        return None
    ids = [m.get("id") for m in data if isinstance(m, dict) and isinstance(m.get("id"), str)]
    return sorted(set(ids))



def list_ollama_models(base: str, timeout: float = 3.0) -> list[str] | None:
    """Same as ``list_models``; nothing here calls it.

    Kept only because master gained it in #2496 on 2026-09-26 and the fleet
    push guard treats removing a symbol that new as a likely rebase revert.
    Delete after 2026-10-26.
    """
    return list_models(base, timeout)

def is_ollama(base: str, timeout: float = 1.0) -> bool:
    """True when ``GET {root}/api/version`` answers like Ollama. Gates the
    Ollama-only hints; every other step works for any server."""
    try:
        with urllib.request.urlopen(ollama_root(base) + "/api/version", timeout=timeout) as resp:
            payload = json.load(resp)
    except (urllib.error.URLError, OSError, ValueError):
        return False
    return isinstance(payload, dict) and isinstance(payload.get("version"), str)


def container_base(host_url: str) -> str:
    """The same endpoint as the server in the container sees it.

    Inside the container, localhost is the container itself, so a host-local
    address becomes host.docker.internal on the same scheme, port and path.
    That holds for any server, not only Ollama. Any other host is already
    reachable by name and is kept as given. The result is the
    OpenAI-compatible base, ``/v1`` included.
    """
    parts = urlsplit(openai_base(host_url))
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


def pick_model(
    models: list[str], requested: str | None, assume_yes: bool, *, ollama: bool = True
) -> str | None:
    if requested:
        if requested not in models:
            if ollama:
                print(f"✗ {requested} is not pulled. Pull it first: ollama pull {requested}")
            else:
                print(f"✗ {requested} is not one of the models the server lists.")
            return None
        return requested
    fallback = default_choice(models)
    if assume_yes or not sys.stdin.isatty():
        return fallback
    print("Models the server lists:")
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


def compose(args: list[str], env_file: Path, settings: dict[str, str]) -> subprocess.CompletedProcess:
    """Run ``docker compose`` for this checkout with the chosen .env and settings.

    Compose reads only ``.env`` beside the compose file unless told otherwise,
    and a variable exported in the calling shell outranks ``.env``. So the
    project directory is the checkout, the env file is passed explicitly, and
    the child environment carries the chosen values, not inherited ones.
    """
    env = dict(os.environ)
    for key in (*OLD_KEYS, ALIAS_KEY):
        env.pop(key, None)
    env.update(settings)
    return subprocess.run(
        ["docker", "compose", "--project-directory", str(REPO_ROOT), "--env-file", str(env_file), *args],
        cwd=REPO_ROOT, env=env, text=True, capture_output=True,
    )


def server_reaches_model(env_file: Path, settings: dict[str, str]) -> bool:
    """True when the server, from inside its container, sees the chosen model listed."""
    probe = (
        "import json,os,sys,urllib.request;"
        f"listed=json.load(urllib.request.urlopen(os.environ['{BASE_KEY}'].rstrip('/')+'/models',timeout=5));"
        f"sys.exit(0 if os.environ['{MODEL_KEY}'] in [m.get('id') for m in listed.get('data',[])] else 1)"
    )
    return compose(["exec", "-T", "governance-mcp", "python", "-c", probe], env_file, settings).returncode == 0


_CLASSIFIER_KEYS = ("UNITARES_MODEL_LOCAL_HOSTS", "UNITARES_MODEL_PRIVACY", "UNITARES_TRUSTED_NETWORKS")


def endpoint_is_local(base: str, env_values: dict[str, str | None]) -> tuple[bool, str]:
    """Classify ``base`` the way the server will, from the given settings.

    Uses the server's own classifier (``src/local_inference_env.py``, stdlib
    only) with the classifier settings the server will read, not this shell's.
    Returns (is_local, the setting that would change the answer).
    """
    if str(REPO_ROOT) not in sys.path:
        sys.path.insert(0, str(REPO_ROOT))
    from src import local_inference_env

    saved = {key: os.environ.get(key) for key in _CLASSIFIER_KEYS}
    try:
        for key in _CLASSIFIER_KEYS:
            value = env_values.get(key)
            if value:
                os.environ[key] = value
            else:
                os.environ.pop(key, None)
        verdict = local_inference_env.classify_endpoint(base)
        return verdict.is_local, verdict.reason
    finally:
        for key, value in saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


def composed_classifier_values(env_text: str) -> dict[str, str | None]:
    """The classifier settings the container will get, as ``compose()`` runs it:
    a variable exported in this shell outranks the env file, as in Compose."""
    values: dict[str, str | None] = {}
    for key in _CLASSIFIER_KEYS:
        exported = os.environ.get(key)
        values[key] = exported if exported is not None else read_env_value(env_text, key)
    return values


def print_privacy_note(base: str, env_values: dict[str, str | None]) -> bool:
    """Warn when the endpoint will classify external; True when it is local."""
    is_local, reason = endpoint_is_local(base, env_values)
    if not is_local:
        print(f"⚠ The server will treat {base} as external, not the operator's own machine.")
        print("  consult with the default privacy='local', and the dialectic reviewer, refuse an external endpoint;")
        print("  only consult(privacy='cloud_allowed') and call_model with privacy='auto' or 'cloud' will use it.")
        print(f"  Why: {reason}.")
    return is_local


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--model", help="model to use (must be one the server lists)")
    parser.add_argument("--yes", "-y", action="store_true", help="no prompts: take the default model and rebuild")
    parser.add_argument("--no-rebuild", action="store_true", help="write .env but do not rebuild the server")
    parser.add_argument("--no-docker", action="store_true", help="source install: print the settings instead of writing .env")
    parser.add_argument("--clear", action="store_true", help="remove the model settings from .env")
    parser.add_argument(
        "--base-url", "--ollama", dest="base_url",
        default=os.environ.get("OLLAMA_HOST_URL", HOST_OLLAMA + "/v1"),
        help=f"where this script finds the OpenAI-compatible model server, /v1 included (default {HOST_OLLAMA}/v1)",
    )
    parser.add_argument("--env-file", type=Path, default=REPO_ROOT / ".env")
    args = parser.parse_args(argv)
    env_file: Path = args.env_file

    owned_old = {key: None for key in OLD_KEYS}
    if args.clear:
        if env_file.exists():
            env_file.write_text(
                update_env_text(
                    env_file.read_text(),
                    # Every name the resolver reads, aliases included, or an
                    # old alias left behind becomes the endpoint again.
                    {BASE_KEY: None, MODEL_KEY: None, ALIAS_KEY: None, **owned_old},
                )
            )
        print(f"✓ Removed the model settings ({BASE_KEY}, {MODEL_KEY} and their older names) from {env_file}. Rebuild to apply: docker compose up -d --build governance-mcp")
        return 0

    base = openai_base(args.base_url)
    models = list_models(base)
    ollama = is_ollama(base) if models is not None else False
    if models is None:
        print(f"✗ No model server answered at {base}/models.")
        print("  Start an OpenAI-compatible server (Ollama from https://ollama.com is one: start it and pull a model,")
        print("  for example ollama pull gemma4:latest), or point --base-url at yours, then run this again.")
        print("  Without a model, consult is unavailable and reviews wait for a peer.")
        return 1
    if not models:
        if ollama:
            print(f"✗ Ollama at {base} has no models. Pull one (for example: ollama pull {PREFERRED_MODEL}) and run this again.")
        else:
            print(f"✗ The server at {base} lists no models. Load one and run this again.")
        return 1

    model = pick_model(models, args.model, args.yes, ollama=ollama)
    if model is None:
        return 1

    if args.no_docker:
        print("✓ Source install: set these in the server's environment (shell, or the launchd plist), then restart it:")
        print(f"  {BASE_KEY}={base}")
        print(f"  {MODEL_KEY}={model}")
        print_privacy_note(base, {key: os.environ.get(key) for key in _CLASSIFIER_KEYS})
        return 0

    server_base = container_base(base)
    before = env_file.read_text() if env_file.exists() else ""
    env_file.write_text(update_env_text(before, {BASE_KEY: server_base, MODEL_KEY: model, **owned_old}))
    print(f"✓ Wrote {BASE_KEY}={server_base} and {MODEL_KEY}={model} to {env_file}")
    replaced = [key for key in OLD_KEYS if read_env_value(before, key) is not None]
    if replaced:
        print(f"  Replaced the older {' and '.join(replaced)} line(s) with these.")
    alias = read_env_value(before, ALIAS_KEY)
    if alias:
        print(f"  Note: {env_file.name} also sets {ALIAS_KEY}={alias}. {BASE_KEY} takes precedence; remove the other line to avoid confusion.")
    endpoint_local = print_privacy_note(server_base, composed_classifier_values(before))

    if args.no_rebuild:
        print("  Rebuild to apply: docker compose up -d --build governance-mcp")
        return 0
    if not (args.yes or not sys.stdin.isatty()):
        if ask("Rebuild and restart the server now? [Y/n] ", "y", on_eof="n").lower() not in ("y", "yes"):
            print("  Rebuild to apply: docker compose up -d --build governance-mcp")
            return 0

    print("… Rebuilding the server (docker compose up -d --build --wait governance-mcp)")
    settings = {BASE_KEY: server_base, MODEL_KEY: model}
    result = compose(["up", "-d", "--build", "--wait", "governance-mcp"], env_file, settings)
    if result.returncode != 0:
        print("✗ The rebuild failed:")
        print((result.stderr or result.stdout).strip()[-2000:])
        return result.returncode or 1

    if server_reaches_model(env_file, settings):
        if endpoint_local:
            print(f"✓ The server reaches {model}. consult and dialectic reviews will use it.")
        else:
            print(f"✓ The server reaches {model}, for cloud-allowed consults only (see the note above).")
        return 0
    print(f"✗ The server cannot reach {model} at {server_base}.")
    if ollama and sys.platform.startswith("linux"):
        print("  On Linux, Ollama listens only on 127.0.0.1 by default. Set OLLAMA_HOST=0.0.0.0 for the Ollama service,")
        print("  allow port 11434 from the Docker bridge, and do not expose it beyond this machine (Ollama has no authentication).")
    elif ollama:
        print("  Check that Ollama is running on this machine, then run: unitares model")
    else:
        print("  Check that the model server is running and listens beyond 127.0.0.1 for the Docker bridge, then run: unitares model")
    return 1


if __name__ == "__main__":
    sys.exit(main())
