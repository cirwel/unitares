"""scripts/install/choose_model.py — the setup step that names a model.

Ollama and Docker are stubbed: these tests pin what the script writes to .env,
which model it picks, and what it tells the operator on each path.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from scripts.install import choose_model as cm


# --- .env editing -------------------------------------------------------------

def test_appends_both_settings_to_an_empty_env():
    out = cm.update_env_text("", {cm.BASE_KEY: "http://host.docker.internal:11434", cm.MODEL_KEY: "gemma4:latest"})
    assert "UNITARES_OLLAMA_BASE=http://host.docker.internal:11434" in out.splitlines()
    assert "UNITARES_LLM_MODEL=gemma4:latest" in out.splitlines()


def test_replaces_existing_values_and_keeps_every_other_line():
    before = "POSTGRES_PASSWORD=secret\nUNITARES_LLM_MODEL=old:1b\n# UNITARES_OLLAMA_BASE=http://example\nGOVERNANCE_HOST_PORT=18767\n"
    out = cm.update_env_text(before, {cm.BASE_KEY: "http://host.docker.internal:11434", cm.MODEL_KEY: "qwen3:8b"})
    lines = out.splitlines()
    assert "POSTGRES_PASSWORD=secret" in lines
    assert "GOVERNANCE_HOST_PORT=18767" in lines
    assert "# UNITARES_OLLAMA_BASE=http://example" in lines  # a commented example is left alone
    assert lines.count("UNITARES_LLM_MODEL=qwen3:8b") == 1
    assert "UNITARES_LLM_MODEL=old:1b" not in lines
    assert lines.count("UNITARES_OLLAMA_BASE=http://host.docker.internal:11434") == 1


def test_is_idempotent():
    settings = {cm.BASE_KEY: "http://host.docker.internal:11434", cm.MODEL_KEY: "gemma4:latest"}
    once = cm.update_env_text("A=1\n", settings)
    assert cm.update_env_text(once, settings) == once


def test_clear_removes_only_the_owned_keys():
    before = "A=1\nUNITARES_OLLAMA_BASE=x\nUNITARES_LLM_MODEL=y\nUNITARES_OLLAMA_BASE_URL=z\n"
    out = cm.update_env_text(before, {cm.BASE_KEY: None, cm.MODEL_KEY: None})
    assert out.splitlines() == ["A=1", "UNITARES_OLLAMA_BASE_URL=z"]


# --- model choice -------------------------------------------------------------

def test_prefers_the_documented_default_when_pulled():
    assert cm.default_choice(["llama3:8b", "gemma4:latest"]) == "gemma4:latest"
    assert cm.default_choice(["llama3:8b", "qwen3:8b"]) == "llama3:8b"


def test_rejects_a_model_that_is_not_pulled(capsys):
    assert cm.pick_model(["gemma4:latest"], "qwen3:8b", assume_yes=True) is None
    assert "ollama pull qwen3:8b" in capsys.readouterr().out


# --- the whole flow -----------------------------------------------------------

@pytest.fixture
def stubs(monkeypatch):
    calls = []
    state = {"models": ["gemma4:latest", "qwen3:8b"], "compose_rc": 0, "reaches": True}
    monkeypatch.setattr(cm, "list_ollama_models", lambda base, timeout=3.0: state["models"])

    def fake_compose(args, cwd):
        calls.append(args)
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
    assert "UNITARES_OLLAMA_BASE=http://host.docker.internal:11434" in text
    assert "UNITARES_LLM_MODEL=gemma4:latest" in text
    assert "POSTGRES_PASSWORD=secret" in text
    assert calls[0] == ["up", "-d", "--build", "--wait", "governance-mcp"]
    assert calls[1][:3] == ["exec", "-T", "governance-mcp"]
    assert "reaches gemma4:latest" in capsys.readouterr().out


def test_says_so_when_the_server_cannot_reach_the_model(tmp_path: Path, stubs, capsys):
    state, _ = stubs
    state["reaches"] = False
    assert cm.main(["--yes", "--env-file", str(tmp_path / ".env")]) == 1
    assert "cannot reach Ollama" in capsys.readouterr().out


def test_no_ollama_writes_nothing(tmp_path: Path, monkeypatch, capsys):
    monkeypatch.setattr(cm, "list_ollama_models", lambda base, timeout=3.0: None)
    env = tmp_path / ".env"
    assert cm.main(["--yes", "--env-file", str(env)]) == 1
    assert not env.exists()
    assert "No Ollama answered" in capsys.readouterr().out


def test_no_models_pulled_writes_nothing(tmp_path: Path, stubs, capsys):
    state, calls = stubs
    state["models"] = []
    env = tmp_path / ".env"
    assert cm.main(["--yes", "--env-file", str(env)]) == 1
    assert not env.exists() and not calls
    assert "ollama pull" in capsys.readouterr().out


def test_no_rebuild_only_writes_env(tmp_path: Path, stubs):
    _, calls = stubs
    env = tmp_path / ".env"
    assert cm.main(["--model", "qwen3:8b", "--yes", "--no-rebuild", "--env-file", str(env)]) == 0
    assert "UNITARES_LLM_MODEL=qwen3:8b" in env.read_text()
    assert calls == []


def test_source_install_prints_host_settings_and_writes_nothing(tmp_path: Path, stubs, capsys):
    _, calls = stubs
    env = tmp_path / ".env"
    assert cm.main(["--yes", "--no-docker", "--env-file", str(env)]) == 0
    out = capsys.readouterr().out
    assert "UNITARES_OLLAMA_BASE=http://localhost:11434" in out
    assert not env.exists() and calls == []


def test_warns_about_a_conflicting_alias_line(tmp_path: Path, stubs, capsys):
    env = tmp_path / ".env"
    env.write_text("UNITARES_OLLAMA_BASE_URL=http://elsewhere:11434/v1\n")
    assert cm.main(["--yes", "--no-rebuild", "--env-file", str(env)]) == 0
    assert "UNITARES_OLLAMA_BASE takes precedence" in capsys.readouterr().out


def test_clear(tmp_path: Path, capsys):
    env = tmp_path / ".env"
    env.write_text("A=1\nUNITARES_OLLAMA_BASE=x\nUNITARES_LLM_MODEL=y\n")
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
    assert "UNITARES_LLM_MODEL=gemma4:latest" in env.read_text()
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
        ("http://localhost:11434", "http://host.docker.internal:11434"),
        ("http://127.0.0.1:11500/", "http://host.docker.internal:11500"),
        ("http://localhost:11434/v1", "http://host.docker.internal:11434"),
        ("http://gpu-box.lan:11434", "http://gpu-box.lan:11434"),
        ("https://ollama.example.org", "https://ollama.example.org"),
    ],
)
def test_container_base_keeps_the_endpoint_and_translates_only_localhost(host_url, expected):
    assert cm.container_base(host_url) == expected


def test_a_custom_endpoint_is_what_the_server_is_given(tmp_path: Path, stubs):
    env = tmp_path / ".env"
    assert cm.main(["--ollama", "http://gpu-box.lan:11500", "--yes", "--no-rebuild", "--env-file", str(env)]) == 0
    assert "UNITARES_OLLAMA_BASE=http://gpu-box.lan:11500" in env.read_text()
