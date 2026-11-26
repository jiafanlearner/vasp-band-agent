"""Layer 3 task autonomy contract facade: TTY approval, OpenSSH verification, and lifecycle APIs.

Zero upward imports into chat/ or control/; delegates rules to contract_rules and FileLock ledger
state machine to contract_ledger.
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from copy import deepcopy
from datetime import datetime
import hashlib
import hmac
import json
import os
from pathlib import Path
import shutil
import stat
import subprocess
import sys
from typing import Any, Callable
from uuid import uuid4

from vasp_agent.common import agent_paths
from vasp_agent.permissions import contract_ledger as _ledger
from vasp_agent.permissions.contract_rules import (
    ACTIVE_LEDGER_STATUSES,
    ALL_LEDGER_STATUSES,
    CONTRACT_VERSION,
    HALTED_LEDGER_STATUSES,
    RESERVED_APPROVAL_MODES,
    REVIEW_MODES,
    SSH_SIGN_NAMESPACE,
    SUPPORTED_APPROVAL_MODES,
    TERMINAL_LEDGER_STATUSES,
    ContractError,
    canonical_bytes,
    canonical_payload,
    canonical_sha256,
    format_utc,
    parse_utc,
    validate_contract_schema,
    validate_identifier,
    validate_job_name,
    validate_physics_assertion,
    validate_resources_in_envelope,
    validate_stricter_retry,
    wall_seconds,
)

__all__ = [
    'ACTIVE_LEDGER_STATUSES', 'ALL_LEDGER_STATUSES', 'ApprovalProvider', 'CONTRACT_VERSION',
    'ContractError', 'HALTED_LEDGER_STATUSES', 'InWindowApprovalProvider', 'LocalTTYApprovalProvider',
    'RESERVED_APPROVAL_MODES', 'REVIEW_MODES', 'SSH_SIGN_NAMESPACE', 'SUPPORTED_APPROVAL_MODES',
    'TERMINAL_LEDGER_STATUSES', 'approve_draft', 'canonical_bytes', 'canonical_payload',
    'canonical_sha256', 'complete_contract', 'confirm_submission', 'contract_status', 'format_utc',
    'halt_contract', 'has_real_tty', 'list_drafts', 'load_autonomy_policy', 'load_contract',
    'load_draft', 'new_contract_id', 'new_draft_id', 'parse_utc', 'read_ledger',
    'record_phase_outcome', 'register_retry', 'release_in_flight', 'require_real_tty',
    'reserve_submission', 'resume_contract', 'revoke_contract', 'save_draft', 'sign_contract_ssh',
    'validate_contract_schema', 'validate_identifier', 'validate_job_name',
    'validate_physics_assertion', 'validate_resources_in_envelope', 'validate_stricter_retry',
    'verify_contract', 'verify_contract_ssh', 'wall_seconds',
]

_CONFIRM_TOKENS = {'1', 'yes', 'y', 'approve', '确认'}
read_ledger = _ledger.read_ledger
halt_contract = _ledger.halt_contract


def new_draft_id(now: datetime | None = None) -> str:
    return f'drf-{_ledger.utc_now(now).strftime("%Y%m%d")}-{uuid4().hex[:8]}'


def new_contract_id(now: datetime | None = None) -> str:
    return f'ctr-{_ledger.utc_now(now).strftime("%Y%m%d")}-{uuid4().hex[:8]}'


# ----------------------------------------------------------- Real TTY & Providers

def has_real_tty(stream: Any = None) -> bool:
    """True only when `stream` (default sys.stdin) is backed by a real interactive terminal."""
    target = sys.stdin if stream is None else stream
    if target is None or not hasattr(target, 'isatty'):
        return False
    try:
        if not target.isatty():
            return False
        fd = target.fileno()
    except (OSError, ValueError, AttributeError):
        return False
    if os.name == 'nt':
        try:
            import ctypes
            import msvcrt

            handle = msvcrt.get_osfhandle(fd)
            mode = ctypes.c_uint32()
            return bool(ctypes.windll.kernel32.GetConsoleMode(ctypes.c_void_p(handle), ctypes.byref(mode)))
        except (OSError, ValueError, AttributeError):
            return False
    try:
        return os.isatty(fd) and stat.S_ISCHR(os.fstat(fd).st_mode)
    except OSError:
        return False


def require_real_tty(stream: Any = None) -> None:
    if not has_real_tty(stream):
        raise ContractError(
            'CONTRACT_TTY_REQUIRED',
            '签发、撤销或恢复自治合同必须在真实交互终端（TTY）执行，拒绝管道或非交互输入。',
        )


class ApprovalProvider(ABC):
    """Abstract approval boundary (v1 enables only `local_tty`; reserves `in_window_approval`)."""

    mode: str

    @abstractmethod
    def confirm(self, prompt_title: str, summary_text: str) -> bool:
        """Ask the human operator to confirm an out-of-band contract transition."""


class LocalTTYApprovalProvider(ApprovalProvider):
    mode = 'local_tty'

    def __init__(
        self, *, reader: Callable[[str], str] | None = None,
        output: Callable[[str], Any] | None = None, stream: Any = None,
    ):
        self.reader = input if reader is None else reader
        self.output = print if output is None else output
        self.stream = stream

    def confirm(self, prompt_title: str, summary_text: str) -> bool:
        require_real_tty(self.stream)
        if summary_text:
            self.output(summary_text)
        return self.reader(f'{prompt_title}（输入 1 确认，其他输入取消）：').strip().lower() in _CONFIRM_TOKENS


class InWindowApprovalProvider(ApprovalProvider):
    """Reserved provider for future host-native UI approval callbacks (disabled in v1)."""

    mode = 'in_window_approval'

    def confirm(self, prompt_title: str, summary_text: str) -> bool:
        raise ContractError(
            'APPROVAL_MODE_UNSUPPORTED',
            '当前版本仅启用本地真实终端 local_tty 签发；in_window_approval 预留至后续宿主原生审批能力。',
        )


def _require_provider(provider: ApprovalProvider | None) -> ApprovalProvider:
    chosen = LocalTTYApprovalProvider() if provider is None else provider
    if getattr(chosen, 'mode', None) not in SUPPORTED_APPROVAL_MODES:
        raise ContractError('APPROVAL_MODE_UNSUPPORTED', f'签发模式 {getattr(chosen, "mode", None)!r} 在当前版本未启用，仅支持 local_tty。')
    return chosen


# ---------------------------------------------- Autonomy Policy & Optional SSH

def _resolve_ssh_keygen() -> str | None:
    return shutil.which('ssh-keygen')


def load_autonomy_policy(workspace: str | Path) -> dict[str, Any]:
    root = Path(workspace).resolve(strict=True)
    path = agent_paths.file_path(root, 'autonomy_policy')
    if path.is_symlink():
        raise ContractError('CONTRACT_SYMLINK_REFUSED', 'autonomy-policy 不能是符号链接。')
    if not path.exists():
        return {'require_ssh_signature': False, 'allowed_signers': None, 'principal': None}
    try:
        data = json.loads(path.read_text(encoding='utf-8'))
    except (OSError, ValueError) as exc:
        raise ContractError('CONTRACT_POLICY_INVALID', 'autonomy-policy.json 格式无效。') from exc
    if not isinstance(data, dict) or type(data.get('require_ssh_signature', False)) is not bool:
        raise ContractError('CONTRACT_POLICY_INVALID', 'autonomy-policy.json 的 require_ssh_signature 必须为布尔值。')
    req, signers, principal = bool(data.get('require_ssh_signature', False)), data.get('allowed_signers'), data.get('principal')
    if req and (not isinstance(signers, str) or not signers.strip()):
        raise ContractError('CONTRACT_POLICY_INVALID', '开启 require_ssh_signature 时必须配置 allowed_signers 路径。')
    if signers is not None:
        if not isinstance(signers, str) or not signers.strip():
            raise ContractError('CONTRACT_POLICY_INVALID', 'autonomy-policy.json 的 allowed_signers 路径无效。')
        signer_path = Path(signers)
        candidate = signer_path if signer_path.is_absolute() else Path(os.path.normpath(root / signer_path))
        if candidate.is_symlink():
            raise ContractError('CONTRACT_SYMLINK_REFUSED', 'allowed_signers 不能是符号链接。')
        signers = str(candidate)
    if principal is not None and (not isinstance(principal, str) or not principal.strip()):
        raise ContractError('CONTRACT_POLICY_INVALID', 'autonomy-policy.json 的 principal 无效。')
    return {'require_ssh_signature': req, 'allowed_signers': signers, 'principal': principal}


def sign_contract_ssh(
    canonical: bytes, sig_path: Path, *, key_path: str | Path, namespace: str = SSH_SIGN_NAMESPACE
) -> Path:
    binary = _resolve_ssh_keygen()
    if not binary:
        raise ContractError('CONTRACT_SIGNATURE_INVALID', '未找到 ssh-keygen，无法执行合同 OpenSSH 签名。')
    key = Path(key_path)
    if key.is_symlink() or not key.is_file():
        raise ContractError('CONTRACT_SIGNATURE_INVALID', '签名私钥文件不存在或为符号链接。')
    try:
        proc = subprocess.run(
            [binary, '-Y', 'sign', '-f', str(key), '-n', namespace],
            input=canonical, capture_output=True, timeout=30, check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise ContractError('CONTRACT_SIGNATURE_INVALID', f'执行 ssh-keygen 签名失败：{exc}') from exc
    if proc.returncode != 0 or not proc.stdout:
        detail = proc.stderr.decode('utf-8', errors='replace').strip()[:200]
        raise ContractError('CONTRACT_SIGNATURE_INVALID', f'ssh-keygen 签名失败：{detail or proc.returncode}')
    _ledger.atomic_write_text(sig_path, proc.stdout.decode('utf-8').replace('\r\n', '\n'))
    return sig_path


def verify_contract_ssh(
    canonical: bytes, sig_path: Path, *, allowed_signers: str | Path, principal: str,
    namespace: str = SSH_SIGN_NAMESPACE,
) -> None:
    binary = _resolve_ssh_keygen()
    if not binary:
        raise ContractError('CONTRACT_SIGNATURE_INVALID', '未找到 ssh-keygen，无法验证合同 OpenSSH 签名。')
    signers = Path(allowed_signers)
    if sig_path.is_symlink() or not sig_path.is_file() or signers.is_symlink() or not signers.is_file():
        raise ContractError('CONTRACT_SIGNATURE_INVALID', '合同签名文件或 allowed_signers 缺失或为符号链接。')
    try:
        proc = subprocess.run(
            [binary, '-Y', 'verify', '-f', str(signers), '-I', principal, '-n', namespace, '-s', str(sig_path)],
            input=canonical, capture_output=True, timeout=30, check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise ContractError('CONTRACT_SIGNATURE_INVALID', f'执行 ssh-keygen 验签失败：{exc}') from exc
    if proc.returncode != 0:
        detail = proc.stderr.decode('utf-8', errors='replace').strip()[:200]
        raise ContractError('CONTRACT_SIGNATURE_INVALID', f'合同 OpenSSH 验签失败：{detail or proc.returncode}')


# ----------------------------------------------------------- Drafts & Contracts

def save_draft(workspace: str | Path, draft: dict[str, Any]) -> dict[str, Any]:
    cleaned = deepcopy(draft)
    if not cleaned.get('draft_id'):
        cleaned['draft_id'] = new_draft_id()
    validate_contract_schema(cleaned, require_approval=False)
    _, folder = _ledger.contracts_subdir(workspace, 'drafts')
    _ledger.atomic_write_json(folder / f'{cleaned["draft_id"]}.json', cleaned)
    return cleaned


def load_draft(workspace: str | Path, draft_id: str) -> dict[str, Any]:
    validate_identifier(draft_id, 'draft_id')
    root, folder = _ledger.contracts_subdir(workspace, 'drafts')
    path = _ledger.safe_existing_file(folder / f'{draft_id}.json', f'合同草案 {draft_id}', workspace=root)
    try:
        data = json.loads(path.read_text(encoding='utf-8'))
    except (OSError, ValueError) as exc:
        raise ContractError('CONTRACT_SCHEMA_INVALID', f'合同草案 {draft_id} 无法解析。') from exc
    validate_contract_schema(data, require_approval=False)
    if data.get('draft_id') != draft_id:
        raise ContractError('CONTRACT_SCHEMA_INVALID', '合同草案文件与 draft_id 不一致。')
    return data


def list_drafts(workspace: str | Path) -> list[dict[str, Any]]:
    root, folder = _ledger.contracts_subdir(workspace, 'drafts')
    if not folder.is_dir():
        return []
    drafts = []
    for path in sorted(folder.glob('*.json')):
        if path.is_symlink() or not path.resolve().is_relative_to(root):
            continue
        try:
            drafts.append(json.loads(path.read_text(encoding='utf-8')))
        except (OSError, ValueError):
            continue
    return drafts


def approve_draft(
    workspace: str | Path, draft_id: str, *, provider: ApprovalProvider | None = None,
    now: datetime | None = None, ssh_key: str | Path | None = None, ssh_principal: str | None = None,
    review_mode: str | None = None, contract_id: str | None = None, summary_text: str = '',
) -> tuple[dict[str, Any], dict[str, Any]]:
    chosen = _require_provider(provider)
    draft = load_draft(workspace, draft_id)
    policy = load_autonomy_policy(workspace)
    current = _ledger.utc_now(now)
    if current > parse_utc(draft['expires_at'], 'expires_at'):
        raise ContractError('CONTRACT_EXPIRED', '合同草案已过期，不能签发。')
    if review_mode is not None and review_mode not in REVIEW_MODES:
        raise ContractError('CONTRACT_SCHEMA_INVALID', f'无效审阅模式：{review_mode!r}。')
    if policy['require_ssh_signature'] and ssh_key is None:
        raise ContractError('CONTRACT_SIGNATURE_REQUIRED', '工作区策略要求 OpenSSH 签名，签发时必须提供私钥路径。')
    if ssh_key is not None:
        key = Path(ssh_key)
        if not _resolve_ssh_keygen() or key.is_symlink() or not key.is_file():
            raise ContractError('CONTRACT_SIGNATURE_INVALID', '缺少 ssh-keygen 或签名私钥文件无效。')
    cid = validate_identifier(contract_id, 'contract_id') if contract_id else new_contract_id(current)
    root, active_dir = _ledger.contracts_subdir(workspace, 'active')
    active_path = active_dir / f'{cid}.json'
    _, ledger_path, _ = _ledger.ledger_paths(root, cid)
    if active_path.exists() or ledger_path.exists():
        raise ContractError('CONTRACT_ALREADY_EXISTS', f'合同 {cid} 已存在，不可覆盖。')
    if not chosen.confirm(f'签发任务自治合同（草案 {draft_id}）', summary_text):
        raise ContractError('CONTRACT_APPROVAL_CANCELLED', '操作者已取消签发合同，未启用自治权限。')

    contract = deepcopy(draft)
    contract.pop('approved_contract_id', None)
    contract['contract_id'] = cid
    if review_mode is not None:
        contract['review_policy']['mode'] = review_mode
    contract['approval'] = {'mode': chosen.mode, 'approved_at': format_utc(current), 'canonical_sha256': '', 'ssh_signature': None}
    canon = canonical_bytes(contract)
    contract['approval']['canonical_sha256'] = hashlib.sha256(canon).hexdigest()

    sig_path: Path | None = None
    wrote_active = False
    try:
        if ssh_key is not None:
            principal = ssh_principal or policy.get('principal') or 'operator@local'
            sig_path = active_dir / f'{cid}.json.sig'
            sign_contract_ssh(canon, sig_path, key_path=ssh_key)
            if policy.get('allowed_signers'):
                verify_contract_ssh(canon, sig_path, allowed_signers=policy['allowed_signers'], principal=principal)
            contract['approval']['ssh_signature'] = {'namespace': SSH_SIGN_NAMESPACE, 'sig_file': sig_path.name, 'principal': principal}
        validate_contract_schema(contract, require_approval=True)
        _ledger.atomic_write_json(active_path, contract, exclusive=True)
        wrote_active = True
        ledger = _ledger.create_initial_ledger(root, contract, current)
    except Exception:
        if wrote_active:
            active_path.unlink(missing_ok=True)
        if sig_path is not None:
            sig_path.unlink(missing_ok=True)
        raise
    return contract, ledger


def load_contract(workspace: str | Path, contract_id: str) -> dict[str, Any]:
    validate_identifier(contract_id, 'contract_id')
    root, active_dir = _ledger.contracts_subdir(workspace, 'active')
    path = _ledger.safe_existing_file(active_dir / f'{contract_id}.json', f'合同 {contract_id}', workspace=root)
    try:
        contract = json.loads(path.read_text(encoding='utf-8'))
    except (OSError, ValueError) as exc:
        raise ContractError('CONTRACT_TAMPERED', f'合同 {contract_id} 无法解析，疑似损坏或篡改。') from exc
    if not isinstance(contract, dict) or contract.get('contract_id') != contract_id:
        raise ContractError('CONTRACT_TAMPERED', '合同文件结构或 contract_id 与文件名不一致。')
    approval = contract.get('approval')
    recorded_sha = approval.get('canonical_sha256') if isinstance(approval, dict) else None
    if not isinstance(recorded_sha, str) or len(recorded_sha) != 64:
        raise ContractError('CONTRACT_TAMPERED', '合同缺少有效 canonical_sha256，疑似篡改。')
    canon = canonical_bytes(contract)
    if not hmac.compare_digest(hashlib.sha256(canon).hexdigest(), recorded_sha):
        raise ContractError('CONTRACT_TAMPERED', f'合同 {contract_id} 哈希校验失败，疑似被篡改。')

    validate_contract_schema(contract, require_approval=True)
    if approval['mode'] not in SUPPORTED_APPROVAL_MODES:
        raise ContractError('APPROVAL_MODE_UNSUPPORTED', f'合同签发模式未启用：{approval["mode"]!r}。')

    policy = load_autonomy_policy(root)
    sig_meta = approval.get('ssh_signature')
    expected_sig_file = f'{contract_id}.json.sig'
    must_verify = policy['require_ssh_signature'] or (isinstance(sig_meta, dict) and bool(policy.get('allowed_signers')))
    if must_verify:
        if not isinstance(sig_meta, dict) or sig_meta.get('sig_file') != expected_sig_file:
            raise ContractError('CONTRACT_SIGNATURE_INVALID', '合同 OpenSSH 签名元数据缺失或文件名不匹配。')
        if sig_meta.get('namespace') != SSH_SIGN_NAMESPACE:
            raise ContractError('CONTRACT_SIGNATURE_INVALID', '合同 OpenSSH 签名命名空间不匹配。')
        principal = policy.get('principal') or sig_meta.get('principal')
        if not isinstance(principal, str) or not principal.strip():
            raise ContractError('CONTRACT_SIGNATURE_INVALID', '缺少 OpenSSH 验签主体（principal）。')
        if policy.get('principal') and sig_meta.get('principal') and sig_meta['principal'] != policy['principal']:
            raise ContractError('CONTRACT_SIGNATURE_INVALID', '合同签名主体与工作区策略 principal 不一致。')
        verify_contract_ssh(
            canon, active_dir / expected_sig_file, allowed_signers=policy['allowed_signers'],
            principal=principal, namespace=SSH_SIGN_NAMESPACE,
        )
    return contract


def verify_contract(
    workspace: str | Path, contract_id: str, *, now: datetime | None = None, require_active: bool = True
) -> tuple[dict[str, Any], dict[str, Any]]:
    contract = load_contract(workspace, contract_id)
    current = _ledger.utc_now(now)
    with _ledger.ledger_transaction(workspace, contract_id, write=True) as ledger:
        if ledger.get('canonical_sha256') != contract['approval']['canonical_sha256']:
            raise ContractError('CONTRACT_TAMPERED', '合同账本哈希与不可变合同不匹配。')
        _ledger.reconcile_expiry(contract, ledger, current)
        snapshot = deepcopy(ledger)
    if require_active:
        status = snapshot['status']
        if status == 'revoked':
            raise ContractError('CONTRACT_REVOKED', f'合同 {contract_id} 已被操作者撤销。')
        if status in HALTED_LEDGER_STATUSES:
            raise ContractError('CONTRACT_HALTED', f'合同 {contract_id} 已熔断（{status}）：{snapshot.get("halt_reason") or ""}')
        if status == 'expired':
            raise ContractError('CONTRACT_EXPIRED', f'合同 {contract_id} 已过期。')
        if status == 'completed':
            raise ContractError('CONTRACT_COMPLETED', f'合同 {contract_id} 已完成。')
    return contract, snapshot


def contract_status(workspace: str | Path, contract_id: str | None = None, *, now: datetime | None = None) -> dict[str, Any]:
    return _ledger.summarize_contracts(workspace, contract_id, loader=load_contract, verifier=verify_contract, now=_ledger.utc_now(now))


def revoke_contract(
    workspace: str | Path, contract_id: str, *, reason: str = 'operator_revoked',
    provider: ApprovalProvider | None = None, now: datetime | None = None, summary_text: str = '',
) -> dict[str, Any]:
    chosen = _require_provider(provider)
    if not chosen.confirm(f'撤销任务自治合同 {contract_id}', summary_text):
        raise ContractError('CONTRACT_APPROVAL_CANCELLED', '操作者已取消撤销合同。')
    return _ledger.apply_revoke(workspace, contract_id, reason=reason, now=_ledger.utc_now(now))


def resume_contract(
    workspace: str | Path, contract_id: str, *, reason: str = 'operator_resumed',
    provider: ApprovalProvider | None = None, reset_consecutive_failures: bool = True,
    now: datetime | None = None, summary_text: str = '',
) -> dict[str, Any]:
    chosen = _require_provider(provider)
    contract = load_contract(workspace, contract_id)
    current = _ledger.utc_now(now)
    if current > parse_utc(contract['expires_at'], 'expires_at'):
        raise ContractError('CONTRACT_EXPIRED', f'合同 {contract_id} 已过期，无法恢复。')
    status = read_ledger(workspace, contract_id)['status']
    if status == 'revoked':
        raise ContractError('CONTRACT_REVOKED', f'合同 {contract_id} 已撤销，无法恢复。')
    if status == 'expired':
        raise ContractError('CONTRACT_EXPIRED', f'合同 {contract_id} 已过期，无法恢复。')
    if status == 'completed':
        raise ContractError('CONTRACT_COMPLETED', f'合同 {contract_id} 已完成，无需恢复。')
    if status not in HALTED_LEDGER_STATUSES:
        raise ContractError('CONTRACT_NOT_HALTED', f'合同 {contract_id} 当前状态为 {status}，无需恢复。')
    if not chosen.confirm(f'恢复已熔断合同 {contract_id}', summary_text):
        raise ContractError('CONTRACT_APPROVAL_CANCELLED', '操作者已取消恢复合同。')
    return _ledger.apply_resume(workspace, contract_id, reason=reason, reset_consecutive_failures=reset_consecutive_failures, now=current)


def complete_contract(
    workspace: str | Path, contract_id: str, *, summary: dict[str, Any] | None = None, now: datetime | None = None
) -> dict[str, Any]:
    return _ledger.complete_ledger(workspace, load_contract(workspace, contract_id), summary=summary, now=now)


def reserve_submission(
    workspace: str | Path, contract_id: str, *, server: str | None, job_name: str, phase: str,
    resources: dict[str, Any], operation_id: str | None = None, tests: int | None = None, now: datetime | None = None,
) -> dict[str, Any]:
    return _ledger.reserve_submission_for(
        workspace, load_contract(workspace, contract_id), server=server, job_name=job_name,
        phase=phase, resources=resources, operation_id=operation_id, tests=tests, now=now,
    )


def confirm_submission(
    workspace: str | Path, contract_id: str, *, job_id: str, operation_id: str | None = None,
    recovered: bool = False, now: datetime | None = None,
) -> dict[str, Any]:
    return _ledger.confirm_submission_for(
        workspace, load_contract(workspace, contract_id), job_id=job_id,
        operation_id=operation_id, recovered=recovered, now=now,
    )


def release_in_flight(
    workspace: str | Path, contract_id: str, *, operation_id: str | None = None,
    refund: bool = False, reason: str = '', now: datetime | None = None,
) -> dict[str, Any]:
    return _ledger.release_in_flight_for(
        workspace, load_contract(workspace, contract_id), operation_id=operation_id,
        refund=refund, reason=reason, now=now,
    )


def record_phase_outcome(
    workspace: str | Path, contract_id: str, *, job_name: str, phase: str,
    passed: bool, failure_info: dict[str, Any] | None = None, now: datetime | None = None,
) -> dict[str, Any]:
    return _ledger.record_phase_outcome_for(
        workspace, load_contract(workspace, contract_id), job_name=job_name,
        phase=phase, passed=passed, failure_info=failure_info, now=now,
    )


def register_retry(
    workspace: str | Path, contract_id: str, *, kind: str, new_job_name: str | None = None,
    diagnostic: dict[str, Any] | None = None, now: datetime | None = None,
) -> dict[str, Any]:
    return _ledger.register_retry_for(
        workspace, load_contract(workspace, contract_id), kind=kind,
        new_job_name=new_job_name, diagnostic=diagnostic, now=now,
    )
