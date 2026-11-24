"""Interactive operator menus and comparison tables for scan/stage/environment plans."""
import json
import math
import re

import vasp_agent.workflow.vasp_band_agent as core
from vasp_agent.common import agent_paths
from vasp_agent.workflow.scan_config import validate_scans


RATIONALE_SHOWN = 3


def _number(value):
    return str(int(value)) if float(value).is_integer() else f'{value:g}'


def _mesh(row):
    return f'{row[0]}³' if len(set(row)) == 1 else '×'.join(str(n) for n in row)


# Shown in the plan menu: the operator must see whether the cell will change.
RELAX_MODES = {'ions': '只优化原子位置，固定晶胞（ISIF=2；保留输入晶格，如实验晶格）',
               'cell': '晶胞与原子一起优化（ISIF=3；晶格改为 PBE 平衡值，需检查应力收敛）'}


def comparison(plan):
    """Side-by-side table of the two scan sequences, so the operator compares instead of parsing JSON."""
    from vasp_agent.ui.menu_text import table
    default, suggestion = plan['defaults'], plan['suggestion']
    counts = [sum(math.prod(row) for row in data['meshes']) for data in (default, suggestion)]
    rows = [['', '工具默认', 'AI 建议', '']]
    for name, a, b in (('ENCUT (eV)', ' / '.join(_number(x) for x in default['encuts']),
                        ' / '.join(_number(x) for x in suggestion['encuts'])),
                       ('k 网格', ' / '.join(_mesh(r) for r in default['meshes']),
                        ' / '.join(_mesh(r) for r in suggestion['meshes']))):
        rows.append([name, a, b, '（相同）' if a == b else '（不同）'])
    rows.append(['网格点总数', counts[0], counts[1],
                 '' if counts[0] == counts[1] else f'（×{counts[1] / counts[0]:.2f}，仅规模参考，不代表耗时）'])
    relaxed = suggestion.get('scan_thresholds')
    if relaxed:
        from vasp_agent.ui.scan_retry_summary import DEFAULT_SCAN_THRESHOLDS, THRESHOLD_LABELS
        for key, value in relaxed.items():
            label, unit = THRESHOLD_LABELS[key]
            rows.append([f'{label}阈值', f'{DEFAULT_SCAN_THRESHOLDS[key]:g} {unit}', f'{value:g} {unit}', '（放宽）'])
    changed = [name for name, key in (('ENCUT', 'encuts'), ('k 网格', 'meshes')) if default[key] != suggestion[key]]
    changed += ['扫描阈值'] if relaxed else []
    mode = RELAX_MODES.get(plan['request'].get('relax_mode'), str(plan['request'].get('relax_mode')))
    lines = [f"收敛扫描参数方案 · {plan['request']['server'] or 'local'} / {plan['request']['job_name']}", f'优化方式：{mode}']
    if plan['request'].get('band_path') is not None:
        from vasp_agent.workflow.band_path import render
        lines.append('能带路径：' + render(plan['request']['band_path']) + '（用户指定；最终坐标在 bands 审阅中显示）')
    return '\n'.join(lines + [table(rows, indent=''),
                      '差异：' + ('、'.join(changed) + ' 不同' if changed else '无')])


def select(box, plan_id, reader, output):
    from vasp_agent.chat.chat_commands import printable
    from vasp_agent.planning import parameter_plans as plans
    target = plans.path(box, plan_id)
    plan = json.loads(target.read_text(encoding='utf-8'))
    if plan['status'] != 'pending':
        output(json.dumps(plans.summary(plan), ensure_ascii=False, indent=2))
        return
    if plan.get('selected') is not None:
        # Scan parameters are settled (no difference, user_explicit, or chosen
        # earlier); stage parameters and/or the run environment are still open.
        output(comparison(plan))
        select_rest(box, plan_id, target, plan, reader, output)
        return
    from vasp_agent.ui.scan_retry_summary import preset_line, threshold_lines
    output(comparison(plan))
    preset = plan['request'].get('scan_preset')
    output(preset_line(preset) + '（两种方案共用；输入 s 切换档位）')
    if plan['suggestion'].get('scan_thresholds'):
        output('\n'.join(threshold_lines(plan['suggestion']['scan_thresholds'], preset)) + '\n选 1 不放宽；选 2 采用放宽的阈值。')
    from vasp_agent.ui.menu_text import bullets, options, points
    reasons = points(printable(plan['rationale']))
    if reasons:
        output('AI 理由：\n' + bullets(reasons, RATIONALE_SHOWN))
    output('两种方案都要经实际收敛测试验证；AI 建议不代表更准确。')
    output('\n请选择：\n' + options([('1', '保留工具默认'), ('2', '采用 AI 建议'), ('3', '自定义参数'), ('4', '暂不决定')]))
    while True:
        answer = reader('输入编号（d 看完整理由；s 切换判据档位；4/q 暂不决定）：').strip()
        if answer == 'd':
            output('完整理由：\n' + bullets(reasons))
            continue
        if answer.lower() == 's':
            # The preset belongs to the task, not to either column; record the operator's switch.
            preset = 'strict' if (preset or 'strict') == 'standard' else 'standard'
            plan['request']['scan_preset'] = preset
            core.save(target, plan)
            box.log({'scan_plan_preset': plan_id, 'preset': preset})
            output('已切换：' + preset_line(preset))
            continue
        if answer:
            break
        output('未收到选项，仍在等待；请输入 1/2/3，或 4/q 暂不决定。')
    if answer in ('q', '4'):
        output('方案保持待确认，未生成正式任务。稍后输入 /plan 继续选择。')
        return
    if answer == '1':
        selected, source = plan['defaults'], 'operator_default'
    elif answer == '2':
        selected, source = plan['suggestion'], 'operator_accept_ai'
    elif answer == '3':
        raw = reader('输入 JSON，包含 encuts 和 meshes 完整三级序列，可选 scan_thresholds（回车/q 取消）：').strip()
        if raw in ('', 'q'):
            return
        custom = json.loads(raw)
        if not isinstance(custom, dict) or not {'encuts', 'meshes'} <= set(custom) <= {'encuts', 'meshes', 'scan_thresholds'}:
            raise ValueError('自定义 JSON 需要 encuts 和 meshes，可选 scan_thresholds。方案仍待确认。')
        from vasp_agent.workflow.full_workflow import validate_scan_thresholds
        selected = plans.with_thresholds(validate_scans(custom['encuts'], custom['meshes']),
                                         validate_scan_thresholds(custom.get('scan_thresholds')))
        source = 'operator_custom'
    else:
        raise ValueError('选择无效，方案仍待确认。')
    if selected['encuts'][0] < plan['potential_floor']:
        raise ValueError('ENCUT 起点低于工具策略下限，方案仍待确认。')
    sources = plan['proposed_sources'] if answer == '2' else {k: source for k in ('encuts', 'meshes')}
    plan.update(selected=selected, selected_sources=sources, choice=source)
    if plans.stage_parameters_open(plan) or plans.environment_open(plan):
        core.save(target, plan)
        box.log({'scan_plan_selected': plan_id, 'choice': source, 'selected': selected})
        select_rest(box, plan_id, target, plan, reader, output)
        return
    plan['status'] = 'selected'
    core.save(target, plan)
    box.log({'scan_plan_selected': plan_id, 'choice': source, 'selected': selected})
    output('你的选择已确认并保存，将回传给 AI；无需再次选择相同参数。尚未生成或提交计算。')


def select_rest(box, plan_id, target, plan, reader, output):
    """After the scan parameters: each open stage's parameters, then the environment."""
    from vasp_agent.planning import parameter_plans as plans
    for name, entry in plan.get('stage_parameters', {}).items():
        if entry['selected'] is None and not select_stage_parameters(box, plan_id, target, plan, name, reader, output):
            return
    if plans.environment_open(plan):
        select_environment(box, plan_id, target, plan, reader, output)
        return
    plans.settle(plan)
    core.save(target, plan)
    output('你的选择已确认并保存，将回传给 AI；无需再次选择相同参数。尚未生成或提交计算。')


def _shown(value, unit):
    if value is None:
        return 'null'
    return (f'{value:g}' if isinstance(value, float) else str(value)) + (f' {unit}' if unit else '')


def stage_table(entry, item):
    from vasp_agent.ui.menu_text import table
    rows = [['参数', '含义', '规范建议', 'AI 建议', '']]
    for field, spec_field in item['fields'].items():
        rows.append([field, spec_field['label'], _shown(entry['defaults'][field], spec_field['unit']),
                     _shown(entry['suggestion'][field], spec_field['unit']),
                     '（AI 修改）' if field in entry['proposed_fields'] else ''])
    return table(rows, indent='')


def parse_assignments(raw, item):
    """'field=value ...' typed by the operator: numbers, listed choices or null only."""
    values = {}
    for part in raw.replace('，', ' ').replace(',', ' ').split():
        if '=' not in part:
            raise ValueError(f'无法解析 {part!r}：请写成 字段=值。方案仍待确认。')
        field, text = part.split('=', 1)
        if field not in item['fields']:
            raise ValueError(f'未知参数 {field}；可修改：' + '、'.join(item['fields']) + '。方案仍待确认。')
        kind = item['fields'][field]['kind']
        if text == 'null':
            values[field] = None
        elif kind == 'choice':
            values[field] = next((c for c in item['fields'][field]['choices'] if str(c) == text), text)
        else:
            try:
                values[field] = int(text) if kind == 'integer' else float(text)
            except ValueError:
                raise ValueError(f'{field} 必须是数值。方案仍待确认。') from None
    return values


def select_stage_parameters(box, plan_id, target, plan, name, reader, output):
    """One stage's parameter menu: the spec suggestion, the AI suggestion, or the operator's own values."""
    from vasp_agent.ui.menu_text import bullets, options
    from vasp_agent.workflow import parameter_specs
    entry = plan['stage_parameters'][name]
    item = parameter_specs.spec(entry['spec']['id'], entry['spec']['version'])
    output(f"\n{name} 阶段参数 · 规范 {item['id']} v{item['version']}：{item['title']}\n" + stage_table(entry, item))
    output('依据：\n' + bullets([f'{field}：{text}' for field, text in entry['basis'].items()]))
    output('关联：' + '；'.join(item['dependencies']) + '\n建议是起点，不是收敛证明；执行端按规范再次严格校验。')
    menu_items = [('1', '采用规范建议')] + ([('2', '采用 AI 建议')] if entry['proposed_fields'] else [])
    output('\n请选择：\n' + options(menu_items + [('3', '逐项修改'), ('4', '暂不决定')]))
    answer = reader('输入编号（4/q 暂不决定）：').strip()
    values = None
    if answer == '1':
        values, choice = entry['defaults'], 'operator_spec_default'
    elif answer == '2' and entry['proposed_fields']:
        values, choice = entry['suggestion'], 'operator_accept_ai'
    elif answer == '3':
        raw = reader('输入 字段=值（空格分隔，null 表示空；在规范建议上修改；回车/q 取消）：').strip()
        if raw not in ('', 'q'):
            values, choice = {**entry['defaults'], **parse_assignments(raw, item)}, 'operator_custom'
    if values is None:
        core.save(target, plan)
        output(f'{name} 阶段参数尚未选择；方案保持待确认，未生成正式任务。稍后输入 /plan 继续。')
        return False
    entry.update(selected=parameter_specs.validate(item, values, entry['context']), choice=choice)
    core.save(target, plan)
    box.log({'stage_parameters_selected': plan_id, 'stage': name, 'choice': choice, 'values': entry['selected']})
    return True


def select_environment(box, plan_id, target, plan, reader, output):
    from vasp_agent.planning import parameter_plans as plans
    from vasp_agent.ui.menu_text import table
    environment = plan['environment']
    rows = [['编号', '环境配置', 'VASP', '能力', '环境脚本']]
    for number, row in enumerate(environment['candidates'], 1):
        rows.append([number, row['name'] + ('（隐式默认）' if row['implicit'] else ''), row.get('version') or '-',
                     '、'.join(row['capabilities']) or '-', row['env_script']])
    needed = '、'.join(environment['required']) or '无特殊能力'
    output(f'\n运行环境 · 启用阶段需要：{needed}\n' + table(rows))
    if environment['excluded']:
        output('不满足：' + '；'.join(f"{row['name']}（{row['reason']}）" for row in environment['excluded']))
    output('任务将绑定所选环境的脚本与可执行文件哈希；之后哈希变化会拒绝提交。')
    answer = reader('选择环境编号（q 暂不决定）：').strip()
    if not answer.isascii() or not answer.isdigit() or not 1 <= int(answer) <= len(environment['candidates']):
        core.save(target, plan)
        output('运行环境尚未选择；方案保持待确认，未生成正式任务。稍后输入 /plan 继续。')
        return
    name = environment['candidates'][int(answer) - 1]['name']
    environment.update(selected=name, source='operator_menu')
    plans.settle(plan)
    core.save(target, plan)
    box.log({'scan_plan_environment': plan_id, 'environment': name})
    output(f'你的选择已确认并保存（运行环境 {name}），将回传给 AI；无需再次选择。尚未生成或提交计算。')


def menu(c, arg):
    from vasp_agent.planning import parameter_plans as plans
    if arg:
        if not re.fullmatch(r'[0-9a-f]{32}', arg) or not plans.path(c.agent.tools, arg).exists():
            return False  # Preserve /plan job-name for existing calculations.
        select(c.agent.tools, arg, c.reader, c.output)
        return True
    entries = []
    for candidate in agent_paths.store_items(c.agent.tools.workspace, 'scan_plans', '*.json'):
        try:
            plan = json.loads(candidate.read_text(encoding='utf-8'))
            if plan['status'] in ('pending', 'selected'):
                entries.append((candidate.stat().st_mtime, plan))
        except (OSError, ValueError, KeyError):
            continue
    entries.sort(key=lambda row: row[0], reverse=True)
    if not entries:
        return False
    for i, (_, plan) in enumerate(entries, 1):
        c.output(f"{i}. {plan['request']['server'] or 'local'} / {plan['request']['job_name']} | {plan['status']} | {plan['id']}")
    answer = c.reader('选择方案编号（回车/q 取消）：').strip()
    if answer in ('', 'q'):
        return True
    if not answer.isascii() or not answer.isdigit() or not 1 <= int(answer) <= len(entries):
        raise ValueError('编号无效。')
    select(c.agent.tools, entries[int(answer) - 1][1]['id'], c.reader, c.output)
    return True
