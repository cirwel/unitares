"""The plugin's freshness checker must date skills by canonical's rule.

unitares-governance-plugin keeps its own copy of THE RULE from
src/skill_attestations.py (it cannot import unitares), and
scripts/dev/check_plugin_attestation_rule.py compares that copy with canonical.
These tests stand up throwaway plugin checkouts carrying a correct port and
the two plausible wrong ones (the plugin's pre-port "lexically newest record"
reading, and a naive "latest date across all records"), and confirm the check
passes only the correct one.
"""

from __future__ import annotations

import importlib.util
import textwrap
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
_spec = importlib.util.spec_from_file_location(
    "check_plugin_attestation_rule", REPO_ROOT / "scripts" / "dev" / "check_plugin_attestation_rule.py"
)
rule_check = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(rule_check)

_PRELUDE = textwrap.dedent('''\
    import hashlib, json
    from pathlib import Path

    def skill_text_digest(skill_md):
        return hashlib.sha256(Path(skill_md).read_bytes()).hexdigest()[:16]

    def _records(skills_dir, name):
        adir = Path(skills_dir) / ".attestations" / name
        if not adir.is_dir():
            return []
        out = []
        for path in sorted(adir.glob("*.json"), reverse=True):
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            if isinstance(data, dict) and isinstance(data.get("source_digests"), dict):
                out.append(data)
        return out

    def _latest(records):
        date = None
        for r in records:
            v = r.get("verified_date")
            if isinstance(v, str) and v and (date is None or v > date):
                date = v
        return date
''')

PORTED = _PRELUDE + textwrap.dedent('''
    def attested_date(skills_dir, name, skill_digest):
        records = _records(skills_dir, name)
        certified = [r for r in records if r.get("skill_digest") == skill_digest]
        return _latest(certified or records[:1])
''')

LEXICALLY_NEWEST = _PRELUDE + textwrap.dedent('''
    def attested_date(skills_dir, name, skill_digest):
        return _latest(_records(skills_dir, name)[:1])
''')

LATEST_OF_ALL = _PRELUDE + textwrap.dedent('''
    def attested_date(skills_dir, name, skill_digest):
        return _latest(_records(skills_dir, name))
''')


def _plugin(tmp_path: Path, checker: str | None) -> Path:
    plugin = tmp_path / "plugin"
    (plugin / "skills").mkdir(parents=True)
    if checker is not None:
        (plugin / "scripts").mkdir()
        (plugin / "scripts" / "_check_freshness.py").write_text(checker)
    return plugin


def test_a_faithful_port_agrees(tmp_path):
    status, lines = rule_check.check(_plugin(tmp_path, PORTED))
    assert status == rule_check.EXIT_OK, lines


# Orders by modification time instead of file name: agrees with canonical
# only while the two coincide, which they do not on an rsync'd mirror.
NEWEST_BY_MTIME = PORTED.replace(
    'sorted(adir.glob("*.json"), reverse=True)',
    'sorted(adir.glob("*.json"), key=lambda p: (p.stat().st_mtime_ns, p.name), reverse=True)',
)
assert NEWEST_BY_MTIME != PORTED


@pytest.mark.parametrize("checker", [LEXICALLY_NEWEST, LATEST_OF_ALL, NEWEST_BY_MTIME],
                         ids=["lexically-newest", "latest-of-all", "newest-by-mtime"])
def test_a_drifted_rule_is_reported(tmp_path, checker):
    status, lines = rule_check.check(_plugin(tmp_path, checker))
    assert status == rule_check.EXIT_DRIFT
    assert any("canonical" in line and "plugin" in line for line in lines), lines


def test_disagreement_on_a_real_synced_skill_is_named(tmp_path):
    # Only the synced skill differs: the stub agrees on every generated case.
    plugin = _plugin(tmp_path, PORTED + textwrap.dedent('''
        _ported = attested_date
        def attested_date(skills_dir, name, skill_digest):
            return "1999-01-01" if name == "real" else _ported(skills_dir, name, skill_digest)
    '''))
    (plugin / "skills" / "real").mkdir()
    (plugin / "skills" / "real" / "SKILL.md").write_text("# Real\n")
    status, lines = rule_check.check(plugin)
    assert status == rule_check.EXIT_DRIFT
    assert lines == ["synced skill real: canonical None, plugin '1999-01-01'"]


def test_a_checker_that_predates_the_port_is_reported(tmp_path):
    status, lines = rule_check.check(_plugin(tmp_path, "def latest_attestation_date(d, n):\n    return None\n"))
    assert status == rule_check.EXIT_DRIFT
    assert "predates the port" in lines[0]


def test_an_unloadable_checker_is_distinguished_from_drift(tmp_path):
    status, lines = rule_check.check(_plugin(tmp_path, "import no_such_module_xyz\n"))
    assert status == rule_check.EXIT_UNUSABLE
    assert "cannot load" in lines[0]


def test_a_checker_that_raises_is_not_reported_as_drift(tmp_path):
    # A port that crashes on some input says nothing about whether the rules
    # agree; reporting it as drift would send the operator to port the rule.
    plugin = _plugin(tmp_path, PORTED + textwrap.dedent('''
        _ported = attested_date
        def attested_date(skills_dir, name, skill_digest):
            if (Path(skills_dir) / ".attestations" / name).is_dir():
                raise RuntimeError("boom")
            return _ported(skills_dir, name, skill_digest)
    '''))
    status, lines = rule_check.check(plugin)
    assert status == rule_check.EXIT_UNUSABLE, lines
    assert lines == ["comparison raised RuntimeError: boom"]


def test_a_plugin_without_a_checker_is_not_reported_as_agreement(tmp_path):
    # "Never compared" must not read as "the rules agree", or a moved plugin
    # checker would leave the detector silently blind.
    status, lines = rule_check.check(_plugin(tmp_path, None))
    assert status == rule_check.EXIT_NOT_COMPARED
    assert "nothing compared" in lines[0]


def test_a_checker_that_imports_yaml_loads_without_pyyaml(tmp_path, monkeypatch):
    # The plugin's real checker imports yaml at module level; the compared
    # functions do not use it. A python3 without PyYAML must still compare.
    import sys
    monkeypatch.setitem(sys.modules, "yaml", None)  # makes `import yaml` fail
    status, lines = rule_check.check(_plugin(tmp_path, "import yaml\n" + PORTED))
    assert status == rule_check.EXIT_OK, lines
    assert sys.modules["yaml"] is None  # the stand-in was removed again


def test_a_rule_that_really_uses_yaml_without_pyyaml_is_not_checked(tmp_path, monkeypatch):
    import sys
    monkeypatch.setitem(sys.modules, "yaml", None)
    checker = "import yaml\n" + PORTED + textwrap.dedent('''
        _ported = attested_date
        def attested_date(skills_dir, name, skill_digest):
            yaml.safe_load("a: 1")
            return _ported(skills_dir, name, skill_digest)
    ''')
    status, lines = rule_check.check(_plugin(tmp_path, checker))
    assert status == rule_check.EXIT_UNUSABLE
    assert "PyYAML is not installed" in lines[0]


@pytest.mark.parametrize("where", ["module", "call"])
def test_a_checker_that_calls_sys_exit_is_not_reported_as_drift(tmp_path, where):
    # SystemExit is not an Exception; uncaught it would exit 1, the drift code.
    if where == "module":
        checker = PORTED + "\nimport sys\nsys.exit(1)\n"
    else:
        checker = PORTED + textwrap.dedent('''
            import sys
            def attested_date(skills_dir, name, skill_digest):
                sys.exit(1)
        ''')
    status, lines = rule_check.check(_plugin(tmp_path, checker))
    assert status == rule_check.EXIT_UNUSABLE, lines
    assert "SystemExit" in lines[0]
