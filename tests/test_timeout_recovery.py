from types import SimpleNamespace as NS
from openai import APIConnectionError, APITimeoutError, _base_client
import pytest

httpx = getattr(_base_client, 'httpx2', None) or getattr(_base_client, 'httpx')

from vasp_agent.chat.ai_agent import Agent, Toolbox
from vasp_agent.models.model_adapters import Item
from vasp_agent.common import agent_paths


@pytest.mark.parametrize('error', [
    TimeoutError('simulated'),
    APITimeoutError(request=httpx.Request('POST', 'https://test.invalid')),
    APIConnectionError(request=httpx.Request('POST', 'https://test.invalid')),
])
def test_timeout_after_tool_then_resume_without_replay(tmp_path, error):
    class Adapter:
        calls = 0
        def create(self, *args):
            self.calls += 1
            if self.calls == 1:
                return NS(status='completed', output_text='', output=[Item(type='function_call',
                     call_id='one', name='list_files', arguments='{}')])
            if self.calls == 2:
                raise error
            history = args[2]
            assert len([x for x in history if x.get('role') == 'user']) == 1
            assert history[-1]['type'] == 'function_call_output'
            return NS(status='completed', output_text='已读取结果，继续。', output=[])
    adapter = Adapter()
    agent = Agent(None, 'test', Toolbox(tmp_path), adapter=adapter)
    assert '/retry' in agent.ask('检查文件')
    # Simulate process restart with exactly the same session.
    recovered = Agent(None, 'test', Toolbox(tmp_path), adapter=adapter)
    assert recovered.ask() == '已读取结果，继续。'
    assert len(agent_paths.audit_file(tmp_path).read_text(encoding='utf-8').splitlines()) == 1


def test_first_request_timeout_preserves_user_message(tmp_path):
    class Adapter:
        def create(self, *args):
            raise TimeoutError()
    agent = Agent(None, 'test', Toolbox(tmp_path), adapter=Adapter())
    assert '/retry' in agent.ask('任务')
    assert agent.file.exists()
    recovered = Agent(None, 'test', Toolbox(tmp_path), adapter=Adapter())
    assert [x for x in recovered.history if x.get('role') != 'developer'] == [{'role': 'user', 'content': '任务'}]
    assert recovered.history[-1] == recovered.runtime_state()


def test_submit_response_lost_returns_record_without_resubmitting(tmp_path, monkeypatch):
    from vasp_agent.remote.remote_compute import RemoteCompute
    from test_submission_permissions import commands
    c = commands(tmp_path, ['1', '', '', '', ''])
    c.handle('/submit once')
    box = c.agent.tools
    monkeypatch.setattr('vasp_agent.permissions.submission_dialog.confirm', lambda *a, **kw: None)
    calls = []
    def rpc(self, action, job, **kwargs):
        calls.append(action)
        if action == 'submit_checked':
            raise TimeoutError('response lost')
        assert action == 'resource_context'
        return {'already_registered': True, 'status': 'submitted', 'job_id': '123'}
    monkeypatch.setattr(RemoteCompute, 'call', rpc)
    result = box.remote_submit('test', 'si', phase='convergence')
    assert result['job_id'] == '123' and result['record_recovered_after_error']
    assert result['submission_confirmed'] and result['original_error']['type'] == 'TimeoutError'
    assert calls == ['submit_checked', 'resource_context']
    assert not box.allow_submit


def test_existing_receipt_at_preflight_does_not_consume_or_submit(tmp_path, monkeypatch):
    from vasp_agent.remote.remote_compute import RemoteCompute
    from test_submission_permissions import commands
    c = commands(tmp_path, ['1', '', '', '', ''])
    c.handle('/submit once')
    box = c.agent.tools
    monkeypatch.setattr('vasp_agent.permissions.submission_dialog.confirm', lambda *a, **kw: None)
    calls = []
    def rpc(self, action, job, **kwargs):
        calls.append(action)
        assert action == 'submit_checked'
        return {'status': 'submission_uncertain', 'job_id': None, 'submission_not_attempted': True, 'existing_record': True}
    monkeypatch.setattr(RemoteCompute, 'call', rpc)
    before = box.submissions
    result = box.remote_submit('test', 'si', phase='convergence')
    assert result['submission_not_attempted']
    assert calls == ['submit_checked'] and box.submissions == before


def test_scheduler_error_is_not_reported_as_ssh_disconnect(tmp_path, monkeypatch):
    from vasp_agent.remote.remote_compute import RemoteCompute
    from vasp_agent.remote.remote_worker import SubmissionError
    from test_submission_permissions import commands
    c = commands(tmp_path, ['1', '', '', '', ''])
    c.handle('/submit once')
    monkeypatch.setattr('vasp_agent.permissions.submission_dialog.confirm', lambda *a, **kw: None)
    def rpc(self, action, job, **kwargs):
        if action == 'submit_checked':
            raise SubmissionError('SUBMISSION_UNCERTAIN', 'scheduler error token=hidden')
        return {'already_registered': True, 'status': 'submission_uncertain', 'job_id': None}
    monkeypatch.setattr(RemoteCompute, 'call', rpc)
    result = c.agent.tools.remote_submit('test', 'si', phase='convergence')
    assert result['submission_confirmed'] is False
    assert 'recovered_after_disconnect' not in result
    assert result['original_error']['code'] == 'SUBMISSION_UNCERTAIN'
    assert 'hidden' not in result['original_error']['message']
