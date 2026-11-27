"""Layer 3 Maker-Checker review read-receipt leases, local attestations, and submit-gate facade.

Zero upward imports into chat/ or control/; delegates pure validation, reviewer formatting, and
symlink/junction checks to `permissions/review_rules.py` while keeping a complete re-export facade.
"""
from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timedelta
import hmac
import json
from pathlib import Path
from typing import Any, Iterable
from uuid import uuid4

from vasp_agent.common.file_lock import FileLock
from vasp_agent.permissions import contract_ledger as _ledger
from vasp_agent.permissions.contract_rules import (
    REVIEW_MODES, ContractError, format_utc, parse_utc, validate_identifier, validate_job_name,
)
from vasp_agent.permissions.review_rules import (
    ATTESTATION_VERSION,
    DEFAULT_LEASE_TTL_SECONDS,
    LEASE_VERSION,
    MAX_LEASE_TTL_SECONDS,
    MIN_LEASE_TTL_SECONDS,
    REVIEW_ROLES,
    canonical_record_sha256,
    enforce_review_policy,
    extract_blocking_items,
    format_reviewer,
    is_link,
    load_verified_record,
    normalize_server,
    parse_reviewer,
    reject_symlink_chain,
    require_actor_id,
    require_phase,
    require_role,
    require_snapshot,
    sanitize_segment,
    store_dirs,
    validate_attestation_record,
    validate_lease_record,
)

__all__ = [
    'ATTESTATION_VERSION', 'DEFAULT_LEASE_TTL_SECONDS', 'LEASE_VERSION', 'MAX_LEASE_TTL_SECONDS',
    'MIN_LEASE_TTL_SECONDS', 'REVIEW_ROLES', 'attestation_path', 'canonical_record_sha256',
    'enforce_review_policy', 'extract_blocking_items', 'format_reviewer', 'issue_lease',
    'lease_path', 'list_leases', 'load_attestation', 'normalize_server', 'parse_reviewer',
    'prune_expired_leases', 'reject_symlink_chain', 'save_attestation',
    'verify_attestation_for_submit', 'verify_lease',
]


def lease_path(workspace: str | Path, lease_id: str) -> Path:
    validate_identifier(lease_id, 'lease_id')
    root, _, leases = store_dirs(workspace)
    target = leases / f'{lease_id}.json'
    reject_symlink_chain(root, target, f'审阅租约 {lease_id}')
    return target


def attestation_path(
    workspace: str | Path, job_name: str, phase: str, snapshot: str, *, server: str | None = None,
) -> Path:
    norm_server = normalize_server(server)
    validate_job_name(job_name)
    require_phase(phase)
    require_snapshot(snapshot)
    root, base, _ = store_dirs(workspace)
    target = base / 'scopes' / (norm_server or 'local') / f'{job_name}-{phase}-{snapshot[:12]}.json'
    reject_symlink_chain(root, target, f'审阅背书 {target.name}')
    return target


# ----------------------------------------------------------- Read-Receipt Leases

def prune_expired_leases(workspace: str | Path, *, now: datetime | None = None) -> int:
    """Remove expired non-symlink lease files under `.agent/state/review-attestations/leases/`."""
    root, _, leases = store_dirs(workspace)
    if not leases.is_dir():
        return 0
    current, removed = _ledger.utc_now(now), 0
    for path in sorted(leases.glob('*.json')):
        if is_link(path) or not path.resolve().is_relative_to(root):
            continue
        try:
            data = json.loads(path.read_text(encoding='utf-8'))
            if not isinstance(data, dict):
                continue
            expires = parse_utc(data.get('expires_at'), 'expires_at')
        except (OSError, ValueError, ContractError):
            continue
        if current > expires:
            try:
                path.unlink(missing_ok=True)
                removed += 1
            except OSError:
                continue
    return removed


def list_leases(workspace: str | Path, *, include_expired: bool = False, now: datetime | None = None) -> list[dict[str, Any]]:
    """Read valid leases without creating directories."""
    root, _, leases = store_dirs(workspace)
    if not leases.is_dir():
        return []
    current, found = _ledger.utc_now(now), []
    for path in sorted(leases.glob('*.json')):
        if is_link(path) or not path.resolve().is_relative_to(root):
            continue
        try:
            rec = load_verified_record(
                path, root=root, label=f'审阅租约 {path.stem}',
                version_key='lease_version', expected_version=LEASE_VERSION, invalid_code='REVIEW_LEASE_INVALID',
            )
            _, expires = validate_lease_record(rec, expected_lease_id=path.stem)
        except ContractError:
            continue
        if include_expired or current <= expires:
            found.append(rec)
    return found


def issue_lease(
    workspace: str | Path, *, actor_id: str, role: str, server: str | None, job_name: str, phase: str,
    snapshot: str, host: str = 'external', model: str = 'unknown',
    ttl_seconds: int = DEFAULT_LEASE_TTL_SECONDS, now: datetime | None = None,
) -> dict[str, Any]:
    """Mint a short-lived read-receipt lease bound to `(actor_id, role, server, job_name, phase, snapshot)`."""
    clean_actor, clean_role, norm_server = require_actor_id(actor_id), require_role(role), normalize_server(server)
    validate_job_name(job_name)
    require_phase(phase)
    require_snapshot(snapshot)
    if type(ttl_seconds) is not int or not MIN_LEASE_TTL_SECONDS <= ttl_seconds <= MAX_LEASE_TTL_SECONDS:
        raise ContractError('REVIEW_INVALID_INPUT', f'租约 TTL 必须为 {MIN_LEASE_TTL_SECONDS}–{MAX_LEASE_TTL_SECONDS} 秒整数。')
    current = _ledger.utc_now(now)
    prune_expired_leases(workspace, now=current)
    lease_id = f'lse-{current.strftime("%Y%m%d")}-{uuid4().hex[:12]}'
    target = lease_path(workspace, lease_id)
    record: dict[str, Any] = {
        'lease_version': LEASE_VERSION, 'lease_id': lease_id, 'actor_id': clean_actor, 'role': clean_role,
        'host': sanitize_segment(host, fallback='external', max_len=32),
        'model': sanitize_segment(model, fallback='unknown', max_len=32),
        'server': norm_server, 'job_name': job_name, 'phase': phase, 'snapshot': snapshot,
        'issued_at': format_utc(current), 'expires_at': format_utc(current + timedelta(seconds=ttl_seconds)),
    }
    record['canonical_sha256'] = canonical_record_sha256(record)
    _ledger.atomic_write_json(target, record, exclusive=True)
    return deepcopy(record)


def _lease_matches_target(
    lease: dict[str, Any], *, actor_id: str, role: str, server: str | None, job_name: str, phase: str,
    host: str | None = None, model: str | None = None,
) -> bool:
    return (
        lease.get('actor_id') == actor_id and lease.get('role') == role and lease.get('server') == server
        and lease.get('job_name') == job_name and lease.get('phase') == phase
        and (host is None or lease.get('host') == host)
        and (model is None or lease.get('model') == model)
    )


def verify_lease(
    workspace: str | Path, *, actor_id: str, role: str, server: str | None, job_name: str, phase: str,
    snapshot: str, lease_id: str | None = None, host: str | None = None, model: str | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Verify an active, untampered read-receipt lease matching the caller and exact snapshot."""
    clean_actor, clean_role, norm_server = require_actor_id(actor_id), require_role(role), normalize_server(server)
    clean_host = sanitize_segment(host, fallback='external', max_len=32) if host is not None else None
    clean_model = sanitize_segment(model, fallback='unknown', max_len=32) if model is not None else None
    validate_job_name(job_name)
    require_phase(phase)
    require_snapshot(snapshot)
    current = _ledger.utc_now(now)
    root, _, leases_dir = store_dirs(workspace)

    if lease_id is not None:
        target = lease_path(workspace, lease_id)
        if not target.exists():
            raise ContractError('REVIEW_LEASE_REQUIRED', f'找不到指定的审阅读回执租约：{lease_id}。')
        rec = load_verified_record(
            target, root=root, label=f'审阅租约 {lease_id}',
            version_key='lease_version', expected_version=LEASE_VERSION, invalid_code='REVIEW_LEASE_INVALID',
        )
        _, expires = validate_lease_record(rec, expected_lease_id=lease_id)
        if not _lease_matches_target(
            rec, actor_id=clean_actor, role=clean_role, server=norm_server, job_name=job_name, phase=phase,
            host=clean_host, model=clean_model,
        ):
            raise ContractError('REVIEW_LEASE_INVALID', '审阅租约与当前调用者身份（角色/宿主/模型）或目标任务阶段不匹配。')
        if not hmac.compare_digest(rec['snapshot'], snapshot):
            raise ContractError('REVIEW_SNAPSHOT_MISMATCH', '读回执租约快照与本次记录快照不一致，请重新读取。')
        if current > expires:
            raise ContractError('REVIEW_LEASE_EXPIRED', '审阅读回执租约已过期，请重新调用 vasp_get_stage_review。')
        return deepcopy(rec)

    if not leases_dir.is_dir():
        raise ContractError('REVIEW_LEASE_REQUIRED', '缺少当前身份对应的有效阶段审阅读回执租约，请先调用 vasp_get_stage_review。')

    valid_exact: list[tuple[datetime, datetime, str, dict[str, Any]]] = []
    saw_expired_exact = saw_other_snapshot = False
    for path in sorted(leases_dir.glob('*.json')):
        if is_link(path) or not path.resolve().is_relative_to(root):
            continue
        try:
            rec = load_verified_record(
                path, root=root, label=f'审阅租约 {path.stem}',
                version_key='lease_version', expected_version=LEASE_VERSION, invalid_code='REVIEW_LEASE_INVALID',
            )
            issued, expires = validate_lease_record(rec, expected_lease_id=path.stem)
        except ContractError:
            continue
        if not _lease_matches_target(
            rec, actor_id=clean_actor, role=clean_role, server=norm_server, job_name=job_name, phase=phase,
            host=clean_host, model=clean_model,
        ):
            continue
        if hmac.compare_digest(rec['snapshot'], snapshot):
            if current <= expires:
                valid_exact.append((expires, issued, rec['lease_id'], rec))
            else:
                saw_expired_exact = True
        elif current <= expires:
            saw_other_snapshot = True

    if valid_exact:
        valid_exact.sort(key=lambda item: (item[0], item[1], item[2]))
        return deepcopy(valid_exact[-1][3])
    if saw_expired_exact:
        raise ContractError('REVIEW_LEASE_EXPIRED', '审阅读回执租约已过期，请重新调用 vasp_get_stage_review。')
    if saw_other_snapshot:
        raise ContractError('REVIEW_SNAPSHOT_MISMATCH', '读回执租约快照与本次提交快照不一致，请重新调用 vasp_get_stage_review。')
    raise ContractError('REVIEW_LEASE_REQUIRED', '缺少当前身份对应的有效阶段审阅读回执租约，请先调用 vasp_get_stage_review。')


# ------------------------------------------- Local Attestation & Submit Gate

def save_attestation(
    workspace: str | Path, *, server: str | None, job_name: str, phase: str, snapshot: str, role: str,
    checker_actor_id: str, reviewer: str, lease_id: str, allowed: bool, checks: list[dict[str, Any]],
    contract_id: str | None = None, maker_actor_id: str | None = None, review_mode: str | None = None,
    host: str = 'external', model: str = 'unknown', program_failures: Iterable[str] = (),
    review_time: str | None = None, now: datetime | None = None,
) -> dict[str, Any]:
    """Persist local attestation under `.agent/state/review-attestations/scopes/<server>/<job>-<phase>-<snap[:12]>.json`."""
    norm_server, clean_role = normalize_server(server), require_role(role)
    validate_job_name(job_name)
    require_phase(phase)
    require_snapshot(snapshot)
    clean_checker = require_actor_id(checker_actor_id, 'checker_actor_id')
    validate_identifier(lease_id, 'lease_id')
    if contract_id is not None:
        validate_identifier(contract_id, 'contract_id')
    clean_maker = require_actor_id(maker_actor_id, 'maker_actor_id') if maker_actor_id is not None else None
    if review_mode is not None and review_mode not in REVIEW_MODES:
        raise ContractError('CONTRACT_SCHEMA_INVALID', f'无效审阅模式：{review_mode!r}。')
    if type(allowed) is not bool:
        raise ContractError('REVIEW_INVALID_INPUT', 'allowed 必须为布尔值。')
    clean_host = sanitize_segment(host, fallback='external', max_len=32)
    clean_model = sanitize_segment(model, fallback='unknown', max_len=32)
    expected_rev = format_reviewer(role=clean_role, host=clean_host, model=clean_model, actor_id=clean_checker)
    if reviewer != expected_rev:
        raise ContractError('REVIEW_INVALID_INPUT', f'结构化 reviewer 与当前身份不匹配：{reviewer!r} != {expected_rev!r}。')
    blocking_items = extract_blocking_items(checks)
    prog_fails = [str(item) for item in program_failures if str(item).strip()]
    if allowed and (blocking_items or prog_fails):
        raise ContractError('REVIEW_BLOCKED_INCONSISTENT', '存在阻断项或程序预检失败时，背书 allowed 不得为 true。')

    current = _ledger.utc_now(now)
    ts = format_utc(parse_utc(review_time, 'review_time')) if review_time else format_utc(current)
    root, base, _ = store_dirs(workspace)
    target = attestation_path(workspace, job_name, phase, snapshot, server=norm_server)
    lock_file = base / '.attestation.lock'
    reject_symlink_chain(root, lock_file, '审阅背书锁文件')
    record: dict[str, Any] = {
        'attestation_version': ATTESTATION_VERSION, 'contract_id': contract_id, 'server': norm_server,
        'job_name': job_name, 'phase': phase, 'snapshot': snapshot, 'role': clean_role,
        'review_mode': review_mode, 'maker_actor_id': clean_maker, 'checker_actor_id': clean_checker,
        'host': clean_host, 'model': clean_model, 'reviewer': reviewer, 'lease_id': lease_id,
        'allowed': allowed, 'blocking_items': blocking_items, 'program_failures': prog_fails,
        'checks_count': len(checks), 'time': ts,
    }
    record['canonical_sha256'] = canonical_record_sha256(record)
    lock_file.parent.mkdir(parents=True, exist_ok=True)
    with _ledger.thread_lock_for(lock_file), _ledger.thread_lock_for(target):
        flock = FileLock(lock_file, blocking=True)
        flock.acquire()
        try:
            _ledger.atomic_write_json(target, record, exclusive=False)
        finally:
            flock.release()
    return deepcopy(record)


def load_attestation(
    workspace: str | Path, *, server: str | None, job_name: str, phase: str, snapshot: str,
) -> dict[str, Any]:
    """Load and verify structural/hash integrity of a local attestation without creating directories."""
    norm_server = normalize_server(server)
    validate_job_name(job_name)
    require_phase(phase)
    require_snapshot(snapshot)
    root, _, _ = store_dirs(workspace)
    target = attestation_path(workspace, job_name, phase, snapshot, server=norm_server)
    with _ledger.thread_lock_for(target):
        if not target.exists():
            raise ContractError(
                'REVIEW_ATTESTATION_MISSING',
                f'缺少阶段 {phase} 当前快照（{snapshot[:12]}）的本地审阅背书，请先经独立审阅调用 vasp_record_stage_review。',
            )
        rec = load_verified_record(
            target, root=root, label=f'审阅背书 {target.name}',
            version_key='attestation_version', expected_version=ATTESTATION_VERSION,
            invalid_code='REVIEW_ATTESTATION_INVALID',
        )
    validate_attestation_record(rec)
    if (
        rec['server'] != norm_server or rec['job_name'] != job_name or rec['phase'] != phase
        or not hmac.compare_digest(rec['snapshot'], snapshot)
    ):
        raise ContractError('REVIEW_ATTESTATION_MISMATCH', '本地审阅背书与当前服务器、任务、阶段或证据快照不匹配。')
    return deepcopy(rec)


def verify_attestation_for_submit(
    workspace: str | Path, contract: dict[str, Any], *, server: str | None, job_name: str, phase: str,
    snapshot: str, submitting_actor_id: str, maker_actor_ids: Iterable[str | None] = (),
) -> dict[str, Any]:
    """Enforce the local Maker-Checker attestation gate immediately before submission."""
    clean_submitter = require_actor_id(submitting_actor_id, 'submitting_actor_id')
    contract_id = validate_identifier(contract.get('contract_id'), 'contract_id')
    review_mode = (contract.get('review_policy') or {}).get('mode')
    att = load_attestation(workspace, server=server, job_name=job_name, phase=phase, snapshot=snapshot)
    if att['contract_id'] != contract_id:
        raise ContractError(
            'REVIEW_ATTESTATION_MISMATCH',
            f'本地审阅背书绑定的合同（{att["contract_id"]!r}）与当前提交合同（{contract_id}）不一致。',
        )
    if att['allowed'] is not True or att['blocking_items'] or att['program_failures']:
        raise ContractError('REVIEW_ATTESTATION_BLOCKED', '本地审阅背书未放行或存在阻断项，禁止提交该阶段。')
    enforce_review_policy(
        str(review_mode), role=att['role'], actor_id=att['checker_actor_id'],
        maker_actor_ids=(clean_submitter, *maker_actor_ids, att['maker_actor_id']),
    )
    return {
        'enforced': True, 'mode': review_mode, 'contract_id': contract_id, 'snapshot': snapshot,
        'role': att['role'], 'checker_actor_id': att['checker_actor_id'], 'reviewer': att['reviewer'],
        'lease_id': att['lease_id'], 'time': att['time'], 'stage_gate_remote_enforced': True,
    }
