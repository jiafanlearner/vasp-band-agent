"""Offline tests for P0 headless ControlService, model_views compaction, and vasp-ctl CLI."""
from __future__ import annotations

import ast
import contextlib
import io
import json
import os
import subprocess
import sys
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import pytest
from jsonschema import Draft202012Validator
from pymatgen.io.vasp.inputs import Potcar

from vasp_agent.control import MAX_RESPONSE_BYTES, ROLES, ControlService
from vasp_agent.control import cli as control_cli
from vasp_agent.workflow import full_workflow, stage_gate

ROOT = Path(__file__).resolve().parents[1]
EXAMPLE_POSCAR = ROOT / 'examples/Si.POSCAR'

READ_ONLY_TOOLS = {
    'vasp_workspace_info',
    'vasp_inspect_structure',
    'vasp_preview_defaults',
    'vasp_task_status',
    'vasp_read_evidence',
    'vasp_diagnose_submission',
    'vasp_contract_status',
    'vasp_get_stage_review',
    'vasp_watch_jobs',
}
CHECKER_TOOLS = READ_ONLY_TOOLS | {'vasp_record_stage_review', 'vasp_circuit_break'}
MAKER_ONLY_TOOLS = {
    'vasp_fetch_results',
    'vasp_sandbox_analysis',
    'vasp_draft_contract',
    'vasp_prepare_task',
    'vasp_submit_stage',
    'vasp_prepare_resume',
    'vasp_prepare_scan_retry',
    'vasp_prepare_repair_fork',
}
FORBIDDEN_SUBMIT_AND_FUTURE_TOOLS = {
    'submit_stage',
    'remote_submit',
    'submit_job',
    'request_submission',
    'request_job_cancellation',
    'run_local_analysis_script',
}

FAKE_POTCAR = 'PAW_PBE Si 05Jan2001\n'


@pytest.fixture(autouse=True)
def fake_pbe_potcar(monkeypatch):
    fake = [SimpleNamespace(element='Si', functional='PBE', TITEL='PAW_PBE Si 05Jan2001', ZVAL=4.0, enmax=245.345)]
    monkeypatch.setattr(Potcar, 'from_str', classmethod(lambda _cls, _text: fake))
    monkeypatch.setattr(Potcar, 'from_file', classmethod(lambda _cls, _path: fake))


def _setup_workspace(tmp_path: Path, *, with_task: bool = False, confirm_physics: bool = False) -> Path:
    structures = tmp_path / 'structures'
    structures.mkdir(parents=True, exist_ok=True)
    (structures / 'Si.POSCAR').write_bytes(EXAMPLE_POSCAR.read_bytes())
    potcar = tmp_path / 'potentials/PBE/Si/POTCAR'
    potcar.parent.mkdir(parents=True, exist_ok=True)
    potcar.write_text(FAKE_POTCAR, encoding='utf-8', newline='\n')
    if with_task:
        folder = tmp_path / 'runs/si-demo'
        args = SimpleNamespace(
            structure=str(structures / 'Si.POSCAR'),
            workdir=str(folder),
            potcar=None,
            potcar_library=str(tmp_path / 'potentials/PBE'),
            encut=400,
            mesh=[4, 4, 4],
            nbands=16,
            relax_mode='cell',
            ediff=1e-7,
            points=20,
            symprec=0.01,
            force_tol=0.01,
            stress_tol=1.0,
            energy_tol=0.001,
            nsw=100,
        )
        with contextlib.redirect_stdout(io.StringIO()):
            full_workflow.prepare_full(args)
        if confirm_physics:
            stage_gate.confirm_physics(folder, 'Si 3D nonmagnetic PBE test', 'operator_slash_command')
    return tmp_path


def test_role_validation_and_tool_matrix(tmp_path):
    assert ROLES == ('maker', 'checker')
    with pytest.raises(ValueError, match='role'):
        ControlService(tmp_path, role='admin')

    maker = ControlService(tmp_path, role='maker', actor_id='maker-1')
    checker = ControlService(tmp_path, role='checker', actor_id='checker-1')

    assert set(maker.tool_names()) == CHECKER_TOOLS | MAKER_ONLY_TOOLS
    assert set(checker.tool_names()) == CHECKER_TOOLS
    assert not (set(maker.tool_names()) & FORBIDDEN_SUBMIT_AND_FUTURE_TOOLS)
    assert not (set(checker.tool_names()) & FORBIDDEN_SUBMIT_AND_FUTURE_TOOLS)

    for service in (maker, checker):
        assert service.box.allow_submit is False
        assert service.box.max_submissions == 0
        assert service.box.plan_reader is None
        assert service.box.require_scan_plans is True
        for spec in service.tools():
            Draft202012Validator.check_schema(spec['inputSchema'])
            assert spec['inputSchema']['type'] == 'object'
            assert spec['inputSchema'].get('additionalProperties') is False


def test_zero_submission_and_checker_write_denial(tmp_path):
    maker = ControlService(tmp_path, role='maker')
    checker = ControlService(tmp_path, role='checker')

    for name in ('submit_stage', 'remote_submit', 'request_submission'):
        res = maker.call(name, {'server': 'hpc', 'job_name': 'si', 'phase': 'convergence'})
        assert res['ok'] is False
        assert res['error_code'] == 'UNKNOWN_TOOL'

    denied_fetch = checker.call('vasp_fetch_results', {'server': 'hpc', 'job_name': 'si'})
    assert denied_fetch['ok'] is False
    assert denied_fetch['error_code'] == 'TOOL_NOT_ALLOWED_FOR_ROLE'

    denied_sandbox = checker.call('vasp_sandbox_analysis', {'action': 'runtime'})
    assert denied_sandbox['ok'] is False
    assert denied_sandbox['error_code'] == 'TOOL_NOT_ALLOWED_FOR_ROLE'

    denied_draft = checker.call('vasp_draft_contract', {})
    assert denied_draft['ok'] is False
    assert denied_draft['error_code'] == 'TOOL_NOT_ALLOWED_FOR_ROLE'

    denied_prepare = checker.call('vasp_prepare_task', {'contract_id': 'ctr-20261127-11111111'})
    assert denied_prepare['ok'] is False
    assert denied_prepare['error_code'] == 'TOOL_NOT_ALLOWED_FOR_ROLE'

    denied_submit = checker.call(
        'vasp_submit_stage',
        {'contract_id': 'ctr-20261127-11111111', 'job_name': 'si', 'phase': 'convergence'},
    )
    assert denied_submit['ok'] is False
    assert denied_submit['error_code'] == 'TOOL_NOT_ALLOWED_FOR_ROLE'

    denied_resume = checker.call(
        'vasp_prepare_resume',
        {'contract_id': 'ctr-20261127-11111111', 'job_name': 'si', 'phase': 'convergence'},
    )
    assert denied_resume['ok'] is False
    assert denied_resume['error_code'] == 'TOOL_NOT_ALLOWED_FOR_ROLE'

    denied_scan_retry = checker.call(
        'vasp_prepare_scan_retry',
        {'contract_id': 'ctr-20261127-11111111', 'source_job': 'si', 'mode': 'mesh'},
    )
    assert denied_scan_retry['ok'] is False
    assert denied_scan_retry['error_code'] == 'TOOL_NOT_ALLOWED_FOR_ROLE'

    denied_repair_fork = checker.call(
        'vasp_prepare_repair_fork',
        {
            'contract_id': 'ctr-20261127-11111111',
            'source_job': 'si',
            'phase': 'convergence',
            'strategy': 'fresh_restart',
            'failure_category': 'oom',
            'evidence_sha256': 'a' * 64,
            'repair_hypothesis': 'OOM on single node',
            'applied_change': 'increase nodes to 2',
        },
    )
    assert denied_repair_fork['ok'] is False
    assert denied_repair_fork['error_code'] == 'TOOL_NOT_ALLOWED_FOR_ROLE'


def test_workspace_info_and_contract_status_are_read_only(tmp_path):
    ws = _setup_workspace(tmp_path, with_task=True)
    service = ControlService(ws, role='checker', actor_id='chk-01')

    info = service.call('vasp_workspace_info', {})
    assert info['ok'] is True
    result = info['result']
    assert result['role'] == 'checker'
    assert result['actor_id'] == 'chk-01'
    assert result['submission_enabled'] is False
    assert result['autonomy_phase'] == 'P3_bounded_autonomy'
    assert 'si-demo' in result['jobs']
    assert result['potcar_library_configured'] is True
    assert result['contract']['active'] is False
    assert str(ws) not in json.dumps(result, ensure_ascii=False)

    # P0 contract status never creates .agent/state/contracts/.
    assert not (ws / '.agent/state/contracts').exists()


def test_inspect_structure_and_checker_blocks_external_import(tmp_path):
    ws = _setup_workspace(tmp_path)
    outside = tmp_path.parent / f'outside-{tmp_path.name}.POSCAR'
    outside.write_bytes(EXAMPLE_POSCAR.read_bytes())
    try:
        checker = ControlService(ws, role='checker')
        inspected = checker.call('vasp_inspect_structure', {'path': 'structures/Si.POSCAR'})
        assert inspected['ok'] is True
        res = inspected['result']
        assert res['content_is_untrusted'] is True
        assert res['formula'] == 'Si'
        assert res['path'] == 'structures/Si.POSCAR'
        assert res['potentials']['library_configured'] is True
        assert res['potentials']['selection'][0]['source'] == 'potentials/PBE/Si/POTCAR'
        assert str(ws) not in json.dumps(res, ensure_ascii=False)

        # Checker must not copy external files into the workspace.
        blocked = checker.call('vasp_inspect_structure', {'path': str(outside)})
        assert blocked['ok'] is False
        assert not (ws / 'structures' / outside.name).exists()
    finally:
        outside.unlink(missing_ok=True)


def test_preview_defaults_status_stage_review_and_read_evidence(tmp_path):
    ws = _setup_workspace(tmp_path, with_task=True, confirm_physics=False)
    service = ControlService(ws, role='checker')

    # Before physics confirmation, stage review refuses with PHYSICS_CONFIRMATION_REQUIRED.
    unconfirmed = service.call('vasp_get_stage_review', {'server': None, 'job_name': 'si-demo'})
    assert unconfirmed['ok'] is False
    assert unconfirmed['error_code'] == 'PHYSICS_CONFIRMATION_REQUIRED'

    stage_gate.confirm_physics(ws / 'runs/si-demo', 'Si 3D nonmagnetic PBE test', 'operator_slash_command')

    preview = service.call(
        'vasp_preview_defaults',
        {
            'server': None,
            'job_name': 'si-new',
            'structure': 'structures/Si.POSCAR',
            'encut': 400,
            'mesh': [4, 4, 4],
            'nbands': 16,
            'relax_mode': 'cell',
            'optional_stages': ['dos'],
        },
    )
    assert preview['ok'] is True
    assert 'defaults' in preview['result'] or 'signature' in preview['result']
    guides_list = preview['result'].get('guides') or []
    assert guides_list and guides_list[0]['id'] == 'dos'

    status = service.call('vasp_task_status', {'server': None, 'job_name': 'si-demo'})
    assert status['ok'] is True
    assert status['result']['manifest.json']['workflow'] == 'full'

    review = service.call('vasp_get_stage_review', {'server': None, 'job_name': 'si-demo'})
    assert review['ok'] is True
    assert review['result']['phase'] == 'convergence'
    assert review['result']['snapshot']
    assert review['result']['review_lease']['lease_id'].startswith('lse-')
    assert 'required_checks' in review['result']
    # Bulky stage review is compacted via model_views with full verbatim evidence saved.
    model_view = review['result'].get('model_view')
    assert model_view and model_view['compact'] is True
    full_sha = model_view['full']['sha256']

    chunks = []
    offset = 0
    while offset is not None:
        evidence = service.call('vasp_read_evidence', {'kind': 'evidence', 'sha256': full_sha, 'offset': offset})
        assert evidence['ok'] is True
        assert evidence['result']['historical_only'] is True
        assert '[…省略' not in evidence['result']['content']
        chunks.append(evidence['result']['content'])
        offset = evidence['result']['next_offset']
    reassembled = json.loads(''.join(chunks))
    assert reassembled['snapshot'] == review['result']['snapshot']

    guide_id = guides_list[0]['id']
    guide_page = service.call('vasp_read_evidence', {'kind': 'guide', 'guide': guide_id, 'page': 1})
    assert guide_page['ok'] is True
    assert guide_page['result']['guide_page']['id'] == guide_id
    assert guide_page['result']['text']

    (ws / 'runs/si-demo/events.jsonl').write_text('{"event":"prepared"}\n', encoding='utf-8', newline='\n')
    log_tail = service.call('vasp_read_evidence', {'kind': 'log', 'job_name': 'si-demo', 'file': 'events.jsonl'})
    assert log_tail['ok'] is True
    assert 'prepared' in log_tail['result']['tail']

    bad_log = service.call('vasp_read_evidence', {'kind': 'log', 'job_name': 'si-demo', 'file': 'POTCAR'})
    assert bad_log['ok'] is False


def test_diagnose_submission_routes_to_toolbox_read_only(tmp_path, monkeypatch):
    ws = _setup_workspace(tmp_path)
    service = ControlService(ws, role='checker')

    monkeypatch.setattr(
        service.box,
        'get_submission_diagnostic',
        lambda server, job_name, phase, job_id=None, start_time=None, end_time=None: {
            'status': 'diagnosed',
            'server': server,
            'job_name': job_name,
            'phase': phase,
            'diagnosis': {'confirmed_configuration_findings': [], 'resource_layout_candidates': []},
        },
    )
    monkeypatch.setattr(
        service.box,
        'get_submission_recovery',
        lambda server, job_name, operation_id: {'operation_id': operation_id, 'status': 'completed'},
    )
    monkeypatch.setattr(
        service.box,
        'get_submission_resources',
        lambda server, job_name, phase: {'resources': {'nodes': 1, 'cores_per_test': 16}, 'signature': 'sig-1'},
    )

    op_id = 'a' * 32
    res = service.call(
        'vasp_diagnose_submission',
        {'server': 'hpc', 'job_name': 'si-1', 'phase': 'convergence', 'operation_id': op_id},
    )
    assert res['ok'] is True
    assert res['result']['status'] == 'diagnosed'
    assert res['result']['recovery']['operation_id'] == op_id
    assert res['result']['resources_context']['signature'] == 'sig-1'


def test_maker_fetch_results_and_docker_only_sandbox_analysis(tmp_path, monkeypatch):
    ws = _setup_workspace(tmp_path)
    maker = ControlService(ws, role='maker')

    monkeypatch.setattr(maker.box, 'remote_fetch', lambda server, job_name: {'downloaded': True, 'job_name': job_name})
    monkeypatch.setattr(
        maker.box,
        'analyze_band_results',
        lambda server, job_name, table_offset=0, table_limit=0: {'gap_eV': 0.62, 'table_limit': table_limit},
    )
    fetched = maker.call('vasp_fetch_results', {'server': 'hpc', 'job_name': 'si-1', 'analyze_bands': True})
    assert fetched['ok'] is True
    assert fetched['result']['fetch']['downloaded'] is True
    assert fetched['result']['band_analysis']['gap_eV'] == 0.62

    monkeypatch.setattr(maker.box, 'get_analysis_runtime', lambda: {'docker_available': True, 'profiles': ['default']})
    runtime = maker.call('vasp_sandbox_analysis', {'action': 'runtime'})
    assert runtime['ok'] is True
    assert runtime['result']['docker_available'] is True

    captured = {}

    def fake_run(script, expected_sha256, inputs, timeout_seconds, image_profile=None, export_outputs=None):
        captured.update(
            script=script,
            expected_sha256=expected_sha256,
            inputs=inputs,
            timeout_seconds=timeout_seconds,
            image_profile=image_profile,
            export_outputs=export_outputs,
        )
        return {'run_id': 'run-1', 'status': 'ok'}

    monkeypatch.setattr(maker.box, 'run_analysis_script', fake_run)
    run_res = maker.call(
        'vasp_sandbox_analysis',
        {
            'action': 'run',
            'script': 'plot.py',
            'expected_sha256': 'b' * 64,
            'inputs': [],
            'timeout_seconds': 20,
            'image_profile': 'scientific',
            'export_outputs': True,
        },
    )
    assert run_res['ok'] is True
    assert captured['image_profile'] == 'scientific'
    assert captured['export_outputs'] is True

    # Never allows local host execution downgrade.
    bad_local = maker.call('vasp_sandbox_analysis', {'action': 'local'})
    assert bad_local['ok'] is False


def test_response_hard_capped_at_12kb_with_evidence_roundtrip_and_scrub(tmp_path, monkeypatch):
    ws = _setup_workspace(tmp_path)
    service = ControlService(ws, role='checker')

    secret_token = 'sk-1234567890abcdefghijklmnop'
    huge_payload = {
        'snapshot': 'snap-abc123',
        'phase': 'scf',
        'status': 'awaiting_review',
        'required_checks': ['convergence', 'electronic'],
        'note': f'secret leaked {secret_token}',
        'uncollapsible': [{'idx': i, 'blob': f'entry-{i}-' + ('x' * 40)} for i in range(400)],
    }
    monkeypatch.setattr(service.box, 'get_stage_review', lambda server, job_name: huge_payload)

    res = service.call('vasp_get_stage_review', {'server': 'hpc', 'job_name': 'si-big'})
    assert res['ok'] is True
    encoded = json.dumps(res, ensure_ascii=False, separators=(',', ':')).encode('utf-8')
    assert len(encoded) <= MAX_RESPONSE_BYTES
    assert secret_token not in encoded.decode('utf-8')

    result = res['result']
    assert result['payload_overflow'] is True
    assert result['summary']['snapshot'] == 'snap-abc123'
    assert result['summary']['phase'] == 'scf'
    read_tool = result['read_tool']
    assert read_tool['name'] == 'vasp_read_evidence'
    assert read_tool['arguments']['kind'] == 'evidence'

    # Round-trip read back from .agent/evidence/<sha256>.json.
    page = service.call(read_tool['name'], read_tool['arguments'])
    assert page['ok'] is True
    assert 'snap-abc123' in page['result']['content']
    assert secret_token not in page['result']['content']


def test_vasp_ctl_cli_tools_call_and_error_exit_codes(tmp_path, capsys):
    ws = _setup_workspace(tmp_path, with_task=True)

    rc = control_cli.main(['--workspace', str(ws), '--role', 'checker', 'tools', '--json'])
    assert rc == 0
    tools_payload = json.loads(capsys.readouterr().out)
    assert set(t['name'] for t in tools_payload['tools']) == CHECKER_TOOLS

    rc = control_cli.main(['--workspace', str(ws), '--role', 'maker', 'call', 'vasp_workspace_info', '--json'])
    assert rc == 0
    call_payload = json.loads(capsys.readouterr().out)
    assert call_payload['ok'] is True
    assert 'si-demo' in call_payload['result']['jobs']

    rc = control_cli.main([
        '--workspace', str(ws), '--role', 'checker', 'call', 'vasp_fetch_results',
        '--args', '{"server":"hpc","job_name":"si-demo"}', '--json',
    ])
    assert rc == 1
    denied = json.loads(capsys.readouterr().out)
    assert denied['ok'] is False
    assert denied['error_code'] == 'TOOL_NOT_ALLOWED_FOR_ROLE'

    rc = control_cli.main(['--workspace', str(ws), 'call', 'vasp_workspace_info', '--args', '{bad json', '--json'])
    assert rc == 2
    bad_json = json.loads(capsys.readouterr().out)
    assert bad_json['ok'] is False
    assert bad_json['error_code'] == 'INVALID_ARGUMENTS'

    rc = control_cli.main(['--workspace', str(tmp_path / 'missing-ws'), 'call', 'vasp_workspace_info', '--json'])
    assert rc == 2
    bad_ws = json.loads(capsys.readouterr().out)
    assert bad_ws['ok'] is False
    assert bad_ws['error_code'] == 'INVALID_WORKSPACE'


def test_remote_preview_defaults_stale_release_emits_no_stdout(tmp_path, monkeypatch, capsys):
    ws = _setup_workspace(tmp_path)
    service = ControlService(ws, role='checker')
    from vasp_agent.chat import release_check
    from vasp_agent.remote import remote_compute

    monkeypatch.setattr(
        release_check,
        'check',
        lambda _box, _server: {
            'current': False,
            'deployed': True,
            'local_version': 'aaaa1111aaaa1111',
            'deployed_version': 'bbbb2222bbbb2222',
        },
    )

    class _FakeRemote:
        def __init__(self, _workspace, _server):
            pass

        def prepare(self, *_args, **_kwargs):
            return {'signature': 'sig-remote', 'defaults': {'encuts': [400, 460, 520]}}

    monkeypatch.setattr(remote_compute, 'RemoteCompute', _FakeRemote)

    res = service.call(
        'vasp_preview_defaults',
        {
            'server': 'hpc',
            'job_name': 'si-remote',
            'structure': 'structures/Si.POSCAR',
            'encut': 400,
            'mesh': [4, 4, 4],
            'nbands': 16,
            'relax_mode': 'cell',
        },
    )
    assert res['ok'] is True
    assert res['result']['signature'] == 'sig-remote'
    assert capsys.readouterr().out == ''


def test_read_evidence_log_enforces_12kb_cap_and_preserves_newest_tail(tmp_path):
    ws = _setup_workspace(tmp_path, with_task=True)
    service = ControlService(ws, role='checker')
    log_path = ws / 'runs/si-demo/OUTCAR'
    # Multibyte UTF-8 + newlines expand well past 12 KB JSON when read_tail reads 12000 chars.
    lines = [f'行号{i:04d}="状态正常"\n' for i in range(900)]
    lines.append('FINAL_TAIL_MARKER_9999\n')
    log_path.write_text(''.join(lines), encoding='utf-8', newline='\n')

    res = service.call('vasp_read_evidence', {'kind': 'log', 'job_name': 'si-demo', 'file': 'OUTCAR'})
    assert res['ok'] is True
    encoded = json.dumps(res, ensure_ascii=False, separators=(',', ':')).encode('utf-8')
    assert len(encoded) <= MAX_RESPONSE_BYTES
    assert 'FINAL_TAIL_MARKER_9999' in res['result']['tail']
    assert res['result']['truncated_to_response_limit'] is True
    read_tool = res['result']['read_tool']
    assert read_tool['name'] == 'vasp_read_evidence'
    assert read_tool['arguments']['kind'] == 'evidence'
    full_page = service.call(read_tool['name'], read_tool['arguments'])
    assert full_page['ok'] is True


def test_control_service_serializes_concurrent_calls(tmp_path, monkeypatch):
    ws = _setup_workspace(tmp_path)
    service = ControlService(ws, role='checker')
    active = 0
    max_active = 0
    probe_lock = threading.Lock()
    entered = threading.Event()

    def slow_status(contract_id=None):
        nonlocal active, max_active
        with probe_lock:
            active += 1
            max_active = max(max_active, active)
            entered.set()
        time.sleep(0.08)
        with probe_lock:
            active -= 1
        return {'active': False, 'contract_id': contract_id, 'contracts': [], 'submission_enabled': False}

    monkeypatch.setattr(service, 'vasp_contract_status', slow_status)

    results = []
    t1 = threading.Thread(target=lambda: results.append(service.call('vasp_contract_status', {})))
    t2 = threading.Thread(target=lambda: results.append(service.call('vasp_contract_status', {})))
    t1.start()
    entered.wait(timeout=2)
    t2.start()
    t1.join(timeout=5)
    t2.join(timeout=5)

    assert len(results) == 2 and all(r['ok'] for r in results)
    assert max_active == 1


def test_no_lower_or_peer_package_imports_control():
    src_root = ROOT / 'src/vasp_agent'
    offenders = []
    for path in sorted(src_root.rglob('*.py')):
        rel = path.relative_to(src_root).as_posix()
        if rel.startswith('control/'):
            continue
        tree = ast.parse(path.read_text(encoding='utf-8'), filename=rel)
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    if alias.name == 'vasp_agent.control' or alias.name.startswith('vasp_agent.control.'):
                        offenders.append((rel, alias.name))
            elif isinstance(node, ast.ImportFrom) and node.module:
                if node.module == 'vasp_agent.control' or node.module.startswith('vasp_agent.control.'):
                    offenders.append((rel, node.module))
    assert not offenders, f'Unexpected reverse/peer imports of control/: {offenders}'


def test_vasp_ctl_main_reconfigures_gbk_stdio_to_utf8_with_non_gbk_chars(tmp_path, monkeypatch):
    ws = _setup_workspace(tmp_path, with_task=True)
    out_raw, err_raw = io.BytesIO(), io.BytesIO()
    out_wrap = io.TextIOWrapper(out_raw, encoding='gbk')
    err_wrap = io.TextIOWrapper(err_raw, encoding='gbk')
    monkeypatch.setattr(sys, 'stdout', out_wrap)
    monkeypatch.setattr(sys, 'stderr', err_wrap)

    expected = {'unit': 'eV/Å', 'mark': '✓', 'tier': 'Tier ③', 'zh': '非磁性 PBE'}
    monkeypatch.setattr(ControlService, 'call', lambda self, name, args=None: {'ok': True, 'result': expected})
    rc = control_cli.main(['--workspace', str(ws), '--role', 'maker', 'call', 'vasp_workspace_info', '--json'])
    assert rc == 0
    out_wrap.flush()
    payload = json.loads(out_raw.getvalue().decode('utf-8'))
    assert payload['ok'] is True and payload['result'] == expected

    rc_err = control_cli.main(['--workspace', str(ws), 'contract', 'show', 'bad;id'])
    assert rc_err == 1
    err_wrap.flush()
    assert 'CONTRACT_SCHEMA_INVALID' in err_raw.getvalue().decode('utf-8')


def test_vasp_ctl_subprocess_json_utf8_under_gbk_mode(tmp_path):
    env = {k: v for k, v in os.environ.items() if k.upper() not in ('PYTHONUTF8', 'PYTHONIOENCODING')}
    src_dir = str(ROOT / 'src')
    env['PYTHONPATH'] = src_dir + (os.pathsep + env['PYTHONPATH'] if env.get('PYTHONPATH') else '')

    for argv in (
        ['--role', 'maker', 'tools', '--json'],
        ['--role', 'maker', 'call', 'vasp_workspace_info', '--json'],
        ['contract', 'show', '--json'],
        ['watch', 'once', '--no-poll', '--json'],
    ):
        proc = subprocess.run(
            [sys.executable, '-X', 'utf8=0', '-m', 'vasp_agent.control.cli', '--workspace', str(tmp_path), *argv],
            capture_output=True, env=env, timeout=30,
        )
        assert proc.returncode == 0, f'{argv}: {proc.stderr.decode("utf-8", "replace")}'
        parsed = json.loads(proc.stdout.decode('utf-8'))
        assert isinstance(parsed, dict)
        if argv[0] == '--role' and argv[2] == 'tools':
            assert len(parsed['tools']) == 19
        else:
            assert parsed['ok'] is True


def test_max_response_bytes_is_24kb_and_paging_is_8000_without_affecting_interactive_cli(tmp_path):
    assert MAX_RESPONSE_BYTES == 24 * 1024
    ws = _setup_workspace(tmp_path, with_task=True)
    service = ControlService(ws, role='checker')
    stored = service.box.store_evidence({'blob': 'a' * 10000})
    sha = stored['sha256']

    ctl_page = service.call('vasp_read_evidence', {'kind': 'evidence', 'sha256': sha, 'offset': 0})
    assert ctl_page['ok'] is True
    assert len(ctl_page['result']['content']) == 8000
    assert ctl_page['result']['next_offset'] == 8000

    # Interactive CLI Toolbox.read_context_evidence keeps its 3500-char default.
    cli_page = service.box.read_context_evidence(sha, 0)
    assert len(cli_page['content']) == 3500
    assert cli_page['next_offset'] == 3500


def test_read_context_evidence_validates_page_chars(tmp_path):
    ws = _setup_workspace(tmp_path)
    service = ControlService(ws, role='checker')
    sha = service.box.store_evidence({'ok': True})['sha256']
    for bad in (True, False, 0, -10, 20001, '8000'):
        with pytest.raises(ValueError, match='分页'):
            service.box.read_context_evidence(sha, 0, page_chars=bad)


def test_stage_review_overflow_stores_compacted_view_and_raw_sha256(tmp_path, monkeypatch):
    ws = _setup_workspace(tmp_path)
    service = ControlService(ws, role='checker')
    secret_token = 'sk-secret1234567890abcdef'
    hashes = {f'step-{i:02d}/vasprun.xml': f'{i:064x}' for i in range(40)}
    big_stage_ctx = {
        'phase': 'scf',
        'snapshot': 'a' * 64,
        'status': 'awaiting_review',
        'required_checks': ['physics', 'geometry', 'post_relax_convergence'],
        'note': f'leaked {secret_token}',
        'incar_solver_diff': {
            'version': 1,
            'phase': 'scf',
            'reason': 'SCF electronic convergence tuning',
            'changes': [{'tag': 'NELM', 'baseline': 150, 'override': 200, 'applies_to': ['scf']}],
        },
        'inputs': {
            'environment': {'selected': 'vasp544-public', 'capabilities': ['vasp:5']},
            'manifest': {'ready': True, 'hashes': dict(hashes)},
            'initial_inputs': {
                f'convergence/encut-{i}/INCAR': 'ENCUT = 400\n' + ('# bulky\n' * 120)
                for i in range(6)
            },
            'plan': {
                'incars': {
                    'convergence/encut-0': {'ENCUT': 350},
                    'relax-1': {'ENCUT': 400, 'IBRION': 2},
                    'scf': {'ENCUT': 400, 'LCHARG': True},
                },
            },
        },
        'evidence': {
            'summaries': {
                'convergence.json': {'passed': True, 'pad': 'c' * 4000},
                'relaxation.json': {'passed': True, 'max_force': 0.002},
            },
            'hashes': dict(hashes),
        },
        'uncollapsible': [{'idx': i, 'blob': f'entry-{i}-' + ('x' * 55)} for i in range(420)],
    }
    monkeypatch.setattr(service.box, 'get_stage_review', lambda server, job_name: big_stage_ctx)

    res = service.call('vasp_get_stage_review', {'server': 'hpc', 'job_name': 'si-huge'})
    assert res['ok'] is True
    encoded = json.dumps(res, ensure_ascii=False, separators=(',', ':')).encode('utf-8')
    assert len(encoded) <= MAX_RESPONSE_BYTES
    assert secret_token not in encoded.decode('utf-8')

    result = res['result']
    assert result['payload_overflow'] is True
    assert result['sha256'] != result['raw_sha256']
    assert result['chars'] < result['raw_chars']
    assert result['read_tool']['arguments']['sha256'] == result['sha256']
    assert result['summary']['incar_solver_diff']['phase'] == 'scf'
    assert result['summary']['review_lease']['lease_id'].startswith('lse-')

    def _read_all(digest: str) -> dict:
        text, offset = '', 0
        while offset is not None:
            pg = service.call('vasp_read_evidence', {'kind': 'evidence', 'sha256': digest, 'offset': offset})
            assert pg['ok'] is True
            text += pg['result']['content']
            offset = pg['result']['next_offset']
        assert secret_token not in text
        return json.loads(text)

    compacted_view = _read_all(result['sha256'])
    raw_view = _read_all(result['raw_sha256'])
    assert compacted_view['stage_review_view']['projected'] is True
    assert compacted_view['stage_review_view']['full_sha256'] == result['raw_sha256']
    assert 'stage_review_view' not in raw_view
    assert 'convergence.json' in raw_view['evidence']['summaries']


def test_stage_review_normal_payload_inlines_without_overflow(tmp_path, monkeypatch):
    ws = _setup_workspace(tmp_path)
    service = ControlService(ws, role='checker')
    hashes = {f'step-{i:02d}/vasprun.xml': f'{i:064x}' for i in range(60)}
    realistic_45kb = {
        'phase': 'scf',
        'snapshot': 'b' * 64,
        'required_checks': ['physics', 'geometry', 'post_relax_convergence'],
        'physics': {'dimension': '3D', 'magnetism': 'nonmagnetic', 'functional': 'PBE'},
        'inputs': {
            'environment': {'selected': 'vasp544-public', 'capabilities': ['vasp:5']},
            'manifest': {'ready': True, 'hashes': dict(hashes)},
            'initial_poscar': 'Si2\n' + ('0.0 0.0 0.0\n' * 150),
            'submit_script': '#!/bin/bash\n' + ('echo step\n' * 200),
            'initial_inputs': {
                f'convergence/encut-{i}/INCAR': 'ENCUT = 400\n' + ('# comment line\n' * 300)
                for i in range(8)
            },
            'plan': {
                'incars': {
                    'convergence/encut-0': {'ENCUT': 350},
                    'relax-1': {'ENCUT': 400, 'IBRION': 2},
                    'scf': {'ENCUT': 400, 'LCHARG': True},
                    'bands': {'ENCUT': 400, 'ICHARG': 11},
                },
                'kpoints': {'relax-1': 'Gamma 8 8 8\n', 'relax-2': 'Gamma 8 8 8\n'},
                'parameter_notes': {f'K{i}': {'source': 'default', 'explanation': 'x' * 80} for i in range(25)},
            },
        },
        'evidence': {
            'summaries': {
                'convergence.json': {'passed': True, 'pad': 'z' * 4000},
                'relaxation.json': {'passed': True, 'max_force': 0.001, 'forces': [[0.001, 0.0, 0.0]] * 20},
                'geometry-summary.json': {'volume_change_pct': 0.2},
                'post-relax-validation.json': {'passed': True},
            },
            'hashes': dict(hashes),
        },
    }
    assert len(json.dumps(realistic_45kb, ensure_ascii=False).encode('utf-8')) > 40 * 1024
    monkeypatch.setattr(service.box, 'get_stage_review', lambda server, job_name: realistic_45kb)

    res = service.call('vasp_get_stage_review', {'server': 'hpc', 'job_name': 'si-normal'})
    assert res['ok'] is True
    assert not res['result'].get('payload_overflow')
    assert res['result']['stage_review_view']['projected'] is True
    assert len(json.dumps(res, ensure_ascii=False, separators=(',', ':')).encode('utf-8')) < 12 * 1024

