"""The resident lifecycle contract, and the scribe's pure formatting.

The lifecycle is the thing being generalised out of `agents/dialectic_reviewer`,
so what matters is the ORDER and the completeness of the sequence: onboard as a
fresh identity, do the job, land one real check-in, disconnect even when the job
raises. A resident that skips the check-in is indistinguishable from a leaked
identity; one that skips disconnect leaks a session.
"""

from __future__ import annotations

import asyncio

import pytest

from agents.local_resident import ResidentSpec, run_local_resident
from agents.triage_scribe.scribe import format_anomalies


class _FakeClient:
    def __init__(self, fail_job: bool = False):
        self.calls: list[tuple[str, dict]] = []
        self.connected = False
        self.disconnected = False

    async def connect(self):
        self.connected = True

    async def disconnect(self):
        self.disconnected = True

    async def onboard(self, **kwargs):
        self.calls.append(("onboard", kwargs))

    async def checkin(self, **kwargs):
        self.calls.append(("checkin", kwargs))


@pytest.fixture
def patched_client(monkeypatch):
    client = _FakeClient()

    class _Module:
        GovernanceClient = staticmethod(lambda url: client)

    import sys
    import types

    module = types.ModuleType("unitares_sdk.client")
    module.GovernanceClient = lambda url: client  # type: ignore[attr-defined]
    pkg = types.ModuleType("unitares_sdk")
    monkeypatch.setitem(sys.modules, "unitares_sdk", pkg)
    monkeypatch.setitem(sys.modules, "unitares_sdk.client", module)
    return client


def test_lifecycle_order_is_onboard_job_checkin(patched_client, monkeypatch):
    monkeypatch.setenv("UNITARES_GOVERNANCE_URL", "http://127.0.0.1:0/mcp/")
    seen: list[str] = []

    async def job(client):
        seen.append("job")
        return "did a thing"

    spec = ResidentSpec(name="TestResident", spawn_reason="resident_cycle")
    summary = asyncio.run(run_local_resident(spec, job))

    assert summary == "did a thing"
    assert [name for name, _ in patched_client.calls] == ["onboard", "checkin"]
    assert seen == ["job"], "the job must run between onboard and checkin"
    assert patched_client.connected and patched_client.disconnected


def test_onboards_fresh_and_never_resumes(patched_client, monkeypatch):
    """force_new is not optional. Co-location is not lineage."""
    monkeypatch.setenv("UNITARES_GOVERNANCE_URL", "http://127.0.0.1:0/mcp/")
    monkeypatch.delenv("UNITARES_PARENT_AGENT_ID", raising=False)

    async def job(client):
        return "ok"

    asyncio.run(
        run_local_resident(
            ResidentSpec(name="R", spawn_reason="resident_cycle"), job
        )
    )
    _, kwargs = patched_client.calls[0]
    assert kwargs["force_new"] is True
    assert kwargs["spawn_reason"] == "resident_cycle"
    # No parent in the environment means no parent claimed — not a guess.
    assert kwargs["parent_agent_id"] is None


def test_parent_is_declared_only_from_the_provisioned_env(patched_client, monkeypatch):
    monkeypatch.setenv("UNITARES_GOVERNANCE_URL", "http://127.0.0.1:0/mcp/")
    monkeypatch.setenv("UNITARES_PARENT_AGENT_ID", "11111111-2222-3333-4444-555555555555")

    async def job(client):
        return "ok"

    asyncio.run(
        run_local_resident(ResidentSpec(name="R", spawn_reason="explicit"), job)
    )
    _, kwargs = patched_client.calls[0]
    assert kwargs["parent_agent_id"] == "11111111-2222-3333-4444-555555555555"


def test_disconnect_happens_even_when_the_job_raises(patched_client, monkeypatch):
    """A crashing resident must not also leak its session."""
    monkeypatch.setenv("UNITARES_GOVERNANCE_URL", "http://127.0.0.1:0/mcp/")

    async def job(client):
        raise RuntimeError("job exploded")

    with pytest.raises(RuntimeError, match="job exploded"):
        asyncio.run(
            run_local_resident(ResidentSpec(name="R", spawn_reason="resident_cycle"), job)
        )

    assert patched_client.disconnected
    # And it must NOT have checked in — reporting success it did not have is
    # worse than reporting nothing.
    assert [name for name, _ in patched_client.calls] == ["onboard"]


# ---------------------------------------------------------------------------
# Scribe formatting — pure, so the interesting behaviour is testable with no
# model and no network (same discipline as the reviewer's verdict parsing).
# ---------------------------------------------------------------------------


def test_anomaly_formatting_uses_the_fields_observe_actually_returns():
    """The payload has type/severity/description — not `reasons`.

    The first version looked for `reasons` / `anomaly_types` / `reason`, found
    none, and rendered every row as "flagged, no reason given". A local model
    reading that concluded the fleet's flags were uninformative, which was true
    of the prompt and false of the tool. Verified against the live server:
    keys are agent_id, agent_name, context, description, severity, stale,
    timestamp, type.
    """
    payload = {"anomalies": [{
        "agent_name": "codex-cirwel#7168c86d",
        "type": "risk_spike",
        "severity": "high",
        "description": "Risk increased from 0.49 to 0.81 (0.32 change)",
        "stale": False,
    }]}
    text = format_anomalies(payload)
    assert "codex-cirwel#7168c86d" in text
    assert "risk_spike" in text
    assert "high" in text
    assert "Risk increased from 0.49 to 0.81" in text
    assert "no detail" not in text


def test_stale_is_carried_because_it_changes_the_response():
    """A stale anomaly means the agent stopped reporting, not that it recovered."""
    text = format_anomalies({"anomalies": [{
        "agent_name": "ag-1", "type": "risk_spike", "stale": True,
    }]})
    assert "[stale]" in text


def test_genuinely_detail_free_row_says_so_without_claiming_a_reason_exists():
    text = format_anomalies({"anomalies": [{"agent_id": "ag-3"}]})
    assert "no detail in payload" in text
    assert "None" not in text


def test_empty_anomalies_state_the_absence_explicitly():
    """An empty list must not render as blank — a model handed nothing will
    invent something to say."""
    assert "no anomalous agents" in format_anomalies({"anomalies": []})


def test_formatting_is_bounded():
    """A resident prompt must not grow without limit with the fleet."""
    payload = {"anomalies": [{"agent_id": f"ag-{i}"} for i in range(200)]}
    assert len(format_anomalies(payload).splitlines()) <= 15


# ---------------------------------------------------------------------------
# Thinking-model text extraction — the defect the first live run exposed.
# ---------------------------------------------------------------------------


class _Msg:
    def __init__(self, content=None, reasoning=None):
        self.content = content
        if reasoning is not None:
            self.reasoning = reasoning


def test_plain_content_passes_through():
    from agents.local_resident import extract_model_text

    assert extract_model_text(_Msg(content="the answer")) == "the answer"


def test_think_block_is_stripped():
    from agents.local_resident import extract_model_text

    msg = _Msg(content="<think>weighing options</think>\n\nthe answer")
    assert extract_model_text(msg) == "the answer"


def test_reasoning_is_surfaced_when_content_is_empty():
    """The live failure: gemma4 spent its whole budget thinking and `content`
    came back empty, so the resident reported "model returned nothing" about a
    model that had produced several hundred words."""
    from agents.local_resident import extract_model_text

    text = extract_model_text(_Msg(content="", reasoning="half a thought"))
    assert "half a thought" in text
    # Flagged rather than laundered — a truncated thought is weaker evidence
    # than an answer and the caller storing it should be able to tell.
    assert "token limit" in text


def test_genuinely_empty_stays_empty():
    """Silence must remain reportable as silence."""
    from agents.local_resident import extract_model_text

    assert extract_model_text(_Msg(content="", reasoning="")) == ""
    assert extract_model_text(_Msg(content=None)) == ""


def test_content_wins_over_reasoning():
    from agents.local_resident import extract_model_text

    msg = _Msg(content="final answer", reasoning="scratch work")
    assert extract_model_text(msg) == "final answer"
