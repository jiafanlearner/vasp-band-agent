"""Zero-dependency headless CLI (`vasp-ctl`) wrapping ControlService."""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any

from vasp_agent.control.service import ROLES, ControlService


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description='Headless control CLI for vasp-band-agent (zero extra dependencies).')
    parser.add_argument('--workspace', default='.', help='Workspace directory path (default: current directory).')
    parser.add_argument('--role', choices=ROLES, default='maker', help='Control plane role (maker or checker).')
    parser.add_argument('--actor-id', default=None, help='Caller identity recorded in audit logs.')
    parser.add_argument(
        '--ssh-transport',
        choices=('pooled', 'subprocess'),
        default=None,
        help='Optional SSH transport override.',
    )

    subparsers = parser.add_subparsers(dest='command', required=True)

    tools_cmd = subparsers.add_parser('tools', help='List tools available for the selected role.')
    tools_cmd.add_argument('--json', action='store_true', dest='as_json', help='Emit machine-readable JSON.')

    call_cmd = subparsers.add_parser('call', help='Invoke one headless control tool.')
    call_cmd.add_argument('tool_name', help='Name of the control tool to invoke.')
    arg_group = call_cmd.add_mutually_exclusive_group()
    arg_group.add_argument('--args', default=None, help='JSON object of tool arguments.')
    arg_group.add_argument('--args-file', default=None, help='Path to a UTF-8 JSON file of tool arguments.')
    call_cmd.add_argument('--json', action='store_true', dest='as_json', help='Emit compact/formatted JSON output.')

    return parser


def _configure_transport(transport: str | None) -> None:
    if transport is None:
        return
    from vasp_agent.remote import ssh_pool

    os.environ[ssh_pool.ENV] = transport
    ssh_pool.configure(transport)


def _load_arguments(args_text: str | None, args_file: str | None) -> dict[str, Any]:
    if args_file is not None:
        raw = Path(args_file).read_text(encoding='utf-8')
    elif args_text is not None:
        raw = args_text
    else:
        return {}
    parsed = json.loads(raw)
    if not isinstance(parsed, dict):
        raise ValueError('工具参数 JSON 顶层必须是对象。')
    return parsed


def _emit_json(payload: dict[str, Any]) -> None:
    sys.stdout.write(json.dumps(payload, ensure_ascii=False, indent=2, default=str) + '\n')


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    _configure_transport(args.ssh_transport)
    try:
        service = ControlService(Path(args.workspace), role=args.role, actor_id=args.actor_id)
    except (OSError, ValueError) as exc:
        _emit_json({
            'ok': False,
            'error_code': 'INVALID_WORKSPACE',
            'error': str(exc),
            'type': type(exc).__name__,
            'recoverable': False,
        })
        return 2

    if args.command == 'tools':
        tools = service.tools()
        if args.as_json:
            _emit_json({'role': service.role, 'actor_id': service.actor_id, 'tools': tools})
        else:
            for item in tools:
                sys.stdout.write(f"{item['name']}: {item['description']}\n")
        return 0

    if args.command == 'call':
        try:
            tool_args = _load_arguments(args.args, args.args_file)
        except (OSError, ValueError) as exc:
            _emit_json({
                'ok': False,
                'error_code': 'INVALID_ARGUMENTS',
                'error': str(exc),
                'type': type(exc).__name__,
                'recoverable': False,
            })
            return 2
        result = service.call(args.tool_name, tool_args)
        _emit_json(result)
        return 0 if result.get('ok') else 1

    return 2


if __name__ == '__main__':
    raise SystemExit(main())
