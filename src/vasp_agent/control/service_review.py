"""Layer 4 Maker-Checker review orchestration: read-receipt leases, stage reviews, and submit gate."""
from __future__ import annotations

import re
from typing import Any

from vasp_agent.chat.toolbox_core import scrub
from vasp_agent.permissions import review_attestation, task_contract
from vasp_agent.permissions.contract_rules import ContractError, validate_job_name
from vasp_agent.workflow import stage_definitions, stage_gate
import vasp_agent.workflow.vasp_band_agent as core

_HEX64_RE = re.compile(r'^[0-9a-f]{64}$')


def _reject_local_job_paths(workspace: Any, job_name: str, phase: str | None = None) -> None:
    root = workspace.resolve(strict=True)
    job_dir = root / 'runs' / job_name
    review_attestation.reject_symlink_chain(root, job_dir, f'任务目录 {job_name}')
    if phase is not None:
        review_attestation.reject_symlink_chain(root, job_dir / 'reviews', f'审阅历史目录 {job_name}/reviews')
        review_attestation.reject_symlink_chain(root, job_dir / f'review-{phase}.json', f'审阅记录 review-{phase}.json')
        review_attestation.reject_symlink_chain(root, job_dir / f'REVIEW-{phase}.md', f'审阅报告 REVIEW-{phase}.md')


def _lease_brief(lease: dict[str, Any]) -> dict[str, Any]:
    return {
        'lease_id': lease['lease_id'],
        'actor_id': lease['actor_id'],
        'role': lease['role'],
        'phase': lease['phase'],
        'snapshot': lease['snapshot'],
        'issued_at': lease['issued_at'],
        'expires_at': lease['expires_at'],
    }


def _resolve_bound_contract(
    service: Any, *, server: str | None, job_name: str, phase: str, contract_id: str | None = None,
) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    """Resolve an explicit active contract or the unique active contract covering `(server, job_name)`."""
    if contract_id is not None:
        contract, ledger = task_contract.verify_contract(service.workspace, contract_id, require_active=True)
        if (
            contract['scope']['server'] != server
            or job_name not in ledger['allowed_jobs']
            or phase not in contract['workflow']['allowed_phases']
        ):
            raise ContractError(
                'CONTRACT_SCOPE_VIOLATION',
                f'审阅目标 ({server}, {job_name}, {phase}) 不在合同 {contract_id} 授权范围内。',
            )
        return contract, ledger

    status = task_contract.contract_status(service.workspace)
    matches: list[tuple[dict[str, Any], dict[str, Any]]] = []
    for item in status.get('contracts') or []:
        if item.get('status') != 'active':
            continue
        scope = item.get('scope')
        if isinstance(scope, dict) and scope.get('server') != server:
            continue
        cid = item.get('contract_id')
        if not isinstance(cid, str):
            continue
        try:
            contract, ledger = task_contract.verify_contract(service.workspace, cid, require_active=True)
        except ContractError:
            continue
        if contract['scope']['server'] == server and job_name in ledger['allowed_jobs']:
            matches.append((contract, ledger))

    if len(matches) > 1:
        ids = '、'.join(c['contract_id'] for c, _ in matches)
        raise ContractError(
            'REVIEW_CONTRACT_AMBIGUOUS',
            f'任务 {job_name} 匹配多个活动合同（{ids}），请在 vasp_record_stage_review 中显式传入 contract_id。',
        )
    if len(matches) == 1:
        contract, ledger = matches[0]
        if phase not in contract['workflow']['allowed_phases']:
            raise ContractError(
                'CONTRACT_SCOPE_VIOLATION',
                f'阶段 {phase} 不在活动合同 {contract["contract_id"]} 的 allowed_phases 内。',
            )
        return contract, ledger
    return None, None


def get_stage_review(service: Any, job_name: str, server: str | None = None) -> dict[str, Any]:
    """Fetch stage review context and mint a 15-minute read-receipt lease when snapshot is canonical."""
    validate_job_name(job_name)
    norm_server = review_attestation.normalize_server(server)
    if norm_server is None:
        from vasp_agent.remote.worker_lifecycle import check_resume

        _reject_local_job_paths(service.workspace, job_name)
        folder = service.box.job(job_name)
        if folder.is_dir():
            check_resume(folder)
    data = service.box.get_stage_review(norm_server, job_name)
    if not isinstance(data, dict):
        return data
    phase = data.get('phase')
    snapshot = data.get('snapshot')
    if (
        isinstance(phase, str)
        and phase in stage_definitions.known_names()
        and isinstance(snapshot, str)
        and _HEX64_RE.fullmatch(snapshot)
    ):
        lease = review_attestation.issue_lease(
            service.workspace,
            actor_id=service.actor_id,
            role=service.role,
            server=norm_server,
            job_name=job_name,
            phase=phase,
            snapshot=snapshot,
            host=service.host,
            model=service.model,
        )
        return {**data, 'review_lease': _lease_brief(lease)}
    return data


def record_stage_review(
    service: Any,
    *,
    job_name: str,
    snapshot: str,
    phase: str,
    checks: list[dict[str, Any]],
    server: str | None = None,
    contract_id: str | None = None,
    lease_id: str | None = None,
) -> dict[str, Any]:
    """Verify read-receipt lease & Maker-Checker policy, record review via engine, and save attestation."""
    validate_job_name(job_name)
    norm_server = review_attestation.normalize_server(server)
    lease = review_attestation.verify_lease(
        service.workspace,
        actor_id=service.actor_id,
        role=service.role,
        server=norm_server,
        job_name=job_name,
        phase=phase,
        snapshot=snapshot,
        lease_id=lease_id,
        host=service.host,
        model=service.model,
    )
    contract, _ledger = _resolve_bound_contract(
        service, server=norm_server, job_name=job_name, phase=phase, contract_id=contract_id,
    )
    if contract is not None:
        review_mode: str | None = contract['review_policy']['mode']
        bound_cid: str | None = contract['contract_id']
        maker_id: str | None = contract.get('maker_actor_id')
        review_attestation.enforce_review_policy(
            review_mode, role=service.role, actor_id=service.actor_id, maker_actor_ids=(maker_id,),
        )
    else:
        if service.role != 'checker':
            raise ContractError(
                'REVIEW_CHECKER_ROLE_REQUIRED',
                '无活动 maker_direct 合同授权时，禁止 maker 角色记录阶段审阅；请使用独立只读 checker 角色。',
            )
        review_mode, bound_cid, maker_id = None, None, None

    clean_checks = [{k: scrub(v) if isinstance(v, str) else v for k, v in item.items()} for item in checks]
    reviewer = review_attestation.format_reviewer(
        role=service.role, host=service.host, model=service.model, actor_id=service.actor_id,
    )
    prog_failures: list[str] = []

    if norm_server is None:
        from vasp_agent.remote.worker_lifecycle import check_resume

        _reject_local_job_paths(service.workspace, job_name, phase=phase)
        folder = service.box.job(job_name)
        with core.digest_memo():
            check_resume(folder)
            ctx_now = stage_gate.context(folder)
            prog_failures = list(stage_gate.program_failures(ctx_now))
            recorded = stage_gate.record(folder, snapshot, phase, clean_checks, reviewer)
    else:
        from vasp_agent.remote.remote_compute import RemoteCompute

        remote = RemoteCompute(service.workspace, norm_server)
        recorded = remote.call(
            'stage_review', job_name, snapshot=snapshot, phase=phase, checks=clean_checks, reviewer=reviewer,
        )
        if phase == 'dos' and not bool(recorded.get('allowed', False)):
            try:
                ctx_remote = remote.call('stage_context', job_name)
                if isinstance(ctx_remote, dict):
                    prog_failures = list(stage_gate.program_failures(ctx_remote))
            except Exception:
                prog_failures = []

    allowed = bool(recorded.get('allowed', False))
    attestation = review_attestation.save_attestation(
        service.workspace,
        server=norm_server,
        job_name=job_name,
        phase=phase,
        snapshot=snapshot,
        role=service.role,
        checker_actor_id=service.actor_id,
        reviewer=reviewer,
        lease_id=lease['lease_id'],
        allowed=allowed,
        checks=clean_checks,
        contract_id=bound_cid,
        maker_actor_id=maker_id,
        review_mode=review_mode,
        host=service.host,
        model=service.model,
        program_failures=prog_failures,
        review_time=recorded.get('time') if isinstance(recorded.get('time'), str) else None,
    )
    service.box.log({
        'control_record_stage_review': {
            'contract_id': bound_cid,
            'server': norm_server,
            'job_name': job_name,
            'phase': phase,
            'snapshot': snapshot,
            'role': service.role,
            'actor_id': service.actor_id,
            'reviewer': reviewer,
            'lease_id': lease['lease_id'],
            'allowed': allowed,
            'blocking_items': attestation['blocking_items'],
        },
    })
    return {
        **recorded,
        'contract_id': bound_cid,
        'review_mode': review_mode,
        'attestation': {
            'contract_id': attestation['contract_id'],
            'role': attestation['role'],
            'checker_actor_id': attestation['checker_actor_id'],
            'reviewer': attestation['reviewer'],
            'lease_id': attestation['lease_id'],
            'snapshot': attestation['snapshot'],
            'allowed': attestation['allowed'],
            'blocking_items': attestation['blocking_items'],
            'program_failures': attestation['program_failures'],
            'time': attestation['time'],
        },
        'next': (
            f'阶段 {phase} 审阅已放行并写入本地背书；Maker 可调用 vasp_submit_stage 提交该阶段。'
            if allowed
            else f'阶段 {phase} 审阅结果为阻断（blocking_items={attestation["blocking_items"]}），禁止提交该阶段。'
        ),
    }


def _authoritative_stage_context(service: Any, server: str | None, job_name: str) -> dict[str, Any]:
    if server is None:
        from vasp_agent.remote.worker_lifecycle import check_resume

        _reject_local_job_paths(service.workspace, job_name)
        folder = service.box.job(job_name)
        with core.digest_memo():
            check_resume(folder)
            return stage_gate.context(folder)
    from vasp_agent.remote.remote_compute import RemoteCompute

    return RemoteCompute(service.workspace, server).call('stage_context', job_name)


def verify_submit_attestation(
    service: Any,
    contract: dict[str, Any],
    ledger: dict[str, Any],
    *,
    server: str | None,
    job_name: str,
    phase: str,
) -> dict[str, Any]:
    """Verify local Maker-Checker attestation against the live authoritative snapshot before submit."""
    del ledger
    validate_job_name(job_name)
    norm_server = review_attestation.normalize_server(server)
    ctx = _authoritative_stage_context(service, norm_server, job_name)
    if ctx.get('phase') != phase:
        raise ContractError(
            'REVIEW_PHASE_MISMATCH',
            f'当前待审阅阶段为 {ctx.get("phase")!r}，与提交请求阶段 {phase!r} 不一致。',
        )
    prog_fails = stage_gate.program_failures(ctx)
    if prog_fails:
        raise ContractError(
            'REVIEW_PROGRAM_PRECHECK_BLOCKED',
            f'程序预检未通过（{"、".join(prog_fails)}），禁止提交阶段 {phase}。',
        )
    return review_attestation.verify_attestation_for_submit(
        service.workspace,
        contract,
        server=norm_server,
        job_name=job_name,
        phase=phase,
        snapshot=str(ctx.get('snapshot') or ''),
        submitting_actor_id=service.actor_id,
        maker_actor_ids=(contract.get('maker_actor_id'),),
    )
