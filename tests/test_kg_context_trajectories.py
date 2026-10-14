"""Offline trajectory controls: evidence, corrections, accounting and isolation."""
import json
import subprocess

import pytest

from scripts.eval import kg_context_trajectories as probe


def task(name):
    return next(t for t in probe.load_tasks() if t["id"] == name)


def request(tool="knowledge", **arguments):
    return {"tool": tool, "arguments": arguments}


def final(name):
    expected = task(name)["expected"]
    return {"answer": expected["answer"], "source_ids": [expected["source_id"]], "reason": "Checked the evidence."}


def builder_for(condition):
    if condition == "after":
        return probe.build_experience_envelope
    historical = subprocess.run(["git", "cat-file", "-e", f"{probe.BASELINE}:{probe.ENVELOPE}"],
                                cwd=probe.ROOT, capture_output=True)
    if historical.returncode:
        pytest.skip("historical formatter integration requires full repository history")
    return probe.builders()[condition]


@pytest.mark.asyncio
@pytest.mark.parametrize("condition", ["before", "after"])
async def test_replacement_and_imported_evidence_routes(condition):
    builder = builder_for(condition)
    for name in ("replacement", "imported_claim"):
        fixture = probe.Fixture(task(name), builder)
        found = await fixture.call(request("search_shared_memory", query="configuration"))
        row = found["memory_suggestions"][0]
        if name == "replacement":
            assert row["superseded_by"] == "r102"
        else:
            assert row["imported_memory"]["trust"] == "unreviewed"
        assert not fixture.score(final(name))["task_success"]
        await fixture.call(request(action="details", discovery_id=task(name)["expected"]["source_id"]))
        assert fixture.score(final(name))["task_success"]


@pytest.mark.asyncio
@pytest.mark.parametrize("condition", ["before", "after"])
async def test_latest_administrative_note_is_not_the_correction(condition):
    fixture = probe.Fixture(task("appended_correction"), builder_for(condition))
    found = await fixture.call(request("search_shared_memory", query="Atlas cache"))
    pointers = found["memory_suggestions"][0]["resolution_notes"]
    assert pointers["first_offset"] > 2000
    assert pointers["latest_offset"] > pointers["first_offset"]
    latest = await fixture.call(request(action="details", discovery_id="r201", offset=pointers["latest_offset"]))
    assert "cache_mode=disabled" not in latest["details"]
    assert not fixture.score(final("appended_correction"))["task_success"]
    await fixture.call(request(action="details", discovery_id="r201", offset=pointers["first_offset"]))
    assert fixture.score(final("appended_correction"))["task_success"]


@pytest.mark.asyncio
async def test_notice_must_be_read_before_exact_ack_and_old_answer_fails():
    fixture = probe.Fixture(task("mid_task_change"), probe.build_experience_envelope)
    first = await fixture.call(request(action="details", discovery_id="r401"))
    revision = first["knowledge_changes"]["notices"][0]["revision"]
    stale = {"answer": "8", "source_ids": ["r401"], "reason": "Old receipt"}
    assert not fixture.score(stale)["task_success"]
    refused = await fixture.call(request(action="notices", acknowledge=[revision]))
    assert refused["success"] is False
    assert not fixture.acknowledged
    current = await fixture.call(request(action="details", discovery_id="r401"))
    assert "retry_limit=5" in current["discovery"]["details"]
    wrong = await fixture.call(request(action="notices", acknowledge=["unseen-revision"]))
    assert wrong["success"] is False
    ack = await fixture.call(request(action="notices", acknowledge=[revision]))
    assert ack == {"success": True, "notices": []}
    assert fixture.score(final("mid_task_change"))["task_success"]
    assert fixture.premature_acks == 2


@pytest.mark.asyncio
async def test_guess_and_unread_or_invalid_citations_do_not_count():
    fixture = probe.Fixture(task("replacement"), probe.build_experience_envelope)
    assert not fixture.score(final("replacement"))["task_success"]
    await fixture.call(request(action="details", discovery_id="r102"))
    for ids in (["r999"], ["r101", "r102"], [], "r102"):
        assert not fixture.score({**final("replacement"), "source_ids": ids})["task_success"]


@pytest.mark.asyncio
async def test_malformed_read_arguments_are_errors_not_lost_episodes():
    fixture = probe.Fixture(task("replacement"), probe.build_experience_envelope)
    for read in (request("search_shared_memory", response_mode=[]),
                 request(action="details", discovery_id={}),
                 request(action="details", discovery_id="r102", offset=True)):
        assert (await fixture.call(read))["success"] is False


class ScriptedConversation:
    def __init__(self, replies, *, usage=True, provider_error=False):
        self.replies = iter(replies)
        self.usage, self.provider_error = usage, provider_error

    def ask(self, prompt, timeout):
        raw = {"response": json.dumps(next(self.replies))}
        if self.usage:
            raw["usage"] = {"input_tokens": 100, "output_tokens": 20}
        return {"raw": raw, "error": "provider unavailable" if self.provider_error else None,
                "prompt_bytes": len(prompt.encode()), "latency_ms": 1}


@pytest.mark.asyncio
async def test_episode_retains_errors_and_sums_all_turn_usage():
    replies = [request("forbidden-write", summary="x"),
               request(action="details", discovery_id="r102"), final("replacement")]
    result = await probe.episode(task("replacement"), probe.build_experience_envelope, ScriptedConversation(replies))
    assert result["task_success"]
    assert result["tool_errors"] == 1
    assert result["tool_calls"] == 2
    assert result["usage"] == {"input_tokens": 300, "output_tokens": 60}
    assert result["usage_complete"]
    assert len(result["turns"]) == 3


@pytest.mark.asyncio
async def test_missing_usage_and_provider_failure_are_not_zero_cost_success():
    result = await probe.episode(task("replacement"), probe.build_experience_envelope,
                                 ScriptedConversation([{}], usage=False, provider_error=True))
    assert result["termination"] == "provider_error"
    assert result["usage"] == {} and not result["usage_complete"]
    assert not result["task_success"]
    assert len(result["turns"]) == 1


@pytest.mark.asyncio
async def test_exhausted_turns_are_reported(monkeypatch):
    monkeypatch.setattr(probe, "MAX_TURNS", 2)
    result = await probe.episode(task("replacement"), probe.build_experience_envelope,
                                 ScriptedConversation([{}, {}]))
    assert result["termination"] == "turn_limit"
    assert result["protocol_errors"] == 2
    assert not result["task_success"]


def test_argon_conversation_reuses_only_its_own_context(monkeypatch, tmp_path):
    commands, envs = [], []
    monkeypatch.setattr(probe.shutil, "which", lambda _: "/fixture/agy")
    monkeypatch.setattr(probe.agy, "isolated_home", lambda *args: str(tmp_path / "isolated-home"))
    monkeypatch.setenv("EVAL_SECRET_MUST_NOT_PASS", "synthetic-secret")

    def fake_run(command, *, cwd, env, timeout_s):
        commands.append(command)
        envs.append(env)
        return 0, json.dumps({"status": "SUCCESS", "conversation_id": "fixture-cid",
                              "response": '{"answer":"x","source_ids":[],"reason":"x"}'}), ""

    monkeypatch.setattr(probe.agy, "_run", fake_run)
    with probe.ArgonConversation("selected-model") as conversation:
        assert conversation.ask("first", 3)["error"] is None
        assert conversation.ask("second", 3)["error"] is None
    assert "--conversation" not in commands[0]
    assert commands[1][-2:] == ["--conversation", "fixture-cid"]
    assert all("EVAL_SECRET_MUST_NOT_PASS" not in env for env in envs)
    assert all(env["HOME"] == str(tmp_path / "isolated-home") for env in envs)
    assert commands[0][2].startswith(probe.agy.PROMPT_GUARD)


@pytest.mark.asyncio
async def test_changed_frozen_inputs_refuse_execution(tmp_path, monkeypatch):
    monkeypatch.setattr(probe, "input_hashes", lambda: {"fixture": "changed"})
    with pytest.raises(ValueError, match="frozen inputs changed"):
        await probe.run({"inputs": {"fixture": "original"}}, tmp_path / "results")
    assert not (tmp_path / "results").exists()
