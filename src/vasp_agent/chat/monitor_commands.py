"""Operator-only job monitoring controls; never invoke the model or submit jobs."""
from datetime import datetime

from vasp_agent.ui.terminal_ui import display_safe


USAGE = ('用法：/watch [status|on|off|auto|interval 分钟|check|remove JobID [服务器]]；'
         '间隔为 1–1440 的整数分钟。')


def _text(value, limit=160):
    if not isinstance(value, (str, int, float)):
        return '—'
    return ' '.join(str(display_safe(str(value))).split())[:limit] or '—'


def _minutes(seconds):
    try:
        return f'{float(seconds) / 60:g}'
    except (TypeError, ValueError):
        return '—'


def _next_poll(value):
    if isinstance(value, (int, float)):
        try:
            return datetime.fromtimestamp(value).astimezone().strftime('%m-%d %H:%M:%S')
        except (OSError, OverflowError, ValueError):
            return '—'
    return _text(value, 40)


def _target(task):
    server = _text(task.get('server') or '本机', 40)
    name = _text(task.get('job_name'), 48)
    return f'{server}/{name} · JobID {_text(task.get("job_id"), 40)}'


def show_status(c, status):
    enabled = bool(status.get('enabled'))
    tasks = status.get('tasks') or []
    c.output(f'监控：{"已开启" if enabled else "已暂停"} · '
             f'模式：{"自动建议" if status.get("auto") else "手动固定"} · '
             f'默认间隔：{_minutes(status.get("interval_seconds"))} 分钟 · 目标：{len(tasks)}')
    if status.get('running') is False and tasks:
        c.output('本窗口未运行后台检查；可能由其他窗口负责，或需启动交互式 Agent。')
    for task in tasks:
        active = task.get('active', True) and task.get('enabled', True) and not task.get('terminal')
        next_poll = _next_poll(task.get('next_due_at')) if enabled and active else '已暂停/停止'
        c.output(f'  {_target(task)} · {_text(task.get("state") or "尚未检查", 40)} · '
                 f'{_minutes(task.get("interval_seconds", status.get("interval_seconds")))} 分钟 · '
                 f'下次：{next_poll}')
    if not tasks:
        c.output('尚无监控目标；成功提交并取得 JobID 后自动登记。')


def _show_check(c, snapshots):
    if not snapshots:
        c.output('本次检查没有返回监控目标。')
        return
    c.output(f'已执行一次只读检查：{len(snapshots)} 个目标。')
    for task in snapshots:
        text = f'  {_target(task)} · Slurm {_text(task.get("state") or "unknown", 40)}'
        workflow = task.get('workflow_status')
        if workflow:
            text += f' · 工作流 {_text(workflow, 40)}'
        if workflow == 'awaiting_review' and task.get('next_phase'):
            text += f' · 下一阶段 {_text(task["next_phase"], 40)} 待审阅'
        if task.get('kind') == 'error' or task.get('error'):
            text += ' · 读取失败：' + _text(task.get('message') or task.get('error'), 160)
        c.output(text)


def execute(c, argument):
    parts = argument.split()
    action = parts[0] if parts else 'status'
    if action in ('status', 'on', 'off', 'auto', 'check'):
        if len(parts) > 1:
            raise ValueError(USAGE)
    elif action == 'interval':
        if (len(parts) != 2 or not parts[1].isascii() or not parts[1].isdigit()
                or not 1 <= int(parts[1]) <= 1440):
            raise ValueError(USAGE)
    elif action == 'remove':
        if len(parts) not in (2, 3):
            raise ValueError(USAGE)
    else:
        raise ValueError(USAGE)

    monitor = c.agent.tools.monitor
    if action == 'on':
        monitor.configure(enabled=True)
    elif action == 'off':
        monitor.configure(enabled=False)
        c.output('已暂停周期检查；监控记录保留，可用 /watch on 恢复。')
    elif action == 'auto':
        monitor.configure(auto=True)
        c.output('已启用自动建议间隔；暂停时需用 /watch on 恢复周期检查。')
    elif action == 'interval':
        monitor.configure(interval_seconds=int(parts[1]) * 60, auto=False)
        c.output('已固定全局检查间隔；暂停时需用 /watch on 恢复周期检查。')
    elif action == 'check':
        _show_check(c, monitor.check_once())
        return
    elif action == 'remove':
        removed = monitor.remove(parts[1], server=parts[2] if len(parts) == 3 else None)
        c.output('已移除监控目标；计算作业继续运行。' if removed else '没有找到匹配的监控目标。')
    show_status(c, monitor.status())
