"""inference-hosts-config-v0 step 1: vocabulary, fail-closed parser, registry family, doctor."""

from __future__ import annotations

import importlib.util
import os
import sys
from pathlib import Path

import pytest

from src import inference_config as ic

VALID = """
version: 1
hosts:
  antigravity:host-adapter: { enabled: true, family: google }
  external:acme/glm:
    kind: cli
    adapter: claude_code
    model: glm-5.3
    family: zhipu
    env: { ANTHROPIC_BASE_URL: "https://api.z.ai/api/anthropic" }
    secrets: { ANTHROPIC_API_KEY: ZAI_API_KEY }
    egress_class: public_only
    may_approve: false
  external:acme/vllm-lab:
    kind: openai_http
    base_url: "https://vllm.lab.internal/v1"
    model: qwen3-coder
    family: other:qwen
    api_key_env: VLLM_LAB_KEY
    egress_class: agent_work
jobs:
  consult.thorough: { mode: first_answer, prefer: [claude:host-adapter, codex:host-adapter, antigravity:host-adapter] }
  pr_review: { prefer: [codex:host-adapter, claude:host-adapter, antigravity:host-adapter, external:acme/glm] }
"""


def _write(tmp_path: Path, text: str, mode: int = 0o600) -> Path:
    p = tmp_path / "inference.yaml"
    p.write_text(text)
    p.chmod(mode)
    return p


@pytest.fixture(autouse=True)
def _fresh_cache():
    ic._cache.update(key=None, result=None)
    yield
    ic._cache.update(key=None, result=None)


def test_valid_file_parses_hosts_jobs_and_digest():
    cfg = ic.parse_text(VALID)
    assert cfg.hosts["external:acme/glm"].family == "zhipu"
    assert cfg.hosts["external:acme/glm"].egress_class == "public_only"
    assert cfg.hosts["codex:host-adapter"].family == "openai"  # builtin default kept
    assert cfg.jobs["pr_review"].mode == "collect_families"
    assert cfg.jobs["pr_review"].prefer[-1] == "external:acme/glm"
    assert cfg.digest.startswith("sha256:")


def test_defaults_without_a_file_name():
    result = ic.load({})
    assert result.state == "defaults"
    assert set(result.config.hosts) == set(ic.BUILTIN_DEFAULT_FAMILY)
    assert result.config.jobs == {}


@pytest.mark.parametrize("text,where,needle", [
    ("version: 1\nhosts: {}\nhosts: {}\n", "line 3", "duplicate key"),
    ("version: 2\n", "version", "must be 1"),
    ("version: 1\nrouters: {}\n", "file", "unknown field"),
    ("version: 1\nhosts:\n  codex:host-adapter: { family: martian }\n", "hosts.codex:host-adapter.family", "unknown family"),
    ("version: 1\nhosts:\n  codex:host-adapter: { argv: [x] }\n", "hosts.codex:host-adapter", "unknown field"),
    ("version: 1\nhosts:\n  acme/glm: { kind: cli }\n", "hosts.acme/glm", "external:<operator>/<name>"),
    ("version: 1\nhosts:\n  external:acme/x: { kind: shell }\n", "hosts.external:acme/x.kind", "must be one of"),
    ("version: 1\nhosts:\n  external:acme/x: { kind: openai_http, model: m, family: openai, egress_class: agent_work,"
     " base_url: 'https://h/v1', api_key_env: sk-live-abc123 }\n", "hosts.external:acme/x.api_key_env", "never a secret value"),
    ("version: 1\nhosts:\n  external:acme/x: { kind: cli, adapter: claude_code, model: m, family: zhipu,"
     " egress_class: public_only, secrets: { ANTHROPIC_API_KEY: 'a1b2-c3d4' } }\n",
     "hosts.external:acme/x.secrets.ANTHROPIC_API_KEY", "never a secret value"),
    ("version: 1\nhosts:\n  external:acme/x: { kind: cli, adapter: claude_code, model: m, family: zhipu }\n",
     "hosts.external:acme/x.egress_class", "required"),
    ("version: 1\njobs:\n  summarize: { prefer: [ollama:local] }\n", "jobs.summarize", "unknown job"),
    ("version: 1\njobs:\n  pr_review: { mode: first_answer, prefer: [codex:host-adapter] }\n", "jobs.pr_review.mode", "collect_families"),
    ("version: 1\njobs:\n  consult.thorough: { prefer: [codex:host-adapter, codex:host-adapter] }\n", "jobs.consult.thorough.prefer", "twice"),
    ("version: 1\njobs:\n  consult.thorough: { prefer: [external:acme/nope] }\n", "jobs.consult.thorough.prefer", "declared nowhere"),
    ("version: 1\njobs:\n  dialectic_reviewer: { prefer: [codex:host-adapter, claude:host-adapter, antigravity:host-adapter,"
     " hf:router] }\n", "jobs.dialectic_reviewer.prefer", "at most 3"),
])
def test_each_error_names_where_it_failed(text, where, needle):
    with pytest.raises(ic.InferenceConfigError) as exc:
        ic.parse_text(text)
    assert exc.value.where == where, str(exc.value)
    assert needle in exc.value.problem


def test_collect_families_job_may_list_four():
    cfg = ic.parse_text(VALID)
    assert len(cfg.jobs["pr_review"].prefer) == 4


def test_loaded_file_and_error_states(tmp_path):
    good = _write(tmp_path, VALID)
    result = ic.load({ic.HOSTS_FILE_ENV: str(good)})
    assert result.state == "loaded" and result.config.source == str(good)
    bad = tmp_path / "bad.yaml"
    bad.write_text("version: 1\nhosts: [\n")
    bad.chmod(0o600)
    result = ic.load({ic.HOSTS_FILE_ENV: str(bad)})
    assert result.state == "error" and result.config is None  # never falls back to defaults


def test_named_but_missing_file_is_an_error_not_defaults(tmp_path):
    result = ic.load({ic.HOSTS_FILE_ENV: str(tmp_path / "absent.yaml")})
    assert result.state == "error"
    assert "does not exist" in str(result.error)


@pytest.mark.skipif(os.name != "posix", reason="POSIX permissions")
def test_file_writable_by_others_is_refused(tmp_path):
    p = _write(tmp_path, VALID, mode=0o666)
    result = ic.load({ic.HOSTS_FILE_ENV: str(p)})
    assert result.state == "error" and "writable by group or others" in str(result.error)


def test_registry_reports_family_additively(monkeypatch):
    monkeypatch.delenv(ic.HOSTS_FILE_ENV, raising=False)
    from src.mcp_handlers.support import inference_registry as reg

    hosts = {h["host_id"]: h for h in reg.list_inference_hosts(include_unconfigured=True)}
    assert hosts["claude:host-adapter"]["family"] == "anthropic"
    assert hosts["codex:host-adapter"]["family"] == "openai"
    assert hosts["antigravity:host-adapter"]["family"] == "google"
    assert hosts["ollama:local"]["family"] == "unknown"
    # Parity: every key the registry returned before step 1 is still there.
    before = {"host_id", "display_name", "provider_kind", "transport", "configured", "available",
              "privacy_class", "cost_class", "accountability_class", "capabilities", "models",
              "implementation_status", "notes", "accepts_host_id_from", "cooldown", "extension"}
    for host in hosts.values():
        assert before <= set(host)


def test_registry_family_follows_the_operator_file(monkeypatch, tmp_path):
    p = _write(tmp_path, "version: 1\nhosts:\n  ollama:local: { family: google }\n")
    monkeypatch.setenv(ic.HOSTS_FILE_ENV, str(p))
    from src.mcp_handlers.support import inference_registry as reg

    assert reg.get_inference_host("ollama:local")["family"] == "google"


def _doctor():
    root = Path(__file__).resolve().parent.parent
    spec = importlib.util.spec_from_file_location("unitares_doctor_t", root / "scripts/dev/unitares_doctor.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules["unitares_doctor_t"] = mod
    spec.loader.exec_module(mod)
    return mod, root


def test_doctor_check_skips_passes_and_fails(monkeypatch, tmp_path):
    doctor, root = _doctor()
    monkeypatch.delenv(ic.HOSTS_FILE_ENV, raising=False)
    assert doctor.check_inference_hosts_file(root).status == doctor.Status.SKIP
    monkeypatch.setenv(ic.HOSTS_FILE_ENV, str(_write(tmp_path, VALID)))
    ok = doctor.check_inference_hosts_file(root)
    assert ok.status == doctor.Status.PASS and "job pr_review" in ok.detail
    bad = tmp_path / "bad.yaml"
    bad.write_text("version: 1\njobs:\n  summarize: { prefer: [ollama:local] }\n")
    bad.chmod(0o600)
    monkeypatch.setenv(ic.HOSTS_FILE_ENV, str(bad))
    failed = doctor.check_inference_hosts_file(root)
    assert failed.status == doctor.Status.FAIL and "jobs.summarize" in failed.message
