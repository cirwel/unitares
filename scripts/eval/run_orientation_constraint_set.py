#!/usr/bin/env python3
"""Enroll and run the preregistered orientation constraint-set experiment.

The runner speaks only to a local Ollama native chat endpoint.  It refuses to
start scored calls until the implementation and enrollment are committed,
pushed, clean, information-equivalent, and still byte-identical to the frozen
digests.  Raw outputs live outside the repository with mode 0700.

Typical sequence::

    python scripts/eval/run_orientation_constraint_set.py validate
    python scripts/eval/run_orientation_constraint_set.py enroll \
      --output docs/evaluations/orientation-constraint-set/enrollment-v0.json \
      --output-dir /absolute/private/output/path \
      --analyst-id <governance-uuid>
    # commit and push the enrollment artifact, then:
    python scripts/eval/run_orientation_constraint_set.py run \
      --enrollment docs/evaluations/orientation-constraint-set/enrollment-v0.json
"""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import inspect
import json
import os
from pathlib import Path
import subprocess
import sys
import time
from typing import Any, Mapping, Sequence
import urllib.error
import urllib.request

if __package__ is None or __package__ == "":
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from scripts.eval.orientation_constraint_set import (  # noqa: E402
    ANALYSIS_SEED,
    ARMS,
    BOOTSTRAP_DRAWS,
    CONDITION_ORDER_SEED,
    ENROLLMENT_SCHEMA,
    FACT_ORDER_SEED,
    FAMILIES,
    REPETITIONS,
    RESPONSE_SCHEMA,
    SIGN_FLIP_DRAWS,
    SYSTEM_PROMPT,
    analyze_results,
    build_canary_schedule,
    build_scored_schedule,
    build_user_prompt,
    canonical_json,
    load_scenarios,
    parse_response_text,
    render_constraint_set,
    render_provider_envelopes,
    representation_equality,
    scenario_manifest,
    schedule_token,
    score_response,
    sha256_file,
    sha256_json,
    sha256_text,
)


REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_SCENARIOS = REPO_ROOT / "tests/orientation_constraint_set/scenarios-v0.json"
DEFAULT_PROTOCOL = (
    REPO_ROOT
    / "docs/proposals/orientation-constraint-set-preregistration-v0.md"
)
CORE_PATH = REPO_ROOT / "scripts/eval/orientation_constraint_set.py"
RUNNER_PATH = Path(__file__).resolve()
DEFAULT_OLLAMA_BASE = "http://127.0.0.1:11434"
DEFAULT_MODEL = "gemma4:latest"


class EnrollmentError(RuntimeError):
    """Raised when enrollment or pre-run validation fails closed."""


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _git(*args: str, check: bool = True) -> str:
    completed = subprocess.run(
        ["git", *args],
        cwd=REPO_ROOT,
        check=False,
        capture_output=True,
        text=True,
    )
    if check and completed.returncode != 0:
        detail = completed.stderr.strip() or completed.stdout.strip()
        raise EnrollmentError(f"git {' '.join(args)} failed: {detail}")
    return completed.stdout.strip()


def _assert_clean_named_pushed_head() -> dict[str, str]:
    dirty = _git("status", "--porcelain=v1", "--untracked-files=all")
    if dirty:
        raise EnrollmentError("worktree must be clean before enrollment or run")
    branch = _git("symbolic-ref", "--quiet", "--short", "HEAD")
    if not branch:
        raise EnrollmentError("a named branch is required")
    upstream = _git("rev-parse", "--abbrev-ref", "@{upstream}")
    head = _git("rev-parse", "HEAD")
    upstream_head = _git("rev-parse", "@{upstream}")
    if head != upstream_head:
        raise EnrollmentError("HEAD must equal its pushed upstream before enrollment or run")
    return {
        "branch": branch,
        "upstream": upstream,
        "head": head,
        "upstream_head": upstream_head,
    }


def _is_ancestor(older: str, newer: str) -> bool:
    completed = subprocess.run(
        ["git", "merge-base", "--is-ancestor", older, newer],
        cwd=REPO_ROOT,
        check=False,
        capture_output=True,
        text=True,
    )
    return completed.returncode == 0


def _repo_relative(path: Path) -> str:
    try:
        return str(path.resolve().relative_to(REPO_ROOT))
    except ValueError as exc:
        raise EnrollmentError(f"tracked artifact is outside repository: {path}") from exc


def _require_external_private_output(path: Path, *, create: bool) -> dict[str, Any]:
    resolved = path.expanduser().resolve()
    try:
        resolved.relative_to(REPO_ROOT)
    except ValueError:
        pass
    else:
        raise EnrollmentError("raw output directory must be outside the repository")
    if create:
        resolved.mkdir(parents=True, exist_ok=True, mode=0o700)
        resolved.chmod(0o700)
    if not resolved.is_dir():
        raise EnrollmentError(f"raw output directory does not exist: {resolved}")
    mode = resolved.stat().st_mode & 0o777
    if mode != 0o700:
        raise EnrollmentError(
            f"raw output directory must have mode 0700, found {mode:04o}"
        )
    user_home = Path.home().resolve()
    try:
        home_relative = resolved.relative_to(user_home)
    except ValueError as exc:
        raise EnrollmentError(
            "raw output directory must be under the operator home so tracked "
            "enrollment can use a portable ~/ path"
        ) from exc
    return {"path": f"~/{home_relative}", "permissions": "0700"}


def resolve_recorded_output_path(output_record: Mapping[str, Any]) -> Path:
    """Resolve the portable path stored in a tracked enrollment record."""
    return Path(str(output_record["path"])).expanduser().resolve()


def _http_json(
    url: str,
    *,
    payload: Mapping[str, Any] | None = None,
    timeout: float,
) -> dict[str, Any]:
    data = None if payload is None else canonical_json(payload).encode("utf-8")
    request = urllib.request.Request(
        url,
        data=data,
        headers={"Content-Type": "application/json"},
        method="GET" if data is None else "POST",
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        parsed = json.load(response)
    if not isinstance(parsed, dict):
        raise EnrollmentError(f"endpoint returned non-object JSON: {url}")
    return parsed


def inspect_local_model(base_url: str, model_name: str) -> dict[str, Any]:
    """Read Ollama's local tag registry without making a generation call."""
    normalized = base_url.rstrip("/")
    try:
        payload = _http_json(f"{normalized}/api/tags", timeout=10.0)
    except (OSError, urllib.error.URLError, json.JSONDecodeError) as exc:
        raise EnrollmentError(f"local Ollama registry unavailable: {exc}") from exc
    matches = [
        model
        for model in payload.get("models", [])
        if model.get("name") == model_name or model.get("model") == model_name
    ]
    if len(matches) != 1:
        available = sorted(
            str(model.get("name") or model.get("model"))
            for model in payload.get("models", [])
        )
        raise EnrollmentError(
            f"model {model_name!r} not uniquely available; available={available}"
        )
    model = matches[0]
    digest = model.get("digest")
    if not isinstance(digest, str) or not digest:
        raise EnrollmentError("Ollama model record has no digest")
    return {
        "requested_model": model_name,
        "reported_model": str(model.get("name") or model.get("model")),
        "digest": digest,
        "modified_at": model.get("modified_at"),
        "size": model.get("size"),
        "details": model.get("details"),
        "registry_record_digest": sha256_json(model),
    }


def _function_digest(function: Any) -> str:
    return sha256_text(inspect.getsource(function))


def build_model_request(
    enrollment: Mapping[str, Any],
    scenario: Mapping[str, Any],
    entry: Mapping[str, Any],
) -> dict[str, Any]:
    """Build the exact frozen Ollama request for one scheduled call."""
    model = enrollment["model"]
    decoding = model["decoding"]
    return {
        "model": model["record"]["requested_model"],
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {
                "role": "user",
                "content": build_user_prompt(scenario, entry["arm"]),
            },
        ],
        "format": RESPONSE_SCHEMA,
        "stream": False,
        "think": decoding["think"],
        "options": {
            "temperature": decoding["temperature"],
            "num_predict": decoding["num_predict"],
            "num_ctx": decoding["num_ctx"],
            "seed": entry["sample_seed"],
        },
        "keep_alive": "30m",
    }


def parse_provider_answer(
    response: Mapping[str, Any],
) -> tuple[str | None, dict[str, Any] | None, str | None]:
    """Parse only a normally terminated answer-channel response."""
    raw_text = response.get("message", {}).get("content")
    done_reason = response.get("done_reason")
    if done_reason != "stop":
        return raw_text, None, f"provider_termination:{done_reason or 'missing'}"
    parsed, parse_error = parse_response_text(raw_text)
    return raw_text, parsed, parse_error


def _prompt_set_digest(
    scenarios: Sequence[Mapping[str, Any]], arm: str
) -> str:
    return sha256_json(
        [
            {
                "scenario_id": scenario["scenario_id"],
                "prompt": build_user_prompt(scenario, arm),
            }
            for scenario in sorted(scenarios, key=lambda row: row["scenario_id"])
        ]
    )


def _digest_without_self(enrollment: Mapping[str, Any]) -> str:
    payload = dict(enrollment)
    payload.pop("enrollment_digest", None)
    return sha256_json(payload)


def build_enrollment(
    *,
    scenarios_path: Path,
    protocol_path: Path,
    output_dir: Path,
    analyst_id: str,
    operator_id: str,
    base_url: str,
    model: str,
    temperature: float,
    num_predict: int,
    num_ctx: int,
    timeout_s: float,
) -> dict[str, Any]:
    git_state = _assert_clean_named_pushed_head()
    scenarios = load_scenarios(scenarios_path)
    manifest = scenario_manifest(scenarios)
    equality = [representation_equality(scenario) for scenario in scenarios]
    if not all(item["equal"] for item in equality):
        raise EnrollmentError("representation fact manifests differ")
    model_record = inspect_local_model(base_url, model)
    scored_schedule = build_scored_schedule(scenarios)
    canary_schedule = build_canary_schedule(scenarios)
    output_record = _require_external_private_output(output_dir, create=True)
    if any(resolve_recorded_output_path(output_record).iterdir()):
        raise EnrollmentError("raw output directory must be empty at enrollment")
    protocol_commit = _git("log", "-n", "1", "--format=%H", "--", _repo_relative(protocol_path))
    if not protocol_commit:
        raise EnrollmentError("protocol file is not committed")
    implementation_commit = git_state["head"]
    if not _is_ancestor(protocol_commit, implementation_commit):
        raise EnrollmentError("protocol commit must precede the implementation commit")

    enrollment: dict[str, Any] = {
        "schema": ENROLLMENT_SCHEMA,
        "enrolled_at": _utc_now(),
        "protocol": {
            "path": _repo_relative(protocol_path),
            "commit": protocol_commit,
            "sha256": sha256_file(protocol_path),
            "governed_review_session": "20ae6cbd2cf02a5c",
        },
        "implementation": {
            "commit": implementation_commit,
            "branch": git_state["branch"],
            "upstream": git_state["upstream"],
            "upstream_commit": git_state["upstream_head"],
            "clean_worktree_assertion": True,
            "files": {
                _repo_relative(CORE_PATH): sha256_file(CORE_PATH),
                _repo_relative(RUNNER_PATH): sha256_file(RUNNER_PATH),
                _repo_relative(scenarios_path): sha256_file(scenarios_path),
            },
        },
        "scenario_cohort": {
            "path": _repo_relative(scenarios_path),
            "schema": "unitares.orientation-constraint-set.scenarios.v0",
            "file_sha256": sha256_file(scenarios_path),
            "total": len(scenarios),
            "scored": sum(row["split"] == "scored" for row in scenarios),
            "canary": sum(row["split"] == "canary" for row in scenarios),
            "families": list(FAMILIES),
            "manifest": manifest,
            "manifest_digest": sha256_json(manifest),
            "answer_key_digest": sha256_json(
                [
                    {
                        "scenario_id": row["scenario_id"],
                        "answer_key": row["answer_key"],
                    }
                    for row in sorted(scenarios, key=lambda item: item["scenario_id"])
                ]
            ),
        },
        "representations": {
            "provider_envelopes_renderer_digest": _function_digest(
                render_provider_envelopes
            ),
            "constraint_set_renderer_digest": _function_digest(render_constraint_set),
            "canonical_fact_equality": True,
            "equality_manifest_digest": sha256_json(equality),
            "equality_manifest": equality,
        },
        "prompt_and_scorer": {
            "system_prompt_sha256": sha256_text(SYSTEM_PROMPT),
            "provider_envelopes_prompt_set_digest": _prompt_set_digest(
                scenarios, "provider_envelopes"
            ),
            "constraint_set_prompt_set_digest": _prompt_set_digest(
                scenarios, "constraint_set"
            ),
            "response_schema": RESPONSE_SCHEMA,
            "response_schema_digest": sha256_json(RESPONSE_SCHEMA),
            "request_builder_digest": _function_digest(build_model_request),
            "scorer_digest": _function_digest(score_response),
            "analyzer_digest": _function_digest(analyze_results),
        },
        "model": {
            "provider": "ollama",
            "endpoint_class": "ollama_native_chat",
            "base_url": base_url.rstrip("/"),
            "privacy": "local",
            "record": model_record,
            "decoding": {
                "temperature": temperature,
                "num_predict": num_predict,
                "num_ctx": num_ctx,
                "think": False,
                "stream": False,
                "structured_format": "json_schema",
                "sample_seed_source": "sha256(condition_order_seed,scenario_id,repetition), shared by paired arms",
            },
            "timeout_s": timeout_s,
            "concurrency": 1,
            "retry_policy": "none",
        },
        "schedule": {
            "repetitions": REPETITIONS,
            "scored_call_count": len(scored_schedule),
            "canary_call_count": len(canary_schedule),
            "condition_order_seed": CONDITION_ORDER_SEED,
            "within_envelope_fact_order_seed": FACT_ORDER_SEED,
            "analysis_seed": ANALYSIS_SEED,
            "scored_schedule_digest": sha256_json(scored_schedule),
            "canary_schedule_digest": sha256_json(canary_schedule),
            "scored_order": [schedule_token(entry) for entry in scored_schedule],
            "canary_order": [schedule_token(entry) for entry in canary_schedule],
        },
        "analysis": {
            "primary_estimand": "equal-family mean paired primary-success difference",
            "family_cluster_bootstrap": {
                "draws": BOOTSTRAP_DRAWS,
                "seed": ANALYSIS_SEED,
                "interval": 0.95,
            },
            "paired_family_sign_flip": {
                "draws": SIGN_FLIP_DRAWS,
                "seed": ANALYSIS_SEED,
                "two_sided": True,
                "plus_one_correction": True,
            },
            "clean_flow_noninferiority": {
                "margin": -0.05,
                "interval": 0.90,
                "cluster_unit": "scored_variant",
                "rationale": "A single family cannot itself be resampled; its three preregistered variants are the frozen within-family clusters.",
            },
            "proceed_effect_floor": 0.25,
            "efficiency_reduction_floor": 0.30,
            "infrastructure_invalid_above": 0.10,
        },
        "storage": {
            "raw_output": output_record,
            "tracked_source_output": False,
            "raw_responses_retained": True,
        },
        "analyst_identity": {
            "operator_id": operator_id,
            "analyst_id": analyst_id,
        },
        "assertions": {
            "no_scored_call_executed": True,
            "no_scored_treatment_output_read": True,
            "all_240_scored_calls_scheduled_before_execution": True,
            "canaries_are_transport_and_parsing_only": True,
            "outcome_aware_retry_forbidden": True,
            "artifact_is_read_only_and_non_authoritative": True,
        },
    }
    enrollment["enrollment_digest"] = _digest_without_self(enrollment)
    return enrollment


def _atomic_write_json(path: Path, payload: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp-{os.getpid()}")
    with temporary.open("w", encoding="utf-8") as handle:
        json.dump(payload, handle, indent=2, ensure_ascii=False)
        handle.write("\n")
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, path)


def write_enrollment(path: Path, enrollment: Mapping[str, Any]) -> None:
    _atomic_write_json(path, enrollment)


def _load_json(path: Path) -> dict[str, Any]:
    with path.open(encoding="utf-8") as handle:
        payload = json.load(handle)
    if not isinstance(payload, dict):
        raise EnrollmentError(f"expected JSON object: {path}")
    return payload


def validate_enrollment(
    enrollment_path: Path,
    *,
    require_pushed_enrollment: bool,
    require_empty_output: bool,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    enrollment = _load_json(enrollment_path)
    if enrollment.get("schema") != ENROLLMENT_SCHEMA:
        raise EnrollmentError("unknown enrollment schema")
    if enrollment.get("enrollment_digest") != _digest_without_self(enrollment):
        raise EnrollmentError("enrollment self-digest mismatch")
    git_state = _assert_clean_named_pushed_head()
    implementation = enrollment["implementation"]
    implementation_commit = implementation["commit"]
    if not _is_ancestor(implementation_commit, git_state["head"]):
        raise EnrollmentError("enrolled implementation is not an ancestor of HEAD")
    if require_pushed_enrollment:
        enrollment_commit = _git(
            "log", "-n", "1", "--format=%H", "--", _repo_relative(enrollment_path)
        )
        if not enrollment_commit:
            raise EnrollmentError("enrollment artifact is not committed")
        if not _is_ancestor(enrollment_commit, git_state["upstream_head"]):
            raise EnrollmentError("enrollment artifact is not pushed upstream")

    for relative, digest in implementation["files"].items():
        current_path = REPO_ROOT / relative
        if sha256_file(current_path) != digest:
            raise EnrollmentError(f"enrolled implementation file changed: {relative}")
    scenario_path = REPO_ROOT / enrollment["scenario_cohort"]["path"]
    scenarios = load_scenarios(scenario_path)
    manifest = scenario_manifest(scenarios)
    if sha256_json(manifest) != enrollment["scenario_cohort"]["manifest_digest"]:
        raise EnrollmentError("scenario manifest digest mismatch")
    if manifest != enrollment["scenario_cohort"]["manifest"]:
        raise EnrollmentError("scenario manifest content mismatch")
    equality = [representation_equality(scenario) for scenario in scenarios]
    if not all(item["equal"] for item in equality):
        raise EnrollmentError("representation facts are no longer equal")
    if sha256_json(equality) != enrollment["representations"]["equality_manifest_digest"]:
        raise EnrollmentError("representation equality manifest changed")
    expected_function_digests = {
        "provider_envelopes_renderer_digest": _function_digest(
            render_provider_envelopes
        ),
        "constraint_set_renderer_digest": _function_digest(render_constraint_set),
    }
    for key, digest in expected_function_digests.items():
        if enrollment["representations"][key] != digest:
            raise EnrollmentError(f"renderer digest mismatch: {key}")
    if enrollment["prompt_and_scorer"]["scorer_digest"] != _function_digest(
        score_response
    ):
        raise EnrollmentError("scorer digest mismatch")
    if enrollment["prompt_and_scorer"]["analyzer_digest"] != _function_digest(
        analyze_results
    ):
        raise EnrollmentError("analyzer digest mismatch")
    if enrollment["prompt_and_scorer"].get(
        "request_builder_digest"
    ) != _function_digest(build_model_request):
        raise EnrollmentError("model request builder digest mismatch")
    if enrollment["prompt_and_scorer"]["system_prompt_sha256"] != sha256_text(
        SYSTEM_PROMPT
    ):
        raise EnrollmentError("system prompt digest mismatch")
    for arm in ARMS:
        key = f"{arm}_prompt_set_digest"
        if enrollment["prompt_and_scorer"][key] != _prompt_set_digest(scenarios, arm):
            raise EnrollmentError(f"prompt-set digest mismatch: {arm}")

    scored_schedule = build_scored_schedule(scenarios)
    canary_schedule = build_canary_schedule(scenarios)
    if enrollment["schedule"]["scored_schedule_digest"] != sha256_json(
        scored_schedule
    ):
        raise EnrollmentError("scored schedule digest mismatch")
    if enrollment["schedule"]["canary_schedule_digest"] != sha256_json(
        canary_schedule
    ):
        raise EnrollmentError("canary schedule digest mismatch")
    if enrollment["schedule"]["scored_order"] != [
        schedule_token(entry) for entry in scored_schedule
    ]:
        raise EnrollmentError("scored schedule order mismatch")
    if enrollment["schedule"]["canary_order"] != [
        schedule_token(entry) for entry in canary_schedule
    ]:
        raise EnrollmentError("canary schedule order mismatch")

    model = enrollment["model"]
    if model["decoding"].get("think") is not False:
        raise EnrollmentError("enrolled model request must explicitly set think=false")
    current_model = inspect_local_model(
        model["base_url"], model["record"]["requested_model"]
    )
    if current_model["digest"] != model["record"]["digest"]:
        raise EnrollmentError("local model digest changed after enrollment")
    output = _require_external_private_output(
        Path(enrollment["storage"]["raw_output"]["path"]), create=False
    )
    if output != enrollment["storage"]["raw_output"]:
        raise EnrollmentError("output directory metadata changed")
    if require_empty_output and any(resolve_recorded_output_path(output).iterdir()):
        raise EnrollmentError("output directory is not empty; cohort cannot be rerun")
    return enrollment, scenarios


def _append_jsonl(path: Path, payload: Mapping[str, Any]) -> None:
    with path.open("a", encoding="utf-8") as handle:
        handle.write(canonical_json(payload) + "\n")
        handle.flush()
        os.fsync(handle.fileno())


def _infrastructure_failure_signature(exc: Exception) -> str:
    """Collapse transport errors to stable common-mode classes."""
    if isinstance(exc, urllib.error.HTTPError):
        return f"HTTPError:{exc.code}"
    if isinstance(exc, urllib.error.URLError):
        return f"URLError:{type(exc.reason).__name__}"
    if isinstance(exc, TimeoutError):
        return "TimeoutError"
    return type(exc).__name__


def _invoke_model(
    *,
    enrollment: Mapping[str, Any],
    scenario: Mapping[str, Any],
    entry: Mapping[str, Any],
) -> dict[str, Any]:
    model = enrollment["model"]
    request_payload = build_model_request(enrollment, scenario, entry)
    started = time.monotonic()
    base_record = {
        **dict(entry),
        "split": scenario["split"],
        "request_digest": sha256_json(request_payload),
        "started_at": _utc_now(),
    }
    try:
        response = _http_json(
            f"{model['base_url'].rstrip('/')}/api/chat",
            payload=request_payload,
            timeout=float(model["timeout_s"]),
        )
    except Exception as exc:  # transport errors are ITT failures; never retry
        score = score_response(
            scenario,
            None,
            parse_error="infrastructure_failure",
        )
        return {
            **base_record,
            "status": "infrastructure_failure",
            "failure_signature": _infrastructure_failure_signature(exc),
            "failure_detail": f"{type(exc).__name__}:{str(exc)[:240]}",
            "latency_ms": round((time.monotonic() - started) * 1000, 3),
            "score": score,
        }

    reported_model = response.get("model")
    if reported_model != model["record"]["reported_model"]:
        score = score_response(
            scenario,
            None,
            parse_error="reported_model_mismatch",
        )
        return {
            **base_record,
            "status": "infrastructure_failure",
            "failure_signature": (
                f"reported_model_mismatch:{reported_model!r}!="
                f"{model['record']['reported_model']!r}"
            ),
            "latency_ms": round((time.monotonic() - started) * 1000, 3),
            "score": score,
        }
    raw_text, parsed, parse_error = parse_provider_answer(response)
    score = score_response(scenario, parsed, parse_error=parse_error)
    return {
        **base_record,
        "status": "ok" if parse_error is None else "parse_failure",
        "reported_model": reported_model,
        "response_digest": sha256_text(raw_text or ""),
        "raw_response": raw_text,
        "parsed_response": parsed,
        "parse_error": parse_error,
        "latency_ms": round((time.monotonic() - started) * 1000, 3),
        "provider_metrics": {
            key: response.get(key)
            for key in (
                "done_reason",
                "total_duration",
                "load_duration",
                "prompt_eval_count",
                "prompt_eval_duration",
                "eval_count",
                "eval_duration",
            )
        },
        "score": score,
    }


def run_cohort(enrollment_path: Path) -> dict[str, Any]:
    enrollment, scenarios = validate_enrollment(
        enrollment_path,
        require_pushed_enrollment=True,
        require_empty_output=True,
    )
    scenario_by_id = {scenario["scenario_id"]: scenario for scenario in scenarios}
    output_dir = resolve_recorded_output_path(enrollment["storage"]["raw_output"])
    lock_path = output_dir / "run.lock.json"
    lock_payload = {
        "enrollment_digest": enrollment["enrollment_digest"],
        "started_at": _utc_now(),
        "pid": os.getpid(),
        "status": "running",
    }
    try:
        with lock_path.open("x", encoding="utf-8") as handle:
            json.dump(lock_payload, handle, indent=2)
            handle.write("\n")
    except FileExistsError as exc:
        raise EnrollmentError("cohort run lock exists; rerun is forbidden") from exc

    canary_path = output_dir / "canaries.jsonl"
    canary_schedule = build_canary_schedule(scenarios)
    canary_results: list[dict[str, Any]] = []
    for index, entry in enumerate(canary_schedule, start=1):
        result = _invoke_model(
            enrollment=enrollment,
            scenario=scenario_by_id[entry["scenario_id"]],
            entry=entry,
        )
        canary_results.append(result)
        _append_jsonl(canary_path, result)
        print(
            f"canary {index:02d}/{len(canary_schedule)} status={result['status']}",
            flush=True,
        )
    canary_transport_valid = all(result["status"] == "ok" for result in canary_results)
    if not canary_transport_valid:
        status_payload = {
            **lock_payload,
            "status": "aborted_before_scored_calls",
            "completed_at": _utc_now(),
            "reason": "canary_transport_or_parse_failure",
            "canary_status_counts": dict(
                sorted(
                    {
                        status: sum(result["status"] == status for result in canary_results)
                        for status in {result["status"] for result in canary_results}
                    }.items()
                )
            ),
        }
        _atomic_write_json(lock_path, status_payload)
        raise EnrollmentError("canary transport/parsing failed; scored run was not started")

    scored_path = output_dir / "scored.jsonl"
    schedule = build_scored_schedule(scenarios)
    results: list[dict[str, Any]] = []
    for index, entry in enumerate(schedule, start=1):
        result = _invoke_model(
            enrollment=enrollment,
            scenario=scenario_by_id[entry["scenario_id"]],
            entry=entry,
        )
        results.append(result)
        _append_jsonl(scored_path, result)
        if index == 1 or index % 10 == 0 or index == len(schedule):
            print(
                f"scored {index:03d}/{len(schedule)} status={result['status']}",
                flush=True,
            )

    aggregate = analyze_results(
        scenarios,
        schedule,
        results,
        manifest_valid=True,
    )
    result_bundle = {
        "schema": "unitares.orientation-constraint-set.result-bundle.v0",
        "completed_at": _utc_now(),
        "enrollment_path": _repo_relative(enrollment_path),
        "enrollment_digest": enrollment["enrollment_digest"],
        "model": enrollment["model"],
        "raw_outputs": {
            "directory": str(output_dir),
            "canaries": str(canary_path),
            "scored": str(scored_path),
            "canaries_sha256": sha256_file(canary_path),
            "scored_sha256": sha256_file(scored_path),
        },
        "canary": {
            "call_count": len(canary_results),
            "transport_and_parsing_valid": canary_transport_valid,
            "excluded_from_estimates": True,
        },
        "aggregate": aggregate,
    }
    result_path = output_dir / "result.json"
    _atomic_write_json(result_path, result_bundle)
    completed_lock = {
        **lock_payload,
        "status": "completed",
        "completed_at": result_bundle["completed_at"],
        "classification": aggregate["classification"],
        "result_sha256": sha256_file(result_path),
    }
    _atomic_write_json(lock_path, completed_lock)
    print(
        json.dumps(
            {
                "classification": aggregate["classification"],
                "theta": aggregate["primary"]["theta"],
                "result_path": str(result_path),
            },
            sort_keys=True,
        ),
        flush=True,
    )
    return result_bundle


def render_result_markdown(bundle: Mapping[str, Any]) -> str:
    aggregate = bundle["aggregate"]
    primary = aggregate["primary"]
    interval = primary["family_cluster_bootstrap_95"]
    efficiency = aggregate["efficiency"]
    safety = aggregate["safety"]
    failures = aggregate["failures"]
    lines = [
        "# Diagnostic orientation constraint set — registered result v0",
        "",
        f"**Classification: `{aggregate['classification']}`.**",
        "",
        "This result evaluates only the registered model and synthetic scenario cohort. "
        "It does not identify the root cause of prior incidents, approve a durable "
        "self-schema, or authorize a runtime integration.",
        "",
        "## Registered estimates",
        "",
        f"- Equal-family effect (`theta`): `{primary['theta']:.4f}`.",
        f"- Family-cluster bootstrap 95% interval: `[{interval['lower']:.4f}, {interval['upper']:.4f}]`.",
        f"- Paired family sign-flip p-value: `{primary['paired_family_sign_flip_p']:.6f}`.",
        f"- Eligible intervention reduction: `{efficiency['reduction']}` ({efficiency['status']}).",
        f"- Treatment authority-source misclassifications: `{safety['treatment_authority_source_misclassifications']}`.",
        f"- Treatment stale/partial decisive uses: `{safety['treatment_stale_partial_decisive_uses']}`.",
        f"- Infrastructure failures: `{failures['infrastructure_count']}` ({failures['infrastructure_rate']:.2%}).",
        "",
        "## Family results",
        "",
        "| Family | Control | Constraint set | Effect |",
        "|---|---:|---:|---:|",
    ]
    for family in FAMILIES:
        row = aggregate["per_family"][family]
        lines.append(
            f"| `{family}` | {row['provider_envelopes_success_rate']:.3f} | "
            f"{row['constraint_set_success_rate']:.3f} | {row['effect']:+.3f} |"
        )
    lines.extend(
        [
            "",
            "## Defects by arm",
            "",
            "```json",
            json.dumps(aggregate["defects_by_arm"], indent=2, sort_keys=True),
            "```",
            "",
            "## Provenance",
            "",
            f"- Enrollment digest: `{bundle['enrollment_digest']}`.",
            f"- Model: `{bundle['model']['record']['reported_model']}` at digest "
            f"`{bundle['model']['record']['digest']}`.",
            f"- Raw scored-output digest: `{bundle['raw_outputs']['scored_sha256']}`.",
            "- Raw outputs remain outside tracked source in the mode-0700 enrollment directory.",
            "",
        ]
    )
    return "\n".join(lines)


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)

    validate = subparsers.add_parser("validate")
    validate.add_argument("--scenarios", type=Path, default=DEFAULT_SCENARIOS)

    enroll = subparsers.add_parser("enroll")
    enroll.add_argument("--scenarios", type=Path, default=DEFAULT_SCENARIOS)
    enroll.add_argument("--protocol", type=Path, default=DEFAULT_PROTOCOL)
    enroll.add_argument("--output", type=Path, required=True)
    enroll.add_argument("--output-dir", type=Path, required=True)
    enroll.add_argument("--analyst-id", required=True)
    enroll.add_argument("--operator-id", default=os.environ.get("USER", "unknown"))
    enroll.add_argument(
        "--ollama-base",
        default=os.environ.get("UNITARES_OLLAMA_BASE", DEFAULT_OLLAMA_BASE),
    )
    enroll.add_argument("--model", default=DEFAULT_MODEL)
    enroll.add_argument("--temperature", type=float, default=0.2)
    enroll.add_argument("--num-predict", type=int, default=320)
    enroll.add_argument("--num-ctx", type=int, default=8192)
    enroll.add_argument("--timeout-s", type=float, default=120.0)

    run = subparsers.add_parser("run")
    run.add_argument("--enrollment", type=Path, required=True)

    summarize = subparsers.add_parser("summarize")
    summarize.add_argument("--result", type=Path, required=True)
    summarize.add_argument("--output", type=Path, default=None)
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        if args.command == "validate":
            scenarios = load_scenarios(args.scenarios)
            manifest = scenario_manifest(scenarios)
            scored = build_scored_schedule(scenarios)
            canaries = build_canary_schedule(scenarios)
            print(
                json.dumps(
                    {
                        "valid": all(row["fact_equality"] for row in manifest),
                        "scenario_count": len(scenarios),
                        "scored_call_count": len(scored),
                        "canary_call_count": len(canaries),
                        "scenario_manifest_digest": sha256_json(manifest),
                        "scored_schedule_digest": sha256_json(scored),
                    },
                    indent=2,
                    sort_keys=True,
                )
            )
            return 0
        if args.command == "enroll":
            enrollment = build_enrollment(
                scenarios_path=args.scenarios.resolve(),
                protocol_path=args.protocol.resolve(),
                output_dir=args.output_dir,
                analyst_id=args.analyst_id,
                operator_id=args.operator_id,
                base_url=args.ollama_base,
                model=args.model,
                temperature=args.temperature,
                num_predict=args.num_predict,
                num_ctx=args.num_ctx,
                timeout_s=args.timeout_s,
            )
            write_enrollment(args.output.resolve(), enrollment)
            print(
                json.dumps(
                    {
                        "enrollment": str(args.output.resolve()),
                        "enrollment_digest": enrollment["enrollment_digest"],
                        "implementation_commit": enrollment["implementation"]["commit"],
                        "model_digest": enrollment["model"]["record"]["digest"],
                        "scored_call_count": enrollment["schedule"]["scored_call_count"],
                    },
                    indent=2,
                    sort_keys=True,
                )
            )
            return 0
        if args.command == "run":
            run_cohort(args.enrollment.resolve())
            return 0
        if args.command == "summarize":
            markdown = render_result_markdown(_load_json(args.result))
            if args.output is None:
                sys.stdout.write(markdown)
            else:
                args.output.write_text(markdown, encoding="utf-8")
            return 0
    except (EnrollmentError, ValueError, OSError, json.JSONDecodeError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    raise AssertionError(f"unhandled command: {args.command}")


if __name__ == "__main__":
    raise SystemExit(main())
