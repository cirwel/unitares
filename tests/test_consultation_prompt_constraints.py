"""Prompt precedence and transport checks for the reproduced two-sentence failure.

These deterministic checks pin the evaluated prompt and its routing. Model
format-following evidence lives in the dated local probe, not a CI model call.
"""

import hashlib
import hmac
import json
from unittest.mock import AsyncMock

import pytest

from src.mcp_handlers.support import consultation as co
from src.mcp_handlers.support import host_adapter as ha
from src.mcp_handlers.support.inference_outcome import InferenceOutcome


ORIGINAL_BRIEF = (
    "In two sentences, explain why having 5 overlapping model-consultation tools "
    "(consult, call_model, delegate_inference, list_inference_hosts, "
    "describe_inference_host) plus request_review creates cognitive overhead for an agent."
)
CONSTRAINT_INSTRUCTION = (
    "Follow the request in the consultation brief, including its length, format, "
    "and scope constraints for the answer content. Use the consultation purpose "
    "within those constraints; do not add unrequested sections."
)
CRITIQUE_INSTRUCTION = (
    "Provide a critical perspective as advisory model evidence. Identify concrete "
    "risks, counterarguments, or improvements where relevant; do not claim a review verdict."
)


def test_reproduced_brief_uses_the_evaluated_prompt():
    request = co.ConsultRequest(brief=ORIGINAL_BRIEF, requester_uuid="test", purpose="critique")
    assert hashlib.sha256(ORIGINAL_BRIEF.encode()).hexdigest() == (
        "5d1915e64e7d3cd640e8ae509bb787c5dc417c01c3e328317a409fd9c40bac6f"
    )
    assert co._constructed_prompt(request) == (
        f"{CONSTRAINT_INSTRUCTION}\n\nConsultation purpose: {CRITIQUE_INSTRUCTION}"
        f"\n\nConsultation brief:\n{ORIGINAL_BRIEF}"
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("purpose", ["answer", "critique", "summarize", "generate"])
@pytest.mark.parametrize("effort", ["standard", "thorough"])
async def test_both_lanes_send_constraints_and_preserve_brief_and_audit_hash(
    monkeypatch, purpose, effort,
):
    # Literal JSON/newlines in caller content must survive the wrapper unchanged.
    brief = 'Return only {"risk": "..."}.\nLimit the string to five words.'
    request = co.ConsultRequest(
        brief=brief, requester_uuid="test", purpose=purpose, effort=effort,
        thorough_host_id="codex:host-adapter" if effort == "thorough" else None,
        privacy="local" if effort == "standard" else "cloud_allowed",
        response_mode="full",
    )
    # Stop after the real routing boundary; CI never contacts a model.
    failure = InferenceOutcome.failed(
        "test terminal failure", code="DELEGATED_INFERENCE_FAILED", category="system_error",
        execution_started=True,
    )
    standard = AsyncMock(return_value=failure)
    thorough = AsyncMock(return_value=failure)
    monkeypatch.setattr(co, "run_model_inference", standard)
    monkeypatch.setattr(co, "run_delegated_inference", thorough)
    outcome = await co.run_consultation(request)
    chosen, unused = (standard, thorough) if effort == "standard" else (thorough, standard)
    chosen.assert_awaited_once()
    unused.assert_not_awaited()
    prompt = chosen.await_args.args[0].prompt
    assert prompt.startswith(CONSTRAINT_INSTRUCTION + "\n\nConsultation purpose: ")
    assert prompt.endswith("\n\nConsultation brief:\n" + brief)
    assert prompt.count(brief) == 1
    key = "11" * 32
    record = co._consultation_record(request, outcome, key=key)
    assert record["hashes"]["constructed_prompt"] == (
        "hmac-sha256:" + hmac.new(bytes.fromhex(key), prompt.encode(), hashlib.sha256).hexdigest()
    )
    assert brief not in json.dumps(record)
    assert CONSTRAINT_INSTRUCTION not in json.dumps(record)


def test_answer_constraints_leave_terminal_envelope_strict():
    request = co.ConsultRequest(
        brief='Return only {"risk": "overlap"}.', requester_uuid="test", purpose="critique",
    )
    prompt = co._constructed_prompt(request)
    wrapped = ha._terminal_answer_prompt(prompt)
    assert wrapped.startswith(prompt + "\n\n--- UNITARES response contract ---\n")
    assert wrapped.endswith(ha._TERMINAL_ANSWER_CONTRACT)
    answer = '{"risk": "overlap"}'
    envelope = json.dumps({"schema": "unitares.terminal_answer.v1", "status": "complete", "answer": answer})
    text, metadata, error = ha._validate_terminal_answer(envelope)
    assert text == answer
    assert metadata["status"] == "complete"
    assert error is None
    # Content constraints do not permit bypassing the outer adapter contract.
    for invalid in (answer, f"```json\n{envelope}\n```", json.dumps({
        "schema": "unitares.terminal_answer.v1", "status": "incomplete", "reason": "unknown",
    })):
        text, metadata, error = ha._validate_terminal_answer(invalid)
        assert text == ""
        assert metadata["status"] == "malformed"
        assert error
