"""Operator-configured OpenAI-compatible reviewer backend.

Why this shape and not a ``gemini`` provider branch: #66/#80 removed exactly
such a branch from ``call_model`` because nothing ever wired its key, so it
could only return MISSING_CONFIG. Here the vendor is configuration, so the same
path is exercisable against a local endpoint with no key at all.

The properties under test are the independence-critical ones:
  - an unconfigured external host degrades to the local model with a warning,
    never harder than the pre-existing path;
  - a disagreeing external verdict survives to ``agrees=False``;
  - provenance names the model the PROVIDER reported, not the one requested;
  - neither the API key nor the endpoint path can leak into provenance.
"""

from __future__ import annotations

import asyncio
import json
import sys
from types import ModuleType, SimpleNamespace
from unittest.mock import patch

from agents.dialectic_reviewer import host_backends as hb
from agents.dialectic_reviewer import reviewer as r

CONFIGURED = {
    "UNITARES_DIALECTIC_REVIEWER_HOST": "external",
    "UNITARES_DIALECTIC_EXTERNAL_BASE_URL": "https://generativelanguage.googleapis.com/v1beta/openai/",
    "UNITARES_DIALECTIC_EXTERNAL_MODEL": "some-model-id",
    "UNITARES_DIALECTIC_EXTERNAL_API_KEY_ENV": "TEST_REVIEWER_KEY",
    "TEST_REVIEWER_KEY": "sk-secret-value-must-never-surface",
}


def _response(content: str, *, model: str = "some-model-id-002", tokens: int = 321):
    """A minimal duck-typed OpenAI chat.completions response."""
    return SimpleNamespace(
        model=model,
        usage=SimpleNamespace(total_tokens=tokens),
        choices=[
            SimpleNamespace(
                finish_reason="stop",
                message=SimpleNamespace(content=content),
            )
        ],
    )


class _FakeClient:
    """Stands in for AsyncOpenAI; records the call and returns a canned reply."""

    def __init__(self, response=None, raises: Exception | None = None):
        self._response = response
        self._raises = raises
        self.seen: dict = {}

        async def _create(**kwargs):
            self.seen = kwargs
            if self._raises is not None:
                raise self._raises
            return self._response

        self.chat = SimpleNamespace(completions=SimpleNamespace(create=_create))


def _patch_openai(client: _FakeClient):
    """Stub the module the backend imports lazily inside the function body.

    ``openai`` is a runner-only optional dependency and is NOT installed in CI,
    so this injects a stub into ``sys.modules`` rather than patching the real
    package — importing it here would make these tests require a dependency the
    code under test deliberately imports lazily.
    """
    stub = ModuleType("openai")
    stub.AsyncOpenAI = lambda **kwargs: client  # type: ignore[attr-defined]
    return patch.dict(sys.modules, {"openai": stub})


# --------------------------------------------------------------------------- #
# Configuration handling
# --------------------------------------------------------------------------- #
def test_unconfigured_host_reports_which_flags_are_missing():
    with patch.dict("os.environ", {}, clear=True):
        result = asyncio.run(hb.call_openai_compat_backend("p"))
    assert result.text is None
    assert result.backend == "external"
    assert "UNITARES_DIALECTIC_EXTERNAL_BASE_URL" in (result.error or "")
    assert "UNITARES_DIALECTIC_EXTERNAL_MODEL" in (result.error or "")


def test_missing_model_alone_is_named():
    env = {"UNITARES_DIALECTIC_EXTERNAL_BASE_URL": "http://localhost:11434/v1"}
    with patch.dict("os.environ", env, clear=True):
        result = asyncio.run(hb.call_openai_compat_backend("p"))
    assert result.text is None
    assert "UNITARES_DIALECTIC_EXTERNAL_MODEL" in (result.error or "")
    # No invented default model may be requested on the operator's behalf.
    assert result.model_requested is None


def test_unconfigured_external_host_falls_back_to_local_with_warning():
    async def fake_local(prompt, model=r.DEFAULT_MODEL):
        return '{"agrees": true, "reasoning": "local"}'

    env = {"UNITARES_DIALECTIC_REVIEWER_HOST": "external"}
    with patch.dict("os.environ", env, clear=True):
        with patch.object(r, "call_reviewer_model", side_effect=fake_local):
            text = asyncio.run(r.obtain_reviewer_text("p"))

    assert json.loads(text)["reasoning"] == "local"
    provenance = r.reviewer_backend_provenance()
    assert provenance["backend"] == "ollama"
    assert any("not configured" in w for w in provenance["warnings"])


def test_gemini_alias_selects_the_external_path_not_a_vendor_branch():
    async def fake_external(prompt):
        return hb.HostReviewResult(
            text='{"agrees": false, "reasoning": "aliased"}',
            host_id="external:generativelanguage.googleapis.com",
            model_used="gemini-x",
            models_used=["gemini-x"],
            backend="external",
        )

    with patch.dict("os.environ", {"UNITARES_DIALECTIC_REVIEWER_HOST": "gemini"}, clear=True):
        with patch.object(r, "call_external_reviewer", side_effect=fake_external) as ext:
            with patch.object(r, "call_reviewer_model") as local:
                text = asyncio.run(r.obtain_reviewer_text("p"))

    assert ext.called
    assert not local.called
    assert json.loads(text)["reasoning"] == "aliased"


# --------------------------------------------------------------------------- #
# Verdict + provenance
# --------------------------------------------------------------------------- #
def test_disagreement_survives_to_the_verdict():
    client = _FakeClient(
        _response(
            'Here is my review:\n'
            '{"agrees": false, "root_cause": "unbounded retry",'
            ' "proposed_conditions": ["cap the retry budget"],'
            ' "reasoning": "the loop re-enters on the same error class"}'
        )
    )
    with patch.dict("os.environ", CONFIGURED, clear=True):
        with _patch_openai(client):
            result = asyncio.run(hb.call_openai_compat_backend("p"))

    assert result.text is not None
    verdict = r.parse_reviewer_verdict(result.text)
    assert verdict.agrees is False
    assert verdict.degraded is False
    assert verdict.proposed_conditions == ["cap the retry budget"]


def test_provenance_reports_provider_model_not_requested_model():
    client = _FakeClient(_response('{"agrees": true, "reasoning": "ok"}', model="served-002"))
    with patch.dict("os.environ", CONFIGURED, clear=True):
        with _patch_openai(client):
            result = asyncio.run(hb.call_openai_compat_backend("p"))

    provenance = result.provenance()
    assert provenance["backend"] == "external"
    assert provenance["model_requested"] == "some-model-id"
    assert provenance["model_used"] == "served-002"
    assert provenance["models_used"] == ["served-002"]
    assert provenance["tokens_used"] == 321
    assert provenance["finish_reason"] == "stop"


def test_provenance_never_carries_the_key_or_endpoint_path():
    client = _FakeClient(_response('{"agrees": true, "reasoning": "ok"}'))
    with patch.dict("os.environ", CONFIGURED, clear=True):
        with _patch_openai(client):
            result = asyncio.run(hb.call_openai_compat_backend("p"))

    blob = json.dumps(result.provenance())
    assert "sk-secret-value-must-never-surface" not in blob
    assert "/v1beta/openai/" not in blob
    # The host is retained — it is the accountability anchor, and is not secret.
    assert result.host_id == "external:generativelanguage.googleapis.com"


def test_upstream_error_message_is_not_propagated_verbatim():
    client = _FakeClient(raises=RuntimeError("401 for key sk-secret-value-must-never-surface"))
    with patch.dict("os.environ", CONFIGURED, clear=True):
        with _patch_openai(client):
            result = asyncio.run(hb.call_openai_compat_backend("p"))

    assert result.text is None
    assert "sk-secret" not in (result.error or "")
    assert "RuntimeError" in (result.error or "")


def test_unparseable_reply_reports_no_verdict_and_keeps_provenance():
    client = _FakeClient(_response("I would rather not answer in JSON."))
    with patch.dict("os.environ", CONFIGURED, clear=True):
        with _patch_openai(client):
            result = asyncio.run(hb.call_openai_compat_backend("p"))

    assert result.text is None
    assert "no parseable dialectic verdict" in (result.error or "")
    assert result.models_used == ["some-model-id-002"]


def test_thinking_block_is_stripped_before_verdict_extraction():
    """Regression from a live run: gemma4 wrapped its reasoning in <think>, and
    the JSON inside that block would otherwise be mistaken for the verdict."""
    client = _FakeClient(
        _response(
            "<think>Maybe I should answer "
            '{"agrees": true, "reasoning": "draft I rejected"}</think>\n'
            '{"agrees": false, "reasoning": "final"}'
        )
    )
    with patch.dict("os.environ", CONFIGURED, clear=True):
        with _patch_openai(client):
            result = asyncio.run(hb.call_openai_compat_backend("p"))

    assert result.text is not None
    assert json.loads(result.text)["reasoning"] == "final"


def test_truncation_is_reported_as_a_budget_problem():
    """Also from the live run: a thinking model can burn the whole budget before
    emitting JSON. That is operator-fixable, so it must not read as a refusal."""
    client = _FakeClient(_response("<think>still reasoning", tokens=700))
    client._response.choices[0].finish_reason = "length"

    with patch.dict("os.environ", CONFIGURED, clear=True):
        with _patch_openai(client):
            result = asyncio.run(hb.call_openai_compat_backend("p"))

    assert result.text is None
    assert "UNITARES_DIALECTIC_REVIEW_MAX_TOKENS" in (result.error or "")
    assert result.finish_reason == "length"


def test_missing_openai_dependency_degrades_instead_of_raising():
    """``openai`` is a runner-only optional dependency — CI does not install it.
    Its absence must look like any other backend failure, not an exception."""
    with patch.dict("os.environ", CONFIGURED, clear=True):
        with patch.dict(sys.modules, {"openai": None}):
            result = asyncio.run(hb.call_openai_compat_backend("p"))

    assert result.text is None
    assert result.backend == "external"
    assert "call failed" in (result.error or "")


def test_default_env_still_routes_to_local_model():
    """The pre-existing default path must remain untouched by this addition."""

    async def fake_local(prompt, model=r.DEFAULT_MODEL):
        return '{"agrees": true, "reasoning": "local"}'

    with patch.dict("os.environ", {}, clear=True):
        with patch.object(r, "call_reviewer_model", side_effect=fake_local) as local:
            with patch.object(r, "call_external_reviewer") as ext:
                text = asyncio.run(r.obtain_reviewer_text("p"))

    assert local.called
    assert not ext.called
    assert json.loads(text)["reasoning"] == "local"
