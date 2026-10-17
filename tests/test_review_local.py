"""Failure modes must not create passing local review records."""
import importlib.util
import json
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


def test_binary_patch_literal_in_source_is_not_a_binary_change():
    assert not local.binary_diff("diff --git a/check.py b/check.py\n+if 'GIT binary patch' in diff:\n")


def test_actual_git_binary_patch_is_rejected(repo):
    path, head = repo
    (path / 'binary').write_bytes(b'\x00payload')
    subprocess.run(['git', 'add', 'binary'], cwd=path, check=True)
    diff = local.command('git', 'diff', '--cached', '--binary', head, cwd=path)
    assert local.binary_diff(diff)


def test_supplied_source_is_immutable_and_delivered_to_reviewer(repo):
    path, head = repo
    contexts = local.seed_source(path, head, ['file.py'])
    def reply(url, payload, timeout):
        prompt = payload['messages'][1]['content']
        assert '1: original' in prompt
        assert 'SECRET' not in prompt
        assert 'not model-requested' in prompt
        return {'done': True, 'done_reason': 'stop', 'message': {'role': 'assistant',
                'content': 'Reviewed supplied source context.\nVERDICT: CLEAN'}}
    text, _, _ = local.review(path, head, 'full diff', 'gemma', 'http://localhost',
                              16384, 10, reply, source_context=contexts)
    assert 'VERDICT: CLEAN' in text


def test_unavailable_seed_does_not_manufacture_source_inspection(repo):
    path, head = repo
    assert local.seed_source(path, head, ['missing.py']) == {}


def test_unposted_completed_reviews_spend_budget_without_double_counting(tmp_path):
    import json
    for i, status in enumerate(['CLEAN', 'FINDINGS', 'UNREVIEWED', 'CLEAN']):
        run = tmp_path / f'pr-123-{i}'
        run.mkdir()
        (run / 'manifest.json').write_text(json.dumps({'pr': 123, 'status': status,
            'head': 'head', 'base': 'base', 'review_id': f'review-{i}'}))
    assert local.local_completed_rounds(tmp_path, 123, {'review-3'}) == 2
    assert local.local_completed_rounds(tmp_path, 124, set()) == 0


@pytest.mark.parametrize('data', [None, '{broken', '{}'])
def test_incomplete_local_review_history_fails_closed(tmp_path, data):
    run = tmp_path / 'pr-123-interrupted'
    run.mkdir()
    if data is not None:
        (run / 'manifest.json').write_text(data)
    with pytest.raises(ValueError, match='history incomplete'):
        local.local_completed_rounds(tmp_path, 123, set())


@pytest.mark.parametrize('missing', ['head', 'base'])
def test_cli_missing_local_objects_reports_unreviewed(repo, monkeypatch, capsys, missing):
    path, head = repo
    run = path / '.git/local-reviews/pr-123-missing'
    run.mkdir(parents=True)
    receipt = {'pr': 123, 'status': 'CLEAN', 'head': head, 'base': head,
               'started_at': 100, 'architecture': 'gemma4'}
    receipt[missing] = 'f' * 40
    (run / 'manifest.json').write_text(json.dumps(receipt))
    gate = local.gate_module()
    monkeypatch.setattr(gate, 'ROUND_CAP_ENABLED', True)
    monkeypatch.setattr(gate, 'repo_slug', lambda: 'o/r')
    monkeypatch.setattr(gate, 'pr_comments', lambda *a: [])
    monkeypatch.setattr(gate, 'pr_rounds', lambda *a: gate.CodexRounds())
    monkeypatch.setattr(local, 'gate_module', lambda: gate)
    monkeypatch.setattr(local, 'pr_info', lambda *a: {
        'state': 'OPEN', 'headRefOid': head, 'baseRefOid': head})
    monkeypatch.setattr(local, 'api', lambda *a: {
        'details': {'format': 'gguf'}, 'capabilities': ['tools'],
        'model_info': {'general.architecture': 'gemma4', 'gemma4.context_length': 131072}})
    monkeypatch.setattr(local, 'review', lambda *a, **kw: pytest.fail('spent budget with missing history'))
    command = local.command
    monkeypatch.setattr(local, 'command', lambda *a, **kw:
                        '' if a[:2] == ('git', 'fetch') else command(*a, **kw))
    monkeypatch.setattr(Path, 'home', lambda: path)
    monkeypatch.delenv('OLLAMA_HOST', raising=False)
    assert local.cli(['--repo', str(path), '--pr', '123', '--model', 'gemma4']) == 2
    assert 'UNREVIEWED: Local review history incomplete' in capsys.readouterr().err
    assert json.loads((run / 'manifest.json').read_text()) == receipt
    assert list(run.parent.iterdir()) == [run]


@pytest.mark.parametrize('model, metadata', [
    ('gemma4:cloud', {}), ('qwen:480b-cloud', {}),
    ('innocent-alias', {'remote_host': 'https://ollama.com'}),
    ('innocent-alias', {'remote_model': 'gemma4:31b'}),
])
def test_cloud_models_are_rejected_before_source_is_sent(model, metadata):
    with pytest.raises(ValueError, match='Cloud-backed'):
        local.local_model_architecture(model, metadata)


def test_validated_local_architecture_does_not_depend_on_alias():
    assert local.local_model_architecture('custom-alias', {'details': {'format': 'gguf'},
        'model_info': {'general.architecture': 'gemma4'}, 'capabilities': ['tools']}) == 'gemma4'


def budget_receipt(tmp_path, name, **fields):
    import json
    run = tmp_path / name
    run.mkdir()
    receipt = {'pr': 123, 'status': 'CLEAN', 'head': 'a' * 40, 'base': 'b' * 40,
               'model': 'custom-alias', 'architecture': 'gemma4', 'started_at': 200,
               'review_id': name}
    receipt.update(fields)
    (run / 'manifest.json').write_text(json.dumps(receipt))


@pytest.fixture
def budget_gate(monkeypatch):
    gate = local.gate_module()
    monkeypatch.setattr(gate, 'diff_key', lambda base, head: base + head)
    return gate


def test_unposted_second_family_finishes_capped_cycle_without_coverage(tmp_path, budget_gate):
    gate = budget_gate
    head, key = 'a' * 40, 'b' * 40 + 'a' * 40
    rounds = gate.count_review_cycles([(100 + i, head, [], key, 'openai') for i in range(3)])
    budget_receipt(tmp_path, 'pr-123-local')
    result = local.local_budget_rounds(gate, rounds, tmp_path, 123, set())
    assert result.count == 3
    assert result.cycles[-1].families == {'openai', 'google'}
    assert not result.can_join(head, key, 'google')
    assert gate.passing_families([], key) == set()


@pytest.mark.parametrize('fields', [
    {}, {'head': 'c' * 40}, {'base': 'd' * 40},
    {'architecture': 'unknown', 'model': 'custom-alias'},
])
def test_unposted_rerun_or_changed_work_consumes_another_cycle(tmp_path, budget_gate, fields):
    gate = budget_gate
    rounds = gate.count_review_cycles([(100, 'a' * 40, [], 'b' * 40 + 'a' * 40, 'google')])
    budget_receipt(tmp_path, 'pr-123-local', **fields)
    assert local.local_budget_rounds(gate, rounds, tmp_path, 123, set()).count == 2


def test_posted_local_receipt_is_counted_only_once(tmp_path, budget_gate):
    gate = budget_gate
    rounds = gate.count_review_cycles([(100, 'a' * 40, [], 'b' * 40 + 'a' * 40, 'google')])
    budget_receipt(tmp_path, 'pr-123-local')
    assert local.local_budget_rounds(gate, rounds, tmp_path, 123, {'pr-123-local'}) is rounds


def test_legacy_local_timestamp_and_unknown_history_are_preserved(tmp_path, budget_gate):
    gate = budget_gate
    budget_receipt(tmp_path, 'pr-123-aaaaaaaaaaaa-1700000000000000000', started_at=None)
    result = local.local_budget_rounds(gate, gate.CodexRounds(unknown_history=True), tmp_path, 123, set())
    assert result.count == 1
    assert result.runs[0][0] == 1700000000
    assert result.unknown_history


@pytest.mark.parametrize('when', [None, 'bad', -1, float('inf')])
def test_local_cycle_with_unknown_time_fails_closed(tmp_path, budget_gate, when):
    budget_receipt(tmp_path, 'pr-123-local', started_at=when)
    with pytest.raises(ValueError, match='timestamp'):
        local.local_budget_rounds(budget_gate, budget_gate.CodexRounds(), tmp_path, 123, set())
