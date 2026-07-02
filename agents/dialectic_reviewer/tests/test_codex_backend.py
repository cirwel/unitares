"""Codex reviewer backend (#dialectic-rework Option B, 2026-07-02).

The independence-critical properties under test:
  - the default path (env unset) is byte-identical to the local-model path;
  - a failed Codex call falls back to the local model, never harder than today;
  - the final verdict is extracted from a real-shaped codex transcript, which
    echoes the (deliberately unparseable) prompt template before the answer.
"""

from __future__ import annotations

import asyncio
import json
from unittest.mock import patch

from agents.dialectic_reviewer import reviewer as r

# Shaped like a real `codex exec` transcript: banner, echoed prompt (whose JSON
# template is NOT valid JSON — `true | false`), an exec trace containing a
# small valid-but-not-verdict JSON, then the final verdict, then a footer.
TRANSCRIPT = """OpenAI Codex v0.142.5
--------
workdir: /tmp/x
model: gpt-5.5
--------
user
Respond with STRICT JSON only, no prose outside it:
{
  "agrees": true | false,
  "root_cause": "your assessment",
  "reasoning": "why"
}
exec
/bin/zsh -lc "cat config.json" succeeded in 0ms:
{"name": "not-a-verdict", "nested": {"k": [1, 2]}}
codex
{"agrees": false, "root_cause": "circular auth", "proposed_conditions": ["make enrollment atomic with onboard"], "reasoning": "the enrollment endpoint is authenticated by the captured credential"}
tokens used
16,585
"""


def test_extracts_final_verdict_from_transcript():
    out = r.extract_last_json_object(TRANSCRIPT)
    assert out is not None
    obj = json.loads(out)
    assert obj["agrees"] is False
    assert obj["root_cause"] == "circular auth"
    # And it round-trips through the existing parser to a non-degraded verdict.
    verdict = r.parse_reviewer_verdict(out)
    assert verdict.agrees is False
    assert verdict.degraded is False
    assert verdict.proposed_conditions == ["make enrollment atomic with onboard"]


def test_extract_ignores_non_verdict_objects_and_bad_json():
    assert r.extract_last_json_object("no json here") is None
    assert r.extract_last_json_object('{"name": "no agrees key"}') is None
    # Last verdict-shaped object wins.
    two = '{"agrees": true, "reasoning": "first"} then {"agrees": false, "reasoning": "second"}'
    assert json.loads(r.extract_last_json_object(two))["reasoning"] == "second"


def test_default_env_routes_to_local_model():
    async def fake_local(prompt, model=r.DEFAULT_MODEL):
        return '{"agrees": true, "reasoning": "local"}'

    with patch.dict("os.environ", {"UNITARES_DIALECTIC_REVIEWER_HOST": ""}):
        with patch.object(r, "call_reviewer_model", side_effect=fake_local) as local:
            with patch.object(r, "call_codex_reviewer") as codex:
                text = asyncio.run(r.obtain_reviewer_text("p"))
    assert json.loads(text)["reasoning"] == "local"
    assert local.called
    assert not codex.called


def test_codex_host_uses_codex_verdict():
    async def fake_codex(prompt):
        return '{"agrees": false, "reasoning": "codex"}'

    with patch.dict("os.environ", {"UNITARES_DIALECTIC_REVIEWER_HOST": "codex"}):
        with patch.object(r, "call_codex_reviewer", side_effect=fake_codex):
            with patch.object(r, "call_reviewer_model") as local:
                text = asyncio.run(r.obtain_reviewer_text("p"))
    assert json.loads(text)["reasoning"] == "codex"
    assert not local.called


def test_codex_failure_falls_back_to_local_model():
    async def fake_codex(prompt):
        return None

    async def fake_local(prompt, model=r.DEFAULT_MODEL):
        return '{"agrees": false, "reasoning": "fallback"}'

    with patch.dict("os.environ", {"UNITARES_DIALECTIC_REVIEWER_HOST": "codex"}):
        with patch.object(r, "call_codex_reviewer", side_effect=fake_codex):
            with patch.object(r, "call_reviewer_model", side_effect=fake_local):
                text = asyncio.run(r.obtain_reviewer_text("p"))
    assert json.loads(text)["reasoning"] == "fallback"


def test_codex_absent_cli_returns_none_without_spawning():
    with patch("shutil.which", return_value=None):
        assert asyncio.run(r.call_codex_reviewer("p")) is None
