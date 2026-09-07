"""The adjudication queue can starve while every liveness check reads green.

`finding_producer_live` asks whether producers are alive; `producer_never_
reported` asks whether they were ever born. Both are answered by the finding
stream itself. The queue does not consume that stream — it consumes a narrow
slice of it (two event types at two severities), so a producer can be loud,
healthy, and contribute nothing, and no liveness signal will say so.

Live state 2026-08-10, which is what prompted the check: 280 findings in 7 days
across 9 producers, 0 queue-eligible. Sentinel alone emitted 203 of them (136 in
one day), all `medium`. The invisibility is the defect.

⛔CORRECTION 2026-08-19: this docstring used to say #1443/#1444/#1459 "removed
the condition" and that the zero was therefore permanently FAIR. False. A real
forced release fired 2026-08-10 23:46:25 on `resident:/steward_eisv_sync` and
was adjudicated 2026-08-13. The fixes made the condition RARE, not absent, so
the lever stays. Which of the three causes a dry window has — condition gone,
queue drained, or alarm path broken — is answered by `forced_release_transform`
(see tests/test_forced_release_transform.py), not by this check.
"""

from __future__ import annotations

import importlib.util
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


def _rows(doctor, monkeypatch, rows):
    monkeypatch.setattr(doctor, "_psql_rows", lambda *_a, **_k: rows)


# --- the mirror must not desync from the queue's real definition ---

def test_mirrored_constants_match_the_queue_source(doctor):
    """The doctor duplicates the queue definition on purpose (it runs against a
    deployed DB from a possibly-undeployed checkout, so importing server code
    would measure the wrong thing). Duplication is only safe if it is pinned."""
    source = (REPO_ROOT / "src" / "http_routes" / "sentinel.py").read_text()

    types_match = re.search(
        r"_SENTINEL_FINDING_EVENT_TYPES\s*=\s*\(([^)]*)\)", source
    )
    assert types_match, "could not find _SENTINEL_FINDING_EVENT_TYPES in http_routes/sentinel.py"
    source_types = tuple(re.findall(r'"([^"]+)"', types_match.group(1)))
    assert source_types == doctor.ADJUDICABLE_EVENT_TYPES, (
        f"queue event types changed in http_routes/sentinel.py ({source_types}) but the "
        f"doctor mirror still says {doctor.ADJUDICABLE_EVENT_TYPES}"
    )

    sev_match = re.search(
        r"_SENTINEL_BACKLOG_DEFAULT_SEVERITIES\s*=\s*frozenset\(\{([^}]*)\}\)",
        source,
    )
    assert sev_match, "could not find _SENTINEL_BACKLOG_DEFAULT_SEVERITIES"
    source_sevs = set(re.findall(r'"([^"]+)"', sev_match.group(1)))
    assert source_sevs == set(doctor.ADJUDICABLE_SEVERITIES), (
        f"queue severities changed ({source_sevs}) but the doctor mirror "
        f"still says {set(doctor.ADJUDICABLE_SEVERITIES)}"
    )


# --- the condition this exists to catch ---

def test_warns_when_producers_are_loud_but_nothing_is_eligible(doctor, monkeypatch):
    """The live 2026-08-10 shape."""
    _rows(doctor, monkeypatch, [
        ["sentinel_alarm_finding", "132", "0", "0.1"],
        ["sentinel_finding", "71", "0", "0.5"],
        ["doctor_check_finding", "38", "0", "0.2"],
    ])
    r = doctor.check_adjudication_feedstock("postgresql://x/y")
    assert r.status is doctor.Status.WARN
    assert "DRY" in r.message
    assert "241" in r.message           # total across producers
    assert "0 eligible" in r.message


def test_warning_reports_per_producer_coverage(doctor, monkeypatch):
    """The federation payload: which producers can reach the queue at all.
    A single boolean would hide that 8 of 10 are structurally excluded."""
    _rows(doctor, monkeypatch, [
        ["sentinel_alarm_finding", "132", "0", "0.1"],
        ["doctor_check_finding", "38", "0", "0.2"],
    ])
    r = doctor.check_adjudication_feedstock("postgresql://x/y")
    assert "sentinel_alarm_finding=0/132" in r.detail
    assert "doctor_check_finding=0/38" in r.detail


def test_warning_forbids_the_tempting_wrong_fix(doctor, monkeypatch):
    """Widening the queue books another producer's finding against Sentinel's
    EISV. The warning has to say so or it invites the regression it warns of."""
    _rows(doctor, monkeypatch, [["sentinel_finding", "80", "0", "0.1"]])
    r = doctor.check_adjudication_feedstock("postgresql://x/y")
    assert "attribution" in r.detail.lower()
    assert "widen" in r.detail.lower()


# --- must not cry wolf ---

def test_passes_when_something_is_eligible(doctor, monkeypatch):
    """⛔Fixture updated by #2086, and the change is deliberate — read before
    'restoring' it. It used to pair an eligible sentinel_alarm_finding with
    `doctor_check_finding 0/38`, which asserted the POOLED rule: one family
    carrying the sum makes the whole check PASS. That rule is what made this
    check self-masking once doctor findings became eligible, so the verdict is
    now per-family and a 0/38 doctor family is a WARN, not background.

    That shape is also unreachable in production: the doctor layer emits
    `warning` and `critical` only, and the queue admits both — so a loud doctor
    family cannot sit at 0 eligible. The realistic 'nothing to cry about' shape
    is every loud family contributing something, which is what this now pins.
    The uneven case has its own test below.
    """
    _rows(doctor, monkeypatch, [
        ["sentinel_alarm_finding", "132", "4", "0.1"],
        ["doctor_check_finding", "38", "38", "0.2"],
    ])
    r = doctor.check_adjudication_feedstock("postgresql://x/y")
    assert r.status is doctor.Status.PASS
    assert "42/170" in r.message


def test_quiet_fleet_is_not_a_dry_queue(doctor, monkeypatch):
    """Below the volume floor, "0 eligible" is indistinguishable from "quiet
    week" — reporting it as dry would train the operator to ignore this."""
    _rows(doctor, monkeypatch, [["sentinel_finding", "3", "0", "1.0"]])
    r = doctor.check_adjudication_feedstock("postgresql://x/y")
    assert r.status is doctor.Status.PASS
    assert "too few" in r.message


def test_no_findings_defers_to_the_liveness_check(doctor, monkeypatch):
    """Zero producers is finding_producer_live's question. Answering it here
    too would double-report one condition as two independent failures."""
    _rows(doctor, monkeypatch, [])
    r = doctor.check_adjudication_feedstock("postgresql://x/y")
    assert r.status is doctor.Status.SKIP


def test_unqueryable_db_skips_rather_than_fails(doctor, monkeypatch):
    _rows(doctor, monkeypatch, None)
    r = doctor.check_adjudication_feedstock("postgresql://x/y")
    assert r.status is doctor.Status.SKIP


# --- wiring ---

def test_check_is_registered(doctor, tmp_path):
    checks = doctor.build_checks(REPO_ROOT, "postgresql://x/y")
    names = [c.name for c in checks]
    assert "adjudication_feedstock" in names
    assert dict((c.name, c.mode) for c in checks)["adjudication_feedstock"] == "operator"


# --- #2086: the per-family override must be MIRRORED *and* APPLIED ---

def test_per_family_override_mirrors_the_queue_source(doctor):
    """The global-severity mirror above was pinned; the per-family one was not,
    so the queue could widen a family without the doctor noticing."""
    source = (REPO_ROOT / "src" / "http_routes" / "sentinel.py").read_text()

    marker = "_ADJUDICABLE_SEVERITIES_BY_EVENT_TYPE = {"
    assert marker in source, "could not find _ADJUDICABLE_SEVERITIES_BY_EVENT_TYPE"
    # Cut at the dict's own closing brace (column 0) — a non-greedy `.*?\}`
    # stops inside `frozenset({...})` and silently yields an empty map.
    block = source.split(marker, 1)[1].split("\n}", 1)[0]

    source_map = {
        k: set(re.findall(r'"([^"]+)"', v))
        for k, v in re.findall(
            r'"([^"]+)":\s*frozenset\(\{([^}]*)\}\)', block
        )
    }
    assert source_map, f"parsed no entries from the queue's map: {block!r}"
    mirror = {
        k: set(v) for k, v in doctor.ADJUDICABLE_SEVERITIES_BY_EVENT_TYPE.items()
    }
    assert source_map == mirror, (
        f"per-family queue severities changed in http_routes/sentinel.py "
        f"({source_map}) but the doctor mirror still says {mirror}"
    )


def test_per_family_override_reaches_the_query(doctor):
    """⛔The regression this file exists for after #2086.

    The override was defined, documented and mirror-tested — and never used by
    the query, which applied ADJUDICABLE_SEVERITIES globally. Every
    doctor_check_finding (that family emits `warning` and nothing else) was
    therefore counted ineligible, so the check reported the queue DRY at
    `0 eligible` while the live server was admitting all of them. Asserting the
    constant matches the source does NOT catch that; only asserting it reaches
    the predicate does.
    """
    sql = doctor._adjudicable_predicate_sql()

    assert "'doctor_check_finding'" in sql and "'warning'" in sql, (
        "doctor_check_finding must be admitted at `warning` — it emits nothing "
        f"else, so without this the family is silently inert. Got: {sql}"
    )

    # ...and the override must not leak to families the queue holds narrower.
    sentinel_clause = next(
        c for c in sql.split(" OR ") if "'sentinel_finding'" in c
    )
    assert "'warning'" not in sentinel_clause, (
        f"sentinel_finding must stay at {doctor.ADJUDICABLE_SEVERITIES} — its "
        f"`medium` alone is ~834 distinct fingerprints/30d. Got: {sentinel_clause}"
    )

    for event_type in doctor.ADJUDICABLE_EVENT_TYPES:
        assert f"'{event_type}'" in sql, f"{event_type} missing from predicate"


def test_rule_text_matches_the_predicate(doctor):
    """The WARN detail told the operator the eligibility rule. When it printed
    the global severities it was reporting a rule the query did not use — the
    operator-facing half of the same bug."""
    text = doctor._adjudicable_rule_text()
    assert "doctor_check_finding" in text and "warning" in text
    for event_type in doctor.ADJUDICABLE_EVENT_TYPES:
        assert event_type in text


# --- #2086: the fix must not make the check self-masking ---

def test_healthy_doctor_family_does_not_mask_a_starved_sentinel(doctor, monkeypatch):
    """⛔The regression the council caught in the #2086 fix itself.

    Once doctor_check_finding is (correctly) eligible at `warning`, the doctor
    feeds its OWN family: doctor_findings.py re-emits any operator-mode WARN —
    including this check's — as a doctor_check_finding at `warning`. Pooling
    `eligible` across families therefore lets one WARN clear itself on the next
    tick, and routine doctor noise holds the pooled sum above zero forever.

    This is the exact 2026-08-10 shape: Sentinel loud, every row below the
    severity gate, not one adjudicable — while the doctor family looks fine.
    Pooled logic reads PASS. It must WARN.
    """
    _rows(doctor, monkeypatch, [
        ["doctor_check_finding", "66", "66", "0.1"],   # self-fed, healthy
        ["sentinel_finding", "40", "0", "0.2"],        # loud, all `medium`
    ])
    r = doctor.check_adjudication_feedstock("postgresql://x")

    assert r.status is doctor.Status.WARN, (
        f"a starved sentinel_finding must WARN even while another family "
        f"carries the pooled total; got {r.status} — {r.message}"
    )
    assert "sentinel_finding" in r.message, (
        f"the WARN must name the starved family, not just report a total: {r.message}"
    )


def test_starved_family_below_the_volume_floor_is_not_called_starved(doctor, monkeypatch):
    """A family with a handful of rows cannot be distinguished from a quiet
    one. Only volume makes 'loud but unadjudicable' a claim worth making."""
    _rows(doctor, monkeypatch, [
        ["doctor_check_finding", "66", "66", "0.1"],
        ["sentinel_alarm_finding", "3", "0", "0.2"],   # too few to call
    ])
    r = doctor.check_adjudication_feedstock("postgresql://x")
    assert r.status is doctor.Status.PASS, (
        f"3 findings is not evidence of starvation: {r.status} — {r.message}"
    )


def test_non_adjudicable_producers_are_not_called_starved(doctor, monkeypatch):
    """lumen_checkin_finding and friends are not in ADJUDICABLE_EVENT_TYPES at
    all — they are out of the queue BY DESIGN, not starved. Flagging them would
    manufacture a permanent WARN nobody can act on."""
    _rows(doctor, monkeypatch, [
        ["doctor_check_finding", "66", "66", "0.1"],
        ["lumen_checkin_finding", "25", "0", "0.2"],
    ])
    r = doctor.check_adjudication_feedstock("postgresql://x")
    assert r.status is doctor.Status.PASS, (
        f"a non-adjudicable producer is not a starved one: {r.status} — {r.message}"
    )


def test_all_families_starved_still_reports_the_dry_queue(doctor, monkeypatch):
    """The original condition must survive the per-family rework."""
    _rows(doctor, monkeypatch, [
        ["sentinel_finding", "40", "0", "0.2"],
        ["doctor_check_finding", "30", "0", "0.1"],
    ])
    r = doctor.check_adjudication_feedstock("postgresql://x")
    assert r.status is doctor.Status.WARN
    assert "DRY" in r.message, f"expected the dry-queue wording: {r.message}"
