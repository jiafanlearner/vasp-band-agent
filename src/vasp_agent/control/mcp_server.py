"""Thin stdio MCP server adapter for ControlService with lazy mcp SDK import."""
from __future__ import annotations

import argparse
import asyncio
import inspect
import json
import os
import sys
from pathlib import Path
from typing import Any

from vasp_agent.control.service import ROLES, ControlService

MCP_INSTALL_HINT = (
    '未安装可选依赖 mcp>=1.6；请运行 python -m pip install -e ".[mcp]"，'
    '或改用零依赖 CLI：vasp-ctl call --json ...'
)


def tool_specs(service: ControlService) -> list[dict[str, Any]]:
    """Return MCP tool specifications visible to the given service role."""
    return service.tools()


def format_call_result(payload: dict[str, Any]) -> dict[str, Any]:
    """Serialize ControlService.call() output into compact UTF-8 text + error flag."""
    text = json.dumps(payload, ensure_ascii=False, separators=(',', ':'), default=str)
    return {'text': text, 'is_error': not bool(payload.get('ok'))}


def load_mcp_sdk() -> tuple[Any, Any, Any]:
    """Lazily import mcp low-level server primitives so base installs stay importable."""
    try:
        from mcp import types as mcp_types
        from mcp.server.lowlevel import Server
        from mcp.server.stdio import stdio_server
    except ImportError as exc:
        raise RuntimeError(MCP_INSTALL_HINT) from exc
    return Server, stdio_server, mcp_types


def _build_mcp_tools(service: ControlService, mcp_types: Any) -> list[Any]:
    tool_cls = mcp_types.Tool
    fields = getattr(tool_cls, 'model_fields', None) or {}
    schema_key = 'input_schema' if ('input_schema' in fields and 'inputSchema' not in fields) else 'inputSchema'
    built = []
    for spec in tool_specs(service):
        kwargs = {
            'name': spec['name'],
            'description': spec['description'],
            schema_key: spec['inputSchema'],
        }
        built.append(tool_cls(**kwargs))
    return built


def _build_call_result(formatted: dict[str, Any], mcp_types: Any, *, snake_case: bool) -> Any:
    content = [mcp_types.TextContent(type='text', text=formatted['text'])]
    if snake_case:
        return mcp_types.CallToolResult(content=content, is_error=formatted['is_error'])
    return mcp_types.CallToolResult(content=content, isError=formatted['is_error'])


def create_mcp_server(service: ControlService, *, server_name: str = 'vasp-band-agent') -> Any:
    """Create a role-gated MCP low-level Server (supports both mcp v1.x and v2.x)."""
    server_cls, _stdio_server, mcp_types = load_mcp_sdk()
    mcp_tools = _build_mcp_tools(service, mcp_types)
    init_params = inspect.signature(server_cls.__init__).parameters

    if 'on_list_tools' in init_params and 'on_call_tool' in init_params:
        async def _on_list_tools(_ctx: Any, _params: Any) -> Any:
            return mcp_types.ListToolsResult(tools=mcp_tools)

        async def _on_call_tool(_ctx: Any, params: Any) -> Any:
            name = getattr(params, 'name', '')
            arguments = getattr(params, 'arguments', None) or {}
            payload = await asyncio.to_thread(service.call, name, arguments)
            return _build_call_result(format_call_result(payload), mcp_types, snake_case=True)

        return server_cls(server_name, on_list_tools=_on_list_tools, on_call_tool=_on_call_tool)

    server = server_cls(server_name)

    @server.list_tools()
    async def _list_tools() -> list[Any]:
        return mcp_tools

    if (
        hasattr(server, 'request_handlers')
        and hasattr(mcp_types, 'CallToolRequest')
        and hasattr(mcp_types, 'ServerResult')
    ):
        async def _handle_call_tool(req: Any) -> Any:
            params = getattr(req, 'params', None)
            name = getattr(params, 'name', '')
            arguments = getattr(params, 'arguments', None) or {}
            payload = await asyncio.to_thread(service.call, name, arguments)
            result = _build_call_result(format_call_result(payload), mcp_types, snake_case=False)
            return mcp_types.ServerResult(result)

        server.request_handlers[mcp_types.CallToolRequest] = _handle_call_tool
    else:
        @server.call_tool()
        async def _call_tool(name: str, arguments: dict[str, Any] | None = None) -> list[Any]:
            payload = await asyncio.to_thread(service.call, name, arguments or {})
            formatted = format_call_result(payload)
            return [mcp_types.TextContent(type='text', text=formatted['text'])]

    return server


async def serve_stdio(service: ControlService, *, server_name: str = 'vasp-band-agent') -> None:
    """Run the role-gated MCP server over stdin/stdout."""
    _server_cls, stdio_server, _mcp_types = load_mcp_sdk()
    server = create_mcp_server(service, server_name=server_name)
    async with stdio_server() as (read_stream, write_stream):
        await server.run(
            read_stream,
            write_stream,
            server.create_initialization_options(),
        )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description='Headless MCP stdio server for vasp-band-agent.')
    parser.add_argument('--workspace', default='.', help='Workspace directory path (default: current directory).')
    parser.add_argument('--role', choices=ROLES, default='maker', help='Control plane role (maker or checker).')
    parser.add_argument('--actor-id', default=None, help='Stable caller identity recorded in audit logs.')
    parser.add_argument('--host', default=None, help='Host identifier embedded in structured stage reviewer tags.')
    parser.add_argument('--model', default=None, help='Model identifier embedded in structured stage reviewer tags.')
    parser.add_argument(
        '--ssh-transport',
        choices=('pooled', 'subprocess'),
        default=None,
        help='Optional SSH transport override (pooled keeps persistent connections across calls).',
    )
    parser.add_argument(
        '--list-tools-json',
        action='store_true',
        help='Print role-filtered MCP tool definitions as JSON and exit (requires no mcp package).',
    )
    return parser


def _configure_transport(transport: str | None) -> None:
    if transport is None:
        return
    from vasp_agent.remote import ssh_pool

    os.environ[ssh_pool.ENV] = transport
    ssh_pool.configure(transport)


def _configure_utf8_stdio() -> None:
    """Ensure stdout/stderr emit UTF-8 even under Windows GBK/CP936 pipes."""
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, 'reconfigure', None)
        if callable(reconfigure):
            try:
                reconfigure(encoding='utf-8', errors='replace')
            except (OSError, ValueError):
                pass


def main(argv: list[str] | None = None) -> int:
    _configure_utf8_stdio()
    args = build_parser().parse_args(argv)
    _configure_transport(args.ssh_transport)
    service = ControlService(
        Path(args.workspace), role=args.role, actor_id=args.actor_id, host=args.host, model=args.model,
    )
    if args.list_tools_json:
        payload = {'role': service.role, 'actor_id': service.actor_id, 'tools': tool_specs(service)}
        sys.stdout.write(json.dumps(payload, ensure_ascii=False, indent=2) + '\n')
        return 0
    try:
        asyncio.run(serve_stdio(service))
    except RuntimeError as exc:
        sys.stderr.write(str(exc) + '\n')
        return 2
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
