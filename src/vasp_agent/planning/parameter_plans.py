"""Persist scan proposals; only the operator menu can accept AI differences."""
import contextlib
import io
import json
import re
from types import SimpleNamespace
from uuid import uuid4

import vasp_agent.workflow.vasp_band_agent as core
from vasp_agent.common import agent_paths
from vasp_agent.planning.plan_menu import (
    RATIONALE_SHOWN,
    RELAX_MODES,
    _mesh,
    _number,
    _shown,
    comparison,
    menu,
    parse_assignments,
    select,
    select_environment,
    select_rest,
    select_stage_parameters,
    stage_table,
)
from vasp_agent.planning.plan_startup import (
    default_physics_goal,
    finalize_prepared,
    planned_convergence_tests,
    select_startup,
    startup_confirmed,
    startup_text,
)
from vasp_agent.workflow.scan_config import validate_scans

__all__ = [
    'RATIONALE_SHOWN',
    'RELAX_MODES',
    '_mesh',
    '_number',
    '_shown',
    'comparison',
    'default_physics_goal',
    'environment_choice',
    'environment_open',
    'environment_payload',
    'finalize_prepared',
    'invoke',
    'list_plans',
    'local_args',
    'menu',
    'parse_assignments',
    'path',
    'planned_convergence_tests',
    'prepare',
    'preview_defaults',
    'propose',
    'require_executable',
    'select',
    'select_environment',
    'select_rest',
    'select_stage_parameters',
    'select_startup',
    'settle',
    'stage_overrides',
    'stage_parameter_drafts',
    'stage_parameters_open',
    'stage_requirements',
    'stage_table',
    'startup_confirmed',
    'startup_text',
    'stated_by_user',
    'summary',
    'with_thresholds',
]


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


def invoke(box, request, preview=False, choice=None, environment=None, solver_overrides=None):
    if request['server'] is not None:
        from vasp_agent.chat.release_check import ensure_current
        from vasp_agent.remote.remote_compute import RemoteCompute
        ensure_current(box, request['server'])
        fields = {k: request[k] for k in ('encut', 'mesh', 'nbands', 'relax_mode', 'parallel_tests', 'cores_per_test')}
        if request.get('scan_preset'):
            fields['scan_preset'] = request['scan_preset']
        if request.get('potential_variants'):
            fields['potential_variants'] = request['potential_variants']
        if request.get('band_path') is not None:
            fields['band_path'] = request['band_path']
        if request.get('optional_stages'):
            fields['optional_stages'] = request['optional_stages']
        if solver_overrides is not None:
            fields['solver_overrides'] = solver_overrides
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
    if solver_overrides is not None:
        args.solver_overrides = solver_overrides
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
            reader = getattr(box, 'plan_reader', None)
            unified = bool(getattr(box, 'unified_plan_confirm', False))
            if reader is not None and (
                saved['status'] == 'pending'
                or (unified and saved['status'] == 'selected' and not startup_confirmed(saved))
            ):
                select(box, saved['id'], reader, getattr(box, 'plan_output', print))
                saved = json.loads(existing.read_text(encoding='utf-8'))
            return summary(saved)
        # Compatibility for the old "query defaults by proposing" conversation:
        # only a default-only, unprepared, never operator-confirmed draft may
        # become an AI proposal. Pending/explicit/operator choices stay protected.
        if (saved['request'] == request and saved['status'] == 'selected'
                and saved.get('choice') == 'no_difference' and saved['origin'] == 'tool_default'
                and not startup_confirmed(saved)
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
    dimension = preview.get('base_config', {}).get('dimensionality', {}).get('dimension')
    document = dict(id=plan_id, request=request, defaults=defaults, suggestion=suggestion, origin=origin,
        rationale=rationale, proposed_sources={'encuts': origin if encuts is not None else 'tool_default',
                                               'meshes': origin if meshes is not None else 'tool_default'}, preview_signature=preview['signature'], potential_floor=preview['potential_floor'],
        user_instruction=getattr(box, 'latest_user_message', ''), status='pending')
    if dimension:
        document['dimension'] = dimension
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
    reader = getattr(box, 'plan_reader', None)
    if bool(getattr(box, 'unified_plan_confirm', False)) and reader is not None:
        # Unified startup mode requires explicit operator confirmation in the 3-in-1 window;
        # cancelling in the menu keeps the plan pending so prepare_scan_plan cannot proceed.
        document['status'] = 'pending'
    if previous:
        document['previous_proposal'] = previous
        document['revision_reason'] = '默认草案补充 AI 建议，需操作者菜单选择。'
    target = path(box, plan_id)
    target.parent.mkdir(parents=True, exist_ok=True)
    core.save(target, document)
    box.log({'scan_plan_proposed': plan_id, 'status': document['status'],
             'revised_default_draft': previous is not None})
    if document['status'] == 'pending' and reader is not None:
        select(box, plan_id, reader, getattr(box, 'plan_output', print))
    return summary(json.loads(target.read_text(encoding='utf-8')))


def summary(plan):
    choice = plan.get('choice')
    options = {'operator_default': '1', 'operator_accept_ai': '2', 'operator_custom': '3'}
    unified_done = startup_confirmed(plan)
    operator_selected = (choice in options or unified_done) and plan['status'] in ('selected', 'prepared')
    extra = {'environment': plan['environment']} if 'environment' in plan else {}
    if unified_done:
        startup = plan['startup_confirmation']
        extra['startup_confirmation'] = {
            k: startup[k]
            for k in ('operator_confirmed', 'confirmed_by', 'physics_goal', 'resources',
                      'preauthorize_requested', 'phase')
            if k in startup
        }
    if 'stage_parameters' in plan:
        extra['stage_parameters'] = {name: {
            'spec': entry['spec'], 'suggestion': entry['suggestion'], 'proposed_fields': entry['proposed_fields'],
            'selected': entry['selected'], 'choice': entry['choice'],
            'operator_confirmed': entry['selected'] is not None,
            'status': 'selected' if entry['selected'] is not None else 'operator_confirmation_required'}
            for name, entry in plan['stage_parameters'].items()}
    startup_fields = ({
        'physics_confirmed': True,
        'resources_confirmed': plan['startup_confirmation'].get('resources') is not None,
        'preauthorize_requested': bool(plan['startup_confirmation'].get('preauthorize_requested')),
    } if unified_done else {})
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
            **startup_fields,
            'scope': 'startup_unified' if unified_done else 'scan_parameters_only',
            'submission_authorized': False,
            'message': ('用户已在三合一启动核对窗口确认物理目标、参数与首阶段资源；请调用 prepare_scan_plan 准备任务，无需重复询问或重开确认菜单。'
                        if unified_done else
                        '用户已在程序菜单中作出选择；这就是用户确认，不是模型自动选中。'
                        '不要重复询问 A/B 或要求再次确认相同参数。按原请求继续准备；若原请求仅要草案，则汇报已选方案。'
                        if operator_selected else '按状态处理；没有参数差异或用户已明确指定时无需重复选择。')},
        'next': {'selected': 'prepare_scan_plan', 'pending': '/plan', 'prepared': 'get_stage_review'}[plan['status']]}


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


def prepare(box, plan_id, *, solver_overrides=None):
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
                    environment=environment_payload(box, plan['request'], plan.get('environment')),
                    solver_overrides=solver_overrides)
    result = finalize_prepared(box, plan, result)
    plan.update(status='prepared', result=result)
    core.save(target, plan)
    return result


def list_plans(box):
    entries = []
    for candidate in agent_paths.store_items(box.workspace, 'scan_plans', '*.json'):
        try:
            plan = json.loads(candidate.read_text(encoding='utf-8'))
            entries.append((candidate.stat().st_mtime, summary(plan)))
        except (OSError, ValueError, KeyError):
            continue
    entries.sort(key=lambda item: item[0], reverse=True)
    open_entries = [item for item in entries if item[1].get('status') in ('pending', 'selected')]
    prepared_entries = [item for item in entries if item[1].get('status') not in ('pending', 'selected')]
    kept = sorted(open_entries + prepared_entries[:max(0, 20 - len(open_entries))],
                  key=lambda item: item[0], reverse=True)
    return {'plans': [item[1] for item in kept], 'total': len(entries)}
