#!/usr/bin/env python3
"""Pure protocol logic for the bounded read-only KG adoption pilot.

This module validates frozen task chains, constructs a deterministic
counterbalanced schedule, renders the four model-facing surface conditions,
normalizes retrieval results, and scores already-produced pilot receipts.  It
does not call a model, contact UNITARES, write audit rows, or mutate a workspace.

The protocol deliberately measures observable stages rather than agent intent:
catalog/context/result exposure -> reachability -> recording -> material use ->
objective outcome.  Post-withdrawal retrieval is secondary continuation
telemetry; it is never labelled voluntary, organic, or causal by itself.
"""

from __future__ import annotations

from collections import Counter, defaultdict
from copy import deepcopy
import hashlib
import json
import math
import re
from statistics import fmean
from typing import Any, Iterable, Mapping, Sequence


PROTOCOL_SCHEMA = "unitares.kg-agent-adoption.protocol.v0"
TASK_CHAIN_SCHEMA = "unitares.kg-agent-adoption.task-chains.v0"
ENROLLMENT_SCHEMA = "unitares.kg-agent-adoption.enrollment.v0"
RESULT_SCHEMA = "unitares.kg-agent-adoption.result.v0"

ARMS = (
    "unavailable",
    "passive",
    "surfaced_then_withdrawn",
    "injected",
)
BACKENDS = ("unitares_kg", "lexical_substitute")

# Unavailable has no backend.  Crossing the other three surface conditions
# with both retrieval backends yields seven unique cells.
EXPERIMENT_CELLS: tuple[tuple[str, str | None], ...] = (
    ("unavailable", None),
    ("passive", "unitares_kg"),
    ("passive", "lexical_substitute"),
    ("surfaced_then_withdrawn", "unitares_kg"),
    ("surfaced_then_withdrawn", "lexical_substitute"),
    ("injected", "unitares_kg"),
    ("injected", "lexical_substitute"),
)

PRIMARY_KG_CELL = "surfaced_then_withdrawn__unitares_kg"
PRIMARY_SUBSTITUTE_CELL = "surfaced_then_withdrawn__lexical_substitute"
UNAVAILABLE_CELL = "unavailable"

_TOKEN_RE = re.compile(r"[a-z0-9]+")
_RESPONSE_INSTRUCTION = (
    "Return exactly one JSON object with string field `answer`, array field "
    "`source_ids`, and short string field `reason`. Cite only source IDs that "
    "were actually delivered by the read-only prior-work tool or injected block."
)
_CONTEXTUAL_SURFACE = (
    "Relevant prior work may exist. A read-only `search_prior_work` tool is "
    "available for this task."
)


class ProtocolError(ValueError):
    """Raised when a task, schedule, receipt, or score violates the protocol."""


def canonical_json(value: Any) -> str:
    """Return stable compact JSON for hashing and comparison."""
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def sha256_json(value: Any) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def _stable_int(seed: int, *parts: object) -> int:
    joined = "|".join([str(seed), *(str(part) for part in parts)])
    return int.from_bytes(hashlib.sha256(joined.encode("utf-8")).digest()[:8], "big")


def cell_id(arm: str, backend: str | None) -> str:
    if arm not in ARMS:
        raise ProtocolError(f"unknown arm: {arm}")
    if arm == "unavailable":
        if backend is not None:
            raise ProtocolError("unavailable arm must not name a retrieval backend")
        return UNAVAILABLE_CELL
    if backend not in BACKENDS:
        raise ProtocolError(f"arm {arm} requires one registered backend")
    return f"{arm}__{backend}"


def experiment_cells() -> list[dict[str, Any]]:
    return [
        {"cell_id": cell_id(arm, backend), "arm": arm, "backend": backend}
        for arm, backend in EXPERIMENT_CELLS
    ]


def _require_exact_keys(
    value: Mapping[str, Any], required: set[str], optional: set[str], *, where: str
) -> None:
    missing = required - set(value)
    extra = set(value) - required - optional
    if missing:
        raise ProtocolError(f"{where} missing fields: {sorted(missing)}")
    if extra:
        raise ProtocolError(f"{where} has unregistered fields: {sorted(extra)}")


def _nonempty_string(value: Any, *, where: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ProtocolError(f"{where} must be a non-empty string")
    return value.strip()


def _string_list(value: Any, *, where: str, allow_empty: bool = False) -> list[str]:
    if not isinstance(value, list) or (not value and not allow_empty):
        requirement = "a list" if allow_empty else "a non-empty list"
        raise ProtocolError(f"{where} must be {requirement} of strings")
    if any(not isinstance(item, str) or not item.strip() for item in value):
        raise ProtocolError(f"{where} must contain only non-empty strings")
    normalized = [item.strip() for item in value]
    if len(normalized) != len(set(normalized)):
        raise ProtocolError(f"{where} must not contain duplicates")
    return normalized


def validate_task_chains(document: Mapping[str, Any]) -> dict[str, Any]:
    """Validate and return a defensive copy of the frozen task-chain document."""
    if not isinstance(document, Mapping):
        raise ProtocolError("task-chain document must be an object")
    _require_exact_keys(
        document,
        {"schema", "chains", "substitute_corpus"},
        {"description"},
        where="task-chain document",
    )
    if document.get("schema") != TASK_CHAIN_SCHEMA:
        raise ProtocolError(f"task-chain schema must be {TASK_CHAIN_SCHEMA}")

    corpus = document.get("substitute_corpus")
    if not isinstance(corpus, list) or not corpus:
        raise ProtocolError("substitute_corpus must be a non-empty list")
    source_ids: set[str] = set()
    for index, source in enumerate(corpus):
        where = f"substitute_corpus[{index}]"
        if not isinstance(source, Mapping):
            raise ProtocolError(f"{where} must be an object")
        _require_exact_keys(source, {"source_id", "title", "text"}, {"tags"}, where=where)
        source_id = _nonempty_string(source.get("source_id"), where=f"{where}.source_id")
        _nonempty_string(source.get("title"), where=f"{where}.title")
        _nonempty_string(source.get("text"), where=f"{where}.text")
        if source_id in source_ids:
            raise ProtocolError(f"duplicate substitute source_id: {source_id}")
        source_ids.add(source_id)
        if "tags" in source:
            _string_list(source["tags"], where=f"{where}.tags", allow_empty=True)

    chains = document.get("chains")
    if not isinstance(chains, list) or not chains:
        raise ProtocolError("chains must be a non-empty list")
    chain_ids: set[str] = set()
    global_step_ids: set[str] = set()
    for chain_index, chain in enumerate(chains):
        where = f"chains[{chain_index}]"
        if not isinstance(chain, Mapping):
            raise ProtocolError(f"{where} must be an object")
        _require_exact_keys(
            chain,
            {"chain_id", "family", "split", "steps"},
            set(),
            where=where,
        )
        chain_id = _nonempty_string(chain.get("chain_id"), where=f"{where}.chain_id")
        if chain_id in chain_ids:
            raise ProtocolError(f"duplicate chain_id: {chain_id}")
        chain_ids.add(chain_id)
        _nonempty_string(chain.get("family"), where=f"{where}.family")
        if chain.get("split") not in {"pilot", "canary"}:
            raise ProtocolError(f"{where}.split must be pilot or canary")
        steps = chain.get("steps")
        if not isinstance(steps, list) or len(steps) < 3:
            raise ProtocolError(f"{where}.steps must contain at least three steps")
        for step_index, step in enumerate(steps):
            step_where = f"{where}.steps[{step_index}]"
            if not isinstance(step, Mapping):
                raise ProtocolError(f"{step_where} must be an object")
            _require_exact_keys(
                step,
                {
                    "step_id",
                    "task",
                    "eligible_for_prior_work",
                    "injection_query",
                    "answer_key",
                },
                set(),
                where=step_where,
            )
            step_id = _nonempty_string(step.get("step_id"), where=f"{step_where}.step_id")
            if step_id in global_step_ids:
                raise ProtocolError(f"duplicate step_id: {step_id}")
            global_step_ids.add(step_id)
            _nonempty_string(step.get("task"), where=f"{step_where}.task")
            eligible = step.get("eligible_for_prior_work")
            if not isinstance(eligible, bool):
                raise ProtocolError(f"{step_where}.eligible_for_prior_work must be boolean")
            query = step.get("injection_query")
            if eligible:
                _nonempty_string(query, where=f"{step_where}.injection_query")
            elif query is not None:
                raise ProtocolError(
                    f"{step_where}.injection_query must be null when prior work is ineligible"
                )

            answer_key = step.get("answer_key")
            if not isinstance(answer_key, Mapping):
                raise ProtocolError(f"{step_where}.answer_key must be an object")
            _require_exact_keys(
                answer_key,
                {"accepted_answers", "forbidden_answers", "material_source_ids"},
                set(),
                where=f"{step_where}.answer_key",
            )
            accepted = set(
                _string_list(
                    answer_key.get("accepted_answers"),
                    where=f"{step_where}.answer_key.accepted_answers",
                )
            )
            forbidden = set(
                _string_list(
                    answer_key.get("forbidden_answers"),
                    where=f"{step_where}.answer_key.forbidden_answers",
                    allow_empty=True,
                )
            )
            material = set(
                _string_list(
                    answer_key.get("material_source_ids"),
                    where=f"{step_where}.answer_key.material_source_ids",
                    allow_empty=True,
                )
            )
            if accepted & forbidden:
                raise ProtocolError(f"{step_where} answer cannot be accepted and forbidden")
            unknown_sources = material - source_ids
            if unknown_sources:
                raise ProtocolError(
                    f"{step_where} names unknown material sources: {sorted(unknown_sources)}"
                )

    return deepcopy(dict(document))


def build_counterbalanced_schedule(
    document: Mapping[str, Any], *, assignment_seed: int, repetitions: int
) -> list[dict[str, Any]]:
    """Assign seven complete-chain cells in stable rotated/reversed blocks.

    The model sampling seed is shared by every cell for the same
    ``(chain_id, repetition)`` block.  A schedule row represents the complete
    task chain and carries every step ID; arms are never assigned per turn.
    """
    validated = validate_task_chains(document)
    if not isinstance(assignment_seed, int) or isinstance(assignment_seed, bool):
        raise ProtocolError("assignment_seed must be an integer")
    if not isinstance(repetitions, int) or isinstance(repetitions, bool) or repetitions <= 0:
        raise ProtocolError("repetitions must be a positive integer")

    cells = experiment_cells()
    schedule: list[dict[str, Any]] = []
    call_order = 0
    for repetition in range(1, repetitions + 1):
        for chain in sorted(validated["chains"], key=lambda item: item["chain_id"]):
            block_key = _stable_int(assignment_seed, chain["chain_id"], repetition)
            offset = block_key % len(cells)
            ordered = cells[offset:] + cells[:offset]
            if (block_key // len(cells)) % 2:
                ordered = list(reversed(ordered))
            shared_sample_seed = _stable_int(
                assignment_seed, "sample", chain["chain_id"], repetition
            ) & 0x7FFFFFFF
            block_id = f"{chain['chain_id']}--r{repetition:02d}"
            for position, cell in enumerate(ordered, start=1):
                call_order += 1
                schedule.append(
                    {
                        "call_order": call_order,
                        "block_id": block_id,
                        "within_block_order": position,
                        "chain_id": chain["chain_id"],
                        "family": chain["family"],
                        "split": chain["split"],
                        "repetition": repetition,
                        "cell_id": cell["cell_id"],
                        "arm": cell["arm"],
                        "backend": cell["backend"],
                        "step_ids": [step["step_id"] for step in chain["steps"]],
                        "sample_seed": shared_sample_seed,
                        "fresh_model_context_required": True,
                        "fresh_agent_identity_required": True,
                    }
                )
    return schedule


def schedule_digest(schedule: Sequence[Mapping[str, Any]]) -> str:
    return sha256_json(list(schedule))


def prior_work_tool_schema() -> dict[str, Any]:
    """Return the backend-neutral model-facing search tool contract."""
    return {
        "type": "function",
        "function": {
            "name": "search_prior_work",
            "description": (
                "Search read-only prior work relevant to the current task. "
                "Returns source IDs, titles, excerpts, and retrieval scores."
            ),
            "parameters": {
                "type": "object",
                "additionalProperties": False,
                "required": ["query"],
                "properties": {
                    "query": {"type": "string", "minLength": 1, "maxLength": 500}
                },
            },
        },
    }


def tools_for_arm(arm: str, backend: str | None = None) -> list[dict[str, Any]]:
    cell_id(arm, backend)
    return [] if arm == "unavailable" else [prior_work_tool_schema()]


def render_step_prompt(
    chain: Mapping[str, Any],
    step_index: int,
    *,
    arm: str,
    backend: str | None,
    injected_results: Sequence[Mapping[str, Any]] | None = None,
) -> dict[str, Any]:
    """Render one step and explicit exposure flags without contacting a backend."""
    cell = cell_id(arm, backend)
    steps = chain.get("steps")
    if not isinstance(steps, list) or not (0 <= step_index < len(steps)):
        raise ProtocolError("step_index is outside the task chain")
    step = steps[step_index]
    if not isinstance(step, Mapping):
        raise ProtocolError("task-chain step must be an object")
    eligible = bool(step.get("eligible_for_prior_work"))
    contextual_surface = arm == "surfaced_then_withdrawn" and step_index == 0 and eligible
    reminder_withdrawn = arm == "surfaced_then_withdrawn" and step_index > 0 and eligible
    result_injected = arm == "injected" and eligible

    lines = [str(step.get("task") or "").strip(), "", _RESPONSE_INSTRUCTION]
    if contextual_surface:
        lines.extend(["", _CONTEXTUAL_SURFACE])
    if result_injected:
        if injected_results is None:
            raise ProtocolError("injected arm requires explicit frozen-query results")
        normalized = normalize_prior_results(list(injected_results))
        lines.extend(
            [
                "",
                "Read-only prior-work results were automatically retrieved using the "
                "enrolled query:",
                json.dumps(normalized, indent=2, ensure_ascii=False),
            ]
        )
    elif injected_results:
        raise ProtocolError("non-injected arms must not receive injected results")

    return {
        "cell_id": cell,
        "step_id": step.get("step_id"),
        "prompt": "\n".join(lines),
        "tools": tools_for_arm(arm, backend),
        "exposure": {
            "eligible": eligible,
            "catalog_exposed": arm != "unavailable",
            "contextual_surface": contextual_surface,
            "reminder_withdrawn": reminder_withdrawn,
            "result_injected": result_injected,
        },
    }


def _extract_discovery_rows(raw: Any) -> list[Any]:
    if isinstance(raw, list):
        return raw
    if not isinstance(raw, Mapping):
        return []
    for key in ("discoveries", "results", "items"):
        if isinstance(raw.get(key), list):
            return list(raw[key])
    raw_governance = raw.get("raw_governance")
    if isinstance(raw_governance, Mapping):
        return _extract_discovery_rows(raw_governance)
    return []


def normalize_prior_results(raw: Any, *, limit: int = 5) -> list[dict[str, Any]]:
    """Normalize KG or substitute rows to one backend-neutral bounded shape."""
    if not isinstance(limit, int) or isinstance(limit, bool) or not 1 <= limit <= 10:
        raise ProtocolError("result limit must be an integer from 1 through 10")
    normalized: list[dict[str, Any]] = []
    seen: set[str] = set()
    for row in _extract_discovery_rows(raw):
        if not isinstance(row, Mapping):
            continue
        source = row.get("source_id") or row.get("discovery_id") or row.get("id")
        if not isinstance(source, str) or not source.strip() or source in seen:
            continue
        title = row.get("title") or row.get("summary") or source
        excerpt = row.get("excerpt") or row.get("snippet") or row.get("details") or row.get("text") or ""
        score = row.get("score", row.get("relevance", row.get("similarity", 0.0)))
        try:
            score_f = round(float(score), 6)
        except (TypeError, ValueError):
            score_f = 0.0
        normalized.append(
            {
                "source_id": source.strip(),
                "title": str(title).strip()[:240],
                "excerpt": str(excerpt).strip()[:1200],
                "score": score_f,
            }
        )
        seen.add(source)
        if len(normalized) >= limit:
            break
    return normalized


def _tokens(text: str) -> list[str]:
    return _TOKEN_RE.findall(text.lower())


def lexical_search(
    corpus: Sequence[Mapping[str, Any]], query: str, *, limit: int = 5
) -> list[dict[str, Any]]:
    """Run deterministic BM25-style retrieval over the frozen flat substitute."""
    query_tokens = _tokens(_nonempty_string(query, where="query"))
    if not query_tokens:
        return []
    if not corpus:
        return []
    documents: list[tuple[Mapping[str, Any], list[str]]] = []
    for index, row in enumerate(corpus):
        if not isinstance(row, Mapping):
            raise ProtocolError(f"corpus row {index} must be an object")
        source_id = _nonempty_string(row.get("source_id"), where=f"corpus[{index}].source_id")
        title = _nonempty_string(row.get("title"), where=f"corpus[{index}].title")
        text = _nonempty_string(row.get("text"), where=f"corpus[{index}].text")
        documents.append((dict(row, source_id=source_id, title=title, text=text), _tokens(f"{title} {text}")))
    avg_len = fmean(len(tokens) for _, tokens in documents) or 1.0
    doc_frequency = Counter(
        token for token in set(query_tokens) for _, tokens in documents if token in set(tokens)
    )
    n_docs = len(documents)
    k1, b = 1.5, 0.75
    scored: list[dict[str, Any]] = []
    for row, tokens in documents:
        counts = Counter(tokens)
        score = 0.0
        for token in query_tokens:
            tf = counts[token]
            if not tf:
                continue
            df = doc_frequency[token]
            inverse = math.log(1.0 + (n_docs - df + 0.5) / (df + 0.5))
            denom = tf + k1 * (1.0 - b + b * len(tokens) / avg_len)
            score += inverse * (tf * (k1 + 1.0) / denom)
        if score <= 0:
            continue
        scored.append(
            {
                "source_id": row["source_id"],
                "title": row["title"],
                "excerpt": row["text"][:1200],
                "score": round(score, 6),
            }
        )
    scored.sort(key=lambda item: (-item["score"], item["source_id"]))
    return normalize_prior_results(scored, limit=limit)


def score_step(
    step: Mapping[str, Any],
    response: Mapping[str, Any],
    *,
    delivered_source_ids: Iterable[str] = (),
) -> dict[str, Any]:
    """Score exact-answer quality and source-grounded material-use evidence."""
    if not isinstance(response, Mapping):
        raise ProtocolError("response must be an object")
    answer_key = step.get("answer_key")
    if not isinstance(answer_key, Mapping):
        raise ProtocolError("step has no valid answer_key")
    answer = response.get("answer")
    if not isinstance(answer, str) or not answer.strip():
        raise ProtocolError("response.answer must be a non-empty string")
    cited = _string_list(response.get("source_ids", []), where="response.source_ids", allow_empty=True)
    accepted = set(answer_key.get("accepted_answers") or [])
    forbidden = set(answer_key.get("forbidden_answers") or [])
    material = set(answer_key.get("material_source_ids") or [])
    delivered = {str(source_id) for source_id in delivered_source_ids}
    valid_citations = [source_id for source_id in cited if source_id in delivered]
    invalid_citations = [source_id for source_id in cited if source_id not in delivered]
    materially_used = sorted(set(valid_citations) & material)
    return {
        "answer": answer.strip(),
        "quality": 1.0 if answer.strip() in accepted else 0.0,
        "forbidden_answer": answer.strip() in forbidden,
        "regret": 1 if answer.strip() in forbidden else 0,
        "valid_source_ids": valid_citations,
        "invalid_source_ids": invalid_citations,
        "invalid_citation_count": len(invalid_citations),
        "material_source_ids": materially_used,
        "material_use": bool(materially_used),
    }


_COST_FIELDS = (
    "latency_ms",
    "input_tokens",
    "output_tokens",
    "tool_failures",
    "invalid_citations",
    "regret",
    "operator_interventions",
)


def compute_net_utility(
    quality: float,
    costs: Mapping[str, Any],
    weights: Mapping[str, Any],
) -> dict[str, Any]:
    """Apply enrolled non-negative cost weights; missing costs never become zero."""
    try:
        quality_f = float(quality)
    except (TypeError, ValueError) as exc:
        raise ProtocolError("quality must be numeric") from exc
    if not 0.0 <= quality_f <= 1.0:
        raise ProtocolError("quality must be within [0, 1]")
    missing = [field for field in _COST_FIELDS if field not in costs or costs[field] is None]
    weight_missing = [field for field in _COST_FIELDS if field not in weights or weights[field] is None]
    if weight_missing:
        raise ProtocolError(f"utility weights missing fields: {weight_missing}")
    parsed_weights: dict[str, float] = {}
    for field in _COST_FIELDS:
        try:
            parsed_weights[field] = float(weights[field])
        except (TypeError, ValueError) as exc:
            raise ProtocolError(f"utility weight {field} must be numeric") from exc
        if parsed_weights[field] < 0:
            raise ProtocolError(f"utility weight {field} must be non-negative")
    if missing:
        return {
            "quality": quality_f,
            "costs_complete": False,
            "missing_cost_fields": missing,
            "cost_penalty": None,
            "net_utility": None,
        }
    penalty = 0.0
    for field in _COST_FIELDS:
        try:
            value = float(costs[field])
        except (TypeError, ValueError) as exc:
            raise ProtocolError(f"cost {field} must be numeric") from exc
        if value < 0:
            raise ProtocolError(f"cost {field} must be non-negative")
        penalty += value * parsed_weights[field]
    return {
        "quality": quality_f,
        "costs_complete": True,
        "missing_cost_fields": [],
        "cost_penalty": round(penalty, 6),
        "net_utility": round(quality_f - penalty, 6),
    }


def _mean_or_none(values: Iterable[Any]) -> float | None:
    parsed = [float(value) for value in values if value is not None]
    return round(fmean(parsed), 6) if parsed else None


def _validate_result_rows(document: Mapping[str, Any]) -> list[dict[str, Any]]:
    if not isinstance(document, Mapping) or document.get("schema") != RESULT_SCHEMA:
        raise ProtocolError(f"result schema must be {RESULT_SCHEMA}")
    rows = document.get("rows")
    if not isinstance(rows, list):
        raise ProtocolError("result rows must be a list")
    required = {
        "chain_instance_id",
        "chain_id",
        "family",
        "repetition",
        "cell_id",
        "arm",
        "backend",
        "step_index",
        "eligible",
        "catalog_exposed",
        "contextual_surface",
        "reminder_withdrawn",
        "result_injected",
        "reachable",
        "recording_verified",
        "tool_invocations",
        "tool_successes",
        "material_use",
        "quality",
        "net_utility",
        "costs_complete",
    }
    normalized: list[dict[str, Any]] = []
    seen: set[tuple[str, int]] = set()
    for index, row in enumerate(rows):
        if not isinstance(row, Mapping):
            raise ProtocolError(f"result row {index} must be an object")
        missing = required - set(row)
        if missing:
            raise ProtocolError(f"result row {index} missing fields: {sorted(missing)}")
        expected_cell = cell_id(str(row["arm"]), row["backend"])
        if row["cell_id"] != expected_cell:
            raise ProtocolError(f"result row {index} cell_id does not match arm/backend")
        key = (str(row["chain_instance_id"]), int(row["step_index"]))
        if key in seen:
            raise ProtocolError(f"duplicate result step receipt: {key}")
        seen.add(key)
        normalized.append(dict(row))
    return normalized


def analyze_results(document: Mapping[str, Any]) -> dict[str, Any]:
    """Summarize the funnel and chain-level paired pilot contrasts."""
    rows = _validate_result_rows(document)
    surface = [
        row
        for row in rows
        if row["catalog_exposed"] or row["contextual_surface"] or row["result_injected"]
    ]
    funnel = {
        "step_receipts": len(rows),
        "eligible": sum(bool(row["eligible"]) for row in rows),
        "surfaced": len(surface),
        "reachable": sum(bool(row["reachable"]) for row in surface),
        "recorded": sum(bool(row["recording_verified"]) for row in surface),
        "materially_used": sum(bool(row["material_use"]) for row in surface),
        "outcome_scored": sum(row["quality"] is not None for row in rows),
        "net_utility_scored": sum(
            row["costs_complete"] and row["net_utility"] is not None for row in rows
        ),
    }

    by_cell_rows: dict[str, list[dict[str, Any]]] = defaultdict(list)
    by_chain_instance: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        by_cell_rows[row["cell_id"]].append(row)
        by_chain_instance[str(row["chain_instance_id"])].append(row)

    by_cell: list[dict[str, Any]] = []
    for cid in sorted(by_cell_rows):
        cell_rows = by_cell_rows[cid]
        by_cell.append(
            {
                "cell_id": cid,
                "arm": cell_rows[0]["arm"],
                "backend": cell_rows[0]["backend"],
                "step_receipts": len(cell_rows),
                "mean_quality": _mean_or_none(row["quality"] for row in cell_rows),
                "mean_net_utility": _mean_or_none(row["net_utility"] for row in cell_rows),
                "material_use_count": sum(bool(row["material_use"]) for row in cell_rows),
            }
        )

    chain_summaries: list[dict[str, Any]] = []
    for instance_id, instance_rows in sorted(by_chain_instance.items()):
        first = instance_rows[0]
        if any(row["cell_id"] != first["cell_id"] for row in instance_rows):
            raise ProtocolError(f"chain instance {instance_id} crossed experiment cells")
        chain_summaries.append(
            {
                "chain_instance_id": instance_id,
                "chain_id": first["chain_id"],
                "family": first["family"],
                "repetition": first["repetition"],
                "cell_id": first["cell_id"],
                "mean_quality": _mean_or_none(row["quality"] for row in instance_rows),
                "mean_net_utility": _mean_or_none(
                    row["net_utility"] for row in instance_rows
                ),
            }
        )

    block_values: dict[tuple[str, int], dict[str, float]] = defaultdict(dict)
    for chain in chain_summaries:
        if chain["mean_net_utility"] is not None:
            block_values[(str(chain["chain_id"]), int(chain["repetition"]))][
                str(chain["cell_id"])
            ] = float(chain["mean_net_utility"])

    def paired_delta(left: str, right: str) -> dict[str, Any]:
        deltas = [
            values[left] - values[right]
            for values in block_values.values()
            if left in values and right in values
        ]
        return {
            "left_cell": left,
            "right_cell": right,
            "paired_blocks": len(deltas),
            "mean_delta": round(fmean(deltas), 6) if deltas else None,
            "pilot_only": True,
        }

    continuation_eligible = 0
    continuation_later = 0
    for instance_rows in by_chain_instance.values():
        ordered = sorted(instance_rows, key=lambda row: int(row["step_index"]))
        if not ordered or ordered[0]["arm"] != "surfaced_then_withdrawn":
            continue
        first = ordered[0]
        later = [
            row for row in ordered[1:]
            if row["eligible"] and row["reminder_withdrawn"]
        ]
        if not (
            first["contextual_surface"]
            and first["reachable"]
            and first["recording_verified"]
            and int(first["tool_successes"]) > 0
            and later
        ):
            continue
        continuation_eligible += 1
        if any(int(row["tool_successes"]) > 0 for row in later):
            continuation_later += 1

    return {
        "schema": "unitares.kg-agent-adoption.summary.v0",
        "experiment_id": document.get("experiment_id"),
        "funnel": funnel,
        "by_cell": by_cell,
        "chain_instances": len(chain_summaries),
        "primary_contrasts": [
            paired_delta(PRIMARY_KG_CELL, PRIMARY_SUBSTITUTE_CELL),
            paired_delta(PRIMARY_KG_CELL, UNAVAILABLE_CELL),
        ],
        "post_withdrawal_retrieval": {
            "eligible_chain_instances": continuation_eligible,
            "later_successful_retrieval": continuation_later,
            "rate": (
                round(continuation_later / continuation_eligible, 6)
                if continuation_eligible
                else None
            ),
            "interpretation": "secondary continuation telemetry; not causal by itself",
        },
        "claim_status": "pilot_descriptive_only",
    }


__all__ = [
    "ARMS",
    "BACKENDS",
    "ENROLLMENT_SCHEMA",
    "EXPERIMENT_CELLS",
    "PROTOCOL_SCHEMA",
    "RESULT_SCHEMA",
    "TASK_CHAIN_SCHEMA",
    "ProtocolError",
    "analyze_results",
    "build_counterbalanced_schedule",
    "canonical_json",
    "cell_id",
    "compute_net_utility",
    "experiment_cells",
    "lexical_search",
    "normalize_prior_results",
    "prior_work_tool_schema",
    "render_step_prompt",
    "schedule_digest",
    "score_step",
    "sha256_json",
    "tools_for_arm",
    "validate_task_chains",
]
