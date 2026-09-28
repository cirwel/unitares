"""scripts/install/choose_model.py — the setup step that names a model.

The model server and Docker are stubbed: these tests pin what the script writes
to .env, which model it picks, and what it tells the operator on each path. The
server is any OpenAI-compatible one; Ollama is detected only for its hints.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from scripts.install import choose_model as cm


# --- .env editing -------------------------------------------------------------

def test_appends_both_settings_to_an_empty_env():
    out = cm.update_env_text("", {cm.BASE_KEY: "http://host.docker.internal:11434/v1", cm.MODEL_KEY: "gemma4:latest"})
    assert "UNITARES_MODEL_BASE_URL=http://host.docker.internal:11434/v1" in out.splitlines()
    assert "UNITARES_MODEL=gemma4:latest" in out.splitlines()


def test_replaces_existing_values_and_keeps_every_other_line():
    before = "POSTGRES_PASSWORD=secret\nUNITARES_MODEL=old:1b\n# UNITARES_MODEL_BASE_URL=http://example\nGOVERNANCE_HOST_PORT=18767\n"
    out = cm.update_env_text(before, {cm.BASE_KEY: "http://host.docker.internal:11434/v1", cm.MODEL_KEY: "qwen3:8b"})
    lines = out.splitlines()
    assert "POSTGRES_PASSWORD=secret" in lines
    assert "GOVERNANCE_HOST_PORT=18767" in lines
    assert "# UNITARES_MODEL_BASE_URL=http://example" in lines  # a commented example is left alone
    assert lines.count("UNITARES_MODEL=qwen3:8b") == 1
    assert "UNITARES_MODEL=old:1b" not in lines
    assert lines.count("UNITARES_MODEL_BASE_URL=http://host.docker.internal:11434/v1") == 1


def test_is_idempotent():
    settings = {cm.BASE_KEY: "http://host.docker.internal:11434/v1", cm.MODEL_KEY: "gemma4:latest"}
    once = cm.update_env_text("A=1\n", settings)
    assert cm.update_env_text(once, settings) == once


def test_clear_removes_only_the_owned_keys():
    before = "A=1\nUNITARES_MODEL_BASE_URL=x\nUNITARES_MODEL=y\nUNITARES_OLLAMA_BASE_URL=z\n"
    out = cm.update_env_text(before, {cm.BASE_KEY: None, cm.MODEL_KEY: None})
    assert out.splitlines() == ["A=1", "UNITARES_OLLAMA_BASE_URL=z"]


# --- model choice -------------------------------------------------------------

def test_prefers_the_documented_default_when_pulled():
    assert cm.default_choice(["llama3:8b", "gemma4:latest"]) == "gemma4:latest"
    assert cm.default_choice(["llama3:8b", "qwen3:8b"]) == "llama3:8b"


def test_rejects_a_model_that_is_not_pulled(capsys):
    assert cm.pick_model(["gemma4:latest"], "qwen3:8b", assume_yes=True) is None
    assert "ollama pull qwen3:8b" in capsys.readouterr().out


def test_a_non_ollama_server_gets_no_pull_hint(capsys):
    assert cm.pick_model(["Qwen/Qwen3-8B"], "qwen3:8b", assume_yes=True, ollama=False) is None
    out = capsys.readouterr().out
    assert "ollama" not in out.lower()
    assert "not one of the models the server lists" in out


# --- the whole flow -----------------------------------------------------------

@pytest.fixture
def stubs(monkeypatch):
    calls = []
    state = {"models": ["gemma4:latest", "qwen3:8b"], "compose_rc": 0, "reaches": True}
    state["ollama"] = True
    monkeypatch.setattr(cm, "list_models", lambda base, timeout=3.0: state["models"])
    monkeypatch.setattr(cm, "is_ollama", lambda base, timeout=1.0: state["ollama"])

    def fake_compose(args, env_file, settings):
        calls.append(args)
        state.setdefault("seen", []).append((env_file, dict(settings)))
        rc = state["compose_rc"] if args[0] == "up" else (0 if state["reaches"] else 1)
        return subprocess.CompletedProcess(args, rc, stdout="", stderr="boom" if rc else "")

    monkeypatch.setattr(cm, "compose", fake_compose)
    return state, calls


def test_writes_env_rebuilds_and_confirms_the_server_reaches_the_model(tmp_path: Path, stubs, capsys):
    state, calls = stubs
    env = tmp_path / ".env"
    env.write_text("POSTGRES_PASSWORD=secret\n")
    assert cm.main(["--yes", "--env-file", str(env)]) == 0
    text = env.read_text()
    assert "UNITARES_MODEL_BASE_URL=http://host.docker.internal:11434/v1" in text
    assert "UNITARES_MODEL=gemma4:latest" in text
    assert "POSTGRES_PASSWORD=secret" in text
    assert calls[0] == ["up", "-d", "--build", "--wait", "governance-mcp"]
    assert calls[1][:3] == ["exec", "-T", "governance-mcp"]
    assert "reaches gemma4:latest" in capsys.readouterr().out


def test_says_so_when_the_server_cannot_reach_the_model(tmp_path: Path, stubs, capsys):
    state, _ = stubs
    state["reaches"] = False
    assert cm.main(["--yes", "--env-file", str(tmp_path / ".env")]) == 1
    assert "cannot reach gemma4:latest" in capsys.readouterr().out


def test_no_server_writes_nothing(tmp_path: Path, monkeypatch, capsys):
    monkeypatch.setattr(cm, "list_models", lambda base, timeout=3.0: None)
    env = tmp_path / ".env"
    assert cm.main(["--yes", "--env-file", str(env)]) == 1
    assert not env.exists()
    assert "No model server answered at http://localhost:11434/v1/models" in capsys.readouterr().out


def test_no_models_pulled_writes_nothing(tmp_path: Path, stubs, capsys):
    state, calls = stubs
    state["models"] = []
    env = tmp_path / ".env"
    assert cm.main(["--yes", "--env-file", str(env)]) == 1
    assert not env.exists() and not calls
    assert "ollama pull" in capsys.readouterr().out


def test_an_empty_non_ollama_server_gets_no_pull_hint(tmp_path: Path, stubs, capsys):
    state, _ = stubs
    state["models"], state["ollama"] = [], False
    assert cm.main(["--yes", "--env-file", str(tmp_path / ".env")]) == 1
    out = capsys.readouterr().out
    assert "lists no models" in out and "ollama" not in out.lower()


def test_no_rebuild_only_writes_env(tmp_path: Path, stubs):
    _, calls = stubs
    env = tmp_path / ".env"
    assert cm.main(["--model", "qwen3:8b", "--yes", "--no-rebuild", "--env-file", str(env)]) == 0
    assert "UNITARES_MODEL=qwen3:8b" in env.read_text()
    assert calls == []


def test_source_install_prints_host_settings_and_writes_nothing(tmp_path: Path, stubs, capsys):
    _, calls = stubs
    env = tmp_path / ".env"
    assert cm.main(["--yes", "--no-docker", "--env-file", str(env)]) == 0
    out = capsys.readouterr().out
    assert "UNITARES_MODEL_BASE_URL=http://localhost:11434/v1" in out
    assert not env.exists() and calls == []


def test_warns_about_a_conflicting_alias_line(tmp_path: Path, stubs, capsys):
    env = tmp_path / ".env"
    env.write_text("UNITARES_OLLAMA_BASE_URL=http://elsewhere:11434/v1\n")
    assert cm.main(["--yes", "--no-rebuild", "--env-file", str(env)]) == 0
    assert "UNITARES_MODEL_BASE_URL takes precedence" in capsys.readouterr().out


def test_writing_replaces_the_older_names_this_script_wrote(tmp_path: Path, stubs, capsys):
    env = tmp_path / ".env"
    env.write_text("A=1\nUNITARES_OLLAMA_BASE=http://host.docker.internal:11434\nUNITARES_LLM_MODEL=old:1b\n")
    assert cm.main(["--model", "qwen3:8b", "--yes", "--no-rebuild", "--env-file", str(env)]) == 0
    lines = env.read_text().splitlines()
    assert "UNITARES_OLLAMA_BASE=http://host.docker.internal:11434" not in lines
    assert "UNITARES_LLM_MODEL=old:1b" not in lines
    assert "UNITARES_MODEL=qwen3:8b" in lines and "A=1" in lines
    assert "Replaced the older" in capsys.readouterr().out


def test_clear(tmp_path: Path, capsys):
    env = tmp_path / ".env"
    env.write_text(
        "A=1\nUNITARES_MODEL_BASE_URL=x\nUNITARES_MODEL=y\nUNITARES_OLLAMA_BASE=x\n"
        "UNITARES_LLM_MODEL=y\nUNITARES_OLLAMA_BASE_URL=http://stale:11434\n"
    )
    assert cm.main(["--clear", "--env-file", str(env)]) == 0
    assert env.read_text() == "A=1\n"


# --- interactive prompts ------------------------------------------------------

def _tty(monkeypatch, answers):
    replies = iter(answers)

    def fake_input(prompt=""):
        try:
            return next(replies)
        except StopIteration:
            raise EOFError

    monkeypatch.setattr("sys.stdin.isatty", lambda: True)
    monkeypatch.setattr("builtins.input", fake_input)


def test_interactive_pick_by_number(monkeypatch):
    _tty(monkeypatch, ["2"])
    assert cm.pick_model(["gemma4:latest", "qwen3:8b"], None, assume_yes=False) == "qwen3:8b"


def test_interactive_enter_takes_the_default(monkeypatch):
    _tty(monkeypatch, [""])
    assert cm.pick_model(["llama3:8b", "gemma4:latest"], None, assume_yes=False) == "gemma4:latest"


def test_interactive_nonsense_is_refused(monkeypatch, capsys):
    _tty(monkeypatch, ["9"])
    assert cm.pick_model(["gemma4:latest"], None, assume_yes=False) is None
    assert "not one of the listed models" in capsys.readouterr().out


def test_declining_the_rebuild_writes_env_only(tmp_path: Path, stubs, monkeypatch):
    _, calls = stubs
    _tty(monkeypatch, ["", "n"])
    env = tmp_path / ".env"
    assert cm.main(["--env-file", str(env)]) == 0
    assert "UNITARES_MODEL=gemma4:latest" in env.read_text()
    assert calls == []


def test_ctrl_d_at_the_rebuild_prompt_does_not_rebuild(tmp_path: Path, stubs, monkeypatch):
    _, calls = stubs
    _tty(monkeypatch, [""])  # model prompt answered; the rebuild prompt gets end of input
    assert cm.main(["--env-file", str(tmp_path / ".env")]) == 0
    assert calls == []


# --- the endpoint the server is given -----------------------------------------

@pytest.mark.parametrize(
    ("host_url", "expected"),
    [
        ("http://localhost:11434", "http://host.docker.internal:11434/v1"),
        ("http://127.0.0.1:11500/", "http://host.docker.internal:11500/v1"),
        ("http://localhost:11434/v1", "http://host.docker.internal:11434/v1"),
        # Any server on the host, not only Ollama: loopback means the container.
        ("http://localhost:8000/v1", "http://host.docker.internal:8000/v1"),
        ("http://gpu-box.lan:11434", "http://gpu-box.lan:11434/v1"),
        ("https://ollama.example.org", "https://ollama.example.org/v1"),
        ("https://router.example.org/api/v1", "https://router.example.org/api/v1"),
    ],
)
def test_container_base_keeps_the_endpoint_and_translates_only_localhost(host_url, expected):
    assert cm.container_base(host_url) == expected


def test_a_custom_endpoint_is_what_the_server_is_given(tmp_path: Path, stubs):
    env = tmp_path / ".env"
    assert cm.main(["--base-url", "http://gpu-box.lan:8000/v1", "--yes", "--no-rebuild", "--env-file", str(env)]) == 0
    assert "UNITARES_MODEL_BASE_URL=http://gpu-box.lan:8000/v1" in env.read_text()


def test_the_older_ollama_flag_still_names_the_server(tmp_path: Path, stubs):
    env = tmp_path / ".env"
    assert cm.main(["--ollama", "http://gpu-box.lan:11500", "--yes", "--no-rebuild", "--env-file", str(env)]) == 0
    assert "UNITARES_MODEL_BASE_URL=http://gpu-box.lan:11500/v1" in env.read_text()


class _Resp:
    def __init__(self, body: bytes):
        self._body = body

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def read(self):
        return self._body


def test_discovery_lists_models_from_the_openai_compatible_route(monkeypatch):
    seen = []

    def fake_urlopen(url, timeout=0):
        seen.append(url)
        return _Resp(b'{"object": "list", "data": [{"id": "qwen3:8b"}, {"id": "gemma4:latest"}]}')

    monkeypatch.setattr(cm.urllib.request, "urlopen", fake_urlopen)
    assert cm.list_models("http://localhost:11434/v1/") == ["gemma4:latest", "qwen3:8b"]
    assert cm.list_models("http://localhost:11434") == ["gemma4:latest", "qwen3:8b"]
    assert cm.list_models("http://vllm.lan:8000/v1") == ["gemma4:latest", "qwen3:8b"]
    assert seen == [
        "http://localhost:11434/v1/models",
        "http://localhost:11434/v1/models",
        "http://vllm.lan:8000/v1/models",
    ]


def test_an_ollama_only_answer_is_not_a_model_list(monkeypatch):
    monkeypatch.setattr(cm.urllib.request, "urlopen", lambda url, timeout=0: _Resp(b'{"models": [{"name": "x"}]}'))
    assert cm.list_models("http://localhost:11434/v1") is None


def test_ollama_detection_reads_api_version_at_the_root(monkeypatch):
    seen = []

    def fake_urlopen(url, timeout=0):
        seen.append(url)
        return _Resp(b'{"version": "0.12.0"}')

    monkeypatch.setattr(cm.urllib.request, "urlopen", fake_urlopen)
    assert cm.is_ollama("http://localhost:11434/v1") is True
    assert seen == ["http://localhost:11434/api/version"]

    def not_found(url, timeout=0):
        raise cm.urllib.error.HTTPError(url, 404, "nf", {}, None)

    monkeypatch.setattr(cm.urllib.request, "urlopen", not_found)
    assert cm.is_ollama("http://vllm.lan:8000/v1") is False


def test_source_install_prints_the_v1_base(tmp_path: Path, stubs, capsys):
    assert cm.main(["--base-url", "http://localhost:11434", "--yes", "--no-docker", "--env-file", str(tmp_path / ".env")]) == 0
    assert "UNITARES_MODEL_BASE_URL=http://localhost:11434/v1\n" in capsys.readouterr().out


def test_compose_gets_the_chosen_env_file_and_values_not_inherited_ones(tmp_path: Path, monkeypatch):
    runs = []

    def fake_run(cmd, cwd=None, env=None, text=None, capture_output=None):
        runs.append((cmd, cwd, env))
        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

    monkeypatch.setattr(cm.subprocess, "run", fake_run)
    monkeypatch.setenv("UNITARES_MODEL_BASE_URL", "http://stale:11434/v1")
    monkeypatch.setenv("UNITARES_OLLAMA_BASE", "http://stale:11434")
    monkeypatch.setenv("UNITARES_LLM_MODEL", "stale:1b")
    monkeypatch.setenv("UNITARES_OLLAMA_BASE_URL", "http://stale:11434/v1")
    env_file = tmp_path / "staging.env"
    cm.compose(["up"], env_file, {cm.BASE_KEY: "http://host.docker.internal:11434/v1", cm.MODEL_KEY: "qwen3:8b"})
    cmd, cwd, env = runs[0]
    assert cmd[:6] == ["docker", "compose", "--project-directory", str(cm.REPO_ROOT), "--env-file", str(env_file)]
    assert cwd == cm.REPO_ROOT
    assert env["UNITARES_MODEL_BASE_URL"] == "http://host.docker.internal:11434/v1"
    assert env["UNITARES_MODEL"] == "qwen3:8b"
    # An older name exported in the shell would outrank nothing, but it would
    # still reach the container and read as a second answer; it is dropped.
    for old in ("UNITARES_OLLAMA_BASE", "UNITARES_LLM_MODEL", "UNITARES_OLLAMA_BASE_URL"):
        assert old not in env


def test_rebuild_and_probe_use_the_file_and_model_just_written(tmp_path: Path, stubs):
    state, _ = stubs
    env = tmp_path / "staging.env"
    assert cm.main(["--model", "qwen3:8b", "--yes", "--env-file", str(env)]) == 0
    assert all(f == env and s == {cm.BASE_KEY: "http://host.docker.internal:11434/v1", cm.MODEL_KEY: "qwen3:8b"} for f, s in state["seen"])
