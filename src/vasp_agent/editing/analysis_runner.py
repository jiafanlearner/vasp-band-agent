"""Draft analysis with separate Docker isolation and operator-approved host execution."""
import ast
import hashlib
import inspect
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import tempfile
import time
import sys
import signal
from uuid import uuid4

from .workspace_editor import WorkspaceEditor, decode, safe_value
from .edit_store import atomic_write, guarded
from . import analysis_outputs
from vasp_agent.workflow.vasp_band_agent import ToolError
from vasp_agent.common import agent_paths

IMAGE = 'python:3.12-slim'
SCIENTIFIC_IMAGE = 'vasp-agent-sci:latest'
SCIENTIFIC_IMAGE_ENV = 'VASP_AGENT_SCIENTIFIC_IMAGE'
SANDBOX_CONFIG_REL = 'state/analysis-sandbox.json'
IMAGE_PROFILES = ('default', 'scientific')
IMAGE_ID_RE = re.compile(r'^sha256:[a-f0-9]{64}$')
IMAGE_REF_RE = re.compile(
    r'^(?:(?:localhost|(?:[A-Za-z0-9](?:[A-Za-z0-9-]*[A-Za-z0-9])?\.)+[A-Za-z0-9](?:[A-Za-z0-9-]*[A-Za-z0-9])?)'
    r'(?::[0-9]{1,5})?/)?'
    r'[a-z0-9]+(?:(?:[._]|__|[-]+)[a-z0-9]+)*(?:/[a-z0-9]+(?:(?:[._]|__|[-]+)[a-z0-9]+)*)*'
    r'(?::[A-Za-z0-9_][A-Za-z0-9._-]{0,127})?'
    r'(?:@sha256:[a-f0-9]{64})?$'
)
DEFAULT_CAPS = {'memory': '256m', 'tmpfs_size': '16m', 'fsize_bytes': 1024 * 1024}
ELEVATED_CAPS = {'memory': '512m', 'tmpfs_size': '32m', 'fsize_bytes': 2 * 1024 * 1024}
MAX_SANDBOX_CONFIG_BYTES = 4096
CONTAINER_ENV = (
    'HOME=/tmp',
    'XDG_CACHE_HOME=/tmp/.cache',
    'MPLCONFIGDIR=/tmp/mpl',
    'MPLBACKEND=Agg',
    'OPENBLAS_NUM_THREADS=1',
    'OMP_NUM_THREADS=1',
    'MKL_NUM_THREADS=1',
    'NUMEXPR_NUM_THREADS=1',
)


def validate_image_ref(ref):
    """Validate an operator-configured Docker image reference (never model-supplied)."""
    if not isinstance(ref, str) or not ref or len(ref) > 255 or ref.startswith('-'):
        raise ValueError('镜像名称格式无效。')
    if any(ch.isspace() or ord(ch) < 32 for ch in ref) or '..' in ref or '//' in ref:
        raise ValueError('镜像名称格式无效。')
    if not IMAGE_REF_RE.fullmatch(ref):
        raise ValueError('镜像名称格式无效。')
    return ref


def resolve_scientific_image(workspace=None):
    """Resolve the scientific image ref from operator env / local controlled config / default."""
    env_ref = os.environ.get(SCIENTIFIC_IMAGE_ENV)
    if env_ref is not None and env_ref != '':
        try:
            return validate_image_ref(env_ref), 'env', None
        except ValueError:
            return None, 'env', f'环境变量 {SCIENTIFIC_IMAGE_ENV} 的镜像名称格式无效；已拒绝使用。'
    if workspace is not None:
        try:
            cfg_path = guarded(agent_paths.agent_path(workspace, SANDBOX_CONFIG_REL))
        except (ToolError, ValueError) as exc:
            return None, 'config', safe_value(str(exc))
        if cfg_path.exists():
            if not cfg_path.is_file():
                return None, 'config', '本地分析沙箱配置路径须为普通文件；已拒绝使用科学镜像。'
            try:
                if cfg_path.stat().st_size > MAX_SANDBOX_CONFIG_BYTES:
                    return None, 'config', '本地分析沙箱配置文件过大；已拒绝使用科学镜像。'
                data = json.loads(cfg_path.read_text(encoding='utf-8'))
            except (OSError, ValueError):
                return None, 'config', '本地分析沙箱配置文件无法解析；已拒绝使用科学镜像。'
            if not isinstance(data, dict):
                return None, 'config', '本地分析沙箱配置格式无效；已拒绝使用科学镜像。'
            if not set(data) <= {'scientific_image'}:
                return None, 'config', '本地分析沙箱配置包含未知字段；已拒绝使用科学镜像。'
            if 'scientific_image' in data and data['scientific_image'] is not None:
                try:
                    return validate_image_ref(data['scientific_image']), 'config', None
                except ValueError:
                    return None, 'config', '本地分析沙箱配置中的 scientific_image 格式无效；已拒绝使用。'
    return SCIENTIFIC_IMAGE, 'default', None


def _inspect_image(docker, image_ref, missing_reason):
    try:
        result = subprocess.run([docker, 'image', 'inspect', image_ref, '--format', '{{.Id}}'],
                                capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.TimeoutExpired):
        return {'available': False, 'image': image_ref, 'image_id': None, 'reason': missing_reason}
    image_id = result.stdout.strip() if result.returncode == 0 else ''
    if not IMAGE_ID_RE.fullmatch(image_id):
        return {'available': False, 'image': image_ref, 'image_id': None, 'reason': missing_reason}
    return {'available': True, 'image': image_ref, 'image_id': image_id}


def probe(workspace=None):
    sci_image, sci_source, sci_error = resolve_scientific_image(workspace)
    docker = shutil.which('docker')
    if not docker:
        reason = '未安装 Docker；不会在宿主机执行脚本。'
        return {
            'available': False,
            'reason': reason,
            'network': False,
            'profiles': {
                'default': {'available': False, 'image': IMAGE, 'image_id': None, 'reason': reason},
                'scientific': {'available': False, 'image': sci_image, 'image_id': None,
                               'source': sci_source, 'reason': sci_error or reason},
            },
        }
    try:
        def query(args):
            result = subprocess.run([docker] + args, capture_output=True, text=True, timeout=10)
            if result.returncode:
                raise ValueError('Docker 检查失败')
            return result.stdout.strip()
        endpoint = json.loads(query(['context', 'inspect']))[0]['Endpoints']['docker']['Host']
        # Environment overrides must not route snapshots to a remote daemon.
        if any(os.environ.get(k) for k in ('DOCKER_HOST', 'DOCKER_CONTEXT', 'DOCKER_TLS_VERIFY')):
            raise ValueError('请先清除 Docker 连接覆盖环境变量，仅支持已选本地上下文。')
        if not endpoint.startswith(('unix:///', 'npipe:////./pipe/')):
            raise ValueError('仅支持本地 Docker daemon。')
        if query(['info', '--format', '{{.OSType}}']) != 'linux':
            raise ValueError('需要 Linux 容器模式。')
    except (OSError, ValueError, KeyError, IndexError, subprocess.TimeoutExpired) as exc:
        reason = safe_value(str(exc))
        return {
            'available': False,
            'reason': reason,
            'network': False,
            'profiles': {
                'default': {'available': False, 'image': IMAGE, 'image_id': None, 'reason': reason},
                'scientific': {'available': False, 'image': sci_image, 'image_id': None,
                               'source': sci_source, 'reason': sci_error or reason},
            },
        }
    default_entry = _inspect_image(docker, IMAGE, f'需预先安装 {IMAGE} 镜像；不会自动拉取。')
    if sci_error is not None:
        sci_entry = {'available': False, 'image': None, 'image_id': None, 'source': sci_source, 'reason': sci_error}
    else:
        sci_entry = {**_inspect_image(docker, sci_image, f'需预先安装科学镜像 {sci_image}；不会自动拉取。'),
                     'source': sci_source}
    any_available = default_entry['available'] or sci_entry['available']
    result = {
        'available': any_available,
        'docker': docker,
        'image_id': default_entry['image_id'],
        'network': False,
        'profiles': {'default': default_entry, 'scientific': sci_entry},
    }
    if not any_available:
        result['reason'] = default_entry['reason']
    return result


def _call_probe(workspace):
    params = inspect.signature(probe).parameters
    return probe(workspace) if params else probe()


def _select_profile_runtime(runtime, image_profile):
    """Resolve the requested profile from a probe() result without silent fallback."""
    if not isinstance(runtime, dict):
        return {'available': False, 'image_profile': image_profile, 'image_id': None, 'reason': '沙箱探测结果无效。'}
    safe_base = {k: v for k, v in runtime.items() if k != 'docker'}
    profiles = runtime.get('profiles')
    if isinstance(profiles, dict):
        entry = profiles.get(image_profile)
        if (runtime.get('available') and isinstance(entry, dict) and entry.get('available')
                and IMAGE_ID_RE.fullmatch(str(entry.get('image_id') or ''))):
            return {**runtime, 'available': True, 'image_profile': image_profile,
                    'image': entry.get('image'), 'image_id': entry['image_id']}
        reason = (entry.get('reason') if isinstance(entry, dict) else None) or runtime.get('reason') or '目标分析镜像不可用；不会自动拉取。'
        return {**safe_base, 'available': False, 'image_profile': image_profile,
                'image': entry.get('image') if isinstance(entry, dict) else None,
                'image_id': None, 'reason': reason}
    # Backward compatibility for legacy test monkeypatches returning flat {'available', 'image_id'}.
    if image_profile == 'default':
        if runtime.get('available') and IMAGE_ID_RE.fullmatch(str(runtime.get('image_id') or '')):
            return {**runtime, 'image_profile': 'default', 'image': runtime.get('image', IMAGE)}
        return {**safe_base, 'available': False, 'image_profile': 'default', 'image': IMAGE, 'image_id': None,
                'reason': runtime.get('reason') or f'需预先安装 {IMAGE} 镜像；不会自动拉取。'}
    return {**safe_base, 'available': False, 'image_profile': image_profile, 'image': None, 'image_id': None,
            'reason': f'未检测到本地预装的 {image_profile} 分析镜像；不会自动拉取。'}


def resource_caps(image_profile='default', export_outputs=False):
    if not isinstance(image_profile, str) or image_profile not in IMAGE_PROFILES:
        raise ToolError('ANALYSIS_ARGUMENT', "镜像档位 image_profile 仅支持 'default' 或 'scientific'。")
    if not isinstance(export_outputs, bool):
        raise ToolError('ANALYSIS_ARGUMENT', 'export_outputs 必须为布尔值。')
    return ELEVATED_CAPS if (image_profile == 'scientific' or export_outputs) else DEFAULT_CAPS


def container_command(runtime, name, snapshot, *, image_profile=None, export_outputs=None, output_dir=None):
    profile = runtime.get('image_profile', 'default') if image_profile is None else image_profile
    export = runtime.get('export_outputs', False) if export_outputs is None else export_outputs
    caps = resource_caps(profile, export)
    image_id = runtime.get('image_id')
    if not isinstance(image_id, str) or not IMAGE_ID_RE.fullmatch(image_id):
        raise ToolError('ANALYSIS_ARGUMENT', '容器镜像 ID 无效，须为已校验的 sha256 摘要。')
    resolved_output = runtime.get('output_dir') if output_dir is None else output_dir
    if resolved_output is not None and not export:
        raise ToolError('ANALYSIS_ARGUMENT', '未启用 export_outputs 时禁止挂载 /output。')
    fsize = f"{caps['fsize_bytes']}:{caps['fsize_bytes']}"
    env_entries = list(CONTAINER_ENV)
    snapshot_text = str(snapshot)
    if any(ch in ',\"' or ord(ch) < 32 for ch in snapshot_text):
        raise ToolError('ANALYSIS_ARGUMENT', '分析快照挂载路径包含非法字符。')
    mounts = ['--mount', 'type=bind,source=' + snapshot_text + ',target=/analysis,readonly']
    if resolved_output is not None:
        output_text = str(resolved_output)
        if any(ch in ',\"' or ord(ch) < 32 for ch in output_text):
            raise ToolError('ANALYSIS_ARGUMENT', '导出挂载路径包含非法字符。')
        env_entries.append('ANALYSIS_OUTPUT_DIR=/output')
        mounts += ['--mount', 'type=bind,source=' + output_text + ',target=/output']
    env_args = [item for pair in (('-e', entry) for entry in env_entries) for item in pair]
    return [runtime['docker'], 'run', '--name', name, '--rm', '--pull=never', '--network=none',
            '--read-only', '--cap-drop=ALL', '--security-opt=no-new-privileges', '--user=65534:65534',
            '--memory=' + caps['memory'], '--memory-swap=' + caps['memory'], '--cpus=1', '--pids-limit=32',
            '--log-driver=none', '--ulimit', 'fsize=' + fsize,
            '--tmpfs', f"/tmp:rw,noexec,nosuid,size={caps['tmpfs_size']}",
            *env_args, *mounts,
            '--workdir=/analysis', image_id, 'python', '-I', '-B', '/analysis/main.py']


def execute(runtime, snapshot, seconds):
    name = 'vasp-analysis-' + uuid4().hex
    process = None
    reason = None
    cleanup_error = False
    with tempfile.TemporaryFile() as output:
        try:
            process = subprocess.Popen(container_command(runtime, name, snapshot), stdout=output, stderr=subprocess.STDOUT)
            deadline = time.monotonic() + seconds
            while process.poll() is None:
                if time.monotonic() >= deadline or os.fstat(output.fileno()).st_size > 32000:
                    reason = 'timeout' if time.monotonic() >= deadline else 'output_limit'
                    break
                time.sleep(0.05)
        finally:
            # Always target this invocation's unique container, including Ctrl+C.
            try:
                cleanup = subprocess.run([runtime['docker'], 'rm', '-f', name], capture_output=True, timeout=10)
                cleanup_error = cleanup.returncode != 0 and bool(process and process.poll() is None)
            except (OSError, subprocess.TimeoutExpired):
                cleanup_error = True
            if process is not None and process.poll() is None:
                process.kill()
                process.wait(timeout=5)
        size = os.fstat(output.fileno()).st_size
        output.seek(0)
        text = output.read(12000).decode('utf-8', errors='replace')
    from vasp_agent.remote.remote_worker import diagnostic_text
    return {'container_name': name, 'exit_code': process.returncode, 'stop_reason': reason,
            'output': safe_value(diagnostic_text(text)), 'output_truncated': size > 12000,
            'cleanup_failed': cleanup_error,
            'status': 'cleanup_failed' if cleanup_error else reason or ('completed' if process.returncode == 0 else 'failed')}


def execute_local(snapshot, seconds):
    """Trusted host execution, explicitly NOT a sandbox."""
    process = None
    stop = None
    cleanup_failed = False
    with tempfile.TemporaryFile() as output:
        try:
            kwargs = {'start_new_session': True} if os.name != 'nt' else {'creationflags': subprocess.CREATE_NEW_PROCESS_GROUP}
            process = subprocess.Popen([sys.executable, '-I', '-B', str(snapshot / 'main.py')],
                cwd=snapshot, stdin=subprocess.DEVNULL, stdout=output, stderr=subprocess.STDOUT, **kwargs)
            deadline = time.monotonic() + seconds
            while process.poll() is None:
                if time.monotonic() >= deadline or os.fstat(output.fileno()).st_size > 32000:
                    stop = 'timeout' if time.monotonic() >= deadline else 'output_limit'
                    break
                time.sleep(0.05)
        finally:
            if process is not None:
                try:
                    if os.name != 'nt':
                        os.killpg(process.pid, signal.SIGKILL)
                    elif process.poll() is None:
                        subprocess.run(['taskkill', '/PID', str(process.pid), '/T', '/F'],
                                       capture_output=True, timeout=10, check=True)
                except ProcessLookupError:
                    pass
                except (OSError, subprocess.SubprocessError):
                    cleanup_failed = True
                if process.poll() is None:
                    process.kill()
                process.wait(timeout=5)
        size = os.fstat(output.fileno()).st_size
        output.seek(0)
        text = output.read(12000).decode('utf-8', errors='replace')
    from vasp_agent.remote.remote_worker import diagnostic_text
    return {'status': stop or ('completed' if process.returncode == 0 else 'failed'),
            'exit_code': process.returncode, 'output': safe_value(diagnostic_text(text)),
            'output_truncated': size > 12000, 'cleanup_failed': cleanup_failed,
            'isolation': 'none', 'note': '本机账户权限执行，非沙箱；不能保证阻止外部副作用或清理逃逸子进程。'}


class AnalysisRunner:
    def __init__(self, workspace):
        self.workspace = Path(workspace).resolve()
        self.editor = WorkspaceEditor(workspace)

    def snapshot(self, script, expected_sha256, inputs):
        if not script.lower().endswith('.py') or len(inputs) > 10:
            raise ToolError('ANALYSIS_ARGUMENT', '仅支持 Python 脚本及最多10个显式输入文件。')
        data = self.editor.read_data(script)
        digest = hashlib.sha256(data).hexdigest()
        if digest != expected_sha256:
            raise ToolError('EDIT_CONFLICT', '脚本已改变，请重新读取并验证。')
        try:
            ast.parse(decode(data))
        except SyntaxError as exc:
            raise ToolError('ANALYSIS_SYNTAX', f'Python语法错误，第{exc.lineno}行；未执行。') from None
        files = {'main.py': data}
        manifest = [{'path': script, 'sandbox_path': 'main.py', 'sha256': digest}]
        for index, item in enumerate(inputs):
            raw = self.editor.read_data(item['path'])
            digest = hashlib.sha256(raw).hexdigest()
            if digest != item['expected_sha256']:
                raise ToolError('EDIT_CONFLICT', '输入文件已改变，请重新读取。')
            name = f'inputs/{index}/' + self.editor.path(item['path']).name
            files[name] = raw
            manifest.append({'path': item['path'], 'sandbox_path': name, 'sha256': digest})
        if sum(map(len, files.values())) > 4 * 1024 * 1024:
            raise ToolError('ANALYSIS_SIZE', '分析快照总量不得超过4 MiB。')
        return files, manifest

    def validate(self, script, expected_sha256, inputs):
        _, manifest = self.snapshot(script, expected_sha256, inputs)
        return {'syntax_valid': True, 'executed': False, 'manifest': manifest,
                'note': '只验证语法及输入边界，不证明算法、科学结论或运行行为正确。'}

    def get(self, run_id):
        if not re.fullmatch('[a-f0-9]{32}', run_id):
            raise ValueError('运行ID无效。')
        path = guarded(agent_paths.store_item(self.workspace, 'analysis_runs', run_id + '.json'))
        return safe_value(json.loads(path.read_text(encoding='utf-8')))

    def run(self, script, expected_sha256, inputs, timeout_seconds=30, request_id=None, *,
            backend="docker", approve=None, image_profile="default", export_outputs=False):
        if type(timeout_seconds) is not int or not 1 <= timeout_seconds <= 60:
            raise ValueError('运行时间须为1至60秒。')
        if backend not in ('docker', 'local'):
            raise ValueError('运行模式无效。')
        if not isinstance(image_profile, str) or image_profile not in IMAGE_PROFILES:
            raise ToolError('ANALYSIS_ARGUMENT', "镜像档位 image_profile 仅支持 'default' 或 'scientific'，不允许指定自定义镜像名。")
        if not isinstance(export_outputs, bool):
            raise ToolError('ANALYSIS_ARGUMENT', 'export_outputs 必须为布尔值。')
        if backend == 'local' and image_profile != 'default':
            raise ToolError('ANALYSIS_ARGUMENT', '本机运行模式不支持指定容器镜像档位。')
        if backend == 'local' and export_outputs:
            raise ToolError('ANALYSIS_ARGUMENT', '受控导出仅支持 Docker 沙箱模式。')
        extras = []
        if backend == 'local':
            extras.append('local')
        if image_profile != 'default':
            extras.append(('image_profile', image_profile))
        if export_outputs:
            extras.append(('export_outputs', True))
        signature_data = [script, expected_sha256, inputs, timeout_seconds] + extras
        signature = hashlib.sha256(json.dumps(signature_data, sort_keys=True).encode()).hexdigest()
        run_id = hashlib.sha256(str(request_id).encode()).hexdigest()[:32] if request_id else uuid4().hex
        path = guarded(agent_paths.store_item(self.workspace, 'analysis_runs', run_id + '.json'))
        root = path.parent
        if path.exists():
            previous = self.get(run_id)
            if previous['signature'] != signature:
                raise ToolError('ANALYSIS_CONFLICT', '同一调用标识对应不同请求。')
            return {**previous, 'replayed': False}
        files, manifest = self.snapshot(script, expected_sha256, inputs)
        consent = None
        if backend == 'local':
            consent = approve(files, manifest, timeout_seconds) if approve else None
            if not consent:
                return {'executed': False, 'status': 'approval_denied'}
            runtime = {'available': True, 'image_id': None, 'image_profile': None, 'image': None}
        else:
            runtime = _select_profile_runtime(_call_probe(self.workspace), image_profile)
            runtime['export_outputs'] = export_outputs
        if not runtime['available']:
            return safe_value({'executed': False, 'status': 'sandbox_unavailable', 'image_profile': image_profile,
                               'runtime': runtime, 'validation': {'syntax_valid': True, 'manifest': manifest}})
        root.mkdir(parents=True, exist_ok=True)
        receipt = {'run_id': run_id, 'signature': signature, 'status': 'running_or_interrupted',
                   'manifest': manifest, 'backend': backend, 'approval': consent,
                   'image_profile': image_profile if backend == 'docker' else None,
                   'image': runtime.get('image'), 'image_id': runtime['image_id'],
                   'export_outputs': export_outputs if backend == 'docker' else False,
                   'content_is_untrusted': True,
                   'note': ('本机执行无隔离，脚本可能产生外部副作用；不代表科学验收或正式工具提交授权。' if backend == 'local'
                            else '独立分析结果，不修改正式任务、不代表科学验收或提交授权。')}
        try:
            with path.open('x', encoding='utf-8') as stream:
                json.dump(receipt, stream, ensure_ascii=False)
                stream.flush()
                os.fsync(stream.fileno())
        except FileExistsError:
            previous = self.get(run_id)
            if previous['signature'] != signature:
                raise ToolError('ANALYSIS_CONFLICT', '同一调用标识对应不同请求。') from None
            return {**previous, 'replayed': False}
        try:
            with tempfile.TemporaryDirectory(prefix='vasp-analysis-', ignore_cleanup_errors=True) as directory, \
                    analysis_outputs.staging_directory(backend == 'docker' and export_outputs) as output_stage:
                snapshot = Path(directory)
                snapshot.chmod(0o755)
                for name, data in files.items():
                    target = snapshot / name
                    target.parent.mkdir(parents=True, exist_ok=True)
                    target.write_bytes(data)
                if output_stage is not None:
                    runtime = {**runtime, 'output_dir': str(output_stage)}
                exec_result = execute(runtime, snapshot, timeout_seconds) if backend == 'docker' else execute_local(snapshot, timeout_seconds)
                receipt.update(exec_result)
                receipt['content_is_untrusted'] = True
                if output_stage is not None:
                    receipt['exported_outputs'] = analysis_outputs.collect_and_publish(
                        self.workspace, run_id, output_stage, receipt.get('status', 'failed'))
        except Exception as exc:
            receipt.update(status='execution_error', error_type=type(exc).__name__)
        atomic_write(path, json.dumps(safe_value(receipt), ensure_ascii=False).encode('utf-8'))
        return safe_value(receipt)
