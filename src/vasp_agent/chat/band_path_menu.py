"""Operator menu: unified k-path, resource, and single-stage confirmation for a bands-only redo.

The candidates come from band_path.candidates (validated and resolved on the
source's bands cell). Only the operator's answer selects a path and confirms
resources/pre-authorization. On directory name collision (same candidate number
or repeated custom path), the menu never overwrites an existing directory and
proposes <base>_2, <base>_3, ... for operator approval.
"""
from vasp_agent.chat.fork_naming import (
    confirm_reactive_increment,
    conflict_notice,
    existing_jobs,
    is_dir_exists_error,
    resolve_name,
)
from vasp_agent.permissions.unified_confirm import (
    apply_and_preauthorize,
    edit_resources,
    initial_resources,
    resource_section_text,
    unified_options_text,
)
from vasp_agent.ui.menu_text import options, table

ATTEMPTS = 3
MAX_ROUNDS = 16
TITLES = {'automatic': '自动 SC', 'hinuma': 'Hinuma', 'layered': '层状面内', 'extrema': '交叉/带边', 'ai_suggestion': 'AI 建议'}


def job_name(source_job, choice):
    """<source>_path<N>, shortened so it stays within the 64-character task name limit."""
    suffix = f'_path{choice}'
    return source_job[:64 - len(suffix)] + suffix


def menu_text(server, source_job, data, selectable, *, resources=None, resource_source=None):
    rows = [['编号', '来源', 'k 点', '路径']]
    # The path goes last: its Greek letters have ambiguous terminal width, and an
    # unpadded last column cannot push anything out of line.
    for number, item in enumerate(selectable, start=1):
        mark = '*' if item['used_by_first_run'] else ''
        rows.append([number, TITLES.get(item['key'], item['key']) + mark, item['total_kpoints'], item['text']])
    lines = [f"能带路径候选 · {server or 'local'} / {source_job} · {data.get('lattice_type', '')}",
             '【计算参数区】',
             table(rows), '理由：']
    lines += [f"  {number}. {item['rationale']}" for number, item in enumerate(selectable, start=1)]
    if any(item['used_by_first_run'] for item in selectable):
        lines.append('* 第一次计算所用路径')
    if data.get('omitted'):
        lines.append('未列出：' + '；'.join(f"{item.get('title', item['key'])}（{item['reason']}）" for item in data['omitted']))
    res_text, _ = resource_section_text(server, 'bands', resources, resource_source, tests=1)
    lines.append(res_text)
    choices = [('编号', '选择该路径'), ('c', '自定义路径，如 Γ-X-M-Γ|Z-R@60')]
    if resources is not None:
        choices.append(('r', '修改核数与资源'))
    choices.append(('q', '取消'))
    lines.append(options(choices))
    return '\n'.join(lines)


def confirm_text(server, source_job, choice, item, base_name, target_name, resources, resource_source):
    label = TITLES.get(item['key'], item['key'])
    lines = [
        f"能带换路径统一核对 · {server or 'local'} / {source_job}",
        '【计算参数区】',
        f"所选路径（{choice} · {label}）：{item['text']}",
        f'目标目录：{target_name}',
    ]
    if target_name != base_name:
        lines.append('目录冲突提示：' + conflict_notice(base_name, target_name))
    res_text, _ = resource_section_text(server, 'bands', resources, resource_source, tests=1)
    lines.append(res_text)
    lines.append(unified_options_text('bands', edit_param_label='重新选择能带路径', can_submit=resources is not None))
    return '\n'.join(lines)


def _create_with_increment(box, server, source_job, base_name, target_name, band_text, taken, reader, output):
    candidate = target_name
    for _ in range(ATTEMPTS):
        try:
            return candidate, box.prepare_bands_path(server, source_job, candidate, band_text)
        except ValueError as exc:
            if is_dir_exists_error(exc):
                next_candidate = confirm_reactive_increment(base_name, candidate, taken, reader, output)
                if next_candidate is None:
                    return None, None
                candidate = next_candidate
                continue
            raise
    return None, None


def request(box, server, source_job, extra_suggestion=None, extra_rationale=None):
    if server is not None:
        from vasp_agent.remote.remote_compute import RemoteCompute
        box.ensure_current_release(server)
        data = RemoteCompute(box.workspace, server).call('band_path_candidates', source_job,
            extra_suggestion=extra_suggestion, extra_rationale=extra_rationale)
    else:
        from vasp_agent.workflow.band_path import candidates_for_task
        data = candidates_for_task(box.job(source_job), extra_suggestion, extra_rationale)
    selectable = [item for item in data['candidates'] if item.get('selectable')]
    summary = {'candidates': data['candidates'], 'omitted': data.get('omitted', []), 'prepared': False, 'submitted': False}
    if not selectable:
        return {**summary, 'status': 'no_alternatives', 'next': data.get('note', '没有可选路径。')}
    reader = getattr(box, 'plan_reader', None)
    if reader is None:
        return {**summary, 'status': 'operator_confirmation_required',
                'next': '需要操作者在交互菜单中选择路径与核数；模型不能代选，也不要在回复里自拟路径。'}
    output = getattr(box, 'plan_output', print)
    taken = existing_jobs(box, server)
    resources, res_source = initial_resources(box, server, 'bands', tests=1)
    can_submit = resources is not None
    picked = None
    refusals = 0
    menu_shown = False
    for _ in range(MAX_ROUNDS):
        if refusals >= ATTEMPTS:
            break
        if picked is None:
            if not menu_shown:
                output(menu_text(server, source_job, data, selectable, resources=resources, resource_source=res_source))
                menu_shown = True
            prompt = '输入编号、c、r 或 q：' if can_submit else '输入编号、c 或 q：'
            answer = reader(prompt).strip()
            if answer.lower() in ('q', ''):
                break
            if answer.lower() == 'r' and can_submit:
                resources, res_source = edit_resources(box, server, 'bands', resources, res_source, reader, output, tests=1)
                menu_shown = False
                continue
            if answer.lower() == 'c':
                text = reader('输入路径：').strip()
                if not text:
                    output('路径为空，请重新选择。')
                    refusals += 1
                    continue
                picked = ('c', {'key': 'custom', 'text': text})
            elif answer.isdigit() and 1 <= int(answer) <= len(selectable):
                picked = (answer, selectable[int(answer) - 1])
            else:
                refusals += 1
                output('无效选择，请重新输入。')
                continue
        choice, item = picked
        custom = choice == 'c'
        base_name = job_name(source_job, choice)
        target_name, _ = resolve_name(base_name, taken)
        _, res_valid = resource_section_text(server, 'bands', resources, res_source, tests=1)
        output(confirm_text(server, source_job, choice, item, base_name, target_name, resources, res_source))
        prompt = '输入 1、2、3、4 或 q：' if can_submit else '输入 1、2、3 或 q：'
        confirm_answer = reader(prompt).strip().lower()
        if confirm_answer in ('q', ''):
            break
        if confirm_answer == '3':
            picked = None
            menu_shown = False
            continue
        if confirm_answer == '4' and can_submit:
            resources, res_source = edit_resources(box, server, 'bands', resources, res_source, reader, output, tests=1)
            continue
        if confirm_answer not in ('1', '2'):
            refusals += 1
            output('无效选择，请重新输入。')
            continue
        if not res_valid:
            refusals += 1
            output('当前资源方案无效，请先选 4 修改核数与资源。')
            continue
        authorize = (confirm_answer == '1') and can_submit
        try:
            final_name, result = _create_with_increment(box, server, source_job, base_name, target_name,
                                                        item['text'], taken, reader, output)
        except ValueError as exc:
            if not custom or getattr(exc, 'error_code', 'REMOTE_ERROR') not in ('REMOTE_ERROR', 'TOOL_ERROR'):
                raise
            refusals += 1
            output(f'路径未被接受（未创建任务）：{exc}')
            picked = None
            continue
        if final_name is None:
            break
        post = apply_and_preauthorize(box, server, final_name, 'bands', resources, authorize=authorize, tests=1)
        box.log({'band_path_choice': {'server': server, 'source_job': source_job, 'job_name': final_name,
                                      'choice': choice, 'key': item['key'], 'text': item['text'],
                                      'resources': post.get('resources'),
                                      'submission_preauthorized': post['submission_preauthorized']}})
        return {**result, **post, 'status': 'prepared', 'job_name': final_name,
                'selection': {'source': 'operator_menu', 'operator_confirmed': True, 'choice': choice,
                              'key': item['key'], 'text': item['text']}}
    return {**summary, 'status': 'cancelled', 'next': '操作者未选择路径，未创建任务。'}
