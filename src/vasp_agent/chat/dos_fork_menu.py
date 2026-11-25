"""Operator menu: unified DOS parameter, resource, and single-stage confirmation for a DOS-only fork.

The engine derives the spec's suggestions from the source's verified SCF
evidence (dos_stage.fork_preview) and shows what they would generate alongside
the submission resource section. Only the operator's answer selects: confirm
and pre-authorize this dos stage, confirm prepare-only, edit DOS fields, edit
resources, or cancel. On directory name collision, the menu never overwrites an
existing directory and proposes <base>_2, <base>_3, ... for operator approval.
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
from vasp_agent.ui.menu_text import bullets, table

# Refused or invalid answers before the menu gives up; field/resource edits do not count.
ATTEMPTS = 3
MAX_ROUNDS = 16
PREVIEW_KEYS = ('ISMEAR', 'EMIN', 'EMAX', 'NEDOS', 'NBANDS')


def job_name(source_job, values):
    """<source>_dos<hash of the values>: base directory name within 64 chars."""
    from vasp_agent.workflow.dos_stage import values_digest
    suffix = '_dos' + values_digest(values)
    return source_job[:64 - len(suffix)] + suffix


def _shown(value, unit):
    return 'null' if value is None else f'{value} {unit}' if unit else str(value)


def menu_text(server, source_job, data, item, values, *, target_name=None, base_name=None,
              resources=None, resource_source=None):
    spec = data['spec']
    rows = [['参数', '含义', '当前取值', '规范建议']]
    for field, entry in item['fields'].items():
        rows.append([field, entry['label'], _shown(values[field], entry['unit']), _shown(data['defaults'][field], entry['unit'])])
    scf = data['scf_evidence']
    preview_data = data['preview']
    lines = [f"DOS 派生统一核对 · {server or 'local'} / {source_job} · 规范 {spec['id']} v{spec['version']}：{spec['title']}",
             f"来源 SCF：E_F {scf['scf_fermi_eV']} eV，NBANDS {scf['scf_nbands']}，NELECT {scf['nelect']}，"
             f"网格 {scf['scf_mesh']}；CHGCAR sha256 {data['charge_sha256'][:12]}…（已校验）",
             '【计算参数区】',
             table(rows, indent=''),
             '依据：\n' + bullets([f"{field}：{entry['basis']}" for field, entry in spec['suggestions'].items()]),
             '规范建议生成的输入：' + '，'.join(f'{k}={preview_data["incar"][k]}' for k in PREVIEW_KEYS)
             + f"，KPOINTS Γ {preview_data['kpoints']['mesh']}"]
    if target_name:
        lines.append(f'目标目录：{target_name}')
        if base_name and target_name != base_name:
            lines.append('目录冲突提示：' + conflict_notice(base_name, target_name))
    if preview_data['blocked']:
        lines.append('规范建议被程序阻断（请选 3 逐项修改）：\n' + bullets(preview_data['blocked']))
    res_text, _ = resource_section_text(server, 'dos', resources, resource_source, tests=1)
    lines.append(res_text)
    lines.append(unified_options_text('dos', edit_param_label='修改计算参数（字段=值）', can_submit=resources is not None))
    return '\n'.join(lines)


def preview(box, server, source_job):
    if server is not None:
        from vasp_agent.remote.remote_compute import RemoteCompute
        box.ensure_current_release(server)
        return RemoteCompute(box.workspace, server).call('dos_fork_preview', source_job)
    from vasp_agent.workflow.dos_stage import fork_preview
    return fork_preview(box.job(source_job))


def _create_with_increment(box, server, source_job, base_name, target_name, values, taken, reader, output):
    """Create the DOS fork, asking the operator to confirm _2/_3 if a race collision occurs."""
    candidate = target_name
    for _ in range(ATTEMPTS):
        try:
            return candidate, box.prepare_dos_fork(server, source_job, candidate, values)
        except ValueError as exc:
            if is_dir_exists_error(exc):
                next_candidate = confirm_reactive_increment(base_name, candidate, taken, reader, output)
                if next_candidate is None:
                    return None, None
                candidate = next_candidate
                continue
            raise
    return None, None


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
                'next': '需要操作者在交互菜单中选定 DOS 参数与核数；模型不能代选，也不要在回复里自拟参数。'}
    output = getattr(box, 'plan_output', print)
    values = dict(data['defaults'])
    taken = existing_jobs(box, server)
    resources, res_source = initial_resources(box, server, 'dos', tests=1)
    can_submit = resources is not None
    refusals = 0
    for _ in range(MAX_ROUNDS):
        if refusals >= ATTEMPTS:
            break
        base_name = job_name(source_job, values)
        target_name, _ = resolve_name(base_name, taken)
        _, res_valid = resource_section_text(server, 'dos', resources, res_source, tests=1)
        output(menu_text(server, source_job, data, item, values, target_name=target_name,
                         base_name=base_name, resources=resources, resource_source=res_source))
        prompt = '输入 1、2、3、4 或 q：' if can_submit else '输入 1、2、3 或 q：'
        answer = reader(prompt).strip().lower()
        if answer in ('q', ''):
            break
        if answer == '3':
            raw = reader('输入 字段=值（空格分隔，null 表示空；回车取消修改）：').strip()
            if raw:
                try:
                    values = parameter_specs.validate(item, {**values, **parse_assignments(raw, item)}, data['context'])
                except ValueError as exc:
                    refusals += 1
                    output(f'参数未被接受：{exc}')
            continue
        if answer == '4' and can_submit:
            resources, res_source = edit_resources(box, server, 'dos', resources, res_source, reader, output, tests=1)
            continue
        if answer not in ('1', '2'):
            refusals += 1
            output('无效选择，请重新输入。')
            continue
        if not res_valid:
            refusals += 1
            output('当前资源方案无效，请先选 4 修改核数与资源。')
            continue
        try:
            values = parameter_specs.validate(item, values, data['context'])
        except ValueError as exc:
            refusals += 1
            output(f'参数未被接受：{exc}')
            continue
        authorize = (answer == '1') and can_submit
        try:
            final_name, result = _create_with_increment(box, server, source_job, base_name, target_name,
                                                        values, taken, reader, output)
        except ValueError as exc:
            if getattr(exc, 'error_code', 'REMOTE_ERROR') not in ('REMOTE_ERROR', 'TOOL_ERROR'):
                raise
            refusals += 1
            output(f'未创建任务：{exc}')
            continue
        if final_name is None:
            break
        post = apply_and_preauthorize(box, server, final_name, 'dos', resources, authorize=authorize, tests=1)
        box.log({'dos_fork_choice': {'server': server, 'source_job': source_job, 'job_name': final_name,
                                     'spec': data['spec']['version'], 'values': values,
                                     'resources': post.get('resources'),
                                     'submission_preauthorized': post['submission_preauthorized']}})
        return {**result, **post, 'status': 'prepared', 'job_name': final_name,
                'selection': {'source': 'operator_menu', 'operator_confirmed': True, 'values': values}}
    return {**summary, 'status': 'cancelled', 'next': '操作者未选定 DOS 参数，未创建任务。'}
