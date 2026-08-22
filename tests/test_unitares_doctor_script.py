"""Smoke tests for scripts/dev/unitares_doctor.py.

The doctor itself probes the live machine (postgres, launchctl, the HTTP
endpoint), which is brittle in CI and not what we want to test. These tests
exercise the runner harness — the part that aggregates check results, filters
by mode, sets exit codes, and renders output — using fake checks.
"""

from __future__ import annotations

import importlib.util
import json
import os
import re
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


def test_redis_continuity_warns_when_cli_is_missing(doctor, monkeypatch):
    monkeypatch.setattr(doctor.shutil, "which", lambda _: None)
    result = doctor.check_redis_continuity("redis://localhost:6379/0")
    assert result.status == doctor.Status.WARN
    assert "production session continuity" in result.message
    assert "degraded local-only" in result.detail


def test_redis_continuity_passes_without_exposing_credentials(doctor, monkeypatch):
    captured = {}

    class Proc:
        returncode = 0
        stdout = "PONG\n"
        stderr = ""

    def fake_run(cmd, **kwargs):
        captured["cmd"] = cmd
        captured["env"] = kwargs["env"]
        return Proc()

    monkeypatch.setattr(doctor.shutil, "which", lambda _: "/usr/bin/redis-cli")
    monkeypatch.setattr(doctor.subprocess, "run", fake_run)
    result = doctor.check_redis_continuity(
        "redis://operator:secret-value@cache.example:6380/2"
    )

    assert result.status == doctor.Status.PASS
    assert "secret-value" not in " ".join(captured["cmd"])
    assert captured["env"]["REDISCLI_AUTH"] == "secret-value"
    assert "secret-value" not in result.message
    assert "redis://cache.example:6380/2" in result.message


def test_redis_continuity_warns_when_ping_fails(doctor, monkeypatch):
    class Proc:
        returncode = 1
        stdout = ""
        stderr = "connection refused"

    monkeypatch.setattr(doctor.shutil, "which", lambda _: "/usr/bin/redis-cli")
    monkeypatch.setattr(doctor.subprocess, "run", lambda *args, **kwargs: Proc())
    result = doctor.check_redis_continuity("redis://localhost:6379/0")
    assert result.status == doctor.Status.WARN
    assert "degraded local-only" in result.message
    assert "connection refused" in result.detail


def test_check_pid_file_warns_on_stale_pid_when_service_active(doctor, monkeypatch, tmp_path):
    root = tmp_path / "repo"
    data_dir = root / "data"
    data_dir.mkdir(parents=True)
    (data_dir / ".mcp_server.pid").write_text("12345")
    monkeypatch.setattr(doctor.os, "kill", lambda *_: (_ for _ in ()).throw(ProcessLookupError()))

    result = doctor.check_pid_file(root, service_active=True)

    assert result.status == doctor.Status.WARN
    assert "stale file" in result.message
    assert "live service detected" in result.message


def test_check_pid_file_fails_on_stale_pid_without_live_service(doctor, monkeypatch, tmp_path):
    root = tmp_path / "repo"
    data_dir = root / "data"
    data_dir.mkdir(parents=True)
    (data_dir / ".mcp_server.pid").write_text("12345")
    monkeypatch.setattr(doctor.os, "kill", lambda *_: (_ for _ in ()).throw(ProcessLookupError()))
    monkeypatch.setattr(doctor, "_http_health_available", lambda: False)

    result = doctor.check_pid_file(root)

    assert result.status == doctor.Status.FAIL
    assert "stale file" in result.message


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


def _src_root_with_insert(tmp_path: Path, sql: str) -> Path:
    """Create a tmp_path/repo with src/fake.py containing the given SQL string."""
    root = tmp_path / "repo"
    src_dir = root / "src"
    src_dir.mkdir(parents=True)
    (src_dir / "fake.py").write_text(
        'async def insert():\n'
        f'    await conn.execute("""\n{sql}\n""")\n'
    )
    return root


def test_check_column_drift_skips_when_no_inserts(doctor, monkeypatch, tmp_path):
    root = tmp_path / "repo"
    (root / "src").mkdir(parents=True)
    monkeypatch.setattr(doctor.shutil, "which", lambda _: "/usr/bin/psql")
    result = doctor.check_column_drift("postgresql://example", root)
    assert result.status == doctor.Status.SKIP
    assert "no INSERT" in result.message


def test_check_column_drift_passes_when_all_columns_exist(doctor, monkeypatch, tmp_path):
    sql = "INSERT INTO core.identities (id, name, status) VALUES ($1, $2, $3)"
    root = _src_root_with_insert(tmp_path, sql)

    class Proc:
        returncode = 0
        stdout = "id\nname\nstatus\n"
        stderr = ""

    monkeypatch.setattr(doctor.shutil, "which", lambda _: "/usr/bin/psql")
    monkeypatch.setattr(doctor.subprocess, "run", lambda *args, **kwargs: Proc())

    result = doctor.check_column_drift("postgresql://example", root)
    assert result.status == doctor.Status.PASS
    assert "3 INSERT-referenced columns" in result.message


def test_check_column_drift_fails_when_column_missing(doctor, monkeypatch, tmp_path):
    """Reproduces the 2026-05-07 discoveries.provenance_chain class of bug:
    code references a column the running DB doesn't have."""
    sql = (
        "INSERT INTO knowledge.discoveries (\n"
        "    id, summary, provenance_chain\n"
        ") VALUES ($1, $2, $3)"
    )
    root = _src_root_with_insert(tmp_path, sql)

    class Proc:
        returncode = 0
        stdout = "id\nsummary\n"  # provenance_chain column missing from DB
        stderr = ""

    monkeypatch.setattr(doctor.shutil, "which", lambda _: "/usr/bin/psql")
    monkeypatch.setattr(doctor.subprocess, "run", lambda *args, **kwargs: Proc())

    result = doctor.check_column_drift("postgresql://example", root)
    assert result.status == doctor.Status.FAIL
    assert "missing from DB" in result.message
    assert "knowledge.discoveries.provenance_chain" in result.detail


def test_check_column_drift_skips_table_lookup_failure(doctor, monkeypatch, tmp_path):
    """If a referenced table doesn't exist (psql lookup returns empty),
    that's another check's concern — column_drift just skips."""
    sql = "INSERT INTO some.notable_table (a, b) VALUES ($1, $2)"
    root = _src_root_with_insert(tmp_path, sql)

    class Proc:
        returncode = 0
        stdout = ""  # table absent
        stderr = ""

    monkeypatch.setattr(doctor.shutil, "which", lambda _: "/usr/bin/psql")
    monkeypatch.setattr(doctor.subprocess, "run", lambda *args, **kwargs: Proc())

    result = doctor.check_column_drift("postgresql://example", root)
    # Pass with 0 refs counted (table skipped)
    assert result.status == doctor.Status.PASS
    assert "0 INSERT-referenced columns" in result.message


def test_check_column_drift_skips_when_psql_missing(doctor, monkeypatch, tmp_path):
    monkeypatch.setattr(doctor.shutil, "which", lambda _: None)
    result = doctor.check_column_drift("postgresql://example", tmp_path)
    assert result.status == doctor.Status.SKIP


def test_main_json_output(doctor, monkeypatch, capsys, tmp_path):
    # Replace build_checks so we don't probe the live system.
    fake_checks = [_fake(doctor, "always_pass", "local", doctor.Status.PASS)]
    monkeypatch.setattr(
        doctor, "build_checks", lambda root, url, redis_url: fake_checks
    )

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
    monkeypatch.setattr(
        doctor, "build_checks", lambda root, url, redis_url: fake_checks
    )

    rc = doctor.main(["--json", "--no-color"])
    assert rc == 1


# ---------- resident_agents ----------


def test_resident_agents_accepts_python_sentinel(doctor):
    loaded = {
        "com.unitares.vigil",
        "com.unitares.sentinel",
        "com.unitares.chronicler",
    }

    result = doctor.check_resident_agents(loaded)

    assert result.status == doctor.Status.PASS
    assert "sentinel=com.unitares.sentinel" in result.message


def test_resident_agents_accepts_beam_sentinel(doctor):
    loaded = {
        "com.unitares.vigil",
        "com.unitares.sentinel-beam",
        "com.unitares.chronicler",
    }

    result = doctor.check_resident_agents(loaded)

    assert result.status == doctor.Status.PASS
    assert "sentinel=com.unitares.sentinel-beam" in result.message


def test_resident_agents_reports_missing_slot_with_alternatives(doctor):
    loaded = {
        "com.unitares.vigil",
        "com.unitares.chronicler",
    }

    result = doctor.check_resident_agents(loaded)

    assert result.status == doctor.Status.WARN
    assert "sentinel (com.unitares.sentinel or com.unitares.sentinel-beam)" in result.message


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


# ---------- elixir_scheme_grammar_lint (RFC §7.11.8 inverse — Phase B prep) ----------


_GRAMMAR_CHECK_DEF = (
    "CHECK ((surface_id ~ "
    "'^(file://|dialectic:/|resident:/|maintenance:/|capture:/|td:/|agent:/)'::text))"
)


def _write_canonicalize(tmp_path: Path, body: str) -> None:
    canonicalize = (
        tmp_path / "elixir" / "lease_plane" / "lib"
        / "unitares_lease_plane" / "canonicalize.ex"
    )
    canonicalize.parent.mkdir(parents=True)
    canonicalize.write_text(body)


def test_grammar_lint_skips_when_psql_missing(doctor, monkeypatch, tmp_path):
    monkeypatch.setattr(doctor.shutil, "which", lambda _: None)
    result = doctor.check_elixir_scheme_grammar_lint("postgresql://example", tmp_path)
    assert result.status == doctor.Status.SKIP
    assert "psql not on PATH" in result.message


def test_grammar_lint_skips_when_constraint_absent(doctor, monkeypatch, tmp_path):
    monkeypatch.setattr(doctor.shutil, "which", lambda _: "/usr/bin/psql")
    monkeypatch.setattr(doctor.subprocess, "run",
                        lambda *a, **kw: _Proc(returncode=0, stdout=""))
    result = doctor.check_elixir_scheme_grammar_lint("postgresql://example", tmp_path)
    assert result.status == doctor.Status.SKIP
    assert "surface_id_grammar" in result.message


def test_grammar_lint_skips_when_canonicalize_ex_missing(doctor, monkeypatch, tmp_path):
    monkeypatch.setattr(doctor.shutil, "which", lambda _: "/usr/bin/psql")
    monkeypatch.setattr(doctor.subprocess, "run",
                        lambda *a, **kw: _Proc(returncode=0, stdout=_GRAMMAR_CHECK_DEF))
    result = doctor.check_elixir_scheme_grammar_lint("postgresql://example", tmp_path)
    assert result.status == doctor.Status.SKIP
    assert "canonicalize.ex" in result.message


def test_grammar_lint_passes_when_elixir_matches_grammar(doctor, monkeypatch, tmp_path):
    monkeypatch.setattr(doctor.shutil, "which", lambda _: "/usr/bin/psql")
    monkeypatch.setattr(doctor.subprocess, "run",
                        lambda *a, **kw: _Proc(returncode=0, stdout=_GRAMMAR_CHECK_DEF))
    _write_canonicalize(tmp_path, '''defmodule Canonicalize do
      @canonical_schemes ~w(file dialectic resident maintenance capture td agent)
      defp dispatch("file://" <> rest), do: rest
      defp dispatch("dialectic:/" <> rest), do: rest
      defp dispatch("resident:/" <> rest), do: rest
      defp dispatch("maintenance:/" <> rest), do: rest
      defp dispatch("capture:/" <> rest), do: rest
      defp dispatch("td:/" <> rest), do: rest
      defp dispatch("agent:/" <> rest), do: rest
    end
    ''')
    result = doctor.check_elixir_scheme_grammar_lint("postgresql://example", tmp_path)
    assert result.status == doctor.Status.PASS, result.message
    for scheme in ("file", "dialectic", "resident", "maintenance", "capture", "td", "agent"):
        assert scheme in result.message


def test_grammar_lint_fails_when_dispatch_arm_not_in_grammar(doctor, monkeypatch, tmp_path):
    """Inverse drift: Elixir ships a dispatch arm for `foo:/` but the
    migration-026 CHECK doesn't allow it. Every acquire would 422 in prod."""
    monkeypatch.setattr(doctor.shutil, "which", lambda _: "/usr/bin/psql")
    monkeypatch.setattr(doctor.subprocess, "run",
                        lambda *a, **kw: _Proc(returncode=0, stdout=_GRAMMAR_CHECK_DEF))
    _write_canonicalize(tmp_path, '''defmodule Canonicalize do
      @canonical_schemes ~w(file dialectic resident maintenance capture td agent)
      defp dispatch("file://" <> rest), do: rest
      defp dispatch("foo:/" <> rest), do: rest
    end
    ''')
    result = doctor.check_elixir_scheme_grammar_lint("postgresql://example", tmp_path)
    assert result.status == doctor.Status.FAIL
    assert "foo" in result.message
    assert "foo" in result.detail
    assert "Grammar allows" in result.detail


def test_grammar_lint_fails_when_wordlist_has_extra_scheme(doctor, monkeypatch, tmp_path):
    """The `@canonical_schemes ~w(...)` wordlist is itself a scheme declaration
    surface — adding a scheme there without a matching grammar update is the
    same drift class as adding a dispatch arm."""
    monkeypatch.setattr(doctor.shutil, "which", lambda _: "/usr/bin/psql")
    monkeypatch.setattr(doctor.subprocess, "run",
                        lambda *a, **kw: _Proc(returncode=0, stdout=_GRAMMAR_CHECK_DEF))
    _write_canonicalize(tmp_path, '''defmodule Canonicalize do
      @canonical_schemes ~w(file dialectic resident maintenance capture td agent bar)
    end
    ''')
    result = doctor.check_elixir_scheme_grammar_lint("postgresql://example", tmp_path)
    assert result.status == doctor.Status.FAIL
    assert "bar" in result.message


def test_grammar_lint_reports_all_drifting_schemes_sorted(doctor, monkeypatch, tmp_path):
    monkeypatch.setattr(doctor.shutil, "which", lambda _: "/usr/bin/psql")
    monkeypatch.setattr(doctor.subprocess, "run",
                        lambda *a, **kw: _Proc(returncode=0, stdout=_GRAMMAR_CHECK_DEF))
    _write_canonicalize(tmp_path, '''defmodule Canonicalize do
      defp dispatch("zeta:/" <> rest), do: rest
      defp dispatch("alpha:/" <> rest), do: rest
    end
    ''')
    result = doctor.check_elixir_scheme_grammar_lint("postgresql://example", tmp_path)
    assert result.status == doctor.Status.FAIL
    # Sorted: alpha before zeta.
    assert result.message.index("alpha") < result.message.index("zeta")


def test_dockerfile_pinned_tags_passes_on_pinned_images(doctor, tmp_path):
    (tmp_path / "Dockerfile").write_text("FROM python:3.12-slim\n")
    (tmp_path / "db" / "postgres").mkdir(parents=True)
    (tmp_path / "db" / "postgres" / "Dockerfile.age-vector").write_text(
        "FROM apache/age:release_PG18_1.7.0\n"
    )
    (tmp_path / "docker-compose.yml").write_text(
        'services:\n  redis:\n    image: "redis:7-alpine"\n'
    )
    result = doctor.check_dockerfile_pinned_tags(tmp_path)
    assert result.status == doctor.Status.PASS


def test_dockerfile_pinned_tags_flags_floating_from(doctor, tmp_path):
    (tmp_path / "Dockerfile.age-vector").write_text("FROM apache/age:latest\n")
    result = doctor.check_dockerfile_pinned_tags(tmp_path)
    assert result.status == doctor.Status.FAIL
    assert "apache/age:latest" in result.detail


def test_dockerfile_pinned_tags_flags_tagless_image_and_compose(doctor, tmp_path):
    # No tag at all floats to :latest implicitly.
    (tmp_path / "Dockerfile").write_text("FROM ubuntu\n")
    (tmp_path / "docker-compose.yml").write_text(
        "services:\n  cache:\n    image: redis:latest\n"
    )
    result = doctor.check_dockerfile_pinned_tags(tmp_path)
    assert result.status == doctor.Status.FAIL
    assert "FROM ubuntu" in result.detail
    assert "redis:latest" in result.detail


def test_dockerfile_pinned_tags_ignores_build_stage_refs(doctor, tmp_path):
    # `FROM builder` references an earlier stage, not a floating external image.
    (tmp_path / "Dockerfile").write_text(
        "FROM python:3.12-slim AS builder\n"
        "RUN echo build\n"
        "FROM builder\n"
        "RUN echo final\n"
    )
    result = doctor.check_dockerfile_pinned_tags(tmp_path)
    assert result.status == doctor.Status.PASS


def test_check_class_anchors_fresh_runs_and_classifies(doctor):
    """Against the real anchor dicts the freshness check returns a valid result
    (PASS/WARN), is registered, and reports per-class age."""
    result = doctor.check_class_anchors_fresh(REPO_ROOT)
    assert result.name == "class_anchors_fresh"
    assert result.status in {doctor.Status.PASS, doctor.Status.WARN}
    # WARN must name the stale classes so the operator knows what to regenerate.
    if result.status == doctor.Status.WARN:
        assert "stale" in result.message
        assert "calibrate_class_conditional.py" in (result.detail or "")


def test_class_anchors_fresh_is_registered(doctor):
    names = {c.name for c in doctor.build_checks(REPO_ROOT, "postgresql://x/y")}
    assert "class_anchors_fresh" in names
    assert "redis_continuity" in names


# --- telemetry-liveness checks ---------------------------------------------


def _psql_proc(stdout: str):
    class Proc:
        returncode = 0
        stderr = ""

    Proc.stdout = stdout
    return Proc()


def _mock_psql(doctor, monkeypatch, stdout: str):
    monkeypatch.setattr(doctor.shutil, "which", lambda _: "/usr/bin/psql")
    monkeypatch.setattr(doctor.subprocess, "run",
                        lambda *a, **k: _psql_proc(stdout))


def test_failure_label_live_warns_on_flatline(doctor, monkeypatch):
    _mock_psql(doctor, monkeypatch, "500000|0\n")
    result = doctor.check_failure_label_live("postgresql://x/y")
    assert result.status == doctor.Status.WARN
    assert "hardcoded-true" in result.message


def test_failure_label_live_passes_with_failures(doctor, monkeypatch):
    _mock_psql(doctor, monkeypatch, "461512|146\n")
    result = doctor.check_failure_label_live("postgresql://x/y")
    assert result.status == doctor.Status.PASS


def test_failure_label_live_skips_low_volume_flatline(doctor, monkeypatch):
    # A quiet install with 200 calls and 0 failures is not evidence of a dead
    # classifier — only flatline-at-volume warns.
    _mock_psql(doctor, monkeypatch, "200|0\n")
    result = doctor.check_failure_label_live("postgresql://x/y")
    assert result.status == doctor.Status.PASS


def test_failure_label_live_skips_fresh_install(doctor, monkeypatch):
    _mock_psql(doctor, monkeypatch, "0|0\n")
    result = doctor.check_failure_label_live("postgresql://x/y")
    assert result.status == doctor.Status.SKIP


def test_checkin_stream_live_warns_when_dark(doctor, monkeypatch):
    _mock_psql(doctor, monkeypatch, "0|4200\n")
    result = doctor.check_checkin_stream_live("postgresql://x/y")
    assert result.status == doctor.Status.WARN
    assert "governance-dark" in result.message


def test_checkin_stream_live_passes_when_flowing(doctor, monkeypatch):
    _mock_psql(doctor, monkeypatch, "239|4200\n")
    result = doctor.check_checkin_stream_live("postgresql://x/y")
    assert result.status == doctor.Status.PASS


def test_checkin_stream_live_skips_without_history(doctor, monkeypatch):
    _mock_psql(doctor, monkeypatch, "0|0\n")
    result = doctor.check_checkin_stream_live("postgresql://x/y")
    assert result.status == doctor.Status.SKIP


def test_grounding_stage_live_warns_when_silent(doctor, monkeypatch):
    _mock_psql(doctor, monkeypatch, "0|3144\n")
    result = doctor.check_grounding_stage_live("postgresql://x/y")
    assert result.status == doctor.Status.WARN
    assert "silent" in result.message


def test_grounding_stage_live_skips_when_shadow_off(doctor, monkeypatch):
    _mock_psql(doctor, monkeypatch, "0|0\n")
    result = doctor.check_grounding_stage_live("postgresql://x/y")
    assert result.status == doctor.Status.SKIP


def test_grounding_stage_live_passes_when_flowing(doctor, monkeypatch):
    _mock_psql(doctor, monkeypatch, "450|3144\n")
    result = doctor.check_grounding_stage_live("postgresql://x/y")
    assert result.status == doctor.Status.PASS


def test_label_join_overlap_warns_on_disjoint_populations(doctor, monkeypatch):
    _mock_psql(doctor, monkeypatch, "26|126|0\n")
    result = doctor.check_label_join_overlap("postgresql://x/y")
    assert result.status == doctor.Status.WARN
    assert "disjoint" in result.message


def test_label_join_overlap_passes_on_any_overlap(doctor, monkeypatch):
    _mock_psql(doctor, monkeypatch, "26|126|2\n")
    result = doctor.check_label_join_overlap("postgresql://x/y")
    assert result.status == doctor.Status.PASS


def test_label_join_overlap_skips_when_one_side_empty(doctor, monkeypatch):
    _mock_psql(doctor, monkeypatch, "0|126|0\n")
    result = doctor.check_label_join_overlap("postgresql://x/y")
    assert result.status == doctor.Status.SKIP


def test_telemetry_checks_skip_without_psql(doctor, monkeypatch):
    monkeypatch.setattr(doctor.shutil, "which", lambda _: None)
    for fn in (doctor.check_failure_label_live, doctor.check_checkin_stream_live,
               doctor.check_grounding_stage_live, doctor.check_label_join_overlap,
               doctor.check_signal_degeneracy):
        assert fn("postgresql://x/y").status == doctor.Status.SKIP


def test_telemetry_checks_are_registered_as_operator(doctor):
    checks = {c.name: c for c in doctor.build_checks(REPO_ROOT, "postgresql://x/y")}
    for name in ("failure_label_live", "checkin_stream_live",
                 "grounding_stage_live", "label_join_overlap", "signal_degeneracy"):
        assert name in checks
        assert checks[name].mode == "operator"


# --- signal_degeneracy -----------------------------------------------------
# Row layout is (stddev, distinct, count, min, max) per metric, in DEGENERACY_METRICS
# order: coherence, entropy, integrity, risk_score.
# stability_index left the tuple with migration 058 — the retired column is
# NULL now, so there is no signal there to be degenerate.

def _degeneracy_row(*metric_stats):
    return "|".join(str(v) for stats in metric_stats for v in stats) + "\n"


_HEALTHY = (
    (0.0432, 6192, 6232, 0.12, 0.71),
    (0.0612, 833, 6232, 0.55, 0.91),
    (0.0804, 5815, 6232, 0.01, 0.72),
)


def test_signal_degeneracy_warns_on_constant_metric(doctor, monkeypatch):
    """A metric pinned to a single value.

    Was the live 2026-07-30 stability_index case; that column is retired and
    NULL since migration 058, so the shape is asserted on entropy instead —
    the check must still catch a constant wherever one appears.
    """
    _mock_psql(doctor, monkeypatch, _degeneracy_row(
        (0.0432, 6192, 6232, 0.1, 0.8), ("", 1, 6232, 0.4, 0.4), *_HEALTHY[:2]))
    result = doctor.check_signal_degeneracy("postgresql://x/y")
    assert result.status == doctor.Status.WARN
    assert "entropy: constant in 7d" in result.message


def test_signal_degeneracy_warns_on_collapsed_dispersion(doctor, monkeypatch):
    """coherence: 5632 distinct values but sd 0.005 — moves only in the 4th decimal."""
    _mock_psql(doctor, monkeypatch, _degeneracy_row(
        (0.005262, 5632, 6233, 0.4696, 0.5039), *_HEALTHY))
    result = doctor.check_signal_degeneracy("postgresql://x/y")
    assert result.status == doctor.Status.WARN
    assert "coherence: low fleet dispersion" in result.message
    assert "range=[0.4696, 0.5039]" in result.message
    # many distinct values must NOT be mistaken for a healthy signal
    assert "constant" not in result.message
    # SD alone is not an information-theoretic verdict.
    assert "not proof of zero information" in result.detail
    assert "effective sample size" in result.detail


def test_signal_degeneracy_excludes_synthetic_bootstrap_rows(doctor, monkeypatch):
    """Server-authored bootstrap rows are not measurements and must not count.

    2026-08-21: ONE synthetic row carrying coherence=1.0000 moved the reported
    range from [0.4659, 0.5039] to [0.4659, 1.0000] and sd from 0.006818 to
    0.008601 over n=9930 — a dynamic-range check reporting 14x the range its
    consumers actually see, erring toward "healthy". The onboarding contract
    already excludes `synthetic` rows from calibration, outcome correlation and
    trust-tier counts, and `resident_checkin_stale` filters them too; this
    check did not.

    The mock returns a fixed row, so the assertion is on the QUERY — that is
    where the defect lived.
    """
    captured = {}

    def _capture(*args, **kwargs):
        captured["sql"] = args[1] if len(args) > 1 else kwargs.get("sql", "")
        return None

    monkeypatch.setattr(doctor, "_psql_row", _capture)
    doctor.check_signal_degeneracy("postgresql://x/y")

    sql = captured["sql"]
    assert "core.agent_state" in sql
    assert "synthetic IS NOT TRUE" in sql, (
        "signal_degeneracy must exclude synthetic bootstrap rows; without this "
        "one non-measurement can dominate the range it reports"
    )


def test_signal_degeneracy_passes_when_all_metrics_vary(doctor, monkeypatch):
    _mock_psql(doctor, monkeypatch, _degeneracy_row(
        (0.0217, 5000, 6232, 0.35, 0.66), *_HEALTHY))
    result = doctor.check_signal_degeneracy("postgresql://x/y")
    assert result.status == doctor.Status.PASS


def test_signal_degeneracy_skips_thin_data(doctor, monkeypatch):
    """A flat metric on 10 rows is small-sample, not a defect."""
    _mock_psql(doctor, monkeypatch, _degeneracy_row(
        ("", 1, 10, 0.5, 0.5), ("", 1, 10, 0.2, 0.2),
        ("", 1, 10, 0.7, 0.7), ("", 1, 10, 0.1, 0.1)))
    result = doctor.check_signal_degeneracy("postgresql://x/y")
    assert result.status == doctor.Status.SKIP


def test_signal_degeneracy_skips_on_short_row(doctor, monkeypatch):
    """A truncated/malformed row must not be read as 'everything healthy'."""
    _mock_psql(doctor, monkeypatch, "0.02|100|6232|0.4|0.6\n")
    result = doctor.check_signal_degeneracy("postgresql://x/y")
    assert result.status == doctor.Status.SKIP


# --- finding_producer_live -------------------------------------------------
# Row layout is (event_type, n, hours_silent, median_gap_hours, active_days,
# last_severity), one per producer. Fixtures use the real 2026-07 numbers so
# the calibration is pinned to measured cadence rather than invented ones.

def _producer_rows(*rows):
    return "".join("|".join(str(v) for v in r) + "\n" for r in rows)


_LIVE_PRODUCERS = (
    ("sentinel_finding", 402, 29.3, 0.58, 48, "medium"),
    ("sentinel_alarm_finding", 598, 0.0, 0.09, 34, "medium"),
    ("bridge_liveness_finding", 136, 71.4, 1.73, 13, "critical"),
)


def test_finding_producer_live_warns_on_the_watcher_outage(doctor, monkeypatch):
    """The 2026-06-29 case, measured as of 07-25: 612h silent, 0.32h cadence.

    This is the check's reason for existing — a detector whose model call was
    404ing on every scan while every other liveness indicator stayed green.
    """
    _mock_psql(doctor, monkeypatch, _producer_rows(
        ("watcher_finding", 98, 612.0, 0.32, 22, "high"), *_LIVE_PRODUCERS))
    result = doctor.check_finding_producer_live("postgresql://x/y")
    assert result.status == doctor.Status.WARN
    assert "watcher_finding" in result.message
    assert "silent 25.5d" in result.message


def test_finding_producer_live_passes_when_all_report(doctor, monkeypatch):
    _mock_psql(doctor, monkeypatch, _producer_rows(
        ("watcher_finding", 99, 30.0, 0.33, 22, "high"), *_LIVE_PRODUCERS))
    result = doctor.check_finding_producer_live("postgresql://x/y")
    assert result.status == doctor.Status.PASS


def test_finding_producer_live_ignores_event_driven_producers(doctor, monkeypatch):
    """vigil fires 4x in 90d — quiet is its normal state, not a fault."""
    _mock_psql(doctor, monkeypatch, _producer_rows(
        ("vigil_finding", 4, 172.7, 13.75, 3, "medium"), *_LIVE_PRODUCERS))
    result = doctor.check_finding_producer_live("postgresql://x/y")
    assert result.status == doctor.Status.PASS
    assert "vigil" not in result.message


def test_finding_producer_live_ignores_slow_cadence_producers(doctor, monkeypatch):
    """A producer that reports every ~16 days has no weekly cadence to miss."""
    _mock_psql(doctor, monkeypatch, _producer_rows(
        ("sentinel_build_finding", 30, 800.0, 396.24, 12, "medium"), *_LIVE_PRODUCERS))
    result = doctor.check_finding_producer_live("postgresql://x/y")
    assert result.status == doctor.Status.PASS


def test_finding_producer_live_holds_fire_below_the_week_floor(doctor, monkeypatch):
    """10x a 1.7h cadence is 17h — far too tight to page on. The floor wins."""
    _mock_psql(doctor, monkeypatch, _producer_rows(
        ("bridge_liveness_finding", 136, 100.0, 1.73, 13, "critical"),))
    result = doctor.check_finding_producer_live("postgresql://x/y")
    assert result.status == doctor.Status.PASS


def test_finding_producer_live_skips_when_nothing_is_judgeable(doctor, monkeypatch):
    _mock_psql(doctor, monkeypatch, _producer_rows(
        ("healthcheck_selftest_finding", 1, 995.0, "", 1, "info"),))
    result = doctor.check_finding_producer_live("postgresql://x/y")
    assert result.status == doctor.Status.SKIP


def test_finding_producer_live_skips_without_psql(doctor, monkeypatch):
    monkeypatch.setattr(doctor.shutil, "which", lambda _: None)
    assert doctor.check_finding_producer_live("postgresql://x/y").status == doctor.Status.SKIP


def test_finding_producer_live_is_registered_as_operator(doctor):
    checks = {c.name: c for c in doctor.build_checks(REPO_ROOT, "postgresql://x/y")}
    assert "finding_producer_live" in checks
    assert checks["finding_producer_live"].mode == "operator"


def test_finding_producer_live_ignores_single_incident_bursts(doctor, monkeypatch):
    """lease_plane_health: 14 findings across ONE day, then 33 days quiet.

    A fire-on-failure producer emits a burst during an incident (here 66
    consecutive synthetic-acquire failures plus the RECOVERED notice) and
    nothing while things are healthy. The burst gives it a 0.5h median gap, so
    without the active-days gate it looks like a high-cadence producer whose
    healthy silence is death — it warned on arrival for exactly that reason.
    """
    _mock_psql(doctor, monkeypatch, _producer_rows(
        ("lease_plane_health_finding", 14, 799.2, 0.50, 1, "info"), *_LIVE_PRODUCERS))
    result = doctor.check_finding_producer_live("postgresql://x/y")
    assert result.status == doctor.Status.PASS
    assert "lease_plane_health" not in result.message


def test_finding_producer_live_trusts_a_recovered_all_clear(doctor, monkeypatch):
    """The 2026-08-05 false positive: bridge_liveness silent 8.4d = health.

    bridge_liveness_finding spans 13 active days (the July 11-24 wedge storms,
    10-22 re-alerts/day), so the single-incident-burst gate does not exclude
    it and its 1.7h "cadence" baseline is wedge-burst contamination. Its last
    finding was the info-severity "RECOVERED: Discord bridge is alive again"
    (07-28) — and while this check called it silent-dead, the watchdog was
    logging OK every 120s and the bridge heartbeat file was seconds fresh. A
    fire-on-failure producer whose last word was an all-clear is healthy BY
    DESIGN when silent; only a producer whose last word was a problem still
    owes us findings.
    """
    _mock_psql(doctor, monkeypatch, _producer_rows(
        ("bridge_liveness_finding", 136, 201.6, 1.73, 13, "info"),
        *_LIVE_PRODUCERS[:2]))
    result = doctor.check_finding_producer_live("postgresql://x/y")
    assert result.status == doctor.Status.PASS
    assert "bridge_liveness" not in result.message


def test_resident_checkin_stale_warns_on_single_dead_resident(doctor, monkeypatch):
    # The 2026-07-29 Sentinel case: one resident silent 24h while the fleet
    # aggregate stayed healthy. checkin_stream_live cannot see this.
    _mock_psql(doctor, monkeypatch,
               "5|1|Sentinel silent 632min (attributable 632min, "
               "own median 5min, p95 18min)\n")
    result = doctor.check_resident_checkin_stale("postgresql://x/y")
    assert result.status == doctor.Status.WARN
    assert "Sentinel" in result.message
    assert "1 of 5" in result.message


def test_resident_checkin_stale_passes_when_all_current(doctor, monkeypatch):
    _mock_psql(doctor, monkeypatch, "5|0|\n")
    result = doctor.check_resident_checkin_stale("postgresql://x/y")
    assert result.status == doctor.Status.PASS


def test_resident_checkin_stale_skips_fresh_install(doctor, monkeypatch):
    # No agent has enough history to have a cadence yet.
    _mock_psql(doctor, monkeypatch, "0|0|\n")
    result = doctor.check_resident_checkin_stale("postgresql://x/y")
    assert result.status == doctor.Status.SKIP
    # The skip reason must name BOTH gates; naming only the check-in count sent
    # an operator looking for the wrong thing once the day gate could also skip.
    assert "distinct days" in result.message


def test_resident_checkin_stale_requires_multi_day_activity(doctor, monkeypatch):
    """#1486: a bare-named task agent clears 20 check-ins in a single burst,
    passes every marker test (no '#', not claude/codex-prefixed), and is then
    scored as a resident forever — last_seen only recedes, so it can never
    return to PASS. Measured 2026-08-02: SchmidtPacketAudit (32 check-ins,
    1 active day) and fable-lease-triage (33, 2 days) were permanently WARN
    while all five real residents (5-8 active days) read healthy.

    Asserted at the SQL level because the gate IS the SQL — a mocked row count
    would pass whether or not the predicate survived an edit.
    """
    captured = {}

    def capture_psql_row(db_url, sql):
        captured["sql"] = sql
        return ("0", "0", "")

    monkeypatch.setattr(doctor, "_psql_row", capture_psql_row)
    doctor.check_resident_checkin_stale("postgresql://x/y")
    sql = captured["sql"]
    assert "count(DISTINCT (recorded_at AT TIME ZONE 'UTC')::date)" in sql
    assert f">= {doctor.RESIDENT_MIN_ACTIVE_DAYS}" in sql
    assert f"count(*) >= {doctor.RESIDENT_MIN_CHECKINS}" in sql


def test_resident_day_gate_sits_in_the_measured_gap(doctor):
    """The threshold is only defensible if it separates the two populations
    measured on 2026-08-02: task bursts at 1-2 active days, real residents at
    5-8. A value outside that band would either re-admit the bursts or start
    dropping residents."""
    assert 2 < doctor.RESIDENT_MIN_ACTIVE_DAYS < 5


def test_resident_stale_threshold_is_the_idle_envelope(doctor, monkeypatch):
    """The predicate must carry the 2x-p95 term, not median alone.

    Watcher is hook-fired, so its cadence tracks operator activity: measured
    2026-08-05 over 7d, p50=3.4min but p95=80min and max=256min. A
    median-only threshold (30min) sat at ~p87 of its NORMAL distribution and
    warned five times over 08-03..08-05 (silent 50-115min) with zero errors in
    Watcher's log — it was not failing, it was not being invoked. 2x p95
    (~160min) clears all five from the agent's own history; the 2026-07-29
    Sentinel outage (1440min) still fires with a 9x margin. Asserted at the
    SQL level because the envelope IS the SQL.
    """
    captured = {}

    def capture_psql_row(db_url, sql):
        captured["sql"] = sql
        return ("0", "0", "")

    monkeypatch.setattr(doctor, "_psql_row", capture_psql_row)
    doctor.check_resident_checkin_stale("postgresql://x/y")
    sql = captured["sql"]
    assert "percentile_cont(0.95)" in sql
    assert "greatest(med_gap * 6, p95_gap * 2, 1800)" in sql


def test_resident_stale_silence_is_clamped_to_host_awake_time(doctor, monkeypatch):
    """Silence accrued while THIS host slept is not attributable to any agent.

    Central Postgres lives here: during host sleep every resident's last_seen
    recedes together, and the interval-coalesced doctor job fires minutes
    after wake — sampling the post-wake worst case. Measured 2026-08-04/05:
    two Watcher warnings were 107/115 and 38/50 minutes host-asleep, and the
    same runs flagged Lumen and Steward 94-163min silent because the laptop
    lid was closed. The clamp caps the judged silence at awake time; when the
    wake time is unknowable it must fail OPEN to wall-clock silence.
    """
    captured = {}

    def capture_psql_row(db_url, sql):
        captured["sql"] = sql
        return ("0", "0", "")

    monkeypatch.setattr(doctor, "_psql_row", capture_psql_row)
    local_db = "postgresql://postgres@localhost:5432/governance"

    monkeypatch.setattr(doctor, "_host_awake_s", lambda: 120.0)
    doctor.check_resident_checkin_stale(local_db)
    assert "least(EXTRACT(epoch FROM (now() - last_seen)), 120)" in captured["sql"]

    # A remote DB never slept with this laptop: the clamp must not apply even
    # when the local wake time is known — else a just-woken doctor masks real
    # outages on always-on infrastructure.
    doctor.check_resident_checkin_stale("postgresql://postgres@db.example.com/gov")
    assert "least(" not in captured["sql"]

    monkeypatch.setattr(doctor, "_host_awake_s", lambda: None)
    doctor.check_resident_checkin_stale(local_db)
    assert "least(" not in captured["sql"]


def test_host_awake_s_parses_waketime_and_fails_open(doctor, monkeypatch):
    monkeypatch.setattr(doctor.time, "time", lambda: 1_785_942_000.0)
    monkeypatch.setattr(
        doctor, "_run_sysctl_waketime",
        lambda: "{ sec = 1785941495, usec = 682499 } Tue Aug  5 11:31:35 2026\n")
    assert doctor._host_awake_s() == pytest.approx(505.0)
    # Parse miss and sec=0 (never slept / non-macOS) both mean "unknowable".
    monkeypatch.setattr(doctor, "_run_sysctl_waketime", lambda: "")
    assert doctor._host_awake_s() is None
    monkeypatch.setattr(
        doctor, "_run_sysctl_waketime", lambda: "{ sec = 0, usec = 0 }")
    assert doctor._host_awake_s() is None


def test_immortal_lease_warns_on_renewed_orphan(doctor, monkeypatch):
    _mock_psql(doctor, monkeypatch,
               "2|resident:/sentinel_fleet_emit, resident:/ship_sh_claude/adjudication-evidence\n")
    result = doctor.check_immortal_lease("postgresql://x/y")
    assert result.status == doctor.Status.WARN
    assert "sentinel_fleet_emit" in result.message
    assert "force-release" in result.detail
    # The remedy must lead with the liveness check, not the release: across
    # 2026-07-31/08-01 the unconditional "force-release each id" text walked
    # operators into killing steward's live presence lease seven times.
    assert "LIVE client" in result.detail
    assert result.detail.index("really gone") < result.detail.index("force-release")


def test_immortal_lease_excludes_leases_with_fresh_client_contact(doctor, monkeypatch):
    """The 2026-08-01 false-positive class: resident:/ presence leases.

    The router puts every resident:/ acquire on the local_beam auto-renew
    path, so a healthy resident presence lease grows span >> TTL by design.
    Client renews refresh substrate_state_observed_at (the plane-side
    auto-renew never does), so the query MUST exclude leases with a fresh
    observation timestamp — otherwise a live steward is flagged ~35min after
    every acquire, forever.
    """
    captured = {}

    def capture_psql_row(db_url, sql):
        captured["sql"] = sql
        return ("0", "")

    monkeypatch.setattr(doctor, "_psql_row", capture_psql_row)
    result = doctor.check_immortal_lease("postgresql://x/y")
    assert result.status == doctor.Status.PASS
    sql = captured["sql"]
    assert "substrate_state_observed_at IS NULL" in sql
    assert "substrate_state_observed_at < now() - interval '35 minutes'" in sql
    # Span predicate retained — the exclusion narrows, never widens.
    assert "(expires_at - acquired_at) > interval '35 minutes'" in sql


def test_immortal_lease_passes_when_clean(doctor, monkeypatch):
    _mock_psql(doctor, monkeypatch, "0|\n")
    result = doctor.check_immortal_lease("postgresql://x/y")
    assert result.status == doctor.Status.PASS


def test_immortal_lease_skips_without_lease_plane(doctor, monkeypatch):
    monkeypatch.setattr(doctor.shutil, "which", lambda _: None)
    result = doctor.check_immortal_lease("postgresql://x/y")
    assert result.status == doctor.Status.SKIP


def test_liveness_gap_checks_are_registered_as_operator(doctor):
    checks = {c.name: c for c in doctor.build_checks(REPO_ROOT, "postgresql://x/y")}
    for name in ("resident_checkin_stale", "immortal_lease"):
        assert name in checks
        assert checks[name].mode == "operator"


# --- producer_never_reported -----------------------------------------------
# Companion to finding_producer_live: that one is self-relative and so detects
# DIED; a producer with zero rows has no cadence and is absent from its result
# set entirely. This one catches NEVER-BORN.

def _write_producer(tmp_path, rel: str, body: str):
    p = tmp_path / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(body)
    return p


def test_declared_producers_are_read_from_source(doctor, tmp_path):
    _write_producer(tmp_path, "scripts/ops/a.py", 'FINDING_KIND = "alpha_finding"\n')
    _write_producer(tmp_path, "agents/b/agent.py", 'post(event_type="beta_finding")\n')
    assert doctor.declared_finding_producers(tmp_path) == {"alpha_finding", "beta_finding"}


def test_declared_producers_ignore_test_fixtures(doctor, tmp_path):
    """Fixtures name event types they never post; counting them would mint
    permanent false positives that train the operator to ignore this check."""
    _write_producer(tmp_path, "agents/b/agent.py", 'post(event_type="real_finding")\n')
    _write_producer(tmp_path, "agents/b/tests/test_x.py", 'post(event_type="fake_finding")\n')
    _write_producer(tmp_path, "scripts/ops/test_y.py", 'FINDING_KIND = "alsofake_finding"\n')
    assert doctor.declared_finding_producers(tmp_path) == {"real_finding"}


def test_producer_never_reported_warns_on_the_drift_doctor_case(
        doctor, monkeypatch, tmp_path):
    """The 2026-08-01 case: declared, scheduled hourly, never posted once.

    deploy_drift_doctor's interpreter could not import the escalation module;
    the failure was swallowed and it had no cadence for finding_producer_live
    to judge it against. A human found it by asking whether it had ever fired.
    """
    _write_producer(tmp_path, "scripts/ops/deploy_drift_doctor.py",
                    'FINDING_KIND = "deploy_drift_finding"\n')
    _write_producer(tmp_path, "agents/sentinel/agent.py",
                    'post(event_type="sentinel_finding")\n')
    _mock_psql(doctor, monkeypatch, "sentinel_finding\n")
    result = doctor.check_producer_never_reported("postgresql://x/y", tmp_path)
    assert result.status == doctor.Status.WARN
    assert "deploy_drift_finding" in result.message
    assert "sentinel_finding" not in result.message


def test_producer_never_reported_passes_when_all_have_fired(
        doctor, monkeypatch, tmp_path):
    _write_producer(tmp_path, "agents/sentinel/agent.py",
                    'post(event_type="sentinel_finding")\n')
    _mock_psql(doctor, monkeypatch, "sentinel_finding\nvigil_finding\n")
    result = doctor.check_producer_never_reported("postgresql://x/y", tmp_path)
    assert result.status == doctor.Status.PASS


def test_producer_never_reported_skips_without_db(doctor, monkeypatch, tmp_path):
    _write_producer(tmp_path, "agents/sentinel/agent.py",
                    'post(event_type="sentinel_finding")\n')
    monkeypatch.setattr(doctor.shutil, "which", lambda _: None)
    result = doctor.check_producer_never_reported("postgresql://x/y", tmp_path)
    assert result.status == doctor.Status.SKIP


def test_producer_never_reported_skips_with_no_declarations(
        doctor, monkeypatch, tmp_path):
    result = doctor.check_producer_never_reported("postgresql://x/y", tmp_path)
    assert result.status == doctor.Status.SKIP


# --- constraint_drift -------------------------------------------------------
# The parser is the risky half: it must replay drop-then-re-add correctly and
# must not read SQL comments as declarations. Both mistakes were live hazards —
# 056 drops and re-adds the same constraint, and 034's header contains the
# words "ADD CONSTRAINT IF NOT EXISTS" in prose.

def _migrations(tmp_path: Path, files: dict[str, str]) -> Path:
    root = tmp_path / "repo"
    mig = root / "db" / "postgres" / "migrations"
    mig.mkdir(parents=True, exist_ok=True)
    for name, body in files.items():
        (mig / name).write_text(body)
    return root


def test_declared_constraints_keeps_readded_constraint(doctor, tmp_path):
    """Drop-then-re-add in one file ends as ADDed, not as retired (cf. 056)."""
    root = _migrations(tmp_path, {"010_x.sql": """
        ALTER TABLE lease_plane.surface_leases DROP CONSTRAINT reason_check;
        ALTER TABLE lease_plane.surface_leases ADD CONSTRAINT reason_check CHECK (x IN ('a'));
    """})
    declared = doctor._declared_constraints(root / "db" / "postgres" / "migrations")
    assert ("lease_plane.surface_leases", "reason_check") in declared


def test_declared_constraints_drops_retired_constraint(doctor, tmp_path):
    """A later migration's DROP retires it — absence from the DB is correct."""
    root = _migrations(tmp_path, {
        "010_x.sql": "ALTER TABLE k.t ADD CONSTRAINT old_check CHECK (x > 0);",
        "011_y.sql": "ALTER TABLE k.t DROP CONSTRAINT IF EXISTS old_check;",
    })
    declared = doctor._declared_constraints(root / "db" / "postgres" / "migrations")
    assert ("k.t", "old_check") not in declared


def test_declared_constraints_ignores_comments(doctor, tmp_path):
    """Prose mentioning ADD CONSTRAINT must not register a constraint."""
    root = _migrations(tmp_path, {"010_x.sql": """
        -- PG has no ADD CONSTRAINT IF NOT EXISTS syntax, so we use a DO block.
        /* also ADD CONSTRAINT block_comment_check here */
        ALTER TABLE k.t ADD CONSTRAINT real_check CHECK (x > 0);
    """})
    declared = doctor._declared_constraints(root / "db" / "postgres" / "migrations")
    assert set(declared) == {("k.t", "real_check")}


def test_declared_constraints_binds_to_nearest_alter_table(doctor, tmp_path):
    """ALTER TABLE and ADD CONSTRAINT routinely sit on separate lines."""
    root = _migrations(tmp_path, {"010_x.sql": """
        ALTER TABLE a.one
            ADD CONSTRAINT c_one CHECK (x > 0);
        ALTER TABLE b.two
            ADD CONSTRAINT c_two CHECK (y > 0);
    """})
    declared = doctor._declared_constraints(root / "db" / "postgres" / "migrations")
    assert declared.keys() == {("a.one", "c_one"), ("b.two", "c_two")}


def test_constraint_drift_fails_on_missing_constraint(doctor, monkeypatch, tmp_path):
    root = _migrations(tmp_path, {
        "010_x.sql": "ALTER TABLE k.t ADD CONSTRAINT missing_check CHECK (x > 0);"})
    monkeypatch.setattr(doctor.shutil, "which", lambda _: "/usr/bin/psql")
    # The table exists in the DB but carries only an unrelated constraint.
    monkeypatch.setattr(doctor, "_fetch_live_constraints", lambda _: {("k.t", "other_check")})
    result = doctor.check_constraint_drift("postgresql://x/y", root)
    assert result.status == doctor.Status.FAIL
    assert "missing_check" in result.detail


def test_constraint_drift_passes_when_present(doctor, monkeypatch, tmp_path):
    root = _migrations(tmp_path, {
        "010_x.sql": "ALTER TABLE k.t ADD CONSTRAINT c CHECK (x > 0);"})
    monkeypatch.setattr(doctor.shutil, "which", lambda _: "/usr/bin/psql")
    monkeypatch.setattr(doctor, "_fetch_live_constraints", lambda _: {("k.t", "c")})
    result = doctor.check_constraint_drift("postgresql://x/y", root)
    assert result.status == doctor.Status.PASS


def test_constraint_drift_ignores_absent_table(doctor, monkeypatch, tmp_path):
    """A table missing entirely is schema_migrations' failure, not this one."""
    root = _migrations(tmp_path, {
        "010_x.sql": "ALTER TABLE gone.t ADD CONSTRAINT c CHECK (x > 0);"})
    monkeypatch.setattr(doctor.shutil, "which", lambda _: "/usr/bin/psql")
    monkeypatch.setattr(doctor, "_fetch_live_constraints", lambda _: {("k.other", "z")})
    result = doctor.check_constraint_drift("postgresql://x/y", root)
    assert result.status == doctor.Status.PASS


def test_constraint_drift_skips_without_psql(doctor, monkeypatch, tmp_path):
    root = _migrations(tmp_path, {
        "010_x.sql": "ALTER TABLE k.t ADD CONSTRAINT c CHECK (x > 0);"})
    monkeypatch.setattr(doctor.shutil, "which", lambda _: None)
    result = doctor.check_constraint_drift("postgresql://x/y", root)
    assert result.status == doctor.Status.SKIP


# --- migration_checksum_drift -----------------------------------------------
# The cause detector under column_drift / constraint_drift. Those know one DDL
# family each; this one only compares bytes, so it catches any post-apply edit.
# The load-bearing behaviour is what it does with a NULL checksum: a row applied
# before content anchoring existed must read as UNVERIFIABLE, never as agreeing.
# Back-filling those from source would re-create the exact false-green that let
# migration 034 run 3-of-4 CHECK constraints in production for three months.

def test_file_checksum_is_sha256_of_bytes(doctor, tmp_path):
    import hashlib
    p = tmp_path / "010_x.sql"
    p.write_bytes(b"SELECT 1;\n")
    assert doctor._file_checksum(p) == hashlib.sha256(b"SELECT 1;\n").hexdigest()


def test_file_checksum_sees_line_ending_change(doctor, tmp_path):
    """Bytes, not decoded text — psql would feed the server different bytes."""
    a = tmp_path / "a.sql"
    b = tmp_path / "b.sql"
    a.write_bytes(b"SELECT 1;\n")
    b.write_bytes(b"SELECT 1;\r\n")
    assert doctor._file_checksum(a) != doctor._file_checksum(b)


def test_checksum_drift_flags_edited_file(doctor, tmp_path):
    """A recorded checksum disagreeing with the file is the 034 failure mode."""
    root = _migrations(tmp_path, {"010_x.sql": "SELECT 1;"})
    mig = root / "db" / "postgres" / "migrations"
    mismatches, unverifiable = doctor._checksum_drift({10: "b" * 64}, mig)
    assert len(mismatches) == 1
    assert "010_x.sql" in mismatches[0]
    assert "changed after it was applied" in mismatches[0]
    assert unverifiable == []


def test_checksum_drift_accepts_matching_file(doctor, tmp_path):
    root = _migrations(tmp_path, {"010_x.sql": "SELECT 1;"})
    mig = root / "db" / "postgres" / "migrations"
    actual = doctor._file_checksum(mig / "010_x.sql")
    assert doctor._checksum_drift({10: actual}, mig) == ([], [])


def test_checksum_drift_reports_null_as_unverifiable_not_mismatch(doctor, tmp_path):
    """NULL is 'unknowable', not 'wrong' — and never silently treated as OK."""
    root = _migrations(tmp_path, {"010_x.sql": "SELECT 1;"})
    mig = root / "db" / "postgres" / "migrations"
    mismatches, unverifiable = doctor._checksum_drift({10: None}, mig)
    assert mismatches == []
    assert unverifiable == [10]


def test_checksum_drift_ignores_version_with_no_source_file(doctor, tmp_path):
    """schema_migrations already reports that as 'unexpected'; no double-count."""
    root = _migrations(tmp_path, {"010_x.sql": "SELECT 1;"})
    mig = root / "db" / "postgres" / "migrations"
    mismatches, _ = doctor._checksum_drift({99: "c" * 64}, mig)
    assert mismatches == []


def test_checksum_check_fails_on_mismatch(doctor, monkeypatch, tmp_path):
    root = _migrations(tmp_path, {"010_x.sql": "SELECT 1;"})
    monkeypatch.setattr(doctor.shutil, "which", lambda _: "/usr/bin/psql")
    monkeypatch.setattr(doctor, "_query_applied_checksums", lambda _: {10: "d" * 64})
    result = doctor.check_migration_checksum_drift("postgresql://x/y", root)
    assert result.status == doctor.Status.FAIL
    # The remedy must not be "edit the checksum to match" — that would launder
    # the drift into agreement while leaving the deployed schema wrong.
    assert "Do NOT edit the checksum" in result.detail
    assert "NEW forward migration" in result.detail


def test_checksum_check_passes_with_unverifiable_note(doctor, monkeypatch, tmp_path):
    root = _migrations(tmp_path, {"010_x.sql": "SELECT 1;"})
    monkeypatch.setattr(doctor.shutil, "which", lambda _: "/usr/bin/psql")
    monkeypatch.setattr(doctor, "_query_applied_checksums", lambda _: {10: None})
    result = doctor.check_migration_checksum_drift("postgresql://x/y", root)
    assert result.status == doctor.Status.PASS
    assert "unverifiable" in result.message
    assert "never back-fill" in result.detail


def test_checksum_check_skips_before_migration_062(doctor, monkeypatch, tmp_path):
    """A DB without the column is pre-062, not broken."""
    root = _migrations(tmp_path, {"010_x.sql": "SELECT 1;"})
    monkeypatch.setattr(doctor.shutil, "which", lambda _: "/usr/bin/psql")
    monkeypatch.setattr(doctor, "_query_applied_checksums", lambda _: {})
    result = doctor.check_migration_checksum_drift("postgresql://x/y", root)
    assert result.status == doctor.Status.SKIP


# --- schema_attestation -----------------------------------------------------
# The federation primitive: two principals compare digests computed over their
# own registries. No shared root, no central registry, neither answer
# authoritative. The property that must hold is that coverage travels WITH the
# digest — equal digests over unverifiable rows are a weak claim, and callers
# have to be able to tell.

def test_attestation_digest_is_stable(doctor):
    a = doctor.schema_attestation({1: "x", 2: "y"}, {1: "a" * 64, 2: "b" * 64})
    b = doctor.schema_attestation({2: "y", 1: "x"}, {2: "b" * 64, 1: "a" * 64})
    assert a["digest"] == b["digest"], "digest must not depend on dict order"


def test_attestation_digest_differs_on_content_not_just_name(doctor):
    """Same versions, same names, different SQL — must NOT attest as equal."""
    peer_a = doctor.schema_attestation({1: "x"}, {1: "a" * 64})
    peer_b = doctor.schema_attestation({1: "x"}, {1: "b" * 64})
    assert peer_a["digest"] != peer_b["digest"]


def test_attestation_marks_partial_coverage(doctor):
    partial = doctor.schema_attestation({1: "x", 2: "y"}, {1: "a" * 64, 2: None})
    assert partial["fully_anchored"] is False
    assert partial["verified"] == 1
    assert partial["unverifiable"] == 1


def test_attestation_full_coverage_is_marked(doctor):
    full = doctor.schema_attestation({1: "x"}, {1: "a" * 64})
    assert full["fully_anchored"] is True
    assert full["unverifiable"] == 0


def test_attestation_empty_registry_is_not_fully_anchored(doctor):
    """An empty DB must not read as 'fully verified' — it has verified nothing."""
    empty = doctor.schema_attestation({}, {})
    assert empty["fully_anchored"] is False


def test_checksum_query_raises_on_real_failure(doctor, monkeypatch):
    """A dropped connection must NOT look like 'pre-062, nothing to verify'.

    Both states would otherwise return {}, and every caller reads {} as "no
    content anchoring here" — so an unreachable DB would silently disarm the
    drift guard and report clean.
    """
    class _Proc:
        returncode = 1
        stdout = ""
        stderr = "could not connect to server: Connection refused"

    monkeypatch.setattr(doctor.subprocess, "run", lambda *a, **k: _Proc())
    with pytest.raises(RuntimeError):
        doctor._query_applied_checksums("postgresql://x/y")


def test_checksum_query_returns_empty_only_for_missing_column(doctor, monkeypatch):
    class _Proc:
        returncode = 1
        stdout = ""
        stderr = 'ERROR:  column "checksum" does not exist'

    monkeypatch.setattr(doctor.subprocess, "run", lambda *a, **k: _Proc())
    assert doctor._query_applied_checksums("postgresql://x/y") == {}


def test_checksum_check_fails_loudly_when_state_unknown(doctor, monkeypatch, tmp_path):
    """'Could not check' must render as FAIL, never as SKIP or PASS."""
    root = _migrations(tmp_path, {"010_x.sql": "SELECT 1;"})
    monkeypatch.setattr(doctor.shutil, "which", lambda _: "/usr/bin/psql")

    def _boom(_):
        raise RuntimeError("connection refused")

    monkeypatch.setattr(doctor, "_query_applied_checksums", _boom)
    result = doctor.check_migration_checksum_drift("postgresql://x/y", root)
    assert result.status == doctor.Status.FAIL
    assert "UNKNOWN" in result.message


# ---------------------------------------------------------------------------
# resident_checkin_stale — event-driven exemption
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _no_live_residents_probe(monkeypatch, doctor):
    """Keep the resident checks hermetic.

    `check_resident_checkin_stale` asks the running server which residents are
    event-driven. Left unpatched, a unit test makes a real call to
    127.0.0.1:8767 and behaves differently depending on whether a server
    happens to be up on the developer's machine — passing locally, exercising a
    different path in CI. Default to the fail-open answer; the tests that care
    override it.
    """
    # Stash the real function first: the tests that exercise IT must not get
    # the stub. Two of them expect set() and so passed vacuously against the
    # stub before this was added.
    monkeypatch.setattr(
        doctor, "_event_driven_labels_real", doctor._event_driven_labels, raising=False
    )
    monkeypatch.setattr(doctor, "_event_driven_labels", lambda *a, **k: set())


class TestEventDrivenExemption:
    """An event-driven resident has no cadence to be stale against.

    Measured 2026-08-15: `/v1/residents` reported Watcher event_driven, healthy,
    110s silent against a 48h threshold, while this check warned it had been
    silent 98min — two surfaces disagreeing about one agent at one instant.
    """

    def _sql(self, doctor, monkeypatch) -> str:
        captured = {}

        def capture(db_url, sql):
            captured["sql"] = sql
            return ("0", "0", "")

        monkeypatch.setattr(doctor, "_psql_row", capture)
        doctor.check_resident_checkin_stale("postgresql://x/y")
        return captured["sql"]

    def test_event_driven_label_is_excluded_from_the_stale_predicate(self, doctor, monkeypatch):
        """Asserted at the SQL level because the gate IS the SQL — a mocked row
        count would pass whether or not the predicate survived an edit."""
        monkeypatch.setattr(doctor, "_event_driven_labels", lambda *a, **k: {"Watcher"})
        assert "AND label NOT IN ('Watcher')" in self._sql(doctor, monkeypatch)

    def test_timer_residents_are_still_judged(self, doctor, monkeypatch):
        """The founding incident — Sentinel silent 24h, 2026-07-29 — must still
        fire. An exemption that quietly widened to every resident would remove
        the only check built to catch a resident hiding behind healthy peers."""
        monkeypatch.setattr(doctor, "_event_driven_labels", lambda *a, **k: {"Watcher"})
        sql = self._sql(doctor, monkeypatch)
        assert "'Sentinel'" not in sql
        # The envelope predicate itself is untouched.
        assert "greatest(med_gap * 6, p95_gap * 2, 1800)" in sql

    def test_no_exemption_clause_when_server_reports_none(self, doctor, monkeypatch):
        monkeypatch.setattr(doctor, "_event_driven_labels", lambda *a, **k: set())
        assert "NOT IN (" not in self._sql(doctor, monkeypatch)

    def test_labels_are_quoted_against_injection(self, doctor, monkeypatch):
        """Labels arrive over HTTP and are interpolated into SQL."""
        monkeypatch.setattr(
            doctor, "_event_driven_labels", lambda *a, **k: {"O'Brien"}
        )
        assert "'O''Brien'" in self._sql(doctor, monkeypatch)

    def test_exemption_is_stated_in_the_message(self, doctor, monkeypatch):
        """A silent exemption is how a check quietly stops covering something."""
        monkeypatch.setattr(doctor, "_event_driven_labels", lambda *a, **k: {"Watcher"})
        monkeypatch.setattr(doctor, "_psql_row", lambda *a: ("5", "0", ""))
        result = doctor.check_resident_checkin_stale("postgresql://x/y")
        assert result.status is doctor.Status.PASS
        assert "1 event-driven, exempt: Watcher" in result.message


class TestEventDrivenLabelsFailsOpen:
    """A doctor that goes quiet because the server it monitors is unreachable
    would be the worst possible failure mode for this check."""

    def test_unreachable_server_yields_no_exemption(self, doctor, monkeypatch):
        def boom(*a, **k):
            raise OSError("connection refused")

        monkeypatch.setattr(doctor.urllib.request, "urlopen", boom)
        assert doctor._event_driven_labels_real() == set()

    def test_malformed_payload_yields_no_exemption(self, doctor, monkeypatch):
        class FakeResp:
            status = 200

            def read(self):
                return b"not json"

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

        monkeypatch.setattr(doctor.urllib.request, "urlopen", lambda *a, **k: FakeResp())
        assert doctor._event_driven_labels_real() == set()

    def test_only_event_driven_true_is_collected(self, doctor, monkeypatch):
        payload = {
            "residents": [
                {"label": "Watcher", "event_driven": True},
                {"label": "Sentinel", "event_driven": False},
                {"label": "Vigil"},
            ]
        }

        class FakeResp:
            status = 200

            def read(self):
                return json.dumps(payload).encode()

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

        monkeypatch.setattr(doctor.urllib.request, "urlopen", lambda *a, **k: FakeResp())
        assert doctor._event_driven_labels_real() == {"Watcher"}


# ---------------------------------------------------------------------------
# check_class_anchors_fresh
#
# Before 2026-08-19 this check iterated DELTA_NORM_MAX_BY_CLASS alone while its
# docstring claimed to cover HEALTHY_OPERATING_POINT_BY_CLASS too, and it had no
# floor on `age`. The tests below pin the three states it used to swallow:
# future-dated (passed forever), unparseable (silently skipped), and the two
# tables desyncing (invisible). The tables are co-generated by one
# calibrate_class_conditional.py run, which is what lets one date describe both.
# ---------------------------------------------------------------------------

from types import SimpleNamespace  # noqa: E402


def _anchor(measured_on: str, provenance: str = "measured"):
    return SimpleNamespace(provenance=provenance, measured_on=measured_on)


@pytest.fixture
def anchor_tables(monkeypatch):
    """Patch both anchor tables; yields a setter taking (delta, healthy_keys)."""
    import config.governance_config as gc

    def _set(delta: dict, healthy_keys=None):
        keys = delta.keys() if healthy_keys is None else healthy_keys
        monkeypatch.setattr(gc, "DELTA_NORM_MAX_BY_CLASS", delta, raising=True)
        monkeypatch.setattr(
            gc, "HEALTHY_OPERATING_POINT_BY_CLASS",
            {k: (0.7, 0.7, 0.2) for k in keys}, raising=True,
        )
    return _set


def _today_minus(days: int) -> str:
    from datetime import datetime, timedelta, timezone
    return (datetime.now(timezone.utc) - timedelta(days=days)).strftime("%Y-%m-%d")


def test_class_anchors_fresh_passes_when_all_recent(doctor, anchor_tables):
    anchor_tables({"embodied": _anchor(_today_minus(10))})
    r = doctor.check_class_anchors_fresh(REPO_ROOT)
    assert r.status is doctor.Status.PASS
    assert "both tables" in r.message


def test_class_anchors_fresh_warns_when_stale(doctor, anchor_tables):
    anchor_tables({"default": _anchor(_today_minus(123))})
    r = doctor.check_class_anchors_fresh(REPO_ROOT)
    assert r.status is doctor.Status.WARN
    assert "stale" in r.message
    # Remediation must not be a bare command: regenerating drops sub-n-min
    # classes, which for `default` is a deletion rather than a refresh.
    assert "deletion" in r.detail


def test_class_anchors_fresh_flags_future_dated(doctor, anchor_tables):
    """A future date used to satisfy `age > stale_days` forever, reading as fresh."""
    anchor_tables({"embodied": _anchor(_today_minus(-400))})
    r = doctor.check_class_anchors_fresh(REPO_ROOT)
    assert r.status is doctor.Status.WARN
    assert "future-dated" in r.detail


def test_class_anchors_fresh_flags_unparseable_date(doctor, anchor_tables):
    anchor_tables({"embodied": _anchor("27-06-2026")})
    r = doctor.check_class_anchors_fresh(REPO_ROOT)
    assert r.status is doctor.Status.WARN
    assert "undated/unparseable" in r.detail


def test_class_anchors_fresh_flags_table_divergence(doctor, anchor_tables):
    """Co-generation invariant: a key in one table and not the other means one
    was hand-edited, so the shared date no longer describes both."""
    anchor_tables({"embodied": _anchor(_today_minus(5))},
                  healthy_keys=["embodied", "resident_persistent"])
    r = doctor.check_class_anchors_fresh(REPO_ROOT)
    assert r.status is doctor.Status.WARN
    assert "diverged" in r.message
    assert "resident_persistent" in r.detail


def test_class_anchors_fresh_ignores_non_measured_provenance(doctor, anchor_tables):
    anchor_tables({"embodied": _anchor(_today_minus(900), provenance="alias")})
    r = doctor.check_class_anchors_fresh(REPO_ROOT)
    assert r.status is doctor.Status.PASS


# ---------------------------------------------------------------------------
# Operator guidance must not re-assert a repaired blocker
# ---------------------------------------------------------------------------
# This text reaches HUMANS running the doctor, not just agents reading memory.
# It told operators that adjudicating a doctor finding would book the outcome
# against Sentinel's EISV, so attribution had to come first. Attribution was
# repaired — http_sentinel_adjudicate resolves the producer and 422s rather
# than mis-booking — but the warning kept shipping, telling people not to do
# work that was already unblocked. Found by an audit that measured 70 stale
# invariants across 343 examined; this was the one with a human audience.


def _doctor_source() -> str:
    return (Path(__file__).resolve().parents[1]
            / "scripts" / "dev" / "unitares_doctor.py").read_text(encoding="utf-8")


# Match the CLAIM, not one phrasing of it. The first version of this guard
# asserted only `"Attribution comes first" not in src` — and passed while the
# stale claim was still live two dozen lines away as "Fix attribution first."
# in the operator-visible detail string. A guard keyed to one wording is
# indistinguishable from no guard.
#
# Scoped to check_adjudication_feedstock, not the whole 2400-line script: a
# global phrase ban would fail a future unrelated check that legitimately
# needs these words, under a test named for this queue.
_STALE_ATTRIBUTION_CLAIM = re.compile(
    r"fix\s+attribution\s+first"
    r"|attribution\s+comes\s+first"
    r"|books?\s+it\s+against\s+SENTINEL"
    r"|attributes?\s+the\s+outcome\s+to\s+the\s+sentinel\s+substrate",
    re.I,
)


def _feedstock_check_source() -> str:
    """Just check_adjudication_feedstock — docstring AND detail string."""
    src = _doctor_source()
    body = src.split("def check_adjudication_feedstock", 1)[1]
    nxt = re.search(r"\ndef \w+", body)
    return body[: nxt.start()] if nxt else body


def test_queue_guidance_does_not_reassert_the_repaired_attribution_blocker():
    """No phrasing of the repaired blocker, in EITHER surface.

    The docstring is read by developers; the CheckResult detail is what
    render_text() prints to an operator. Fixing only the first is worse than
    fixing neither — the two then contradict each other and the reader cannot
    tell which was maintained.
    """
    hits = _STALE_ATTRIBUTION_CLAIM.findall(_feedstock_check_source())
    assert not hits, (
        f"stale attribution blocker present as {hits!r}. Attribution resolves "
        "the producer and 422s rather than mis-booking — verify "
        "src/http_routes/sentinel.py before restoring."
    )


def test_corrected_guidance_reaches_the_OPERATOR_surface_not_just_the_docstring():
    """A docstring-only fix leaves render_text() printing the old claim."""
    body = _feedstock_check_source()
    after_docstring = body.split('"""', 2)[-1]
    assert "ATTRIBUTION CONFORMANCE" in after_docstring, (
        "corrected guidance is in the docstring but not in the detail string "
        "operators actually see"
    )


def test_guidance_does_not_claim_conformance_alone_opens_the_queue():
    """Eligibility (event_type + severity) and attribution conformance are TWO
    independent gates. An earlier draft said conformance was "what actually
    gates widening", which would send an operator to provision an identity and
    expect a queue that cannot open — this check measures eligibility."""
    body = _feedstock_check_source()
    assert "ELIGIBILITY" in body and "ADJUDICABLE_EVENT_TYPES" in body


def test_guidance_does_not_import_dismissal_evidence_from_another_channel():
    """The in-queue population has never produced a dismissal. Borrowing
    watcher_finding_dismissed as reassurance is a different population on a
    different path."""
    body = _feedstock_check_source()
    assert "ZERO dismissals ever" in body
    assert "does not transfer" in body or "DIFFERENT population" in body


def test_provisioning_script_it_points_at_actually_exists_and_runs(tmp_path):
    """Assert the FILE, not the filename string, and run it HERMETICALLY.

    Two hazards in the obvious version of this test, both real:

    - Inheriting the ambient environment means an EXISTING anchor short-circuits
      to exit 0 before the dry-run branch is reached, so the test can pass
      without exercising what it claims to. Point UNITARES_DOCTOR_ANCHOR at
      tmp_path so the absent-anchor path is the one under test.
    - If --dry-run ever regresses to the apply path, an ambient run would
      contact the live governance service and WRITE THE REAL ANCHOR — a test
      that mints a fleet identity. The tmp anchor plus an unroutable URL make
      that impossible rather than unlikely.

    Asserts on OUTPUT, not just exit status: exit 0 is what a short-circuit and
    a real dry run have in common.
    """
    import subprocess
    root = Path(__file__).resolve().parents[1]
    script = root / "scripts" / "ops" / "provision_doctor_identity.py"
    assert script.exists(), "guidance points at a script that does not exist"

    env = dict(os.environ)
    env["UNITARES_DOCTOR_ANCHOR"] = str(tmp_path / "doctor.json")
    env["UNITARES_GOV_URL"] = "http://127.0.0.1:9"  # discard port; nothing listens
    env.pop("UNITARES_HTTP_API_TOKEN", None)

    r = subprocess.run([sys.executable, str(script), "--dry-run"],
                       capture_output=True, text=True, timeout=60, env=env)
    assert r.returncode == 0, (
        f"the documented first step fails: {(r.stderr or r.stdout).strip()[:200]}"
    )
    assert "Dry run" in r.stdout, (
        "exited 0 without reaching the dry-run branch — a short-circuit and a "
        f"real dry run are indistinguishable by exit code alone. stdout={r.stdout!r}"
    )
    assert not (tmp_path / "doctor.json").exists(), (
        "--dry-run wrote an anchor; it must change nothing"
    )
