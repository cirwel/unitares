"""One model endpoint for the server and the agent processes.

The server used to read ``UNITARES_OLLAMA_BASE`` (root URL) while the dialectic
reviewer and the local residents read ``UNITARES_OLLAMA_BASE_URL`` (with
``/v1``), and the orchestrator forwarded only the second. Setting one name moved
half the local-inference plane. ``src/local_inference_env.py`` now resolves both,
and these tests pin the contract: the documented name wins, the alias still
works in either form, a trailing ``/v1`` is normalized so every caller gets the
form it needs, and an empty value counts as unset.

Since the endpoint became any OpenAI-compatible server, the documented names
are ``UNITARES_MODEL_BASE_URL`` and ``UNITARES_MODEL``; the Ollama names are
rows in ``SETTING_ALIASES`` with a removal release, and the tests below also pin
that table: precedence, quiet aliases, and the expiry the release cut enforces.
"""

from __future__ import annotations

import logging
import os
import subprocess
import sys
from pathlib import Path

import re

import pytest

from src import local_inference_env as env

REPO = Path(__file__).resolve().parent.parent


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    for name in (
        "UNITARES_OLLAMA_BASE",
        "UNITARES_OLLAMA_BASE_URL",
        "UNITARES_LLM_MODEL",
        *env.LOCAL_MODEL_SETTINGS,
        "UNITARES_TRUSTED_NETWORKS",
    ):
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
        # The new name, for any OpenAI-compatible server, taken as given.
        ({"UNITARES_MODEL_BASE_URL": "http://vllm.lan:8000/v1"}, "http://vllm.lan:8000/v1"),
    ],
)
def test_agent_processes_resolve_the_same_host(module_name, extra_env, expected_url):
    """Import each agent module in a fresh interpreter: its constants are read
    at import, and reloading it here would swap class objects under other tests."""
    child_env = {
        k: v
        for k, v in os.environ.items()
        if k not in ("UNITARES_OLLAMA_BASE", "UNITARES_OLLAMA_BASE_URL", "UNITARES_LLM_MODEL")
        and k not in env.LOCAL_MODEL_SETTINGS
    }
    # Present-but-empty, not absent: the agent modules load the repo's .env on
    # import, and dotenv never overrides a variable that is already set, so an
    # operator's own .env cannot leak into the case. The resolver reads empty
    # as unset.
    child_env["UNITARES_OLLAMA_BASE"] = ""
    child_env["UNITARES_OLLAMA_BASE_URL"] = ""
    for name in env.LOCAL_MODEL_SETTINGS:
        child_env[name] = ""
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
    """Alias-only server: the spawn env carries the server's resolved endpoint
    under the new name, so an older name the child inherits from the
    orchestrator daemon cannot outrank it and split the reviewer from the server."""
    monkeypatch.setenv("UNITARES_OLLAMA_BASE_URL", "http://gpu:11434/v1")
    monkeypatch.setenv("UNITARES_LLM_MODEL", "qwen3:8b")
    spawn = _reviewer_spawn_env()
    assert spawn["UNITARES_MODEL_BASE_URL"] == "http://gpu:11434/v1"
    assert spawn["UNITARES_MODEL"] == "qwen3:8b"
    # Only the names this release reads first; the old ones are not re-emitted.
    for old in ("UNITARES_OLLAMA_BASE", "UNITARES_OLLAMA_BASE_URL", "UNITARES_LLM_MODEL"):
        assert old not in spawn

    # The child's env is the orchestrator's env overlaid with the spec env.
    monkeypatch.delenv("UNITARES_OLLAMA_BASE_URL", raising=False)
    monkeypatch.setenv("UNITARES_OLLAMA_BASE", "http://orch-default:11434")
    monkeypatch.setenv("UNITARES_LLM_MODEL", "orch-default:1b")
    for name in ("UNITARES_MODEL_BASE_URL", "UNITARES_MODEL"):
        monkeypatch.setenv(name, spawn[name])
    assert env.model_base_url() == "http://gpu:11434/v1"
    assert env.ollama_base_url() == "http://gpu:11434"
    assert env.default_local_model() == "qwen3:8b"


def test_orchestrator_forwards_the_new_name_when_several_are_set(monkeypatch):
    monkeypatch.setenv("UNITARES_MODEL_BASE_URL", "http://vllm.lan:8000/v1/")
    monkeypatch.setenv("UNITARES_OLLAMA_BASE", "http://host.docker.internal:11434/")
    spawn = _reviewer_spawn_env()
    assert spawn["UNITARES_MODEL_BASE_URL"] == "http://vllm.lan:8000/v1"


@pytest.mark.parametrize("unset_value", [None, "", "  "])
def test_orchestrator_leaves_the_host_to_the_orchestrator_when_the_server_sets_none(
    monkeypatch, unset_value
):
    names = ("UNITARES_MODEL_BASE_URL", "UNITARES_OLLAMA_BASE", "UNITARES_OLLAMA_BASE_URL",
             "UNITARES_MODEL", "UNITARES_LLM_MODEL")
    if unset_value is not None:
        for name in names:
            monkeypatch.setenv(name, unset_value)
    spawn = _reviewer_spawn_env()
    for name in names:
        assert name not in spawn


# --- the alias table (docs/proposals/active/local-inference-one-endpoint-v0.md 2.1.1)


def test_new_names_win_over_every_alias(monkeypatch):
    monkeypatch.setenv("UNITARES_MODEL_BASE_URL", "http://new:8000/v1")
    monkeypatch.setenv("UNITARES_OLLAMA_BASE", "http://old:11434")
    monkeypatch.setenv("UNITARES_OLLAMA_BASE_URL", "http://older:11434/v1")
    monkeypatch.setenv("UNITARES_MODEL", "new-model")
    monkeypatch.setenv("UNITARES_LLM_MODEL", "old-model")
    assert env.model_base_url() == "http://new:8000/v1"
    assert env.ollama_base_url() == "http://new:8000"
    assert env.default_local_model() == "new-model"


@pytest.mark.parametrize(
    ("value", "base"),
    [
        # Documented form: the base including /v1, kept as given.
        ("http://vllm.lan:8000/v1", "http://vllm.lan:8000/v1"),
        # A base with its own path (a hosted router) is not rewritten.
        ("https://router.example.org/api/v1/", "https://router.example.org/api/v1"),
        # A bare root gets /v1, so an old Ollama value renamed as-is still works.
        ("http://gpu-box:11434", "http://gpu-box:11434/v1"),
    ],
)
def test_new_base_name_forms(monkeypatch, value, base):
    monkeypatch.setenv("UNITARES_MODEL_BASE_URL", value)
    assert env.model_base_url() == base
    assert env.ollama_base_url() == re.sub(r"/v1$", "", base)


def test_an_old_name_alone_is_quiet(monkeypatch, caplog):
    monkeypatch.setenv("UNITARES_LLM_MODEL", "qwen3:8b")
    monkeypatch.setenv("UNITARES_OLLAMA_BASE", "http://gpu:11434")
    with caplog.at_level(logging.DEBUG, logger=env.__name__):
        for _ in range(3):
            assert env.default_local_model() == "qwen3:8b"
            assert env.model_base_url() == "http://gpu:11434/v1"
    assert caplog.records == []


def test_new_and_old_disagreeing_warn_once(monkeypatch, caplog):
    monkeypatch.setenv("UNITARES_MODEL", "a")
    monkeypatch.setenv("UNITARES_LLM_MODEL", "b")
    with caplog.at_level(logging.WARNING, logger=env.__name__):
        for _ in range(3):
            assert env.default_local_model() == "a"
    warnings = [r.getMessage() for r in caplog.records if "disagree" in r.getMessage()]
    assert len(warnings) == 1
    assert "UNITARES_MODEL" in warnings[0] and "UNITARES_LLM_MODEL" in warnings[0]


def test_every_old_name_has_one_row_and_a_new_name_that_is_a_setting():
    olds = [a.old for a in env.SETTING_ALIASES]
    assert len(olds) == len(set(olds))
    assert set(olds) == {"UNITARES_OLLAMA_BASE", "UNITARES_OLLAMA_BASE_URL", "UNITARES_LLM_MODEL"}
    for alias in env.SETTING_ALIASES:
        assert alias.new in env.LOCAL_MODEL_SETTINGS
        assert re.fullmatch(r"\d+\.\d+\.\d+", alias.removed_in)


def _version(text: str) -> tuple[int, ...]:
    return tuple(int(part) for part in text.strip().split(".")[:3])


def test_no_alias_outlives_its_removal_release():
    """Fails once VERSION reaches an alias's removal release.

    The release cut then deletes the row (and the old name from compose, the
    plist template and the docs), or a reviewed diff moves its date. An alias
    never outlives its date silently.
    """
    current = _version((REPO / "VERSION").read_text())
    expired = [
        f"{a.old} (alias of {a.new}) was due for removal in {a.removed_in}"
        for a in env.SETTING_ALIASES
        if current >= _version(a.removed_in)
    ]
    assert not expired, "; ".join(expired)


def test_old_names_in_use_reports_only_set_names(monkeypatch):
    assert env.old_names_in_use() == []
    monkeypatch.setenv("UNITARES_LLM_MODEL", "x")
    monkeypatch.setenv("UNITARES_OLLAMA_BASE", "  ")
    assert [a.old for a in env.old_names_in_use()] == ["UNITARES_LLM_MODEL"]


# --- every setting reaches every process that reads it (2.1.2)


def test_every_local_model_setting_reaches_every_reader(monkeypatch):
    """One list, three destinations: Compose must map it for governance-mcp, the
    LaunchAgent template must carry it (launchd inherits no shell), and the
    orchestrated reviewer must receive it. A setting missing from any of them
    makes that install look configured while the reader never sees it."""
    import yaml

    compose = yaml.safe_load((REPO / "docker-compose.yml").read_text())
    compose_env = compose["services"]["governance-mcp"]["environment"]
    plist = (REPO / "scripts/ops/com.unitares.governance-mcp.plist").read_text()

    values = {
        "UNITARES_MODEL_BASE_URL": "http://gpu:11434/v1",
        "UNITARES_MODEL": "qwen3:8b",
        "UNITARES_MODEL_LOCAL_HOSTS": "gpu",
        "UNITARES_MODEL_PRIVACY": "local",
        "UNITARES_TRUSTED_NETWORKS": "100.64.0.0/10",
    }
    assert set(values) == set(env.LOCAL_MODEL_SETTINGS)
    for name, value in values.items():
        monkeypatch.setenv(name, value)
    spawn = _reviewer_spawn_env()

    for name in env.LOCAL_MODEL_SETTINGS:
        assert compose_env.get(name) == "${%s:-}" % name, f"{name} not mapped in docker-compose.yml"
        assert f"<key>{name}</key>" in plist, f"{name} missing from the LaunchAgent template"
        assert spawn.get(name) == values[name], f"{name} not forwarded to the orchestrated reviewer"
