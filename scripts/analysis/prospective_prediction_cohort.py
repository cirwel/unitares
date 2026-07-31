#!/usr/bin/env python3
"""Report prospective prediction-bound outcome cohorts.

This is a small holdout-oriented companion to the skeptical ablation matrix. It
counts only outcomes tied to a real prospective prediction registry binding,
not fallback confidence/audit bindings, so future validation can be separated
from retrospective or heuristic labels.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import math
import os
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Sequence

if __package__ is None or __package__ == "":
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from scripts.analysis.eisv_skeptic_report import (  # noqa: E402
    DEFAULT_DB_URL,
    STRICT_OUTCOMES,
    TASK_OUTCOMES,
    OutcomeRow,
    fetch_rows,
)
from scripts.analysis.outcome_inventory import (  # noqa: E402
    harness_lane_from_detail,
    is_controlled_validation_fixture,
)
from scripts.utils.date_utils import now_utc  # noqa: E402
from src.grounding.outcome_anchors import is_anchorable  # noqa: E402


CONTRACT_SCHEMA_VERSION = 1
CONTRACT_VERSION = "outcome-attested-prediction-binding-funnel-v1"
PREDICTION_CREATED_UNAVAILABLE_REASON = (
    "prediction registration is in-memory and not durably reconstructible"
)

# These strings are serialized verbatim into create-only cohort contracts.
# Change them only with CONTRACT_VERSION so a reader cannot silently
# reinterpret a frozen cohort under new selection semantics.
COHORT_PREDICATES = {
    "fetched_outcomes": (
        "outcome_type in selection.outcome_types and "
        "selection.as_of - selection.window_days <= ts <= selection.as_of and "
        "detail source is audit.outcome_events.detail (mutable identity metadata excluded)"
    ),
    "nonfixture_outcomes": (
        "not is_controlled_validation_fixture(detail, "
        "include_declared_purpose=True)"
    ),
    "trusted_outcomes": (
        "is_anchorable(verification_source, "
        "eisv_present=(snapshot_e is not None), "
        "snapshot_missing=(detail.get('snapshot_missing') is True), "
        "include_soft=False)"
    ),
    "prediction_id_presented": (
        "nonfixture_outcomes and detail.get('prediction_id') not in (None, '')"
    ),
    "registry_bound": (
        "prediction_id_presented and detail.get('prediction_binding') == 'registry'"
    ),
    "trusted_registry_bound": (
        "registry_bound and is_anchorable(verification_source, "
        "eisv_present=(snapshot_e is not None), "
        "snapshot_missing=(detail.get('snapshot_missing') is True), "
        "include_soft=False)"
    ),
    "prior_state_available": (
        "trusted_registry_bound and prior_state_age_seconds is not None"
    ),
    "accepted_outcome_attested": "prior_state_available",
}


class CohortContractError(ValueError):
    """Raised when a frozen cohort contract cannot be validated or replayed."""


@dataclass(frozen=True)
class OutcomeFunnel:
    """Monotonic outcome-side attrition counts from persisted fields only."""

    fetched_outcomes: int
    nonfixture_outcomes: int
    prediction_id_presented: int
    registry_bound: int
    trusted_registry_bound: int
    prior_state_available: int
    accepted_outcome_attested: int

    def __post_init__(self) -> None:
        """Reject negative or non-monotonic funnel counts."""

        if any(count < 0 for count in self.counts):
            raise ValueError("outcome funnel counts must be non-negative")
        if any(left < right for left, right in zip(self.counts, self.counts[1:])):
            raise ValueError("outcome funnel counts must be monotonic")

    @property
    def counts(self) -> tuple[int, ...]:
        """Return stage counts in contract order."""

        return (
            self.fetched_outcomes,
            self.nonfixture_outcomes,
            self.prediction_id_presented,
            self.registry_bound,
            self.trusted_registry_bound,
            self.prior_state_available,
            self.accepted_outcome_attested,
        )


@dataclass(frozen=True)
class ProspectiveCohortSummary:
    """Compact prospective prediction-bound cohort summary."""

    scope: str
    window_days: int
    lead_minutes: float
    total_outcomes: int
    prediction_bound: int
    prediction_bound_bad: int
    prediction_bound_prior_state: int
    by_harness_lane: dict[str, int]
    funnel: OutcomeFunnel | None = None
    as_of: datetime | None = None

    @property
    def prediction_coverage(self) -> float:
        """Share of trusted rows with registry-bound prospective predictions."""

        return self.prediction_bound / self.total_outcomes if self.total_outcomes else 0.0

    @property
    def prediction_bound_bad_rate(self) -> float:
        """Bad-outcome rate within the prospective prediction-bound cohort."""

        return (
            self.prediction_bound_bad / self.prediction_bound
            if self.prediction_bound
            else 0.0
        )

    @property
    def prediction_prior_state_coverage(self) -> float:
        """Prior-state coverage within the registry-bound prediction cohort."""

        return (
            self.prediction_bound_prior_state / self.prediction_bound
            if self.prediction_bound
            else 0.0
        )


@dataclass(frozen=True)
class ReadinessThresholds:
    """Minimum bar for calling a prospective prediction cohort strong."""

    min_prediction_bound: int = 100
    min_prediction_bound_bad: int = 10
    min_prediction_coverage: float = 0.05
    min_prediction_prior_state_coverage: float = 0.8


@dataclass(frozen=True)
class ValidationReadiness:
    """Readiness decision plus explicit unmet gates."""

    status: str
    reasons: tuple[str, ...]
    thresholds: ReadinessThresholds


def is_prospective_prediction_bound(row: OutcomeRow) -> bool:
    """True only for rows tied to a real registry prediction binding."""

    return (
        row.detail.get("prediction_id") not in (None, "")
        and row.detail.get("prediction_binding") == "registry"
    )


def is_trusted_outcome(row: OutcomeRow) -> bool:
    """Apply the canonical trusted, joinable outcome-anchor contract."""

    return is_anchorable(
        row.verification_source,
        eisv_present=row.snapshot_e is not None,
        snapshot_missing=row.detail.get("snapshot_missing") is True,
        include_soft=False,
    )


def build_cohort_summary(
    rows: Sequence[OutcomeRow],
    *,
    scope: str,
    window_days: int,
    lead_minutes: float,
    as_of: datetime | None = None,
) -> ProspectiveCohortSummary:
    """Summarize prospective prediction-bound rows without fallback leakage."""

    fetched = list(rows)
    nonfixture = [
        row
        for row in fetched
        if not is_controlled_validation_fixture(
            row.detail,
            include_declared_purpose=True,
        )
    ]
    trusted = [row for row in nonfixture if is_trusted_outcome(row)]
    prediction_id_rows = [
        row
        for row in nonfixture
        if row.detail.get("prediction_id") not in (None, "")
    ]
    registry_rows = [
        row for row in prediction_id_rows if is_prospective_prediction_bound(row)
    ]
    prediction_rows = [row for row in registry_rows if is_trusted_outcome(row)]
    prior_state_rows = [
        row for row in prediction_rows if row.prior_state_age_seconds is not None
    ]
    funnel = OutcomeFunnel(
        fetched_outcomes=len(fetched),
        nonfixture_outcomes=len(nonfixture),
        prediction_id_presented=len(prediction_id_rows),
        registry_bound=len(registry_rows),
        trusted_registry_bound=len(prediction_rows),
        prior_state_available=len(prior_state_rows),
        accepted_outcome_attested=len(prior_state_rows),
    )
    by_lane: dict[str, int] = {}
    for row in prediction_rows:
        lane = harness_lane_from_detail(row.detail)
        by_lane[lane] = by_lane.get(lane, 0) + 1
    return ProspectiveCohortSummary(
        scope=scope,
        window_days=window_days,
        lead_minutes=lead_minutes,
        total_outcomes=len(trusted),
        prediction_bound=len(prediction_rows),
        prediction_bound_bad=sum(int(row.is_bad) for row in prediction_rows),
        prediction_bound_prior_state=len(prior_state_rows),
        by_harness_lane=dict(sorted(by_lane.items())),
        funnel=funnel,
        as_of=as_of,
    )


def _fmt_lead(value: float) -> str:
    return f"{value:g}"


def _fmt_as_of(value: datetime | None) -> str:
    """Format an explicit UTC boundary or describe the compatible live default."""

    return _canonical_datetime(value) if value is not None else "database_now_at_query"


def evaluate_readiness(
    summary: ProspectiveCohortSummary,
    thresholds: ReadinessThresholds | None = None,
) -> ValidationReadiness:
    """Evaluate whether a prospective prediction cohort is strong enough.

    This is a data-quality gate, not an EISV validation claim. It separates
    registry-bound prospective volume, negative-class coverage, prediction
    coverage, and prior-state coverage so weak datasets fail explicitly.
    """

    thresholds = thresholds or ReadinessThresholds()
    reasons: list[str] = []
    if summary.prediction_bound < thresholds.min_prediction_bound:
        reasons.append(
            f"prediction_bound {summary.prediction_bound} < {thresholds.min_prediction_bound}"
        )
    if summary.prediction_bound_bad < thresholds.min_prediction_bound_bad:
        reasons.append(
            "prediction_bound_bad "
            f"{summary.prediction_bound_bad} < {thresholds.min_prediction_bound_bad}"
        )
    if summary.prediction_coverage < thresholds.min_prediction_coverage:
        reasons.append(
            "prediction_coverage "
            f"{summary.prediction_coverage:.3f} < {thresholds.min_prediction_coverage:.3f}"
        )
    if summary.prediction_prior_state_coverage < thresholds.min_prediction_prior_state_coverage:
        reasons.append(
            "prediction_prior_state_coverage "
            f"{summary.prediction_prior_state_coverage:.3f} < "
            f"{thresholds.min_prediction_prior_state_coverage:.3f}"
        )
    return ValidationReadiness(
        status="not_ready" if reasons else "strong",
        reasons=tuple(reasons),
        thresholds=thresholds,
    )


def format_cohort_report(
    summary: ProspectiveCohortSummary,
    *,
    thresholds: ReadinessThresholds | None = None,
) -> str:
    """Render a markdown summary for prospective holdout readiness."""

    readiness = evaluate_readiness(summary, thresholds)
    threshold_text = (
        f"min_prediction_bound={readiness.thresholds.min_prediction_bound}, "
        f"min_prediction_bound_bad={readiness.thresholds.min_prediction_bound_bad}, "
        f"min_prediction_coverage={readiness.thresholds.min_prediction_coverage:.3f}, "
        "min_prediction_prior_state_coverage="
        f"{readiness.thresholds.min_prediction_prior_state_coverage:.3f}"
    )
    readiness_lines = [
        f"readiness: {readiness.status}",
        f"readiness_thresholds: {threshold_text}",
    ]
    if readiness.reasons:
        readiness_lines.append("readiness_reasons:")
        readiness_lines.extend(f"- {reason}" for reason in readiness.reasons)
    else:
        readiness_lines.append("readiness_reasons: none")

    lanes = ",".join(
        f"{lane}={count}" for lane, count in summary.by_harness_lane.items()
    ) or "none"
    if summary.funnel is None:
        funnel_lines = ["outcome_funnel: unavailable_for_legacy_summary"]
    else:
        funnel = summary.funnel
        funnel_lines = [
            f"fetched_outcomes: {funnel.fetched_outcomes}",
            f"nonfixture_outcomes: {funnel.nonfixture_outcomes}",
            f"prediction_id_presented: {funnel.prediction_id_presented}",
            f"registry_bound: {funnel.registry_bound}",
            f"trusted_registry_bound: {funnel.trusted_registry_bound}",
            f"prior_state_available: {funnel.prior_state_available}",
            "accepted_outcome_attested: "
            f"{funnel.accepted_outcome_attested}",
        ]
    return "\n".join(
        [
            "# Outcome-Attested Prediction-Binding Cohort",
            "",
            f"scope: {summary.scope}",
            f"window_days: {summary.window_days}",
            f"lead_minutes: {_fmt_lead(summary.lead_minutes)}",
            f"as_of: {_fmt_as_of(summary.as_of)}",
            f"outcome_funnel_contract: {CONTRACT_VERSION}",
            "contract_integrity: self-contained consistency checksum; "
            "Git history can provide an external anchor, but the checksum alone "
            "does not prove preregistration",
            "",
            "## Outcome binding funnel",
            "",
            *funnel_lines,
            "",
            "## Side statistics",
            "",
            f"trusted_outcomes: {summary.total_outcomes}",
            "prediction_created: unavailable",
            f"prediction_created_reason: {PREDICTION_CREATED_UNAVAILABLE_REASON}",
            f"prediction_bound: {summary.prediction_bound}",
            f"prediction_coverage: {summary.prediction_coverage:.3f}",
            f"prediction_bound_bad: {summary.prediction_bound_bad}",
            f"prediction_bound_bad_rate: {summary.prediction_bound_bad_rate:.3f}",
            "prediction_bound_prior_state: "
            f"{summary.prediction_bound_prior_state}/{summary.prediction_bound}",
            f"prediction_prior_state_coverage: {summary.prediction_prior_state_coverage:.3f}",
            f"harness_lanes: {lanes}",
            "",
            *readiness_lines,
            "",
            "Interpretation rule: these are outcome-attested registry bindings. "
            "This does not establish when the prediction was created, so these "
            "rows are not accepted as prospective validation. EISV remains online "
            "agent-state estimation (agent proprioception), not an outcome oracle "
            "or bad-verdict dispenser; external labels still own outcome truth.",
        ]
    )


def _outcome_types_for_scope(scope: str) -> tuple[str, ...]:
    """Return the exact persisted outcome types selected by a scope."""

    if scope == "strict":
        return tuple(STRICT_OUTCOMES)
    if scope == "task":
        return tuple(TASK_OUTCOMES)
    raise CohortContractError(f"unsupported cohort scope: {scope!r}")


def _canonical_datetime(value: datetime) -> str:
    """Return one stable UTC ISO-8601 representation for contract boundaries."""

    if value.tzinfo is None or value.utcoffset() is None:
        raise CohortContractError("as_of must include a timezone")
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _datetime_from_contract(value: object) -> datetime:
    """Parse and validate a contract's timezone-aware ISO-8601 boundary."""

    if not isinstance(value, str):
        raise CohortContractError("selection.as_of must be an ISO-8601 string")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise CohortContractError("selection.as_of is not valid ISO-8601") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise CohortContractError("selection.as_of must include a timezone")
    return parsed.astimezone(timezone.utc)


def _canonical_json(payload: Mapping[str, Any]) -> str:
    """Serialize a JSON mapping deterministically for hashing."""

    return json.dumps(
        payload,
        allow_nan=False,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )


def with_cohort_contract_digest(
    contract: Mapping[str, Any],
) -> dict[str, Any]:
    """Return a JSON-safe contract copy with its deterministic SHA-256 digest."""

    unsigned = {key: value for key, value in contract.items() if key != "digest"}
    normalized = json.loads(_canonical_json(unsigned))
    digest = hashlib.sha256(_canonical_json(normalized).encode("utf-8")).hexdigest()
    return {
        **normalized,
        "digest": {
            "algorithm": "sha256",
            "value": digest,
        },
    }


def build_cohort_contract(
    *,
    scope: str,
    window_days: int,
    lead_minutes: float,
    as_of: datetime,
) -> dict[str, Any]:
    """Build a frozen prospective-cohort selection contract."""

    outcome_types = _outcome_types_for_scope(scope)
    if type(window_days) is not int or window_days <= 0:
        raise CohortContractError("window_days must be a positive integer")
    if isinstance(lead_minutes, bool) or not isinstance(lead_minutes, (int, float)):
        raise CohortContractError("lead_minutes must be numeric")
    if not math.isfinite(float(lead_minutes)) or float(lead_minutes) < 0:
        raise CohortContractError("lead_minutes must be finite and non-negative")
    contract = {
        "schema_version": CONTRACT_SCHEMA_VERSION,
        "contract_version": CONTRACT_VERSION,
        "selection": {
            "scope": scope,
            "window_days": window_days,
            "lead_minutes": float(lead_minutes),
            "as_of": _canonical_datetime(as_of),
            "outcome_types": list(outcome_types),
        },
        "predicates": dict(COHORT_PREDICATES),
    }
    return with_cohort_contract_digest(contract)


def _validate_contract_schema(contract: object) -> dict[str, Any]:
    """Validate schema, frozen predicates, selection, and digest."""

    if not isinstance(contract, dict):
        raise CohortContractError("cohort contract root must be a JSON object")
    expected_keys = {
        "schema_version",
        "contract_version",
        "selection",
        "predicates",
        "digest",
    }
    if set(contract) != expected_keys:
        raise CohortContractError("cohort contract top-level schema drift")
    if (
        type(contract["schema_version"]) is not int
        or contract["schema_version"] != CONTRACT_SCHEMA_VERSION
    ):
        raise CohortContractError(
            "unsupported schema_version: "
            f"{contract['schema_version']!r} != {CONTRACT_SCHEMA_VERSION}"
        )
    if (
        not isinstance(contract["contract_version"], str)
        or contract["contract_version"] != CONTRACT_VERSION
    ):
        raise CohortContractError(
            "unsupported contract_version: "
            f"{contract['contract_version']!r} != {CONTRACT_VERSION!r}"
        )

    selection = contract["selection"]
    if not isinstance(selection, dict) or set(selection) != {
        "scope",
        "window_days",
        "lead_minutes",
        "as_of",
        "outcome_types",
    }:
        raise CohortContractError("selection schema drift")
    scope = selection["scope"]
    if not isinstance(scope, str):
        raise CohortContractError("selection.scope must be a string")
    expected_outcome_types = list(_outcome_types_for_scope(scope))
    if selection["outcome_types"] != expected_outcome_types:
        raise CohortContractError("selection outcome_types contract drift")
    window_days = selection["window_days"]
    if type(window_days) is not int or window_days <= 0:
        raise CohortContractError("selection.window_days must be a positive integer")
    lead_minutes = selection["lead_minutes"]
    if type(lead_minutes) is not float:
        raise CohortContractError("selection.lead_minutes must be a JSON number")
    if not math.isfinite(lead_minutes) or lead_minutes < 0:
        raise CohortContractError(
            "selection.lead_minutes must be finite and non-negative"
        )
    parsed_as_of = _datetime_from_contract(selection["as_of"])
    if selection["as_of"] != _canonical_datetime(parsed_as_of):
        raise CohortContractError("selection.as_of must use canonical UTC form")

    predicates = contract["predicates"]
    if not isinstance(predicates, dict) or predicates != COHORT_PREDICATES:
        raise CohortContractError("predicate contract drift")

    digest = contract["digest"]
    if not isinstance(digest, dict) or set(digest) != {"algorithm", "value"}:
        raise CohortContractError("digest schema drift")
    digest_value = digest["value"]
    if digest["algorithm"] != "sha256" or not isinstance(digest_value, str):
        raise CohortContractError("digest must use SHA-256")
    if len(digest_value) != 64 or any(
        character not in "0123456789abcdef" for character in digest_value
    ):
        raise CohortContractError("digest must be 64 lowercase SHA-256 hex characters")
    expected_digest = with_cohort_contract_digest(contract)["digest"]["value"]
    if digest_value != expected_digest:
        raise CohortContractError("cohort contract digest mismatch")
    return contract


def write_cohort_contract(
    path: Path,
    contract: Mapping[str, Any],
) -> None:
    """Create a validated JSON contract exclusively with overwrite refusal."""

    validated = _validate_contract_schema(dict(contract))
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("x", encoding="utf-8") as handle:
        json.dump(validated, handle, indent=2, sort_keys=True)
        handle.write("\n")


def read_cohort_contract(
    path: Path,
    *,
    scope: str | None = None,
    window_days: int | None = None,
    lead_minutes: float | None = None,
    as_of: datetime | None = None,
) -> dict[str, Any]:
    """Read a contract and reject integrity, schema, or explicit CLI drift."""

    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise CohortContractError("cohort contract is not valid JSON") from exc
    contract = _validate_contract_schema(raw)
    selection = contract["selection"]
    cli_values = {
        "scope": scope,
        "window_days": window_days,
        "lead_minutes": float(lead_minutes) if lead_minutes is not None else None,
        "as_of": _canonical_datetime(as_of) if as_of is not None else None,
    }
    for name, cli_value in cli_values.items():
        if cli_value is not None and cli_value != selection[name]:
            raise CohortContractError(
                "CLI parameter drift for "
                f"{name}: {cli_value!r} != frozen {selection[name]!r}"
            )
    return contract


async def build_summary_from_db(
    db_url: str,
    *,
    scope: str,
    window_days: int,
    lead_minutes: float,
    as_of: datetime | None = None,
) -> ProspectiveCohortSummary:
    """Fetch trusted rows and summarize registry-bound prediction coverage."""

    outcome_types = _outcome_types_for_scope(scope)
    rows = await fetch_rows(
        db_url,
        window_days=window_days,
        lead_minutes=lead_minutes,
        outcome_types=outcome_types,
        as_of=as_of,
        exclude_controlled_fixtures=False,
        include_identity_metadata=False,
    )
    return build_cohort_summary(
        rows,
        scope=scope,
        window_days=window_days,
        lead_minutes=lead_minutes,
        as_of=as_of,
    )


def _parse_cli_as_of(value: str) -> datetime:
    """Parse a timezone-aware ISO-8601 CLI boundary."""

    try:
        return _datetime_from_contract(value)
    except CohortContractError as exc:
        raise argparse.ArgumentTypeError(str(exc)) from exc


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    """Parse CLI options."""

    parser = argparse.ArgumentParser(description=__doc__, allow_abbrev=False)
    parser.add_argument("--db-url", default=DEFAULT_DB_URL)
    parser.add_argument("--scope", choices=("strict", "task"), default="task")
    parser.add_argument("--window-days", type=int, default=90)
    parser.add_argument("--lead-minutes", type=float, default=30.0)
    default_thresholds = ReadinessThresholds()
    parser.add_argument("--min-prediction-bound", type=int, default=default_thresholds.min_prediction_bound)
    parser.add_argument("--min-prediction-bound-bad", type=int, default=default_thresholds.min_prediction_bound_bad)
    parser.add_argument("--min-prediction-coverage", type=float, default=default_thresholds.min_prediction_coverage)
    parser.add_argument(
        "--min-prediction-prior-state-coverage",
        type=float,
        default=default_thresholds.min_prediction_prior_state_coverage,
    )
    parser.add_argument("--output", help="Optional markdown output path")
    parser.add_argument(
        "--as-of",
        type=_parse_cli_as_of,
        help="Optional timezone-aware cohort boundary (ISO-8601)",
    )
    contract_group = parser.add_mutually_exclusive_group()
    contract_group.add_argument(
        "--write-cohort-contract",
        type=Path,
        help=(
            "Create a JSON cohort consistency contract with exclusive creation; "
            "refuses overwrite"
        ),
    )
    contract_group.add_argument(
        "--read-cohort-contract",
        type=Path,
        help="Validate and replay a JSON cohort consistency contract",
    )
    raw_argv = list(sys.argv[1:] if argv is None else argv)
    args = parser.parse_args(raw_argv)
    selection_options = {
        "--scope": "scope",
        "--window-days": "window_days",
        "--lead-minutes": "lead_minutes",
        "--as-of": "as_of",
    }
    args._explicit_selection_options = frozenset(
        destination
        for option, destination in selection_options.items()
        if any(token == option or token.startswith(option + "=") for token in raw_argv)
    )
    return args


def _explicit_contract_cli_values(args: argparse.Namespace) -> dict[str, Any]:
    """Return only selection values the caller explicitly supplied."""

    explicit = getattr(args, "_explicit_selection_options", frozenset())
    return {name: getattr(args, name) for name in explicit}


def _selection_from_contract(
    contract: Mapping[str, Any],
) -> tuple[str, int, float, datetime]:
    """Extract a typed selection tuple from a validated contract."""

    selection = contract["selection"]
    return (
        str(selection["scope"]),
        int(selection["window_days"]),
        float(selection["lead_minutes"]),
        _datetime_from_contract(selection["as_of"]),
    )


def _paths_alias(left: str | Path, right: str | Path) -> bool:
    """Return whether two paths resolve to the same filesystem object."""

    left_path = Path(left)
    right_path = Path(right)
    if left_path.resolve(strict=False) == right_path.resolve(strict=False):
        return True
    if not left_path.exists() or not right_path.exists():
        return False
    try:
        return os.path.samefile(left_path, right_path)
    except OSError:
        return False


async def main_async(args: argparse.Namespace) -> int:
    """Run the outcome-attested prediction-binding cohort report."""

    contract_path = args.read_cohort_contract or args.write_cohort_contract
    if contract_path and args.output and _paths_alias(contract_path, args.output):
        print(
            "error: cohort contract and report output paths must not alias",
            file=sys.stderr,
        )
        return 2

    scope = args.scope
    window_days = args.window_days
    lead_minutes = args.lead_minutes
    as_of = args.as_of
    contract_written: Path | None = None
    if args.read_cohort_contract:
        try:
            contract = read_cohort_contract(
                args.read_cohort_contract,
                **_explicit_contract_cli_values(args),
            )
        except (CohortContractError, OSError) as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 2
        scope, window_days, lead_minutes, as_of = _selection_from_contract(contract)
    elif args.write_cohort_contract:
        as_of = as_of or now_utc()
        try:
            contract = build_cohort_contract(
                scope=scope,
                window_days=window_days,
                lead_minutes=lead_minutes,
                as_of=as_of,
            )
            write_cohort_contract(args.write_cohort_contract, contract)
        except FileExistsError:
            print(
                "error: cohort contract already exists; refusing overwrite: "
                f"{args.write_cohort_contract}",
                file=sys.stderr,
            )
            return 2
        except (CohortContractError, OSError) as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 2
        contract_written = args.write_cohort_contract

    summary = await build_summary_from_db(
        args.db_url,
        scope=scope,
        window_days=window_days,
        lead_minutes=lead_minutes,
        as_of=as_of,
    )
    thresholds = ReadinessThresholds(
        min_prediction_bound=args.min_prediction_bound,
        min_prediction_bound_bad=args.min_prediction_bound_bad,
        min_prediction_coverage=args.min_prediction_coverage,
        min_prediction_prior_state_coverage=args.min_prediction_prior_state_coverage,
    )
    report = format_cohort_report(summary, thresholds=thresholds)
    if contract_written:
        print(f"Wrote {contract_written}")
    if args.output:
        path = Path(args.output)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(report + "\n", encoding="utf-8")
        print(f"Wrote {path}")
    else:
        print(report)
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    """CLI entrypoint."""

    return asyncio.run(main_async(parse_args(argv)))


if __name__ == "__main__":
    raise SystemExit(main())
