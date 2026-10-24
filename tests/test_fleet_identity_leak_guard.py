"""The fleet-identity guard also refuses the operator's own domain.

On 2026-09-25 the passkey relying party fell back to the operator's domain in
shipped source, which no browser would accept for any other install. These
pin that the guard catches that shape and keeps its documented exemptions.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path
import sys

import pytest

_GUARD = Path(__file__).resolve().parents[1] / "scripts/dev/check_fleet_identity_leak.py"
_spec = importlib.util.spec_from_file_location("check_fleet_identity_leak", _GUARD)
guard = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(guard)


def _scan(tmp_path, source: str) -> list[str]:
    path = tmp_path / "shipped.py"
    path.write_text(source)
    return guard.scan_file(path)


def test_domain_fallback_in_a_literal_is_flagged(tmp_path):
    hits = _scan(tmp_path, 'import os\nrp = os.getenv("X", "") or "gov.cirwel.org"\n')
    assert len(hits) == 1
    assert 'operator domain "cirwel.org"' in hits[0]


def test_domain_inside_a_url_is_flagged(tmp_path):
    hits = _scan(tmp_path, 'ORIGIN = "https://gov.cirwel.org/mcp/"\n')
    assert len(hits) == 1


def test_domain_in_comment_or_docstring_is_provenance_not_a_leak(tmp_path):
    source = (
        '"""Module docs may say gov.cirwel.org."""\n'
        "# a comment naming gov.cirwel.org is provenance\n"
        "def f():\n"
        '    """So may a docstring: gov.cirwel.org."""\n'
        "    return 1\n"
    )
    assert _scan(tmp_path, source) == []


def test_configured_hostname_is_not_flagged(tmp_path):
    assert _scan(tmp_path, 'import os\nrp = os.getenv("UNITARES_DASHBOARD_RP_ID", "")\n') == []


def test_shipped_tree_carries_no_operator_domain():
    repo = Path(__file__).resolve().parents[1]
    hits = []
    for root in guard.DEFAULT_PATHS:
        for path in sorted((repo / root).rglob("*.py")):
            hits += [h for h in guard.scan_file(path) if "operator domain" in h]
    assert hits == []


def test_name_exemptions_do_not_exempt_the_domain(monkeypatch):
    # NOT_IDENTITIES / KNOWN_COUPLINGS exist for resident-name homonyms and
    # deferred couplings. A domain hit in those files must still fail.
    # KNOWN_COUPLINGS is empty since 2026-09-27, so the coupled case uses a
    # fixture entry: the rule must hold for whatever is added there next.
    exempt = next(iter(guard.NOT_IDENTITIES))
    coupled = "src/example_known_coupling.py"
    monkeypatch.setitem(guard.KNOWN_COUPLINGS, coupled, "test fixture")
    domain_hit = f'  {exempt}:1: hardcoded operator domain "cirwel.org" in a string literal'
    name_hit = f'  {exempt}:2: hardcoded fleet identity "Sentinel" in a string literal'

    assert guard.triage(exempt, [domain_hit, name_hit]) == ([domain_hit], [])
    failing, deferred = guard.triage(coupled, [domain_hit, name_hit])
    assert failing == [domain_hit] and deferred == [name_hit]
    assert guard.triage("src/other.py", [name_hit]) == ([name_hit], [])


@pytest.mark.parametrize("checkout", ["checkout", "tests/checkout", "tests"])
def test_main_scans_runtime_regardless_of_checkout_directory(
    tmp_path, monkeypatch, capsys, checkout
):
    repo = tmp_path / checkout
    source = repo / "src" / "runtime.py"
    source.parent.mkdir(parents=True)
    source.write_text('ORIGIN = "https://gov.cirwel.org/mcp/"\n')
    monkeypatch.setattr(guard, "REPO_ROOT", repo)
    monkeypatch.setattr(sys, "argv", [str(_GUARD), "--paths", "src", "--no-served"])

    assert guard.main() == 1
    output = capsys.readouterr().out
    assert 'src/runtime.py:1: hardcoded operator domain "cirwel.org"' in output


def test_main_exempts_only_repository_test_paths(tmp_path, monkeypatch, capsys):
    repo = tmp_path / "tests" / "checkout"
    for relative, contents in {
        "agents/example/agent.py": 'ORIGIN = ""\n',
        "agents/example/tests/fixture.py": 'ORIGIN = "https://gov.cirwel.org"\n',
        "agents/example/test_agent.py": 'ORIGIN = "https://gov.cirwel.org"\n',
    }.items():
        path = repo / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(contents)
    monkeypatch.setattr(guard, "REPO_ROOT", repo)
    monkeypatch.setattr(sys, "argv", [str(_GUARD), "--paths", "agents", "--no-served"])

    assert guard.main() == 0
    assert "1 file(s) scanned" in capsys.readouterr().out
