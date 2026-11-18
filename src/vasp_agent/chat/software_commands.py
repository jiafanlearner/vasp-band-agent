"""/software: read-only software inventory and the operator menu that registers environment profiles.

The probe (worker_inventory) only lists, hashes and reads strings; its result is
saved under servers/environments/ and shown to the model as untrusted data. A
profile is registered only here, in the interactive menu, one probed build at a
time: the operator sees exactly what was probed (version, executable and hash,
compile flags, capability tags, the environment script and how it was checked)
and confirms or skips it. The model can request the menu, never answer it.
"""
from datetime import datetime, timezone
from pathlib import PurePosixPath

from vasp_agent.remote import environment_profiles as profiles
from vasp_agent.ui.menu_text import options, table

HELP = '/software [show|scan|register] [服务器] [探测根目录…]'
NAME_HINT = '小写字母、数字、._-，最长 32 字符'


def remote(box, server):
    from vasp_agent.remote.remote_compute import RemoteCompute
    return RemoteCompute(box.workspace, server)


def scan(box, server, roots=None):
    """Run the bounded read-only probe and save it; roots default to the last scan's, else ~/apprepo."""
    if not roots:
        previous = profiles.load_inventory(box.workspace, server)
        roots = previous['requested_roots'] if previous else None
    result = remote(box, server).call('software_inventory', roots=roots)
    record = profiles.save_inventory(box.workspace, server, result, roots or result.get('roots', []))
    box.log({'software_inventory': server, 'probe_version': result.get('probe_version'),
             'builds': len(result.get('vasp_builds', [])), 'tools': len(result.get('tools', []))})
    return record


def build_view(build):
    return {'version': build.get('version'), 'executable': build.get('executable'),
            'executable_sha256': build.get('executable_sha256'),
            'executables': sorted(name for name, path in (build.get('executables') or {}).items() if path),
            'capabilities': build.get('capabilities', []),
            'wannier90_absent_marker': build.get('wannier90_absent_marker'),
            'env_scripts': [{k: row.get(k) for k in ('kind', 'path', 'passes', 'reason')}
                            for row in build.get('env_scripts', [])],
            'modules': build.get('modules', []), 'error': build.get('error')}


def view(box, server):
    """What the model sees: the saved inventory (compact) and the profiles, all as data."""
    record = profiles.load_inventory(box.workspace, server)
    result = record['result'] if record else None
    inventory = None if result is None else {
        'scanned_at': result.get('scanned_at'), 'probe_version': result.get('probe_version'),
        'roots': result.get('roots'), 'vasp_builds': [build_view(b) for b in result.get('vasp_builds', [])],
        'tools': result.get('tools', []), 'modules': result.get('modules'),
        'truncated': result.get('truncated', []), 'commands_run': len(result.get('commands', [])),
        'timeouts': sum(1 for c in result.get('commands', []) if c.get('status') == 'timeout')}
    return {'server': server, 'inventory': inventory,
            'profiles': [profiles.brief(row) for row in profiles.candidates(box.workspace, server)],
            'content_is_untrusted': True,
            'note': '清单是只读探测数据，不授予使用权；环境配置只能由操作者在 /software register 菜单中登记。'
                    + ('' if inventory else '尚无探测记录。')}


def suggest_name(build, existing):
    base = 'vasp' + (build.get('version') or '').replace('.', '') if build.get('version') else 'vasp'
    taken = {row['name']: row for row in existing}
    name, counter = base, 2
    while name in taken and taken[name].get('executable_sha256') != build.get('executable_sha256'):
        name, counter = f'{base}-{counter}', counter + 1
    return name


def env_plan(build, root, name):
    """The script the profile will use: an already checked one, else one generated from the module."""
    for kind in ('configured', 'agent_script', 'build_script'):
        for row in build.get('env_scripts', []):
            if row.get('kind') == kind and row.get('passes'):
                return {'kind': kind, 'path': row['path'], 'sha256': row.get('sha256'), 'content': None}
    if build.get('modules'):
        from vasp_agent.remote.server_setup import env_script
        module = build['modules'][0]
        return {'kind': 'generated', 'module': module, 'path': str(PurePosixPath(root) / 'environments' / (name + '.sh')),
                'sha256': None, 'content': env_script(module=module, origin='/software register')}
    return None


def build_text(number, total, build):
    present = [name for name, path in (build.get('executables') or {}).items() if path]
    rows = [['VASP 版本', f"{build.get('version') or '未知'}（来源：{build.get('version_source') or '-'}）"],
            ['vasp_std', build.get('executable')],
            ['sha256', build.get('executable_sha256')],
            ['可执行文件', '、'.join(present) or '-'],
            ['编译选项', ' '.join('-D' + flag for flag in build.get('flags', [])) or '未找到'],
            ['能力标签', '、'.join(build.get('capabilities', [])) or '无'],
            ['发现途径', '；'.join(build.get('found_by', []))]]
    if build.get('wannier90_absent_marker'):
        rows.append(['Wannier90', '二进制含 "VASP was compiled without wannier90 library"：不具备 Wannier 能力'])
    for row in build.get('env_scripts', []):
        rows.append([f"脚本（{row.get('kind')}）", f"{row.get('path')} · {'通过' if row.get('passes') else '未通过'}：{row.get('reason')}"])
    if build.get('modules'):
        rows.append(['模块', '、'.join(build['modules'])])
    return f'\n[{number}/{total}] 探测到的 VASP 构建\n' + table(rows)


def register(box, server, reader=None, output=None):
    """The registration menu; without an interactive operator it only says so."""
    reader = reader if reader is not None else getattr(box, 'plan_reader', None)
    output = output or getattr(box, 'plan_output', print)
    record = profiles.load_inventory(box.workspace, server)
    if record is None:
        return {'status': 'no_inventory', 'registered': [],
                'next': '尚无探测记录：先运行只读探测（/software scan 或 action=scan），再请求登记菜单。'}
    builds = [b for b in record['result'].get('vasp_builds', []) if b.get('executable_sha256')]
    if reader is None:
        return {'status': 'operator_confirmation_required', 'registered': [],
                'candidates': [build_view(b) for b in builds], 'content_is_untrusted': True,
                'next': '环境配置只能由操作者在交互菜单（/software register）中逐项确认；模型不能代为登记或代选。'}
    root = profiles.compute_config(box.workspace, server)['root']
    result = record['result']
    output(f"环境配置登记 · {server} · 探测于 {result.get('scanned_at')} · 探测版本 {result.get('probe_version')}")
    output(f"探测根目录：{'、'.join(result.get('roots', []))}；命令 {len(result.get('commands', []))} 条，均有超时，无递归搜索。")
    output('登记只记录脚本、可执行文件哈希和能力标签；准备任务时按阶段所需能力选择，哈希变化即拒绝。')
    registered, skipped = [], []
    for number, build in enumerate(builds, 1):
        existing = profiles.load_profiles(box.workspace, server)
        output(build_text(number, len(builds), build))
        same = [row for row in existing if row['executable_sha256'] == build['executable_sha256']]
        if same:
            output('已登记：' + '、'.join(row['name'] for row in same) + '（可执行文件哈希相同）。')
        name = suggest_name(build, existing)
        plan = env_plan(build, root, name)
        if plan is None:
            output('没有通过检查的环境脚本，也没有可加载它的模块；不能登记。')
            skipped.append({'executable': build['executable'], 'reason': 'no_environment_script'})
            continue
        output(f"环境脚本：{plan['path']}（{ {'configured': '服务器计算配置当前使用', 'agent_script': 'Agent environments/ 中已有', 'build_script': '构建自带 scripts/env.sh，已检查', 'generated': '将由模块 ' + str(plan.get('module')) + ' 生成'}[plan['kind']] }）")
        output(options([('y', f'登记为 {name}'), ('名称', f'以其他名称登记（{NAME_HINT}）'), ('n', '跳过'), ('q', '结束')]))
        answer = reader('输入选择：').strip()
        if answer.lower() == 'q':
            break
        if answer.lower() in ('', 'n'):
            skipped.append({'executable': build['executable'], 'reason': 'operator_skipped'})
            continue
        if answer.lower() != 'y':
            if not profiles.NAME.fullmatch(answer) or answer == profiles.DEFAULT:
                output(f'名称无效（{NAME_HINT}，不能是 default）；跳过此构建。')
                skipped.append({'executable': build['executable'], 'reason': 'invalid_name'})
                continue
            name = answer
            plan = env_plan(build, root, name)
        replaced = next((row for row in existing if row['name'] == name), None)
        if replaced and replaced['executable_sha256'] != build['executable_sha256']:
            output(f"将替换已登记的 {name}（原可执行文件 {replaced['executable_sha256'][:12]}）。"
                   '绑定旧记录的任务不会改用新环境：提交时会因哈希不同被拒绝，需要新建任务。')
        if plan['content'] is not None:
            output(f"将在远程写入 {plan['path']}：")
            output('\n'.join('    ' + line for line in plan['content'].splitlines()))
            if reader('输入“写入”确认；其他输入跳过：').strip() != '写入':
                skipped.append({'executable': build['executable'], 'reason': 'operator_skipped'})
                continue
            from vasp_agent.remote.server_setup import install_env_script
            install_env_script(box.workspace, server, plan['path'], plan['content'])
        notes = reader('备注（如补丁说明；可空）：').strip()[:300]
        evidence = remote(box, server).call('environment_profile_check', env_script=plan['path'])
        if evidence.get('executable_sha256') != build['executable_sha256']:
            output(f"未登记：脚本现在解析到 {evidence.get('executable')}（{str(evidence.get('executable_sha256'))[:12]}），"
                   '与探测到的构建不同。请重新 /software scan 后再登记。')
            skipped.append({'executable': build['executable'], 'reason': 'executable_changed'})
            continue
        if plan['sha256'] and evidence.get('environment_sha256') != plan['sha256']:
            output('未登记：环境脚本自探测后已改变。请重新 /software scan 后再登记。')
            skipped.append({'executable': build['executable'], 'reason': 'script_changed'})
            continue
        profile = {'name': name, 'env_script': plan['path'], 'environment_sha256': evidence['environment_sha256'],
                   'executable': evidence['executable'], 'executable_sha256': evidence['executable_sha256'],
                   'version': build.get('version'), 'capabilities': list(build.get('capabilities', [])),
                   'flags': build.get('flags', []), 'script_source': plan['kind'], 'module': plan.get('module'),
                   'constraint_markers': evidence.get('constraint_markers', []), 'notes': notes,
                   'probe_version': result.get('probe_version'), 'scanned_at': result.get('scanned_at'),
                   'confirmed_at': datetime.now(timezone.utc).isoformat(timespec='seconds'),
                   'confirmed_by': 'operator_menu'}
        profiles.save_profile(box.workspace, server, profile)
        registered.append(name)
        output(f'已登记环境配置 {name}。')
    box.log({'environment_profiles_registered': {'server': server, 'registered': registered, 'skipped': skipped}})
    return {'status': 'registered' if registered else 'nothing_registered', 'registered': registered,
            'skipped': skipped, 'selection': {'source': 'operator_menu', 'operator_confirmed': bool(registered)},
            'profiles': [profiles.brief(row) for row in profiles.candidates(box.workspace, server)]}


def tool(box, server, action):
    """software_environments: read the saved record, run the read-only probe, or request the menu."""
    if action == 'scan':
        scan(box, server)
        return view(box, server)
    if action == 'register':
        return register(box, server)
    return view(box, server)


def show(c, server):
    data = view(c.agent.tools, server)
    inventory = data['inventory']
    if inventory is None:
        c.output(f'{server}：尚无探测记录。输入 /software scan {server} 只读探测。')
    else:
        c.output(f"{server} · 探测于 {inventory['scanned_at']} · 根目录 {'、'.join(inventory['roots'] or [])}")
        rows = [['VASP', 'vasp_std', '能力', '环境脚本']]
        for build in inventory['vasp_builds']:
            scripts = [row['path'] for row in build['env_scripts'] if row['passes']]
            rows.append([build['version'] or '?', build['executable'], '、'.join(build['capabilities']) or '-',
                         scripts[0] if scripts else ('模块 ' + build['modules'][0] if build['modules'] else '无')])
        c.output(table(rows))
        if inventory['tools']:
            c.output(table([['工具', '路径', '版本']] + [[t['tool'], t['executable'], t.get('version') or '?']
                                                       for t in inventory['tools']]))
        if inventory['timeouts'] or inventory['truncated']:
            c.output(f"注意：{inventory['timeouts']} 条命令超时；截断：{'、'.join(inventory['truncated']) or '无'}。")
    c.output('环境配置：')
    c.output(table([['名称', '环境脚本', '能力']] + [[row['name'] + ('（隐式）' if row['implicit'] else ''), row['env_script'],
                                                  '、'.join(row['capabilities']) or '-'] for row in data['profiles']]))


def execute(c, arg):
    from vasp_agent.chat.extra_commands import choose, configured_servers
    words = arg.split()
    # A bare /software (optionally with a server) walks the operator through
    # show -> scan -> register with one-key menus; explicit subcommands stay
    # single-step for scripting.
    guided = not words or words[0] not in ('show', 'scan', 'register')
    action = words.pop(0) if not guided else 'show'
    servers = configured_servers(c.agent.tools)
    if words and words[0] in servers:
        server = words.pop(0)
    elif len(servers) == 1:
        server = servers[0]
    else:
        server = choose(c, servers, '选择服务器')
        if not server:
            return
    if action == 'scan':
        run_scan(c, server, words)
    elif action == 'register':
        register(c.agent.tools, server, c.reader, c.output)
    else:
        show(c, server)
    if not guided:
        return
    has_inventory = profiles.load_inventory(c.agent.tools.workspace, server) is not None
    if has_inventory:
        c.output('  1  重新只读探测\n  2  登记环境配置（逐项确认）\n  q  返回')
    else:
        c.output('  1  现在只读探测（约 1–4 分钟，不启动 VASP）\n  q  返回')
    choice = c.reader('选择：').strip().lower()
    if choice == '1':
        run_scan(c, server, words)
        c.output('  1  现在登记环境配置（逐项确认）\n  q  稍后')
        if c.reader('选择：').strip().lower() != '1':
            return
    elif choice != '2' or not has_inventory:
        return
    register(c.agent.tools, server, c.reader, c.output)


def run_scan(c, server, roots):
    c.output(f'只读探测 {server}：module avail、已配置环境脚本、根目录 {"、".join(roots) or "（沿用上次或 ~/apprepo）"} '
             '下的已知布局；每条命令有超时，不做递归搜索，不启动 VASP（约 1–4 分钟）……')
    scan(c.agent.tools, server, roots or None)
    show(c, server)
