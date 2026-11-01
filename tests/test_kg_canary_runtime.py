from __future__ import annotations

import hashlib
import importlib.util
import json
import marshal
import os
from pathlib import Path
import shutil
import signal
import struct
import subprocess
import sys
import time

import pytest

from scripts.eval import kg_canary_runtime as runtime


def test_fresh_loader_ignores_a_success_looking_stale_pyc(tmp_path):
    source = tmp_path / "target.py"
    source.write_text("answer = 'source'\n")
    cached = Path(importlib.util.cache_from_source(str(source)))
    cached.parent.mkdir()
    stat = source.stat()
    # A timestamp/size-valid cache would execute a different payload normally.
    cached.write_bytes(importlib.util.MAGIC_NUMBER + struct.pack("<III", 0, int(stat.st_mtime), stat.st_size)
                       + marshal.dumps(compile("answer = 'stale bytecode'", str(source), "exec")))
    loader = runtime.FreshSourceLoader("target", str(source), runtime.digest(source))
    values = {}
    exec(loader.get_code("target"), values)
    assert values["answer"] == "source"
    source.write_text("answer = 'changed'\n")
    with pytest.raises(ValueError, match="changed before compilation"):
        loader.get_code("target")


def test_origin_finder_refuses_shadow_and_sourceless_modules(tmp_path, monkeypatch):
    source = tmp_path / "shadow.py"
    source.write_text("answer = 1\n")
    monkeypatch.syspath_prepend(str(tmp_path))
    finder = runtime.OriginFinder({})
    with pytest.raises(ValueError, match="outside sealed runtime"):
        finder.find_spec("shadow")
    finder = runtime.OriginFinder({str(source): runtime.digest(source)})
    spec = finder.find_spec("shadow")
    assert isinstance(spec.loader, runtime.FreshSourceLoader)
    sourceless = tmp_path / "only_bytecode.pyc"
    sourceless.write_bytes(importlib.util.MAGIC_NUMBER + b"\0" * 12)
    finder = runtime.OriginFinder({str(sourceless): runtime.digest(sourceless)})
    with pytest.raises(ValueError, match="sourceless"):
        finder.find_spec("only_bytecode")


def test_db_config_selects_only_db_assignments_without_interpolation(tmp_path):
    config = tmp_path / "settings.env"
    config.write_text('IGNORED="unterminated\nDB_POSTGRES_URL="postgresql://u:p$word@localhost/db" # comment\n'
                      'DB_AGE_GRAPH=graph\nOTHER_TOKEN=secret\n')
    selected = runtime.db_environment(config)
    assert selected["DB_POSTGRES_URL"] == "postgresql://u:p$word@localhost/db"
    assert selected["DB_POSTGRES_MAX_CONN"] == "1"
    assert "OTHER_TOKEN" not in selected
    config.write_text(config.read_text() + "OPENAI_API_KEY=not-a-real-key\n")
    with pytest.raises(ValueError, match="OpenAI assignments forbidden"):
        runtime.db_environment(config)


def test_minimal_environment_has_no_inherited_credentials_or_slots(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "not-a-real-key")
    monkeypatch.setenv("UNITARES_CLIENT_SESSION_ID", "not-a-real-session")
    monkeypatch.setenv("PYTHONPATH", "/unreviewed")
    env = runtime.minimal_environment(tmp_path, {"DB_POSTGRES_URL": "postgresql://localhost/test"})
    assert not any("OPENAI" in k or "SESSION" in k or "SLOT" in k for k in env)
    assert "PYTHONPATH" not in env
    assert env["PYTHON_DOTENV_DISABLED"] == "1"
    assert env["HOME"] == str(tmp_path / "home")


def test_reservation_is_immutable_and_directory_synced(tmp_path, monkeypatch):
    calls = []
    real = runtime.sync_dir

    def sync(path):
        calls.append(path)
        real(path)

    monkeypatch.setattr(runtime, "sync_dir", sync)
    marker = tmp_path / "attempt.json"
    runtime.save_new(marker, {"status": "reserved"})
    original = marker.read_bytes()
    with pytest.raises(FileExistsError):
        runtime.save_new(marker, {"status": "replaced"})
    assert marker.read_bytes() == original
    assert calls == [tmp_path]
    runtime.publish(tmp_path / "outcome.json", {"status": "ambiguous"})
    assert marker.read_bytes() == original
    assert json.loads((tmp_path / "outcome.json").read_text())["status"] == "ambiguous"
    assert (tmp_path / "outcome.json").stat().st_mode & 0o777 == 0o600


def test_private_json_rejects_symlinks_and_public_files(tmp_path):
    target = tmp_path / "target.json"
    runtime.save_new(target, {})
    link = tmp_path / "link.json"
    link.symlink_to(target)
    with pytest.raises(OSError):
        runtime.load(link)
    target.chmod(0o644)
    with pytest.raises(ValueError, match="owned/private/regular"):
        runtime.load(target)


@pytest.fixture
def isolated_case(tmp_path, request):
    repo = tmp_path / "repo"
    site = tmp_path / "site"
    artifacts = tmp_path / "artifacts"
    for path in (repo, site, artifacts):
        path.mkdir(mode=0o700)
    bootstrap = repo / "scripts/eval/kg_canary_runtime.py"
    bootstrap.parent.mkdir(parents=True)
    shutil.copyfile(runtime.__file__, bootstrap)
    modules = ["scripts", "scripts.eval", "src", "src.db", "src.audit_db", "src.db.postgres_backend",
               "src.mcp_handlers", "src.mcp_handlers.identity", "src.mcp_handlers.identity.stable_session",
               "mcp", "mcp.client", "mcp.client.session", "mcp.client.streamable_http", "src.mcp_compat"]
    packages = {"scripts", "scripts.eval", "src", "src.db", "src.mcp_handlers",
                "src.mcp_handlers.identity", "mcp", "mcp.client"}
    for name in modules:
        path = (repo if name.startswith(("scripts", "src")) else site) / name.replace(".", "/")
        path = path / "__init__.py" if name in packages else path.with_suffix(".py")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("")
    entry = repo / "scripts/eval/run_kg_agent_adoption_live_canary_v1.py"
    extra = ""
    variant = getattr(request, "param", "normal")
    if variant in {"unlink", "open", "chmod"}:
        victim = repo / "victim.txt"
        victim.write_text("preserve me")
        extra = "fd = os.open(str(Path(__file__).resolve().parents[2]), os.O_RDONLY)\n"
        extra += {"unlink": "os.unlink('victim.txt', dir_fd=fd)\n",
                  "open": "os.open('victim.txt', os.O_WRONLY|os.O_TRUNC, dir_fd=fd)\n",
                  "chmod": "os.fchmod(fd, 0)\n"}[variant]
    elif variant == "retarget":
        extra = "os.environ['DB_AGE_GRAPH'] = 'unreviewed'\n"
    sleep = ("(Path(os.environ['HOME']).parent / 'child.pid').write_text(str(os.getpid()))\n"
             "import time\ntime.sleep(120)\n") if variant == "sleep" else ""
    entry.write_text(
        "import socket\nfrom pathlib import Path\nimport os\n"
        "from scripts.eval import run_kg_agent_adoption_live_canary as v0\n"
        "from importlib.metadata import version\nassert version('canary-fixture') == '1.0'\n"
        "socket.getaddrinfo = lambda *a, **k: [(socket.AF_INET, socket.SOCK_STREAM, 6, '', ('127.0.0.1', 5432))]\n"
        + extra + sleep +
        "async def run_live_canary(**kwargs):\n"
        "    assert kwargs['authorization_ref'] == 'operator-test'\n"
        "    (Path(os.environ['HOME']).parent / 'entered-main').write_text('once')\n"
        + "    content = {'status': 'hold', 'scope': 'inert test'}\n"
        "    return {'schema': 'unitares.content-addressed-receipt.v0', 'content': content, 'receipt_content_sha256': v0.sha256_json(content)}\n")
    source_root = Path(runtime.__file__).resolve().parents[2]
    for name in ("scripts/eval/run_kg_agent_adoption_live_canary.py", "scripts/eval/run_kg_agent_adoption.py",
                 "scripts/eval/kg_agent_adoption.py", "scripts/dev/portable_knowledge_bundle.py"):
        path = repo / name
        path.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source_root / name, path)
    (repo / "scripts/dev/__init__.py").write_text("")
    for name in runtime.INPUTS:
        path = repo / name
        path.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source_root / name, path)
    config = tmp_path / "db.env"
    config.write_text("DB_POSTGRES_URL=postgresql://localhost/fake\n")
    # These startup hooks must never run, even though their directory is used.
    hook = f"open({str(artifacts / 'site-hook-ran')!r}, 'w').write('bad')"
    (site / "sitecustomize.py").write_text(hook)
    (site / "canary-test.pth").write_text("import os; " + hook + "\n")
    metadata = site / "canary_fixture-1.0.dist-info/METADATA"
    metadata.parent.mkdir()
    metadata.write_text("Metadata-Version: 2.1\nName: canary-fixture\nVersion: 1.0\n")
    command = [sys.executable, "-I", "-S", "-B", str(bootstrap)]
    seal = subprocess.run([*command, "seal", "--repo", str(repo), "--site", str(site),
                           "--artifacts", str(artifacts), "--db-config", str(config)],
                          text=True, capture_output=True, timeout=30)
    assert seal.returncode == 0, seal.stderr
    return command, artifacts, config, repo, site


def invoke(case, mode, *args):
    command, artifacts, config, _, _ = case
    return subprocess.run([*command, mode, "--artifacts", str(artifacts), "--db-config", str(config), *args],
                          text=True, capture_output=True, timeout=30)


def test_real_child_skips_site_hooks_and_has_no_live_execution_path(isolated_case):
    _, artifacts, _, _, _ = isolated_case
    prepared = invoke(isolated_case, "prepare")
    assert prepared.returncode == 0, prepared.stderr
    assert not (artifacts / "site-hook-ran").exists()
    assert not (artifacts / "entered-main").exists()
    assert not list((artifacts / "prepare.empty-cache").rglob("*.pyc"))
    refused = invoke(isolated_case, "run")
    assert refused.returncode != 0
    assert not (artifacts / "attempt.json").exists()
    with pytest.raises(ValueError, match="live execution withdrawn"):
        runtime.child("run", None, None, None, None)
    with pytest.raises(ValueError, match="live execution withdrawn"):
        runtime.launch("run", artifacts, None)


def test_child_refuses_import_drift_before_entrypoint(isolated_case):
    _, artifacts, _, _, site = isolated_case
    (site / "mcp/client/session.py").write_text("raise AssertionError('unreviewed source executed')\n")
    result = invoke(isolated_case, "prepare")
    assert result.returncode != 0
    assert not (artifacts / "entered-main").exists()
    errors = (artifacts / "prepare.stderr.log").read_text()
    assert "outside sealed runtime" in errors
    assert "unreviewed source executed" not in errors


def test_config_target_and_graph_are_bound_before_reservation(isolated_case):
    _, artifacts, config, _, _ = isolated_case
    config.write_text("DB_POSTGRES_URL=postgresql://192.0.2.9/changed\nDB_AGE_GRAPH=changed\n")
    refused = invoke(isolated_case, "prepare")
    assert refused.returncode != 0
    assert "differs from reviewed target" in refused.stderr
    assert not (artifacts / "attempt.json").exists()
    config.unlink()
    assert invoke(isolated_case, "prepare").returncode != 0
    assert not (artifacts / "attempt.json").exists()


def test_manifest_digest_checks_the_same_bytes_that_are_decoded(tmp_path):
    path = tmp_path / "runtime.json"
    runtime.save_new(path, {"approved": True})
    approved = runtime.digest(path)
    path.write_text('{"approved": false}')
    with pytest.raises(ValueError, match="approved manifest bytes changed"):
        runtime.load(path, expected_digest=approved)


def test_captured_inputs_keep_approved_bytes_after_the_disk_changes(tmp_path):
    for name in runtime.INPUTS:
        path = tmp_path / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b'{"approved": true}\n')
    manifest = {"repo": str(tmp_path), "inputs": {name: runtime.digest(tmp_path / name) for name in runtime.INPUTS}}
    captured = runtime.capture_inputs(manifest)
    for name in runtime.INPUTS:
        (tmp_path / name).write_bytes(b'{"approved": false}\n')
        value = captured[name]
        assert value.resolve() == (tmp_path / name).resolve()
        assert json.loads(value.read_text())["approved"] is True
        with value.open("rb") as stream:
            assert hashlib.sha256(stream.read()).hexdigest() == manifest["inputs"][name]
        with pytest.raises(TypeError, match="verified read accessors"):
            open(value)


@pytest.mark.parametrize("isolated_case", ["unlink", "open", "chmod"], indirect=True)
def test_actual_child_denies_relative_dirfd_and_descriptor_mutations(isolated_case):
    _, artifacts, _, repo, _ = isolated_case
    mode = repo.stat().st_mode
    result = invoke(isolated_case, "prepare")
    assert result.returncode != 0
    assert (repo / "victim.txt").read_text() == "preserve me"
    assert repo.stat().st_mode == mode
    assert not (artifacts / "entered-main").exists()


@pytest.mark.parametrize("isolated_case", ["retarget"], indirect=True)
def test_actual_child_rechecks_database_settings_after_application_imports(isolated_case):
    _, artifacts, _, _, _ = isolated_case
    result = invoke(isolated_case, "prepare")
    assert result.returncode != 0
    assert "imports changed the reviewed DB settings" in (artifacts / "prepare.stderr.log").read_text()
    assert not (artifacts / "attempt.json").exists()
    assert not (artifacts / "entered-main").exists()


@pytest.mark.parametrize("isolated_case", ["sleep"], indirect=True)
@pytest.mark.parametrize("interrupt", [signal.SIGINT, signal.SIGTERM])
def test_parent_interrupt_kills_reaps_and_records_ambiguous_preparation(isolated_case, interrupt):
    command, artifacts, config, _, _ = isolated_case
    parent = subprocess.Popen([*command, "prepare", "--artifacts", str(artifacts), "--db-config", str(config)],
                              stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    child_pid = None
    try:
        deadline = time.monotonic() + 10
        while not (artifacts / "child.pid").exists() and parent.poll() is None and time.monotonic() < deadline:
            time.sleep(0.02)
        assert (artifacts / "child.pid").exists()
        child_pid = int((artifacts / "child.pid").read_text())
        parent.send_signal(interrupt)
        parent.communicate(timeout=10)
        outcome = json.loads((artifacts / "prepare.outcome.json").read_text())
        assert outcome["status"] == "interrupted_ambiguous_no_retry"
        assert not (artifacts / "attempt.json").exists()
        with pytest.raises(ProcessLookupError):
            os.kill(child_pid, 0)
    finally:
        if parent.poll() is None:
            parent.kill()
            parent.wait()
        if child_pid is not None:
            try:
                os.killpg(child_pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
