import copy
import json
from types import SimpleNamespace as NS

import pytest

from test_context_compaction import summary
from vasp_agent.models.context_budget import ContextBudget
from vasp_agent.chat.ai_agent import Agent, Toolbox
from vasp_agent.models.model_adapters import Item


def test_budget_reserves_and_calibration():
    b = ContextBudget(32000, 4000, 2000)
    assert b.available == 26000
    overhead = ['system', {'tools': 'x' * 1000}]
    b.overhead = b.estimate(overhead)
    h = [{'role': 'user', 'content': '硅的能带' * 100}]
    before = b.measure(h)
    b.calibrate(h, overhead, before * 2)
    b.overhead = b.estimate(overhead)
    assert b.measure(h) > before * 2
    assert b.status(h)['last_api_input_tokens'] == before * 2
    with pytest.raises(ValueError):
        ContextBudget(6000, 4000, 2048)
    with pytest.raises(ValueError):
        ContextBudget(trigger=.5, target=.8)


def test_status_no_model_call_includes_tool_overhead(tmp_path):
    a = Agent(None, 'unknown-model-alias', Toolbox(tmp_path), adapter=NS(create=lambda *a: pytest.fail('offline')))
    info = a.context_status()
    assert info['measurement'] == 'estimate_utf8_calibrated'
    assert info['estimated_input_tokens'] > 1000
    assert info['configured_context_window'] == 32768
    assert info['input_budget'] == 26720
    assert a.usage['requests'] == 0


def test_manual_compact_below_threshold_preserves_history(tmp_path):
    calls = []
    def create(model, instructions, history, tools, **kw):
        calls.append((tools, kw))
        return NS(status='completed', output_text=summary('用户在验证硅能带，未提交。'), output=[], usage=None)
    box = Toolbox(tmp_path)
    a = Agent(None, 'unknown', box, adapter=NS(create=create))
    a.history = [{'role': 'user', 'content': str(i) + '原始背景' * 500} for i in range(6)]
    original = copy.deepcopy(a.history)
    assert a.compact_context()['compacted']
    # The prompt and catalog fit a 32k window beside this history, so the
    # summary is the continuation request: same catalog, tools disabled.
    assert calls == [(box.model_schemas(), {'tool_choice': 'none'})]
    assert a.history == original


def test_oversized_current_turn_stops_before_paid_request(tmp_path):
    a = Agent(None, 'unknown', Toolbox(tmp_path), adapter=NS(create=lambda *a: pytest.fail('must not call model')))
    result = a.ask('很长' * 50000)
    assert '超过输入预算' in result
    assert a.usage['requests'] == 0
    assert a.file.exists()


def test_profile_budget_precedence_and_validation(tmp_path):
    from vasp_agent.models.model_config import resolve_config
    from test_models import args
    (tmp_path / 'models.toml').write_text('''[profiles.relay]
base_url="https://example.invalid/v1"
model="unknown"
protocol="chat_completions"
api_key_env="TEST_KEY"
context_window=64000
output_reserve=5000
compact_target=0.5
''')
    cfg = resolve_config(args(tmp_path, profile='relay'), {})
    assert cfg.context_window == 64000 and cfg.output_reserve == 5000
    cfg = resolve_config(args(tmp_path, profile='relay', context_window=96000), {'VASP_AGENT_CONTEXT_WINDOW': '128000'})
    assert cfg.context_window == 96000
    with pytest.raises(ValueError):
        resolve_config(args(tmp_path, profile='relay'), {'VASP_AGENT_COMPACT_TRIGGER': '.4'})


def test_slash_catalog_contains_context_management():
    from vasp_agent.ui.interactive_prompt import command_catalog
    assert {'/context', '/compact'} <= set(command_catalog())


def test_token_trigger_compacts_before_main_request(tmp_path):
    calls = []
    def create(model, instructions, history, tools):
        calls.append(bool(tools))
        text = summary('保留硅计算目标和未提交状态。') if not tools else '继续检查。'
        return NS(status='completed', output_text=text,
                  output=[Item(type='message', role='assistant', content=text)], usage=None)
    a = Agent(None, 'unknown', Toolbox(tmp_path), adapter=NS(create=create))
    a.history = [{'role': 'user', 'content': '硅' * 3000} for _ in range(8)]
    assert a.ask('继续') == '继续检查。'
    assert calls[-1] is True and calls[:-1] and not any(calls[:-1])
    assert a.context_status()['input_budget_percent'] < 80
