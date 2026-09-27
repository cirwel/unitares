"""A governance restart must not end the reviewer's continuation window.

Live instances: sessions 490c7cf515b89a6e (2026-09-13) and 85bd219ebb5b9ec9
(2026-09-24). Each time the reviewer filed its rejection, polled
``dialectic(get)`` for the paused agent's answer, and exited status 1 when
gov-mcp restarted under it. The paused agent answered 2-3 minutes later; no
one was left to read the answer, and the session waited out the 4h liveness
reap.

The mechanism, reproduced against a real mcp 2.x streamable-HTTP server that
was killed and restarted mid-poll: the transport's anyio task group cancels
the task that opened it. The poll sees ``CancelledError`` -- a BaseException,
so the loop's ``except Exception`` never runs -- and the client is dead for
good even once the server is back. These fakes model exactly that: a client
whose server went away raises ``CancelledError`` on every later call.
"""

import asyncio
import sys
import types

import pytest

import agents.dialectic_reviewer.reviewer as r
from agents.dialectic_reviewer.reviewer import Thesis
from src.dialectic_protocol import DialecticMessage, DialecticPhase, DialecticSession

REVIEWER = "reviewer-uuid"
PAUSED = "paused-uuid"


def _open_session(session_id: str) -> DialecticSession:
    session = DialecticSession(paused_agent_id=PAUSED)
    session.session_id = session_id
    assert session.submit_thesis(
        DialecticMessage(
            phase="thesis",
            agent_id=PAUSED,
            timestamp="2026-09-26T00:00:00+00:00",
            root_cause="claimed",
            proposed_conditions=["initial"],
            reasoning="initial claim",
        )
    )["success"] is True
    return session


def _paused_agent_answers(session: DialecticSession) -> None:
    result = session.submit_synthesis(
        DialecticMessage(
            phase="synthesis",
            agent_id=PAUSED,
            timestamp="2026-09-26T00:03:00+00:00",
            agrees=True,
            root_cause="verified",
            proposed_conditions=["ship the evidence"],
            reasoning="here is the missing evidence",
        )
    )
    assert result["blocked"] == "reviewer_objection_stands"


def _fake_sdk(monkeypatch, session, *, restart_after_gets: int, fail_first_filing=False,
              filing_error=None):
    """Install a fake GovernanceClient backed by ``session``.

    The server "restarts" on the Nth ``get``: that client is dead from then on.
    The paused agent answers during the outage, as it did live.
    """
    state = {
        "gets": 0,
        "instances": [],
        "identity_calls": [],
        "calls": [],
        "filing_failures_left": 1 if fail_first_filing else 0,
        "filing_error": filing_error or ConnectionError("connection reset while filing"),
        "filings": 0,
    }

    class FakeClient:
        def __init__(self, url):
            self.url = url
            self.agent_uuid = None
            self.client_session_id = None
            self.continuity_token = None
            self.dead = False
            self.disconnected = False
            state["instances"].append(self)

        async def connect(self):
            return None

        async def disconnect(self):
            self.disconnected = True

        async def onboard(self, **kw):
            self.agent_uuid = REVIEWER
            self.client_session_id = "agent-reviewer"
            self.continuity_token = "token-1"
            return None

        async def identity(self, **kw):
            state["identity_calls"].append(kw)
            self.agent_uuid = kw.get("agent_uuid")
            self.continuity_token = "token-2"
            return None

        async def checkin(self, response_text, complexity=0.3, confidence=0.7, **kw):
            return None

        async def call_tool(self, name, args, **kw):
            if self.dead:
                raise asyncio.CancelledError("Cancelled via cancel scope")
            state["calls"].append((name, dict(args)))
            action = args.get("action")
            if action == "antithesis":
                return session.submit_antithesis(
                    DialecticMessage(
                        phase="antithesis",
                        agent_id=REVIEWER,
                        timestamp="2026-09-26T00:01:00+00:00",
                        reasoning=args["reasoning"],
                    )
                )
            if action == "synthesis":
                state["filings"] += 1
                if args["agrees"] is True and state["filing_failures_left"]:
                    state["filing_failures_left"] -= 1
                    raise state["filing_error"]
                return session.submit_synthesis(
                    DialecticMessage(
                        phase="synthesis",
                        agent_id=REVIEWER,
                        timestamp="2026-09-26T00:04:00+00:00",
                        agrees=args["agrees"],
                        root_cause=args.get("root_cause"),
                        proposed_conditions=args.get("proposed_conditions"),
                        reasoning=args.get("reasoning"),
                    )
                )
            if action == "get":
                state["gets"] += 1
                if state["gets"] == restart_after_gets:
                    # gov-mcp restarts: this transport is gone for good, and
                    # the paused agent answers while no reviewer is connected.
                    self.dead = True
                    _paused_agent_answers(session)
                    raise asyncio.CancelledError("Cancelled via cancel scope")
                return {"success": True, **session.to_dict()}
            raise AssertionError(f"unexpected call: {name} {args}")

    fake_mod = types.ModuleType("unitares_sdk.client")
    fake_mod.GovernanceClient = FakeClient  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "unitares_sdk", types.ModuleType("unitares_sdk"))
    monkeypatch.setitem(sys.modules, "unitares_sdk.client", fake_mod)
    return state


def _model_replies(monkeypatch):
    calls = []
    outputs = iter(
        [
            '{"agrees": false, "root_cause": "shallow", '
            '"proposed_conditions": ["supply evidence"], "reasoning": "missing"}',
            '{"agrees": true, "root_cause": "verified", '
            '"proposed_conditions": ["ship the evidence"], "reasoning": "addressed"}',
            '{"agrees": true, "root_cause": "verified", '
            '"proposed_conditions": ["ship the evidence"], "reasoning": "addressed"}',
        ]
    )

    async def fake_obtain(prompt):
        calls.append(prompt)
        # The selected host answered; no fallback fired.
        r._record_reviewer_provenance(
            {"backend": "codex", "host_id": "codex:host-adapter", "models_used": [], "warnings": []}
        )
        return next(outputs)

    monkeypatch.setattr(r, "obtain_reviewer_text", fake_obtain)
    monkeypatch.setenv("UNITARES_DIALECTIC_CONTINUATION_WAIT_S", "2")
    monkeypatch.setenv("UNITARES_DIALECTIC_CONTINUATION_POLL_S", "0.01")
    return calls


@pytest.mark.asyncio
async def test_a_governance_restart_mid_poll_does_not_end_the_continuation(monkeypatch):
    _model_replies(monkeypatch)
    session = _open_session("sess-restart")
    state = _fake_sdk(monkeypatch, session, restart_after_gets=2)

    verdict = await r.run(
        Thesis(session_id="sess-restart", root_cause="claimed", proposed_conditions=["initial"]),
        governance_url="http://localhost:8767",
        parent_agent_id=PAUSED,
    )

    # The answer given during the outage was read and answered.
    assert verdict.agrees is True
    assert session.phase == DialecticPhase.RESOLVED
    assert [m.agent_id for m in session.transcript[-3:]] == [REVIEWER, PAUSED, REVIEWER]
    # The replacement connection proved it is the same reviewer process with
    # the same-process continuity token, never by minting a new identity.
    assert state["identity_calls"], "reconnected without rebinding"
    rebind = state["identity_calls"][-1]
    assert rebind["agent_uuid"] == REVIEWER
    assert rebind["resume"] is True
    assert rebind["continuity_token"] in {"token-1", "token-2"}
    assert all(not kw.get("force_new") for kw in state["identity_calls"])
    # Every connection that was opened was also closed.
    assert all(c.disconnected for c in state["instances"])


@pytest.mark.asyncio
async def test_a_dropped_filing_is_retried_from_session_state(monkeypatch):
    """A connection error while filing is not fatal and does not double-file.

    The loop is state-driven: if the write never landed, the paused response
    is still pending on the next read and is answered again; if it had
    landed, the reviewer's own synthesis now follows it and nothing is owed.
    """
    model_calls = _model_replies(monkeypatch)
    session = _open_session("sess-refile")
    state = _fake_sdk(monkeypatch, session, restart_after_gets=2, fail_first_filing=True)

    verdict = await r.run(
        Thesis(session_id="sess-refile", root_cause="claimed", proposed_conditions=["initial"]),
        governance_url="http://localhost:8767",
        parent_agent_id=PAUSED,
    )

    assert verdict.agrees is True
    reviewer_syntheses = [
        m for m in session.transcript if m.phase == "synthesis" and m.agent_id == REVIEWER
    ]
    assert [m.agrees for m in reviewer_syntheses] == [False, True]
    assert state["filing_failures_left"] == 0
    # Review round 1 on #2508: the formed verdict is re-filed as formed. A
    # second model call could reach a different verdict, and a dropped
    # connection must not be able to change the outcome.
    assert len(model_calls) == 2, "the continuation re-judged instead of re-filing"
    assert state["filings"] == 3  # initial rejection, cut-off approval, re-filed approval


@pytest.mark.asyncio
async def test_a_refused_filing_is_an_answer_not_an_outage(monkeypatch):
    """The SDK raises for success=false; retrying that would re-file on
    every poll until the deadline (review round 1 on #2508)."""

    class ToolRefused(Exception):
        pass

    model_calls = _model_replies(monkeypatch)
    session = _open_session("sess-refused")
    state = _fake_sdk(monkeypatch, session, restart_after_gets=2, fail_first_filing=True,
                      filing_error=ToolRefused("Tool dialectic failed: not authorized"))

    verdict = await r.run(
        Thesis(session_id="sess-refused", root_cause="claimed", proposed_conditions=["initial"]),
        governance_url="http://localhost:8767",
        parent_agent_id=PAUSED,
    )

    assert verdict.agrees is False  # the standing rejection is what stands
    assert state["filings"] == 2
    assert len(model_calls) == 2


@pytest.mark.asyncio
async def test_cancelling_the_reviewer_itself_still_cancels_it(monkeypatch):
    """Containing the transport's cancellation must not swallow a real one."""
    monkeypatch.setenv("UNITARES_DIALECTIC_CONTINUATION_WAIT_S", "30")
    monkeypatch.setenv("UNITARES_DIALECTIC_CONTINUATION_POLL_S", "0.01")
    session = _open_session("sess-cancel")
    _fake_sdk(monkeypatch, session, restart_after_gets=10**9)

    from unitares_sdk.client import GovernanceClient  # the fake

    link = r._GovernanceLink("http://localhost:8767", GovernanceClient)
    await link.run(lambda c: c.onboard())
    task = asyncio.create_task(
        r.continue_after_disagreement(
            link,
            Thesis(session_id="sess-cancel"),
            r.Verdict(agrees=False, root_cause="rc", proposed_conditions=["c"], reasoning="no"),
            paused_agent_id=PAUSED,
            reviewer_agent_id=REVIEWER,
        )
    )
    await asyncio.sleep(0.1)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    await link.close()


# --------------------------------------------------------------------------- #
# The same failure against a real mcp streamable-HTTP server, killed and
# restarted mid-review. The fakes above cannot model anyio's scope ownership,
# which is the actual mechanism: before the fix, the reviewer's own task was
# cancelled here, and a client merely closed in that task still cancelled it
# at its next await.
# --------------------------------------------------------------------------- #
_FAKE_GOVERNANCE = """
import json, sys
from mcp.server.mcpserver import MCPServer
server = MCPServer("fake-governance")

@server.tool()
def dialectic(action: str, session_id: str = "", client_session_id: str = "") -> str:
    return json.dumps({"success": True, "phase": "synthesis", "transcript": []})

server.run(transport="streamable-http", host="127.0.0.1", port=int(sys.argv[1]))
"""


def _free_port() -> int:
    import socket

    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.mark.chaos
@pytest.mark.asyncio
async def test_the_link_survives_a_real_server_restart(tmp_path):
    pytest.importorskip("mcp.server.mcpserver")
    client_mod = pytest.importorskip("unitares_sdk.client")
    import subprocess
    import time

    script = tmp_path / "fake_governance.py"
    script.write_text(_FAKE_GOVERNANCE)
    port = _free_port()
    url = f"http://127.0.0.1:{port}/mcp"

    def start():
        return subprocess.Popen(
            [sys.executable, str(script), str(port)],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )

    async def get_until_up(link, budget_s=20.0):
        deadline = time.monotonic() + budget_s
        while True:
            try:
                return await link.call_tool("dialectic", {"action": "get", "session_id": "s"})
            except Exception:  # noqa: BLE001 — server still starting
                if time.monotonic() > deadline:
                    raise
                await asyncio.sleep(0.2)

    server = start()
    link = r._GovernanceLink(
        url,
        lambda u: client_mod.GovernanceClient(u, timeout=5, retry_delay=0.1, connect_retries=0),
    )
    try:
        assert (await get_until_up(link))["phase"] == "synthesis"

        server.kill()
        await asyncio.to_thread(server.wait)
        with pytest.raises(Exception):  # an ordinary Exception, never a cancellation
            await link.call_tool("dialectic", {"action": "get", "session_id": "s"})

        server = start()
        assert (await get_until_up(link))["phase"] == "synthesis"
        # No stray cancellation is still pending against this task.
        await asyncio.sleep(0.3)
    finally:
        await link.close()
        server.kill()
        await asyncio.to_thread(server.wait)


def test_sdk_wrapped_transport_failures_are_retryable_and_refusals_are_not():
    """Review round 2 on #2508: the real client wraps a socket failure as
    GovernanceConnectionError, and a success=false answer as its subclass
    GovernanceToolRefused. Only the answer is final."""
    errors = pytest.importorskip("unitares_sdk.errors")

    assert r._is_transport_loss(errors.GovernanceConnectionError("All connection attempts failed"))
    assert r._is_transport_loss(errors.GovernanceTimeoutError("dialectic timed out after 30s"))
    assert r._is_transport_loss(errors.GovernanceUnavailableError("503", retry_after_seconds=1))
    assert r._is_transport_loss(r.GovernanceLinkLost("ended"))
    assert not r._is_transport_loss(errors.GovernanceToolRefused("Tool dialectic failed: no"))
    assert not r._is_transport_loss(ValueError("a bug, not an outage"))
