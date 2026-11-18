import copy
import json
from contextlib import contextmanager
from types import SimpleNamespace as NS

import pytest

from vasp_agent.models.cache_probe import INSTRUCTIONS, TOOLS, run_probe
from vasp_agent.models.cache_probe_transport import ProbeTransport
from vasp_agent.models.model_adapters import Item, adapter_for


def fixture_client(calls, streaming=False, error=None):
    raw = {'id': 'resp_test_123', 'usage': {'input_tokens': 2500, 'input_tokens_details': {'cached_tokens': 0}}}
    result = NS(status='completed', output_text='cache-probe-ok', usage=NS(input_tokens=2500,
        input_tokens_details=NS(cached_tokens=0)), output=[Item(type='message', role='assistant',
        content=[{'type': 'output_text', 'text': 'cache-probe-ok'}])], model_dump=lambda **kw: copy.deepcopy(raw))
    def create(**kw):
        calls.append(copy.deepcopy(kw))
        if error:
            raise error
        return NS(http_response=NS(json=lambda: copy.deepcopy(raw)), parse=lambda: result)
    @contextmanager
    def stream(**kw):
        calls.append(copy.deepcopy(kw))
        if error:
            raise error
        yield NS(get_final_response=lambda: result, get_final_completion=lambda: result)
    return NS(responses=NS(with_raw_response=NS(create=create), stream=stream)), raw


def test_replay_identical_at_sdk_boundary_and_missing_write_not_zero(tmp_path):
    calls = []
    client, raw = fixture_client(calls)
    transport = ProbeTransport(client, 'responses')
    adapter = adapter_for(transport, 'responses')
    adapter.output_limit = 1024
    report = run_probe(adapter, 'test', tmp_path, records=120, mode='replay', transport=transport)
    assert calls[0] == calls[1] == calls[2]
    assert report['completed'] and report['identical_request_parameters']
    assert not report['cache_observed']
    assert report['results'][1]['history_change'] == 'unchanged'
    assert all(r['cached_tokens_present'] and not r['cache_write_tokens_present'] for r in report['results'])
    assert report['results'][0]['wire_usage']['cache_write_tokens'] is None


def test_diagnostics_are_opt_in_change_request_and_do_not_load_previous_history(tmp_path):
    calls = []
    client, raw = fixture_client(calls)
    raw['prompt_cache_diagnostics'] = {'miss_reason': 'prefix_mismatch', 'message': 'Private upstream prose'}
    transport = ProbeTransport(client, 'responses', diagnostics=True)
    adapter = adapter_for(transport, 'responses')
    adapter.output_limit = 1024
    report = run_probe(adapter, 'test', tmp_path, records=120, mode='replay', transport=transport)
    assert not report['identical_request_parameters']
    assert 'prompt_cache_options' not in calls[0]
    assert calls[1]['prompt_cache_options']['comparison_response_id'] == raw['id']
    assert calls[0]['input'] == calls[1]['input']
    assert 'previous_response_id' not in calls[1]
    assert report['results'][1]['cache_diagnostics']['miss_reason'] == 'prefix_mismatch'
    assert 'Private upstream prose' not in (tmp_path / 'report.json').read_text()


def test_stream_reasoning_breakpoint_and_no_mutation():
    calls = []
    client, _ = fixture_client(calls)
    transport = ProbeTransport(client, 'responses', stream=True, reasoning='high',
                               cache_mode='explicit', breakpoint=True)
    history = [{'role': 'user', 'content': 'synthetic reference'}]
    adapter_for(transport, 'responses').create('test', INSTRUCTIONS, history, TOOLS)
    assert history[0]['content'] == 'synthetic reference'
    assert calls[0]['reasoning'] == {'effort': 'high'}
    assert calls[0]['prompt_cache_options'] == {'mode': 'explicit'}
    assert calls[0]['input'][0]['content'][0]['prompt_cache_breakpoint'] == {'mode': 'explicit'}
    assert transport.last['usage_source'] == 'stream_final_fields_received_by_sdk'


def test_failed_stream_stops_no_automatic_protocol_fallback(tmp_path):
    calls = []
    client, _ = fixture_client(calls, error=ConnectionError('private gateway message'))
    transport = ProbeTransport(client, 'responses', stream=True)
    adapter = adapter_for(transport, 'responses')
    adapter.output_limit = 1024
    report = run_probe(adapter, 'test', tmp_path, records=120, mode='replay', transport=transport)
    assert len(calls) == 1 and not report['completed']
    assert 'private gateway message' not in (tmp_path / 'report.json').read_text()


def test_unsupported_diagnostics_stop_without_retry(tmp_path):
    calls = []
    client, _ = fixture_client(calls)
    original = client.responses.with_raw_response.create
    class Unsupported(Exception):
        status_code = 400
    def create(**kwargs):
        if 'prompt_cache_options' in kwargs:
            calls.append(copy.deepcopy(kwargs))
            raise Unsupported('private error body')
        return original(**kwargs)
    client.responses.with_raw_response.create = create
    transport = ProbeTransport(client, 'responses', diagnostics=True)
    adapter = adapter_for(transport, 'responses')
    adapter.output_limit = 1024
    report = run_probe(adapter, 'test', tmp_path, records=120, mode='replay', transport=transport)
    assert len(calls) == 2 and not report['completed']
    assert report['results'][-1]['http_status'] == 400
    assert report['results'][-1]['comparison_requested']
    assert 'private error body' not in (tmp_path / 'report.json').read_text()
    with pytest.raises(ValueError, match='已有测试报告'):
        run_probe(adapter, 'test', tmp_path)
    assert len(calls) == 2


@pytest.mark.parametrize('protocol,options', [('chat_completions', {'diagnostics': True}),
    ('chat_completions', {'breakpoint': True}), ('responses', {'cache_mode': 'explicit'})])
def test_invalid_options_rejected_before_request(protocol, options):
    with pytest.raises(ValueError):
        ProbeTransport(None, protocol, **options)


@pytest.mark.parametrize('protocol', ['responses', 'chat_completions'])
@pytest.mark.parametrize('streaming', [False, True])
def test_real_sdk_http_and_sse_usage_without_network(protocol, streaming):
    from openai import OpenAI
    from openai import _base_client
    httpx = getattr(_base_client, 'httpx2', None) or getattr(_base_client, 'httpx')
    sent = []
    def handle(request):
        sent.append(json.loads(request.content))
        if protocol == 'responses':
            raw = {'id': 'resp_synthetic', 'object': 'response', 'created_at': 0, 'model': 'test',
                   'status': 'completed', 'output': [{'id': 'msg_test', 'type': 'message', 'role': 'assistant',
                   'status': 'completed', 'content': [{'type': 'output_text', 'text': 'cache-probe-ok', 'annotations': []}]}],
                   'usage': {'input_tokens': 2000, 'output_tokens': 5, 'total_tokens': 2005,
                             'input_tokens_details': {'cached_tokens': 1500}, 'output_tokens_details': {'reasoning_tokens': 0}}}
            events = [{'type': 'response.created', 'sequence_number': 0,
                       'response': {**raw, 'status': 'in_progress', 'output': [], 'usage': None}},
                      {'type': 'response.completed', 'sequence_number': 1, 'response': raw}]
        else:
            usage = {'prompt_tokens': 2000, 'completion_tokens': 5, 'total_tokens': 2005,
                     'prompt_tokens_details': {'cached_tokens': 1500}}
            raw = {'id': 'chatcmpl_test', 'object': 'chat.completion', 'created': 0, 'model': 'test',
                   'choices': [{'index': 0, 'message': {'role': 'assistant', 'content': 'cache-probe-ok'},
                                'finish_reason': 'stop'}], 'usage': usage}
            common = {'id': raw['id'], 'object': 'chat.completion.chunk', 'created': 0, 'model': 'test'}
            events = [{**common, 'choices': [{'index': 0, 'delta': {'role': 'assistant', 'content': 'cache-probe-ok'},
                        'finish_reason': None}]},
                      {**common, 'choices': [{'index': 0, 'delta': {}, 'finish_reason': 'stop'}]},
                      {**common, 'choices': [], 'usage': usage}]
        if streaming:
            text = ''.join('data: ' + json.dumps(event) + '\n\n' for event in events) + 'data: [DONE]\n\n'
            return httpx.Response(200, content=text.encode(), headers={'content-type': 'text/event-stream'})
        return httpx.Response(200, json=raw)
    with OpenAI(api_key='synthetic-test-key', base_url='https://test.invalid/v1', max_retries=0,
                http_client=httpx.Client(transport=httpx.MockTransport(handle))) as client:
        transport = ProbeTransport(client, protocol, stream=streaming, reasoning='high')
        result = adapter_for(transport, protocol).create('test', INSTRUCTIONS,
                      [{'role': 'user', 'content': 'synthetic'}], TOOLS)
    assert result.output_text == 'cache-probe-ok' and result.status == 'completed'
    assert transport.last['wire_usage']['cached_tokens'] == 1500
    assert transport.last['cached_tokens_present'] and not transport.last['cache_write_tokens_present']
    assert len(sent) == 1
    assert bool(sent[0].get('stream')) == streaming
