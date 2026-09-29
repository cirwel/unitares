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
        "http://[::ffff:127.0.0.1]:11434/v1",  # IPv4-mapped loopback
    ],
)
def test_private_ip_literals_are_local(no_dns, url):
    assert env.classify_endpoint(url).privacy == env.LOCAL


@pytest.mark.parametrize(
    "url",
    [
        "http://8.8.8.8/v1",
        "http://100.100.1.2:8000/v1",
        "http://[2001:db8::1]/v1",
        "http://[fe80::1]/v1",
        # RFC 4193 is not local by default: the REST access checks do not
        # trust it either, and "local" means one thing server-wide.
        "http://[fd12:3456::1]:8000/v1",
    ],
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
        "src.mcp_handlers.support.llm_delegation.direct_urlopen", MagicMock(side_effect=AssertionError("request sent"))
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

    monkeypatch.setattr(env, "direct_urlopen", fake)
    assert env.is_ollama_endpoint() is True
    assert env.is_ollama_endpoint() is True
    assert calls == [("http://localhost:11434/api/version", env.LOCAL_PROBE_TIMEOUT_S)]


def test_detection_says_no_for_a_server_without_the_route(monkeypatch):
    def not_found(url, timeout=0):
        raise env.urllib.error.HTTPError(url, 404, "nf", {}, None)

    monkeypatch.setattr(env, "direct_urlopen", not_found)
    assert env.is_ollama_endpoint("http://vllm.lan:8000/v1") is False
    monkeypatch.setattr(env, "direct_urlopen", lambda url, timeout=0: _Resp(b'{"object": "x"}'))
    assert env.is_ollama_endpoint("http://other.lan:8000/v1") is False

    def silent(url, timeout=0):
        raise env.urllib.error.URLError("timed out")

    monkeypatch.setattr(env, "direct_urlopen", silent)
    assert env.is_ollama_endpoint("http://nobody.lan:8000/v1") is False


def test_a_busy_ollama_that_misses_the_budget_stays_ollama(monkeypatch):
    clock = [100.0]
    monkeypatch.setattr(env.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(env, "direct_urlopen", lambda url, timeout=0: _Resp(b'{"version": "0.12.0"}'))
    assert env.is_ollama_endpoint() is True

    def silent(url, timeout=0):
        raise env.urllib.error.URLError("timed out")

    monkeypatch.setattr(env, "direct_urlopen", silent)
    clock[0] += 10  # past the cache
    assert env.is_ollama_endpoint() is True

    def not_found(url, timeout=0):
        raise env.urllib.error.HTTPError(url, 404, "nf", {}, None)

    monkeypatch.setattr(env, "direct_urlopen", not_found)
    clock[0] += 10
    assert env.is_ollama_endpoint() is False  # an answer without the route is definitive


@pytest.mark.asyncio
async def test_structured_call_skips_api_chat_when_not_ollama(monkeypatch):
    from src.mcp_handlers.support import llm_delegation

    monkeypatch.setattr(llm_delegation, "is_ollama_endpoint", lambda: False)
    monkeypatch.setattr(
        "src.mcp_handlers.support.llm_delegation.direct_urlopen", MagicMock(side_effect=AssertionError("/api/chat attempted"))
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
    monkeypatch.setattr("src.mcp_handlers.support.llm_delegation.direct_urlopen", fake)
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


# --- the spawn boundary and discovery follow the classification ---------------


def test_reviewer_spawn_blanks_classifier_settings_the_server_leaves_unset(monkeypatch):
    """The orchestrator merges the spawn env over its own; an omitted key would
    let the child keep a stale UNITARES_MODEL_PRIVACY=local and classify an
    external endpoint as local where this server refuses."""
    from src.mcp_handlers.dialectic import orchestrator_dispatch as od

    for name in od._CLASSIFIER_SETTINGS:
        monkeypatch.delenv(name, raising=False)
    spec = od._build_spec("s", {"root_cause": "r", "proposed_conditions": [], "reasoning": ""}, None)
    for name in od._CLASSIFIER_SETTINGS:
        assert spec["env"][name] == ""

    monkeypatch.setenv("UNITARES_MODEL_LOCAL_HOSTS", "vllm")
    spec = od._build_spec("s", {"root_cause": "r", "proposed_conditions": [], "reasoning": ""}, None)
    assert spec["env"]["UNITARES_MODEL_LOCAL_HOSTS"] == "vllm"


@pytest.mark.parametrize(
    "base, expected",
    [
        ("https://models.example.com/v1", ("models.example.com", 443)),
        ("http://models.internal/v1", ("models.internal", 80)),
        ("http://localhost:11434/v1", ("localhost", 11434)),
        ("http://10.0.0.5:8000/v1", ("10.0.0.5", 8000)),
    ],
)
def test_availability_probe_uses_the_port_requests_go_to(monkeypatch, base, expected):
    from src.mcp_handlers.support import inference_registry

    monkeypatch.setenv("UNITARES_MODEL_BASE_URL", base)
    assert inference_registry._ollama_host_port() == expected


def test_discovery_reports_an_external_endpoint_as_external(external_endpoint, monkeypatch):
    from src.mcp_handlers.support import inference_registry

    monkeypatch.setattr(inference_registry, "_ollama_available", lambda: False)
    host = inference_registry.get_inference_host("ollama:local")
    assert host["privacy_class"] == "external"
    assert host["cost_class"] == "unknown"
    # Not advertised as a call_model host_id: that call forces local privacy.
    assert host["accepts_host_id_from"] == []
    assert "privacy='auto'" in host["notes"]


def test_discovery_keeps_the_default_endpoint_local(monkeypatch):
    from src.mcp_handlers.support import inference_registry

    monkeypatch.setattr(inference_registry, "_ollama_available", lambda: False)
    host = inference_registry.get_inference_host("ollama:local")
    assert host["privacy_class"] == "local"
    assert host["cost_class"] == "local_free"


@pytest.mark.asyncio
async def test_host_id_ollama_local_refuses_before_the_availability_probe(external_endpoint, monkeypatch):
    """host_id='ollama:local' forces a local request. Building its registry record
    runs a socket probe to the endpoint, so the refusal has to come first."""
    from src.mcp_handlers.support import inference_registry, model_inference

    def probe_must_not_run():
        raise AssertionError("the availability probe contacted the endpoint")

    monkeypatch.setattr(inference_registry, "_ollama_available", probe_must_not_run)
    client = _forbid_client(monkeypatch)
    outcome = await model_inference.run_model_inference(
        model_inference.CallModelRequest(
            prompt="hi", requesting_agent_uuid=None, host_id="ollama:local"
        )
    )
    assert not outcome.ok
    assert outcome.failure.code == "MODEL_ENDPOINT_NOT_LOCAL"
    assert client.call_count == 0


def test_availability_probe_reaches_an_ipv6_endpoint(monkeypatch):
    """The probe must not assume IPv4: a local model on [::1] is available."""
    import socket as _socket

    from src.mcp_handlers.support import inference_registry

    listener = _socket.socket(_socket.AF_INET6, _socket.SOCK_STREAM)
    try:
        listener.bind(("::1", 0))
    except OSError:
        listener.close()
        pytest.skip("no IPv6 loopback on this host")
    listener.listen(1)
    port = listener.getsockname()[1]
    try:
        monkeypatch.setenv("UNITARES_MODEL_BASE_URL", f"http://[::1]:{port}/v1")
        assert inference_registry._probe_ollama_socket() is True
    finally:
        listener.close()


def test_installer_warns_when_the_endpoint_will_classify_external(capsys):
    import sys as _sys

    _sys.path.insert(0, "scripts/install")
    import choose_model

    assert choose_model.print_privacy_note("http://host.docker.internal:11434/v1", {}) is True
    assert "external" not in capsys.readouterr().out

    assert choose_model.print_privacy_note("http://vllm:8000/v1", {}) is False
    out = capsys.readouterr().out
    assert "treat http://vllm:8000/v1 as external" in out
    assert "UNITARES_MODEL_LOCAL_HOSTS" in out

    assert choose_model.print_privacy_note(
        "http://vllm:8000/v1", {"UNITARES_MODEL_LOCAL_HOSTS": "vllm"}
    ) is True


# --- redirects never carry a local prompt to an unclassified host -------------


class _RedirectHarness:
    """A local server that 307s every POST to a second server, which records hits."""

    def __init__(self):
        import http.server
        import threading

        harness = self
        self.target_hits = 0

        class Target(http.server.BaseHTTPRequestHandler):
            def do_POST(self):  # noqa: N802
                harness.target_hits += 1
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(b"{}")

            def log_message(self, *a):
                pass

        self.target = http.server.HTTPServer(("127.0.0.1", 0), Target)
        target_url = f"http://127.0.0.1:{self.target.server_address[1]}"

        class Redirector(http.server.BaseHTTPRequestHandler):
            def do_POST(self):  # noqa: N802
                length = int(self.headers.get("Content-Length", "0"))
                self.rfile.read(length)
                self.send_response(307)
                # A fixed path: the redirect target does not echo the request.
                self.send_header("Location", target_url + "/v1/chat/completions")
                self.send_header("Content-Length", "0")
                self.end_headers()

            def log_message(self, *a):
                pass

        self.redirector = http.server.HTTPServer(("127.0.0.1", 0), Redirector)
        self.base = f"http://127.0.0.1:{self.redirector.server_address[1]}/v1"
        self.threads = [
            threading.Thread(target=s.serve_forever, daemon=True)
            for s in (self.target, self.redirector)
        ]
        for t in self.threads:
            t.start()

    def close(self):
        for s in (self.target, self.redirector):
            s.shutdown()
            s.server_close()


@pytest.mark.asyncio
async def test_a_redirect_from_a_local_endpoint_is_not_followed(monkeypatch):
    from src.mcp_handlers.support import model_inference

    harness = _RedirectHarness()
    try:
        monkeypatch.setenv("UNITARES_MODEL_BASE_URL", harness.base)
        outcome = await model_inference.run_model_inference(
            model_inference.CallModelRequest(
                prompt="secret", requesting_agent_uuid=None, provider="ollama", privacy="local",
                timeout_s=5,
            )
        )
        assert not outcome.ok
        assert harness.target_hits == 0
    finally:
        harness.close()


def test_every_local_route_client_refuses_redirects():
    from src.local_inference_env import no_redirect_http_client

    assert no_redirect_http_client().follow_redirects is False
    assert no_redirect_http_client(asynchronous=False).follow_redirects is False
    for path in (
        "src/mcp_handlers/support/model_inference.py",
        "src/mcp_handlers/support/llm_delegation.py",
        "agents/dialectic_reviewer/reviewer.py",
        "agents/local_resident/runner.py",
    ):
        with open(path, encoding="utf-8") as fh:
            assert "http_client=no_redirect_http_client(" in fh.read(), path


def test_installer_classifies_with_the_settings_compose_will_pass(monkeypatch):
    """A classifier setting exported in the shell outranks the env file in
    Compose, so the installer's check must read it the same way."""
    import sys as _sys

    _sys.path.insert(0, "scripts/install")
    import choose_model

    env_text = "UNITARES_MODEL_PRIVACY=local\n"
    monkeypatch.delenv("UNITARES_MODEL_PRIVACY", raising=False)
    assert choose_model.composed_classifier_values(env_text)["UNITARES_MODEL_PRIVACY"] == "local"
    monkeypatch.setenv("UNITARES_MODEL_PRIVACY", "external")
    values = choose_model.composed_classifier_values(env_text)
    assert values["UNITARES_MODEL_PRIVACY"] == "external"
    assert choose_model.endpoint_is_local("http://host.docker.internal:11434/v1", values)[0] is False


def test_the_availability_probe_has_one_budget_across_addresses(monkeypatch):
    import time as _time

    from src.mcp_handlers.support import inference_registry

    addresses = [(2, 1, 6, "", (f"10.0.0.{i}", 11434)) for i in range(1, 9)]
    monkeypatch.setattr(inference_registry.socket, "getaddrinfo", lambda *a, **k: addresses)

    class SlowSocket:
        def __init__(self, *a):
            self.timeout = None

        def settimeout(self, t):
            self.timeout = t

        def connect_ex(self, _addr):
            _time.sleep(self.timeout)
            return 1

        def close(self):
            pass

    monkeypatch.setattr(inference_registry.socket, "socket", SlowSocket)
    monkeypatch.setenv("UNITARES_MODEL_BASE_URL", "http://models.internal:11434/v1")
    started = _time.monotonic()
    assert inference_registry._probe_ollama_socket() is False
    assert _time.monotonic() - started < 0.9



# --- no environment proxy for a local endpoint --------------------------------


def test_local_clients_ignore_environment_proxies_external_ones_keep_them():
    from src.local_inference_env import no_redirect_http_client

    assert no_redirect_http_client().trust_env is False
    assert no_redirect_http_client(asynchronous=False).trust_env is False
    assert no_redirect_http_client(local=False).trust_env is True


@pytest.mark.asyncio
@pytest.mark.parametrize("base, trusts_env", [
    ("http://localhost:11434/v1", False),
    (EXTERNAL_BASE, True),
])
async def test_call_model_client_proxy_policy_follows_the_classification(monkeypatch, base, trusts_env):
    from src.mcp_handlers.support import model_inference

    monkeypatch.setenv("UNITARES_MODEL_BASE_URL", base)
    response = SimpleNamespace(
        choices=[SimpleNamespace(message=SimpleNamespace(content="ok", reasoning=None), finish_reason="stop")],
        usage=SimpleNamespace(total_tokens=3),
        model="m",
    )
    fake = MagicMock()
    fake.return_value.chat.completions.create = AsyncMock(return_value=response)
    fake.return_value.close = AsyncMock()
    monkeypatch.setattr(model_inference, "OpenAI", fake)
    await model_inference.run_model_inference(
        model_inference.CallModelRequest(
            prompt="hi", requesting_agent_uuid=None, provider="ollama", privacy="cloud"
        )
    )
    assert fake.call_args.kwargs["http_client"].trust_env is trusts_env


def test_native_route_reaches_a_local_server_directly_despite_a_proxy(monkeypatch):
    import http.server
    import threading

    from src.local_inference_env import direct_urlopen

    class Ok(http.server.BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802
            self.send_response(200)
            self.send_header("Content-Length", "2")
            self.end_headers()
            self.wfile.write(b"ok")

        def log_message(self, *a):
            pass

    server = http.server.HTTPServer(("127.0.0.1", 0), Ok)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        # A proxy that accepts nothing: going through it would fail.
        monkeypatch.setenv("HTTP_PROXY", "http://127.0.0.1:1")
        monkeypatch.setenv("http_proxy", "http://127.0.0.1:1")
        monkeypatch.delenv("NO_PROXY", raising=False)
        monkeypatch.delenv("no_proxy", raising=False)
        url = f"http://127.0.0.1:{server.server_address[1]}/api/version"
        with direct_urlopen(url, timeout=2) as resp:
            assert resp.read() == b"ok"
    finally:
        server.shutdown()
        server.server_close()


def test_reviewer_spawn_always_carries_the_servers_resolved_endpoint(monkeypatch):
    """With the server on defaults, a stale endpoint in the orchestrator's own
    environment must not reach the child: the resolved values always override."""
    from src.mcp_handlers.dialectic import orchestrator_dispatch as od

    for name in ("UNITARES_MODEL_BASE_URL", "UNITARES_MODEL_ID", "UNITARES_OLLAMA_BASE",
                 "UNITARES_OLLAMA_BASE_URL", "UNITARES_LLM_MODEL"):
        monkeypatch.delenv(name, raising=False)
    spec = od._build_spec("s", {"root_cause": "r", "proposed_conditions": [], "reasoning": ""}, None)
    assert spec["env"]["UNITARES_MODEL_BASE_URL"] == env.model_base_url()
    assert spec["env"]["UNITARES_MODEL_ID"] == env.default_local_model()


def test_a_stalled_resolver_cannot_hold_the_probe(monkeypatch):
    import threading as _threading
    import time as _time

    from src.mcp_handlers.support import inference_registry

    release = _threading.Event()

    def stalled(*_a, **_k):
        release.wait(5)
        return []

    monkeypatch.setattr(inference_registry.socket, "getaddrinfo", stalled)
    monkeypatch.setenv("UNITARES_MODEL_BASE_URL", "http://models.internal:8000/v1")
    started = _time.monotonic()
    try:
        assert inference_registry._probe_ollama_socket() is False
        assert _time.monotonic() - started < 0.9
    finally:
        release.set()


def test_a_stalled_resolver_is_reused_not_multiplied(monkeypatch):
    import threading as _threading

    from src.mcp_handlers.support import inference_registry

    release = _threading.Event()
    calls = []

    def stalled(*_a, **_k):
        calls.append(1)
        release.wait(5)
        return []

    monkeypatch.setattr(inference_registry.socket, "getaddrinfo", stalled)
    try:
        for _ in range(4):
            assert inference_registry._resolve_within("models.internal", 8000, 0.05) == []
        assert len(calls) == 1
    finally:
        release.set()


def test_local_clients_keep_the_environment_ca_while_skipping_proxies(monkeypatch):
    import httpx

    from src import local_inference_env

    calls = []
    real = httpx.create_ssl_context

    def spy(*a, **k):
        calls.append(k.get("trust_env"))
        return real(*a, **k)

    monkeypatch.setattr(httpx, "create_ssl_context", spy)
    client = local_inference_env.no_redirect_http_client()
    assert client.trust_env is False
    assert calls == [True]  # SSL_CERT_FILE / SSL_CERT_DIR still honoured


@pytest.mark.asyncio
@pytest.mark.parametrize("base, is_ollama, expect", [
    ("http://localhost:11434/v1", False, "Ollama is not reachable"),
    ("http://10.0.0.5:8000/v1", False, "The model server at http://10.0.0.5:8000/v1 is not reachable"),
    ("http://10.0.0.5:11434/v1", True, "Ollama is not reachable"),
])
async def test_unreachable_hint_names_ollama_only_for_ollama(monkeypatch, base, is_ollama, expect):
    from src.mcp_handlers.support import model_inference

    monkeypatch.setenv("UNITARES_MODEL_BASE_URL", base)
    monkeypatch.setattr(model_inference, "is_ollama_endpoint", lambda *a, **k: is_ollama)
    fake = MagicMock()
    fake.return_value.chat.completions.create = AsyncMock(side_effect=Exception("Connection error."))
    fake.return_value.close = AsyncMock()
    monkeypatch.setattr(model_inference, "OpenAI", fake)
    outcome = await model_inference.run_model_inference(
        model_inference.CallModelRequest(prompt="hi", requesting_agent_uuid=None, provider="ollama")
    )
    assert not outcome.ok
    assert expect in outcome.failure.recovery["action"]


def test_a_resolver_slower_than_the_budget_still_lets_the_next_probe_connect(monkeypatch):
    """A lookup that finishes after its caller gave up is reused by the next
    probe, so a slow-but-working resolver does not read as down forever."""
    import time as _time

    from src.mcp_handlers.support import inference_registry

    answer = [(2, 1, 6, "", ("10.0.0.9", 8000))]

    def slow(*_a, **_k):
        _time.sleep(0.2)
        return answer

    monkeypatch.setattr(inference_registry.socket, "getaddrinfo", slow)
    inference_registry._resolve_inflight.pop(("slow.internal", 8000), None)
    assert inference_registry._resolve_within("slow.internal", 8000, 0.05) == []
    _time.sleep(0.3)  # the lookup finishes in the background
    assert inference_registry._resolve_within("slow.internal", 8000, 0.05) == answer



def test_a_unique_local_range_is_local_once_it_is_trusted(no_dns, monkeypatch):
    monkeypatch.setenv("UNITARES_TRUSTED_NETWORKS", "fd12:3456::/32")
    assert env.classify_endpoint("http://[fd12:3456::1]:8000/v1").privacy == env.LOCAL


@pytest.mark.asyncio
async def test_auto_routing_names_an_unreachable_non_ollama_server(monkeypatch):
    from src.mcp_handlers.support import model_inference

    monkeypatch.setenv("UNITARES_MODEL_BASE_URL", "http://10.0.0.5:8000/v1")
    monkeypatch.delenv("HF_TOKEN", raising=False)
    monkeypatch.delenv("HUGGINGFACE_TOKEN", raising=False)
    monkeypatch.setattr(model_inference, "_ollama_available", lambda: False)
    monkeypatch.setattr(model_inference, "is_ollama_endpoint", lambda *a, **k: False)
    outcome = await model_inference.run_model_inference(
        model_inference.CallModelRequest(
            prompt="hi", requesting_agent_uuid=None, provider="auto", privacy="cloud"
        )
    )
    assert outcome.failure.code == "MISSING_CONFIG"
    assert "http://10.0.0.5:8000/v1" in outcome.failure.message
    assert "Ollama" not in outcome.failure.recovery["action"]


def test_a_url_without_a_scheme_means_http(no_dns, monkeypatch):
    monkeypatch.setenv("UNITARES_MODEL_BASE_URL", "localhost:11434/v1")
    assert env.model_base_url() == "http://localhost:11434/v1"
    assert env.classify_endpoint().privacy == env.LOCAL
    assert env.normalize_ollama_base("gpu-box:11434") == "http://gpu-box:11434"


def test_a_non_json_answer_is_not_ollama(monkeypatch):
    """A 200 with a proxy page is an answer, not silence: not Ollama."""
    monkeypatch.setattr(env, "direct_urlopen", lambda url, timeout=0: _Resp(b"<html>portal</html>"))
    assert env._probe_ollama_version("http://10.0.0.7:11434", 0.5) is False
