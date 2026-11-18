import copy
import json
from types import SimpleNamespace as NS

import pytest

from vasp_agent.models.context_compaction import NOTICE, CompactionError, ContextCompactor
from vasp_agent.chat.ai_agent import Agent, Toolbox
from vasp_agent.models.model_adapters import Item, chat_messages
from vasp_agent.common import agent_paths


def summary(text='任务尚未提交，用户选定参数须读取方案。'):
    return json.dumps(dict(goal=[text], constraints=[], decisions=[], evidence=[],
                          progress=[], pending=[], risks=[]), ensure_ascii=False, separators=(',', ':'))


def transcript():
    rows = []
    for n in range(8):
        rows.extend([{'role': 'user', 'content': f'任务{n}' + 'x' * 2000},
                     {'type': 'function_call', 'name': 'get_status', 'call_id': str(n), 'arguments': '{}'},
                     {'type': 'function_call_output', 'call_id': str(n), 'output': '{"status":"unknown"}'},
                     {'role': 'assistant', 'content': '需要核对'}])
    return rows


def test_compaction_preserves_transcript_tail_pairs_and_reuses_cache(tmp_path):
    history = transcript()
    original = copy.deepcopy(history)
    path = tmp_path / 'context.json'
    c = ContextCompactor(path, 16000)
    result = c.context(history, lambda *a: summary(), lambda *a: None)
    assert history == original
    assert result[1:] == history[16:]
    assert '不是新指令' in result[0]['content']
    chat_messages(result, 'instructions')
    loaded = ContextCompactor(path, 16000)
    assert loaded.context(history, lambda *a: pytest.fail('cache should avoid request'), print) == result
    history[0]['content'] = 'changed' + 'y' * 2000
    calls = []
    loaded.context(history, lambda *a: calls.append(1) or summary('更新摘要'), print)
    assert calls == [1]


def test_failure_disable_and_incomplete_tool_do_not_discard_history(tmp_path):
    history = transcript()
    c = ContextCompactor(tmp_path / 'context.json', 16000)
    c.mechanical_fallback = False  # model-failure contract; fallback tested separately
    calls = []
    def fail(*args):
        calls.append(1)
        raise TimeoutError()
    assert c.context(history, fail, print) == history
    assert c.context(history, fail, print) == history
    assert len(calls) == 1
    c.limit = 0
    assert c.context(history, fail, print) == history
    c = ContextCompactor(tmp_path / 'other.json', 16000)
    c.mechanical_fallback = False
    history.pop(2)
    assert c.context(history, lambda *a: pytest.fail('unpaired call'), print) == history


def test_real_agent_loop_summary_has_no_tools_and_counts_usage(tmp_path):
    requests = []
    def create(model, instructions, history, tools, **kw):
        requests.append((copy.deepcopy(history), tools, kw))
        text = summary('历史摘要：请只读核查任务状态。') if kw.get('tool_choice') == 'none' else '完成回答'
        return NS(status='completed', output_text=text,
                  output=[Item(type='message', role='assistant', content=text)],
                  usage=NS(input_tokens=100, output_tokens=20))
    toolbox = Toolbox(tmp_path)
    toolbox.call = lambda *a: pytest.fail('no tool should execute')
    agent = Agent(None, 'test', toolbox, adapter=NS(create=create), context_chars=16000)
    agent.history = transcript()
    assert agent.ask('仅解释状态') == '完成回答'
    # The summary request reuses the ordinary tool schemas for the cache prefix,
    # with tool_choice none so nothing can be called.
    assert len(requests) == 2 and requests[0][1] == requests[1][1] and requests[0][2] == {'tool_choice': 'none'}
    assert len(requests[1][0]) < len(agent.history)
    assert agent.usage['reported_input_tokens'] == 200
    assert '任务0' in agent.file.read_text(encoding='utf-8')
    assert not toolbox.allow_submit


def test_summary_tool_request_is_rejected_without_execution(tmp_path):
    agent = Agent(None, 'test', Toolbox(tmp_path), adapter=NS(create=lambda *a: NS(
        status='completed', output_text='摘要', usage=None,
        output=[Item(type='function_call', name='submit_stage')])) )
    with pytest.raises(ValueError, match='工具调用'):
        agent.summarize_context('summary', [])


def test_large_recent_turns_adapt_and_do_not_summarize_every_message(tmp_path):
    history = [{'role': 'user', 'content': str(i) + 'x' * 9000} for i in range(8)]
    calls = []
    summarize = lambda *a: calls.append(1) or summary('历史摘要')
    path = tmp_path / 'cadence.json'
    c = ContextCompactor(path, 16000)
    result = c.context(history, summarize, lambda *a: None)
    assert result[1:] == history[-2:]
    assert len(calls) == 4
    # Still above threshold, but a small follow-up must not trigger another call.
    history.append({'role': 'user', 'content': '继续'})
    c.context(history, summarize, lambda *a: None)
    assert len(calls) == 4
    # Cooldown survives process restarts via the validated summary cache.
    c = ContextCompactor(path, 16000)
    history.append({'role': 'user', 'content': '状态呢'})
    c.context(history, summarize, lambda *a: None)
    assert len(calls) == 4
    # Substantial new input can bypass the cooldown.
    history.append({'role': 'user', 'content': 'new' * 5000})
    c.context(history, summarize, lambda *a: None)
    assert len(calls) > 4


def test_failed_summary_not_retried_each_small_followup(tmp_path):
    history = transcript()
    calls = []
    def fail(*a):
        calls.append(1)
        raise TimeoutError()
    c = ContextCompactor(tmp_path / 'failure.json', 16000)
    c.context(history, fail, lambda *a: None)
    history.append({'role': 'user', 'content': '继续'})
    c.context(history, fail, lambda *a: None)
    assert len(calls) == 1


def test_summary_reconnect_counts_attempts_and_never_calls_tools(tmp_path, monkeypatch):
    from vasp_agent.models import reconnect
    waits, requests = [], []
    monkeypatch.setattr(reconnect, 'wait_for_retry', lambda delay, event: waits.append(delay))
    def create(model, instructions, history, tools):
        requests.append(tools)
        if len(requests) < 3:
            raise TimeoutError('secret-provider-body')
        return NS(status='completed', output=[], output_text='安全摘要', usage=None)
    agent = Agent(None, 'test', Toolbox(tmp_path), adapter=NS(create=create), model_retries=3)
    assert agent.summarize_context('summary', []) == '安全摘要'
    assert waits == [5, 10] and requests == [[], [], []]
    assert agent.usage['requests'] == 3 and agent.usage['failed_requests'] == 2


def test_terminal_failure_saved_without_provider_body(tmp_path, monkeypatch):
    from vasp_agent.models import reconnect
    monkeypatch.setattr(reconnect, 'wait_for_retry', lambda *a: None)
    def create(*a, **kw):
        raise ConnectionError('Bearer SECRET private user message')
    agent = Agent(None, 'test', Toolbox(tmp_path), adapter=NS(create=create), model_retries=2, context_chars=16000)
    agent.compactor.mechanical_fallback = False
    history = transcript()
    reports = []
    assert agent.compactor.context(history, agent.summarize_context, reports.append) == history
    assert agent.usage['requests'] == 3
    assert '连接异常' in reports[-1]
    raw = agent.compactor.path.with_suffix('.errors.jsonl').read_text(encoding='utf-8')
    assert 'SECRET' not in raw and 'private user message' not in raw
    assert agent.compactor.last_error['stage'] == 'summary_request'


@pytest.mark.parametrize('text,reason', [('', '摘要为空'), ('x'*12001, '没有JSON对象')])
def test_invalid_summary_diagnostic(tmp_path, text, reason):
    c = ContextCompactor(tmp_path / 'ctx.json', 16000)
    c.mechanical_fallback = False
    reports = []
    history = transcript()
    assert c.context(history, lambda *a: text, reports.append) == history
    assert reason in reports[-1]
    assert c.last_error['elapsed_seconds'] >= 0


def test_cache_failure_distinct_and_old_cache_preserved(tmp_path, monkeypatch):
    from pathlib import Path
    c = ContextCompactor(tmp_path / 'ctx.json', 16000)
    original = Path.replace
    def replace(path, target):
        if path == c.path.with_suffix('.tmp'):
            raise PermissionError('sensitive path')
        return original(path, target)
    monkeypatch.setattr(Path, 'replace', replace)
    history = transcript()
    assert c.context(history, lambda *a: summary('摘要'), lambda *a: None) == history
    assert c.cache is None and c.last_error['stage'] == 'cache_write'
    assert '缓存写入失败' in c.last_error['reason']


def test_nonretryable_summary_error_and_disabled_retries(tmp_path, monkeypatch):
    from vasp_agent.models import reconnect
    monkeypatch.setattr(reconnect, 'wait_for_retry', lambda *a: pytest.fail('must not wait'))
    class Denied(Exception):
        status_code = 403
    for error, retries in [(Denied('secret'), 5), (TimeoutError(), 0)]:
        def create(*a):
            raise error
        agent = Agent(None, 'test', Toolbox(tmp_path), adapter=NS(create=create), model_retries=retries)
        with pytest.raises(type(error)):
            agent.summarize_context('summary', [])
        assert agent.usage['requests'] == 1


def test_cancel_summary_wait_propagates_without_retrying_tools(tmp_path, monkeypatch):
    from vasp_agent.models import reconnect
    def wait(*a):
        raise KeyboardInterrupt()
    monkeypatch.setattr(reconnect, 'wait_for_retry', wait)
    def create(*a):
        raise TimeoutError()
    agent = Agent(None, 'test', Toolbox(tmp_path), adapter=NS(create=create), model_retries=2)
    with pytest.raises(KeyboardInterrupt):
        agent.summarize_context('summary', [])
    assert agent.usage['requests'] == 1


def test_batches_resume_after_restart_and_changed_input_invalidates(tmp_path):
    import json
    from vasp_agent.models.context_compaction import encoded
    source = [{'role': 'user', 'content': '中文历史' * 13000}]
    c = ContextCompactor(tmp_path / 'ctx.json')
    calls = []
    def fail_second(instructions, history):
        calls.append(history[0]['content'])
        if len(calls) == 2:
            raise TimeoutError()
        return summary('首批摘要')
    with pytest.raises(TimeoutError):
        c.summarize_batches(source, fail_second, lambda _: None)
    resumed = ContextCompactor(c.path)
    reports, resumed_calls = [], []
    def summarize(instructions, history):
        data = json.loads(history[0]['content'])
        assert len(data['fragment']) <= 18000
        assert len(data['previous_summary']) <= 12000
        resumed_calls.append(data)
        return summary('累积摘要')
    resumed.summarize_batches(source, summarize, reports.append)
    assert '复用' in reports[0]
    assert resumed_calls[0]['previous_summary'] == summary('首批摘要')
    assert len(resumed_calls) == (len(encoded(source)) + 17999) // 18000 - 1
    changed = [{'role': 'user', 'content': '不同' + source[0]['content']}]
    reports.clear()
    resumed.summarize_batches(changed, summarize, reports.append)
    assert '正在生成' in reports[0]


def test_batch_failure_does_not_replace_final_context(tmp_path):
    c = ContextCompactor(tmp_path / 'ctx.json', 16000)
    c.mechanical_fallback = False
    history = [{'role': 'user', 'content': 'x' * 10000} for _ in range(9)]
    original = copy.deepcopy(history)
    calls = []
    def summarize(*a):
        calls.append(1)
        if len(calls) > 1:
            raise ConnectionError()
        return summary('部分摘要')
    assert c.context(history, summarize, lambda _: None) == history
    assert history == original and c.cache is None
    assert not c.path.exists() and c.path.with_suffix('.parts.json').exists()


def test_http524_retry_delay_and_budget():
    from vasp_agent.models.reconnect import retry_delay
    class GatewayTimeout(Exception):
        status_code = 524
    assert retry_delay(GatewayTimeout(), 0, 300) == 5
    assert retry_delay(GatewayTimeout(), 0, 4) is None


def _summary_response(status='completed', text='', reasoning=0, reason=None):
    from types import SimpleNamespace as NS
    return NS(status=status, output_text=text, output=[NS(type='reasoning')] + ([NS(type='message')] if text else []),
              incomplete_details=NS(reason=reason) if reason else None,
              usage=NS(input_tokens=100, output_tokens=8000 if reason else 50,
                       output_tokens_details=NS(reasoning_tokens=reasoning)))


def _summary_agent(tmp_path, replies):
    from types import SimpleNamespace as NS
    import vasp_agent.chat.ai_agent as ai
    calls = []
    class Adapter:
        def create(self, model, instructions, history, tools, probe=False, reasoning=None):
            calls.append(reasoning)
            return replies.pop(0)
    agent = ai.Agent(NS(responses=None), 'test', ai.Toolbox(tmp_path), adapter=Adapter(), ui=NS())
    return agent, calls


def test_summary_spent_entirely_on_reasoning_retries_once_without_it(tmp_path):
    agent, calls = _summary_agent(tmp_path, [
        _summary_response('incomplete', '', 8000, 'max_output_tokens'), _summary_response(text='摘要正文')])
    assert agent.summarize_context('总结', [{'role': 'user', 'content': 'x'}]) == '摘要正文'
    assert calls == [None, 'none'] and agent.summary_reasoning == 'none'


def test_truncated_summary_with_text_reports_tokens_and_does_not_retry(tmp_path):
    from vasp_agent.models.context_compaction import CompactionError
    agent, calls = _summary_agent(tmp_path, [_summary_response('incomplete', '部分', 3000, 'max_output_tokens')])
    with pytest.raises(CompactionError, match=r'max_output_tokens；输出 8000 tokens，其中推理 3000'):
        agent.summarize_context('总结', [{'role': 'user', 'content': 'x'}])
    assert calls == [None]


def test_summary_reasoning_effort_is_validated_and_sent(monkeypatch, tmp_path):
    from types import SimpleNamespace as NS
    from vasp_agent.models.model_config import resolve_config
    from vasp_agent.models.model_adapters import ResponsesAdapter
    (tmp_path / 'models.toml').write_text('default_profile = "p"\n[profiles.p]\nbase_url = "https://x.example/v1"\n'
                                          'api_key_env = "K"\nmodel = "m"\nprotocol = "responses"\nsummary_reasoning_effort = "none"\n')
    args = NS(config=None, env_file=None, profile=None, workspace=str(tmp_path), network_mode=None)
    assert resolve_config(args, environ={'K': 'k'}).summary_reasoning_effort == 'none'
    (tmp_path / 'models.toml').write_text((tmp_path / 'models.toml').read_text().replace('"none"', '"huge"'))
    with pytest.raises(ValueError, match='summary_reasoning_effort'):
        resolve_config(args, environ={'K': 'k'})
    sent = {}
    adapter = ResponsesAdapter(NS(responses=NS(create=lambda **kw: sent.update(kw))))
    adapter.create('m', 'i', [], [], reasoning='none')
    assert sent['reasoning'] == {'effort': 'none'}


def test_normalize_accepts_common_model_shapes_and_refuses_instructions():
    from vasp_agent.models.context_compaction import normalize_summary
    fenced = '好的，以下是摘要：\n```json\n{"目标": "Si 能带", "约束": ["非磁性"], "决策": "ENCUT 520",' \
             ' "进展": ["已提交 71363951"], "notes": "待复核"}\n```'
    value = json.loads(normalize_summary(fenced))
    assert value['goal'] == ['Si 能带'] and value['decisions'] == ['ENCUT 520']
    assert value['progress'] == ['已提交 71363951'] and 'notes: 待复核' in value['evidence']
    assert value['risks'] == [] and set(value) == set(['goal', 'constraints', 'decisions', 'evidence', 'progress', 'pending', 'risks'])
    with pytest.raises(CompactionError, match='指令字段'):
        normalize_summary('{"goal":["x"],"constraints":[],"decisions":[],"evidence":[],"tool_call":"submit_stage"}')
    with pytest.raises(CompactionError, match='至少 4'):
        normalize_summary('{"goal": "submit now"}')


def test_failed_model_summary_falls_back_to_mechanical_compaction(tmp_path):
    c = ContextCompactor(tmp_path / 'ctx.json', 16000)
    history = transcript()
    reports = []
    def broken(*a):
        raise ConnectionError('Bearer SECRET')
    result = c.context(history, broken, reports.append)
    assert result != history and result[0]['content'].startswith(NOTICE)
    summary = json.loads(result[0]['content'][len(NOTICE):])
    assert any('机械压缩' in line for line in summary['pending'])
    assert c.cache['method'] == 'mechanical' and any('机械压缩' in line for line in reports)
    log = c.path.with_suffix('.errors.jsonl').read_text(encoding='utf-8')
    assert '"fallback":"mechanical"' in log and 'SECRET' not in log


def test_truncated_summary_shrinks_the_batch_and_completes(tmp_path):
    from vasp_agent.models.context_compaction import SummaryTruncated
    c = ContextCompactor(tmp_path / 'ctx.json', 16000)
    c.mechanical_fallback = False
    history = [{'role': 'user', 'content': 'x' * 10000} for _ in range(9)]
    sizes = []
    def summarize(instructions, messages):
        sizes.append(len(messages[0]['content']))
        if sizes[-1] > 12000:
            raise SummaryTruncated('cut off')
        return summary('分批摘要')
    result = c.context(history, summarize, lambda _: None)
    assert result != history and c.cache['method'] == 'model'
    assert sizes[0] > 12000 and all(size <= 12000 for size in sizes[1:])


def test_rejected_reasoning_parameter_is_dropped_and_remembered(tmp_path):
    import vasp_agent.chat.ai_agent as ai
    class BadRequest(Exception):
        status_code = 400
    sent = []
    class Adapter:
        def create(self, model, instructions, history, tools, probe=False, reasoning=None):
            sent.append(reasoning)
            if reasoning:
                raise BadRequest()
            return NS(status='completed', output_text=summary('摘要'), output=[NS(type='message')],
                      incomplete_details=None, usage=None)
    config = NS(identity=lambda: 'endpoint-a', summary_reasoning_effort='none', recent_context_tokens=12000,
                context_window=65536, output_reserve=4000, context_safety=2048, compact_trigger=.8,
                compact_target=.55, compaction_mode='summary')
    agent = ai.Agent(None, 'test', ai.Toolbox(tmp_path), adapter=Adapter(), ui=NS(), context_config=config)
    assert agent.summarize_context('总结', [{'role': 'user', 'content': 'x'}])
    assert sent == ['none', None] and agent.summary_reasoning is None
    stored = json.loads(agent_paths.file_path(tmp_path, 'model_capabilities').read_text(encoding='utf-8'))
    assert stored['endpoint-a']['summary_reasoning'] == 'unsupported'


CONTINUATION = ('<analysis>按时间梳理……</analysis>\n<summary>\n## 1. 主要请求与意图\n- 用户要求计算 Si 的 PBE 能带，'
                'convergence 已提交 job 71363951。\n## 8. 当前工作\n- 等待 relaxation 审阅点。\n## 9. 下一步\n- 等待用户指示。\n</summary>')


def test_continuation_summary_is_first_choice_and_survives_reload(tmp_path):
    from vasp_agent.models.context_compaction import CONTINUATION_FORMAT
    c = ContextCompactor(tmp_path / 'ctx.json', 16000)
    seen = []
    c.conversation = lambda items, focus: seen.append((len(items), focus)) or CONTINUATION
    result = c.context(transcript(), lambda *a: pytest.fail('batched path not needed'), lambda _: None)
    assert c.cache['format'] == CONTINUATION_FORMAT and c.cache['method'] == 'conversation'
    assert result[0]['content'].startswith(NOTICE) and '## 8. 当前工作' in result[0]['content']
    assert '<analysis>' not in result[0]['content'] and seen[0][1] is None
    again = ContextCompactor(c.path, 16000)
    assert again.context(transcript(), None, lambda _: None, preview=True)[0] == result[0]


def test_continuation_content_failure_falls_back_to_batches_but_transport_does_not(tmp_path):
    c = ContextCompactor(tmp_path / 'ctx.json', 16000)
    c.conversation = lambda items, focus: '太短'
    c.context(transcript(), lambda *a: summary('分批摘要'), lambda _: None)
    assert c.cache['method'] == 'model'
    d = ContextCompactor(tmp_path / 'other.json', 16000)
    def offline(items, focus):
        raise ConnectionError('down')
    d.conversation = offline
    d.context(transcript(), lambda *a: pytest.fail('no batched retry on transport failure'), lambda _: None)
    assert d.cache['method'] == 'mechanical'


def test_rejected_tool_choice_drops_tools_and_is_remembered(tmp_path):
    import vasp_agent.chat.ai_agent as ai
    class BadRequest(Exception):
        status_code = 400
    sent = []
    class Adapter:
        def create(self, model, instructions, history, tools, probe=False, reasoning=None, tool_choice=None):
            sent.append((bool(tools), tool_choice))
            if tool_choice:
                raise BadRequest()
            return NS(status='completed', output_text=CONTINUATION, output=[NS(type='message')],
                      incomplete_details=None, usage=None)
    config = NS(identity=lambda: 'endpoint-b', summary_reasoning_effort=None, recent_context_tokens=12000,
                context_window=65536, output_reserve=4000, context_safety=2048, compact_trigger=.8,
                compact_target=.55, compaction_mode='summary')
    agent = ai.Agent(None, 'test', ai.Toolbox(tmp_path), adapter=Adapter(), ui=NS(), context_config=config)
    assert '## 8. 当前工作' in agent.summarize_conversation(transcript(), focus='重点保留 job_id')
    assert sent == [(True, 'none'), (False, None)]
    assert agent.capability('tool_choice_none') == 'unsupported'
    sent.clear()
    agent.summarize_conversation(transcript())
    assert sent == [(False, None)]


def test_compact_focus_reaches_the_request(tmp_path):
    seen = []
    agent = Agent(None, 'test', Toolbox(tmp_path), context_chars=16000)
    agent.history = transcript()
    agent.compactor.conversation = lambda items, focus: seen.append(focus) or CONTINUATION
    agent.compact_context('重点保留 MoS2 的网格选择')
    assert seen == ['重点保留 MoS2 的网格选择'] and agent.compactor.focus is None


@pytest.mark.parametrize('reply', [
    CONTINUATION,
    CONTINUATION.replace('</summary>', ''),
    CONTINUATION.replace('<summary>', '').replace('</summary>', ''),
    CONTINUATION.replace('</analysis>', '').replace('<summary>', '').replace('</summary>', '').replace('<analysis>', '<analysis>只有分析') + '',
])
def test_continuation_extraction_tolerates_missing_tags(reply):
    from vasp_agent.models.context_compaction import extract_continuation
    if '<analysis>只有分析' in reply:
        with pytest.raises(CompactionError):
            extract_continuation(reply)  # nothing outside an unclosed analysis block
        return
    text = extract_continuation(reply)
    assert '## 8. 当前工作' in text and '<analysis>' not in text and '按时间梳理' not in text and '</summary>' not in text


def test_large_tool_output_keeps_nested_identifiers(tmp_path):
    big = {'ok': True, 'result': {'manifest.json': {'blob': 'x' * 9000},
           'stage_submissions': {p: {'job_id': str(71363951 + i), 'status': 'submitted'}
                                 for i, p in enumerate(('convergence', 'relaxation', 'scf', 'bands'))},
           'workflow-state.json': {'status': 'complete', 'next': None},
           **{f'file-{i}.json': {'text': 'y' * 700} for i in range(12)}}}
    c = ContextCompactor(tmp_path / 'ctx.json', 16000)
    history = [{'type': 'function_call', 'call_id': 'a', 'name': 'remote_status'},
               {'type': 'function_call_output', 'call_id': 'a', 'output': json.dumps(big)}]
    shown = json.loads(c.project(history)[1]['output'])['summary']
    assert shown['preview_only']
    assert shown['identifiers']['result.stage_submissions.relaxation.job_id'] == '71363952'
    assert shown['identifiers']['result.workflow-state.json.status'] == 'complete'
