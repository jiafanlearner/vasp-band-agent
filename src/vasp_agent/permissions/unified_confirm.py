"""Shared unified operator confirmation for parameters, resources, and single-stage pre-authorization.

Reuses permissions.resource_menu and permissions.submission_dialog validation,
saved server defaults, and KPAR/NCORE preview. Resources are written immediately
after directory creation so subsequent get_stage_review inspects the final
snapshot with zero rework. Pre-authorization is strictly bound to one
(server, job_name, phase) and still requires get_stage_review ->
record_stage_review(allowed=true) and submit_checked/submit_job preflight.
"""
from __future__ import annotations

import json
import re
import unicodedata
from uuid import uuid4

from vasp_agent.permissions import resource_menu, submission_dialog
from vasp_agent.ui.menu_text import options, table

SAVE_TOKENS = {'保存', 'save'}


def can_configure_resources(box, server) -> bool:
    """True when the target environment has a Slurm script that can be configured and submitted."""
    return server is not None or getattr(box, 'submit_script', None) is not None


def _base_resources(box, server, phase: str) -> tuple[dict, str]:
    if server is not None:
        try:
            from vasp_agent.remote.remote_servers import Servers
            cfg_path = Servers(box.workspace).file(server).with_name(server + '.compute.json')
            cfg = json.loads(cfg_path.read_text(encoding='utf-8'))
            parallel = int(cfg.get('parallel_tests') or 1) if phase in (None, 'convergence') else 1
            cores = int(cfg.get('cores_per_test') or cfg.get('ntasks') or 8)
            return ({'parallel_tests': max(1, parallel), 'cores_per_test': max(1, cores), 'nodes': 1,
                     'partition': str(cfg.get('partition') or 'compute'),
                     'walltime': str(cfg.get('walltime') or '01:00:00')}, '服务器配置')
        except Exception:
            return ({'parallel_tests': 1, 'cores_per_test': 8, 'nodes': 1,
                     'partition': 'compute', 'walltime': '01:00:00'}, '默认配置')
    script_path = getattr(box, 'submit_script', None)
    text = script_path.read_text(encoding='utf-8')
    values = dict(re.findall(r'^#SBATCH --([\w-]+)=([^\n]+)$', text, re.M))
    nodes = max(1, int(values.get('nodes', 1)))
    cores = max(1, int(values.get('ntasks', 8)))
    if cores % nodes:
        nodes = 1
    return ({'parallel_tests': 1, 'cores_per_test': cores, 'nodes': nodes,
             'partition': values.get('partition', 'compute'),
             'walltime': values.get('time', '01:00:00')}, '本机提交脚本')


def initial_resources(box, server, phase: str, *, tests: int = 1) -> tuple[dict | None, str | None]:
    """Load initial resources (saved defaults first, else server/script defaults)."""
    if not can_configure_resources(box, server):
        return None, None
    server_key = server or 'local'
    resources, source = _base_resources(box, server, phase)
    workspace = getattr(box, 'workspace', None)
    if workspace is not None:
        try:
            saved = resource_menu.load_defaults(workspace, server_key, phase)
        except (ValueError, OSError, TypeError, KeyError):
            saved = None
        if saved:
            resources, source = saved, '用户已保存的默认资源'
    resources = resource_menu.selection(resources, phase)
    maximum = min(6, max(1, tests)) if phase in (None, 'convergence') else 1
    if resources['parallel_tests'] > maximum:
        resources = {**resources, 'parallel_tests': maximum}
    if resources['cores_per_test'] % max(1, resources['nodes']):
        resources = {**resources, 'nodes': 1}
    return resources, source


def resource_section_text(server, phase: str, resources: dict | None, source_label: str | None, *,
                          tests: int = 1, execution_tags: dict | None = None) -> tuple[str, bool]:
    """Render the 【提交资源区】 block and return (text, valid)."""
    if resources is None:
        return '【提交资源区】本机未配置 Slurm 提交脚本，本次仅准备新目录、不授予提交权限。', True
    server_key = server or 'local'
    mode = resources.get('submission_mode', 'allocation')
    nodes = max(1, resources['nodes'])
    maximum = min(6, max(1, tests)) if phase in (None, 'convergence') else 1
    total = resources['parallel_tests'] * resources['cores_per_test']
    mode_note = ('可切换 array / allocation' if phase == 'convergence' and server_key != 'local'
                 else '本机仅 allocation' if server_key == 'local'
                 else '仅 convergence 支持 array')
    rows = [['提交模式', mode, mode_note]]
    if mode == 'array':
        rows += [
            ['每个数组元素', f'{resources["cores_per_test"]} MPI × {resources["nodes"]} 节点',
             f'每节点 {resources["cores_per_test"] // nodes} MPI'],
            ['最多同时运行', f'{resources["parallel_tests"]} 个', f'上限 {maximum}；共 {tests} 项测试'],
            ['并发峰值', f'{total} 核 / {resources["parallel_tests"] * resources["nodes"]} 节点', ''],
            ['分区 / 每项时限', f'{resources["partition"]} / {resources["walltime"]}', ''],
        ]
    else:
        rows += [
            ['并发', f'{resources["parallel_tests"]} 组 × 每组 {resources["cores_per_test"]} MPI', f'上限 {maximum} 组'],
            ['节点', resources['nodes'], f'每节点 {total // nodes} MPI，每组每节点 {resources["cores_per_test"] // nodes}'],
            ['分区 / 时限', f'{resources["partition"]} / {resources["walltime"]}', f'CPU 总数 {total} 核'],
        ]
    tag_row = resource_menu.execution_row(resources, {'enabled': True} if execution_tags is None else execution_tags)
    if tag_row:
        rows.append(tag_row)
    lines = [f'【提交资源区】（{source_label or "默认配置"}）', table(rows, indent='')]
    try:
        resource_menu.validate_server_mode(server_key, resources)
        submission_dialog.validate_resources(resources, phase, resource_menu.ceiling(resources), tests)
        valid = True
    except ValueError as exc:
        lines.append(f'当前资源方案不可确认（请先修改核数与资源）：{exc}')
        valid = False
    return '\n'.join(lines), valid


def unified_options_text(phase: str, *, edit_param_label: str | None = None, can_submit: bool = True) -> str:
    """Standardized footer options across unified confirmation menus."""
    if can_submit:
        items = [
            ('1', f'确认参数与核数，并在审阅通过后直接提交本阶段（{phase}）'),
            ('2', '确认参数与核数，仅准备（暂不提交）'),
        ]
        if edit_param_label:
            items.append(('3', edit_param_label))
            items.append(('4', '修改核数与资源（支持 并发=... 每组=... 节点=... 分区=... 时限=...）'))
        else:
            items.append(('3', '修改核数与资源（支持 并发=... 每组=... 节点=... 分区=... 时限=...）'))
        items.append(('q', '取消'))
        return (options(items) + '\n'
                f'说明：选 1 仅预授权本任务的 {phase} 阶段（1 次）；落盘后仍须通过阶段审阅与远端预检才提交，不跨阶段连授。')
    items = [
        ('1', '确认参数并准备新目录（本机未配置提交脚本，仅准备不授权）'),
        ('2', '确认参数并仅准备（暂不提交）'),
    ]
    if edit_param_label:
        items.append(('3', edit_param_label))
    items.append(('q', '取消'))
    return options(items)


def edit_resources(box, server, phase: str, resources: dict, source_label: str, reader, output, *,
                   tests: int = 1) -> tuple[dict, str]:
    """Inline resource editing for option 4 (or 3 in retry) inside a unified menu."""
    server_key = server or 'local'
    mode_hint = '模式=array ' if phase == 'convergence' and server_key != 'local' else ''
    prompt = (f'输入资源参数（如 {mode_hint}并发=1 每组=16 节点=1 分区=compute 时限=04:00:00，'
              '末尾加 保存 可存为服务器默认；e 逐项修改；s 保存当前为默认；回车返回）：')
    raw = reader(prompt).strip()
    if not raw or raw.lower() == 'q':
        return resources, source_label
    if raw.lower() == 's':
        try:
            resource_menu.validate_server_mode(server_key, resources)
            submission_dialog.validate_resources(resources, phase, resource_menu.ceiling(resources), tests)
            if getattr(box, 'workspace', None) is not None:
                resource_menu.save_defaults(box.workspace, server_key, phase, resources)
                output('默认资源已保存；保存内容不包含提交授权。')
                return resources, '用户已保存的默认资源'
        except ValueError as exc:
            output(f'默认资源未保存：{exc}')
        return resources, source_label
    if raw.lower() == 'e':
        try:
            edited = resource_menu.edit_fields(resources, phase, tests, None, reader, output)
            resource_menu.validate_server_mode(server_key, edited)
            submission_dialog.validate_resources(edited, phase, resource_menu.ceiling(edited), tests)
            return edited, '操作者修改，待确认'
        except ValueError as exc:
            output(str(exc))
            return resources, source_label
    if raw.lower() in ('array', 'allocation'):
        candidate = {**resources, 'submission_mode': raw.lower()}
        try:
            resource_menu.validate_server_mode(server_key, candidate)
            submission_dialog.validate_mode(candidate, phase)
            submission_dialog.validate_resources(candidate, phase, resource_menu.ceiling(candidate), tests)
            return candidate, '操作者切换模式，待确认'
        except ValueError as exc:
            output(f'{exc} 模式未切换。')
            return resources, source_label
    normalized = unicodedata.normalize('NFKC', raw).replace(',', ' ').replace('、', ' ')
    tokens = normalized.split()
    want_save = False
    kept = []
    for token in tokens:
        if token.lower() in SAVE_TOKENS:
            want_save = True
        else:
            kept.append(token)
    if not kept:
        output('未提供资源参数，配置未变化。')
        return resources, source_label
    try:
        candidate = resource_menu.parse_custom(' '.join(kept), resources)
        resource_menu.validate_server_mode(server_key, candidate)
        submission_dialog.validate_resources(candidate, phase, resource_menu.ceiling(candidate), tests)
    except ValueError as exc:
        output(f'{exc} 参数未应用。')
        return resources, source_label
    new_source = '操作者修改，待确认'
    if want_save and getattr(box, 'workspace', None) is not None:
        resource_menu.save_defaults(box.workspace, server_key, phase, candidate)
        output('默认资源已保存；保存内容不包含提交授权。')
        new_source = '用户已保存的默认资源'
    return candidate, new_source


def _limits_for(server_key: str, resources: dict) -> dict:
    array = resources.get('submission_mode', 'allocation') == 'array'
    return dict(
        server=server_key,
        partition=resources['partition'],
        nodes=resources['nodes'] * (resources['parallel_tests'] if array else 1),
        cores=resources['parallel_tests'] * resources['cores_per_test'],
        walltime=resources['walltime'],
    )


def apply_and_preauthorize(box, server, job_name: str, phase: str, resources: dict | None, *,
                           authorize: bool, tests: int = 1) -> dict:
    """Apply confirmed resources to the newly created task and optionally grant 1-shot phase pre-auth."""
    if resources is None:
        return {
            'resources_applied': False,
            'submission_preauthorized': False,
            'submission_target': None,
            'next': f'新目录已准备完成（本机未配置提交脚本，未授予提交权）；请先审阅 {phase}。',
        }
    server_key = server or 'local'
    key = (server_key, job_name, phase)
    resource_menu.validate_server_mode(server_key, resources)
    submission_dialog.validate_resources(resources, phase, resource_menu.ceiling(resources), tests)
    limits = _limits_for(server_key, resources)
    operation_id = uuid4().hex
    if server is not None:
        from vasp_agent.remote.remote_compute import RemoteCompute
        remote = RemoteCompute(box.workspace, server)
        ctx = remote.call('resource_context', job_name, phase=phase)
        applied = remote.call('configure_resources', job_name, phase=phase, resources=resources,
                              signature=ctx['signature'], limits=limits, operation_id=operation_id)
    else:
        from vasp_agent.remote.remote_worker import configure_resources, resource_context
        folder = box.job(job_name)
        box.stage_submit_script(folder)
        ctx = resource_context(folder, phase)
        applied = configure_resources(folder, phase, resources, ctx['signature'], limits, operation_id=operation_id)
    final_sig = applied['signature']
    final_resources = applied['resources'].copy()
    box.resource_confirmations[key] = final_sig
    if not hasattr(box, 'confirmed_resource_selections'):
        box.confirmed_resource_selections = {}
    box.confirmed_resource_selections[key] = {'signature': final_sig, 'resources': final_resources}
    box.log({'operator_submission_resources': {'server': server_key, 'job_name': job_name,
                                               'phase': phase, **final_resources}})
    if authorize:
        box.submission_limits = limits
        box.submission_target = key
        box.max_submissions, box.submissions, box.allow_submit = 1, 0, True
        box.revocation = None
        box.log({'permission': 'grant_target_once', 'server': server_key, 'job_name': job_name,
                 'phase': phase, 'limits': limits, 'source': 'unified_confirm_menu'})
    return {
        'resources': final_resources,
        'resources_applied': True,
        'submission_preauthorized': bool(authorize),
        'submission_target': {'server': server, 'job_name': job_name, 'phase': phase} if authorize else None,
        'next': (
            f'新目录已按确认核数准备并预授权仅本阶段（{phase}）1 次提交；'
            f'请执行 get_stage_review → record_stage_review，审阅 allowed=true 后可直接调用 submit_stage（无需再弹 request_submission）；'
            '若审阅为 blocked 或预检未过，严禁提交。'
            if authorize else
            f'新目录已按确认核数准备完成（未授予提交权）；请先审阅 {phase}，需要提交时再通过 request_submission 授权。'
        ),
    }
