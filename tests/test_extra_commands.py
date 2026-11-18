import json
from types import SimpleNamespace as NS

import pytest

from vasp_agent.chat.ai_agent import Agent, Toolbox
from vasp_agent.chat.chat_commands import Commands
from vasp_agent.ui.terminal_ui import TerminalUI
import vasp_agent.chat.extra_commands as extra


def make(tmp_path):
    tools = Toolbox(tmp_path)
    config = NS(identity=lambda: '123456abcdef', public=lambda: {'api_key': '[configured]'})
    def factory(name):
        return Agent(None, 'test', tools, f'{name}-{config.identity()}')
    output = []
    commands = Commands(factory('default'), factory, config, 'default', TerminalUI('never'), output=output.append)
    commands.handle('/new first')
    commands.agent.history = [{'role': 'user', 'content': 'silicon investigation'}]
    return commands, output


def test_rename_export_archive_restore(tmp_path):
    c, output = make(tmp_path)
    old = c.agent.file
    c.handle('/rename silicon')
    assert c.name == 'silicon' and not old.exists()
    c.handle('/export')
    assert 'silicon investigation' in next((tmp_path / 'exports').glob('*.md')).read_text(encoding='utf-8')
    c.handle('/archive chat')
    assert c.name != 'silicon'
    assert 'silicon' not in [x[0] for x in c.entries()]
    c.handle('/search investigation')
    assert any('silicon investigation' in line for line in output)
    c.handle('/restore silicon')
    assert c.name == 'silicon' and c.agent.history[0]['content'] == 'silicon investigation'


def test_rename_does_not_overwrite(tmp_path):
    c, output = make(tmp_path)
    target = c.path('existing')
    target.write_text('[]')
    c.handle('/rename existing')
    assert c.name == 'first' and target.read_text() == '[]'


def test_cancel_requires_exact_job_id(tmp_path, monkeypatch):
    c, output = make(tmp_path)
    folder = c.agent.tools.job('si')
    folder.mkdir(parents=True)
    (folder / 'submission.json').write_text('{"job_id":"12345"}')
    calls = []
    monkeypatch.setattr(extra, 'run_slurm', lambda args: calls.append(args))
    c.reader = lambda prompt: 'yes'
    c.handle('/cancel si')
    assert calls == []
    c.reader = lambda prompt: '12345'
    c.handle('/cancel si')
    assert calls == [['scancel', '12345']]
    assert 'cancellation_requested' in (folder / 'events.jsonl').read_text()


def test_logs_and_results_are_local(tmp_path):
    c, output = make(tmp_path)
    folder = c.agent.tools.job('si')
    folder.mkdir(parents=True)
    (folder / 'stdout.log').write_text('SCF failed')
    (folder / 'report.json').write_text('{"sampled_path_gap": 1.2}')
    c.reader = lambda prompt: '1'
    c.handle('/logs si')
    c.handle('/results si')
    assert any('SCF failed' in line for line in output)
    assert any('sampled_path_gap' in line for line in output)


def test_usage_tracks_returned_tokens(tmp_path):
    class Adapter:
        def create(self, *args):
            return NS(output=[], output_text='done', status='completed', usage=NS(input_tokens=10, output_tokens=3))
    agent = Agent(None, 'test', Toolbox(tmp_path), adapter=Adapter())
    agent.ask('hello')
    assert agent.usage['requests'] == 1
    assert agent.usage['reported_input_tokens'] == 10
    assert agent.usage['reported_output_tokens'] == 3


def test_runtime_dispatch_and_error_keeps_session(tmp_path):
    c, output = make(tmp_path)
    calls = []
    c.runtime = lambda owner, command, arg: calls.append((command, arg))
    for command in ('/model demo', '/profile relay', '/connection', '/settings timeout 300'):
        c.handle(command)
    assert len(calls) == 4
    assert c.name == 'first'


def test_archive_twice_keeps_both_and_restore_refuses_overwrite(tmp_path):
    c, output = make(tmp_path)
    c.handle('/rename silicon')
    c.handle('/archive chat')
    c.switch('silicon', create=True)
    c.agent.history = [{'role': 'user', 'content': 'second'}]
    c.handle('/archive chat')
    from vasp_agent.common import agent_paths
    archived = sorted(stem for stem in agent_paths.session_files(tmp_path, archived=True) if stem.startswith('silicon'))
    assert len(archived) == 2
    c.switch('silicon', create=True)
    c.agent.history = [{'role': 'user', 'content': 'live'}]
    from vasp_agent.workflow import vasp_band_agent as core
    core.save(c.agent.file, c.agent.history)
    c.handle('/restore silicon')
    assert any('未覆盖' in line for line in output)
    assert json.loads(c.agent.file.read_text(encoding='utf-8'))[0]['content'] == 'live'


def test_diagnose_evidence_is_bounded(tmp_path, monkeypatch):
    c, output = make(tmp_path)
    tools = c.agent.tools
    monkeypatch.setattr(tools, 'get_status', lambda name: {'workflow-state.json': {'stage': 'scf'}, 'x': 'a' * 50000})
    monkeypatch.setattr(tools, 'read_log', lambda name, file: {'file': file, 'tail': 'b' * 11000 + 'ERROR END'})
    prompts = []
    monkeypatch.setattr(c.agent, 'ask', lambda message: prompts.append(message) or 'ok')
    c.handle('/diagnose si')
    assert len(prompts[0]) < extra.DIAGNOSE_EVIDENCE_CHARS + 200
    assert '证据已截断' in prompts[0] and 'ERROR END' in prompts[0]


def test_server_pickers_share_one_list(tmp_path):
    servers = tmp_path / 'servers'
    servers.mkdir()
    for name in ('beta', 'alpha'):
        (servers / f'{name}.json').write_text('{}')
        (servers / f'{name}.compute.json').write_text('{}')
    (servers / 'orphan.compute.json').write_text('{}')
    assert extra.configured_servers(Toolbox(tmp_path)) == ['alpha', 'beta']


def test_reselecting_current_session_keeps_grant_and_switch_explains_revocation(tmp_path):
    c, output = make(tmp_path)
    box = c.agent.tools
    box.allow_submit, box.submission_target = True, ('test', 'si', 'convergence')
    c.handle('/sessions first')
    assert c.agent.tools.allow_submit and any('已在当前对话' in line for line in output)
    c.handle('/new second')
    assert not c.agent.tools.allow_submit
    assert any('已撤销提交授权' in line for line in output)
    result = c.agent.tools.call('submit_stage', {'server': 'test', 'job_name': 'si', 'phase': 'convergence'})
    assert not result['ok'] and '切换了对话' in result['error'] and 'request_submission' in result['error']
