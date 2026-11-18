"""Operator-only, in-memory submission grants. Never exposed as model tools."""
import json
import re


def execute(c, argument, menu=False):
    from vasp_agent.chat.extra_commands import choose, configured_servers
    from vasp_agent.remote.remote_compute import RemoteCompute
    box = c.agent.tools
    if not argument:
        # A bare /submit used to open only the defaults editor, which reads like
        # it enabled submission but never grants anything. Ask what is meant.
        from vasp_agent.ui.menu_text import options
        state = '已授权' if box.allow_submit else '未授权'
        c.output(f'提交权限（当前：{state}）\n' + options([
            ('1', '授权一次提交（选择服务器并填写资源上限）'), ('2', '设置默认资源（不授权提交）'),
            ('3', '查看权限状态'), ('4', '关闭提交权限')]))
        c.output('远程任务也可以直接让 AI 提交：它会打开同一个授权菜单。')
        argument = {'1': 'once', '2': 'defaults', '3': 'status', '4': 'off'}.get(c.reader('输入编号（回车/q 取消）：').strip())
        if argument is None:
            return
    if argument == 'defaults':
        from vasp_agent.permissions.resource_menu import resource_menu, selection
        c.output(json.dumps(box.permission_status(), ensure_ascii=False, indent=2))
        servers = configured_servers(box)
        if box.submit_script:
            servers.append('local')
        server = choose(c, servers, '选择默认资源使用的服务器')
        if not server:
            return
        phase = choose(c, ['convergence', 'relaxation'], '默认资源类型：convergence 收敛扫描；relaxation 优化/SCF/能带（共用单组默认）')
        if not phase:
            return
        config = RemoteCompute(box.workspace, server).config if server != 'local' else {}
        current = dict(parallel_tests=1, cores_per_test=config.get('ntasks', 8), nodes=config.get('nodes', 1),
                       partition=config.get('partition', 'xahcnormal'), walltime=config.get('walltime', '04:00:00'))
        if 'submission_mode' in config:
            current['submission_mode'] = config['submission_mode']
        current = selection(current, phase)
        resource_menu(box.workspace, server, phase, current, c.reader, c.output, tests=6 if phase == 'convergence' else 1)
        c.output('只保存了默认资源，没有授权提交。需要提交时让 AI 提交（会弹出授权菜单并预填这些资源），'
                 '或输入 /submit once。')
        return
    if argument == 'off':
        box.revoke_submission()
    elif argument in ('on', 'once'):
        servers = configured_servers(box)
        if box.submit_script:
            servers.append('local')
        server = choose(c, servers, '选择授权服务器（local 表示当前机器）')
        if not server:
            return
        defaults = dict(partition='xahcnormal', ntasks=8, walltime='04:00:00')
        if server != 'local':
            defaults.update(RemoteCompute(box.workspace, server).config)
        def answer(label, default):
            value = c.reader(f'{label} [{default}]（q 取消）：').strip()
            if value == 'q':
                raise ValueError('已取消，原权限未改变。')
            return value or str(default)
        partition = answer('允许的分区', defaults['partition'])
        nodes = int(answer('最大节点数', 1))
        cores = int(answer('最大总 CPU 核数', defaults['ntasks']))
        walltime = answer('最长运行时间 HH:MM:SS', defaults['walltime'])
        count = 1 if argument == 'once' else int(answer('允许提交次数（1–10）', 1))
        if not re.fullmatch(r'[A-Za-z0-9_-]+', partition) or not 1 <= nodes <= 256 or not 1 <= cores <= 65536 or not 1 <= count <= 10:
            raise ValueError('分区或资源上限无效，原权限未改变。')
        from vasp_agent.remote.remote_worker import wall_seconds
        wall_seconds(walltime)
        box.submission_limits = dict(server=server, partition=partition, nodes=nodes, cores=cores, walltime=walltime)
        box.resource_confirmations.clear()
        # A selection confirmed under the previous grant must not skip the
        # operator menu under new limits; revoke_submission clears it too.
        box.confirmed_resource_selections.clear()
        box.submission_target = None
        box.submissions = 0
        box.max_submissions = count
        box.allow_submit = True
        box.revocation = None
        box.log({'permission': 'grant', **box.permission_status()})
        c.output('已授权；这不会启动计算。请在对话中明确指定需要提交的任务。')
    elif argument != 'status':
        raise ValueError('用法：/submit on | off | once | status')
    c.output(json.dumps(box.permission_status(), ensure_ascii=False, indent=2))
