"""Layer 4 contract-gated self-healing base and in-place interrupted-phase resume (`vasp_prepare_resume`).

Strictly preserves frozen `ENGINE_MODULES` / `WORKER_UNITS` by delegating remote resume mechanics
to `worker_lifecycle.prepare_phase_resume` while enforcing contract scope, budgets, consecutive-failure
circuit breaking, resource envelopes, and JobMonitor deactivation.
"""
from __future__ import annotations

from datetime import datetime, timezone
import subprocess
from typing import Any

from vasp_agent.chat.toolbox_core import scrub
from vasp_agent.control import execution_reconcile as rec
from vasp_agent.control import service_execution
from vasp_agent.permissions import contract_rules, submission_dialog, task_contract
from vasp_agent.permissions.contract_rules import ContractError
import vasp_agent.workflow.vasp_band_agent as core

RESUMABLE_STATES = ('TIMEOUT', 'CANCELLED', 'NODE_FAIL', 'PREEMPTED')
_DISCONNECT_ERROR_CODES = frozenset({'REMOTE_TIMEOUT', 'SSH_FAILED', 'REMOTE_RESPONSE_INVALID'})
_RETRY_BUDGET_KEYS = {
    'resume': ('resume_retries_used', 'max_resume_retries'),
    'repair': ('repair_retries_used', 'max_repair_retries'),
    'scan': ('scan_retries_used', 'max_scan_retries'),
}


def healing_ledger_brief(ledger: dict[str, Any], contract: dict[str, Any]) -> dict[str, Any]:
    """Extend standard ledger brief with retry counters and allowed_jobs for healing responses."""
    budget = contract['budget']
    brief = rec.ledger_brief(ledger, contract)
    for kind, (used_key, max_key) in _RETRY_BUDGET_KEYS.items():
        used = int(ledger.get(used_key, 0))
        brief[used_key] = used
        brief[f'{kind}_retries_remaining'] = max(0, int(budget[max_key]) - used)
    brief['allowed_jobs'] = list(ledger.get('allowed_jobs') or [])
    return brief


def verify_healing_contract(
    service: Any,
    *,
    contract_id: str,
    server: str | None,
    job_name: str,
    phase: str,
) -> tuple[str | None, dict[str, Any], dict[str, Any]]:
    """Shared Layer 4 preflight for all self-healing tools: active contract, scope, in_flight, env binding."""
    target_server = rec.normalize_server(server)
    contract, ledger = task_contract.verify_contract(service.workspace, contract_id, require_active=True)
    if (
        target_server != contract['scope']['server']
        or job_name not in ledger['allowed_jobs']
        or phase not in contract['workflow']['allowed_phases']
    ):
        raise ContractError('CONTRACT_SCOPE_VIOLATION', f'自愈请求 ({target_server}, {job_name}, {phase}) 超出合同授权范围。')
    if ledger.get('in_flight') is not None:
        raise ContractError('SUBMISSION_IN_FLIGHT', '合同存在未对账在途提交，禁止执行自愈操作。')
    try:
        rec.verify_env_profile_binding(service.workspace, contract)
    except ContractError as exc:
        raise rec.halt_on_drift(service, contract_id, exc.error_code, str(exc), details={'phase': phase}) from exc
    return target_server, contract, ledger


def precheck_retry_budget(workspace: Any, contract: dict[str, Any], ledger: dict[str, Any], *, kind: str) -> None:
    """Auto-halt via ledger BEFORE remote mutation if submission/retry/failure budget is already exhausted."""
    budget = contract['budget']
    used_key, max_key = _RETRY_BUDGET_KEYS[kind]
    if (
        int(ledger.get('submissions_used', 0)) >= int(budget['max_submissions'])
        or int(ledger.get('consecutive_failures', 0)) >= int(budget['max_consecutive_failures'])
        or int(ledger.get(used_key, 0)) >= int(budget[max_key])
    ):
        task_contract.register_retry(workspace, contract['contract_id'], kind=kind)


def failure_already_recorded(ledger: dict[str, Any], *, job_name: str, phase: str, job_id: str) -> bool:
    """Avoid double-counting the same failed JobID in ledger consecutive_failures."""
    for entry in reversed(ledger.get('history') or []):
        if not isinstance(entry, dict):
            continue
        event = entry.get('event')
        if event in ('phase_passed', 'resumed'):
            return False
        if (
            event == 'phase_failed'
            and entry.get('job_name') == job_name
            and entry.get('phase') == phase
            and isinstance(entry.get('failure_info'), dict)
            and str(entry['failure_info'].get('job_id', '')) == job_id
        ):
            return True
    return False


def _classify_preview_error(exc: Exception, *, job_name: str, phase: str) -> Exception:
    msg = scrub(str(exc))
    if '仍在队列' in msg:
        return core.ToolError(
            'RESUME_JOB_STILL_ACTIVE',
            f'目标作业仍在队列中，暂不能续算：{msg[:400]}',
            {'action': 'poll_vasp_watch_jobs', 'job_name': job_name, 'phase': phase},
        )
    ineligible_markers = (
        '仅支持已确认 JobID 的普通（非数组）提交',
        '调度记账未显示作业',
        '工作流记录为 failed',
        '已不是当前待执行阶段',
        '没有提交回执',
    )
    if any(marker in msg for marker in ineligible_markers):
        return core.ToolError(
            'RESUME_NOT_ELIGIBLE',
            f'当前阶段不满足原地续算前提（仅限非数组且记账为 {"/".join(RESUMABLE_STATES)} 且未进入 failed 的中断作业）：'
            f'{msg[:400]}；若是 array 中断、Slurm FAILED/OOM 或 VASP 报错，请转入派生修复或人工诊断。',
        )
    return exc


def is_transport_disconnect(exc: Exception) -> bool:
    """True only for transport/timeout/invalid-response interruptions, not explicit worker REMOTE_ERROR refusals."""
    if getattr(exc, 'error_code', None) in _DISCONNECT_ERROR_CODES:
        return True
    if isinstance(exc, (ConnectionError, TimeoutError, subprocess.TimeoutExpired)):
        return True
    return isinstance(exc, OSError) and not isinstance(exc, FileNotFoundError)


_is_transport_disconnect = is_transport_disconnect


def _reconcile_resume_disconnect(
    remote: Any,
    job_name: str,
    phase: str,
    preview: dict[str, Any],
    preview_job_id: str,
    exc: Exception,
) -> dict[str, Any]:
    """Read-only status check after a transport disconnect during commit `prepare_phase_resume`."""
    try:
        status = remote.call('status', job_name)
    except Exception as status_exc:
        raise core.ToolError(
            'RESUME_RESULT_UNCERTAIN',
            f'续算执行通信中断且只读 status 核查失败（{scrub(str(status_exc))[:200]}）；'
            '请先通过 vasp_task_status 核对 checkpoint.json 与 submissions/ 后再处理。',
        ) from exc
    if not isinstance(status, dict):
        raise core.ToolError('RESUME_RESULT_UNCERTAIN', '续算通信中断且远端状态响应无效，请人工核查。') from exc
    checkpoint = status.get('checkpoint.json') if isinstance(status.get('checkpoint.json'), dict) else {}
    workflow = status.get('workflow-state.json') if isinstance(status.get('workflow-state.json'), dict) else {}
    resume_rec = checkpoint.get('resume') if isinstance(checkpoint.get('resume'), dict) else {}
    stage_subs = status.get('stage_submissions') if isinstance(status.get('stage_submissions'), dict) else {}
    if (
        resume_rec.get('phase') == phase
        and str(resume_rec.get('job_id', '')) == preview_job_id
        and phase not in stage_subs
    ):
        state = str(resume_rec.get('scheduler_state') or preview['scheduler_state'])
        return {
            **preview,
            'prepared': True,
            'recovered': True,
            'scheduler_state': state,
            'archived_receipt': f'submissions/{phase}.{state.lower()}-{preview_job_id}.json',
        }
    existing = stage_subs.get(phase)
    partial_written = (
        str(resume_rec.get('job_id', '')) == preview_job_id
        or str(workflow.get('resumed_from_job', '')) == preview_job_id
    )
    if isinstance(existing, dict) and str(existing.get('job_id', '')) == preview_job_id and not partial_written:
        raise core.ToolError(
            'RESUME_DISCONNECTED_NOT_APPLIED',
            f'续算通信中断，但只读核查确认阶段 {phase} 原提交回执（JobID={preview_job_id}）仍在、未归档；可安全重试续算。',
            {'action': 'retry_vasp_prepare_resume', 'job_name': job_name, 'phase': phase, 'job_id': preview_job_id},
        ) from exc
    raise core.ToolError(
        'RESUME_RESULT_UNCERTAIN',
        f'续算通信中断且远端回执处于半提交或不明确状态（原异常：{scrub(str(exc))[:200]}）；请先调用 vasp_task_status 核查。',
    ) from exc


def prepare_resume(
    service: Any,
    *,
    contract_id: str,
    job_name: str,
    phase: str,
    server: str | None = None,
    job_id: str | None = None,
    resources: dict[str, Any] | None = None,
    reason: str | None = None,
    evidence_sha256: str | None = None,
) -> dict[str, Any]:
    """Contract-gated in-place resume for non-array interrupted Slurm jobs (`TIMEOUT`/`CANCELLED`/`NODE_FAIL`/`PREEMPTED`)."""
    target_server, contract, ledger = verify_healing_contract(
        service, contract_id=contract_id, server=server, job_name=job_name, phase=phase,
    )
    if target_server is None:
        raise core.ToolError(
            'RESUME_UNSUPPORTED_LOCAL',
            '原地中断续算（prepare_phase_resume）仅支持远端集群任务；本机任务请核查输出或改用新目录派生。',
        )
    precheck_retry_budget(service.workspace, contract, ledger, kind='resume')

    envelope = contract['resource_envelope']
    pre_validated_res: dict[str, Any] | None = None
    if resources is not None:
        if resources.get('submission_mode', 'allocation') == 'array':
            raise ContractError('CONTRACT_RESOURCE_ENVELOPE_EXCEEDED', '原地续算仅支持非数组（allocation）提交模式，禁止切换为 array。')
        pre_validated_res = contract_rules.validate_resources_in_envelope(resources, phase, envelope, server=target_server)
        submission_dialog.validate_resources(
            pre_validated_res, phase, rec.limits_for(target_server, pre_validated_res),
            tests=None if phase == 'convergence' else 1,
        )

    from vasp_agent.remote.remote_compute import RemoteCompute

    remote = RemoteCompute(service.workspace, target_server)
    try:
        preview = remote.call('prepare_phase_resume', job_name, phase=phase)
    except Exception as exc:
        drift = rec.detect_drift_code(getattr(exc, 'error_code', None), str(exc))
        if drift is not None:
            raise rec.halt_on_drift(service, contract_id, drift, str(exc), details={'phase': phase}) from exc
        raise _classify_preview_error(exc, job_name=job_name, phase=phase) from exc

    preview_job_id = str(preview.get('job_id', '')) if isinstance(preview, dict) else ''
    scheduler_state = preview.get('scheduler_state') if isinstance(preview, dict) else None
    if (
        not isinstance(preview, dict)
        or preview.get('preview') is not True
        or preview.get('prepared') is not False
        or preview.get('phase') != phase
        or not rec.JOB_ID_RE.fullmatch(preview_job_id)
        or scheduler_state not in RESUMABLE_STATES
    ):
        raise core.ToolError('RESUME_PREVIEW_INVALID', '远端续算预览返回无效或不满足可续算终态，未修改任务。')
    if job_id is not None and str(job_id) != preview_job_id:
        raise core.ToolError(
            'RESUME_JOB_ID_MISMATCH',
            f'指定的 job_id={job_id} 与远端阶段回执 JobID={preview_job_id} 不一致，已拒绝续算。',
        )

    already_counted = failure_already_recorded(ledger, job_name=job_name, phase=phase, job_id=preview_job_id)
    max_failures = int(contract['budget']['max_consecutive_failures'])
    if not already_counted and int(ledger['consecutive_failures']) + 1 >= max_failures:
        task_contract.record_phase_outcome(
            service.workspace, contract_id, job_name=job_name, phase=phase, passed=False,
            failure_info={'job_id': preview_job_id, 'scheduler_state': scheduler_state, 'source': 'prepare_resume'},
        )
        raise ContractError(
            'CONTRACT_CONSECUTIVE_FAILURES_EXCEEDED',
            f'阶段 {phase} 连续失败已达合同上限 {max_failures}，合同已自动熔断交人工审核（未执行原地续算）。',
        )

    recovered = False
    try:
        resumed = remote.call('prepare_phase_resume', job_name, phase=phase, confirm_job_id=preview_job_id)
    except Exception as exc:
        drift = rec.detect_drift_code(getattr(exc, 'error_code', None), str(exc))
        if drift is not None:
            raise rec.halt_on_drift(service, contract_id, drift, str(exc), details={'phase': phase, 'job_id': preview_job_id}) from exc
        if not _is_transport_disconnect(exc):
            raise _classify_preview_error(exc, job_name=job_name, phase=phase) from exc
        resumed = _reconcile_resume_disconnect(remote, job_name, phase, preview, preview_job_id, exc)
        recovered = bool(resumed.get('recovered', False))

    if (
        not isinstance(resumed, dict)
        or resumed.get('prepared') is not True
        or resumed.get('phase', phase) != phase
        or str(resumed.get('job_id', '')) != preview_job_id
    ):
        raise core.ToolError('RESUME_RESULT_UNCERTAIN', '远端续算确认未返回合法已准备结果，请通过 vasp_task_status 核查。')

    monitor_deactivated = False
    monitor_warning: str | None = None
    try:
        monitor_deactivated = bool(
            service.box.monitor.deactivate(target_server, job_name, phase, preview_job_id, state=scheduler_state)
        )
    except Exception as exc:
        monitor_warning = f'旧作业监控记录停用失败（{type(exc).__name__}）；请通过 vasp_watch_jobs 核查。'

    diag_entry: dict[str, Any] = {
        'server': target_server,
        'job_name': job_name,
        'phase': phase,
        'job_id': preview_job_id,
        'scheduler_state': scheduler_state,
        'reused': list(resumed.get('reused') or []),
        'rerun': list(resumed.get('rerun') or []),
        'archive': resumed.get('archive'),
        'archived_receipt': resumed.get('archived_receipt'),
    }
    if reason:
        diag_entry['reason'] = scrub(reason)[:300]
    if evidence_sha256:
        diag_entry['evidence_sha256'] = evidence_sha256

    ledger_warning: dict[str, Any] | None = None
    cur_ledger: dict[str, Any] | None = None
    try:
        if not already_counted:
            task_contract.record_phase_outcome(
                service.workspace, contract_id, job_name=job_name, phase=phase, passed=False,
                failure_info={'job_id': preview_job_id, 'scheduler_state': scheduler_state, 'source': 'prepare_resume'},
            )
        cur_ledger = task_contract.register_retry(service.workspace, contract_id, kind='resume', diagnostic=diag_entry)
    except Exception as exc:
        ledger_warning = {'error_code': getattr(exc, 'error_code', type(exc).__name__), 'message': scrub(str(exc))[:300]}
        try:
            cur_ledger = task_contract.read_ledger(service.workspace, contract_id)
        except Exception:
            cur_ledger = None

    res_applied = False
    res_changed = False
    applied_resources: dict[str, Any] | None = None
    res_signature: str | None = None
    resource_warning: str | None = None
    post_drift: tuple[str, Exception] | None = None
    if pre_validated_res is not None and ledger_warning is None:
        try:
            res_info = service_execution._configure_phase_resources(
                service, target_server, job_name, phase, pre_validated_res, envelope, allow_missing_local_script=False,
            )
            if res_info.get('already_registered'):
                resource_warning = (
                    f'续算已准备完成，但阶段 {phase} 出现并发已登记提交回执，未覆写资源；'
                    '请通过 vasp_task_status 核查，禁止重复提交。'
                )
            else:
                res_applied = bool(res_info.get('resources_applied', False))
                res_changed = bool(res_info.get('resources_changed', False))
                applied_resources = res_info.get('resources')
                res_signature = res_info.get('signature')
        except Exception as exc:
            drift = rec.detect_drift_code(getattr(exc, 'error_code', None), str(exc))
            if drift is not None:
                post_drift = (drift, exc)
            else:
                resource_warning = f'续算已准备完成，但续算后资源更新失败（{scrub(str(exc))[:200]}）；可在下次提交前重试调整。'

    service.box.log({
        'control_prepare_resume': {
            'contract_id': contract_id, 'server': target_server, 'job_name': job_name, 'phase': phase,
            'job_id': preview_job_id, 'scheduler_state': scheduler_state, 'recovered': recovered,
            'monitor_deactivated': monitor_deactivated, 'resources_applied': res_applied,
            'resources_changed': res_changed, 'archived_receipt': resumed.get('archived_receipt'),
            'time': datetime.now(timezone.utc).isoformat(),
            **({'ledger_warning': ledger_warning} if ledger_warning else {}),
            **({'resource_warning': resource_warning} if resource_warning else {}),
            **({'drift_code': post_drift[0]} if post_drift is not None else {}),
        },
    })
    if post_drift is not None:
        drift_code, drift_exc = post_drift
        raise rec.halt_on_drift(
            service, contract_id, drift_code, str(drift_exc),
            details={'phase': phase, 'job_id': preview_job_id, 'archived_receipt': resumed.get('archived_receipt')},
        ) from drift_exc
    next_msg = (
        f'远端中断续算已准备完成（原 JobID={preview_job_id} 已归档），但合同账本更新失败'
        f'（{ledger_warning["error_code"]}），后续提交将被阻断；请人工核查。'
        if ledger_warning else
        '中断阶段已归档部分输出并锁定复用哈希（未提交、未消耗提交额度）；请重新执行 vasp_get_stage_review 与阶段审阅后再调用 vasp_submit_stage。'
    )
    out: dict[str, Any] = {
        'contract_id': contract_id, 'server': target_server, 'job_name': job_name, 'phase': phase,
        'job_id': preview_job_id, 'scheduler_state': scheduler_state, 'prepared': True, 'submitted': False,
        'recovered': recovered, 'reused': list(resumed.get('reused') or []), 'rerun': list(resumed.get('rerun') or []),
        'archive': resumed.get('archive'), 'archived': list(resumed.get('archived') or []),
        'archived_receipt': resumed.get('archived_receipt'), 'stale_lock': bool(resumed.get('stale_lock', False)),
        'monitor_deactivated': monitor_deactivated, 'resources_applied': res_applied,
        'resources_changed': res_changed, 'resources': applied_resources, 'resource_signature': res_signature,
        'ledger': healing_ledger_brief(cur_ledger, contract) if cur_ledger is not None else None,
        'next': next_msg,
    }
    if monitor_warning:
        out['monitor_warning'] = monitor_warning
    if ledger_warning:
        out['ledger_warning'] = ledger_warning
    if resource_warning:
        out['resource_warning'] = resource_warning
    return out
