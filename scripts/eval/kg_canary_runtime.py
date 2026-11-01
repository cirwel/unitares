#!/usr/bin/env python3
"""Seal and verify an isolated runtime for the HOLD-only KG plumbing canary.

Use CPython with -I -S -B. ``seal`` and ``prepare`` have no live operation.
``run`` additionally needs an independently reviewed, digest-bound approval.
This is an experiment launcher, not a hostile-native-code security sandbox.
"""
from __future__ import annotations

import os
import sys

# The child installs its guard before importing even the remaining stdlib.
# -S prevents site/.pth startup; -I ignores user paths and Python environment.
_CHILD = __name__ == "__main__" and sys.argv[1:2] == ["--_child"]
_STATE = {"live": False, "reads": [], "artifacts": "", "destinations": set()}


def _within(path, root):
    try:
        return os.path.commonpath((os.path.realpath(path), root)) == root
    except (ValueError, TypeError):
        return False


def _audit(event, args):
    if event in {"subprocess.Popen", "os.system", "os.fork", "os.forkpty",
                 "os.posix_spawn", "os.posix_spawnp", "os.exec", "ctypes.dlopen"}:
        raise PermissionError("canary child process/native-loader operation denied")
    if event == "open" and isinstance(args[0], (str, bytes, os.PathLike)):
        if not os.path.isabs(args[0]):
            raise PermissionError("relative opens cannot be bound to an approved directory")
        path = os.path.realpath(os.fsdecode(args[0]))
        name = os.path.basename(path)
        if (name.startswith(".env") or name in {"auth.json", "secrets.env"}
                or any(part in {".ssh", ".aws", ".unitares"} for part in path.split(os.sep))):
            raise PermissionError("canary credential/config read denied")
        if path.endswith((".pyc", ".pyo")):
            raise PermissionError("canary bytecode-cache access denied")
        flags = args[2]
        write = bool(flags & (os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_TRUNC | os.O_APPEND))
        if write and not _within(path, _STATE["artifacts"]):
            raise PermissionError("canary external file write denied")
        if not write and not any(_within(path, root) for root in _STATE["reads"]):
            raise PermissionError("canary external file read denied")
    if event in {"os.mkdir", "os.remove", "os.rmdir", "os.chmod", "os.truncate", "os.utime", "os.chown"}:
        if (not isinstance(args[0], (str, bytes, os.PathLike)) or not os.path.isabs(args[0])
                or not _within(args[0], _STATE["artifacts"])):
            raise PermissionError("canary external mutation denied")
        index = {"os.mkdir": 2, "os.remove": 1, "os.rmdir": 1, "os.chmod": 2,
                 "os.utime": 3, "os.chown": 3}.get(event)
        if index is not None and len(args) > index and args[index] not in (None, -1):
            raise PermissionError("directory-descriptor mutations denied")
    if event in {"os.rename", "os.replace", "os.link", "os.symlink"}:
        if (not all(isinstance(p, (str, bytes, os.PathLike)) and os.path.isabs(p)
                    and _within(p, _STATE["artifacts"]) for p in args[:2])
                or any(fd not in (None, -1) for fd in args[2:])):
            raise PermissionError("canary external rename/link denied")
    if event.startswith("socket."):
        if not _STATE["live"]:
            raise PermissionError("network disabled during runtime preparation")
        if event in {"socket.connect", "socket.sendto"}:
            address = args[-1]
            if not isinstance(address, tuple) or address[:2] not in _STATE["destinations"]:
                raise PermissionError("canary network destination denied")


if _CHILD:
    if not (sys.flags.isolated and sys.flags.no_site and sys.dont_write_bytecode):
        raise SystemExit("child requires -I -S -B")
    _STATE["artifacts"] = os.path.realpath(sys.argv[4])
    _STATE["reads"] = [os.path.realpath(sys.base_prefix), _STATE["artifacts"]]
    sys.addaudithook(_audit)

import argparse  # noqa: E402
import asyncio  # noqa: E402
from dataclasses import dataclass  # noqa: E402
from datetime import datetime, timezone  # noqa: E402
import hashlib  # noqa: E402
import hmac  # noqa: E402
import importlib  # noqa: E402
import importlib.machinery  # noqa: E402
import io  # noqa: E402
import json  # noqa: E402
from pathlib import Path  # noqa: E402
import resource  # noqa: E402
import shlex  # noqa: E402
import signal  # noqa: E402
import socket  # noqa: E402
import stat  # noqa: E402
import subprocess  # noqa: E402
import sysconfig  # noqa: E402
import threading  # noqa: E402
import time  # noqa: E402
from urllib.parse import parse_qs, urlsplit  # noqa: E402

SCHEMA = "unitares.kg-canary-runtime.v1"
ENTRY = "scripts.eval.run_kg_agent_adoption_live_canary_v1"
LIMIT = 128 * 1024
INPUTS = (
    "tests/kg_agent_adoption/task-chains-v0.json",
    "docs/evaluations/kg-agent-adoption/enrollment-v0.example.json",
    "docs/evaluations/kg-agent-adoption/live-source-map-v0.json",
    "docs/evaluations/kg-agent-adoption/offline-fixture-canary-v0.receipt.json",
)
DB_KEYS = {"DB_BACKEND", "DB_POSTGRES_URL", "DB_AGE_GRAPH", "DB_POSTGRES_MIN_CONN",
           "DB_POSTGRES_MAX_CONN", "UNITARES_KNOWLEDGE_BACKEND"}


def digest(path):
    with Path(path).open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def require(condition, message):
    if not condition:
        raise ValueError(message)


def sync_dir(path):
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def private_dir(path):
    info = path.stat()
    require(info.st_uid == os.getuid() and stat.S_IMODE(info.st_mode) == 0o700,
            "artifact directory must be owned and mode 0700")


def save_new(path, document):
    """An immutable reservation is never replaced or truncated."""
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "w") as stream:
        json.dump(document, stream, indent=2, sort_keys=True)
        stream.write("\n")
        stream.flush()
        os.fsync(stream.fileno())
    sync_dir(path.parent)


def publish(path, document):
    temporary = path.with_suffix(path.suffix + ".writing")
    save_new(temporary, document)
    os.replace(temporary, path)
    sync_dir(path.parent)


def load(path, limit=16 * 1024 * 1024, expected_digest=None, with_digest=False):
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    with os.fdopen(fd, "rb") as stream:
        info = os.fstat(stream.fileno())
        require(stat.S_ISREG(info.st_mode) and info.st_uid == os.getuid()
                and stat.S_IMODE(info.st_mode) == 0o600, "JSON artifact must be owned/private/regular")
        require(info.st_size <= limit, "JSON artifact too large")
        data = stream.read(limit + 1)
    require(len(data) <= limit, "JSON artifact too large")
    if expected_digest is not None:
        require(hashlib.sha256(data).hexdigest() == expected_digest, "approved manifest bytes changed")
    value = json.loads(data)
    require(isinstance(value, dict), "JSON artifact must be an object")
    return (value, hashlib.sha256(data).hexdigest()) if with_digest else value


def code_files(root, *, exclude_site=False):
    for directory, names, files in os.walk(root):
        names[:] = [n for n in names if not n.startswith(".") and n not in
                    {"__pycache__", "node_modules", "venv", "site-packages" if exclude_site else ""}]
        for name in files:
            path = Path(directory, name)
            if path.suffix in {".py", ".so", ".pyd"} or name in {"METADATA", "entry_points.txt", "top_level.txt"}:
                yield path.resolve()


def seal(repo, artifacts, site, db_config):
    private_dir(artifacts)
    require(sys.flags.isolated and sys.flags.no_site and sys.dont_write_bytecode,
            "seal requires python -I -S -B")
    stdlib = Path(sysconfig.get_path("stdlib")).resolve()
    paths = set(code_files(stdlib, exclude_site=True)) | set(code_files(site)) | set(code_files(repo))
    executable = Path(sys.executable).resolve()
    paths.add(executable)
    framework = Path(sys.base_prefix) / "Python"
    if framework.is_file():
        paths.add(framework.resolve())
    files = {str(path): digest(path) for path in sorted(paths)}
    db = db_environment(db_config)
    key_path = artifacts / "db-binding.key"
    fd = os.open(key_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "wb") as stream:
        stream.write(os.urandom(32))
        stream.flush()
        os.fsync(stream.fileno())
    sync_dir(artifacts)
    document = {
        "schema": SCHEMA, "entrypoint": ENTRY, "repo": str(repo), "site": str(site),
        "python": str(executable), "python_version": sys.version,
        "stdlib_path": list(sys.path), "files": files,
        "metadata_files": [path for path in files if Path(path).name in
                           {"METADATA", "entry_points.txt", "top_level.txt"}],
        "bootstrap": str(Path(__file__).resolve()),
        "inputs": {name: digest(repo / name) for name in INPUTS},
        "db_binding": db_binding(artifacts, db),
        "bounds": {"detail_pages": 2, "fixed_searches": 6, "explicit_audit_appends": 1,
                   "client_model_calls": 0, "kg_writes": 0, "automatic_retries": 0},
        "claims": {"scored_run_authorized": False, "parity_proven": False,
                   "durable_calibration_exclusion_proven": False},
    }
    require(document["bootstrap"] in files, "bootstrap must be inside sealed repository")
    save_new(artifacts / "runtime.json", document)
    return {"runtime_sha256": digest(artifacts / "runtime.json"), "code_files": len(files)}


class FreshSourceLoader(importlib.machinery.SourceFileLoader):
    def __init__(self, name, path, expected):
        super().__init__(name, path)
        self.expected = expected

    def get_code(self, fullname):
        data = self.get_data(self.path)
        require(hashlib.sha256(data).hexdigest() == self.expected, "source changed before compilation")
        return compile(data, self.path, "exec", dont_inherit=True, optimize=sys.flags.optimize)


@dataclass(frozen=True)
class CapturedInput:
    """Reviewed bytes with the original path identity, never a later disk read.

    These are the read accessors used by the unchanged v0/v1 validators.
    Generic filesystem coercion refuses instead of reopening a mutable path.
    """
    path: Path
    data: bytes

    def resolve(self):
        return self.path

    def read_bytes(self):
        return self.data

    def read_text(self, encoding="utf-8", errors="strict"):
        return self.data.decode(encoding, errors)

    def open(self, mode="r", encoding="utf-8"):
        require(mode in {"r", "rb"}, "captured input is read-only")
        return io.BytesIO(self.data) if mode == "rb" else io.StringIO(self.read_text(encoding))

    def __fspath__(self):
        raise TypeError("captured inputs must use their verified read accessors")


def capture_inputs(manifest):
    inputs = {}
    for name in INPUTS:
        path = (Path(manifest["repo"]) / name).resolve()
        data = path.read_bytes()
        require(hashlib.sha256(data).hexdigest() == manifest["inputs"][name], "input changed before capture")
        inputs[name] = CapturedInput(path, data)
    return inputs


class OriginFinder:
    """Resolve only the sealed paths; compile Python source without pyc reuse."""
    def __init__(self, files):
        self.files = files
        self.loaded = {}

    @staticmethod
    def find_distributions(*args, **kwargs):
        # PathFinder also carries this metadata protocol. Preserve it when
        # replacing source import resolution, without processing .pth files.
        from importlib.metadata import MetadataPathFinder

        return MetadataPathFinder.find_distributions(*args, **kwargs)

    def find_spec(self, fullname, path=None, target=None):
        spec = importlib.machinery.PathFinder.find_spec(fullname, path, target)
        if spec is None:
            return None
        if spec.loader is None:
            require(all(any(_within(f, os.path.realpath(p)) for f in self.files)
                        for p in spec.submodule_search_locations or ()), "unsealed namespace package")
            return spec
        origin = str(Path(spec.origin).resolve())
        expected = self.files.get(origin)
        require(expected is not None and digest(origin) == expected, "import origin is outside sealed runtime")
        if isinstance(spec.loader, importlib.machinery.SourceFileLoader):
            spec.loader = FreshSourceLoader(fullname, origin, expected)
        else:
            require(isinstance(spec.loader, importlib.machinery.ExtensionFileLoader),
                    "sourceless or unsupported module loader")
        self.loaded[fullname] = {"origin": origin, "sha256": expected}
        return spec


def db_environment(path):
    """Parse only the explicitly selected DB assignments; never interpolate."""
    selected = {}
    with path.open() as stream:
        for raw in stream:
            line = raw.strip().removeprefix("export ")
            name, sep, value = line.partition("=")
            name = name.strip()
            if sep and name.startswith("OPENAI_"):
                raise ValueError("OpenAI assignments forbidden in DB-only config")
            if not sep or name not in DB_KEYS:
                continue
            tokens = shlex.split(value, comments=False)
            if not tokens or tokens[0].startswith("#"):
                continue
            require(len(tokens) == 1 or tokens[1].startswith("#"), "invalid DB assignment")
            selected[name] = tokens[0]
    require(bool(selected.get("DB_POSTGRES_URL")), "explicit DB_POSTGRES_URL required")
    selected.update(DB_POSTGRES_MIN_CONN="1", DB_POSTGRES_MAX_CONN="1")
    target = urlsplit(selected["DB_POSTGRES_URL"])
    require(target.scheme in {"postgres", "postgresql"} and bool(target.hostname),
            "explicit TCP PostgreSQL URL required")
    require(not ({"host", "hostaddr", "port", "database", "passfile", "service"}
                 & set(parse_qs(target.query))), "DB endpoint/credential-file overrides forbidden")
    return selected


def db_binding(artifacts, db):
    key_path = artifacts / "db-binding.key"
    fd = os.open(key_path, os.O_RDONLY | os.O_NOFOLLOW)
    with os.fdopen(fd, "rb") as stream:
        info = os.fstat(stream.fileno())
        require(stat.S_ISREG(info.st_mode) and info.st_uid == os.getuid()
                and stat.S_IMODE(info.st_mode) == 0o600 and info.st_size == 32,
                "DB binding key must be private, regular, and 32 bytes")
        key = stream.read()
    target = urlsplit(db["DB_POSTGRES_URL"])
    return {"settings_hmac_sha256": hmac.new(key, json.dumps(db, sort_keys=True).encode(), hashlib.sha256).hexdigest(),
            "binding_key_sha256": hashlib.sha256(key).hexdigest(),
            "setting_names": sorted(db), "host": target.hostname, "port": target.port or 5432,
            "database": target.path, "graph": db.get("DB_AGE_GRAPH"),
            "backend": db.get("UNITARES_KNOWLEDGE_BACKEND")}


def minimal_environment(artifacts, db):
    home = artifacts / "home"
    home.mkdir(mode=0o700, exist_ok=True)
    env = {"HOME": str(home), "PATH": os.defpath, "LANG": "en_US.UTF-8",
           "PYTHON_DOTENV_DISABLED": "1", **db}
    require(not any("OPENAI" in name or "SESSION" in name or "SLOT" in name for name in env),
            "model credentials or host binding in child environment")
    return env


def validate_manifest(manifest):
    require(manifest.get("schema") == SCHEMA and manifest.get("entrypoint") == ENTRY,
            "wrong runtime schema or entrypoint")
    require(Path(sys.executable).resolve() == Path(manifest["python"])
            and sys.version == manifest["python_version"], "child interpreter differs from reviewed runtime")
    for path in (manifest["python"], manifest["bootstrap"]):
        require(digest(path) == manifest["files"].get(path), "reviewed executable/bootstrap changed")
    for path in manifest["metadata_files"]:
        require(digest(path) == manifest["files"].get(path), "dependency metadata changed")
    for name in INPUTS:
        require(digest(Path(manifest["repo"]) / name) == manifest["inputs"].get(name),
                "frozen canary input changed")


def _watch_parent(artifacts, parent_pid, stopped):
    deadline = time.monotonic() + 180
    while not stopped.wait(0.1):
        reason = "parent_lost" if os.getppid() != parent_pid else "child_deadline" if time.monotonic() >= deadline else None
        if reason:
            try:
                save_new(artifacts / "watchdog.json", {"reason": reason, "automatic_retry": False})
            finally:
                os._exit(125 if reason == "parent_lost" else 124)


def child(mode, manifest_path, artifacts, expected_digest, parent_pid):
    stopped = threading.Event()
    threading.Thread(target=_watch_parent, args=(artifacts, parent_pid, stopped), daemon=True).start()
    manifest = load(manifest_path, expected_digest=expected_digest)
    repo = Path(manifest["repo"])
    _STATE["reads"].extend([str(repo), manifest["site"]])
    require(Path(manifest["bootstrap"]) == Path(__file__).resolve(), "child bootstrap path changed")
    validate_manifest(manifest)
    db = {name: os.environ[name] for name in DB_KEYS if name in os.environ}
    require(db_binding(artifacts, db) == manifest["db_binding"], "child DB settings differ from reviewed target")
    if mode == "run":
        require(load(artifacts / "attempt.json")["runtime_sha256"] == expected_digest,
                "child manifest differs from reserved attempt")
    sys.path[:] = [*manifest["stdlib_path"], str(repo), manifest["site"]]
    finder = OriginFinder(manifest["files"])
    sys.meta_path[:] = [importlib.machinery.BuiltinImporter, importlib.machinery.FrozenImporter, finder]
    module = importlib.import_module(ENTRY)
    # Resolve the actual DB/audit and SDK modules with all external I/O denied.
    for name in ("src.db.postgres_backend", "src.audit_db",
                 "src.mcp_handlers.identity.stable_session", "mcp.client.session",
                 "mcp.client.streamable_http", "src.mcp_compat"):
        importlib.import_module(name)
    inputs = capture_inputs(manifest)
    enrollment, tasks, source_map, offline_receipt = (inputs[INPUTS[i]] for i in (1, 0, 2, 3))
    module.v0.validate_reviewed_offline_basis(
        module.v0._load_json(enrollment), enrollment_path=enrollment,
        tasks=module.v0._load_json(tasks), tasks_path=tasks,
        offline_receipt=module.v0._load_json(offline_receipt),
    )
    require({name: os.environ[name] for name in DB_KEYS if name in os.environ} == db,
            "application imports changed the reviewed DB settings")
    forbidden_env = [name for name in os.environ if "OPENAI" in name or "SESSION" in name or "SLOT" in name]
    require(not forbidden_env, "application imports introduced model credentials or host bindings")
    require(str(Path(module.__file__).resolve()) == str(repo / "scripts/eval/run_kg_agent_adoption_live_canary_v1.py"),
            "entrypoint resolved to another checkout")
    # Modules imported before the finder are stdlib only and are checked too.
    for loaded in tuple(sys.modules.values()):
        origin = getattr(getattr(loaded, "__spec__", None), "origin", None)
        if origin and origin not in {"built-in", "frozen"}:
            path = str(Path(origin).resolve())
            require(digest(path) == manifest["files"].get(path), "preloaded module differs from sealed source")
    indexes = {path: index for index, path in enumerate(sorted(manifest["files"]))}
    origins = {name: indexes[row["origin"]] for name, row in finder.loaded.items()
               if _within(row["origin"], str(repo)) or _within(row["origin"], manifest["site"])}
    save_new(artifacts / f"{mode}.origins.json", {
        "runtime_sha256": digest(manifest_path), "module_origins": origins,
        "origin_encoding": "index into sorted(runtime.files); each entry binds its path and digest",
        "site_startup_disabled": bool(sys.flags.no_site), "source_cache_reuse": False,
        "model_credential_environment_keys": [], "host_binding_environment_keys": [],
        "network_enabled_during_preparation": False, "frozen_inputs": dict(manifest["inputs"]),
        "process_creation_permitted": False,
    })
    if mode == "prepare":
        stopped.set()
        return 0
    require(mode == "run", "unsupported child mode")
    target = urlsplit(os.environ["DB_POSTGRES_URL"])
    host, port = target.hostname, target.port or 5432
    require(bool(host), "Unix/default DB endpoint is not authorized")
    # Resolve only the configured DB target, then enforce exact destinations.
    # DNS here is preparation of the allowlist, not a KG/model request.
    _STATE["live"] = True
    addresses = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    _STATE["destinations"] = {("127.0.0.1", 8767), *((a[4][0], port) for a in addresses)}
    receipt = asyncio.run(module.run_live_canary(
        enrollment_path=enrollment, tasks_path=tasks, source_map_path=source_map,
        offline_receipt_path=offline_receipt, mcp_url="http://127.0.0.1:8767/mcp/",
        timeout_s=15, progress_path=artifacts / "live.progress.jsonl",
        authorization_ref=load(artifacts / "attempt.json")["operator_authorization_ref"],
    ))
    module.v0.write_content_addressed_receipt(artifacts / "live.receipt.json", receipt)
    stopped.set()
    return 0


def bound_files():
    resource.setrlimit(resource.RLIMIT_FSIZE, (LIMIT, LIMIT))


def launch(mode, artifacts, db_config, approval=None):
    private_dir(artifacts)
    manifest_path = artifacts / "runtime.json"
    manifest, runtime_digest = load(manifest_path, with_digest=True)
    validate_manifest(manifest)
    db = db_environment(db_config)
    require(db_binding(artifacts, db) == manifest["db_binding"], "DB configuration differs from reviewed target")
    env = minimal_environment(artifacts, db)
    reviewed = None
    if mode == "run":
        require(not (artifacts / "attempt.json").exists(), "live attempt already reserved; never retry")
        prepared = load(artifacts / "prepare.outcome.json")
        require(prepared.get("status") == "completed" and prepared.get("runtime_sha256") == runtime_digest,
                "reviewed runtime has not passed inert preparation")
        require(approval is not None, "live run needs independent execution approval")
        reviewed = load(approval)
        require(reviewed.get("runtime_sha256") == runtime_digest
                and reviewed.get("scope") == "one_hold_only_infrastructure_canary"
                and reviewed.get("approved") is True
                and isinstance(reviewed.get("review_session"), str) and reviewed["review_session"]
                and isinstance(reviewed.get("operator_authorization_ref"), str)
                and reviewed["operator_authorization_ref"],
                "approval does not bind this one-canary runtime")
    else:
        require(mode == "prepare", "unsupported launcher mode")
    handles = []
    process = None
    outcome = {"status": "setup_failed_before_spawn_no_retry", "mode": mode,
               "runtime_sha256": runtime_digest, "child_started": False}
    handled = {signal.SIGINT, signal.SIGTERM}
    previous_handlers = {}
    previous_mask = None

    def interrupted(signum, frame):
        raise InterruptedError("launcher interrupted")

    def kill_and_reap():
        if process is not None:
            if process.poll() is None:
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
            process.wait()
            if process.stdin is not None and not process.stdin.closed:
                try:
                    process.stdin.close()
                except OSError:
                    pass

    try:
        cache = artifacts / f"{mode}.empty-cache"
        cache.mkdir(mode=0o500)
        for suffix in ("stdout.log", "stderr.log"):
            fd = os.open(artifacts / f"{mode}.{suffix}", os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            handles.append(os.fdopen(fd, "wb", buffering=0))
        sync_dir(artifacts)
        if mode == "run":
            save_new(artifacts / "attempt.json", {
                "started_at": datetime.now(timezone.utc).isoformat(), "runtime_sha256": runtime_digest,
                "review_session": reviewed["review_session"], "automatic_retries": 0,
                "operator_authorization_ref": reviewed["operator_authorization_ref"],
            })
        bootstrap = Path(manifest["bootstrap"]).read_bytes()
        require(hashlib.sha256(bootstrap).hexdigest() == manifest["files"][manifest["bootstrap"]],
                "bootstrap changed before spawn")
        # Execute the verified bytes, not a mutable script pathname. The small
        # prefix imports only builtin sys before the captured guard installs.
        code = "import sys; __file__=sys.argv.pop(1); exec(compile(sys.stdin.buffer.read(), __file__, 'exec'))"
        command = [manifest["python"], "-I", "-S", "-B", "-X", f"pycache_prefix={cache}",
                   "-c", code, manifest["bootstrap"], "--_child", mode, str(manifest_path),
                   str(artifacts), runtime_digest, str(os.getpid())]
        previous_handlers = {sig: signal.getsignal(sig) for sig in handled}
        for sig in handled:
            signal.signal(sig, interrupted)
        previous_mask = signal.pthread_sigmask(signal.SIG_BLOCK, handled)

        def configure_child():
            bound_files()
            signal.pthread_sigmask(signal.SIG_SETMASK, previous_mask)

        process = subprocess.Popen(command, cwd=artifacts, env=env, stdout=handles[0], stderr=handles[1],
                                   stdin=subprocess.PIPE, close_fds=True, start_new_session=True,
                                   preexec_fn=configure_child)
        outcome["child_started"] = True
        try:
            signal.pthread_sigmask(signal.SIG_SETMASK, previous_mask)
            process.communicate(input=bootstrap, timeout=180)
        except subprocess.TimeoutExpired:
            signal.pthread_sigmask(signal.SIG_BLOCK, handled)
            kill_and_reap()
            outcome["status"] = "timeout_ambiguous_no_retry"
        except BaseException as exc:
            signal.pthread_sigmask(signal.SIG_BLOCK, handled)
            kill_and_reap()
            outcome.update(status="interrupted_ambiguous_no_retry", exception_type=type(exc).__name__)
        else:
            outcome.update(returncode=process.returncode,
                           status="completed" if process.returncode == 0 else
                           "child_guard_ambiguous_no_retry" if process.returncode in (124, 125) else "failed_no_retry")
    except BaseException as exc:
        if previous_mask is not None:
            signal.pthread_sigmask(signal.SIG_BLOCK, handled)
        kill_and_reap()
        outcome.update(status="launcher_failure_ambiguous_no_retry" if process is not None else
                       "setup_failed_before_spawn_no_retry", exception_type=type(exc).__name__)
    finally:
        try:
            if previous_mask is not None:
                signal.pthread_sigmask(signal.SIG_BLOCK, handled)
            for handle in handles:
                os.fsync(handle.fileno())
                handle.close()
            outcome["ended_at"] = datetime.now(timezone.utc).isoformat()
            outcome["logs"] = {path.name: {"bytes": path.stat().st_size, "sha256": digest(path)}
                               for path in artifacts.glob(f"{mode}.*.log")}
            publish(artifacts / f"{mode}.outcome.json", outcome)
        finally:
            for sig, handler in previous_handlers.items():
                signal.signal(sig, handler)
            if previous_mask is not None:
                signal.pthread_sigmask(signal.SIG_SETMASK, previous_mask)
    return outcome


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("seal", "prepare", "run"))
    parser.add_argument("--artifacts", type=Path, required=True)
    parser.add_argument("--repo", type=Path, default=Path(__file__).resolve().parents[2])
    parser.add_argument("--site", type=Path, default=Path(sysconfig.get_path("purelib")))
    parser.add_argument("--db-config", type=Path)
    parser.add_argument("--approval", type=Path)
    args = parser.parse_args(argv)
    require(sys.flags.isolated and sys.flags.no_site and sys.dont_write_bytecode,
            "launcher requires python -I -S -B")
    artifacts = args.artifacts.resolve()
    require(args.db_config is not None, "explicit DB-only config required")
    if args.action == "seal":
        result = seal(args.repo.resolve(), artifacts, args.site.resolve(), args.db_config.resolve())
    else:
        result = launch(args.action, artifacts, args.db_config.resolve(), args.approval)
    print(json.dumps(result))
    return 0 if result.get("status", "completed") == "completed" else 1


if __name__ == "__main__":
    if _CHILD:
        raise SystemExit(child(sys.argv[2], Path(sys.argv[3]), Path(sys.argv[4]), sys.argv[5], int(sys.argv[6])))
    raise SystemExit(main())
