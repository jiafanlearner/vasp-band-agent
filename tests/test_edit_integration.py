import json
from types import SimpleNamespace as NS

import pytest

from vasp_agent.chat.ai_agent import Agent, Toolbox
from vasp_agent.ui.terminal_ui import TerminalUI
from test_ai_agent import FakeResponses, function, reply
from test_extra_commands import make
from test_server_identity import setup as setup_server
from vasp_agent.common import agent_paths


def edit_args(path='demo.py', content='x = 1\n'):
    return dict(path=path, operation='create', expected_sha256=None, content=content, patch=None, reason='用户要求')


def test_model_edits_persists_and_does_not_execute(tmp_path, monkeypatch):
    box = Toolbox(tmp_path, allow_submit=True)
    permission = box.permission_status()
    def no_run(*args, **kwargs):
        pytest.fail('draft editing must not execute commands')
    monkeypatch.setattr('subprocess.run', no_run)
    ui = TerminalUI('never')
    monkeypatch.setattr(ui, 'tool_finished', lambda *a: (_ for _ in ()).throw(RuntimeError('UI failure')))
    responses = FakeResponses([reply(function('edit_workspace_file', edit_args(), 'edit-1')), reply(text='完成')])
    agent = Agent(NS(responses=responses), 'test', box, ui=ui)
    assert agent.ask('创建草稿，不执行') == '完成'
    assert (tmp_path / 'workbench/demo.py').read_text() == 'x = 1\n'
    results = [json.loads(x['output']) for x in agent.history if x.get('type') == 'function_call_output']
    change_id = results[0]['result']['change_id']
    assert box.get_workspace_edit(change_id)['current_state'] == 'applied'
    assert box.permission_status() == permission
    audit = agent_paths.audit_file(tmp_path).read_text(encoding='utf-8')
    assert 'x = 1' not in audit and change_id in audit
    assert not (tmp_path / 'runs').exists()


def test_sensitive_arguments_not_in_trace_history_or_audit(tmp_path):
    token = 'sk-' + 'z' * 30
    ui = TerminalUI('never')
    responses = FakeResponses([reply(function('edit_workspace_file', edit_args(content=f'key = "{token}"\n'))), reply(text='拒绝')])
    agent = Agent(NS(responses=responses), 'test', Toolbox(tmp_path), ui=ui)
    agent.ask('创建脚本')
    assert token not in json.dumps(ui.trace)
    assert token not in agent.file.read_text(encoding='utf-8')
    assert token not in agent_paths.audit_file(tmp_path).read_text(encoding='utf-8')
    assert not (tmp_path / 'workbench/demo.py').exists()


def test_local_commands_work_without_model_or_network(tmp_path, monkeypatch):
    servers, config, compute, key = setup_server(tmp_path)
    c, out = make(tmp_path)
    c.agent.ask = lambda *a: pytest.fail('must not call model')
    monkeypatch.setattr('subprocess.run', lambda *a, **kw: pytest.fail('must not connect during key update'))
    c.handle(f'/server-key hpc "{key}"')
    record = json.loads(out[-1])['result']
    assert record['changed'] and not record['connection_verified']
    c.handle('/server-key --undo ' + record['change_id'])
    assert json.loads(config.read_text())['identity_file'].endswith('old')
    created = c.agent.tools.edit_workspace_file(**edit_args())
    c.handle('/changes')
    assert created['change_id'] in out[-1]
    c.handle('/changes ' + created['change_id'])
    assert '+x = 1' in out[-1]
    c.handle('/undo ' + created['change_id'])
    assert not (tmp_path / 'workbench/demo.py').exists()


def test_empty_connect_menu_cancel_and_selection(tmp_path, monkeypatch):
    c, out = make(tmp_path)
    c.handle('/connect')
    assert '尚无服务器配置' in out[-1]
    servers, _, _, key = setup_server(tmp_path)
    calls = []
    monkeypatch.setattr(c.agent.tools, 'remote_operation', lambda *a: calls.append(a) or {'connected': True})
    c.reader = lambda prompt: 'q'
    c.handle('/connect')
    assert not calls
    c.reader = lambda prompt: '1'
    c.handle('/connect')
    assert calls == [('hpc', 'info', None)]
    assert any('ssh_executable' in line for line in out)
    answers = iter(['1', str(key)])
    c.reader = lambda prompt: next(answers)
    c.handle('/server-key')
    assert json.loads(out[-1])['ok']


def test_formal_task_and_deployment_boundaries(tmp_path):
    box = Toolbox(tmp_path)
    from vasp_agent.remote.remote_worker import SOURCE_FILES
    assert not {'workspace_editor.py', 'edit_store.py', 'edit_commands.py'} & {name.rsplit('/', 1)[-1] for name in SOURCE_FILES}
    for name in ('../ai_agent.py', '../runs/si/INCAR', '../servers/hpc.json', '../.ai-audit.jsonl'):
        result = box.call('edit_workspace_file', edit_args(name))
        assert not result['ok'] and result['error_code'] == 'EDIT_PATH_BLOCKED'
    assert not box.allow_submit
    assert not any(s['name'] in ('shell', 'exec', 'submit_stage') for s in box.schemas())


def test_all_new_schemas_are_strict(tmp_path):
    from jsonschema import Draft202012Validator
    from vasp_agent.editing.workspace_editor import EDIT_TOOLS
    for schema in Toolbox(tmp_path).schemas():
        if schema['name'] in EDIT_TOOLS:
            Draft202012Validator.check_schema(schema['parameters'])
            assert set(schema['parameters']['required']) == set(schema['parameters']['properties'])
            assert not schema['parameters']['additionalProperties']
