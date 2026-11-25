"""Unified 3-in-1 startup confirmation window for new /plan tasks.

Combines three operator confirmations into one menu for new tasks:
  Block A: Physical model scope & research goal (writes physics-model.json on prepare)
  Block B: Scan sequences, preset, optional stage parameters, and run environment
  Block C: First-stage (convergence) submission resources & 1-shot pre-authorization

Security invariants:
- Model tool arguments can never confirm physics, resources, or submission.
- Pre-authorization lives in memory on Toolbox (`pending_plan_preauth`) for the
  live session only, is popped on prepare, and is strictly bound to one
  (server, job_name, 'convergence') submission.
- Prepared tasks still require get_stage_review -> record_stage_review(allowed=true)
  and full remote/local preflight before sbatch.
"""
from __future__ import annotations

import json

import vasp_agent.workflow.vasp_band_agent as core
from vasp_agent.chat.chat_commands import printable
from vasp_agent.permissions import unified_confirm
from vasp_agent.planning.plan_menu import RATIONALE_SHOWN, comparison, parse_assignments, stage_table
from vasp_agent.ui.menu_text import bullets, options, points, table
from vasp_agent.workflow.scan_config import validate_scans

MAX_ROUNDS = 24
GOAL_LIMIT = 2000
SCAN_CHOICE_LABELS = {
    'operator_default': '工具默认', 'operator_accept_ai': 'AI 建议',
    'operator_custom': '操作者自定义', 'user_explicit': '用户明确指定', 'no_difference': '工具默认（无差异）',
}
STAGE_CHOICE_LABELS = {
    'operator_spec_default': '规范建议', 'operator_accept_ai': 'AI 建议', 'operator_custom': '操作者自定义',
}


def startup_confirmed(plan: dict) -> bool:
    """True only when the operator completed the 3-in-1 startup menu for this plan."""
    entry = plan.get('startup_confirmation')
    return (isinstance(entry, dict) and entry.get('operator_confirmed') is True
            and entry.get('confirmed_by') == 'operator_chat_menu'
            and isinstance(entry.get('physics_goal'), str) and bool(entry['physics_goal'].strip()))


def planned_convergence_tests(plan: dict) -> int:
    """Count independent convergence tests for a new task (3 ENCUT + 3 k-mesh = 6)."""
    selected = plan.get('selected') or plan.get('suggestion') or plan.get('defaults') or {}
    total = len(selected.get('encuts') or []) + len(selected.get('meshes') or [])
    return total if total > 0 else 6


def default_physics_goal(plan: dict) -> str:
    """Generate an editable initial research goal suggestion from plan metadata."""
    req = plan.get('request') or {}
    dim = plan.get('dimension')
    dim_label = '二维 slab' if dim == '2D' else '三维体相' if dim == '3D' else '有序体系'
    relax = '晶胞与原子优化' if req.get('relax_mode') == 'cell' else '固定晶胞优化原子'
    optional = [name for name in (req.get('optional_stages') or []) if isinstance(name, str)]
    opt_suffix = f"及可选阶段（{'、'.join(optional)}）" if optional else ''
    return f"非磁性{dim_label} PBE 收敛测试、{relax}、能带{opt_suffix}计算（任务 {req.get('job_name') or 'new-task'}）"[:GOAL_LIMIT]


def _initial_startup_resources(box, plan: dict, tests: int) -> tuple[dict | None, str | None]:
    req = plan.get('request') or {}
    resources, source = unified_confirm.initial_resources(box, req.get('server'), 'convergence', tests=tests)
    if resources is None:
        return None, None
    saved = (plan.get('startup_confirmation') or {}).get('resources')
    if isinstance(saved, dict):
        return dict(saved), '本方案已配置资源'
    updated, changed = dict(resources), False
    if req.get('parallel_tests') is not None:
        updated['parallel_tests'] = max(1, min(6, tests, int(req['parallel_tests'])))
        changed = True
    if req.get('cores_per_test') is not None:
        updated['cores_per_test'] = max(1, int(req['cores_per_test']))
        changed = True
    if changed:
        if updated['cores_per_test'] % max(1, updated['nodes']):
            updated['nodes'] = 1
        source = f'方案请求 + {source}' if source else '方案请求配置'
    return updated, source


def _render_block_a(goal: str) -> str:
    return '\n'.join([
        '【区块 A · 物理模型与研究目标】',
        '固定物理模型：电中性、非磁性 PBE；不含 SOC、DFT+U 或杂化泛函。体系维度（三维体相或二维 slab）由结构自动检测并绑定哈希。',
        '不支持磁性、SOC、DFT+U、杂化泛函或一维/零维体系；确认后随准备写入 physics-model.json（confirmed_by=operator_chat_menu）。',
        '研究目标（按 3 修改）：' + printable(goal),
    ])


def _render_block_b(plan: dict, scan_choice: str, stage_state: dict, env_selected: str | None) -> str:
    from vasp_agent.ui.scan_retry_summary import preset_line, threshold_lines
    from vasp_agent.workflow import parameter_specs

    lines = ['【区块 B · 扫描参数、可选阶段与环境配置】', comparison(plan)]
    preset = plan['request'].get('scan_preset')
    lines.append(preset_line(preset) + '（按 s 切换档位）')
    if plan['request'].get('potential_variants'):
        lines.append('赝势变体：' + '、'.join(f"{v['element']}={v['variant']}" for v in plan['request']['potential_variants']))
    if plan['suggestion'].get('scan_thresholds'):
        lines.extend(threshold_lines(plan['suggestion']['scan_thresholds'], preset))
    label = SCAN_CHOICE_LABELS.get(scan_choice, scan_choice)
    suffix = '（按 4 可切换工具默认 / AI 建议 / 自定义；两种方案都要经实际收敛测试验证）' if plan['defaults'] != plan['suggestion'] else ''
    lines.append(f'当前拟采用扫描序列：{label}{suffix}')
    reasons = points(printable(plan.get('rationale', '')))
    if reasons:
        lines.append('AI 理由（按 d 查看完整理由）：\n' + bullets(reasons, RATIONALE_SHOWN))
    for name, state in stage_state.items():
        entry = plan['stage_parameters'][name]
        item = parameter_specs.spec(entry['spec']['id'], entry['spec']['version'])
        lines.append(f"\n{name} 阶段参数 · 规范 {item['id']} v{item['version']}（当前拟采用：{STAGE_CHOICE_LABELS.get(state['choice'], state['choice'])}；按 4 修改）")
        lines.append(stage_table(entry, item))
        lines.append('关联：' + '；'.join(item['dependencies']) + '（建议是起点，执行端按规范再次校验）')
    if 'environment' in plan:
        env = plan['environment']
        needed = '、'.join(env['required']) or '无特殊能力'
        if env_selected is not None:
            lines.append(f'运行环境（需要 {needed}）：{env_selected}（按 4 可更换）')
        else:
            lines.append(f"运行环境（需要 {needed}）：尚未选定（候选：{'、'.join(row['name'] for row in env['candidates'])}；确认前请先按 4 选择环境）")
    return '\n'.join(lines)


def startup_text(plan: dict, *, goal: str, scan_choice: str, stage_state: dict, env_selected: str | None,
                 resources: dict | None, resource_source: str | None, tests: int = 6) -> tuple[str, bool]:
    """Render the full 3-in-1 startup confirmation window and return (text, resources_valid)."""
    req = plan['request']
    block_c, res_valid = unified_confirm.resource_section_text(
        req['server'], 'convergence', resources, resource_source, tests=tests)
    if resources is not None:
        footer_items = [
            ('1', '确认物理目标、参数与核数，准备并在审阅通过后直接提交首阶段（convergence）'),
            ('2', '确认物理目标、参数与核数，仅准备（暂不提交）'),
            ('3', '修改物理研究目标（区块 A）'),
            ('4', '修改扫描/阶段/环境参数（区块 B）'),
            ('5', '修改首阶段核数与资源（区块 C，支持 模式=array 并发=... 每组=... 节点=... 分区=... 时限=...）'),
            ('q', '暂不决定 / 取消（不建目录、不授权）'),
        ]
        note = '说明：选 1 仅预授权本任务的 convergence 阶段（1 次）；落盘后仍须通过阶段审阅与远端预检才提交，不跨阶段连授。'
    else:
        footer_items = [
            ('1', '确认物理目标与参数，准备新目录（本机未配置提交脚本，仅准备不授权）'),
            ('2', '确认物理目标与参数，仅准备（暂不提交）'),
            ('3', '修改物理研究目标（区块 A）'),
            ('4', '修改扫描/阶段/环境参数（区块 B）'),
            ('q', '暂不决定 / 取消（不建目录、不授权）'),
        ]
        note = '说明：本机未配置提交脚本，选 1 或 2 均仅确认物理目标与参数、不授予提交权。'
    sections = [
        f"新任务启动核对（三合一）· {req['server'] or 'local'} / {req['job_name']}",
        _render_block_a(goal),
        _render_block_b(plan, scan_choice, stage_state, env_selected),
        block_c,
        options(footer_items) + '\n' + note,
    ]
    return '\n\n'.join(sections), res_valid


def _edit_scan(plan: dict, scan_selected: dict, scan_choice: str, scan_sources: dict, reader, output):
    from vasp_agent.planning import parameter_plans as plans
    from vasp_agent.workflow import dimensionality
    from vasp_agent.workflow.full_workflow import validate_scan_thresholds

    output(options([('1', '采用工具默认'), ('2', '采用 AI 建议'),
                    ('3', '自定义 JSON（encuts / meshes / 可选 scan_thresholds）'), ('q', '返回')]))
    ans = reader('选择扫描序列（1/2/3/q）：').strip().lower()
    if ans == '1':
        return plan['defaults'], 'operator_default', {'encuts': 'operator_default', 'meshes': 'operator_default'}
    if ans == '2':
        return plan['suggestion'], 'operator_accept_ai', dict(plan['proposed_sources'])
    if ans == '3':
        raw = reader('输入 JSON，包含 encuts 和 meshes 完整三级序列，可选 scan_thresholds（回车/q 取消）：').strip()
        if raw in ('', 'q'):
            return scan_selected, scan_choice, scan_sources
        try:
            custom = json.loads(raw)
            if not isinstance(custom, dict) or not {'encuts', 'meshes'} <= set(custom) <= {'encuts', 'meshes', 'scan_thresholds'}:
                raise ValueError('自定义 JSON 需要 encuts 和 meshes，可选 scan_thresholds。')
            selected = plans.with_thresholds(validate_scans(custom['encuts'], custom['meshes']),
                                             validate_scan_thresholds(custom.get('scan_thresholds')))
            if plan.get('dimension') == '2D':
                dimensionality.check_meshes(selected['meshes'])
            if selected['encuts'][0] < plan['potential_floor']:
                raise ValueError('ENCUT 起点低于工具策略下限。')
        except ValueError as exc:
            output(f'扫描序列未修改：{exc}')
            return scan_selected, scan_choice, scan_sources
        return selected, 'operator_custom', {'encuts': 'operator_custom', 'meshes': 'operator_custom'}
    return scan_selected, scan_choice, scan_sources


def _edit_stage_params(plan: dict, stage_state: dict, reader, output) -> None:
    from vasp_agent.workflow import parameter_specs

    names = list(stage_state)
    if not names:
        output('当前方案没有可选阶段参数。')
        return
    target_name = names[0]
    if len(names) > 1:
        output(options([(str(i), name) for i, name in enumerate(names, 1)] + [('q', '返回')]))
        pick = reader('选择要修改的可选阶段编号：').strip().lower()
        if not pick.isdigit() or not 1 <= int(pick) <= len(names):
            return
        target_name = names[int(pick) - 1]
    entry = plan['stage_parameters'][target_name]
    item = parameter_specs.spec(entry['spec']['id'], entry['spec']['version'])
    items = [('1', '采用规范建议')] + ([('2', '采用 AI 建议')] if entry['proposed_fields'] else []) + [('3', '逐项修改（字段=值）'), ('q', '返回')]
    output(f'{target_name} 阶段参数修改：\n' + options(items))
    ans = reader('输入编号：').strip().lower()
    try:
        if ans == '1':
            stage_state[target_name] = {'selected': parameter_specs.validate(item, entry['defaults'], entry['context']), 'choice': 'operator_spec_default'}
        elif ans == '2' and entry['proposed_fields']:
            stage_state[target_name] = {'selected': parameter_specs.validate(item, entry['suggestion'], entry['context']), 'choice': 'operator_accept_ai'}
        elif ans == '3':
            raw = reader('输入 字段=值（空格分隔，null 表示空；回车/q 取消）：').strip()
            if raw and raw.lower() != 'q':
                merged = {**stage_state[target_name]['selected'], **parse_assignments(raw, item)}
                stage_state[target_name] = {'selected': parameter_specs.validate(item, merged, entry['context']), 'choice': 'operator_custom'}
    except ValueError as exc:
        output(f'{target_name} 阶段参数未修改：{exc}')


def _edit_environment(plan: dict, env_selected: str | None, reader, output) -> str | None:
    env = plan.get('environment')
    if not env:
        output('当前方案没有可切换的运行环境。')
        return env_selected
    rows = [['编号', '环境配置', 'VASP', '能力', '环境脚本']]
    for number, row in enumerate(env['candidates'], 1):
        rows.append([number, row['name'] + ('（隐式默认）' if row['implicit'] else ''), row.get('version') or '-',
                     '、'.join(row['capabilities']) or '-', row['env_script']])
    output(table(rows))
    ans = reader('选择环境编号（回车/q 返回）：').strip()
    if ans.isascii() and ans.isdigit() and 1 <= int(ans) <= len(env['candidates']):
        return env['candidates'][int(ans) - 1]['name']
    return env_selected


def _edit_block_b(plan: dict, scan_selected: dict, scan_choice: str, scan_sources: dict,
                  stage_state: dict, env_selected: str | None, reader, output):
    has_stages = bool(stage_state)
    has_env = 'environment' in plan and len(plan['environment'].get('candidates', [])) > 1
    if not has_stages and not has_env:
        return (*_edit_scan(plan, scan_selected, scan_choice, scan_sources, reader, output), env_selected)
    choices = [('a', '修改扫描序列（工具默认 / AI 建议 / 自定义）')]
    if has_stages:
        choices.append(('b', '修改可选阶段参数'))
    if has_env:
        choices.append(('c', '选择运行环境'))
    output(options(choices + [('q', '返回')]))
    ans = reader('选择区块 B 修改项：').strip().lower()
    if ans == 'a':
        scan_selected, scan_choice, scan_sources = _edit_scan(plan, scan_selected, scan_choice, scan_sources, reader, output)
    elif ans == 'b' and has_stages:
        _edit_stage_params(plan, stage_state, reader, output)
    elif ans == 'c' and has_env:
        env_selected = _edit_environment(plan, env_selected, reader, output)
    return scan_selected, scan_choice, scan_sources, env_selected


def _initial_state(plan: dict):
    goal = (plan.get('startup_confirmation') or {}).get('physics_goal') or default_physics_goal(plan)
    if plan.get('selected') is not None:
        scan_selected = plan['selected']
        scan_choice = plan.get('choice') or ('user_explicit' if plan.get('origin') == 'user_explicit' else 'no_difference')
        scan_sources = dict(plan.get('selected_sources') or plan.get('proposed_sources') or {})
    elif plan['defaults'] != plan['suggestion']:
        scan_selected, scan_choice, scan_sources = plan['suggestion'], 'operator_accept_ai', dict(plan['proposed_sources'])
    else:
        scan_selected = plan['defaults']
        scan_choice = 'user_explicit' if plan.get('origin') == 'user_explicit' else 'no_difference'
        scan_sources = dict(plan['proposed_sources'])
    stage_state = {
        name: ({'selected': dict(e['selected']), 'choice': e['choice']} if e.get('selected') is not None
               else {'selected': dict(e['suggestion']), 'choice': 'operator_accept_ai' if e['proposed_fields'] else 'operator_spec_default'})
        for name, e in plan.get('stage_parameters', {}).items()
    }
    env_selected = plan['environment']['selected'] if 'environment' in plan else None
    return goal, scan_selected, scan_choice, scan_sources, stage_state, env_selected


def select_startup(box, plan_id: str, reader, output):
    """Run the unified 3-in-1 startup confirmation window for a scan plan."""
    from vasp_agent.planning import parameter_plans as plans
    from vasp_agent.ui.scan_retry_summary import preset_line
    from vasp_agent.workflow import parameter_specs

    target = plans.path(box, plan_id)
    plan = json.loads(target.read_text(encoding='utf-8'))
    if plan['status'] == 'prepared' or (plan['status'] == 'selected' and startup_confirmed(plan)):
        output(json.dumps(plans.summary(plan), ensure_ascii=False, indent=2))
        return
    server, job_name = plan['request']['server'], plan['request']['job_name']
    goal, scan_selected, scan_choice, scan_sources, stage_state, env_selected = _initial_state(plan)
    tests = planned_convergence_tests(plan)
    resources, res_source = _initial_startup_resources(box, plan, tests)
    can_submit = resources is not None
    prompt = '输入 1、2、3、4、5 或 q（d 完整理由；s 切档位）：' if can_submit else '输入 1、2、3、4 或 q（d 完整理由；s 切档位）：'
    shown_dirty, res_valid = True, True
    for _ in range(MAX_ROUNDS):
        if shown_dirty:
            text, res_valid = startup_text(
                plan, goal=goal, scan_choice=scan_choice, stage_state=stage_state,
                env_selected=env_selected, resources=resources, resource_source=res_source, tests=tests)
            output(text)
            shown_dirty = False
        answer = reader(prompt).strip().lower()
        if not answer:
            output('未收到选项，仍在等待；请输入 1/2/3/4' + ('/5' if can_submit else '') + '，或 q 暂不决定。')
            continue
        if answer == 'd':
            reasons = points(printable(plan.get('rationale', '')))
            output('完整理由：\n' + bullets(reasons) if reasons else '无补充理由。')
            continue
        if answer == 's':
            preset = 'strict' if (plan['request'].get('scan_preset') or 'strict') == 'standard' else 'standard'
            plan['request']['scan_preset'] = preset
            core.save(target, plan)
            box.log({'scan_plan_preset': plan_id, 'preset': preset})
            output('已切换：' + preset_line(preset))
            shown_dirty = True
            continue
        if answer in ('q', '取消'):
            getattr(box, 'pending_plan_preauth', {}).pop(plan_id, None)
            output('方案保持待确认，未生成正式任务，未授予提交权。稍后输入 /plan 继续确认。')
            return
        if answer == '3':
            raw = reader('输入新的研究目标（不超过 2000 字；回车/q 保持原目标）：').strip()
            if raw and raw.lower() != 'q':
                if len(raw) > GOAL_LIMIT:
                    output(f'研究目标不能超过 {GOAL_LIMIT} 字，未修改。')
                else:
                    goal = raw
            shown_dirty = True
            continue
        if answer == '4':
            scan_selected, scan_choice, scan_sources, env_selected = _edit_block_b(
                plan, scan_selected, scan_choice, scan_sources, stage_state, env_selected, reader, output)
            shown_dirty = True
            continue
        if answer == '5' and can_submit:
            resources, res_source = unified_confirm.edit_resources(
                box, server, 'convergence', resources, res_source, reader, output, tests=tests)
            shown_dirty = True
            continue
        if answer not in ('1', '2'):
            output('无效选择，请重新输入。')
            continue
        if 'environment' in plan and env_selected is None:
            output('运行环境尚未选定，请先选 4 选择运行环境。')
            continue
        if not res_valid:
            output('当前资源方案无效，请先选 5 修改核数与资源。')
            continue
        if scan_selected['encuts'][0] < plan['potential_floor']:
            output('ENCUT 起点低于工具策略下限，请先选 4 修改扫描序列。')
            continue
        validated_stages = {}
        try:
            for name, state in stage_state.items():
                entry = plan['stage_parameters'][name]
                item = parameter_specs.spec(entry['spec']['id'], entry['spec']['version'])
                validated_stages[name] = {'selected': parameter_specs.validate(item, state['selected'], entry['context']), 'choice': state['choice']}
        except ValueError as exc:
            output(f'阶段参数无效（请先选 4 修改）：{exc}')
            continue
        authorize = (answer == '1') and can_submit
        plan.update(selected=scan_selected, selected_sources=scan_sources, choice=scan_choice)
        for name, vals in validated_stages.items():
            plan['stage_parameters'][name].update(selected=vals['selected'], choice=vals['choice'])
        if 'environment' in plan and plan['environment'].get('selected') != env_selected:
            plan['environment'].update(selected=env_selected, source='operator_menu')
        plan['startup_confirmation'] = {
            'operator_confirmed': True, 'confirmed_by': 'operator_chat_menu',
            'physics_goal': goal.strip(), 'resources': resources.copy() if resources is not None else None,
            'preauthorize_requested': bool(authorize), 'phase': 'convergence',
        }
        plans.settle(plan)
        core.save(target, plan)
        if not hasattr(box, 'pending_plan_preauth'):
            box.pending_plan_preauth = {}
        if authorize:
            box.pending_plan_preauth[plan_id] = {
                'server': server, 'job_name': job_name, 'phase': 'convergence',
                'resources': resources.copy(), 'authorize': True,
            }
        else:
            box.pending_plan_preauth.pop(plan_id, None)
        box.log({'scan_plan_startup_confirmed': plan_id, 'choice': scan_choice, 'physics_confirmed': True,
                 'resources': resources, 'preauthorize_requested': bool(authorize)})
        output('已确认物理目标、参数与首阶段资源，并登记 convergence 1 次预授权（准备后仍须通过审阅与预检才提交）。'
               if authorize else '已确认物理目标、参数与资源（仅准备，不授予提交权）。')
        return
    getattr(box, 'pending_plan_preauth', {}).pop(plan_id, None)
    output('操作次数已达上限；方案保持待确认，未生成正式任务。稍后输入 /plan 继续。')


def finalize_prepared(box, plan: dict, result: dict) -> dict:
    """Apply operator-confirmed physics goal, resources, and 1-shot convergence pre-auth after prepare."""
    if not startup_confirmed(plan):
        return result
    from vasp_agent.chat import stage_commands

    startup = plan['startup_confirmation']
    server, job_name, plan_id = plan['request']['server'], plan['request']['job_name'], plan['id']
    pending = getattr(box, 'pending_plan_preauth', {}).pop(plan_id, None)
    physics_confirmed, physics_record, physics_error = False, None, None
    try:
        physics_record = stage_commands.save_confirmation(box, server, job_name, startup['physics_goal'], 'operator_chat_menu')
        physics_confirmed = True
    except Exception as exc:
        physics_error = str(exc)[:400]
        box.log({'startup_physics_confirm_error': {'server': server, 'job_name': job_name, 'plan_id': plan_id, 'error': physics_error}})
    if not physics_confirmed:
        post = unified_confirm.apply_and_preauthorize(
            box, server, job_name, 'convergence', None, authorize=False, tests=planned_convergence_tests(plan), requires_physics=True)
        return {**result, **post, 'job_name': job_name, 'physics_confirmed': False,
                'physics_uncertain': True, 'physics_error': physics_error,
                'next': (f'新目录 {job_name} 已创建，但写入物理模型确认失败（未授予提交权）：{physics_error}；'
                         '请先核查状态并用 request_physics_confirmation 补齐物理确认，勿重复准备或提交。')}
    authorize = bool(
        isinstance(pending, dict) and pending.get('authorize') is True and startup.get('preauthorize_requested') is True
        and pending.get('server') == server and pending.get('job_name') == job_name and pending.get('phase') == 'convergence'
    )
    post = unified_confirm.apply_and_preauthorize(
        box, server, job_name, 'convergence', startup.get('resources'),
        authorize=authorize, tests=planned_convergence_tests(plan), requires_physics=False)
    return {**result, **post, 'job_name': job_name, 'physics_confirmed': True, 'physics': physics_record}
