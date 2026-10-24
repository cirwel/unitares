#!/usr/bin/env python3
"""Prove the quickstart's knowledge graph is a graph: stored findings get edges.

The Compose quickstart runs the AGE knowledge backend. This script checks that
end to end against a running stack:

1. Store two findings that share a tag, through REST ``store_finding``.
2. Find the first one again through ``search_shared_memory``.
3. Count, in AGE, the first finding's ``AUTHORED`` edge, its ``TAGGED`` edges,
   and the ``RELATED_TO`` edge that tag overlap draws from the second finding
   to the first.

Step 3 runs Cypher through ``psql`` in the Postgres container. Each query
returns one column, the shape ``graph_query`` requires (a two-column RETURN is
the defect class tests/test_age_query_single_column.py guards).

Usage (stack already up):

    python3 scripts/ci/kg_graph_proof.py

What this does not claim: semantic search, ranking quality, or supersession;
it proves the edges are written on a default install, nothing more.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
import urllib.request
import uuid

GRAPH = "governance_graph"


def call(url: str, tool: str, args: dict) -> dict:
    body = json.dumps({"name": tool, "arguments": args}).encode()
    req = urllib.request.Request(url, data=body, headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=30) as resp:
        payload = json.loads(resp.read())
    result = payload.get("result", payload)
    if isinstance(result, dict) and result.get("success") is False:
        raise SystemExit(f"{tool} failed: {json.dumps(result)[:600]}")
    return result


def cypher_count(container: str, query: str) -> int:
    sql = (
        "LOAD 'age'; SET search_path = ag_catalog, \"$user\", public; "
        f"SELECT * FROM cypher('{GRAPH}', $$ {query} $$) AS (n agtype);"
    )
    out = subprocess.run(
        ["docker", "exec", container, "psql", "-U", "postgres", "-d", "governance", "-tAc", sql],
        check=True, capture_output=True, text=True,
    ).stdout
    # LOAD and SET echo their command tags; the count is the last line.
    return int(out.strip().splitlines()[-1])


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--url", default="http://127.0.0.1:8767/v1/tools/call")
    parser.add_argument("--postgres-container", default="unitares-postgres-age")
    args = parser.parse_args()

    marker = f"kgproof{uuid.uuid4().hex[:10]}"
    session = call(args.url, "start_session", {"force_new": True, "name": "kg-graph-proof"})
    csid = session.get("client_session_id")
    if not csid:
        raise SystemExit(f"start_session returned no client_session_id: {json.dumps(session)[:600]}")

    first = call(args.url, "store_finding", {
        "client_session_id": csid,
        "summary": f"CI graph proof {marker}: first finding",
        "discovery_type": "note",
        "tags": [marker, "ci-graph-proof"],
    })
    second = call(args.url, "store_finding", {
        "client_session_id": csid,
        "summary": f"CI graph proof {marker}: second finding sharing the tag",
        "discovery_type": "note",
        "tags": [marker, "ci-graph-proof"],
    })
    first_id, second_id = first.get("discovery_id"), second.get("discovery_id")
    if not (first_id and second_id):
        raise SystemExit(f"store_finding returned no discovery_id: {first} / {second}")

    found = []
    for _ in range(10):
        search = call(args.url, "search_shared_memory", {"query": marker, "response_mode": "full"})
        payload = search.get("raw_governance", search)
        found = [d.get("id") for d in payload.get("discoveries", [])]
        if first_id in found:
            break
        time.sleep(1)
    if first_id not in found:
        raise SystemExit(f"search for {marker} did not return {first_id}: {found}")

    def ident(value: str) -> str:
        return value.replace("'", "")

    authored = cypher_count(
        args.postgres_container,
        f"MATCH (:Agent)-[r:AUTHORED]->(:Discovery {{id: '{ident(first_id)}'}}) RETURN count(r)",
    )
    tagged = cypher_count(
        args.postgres_container,
        f"MATCH (:Discovery {{id: '{ident(first_id)}'}})-[r:TAGGED]->(:Tag) RETURN count(r)",
    )
    related = cypher_count(
        args.postgres_container,
        f"MATCH (:Discovery {{id: '{ident(second_id)}'}})-[r:RELATED_TO]->"
        f"(:Discovery {{id: '{ident(first_id)}'}}) RETURN count(r)",
    )
    print(json.dumps({
        "first": first_id, "second": second_id,
        "authored": authored, "tagged": tagged, "related_to": related,
    }))
    failures = []
    if authored < 1:
        failures.append("no AUTHORED edge")
    if tagged < 2:
        failures.append(f"{tagged} TAGGED edges, expected 2")
    if related < 1:
        failures.append("no RELATED_TO edge from the second finding to the first")
    if failures:
        print("graph proof failed: " + "; ".join(failures), file=sys.stderr)
        return 1
    print("graph proof passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
