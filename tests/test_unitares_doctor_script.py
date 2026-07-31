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
# Row layout is (stddev, distinct, count) per metric, in DEGENERACY_METRICS
# order: coherence, stability_index, entropy, integrity, risk_score.

def _degeneracy_row(*triples):
    return "|".join(str(v) for t in triples for v in t) + "\n"


_HEALTHY = ((0.0432, 6192, 6232), (0.0612, 833, 6232), (0.0804, 5815, 6232))


def test_signal_degeneracy_warns_on_constant_metric(doctor, monkeypatch):
    """stability_index pinned to a single value — the live 2026-07-30 case."""
    _mock_psql(doctor, monkeypatch, _degeneracy_row(
        (0.0432, 6192, 6232), ("", 1, 6232), *_HEALTHY[:3]))
    result = doctor.check_signal_degeneracy("postgresql://x/y")
    assert result.status == doctor.Status.WARN
    assert "stability_index: constant" in result.message


def test_signal_degeneracy_warns_on_collapsed_dispersion(doctor, monkeypatch):
    """coherence: 5632 distinct values but sd 0.005 — moves only in the 4th decimal."""
    _mock_psql(doctor, monkeypatch, _degeneracy_row(
        (0.005262, 5632, 6233), *_HEALTHY, (0.0612, 833, 6233)))
    result = doctor.check_signal_degeneracy("postgresql://x/y")
    assert result.status == doctor.Status.WARN
    assert "coherence" in result.message
    # many distinct values must NOT be mistaken for a healthy signal
    assert "constant" not in result.message


def test_signal_degeneracy_passes_when_all_metrics_vary(doctor, monkeypatch):
    _mock_psql(doctor, monkeypatch, _degeneracy_row(
        (0.0217, 5000, 6232), (0.1009, 491, 6232), *_HEALTHY))
    result = doctor.check_signal_degeneracy("postgresql://x/y")
    assert result.status == doctor.Status.PASS


def test_signal_degeneracy_skips_thin_data(doctor, monkeypatch):
    """A flat metric on 10 rows is small-sample, not a defect."""
    _mock_psql(doctor, monkeypatch, _degeneracy_row(
        ("", 1, 10), ("", 1, 10), ("", 1, 10), ("", 1, 10), ("", 1, 10)))
    result = doctor.check_signal_degeneracy("postgresql://x/y")
    assert result.status == doctor.Status.SKIP


def test_signal_degeneracy_skips_on_short_row(doctor, monkeypatch):
    """A truncated/malformed row must not be read as 'everything healthy'."""
    _mock_psql(doctor, monkeypatch, "0.02|100|6232\n")
    result = doctor.check_signal_degeneracy("postgresql://x/y")
    assert result.status == doctor.Status.SKIP


# --- finding_producer_live -------------------------------------------------
# Row layout is (event_type, n, hours_silent, median_gap_hours, active_days),
# one per producer. Fixtures use the real 2026-07 numbers so the calibration is
# pinned to measured cadence rather than invented ones.

def _producer_rows(*rows):
    return "".join("|".join(str(v) for v in r) + "\n" for r in rows)


_LIVE_PRODUCERS = (
    ("sentinel_finding", 402, 29.3, 0.58, 48),
    ("sentinel_alarm_finding", 598, 0.0, 0.09, 34),
    ("bridge_liveness_finding", 136, 71.4, 1.73, 13),
)


def test_finding_producer_live_warns_on_the_watcher_outage(doctor, monkeypatch):
    """The 2026-06-29 case, measured as of 07-25: 612h silent, 0.32h cadence.

    This is the check's reason for existing — a detector whose model call was
    404ing on every scan while every other liveness indicator stayed green.
    """
    _mock_psql(doctor, monkeypatch, _producer_rows(
        ("watcher_finding", 98, 612.0, 0.32, 22), *_LIVE_PRODUCERS))
    result = doctor.check_finding_producer_live("postgresql://x/y")
    assert result.status == doctor.Status.WARN
    assert "watcher_finding" in result.message
    assert "silent 25.5d" in result.message


def test_finding_producer_live_passes_when_all_report(doctor, monkeypatch):
    _mock_psql(doctor, monkeypatch, _producer_rows(
        ("watcher_finding", 99, 30.0, 0.33, 22), *_LIVE_PRODUCERS))
    result = doctor.check_finding_producer_live("postgresql://x/y")
    assert result.status == doctor.Status.PASS


def test_finding_producer_live_ignores_event_driven_producers(doctor, monkeypatch):
    """vigil fires 4x in 90d — quiet is its normal state, not a fault."""
    _mock_psql(doctor, monkeypatch, _producer_rows(
        ("vigil_finding", 4, 172.7, 13.75, 3), *_LIVE_PRODUCERS))
    result = doctor.check_finding_producer_live("postgresql://x/y")
    assert result.status == doctor.Status.PASS
    assert "vigil" not in result.message


def test_finding_producer_live_ignores_slow_cadence_producers(doctor, monkeypatch):
    """A producer that reports every ~16 days has no weekly cadence to miss."""
    _mock_psql(doctor, monkeypatch, _producer_rows(
        ("sentinel_build_finding", 30, 800.0, 396.24, 12), *_LIVE_PRODUCERS))
    result = doctor.check_finding_producer_live("postgresql://x/y")
    assert result.status == doctor.Status.PASS


def test_finding_producer_live_holds_fire_below_the_week_floor(doctor, monkeypatch):
    """10x a 1.7h cadence is 17h — far too tight to page on. The floor wins."""
    _mock_psql(doctor, monkeypatch, _producer_rows(
        ("bridge_liveness_finding", 136, 100.0, 1.73, 13),))
    result = doctor.check_finding_producer_live("postgresql://x/y")
    assert result.status == doctor.Status.PASS


def test_finding_producer_live_skips_when_nothing_is_judgeable(doctor, monkeypatch):
    _mock_psql(doctor, monkeypatch, _producer_rows(
        ("healthcheck_selftest_finding", 1, 995.0, "", 1),))
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
        ("lease_plane_health_finding", 14, 799.2, 0.50, 1), *_LIVE_PRODUCERS))
    result = doctor.check_finding_producer_live("postgresql://x/y")
    assert result.status == doctor.Status.PASS
    assert "lease_plane_health" not in result.message


def test_resident_checkin_stale_warns_on_single_dead_resident(doctor, monkeypatch):
    # The 2026-07-29 Sentinel case: one resident silent 24h while the fleet
    # aggregate stayed healthy. checkin_stream_live cannot see this.
    _mock_psql(doctor, monkeypatch,
               "5|1|Sentinel silent 632min (own median 5min)\n")
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


def test_immortal_lease_warns_on_renewed_orphan(doctor, monkeypatch):
    _mock_psql(doctor, monkeypatch,
               "2|resident:/sentinel_fleet_emit, resident:/steward\n")
    result = doctor.check_immortal_lease("postgresql://x/y")
    assert result.status == doctor.Status.WARN
    assert "sentinel_fleet_emit" in result.message
    assert "force-release" in result.detail


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
