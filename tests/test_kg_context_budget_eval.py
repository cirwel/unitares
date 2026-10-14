"""The replay must exercise replacement pointers, not compare absent fields."""
import copy
import json

import pytest
import pytest_asyncio

from scripts.eval.kg_context_budget import capture, compare, visible_rows

pytestmark = pytest.mark.asyncio


@pytest_asyncio.fixture
async def captured(tmp_path):
    snapshot = tmp_path / "snapshot.json"
    snapshot.write_text(json.dumps({"documents": [{
        "id": "older", "agent_id": "writer", "type": "note", "summary": "Old decision",
        "status": "superseded", "created_at": "2026-10-14T00:00:00+00:00",
    }]}))
    successors = tmp_path / "successors.json"
    successors.write_text(json.dumps({"captured_at": "2026-10-14T01:00:00+00:00",
                                      "successors": {"older": ["newer"]}}))
    return await capture(snapshot, successors)


async def test_replay_populates_and_counts_replacement_pointers(captured):
    result = compare(captured, captured)
    assert result["visible_replacement_pointer_occurrences"] == {"before": 4, "after": 4}
    for case in captured["cases"][1:]:
        assert visible_rows(case["envelopes"]["lean"])[0]["superseded_by"] == "newer"


async def test_comparison_rejects_lost_replacement_pointer(captured):
    changed = copy.deepcopy(captured)
    visible_rows(changed["cases"][1]["envelopes"]["lean"])[0].pop("superseded_by")
    with pytest.raises(AssertionError):
        compare(captured, changed)


async def test_comparison_rejects_vacuous_replacement_coverage(captured):
    for case in captured["cases"]:
        for row in visible_rows(case["envelopes"]["lean"]):
            row.pop("superseded_by", None)
    with pytest.raises(AssertionError, match="must exercise visible replacement pointers"):
        compare(captured, captured)
