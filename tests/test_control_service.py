"""Offline tests for P0 headless ControlService, model_views compaction, and vasp-ctl CLI."""
from __future__ import annotations

import ast
import contextlib
import io
import json
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
}
MAKER_ONLY_TOOLS = {
    'vasp_fetch_results',
    'vasp_sandbox_analysis',
}
FORBIDDEN_P0_TOOLS = {
    'submit_stage',
    'remote_submit',
    'submit_job',
    'request_submission',
    'request_job_cancellation',
    'vasp_submit_stage',
    'vasp_draft_contract',
    'vasp_prepare_task',
    'vasp_prepare_resume',
    'vasp_prepare_repair_fork',
    'vasp_prepare_scan_retry',
    'vasp_record_stage_review',
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

    assert set(maker.tool_names()) == READ_ONLY_TOOLS | MAKER_ONLY_TOOLS
    assert set(checker.tool_names()) == READ_ONLY_TOOLS
    assert not (set(maker.tool_names()) & FORBIDDEN_P0_TOOLS)
    assert not (set(checker.tool_names()) & FORBIDDEN_P0_TOOLS)

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

    for name in ('submit_stage', 'remote_submit', 'vasp_submit_stage', 'request_submission', 'vasp_draft_contract'):
        res = maker.call(name, {'server': 'hpc', 'job_name': 'si', 'phase': 'convergence'})
        assert res['ok'] is False
        assert res['error_code'] == 'UNKNOWN_TOOL'

    denied_fetch = checker.call('vasp_fetch_results', {'server': 'hpc', 'job_name': 'si'})
    assert denied_fetch['ok'] is False
    assert denied_fetch['error_code'] == 'TOOL_NOT_ALLOWED_FOR_ROLE'

    denied_sandbox = checker.call('vasp_sandbox_analysis', {'action': 'runtime'})
    assert denied_sandbox['ok'] is False
    assert denied_sandbox['error_code'] == 'TOOL_NOT_ALLOWED_FOR_ROLE'


def test_workspace_info_and_contract_status_are_read_only(tmp_path):
    ws = _setup_workspace(tmp_path, with_task=True)
    service = ControlService(ws, role='checker', actor_id='chk-01')

    info = service.call('vasp_workspace_info', {})
    assert info['ok'] is True
    result = info['result']
    assert result['role'] == 'checker'
    assert result['actor_id'] == 'chk-01'
    assert result['submission_enabled'] is False
    assert result['autonomy_phase'] == 'P0_read_only'
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
    assert set(t['name'] for t in tools_payload['tools']) == READ_ONLY_TOOLS

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
