from types import SimpleNamespace as NS

import httpx
import pytest
from openai import APIStatusError

from vasp_agent.models.compaction_probe import (
    build_history, classify_error, estimate_input, main, parse_sizes, run_trial, summarize,
)


def test_synthetic_size_and_tool_pairing():
    history = build_history(8000)
    assert 7900 <= estimate_input(history) <= 8000
    assert history == build_history(8000)
    assert history != build_history(8000, seed=18)
    calls = {item['call_id'] for item in history if item.get('type') == 'function_call'}
    results = {item['call_id'] for item in history if item.get('type') == 'function_call_output'}
    assert calls and calls == results
    assert not any(item.get('type') in ('reasoning', 'compaction') for item in history)


def test_explicit_units_and_invalid_sizes():
    assert parse_sizes('8k,32000,8K') == [8000, 32000]
    with pytest.raises(Exception):
        parse_sizes('258x')


def error(status, code=None):
    response = httpx.Response(status, request=httpx.Request('POST', 'https://example.invalid'))
    return APIStatusError('secret provider text', response=response, body={'error': {'code': code}})


def test_rate_and_gateway_failures_are_not_capacity_limits():
    assert classify_error(error(429))['category'] == 'rate_limit'
    assert classify_error(error(502))['category'] == 'gateway_or_upstream'
    assert classify_error(error(400, 'context_length_exceeded'))['category'] == 'explicit_context_limit'
    assert classify_error(error(429, 'insufficient_quota'))['category'] == 'quota'
    assert 'secret' not in str(classify_error(error(502, 'secret code')))


def test_success_requires_valid_native_compaction_and_records_usage():
    calls = []
    def compact(**kwargs):
        calls.append(kwargs)
        return NS(object='response.compaction', usage=NS(input_tokens=4321, output_tokens=90),
                  output=[{'type': 'compaction', 'encrypted_content': 'opaque-private-state'}])
    row = run_trial(NS(responses=NS(compact=compact)), 'test', build_history(8000), 8000, 1)
    assert row['ok'] and row['actual_input_tokens'] == 4321
    assert set(calls[0]) == {'model', 'instructions', 'input'}
    assert 'opaque-private-state' not in str(row)
    client = NS(responses=NS(compact=lambda **kwargs: NS(object='response.compaction', output=[], usage=None)))
    assert not run_trial(client, 'test', [], 8000, 1)['ok']


def test_summary_does_not_promote_partial_or_transient_failures():
    rows = [dict(purpose='sweep', target_estimated_tokens=8000, ok=True, category='success',
                 elapsed_seconds=2, actual_input_tokens=6000),
            dict(purpose='sweep', target_estimated_tokens=32000, ok=False, category='rate_limit',
                 elapsed_seconds=3, actual_input_tokens=None)]
    report = summarize(rows, [8000, 32000], repeats=2)
    assert report['largest_all_passed_estimated_size'] is None
    assert report['smallest_explicit_limit_estimated_size'] is None
    assert report['transient_failures_observed']
    assert '未证实' in report['conclusion']


def configure_main(monkeypatch):
    config = NS(protocol='responses', api_key='fake', model='test', profile='relay',
                base_url='https://example.invalid/v1', context_window=240000,
                output_reserve=8000, context_safety=16000)
    monkeypatch.setattr('vasp_agent.models.model_config.resolve_config', lambda args: config)


def test_dry_run_makes_no_network_calls(tmp_path, monkeypatch):
    import json
    configure_main(monkeypatch)
    def forbidden(**kwargs):
        pytest.fail('dry-run must not construct a network client')
    monkeypatch.setattr('openai.OpenAI', forbidden)
    assert main(['--sizes', '8k', '--dry-run', '--output-dir', str(tmp_path)]) == 0
    report = json.loads((tmp_path / 'results.json').read_text(encoding='utf-8'))
    assert report['stop_reason'] == 'dry_run_no_network' and report['attempts'] == []


def test_small_control_failure_stops_larger_requests(tmp_path, monkeypatch):
    import json
    configure_main(monkeypatch)
    calls = []
    def compact(**kwargs):
        calls.append(kwargs)
        raise error(502)
    monkeypatch.setattr('openai.OpenAI', lambda **kwargs: NS(
        responses=NS(compact=compact), close=lambda: None))
    assert main(['--sizes', '8k,32k', '--repeats', '1', '--interval', '0',
                 '--cooldown', '0', '--output-dir', str(tmp_path)]) == 2
    report = json.loads((tmp_path / 'results.json').read_text(encoding='utf-8'))
    assert len(calls) == 2
    assert report['stop_reason'] == 'small_control_also_failed_capacity_not_established'
    assert report['attempts'][1]['purpose'] == 'small_control'
    assert report['summary']['tiers'][1]['trials'] == 0


def test_server_retry_after_over_budget_stops_without_wait(tmp_path, monkeypatch):
    import json
    configure_main(monkeypatch)
    exc = error(429)
    exc.response.headers['Retry-After'] = '600'
    def compact(**kwargs):
        raise exc
    monkeypatch.setattr('openai.OpenAI', lambda **kwargs: NS(
        responses=NS(compact=compact), close=lambda: None))
    monkeypatch.setattr('vasp_agent.models.compaction_probe.wait', lambda seconds: pytest.fail('must not wait'))
    assert main(['--sizes', '8k', '--repeats', '1', '--output-dir', str(tmp_path)]) == 2
    report = json.loads((tmp_path / 'results.json').read_text(encoding='utf-8'))
    assert report['stop_reason'] == 'server_wait_exceeds_cooldown_budget'
    assert len(report['attempts']) == 1


def test_exhausted_request_budget_does_not_wait_for_unaffordable_control(tmp_path, monkeypatch):
    import json
    configure_main(monkeypatch)
    def compact(**kwargs):
        raise error(502)
    monkeypatch.setattr('openai.OpenAI', lambda **kwargs: NS(
        responses=NS(compact=compact), close=lambda: None))
    monkeypatch.setattr('vasp_agent.models.compaction_probe.wait', lambda seconds: pytest.fail('budget exhausted'))
    assert main(['--sizes', '8k', '--repeats', '1', '--max-requests', '1',
                 '--output-dir', str(tmp_path)]) == 2
    report = json.loads((tmp_path / 'results.json').read_text(encoding='utf-8'))
    assert report['stop_reason'] == 'request_or_input_budget_reached'
