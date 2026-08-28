"""Covers the read-only /api/automations census-snapshot endpoint.

The endpoint reads the snapshot written by `unitares-automations census --write`
(path overridable via UNITARES_AUTOMATION_CENSUS_PATH) and passes it through with
freshness metadata. It must NOT shell out and must degrade gracefully when the
snapshot is missing. Auth is exercised elsewhere; patched True here so the test
focuses on the snapshot logic.
"""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from src.http_routes import access
from src.http_api import http_automations


def _req():
    return SimpleNamespace(headers={})


@pytest.mark.asyncio
async def test_passthrough_with_freshness(tmp_path, monkeypatch):
    monkeypatch.setattr(access, "_check_http_auth", lambda *a, **k: True)
    snap = tmp_path / "census.json"
    snap.write_text(json.dumps({
        "schema": "unitares.automation_census.v1",
        "summary": {"total": 2, "by_source": {"launchd": 2}, "by_kind": {"dogfood": 1},
                    "needs_attention": ["a"], "warnings": []},
        "automations": [
            {"id": "a", "name": "A", "source": "launchd", "kind": "dogfood"},
            {"id": "b", "name": "B", "source": "launchd", "kind": "test"},
        ],
    }))
    monkeypatch.setenv("UNITARES_AUTOMATION_CENSUS_PATH", str(snap))

    resp = await http_automations(_req())
    assert resp.status_code == 200
    body = json.loads(resp.body)
    assert body["summary"]["total"] == 2
    assert len(body["automations"]) == 2
    assert body["snapshot_path"] == str(snap)
    assert body["snapshot_age_seconds"] is not None
    assert body["stale"] is False


@pytest.mark.asyncio
async def test_missing_snapshot_degrades(tmp_path, monkeypatch):
    monkeypatch.setattr(access, "_check_http_auth", lambda *a, **k: True)
    monkeypatch.setenv("UNITARES_AUTOMATION_CENSUS_PATH", str(tmp_path / "absent.json"))

    resp = await http_automations(_req())
    assert resp.status_code == 200
    body = json.loads(resp.body)
    assert body["automations"] == []
    assert body["summary"]["total"] == 0
    assert body["stale"] is True
    assert any("census" in w.lower() for w in body["warnings"])


@pytest.mark.asyncio
async def test_summary_view_omits_the_item_list_and_precomputes_ungated(tmp_path, monkeypatch):
    """?view=summary serves the Overview card without the census body.

    The card reads the summary block, `stale`, and a COUNT of ungated entries.
    The full census was ~206 KB of per-automation detail (228 items live on
    2026-08-28) for ~641 B of consumed fields — 99.7% discarded, on the DEFAULT
    page, every load. Cheap on loopback; the whole render cost over a tunnel.

    The ungated count is computed server-side precisely because counting is the
    only thing the caller ever did with those notes arrays.
    """
    monkeypatch.setattr(access, "_check_http_auth", lambda *a, **k: True)
    snap = tmp_path / "census.json"
    snap.write_text(json.dumps({
        "schema": "unitares.automation_census.v1",
        "summary": {"total": 3, "by_source": {"launchd": 3}, "by_kind": {"dogfood": 1},
                    "needs_attention": ["a"], "warnings": []},
        "automations": [
            {"id": "a", "notes": ["gate:ungated"]},
            {"id": "b", "notes": ["gate:reviewed"]},
            {"id": "c", "notes": ["gate:ungated"]},
        ],
    }))
    monkeypatch.setenv("UNITARES_AUTOMATION_CENSUS_PATH", str(snap))

    resp = await http_automations(SimpleNamespace(headers={}, query_params={"view": "summary"}))
    assert resp.status_code == 200
    body = json.loads(resp.body)

    assert body["view"] == "summary"
    assert body["summary"]["total"] == 3
    assert body["ungated"] == 2
    assert "stale" in body
    # The point of the view: the payload nobody reads does not ship.
    assert "automations" not in body


@pytest.mark.asyncio
async def test_default_response_is_unchanged_for_the_automations_tab(tmp_path, monkeypatch):
    """The tab renders every item, so the default shape must not shrink."""
    monkeypatch.setattr(access, "_check_http_auth", lambda *a, **k: True)
    snap = tmp_path / "census.json"
    snap.write_text(json.dumps({
        "schema": "unitares.automation_census.v1",
        "summary": {"total": 1, "by_source": {}, "by_kind": {}, "needs_attention": [], "warnings": []},
        "automations": [{"id": "a", "notes": ["gate:ungated"]}],
    }))
    monkeypatch.setenv("UNITARES_AUTOMATION_CENSUS_PATH", str(snap))

    for req in (SimpleNamespace(headers={}),
                SimpleNamespace(headers={}, query_params={}),
                SimpleNamespace(headers={}, query_params={"view": "nonsense"})):
        body = json.loads((await http_automations(req)).body)
        assert len(body["automations"]) == 1, "default shape must carry the items"
        assert "view" not in body


@pytest.mark.asyncio
async def test_summary_reports_unclassified_not_just_the_explicit_marker(tmp_path, monkeypatch):
    """`ungated` alone is an unfair zero; `unclassified` is the honest number.

    The Overview card exists to surface "nothing verifies this" — its own
    comment says so. It counted only an explicit `gate:ungated` note, and
    nothing writes that marker: measured 2026-08-28, 0 of 228 automations
    carried it while 221 carried no gate note at all. So the card rendered
    "0 ungated" permanently, which is the most reassuring possible way to say
    "no determination was made."

    Classification mirrors sections/automations.js::gateClass exactly, so the
    Overview card and the Automations tab cannot disagree: explicit note wins,
    then github-actions/claude are machine-gated by construction, else
    unclassified.
    """
    monkeypatch.setattr(access, "_check_http_auth", lambda *a, **k: True)
    snap = tmp_path / "census.json"
    snap.write_text(json.dumps({
        "schema": "unitares.automation_census.v1",
        "summary": {"total": 5, "by_source": {}, "by_kind": {}, "needs_attention": [], "warnings": []},
        "automations": [
            {"id": "a", "notes": ["gate:human"], "source": "launchd"},
            {"id": "b", "source": "github-actions"},          # machine by construction
            {"id": "c", "source": "claude"},                  # machine by construction
            {"id": "d", "source": "launchd"},                 # no determination
            {"id": "e", "source": "hermes", "notes": []},     # no determination
        ],
    }))
    monkeypatch.setenv("UNITARES_AUTOMATION_CENSUS_PATH", str(snap))

    body = json.loads((await http_automations(
        SimpleNamespace(headers={}, query_params={"view": "summary"}))).body)

    assert body["gates"] == {"human": 1, "machine": 2, "unclassified": 2}
    # The honest headline: two automations have no grounding determination.
    assert body["unclassified"] == 2
    # And the explicit-marker count is still 0 — which is exactly why reporting
    # it alone was misleading.
    assert body["ungated"] == 0


@pytest.mark.asyncio
async def test_explicit_ungated_marker_is_still_counted_when_present(tmp_path, monkeypatch):
    """Honouring the marker is not the bug; treating its absence as safety was."""
    monkeypatch.setattr(access, "_check_http_auth", lambda *a, **k: True)
    snap = tmp_path / "census.json"
    snap.write_text(json.dumps({
        "schema": "unitares.automation_census.v1",
        "summary": {"total": 2, "by_source": {}, "by_kind": {}, "needs_attention": [], "warnings": []},
        "automations": [
            {"id": "a", "notes": ["gate:ungated"], "source": "github-actions"},
            {"id": "b", "source": "launchd"},
        ],
    }))
    monkeypatch.setenv("UNITARES_AUTOMATION_CENSUS_PATH", str(snap))

    body = json.loads((await http_automations(
        SimpleNamespace(headers={}, query_params={"view": "summary"}))).body)

    # An explicit note beats the source heuristic, even for github-actions.
    assert body["ungated"] == 1
    assert body["unclassified"] == 1
