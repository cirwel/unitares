"""privacy='local' means the prompt stays on machines the operator runs.

The local model endpoint can now name any OpenAI-compatible server, so the
server classifies it from the URL alone (never DNS) and every path that reads
it refuses a privacy='local' request against an ``external`` endpoint before
any network call. A default install (Ollama on loopback) classifies local and
behaves as before. Also pinned here: the in-process reviewer's native
``/api/chat`` attempt happens only when the endpoint answers like Ollama.
"""

from __future__ import annotations

import socket
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src import local_inference_env as env
from src import trusted_networks

EXTERNAL_BASE = "https://models.example.com/v1"


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    for name in (
        *env.LOCAL_MODEL_SETTINGS,
        "UNITARES_OLLAMA_BASE",
        "UNITARES_OLLAMA_BASE_URL",
        "UNITARES_LLM_MODEL",
        "UNITARES_TRUSTED_NETWORKS",
    ):
        monkeypatch.delenv(name, raising=False)
    env._ollama_detect_cache.clear()
    from src.mcp_handlers.support import llm_delegation

    llm_delegation._refusals_logged.clear()


@pytest.fixture
def no_dns(monkeypatch):
    """Any name resolution or connection is a test failure."""

    def boom(*_a, **_k):
        raise AssertionError("the classifier consulted the network")

    for name in ("getaddrinfo", "gethostbyname", "gethostbyname_ex", "create_connection"):
        monkeypatch.setattr(socket, name, boom)


@pytest.fixture
def external_endpoint(monkeypatch):
    monkeypatch.setenv("UNITARES_MODEL_BASE_URL", EXTERNAL_BASE)


# --- the classifier -----------------------------------------------------------


def test_default_install_is_local(no_dns):
    result = env.classify_endpoint()
    assert result.privacy == env.LOCAL
    assert env.model_base_url() == "http://localhost:11434/v1"


@pytest.mark.parametrize(
    "url",
    [
        "http://127.0.0.1:11434/v1",
        "http://[::1]:11434/v1",
        "http://10.2.3.4:8000/v1",
        "http://172.17.0.1:11434/v1",
        "http://192.168.1.20:1234/v1",
        "http://[fd12:3456::1]:8000/v1",  # RFC 4193
        "http://[::ffff:127.0.0.1]:11434/v1",  # IPv4-mapped loopback
    ],
)
def test_private_ip_literals_are_local(no_dns, url):
    assert env.classify_endpoint(url).privacy == env.LOCAL


@pytest.mark.parametrize(
    "url",
    ["http://8.8.8.8/v1", "http://100.100.1.2:8000/v1", "http://[2001:db8::1]/v1", "http://[fe80::1]/v1"],
)
def test_other_ip_literals_are_external(no_dns, url):
    result = env.classify_endpoint(url)
    assert result.privacy == env.EXTERNAL
    assert "UNITARES_TRUSTED_NETWORKS" in result.reason


def test_a_tailnet_address_is_local_once_its_network_is_trusted(no_dns, monkeypatch):
    url = "http://100.100.1.2:11434/v1"
    assert env.classify_endpoint(url).privacy == env.EXTERNAL
    monkeypatch.setenv("UNITARES_TRUSTED_NETWORKS", "100.64.0.0/10")
    assert env.classify_endpoint(url).privacy == env.LOCAL


@pytest.mark.parametrize(
    "url", ["http://localhost:11434/v1", "http://LocalHost.:8000/v1", "http://host.docker.internal:11434/v1"]
)
def test_builtin_local_hostnames(no_dns, url):
    assert env.classify_endpoint(url).privacy == env.LOCAL


def test_other_hostnames_are_external_unless_listed(no_dns, monkeypatch):
    url = "http://vllm:8000/v1"
    result = env.classify_endpoint(url)
    assert result.privacy == env.EXTERNAL
    assert "UNITARES_MODEL_LOCAL_HOSTS" in result.reason
    monkeypatch.setenv("UNITARES_MODEL_LOCAL_HOSTS", "gpu-box.lan, VLLM ")
    assert env.classify_endpoint(url).privacy == env.LOCAL
    assert env.classify_endpoint("http://gpu-box.lan:11434/v1").privacy == env.LOCAL
    # Listing is exact: a subdomain is not covered.
    assert env.classify_endpoint("http://a.gpu-box.lan/v1").privacy == env.EXTERNAL


def test_a_name_that_resolves_privately_is_still_external(no_dns):
    """No DNS: 'localtest.me' style names resolve to 127.0.0.1 publicly, and
    the classifier must not find that out."""
    assert env.classify_endpoint("http://localtest.me:11434/v1").privacy == env.EXTERNAL


def test_privacy_override_wins_both_ways(no_dns, monkeypatch):
    monkeypatch.setenv("UNITARES_MODEL_PRIVACY", "local")
    assert env.classify_endpoint(EXTERNAL_BASE).privacy == env.LOCAL
    monkeypatch.setenv("UNITARES_MODEL_PRIVACY", "external")
    assert env.classify_endpoint("http://localhost:11434/v1").privacy == env.EXTERNAL
    monkeypatch.setenv("UNITARES_MODEL_PRIVACY", "sure")  # not a value: ignored
    assert env.classify_endpoint(EXTERNAL_BASE).privacy == env.EXTERNAL


def test_access_checks_and_the_classifier_share_one_trusted_set():
    from src.http_routes import access

    assert access.extra_trusted_networks is trusted_networks.extra_trusted_networks
    assert list(access._TRUSTED_NETWORKS) == list(trusted_networks.BUILTIN_TRUSTED_NETWORKS)
    assert env.is_trusted_address is trusted_networks.is_trusted_address


def test_the_refusal_names_the_setting_and_not_the_url(external_endpoint):
    with pytest.raises(env.EndpointNotLocalError) as info:
        env.require_local_endpoint()
    assert info.value.code == "MODEL_ENDPOINT_NOT_LOCAL"
    assert "UNITARES_MODEL_LOCAL_HOSTS" in str(info.value)
    assert "/v1" not in str(info.value)


# --- call_model and consult ---------------------------------------------------


def _forbid_client(monkeypatch):
    from src.mcp_handlers.support import model_inference

    client = MagicMock(side_effect=AssertionError("a client was built for a refused request"))
    monkeypatch.setattr(model_inference, "OpenAI", client)
    return client


@pytest.mark.asyncio
@pytest.mark.parametrize("privacy", [None, "local"])
async def test_call_model_refuses_local_privacy_before_any_request(external_endpoint, monkeypatch, privacy):
    from src.mcp_handlers.support.model_inference import CallModelRequest, run_model_inference

    client = _forbid_client(monkeypatch)
    outcome = await run_model_inference(
        CallModelRequest(prompt="hi", requesting_agent_uuid=None, provider="ollama", privacy=privacy)
    )
    assert not outcome.ok
    assert outcome.failure.code == "MODEL_ENDPOINT_NOT_LOCAL"
    assert client.call_count == 0


@pytest.mark.asyncio
async def test_call_model_sends_to_an_external_endpoint_when_privacy_allows(external_endpoint, monkeypatch):
    from src.mcp_handlers.support import model_inference

    response = SimpleNamespace(
        choices=[SimpleNamespace(message=SimpleNamespace(content="ok", reasoning=None), finish_reason="stop")],
        usage=SimpleNamespace(total_tokens=3),
        model="m",
    )
    fake = MagicMock()
    fake.return_value.chat.completions.create = AsyncMock(return_value=response)
    fake.return_value.close = AsyncMock()
    monkeypatch.setattr(model_inference, "OpenAI", fake)
    outcome = await model_inference.run_model_inference(
        model_inference.CallModelRequest(
            prompt="hi", requesting_agent_uuid=None, provider="ollama", privacy="cloud"
        )
    )
    assert outcome.ok, outcome.failure
    assert fake.call_args.kwargs["base_url"] == EXTERNAL_BASE
    # The record says where the prompt went, not what ollama:local usually is.
    assert outcome.inference["privacy_class"] == "external"
    assert outcome.inference["cost_class"] == "unknown"
    assert any(w.startswith("local_route_endpoint_external") for w in outcome.inference["warnings"])


@pytest.mark.asyncio
async def test_a_local_endpoint_keeps_the_local_record(monkeypatch):
    from src.mcp_handlers.support import model_inference

    response = SimpleNamespace(
        choices=[SimpleNamespace(message=SimpleNamespace(content="ok", reasoning=None), finish_reason="stop")],
        usage=SimpleNamespace(total_tokens=3),
        model="m",
    )
    fake = MagicMock()
    fake.return_value.chat.completions.create = AsyncMock(return_value=response)
    fake.return_value.close = AsyncMock()
    monkeypatch.setattr(model_inference, "OpenAI", fake)
    outcome = await model_inference.run_model_inference(
        model_inference.CallModelRequest(
            prompt="hi", requesting_agent_uuid=None, provider="ollama", privacy="cloud"
        )
    )
    assert outcome.ok, outcome.failure
    assert outcome.inference["privacy_class"] == "local"
    assert outcome.inference["warnings"] == []


@pytest.mark.asyncio
async def test_consult_local_is_refused_with_the_named_code(external_endpoint, monkeypatch):
    from src.mcp_handlers.support.consultation import ConsultRequest, run_consultation

    client = _forbid_client(monkeypatch)
    outcome = await run_consultation(ConsultRequest(brief="question", requester_uuid=None, privacy="local"))
    assert outcome.failure is not None
    assert outcome.failure.code == "MODEL_ENDPOINT_NOT_LOCAL"
    assert "cloud_allowed" in outcome.failure.recovery["action"]
    assert client.call_count == 0


# --- the internal lane (llm_delegation) and its callers -------------------------


@pytest.mark.asyncio
async def test_internal_lane_sends_nothing_to_an_external_endpoint(external_endpoint, monkeypatch):
    from src.mcp_handlers.support import llm_delegation

    monkeypatch.setattr(
        llm_delegation, "_get_ollama_client", MagicMock(side_effect=AssertionError("client built"))
    )
    monkeypatch.setattr(
        "urllib.request.urlopen", MagicMock(side_effect=AssertionError("request sent"))
    )
    monkeypatch.setattr(
        llm_delegation, "_ollama_available", MagicMock(side_effect=AssertionError("probe sent"))
    )

    assert await llm_delegation.call_local_llm("p") is None
    assert await llm_delegation.call_local_llm_structured([{"role": "user", "content": "p"}], {}) is None
    assert await llm_delegation.is_llm_available() is False
    # Check-in enrichments and knowledge synthesis ride these calls.
    assert await llm_delegation.explain_anomaly("a", "guide", "d") is None
    assert await llm_delegation.synthesize_results([{"summary": "s", "type": "note"}]) is None


@pytest.mark.asyncio
async def test_knowledge_rollup_falls_back_to_its_deterministic_text(external_endpoint, monkeypatch):
    from src.mcp_handlers.knowledge import synthesis
    from src.mcp_handlers.support import llm_delegation

    monkeypatch.setattr(
        llm_delegation, "_get_ollama_client", MagicMock(side_effect=AssertionError("client built"))
    )
    _, source = await synthesis._generate_narrative(
        "t", [{"id": "1", "summary": "s", "type": "note"}], [], use_llm=True
    )
    assert source == "deterministic"


@pytest.mark.asyncio
async def test_synthetic_reviewer_abstains_with_the_reason(external_endpoint, monkeypatch):
    from src.dialectic_protocol import DialecticSession, DialecticPhase
    from src.mcp_handlers.dialectic import handlers
    from src.mcp_handlers.support import llm_delegation

    session = DialecticSession(paused_agent_id="agent-paused", reviewer_agent_id=None, session_type="recovery")
    session.phase = DialecticPhase.ANTITHESIS
    abstained = AsyncMock(return_value=True)
    monkeypatch.setattr(handlers, "emit_reviewer_abstained", abstained)
    monkeypatch.setattr(
        llm_delegation, "generate_antithesis", AsyncMock(side_effect=AssertionError("model asked"))
    )
    result = await handlers._run_synthetic_review(session, {"root_cause": "r"}, "agent-paused")
    assert result is None
    assert abstained.await_args.kwargs["reason"] == "local_model_endpoint_not_local"


@pytest.mark.asyncio
async def test_llm_assisted_dialectic_is_refused_by_name(external_endpoint, monkeypatch):
    from src.mcp_handlers.dialectic import handlers
    from src.mcp_handlers.support import llm_delegation
    from tests.helpers import parse_result

    monkeypatch.setattr(handlers, "require_registered_agent", lambda args: ("agent-paused", None))
    monkeypatch.setattr(handlers, "resolve_agent_uuid", lambda args, agent_id: "agent-paused")
    monkeypatch.setattr(
        llm_delegation, "run_full_dialectic", AsyncMock(side_effect=AssertionError("model asked"))
    )
    result = parse_result(await handlers.handle_llm_assisted_dialectic({"root_cause": "r"}))
    assert result["success"] is False
    assert result["error_code"] == "MODEL_ENDPOINT_NOT_LOCAL"


# --- the agent processes -------------------------------------------------------


@pytest.mark.asyncio
async def test_orchestrated_reviewer_local_backend_abstains(monkeypatch):
    from agents.dialectic_reviewer import reviewer

    monkeypatch.setattr(reviewer, "OLLAMA_BASE_URL", EXTERNAL_BASE)
    monkeypatch.delenv("UNITARES_DIALECTIC_REVIEWER_HOST", raising=False)
    with patch("openai.AsyncOpenAI", side_effect=AssertionError("client built")):
        text = await reviewer.obtain_reviewer_text("prompt")
    assert text == ""
    assert reviewer.parse_reviewer_verdict(text).judgment_formed is False
    warnings = reviewer.reviewer_backend_provenance()["warnings"]
    assert any("MODEL_ENDPOINT_NOT_LOCAL" in w for w in warnings)


@pytest.mark.asyncio
async def test_local_resident_runner_refuses(monkeypatch):
    from agents.local_resident import runner

    monkeypatch.setattr(runner, "OLLAMA_BASE_URL", EXTERNAL_BASE)
    with patch("openai.AsyncOpenAI", side_effect=AssertionError("client built")):
        with pytest.raises(env.EndpointNotLocalError):
            await runner.call_local_model("prompt")


# --- Ollama detection gates the native /api/chat route ---------------------------


class _Resp:
    def __init__(self, body: bytes, status: int = 200):
        self._body, self.status = body, status

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def read(self, *a):
        return self._body


def test_detection_is_one_cached_version_probe(monkeypatch):
    calls = []

    def fake(url, timeout=0):
        calls.append((url, timeout))
        return _Resp(b'{"version": "0.12.0"}')

    monkeypatch.setattr(env.urllib.request, "urlopen", fake)
    assert env.is_ollama_endpoint() is True
    assert env.is_ollama_endpoint() is True
    assert calls == [("http://localhost:11434/api/version", env.LOCAL_PROBE_TIMEOUT_S)]


def test_detection_says_no_for_a_server_without_the_route(monkeypatch):
    def not_found(url, timeout=0):
        raise env.urllib.error.HTTPError(url, 404, "nf", {}, None)

    monkeypatch.setattr(env.urllib.request, "urlopen", not_found)
    assert env.is_ollama_endpoint("http://vllm.lan:8000/v1") is False
    monkeypatch.setattr(env.urllib.request, "urlopen", lambda url, timeout=0: _Resp(b'{"object": "x"}'))
    assert env.is_ollama_endpoint("http://other.lan:8000/v1") is False

    def silent(url, timeout=0):
        raise env.urllib.error.URLError("timed out")

    monkeypatch.setattr(env.urllib.request, "urlopen", silent)
    assert env.is_ollama_endpoint("http://nobody.lan:8000/v1") is False


def test_a_busy_ollama_that_misses_the_budget_stays_ollama(monkeypatch):
    clock = [100.0]
    monkeypatch.setattr(env.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(env.urllib.request, "urlopen", lambda url, timeout=0: _Resp(b'{"version": "0.12.0"}'))
    assert env.is_ollama_endpoint() is True

    def silent(url, timeout=0):
        raise env.urllib.error.URLError("timed out")

    monkeypatch.setattr(env.urllib.request, "urlopen", silent)
    clock[0] += 10  # past the cache
    assert env.is_ollama_endpoint() is True

    def not_found(url, timeout=0):
        raise env.urllib.error.HTTPError(url, 404, "nf", {}, None)

    monkeypatch.setattr(env.urllib.request, "urlopen", not_found)
    clock[0] += 10
    assert env.is_ollama_endpoint() is False  # an answer without the route is definitive


@pytest.mark.asyncio
async def test_structured_call_skips_api_chat_when_not_ollama(monkeypatch):
    from src.mcp_handlers.support import llm_delegation

    monkeypatch.setattr(llm_delegation, "is_ollama_endpoint", lambda: False)
    monkeypatch.setattr(
        "urllib.request.urlopen", MagicMock(side_effect=AssertionError("/api/chat attempted"))
    )
    assert await llm_delegation.call_local_llm_structured([{"role": "user", "content": "p"}], {}) is None


@pytest.mark.asyncio
async def test_structured_call_uses_api_chat_on_ollama(monkeypatch):
    from src.mcp_handlers.support import llm_delegation

    seen = []

    def fake(req, timeout=0):
        seen.append(req.full_url)
        return _Resp(b'{"message": {"content": "{\\"a\\": 1}"}}')

    monkeypatch.setattr(llm_delegation, "is_ollama_endpoint", lambda: True)
    monkeypatch.setattr("urllib.request.urlopen", fake)
    assert await llm_delegation.call_local_llm_structured([{"role": "user", "content": "p"}], {}) == {"a": 1}
    assert seen == ["http://localhost:11434/api/chat"]


@pytest.mark.asyncio
async def test_antithesis_goes_straight_to_the_openai_route_when_not_ollama(monkeypatch):
    from src.mcp_handlers.support import llm_delegation

    monkeypatch.setattr(llm_delegation, "is_ollama_endpoint", lambda: False)
    prose = AsyncMock(return_value="a critical antithesis")
    monkeypatch.setattr(llm_delegation, "call_local_llm", prose)
    result = await llm_delegation.generate_antithesis({"root_cause": "r"})
    assert result["_degraded"] is True and result["counter_reasoning"] == "a critical antithesis"
    prose.assert_awaited_once()
