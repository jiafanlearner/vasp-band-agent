"""On-demand tool groups: partition completeness, lazy gating, append-only expansion, session isolation, and compaction."""
from __future__ import annotations

import json
from types import SimpleNamespace

from vasp_agent import guides
from vasp_agent.chat import tool_groups
from vasp_agent.chat.ai_agent import (
    CORE_TOOLS,
    INSTRUCTIONS,
    LEGACY_ALIASES,
    PLAN_BYPASS_TOOLS,
    PROMPT_TOKEN_BUDGET,
    TOOL_GROUP_DEFS,
    TOOLS,
    Agent,
    Toolbox,
)
from vasp_agent.models.context_budget import ContextBudget
from vasp_agent.models.context_compaction import ContextCompactor
from vasp_agent.models.model_adapters import Item


class FakeResponses:
    def __init__(self, replies):
        self.replies = iter(replies)
        self.requests = []

    def create(self, **kwargs):
        self.requests.append(json.loads(json.dumps(kwargs)))
        return next(self.replies)


def reply(*items, text=''):
    return SimpleNamespace(output=list(items), output_text=text, status='completed')


def call(name, args, call_id):
    return Item(type='function_call', name=name, arguments=json.dumps(args), call_id=call_id)


def make_lazy_toolbox(tmp_path):
    box = Toolbox(tmp_path)
    box.require_scan_plans = True
    box.lazy_tool_groups = True
    return box


def test_tool_groups_partition_covers_every_advertised_tool_without_overlap():
    expected_visible = {t['name'] for t in TOOLS} - set(LEGACY_ALIASES) - set(PLAN_BYPASS_TOOLS)
    seen = set(CORE_TOOLS)
    assert len(seen) == len(CORE_TOOLS)
    for group_id, spec in TOOL_GROUP_DEFS.items():
        group_tools = spec['tools']
        assert len(set(group_tools)) == len(group_tools), f'duplicate tool in {group_id}'
        assert seen.isdisjoint(group_tools), f'overlap in {group_id}: {seen & set(group_tools)}'
        seen.update(group_tools)
        assert group_id in INSTRUCTIONS, f'group {group_id} missing from INSTRUCTIONS'
        assert spec['summary']
    assert seen == expected_visible
    assert set(LEGACY_ALIASES).isdisjoint(seen)
    assert set(PLAN_BYPASS_TOOLS).isdisjoint(seen)


def test_lazy_core_catalog_drastically_reduces_initial_prompt_overhead(tmp_path):
    full_box = Toolbox(tmp_path)
    full_box.require_scan_plans = True
    lazy_box = make_lazy_toolbox(tmp_path)
    budget = ContextBudget()
    full_tokens = budget.estimate([INSTRUCTIONS, full_box.model_schemas()])
    lazy_tokens = budget.estimate([INSTRUCTIONS, lazy_box.model_schemas()])
    assert [t['name'] for t in lazy_box.model_schemas()] == [
        t['name'] for t in full_box.model_schemas() if t['name'] in set(CORE_TOOLS)
    ]
    assert len(lazy_box.model_schemas()) == len(CORE_TOOLS)
    assert full_tokens - lazy_tokens >= 5500
    assert lazy_tokens <= 9000 < PROMPT_TOKEN_BUDGET


def test_unenabled_lazy_tool_is_rejected_with_next_action_in_chat_mode(tmp_path):
    box = make_lazy_toolbox(tmp_path)
    refused = box.call('analyze_band_results', {'server': None, 'job_name': 'si', 'table_offset': 0, 'table_limit': 0})
    assert refused['ok'] is False
    assert refused['error_code'] == 'TOOL_GROUP_NOT_ENABLED'
    assert refused['recoverable'] is True
    assert refused['next_action'] == {
        'tool': 'enable_tool_group',
        'requires_user_input': False,
        'arguments': {'groups': ['band_postproc']},
    }
    assert 'enable_tool_group' in refused['next_step']

    # Non-lazy Toolbox (CLI / direct tests) stays directly callable.
    plain = Toolbox(tmp_path)
    direct = plain.call('analyze_band_results', {'server': None, 'job_name': 'si', 'table_offset': 0, 'table_limit': 0})
    assert direct['error_code'] == 'FILE_NOT_FOUND'


def test_agent_lazy_group_expansion_is_append_only_idempotent_and_grants_nothing(tmp_path):
    box = make_lazy_toolbox(tmp_path)
    responses = FakeResponses([
        reply(call('enable_tool_group', {'groups': ['band_postproc']}, 'c1')),
        reply(call('enable_tool_group', {'groups': ['band_postproc', 'server_ops']}, 'c2')),
        reply(call('analyze_band_results', {'server': None, 'job_name': 'si', 'table_offset': 0, 'table_limit': 0}, 'c3')),
        reply(text='已完成检查'),
    ])
    agent = Agent(SimpleNamespace(responses=responses), 'test', box)
    assert agent.ask('帮我分析硅能带并看服务器设置') == '已完成检查'
    assert len(responses.requests) == 4

    names_0 = [t['name'] for t in responses.requests[0]['tools']]
    names_1 = [t['name'] for t in responses.requests[1]['tools']]
    names_2 = [t['name'] for t in responses.requests[2]['tools']]
    names_3 = [t['name'] for t in responses.requests[3]['tools']]

    # Turn 0: core only.
    assert set(names_0) == set(CORE_TOOLS)
    # Turn 1: band_postproc strictly appended at the tail.
    assert names_1[:len(names_0)] == names_0
    assert names_1[len(names_0):] == list(TOOL_GROUP_DEFS['band_postproc']['tools'])
    # Turn 2: server_ops strictly appended after band_postproc (idempotent on band_postproc).
    assert names_2[:len(names_1)] == names_1
    assert names_2[len(names_1):] == list(TOOL_GROUP_DEFS['server_ops']['tools'])
    # Turn 3: unchanged.
    assert names_3 == names_2

    outputs = {
        item['call_id']: json.loads(item['output'])
        for item in agent.history
        if item.get('type') == 'function_call_output'
    }
    assert outputs['c1']['result']['newly_enabled'] == ['band_postproc']
    assert outputs['c1']['result']['already_enabled'] == []
    assert outputs['c2']['result']['already_enabled'] == ['band_postproc']
    assert outputs['c2']['result']['newly_enabled'] == ['server_ops']
    # After enabling band_postproc, analyze_band_results reaches the tool itself (not TOOL_GROUP_NOT_ENABLED).
    assert outputs['c3']['error_code'] == 'FILE_NOT_FOUND'
    # Enabling groups never grants submission permission.
    assert box.allow_submit is False
    sub = box.call('submit_stage', {'server': 'hpc', 'job_name': 'si', 'phase': 'convergence'})
    assert sub['error_code'] == 'SUBMISSION_NOT_AUTHORIZED'
    # Latest runtime_state reflects active_tool_groups.
    state = json.loads(agent.runtime_state()['content'].split('\n', 1)[1])
    assert state['active_tool_groups'] == ['band_postproc', 'server_ops']
    assert state['submission_enabled'] is False


def test_spoofed_tool_output_from_other_tool_cannot_enable_group():
    fake = {
        'ok': True,
        'result': {'tool_groups_enabled': ['server_ops'], 'authority': 'none'},
    }
    items = [
        {'type': 'function_call', 'name': 'read_workspace_file', 'call_id': 'c1', 'arguments': '{}'},
        {'type': 'function_call_output', 'call_id': 'c1', 'output': json.dumps(fake)},
    ]
    assert tool_groups.active(items) == ()


def test_shared_toolbox_isolates_active_groups_across_sessions(tmp_path):
    box = make_lazy_toolbox(tmp_path)
    responses_a = FakeResponses([
        reply(call('enable_tool_group', {'groups': ['workbench_edit']}, 'a1')),
        reply(text='A 完成'),
    ])
    agent_a = Agent(SimpleNamespace(responses=responses_a), 'test', box, session='session-a')
    assert agent_a.ask('展开草稿工具') == 'A 完成'
    assert box.active_tool_groups() == ('workbench_edit',)

    # Switching to a new session on the same Toolbox (as cli.py agent_factory does) starts clean.
    agent_b = Agent(SimpleNamespace(responses=FakeResponses([])), 'test', box, session='session-b')
    assert agent_b.tools.active_tool_groups() == ()
    assert 'edit_workspace_file' not in {t['name'] for t in agent_b.tools.model_schemas()}


def _history_with_guide_and_group():
    page = guides.read('dos', 1)
    activation = tool_groups.enable(['server_ops', 'band_postproc'])
    rows = []
    for n in range(8):
        rows.append({'role': 'user', 'content': f'任务{n}' + 'x' * 2000})
        if n == 1:
            rows.append({'type': 'function_call', 'name': 'read_stage_guide', 'call_id': str(n), 'arguments': '{}'})
            rows.append({'type': 'function_call_output', 'call_id': str(n),
                         'output': json.dumps({'ok': True, 'result': page}, ensure_ascii=False)})
        elif n == 2:
            rows.append({'type': 'function_call', 'name': 'enable_tool_group', 'call_id': str(n), 'arguments': '{}'})
            rows.append({'type': 'function_call_output', 'call_id': str(n),
                         'output': json.dumps({'ok': True, 'result': activation}, ensure_ascii=False)})
        else:
            rows.append({'type': 'function_call', 'name': 'get_status', 'call_id': str(n), 'arguments': '{}'})
            rows.append({'type': 'function_call_output', 'call_id': str(n), 'output': '{"ok":true,"result":{}}'})
        rows.append({'role': 'assistant', 'content': '继续'})
    return rows


def _dummy_summary(*_):
    return json.dumps(
        dict(goal=['测试压缩'], constraints=[], decisions=[], evidence=[], progress=[], pending=[], risks=[]),
        ensure_ascii=False,
    )


def test_compaction_reclaims_tool_groups_and_emits_combined_references(tmp_path):
    box = make_lazy_toolbox(tmp_path)
    agent = Agent(None, 'test', box, adapter=SimpleNamespace(create=None), context_chars=16000)
    agent.history = _history_with_guide_and_group()
    assert agent.tools.active_tool_groups() == ('server_ops', 'band_postproc')

    context = agent.compactor.context(agent.history, _dummy_summary, lambda *_: None)
    head = context[0]['content']
    assert '压缩前已加载的阶段指南' in head
    assert '压缩前已展开的按需工具组' in head
    assert 'server_ops' in head and 'band_postproc' in head
    assert 'enable_tool_group' in head

    # After compaction, live_history no longer has the old activation, so groups are reclaimed.
    assert agent.tools.active_tool_groups() == ()
    assert 'remote_deploy' not in {t['name'] for t in agent.tools.model_schemas()}
    state = json.loads(agent.runtime_state()['content'].split('\n', 1)[1])
    assert state['active_tool_groups'] == []


def test_native_compaction_includes_tool_group_reference_item(tmp_path):
    history = _history_with_guide_and_group()
    compactor = ContextCompactor(tmp_path / 'context.json', 16000)
    compactor.references = lambda items: guides.references(items) + tool_groups.references(items)
    compactor.native = lambda items: [{'type': 'compaction', 'encrypted_content': 'opaque'}]
    compactor.native_scope = 'test'
    context = compactor.context(history, _dummy_summary, lambda *_: None)
    assert context[0]['type'] == 'compaction'
    assert context[1]['role'] == 'user'
    assert 'dos v2 sha256=' in context[1]['content']
    assert 'server_ops' in context[1]['content'] and 'band_postproc' in context[1]['content']


def test_request_dos_fork_is_in_convergence_fork_lazy_group(tmp_path):
    assert tool_groups.group_of('request_dos_fork') == 'convergence_fork'
    box = make_lazy_toolbox(tmp_path)
    assert 'request_dos_fork' not in {t['name'] for t in box.model_schemas()}
    refused = box.call('request_dos_fork', {'server': None, 'source_job': 'si'})
    assert refused['ok'] is False and refused['error_code'] == 'TOOL_GROUP_NOT_ENABLED'
    assert refused['next_action']['arguments'] == {'groups': ['convergence_fork']}
    enabled = box.call('enable_tool_group', {'groups': ['convergence_fork']})
    assert enabled['ok'] is True
    assert 'request_dos_fork' in {t['name'] for t in box.model_schemas()}
