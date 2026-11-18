"""Read-only software inventory of a cluster: VASP builds, their capabilities, companion tools.

Part of the stdin worker program (see remote_worker.WORKER_UNITS): standard library only.

Bounded by construction. Candidates come from `module avail`, the configured
environment script, the agent's own environments/ folder and a few operator-listed
roots, each listed at most two levels deep with a cap on entries; every external
command runs through `bounded` with its own timeout, and the whole probe has a
wall-clock budget. There is no recursive search: a `find` across the shared NFS
tree timed out on the first cluster. The only scripts sourced are a build's own
scripts/env.sh and environment scripts already used for VASP (as /setup-server and
inspect_environment do); nothing launches VASP or a job.

The result is evidence for the operator's registration menu, never a grant: the
capabilities are derived from compile flags and strings found in the files.
"""
import hashlib
import os
from pathlib import Path
import re
import subprocess
import time
from datetime import datetime, timezone

# 2: version from build metadata/path/module before the binary banner, and -D
# flags only from the compile-flag line (v1 misread help text as 5.2.2).
INVENTORY_VERSION = 2
COMMAND_TIMEOUT = 20
MODULE_TIMEOUT = 30
PROBE_BUDGET = 240
LIST_LIMIT = 80
MAX_BUILDS = 12
MAX_SOURCED = 12
MAX_MODULES = 12
MAX_ROOTS = 8
METADATA_LIMIT = 512_000
DEFAULT_ROOTS = ('~/apprepo',)
VASP_EXECUTABLES = ('vasp_std', 'vasp_gam', 'vasp_ncl')
# Where a build directory keeps its binaries (relative to the directory).
BIN_LAYOUTS = ('bin', 'app/bin', '')
METADATA_FILES = ('makefile.include', 'package_summary.json', 'app/package_summary.json',
                  'scripts/package_summary.json')
TOOLS = {'phonopy': ('phonopy',), 'wannier90': ('wannier90.x', 'postw90.x'), 'vaspkit': ('vaspkit',),
         'bader': ('bader',)}
NO_WANNIER = b'VASP was compiled without wannier90 library'
LAUNCH = re.compile(r'(mpirun|mpiexec|srun|sbatch|^[ \t]*vasp_(std|gam|ncl))', re.M)
# A -D option ends at whitespace, '=' or the end; '-Directed' inside prose does not
# count unless it sits in a run of options (FLAG_LINE).
FLAG = re.compile(rb'-D([A-Za-z_][A-Za-z0-9_]{1,40})(?=[\s=]|$)')
FLAG_VALUE = rb'(?:=(?:\\?"[^"\s]*\\?"|[^\s]*))?'
# The preprocessor line VASP embeds: at least three -D options separated by spaces.
FLAG_LINE = re.compile(rb'(?:-D[A-Za-z_][A-Za-z0-9_]*' + FLAG_VALUE + rb'[ \t]+){2,}-D[A-Za-z_][A-Za-z0-9_]*'
                       + FLAG_VALUE)
# The run banner, 'vasp.6.4.3 19Mar24' or 'vasp.5.4.4.18Apr17-...': version plus a
# build date. Help text such as 'equivalent to ... in vasp.5.2.2' has no date.
BINARY_VERSION = re.compile(rb'vasp\.(\d+\.\d+\.\d+)[ .]\d{2}[A-Z][a-z]{2}\d{2}')
FLAG_ANCHOR = re.compile(rb'^-D(HOST|MPI|vasp6)(=|$)')
META_VERSION = re.compile(r'"version"\s*:\s*"(\d+\.\d+\.\d+)"')
PATH_VERSION = re.compile(r'(?<![\d.])(\d+\.\d+\.\d+)(?![\d.])')
FLAG_CAPABILITIES = {'VASP2WANNIER90': ('vasp:wannier90',),
                     'VASP2WANNIER90v2': ('vasp:wannier90', 'vasp:wannier90v2'),
                     'VASP_HDF5': ('vasp:hdf5',), '_OPENMP': ('vasp:openmp',), 'USELIBXC': ('vasp:libxc',)}
# -Dsol_compat ships in stock VASP 6 makefiles; it is not evidence of the VASPsol
# patch, which is recognised only from build names/metadata (MARKER_CAPABILITIES).
# Words in the build's path, compile flags or build metadata that name a patch.
MARKER_CAPABILITIES = (('vtst', 'vasp:vtst'), ('vaspsol', 'vasp:vaspsol'), ('fixz', 'patch:fixz'),
                       ('ioptcell', 'patch:ioptcell'))
MODULE_INIT = '''if ! command -v module >/dev/null 2>&1; then
  for f in /etc/profile.d/modules.sh /etc/profile.d/lmod.sh /usr/share/lmod/lmod/init/bash /usr/share/Modules/init/bash; do
    if [ -f "$f" ]; then . "$f" >/dev/null 2>&1; break; fi
  done
fi
'''
MODULE_NAME = re.compile(r'[A-Za-z0-9_./+-]{1,120}')


class Probe:
    """One inventory run: the command runner, its log and the time budget."""

    def __init__(self, run=None, budget=PROBE_BUDGET):
        self.run = run or run_command
        self.deadline = time.monotonic() + budget
        self.commands = []
        self.truncated = []

    def bounded(self, argv, timeout=COMMAND_TIMEOUT):
        """Run one command with its own timeout; None when it timed out, failed to start or no budget is left."""
        left = self.deadline - time.monotonic()
        entry = {'command': display(argv), 'timeout': timeout}
        self.commands.append(entry)
        if left <= 1:
            entry['status'] = 'skipped_budget'
            return None
        try:
            result = self.run(argv, min(timeout, int(left)))
        except subprocess.TimeoutExpired:
            entry['status'] = 'timeout'
            return None
        except OSError as exc:
            entry['status'] = 'error: ' + type(exc).__name__
            return None
        entry['status'] = 'ok' if result.returncode == 0 else f'exit {result.returncode}'
        return result

    def out_of_time(self):
        return time.monotonic() >= self.deadline


def run_command(argv, timeout):
    return subprocess.run(argv, capture_output=True, text=True, errors='replace', timeout=timeout)


def display(argv):
    if argv[:2] == ['bash', '-c']:
        return 'bash -c ' + ' '.join(line.strip() for line in argv[2].splitlines() if line.strip())[-300:] + (
            ' ' + ' '.join(argv[4:]) if len(argv) > 4 else '')
    return ' '.join(argv)[:300]


def listdir(path, probe, limit=LIST_LIMIT):
    """Names in one directory, capped; never recursive."""
    try:
        with os.scandir(path) as entries:
            names = []
            for entry in entries:
                if len(names) >= limit:
                    probe.truncated.append(str(path))
                    break
                names.append(entry.name)
    except OSError:
        return []
    return sorted(names)


def is_executable(path):
    try:
        return path.is_file() and os.access(path, os.X_OK)
    except OSError:
        return False


def expand_root(root):
    text = str(root)
    path = Path(os.path.expanduser(text))
    if not path.is_absolute() or '..' in path.parts or any(c in text for c in '\n\x00'):
        raise ValueError('探测根目录须为绝对路径或 ~/ 开头，且不含 ..。')
    if len(path.parts) < 3:
        raise ValueError('探测根目录过宽（至少两级，如 ~/apprepo）；探测只列目录，不做全盘搜索。')
    return path


def build_directory(binary):
    """The build a binary belongs to: <build>/bin/vasp_std or <build>/app/bin/vasp_std."""
    folder = binary.parent
    if folder.name == 'bin':
        folder = folder.parent
        if folder.name == 'app':
            folder = folder.parent
    return folder


def scan_binary(path):
    """One streaming pass: sha256, -D flags, version strings, Wannier markers."""
    digest, flags, versions = hashlib.sha256(), set(), set()
    absent = symbols = False
    tail = b''
    with open(path, 'rb') as stream:
        for block in iter(lambda: stream.read(1 << 22), b''):
            digest.update(block)
            window = tail + block
            for line in FLAG_LINE.findall(window):
                # VASP's preprocessor line always names one of these; a run of
                # stray '-Dxxx' fragments in prose does not.
                if any(FLAG_ANCHOR.search(token) for token in line.split()):
                    flags.update(m.decode('ascii') for m in FLAG.findall(line))
            versions.update(m.decode('ascii') for m in BINARY_VERSION.findall(window))
            absent = absent or NO_WANNIER in window
            symbols = symbols or b'wannier_setup' in window or b'w90_' in window
            tail = window[-4096:]
    return {'sha256': digest.hexdigest(), 'flags': flags, 'versions': versions,
            'wannier90_absent_marker': absent, 'wannier90_symbols': symbols}


def read_metadata(folder):
    """Build metadata next to the binaries (bounded reads): flags and patch words."""
    found, text = [], ''
    for name in METADATA_FILES:
        path = folder / name
        try:
            if path.is_file() and path.stat().st_size <= METADATA_LIMIT:
                text += '\n' + path.read_text(encoding='utf-8', errors='replace')
                found.append(name)
        except OSError:
            continue
    return found, text


def choose_version(banner_versions, metadata_text, folder, modules, flags):
    """Most specific trustworthy source first; -Dvasp6 vetoes a contradicting major."""
    candidates = []
    match = META_VERSION.search(metadata_text)
    if match:
        candidates.append((match.group(1), 'metadata'))
    match = PATH_VERSION.search(str(folder))
    if match:
        candidates.append((match.group(1), 'path'))
    for name in modules:
        match = PATH_VERSION.search(name)
        if match:
            candidates.append((match.group(1), 'module'))
            break
    candidates += [(v, 'binary') for v in sorted(banner_versions, reverse=True)]
    six = 'vasp6' in flags
    for version, source in candidates:
        if six and version.split('.')[0] != '6':
            continue
        return version, source
    return ('6', 'flag') if six else (None, None)


def capabilities(version, flags, words, absent_marker):
    """Capability tags from the version, the -D flags and patch words; the marker wins over flags."""
    tags = set()
    if version:
        tags.add('vasp:' + version.split('.')[0])
    for flag in flags:
        tags.update(FLAG_CAPABILITIES.get(flag, ()))
    lowered = words.lower()
    for word, tag in MARKER_CAPABILITIES:
        if word in lowered:
            tags.add(tag)
    if absent_marker:
        tags -= {'vasp:wannier90', 'vasp:wannier90v2'}
    return sorted(tags)


def resolve_vasp(script, probe):
    """vasp_std as the script puts it on PATH, without running VASP."""
    result = probe.bounded(['bash', '-c', 'source "$1" >/dev/null 2>&1 && command -v vasp_std',
                            'vasp-inventory', str(script)])
    line = result.stdout.strip().splitlines()[-1] if result is not None and result.stdout.strip() else ''
    return Path(line) if line and os.path.isabs(line) else None


def inspect_script(script, binary, probe):
    """A build's own env script is reused only if it sets an environment and loads this binary."""
    row = {'path': str(script), 'sha256': None, 'passes': False}
    try:
        if not script.is_file() or script.stat().st_size > 65536:
            return {**row, 'reason': '不是普通文件或超过 64 KB'}
        data = script.read_bytes()
    except OSError as exc:
        return {**row, 'reason': type(exc).__name__}
    row['sha256'] = hashlib.sha256(data).hexdigest()
    if LAUNCH.search(data.decode('utf-8', errors='replace')):
        return {**row, 'reason': '脚本含启动命令（mpirun/srun/sbatch/vasp_*），不作为环境脚本'}
    resolved = resolve_vasp(script, probe)
    if resolved is None:
        return {**row, 'reason': '加载后找不到 vasp_std'}
    try:
        same = resolved.resolve() == binary.resolve()
    except OSError:
        same = False
    if not same:
        return {**row, 'reason': f'加载后的 vasp_std 是 {resolved}，不是本构建'}
    return {**row, 'passes': True, 'reason': '加载后解析到本构建的 vasp_std'}


def module_avail(probe):
    result = probe.bounded(['bash', '-c', MODULE_INIT + 'module -t avail 2>&1'], MODULE_TIMEOUT)
    if result is None:
        return [], None
    names = []
    for line in (result.stdout + '\n' + (result.stderr or '')).splitlines():
        line = line.strip()
        if not line or line.endswith(':') or not MODULE_NAME.fullmatch(line):
            continue
        if line not in names:
            names.append(line)
    return names, result


def module_paths(name, probe):
    """PATH entries a module prepends, from `module show` (Tcl and Lmod forms)."""
    result = probe.bounded(['bash', '-c', MODULE_INIT + 'module show "$1" 2>&1', 'vasp-inventory', name])
    if result is None:
        return []
    text = result.stdout + '\n' + (result.stderr or '')
    paths = []
    for match in re.findall(r'prepend[-_]path\s*\(?\s*"?PATH"?\s*,?\s*"?([^"\s)]+)', text):
        paths += [p for p in match.split(os.pathsep) if os.path.isabs(p)]
    return list(dict.fromkeys(paths))


def tool_version(name, executable, probe):
    if name == 'phonopy':
        result = probe.bounded([str(executable), '--version'])
        if result is not None and result.returncode == 0 and result.stdout.strip():
            return result.stdout.strip().splitlines()[0][:40], 'phonopy --version'
    match = PATH_VERSION.search(str(executable))
    return (match.group(1), 'path') if match else (None, None)


def software_inventory(vasp_env, agent_root, roots=DEFAULT_ROOTS, run=None, budget=PROBE_BUDGET):
    """Probe VASP builds and companion tools; read-only and bounded (see the module doc)."""
    probe = Probe(run, budget)
    roots = [expand_root(root) for root in list(roots)[:MAX_ROOTS]]
    builds = {}           # resolved vasp_std -> build record
    found_by = {}         # resolved vasp_std -> how it was found
    scripts = {}          # resolved vasp_std -> environment scripts seen loading it
    modules_for = {}      # resolved vasp_std -> module names putting it on PATH

    def add_binary(binary, how):
        try:
            binary = binary.resolve()
        except OSError:
            return None
        if not is_executable(binary):
            return None
        if binary not in found_by and len(found_by) >= MAX_BUILDS:
            probe.truncated.append('vasp_builds')
            return None
        found_by.setdefault(binary, [])
        if how not in found_by[binary]:
            found_by[binary].append(how)
        return binary

    # 1. Environment scripts already used for VASP: the configured one and the agent's own.
    sourced = []
    if vasp_env:
        sourced.append((Path(vasp_env), 'configured'))
    environments = Path(agent_root) / 'environments'
    for name in listdir(environments, probe)[:MAX_SOURCED]:
        if name.endswith('.sh') and str(environments / name) != str(vasp_env):
            sourced.append((environments / name, 'agent_script'))
    for script, kind in sourced:
        resolved = resolve_vasp(script, probe) if script.is_file() else None
        binary = add_binary(resolved, kind) if resolved else None
        if binary:
            scripts.setdefault(binary, []).append((script, kind))

    # 2. Modules: names from `module avail`, binaries from `module show`.
    module_names, _ = module_avail(probe)
    vasp_modules = [n for n in module_names if 'vasp' in n.lower() and 'vaspkit' not in n.lower()]
    tool_modules = [n for n in module_names if any(t in n.lower() for t in ('vaspkit', 'phonopy', 'wannier', 'bader'))]
    for name in vasp_modules[:MAX_MODULES]:
        for folder in module_paths(name, probe):
            binary = add_binary(Path(folder) / 'vasp_std', 'module:' + name) if (Path(folder) / 'vasp_std').exists() else None
            if binary:
                modules_for.setdefault(binary, []).append(name)

    # 3. Operator-listed roots: <root>, <root>/*, <root>/vasp/* at known layouts only.
    tool_dirs = []
    for root in roots:
        candidates = [root]
        for child in listdir(root, probe):
            candidates.append(root / child)
            if any(child.lower().startswith(t) for t in TOOLS):
                tool_dirs.append(root / child)
        for nest in ('vasp', 'VASP'):
            candidates += [root / nest / child for child in listdir(root / nest, probe)]
        for folder in candidates:
            if probe.out_of_time():
                break
            for layout in BIN_LAYOUTS:
                binary = folder / layout / 'vasp_std' if layout else folder / 'vasp_std'
                if binary.exists():
                    add_binary(binary, 'root:' + str(root))
                    break

    # 4. Each build: one pass over vasp_std, the siblings, metadata, env scripts.
    for binary, how in found_by.items():
        if probe.out_of_time():
            probe.truncated.append('build:' + str(binary))
            continue
        folder = build_directory(binary)
        try:
            scanned = scan_binary(binary)
        except OSError as exc:
            builds[binary] = {'executable': str(binary), 'error': type(exc).__name__, 'found_by': how}
            continue
        metadata, text = read_metadata(folder)
        flags = set(scanned['flags']) | {m.decode('ascii') for m in FLAG.findall(text.encode('utf-8', 'replace'))}
        version, version_source = choose_version(scanned['versions'], text, folder, modules_for.get(binary, []), flags)
        words = ' '.join([str(folder), str(binary), ' '.join(sorted(flags)), text])
        env_scripts = []
        for script, kind in scripts.get(binary, []):
            try:
                digest = hashlib.sha256(script.read_bytes()).hexdigest()
            except OSError:
                digest = None
            env_scripts.append({'kind': kind, 'path': str(script), 'sha256': digest, 'passes': digest is not None,
                                'reason': '已在使用的环境脚本，加载后解析到本构建的 vasp_std'})
        own = folder / 'scripts' / 'env.sh'
        if own.exists() and all(row['path'] != str(own) for row in env_scripts):
            env_scripts.append({'kind': 'build_script', **inspect_script(own, binary, probe)})
        builds[binary] = {
            'kind': 'vasp', 'build_dir': str(folder), 'version': version, 'version_source': version_source,
            'executable': str(binary), 'executable_sha256': scanned['sha256'],
            'executables': {name: str(binary.parent / name) if is_executable(binary.parent / name) else None
                            for name in VASP_EXECUTABLES},
            'flags': sorted(flags), 'metadata_files': metadata,
            'wannier90_absent_marker': scanned['wannier90_absent_marker'],
            'wannier90_symbols': scanned['wannier90_symbols'],
            'capabilities': capabilities(version, flags, words, scanned['wannier90_absent_marker']),
            'env_scripts': env_scripts, 'modules': modules_for.get(binary, []), 'found_by': how}

    # 5. Companion tools: on PATH, and under the roots at <dir>/bin, <dir>, <dir>/*/bin.
    tools = []
    names = [exe for group in TOOLS.values() for exe in group]
    result = probe.bounded(['bash', '-c', 'for n in "$@"; do printf "%s\\t%s\\n" "$n" "$(command -v "$n")"; done',
                            'vasp-inventory', *names])
    on_path = {}
    for line in (result.stdout.splitlines() if result is not None else []):
        name, _, where = line.partition('\t')
        if where and os.path.isabs(where):
            on_path[name] = Path(where)
    seen = set()
    for tool, executables in TOOLS.items():
        places = [(on_path[exe], 'PATH') for exe in executables if exe in on_path]
        for folder in tool_dirs:
            if not folder.name.lower().startswith(tool):
                continue
            nested = [folder / child / 'bin' for child in listdir(folder, probe, 20)]
            for exe in executables:
                for candidate in [folder / 'bin' / exe, folder / exe] + [n / exe for n in nested]:
                    if is_executable(candidate):
                        places.append((candidate, 'root:' + str(folder.parent)))
                        break
        for executable, how in places:
            if str(executable) in seen:
                continue
            seen.add(str(executable))
            version, source = tool_version(tool, executable, probe)
            tools.append({'tool': tool, 'executable': str(executable), 'version': version,
                          'version_source': source, 'found_by': how})

    return {'probe_version': INVENTORY_VERSION,
            'scanned_at': datetime.now(timezone.utc).isoformat(timespec='seconds'),
            'roots': [str(root) for root in roots], 'vasp_env': vasp_env,
            'vasp_builds': list(builds.values()), 'tools': tools,
            'modules': {'vasp': vasp_modules, 'tools': tool_modules},
            'commands': probe.commands, 'truncated': probe.truncated,
            'bounds': {'command_timeout': COMMAND_TIMEOUT, 'module_timeout': MODULE_TIMEOUT, 'budget': budget,
                       'list_limit': LIST_LIMIT, 'depth': 2, 'recursive_search': False},
            'content_is_untrusted': True}
