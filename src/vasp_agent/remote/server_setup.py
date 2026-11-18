"""Read-only cluster probe and compute-profile writing for /setup-server.

The probe is one bash program sent on stdin over the saved SSH connection. It
only lists and queries: candidate Pythons and their package versions, Slurm
partitions, VASP environment scripts or modules, and POTCAR library paths. It
never reads a POTCAR body and never launches VASP. Everything it reports is a
candidate; the operator picks and confirms before anything is written.
"""
import json
import math
import os
import re
import shlex
import subprocess
import time
from datetime import datetime
from pathlib import Path, PurePosixPath

from vasp_agent.remote import ssh_pool
from vasp_agent.remote.remote_servers import Servers, run_ssh, ssh_error


PROBE_TIMEOUT = 600
COMPUTE_KEYS = ('root', 'python', 'potcar_library', 'vasp_env', 'partition', 'ntasks', 'walltime')
ENGINE_PACKAGES = ('pymatgen', 'numpy', 'matplotlib')

# Shared by the probe, the software inventory and generated environment scripts:
# non-interactive bash over SSH usually has no `module` function until an init
# file is sourced. Defined in the stdlib worker unit, which cannot import this module.
from vasp_agent.remote.worker_inventory import MODULE_INIT  # noqa: E402

PY_VERSIONS = r'''import platform
def ver(name, probe):
    try:
        from importlib.metadata import version
        found = version(name)
    except Exception:
        found = None
    try:
        module = __import__(probe)
    except Exception:
        return "-"
    return found or str(getattr(module, "__version__", "?"))
libc = "-".join(x for x in platform.libc_ver() if x) or "-"
print("\t".join([platform.python_version(), ver("pymatgen", "pymatgen.io.vasp"), ver("numpy", "numpy"),
                 ver("matplotlib", "matplotlib"), platform.machine() or "-", libc]))
'''

PROBE_BODY = r'''
exec 2>/dev/null
printf 'HOME\t%s\n' "$HOME"
printf 'SBATCH\t%s\n' "$(command -v sbatch)"
PYCODE=$(cat <<'VASP_AGENT_PY'
@PY_VERSIONS@
VASP_AGENT_PY
)
cands=""
add() { case " $cands " in *" $1 "*) ;; *) if [ -x "$1" ]; then cands="$cands $1"; fi;; esac; }
for n in python3 python; do p=$(command -v "$n") && add "$p"; done
for base in "$HOME/miniconda3" "$HOME/anaconda3" "$HOME/miniforge3" "$HOME/mambaforge" "$HOME/.conda"; do
  add "$base/bin/python"
  for e in "$base"/envs/*/bin/python; do add "$e"; done
done
if command -v conda >/dev/null; then
  for e in $(conda env list | awk '!/^#/ && NF {print $NF}'); do add "$e/bin/python"; done
fi
if [ -n "$ROOT" ]; then add "$ROOT/venv/bin/python"; fi
if [ -n "$CURRENT_PYTHON" ]; then add "$CURRENT_PYTHON"; fi
for p in $cands; do
  printf 'PY\t%s\t%s\n' "$p" "$(timeout 90 "$p" -c "$PYCODE" | tail -n 1)"
done
@MODULE_INIT@
if command -v module >/dev/null; then
  module -t avail 2>&1 | grep -iE '^(python|anaconda|miniconda|miniforge|conda)[^:]*$' | head -20 | while IFS= read -r m; do printf 'PYMOD\t%s\n' "$m"; done
  module -t avail 2>&1 | grep -iE 'vasp[^:]*$' | head -20 | while IFS= read -r m; do printf 'VASPMOD\t%s\n' "$m"; done
fi
sinfo -h -o '%P|%c|%l|%a' | while IFS= read -r l; do printf 'PART\t%s\n' "$l"; done
scontrol show partition -o | while IFS= read -r l; do printf 'PCONF\t%s\n' "$l"; done
printf 'VASPBIN\t%s\n' "$(command -v vasp_std)"
envs=""
if [ -n "$CURRENT_ENV" ]; then envs="$CURRENT_ENV"; fi
for f in $(timeout 30 find "$HOME" /opt /public/software /share/apps /soft /apps /software -maxdepth 4 -type f \
    \( -iname '*vasp*env*' -o -iname '*env*vasp*' -o -iname 'vasp*.sh' \) | head -40); do
  envs="$envs $f"
done
for f in $envs; do
  # Only scripts that set an environment; anything that could start VASP or a job is skipped unsourced.
  if grep -qE '(mpirun|mpiexec|srun|sbatch|^[[:space:]]*vasp_(std|gam|ncl))' "$f"; then continue; fi
  printf 'VASPENV\t%s\t%s\n' "$f" "$(timeout 15 bash -c 'source "$1" >/dev/null 2>&1 && command -v vasp_std' vasp-agent-probe "$f")"
done
for d in "$VASP_PP_PATH" "$PMG_VASP_PSP_DIR"; do
  if [ -n "$d" ]; then for s in "$d"/*/Si/POTCAR "$d"/Si/POTCAR; do if [ -f "$s" ]; then printf 'POTSI\t%s\n' "$s"; fi; done; fi
done
timeout 40 find "$HOME" /opt /public/software /share/apps /soft /apps /software /work -maxdepth 6 -type f -path '*/Si/POTCAR' | head -20 | while IFS= read -r s; do printf 'POTSI\t%s\n' "$s"; done
if [ -n "$CURRENT_POTCAR" ] && [ -d "$CURRENT_POTCAR" ]; then printf 'POTSI\t%s/Si/POTCAR\n' "$CURRENT_POTCAR"; fi
code=$(timeout 10 curl -s -o /dev/null -w '%{http_code}' https://pypi.org/simple/pip/)
if [ "$code" = 200 ]; then printf 'NET\tyes\n'; else printf 'NET\tno\n'; fi
printf 'END\n'
'''


def probe_program(current=None):
    """The probe as one bash program; existing profile values join the candidates."""
    current = current or {}
    lines = []
    for var, key in (('ROOT', 'root'), ('CURRENT_PYTHON', 'python'), ('CURRENT_ENV', 'vasp_env'),
                     ('CURRENT_POTCAR', 'potcar_library')):
        value = current.get(key)
        lines.append(var + '=' + shlex.quote(value if isinstance(value, str) and value.startswith('/') else ''))
    body = PROBE_BODY.replace('@PY_VERSIONS@', PY_VERSIONS.strip()).replace('@MODULE_INIT@', MODULE_INIT)
    return '\n'.join(lines) + body


def run_remote(workspace, server, program, timeout, input_bytes=None, command='bash -s'):
    """Run a bash program over the saved SSH connection; the program travels on stdin."""
    servers = Servers(workspace)
    args = servers.ssh_args(server) + [command]
    deadline = time.monotonic() + timeout
    try:
        if input_bytes is None:
            # Text programs reuse the pooled connection; None means nothing was
            # sent there. Binary uploads stay on ssh: a remote side that exits
            # without reading a large stdin should report its own exit status,
            # not a lost connection.
            result = ssh_pool.run(servers, server, command, program, timeout)
            if result is None:
                remaining = math.ceil(deadline - time.monotonic())
                if remaining <= 0:
                    raise subprocess.TimeoutExpired(command, timeout)
                result = run_ssh(args, input=program, capture_output=True, text=True, encoding='utf-8',
                                 errors='replace', timeout=remaining)
        else:
            result = run_ssh(args, input=input_bytes, capture_output=True, timeout=timeout)
            result.stdout = result.stdout.decode('utf-8', 'replace')
            result.stderr = result.stderr.decode('utf-8', 'replace')
    except subprocess.TimeoutExpired:
        raise ssh_error('远程操作超时；结果未知。', servers.connection_details(server), timed_out=True) from None
    if result.returncode == 255:
        raise ssh_error(result.stderr, servers.connection_details(server))
    return result


def slurm_time(text):
    """Slurm time limit -> HH:MM:SS, or None when unlimited, unset or over 999 hours."""
    text = (text or '').strip()
    match = re.fullmatch(r'(?:(\d+)-)?(\d+)(?::(\d+))?(?::(\d+))?', text)
    if not match:
        return None
    days, first, second, third = match.groups()
    if days is None and third is None:
        # Plain minutes, or minutes:seconds.
        hours, minutes, seconds = 0, int(first), int(second or 0)
    elif days is not None and second is None:
        hours, minutes, seconds = int(first), 0, 0
    else:
        hours, minutes, seconds = int(first), int(second or 0), int(third or 0)
    total = int(days or 0) * 86400 + hours * 3600 + minutes * 60 + seconds
    if total <= 0 or total >= 1000 * 3600:
        return None
    return f'{total // 3600:02d}:{total % 3600 // 60:02d}:{total % 60:02d}'


def parse_probe(text):
    found = {'home': None, 'sbatch': None, 'network': None, 'complete': False, 'pythons': [],
             'python_modules': [], 'partitions': [], 'vasp_on_path': None, 'vasp_envs': [],
             'vasp_modules': [], 'potcar_libraries': []}
    partitions, configs, libraries = {}, {}, {}

    def absolute(value):
        return value if value.startswith('/') and not any(c in value for c in '\r\x00') else None

    for line in text.splitlines():
        tag, _, rest = line.partition('\t')
        fields = rest.split('\t')
        if tag == 'HOME':
            found['home'] = absolute(rest)
        elif tag == 'SBATCH':
            found['sbatch'] = absolute(rest)
        elif tag == 'NET':
            found['network'] = rest == 'yes'
        elif tag == 'END':
            found['complete'] = True
        elif tag == 'PY' and absolute(fields[0]):
            values = fields[1:7] + ['-'] * (7 - len(fields))
            version, pymatgen, numpy, matplotlib, machine, libc = values[:6]
            if not re.fullmatch(r'\d+\.\d+(\.\d+)?\S*', version or ''):
                continue  # the interpreter did not run; not a usable candidate
            row = {'path': fields[0], 'version': version, 'machine': machine, 'libc': libc}
            for name, value in zip(ENGINE_PACKAGES, (pymatgen, numpy, matplotlib)):
                row[name] = None if value in ('', '-') else value
            row['ready'] = all(row[name] for name in ENGINE_PACKAGES)
            if all(p['path'] != row['path'] for p in found['pythons']):
                found['pythons'].append(row)
        elif tag in ('PYMOD', 'VASPMOD') and re.fullmatch(r'[A-Za-z0-9_./+-]+', rest.strip()):
            key = 'python_modules' if tag == 'PYMOD' else 'vasp_modules'
            if rest.strip() not in found[key]:
                found[key].append(rest.strip())
        elif tag == 'PART':
            parts = rest.split('|')
            if len(parts) < 4:
                continue
            default = parts[0].endswith('*')
            name = parts[0].rstrip('*')
            if not re.fullmatch(r'[A-Za-z0-9_-]+', name):
                continue
            cores = int(re.match(r'\d+', parts[1]).group()) if re.match(r'\d+', parts[1]) else None
            row = partitions.setdefault(name, {'name': name, 'default': default, 'cores': cores,
                                               'max_time': slurm_time(parts[2]), 'state': parts[3]})
            # Mixed node types: one node must hold the whole job (nodes=1), so keep the smallest.
            if cores and (row['cores'] is None or cores < row['cores']):
                row['cores'] = cores
        elif tag == 'PCONF':
            pairs = dict(item.split('=', 1) for item in rest.split() if '=' in item)
            if pairs.get('PartitionName'):
                configs[pairs['PartitionName']] = pairs
        elif tag == 'VASPBIN':
            found['vasp_on_path'] = absolute(rest)
        elif tag == 'VASPENV' and absolute(fields[0]):
            binary = absolute(fields[1]) if len(fields) > 1 else None
            if binary and all(e['path'] != fields[0] for e in found['vasp_envs']):
                found['vasp_envs'].append({'path': fields[0], 'vasp_std': binary})
        elif tag == 'POTSI' and absolute(rest) and rest.endswith('/Si/POTCAR'):
            library = str(PurePosixPath(rest).parent.parent)
            libraries.setdefault(library, {'path': library, 'pbe_named': 'pbe' in library.lower()})
    for name, row in partitions.items():
        config = configs.get(name, {})
        row['default_time'] = slurm_time(config.get('DefaultTime'))
        if config.get('Default') == 'YES':
            row['default'] = True
        if row['max_time'] is None:
            row['max_time'] = slurm_time(config.get('MaxTime'))
        found['partitions'].append(row)
    found['potcar_libraries'] = sorted(libraries.values(), key=lambda r: (not r['pbe_named'], r['path']))
    found['pythons'].sort(key=lambda r: not r['ready'])
    found['partitions'].sort(key=lambda r: not r['default'])
    return found


def probe(workspace, server, current=None):
    result = run_remote(workspace, server, probe_program(current), PROBE_TIMEOUT)
    found = parse_probe(result.stdout)
    if not found['complete']:
        from vasp_agent.chat.ai_agent import scrub
        found['warning'] = '探测未完整结束，以下候选可能不全：' + scrub(result.stderr[-500:])
    return found


def validate_compute(cfg):
    """The rules RemoteCompute and the remote worker apply to a compute profile."""
    from vasp_agent.remote.remote_compute import validate_paths
    from vasp_agent.remote.remote_worker import validate_scheduler
    missing = [key for key in COMPUTE_KEYS if cfg.get(key) in (None, '')]
    if missing:
        raise ValueError('计算配置缺少：' + '、'.join(missing))
    validate_paths(cfg)
    root = PurePosixPath(cfg['root'])
    if '..' in root.parts or len(root.parts) < 4:
        raise ValueError('远程根目录需至少三级的绝对路径（如 /home/用户/vasp-agent），不能含 ..。')
    validate_scheduler(cfg)
    if type(cfg['ntasks']) is not int or not 1 <= cfg['ntasks'] <= 256:
        raise ValueError('核数须为 1–256 的整数。')
    return cfg


def compute_file(workspace, server):
    return Servers(workspace).file(server).with_name(server + '.compute.json')


def load_compute(workspace, server):
    path = compute_file(workspace, server)
    if not path.is_file():
        return None
    data = json.loads(path.read_text(encoding='utf-8'))
    return data if isinstance(data, dict) else None


def write_compute(workspace, server, cfg):
    """Write the profile; an existing one is first copied to a timestamped backup."""
    validate_compute(cfg)
    path = compute_file(workspace, server)
    backup = None
    if path.exists():
        stamp = f'{server}.compute.json.bak-{datetime.now():%Y%m%d-%H%M%S}'
        backup, counter = path.with_name(stamp), 1
        while backup.exists():
            backup, counter = path.with_name(f'{stamp}-{counter}'), counter + 1
        backup.write_bytes(path.read_bytes())
    temporary = path.with_name(path.name + '.tmp')
    temporary.write_text(json.dumps(cfg, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    os.replace(temporary, path)
    if os.name != 'nt':
        path.chmod(0o600)
    return {'written': str(path), 'backup': str(backup) if backup else None}


def env_script(module=None, binary_dir=None, origin='/setup-server'):
    """A VASP environment script for a module, or for vasp_std already on the login PATH."""
    header = f'# Generated by vasp-agent {origin} {datetime.now():%Y-%m-%d %H:%M}.\n'
    if module:
        if not re.fullmatch(r'[A-Za-z0-9_./+-]+', module):
            raise ValueError('模块名称无效。')
        return header + MODULE_INIT + 'module load ' + module + '\n'
    if not binary_dir or not binary_dir.startswith('/') or any(c in binary_dir for c in '\n\x00'):
        raise ValueError('vasp_std 所在目录无效。')
    return header + 'export PATH=' + shlex.quote(binary_dir) + ':"$PATH"\n'


def install_env_script(workspace, server, path, content):
    """Write one environment script under the remote root, then resolve vasp_std through it."""
    if not path.startswith('/') or any(c in path for c in '\n\x00'):
        raise ValueError('环境脚本路径无效。')
    program = ('set -e\nmkdir -p ' + shlex.quote(str(PurePosixPath(path).parent)) + '\n'
               'cat > ' + shlex.quote(path) + " <<'VASP_AGENT_ENV'\n" + content + 'VASP_AGENT_ENV\n'
               'bash -c \'source "$1" >/dev/null 2>&1 && command -v vasp_std\' vasp-agent-check ' + shlex.quote(path) + ' || true\n')
    result = run_remote(workspace, server, program, 60)
    if result.returncode:
        raise ValueError('环境脚本写入失败：' + result.stderr[-500:])
    binary = result.stdout.strip().splitlines()[-1] if result.stdout.strip() else ''
    return {'path': path, 'vasp_std': binary if binary.startswith('/') else None}


# ---- Cluster Python bootstrap: a venv under the remote root, never sudo ----

BOOTSTRAP_TIMEOUT = 1800
MIN_PYTHON = (3, 10)


def engine_requirements(path=None):
    """The engine's pins from requirements-tested.txt (pymatgen, numpy, matplotlib only)."""
    path = Path(path) if path else Path(__file__).resolve().parents[3] / 'requirements-tested.txt'
    pins = {}
    for line in path.read_text(encoding='utf-8').splitlines():
        match = re.fullmatch(r'\s*([A-Za-z0-9_.-]+)==([A-Za-z0-9_.+-]+)\s*(?:#.*)?', line)
        if match and match[1].lower() in ENGINE_PACKAGES:
            pins[match[1].lower()] = match[2]
    missing = [name for name in ENGINE_PACKAGES if name not in pins]
    if missing:
        raise ValueError(f'{path.name} 缺少固定版本：' + '、'.join(missing))
    return [f'{name}=={pins[name]}' for name in ENGINE_PACKAGES]


def python_version(text):
    match = re.match(r'(\d+)\.(\d+)', text or '')
    return (int(match[1]), int(match[2])) if match else None


def wheel_platforms(machine, libc):
    """manylinux tags a cluster accepts, newest first, from its glibc version."""
    if machine not in ('x86_64', 'aarch64'):
        raise ValueError(f'不支持为 {machine} 平台准备离线 wheel。')
    match = re.fullmatch(r'glibc-2\.(\d+)', libc or '')
    minor = int(match[1]) if match else 17
    if minor < 17:
        raise ValueError(f'集群 glibc 2.{minor} 过旧，现有 wheel 需要 2.17 以上。')
    return [f'manylinux_2_{n}_{machine}' for n in range(minor, 16, -1)] + [f'manylinux2014_{machine}']


def wheel_directory(root, version, machine):
    major, minor = python_version(version)
    return f'{root}/wheels/cp{major}{minor}-{machine}'


def download_wheels(dest, requirements, version, machine, libc, runner=subprocess.run):
    """pip download on this machine for the cluster's interpreter and platform."""
    import sys
    major, minor = python_version(version)
    command = [sys.executable, '-m', 'pip', 'download', '--disable-pip-version-check', '--only-binary=:all:',
               '--dest', str(dest), '--implementation', 'cp', '--python-version', f'{major}.{minor}',
               '--abi', f'cp{major}{minor}']
    for platform in wheel_platforms(machine, libc):
        command += ['--platform', platform]
    result = runner(command + list(requirements), capture_output=True, text=True, encoding='utf-8',
                    errors='replace', timeout=BOOTSTRAP_TIMEOUT)
    if result.returncode:
        raise ValueError('本机下载 wheel 失败（需本机联网；某些依赖可能没有该平台的 wheel）：\n' + result.stderr[-1500:])
    return sorted(p.name for p in Path(dest).iterdir() if p.suffix == '.whl')


def upload_wheels(workspace, server, local_dir, remote_dir):
    import io
    import tarfile
    buffer = io.BytesIO()
    names = sorted(p.name for p in Path(local_dir).iterdir() if p.suffix == '.whl')
    with tarfile.open(fileobj=buffer, mode='w:gz') as archive:
        for name in names:
            archive.add(Path(local_dir) / name, arcname=name)
    data = buffer.getvalue()
    command = f'mkdir -p {shlex.quote(remote_dir)} && tar -xzf - -C {shlex.quote(remote_dir)}'
    result = run_remote(workspace, server, None, 900, input_bytes=data, command=command)
    if result.returncode:
        raise ValueError('上传 wheel 失败：' + result.stderr[-1000:])
    return {'directory': remote_dir, 'files': names, 'bytes': len(data)}


def bootstrap_program(root, base_python, requirements, wheel_dir=None):
    venv = root + '/venv'
    for value in (root, base_python) + ((wheel_dir,) if wheel_dir else ()):
        absolute_check(value)
    source = ['--no-index', '--find-links', wheel_dir] if wheel_dir else []
    q = shlex.quote
    program = '\n'.join([
        'set -e',
        f'VENV={q(venv)}',
        'if [ ! -x "$VENV/bin/python" ]; then',
        f'  mkdir -p {q(root)}',
        f'  {q(base_python)} -m venv "$VENV"',
        "  echo '@@CREATED'",
        'fi',
        "echo '@@BEFORE'",
        '"$VENV/bin/python" -m pip freeze --disable-pip-version-check',
        "echo '@@INSTALL'",
        '"$VENV/bin/python" -m pip install --no-input --disable-pip-version-check --require-virtualenv '
        + ' '.join(q(x) for x in (*source, *requirements)) + ' 2>&1',
        "echo '@@AFTER'",
        '"$VENV/bin/python" -m pip freeze --disable-pip-version-check',
        "echo '@@IMPORT'",
        '"$VENV/bin/python" -c "import pymatgen.io.vasp, numpy, matplotlib; print(\'ok\')"',
        "echo '@@END'", ''])
    if re.search(r'\bsudo\b', program):
        raise ValueError('引导程序不得使用 sudo。')
    return program


def absolute_check(value):
    if not value.startswith('/') or any(c in value for c in '\n\x00'):
        raise ValueError('远程路径需为绝对路径。')


def parse_bootstrap(stdout):
    sections, current = {}, None
    for line in stdout.splitlines():
        if re.fullmatch(r'@@[A-Z]+', line.strip()):
            current = line.strip()[2:]
            sections.setdefault(current, [])
        elif current:
            sections[current].append(line)

    def freeze(lines):
        packages = {}
        for line in lines:
            match = re.fullmatch(r'([A-Za-z0-9_.-]+)==(\S+)', line.strip())
            if match:
                packages[match[1].lower().replace('_', '-')] = match[2]
        return packages
    before, after = freeze(sections.get('BEFORE', [])), freeze(sections.get('AFTER', []))
    return {'created': 'CREATED' in sections,
            'installed': {name: version for name, version in sorted(after.items()) if before.get(name) != version},
            'previous': {name: before[name] for name in sorted(after) if name in before and before[name] != after[name]},
            'import_ok': 'ok' in [x.strip() for x in sections.get('IMPORT', [])],
            'complete': 'END' in sections,
            'pip_tail': '\n'.join(sections.get('INSTALL', [])[-15:])}


def bootstrap(workspace, server, root, base_python, requirements, wheel_dir=None):
    result = run_remote(workspace, server, bootstrap_program(root, base_python, requirements, wheel_dir), BOOTSTRAP_TIMEOUT)
    report = parse_bootstrap(result.stdout)
    report['python'] = root + '/venv/bin/python'
    if result.returncode or not report['complete'] or not report['import_ok']:
        from vasp_agent.chat.ai_agent import scrub
        raise ValueError('虚拟环境安装未完成：\n' + scrub((report['pip_tail'] + '\n' + result.stderr)[-2000:]))
    return report
