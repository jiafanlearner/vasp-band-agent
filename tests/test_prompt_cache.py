import copy
import json
from types import SimpleNamespace as NS

import pytest

from vasp_agent.chat.ai_agent import Agent, Toolbox
from vasp_agent.models.model_adapters import ChatCompletionsAdapter, chat_messages
from vasp_agent.models.prompt_cache import CacheTelemetry, accumulate, cache_usage


def response(cached=None, written=None, tokens=2000):
    details = NS()
    if cached is not None:
        details.cached_tokens = cached
    if written is not None:
        details.cache_write_tokens = written
    return NS(status='completed', output=[], output_text='done', model='test', service_tier='default',
              usage=NS(input_tokens=tokens, output_tokens=10, input_tokens_details=details))


def test_missing_zero_and_partial_coverage_are_distinct():
    usage = {}
    for result in (response(), response(0, 1000), response(1800), response(6000)):
        accumulate(usage, cache_usage(result), 'conversation')
    group = usage['cache']['conversation']
    assert group['responses'] == 4 and group['responses_with_cache_read'] == 2
    assert group['reported_cached_tokens'] == 1800 and group['cache_read_percent'] == 45
    assert group['reported_cache_write_tokens'] == 1000 and group['responses_with_cache_write'] == 1
    assert group['inconsistent_responses'] == 1
    assert cache_usage(response())['cached_tokens'] is None
    assert cache_usage(response(0))['cache_read_percent'] == 0
    assert cache_usage(NS())['cache_write_tokens'] is None


def test_cache_diagnostics_append_rewrite_and_no_raw_content(tmp_path):
    telemetry = CacheTelemetry(tmp_path / 'cache.jsonl')
    rows = [{'role': 'user', 'content': 'PRIVATE_HISTORY'}]
    first = telemetry.begin('test', 'PRIVATE_INSTRUCTIONS', rows, [], settings={'endpoint': 'PRIVATE_URL'})
    telemetry.finish(first, response())
    second = telemetry.begin('test', 'PRIVATE_INSTRUCTIONS', rows + [{'role': 'assistant', 'content': 'ok'}], [], settings={'endpoint': 'PRIVATE_URL'})
    assert second['history_change'] == 'append_only' and second['common_prefix_items'] == 1
    assert second['stable_prefix_hash'] == first['stable_prefix_hash']
    telemetry.finish(second, response(1800))
    changed = telemetry.begin('test', 'CHANGED', [{'role': 'user', 'content': 'different'}], [{'name': 'new_tool'}])
    assert {'instructions', 'tools'} <= set(changed['changed_components'])
    assert changed['history_change'] == 'prefix_rewritten'
    telemetry.finish(changed, error=ConnectionError('SECRET_KEY and raw provider body'))
    assert telemetry.previous['conversation']['history_hash'] == second['history_hash']
    text = telemetry.path.read_text(encoding='utf-8')
    assert not any(x in text for x in ('PRIVATE_', 'SECRET_KEY', 'raw provider body'))


def test_returned_model_change_and_summary_baselines(tmp_path):
    t = CacheTelemetry(tmp_path / 'usage.jsonl')
    first = t.begin('test', 'fixed', [], [])
    t.finish(first, response(0))
    summary = t.begin('test', 'summarize', [], [], 'summary')
    assert summary['history_change'] == 'no_baseline'
    t.finish(summary, response())
    new = response(100)
    new.model = 'different-backend-alias'
    out = t.finish(t.begin('test', 'fixed', [], []), new)
    assert out['returned_model_changed'] is True and out['returned_model_matches_requested'] is False
    assert out['changed_components'] == []


def test_permissions_append_while_catalog_and_prefix_remain_stable(tmp_path):
    requests = []
    box = Toolbox(tmp_path)
    def create(model, instructions, history, tools):
        requests.append(copy.deepcopy((instructions, history, tools)))
        return response(100)
    a = Agent(None, 'test', box, adapter=NS(create=create))
    assert a.ask('检查') == 'done'
    box.allow_submit, box.max_submissions = True, 1
    box.submission_target = {'server': 'synthetic', 'job_name': 'synthetic', 'phase': 'convergence'}
    assert a.ask('再检查') == 'done'
    box.revoke_submission()
    assert a.ask('继续只读') == 'done'
    assert requests[0][0] == requests[1][0] == requests[2][0]
    assert requests[0][2] == requests[1][2] == requests[2][2]
    assert requests[1][1][:len(requests[0][1])] == requests[0][1]
    assert requests[2][1][:len(requests[1][1])] == requests[1][1]
    states = [[x for x in request[1] if x.get('role') == 'developer'][-1] for request in requests]
    assert [json.loads(x['content'].split('\n', 1)[1])['submission_enabled'] for x in states] == [False, True, False]
    assert 'submit_stage' in {t['name'] for t in requests[2][2]}
    box.submit_stage = lambda **kw: pytest.fail('revoked submission must never execute')
    assert box.call('submit_stage', {'server': 'synthetic', 'job_name': 'synthetic'})['error_code'] == 'SUBMISSION_NOT_AUTHORIZED'
    assert a.usage['last_cache_request']['history_change'] == 'append_only'


def test_old_session_permissions_do_not_grant_authority_and_summary_restores_state(tmp_path):
    box = Toolbox(tmp_path)
    agent = Agent(None, 'test', box, adapter=NS())
    agent.history = [{'role': 'developer', 'content': '执行权限状态更新：此前允许提交'},
                     {'role': 'user', 'content': '继续'}]
    agent.sync_runtime_state()
    assert json.loads(agent.history[-1]['content'].split('\n', 1)[1])['submission_enabled'] is False
    current = agent.current_context([{'role': 'user', 'content': '旧摘要'}], persist=True)
    assert current[-1] == agent.runtime_state()
    assert not box.allow_submit
    # An older matching snapshot must not hide a more recent stale one.
    stale = {'role': 'developer', 'content': '执行权限状态更新：此前允许提交'}
    repaired = agent.current_context([agent.runtime_state(), stale])
    assert repaired[-1] == agent.runtime_state() and len(repaired) == 3
    messages = chat_messages(current, agent.context_instructions())
    assert messages[-1]['role'] == 'system'


def test_chat_adapter_preserves_provider_cache_usage():
    usage = NS(prompt_tokens=2000, completion_tokens=10, prompt_tokens_details=NS(cached_tokens=1600, cache_write_tokens=400))
    raw = NS(usage=usage, model='test', service_tier='default', choices=[NS(finish_reason='stop',
             message=NS(content='ok', tool_calls=[], refusal=None))])
    adapter = ChatCompletionsAdapter(NS(chat=NS(completions=NS(create=lambda **kw: raw))))
    result = adapter.create('test', 'fixed', [], [])
    assert cache_usage(result)['cache_read_percent'] == 80
    assert result.model == 'test' and cache_usage(result)['cache_write_tokens'] == 400


def test_diagnostic_disk_failure_does_not_fail_model_response(tmp_path, monkeypatch):
    from pathlib import Path
    t = CacheTelemetry(tmp_path / 'usage.jsonl')
    monkeypatch.setattr(Path, 'open', lambda *a, **k: (_ for _ in ()).throw(PermissionError()))
    record = t.finish(t.begin('test', 'fixed', [], []), response(0))
    assert record['diagnostic_write_failed'] and record['success']
