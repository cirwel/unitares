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
    "CHECK ((surface_id ~ '^(file://|dialectic:/|resident:/|capture:/|td:/)'::text))"
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
      @canonical_schemes ~w(file dialectic resident capture td)
      defp dispatch("file://" <> rest), do: rest
      defp dispatch("dialectic:/" <> rest), do: rest
      defp dispatch("resident:/" <> rest), do: rest
      defp dispatch("capture:/" <> rest), do: rest
      defp dispatch("td:/" <> rest), do: rest
    end
    ''')
    result = doctor.check_elixir_scheme_grammar_lint("postgresql://example", tmp_path)
    assert result.status == doctor.Status.PASS, result.message
    for scheme in ("file", "dialectic", "resident", "capture", "td"):
        assert scheme in result.message


def test_grammar_lint_fails_when_dispatch_arm_not_in_grammar(doctor, monkeypatch, tmp_path):
    """Inverse drift: Elixir ships a dispatch arm for `foo:/` but the
    migration-026 CHECK doesn't allow it. Every acquire would 422 in prod."""
    monkeypatch.setattr(doctor.shutil, "which", lambda _: "/usr/bin/psql")
    monkeypatch.setattr(doctor.subprocess, "run",
                        lambda *a, **kw: _Proc(returncode=0, stdout=_GRAMMAR_CHECK_DEF))
    _write_canonicalize(tmp_path, '''defmodule Canonicalize do
      @canonical_schemes ~w(file dialectic resident capture td)
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
      @canonical_schemes ~w(file dialectic resident capture td bar)
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
