"""Strategy execution-plan building and remote disconnect reconciliation for P1e-3."""
from __future__ import annotations

import hashlib
import hmac
import json
import math
from pathlib import PurePosixPath
from typing import Any

from vasp_agent.chat.toolbox_core import scrub
from vasp_agent.control import execution_reconcile as rec
from vasp_agent.control import repair_fork_checks as checks
from vasp_agent.control import service_execution, service_healing
from vasp_agent.permissions.contract_rules import ContractError
from vasp_agent.planning import parameter_plans
from vasp_agent.workflow import (
    band_path as band_path_mod,
    convergence_reuse,
    parameter_specs,
    scan_config,
    stage_gate,
)
import vasp_agent.workflow.vasp_band_agent as core

_ALLOWED_DOS_REPAIR_FIELDS = frozenset({'nbands', 'kmesh_factor'})


def _check_scans_within_bounds(contract: dict[str, Any], encuts: list[float], meshes: list[list[int]]) -> None:
    bounds, is_2d = contract['precision_bounds'], contract['scope']['dimension'] == '2D'
    if max(encuts) > float(bounds['max_encut']):
        raise ContractError('CONTRACT_PRECISION_BOUND_EXCEEDED', f'ENCUT 最大值 {max(encuts)} 超出合同上限 {bounds["max_encut"]}。')
    for m in meshes:
        if (is_2d and m[2] != 1) or max(m) > int(bounds['max_kmesh_axis']) or m[0] * m[1] * m[2] > int(bounds['max_kmesh_points']):
            raise ContractError('CONTRACT_PRECISION_BOUND_EXCEEDED', f'k 网格 {m} 超出合同 precision_bounds 上限或违反 2D 约束。')


def _read_local_scf_summary(service: Any, target_server: str | None, source_job: str) -> dict[str, Any] | None:
    if target_server is not None:
        return None
    path = rec.reject_symlink_rel(service.workspace, f'runs/{source_job}/scf-summary.json', '来源 SCF 摘要')
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding='utf-8'))
        return data if isinstance(data, dict) else None
    except (OSError, ValueError):
        return None


def _merge_dos_values(
    contract: dict[str, Any],
    source_config: dict[str, Any],
    plan_doc: dict[str, Any] | None,
    overrides: dict[str, Any] | None,
    *,
    failure_category: str,
    scf_summary: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Merge DOS parameters while strictly enforcing monotonic allowlist (no relaxation or smearing switch)."""
    dos_spec = parameter_specs.for_stage('dos')
    ctx = {'dimension': contract['scope']['dimension']}
    base: dict[str, Any] | None = None
    src_sp = source_config.get('stage_parameters')
    if isinstance(src_sp, dict) and isinstance(src_sp.get('dos'), dict) and isinstance(src_sp['dos'].get('values'), dict):
        base = dict(src_sp['dos']['values'])
    elif isinstance(plan_doc, dict) and isinstance((plan_doc.get('stage_parameters') or {}).get('dos'), dict):
        sel = plan_doc['stage_parameters']['dos'].get('selected')
        if isinstance(sel, dict):
            base = dict(sel)
    if base is None:
        base = {k: v['value'] for k, v in parameter_specs.suggestions(dos_spec, ctx).items()}

    if overrides:
        if not isinstance(overrides, dict):
            raise ContractError('CONTRACT_SCHEMA_INVALID', 'dos_values 必须是 JSON 对象。')
        unknown = sorted(set(overrides) - set(dos_spec['fields']))
        if unknown:
            raise ContractError('CONTRACT_SCHEMA_INVALID', f'dos_values 包含未知参数：{"、".join(unknown)}。')
        disallowed = sorted(k for k, v in overrides.items() if k not in _ALLOWED_DOS_REPAIR_FIELDS and v != base.get(k))
        if disallowed:
            raise ContractError(
                'CONTRACT_DOS_RELAXATION_FORBIDDEN',
                f'自治派生修复禁止修改 DOS 积分方法/窗口/采样/余量参数（{"、".join(disallowed)}）；'
                '仅允许单调提升 nbands 或在合同 precision_bounds 内提高 kmesh_factor，其余改动请交人工审批。',
            )

    merged = {**base, **(overrides or {})}
    try:
        validated = parameter_specs.validate(dos_spec, merged, ctx)
    except ValueError as exc:
        raise ContractError('CONTRACT_SCHEMA_INVALID', f'DOS 修复参数无效：{exc}') from exc

    if overrides and 'nbands' in overrides:
        scf_nb = int(scf_summary['nbands']) if isinstance(scf_summary, dict) and type(scf_summary.get('nbands')) is int else int(source_config.get('nbands') or 0)
        base_nb = int(base['nbands']) if type(base.get('nbands')) is int else 0
        floor_nb = max(scf_nb, base_nb)
        new_nb = validated['nbands']
        if new_nb is None or new_nb < floor_nb:
            raise ContractError('CONTRACT_DOS_RELAXATION_FORBIDDEN', f'DOS 修复 nbands={new_nb} 不得低于当前基准 {floor_nb}。')
        if failure_category == 'empty_bands_insufficient' and new_nb <= floor_nb:
            raise ContractError('CONTRACT_DOS_RELAXATION_FORBIDDEN', f'空带不足修复须将 DOS nbands 严格提高至大于 {floor_nb}。')

    if overrides and 'kmesh_factor' in overrides:
        base_kf = int(base['kmesh_factor'])
        new_kf = int(validated['kmesh_factor'])
        if new_kf < base_kf:
            raise ContractError('CONTRACT_DOS_RELAXATION_FORBIDDEN', f'禁止降低 DOS kmesh_factor（{new_kf} < {base_kf}）。')
        if new_kf > base_kf:
            ref_mesh = (
                [int(v) for v in scf_summary['mesh']]
                if isinstance(scf_summary, dict) and isinstance(scf_summary.get('mesh'), list) and len(scf_summary['mesh']) == 3
                else [int(v) for v in (source_config.get('production_mesh') or source_config['meshes'][-1])]
            )
            dos_mesh = [math.ceil(n * new_kf) for n in ref_mesh]
            bounds = contract['precision_bounds']
            if max(dos_mesh) > int(bounds['max_kmesh_axis']) or (dos_mesh[0] * dos_mesh[1] * dos_mesh[2]) > int(bounds['max_kmesh_points']):
                raise ContractError(
                    'CONTRACT_PRECISION_BOUND_EXCEEDED',
                    f'提高 kmesh_factor={new_kf} 推导的 DOS 网格 {dos_mesh} 超出合同 precision_bounds 上限。',
                )
    return validated


def prepare_strategy_plan(
    service: Any,
    contract: dict[str, Any],
    source_ctx: dict[str, Any],
    *,
    target_server: str | None,
    remote: Any | None,
    source_job: str,
    phase: str,
    strategy: str,
    failure_category: str,
    nbands: int | None,
    production_mesh: list[int] | None,
    production_encut: float | None,
    band_path: str | None,
    dos_values: dict[str, Any] | None,
) -> dict[str, Any]:
    """Validate all strategy-specific parameters and upstream prerequisites before any mutation."""
    cid, cfg, ckpt = contract['contract_id'], source_ctx['config'], source_ctx['checkpoint']
    bounds, is_2d = contract['precision_bounds'], contract['scope']['dimension'] == '2D'

    if strategy == 'fresh_restart':
        scope = contract['scope']
        struct_path = rec.reject_symlink_rel(service.workspace, scope['structure'], '结构文件')
        if not struct_path.is_file():
            raise ContractError('CONTRACT_TAMPERED', f'合同绑定的结构文件缺失：{scope["structure"]}。')
        if not hmac.compare_digest(hashlib.sha256(struct_path.read_bytes()).hexdigest(), scope['structure_sha256']):
            raise rec.halt_on_drift(service, cid, 'INPUT_HASH_MISMATCH', f'结构文件 {scope["structure"]} 哈希与合同不一致。', details={'phase': phase})
        plan, _ = service_execution._validate_plan_for_prepare(service.workspace, contract)
        scans = scan_config.validate_scans(list(cfg['encuts']), [list(m) for m in cfg['meshes']])
        _check_scans_within_bounds(contract, scans['encuts'], scans['meshes'])
        plan_sel = plan['selected']
        if scans['encuts'][-1] < float(plan_sel['encuts'][-1]) or (scans['meshes'][-1][0] * scans['meshes'][-1][1] * scans['meshes'][-1][2]) < (
            plan_sel['meshes'][-1][0] * plan_sel['meshes'][-1][1] * plan_sel['meshes'][-1][2]
        ):
            raise rec.halt_on_drift(service, cid, 'CONTRACT_TAMPERED', f'来源任务 {source_job} 扫描精度低于合同锁定基线。', details={'phase': phase, 'source_job': source_job})

        base_nbands = int(cfg.get('nbands') or plan['request']['nbands'])
        eff_nbands = base_nbands if nbands is None else int(nbands)
        if nbands is not None and (type(nbands) is not int or not 1 <= nbands <= 4000 or nbands < base_nbands):
            raise ContractError('CONTRACT_SCHEMA_INVALID', f'修复 nbands={nbands} 须为 [{base_nbands}, 4000] 内整数（不得低于来源能带数）。')
        if failure_category == 'empty_bands_insufficient' and phase != 'dos' and eff_nbands <= base_nbands:
            raise ContractError('CONTRACT_SCHEMA_INVALID', f'空带不足修复（{phase}）须传入大于来源 {base_nbands} 的 nbands。')

        req = dict(plan['request'])
        optional_stages = list(req.get('optional_stages') or [])
        plan_stage_params: dict[str, Any] | None = None
        if plan.get('stage_parameters'):
            plan_stage_params = {k: {'spec': v['spec'], 'values': v['selected']} for k, v in plan['stage_parameters'].items()}
        eff_stage_params = dict(plan_stage_params) if plan_stage_params else None
        if dos_values is not None:
            if 'dos' not in optional_stages:
                raise ContractError('CONTRACT_SCHEMA_INVALID', '合同扫描方案未启用 dos 阶段，无法在 fresh_restart 传入 dos_values。')
            eff_dos = _merge_dos_values(contract, cfg, plan, dos_values, failure_category=failure_category)
            eff_stage_params = {**(eff_stage_params or {}), 'dos': parameter_specs.record_entry(parameter_specs.for_stage('dos'), eff_dos, {'dimension': scope['dimension']})}
        elif isinstance(cfg.get('stage_parameters'), dict) and 'dos' in optional_stages and 'dos' in cfg['stage_parameters']:
            eff_stage_params = {**(eff_stage_params or {}), 'dos': cfg['stage_parameters']['dos']}

        preset = cfg.get('scan_preset') or 'strict'
        thresholds = cfg.get('scan_thresholds') or None
        same_as_plan = (
            scans['encuts'] == [float(x) for x in plan_sel['encuts']]
            and scans['meshes'] == [[int(v) for v in m] for m in plan_sel['meshes']]
            and eff_nbands == int(req['nbands'])
            and (eff_stage_params or {}) == (plan_stage_params or {})
            and preset == (req.get('scan_preset') or 'standard')
            and thresholds is None
        )
        choice = None
        if same_as_plan:
            choice = {k: plan[k] for k in ('id', 'preview_signature', 'defaults', 'suggestion', 'selected', 'origin',
                                           'choice', 'rationale', 'user_instruction', 'proposed_sources', 'selected_sources') if k in plan}
            if plan_stage_params:
                choice['stage_parameters'] = plan_stage_params
                choice['stage_parameter_choices'] = {k: v['choice'] for k, v in plan['stage_parameters'].items()}
        env_payload = parameter_plans.environment_payload(service.box, req, plan.get('environment')) if target_server is not None else None
        return {
            'strategy': strategy, 'planned_tests': 6, 'request': req, 'encuts': scans['encuts'], 'meshes': scans['meshes'],
            'nbands': eff_nbands, 'scan_preset': preset, 'scan_thresholds': thresholds, 'optional_stages': optional_stages,
            'stage_parameters': eff_stage_params, 'parameter_choice': choice, 'environment_payload': env_payload,
            'applied_parameters': {'nbands': eff_nbands, **({'dos_values': eff_stage_params['dos']['values']} if dos_values is not None and eff_stage_params else {})},
        }

    if strategy == 'production_from_convergence':
        if ckpt.get('completed') != ['convergence'] or ckpt.get('next') != 'relaxation':
            raise core.ToolError(
                'REPAIR_FORK_NOT_ELIGIBLE',
                f'来源任务 {source_job} 检查点不是仅完成 convergence（completed={ckpt.get("completed")}, next={ckpt.get("next")}）；'
                '冻结引擎 prepare_production 仅接受干净 convergence 来源，请改用干净 convergence 来源目录或 fresh_restart。',
            )
        later_subs = [p for p in source_ctx['stage_submissions'] if p != 'convergence']
        if later_subs:
            raise core.ToolError(
                'REPAIR_FORK_NOT_ELIGIBLE',
                f'来源任务 {source_job} 已有后续阶段提交记录（{"、".join(later_subs)}），冻结引擎 convergence_reuse.prepare_production(check_idle) 会拒绝；'
                '请传入干净 convergence 来源目录（配合 failed_job）或改用 fresh_restart。',
            )
        if target_server is None:
            src_dir = rec.reject_symlink_rel(service.workspace, f'runs/{source_job}', f'来源任务目录 {source_job}')
            later = stage_gate.task_stages(src_dir)[1:]
            existing_later = [n for n in ('relax-1', 'relax-2', 'validation', *later[1:]) if (src_dir / n).exists()]
            if existing_later:
                raise core.ToolError(
                    'REPAIR_FORK_NOT_ELIGIBLE',
                    f'来源任务 {source_job} 已存在后续目录（{"、".join(existing_later)}），冻结引擎 prepare_production 会拒绝；请改用干净来源或 fresh_restart。',
                )
            stage_ctx = stage_gate.context(src_dir)
        else:
            assert remote is not None
            try:
                stage_ctx = remote.call('stage_context', source_job)
            except Exception as exc:
                raise checks.handle_error(service, cid, exc, source_job=source_job, phase=phase) from exc
        if not isinstance(stage_ctx, dict) or stage_ctx.get('phase') != 'relaxation' or not stage_ctx.get('snapshot'):
            raise core.ToolError('REPAIR_FORK_NOT_ELIGIBLE', f'来源任务 {source_job} 当前不在 relaxation 待审状态，无法执行 production_from_convergence。')
        eff_mesh = [int(v) for v in (production_mesh if production_mesh is not None else (cfg.get('production_mesh') or cfg['meshes'][-1]))]
        if eff_mesh not in [[int(v) for v in m] for m in cfg['meshes']]:
            raise ContractError('CONTRACT_SCHEMA_INVALID', f'production_mesh={eff_mesh} 必须是来源已测试网格之一。')
        if (is_2d and eff_mesh[2] != 1) or max(eff_mesh) > int(bounds['max_kmesh_axis']) or eff_mesh[0] * eff_mesh[1] * eff_mesh[2] > int(bounds['max_kmesh_points']):
            raise ContractError('CONTRACT_PRECISION_BOUND_EXCEEDED', f'production_mesh={eff_mesh} 超出合同 precision_bounds 上限。')
        eff_encut = float(production_encut) if production_encut is not None else None
        if eff_encut is not None:
            if eff_encut not in [float(x) for x in cfg['encuts']]:
                raise ContractError('CONTRACT_SCHEMA_INVALID', f'production_encut={eff_encut} 必须是来源已测试 ENCUT 之一。')
            if eff_encut > float(bounds['max_encut']):
                raise ContractError('CONTRACT_PRECISION_BOUND_EXCEEDED', f'production_encut={eff_encut} 超出合同上限 {bounds["max_encut"]}。')
        return {
            'strategy': strategy, 'planned_tests': 1, 'source_snapshot': stage_ctx['snapshot'],
            'mesh': eff_mesh, 'encut': eff_encut,
            'applied_parameters': {'production_mesh': eff_mesh, **({'production_encut': eff_encut} if eff_encut is not None else {})},
        }

    if 'scf' not in (ckpt.get('completed') or []):
        raise core.ToolError('REPAIR_FORK_NOT_ELIGIBLE', f'来源任务 {source_job} 的 SCF 尚未验收通过，无法执行 {strategy}；请改用 fresh_restart。')

    if strategy == 'bands_from_scf':
        raw_path = band_path if band_path is not None else (band_path_mod.render(cfg['band_path']) if cfg.get('band_path') else None)
        if not raw_path:
            raise ContractError('CONTRACT_SCHEMA_INVALID', '来源任务未记录显式 band_path；使用 bands_from_scf 必须提供 band_path，或改用 fresh_restart。')
        try:
            validated_spec = band_path_mod.validate(raw_path, int(cfg.get('points', 40)))
        except ValueError as exc:
            raise ContractError('CONTRACT_SCHEMA_INVALID', f'band_path 无效：{exc}') from exc
        rendered = band_path_mod.render(validated_spec)
        return {'strategy': strategy, 'planned_tests': 1, 'band_path': rendered, 'applied_parameters': {'band_path': rendered}}

    plan_doc: dict[str, Any] | None = None
    src_sp = cfg.get('stage_parameters')
    if not (isinstance(src_sp, dict) and isinstance(src_sp.get('dos'), dict) and isinstance(src_sp['dos'].get('values'), dict)):
        plan_doc, _ = service_execution._validate_plan_for_prepare(service.workspace, contract)
    scf_summary = _read_local_scf_summary(service, target_server, source_job)
    eff_dos = _merge_dos_values(contract, cfg, plan_doc, dos_values, failure_category=failure_category, scf_summary=scf_summary)
    return {'strategy': strategy, 'planned_tests': 1, 'dos_values': eff_dos, 'applied_parameters': {'dos_values': eff_dos}}


def reconcile_fork_disconnect(
    remote: Any, *, contract: dict[str, Any], source_job: str, source_ctx: dict[str, Any],
    candidate_job: str, strategy: str, plan_spec: dict[str, Any], exc: Exception,
) -> dict[str, Any]:
    """Read-only reconciliation after SSH disconnect during remote repair-fork creation."""
    try:
        ctx = remote.call('review_context', candidate_job)
    except Exception as check_exc:
        try:
            remote.call('status', candidate_job)
        except Exception as status_exc:
            smsg = str(status_exc)
            is_missing = not service_healing.is_transport_disconnect(status_exc) and (
                isinstance(status_exc, FileNotFoundError)
                or getattr(status_exc, 'error_code', None) == 'FILE_NOT_FOUND'
                or ('任务' in smsg and '不存在' in smsg)
            )
            if is_missing:
                raise core.ToolError(
                    'REPAIR_FORK_DISCONNECTED_NOT_CREATED',
                    f'派生修复通信中断，只读核查确认远端目录 {candidate_job} 尚未创建；未消耗修复额度，可安全重试。',
                    {'action': 'retry_vasp_prepare_repair_fork', 'job_name': candidate_job},
                ) from exc
            raise core.ToolError(
                'REPAIR_FORK_RESULT_UNCERTAIN',
                f'派生修复通信中断且只读核查目录 {candidate_job} 失败（{scrub(smsg)[:200]}）；请先调用 vasp_task_status 核查。',
            ) from exc
        raise core.ToolError(
            'REPAIR_FORK_RESULT_UNCERTAIN',
            f'派生修复通信中断，远端目录 {candidate_job} 已部分创建但缺少有效 manifest（{scrub(str(check_exc))[:180]}）；请先核查。',
        ) from exc

    try:
        st = remote.call('status', candidate_job)
    except Exception as status_exc:
        raise core.ToolError(
            'REPAIR_FORK_RESULT_UNCERTAIN',
            f'派生修复通信中断，远端目录 {candidate_job} 存在但 status 核查失败（{scrub(str(status_exc))[:180]}）；请先核查。',
        ) from exc

    cfg = ctx.get('config') if isinstance(ctx, dict) else None
    manifest = ctx.get('manifest') if isinstance(ctx, dict) else None
    hashes = manifest.get('hashes') if isinstance(manifest, dict) and isinstance(manifest.get('hashes'), dict) else {}
    wf_state = st.get('workflow-state.json') if isinstance(st, dict) and isinstance(st.get('workflow-state.json'), dict) else {}
    ckpt = st.get('checkpoint.json') if isinstance(st, dict) else None
    phys = st.get('physics-model.json') if isinstance(st, dict) else None
    phys_ok = (phys is None) if strategy == 'fresh_restart' else (
        isinstance(phys, dict)
        and phys.get('goal') == contract['physics_assertion']['goal']
        and phys.get('dimension') == contract['scope']['dimension']
    )
    bound_env = contract['scope'].get('environment_profile')
    env_ok = True
    if isinstance(bound_env, dict):
        cfg_prof = cfg.get('environment_profile') if isinstance(cfg, dict) else None
        ctx_env = ctx.get('environment') if isinstance(ctx, dict) else None
        env_ok = (
            isinstance(cfg_prof, dict)
            and cfg_prof.get('name') == bound_env['name']
            and cfg_prof.get('environment_sha256') == bound_env['environment_sha256']
            and cfg_prof.get('executable_sha256') == bound_env['executable_sha256']
            and isinstance(ctx_env, dict)
            and ctx_env.get('environment_sha256') == bound_env['environment_sha256']
            and ctx_env.get('executable_sha256') == bound_env['executable_sha256']
        )
    prov_ok = (
        isinstance(st, dict) and not st.get('stage_submissions') and phys_ok and env_ok
        and isinstance(ctx, dict) and isinstance(manifest, dict) and manifest.get('ready') is True
        and bool(ctx.get('submit_script')) and isinstance(ctx.get('environment'), dict) and isinstance(cfg, dict)
        and (source_ctx['initial_poscar'] is None or ctx.get('initial_poscar') == source_ctx['initial_poscar'])
        and (source_ctx['potcar_sha256'] is None or hashes.get('POTCAR') == source_ctx['potcar_sha256'])
        and ((cfg.get('dimensionality') or {}).get('dimension') or '3D') == contract['scope']['dimension']
    )

    def _base(path_val: Any) -> str:
        return PurePosixPath(str(path_val or '').replace('\\', '/')).name

    strat_ok = False
    if prov_ok and isinstance(cfg, dict):
        try:
            if strategy == 'fresh_restart':
                dos_want = ((plan_spec.get('stage_parameters') or {}).get('dos') or {}).get('values')
                dos_got = ((cfg.get('stage_parameters') or {}).get('dos') or {}).get('values')
                req_bp = plan_spec['request'].get('band_path')
                got_bp = band_path_mod.render(cfg['band_path']) if isinstance(cfg.get('band_path'), dict) else None
                want_bp = band_path_mod.render(band_path_mod.validate(req_bp, int(cfg.get('points', 40)))) if req_bp is not None else None
                strat_ok = (
                    wf_state.get('status') == 'prepared' and ckpt is None
                    and cfg.get('active_scans', ['encut', 'mesh']) == ['encut', 'mesh']
                    and cfg.get('fixed_encut') is None
                    and cfg.get('relax_mode') == plan_spec['request']['relax_mode']
                    and [float(x) for x in (cfg.get('encuts') or [])] == plan_spec['encuts']
                    and [[int(v) for v in m] for m in (cfg.get('meshes') or [])] == plan_spec['meshes']
                    and cfg.get('nbands') == plan_spec['nbands']
                    and (cfg.get('scan_preset') or 'strict') == plan_spec['scan_preset']
                    and (cfg.get('scan_thresholds') or None) == (plan_spec['scan_thresholds'] or None)
                    and dos_got == dos_want and got_bp == want_bp
                )
            elif strategy == 'production_from_convergence':
                encut_ok = (
                    float(convergence_reuse.production_cutoff(cfg)) == float(plan_spec['encut'])
                    if plan_spec['encut'] is not None else 'production_encut' not in cfg
                )
                prod_choice = ctx.get('production_choice')
                snap_ok = not isinstance(prod_choice, dict) or prod_choice.get('source_snapshot') == plan_spec['source_snapshot']
                strat_ok = (
                    wf_state.get('status') == 'awaiting_review' and wf_state.get('next') == 'relaxation'
                    and isinstance(ckpt, dict) and ckpt.get('completed') == ['convergence']
                    and _base(wf_state.get('source')) == source_job
                    and cfg.get('production_mesh') == plan_spec['mesh'] and encut_ok and snap_ok
                )
            elif strategy == 'bands_from_scf':
                bp_ok = isinstance(cfg.get('band_path'), dict) and band_path_mod.render(cfg['band_path']) == plan_spec['band_path']
                strat_ok = (
                    wf_state.get('status') == 'awaiting_review' and wf_state.get('next') == 'bands'
                    and isinstance(ckpt, dict) and 'scf' in (ckpt.get('completed') or [])
                    and _base(ckpt.get('bands_path_fork')) == source_job and bp_ok
                )
            elif strategy == 'dos_from_scf':
                dos_cfg = ((cfg.get('stage_parameters') or {}).get('dos') or {}).get('values')
                strat_ok = (
                    wf_state.get('status') == 'awaiting_review' and wf_state.get('next') == 'dos'
                    and isinstance(ckpt, dict) and 'scf' in (ckpt.get('completed') or [])
                    and _base(ckpt.get('dos_fork')) == source_job and dos_cfg == plan_spec['dos_values']
                )
        except (TypeError, ValueError, KeyError):
            strat_ok = False
    if strat_ok:
        return {'prepared': True, 'recovered': True, 'directory': candidate_job}
    raise core.ToolError(
        'REPAIR_FORK_RESULT_UNCERTAIN',
        f'派生修复通信中断，远端目录 {candidate_job} 存在但不满足本次 {strategy} 任务的归属或配置核验；请通过 vasp_task_status 核查。',
    ) from exc
