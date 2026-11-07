"""First-run safety and interruption tests; no shared service or database writes."""
import copy
import os
from pathlib import Path
from types import SimpleNamespace

import pytest

from scripts.install import homebrew_provision as provision
from scripts.install import homebrew_services as native


ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def installation(tmp_path, monkeypatch):
    brew = native.Homebrew.__new__(native.Homebrew)
    brew.prefix = tmp_path / 'brew'
    brew.app = ROOT
    brew.config = brew.prefix / 'etc/unitares/native.env'
    brew.env = {'PATH': '/usr/bin:/bin', 'PGHOSTADDR': 'foreign', 'PGSERVICE': 'foreign'}
    python = brew.prefix / 'opt/unitares/libexec/venv/bin/python'
    python.parent.mkdir(parents=True)
    python.touch()
    calls = []
    monkeypatch.delenv('DB_POSTGRES_URL', raising=False)
    monkeypatch.setattr(brew, 'check_installation', lambda: None)
    monkeypatch.setattr(brew, 'check_schema', lambda: calls.append('schema-check'))
    records = {name: {'running': name not in native.WRITERS} for name in native.SERVICES}
    db = dict(cluster='1234', version=170011, extensions={'age': '1.7.0', 'vector': '0.8.0'}, role=None, database=None)
    fault = {'sql': None, 'after': False, 'bootstrap': None}

    class FixtureProvisioner(provision.NativeProvisioner):
        def bind_redis(self):
            pass

        def preflight(self):
            return records

        def snapshot(self):
            return copy.deepcopy(db)

        def sql(self, sql, **kwargs):
            kind = ('role' if 'CREATE ROLE' in sql else
                    'database' if 'CREATE DATABASE' in sql else
                    'marker' if 'COMMENT ON DATABASE' in sql else
                    'extensions' if 'CREATE EXTENSION' in sql else 'probe')
            calls.append(kind)
            if fault['sql'] == kind and not fault['after']:
                fault['sql'] = None
                raise native.Refusal('injected before mutation')
            if kind == 'role':
                from urllib.parse import urlsplit
                assert urlsplit(self.state['config']['DB_POSTGRES_URL']).password not in sql
                assert 'PASSWORD \'SCRAM-SHA-256$' in sql
                db['role'] = {'safe': True, 'marker': self.marker + ':role'}
            elif kind == 'database':
                db['database'] = {'owner': 'unitares', 'marker': None}
            elif kind == 'marker':
                db['database']['marker'] = self.marker + ':prepared'
            if fault['sql'] == kind:
                fault['sql'] = None
                raise native.Refusal('injected after mutation')
            return '1\n1' if kind == 'probe' else ''

    def run(args, *, env, timeout):
        assert 'PGHOSTADDR' not in env and 'PGSERVICE' not in env
        assert env['PGPASSWORD'] not in ' '.join(args)
        assert '--install-id' in args
        calls.append('bootstrap')
        if fault['bootstrap'] == 'before':
            fault['bootstrap'] = None
            raise native.Refusal('injected before bootstrap')
        install_id = args[args.index('--install-id') + 1]
        db['database']['marker'] = f'unitares-install:{install_id}:bootstrapping'
        if fault['bootstrap'] == 'during':
            fault['bootstrap'] = None
            return SimpleNamespace(returncode=1)
        db['database']['marker'] = f'unitares-install:{install_id}:bootstrapped'
        if fault['bootstrap'] == 'after':
            fault['bootstrap'] = None
            raise native.Refusal('injected after bootstrap')
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr(provision, 'run', run)
    return SimpleNamespace(brew=brew, new=lambda: FixtureProvisioner(brew), db=db,
                           calls=calls, fault=fault, records=records)


def test_dry_run_is_read_only(installation):
    i = installation
    i.new().setup()
    assert not i.calls
    assert not i.brew.config.exists()
    assert not (i.brew.prefix / 'var/unitares').exists()


@pytest.mark.parametrize('password', ['', 'non ascii é', 'has space', "quote'"])
def test_scram_refuses_passwords_outside_generated_alphabet(password):
    with pytest.raises(native.Refusal, match='URL-safe'):
        provision.scram_verifier(password)


@pytest.mark.parametrize('occupied', ['role', 'database'])
def test_foreign_or_partial_state_is_never_adopted(installation, occupied):
    i = installation
    i.db[occupied] = {'foreign': True}
    with pytest.raises(native.Refusal, match='already exists'):
        i.new().setup(apply=True)
    assert not i.calls
    assert not i.brew.config.exists()


def test_provisioning_publishes_once_and_repeated_apply_preserves_credentials(installation):
    i = installation
    i.new().setup(apply=True)
    config = i.brew.config.read_bytes()
    journal = provision.read_journal(i.new().path)
    assert journal['stage'] == 'complete'
    assert i.brew.config.stat().st_mode & 0o777 == 0o600
    assert i.new().path.stat().st_mode & 0o777 == 0o600
    assert native.read_config(i.brew.config) == journal['config']
    assert 'SUPERUSER' not in config.decode()
    calls = i.calls.copy()
    i.new().setup(apply=True)
    assert i.calls == calls
    assert i.brew.config.read_bytes() == config


@pytest.mark.parametrize('kind,after,safe', [
    ('role', False, True), ('role', True, True),
    ('database', False, True), ('database', True, False),
    ('marker', False, False), ('marker', True, True),
    ('extensions', False, True), ('extensions', True, True),
    ('probe', False, True), ('probe', True, True),
])
def test_resume_at_database_mutation_boundaries(installation, kind, after, safe):
    i = installation
    i.fault.update(sql=kind, after=after)
    with pytest.raises(native.Refusal, match='injected'):
        i.new().setup(apply=True)
    before = provision.read_journal(i.new().path)
    with pytest.raises(native.Refusal, match='unfinished'):
        i.brew.check_setup_barrier()
    with pytest.raises(native.Refusal, match='--resume'):
        i.new().setup(apply=True)
    if safe:
        i.new().setup(apply=True, resume=True)
        assert provision.read_journal(i.new().path)['stage'] == 'complete'
        assert native.read_config(i.brew.config) == before['config']
    else:
        preserved = copy.deepcopy(i.db)
        with pytest.raises(native.Refusal, match='unmarked'):
            i.new().setup(apply=True, resume=True)
        assert i.db == preserved
        assert not i.brew.config.exists()


@pytest.mark.parametrize('when', ['before', 'during', 'after'])
def test_bootstrap_interruption_never_replays_uncertain_ddl(installation, when):
    i = installation
    i.fault['bootstrap'] = when
    with pytest.raises(native.Refusal):
        i.new().setup(apply=True)
    if when == 'during':
        with pytest.raises(native.Refusal, match='interrupted after DDL'):
            i.new().setup(apply=True, resume=True)
    else:
        i.new().setup(apply=True, resume=True)
    assert i.calls.count('bootstrap') == (2 if when == 'before' else 1)


@pytest.mark.parametrize('after', [False, True])
def test_config_publication_interruption_preserves_credentials(installation, monkeypatch, after):
    i = installation
    publish = i.brew.write_configuration
    def interrupted(config):
        if after:
            publish(config)
        raise OSError('injected publication interruption')
    monkeypatch.setattr(i.brew, 'write_configuration', interrupted)
    with pytest.raises(OSError):
        i.new().setup(apply=True)
    config = provision.read_journal(i.new().path)['config']
    monkeypatch.setattr(i.brew, 'write_configuration', publish)
    i.new().setup(apply=True, resume=True)
    assert native.read_config(i.brew.config) == config
    assert i.calls.count('bootstrap') == 1


def test_redis_changed_during_bootstrap_blocks_configuration_publication(installation, monkeypatch):
    i = installation
    calls = []
    def bind(self):
        calls.append('binding')
        if 'bootstrap' in i.calls:
            raise native.Refusal('foreign key arrived during bootstrap')
    monkeypatch.setattr(type(i.new()), 'bind_redis', bind)
    with pytest.raises(native.Refusal, match='foreign key'):
        i.new().setup(apply=True)
    assert len(calls) == 2
    assert not i.brew.config.exists()
    assert provision.read_journal(i.new().path)['stage'] == 'bootstrapped'


def test_directory_sync_failure_keeps_journal_incomplete_and_resume_syncs_again(installation, monkeypatch):
    i = installation
    sync = i.brew.sync_configuration
    calls = []
    def interrupted():
        calls.append('sync')
        raise OSError('directory sync failed')
    monkeypatch.setattr(i.brew, 'sync_configuration', interrupted)
    with pytest.raises(OSError, match='directory sync failed'):
        i.new().setup(apply=True)
    assert i.brew.config.exists()
    assert provision.read_journal(i.new().path)['stage'] == 'bootstrapped'
    def resumed_sync():
        calls.append('sync')
        sync()
    monkeypatch.setattr(i.brew, 'sync_configuration', resumed_sync)
    i.new().setup(apply=True, resume=True)
    assert calls == ['sync', 'sync']
    assert provision.read_journal(i.new().path)['stage'] == 'complete'
    assert i.calls.count('bootstrap') == 1


def test_journal_parent_sync_failure_prevents_database_mutations(installation, monkeypatch):
    i = installation
    sync = os.fsync
    parent = i.new().directory.parent
    def fail_parent(fd):
        if parent.exists() and os.fstat(fd).st_ino == parent.stat().st_ino:
            raise OSError('journal parent sync failed')
        sync(fd)
    monkeypatch.setattr(provision.os, 'fsync', fail_parent)
    with pytest.raises(OSError, match='journal parent sync failed'):
        i.new().setup(apply=True)
    assert not i.calls
    assert not i.db['role'] and not i.db['database']
    monkeypatch.setattr(provision.os, 'fsync', sync)
    i.new().setup(apply=True, resume=True)
    assert provision.read_journal(i.new().path)['stage'] == 'complete'


@pytest.mark.parametrize('change', ['cluster', 'privileges', 'config', 'files', 'extensions'])
def test_resume_refuses_changed_evidence(installation, change, monkeypatch):
    i = installation
    i.fault['sql'] = 'extensions'
    with pytest.raises(native.Refusal):
        i.new().setup(apply=True)
    if change == 'cluster':
        i.db['cluster'] = 'different'
    elif change == 'privileges':
        i.db['role']['safe'] = False
    elif change == 'config':
        i.brew.config.parent.mkdir(parents=True)
        i.brew.config.write_text('DB_POSTGRES_URL=foreign\n')
        i.brew.config.chmod(0o600)
    elif change == 'extensions':
        i.db['extensions']['age'] = 'different'
    else:
        monkeypatch.setattr(provision.NativeProvisioner, 'source_hash', lambda self: 'changed')
    calls = i.calls.copy()
    with pytest.raises(native.Refusal):
        i.new().setup(apply=True, resume=True)
    assert i.calls == calls


@pytest.mark.parametrize('kind', ['symlink', 'mode', 'corrupt'])
def test_untrusted_journal_blocks_writers(installation, kind):
    i = installation
    i.fault['sql'] = 'role'
    with pytest.raises(native.Refusal):
        i.new().setup(apply=True)
    path = i.new().path
    if kind == 'symlink':
        moved = path.with_suffix('.saved')
        path.rename(moved)
        path.symlink_to(moved)
    elif kind == 'mode':
        path.chmod(0o644)
    else:
        path.write_text('{}')
    with pytest.raises(native.Refusal):
        i.brew.check_setup_barrier()


@pytest.mark.parametrize('value', ['foreign', ''])
def test_existing_config_is_not_adopted_even_when_empty(installation, value):
    i = installation
    i.brew.config.parent.mkdir(parents=True)
    i.brew.config.write_text(value)
    with pytest.raises(native.Refusal, match='existing native.env'):
        i.new().setup(apply=True)
    assert i.brew.config.read_text() == value


def test_sql_redacts_credentials_and_rechecks_cluster_on_mutating_connection(installation, monkeypatch):
    i = installation
    i.new().setup(apply=True)
    p = provision.NativeProvisioner(i.brew)
    p.state = provision.read_journal(p.path)
    seen = []
    def execute(args, **kwargs):
        seen.append((args, kwargs))
        return SimpleNamespace(returncode=1, stdout='', stderr='sensitive diagnostic')
    monkeypatch.setattr(provision.subprocess, 'run', execute)
    with pytest.raises(native.Refusal) as error:
        p.sql('CREATE DATABASE example;')
    assert 'sensitive diagnostic' not in str(error.value)
    assert 'pg_control_system' in seen[0][1]['input']
    assert 'PGHOSTADDR' not in seen[0][1]['env']
    with pytest.raises(native.Refusal):
        p.sql('SELECT 1', database='unitares', application=True)
    assert seen[1][1]['env']['PGPASSWORD'] not in ' '.join(seen[1][0])


@pytest.mark.parametrize('problem', ['writer', 'postgres-command', 'redis-command', 'process',
                                   'docker', 'docker-unreadable', 'redis-data', 'redis-unreadable', 'port'])
def test_preflight_refuses_conflicts_before_any_mutation(installation, monkeypatch, problem):
    i = installation
    p = provision.NativeProvisioner(i.brew)
    records = {name: {'running': name not in native.WRITERS, 'pid': 123} for name in native.SERVICES}
    for name in native.WRITERS:
        binary = 'unitares-server' if name == 'unitares' else name
        records[name]['command'] = str(i.brew.prefix / 'opt' / name / 'bin' / binary)
    postgres = str(i.brew.prefix / 'opt/postgresql@17/bin/postgres')
    records['postgresql@17']['command'] = f'LC_ALL="en_US.UTF-8" {postgres} -D {i.brew.prefix}/var/postgresql@17'
    records['redis']['command'] = f'{i.brew.prefix}/opt/redis/bin/redis-server {i.brew.prefix}/etc/redis.conf'
    monkeypatch.setattr(i.brew, 'info', lambda: records)
    def ports(records):
        if problem == 'port':
            raise native.Refusal('foreign listener')
    monkeypatch.setattr(i.brew, 'check_ports', ports)
    monkeypatch.setattr(provision.shutil, 'which', lambda *a, **kw: '/bin/docker' if problem.startswith('docker') else None)
    def run(args, **kwargs):
        if args[0] == 'ps':
            return SimpleNamespace(returncode=0, stdout='/foreign/postgres' if problem == 'process' else postgres)
        if args[0] == 'docker':
            return SimpleNamespace(returncode=1 if problem == 'docker-unreadable' else 0, stdout='postgres-age\n')
        return SimpleNamespace(returncode=0, stdout='7' if problem == 'redis-data' else 'NOAUTH')
    monkeypatch.setattr(provision, 'run', run)
    if problem == 'writer':
        records['unitares']['running'] = True
    elif problem == 'postgres-command':
        records['postgresql@17']['command'] += ' -p 6432'
    elif problem == 'redis-command':
        records['redis']['command'] = 'custom'
    with pytest.raises(native.Refusal):
        p.preflight()
    assert not p.path.exists()
    assert not i.brew.config.exists()


@pytest.mark.parametrize('answer', ['0', 'NOAUTH Authentication required.'])
def test_atomic_redis_claim_refuses_foreign_or_unreadable_state(installation, monkeypatch, answer):
    p = provision.NativeProvisioner(installation.brew)
    p.state = {'install_id': 'fixture'}
    calls = []
    def redis(*args):
        calls.append(args)
        return answer
    monkeypatch.setattr(p, 'redis', redis)
    with pytest.raises(native.Refusal, match='Redis changed'):
        p.bind_redis()
    assert len(calls) == 1
    assert calls[0][0] == 'EVAL'
    assert 'DBSIZE' in calls[0][1]
    assert not any(word in calls[0][1] for word in ('FLUSH', 'DEL'))
