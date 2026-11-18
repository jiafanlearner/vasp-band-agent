import json
from types import SimpleNamespace as NS

import pytest

from vasp_agent.models.model_config import resolve_config
from vasp_agent.models.model_adapters import ChatCompletionsAdapter, test_connection as probe_connection
from vasp_agent.chat.ai_agent import Agent, Toolbox
from vasp_agent.common import agent_paths


def args(root, **kwargs):
    return NS(**(dict(workspace=str(root), env_file=None, config=None, profile=None,
                      model=None, base_url=None, protocol=None, api_key_env=None) | kwargs))


@pytest.fixture
def configured(tmp_path):
    (tmp_path / 'models.toml').write_text('''default_profile = "relay"
[profiles.relay]
base_url = "https://relay.example/v1"
api_key_env = "RELAY_API_KEY"
model = "file-model"
protocol = "chat_completions"
''')
    (tmp_path / '.env').write_text('RELAY_API_KEY=private-test-value\nVASP_AGENT_MODEL=dotenv-model\n')
    return tmp_path


def test_precedence_and_redaction(configured):
    assert resolve_config(args(configured), {}).model == 'dotenv-model'
    assert resolve_config(args(configured), {'VASP_AGENT_MODEL': 'env-model'}).model == 'env-model'
    cfg = resolve_config(args(configured, model='cli-model'), {'VASP_AGENT_MODEL': 'env-model', 'RELAY_API_KEY': 'process-key'})
    assert cfg.model == 'cli-model' and cfg.api_key == 'process-key'
    assert 'process-key' not in repr(cfg)
    assert 'process-key' not in json.dumps(cfg.public())
    assert cfg.protocol == 'chat_completions'


def test_unknown_profile_no_fallback(configured):
    with pytest.raises(ValueError, match='profile'):
        resolve_config(args(configured, profile='missing'), {})


def test_explicit_protocol_and_endpoint(configured):
    with pytest.raises(ValueError, match='protocol'):
        resolve_config(args(configured, protocol='auto'), {})
    with pytest.raises(ValueError, match='base_url'):
        resolve_config(args(configured, base_url='https://user:password@relay.example/v1'), {})


def test_no_env_interpolation(configured):
    (configured / '.env').write_text('RELAY_API_KEY="${DO_NOT_EXPAND}"\n')
    cfg = resolve_config(args(configured), {'DO_NOT_EXPAND': 'secret'})
    assert cfg.api_key == '${DO_NOT_EXPAND}'


def test_identity_changes(configured):
    first = resolve_config(args(configured), {})
    second = resolve_config(args(configured, model='other'), {})
    assert first.identity() != second.identity()


def chat_reply(calls=None, text=None, reason='stop'):
    return NS(choices=[NS(message=NS(content=text, tool_calls=calls, refusal=None), finish_reason=reason)])


def call(name='list_files', arguments='{}', id='call-1'):
    return NS(id=id, type='function', function=NS(name=name, arguments=arguments))


class Chat:
    def __init__(self, replies):
        self.replies = iter(replies)
        self.requests = []

    def create(self, **kwargs):
        self.requests.append(kwargs)
        return next(self.replies)


def test_chat_agent_roundtrip(tmp_path):
    chat = Chat([chat_reply([call()], reason='tool_calls'), chat_reply(text='已检查')])
    client = NS(chat=NS(completions=chat))
    agent = Agent(client, 'test', Toolbox(tmp_path), adapter=ChatCompletionsAdapter(client))
    assert agent.ask('检查文件') == '已检查'
    messages = chat.requests[1]['messages']
    assert messages[-2]['tool_calls'][0]['id'] == 'call-1'
    assert messages[-1]['role'] == 'tool' and messages[-1]['tool_call_id'] == 'call-1'
    assert 'list_files' in [tool['function']['name'] for tool in chat.requests[0]['tools']]


def test_connection_probe_passes_without_vasp_tools():
    chat = Chat([chat_reply([call('connection_probe', '{"ping":"vasp-agent-probe"}')], reason='tool_calls'),
                 chat_reply(text='probe-ok')])
    adapter = ChatCompletionsAdapter(NS(chat=NS(completions=chat)))
    assert probe_connection(adapter, 'test')['tool_result_roundtrip'] == 'ok'
    assert len(chat.requests[0]['tools']) == 1
    assert chat.requests[0]['tools'][0]['function']['name'] == 'connection_probe'


def test_chat_only_provider_fails_probe():
    chat = Chat([chat_reply(text='I can help')])
    with pytest.raises(ValueError, match='工具调用测试'):
        probe_connection(ChatCompletionsAdapter(NS(chat=NS(completions=chat))), 'test')


def test_truncated_tool_call_never_executes(tmp_path):
    chat = Chat([chat_reply([call()], reason='length')])
    client = NS(chat=NS(completions=chat))
    agent = Agent(client, 'test', Toolbox(tmp_path), adapter=ChatCompletionsAdapter(client))
    assert '未完成' in agent.ask('测试')
    assert not agent_paths.audit_file(tmp_path).exists()
