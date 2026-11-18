from types import SimpleNamespace as NS
import ssl

import pytest
from vasp_agent.chat.ai_agent import Agent, Toolbox
from vasp_agent.models.model_adapters import Item
from vasp_agent.models.reconnect import retry_delay, wait_for_retry
from test_model_request_errors import failure
from vasp_agent.common import agent_paths


def test_reconnect_after_tool_does_not_replay_and_recovers(tmp_path, monkeypatch):
    delays, events = [], []
    monkeypatch.setattr('vasp_agent.models.reconnect.wait_for_retry', lambda seconds, event: delays.append(seconds))
    class Adapter:
        calls = 0
        def create(self, *args):
            self.calls += 1
            if self.calls == 1:
                return NS(status='completed', output_text='', output=[Item(type='function_call', call_id='a', name='list_files', arguments='{}')])
            assert args[2][-1]['type'] == 'function_call_output'
            assert sum(x.get('role') == 'user' for x in args[2]) == 1
            if self.calls <= 3:
                raise ConnectionError('secret URL')
            return NS(status='completed', output_text='恢复成功', output=[])
    adapter = Adapter()
    agent = Agent(None, 'test', Toolbox(tmp_path), adapter=adapter, model_retries=5)
    agent.ui.activity = events.append
    assert agent.ask('读取文件') == '恢复成功'
    assert delays == [5, 10] and adapter.calls == 4
    assert agent.usage['requests'] == 4 and agent.usage['failed_requests'] == 2
    assert len(agent_paths.audit_file(tmp_path).read_text().splitlines()) == 1
    assert any('已恢复' in x for x in events) and not any('secret' in x for x in events)


def test_exhaustion_and_manual_resume(tmp_path, monkeypatch):
    delays = []
    monkeypatch.setattr('vasp_agent.models.reconnect.wait_for_retry', lambda seconds, event: delays.append(seconds))
    class Adapter:
        def create(self, *args):
            raise TimeoutError()
    agent = Agent(None, 'test', Toolbox(tmp_path), adapter=Adapter(), model_retries=5)
    assert '已自动重试 5 次' in agent.ask('任务')
    assert delays == [5, 10, 20, 40, 60]
    assert agent.usage['requests'] == 6
    recovered = Agent(None, 'test', Toolbox(tmp_path), adapter=Adapter())
    assert [x for x in recovered.history if x.get('role') != 'developer'] == [{'role': 'user', 'content': '任务'}]
    assert recovered.history[-1] == recovered.runtime_state()


@pytest.mark.parametrize('status', [400, 401, 403, 404, 422, 501])
def test_permanent_errors_do_not_retry(status):
    assert retry_delay(failure(status), 0, 300) is None


@pytest.mark.parametrize('status', [408, 429, 500, 502, 503, 504])
def test_transient_http_errors_honor_retry_after(status):
    assert retry_delay(failure(status, body={'retry_after': 60}), 0, 300) == 60
    assert retry_delay(failure(status, headers={'Retry-After': '120'}), 0, 100) is None


def test_certificate_and_quota_are_not_retried():
    exc = ConnectionError()
    exc.__cause__ = ssl.SSLCertVerificationError('secret')
    assert retry_delay(exc, 0, 300) is None
    assert retry_delay(failure(429, body={'error': {'code': 'insufficient_quota'}}), 0, 300) is None
    assert retry_delay(ValueError('bug'), 0, 300) is None


@pytest.mark.parametrize('interrupt_in_wait', [True, False])
def test_ctrl_c_keeps_prompt_and_history(tmp_path, monkeypatch, interrupt_in_wait):
    def wait(*args):
        raise KeyboardInterrupt()
    monkeypatch.setattr('vasp_agent.models.reconnect.wait_for_retry', wait)
    class Adapter:
        def create(self, *args):
            if interrupt_in_wait:
                raise ConnectionError()
            raise KeyboardInterrupt()
    agent = Agent(None, 'test', Toolbox(tmp_path), adapter=Adapter(), model_retries=5)
    assert '已取消' in agent.ask('任务')
    assert agent.file.exists() and agent.usage['requests'] == 1


@pytest.mark.parametrize('http_failure', [True, False])
def test_retry_reason_visible_before_cancelling_wait(tmp_path, monkeypatch, http_failure):
    import httpx
    from openai import APIConnectionError

    events = []
    class Adapter:
        def create(self, *args):
            if http_failure:
                raise failure(502, body={'error': 'secret provider body'})
            raise APIConnectionError(request=httpx.Request('POST', 'https://test.invalid?key=secret')) from httpx.RemoteProtocolError('secret URL')

    def cancel_wait(*args):
        expected = 'HTTP 502' if http_failure else '远端 HTTP 响应不完整或协议异常'
        assert any(expected in text for text in events)
        raise KeyboardInterrupt()

    monkeypatch.setattr('vasp_agent.models.reconnect.wait_for_retry', cancel_wait)
    agent = Agent(None, 'test', Toolbox(tmp_path), adapter=Adapter(), model_retries=5)
    agent.ui.activity = events.append
    assert '已取消' in agent.ask('你好')
    assert agent.usage['requests'] == 1
    assert not any('secret' in text or 'test.invalid' in text for text in events)


def test_countdown_uses_short_sleeps_and_reports_progress(monkeypatch):
    now, sleeps, events = [0.0], [], []
    monkeypatch.setattr('vasp_agent.models.reconnect.time.monotonic', lambda: now[0])
    def sleep(seconds):
        sleeps.append(seconds)
        now[0] += seconds
    monkeypatch.setattr('vasp_agent.models.reconnect.time.sleep', sleep)
    wait_for_retry(25, events.append)
    assert max(sleeps) <= 1 and sum(sleeps) == 25 and len(events) == 2


def test_wait_budget_stops_without_ignoring_server_delay(tmp_path, monkeypatch):
    delays = []
    monkeypatch.setattr('vasp_agent.models.reconnect.wait_for_retry', lambda seconds, event: delays.append(seconds))
    class Adapter:
        def create(self, *args):
            raise failure(503, body={'retry_after': 200})
    agent = Agent(None, 'test', Toolbox(tmp_path), adapter=Adapter(), model_retries=5)
    result = agent.ask('任务')
    assert delays == [200] and agent.usage['requests'] == 2
    assert '已自动重试 1 次' in result and '至少等待 200 秒' in result
