"""Offline trajectory controls: evidence, corrections, accounting and isolation."""
import json
from copy import deepcopy
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
                 request("forbidden-write", summary="x"),
                 request("search_shared_memory", query="   "),
                 request("search_shared_memory", query={}),
                 request(action="details", discovery_id={}),
                 request(action="details", discovery_id="r102", offset=True)):
        assert (await fixture.call(read))["success"] is False


class ScriptedConversation:
    def __init__(self, replies, *, usage=True, provider_error=False):
        self.replies = iter(replies)
        self.usage, self.provider_error = usage, provider_error
        self.calls = 0

    def ask(self, prompt, timeout):
        self.calls += 1
        raw = {"response": json.dumps(next(self.replies))}
        if self.usage:
            raw["usage"] = {"input_tokens": 100 * self.calls, "output_tokens": 20 * self.calls}
        return {"raw": raw, "error": "provider unavailable" if self.provider_error else None,
                "prompt_bytes": len(prompt.encode()), "latency_ms": 1}


@pytest.mark.asyncio
async def test_episode_retains_errors_and_cumulative_usage_without_double_counting():
    replies = [request("search_shared_memory", query="   "),
               request(action="details", discovery_id="r102"), final("replacement")]
    checkpoints = []
    result = await probe.episode(task("replacement"), probe.build_experience_envelope, ScriptedConversation(replies),
                                 lambda turns: checkpoints.append(deepcopy(turns)))
    assert result["task_success"]
    assert result["tool_errors"] == 1
    assert result["tool_calls"] == 2
    assert result["usage"] == {"input_tokens": 300, "output_tokens": 60}
    assert result["usage_complete"]
    assert result["peak_call_input_tokens"] == 100
    assert result["peak_context_tokens"] is None
    assert len(result["turns"]) == 3
    assert checkpoints[0][0]["pending"] is True
    assert any(c[-1].get("tool_response", {}).get("success") is False for c in checkpoints)


def test_cumulative_usage_uses_last_snapshot_and_keeps_cache_separate():
    turns = [{"raw": {"usage": {"input_tokens": 14000, "output_tokens": 300, "cache_read_tokens": 0}}},
             {"raw": {"usage": {"input_tokens": 17000, "output_tokens": 700, "cache_read_tokens": 12000}}}]
    metrics = probe.usage_metrics(turns)
    assert metrics["usage"] == turns[-1]["raw"]["usage"]
    assert metrics["peak_call_input_tokens"] == 14000
    assert metrics["usage_complete"]
    assert not metrics["usage_issues"]


def test_missing_or_decreasing_usage_stays_partial():
    good = {"raw": {"usage": {"input_tokens": 100, "output_tokens": 20}}}
    for bad in ({"raw": {}}, {"pending": True},
                {"raw": {"usage": {"input_tokens": 10, "output_tokens": 5}}},
                {"raw": {"usage": {"input_tokens": True, "output_tokens": 20}}}):
        metrics = probe.usage_metrics([good, bad])
        assert not metrics["usage_complete"]
        assert metrics["usage_issues"]
        assert metrics["peak_call_input_tokens"] is None
    assert probe.usage_metrics([good, {"raw": {}}])["usage"] == good["raw"]["usage"]
    assert not probe.usage_metrics([])["usage_complete"]


@pytest.mark.asyncio
async def test_offline_report_preserves_scores_and_original_receipts(tmp_path, monkeypatch):
    result = await probe.episode(task("replacement"), probe.build_experience_envelope,
        ScriptedConversation([request(action="details", discovery_id="r102"), final("replacement")]))
    schedule = {"task": "replacement", "condition": "after"}
    record = {**result, **schedule, "usage": {"input_tokens": 300, "output_tokens": 60}}
    plan = {"schedule": [schedule], "model_selected": "fixture"}
    probe.write_json(tmp_path / "plan.json", plan)
    probe.write_json(tmp_path / "episode-01.json", record)
    probe.write_json(tmp_path / "aggregate.json", {"completed_episode_receipts": 1, "transport_canary_passed": True})
    originals = {p.name: p.read_bytes() for p in tmp_path.iterdir()}
    monkeypatch.setattr(probe, "ArgonConversation", lambda *_: pytest.fail("offline report called model"))
    report = probe.report_existing(tmp_path)
    row = report["rows"][0]
    assert row["usage"] == {"input_tokens": 200, "output_tokens": 40}
    for key in ("task_success", "answer_correct", "evidence_read", "citations_valid", "tool_calls", "termination"):
        assert row[key] == record[key]
    assert {p.name: p.read_bytes() for p in tmp_path.iterdir()} == originals
    assert report["accounting_correction"]["source_receipt_sha256"]["episode-01.json"] == probe.digest(originals["episode-01.json"])


@pytest.mark.asyncio
async def test_split_evidence_joins_adjacent_pages_but_not_gaps_or_revisions():
    fixture = probe.Fixture(task("replacement"), probe.build_experience_envelope)
    body = fixture.docs["r102"].details
    split = body.index("batch_size=32") + 6
    await fixture.call(request(action="details", discovery_id="r102", offset=split + 1))
    await fixture.call(request(action="details", discovery_id="r102", length=split))
    assert not fixture.score(final("replacement"))["task_success"]
    await fixture.call(request(action="details", discovery_id="r102", offset=split, length=1))
    assert fixture.score(final("replacement"))["task_success"]

    separate = probe.Fixture(task("replacement"), probe.build_experience_envelope)
    await separate.call(request(action="details", discovery_id="r102", length=split))
    separate.docs["r102"].details = body + " A new revision."
    await separate.call(request(action="details", discovery_id="r102", offset=split))
    assert not separate.score(final("replacement"))["task_success"]
    await separate.call(request(action="details", discovery_id="r102", length=split))
    assert separate.score(final("replacement"))["task_success"]


@pytest.mark.asyncio
async def test_changed_details_can_be_acknowledged_after_multiple_pages():
    fixture = probe.Fixture(task("mid_task_change"), probe.build_experience_envelope)
    old = await fixture.call(request(action="details", discovery_id="r401"))
    revision = old["knowledge_changes"]["notices"][0]["revision"]
    await fixture.call(request(action="details", discovery_id="r401", length=50))
    assert not (await fixture.call(request(action="notices", acknowledge=[revision])))["success"]
    await fixture.call(request(action="details", discovery_id="r401", offset=50))
    assert (await fixture.call(request(action="notices", acknowledge=[revision])))["success"]


@pytest.mark.asyncio
async def test_complete_evidence_in_full_search_is_credited_and_triggers_notice():
    fixture = probe.Fixture(task("imported_claim"), probe.build_experience_envelope)
    await fixture.call(request("search_shared_memory", query="queue", response_mode="lean"))
    assert not fixture.score(final("imported_claim"))["task_success"]
    await fixture.call(request("search_shared_memory", query="queue", response_mode="full"))
    assert fixture.score(final("imported_claim"))["task_success"]
    changing = probe.Fixture(task("mid_task_change"), probe.build_experience_envelope)
    first = await changing.call(request("search_shared_memory", query="retry", response_mode="full"))
    assert first["knowledge_changes"]["notices"]
    assert not changing.score(final("mid_task_change"))["task_success"]
    await changing.call(request("search_shared_memory", query="retry", response_mode="full"))
    assert changing.score(final("mid_task_change"))["task_success"]


@pytest.mark.asyncio
async def test_unexpected_fixture_failure_retains_attempted_model_usage(monkeypatch):
    async def broken(*_):
        raise RuntimeError("fixture defect")

    monkeypatch.setattr(probe.Fixture, "call", broken)
    result = await probe.episode(task("replacement"), probe.build_experience_envelope,
                                 ScriptedConversation([request(action="details", discovery_id="r102")]))
    assert result["termination"] == "harness_error"
    assert result["tool_calls"] == 1
    assert result["usage"]["input_tokens"] == 100
    assert "fixture defect" in result["turns"][0]["harness_error"]
    assert not result["task_success"]


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


@pytest.mark.asyncio
async def test_failed_canary_prevents_scored_calls_and_retains_receipt(tmp_path, monkeypatch):
    monkeypatch.setattr(probe, "input_hashes", lambda: {})
    monkeypatch.setattr(probe, "transport_canary", lambda _: {"passed": False, "turns": [{"error": "bad resume"}]})
    plan = {"inputs": {}, "model_selected": "fixture", "schedule": [{"task": "replacement", "condition": "before"}],
            "limits": {"turns": probe.MAX_TURNS, "turn_seconds": probe.TURN_SECONDS,
                       "episode_seconds": probe.EPISODE_SECONDS}}
    output = tmp_path / "result"
    with pytest.raises(RuntimeError, match="no scored episodes"):
        await probe.run(plan, output)
    assert not list(output.glob("episode-*.json"))
    assert json.loads((output / "aggregate.json").read_text())["completed_episode_receipts"] == 0
    assert json.loads((output / "transport-canary.json").read_text())["passed"] is False
