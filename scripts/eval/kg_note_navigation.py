#!/usr/bin/env python3
"""Replay KG note navigation on a private snapshot; export aggregates only."""
import argparse
import hashlib
import json
from pathlib import Path
import sys
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from src.mcp_handlers.knowledge import handlers  # noqa: E402
from src.mcp_handlers.middleware.envelope_step import build_experience_envelope  # noqa: E402
from src.storage.knowledge_graph_age import KnowledgeGraphAGE  # noqa: E402


def evaluate(path):
    raw_snapshot = Path(path).read_bytes()
    snapshot = json.loads(raw_snapshot)
    graph = KnowledgeGraphAGE()
    request = handlers._parse_knowledge_search_request({"query": "evidence"})
    counts = {"visible": 0, "with_notes": 0, "offsets_verified": 0,
              "single_digest_note_pointers": 0, "all_modes_equal": 0,
              "latest_beyond_default_page": 0, "latest_beyond_embedding_window": 0}
    sizes, pages = [], []
    # No storage reads or model calls. Frozen write-time attribution is kept;
    # a missing historical label uses this deterministic display fallback.
    with patch.object(handlers, "_resolve_agent_display", return_value={"display_name": "writer"}):
        for raw in snapshot["documents"]:
            if raw.get("status") in {"archived", "cold"}:
                continue
            counts["visible"] += 1
            doc = graph._dict_to_discovery(raw)
            state = handlers._KnowledgeSearchState(request=request, graph=None)
            state.results = [doc]
            rows = handlers._serialize_search_discoveries(state, include_details=False)
            if "resolution_notes" not in rows[0]:
                continue
            counts["with_notes"] += 1
            pointer = rows[0]["resolution_notes"]
            text = doc.details
            # Independent text-position checks on this frozen sample. A future
            # sample with malformed trailing headers may fail this stricter
            # check and must be inspected, not silently excluded.
            assert text[pointer["first_offset"]:].startswith("Resolution notes (")
            assert text[pointer["latest_offset"]:].startswith("Resolution notes (")
            assert pointer["latest_offset"] == text.rfind("Resolution notes (")
            counts["offsets_verified"] += 1
            counts["latest_beyond_default_page"] += pointer["latest_offset"] >= 2000
            counts["latest_beyond_embedding_window"] += pointer["latest_offset"] >= 6000
            payload = {"success": True, **handlers._base_search_response(
                state, rows, auto_details=False, include_details=False)}
            observed = []
            for mode in ("lean", "compact", "full"):
                source = handlers._lean_search_payload(payload) if mode == "lean" else payload
                env = build_experience_envelope("search_shared_memory", "knowledge", source,
                                                {"response_mode": mode})
                row = (env["raw_governance"]["discoveries"][0] if mode == "full"
                       else env["memory_suggestions"][0])
                observed.append(row.get("resolution_notes"))
                if mode == "lean":
                    counts["single_digest_note_pointers"] += bool(row.get("resolution_notes"))
                    sizes.append(len(json.dumps(env, ensure_ascii=False).encode()))
            counts["all_modes_equal"] += all(p == pointer for p in observed)
            pages.append(pointer["latest_offset"] // 2000 + 1)
    return {
        "snapshot_sha256": hashlib.sha256(raw_snapshot).hexdigest(),
        "captured_at": snapshot["captured_at"], "counts": counts,
        "single_lean_response_bytes": {"min": min(sizes, default=0), "max": max(sizes, default=0)},
        "sequential_pages_to_last_note_header": {"total": sum(pages), "max": max(pages, default=0)},
        "direct_offset_reads_to_last_note_header": len(pages),
        "scope": "Frozen navigation replay, not retrieval quality or agent comprehension. "
                 "Page counts compare sequential 2000-character pages to direct offsets; "
                 "larger pages or a calculated tail offset were already possible. "
                 "One page reaches a header, not necessarily all evidence. "
                 "Full mode here retains summaries, not inline details.",
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("snapshot", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    args.output.write_text(json.dumps(evaluate(args.snapshot), indent=2) + "\n")
