"""Executable checks for the repeated public UNITARES product framing."""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest


PROJECT_ROOT = Path(__file__).resolve().parents[1]
_SPEC = importlib.util.spec_from_file_location(
    "check_doc_drift_public_positioning",
    PROJECT_ROOT / "scripts" / "diagnostics" / "check_doc_drift.py",
)
assert _SPEC and _SPEC.loader
check_doc_drift = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(check_doc_drift)


def test_current_public_positioning_satisfies_contract() -> None:
    assert check_doc_drift.public_positioning_failures(PROJECT_ROOT) == []


def test_missing_requirement_is_reported(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / "README.md").write_text("an accountability server\n", encoding="utf-8")
    monkeypatch.setattr(
        check_doc_drift,
        "PUBLIC_POSITIONING_CHECKS",
        {"README.md": [("product category", ("federation kernel",))]},
    )

    assert check_doc_drift.public_positioning_failures(tmp_path) == [
        "README.md: missing public positioning requirement 'product category'"
    ]


def test_requirement_accepts_any_declared_wording(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / "README.md").write_text("multi-harness kernel\n", encoding="utf-8")
    monkeypatch.setattr(
        check_doc_drift,
        "PUBLIC_POSITIONING_CHECKS",
        {
            "README.md": [
                ("product category", ("federation kernel", "multi-harness kernel"))
            ]
        },
    )

    assert check_doc_drift.public_positioning_failures(tmp_path) == []
