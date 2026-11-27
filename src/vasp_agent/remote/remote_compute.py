"""SSH transport for deployment, preparation, Slurm submission and result retrieval."""
import argparse
import base64
import hashlib
import json
import math
import os
from pathlib import Path
import shlex
import shutil
import subprocess
import time
from uuid import uuid4

from vasp_agent.remote import ssh_pool
from vasp_agent.remote.bundle import stdin_program
from vasp_agent.remote.remote_servers import Servers, run_ssh
from vasp_agent.remote.remote_worker import RESULT_FILES
from vasp_agent.workflow import stage_definitions as stages


# One ceiling for every action was too small for the two that run an entire remote
# program rather than a query: deploy's preflight imports pymatgen, matplotlib and
# numpy in a cold conda env (self-capped at 180s in the worker), and prepare runs
# all of full_workflow.prepare_full. Both left little room for the file writes
# around them inside the same 300.
CALL_TIMEOUTS = {'monitor_status': 60, 'deployment_info': 60, 'deploy': 900, 'prepare': 600, 'preview': 600,
                 # Hashes every reused stage's vasprun.xml, which can be large.
                 'prepare_phase_resume': 600,
                 # Full evidence verification plus sbatch in one call.
                 'submit_checked': 600, 'configure_and_submit': 600,
                 # Bounded in the worker (worker_inventory.PROBE_BUDGET plus per-build hashing).
                 'software_inventory': 600}
DEFAULT_CALL_TIMEOUT = 300


def delivery_summary(directory):
    """Distinguish a useful partial download from a complete result package.

    What "complete" means follows the task's enabled stages: a download carries
    config.json exactly when the task records them; without it the task runs the
    legacy chain and the package is judged as before (bands last).
    """
    directory = Path(directory)
    def read(name):
        path = directory / name
        return json.loads(path.read_text(encoding='utf-8')) if path.exists() else {}
    issues = []
    try:
        config = read('config.json')
        order, required = stages.order(config), stages.delivery_files(config)
    except (ValueError, AttributeError, TypeError, KeyError):
        order, required = None, stages.COMMON_DELIVERY_FILES
        issues.append('任务阶段记录无效或本地程序不认识其阶段定义版本，无法确认完成范围')
    missing = [name for name in required if not (directory / name).is_file() or (directory / name).stat().st_size == 0]
    try:
        state, checkpoint, report = (read(name) for name in ('workflow-state.json', 'checkpoint.json', 'report.json'))
        if state.get('status') != 'complete':
            issues.append('工作流尚未标记complete')
        if order is not None and (checkpoint.get('next') != 'complete' or checkpoint.get('completed') != list(order)):
            issues.append('四阶段完成记录缺失或不一致' if order == stages.LEGACY_STAGES
                          else f'{len(order)} 个启用阶段（{"、".join(order)}）的完成记录缺失或不一致')
        if order is not None and 'bands' in order:
            validation = report.get('band_validation', {})
            if not validation.get('passed'):
                issues.append('缺少全路径能带验收通过记录（旧版报告需另行核验）')
            gap = report.get('sampled_path_gap')
            if not isinstance(gap, dict) or type(gap.get('energy')) not in (float, int) or not math.isfinite(gap['energy']) or gap['energy'] < 0:
                issues.append('带隙报告缺失或格式无效')
        if order is not None and 'dos' in order:
            dos = report.get('dos')
            if not isinstance(dos, dict) or dos.get('status') != 'passed':
                issues.append('缺少 DOS 程序验收通过记录（report.json 的 dos 字段）')
    except (ValueError, AttributeError, TypeError):
        state, report = {}, {}
        issues.append('结果JSON格式无效')
    return {'status': 'complete_package' if not missing and not issues else 'partial_or_unverified',
            'missing_files': missing, 'issues': issues,
            'sampled_path_gap': report.get('sampled_path_gap'),
            'note': '这是交付文件一致性检查；不替代原始输出验收，也不等同全布里渊区带隙证明。'}


def validate_paths(config):
    """The four remote paths a compute profile names must be absolute and single-line."""
    for key in ('root', 'python', 'potcar_library', 'vasp_env'):
        value = config[key]
        if not isinstance(value, str) or not value.startswith('/') or any(c in value for c in ('\n', '\x00')):
            raise ValueError(f'远程配置 {key} 需要绝对路径。')
    return config


class RemoteCompute:
    def __init__(self, workspace, server):
        self.workspace = Path(workspace).resolve()
        self.server = server
        self.servers = Servers(workspace)
        connection_file = self.servers.file(server)
        self.config = validate_paths(json.loads(connection_file.with_name(server + '.compute.json').read_text(encoding='utf-8')))

    def call(self, action, job_name=None, **params):
        # Keep multiline Python out of the Windows argv -> SSH -> shell quoting chain.
        # Python reads the complete program from stdin before executing this prelude.
        # Carry the session with every remote call so the remote worker's
        # events.jsonl and the submitted job's environment agree with the local
        # audit trail on which session produced them. Absent when this process
        # was not started by the agent (e.g. the CLI entry point).
        payload = json.dumps({'config': self.config, 'action': action, 'job_name': job_name,
                              'session': os.environ.get('VASP_AGENT_SESSION'), **params})
        program = stdin_program(payload)
        command = shlex.join([self.config['python'], '-X', 'utf8', '-'])
        # The background monitor owns quiet retry/backoff. Avoid transport retry
        # banners interrupting the current conversation during each failed poll.
        transport = subprocess.run if action == 'monitor_status' else run_ssh
        from vasp_agent.workflow.vasp_band_agent import ToolError
        timeout = CALL_TIMEOUTS.get(action, DEFAULT_CALL_TIMEOUT)
        deadline = time.monotonic() + timeout
        try:
            # The pooled connection, when it can be used; None means nothing was
            # sent and this call goes through a new ssh process as before.
            result = ssh_pool.run(self.servers, self.server, command, program, timeout)
            if result is None:
                # Whole seconds: a pooled attempt that fell back spent part of the budget.
                remaining = math.ceil(deadline - time.monotonic())
                if remaining <= 0:
                    raise subprocess.TimeoutExpired(command, timeout)
                result = transport(self.servers.ssh_args(self.server) + [command],
                    input=program,
                    capture_output=True, text=True, encoding='utf-8', errors='replace',
                    timeout=remaining)
        except subprocess.TimeoutExpired:
            # Without this the raw TimeoutExpired reached job_monitor, which records
            # getattr(exc, 'error_code', type(exc).__name__) -- the operator saw the
            # string 'TimeoutExpired' and never learned which action was cut off.
            raise ToolError('REMOTE_TIMEOUT', f'远程操作 {action} 超时（'
                f'{CALL_TIMEOUTS.get(action, DEFAULT_CALL_TIMEOUT)} 秒）；'
                f'远端是否已执行该操作未知，请用只读入口核对任务状态后再重试，不要直接重发。') from None
        if result.returncode == 255:
            from vasp_agent.remote.remote_servers import ssh_error
            try:
                details = self.servers.connection_details(self.server)
            except (AttributeError, OSError, ValueError):
                details = {'server': self.server}
            raise ssh_error(result.stderr, details)
        try:
            response = json.loads(result.stdout.strip().splitlines()[-1])
        except (ValueError, IndexError):
            stdout = result.stdout or ''
            if result.stderr and result.stderr.strip():
                detail = f'{result.stderr[-1900:]} [rc={result.returncode}, stdout_len={len(stdout)}]'
            else:
                tail = repr(stdout[-200:]) if stdout else "''"
                detail = f'rc={result.returncode}, stdout_len={len(stdout)}, stdout_tail={tail}'
            raise ToolError('REMOTE_RESPONSE_INVALID', '远程响应无效：' + detail) from None
        if result.returncode or not response['ok']:
            raise ToolError(response.get('error_code', 'REMOTE_ERROR'), response.get('error', '远程操作失败'))
        return response['result']

    def deploy(self):
        from vasp_agent.remote.bundle import engine_sources
        return self.call('deploy', sources=engine_sources())

    def cancel_job(self, name, phase=None, confirm_job_id=None):
        """Preview the receipt's JobID, or scancel it once the operator typed it."""
        return self.call('cancel_job', name, phase=phase, confirm_job_id=confirm_job_id)

    def release_cancelled_submission(self, name, phase, confirm_job_id):
        return self.call('release_cancelled_submission', name, phase=phase, confirm_job_id=confirm_job_id)

    def prepare_phase_resume(self, name, phase, confirm_job_id=None):
        """Preview a resume of an ended phase, or prepare it once the operator confirmed the JobID."""
        return self.call('prepare_phase_resume', name, phase=phase, confirm_job_id=confirm_job_id)

    def freshness(self):
        """Compare the deployed release with the one this checkout would deploy."""
        from vasp_agent.remote.bundle import local_version
        deployed = self.call('deployment_info')
        local = local_version()
        return {'server': self.server, 'local_version': local, 'deployed': bool(deployed.get('deployed')),
                'deployed_version': deployed.get('version'), 'release': deployed.get('release'),
                'current': deployed.get('version') == local}

    def prepare(self, name, structure, encut=520, mesh=None, nbands=16, relax_mode='cell', parallel_tests=None, cores_per_test=None, encuts=None, meshes=None, parameter_choice=None, preview_only=False, potential_variants=None, scan_thresholds=None, scan_preset=None, band_path=None, environment_profile=None, optional_stages=None, stage_parameters=None, solver_overrides=None):
        from pymatgen.core import Structure
        from pymatgen.io.vasp.inputs import Poscar
        from vasp_agent.common.workspace_paths import workspace_path
        source = workspace_path(self.workspace, structure)
        # Normalize CIF or POSCAR to VASP format before transport.
        data = str(Poscar(Structure.from_file(source))).encode('utf-8')
        return self.call('preview' if preview_only else 'prepare', name, structure=base64.b64encode(data).decode(), parameters={
            'encut': encut, 'mesh': mesh or [8, 8, 8], 'nbands': nbands, 'relax_mode': relax_mode,
            'parallel_tests': parallel_tests, 'cores_per_test': cores_per_test,
            'encuts': encuts, 'meshes': meshes, 'parameter_choice': parameter_choice, 'potential_variants': potential_variants,
            **({'scan_thresholds': scan_thresholds} if scan_thresholds else {}),
            **({'scan_preset': scan_preset} if scan_preset else {}),
            **({'band_path': band_path} if band_path is not None else {}),
            # Only tasks enabling optional stages send these (legacy payload otherwise).
            **({'optional_stages': list(optional_stages)} if optional_stages else {}),
            **({'stage_parameters': stage_parameters} if stage_parameters else {}),
            **({'solver_overrides': solver_overrides} if solver_overrides is not None else {})},
            # Only a registered profile is sent; the implicit default sends nothing (legacy payload).
            **({'environment_profile': environment_profile} if environment_profile is not None else {}))

    def fetch(self, name):
        result = self.call('fetch', name)
        dest = self.workspace / 'downloads' / self.server / (name + '-' + uuid4().hex[:8])
        if not dest.resolve().is_relative_to(self.workspace):
            raise ValueError('下载目录越界。')
        # Assemble under a sibling name and publish with os.replace. The final
        # directory was created before any validation and the uuid suffix guaranteed
        # a retry got a fresh one, so a truncated response or a hash mismatch midway
        # abandoned a directory holding some valid results, no delivery-summary.json
        # and no receipt -- which reads exactly like a genuinely truncated delivery
        # whose summary write failed. Since delivery-summary.json is the artifact the
        # whole verification story rests on, the one state that must not exist on
        # disk is a final-named directory that never got one.
        staging = dest.with_name(dest.name + '.partial')
        staging.mkdir(parents=True)
        try:
            for filename, item in result['files'].items():
                if filename not in RESULT_FILES and filename not in stages.fetchable_files():
                    raise ValueError('服务器返回非结果文件。')
                data = base64.b64decode(item['data'], validate=True)
                if hashlib.sha256(data).hexdigest() != item['sha256']:
                    raise ValueError('下载校验失败。')
                (staging / filename).write_bytes(data)
            delivery = delivery_summary(staging)
            (staging / 'delivery-summary.json').write_text(json.dumps(delivery, ensure_ascii=False, indent=2), encoding='utf-8')
            receipt = {'server': self.server, 'job_name': name,
                       'files': {filename: {'sha256': item['sha256'], 'verified': True}
                                 for filename, item in result['files'].items()},
                       'note': '下载完整性校验不是科学验收；完成状态以checkpoint和报告为准。'}
            (staging / 'download-receipt.json').write_text(json.dumps(receipt, ensure_ascii=False, indent=2), encoding='utf-8')
        except BaseException:
            shutil.rmtree(staging, ignore_errors=True)
            raise
        os.replace(staging, dest)
        return {'directory': str(dest), 'files': list(result['files']),
                'hashes_verified': True, 'delivery': delivery, 'receipt': str(dest / 'download-receipt.json')}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=['deploy', 'prepare', 'submit', 'status', 'fetch'])
    parser.add_argument('--server', required=True)
    parser.add_argument('--workspace', default='.')
    parser.add_argument('--job')
    parser.add_argument('--structure')
    parser.add_argument('--encuts', type=float, nargs=3)
    parser.add_argument('--meshes', type=json.loads)
    parser.add_argument('--phase', choices=list(stages.known_names()), help='新版分阶段提交时必填')
    parser.add_argument('--parallel-tests', type=int, help='独立测试并发数 1–6')
    parser.add_argument('--cores-per-test', type=int, help='每个测试的核数')
    args = parser.parse_args()
    remote = RemoteCompute(args.workspace, args.server)
    if args.action != 'deploy' and not args.job:
        parser.error('--job 必填')
    if args.action == 'deploy':
        result = remote.deploy()
    elif args.action == 'prepare':
        if not args.structure:
            parser.error('--structure 必填')
        result = remote.prepare(args.job, args.structure, parallel_tests=args.parallel_tests, cores_per_test=args.cores_per_test, encuts=args.encuts, meshes=args.meshes)
    elif args.action == 'fetch':
        result = remote.fetch(args.job)
    elif args.action == 'submit':
        result = interactive_submit(args.workspace, args.server, args.job, args.phase)
    else:
        result = remote.call(args.action, args.job, **({'phase': args.phase} if args.action == 'submit' else {}))
    print(json.dumps(result, ensure_ascii=False, indent=2))


def interactive_submit(workspace, server, job, phase, reader=None, output=print):
    import sys
    from vasp_agent.chat.ai_agent import Toolbox
    if reader is None:
        if not sys.stdin.isatty():
            raise ValueError('CLI 提交必须在交互终端确认权限与资源，未提交。')
        reader = input
    box = Toolbox(workspace)
    box.plan_reader, box.plan_output = reader, output
    output(f'目标：{server} / {job} / {phase or "完整流程"}。统一确认资源并授权本次提交。')
    result = box.request_submission(server, job, phase)
    if result.get('status') == 'awaiting_resource_review':
        output('资源已保存，但 CLI 不调用模型完成审阅。请回到 AI 对话重新审阅并提交；此次没有作业 ID。')
    return result


if __name__ == '__main__':
    main()
