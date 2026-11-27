"""Layer 4 contract-gated task preparation (`vasp_prepare_task`) and bounded submission (`vasp_submit_stage`).

Delegates low-level drift/halt guards, local sbatch preflight, and disconnect reconciliation to
`execution_reconcile` so both files stay cleanly below the 500-line engineering threshold.
"""
from __future__ import annotations

import hashlib
import hmac
import json
from pathlib import Path
from typing import Any
from uuid import uuid4

from vasp_agent.chat.toolbox_core import scrub
from vasp_agent.control import execution_reconcile as rec
from vasp_agent.permissions import contract_rules, submission_dialog, task_contract, unified_confirm
from vasp_agent.permissions.contract_rules import ContractError
from vasp_agent.planning import parameter_plans, plan_startup
import vasp_agent.workflow.vasp_band_agent as core


def _verify_review_attestation(
    service: Any,
    contract: dict[str, Any],
    ledger: dict[str, Any],
    *,
    server: str | None,
    job_name: str,
    phase: str,
) -> dict[str, Any]:
    """P2 extension point for local Maker–Checker review attestation before submission."""
    hook = getattr(service, '_review_attestation_verifier', None)
    if callable(hook):
        checked = hook(contract, ledger, server=server, job_name=job_name, phase=phase)
        return dict(checked) if isinstance(checked, dict) else {'enforced': True, 'mode': contract['review_policy']['mode']}
    return {
        'enforced': False,
        'mode': contract['review_policy']['mode'],
        'stage_gate_remote_enforced': True,
        'note': 'P1d 阶段执行远端/本地 stage_gate.require_review() 硬闸门；P2 将叠加本地 Maker–Checker 审阅背书闸门。',
    }


def _configure_phase_resources(
    service: Any,
    server: str | None,
    job_name: str,
    phase: str,
    resources: dict[str, Any],
    envelope: dict[str, Any],
    *,
    allow_missing_local_script: bool = False,
) -> dict[str, Any]:
    """Read resource_context, validate against envelope+tests, and apply configure_resources."""
    server_key = server or 'local'
    op_id = uuid4().hex
    if server is not None:
        from vasp_agent.remote.remote_compute import RemoteCompute

        remote = RemoteCompute(service.workspace, server)
        ctx = remote.call('resource_context', job_name, phase=phase)
        if ctx.get('already_registered'):
            return {'already_registered': True, 'receipt': ctx}
        tests = ctx.get('execution_plan', {}).get('tests') if phase == 'convergence' else 1
        validated = contract_rules.validate_resources_in_envelope(resources, phase, envelope, server=server, tests=tests)
        limits = rec.limits_for(server_key, validated)
        submission_dialog.validate_resources(validated, phase, limits, tests=tests)
        applied = remote.call(
            'configure_resources', job_name, phase=phase, resources=validated,
            signature=ctx['signature'], limits=limits, operation_id=op_id,
        )
        return {
            'already_registered': False, 'resources_applied': True,
            'resources_changed': bool(applied.get('changed', False)),
            'resources': applied.get('resources', validated), 'validated_resources': validated,
            'signature': applied.get('signature'), 'tests': tests, 'limits': limits,
        }

    folder = service.box.job(job_name)
    rec.reject_symlink_rel(service.workspace, f'runs/{job_name}', f'任务目录 {job_name}')
    rec.reject_symlink_rel(service.workspace, f'runs/{job_name}/submit.slurm', '本机提交脚本')
    rec.reject_symlink_rel(service.workspace, f'runs/{job_name}/submit-script-source.json', '提交脚本来源记录')
    if service.box.submit_script is None:
        if allow_missing_local_script:
            validated = contract_rules.validate_resources_in_envelope(resources, phase, envelope, server=None)
            return {
                'already_registered': False, 'resources_applied': False, 'resources_changed': False,
                'resources': validated, 'validated_resources': validated, 'signature': None,
                'tests': 1, 'limits': rec.limits_for('local', validated),
            }
        raise core.ToolError('LOCAL_SUBMIT_SCRIPT_MISSING', '本机未配置已审核 Slurm 提交脚本，无法配置或提交本机任务。')

    from vasp_agent.remote.remote_worker import configure_resources, resource_context

    if core.digest(service.box.submit_script) != service.box.script_hash:
        raise core.ToolError('SCRIPT_HASH_DRIFT', 'Slurm 脚本已改变，请重新启动会话审核配置。')
    service.box.stage_submit_script(folder)
    ctx = resource_context(folder, phase)
    if ctx.get('already_registered'):
        return {'already_registered': True, 'receipt': ctx}
    tests = ctx.get('execution_plan', {}).get('tests') if phase == 'convergence' else 1
    validated = contract_rules.validate_resources_in_envelope(resources, phase, envelope, server=None, tests=tests)
    limits = rec.limits_for('local', validated)
    submission_dialog.validate_resources(validated, phase, limits, tests=tests)
    applied = configure_resources(folder, phase, validated, ctx['signature'], limits, operation_id=op_id)
    return {
        'already_registered': False, 'resources_applied': True,
        'resources_changed': bool(applied.get('changed', False)),
        'resources': applied.get('resources', validated), 'validated_resources': validated,
        'signature': applied.get('signature'), 'tests': tests, 'limits': limits,
    }


def _validate_plan_for_prepare(workspace: Path, contract: dict[str, Any]) -> tuple[dict[str, Any], bool]:
    """Verify linked scan_plan hash integrity (including reconstructed digest when already prepared)."""
    plan_id = contract['workflow']['plan_id']
    plan_path = rec.plan_path_checked(workspace, plan_id)
    raw = plan_path.read_bytes()
    try:
        plan = json.loads(raw.decode('utf-8'))
    except (UnicodeDecodeError, ValueError) as exc:
        raise ContractError('CONTRACT_TAMPERED', f'关联扫描方案 {plan_id} JSON 已损坏。') from exc
    if not isinstance(plan, dict) or not isinstance(plan.get('request'), dict):
        raise ContractError('CONTRACT_TAMPERED', f'关联扫描方案 {plan_id} 结构无效。')
    req, scope = plan['request'], contract['scope']
    if req.get('server') != scope['server'] or req.get('job_name') != scope['job_name']:
        raise ContractError('CONTRACT_TAMPERED', '关联扫描方案的服务器或任务名与合同不一致。')
    if req.get('structure') != scope['structure']:
        raise ContractError('CONTRACT_TAMPERED', '关联扫描方案的结构路径与合同不一致。')
    if (req.get('scan_preset') or 'standard') != contract['workflow']['scan_preset']:
        raise ContractError('CONTRACT_TAMPERED', '关联扫描方案的 scan_preset 与合同不一致。')

    already_prepared = plan.get('status') == 'prepared'
    expected_sha = contract['workflow']['plan_sha256']
    if not already_prepared:
        if not hmac.compare_digest(hashlib.sha256(raw).hexdigest(), expected_sha):
            raise ContractError('CONTRACT_TAMPERED', f'关联扫描方案 {plan_id} 在合同签发后被修改（哈希不匹配）。')
        if plan.get('status') != 'selected':
            raise ContractError('CONTRACT_PLAN_NOT_SETTLED', f'关联扫描方案 {plan_id} 未完成签发锁定（status={plan.get("status")}）。')
    else:
        if not isinstance(plan.get('result'), dict):
            raise ContractError('CONTRACT_TAMPERED', f'已准备扫描方案 {plan_id} 缺少有效结果元数据。')
        restored = {k: v for k, v in plan.items() if k != 'result'}
        restored['status'] = 'selected'
        rendered = json.dumps(restored, ensure_ascii=False, indent=2)
        candidates = (
            hashlib.sha256((rendered + '\n').encode('utf-8')).hexdigest(),
            hashlib.sha256(rendered.encode('utf-8')).hexdigest(),
        )
        if not any(hmac.compare_digest(digest, expected_sha) for digest in candidates):
            raise ContractError('CONTRACT_TAMPERED', f'已准备扫描方案 {plan_id} 内容与合同签发哈希不一致。')

    selected = plan.get('selected') or {}
    if selected.get('scan_thresholds'):
        raise ContractError('CONTRACT_THRESHOLD_RELAXATION_FORBIDDEN', '自治合同禁止夹带放宽的 scan_thresholds。')
    bounds = contract['precision_bounds']
    encuts, meshes = selected.get('encuts') or [], selected.get('meshes') or []
    if encuts and max(encuts) > bounds['max_encut']:
        raise ContractError('CONTRACT_PRECISION_BOUND_EXCEEDED', '选定 ENCUT 超出合同 precision_bounds 上限。')
    if meshes:
        if max(a for m in meshes for a in m) > bounds['max_kmesh_axis']:
            raise ContractError('CONTRACT_PRECISION_BOUND_EXCEEDED', '选定 k 网格单轴超出合同 precision_bounds 上限。')
        if max(m[0] * m[1] * m[2] for m in meshes) > bounds['max_kmesh_points']:
            raise ContractError('CONTRACT_PRECISION_BOUND_EXCEEDED', '选定 k 网格总点数超出合同 precision_bounds 上限。')
    return plan, already_prepared


def prepare_task(service: Any, *, contract_id: str, resources: dict[str, Any] | None = None) -> dict[str, Any]:
    """Contract-gated prepare + confirm_physics('operator_slash_command') + first-phase resources."""
    contract, ledger = task_contract.verify_contract(service.workspace, contract_id, require_active=True)
    if ledger.get('in_flight') is not None:
        raise ContractError('SUBMISSION_IN_FLIGHT', '合同存在未结清在途提交，禁止准备新任务。')
    scope = contract['scope']
    server, job_name = scope['server'], scope['job_name']
    if job_name not in ledger['allowed_jobs']:
        raise ContractError('CONTRACT_SCOPE_VIOLATION', f'任务 {job_name} 不在合同允许目录清单中。')

    struct_path = rec.reject_symlink_rel(service.workspace, scope['structure'], '结构文件')
    if not struct_path.is_file():
        raise ContractError('CONTRACT_TAMPERED', f'合同绑定的结构文件缺失：{scope["structure"]}。')
    if not hmac.compare_digest(hashlib.sha256(struct_path.read_bytes()).hexdigest(), scope['structure_sha256']):
        raise rec.halt_on_drift(
            service, contract_id, 'INPUT_HASH_MISMATCH',
            f'结构文件 {scope["structure"]} 哈希与合同不一致。', details={'structure': scope['structure']},
        )

    try:
        rec.verify_env_profile_binding(service.workspace, contract)
    except ContractError as exc:
        raise rec.halt_on_drift(service, contract_id, exc.error_code, str(exc)) from exc

    plan, _ = _validate_plan_for_prepare(service.workspace, contract)
    first_phase = contract['workflow']['allowed_phases'][0]
    if first_phase != 'convergence':
        raise ContractError(
            'CONTRACT_SCOPE_VIOLATION',
            f'vasp_prepare_task 仅用于从 convergence 起步的新任务准备（当前首阶段为 {first_phase}）；派生合同请使用对应派生入口。',
        )
    envelope = contract['resource_envelope']
    target_res = envelope['phase_defaults'][first_phase] if resources is None else resources
    tests = plan_startup.planned_convergence_tests(plan)
    validated_res = contract_rules.validate_resources_in_envelope(target_res, first_phase, envelope, server=server, tests=tests)
    submission_dialog.validate_resources(validated_res, first_phase, rec.limits_for(server or 'local', validated_res), tests=tests)

    prep_raw = parameter_plans.prepare(service.box, contract['workflow']['plan_id'])
    already_prepared = bool(isinstance(prep_raw, dict) and prep_raw.get('already_prepared'))

    physics_record = unified_confirm.save_physics_confirmation(
        service.box, server, job_name, contract['physics_assertion']['goal'], 'operator_slash_command',
    )
    if physics_record.get('dimension') and physics_record['dimension'] != scope['dimension']:
        raise ContractError('CONTRACT_TAMPERED', '准备后任务检测维度与合同声明不一致。')

    try:
        res_info = _configure_phase_resources(
            service, server, job_name, first_phase, validated_res, envelope, allow_missing_local_script=True,
        )
    except Exception as exc:
        drift = rec.detect_drift_code(getattr(exc, 'error_code', None), str(exc))
        if drift is not None:
            raise rec.halt_on_drift(service, contract_id, drift, str(exc), details={'phase': first_phase}) from exc
        raise
    if res_info.get('already_registered'):
        raise ContractError('SUBMISSION_ALREADY_REGISTERED', f'阶段 {first_phase} 已有提交记录，不能重新配置首阶段资源。')

    service.box.log({
        'control_prepare_task': {
            'contract_id': contract_id, 'server': server, 'job_name': job_name, 'phase': first_phase,
            'plan_id': contract['workflow']['plan_id'], 'already_prepared': already_prepared,
            'resources_applied': res_info['resources_applied'], 'resources_changed': res_info['resources_changed'],
        },
    })
    return {
        'contract_id': contract_id, 'server': server, 'job_name': job_name, 'phase': first_phase,
        'plan_id': contract['workflow']['plan_id'], 'prepared': True, 'already_prepared': already_prepared,
        'submitted': False, 'physics_confirmed': True,
        'physics': {
            'goal': physics_record.get('goal'), 'dimension': physics_record.get('dimension'),
            'confirmed_by': physics_record.get('confirmed_by'), 'structure_sha256': physics_record.get('structure_sha256'),
        },
        'resources_applied': res_info['resources_applied'], 'resources_changed': res_info['resources_changed'],
        'resources': res_info['resources'], 'resource_signature': res_info['signature'],
        'ledger': rec.ledger_brief(ledger, contract),
        'next': f'任务已按合同准备并完成物理确认与首阶段（{first_phase}）资源配置（未提交）；请执行 vasp_get_stage_review 与阶段审阅后再调用 vasp_submit_stage。',
    }


def submit_stage(
    service: Any,
    *,
    contract_id: str,
    job_name: str,
    phase: str,
    server: str | None = None,
    resources: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Contract-gated bounded submission with pre-write budget reservation and disconnect reconciliation."""
    target_server = rec.normalize_server(server)
    contract, ledger = task_contract.verify_contract(service.workspace, contract_id, require_active=True)
    if (
        target_server != contract['scope']['server']
        or job_name not in ledger['allowed_jobs']
        or phase not in contract['workflow']['allowed_phases']
    ):
        raise ContractError('CONTRACT_SCOPE_VIOLATION', f'提交请求 ({target_server}, {job_name}, {phase}) 超出合同授权范围。')
    if ledger.get('in_flight') is not None:
        raise ContractError('SUBMISSION_IN_FLIGHT', '合同存在未对账在途提交，禁止并发或重复提交。')

    envelope = contract['resource_envelope']
    target_res = envelope['phase_defaults'][phase] if resources is None else resources
    pre_validated = contract_rules.validate_resources_in_envelope(target_res, phase, envelope, server=target_server)

    budget = contract['budget']
    if ledger['consecutive_failures'] >= budget['max_consecutive_failures'] or ledger['submissions_used'] >= budget['max_submissions']:
        task_contract.reserve_submission(
            service.workspace, contract_id, server=target_server, job_name=job_name, phase=phase, resources=pre_validated,
        )

    try:
        rec.verify_env_profile_binding(service.workspace, contract)
    except ContractError as exc:
        raise rec.halt_on_drift(service, contract_id, exc.error_code, str(exc)) from exc

    try:
        res_info = _configure_phase_resources(
            service, target_server, job_name, phase, pre_validated, envelope, allow_missing_local_script=False,
        )
    except Exception as exc:
        drift = rec.detect_drift_code(getattr(exc, 'error_code', None), str(exc))
        if drift is not None:
            raise rec.halt_on_drift(service, contract_id, drift, str(exc), details={'phase': phase}) from exc
        raise

    if res_info.get('already_registered'):
        receipt = res_info['receipt']
        job_id = rec.confirmed_receipt_job_id(receipt, phase)
        if job_id is not None:
            monitoring = rec.register_monitor_safe(service, target_server, job_name, phase, job_id, pre_validated)
            return {
                'contract_id': contract_id, 'server': target_server, 'job_name': job_name, 'phase': phase,
                'status': 'already_submitted', 'already_registered': True, 'submitted': False, 'job_id': job_id,
                'monitoring': monitoring, 'suggested_poll_seconds': monitoring.get('interval_seconds'),
                'review_attestation': {'enforced': False, 'mode': contract['review_policy']['mode'], 'skipped_already_submitted': True},
                'ledger': rec.ledger_brief(ledger, contract),
                'next': '该阶段已有已确认提交回执，未重复提交、未扣减额度；请用 vasp_watch_jobs 监控。',
            }
        raise rec.halt_uncertain(
            service, contract_id,
            reason=f'阶段 {phase} 已存在非 confirmed 或阶段不匹配提交回执（status={receipt.get("status")}）',
            details={'phase': phase, 'remote_status': receipt.get('status'), 'receipt_phase': receipt.get('phase')},
        )

    if res_info['resources_changed']:
        return {
            'contract_id': contract_id, 'server': target_server, 'job_name': job_name, 'phase': phase,
            'status': 'awaiting_resource_review', 'submitted': False, 'resources_changed': True,
            'resources': res_info['resources'], 'resource_signature': res_info['signature'],
            'review_attestation': {'enforced': False, 'mode': contract['review_policy']['mode'], 'pending_resource_review': True},
            'ledger': rec.ledger_brief(ledger, contract),
            'next': '阶段资源已按合同包络更新落盘（未提交、未扣减提交额度）；快照已变化，请先重新执行 vasp_get_stage_review 与阶段审阅后再调用 vasp_submit_stage。',
        }

    attestation = _verify_review_attestation(service, contract, ledger, server=target_server, job_name=job_name, phase=phase)

    validated_res, limits, operation_id = res_info['validated_resources'], res_info['limits'], uuid4().hex
    task_contract.reserve_submission(
        service.workspace, contract_id, server=target_server, job_name=job_name, phase=phase,
        resources=validated_res, operation_id=operation_id, tests=res_info['tests'],
    )

    try:
        if target_server is not None:
            from vasp_agent.remote.remote_compute import RemoteCompute

            raw = RemoteCompute(service.workspace, target_server).call('submit_checked', job_name, limits=limits, phase=phase)
        else:
            raw = rec.local_submit_checked(service, service.box.job(job_name), phase, limits)
    except Exception as exc:
        return rec.reconcile_after_error(
            service, contract, server=target_server, job_name=job_name, phase=phase,
            operation_id=operation_id, resources=validated_res, attestation=attestation, exc=exc,
        )

    if isinstance(raw, dict) and raw.get('submission_not_attempted'):
        if raw.get('existing_record'):
            job_id = rec.confirmed_receipt_job_id(raw, phase)
            if job_id is not None:
                monitoring = rec.register_monitor_safe(service, target_server, job_name, phase, job_id, validated_res)
                ledger_warning: dict[str, Any] | None = None
                cur_ledger: dict[str, Any] | None = None
                try:
                    cur_ledger = task_contract.release_in_flight(
                        service.workspace, contract_id, operation_id=operation_id, refund=True, reason='existing_submitted_receipt',
                    )
                except Exception as exc:
                    ledger_warning = {'error_code': getattr(exc, 'error_code', type(exc).__name__), 'message': scrub(str(exc))[:300]}
                    try:
                        cur_ledger = task_contract.read_ledger(service.workspace, contract_id)
                    except Exception:
                        cur_ledger = None
                resp: dict[str, Any] = {
                    'contract_id': contract_id, 'server': target_server, 'job_name': job_name, 'phase': phase,
                    'status': 'already_submitted', 'already_registered': True, 'submitted': False, 'job_id': job_id,
                    'monitoring': monitoring, 'suggested_poll_seconds': monitoring.get('interval_seconds'),
                    'review_attestation': attestation,
                    'ledger': rec.ledger_brief(cur_ledger, contract) if cur_ledger is not None else None,
                    'next': '预检发现既有已确认提交回执，已退还本次预扣额度；请用 vasp_watch_jobs 监控。',
                }
                if ledger_warning is not None:
                    resp['ledger_warning'] = ledger_warning
                return resp
            raise rec.halt_uncertain(
                service, contract_id, reason=f'预检发现不确定或阶段不匹配提交回执（status={raw.get("status")}）',
                details={'operation_id': operation_id, 'phase': phase, 'remote_status': raw.get('status'),
                         'receipt_phase': raw.get('phase')},
            )
        err_code = str(raw.get('error_code') or 'PREFLIGHT_REFUSED')
        err_msg = str(raw.get('error') or '提交前预检未通过，未触发 sbatch。')
        task_contract.release_in_flight(
            service.workspace, contract_id, operation_id=operation_id, refund=True, reason=f'preflight_refused:{err_code}',
        )
        drift = rec.detect_drift_code(err_code, err_msg)
        if drift is not None:
            raise rec.halt_on_drift(service, contract_id, drift, err_msg, details={'operation_id': operation_id, 'phase': phase})
        raise core.ToolError(err_code, err_msg)

    job_id = rec.confirmed_receipt_job_id(raw, phase)
    if job_id is None:
        raise rec.halt_uncertain(
            service, contract_id,
            reason=f'提交返回缺少合法已提交回执（status={raw.get("status") if isinstance(raw, dict) else None}）',
            details={'operation_id': operation_id, 'phase': phase,
                     'receipt_phase': raw.get('phase') if isinstance(raw, dict) else None},
        )

    return rec.finalize_confirmed_submission(
        service, contract, server=target_server, job_name=job_name, phase=phase, job_id=job_id,
        operation_id=operation_id, resources=validated_res, attestation=attestation, recovered=False,
        next_msg='作业已按合同提交并登记至 JobMonitor；请通过 vasp_watch_jobs(poll=true) 周期只读轮询，终态后派发审阅。',
    )
