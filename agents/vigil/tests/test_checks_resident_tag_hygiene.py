"""Tests for the ResidentTagHygiene Vigil check."""

from __future__ import annotations

import asyncio

import pytest


def _reset_registry():
    from agents.vigil.checks import registry
    registry._CHECKS.clear()
    registry._LOADED = False


@pytest.fixture(autouse=True)
def clean_registry():
    _reset_registry()
    yield
    _reset_registry()


def test_resident_tag_hygiene_identity():
    from agents.vigil.checks.resident_tag_hygiene import ResidentTagHygiene

    check = ResidentTagHygiene()
    assert check.name == "resident_tag_hygiene"
    assert check.service_key == "resident_tags"


def test_hygiene_ok_when_no_tags_missing(monkeypatch):
    from agents.vigil.checks import resident_tag_hygiene

    monkeypatch.setattr(
        resident_tag_hygiene,
        "fetch_tag_audit",
        lambda url, timeout=5.0: (
            True,
            {
                "success": True,
                "required_tags": ["autonomous", "persistent"],
                "checked": ["Lumen", "Sentinel", "Steward", "Vigil", "Watcher"],
                "missing": {},
                "ok_count": 5,
            },
            "18ms",
        ),
    )

    result = asyncio.run(resident_tag_hygiene.ResidentTagHygiene().run())
    assert result.ok is True
    assert "5/5" in result.summary


def test_hygiene_critical_when_tags_missing(monkeypatch):
    from agents.vigil.checks import resident_tag_hygiene

    monkeypatch.setattr(
        resident_tag_hygiene,
        "fetch_tag_audit",
        lambda url, timeout=5.0: (
            True,
            {
                "success": True,
                "required_tags": ["autonomous", "persistent"],
                "checked": ["Vigil", "Watcher"],
                "missing": {"Watcher": ["autonomous", "persistent"]},
                "ok_count": 1,
            },
            "22ms",
        ),
    )

    result = asyncio.run(resident_tag_hygiene.ResidentTagHygiene().run())
    assert result.ok is False
    assert result.severity == "critical"
    assert "Watcher" in result.summary
    assert "autonomous" in result.summary
    assert "persistent" in result.summary
    assert result.fingerprint_key == "resident_tag_gap:Watcher"


def test_hygiene_multiple_residents_missing_compressed_summary(monkeypatch):
    """Gaps across multiple residents collapse into one finding row."""
    from agents.vigil.checks import resident_tag_hygiene

    monkeypatch.setattr(
        resident_tag_hygiene,
        "fetch_tag_audit",
        lambda url, timeout=5.0: (
            True,
            {
                "success": True,
                "required_tags": ["autonomous", "persistent"],
                "checked": ["Vigil", "Sentinel", "Watcher"],
                "missing": {"Sentinel": ["autonomous"], "Watcher": ["autonomous", "persistent"]},
                "ok_count": 1,
            },
            "15ms",
        ),
    )

    result = asyncio.run(resident_tag_hygiene.ResidentTagHygiene().run())
    assert result.ok is False
    assert result.fingerprint_key == "resident_tag_gap:Sentinel+Watcher"
    assert result.detail["missing"] == {
        "Sentinel": ["autonomous"],
        "Watcher": ["autonomous", "persistent"],
    }


def test_hygiene_endpoint_unreachable_degrades_to_warning(monkeypatch):
    """Endpoint down = governance_health's problem; don't page twice as critical."""
    from agents.vigil.checks import resident_tag_hygiene

    monkeypatch.setattr(
        resident_tag_hygiene,
        "fetch_tag_audit",
        lambda url, timeout=5.0: (False, {}, "unreachable"),
    )

    result = asyncio.run(resident_tag_hygiene.ResidentTagHygiene().run())
    assert result.ok is False
    assert result.severity == "warning"
    assert result.fingerprint_key == "resident_tag_audit_unreachable"


def test_hygiene_uses_configured_url(monkeypatch):
    from agents.vigil.checks import resident_tag_hygiene

    captured = {}

    def fake(url, timeout=5.0):
        captured["url"] = url
        return True, {"missing": {}, "checked": [], "ok_count": 0, "required_tags": []}, "ok"

    monkeypatch.setattr(resident_tag_hygiene, "fetch_tag_audit", fake)
    monkeypatch.setattr(
        resident_tag_hygiene,
        "RESIDENT_TAG_AUDIT_URL",
        "http://test.local:9999/v1/residents/tag_audit",
    )

    asyncio.run(resident_tag_hygiene.ResidentTagHygiene().run())
    assert captured["url"] == "http://test.local:9999/v1/residents/tag_audit"


def test_load_plugins_registers_resident_tag_hygiene(monkeypatch):
    from agents.vigil.checks import registry

    monkeypatch.delenv("VIGIL_CHECK_PLUGINS", raising=False)
    registry.load_plugins()
    names = [c.name for c in registry.all_checks()]
    assert "resident_tag_hygiene" in names
