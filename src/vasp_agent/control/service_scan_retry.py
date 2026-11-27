"""Layer 4 contract-gated strictly tighter convergence scan retry (`vasp_prepare_scan_retry`).

Preserves frozen `ENGINE_MODULES` / `WORKER_UNITS` by invoking remote `prepare_scan_retry` or local
`convergence_reuse.prepare_retry` directly after enforcing contract scope, inspect_convergence
eligibility, precision_bounds ceiling halts, monotonic tightening (`validate_stricter_retry`),
non-colliding directory naming (`fork_naming`), physics confirmation, and resource envelopes.
"""
from __future__ import annotations

import contextlib
from datetime import datetime, timezone
import io
import re
from typing import Any

from vasp_agent.chat import fork_naming
from vasp_agent.chat.toolbox_core import scrub
from vasp_agent.control import execution_reconcile as rec
from vasp_agent.control import scan_retry_checks as checks
from vasp_agent.control import service_execution, service_healing
from vasp_agent.permissions import contract_rules, submission_dialog, task_contract, unified_confirm
from vasp_agent.permissions.contract_rules import ContractError
from vasp_agent.ui.scan_retry_summary import planned_tests
from vasp_agent.workflow import convergence_reuse
import vasp_agent.workflow.vasp_band_agent as core

_RETRY_SUFFIX_RE = re.compile(r'^(.+)_r([1-9][0-9]*)$')
_MAX_NAME_ATTEMPTS = 5


def _default_retry_base_name(source_job: str) -> str:
    """Derive `<job>_r1` (or increment `<stem>_rN` -> `<stem>_r(N+1)`) within the 64-char JOB limit."""
    match = _RETRY_SUFFIX_RE.fullmatch(source_job)
    stem, suffix = (match.group(1), f'_r{int(match.group(2)) + 1}') if match else (source_job, '_r1')
    base = stem[:64 - len(suffix)] + suffix
    if not fork_naming.JOB_NAME_RE.fullmatch(base):
        raise ValueError(f'无法为来源任务 {source_job} 生成合法重扫目录名。')
    return base


def _create_retry_dir(
    service: Any, *, contract: dict[str, Any], target_server: str | None, remote: Any | None,
    source_job: str, source_ctx: dict[str, Any], base_name: str, taken: set[str],
    mode: str, validated: dict[str, Any], fixed_encut: float | None,
) -> tuple[str, bool, bool]:
    """Allocate a non-colliding directory via `fork_naming` and run local/remote `prepare_scan_retry`."""
    contract_id = contract['contract_id']
    if target_server is not None:
        service.box.ensure_current_release(target_server)
    call_encuts = validated['encuts'] if mode in ('encut', 'both') else None
    call_meshes = validated['meshes'] if mode in ('mesh', 'both') else None
    extra_opts: dict[str, Any] = {'scan_preset': validated['scan_preset']}
    if validated['scan_thresholds']:
        extra_opts['scan_thresholds'] = validated['scan_thresholds']

    name_conflicted = False
    for _attempt in range(_MAX_NAME_ATTEMPTS):
        candidate_job, conflicted = fork_naming.resolve_name(base_name, taken)
        name_conflicted = name_conflicted or conflicted
        claimed_local_dir = None
        try:
            if target_server is not None:
                assert remote is not None
                remote.call(
                    'prepare_scan_retry', candidate_job, source_job=source_job, mode=mode,
                    encuts=call_encuts, meshes=call_meshes, fixed_encut=fixed_encut, **extra_opts,
                )
            else:
                target_dir = rec.reject_symlink_rel(service.workspace, f'runs/{candidate_job}', f'目标任务目录 {candidate_job}')
                source_dir = rec.reject_symlink_rel(service.workspace, f'runs/{source_job}', f'来源任务目录 {source_job}')
                try:
                    target_dir.mkdir(parents=True, exist_ok=False)
                except FileExistsError:
                    taken.add(candidate_job)
                    name_conflicted = True
                    continue
                claimed_local_dir = target_dir
                with contextlib.redirect_stdout(io.StringIO()):
                    convergence_reuse.prepare_retry(
                        source_dir, target_dir, mode, call_encuts, call_meshes, fixed_encut=fixed_encut,
                        scan_thresholds=validated['scan_thresholds'], scan_preset=validated['scan_preset'],
                    )
            return candidate_job, name_conflicted, False
        except Exception as exc:
            if claimed_local_dir is not None:
                with contextlib.suppress(OSError):
                    claimed_local_dir.rmdir()
            if fork_naming.is_dir_exists_error(exc):
                taken.add(candidate_job)
                name_conflicted = True
                continue
            if target_server is not None and service_healing.is_transport_disconnect(exc):
                assert remote is not None
                reconciled = checks.reconcile_prepare_disconnect(
                    remote, contract=contract, source_job=source_job, source_ctx=source_ctx,
                    candidate_job=candidate_job, mode=mode, validated=validated, fixed_encut=fixed_encut, exc=exc,
                )
                return candidate_job, name_conflicted, bool(reconciled.get('recovered', False))
            raise checks.handle_drift_or_running(service, contract_id, exc, source_job=source_job) from exc
    raise core.ToolError('SCAN_RETRY_NAME_CONFLICT', f'目录名 {base_name} 连续冲突，未能分配新目录。')


def prepare_scan_retry(
    service: Any, *, contract_id: str, source_job: str, mode: str, server: str | None = None,
    job_name: str | None = None, encuts: list[float] | None = None, meshes: list[list[int]] | None = None,
    fixed_encut: float | None = None, scan_preset: str | None = None, scan_thresholds: dict[str, Any] | None = None,
    resources: dict[str, Any] | None = None, reason: str | None = None, evidence_sha256: str | None = None,
) -> dict[str, Any]:
    """Contract-gated strictly tighter convergence scan retry into a fresh immutable directory."""
    if job_name is not None:
        contract_rules.validate_job_name(job_name)
    target_server, contract, ledger = service_healing.verify_healing_contract(
        service, contract_id=contract_id, server=server, job_name=source_job, phase='convergence',
    )
    service_healing.precheck_retry_budget(service.workspace, contract, ledger, kind='scan')

    remote = None
    if target_server is not None:
        from vasp_agent.remote.remote_compute import RemoteCompute

        remote = RemoteCompute(service.workspace, target_server)

    source_ctx = checks.read_source_state(
        service, contract_id=contract_id, target_server=target_server, source_job=source_job, remote=remote,
    )
    checks.validate_source_contract_binding(service, contract, source_ctx, source_job=source_job)
    source_config = source_ctx['config']
    eligibility = checks.validate_convergence_eligibility(
        service, contract_id, source_job=source_job, evidence=source_ctx['evidence'],
        source_config=source_config, mode=mode, fixed_encut=fixed_encut,
    )
    checks.check_precision_bound_ceiling(service, contract, source_config, eligibility, source_job=source_job)

    if mode == 'mesh' and encuts is not None and [float(x) for x in encuts] != [float(x) for x in source_config['encuts']]:
        raise ContractError('CONTRACT_RETRY_NOT_STRICTER', 'mesh 模式下复用来源 ENCUT 序列，禁止修改 encuts。')
    if mode == 'encut' and meshes is not None and [[int(v) for v in m] for m in meshes] != [[int(v) for v in m] for m in source_config['meshes']]:
        raise ContractError('CONTRACT_RETRY_NOT_STRICTER', 'encut 模式下复用来源 k 网格序列，禁止修改 meshes。')

    potential_floor = checks.compute_potential_floor(service.workspace, contract, source_config, source_ctx['potential_metadata'])
    if fixed_encut is not None and float(fixed_encut) < potential_floor:
        raise ContractError('CONTRACT_RETRY_NOT_STRICTER', f'fixed_encut={fixed_encut} 低于赝势策略下限 {potential_floor} eV。')

    validated = task_contract.validate_stricter_retry(
        contract, source_encuts=source_config['encuts'], source_meshes=source_config['meshes'],
        new_encuts=encuts, new_meshes=meshes, mode=mode, potential_floor=potential_floor,
        scan_thresholds=scan_thresholds, source_scan_thresholds=source_config.get('scan_thresholds'),
        scan_preset=scan_preset, source_scan_preset=source_config.get('scan_preset') or 'strict',
    )

    tests, envelope = planned_tests(mode, fixed_encut), contract['resource_envelope']
    if resources is None:
        target_res = dict(envelope['phase_defaults']['convergence'])
        target_res['parallel_tests'] = min(int(target_res['parallel_tests']), tests)
    else:
        target_res = resources
    validated_res = contract_rules.validate_resources_in_envelope(target_res, 'convergence', envelope, server=target_server, tests=tests)
    submission_dialog.validate_resources(validated_res, 'convergence', rec.limits_for(target_server or 'local', validated_res), tests=tests)

    base_name = job_name if job_name is not None else _default_retry_base_name(source_job)
    taken = fork_naming.existing_jobs(service.box, target_server) | {source_job, *(ledger.get('allowed_jobs') or [])}
    new_job_name, name_conflicted, recovered = _create_retry_dir(
        service, contract=contract, target_server=target_server, remote=remote, source_job=source_job,
        source_ctx=source_ctx, base_name=base_name, taken=taken, mode=mode, validated=validated, fixed_encut=fixed_encut,
    )

    try:
        physics_record = unified_confirm.save_physics_confirmation(
            service.box, target_server, new_job_name, contract['physics_assertion']['goal'],
            'operator_task_contract', contract_id=contract_id,
            contract_sha256=contract['approval']['canonical_sha256'],
        )
    except Exception as exc:
        drift = rec.detect_drift_code(getattr(exc, 'error_code', None), str(exc))
        service.box.log({'control_prepare_scan_retry_error': {'contract_id': contract_id, 'server': target_server,
                         'source_job': source_job, 'job_name': new_job_name, 'stage': 'confirm_physics', 'error': scrub(str(exc))[:300]}})
        if drift is not None:
            raise rec.halt_on_drift(service, contract_id, drift, str(exc), details={'phase': 'convergence', 'source_job': source_job, 'job_name': new_job_name}) from exc
        raise core.ToolError('SCAN_RETRY_POST_PREPARE_FAILED', f'新重扫目录 {new_job_name} 已创建，但写入物理模型确认失败（{scrub(str(exc))[:200]}）；该目录未加入合同 allowed_jobs，请核查。') from exc
    if not isinstance(physics_record, dict) or physics_record.get('dimension') != contract['scope']['dimension']:
        raise rec.halt_on_drift(
            service, contract_id, 'CONTRACT_TAMPERED',
            f'新重扫目录 {new_job_name} 物理确认维度 {physics_record.get("dimension") if isinstance(physics_record, dict) else None!r} 与合同不一致。',
            details={'phase': 'convergence', 'source_job': source_job, 'job_name': new_job_name},
        )

    diag_entry: dict[str, Any] = {
        'server': target_server, 'source_job': source_job, 'new_job_name': new_job_name, 'mode': mode,
        'source_encuts': source_config['encuts'], 'source_meshes': source_config['meshes'],
        'new_encuts': validated['encuts'], 'new_meshes': validated['meshes'], 'fixed_encut': fixed_encut,
        'scan_preset': validated['scan_preset'], 'scan_thresholds': validated['scan_thresholds'],
        'failed_parameters': eligibility['failed_parameters'], 'recovered': recovered,
        **({'reason': scrub(reason)[:300]} if reason else {}),
        **({'evidence_sha256': evidence_sha256} if evidence_sha256 else {}),
    }
    ledger_warning, cur_ledger = None, None
    try:
        cur_ledger = task_contract.register_retry(service.workspace, contract_id, kind='scan', new_job_name=new_job_name, diagnostic=diag_entry)
    except Exception as exc:
        ledger_warning = {'error_code': getattr(exc, 'error_code', type(exc).__name__), 'message': scrub(str(exc))[:300]}
        with contextlib.suppress(Exception):
            cur_ledger = task_contract.read_ledger(service.workspace, contract_id)

    res_applied, res_changed, applied_resources, res_signature, resource_warning, post_drift = False, False, None, None, None, None
    if ledger_warning is None:
        try:
            res_info = service_execution._configure_phase_resources(
                service, target_server, new_job_name, 'convergence', validated_res, envelope, allow_missing_local_script=True,
            )
            if res_info.get('already_registered'):
                resource_warning = f'新目录 {new_job_name} 出现意外已登记提交回执，未覆写资源；请先核查。'
            else:
                res_applied, res_changed = bool(res_info.get('resources_applied', False)), bool(res_info.get('resources_changed', False))
                applied_resources, res_signature = res_info.get('resources'), res_info.get('signature')
        except Exception as exc:
            drift = rec.detect_drift_code(getattr(exc, 'error_code', None), str(exc))
            if drift is not None:
                post_drift = (drift, exc)
            else:
                resource_warning = f'新重扫目录 {new_job_name} 已准备完成，但资源配置失败（{scrub(str(exc))[:200]}）；可在提交前重试调整。'

    service.box.log({'control_prepare_scan_retry': {
        'contract_id': contract_id, 'server': target_server, 'source_job': source_job, 'job_name': new_job_name,
        'mode': mode, 'encuts': validated['encuts'], 'meshes': validated['meshes'], 'fixed_encut': fixed_encut,
        'scan_preset': validated['scan_preset'], 'recovered': recovered, 'name_conflicted': name_conflicted,
        'resources_applied': res_applied, 'resources_changed': res_changed, 'time': datetime.now(timezone.utc).isoformat(),
        **({'ledger_warning': ledger_warning} if ledger_warning else {}),
        **({'resource_warning': resource_warning} if resource_warning else {}),
        **({'drift_code': post_drift[0]} if post_drift is not None else {}),
    }})
    if post_drift is not None:
        raise rec.halt_on_drift(service, contract_id, post_drift[0], str(post_drift[1]),
                                details={'phase': 'convergence', 'source_job': source_job, 'job_name': new_job_name}) from post_drift[1]
    next_msg = (
        f'新重扫目录 {new_job_name} 已准备完成，但合同账本更新失败（{ledger_warning["error_code"]}），后续提交将被阻断；请人工核查。'
        if ledger_warning else
        f'已在不冲突新目录 {new_job_name} 完成严格加密重扫准备并沿用合同物理确认（未提交、未消耗提交额度）；请执行 vasp_get_stage_review 与阶段审阅后再调用 vasp_submit_stage。'
    )
    return {
        'contract_id': contract_id, 'server': target_server, 'source_job': source_job, 'job_name': new_job_name,
        'name_conflicted': name_conflicted, 'phase': 'convergence', 'mode': mode,
        'prepared': True, 'submitted': False, 'recovered': recovered, 'source_convergence': eligibility,
        'encuts': validated['encuts'], 'meshes': validated['meshes'], 'fixed_encut': fixed_encut,
        'scan_preset': validated['scan_preset'], 'scan_thresholds': validated['scan_thresholds'],
        'potential_floor': potential_floor, 'planned_tests': tests, 'physics_confirmed': True,
        'physics': {
            **{k: physics_record.get(k) for k in ('goal', 'dimension', 'confirmed_by', 'structure_sha256')},
            **{k: physics_record[k] for k in ('contract_id', 'contract_sha256') if k in physics_record},
        },
        'resources_applied': res_applied, 'resources_changed': res_changed,
        'resources': applied_resources, 'resource_signature': res_signature,
        'ledger': service_healing.healing_ledger_brief(cur_ledger, contract) if cur_ledger is not None else None,
        'next': next_msg,
        **({'ledger_warning': ledger_warning} if ledger_warning else {}),
        **({'resource_warning': resource_warning} if resource_warning else {}),
    }
