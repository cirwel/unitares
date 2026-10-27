"""The doctor's ``orchestrator_profiles`` check (inference-hosts-config-v0 step 4b).

The orchestrator's /v1/health is stubbed; the freshness comparison runs the
real generator (scripts/ops/orchestrator-profiles.py) against files in a
temporary directory, so a stale file is caught the way the doctor catches it.
"""

from __future__ import annotations

import hashlib
import importlib.util
import subprocess
import sys
import urllib.error
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent


def _load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


doctor = _load("unitares_doctor_profiles_test", REPO_ROOT / "scripts" / "dev" / "unitares_doctor.py")
gen = _load("orchestrator_profiles_doctor_test", REPO_ROOT / "scripts" / "ops" / "orchestrator-profiles.py")

Status = doctor.Status


@pytest.fixture(autouse=True)
def _bearer(monkeypatch):
    monkeypatch.setenv("AGENT_ORCHESTRATOR_BEARER_TOKEN", "tok")
    monkeypatch.delenv("AGENT_ORCHESTRATOR_URL", raising=False)
    monkeypatch.delenv("UNITARES_SECRETS_ENV", raising=False)


def _generate(tmp_path) -> Path:
    cli = tmp_path / "bin" / "claude"
    cli.parent.mkdir(exist_ok=True)
    cli.write_text("#!/bin/sh\n")
    cli.chmod(0o700)
    (tmp_path / "server.env").write_text(f"UNITARES_CLAUDE_CLI={cli}\n")
    out = tmp_path / "state" / "profiles.json"
    assert gen.main(["--out", str(out), "--repo-root", str(REPO_ROOT), "--python", sys.executable,
                     "--env-file", str(tmp_path / "server.env")]) == 0
    return out


def _health(path: Path | None, *, digest: str | None = None, **overrides) -> dict:
    profiles = {
        "state": "ok",
        "raw_spawn": "deny",
        "file": str(path) if path else None,
        "digest": digest or (hashlib.sha256(path.read_bytes()).hexdigest() if path else None),
        "profiles": ["host.claude"],
        "unconfigured": [],
        "error": None,
    }
    profiles.update(overrides)
    return {"ok": True, "profiles": profiles}


def _check(health, *, run=doctor._run_replay, sleeps=None, later=None):
    """``later``: what health reports on a second ask (after the reload wait)."""
    answers = [health] if later is None else [health, later]

    def fetch(url, token):
        assert token == "tok"
        answer = answers.pop(0) if len(answers) > 1 else answers[0]
        if isinstance(answer, BaseException):
            raise answer
        return answer

    return doctor.check_orchestrator_profiles(
        fetch=fetch, run=run, sleep=(sleeps.append if sleeps is not None else lambda _s: None))


def test_current_profiles_pass(tmp_path):
    out = _generate(tmp_path)
    result = _check(_health(out))
    assert result.status is Status.PASS, result.message
    assert "loaded and current" in result.message


def test_an_input_that_changed_makes_the_loaded_profiles_stale(tmp_path):
    out = _generate(tmp_path)
    moved = tmp_path / "bin" / "claude-moved"
    moved.write_text("#!/bin/sh\n")
    moved.chmod(0o700)
    (tmp_path / "server.env").write_text(f"UNITARES_CLAUDE_CLI={moved}\n")

    result = _check(_health(out))
    assert result.status is Status.FAIL
    assert "differ from a fresh generation" in result.message
    assert "--replay" in result.message


def test_a_file_the_orchestrator_has_not_loaded_fails_after_one_wait(tmp_path):
    out = _generate(tmp_path)
    sleeps: list[float] = []
    result = _check(_health(out, digest="0" * 64), sleeps=sleeps)
    assert result.status is Status.FAIL
    assert "is not what the orchestrator loaded" in result.message
    assert sleeps == [doctor.PROFILES_RELOAD_WAIT_S]


def test_a_file_written_just_now_passes_once_the_orchestrator_reloads_it(tmp_path):
    """The second reading asks the orchestrator again: comparing the new file
    with the digest read before the wait would fail every fresh write."""
    out = _generate(tmp_path)
    sleeps: list[float] = []
    result = _check(_health(out, digest="0" * 64), sleeps=sleeps, later=_health(out))
    assert result.status is Status.PASS, result.message
    assert sleeps == [doctor.PROFILES_RELOAD_WAIT_S]


def test_a_broken_replacement_names_the_orchestrators_error(tmp_path):
    out = _generate(tmp_path)
    loaded = hashlib.sha256(out.read_bytes()).hexdigest()
    out.write_text("{")
    later = _health(out, digest=loaded, state="stale", error="invalid JSON")
    result = _check(_health(out, digest=loaded), later=later)
    assert result.status is Status.FAIL
    assert "is not what the orchestrator loaded" in result.message
    assert "invalid JSON" in result.message


def test_an_error_the_next_reload_clears_is_not_reported(tmp_path):
    """The good file was put back: the orchestrator clears its error on the
    next reload, so the check reads it again before judging."""
    out = _generate(tmp_path)
    sleeps: list[float] = []
    result = _check(_health(out, state="stale", error="invalid JSON"), sleeps=sleeps,
                    later=_health(out))
    assert result.status is Status.PASS, result.message
    assert sleeps == [doctor.PROFILES_RELOAD_WAIT_S]


def test_a_settled_reading_does_not_wait(tmp_path):
    out = _generate(tmp_path)
    sleeps: list[float] = []
    assert _check(_health(out), sleeps=sleeps).status is Status.PASS
    assert sleeps == []


def test_a_file_without_recorded_inputs_is_a_warning(tmp_path):
    out = _generate(tmp_path)
    Path(f"{out}{doctor.PROFILES_INPUTS_SUFFIX}").unlink()
    result = _check(_health(out))
    assert result.status is Status.WARN
    assert "cannot be checked" in result.message


def test_a_record_naming_another_file_does_not_vouch_for_this_one(tmp_path):
    """A copied file and record: the replay would check the original, so a
    stale copy could pass. The record must name the loaded file."""
    original = _generate(tmp_path)
    copy_dir = tmp_path / "elsewhere"
    copy_dir.mkdir(mode=0o700)
    copy = copy_dir / "profiles.json"
    copy.write_bytes(original.read_bytes())
    Path(f"{copy}{doctor.PROFILES_INPUTS_SUFFIX}").write_bytes(
        Path(f"{original}{doctor.PROFILES_INPUTS_SUFFIX}").read_bytes())

    result = _check(_health(copy))
    assert result.status is Status.WARN
    assert "not the loaded file" in result.message


def test_a_generator_error_is_a_warning_not_stale(tmp_path):
    out = _generate(tmp_path)

    def broken(argv):
        return subprocess.CompletedProcess(argv, 1, stdout="", stderr="cannot read env file x\n")

    result = _check(_health(out), run=broken)
    assert result.status is Status.WARN
    assert "could not check" in result.message


def test_a_path_that_says_is_current_does_not_pass_a_stale_file(tmp_path):
    """The verdict is the exit status and the line's end, not a substring a
    directory name can supply."""
    out = _generate(tmp_path)

    def stale(argv):
        return subprocess.CompletedProcess(
            argv, 1, stdout="orchestrator-profiles: /op/is current/profiles.json is stale\n",
            stderr="")

    result = _check(_health(out), run=stale)
    assert result.status is Status.FAIL
    assert "differ from a fresh generation" in result.message


def test_the_doctor_and_the_generator_agree_on_the_inputs_suffix():
    assert doctor.PROFILES_INPUTS_SUFFIX == gen.INPUTS_SUFFIX


@pytest.mark.parametrize("overrides, status, words", [
    ({"state": "invalid", "error": "cannot stat"}, Status.FAIL, "every profile spawn is refused"),
    ({"state": "stale", "error": "bad json"}, Status.FAIL, "last good set stays in force"),
    ({"raw_spawn": "allow"}, Status.WARN, "RAW_SPAWN=allow"),
    ({"unconfigured": ["dialectic.reviewer"]}, Status.WARN, "dialectic.reviewer"),
    ({"file": None}, Status.WARN, "does not report its profiles file"),
    ({"state": "stale", "generation": {"managed": True, "enforced": True, "running": False,
                                        "error": "the generator exited 1: boom"}},
     Status.FAIL, "refuses profile spawns: the generator exited 1"),
])
def test_health_states(tmp_path, overrides, status, words):
    out = _generate(tmp_path)
    result = _check(_health(out, **overrides))
    assert result.status is status
    assert words in result.message


def test_no_profiles_file_skips():
    result = _check({"ok": True, "profiles": {"state": "off", "raw_spawn": "allow"}})
    assert result.status is Status.SKIP


def test_an_unreachable_orchestrator_skips_and_a_refused_bearer_warns():
    assert _check(urllib.error.URLError("refused")).status is Status.SKIP
    refused = urllib.error.HTTPError("u", 401, "unauthorized", {}, None)
    assert _check(refused).status is Status.WARN


def test_without_a_bearer_it_skips(monkeypatch):
    monkeypatch.delenv("AGENT_ORCHESTRATOR_BEARER_TOKEN")
    result = doctor.check_orchestrator_profiles(fetch=lambda *_: pytest.fail("fetched"))
    assert result.status is Status.SKIP


def test_the_bearer_is_read_from_the_selected_secrets_file(tmp_path, monkeypatch):
    monkeypatch.delenv("AGENT_ORCHESTRATOR_BEARER_TOKEN")
    secrets = tmp_path / "secrets.env"
    secrets.write_text(
        "AGENT_ORCHESTRATOR_BEARER_TOKEN=old\n"
        "# AGENT_ORCHESTRATOR_BEARER_TOKEN=commented\n"
        "export AGENT_ORCHESTRATOR_BEARER_TOKEN='tok'\n"
    )
    monkeypatch.setenv("UNITARES_SECRETS_ENV", str(secrets))
    assert doctor._orchestrator_bearer() == "tok"


def test_it_is_registered_as_an_operator_check():
    checks = {c.name: c.mode for c in doctor.build_checks(REPO_ROOT, "postgresql://x/y")}
    assert checks["orchestrator_profiles"] == "operator"
