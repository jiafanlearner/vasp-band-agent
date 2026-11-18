import copy
import json
from types import SimpleNamespace as NS

from vasp_agent.models.cache_probe import run_probe
from vasp_agent.models.model_adapters import Item


def test_probe_appends_response_and_state_without_rewriting_prefix(tmp_path):
    calls = []
    def create(model, instructions, history, tools):
        calls.append(copy.deepcopy((instructions, history, tools)))
        return NS(status='completed', output_text='cache-probe-ok', usage=None,
                  output=[Item(type='message', role='assistant', content=[{'type': 'output_text', 'text': 'cache-probe-ok'}])])
    adapter = NS(create=create, protocol='responses', output_limit=1024)
    report = run_probe(adapter, 'test', tmp_path)
    assert report['completed'] and report['tools_executed'] == 0
    assert calls[0][0] == calls[1][0] == calls[2][0]
    assert calls[0][2] == calls[1][2] == calls[2][2]
    assert calls[1][1][:len(calls[0][1])] == calls[0][1]
    assert report['results'][1]['history_change'] == 'append_only'
    assert all(x['cached_tokens'] is None for x in report['results'])
    assert json.loads((tmp_path / 'report.json').read_text())['completed']


def test_probe_stops_after_failure_without_persisting_error_body(tmp_path):
    def create(*args):
        raise ConnectionError('PRIVATE_KEY in upstream error')
    report = run_probe(NS(create=create, protocol='responses', output_limit=1024), 'test', tmp_path)
    assert not report['completed'] and len(report['results']) == 1
    assert 'PRIVATE_KEY' not in (tmp_path / 'report.json').read_text()
