"""Offline tests for P0 MCP stdio server adapter, role matrix, lazy imports, and protocol round-trip."""
from __future__ import annotations

import asyncio
import importlib
import json
import sys
import types

import pytest

from vasp_agent.control import ControlService
from vasp_agent.control import mcp_server

CHECKER_TOOLS = {
    'vasp_workspace_info',
    'vasp_inspect_structure',
    'vasp_preview_defaults',
    'vasp_task_status',
    'vasp_read_evidence',
    'vasp_diagnose_submission',
    'vasp_contract_status',
    'vasp_get_stage_review',
    'vasp_circuit_break',
}
MAKER_TOOLS = CHECKER_TOOLS | {'vasp_fetch_results', 'vasp_sandbox_analysis', 'vasp_draft_contract'}


def _install_fake_mcp_v1(monkeypatch):
    """Install a minimal faithful v1.x-shaped mcp package into sys.modules."""
    mcp_pkg = types.ModuleType('mcp')
    server_pkg = types.ModuleType('mcp.server')
    lowlevel_mod = types.ModuleType('mcp.server.lowlevel')
    stdio_mod = types.ModuleType('mcp.server.stdio')
    types_mod = types.ModuleType('mcp.types')

    class Tool:
        model_fields = {'name': None, 'description': None, 'inputSchema': None}

        def __init__(self, *, name, description, inputSchema):
            self.name = name
            self.description = description
            self.inputSchema = inputSchema

    class TextContent:
        def __init__(self, *, type, text):
            self.type = type
            self.text = text

    class ListToolsRequest:
        pass

    class CallToolRequest:
        def __init__(self, name, arguments=None):
            self.params = types.SimpleNamespace(name=name, arguments=arguments)

    class ListToolsResult:
        def __init__(self, *, tools):
            self.tools = tools

    class CallToolResult:
        def __init__(self, *, content, isError=False):
            self.content = content
            self.isError = isError

    class ServerResult:
        def __init__(self, root):
            self.root = root

    class Server:
        def __init__(self, name):
            self.name = name
            self.request_handlers = {}

        def list_tools(self):
            def decorator(fn):
                async def wrapper(_req):
                    return ServerResult(ListToolsResult(tools=await fn()))
                self.request_handlers[ListToolsRequest] = wrapper
                return fn
            return decorator

        def create_initialization_options(self):
            return {'server': self.name}

    types_mod.Tool = Tool
    types_mod.TextContent = TextContent
    types_mod.ListToolsRequest = ListToolsRequest
    types_mod.CallToolRequest = CallToolRequest
    types_mod.ListToolsResult = ListToolsResult
    types_mod.CallToolResult = CallToolResult
    types_mod.ServerResult = ServerResult
    lowlevel_mod.Server = Server
    stdio_mod.stdio_server = None

    for mod_name, mod in (
        ('mcp', mcp_pkg),
        ('mcp.server', server_pkg),
        ('mcp.server.lowlevel', lowlevel_mod),
        ('mcp.server.stdio', stdio_mod),
        ('mcp.types', types_mod),
    ):
        monkeypatch.setitem(sys.modules, mod_name, mod)
    return types_mod


def _install_fake_mcp_v2(monkeypatch):
    """Install a minimal v2-shaped mcp package (constructor handlers + snake_case fields)."""
    mcp_pkg = types.ModuleType('mcp')
    server_pkg = types.ModuleType('mcp.server')
    lowlevel_mod = types.ModuleType('mcp.server.lowlevel')
    stdio_mod = types.ModuleType('mcp.server.stdio')
    types_mod = types.ModuleType('mcp.types')

    class Tool:
        model_fields = {'name': None, 'description': None, 'input_schema': None}

        def __init__(self, *, name, description, input_schema):
            self.name = name
            self.description = description
            self.input_schema = input_schema

    class TextContent:
        def __init__(self, *, type, text):
            self.type = type
            self.text = text

    class ListToolsResult:
        def __init__(self, *, tools):
            self.tools = tools

    class CallToolResult:
        def __init__(self, *, content, is_error=False):
            self.content = content
            self.is_error = is_error

    class Server:
        def __init__(self, name, *, on_list_tools=None, on_call_tool=None):
            self.name = name
            self.on_list_tools = on_list_tools
            self.on_call_tool = on_call_tool

    types_mod.Tool = Tool
    types_mod.TextContent = TextContent
    types_mod.ListToolsResult = ListToolsResult
    types_mod.CallToolResult = CallToolResult
    lowlevel_mod.Server = Server
    stdio_mod.stdio_server = None

    for mod_name, mod in (
        ('mcp', mcp_pkg),
        ('mcp.server', server_pkg),
        ('mcp.server.lowlevel', lowlevel_mod),
        ('mcp.server.stdio', stdio_mod),
        ('mcp.types', types_mod),
    ):
        monkeypatch.setitem(sys.modules, mod_name, mod)
    return types_mod


def test_mcp_server_imports_lazily_without_mcp_package(monkeypatch, tmp_path, capsys):
    for mod_name in list(sys.modules):
        if mod_name == 'mcp' or mod_name.startswith('mcp.'):
            monkeypatch.delitem(sys.modules, mod_name, raising=False)
    for blocked in ('mcp', 'mcp.server', 'mcp.server.lowlevel', 'mcp.server.stdio', 'mcp.types'):
        monkeypatch.setitem(sys.modules, blocked, None)

    reloaded = importlib.reload(mcp_server)
    assert reloaded is mcp_server

    service = ControlService(tmp_path, role='checker')
    with pytest.raises(RuntimeError, match=r'pip install -e "\.\[mcp\]"'):
        mcp_server.create_mcp_server(service)

    rc = mcp_server.main(['--workspace', str(tmp_path), '--role', 'checker'])
    assert rc == 2
    assert 'vasp-ctl' in capsys.readouterr().err


def test_tool_registration_matrix_and_list_tools_json_cli(tmp_path, capsys):
    maker_specs = mcp_server.tool_specs(ControlService(tmp_path, role='maker'))
    checker_specs = mcp_server.tool_specs(ControlService(tmp_path, role='checker'))

    assert {t['name'] for t in maker_specs} == MAKER_TOOLS
    assert {t['name'] for t in checker_specs} == CHECKER_TOOLS
    assert all({'name', 'description', 'inputSchema'} <= set(t) for t in maker_specs)

    rc = mcp_server.main(['--workspace', str(tmp_path), '--role', 'checker', '--list-tools-json'])
    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload['role'] == 'checker'
    assert {t['name'] for t in payload['tools']} == CHECKER_TOOLS


def test_mcp_v1_adapter_registers_role_matrix_and_dispatches(tmp_path, monkeypatch):
    types_mod = _install_fake_mcp_v1(monkeypatch)
    checker = ControlService(tmp_path, role='checker', actor_id='rev-1')
    server = mcp_server.create_mcp_server(checker)

    list_handler = server.request_handlers[types_mod.ListToolsRequest]
    call_handler = server.request_handlers[types_mod.CallToolRequest]

    listed = asyncio.run(list_handler(types_mod.ListToolsRequest()))
    assert {t.name for t in listed.root.tools} == CHECKER_TOOLS

    ok_res = asyncio.run(call_handler(types_mod.CallToolRequest('vasp_workspace_info', {})))
    assert ok_res.root.isError is False
    ok_body = json.loads(ok_res.root.content[0].text)
    assert ok_body['ok'] is True
    assert ok_body['result']['role'] == 'checker'

    denied_res = asyncio.run(
        call_handler(types_mod.CallToolRequest('vasp_fetch_results', {'server': 'hpc', 'job_name': 'si'}))
    )
    assert denied_res.root.isError is True
    denied_body = json.loads(denied_res.root.content[0].text)
    assert denied_body['ok'] is False
    assert denied_body['error_code'] == 'TOOL_NOT_ALLOWED_FOR_ROLE'


def test_mcp_v2_adapter_registers_role_matrix_and_dispatches(tmp_path, monkeypatch):
    _install_fake_mcp_v2(monkeypatch)
    maker = ControlService(tmp_path, role='maker', actor_id='mk-1')
    server = mcp_server.create_mcp_server(maker)

    listed = asyncio.run(server.on_list_tools(None, None))
    assert {t.name for t in listed.tools} == MAKER_TOOLS

    ok_res = asyncio.run(
        server.on_call_tool(None, types.SimpleNamespace(name='vasp_contract_status', arguments={}))
    )
    assert ok_res.is_error is False
    body = json.loads(ok_res.content[0].text)
    assert body['ok'] is True
    assert body['result']['active'] is False


def test_real_mcp_sdk_memory_session_roundtrip(tmp_path):
    """Runs against the real mcp SDK whenever installed (always active in GitHub Actions CI)."""
    pytest.importorskip('mcp')
    anyio = pytest.importorskip('anyio')
    from mcp.shared import memory as mcp_memory

    async def exercise(role: str):
        service = ControlService(tmp_path, role=role, actor_id=f'actor-{role}')
        server = mcp_server.create_mcp_server(service)
        if hasattr(mcp_memory, 'create_connected_server_and_client_session'):
            async with mcp_memory.create_connected_server_and_client_session(server) as session:
                listed = await session.list_tools()
                names = {tool.name for tool in listed.tools}
                info = await session.call_tool('vasp_workspace_info', {})
                denied = await session.call_tool('vasp_fetch_results', {'server': 'hpc', 'job_name': 'si'})
            return names, info, denied

        from mcp.client.session import ClientSession

        async with mcp_memory.create_client_server_memory_streams() as (client_streams, server_streams):
            client_read, client_write = client_streams
            server_read, server_write = server_streams
            init_opts = server.create_initialization_options()

            async with anyio.create_task_group() as tg:
                tg.start_soon(server.run, server_read, server_write, init_opts)
                async with ClientSession(client_read, client_write) as session:
                    await session.initialize()
                    listed = await session.list_tools()
                    names = {tool.name for tool in listed.tools}
                    info = await session.call_tool('vasp_workspace_info', {})
                    denied = await session.call_tool('vasp_fetch_results', {'server': 'hpc', 'job_name': 'si'})
                tg.cancel_scope.cancel()
        return names, info, denied

    checker_names, info_res, denied_res = anyio.run(exercise, 'checker')
    assert checker_names == CHECKER_TOOLS
    assert info_res.isError is False
    assert json.loads(info_res.content[0].text)['result']['role'] == 'checker'
    assert denied_res.isError is True
    assert json.loads(denied_res.content[0].text)['error_code'] == 'TOOL_NOT_ALLOWED_FOR_ROLE'

    maker_names, _, _ = anyio.run(exercise, 'maker')
    assert maker_names == MAKER_TOOLS
