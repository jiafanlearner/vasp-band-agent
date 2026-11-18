"""Operator-only approval choices; model arguments cannot set policy."""
from vasp_agent.editing.workspace_editor import decode


def record_mode(box, mode, channel):
    """Audit every policy change so receipts saying operator_session can be traced."""
    previous = getattr(box, 'local_analysis_mode', 'ask')
    box.local_analysis_mode = mode
    log = getattr(box, 'log', None)
    if callable(log) and previous != mode:
        log({'permission': 'local_analysis_mode', 'from': previous, 'to': mode,
             'confirmed_by': channel, 'scope': 'this_session'})


def settings(box):
    reader = box.plan_reader
    output = getattr(box, 'plan_output', print)
    if reader is None:
        return {'status': 'operator_input_required'}
    output('脚本批准模式（不改变计算提交授权）\n1. 请求批准：每次本机运行询问（默认）\n2. 帮我批准：仅允许隔离容器自动运行，本机运行关闭\n3. 本会话本机执行：未检测到风险操作的脚本免询问，非沙箱\n回车/q 保持当前设置。')
    choice = reader('选择批准模式：').strip()
    if choice == '3':
        output('本机脚本可访问网络、账户文件、SSH及外部命令；可能绕过工具提交流程。')
        if reader('确认本会话本机脚本免询问？输入 y 确认：').strip().lower() not in ('y', 'yes', '是'):
            return {'status': 'cancelled', 'mode': getattr(box, 'local_analysis_mode', 'ask')}
    modes = {'1': 'ask', '2': 'container_only', '3': 'session_trusted'}
    if choice in modes:
        record_mode(box, modes[choice], 'operator_settings_menu')
    return {'mode': getattr(box, 'local_analysis_mode', 'ask'), 'submission_permission_changed': False}


NETWORK_MODULES = {'socket', 'urllib', 'http', 'requests', 'httpx', 'aiohttp', 'ftplib', 'smtplib', 'poplib',
                   'imaplib', 'telnetlib', 'paramiko', 'ssl', 'websocket', 'websockets', 'xmlrpc', 'asyncssh'}
PROCESS_MODULES = {'subprocess', 'pty', 'ctypes', 'winreg', 'pexpect'}
# Builtins called by bare name (re.compile or str.replace are not these).
BUILTIN_RISKS = {'eval': '动态执行代码', 'exec': '动态执行代码', 'compile': '动态执行代码', '__import__': '动态导入'}
# Method names no string, list or dict has: risky whatever object they are called on.
METHOD_RISKS = {'unlink': '删除文件', 'rmdir': '删除目录', 'rmtree': '删除目录', 'rename': '改名/移动文件',
                'chmod': '修改权限', 'chown': '修改权限', 'write_text': '写文件', 'write_bytes': '写文件',
                'symlink_to': '创建链接', 'hardlink_to': '创建链接', 'popen': '调用外部命令'}
# Common names (remove, replace, move, system…) are risky only on these modules.
MODULE_OWNERS = {'os', 'shutil', 'importlib', 'subprocess', 'socket', 'pty', 'ctypes'}
OWNED_RISKS = {'system': '调用外部命令', 'remove': '删除文件', 'removedirs': '删除目录', 'replace': '改名/移动文件',
               'move': '改名/移动文件', 'copy': '复制文件', 'copyfile': '复制文件', 'copytree': '复制目录',
               'symlink': '创建链接', 'import_module': '动态导入', 'run': '调用外部命令', 'call': '调用外部命令',
               'check_output': '调用外部命令', 'Popen': '调用外部命令', 'create_connection': '联网', 'socket': '联网'}


def script_risks(source):
    """Static notes for the operator: imports and operations worth a look before running.

    A relative-path open() for writing is not flagged: local runs execute inside a
    temporary snapshot directory. Static checks cannot prove a script safe.
    """
    import ast
    tree = ast.parse(source)
    imports, flags = set(), []
    def flag(node, text):
        flags.append(f'第 {getattr(node, "lineno", "?")} 行：{text}')
    for node in ast.walk(tree):
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            names = [alias.name for alias in node.names] if isinstance(node, ast.Import) else [node.module or '']
            for name in names:
                top = name.split('.')[0]
                imports.add(top)
                if top in NETWORK_MODULES:
                    flag(node, f'导入联网模块 {name}')
                elif top in PROCESS_MODULES:
                    flag(node, f'导入进程/系统模块 {name}')
        elif isinstance(node, ast.Call):
            func = node.func
            owner = func.value.id if isinstance(func, ast.Attribute) and isinstance(func.value, ast.Name) else None
            name = func.attr if isinstance(func, ast.Attribute) else func.id if isinstance(func, ast.Name) else ''
            if owner == 'os' and name.startswith(('exec', 'spawn')):
                flag(node, f'调用外部程序（os.{name}）')
            elif isinstance(func, ast.Name) and name in BUILTIN_RISKS:
                flag(node, BUILTIN_RISKS[name] + f'（{name}）')
            elif isinstance(func, ast.Attribute) and (name in METHOD_RISKS or (owner in MODULE_OWNERS and name in OWNED_RISKS)):
                flag(node, {**OWNED_RISKS, **METHOD_RISKS}[name] + f'（{owner + "." if owner else ""}{name}）')
            elif (name == 'open' and len(node.args) > 1 and isinstance(node.args[1], ast.Constant)
                  and any(c in str(node.args[1].value) for c in 'wax+')):
                target = node.args[0]
                if not (isinstance(target, ast.Constant) and isinstance(target.value, str) and not _outside(target.value)):
                    flag(node, '写入路径不是固定的相对路径')
        elif isinstance(node, ast.Constant) and isinstance(node.value, str) and _outside(node.value):
            flag(node, f'引用临时目录以外的路径 {node.value[:60]!r}')
    return {'lines': len(source.splitlines()), 'imports': sorted(i for i in imports if i), 'flags': flags}


def _outside(text):
    import re
    return bool(re.match(r'^(?:[A-Za-z]:[\\/]|/|~|\\\\)', text) or '..' in text.replace('\\', '/').split('/'))


def approve(box, files, manifest, seconds):
    mode = getattr(box, 'local_analysis_mode', 'ask')
    output = getattr(box, 'plan_output', print)
    if mode == 'container_only':
        return None
    source = decode(files['main.py'])
    risks = script_risks(source)
    import hashlib
    script = manifest[0] if manifest else {'path': 'main.py', 'sha256': hashlib.sha256(files['main.py']).hexdigest()}
    inputs = [item['path'] for item in manifest[1:]]
    output(f"本机 Python 执行（非沙箱）：{script['path']}，{risks['lines']} 行，sha256 {script['sha256'][:12]}…\n"
           f"输入 {len(inputs)} 个：{'、'.join(inputs) or '无'}；时限 {seconds} 秒；在临时快照目录中运行\n"
           f"导入：{'、'.join(risks['imports']) or '无'}")
    if risks['flags']:
        output('需要注意的操作：\n' + '\n'.join('  · ' + item for item in risks['flags'][:20]))
    else:
        output('未检测到联网、外部命令、删除/改名文件、绝对路径或动态执行（静态检查不能证明脚本安全）。')
    if mode == 'session_trusted':
        if not risks['flags']:
            output('本会话已允许本机执行，且未检测到需要注意的操作：直接运行。')
            return {'source': 'operator_session', 'mode': mode, 'risk_flags': []}
        output('本会话已允许本机执行，但该脚本含上述操作，需要单独确认。')
    if box.plan_reader is None:
        return None
    output('1. 仅批准此次\n2. 本会话免询问（之后只有未检测到风险操作的脚本会自动运行）\n3. 拒绝（默认）\nv. 查看完整代码')
    while True:
        choice = box.plan_reader('批准本机执行？[3]：').strip().lower()
        if choice != 'v':
            break
        output('完整代码：\n' + source)
    if choice == '1':
        return {'source': 'operator_once', 'mode': 'once', 'risk_flags': risks['flags']}
    if choice == '2' and box.plan_reader('确认本会话内本机脚本免询问？输入 y 确认：').strip().lower() in ('y', 'yes', '是'):
        record_mode(box, 'session_trusted', 'operator_approval_prompt')
        return {'source': 'operator_session', 'mode': 'session_trusted', 'risk_flags': risks['flags']}
    return None
