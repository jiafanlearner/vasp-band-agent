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

from pymatgen.io.vasp.inputs import Incar, Kpoints, Poscar

import vasp_agent.workflow.full_workflow as full
import vasp_agent.workflow.stage_gate as gate
import vasp_agent.workflow.vasp_band_agent as core

ARRAY_EXECUTION_VERSION = 1
RECEIPT_NAME = 'array-receipt.json'
SUMMARY_NAME = 'convergence-array.json'
JOURNAL_NAME = 'convergence-array-finalization.json'
PARENT_NAME = 'convergence-array-parent.json'


def tests_from_config(config):
    return full.convergence_tests(config)


def _number(value, name):
    if not isinstance(value, str) or not re.fullmatch(r'[0-9]+', value):
        raise ValueError(f'{name} 必须为 Slurm 提供的数字。')
    return int(value)


def _parent(value):
    _number(value, 'SLURM_ARRAY_JOB_ID')
    if int(value) < 1:
        raise ValueError('SLURM_ARRAY_JOB_ID 无效。')
    return value


def _bindings(root):
    return {name.replace('.json', '').replace('-', '_') + '_sha256': core.digest(root / name)
            for name in ('config.json', 'manifest.json', 'review-convergence.json',
                         'physics-model.json', 'environment-review.json')}


def _submission_parent(root, parent_job_id, tests):
    """The reservation is durable before sbatch; its parent ID may arrive later."""
    paths = [root / 'submissions/convergence.json', root / 'stage-submissions/convergence.json']
    latest = root / 'remote-submission.json'
    if latest.exists():
        data = json.loads(latest.read_text(encoding='utf-8'))
        if data.get('phase') == 'convergence':
            paths.append(latest)
    for path in paths:
        if not path.exists():
            continue
        data = json.loads(path.read_text(encoding='utf-8'))
        if data.get('phase', 'convergence') != 'convergence' or data.get('submission_mode', 'array') != 'array':
            raise ValueError('保存的收敛提交记录不是本次数组阶段。')
        recorded = data.get('job_id')
        if recorded is None:
            if data.get('status') not in ('pending', 'submitting', 'reserved', 'submission_uncertain'):
                raise ValueError('提交记录缺少明确父作业编号或正在登记的预约。')
        elif _parent(str(recorded)) != parent_job_id:
            raise ValueError('数组父作业编号与保存的收敛提交记录不一致。')
        if ('array_task_count' in data and (type(data['array_task_count']) is not int or data['array_task_count'] != len(tests))
                or 'array_task_ids' in data and (data['array_task_ids'] != [test['task_id'] for test in tests]
                    or any(type(index) is not int for index in data['array_task_ids']))):
            raise ValueError('提交记录的数组元素清单与冻结测试不一致。')


def _parent_claim(root, parent_job_id, tests, bindings, create=False):
    expected = {'version': ARRAY_EXECUTION_VERSION, 'parent_job_id': parent_job_id,
                'tests': tests, 'bindings': bindings}
    path = root / PARENT_NAME
    if path.exists():
        if json.loads(path.read_text(encoding='utf-8')) != expected:
            raise ValueError('当前测试目录已绑定另一父数组或输入方案。')
    elif create:
        core.save(path, expected)


def _journal_context(root, parent_job_id, tests, bindings, review):
    """Authorize recovery of the summary-write/checkpoint-write crash window."""
    from vasp_agent.workflow.input_review import context as input_context
    journal = json.loads((root / JOURNAL_NAME).read_text(encoding='utf-8'))
    expected = {'version': ARRAY_EXECUTION_VERSION, 'parent_job_id': parent_job_id,
                'tests': tests, 'bindings': bindings, 'review_snapshot': review['snapshot']}
    if any(journal.get(name) != value for name, value in expected.items()):
        raise ValueError('收敛汇总恢复日志的输入、审阅或父作业不一致。')
    if journal.get('input_snapshot') != input_context(root)['snapshot']:
        raise ValueError('汇总中断后提交脚本、环境或输入审阅上下文已改变。')
    checkpoint_path = root / 'checkpoint.json'
    checkpoint_hash = core.digest(checkpoint_path) if checkpoint_path.exists() else None
    if gate.current(root) == 'convergence' and checkpoint_hash != journal.get('initial_checkpoint_sha256'):
        raise ValueError('汇总中断后的原阶段记录已改变。')
    expected_receipts = {test['stage'] + '/' + RECEIPT_NAME: core.digest(root / test['stage'] / RECEIPT_NAME) for test in tests}
    if expected_receipts != journal.get('receipt_hashes'):
        raise ValueError('汇总中断后的元素回执已改变。')
    original_hashes = journal.get('initial_evidence_hashes', {})
    other_evidence = {name: value for name, value in gate.evidence(root, 'convergence')['hashes'].items() if name != 'convergence.json'}
    if other_evidence != {name: value for name, value in original_hashes.items() if name != 'convergence.json'}:
        raise ValueError('汇总中断后出现不同的其他阶段证据。')
    for name, expected_hash in original_hashes.items():
        if name != 'convergence.json' and core.digest(root / name) != expected_hash:
            raise ValueError('汇总中断后的其他阶段证据已改变。')
    for test in tests:
        _validate_receipt(root, test, parent_job_id, bindings)
    return journal


def _prepare_journal(root, parent_job_id, tests, bindings):
    from vasp_agent.workflow.input_review import context as input_context
    review = json.loads((root / 'review-convergence.json').read_text(encoding='utf-8'))
    path = root / JOURNAL_NAME
    if path.exists():
        return _journal_context(root, parent_job_id, tests, bindings, review)
    checkpoint = root / 'checkpoint.json'
    journal = {'version': ARRAY_EXECUTION_VERSION, 'parent_job_id': parent_job_id,
        'tests': tests, 'bindings': bindings, 'review_snapshot': review['snapshot'],
        'input_snapshot': input_context(root)['snapshot'],
        'initial_checkpoint_sha256': core.digest(checkpoint) if checkpoint.exists() else None,
        'initial_evidence_hashes': gate.evidence(root, 'convergence')['hashes'],
        'receipt_hashes': {test['stage'] + '/' + RECEIPT_NAME: core.digest(root / test['stage'] / RECEIPT_NAME) for test in tests}}
    core.save(path, journal)
    return journal


def _context(root, parent_job_id):
    manifest = core.verify(root)
    config = json.loads((root / 'config.json').read_text(encoding='utf-8'))
    if (manifest.get('workflow') != 'full' or not manifest.get('stage_gates')
            or not config.get('stage_gates') or config.get('submission_mode', 'allocation') != 'array'):
        raise ValueError('作业数组仅支持已审阅的完整流程收敛阶段。')
    tests = tests_from_config(config)
    bindings = _bindings(root)
    _submission_parent(root, parent_job_id, tests)
    _parent_claim(root, parent_job_id, tests, bindings)
    review = json.loads((root / 'review-convergence.json').read_text(encoding='utf-8'))
    if review.get('phase') != 'convergence' or not review.get('allowed'):
        raise ValueError('收敛阶段审阅未放行。')
    summary_path = root / SUMMARY_NAME
    if summary_path.exists():
        summary = json.loads(summary_path.read_text(encoding='utf-8'))
        if (summary.get('version') != ARRAY_EXECUTION_VERSION or summary.get('parent_job_id') != parent_job_id
                or summary.get('bindings') != bindings or summary.get('tests') != tests):
            raise ValueError('数组汇总的父作业、输入或审阅指纹与本次不一致。')
        if summary.get('status') == 'awaiting_review':
            if gate.current(root) == 'convergence' or core.digest(root / 'convergence.json') != summary.get('convergence_sha256'):
                raise ValueError('数组完成后的收敛证据或阶段记录已改变。')
            actual = gate.evidence(root, 'relaxation')['hashes']
            if actual != summary.get('evidence_hashes'):
                raise ValueError('数组完成后的输入或输出证据已改变。')
    else:
        if os.environ.get('VASP_AGENT_PHASE', 'convergence') != 'convergence':
            raise ValueError('数组执行只允许 convergence 阶段。')
        if gate.current(root) == 'convergence':
            try:
                gate.require_review(root, 'convergence')
            except ValueError:
                if not (root / 'convergence.json').exists() or not (root / JOURNAL_NAME).exists():
                    raise
                _journal_context(root, parent_job_id, tests, bindings, review)
        else:
            # A killed finalizer may have written the checkpoint just before
            # its summary. Recover only from every validated element and the
            # checkpoint's exact physical evidence, never from scheduler state.
            checkpoint = json.loads((root / 'checkpoint.json').read_text(encoding='utf-8'))
            if (gate.current(root) != 'relaxation' or checkpoint.get('completed') != ['convergence']
                    or checkpoint.get('evidence_hashes') != gate.evidence(root, 'relaxation')['hashes']):
                raise ValueError('数组缺少可验证的收敛阶段完成证据。')
            for test in tests:
                _validate_receipt(root, test, parent_job_id, bindings)
            if (root / JOURNAL_NAME).exists():
                _journal_context(root, parent_job_id, tests, bindings, review)
    return config, tests, bindings


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


def _identity(test, parent_job_id, bindings):
    return {'version': ARRAY_EXECUTION_VERSION, 'parent_job_id': parent_job_id,
            'task_id': test['task_id'], 'stage': test['stage'], 'test': test,
            'config_sha256': bindings['config_sha256'], 'bindings': bindings}


def _validate_receipt(root, test, parent_job_id, bindings):
    folder = root / test['stage']
    receipt = json.loads((folder / RECEIPT_NAME).read_text(encoding='utf-8'))
    if (type(receipt.get('version')) is not int or type(receipt.get('task_id')) is not int
            or any(receipt.get(k) != value for k, value in _identity(test, parent_job_id, bindings).items())):
        raise ValueError('数组元素回执的索引、父作业或输入指纹不一致。')
    if receipt.get('status') == 'failed':
        raise ValueError(receipt.get('error') or '数组元素执行失败。')
    if receipt.get('status') != 'complete':
        raise ValueError('数组元素没有已验收完成回执。')
    if type(receipt.get('execution_exit_code')) is not int or receipt['execution_exit_code'] != 0:
        raise ValueError('数组元素缺少 VASP 正常退出的执行回执。')
    inputs = json.loads((folder / 'inputs.json').read_text(encoding='utf-8'))
    if set(inputs) != {'POSCAR', 'INCAR', 'KPOINTS', 'POTCAR'} or inputs != receipt.get('input_hashes'):
        raise ValueError('数组元素输入来源记录不完整或已改变。')
    for name, expected in inputs.items():
        if core.digest(folder / name) != expected:
            raise ValueError('数组元素输入哈希不一致：' + test['stage'] + '/' + name)
    if (core.digest(folder / 'POSCAR') != core.digest(root / 'initial.POSCAR')
            or core.digest(folder / 'POTCAR') != core.digest(root / 'POTCAR')):
        raise ValueError('数组测试结构或赝势与冻结方案不一致。')
    config = json.loads((root / 'config.json').read_text(encoding='utf-8'))
    if not full.incar_matches(folder, config, full.base_incar(config, test['encut'])):
        raise ValueError('数组测试 INCAR 与冻结方案不一致。')
    points = Kpoints.from_file(folder / 'KPOINTS')
    if points.style.name != 'Gamma' or list(points.kpts[0]) != test['mesh'] or tuple(points.kpts_shift) != (0, 0, 0):
        raise ValueError('数组测试网格与冻结方案不一致。')
    stamp = json.loads((folder / 'complete.json').read_text(encoding='utf-8'))
    output_sha256 = core.digest(folder / 'vasprun.xml')
    if (output_sha256 != receipt.get('output_sha256') or output_sha256 != stamp.get('xml_sha256')
            or core.digest(folder / 'complete.json') != receipt.get('complete_sha256')
            or core.digest(folder / 'inputs.json') != receipt.get('inputs_sha256')):
        raise ValueError('数组元素实际输出与验收回执不一致。')
    return receipt, full.metrics(core.read_result(folder))


def _scheduler_check(parent_job_id, tests, scheduler_terminal, scheduler_evidence):
    if not scheduler_terminal:
        return [], None
    count = len(tests)
    evidence = scheduler_evidence
    if (not isinstance(evidence, dict) or evidence.get('job_id') != parent_job_id
            or type(evidence.get('task_count')) is not int or evidence['task_count'] != count
            or evidence.get('all_terminal') is not True or evidence.get('active') is not False
            or evidence.get('known') is not True or not isinstance(evidence.get('tasks'), list)
            or len(evidence['tasks']) != count):
        return list(range(count)), '缺少与父作业和完整元素清单匹配的调度终态证据。'
    rows = {}
    for row in evidence['tasks']:
        if (not isinstance(row, dict) or type(row.get('task_id')) is not int
                or row['task_id'] in rows or not 0 <= row['task_id'] < count
                or row.get('job_id') != f"{parent_job_id}_{row['task_id']}"):
            return list(range(count)), '调度终态证据包含错误、重复或越界元素。'
        rows[row['task_id']] = row
    failures = [index for index, row in rows.items() if row.get('terminal') is not True
                or row.get('state') != 'COMPLETED' or row.get('exit_code') != '0:0'
                or row.get('successful') is not True]
    if failures or evidence.get('all_successful') is not True:
        return sorted(failures) if failures else list(range(count)), '调度元素失败、取消、超时或未正常退出；已有 XML 不能替代正常终态。'
    return [], None


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
