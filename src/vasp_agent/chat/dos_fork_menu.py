"""Operator menu: DOS parameters for a DOS-only fork of a task whose SCF passed.

The engine derives the spec's suggestions from the source's verified SCF
evidence (dos_stage.fork_preview) and shows what they would generate. Only the
operator's answer selects: take the shown values, change fields one by one, or
cancel. A pick calls Toolbox.prepare_dos_fork; the engine validates the values
again and refuses inputs it blocks (tetrahedron preconditions, empty bands,
NEDOS) before writing anything, and the menu asks again. The new task still
needs its own dos review and submission grant.
"""
from vasp_agent.ui.menu_text import bullets, options, table

# Refused or invalid answers before the menu gives up; field edits do not count.
ATTEMPTS = 3
MAX_ROUNDS = 12
PREVIEW_KEYS = ('ISMEAR', 'EMIN', 'EMAX', 'NEDOS', 'NBANDS')


def job_name(source_job, values):
    """<source>_dos<hash of the values>: the same choice twice names the same (refused) directory."""
    from vasp_agent.workflow.dos_stage import values_digest
    suffix = '_dos' + values_digest(values)
    return source_job[:64 - len(suffix)] + suffix


def _shown(value, unit):
    return 'null' if value is None else f'{value} {unit}' if unit else str(value)


def menu_text(server, source_job, data, item, values):
    spec = data['spec']
    rows = [['参数', '含义', '当前取值', '规范建议']]
    for field, entry in item['fields'].items():
        rows.append([field, entry['label'], _shown(values[field], entry['unit']), _shown(data['defaults'][field], entry['unit'])])
    scf = data['scf_evidence']
    preview = data['preview']
    lines = [f"DOS 派生 · {server or 'local'} / {source_job} · 规范 {spec['id']} v{spec['version']}：{spec['title']}",
             f"来源 SCF：E_F {scf['scf_fermi_eV']} eV，NBANDS {scf['scf_nbands']}，NELECT {scf['nelect']}，"
             f"网格 {scf['scf_mesh']}；CHGCAR sha256 {data['charge_sha256'][:12]}…（已校验）",
             table(rows, indent=''),
             '依据：\n' + bullets([f"{field}：{entry['basis']}" for field, entry in spec['suggestions'].items()]),
             '规范建议生成的输入：' + '，'.join(f'{k}={preview["incar"][k]}' for k in PREVIEW_KEYS)
             + f"，KPOINTS Γ {preview['kpoints']['mesh']}"]
    if preview['blocked']:
        lines.append('规范建议被程序阻断（请逐项修改）：\n' + bullets(preview['blocked']))
    lines += ['选定后新目录复用来源 SCF（不重算、不改来源），只做 DOS；之后仍需审阅 dos 并单独授权提交。',
              options([('1', '按当前取值派生'), ('2', '逐项修改（字段=值）'), ('q', '取消')])]
    return '\n'.join(lines)


def preview(box, server, source_job):
    if server is not None:
        from vasp_agent.remote.remote_compute import RemoteCompute
        box.ensure_current_release(server)
        return RemoteCompute(box.workspace, server).call('dos_fork_preview', source_job)
    from vasp_agent.workflow.dos_stage import fork_preview
    return fork_preview(box.job(source_job))


def request(box, server, source_job):
    from vasp_agent.planning.parameter_plans import parse_assignments
    from vasp_agent.workflow import parameter_specs
    data = preview(box, server, source_job)
    item = parameter_specs.spec(data['spec']['id'], data['spec']['version'])
    summary = {'spec': {k: data['spec'][k] for k in ('id', 'version', 'title')}, 'defaults': data['defaults'],
               'preview': {k: data['preview'][k] for k in ('incar', 'kpoints', 'blocked')}, 'prepared': False, 'submitted': False}
    reader = getattr(box, 'plan_reader', None)
    if reader is None:
        return {**summary, 'status': 'operator_confirmation_required',
                'next': '需要操作者在交互菜单中选定 DOS 参数；模型不能代选，也不要在回复里自拟参数。'}
    output = getattr(box, 'plan_output', print)
    values = dict(data['defaults'])
    refusals = 0
    for _ in range(MAX_ROUNDS):
        if refusals >= ATTEMPTS:
            break
        output(menu_text(server, source_job, data, item, values))
        answer = reader('输入 1、2 或 q：').strip().lower()
        if answer in ('q', ''):
            break
        if answer == '2':
            raw = reader('输入 字段=值（空格分隔，null 表示空；回车取消修改）：').strip()
            if raw:
                try:
                    values = parameter_specs.validate(item, {**values, **parse_assignments(raw, item)}, data['context'])
                except ValueError as exc:
                    refusals += 1
                    output(f'参数未被接受：{exc}')
            continue
        if answer != '1':
            refusals += 1
            output('无效选择，请重新输入。')
            continue
        try:
            values = parameter_specs.validate(item, values, data['context'])
        except ValueError as exc:
            refusals += 1
            output(f'参数未被接受：{exc}')
            continue
        name = job_name(source_job, values)
        try:
            result = box.prepare_dos_fork(server, source_job, name, values)
        except ValueError as exc:
            # Only a refusal before writing is re-asked; a transport failure could
            # have left a half-known result and is reported as is.
            if getattr(exc, 'error_code', 'REMOTE_ERROR') not in ('REMOTE_ERROR', 'TOOL_ERROR'):
                raise
            refusals += 1
            output(f'未创建任务：{exc}')
            continue
        box.log({'dos_fork_choice': {'server': server, 'source_job': source_job, 'job_name': name,
                                     'spec': data['spec']['version'], 'values': values}})
        return {**result, 'status': 'prepared', 'job_name': name,
                'selection': {'source': 'operator_menu', 'operator_confirmed': True, 'values': values}}
    return {**summary, 'status': 'cancelled', 'next': '操作者未选定 DOS 参数，未创建任务。'}
