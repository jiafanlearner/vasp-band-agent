"""Read-only inspection, contract-binding, eligibility, ceiling, and disconnect checks for P1e-2."""
from __future__ import annotations

import json
import math
from pathlib import PurePosixPath
from typing import Any

from vasp_agent.chat.toolbox_core import scrub
from vasp_agent.control import execution_reconcile as rec
from vasp_agent.control import service_healing
from vasp_agent.permissions import contract_rules, task_contract
from vasp_agent.permissions.contract_rules import ContractError
from vasp_agent.workflow import (
    convergence_reuse, input_review, scan_config, solver_overrides as solver_ov, stage_definitions, workflow_policy,
)
import vasp_agent.workflow.vasp_band_agent as core

_PRESET_RANK = {'standard': 0, 'strict': 1}
_DEFAULT_THRESHOLDS = {'energy_eV_per_atom': 0.001, 'force_eV_per_A': 0.005, 'stress_kbar': 0.5}


def handle_drift_or_running(service: Any, contract_id: str, exc: Exception, *, source_job: str) -> Exception:
    msg = str(exc)
    drift = rec.detect_drift_code(getattr(exc, 'error_code', None), msg)
    if drift is not None:
        return rec.halt_on_drift(service, contract_id, drift, msg, details={'phase': 'convergence', 'source_job': source_job})
    if '运行锁' in msg or '仍在运行' in msg:
        return core.ToolError(
            'SCAN_RETRY_SOURCE_RUNNING',
            f'来源任务 {source_job} 仍在运行，暂不能发起收敛加密重扫：{scrub(msg)[:300]}',
            {'action': 'poll_vasp_watch_jobs', 'job_name': source_job, 'phase': 'convergence'},
        )
    return exc


def _check_source_live_guards(source_job: str, conv_sub: Any, ckpt: Any) -> None:
    if conv_sub is not None and (not isinstance(conv_sub, dict) or conv_sub.get('status') != 'submitted'):
        st = conv_sub.get('status') if isinstance(conv_sub, dict) else type(conv_sub).__name__
        raise core.ToolError('SCAN_RETRY_SOURCE_UNCERTAIN', f'来源任务 convergence 存在非 confirmed 提交回执（status={st}），请先核查。')
    if isinstance(ckpt, dict) and 'convergence' in (ckpt.get('completed') or []):
        raise core.ToolError('SCAN_RETRY_ALREADY_PASSED', f'来源任务 {source_job} 已完成 convergence 检查点，无需加密重扫。')


def read_source_state(
    service: Any, *, contract_id: str, target_server: str | None, source_job: str, remote: Any | None,
) -> dict[str, Any]:
    """Read-only inspection of source convergence evidence, config, metadata, provenance, and live status."""
    if target_server is None:
        source_dir = rec.reject_symlink_rel(service.workspace, f'runs/{source_job}', f'来源任务目录 {source_job}')
        if (source_dir / '.task-archived.json').exists():
            raise core.ToolError('SCAN_RETRY_NOT_ELIGIBLE', f'来源任务 {source_job} 已归档，无法发起收敛加密重扫。')
        if (source_dir / '.agent.lock').exists():
            raise core.ToolError(
                'SCAN_RETRY_SOURCE_RUNNING', f'来源任务 {source_job} 仍有运行锁，请等待收敛完成后再发起加密重扫。',
                {'action': 'poll_vasp_watch_jobs', 'job_name': source_job, 'phase': 'convergence'},
            )
        conv_receipt = rec.reject_symlink_rel(service.workspace, f'runs/{source_job}/submissions/convergence.json', '来源提交回执')
        conv_sub: Any = None
        if conv_receipt.exists():
            try:
                conv_sub = json.loads(conv_receipt.read_text(encoding='utf-8'))
            except (OSError, ValueError) as exc:
                raise core.ToolError('SCAN_RETRY_SOURCE_UNCERTAIN', f'来源任务 convergence 提交回执损坏：{scrub(str(exc))[:200]}') from exc
        ckpt_path = rec.reject_symlink_rel(service.workspace, f'runs/{source_job}/checkpoint.json', '来源检查点')
        ckpt: Any = None
        if ckpt_path.exists():
            try:
                ckpt = json.loads(ckpt_path.read_text(encoding='utf-8'))
            except (OSError, ValueError) as exc:
                raise core.ToolError('SCAN_RETRY_SOURCE_UNCERTAIN', f'来源任务 checkpoint.json 损坏：{scrub(str(exc))[:200]}') from exc
            if not isinstance(ckpt, dict):
                raise core.ToolError('SCAN_RETRY_SOURCE_UNCERTAIN', '来源任务 checkpoint.json 结构无效。')
        _check_source_live_guards(source_job, conv_sub, ckpt)
        try:
            evidence, review_ctx = convergence_reuse.inspect(source_dir), input_review.context(source_dir)
        except Exception as exc:
            raise handle_drift_or_running(service, contract_id, exc, source_job=source_job) from exc
    else:
        assert remote is not None
        try:
            status = remote.call('status', source_job)
        except Exception as exc:
            raise handle_drift_or_running(service, contract_id, exc, source_job=source_job) from exc
        if isinstance(status, dict):
            array_tasks = status.get('array_tasks')
            if status.get('queue') or (isinstance(array_tasks, dict) and array_tasks.get('all_terminal') is False):
                raise core.ToolError(
                    'SCAN_RETRY_SOURCE_RUNNING', f'来源任务 {source_job} 仍在 Slurm 队列或数组未全部终态，请待结束后再发起加密重扫。',
                    {'action': 'poll_vasp_watch_jobs', 'job_name': source_job, 'phase': 'convergence'},
                )
            if status.get('scheduler_status_known') is False:
                raise core.ToolError('SCAN_RETRY_SOURCE_UNCERTAIN', f'来源任务 {source_job} 调度状态未知，暂不能发起加密重扫；请先诊断核查。')
            stage_subs = status.get('stage_submissions') if isinstance(status.get('stage_submissions'), dict) else {}
            _check_source_live_guards(source_job, stage_subs.get('convergence'), status.get('checkpoint.json'))
        try:
            evidence, review_ctx = remote.call('inspect_convergence', source_job), remote.call('review_context', source_job)
        except Exception as exc:
            raise handle_drift_or_running(service, contract_id, exc, source_job=source_job) from exc

    if not isinstance(evidence, dict) or not isinstance(review_ctx, dict) or not isinstance(review_ctx.get('config'), dict):
        raise core.ToolError('SCAN_RETRY_INSPECTION_INVALID', '来源收敛核验或审阅配置响应无效，未创建新任务。')
    manifest = review_ctx.get('manifest') if isinstance(review_ctx.get('manifest'), dict) else {}
    hashes = manifest.get('hashes') if isinstance(manifest.get('hashes'), dict) else {}
    return {
        'evidence': evidence,
        'config': review_ctx['config'],
        'potential_metadata': review_ctx.get('potential_metadata'),
        'initial_poscar': review_ctx.get('initial_poscar'),
        'potcar_sha256': hashes.get('POTCAR'),
        'environment': review_ctx.get('environment'),
    }


def validate_source_contract_binding(
    service: Any, contract: dict[str, Any], source_ctx: dict[str, Any], *, source_job: str,
) -> None:
    """Verify source_config dimension, environment_profile, and scan thresholds never drift below contract."""
    cid, cfg = contract['contract_id'], source_ctx['config']
    src_dim = (cfg.get('dimensionality') or {}).get('dimension') or '3D'
    if src_dim != contract['scope']['dimension']:
        raise rec.halt_on_drift(
            service, cid, 'CONTRACT_TAMPERED',
            f'来源任务维度 {src_dim} 与合同声明维度 {contract["scope"]["dimension"]} 不一致。',
            details={'phase': 'convergence', 'source_job': source_job},
        )

    bound = contract['scope'].get('environment_profile')
    if isinstance(bound, dict):
        src_prof = cfg.get('environment_profile')
        if (
            not isinstance(src_prof, dict)
            or src_prof.get('name') != bound['name']
            or src_prof.get('environment_sha256') != bound['environment_sha256']
            or src_prof.get('executable_sha256') != bound['executable_sha256']
        ):
            raise rec.halt_on_drift(
                service, cid, 'ENVIRONMENT_PROFILE_DRIFT',
                f'来源任务 {source_job} 的 environment_profile 与合同绑定环境 {bound["name"]} 不一致。',
                details={'phase': 'convergence', 'source_job': source_job},
            )
        src_env = source_ctx.get('environment')
        if isinstance(src_env, dict):
            if (
                (src_env.get('environment_sha256') is not None and src_env.get('environment_sha256') != bound['environment_sha256'])
                or (src_env.get('executable_sha256') is not None and src_env.get('executable_sha256') != bound['executable_sha256'])
            ):
                raise rec.halt_on_drift(
                    service, cid, 'ENVIRONMENT_PROFILE_DRIFT',
                    f'来源任务 {source_job} 环境指纹与合同绑定环境 {bound["name"]} 哈希漂移。',
                    details={'phase': 'convergence', 'source_job': source_job},
                )

    src_preset = cfg.get('scan_preset') or 'strict'
    ctr_preset = contract['workflow']['scan_preset']
    if src_preset not in _PRESET_RANK or _PRESET_RANK[src_preset] < _PRESET_RANK[ctr_preset]:
        raise rec.halt_on_drift(
            service, cid, 'CONTRACT_TAMPERED',
            f'来源任务扫描档位 {src_preset!r} 宽松于或偏离合同基准 {ctr_preset!r}。',
            details={'phase': 'convergence', 'source_job': source_job},
        )
    try:
        src_th = workflow_policy.validate_scan_thresholds(cfg.get('scan_thresholds'))
    except ValueError as exc:
        raise rec.halt_on_drift(service, cid, 'CONTRACT_TAMPERED', f'来源任务扫描阈值无效：{exc}', details={'phase': 'convergence', 'source_job': source_job}) from exc
    eff_ctr = {**_DEFAULT_THRESHOLDS, **workflow_policy.SCAN_PRESETS.get(ctr_preset, {})}
    eff_src = {**_DEFAULT_THRESHOLDS, **workflow_policy.SCAN_PRESETS.get(src_preset, {}), **(src_th or {})}
    if any(eff_src[k] > eff_ctr[k] for k in _DEFAULT_THRESHOLDS):
        raise rec.halt_on_drift(
            service, cid, 'CONTRACT_TAMPERED',
            f'来源任务 {source_job} 的有效扫描阈值宽松于合同基准 {ctr_preset}，拒绝继承宽松阈值。',
            details={'phase': 'convergence', 'source_job': source_job},
        )


def validate_convergence_eligibility(
    service: Any, contract_id: str, *, source_job: str, evidence: dict[str, Any],
    source_config: dict[str, Any], mode: str, fixed_encut: float | None,
) -> dict[str, Any]:
    """Ensure source convergence genuinely finished and failed on convergence thresholds (not crashes)."""
    try:
        src_scans = scan_config.validate_scans(list(source_config['encuts']), [list(m) for m in source_config['meshes']])
    except (KeyError, TypeError, ValueError) as exc:
        raise core.ToolError('SCAN_RETRY_INSPECTION_INVALID', f'来源 config.json 扫描序列无效：{scrub(str(exc))[:200]}') from exc

    params = evidence.get('parameters')
    if not isinstance(params, dict):
        raise core.ToolError('SCAN_RETRY_INSPECTION_INVALID', 'inspect_convergence 缺少 parameters 字段。')
    encut_info, mesh_info, cross_info = params.get('encut'), params.get('mesh'), params.get('cross_cutoff')
    if not isinstance(encut_info, dict) or not isinstance(mesh_info, dict):
        raise core.ToolError('SCAN_RETRY_INSPECTION_INVALID', 'inspect_convergence 缺少 encut 或 mesh 核验结果。')
    if source_config.get('fixed_encut') is not None and not isinstance(cross_info, dict):
        raise core.ToolError('SCAN_RETRY_INSPECTION_INVALID', '来源启用了 fixed_encut 但 inspect_convergence 缺少 cross_cutoff 核验结果。')

    entries = [('encut', encut_info), ('mesh', mesh_info)] + ([('cross_cutoff', cross_info)] if isinstance(cross_info, dict) else [])
    for kind, info in entries:
        status = info.get('status')
        if status == 'unavailable':
            reason = str(info.get('reason') or '')
            drift = rec.detect_drift_code(None, reason)
            if drift is not None:
                raise rec.halt_on_drift(service, contract_id, drift, reason, details={'phase': 'convergence', 'source_job': source_job, 'parameter': kind})
            raise core.ToolError(
                'SCAN_RETRY_NOT_ELIGIBLE',
                f'来源任务 {source_job} 的 {kind} 收敛证据不可用（{scrub(reason)[:240]}），疑似计算中断或 VASP 异常；'
                '请改用 vasp_diagnose_submission / vasp_prepare_resume / vasp_prepare_repair_fork，而非加密重扫。',
            )
        if status not in ('passed', 'failed'):
            raise core.ToolError('SCAN_RETRY_INSPECTION_INVALID', f'inspect_convergence 返回未知状态：{kind}={status!r}。')

    encut_st, mesh_st = encut_info['status'], mesh_info['status']
    cross_st = cross_info['status'] if isinstance(cross_info, dict) else None
    try:
        ev_encuts = [float(x) for x in (encut_info.get('values') or [])]
        ev_meshes = [[int(v) for v in m] for m in (mesh_info.get('values') or [])]
    except (TypeError, ValueError) as exc:
        raise core.ToolError('SCAN_RETRY_INSPECTION_INVALID', 'inspect_convergence 返回的扫描值格式无效。') from exc
    if ev_encuts != src_scans['encuts'] or ev_meshes != src_scans['meshes']:
        raise rec.halt_on_drift(
            service, contract_id, 'INPUT_HASH_MISMATCH',
            f'来源任务 {source_job} 的 inspect_convergence 序列与 config.json 不一致。',
            details={'phase': 'convergence', 'source_job': source_job},
        )
    if encut_st == 'passed' and mesh_st == 'passed' and cross_st in (None, 'passed'):
        raise core.ToolError('SCAN_RETRY_ALREADY_PASSED', f'来源任务 {source_job} 的 ENCUT 与 k 网格收敛核验均已通过，无需加密重扫。')

    for kind, info in (('encut', encut_info), ('mesh', mesh_info)):
        if info['status'] == 'failed':
            comps = info.get('comparisons')
            if not isinstance(comps, list) or not any(isinstance(c, dict) and c.get('passed') is False for c in comps):
                raise core.ToolError('SCAN_RETRY_INSPECTION_INVALID', f'来源任务 {kind} 标记 failed 但缺少未通过的阈值比较证据。')
    if cross_st == 'failed':
        comp = cross_info.get('comparison') if isinstance(cross_info, dict) else None
        if not isinstance(comp, dict) or comp.get('passed') is not False:
            raise core.ToolError('SCAN_RETRY_INSPECTION_INVALID', '来源任务 cross_cutoff 标记 failed 但缺少未通过的阈值比较证据。')

    if encut_st == 'failed' and mode == 'mesh':
        raise core.ToolError('SCAN_RETRY_MODE_MISMATCH', '来源 ENCUT 未通过收敛，禁止在 mesh 模式下复用 ENCUT；请改用 encut 或 both 模式。')
    if (mesh_st == 'failed' or cross_st == 'failed') and mode == 'encut':
        raise core.ToolError('SCAN_RETRY_MODE_MISMATCH', '来源 k 网格或固定 ENCUT 交叉复核未通过，禁止在 encut 模式下复用来源网格证据；请改用 mesh 或 both 模式。')

    if fixed_encut is not None:
        if mode != 'mesh':
            raise ContractError('CONTRACT_SCHEMA_INVALID', 'fixed_encut 仅可用于 mesh 重扫模式。')
        if float(fixed_encut) not in src_scans['encuts'][:-1]:
            raise ContractError('CONTRACT_SCHEMA_INVALID', 'fixed_encut 须为来源 ENCUT 扫描中低于最高值的已测点。')
        src_fixed = source_config.get('fixed_encut')
        if src_fixed is not None:
            if cross_st == 'failed' and float(fixed_encut) <= float(src_fixed):
                raise ContractError('CONTRACT_RETRY_NOT_STRICTER', f'来源 fixed_encut={src_fixed} 交叉复核失败，禁止复用相同或更低的 fixed_encut={fixed_encut}。')
            if float(fixed_encut) < float(src_fixed):
                raise ContractError('CONTRACT_RETRY_NOT_STRICTER', f'禁止将 fixed_encut 从 {src_fixed} 放宽降低至 {fixed_encut}。')
        rows = encut_info.get('cutoff_vs_highest')
        matched = next((r for r in rows if isinstance(r, dict) and r.get('encut') == fixed_encut), None) if isinstance(rows, list) else None
        if matched is None:
            raise core.ToolError('SCAN_RETRY_INSPECTION_INVALID', f'inspect_convergence 缺少 fixed_encut={fixed_encut} 的 cutoff_vs_highest 校验记录。')
        if matched.get('passed') is not True:
            raise core.ToolError('SCAN_RETRY_FIXED_ENCUT_INELIGIBLE', f'来源数据已显示 fixed_encut={fixed_encut} eV 与最高已测值差值超阈值，未准备任务。')

    failed_params = [k for k, st in (('encut', encut_st), ('mesh', mesh_st), ('cross_cutoff', cross_st)) if st == 'failed']
    return {'encut_status': encut_st, 'mesh_status': mesh_st, 'cross_cutoff_status': cross_st, 'failed_parameters': failed_params}


def compute_potential_floor(workspace: Any, contract: dict[str, Any], source_config: dict[str, Any], potential_metadata: Any) -> float:
    """Compute fail-closed ENCUT lower bound from POTCAR metadata (`enmax_eV`/`enmax`) and scan plan."""
    factor = 1.3 if source_config.get('relax_mode') == 'cell' else 1.0
    meta_floor = 0.0
    if isinstance(potential_metadata, list):
        enmaxes = []
        for row in potential_metadata:
            if not isinstance(row, dict):
                continue
            raw = row.get('enmax_eV') if row.get('enmax_eV') is not None else row.get('enmax')
            if type(raw) in (int, float) and math.isfinite(raw) and raw > 0:
                enmaxes.append(float(raw))
        if enmaxes:
            meta_floor = float(math.ceil(factor * max(enmaxes)))

    plan_floor = 0.0
    try:
        plan_path = rec.plan_path_checked(workspace, contract['workflow']['plan_id'])
        plan_doc = json.loads(plan_path.read_text(encoding='utf-8'))
        raw_pf = plan_doc.get('potential_floor') if isinstance(plan_doc, dict) else None
        if type(raw_pf) in (int, float) and math.isfinite(raw_pf) and raw_pf > 0:
            plan_floor = float(raw_pf)
    except ContractError as exc:
        if exc.error_code in ('CONTRACT_SYMLINK_REFUSED', 'CONTRACT_PATH_ESCAPE'):
            raise
        plan_floor = 0.0
    except (OSError, ValueError):
        plan_floor = 0.0

    if meta_floor == 0.0 and plan_floor == 0.0:
        raise core.ToolError('SCAN_RETRY_INSPECTION_INVALID', '无法从来源赝势元数据或合同扫描方案确定有效 ENCUT 赝势下限，已拒绝加密重扫。')
    return max(200.0, meta_floor, plan_floor)


def check_precision_bound_ceiling(
    service: Any, contract: dict[str, Any], source_config: dict[str, Any], eligibility: dict[str, Any], *, source_job: str,
) -> None:
    """Auto-halt when a non-converged parameter has already reached contract precision_bounds ceiling."""
    bounds, is_2d, cid = contract['precision_bounds'], contract['scope']['dimension'] == '2D', contract['contract_id']
    reasons: list[str] = []
    if eligibility['encut_status'] == 'failed' and float(source_config['encuts'][-1]) >= float(bounds['max_encut']):
        reasons.append(f'ENCUT 最高值 {source_config["encuts"][-1]} 已达合同上限 {bounds["max_encut"]} eV 仍未收敛')
    if eligibility['mesh_status'] == 'failed' or eligibility['cross_cutoff_status'] == 'failed':
        top = [int(x) for x in source_config['meshes'][-1]]
        axes = (0, 1) if is_2d else (0, 1, 2)
        can_tighten = False
        for i in axes:
            cand = [top[0] + int(i == 0), top[1] + int(i == 1), top[2] + int(i == 2)]
            if max(cand) <= int(bounds['max_kmesh_axis']) and cand[0] * cand[1] * cand[2] <= int(bounds['max_kmesh_points']):
                can_tighten = True
                break
        if not can_tighten:
            reasons.append(f'最密 k 网格 {top} 已达合同精度上限（max_kmesh_axis={bounds["max_kmesh_axis"]}, max_kmesh_points={bounds["max_kmesh_points"]}）仍未收敛')
    if reasons:
        detail_msg = '；'.join(reasons) + '；合同已自动熔断上报人工审核。'
        task_contract.halt_contract(
            service.workspace, cid, detail_msg, code='CONTRACT_PRECISION_BOUND_EXCEEDED', uncertain=False,
            details={
                'phase': 'convergence', 'source_job': source_job, 'source_encuts': source_config['encuts'],
                'source_meshes': source_config['meshes'], 'precision_bounds': bounds, 'failed_parameters': eligibility['failed_parameters'],
            },
            actor=f'{service.role}:{service.actor_id}',
        )
        raise ContractError('CONTRACT_PRECISION_BOUND_EXCEEDED', detail_msg)


def reconcile_prepare_disconnect(
    remote: Any, *, contract: dict[str, Any], source_job: str, source_ctx: dict[str, Any],
    candidate_job: str, mode: str, validated: dict[str, Any], fixed_encut: float | None, exc: Exception,
) -> dict[str, Any]:
    """Read-only reconciliation after SSH disconnect during remote `prepare_scan_retry`."""
    try:
        ctx = remote.call('review_context', candidate_job)
    except Exception as check_exc:
        try:
            remote.call('status', candidate_job)
        except Exception as status_exc:
            smsg = str(status_exc)
            is_missing = (
                not service_healing.is_transport_disconnect(status_exc)
                and (
                    isinstance(status_exc, FileNotFoundError)
                    or getattr(status_exc, 'error_code', None) == 'FILE_NOT_FOUND'
                    or ('任务' in smsg and '不存在' in smsg)
                )
            )
            if is_missing:
                raise core.ToolError(
                    'SCAN_RETRY_DISCONNECTED_NOT_CREATED',
                    f'加密重扫通信中断，只读核查确认远端目录 {candidate_job} 尚未创建；未消耗重扫额度，可安全重试。',
                    {'action': 'retry_vasp_prepare_scan_retry', 'job_name': candidate_job},
                ) from exc
            raise core.ToolError(
                'SCAN_RETRY_RESULT_UNCERTAIN',
                f'加密重扫通信中断且只读核查目录 {candidate_job} 失败（{scrub(smsg)[:200]}）；请先调用 vasp_task_status 核查。',
            ) from exc
        raise core.ToolError(
            'SCAN_RETRY_RESULT_UNCERTAIN',
            f'加密重扫通信中断，远端目录 {candidate_job} 已部分创建但缺少有效 manifest（{scrub(str(check_exc))[:180]}）；请先调用 vasp_task_status 核查。',
        ) from exc

    try:
        st = remote.call('status', candidate_job)
    except Exception as status_exc:
        raise core.ToolError(
            'SCAN_RETRY_RESULT_UNCERTAIN',
            f'加密重扫通信中断，远端目录 {candidate_job} 存在但 status 核查失败（{scrub(str(status_exc))[:180]}）；请先人工核查。',
        ) from exc

    cfg = ctx.get('config') if isinstance(ctx, dict) else None
    manifest = ctx.get('manifest') if isinstance(ctx, dict) else None
    hashes = manifest.get('hashes') if isinstance(manifest, dict) and isinstance(manifest.get('hashes'), dict) else {}
    src_cfg = source_ctx['config']
    expected_scans = ['encut', 'mesh'] if mode == 'both' else [mode]
    wf_state = st.get('workflow-state.json') if isinstance(st, dict) and isinstance(st.get('workflow-state.json'), dict) else {}
    phys = st.get('physics-model.json') if isinstance(st, dict) else None
    phys_ok = phys is None or contract_rules.physics_record_matches_contract(contract, phys)
    fresh_ok = (
        isinstance(st, dict) and wf_state.get('status') == 'prepared'
        and not st.get('stage_submissions') and st.get('checkpoint.json') is None and phys_ok
    )
    if mode == 'both':
        reuse_ok = True
    else:
        reused_kind = 'encut' if mode == 'mesh' else 'mesh'
        reuse_rec = (ctx.get('convergence_reuse') or {}).get(reused_kind) if isinstance(ctx, dict) else None
        reuse_src = PurePosixPath(str(reuse_rec.get('source', '')).replace('\\', '/')).name if isinstance(reuse_rec, dict) else ''
        reuse_ok = isinstance(reuse_rec, dict) and reuse_src == source_job
    try:
        expected_ov = solver_ov.merge_fork_overrides(
            src_cfg, None, locked_phases=(), enabled_phases=stage_definitions.order(src_cfg),
        )
        scans_match = (
            isinstance(cfg, dict)
            and [float(x) for x in (cfg.get('encuts') or [])] == [float(x) for x in validated['encuts']]
            and [[int(v) for v in m] for m in (cfg.get('meshes') or [])] == validated['meshes']
            and cfg.get('active_scans', ['encut', 'mesh']) == expected_scans
            and cfg.get('fixed_encut') == fixed_encut
            and (cfg.get('scan_preset') or 'strict') == validated['scan_preset']
            and (cfg.get('scan_thresholds') or None) == (validated['scan_thresholds'] or None)
            and (cfg.get('solver_overrides') or None) == expected_ov
        )
    except (TypeError, ValueError):
        scans_match = False
    prov_ok = (
        isinstance(ctx, dict) and isinstance(manifest, dict) and manifest.get('ready') is True
        and bool(ctx.get('submit_script')) and isinstance(ctx.get('environment'), dict)
        and (source_ctx['initial_poscar'] is None or ctx.get('initial_poscar') == source_ctx['initial_poscar'])
        and (source_ctx['potcar_sha256'] is None or hashes.get('POTCAR') == source_ctx['potcar_sha256'])
        and isinstance(cfg, dict) and cfg.get('relax_mode') == src_cfg.get('relax_mode')
        and cfg.get('nbands') == src_cfg.get('nbands')
        and ((cfg.get('dimensionality') or {}).get('dimension') or '3D') == contract['scope']['dimension']
    )
    if fresh_ok and reuse_ok and scans_match and prov_ok:
        return {'prepared': True, 'recovered': True, 'directory': candidate_job}
    raise core.ToolError(
        'SCAN_RETRY_RESULT_UNCERTAIN',
        f'加密重扫通信中断，远端目录 {candidate_job} 存在但不满足本次新建重扫任务的归属或配置核验；请通过 vasp_task_status 核查。',
    ) from exc
