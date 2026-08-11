#!/usr/bin/env python3
"""Reproduce and score the frozen dialectic reviewer-verdict cohort.

Issue #1585 cites a 2026-08-09 live-database review: 54 sessions since
2026-06-28, 53 reviewer verdicts, and 49 recorded rejections.  The declared
probe-family exclusion was based on the paused-agent label and left 21
sessions.  This script freezes both time bounds and that exclusion rule so the
denominators cannot drift as new canaries arrive.

The checked-in label file contains only derived route labels keyed by stable
case pseudonyms.  Raw session IDs, agent labels, topics, prompts, and reviewer
prose stay in PostgreSQL.  The pseudonyms are linkable by an operator with
database access; they are data minimization, not anonymization.

Usage:
    python3 scripts/dev/dialectic_verdict_labels.py --verify-baseline
    python3 scripts/dev/dialectic_verdict_labels.py --verify-baseline --json
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import statistics
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence


COHORT_ID = "dialectic-reviewer-verdicts-2026-08-09"
COHORT_START = datetime(2026, 6, 28, 6, 0, 0, tzinfo=timezone.utc)
COHORT_CUTOFF = datetime(2026, 8, 9, 19, 31, 50, tzinfo=timezone.utc)

# This is deliberately the rule used by the source analysis.  Do not expand it
# after looking at outcomes: changing an exclusion requires a new cohort ID.
PROBE_FAMILY_RE = re.compile(r"(probe|canary)|^RP[0-9]", re.IGNORECASE)

ROUTE_LABELS = frozenset({"deny", "cooldown", "needs_evidence", "human"})
CONFIDENCE_LABELS = frozenset({"high", "medium", "low"})
CORRECTNESS_LABELS = frozenset(
    {"unadjudicated", "false_block", "justified_block"}
)

REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_LABELS_PATH = (
    REPO_ROOT
    / "docs"
    / "operations"
    / "dialectic-reviewer-verdict-labels-2026-08-09.json"
)

EXPECTED_BASELINE = {
    "population_sessions": 54,
    "population_verdicts": 53,
    "population_rejected": 49,
    "population_approved": 4,
    "review_sessions": 21,
    "review_verdicts": 20,
    "review_rejected": 18,
    "review_approved": 2,
    "review_missing_verdict": 1,
    "self_clear_after_reject": 13,
    "reviewer_gap_median_ms": 42,
    "reviewer_gap_max_ms": 64,
}

_TRANSCRIPT_QUERY = """
    SELECT
        s.session_id,
        s.created_at,
        s.paused_agent_id,
        s.reviewer_agent_id,
        coalesce(pa.label, '') AS paused_label,
        m.message_id,
        m.agent_id AS message_agent_id,
        m.message_type,
        m.timestamp AS message_timestamp,
        m.agrees
    FROM core.dialectic_sessions s
    LEFT JOIN core.agents pa ON pa.id = s.paused_agent_id
    LEFT JOIN core.dialectic_messages m
      ON m.session_id = s.session_id
     AND m.timestamp <= %(cutoff)s
     AND m.message_type IN ('antithesis', 'synthesis')
    WHERE s.created_at >= %(start)s
      AND s.created_at <= %(cutoff)s
    ORDER BY s.created_at, s.session_id, m.timestamp, m.message_id
"""


@dataclass(frozen=True)
class MessageEvidence:
    message_id: int
    agent_id: str
    message_type: str
    timestamp: datetime
    agrees: bool | None


@dataclass
class SessionEvidence:
    session_id: str
    created_at: datetime
    paused_agent_id: str
    reviewer_agent_id: str | None
    paused_label: str
    messages: list[MessageEvidence] = field(default_factory=list)

    def reviewer_verdicts(self) -> list[MessageEvidence]:
        return [
            message
            for message in self.messages
            if message.message_type == "synthesis"
            and message.agent_id == self.reviewer_agent_id
        ]

    def reviewer_antitheses(self) -> list[MessageEvidence]:
        return [
            message
            for message in self.messages
            if message.message_type == "antithesis"
            and message.agent_id == self.reviewer_agent_id
        ]


def is_probe_family(paused_label: str) -> bool:
    """Return whether a paused-agent label matches the frozen exclusion."""

    return bool(PROBE_FAMILY_RE.search(paused_label or ""))


def case_id(session_id: str) -> str:
    """Return a stable, cohort-scoped pseudonym for a source session ID."""

    material = f"{COHORT_ID}\0{session_id}".encode("utf-8")
    return f"rv-{hashlib.sha256(material).hexdigest()[:12]}"


def _as_mapping(record: Any) -> Mapping[str, Any]:
    if isinstance(record, Mapping):
        return record
    raise TypeError(f"record must be a mapping, got {type(record).__name__}")


def build_sessions(records: Iterable[Mapping[str, Any]]) -> list[SessionEvidence]:
    """Group flat SQL rows into sessions without retaining transcript prose."""

    sessions: dict[str, SessionEvidence] = {}
    order: list[str] = []
    for raw_record in records:
        record = _as_mapping(raw_record)
        session_id = str(record["session_id"])
        session = sessions.get(session_id)
        if session is None:
            session = SessionEvidence(
                session_id=session_id,
                created_at=record["created_at"],
                paused_agent_id=str(record["paused_agent_id"]),
                reviewer_agent_id=(
                    str(record["reviewer_agent_id"])
                    if record.get("reviewer_agent_id") is not None
                    else None
                ),
                paused_label=str(record.get("paused_label") or ""),
            )
            sessions[session_id] = session
            order.append(session_id)
        else:
            stable_fields = (
                session.created_at == record["created_at"],
                session.paused_agent_id == str(record["paused_agent_id"]),
                session.reviewer_agent_id
                == (
                    str(record["reviewer_agent_id"])
                    if record.get("reviewer_agent_id") is not None
                    else None
                ),
                session.paused_label == str(record.get("paused_label") or ""),
            )
            if not all(stable_fields):
                raise ValueError(f"inconsistent session metadata for {session_id}")

        if record.get("message_id") is not None:
            session.messages.append(
                MessageEvidence(
                    message_id=int(record["message_id"]),
                    agent_id=str(record["message_agent_id"]),
                    message_type=str(record["message_type"]),
                    timestamp=record["message_timestamp"],
                    agrees=record.get("agrees"),
                )
            )

    return [sessions[session_id] for session_id in order]


def _one_verdict(session: SessionEvidence) -> MessageEvidence | None:
    verdicts = session.reviewer_verdicts()
    if len(verdicts) > 1:
        raise ValueError(
            f"{case_id(session.session_id)} has {len(verdicts)} reviewer verdicts; "
            "the frozen one-verdict-per-session cohort is no longer reproducible"
        )
    return verdicts[0] if verdicts else None


def _percent(numerator: int, denominator: int) -> float | None:
    return round(100.0 * numerator / denominator, 1) if denominator else None


def _latency_ms(session: SessionEvidence, verdict: MessageEvidence) -> int | None:
    antitheses = session.reviewer_antitheses()
    if not antitheses:
        return None
    antithesis = min(antitheses, key=lambda message: message.timestamp)
    if verdict.timestamp < antithesis.timestamp:
        return None
    return round((verdict.timestamp - antithesis.timestamp).total_seconds() * 1000)


def _has_later_self_clear(
    session: SessionEvidence, verdict: MessageEvidence
) -> bool:
    return any(
        message.message_type == "synthesis"
        and message.agent_id == session.paused_agent_id
        and message.agrees is True
        and message.timestamp > verdict.timestamp
        for message in session.messages
    )


def validate_annotations(
    document: Mapping[str, Any], rejected_sessions: Sequence[SessionEvidence]
) -> dict[str, Mapping[str, Any]]:
    """Validate completeness, enum values, and the privacy-safe label shape."""

    if document.get("schema_version") != 1:
        raise ValueError("label document schema_version must be 1")
    if document.get("cohort_id") != COHORT_ID:
        raise ValueError(f"label document cohort_id must be {COHORT_ID!r}")

    annotations = document.get("annotations")
    if not isinstance(annotations, list):
        raise ValueError("label document annotations must be a list")

    expected_ids = {case_id(session.session_id) for session in rejected_sessions}
    by_id: dict[str, Mapping[str, Any]] = {}
    forbidden_key_fragments = (
        "session",
        "agent",
        "topic",
        "prompt",
        "reasoning",
        "prose",
        "root_cause",
        "condition",
    )

    for raw_annotation in annotations:
        if not isinstance(raw_annotation, Mapping):
            raise ValueError("every annotation must be an object")
        annotation = dict(raw_annotation)
        annotation_id = annotation.get("case_id")
        if not isinstance(annotation_id, str) or not annotation_id:
            raise ValueError("every annotation needs a non-empty case_id")
        if annotation_id in by_id:
            raise ValueError(f"duplicate annotation for {annotation_id}")
        for key in annotation:
            lowered = key.lower()
            if any(fragment in lowered for fragment in forbidden_key_fragments):
                raise ValueError(
                    f"privacy-unsafe annotation field {key!r} on {annotation_id}"
                )

        route_label = annotation.get("route_label")
        if route_label not in ROUTE_LABELS:
            raise ValueError(
                f"invalid route_label {route_label!r} on {annotation_id}"
            )
        confidence = annotation.get("confidence")
        if confidence not in CONFIDENCE_LABELS:
            raise ValueError(
                f"invalid confidence {confidence!r} on {annotation_id}"
            )
        correctness = annotation.get("block_correctness")
        if correctness not in CORRECTNESS_LABELS:
            raise ValueError(
                f"invalid block_correctness {correctness!r} on {annotation_id}"
            )
        basis_codes = annotation.get("basis_codes")
        if (
            not isinstance(basis_codes, list)
            or not basis_codes
            or not all(
                isinstance(code, str) and re.fullmatch(r"[a-z0-9_]+", code)
                for code in basis_codes
            )
        ):
            raise ValueError(
                f"basis_codes must be non-empty snake_case strings on {annotation_id}"
            )
        by_id[annotation_id] = annotation

    actual_ids = set(by_id)
    if actual_ids != expected_ids:
        missing = sorted(expected_ids - actual_ids)
        extra = sorted(actual_ids - expected_ids)
        raise ValueError(f"annotation coverage mismatch: missing={missing}, extra={extra}")
    return by_id


def evaluate(
    sessions: Sequence[SessionEvidence],
    label_document: Mapping[str, Any],
) -> dict[str, Any]:
    """Compute frozen cohort accounting and semantic-routing metrics."""

    population_verdicts: list[MessageEvidence] = []
    for session in sessions:
        verdict = _one_verdict(session)
        if verdict is not None:
            population_verdicts.append(verdict)

    review_sessions = [
        session for session in sessions if not is_probe_family(session.paused_label)
    ]
    paired: list[tuple[SessionEvidence, MessageEvidence]] = []
    missing_verdict = 0
    for session in review_sessions:
        verdict = _one_verdict(session)
        if verdict is None:
            missing_verdict += 1
        else:
            paired.append((session, verdict))

    rejected_pairs = [pair for pair in paired if pair[1].agrees is False]
    approved_pairs = [pair for pair in paired if pair[1].agrees is True]
    unknown_pairs = [pair for pair in paired if pair[1].agrees is None]
    annotations = validate_annotations(
        label_document, [session for session, _ in rejected_pairs]
    )

    route_counts = Counter(
        annotations[case_id(session.session_id)]["route_label"]
        for session, _ in rejected_pairs
    )
    correctness_counts = Counter(
        annotations[case_id(session.session_id)]["block_correctness"]
        for session, _ in rejected_pairs
    )
    rejection_count = len(rejected_pairs)
    nonterminal_count = rejection_count - route_counts["deny"]
    retryable_count = route_counts["cooldown"] + route_counts["needs_evidence"]
    adjudicated_count = (
        correctness_counts["false_block"]
        + correctness_counts["justified_block"]
    )
    false_block_rate = (
        _percent(correctness_counts["false_block"], adjudicated_count)
        if adjudicated_count == rejection_count
        else None
    )

    latencies = [
        latency
        for session, verdict in paired
        if (latency := _latency_ms(session, verdict)) is not None
    ]
    self_clear_count = sum(
        _has_later_self_clear(session, verdict)
        for session, verdict in rejected_pairs
    )

    return {
        "cohort": {
            "id": COHORT_ID,
            "start": COHORT_START.isoformat(),
            "cutoff": COHORT_CUTOFF.isoformat(),
            "probe_family_exclusion": PROBE_FAMILY_RE.pattern,
        },
        "population": {
            "sessions": len(sessions),
            "verdicts": len(population_verdicts),
            "rejected": sum(
                verdict.agrees is False for verdict in population_verdicts
            ),
            "approved": sum(
                verdict.agrees is True for verdict in population_verdicts
            ),
            "unknown": sum(
                verdict.agrees is None for verdict in population_verdicts
            ),
            "rejection_pct": _percent(
                sum(verdict.agrees is False for verdict in population_verdicts),
                len(population_verdicts),
            ),
        },
        "review_cohort": {
            "sessions": len(review_sessions),
            "verdicts": len(paired),
            "rejected": len(rejected_pairs),
            "approved": len(approved_pairs),
            "unknown": len(unknown_pairs),
            "missing_verdict": missing_verdict,
            "self_clear_after_reject": self_clear_count,
            "self_clear_after_reject_pct": _percent(
                self_clear_count, rejection_count
            ),
            "reviewer_gap_median_ms": (
                round(statistics.median(latencies)) if latencies else None
            ),
            "reviewer_gap_max_ms": max(latencies) if latencies else None,
        },
        "routing": {
            "annotated_rejections": rejection_count,
            "counts": {
                label: route_counts[label] for label in sorted(ROUTE_LABELS)
            },
            "terminal_denial_mismatch": nonterminal_count,
            "terminal_denial_mismatch_pct": _percent(
                nonterminal_count, rejection_count
            ),
            "retryable_or_evidence_route": retryable_count,
            "retryable_or_evidence_route_pct": _percent(
                retryable_count, rejection_count
            ),
            "human_route": route_counts["human"],
            "human_route_pct": _percent(route_counts["human"], rejection_count),
            "block_correctness_counts": {
                label: correctness_counts[label]
                for label in sorted(CORRECTNESS_LABELS)
            },
            "false_block_pct": false_block_rate,
            "false_block_status": (
                "identified"
                if false_block_rate is not None
                else "not_identified_without_complete_independent_adjudication"
            ),
        },
        "privacy": {
            "artifact_contains_raw_session_ids": False,
            "artifact_contains_agent_labels_or_transcript": False,
            "case_ids_are_anonymous": False,
            "case_ids_are_linkable_pseudonyms": True,
        },
    }


def baseline_values(report: Mapping[str, Any]) -> dict[str, Any]:
    """Flatten the report fields pinned by the frozen source analysis."""

    population = report["population"]
    review = report["review_cohort"]
    return {
        "population_sessions": population["sessions"],
        "population_verdicts": population["verdicts"],
        "population_rejected": population["rejected"],
        "population_approved": population["approved"],
        "review_sessions": review["sessions"],
        "review_verdicts": review["verdicts"],
        "review_rejected": review["rejected"],
        "review_approved": review["approved"],
        "review_missing_verdict": review["missing_verdict"],
        "self_clear_after_reject": review["self_clear_after_reject"],
        "reviewer_gap_median_ms": review["reviewer_gap_median_ms"],
        "reviewer_gap_max_ms": review["reviewer_gap_max_ms"],
    }


def verify_baseline(report: Mapping[str, Any]) -> None:
    actual = baseline_values(report)
    if actual != EXPECTED_BASELINE:
        differences = {
            key: {"expected": EXPECTED_BASELINE[key], "actual": actual.get(key)}
            for key in EXPECTED_BASELINE
            if actual.get(key) != EXPECTED_BASELINE[key]
        }
        raise ValueError(f"frozen cohort baseline mismatch: {differences}")


def load_sessions(dsn: str | None = None) -> list[SessionEvidence]:
    """Read transcript metadata from PostgreSQL within the frozen time bounds."""

    import psycopg2  # type: ignore
    import psycopg2.extras  # type: ignore

    database_url = dsn or os.environ.get(
        "GOVERNANCE_DATABASE_URL",
        "postgresql://postgres:postgres@localhost:5432/governance",
    )
    with psycopg2.connect(database_url) as connection:
        with connection.cursor(
            cursor_factory=psycopg2.extras.RealDictCursor
        ) as cursor:
            cursor.execute(
                _TRANSCRIPT_QUERY,
                {"start": COHORT_START, "cutoff": COHORT_CUTOFF},
            )
            records = list(cursor.fetchall())
    return build_sessions(records)


def _human_report(report: Mapping[str, Any]) -> str:
    population = report["population"]
    review = report["review_cohort"]
    routing = report["routing"]
    counts = routing["counts"]
    return "\n".join(
        [
            f"Frozen reviewer-verdict cohort — {COHORT_ID}",
            (
                "  population: "
                f"{population['sessions']} sessions, {population['verdicts']} verdicts, "
                f"{population['rejected']} rejected ({population['rejection_pct']}%)"
            ),
            (
                "  after declared probe-family exclusion: "
                f"{review['sessions']} sessions, {review['verdicts']} verdicts "
                f"({review['rejected']} reject, {review['approved']} approve, "
                f"{review['missing_verdict']} missing)"
            ),
            (
                "  rejection routes: "
                f"deny={counts['deny']}, cooldown={counts['cooldown']}, "
                f"needs_evidence={counts['needs_evidence']}, human={counts['human']}"
            ),
            (
                "  terminal-denial mismatch: "
                f"{routing['terminal_denial_mismatch']}/{routing['annotated_rejections']} "
                f"({routing['terminal_denial_mismatch_pct']}%)"
            ),
            (
                "  operator route: "
                f"{routing['human_route']}/{routing['annotated_rejections']} "
                f"({routing['human_route_pct']}%)"
            ),
            (
                "  actual false-block rate: "
                f"{routing['false_block_status']}"
            ),
        ]
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--labels",
        type=Path,
        default=DEFAULT_LABELS_PATH,
        help="Privacy-minimized route-label JSON (default: checked-in artifact).",
    )
    parser.add_argument(
        "--dsn",
        default=None,
        help="PostgreSQL DSN; defaults to GOVERNANCE_DATABASE_URL.",
    )
    parser.add_argument("--json", action="store_true", help="Print JSON report.")
    parser.add_argument(
        "--verify-baseline",
        action="store_true",
        help="Fail unless the frozen source-analysis counts still reproduce.",
    )
    args = parser.parse_args()

    label_document = json.loads(args.labels.read_text(encoding="utf-8"))
    report = evaluate(load_sessions(args.dsn), label_document)
    if args.verify_baseline:
        verify_baseline(report)

    if args.json:
        print(json.dumps(report, indent=2, sort_keys=True))
    else:
        print(_human_report(report))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
