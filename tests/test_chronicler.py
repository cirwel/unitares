"""Tests for agents/chronicler/{scrapers,agent}.py."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


# ---------------------------------------------------------------------------
# scrapers.tokei_unitares_src_code
# ---------------------------------------------------------------------------


class TestTokeiScraper:
    def test_counts_lines_in_real_src(self, tmp_path: Path):
        from agents.chronicler.scrapers import tokei_unitares_src_code

        src = tmp_path / "src"
        src.mkdir()
        (src / "a.py").write_text("print(1)\nprint(2)\nprint(3)\n")
        (src / "b.py").write_text("x = 1\n")
        (src / "ignored.js").write_text("// not python\nstill not\n")

        value = tokei_unitares_src_code(tmp_path)
        # 3 + 1 = 4; the .js file must not count.
        assert value == 4.0

    def test_empty_src_returns_zero(self, tmp_path: Path):
        from agents.chronicler.scrapers import tokei_unitares_src_code

        (tmp_path / "src").mkdir()
        assert tokei_unitares_src_code(tmp_path) == 0.0

    def test_missing_src_raises(self, tmp_path: Path):
        from agents.chronicler.scrapers import tokei_unitares_src_code

        with pytest.raises(FileNotFoundError):
            tokei_unitares_src_code(tmp_path)


class TestDbScrapers:
    """DB-backed scrapers delegate to `_fetchval`. Test each one calls it
    with the right SQL shape — the actual DB round-trip is integration-tested
    elsewhere."""

    def test_agents_active_7d_queries_distinct_governed_agents(self, tmp_path: Path):
        from agents.chronicler import scrapers

        with patch.object(scrapers, "_fetchval", return_value=42.0) as m:
            value = scrapers.agents_active_7d(tmp_path)

        assert value == 42.0
        sql = m.call_args.args[0]
        assert "count(DISTINCT identity_id)" in sql
        assert "core.agent_state" in sql
        assert "7 days" in sql

    def test_kg_entries_count_queries_discoveries(self, tmp_path: Path):
        from agents.chronicler import scrapers

        with patch.object(scrapers, "_fetchval", return_value=137.0) as m:
            value = scrapers.kg_entries_count(tmp_path)

        assert value == 137.0
        assert "knowledge.discoveries" in m.call_args.args[0]

    def test_checkins_7d_filters_process_agent_update(self, tmp_path: Path):
        from agents.chronicler import scrapers

        with patch.object(scrapers, "_fetchval", return_value=9001.0) as m:
            value = scrapers.checkins_7d(tmp_path)

        assert value == 9001.0
        sql = m.call_args.args[0]
        assert "tool_name = 'process_agent_update'" in sql
        assert "7 days" in sql

    def test_new_scrapers_registered_in_SCRAPERS(self):
        from agents.chronicler.scrapers import SCRAPERS

        for name in ("agents.active.7d", "kg.entries.count", "checkins.7d"):
            assert name in SCRAPERS, f"{name} missing from SCRAPERS registry"


class TestGovernanceHealthScrapers:
    """The governance-health series — EISV/verdict/finding aggregates that were
    live-only until now. Each reads core.agent_state or audit.events over a
    trailing 7-day window. Tests pin the SQL shape (table, window, filter) so a
    refactor can't silently change what the chart means."""

    def test_coherence_mean_averages_nonsynthetic_checkins(self, tmp_path: Path):
        from agents.chronicler import scrapers

        with patch.object(scrapers, "_fetchval", return_value=0.49) as m:
            value = scrapers.governance_coherence_mean_7d(tmp_path)

        assert value == 0.49
        sql = m.call_args.args[0]
        assert "avg(coherence)" in sql
        assert "core.agent_state" in sql
        assert "synthetic = false" in sql
        assert "7 days" in sql

    def test_risk_mean_averages_risk_score(self, tmp_path: Path):
        from agents.chronicler import scrapers

        with patch.object(scrapers, "_fetchval", return_value=0.07) as m:
            value = scrapers.governance_risk_mean_7d(tmp_path)

        assert value == 0.07
        sql = m.call_args.args[0]
        assert "avg(risk_score)" in sql
        assert "synthetic = false" in sql

    def test_guide_counts_guide_action(self, tmp_path: Path):
        from agents.chronicler import scrapers

        with patch.object(scrapers, "_fetchval", return_value=310.0) as m:
            value = scrapers.governance_guide_7d(tmp_path)

        assert value == 310.0
        sql = m.call_args.args[0]
        assert "state_json->>'action' = 'guide'" in sql
        assert "7 days" in sql

    def test_pause_counts_nonapprove_nonguide_actions(self, tmp_path: Path):
        """Hard interventions = anything that isn't approve/guide, kept
        open-ended so a new hard-stop action folds in without a code change."""
        from agents.chronicler import scrapers

        with patch.object(scrapers, "_fetchval", return_value=3.0) as m:
            value = scrapers.governance_pause_7d(tmp_path)

        assert value == 3.0
        sql = m.call_args.args[0]
        assert "NOT IN ('approve', 'guide')" in sql
        assert "IS NOT NULL" in sql  # NULL action must not count as a pause

    def test_sentinel_findings_count_durable_audit_events(self, tmp_path: Path):
        from agents.chronicler import scrapers

        with patch.object(scrapers, "_fetchval", return_value=135.0) as m:
            value = scrapers.governance_sentinel_findings_7d(tmp_path)

        assert value == 135.0
        sql = m.call_args.args[0]
        assert "audit.events" in sql
        assert "sentinel_finding" in sql
        assert "sentinel_alarm_finding" in sql


class TestTestsCountScraper:
    def test_counts_only_test_prefixed_py_files(self, tmp_path: Path):
        from agents.chronicler.scrapers import tests_unitares_count

        tests = tmp_path / "tests"
        tests.mkdir()
        (tests / "test_a.py").write_text("")
        (tests / "test_b.py").write_text("")
        (tests / "conftest.py").write_text("")        # not test_*.py — excluded
        (tests / "helper_utils.py").write_text("")    # not test_*.py — excluded
        (tests / "notes.md").write_text("")           # not python — excluded

        sub = tests / "sub"
        sub.mkdir()
        (sub / "test_nested.py").write_text("")       # nested test_*.py — counted

        assert tests_unitares_count(tmp_path) == 3.0

    def test_empty_tests_dir_returns_zero(self, tmp_path: Path):
        from agents.chronicler.scrapers import tests_unitares_count

        (tmp_path / "tests").mkdir()
        assert tests_unitares_count(tmp_path) == 0.0

    def test_missing_tests_dir_raises(self, tmp_path: Path):
        from agents.chronicler.scrapers import tests_unitares_count

        with pytest.raises(FileNotFoundError):
            tests_unitares_count(tmp_path)


# ---------------------------------------------------------------------------
# agent.run
# ---------------------------------------------------------------------------


class FakeHttpResponse:
    def __init__(self, status_code: int, text: str = ""):
        self.status_code = status_code
        self.text = text


class FakeHttpClient:
    """Tracks calls to client.post so we can assert payload/url/auth shape.

    `priors` maps a metric name to the series `points` the GET should return;
    an absent name yields an empty series, i.e. no prior reading.
    """

    def __init__(
        self,
        response: FakeHttpResponse | None = None,
        priors: dict[str, list[dict]] | None = None,
    ):
        self.calls: list[dict] = []
        self.gets: list[dict] = []
        self._response = response or FakeHttpResponse(201)
        self._priors = priors or {}

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def post(self, url, *, headers, content, timeout):
        self.calls.append(
            {
                "url": url,
                "headers": headers,
                "body": json.loads(content),
                "timeout": timeout,
            }
        )
        return self._response

    def get(self, url, *, headers, params, timeout):
        self.gets.append({"url": url, "headers": headers, "params": params})
        name = params.get("name")
        return FakeJsonResponse(
            200, {"success": True, "points": self._priors.get(name, [])}
        )


class FakeJsonResponse(FakeHttpResponse):
    def __init__(self, status_code: int, payload: dict):
        super().__init__(status_code)
        self._payload = payload

    def json(self):
        return self._payload


class TestAgentRun:
    def test_success_case_posts_each_scraper(self, tmp_path: Path):
        from agents.chronicler import agent as chronicler

        fake = FakeHttpClient()
        scrapers = {
            "tokei.unitares.src.code": lambda _root: 70000.0,
            "tests.unitares.count": lambda _root: 6601.0,
        }

        with (
            patch.object(chronicler, "SCRAPERS", scrapers),
            patch("agents.chronicler.agent.httpx.Client", return_value=fake),
        ):
            report = chronicler.run("http://127.0.0.1:8767", token=None, repo_root=tmp_path)
            ok, fail = report.successes, report.failures

        assert ok == 2
        assert fail == 0
        assert len(fake.calls) == 2
        names = {call["body"]["name"] for call in fake.calls}
        assert names == {"tokei.unitares.src.code", "tests.unitares.count"}

    def test_dry_run_does_not_post(self, tmp_path: Path):
        from agents.chronicler import agent as chronicler

        fake = FakeHttpClient()
        with (
            patch.object(chronicler, "SCRAPERS", {"x.y.z": lambda _r: 1.0}),
            patch("agents.chronicler.agent.httpx.Client", return_value=fake),
        ):
            report = chronicler.run(
                "http://127.0.0.1:8767", token=None, repo_root=tmp_path, dry_run=True
            )
            ok, fail = report.successes, report.failures

        assert ok == 1
        assert fail == 0
        assert fake.calls == []

    def test_scrape_failure_emits_error_metric(self, tmp_path: Path):
        from agents.chronicler import agent as chronicler

        fake = FakeHttpClient()

        def boom(_root):
            raise RuntimeError("tokei missing")

        with (
            patch.object(chronicler, "SCRAPERS", {"tokei.unitares.src.code": boom}),
            patch("agents.chronicler.agent.httpx.Client", return_value=fake),
        ):
            report = chronicler.run("http://127.0.0.1:8767", token=None, repo_root=tmp_path)
            ok, fail = report.successes, report.failures

        assert ok == 0
        assert fail == 1
        assert len(fake.calls) == 1
        error_call = fake.calls[0]
        assert error_call["body"]["name"] == "tokei.unitares.src.code.error"
        assert error_call["body"]["value"] == 1.0

    def test_http_error_counts_as_failure_but_does_not_raise(self, tmp_path: Path):
        from agents.chronicler import agent as chronicler

        fake = FakeHttpClient(response=FakeHttpResponse(500, "boom"))
        with (
            patch.object(chronicler, "SCRAPERS", {"x.y.z": lambda _r: 1.0}),
            patch("agents.chronicler.agent.httpx.Client", return_value=fake),
        ):
            report = chronicler.run("http://127.0.0.1:8767", token=None, repo_root=tmp_path)
            ok, fail = report.successes, report.failures

        assert ok == 0
        assert fail == 1

    def test_bearer_token_header_included(self, tmp_path: Path):
        from agents.chronicler import agent as chronicler

        fake = FakeHttpClient()
        with (
            patch.object(chronicler, "SCRAPERS", {"x.y.z": lambda _r: 1.0}),
            patch("agents.chronicler.agent.httpx.Client", return_value=fake),
        ):
            chronicler.run("http://127.0.0.1:8767", token="secret-123", repo_root=tmp_path)

        assert fake.calls[0]["headers"]["authorization"] == "Bearer secret-123"

    def test_post_error_metric_survives_if_server_unreachable(self, tmp_path: Path):
        """If the post-error call itself fails, we still complete the loop."""
        from agents.chronicler import agent as chronicler

        class AlwaysFails:
            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

            def post(self, *a, **kw):
                raise RuntimeError("network down")

        def boom(_root):
            raise RuntimeError("scrape failed")

        with (
            patch.object(chronicler, "SCRAPERS", {"x.y.z": boom}),
            patch("agents.chronicler.agent.httpx.Client", return_value=AlwaysFails()),
        ):
            report = chronicler.run("http://127.0.0.1:8767", token=None, repo_root=tmp_path)
            ok, fail = report.successes, report.failures

        assert ok == 0
        assert fail == 1  # scrape failure counted, post-error failure swallowed


# ---------------------------------------------------------------------------
# fetch_prior / format_digest — the per-run KG digest
# ---------------------------------------------------------------------------


class TestFetchPrior:
    def test_returns_most_recent_point_not_oldest(self, tmp_path: Path):
        """`/v1/metrics` sorts ts ASC, so the newest reading is the LAST point.
        Taking points[0] would compare today against ancient history."""
        from agents.chronicler import agent as chronicler

        fake = FakeHttpClient(
            priors={
                "x.y.z": [
                    {"ts": "2026-08-01T00:00:00+00:00", "value": 100.0},
                    {"ts": "2026-08-13T00:00:00+00:00", "value": 130.0},
                ]
            }
        )
        assert chronicler.fetch_prior(fake, "http://h", None, "x.y.z") == 130.0

    def test_reads_the_series_route_not_the_post_route(self, tmp_path: Path):
        """`/v1/metrics` is POST-only and answers a GET with 405, which
        `fetch_prior` cannot distinguish from an empty series — so a regression
        here would silently report every metric as a first reading forever."""
        from agents.chronicler import agent as chronicler

        fake = FakeHttpClient()
        chronicler.fetch_prior(fake, "http://h", None, "x.y.z")
        assert fake.gets[0]["url"] == "http://h/v1/metrics/series"

    def test_http_error_is_logged_not_swallowed(self, tmp_path: Path, caplog):
        from agents.chronicler import agent as chronicler

        class Rejects:
            def get(self, *a, **kw):
                return FakeHttpResponse(405, "Method Not Allowed")

        with caplog.at_level("WARNING"):
            assert chronicler.fetch_prior(Rejects(), "http://h", None, "x") is None
        assert "405" in caplog.text

    def test_empty_series_means_no_prior(self, tmp_path: Path):
        from agents.chronicler import agent as chronicler

        fake = FakeHttpClient()
        assert chronicler.fetch_prior(fake, "http://h", None, "never.seen") is None

    def test_unreadable_history_never_raises(self, tmp_path: Path):
        """A digest is layered on top of the scrape; it must not cost a metric."""
        from agents.chronicler import agent as chronicler

        class Exploding:
            def get(self, *a, **kw):
                raise RuntimeError("network down")

        assert chronicler.fetch_prior(Exploding(), "http://h", None, "x") is None

    def test_reads_before_writing(self, tmp_path: Path):
        """The GET must precede the POST for the same metric, or 'prior' is
        just the value we are about to write."""
        from agents.chronicler import agent as chronicler

        order: list[str] = []
        fake = FakeHttpClient()
        real_get, real_post = fake.get, fake.post
        fake.get = lambda *a, **kw: (order.append("get"), real_get(*a, **kw))[1]
        fake.post = lambda *a, **kw: (order.append("post"), real_post(*a, **kw))[1]

        with (
            patch.object(chronicler, "SCRAPERS", {"x.y.z": lambda _r: 1.0}),
            patch("agents.chronicler.agent.httpx.Client", return_value=fake),
        ):
            chronicler.run("http://127.0.0.1:8767", token=None, repo_root=tmp_path)

        assert order == ["get", "post"]


class TestFormatDigest:
    def _report(self, movements, **kw):
        from agents.chronicler.agent import Movement, ScrapeReport

        return ScrapeReport(
            successes=kw.get("successes", len(movements)),
            failures=kw.get("failures", 0),
            movements=[Movement(*m) for m in movements],
            failed=kw.get("failed", []),
        )

    def test_summary_counts_movers_not_scrapers(self):
        from agents.chronicler.agent import format_digest

        report = self._report(
            [("a", 2.0, 1.0), ("b", 5.0, 5.0), ("c", 9.0, 7.0)]
        )
        summary, _ = format_digest(report)
        assert summary == "Chronicler daily: 3/3 scrapers ok, 2 moved"

    def test_details_name_the_delta(self):
        from agents.chronicler.agent import format_digest

        report = self._report([("tests.unitares.count", 663.0, 655.0)])
        _, details = format_digest(report)
        assert "tests.unitares.count: 655 -> 663 (+8)" in details

    def test_negative_delta_renders_with_a_minus(self):
        from agents.chronicler.agent import format_digest

        _, details = format_digest(self._report([("kg.entries.count", 1450.0, 1459.0)]))
        assert "1459 -> 1450 (-9)" in details

    def test_means_keep_decimals_counts_do_not(self):
        """The series mixes counts with means; 655.0 must not print '655.0000'
        and a coherence mean must not round away to an integer."""
        from agents.chronicler.agent import format_digest

        _, details = format_digest(
            self._report([("governance.coherence.mean.7d", 0.4799621, 0.4813619)])
        )
        assert "0.4814 -> 0.4800 (-0.0014)" in details

    def test_no_prior_is_distinct_from_unchanged(self):
        """After an outage these are different claims and an operator needs to
        tell them apart."""
        from agents.chronicler.agent import format_digest

        _, details = format_digest(
            self._report([("fresh", 1.0, None), ("steady", 4.0, 4.0)])
        )
        assert "No prior reading in 30d (1): fresh" in details
        assert "Unchanged (1): steady" in details

    def test_failures_are_named_not_just_counted(self):
        from agents.chronicler.agent import format_digest

        report = self._report(
            [("ok", 1.0, 1.0)], successes=1, failures=1, failed=["tokei.unitares.src.code"]
        )
        summary, details = format_digest(report)
        assert summary == "Chronicler daily: 1/2 scrapers ok, 0 moved"
        assert "Failed (1): tokei.unitares.src.code" in details

    def test_empty_run_still_says_something(self):
        from agents.chronicler.agent import format_digest

        summary, details = format_digest(self._report([]))
        assert summary == "Chronicler daily: 0/0 scrapers ok, 0 moved"
        assert details == "No scrapers ran."


# ---------------------------------------------------------------------------
# ChroniclerAgent — governance identity wrapper
# ---------------------------------------------------------------------------


class TestChroniclerAgent:
    def test_is_persistent_resident_refusing_fresh_onboard(self, tmp_path: Path):
        """Chronicler is a resident; the SDK must refuse fresh onboard without
        UNITARES_FIRST_RUN, matching Vigil/Sentinel/Watcher."""
        from agents.chronicler.agent import ChroniclerAgent

        agent = ChroniclerAgent(
            base_url="http://127.0.0.1:8767", token=None, repo_root=tmp_path,
        )
        assert agent.name == "Chronicler"
        assert agent.persistent is True
        assert agent.refuse_fresh_onboard is True
        # MCP URL is derived from the metrics base URL, not hardcoded, so
        # UNITARES_METRICS_URL overrides route to the same server.
        assert agent.mcp_url == "http://127.0.0.1:8767/mcp/"

    def test_run_cycle_returns_summary_on_clean_run(self, tmp_path: Path):
        from agents.chronicler import agent as chronicler
        from agents.chronicler.agent import ChroniclerAgent

        scrapers = {
            "a": lambda _r: 1.0,
            "b": lambda _r: 2.0,
        }
        fake = FakeHttpClient()
        agent = ChroniclerAgent(
            base_url="http://127.0.0.1:8767", token=None, repo_root=tmp_path,
        )
        with (
            patch.object(chronicler, "SCRAPERS", scrapers),
            patch("agents.chronicler.agent.httpx.Client", return_value=fake),
        ):
            result = asyncio.run(agent.run_cycle(client=AsyncMock()))

        assert result is not None
        assert result.summary == "Chronicler: 2/2 scrapers ok"
        # Clean runs stay low-complexity / high-confidence so a routine day
        # doesn't perturb the trajectory.
        assert result.complexity == 0.1
        assert result.confidence == 0.9

    def test_run_cycle_bumps_complexity_when_a_scraper_fails(self, tmp_path: Path):
        from agents.chronicler import agent as chronicler
        from agents.chronicler.agent import ChroniclerAgent

        def boom(_root):
            raise RuntimeError("broken")

        scrapers = {"ok": lambda _r: 1.0, "boom": boom}
        fake = FakeHttpClient()
        agent = ChroniclerAgent(
            base_url="http://127.0.0.1:8767", token=None, repo_root=tmp_path,
        )
        with (
            patch.object(chronicler, "SCRAPERS", scrapers),
            patch("agents.chronicler.agent.httpx.Client", return_value=fake),
        ):
            result = asyncio.run(agent.run_cycle(client=AsyncMock()))

        assert result is not None
        assert result.summary == "Chronicler: 1/2 scrapers ok"
        # Failure bumps both dimensions so the check-in carries honest uncertainty.
        assert result.complexity == 0.4
        assert result.confidence == 0.5

    def test_cycle_stores_a_kg_digest(self, tmp_path: Path):
        """The point of the digest: a run leaves something an operator reads,
        not just a chart point."""
        from agents.chronicler import agent as chronicler
        from agents.chronicler.agent import ChroniclerAgent

        fake = FakeHttpClient(
            priors={"tests.unitares.count": [{"ts": "2026-08-13", "value": 655.0}]}
        )
        client = AsyncMock()
        agent = ChroniclerAgent(
            base_url="http://127.0.0.1:8767", token=None, repo_root=tmp_path,
        )
        with (
            patch.object(chronicler, "SCRAPERS", {"tests.unitares.count": lambda _r: 663.0}),
            patch("agents.chronicler.agent.httpx.Client", return_value=fake),
        ):
            asyncio.run(agent.run_cycle(client=client))

        tool, args = client.call_tool.call_args.args
        assert tool == "knowledge"
        assert args["action"] == "store"
        assert args["summary"] == "Chronicler daily: 1/1 scrapers ok, 1 moved"
        assert "tests.unitares.count: 655 -> 663 (+8)" in args["details"]

    def test_digest_is_tagged_ephemeral(self, tmp_path: Path):
        """Without the tag a snapshot has no resolution condition, so every
        later KG sweep re-reads it as unfinished work."""
        from agents.chronicler.agent import DIGEST_TAGS

        assert "ephemeral" in DIGEST_TAGS

    def test_kg_failure_does_not_fail_the_run(self, tmp_path: Path):
        """The metrics have already landed by then — losing the digest is not
        worth a red run."""
        from agents.chronicler import agent as chronicler
        from agents.chronicler.agent import ChroniclerAgent

        client = AsyncMock()
        client.call_tool.side_effect = RuntimeError("KG unreachable")
        fake = FakeHttpClient()
        agent = ChroniclerAgent(
            base_url="http://127.0.0.1:8767", token=None, repo_root=tmp_path,
        )
        with (
            patch.object(chronicler, "SCRAPERS", {"x": lambda _r: 1.0}),
            patch("agents.chronicler.agent.httpx.Client", return_value=fake),
        ):
            result = asyncio.run(agent.run_cycle(client=client))

        assert result is not None
        assert result.summary == "Chronicler: 1/1 scrapers ok"

    def test_digest_can_be_switched_off(self, tmp_path: Path, monkeypatch):
        from agents.chronicler import agent as chronicler
        from agents.chronicler.agent import ChroniclerAgent

        monkeypatch.setenv("CHRONICLER_KG_DIGEST", "0")
        client = AsyncMock()
        fake = FakeHttpClient()
        agent = ChroniclerAgent(
            base_url="http://127.0.0.1:8767", token=None, repo_root=tmp_path,
        )
        with (
            patch.object(chronicler, "SCRAPERS", {"x": lambda _r: 1.0}),
            patch("agents.chronicler.agent.httpx.Client", return_value=fake),
        ):
            asyncio.run(agent.run_cycle(client=client))

        client.call_tool.assert_not_called()

    def test_dry_run_writes_no_digest(self, tmp_path: Path):
        """--dry must stay a pure diagnostic: no check-in, no KG entry."""
        from agents.chronicler import agent as chronicler
        from agents.chronicler.agent import ChroniclerAgent

        client = AsyncMock()
        fake = FakeHttpClient()
        agent = ChroniclerAgent(
            base_url="http://127.0.0.1:8767", token=None, repo_root=tmp_path,
            dry_run=True,
        )
        with (
            patch.object(chronicler, "SCRAPERS", {"x": lambda _r: 1.0}),
            patch("agents.chronicler.agent.httpx.Client", return_value=fake),
        ):
            asyncio.run(agent.run_cycle(client=client))

        client.call_tool.assert_not_called()

    def test_dry_run_cycle_returns_none(self, tmp_path: Path):
        """--dry is a diagnostic — it must not write a check-in (would pollute
        the trajectory with ad-hoc operator invocations)."""
        from agents.chronicler import agent as chronicler
        from agents.chronicler.agent import ChroniclerAgent

        fake = FakeHttpClient()
        agent = ChroniclerAgent(
            base_url="http://127.0.0.1:8767", token=None, repo_root=tmp_path,
            dry_run=True,
        )
        with (
            patch.object(chronicler, "SCRAPERS", {"x": lambda _r: 1.0}),
            patch("agents.chronicler.agent.httpx.Client", return_value=fake),
        ):
            result = asyncio.run(agent.run_cycle(client=AsyncMock()))

        assert result is None  # GovernanceAgent._handle_cycle_result skips check-in


class TestGithubTrafficScrapers:
    """Aggregate GitHub traffic across non-archived CIRWEL repos.

    The four scrapers share one process-lifetime fetch (Chronicler is a
    one-shot launchd job) so a single run hits the GitHub API once, not
    four times. Tests clear that cache to keep cases independent.
    """

    def setup_method(self):
        from agents.chronicler import scrapers
        scrapers._fetch_cirwel_traffic.cache_clear()

    def teardown_method(self):
        from agents.chronicler import scrapers
        scrapers._fetch_cirwel_traffic.cache_clear()

    @staticmethod
    def _gh_runner(repos, traffic):
        """Build a fake subprocess.run that answers `gh` calls.

        ``repos``: list of {"name": ..., "isArchived": ...} dicts.
        ``traffic``: {repo_name: {"views": (count, uniques), "clones": (count, uniques)}}
        """
        from subprocess import CompletedProcess

        def fake_run(cmd, *args, **kwargs):
            if cmd[:3] == ["gh", "repo", "list"]:
                return CompletedProcess(cmd, 0, stdout=json.dumps(repos), stderr="")
            if cmd[:2] == ["gh", "api"]:
                # cmd shape: ["gh", "api", "repos/CIRWEL/<name>/traffic/<kind>"]
                path = cmd[2]
                _, _, repo_name, _, kind = path.split("/")
                count, uniques = traffic[repo_name][kind]
                payload = {"count": count, "uniques": uniques, "views": [], "clones": []}
                return CompletedProcess(cmd, 0, stdout=json.dumps(payload), stderr="")
            raise AssertionError(f"unexpected gh call: {cmd}")

        return fake_run

    def test_aggregates_views_and_clones_across_repos(self):
        from agents.chronicler import scrapers

        repos = [
            {"name": "alpha", "isArchived": False},
            {"name": "beta", "isArchived": False},
        ]
        traffic = {
            "alpha": {"views": (100, 5), "clones": (40, 8)},
            "beta": {"views": (23, 2), "clones": (7, 3)},
        }
        with patch.object(scrapers.subprocess, "run", side_effect=self._gh_runner(repos, traffic)):
            result = scrapers._fetch_cirwel_traffic()

        assert result == {
            "views": 123,
            "views_uniques": 7,
            "clones": 47,
            "clones_uniques": 11,
        }

    def test_skips_archived_repos(self):
        from agents.chronicler import scrapers

        repos = [
            {"name": "alive", "isArchived": False},
            {"name": "frozen", "isArchived": True},
        ]
        traffic = {"alive": {"views": (10, 1), "clones": (2, 1)}}
        # Only `alive` should be queried; `frozen` would KeyError if asked.
        with patch.object(scrapers.subprocess, "run", side_effect=self._gh_runner(repos, traffic)):
            result = scrapers._fetch_cirwel_traffic()

        assert result == {
            "views": 10, "views_uniques": 1,
            "clones": 2, "clones_uniques": 1,
        }

    def test_caches_within_process_lifetime(self):
        from agents.chronicler import scrapers

        repos = [{"name": "x", "isArchived": False}]
        traffic = {"x": {"views": (1, 1), "clones": (1, 1)}}
        runner = MagicMock(side_effect=self._gh_runner(repos, traffic))
        with patch.object(scrapers.subprocess, "run", runner):
            scrapers._fetch_cirwel_traffic()
            scrapers._fetch_cirwel_traffic()

        # 1 repo-list call + (views, clones) per repo = 3 total. If the second
        # call re-fetched, we'd see 6.
        assert runner.call_count == 3

    def test_each_traffic_scraper_returns_its_dimension(self):
        from agents.chronicler import scrapers

        snapshot = {"views": 7, "views_uniques": 3, "clones": 12, "clones_uniques": 4}
        with patch.object(scrapers, "_fetch_cirwel_traffic", return_value=snapshot):
            assert scrapers.github_cirwel_traffic_views_14d(Path("/")) == 7.0
            assert scrapers.github_cirwel_traffic_views_uniques_14d(Path("/")) == 3.0
            assert scrapers.github_cirwel_traffic_clones_14d(Path("/")) == 12.0
            assert scrapers.github_cirwel_traffic_clones_uniques_14d(Path("/")) == 4.0

    def test_traffic_scrapers_registered(self):
        from agents.chronicler.scrapers import SCRAPERS

        for name in (
            "github.cirwel.traffic.views.14d",
            "github.cirwel.traffic.views.uniques.14d",
            "github.cirwel.traffic.clones.14d",
            "github.cirwel.traffic.clones.uniques.14d",
        ):
            assert name in SCRAPERS, f"{name} missing from SCRAPERS registry"

    def test_traffic_metrics_in_catalog(self):
        from src.fleet_metrics.catalog import catalog as _catalog

        for name in (
            "github.cirwel.traffic.views.14d",
            "github.cirwel.traffic.views.uniques.14d",
            "github.cirwel.traffic.clones.14d",
            "github.cirwel.traffic.clones.uniques.14d",
        ):
            assert name in _catalog, f"{name} missing from catalog"
            entry = _catalog[name]
            # Catch-future-self caveat: explicit window semantics in description.
            assert "rolling 14-day" in entry.description, (
                f"{name} description must say 'rolling 14-day' so the chart "
                f"isn't misread as a daily delta"
            )

    def test_scraper_failure_emits_error_metric_and_lands_in_catalog(self):
        """Regression for the .error gate: when a traffic scraper fails, the
        emitted `<name>.error` metric must be a registered catalog name so
        the POST is accepted (not 404'd) — this exercises both the new
        traffic surface and the auto-twin catalog fix."""
        from src.fleet_metrics.catalog import catalog as _catalog

        for name in (
            "github.cirwel.traffic.views.14d",
            "github.cirwel.traffic.views.uniques.14d",
            "github.cirwel.traffic.clones.14d",
            "github.cirwel.traffic.clones.uniques.14d",
        ):
            assert f"{name}.error" in _catalog


class TestChroniclerAsKnownResident:
    def test_chronicler_in_known_resident_labels(self):
        from src.grounding.class_indicator import KNOWN_RESIDENT_LABELS
        assert "Chronicler" in KNOWN_RESIDENT_LABELS

    def test_chronicler_silence_threshold_configured(self):
        from src.http_api import _DEFAULT_RESIDENT_SILENCE_SECONDS
        # Daily cadence → must be at least 24hr so a normal gap doesn't
        # get flagged as silence.
        assert _DEFAULT_RESIDENT_SILENCE_SECONDS["chronicler"] >= 24 * 3600
