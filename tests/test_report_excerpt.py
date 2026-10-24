"""Tests for the bounded check-in report excerpt (src/report_excerpt.py) and
its persistence into core.agent_state.state_json."""

from __future__ import annotations

import ast
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.report_excerpt import (
    DEFAULT_EXCERPT_CHARS,
    MAX_EXCERPT_CHARS,
    REDACTED,
    excerpt_chars,
    make_report_excerpt,
    scrub_credentials,
)

REPO_ROOT = Path(__file__).resolve().parent.parent


class TestLimit:
    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            (None, DEFAULT_EXCERPT_CHARS),
            ("", DEFAULT_EXCERPT_CHARS),
            ("abc", DEFAULT_EXCERPT_CHARS),
            ("0", 0),
            ("-3", 0),
            ("120", 120),
            ("999999", MAX_EXCERPT_CHARS),
        ],
    )
    def test_env_parsing(self, monkeypatch, raw, expected):
        if raw is None:
            monkeypatch.delenv("UNITARES_CHECKIN_EXCERPT_CHARS", raising=False)
        else:
            monkeypatch.setenv("UNITARES_CHECKIN_EXCERPT_CHARS", raw)
        assert excerpt_chars() == expected

    def test_zero_turns_retention_off(self, monkeypatch):
        monkeypatch.setenv("UNITARES_CHECKIN_EXCERPT_CHARS", "0")
        assert make_report_excerpt("Shipped the fix.") is None


class TestExcerpt:
    def test_empty_and_non_text_give_none(self):
        assert make_report_excerpt(None) is None
        assert make_report_excerpt("") is None
        assert make_report_excerpt("   \n\t ") is None
        assert make_report_excerpt(42) is None

    def test_short_text_kept_whole_with_whitespace_collapsed(self):
        assert make_report_excerpt("Opened  PR\n#3045.\tCI green.", limit=100) == (
            "Opened PR #3045. CI green."
        )

    def test_long_text_cut_at_a_word_boundary_never_mid_token(self):
        # The old hard cut at a fixed width turned a PR number into "#21".
        text = "Two follow-ups each wait on #2167 before they can land safely"
        out = make_report_excerpt(text, limit=33)
        assert out == "Two follow-ups each wait on…"
        assert len(out) <= 33
        assert "#21" not in out

    def test_single_overlong_word_is_cut_at_the_limit(self):
        out = make_report_excerpt("x" * 50, limit=10)
        assert out == "x" * 9 + "…"

    def test_exact_fit_is_not_truncated(self):
        assert make_report_excerpt("abcde fghij", limit=11) == "abcde fghij"

    def test_default_limit_applies(self, monkeypatch):
        monkeypatch.delenv("UNITARES_CHECKIN_EXCERPT_CHARS", raising=False)
        out = make_report_excerpt("word " * 400)
        assert len(out) <= DEFAULT_EXCERPT_CHARS
        assert out.endswith("…")


class TestScrub:
    @pytest.mark.parametrize(
        "secret",
        [
            "ghp_" + "a1B2c3D4e5F6g7H8i9J0k1L2m3",
            "github_pat_" + "11ABCDEFG0123456789_abcdefghijklmnop",
            "sk-ant-" + "api03-abcdefghijklmnopqrstuv",
            "sk-proj-" + "abcdefghijklmnopqrstuvwxyz",
            "xoxb-" + "1234567890-abcdefghij",
            "AKIA" + "ABCDEFGHIJKLMNOP",
            "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.abcdefghijklmnopq",
        ],
    )
    def test_token_shapes_are_redacted(self, secret):
        out = scrub_credentials(f"used {secret} to push")
        assert secret not in out
        assert REDACTED in out
        assert out.startswith("used ") and out.endswith(" to push")

    def test_bearer_and_secret_assignments_are_redacted(self):
        out = scrub_credentials(
            "Authorization: Bearer abcdef0123456789xyz; "
            "UNITARES_HTTP_API_TOKEN=s3cr3tvalue99 api_key: 'k-998877665544' password=hunter2hunter2"
        )
        for leaked in ("abcdef0123456789xyz", "s3cr3tvalue99", "k-998877665544", "hunter2hunter2"):
            assert leaked not in out
        assert "UNITARES_HTTP_API_TOKEN=" in out  # the name stays, the value goes

    def test_url_credentials_and_private_keys_are_redacted(self):
        out = scrub_credentials(
            "db at postgresql://admin:pa55word@localhost:5432/governance and "
            "-----BEGIN OPENSSH PRIVATE KEY-----\nAAAAB3Nza\n-----END OPENSSH PRIVATE KEY----- done"
        )
        assert "pa55word" not in out
        assert "AAAAB3Nza" not in out
        assert "localhost:5432/governance" in out
        assert out.endswith(" done")

    @pytest.mark.parametrize(
        "text",
        [
            "Fixed the token refresh bug in PR #3045; the key insight was the cache TTL.",
            "tests/test_token_refresh.py::test_rotates_on_expiry failed",
            "see src/auth/password_reset.py:1234567",
            "set max_tokens=200000 and input_tokens: 123456",
            "The token: refreshed correctly",
            "PR #3045 touches docs/FLAGS.md and src/http_routes/timeline.py",
        ],
    )
    def test_ordinary_prose_is_untouched(self, text):
        assert scrub_credentials(text) == text

    @pytest.mark.parametrize(
        ("text", "secret"),
        [
            ('{"api_key": "abcd1234efgh5678"}', "abcd1234efgh5678"),
            ('password="correct horse battery staple 9"', "correct horse battery staple 9"),
            ("STRIPE_KEY=sk_live_" + "abcdefghij0123456789", "sk_live_abcdefghij0123456789"),
            ("key AIza" + "SyA1234567890abcdefghijklmnopqrstuv", "SyA1234567890abcdefghijklmnopqrstuv"),
            ("glpat-" + "abcdefghij0123456789", "abcdefghij0123456789"),
            ("hf_" + "abcdefghijklmnopqrstuvwxyz012345", "abcdefghijklmnopqrstuvwxyz012345"),
            ("npm_" + "abcdefghijklmnopqrstuvwxyz0123456789", "abcdefghijklmnopqrstuvwxyz0123456789"),
            ("ASIA" + "ABCDEFGHIJKLMNOP", "ASIAABCDEFGHIJKLMNOP"),
            ("Authorization: Basic dXNlcjpwYXNzd29yZDEyMw==", "dXNlcjpwYXNzd29yZDEyMw"),
            ("https://user:p@ss99@example.com/x", "p@ss99"),
            ("-----BEGIN PGP PRIVATE KEY BLOCK-----\nlQOYBF\n-----END PGP PRIVATE KEY BLOCK-----", "lQOYBF"),
        ],
    )
    def test_more_secret_shapes_are_redacted(self, text, secret):
        assert secret not in scrub_credentials(text)

    @pytest.mark.parametrize(
        "hostile",
        ["token." * 1333, "a-secret" * 1000, "a." * 16000, "x" * 200000, "password=" * 5000],
    )
    def test_hostile_input_is_fast(self, hostile):
        import time

        start = time.perf_counter()
        make_report_excerpt(hostile, limit=500)
        assert time.perf_counter() - start < 0.5

    def test_unstorable_characters_are_stripped(self):
        out = make_report_excerpt("done\x00 here \ud800 now\x07", limit=100)
        assert out == "done here now"
        out.encode("utf-8")  # no lone surrogate survives

    def test_tiny_limit_gives_none_not_a_bare_ellipsis(self):
        assert make_report_excerpt("ab", limit=1) is None

    def test_window_cut_short_of_the_text_is_marked(self):
        # Long text whose scanned window is shorter than the original.
        out = make_report_excerpt("word " * 2000, limit=50)
        assert out.endswith("…") and len(out) <= 50

    def test_never_raises(self, monkeypatch):
        import src.report_excerpt as mod

        def boom(_text):
            raise RuntimeError("scrubber broke")

        monkeypatch.setattr(mod, "scrub_credentials", boom)
        assert mod.make_report_excerpt("anything at all", limit=100) is None

    def test_excerpt_scrubs_before_cutting(self):
        out = make_report_excerpt("token=abcdefghijklmnop " + "more " * 10, limit=30)
        assert "abcdefghijklmnop" not in out


@pytest.mark.asyncio
async def test_record_agent_state_persists_the_excerpt():
    from src import agent_storage

    db = MagicMock()
    db.init = AsyncMock()
    db.get_identity = AsyncMock(return_value=SimpleNamespace(identity_id=7))
    db.record_agent_state = AsyncMock(return_value=1)
    agent_storage._db_ready_cache.clear()
    with patch("src.agent_storage.get_db", return_value=db):
        await agent_storage.record_agent_state(
            "agent-1", E=0.7, I=0.8, S=0.2, V=0.0, regime="EXPLORATION",
            coherence=0.5, report_excerpt="Opened PR #3045.",
        )
        await agent_storage.record_agent_state(
            "agent-1", E=0.7, I=0.8, S=0.2, V=0.0, regime="EXPLORATION", coherence=0.5,
        )
    agent_storage._db_ready_cache.clear()
    first, second = (c.kwargs["state_json"] for c in db.record_agent_state.call_args_list)
    assert first["report_excerpt"] == "Opened PR #3045."
    assert "report_excerpt" not in second


def test_both_record_sites_pass_one_excerpt_computed_outside_the_write():
    source = (REPO_ROOT / "src" / "mcp_handlers" / "updates" / "phases.py").read_text(encoding="utf-8")
    assert source.count("report_excerpt=report_excerpt,") == 2
    tree = ast.parse(source)
    parents = {child: node for node in ast.walk(tree) for child in ast.iter_child_nodes(node)}
    computes = [
        node for node in ast.walk(tree)
        if isinstance(node, ast.Assign)
        and isinstance(node.value, ast.Call)
        and getattr(node.value.func, "id", None) == "make_report_excerpt"
    ]
    assert len(computes) == 1
    # No enclosing try: an exception there would be caught with the write's.
    node = computes[0]
    while node in parents:
        node = parents[node]
        assert not isinstance(node, ast.Try), "make_report_excerpt is inside a try"


@pytest.mark.parametrize(
    ("text", "secret"),
    [
        ("GOOGLE_CLIENT_SECRET=" + "Ab1" * 140, "Ab1" * 10),
        ('{"api_key": "' + "Zz9" * 110 + '"}', "Zz9" * 10),
        ("aws_secret_access_key=wJalrXUtnFEMI0K7MDENG", "wJalrXUtnFEMI0K7MDENG"),
        ("secret_key: 'django-insecure-0123456789abc'", "django-insecure-0123456789abc"),
        ("UNITARES_" + "X" * 50 + "_BEARER_API_TOKEN=abc123def456", "abc123def456"),
    ],
)
def test_long_values_and_compound_names_are_redacted(text, secret):
    assert secret not in scrub_credentials(text)


def test_basic_and_bearer_in_prose_survive():
    text = "basic authentication was the cause; Fixed the Bearer authentication middleware"
    assert scrub_credentials(text) == text


def test_short_assigned_secrets_and_escaped_quotes_are_redacted():
    out = scrub_credentials('password=hunter2 api_key=abc1234 {"password": "ab\\"cdefghijk"}')
    for secret in ("hunter2", "abc1234", "cdefghijk"):
        assert secret not in out


def test_the_scan_margin_counts_after_whitespace_is_collapsed():
    token = "ghp_" + "abcdefghij" * 3
    out = make_report_excerpt(" " * 1010 + token + " and more")
    assert out == "[REDACTED] and more"
    pr = make_report_excerpt("see " + " " * 1010 + "#2167 then words " * 60)
    assert "#216…" not in pr and pr.endswith("…")


def test_a_raw_read_ending_mid_token_keeps_nothing_near_it():
    raw_end = (500 + 512) * 16
    assert make_report_excerpt("x " + " " * (raw_end - 4) + "ghp_" + "a" * 40) is None
    assert make_report_excerpt("x " + " " * 200000 + "ghp_" + "a" * 40) == "x…"


@pytest.mark.parametrize(
    "text",
    ["export TOKEN='abc123defg\\'", 'DB_PASSWORD="C:\\secrets\\"', 'password="hunter2xyz\\"'],
)
def test_a_quoted_value_ending_in_a_backslash_is_still_redacted(text):
    assert REDACTED in scrub_credentials(text)


def test_redactions_inside_the_window_do_not_eat_the_margin():
    tokens = " ".join("glpat-" + "a" * 255 for _ in range(3))
    text = tokens + " " + "w " * 30 + 'password: "correct horse battery staple ' + "x " * 400 + '"'
    out = make_report_excerpt(text)
    assert "correct horse" not in out
