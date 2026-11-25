"""Workflow guides: versioned files, paging, dedup, caps, compaction references, a stable prompt."""
import json
from types import SimpleNamespace

import pytest

from vasp_agent import guides
from vasp_agent.chat.ai_agent import Agent, Toolbox
from vasp_agent.chat.model_views import compact_result
from vasp_agent.models.context_budget import ContextBudget
from vasp_agent.models.context_compaction import ContextCompactor
from vasp_agent.models.model_adapters import Item
import vasp_agent.workflow.vasp_band_agent as core


def test_shipped_guides_are_versioned_and_say_what_is_not_implemented():
    assert guides.ids() == ('dos', 'wannier')
    for guide_id in guides.ids():
        guide = guides.load(guide_id)
        assert guide['id'] == guide_id and guide['version'] == {'dos': 2, 'wannier': 1}[guide_id] and len(guide['sha256']) == 64
        assert guide['implemented'] is (guide_id == 'dos') and guide['summary']
        assert all(size <= guides.PAGE_TOKENS for size in guide['page_tokens'])
        assert guide['page_tokens'] == [ContextBudget().estimate(page) for page in guide['pages']]
        assert '不授权' in ''.join(guide['pages'])
    dos = ''.join(guides.load('dos')['pages'])
    assert 'request_dos_fork' in dos and '不是分辨率' in dos and '不静默改为 Gaussian' in dos and 'LORBIT=11' in dos
    assert '半芯态' in dos and '二维 slab 的 DOS 尚未开放' in dos and '5.4.4' in dos
    wannier = guides.load('wannier')
    text = ''.join(wannier['pages'])
    assert wannier['pages'][0].splitlines()[2].startswith('> 尚未实现，不能执行')
    assert '尚未实现' in wannier['summary']
    for fact in ('5.4.4', '没有 Wannier90 接口', '6.4.3', '6.6.0', 'wannier90.x'):
        assert fact in text
    assert 'VASP 6.4.3 与 6.6.0 带 Wannier90 库' in wannier['summary'] and '没有独立的 wannier90.x' in wannier['summary']


def test_long_guides_are_paged_within_the_cap():
    body = '\n'.join(f'## 第 {n} 节\n' + '\n\n'.join('这是一段用于分页测试的说明文字。' * 12 for _ in range(6))
                     for n in range(8))
    pages = guides.paginate(body)
    assert len(pages) > 3
    assert all(guides.tokens(page) <= guides.PAGE_TOKENS for page in pages)
    assert ''.join(pages).replace('\n', '') == body.replace('\n', '')
    one_line = '无换行的超长段落' * 800
    pieces = guides.paginate(one_line)
    assert len(pieces) > 1 and ''.join(pieces) == one_line
    assert all(guides.tokens(page) <= guides.PAGE_TOKENS for page in pieces)


def output(call_id, result):
    return {'type': 'function_call_output', 'call_id': call_id, 'output': json.dumps({'ok': True, 'result': result},
                                                                                     ensure_ascii=False)}


def test_a_page_already_in_context_is_not_injected_again():
    first = guides.read('dos', 1)
    assert first['guide_page']['sha256'] == guides.load('dos')['sha256'] and first['text']
    assert first['authority'] == guides.AUTHORITY and '验收由代码判定' in first['authority']
    again = guides.read('dos', 1, guides.loaded([output('c7', first)]))
    assert again['already_loaded'] is True and again['call_id'] == 'c7' and 'text' not in again
    # Summaries appear once per version: later pointers only say they are loaded.
    pointer = guides.pointer('dos')
    assert 'summary' in pointer and pointer['read'] == {'tool': 'read_stage_guide', 'guide': 'dos', 'page': 1}
    later = guides.pointer('dos', guides.loaded([output('c1', {'guides': [pointer]})]))
    assert later['summary_loaded'] is True and 'summary' not in later
    with pytest.raises(ValueError, match='共 2 页'):
        guides.read('dos', 3)


def test_injection_cap_is_enforced_with_a_clear_message(monkeypatch):
    page = guides.read('dos', 1)
    ledger = guides.loaded([output('c1', page)])
    assert ledger['tokens'] == guides.tokens(page['text'])
    monkeypatch.setattr(guides, 'INJECTION_TOKENS', ledger['tokens'] + 100)
    with pytest.raises(core.ToolError, match='将超过上限') as error:
        guides.read('wannier', 1, ledger)
    assert error.value.error_code == 'GUIDE_BUDGET_EXCEEDED' and '/compact' in str(error.value)
    # The default cap leaves room for both shipped guides and their summaries.
    monkeypatch.undo()
    assert guides.read('wannier', 1, ledger)['text']


def test_page_cap_applies_to_every_page(monkeypatch, tmp_path):
    (tmp_path / 'long.md').write_text('---\nid: long\nversion: 3\nimplemented: true\ntitle: 长\nsummary: 摘要\n---\n'
                                      + '\n'.join(f'## 节 {n}\n' + '长文本。' * 600 for n in range(4)), encoding='utf-8')
    monkeypatch.setattr(guides, 'FOLDER', tmp_path)
    guide = guides.load('long')
    assert len(guide['pages']) >= 4 and max(guide['page_tokens']) <= guides.PAGE_TOKENS
    assert guides.read('long', 2)['next_page'] == 3 and guides.pointer('long')['pages'] == len(guide['pages'])


def test_compact_view_keeps_guide_pointers_verbatim():
    pointers = [guides.pointer('dos'), guides.pointer('wannier')]
    result = {'phase': 'probe', 'guides': pointers, 'bulk': {'x' + str(n): 'y' * 300 for n in range(20)},
              'hashes': {str(n): 'a' * 64 for n in range(6)}}
    view = compact_result(result, lambda payload: {'sha256': 'f' * 64, 'chars': 1})
    assert view['model_view']['compact'] is True and view['guides'] == pointers


def history_with_guide():
    page = guides.read('dos', 1)
    rows = []
    for n in range(8):
        rows.append({'role': 'user', 'content': f'任务{n}' + 'x' * 2000})
        rows.append({'type': 'function_call', 'name': 'read_stage_guide' if n == 1 else 'get_status',
                     'call_id': str(n), 'arguments': '{}'})
        rows.append(output(str(n), page) if n == 1 else
                    {'type': 'function_call_output', 'call_id': str(n), 'output': '{"status":"unknown"}'})
        rows.append({'role': 'assistant', 'content': '继续'})
    return rows


def summary(*_):
    return json.dumps(dict(goal=['摘要会改写一切'], constraints=[], decisions=[], evidence=[], progress=[],
                           pending=[], risks=[]), ensure_ascii=False)


def test_compaction_keeps_a_reference_to_loaded_guides(tmp_path):
    history = history_with_guide()
    compactor = ContextCompactor(tmp_path / 'context.json', 16000)
    compactor.references = guides.references
    context = compactor.context(history, summary, lambda *a: None)
    head = context[0]['content']
    guide = guides.load('dos')
    assert f"dos v2 sha256={guide['sha256']}（已读第 1 页）" in head and 'read_stage_guide 重新读取' in head
    assert head.index('摘要会改写一切') < head.index('压缩前已加载的阶段指南')
    # The page itself left the context, so it may be read again.
    live = history[compactor.cache['end']:]
    assert guides.loaded(live)['pages'] == {}
    assert 'text' in guides.read('dos', 1, guides.loaded(live))
    # The cached prefix is rebuilt identically on the next request.
    assert compactor.context(history, lambda *a: pytest.fail('cached'), lambda *a: None)[0]['content'] == head


def test_native_compaction_appends_the_reference_as_its_own_message(tmp_path):
    history = history_with_guide()
    compactor = ContextCompactor(tmp_path / 'context.json', 16000)
    compactor.references = guides.references
    compactor.native = lambda items: [{'type': 'compaction', 'encrypted_content': 'opaque'}]
    compactor.native_scope = 'test'
    context = compactor.context(history, summary, lambda *a: None)
    assert context[0]['type'] == 'compaction'
    assert context[1]['role'] == 'user' and 'dos v2 sha256=' in context[1]['content']


def test_references_are_empty_without_guides():
    assert guides.references([{'type': 'function_call_output', 'call_id': '1', 'output': '{"ok":true}'}]) == ''


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


def test_system_prompt_and_catalog_do_not_change_with_guides_or_stage(tmp_path, monkeypatch):
    box = Toolbox(tmp_path)
    box.require_scan_plans = True
    review = {'phase': 'probe', 'snapshot': 's' * 64, 'inputs': {'config': {}}}
    monkeypatch.setattr(Toolbox, 'get_stage_review', lambda self, server, job_name: guides.attach(
        review, ['dos'], self.loaded_guides()))
    responses = FakeResponses([
        reply(call('get_stage_review', {'server': None, 'job_name': 'si'}, 'a')),
        reply(call('read_stage_guide', {'guide': 'dos', 'page': 1}, 'b')),
        reply(call('read_stage_guide', {'guide': 'dos', 'page': 1}, 'c'),
              call('get_stage_review', {'server': None, 'job_name': 'si'}, 'd')),
        reply(call('read_stage_guide', {'guide': 'wannier', 'page': 1}, 'e')),
        reply(text='完成')])
    agent = Agent(SimpleNamespace(responses=responses), 'test', box)
    assert agent.ask('读一下 DOS 指南') == '完成'
    instructions = {r['instructions'] for r in responses.requests}
    catalogs = {json.dumps(r['tools'], sort_keys=True) for r in responses.requests}
    assert len(responses.requests) == 5 and len(instructions) == 1 and len(catalogs) == 1
    assert instructions == {agent.context_instructions()}
    outputs = {item['call_id']: json.loads(item['output']) for item in agent.history
               if item.get('type') == 'function_call_output'}
    assert outputs['a']['result']['guides'][0]['summary']
    assert outputs['b']['result']['guide_page']['page'] == 1
    assert outputs['c']['result']['already_loaded'] is True and outputs['c']['result']['call_id'] == 'b'
    assert outputs['d']['result']['guides'][0]['summary_loaded'] is True
    assert outputs['e']['result']['implemented'] is False and '尚未实现' in outputs['e']['result']['text']
    # Guides arrive as tool results, never as instructions or developer messages.
    assert not any(item.get('role') in ('system', 'developer') and '指南' in str(item.get('content'))
                   for item in agent.history)
