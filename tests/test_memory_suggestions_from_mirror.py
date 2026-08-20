"""`memory_suggestions` must see the prior work the response already carries.

The envelope advertises `memory_suggestions` as the field an agent reads for
prior discoveries, and the plugin skills instruct agents to do exactly that. It
was empty on every check-in for two reasons, both on the reader side:

  * the check-in path's KG lookup reaches the response as `relevant_prior_work`
    (built by `_format_mirror` from the mirror's search), and nothing here
    looked at that key;
  * the other producer emits `{"message": ..., "discoveries": [...]}` while this
    required a bare list and dropped the dict on an isinstance check.

Neither needed a new lookup or a wider response — the content was already there.
"""

from __future__ import annotations

import pytest

from src.mcp_handlers.middleware.envelope_step import _memory_suggestions
from src.mcp_handlers.response_formatter import _format_mirror


class TestMemorySuggestionsReadsMirrorWork:
    def test_reads_relevant_prior_work(self):
        payload = {
            "relevant_prior_work": [
                {"discovery_id": "d1", "summary": "coherence gate soak", "relevance": 0.55},
                {"discovery_id": "d2", "summary": "lease deadlock", "relevance": 0.31},
            ]
        }
        got = _memory_suggestions(payload)
        assert [s["discovery_id"] for s in got] == ["d1", "d2"]

    def test_relevance_survives_the_lift(self):
        payload = {"relevant_prior_work": [{"discovery_id": "d1", "summary": "s", "relevance": 0.55}]}
        assert _memory_suggestions(payload)[0]["relevance"] == pytest.approx(0.55)

    def test_reads_the_dict_shaped_producer(self):
        payload = {
            "relevant_discoveries": {
                "message": "Found 1",
                "discoveries": [{"discovery_id": "d9", "summary": "prior art"}],
            }
        }
        assert _memory_suggestions(payload)[0]["discovery_id"] == "d9"

    def test_dict_producer_takes_precedence_over_mirror_work(self):
        payload = {
            "relevant_discoveries": {"discoveries": [{"discovery_id": "explicit", "summary": "a"}]},
            "relevant_prior_work": [{"discovery_id": "mirror", "summary": "b"}],
        }
        assert _memory_suggestions(payload)[0]["discovery_id"] == "explicit"

    def test_empty_dict_producer_falls_through_to_mirror_work(self):
        payload = {
            "relevant_discoveries": {"message": "Found 0", "discoveries": []},
            "relevant_prior_work": [{"discovery_id": "mirror", "summary": "b"}],
        }
        assert _memory_suggestions(payload)[0]["discovery_id"] == "mirror"

    def test_bare_list_still_works(self):
        payload = {"relevant_discoveries": [{"discovery_id": "d1", "summary": "s"}]}
        assert _memory_suggestions(payload)[0]["discovery_id"] == "d1"

    @pytest.mark.parametrize(
        "payload", [{}, {"relevant_prior_work": []}, {"relevant_discoveries": {}}]
    )
    def test_nothing_to_say_stays_absent(self, payload):
        assert _memory_suggestions(payload) is None


class TestMirrorEntryCarriesAnId:
    def _mirror(self, response_data):
        base = {"metrics": {}, "decision": {"action": "proceed"}}
        base.update(response_data)
        return _format_mirror(base, saved_trust_tier=None)

    def test_discovery_id_is_carried(self):
        """Without it the agent can read a summary but never open the entry."""
        out = self._mirror(
            {"_mirror_kg_results": [{"discovery_id": "d1", "summary": "s", "agent_id": "a"}]}
        )
        assert out["relevant_prior_work"][0]["discovery_id"] == "d1"

    def test_falls_back_to_plain_id(self):
        out = self._mirror({"_mirror_kg_results": [{"id": "d2", "summary": "s"}]})
        assert out["relevant_prior_work"][0]["discovery_id"] == "d2"

    def test_absent_id_is_omitted_not_faked(self):
        out = self._mirror({"_mirror_kg_results": [{"summary": "s"}]})
        assert "discovery_id" not in out["relevant_prior_work"][0]

    def test_dict_shaped_enrichment_is_merged_in(self):
        """This producer used to be dropped wholesale by an isinstance check."""
        out = self._mirror(
            {
                "relevant_discoveries": {
                    "message": "Found 1",
                    "discoveries": [{"discovery_id": "d3", "summary": "from enrichment"}],
                }
            }
        )
        assert [e["discovery_id"] for e in out["relevant_prior_work"]] == ["d3"]

    def test_both_producers_merge(self):
        out = self._mirror(
            {
                "_mirror_kg_results": [{"discovery_id": "m1", "summary": "mirror"}],
                "relevant_discoveries": {"discoveries": [{"discovery_id": "e1", "summary": "enr"}]},
            }
        )
        assert {e["discovery_id"] for e in out["relevant_prior_work"]} == {"m1", "e1"}
