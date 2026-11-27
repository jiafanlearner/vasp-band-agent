"""Layer 4 contract drafting, live status, and one-way circuit-breaker helpers for ControlService."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
import hashlib
import json
from pathlib import Path
from typing import Any

from pymatgen.core import Structure

from vasp_agent.common import agent_paths
from vasp_agent.permissions import contract_rules, task_contract
from vasp_agent.permissions.contract_rules import ContractError
from vasp_agent.planning import parameter_plans
from vasp_agent.workflow import dimensionality, stage_definitions

AUTONOMY_PHASE = 'P1e1_bounded_resume'
DEFAULT_BUDGET: dict[str, int] = {
    'max_submissions': 10,
    'max_resume_retries': 2,
    'max_repair_retries': 3,
    'max_scan_retries': 1,
    'max_consecutive_failures': 3,
}


def _normalize_server(server: str | None) -> str | None:
    return None if server in (None, 'local') else server


def _draft_brief(draft: dict[str, Any]) -> dict[str, Any]:
    wf = draft.get('workflow') or {}
    return {
        'draft_id': draft.get('draft_id'),
        'expires_at': draft.get('expires_at'),
        'scope': draft.get('scope'),
        'plan_id': wf.get('plan_id'),
        'allowed_phases': wf.get('allowed_phases'),
        'review_policy': draft.get('review_policy'),
    }


def contract_status(service: Any, contract_id: str | None = None) -> dict[str, Any]:
    """Read live immutable contracts, FileLock ledgers, and pending drafts without creating dirs."""
    status = task_contract.contract_status(service.workspace, contract_id)
    drafts = [_draft_brief(d) for d in task_contract.list_drafts(service.workspace)]
    selected_ledger = status.get('ledger')
    can_submit = (
        service.role == 'maker'
        and (
            isinstance(selected_ledger, dict) and selected_ledger.get('status') == 'active'
            if contract_id is not None
            else bool(status.get('active'))
        )
    )
    return {
        **status,
        'drafts': drafts,
        'autonomy_phase': AUTONOMY_PHASE,
        'submission_enabled': can_submit,
    }


def _resolve_workspace_structure(service: Any, structure: str) -> tuple[Path, str, str, str]:
    candidate = service.box.path(structure)
    if candidate.is_symlink():
        raise ContractError('CONTRACT_SYMLINK_REFUSED', '结构文件不能是符号链接。')
    if not candidate.is_file():
        raise ContractError('CONTRACT_SCHEMA_INVALID', f'找不到结构文件：{structure}。')
    resolved = candidate.resolve(strict=True)
    if not resolved.is_relative_to(service.workspace):
        raise ContractError('CONTRACT_PATH_ESCAPE', '结构文件必须位于授权工作区内。')
    rel_posix = resolved.relative_to(service.workspace).as_posix()
    digest = hashlib.sha256(resolved.read_bytes()).hexdigest()
    dim = dimensionality.classify(Structure.from_file(resolved))['dimension']
    return resolved, rel_posix, digest, dim


def _selected_env_profile(workspace: Path, server: str | None, plan_doc: dict[str, Any]) -> dict[str, Any] | None:
    env = plan_doc.get('environment')
    if server is None or not isinstance(env, dict) or not env.get('selected'):
        return None
    from vasp_agent.remote import environment_profiles as profiles

    choice = profiles.choose(workspace, server, plan_doc['request']['relax_mode'], env.get('required'))
    row = next((r for r in choice['matches'] if r['name'] == env['selected']), None)
    if row is None:
        profiles.by_name(workspace, server, env['selected'])
        raise ContractError('ENVIRONMENT_CAPABILITY_MISSING', f'环境配置 {env["selected"]} 不满足当前任务能力或优化模式要求。')
    if row.get('implicit'):
        return None
    return {
        'name': row['name'],
        'environment_sha256': row['environment_sha256'],
        'executable_sha256': row['executable_sha256'],
    }


def _derive_precision_bounds(
    explicit: dict[str, Any] | None,
    proposed_encuts: list[float],
    proposed_meshes: list[list[int]],
) -> dict[str, Any]:
    max_encut = max(proposed_encuts)
    max_axis = max(axis for mesh in proposed_meshes for axis in mesh)
    max_points = max(mesh[0] * mesh[1] * mesh[2] for mesh in proposed_meshes)
    if explicit is None:
        return {
            'max_encut': max(700, max_encut),
            'max_kmesh_axis': max(16, max_axis),
            'max_kmesh_points': max(4096, max_points),
        }
    bounds = {
        'max_encut': explicit.get('max_encut', max(700, max_encut)),
        'max_kmesh_axis': explicit.get('max_kmesh_axis', max(16, max_axis)),
        'max_kmesh_points': explicit.get('max_kmesh_points', max(4096, max_points)),
    }
    if max_encut > bounds['max_encut'] or max_axis > bounds['max_kmesh_axis'] or max_points > bounds['max_kmesh_points']:
        raise ContractError(
            'CONTRACT_PRECISION_BOUND_EXCEEDED',
            '草案拟定的初始扫描序列已超出 precision_bounds 上限，请调高上限或缩小扫描序列。',
        )
    return bounds


def draft_contract(
    service: Any,
    *,
    job_name: str,
    structure: str,
    encut: float,
    mesh: list[int],
    nbands: int,
    relax_mode: str,
    goal: str,
    resource_envelope: dict[str, Any],
    server: str | None = None,
    parallel_tests: int | None = None,
    cores_per_test: int | None = None,
    encuts: list[float] | None = None,
    meshes: list[list[int]] | None = None,
    scan_preset: str | None = None,
    band_path: str | None = None,
    optional_stages: list[str] | None = None,
    stage_parameters: list[dict[str, Any]] | None = None,
    potential_variants: list[dict[str, str]] | None = None,
    rationale: str = '',
    allowed_phases: list[str] | None = None,
    allow_forks: bool = False,
    expires_in_hours: int = 168,
    precision_bounds: dict[str, Any] | None = None,
    budget: dict[str, Any] | None = None,
    review_mode: str = 'maker_checker',
    auto_advance_on_green: bool = True,
    physics_assertion: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Propose a scan plan and persist an unapproved v1 contract draft for human TTY signing."""
    target_server = _normalize_server(server)
    _struct_path, rel_structure, structure_sha256, detected_dim = _resolve_workspace_structure(service, structure)

    phys_in = dict(physics_assertion or {})
    physics = {
        'goal': goal,
        'magnetism': phys_in.get('magnetism', 'nonmagnetic'),
        'charge': phys_in.get('charge', 0),
        'functional': phys_in.get('functional', 'PBE'),
        'soc': phys_in.get('soc', False),
        'hubbard_u': phys_in.get('hubbard_u', False),
        'hybrid': phys_in.get('hybrid', False),
        'confirmed_by': phys_in.get('confirmed_by', 'operator_slash_command'),
        'assumptions_source': phys_in.get('assumptions_source', 'operator_assertion_not_structure_derived'),
    }
    contract_rules.validate_physics_assertion(physics, detected_dim)

    phases = list(allowed_phases) if allowed_phases is not None else list(stage_definitions.with_optional(optional_stages))
    enabled_optional = set(optional_stages or [])
    base_stages = set(stage_definitions.LEGACY_STAGES)
    unenabled_opt = [p for p in phases if p not in base_stages and p not in enabled_optional]
    if unenabled_opt:
        raise ContractError(
            'CONTRACT_SCHEMA_INVALID',
            f'allowed_phases 包含未在 optional_stages 启用的可选阶段：{"、".join(unenabled_opt)}。',
        )
    effective_preset = scan_preset or 'standard'
    effective_budget = {**DEFAULT_BUDGET, **(budget or {})}
    effective_review = {'mode': review_mode, 'auto_advance_on_green': bool(auto_advance_on_green)}

    now = datetime.now(timezone.utc)
    created_at = contract_rules.format_utc(now)
    expires_at = contract_rules.format_utc(now + timedelta(hours=int(expires_in_hours)))
    draft_id = task_contract.new_draft_id(now)

    # Pre-validate envelope, phases, budget, and bounds BEFORE creating any scan plan on disk.
    provisional_bounds = {
        'max_encut': (precision_bounds or {}).get('max_encut', 700),
        'max_kmesh_axis': (precision_bounds or {}).get('max_kmesh_axis', 16),
        'max_kmesh_points': (precision_bounds or {}).get('max_kmesh_points', 4096),
    }
    if precision_bounds is not None:
        if encuts is not None and max(encuts) > provisional_bounds['max_encut']:
            raise ContractError('CONTRACT_PRECISION_BOUND_EXCEEDED', '拟定 ENCUT 序列超出 precision_bounds.max_encut。')
        if meshes is not None:
            if max(a for m in meshes for a in m) > provisional_bounds['max_kmesh_axis']:
                raise ContractError('CONTRACT_PRECISION_BOUND_EXCEEDED', '拟定 k 网格单轴超出 precision_bounds.max_kmesh_axis。')
            if max(m[0] * m[1] * m[2] for m in meshes) > provisional_bounds['max_kmesh_points']:
                raise ContractError('CONTRACT_PRECISION_BOUND_EXCEEDED', '拟定 k 网格总点数超出 precision_bounds.max_kmesh_points。')
    provisional = {
        'contract_version': contract_rules.CONTRACT_VERSION,
        'draft_id': draft_id,
        'created_at': created_at,
        'expires_at': expires_at,
        'scope': {
            'server': target_server,
            'job_name': job_name,
            'structure': rel_structure,
            'structure_sha256': structure_sha256,
            'dimension': detected_dim,
            'environment_profile': None,
            'allow_forks': bool(allow_forks),
        },
        'physics_assertion': physics,
        'workflow': {
            'plan_id': '0' * 32,
            'plan_sha256': '0' * 64,
            'allowed_phases': phases,
            'scan_preset': effective_preset,
            'allow_threshold_relaxation': False,
        },
        'precision_bounds': provisional_bounds,
        'resource_envelope': resource_envelope,
        'budget': effective_budget,
        'review_policy': effective_review,
    }
    contract_rules.validate_contract_schema(provisional, require_approval=False)

    existing_plans = set(agent_paths.store_items(service.workspace, 'scan_plans', '*.json'))
    origin = 'ai_suggestion' if (encuts is not None or meshes is not None) else 'tool_default'
    plan_summary = service.box.propose_scan_plan(
        target_server,
        job_name,
        rel_structure,
        encut,
        mesh,
        nbands,
        relax_mode,
        parallel_tests,
        cores_per_test,
        encuts,
        meshes,
        origin,
        rationale,
        potential_variants=potential_variants,
        scan_thresholds=None,
        scan_preset=effective_preset,
        band_path=band_path,
        optional_stages=optional_stages,
        stage_parameters=stage_parameters,
    )
    plan_id = plan_summary['id']
    plan_path = parameter_plans.path(service.box, plan_id)
    created_plan = plan_path not in existing_plans
    try:
        plan_bytes = plan_path.read_bytes()
        plan_sha256 = hashlib.sha256(plan_bytes).hexdigest()
        plan_doc = json.loads(plan_bytes.decode('utf-8'))
        if plan_doc.get('status') == 'prepared':
            raise ContractError('CONTRACT_PLAN_ALREADY_PREPARED', f'扫描方案 {plan_id} 已准备，不能作为新合同草案复用。')

        final_dim = plan_doc.get('dimension') or detected_dim
        final_bounds = _derive_precision_bounds(
            precision_bounds,
            plan_doc['suggestion']['encuts'],
            plan_doc['suggestion']['meshes'],
        )
        env_profile = _selected_env_profile(service.workspace, target_server, plan_doc)
        draft = {
            **provisional,
            'scope': {**provisional['scope'], 'dimension': final_dim, 'environment_profile': env_profile},
            'workflow': {**provisional['workflow'], 'plan_id': plan_id, 'plan_sha256': plan_sha256},
            'precision_bounds': final_bounds,
        }
        saved = task_contract.save_draft(service.workspace, draft)
    except Exception:
        if created_plan:
            plan_path.unlink(missing_ok=True)
        raise
    approve_cmd = f'vasp-ctl contract approve {saved["draft_id"]}'
    return {
        'draft_id': saved['draft_id'],
        'plan_id': plan_id,
        'plan_status': plan_summary['status'],
        'status': 'draft',
        'expires_at': saved['expires_at'],
        'scope': saved['scope'],
        'workflow': saved['workflow'],
        'precision_bounds': saved['precision_bounds'],
        'review_policy': saved['review_policy'],
        'approve_command': approve_cmd,
        'show_command': f'vasp-ctl contract show {saved["draft_id"]}',
        'approved': False,
        'submission_enabled': False,
        'next': f'合同草案已保存（未签发、未授权提交）；请操作者在本地真实终端执行：{approve_cmd}',
    }


def circuit_break(
    service: Any,
    *,
    contract_id: str,
    reason: str,
    code: str = 'CIRCUIT_BREAKER',
    uncertain: bool = False,
    details: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """One-way circuit breaker: halts a contract ledger so only human TTY can resume/revoke."""
    clean_reason = reason.strip() if isinstance(reason, str) else ''
    clean_code = code.strip() if isinstance(code, str) else ''
    if not clean_reason or not clean_code:
        raise ContractError('CONTRACT_SCHEMA_INVALID', 'vasp_circuit_break 的 reason 与 code 必须为非空字符串。')
    ledger = task_contract.halt_contract(
        service.workspace,
        contract_id,
        clean_reason,
        code=clean_code,
        uncertain=bool(uncertain),
        details=details,
        actor=f'{service.role}:{service.actor_id}',
    )
    return {
        'contract_id': contract_id,
        'status': ledger['status'],
        'halt_code': ledger['halt_code'],
        'halt_reason': ledger['halt_reason'],
        'updated_at': ledger['updated_at'],
        'reversible_by_agent': False,
        'resume_command': f'vasp-ctl contract resume {contract_id}',
        'revoke_command': f'vasp-ctl contract revoke {contract_id}',
        'next': f'合同已单向熔断，任何代理均不可自解；请操作者人工核查后在本地终端执行 vasp-ctl contract resume {contract_id} 或 revoke。',
    }
