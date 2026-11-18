"""Draft analysis with separate Docker isolation and operator-approved host execution."""
import ast
import hashlib
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
from .edit_store import guarded
from vasp_agent.workflow.vasp_band_agent import ToolError
from vasp_agent.common import agent_paths

IMAGE = 'python:3.12-slim'


def probe():
    docker = shutil.which('docker')
    if not docker:
        return {'available': False, 'reason': '未安装 Docker；不会在宿主机执行脚本。'}
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
        image = query(['image', 'inspect', IMAGE, '--format', '{{.Id}}'])
        if not re.fullmatch(r'sha256:[a-f0-9]{64}', image):
            raise ValueError('需预先安装 python:3.12-slim 镜像；不会自动拉取。')
        return {'available': True, 'docker': docker, 'image_id': image, 'network': False}
    except (OSError, ValueError, KeyError, IndexError, subprocess.TimeoutExpired) as exc:
        return {'available': False, 'reason': safe_value(str(exc))}


def container_command(runtime, name, snapshot):
    return [runtime['docker'], 'run', '--name', name, '--rm', '--pull=never', '--network=none',
            '--read-only', '--cap-drop=ALL', '--security-opt=no-new-privileges', '--user=65534:65534',
            '--memory=256m', '--memory-swap=256m', '--cpus=1', '--pids-limit=32', '--log-driver=none',
            '--ulimit', 'fsize=1048576:1048576', '--tmpfs', '/tmp:rw,noexec,nosuid,size=16m',
            '--mount', 'type=bind,source=' + str(snapshot) + ',target=/analysis,readonly',
            '--workdir=/analysis', runtime['image_id'], 'python', '-I', '-B', '/analysis/main.py']


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

    def run(self, script, expected_sha256, inputs, timeout_seconds=30, request_id=None, *, backend="docker", approve=None):
        if type(timeout_seconds) is not int or not 1 <= timeout_seconds <= 60:
            raise ValueError('运行时间须为1至60秒。')
        if backend not in ('docker', 'local'):
            raise ValueError('运行模式无效。')
        signature_data = [script, expected_sha256, inputs, timeout_seconds] + (['local'] if backend == 'local' else [])
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
        runtime = probe() if backend == 'docker' else {'available': True, 'image_id': None}
        if not runtime['available']:
            return {'executed': False, 'status': 'sandbox_unavailable', 'runtime': runtime,
                    'validation': {'syntax_valid': True, 'manifest': manifest}}
        root.mkdir(parents=True, exist_ok=True)
        receipt = {'run_id': run_id, 'signature': signature, 'status': 'running_or_interrupted',
                   'manifest': manifest, 'backend': backend, 'approval': consent, 'image_id': runtime['image_id'], 'content_is_untrusted': True,
                   'note': ('本机执行无隔离，脚本可能产生外部副作用；不代表科学验收或正式工具提交授权。' if backend == 'local'
                            else '独立分析结果，不修改正式任务、不代表科学验收或提交授权。')}
        try:
            with path.open('x', encoding='utf-8') as stream:
                json.dump(receipt, stream, ensure_ascii=False)
                stream.flush()
                os.fsync(stream.fileno())
        except FileExistsError:
            return self.get(run_id)
        try:
            with tempfile.TemporaryDirectory(prefix='vasp-analysis-') as directory:
                snapshot = Path(directory)
                snapshot.chmod(0o755)
                for name, data in files.items():
                    target = snapshot / name
                    target.parent.mkdir(parents=True, exist_ok=True)
                    target.write_bytes(data)
                receipt.update(execute(runtime, snapshot, timeout_seconds) if backend == 'docker' else execute_local(snapshot, timeout_seconds))
        except Exception as exc:
            receipt.update(status='execution_error', error_type=type(exc).__name__)
        temp = path.with_suffix('.tmp')
        temp.write_text(json.dumps(safe_value(receipt), ensure_ascii=False), encoding='utf-8')
        os.replace(temp, path)
        return safe_value(receipt)
