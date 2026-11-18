"""A bounded end-to-end summary test using synthetic VASP history only."""
import argparse
import copy
import json
import random
import traceback
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
from time import monotonic

from vasp_agent.models.context_compaction import encoded, digest


def exception_frames(exc):
    """Record exception classes and code locations, never messages/locals/URLs."""
    result, seen = [], set()
    while exc is not None and id(exc) not in seen and len(result) < 6:
        seen.add(id(exc))
        result.append({'type': type(exc).__name__, 'frames': [
            {'file': Path(frame.filename).name, 'line': frame.lineno, 'function': frame.name}
            for frame in traceback.extract_tb(exc.__traceback__)[-8:]]})
        exc = exc.__cause__ or exc.__context__
    return result


def synthetic_history(diagnostic_lines=90):
    """Known facts occur in old turns, so keeping the recent tail cannot pass."""
    rng = random.Random(20261008)
    rows = []
    cases = [
        ('在 eshell111 上计算 Si 三维非磁性、电中性 PBE 能带，不用 SOC、DFT+U 或杂化泛函。'
         'NBANDS=16，资源上限16核、1节点、04:00:00。这些是历史约束，当前提交权限必须查程序。',
         'inspect_structure', {'structure': 'examples/Si.POSCAR', 'elements': ['Si'], 'atoms': 2}),
        ('检查旧任务 si-source-synthetic 的收敛证据。', 'inspect_convergence',
         {'source_job': 'si-source-synthetic', 'encut': {'passed': True, 'tested': [520, 598, 676],
          'fixed_mesh': [12, 12, 12], 'structure': '初始结构', 'energy_delta_meV_per_atom': [0.061905, 0.037395]},
          'mesh': {'passed': False, 'tested': [8, 10, 12]}, 'reusable_encut': 676,
          'caveat': '仅证明原初始结构和固定12³网格下ENCUT收敛；新网格组合与优化后结构仍需交叉精度复核。'}),
        ('新任务 si-summary-synthetic，仅扫描 k 网格；菜单选择2，采用AI建议12³/14³/16³，替换默认12³/15³/18³。',
         'propose_scan_plan', {'plan_id': 'synthetic-plan-01', 'status': 'selected',
          'selected_by': 'operator_menu_2', 'mode': 'mesh', 'encut': 676, 'nbands': 16,
          'meshes': [[12, 12, 12], [14, 14, 14], [16, 16, 16]], 'source_job': 'si-source-synthetic'}),
        ('按已选择方案准备新目录，先不提交。', 'remote_prepare',
         {'job_name': 'si-summary-synthetic', 'status': 'prepared', 'job_id': None,
          'path': '/synthetic/jobs/si-summary-synthetic', 'mode': 'mesh', 'encut': 676, 'nbands': 16,
          'meshes': [[12, 12, 12], [14, 14, 14], [16, 16, 16]],
          'potential': {'TITEL': 'PAW_PBE Si 05Jan2001', 'ENMAX': 245.345}}),
        ('检查完整晶胞优化的环境；摘要不能把准备当作提交或完成。', 'check_remote_environment',
         {'ok': False, 'cell_constraint_marker': 'FixZ',
          'next_step': '三维完整晶胞优化需核验适用环境；不能把标记检查解释为所有实际自由度已验证。'}),
        ('我暂时不允许提交。接下来只读取新任务输入并解释；不要重复问我选扫描参数。', 'permission_status',
         {'submission_enabled': False, 'submissions_remaining': 0, 'source': 'synthetic_current_state'}),
    ]
    for i, (question, tool, result) in enumerate(cases):
        # Synthetic bulky diagnostics, with critical facts kept outside the filler.
        noise = '\n'.join(f'diagnostic-{i}-{j}: {rng.random():.14f}; {rng.random():.14f}' for j in range(diagnostic_lines))
        if i == len(cases) - 1 and diagnostic_lines == 0:
            # Keep every fact-bearing turn outside the recent retention budget,
            # including in the small connectivity control.
            noise = '合成诊断占位，可从摘要省略。' * 110
        rows += [{'role': 'user', 'content': question},
                 {'type': 'function_call', 'name': tool, 'call_id': f'synthetic-{i}', 'arguments': '{}'},
                 {'type': 'function_call_output', 'call_id': f'synthetic-{i}', 'output': encoded(result)},
                 {'role': 'assistant', 'content': '合成测试的无关诊断（可省略）：\n' + noise}]
    rows += [{'role': 'user', 'content': '请继续只读核查，保留我的选择。'},
             {'role': 'assistant', 'content': '下一步查询真实输入；尚未执行。'},
             {'role': 'user', 'content': '先总结之前进展。'}]
    return rows


# Markdown continuation headings that carry each JSON handoff field's content.
SECTION_KEYWORDS = {'decisions': ('主要请求', '任务、文件与参数', '参数'),
                    'evidence': ('真实结果', '证据'),
                    'progress': ('真实结果', '当前工作', '进展'),
                    'pending': ('待办', '下一步')}


def summary_parts(summary):
    """Field texts from either a JSON handoff or a Markdown continuation summary."""
    try:
        data = json.loads(summary)
        return encoded(data).lower(), {k: encoded(data[k]).lower() for k in SECTION_KEYWORDS}
    except (ValueError, TypeError, KeyError):
        pass
    import re
    text = summary.lower()
    chunks = re.split(r'(?m)^(#{1,6}[^\n]*)$', text)
    sections = list(zip(chunks[1::2], chunks[2::2]))
    parts = {}
    for field, keywords in SECTION_KEYWORDS.items():
        body = ''.join(head + content for head, content in sections if any(k in head for k in keywords))
        parts[field] = body or text  # no matching heading: judge on the whole summary
    return text, parts


def check_facts(summary):
    """Fixture-specific presence checks; not a general factuality certificate."""
    combined, parts = summary_parts(summary)
    decisions, evidence, progress, pending = (parts[k] for k in ('decisions', 'evidence', 'progress', 'pending'))
    return {
        'server_and_target': 'eshell111' in combined and 'si-summary-synthetic' in combined,
        'nbands': 'nbands' in combined and '16' in combined,
        'physical_model': 'pbe' in combined and '非磁' in combined and any(x in combined for x in ('3d', '三维')),
        'resource_limits': '16' in combined and '节点' in combined and any(x in combined for x in ('04:00:00', '4小时', '4 小时')),
        'chosen_scan_values': all(x in decisions for x in ('12', '14', '16', '676')),
        'evidence_source': 'si-source-synthetic' in evidence,
        'conditional_convergence': '初始' in evidence and '12' in evidence and '结构' in evidence,
        'prepared_not_submitted': ('prepared' in progress or '准备' in progress)
                                  and any(x in progress for x in ('未提交', '没有提交', '未获得', '无作业', '无提交', '尚未提交')),
        'environment_risk': 'fixz' in combined,
        'pending_read_check': any(x in pending for x in ('只读', '读取', '核查', '核验', '查询')),
    }


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--workspace', default='.')
    parser.add_argument('--profile')
    parser.add_argument('--config')
    parser.add_argument('--env-file')
    parser.add_argument('--timeout', type=float, default=90)
    parser.add_argument('--retries', type=int, choices=(0, 1), default=1)
    parser.add_argument('--diagnostic-lines', type=int, choices=(0, 90), default=90,
                        help='每轮合成诊断行数；0为保留同样关键事实的小输入对照')
    parser.add_argument('--output-dir')
    parser.add_argument('--dry-run', action='store_true')
    args = parser.parse_args(argv)
    if not 1 <= args.timeout <= 180:
        parser.error('--timeout must be between 1 and 180 seconds')
    from vasp_agent.models.model_config import resolve_config
    config = replace(resolve_config(args), compaction_mode='summary', recent_context_tokens=1024)
    # Never silently change transport/model/provider. Only compaction mode is tested.
    stamp = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ')
    from vasp_agent.common import agent_paths
    folder = Path(args.output_dir or agent_paths.cache_dir(args.workspace, 'summary-probe', stamp)).resolve()
    history = synthetic_history(args.diagnostic_lines)
    from vasp_agent.models.context_budget import ContextBudget
    estimate = ContextBudget().estimate(history)
    if args.dry_run:
        print(encoded({'profile': config.profile, 'protocol': config.protocol, 'model': config.model,
                       'mode': 'summary', 'synthetic_input_estimate': estimate,
                       'request_cap': 6, 'output_directory': str(folder)}))
        return 0
    if not config.api_key:
        parser.error('API key is not configured')
    folder.mkdir(parents=True, exist_ok=False)
    from vasp_agent.models.api_client import create_client
    from vasp_agent.models.model_adapters import adapter_for
    from vasp_agent.models.context_compaction import CompactionError
    from vasp_agent.chat.ai_agent import Agent, Toolbox
    from vasp_agent.ui.terminal_ui import TerminalUI
    report = {'synthetic_only': True, 'mode': 'summary', 'profile': config.profile,
              'model': config.model, 'protocol': config.protocol, 'passed': False,
              'diagnostic_lines': args.diagnostic_lines}
    started = monotonic()
    with create_client(config, args.timeout) as client:
        adapter = adapter_for(client, config.protocol)
        calls = []
        create = adapter.create
        def bounded_create(model, instructions, rows, tools, **kw):
            # Tools may ride along only with tool_choice='none' (the continuation
            # summary keeps them for the cache prefix); nothing can be called.
            if (tools and kw.get('tool_choice') != 'none') or len(calls) >= 6:
                raise CompactionError('测试请求已达到上限或错误地携带工具')
            calls.append({'input_estimate': ContextBudget().estimate([instructions, rows]),
                          'tools': len(tools), 'tool_choice': kw.get('tool_choice')})
            try:
                return create(model, instructions, rows, tools, **kw)
            except Exception as exc:
                calls[-1]['exception_frames'] = exception_frames(exc)
                raise
        adapter.create = bounded_create
        toolbox = Toolbox(folder)
        unexpected_calls = []
        def no_execution(*a, **kw):
            unexpected_calls.append(True)
            raise AssertionError('The synthetic test must never execute tools')
        toolbox.call = no_execution
        agent = Agent(client, config.model, toolbox, session='synthetic', adapter=adapter,
                      ui=TerminalUI('never'), context_config=config, model_retries=args.retries)
        # The probe measures the model's own summary; a rule-built fallback would
        # report success the model never achieved.
        agent.compactor.mechanical_fallback = False
        agent.history = copy.deepcopy(history)
        agent.file.write_text(encoded(history), encoding='utf-8')
        original_file = agent.file.read_bytes()
        permissions = copy.deepcopy(toolbox.permission_status())
        before = agent.context_status()
        history_before = agent.context_budget.estimate(agent.compactor.project(history))
        result = agent.compact_context()
        report.update(result=result, usage=agent.usage, elapsed_seconds=round(monotonic() - started, 2),
                      requests=calls, before=before, history_estimate_before=history_before)
        checks = {
            'history_unchanged': agent.history == history and agent.file.read_bytes() == original_file,
            'permissions_unchanged': toolbox.permission_status() == permissions and not toolbox.allow_submit,
            'compacted': result['compacted'],
        }
        if result['compacted']:
            summary = agent.compactor.cache['summary']
            # Continuation summaries are Markdown; batched/mechanical ones are JSON.
            name = 'summary.md' if agent.compactor.cache.get('format') == 'continuation_v1' else 'summary.json'
            (folder / name).write_text(summary, encoding='utf-8')
            report['summary_method'] = agent.compactor.cache.get('method')
            report['summary_file'] = name
            checks.update(check_facts(summary))
            end = agent.compactor.cache['end']
            context = agent.compactor.context(history, no_execution, print, preview=True)
            report['history_estimate_after'] = agent.context_budget.estimate(context)
            checks['recent_turns_unchanged'] = context[1:] == history[end:]
            # Two small follow-ups must use the saved summary without another API request.
            for question in ('状态呢', '继续只读'):
                agent.history.append({'role': 'user', 'content': question})
                agent.compactor.context(agent.history, no_execution, print)
            checks['no_immediate_recompression'] = not unexpected_calls and agent.compactor.last_error is None
            checks['smaller_context'] = result['context']['estimated_input_tokens'] < before['estimated_input_tokens']
        report.update(checks=checks, passed=all(checks.values()), original_sha256=digest(history))
    (folder / 'report.json').write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
    lines = ['# 结构化摘要实测', '', f"结果：{'通过' if report['passed'] else '未通过'}", '',
             '仅合成历史；不读取真实会话，不执行 SSH/VASP，不提交任务。', '',
             '保真检查为固定场景字段匹配，不能证明所有对话语义都无损；summary.md / summary.json 可人工复核。', '',
             f"摘要方式：{report.get('summary_method') or '未生成'}。", '',
             f"耗时 {report['elapsed_seconds']} 秒；API 请求 {len(calls)} 次。", '',
             *[f"- {k}: {'通过' if v else '未通过'}" for k, v in report['checks'].items()], '',
             '完整诊断、估算和接口报告用量见 report.json。']
    (folder / 'report.md').write_text('\n'.join(lines) + '\n', encoding='utf-8')
    print(encoded({'passed': report['passed'], 'report': str(folder / 'report.md'), 'checks': report['checks']}))
    return 0 if report['passed'] else 1


if __name__ == '__main__':
    raise SystemExit(main())
