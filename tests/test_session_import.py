import json

import pytest

from vasp_agent.chat.chat_commands import portable_history
from test_chat_commands import setup


def old_session(tmp_path, archived=False):
    folder = tmp_path / '.ai-archive' if archived else tmp_path
    folder.mkdir(exist_ok=True)
    path = folder / '.ai-session-old-conversation-000000000000.json'
    history = [
        {'role': 'user', 'content': '先检查 relaxation 的状态'},
        {'type': 'reasoning', 'id': 'old-id', 'encrypted_content': 'provider-private-data'},
        {'type': 'function_call', 'name': 'submit_stage', 'call_id': 'call-old',
         'arguments': '{"server":"test","job_name":"si","phase":"relaxation"}'},
        {'type': 'function_call_output', 'call_id': 'call-old', 'output': '{"ok":false,"error":"timeout"}'},
        {'type': 'message', 'role': 'assistant', 'id': 'msg-old',
         'content': [{'type': 'output_text', 'text': '提交状态未知，须核对。'}]},
    ]
    path.write_text(json.dumps(history, ensure_ascii=False), encoding='utf-8')
    return path


@pytest.mark.parametrize('archived', [False, True])
def test_import_preserves_source_revokes_permission_and_never_calls_api(tmp_path, archived, monkeypatch):
    source = old_session(tmp_path, archived)
    before = source.read_bytes()
    c, output = setup(tmp_path)
    c.agent.tools.allow_submit = True
    c.agent.tools.resource_confirmations['old'] = {'confirmed': True}
    c.agent.ask = lambda *args: pytest.fail('must not contact model')
    monkeypatch.setattr('subprocess.run', lambda *a, **kw: pytest.fail('must not connect to SSH'))
    responses = iter(['1', '导入'])
    c.reader = lambda _: next(responses)
    assert bool(c.entries()) is (not archived)
    assert c.handle('/import-session') == 'handled'
    assert c.name.startswith('import-')
    assert source.read_bytes() == before
    assert not c.agent.tools.allow_submit
    assert not c.agent.tools.resource_confirmations
    text = c.agent.file.read_text(encoding='utf-8')
    assert '提交状态未知' in text and 'relaxation' in text
    assert 'provider-private-data' not in text and 'msg-old' not in text
    assert all(set(item) == {'role', 'content'} for item in c.agent.history)
    assert any('发送给新接口' in line for line in output)
    assert any(entry[0] == c.name for entry in c.entries())
    imported_file = c.agent.file
    c.switch(c.name)
    assert c.agent.file == imported_file


@pytest.mark.parametrize('answers', [['q'], ['1', ''], ['1', '确认'], ['99']])
def test_cancel_or_invalid_input_does_not_import(tmp_path, answers):
    old_session(tmp_path)
    c, _ = setup(tmp_path)
    before = set(tmp_path.glob('.ai-session-*.json'))
    responses = iter(answers)
    c.reader = lambda _: next(responses)
    c.handle('/import-session')
    assert c.name == 'default'
    assert set(tmp_path.glob('.ai-session-*.json')) == before


def test_import_incomplete_call_is_only_text():
    result = portable_history([{'type': 'function_call', 'name': 'submit_stage',
                                'arguments': '{}', 'call_id': 'pending'}])
    assert '不代表已执行' in result[0]['content']
    assert all('type' not in item for item in result)
    assert '不得重放' in result[-1]['content']


@pytest.mark.parametrize('bad', [{}, ['bad'], [], [{'type': 'reasoning', 'encrypted_content': 'x'}]])
def test_bad_history_does_not_import(bad):
    with pytest.raises(ValueError):
        portable_history(bad)


def test_same_provider_session_remains_in_regular_menu(tmp_path):
    c, output = setup(tmp_path)
    c.handle('/new saved')
    c.handle('/import-session')
    assert '没有其他接口/模型' in output[-1]


def test_sessions_continue_other_provider_preserves_original(tmp_path):
    source = old_session(tmp_path)
    original = source.read_bytes()
    c, output = setup(tmp_path)
    c.agent.tools.allow_submit = True
    c.reader = lambda _: '继续'
    assert c.entries()[0][0] == 'old-conversation@000000000000'
    c.switch(c.entries()[0][0])
    assert c.name == 'old-conversation'
    assert source.read_bytes() == original
    assert not c.agent.tools.allow_submit
    assert all(x.get('type') not in ('reasoning', 'compaction', 'function_call', 'function_call_output') for x in c.agent.history)
    assert '提交状态未知' in json.dumps(c.agent.history, ensure_ascii=False)
    assert not c.agent.file.with_suffix('.context.json').exists()


def test_cross_provider_cancel_no_files_or_state_changed(tmp_path):
    old_session(tmp_path)
    c, _ = setup(tmp_path)
    before = set(tmp_path.iterdir())
    c.reader = lambda _: 'q'
    c.switch('old-conversation@000000000000')
    assert c.name == 'default' and set(tmp_path.iterdir()) == before


def test_name_collision_preserves_existing_target(tmp_path):
    source = old_session(tmp_path)
    c, _ = setup(tmp_path)
    c.switch('old-conversation', create=True)
    target = c.agent.file
    target.write_text('[{"role":"user","content":"newer conversation"}]', encoding='utf-8')
    c.reader = lambda _: '继续'
    c.switch('old-conversation@000000000000')
    assert c.name.startswith('old-conversation-')
    assert 'newer conversation' in target.read_text(encoding='utf-8')
    assert source.exists()


def test_bare_resume_finds_foreign_session(tmp_path):
    old_session(tmp_path)
    c, _ = setup(tmp_path)
    c.reader = lambda _: '继续'
    c.handle('/resume old-conversation')
    assert c.name == 'old-conversation'


def test_native_compaction_stripped_and_provider_ids_not_protocol_items():
    history = [{'role':'user','content':'task'},
               {'type':'compaction','encrypted_content':'DO-NOT-FORWARD'},
               {'type':'reasoning','encrypted_content':'DO-NOT-FORWARD'}]
    converted = portable_history(history)
    assert 'DO-NOT-FORWARD' not in json.dumps(converted)


def test_session_list_excludes_cache_and_invalid_files(tmp_path):
    c, _ = setup(tmp_path)
    for name in ('.ai-session-foo-000000000000.context.json', '.ai-session-foo-000000000000.parts.json'):
        (tmp_path / name).write_text('[]')
    assert c.entries() == []
