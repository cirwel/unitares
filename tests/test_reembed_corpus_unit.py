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


def test_build_text_without_details_is_summary():
    assert reembed.build_text("sum", None) == "sum"
    assert reembed.build_text("sum", "") == "sum"


def test_legacy_constant_removed():
    assert not hasattr(reembed, "MAX_DETAILS_CHARS")


def test_default_is_recovery_and_rebuild_is_opt_in():
    parser = reembed.build_parser()
    assert parser.parse_args([]).rebuild is False
    assert parser.parse_args(["--rebuild"]).rebuild is True
    # legacy flag still accepted
    assert parser.parse_args(["--only-missing"]).only_missing is True
