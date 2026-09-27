"""One Ollama host for the server and the agent processes.

The server used to read ``UNITARES_OLLAMA_BASE`` (root URL) while the dialectic
reviewer and the local residents read ``UNITARES_OLLAMA_BASE_URL`` (with
``/v1``), and the orchestrator forwarded only the second. Setting one name moved
half the local-inference plane. ``src/local_inference_env.py`` now resolves both,
and these tests pin the contract: the documented name wins, the alias still
works in either form, a trailing ``/v1`` is normalized so every caller gets the
form it needs, and an empty value counts as unset.
"""

from __future__ import annotations

import logging
import os
import subprocess
import sys
from pathlib import Path

import pytest

from src import local_inference_env as env

REPO = Path(__file__).resolve().parent.parent


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    for name in ("UNITARES_OLLAMA_BASE", "UNITARES_OLLAMA_BASE_URL", "UNITARES_LLM_MODEL"):
        monkeypatch.delenv(name, raising=False)
    env._warned_disagreements.clear()


def _both(expected_root: str) -> None:
    assert env.ollama_base_url() == expected_root
    assert env.ollama_openai_base_url() == expected_root + "/v1"


def test_neither_set_keeps_both_historical_defaults():
    # Server default was http://localhost:11434; agents' was .../v1.
    _both("http://localhost:11434")


@pytest.mark.parametrize(
    "value",
    ["http://gpu-box:11434", "http://gpu-box:11434/", "http://gpu-box:11434/v1", "http://gpu-box:11434/v1/"],
)
def test_documented_name_with_or_without_v1(monkeypatch, value):
    monkeypatch.setenv("UNITARES_OLLAMA_BASE", value)
    _both("http://gpu-box:11434")


@pytest.mark.parametrize(
    "value",
    ["http://gpu-box:11434/v1", "http://gpu-box:11434", " http://gpu-box:11434/v1/ "],
)
def test_alias_alone_with_or_without_v1(monkeypatch, value):
    # A deployment that set only the reviewer's old name keeps its host, and the
    # agents still get exactly one /v1 (the old value, unchanged, for /v1 input).
    monkeypatch.setenv("UNITARES_OLLAMA_BASE_URL", value)
    _both("http://gpu-box:11434")


def test_documented_name_wins_and_disagreement_is_logged_once(monkeypatch, caplog):
    monkeypatch.setenv("UNITARES_OLLAMA_BASE", "http://a:11434")
    monkeypatch.setenv("UNITARES_OLLAMA_BASE_URL", "http://b:11434/v1")
    with caplog.at_level(logging.WARNING, logger=env.__name__):
        _both("http://a:11434")
        env.ollama_base_url()
    warnings = [r for r in caplog.records if "disagree" in r.getMessage()]
    assert len(warnings) == 1


def test_agreeing_names_do_not_warn(monkeypatch, caplog):
    monkeypatch.setenv("UNITARES_OLLAMA_BASE", "http://a:11434")
    monkeypatch.setenv("UNITARES_OLLAMA_BASE_URL", "http://a:11434/v1")
    with caplog.at_level(logging.WARNING, logger=env.__name__):
        _both("http://a:11434")
    assert not [r for r in caplog.records if "disagree" in r.getMessage()]


def test_empty_values_count_as_unset(monkeypatch):
    # docker-compose passes ${VAR:-} through as "" — that must not become a base
    # URL of "/v1" or a model named "".
    monkeypatch.setenv("UNITARES_OLLAMA_BASE", "")
    monkeypatch.setenv("UNITARES_OLLAMA_BASE_URL", "")
    monkeypatch.setenv("UNITARES_LLM_MODEL", "")
    _both("http://localhost:11434")
    assert env.default_local_model() == "gemma4:latest"


def test_empty_documented_name_falls_through_to_alias(monkeypatch):
    monkeypatch.setenv("UNITARES_OLLAMA_BASE", "")
    monkeypatch.setenv("UNITARES_OLLAMA_BASE_URL", "http://b:11434/v1")
    _both("http://b:11434")


def test_model_override_and_default(monkeypatch):
    assert env.default_local_model() == "gemma4:latest"
    monkeypatch.setenv("UNITARES_LLM_MODEL", "qwen3:8b")
    assert env.default_local_model() == "qwen3:8b"


def test_server_registry_uses_the_shared_resolver(monkeypatch):
    from src.mcp_handlers.support import inference_registry

    assert inference_registry.ollama_base_url is env.ollama_base_url
    assert inference_registry.default_local_model is env.default_local_model
    monkeypatch.setenv("UNITARES_OLLAMA_BASE_URL", "http://b:11500/v1")
    assert inference_registry._ollama_host_port() == ("b", 11500)


@pytest.mark.parametrize(
    "module_name",
    ["agents.local_resident.runner", "agents.dialectic_reviewer.reviewer"],
)
@pytest.mark.parametrize(
    ("extra_env", "expected_url"),
    [
        # Only the documented name: the agents now follow it (they used to stay
        # on localhost), with exactly one /v1 for their OpenAI-compatible client.
        ({"UNITARES_OLLAMA_BASE": "http://gpu-box:11434"}, "http://gpu-box:11434/v1"),
        # Only the old name, as agent deployments set it: unchanged.
        ({"UNITARES_OLLAMA_BASE_URL": "http://gpu-box:11434/v1"}, "http://gpu-box:11434/v1"),
        # Neither: the agents' historical default.
        ({}, "http://localhost:11434/v1"),
    ],
)
def test_agent_processes_resolve_the_same_host(module_name, extra_env, expected_url):
    """Import each agent module in a fresh interpreter: its constants are read
    at import, and reloading it here would swap class objects under other tests."""
    child_env = {
        k: v
        for k, v in os.environ.items()
        if k not in ("UNITARES_OLLAMA_BASE", "UNITARES_OLLAMA_BASE_URL", "UNITARES_LLM_MODEL")
    }
    # Present-but-empty, not absent: the agent modules load the repo's .env on
    # import, and dotenv never overrides a variable that is already set, so an
    # operator's own .env cannot leak into the case. The resolver reads empty
    # as unset.
    child_env["UNITARES_OLLAMA_BASE"] = ""
    child_env["UNITARES_OLLAMA_BASE_URL"] = ""
    child_env.update(extra_env)
    child_env["UNITARES_LLM_MODEL"] = "qwen3:8b"
    out = subprocess.run(
        [
            sys.executable,
            "-c",
            # Marked lines: importing the reviewer loads the handler package,
            # which may print, so the whole stdout is not the answer.
            f"import {module_name} as m; print('RESOLVED', m.OLLAMA_BASE_URL); print('RESOLVED', m.DEFAULT_MODEL)",
        ],
        cwd=REPO,
        env=child_env,
        capture_output=True,
        text=True,
        timeout=120,
        check=True,
    )
    resolved = [line.split(" ", 1)[1] for line in out.stdout.splitlines() if line.startswith("RESOLVED ")]
    assert resolved == [expected_url, "qwen3:8b"]


def _reviewer_spawn_env() -> dict:
    from src.mcp_handlers.dialectic import orchestrator_dispatch

    spec = orchestrator_dispatch._build_spec("s1", {"root_cause": "r"}, None)
    return spec["env"]


def test_orchestrator_forwards_the_servers_resolved_host_for_an_alias_only_server(
    monkeypatch,
):
    """Alias-only server: the spawn env carries the server's resolved root under
    the canonical name, so a UNITARES_OLLAMA_BASE the child inherits from the
    orchestrator daemon cannot outrank it and split the reviewer from the server."""
    monkeypatch.setenv("UNITARES_OLLAMA_BASE_URL", "http://gpu:11434/v1")
    monkeypatch.setenv("UNITARES_LLM_MODEL", "qwen3:8b")
    spawn = _reviewer_spawn_env()
    assert spawn["UNITARES_OLLAMA_BASE"] == "http://gpu:11434"
    assert spawn["UNITARES_OLLAMA_BASE_URL"] == "http://gpu:11434"
    assert spawn["UNITARES_LLM_MODEL"] == "qwen3:8b"

    # The child's env is the orchestrator's env overlaid with the spec env.
    for name in ("UNITARES_OLLAMA_BASE", "UNITARES_OLLAMA_BASE_URL"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("UNITARES_OLLAMA_BASE", "http://orch-default:11434")
    for name in ("UNITARES_OLLAMA_BASE", "UNITARES_OLLAMA_BASE_URL"):
        monkeypatch.setenv(name, spawn[name])
    assert env.ollama_base_url() == "http://gpu:11434"
    assert env.ollama_openai_base_url() == "http://gpu:11434/v1"


def test_orchestrator_forwards_the_canonical_name_when_both_are_set(monkeypatch):
    monkeypatch.setenv("UNITARES_OLLAMA_BASE", "http://host.docker.internal:11434/")
    monkeypatch.setenv("UNITARES_OLLAMA_BASE_URL", "http://other:11434/v1")
    spawn = _reviewer_spawn_env()
    assert spawn["UNITARES_OLLAMA_BASE"] == "http://host.docker.internal:11434"
    assert spawn["UNITARES_OLLAMA_BASE_URL"] == "http://host.docker.internal:11434"


@pytest.mark.parametrize("unset_value", [None, "", "  "])
def test_orchestrator_leaves_the_host_to_the_orchestrator_when_the_server_sets_none(
    monkeypatch, unset_value
):
    if unset_value is not None:
        monkeypatch.setenv("UNITARES_OLLAMA_BASE", unset_value)
        monkeypatch.setenv("UNITARES_OLLAMA_BASE_URL", unset_value)
    spawn = _reviewer_spawn_env()
    assert "UNITARES_OLLAMA_BASE" not in spawn
    assert "UNITARES_OLLAMA_BASE_URL" not in spawn
