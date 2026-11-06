"""Opt-in native acceptance against a private, Unix-socket-only PG17 cluster.

UNITARES_NATIVE_TEST_PG_BIN=/opt/homebrew/opt/postgresql@17/bin pytest ...
Requires the installed AGE/pgvector libraries. Never touches brew services or
the operator's databases. The cluster is stopped in the fixture's finalizer.
"""
import asyncio
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
from types import SimpleNamespace

import pytest

from scripts.install import homebrew_provision as provision
from scripts.install import homebrew_services as native

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def cluster(tmp_path, monkeypatch):
    configured = os.environ.get('UNITARES_NATIVE_TEST_PG_BIN')
    if not configured:
        pytest.skip('opt-in private PG17/AGE/pgvector acceptance')
    binaries = Path(configured)
    prefix = tmp_path / 'brew'
    data = prefix / 'var/postgresql@17'
    data.parent.mkdir(parents=True)
    env = {k: v for k, v in os.environ.items() if not k.startswith('PG')}
    env['PATH'] = str(binaries) + os.pathsep + env.get('PATH', '')
    execute = subprocess.run
    def command(args, **kwargs):
        return execute(args, env=env, text=True, capture_output=True, check=True, **kwargs)
    command([str(binaries / 'initdb'), '-D', str(data), '-A', 'trust', '--no-locale'])
    with tempfile.TemporaryDirectory(prefix='unit-pg-', dir='/tmp') as socket:
        command([str(binaries / 'pg_ctl'), '-D', str(data), '-l', str(tmp_path / 'postgres.log'),
                 '-o', f'-k {socket} -p 15437 -h ""', 'start'])
        try:
            def sql(statement, database='postgres', user=None):
                args = [str(binaries / 'psql'), '-X', '-Atq', '-v', 'ON_ERROR_STOP=1',
                        '-h', socket, '-p', '15437', '-d', database]
                if user:
                    args += ['-U', user]
                return command(args, input=statement).stdout.strip()
            available = sql("SELECT count(*) FROM pg_available_extensions WHERE name IN ('age','vector')")
            assert available == '2', 'Native acceptance requires AGE and pgvector for this PG17'
            yield prefix, socket, env, sql, execute, binaries
        finally:
            command([str(binaries / 'pg_ctl'), '-D', str(data), '-m', 'fast', 'stop'])


def test_real_provisioning_nonsuperuser_graph_and_no_bootstrap_replay(cluster, monkeypatch):
    prefix, socket, env, sql, execute, binaries = cluster
    brew = native.Homebrew.__new__(native.Homebrew)
    brew.prefix, brew.app, brew.env = prefix, ROOT, env
    brew.config = prefix / 'etc/unitares/native.env'
    pg = prefix / 'opt/postgresql@17/bin'
    pg.parent.mkdir(parents=True)
    pg.symlink_to(binaries)
    python = prefix / 'opt/unitares/libexec/venv/bin/python'
    python.parent.mkdir(parents=True)
    python.symlink_to(sys.executable)
    (python.parent / 'python3').symlink_to(sys.executable)
    monkeypatch.delenv('DB_POSTGRES_URL', raising=False)
    monkeypatch.setattr(brew, 'check_installation', lambda: None)
    # Service control is simulated; all SQL/bootstrap/schema verification is real.
    monkeypatch.setattr(provision.NativeProvisioner, 'preflight',
                        lambda self: {name: {'running': name not in native.WRITERS} for name in native.SERVICES})
    monkeypatch.setattr(provision.NativeProvisioner, 'bind_redis', lambda self: None)
    original_environment = brew.database_environment
    def database_environment():
        _, runtime = original_environment()
        url = f'postgresql://unitares@/unitares?host={socket}&port=15437'
        runtime['DB_POSTGRES_URL'] = runtime['UNITARES_LEASE_PLANE_DATABASE_URL'] = url
        return url, runtime
    monkeypatch.setattr(brew, 'database_environment', database_environment)
    def redirect_private_cluster(args, **kwargs):
        args = list(args)
        if Path(args[0]).name == 'psql' and '-h' in args:
            args[args.index('-h') + 1] = socket
            args[args.index('-p') + 1] = '15437'
        return execute(args, **kwargs)
    monkeypatch.setattr(provision.subprocess, 'run', redirect_private_cluster)
    provision.NativeProvisioner(brew).setup(apply=True)
    state = provision.read_journal(prefix / 'var/unitares/native-setup.json')
    assert state['stage'] == 'complete'
    assert sql("SELECT rolsuper OR rolcreatedb OR rolcreaterole OR rolreplication OR rolbypassrls FROM pg_roles WHERE rolname='unitares'") == 'f'
    assert sql("SELECT DISTINCT tableowner FROM pg_tables WHERE schemaname IN ('core','audit','knowledge','governance_graph')", 'unitares') == 'unitares'
    assert sql("SET search_path=ag_catalog,public; SELECT * FROM cypher('governance_graph', $$ CREATE (n:Acceptance {value: 7}) RETURN n.value $$) AS (v agtype)", 'unitares', 'unitares') == '7'
    async def runtime_readiness():
        import asyncpg
        from src.db.mixins.graph import GraphMixin
        graph = GraphMixin()
        graph._age_graph = 'governance_graph'
        conn = await asyncpg.connect(host=socket, port=15437, user='unitares', database='unitares')
        try:
            async with conn.transaction():
                await graph._prepare_age_connection(conn)
                await graph._ensure_age_graph_exists(conn, external_conn=True)
                await graph._probe_age_graph(conn)
        finally:
            await conn.close()
    asyncio.run(runtime_readiness())
    # AGE's extension configuration tables are included in database backups.
    # The application role must be able to dump them without becoming superuser.
    backup = prefix / 'acceptance.dump'
    result = execute([str(binaries / 'pg_dump'), '-h', socket, '-p', '15437', '-U', 'unitares',
                      '-d', 'unitares', '-Fc', '-f', str(backup)], env=env, text=True, capture_output=True)
    assert result.returncode == 0, result.stderr
    result = execute([str(binaries / 'pg_restore'), '--list', str(backup)], env=env, text=True, capture_output=True)
    assert result.returncode == 0, result.stderr
    before = sql('SELECT count(*) FROM core.schema_migrations', 'unitares')
    url, runtime = database_environment()
    replay = execute(['bash', str(ROOT / 'scripts/install/bootstrap_postgres.sh'), '--apply',
                      '--install-id', state['install_id'], '--db-url', url],
                     env=runtime, capture_output=True, text=True)
    assert replay.returncode != 0
    assert 'Bootstrap refused' in replay.stderr
    assert sql('SELECT count(*) FROM core.schema_migrations', 'unitares') == before
    assert sql("SET search_path=ag_catalog,public; SELECT * FROM cypher('governance_graph', $$ MATCH (n:Acceptance) RETURN n.value $$) AS (v agtype)", 'unitares', 'unitares') == '7'
    config = brew.config.read_bytes()
    provision.NativeProvisioner(brew).setup(apply=True)
    assert brew.config.read_bytes() == config


@pytest.mark.parametrize('contents', ['empty', 'foreign-data', 'interrupted'])
def test_real_bootstrap_refuses_unowned_or_interrupted_database(cluster, contents):
    _, socket, env, sql, execute, _ = cluster
    sql('CREATE DATABASE guard_fixture;')
    install_id = '33333333-3333-4333-8333-333333333333'
    if contents == 'foreign-data':
        sql('CREATE TABLE public.keep_me(value int); INSERT INTO public.keep_me VALUES (7);', 'guard_fixture')
    elif contents == 'interrupted':
        sql(f"COMMENT ON DATABASE guard_fixture IS 'unitares-install:{install_id}:bootstrapping';")
    result = execute(['bash', str(ROOT / 'scripts/install/bootstrap_postgres.sh'), '--apply',
                      '--install-id', install_id, '--db-url', f'postgresql:///guard_fixture?host={socket}&port=15437'],
                     env=env, capture_output=True, text=True)
    assert result.returncode != 0
    assert 'Bootstrap refused' in result.stderr
    assert sql("SELECT count(*) FROM pg_namespace WHERE nspname='core'", 'guard_fixture') == '0'
    if contents == 'foreign-data':
        assert sql('SELECT value FROM public.keep_me', 'guard_fixture') == '7'


def test_real_redis_claim_preserves_foreign_keys(tmp_path, monkeypatch):
    configured = os.environ.get('UNITARES_NATIVE_TEST_REDIS_BIN')
    if not configured:
        pytest.skip('opt-in private Redis acceptance')
    binaries = Path(configured)
    with tempfile.TemporaryDirectory(prefix='unit-redis-', dir='/tmp') as directory:
        socket = Path(directory) / 'redis.sock'
        with (tmp_path / 'redis.log').open('w') as log:
            process = subprocess.Popen([str(binaries / 'redis-server'), '--port', '0',
                                        '--unixsocket', str(socket), '--save', '', '--appendonly', 'no'],
                                       stdout=log, stderr=log)
            try:
                deadline = time.monotonic() + 10
                while not socket.exists() and time.monotonic() < deadline and process.poll() is None:
                    time.sleep(0.05)
                assert socket.exists(), 'private Redis did not start'
                def redirect(args, **kwargs):
                    command = [str(binaries / 'redis-cli'), '-s', str(socket), '-n', '0', '--raw']
                    command += args[args.index('--raw') + 1:]
                    return subprocess.run(command, text=True, capture_output=True, **kwargs)
                monkeypatch.setattr(provision, 'run', redirect)
                p = provision.NativeProvisioner(SimpleNamespace(prefix=tmp_path, env={}))
                p.state = {'install_id': 'fixture-install'}
                p.bind_redis()
                p.bind_redis()
                assert p.redis('DBSIZE') == '1'
                p.redis('SET', 'foreign', 'keep')
                with pytest.raises(native.Refusal, match='Redis changed'):
                    p.bind_redis()
                assert p.redis('GET', 'foreign') == 'keep'
                assert p.redis('GET', provision.REDIS_INSTALL_KEY) == 'fixture-install'
            finally:
                process.terminate()
                process.wait(timeout=10)
