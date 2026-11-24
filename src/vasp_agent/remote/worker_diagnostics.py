"""Read-only submission diagnostics, bounded evidence files, and SCF occupation audit.

Part of the stdin worker program (see remote_worker.WORKER_UNITS): standard library only
(scf_occupation_audit lazily imports numpy/pymatgen only when called in the science env).
"""
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess

# <bundle:package-only>
from vasp_agent.remote.worker_common import LEGACY_PHASES, SubmissionError, diagnostic_text, sha, task_phases
from vasp_agent.remote.worker_slurm import (
    array_scheduler_status, capacity_preflight, scheduler_read, submission_accounting, submission_command_evidence)
from vasp_agent.remote.worker_env import engine_module, task_release
# </bundle:package-only>


def diagnostic_file(folder, name, tail=False):
    """Only fixed caller-selected task files; never follow links or execute content."""
    path = folder / name
    if path.is_symlink() or not path.resolve().is_relative_to(folder.resolve()):
        raise ValueError('诊断文件路径越界或为链接。')
    if not path.exists():
        return None
    if not path.is_file():
        raise ValueError('诊断目标不是普通文件。')
    if not tail and path.stat().st_size > 1_000_000:
        raise ValueError('诊断文件过大。')
    with path.open('rb') as stream:
        if tail:
            stream.seek(max(0, path.stat().st_size - 12000))
        return stream.read(12000 if tail else 1_000_001).decode('utf-8', errors='replace' if tail else 'strict').replace('\r\n', '\n')


def task_evidence_files(folder, phase):
    """Fixed allowlist only; hash exactly the bounded bytes that are returned."""
    names = ['events.jsonl', 'remote-submission.json']
    if phase in task_phases(folder):
        names.append('submissions/' + phase + '.json')
    results = []
    for name in names:
        path = folder / name
        item = {'path': name, 'source': 'task_file', 'content_is_untrusted': True}
        try:
            cursor = path
            while cursor != folder:
                if cursor.is_symlink():
                    raise ValueError('日志路径不允许链接。')
                cursor = cursor.parent
            if not path.resolve().is_relative_to(folder.resolve()):
                raise ValueError('日志路径越界。')
            if not path.exists():
                results.append({**item, 'exists': False})
                continue
            if not path.is_file() or path.stat().st_nlink != 1:
                raise ValueError('日志须为非硬链接普通文件。')
            with path.open('rb') as stream:
                size = os.fstat(stream.fileno()).st_size
                offset = max(0, size - 12000)
                stream.seek(offset)
                data = stream.read(12000)
            text = data.decode('utf-8', errors='replace')
            text = re.sub(r'(?i)("(?:password|passwd|api[_-]?key|secret|access[_-]?token|token)"\s*:\s*)"(?:\\.|[^"\\])*"',
                          r'\1"[REDACTED]"', text)
            results.append({**item, 'exists': True, 'file_size': size, 'offset': offset,
                            'bytes_read': len(data), 'truncated': offset > 0 or offset + len(data) < size,
                            'sha256_scope': 'raw_returned_slice', 'sha256': hashlib.sha256(data).hexdigest(),
                            'text': diagnostic_text(text),
                            'note': '哈希对应脱敏前读取片段；截断片段可能从记录中间开始，文件可能仍在增长。'})
        except (OSError, ValueError) as exc:
            results.append({**item, 'read_error': diagnostic_text(str(exc))})
    return results


def submission_diagnostic(folder, phase, job_id=None, start_time=None, end_time=None):
    result = submission_accounting(folder, phase, job_id, start_time, end_time)
    evidence = {'checked_at': datetime.now(timezone.utc).isoformat(), 'current_script': None,
                'capacity': None, 'logs': [], 'errors': []}
    evidence['task_files'] = task_evidence_files(folder, phase)
    if result.get('target_matched'):
        evidence['submission_command'] = submission_command_evidence(folder, result['accounting'][0], phase)
    else:
        evidence['submission_command'] = {'status': 'requires_unique_job', 'submission_phase_verified': False}
    result['submission_phase_verified'] = evidence['submission_command'].get('submission_phase_verified', False)
    result['execution_phase_verified'] = False
    findings, suggestions = [], []
    cache, failures = {}, {}
    def read(command):
        key = tuple(command)
        if key in failures:
            raise failures[key]
        if key not in cache:
            try:
                cache[key] = scheduler_read(command)
            except SubmissionError as exc:
                failures[key] = exc
                raise
        return cache[key]
    def assess(script):
        try:
            return {'status': 'compatible', 'details': capacity_preflight(script, read=read)}
        except SubmissionError as exc:
            return {'status': 'incompatible' if exc.error_code == 'SCHEDULER_CAPACITY_EXCEEDED' else 'unknown',
                    'code': exc.error_code, 'message': diagnostic_text(str(exc))}
    try:
        script = diagnostic_file(folder, 'submit.slurm')
        if script is not None:
            directives = dict(re.findall(r'^#SBATCH --([\w-]+)=([^\n]+)$', script, re.M))
            evidence['current_script'] = {'sha256': sha(folder / 'submit.slurm'),
                'resources': {k: diagnostic_text(v) for k, v in directives.items()
                              if k in ('partition', 'nodes', 'ntasks', 'cpus-per-task', 'time')},
                'historical_snapshot': False}
            evidence['capacity'] = assess(script)
            if evidence['capacity']['status'] == 'incompatible':
                findings.append({'code': 'CURRENT_RESOURCE_CAPACITY_CONFLICT', 'evidence': 'current_script + current_partition',
                                 'message': evidence['capacity']['message']})
            config_text = diagnostic_file(folder, 'config.json')
            config = json.loads(config_text) if config_text else {}
            if not isinstance(config, dict):
                raise ValueError('计算配置不是对象。')
            parallel, cores = config.get('parallel_tests'), config.get('cores_per_test')
            evidence['current_config_resources'] = {'parallel_tests': parallel, 'cores_per_test': cores}
            nodes = int(directives.get('nodes', '0'))
            if type(cores) is int and cores > 0 and nodes > 0 and cores % nodes:
                findings.append({'code': 'PER_TEST_NODE_DIVISIBILITY', 'evidence': 'current_config + current_script',
                                 'message': f'每组 {cores} MPI 进程不能在 {nodes} 节点均分。'})
            if (type(parallel) is int and type(cores) is int and parallel > 0 and 0 < cores <= 65536
                    and parallel * cores == int(directives.get('ntasks', '0'))
                    and (evidence['capacity']['status'] != 'compatible' or (nodes > 0 and cores % nodes))):
                for candidate in range(1, min(cores, 256) + 1):
                    if cores % candidate or candidate == nodes:
                        continue
                    candidate_script = re.sub(r'^#SBATCH --nodes=.*$', f'#SBATCH --nodes={candidate}', script, flags=re.M)
                    check = assess(candidate_script)
                    if check['status'] == 'compatible':
                        suggestions.append({'nodes': candidate, 'parallel_tests': parallel, 'cores_per_test': cores,
                                            'cpus_per_node': check['details']['cpus_per_node'],
                                            'default_memory_mb_per_node': check['details']['default_memory_mb_per_node']})
                        if len(suggestions) == 3:
                            break
    except (OSError, ValueError, TypeError, UnicodeError) as exc:
        evidence['errors'].append({'source': 'current_task_files', 'message': diagnostic_text(str(exc))})
    for row in (result.get('accounting') or [])[:3]:
        try:
            cpus, nodes = int(row.get('requested_cpus', '')), int(row.get('requested_nodes', ''))
            partition = row.get('partition', '')
            if cpus <= 0 or nodes <= 0 or cpus % nodes or not re.fullmatch(r'[A-Za-z0-9_-]+', partition):
                continue
            historical_script = f'#SBATCH --partition={partition}\n#SBATCH --nodes={nodes}\n#SBATCH --ntasks={cpus}\n'
            check = assess(historical_script)
            row['comparison_with_current_partition'] = check
            if check['status'] == 'incompatible':
                findings.append({'code': 'HISTORICAL_REQUEST_CURRENT_CAPACITY_CONFLICT', 'job_id': row['job_id'],
                    'evidence': 'sacct_requested_resources + current_partition', 'message': check['message'],
                    'historical_cause_proven': False})
        except (ValueError, TypeError):
            pass
    if result.get('target_matched'):
        row = result['accounting'][0]
        name = 'slurm-' + row['job_id'] + '.log'
        try:
            log = diagnostic_file(folder, name, tail=True)
            evidence['logs'].append({'path': name, 'exists': log is not None,
                                     'tail': diagnostic_text(log) if log is not None else None})
        except (OSError, ValueError, UnicodeError) as exc:
            evidence['errors'].append({'source': name, 'message': diagnostic_text(str(exc))})
    evidence['errors'].extend({'source': list(command), 'message': diagnostic_text(str(exc))}
                              for command, exc in failures.items())
    evidence['partition_queries'] = [{'command': list(command), 'output': diagnostic_text(output)}
                                     for command, output in cache.items()]
    return {**result, 'diagnosis': {'evidence': evidence, 'confirmed_configuration_findings': findings,
        'resource_layout_candidates': suggestions, 'historical_root_cause_confirmed': False,
        'limitations': ['当前分区规格不是历史配置快照；当前脚本不是历史提交脚本证据。',
                       '容量对比默认采用当前分区默认内存，历史ReqMem/ReqTRES单独列出，不能视为相同。',
                       '失败码和缺少日志不能单独证明VASP错误；查询失败应保留已取得的其他证据。'],
        'next_steps': ['先说明已有配置冲突，再说明历史根因的证据缺口，不因缺少控制器日志而忽略配置问题。',
                       '候选布局只验证当前CPU/默认内存和均分规则，不保证配额、可用性或性能；需操作者确认及重新审阅。',
                       '本工具不执行脚本、不修改资源或回执、不重交；历史精确错误可由管理员补查。']}}


OCCUPIED = 1e-6


def scf_occupation_audit(folder):
    """Highest-band occupation at every SCF k-point, read from the hash-verified vasprun.xml."""
    import numpy as np
    from pymatgen.io.vasp.outputs import Vasprun
    folder = Path(folder)
    scf = folder / 'scf'
    stamp = json.loads((scf / 'complete.json').read_text(encoding='utf-8'))
    if not stamp.get('xml_sha256') or engine_module('vasp_band_agent').digest(scf / 'vasprun.xml') != stamp['xml_sha256']:
        raise ValueError('scf/vasprun.xml 与完成记录不一致，不能作为占据证据。')
    summary = json.loads((folder / 'scf-summary.json').read_text(encoding='utf-8'))
    efermi = summary.get('efermi')
    run = Vasprun(scf / 'vasprun.xml', parse_dos=False, parse_projected_eigen=False, parse_potcar_file=False)
    channels = {}
    for spin, values in run.eigenvalues.items():
        values = np.asarray(values)  # (k-points, bands, [energy, occupation])
        occupation = values[:, :, 1]
        occupied = np.flatnonzero((occupation > OCCUPIED).any(axis=0))
        highest = int(occupied.max()) + 1 if occupied.size else 0
        channels[getattr(spin, 'name', str(spin))] = {
            'kpoints': int(values.shape[0]), 'nbands': int(values.shape[1]),
            'highest_band_max_occupation': float(occupation[:, -1].max()),
            'highest_occupied_band': highest, 'empty_bands_above': int(values.shape[1]) - highest,
            'top_band_min_minus_efermi_eV': None if efermi is None else round(float(values[:, -1, 0].min() - efermi), 4)}
    if not channels:
        raise ValueError('scf/vasprun.xml 缺少本征值及占据数据。')
    return {'passed': all(c['highest_band_max_occupation'] <= OCCUPIED for c in channels.values()),
            'threshold': OCCUPIED, 'channels': channels, 'efermi': efermi, 'nelect': summary.get('nelect'),
            'source': 'scf/vasprun.xml', 'xml_sha256': stamp['xml_sha256'],
            'scope': '覆盖 SCF 网格全部 k 点；能带路径的顶部空带在能带计算完成后另行校验。'}


def _status(payload, cfg, folder):
    info = {}
    def read_json(path):
        if path.is_symlink() or not path.resolve().is_relative_to(folder):
            raise ValueError('任务元数据路径越界或为符号链接。')
        if path.stat().st_size > 200_000:
            raise ValueError('任务元数据过大。')
        return json.loads(path.read_text(encoding='utf-8'))
    for filename in ('manifest.json', 'workflow-state.json', 'remote-submission.json', 'remote-job.json', 'ai-review.json', 'checkpoint.json', 'physics-model.json', 'report.json', 'convergence-array.json'):
        if (folder / filename).is_file():
            info[filename] = read_json(folder / filename)
    try:
        phases = task_phases(folder)
    except (ValueError, OSError) as exc:
        phases, info['stage_record_error'] = LEGACY_PHASES, diagnostic_text(str(exc))
    info['stage_submissions'] = {name: read_json(folder / 'submissions' / (name + '.json'))
                                 for name in phases
                                 if (folder / 'submissions' / (name + '.json')).is_file()}
    info['stage_reviews'] = {name: read_json(folder / ('review-' + name + '.json'))
                             for name in phases
                             if (folder / ('review-' + name + '.json')).is_file()}
    job_id = str(info.get('remote-submission.json', {}).get('job_id', ''))
    if re.fullmatch(r'\d+', job_id):
        array_receipt = info.get('remote-submission.json', {})
        is_array = array_receipt.get('submission_mode') == 'array'
        queue_command = ['squeue', '-h', '-j', job_id, '-o', '%i|%T|%M|%R'] + (['--array'] if is_array else [])
        queue = subprocess.run(queue_command, capture_output=True, text=True, timeout=15)
        info['scheduler_errors'] = []
        if queue.returncode:
            info['scheduler_errors'].append({'command': 'squeue', 'returncode': queue.returncode, 'stderr': queue.stderr[-2000:]})
        info['queue'] = queue.stdout.strip() if queue.returncode == 0 else None
        if not info['queue'] or is_array:
            account = subprocess.run(['sacct', '-n', '-j', job_id, '--format=JobID,State,ExitCode', '-P'], capture_output=True, text=True, timeout=15)
            if account.returncode:
                info['scheduler_errors'].append({'command': 'sacct', 'returncode': account.returncode, 'stderr': account.stderr[-2000:]})
            info['accounting'] = account.stdout[-4000:] if account.returncode == 0 else None
        info['scheduler_status_known'] = bool(info.get('queue')) or any(
            len(parts) >= 3 and parts[0] == job_id and bool(parts[1].strip())
            for parts in (line.split('|') for line in (info.get('accounting') or '').splitlines()))
        if is_array:
            summary = array_scheduler_status(array_receipt, info.get('queue'), info.get('accounting'))
            info['array_tasks'] = summary
            info['scheduler_status_known'] = summary['known']
            info['array_note'] = '数组元素分别排队；squeue -r 可展开。全部真实输出验收后仅放行下一阶段审阅，不自动提交。'
            config_path = folder / 'config.json'
            checkpoint = info.get('checkpoint.json') or {}
            array_config = json.loads(config_path.read_text(encoding='utf-8')) if config_path.exists() else {}
            if (summary['all_terminal'] and queue.returncode == 0
                    and array_config.get('submission_mode') == 'array'
                    and checkpoint.get('next', 'convergence') in ('convergence', 'relaxation')):
                try:
                    meta = info.get('remote-job.json', {})
                    task_release(meta)
                    result = engine_module('convergence_array').finalize_array(folder, job_id, scheduler_terminal=True, scheduler_evidence=summary)
                    info['array_collection'] = result
                    for filename in ('convergence-array.json', 'workflow-state.json', 'checkpoint.json'):
                        if (folder / filename).exists():
                            info[filename] = json.loads((folder / filename).read_text(encoding='utf-8'))
                except (ValueError, OSError, ImportError, KeyError) as exc:
                    info['array_collection_error'] = diagnostic_text(str(exc))
        if not info['scheduler_status_known']:
            info['scheduler_note'] = '调度状态未知：查询失败或没有记录，不代表作业完成；不要据此重复提交。'
        log = folder / f'slurm-{job_id}.log'
        if log.exists():
            with log.open('rb') as stream:
                stream.seek(max(0, log.stat().st_size - 12000))
                info['log_tail'] = stream.read().decode('utf-8', errors='replace')
        if is_array:
            info['array_log_tails'] = {}
            for item in info['array_tasks']['tasks']:
                log = folder / f'slurm-{item["job_id"]}.log'
                if log.is_file() and not log.is_symlink():
                    with log.open('rb') as stream:
                        stream.seek(max(0, log.stat().st_size - 2000))
                        info['array_log_tails'][item['job_id']] = diagnostic_text(stream.read().decode('utf-8', errors='replace'))
    return info
