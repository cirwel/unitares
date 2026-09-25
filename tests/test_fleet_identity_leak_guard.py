"""The fleet-identity guard also refuses the operator's own domain.

On 2026-09-25 the passkey relying party fell back to the operator's domain in
shipped source, which no browser would accept for any other install. These
pin that the guard catches that shape and keeps its documented exemptions.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

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
