"""Persist scan proposals; only the operator menu can accept AI differences."""
import contextlib
import io
import json
import re
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import vasp_agent.workflow.vasp_band_agent as core
from vasp_agent.common import agent_paths
from vasp_agent.workflow.scan_config import validate_scans


def path(box, plan_id):
    if not re.fullmatch(r'[0-9a-f]{32}', plan_id):
        raise ValueError('无效参数方案编号。')
    return agent_paths.store_item(box.workspace, 'scan_plans', plan_id + '.json')


def local_args(box, request):
    return SimpleNamespace(structure=str(box.path(request['structure'])), workdir=str(box.job(request['job_name'])),
        potcar=str(box.potcar) if box.potcar else None,
        potcar_library=str(box.potcar_library) if box.potcar_library else None,
        potential_variants=request.get('potential_variants'),
        encut=request['encut'], mesh=request['mesh'], nbands=request['nbands'], relax_mode=request['relax_mode'],
        parallel_tests=request['parallel_tests'] or 1, cores_per_test=request['cores_per_test'] or 8,
        scan_preset=request.get('scan_preset'), band_path=request.get('band_path'),
        ediff=1e-7, points=40, symprec=.01, force_tol=.01, stress_tol=1., energy_tol=.001, nsw=200)


def invoke(box, request, preview=False, choice=None, environment=None):
    if request['server'] is not None:
        from vasp_agent.chat.release_check import ensure_current
        from vasp_agent.remote.remote_compute import RemoteCompute
        ensure_current(box, request['server'])
        fields ={k: request[k] for k in ('encut', 'mesh', 'nbands', 'relax_mode', 'parallel_tests', 'cores_per_test')}
        if request.get('scan_preset'):
            fields['scan_preset'] = request['scan_preset']
        if request.get('potential_variants'):
            fields['potential_variants'] = request['potential_variants']
        if request.get('band_path') is not None:
            fields['band_path'] = request['band_path']
        if request.get('optional_stages'):
            fields['optional_stages'] = request['optional_stages']
        if choice:
            fields.update(choice['selected'], parameter_choice=choice)  # may carry scan_thresholds
            if choice.get('stage_parameters'):
                fields['stage_parameters'] = choice['stage_parameters']
        return RemoteCompute(box.workspace, request['server']).prepare(request['job_name'], request['structure'],
            **fields, preview_only=preview, environment_profile=environment)
    from vasp_agent.workflow.full_workflow import prepare_full
    args = local_args(box, request)
    args.preview_only = preview
    args.optional_stages = request.get('optional_stages')
    if choice:
        args.stage_parameters = choice.get('stage_parameters')
        args.encuts, args.meshes = choice['selected']['encuts'], choice['selected']['meshes']
        args.scan_thresholds = choice['selected'].get('scan_thresholds')
        args.parameter_choice = choice
    with contextlib.redirect_stdout(io.StringIO()):
        result = prepare_full(args)
    if preview:
        return result
    from vasp_agent.workflow.input_review import preparation_summary
    return {**box.get_status(request['job_name']), 'preparation_summary': preparation_summary(box.job(request['job_name']))}


def preview_defaults(box, request):
    result = invoke(box, request, preview=True)
    from pymatgen.core import Structure
    from vasp_agent.planning.kmesh import suggest_meshes
    suggestion = suggest_meshes(Structure.from_file(box.path(request['structure'])))
    blocked = [name for name, item in result.get('stage_specs', {}).items() if not item['executable']]
    return {'kmesh_suggestion': suggestion, **result, 'preview_only': True, 'plan_saved': False,
            'next': 'propose_scan_plan',
            'note': '仅预览真实默认参数；未选择、未生成正式任务，尚未验证收敛。'
                    + (f"可选阶段 {'、'.join(blocked)} 尚不可执行：只能查看参数规范，不能保存方案或准备任务。" if blocked else '')}


def stage_overrides(request, stage_parameters):
    """AI-proposed stage values as {stage: {field: value}}; field checks follow against the spec."""
    optional = request.get('optional_stages') or []
    overrides = {}
    for item in stage_parameters or []:
        if not isinstance(item, dict) or set(item) != {'stage', 'field', 'value'}:
            raise ValueError('stage_parameters 每项须为 {stage, field, value}。')
        if item['stage'] not in optional:
            raise ValueError(f"阶段 {item['stage']} 未在 optional_stages 中启用。")
        if item['field'] in overrides.setdefault(item['stage'], {}):
            raise ValueError(f"阶段参数 {item['stage']}.{item['field']} 重复。")
        overrides[item['stage']][item['field']] = item['value']
    return overrides


def require_executable(optional):
    """Refuse before anything is saved when a requested optional stage cannot run yet."""
    from vasp_agent.workflow import parameter_specs
    for name in optional or []:
        item = parameter_specs.for_stage(name)
        if not item['executable']:
            raise core.ToolError('STAGE_NOT_EXECUTABLE', f"可选阶段 {name} 尚不可执行：{item['not_executable_reason']}"
                                 '可用 preview_scan_defaults 查看其参数规范；未保存方案。')


def stage_parameter_drafts(preview, overrides, rationale):
    """Per optional stage: spec suggestions with their basis, the AI's changes, nothing selected yet."""
    from vasp_agent.workflow import parameter_specs
    drafts = {}
    for name, described in preview.get('stage_specs', {}).items():
        if not described['executable']:
            raise core.ToolError('STAGE_NOT_EXECUTABLE', f"可选阶段 {name} 尚不可执行：{described['not_executable_reason']}")
        item = parameter_specs.spec(described['id'], described['version'])
        context = {'dimension': preview['base_config']['dimensionality']['dimension']}
        defaults = {field: entry['value'] for field, entry in described['suggestions'].items()}
        changes = overrides.get(name, {})
        suggestion = parameter_specs.merged(item, context, changes)
        if changes and not rationale.strip():
            raise ValueError('AI 修改阶段参数必须说明理由。')
        drafts[name] = {'spec': parameter_specs.reference(item), 'context': context, 'defaults': defaults,
                        'basis': {field: entry['basis'] for field, entry in described['suggestions'].items()},
                        'suggestion': suggestion, 'proposed_fields': sorted(changes),
                        'selected': None, 'choice': None}
    missing = set(overrides) - set(drafts)
    if missing:
        raise ValueError('未知可选阶段：' + '、'.join(sorted(missing)))
    return drafts


def stage_parameters_open(plan):
    return any(entry['selected'] is None for entry in plan.get('stage_parameters', {}).values())


def settle(plan):
    """selected once the scans, every stage's parameters and the environment are chosen."""
    done = plan.get('selected') is not None and not stage_parameters_open(plan) and not environment_open(plan)
    plan['status'] = 'selected' if done else 'pending'


def stated_by_user(box, encuts, meshes, thresholds=None):
    """Check that the operator's own message contains the numbers being claimed.

    `origin='user_explicit'` skips the selection menu, on the reasoning that the
    user already said the numbers and should not be asked twice. That makes the
    label an assertion of operator intent held only by the model: a tool argument
    of `user_explicit` selected a scan sequence no operator ever saw. Binding the
    claim to the recorded message turns it into something checkable -- each
    proposed number must appear as a number in what the user typed. Digits are
    matched with a boundary so that 520 is not found inside 5200.
    """
    message = getattr(box, 'latest_user_message', '') or ''
    numbers = list(encuts or []) + [value for row in (meshes or []) for value in row]
    numbers += [f'{value:g}' for value in (thresholds or {}).values()]
    return all(re.search(r'(?<![\d.])' + re.escape(str(value)) + r'(?![\d.])', message)
               for value in numbers)


def with_thresholds(scans, thresholds):
    """A scan suggestion, carrying operator-relaxable scan thresholds when proposed."""
    return {**scans, 'scan_thresholds': thresholds} if thresholds else scans


def propose(box, request, encuts, meshes, origin, rationale, scan_thresholds=None, stage_parameters=None):
    from vasp_agent.workflow.full_workflow import validate_scan_thresholds
    scan_thresholds = validate_scan_thresholds(scan_thresholds)
    if origin not in ('ai_suggestion', 'user_explicit', 'tool_default'):
        raise ValueError('参数来源无效。')
    if origin == 'user_explicit' and encuts is None and meshes is None and not scan_thresholds:
        raise ValueError('用户明确指定模式需要完整 ENCUT 或网格序列。')
    if origin == 'tool_default' and scan_thresholds:
        raise ValueError('默认模式不能夹带放宽的扫描阈值。')
    if origin == 'user_explicit' and not stated_by_user(box, encuts, meshes, scan_thresholds):
        raise core.ToolError('ORIGIN_NOT_OPERATOR_STATED',
            '用户消息中没有出现所声称的序列，不能标记为 user_explicit。'
            '请改用 ai_suggestion 并说明理由，由程序菜单让用户选择。')
    if origin == 'tool_default' and (encuts is not None or meshes is not None):
        raise ValueError('默认模式不能夹带建议值。')
    require_executable(request.get('optional_stages'))
    overrides = stage_overrides(request, stage_parameters)
    previous = None
    for existing in agent_paths.store_items(box.workspace, 'scan_plans', '*.json'):
        saved = json.loads(existing.read_text(encoding='utf-8'))
        if saved['request']['server'] != request['server'] or saved['request']['job_name'] != request['job_name']:
            continue
        suggestion = with_thresholds(validate_scans(encuts if encuts is not None else saved['defaults']['encuts'],
                                                    meshes if meshes is not None else saved['defaults']['meshes']), scan_thresholds)
        sources = {'encuts': origin if encuts is not None else 'tool_default',
                   'meshes': origin if meshes is not None else 'tool_default'}
        same = (saved['request'] == request and saved['suggestion'] == suggestion
                and saved['origin'] == origin and saved['proposed_sources'] == sources
                and saved.get('stage_parameter_overrides', {}) == overrides)
        if same:
            if saved['status'] == 'pending' and getattr(box, 'plan_reader', None) is not None:
                select(box, saved['id'], box.plan_reader, getattr(box, 'plan_output', print))
                saved = json.loads(existing.read_text(encoding='utf-8'))
            return summary(saved)
        # Compatibility for the old "query defaults by proposing" conversation:
        # only a default-only, unprepared, never operator-confirmed draft may
        # become an AI proposal. Pending/explicit/operator choices stay protected.
        if (saved['request'] == request and saved['status'] == 'selected'
                and saved.get('choice') == 'no_difference' and saved['origin'] == 'tool_default'
                and origin == 'ai_suggestion' and suggestion != saved['defaults']):
            previous = saved
            break
        raise core.ToolError('SCAN_PLAN_CONFLICT',
            '同名草案与本次请求不同，未覆盖原选择。草案 ID：' + saved['id'] +
            '。请读取 list_scan_plans 核对原方案；继续原方案，或由用户指定新任务名重新拟定。')
    environment = environment_choice(box, request)
    preview = invoke(box, request, preview=True, environment=environment_payload(box, request, environment))
    defaults = preview['defaults']
    suggestion = with_thresholds(validate_scans(encuts if encuts is not None else defaults['encuts'],
                                                meshes if meshes is not None else defaults['meshes']), scan_thresholds)
    if suggestion['encuts'][0] < preview['potential_floor']:
        raise ValueError('建议 ENCUT 低于工具赝势/优化策略下限，请重新提出方案。')
    if suggestion != defaults and origin == 'ai_suggestion' and not rationale.strip():
        raise ValueError('AI 建议必须说明修改理由、资源影响和待验证事项。')
    plan_id = previous['id'] if previous else uuid4().hex
    document = dict(id=plan_id, request=request, defaults=defaults, suggestion=suggestion, origin=origin,
        rationale=rationale, proposed_sources={'encuts': origin if encuts is not None else 'tool_default',
                                               'meshes': origin if meshes is not None else 'tool_default'}, preview_signature=preview['signature'], potential_floor=preview['potential_floor'],
        user_instruction=getattr(box, 'latest_user_message', ''), status='pending')
    if origin == 'user_explicit' or suggestion == defaults:
        document.update(status='selected', selected=suggestion, selected_sources=document['proposed_sources'],
            choice='user_explicit' if origin == 'user_explicit' else 'no_difference')
    if request.get('optional_stages'):
        # Stage parameters are always the operator's choice in the menu, even when
        # nobody proposed a change: the spec suggestion is only a starting point.
        document['stage_parameters'] = stage_parameter_drafts(preview, overrides, rationale)
        document['stage_parameter_overrides'] = overrides
    if environment is not None:
        document['environment'] = environment
    settle(document)  # pending while the environment or any stage's parameters are open
    if previous:
        document['previous_proposal'] = previous
        document['revision_reason'] = '默认草案补充 AI 建议，需操作者菜单选择。'
    target = path(box, plan_id)
    target.parent.mkdir(parents=True, exist_ok=True)
    core.save(target, document)
    box.log({'scan_plan_proposed': plan_id, 'status': document['status'],
             'revised_default_draft': previous is not None})
    reader = getattr(box, 'plan_reader', None)
    if document['status'] == 'pending' and reader is not None:
        select(box, plan_id, reader, getattr(box, 'plan_output', print))
    return summary(json.loads(target.read_text(encoding='utf-8')))


def summary(plan):
    choice = plan.get('choice')
    options = {'operator_default': '1', 'operator_accept_ai': '2', 'operator_custom': '3'}
    operator_selected = choice in options and plan['status'] in ('selected', 'prepared')
    extra = {'environment': plan['environment']} if 'environment' in plan else {}
    if 'stage_parameters' in plan:
        extra['stage_parameters'] = {name: {
            'spec': entry['spec'], 'suggestion': entry['suggestion'], 'proposed_fields': entry['proposed_fields'],
            'selected': entry['selected'], 'choice': entry['choice'],
            'operator_confirmed': entry['selected'] is not None,
            'status': 'selected' if entry['selected'] is not None else 'operator_confirmation_required'}
            for name, entry in plan['stage_parameters'].items()}
    return {k: plan[k] for k in ('id', 'status', 'request', 'defaults', 'suggestion', 'rationale')} | extra | {
        'selected': plan.get('selected'), 'choice': choice,
        'revision_reason': plan.get('revision_reason'),
        'selected_sources': plan.get('selected_sources'),
        'selection': {
            'source': 'operator_menu' if operator_selected else choice,
            'operator_confirmed': operator_selected,
            'menu_option': options.get(choice),
            'requires_parameter_choice': plan['status'] == 'pending' and plan.get('selected') is None,
            **({'requires_environment_choice': environment_open(plan)} if 'environment' in plan else {}),
            **({'requires_stage_parameter_choice': stage_parameters_open(plan)} if 'stage_parameters' in plan else {}),
            'scope': 'scan_parameters_only',
            'submission_authorized': False,
            'message': ('用户已在程序菜单中作出选择；这就是用户确认，不是模型自动选中。'
                        '不要重复询问 A/B 或要求再次确认相同参数。按原请求继续准备；若原请求仅要草案，则汇报已选方案。'
                        if operator_selected else '按状态处理；没有参数差异或用户已明确指定时无需重复选择。')},
        'next': {'selected':'prepare_scan_plan', 'pending':'/plan', 'prepared':'get_stage_review'}[plan['status']]}


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
    import math
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
    target = path(box, plan_id)
    plan = json.loads(target.read_text(encoding='utf-8'))
    if plan['status'] != 'pending':
        output(json.dumps(summary(plan), ensure_ascii=False, indent=2))
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
        selected = with_thresholds(validate_scans(custom['encuts'], custom['meshes']),
                                   validate_scan_thresholds(custom.get('scan_thresholds')))
        source = 'operator_custom'
    else:
        raise ValueError('选择无效，方案仍待确认。')
    if selected['encuts'][0] < plan['potential_floor']:
        raise ValueError('ENCUT 起点低于工具策略下限，方案仍待确认。')
    sources = plan['proposed_sources'] if answer == '2' else {k: source for k in ('encuts', 'meshes')}
    plan.update(selected=selected, selected_sources=sources, choice=source)
    if stage_parameters_open(plan) or environment_open(plan):
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
    for name, entry in plan.get('stage_parameters', {}).items():
        if entry['selected'] is None and not select_stage_parameters(box, plan_id, target, plan, name, reader, output):
            return
    if environment_open(plan):
        select_environment(box, plan_id, target, plan, reader, output)
        return
    settle(plan)
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
    menu = [('1', '采用规范建议')] + ([('2', '采用 AI 建议')] if entry['proposed_fields'] else [])
    output('\n请选择：\n' + options(menu + [('3', '逐项修改'), ('4', '暂不决定')]))
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


def environment_choice(box, request):
    """The run environment for a new remote task, or None when no profile is registered (legacy).

    One matching profile is used and named in the plan; several are left to
    the operator in this menu; none refuses before anything is prepared.
    """
    from vasp_agent.remote import environment_profiles as profiles
    server = request['server']
    if server is None or not profiles.registered(box.workspace, server):
        return None
    choice = profiles.choose(box.workspace, server, request['relax_mode'], stage_requirements(request))
    if choice['status'] == 'none':
        raise profiles.unregistered_error(server, choice)
    names = [row['name'] for row in choice['matches']]
    one = choice['status'] == 'one'
    return {'required': choice['required'], 'candidates': [profiles.brief(row) for row in choice['matches']],
            'excluded': choice['excluded'], 'selected': names[0] if one else None,
            'source': 'only_match' if one else None,
            'message': (f'唯一满足条件的环境配置：{names[0]}' if one
                        else f"{len(names)} 个环境配置满足条件（{'、'.join(names)}），由操作者在 /plan 菜单中选择")}


def stage_requirements(request):
    """Capability tags the task's stages (legacy chain plus optional stages) need."""
    from vasp_agent.remote import environment_profiles as profiles
    from vasp_agent.workflow import stage_definitions as stages
    return profiles.requirements(stages.record(stages.with_optional(request.get('optional_stages'))))


def environment_open(plan):
    return 'environment' in plan and plan['environment']['selected'] is None


def environment_payload(box, request, environment):
    """The profile record to send for the selected environment; None for the implicit default."""
    if not environment or environment['selected'] is None:
        return None
    from vasp_agent.remote import environment_profiles as profiles
    choice = profiles.choose(box.workspace, request['server'], request['relax_mode'], environment['required'])
    row = next((row for row in choice['matches'] if row['name'] == environment['selected']), None)
    if row is None:
        profiles.by_name(box.workspace, request['server'], environment['selected'])  # gone: say so
        raise core.ToolError('ENVIRONMENT_CAPABILITY_MISSING',
            f"环境配置 {environment['selected']} 已不再满足 {'、'.join(environment['required']) or '本任务'}；请重新提出方案。")
    return profiles.payload(row)


def select_environment(box, plan_id, target, plan, reader, output):
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
    settle(plan)
    core.save(target, plan)
    box.log({'scan_plan_environment': plan_id, 'environment': name})
    output(f'你的选择已确认并保存（运行环境 {name}），将回传给 AI；无需再次选择。尚未生成或提交计算。')


def prepare(box, plan_id):
    target = path(box, plan_id)
    plan = json.loads(target.read_text(encoding='utf-8'))
    if plan['status'] == 'prepared':
        return {'already_prepared': True, 'result': plan['result']}
    if plan['status'] != 'selected':
        raise ValueError('方案尚未选择，请使用 /plan；不能默认用户同意。')
    choice = {k: plan[k] for k in ('id', 'preview_signature', 'defaults', 'suggestion', 'selected', 'origin', 'choice', 'rationale', 'user_instruction', 'proposed_sources', 'selected_sources')}
    if plan.get('stage_parameters'):
        if stage_parameters_open(plan):
            raise ValueError('阶段参数尚未由操作者选择，请使用 /plan；不能默认用户同意。')
        choice['stage_parameters'] = {name: {'spec': entry['spec'], 'values': entry['selected']}
                                      for name, entry in plan['stage_parameters'].items()}
        choice['stage_parameter_choices'] = {name: entry['choice'] for name, entry in plan['stage_parameters'].items()}
    result = invoke(box, plan['request'], choice=choice,
                    environment=environment_payload(box, plan['request'], plan.get('environment')))
    plan.update(status='prepared', result=result)
    core.save(target, plan)
    return result


def menu(c, arg):
    if arg:
        if not re.fullmatch(r'[0-9a-f]{32}', arg) or not path(c.agent.tools, arg).exists():
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
    select(c.agent.tools, entries[int(answer)-1][1]['id'], c.reader, c.output)
    return True


def list_plans(box):
    entries = []
    for candidate in agent_paths.store_items(box.workspace, 'scan_plans', '*.json'):
        try:
            plan = json.loads(candidate.read_text(encoding='utf-8'))
            entries.append((candidate.stat().st_mtime, summary(plan)))
        except (OSError, ValueError, KeyError):
            continue
    entries.sort(key=lambda item: item[0], reverse=True)
    return {'plans': [item[1] for item in entries[:30]], 'total': len(entries)}
