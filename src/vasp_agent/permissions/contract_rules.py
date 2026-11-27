"""Pure Layer 3 schema, canonicalization, resource-envelope, and retry rules for task contracts.

Zero upward imports into chat/ or control/; deterministic across Windows and Linux.
"""
from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timezone
import hashlib
import json
import math
from pathlib import PurePosixPath
import re
from typing import Any

from vasp_agent.workflow import scan_config, stage_definitions, workflow_policy
import vasp_agent.workflow.vasp_band_agent as core

CONTRACT_VERSION = 1
SSH_SIGN_NAMESPACE = 'vasp-agent-contract'
SUPPORTED_APPROVAL_MODES = ('local_tty',)
RESERVED_APPROVAL_MODES = ('local_tty', 'in_window_approval')
REVIEW_MODES = ('maker_checker', 'maker_direct')
ACTIVE_LEDGER_STATUSES = ('active',)
HALTED_LEDGER_STATUSES = ('halted', 'halted_uncertain')
TERMINAL_LEDGER_STATUSES = ('completed', 'revoked', 'expired')
ALL_LEDGER_STATUSES = ACTIVE_LEDGER_STATUSES + HALTED_LEDGER_STATUSES + TERMINAL_LEDGER_STATUSES

_ID_RE = re.compile(r'^[A-Za-z0-9][A-Za-z0-9_-]{3,63}$')
_SERVER_RE = re.compile(r'^[A-Za-z0-9][A-Za-z0-9_-]{0,47}$')
_JOB_RE = re.compile(r'^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$')
_JOB_ID_RE = re.compile(r'^[0-9]+$')
_PART_RE = re.compile(r'^[A-Za-z0-9_-]+$')
_HEX32_RE = re.compile(r'^[0-9a-f]{32}$')
_HEX64_RE = re.compile(r'^[0-9a-f]{64}$')
_WALL_RE = re.compile(r'^(\d{1,3}):([0-5]\d):([0-5]\d)$')
_PRESET_RANK = {'standard': 0, 'strict': 1}
_DEFAULT_THRESHOLDS = {'energy_eV_per_atom': 0.001, 'force_eV_per_A': 0.005, 'stress_kbar': 0.5}


class ContractError(core.ToolError):
    """Structured error raised by autonomy contract validation or enforcement."""


def _is_int(value: Any) -> bool:
    return type(value) is int


def _is_number(value: Any) -> bool:
    return type(value) in (int, float) and math.isfinite(value)


def _require_dict(value: Any, label: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ContractError('CONTRACT_SCHEMA_INVALID', f'{label} 必须是 JSON 对象。')
    return value


def _require_str(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ContractError('CONTRACT_SCHEMA_INVALID', f'{label} 必须是非空字符串。')
    return value


def _require_hex(value: Any, regex: re.Pattern[str], label: str) -> str:
    if not isinstance(value, str) or not regex.fullmatch(value):
        raise ContractError('CONTRACT_SCHEMA_INVALID', f'{label} 格式无效。')
    return value


def validate_identifier(value: Any, label: str = 'contract_id') -> str:
    if not isinstance(value, str) or not _ID_RE.fullmatch(value):
        raise ContractError('CONTRACT_SCHEMA_INVALID', f'无效的 {label}：{value!r}。')
    return value


def validate_job_name(value: Any) -> str:
    if not isinstance(value, str) or not _JOB_RE.fullmatch(value):
        raise ContractError('CONTRACT_SCHEMA_INVALID', f'无效的任务目录名：{value!r}。')
    return value


def validate_job_id(value: Any) -> str:
    if isinstance(value, bool) or not isinstance(value, (str, int)) or not _JOB_ID_RE.fullmatch(str(value)):
        raise ContractError('CONTRACT_SCHEMA_INVALID', f'无效的 Slurm JobID：{value!r}。')
    return str(value)


def wall_seconds(value: Any) -> int:
    if not isinstance(value, str):
        raise ContractError('CONTRACT_SCHEMA_INVALID', '运行时间必须为 HH:MM:SS 字符串。')
    match = _WALL_RE.fullmatch(value)
    if not match:
        raise ContractError('CONTRACT_SCHEMA_INVALID', f'运行时间格式无效（需 HH:MM:SS）：{value!r}。')
    seconds = int(match[1]) * 3600 + int(match[2]) * 60 + int(match[3])
    if seconds <= 0:
        raise ContractError('CONTRACT_SCHEMA_INVALID', '运行时间必须大于零。')
    return seconds


def parse_utc(value: Any, label: str = 'timestamp') -> datetime:
    text = _require_str(value, label)
    iso = text[:-1] + '+00:00' if text.endswith('Z') else text
    try:
        parsed = datetime.fromisoformat(iso)
    except ValueError as exc:
        raise ContractError('CONTRACT_SCHEMA_INVALID', f'{label} 时间格式无效：{value!r}。') from exc
    if parsed.tzinfo is None:
        raise ContractError('CONTRACT_SCHEMA_INVALID', f'{label} 必须带时区（推荐 UTC Z）。')
    return parsed.astimezone(timezone.utc)


def format_utc(dt: datetime | None = None) -> str:
    current = datetime.now(timezone.utc) if dt is None else dt.astimezone(timezone.utc)
    return current.replace(microsecond=0).strftime('%Y-%m-%dT%H:%M:%SZ')


def _validate_rel_posix_path(value: Any, label: str) -> str:
    text = _require_str(value, label)
    if '\\' in text or ':' in text or text.startswith('/'):
        raise ContractError('CONTRACT_SCHEMA_INVALID', f'{label} 必须是工作区相对 POSIX 路径：{text!r}。')
    parts = PurePosixPath(text).parts
    if not parts or any(part in ('', '.', '..') for part in parts):
        raise ContractError('CONTRACT_SCHEMA_INVALID', f'{label} 不能包含父目录跳转或空段：{text!r}。')
    return '/'.join(parts)


def canonical_payload(contract: dict[str, Any]) -> dict[str, Any]:
    """Deterministic copy of contract excluding volatile digest/signature fields."""
    payload = deepcopy(contract)
    approval = payload.get('approval')
    if isinstance(approval, dict):
        approval.pop('canonical_sha256', None)
        approval.pop('ssh_signature', None)
    return payload


def canonical_bytes(contract: dict[str, Any]) -> bytes:
    """Deterministic UTF-8 JSON bytes hashed and optionally signed by OpenSSH."""
    return json.dumps(canonical_payload(contract), ensure_ascii=False, sort_keys=True, separators=(',', ':')).encode('utf-8')


def canonical_sha256(contract: dict[str, Any]) -> str:
    return hashlib.sha256(canonical_bytes(contract)).hexdigest()


def validate_physics_assertion(physics: Any, dimension: Any) -> dict[str, Any]:
    """Enforce deterministic non-magnetic PBE 3D/2D physics boundary."""
    if dimension not in ('3D', '2D'):
        raise ContractError('CONTRACT_UNSUPPORTED_PHYSICS', f'自治合同仅支持 3D 或 2D 体系，拒绝维度 {dimension!r}。')
    data = _require_dict(physics, 'physics_assertion')
    _require_str(data.get('goal'), 'physics_assertion.goal')
    if data.get('magnetism') != 'nonmagnetic':
        raise ContractError('CONTRACT_UNSUPPORTED_PHYSICS', '自治合同仅支持非磁性体系（magnetism=nonmagnetic）。')
    if not _is_int(data.get('charge')) or data['charge'] != 0:
        raise ContractError('CONTRACT_UNSUPPORTED_PHYSICS', '自治合同仅支持电中性体系（charge=0）。')
    if data.get('functional') != 'PBE':
        raise ContractError('CONTRACT_UNSUPPORTED_PHYSICS', '自治合同仅支持 PBE 泛函。')
    for flag in ('soc', 'hubbard_u', 'hybrid'):
        if data.get(flag) is not False:
            raise ContractError('CONTRACT_UNSUPPORTED_PHYSICS', f'自治合同不支持 {flag}=true 的物理体系。')
    if data.get('confirmed_by') != 'operator_slash_command':
        raise ContractError('CONTRACT_UNSUPPORTED_PHYSICS', 'physics_assertion.confirmed_by 必须为 operator_slash_command。')
    if data.get('assumptions_source') != 'operator_assertion_not_structure_derived':
        raise ContractError('CONTRACT_UNSUPPORTED_PHYSICS', 'physics_assertion.assumptions_source 不符合要求。')
    return data


def validate_resources_in_envelope(
    resources: Any, phase: str, envelope: dict[str, Any], *, server: str | None = None, tests: int | None = None
) -> dict[str, Any]:
    """Ensure a phase's requested Slurm resources stay strictly inside the contract envelope."""
    if phase not in stage_definitions.known_names():
        raise ContractError('CONTRACT_SCOPE_VIOLATION', f'未知阶段：{phase!r}。')
    res = _require_dict(resources, f'resources[{phase}]')
    mode = res.get('submission_mode', 'allocation')
    if mode not in ('allocation', 'array') or mode not in envelope['allowed_modes']:
        raise ContractError('CONTRACT_RESOURCE_ENVELOPE_EXCEEDED', f'提交模式 {mode!r} 不在合同允许列表内。')
    if mode == 'array' and (phase != 'convergence' or server is None):
        raise ContractError('CONTRACT_RESOURCE_ENVELOPE_EXCEEDED', 'array 模式仅允许用于远端 convergence 阶段。')
    partition = res.get('partition')
    if not isinstance(partition, str) or not _PART_RE.fullmatch(partition) or partition not in envelope['allowed_partitions']:
        raise ContractError('CONTRACT_RESOURCE_ENVELOPE_EXCEEDED', f'分区 {partition!r} 不在合同允许列表内。')
    parallel, cores, nodes = res.get('parallel_tests'), res.get('cores_per_test'), res.get('nodes')
    if not (_is_int(parallel) and _is_int(cores) and _is_int(nodes)):
        raise ContractError('CONTRACT_RESOURCE_ENVELOPE_EXCEEDED', 'parallel_tests / cores_per_test / nodes 必须为整数。')
    if parallel < 1 or parallel > envelope['max_parallel_tests'] or (phase != 'convergence' and parallel != 1):
        raise ContractError('CONTRACT_RESOURCE_ENVELOPE_EXCEEDED', f'并发数 {parallel} 超出阶段或合同上限。')
    if tests is not None and _is_int(tests) and parallel > tests:
        raise ContractError('CONTRACT_RESOURCE_ENVELOPE_EXCEEDED', f'并发数 {parallel} 超过测试项数 {tests}。')
    if cores < 1 or cores > envelope['max_cores_per_test'] or nodes < 1 or nodes > envelope['max_nodes'] or cores % nodes != 0:
        raise ContractError('CONTRACT_RESOURCE_ENVELOPE_EXCEEDED', f'核数/节点配置 ({cores}/{nodes}) 超出合同包络或不可整除。')
    if parallel * cores > envelope['max_total_cores']:
        raise ContractError('CONTRACT_RESOURCE_ENVELOPE_EXCEEDED', f'总核数 {parallel * cores} 超出合同上限 {envelope["max_total_cores"]}。')
    try:
        req_wall, max_wall = wall_seconds(res.get('walltime')), wall_seconds(envelope['max_walltime'])
    except ContractError as exc:
        raise ContractError('CONTRACT_RESOURCE_ENVELOPE_EXCEEDED', str(exc)) from exc
    if req_wall > max_wall:
        raise ContractError('CONTRACT_RESOURCE_ENVELOPE_EXCEEDED', f'运行时限 {res.get("walltime")} 超出合同上限 {envelope["max_walltime"]}。')
    return {'submission_mode': mode, 'parallel_tests': parallel, 'cores_per_test': cores,
            'nodes': nodes, 'partition': partition, 'walltime': res['walltime']}


def _validate_scope(scope: Any) -> dict[str, Any]:
    data = _require_dict(scope, 'scope')
    server = data.get('server')
    if server is not None and (not isinstance(server, str) or server == 'local' or not _SERVER_RE.fullmatch(server)):
        raise ContractError('CONTRACT_SCHEMA_INVALID', f'无效的 scope.server：{server!r}。')
    validate_job_name(data.get('job_name'))
    _validate_rel_posix_path(data.get('structure'), 'scope.structure')
    _require_hex(data.get('structure_sha256'), _HEX64_RE, 'scope.structure_sha256')
    if data.get('environment_profile') is not None:
        prof = _require_dict(data['environment_profile'], 'scope.environment_profile')
        _require_str(prof.get('name'), 'scope.environment_profile.name')
        _require_hex(prof.get('environment_sha256'), _HEX64_RE, 'scope.environment_profile.environment_sha256')
        _require_hex(prof.get('executable_sha256'), _HEX64_RE, 'scope.environment_profile.executable_sha256')
    if type(data.get('allow_forks')) is not bool:
        raise ContractError('CONTRACT_SCHEMA_INVALID', 'scope.allow_forks 必须为布尔值。')
    return data


def _validate_workflow(workflow: Any, *, allow_forks: bool) -> dict[str, Any]:
    data = _require_dict(workflow, 'workflow')
    _require_hex(data.get('plan_id'), _HEX32_RE, 'workflow.plan_id')
    _require_hex(data.get('plan_sha256'), _HEX64_RE, 'workflow.plan_sha256')
    phases, known = data.get('allowed_phases'), stage_definitions.known_names()
    if not isinstance(phases, list) or not phases or any(not isinstance(p, str) or p not in known for p in phases):
        raise ContractError('CONTRACT_SCHEMA_INVALID', 'workflow.allowed_phases 必须为非空已知阶段列表。')
    if len(set(phases)) != len(phases) or [known.index(p) for p in phases] != sorted(known.index(p) for p in phases):
        raise ContractError('CONTRACT_SCHEMA_INVALID', 'workflow.allowed_phases 不可重复且须遵循标准阶段顺序。')
    if not allow_forks or 'convergence' in phases:
        if not allow_forks and phases[0] != 'convergence':
            raise ContractError('CONTRACT_SCHEMA_INVALID', '非派生合同的 allowed_phases 须从 convergence 开始。')
        for idx, phase in enumerate(phases):
            latest = max(stage_definitions.DEFINITIONS[phase])
            deps = stage_definitions.DEFINITIONS[phase][latest]['depends_on']
            missing = [dep for dep in deps if dep not in phases[:idx]]
            if missing:
                raise ContractError('CONTRACT_SCHEMA_INVALID', f'阶段 {phase} 缺少前置依赖阶段：{"、".join(missing)}。')
    if data.get('scan_preset') not in workflow_policy.SCAN_PRESETS:
        raise ContractError('CONTRACT_SCHEMA_INVALID', f'workflow.scan_preset 无效：{data.get("scan_preset")!r}。')
    if type(data.get('allow_threshold_relaxation')) is not bool:
        raise ContractError('CONTRACT_SCHEMA_INVALID', 'workflow.allow_threshold_relaxation 必须为布尔值。')
    if data['allow_threshold_relaxation'] is True:
        raise ContractError('CONTRACT_THRESHOLD_RELAXATION_FORBIDDEN', '自治合同严禁开启 allow_threshold_relaxation。')
    return data


def _validate_bounds_budget_review(bounds: Any, budget: Any, review_policy: Any) -> None:
    pb = _require_dict(bounds, 'precision_bounds')
    if not _is_number(pb.get('max_encut')) or not 200 <= pb['max_encut'] <= 3000:
        raise ContractError('CONTRACT_SCHEMA_INVALID', 'precision_bounds.max_encut 须在 200–3000 之间。')
    if not _is_int(pb.get('max_kmesh_axis')) or not 1 <= pb['max_kmesh_axis'] <= 128:
        raise ContractError('CONTRACT_SCHEMA_INVALID', 'precision_bounds.max_kmesh_axis 须为 1–128 整数。')
    if not _is_int(pb.get('max_kmesh_points')) or not 1 <= pb['max_kmesh_points'] <= 128 ** 3:
        raise ContractError('CONTRACT_SCHEMA_INVALID', 'precision_bounds.max_kmesh_points 无效。')
    bg = _require_dict(budget, 'budget')
    for key, lo, hi in (('max_submissions', 1, 200), ('max_resume_retries', 0, 20),
                        ('max_repair_retries', 0, 20), ('max_scan_retries', 0, 20), ('max_consecutive_failures', 1, 20)):
        if not _is_int(bg.get(key)) or not lo <= bg[key] <= hi:
            raise ContractError('CONTRACT_SCHEMA_INVALID', f'budget.{key} 须为 {lo}–{hi} 整数。')
    rp = _require_dict(review_policy, 'review_policy')
    if rp.get('mode') not in REVIEW_MODES or type(rp.get('auto_advance_on_green')) is not bool:
        raise ContractError('CONTRACT_SCHEMA_INVALID', 'review_policy 配置无效。')


def _validate_envelope(envelope: Any, *, server: str | None, allowed_phases: list[str]) -> dict[str, Any]:
    env = _require_dict(envelope, 'resource_envelope')
    modes, parts = env.get('allowed_modes'), env.get('allowed_partitions')
    if not isinstance(modes, list) or not modes or any(m not in ('allocation', 'array') for m in modes):
        raise ContractError('CONTRACT_SCHEMA_INVALID', 'resource_envelope.allowed_modes 无效。')
    if server is None and 'array' in modes:
        raise ContractError('CONTRACT_SCHEMA_INVALID', '本地任务合同不允许启用 array 提交模式。')
    if not isinstance(parts, list) or not parts or any(not isinstance(p, str) or not _PART_RE.fullmatch(p) for p in parts):
        raise ContractError('CONTRACT_SCHEMA_INVALID', 'resource_envelope.allowed_partitions 无效。')
    for key, lo, hi in (('max_parallel_tests', 1, 6), ('max_cores_per_test', 1, 65536),
                        ('max_nodes', 1, 256), ('max_total_cores', 1, 65536)):
        if not _is_int(env.get(key)) or not lo <= env[key] <= hi:
            raise ContractError('CONTRACT_SCHEMA_INVALID', f'resource_envelope.{key} 须为 {lo}–{hi} 整数。')
    wall_seconds(env.get('max_walltime'))
    defaults = _require_dict(env.get('phase_defaults'), 'resource_envelope.phase_defaults')
    if set(defaults) != set(allowed_phases):
        raise ContractError('CONTRACT_SCHEMA_INVALID', 'resource_envelope.phase_defaults 须与 allowed_phases 一一对应。')
    for phase, res in defaults.items():
        validate_resources_in_envelope(res, phase, env, server=server)
    return env


def validate_contract_schema(doc: Any, *, require_approval: bool = True) -> dict[str, Any]:
    """Validate draft (`require_approval=False`) or approved contract (`require_approval=True`)."""
    data = _require_dict(doc, 'contract')
    if data.get('contract_version') != CONTRACT_VERSION:
        raise ContractError('CONTRACT_SCHEMA_INVALID', f'不支持的合同版本：{data.get("contract_version")!r}。')
    validate_identifier(data.get('draft_id'), 'draft_id')
    if data.get('maker_actor_id') is not None:
        maker_id = _require_str(data.get('maker_actor_id'), 'maker_actor_id')
        if len(maker_id) > 128 or maker_id != maker_id.strip() or any(ord(ch) < 32 for ch in maker_id):
            raise ContractError('CONTRACT_SCHEMA_INVALID', 'maker_actor_id 格式无效。')
    if require_approval:
        validate_identifier(data.get('contract_id'), 'contract_id')
    if parse_utc(data.get('expires_at'), 'expires_at') <= parse_utc(data.get('created_at'), 'created_at'):
        raise ContractError('CONTRACT_SCHEMA_INVALID', 'expires_at 必须晚于 created_at。')
    scope = _validate_scope(data.get('scope'))
    validate_physics_assertion(data.get('physics_assertion'), scope.get('dimension'))
    workflow = _validate_workflow(data.get('workflow'), allow_forks=scope['allow_forks'])
    _validate_bounds_budget_review(data.get('precision_bounds'), data.get('budget'), data.get('review_policy'))
    _validate_envelope(data.get('resource_envelope'), server=scope['server'], allowed_phases=workflow['allowed_phases'])
    if require_approval:
        approval = _require_dict(data.get('approval'), 'approval')
        if approval.get('mode') not in RESERVED_APPROVAL_MODES:
            raise ContractError('CONTRACT_SCHEMA_INVALID', f'无效签发模式：{approval.get("mode")!r}。')
        parse_utc(approval.get('approved_at'), 'approval.approved_at')
        _require_hex(approval.get('canonical_sha256'), _HEX64_RE, 'approval.canonical_sha256')
        if approval.get('ssh_signature') is not None and not isinstance(approval['ssh_signature'], dict):
            raise ContractError('CONTRACT_SCHEMA_INVALID', 'approval.ssh_signature 必须为对象或 null。')
    return data


def _effective_thresholds(preset: str, overrides: dict[str, float] | None) -> dict[str, float]:
    values = {**_DEFAULT_THRESHOLDS, **workflow_policy.SCAN_PRESETS.get(preset, {})}
    if overrides:
        values.update(overrides)
    return values


def validate_stricter_retry(
    contract: dict[str, Any], *, source_encuts: list[float], source_meshes: list[list[int]],
    new_encuts: list[float] | None = None, new_meshes: list[list[int]] | None = None,
    mode: str = 'both', potential_floor: float = 200.0, scan_thresholds: dict[str, Any] | None = None,
    source_scan_thresholds: dict[str, Any] | None = None, scan_preset: str | None = None,
    source_scan_preset: str | None = None,
) -> dict[str, Any]:
    """Enforce strictly tighter/monotonic scan retry inside contract precision_bounds."""
    if mode not in ('encut', 'mesh', 'both'):
        raise ContractError('CONTRACT_SCHEMA_INVALID', f'无效重扫模式：{mode!r}。')
    try:
        src = scan_config.validate_scans(list(source_encuts), [list(m) for m in source_meshes])
    except ValueError as exc:
        raise ContractError('CONTRACT_SCHEMA_INVALID', f'来源扫描序列无效：{exc}') from exc
    src_preset = source_scan_preset or contract['workflow']['scan_preset']
    dst_preset = scan_preset or src_preset
    if src_preset not in _PRESET_RANK or dst_preset not in _PRESET_RANK:
        raise ContractError('CONTRACT_SCHEMA_INVALID', '扫描档位必须为 standard 或 strict。')
    if _PRESET_RANK[dst_preset] < _PRESET_RANK[src_preset]:
        raise ContractError('CONTRACT_THRESHOLD_RELAXATION_FORBIDDEN', '严禁在自动重扫中放宽 scan_preset。')
    try:
        clean_src_th = workflow_policy.validate_scan_thresholds(source_scan_thresholds)
        clean_dst_th = workflow_policy.validate_scan_thresholds(scan_thresholds)
    except ValueError as exc:
        raise ContractError('CONTRACT_SCHEMA_INVALID', str(exc)) from exc
    eff_src = _effective_thresholds(src_preset, clean_src_th)
    eff_dst = _effective_thresholds(dst_preset, clean_dst_th if scan_thresholds is not None else clean_src_th)
    for key, src_val in eff_src.items():
        if eff_dst[key] > src_val:
            raise ContractError('CONTRACT_THRESHOLD_RELAXATION_FORBIDDEN', f'严禁放宽扫描阈值：{key} ({eff_dst[key]} > {src_val})。')

    bounds, out_encuts, out_meshes = contract['precision_bounds'], src['encuts'], src['meshes']
    if mode in ('encut', 'both'):
        if new_encuts is None:
            raise ContractError('CONTRACT_RETRY_NOT_STRICTER', '重扫 ENCUT 必须提供新的三级序列。')
        try:
            out_encuts = scan_config.validate_scans(list(new_encuts), src['meshes'])['encuts']
        except ValueError as exc:
            raise ContractError('CONTRACT_RETRY_NOT_STRICTER', str(exc)) from exc
        if out_encuts[0] < potential_floor or out_encuts[-1] <= src['encuts'][-1]:
            raise ContractError('CONTRACT_RETRY_NOT_STRICTER', '新 ENCUT 序列必须不低于赝势下限且最高值严格高于来源。')
        if out_encuts[-1] > bounds['max_encut']:
            raise ContractError('CONTRACT_PRECISION_BOUND_EXCEEDED', f'新 ENCUT 最高值 {out_encuts[-1]} 超出合同上限 {bounds["max_encut"]}。')
    if mode in ('mesh', 'both'):
        if new_meshes is None:
            raise ContractError('CONTRACT_RETRY_NOT_STRICTER', '重扫 k 网格必须提供新的三级网格。')
        try:
            out_meshes = scan_config.validate_scans(out_encuts, [list(m) for m in new_meshes])['meshes']
        except ValueError as exc:
            raise ContractError('CONTRACT_RETRY_NOT_STRICTER', str(exc)) from exc
        is_2d = contract['scope']['dimension'] == '2D'
        for old_m, new_m in zip(src['meshes'], out_meshes):
            if (is_2d and new_m[2] != 1) or any(nv < ov for ov, nv in zip(old_m, new_m)):
                raise ContractError('CONTRACT_RETRY_NOT_STRICTER', '新网格须严格不小于来源网格，且 2D 保持 kz == 1。')
            if max(new_m) > bounds['max_kmesh_axis'] or new_m[0] * new_m[1] * new_m[2] > bounds['max_kmesh_points']:
                raise ContractError('CONTRACT_PRECISION_BOUND_EXCEEDED', '新网格单轴或总 k 点数超出合同精度上限。')
        old_pts = src['meshes'][-1][0] * src['meshes'][-1][1] * src['meshes'][-1][2]
        new_pts = out_meshes[-1][0] * out_meshes[-1][1] * out_meshes[-1][2]
        if new_pts <= old_pts:
            raise ContractError('CONTRACT_RETRY_NOT_STRICTER', '新最密网格总 k 点数必须严格大于来源最密网格。')
    return {'mode': mode, 'encuts': out_encuts, 'meshes': out_meshes, 'scan_preset': dst_preset,
            'scan_thresholds': clean_dst_th if scan_thresholds is not None else clean_src_th}
