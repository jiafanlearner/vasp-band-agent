"""Layer 3 FileLock-protected mutable ledger and atomic storage helpers for task contracts.

Zero upward imports into chat/ or control/; LF UTF-8 atomic writes and symlink rejection.
"""
from __future__ import annotations

from contextlib import contextmanager
from copy import deepcopy
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import threading
from typing import Any, Callable
from uuid import uuid4

from vasp_agent.common import agent_paths
from vasp_agent.common.file_lock import FileLock
from vasp_agent.permissions.contract_rules import (
    ALL_LEDGER_STATUSES, HALTED_LEDGER_STATUSES, TERMINAL_LEDGER_STATUSES, ContractError,
    format_utc, parse_utc, validate_identifier, validate_job_id, validate_job_name, validate_resources_in_envelope,
)

_THREAD_LOCKS_GUARD = threading.Lock()
_THREAD_LOCKS: dict[str, threading.RLock] = {}


def utc_now(now: datetime | None = None) -> datetime:
    return datetime.now(timezone.utc) if now is None else now.astimezone(timezone.utc)


def thread_lock_for(path: Path) -> threading.RLock:
    key = str(path.resolve())
    with _THREAD_LOCKS_GUARD:
        lock = _THREAD_LOCKS.get(key)
        if lock is None:
            lock = threading.RLock()
            _THREAD_LOCKS[key] = lock
        return lock


def contracts_subdir(workspace: str | Path, sub: str) -> tuple[Path, Path]:
    root = Path(workspace).resolve(strict=True)
    base = agent_paths.store_dir(root, 'contracts')
    folder = base / sub
    if base.is_symlink() or folder.is_symlink():
        raise ContractError('CONTRACT_SYMLINK_REFUSED', '合同存储目录不能是符号链接。')
    return root, folder


def safe_existing_file(path: Path, label: str, *, workspace: Path | None = None) -> Path:
    if path.is_symlink():
        raise ContractError('CONTRACT_SYMLINK_REFUSED', f'{label} 不能是符号链接。')
    if not path.is_file():
        raise ContractError('CONTRACT_NOT_FOUND', f'找不到 {label}。')
    if workspace is not None and not path.resolve().is_relative_to(workspace):
        raise ContractError('CONTRACT_PATH_ESCAPE', f'{label} 超出授权工作区。')
    return path


def atomic_write_text(path: Path, text: str, *, exclusive: bool = False) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.is_symlink():
        raise ContractError('CONTRACT_SYMLINK_REFUSED', f'拒绝写入符号链接：{path.name}')
    if exclusive and path.exists():
        raise ContractError('CONTRACT_ALREADY_EXISTS', f'不可变合同文件已存在，禁止覆盖：{path.name}')
    temp = path.parent / f'.{path.name}.{uuid4().hex}.tmp'
    try:
        with temp.open('x', encoding='utf-8', newline='\n') as stream:
            stream.write(text)
            stream.flush()
            os.fsync(stream.fileno())
        if exclusive:
            try:
                os.link(temp, path)
            except FileExistsError as exc:
                raise ContractError('CONTRACT_ALREADY_EXISTS', f'不可变合同文件已存在：{path.name}') from exc
            except OSError:
                if path.exists():
                    raise ContractError('CONTRACT_ALREADY_EXISTS', f'不可变合同文件已存在：{path.name}')
                os.replace(temp, path)
            else:
                temp.unlink(missing_ok=True)
        else:
            os.replace(temp, path)
    finally:
        temp.unlink(missing_ok=True)


def atomic_write_json(path: Path, data: dict[str, Any], *, exclusive: bool = False) -> None:
    atomic_write_text(path, json.dumps(data, ensure_ascii=False, indent=2) + '\n', exclusive=exclusive)


def ledger_paths(workspace: str | Path, contract_id: str) -> tuple[Path, Path, Path]:
    validate_identifier(contract_id, 'contract_id')
    root, folder = contracts_subdir(workspace, 'ledger')
    return root, folder / f'{contract_id}.json', folder / f'{contract_id}.lock'


def create_initial_ledger(workspace: str | Path, contract: dict[str, Any], now: datetime) -> dict[str, Any]:
    ledger = {
        'ledger_version': 1,
        'contract_id': contract['contract_id'],
        'canonical_sha256': contract['approval']['canonical_sha256'],
        'status': 'active',
        'created_at': format_utc(now),
        'updated_at': format_utc(now),
        'submissions_used': 0,
        'resume_retries_used': 0,
        'repair_retries_used': 0,
        'scan_retries_used': 0,
        'consecutive_failures': 0,
        'allowed_jobs': [contract['scope']['job_name']],
        'in_flight': None,
        'halt_reason': None,
        'halt_code': None,
        'diagnostics': [],
        'history': [{'event': 'approved', 'time': format_utc(now), 'mode': contract['approval']['mode']}],
    }
    _, data_path, lock_path = ledger_paths(workspace, contract['contract_id'])
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with thread_lock_for(lock_path):
        flock = FileLock(lock_path, blocking=True)
        flock.acquire()
        try:
            atomic_write_json(data_path, ledger, exclusive=True)
        finally:
            flock.release()
    return deepcopy(ledger)


@contextmanager
def ledger_transaction(workspace: str | Path, contract_id: str, *, write: bool = False):
    root, data_path, lock_path = ledger_paths(workspace, contract_id)
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with thread_lock_for(lock_path):
        flock = FileLock(lock_path, blocking=True)
        flock.acquire()
        try:
            safe_existing_file(data_path, f'合同账本 {contract_id}', workspace=root)
            try:
                raw_before = data_path.read_text(encoding='utf-8')
                ledger = json.loads(raw_before)
            except (OSError, ValueError) as exc:
                raise ContractError('CONTRACT_LEDGER_CORRUPT', '合同账本已损坏，无法读取。') from exc
            if not isinstance(ledger, dict) or ledger.get('status') not in ALL_LEDGER_STATUSES:
                raise ContractError('CONTRACT_LEDGER_CORRUPT', '合同账本状态字段无效。')
            before = json.dumps(ledger, ensure_ascii=False, sort_keys=True, separators=(',', ':')) if write else None
            yield ledger
            if write:
                after = json.dumps(ledger, ensure_ascii=False, sort_keys=True, separators=(',', ':'))
                if after != before:
                    atomic_write_json(data_path, ledger)
        finally:
            flock.release()


def check_ledger_binding(contract: dict[str, Any], ledger: dict[str, Any]) -> None:
    if (
        ledger.get('contract_id') != contract['contract_id']
        or ledger.get('canonical_sha256') != contract['approval']['canonical_sha256']
    ):
        raise ContractError('CONTRACT_TAMPERED', '合同账本与不可变合同标识或哈希不匹配。')


def reconcile_expiry(contract: dict[str, Any], ledger: dict[str, Any], now: datetime) -> bool:
    if now > parse_utc(contract['expires_at'], 'expires_at') and ledger['status'] == 'active':
        ledger['status'] = 'expired'
        ledger['updated_at'] = format_utc(now)
        ledger['history'].append({'event': 'expired', 'time': format_utc(now)})
        return True
    return False


def read_ledger(workspace: str | Path, contract_id: str) -> dict[str, Any]:
    with ledger_transaction(workspace, contract_id, write=False) as ledger:
        return deepcopy(ledger)


def _inactive_error(contract_id: str, status: str) -> ContractError:
    if status == 'revoked':
        return ContractError('CONTRACT_REVOKED', f'合同 {contract_id} 已被操作者撤销。')
    if status == 'completed':
        return ContractError('CONTRACT_COMPLETED', f'合同 {contract_id} 已完成。')
    if status == 'expired':
        return ContractError('CONTRACT_EXPIRED', f'合同 {contract_id} 已过期。')
    return ContractError('CONTRACT_HALTED', f'合同当前不可执行（status={status}）。')


def halt_contract(
    workspace: str | Path, contract_id: str, reason: str, *, code: str = 'CIRCUIT_BREAKER',
    uncertain: bool = False, details: dict[str, Any] | None = None, actor: str = 'agent',
    now: datetime | None = None,
) -> dict[str, Any]:
    current = utc_now(now)
    with ledger_transaction(workspace, contract_id, write=True) as ledger:
        if ledger['status'] in TERMINAL_LEDGER_STATUSES:
            raise ContractError('CONTRACT_TERMINAL', f'合同处于终态 {ledger["status"]}，无法熔断。')
        target = 'halted_uncertain' if (uncertain or ledger['status'] == 'halted_uncertain') else 'halted'
        ledger['status'] = target
        ledger['halt_reason'] = str(reason)[:500]
        ledger['halt_code'] = code
        ledger['updated_at'] = format_utc(current)
        entry: dict[str, Any] = {'event': target, 'time': format_utc(current), 'code': code, 'reason': ledger['halt_reason'], 'actor': actor}
        if details:
            entry['details'] = details
        ledger['history'].append(entry)
        return deepcopy(ledger)


def apply_revoke(workspace: str | Path, contract_id: str, *, reason: str, now: datetime) -> dict[str, Any]:
    with ledger_transaction(workspace, contract_id, write=True) as ledger:
        ledger['status'] = 'revoked'
        ledger['halt_reason'] = reason
        ledger['in_flight'] = None
        ledger['updated_at'] = format_utc(now)
        ledger['history'].append({'event': 'revoked', 'time': format_utc(now), 'reason': reason})
        return deepcopy(ledger)


def apply_resume(
    workspace: str | Path, contract_id: str, *, reason: str, reset_consecutive_failures: bool, now: datetime
) -> dict[str, Any]:
    with ledger_transaction(workspace, contract_id, write=True) as ledger:
        if ledger['status'] not in HALTED_LEDGER_STATUSES:
            raise ContractError('CONTRACT_NOT_HALTED', f'合同 {contract_id} 状态已变化：{ledger["status"]}。')
        ledger['status'] = 'active'
        ledger['halt_reason'] = None
        ledger['halt_code'] = None
        ledger['in_flight'] = None
        if reset_consecutive_failures:
            ledger['consecutive_failures'] = 0
        ledger['updated_at'] = format_utc(now)
        ledger['history'].append({'event': 'resumed', 'time': format_utc(now), 'reason': reason})
        return deepcopy(ledger)


def complete_ledger(
    workspace: str | Path, contract: dict[str, Any], *, summary: dict[str, Any] | None = None, now: datetime | None = None
) -> dict[str, Any]:
    current = utc_now(now)
    cid = contract['contract_id']
    pending_error: ContractError | None = None
    with ledger_transaction(workspace, cid, write=True) as ledger:
        check_ledger_binding(contract, ledger)
        if reconcile_expiry(contract, ledger, current):
            pending_error = ContractError('CONTRACT_EXPIRED', f'合同 {cid} 已过期。')
        elif ledger['status'] != 'active':
            raise ContractError('CONTRACT_HALTED', f'合同非 active 状态：{ledger["status"]}。')
        elif ledger.get('in_flight') is not None:
            raise ContractError('SUBMISSION_IN_FLIGHT', '存在未结清的提交中操作，不能标记完成。')
        else:
            ledger['status'] = 'completed'
            ledger['updated_at'] = format_utc(current)
            entry: dict[str, Any] = {'event': 'completed', 'time': format_utc(current)}
            if summary:
                entry['summary'] = summary
            ledger['history'].append(entry)
            result = deepcopy(ledger)
    if pending_error is not None:
        raise pending_error
    return result


def _auto_halt(ledger: dict[str, Any], code: str, reason: str, now: datetime) -> ContractError:
    ledger['status'] = 'halted'
    ledger['halt_code'] = code
    ledger['halt_reason'] = reason
    ledger['updated_at'] = format_utc(now)
    ledger['history'].append({'event': 'halted', 'time': format_utc(now), 'code': code, 'reason': reason, 'actor': 'ledger'})
    return ContractError(code, reason)


def reserve_submission_for(
    workspace: str | Path, contract: dict[str, Any], *, server: str | None, job_name: str, phase: str,
    resources: dict[str, Any], operation_id: str | None = None, tests: int | None = None, now: datetime | None = None,
) -> dict[str, Any]:
    current, cid, budget = utc_now(now), contract['contract_id'], contract['budget']
    op_id = operation_id or uuid4().hex
    pending_error: ContractError | None = None
    with ledger_transaction(workspace, cid, write=True) as ledger:
        check_ledger_binding(contract, ledger)
        if reconcile_expiry(contract, ledger, current):
            pending_error = ContractError('CONTRACT_EXPIRED', f'合同 {cid} 已过期。')
        elif ledger['status'] != 'active':
            code = 'CONTRACT_REVOKED' if ledger['status'] == 'revoked' else 'CONTRACT_HALTED'
            raise ContractError(code, f'合同当前不可提交（status={ledger["status"]}）。')
        elif server != contract['scope']['server'] or job_name not in ledger['allowed_jobs'] or phase not in contract['workflow']['allowed_phases']:
            raise ContractError('CONTRACT_SCOPE_VIOLATION', f'请求 ({server}, {job_name}, {phase}) 超出合同授权范围。')
        else:
            validated = validate_resources_in_envelope(resources, phase, contract['resource_envelope'], server=server, tests=tests)
            if ledger.get('in_flight') is not None:
                raise ContractError('SUBMISSION_IN_FLIGHT', '合同已有在途提交未对账，禁止并发双交。')
            if ledger['consecutive_failures'] >= budget['max_consecutive_failures']:
                pending_error = _auto_halt(ledger, 'CONTRACT_CONSECUTIVE_FAILURES_EXCEEDED', '连续失败次数达到合同上限，自动熔断。', current)
            elif ledger['submissions_used'] >= budget['max_submissions']:
                pending_error = _auto_halt(ledger, 'CONTRACT_BUDGET_EXHAUSTED', f'提交次数已达合同上限 {budget["max_submissions"]}，自动熔断。', current)
            else:
                ledger['submissions_used'] += 1
                ledger['in_flight'] = {'server': server, 'job_name': job_name, 'phase': phase,
                                       'operation_id': op_id, 'resources': validated, 'reserved_at': format_utc(current)}
                ledger['updated_at'] = format_utc(current)
                ledger['history'].append({'event': 'submission_reserved', 'time': format_utc(current),
                                          'job_name': job_name, 'phase': phase, 'operation_id': op_id})
                result = deepcopy(ledger)
    if pending_error is not None:
        raise pending_error
    return result


def confirm_submission_for(
    workspace: str | Path, contract: dict[str, Any], *, job_id: str, operation_id: str | None = None,
    recovered: bool = False, now: datetime | None = None,
) -> dict[str, Any]:
    clean_job_id = validate_job_id(job_id)
    current, cid = utc_now(now), contract['contract_id']
    pending_error: ContractError | None = None
    with ledger_transaction(workspace, cid, write=True) as ledger:
        check_ledger_binding(contract, ledger)
        if reconcile_expiry(contract, ledger, current):
            pending_error = ContractError('CONTRACT_EXPIRED', f'合同 {cid} 已过期。')
        elif ledger['status'] != 'active':
            raise _inactive_error(cid, ledger['status'])
        else:
            in_flight = ledger.get('in_flight')
            if in_flight is None:
                if operation_id is not None:
                    raise ContractError('SUBMISSION_IN_FLIGHT', '无在途提交记录，与传入 operation_id 不匹配。')
                if not recovered:
                    raise ContractError('SUBMISSION_NOT_IN_FLIGHT', '当前没有在途提交记录可确认。')
            elif operation_id is not None and in_flight.get('operation_id') != operation_id:
                raise ContractError('SUBMISSION_IN_FLIGHT', '确认的 operation_id 与在途提交记录不匹配。')
            ledger['in_flight'] = None
            ledger['updated_at'] = format_utc(current)
            ledger['history'].append({
                'event': 'submission_confirmed', 'time': format_utc(current), 'job_id': clean_job_id,
                'recovered': bool(recovered),
                **({'job_name': in_flight['job_name'], 'phase': in_flight['phase']} if isinstance(in_flight, dict) else {}),
            })
            result = deepcopy(ledger)
    if pending_error is not None:
        raise pending_error
    return result


def release_in_flight_for(
    workspace: str | Path, contract: dict[str, Any], *, operation_id: str | None = None,
    refund: bool = False, reason: str = '', now: datetime | None = None,
) -> dict[str, Any]:
    current, cid = utc_now(now), contract['contract_id']
    pending_error: ContractError | None = None
    with ledger_transaction(workspace, cid, write=True) as ledger:
        check_ledger_binding(contract, ledger)
        if reconcile_expiry(contract, ledger, current):
            pending_error = ContractError('CONTRACT_EXPIRED', f'合同 {cid} 已过期。')
        elif ledger['status'] != 'active':
            raise _inactive_error(cid, ledger['status'])
        else:
            in_flight = ledger.get('in_flight')
            if in_flight is None:
                if operation_id is not None:
                    raise ContractError('SUBMISSION_IN_FLIGHT', '当前没有匹配的在途提交记录可释放。')
                result = deepcopy(ledger)
            else:
                if operation_id is not None and in_flight.get('operation_id') != operation_id:
                    raise ContractError('SUBMISSION_IN_FLIGHT', '释放的 operation_id 与在途提交记录不匹配。')
                ledger['in_flight'] = None
                if refund and ledger['submissions_used'] > 0:
                    ledger['submissions_used'] -= 1
                ledger['updated_at'] = format_utc(current)
                ledger['history'].append({'event': 'in_flight_released', 'time': format_utc(current), 'refund': bool(refund), 'reason': reason})
                result = deepcopy(ledger)
    if pending_error is not None:
        raise pending_error
    return result


def record_phase_outcome_for(
    workspace: str | Path, contract: dict[str, Any], *, job_name: str, phase: str,
    passed: bool, failure_info: dict[str, Any] | None = None, now: datetime | None = None,
) -> dict[str, Any]:
    current, cid = utc_now(now), contract['contract_id']
    pending_error: ContractError | None = None
    with ledger_transaction(workspace, cid, write=True) as ledger:
        check_ledger_binding(contract, ledger)
        if reconcile_expiry(contract, ledger, current):
            pending_error = ContractError('CONTRACT_EXPIRED', f'合同 {cid} 已过期。')
        elif ledger['status'] != 'active':
            raise _inactive_error(cid, ledger['status'])
        else:
            if passed:
                ledger['consecutive_failures'] = 0
                ledger['history'].append({'event': 'phase_passed', 'time': format_utc(current), 'job_name': job_name, 'phase': phase})
            else:
                ledger['consecutive_failures'] += 1
                entry: dict[str, Any] = {'event': 'phase_failed', 'time': format_utc(current), 'job_name': job_name, 'phase': phase}
                if failure_info:
                    entry['failure_info'] = failure_info
                ledger['history'].append(entry)
                if ledger['consecutive_failures'] >= contract['budget']['max_consecutive_failures']:
                    _auto_halt(ledger, 'CONTRACT_CONSECUTIVE_FAILURES_EXCEEDED',
                               f'连续失败达到合同上限 {contract["budget"]["max_consecutive_failures"]}，自动熔断交人工审核。', current)
            ledger['updated_at'] = format_utc(current)
            result = deepcopy(ledger)
    if pending_error is not None:
        raise pending_error
    return result


def register_retry_for(
    workspace: str | Path, contract: dict[str, Any], *, kind: str, new_job_name: str | None = None,
    diagnostic: dict[str, Any] | None = None, record_failure: dict[str, Any] | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    field_map = {'resume': ('resume_retries_used', 'max_resume_retries'),
                 'repair': ('repair_retries_used', 'max_repair_retries'),
                 'scan': ('scan_retries_used', 'max_scan_retries')}
    if kind not in field_map:
        raise ContractError('CONTRACT_SCHEMA_INVALID', f'未知重试类型：{kind!r}。')
    if new_job_name is not None:
        validate_job_name(new_job_name)
    current, cid, budget = utc_now(now), contract['contract_id'], contract['budget']
    used_key, max_key = field_map[kind]
    pending_error: ContractError | None = None
    with ledger_transaction(workspace, cid, write=True) as ledger:
        check_ledger_binding(contract, ledger)
        if reconcile_expiry(contract, ledger, current):
            pending_error = ContractError('CONTRACT_EXPIRED', f'合同 {cid} 已过期。')
        elif ledger['status'] != 'active':
            raise ContractError('CONTRACT_HALTED', f'合同当前不可自愈重试（status={ledger["status"]}）。')
        elif ledger.get('in_flight') is not None:
            raise ContractError('SUBMISSION_IN_FLIGHT', '存在未对账在途提交，禁止登记重试。')
        else:
            max_fail = budget['max_consecutive_failures']
            halt_pair: tuple[str, str] | None = None
            if ledger['submissions_used'] >= budget['max_submissions']:
                halt_pair = ('CONTRACT_BUDGET_EXHAUSTED', '总提交额度已耗尽，自动熔断。')
            elif ledger['consecutive_failures'] >= max_fail:
                halt_pair = ('CONTRACT_CONSECUTIVE_FAILURES_EXCEEDED', '连续失败次数达到合同上限，自动熔断。')
            elif ledger[used_key] >= budget[max_key]:
                halt_pair = ('CONTRACT_BUDGET_EXHAUSTED', f'{kind} 重试次数已达合同上限 {budget[max_key]}，自动熔断交人工审核。')
            elif record_failure is not None and ledger['consecutive_failures'] + 1 >= max_fail:
                ledger['consecutive_failures'] += 1
                ledger['history'].append({'event': 'phase_failed', 'time': format_utc(current), **record_failure})
                halt_pair = ('CONTRACT_CONSECUTIVE_FAILURES_EXCEEDED', f'连续失败达到合同上限 {max_fail}，自动熔断交人工审核。')
            if halt_pair is not None:
                if diagnostic is not None:
                    ledger['diagnostics'].append({'kind': kind, 'time': format_utc(current), 'halted': True, **diagnostic})
                pending_error = _auto_halt(ledger, halt_pair[0], halt_pair[1], current)
            else:
                if record_failure is not None:
                    ledger['consecutive_failures'] += 1
                    ledger['history'].append({'event': 'phase_failed', 'time': format_utc(current), **record_failure})
                ledger[used_key] += 1
                if new_job_name and new_job_name not in ledger['allowed_jobs']:
                    ledger['allowed_jobs'].append(new_job_name)
                if diagnostic is not None:
                    ledger['diagnostics'].append({'kind': kind, 'time': format_utc(current), **diagnostic})
                ledger['updated_at'] = format_utc(current)
                ledger['history'].append({'event': f'retry_{kind}', 'time': format_utc(current), 'new_job_name': new_job_name})
                result = deepcopy(ledger)
    if pending_error is not None:
        raise pending_error
    return result


def summarize_contracts(
    workspace: str | Path, contract_id: str | None, *, loader: Callable[[str | Path, str], dict[str, Any]],
    verifier: Callable[..., tuple[dict[str, Any], dict[str, Any]]], now: datetime,
) -> dict[str, Any]:
    root, active_dir = contracts_subdir(workspace, 'active')
    items: list[dict[str, Any]] = []
    if active_dir.is_dir():
        for path in sorted(active_dir.glob('*.json')):
            if path.is_symlink() or not path.resolve().is_relative_to(root):
                continue
            cid = path.stem
            try:
                contract = loader(root, cid)
                ledger = read_ledger(root, cid)
                check_ledger_binding(contract, ledger)
                status = 'expired' if (ledger['status'] == 'active' and now > parse_utc(contract['expires_at'])) else ledger['status']
                items.append({'contract_id': cid, 'status': status, 'expires_at': contract['expires_at'],
                              'scope': contract['scope'], 'review_policy': contract['review_policy'],
                              'budget': contract['budget'], 'submissions_used': ledger['submissions_used'],
                              'halt_reason': ledger.get('halt_reason')})
            except ContractError as exc:
                items.append({'contract_id': cid, 'status': 'invalid', 'error_code': exc.error_code, 'error': str(exc)})
    selected_contract, selected_ledger = (None, None)
    if contract_id is not None:
        selected_contract, selected_ledger = verifier(root, contract_id, now=now, require_active=False)
    return {'active': any(item.get('status') == 'active' for item in items), 'contract_id': contract_id,
            'contract': selected_contract, 'ledger': selected_ledger, 'contracts': items}
