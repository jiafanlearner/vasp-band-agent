"""Offline model gateway failures: persistence, user-driven retries, safe output."""
from datetime import datetime, timezone
from types import SimpleNamespace as NS

from openai import APIStatusError, APIConnectionError, APITimeoutError, _base_client
import pytest

httpx = getattr(_base_client, 'httpx2', None) or getattr(_base_client, 'httpx')

from vasp_agent.chat.ai_agent import Agent, Toolbox
from vasp_agent.models.model_adapters import Item
from vasp_agent.models.request_errors import request_failure_message, request_failure_summary
from vasp_agent.common import agent_paths


def failure(status=502, body=None, headers=None):
    response = httpx.Response(status, headers=headers,
                              request=httpx.Request('POST', 'https://test.invalid/v1/responses'))
    return APIStatusError('untrusted secret error body', response=response, body=body)


def test_gateway_failure_after_tool_restart_and_repeated_retry(tmp_path):
    class Adapter:
        calls = 0

        def create(self, *args):
            self.calls += 1
            if self.calls == 1:
                return NS(status='completed', output_text='', output=[Item(
                    type='function_call', call_id='one', name='list_files', arguments='{}')])
            assert args[2][-1]['type'] == 'function_call_output'
            assert len([x for x in args[2] if x.get('role') == 'user']) == 1
            if self.calls <= 3:
                raise failure(body={'retry_after': 60, 'what_you_should_do': 'secret'})
            return NS(status='completed', output_text='恢复成功', output=[])

    adapter = Adapter()
    agent = Agent(None, 'test', Toolbox(tmp_path), adapter=adapter)
    message = agent.ask('检查 Si.POSCAR')
    assert 'HTTP 502' in message and '至少等待 60 秒' in message
    assert '--timeout' not in message and 'secret' not in message
    assert adapter.calls == 2  # No hidden automatic model retries.
    saved = agent.file.read_bytes()
    recovered = Agent(None, 'test', Toolbox(tmp_path), adapter=adapter)
    assert 'HTTP 502' in recovered.ask()
    assert recovered.file.read_bytes() == saved
    assert recovered.ask() == '恢复成功'
    assert adapter.calls == 4
    assert len(agent_paths.audit_file(tmp_path).read_text(encoding='utf-8').splitlines()) == 1
    assert recovered.usage['failed_requests'] == 1


@pytest.mark.parametrize('status', [400, 401, 403, 408, 429, 500, 502, 503, 504])
def test_first_request_http_failure_preserves_message(tmp_path, status):
    class Adapter:
        calls = 0

        def create(self, *args):
            self.calls += 1
            raise failure(status)

    adapter = Adapter()
    agent = Agent(None, 'test', Toolbox(tmp_path), adapter=adapter)
    result = agent.ask('未优化的 Si 结构')
    assert f'HTTP {status}' in result and '/retry' in result
    assert adapter.calls == 1
    recovered = Agent(None, 'test', Toolbox(tmp_path), adapter=adapter)
    assert recovered.history[-1] == recovered.runtime_state()
    assert [x for x in recovered.history if x.get('role') != 'developer'] == [
        {'role': 'user', 'content': '未优化的 Si 结构'}]
    assert not agent_paths.audit_file(tmp_path).exists()
    if status in (401, 403):
        assert '权限' in result and '不能仅靠等待' in result
    elif status == 400:
        assert '请求配置' in result


@pytest.mark.parametrize('value', [None, {}, [], True, -1, 10 ** 400, 'NaN', 'Infinity',
                                  '999999999999999999999', 'secret\x1b[31m', 'x' * 10000])
def test_malformed_retry_metadata_is_not_echoed(value):
    message = request_failure_message(failure(body={'retry_after': value}))
    assert '稍后输入 /retry' in message
    assert 'secret' not in message and '\x1b' not in message


def test_retry_after_header_date_and_nested_body():
    now = datetime(2026, 9, 28, 6, 50, tzinfo=timezone.utc)
    exc = failure(headers={'Retry-After': 'Mon, 28 Sep 2026 06:51:00 GMT'},
                  body={'error': {'retry_after': 90}})
    assert '至少等待 90 秒' in request_failure_message(exc, now=now)
    assert '至少等待 60 秒' in request_failure_message(
        failure(headers={'Retry-After': '60'}), now=now)
    assert '至少等待 60 秒' in request_failure_message(
        failure(headers={'Retry-After': 'Mon, 28 Sep 2026 06:51:00 GMT'}), now=now)


@pytest.mark.parametrize('body', [None, 'secret raw HTML', ['secret'], {'error': 'secret'}])
def test_non_object_error_bodies_are_safe(body):
    message = request_failure_message(failure(body=body))
    assert 'HTTP 502' in message and 'secret' not in message


def test_programming_error_is_not_disguised_as_gateway_failure(tmp_path):
    class Adapter:
        def create(self, *args):
            raise ValueError('bug')

    assert request_failure_message(ValueError('bug')) is None
    with pytest.raises(ValueError, match='bug'):
        Agent(None, 'test', Toolbox(tmp_path), adapter=Adapter()).ask('任务')


@pytest.mark.parametrize('protocol', ['responses', 'chat_completions'])
def test_interactive_main_keeps_prompt_and_retry_after_502(tmp_path, monkeypatch, protocol):
    from vasp_agent.chat.ai_agent import main
    from vasp_agent.models.model_config import ModelConfig
    from vasp_agent.ui.terminal_ui import TerminalUI

    class Adapter:
        calls = 0

        def create(self, *args):
            self.calls += 1
            if self.calls == 1:
                raise failure(body={'retry_after': 60})
            return NS(status='completed', output_text='可继续', output=[])

    adapter = Adapter()
    inputs = iter(['检查结构', '/retry', '/exit'])
    replies = []
    config = ModelConfig('test', 'https://test.invalid/v1', 'test', protocol, 'TEST_KEY', 'fake')
    monkeypatch.setattr('sys.argv', ['ai_agent.py', '--workspace', str(tmp_path), '--color', 'never', '--model-retries', '0'])
    monkeypatch.setattr('vasp_agent.models.model_config.resolve_config', lambda *a, **k: config)
    monkeypatch.setattr('openai.OpenAI', lambda **kwargs: NS())
    monkeypatch.setattr('vasp_agent.models.model_adapters.adapter_for', lambda *a: adapter)
    monkeypatch.setattr(TerminalUI, 'prompt', lambda self: next(inputs))
    monkeypatch.setattr(TerminalUI, 'reply', lambda self, value: replies.append(value))
    main()
    assert len(replies) == 2
    assert 'HTTP 502' in replies[0] and replies[1] == '可继续'
    assert adapter.calls == 2


@pytest.mark.parametrize('timed_out', [False, True])
def test_transport_diagnostics_use_real_elapsed_and_active_client(tmp_path, monkeypatch, timed_out):
    clock = iter([100.0, 105.2])
    monkeypatch.setattr('vasp_agent.chat.ai_agent.monotonic', lambda: next(clock))

    class Adapter:
        client = NS(timeout=180)

        def create(self, *args):
            req = httpx.Request('POST', 'https://test.invalid')
            if timed_out:
                raise APITimeoutError(request=req) from httpx.ReadTimeout('secret-token')
            raise APIConnectionError(request=req) from httpx.RemoteProtocolError('secret-token')

    agent = Agent(NS(timeout=999), 'test', Toolbox(tmp_path), adapter=Adapter())
    result = agent.ask('检查文件')
    assert '实际等待：5.2 秒' in result
    assert '配置超时：180 秒' in result and '999' not in result
    assert 'secret-token' not in result
    assert '并非总等待时长' in result
    if timed_out:
        assert '异常类型：APITimeoutError' in result
        assert '等待响应数据超时' in result
    else:
        assert '异常类型：APIConnectionError' in result
        assert '远端 HTTP 响应不完整或协议异常' in result
        assert '配置超时发生' in result and '/settings timeout 600' not in result
    assert agent.usage['requests'] == agent.usage['failed_requests'] == 1


@pytest.mark.parametrize('cause,expected', [
    (httpx.ConnectTimeout('secret'), '建立连接超时'),
    (httpx.ReadTimeout('secret'), '等待响应数据超时'),
    (httpx.WriteTimeout('secret'), '发送请求数据超时'),
    (httpx.PoolTimeout('secret'), '等待连接池超时'),
    (ConnectionResetError('secret'), '连接被重置'),
    (ConnectionRefusedError('secret'), '连接被拒绝'),
    (httpx.ProxyError('secret'), '代理连接异常'),
])
def test_cause_categories_never_echo_raw_errors(cause, expected):
    exc = APIConnectionError(request=httpx.Request('POST', 'https://test.invalid?key=secret'))
    exc.__cause__ = cause
    message = request_failure_message(exc, elapsed_seconds=.4, timeout=180)
    assert expected in message
    assert 'secret' not in message and 'test.invalid' not in message


def test_nested_tls_dns_and_cyclic_chains_are_safe():
    import ssl
    import socket

    exc = APIConnectionError(request=httpx.Request('POST', 'https://test.invalid'))
    exc.__cause__ = httpx.ConnectError('Authorization: Bearer secret')
    exc.__cause__.__cause__ = ssl.SSLCertVerificationError('secret certificate')
    exc.__cause__.__cause__.__cause__ = exc
    message = request_failure_message(exc)
    assert '连接建立失败 → TLS 证书验证失败' in message
    assert '实际等待：未知' in message and '配置超时：未知' in message
    assert 'secret' not in message
    exc.__cause__ = socket.gaierror('secret host')
    assert 'DNS 地址解析失败' in request_failure_message(exc)


def test_unknown_or_suppressed_cause_is_not_invented():
    exc = APIConnectionError(request=httpx.Request('POST', 'https://test.invalid'), message='secret')
    exc.__context__ = ConnectionResetError('secret')
    exc.__suppress_context__ = True
    message = request_failure_message(exc)
    assert '无法进一步定位' in message and '连接被重置' not in message
    assert 'secret' not in message


@pytest.mark.parametrize('module', ['httpcore', 'httpcore2'])
def test_mapped_tls_failure_remains_visible_without_sensitive_prose(module):
    import ssl

    mapped_type = type('ConnectError', (Exception,), {'__module__': module})
    mapped = mapped_type('secret proxy address')
    mapped.__context__ = ssl.SSLEOFError('secret TLS details')
    mapped.__suppress_context__ = True
    exc = APIConnectionError(request=httpx.Request('POST', 'https://test.invalid?key=secret'))
    exc.__cause__ = httpx.ConnectError('secret bearer token')
    exc.__cause__.__cause__ = mapped
    message = request_failure_message(exc)
    assert '连接建立失败' in message and 'SSLEOFError' in message
    assert 'secret' not in message and 'test.invalid' not in message
    assert 'SSLEOFError' in request_failure_summary(exc)


def test_retry_summary_reports_status_and_safe_causes():
    assert request_failure_summary(failure(502, body={'error': 'secret'})) == 'HTTP 502'
    exc = APIConnectionError(request=httpx.Request('POST', 'https://test.invalid?key=secret'))
    exc.__cause__ = httpx.RemoteProtocolError('Authorization: Bearer secret')
    assert request_failure_summary(exc) == '连接异常 · 远端 HTTP 响应不完整或协议异常'
    exc.__cause__ = TypeError('secret argument https://test.invalid')
    summary = request_failure_summary(exc)
    assert 'TypeError' in summary and 'secret' not in summary and 'test.invalid' not in summary
    assert 'TypeError' in request_failure_message(exc)
    assert request_failure_summary(ValueError('secret programming error')) is None


def test_split_and_disabled_timeouts():
    exc = TimeoutError('secret')
    message = request_failure_message(exc, timeout=httpx.Timeout(180, connect=10, pool=None))
    assert '连接 10 秒；读取 180 秒；写入 180 秒；连接池 无限制' in message
    assert '配置超时：未设置超时限制' in request_failure_message(exc, timeout=None)


def test_settings_timeout_change_is_reported_on_next_retry(tmp_path, monkeypatch):
    from vasp_agent.chat.ai_agent import main
    from vasp_agent.models.model_config import ModelConfig
    from vasp_agent.ui.terminal_ui import TerminalUI

    class Adapter:
        def create(self, *args):
            raise APIConnectionError(request=httpx.Request('POST', 'https://test.invalid'))

    inputs = iter(['任务', '/settings timeout 600', '/retry', '/exit'])
    replies = []
    config = ModelConfig('test', 'https://test.invalid/v1', 'test', 'responses', 'TEST_KEY', 'fake')
    monkeypatch.setattr('sys.argv', ['ai_agent.py', '--workspace', str(tmp_path), '--color', 'never', '--model-retries', '0'])
    monkeypatch.setattr('vasp_agent.models.model_config.resolve_config', lambda *a, **k: config)
    monkeypatch.setattr('openai.OpenAI', lambda **kwargs: NS(timeout=kwargs['timeout']))
    monkeypatch.setattr('vasp_agent.models.model_adapters.adapter_for', lambda *a: Adapter())
    monkeypatch.setattr(TerminalUI, 'prompt', lambda self: next(inputs))
    monkeypatch.setattr(TerminalUI, 'reply', lambda self, value: replies.append(value))
    main()
    assert len(replies) == 2
    assert '配置超时：180 秒' in replies[0]
    assert '配置超时：600 秒' in replies[1]
