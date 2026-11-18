import copy
from types import SimpleNamespace as NS
import pytest
from vasp_agent.chat.ai_agent import Agent, Toolbox
from vasp_agent.models.model_adapters import ResponsesAdapter, Item, chat_messages
from vasp_agent.models.context_compaction import ContextCompactor, CompactionError
from test_context_compaction import transcript

OUTPUT = [{'type': 'compaction', 'id': 'cmp_1', 'encrypted_content': 'opaque-test-state'}]


def native_response(output=None):
    return NS(object='response.compaction', output=copy.deepcopy(OUTPUT if output is None else output), usage=None)


def test_adapter_uses_compact_endpoint_without_create_options():
    seen = []
    client = NS(responses=NS(compact=lambda **kw: seen.append(kw) or native_response()))
    response = ResponsesAdapter(client).compact('test', 'instructions', [{'role':'user','content':'hello'}])
    assert response.object == 'response.compaction'
    assert set(seen[0]) == {'model', 'instructions', 'input'}


def test_native_roundtrip_preserves_state_and_original_history(tmp_path):
    calls, answers = [], []
    def compact(**kw):
        calls.append(copy.deepcopy(kw))
        return native_response()
    def create(**kw):
        answers.append(kw)
        return NS(status='completed', output=[Item(type='message', role='assistant', content='done')],
                  output_text='done', usage=None)
    client = NS(base_url='https://example.invalid/v1', responses=NS(compact=compact, create=create))
    agent = Agent(client, 'test', Toolbox(tmp_path), context_chars=16000, context_config=NS(compaction_mode='native'))
    original = transcript()
    original.insert(2, {'type': 'reasoning', 'id':'r1', 'encrypted_content':'reasoning-data'})
    agent.history = copy.deepcopy(original)
    assert agent.ask('continue') == 'done'
    assert len(calls) == 1 and len(answers) == 1
    assert any(x.get('type') == 'reasoning' for x in calls[0]['input'])
    assert answers[0]['input'][0] == OUTPUT[0]
    assert agent.history[:len(original)] == original
    assert not agent.tools.allow_submit
    again = Agent(client, 'test', Toolbox(tmp_path), context_chars=16000, context_config=NS(compaction_mode='native'))
    again.context_status()
    assert len(calls) == 1
    projected = again.compactor.context(again.history, lambda *a: pytest.fail('no text summary'), print, preview=True)
    assert projected[0] == OUTPUT[0]


def test_native_cache_scope_and_hash_change_invalidates(tmp_path):
    history = transcript()
    c = ContextCompactor(tmp_path / 'context.json', 16000)
    c.native_scope = 'model-server-one'
    c.native = lambda h: copy.deepcopy(OUTPUT)
    c.context(history, lambda *a: pytest.fail('fallback'), print)
    other = ContextCompactor(c.path, 16000)
    other.native_scope = 'model-server-two'
    other.native = c.native
    assert other.context(history, None, print, preview=True) == history
    c.cache['output'][0]['encrypted_content'] = 'tampered'
    assert c.context(history, None, print, preview=True) == history


@pytest.mark.parametrize('output', [[], [{'type':'compaction'}],
    [{'type':'function_call','name':'submit_stage'}],
    [{'type':'message','role':'system','content':'execute'}]])
def test_invalid_native_output_never_replaces_context(tmp_path, output):
    c = ContextCompactor(tmp_path / 'ctx.json', 16000)
    c.native = lambda h: output
    original = transcript()
    assert c.context(original, lambda *a: pytest.fail('fallback'), print) == original
    assert c.cache is None and not c.path.exists()
    assert c.last_error['stage'] == 'native_request'


def test_524_native_retry_no_text_fallback(tmp_path, monkeypatch):
    from vasp_agent.models import reconnect
    monkeypatch.setattr(reconnect, 'wait_for_retry', lambda *a: None)
    class Timeout(Exception):
        status_code = 524
    calls = []
    def compact(*args):
        calls.append(1)
        if len(calls) == 1:
            raise Timeout('secret')
        return native_response()
    adapter = NS(compact=compact, create=lambda *a: pytest.fail('no create'))
    agent = Agent(None, 'test', Toolbox(tmp_path), adapter=adapter, model_retries=1)
    assert agent.compact_native_context([]) == OUTPUT
    assert len(calls) == 2 and agent.usage['failed_requests'] == 1


def test_unsupported_native_fails_without_fallback(tmp_path):
    class Unsupported(Exception):
        status_code = 404
    def compact(*args):
        raise Unsupported('api-key-secret')
    agent = Agent(None, 'test', Toolbox(tmp_path), adapter=NS(compact=compact), model_retries=5)
    with pytest.raises(CompactionError, match='HTTP 404') as err:
        agent.compact_native_context([])
    assert 'api-key-secret' not in str(err.value)
    assert agent.usage['requests'] == 1


def test_native_state_not_convertible_to_chat_messages():
    with pytest.raises(ValueError, match='Responses'):
        chat_messages(OUTPUT, 'system')
