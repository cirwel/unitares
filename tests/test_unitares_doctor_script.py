"""Smoke tests for scripts/dev/unitares_doctor.py.

The doctor itself probes the live machine (postgres, launchctl, the HTTP
endpoint), which is brittle in CI and not what we want to test. These tests
exercise the runner harness — the part that aggregates check results, filters
by mode, sets exit codes, and renders output — using fake checks.
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPT = REPO_ROOT / "scripts" / "dev" / "unitares_doctor.py"


@pytest.fixture(scope="module")
def doctor():
    spec = importlib.util.spec_from_file_location("unitares_doctor", SCRIPT)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    sys.modules["unitares_doctor"] = mod  # Python 3.14 dataclass needs this
    spec.loader.exec_module(mod)
    return mod


def _fake(doctor, name: str, mode: str, status):
    return doctor.Check(
        name, mode,
        lambda: doctor.CheckResult(name, mode, status, f"{name} message"),
    )


def test_run_checks_filters_by_mode(doctor):
    checks = [
        _fake(doctor, "a", "local", doctor.Status.PASS),
        _fake(doctor, "b", "operator", doctor.Status.PASS),
    ]
    local = doctor.run_checks(checks, "local")
    assert [r.name for r in local] == ["a"]
    op = doctor.run_checks(checks, "operator")
    assert [r.name for r in op] == ["b"]
    all_r = doctor.run_checks(checks, "all")
    assert [r.name for r in all_r] == ["a", "b"]


def test_exit_code_zero_when_no_failures(doctor):
    results = [
        doctor.CheckResult("a", "local", doctor.Status.PASS, "ok"),
        doctor.CheckResult("b", "local", doctor.Status.WARN, "meh"),
        doctor.CheckResult("c", "local", doctor.Status.SKIP, "skipped"),
    ]
    assert doctor.exit_code(results) == 0


def test_exit_code_nonzero_on_failure(doctor):
    results = [
        doctor.CheckResult("a", "local", doctor.Status.PASS, "ok"),
        doctor.CheckResult("b", "local", doctor.Status.FAIL, "broken"),
    ]
    assert doctor.exit_code(results) == 1


def test_check_exception_becomes_fail(doctor):
    def boom():
        raise RuntimeError("kaboom")

    results = doctor.run_checks([doctor.Check("explodes", "local", boom)], "all")
    assert len(results) == 1
    assert results[0].status == doctor.Status.FAIL
    assert "kaboom" in results[0].detail


def test_render_text_includes_all_results(doctor):
    results = [
        doctor.CheckResult("a", "local", doctor.Status.PASS, "all good"),
        doctor.CheckResult("b", "operator", doctor.Status.FAIL, "nope",
                           detail="hint here"),
    ]
    text = doctor.render_text(results, use_color=False)
    assert "=== local ===" in text
    assert "=== operator ===" in text
    assert "all good" in text
    assert "nope" in text
    assert "hint here" in text
    assert "1 pass" in text and "1 fail" in text


def test_render_text_no_color_does_not_emit_ansi(doctor):
    results = [doctor.CheckResult("a", "local", doctor.Status.PASS, "ok")]
    text = doctor.render_text(results, use_color=False)
    assert "\033[" not in text


def test_redact_strips_password(doctor):
    redacted = doctor._redact("postgresql://postgres:secretpass@localhost:5432/governance")
    assert "secretpass" not in redacted
    assert "postgres" not in redacted.split("@")[0].split("://")[1]
    assert "@localhost:5432/governance" in redacted


def _migration_root(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    migrations = root / "db" / "postgres" / "migrations"
    migrations.mkdir(parents=True)
    (migrations / "001_initial_schema.sql").write_text(
        "INSERT INTO core.schema_migrations (version, name) "
        "VALUES (1, 'initial_schema') ON CONFLICT (version) DO NOTHING;\n"
    )
    return root


def test_check_schema_migrations_allows_known_slot_18_exception(doctor, monkeypatch, tmp_path):
    root = _migration_root(tmp_path)

    class Proc:
        returncode = 0
        stdout = "1|initial_schema\n18|progress flat telemetry tables\n"
        stderr = ""

    monkeypatch.setattr(doctor.shutil, "which", lambda _: "/usr/bin/psql")
    monkeypatch.setattr(doctor.subprocess, "run", lambda *args, **kwargs: Proc())

    result = doctor.check_schema_migrations("postgresql://example", root)

    assert result.status == doctor.Status.PASS
    assert "registry matches source manifest" in result.message


def test_check_schema_migrations_detects_unexpected_out_of_band_row(doctor, monkeypatch, tmp_path):
    root = _migration_root(tmp_path)

    class Proc:
        returncode = 0
        stdout = "1|initial_schema\n24|manual hotfix\n"
        stderr = ""

    monkeypatch.setattr(doctor.shutil, "which", lambda _: "/usr/bin/psql")
    monkeypatch.setattr(doctor.subprocess, "run", lambda *args, **kwargs: Proc())

    result = doctor.check_schema_migrations("postgresql://example", root)

    assert result.status == doctor.Status.FAIL
    assert "schema registry drift detected" in result.message
    assert "unexpected 24:manual hotfix" in result.detail


def test_main_json_output(doctor, monkeypatch, capsys, tmp_path):
    # Replace build_checks so we don't probe the live system.
    fake_checks = [_fake(doctor, "always_pass", "local", doctor.Status.PASS)]
    monkeypatch.setattr(doctor, "build_checks", lambda root, url: fake_checks)

    rc = doctor.main(["--json", "--mode", "local"])
    assert rc == 0
    out = capsys.readouterr().out
    payload = json.loads(out)
    assert payload["mode"] == "local"
    assert payload["exit_code"] == 0
    assert payload["results"][0]["name"] == "always_pass"
    assert payload["results"][0]["status"] == "pass"


def test_main_returns_failure_when_check_fails(doctor, monkeypatch, capsys):
    fake_checks = [_fake(doctor, "always_fail", "local", doctor.Status.FAIL)]
    monkeypatch.setattr(doctor, "build_checks", lambda root, url: fake_checks)

    rc = doctor.main(["--json", "--no-color"])
    assert rc == 1


# ---------- elixir_deprecated_scheme_lint (RFC §7.11.8 — Phase B prep) ----------


class _Proc:
    """Tiny stand-in for subprocess.CompletedProcess. Tests pass returncode + stdout."""

    def __init__(self, returncode: int, stdout: str = "", stderr: str = ""):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


def test_elixir_lint_skips_when_psql_missing(doctor, monkeypatch, tmp_path):
    monkeypatch.setattr(doctor.shutil, "which", lambda _: None)
    result = doctor.check_elixir_deprecated_scheme_lint("postgresql://example", tmp_path)
    assert result.status == doctor.Status.SKIP
    assert "psql not on PATH" in result.message


def test_elixir_lint_skips_when_deprecated_schemes_table_absent(doctor, monkeypatch, tmp_path):
    monkeypatch.setattr(doctor.shutil, "which", lambda _: "/usr/bin/psql")
    monkeypatch.setattr(doctor.subprocess, "run",
                        lambda *a, **kw: _Proc(returncode=1, stderr="relation does not exist"))
    result = doctor.check_elixir_deprecated_scheme_lint("postgresql://example", tmp_path)
    assert result.status == doctor.Status.SKIP
    assert "deprecated_schemes not queryable" in result.message


def test_elixir_lint_passes_when_no_deprecated_schemes(doctor, monkeypatch, tmp_path):
    monkeypatch.setattr(doctor.shutil, "which", lambda _: "/usr/bin/psql")
    monkeypatch.setattr(doctor.subprocess, "run",
                        lambda *a, **kw: _Proc(returncode=0, stdout=""))
    result = doctor.check_elixir_deprecated_scheme_lint("postgresql://example", tmp_path)
    assert result.status == doctor.Status.PASS
    assert "no deprecated schemes" in result.message


def test_elixir_lint_passes_when_psql_returns_trailing_newline_only(doctor, monkeypatch, tmp_path):
    """Council CONCERN 2: psql -Atq sometimes emits a trailing newline even
    on zero-row results. The `if line.strip()` guard handles it correctly;
    this test pins that behavior so a refactor doesn't break the PASS gate.
    """
    monkeypatch.setattr(doctor.shutil, "which", lambda _: "/usr/bin/psql")
    monkeypatch.setattr(doctor.subprocess, "run",
                        lambda *a, **kw: _Proc(returncode=0, stdout="\n"))
    result = doctor.check_elixir_deprecated_scheme_lint("postgresql://example", tmp_path)
    assert result.status == doctor.Status.PASS
    assert "no deprecated schemes" in result.message


def test_elixir_lint_skips_when_no_elixir_directory(doctor, monkeypatch, tmp_path):
    monkeypatch.setattr(doctor.shutil, "which", lambda _: "/usr/bin/psql")
    monkeypatch.setattr(doctor.subprocess, "run",
                        lambda *a, **kw: _Proc(returncode=0, stdout="dialectic\n"))
    result = doctor.check_elixir_deprecated_scheme_lint("postgresql://example", tmp_path)
    assert result.status == doctor.Status.SKIP
    assert "no elixir/ directory" in result.message


def test_elixir_lint_passes_when_elixir_does_not_mention_deprecated_kind(doctor, monkeypatch, tmp_path):
    monkeypatch.setattr(doctor.shutil, "which", lambda _: "/usr/bin/psql")
    monkeypatch.setattr(doctor.subprocess, "run",
                        lambda *a, **kw: _Proc(returncode=0, stdout="dialectic\n"))
    elixir_dir = tmp_path / "elixir" / "lease_plane" / "lib"
    elixir_dir.mkdir(parents=True)
    (elixir_dir / "router.ex").write_text(
        '''defmodule Router do
          def dispatch("file://" <> rest), do: rest
          def dispatch("resident:/" <> rest), do: rest
        end
        '''
    )
    result = doctor.check_elixir_deprecated_scheme_lint("postgresql://example", tmp_path)
    assert result.status == doctor.Status.PASS
    assert "dialectic" in result.message


def test_elixir_lint_warns_when_elixir_mentions_deprecated_kind(doctor, monkeypatch, tmp_path):
    monkeypatch.setattr(doctor.shutil, "which", lambda _: "/usr/bin/psql")
    monkeypatch.setattr(doctor.subprocess, "run",
                        lambda *a, **kw: _Proc(returncode=0, stdout="dialectic\n"))
    elixir_dir = tmp_path / "elixir" / "lease_plane" / "lib"
    elixir_dir.mkdir(parents=True)
    (elixir_dir / "canonicalize.ex").write_text(
        '''defmodule Canonicalize do
          defp dispatch("dialectic:" <> rest), do: rest
        end
        '''
    )
    result = doctor.check_elixir_deprecated_scheme_lint("postgresql://example", tmp_path)
    assert result.status == doctor.Status.WARN
    assert "1 Elixir source mention" in result.message
    assert "dialectic" in result.message
    assert "canonicalize.ex" in result.detail


def test_elixir_lint_excludes_deps_and_build_dirs(doctor, monkeypatch, tmp_path):
    """Vendored deps + _build artifacts mention scheme strings (e.g., bandit
    docs) but they're third-party; lint must skip them to avoid noise."""
    monkeypatch.setattr(doctor.shutil, "which", lambda _: "/usr/bin/psql")
    monkeypatch.setattr(doctor.subprocess, "run",
                        lambda *a, **kw: _Proc(returncode=0, stdout="dialectic\n"))
    deps_dir = tmp_path / "elixir" / "lease_plane" / "deps" / "bandit"
    deps_dir.mkdir(parents=True)
    (deps_dir / "vendored.ex").write_text('"dialectic:" — incidental string in vendored dep')

    result = doctor.check_elixir_deprecated_scheme_lint("postgresql://example", tmp_path)
    assert result.status == doctor.Status.PASS, (
        f"vendored deps/ mentions must not trigger WARN; got {result.status}: {result.message}"
    )
