"""Operator-confirmed cancellation of remote Slurm jobs, release of cancelled receipts,
and resume of a phase whose job ended early.

The slash command asks the operator to type the JobID. The model tool
(request_job_cancellation) can only open a menu: the operator's own choice there
is the confirmation, and the worker still checks the JobID from the receipt.
"""
import json
import time

from vasp_agent.workflow import stage_definitions

# Patched in tests; scancel usually reaches CANCELLED in seconds.
sleep = time.sleep
RELEASE_ATTEMPTS = 12
RELEASE_INTERVAL = 5
NOT_YET = ('仍在队列', '未显示作业')

USAGE = ('用法：/cancel 任务名（本机）；/cancel 服务器 任务名 [阶段] 取消远程作业；'
         '/cancel release 服务器 任务名 [阶段] 在作业记账为 CANCELLED 后释放提交记录；'
         '/cancel resume 服务器 任务名 阶段 在作业因超时/取消/节点故障/抢占结束后续算（复用已验收阶段）。')
PHASES = stage_definitions.known_names()


def remote_target(tools, parts):
    """(mode, server, job, phase) for a remote form, or None for the local form.

    mode is 'release', 'resume' or False (cancel).
    """
    from vasp_agent.chat.extra_commands import configured_servers
    release = parts[0] if parts[:1] in (['release'], ['resume']) and len(parts) >= 3 else False
    rest = parts[1:] if release else parts
    if len(rest) < 2:
        if release:
            raise ValueError(USAGE)
        return None
    if len(rest) > 3 or rest[0] not in configured_servers(tools) or len(rest) == 3 and rest[2] not in PHASES:
        raise ValueError(USAGE)
    return release, rest[0], rest[1], rest[2] if len(rest) == 3 else None


def execute(c, target):
    from vasp_agent.remote.remote_compute import RemoteCompute
    release, server, job, phase = target
    tools = c.agent.tools
    remote = RemoteCompute(tools.workspace, server)
    preview = remote.cancel_job(job, phase)
    phase, job_id = preview['phase'], preview['job_id']
    label = f'{server} / {job} / {phase or "完整流程"}，作业 {job_id}'
    if release == 'resume':
        plan = remote.prepare_phase_resume(job, phase)
        for line in resume_lines(plan):
            c.output(line)
        if c.reader('输入作业编号确认续算准备：').strip() != job_id:
            c.output('未续算。')
            return
        c.output(json.dumps(resume_receipt(tools, remote, server, job, phase, job_id), ensure_ascii=False, indent=2))
        return
    if release:
        c.output(f'释放 {label} 的提交记录：仅当作业已退出、记账为 CANCELLED 且本阶段没有任何输出时才会归档回执；'
                 '之后须重新确认资源、重新审阅并重新授权，不会自动提交。')
        if c.reader('输入作业编号确认释放：').strip() != job_id:
            c.output('未释放。')
            return
        result = release_receipt(tools, remote, server, job, phase, job_id)
        c.output(json.dumps(result, ensure_ascii=False, indent=2))
        return
    if c.reader(f'取消 {label}？输入作业编号确认：').strip() != job_id:
        c.output('未取消。')
        return
    result = remote.cancel_job(job, phase, job_id)
    tools.log({'operator_cancel_job': {'server': server, 'job_name': job, **result}})
    c.output(json.dumps(result, ensure_ascii=False, indent=2))
    c.output(f'作业退出并记账为 CANCELLED 后，可用 /cancel release {server} {job} {phase or ""}'.rstrip()
             + ' 释放该阶段，以便按新资源重新提交。')


def release_receipt(tools, remote, server, job, phase, job_id, output=print):
    """Archive the receipt once accounting shows CANCELLED; drop stale grants and watch."""
    result = remote.release_cancelled_submission(job, phase, job_id)
    try:
        tools.monitor.deactivate(server, job, phase, job_id)
    except Exception:
        result = {**result, 'monitor_warning': '监控记录未能停用；请用 /watch remove 处理。'}
    tools.forget_submission_target(server, job, phase)
    tools.log({'operator_release_cancelled_submission': {'server': server, 'job_name': job, **result}})
    return result


def resume_lines(plan):
    """What the operator confirms: which stages are reused, which rerun, what is archived."""
    lines = [f"作业 {plan['job_id']} 记账为 {plan['scheduler_state']}，阶段 {plan['phase']} 可续算：",
             '  复用（哈希与输入已核对）：' + (', '.join(plan['reused']) or '无'),
             '  重算（从审阅过的输入重新开始）：' + (', '.join(plan['rerun']) or '无')]
    if plan['archived']:
        shown = ', '.join(plan['archived'][:8]) + (' 等' if len(plan['archived']) > 8 else '')
        lines.append(f"  中断输出移入 {plan['archive']}/（不删除）：{shown}")
    if plan['stale_lock']:
        lines.append('  作业已结束，遗留的运行锁 .agent.lock 一并归档。')
    lines.append('之后需重新审阅本阶段、重新确认资源（可改时限）并重新授权，不会自动提交。')
    return lines


def resume_receipt(tools, remote, server, job, phase, job_id):
    """Prepare the confirmed resume; drop the watch, grants and resource choices of the ended job."""
    result = remote.prepare_phase_resume(job, phase, job_id)
    try:
        tools.monitor.deactivate(server, job, phase, job_id, state=result['scheduler_state'])
    except Exception:
        result = {**result, 'monitor_warning': '监控记录未能停用；请用 /watch remove 处理。'}
    tools.forget_submission_target(server, job, phase)
    tools.log({'operator_prepare_phase_resume': {'server': server, 'job_name': job, **result}})
    return result


def wait_and_release(tools, remote, server, job, phase, job_id, output):
    """Retry release while scancel takes effect; other refusals return at once."""
    last = None
    for attempt in range(RELEASE_ATTEMPTS):
        if attempt:
            sleep(RELEASE_INTERVAL)
        try:
            return release_receipt(tools, remote, server, job, phase, job_id, output)
        except ValueError as exc:
            last = str(exc)
            if not any(marker in last for marker in NOT_YET):
                return {'released': False, 'reason': last}
    return {'released': False, 'reason': last,
            'next': '作业尚未显示为 CANCELLED；稍后用 action=release 再试，不会重复取消。'}


MENUS = {
    'cancel_and_release': ('1', '取消作业，退出后释放本阶段提交记录（之后可改资源重新提交）'),
    'cancel': ('1', '只取消作业，保留提交记录'),
    'release': ('1', '释放已取消作业的提交记录（之后可改资源重新提交）'),
    'resume': ('1', '续算：归档中断输出、复用已验收阶段并释放提交记录（之后可改资源重新提交）'),
}


def request(tools, server, job, phase, action):
    """Model-requested cancel/release: the operator decides in a menu, nothing runs otherwise."""
    from vasp_agent.remote.remote_compute import RemoteCompute
    if action not in MENUS:
        raise ValueError('action 必须是 cancel_and_release、cancel、release 或 resume。')
    remote = RemoteCompute(tools.workspace, server)
    if action == 'resume':
        return request_resume(tools, remote, server, job, phase)
    preview = remote.cancel_job(job, phase)
    phase, job_id = preview['phase'], preview['job_id']
    if action != 'cancel' and phase is None:
        raise ValueError('旧任务（无阶段）只支持取消，不支持释放提交记录。')
    if tools.plan_reader is None:
        return {'status': 'operator_confirmation_required', 'cancel_requested': False, **preview}
    output = getattr(tools, 'plan_output', print)
    key, label = MENUS[action]
    output(f'{server} / {job} / {phase or "完整流程"}，JobID {job_id}（回执状态 {preview.get("receipt_status")}）')
    output(f'  {key}  {label}')
    output('  q  不执行')
    if action != 'cancel':
        output('释放后需重新确认资源、重新审阅并重新授权；作业若已开始计算并留下输出，不会释放，需新建任务。')
    if tools.plan_reader('选择：').strip().lower() != key:
        return {'status': 'cancelled_by_operator', 'cancel_requested': False, 'job_id': job_id}
    # The grant belonged to the submission being withdrawn.
    tools.revoke_submission('job_cancellation')
    if action == 'release':
        return {'release': wait_and_release(tools, remote, server, job, phase, job_id, output)}
    result = remote.cancel_job(job, phase, job_id)
    tools.log({'operator_cancel_job': {'server': server, 'job_name': job, 'via': 'menu', **result}})
    if action == 'cancel':
        return result
    output('已发送取消请求，等待调度记账显示 CANCELLED…')
    sleep(RELEASE_INTERVAL)
    return {**result, 'release': wait_and_release(tools, remote, server, job, phase, job_id, output)}


def request_resume(tools, remote, server, job, phase):
    """Model-requested resume: the worker's preview is shown, the operator's key confirms it."""
    if phase is None:
        raise ValueError('续算需要明确的阶段。')
    plan = remote.prepare_phase_resume(job, phase)
    job_id = plan['job_id']
    if tools.plan_reader is None:
        return {'status': 'operator_confirmation_required', **plan}
    output = getattr(tools, 'plan_output', print)
    output(f'{server} / {job} / {phase}，JobID {job_id}')
    for line in resume_lines(plan):
        output(line)
    key, label = MENUS['resume']
    output(f'  {key}  {label}')
    output('  q  不执行')
    if tools.plan_reader('选择：').strip().lower() != key:
        return {'status': 'cancelled_by_operator', 'prepared': False, 'job_id': job_id}
    # Any grant belonged to the submission that ended; the rerun needs a new one.
    tools.revoke_submission('phase_resume')
    return resume_receipt(tools, remote, server, job, phase, job_id)
