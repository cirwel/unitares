"""File-backed registry for agent-network research runs.

The registry is intentionally thin: it gives operators and agents one durable,
queryable place to describe a run without introducing a migration or coupling
research bookkeeping to KG storage. KG/outcome/finding ids stay as links.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


SCHEMA = "unitares.research_run.v1"
REGISTRY_ENV = "UNITARES_RESEARCH_REGISTRY_DIR"
DEFAULT_REGISTRY_DIR = Path("~/.local/state/unitares/research-runs")

VALID_STATUSES = frozenset({"planned", "running", "completed", "aborted", "archived"})
_RUN_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")

_LIST_FIELDS = (
    "population",
    "tools",
    "memory",
    "communication_channels",
    "interventions",
    "metrics",
    "observations",
    "outcomes",
    "artifacts",
    "linked_knowledge_ids",
    "linked_outcome_ids",
    "linked_finding_ids",
    "research_areas",
    "tags",
)

_DICT_FIELDS = ("scenario", "topology", "exogenous_anchor")


class ResearchRegistryError(ValueError):
    """Raised when a research-run record is invalid."""


class ResearchRunNotFound(FileNotFoundError):
    """Raised when a requested run id is absent from the registry."""


def registry_dir(root: str | Path | None = None) -> Path:
    """Resolve the registry directory, honoring the env override."""

    raw = root or os.getenv(REGISTRY_ENV) or DEFAULT_REGISTRY_DIR
    return Path(raw).expanduser()


def validate_run_id(run_id: str) -> str:
    if not isinstance(run_id, str) or not _RUN_ID_RE.match(run_id):
        raise ResearchRegistryError(
            "run_id must start with an alphanumeric character and contain only "
            "letters, numbers, '.', '_' or '-' (max 128 chars)"
        )
    return run_id


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _slug(text: str) -> str:
    slug = re.sub(r"[^a-zA-Z0-9]+", "-", text.strip().lower()).strip("-")
    return slug or "research-run"


def _generate_run_id(payload: dict[str, Any]) -> str:
    base = payload.get("title")
    if not isinstance(base, str) or not base.strip():
        scenario = payload.get("scenario")
        if isinstance(scenario, dict):
            base = str(scenario.get("id") or scenario.get("name") or "research-run")
        else:
            base = "research-run"
    digest = hashlib.sha256(
        json.dumps(payload, sort_keys=True, default=str).encode("utf-8")
    ).hexdigest()[:8]
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    return validate_run_id(f"{stamp}-{_slug(base)[:48]}-{digest}")


def _record_path(run_id: str, root: str | Path | None = None) -> Path:
    run_id = validate_run_id(run_id)
    return registry_dir(root) / f"{run_id}.json"


def _coerce_dict(value: Any, field: str) -> dict[str, Any]:
    if value is None:
        return {}
    if isinstance(value, dict):
        return dict(value)
    raise ResearchRegistryError(f"{field} must be an object")


def _coerce_list(value: Any, field: str) -> list[Any]:
    if value is None:
        return []
    if isinstance(value, list):
        return list(value)
    if isinstance(value, tuple):
        return list(value)
    raise ResearchRegistryError(f"{field} must be an array")


def _coerce_str_list(value: Any, field: str) -> list[str]:
    return [str(v) for v in _coerce_list(value, field) if v is not None and str(v)]


def _normalize_status(value: Any) -> str:
    status = str(value or "planned").strip().lower()
    if status not in VALID_STATUSES:
        raise ResearchRegistryError(
            f"status must be one of {', '.join(sorted(VALID_STATUSES))}"
        )
    return status


def _require_shape(record: dict[str, Any]) -> None:
    scenario = record["scenario"]
    topology = record["topology"]
    population = record["population"]
    if not (scenario.get("id") or scenario.get("name")):
        raise ResearchRegistryError("scenario.id or scenario.name is required")
    if not topology:
        raise ResearchRegistryError("topology is required")
    if not population:
        raise ResearchRegistryError("population must contain at least one agent or class")


def normalize_research_run(
    payload: dict[str, Any], *, existing: dict[str, Any] | None = None
) -> dict[str, Any]:
    """Validate and normalize a run payload into the on-disk schema."""

    if not isinstance(payload, dict):
        raise ResearchRegistryError("research run payload must be an object")

    now = _now_iso()
    base = dict(existing or {})
    base.update(payload)

    run_id = validate_run_id(str(base.get("run_id") or _generate_run_id(base)))
    status = _normalize_status(base.get("status"))
    title = str(base.get("title") or "").strip()
    if not title:
        scenario = base.get("scenario") if isinstance(base.get("scenario"), dict) else {}
        title = str(scenario.get("name") or scenario.get("id") or run_id)

    record: dict[str, Any] = {
        "schema": SCHEMA,
        "run_id": run_id,
        "title": title,
        "status": status,
        "created_at": existing.get("created_at") if existing else base.get("created_at"),
        "updated_at": now,
    }
    if not record["created_at"]:
        record["created_at"] = now

    for field in _DICT_FIELDS:
        record[field] = _coerce_dict(base.get(field), field)
    for field in _LIST_FIELDS:
        if field in {"tags", "research_areas", "linked_knowledge_ids", "linked_outcome_ids", "linked_finding_ids"}:
            record[field] = _coerce_str_list(base.get(field), field)
        else:
            record[field] = _coerce_list(base.get(field), field)

    for field in ("hypothesis", "operator_question", "notes"):
        value = base.get(field)
        if value is not None:
            record[field] = str(value)

    _require_shape(record)
    return record


def load_research_run(run_id: str, *, root: str | Path | None = None) -> dict[str, Any]:
    path = _record_path(run_id, root)
    if not path.exists():
        raise ResearchRunNotFound(f"research run not found: {run_id}")
    with path.open("r", encoding="utf-8") as fh:
        data = json.load(fh)
    if not isinstance(data, dict):
        raise ResearchRegistryError(f"research run file is not an object: {run_id}")
    return data


def record_research_run(payload: dict[str, Any], *, root: str | Path | None = None) -> dict[str, Any]:
    """Create or replace a run record, preserving created_at on updates."""

    run_id = payload.get("run_id") if isinstance(payload, dict) else None
    existing = None
    if run_id:
        try:
            existing = load_research_run(str(run_id), root=root)
        except ResearchRunNotFound:
            existing = None

    record = normalize_research_run(payload, existing=existing)
    root_path = registry_dir(root)
    root_path.mkdir(parents=True, exist_ok=True)
    path = _record_path(record["run_id"], root_path)
    tmp = path.with_suffix(".json.tmp")
    with tmp.open("w", encoding="utf-8") as fh:
        json.dump(record, fh, ensure_ascii=False, indent=2, sort_keys=True)
        fh.write("\n")
    tmp.replace(path)
    return record


def _load_all(root: str | Path | None = None) -> tuple[list[dict[str, Any]], list[str]]:
    root_path = registry_dir(root)
    if not root_path.exists():
        return [], []

    runs: list[dict[str, Any]] = []
    warnings: list[str] = []
    for path in sorted(root_path.glob("*.json")):
        try:
            with path.open("r", encoding="utf-8") as fh:
                data = json.load(fh)
            if isinstance(data, dict):
                runs.append(data)
            else:
                warnings.append(f"{path.name}: expected object")
        except Exception as exc:  # noqa: BLE001 - registry should keep serving valid rows
            warnings.append(f"{path.name}: {exc}")
    return runs, warnings


def grounding_status(record: dict[str, Any]) -> str:
    anchor = record.get("exogenous_anchor") or {}
    if isinstance(anchor, dict) and (
        anchor.get("source") or anchor.get("dataset") or anchor.get("outcome")
    ):
        return "anchored"
    if record.get("linked_outcome_ids") or record.get("linked_knowledge_ids"):
        return "linked"
    return "missing"


def rigor_checklist(record: dict[str, Any]) -> dict[str, bool]:
    return {
        "scenario": bool((record.get("scenario") or {}).get("id") or (record.get("scenario") or {}).get("name")),
        "topology": bool(record.get("topology")),
        "population": bool(record.get("population")),
        "metrics": bool(record.get("metrics")),
        "exogenous_grounding": grounding_status(record) == "anchored",
        "artifacts": bool(record.get("artifacts")),
    }


def summarize_research_run(record: dict[str, Any]) -> dict[str, Any]:
    scenario = record.get("scenario") if isinstance(record.get("scenario"), dict) else {}
    topology = record.get("topology") if isinstance(record.get("topology"), dict) else {}
    checklist = rigor_checklist(record)
    return {
        "run_id": record.get("run_id"),
        "title": record.get("title"),
        "status": record.get("status"),
        "scenario_id": scenario.get("id"),
        "scenario_name": scenario.get("name"),
        "topology_kind": topology.get("kind"),
        "population_count": len(record.get("population") or []),
        "research_areas": record.get("research_areas") or [],
        "tags": record.get("tags") or [],
        "grounding_status": grounding_status(record),
        "rigor_checklist": checklist,
        "rigor_complete": all(checklist.values()),
        "created_at": record.get("created_at"),
        "updated_at": record.get("updated_at"),
    }


def _matches_text(record: dict[str, Any], query: str) -> bool:
    needle = query.strip().lower()
    if not needle:
        return True
    hay = json.dumps(record, sort_keys=True, default=str).lower()
    return needle in hay


def query_research_runs(
    *,
    status: str | None = None,
    tag: str | None = None,
    scenario_id: str | None = None,
    research_area: str | None = None,
    grounding: str | None = None,
    query: str | None = None,
    limit: int = 50,
    include_details: bool = False,
    root: str | Path | None = None,
) -> dict[str, Any]:
    runs, warnings = _load_all(root)

    if status:
        status = status.strip().lower()
        runs = [r for r in runs if str(r.get("status", "")).lower() == status]
    if tag:
        runs = [r for r in runs if tag in (r.get("tags") or [])]
    if scenario_id:
        runs = [
            r for r in runs
            if isinstance(r.get("scenario"), dict) and r["scenario"].get("id") == scenario_id
        ]
    if research_area:
        runs = [r for r in runs if research_area in (r.get("research_areas") or [])]
    if grounding:
        grounding = grounding.strip().lower()
        runs = [r for r in runs if grounding_status(r) == grounding]
    if query:
        runs = [r for r in runs if _matches_text(r, query)]

    runs.sort(key=lambda r: str(r.get("updated_at") or r.get("created_at") or ""), reverse=True)
    try:
        limit = max(1, min(int(limit), 500))
    except (TypeError, ValueError):
        limit = 50
    selected = runs[:limit]
    return {
        "schema": SCHEMA,
        "runs": selected if include_details else [summarize_research_run(r) for r in selected],
        "count": len(selected),
        "total_matched": len(runs),
        "warnings": warnings,
    }


def research_registry_stats(*, root: str | Path | None = None) -> dict[str, Any]:
    runs, warnings = _load_all(root)
    by_status = Counter(str(r.get("status") or "unknown") for r in runs)
    by_grounding = Counter(grounding_status(r) for r in runs)
    by_scenario = Counter()
    by_research_area = Counter()
    for record in runs:
        scenario = record.get("scenario") if isinstance(record.get("scenario"), dict) else {}
        key = scenario.get("id") or scenario.get("name") or "unknown"
        by_scenario[str(key)] += 1
        for area in record.get("research_areas") or []:
            by_research_area[str(area)] += 1
    rigor_complete = sum(1 for record in runs if all(rigor_checklist(record).values()))
    return {
        "schema": SCHEMA,
        "total": len(runs),
        "by_status": dict(by_status),
        "by_grounding": dict(by_grounding),
        "by_scenario": dict(by_scenario),
        "by_research_area": dict(by_research_area),
        "rigor_complete": rigor_complete,
        "rigor_incomplete": len(runs) - rigor_complete,
        "registry_dir": str(registry_dir(root)),
        "warnings": warnings,
    }
