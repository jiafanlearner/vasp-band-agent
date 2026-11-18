"""Optional HTTP proxy bypass; all SDK requests use an in-process transport."""
import importlib
import json
import ssl
from types import SimpleNamespace as NS

import pytest

from vasp_agent.models.api_client import create_client
from vasp_agent.models.model_config import ModelConfig, resolve_config


def args(root, **overrides):
    return NS(**(dict(workspace=str(root), env_file=None, config=None, profile=None,
                      model=None, base_url=None, protocol=None, api_key_env=None,
                      network_mode=None) | overrides))


def write_profiles(root, mode='auto'):
    (root / 'models.toml').write_text(f'''default_profile = "relay"
[profiles.relay]
base_url = "https://relay.invalid/v1"
api_key_env = "TEST_KEY"
model = "test"
protocol = "responses"
network_mode = "{mode}"
[profiles.second]
base_url = "https://second.invalid/v1"
api_key_env = "TEST_KEY"
model = "test"
protocol = "responses"
network_mode = "auto"
''', encoding='utf-8')


def stack():
    openai = pytest.importorskip('openai')
    package = next(base.__module__.split('.')[0] for base in openai.DefaultHttpxClient.__mro__
                   if base.__module__.split('.')[0] in ('httpx', 'httpx2'))
    return openai, importlib.import_module(package)


def test_config_network_mode_precedence_and_redaction(tmp_path):
    write_profiles(tmp_path, 'direct')
    assert resolve_config(args(tmp_path), {}).network_mode == 'direct'
    (tmp_path / '.env').write_text('TEST_KEY=fake-secret\nVASP_AGENT_NETWORK_MODE=auto\n')
    assert resolve_config(args(tmp_path), {}).network_mode == 'auto'
    env = {'VASP_AGENT_NETWORK_MODE': 'direct', 'TEST_KEY': 'process-secret'}
    assert resolve_config(args(tmp_path), env).network_mode == 'direct'
    config = resolve_config(args(tmp_path, network_mode='auto'), env)
    assert config.network_mode == 'auto'
    assert config.public()['network_mode'] == 'auto'
    assert 'process-secret' not in json.dumps(config.public())
    assert 'process-secret' not in repr(config)


def test_default_network_mode_and_identity_unchanged(tmp_path):
    config = resolve_config(args(tmp_path, model='test'), {})
    assert config.network_mode == 'auto'
    direct = resolve_config(args(tmp_path, model='test', network_mode='direct'), {})
    assert direct.identity() == config.identity()
    # Existing positional construction remains compatible.
    assert ModelConfig('p', 'https://test.invalid', 'm', 'responses', 'K', 'fake').network_mode == 'auto'


@pytest.mark.parametrize('bad', ['proxy', '', False, 1, ['direct'], None])
def test_invalid_profile_network_mode_fails_closed(tmp_path, bad):
    write_profiles(tmp_path)
    text = (tmp_path / 'models.toml').read_text()
    if bad is None:
        value = '{ invalid = true }'
    else:
        value = json.dumps(bad)
    (tmp_path / 'models.toml').write_text(text.replace('network_mode = "auto"', 'network_mode = ' + value, 1))
    with pytest.raises(ValueError, match='network_mode'):
        resolve_config(args(tmp_path), {})


@pytest.mark.parametrize('source', ['cli', 'env', 'dotenv'])
def test_invalid_override_network_mode_fails_closed(tmp_path, source):
    write_profiles(tmp_path)
    options, env = args(tmp_path), {}
    if source == 'cli':
        options.network_mode = 'proxy'
    elif source == 'env':
        env['VASP_AGENT_NETWORK_MODE'] = ''
    else:
        (tmp_path / '.env').write_text('VASP_AGENT_NETWORK_MODE=proxy\n')
    with pytest.raises(ValueError, match='network_mode'):
        resolve_config(options, env)


def test_auto_client_preserves_existing_constructor_options(monkeypatch):
    openai, _ = stack()
    seen = []
    monkeypatch.setattr(openai, 'OpenAI', lambda **kwargs: seen.append(kwargs) or NS())
    create_client(NS(api_key='fake', base_url='https://example.invalid/v1'), 19)
    assert seen == [dict(api_key='fake', base_url='https://example.invalid/v1', timeout=19,
                         max_retries=0, default_headers={'Accept-Encoding': 'gzip, deflate'})]


def test_direct_client_disables_application_proxies_but_keeps_strict_ca_env(monkeypatch):
    openai, http = stack()
    for name in ('HTTP_PROXY', 'HTTPS_PROXY', 'ALL_PROXY'):
        monkeypatch.setenv(name, 'http://127.0.0.1:9')
    calls = []
    real_factory = http.create_ssl_context
    def record_factory(**kwargs):
        calls.append(kwargs)
        return real_factory(**kwargs)
    monkeypatch.setattr(http, 'create_ssl_context', record_factory)
    with create_client(NS(api_key='fake', base_url='https://example.invalid/v1', network_mode='direct'), 17) as client:
        transport = client._client
        assert transport._trust_env is False
        assert all(value is None for value in transport._mounts.values())
        context = transport._transport._pool._ssl_context
        assert context.verify_mode == ssl.CERT_REQUIRED and context.check_hostname is True
        assert transport._transport._pool._http2 is False
        assert client.timeout == 17 and client.max_retries == 0
        assert transport.timeout.connect == 17 and transport.timeout.read == 17
    assert calls[0] == {'verify': True, 'trust_env': True}


def test_direct_respects_invalid_ca_file_without_fallback(tmp_path, monkeypatch):
    stack()
    monkeypatch.setenv('SSL_CERT_FILE', str(tmp_path / 'missing-ca.pem'))
    with pytest.raises(FileNotFoundError):
        create_client(NS(api_key='fake', base_url='https://example.invalid', network_mode='direct'))


def test_direct_respects_ca_directory(tmp_path, monkeypatch):
    stack()
    monkeypatch.delenv('SSL_CERT_FILE', raising=False)
    monkeypatch.setenv('SSL_CERT_DIR', str(tmp_path))
    with create_client(NS(api_key='fake', base_url='https://example.invalid', network_mode='direct')) as client:
        context = client._client._transport._pool._ssl_context
        assert context.verify_mode == ssl.CERT_REQUIRED and context.check_hostname


def test_ssl_context_uses_installed_stack_helper_when_not_public(monkeypatch):
    from vasp_agent.models.api_client import _strict_ssl_context
    openai, http = stack()
    config = importlib.import_module(http.__name__ + '._config')
    calls = []
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    def factory(**kwargs):
        calls.append(kwargs)
        return context
    monkeypatch.delattr(http, 'create_ssl_context', raising=False)
    monkeypatch.setattr(config, 'create_ssl_context', factory)
    assert _strict_ssl_context(openai.DefaultHttpxClient) is context
    assert calls == [{'verify': True, 'trust_env': True}]


def test_unrecognized_http_stack_fails_closed():
    from vasp_agent.models.api_client import _strict_ssl_context
    class UnknownClient:
        pass
    with pytest.raises(ValueError, match='无法识别'):
        _strict_ssl_context(UnknownClient)


@pytest.mark.parametrize('disable', ['certificate', 'hostname'])
def test_direct_rejects_insecure_context(monkeypatch, disable):
    _, http = stack()
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    context.check_hostname = False
    if disable == 'certificate':
        context.verify_mode = ssl.CERT_NONE
    monkeypatch.setattr(http, 'create_ssl_context', lambda **kwargs: context)
    with pytest.raises(ValueError, match='验证证书'):
        create_client(NS(api_key='fake', base_url='https://example.invalid', network_mode='direct'))


def test_direct_constructor_failure_closes_its_client(monkeypatch):
    openai, _ = stack()
    created = []
    def reject(**kwargs):
        created.append(kwargs['http_client'])
        raise ValueError('constructor test')
    monkeypatch.setattr(openai, 'OpenAI', reject)
    with pytest.raises(ValueError, match='constructor test'):
        create_client(NS(api_key='fake', base_url='https://example.invalid', network_mode='direct'))
    assert len(created) == 1 and created[0].is_closed


def test_invalid_client_network_mode_never_creates_transport(monkeypatch):
    openai, _ = stack()
    def forbidden(**kwargs):
        pytest.fail('invalid network mode must not create a client')
    monkeypatch.setattr(openai, 'OpenAI', forbidden)
    monkeypatch.setattr(openai, 'DefaultHttpxClient', forbidden)
    with pytest.raises(ValueError, match='network_mode'):
        create_client(NS(api_key='fake', base_url='https://example.invalid', network_mode='proxy'))


def test_mock_sdk_request_body_auth_and_gzip_unchanged_between_modes(monkeypatch):
    import gzip
    openai, http = stack()
    base_client, base_constructor = openai.DefaultHttpxClient, openai.OpenAI
    seen, options = [], []
    def serve(request):
        seen.append(request)
        payload = {'id': 'resp_test', 'object': 'response', 'created_at': 0, 'model': 'test',
                   'status': 'completed', 'output': [{'type': 'message', 'id': 'msg_test',
                       'status': 'completed', 'role': 'assistant', 'content': [
                           {'type': 'output_text', 'text': 'probe-ok', 'annotations': []}]}]}
        return http.Response(200, headers={'content-type': 'application/json', 'content-encoding': 'gzip'},
                             content=gzip.compress(json.dumps(payload).encode()))
    class LocalClient(base_client):
        def __init__(self, **kwargs):
            options.append(dict(kwargs))
            super().__init__(**kwargs, transport=http.MockTransport(serve))
    def constructor(**kwargs):
        if 'http_client' not in kwargs:
            kwargs['http_client'] = LocalClient()
        return base_constructor(**kwargs)
    monkeypatch.setattr(openai, 'DefaultHttpxClient', LocalClient)
    monkeypatch.setattr(openai, 'OpenAI', constructor)
    for mode in ('auto', 'direct'):
        config = NS(api_key='fake-key', base_url='https://example.invalid/v1', network_mode=mode)
        with create_client(config, 23) as client:
            response = client.responses.create(model='test', instructions='fixed', input='synthetic', store=False)
            assert response.output_text == 'probe-ok'
    assert seen[0].content == seen[1].content
    assert seen[0].url == seen[1].url
    assert all(request.headers['authorization'] == 'Bearer fake-key' for request in seen)
    assert all(request.headers['accept-encoding'] == 'gzip, deflate' for request in seen)
    assert options[0] == {}
    assert options[1]['trust_env'] is False and options[1]['http2'] is False


def test_cli_profile_switch_does_not_inherit_cli_direct_mode(tmp_path, monkeypatch):
    from vasp_agent.chat import ai_agent
    from vasp_agent.ui.terminal_ui import TerminalUI
    from vasp_agent.chat import chat_commands

    write_profiles(tmp_path)
    (tmp_path / '.env').write_text('TEST_KEY=fake\n')
    monkeypatch.delenv('VASP_AGENT_NETWORK_MODE', raising=False)
    monkeypatch.delenv('VASP_AGENT_PROFILE', raising=False)
    clients, snapshots = [], []
    def factory(config, timeout):
        clients.append(config.network_mode)
        return NS(close=lambda: None)
    class FakeAgent:
        def __init__(self, client, model, toolbox, *a, **kw):
            self.history, self.usage, self.tools = [], {}, toolbox
    class FakeCommands:
        def __init__(self, agent, factory, config, name, ui, reader, runtime):
            self.agent, self.config, self.name, self.runtime = agent, config, name, runtime
            self.output = lambda _: None
        def handle(self, message):
            if message == '/exit':
                return 'exit'
            command, _, argument = message.partition(' ')
            self.runtime(self, command, argument)
            snapshots.append(self.config.network_mode)
            return 'handled'
    inputs = iter(['/profile second', '/model test', '/profile relay', '/exit'])
    monkeypatch.setattr('sys.argv', ['ai_agent.py', '--workspace', str(tmp_path), '--color', 'never',
                                     '--network-mode', 'direct'])
    monkeypatch.setattr('vasp_agent.models.api_client.create_client', factory)
    monkeypatch.setattr('vasp_agent.models.model_adapters.adapter_for', lambda *a: NS())
    monkeypatch.setattr('vasp_agent.models.model_catalog.list_models', lambda *a: ['test'])
    monkeypatch.setattr(ai_agent, 'Agent', FakeAgent)
    monkeypatch.setattr(chat_commands, 'Commands', FakeCommands)
    monkeypatch.setattr(TerminalUI, 'prompt', lambda self: next(inputs))
    ai_agent.main()
    assert clients == ['direct', 'auto', 'auto', 'auto']
    assert snapshots == ['auto', 'auto', 'auto']


def test_cli_help_describes_network_modes(monkeypatch, capsys):
    from vasp_agent.chat.ai_agent import main
    monkeypatch.setattr('sys.argv', ['ai_agent.py', '--help'])
    with pytest.raises(SystemExit) as caught:
        main()
    assert caught.value.code == 0
    text = capsys.readouterr().out
    assert '--network-mode {auto,direct}' in text and '仍验证证书' in text
