#!/usr/bin/env python3
"""Supervise private backing services; stdout belongs exclusively to MCP."""
import base64
import json
import os
from pathlib import Path
import pwd
import secrets
import signal
import subprocess
import sys
import time
import urllib.request

DATA = Path('/data')
PG = DATA / 'postgres'
MARKER = DATA / 'schema.sha256'
CHILDREN = []
STOP = False
VOLUME_LOCK = None


def log(message):
    print(f'[unitares-bundle] {message}', file=sys.stderr, flush=True)


def stopping(signum, frame):
    global STOP
    STOP = True


def start(name, argv, user=None, mcp=False, cwd='/app'):
    account = pwd.getpwnam(user) if user else None
    child = subprocess.Popen(
        argv, cwd=cwd, env=os.environ,
        stdin=None if mcp else subprocess.DEVNULL,
        stdout=None if mcp else sys.stderr, stderr=sys.stderr,
        user=account.pw_uid if account else None,
        group=account.pw_gid if account else None,
        extra_groups=[] if account else None, start_new_session=True,
    )
    CHILDREN.append((name, child))
    return child


def check_children():
    if STOP:
        raise InterruptedError('shutdown requested')
    for name, child in CHILDREN:
        if child.poll() is not None:
            raise RuntimeError(f'{name} exited ({child.returncode})')


def wait_ready(name, probe, timeout):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        check_children()
        try:
            if probe():
                log(f'{name} ready')
                return
        except (OSError, ValueError):
            pass
        time.sleep(.25)
    raise RuntimeError(f'{name} readiness timed out after {timeout}s')


def command_ok(argv):
    return subprocess.run(argv, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                          stderr=subprocess.DEVNULL, timeout=3).returncode == 0


def http_ready(port, path, token):
    req = urllib.request.Request(f'http://127.0.0.1:{port}{path}',
                                 headers={'Authorization': f'Bearer {token}'})
    with urllib.request.urlopen(req, timeout=2) as response:
        return response.status == 200


def configure():
    if os.geteuid() != 0:
        raise RuntimeError('startup requires root to set /data ownership; services run unprivileged')
    DATA.mkdir(exist_ok=True)
    # Lock the volume for the whole lifetime, preventing concurrent writers.
    import fcntl
    lock = open(DATA / '.bundle.lock', 'a')
    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    os.chmod(DATA / '.bundle.lock', 0o600)
    for directory, user in [(PG, 'postgres'), (DATA/'redis', 'redis'), (DATA/'unitares', 'unitares')]:
        directory.mkdir(exist_ok=True)
        account = pwd.getpwnam(user)
        os.chown(directory, account.pw_uid, account.pw_gid)
        os.chmod(directory, 0o700)
    secret_file = DATA / 'secrets.json'
    if not secret_file.exists():
        if (PG/'PG_VERSION').exists():
            raise RuntimeError('existing database has no identity secrets; restore matching secrets')
        values = {key: secrets.token_hex(32) for key in ('database', 'http', 'lease', 'continuity')}
        values['signing'] = base64.urlsafe_b64encode(secrets.token_bytes(32)).decode().rstrip('=')
        fd = os.open(secret_file, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, 'w') as stream:
            json.dump(values, stream)
    values = json.loads(secret_file.read_text())
    os.chmod(secret_file, 0o600)
    url = f"postgresql://postgres:{values['database']}@127.0.0.1:5432/governance"
    # Always own the internal addresses. Never inherit a production URL or proxy.
    for key in list(os.environ):
        if key.startswith('UNITARES_STDIO_PROXY') or key == 'UNITARES_PROXY_URL':
            del os.environ[key]
    os.environ.update(
        PATH='/opt/unitares/bin:/opt/elixir/bin:/usr/lib/postgresql/18/bin:'+os.environ['PATH'],
        HOME='/data/unitares', MIX_ENV='prod', ERL_FLAGS='+S 2:2',
        DB_BACKEND='postgres', DB_POSTGRES_URL=url, DB_AGE_GRAPH='governance_graph',
        UNITARES_KNOWLEDGE_BACKEND=os.getenv('UNITARES_KNOWLEDGE_BACKEND', 'postgres'),
        UNITARES_DIALECTIC_BACKEND='postgres', REDIS_URL='redis://127.0.0.1:6379/0',
        UNITARES_LEASE_PLANE_DATABASE_URL=url,
        LEASE_PLANE_BASE_URL='http://127.0.0.1:8788', UNITARES_LEASE_PLANE_URL='http://127.0.0.1:8788',
        LEASE_PLANE_BEARER_TOKEN=values['lease'], UNITARES_HTTP_API_TOKEN=values['http'],
        UNITARES_CONTINUITY_TOKEN_SECRET=values['continuity'],
        UNITARES_LEASE_ATTESTATION_SIGNING_KEY=values['signing'],
        UNITARES_LEASE_ATTESTATION_ISSUER='unitares-bundle',
        UNITARES_LEASE_ATTESTATION_AUDIENCE='unitares-bundle',
        UNITARES_LEASE_TRUSTED_ISSUERS=json.dumps({'unitares-bundle':'http://127.0.0.1:8767/v1/lease-holder/keys'}),
        UNITARES_LEASE_TRUST_INSECURE_HTTP_URLS='http://127.0.0.1:8767/v1/lease-holder/keys',
        UNITARES_LEASE_IDENTITY_BINDING='enforce', UNITARES_LEASE_IDENTITY_PROOF_FORMAT='attestation',
        UNITARES_LEASE_IDENTITY_BOUND_SURFACE_KINDS='maintenance',
        UNITARES_LEASE_PLANE_HTTP_IP='127.0.0.1',
        UNITARES_BIND_ALL_INTERFACES='0', UNITARES_RESIDENTS='',
        POSTGRES_USER='postgres', POSTGRES_DB='governance',
        PGHOST='127.0.0.1', PGUSER='postgres', PGDATABASE='governance', PGPASSWORD=values['database'],
    )
    return lock, values


def main():
    global VOLUME_LOCK
    VOLUME_LOCK, values = configure()
    expected = Path('/opt/unitares-schema.sha256').read_text()
    fresh = not (PG/'PG_VERSION').exists()
    if not fresh and (not MARKER.exists() or MARKER.read_text() != expected):
        raise RuntimeError('schema fingerprint mismatch or incomplete bootstrap; restore previous image/backup; explicit migration required')
    if not fresh and (PG/'PG_VERSION').read_text().strip() != '18':
        raise RuntimeError('PostgreSQL major mismatch; explicit pg_upgrade/restore required')
    if fresh:
        if any(PG.iterdir()):
            raise RuntimeError('nonempty uninitialized PostgreSQL directory; refusing overwrite')
        password_file = DATA/'postgres-password'
        password_file.write_text(values['database'])
        os.chmod(password_file, 0o600)
        os.chown(password_file, pwd.getpwnam('postgres').pw_uid, pwd.getpwnam('postgres').pw_gid)
        child = start('initdb', ['initdb', '-D', str(PG), '--auth-host=scram-sha-256', '--auth-local=peer', '--pwfile='+str(password_file)], 'postgres')
        if child.wait() != 0:
            raise RuntimeError('initdb failed')
        CHILDREN.remove(('initdb', child))
        password_file.unlink()
    timeout = int(os.getenv('UNITARES_BUNDLE_STARTUP_TIMEOUT', '120'))
    start('postgres', ['postgres', '-D', str(PG), '-c', 'listen_addresses=127.0.0.1', '-c', 'unix_socket_directories=/var/run/postgresql', '-c', 'shared_buffers=64MB', '-c', 'max_connections=60'], 'postgres')
    wait_ready('postgres', lambda: command_ok(['pg_isready','-h','127.0.0.1','-U','postgres']), timeout)
    if fresh:
        for argv in [ ['createdb','governance'], ['psql','-v','ON_ERROR_STOP=1','-f','/app/db/postgres/init-extensions.sql'], ['bash','/app/db/postgres/docker-initdb.sh'] ]:
            subprocess.run(argv, check=True, stdout=sys.stderr, stderr=sys.stderr, timeout=timeout)
        MARKER.write_text(expected)
    wait_ready('database schema/authentication', lambda: command_ok([
        'psql', '-v', 'ON_ERROR_STOP=1', '-Atc',
        'SELECT 1 FROM core.agents LIMIT 0; SELECT 1 FROM knowledge.discoveries LIMIT 0; SELECT 1 FROM core.schema_migrations LIMIT 0'
    ]), timeout)
    start('redis', ['redis-server','--bind','127.0.0.1','--dir','/data/redis','--appendonly','yes','--appendfsync','always','--save','', '--logfile',''], 'redis')
    wait_ready('redis', lambda: command_ok(['redis-cli','ping']), timeout)
    start('http', ['python','src/mcp_server.py','--host','127.0.0.1','--port','8767','--force'], 'unitares')
    wait_ready('http', lambda: http_ready(8767,'/v1/tools',values['http']), timeout)
    expression = 'Application.put_env(:lease_plane, :database_url, System.fetch_env!("UNITARES_LEASE_PLANE_DATABASE_URL")); {:ok, _} = Application.ensure_all_started(:lease_plane)'
    beam_paths = sorted(Path('/app/elixir/lease_plane/_build/prod/lib').glob('*/ebin'))
    lease_command = ['elixir', '--no-halt']
    for path in beam_paths:
        lease_command.extend(['-pa', str(path)])
    start('lease-plane', lease_command + ['-e', expression], 'unitares')
    wait_ready('lease-plane', lambda: http_ready(8788,'/v1/health',values['lease']), timeout)
    os.environ.update(UNITARES_STDIO_PROXY_HTTP_URL='http://127.0.0.1:8767', UNITARES_STDIO_PROXY_HTTP_BEARER_TOKEN=values['http'])
    mcp = start('mcp', ['python','src/mcp_server_std.py'], 'unitares', mcp=True)
    log('ready: MCP stdio; PostgreSQL, Redis and lease plane are private')
    while not STOP:
        if mcp.poll() == 0:
            return 0
        check_children()
        time.sleep(.25)
    return 0


def shutdown():
    for name, child in reversed(CHILDREN):
        if child.poll() is None:
            # PostgreSQL's fast shutdown commits/checkpoints without waiting for clients.
            try:
                os.killpg(child.pid, signal.SIGINT if name == 'postgres' else signal.SIGTERM)
            except ProcessLookupError:
                continue
            try:
                child.wait(timeout=10)
            except subprocess.TimeoutExpired:
                os.killpg(child.pid, signal.SIGKILL)
                child.wait()


if __name__ == '__main__':
    signal.signal(signal.SIGTERM, stopping)
    signal.signal(signal.SIGINT, stopping)
    result = 1
    try:
        result = main()
    except InterruptedError:
        result = 0
    except Exception as exc:
        # Avoid exception text containing DSNs, environment values or secrets.
        log(f'FAIL: {type(exc).__name__}' + (f': {exc}' if isinstance(exc, RuntimeError) else '; inspect service stderr'))
    finally:
        shutdown()
        if VOLUME_LOCK is not None:
            VOLUME_LOCK.close()
    sys.exit(result)
