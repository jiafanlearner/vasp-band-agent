"""Job cancellation, cancelled receipt release, and interrupted-phase resume.

Part of the stdin worker program (see remote_worker.WORKER_UNITS): standard library only.
"""
from datetime import datetime, timezone
import json
import os
import re
import shutil
import subprocess

# <bundle:package-only>
from vasp_agent.remote.worker_common import (
    LEGACY_PHASES, SubmissionError, diagnostic_text, file_sha, task_phases, write_json)
from vasp_agent.remote.worker_receipts import submission_lock
from vasp_agent.remote.worker_env import engine_module, stage_definitions, task_config, task_release
from vasp_agent.remote.worker_tasks import scheduler_query
# </bundle:package-only>


PHASES = LEGACY_PHASES
# Files a phase writes at the task root once its run has started. Their presence
# means the cancelled job did real work, which must never be silently reused.
PHASE_OUTPUTS = {'convergence': ('convergence.json', 'convergence-array.json'),
                 'relaxation': ('relaxation.json', 'relaxation-diagnostic.json', 'relaxed-1.POSCAR', 'relaxed-2.POSCAR',
                                'geometry-summary.json', 'post-relax-validation.json'),
                 'scf': ('scf-summary.json',),
                 'bands': ('report.json', 'bands.csv', 'bands.png', 'bands.pdf')}


def phase_outputs(folder, phase):
    config = task_config(folder)
    definitions = stage_definitions(config)
    return PHASE_OUTPUTS[phase] if definitions is None else definitions.phase_outputs(config, phase)


def kept_stage_inputs(folder, phase):
    """Inputs a stage folder of phase receives besides STAGE_INPUTS (bands: the SCF charge density)."""
    config = task_config(folder)
    definitions = stage_definitions(config)
    if definitions is None:
        return ('CHGCAR',) if phase == 'bands' else ()
    return definitions.kept_inputs(config, phase)


def submitted_receipt(folder, phase):
    """The phase's registered receipt and its strict JobID, or a refusal."""
    manifest = folder / 'manifest.json'
    staged = json.loads(manifest.read_text()).get('stage_gates', False) if manifest.is_file() else False
    if phase is None and staged:
        checkpoint = folder / 'checkpoint.json'
        phase = json.loads(checkpoint.read_text())['next'] if checkpoint.is_file() else task_phases(folder)[0]
    if phase is not None and phase not in task_phases(folder):
        raise ValueError('无效阶段。')
    if staged != (phase is not None):
        raise ValueError('阶段式任务需指定阶段；旧任务不带阶段。')
    record = folder / 'submissions' / (phase + '.json') if phase else folder / 'remote-submission.json'
    if record.is_symlink() or not record.is_file():
        raise ValueError(f'阶段 {phase or "完整流程"} 没有提交回执，没有可取消的作业。')
    receipt = json.loads(record.read_text(encoding='utf-8'))
    job_id = str(receipt.get('job_id') or '')
    if not re.fullmatch(r'[0-9]+', job_id):
        raise ValueError('提交回执没有明确的 JobID；不取消、不释放，请人工核对调度记录。')
    return phase, record, receipt, job_id


def record_event(folder, action, **details):
    task_release(json.loads((folder / 'remote-job.json').read_text()))
    engine_module('vasp_band_agent').event(folder, action, **details)


def cancel_job(folder, phase=None, confirm_job_id=None):
    """scancel the JobID a receipt names. Without the typed JobID it only previews."""
    phase, _, receipt, job_id = submitted_receipt(folder, phase)
    preview = {'phase': phase, 'job_id': job_id, 'receipt_status': receipt.get('status'),
               'submission_mode': receipt.get('submission_mode', 'allocation')}
    if confirm_job_id is None:
        return {**preview, 'preview': True, 'cancel_requested': False}
    if str(confirm_job_id) != job_id:
        raise ValueError('输入的作业编号与提交回执不一致，未取消。')
    try:
        result = subprocess.run(['scancel', job_id], capture_output=True, text=True, timeout=30)
    except subprocess.TimeoutExpired as exc:
        raise SubmissionError('CANCEL_UNCERTAIN', 'scancel 超时；取消是否生效未知，请用 /jobs 或任务状态核对，不要据此重新提交。') from exc
    except OSError as exc:
        raise ValueError(f'无法执行 scancel：{exc}') from exc
    if result.returncode:
        detail = diagnostic_text(result.stderr or result.stdout).strip()
        raise ValueError(f'scancel 失败（退出码 {result.returncode}）：{detail or "未返回诊断信息"}')
    record_event(folder, 'cancellation_requested', job_id=job_id, phase=phase)
    return {**preview, 'cancel_requested': True, 'stderr': diagnostic_text(result.stderr).strip(),
            'next': '已发送取消请求；作业退出且记账为 CANCELLED 后，可由操作者释放该阶段提交记录并重新确认资源与授权。'}


def started_outputs(folder, phase, stage_gate):
    """Paths showing the phase already ran: anything beyond its prepared inputs."""
    prepared = set(json.loads((folder / 'manifest.json').read_text()).get('hashes', {}))
    plan = json.loads((folder / 'input-plan.json').read_text(encoding='utf-8'))
    found = [name for name in phase_outputs(folder, phase) if (folder / name).exists()]
    for name in plan['incars']:
        if stage_gate.phase_of(name) != phase:
            continue
        stage = folder / name
        if stage.is_symlink():
            found.append(name)
        elif stage.is_dir():
            for path in sorted(stage.rglob('*')):
                relative = path.relative_to(folder).as_posix()
                if not path.is_dir() and relative not in prepared:
                    found.append(relative)
    state = folder / 'workflow-state.json'
    if state.is_file() and json.loads(state.read_text(encoding='utf-8')).get('status') in ('running', 'failed'):
        found.append('workflow-state.json')
    return found


def release_cancelled_submission(folder, phase, confirm_job_id):
    """Archive a phase receipt whose job Slurm records as CANCELLED, unlocking the phase."""
    if phase not in task_phases(folder):
        raise ValueError('释放提交记录需要明确的阶段。')
    phase, record, receipt, job_id = submitted_receipt(folder, phase)
    if str(confirm_job_id) != job_id:
        raise ValueError('输入的作业编号与提交回执不一致，未释放。')
    if receipt.get('status') != 'submitted' or receipt.get('submission_mode') == 'array':
        raise ValueError('仅支持已确认 JobID 的普通（非数组）提交；其他记录请新建任务。')
    if (folder / '.task-archived.json').exists():
        raise ValueError('任务已归档，请先恢复。')
    task_release(json.loads((folder / 'remote-job.json').read_text()))
    stage_gate = engine_module('stage_gate')
    if stage_gate.current(folder) != phase:
        raise ValueError('该阶段已不是当前待执行阶段（可能已完成），不释放提交记录。')
    archived = folder / 'submissions' / f'{phase}.cancelled-{job_id}.json'
    lock = submission_lock(folder, '提交或恢复正在登记，禁止并行操作。')
    try:
        if archived.exists():
            raise ValueError('已有同名取消归档，不覆盖。')
        consequence = '未确认作业已取消退出，不释放提交记录'
        queued = scheduler_query(['squeue', '--me', '-h', '-o', '%i'], consequence)
        if any(line.strip() == job_id or line.strip().startswith(job_id + '_') for line in queued.splitlines()):
            raise ValueError('作业仍在队列中（可能仍在排队、运行或正在退出），不释放；请稍后再试。')
        accounting = scheduler_query(['sacct', '-n', '-j', job_id, '--format=JobIDRaw,State,ExitCode', '-P'], consequence)
        states = [row[1].strip() for row in (line.split('|') for line in accounting.splitlines())
                  if len(row) >= 3 and row[0].strip() == job_id]
        if len(states) != 1 or states[0].split()[:1] != ['CANCELLED']:
            raise ValueError(f'调度记账未显示作业 {job_id} 为 CANCELLED（记录：{", ".join(states) or "无"}）；'
                             '只有确认取消的作业才能释放，完成、失败或运行中的作业不释放。')
        if (folder / '.agent.lock').exists():
            raise ValueError('任务目录留有运行锁，说明被取消的作业已开始计算；部分输出未经验证，'
                             '不能在本任务中重算。请基于已通过的阶段新建任务（fork）后按新资源提交。')
        partial = started_outputs(folder, phase, stage_gate)
        if partial:
            raise ValueError('被取消的作业已留下本阶段输出（' + ', '.join(partial[:8]) +
                             (' 等' if len(partial) > 8 else '') + '）；部分输出未经验证，不能静默复用或覆盖。'
                             '请基于已通过的阶段新建任务（fork）后按新资源提交。')
        os.replace(record, archived)
    finally:
        lock.close()
    relative = archived.relative_to(folder).as_posix()
    record_event(folder, 'cancelled_submission_released', job_id=job_id, phase=phase,
                 archived_receipt=relative, scheduler_state=states[0])
    return {'released': True, 'phase': phase, 'job_id': job_id, 'scheduler_state': states[0],
            'archived_receipt': relative, 'submission_authorized': False,
            'next': '提交记录已归档；该阶段需重新确认资源、按新快照重新审阅，并通过操作者菜单重新授权后才能提交。'}


# Slurm end states after which a phase may continue from its completed stages.
RESUMABLE_STATES = ('TIMEOUT', 'CANCELLED', 'NODE_FAIL', 'PREEMPTED')
PHASE_RESUME_VERSION = 1
STAGE_INPUTS = ('POSCAR', 'INCAR', 'KPOINTS', 'POTCAR', 'inputs.json', 'execution.json')


def generated_at_start(plan, name):
    """A stage whose INCAR is generated at its start from verified evidence (dos), not listed in the plan."""
    entry = plan.get('stage_parameters', {}).get(name)
    return name not in plan['incars'] and isinstance(entry, dict) and entry.get('generate_at') == 'stage_start'


def phase_stage_folders(plan, phase, phase_of):
    """Stage folders of phase: the plan's INCAR entries, or the phase folder of a stage generated at start."""
    names = [name for name in plan['incars'] if phase_of(name) == phase]
    return names or ([phase] if generated_at_start(plan, phase) else [])


def verified_stage(folder, name, plan, config):
    """Hashes of a completed stage, after checking it against its record and the reviewed plan."""
    from pymatgen.io.vasp.inputs import Incar
    stage = folder / name
    stamp = json.loads((stage / 'complete.json').read_text(encoding='utf-8'))
    if not stamp.get('xml_sha256') or not (stage / 'vasprun.xml').is_file() or file_sha(stage / 'vasprun.xml') != stamp['xml_sha256']:
        raise ValueError(f'{name} 的输出与完成记录不一致，不能复用；请人工检查，不续算。')
    if name == 'scf' and (not stamp.get('charge_sha256') or not (stage / 'CHGCAR').is_file()
                          or file_sha(stage / 'CHGCAR') != stamp['charge_sha256']):
        raise ValueError('scf 电荷文件与完成记录不一致，不能复用。')
    if not (stage / 'inputs.json').is_file():
        raise ValueError(f'{name} 缺少 inputs.json，无法核对输入，不能复用。')
    inputs = json.loads((stage / 'inputs.json').read_text(encoding='utf-8'))
    for filename, expected in inputs.items():
        if not (stage / filename).is_file() or file_sha(stage / filename) != expected:
            raise ValueError(f'{name}/{filename} 与 inputs.json 不一致，不能复用。')
    if file_sha(stage / 'POTCAR') != file_sha(folder / 'POTCAR'):
        raise ValueError(f'{name}/POTCAR 与任务 POTCAR 不一致，不能复用。')
    data = dict(Incar.from_file(stage / 'INCAR'))
    tags = {key: data.pop(key) for key in ('KPAR', 'NCORE') if key in data}
    # A stage generated at its start has no plan entry: inputs.json above binds its
    # files, and the engine regenerates and compares them byte for byte at its run.
    if not generated_at_start(plan, name) and (name not in plan['incars'] or data != dict(Incar(plan['incars'][name]))):
        raise ValueError(f'{name} 的 INCAR 与审阅方案不一致，不能复用。')
    if tags and (config.get('execution_tags') is None or not (stage / 'execution.json').is_file()
                 or json.loads((stage / 'execution.json').read_text(encoding='utf-8')).get('tags') != tags):
        raise ValueError(f'{name} 的并行标签与执行记录不一致，不能复用。')
    recorded = plan.get('kpoints', {}).get(name)
    if recorded is not None and (stage / 'KPOINTS').read_text(encoding='utf-8') != recorded:
        raise ValueError(f'{name} 的 KPOINTS 与审阅方案不一致，不能复用。')
    names = {'complete.json', 'vasprun.xml', 'inputs.json', *inputs}
    names |= {'execution.json'} if (stage / 'execution.json').is_file() else set()
    names |= {'CHGCAR'} if name == 'scf' else set()
    return {filename: file_sha(stage / filename) for filename in sorted(names)}


def check_resume(folder):
    """A resumed phase's reused stages still hash to what the operator confirmed."""
    path = folder / 'checkpoint.json'
    if not path.is_file():
        return None
    checkpoint = json.loads(path.read_text(encoding='utf-8'))
    resume = checkpoint.get('resume')
    if not resume or resume.get('phase') != checkpoint.get('next'):
        return None
    for name, hashes in resume['reused'].items():
        for filename, expected in hashes.items():
            target = folder / name / filename
            if target.is_symlink() or not target.is_file() or file_sha(target) != expected:
                raise ValueError(f'续算复用的 {name}/{filename} 在续算确认后发生变化；请人工检查，不能审阅或提交。')
    return resume


def prepare_phase_resume(folder, phase, confirm_job_id=None):
    """Resume a phase whose job Slurm ended early, reusing every verified stage."""
    if phase not in task_phases(folder):
        raise ValueError('续算需要明确的阶段。')
    phase, record, receipt, job_id = submitted_receipt(folder, phase)
    if confirm_job_id is not None and str(confirm_job_id) != job_id:
        raise ValueError('输入的作业编号与提交回执不一致，未续算。')
    if receipt.get('status') != 'submitted' or receipt.get('submission_mode') == 'array':
        raise ValueError('仅支持已确认 JobID 的普通（非数组）提交；其他记录请人工核对。')
    if (folder / '.task-archived.json').exists():
        raise ValueError('任务已归档，请先恢复。')
    task_release(json.loads((folder / 'remote-job.json').read_text()))
    stage_gate = engine_module('stage_gate')
    if stage_gate.current(folder) != phase:
        raise ValueError('该阶段已不是当前待执行阶段（可能已完成），不续算。')
    lock = submission_lock(folder, '提交或恢复正在登记，禁止并行操作。')
    try:
        consequence = '未确认作业已结束，不续算'
        queued = scheduler_query(['squeue', '--me', '-h', '-o', '%i'], consequence)
        live = {line.strip().split('_')[0] for line in queued.splitlines() if line.strip()}
        if job_id in live:
            raise ValueError('作业仍在队列中（可能仍在排队、运行或正在退出），不续算；请稍后再试。')
        for other in sorted((folder / 'submissions').glob('*.json')):
            other_id = str(json.loads(other.read_text(encoding='utf-8')).get('job_id') or '')
            if other_id in live:
                raise ValueError(f'该任务的另一个作业 {other_id} 仍在队列中，不续算。')
        accounting = scheduler_query(['sacct', '-n', '-j', job_id, '--format=JobIDRaw,State,ExitCode', '-P'], consequence)
        states = [row[1].strip() for row in (line.split('|') for line in accounting.splitlines())
                  if len(row) >= 3 and row[0].strip() == job_id]
        state = states[0].split()[0] if len(states) == 1 and states[0].split() else None
        if state not in RESUMABLE_STATES:
            raise ValueError(f'调度记账未显示作业 {job_id} 为 {"/".join(RESUMABLE_STATES)} 之一（记录：{", ".join(states) or "无"}）；'
                             '只有被时限、取消、节点故障或抢占中断的作业才能续算。COMPLETED 无需续算，FAILED 需先诊断。')
        state_path = folder / 'workflow-state.json'
        workflow = json.loads(state_path.read_text(encoding='utf-8')) if state_path.is_file() else {}
        if workflow.get('status') == 'failed':
            raise ValueError('工作流记录为 failed（' + str(workflow.get('error', ''))[:300] + '）；失败需先诊断，不续算。')
        archive = folder / 'interrupted' / job_id
        archived_receipt = folder / 'submissions' / f'{phase}.{state.lower()}-{job_id}.json'
        if archive.exists() or archive.is_symlink() or archived_receipt.exists():
            raise ValueError(f'已有该作业的中断归档（{archive.relative_to(folder).as_posix()}），不覆盖；请人工检查。')
        checkpoint_path = folder / 'checkpoint.json'
        checkpoint = json.loads(checkpoint_path.read_text(encoding='utf-8')) if checkpoint_path.is_file() else {'next': phase}
        config = json.loads((folder / 'config.json').read_text(encoding='utf-8'))
        plan = json.loads((folder / 'input-plan.json').read_text(encoding='utf-8'))
        outputs, kept_extra = phase_outputs(folder, phase), kept_stage_inputs(folder, phase)
        reused, rerun, moves = {}, [], []
        for name in phase_stage_folders(plan, phase, stage_gate.phase_of):
            stage = folder / name
            if stage.is_symlink():
                raise ValueError(f'{name} 是链接，不续算。')
            if (stage / 'complete.json').is_file():
                reused[name] = verified_stage(folder, name, plan, config)
                continue
            rerun.append(name)
            if not stage.is_dir():
                continue
            if (stage / 'inputs.json').is_file():
                for filename, expected in json.loads((stage / 'inputs.json').read_text(encoding='utf-8')).items():
                    if not (stage / filename).is_file() or file_sha(stage / filename) != expected:
                        raise ValueError(f'{name}/{filename} 与 inputs.json 不一致；中断阶段的输入已改变，不续算。')
            kept = STAGE_INPUTS + kept_extra
            moves += [(path, archive / name / path.name) for path in sorted(stage.iterdir()) if path.name not in kept]
        moves += [(folder / path, archive / 'root' / path) for path in outputs if (folder / path).exists()]
        upstream = {key: value for key, value in stage_gate.evidence(folder, phase)['hashes'].items()
                    if key not in outputs}
        if checkpoint.get('evidence_hashes', {}) != upstream:
            raise ValueError('上游阶段证据与检查点记录不一致；请人工检查，不续算。')
        lock_file = folder / '.agent.lock'
        stale_lock = lock_file.exists() or lock_file.is_symlink()
        summary = {'phase': phase, 'job_id': job_id, 'scheduler_state': state, 'reused': sorted(reused),
                   'rerun': rerun, 'archive': archive.relative_to(folder).as_posix(),
                   'archived': sorted(target.relative_to(archive).as_posix() for _, target in moves),
                   'stale_lock': stale_lock, 'resume_version': PHASE_RESUME_VERSION,
                   'reuse_checked_by': ['worker', 'engine'] if getattr(stage_gate, 'RESUME_VERSION', 0) >= 1 else ['worker'],
                   'submission_authorized': False}
        if confirm_job_id is None:
            return {**summary, 'preview': True, 'prepared': False}
        archive.mkdir(parents=True)
        for source, target in moves:
            target.parent.mkdir(parents=True, exist_ok=True)
            os.replace(source, target)
        if stale_lock:
            os.replace(lock_file, archive / '.agent.lock')
        if state_path.is_file():
            shutil.copy2(state_path, archive / 'workflow-state.json')
        resume = {'version': PHASE_RESUME_VERSION, 'phase': phase, 'job_id': job_id, 'scheduler_state': state,
                  'archive': summary['archive'], 'reused': reused, 'rerun': rerun,
                  'stale_lock_archived': stale_lock, 'time': datetime.now(timezone.utc).isoformat()}
        write_json(archive / 'resume.json', resume)
        write_json(checkpoint_path, {**checkpoint, 'evidence_hashes': checkpoint.get('evidence_hashes', {}), 'resume': resume})
        write_json(state_path, {'status': 'awaiting_review', 'completed': checkpoint.get('completed', []),
                                'next': phase, 'resumed_from_job': job_id})
        os.replace(record, archived_receipt)
    finally:
        lock.close()
    relative = archived_receipt.relative_to(folder).as_posix()
    record_event(folder, 'phase_resume_prepared', job_id=job_id, phase=phase, scheduler_state=state,
                 reused=sorted(reused), rerun=rerun, archive=summary['archive'],
                 archived_receipt=relative, stale_lock_archived=stale_lock)
    return {**summary, 'prepared': True, 'archived_receipt': relative,
            'next': '提交记录已归档；本阶段需重新审阅（快照包含复用阶段的哈希），重新确认资源（可改时限）并经操作者重新授权后才能提交。'}
