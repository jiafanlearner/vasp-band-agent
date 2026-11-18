"""Operator menu: pick one of the engine's k-path candidates (or type one) for a bands-only redo.

The candidates come from band_path.candidates (validated and resolved on the
source's bands cell). Only the operator's answer selects; the model can add at
most one suggestion, which goes through the same validator and is labelled.
A pick calls Toolbox.prepare_bands_path, so the new task still needs its own
bands review and submission grant.
"""
from vasp_agent.ui.menu_text import options, table

ATTEMPTS = 3
TITLES = {'automatic': '自动 SC', 'hinuma': 'Hinuma', 'layered': '层状面内', 'extrema': '交叉/带边', 'ai_suggestion': 'AI 建议'}


def job_name(source_job, choice):
    """<source>_path<N>, shortened so it stays within the 64-character task name limit."""
    suffix = f'_path{choice}'
    return source_job[:64 - len(suffix)] + suffix


def menu_text(server, source_job, data, selectable):
    rows = [['编号', '来源', 'k 点', '路径']]
    # The path goes last: its Greek letters have ambiguous terminal width, and an
    # unpadded last column cannot push anything out of line.
    for number, item in enumerate(selectable, start=1):
        mark = '*' if item['used_by_first_run'] else ''
        rows.append([number, TITLES.get(item['key'], item['key']) + mark, item['total_kpoints'], item['text']])
    lines = [f"能带路径候选 · {server or 'local'} / {source_job} · {data.get('lattice_type', '')}",
             table(rows), '理由：']
    lines += [f"  {number}. {item['rationale']}" for number, item in enumerate(selectable, start=1)]
    if any(item['used_by_first_run'] for item in selectable):
        lines.append('* 第一次计算所用路径')
    if data.get('omitted'):
        lines.append('未列出：' + '；'.join(f"{item.get('title', item['key'])}（{item['reason']}）" for item in data['omitted']))
    lines += ['选中后在新目录复用来源 SCF，只重算能带；之后仍需审阅 bands 并单独授权提交。',
              options([('编号', '选择该路径'), ('c', '自定义路径，如 Γ-X-M-Γ|Z-R@60'), ('q', '取消')])]
    return '\n'.join(lines)


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
                'next': '需要操作者在交互菜单中选择路径；模型不能代选，也不要在回复里自拟路径。'}
    output = getattr(box, 'plan_output', print)
    output(menu_text(server, source_job, data, selectable))
    for _ in range(ATTEMPTS):
        answer = reader('输入编号、c 或 q：').strip()
        if answer.lower() in ('q', ''):
            break
        custom = answer.lower() == 'c'
        if custom:
            choice, item = 'c', {'key': 'custom', 'text': reader('输入路径：').strip()}
        elif answer.isdigit() and 1 <= int(answer) <= len(selectable):
            choice, item = answer, selectable[int(answer) - 1]
        else:
            output('无效选择，请重新输入。')
            continue
        name = job_name(source_job, choice)
        try:
            result = box.prepare_bands_path(server, source_job, name, item['text'])
        except ValueError as exc:
            # Only a path the engine refused before writing is re-asked; a transport
            # failure could have left a half-known result and is reported as is.
            if not custom or getattr(exc, 'error_code', 'REMOTE_ERROR') not in ('REMOTE_ERROR', 'TOOL_ERROR'):
                raise
            output(f'路径未被接受（未创建任务）：{exc}')
            continue
        box.log({'band_path_choice': {'server': server, 'source_job': source_job, 'job_name': name,
                                      'choice': choice, 'key': item['key'], 'text': item['text']}})
        return {**result, 'status': 'prepared', 'job_name': name,
                'selection': {'source': 'operator_menu', 'operator_confirmed': True, 'choice': choice,
                              'key': item['key'], 'text': item['text']}}
    return {**summary, 'status': 'cancelled', 'next': '操作者未选择路径，未创建任务。'}
