"""Failure modes must not create passing local review records."""
import importlib.util
from pathlib import Path
import subprocess
import pytest

spec = importlib.util.spec_from_file_location('review_local', Path(__file__).parents[1] / 'scripts/dev/review_local.py')
local = importlib.util.module_from_spec(spec)
spec.loader.exec_module(local)


@pytest.mark.parametrize('value', ['https://localhost:11434', 'http://example.com',
                                  'http://user:pass@localhost', 'http://localhost/api',
                                  'http://localhost?target=x'])
def test_endpoint_refuses_remote_or_ambiguous(value):
    with pytest.raises(ValueError):
        local.endpoint(value)


def test_endpoint_accepts_loopback():
    assert local.endpoint('127.0.0.1:11434') == 'http://127.0.0.1:11434'


@pytest.mark.parametrize('path', ['../secret', '/etc/passwd', 'a/../../secret', 'HEAD:file', 'a\\b'])
def test_repository_paths_cannot_escape(path):
    with pytest.raises(ValueError):
        local.safe_path(path)


@pytest.fixture
def repo(tmp_path):
    def git(*args):
        return subprocess.run(['git', *args], cwd=tmp_path, check=True, capture_output=True, text=True).stdout.strip()
    git('init')
    git('config', 'user.email', 'test@example.com')
    git('config', 'user.name', 'Test')
    (tmp_path / 'file.py').write_text('original\nsecond\n')
    git('add', '.')
    git('commit', '-m', 'initial')
    head = git('rev-parse', 'HEAD')
    (tmp_path / 'file.py').write_text('uncommitted SECRET\n')
    return tmp_path, head


def test_tools_read_commit_not_working_tree(repo):
    path, head = repo
    output = local.read_file(path, head, {'path': 'file.py', 'start_line': 2, 'line_count': 1})
    assert '2: second' in output
    assert 'SECRET' not in local.search(path, head, {'query': 'original'})
    assert 'original' in local.search(path, head, {'query': 'original'})


def test_context_failure_never_calls_model(repo):
    path, head = repo
    def forbidden(*args):
        pytest.fail('oversized diff must not reach model')
    with pytest.raises(ValueError, match='Context budget'):
        local.review(path, head, 'x' * 17000, 'gemma4', 'http://localhost', 16384, 10, forbidden)


def test_bare_verdict_without_source_inspection_is_unreviewed(repo):
    path, head = repo
    def response(*args):
        return {'done': True, 'done_reason': 'stop', 'message': {'role': 'assistant', 'content': 'VERDICT: CLEAN'}}
    with pytest.raises(ValueError, match='surrounding source'):
        local.review(path, head, 'diff', 'gemma4', 'http://localhost', 16384, 10, response)


def test_token_limit_is_unreviewed(repo):
    path, head = repo
    with pytest.raises(ValueError, match='token-limited'):
        local.review(path, head, 'diff', 'gemma4', 'http://localhost', 16384, 10,
                     lambda *a: {'done': True, 'done_reason': 'length'})


def test_successful_tool_review_has_source_and_transcript(repo):
    path, head = repo
    responses = iter([
        {'done': True, 'message': {'role': 'assistant', 'content': '', 'tool_calls': [
            {'function': {'name': 'read_file', 'arguments': {'path': 'file.py'}}}]}},
        {'done': True, 'done_reason': 'stop', 'message': {'role': 'assistant',
         'content': 'Inspected original source and its callers. VERDICT: CLEAN'}},
    ])
    result, transcript, _ = local.review(path, head, 'diff', 'gemma4', 'http://localhost',
                                         16384, 10, lambda *a: next(responses))
    assert 'VERDICT: CLEAN' in result
    assert 'original' in transcript[3]['content']
    assert 'SECRET' not in str(transcript)


@pytest.mark.parametrize('changed', ['headRefOid', 'baseRefOid', 'state'])
def test_recording_rejects_stale_or_closed_pr(changed):
    initial = {'headRefOid': 'head', 'baseRefOid': 'base', 'state': 'OPEN'}
    current = dict(initial)
    current[changed] = 'changed'
    with pytest.raises(ValueError, match='rerun'):
        local.assert_current(initial, current)


def test_empty_source_range_does_not_count_as_inspection(repo):
    path, head = repo
    with pytest.raises(ValueError, match='empty'):
        local.read_file(path, head, {'path': 'file.py', 'start_line': 999})


def test_symlink_is_not_followed(repo):
    path, _ = repo
    (path / 'link').symlink_to('/etc/passwd')
    subprocess.run(['git', 'add', 'link'], cwd=path, check=True)
    subprocess.run(['git', 'commit', '-m', 'symlink'], cwd=path, check=True, capture_output=True)
    head = local.command('git', 'rev-parse', 'HEAD', cwd=path).strip()
    with pytest.raises(ValueError, match='regular tracked'):
        local.read_file(path, head, {'path': 'link'})


def test_repeated_tool_loop_fails_early(repo):
    path, head = repo
    def repeated(*args):
        return {'done': True, 'message': {'role': 'assistant', 'content': '', 'tool_calls': [
            {'id': 'call-1', 'function': {'name': 'read_file', 'arguments': {'path': 'file.py'}}}]}}
    with pytest.raises(ValueError, match='without progress'):
        local.review(path, head, 'diff', 'qwen', 'http://localhost', 65536, 10, repeated)
