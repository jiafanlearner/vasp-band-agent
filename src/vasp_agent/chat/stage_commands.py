"""Physics confirmation through an operator menu, from slash input or a tool request."""
import json
from datetime import datetime


def select_task(c, server):
    from vasp_agent.chat.chat_commands import printable
    if server == 'local':
        from vasp_agent.remote.remote_worker import list_workflow_jobs
        data = list_workflow_jobs(c.agent.tools.path('runs'))
    else:
        from vasp_agent.remote.remote_compute import RemoteCompute
        c.output(f'正在读取 {server} 的任务列表……')
        data = RemoteCompute(c.agent.tools.workspace, server).call('list_jobs')
    rows = data['jobs']
    if not rows:
        c.output('该工作目录没有已有任务。请先让 AI 在新目录准备任务，再使用 /physics。')
        return None
    c.output('已有任务（最近更新优先；仅列出 Agent 工作目录中的任务）：')
    for index, row in enumerate(rows, 1):
        date = datetime.fromtimestamp(row['modified']).strftime('%m-%d %H:%M')
        label = '已确认' if row['physics_confirmed'] else '未确认'
        reason = f" | 不可选：{row['reason']}" if not row['selectable'] else ''
        c.output(printable(f"{index}. {row['name']} | {row['status']} | {row['phase']} | {label} | {date}{reason}").replace('\n', ' '))
    if data['total'] > len(rows):
        c.output(f"仅显示最近 {len(rows)} 个，共 {data['total']} 个；其他任务可直接输入完整命令。")
    answer = c.reader('输入任务编号或名称，回车/q 取消：').strip()
    if answer in ('', 'q'):
        return None
    row = next((r for r in rows if r['name'] == answer), None)
    if row is None and answer.isascii() and answer.isdigit() and 1 <= int(answer) <= len(rows):
        row = rows[int(answer) - 1]
    if row is None:
        raise ValueError('任务选择无效，未修改任何任务。')
    if not row['selectable']:
        raise ValueError(row['reason'])
    c.output(f"已选择：/physics {server} {row['name']}")
    if row['physics_confirmed']:
        c.output('该任务已有模型确认。若无需修改，可在下一步回车取消，然后让 AI 审阅当前阶段。')
        c.output('原研究目标：' + printable(row.get('goal', '')).replace('\n', ' '))
    return row['name']


def execute(c, arg):
    parts = arg.split()
    if not parts:
        from vasp_agent.chat.extra_commands import choose, configured_servers
        servers = configured_servers(c.agent.tools)
        c.output('选择服务器；local 表示本机。回车或 q 可取消。')
        server = choose(c, servers + ['local'], '选择服务器')
        if not server:
            return
        parts = [server]
    if len(parts) == 1:
        job = select_task(c, parts[0])
        if job is None:
            return
        parts.append(job)
    if len(parts) != 2:
        raise ValueError('用法：/physics 打开菜单，或 /physics 服务器名 [任务名]（本机用 local）')
    server, job = parts
    c.output('当前支持模型：非磁性、电中性、PBE，不含 SOC、DFT+U 或杂化泛函。\n'
             '体系维度（三维体相或真空沿 c 的二维 slab）由结构自动检测并写入记录。\n'
             '若与你的体系或研究目标不符，请取消；磁性和电荷等假设不能由结构文件自动证明。')
    goal = c.reader('研究目标（空白取消）：').strip()
    if not goal:
        return
    answer = c.reader('确认以上全部物理假设适用于本任务请输入“确认”，其他输入取消：').strip()
    if answer != '确认':
        c.output('已取消，未确认模型。')
        return
    result = save_confirmation(c.agent.tools, server, job, goal, 'operator_slash_command')
    c.output(json.dumps(result, ensure_ascii=False, indent=2))


def save_confirmation(box, server, job, goal, channel):
    """Write the operator's physical-model confirmation.

    The channel is chosen by which menu ran, not by the caller's preference, so
    the record names the surface the operator actually used. It travels to the
    remote worker in the payload because that is where the write happens.
    """
    from vasp_agent.permissions.unified_confirm import save_physics_confirmation
    return save_physics_confirmation(box, server, job, goal, channel)


def request_confirmation(box, server, job_name, proposed_goal):
    from vasp_agent.workflow.vasp_band_agent import ToolError
    from vasp_agent.chat.chat_commands import printable
    server = None if server == 'local' else server
    if box.plan_reader is None:
        return {'confirmed': False, 'status': 'interaction_required', 'submission_authorized': False,
                'note': '需要操作者交互确认；未保存模型确认。'}
    # Also recovers a successful write whose tool response was interrupted.
    try:
        context = box.get_stage_review(server, job_name)
    except ToolError as exc:
        if exc.error_code != 'PHYSICS_CONFIRMATION_REQUIRED':
            raise
    else:
        return {'confirmed': True, 'status': 'already_confirmed', 'physics': context['physics'],
                'submission_authorized': False, 'next': 'get_stage_review'}
    if not isinstance(proposed_goal, str) or not proposed_goal.strip() or len(proposed_goal) > 2000:
        raise ValueError('请提供不超过 2000 字的研究目标建议。')
    output, reader = getattr(box, 'plan_output', print), box.plan_reader
    output(printable(f'模型确认：{server or "local"} / {job_name}'))
    output('电中性、非磁性 PBE；不含 SOC、DFT+U 或杂化泛函。体系维度（三维体相或二维 slab）由结构自动检测。')
    output('这是本任务的研究假设，不代表真实材料基态，也不授予计算提交权限。')
    output('建议研究目标：' + printable(proposed_goal))
    output('1. 确认以上模型和研究目标\n2. 自行填写研究目标后确认\n3. 模型不适用／取消')
    try:
        answer = reader('选择（回车/q 取消）：').strip()
        goal = proposed_goal.strip()
        if answer == '2':
            goal = reader('研究目标（回车/q 取消）：').strip()
            if not goal or goal == 'q':
                answer = 'q'
            else:
                output('研究目标：' + printable(goal))
                answer = '1' if reader('确认上述固定物理模型和此目标请输入“确认”：').strip() == '确认' else 'q'
    except (EOFError, KeyboardInterrupt):
        answer = 'q'
    if answer != '1':
        return {'confirmed': False, 'status': 'cancelled', 'submission_authorized': False,
                'note': '操作者未确认；不要在本轮再次弹出相同菜单或继续阶段放行。'}
    result = save_confirmation(box, server, job_name, goal, 'operator_chat_menu')
    return {'confirmed': True, 'status': 'confirmed', 'physics': result,
            'submission_authorized': False, 'next': 'get_stage_review',
            'note': '操作者已现场确认；继续本轮阶段审阅，无需再输入 /physics。'}
