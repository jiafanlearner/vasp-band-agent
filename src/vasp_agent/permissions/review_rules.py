"""Pure Layer 3 validation, reviewer identity formatting, and symlink/junction guards for review attestations.

Zero upward imports into chat/ or control/; shared by `permissions/review_attestation.py`.
"""
from __future__ import annotations

from copy import deepcopy
from datetime import datetime
import hashlib
import hmac
import json
from pathlib import Path, PurePosixPath
import re
import time
from typing import Any, Iterable

from vasp_agent.common import agent_paths
from vasp_agent.permissions.contract_rules import (
    REVIEW_MODES, ContractError, parse_utc, validate_identifier, validate_job_name,
)
from vasp_agent.workflow import stage_definitions

LEASE_VERSION = 1
ATTESTATION_VERSION = 1
REVIEW_ROLES = ('maker', 'checker')
DEFAULT_LEASE_TTL_SECONDS = 15 * 60
MIN_LEASE_TTL_SECONDS = 1
MAX_LEASE_TTL_SECONDS = 60 * 60

_HEX64_RE = re.compile(r'^[0-9a-f]{64}$')
_SERVER_RE = re.compile(r'^[A-Za-z0-9][A-Za-z0-9_-]{0,47}$')
_SEGMENT_UNSAFE_RE = re.compile(r'[^A-Za-z0-9._-]+')
_REVIEWER_RE = re.compile(r'^(maker|checker):[A-Za-z0-9._-]+:[A-Za-z0-9._-]+:[A-Za-z0-9._-]+$')


def is_link(path: Path) -> bool:
    return path.is_symlink() or bool(getattr(path, 'is_junction', lambda: False)())


def normalize_server(server: str | None) -> str | None:
    if server is None or server == 'local':
        return None
    if not isinstance(server, str) or not _SERVER_RE.fullmatch(server):
        raise ContractError('REVIEW_INVALID_INPUT', f'无效的服务器名：{server!r}。')
    return server


def require_role(role: Any) -> str:
    if role not in REVIEW_ROLES:
        raise ContractError('REVIEW_INVALID_INPUT', f'无效审阅角色：{role!r}，必须为 maker 或 checker。')
    return str(role)


def require_actor_id(actor_id: Any, label: str = 'actor_id') -> str:
    if not isinstance(actor_id, str):
        raise ContractError('REVIEW_INVALID_INPUT', f'{label} 必须是非空字符串。')
    cleaned = actor_id.strip()
    if not cleaned or len(cleaned) > 128 or any(ord(ch) < 32 for ch in cleaned):
        raise ContractError('REVIEW_INVALID_INPUT', f'{label} 格式无效或包含控制字符。')
    return cleaned


def require_phase(phase: Any) -> str:
    if not isinstance(phase, str) or phase not in stage_definitions.known_names():
        raise ContractError('REVIEW_INVALID_INPUT', f'未知阶段：{phase!r}。')
    return phase


def require_snapshot(snapshot: Any) -> str:
    if not isinstance(snapshot, str) or not _HEX64_RE.fullmatch(snapshot):
        raise ContractError('REVIEW_INVALID_INPUT', '审阅 snapshot 必须是 64 位小写十六进制 SHA-256。')
    return snapshot


def sanitize_segment(value: Any, *, fallback: str, max_len: int = 32) -> str:
    if not isinstance(value, str):
        return fallback
    cleaned = _SEGMENT_UNSAFE_RE.sub('_', value.strip()).strip('._-')
    return cleaned[:max_len] if cleaned else fallback


def format_reviewer(*, role: str, actor_id: str, host: str = 'external', model: str = 'unknown') -> str:
    """Build structured remote reviewer tag `role:host:model:actor[:12]`."""
    clean_role, clean_actor = require_role(role), require_actor_id(actor_id)
    host_seg = sanitize_segment(host, fallback='external', max_len=32)
    model_seg = sanitize_segment(model, fallback='unknown', max_len=32)
    actor_fallback = hashlib.sha256(clean_actor.encode('utf-8')).hexdigest()[:12]
    actor_seg = sanitize_segment(clean_actor[:12], fallback=actor_fallback, max_len=12)
    return f'{clean_role}:{host_seg}:{model_seg}:{actor_seg}'


def parse_reviewer(reviewer: Any) -> dict[str, str] | None:
    if not isinstance(reviewer, str) or not _REVIEWER_RE.fullmatch(reviewer):
        return None
    role, host, model, actor_prefix = reviewer.split(':', 3)
    return {'role': role, 'host': host, 'model': model, 'actor_prefix': actor_prefix}


def canonical_record_sha256(record: dict[str, Any]) -> str:
    payload = deepcopy(record)
    payload.pop('canonical_sha256', None)
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(',', ':')).encode('utf-8')
    return hashlib.sha256(encoded).hexdigest()


def reject_symlink_chain(root: Path, target: Path, label: str) -> None:
    """Reject symlinks and Windows junctions on every existing component from `root` to `target`."""
    try:
        rel_parts = target.relative_to(root).parts
    except ValueError as exc:
        raise ContractError('REVIEW_PATH_ESCAPE', f'{label} 超出授权工作区。') from exc
    cursor = root
    for part in rel_parts:
        cursor = cursor / part
        if is_link(cursor):
            raise ContractError('REVIEW_SYMLINK_REFUSED', f'{label} 路径包含符号链接或目录联接：{cursor.name}')
    if target.exists() and not target.resolve().is_relative_to(root):
        raise ContractError('REVIEW_PATH_ESCAPE', f'{label} 解析后超出授权工作区。')


def store_dirs(workspace: str | Path) -> tuple[Path, Path, Path]:
    root = Path(workspace).resolve(strict=True)
    rel_store = PurePosixPath(agent_paths.AGENT_DIR, agent_paths.STORES['review_attestations'][0])
    base = root.joinpath(*rel_store.parts)
    leases = base / 'leases'
    reject_symlink_chain(root, base, '审阅背书目录')
    reject_symlink_chain(root, leases, '审阅租约目录')
    return root, base, leases


def read_text_retry(path: Path) -> str:
    for attempt in range(5):
        try:
            return path.read_text(encoding='utf-8')
        except PermissionError:
            if attempt == 4:
                raise
            time.sleep(0.01 * (attempt + 1))
    return path.read_text(encoding='utf-8')


def load_verified_record(
    path: Path, *, root: Path, label: str, version_key: str, expected_version: int, invalid_code: str,
) -> dict[str, Any]:
    reject_symlink_chain(root, path, label)
    if not path.is_file():
        raise ContractError(invalid_code, f'找不到 {label}。')
    try:
        data = json.loads(read_text_retry(path))
    except (OSError, ValueError) as exc:
        raise ContractError(invalid_code, f'{label} JSON 损坏或无法读取。') from exc
    if not isinstance(data, dict) or type(data.get(version_key)) is not int or data.get(version_key) != expected_version:
        raise ContractError(invalid_code, f'{label} 结构或版本无效。')
    recorded = data.get('canonical_sha256')
    if not isinstance(recorded, str) or not _HEX64_RE.fullmatch(recorded):
        raise ContractError(invalid_code, f'{label} 缺少有效完整性哈希。')
    if not hmac.compare_digest(canonical_record_sha256(data), recorded):
        raise ContractError(invalid_code, f'{label} 完整性校验失败，疑似损坏或篡改。')
    return data


def validate_lease_record(rec: dict[str, Any], *, expected_lease_id: str | None = None) -> tuple[datetime, datetime]:
    try:
        lid = validate_identifier(rec.get('lease_id'), 'lease_id')
        if expected_lease_id is not None and lid != expected_lease_id:
            raise ContractError('REVIEW_LEASE_INVALID', '租约 lease_id 与文件名不一致。')
        require_actor_id(rec.get('actor_id'))
        require_role(rec.get('role'))
        server = rec.get('server')
        if server is not None and (server == 'local' or normalize_server(server) != server):
            raise ContractError('REVIEW_LEASE_INVALID', '租约 server 字段无效。')
        validate_job_name(rec.get('job_name'))
        require_phase(rec.get('phase'))
        require_snapshot(rec.get('snapshot'))
        if not isinstance(rec.get('host'), str) or not rec['host'].strip():
            raise ContractError('REVIEW_LEASE_INVALID', '租约 host 字段无效。')
        if not isinstance(rec.get('model'), str) or not rec['model'].strip():
            raise ContractError('REVIEW_LEASE_INVALID', '租约 model 字段无效。')
        issued = parse_utc(rec.get('issued_at'), 'issued_at')
        expires = parse_utc(rec.get('expires_at'), 'expires_at')
        if expires < issued:
            raise ContractError('REVIEW_LEASE_INVALID', '租约 expires_at 早于 issued_at。')
        return issued, expires
    except ContractError as exc:
        raise ContractError('REVIEW_LEASE_INVALID', str(exc)) from exc


def validate_attestation_record(rec: dict[str, Any]) -> None:
    try:
        if rec.get('contract_id') is not None:
            validate_identifier(rec['contract_id'], 'contract_id')
        server = rec.get('server')
        if server is not None and (server == 'local' or normalize_server(server) != server):
            raise ContractError('REVIEW_ATTESTATION_INVALID', '背书 server 字段无效。')
        validate_job_name(rec.get('job_name'))
        require_phase(rec.get('phase'))
        require_snapshot(rec.get('snapshot'))
        role = require_role(rec.get('role'))
        if rec.get('review_mode') is not None and rec['review_mode'] not in REVIEW_MODES:
            raise ContractError('REVIEW_ATTESTATION_INVALID', '背书 review_mode 无效。')
        if rec.get('maker_actor_id') is not None:
            require_actor_id(rec['maker_actor_id'], 'maker_actor_id')
        require_actor_id(rec.get('checker_actor_id'), 'checker_actor_id')
        validate_identifier(rec.get('lease_id'), 'lease_id')
        if type(rec.get('allowed')) is not bool:
            raise ContractError('REVIEW_ATTESTATION_INVALID', '背书 allowed 必须为布尔值。')
        for key in ('blocking_items', 'program_failures'):
            val = rec.get(key)
            if not isinstance(val, list) or any(not isinstance(item, str) for item in val):
                raise ContractError('REVIEW_ATTESTATION_INVALID', f'背书 {key} 必须为字符串列表。')
        parse_utc(rec.get('time'), 'time')
        parsed_rev = parse_reviewer(rec.get('reviewer'))
        if parsed_rev is None or parsed_rev['role'] != role:
            raise ContractError('REVIEW_ATTESTATION_INVALID', '本地审阅背书的 reviewer 标识无效。')
    except ContractError as exc:
        raise ContractError('REVIEW_ATTESTATION_INVALID', str(exc)) from exc


def enforce_review_policy(
    review_mode: str, *, role: str, actor_id: str, maker_actor_ids: Iterable[str | None] = (),
) -> None:
    """Enforce `maker_checker` (independent checker != maker) vs explicit `maker_direct`."""
    if review_mode not in REVIEW_MODES:
        raise ContractError('CONTRACT_SCHEMA_INVALID', f'未知审阅模式：{review_mode!r}。')
    clean_role, clean_actor = require_role(role), require_actor_id(actor_id)
    makers = {m.strip() for m in maker_actor_ids if isinstance(m, str) and m.strip()}
    if review_mode == 'maker_checker':
        if clean_role != 'checker':
            raise ContractError('REVIEW_CHECKER_ROLE_REQUIRED', '合同处于 maker_checker 模式，仅允许独立只读 checker 角色记录审阅，禁止 maker 自审。')
        if clean_actor in makers:
            raise ContractError('REVIEW_SELF_APPROVAL_FORBIDDEN', f'maker_checker 模式禁止审阅者与任务发起/提交者身份相同（actor_id={clean_actor!r}）。')


def extract_blocking_items(checks: Any) -> list[str]:
    if not isinstance(checks, list) or not checks:
        raise ContractError('REVIEW_INVALID_INPUT', 'checks 必须为非空审阅条目列表。')
    blocking: list[str] = []
    for entry in checks:
        if not isinstance(entry, dict) or not isinstance(entry.get('item'), str) or not entry['item'].strip():
            raise ContractError('REVIEW_INVALID_INPUT', '审阅检查项格式无效。')
        if type(entry.get('blocking')) is not bool:
            raise ContractError('REVIEW_INVALID_INPUT', f'审阅项 {entry["item"]} 缺少布尔型 blocking 字段。')
        if (entry['blocking'] or entry.get('judgment') == 'blocked') and entry['item'] not in blocking:
            blocking.append(entry['item'])
    return blocking
