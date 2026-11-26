"""CLI subcommand handlers for `vasp-ctl contract approve|show|revoke|resume`."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys
from typing import Any

from vasp_agent.permissions import contract_rules, contract_ui, task_contract
from vasp_agent.permissions.contract_rules import REVIEW_MODES, ContractError


def register_contract_subparser(subparsers: argparse._SubParsersAction) -> None:
    """Attach `vasp-ctl contract {approve,show,revoke,resume}` subcommands."""
    contract_cmd = subparsers.add_parser(
        'contract',
        help='Manage task autonomy contracts out-of-band on a real interactive terminal.',
    )
    sub = contract_cmd.add_subparsers(dest='contract_action', required=True)

    approve = sub.add_parser('approve', help='Inspect a contract draft and approve it via real TTY.')
    approve.add_argument('draft_id', help='Contract draft ID (drf-...).')
    approve.add_argument('--ssh-key', default=None, help='Optional OpenSSH private key path for signing.')
    approve.add_argument('--ssh-principal', default=None, help='Optional OpenSSH principal identity.')
    approve.add_argument('--review-mode', choices=REVIEW_MODES, default=None, help='Override initial review mode.')
    approve.add_argument('--json', action='store_true', dest='as_json', help='Emit machine-readable JSON on stdout.')

    show = sub.add_parser('show', help='Show all contracts/drafts or inspect one contract/draft in detail.')
    show.add_argument('target_id', nargs='?', default=None, help='Optional contract ID (ctr-...) or draft ID (drf-...).')
    show.add_argument('--json', action='store_true', dest='as_json', help='Emit machine-readable JSON on stdout.')

    revoke = sub.add_parser('revoke', help='Terminally revoke an active or halted contract via real TTY.')
    revoke.add_argument('contract_id', help='Contract ID (ctr-...).')
    revoke.add_argument('--reason', default='operator_revoked', help='Audit reason recorded in ledger.')
    revoke.add_argument('--json', action='store_true', dest='as_json', help='Emit machine-readable JSON on stdout.')

    resume = sub.add_parser('resume', help='Resume a halted contract via real TTY after manual review.')
    resume.add_argument('contract_id', help='Contract ID (ctr-...).')
    resume.add_argument('--reason', default='operator_resumed', help='Audit reason recorded in ledger.')
    resume.add_argument(
        '--keep-failures',
        action='store_true',
        help='Keep existing consecutive_failures count instead of resetting it to 0.',
    )
    resume.add_argument('--json', action='store_true', dest='as_json', help='Emit machine-readable JSON on stdout.')


def _emit_json(payload: dict[str, Any]) -> None:
    sys.stdout.write(json.dumps(payload, ensure_ascii=False, indent=2, default=str) + '\n')


def _emit_error(exc: Exception, *, as_json: bool, default_code: str = 'CONTRACT_ERROR') -> int:
    code = getattr(exc, 'error_code', default_code)
    if as_json:
        _emit_json({
            'ok': False,
            'error_code': code,
            'error': str(exc),
            'type': type(exc).__name__,
            'recoverable': False,
        })
    else:
        sys.stderr.write(f'[{code}] {exc}\n')
    return 1


def _prompt_output(as_json: bool):
    if as_json:
        return lambda text: sys.stderr.write(str(text) + '\n')
    return print


def _prompt_reader(as_json: bool):
    if not as_json:
        return input

    def _read(prompt: str = '') -> str:
        if prompt:
            sys.stderr.write(prompt)
            sys.stderr.flush()
        return input('')

    return _read


def _try_load_plan(workspace: Path, doc: dict[str, Any]) -> dict[str, Any] | None:
    try:
        _, plan, _ = contract_ui.load_linked_plan(workspace, doc)
        return plan
    except ContractError:
        return None


def _handle_approve(workspace: Path, args: argparse.Namespace) -> int:
    result = contract_ui.approve_contract_interactive(
        workspace,
        args.draft_id,
        reader=_prompt_reader(args.as_json),
        output=_prompt_output(args.as_json),
        ssh_key=args.ssh_key,
        ssh_principal=args.ssh_principal,
        review_mode=args.review_mode,
    )
    if args.as_json:
        _emit_json({'ok': True, 'result': result})
    else:
        cid = result['contract']['contract_id']
        mode = result['contract']['review_policy']['mode']
        sys.stdout.write(f'已签发不可变合同：{cid}（审阅模式：{mode}，关联方案状态：{result["plan_status"]}）。\n')
    return 0


def _handle_show(workspace: Path, args: argparse.Namespace) -> int:
    target = args.target_id
    if target is None:
        status = task_contract.contract_status(workspace)
        drafts = task_contract.list_drafts(workspace)
        if args.as_json:
            _emit_json({'ok': True, 'result': {**status, 'drafts': drafts}})
        else:
            sys.stdout.write(contract_ui.render_contracts_overview(status, drafts) + '\n')
        return 0

    contract_rules.validate_identifier(target, 'target_id')
    try:
        draft = task_contract.load_draft(workspace, target)
    except ContractError as exc:
        if exc.error_code != 'CONTRACT_NOT_FOUND':
            raise
        draft = None
    if draft is not None:
        plan = _try_load_plan(workspace, draft)
        if args.as_json:
            _emit_json({'ok': True, 'result': {'kind': 'draft', 'draft': draft, 'plan': plan}})
        else:
            sys.stdout.write(contract_ui.render_contract_summary(draft, plan=plan) + '\n')
        return 0

    status = task_contract.contract_status(workspace, target)
    contract, ledger = status['contract'], status['ledger']
    if args.as_json:
        _emit_json({'ok': True, 'result': {'kind': 'contract', 'contract': contract, 'ledger': ledger}})
    else:
        sys.stdout.write(contract_ui.render_contract_summary(contract, ledger=ledger) + '\n')
    return 0


def _handle_revoke(workspace: Path, args: argparse.Namespace) -> int:
    summary = ''
    try:
        status = task_contract.contract_status(workspace, args.contract_id)
        summary = contract_ui.render_contract_summary(status['contract'], ledger=status['ledger'])
    except ContractError:
        ledger = task_contract.read_ledger(workspace, args.contract_id)
        summary = f"合同 {args.contract_id} 当前账本状态：{ledger['status']}（不可逆撤销）"
    provider = task_contract.LocalTTYApprovalProvider(
        reader=_prompt_reader(args.as_json),
        output=_prompt_output(args.as_json),
    )
    ledger = task_contract.revoke_contract(
        workspace,
        args.contract_id,
        reason=args.reason,
        provider=provider,
        summary_text=summary,
    )
    if args.as_json:
        _emit_json({'ok': True, 'result': {'contract_id': args.contract_id, 'ledger': ledger}})
    else:
        sys.stdout.write(f'已永久撤销合同 {args.contract_id}（原因：{args.reason}）。\n')
    return 0


def _handle_resume(workspace: Path, args: argparse.Namespace) -> int:
    status = task_contract.contract_status(workspace, args.contract_id)
    summary = contract_ui.render_contract_summary(status['contract'], ledger=status['ledger'])
    provider = task_contract.LocalTTYApprovalProvider(
        reader=_prompt_reader(args.as_json),
        output=_prompt_output(args.as_json),
    )
    ledger = task_contract.resume_contract(
        workspace,
        args.contract_id,
        reason=args.reason,
        provider=provider,
        reset_consecutive_failures=not bool(args.keep_failures),
        summary_text=summary,
    )
    if args.as_json:
        _emit_json({'ok': True, 'result': {'contract_id': args.contract_id, 'ledger': ledger}})
    else:
        sys.stdout.write(f'已恢复合同 {args.contract_id} 为 active 状态（原因：{args.reason}）。\n')
    return 0


def handle_contract(args: argparse.Namespace) -> int:
    """Validate workspace and dispatch `vasp-ctl contract` subcommands."""
    as_json = bool(getattr(args, 'as_json', False))
    try:
        workspace = Path(args.workspace).resolve(strict=True)
        if not workspace.is_dir():
            raise NotADirectoryError(f'工作区路径不是目录：{workspace.name}')
    except (OSError, ValueError) as exc:
        _emit_error(exc, as_json=as_json, default_code='INVALID_WORKSPACE')
        return 2
    try:
        if args.contract_action == 'approve':
            return _handle_approve(workspace, args)
        if args.contract_action == 'show':
            return _handle_show(workspace, args)
        if args.contract_action == 'revoke':
            return _handle_revoke(workspace, args)
        if args.contract_action == 'resume':
            return _handle_resume(workspace, args)
        return 2
    except ContractError as exc:
        return _emit_error(exc, as_json=as_json)
    except (OSError, ValueError) as exc:
        return _emit_error(exc, as_json=as_json, default_code='CONTRACT_ERROR')
