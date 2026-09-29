"""The doctor's local model endpoint check and its alias INFO lines.

``model_endpoint`` asks the configured endpoint for ``GET {base}/models`` and
looks for the configured model; ``setting_alias:<old>`` prints one INFO line
per older setting name in use, naming the new name and its removal release.
The endpoint is stubbed: nothing here touches the network.
"""

from __future__ import annotations

import importlib.util
import io
import json
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPT = REPO_ROOT / "scripts" / "dev" / "unitares_doctor.py"
NAMES = (
    "UNITARES_MODEL_BASE_URL",
    "UNITARES_MODEL_ID",
    "UNITARES_MODEL_LOCAL_HOSTS",
    "UNITARES_MODEL_PRIVACY",
    "UNITARES_OLLAMA_BASE",
    "UNITARES_OLLAMA_BASE_URL",
    "UNITARES_LLM_MODEL",
)


@pytest.fixture(scope="module")
def doctor():
    spec = importlib.util.spec_from_file_location("unitares_doctor_model", SCRIPT)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    sys.modules["unitares_doctor_model"] = mod
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    for name in NAMES:
        monkeypatch.delenv(name, raising=False)


class _Resp(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def _serve(monkeypatch, doctor, payload=None, error=None):
    seen = []

    def fake(url, timeout=0):
        seen.append(url)
        if error is not None:
            raise error
        return _Resp(json.dumps(payload).encode())

    monkeypatch.setattr(doctor.urllib.request, "urlopen", fake)
    # A local endpoint is reached through direct_urlopen, as at runtime.
    from src import local_inference_env

    monkeypatch.setattr(local_inference_env, "direct_urlopen", fake)
    return seen


def test_nothing_configured_skips_without_a_request(doctor, monkeypatch):
    seen = _serve(monkeypatch, doctor, {"data": []})
    result = doctor.check_model_endpoint(REPO_ROOT)
    assert result.status == doctor.Status.SKIP
    assert "UNITARES_MODEL_BASE_URL" in result.message
    assert seen == []


def test_lists_the_configured_model(doctor, monkeypatch):
    monkeypatch.setenv("UNITARES_MODEL_BASE_URL", "http://vllm.lan:8000/v1")
    monkeypatch.setenv("UNITARES_MODEL_ID", "qwen3:8b")
    seen = _serve(monkeypatch, doctor, {"data": [{"id": "qwen3:8b"}, {"id": "other"}]})
    result = doctor.check_model_endpoint(REPO_ROOT)
    assert result.status == doctor.Status.PASS, result
    assert "classified external" in result.message
    assert seen == ["http://vllm.lan:8000/v1/models"]


def test_a_model_the_endpoint_does_not_list_warns(doctor, monkeypatch):
    monkeypatch.setenv("UNITARES_MODEL_ID", "qwen3:8b")
    _serve(monkeypatch, doctor, {"data": [{"id": "gemma4:latest"}]})
    result = doctor.check_model_endpoint(REPO_ROOT)
    assert result.status == doctor.Status.WARN
    assert "qwen3:8b" in result.message and "gemma4:latest" in result.detail


def test_an_endpoint_without_a_named_model_warns(doctor, monkeypatch):
    monkeypatch.setenv("UNITARES_MODEL_BASE_URL", "http://localhost:11434/v1")
    _serve(monkeypatch, doctor, {"data": [{"id": "gemma4:latest"}]})
    result = doctor.check_model_endpoint(REPO_ROOT)
    assert result.status == doctor.Status.WARN
    assert "no model is named" in result.message
    assert "UNITARES_MODEL_ID" in result.detail


def test_an_unreachable_named_endpoint_warns(doctor, monkeypatch):
    """A configured endpoint that does not answer is a broken configuration,
    not an optional feature left off, so the doctor warns rather than skips."""
    monkeypatch.setenv("UNITARES_MODEL_ID", "qwen3:8b")
    _serve(monkeypatch, doctor, error=OSError("connection refused"))
    result = doctor.check_model_endpoint(REPO_ROOT)
    assert result.status == doctor.Status.WARN
    assert "did not answer" in result.message


def test_a_non_openai_answer_warns(doctor, monkeypatch):
    monkeypatch.setenv("UNITARES_MODEL_BASE_URL", "http://localhost:11434/v1")
    monkeypatch.setenv("UNITARES_MODEL_ID", "m")
    _serve(monkeypatch, doctor, {"models": [{"name": "m"}]})
    assert doctor.check_model_endpoint(REPO_ROOT).status == doctor.Status.WARN


def test_credentials_in_the_url_are_not_printed(doctor, monkeypatch):
    monkeypatch.setenv("UNITARES_MODEL_BASE_URL", "http://user:secret@vllm.lan:8000/v1")
    monkeypatch.setenv("UNITARES_MODEL_ID", "m")
    _serve(monkeypatch, doctor, {"data": [{"id": "m"}]})
    result = doctor.check_model_endpoint(REPO_ROOT)
    assert "secret" not in result.message + result.detail


def test_one_info_line_per_old_name_in_use(doctor, monkeypatch):
    monkeypatch.setenv("UNITARES_LLM_MODEL", "qwen3:8b")
    monkeypatch.setenv("UNITARES_OLLAMA_BASE", "http://gpu:11434")
    checks = [c for c in doctor.build_checks(REPO_ROOT, "postgresql://x") if c.name.startswith("setting_alias:")]
    results = doctor.run_checks(checks, "local")
    assert {r.name for r in results} == {"setting_alias:UNITARES_LLM_MODEL", "setting_alias:UNITARES_OLLAMA_BASE"}
    assert all(r.status == doctor.Status.INFO for r in results)
    by_name = {r.name: r.message for r in results}
    assert "UNITARES_MODEL_ID until v3.2.0" in by_name["setting_alias:UNITARES_LLM_MODEL"]
    assert "UNITARES_MODEL_BASE_URL" in by_name["setting_alias:UNITARES_OLLAMA_BASE"]
    rendered = doctor.render_text(results, use_color=False)
    assert "i setting_alias:UNITARES_LLM_MODEL" in rendered
    assert doctor.exit_code(results) == 0


def test_no_old_names_means_no_lines(doctor):
    checks = [c for c in doctor.build_checks(REPO_ROOT, "postgresql://x") if c.name.startswith("setting_alias:")]
    assert checks, "one alias check per table row"
    assert doctor.run_checks(checks, "local") == []


def test_display_url_keeps_ipv6_brackets(doctor):
    assert doctor._display_url("http://user:pass@[::1]:11434/v1") == "http://[::1]:11434/v1"
    assert doctor._display_url("http://user:pass@gpu:8000/v1") == "http://gpu:8000/v1"



def test_a_local_endpoint_is_checked_directly_despite_a_proxy(doctor, monkeypatch):
    import http.server
    import threading

    body = json.dumps({"object": "list", "data": [{"id": "qwen3:8b"}]}).encode()

    class Models(http.server.BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802
            self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *a):
            pass

    server = http.server.HTTPServer(("127.0.0.1", 0), Models)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        for key in ("HTTP_PROXY", "http_proxy"):
            monkeypatch.setenv(key, "http://127.0.0.1:1")
        for key in ("NO_PROXY", "no_proxy"):
            monkeypatch.delenv(key, raising=False)
        monkeypatch.setenv("UNITARES_MODEL_BASE_URL", f"http://127.0.0.1:{server.server_address[1]}/v1")
        monkeypatch.setenv("UNITARES_MODEL_ID", "qwen3:8b")
        result = doctor.check_model_endpoint(REPO_ROOT)
        assert result.status == doctor.Status.PASS, result.message
    finally:
        server.shutdown()
        server.server_close()
