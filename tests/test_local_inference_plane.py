"""One local-inference plane, shared by every route.

Before this consolidation, ``llm_delegation.py`` (dialectic synthetic
reviewer, knowledge synthesis, check-in coaching) carried its own Ollama
client, its own default-model resolver, its own availability ping, and the
base URL was resolved differently per path: the structured dialectic route
honored the base setting (then ``UNITARES_OLLAMA_BASE``, now
``UNITARES_MODEL_BASE_URL``) while ``call_model`` and the OpenAI-compat
internal route hardcoded localhost — so setting the env var silently split
inference between two hosts. These tests pin the shared primitives in
``inference_registry`` as the single source: one base URL, one default model,
one cached availability probe, one provenance hash.

They also pin the boundary that stays deliberately UNshared: internal
delegation must not attribute Energy/EISV to the agent it is reasoning about
(the #1424 ``audit_only`` principle — instrumenting a surface must not enrol
it in a behavioral feed nobody measured).
"""

import inspect
from unittest.mock import patch

import pytest

from src.mcp_handlers.support import (
    delegated_inference,
    inference_registry,
    llm_delegation,
    model_inference,
)


BASE_OVERRIDE = "http://inference-box.lan:11500"


def test_one_base_url_for_every_local_route(monkeypatch):
    """UNITARES_MODEL_BASE_URL must move the OpenAI-compat client, the native
    structured endpoint, AND the availability probe together — a base override
    that only some routes honor splits the plane between two hosts."""
    monkeypatch.setenv("UNITARES_MODEL_BASE_URL", BASE_OVERRIDE + "/v1")

    assert inference_registry.ollama_base_url() == BASE_OVERRIDE
    assert llm_delegation._ollama_native_url() == BASE_OVERRIDE + "/api/chat"
    assert inference_registry._ollama_host_port() == ("inference-box.lan", 11500)

    client = llm_delegation._get_ollama_client()
    if client is not None:  # OpenAI SDK present
        assert str(client.base_url).rstrip("/") == BASE_OVERRIDE + "/v1"


def test_trailing_slash_and_default_port_are_normalized(monkeypatch):
    # A bare root (no /v1) with a trailing slash is the same endpoint.
    monkeypatch.setenv("UNITARES_MODEL_BASE_URL", "http://10.0.0.5:11434/")
    assert inference_registry.ollama_base_url() == "http://10.0.0.5:11434"
    assert inference_registry._ollama_host_port() == ("10.0.0.5", 11434)

    monkeypatch.delenv("UNITARES_MODEL_BASE_URL", raising=False)
    assert inference_registry.ollama_base_url() == "http://localhost:11434"
    assert inference_registry._ollama_host_port() == ("localhost", 11434)


def test_one_default_model_resolver(monkeypatch):
    """UNITARES_MODEL_ID resolves through exactly one function; the internal
    lane must not keep a private copy that can drift from the registry's."""
    monkeypatch.setenv("UNITARES_MODEL_ID", "qwen3:8b")
    assert inference_registry.default_local_model() == "qwen3:8b"
    # The internal lane's resolver IS the registry's, not a lookalike.
    assert llm_delegation._get_default_model is inference_registry.default_local_model
    # The host catalog advertises the same resolution.
    ollama_host = inference_registry.get_inference_host("ollama:local")
    assert ollama_host["models"] == ["qwen3:8b"]


@pytest.mark.asyncio
async def test_is_llm_available_uses_the_registry_probe(monkeypatch):
    """Availability comes from the registry's TTL-cached probe — the same
    source call_model's auto-routing consults — not a private models.list ping."""
    monkeypatch.setenv("UNITARES_MODEL_ID", "qwen3:8b")
    with patch.object(llm_delegation, "OPENAI_AVAILABLE", True), \
         patch.object(llm_delegation, "_ollama_available", return_value=True) as probe:
        assert await llm_delegation.is_llm_available() is True
    probe.assert_called_once()

    with patch.object(llm_delegation, "OPENAI_AVAILABLE", True), \
         patch.object(llm_delegation, "_ollama_available", return_value=False):
        assert await llm_delegation.is_llm_available() is False


@pytest.mark.asyncio
async def test_internal_lane_sends_nothing_without_a_named_model(monkeypatch):
    """No model is built in: with UNITARES_MODEL_ID unset the internal lane is
    unavailable and its calls return None without creating a client."""
    monkeypatch.delenv("UNITARES_MODEL_ID", raising=False)
    with patch.object(llm_delegation, "OPENAI_AVAILABLE", True), \
         patch.object(llm_delegation, "_ollama_available", return_value=True) as probe, \
         patch.object(llm_delegation, "_get_ollama_client") as client:
        assert await llm_delegation.is_llm_available() is False
        assert await llm_delegation.call_local_llm("hello") is None
    probe.assert_not_called()
    client.assert_not_called()


def test_one_provenance_hash_helper():
    """call_model and delegate_inference evidence hashing is the registry's
    single helper, not per-module copies that could format-drift."""
    assert model_inference._sha256_text is inference_registry.sha256_text
    assert delegated_inference._sha256_text is inference_registry.sha256_text
    assert inference_registry.sha256_text("x").startswith("sha256:")


def test_internal_delegation_never_attributes_energy():
    """The internal lane shares the transport plane but NOT the accounting: a
    call made ABOUT an agent (coaching, synthetic review) is not work done BY
    that agent, so llm_delegation must not reach the per-agent monitor. If this
    boundary should ever move, it is a measured governance decision, not a
    refactor side effect."""
    source = inspect.getsource(llm_delegation)
    assert "process_update" not in source
    assert "get_or_create_monitor" not in source


def test_registry_lists_the_endpoints_served_models(monkeypatch):
    """An agent picks the local model from what list_inference_hosts reports
    (operator decision 2026-10-21)."""
    monkeypatch.setenv("UNITARES_MODEL_ID", "qwen3:8b")
    monkeypatch.setattr(inference_registry, "_ollama_available", lambda: True)
    monkeypatch.setattr(inference_registry, "served_models", lambda: ("gemma4:latest", "qwen3:8b"))
    host = inference_registry.get_inference_host("ollama:local")
    assert host["models"] == ["gemma4:latest", "qwen3:8b"]
    assert "consult(model=...)" in host["notes"]

    monkeypatch.setattr(inference_registry, "_ollama_available", lambda: False)
    assert inference_registry.get_inference_host("ollama:local")["models"] == ["qwen3:8b"]


def test_unnamed_model_and_external_endpoint_are_both_reported(monkeypatch):
    """Review finding on #2971: with no model named and an external endpoint,
    the host record names both problems, not only the first."""
    monkeypatch.delenv("UNITARES_MODEL_ID", raising=False)
    monkeypatch.setenv("UNITARES_MODEL_BASE_URL", "https://models.example.com/v1")
    monkeypatch.setattr(inference_registry, "_ollama_available", lambda: False)
    host = inference_registry.get_inference_host("ollama:local")
    assert host["models"] == []
    assert host["accepts_host_id_from"] == []
    assert "classifies external" in host["notes"]
    assert "No model is named" in host["notes"]
