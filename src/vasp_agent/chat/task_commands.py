"""Operator-only lifecycle commands for local and remote calculation tasks."""
import json


def execute(c, command, arg):
    from vasp_agent.chat.extra_commands import choose, configured_servers
    from vasp_agent.remote.remote_worker import list_workflow_jobs, manage_task
    tools = c.agent.tools
    parts = arg.split()
    if parts[:1] == ['task']:
        parts.pop(0)
    if not parts:
        servers = configured_servers(tools)
        server = choose(c, servers + ['local'], '选择服务器（local 为本机）')
        if not server:
            return
        parts = [server]
    if len(parts) > 2:
        raise ValueError(f'用法：{command} task 服务器 [任务名]；本机用 local。')
    server = parts[0]
    remote = None
    if server != 'local':
        from vasp_agent.remote.remote_compute import RemoteCompute
        remote = RemoteCompute(tools.workspace, server)

    def action(operation, **kwargs):
        if remote:
            return remote.call('manage_task', name, operation=operation, **kwargs)
        return manage_task(tools.path('runs'), name, operation, require_scheduler=False, **kwargs)

    if len(parts) == 1:
        c.output(f'正在读取 {server} 的任务列表……')
        data = remote.call('list_jobs', include_archived=True) if remote else list_workflow_jobs(tools.path('runs'), True)
        rows = [r for r in data['jobs'] if command == '/delete' or
                bool(r.get('archived')) == (command == '/restore')]
        if not rows:
            c.output('没有符合条件的任务。')
            return
        labels = [f"{r['name']} | {r['status']} | {'已归档' if r.get('archived') else '未归档'}" for r in rows]
        for i, label in enumerate(labels, 1):
            c.output(f'{i}. {label}')
        answer = c.reader('输入任务编号或完整名称，回车/q 取消：').strip()
        if answer in ('', 'q'):
            return
        name = next((r['name'] for r in rows if r['name'] == answer), None)
        if name is None and answer.isascii() and answer.isdigit() and 1 <= int(answer) <= len(rows):
            name = rows[int(answer) - 1]['name']
        if name is None:
            raise ValueError('任务选择无效。')
    else:
        name = parts[1]
    if command == '/delete':
        preview = action('delete_preview')
        c.output('删除预览：\n' + json.dumps(preview, ensure_ascii=False, indent=2))
        c.output('将永久删除此任务目录及输入、日志、结果；无法通过 /restore 恢复。')
        confirmation = c.reader(f'请输入完整任务名 {name} 确认删除，其他输入取消：').strip()
        if confirmation != name:
            c.output('已取消删除。')
            return
        result = action('delete', confirmation=confirmation, fingerprint=preview['fingerprint'])
    else:
        result = action(command.lstrip('/'))
    tools.log({'operator_task_management': {'action': command, 'server': server, 'name': name, **result}})
    c.output(json.dumps(result, ensure_ascii=False, indent=2))
