"""reembed_corpus.py embeds the same text as the server and defaults to recovery (#2364)."""

import importlib.util
from pathlib import Path

from src.mcp_handlers.knowledge.limits import EMBED_DETAILS_WINDOW

_PATH = Path(__file__).resolve().parent.parent / "scripts" / "migration" / "reembed_corpus.py"
_spec = importlib.util.spec_from_file_location("reembed_corpus", _PATH)
reembed = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(reembed)


def test_build_text_uses_embed_window():
    details = "D" * (EMBED_DETAILS_WINDOW + 1000)
    text = reembed.build_text("sum", details)
    assert text == "sum\n" + "D" * EMBED_DETAILS_WINDOW
    assert len(text) > 500 + len("sum")


def test_build_text_without_details_matches_server_shape():
    # The server builds f"{summary}\n{details...}" even with no details.
    assert reembed.build_text("sum", None) == "sum\n"
    assert reembed.build_text("sum", "") == "sum\n"


def test_limit_applies_after_the_missing_filter():
    rows = [("d%d" % i, "s", None) for i in range(6)]  # newest first
    already = {"d0", "d1", "d2"}  # the newest three already have vectors
    picked = reembed.select_targets(rows, already, rebuild=False, limit=2)
    assert [r[0] for r in picked] == ["d3", "d4"]  # advances, not "nothing to do"


def test_rebuild_ignores_existing_vectors_but_honors_limit():
    rows = [("d%d" % i, "s", None) for i in range(4)]
    picked = reembed.select_targets(rows, {"d0"}, rebuild=True, limit=3)
    assert [r[0] for r in picked] == ["d0", "d1", "d2"]


def test_rebuild_and_only_missing_conflict_exits_before_any_db_access(monkeypatch):
    import asyncio
    import pytest

    def _no_db():
        raise AssertionError("get_db must not be reached")

    monkeypatch.setattr(reembed, "get_db", _no_db)
    monkeypatch.setattr(reembed.sys, "argv", ["x", "--rebuild", "--only-missing"])
    with pytest.raises(SystemExit) as exc:
        asyncio.run(reembed.main())
    assert exc.value.code == 2


def test_legacy_constant_removed():
    assert not hasattr(reembed, "MAX_DETAILS_CHARS")


def test_default_is_recovery_and_rebuild_is_opt_in():
    parser = reembed.build_parser()
    assert parser.parse_args([]).rebuild is False
    assert parser.parse_args(["--rebuild"]).rebuild is True
    # legacy flag still accepted
    assert parser.parse_args(["--only-missing"]).only_missing is True
