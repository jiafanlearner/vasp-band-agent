"""Layer 4 helpers for contract execution: path/drift/halt guards, local submit, and disconnect reconciliation."""
from __future__ import annotations

from datetime import datetime, timezone
import json
import os
from pathlib import Path, PurePosixPath
import re
import subprocess
from typing import Any

from vasp_agent.chat.toolbox_core import scrub
from vasp_agent.common import agent_paths
from vasp_agent.permissions import task_contract
from vasp_agent.permissions.contract_rules import ContractError
from vasp_agent.workflow import stage_gate
import vasp_agent.workflow.vasp_band_agent as core

JOB_ID_RE = re.compile(r'^[0-9]+$')
DRIFT_CODES = frozenset({'INPUT_HASH_MISMATCH', 'ENVIRONMENT_PROFILE_DRIFT', 'SCRIPT_HASH_DRIFT'})
DRIFT_MARKERS = (
    ('ENVIRONMENT_PROFILE_DRIFT', ('VASP环境已改变', 'ENVIRONMENT_PROFILE_DRIFT')),
    ('SCRIPT_HASH_DRIFT', ('提交脚本已改变', '提交脚本哈希已变化', 'Slurm 脚本已改变', '来自另一份提交脚本')),
    (
        'INPUT_HASH_MISMATCH',
        (
            '输入与 manifest 不一致',
            '输入文件已改变',
            '在续算确认后发生变化',
            '与完成记录不一致',
            '与 inputs.json 不一致',
            '中断阶段的输入已改变',
            '与任务 POTCAR 不一致',
            '与审阅方案不一致',
            '与执行记录不一致',
            '上游阶段证据与检查点记录不一致',
            '哈希不匹配',
            'INPUT_HASH_MISMATCH',
        ),
    ),
)


def confirmed_receipt_job_id(data: Any, phase: str) -> str | None:
    """Return numeric JobID iff `data` is a confirmed submitted receipt matching `phase`."""
    if not isinstance(data, dict) or data.get('status') != 'submitted':
        return None
    if data.get('phase', phase) != phase:
        return None
    job_id = str(data.get('job_id', ''))
    return job_id if JOB_ID_RE.fullmatch(job_id) else None


def normalize_server(server: str | None) -> str | None:
    return None if server in (None, 'local') else server


def limits_for(server_key: str, resources: dict[str, Any]) -> dict[str, Any]:
    array = resources.get('submission_mode', 'allocation') == 'array'
    return {
        'server': server_key,
        'partition': resources['partition'],
        'nodes': resources['nodes'] * (resources['parallel_tests'] if array else 1),
        'cores': resources['parallel_tests'] * resources['cores_per_test'],
        'walltime': resources['walltime'],
    }


def ledger_brief(ledger: dict[str, Any], contract: dict[str, Any]) -> dict[str, Any]:
    used = int(ledger.get('submissions_used', 0))
    return {
        'status': ledger['status'],
        'submissions_used': used,
        'submissions_remaining': max(0, int(contract['budget']['max_submissions']) - used),
        'consecutive_failures': int(ledger.get('consecutive_failures', 0)),
        'in_flight': ledger.get('in_flight'),
    }


def last_failure_phase(ledger: dict[str, Any]) -> tuple[str, str] | None:
    """Return `(job_name, phase)` of the latest un-cleared `phase_failed` history entry, if any."""
    for entry in reversed(ledger.get('history') or []):
        if not isinstance(entry, dict):
            continue
        event = entry.get('event')
        if event in ('phase_passed', 'resumed'):
            return None
        if event == 'phase_failed' and isinstance(entry.get('job_name'), str) and isinstance(entry.get('phase'), str):
            return entry['job_name'], entry['phase']
    return None


def reject_symlink_rel(workspace: Path, rel_posix: str, label: str) -> Path:
    """Reject symlinks along every unresolved segment before calling resolve()."""
    parts = PurePosixPath(rel_posix).parts
    if not parts or any(part in ('', '.', '..') for part in parts):
        raise ContractError('CONTRACT_PATH_ESCAPE', f'{label} 路径非法。')
    cursor = workspace
    for part in parts:
        cursor = cursor / part
        if cursor.is_symlink():
            raise ContractError('CONTRACT_SYMLINK_REFUSED', f'{label} 路径包含符号链接：{part}')
    if not cursor.resolve().is_relative_to(workspace):
        raise ContractError('CONTRACT_PATH_ESCAPE', f'{label} 超出授权工作区。')
    return cursor


def plan_path_checked(workspace: Path, plan_id: str) -> Path:
    """Locate scan plan via agent_paths layout while rejecting symlinks on unresolved segments."""
    new_rel, legacy_rel = agent_paths.STORES['scan_plans']
    primary = reject_symlink_rel(workspace, f'{agent_paths.AGENT_DIR}/{new_rel}/{plan_id}.json', f'关联扫描方案 {plan_id}')
    if primary.is_file():
        return primary
    if legacy_rel is not None:
        legacy = reject_symlink_rel(workspace, f'{legacy_rel}/{plan_id}.json', f'关联扫描方案 {plan_id}')
        if legacy.is_file():
            return legacy
    raise ContractError('CONTRACT_NOT_FOUND', f'找不到关联扫描方案 {plan_id}。')


def detect_drift_code(error_code: str | None, message: str) -> str | None:
    if error_code in DRIFT_CODES:
        return error_code
    for code, markers in DRIFT_MARKERS:
        if any(marker in message for marker in markers):
            return code
    return None


def verify_env_profile_binding(workspace: Path, contract: dict[str, Any]) -> None:
    server, bound = contract['scope']['server'], contract['scope'].get('environment_profile')
    if server is None or not isinstance(bound, dict):
        return
    from vasp_agent.remote import environment_profiles as profiles

    try:
        current = profiles.by_name(workspace, server, bound['name'])
    except Exception as exc:
        raise ContractError(
            'ENVIRONMENT_PROFILE_DRIFT',
            f'合同绑定的环境配置 {bound["name"]} 已不可用：{scrub(str(exc))[:200]}',
        ) from exc
    if (
        current.get('environment_sha256') != bound['environment_sha256']
        or current.get('executable_sha256') != bound['executable_sha256']
    ):
        raise ContractError('ENVIRONMENT_PROFILE_DRIFT', f'合同绑定的环境配置 {bound["name"]} 哈希已漂移，拒绝继续执行。')


def halt_on_drift(
    service: Any, contract_id: str, code: str, reason: str, *, details: dict[str, Any] | None = None
) -> ContractError:
    task_contract.halt_contract(
        service.workspace, contract_id, scrub(reason)[:500],
        code=code, uncertain=False, details=details, actor=f'{service.role}:{service.actor_id}',
    )
    return ContractError(code, f'检测到哈希或环境漂移（{code}），合同已自动熔断：{scrub(reason)[:300]}')


def halt_uncertain(
    service: Any, contract_id: str, *, reason: str, details: dict[str, Any] | None = None
) -> ContractError:
    task_contract.halt_contract(
        service.workspace, contract_id, scrub(reason)[:500],
        code='SUBMISSION_UNCERTAIN', uncertain=True, details=details, actor=f'{service.role}:{service.actor_id}',
    )
    return ContractError(
        'SUBMISSION_UNCERTAIN',
        f'{scrub(reason)[:300]}；合同已硬熔断为 halted_uncertain，严禁自动重交，请人工核查后决定是否恢复。',
    )


def register_monitor_safe(
    service: Any, server: str | None, job_name: str, phase: str, job_id: str, resources: dict[str, Any]
) -> dict[str, Any]:
    """Register confirmed JobID into JobMonitor without turning a confirmed submit into failure."""
    try:
        task = service.box.monitor.register(
            server or 'local', job_name, phase, job_id,
            resources=resources, conversation=getattr(service, 'actor_id', None),
        )
        settings = service.box.monitor.status()
        return {
            'registered': True, 'enabled': settings['enabled'], 'interval_seconds': task['interval_seconds'],
            'active': task['active'], 'next_due_at': task['next_due_at'],
        }
    except Exception as exc:
        return {
            'registered': False,
            'warning': f'提交回执已确认，但监控登记失败（{type(exc).__name__}）；请用 vasp_watch_jobs 核查，禁止重复提交。',
        }


def finalize_confirmed_submission(
    service: Any,
    contract: dict[str, Any],
    *,
    server: str | None,
    job_name: str,
    phase: str,
    job_id: str,
    operation_id: str,
    resources: dict[str, Any],
    attestation: dict[str, Any],
    recovered: bool,
    next_msg: str,
    original_error: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Register monitor and audit FIRST, then settle ledger without ever swallowing a confirmed JobID."""
    cid = contract['contract_id']
    monitoring = register_monitor_safe(service, server, job_name, phase, job_id, resources)
    ledger_warning: dict[str, Any] | None = None
    ledger: dict[str, Any] | None = None
    try:
        ledger = task_contract.confirm_submission(
            service.workspace, cid, job_id=job_id, operation_id=operation_id, recovered=recovered,
        )
    except Exception as exc:
        ledger_warning = {
            'error_code': getattr(exc, 'error_code', type(exc).__name__),
            'message': scrub(str(exc))[:300],
        }
        try:
            ledger = task_contract.read_ledger(service.workspace, cid)
        except Exception:
            ledger = None
    log_key = 'control_submit_recovered' if recovered else 'control_submit_stage'
    service.box.log({
        log_key: {
            'contract_id': cid, 'server': server, 'job_name': job_name, 'phase': phase,
            'job_id': job_id, 'operation_id': operation_id,
            'submitted_at': datetime.now(timezone.utc).isoformat(),
            **({'ledger_warning': ledger_warning} if ledger_warning else {}),
        },
    })
    next_text = (
        f'作业已真实提交（JobID={job_id}）并已尝试登记 JobMonitor，但合同账本结清失败'
        f'（{ledger_warning["error_code"]}），后续阶段将被合同门禁阻断；请人工核查并严禁重复提交。'
        if ledger_warning else next_msg
    )
    result: dict[str, Any] = {
        'contract_id': cid, 'server': server, 'job_name': job_name, 'phase': phase,
        'status': 'submitted', 'submitted': True, 'recovered': recovered, 'job_id': job_id,
        'operation_id': operation_id, 'resources': resources, 'monitoring': monitoring,
        'suggested_poll_seconds': monitoring.get('interval_seconds'), 'review_attestation': attestation,
        'ledger': ledger_brief(ledger, contract) if ledger is not None else None,
        'next': next_text,
    }
    if original_error is not None:
        result['original_error'] = original_error
    if ledger_warning is not None:
        result['ledger_warning'] = ledger_warning
    return result


def local_submit_checked(service: Any, folder: Path, phase: str, limits: dict[str, Any]) -> dict[str, Any]:
    """Contract-gated local Slurm preflight + O_EXCL receipt + sbatch (mirrors remote submit_checked)."""
    if service.box.submit_script is None:
        return {'ready': False, 'submission_not_attempted': True,
                'error_code': 'LOCAL_SUBMIT_SCRIPT_MISSING', 'error': '本机未配置 Slurm 提交脚本，未提交。'}
    try:
        job_rel = f'runs/{folder.name}'
        reject_symlink_rel(service.workspace, job_rel, f'任务目录 {folder.name}')
        reject_symlink_rel(service.workspace, f'{job_rel}/submit.slurm', '本机提交脚本')
        reject_symlink_rel(service.workspace, f'{job_rel}/submit-script-source.json', '提交脚本来源记录')
        reject_symlink_rel(service.workspace, f'{job_rel}/submission.json', '任务提交回执')
        if (folder / '.task-archived.json').exists():
            raise ValueError('任务已归档，请先恢复再提交。')
        manifest = core.verify(folder)
        if manifest.get('workflow') != 'full' or not manifest.get('stage_gates'):
            raise ValueError('合同自动提交仅支持带阶段门禁的完整流程任务。')
        record = stage_gate.submission_record(folder, phase)
        reject_symlink_rel(service.workspace, f'{job_rel}/submissions/{record.name}', f'提交回执 {record.name}')
        if record.exists():
            try:
                existing = json.loads(record.read_text(encoding='utf-8'))
            except (OSError, ValueError):
                existing = {'status': 'corrupt_receipt', 'job_id': None, 'phase': phase}
            if not isinstance(existing, dict):
                existing = {'status': 'corrupt_receipt', 'job_id': None, 'phase': phase}
            return {**existing, 'submission_not_attempted': True, 'existing_record': True}
        if core.digest(service.box.submit_script) != service.box.script_hash:
            raise core.ToolError('SCRIPT_HASH_DRIFT', 'Slurm 脚本已改变，请重新启动会话审核配置。')
        task_script = service.box.stage_submit_script(folder)
        from vasp_agent.remote.remote_worker import array_layout, check_previous_job, check_resources

        script_text = task_script.read_text(encoding='utf-8')
        check_resources(script_text, limits)
        cfg = json.loads((folder / 'config.json').read_text(encoding='utf-8'))
        if cfg.get('submission_mode') == 'array' or array_layout(script_text) is not None:
            raise ValueError('本机提交不支持 array 模式。')
        stage_gate.require_review(folder, phase)
        check_previous_job(folder, phase)
        if (folder / '.agent.lock').exists():
            raise ValueError('该计算目录正在被使用。')
    except Exception as exc:
        return {
            'ready': False, 'submission_not_attempted': True,
            'error_code': getattr(exc, 'error_code', 'PREFLIGHT_REFUSED'),
            'error': scrub(str(exc))[:2000],
        }

    record.parent.mkdir(exist_ok=True)
    with record.open('x', encoding='utf-8', newline='\n') as stream:
        json.dump({'status': 'submission_pending', 'job_id': None, 'phase': phase}, stream, ensure_ascii=False)
        stream.flush()
        os.fsync(stream.fileno())
    env = dict({k: v for k, v in os.environ.items() if not k.startswith('SBATCH_')},
               AGENT_DIR=str(service.box.agent_dir), WORK_DIR=str(folder), VASP_AGENT_PHASE=phase)
    try:
        proc = subprocess.run(
            ['sbatch', '--parsable', str(task_script)],
            cwd=folder, env=env, capture_output=True, text=True, timeout=30, check=True,
        )
        job_id = proc.stdout.strip().split(';')[0]
        if not JOB_ID_RE.fullmatch(job_id):
            raise ValueError('sbatch 返回无法识别，必须人工核对队列后再处理。')
        data = {'status': 'submitted', 'job_id': job_id, 'phase': phase, 'script_sha256': core.digest(task_script)}
        core.save(folder / 'submission.json', data)
        core.save(record, data)
        return data
    except Exception:
        core.save(record, {'status': 'submission_uncertain', 'job_id': None, 'phase': phase,
                           'next': '人工核对 squeue/sacct；禁止自动重新提交。'})
        raise


def diagnostic_confirms_no_submission(diag: dict[str, Any], phase: str) -> bool:
    """Strictly verify read-only submission_diagnostic proves ZERO receipt and ZERO Slurm candidate."""
    if not isinstance(diag, dict) or diag.get('receipt') is not None or diag.get('query_error'):
        return False
    if diag.get('lookup_status') != 'no_match' or diag.get('accounting') != []:
        return False
    if diag.get('candidate_count') != 0 or diag.get('malformed_rows') != 0 or diag.get('truncated'):
        return False
    diagnosis = diag.get('diagnosis')
    evidence = diagnosis.get('evidence') if isinstance(diagnosis, dict) else None
    task_files = evidence.get('task_files') if isinstance(evidence, dict) else None
    if not isinstance(task_files, list):
        return False
    expected_path = f'submissions/{phase}.json'
    matched = [item for item in task_files if isinstance(item, dict) and item.get('path') == expected_path]
    if len(matched) != 1:
        return False
    return matched[0].get('exists') is False and not matched[0].get('read_error')


def reconcile_after_error(
    service: Any,
    contract: dict[str, Any],
    *,
    server: str | None,
    job_name: str,
    phase: str,
    operation_id: str,
    resources: dict[str, Any],
    attestation: dict[str, Any],
    exc: Exception,
) -> dict[str, Any]:
    """Read-only reconciliation after submit error: confirm valid JobID, allow safe retry, or hard-halt."""
    cid = contract['contract_id']
    orig_err = {'type': type(exc).__name__, 'code': getattr(exc, 'error_code', None), 'message': scrub(str(exc))[:300]}

    if server is None:
        data: Any = None
        try:
            folder = reject_symlink_rel(service.workspace, f'runs/{job_name}', f'任务目录 {job_name}')
            record = stage_gate.submission_record(folder, phase)
            reject_symlink_rel(service.workspace, f'runs/{job_name}/submissions/{record.name}', f'提交回执 {record.name}')
            if record.is_file():
                data = json.loads(record.read_text(encoding='utf-8'))
        except (OSError, ValueError, ContractError):
            data = None
        job_id = confirmed_receipt_job_id(data, phase)
        if job_id is not None:
            return finalize_confirmed_submission(
                service, contract, server=None, job_name=job_name, phase=phase, job_id=job_id,
                operation_id=operation_id, resources=resources, attestation=attestation, recovered=True,
                original_error=orig_err, next_msg='断连后只读对账已确认合法 JobID 并补登监控，禁止重复提交。',
            )
        raise halt_uncertain(
            service, cid,
            reason=f'本机提交阶段 {phase} 异常且回执未确认有效 JobID：{orig_err["message"]}',
            details={'operation_id': operation_id, 'phase': phase, 'original_error': orig_err},
        ) from exc

    from vasp_agent.remote.remote_compute import RemoteCompute

    remote = RemoteCompute(service.workspace, server)
    try:
        ctx_after = remote.call('resource_context', job_name, phase=phase)
    except Exception as recon_exc:
        raise halt_uncertain(
            service, cid,
            reason=f'远端提交异常后只读 resource_context 对账失败：{scrub(str(recon_exc))[:200]}',
            details={'operation_id': operation_id, 'phase': phase, 'original_error': orig_err},
        ) from exc

    if ctx_after.get('already_registered'):
        job_id = confirmed_receipt_job_id(ctx_after, phase)
        if job_id is not None:
            return finalize_confirmed_submission(
                service, contract, server=server, job_name=job_name, phase=phase, job_id=job_id,
                operation_id=operation_id, resources=resources, attestation=attestation, recovered=True,
                original_error=orig_err, next_msg='SSH 断连后只读对账已确认远端有效 JobID 并自动补登监控；请通过 vasp_watch_jobs 周期轮询。',
            )
        raise halt_uncertain(
            service, cid,
            reason=f'断连对账发现远端回执处于不确定态（status={ctx_after.get("status")}）',
            details={'operation_id': operation_id, 'phase': phase, 'remote_status': ctx_after.get('status'),
                     'original_error': orig_err},
        ) from exc

    try:
        diag = remote.call('submission_diagnostic', job_name, phase=phase)
    except Exception as diag_exc:
        raise halt_uncertain(
            service, cid,
            reason=f'远端无回执但 submission_diagnostic 对账失败：{scrub(str(diag_exc))[:200]}',
            details={'operation_id': operation_id, 'phase': phase, 'original_error': orig_err},
        ) from exc

    if not diagnostic_confirms_no_submission(diag, phase):
        raise halt_uncertain(
            service, cid,
            reason=f'断连对账存在候选作业或查询不明确（lookup_status={diag.get("lookup_status")}）',
            details={'operation_id': operation_id, 'phase': phase, 'lookup_status': diag.get('lookup_status'),
                     'candidate_count': diag.get('candidate_count'), 'query_error': diag.get('query_error'),
                     'original_error': orig_err},
        ) from exc

    released = task_contract.release_in_flight(
        service.workspace, cid, operation_id=operation_id, refund=False,
        reason='disconnected_no_receipt_no_slurm_match',
    )
    remaining = max(0, int(contract['budget']['max_submissions']) - int(released['submissions_used']))
    if remaining <= 0:
        task_contract.halt_contract(
            service.workspace, cid,
            '断连对账确认未提交，但合同总提交额度已耗尽，自动熔断交人工审核。',
            code='CONTRACT_BUDGET_EXHAUSTED', uncertain=False,
            details={'operation_id': operation_id, 'phase': phase}, actor=f'{service.role}:{service.actor_id}',
        )
        raise ContractError('CONTRACT_BUDGET_EXHAUSTED', '断连对账确认未提交，但合同剩余提交额度为 0，已自动熔断。') from exc
    raise core.ToolError(
        'SUBMISSION_DISCONNECTED_NO_RECEIPT',
        '提交通信中断，只读对账（resource_context + submission_diagnostic）确认远端无提交回执且无匹配 Slurm 作业；'
        f'已释放 in_flight（剩余额度 {remaining} 次），可安全发起 1 次重试。',
        {
            'action': 'retry_vasp_submit_stage', 'contract_id': cid, 'server': server,
            'job_name': job_name, 'phase': phase, 'remaining_submissions': remaining,
        },
    ) from exc
