#!/usr/bin/env python3
"""Compare frozen KG read envelopes. Captures contain private corpus text."""
import argparse
import asyncio
import hashlib
import json
from pathlib import Path
import statistics
import sys
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from src.mcp_handlers.knowledge import handlers  # noqa: E402
from src.mcp_handlers.middleware.envelope_step import build_experience_envelope  # noqa: E402
from src.storage.knowledge_graph_age import KnowledgeGraphAGE  # noqa: E402


def wire(value):
    return len(json.dumps(value, ensure_ascii=False).encode("utf-8"))


async def capture(snapshot_path, successors_path):
    raw = Path(snapshot_path).read_bytes()
    snapshot = json.loads(raw)
    successor_raw = Path(successors_path).read_bytes()
    successor_snapshot = json.loads(successor_raw)

    class FrozenSuccessors:
        async def get_superseded_by(self, ids):
            return {key: value for key, value in successor_snapshot["successors"].items()
                    if key in ids}

    graph = KnowledgeGraphAGE()
    docs = [graph._dict_to_discovery(row) for row in snapshot["documents"]
            if row.get("status") not in {"archived", "cold"}]
    cases = [("empty", [], {})]
    for size, explicit in ((1, False), (3, False), (10, False), (10, True)):
        for start in range(0, len(docs), size):
            cases.append((f"{size}:{explicit}:{start}", docs[start:start + size],
                          {"limit": 10} if explicit else {}))
    results = []
    with patch.object(handlers, "_resolve_agent_display", return_value={"display_name": "writer"}):
        for name, rows, args in cases:
            state = handlers._KnowledgeSearchState(
                request=handlers._parse_knowledge_search_request({"query": "evidence"}), graph=None)
            state.results = rows
            serialized = handlers._serialize_search_discoveries(state, include_details=False)
            await handlers._annotate_supersession(serialized, FrozenSuccessors())
            payload = {"success": True, **handlers._base_search_response(
                state, serialized, auto_details=False, include_details=False)}
            modes = {}
            for mode in ("lean", "compact", "full"):
                source = handlers._lean_search_payload(payload) if mode == "lean" else payload
                modes[mode] = build_experience_envelope(
                    "search_shared_memory", "knowledge", source, {**args, "response_mode": mode})
            results.append({"case": name, "envelopes": modes})
    return {"snapshot_sha256": hashlib.sha256(raw).hexdigest(),
            "successors_sha256": hashlib.sha256(successor_raw).hexdigest(),
            "successors_captured_at": successor_snapshot["captured_at"],
            "visible_records": len(docs),
            "cases": results}


def visible_rows(envelope):
    return envelope.get("memory_suggestions", []) + envelope.get("more_results", [])


def compare(before, after):
    assert before["snapshot_sha256"] == after["snapshot_sha256"]
    assert before["successors_sha256"] == after["successors_sha256"]
    assert [c["case"] for c in before["cases"]] == [c["case"] for c in after["cases"]]
    sizes = {"before": [], "after": []}
    totals = {"before": 0, "after": 0}
    unchanged = {"compact": 0, "full": 0}
    lost_rows, changed_evidence = 0, 0
    successor_occurrences = {"before": 0, "after": 0}
    preserved = ("status", "superseded_by", "last_activity_days", "imported_memory",
                 "closure_class", "closure_standard", "resolution_notes", "agent_id", "by",
                 "relevance", "relevance_basis", "has_details")
    for old, new in zip(before["cases"], after["cases"], strict=True):
        for mode in unchanged:
            unchanged[mode] += old["envelopes"][mode] == new["envelopes"][mode]
        a, b = old["envelopes"]["lean"], new["envelopes"]["lean"]
        sizes["before"].append(wire(a))
        sizes["after"].append(wire(b))
        old_rows, new_rows = visible_rows(a), visible_rows(b)
        totals["before"] += len(old_rows)
        totals["after"] += len(new_rows)
        successor_occurrences["before"] += sum(bool(row.get("superseded_by")) for row in old_rows)
        successor_occurrences["after"] += sum(bool(row.get("superseded_by")) for row in new_rows)
        by_id = {row["discovery_id"]: row for row in new_rows}
        for row in old_rows:
            current = by_id.get(row["discovery_id"])
            if current is None:
                lost_rows += 1
                continue
            changed_evidence += any(current.get(key) != row[key] for key in preserved if key in row)
            # A formerly compacted summary may regain its longer preview.
            old_summary = row.get("summary", "").rstrip(".…")
            changed_evidence += not current.get("summary", "").startswith(old_summary)
            changed_evidence += any(tag not in current.get("tags", []) for tag in row.get("tags", []))
    count = len(before["cases"])
    assert not lost_rows and not changed_evidence, (lost_rows, changed_evidence)
    assert successor_occurrences["before"] > 0, "replay must exercise visible replacement pointers"
    assert unchanged == {"compact": count, "full": count}, unchanged
    result = {"snapshot_sha256": before["snapshot_sha256"],
              "successors_sha256": before["successors_sha256"],
              "successors_captured_at": before["successors_captured_at"],
              "visible_records": before["visible_records"],
              "cases": count, "mode_envelopes_identical": unchanged,
              "previously_visible_rows_lost": lost_rows, "changed_evidence_fields": changed_evidence,
              "visible_row_occurrences": totals,
              "visible_replacement_pointer_occurrences": successor_occurrences,
              "lean_wire_bytes": {}}
    for label, values in sizes.items():
        result["lean_wire_bytes"][label] = {"total": sum(values), "min": min(values),
            "median": statistics.median(values), "p95": sorted(values)[int((count - 1) * .95)],
            "max": max(values)}
    result["lean_wire_bytes"]["total_reduction_percent"] = round(
        100 * (1 - sum(sizes["after"]) / sum(sizes["before"])), 2)
    result["scope"] = ("Frozen single-record and contiguous 3/10-record projections, plus an empty result. "
                       "No query retrieval, model inference, live latency or agent-comprehension measurement. "
                       "Byte counts are UTF-8 JSON, not tokenizer estimates. Full mode excludes inline details. "
                       "The same record appears in multiple cases. Change-notice middleware is outside this replay.")
    return result


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="action", required=True)
    cap = sub.add_parser("capture", help="Write private envelopes; never publish this capture")
    cap.add_argument("snapshot", type=Path)
    cap.add_argument("--successors", type=Path, required=True, help="Frozen AGE successor sidecar")
    cap.add_argument("--output", type=Path, required=True)
    diff = sub.add_parser("compare", help="Export aggregate comparison only")
    diff.add_argument("before", type=Path)
    diff.add_argument("after", type=Path)
    diff.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    result = (asyncio.run(capture(args.snapshot, args.successors)) if args.action == "capture" else
              compare(json.loads(args.before.read_text()), json.loads(args.after.read_text())))
    args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n")
