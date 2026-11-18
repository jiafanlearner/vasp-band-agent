import json
from types import SimpleNamespace as NS

import pytest

from vasp_agent.chat.ai_agent import Agent, Toolbox
from vasp_agent.chat.chat_commands import Commands
from vasp_agent.remote.remote_worker import check_resources
from vasp_agent.ui.terminal_ui import TerminalUI


SCRIPT = '''#!/bin/bash
#SBATCH --partition=compute
#SBATCH --nodes=1
#SBATCH --ntasks=8
#SBATCH --time=04:00:00
echo run
'''
LIMITS = dict(server='test', partition='compute', nodes=1, cores=8, walltime='04:00:00')


def commands(tmp_path, answers):
    servers = tmp_path / 'servers'
    servers.mkdir(exist_ok=True)
    (servers / 'test.json').write_text('{"host":"example.org","user":"tester","port":22}')
    (servers / 'test.compute.json').write_text(json.dumps(dict(root='/work/test/agent',
        python='/opt/python', potcar_library='/data/PBE', vasp_env='/opt/env.sh',
        partition='compute', ntasks=8, walltime='04:00:00')))
    box = Toolbox(tmp_path)
    factory = lambda name: Agent(None, 'test', box, name + '-abc')
    responses = iter(answers)
    return Commands(factory('default'), factory, NS(identity=lambda: 'abc'), 'default',
                    TerminalUI('never'), reader=lambda _: next(responses), output=lambda _: None)


def test_once_is_operator_only_and_revokes_after_attempt(tmp_path, monkeypatch):
    monkeypatch.setattr('vasp_agent.permissions.submission_dialog.confirm', lambda *a, **kw: None)
    c = commands(tmp_path, ['1', '', '', '', ''])
    c.handle('/submit once')
    box = c.agent.tools
    assert box.submission_limits == LIMITS
    assert box.allow_submit
    calls = []
    from vasp_agent.remote.remote_compute import RemoteCompute
    def transport(self, action, job, **kwargs):
        if action == 'submission_preflight':
            return {'ready': True}
        if action == 'resource_context':
            raise TimeoutError('read also unavailable')
        calls.append(kwargs)
        raise TimeoutError('uncertain')
    monkeypatch.setattr(RemoteCompute, 'call', transport)
    assert not box.call('remote_submit', dict(server='other', job_name='si'))['ok']
    assert box.allow_submit and not calls
    assert not box.call('remote_submit', dict(server='test', job_name='si'))['ok']
    assert calls[0]['limits'] == LIMITS
    assert not box.allow_submit
    assert not box.call('remote_submit', dict(server='test', job_name='si'))['ok']
    assert len(calls) == 1
    assert not box.call('grant_submission', {})['ok']


def test_switch_and_restart_reset_permissions(tmp_path):
    c = commands(tmp_path, ['1', '', '', '', ''])
    c.handle('/submit once')
    c.handle('/new other')
    assert not c.agent.tools.allow_submit
    assert not Toolbox(tmp_path).allow_submit


def test_cancel_preserves_grant_and_off_revokes(tmp_path):
    c = commands(tmp_path, ['1', '', '', '', '', 'q'])
    c.handle('/submit once')
    c.handle('/submit on')
    assert c.agent.tools.allow_submit
    c.handle('/submit off')
    assert not c.agent.tools.allow_submit


@pytest.mark.parametrize('before,after', [
    ('--ntasks=8', '--ntasks=9'), ('--nodes=1', '--nodes=2'),
    ('--partition=compute', '--partition=other'), ('04:00:00', '04:00:01'),
    ('--ntasks=8', '--ntasks=8\n#SBATCH --cpus-per-task=2'),
    ('--nodes=1', '-N 1'), ('--nodes=1', '--nodes=1\n#SBATCH --nodes=2'),
])
def test_resource_limits_fail_closed(before, after):
    with pytest.raises(ValueError):
        check_resources(SCRIPT.replace(before, after), LIMITS)


def test_matching_resources_allowed():
    check_resources(SCRIPT, LIMITS)


def test_on_has_bounded_attempts_and_slash_hints(tmp_path):
    c = commands(tmp_path, ['1', '', '', '', '', '2'])
    c.handle('/submit on')
    box = c.agent.tools
    box.consume_submission()
    assert box.allow_submit and box.permission_status()['submissions_remaining'] == 1
    box.consume_submission()
    assert not box.allow_submit
    from vasp_agent.ui.interactive_prompt import SlashCompleter, command_catalog
    from prompt_toolkit.document import Document
    assert '/permissions' in command_catalog()
    assert [x.text for x in SlashCompleter().get_completions(Document('/submit o'), None)] == ['once', 'on', 'off']


def test_remote_limit_checked_before_any_submission(tmp_path, monkeypatch):
    import vasp_agent.remote.remote_worker as worker
    folder = tmp_path / 'jobs/si'
    folder.mkdir(parents=True)
    script = folder / 'submit.slurm'
    script.write_text(SCRIPT.replace('--ntasks=8', '--ntasks=16'))
    (folder / 'remote-job.json').write_text(json.dumps(dict(script_sha256=worker.sha(script))))
    monkeypatch.setattr(worker.subprocess, 'run', lambda *a, **k: pytest.fail('must not submit'))
    with pytest.raises(ValueError, match='上限'):
        worker.dispatch(dict(config={'root': str(tmp_path)}, action='submit', job_name='si', limits=LIMITS))
    assert not (folder / 'remote-submission.json').exists()


def test_new_grant_drops_selections_confirmed_under_previous_limits(tmp_path):
    c = commands(tmp_path, ['1', '', '', '', ''])
    box = c.agent.tools
    box.confirmed_resource_selections[('test', 'si', 'convergence')] = {'signature': 's', 'resources': {}}
    c.handle('/submit once')
    assert box.allow_submit and box.confirmed_resource_selections == {}


def test_bare_submit_offers_a_grant_and_defaults_never_grant(tmp_path):
    shown = []
    c = commands(tmp_path, ['1', '1', '', '', '', ''])
    c.output = shown.append
    c.handle('/submit')
    assert c.agent.tools.allow_submit and c.agent.tools.submission_limits == LIMITS
    assert any('授权一次提交' in line for line in shown)
    c = commands(tmp_path, ['2', '1', '1', '1'])
    c.output = shown.append
    c.handle('/submit')
    assert not c.agent.tools.allow_submit
    assert any('没有授权提交' in line for line in shown)
