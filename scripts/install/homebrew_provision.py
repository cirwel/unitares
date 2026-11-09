"""Explicit first-run provisioning; never adopt, reset or drop an existing DB.

The private journal records intent before each mutation. Role creation and its
marker are transactional; CREATE DATABASE is not. An unmarked database after a
crash is therefore ambiguous and requires inspection, never automatic adoption.
Bootstrap similarly marks itself running before DDL and refuses unsafe replay.
"""
from __future__ import annotations

import base64
import getpass
import hashlib
import hmac
import json
import os
from pathlib import Path
import re
import secrets
import shlex
import shutil
import stat
import subprocess
import tempfile
import uuid

try:
    from .homebrew_services import Refusal, SERVICES, WRITERS, read_config, run
except ImportError:  # installed script entrypoint
    from homebrew_services import Refusal, SERVICES, WRITERS, read_config, run


STAGES = {'planned', 'role_pending', 'role_created', 'database_pending',
          'database_created', 'bootstrap_pending', 'bootstrapped', 'complete'}
JOURNAL = 'native-setup.json'
REDIS_INSTALL_KEY = 'unitares:installation_id'


def private_directory(path: Path) -> None:
    info = path.lstat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o022:
        raise Refusal('Native setup requires an owned directory without group/world write access.')


def read_journal(path: Path) -> dict:
    private_directory(path.parent)
    info = path.lstat()
    if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
        raise Refusal('Native setup journal must be an owned private regular file.')
    try:
        state = json.loads(path.read_text())
        if state['version'] != 1 or state['stage'] not in STAGES:
            raise ValueError()
        if str(uuid.UUID(state['install_id'])) != state['install_id']:
            raise ValueError()
        for key in ('prefix', 'cluster', 'admin', 'source_hash', 'config', 'postgres_version', 'extensions'):
            if not state[key]:
                raise ValueError()
        config = state['config']
        if config['UNITARES_INSTALL_ID'] != state['install_id']:
            raise ValueError()
        if not re.fullmatch(r'postgresql://unitares:[A-Za-z0-9_-]+@127\.0\.0\.1:5432/unitares', config['DB_POSTGRES_URL']):
            raise ValueError()
        if config['DB_POSTGRES_URL'] != config['UNITARES_LEASE_PLANE_DATABASE_URL']:
            raise ValueError()
        return state
    except (ValueError, TypeError, KeyError, AttributeError) as exc:
        raise Refusal('Invalid setup journal; preserve it and inspect before continuing.') from exc


def check_setup_barrier(prefix: Path) -> None:
    path = prefix / 'var/unitares' / JOURNAL
    if os.path.lexists(path) and read_journal(path)['stage'] != 'complete':
        raise Refusal('Native database setup is unfinished. Keep writers stopped; run '
                      '`unitares setup --homebrew --provision-db --apply --resume`.')


def literal(value: str) -> str:
    # All SQL is sent on stdin, never through a shell or credential-bearing argv.
    return "'" + value.replace("'", "''") + "'"


def scram_verifier(password: str) -> str:
    """Encode our generated ASCII secret without sending it in SQL statements.

    PostgreSQL accepts this verifier as-is (pg_authid format; RFC 5802/7677).
    Restrict input to the journal's URL-safe alphabet, for which SASLprep is
    the identity transformation. This is not a general password encoder.
    """
    if not re.fullmatch(r'[A-Za-z0-9_-]+', password):
        raise Refusal('Provisioning requires a generated URL-safe password.')
    salt = secrets.token_bytes(16)
    iterations = 4096
    salted = hashlib.pbkdf2_hmac('sha256', password.encode('ascii'), salt, iterations)
    client_key = hmac.digest(salted, b'Client Key', 'sha256')
    stored_key = hashlib.sha256(client_key).digest()
    server_key = hmac.digest(salted, b'Server Key', 'sha256')
    encoded = [base64.b64encode(value).decode('ascii') for value in (salt, stored_key, server_key)]
    return f'SCRAM-SHA-256${iterations}:{encoded[0]}${encoded[1]}:{encoded[2]}'


class NativeProvisioner:
    def __init__(self, brew):
        self.brew = brew
        self.directory = brew.prefix / 'var/unitares'
        self.path = self.directory / JOURNAL
        self.psql = str(brew.prefix / 'opt/postgresql@17/bin/psql')
        self.admin = getpass.getuser()
        self.env = {k: v for k, v in brew.env.items() if not k.startswith('PG')}
        self.env.update(PGCONNECT_TIMEOUT='5', PGPASSFILE='/dev/null')
        self.state = None

    def save(self, stage: str) -> None:
        self.state['stage'] = stage
        self.directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        private_directory(self.directory)
        fd, temporary = tempfile.mkstemp(prefix='.setup-', dir=self.directory)
        try:
            with os.fdopen(fd, 'w') as output:
                json.dump(self.state, output)
                output.flush()
                os.fsync(output.fileno())
            if os.path.lexists(self.path):
                read_journal(self.path)
                os.replace(temporary, self.path)
            else:
                os.link(temporary, self.path)
            # The lifecycle lock may have just created var/unitares. Persist
            # its parent links too before treating the journal as durable intent.
            for directory in (self.directory, self.directory.parent, self.brew.prefix):
                directory_fd = os.open(directory, os.O_RDONLY)
                try:
                    os.fsync(directory_fd)
                finally:
                    os.close(directory_fd)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)

    def sql(self, sql: str, *, database='postgres', application=False) -> str:
        env = dict(self.env)
        user = self.admin
        if application:
            from urllib.parse import urlsplit
            user = 'unitares'
            env['PGPASSWORD'] = urlsplit(self.state['config']['DB_POSTGRES_URL']).password
        elif self.state:
            # Recheck cluster identity on the very connection executing SQL,
            # not just the earlier listener/inspection connection.
            sql = ("DO $$ BEGIN IF (SELECT system_identifier::text FROM pg_control_system()) <> "
                   + literal(self.state['cluster'])
                   + " THEN RAISE EXCEPTION 'Setup cluster changed'; END IF; END $$;\n" + sql)
        if self.state and database == 'unitares':
            phase = 'bootstrapped' if application else 'prepared'
            sql = ("DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_database "
                   "WHERE datname=current_database() AND pg_get_userbyid(datdba)='unitares' "
                   "AND shobj_description(oid,'pg_database')=" + literal(self.marker + ':' + phase)
                   + ") THEN RAISE EXCEPTION 'Setup database ownership changed'; END IF; END $$;\n" + sql)
        try:
            result = subprocess.run(
                [self.psql, '-X', '-w', '-h', '127.0.0.1', '-p', '5432', '-U', user,
                 '-d', database, '-v', 'ON_ERROR_STOP=1', '-Atq'],
                input=sql, env=env, text=True, capture_output=True, timeout=60,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise Refusal('Database operation could not finish; preserve the setup journal and resume for inspection.') from exc
        if result.returncode:
            raise Refusal('Database operation failed; no cleanup or credential rotation was attempted. '
                          'Preserve the journal and inspect the database before resuming.')
        return result.stdout.strip()

    def snapshot(self) -> dict:
        try:
            result = json.loads(self.sql("""
SELECT json_build_object(
 'cluster', (SELECT system_identifier::text FROM pg_control_system()),
 'directory', current_setting('data_directory'),
 'version', current_setting('server_version_num')::int,
 'superuser', (SELECT rolsuper FROM pg_roles WHERE rolname=current_user),
 'role', (SELECT json_build_object('marker', shobj_description(oid,'pg_authid'),
   'safe', NOT rolsuper AND NOT rolcreatedb AND NOT rolcreaterole AND NOT rolreplication
           AND NOT rolbypassrls AND rolcanlogin AND rolconfig IS NULL
           AND NOT EXISTS (SELECT 1 FROM pg_auth_members WHERE member=pg_roles.oid))
   FROM pg_roles WHERE rolname='unitares'),
 'database', (SELECT json_build_object('marker', shobj_description(oid,'pg_database'),
    'owner', pg_get_userbyid(datdba)) FROM pg_database WHERE datname='unitares'),
 'extensions', (SELECT json_object_agg(name,default_version) FROM pg_available_extensions
                WHERE name IN ('age','vector')));
"""))
            if (not result['superuser'] or not 170000 <= result['version'] < 180000
                    or Path(result['directory']).resolve() != (self.brew.prefix / 'var/postgresql@17').resolve()):
                raise ValueError()
            if set(result['extensions'] or {}) != {'age', 'vector'}:
                raise Refusal('Install the tap’s AGE and pgvector packages for PostgreSQL 17 before provisioning.')
            return result
        except (ValueError, TypeError, KeyError) as exc:
            raise Refusal('The connected database is not this Homebrew PostgreSQL 17 cluster with a local administrator.') from exc

    def preflight(self):
        self.brew.check_installation()
        self.brew.check_update_barrier()
        records = self.brew.info()
        self.brew.check_commands(records)
        postgres_command = shlex.split(records['postgresql@17'].get('command', ''))
        if postgres_command and postgres_command[0] == 'LC_ALL=en_US.UTF-8':
            postgres_command.pop(0)
        if postgres_command != [str(self.brew.prefix / 'opt/postgresql@17/bin/postgres'),
                                '-D', str(self.brew.prefix / 'var/postgresql@17')]:
            raise Refusal('PostgreSQL has a custom service command; use its own provisioning procedure.')
        if shlex.split(records['redis'].get('command', '')) != [
                str(self.brew.prefix / 'opt/redis/bin/redis-server'), str(self.brew.prefix / 'etc/redis.conf')]:
            raise Refusal('Redis has a custom service command; use its own provisioning procedure.')
        self.brew.ports = dict(zip(SERVICES, (5432, 6379, 8788, 8767)))
        self.brew.check_ports(records)
        if any(records[name].get('running') or records[name].get('loaded') for name in WRITERS):
            raise Refusal('Stop both native writers before first-run database provisioning.')
        if records['postgresql@17'].get('running'):
            pid = records['postgresql@17'].get('pid')
            executable = run(['ps', '-p', str(pid), '-o', 'comm=']).stdout.strip()
            if Path(executable).resolve() != (self.brew.prefix / 'opt/postgresql@17/bin/postgres').resolve():
                raise Refusal('The PostgreSQL service process does not match this Homebrew prefix.')
        # Container names are supplementary evidence, not proof of port ownership.
        if shutil.which('docker', path=self.env.get('PATH')):
            result = run(['docker', 'ps', '--format', '{{.Names}}'], env=self.env, timeout=15)
            if result.returncode:
                raise Refusal('Cannot inspect Docker containers. Resolve Docker connectivity before provisioning.')
            if {'postgres-age', 'unitares-postgres-age'} & set(result.stdout.splitlines()):
                raise Refusal('Docker postgres-age is active; reuse that installation instead of provisioning another.')
        if records['redis'].get('running'):
            size = self.redis('DBSIZE')
            owned = (self.state is not None and size == '1'
                     and self.redis('GET', REDIS_INSTALL_KEY) == self.state['install_id'])
            if size != '0' and not owned:
                raise Refusal('Redis database 0 is nonempty or unreadable; do not pair existing session state with a fresh database.')
        return records

    def redis(self, *args):
        result = run([str(self.brew.prefix / 'opt/redis/bin/redis-cli'), '-h', '127.0.0.1',
                      '-p', '6379', '-n', '0', '--raw', *args], env=self.env)
        if result.returncode:
            raise Refusal('Cannot inspect the selected Redis database; no existing keys were changed.')
        return result.stdout.strip()

    def bind_redis(self):
        # Check emptiness and claim in one Redis operation, including on resume.
        # Never clear existing keys, and never attach a fresh DB to old identities.
        result = self.redis('EVAL', """
local size = redis.call('DBSIZE')
if size == 0 then redis.call('SET', KEYS[1], ARGV[1]); return 1 end
if size == 1 and redis.call('GET', KEYS[1]) == ARGV[1] then return 1 end
return 0
""", '1', REDIS_INSTALL_KEY, self.state['install_id'])
        if result != '1':
            raise Refusal('Redis changed or belongs to another installation; no existing keys were changed.')

    def source_hash(self) -> str:
        root = self.brew.app
        files = sorted((root / 'db/postgres').rglob('*.sql')) + [
            root / 'scripts/install/bootstrap_postgres.sh', root / 'scripts/dev/apply_migrations.py',
            root / 'scripts/install/homebrew_provision.py', root / 'scripts/install/homebrew_services.py']
        digest = hashlib.sha256()
        for path in files:
            digest.update(str(path.relative_to(root)).encode())
            digest.update(path.read_bytes())
        return digest.hexdigest()

    @property
    def marker(self):
        return 'unitares-install:' + self.state['install_id']

    def verify_owned(self, snapshot):
        if snapshot['cluster'] != self.state['cluster']:
            raise Refusal('The PostgreSQL cluster changed since setup began; refusing to resume.')
        if (snapshot['version'] != self.state['postgres_version']
                or snapshot['extensions'] != self.state['extensions']):
            raise Refusal('PostgreSQL or extension versions changed during setup; inspect before resuming.')
        role = snapshot['role']
        if role and (role['marker'] != self.marker + ':role' or not role['safe']):
            raise Refusal('The unitares role is foreign or its privileges changed; refusing to adopt it.')
        db = snapshot['database']
        if db and (db['owner'] != 'unitares' or db['marker'] not in
                   [self.marker + ':' + phase for phase in ('prepared', 'bootstrapping', 'bootstrapped')]):
            raise Refusal('The unitares database is unmarked or foreign. A CREATE DATABASE interruption requires manual inspection; no database was adopted or dropped.')

    def setup(self, *, apply=False, resume=False):
        existing = os.path.lexists(self.path)
        if existing:
            self.state = read_journal(self.path)
            if self.state['prefix'] != str(self.brew.prefix) or self.state['admin'] != self.admin:
                raise Refusal('Setup journal belongs to a different prefix or administrator.')
            if self.state['stage'] == 'complete':
                if read_config(self.brew.config) != self.state['config']:
                    raise Refusal('Completed setup configuration changed; use the existing installation procedure.')
                print('Database provisioning already completed; existing credentials and data preserved.')
                return
            if apply and not resume:
                raise Refusal('An unfinished setup journal exists; repeat with --resume.')
            if self.state['source_hash'] != self.source_hash():
                raise Refusal('Bootstrap files changed during setup. Inspect the interrupted install before upgrading or resuming.')
        elif resume:
            raise Refusal('No setup journal exists to resume.')
        elif os.path.lexists(self.brew.config) or os.environ.get('DB_POSTGRES_URL'):
            raise Refusal('Provisioning requires no existing native.env or shell database URL; use setup for an existing database.')
        records = self.preflight()
        snapshot = self.snapshot() if records['postgresql@17'].get('running') else None
        if not existing and snapshot and (snapshot['role'] or snapshot['database']):
            raise Refusal('The unitares role or database already exists; refusing to adopt partial or foreign state.')
        if not apply:
            print('Plan: verify/start Homebrew PostgreSQL 17 and Redis; create a dedicated non-superuser role and database; '
                  'bootstrap only this installation; publish private native.env. Writers remain stopped.')
            if snapshot is None:
                print('Database preflight pending: PostgreSQL is stopped. No changes made.')
            return
        # Verify the packaged bootstrap and its interpreter before creating anything.
        source_hash = self.source_hash()
        python = self.brew.prefix / 'opt/unitares/libexec/venv/bin/python'
        if not python.is_file():
            raise Refusal('The installed package lacks its Python interpreter.')
        if snapshot is None:
            self.brew.call('services', 'start', 'postgresql@17')
            self.brew.wait_port('postgresql@17', 60)
            self.preflight()
            snapshot = self.snapshot()
        if not records['redis'].get('running'):
            self.brew.call('services', 'start', 'redis')
            self.brew.wait_port('redis', 60)
            self.preflight()
        if not existing:
            if snapshot['role'] or snapshot['database']:
                raise Refusal('The unitares role or database already exists; no configuration was written.')
            install_id = str(uuid.uuid4())
            config = self.brew.default_configuration(
                'postgresql://unitares:' + secrets.token_urlsafe(32) + '@127.0.0.1:5432/unitares')
            config['UNITARES_INSTALL_ID'] = install_id
            self.state = dict(version=1, install_id=install_id, prefix=str(self.brew.prefix),
                              cluster=snapshot['cluster'], admin=self.admin, source_hash=source_hash,
                              postgres_version=snapshot['version'], extensions=snapshot['extensions'],
                              config=config)
            self.save('planned')
        self.verify_owned(snapshot)
        if os.path.lexists(self.brew.config) and read_config(self.brew.config) != self.state['config']:
            raise Refusal('Configuration appeared or changed during setup; refusing to overwrite it.')
        if existing:
            # A prior filesystem error may have left readable but unsynced
            # intent. Re-establish durability before touching Redis or SQL.
            self.save(self.state['stage'])
        self.bind_redis()
        stage = self.state['stage']
        if stage in ('planned', 'role_pending'):
            if snapshot['database']:
                raise Refusal('Unexpected database before role creation.')
            if not snapshot['role']:
                from urllib.parse import urlsplit
                password = urlsplit(self.state['config']['DB_POSTGRES_URL']).password
                self.save('role_pending')
                self.sql('BEGIN; SET LOCAL standard_conforming_strings=on; '
                         'CREATE ROLE unitares LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS PASSWORD '
                         + literal(scram_verifier(password)) + '; COMMENT ON ROLE unitares IS ' + literal(self.marker + ':role') + '; COMMIT;')
            self.save('role_created')
        elif not snapshot['role']:
            raise Refusal('The journal’s role disappeared; refusing to recreate credentials.')
        if self.state['stage'] in ('role_created', 'database_pending'):
            snapshot = self.snapshot()
            self.verify_owned(snapshot)
            if not snapshot['database']:
                self.save('database_pending')
                self.sql('CREATE DATABASE unitares OWNER unitares TEMPLATE template0;')
                self.sql('COMMENT ON DATABASE unitares IS ' + literal(self.marker + ':prepared') + ';')
            self.save('database_created')
        snapshot = self.snapshot()
        self.verify_owned(snapshot)
        if not snapshot['database']:
            raise Refusal('The journal’s database disappeared; refusing to recreate it.')
        marker = snapshot['database']['marker']
        if marker == self.marker + ':bootstrapping':
            raise Refusal('Bootstrap was interrupted after DDL began. Preserve the database and journal for manual inspection; fresh bootstrap will not replay.')
        if self.state['stage'] == 'database_created':
            # Only the new database and this role/database setting are changed.
            # The application and every object it creates remain nonsuperuser.
            # Schema USAGE alone does not permit runtime catalog reads or the
            # extension configuration-table reads performed by pg_dump.
            self.sql((self.brew.app / 'db/postgres/init-extensions.sql').read_text()
                     + "\nGRANT USAGE ON SCHEMA ag_catalog TO unitares;\n"
                     + "GRANT SELECT ON ag_catalog.ag_graph, ag_catalog.ag_label TO unitares;\n"
                     + "ALTER ROLE unitares IN DATABASE unitares SET session_preload_libraries='age';",
                     database='unitares')
            self.save('bootstrap_pending')
        if self.state['stage'] == 'bootstrap_pending':
            if marker != self.marker + ':bootstrapped':
                self.brew.configure(self.state['config'])
                public_url, env = self.brew.database_environment()
                env['PATH'] = str(python.parent) + os.pathsep + env['PATH']
                env['PGPASSFILE'] = '/dev/null'
                result = run(['bash', str(self.brew.app / 'scripts/install/bootstrap_postgres.sh'),
                              '--apply', '--install-id', self.state['install_id'], '--db-url', public_url],
                             env=env, timeout=600)
                if result.returncode:
                    raise Refusal('Bootstrap failed. Preserve the database and journal; --resume will inspect its durable stage without replaying uncertain DDL.')
            self.save('bootstrapped')
        self.brew.configure(self.state['config'])
        final_snapshot = self.snapshot()
        self.verify_owned(final_snapshot)
        if not final_snapshot['database'] or final_snapshot['database']['marker'] != self.marker + ':bootstrapped':
            raise Refusal('Bootstrap completion marker is missing; refusing to publish configuration.')
        self.brew.check_schema()
        # A real Cypher read under application credentials verifies the role-
        # scoped preload, extension grants, graph ownership and runtime access.
        probe = self.sql("SET search_path=ag_catalog,public; "
                         "SELECT count(*) FROM ag_catalog.ag_graph WHERE name='governance_graph'; "
                         "SELECT * FROM cypher('governance_graph', $$ RETURN 1 $$) AS (value agtype);",
                         database='unitares', application=True)
        if probe != '1\n1':
            raise Refusal('Application-role AGE probe failed; configuration was not published.')
        self.bind_redis()
        if not os.path.lexists(self.brew.config):
            self.brew.write_configuration(self.state['config'])
        elif read_config(self.brew.config) != self.state['config']:
            raise Refusal('Configuration changed during verification; refusing to replace it.')
        else:
            # A previous attempt may have linked native.env and crashed before
            # syncing its directory. Resume must make that link durable too.
            self.brew.sync_configuration()
        self.save('complete')
        print('Provisioned and verified the dedicated unitares database. Private configuration saved; writers remain stopped. '
              'Next: unitares start --homebrew --check, then unitares start --homebrew.')
