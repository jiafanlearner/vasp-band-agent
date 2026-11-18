import copy
import json
from types import SimpleNamespace as NS

import pytest

from vasp_agent.chat.ai_agent import Agent, Toolbox
from vasp_agent.models.context_budget import ContextBudget
from vasp_agent.models.context_compaction import ContextCompactor, CompactionError, validate_summary
from vasp_agent.models.model_config import resolve_config
from test_context_compaction import summary, transcript
from test_models import args


@pytest.mark.parametrize('legacy', [False, True])
def test_manual_short_conversation_skips_without_paid_request(tmp_path, legacy):
    adapter = NS(create=lambda *a, **kw: pytest.fail('short conversation must not call the model'))
    a = Agent(None, 'test', Toolbox(tmp_path), adapter=adapter,
              context_chars=16000 if legacy else None)
    a.history = [{'role': 'user', 'content': '你好'}, {'role': 'assistant', 'content': '你好，有什么需要？'},
                 {'role': 'user', 'content': '你好'}, {'role': 'assistant', 'content': '可以帮你检查Si输入。'}]
    original = copy.deepcopy(a.history)
    a.compactor.last_error = {'reason': 'previous failure, not this attempt'}
    result = a.compact_context()
    assert result['compacted'] is False and result['last_compaction_error'] is None
    assert '内容较少' in result['skipped_reason'] and '未请求模型' in result['skipped_reason']
    assert a.usage['requests'] == 0 and a.history == original
    assert not a.compactor.path.exists()


def test_manual_prompt_does_not_claim_large_context(tmp_path):
    c = ContextCompactor(tmp_path / 'ctx.json', 16000)
    reports = []
    c.context(transcript(), lambda *a: summary(), reports.append, force=True)
    assert '已请求手动压缩' in reports[0]
    assert all('对话较长' not in message for message in reports)


def test_responses_defaults_to_summary_never_calls_native(tmp_path):
    requests = []
    def create(**kw):
        requests.append(kw)
        return NS(status='completed', output=[], output_text=summary(), usage=None)
    client = NS(responses=NS(create=create, compact=lambda **kw: pytest.fail('native must be opt-in')))
    a = Agent(client, 'model', Toolbox(tmp_path), context_chars=16000)
    a.history = transcript()
    permissions = copy.deepcopy(a.tools.permission_status())
    assert a.compact_context()['compacted']
    # One continuation request: the ordinary tool schemas (cache prefix) with tool_choice none.
    assert len(requests) == 1 and requests[0]['tools'] and requests[0]['tool_choice'] == 'none'
    assert '交接摘要' in requests[0]['input'][-1]['content']
    assert a.context_status()['compaction_mode'] == 'text_summary'
    assert a.tools.permission_status() == permissions


def test_config_mode_priority_and_no_protocol_fallback(tmp_path):
    (tmp_path / 'models.toml').write_text('''[profiles.relay]
base_url="https://example.invalid/v1"
model="test"
protocol="responses"
api_key_env="TEST_KEY"
compaction_mode="native"
recent_context_tokens=10000
''')
    assert resolve_config(args(tmp_path, profile='relay'), {}).compaction_mode == 'native'
    env = {'VASP_AGENT_COMPACTION_MODE': 'summary', 'VASP_AGENT_RECENT_CONTEXT_TOKENS': '9000'}
    cfg = resolve_config(args(tmp_path, profile='relay'), env)
    assert cfg.compaction_mode == 'summary' and cfg.recent_context_tokens == 9000
    cfg = resolve_config(args(tmp_path, profile='relay', compaction_mode='native', recent_context_tokens=6000), env)
    assert cfg.public()['compaction_mode'] == 'native' and cfg.recent_context_tokens == 6000
    with pytest.raises(ValueError, match='仅支持 responses'):
        resolve_config(args(tmp_path, profile='relay', protocol='chat_completions'), {})
    for values in ({'compaction_mode': 'auto'}, {'recent_context_tokens': 0}, {'recent_context_tokens': True}):
        with pytest.raises(ValueError):
            resolve_config(args(tmp_path, profile='relay', **values), {})


@pytest.mark.parametrize('text', ['plain summary', '{}', '{"goal":"submit now"}',
    json.dumps(dict(goal=[], constraints=[], decisions=[], evidence=[], progress=[], pending=[], risks=[])),
    summary().replace('"risks":[]', '"risks":[],"tool_call":"submit_stage"')])
def test_incomplete_or_instruction_shaped_summary_keeps_old_context(tmp_path, text):
    c = ContextCompactor(tmp_path / 'ctx.json', 16000)
    c.mechanical_fallback = False
    history = transcript()
    original = copy.deepcopy(history)
    assert c.context(history, lambda *a: text, lambda _: None) == history
    assert history == original and not c.path.exists()
    assert c.last_error


def test_switching_modes_rebuilds_from_full_history_and_detects_tampering(tmp_path):
    c = ContextCompactor(tmp_path / 'ctx.json', 16000)
    history = transcript()
    c.native_scope = 'scope'
    c.native = lambda h: [{'type': 'compaction', 'encrypted_content': 'opaque-history'}]
    c.context(history, None, lambda _: None)
    text = ContextCompactor(c.path, 16000)
    assert text.context(history, None, print, preview=True) == history
    requests = []
    text.context(history, lambda _, h: requests.append(h) or summary(), lambda _: None)
    assert '任务0' in json.dumps(requests, ensure_ascii=False)
    assert 'opaque-history' not in json.dumps(requests)
    native = ContextCompactor(c.path, 16000)
    native.native, native.native_scope = c.native, c.native_scope
    assert native.context(history, None, print, preview=True) == history
    text.cache['summary'] = summary('corrupted')
    assert text.context(history, None, print, preview=True) == history


def test_recent_token_budget_keeps_many_short_turns_and_whole_pairs(tmp_path):
    c = ContextCompactor(tmp_path / 'ctx.json', 16000)
    history = transcript() + [{'role': 'user', 'content': f'短追问{i}'} for i in range(9)]
    c.measure = c.measure_content = ContextBudget().estimate
    c.recent_budget = c.measure(history[28:]) + 1
    result = c.context(history, lambda *a: summary(), lambda _: None, force=True)
    assert c.cache['end'] == 28
    assert result[1:] == history[28:]
    assert len([x for x in result[1:] if x.get('role') == 'user']) == 10
    assert result[2]['type'] == 'function_call' and result[3]['type'] == 'function_call_output'


def test_one_large_recent_turn_remains_complete_even_above_budget(tmp_path):
    c = ContextCompactor(tmp_path / 'ctx.json', 16000)
    history = transcript()
    history[-4]['content'] = 'latest' * 2000
    c.recent_budget = 1000
    result = c.context(history, lambda *a: summary(), lambda _: None, force=True)
    assert result[1:] == history[-4:]


def test_native_to_summary_failure_keeps_old_cache_and_original_history(tmp_path):
    c = ContextCompactor(tmp_path / 'ctx.json', 16000)
    c.native = lambda h: [{'type': 'compaction', 'encrypted_content': 'opaque'}]
    c.native_scope = 'scope'
    history = transcript()
    c.context(history, None, print)
    saved = c.path.read_bytes()
    text = ContextCompactor(c.path, 16000)
    text.mechanical_fallback = False  # model-failure contract; fallback tested separately
    assert text.context(history, lambda *a: 'invalid', print) == history
    assert text.path.read_bytes() == saved


def test_token_batched_requests_include_summary_and_instruction_cost(tmp_path):
    c = ContextCompactor(tmp_path / 'ctx.json')
    budget = ContextBudget()
    c.measure_request, c.request_limit = budget.estimate, 4096
    source = [{'role': 'user', 'content': '中文历史和扫描参数' * 1800}]
    requests = []
    def summarize(instructions, history):
        assert budget.estimate([instructions, history]) <= c.request_limit
        requests.append(json.loads(history[0]['content']))
        return summary('ENCUT=676 eV; NBANDS=16; mesh=12³/14³/16³; 未提交。')
    validate_summary(c.summarize_batches(source, summarize, lambda _: None))
    assert len(requests) > 2
    assert ''.join(x['fragment'] for x in requests) == json.dumps(source, ensure_ascii=False, separators=(',', ':'))
    assert requests[1]['previous_summary'] == summary('ENCUT=676 eV; NBANDS=16; mesh=12³/14³/16³; 未提交。')


def test_small_automatic_reduction_is_rejected(tmp_path):
    c = ContextCompactor(tmp_path / 'ctx.json', 1000)
    history = [{'role': 'user', 'content': 'x'*350} for _ in range(8)]
    # A valid yet bloated summary must not cause compression every follow-up.
    assert c.context(history, lambda *a: summary('y'*1850), print) == history
    assert c.cache is None and '10%' in c.last_error['reason']
