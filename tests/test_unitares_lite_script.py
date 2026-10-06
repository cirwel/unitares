"""unitares_lite.py under the strict identity default.

/v1/tools/call wraps the tool's payload as {"name", "result", "success"}, so
the client_session_id onboard returns is nested. Lite used to look for it at
the top level, never saved it, sent its next update with no caller proof, and
printed the success-shaped strict refusal as "Verdict: UNKNOWN".
"""
import importlib.util
import io
import json
from pathlib import Path

import pytest

_MODULE_PATH = Path(__file__).resolve().parents[1] / "scripts" / "analysis" / "unitares_lite.py"


@pytest.fixture
def lite(tmp_path, monkeypatch):
    spec = importlib.util.spec_from_file_location("unitares_lite", _MODULE_PATH)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    monkeypatch.setattr(mod, "SESSION_FILE", tmp_path / ".mcp_session")
    return mod


class _Server:
    """Stands in for urlopen: records each request and replays envelopes."""

    def __init__(self, *payloads):
        self.payloads = list(payloads)
        self.requests = []

    def __call__(self, req, timeout=None):
        self.requests.append(json.loads(req.data))
        body = {"name": self.requests[-1]["name"], "result": self.payloads.pop(0), "success": True}
        resp = io.BytesIO(json.dumps(body).encode())
        return resp


def _refusal(status="identity_required"):
    return {"success": True, "status": status, "refused": True,
            "hint": "Pass client_session_id.", "rollout_flag": "STRICT_IDENTITY_REQUIRED"}


def test_onboard_saves_the_nested_session_and_update_sends_it(lite, monkeypatch, capsys):
    server = _Server(
        {"success": True, "uuid": "11111111-2222-4333-8444-555555555555",
         "agent_id": "lite-test", "client_session_id": "agent-111111112222-tag"},
        {"success": True, "decision": {"action": "proceed"}, "metrics": {"E": 0.7}},
    )
    monkeypatch.setattr(lite.urllib.request, "urlopen", server)

    lite.onboard_cmd("lite-test")
    assert lite.load_session() == "agent-111111112222-tag"

    lite.update_cmd("did a thing")
    assert server.requests[1]["arguments"]["client_session_id"] == "agent-111111112222-tag"
    assert "Verdict: PROCEED" in capsys.readouterr().out


def test_a_refused_update_is_reported_as_an_error(lite, monkeypatch, capsys):
    monkeypatch.setattr(lite.urllib.request, "urlopen", _Server(_refusal()))

    result = lite.update_cmd("did a thing")

    out = capsys.readouterr().out
    assert result["success"] is False
    assert result["refused"] == "identity_required"
    assert "Verdict" not in out
    assert "identity refused (identity_required)" in out


def test_a_refused_onboard_is_reported_as_an_error(lite, monkeypatch, capsys):
    monkeypatch.setattr(lite.urllib.request, "urlopen", _Server(_refusal("lineage_declaration_required")))

    result = lite.onboard_cmd()

    assert result["success"] is False
    assert "Onboarded" not in capsys.readouterr().out
    assert lite.load_session() is None


@pytest.mark.parametrize("argv", [
    ["onboard"], ["update", "did a thing"], ["metrics"], ["status"],
])
def test_main_exits_nonzero_when_the_call_fails(lite, monkeypatch, capsys, argv):
    def boom(req, timeout=None):
        raise lite.urllib.error.URLError("refused")

    monkeypatch.setattr(lite.urllib.request, "urlopen", boom)
    monkeypatch.setattr(lite.sys, "argv", ["unitares_lite.py", *argv])

    with pytest.raises(SystemExit) as exc:
        lite.main()

    assert exc.value.code == 1
    assert "Error" in capsys.readouterr().out


def test_main_exits_nonzero_on_a_strict_identity_refusal(lite, monkeypatch):
    monkeypatch.setattr(lite.urllib.request, "urlopen", _Server(_refusal()))
    monkeypatch.setattr(lite.sys, "argv", ["unitares_lite.py", "update", "x"])

    with pytest.raises(SystemExit) as exc:
        lite.main()

    assert exc.value.code == 1


def test_main_returns_normally_on_success(lite, monkeypatch):
    monkeypatch.setattr(lite.urllib.request, "urlopen",
                        _Server({"success": True, "decision": {"action": "proceed"}, "metrics": {"E": 0.7}}))
    monkeypatch.setattr(lite.sys, "argv", ["unitares_lite.py", "update", "x"])

    lite.main()
