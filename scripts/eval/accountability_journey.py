#!/usr/bin/env python3
"""Evaluate one deterministic accountability-reconstruction rehearsal.

This is mechanism validation, not a run of the frozen accountable-testbed
preregistration.  Both arms must expose the same semantic fact and edge
manifest.  The evaluator checks exact reconstruction, attribution, and graph
completeness while reporting representation cost descriptively.
"""

from __future__ import annotations

import argparse
from copy import deepcopy
from dataclasses import dataclass
import hashlib
import json
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence


SCHEMA = "unitares.accountability-journey.v0"
RESULT_SCHEMA = "unitares.accountability-journey.result.v0"
RETRIEVAL_STAGE = "retrieval_control"
ARMS = ("unitares", "structured_handoff")
DEFAULT_FIXTURE = (
    Path(__file__).resolve().parents[2]
    / "tests/accountability_journey/incident-v0.json"
)


class JourneyError(ValueError):
    """Raised when the rehearsal fixture violates its declared contract."""


@dataclass(frozen=True, slots=True)
class ArmManifest:
    facts: dict[str, str]
    edges: frozenset[tuple[str, str, str]]
    attributions: dict[str, tuple[str, str]]
    fact_conflicts: tuple[str, ...]
    attribution_conflicts: tuple[str, ...]


def canonical_json(value: Any) -> str:
    """Return the stable encoding used for equality digests."""
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def sha256_json(value: Any) -> str:
    """Hash a JSON-compatible value using the canonical encoding."""
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def load_fixture(path: Path = DEFAULT_FIXTURE) -> dict[str, Any]:
    """Load and structurally validate a journey fixture."""
    try:
        fixture = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise JourneyError(f"cannot load fixture {path}: {exc}") from exc
    validate_fixture(fixture)
    return fixture


def _edge_tuple(edge: Mapping[str, Any]) -> tuple[str, str, str]:
    try:
        values = (edge["source"], edge["relation"], edge["target"])
    except KeyError as exc:
        raise JourneyError(f"edge is missing {exc.args[0]!r}") from exc
    if not all(isinstance(value, str) and value for value in values):
        raise JourneyError(
            "edge source, relation, and target must be non-empty strings"
        )
    return values


def _attribution_tuple(row: Mapping[str, Any]) -> tuple[str, tuple[str, str]]:
    required = ("effect_id", "process_id", "principal_id")
    if not all(isinstance(row.get(key), str) and row[key] for key in required):
        raise JourneyError("attribution fields must be non-empty strings")
    return row["effect_id"], (row["process_id"], row["principal_id"])


def arm_manifest(arm: Mapping[str, Any]) -> ArmManifest:
    """Collapse one representation into its semantic manifest."""
    facts: dict[str, str] = {}
    edges: set[tuple[str, str, str]] = set()
    attributions: dict[str, tuple[str, str]] = {}
    fact_conflicts: set[str] = set()
    attribution_conflicts: set[str] = set()

    records = arm.get("records")
    if not isinstance(records, list) or not records:
        raise JourneyError("each arm must contain at least one record")
    record_ids: set[str] = set()
    for record in records:
        if not isinstance(record, dict):
            raise JourneyError("arm records must be objects")
        record_id = record.get("record_id")
        if not isinstance(record_id, str) or not record_id:
            raise JourneyError("every arm record needs a non-empty record_id")
        if record_id in record_ids:
            raise JourneyError(f"duplicate record_id: {record_id}")
        record_ids.add(record_id)

        record_facts = record.get("facts", {})
        if not isinstance(record_facts, dict):
            raise JourneyError(f"record {record_id} facts must be an object")
        for key, value in record_facts.items():
            if not isinstance(key, str) or not isinstance(value, str):
                raise JourneyError("fact keys and values must be strings")
            if key in facts and facts[key] != value:
                fact_conflicts.add(key)
            else:
                facts[key] = value

        for edge in record.get("edges", []):
            edges.add(_edge_tuple(edge))

        for attribution in record.get("attributions", []):
            effect_id, actor = _attribution_tuple(attribution)
            if effect_id in attributions and attributions[effect_id] != actor:
                attribution_conflicts.add(effect_id)
            else:
                attributions[effect_id] = actor

    return ArmManifest(
        facts=facts,
        edges=frozenset(edges),
        attributions=attributions,
        fact_conflicts=tuple(sorted(fact_conflicts)),
        attribution_conflicts=tuple(sorted(attribution_conflicts)),
    )


def _oracle_manifest(fixture: Mapping[str, Any]) -> ArmManifest:
    oracle = fixture["oracle"]
    facts = oracle["facts"]
    return ArmManifest(
        facts=dict(facts),
        edges=frozenset(_edge_tuple(edge) for edge in oracle["edges"]),
        attributions=dict(_attribution_tuple(row) for row in oracle["attributions"]),
        fact_conflicts=(),
        attribution_conflicts=(),
    )


def _manifest_payload(manifest: ArmManifest) -> dict[str, Any]:
    return {
        "facts": manifest.facts,
        "edges": [
            {"source": source, "relation": relation, "target": target}
            for source, relation, target in sorted(manifest.edges)
        ],
        "attributions": [
            {
                "effect_id": effect_id,
                "process_id": actor[0],
                "principal_id": actor[1],
            }
            for effect_id, actor in sorted(manifest.attributions.items())
        ],
    }


def validate_fixture(fixture: Mapping[str, Any]) -> None:
    """Reject ambiguous fixtures before any comparative output is produced."""
    if fixture.get("schema") != SCHEMA:
        raise JourneyError(f"fixture schema must be {SCHEMA!r}")
    classification = fixture.get("classification", {})
    if not isinstance(classification, dict):
        raise JourneyError("classification must be an object")
    if classification.get("evidence_class") != "mechanism_validation":
        raise JourneyError("evidence_class must be mechanism_validation")
    if classification.get("evaluation_stage") != RETRIEVAL_STAGE:
        raise JourneyError(f"evaluation_stage must be {RETRIEVAL_STAGE}")
    if classification.get("headline_eligible") is not False:
        raise JourneyError("headline_eligible must be false")
    if classification.get("frozen_protocol_affected") is not False:
        raise JourneyError("frozen_protocol_affected must be false")
    if classification.get("information_equivalence_required") is not True:
        raise JourneyError("retrieval control requires information equivalence")
    if classification.get("natural_capture_evaluated") is not False:
        raise JourneyError("retrieval control cannot claim natural capture evaluation")

    oracle = fixture.get("oracle")
    if not isinstance(oracle, dict):
        raise JourneyError("fixture needs an oracle object")
    if not isinstance(oracle.get("facts"), dict) or not oracle["facts"]:
        raise JourneyError("oracle facts must be a non-empty object")
    if not isinstance(oracle.get("edges"), list) or not oracle["edges"]:
        raise JourneyError("oracle edges must be a non-empty list")
    if not isinstance(oracle.get("attributions"), list) or not oracle["attributions"]:
        raise JourneyError("oracle attributions must be a non-empty list")
    if not all(
        isinstance(key, str) and key and isinstance(value, str) and value
        for key, value in oracle["facts"].items()
    ):
        raise JourneyError("oracle fact keys and values must be non-empty strings")
    oracle_manifest = _oracle_manifest(fixture)
    if len(oracle_manifest.edges) != len(oracle["edges"]):
        raise JourneyError("oracle edges must be unique")
    if len(oracle_manifest.attributions) != len(oracle["attributions"]):
        raise JourneyError("oracle effect attributions must be unique")

    questions = fixture.get("questions")
    if not isinstance(questions, list) or not questions:
        raise JourneyError("fixture needs at least one reconstruction question")
    question_ids: set[str] = set()
    fact_keys: set[str] = set()
    for question in questions:
        if not isinstance(question, dict):
            raise JourneyError("reconstruction questions must be objects")
        question_id = question.get("question_id")
        fact_key = question.get("fact_key")
        if not isinstance(question_id, str) or not question_id:
            raise JourneyError("question_id must be a non-empty string")
        if question_id in question_ids:
            raise JourneyError(f"duplicate question_id: {question_id}")
        if fact_key not in oracle["facts"]:
            raise JourneyError(
                f"question {question_id} references unknown fact {fact_key!r}"
            )
        question_ids.add(question_id)
        fact_keys.add(fact_key)
    if fact_keys != set(oracle["facts"]):
        missing = sorted(set(oracle["facts"]) - fact_keys)
        raise JourneyError(f"questions do not cover every oracle fact: {missing}")

    arms = fixture.get("arms")
    if not isinstance(arms, dict) or set(arms) != set(ARMS):
        raise JourneyError(f"fixture arms must be exactly {ARMS}")
    for arm_name in ARMS:
        arm = arms[arm_name]
        if not isinstance(arm, dict):
            raise JourneyError(f"arm {arm_name} must be an object")
        if not isinstance(arm.get("representation"), str):
            raise JourneyError(f"arm {arm_name} needs a representation label")
        arm_manifest(arm)


def _reconstruction_steps(
    records: Sequence[Mapping[str, Any]], required_fact_keys: set[str]
) -> int:
    """Count record inspections until every reconstruction answer is available."""
    found: set[str] = set()
    for position, record in enumerate(records, start=1):
        found.update(set(record.get("facts", {})) & required_fact_keys)
        if found == required_fact_keys:
            return position
    return len(records)


def evaluate_arm(
    fixture: Mapping[str, Any], arm_name: str, oracle: ArmManifest
) -> dict[str, Any]:
    arm = fixture["arms"][arm_name]
    manifest = arm_manifest(arm)
    answers = {
        question["question_id"]: manifest.facts.get(question["fact_key"])
        for question in fixture["questions"]
    }
    expected_answers = {
        question["question_id"]: oracle.facts[question["fact_key"]]
        for question in fixture["questions"]
    }
    correct = sum(answers[key] == value for key, value in expected_answers.items())
    total = len(expected_answers)

    attribution_matches = sum(
        oracle.attributions.get(effect_id) == actor
        for effect_id, actor in manifest.attributions.items()
    )
    reported_attributions = len(manifest.attributions)
    attributed_oracle_effects = set(manifest.attributions) & set(oracle.attributions)
    missing_attributions = set(oracle.attributions) - set(manifest.attributions)
    unexpected_attributions = set(manifest.attributions) - set(oracle.attributions)
    incorrect_attributions = {
        effect_id
        for effect_id in attributed_oracle_effects
        if manifest.attributions[effect_id] != oracle.attributions[effect_id]
    }
    missing_edges = oracle.edges - manifest.edges
    unexpected_edges = manifest.edges - oracle.edges
    missing_facts = set(oracle.facts) - set(manifest.facts)
    unexpected_facts = set(manifest.facts) - set(oracle.facts)
    exact_manifest = _manifest_payload(manifest) == _manifest_payload(oracle)

    fixture_bytes = len(canonical_json(arm["records"]).encode("utf-8"))
    failures: list[str] = []
    if correct != total:
        failures.append(f"{total - correct} reconstruction answer(s) incorrect")
    if missing_facts:
        failures.append(f"{len(missing_facts)} oracle fact(s) missing")
    if unexpected_facts:
        failures.append(f"{len(unexpected_facts)} unexpected fact(s) present")
    if missing_attributions:
        failures.append(f"{len(missing_attributions)} effect attribution(s) missing")
    if unexpected_attributions:
        failures.append(
            f"{len(unexpected_attributions)} unexpected effect attribution(s) present"
        )
    if incorrect_attributions:
        failures.append(
            f"{len(incorrect_attributions)} effect attribution(s) incorrect"
        )
    if missing_edges:
        failures.append(f"{len(missing_edges)} oracle edge(s) missing")
    if unexpected_edges:
        failures.append(f"{len(unexpected_edges)} unexpected edge(s) present")
    if manifest.fact_conflicts:
        failures.append(f"conflicting facts: {list(manifest.fact_conflicts)}")
    if manifest.attribution_conflicts:
        failures.append(
            f"conflicting attributions: {list(manifest.attribution_conflicts)}"
        )

    return {
        "arm": arm_name,
        "representation": arm["representation"],
        "answers": answers,
        "metrics": {
            "reconstruction_accuracy": correct / total,
            "attribution_precision": (
                attribution_matches / reported_attributions
                if reported_attributions
                else None
            ),
            "attribution_coverage": (
                len(attributed_oracle_effects) / len(oracle.attributions)
            ),
            "missing_edge_rate": len(missing_edges) / len(oracle.edges),
            "record_count": len(arm["records"]),
            "record_inspection_steps": _reconstruction_steps(
                arm["records"], set(oracle.facts)
            ),
            "fixture_bytes": fixture_bytes,
        },
        "manifest": {
            "digest": sha256_json(_manifest_payload(manifest)),
            "exact_oracle_match": exact_manifest,
            "missing_facts": sorted(missing_facts),
            "unexpected_facts": sorted(unexpected_facts),
            "missing_edges": [list(edge) for edge in sorted(missing_edges)],
            "unexpected_edges": [list(edge) for edge in sorted(unexpected_edges)],
            "fact_conflicts": list(manifest.fact_conflicts),
            "attribution_conflicts": list(manifest.attribution_conflicts),
        },
        "failures": failures,
    }


def evaluate_journey(fixture: Mapping[str, Any]) -> dict[str, Any]:
    """Evaluate both information-equivalent representations."""
    validate_fixture(fixture)
    oracle = _oracle_manifest(fixture)
    arm_results = {
        arm_name: evaluate_arm(fixture, arm_name, oracle) for arm_name in ARMS
    }
    metrics = {name: result["metrics"] for name, result in arm_results.items()}
    deltas: dict[str, float | int | None] = {}
    for metric in (
        "reconstruction_accuracy",
        "attribution_precision",
        "attribution_coverage",
        "missing_edge_rate",
        "record_inspection_steps",
        "fixture_bytes",
    ):
        left = metrics["unitares"][metric]
        right = metrics["structured_handoff"][metric]
        deltas[metric] = None if left is None or right is None else left - right
    failures = [
        f"{arm_name}: {failure}"
        for arm_name, result in arm_results.items()
        for failure in result["failures"]
    ]
    semantic_equivalence = (
        arm_results["unitares"]["manifest"]["exact_oracle_match"]
        and arm_results["structured_handoff"]["manifest"]["exact_oracle_match"]
    )
    retrieval_status = "passed" if semantic_equivalence and not failures else "failed"
    return {
        "schema": RESULT_SCHEMA,
        "classification": deepcopy(fixture["classification"]),
        "incident": deepcopy(fixture["incident"]),
        "oracle_manifest_digest": sha256_json(_manifest_payload(oracle)),
        "semantic_equivalence": semantic_equivalence,
        "arms": arm_results,
        "descriptive_deltas_unitares_minus_handoff": deltas,
        "failures": failures,
        "stage_summary": {
            "capture_quality": {
                "status": "not_run",
                "question": "What does each system capture during natural use?",
                "required_input": "external oracle plus naturally produced records",
            },
            "retrieval_control": {
                "status": retrieval_status,
                "question": "Given equivalent facts, can each path reconstruct the incident?",
                "information_equivalence": semantic_equivalence,
            },
        },
        "interpretation": {
            "status": "retrieval_control_mechanism_validation",
            "comparative_claim": "not_evaluated",
            "reason": (
                "One deterministic, authored fixture can validate the evaluator "
                "and reconstruction path but cannot estimate real-world benefit."
            ),
        },
    }


def _metric(value: float | int | None) -> str:
    if value is None:
        return "N/A"
    if isinstance(value, float):
        return f"{value:.3f}"
    return str(value)


def render_report(result: Mapping[str, Any]) -> str:
    """Render the compact, reproducible failure/comparison report."""
    if result["semantic_equivalence"]:
        equivalence_statement = (
            "Both arms carry the same oracle fact, edge, and attribution manifest. "
            "This prevents a favorable result produced by withholding information "
            "from the Git plus structured-handoff baseline."
        )
    else:
        equivalence_statement = (
            "The arms do not carry the same oracle manifest. Comparative "
            "interpretation is invalid until the listed failures are repaired."
        )
    lines = [
        "# Accountability journey rehearsal — v0 result",
        "",
        "**Evidence class:** mechanism validation only.",
        "**Evaluation stage:** retrieval control (stage 2 of 2).",
        "**Headline comparison:** not evaluated.",
        "**Frozen preregistration:** unaffected.",
        "",
        f"Incident: {result['incident']['summary']}",
        "",
        equivalence_statement,
        "",
        "| Arm | Reconstruction accuracy | Attribution precision | Attribution coverage | Missing-edge rate | Records inspected | Fixture bytes |",
        "|---|---:|---:|---:|---:|---:|---:|",
    ]
    for arm_name in ARMS:
        arm = result["arms"][arm_name]
        metrics = arm["metrics"]
        lines.append(
            "| "
            + arm_name.replace("_", " ")
            + " | "
            + " | ".join(
                _metric(metrics[key])
                for key in (
                    "reconstruction_accuracy",
                    "attribution_precision",
                    "attribution_coverage",
                    "missing_edge_rate",
                    "record_inspection_steps",
                    "fixture_bytes",
                )
            )
            + " |"
        )
    lines.extend(["", "## Observed failures", ""])
    if result["failures"]:
        lines.extend(f"- {failure}" for failure in result["failures"])
    else:
        lines.append("None in the deterministic fixture.")
    lines.extend(
        [
            "",
            "## Two-stage evaluation",
            "",
            "1. **Capture quality — not run.** Let UNITARES and the operator's "
            "ordinary Git/handoff workflow record an incident naturally, then score "
            "both against an external oracle. Missing facts are findings, not a "
            "reason to equalize the inputs after the fact.",
            "2. **Retrieval control — this rehearsal.** Give both paths equivalent "
            "facts and test reconstruction. This isolates retrieval from capture.",
            "",
            "## Interpretation and limits",
            "",
            "The result establishes that the scenario, scorer, and both "
            "reconstruction paths preserve the declared incident facts. It does "
            "not establish that UNITARES improves outcomes, reduces reconstruction "
            "time, or outperforms a structured handoff in real work.",
            "",
            "The next step is stage 1 against actual retained UNITARES records and "
            "naturally produced Git/handoff artifacts. Only after capture coverage "
            "is measured should stage 2 compare retrieval effort. The frozen "
            "multi-scenario evaluation remains unchanged.",
            "",
            f"Oracle manifest SHA-256: `{result['oracle_manifest_digest']}`",
            "",
        ]
    )
    return "\n".join(lines)


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def main(argv: Iterable[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--fixture", type=Path, default=DEFAULT_FIXTURE)
    parser.add_argument("--output-json", type=Path)
    parser.add_argument("--output-report", type=Path)
    args = parser.parse_args(list(argv) if argv is not None else None)

    result = evaluate_journey(load_fixture(args.fixture))
    report = render_report(result)
    if args.output_json:
        _write(args.output_json, json.dumps(result, indent=2, sort_keys=True) + "\n")
    if args.output_report:
        _write(args.output_report, report)
    if not args.output_json and not args.output_report:
        print(report, end="")
    return 1 if result["failures"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
