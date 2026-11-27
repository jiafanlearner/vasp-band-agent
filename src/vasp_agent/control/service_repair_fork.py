"""Layer 4 contract-gated bounded repair fork (`vasp_prepare_repair_fork`).

Strictly preserves frozen `ENGINE_MODULES` / `WORKER_UNITS` by orchestrating clean
re-preparation (`fresh_restart`) or frozen upstream-reuse forks (`production_from_convergence`,
`bands_from_scf`, `dos_from_scf`) into fresh `<job>_fix1` / `_2` directories while enforcing
structured diagnostic records, contract envelopes/bounds/physics immutability, repair &
consecutive-failure circuit breakers, and post-fork re-review.
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
from vasp_agent.control import repair_fork_checks as checks
from vasp_agent.control import repair_fork_plans as plans
from vasp_agent.control import service_execution, service_healing
from vasp_agent.permissions import contract_rules, submission_dialog, task_contract, unified_confirm
from vasp_agent.permissions.contract_rules import ContractError
from vasp_agent.planning import parameter_plans
from vasp_agent.workflow import band_path as band_path_mod
from vasp_agent.workflow import convergence_reuse, dos_stage, full_workflow, stage_gate
import vasp_agent.workflow.vasp_band_agent as core

_FIX_SUFFIX_RE = re.compile(r'^(.+)_fix([1-9][0-9]*)$')
_MAX_NAME_ATTEMPTS = 5


def _default_repair_base_name(failed_job: str) -> str:
    """Derive `<job>_fix1` (or increment `<stem>_fixN` -> `<stem>_fix(N+1)`) within the 64-char JOB limit."""
    match = _FIX_SUFFIX_RE.fullmatch(failed_job)
    stem, suffix = (match.group(1), f'_fix{int(match.group(2)) + 1}') if match else (failed_job, '_fix1')
    base = stem[:64 - len(suffix)] + suffix
    if not fork_naming.JOB_NAME_RE.fullmatch(base):
        raise ValueError(f'无法为来源任务 {failed_job} 生成合法修复目录名。')
    return base


def _is_dir_collision_error(exc: BaseException) -> bool:
    if fork_naming.is_dir_exists_error(exc):
        return True
    return (
        isinstance(exc, ValueError)
        and getattr(exc, 'error_code', 'REMOTE_ERROR') in ('REMOTE_ERROR', 'TOOL_ERROR')
        and '工作目录非空' in str(exc)
    )


def _run_remote_create(
    service: Any, *, target_server: str, remote: Any, candidate_job: str, source_job: str, strategy: str, plan_spec: dict[str, Any],
) -> None:
    service.box.ensure_current_release(target_server)
    if strategy == 'fresh_restart':
        req = plan_spec['request']
        fields: dict[str, Any] = {
            'encut': req['encut'], 'mesh': req['mesh'], 'nbands': plan_spec['nbands'], 'relax_mode': req['relax_mode'],
            'parallel_tests': req.get('parallel_tests'), 'cores_per_test': req.get('cores_per_test'),
            'encuts': plan_spec['encuts'], 'meshes': plan_spec['meshes'], 'parameter_choice': plan_spec['parameter_choice'],
            'scan_preset': plan_spec['scan_preset'],
        }
        if plan_spec['scan_thresholds']:
            fields['scan_thresholds'] = plan_spec['scan_thresholds']
        if req.get('potential_variants'):
            fields['potential_variants'] = req['potential_variants']
        if req.get('band_path') is not None:
            fields['band_path'] = req['band_path']
        if plan_spec['optional_stages']:
            fields['optional_stages'] = plan_spec['optional_stages']
        if plan_spec['stage_parameters']:
            fields['stage_parameters'] = plan_spec['stage_parameters']
        remote.prepare(candidate_job, req['structure'], **fields, preview_only=False, environment_profile=plan_spec['environment_payload'])
    elif strategy == 'production_from_convergence':
        kwargs = {'encut': plan_spec['encut']} if plan_spec['encut'] is not None else {}
        remote.call(
            'prepare_production_from_convergence', candidate_job,
            source_job=source_job, mesh=plan_spec['mesh'], source_snapshot=plan_spec['source_snapshot'], **kwargs,
        )
    elif strategy == 'bands_from_scf':
        remote.call('prepare_bands_path', candidate_job, source_job=source_job, band_path=plan_spec['band_path'])
    elif strategy == 'dos_from_scf':
        remote.call('prepare_dos_fork', candidate_job, source_job=source_job, values=plan_spec['dos_values'])


def _run_local_create(
    service: Any, *, candidate_job: str, source_job: str, strategy: str, plan_spec: dict[str, Any],
) -> None:
    target_dir = rec.reject_symlink_rel(service.workspace, f'runs/{candidate_job}', f'目标修复目录 {candidate_job}')
    if strategy == 'fresh_restart':
        target_dir.mkdir(parents=True, exist_ok=False)
        try:
            req = {**plan_spec['request'], 'job_name': candidate_job, 'nbands': plan_spec['nbands'], 'scan_preset': plan_spec['scan_preset']}
            args = parameter_plans.local_args(service.box, req)
            args.preview_only = False
            args.optional_stages = plan_spec['optional_stages'] or None
            args.stage_parameters = plan_spec['stage_parameters']
            args.encuts = plan_spec['encuts']
            args.meshes = plan_spec['meshes']
            args.scan_thresholds = plan_spec['scan_thresholds']
            args.parameter_choice = plan_spec['parameter_choice']
            with contextlib.redirect_stdout(io.StringIO()):
                full_workflow.prepare_full(args)
        except Exception:
            with contextlib.suppress(OSError):
                target_dir.rmdir()
            raise
        return
    source_dir = rec.reject_symlink_rel(service.workspace, f'runs/{source_job}', f'来源任务目录 {source_job}')
    if target_dir.exists():
        raise FileExistsError(f'目标目录 {candidate_job} 已存在。')
    with contextlib.redirect_stdout(io.StringIO()):
        if strategy == 'production_from_convergence':
            convergence_reuse.prepare_production(
                source_dir, target_dir, plan_spec['mesh'], plan_spec['source_snapshot'], encut=plan_spec['encut'],
            )
        elif strategy == 'bands_from_scf':
            band_path_mod.prepare_bands_path(source_dir, target_dir, plan_spec['band_path'])
        elif strategy == 'dos_from_scf':
            dos_stage.prepare_dos_fork(source_dir, target_dir, plan_spec['dos_values'])


def _create_repair_dir(
    service: Any, *, contract: dict[str, Any], target_server: str | None, remote: Any | None,
    source_job: str, source_ctx: dict[str, Any], phase: str, strategy: str,
    plan_spec: dict[str, Any], base_name: str, taken: set[str],
) -> tuple[str, bool, bool]:
    contract_id = contract['contract_id']
    name_conflicted = False
    for _attempt in range(_MAX_NAME_ATTEMPTS):
        candidate_job, conflicted = fork_naming.resolve_name(base_name, taken)
        name_conflicted = name_conflicted or conflicted
        try:
            if target_server is not None:
                assert remote is not None
                _run_remote_create(
                    service, target_server=target_server, remote=remote, candidate_job=candidate_job,
                    source_job=source_job, strategy=strategy, plan_spec=plan_spec,
                )
            else:
                _run_local_create(
                    service, candidate_job=candidate_job, source_job=source_job, strategy=strategy, plan_spec=plan_spec,
                )
            return candidate_job, name_conflicted, False
        except FileExistsError:
            taken.add(candidate_job)
            name_conflicted = True
            continue
        except Exception as exc:
            if _is_dir_collision_error(exc):
                taken.add(candidate_job)
                name_conflicted = True
                continue
            if target_server is not None and service_healing.is_transport_disconnect(exc):
                assert remote is not None
                reconciled = plans.reconcile_fork_disconnect(
                    remote, contract=contract, source_job=source_job, source_ctx=source_ctx,
                    candidate_job=candidate_job, strategy=strategy, plan_spec=plan_spec, exc=exc,
                )
                return candidate_job, name_conflicted, bool(reconciled.get('recovered', False))
            raise checks.handle_error(service, contract_id, exc, source_job=source_job, phase=phase) from exc
    raise core.ToolError('REPAIR_FORK_NAME_CONFLICT', f'目录名 {base_name} 连续冲突，未能分配新修复目录。')


def _verify_fresh_potcar(
    service: Any, *, contract_id: str, target_server: str | None, remote: Any | None,
    source_job: str, source_ctx: dict[str, Any], new_job_name: str, phase: str,
) -> None:
    expected_pot = source_ctx.get('potcar_sha256')
    if not expected_pot:
        return
    if target_server is None:
        pot_path = rec.reject_symlink_rel(service.workspace, f'runs/{new_job_name}/POTCAR', f'新目录 {new_job_name} POTCAR')
        actual_pot = core.digest(pot_path) if pot_path.is_file() else None
    else:
        assert remote is not None
        ctx = remote.call('review_context', new_job_name)
        manifest = ctx.get('manifest') if isinstance(ctx, dict) else None
        hashes = manifest.get('hashes') if isinstance(manifest, dict) and isinstance(manifest.get('hashes'), dict) else {}
        actual_pot = hashes.get('POTCAR')
    if actual_pot != expected_pot:
        raise rec.halt_on_drift(
            service, contract_id, 'INPUT_HASH_MISMATCH',
            f'新修复目录 {new_job_name} 的 POTCAR 哈希与来源任务 {source_job} 不一致，疑似赝势库漂移。',
            details={'phase': phase, 'source_job': source_job, 'job_name': new_job_name},
        )


def _confirm_or_verify_physics(
    service: Any, *, contract: dict[str, Any], target_server: str | None, remote: Any | None,
    source_job: str, source_ctx: dict[str, Any], new_job_name: str, phase: str, strategy: str,
) -> dict[str, Any]:
    cid = contract['contract_id']
    if strategy == 'fresh_restart':
        _verify_fresh_potcar(
            service, contract_id=cid, target_server=target_server, remote=remote,
            source_job=source_job, source_ctx=source_ctx, new_job_name=new_job_name, phase=phase,
        )
        try:
            phys = unified_confirm.save_physics_confirmation(
                service.box, target_server, new_job_name, contract['physics_assertion']['goal'], 'operator_slash_command',
            )
        except Exception as exc:
            drift = checks.detect_drift_code(getattr(exc, 'error_code', None), str(exc))
            service.box.log({'control_prepare_repair_fork_error': {
                'contract_id': cid, 'server': target_server, 'source_job': source_job,
                'job_name': new_job_name, 'stage': 'confirm_physics', 'error': scrub(str(exc))[:300],
            }})
            if drift is not None:
                raise rec.halt_on_drift(service, cid, drift, str(exc), details={'phase': phase, 'source_job': source_job, 'job_name': new_job_name}) from exc
            raise core.ToolError(
                'REPAIR_FORK_POST_PREPARE_FAILED',
                f'新修复目录 {new_job_name} 已创建，但写入物理模型确认失败（{scrub(str(exc))[:200]}）；该目录未加入合同 allowed_jobs，请核查。',
            ) from exc
        checks.validate_physics_record_matches(service, contract, phys, job_name=new_job_name, phase=phase, required=True)
        return phys

    try:
        if target_server is None:
            new_dir = rec.reject_symlink_rel(service.workspace, f'runs/{new_job_name}', f'新修复目录 {new_job_name}')
            rec.reject_symlink_rel(service.workspace, f'runs/{new_job_name}/physics-model.json', '新修复目录物理确认')
            phys = stage_gate.physics(new_dir)
        else:
            assert remote is not None
            new_status = remote.call('status', new_job_name)
            phys = new_status.get('physics-model.json') if isinstance(new_status, dict) else None
    except Exception as exc:
        drift = checks.detect_drift_code(getattr(exc, 'error_code', None), str(exc))
        if drift is not None:
            raise rec.halt_on_drift(service, cid, drift, str(exc), details={'phase': phase, 'job_name': new_job_name}) from exc
        raise core.ToolError(
            'REPAIR_FORK_POST_PREPARE_FAILED',
            f'新修复目录 {new_job_name} 已创建，但核验继承的物理确认失败（{scrub(str(exc))[:200]}）；该目录未加入合同 allowed_jobs，请核查。',
        ) from exc
    checks.validate_physics_record_matches(service, contract, phys, job_name=new_job_name, phase=phase, required=True)
    return phys


def prepare_repair_fork(
    service: Any, *, contract_id: str, source_job: str, phase: str, strategy: str,
    failure_category: str, evidence_sha256: str, repair_hypothesis: str, applied_change: str,
    server: str | None = None, failed_job: str | None = None, job_name: str | None = None,
    resources: dict[str, Any] | None = None, nbands: int | None = None,
    production_mesh: list[int] | None = None, production_encut: float | None = None,
    band_path: str | None = None, dos_values: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Contract-gated bounded repair fork into a fresh `<job>_fix1` / `_2` directory."""
    if job_name is not None:
        contract_rules.validate_job_name(job_name)
    effective_failed = source_job if failed_job is None else contract_rules.validate_job_name(failed_job)
    diag_fields = checks.validate_diagnostic_fields(
        failure_category=failure_category, evidence_sha256=evidence_sha256,
        repair_hypothesis=repair_hypothesis, applied_change=applied_change,
    )
    target_server, contract, ledger = service_healing.verify_healing_contract(
        service, contract_id=contract_id, server=server, job_name=source_job, phase=phase,
    )
    if effective_failed not in ledger['allowed_jobs']:
        raise ContractError('CONTRACT_SCOPE_VIOLATION', f'失败任务 {effective_failed} 不在合同允许目录清单中。')

    if failure_category == 'incar_numerical_unsupported':
        prior_count = len(ledger.get('diagnostics') or [])
        halt_msg = (
            f'阶段 {phase} 诊断为冻结引擎未暴露的 VASP INCAR 数值标签故障（如 ALGO/AMIX/POTIM/IBRION），'
            f'合同内资源/派生重跑无法解决（历轮诊断 {prior_count} 条）；合同已自动熔断交人工审核（白名单 INCAR 恢复档位留待 P3）。'
        )
        task_contract.halt_contract(
            service.workspace, contract_id, halt_msg, code='REPAIR_UNSUPPORTED_INCAR_RECOVERY', uncertain=False,
            details={'server': target_server, 'source_job': source_job, 'failed_job': effective_failed,
                     'phase': phase, 'strategy': strategy, 'diagnostic': diag_fields, 'prior_diagnostics_count': prior_count},
            actor=f'{service.role}:{service.actor_id}',
        )
        service.box.log({'control_prepare_repair_fork_halted': {
            'contract_id': contract_id, 'code': 'REPAIR_UNSUPPORTED_INCAR_RECOVERY', 'phase': phase,
            'source_job': source_job, 'failed_job': effective_failed, 'diagnostic': diag_fields,
        }})
        raise ContractError('REPAIR_UNSUPPORTED_INCAR_RECOVERY', halt_msg)

    target_phase = checks.validate_strategy_and_params(
        contract, source_job=source_job, failed_job=effective_failed, phase=phase, strategy=strategy,
        resources=resources, nbands=nbands, production_mesh=production_mesh,
        production_encut=production_encut, band_path=band_path, dos_values=dos_values,
    )
    budget_diag = {'server': target_server, 'source_job': source_job, 'failed_job': effective_failed,
                   'phase': phase, 'target_phase': target_phase, 'strategy': strategy, **diag_fields}
    budget = contract['budget']
    if (
        int(ledger.get('submissions_used', 0)) >= int(budget['max_submissions'])
        or int(ledger.get('consecutive_failures', 0)) >= int(budget['max_consecutive_failures'])
        or int(ledger.get('repair_retries_used', 0)) >= int(budget['max_repair_retries'])
    ):
        try:
            task_contract.register_retry(service.workspace, contract_id, kind='repair', diagnostic=budget_diag)
        except ContractError as exc:
            prior = list(ledger.get('diagnostics') or [])
            service.box.log({'control_prepare_repair_fork_halted': {
                'contract_id': contract_id, 'code': exc.error_code, 'phase': phase, 'source_job': source_job,
                'failed_job': effective_failed, 'diagnostic': diag_fields, 'prior_diagnostics': prior,
            }})
            raise ContractError(
                exc.error_code,
                f'{exc}（已留存历轮诊断 {len(prior) + 1} 条；请通过 vasp_contract_status 查看完整记录并由操作者人工审核）。',
            ) from exc

    remote = None
    if target_server is not None:
        from vasp_agent.remote.remote_compute import RemoteCompute

        remote = RemoteCompute(service.workspace, target_server)

    failed_status = checks.read_job_status(
        service, contract_id=contract_id, target_server=target_server, job_name=effective_failed, phase=phase, remote=remote,
    )
    failure_meta = checks.validate_failed_phase_eligibility(
        failed_job=effective_failed, phase=phase, failure_category=failure_category,
        resources=resources, status=failed_status, is_remote=(target_server is not None),
    )
    failed_job_id = failure_meta['failed_job_id']
    checks.ensure_failure_not_already_repaired(ledger, failed_job=effective_failed, phase=phase, failed_job_id=failed_job_id)

    source_ctx = checks.read_source_context(
        service, contract_id=contract_id, target_server=target_server, source_job=source_job, phase=phase,
        remote=remote, cached_status=failed_status if source_job == effective_failed else None,
    )
    checks.validate_source_binding(
        service, contract, source_ctx, source_job=source_job, phase=phase, require_physics=(strategy != 'fresh_restart'),
    )
    if effective_failed != source_job:
        failed_ctx = checks.read_source_context(
            service, contract_id=contract_id, target_server=target_server, source_job=effective_failed,
            phase=phase, remote=remote, cached_status=failed_status,
        )
        checks.validate_cross_job_provenance(
            service, contract, target_server=target_server, source_job=source_job, failed_job=effective_failed,
            phase=phase, strategy=strategy, source_ctx=source_ctx, failed_ctx=failed_ctx,
        )

    plan_spec = plans.prepare_strategy_plan(
        service, contract, source_ctx, target_server=target_server, remote=remote, source_job=source_job,
        phase=phase, strategy=strategy, failure_category=failure_category, nbands=nbands,
        production_mesh=production_mesh, production_encut=production_encut, band_path=band_path, dos_values=dos_values,
    )

    tests, envelope = plan_spec['planned_tests'], contract['resource_envelope']
    if resources is None:
        target_res = dict(envelope['phase_defaults'][target_phase])
        if target_phase == 'convergence':
            target_res['parallel_tests'] = min(int(target_res['parallel_tests']), tests)
    else:
        target_res = resources
    validated_res = contract_rules.validate_resources_in_envelope(target_res, target_phase, envelope, server=target_server, tests=tests)
    submission_dialog.validate_resources(validated_res, target_phase, rec.limits_for(target_server or 'local', validated_res), tests=tests)

    already_counted = bool(
        failed_job_id and service_healing.failure_already_recorded(ledger, job_name=effective_failed, phase=phase, job_id=failed_job_id)
    )
    max_failures = int(budget['max_consecutive_failures'])
    failure_info = {
        'source': 'prepare_repair_fork', 'strategy': strategy, **diag_fields,
        **({'job_id': failed_job_id} if failed_job_id else {}),
    }
    failure_rec = None if already_counted else {'job_name': effective_failed, 'phase': phase, 'failure_info': failure_info}
    if failure_rec is not None and int(ledger['consecutive_failures']) + 1 >= max_failures:
        try:
            task_contract.register_retry(service.workspace, contract_id, kind='repair', diagnostic=budget_diag, record_failure=failure_rec)
        except ContractError as exc:
            prior = list(ledger.get('diagnostics') or [])
            service.box.log({'control_prepare_repair_fork_halted': {
                'contract_id': contract_id, 'code': exc.error_code, 'phase': phase, 'source_job': source_job,
                'failed_job': effective_failed, 'diagnostic': diag_fields, 'prior_diagnostics': prior,
            }})
            raise ContractError(
                'CONTRACT_CONSECUTIVE_FAILURES_EXCEEDED',
                f'阶段 {phase} 连续失败已达合同上限 {max_failures}（已汇总历轮诊断 {len(prior) + 1} 条），合同已自动熔断交人工审核（未创建派生修复目录）。',
            ) from exc

    base_name = job_name if job_name is not None else _default_repair_base_name(effective_failed)
    taken = fork_naming.existing_jobs(service.box, target_server) | {source_job, effective_failed, *(ledger.get('allowed_jobs') or [])}
    new_job_name, name_conflicted, recovered = _create_repair_dir(
        service, contract=contract, target_server=target_server, remote=remote, source_job=source_job,
        source_ctx=source_ctx, phase=phase, strategy=strategy, plan_spec=plan_spec, base_name=base_name, taken=taken,
    )

    physics_record = _confirm_or_verify_physics(
        service, contract=contract, target_server=target_server, remote=remote,
        source_job=source_job, source_ctx=source_ctx, new_job_name=new_job_name, phase=phase, strategy=strategy,
    )

    monitor_deactivated, monitor_warning = False, None
    if failed_job_id:
        try:
            monitor_deactivated = bool(
                service.box.monitor.deactivate(target_server or 'local', effective_failed, phase, failed_job_id, state=failure_meta['failed_state'])
            )
        except Exception as exc:
            monitor_warning = f'旧作业监控记录停用失败（{type(exc).__name__}）；请通过 vasp_watch_jobs 核查。'

    diag_entry: dict[str, Any] = {
        **budget_diag, 'new_job_name': new_job_name, 'recovered': recovered,
        **({'failed_job_id': failed_job_id} if failed_job_id else {}),
        **({'applied_parameters': plan_spec['applied_parameters']} if plan_spec.get('applied_parameters') else {}),
    }
    ledger_warning, cur_ledger = None, None
    try:
        cur_ledger = task_contract.register_retry(
            service.workspace, contract_id, kind='repair', new_job_name=new_job_name,
            diagnostic=diag_entry, record_failure=failure_rec,
        )
    except Exception as exc:
        ledger_warning = {'error_code': getattr(exc, 'error_code', type(exc).__name__), 'message': scrub(str(exc))[:300]}
        with contextlib.suppress(Exception):
            cur_ledger = task_contract.read_ledger(service.workspace, contract_id)

    res_applied, res_changed, applied_resources, res_signature, resource_warning, post_drift = False, False, None, None, None, None
    if ledger_warning is None:
        try:
            res_info = service_execution._configure_phase_resources(
                service, target_server, new_job_name, target_phase, validated_res, envelope,
                allow_missing_local_script=True, clamp_parallel_to_tests=(resources is None and target_phase == 'convergence'),
            )
            if res_info.get('already_registered'):
                resource_warning = f'新修复目录 {new_job_name} 出现意外已登记提交回执，未覆写资源；请先核查。'
            else:
                res_applied, res_changed = bool(res_info.get('resources_applied', False)), bool(res_info.get('resources_changed', False))
                applied_resources, res_signature = res_info.get('resources'), res_info.get('signature')
        except Exception as exc:
            drift = checks.detect_drift_code(getattr(exc, 'error_code', None), str(exc))
            if drift is not None:
                post_drift = (drift, exc)
            else:
                resource_warning = f'新修复目录 {new_job_name} 已准备完成，但资源配置失败（{scrub(str(exc))[:200]}）；可在提交前重试调整。'

    service.box.log({'control_prepare_repair_fork': {
        'contract_id': contract_id, 'server': target_server, 'source_job': source_job, 'failed_job': effective_failed,
        'job_name': new_job_name, 'phase': phase, 'target_phase': target_phase, 'strategy': strategy,
        'failure_category': failure_category, 'evidence_sha256': evidence_sha256, 'recovered': recovered,
        'name_conflicted': name_conflicted, 'monitor_deactivated': monitor_deactivated,
        'resources_applied': res_applied, 'resources_changed': res_changed, 'time': datetime.now(timezone.utc).isoformat(),
        **({'ledger_warning': ledger_warning} if ledger_warning else {}),
        **({'resource_warning': resource_warning} if resource_warning else {}),
        **({'drift_code': post_drift[0]} if post_drift is not None else {}),
    }})
    if post_drift is not None:
        raise rec.halt_on_drift(
            service, contract_id, post_drift[0], str(post_drift[1]),
            details={'phase': target_phase, 'source_job': source_job, 'job_name': new_job_name},
        ) from post_drift[1]

    next_msg = (
        f'新修复目录 {new_job_name} 已准备完成，但合同账本更新失败（{ledger_warning["error_code"]}），后续提交将被阻断；请人工核查。'
        if ledger_warning else
        f'已在不冲突新目录 {new_job_name} 完成派生修复准备（策略 {strategy}，待审阶段 {target_phase}，未提交、未消耗提交额度）；'
        '请执行 vasp_get_stage_review 与独立阶段审阅后再调用 vasp_submit_stage。'
    )
    return {
        'contract_id': contract_id, 'server': target_server, 'source_job': source_job, 'failed_job': effective_failed,
        'job_name': new_job_name, 'name_conflicted': name_conflicted, 'phase': phase, 'target_phase': target_phase,
        'strategy': strategy, 'prepared': True, 'submitted': False, 'recovered': recovered,
        'diagnostic': diag_fields, 'applied_parameters': plan_spec.get('applied_parameters') or {},
        'physics_confirmed': True,
        'physics': {k: physics_record.get(k) for k in ('goal', 'dimension', 'confirmed_by', 'structure_sha256')},
        'monitor_deactivated': monitor_deactivated, 'resources_applied': res_applied, 'resources_changed': res_changed,
        'resources': applied_resources, 'resource_signature': res_signature,
        'ledger': service_healing.healing_ledger_brief(cur_ledger, contract) if cur_ledger is not None else None,
        'next': next_msg,
        **({'monitor_warning': monitor_warning} if monitor_warning else {}),
        **({'ledger_warning': ledger_warning} if ledger_warning else {}),
        **({'resource_warning': resource_warning} if resource_warning else {}),
    }
