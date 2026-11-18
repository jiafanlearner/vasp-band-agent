"""/setup-server: probe a connected cluster read-only and write its compute profile.

Every value is a candidate the operator picks or edits; nothing is written,
locally or remotely, before the final confirmation.
"""
import re
from pathlib import PurePosixPath

from vasp_agent.ui.menu_text import table


class Cancelled(Exception):
    pass


def absolute_path(value):
    value = str(value).strip()
    if not value.startswith('/') or any(c in value for c in '\r\n\x00') or '..' in PurePosixPath(value).parts:
        raise ValueError('需要 Linux 绝对路径（以 / 开头，不含 ..）。')
    return value.rstrip('/') or '/'


def root_path(value):
    value = absolute_path(value)
    if len(PurePosixPath(value).parts) < 4:
        raise ValueError('远程根目录需至少三级（如 /home/用户/vasp-agent），避免写入共享目录。')
    return value


def partition_name(value):
    value = str(value).strip()
    if not re.fullmatch(r'[A-Za-z0-9_-]+', value):
        raise ValueError('分区名只能包含字母、数字、下划线、短横线。')
    return value


def core_count(value):
    text = str(value).strip()
    if not text.isascii() or not text.isdigit() or not 1 <= int(text) <= 256:
        raise ValueError('核数须为 1–256 的整数。')
    return int(text)


def walltime(value):
    from vasp_agent.remote.remote_worker import wall_seconds
    value = str(value).strip()
    wall_seconds(value)
    return value


def pick(c, step, title, header, rows, values, default=None, extra=(), validate=None, notes=()):
    """One menu: numbered candidates, manual entry, Enter for the default.

    Returns the chosen (validated) value, or ('extra', key) for an extra option.
    """
    c.output(f'\n[{step}] {title}')
    for note in notes:
        c.output(f'  · {note}')
    if rows:
        c.output(table([['#', *header]] + [[str(i), *row] for i, row in enumerate(rows, 1)]))
    else:
        c.output('  （未探测到候选）')
    hints = ([f'1–{len(rows)} 选择'] if rows else []) + ['e 手动输入'] + [f'{k} {label}' for k, label in extra]
    if default is not None:
        hints.append(f'回车 采用 {default}')
    hints.append('q 取消')
    while True:
        answer = c.reader('  ' + ' · '.join(hints) + '：').strip()
        if answer == 'q':
            raise Cancelled
        if answer in dict(extra):
            return ('extra', answer)
        if answer == '' and default is not None:
            value = default
        elif answer.isascii() and answer.isdigit() and 1 <= int(answer) <= len(rows):
            value = values[int(answer) - 1]
        elif answer == 'e':
            value = c.reader('  输入值：').strip()
        else:
            c.output('  输入无效，请重试。')
            continue
        try:
            return validate(value) if validate and not isinstance(value, tuple) else value
        except ValueError as exc:
            c.output('  ' + str(exc))


def dash(value):
    return '-' if value in (None, '') else value


def choose_python(c, step, found, current, extra=()):
    pythons = found['pythons']
    rows = [[p['path'], p['version'], dash(p['pymatgen']), dash(p['numpy']), dash(p['matplotlib']),
             '可用' if p['ready'] else '缺依赖'] for p in pythons]
    ready = [p['path'] for p in pythons if p['ready']]
    default = current if current in ready else (ready[0] if ready else current)
    notes = ['需能导入 pymatgen.io.vasp、numpy、matplotlib；作业和部署都用这个 Python。']
    if not ready:
        notes.append('没有候选能导入全部依赖；可输入 v 在远程根目录建虚拟环境并安装。')
    if found['python_modules']:
        notes.append('另有模块（需加载后才有路径，可加载后用 which python3 查到再手动输入）：'
                     + '、'.join(found['python_modules'][:6]))
    return pick(c, step, 'Python', ['路径', '版本', 'pymatgen', 'numpy', 'matplotlib', '状态'], rows,
                [p['path'] for p in pythons], default, extra, absolute_path, notes)


def choose_vasp_env(c, step, found, current, root):
    """Return (vasp_env path, script content to install or None)."""
    from vasp_agent.remote.server_setup import env_script
    rows, values = [], []
    for env in found['vasp_envs']:
        rows.append(['脚本', env['path'], env['vasp_std']])
        values.append(('script', env['path']))
    for module in found['vasp_modules']:
        rows.append(['模块', module, f'将生成 {root}/vasp-env.sh'])
        values.append(('module', module))
    if found['vasp_on_path']:
        rows.append(['PATH', found['vasp_on_path'], f'将生成 {root}/vasp-env.sh'])
        values.append(('path', found['vasp_on_path']))
    known = [v[1] for v in values if v[0] == 'script']
    default = current if current in known else (known[0] if known else current)
    notes = ['作业脚本会 source 此文件后调用 vasp_std；只列出能解析出 vasp_std 的脚本。',
             '选模块或 PATH 时会在远程根目录生成环境脚本，最终确认后才写入。']
    choice = pick(c, step, 'VASP 环境', ['类型', '来源', 'vasp_std / 处理'], rows, values, default,
                  validate=absolute_path, notes=notes)
    if not isinstance(choice, tuple):
        return choice, None
    kind, value = choice
    if kind == 'script':
        return value, None
    path = root + '/vasp-env.sh'
    if kind == 'module':
        return path, env_script(module=value)
    return path, env_script(binary_dir=str(PurePosixPath(value).parent))


def choose_partition(c, step, found, current):
    parts = found['partitions']
    rows = [[p['name'] + (' (默认)' if p['default'] else ''), dash(p['cores']), dash(p['default_time']),
             dash(p['max_time']), p['state']] for p in parts]
    names = [p['name'] for p in parts]
    default = current if current in names or (current and not names) else (names[0] if names else None)
    notes = [] if found['sbatch'] else ['登录节点上未找到 sbatch；请确认这是 Slurm 集群。']
    return pick(c, step, 'Slurm 分区', ['分区', '每节点核数', '默认时限', '最长时限', '状态'], rows, names,
                default, validate=partition_name, notes=notes)


def bootstrap_python(c, name, found, root, download=None):
    """Create <root>/venv from a cluster Python and install the pinned engine packages."""
    import tempfile
    from vasp_agent.remote import server_setup as setup
    requirements = setup.engine_requirements()
    bases = [p for p in found['pythons'] if (setup.python_version(p['version']) or (0, 0)) >= setup.MIN_PYTHON]
    venv = root + '/venv/bin/python'
    if not bases:
        raise ValueError('集群上没有 Python ≥ 3.10 可作为虚拟环境基础；请先加载新版 Python 模块或安装 Miniforge 到个人目录。')
    base = pick(c, '2/7 · 虚拟环境', '选择基础 Python（只用来创建虚拟环境，不向它安装任何包）',
                ['路径', '版本', '平台'], [[p['path'], p['version'], f'{p["machine"]} {p["libc"]}'] for p in bases],
                [p['path'] for p in bases], bases[0]['path'], validate=absolute_path)
    row = next((p for p in bases if p['path'] == base), None)
    if row is None:
        raise ValueError('手动输入的基础 Python 未经探测，无法确定版本和平台；请从列表中选择。')
    online = found['network']
    c.output(f'\n将在 {root}/venv 创建虚拟环境（已存在则复用），安装：' + '、'.join(requirements))
    c.output('  · 版本来自 requirements-tested.txt；其余依赖由 pip 按这三项解析。不使用 sudo，不改动基础 Python。')
    modes = [('1', '集群上直接从 PyPI 安装' + ('（探测到可访问 PyPI）' if online else '（探测到无法访问 PyPI）')),
             ('2', '本机按集群平台下载 wheel，上传到 ' + setup.wheel_directory(root, row['version'], row['machine']) + ' 后离线安装')]
    c.output('\n'.join(f'  {k}  {label}' for k, label in modes))
    mode = c.reader(f'输入 1 或 2 开始安装（回车 {"1" if online else "2"}），其他输入取消：').strip() or ('1' if online else '2')
    if mode not in ('1', '2'):
        raise Cancelled
    wheel_dir = None
    if mode == '2':
        wheel_dir = setup.wheel_directory(root, row['version'], row['machine'])
        with tempfile.TemporaryDirectory(prefix='vasp-agent-wheels-') as local:
            c.output(f'本机下载 cp{"".join(row["version"].split(".")[:2])} / {row["machine"]} / {row["libc"]} 的 wheel……')
            files = (download or setup.download_wheels)(local, requirements, row['version'], row['machine'], row['libc'])
            uploaded = setup.upload_wheels(c.agent.tools.workspace, name, local, wheel_dir)
        c.output(f'已上传 {len(files)} 个 wheel（{uploaded["bytes"] // 1024} KiB）到 {wheel_dir}。')
    c.output('安装中（可能需要数分钟）……')
    report = setup.bootstrap(c.agent.tools.workspace, name, root, base, requirements, wheel_dir)
    c.output(('已创建' if report['created'] else '已复用') + f'虚拟环境 {venv}；导入 pymatgen.io.vasp/numpy/matplotlib 成功。')
    if report['installed']:
        c.output(f'本次安装或变更 {len(report["installed"])} 个包：')
        c.output(table([['包', '版本', '原版本']] + [[k, v, report['previous'].get(k, '-')] for k, v in report['installed'].items()]))
    else:
        c.output('所需包均已安装，未变更任何包。')
    return venv


def summary(current, cfg, keys):
    rows = [['项目', '当前', '新值']]
    for key in keys:
        old, new = current.get(key), cfg.get(key)
        rows.append([key, dash(old), f'{new}' + ('' if old == new else '  ← 更改' if old is not None else '  ← 新增')])
    return table(rows)


def wizard(c, name, found, current, extras=None):
    """Walk the operator through every field; returns (cfg, pending env script) or raises Cancelled."""
    from vasp_agent.remote.server_setup import COMPUTE_KEYS
    extras = extras or {}
    cfg = dict(current)
    home = found['home'] or ''
    root_candidates = [v for v in dict.fromkeys([current.get('root'), home and home + '/vasp-agent']) if v]
    cfg['root'] = pick(c, '1/7', '远程根目录（任务、release、虚拟环境都放在这里）', ['路径'],
                       [[v] for v in root_candidates], root_candidates,
                       root_candidates[0] if root_candidates else None, validate=root_path)
    python = choose_python(c, '2/7', found, current.get('python'), extras.get('python_extra', ()))
    if isinstance(python, tuple):
        python = extras['python_action'](cfg, python[1])
    cfg['python'] = python
    cfg['vasp_env'], pending = choose_vasp_env(c, '3/7', found, current.get('vasp_env'), cfg['root'])
    libraries = found['potcar_libraries']
    known = [lib['path'] for lib in libraries]
    cfg['potcar_library'] = pick(
        c, '4/7', 'POTCAR 库（PBE）', ['路径', '名称含 PBE'], [[lib['path'], '是' if lib['pbe_named'] else '否'] for lib in libraries],
        known, current.get('potcar_library') or (known[0] if known else None), validate=absolute_path,
        notes=['按含 Si/POTCAR 的元素子目录识别，只列路径，未读取 POTCAR 内容。', '请确认所选为 PBE 库（不是 LDA/PW91）。'])
    cfg['partition'] = choose_partition(c, '5/7', found, current.get('partition'))
    partition = next((p for p in found['partitions'] if p['name'] == cfg['partition']), {})
    cores = [n for n in dict.fromkeys([current.get('ntasks'), partition.get('cores')]) if n]
    cfg['ntasks'] = pick(c, '6/7', '默认核数（单节点；准备任务时的默认总核数）', ['核数'], [[n] for n in cores], cores,
                         cores[0] if cores else None, validate=core_count,
                         notes=[f'分区 {cfg["partition"]} 每节点 {partition["cores"]} 核。'] if partition.get('cores') else [])
    times = [t for t in dict.fromkeys([current.get('walltime'), partition.get('default_time'), partition.get('max_time')]) if t]
    labels = {current.get('walltime'): '当前配置', partition.get('default_time'): '分区默认', partition.get('max_time'): '分区上限'}
    cfg['walltime'] = pick(c, '7/7', '默认运行时限 HH:MM:SS', ['时限', '来源'], [[t, labels.get(t, '')] for t in times], times,
                           times[0] if times else None, validate=walltime)
    if partition.get('cores') and cfg['ntasks'] > partition['cores']:
        c.output(f'  注意：{cfg["ntasks"]} 核超过分区每节点 {partition["cores"]} 核，单节点作业将无法调度。')
    return {k: cfg[k] for k in (*COMPUTE_KEYS, *[k for k in cfg if k not in COMPUTE_KEYS])}, pending


def show_findings(c, found):
    c.output(table([
        ['Python', f'{len(found["pythons"])} 个（{sum(p["ready"] for p in found["pythons"])} 个依赖齐全）'],
        ['Slurm 分区', f'{len(found["partitions"])} 个' + ('' if found['sbatch'] else '（未找到 sbatch）')],
        ['VASP', f'{len(found["vasp_envs"])} 个环境脚本，{len(found["vasp_modules"])} 个模块'
                 + ('，登录 PATH 有 vasp_std' if found['vasp_on_path'] else '')],
        ['POTCAR 库', f'{len(found["potcar_libraries"])} 个候选'],
        ['外网 (PyPI)', {True: '可访问', False: '不可访问', None: '未知'}[found['network']]]]))
    if found.get('warning'):
        c.output('  ' + found['warning'])


def execute(c, arg, download=None):
    from vasp_agent.chat.extra_commands import choose
    from vasp_agent.remote.remote_servers import Servers
    from vasp_agent.remote.server_setup import (COMPUTE_KEYS, install_env_script, load_compute, probe,
                                                validate_compute, write_compute)
    workspace = c.agent.tools.workspace
    servers = Servers(workspace)
    name = arg.strip()
    if not name:
        names = [row['name'] for row in servers.list()]
        if not names:
            c.output('尚无服务器连接。请先在对话中导入只引用密钥路径的 SSH 登录脚本（servers/README.md）。')
            return
        name = choose(c, names, '选择要配置计算环境的服务器')
        if not name:
            return
    if not servers.file(name).is_file():
        raise ValueError(f'没有服务器连接 {name}；请先导入 SSH 登录脚本。')
    servers.load(name)
    current = load_compute(workspace, name) or {}
    c.output(f'只读探测 {name}：Python、Slurm 分区、VASP 环境、POTCAR 库路径（不写入、不读取 POTCAR 内容，约 1–3 分钟）……')
    found = probe(workspace, name, current)
    show_findings(c, found)
    extras = {'python_extra': (('v', '在远程根目录建虚拟环境并安装依赖'),),
              'python_action': lambda cfg, key: bootstrap_python(c, name, found, cfg['root'], download)}
    try:
        cfg, pending = wizard(c, name, found, current, extras)
        validate_compute(cfg)
        c.output('\n确认写入 servers/' + name + '.compute.json：')
        c.output(summary(current, cfg, COMPUTE_KEYS))
        if pending:
            c.output(f'  远程将写入环境脚本 {cfg["vasp_env"]}：')
            c.output('\n'.join('    ' + line for line in pending.splitlines()))
        if current:
            c.output('  原配置将先备份为同目录 .bak-时间戳 文件。')
        if c.reader('输入“写入”确认；其他输入取消：').strip() != '写入':
            raise Cancelled
    except Cancelled:
        c.output('已取消；未写入任何文件。')
        return
    if pending:
        installed = install_env_script(workspace, name, cfg['vasp_env'], pending)
        c.output(f'已写入远程环境脚本 {installed["path"]}；'
                 + (f'解析到 vasp_std：{installed["vasp_std"]}' if installed['vasp_std']
                    else '但未能通过它解析 vasp_std，请检查模块名后重新运行 /setup-server。'))
    result = write_compute(workspace, name, cfg)
    c.output(f'已写入 {result["written"]}' + (f'；原配置备份为 {result["backup"]}' if result['backup'] else '') + '。')
    offer_deploy(c, name)
    return cfg


def offer_deploy(c, name):
    from vasp_agent.chat.release_check import check, short
    try:
        state = check(c.agent.tools, name)
    except Exception as exc:
        c.output(f'未能查询引擎部署版本（{type(exc).__name__}）；稍后可用 /servers 查看。')
        return
    if state['current']:
        c.output(f'服务器引擎已是本机当前版本 {short(state["local_version"])}。')
        return
    c.output(f'服务器引擎{"尚未部署" if not state["deployed"] else "不是本机当前版本"}'
             f'（本机 {short(state["local_version"])}，已部署 {short(state["deployed_version"])}）。')
    if c.reader('输入 1 立即部署（上传引擎源码到 releases/ 并检查 Python 依赖）；回车跳过：').strip() == '1':
        deployed = c.agent.tools.remote_deploy(name)
        c.output(f'已部署 {deployed.get("release")}。')
