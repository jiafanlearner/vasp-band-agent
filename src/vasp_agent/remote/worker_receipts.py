"""Submission receipts, the submission/recovery lock, and the recovery journal.

The protocol: a submit takes the advisory lock, writes and fsyncs a
submission_uncertain receipt with an exclusive create, releases the lock and only
then runs sbatch, so an interrupted submit can never be repeated automatically.
Recovery from an uncertain receipt is journaled under submission-recoveries/ and
the barrier refuses to configure or submit until that journal is complete.

Part of the stdin worker program (see remote_worker.WORKER_UNITS): standard library
and earlier units only.
"""
from datetime import datetime, timezone
import hashlib
import json
import os
import re
import subprocess

# <bundle:package-only>
from vasp_agent.remote.worker_common import SubmissionError, diagnostic_text, sha, write_json
from vasp_agent.remote.worker_slurm import array_layout, submission_accounting, submission_command_evidence
# </bundle:package-only>


def open_lock(path):
    """Open a lock file for nothing but locking. Never unlinked -- see below."""
    if path.is_symlink():
        raise ValueError('锁文件不能是符号链接。')
    stream = path.open('a+b')
    # msvcrt locks a byte range rather than the whole file, so the region has to
    # exist on disk; flock does not care, but both then lock offset 0 uniformly.
    if stream.seek(0, 2) == 0:
        stream.write(b'0')
        stream.flush()
    stream.seek(0)
    return stream


def try_lock(stream):
    """Take the exclusive lock if it is free. Never waits, never raises."""
    try:
        stream.seek(0)
        if os.name == 'nt':
            import msvcrt
            msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        return False
    return True


def lock_is_held(path):
    """True if some process currently holds an advisory lock on this path.

    Ownership lives in the open file description, not in the path, so a lock file
    left on disk by a killed process reads as free. Acquiring never waits, so a
    busy lock refuses immediately with a stated reason rather than hanging.
    """
    if not path.exists():
        return False
    try:
        stream = open_lock(path)
    except (OSError, ValueError):
        return True
    try:
        return not try_lock(stream)
    finally:
        stream.close()  # closing the description releases whatever it took


def submission_lock(folder, busy_message):
    """Take the submission/recovery lock for one job folder.

    An advisory OS lock rather than a path created with O_EXCL. The two look
    equivalent and are not: an O_EXCL marker outlives the process that made it,
    and this is exactly the section where the worker gets killed, because it
    claims the path and only then writes the receipt to disk. Dying in between
    left a path that blocked every later submit, while no receipt existed to
    short-circuit against -- and recover_submission takes this same lock, so the
    one documented way out of a bad submission was blocked by it too. The durable
    record of an interrupted recovery is the journal under submission-recoveries/,
    which recovery_barrier reads on its own. A lock is not evidence, and must not
    be able to condemn a job permanently.

    The file itself is deliberately never unlinked, matching job_monitor.FileLock.
    Removing it on release would break mutual exclusion rather than tidy up: a
    second process that opened the path before the unlink keeps a lock on the
    orphaned inode, while a third creates a fresh file and locks that, and both
    then believe they hold it.
    """
    stream = open_lock(folder / '.submission-recovery.lock')  # ValueError on symlink
    if not try_lock(stream):
        stream.close()
        raise ValueError(busy_message)
    return stream


def submit_with_receipt(folder, record, phase, staged, capacity):
    """Exclusive reservation plus durable outcome; uncertain outcomes never requeue."""
    # The session travels with the phase so the job's own events.jsonl, written
    # on the compute node, can be tied back to the conversation that submitted
    # it. Both ride the same --export argument, whose parser below already
    # understands comma-separated NAME=value items.
    exports = [item for item in (f'VASP_AGENT_PHASE={phase}' if staged else None,
                                 f'VASP_AGENT_SESSION={os.environ["VASP_AGENT_SESSION"]}'
                                 if re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_-]{0,63}',
                                                 os.environ.get('VASP_AGENT_SESSION', '')) else None)
               if item]
    command = ['sbatch', '--parsable'] + ([f'--export=ALL,{",".join(exports)}'] if exports else []) + ['submit.slurm']
    info = {'status': 'submission_uncertain', 'job_id': None, 'phase': phase,
            'submission_diagnostic': {'started_at': datetime.now(timezone.utc).isoformat(),
                'command': command, 'capacity': capacity}}
    layout = array_layout((folder / 'submit.slurm').read_text(encoding='utf-8'))
    if layout:
        if not staged or phase != 'convergence':
            raise ValueError('数组仅用于阶段式 convergence 收敛任务。')
        info.update(submission_mode='array', array_task_count=layout['task_count'],
                    array_concurrency=layout['concurrency'], array_task_ids=layout['task_ids'])
    record.parent.mkdir(exist_ok=True)
    # Released only after the receipt is durable. The lock orders concurrent
    # submissions; the 'x' open below is what actually makes a double submit
    # impossible, and it is checked again by the existing receipt on replays.
    reservation_lock = submission_lock(folder, '提交或恢复正在登记，禁止并行操作。')
    try:
        recovery_barrier(folder, phase, require_new_review=True, check_lock=False)
        with record.open('x', encoding='utf-8') as stream:
            json.dump(info, stream, ensure_ascii=False)
            stream.flush()
            os.fsync(stream.fileno())
    finally:
        reservation_lock.close()
    env = {k: v for k, v in os.environ.items() if not k.startswith('SBATCH_')}
    if staged:
        env['VASP_AGENT_PHASE'] = phase
    try:
        result = subprocess.run(command, cwd=folder, env=env, capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.TimeoutExpired) as exc:
        info['submission_diagnostic'].update(error_type=type(exc).__name__,
            stdout=diagnostic_text(getattr(exc, 'stdout', '')), stderr=diagnostic_text(getattr(exc, 'stderr', '')),
            returncode=None, finished_at=datetime.now(timezone.utc).isoformat())
        write_json(record, info)
        raise SubmissionError('SUBMISSION_UNCERTAIN', 'sbatch 执行异常，诊断已保存；提交状态不明确，禁止重复提交。') from exc
    info['submission_diagnostic'].update(returncode=result.returncode,
        stdout=diagnostic_text(result.stdout), stderr=diagnostic_text(result.stderr),
        finished_at=datetime.now(timezone.utc).isoformat())
    match = re.fullmatch(r'([0-9]+)(?:;[A-Za-z0-9_.-]+)?', result.stdout.strip())
    if result.returncode or not match:
        info['submission_diagnostic']['reason'] = 'nonzero_exit' if result.returncode else 'unrecognized_stdout'
        write_json(record, info)
        raise SubmissionError('SUBMISSION_UNCERTAIN', 'sbatch 返回异常或JobID无法解析；已保存返回码和脱敏输出，禁止自动重交。')
    info.update(status='submitted', job_id=match[1])
    write_json(record, info)
    if staged:
        write_json(folder / 'remote-submission.json', info)
    return info


def recovery_path(folder, relative):
    path = folder / relative
    cursor = path
    while cursor != folder:
        if cursor.is_symlink():
            raise ValueError('恢复记录路径不允许链接。')
        cursor = cursor.parent
    if not path.resolve().is_relative_to(folder.resolve()):
        raise ValueError('恢复路径越界。')
    if path.exists() and (not path.is_file() or path.stat().st_nlink != 1 or path.stat().st_size > 1000000):
        raise ValueError('恢复证据必须为限长非硬链接普通文件。')
    return path


def recovery_context(folder, phase, job_id):
    # First release intentionally supports only the legacy, pre-start initial stage.
    if phase != 'convergence' or not re.fullmatch(r'[0-9]+', str(job_id)):
        raise ValueError('当前恢复仅支持明确JobID的初始convergence阶段。')
    if (folder / '.agent.lock').exists() or (folder / '.task-archived.json').exists():
        raise ValueError('运行中或归档任务不能恢复。')
    record = recovery_path(folder, 'submissions/convergence.json')
    receipt = json.loads(record.read_text(encoding='utf-8'))
    if receipt.get('status') != 'submission_uncertain' or receipt.get('job_id') is not None:
        raise ValueError('仅处理JobID为空的不确定回执。')
    for path in (folder / 'submissions').glob('*.json'):
        if path != record:
            raise ValueError('存在其他阶段提交回执，需另行核查，当前不放行。')
    if (folder / 'remote-submission.json').exists() or (folder / 'submission.json').exists():
        raise ValueError('存在其他提交记录，当前不放行。')
    state = json.loads(recovery_path(folder, 'workflow-state.json').read_text(encoding='utf-8'))
    if state.get('status') != 'prepared' or state.get('next') != phase:
        raise ValueError('仅支持仍处于prepared/convergence的未启动任务。')
    account = submission_accounting(folder, phase, job_id)
    if not account.get('target_matched'):
        raise ValueError('历史作业未唯一匹配目标。')
    row = account['accounting'][0]
    if row.get('state') != 'FAILED' or row.get('allocated_nodes') != '0' or row.get('elapsed') != '00:00:00':
        raise ValueError('首版仅支持FAILED、零分配节点且零运行时长的作业；其他状态需单独恢复流程。')
    command = submission_command_evidence(folder, row, phase)
    if not command.get('submission_phase_verified'):
        raise ValueError('历史提交命令未核实目标阶段。')
    # Window discovery is separate from the explicit ID query; ambiguity blocks.
    discovered = submission_accounting(folder, phase)
    if (not discovered.get('target_matched') or discovered.get('discovered_job_id') != str(job_id)):
        raise ValueError('回执时间窗内存在其他候选或历史记录不完整，不能恢复。')
    queued = subprocess.run(['squeue', '--me', '-h', '-o', '%i|%j|%Z'], capture_output=True, text=True, timeout=20)
    if queued.returncode or len(queued.stdout) > 1000000:
        raise ValueError('当前队列查询失败，不能排除重复计算。')
    for line in queued.stdout.splitlines():
        if not line.strip():
            continue
        fields = line.strip().split('|')
        if len(fields) != 3:
            raise ValueError('当前队列格式不明确。')
        if fields[0] == str(job_id) or fields[1] == 'va-' + folder.name or fields[2] == str(folder):
            raise ValueError('当前队列存在同任务或同名作业，停止恢复。')
    hashes = {}
    for name in ('submissions/convergence.json', 'workflow-state.json', 'manifest.json', 'config.json',
                 'submit.slurm', 'review-convergence.json', 'physics-model.json', 'remote-job.json'):
        path = recovery_path(folder, name)
        hashes[name] = sha(path) if path.exists() else None
    evidence = {'phase': phase, 'job_id': str(job_id), 'hashes': hashes, 'accounting': row,
                'submission_command': command, 'search_window': discovered.get('search_window'),
                'queue_checked_empty_for_target': True}
    snapshot = hashlib.sha256(json.dumps(evidence, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
    return {'eligible': True, 'snapshot': snapshot, 'evidence': evidence,
            'attempt_binding_verified': False, 'operator_accepts_legacy_gap_required': True,
            'note': '仅确认人工对账依据；旧回执没有唯一尝试标识。恢复不代表根因查明、不授权重试或提交。'}


def recover_submission(folder, phase, job_id, snapshot, operation_id, operator_confirmed=False):
    if phase != 'convergence' or not re.fullmatch(r'[0-9]+', str(job_id)):
        raise ValueError('恢复目标无效。')
    if not operator_confirmed or not re.fullmatch(r'[a-f0-9]{32}', operation_id):
        raise ValueError('恢复必须由本地操作者明确确认并提供操作编号。')
    root = folder / 'submission-recoveries'
    if root.is_symlink():
        raise ValueError('恢复目录不能是链接。')
    root.mkdir(exist_ok=True)
    journal = recovery_path(folder, 'submission-recoveries/' + operation_id + '.json')
    archive = recovery_path(folder, 'submission-recoveries/' + operation_id + '.original.json')
    record = recovery_path(folder, 'submissions/convergence.json')
    # Previously an O_EXCL marker, which meant the repair action could not run
    # while a stale marker from the very failure it exists to repair was present.
    lock = submission_lock(folder, '另一恢复正在进行；不得并行提交。')
    try:
        if journal.exists():
            saved = json.loads(journal.read_text(encoding='utf-8'))
            if (saved['snapshot'], saved['job_id'], saved['phase']) != (snapshot, str(job_id), phase):
                raise ValueError('恢复操作ID冲突。')
            if archive.exists() and sha(archive) == saved['original_sha256']:
                if saved['status'] != 'recovered':
                    saved['status'] = 'recovered'
                    write_json(journal, saved)
                return saved
            if archive.exists():
                raise ValueError('恢复归档哈希不匹配，保持阻断。')
            current = recovery_context(folder, phase, job_id)
            if current['snapshot'] != snapshot or not record.exists() or sha(record) != saved['original_sha256']:
                raise ValueError('恢复意图与当前证据不一致，保持阻断。')
            os.replace(record, archive)
            saved['status'] = 'recovered'
            write_json(journal, saved)
            return saved
        current = recovery_context(folder, phase, job_id)
        if current['snapshot'] != snapshot:
            raise ValueError('回执、任务文件或调度证据已变化，请重新预览确认。')
        saved = {'operation_id': operation_id, 'phase': phase, 'job_id': str(job_id), 'snapshot': snapshot,
                 'status': 'recovery_pending', 'original_sha256': sha(record), 'evidence': current['evidence'],
                 'confirmed_by': 'operator_menu_legacy_gap_accepted', 'attempt_binding_verified': False,
                 'submission_authorized': False, 'time': datetime.now(timezone.utc).isoformat()}
        with journal.open('x', encoding='utf-8') as stream:
            json.dump(saved, stream, ensure_ascii=False)
            stream.flush()
            os.fsync(stream.fileno())
        if sha(record) != saved['original_sha256']:
            raise ValueError('归档前回执已改变，恢复停止。')
        os.replace(record, archive)
        saved['status'] = 'recovered'
        write_json(journal, saved)
        return saved
    finally:
        lock.close()


def recovery_barrier(folder, phase, require_new_review=False, check_lock=True):
    # The lock file is never unlinked, so its mere presence says nothing -- ask
    # whether anyone actually holds it. A file left by a killed worker must not
    # block configuring or submitting, and it must especially not block
    # recover_submission, which is the documented way out of a bad submit.
    if check_lock and lock_is_held(folder / '.submission-recovery.lock'):
        raise ValueError('恢复进行中，暂不能配置或提交。')
    for path in (folder / 'submission-recoveries').glob('*.json'):
        if path.name.endswith('.original.json'):
            continue
        safe = recovery_path(folder, 'submission-recoveries/' + path.name)
        saved = json.loads(safe.read_text(encoding='utf-8'))
        if saved.get('phase') != phase:
            continue
        if saved.get('status') != 'recovered':
            raise ValueError('恢复记录未完成，不能提交。')
        if not re.fullmatch(r'[a-f0-9]{32}', str(saved.get('operation_id', ''))):
            raise ValueError('恢复记录编号无效。')
        archive = recovery_path(folder, 'submission-recoveries/' + saved['operation_id'] + '.original.json')
        if not archive.exists() or sha(archive) != saved['original_sha256']:
            raise ValueError('恢复归档证据缺失或改变。')
        if require_new_review:
            review = folder / 'review-convergence.json'
            if not review.exists() or sha(review) == saved['evidence']['hashes']['review-convergence.json']:
                raise ValueError('恢复后必须重新审阅当前输入和资源，旧审阅不能放行重试。')
