"""Smoke tests for scripts/install/setup.py.

Mirrors tests/test_unitares_doctor_script.py: imports the script as a module,
patches its internal subprocess wrapper, and uses tmp_path for filesystem
mutations. Never touches real ~/.unitares/, real client configs, or live
postgres.
"""

from __future__ import annotations

import importlib.util
import stat
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPT = REPO_ROOT / "scripts" / "install" / "setup.py"


@pytest.fixture(scope="module")
def setup_mod():
    spec = importlib.util.spec_from_file_location("unitares_setup", SCRIPT)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    sys.modules["unitares_setup"] = mod  # Python 3.14 dataclass needs this
    spec.loader.exec_module(mod)
    return mod


def test_module_loads_and_exposes_main(setup_mod):
    assert callable(setup_mod.main)
    assert setup_mod.SCHEMA_VERSION == 1


def test_bootstrap_check_passes_when_mcp_importable(setup_mod, monkeypatch):
    # mcp SDK is installed in the project's full requirements; this is the
    # happy path — bootstrap_check should return without raising.
    setup_mod.bootstrap_check()  # no exception


def test_bootstrap_check_exits_when_mcp_missing(setup_mod, monkeypatch, capsys):
    import builtins
    real_import = builtins.__import__

    def fake_import(name, *args, **kwargs):
        if name == "mcp":
            raise ImportError("No module named 'mcp'")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", fake_import)
    with pytest.raises(SystemExit) as exc:
        setup_mod.bootstrap_check()
    assert exc.value.code == 2
    err = capsys.readouterr().out
    assert "pip install -r requirements-full.txt" in err


def test_run_doctor_parses_pass_payload(setup_mod, monkeypatch):
    fake_stdout = '{"mode": "local", "results": [{"name": "x", "mode": "local", "status": "pass", "message": "ok", "detail": ""}], "exit_code": 0}'

    class FakeProc:
        returncode = 0
        stdout = fake_stdout
        stderr = ""

    monkeypatch.setattr(setup_mod.subprocess, "run", lambda *a, **kw: FakeProc())
    result = setup_mod.run_doctor()
    assert result["mode"] == "local"
    assert result["results"][0]["status"] == "pass"
    assert result["exit_code"] == 0


def test_run_doctor_uses_homebrew_trust_dsn_by_default(setup_mod, monkeypatch):
    captured = {}

    class FakeProc:
        returncode = 0
        stdout = '{"mode":"local","results":[],"exit_code":0}'
        stderr = ""

    def fake_run(cmd, **kwargs):
        captured["cmd"] = cmd
        return FakeProc()

    monkeypatch.delenv("DB_POSTGRES_URL", raising=False)
    monkeypatch.setattr(setup_mod.subprocess, "run", fake_run)
    setup_mod.run_doctor()

    db_arg = captured["cmd"].index("--db-url") + 1
    assert captured["cmd"][db_arg] == "postgresql://localhost:5432/governance"


def test_run_doctor_handles_nonzero_exit_with_valid_json(setup_mod, monkeypatch):
    """Doctor exits 1 when any local check fails; setup must NOT treat that
    as an error — the JSON payload is still complete and is the input to
    remediation."""
    fake_stdout = '{"mode": "local", "results": [{"name": "postgres_running", "mode": "local", "status": "fail", "message": "down", "detail": ""}], "exit_code": 1}'

    class FakeProc:
        returncode = 1
        stdout = fake_stdout
        stderr = ""

    monkeypatch.setattr(setup_mod.subprocess, "run", lambda *a, **kw: FakeProc())
    result = setup_mod.run_doctor()
    assert result["results"][0]["status"] == "fail"
    assert result["exit_code"] == 1


def test_run_doctor_raises_on_invalid_json(setup_mod, monkeypatch):
    class FakeProc:
        returncode = 0
        stdout = "not json"
        stderr = ""

    monkeypatch.setattr(setup_mod.subprocess, "run", lambda *a, **kw: FakeProc())
    with pytest.raises(setup_mod.DoctorError):
        setup_mod.run_doctor()


def _doctor_payload(*finds):
    """Build a doctor payload with the given (name, status, message) tuples."""
    return {
        "mode": "local",
        "results": [
            {"name": n, "mode": "local", "status": s, "message": m, "detail": ""}
            for n, s, m in finds
        ],
        "exit_code": 1 if any(s == "fail" for _, s, _ in finds) else 0,
    }


def test_remediation_for_postgres_fail(setup_mod):
    payload = _doctor_payload(("postgres_running", "fail", "down"))
    items = setup_mod.build_remediation(payload)
    assert len(items) == 1
    assert items[0].finding == "postgres_running"
    assert "brew install postgresql@17" in items[0].command
    assert items[0].applied is False


def test_remediation_for_pg_extensions_fail(setup_mod):
    payload = _doctor_payload(("pg_extensions", "fail", "missing: age, vector"))
    items = setup_mod.build_remediation(payload)
    assert "postgresql://localhost:5432/governance" in items[0].command
    assert "-U postgres" not in items[0].command
    assert "init-extensions.sql" in items[0].command


def test_remediation_for_redis_continuity_warning(setup_mod):
    payload = _doctor_payload(("redis_continuity", "warn", "degraded-local"))
    items = setup_mod.build_remediation(payload)
    assert "brew install redis" in items[0].command
    assert "not production session continuity" in items[0].note


def test_schema_remediation_uses_canonical_bootstrap(setup_mod):
    payload = _doctor_payload(("schema_migrations", "fail", "missing"))
    items = setup_mod.build_remediation(payload)
    assert items[0].command == "./scripts/install/bootstrap_postgres.sh --apply"


def test_remediation_for_secrets_wrong_mode(setup_mod):
    payload = _doctor_payload(("secrets_file", "fail", "mode is 0o644 — must be 0600"))
    items = setup_mod.build_remediation(payload)
    assert "chmod 600" in items[0].command


def test_remediation_skips_pass_results(setup_mod):
    payload = _doctor_payload(
        ("python_version", "pass", "Python 3.14"),
        ("postgres_running", "fail", "down"),
    )
    items = setup_mod.build_remediation(payload)
    # python_version is pass, only postgres_running gets a remediation block.
    assert len(items) == 1
    assert items[0].finding == "postgres_running"


def test_remediation_includes_warns(setup_mod):
    payload = _doctor_payload(("anchor_directory", "warn", "missing"))
    items = setup_mod.build_remediation(payload)
    assert len(items) == 1
    assert items[0].finding == "anchor_directory"


def test_ensure_anchor_dir_dry_run_no_writes(setup_mod, tmp_path):
    target = tmp_path / "unitares"
    assert not target.exists()
    item = setup_mod.ensure_anchor_dir(target, apply=False)
    assert not target.exists()
    assert item.applied is False
    assert item.path == str(target)
    assert item.mode == "0o700"


def test_ensure_anchor_dir_apply_creates_with_mode_0700(setup_mod, tmp_path):
    target = tmp_path / "unitares"
    item = setup_mod.ensure_anchor_dir(target, apply=True)
    assert target.is_dir()
    actual = stat.S_IMODE(target.stat().st_mode)
    assert actual == 0o700, f"expected 0o700 got {oct(actual)}"
    assert item.applied is True


def test_ensure_anchor_dir_apply_idempotent(setup_mod, tmp_path):
    target = tmp_path / "unitares"
    target.mkdir(mode=0o700)
    item = setup_mod.ensure_anchor_dir(target, apply=True)
    # Already existed — applied should be False (we did not mutate).
    assert item.applied is False


def test_ensure_secrets_file_dry_run_no_writes(setup_mod, tmp_path):
    target = tmp_path / "secrets.env"
    item = setup_mod.ensure_secrets_file(target, apply=False)
    assert not target.exists()
    assert item.applied is False
    assert item.mode == "0o600"


def test_ensure_secrets_file_apply_creates_with_mode_0600(setup_mod, tmp_path):
    target = tmp_path / "secrets.env"
    item = setup_mod.ensure_secrets_file(target, apply=True)
    assert target.is_file()
    actual = stat.S_IMODE(target.stat().st_mode)
    assert actual == 0o600, f"expected 0o600 got {oct(actual)}"
    content = target.read_text()
    assert "ANTHROPIC_API_KEY" in content
    assert "mode 0600, never commit" in content
    assert item.applied is True


def test_ensure_secrets_file_does_not_overwrite_existing(setup_mod, tmp_path):
    target = tmp_path / "secrets.env"
    target.write_text("ANTHROPIC_API_KEY=existing-secret\n")
    target.chmod(0o600)
    item = setup_mod.ensure_secrets_file(target, apply=True)
    # File preserved exactly.
    assert target.read_text() == "ANTHROPIC_API_KEY=existing-secret\n"
    assert item.applied is False


def test_detect_clients_finds_existing_paths(setup_mod, tmp_path):
    # Build a fake home with claude_code + codex installed; antigravity and
    # copilot absent.
    fake_home = tmp_path / "home"
    (fake_home / ".claude").mkdir(parents=True)
    (fake_home / ".codex").mkdir(parents=True)
    detected = setup_mod.detect_clients(fake_home)
    assert "claude_code" in detected
    assert "codex" in detected
    assert "antigravity" not in detected
    assert "copilot" not in detected


def test_detect_clients_finds_antigravity_at_its_real_path(setup_mod, tmp_path):
    """The retired Gemini CLI entry pointed at ~/.config/gemini, which no real
    install creates, so it could never fire. Antigravity's own directory does."""
    fake_home = tmp_path / "home"
    (fake_home / ".gemini" / "antigravity").mkdir(parents=True)
    detected = setup_mod.detect_clients(fake_home)
    assert "antigravity" in detected
    assert detected["antigravity"]["config_path"].endswith(".gemini/config/mcp_config.json")
    assert detected["antigravity"]["format"] == "json"


def test_detect_clients_returns_config_paths(setup_mod, tmp_path):
    fake_home = tmp_path / "home"
    (fake_home / ".claude").mkdir(parents=True)
    detected = setup_mod.detect_clients(fake_home)
    assert detected["claude_code"]["config_path"].endswith(".claude/settings.json")
    assert detected["claude_code"]["format"] == "json"


def test_detect_clients_handles_empty_home(setup_mod, tmp_path):
    detected = setup_mod.detect_clients(tmp_path / "empty")
    assert detected == {}


import json as _json


def test_build_snippet_claude_code_json(setup_mod):
    item = setup_mod.build_snippet(
        client="claude_code",
        config_path="/Users/x/.claude/settings.json",
        fmt="json",
        repo_root=Path("/repo"),
        proxy_url=None,
    )
    parsed = _json.loads(item.snippet)
    assert "unitares-governance" in parsed
    entry = parsed["unitares-governance"]
    assert entry["command"] == "python3"
    assert entry["args"] == ["/repo/src/mcp_server_std.py"]
    assert "DB_POSTGRES_URL" in entry["env"]
    assert "UNITARES_STDIO_PROXY_HTTP_URL" not in entry["env"]


def test_build_snippet_codex_toml(setup_mod):
    item = setup_mod.build_snippet(
        client="codex",
        config_path="/Users/x/.codex/config.toml",
        fmt="toml",
        repo_root=Path("/repo"),
        proxy_url=None,
    )
    s = item.snippet
    assert "[mcp_servers.unitares-governance]" in s
    assert 'command = "python3"' in s
    assert '"/repo/src/mcp_server_std.py"' in s


def test_build_snippet_with_proxy_url_adds_env_entry(setup_mod):
    item = setup_mod.build_snippet(
        client="claude_code",
        config_path="/Users/x/.claude/settings.json",
        fmt="json",
        repo_root=Path("/repo"),
        proxy_url="https://gov.example.org/mcp/",
    )
    parsed = _json.loads(item.snippet)
    env = parsed["unitares-governance"]["env"]
    assert env["UNITARES_STDIO_PROXY_HTTP_URL"] == "https://gov.example.org/mcp/"


def test_build_snippet_copilot_todo_note(setup_mod):
    item = setup_mod.build_snippet(
        client="copilot",
        config_path="/Users/x/.config/github-copilot-cli/config.json",
        fmt="todo",
        repo_root=Path("/repo"),
        proxy_url=None,
    )
    assert "TODO" in item.snippet
    assert "speculative" in item.snippet.lower() or "not yet" in item.snippet.lower()


def _patch_doctor(monkeypatch, setup_mod, payload):
    monkeypatch.setattr(setup_mod, "run_doctor", lambda: payload)


def test_run_pipeline_dry_run_emits_full_plan(setup_mod, monkeypatch, tmp_path):
    fake_home = tmp_path / "home"
    (fake_home / ".claude").mkdir(parents=True)
    initial = _doctor_payload(
        ("python_version", "pass", "ok"),
        ("postgres_running", "fail", "down"),
        ("anchor_directory", "warn", "missing"),
    )
    _patch_doctor(monkeypatch, setup_mod, initial)

    out = setup_mod.run_pipeline(
        apply=False, home=fake_home, proxy_url=None,
    )

    assert out["schema_version"] == 1
    assert out["doctor_initial"] == initial
    assert out["doctor_final"] is None  # dry-run does not re-run doctor
    phases = {p.phase for p in out["plan"]}
    assert 1 in phases  # remediation
    assert 2 in phases  # mkdir/file
    assert 3 in phases  # snippet
    # Nothing applied in dry-run.
    assert all(not p.applied for p in out["plan"])


def test_run_pipeline_apply_runs_final_doctor(setup_mod, monkeypatch, tmp_path):
    fake_home = tmp_path / "home"
    initial = _doctor_payload(("anchor_directory", "warn", "missing"))
    final = _doctor_payload(("anchor_directory", "pass", "exists"))

    calls = {"n": 0}

    def fake_run_doctor():
        calls["n"] += 1
        return initial if calls["n"] == 1 else final

    monkeypatch.setattr(setup_mod, "run_doctor", fake_run_doctor)

    out = setup_mod.run_pipeline(
        apply=True, home=fake_home, proxy_url=None,
    )
    assert out["doctor_final"] == final
    # Anchor dir was created.
    assert (fake_home / ".unitares").is_dir()


def test_run_pipeline_idempotent_apply_on_healthy_state(setup_mod, monkeypatch, tmp_path):
    fake_home = tmp_path / "home"
    (fake_home / ".unitares").mkdir(mode=0o700, parents=True)
    secrets = fake_home / ".config" / "cirwel" / "secrets.env"
    secrets.parent.mkdir(parents=True)
    secrets.write_text("EXISTING=1\n")
    secrets.chmod(0o600)

    healthy = _doctor_payload(
        ("python_version", "pass", "ok"),
        ("postgres_running", "pass", "ok"),
    )
    _patch_doctor(monkeypatch, setup_mod, healthy)

    out = setup_mod.run_pipeline(apply=True, home=fake_home, proxy_url=None)

    # Phase 2 items are present but applied=False because everything already
    # existed.
    phase2 = [p for p in out["plan"] if p.phase == 2]
    assert phase2  # we still emit the items
    assert all(not p.applied for p in phase2)
    # Existing secret content untouched.
    assert secrets.read_text() == "EXISTING=1\n"


def test_main_json_dry_run(setup_mod, monkeypatch, tmp_path, capsys):
    initial = _doctor_payload(("python_version", "pass", "ok"))
    monkeypatch.setattr(setup_mod, "run_doctor", lambda: initial)
    monkeypatch.setattr(setup_mod, "Path", setup_mod.Path)
    # Redirect HOME so detect_clients sees nothing.
    monkeypatch.setenv("HOME", str(tmp_path))
    rc = setup_mod.main(["--json"])
    assert rc == 0
    out = capsys.readouterr().out
    payload = _json.loads(out)
    assert payload["schema_version"] == 1
    assert payload["doctor_final"] is None  # dry-run


def test_main_apply_with_non_interactive_skips_prompt(setup_mod, monkeypatch, tmp_path, capsys):
    healthy = _doctor_payload(("python_version", "pass", "ok"))
    monkeypatch.setattr(setup_mod, "run_doctor", lambda: healthy)
    monkeypatch.setenv("HOME", str(tmp_path))
    # If main attempted input(), this would block. The test passing proves
    # --non-interactive bypasses the prompt.
    rc = setup_mod.main(["--apply", "--non-interactive", "--json"])
    assert rc == 0


def test_main_text_output_lists_remediations(setup_mod, monkeypatch, tmp_path, capsys):
    failing = _doctor_payload(("postgres_running", "fail", "down"))
    monkeypatch.setattr(setup_mod, "run_doctor", lambda: failing)
    monkeypatch.setenv("HOME", str(tmp_path))
    setup_mod.main(["--non-interactive"])  # dry-run + non-interactive
    out = capsys.readouterr().out
    assert "brew install postgresql@17" in out
    assert "remediation" in out.lower() or "phase 1" in out.lower()


def test_main_proxy_url_propagates_to_snippet(setup_mod, monkeypatch, tmp_path, capsys):
    healthy = _doctor_payload(("python_version", "pass", "ok"))
    monkeypatch.setattr(setup_mod, "run_doctor", lambda: healthy)
    fake_home = tmp_path
    (fake_home / ".claude").mkdir()
    monkeypatch.setenv("HOME", str(fake_home))
    setup_mod.main([
        "--json",
        "--proxy-url=https://gov.example.org/mcp/",
    ])
    out = capsys.readouterr().out
    payload = _json.loads(out)
    snippet_items = [p for p in payload["plan"] if p["phase"] == 3]
    assert any("UNITARES_STDIO_PROXY_HTTP_URL" in p["snippet"] for p in snippet_items)


def test_dry_run_exit_code_is_1_when_initial_doctor_has_fails(setup_mod, monkeypatch, tmp_path):
    """CI consumers gate on exit_code; a dry-run that hides initial failures
    behind exit 0 would let broken installs slip through."""
    failing = _doctor_payload(("postgres_running", "fail", "down"))
    monkeypatch.setattr(setup_mod, "run_doctor", lambda: failing)
    out = setup_mod.run_pipeline(apply=False, home=tmp_path, proxy_url=None)
    assert out["exit_code"] == 1


def test_dry_run_exit_code_is_0_when_initial_doctor_is_clean(setup_mod, monkeypatch, tmp_path):
    healthy = _doctor_payload(("python_version", "pass", "ok"))
    monkeypatch.setattr(setup_mod, "run_doctor", lambda: healthy)
    out = setup_mod.run_pipeline(apply=False, home=tmp_path, proxy_url=None)
    assert out["exit_code"] == 0


def test_json_output_always_includes_applied_field(setup_mod, monkeypatch, tmp_path, capsys):
    """Spec line 158-165 shows `applied: false` explicitly; consumers need it
    to distinguish 'pending' from 'done' without inferring from absence."""
    failing = _doctor_payload(("postgres_running", "fail", "down"))
    monkeypatch.setattr(setup_mod, "run_doctor", lambda: failing)
    monkeypatch.setenv("HOME", str(tmp_path))
    setup_mod.main(["--json", "--non-interactive"])
    out = capsys.readouterr().out
    payload = _json.loads(out)
    # Every plan item, regardless of phase, must report `applied`.
    for item in payload["plan"]:
        assert "applied" in item, f"item missing applied field: {item}"
