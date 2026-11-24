"""One independent Slurm allocation per frozen convergence test.

Receipts describe validated VASP output, never scheduler completion. The last
element collects every output under an advisory process lock. Remote status may
also finalize a scheduler-terminal array, making missing receipts a failure.
"""
import json
import os
import re
import shutil
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

from pymatgen.io.vasp.inputs import Kpoints, Poscar

import vasp_agent.workflow.full_workflow as full
import vasp_agent.workflow.stage_gate as gate
import vasp_agent.workflow.vasp_band_agent as core
from vasp_agent.workflow.convergence_array_checks import (
    ARRAY_EXECUTION_VERSION,
    JOURNAL_NAME,
    PARENT_NAME,
    RECEIPT_NAME,
    SUMMARY_NAME,
    _bindings,
    _context,
    _identity,
    _journal_context,
    _number,
    _parent,
    _parent_claim,
    _prepare_journal,
    _scheduler_check,
    _submission_parent,
    _validate_receipt,
    tests_from_config,
)

__all__ = [
    'ARRAY_EXECUTION_VERSION',
    'JOURNAL_NAME',
    'PARENT_NAME',
    'RECEIPT_NAME',
    'SUMMARY_NAME',
    '_bindings',
    '_collect',
    '_context',
    '_environment',
    '_finalize_lock',
    '_identity',
    '_journal_context',
    '_number',
    '_parent',
    '_parent_claim',
    '_prepare_journal',
    '_scheduler_check',
    '_submission_parent',
    '_validate_receipt',
    'collect_array',
    'execution_command',
    'finalize_array',
    'run_array_test',
    'tests_from_config',
]


def _environment(root):
    environment = json.loads((root / 'environment-review.json').read_text(encoding='utf-8'))
    executable = shutil.which('vasp_std')
    if not executable or core.digest(Path(executable)) != environment.get('executable_sha256'):
        raise ValueError('计算节点VASP与准备阶段环境指纹不同，停止执行。')
    script = environment.get('environment_script')
    if script and core.digest(Path(script)) != environment.get('environment_sha256'):
        raise ValueError('VASP 环境脚本指纹已改变，停止执行。')


def execution_command(config, command):
    if command != ['srun', 'vasp_std']:
        raise ValueError('作业数组只支持受控启动命令 srun vasp_std。')
    cores, nodes, parallel = (config.get('cores_per_test', 8), config.get('nodes', 1),
                              config.get('parallel_tests', 1))
    if (type(cores) is not int or not 1 <= cores <= 65536 or type(nodes) is not int
            or not 1 <= nodes <= 256 or cores < nodes or cores % nodes
            or type(parallel) is not int or not 1 <= parallel <= 6 or parallel * cores > 65536):
        raise ValueError('数组每元素 MPI/节点布局或最大活跃元素数无效。')
    if _number(os.environ.get('SLURM_NTASKS'), 'SLURM_NTASKS') != cores:
        raise ValueError('Slurm 数组元素实际 MPI 进程数与每测试核数不一致。')
    if _number(os.environ.get('SLURM_CPUS_PER_TASK', '1'), 'SLURM_CPUS_PER_TASK') != 1:
        raise ValueError('数组每个 MPI 进程必须分配一个 CPU。')
    allocated_nodes = os.environ.get('SLURM_JOB_NUM_NODES', os.environ.get('SLURM_NNODES'))
    if _number(allocated_nodes, 'SLURM_JOB_NUM_NODES') != nodes:
        raise ValueError('Slurm 数组元素实际分配节点数与配置不一致。')
    cpu_layout = os.environ.get('SLURM_JOB_CPUS_PER_NODE')
    if cpu_layout:
        allocation = []
        for group in cpu_layout.split(','):
            match = re.fullmatch(r'([0-9]+)(?:\(x([0-9]+)\))?', group.strip())
            if not match:
                raise ValueError('Slurm 已分配 CPU 清单无效。')
            allocation.extend([int(match[1])] * int(match[2] or 1))
        if len(allocation) != nodes or any(cpu < cores // nodes for cpu in allocation):
            raise ValueError('Slurm 数组元素实际分配 CPU 不足。')
    elif nodes == 1 and os.environ.get('SLURM_CPUS_ON_NODE'):
        if _number(os.environ['SLURM_CPUS_ON_NODE'], 'SLURM_CPUS_ON_NODE') < cores:
            raise ValueError('Slurm 数组元素实际分配 CPU 不足。')
    result = ['srun', '--exclusive', '--exact', f'--nodes={nodes}', f'--ntasks={cores}', '--cpus-per-task=1']
    if nodes > 1:
        result.append(f'--ntasks-per-node={cores // nodes}')
    return [*result, 'vasp_std']


def _collect(root, parent_job_id, config, tests, bindings, scheduler_terminal, scheduler_evidence=None):
    rows, samples, missing, failures = [], [], [], []
    for test in tests:
        row = {'task_id': test['task_id'], 'stage': test['stage']}
        path = root / test['stage'] / RECEIPT_NAME
        if not path.exists():
            missing.append(test['task_id'])
            row.update(status='missing', error='缺少元素验收回执。')
            samples.append(None)
        else:
            try:
                _, sample = _validate_receipt(root, test, parent_job_id, bindings)
                samples.append(sample)
                row['status'] = 'complete'
            except Exception as exc:
                failures.append(test['task_id'])
                samples.append(None)
                row.update(status='failed', error=str(exc))
        rows.append(row)
    scheduler_failures, scheduler_error = _scheduler_check(parent_job_id, tests, scheduler_terminal, scheduler_evidence)
    status = 'failed' if failures or scheduler_failures or scheduler_terminal and missing else 'running'
    summary_path = root / SUMMARY_NAME
    previous = {}
    if summary_path.exists():
        previous = json.loads(summary_path.read_text(encoding='utf-8'))
        if previous.get('scheduler_failure'):
            scheduler_failures = previous.get('scheduler_failed_tasks') or list(range(len(tests)))
            scheduler_error = previous.get('error') or '已记录数组调度失败，禁止由 worker 再次放行。'
            status = 'failed'
        elif not failures and not missing and not scheduler_failures:
            status = previous.get('status', 'running')
    raw_scheduler_rows = scheduler_evidence.get('tasks') if isinstance(scheduler_evidence, dict) else None
    scheduler_rows = {row['task_id']: row for row in (raw_scheduler_rows or [])
                      if isinstance(row, dict) and type(row.get('task_id')) is int} if isinstance(raw_scheduler_rows, list) else {}
    for row in rows:
        scheduled = scheduler_rows.get(row['task_id'])
        if scheduled:
            row.update(scheduler_state=scheduled.get('state'), scheduler_exit_code=scheduled.get('exit_code'))
        if row['task_id'] in scheduler_failures:
            row.update(receipt_status=row['status'], status='failed', error=scheduler_error)
    failures = sorted(set(failures) | set(scheduler_failures))
    report = {'version': ARRAY_EXECUTION_VERSION, 'parent_job_id': parent_job_id,
        'status': status, 'phase': 'convergence', 'test_count': len(tests), 'tests': tests,
        'tasks': rows, 'missing_tasks': missing, 'failed_tasks': failures, 'bindings': bindings,
        'config_sha256': bindings['config_sha256'], 'all_receipts_present': not missing,
        'ready_to_finalize': not missing, 'scheduler_terminal': bool(scheduler_terminal)}
    if scheduler_terminal or previous.get('scheduler_failure'):
        report.update(scheduler_evidence=scheduler_evidence or previous.get('scheduler_evidence'),
                      scheduler_failed_tasks=scheduler_failures, scheduler_failure=bool(scheduler_failures))
    if failures or scheduler_terminal and missing:
        report['error'] = scheduler_error or '作业数组未通过全部实际输出验收；缺失或失败元素禁止放行。'
    if status == 'awaiting_review':
        report['next'] = 'relaxation'
    return report, samples


def collect_array(root, parent_job_id, scheduler_terminal=False, scheduler_evidence=None):
    """Read and independently validate receipts and real outputs; never write state."""
    root, parent_job_id = Path(root).resolve(), _parent(str(parent_job_id))
    try:
        config, tests, bindings = _context(root, parent_job_id)
        return _collect(root, parent_job_id, config, tests, bindings, scheduler_terminal, scheduler_evidence)[0]
    except Exception as exc:
        return {'version': ARRAY_EXECUTION_VERSION, 'parent_job_id': parent_job_id,
                'phase': 'convergence', 'status': 'failed', 'error': str(exc),
                'ready_to_finalize': False, 'scheduler_terminal': bool(scheduler_terminal)}


@contextmanager
def _finalize_lock(root):
    """Kernel lock survives crashes without leaving an unacquirable file lock."""
    path = root / 'convergence/.array-finalize.lock'
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open('a+b') as stream:
        if os.name == 'nt':
            import msvcrt
            stream.seek(0)
            msvcrt.locking(stream.fileno(), msvcrt.LK_LOCK, 1)
            try:
                yield
            finally:
                stream.seek(0)
                msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl
            fcntl.flock(stream.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(stream.fileno(), fcntl.LOCK_UN)


def finalize_array(root, parent_job_id, scheduler_terminal=False, scheduler_evidence=None):
    """Accept the whole array once, or record a unified terminal failure.

    Only the last worker or status with scheduler-terminal evidence should call this.
    Terminal status requires every scheduler task COMPLETED with exit 0:0 as well
    as real output validation. The last worker can finish locally while RUNNING.
    """
    root, parent_job_id = Path(root).resolve(), _parent(str(parent_job_id))
    with _finalize_lock(root):
        try:
            config, tests, bindings = _context(root, parent_job_id)
            report, samples = _collect(root, parent_job_id, config, tests, bindings, scheduler_terminal, scheduler_evidence)
            if report['missing_tasks'] and not scheduler_terminal:
                return report
            if report['status'] == 'awaiting_review':
                if scheduler_terminal:
                    previous = json.loads((root / SUMMARY_NAME).read_text(encoding='utf-8'))
                    report = {**previous, **report}
                    if previous.get('scheduler_evidence') != scheduler_evidence or not previous.get('scheduler_terminal'):
                        report['scheduler_confirmed_at'] = datetime.now(timezone.utc).isoformat()
                        core.save(root / SUMMARY_NAME, report)
                return report
            if report['failed_tasks'] or report['missing_tasks']:
                full.save_partial_convergence(root, config)
            else:
                _prepare_journal(root, parent_job_id, tests, bindings)
                full.accept_convergence(root, config, samples)
                if gate.current(root) == 'convergence':
                    gate.complete(root, 'convergence')
                else:
                    # Checkpoint may have been saved just before the process
                    # died, leaving its matching workflow state unwritten.
                    checkpoint = json.loads((root / 'checkpoint.json').read_text(encoding='utf-8'))
                    core.save(root / 'workflow-state.json', {'status': 'awaiting_review', **checkpoint})
                report.update(status='awaiting_review', next='relaxation',
                    convergence_sha256=core.digest(root / 'convergence.json'),
                    evidence_hashes=gate.evidence(root, 'relaxation')['hashes'])
        except Exception as exc:
            if 'report' not in locals():
                report = {'version': ARRAY_EXECUTION_VERSION, 'parent_job_id': parent_job_id,
                          'phase': 'convergence', 'scheduler_terminal': bool(scheduler_terminal)}
            report.update(status='failed', error=str(exc))
        if report['status'] != 'awaiting_review':
            report['status'] = 'failed'
            state_path = root / 'workflow-state.json'
            try:
                state = json.loads(state_path.read_text(encoding='utf-8')) if state_path.exists() else {}
            except (ValueError, OSError):
                state = {}
            core.save(state_path, {**state, 'status': 'failed', 'stage': 'convergence', 'next': 'convergence', 'completed': [],
                                  'array_job_id': parent_job_id, 'error': report.get('error', '数组验收失败。')})
            try:
                current_phase = gate.current(root)
            except Exception:
                current_phase = None
            if current_phase != 'convergence':
                # Revoke a previously accepted array if later provenance
                # validation fails; its old checkpoint cannot authorize work.
                checkpoint = {'completed': [], 'next': 'convergence', 'array_failed': True}
                try:
                    checkpoint['evidence_hashes'] = gate.evidence(root, 'convergence')['hashes']
                except Exception:
                    pass
                core.save(root / 'checkpoint.json', checkpoint)
            core.event(root, 'array_failed', parent_job_id=parent_job_id, error=report.get('error'))
        report['finalized_at'] = datetime.now(timezone.utc).isoformat()
        core.save(root / SUMMARY_NAME, report)
        return report


def run_array_test(root, command):
    root = Path(root).resolve()
    if os.environ.get('VASP_AGENT_PHASE', 'convergence') != 'convergence':
        raise ValueError('作业数组只允许 convergence 阶段。')
    parent_job_id = _parent(os.environ.get('SLURM_ARRAY_JOB_ID'))
    task_id = _number(os.environ.get('SLURM_ARRAY_TASK_ID'), 'SLURM_ARRAY_TASK_ID')
    config, tests, bindings = _context(root, parent_job_id)
    if not 0 <= task_id < len(tests):
        raise ValueError('SLURM_ARRAY_TASK_ID 超出冻结测试清单。')
    count = os.environ.get('SLURM_ARRAY_TASK_COUNT')
    if count is not None and _number(count, 'SLURM_ARRAY_TASK_COUNT') != len(tests):
        raise ValueError('Slurm 数组元素总数与冻结方案不一致。')
    with _finalize_lock(root):
        _submission_parent(root, parent_job_id, tests)
        _parent_claim(root, parent_job_id, tests, bindings, create=True)
    test = tests[task_id]
    folder = root / test['stage']
    folder.mkdir(parents=True, exist_ok=True)
    lock = folder / '.array-test.lock'
    with lock.open('x', encoding='utf-8') as stream:
        json.dump({'parent_job_id': parent_job_id, 'task_id': task_id}, stream)
    failure = None
    try:
        if (folder / RECEIPT_NAME).exists():
            _environment(root)
            execution_command(config, command)
            _validate_receipt(root, test, parent_job_id, bindings)
        else:
            try:
                _environment(root)
                controlled = execution_command(config, command)
                initial = Poscar.from_file(root / 'initial.POSCAR').structure
                full._stage_context.parallel = True
                data = full.stage(root, test['stage'], initial, full.base_incar(config, test['encut']),
                                  Kpoints.gamma_automatic(test['mesh']), controlled, event_root=folder)
                full.metrics(data)
                receipt = {**_identity(test, parent_job_id, bindings), 'status': 'complete', 'execution_exit_code': 0,
                    'input_hashes': json.loads((folder / 'inputs.json').read_text(encoding='utf-8')),
                    'inputs_sha256': core.digest(folder / 'inputs.json'),
                    'output_sha256': core.digest(folder / 'vasprun.xml'),
                    'complete_sha256': core.digest(folder / 'complete.json')}
                core.save(folder / RECEIPT_NAME, receipt)
                _validate_receipt(root, test, parent_job_id, bindings)
            except Exception as exc:
                failure = exc
                receipt = {**_identity(test, parent_job_id, bindings), 'status': 'failed', 'error': str(exc),
                    'input_hashes': {name: core.digest(folder / name) for name in ('POSCAR', 'INCAR', 'KPOINTS', 'POTCAR') if (folder / name).is_file()},
                    'output_sha256': core.digest(folder / 'vasprun.xml') if (folder / 'vasprun.xml').is_file() else None}
                core.save(folder / RECEIPT_NAME, receipt)
                core.event(folder, 'array_test_failed', parent_job_id=parent_job_id, task_id=task_id, error=str(exc))
            finally:
                full._stage_context.parallel = False
    finally:
        lock.unlink()
    # No shared workflow-state or root event writes until all receipts exist.
    if all((root / item['stage'] / RECEIPT_NAME).is_file() for item in tests):
        report = finalize_array(root, parent_job_id)
    else:
        report = {'status': 'running', 'parent_job_id': parent_job_id, 'task_id': task_id,
                  'stage': test['stage'], 'test_count': len(tests)}
    if failure is not None:
        raise failure
    if report['status'] == 'failed':
        raise ValueError(report.get('error', '作业数组验收失败。'))
    return report
