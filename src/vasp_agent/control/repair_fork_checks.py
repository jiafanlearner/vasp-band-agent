"""Read-only diagnostic, eligibility, status, provenance, and contract-binding checks for P1e-3."""
from __future__ import annotations

import json
from pathlib import Path, PurePosixPath
import re
import shutil
import subprocess
from typing import Any

from vasp_agent.chat.toolbox_core import scrub
from vasp_agent.control import execution_reconcile as rec
from vasp_agent.control import scan_retry_checks
from vasp_agent.permissions.contract_rules import ContractError
from vasp_agent.workflow import input_review, stage_gate, workflow_policy
import vasp_agent.workflow.vasp_band_agent as core

REPAIR_STRATEGIES = ('fresh_restart', 'production_from_convergence', 'bands_from_scf', 'dos_from_scf')
STRATEGY_TARGET_PHASE = {
    'fresh_restart': 'convergence',
    'production_from_convergence': 'relaxation',
    'bands_from_scf': 'bands',
    'dos_from_scf': 'dos',
}
FAILURE_CATEGORIES = (
    'array_failure',
    'oom',
    'scheduler_failed',
    'capacity_conflict',
    'node_divisibility',
    'walltime_exceeded',
    'vasp_crash',
    'empty_bands_insufficient',
    'transient_failure',
    'incar_numerical_unsupported',
)
_PREFLIGHT_ONLY_CATEGORIES = frozenset({'capacity_conflict', 'node_divisibility'})
_HEX64_RE = re.compile(r'^[a-f0-9]{64}$')
_PRESET_RANK = {'standard': 0, 'strict': 1}
_DEFAULT_THRESHOLDS = {'energy_eV_per_atom': 0.001, 'force_eV_per_A': 0.005, 'stress_kbar': 0.5}
_LOCAL_META_FILES = (
    'manifest.json', 'config.json', 'initial.POSCAR', 'POTCAR', 'input-plan.json',
    'environment-review.json', 'submit.slurm', 'parameter-choice.json', 'potential-selection.json',
    'production-choice.json', 'convergence-reuse.json', 'scan-verification.json',
    'bands-path-choice.json', 'dos-fork.json', 'scf-summary.json',
)
_FORK_DRIFT_MARKERS = (
    '来源 SCF 完成后证据文件发生变化',
    '复制期间来源证据发生变化',
    '准备期间来源证据发生变化',
    '准备期间来源快照发生变化',
    '物理模型确认后 initial.POSCAR 已改变',
    '交叉复核模型与声明不一致',
    '物理模型记录的维度',
)
_INELIGIBLE_MARKERS = (
    '来源已生成后续输入或输出',
    '来源已有后续阶段提交记录',
    '来源必须完成收敛且快照一致',
    '来源检查点不是仅完成收敛',
    '原始ENCUT及网格输出必须通过连续两级收敛验收',
    '固定ENCUT交叉复核未通过',
    '来源 SCF 尚未通过验收',
    '二维 slab 的能带晶胞未标准化',
    '二维 DOS 尚未开放',
    '来源仍在运行或已归档',
)


def detect_drift_code(error_code: str | None, message: str) -> str | None:
    code = rec.detect_drift_code(error_code, message)
    if code is not None:
        return code
    if any(marker in message for marker in _FORK_DRIFT_MARKERS):
        return 'INPUT_HASH_MISMATCH'
    return None


def handle_error(service: Any, contract_id: str, exc: Exception, *, source_job: str, phase: str) -> Exception:
    msg = str(exc)
    drift = detect_drift_code(getattr(exc, 'error_code', None), msg)
    if drift is not None:
        return rec.halt_on_drift(service, contract_id, drift, msg, details={'phase': phase, 'source_job': source_job})
    if '运行锁' in msg or '仍在运行' in msg or '正在提交或恢复' in msg:
        return core.ToolError(
            'REPAIR_FORK_SOURCE_RUNNING',
            f'来源任务 {source_job} 仍在运行或提交中，暂不能派生修复：{scrub(msg)[:300]}',
            {'action': 'poll_vasp_watch_jobs', 'job_name': source_job, 'phase': phase},
        )
    if 'DOS 输入被程序阻断' in msg:
        return core.ToolError('REPAIR_FORK_BLOCKED', f'DOS 派生修复输入被确定性引擎阻断：{scrub(msg)[:360]}')
    if any(marker in msg for marker in _INELIGIBLE_MARKERS):
        return core.ToolError('REPAIR_FORK_NOT_ELIGIBLE', f'来源任务 {source_job} 不满足该派生修复策略前提：{scrub(msg)[:360]}')
    return exc


def validate_diagnostic_fields(
    *, failure_category: str, evidence_sha256: str, repair_hypothesis: str, applied_change: str,
) -> dict[str, str]:
    if failure_category not in FAILURE_CATEGORIES:
        raise ContractError('CONTRACT_SCHEMA_INVALID', f'未知故障分类 failure_category={failure_category!r}。')
    if not isinstance(evidence_sha256, str) or not _HEX64_RE.fullmatch(evidence_sha256):
        raise ContractError('CONTRACT_SCHEMA_INVALID', 'evidence_sha256 必须为 64 位小写十六进制 SHA256 摘要。')
    hyp = scrub(repair_hypothesis).strip() if isinstance(repair_hypothesis, str) else ''
    chg = scrub(applied_change).strip() if isinstance(applied_change, str) else ''
    if not hyp or not chg:
        raise ContractError('CONTRACT_SCHEMA_INVALID', 'repair_hypothesis 与 applied_change 必须为非空诊断记录。')
    return {
        'failure_category': failure_category,
        'evidence_sha256': evidence_sha256,
        'repair_hypothesis': hyp[:500],
        'applied_change': chg[:500],
    }


def validate_strategy_and_params(
    contract: dict[str, Any],
    *,
    source_job: str,
    failed_job: str,
    phase: str,
    strategy: str,
    resources: dict[str, Any] | None,
    nbands: int | None,
    production_mesh: list[int] | None,
    production_encut: float | None,
    band_path: str | None,
    dos_values: dict[str, Any] | None,
) -> str:
    if strategy not in REPAIR_STRATEGIES:
        raise ContractError('CONTRACT_SCHEMA_INVALID', f'未知派生修复策略 strategy={strategy!r}。')
    target_phase = STRATEGY_TARGET_PHASE[strategy]
    allowed = contract['workflow']['allowed_phases']
    dim = contract['scope']['dimension']
    if strategy == 'fresh_restart':
        if failed_job != source_job:
            raise ContractError('CONTRACT_SCHEMA_INVALID', 'fresh_restart 策略不支持指定与 source_job 不同的 failed_job。')
        if 'convergence' not in allowed:
            raise ContractError('CONTRACT_SCOPE_VIOLATION', '当前合同未启用 convergence 阶段，禁止执行 fresh_restart。')
        if production_mesh is not None or production_encut is not None or band_path is not None:
            raise ContractError('CONTRACT_SCHEMA_INVALID', 'fresh_restart 不接受 production_mesh / production_encut / band_path。')
        if dos_values is not None and 'dos' not in allowed:
            raise ContractError('CONTRACT_SCOPE_VIOLATION', '当前合同未启用 dos 阶段，禁止在 fresh_restart 中传入 dos_values。')
    elif strategy == 'production_from_convergence':
        if phase != 'relaxation':
            raise ContractError('CONTRACT_SCHEMA_INVALID', 'production_from_convergence 仅可用于修复 relaxation 阶段。')
        if nbands is not None or band_path is not None or dos_values is not None:
            raise ContractError('CONTRACT_SCHEMA_INVALID', 'production_from_convergence 仅接受 resources / production_mesh / production_encut。')
    elif strategy == 'bands_from_scf':
        if phase != 'bands':
            raise ContractError('CONTRACT_SCHEMA_INVALID', 'bands_from_scf 仅可用于修复 bands 阶段。')
        if dim != '3D':
            raise core.ToolError('REPAIR_FORK_NOT_ELIGIBLE', '冻结引擎的 prepare_bands_path 仅支持 3D 体系；2D slab 请改用 fresh_restart 或熔断交人工。')
        if nbands is not None or production_mesh is not None or production_encut is not None or dos_values is not None:
            raise ContractError('CONTRACT_SCHEMA_INVALID', 'bands_from_scf 仅接受 resources 与 band_path。')
    elif strategy == 'dos_from_scf':
        if phase != 'dos':
            raise ContractError('CONTRACT_SCHEMA_INVALID', 'dos_from_scf 仅可用于修复 dos 阶段。')
        if dim != '3D':
            raise core.ToolError('REPAIR_FORK_NOT_ELIGIBLE', '冻结引擎的 prepare_dos_fork 仅支持 3D 体系。')
        if nbands is not None or production_mesh is not None or production_encut is not None or band_path is not None:
            raise ContractError('CONTRACT_SCHEMA_INVALID', 'dos_from_scf 仅接受 resources 与 dos_values（调整 DOS 能带数请传 dos_values.nbands）。')
    if target_phase not in allowed:
        raise ContractError('CONTRACT_SCOPE_VIOLATION', f'修复目标阶段 {target_phase} 不在合同允许阶段列表内。')
    if resources is not None and not isinstance(resources, dict):
        raise ContractError('CONTRACT_SCHEMA_INVALID', 'resources 必须是 JSON 对象或 null。')
    return target_phase


def ensure_failure_not_already_repaired(
    ledger: dict[str, Any], *, failed_job: str, phase: str, failed_job_id: str | None,
) -> None:
    """Block replaying an already-repaired `(failed_job, phase, job_id)` to bypass `max_consecutive_failures`."""
    if not failed_job_id:
        return
    for entry in ledger.get('diagnostics') or []:
        if (
            isinstance(entry, dict)
            and entry.get('kind') == 'repair'
            and not entry.get('halted')
            and entry.get('failed_job') == failed_job
            and entry.get('phase') == phase
            and str(entry.get('failed_job_id', '')) == failed_job_id
        ):
            raise core.ToolError(
                'REPAIR_FORK_ALREADY_HANDLED',
                f'失败作业 {failed_job}/{phase}（JobID={failed_job_id}）已派生过修复目录 {entry.get("new_job_name")}，'
                '禁止重复复用同一失败回执绕过连续失败上限；请针对最新失败任务发起诊断或熔断交人工。',
            )
    saw_later_activity = False
    for entry in reversed(ledger.get('history') or []):
        if not isinstance(entry, dict):
            continue
        ev = entry.get('event')
        if ev in ('phase_passed', 'resumed'):
            break
        if ev in ('submission_confirmed', 'retry_repair'):
            saw_later_activity = True
        elif (
            ev == 'phase_failed'
            and entry.get('job_name') == failed_job
            and entry.get('phase') == phase
            and isinstance(entry.get('failure_info'), dict)
            and str(entry['failure_info'].get('job_id', '')) == failed_job_id
        ):
            if saw_later_activity:
                raise core.ToolError(
                    'REPAIR_FORK_ALREADY_HANDLED',
                    f'失败作业 {failed_job}/{phase}（JobID={failed_job_id}）之后已发生后续提交或修复尝试，禁止回退复用旧失败回执。',
                )
            break


def _read_local_json(workspace: Path, rel: str, label: str) -> Any:
    path = rec.reject_symlink_rel(workspace, rel, label)
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding='utf-8'))
    except (OSError, ValueError) as exc:
        raise core.ToolError('REPAIR_FORK_SOURCE_UNCERTAIN', f'{label} JSON 损坏：{scrub(str(exc))[:200]}') from exc


def _check_local_slurm_queue(job_id: str, *, job_name: str, phase: str) -> None:
    squeue = shutil.which('squeue')
    if not squeue:
        return
    try:
        proc = subprocess.run([squeue, '-h', '-j', job_id, '-o', '%i|%T'], capture_output=True, text=True, timeout=15, check=False)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise core.ToolError('REPAIR_FORK_SOURCE_UNCERTAIN', f'本机 squeue 核查 JobID={job_id} 失败（{scrub(str(exc))[:160]}）；请先人工核查。') from exc
    if proc.returncode != 0:
        raise core.ToolError('REPAIR_FORK_SOURCE_UNCERTAIN', f'本机 squeue 返回错误码 {proc.returncode}，无法确认 JobID={job_id} 是否已结束。')
    if proc.stdout.strip():
        raise core.ToolError(
            'REPAIR_FORK_SOURCE_RUNNING',
            f'本机任务 {job_name}（JobID={job_id}）仍在 Slurm 队列中（PENDING/RUNNING），暂不能派生修复。',
            {'action': 'poll_vasp_watch_jobs', 'job_name': job_name, 'phase': phase},
        )


def read_job_status(
    service: Any, *, contract_id: str, target_server: str | None, job_name: str, phase: str, remote: Any | None,
) -> dict[str, Any]:
    """Read local or remote task status while guarding symlinks, locks, archives, and live queue."""
    if target_server is None:
        folder = rec.reject_symlink_rel(service.workspace, f'runs/{job_name}', f'任务目录 {job_name}')
        if not folder.is_dir():
            raise FileNotFoundError(f'任务目录不存在：{job_name}')
        if rec.reject_symlink_rel(service.workspace, f'runs/{job_name}/.task-archived.json', '归档标记').exists():
            raise core.ToolError('REPAIR_FORK_NOT_ELIGIBLE', f'任务 {job_name} 已归档，无法作为派生修复目标。')
        if rec.reject_symlink_rel(service.workspace, f'runs/{job_name}/.agent.lock', '运行锁').exists():
            raise core.ToolError(
                'REPAIR_FORK_SOURCE_RUNNING',
                f'任务 {job_name} 仍有运行锁，请等待运行结束后再派生修复。',
                {'action': 'poll_vasp_watch_jobs', 'job_name': job_name, 'phase': phase},
            )
        rec.reject_symlink_rel(service.workspace, f'runs/{job_name}/config.json', f'任务 {job_name} 配置')
        stage_subs: dict[str, Any] = {}
        subs_dir = rec.reject_symlink_rel(service.workspace, f'runs/{job_name}/submissions', f'任务 {job_name} 提交回执目录')
        if subs_dir.is_dir():
            for name in stage_gate.task_stages(folder):
                rec_data = _read_local_json(service.workspace, f'runs/{job_name}/submissions/{name}.json', f'阶段 {name} 提交回执')
                if rec_data is not None:
                    stage_subs[name] = rec_data
        phase_sub = stage_subs.get(phase)
        local_jid = rec.confirmed_receipt_job_id(phase_sub, phase) if isinstance(phase_sub, dict) else None
        if local_jid is not None:
            _check_local_slurm_queue(local_jid, job_name=job_name, phase=phase)
        return {
            'workflow-state.json': _read_local_json(service.workspace, f'runs/{job_name}/workflow-state.json', '工作流状态'),
            'checkpoint.json': _read_local_json(service.workspace, f'runs/{job_name}/checkpoint.json', '检查点'),
            'physics-model.json': _read_local_json(service.workspace, f'runs/{job_name}/physics-model.json', '物理模型确认'),
            'convergence-array.json': _read_local_json(service.workspace, f'runs/{job_name}/convergence-array.json', '数组状态'),
            'stage_submissions': stage_subs,
            'queue': None,
            'scheduler_status_known': True,
        }

    assert remote is not None
    try:
        status = remote.call('status', job_name)
    except Exception as exc:
        raise handle_error(service, contract_id, exc, source_job=job_name, phase=phase) from exc
    if not isinstance(status, dict):
        raise core.ToolError('REPAIR_FORK_SOURCE_UNCERTAIN', f'远端任务 {job_name} 状态响应无效。')
    array_tasks = status.get('array_tasks')
    if status.get('queue') or (isinstance(array_tasks, dict) and array_tasks.get('all_terminal') is False):
        raise core.ToolError(
            'REPAIR_FORK_SOURCE_RUNNING',
            f'任务 {job_name} 仍在 Slurm 队列或数组未全部终态，请待结束后再派生修复。',
            {'action': 'poll_vasp_watch_jobs', 'job_name': job_name, 'phase': phase},
        )
    if status.get('scheduler_status_known') is False:
        raise core.ToolError('REPAIR_FORK_SOURCE_UNCERTAIN', f'任务 {job_name} 调度状态未知，禁止盲目派生修复；请先诊断核查。')
    return status


def validate_failed_phase_eligibility(
    *, failed_job: str, phase: str, failure_category: str, resources: dict[str, Any] | None,
    status: dict[str, Any], is_remote: bool,
) -> dict[str, Any]:
    """Ensure `failed_job` genuinely failed or hit a layout block on `phase` (never running/uncertain/passed)."""
    ckpt = status.get('checkpoint.json') if isinstance(status.get('checkpoint.json'), dict) else {}
    wf_state = status.get('workflow-state.json') if isinstance(status.get('workflow-state.json'), dict) else {}
    stage_subs = status.get('stage_submissions') if isinstance(status.get('stage_submissions'), dict) else {}
    phase_sub = stage_subs.get(phase)

    if phase_sub is not None:
        if not isinstance(phase_sub, dict) or phase_sub.get('status') != 'submitted':
            st = phase_sub.get('status') if isinstance(phase_sub, dict) else type(phase_sub).__name__
            raise core.ToolError(
                'REPAIR_FORK_SOURCE_UNCERTAIN',
                f'任务 {failed_job} 阶段 {phase} 存在非 confirmed 提交回执（status={st}），请先人工对账或熔断。',
            )
    if phase in (ckpt.get('completed') or []):
        raise core.ToolError('REPAIR_FORK_ALREADY_PASSED', f'任务 {failed_job} 阶段 {phase} 已验收通过，无需派生修复。')

    failed_job_id = rec.confirmed_receipt_job_id(phase_sub, phase) if phase_sub is not None else None
    if is_remote and failed_job_id is not None:
        if status.get('scheduler_status_known') is not True:
            raise core.ToolError(
                'REPAIR_FORK_SOURCE_UNCERTAIN',
                f'任务 {failed_job} 阶段 {phase}（JobID={failed_job_id}）缺少已确认的远端调度终态（scheduler_status_known={status.get("scheduler_status_known")!r}），禁止盲目派生。',
            )
        rem_sub = status.get('remote-submission.json')
        if isinstance(rem_sub, dict) and str(rem_sub.get('job_id', '')) != failed_job_id:
            raise core.ToolError(
                'REPAIR_FORK_SOURCE_UNCERTAIN',
                f'任务 {failed_job} 的 remote-submission.json（JobID={rem_sub.get("job_id")}）与阶段 {phase} 回执（JobID={failed_job_id}）不一致，无法确认该阶段作业已结束。',
            )

    current_phase = ckpt.get('next', 'convergence')
    wf_stage = wf_state.get('phase')
    if wf_stage is None and isinstance(wf_state.get('stage'), str):
        wf_stage = stage_gate.phase_of(wf_state['stage'])
    has_failed_wf = wf_state.get('status') == 'failed' and (
        wf_stage == phase or (wf_stage is None and current_phase == phase)
    )
    array_tasks = status.get('array_tasks') if isinstance(status.get('array_tasks'), dict) else None
    has_array_failure = bool(
        phase == 'convergence'
        and array_tasks and array_tasks.get('all_terminal') is True
        and any(isinstance(t, dict) and t.get('state') != 'COMPLETED' for t in (array_tasks.get('tasks') or []))
    )
    has_submitted_non_passed = phase_sub is not None and failed_job_id is not None

    if not (has_failed_wf or has_array_failure or has_submitted_non_passed):
        if failure_category in _PREFLIGHT_ONLY_CATEGORIES and resources is not None and current_phase == phase:
            return {'failed_job_id': None, 'failed_state': failure_category.upper(), 'workflow_status': wf_state.get('status'), 'had_submission': False}
        raise core.ToolError(
            'REPAIR_FORK_NOT_ELIGIBLE',
            f'任务 {failed_job} 阶段 {phase} 尚未提交且工作流未处于该阶段 failed 状态（当前阶段={current_phase}），不满足派生修复前提；'
            '若属当前阶段的提交前容量或节点均分冲突，请显式传入修复后的 resources 与对应 failure_category。',
        )

    failed_state = 'FAILED'
    if has_array_failure and array_tasks:
        bad = [str(t.get('state') or 'FAILED') for t in (array_tasks.get('tasks') or []) if isinstance(t, dict) and t.get('state') != 'COMPLETED']
        failed_state = bad[0] if bad else 'FAILED'
    elif failure_category == 'oom':
        failed_state = 'OUT_OF_MEMORY'
    elif failure_category == 'walltime_exceeded':
        failed_state = 'TIMEOUT'
    return {'failed_job_id': failed_job_id, 'failed_state': failed_state, 'workflow_status': wf_state.get('status'), 'had_submission': phase_sub is not None}


def _reject_local_source_symlinks(workspace: Path, job_name: str) -> Path:
    source_dir = rec.reject_symlink_rel(workspace, f'runs/{job_name}', f'来源任务目录 {job_name}')
    for leaf in _LOCAL_META_FILES:
        rec.reject_symlink_rel(workspace, f'runs/{job_name}/{leaf}', f'来源任务文件 {leaf}')
    manifest_path = source_dir / 'manifest.json'
    if manifest_path.is_file():
        try:
            manifest = json.loads(manifest_path.read_text(encoding='utf-8'))
        except (OSError, ValueError):
            manifest = None
        if isinstance(manifest, dict) and isinstance(manifest.get('hashes'), dict):
            for rel_name in manifest['hashes']:
                if isinstance(rel_name, str):
                    rec.reject_symlink_rel(workspace, f'runs/{job_name}/{rel_name}', f'来源清单文件 {rel_name}')
    return source_dir


def read_source_context(
    service: Any, *, contract_id: str, target_server: str | None, source_job: str, phase: str,
    remote: Any | None, cached_status: dict[str, Any] | None,
) -> dict[str, Any]:
    status = cached_status if cached_status is not None else read_job_status(
        service, contract_id=contract_id, target_server=target_server, job_name=source_job, phase=phase, remote=remote,
    )
    stage_subs = status.get('stage_submissions') if isinstance(status.get('stage_submissions'), dict) else {}
    for sub_phase, sub_rec in stage_subs.items():
        if not isinstance(sub_rec, dict) or sub_rec.get('status') != 'submitted':
            st = sub_rec.get('status') if isinstance(sub_rec, dict) else type(sub_rec).__name__
            raise core.ToolError(
                'REPAIR_FORK_SOURCE_UNCERTAIN',
                f'来源任务 {source_job} 阶段 {sub_phase} 存在非 confirmed 提交回执（status={st}），禁止作为派生来源。',
            )
    try:
        if target_server is None:
            source_dir = _reject_local_source_symlinks(service.workspace, source_job)
            review_ctx = input_review.context(source_dir)
        else:
            assert remote is not None
            review_ctx = remote.call('review_context', source_job)
    except Exception as exc:
        raise handle_error(service, contract_id, exc, source_job=source_job, phase=phase) from exc

    if not isinstance(review_ctx, dict) or not isinstance(review_ctx.get('config'), dict):
        raise core.ToolError('REPAIR_FORK_INSPECTION_INVALID', f'来源任务 {source_job} 的 review_context 响应无效。')
    manifest = review_ctx.get('manifest') if isinstance(review_ctx.get('manifest'), dict) else {}
    hashes = manifest.get('hashes') if isinstance(manifest.get('hashes'), dict) else {}
    return {
        'status': status, 'config': review_ctx['config'], 'manifest': manifest,
        'initial_poscar': review_ctx.get('initial_poscar'), 'potcar_sha256': hashes.get('POTCAR'),
        'environment': review_ctx.get('environment'), 'physics': status.get('physics-model.json'),
        'checkpoint': status.get('checkpoint.json') if isinstance(status.get('checkpoint.json'), dict) else {},
        'workflow_state': status.get('workflow-state.json') if isinstance(status.get('workflow-state.json'), dict) else {},
        'stage_submissions': stage_subs,
    }


def validate_physics_record_matches(
    service: Any, contract: dict[str, Any], phys: Any, *, job_name: str, phase: str, required: bool,
) -> None:
    if phys is None:
        if required:
            raise core.ToolError('PHYSICS_CONFIRMATION_REQUIRED', f'任务 {job_name} 缺少物理模型确认，禁止派生。')
        return
    pa = contract['physics_assertion']
    mismatch = (
        not isinstance(phys, dict) or phys.get('goal') != pa['goal']
        or phys.get('dimension') != contract['scope']['dimension']
        or phys.get('magnetism') != pa['magnetism'] or phys.get('charge') != pa['charge']
        or phys.get('functional') != pa['functional']
        or phys.get('soc') is not False or phys.get('hubbard_u') is not False or phys.get('hybrid') is not False
        or phys.get('confirmed_by', 'operator_slash_command') != pa['confirmed_by']
        or phys.get('assumptions_source', pa['assumptions_source']) != pa['assumptions_source']
    )
    if mismatch:
        raise rec.halt_on_drift(
            service, contract['contract_id'], 'CONTRACT_TAMPERED',
            f'任务 {job_name} 的 physics-model.json 与不可变合同 physics_assertion 不一致。',
            details={'phase': phase, 'job_name': job_name},
        )


def validate_source_binding(
    service: Any, contract: dict[str, Any], source_ctx: dict[str, Any], *, source_job: str, phase: str, require_physics: bool,
) -> None:
    scan_retry_checks.validate_source_contract_binding(service, contract, source_ctx, source_job=source_job)
    validate_physics_record_matches(service, contract, source_ctx['physics'], job_name=source_job, phase=phase, required=require_physics)


def validate_cross_job_provenance(
    service: Any, contract: dict[str, Any], *, target_server: str | None, source_job: str,
    failed_job: str, phase: str, strategy: str, source_ctx: dict[str, Any], failed_ctx: dict[str, Any],
) -> None:
    """When `failed_job != source_job`, verify tamper binding, provenance, and no precision downgrade."""
    cid = contract['contract_id']
    validate_source_binding(service, contract, failed_ctx, source_job=failed_job, phase=phase, require_physics=False)
    if (
        (source_ctx['initial_poscar'] is not None and failed_ctx['initial_poscar'] is not None
         and source_ctx['initial_poscar'] != failed_ctx['initial_poscar'])
        or (source_ctx['potcar_sha256'] is not None and failed_ctx['potcar_sha256'] is not None
            and source_ctx['potcar_sha256'] != failed_ctx['potcar_sha256'])
    ):
        raise rec.halt_on_drift(
            service, cid, 'CONTRACT_TAMPERED',
            f'来源任务 {source_job} 与失败任务 {failed_job} 的结构或 POTCAR 哈希不一致。',
            details={'phase': phase, 'source_job': source_job, 'failed_job': failed_job},
        )
    src_cfg, fail_cfg = source_ctx['config'], failed_ctx['config']
    src_preset, fail_preset = src_cfg.get('scan_preset') or 'strict', fail_cfg.get('scan_preset') or 'strict'
    if _PRESET_RANK.get(src_preset, -1) < _PRESET_RANK.get(fail_preset, -1):
        raise ContractError('CONTRACT_RETRY_NOT_STRICTER', f'来源任务 {source_job} 扫描档位（{src_preset}）宽松于失败任务 {failed_job}（{fail_preset}），禁止跨分支降级。')
    src_th = {**_DEFAULT_THRESHOLDS, **workflow_policy.SCAN_PRESETS.get(src_preset, {}), **(workflow_policy.validate_scan_thresholds(src_cfg.get('scan_thresholds')) or {})}
    fail_th = {**_DEFAULT_THRESHOLDS, **workflow_policy.SCAN_PRESETS.get(fail_preset, {}), **(workflow_policy.validate_scan_thresholds(fail_cfg.get('scan_thresholds')) or {})}
    if any(src_th[k] > fail_th[k] for k in _DEFAULT_THRESHOLDS):
        raise ContractError('CONTRACT_RETRY_NOT_STRICTER', f'来源任务 {source_job} 扫描阈值宽松于失败任务 {failed_job}，禁止跨分支降级。')
    if max(src_cfg['encuts']) < max(fail_cfg['encuts']) or (
        src_cfg['meshes'][-1][0] * src_cfg['meshes'][-1][1] * src_cfg['meshes'][-1][2]
        < fail_cfg['meshes'][-1][0] * fail_cfg['meshes'][-1][1] * fail_cfg['meshes'][-1][2]
    ):
        raise ContractError('CONTRACT_RETRY_NOT_STRICTER', f'来源任务 {source_job} 收敛扫描精度低于失败任务 {failed_job}，禁止跨分支回退降级。')
    if target_server is None and strategy == 'production_from_convergence':
        choice = _read_local_json(service.workspace, f'runs/{failed_job}/production-choice.json', '失败任务派生回执')
        if isinstance(choice, dict) and choice.get('source'):
            ancestor = PurePosixPath(str(choice['source']).replace('\\', '/')).name
            if ancestor != source_job:
                raise ContractError('CONTRACT_SCOPE_VIOLATION', f'失败任务 {failed_job} 派生自 {ancestor} 而非 {source_job}，禁止跨无关来源派生。')
