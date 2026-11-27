"""Provenance, journal, receipt, and scheduler-terminal verification for convergence arrays."""
import json
import os
import re

from pymatgen.io.vasp.inputs import Kpoints

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
    from vasp_agent.workflow.solver_overrides import apply_to_incar
    if not full.incar_matches(folder, config, apply_to_incar(full.base_incar(config, test['encut']), config, 'convergence', test['stage'])):
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
