import json
from contextlib import nullcontext
from types import SimpleNamespace as NS

from vasp_agent.models.model_config import ModelConfig
from vasp_agent.models.summary_probe import main, check_facts, exception_frames


SUMMARY = json.dumps({
    'goal': ['eshell111 的 Si 三维非磁性 PBE，电中性，无SOC/U/杂化；NBANDS=16。'],
    'constraints': ['上限16核、1节点、04:00:00；当前未授权提交。'],
    'decisions': ['已选择固定ENCUT=676 eV；12³/14³/16³网格，替换旧默认。'],
    'evidence': ['si-source-synthetic 仅在初始结构、固定12³网格下证明ENCUT通过。'],
    'progress': ['si-summary-synthetic 已准备prepared，未提交；无job_id。'],
    'pending': ['下一步只读核查实际输入；优化后复核精度。'],
    'risks': ['FixZ环境限制，未放行完整晶胞优化。'],
}, ensure_ascii=False)


FACTS = json.loads(SUMMARY)
# The continuation summary (Markdown under the continuation headings) carrying the same facts.
CONTINUATION_SUMMARY = '<summary>\n' + ''.join(
    f'## {head}\n' + ''.join(f'- {x}\n' for k in keys for x in FACTS[k])
    for head, keys in (('1. 主要请求与参数', ('goal', 'constraints', 'decisions')),
                       ('2. 真实结果与证据', ('evidence', 'progress', 'risks')),
                       ('3. 下一步', ('pending',)))) + '</summary>'


def install_fake(monkeypatch, create):
    config = ModelConfig('relay', 'https://example.invalid/v1', 'test', 'responses', 'SECRET_KEY', 'secret-value')
    monkeypatch.setattr('vasp_agent.models.model_config.resolve_config', lambda *a: config)
    monkeypatch.setattr('openai.OpenAI', lambda **kw: nullcontext(NS()))
    monkeypatch.setattr('vasp_agent.models.model_adapters.adapter_for', lambda *a: NS(create=create))


def test_probe_success_uses_summary_and_preserves_records(tmp_path, monkeypatch):
    requests = []
    def create(model, instructions, history, tools, **kw):
        # Tools ride along only on the continuation request, which disables them.
        assert not tools or kw.get('tool_choice') == 'none'
        requests.append(history)
        text = CONTINUATION_SUMMARY if kw.get('tool_choice') == 'none' else SUMMARY
        return NS(status='completed', output=[], output_text=text, usage=NS(input_tokens=100, output_tokens=50))
    install_fake(monkeypatch, create)
    folder = tmp_path / 'probe'
    assert main(['--workspace', str(tmp_path), '--output-dir', str(folder), '--retries', '0']) == 0
    report = json.loads((folder / 'report.json').read_text(encoding='utf-8'))
    assert all(report['checks'].values()) and report['passed']
    assert report['usage']['requests'] == len(requests)
    assert len(requests) == 1
    assert 'secret-value' not in (folder / 'report.json').read_text(encoding='utf-8')


def test_probe_failure_reports_no_success_and_preserves_original(tmp_path, monkeypatch):
    def create(*a, **kw):
        raise ConnectionError('secret-value provider response')
    install_fake(monkeypatch, create)
    folder = tmp_path / 'probe'
    assert main(['--workspace', str(tmp_path), '--output-dir', str(folder), '--retries', '0']) == 1
    report = json.loads((folder / 'report.json').read_text(encoding='utf-8'))
    assert not report['passed'] and not report['checks']['compacted']
    assert report['checks']['history_unchanged'] and report['checks']['permissions_unchanged']
    assert not (folder / 'summary.json').exists()
    assert 'secret-value' not in json.dumps(report)


def test_fact_check_detects_chosen_scan_and_submission_omissions():
    data = json.loads(SUMMARY)
    data['decisions'] = ['工具默认12³/15³/18³']
    data['progress'] = ['成功提交，已完成。']
    checks = check_facts(json.dumps(data, ensure_ascii=False))
    assert not checks['chosen_scan_values'] and not checks['prepared_not_submitted']


def test_fact_check_accepts_explicit_no_submission_record():
    data = json.loads(SUMMARY)
    data['progress'] = ['si-summary-synthetic 仅为prepared，job_id=null；记录中无提交或运行结果。']
    assert check_facts(json.dumps(data, ensure_ascii=False))['prepared_not_submitted']


def test_diagnostics_omit_exception_messages():
    try:
        try:
            raise TypeError('Bearer secret-value')
        except TypeError as exc:
            raise ConnectionError('private-url') from exc
    except ConnectionError as exc:
        result = exception_frames(exc)
    assert [x['type'] for x in result] == ['ConnectionError', 'TypeError']
    assert all(x['frames'] for x in result)
    assert 'secret-value' not in json.dumps(result) and 'private-url' not in json.dumps(result)


def test_fact_checks_read_markdown_continuation_summaries():
    from vasp_agent.models.summary_probe import check_facts
    markdown = ('## 1. 主要请求与意图\n- eshell111 / si-summary-synthetic，非磁性三维 PBE，NBANDS=16\n'
                '## 3. 任务、文件与参数\n- 用户选择 ENCUT 676，k 网格 12/14/16；资源 16 核 1 节点 04:00:00\n'
                '## 4. 真实结果与证据\n- 来源 si-source-synthetic 的初始结构收敛于 12³；任务已准备 prepared，未提交；fixz 环境风险\n'
                '## 7. 待办事项\n- 先只读核查输入\n')
    assert all(check_facts(markdown).values())
