"""Tests for the descriptive harness census (src.identity.harness_census).

Pure aggregation over normalized S22 write-context rows — no DB. Fixtures are
DB-row-shaped dicts (``s22_context`` block), exercised through the real
normalize_s22_h5_entry path the live collector uses.
"""

from src.identity.harness_census import build_harness_census


def _row(entry_id, source, agent_id, recorded_at, ctx):
    return {
        "entry_id": entry_id,
        "source": source,
        "agent_id": agent_id,
        "recorded_at": recorded_at,
        "s22_context": ctx,
    }


# A small mixed population: two claude-code entries (one aliased "claude"), one
# codex, one hermes, and one row with no harness label at all.
ROWS = [
    _row("e1", "agent_state", "agent-A", "2026-06-01T00:00:00+00:00",
         {"harness_type": "claude-code", "transport": "cli", "model": "opus",
          "model_provider": "anthropic", "comparison_key": "k1"}),
    _row("e2", "agent_state", "agent-B", "2026-06-03T00:00:00+00:00",
         {"harness_type": "claude", "transport": "cli", "comparison_key": "k2"}),  # alias
    _row("e3", "knowledge_discovery", "agent-A", "2026-06-02T00:00:00+00:00",
         {"harness_type": "claude-code", "transport": "vscode", "model": "sonnet"}),
    _row("e4", "agent_state", "agent-C", "2026-06-04T00:00:00+00:00",
         {"harness_type": "codex-cli", "transport": "cli"}),
    _row("e5", "agent_state", "agent-D", "2026-06-05T00:00:00+00:00",
         {"harness": "hermes-agent", "model": "local"}),  # 'harness' alias + canonicalized
    _row("e6", "agent_state", "agent-E", "2026-06-06T00:00:00+00:00",
         {"transport": "cli"}),  # NO harness label → unattributed
]


def _census():
    return build_harness_census(ROWS)


class TestTotals:
    def test_entry_counts(self):
        c = _census()
        assert c["total_entries"] == 6
        assert c["unattributed_entries"] == 1
        assert c["attributed_entries"] == 5

    def test_distinct_harnesses_canonicalized(self):
        c = _census()
        # claude + claude-code collapse to one canonical harness.
        names = [h["canonical_harness"] for h in c["harnesses"]]
        assert names.count("claude-code") == 1
        assert set(names) == {"claude-code", "codex-cli", "hermes"}
        assert c["distinct_harnesses"] == 3


class TestPerHarnessRollup:
    def _claude(self):
        return next(h for h in _census()["harnesses"] if h["canonical_harness"] == "claude-code")

    def test_claude_aggregates_three_entries(self):
        h = self._claude()
        assert h["entry_count"] == 3
        assert h["distinct_agents"] == 2  # agent-A appears twice
        assert sorted(h["raw_harness_types"]) == ["claude", "claude-code"]

    def test_first_and_last_seen(self):
        h = self._claude()
        assert h["first_seen"] == "2026-06-01T00:00:00+00:00"
        assert h["last_seen"] == "2026-06-03T00:00:00+00:00"

    def test_sources_split(self):
        h = self._claude()
        assert h["sources"] == {"agent_state": 2, "knowledge_discovery": 1}

    def test_transports_and_models_deduped(self):
        h = self._claude()
        assert h["transports"] == ["cli", "vscode"]
        assert h["models"] == ["opus", "sonnet"]

    def test_distinct_comparison_keys(self):
        h = self._claude()
        assert h["distinct_comparison_keys"] == 2


class TestSituatingRatio:
    def test_ratio_reflects_metadata_coverage(self):
        c = _census()
        # codex-cli's single entry has only transport → situated.
        codex = next(h for h in c["harnesses"] if h["canonical_harness"] == "codex-cli")
        assert codex["situating_metadata_ratio"] == 1.0


class TestOrderingAndDeterminism:
    def test_sorted_by_entry_count_desc(self):
        names = [h["canonical_harness"] for h in _census()["harnesses"]]
        assert names[0] == "claude-code"  # 3 entries, the most

    def test_deterministic(self):
        assert build_harness_census(ROWS) == build_harness_census(ROWS)

    def test_empty_input(self):
        c = build_harness_census([])
        assert c["total_entries"] == 0
        assert c["distinct_harnesses"] == 0
        assert c["harnesses"] == []
