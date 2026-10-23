#!/usr/bin/env python3
"""Control the existing, configured macOS Homebrew install; never install or migrate."""
from __future__ import annotations

import argparse
import contextlib
import fcntl
import json
import os
from pathlib import Path
import re
import shlex
import shutil
import socket
import stat
import subprocess
import sys
import time
import urllib.request

SERVICES = ('postgresql@17', 'redis', 'unitares-lease-plane', 'unitares')
WRITERS = ('unitares', 'unitares-lease-plane')
# Literal shell words only: the formula wrappers source this same file.
WORD = r'''(?:[A-Za-z0-9_./:@%?=+,\-]+|'[^']*'|"[^"$`\\]*")*'''
ASSIGNMENT = re.compile(rf'^(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)=({WORD})(?:\s+#.*)?$')


class Refusal(RuntimeError):
    pass


def run(args, *, env=None, timeout=30):
    try:
        return subprocess.run(args, env=env, text=True, capture_output=True, timeout=timeout)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise Refusal(f'Could not complete {Path(args[0]).name}; check the installation.') from exc


def read_config(path: Path) -> dict[str, str]:
    try:
        info = path.lstat()
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
            raise Refusal(f'{path} must be a regular file owned by you with mode 600.')
        lines = path.read_text().splitlines()
    except OSError as exc:
        raise Refusal(f'Configure {path} before starting; see docs/install/CHOOSING_INSTALLATION.md.') from exc
    values = {}
    for number, line in enumerate(lines, 1):
        line = line.strip()
        if not line or line.startswith('#'):
            continue
        match = ASSIGNMENT.fullmatch(line)
        if not match:
            raise Refusal(f'{path}:{number}: use single-line literal KEY=value assignments, without shell expansion.')
        key, value = match.groups()
        values[key] = ''.join(shlex.split(value))
    return values


def local_url(value: str, scheme: tuple[str, ...], port: int | None = None):
    from urllib.parse import urlsplit
    try:
        url = urlsplit(value)
        if url.scheme not in scheme or url.hostname not in ('localhost', '127.0.0.1'):
            raise ValueError()
        actual_port = url.port or {'postgresql': 5432, 'postgres': 5432, 'redis': 6379, 'http': 80}[url.scheme]
        if port is not None and actual_port != port:
            raise ValueError()
        return url, actual_port
    except ValueError as exc:
        raise Refusal('Homebrew service control requires local, explicitly configured service endpoints.') from exc


class Homebrew:
    def __init__(self):
        if sys.platform != 'darwin' or os.geteuid() == 0:
            raise Refusal('Homebrew service control supports a regular macOS user, without sudo.')
        self.brew = shutil.which('brew')
        if not self.brew:
            self.brew = next((p for p in ('/opt/homebrew/bin/brew', '/usr/local/bin/brew') if os.access(p, os.X_OK)), None)
        if not self.brew:
            raise Refusal('Homebrew is not installed or available on PATH.')
        self.env = dict(os.environ, HOMEBREW_NO_AUTO_UPDATE='1', HOMEBREW_NO_ANALYTICS='1')
        prefix = self.call('--prefix').strip()
        if not prefix.startswith('/') or '\n' in prefix:
            raise Refusal('Homebrew returned an invalid prefix.')
        self.prefix = Path(prefix)
        self.app = self.prefix / 'opt/unitares/libexec/app'
        self.config = self.prefix / 'etc/unitares/native.env'

    def call(self, *args):
        result = run([self.brew, *args], env=self.env)
        if result.returncode:
            # Homebrew output may include service environment values. Do not echo it.
            raise Refusal(f'Homebrew {" ".join(args)} failed; inspect it with brew services info.')
        return result.stdout

    def info(self):
        try:
            records = json.loads(self.call('services', 'info', *SERVICES, '--json'))
            result = {record['name']: record for record in records}
            if set(result) != set(SERVICES):
                raise ValueError()
            return result
        except (ValueError, TypeError, KeyError) as exc:
            raise Refusal('Homebrew did not return a complete service inventory.') from exc

    def configure(self):
        override = os.environ.get('UNITARES_NATIVE_ENV')
        if override and Path(override).absolute() != self.config:
            raise Refusal('brew services uses its configured environment, not this shell’s UNITARES_NATIVE_ENV override. Use the canonical native.env.')
        config_home = Path(os.environ.get('HOMEBREW_USER_CONFIG_HOME', str(Path.home() / '.homebrew')))
        for name in SERVICES:
            if (config_home / 'services' / f'{name}.env').exists():
                raise Refusal(f'Custom Homebrew environment for {name}: use that service’s own procedure.')
        for name, binary in (('unitares', 'unitares-server'), ('unitares-lease-plane', 'unitares-lease-plane'), ('postgresql@17', 'psql'), ('redis', 'redis-server')):
            if not os.access(self.prefix / 'opt' / name / 'bin' / binary, os.X_OK):
                raise Refusal(f'Install the {name} formula before starting.')
        config = read_config(self.config)
        required = ('DB_POSTGRES_URL', 'UNITARES_LEASE_PLANE_DATABASE_URL', 'LEASE_PLANE_BEARER_TOKEN', 'REDIS_URL')
        if any(not config.get(key) for key in required):
            raise Refusal('native.env is incomplete; configure both database URLs, Redis and the lease bearer.')
        if config['DB_POSTGRES_URL'] != config['UNITARES_LEASE_PLANE_DATABASE_URL']:
            raise Refusal('The server and lease plane must use the same database URL.')
        db, _ = local_url(config['DB_POSTGRES_URL'], ('postgresql', 'postgres'), 5432)
        if not db.username or not db.password or not db.path.strip('/') or db.query or db.fragment:
            raise Refusal('Use an explicit local database URL with a URL-encoded user, password and database, without query options.')
        if any(marker in config['LEASE_PLANE_BEARER_TOKEN'].lower() for marker in ('replace-before', 'change-me', 'changeme')):
            raise Refusal('Replace the template lease bearer before starting.')
        local_url(config['REDIS_URL'], ('redis',), 6379)
        local_url(config.get('UNITARES_GOVERNANCE_URL', 'http://127.0.0.1:8767'), ('http',), 8767)
        try:
            lease_port = int(config.get('UNITARES_LEASE_PLANE_HTTP_PORT', '8788'))
            if not 1 <= lease_port <= 65535 or lease_port in (8767, 5432, 6379):
                raise ValueError()
        except ValueError as exc:
            raise Refusal('Configure a valid, distinct lease-plane port.') from exc
        if config.get('UNITARES_LEASE_PLANE_HTTP_IP', '127.0.0.1') != '127.0.0.1':
            raise Refusal('This Homebrew service path requires loopback-only listeners.')
        for key in ('UNITARES_LEASE_PLANE_URL', 'LEASE_PLANE_BASE_URL'):
            local_url(config.get(key, 'http://127.0.0.1:8788'), ('http',), lease_port)
        tokens = [token.strip() for token in config.get('UNITARES_MCP_BEARER_TOKENS', '').split(',') if token.strip()]
        # Use the service's configuration, never a shell-only client credential.
        self.health_token = tokens[0] if tokens else config.get('UNITARES_HTTP_API_TOKEN', '')
        self.runtime_env = dict(self.env, **config)
        self.runtime_env['PATH'] = f'{self.prefix}/opt/postgresql@17/bin:{self.env.get("PATH", "")}'
        self.ports = dict(zip(SERVICES, (5432, 6379, lease_port, 8767)))

    @contextlib.contextmanager
    def lock(self):
        directory = self.prefix / 'var/unitares'
        directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        fd = os.open(directory / 'service-control.lock', os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
        try:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as exc:
                raise Refusal('Another native service-control command is running.') from exc
            yield
        finally:
            os.close(fd)

    def check_ports(self, records):
        for name, port in self.ports.items():
            try:
                with socket.create_connection(('127.0.0.1', port), timeout=0.3):
                    pass
            except OSError:
                continue
            owners = run(['lsof', '-nP', '-t', f'-iTCP:{port}', '-sTCP:LISTEN']).stdout.split()
            pid = records[name].get('pid')
            if not records[name].get('running') or not pid or not owners or set(owners) != {str(pid)}:
                raise Refusal(f'Port {port} is occupied outside the selected Homebrew {name} service; reuse that installation or choose separate ports.')

    def wait_port(self, name, timeout):
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            records = self.info()
            self.check_ports(records)
            try:
                with socket.create_connection(('127.0.0.1', self.ports[name]), timeout=0.3):
                    if records[name].get('running'):
                        return
            except OSError:
                pass
            time.sleep(1)
        raise Refusal(f'{name} did not become ready; inspect its Homebrew logs.')

    def check_schema(self):
        python = self.prefix / 'opt/unitares/libexec/venv/bin/python'
        script = self.app / 'scripts/dev/apply_migrations.py'
        if not python.is_file() or not script.is_file():
            raise Refusal('The installed package lacks the native schema check.')
        # The checker forwards its URL to psql. Keep the password in libpq's
        # environment so neither Python nor nested psql argv contains it.
        from urllib.parse import unquote, urlsplit, urlunsplit
        db = urlsplit(self.runtime_env['DB_POSTGRES_URL'])
        public_url = urlunsplit(db._replace(netloc=f'{db.username}@{db.hostname}:{db.port or 5432}'))
        env = dict(self.runtime_env, PGPASSWORD=unquote(db.password))
        result = run([str(python), str(script), '--check', '--db-url', public_url], env=env, timeout=60)
        if result.returncode:
            raise Refusal('The installed schema check failed. Bootstrap a fresh database or use the documented backed-up native upgrade procedure before starting writers.')

    def start(self, check=False, timeout=60):
        self.configure()
        records = self.info()
        for name in WRITERS:
            command = records[name].get('command', '')
            binary = 'unitares-server' if name == 'unitares' else name
            if shlex.split(command) != [str(self.prefix / 'opt' / name / 'bin' / binary)]:
                raise Refusal(f'{name} has a custom service command; use its own procedure.')
        self.check_ports(records)
        print(f'Installation: Homebrew\nConfiguration: {self.config}\nEndpoint: http://127.0.0.1:8767')
        if check:
            print('Plan: start missing services in order: PostgreSQL, Redis, lease plane, server. Check the schema before starting writers; no migrations run.')
            return
        started = []
        try:
            for name in SERVICES:
                if name == 'unitares-lease-plane':
                    self.check_schema()
                if not records[name].get('running'):
                    # Include a partially successful start in failure cleanup.
                    started.append(name)
                    self.call('services', 'start', name)
                self.wait_port(name, timeout)
            end = time.monotonic() + timeout
            opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
            headers = {'Authorization': 'Bearer ' + self.health_token} if self.health_token else {}
            request = urllib.request.Request('http://127.0.0.1:8767/health/deep', headers=headers)
            while time.monotonic() < end:
                try:
                    with opener.open(request, timeout=2) as response:
                        payload = json.load(response)
                        status = payload.get('status') if isinstance(payload, dict) else None
                    if status in ('healthy', 'moderate'):
                        print(f'Deep health: {status}')
                        return
                except (OSError, ValueError):
                    pass
                time.sleep(1)
            raise Refusal('Server deep health did not become healthy/moderate; inspect unitares logs.')
        except (Refusal, KeyboardInterrupt):
            for name in reversed(started):
                if name in WRITERS:
                    try:
                        self.call('services', 'stop', name)
                    except Refusal:
                        print(f'warning: could not stop newly started {name}; inspect brew services info.', file=sys.stderr)
            print('Any dependencies started by this command remain available; pre-existing writers were not stopped.', file=sys.stderr)
            raise

    def stop(self, check=False):
        records = self.info()
        if check:
            print('Plan: stop the Homebrew server, then lease plane. Leave PostgreSQL and Redis running.')
            return
        errors = []
        for name in WRITERS:
            if records[name].get('running') or records[name].get('loaded') or records[name].get('registered'):
                try:
                    self.call('services', 'stop', name)
                except Refusal as exc:
                    errors.append(str(exc))
        if errors:
            raise Refusal('; '.join(errors))
        after = self.info()
        if any(after[name].get('running') or after[name].get('loaded') for name in WRITERS):
            raise Refusal('A Homebrew writer remains running; inspect brew services info.')
        print('Stopped Homebrew server and lease plane. PostgreSQL/Redis were left unchanged.')

    def logs(self, service, follow=True, lines=100):
        name = {'server': 'unitares', 'lease-plane': 'unitares-lease-plane'}[service]
        path = self.prefix / 'var/log' / f'{name}.log'
        if not path.is_file():
            raise Refusal(f'No log file yet: {path}')
        args = ['tail', '-n', str(lines)] + (['-F'] if follow else []) + [str(path)]
        os.execvp('tail', args)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    actions = parser.add_subparsers(dest='action', required=True)
    start = actions.add_parser('start')
    start.add_argument('--check', action='store_true', help='report the startup plan without changing services')
    start.add_argument('--timeout', type=int, default=60, help='readiness timeout per service, in seconds')
    stop = actions.add_parser('stop')
    stop.add_argument('--check', action='store_true', help='report the stop plan without changing services')
    logs = actions.add_parser('logs')
    logs.add_argument('service', nargs='?', choices=('server', 'lease-plane'), default='server')
    logs.add_argument('--no-follow', action='store_true')
    logs.add_argument('--tail', type=int, default=100)
    args = parser.parse_args(argv)
    if not 1 <= getattr(args, 'timeout', 60) <= 300 or not 1 <= getattr(args, 'tail', 100) <= 10000:
        parser.error('timeout must be 1–300 seconds; tail must be 1–10000 lines')
    try:
        brew = Homebrew()
        if args.action == 'logs':
            brew.logs(args.service, not args.no_follow, args.tail)
        elif args.check:
            getattr(brew, args.action)(check=True)
        else:
            with brew.lock():
                if args.action == 'start':
                    brew.start(timeout=args.timeout)
                else:
                    brew.stop()
        return 0
    except (Refusal, OSError) as exc:
        print(f'error: {exc}', file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        return 130


if __name__ == '__main__':
    raise SystemExit(main())
