"""Inference hosts and jobs from one operator file: vocabulary and parser.

Design: ``docs/proposals/active/inference-hosts-config-v0.md``. This module is
step 1 of that design. It defines the closed vocabularies every inference
caller will share and parses the operator file named by
``UNITARES_INFERENCE_HOSTS_FILE``. It does not route anything yet: the registry
reads only ``family`` from it, and the doctor shows what the server would load.

Loading fails closed. A file that does not load is reported as an error, and
callers must refuse the jobs it would configure rather than fall back to the
shipped defaults, which could re-enable a host the operator switched off. With
no file named, the shipped defaults apply and behavior is unchanged.

The file never holds secret values: ``api_key_env`` and the values of
``secrets`` are environment variable names, and a value that does not look like
one is rejected.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import hashlib
import os
import re
import stat
from pathlib import Path
from typing import Any, Mapping

HOSTS_FILE_ENV = "UNITARES_INFERENCE_HOSTS_FILE"
SUPPORTED_VERSION = 1

# --- vocabularies (proposal 2.3, 2.4) ---------------------------------------

FAMILIES: tuple[str, ...] = ("anthropic", "openai", "google", "meta", "zhipu", "unknown")
_OTHER_FAMILY = re.compile(r"^other:[a-z0-9][a-z0-9-]{0,39}$")
EGRESS_CLASSES: tuple[str, ...] = ("public_only", "agent_work", "operator_private")
DECLARED_KINDS: tuple[str, ...] = ("openai_http", "cli")
CLI_ADAPTERS: tuple[str, ...] = ("claude_code", "codex", "antigravity")
MODES: tuple[str, ...] = ("first_answer", "collect_families")
# Jobs are a closed set the code defines; an operator cannot invent one.
JOB_MODES: Mapping[str, str] = {
    "consult.thorough": "first_answer",
    "dialectic_reviewer": "first_answer",
    "pr_review": "collect_families",
}
JOB_HOST_CAP: Mapping[str, int] = {"first_answer": 3, "collect_families": 4}
SKIP_REASONS: tuple[str, ...] = ("disabled", "unconfigured", "cooldown", "excluded_family", "egress")

# Builtins stay in code (proposal 2.2); the file may switch one off, set its
# family, and list it in jobs. The default family is the family of the model
# each builtin serves by default; hosts that serve whatever a request names
# are ``unknown`` until a model is pinned.
BUILTIN_DEFAULT_FAMILY: Mapping[str, str] = {
    "ollama:local": "unknown",
    "hf:router": "unknown",
    "claude:host-adapter": "anthropic",
    "codex:host-adapter": "openai",
    "antigravity:host-adapter": "google",
}
_BUILTIN_FIELDS = {"enabled", "family"}
_COMMON_DECLARED = {"kind", "model", "family", "egress_class", "may_approve", "enabled", "timeout_s"}
_KIND_FIELDS: Mapping[str, set[str]] = {
    "openai_http": _COMMON_DECLARED | {"base_url", "api_key_env"},
    "cli": _COMMON_DECLARED | {"adapter", "env", "secrets"},
}
_DECLARED_ID = re.compile(r"^external:[a-z0-9][a-z0-9-]{0,39}/[a-z0-9][a-z0-9._-]{0,63}$")
_ENV_NAME = re.compile(r"^[A-Z_][A-Z0-9_]{0,127}$")
_TOP_LEVEL = {"version", "hosts", "jobs"}


def is_family(value: Any) -> bool:
    return isinstance(value, str) and (value in FAMILIES or bool(_OTHER_FAMILY.match(value)))


class InferenceConfigError(ValueError):
    """The file did not load. ``where`` names the line or field at fault."""

    def __init__(self, where: str, problem: str):
        super().__init__(f"{where}: {problem}")
        self.where = where
        self.problem = problem


@dataclass(frozen=True)
class HostEntry:
    host_id: str
    kind: str  # "builtin" or one of DECLARED_KINDS
    family: str
    enabled: bool = True
    egress_class: str | None = None
    settings: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class JobEntry:
    name: str
    mode: str
    prefer: tuple[str, ...]


@dataclass(frozen=True)
class InferenceConfig:
    hosts: Mapping[str, HostEntry]
    jobs: Mapping[str, JobEntry]
    source: str  # the file path, or "defaults"
    digest: str | None  # sha256 of the file bytes; None for defaults


@dataclass(frozen=True)
class LoadResult:
    """What a caller gets: a config, or the error that must make jobs refuse."""

    state: str  # "defaults" | "loaded" | "error"
    config: InferenceConfig | None
    error: InferenceConfigError | None = None
    path: str | None = None


def default_config() -> InferenceConfig:
    hosts = {hid: HostEntry(hid, "builtin", fam) for hid, fam in BUILTIN_DEFAULT_FAMILY.items()}
    return InferenceConfig(hosts=hosts, jobs={}, source="defaults", digest=None)


# --- YAML with duplicate keys rejected ---------------------------------------

def _yaml_load(text: str) -> Any:
    try:
        import yaml
    except ImportError as exc:  # PyYAML is a core dependency; this is defensive
        raise InferenceConfigError("file", f"PyYAML is not importable ({exc})") from exc

    class _StrictLoader(yaml.SafeLoader):
        pass

    def _mapping(loader: Any, node: Any, deep: bool = False) -> dict:
        seen: dict[Any, Any] = {}
        for key_node, value_node in node.value:
            key = loader.construct_object(key_node, deep=deep)
            if not isinstance(key, str):
                raise InferenceConfigError(
                    f"line {key_node.start_mark.line + 1}", f"mapping key {key!r} must be a string")
            if key in seen:
                raise InferenceConfigError(
                    f"line {key_node.start_mark.line + 1}", f"duplicate key {key!r}")
            seen[key] = loader.construct_object(value_node, deep=deep)
        return seen

    _StrictLoader.add_constructor(yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, _mapping)
    try:
        return yaml.load(text, Loader=_StrictLoader)  # noqa: S506 - SafeLoader subclass
    except InferenceConfigError:
        raise
    except yaml.YAMLError as exc:
        mark = getattr(exc, "problem_mark", None)
        where = f"line {mark.line + 1}" if mark is not None else "file"
        raise InferenceConfigError(where, f"not valid YAML ({getattr(exc, 'problem', exc)})") from exc


# --- validation ---------------------------------------------------------------

def _require_mapping(value: Any, where: str) -> dict:
    if not isinstance(value, dict):
        raise InferenceConfigError(where, "must be a mapping")
    return value


def _unknown_fields(found: Mapping[str, Any], allowed: set[str], where: str) -> None:
    extra = sorted(set(found) - allowed)
    if extra:
        raise InferenceConfigError(where, f"unknown field(s) {', '.join(extra)}")


def _env_name(value: Any, where: str) -> str:
    if not isinstance(value, str) or not _ENV_NAME.match(value):
        raise InferenceConfigError(
            where, "must be an environment variable NAME (e.g. ZAI_API_KEY), never a secret value")
    return value


def _parse_host(host_id: str, raw: Any) -> HostEntry:
    where = f"hosts.{host_id}"
    body = _require_mapping(raw, where)
    if host_id in BUILTIN_DEFAULT_FAMILY:
        _unknown_fields(body, _BUILTIN_FIELDS, where)
        family = body.get("family", BUILTIN_DEFAULT_FAMILY[host_id])
        if not is_family(family):
            raise InferenceConfigError(f"{where}.family", f"unknown family {family!r}")
        enabled = body.get("enabled", True)
        if not isinstance(enabled, bool):
            raise InferenceConfigError(f"{where}.enabled", "must be true or false")
        return HostEntry(host_id, "builtin", family, enabled=enabled)
    if not _DECLARED_ID.match(host_id):
        raise InferenceConfigError(
            where, "a declared host id must look like external:<operator>/<name>")
    kind = body.get("kind")
    if kind not in DECLARED_KINDS:
        raise InferenceConfigError(f"{where}.kind", f"must be one of {', '.join(DECLARED_KINDS)}")
    _unknown_fields(body, _KIND_FIELDS[kind], where)
    if "family" not in body or not is_family(body["family"]):
        raise InferenceConfigError(f"{where}.family", "required, from the family vocabulary")
    egress = body.get("egress_class")
    if egress not in EGRESS_CLASSES:
        raise InferenceConfigError(
            f"{where}.egress_class", f"required, one of {', '.join(EGRESS_CLASSES)}")
    if not isinstance(body.get("model"), str) or not body["model"].strip():
        raise InferenceConfigError(f"{where}.model", "required: a declared host pins one model")
    enabled = body.get("enabled", True)
    if not isinstance(enabled, bool):
        raise InferenceConfigError(f"{where}.enabled", "must be true or false")
    if "may_approve" in body and not isinstance(body["may_approve"], bool):
        raise InferenceConfigError(f"{where}.may_approve", "must be true or false")
    if "timeout_s" in body:
        t = body["timeout_s"]
        if isinstance(t, bool) or not isinstance(t, (int, float)) or not 0 < t <= 3600:
            raise InferenceConfigError(f"{where}.timeout_s", "must be a number of seconds, 1 to 3600")
    if kind == "openai_http":
        url = body.get("base_url")
        if not isinstance(url, str) or not re.match(r"^https?://[^\s/]+", url):
            raise InferenceConfigError(f"{where}.base_url", "required: an http(s) URL")
        if "api_key_env" in body:
            _env_name(body["api_key_env"], f"{where}.api_key_env")
    else:
        if body.get("adapter") not in CLI_ADAPTERS:
            raise InferenceConfigError(
                f"{where}.adapter", f"required, one of {', '.join(CLI_ADAPTERS)}")
        env = _require_mapping(body.get("env", {}), f"{where}.env")
        for k, v in env.items():
            _env_name(k, f"{where}.env")
            if not isinstance(v, str):
                raise InferenceConfigError(f"{where}.env.{k}", "must be a string")
        secrets = _require_mapping(body.get("secrets", {}), f"{where}.secrets")
        for k, v in secrets.items():
            _env_name(k, f"{where}.secrets")
            _env_name(v, f"{where}.secrets.{k}")
            if k in env:
                raise InferenceConfigError(f"{where}.secrets.{k}", "also set in env; set it once")
    settings = {k: v for k, v in body.items() if k not in {"kind", "family", "enabled", "egress_class"}}
    return HostEntry(host_id, kind, body["family"], enabled=enabled, egress_class=egress,
                     settings=settings)


def _parse_job(name: str, raw: Any, hosts: Mapping[str, HostEntry]) -> JobEntry:
    where = f"jobs.{name}"
    if name not in JOB_MODES:
        raise InferenceConfigError(where, f"unknown job; jobs are {', '.join(JOB_MODES)}")
    body = _require_mapping(raw, where)
    _unknown_fields(body, {"mode", "prefer"}, where)
    mode = body.get("mode", JOB_MODES[name])
    if mode != JOB_MODES[name]:
        raise InferenceConfigError(f"{where}.mode", f"this job's mode is {JOB_MODES[name]}")
    prefer = body.get("prefer")
    if not isinstance(prefer, list) or not prefer or not all(isinstance(h, str) for h in prefer):
        raise InferenceConfigError(f"{where}.prefer", "required: a non-empty list of host ids")
    if len(set(prefer)) != len(prefer):
        raise InferenceConfigError(f"{where}.prefer", "lists a host twice")
    cap = JOB_HOST_CAP[mode]
    if len(prefer) > cap:
        raise InferenceConfigError(f"{where}.prefer", f"lists {len(prefer)} hosts; a {mode} job takes at most {cap}")
    for host_id in prefer:
        if host_id not in hosts:
            raise InferenceConfigError(f"{where}.prefer", f"names {host_id!r}, which is declared nowhere")
    return JobEntry(name, mode, tuple(prefer))


def parse_text(text: str, *, source: str = "<text>") -> InferenceConfig:
    data = _yaml_load(text)
    top = _require_mapping(data, "file")
    _unknown_fields(top, _TOP_LEVEL, "file")
    if top.get("version") != SUPPORTED_VERSION:
        raise InferenceConfigError("version", f"must be {SUPPORTED_VERSION}")
    hosts = dict(default_config().hosts)
    for host_id, raw in _require_mapping(top.get("hosts", {}), "hosts").items():
        if not isinstance(host_id, str):
            raise InferenceConfigError("hosts", f"host id {host_id!r} must be a string")
        hosts[host_id] = _parse_host(host_id, raw)
    jobs = {name: _parse_job(name, raw, hosts)
            for name, raw in _require_mapping(top.get("jobs", {}), "jobs").items()}
    digest = "sha256:" + hashlib.sha256(text.encode("utf-8")).hexdigest()
    return InferenceConfig(hosts=hosts, jobs=jobs, source=source, digest=digest)


def _check_private(path: Path) -> None:
    try:
        info = path.stat()
    except FileNotFoundError:
        raise InferenceConfigError("file", f"{path} does not exist (named by {HOSTS_FILE_ENV})")
    if not stat.S_ISREG(info.st_mode):
        raise InferenceConfigError("file", f"{path} is not a regular file")
    if info.st_uid != os.getuid():
        raise InferenceConfigError("file", f"{path} is not owned by the user running the server")
    if info.st_mode & 0o022:
        raise InferenceConfigError("file", f"{path} is writable by group or others (chmod go-w)")


_cache: dict[str, Any] = {"key": None, "result": None}


def load(environ: Mapping[str, str] | None = None) -> LoadResult:
    """Load the operator's inference hosts file, or the shipped defaults when none is named.

    Cached on the file's path, size and mtime.
    """
    raw_path = ((os.environ if environ is None else environ).get(HOSTS_FILE_ENV) or "").strip()
    if not raw_path:
        return LoadResult("defaults", default_config())
    path = Path(raw_path).expanduser()
    try:
        info = path.stat()
        # Permissions and owner are part of the key, so a chmod that fixes (or
        # breaks) the file takes effect without a restart.
        key = (str(path), info.st_size, info.st_mtime_ns, info.st_mode, info.st_uid)
    except OSError:
        key = None
    if key is not None and _cache["key"] == key:
        return _cache["result"]
    try:
        _check_private(path)
        result = LoadResult("loaded", parse_text(path.read_text(encoding="utf-8"), source=str(path)),
                            path=str(path))
    except InferenceConfigError as exc:
        result = LoadResult("error", None, exc, path=str(path))
    except (OSError, UnicodeDecodeError) as exc:
        result = LoadResult("error", None, InferenceConfigError("file", f"unreadable ({exc})"), path=str(path))
    except Exception as exc:  # fail closed on anything the validators did not anticipate
        result = LoadResult("error", None,
                            InferenceConfigError("file", f"could not be validated ({type(exc).__name__}: {exc})"),
                            path=str(path))
    if key is not None:
        _cache.update(key=key, result=result)
    return result


def host_family(host_id: str, environ: Mapping[str, str] | None = None) -> str | None:
    """A host's declared family, or its builtin default when the file did not load.

    Family is descriptive in step 1: it changes no routing. On a load error the
    builtin default is reported, since nothing reads it to decide anything yet.
    """
    result = load(environ)
    config = result.config or default_config()
    entry = config.hosts.get(host_id)
    return entry.family if entry is not None else None


def describe(result: LoadResult) -> list[str]:
    """Human-readable lines for the doctor: source, hosts and job orders."""
    if result.state == "error":
        assert result.error is not None
        return [f"{result.path}: {result.error}",
                "jobs this file would configure must refuse until it loads (fail closed)"]
    config = result.config or default_config()
    lines = [f"source: {config.source}" + (f" ({config.digest[:19]}...)" if config.digest else "")]
    for host in config.hosts.values():
        extra = f", egress {host.egress_class}" if host.egress_class else ""
        state = "" if host.enabled else ", disabled"
        lines.append(f"host {host.host_id}: {host.kind}, family {host.family}{extra}{state}")
    for job in config.jobs.values():
        lines.append(f"job {job.name} ({job.mode}): {' -> '.join(job.prefer)}")
    if not config.jobs:
        lines.append("jobs: none declared; each caller keeps its built-in order")
    lines.append("routing does not read this file yet (inference-hosts-config-v0 step 2)")
    return lines
