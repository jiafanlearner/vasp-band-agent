"""Offline tests for P1b: vasp_draft_contract, vasp_circuit_break, vasp_contract_status, and vasp-ctl contract."""
from __future__ import annotations

import ast
import hashlib
import io
import json
from pathlib import Path
import shutil
import subprocess
from types import SimpleNamespace

import pytest
from pymatgen.io.vasp.inputs import Potcar

from vasp_agent.common import agent_paths
from vasp_agent.control import ControlService
from vasp_agent.control import cli as control_cli
from vasp_agent.permissions import contract_ui, task_contract
from vasp_agent.planning import parameter_plans

ROOT = Path(__file__).resolve().parents[1]
EXAMPLE_POSCAR = ROOT / 'examples/Si.POSCAR'
FAKE_POTCAR = 'PAW_PBE Si 05Jan2001\n'


@pytest.fixture(autouse=True)
def fake_pbe_potcar(monkeypatch):
    fake = [SimpleNamespace(element='Si', functional='PBE', TITEL='PAW_PBE Si 05Jan2001', ZVAL=4.0, enmax=245.345)]
    monkeypatch.setattr(Potcar, 'from_str', classmethod(lambda _cls, _text: fake))
    monkeypatch.setattr(Potcar, 'from_file', classmethod(lambda _cls, _path: fake))


def _setup_workspace(tmp_path: Path) -> Path:
    structures = tmp_path / 'structures'
    structures.mkdir(parents=True, exist_ok=True)
    (structures / 'Si.POSCAR').write_bytes(EXAMPLE_POSCAR.read_bytes())
    potcar = tmp_path / 'potentials/PBE/Si/POTCAR'
    potcar.parent.mkdir(parents=True, exist_ok=True)
    potcar.write_text(FAKE_POTCAR, encoding='utf-8', newline='\n')
    return tmp_path


def _local_envelope(phases: list[str] | None = None) -> dict:
    allowed = phases or ['convergence', 'relaxation', 'scf', 'bands']
    base = {
        'submission_mode': 'allocation',
        'parallel_tests': 1,
        'cores_per_test': 8,
        'nodes': 1,
        'partition': 'compute',
        'walltime': '02:00:00',
    }
    return {
        'allowed_modes': ['allocation'],
        'allowed_partitions': ['compute'],
        'max_parallel_tests': 1,
        'max_cores_per_test': 16,
        'max_nodes': 1,
        'max_total_cores': 16,
        'max_walltime': '04:00:00',
        'phase_defaults': {phase: dict(base) for phase in allowed},
    }


def _draft_args(**overrides) -> dict:
    phases = overrides.get('allowed_phases') or (
        ['convergence', 'relaxation', 'scf', 'bands', 'dos']
        if overrides.get('optional_stages') == ['dos']
        else ['convergence', 'relaxation', 'scf', 'bands']
    )
    payload = {
        'server': None,
        'job_name': 'si-p1b-01',
        'structure': 'structures/Si.POSCAR',
        'encut': 400,
        'mesh': [4, 4, 4],
        'nbands': 16,
        'relax_mode': 'cell',
        'goal': 'Si 3D 非磁性 PBE 收敛测试、晶胞优化、SCF 与能带计算',
        'encuts': [420, 480, 540],
        'meshes': [[4, 4, 4], [6, 6, 6], [8, 8, 8]],
        'rationale': '提高 ENCUT 与 k 网格覆盖以验证收敛性；计算量适中；待收敛核验。',
        'resource_envelope': _local_envelope(phases),
    }
    payload.update(overrides)
    return payload


def _patch_tty(monkeypatch, answers: list[str], *, real_tty: bool = True):
    monkeypatch.setattr(task_contract, 'has_real_tty', lambda stream=None: real_tty)
    it = iter(answers)
    monkeypatch.setattr('builtins.input', lambda _prompt='': next(it))


# ------------------------------------------------ 1. vasp_draft_contract & roles

def test_draft_contract_creates_plan_and_draft_and_blocks_checker_and_bad_physics(tmp_path):
    ws = _setup_workspace(tmp_path)
    maker = ControlService(ws, role='maker', actor_id='mk-1')
    checker = ControlService(ws, role='checker', actor_id='chk-1')

    # Checker cannot draft contracts.
    denied = checker.call('vasp_draft_contract', _draft_args())
    assert denied['ok'] is False
    assert denied['error_code'] == 'TOOL_NOT_ALLOWED_FOR_ROLE'

    # Unsupported physics is hard-rejected before writing any scan plan or draft.
    bad_phys = maker.call(
        'vasp_draft_contract',
        _draft_args(job_name='si-mag', physics_assertion={'magnetism': 'collinear'}),
    )
    assert bad_phys['ok'] is False
    assert bad_phys['error_code'] == 'CONTRACT_UNSUPPORTED_PHYSICS'
    assert not list(agent_paths.store_items(ws, 'scan_plans', '*.json'))

    # Maker creates a valid draft linked to a pending scan plan (with AI scan differences).
    res = maker.call('vasp_draft_contract', _draft_args())
    assert res['ok'] is True, res
    result = res['result']
    draft_id = result['draft_id']
    plan_id = result['plan_id']
    assert result['status'] == 'draft'
    assert result['approved'] is False
    assert result['submission_enabled'] is False
    assert result['plan_status'] == 'pending'
    assert result['approve_command'] == f'vasp-ctl contract approve {draft_id}'
    assert str(ws) not in json.dumps(result, ensure_ascii=False)

    draft_file = agent_paths.store_dir(ws, 'contracts') / 'drafts' / f'{draft_id}.json'
    assert draft_file.is_file()
    assert b'\r\n' not in draft_file.read_bytes()
    draft = task_contract.load_draft(ws, draft_id)
    assert draft['scope']['structure'] == 'structures/Si.POSCAR'
    assert draft['scope']['structure_sha256'] == hashlib.sha256((ws / 'structures/Si.POSCAR').read_bytes()).hexdigest()
    assert draft['scope']['dimension'] == '3D'
    assert draft['workflow']['plan_id'] == plan_id
    plan_file = parameter_plans.path(maker.box, plan_id)
    assert draft['workflow']['plan_sha256'] == hashlib.sha256(plan_file.read_bytes()).hexdigest()
    assert json.loads(plan_file.read_text(encoding='utf-8'))['status'] == 'pending'

    # Contract status lists pending draft and remains inactive with submission disabled.
    status = checker.call('vasp_contract_status', {})
    assert status['ok'] is True
    assert status['result']['active'] is False
    assert status['result']['submission_enabled'] is False
    assert any(d['draft_id'] == draft_id for d in status['result']['drafts'])


# ------------------------------------ 2. Non-TTY refusal on approve/revoke/resume

def test_vasp_ctl_contract_refuses_non_tty_for_approve_revoke_and_resume(tmp_path, monkeypatch, capsys):
    ws = _setup_workspace(tmp_path)
    maker = ControlService(ws, role='maker')
    draft_res = maker.call('vasp_draft_contract', _draft_args())
    draft_id = draft_res['result']['draft_id']
    plan_id = draft_res['result']['plan_id']

    # Real non-TTY stream (StringIO) must fail without mocking has_real_tty.
    monkeypatch.setattr('sys.stdin', io.StringIO('1\n'))
    rc = control_cli.main(['--workspace', str(ws), 'contract', 'approve', draft_id, '--json'])
    assert rc == 1
    out = json.loads(capsys.readouterr().out)
    assert out['ok'] is False
    assert out['error_code'] == 'CONTRACT_TTY_REQUIRED'
    # Plan must stay pending and no active contract created.
    assert json.loads(parameter_plans.path(maker.box, plan_id).read_text(encoding='utf-8'))['status'] == 'pending'
    assert task_contract.contract_status(ws)['contracts'] == []

    # Approve via mocked real TTY, halt it, then verify non-TTY blocks revoke and resume too.
    _patch_tty(monkeypatch, ['1'], real_tty=True)
    rc_ok = control_cli.main(['--workspace', str(ws), 'contract', 'approve', draft_id, '--json'])
    assert rc_ok == 0
    cid = json.loads(capsys.readouterr().out)['result']['contract']['contract_id']
    task_contract.halt_contract(ws, cid, 'need review')

    monkeypatch.setattr(task_contract, 'has_real_tty', lambda stream=None: False)
    rc_res = control_cli.main(['--workspace', str(ws), 'contract', 'resume', cid, '--json'])
    assert rc_res == 1
    assert json.loads(capsys.readouterr().out)['error_code'] == 'CONTRACT_TTY_REQUIRED'

    rc_rev = control_cli.main(['--workspace', str(ws), 'contract', 'revoke', cid, '--json'])
    assert rc_rev == 1
    assert json.loads(capsys.readouterr().out)['error_code'] == 'CONTRACT_TTY_REQUIRED'


# --------------------- 3. Full TTY lifecycle + settle linkage + circuit break

def test_full_lifecycle_draft_approve_settle_halt_resume_revoke_and_show(tmp_path, monkeypatch, capsys):
    ws = _setup_workspace(tmp_path)
    maker = ControlService(ws, role='maker', actor_id='mk-life')
    checker = ControlService(ws, role='checker', actor_id='chk-life')

    # Draft with optional DOS stage so stage_parameters also needs settling on approval.
    draft_res = maker.call(
        'vasp_draft_contract',
        _draft_args(
            job_name='si-dos-life',
            optional_stages=['dos'],
            stage_parameters=[{'stage': 'dos', 'field': 'sampling_interval_eV', 'value': 0.005}],
            rationale='启用 DOS 并加密能量采样间隔以解析态密度峰；增加少量计算量；待审阅确认。',
        ),
    )
    assert draft_res['ok'] is True, draft_res
    draft_id = draft_res['result']['draft_id']
    plan_id = draft_res['result']['plan_id']

    # `vasp-ctl contract show <draft_id>` renders sectioned menu_text summary.
    rc_show_draft = control_cli.main(['--workspace', str(ws), 'contract', 'show', draft_id])
    assert rc_show_draft == 0
    shown_draft = capsys.readouterr().out
    assert draft_id in shown_draft
    assert '物理模型' in shown_draft
    assert '阶段链' in shown_draft
    assert '资源包络' in shown_draft
    assert 'maker_checker' in shown_draft

    # Operator cancels first -> plan remains pending, no active contract.
    _patch_tty(monkeypatch, ['q'], real_tty=True)
    rc_cancel = control_cli.main(['--workspace', str(ws), 'contract', 'approve', draft_id])
    assert rc_cancel == 1
    _ = capsys.readouterr()
    assert json.loads(parameter_plans.path(maker.box, plan_id).read_text(encoding='utf-8'))['status'] == 'pending'

    # Operator toggles review mode (`m` -> maker_direct), switches scan choice (`p` -> defaults), then approves (`1`).
    _patch_tty(monkeypatch, ['m', 'p', '1'], real_tty=True)
    rc_approve = control_cli.main(['--workspace', str(ws), 'contract', 'approve', draft_id])
    assert rc_approve == 0
    approve_out = capsys.readouterr().out
    assert 'maker_direct' in approve_out
    assert '工具默认' in approve_out

    # Verify settled scan plan matches approved contract's plan_sha256 and status == 'selected'.
    status_all = maker.call('vasp_contract_status', {})
    assert status_all['ok'] is True
    assert status_all['result']['active'] is True
    cid = status_all['result']['contracts'][0]['contract_id']

    detail = checker.call('vasp_contract_status', {'contract_id': cid})
    assert detail['ok'] is True
    contract = detail['result']['contract']
    ledger = detail['result']['ledger']
    assert contract['review_policy']['mode'] == 'maker_direct'
    assert ledger['status'] == 'active'

    plan_file = parameter_plans.path(maker.box, plan_id)
    settled_plan = json.loads(plan_file.read_text(encoding='utf-8'))
    assert settled_plan['status'] == 'selected'
    assert settled_plan['choice'] == 'operator_default'
    assert settled_plan['selected'] == settled_plan['defaults']
    assert settled_plan['stage_parameters']['dos']['selected']['sampling_interval_eV'] == 0.005
    assert contract['workflow']['plan_sha256'] == hashlib.sha256(plan_file.read_bytes()).hexdigest()

    # Checker triggers one-way circuit break (`vasp_circuit_break`) -> halted_uncertain.
    cb = checker.call(
        'vasp_circuit_break',
        {
            'contract_id': cid,
            'reason': 'SCF 出现不可解释的占据异常，熔断交人工复核',
            'code': 'SCF_ANOMALY',
            'uncertain': True,
            'details': {'phase': 'scf'},
        },
    )
    assert cb['ok'] is True
    assert cb['result']['status'] == 'halted_uncertain'
    assert cb['result']['reversible_by_agent'] is False
    assert f'vasp-ctl contract resume {cid}' in cb['result']['resume_command']

    # `vasp-ctl contract show <cid>` shows ledger status and halt reason.
    rc_show_c = control_cli.main(['--workspace', str(ws), 'contract', 'show', cid])
    assert rc_show_c == 0
    shown_c = capsys.readouterr().out
    assert cid in shown_c
    assert 'halted_uncertain' in shown_c
    assert 'SCF 出现不可解释的占据异常' in shown_c

    # Human TTY resumes contract -> active.
    _patch_tty(monkeypatch, ['1'], real_tty=True)
    rc_resume = control_cli.main(['--workspace', str(ws), 'contract', 'resume', cid, '--reason', 'checked_ok', '--json'])
    assert rc_resume == 0
    resumed_payload = json.loads(capsys.readouterr().out)
    assert resumed_payload['ok'] is True
    assert resumed_payload['result']['ledger']['status'] == 'active'

    # Human TTY revokes contract -> revoked (terminal).
    _patch_tty(monkeypatch, ['1'], real_tty=True)
    rc_revoke = control_cli.main(['--workspace', str(ws), 'contract', 'revoke', cid, '--reason', 'done_manual', '--json'])
    assert rc_revoke == 0
    revoked_payload = json.loads(capsys.readouterr().out)
    assert revoked_payload['ok'] is True
    assert revoked_payload['result']['ledger']['status'] == 'revoked'

    # Resuming a revoked contract is refused.
    _patch_tty(monkeypatch, ['1'], real_tty=True)
    rc_resume_dead = control_cli.main(['--workspace', str(ws), 'contract', 'resume', cid, '--json'])
    assert rc_resume_dead == 1
    assert json.loads(capsys.readouterr().out)['error_code'] == 'CONTRACT_REVOKED'


# --------------------- 4. Tampered plan detection, rollback & SSH CLI roundtrip

def test_approve_detects_tampered_plan_and_rolls_back_on_ledger_failure(tmp_path, monkeypatch):
    ws = _setup_workspace(tmp_path)
    maker = ControlService(ws, role='maker')
    draft_res = maker.call('vasp_draft_contract', _draft_args(job_name='si-rb-01'))
    draft_id = draft_res['result']['draft_id']
    plan_id = draft_res['result']['plan_id']
    plan_file = parameter_plans.path(maker.box, plan_id)
    original_bytes = plan_file.read_bytes()

    # Tampering scan plan between draft and approve must fail with CONTRACT_TAMPERED.
    tampered = json.loads(original_bytes.decode('utf-8'))
    tampered['suggestion']['encuts'] = [900, 950, 1000]
    plan_file.write_text(json.dumps(tampered, ensure_ascii=False, indent=2) + '\n', encoding='utf-8', newline='\n')
    monkeypatch.setattr(task_contract, 'has_real_tty', lambda stream=None: True)
    with pytest.raises(task_contract.ContractError) as exc:
        contract_ui.approve_contract_interactive(ws, draft_id, reader=lambda _p: '1', output=lambda _s: None)
    assert exc.value.error_code == 'CONTRACT_TAMPERED'

    # Restore original plan, simulate ledger creation failure -> plan must roll back to original bytes!
    plan_file.write_bytes(original_bytes)
    from vasp_agent.permissions import contract_ledger

    monkeypatch.setattr(
        contract_ledger,
        'create_initial_ledger',
        lambda *_a, **_kw: (_ for _ in ()).throw(OSError('simulated disk error')),
    )
    with pytest.raises(OSError, match='simulated disk error'):
        contract_ui.approve_contract_interactive(
            ws, draft_id, reader=lambda _p: '1', output=lambda _s: None, contract_id='ctr-20261126-rb000001',
        )
    assert plan_file.read_bytes() == original_bytes
    assert not (agent_paths.store_dir(ws, 'contracts') / 'active' / 'ctr-20261126-rb000001.json').exists()


def test_vasp_ctl_contract_approve_with_real_ssh_signing_and_overview(tmp_path, monkeypatch, capsys):
    ssh_keygen = shutil.which('ssh-keygen')
    if not ssh_keygen:
        pytest.skip('ssh-keygen not installed on test host')
    ws = _setup_workspace(tmp_path)
    key_dir = tmp_path / 'keys'
    key_dir.mkdir()
    key_path = key_dir / 'op_ed25519'
    subprocess.run([ssh_keygen, '-t', 'ed25519', '-N', '', '-f', str(key_path), '-q'], check=True)
    pub = (key_dir / 'op_ed25519.pub').read_text(encoding='utf-8').strip()
    (ws / 'servers').mkdir(exist_ok=True)
    (ws / 'servers/allowed_signers').write_text(f'op@local {pub}\n', encoding='utf-8', newline='\n')
    pol = agent_paths.file_path(ws, 'autonomy_policy')
    pol.parent.mkdir(parents=True, exist_ok=True)
    pol.write_text(
        json.dumps({'require_ssh_signature': True, 'allowed_signers': 'servers/allowed_signers', 'principal': 'op@local'}),
        encoding='utf-8',
        newline='\n',
    )

    maker = ControlService(ws, role='maker')
    draft_id = maker.call('vasp_draft_contract', _draft_args(job_name='si-ssh-cli'))['result']['draft_id']

    _patch_tty(monkeypatch, ['1'], real_tty=True)
    rc = control_cli.main([
        '--workspace', str(ws), 'contract', 'approve', draft_id,
        '--ssh-key', str(key_path), '--ssh-principal', 'op@local', '--json',
    ])
    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    cid = payload['result']['contract']['contract_id']
    assert payload['result']['contract']['approval']['ssh_signature']['principal'] == 'op@local'

    rc_ov = control_cli.main(['--workspace', str(ws), 'contract', 'show'])
    assert rc_ov == 0
    assert cid in capsys.readouterr().out


def test_prefix_draft_and_precision_bounds_validation_and_multi_env_tty_selection(tmp_path, monkeypatch):
    ws = _setup_workspace(tmp_path)
    maker = ControlService(ws, role='maker')

    # Prefix-only contract (`['convergence']`) is valid and creates a draft cleanly.
    prefix_res = maker.call(
        'vasp_draft_contract',
        _draft_args(
            job_name='si-prefix-01',
            allowed_phases=['convergence'],
            encuts=None,
            meshes=None,
            rationale='',
            resource_envelope=_local_envelope(['convergence']),
        ),
    )
    assert prefix_res['ok'] is True, prefix_res
    assert prefix_res['result']['workflow']['allowed_phases'] == ['convergence']

    # Initial scans exceeding caller's precision_bounds must be refused cleanly without stray scan plans.
    before_plans = set(agent_paths.store_items(ws, 'scan_plans', '*.json'))
    too_tight = maker.call(
        'vasp_draft_contract',
        _draft_args(
            job_name='si-tight-01',
            encuts=[450, 520, 620],
            precision_bounds={'max_encut': 550, 'max_kmesh_axis': 16, 'max_kmesh_points': 4096},
        ),
    )
    assert too_tight['ok'] is False
    assert too_tight['error_code'] == 'CONTRACT_PRECISION_BOUND_EXCEEDED'
    assert set(agent_paths.store_items(ws, 'scan_plans', '*.json')) == before_plans

    # Referencing an optional stage in allowed_phases without enabling it in optional_stages is refused.
    unenabled = maker.call(
        'vasp_draft_contract',
        _draft_args(
            job_name='si-opt-miss',
            allowed_phases=['convergence', 'relaxation', 'scf', 'bands', 'dos'],
            optional_stages=None,
            resource_envelope=_local_envelope(['convergence', 'relaxation', 'scf', 'bands', 'dos']),
        ),
    )
    assert unenabled['ok'] is False
    assert unenabled['error_code'] == 'CONTRACT_SCHEMA_INVALID'
    assert set(agent_paths.store_items(ws, 'scan_plans', '*.json')) == before_plans

    # Multi-environment plan: operator cannot confirm (`1`) until selecting an environment (`e` -> `2` -> `1`).
    draft_id = prefix_res['result']['draft_id']
    plan_id = prefix_res['result']['plan_id']
    plan_file = parameter_plans.path(maker.box, plan_id)
    plan = json.loads(plan_file.read_text(encoding='utf-8'))
    plan['status'] = 'pending'
    plan['environment'] = {
        'required': [],
        'candidates': [
            {'name': 'default', 'env_script': '/env/default.sh', 'version': '5.4.4', 'capabilities': [], 'implicit': True},
            {'name': 'vasp6', 'env_script': '/env/vasp6.sh', 'version': '6.4.3', 'capabilities': ['vasp6'], 'implicit': False},
        ],
        'excluded': [],
        'selected': None,
        'source': None,
    }
    plan_file.write_text(json.dumps(plan, ensure_ascii=False, indent=2) + '\n', encoding='utf-8', newline='\n')
    draft = task_contract.load_draft(ws, draft_id)
    draft['workflow']['plan_sha256'] = hashlib.sha256(plan_file.read_bytes()).hexdigest()
    task_contract.save_draft(ws, draft)

    shown: list[str] = []
    answers = iter(['1', 'e', '2', '1'])
    monkeypatch.setattr(task_contract, 'has_real_tty', lambda stream=None: True)
    approved = contract_ui.approve_contract_interactive(
        ws, draft_id, reader=lambda _p: next(answers), output=shown.append,
    )
    assert any('尚未选定' in msg for msg in shown)
    assert approved['plan_status'] == 'selected'
    settled = json.loads(plan_file.read_text(encoding='utf-8'))
    assert settled['environment']['selected'] == 'vasp6'
    assert settled['environment']['source'] == 'operator_menu'


# ------------------------ 6. P1b review hardening regressions (7 findings)

def test_toctou_tamper_during_tty_prompt_and_duplicate_draft_approval_and_prepared_precheck(tmp_path, monkeypatch):
    ws = _setup_workspace(tmp_path)
    maker = ControlService(ws, role='maker')

    # 1) TOCTOU: plan file modified AFTER menu opens and BEFORE operator presses '1' must be rejected.
    draft_res = maker.call('vasp_draft_contract', _draft_args(job_name='si-toctou-01'))
    draft_id = draft_res['result']['draft_id']
    plan_id = draft_res['result']['plan_id']
    plan_file = parameter_plans.path(maker.box, plan_id)
    orig_bytes = plan_file.read_bytes()

    def tamper_during_prompt(_prompt: str) -> str:
        mutated = json.loads(orig_bytes.decode('utf-8'))
        mutated['suggestion']['encuts'] = [650, 680, 700]
        plan_file.write_text(json.dumps(mutated, ensure_ascii=False, indent=2) + '\n', encoding='utf-8', newline='\n')
        return '1'

    monkeypatch.setattr(task_contract, 'has_real_tty', lambda stream=None: True)
    with pytest.raises(task_contract.ContractError) as exc:
        contract_ui.approve_contract_interactive(ws, draft_id, reader=tamper_during_prompt, output=lambda _s: None)
    assert exc.value.error_code == 'CONTRACT_TAMPERED'
    assert task_contract.contract_status(ws)['contracts'] == []

    # 2) Restore plan, approve once -> second approval of same pending draft MUST fail with CONTRACT_DRAFT_ALREADY_APPROVED.
    plan_file.write_bytes(orig_bytes)
    prompted: list[str] = []
    contract_ui.approve_contract_interactive(ws, draft_id, reader=lambda _p: '1', output=lambda _s: None)
    with pytest.raises(task_contract.ContractError) as exc:
        contract_ui.approve_contract_interactive(
            ws, draft_id, reader=lambda p: prompted.append(p) or '1', output=lambda _s: None,
        )
    assert exc.value.error_code == 'CONTRACT_DRAFT_ALREADY_APPROVED'
    assert prompted == []

    # 3) Same for tool_default draft (whose scan_plan was already selected before first approve).
    def_res = maker.call(
        'vasp_draft_contract',
        _draft_args(job_name='si-dup-default', encuts=None, meshes=None, rationale=''),
    )
    def_draft_id = def_res['result']['draft_id']
    contract_ui.approve_contract_interactive(ws, def_draft_id, reader=lambda _p: '1', output=lambda _s: None)
    with pytest.raises(task_contract.ContractError) as exc:
        contract_ui.approve_contract_interactive(
            ws, def_draft_id, reader=lambda p: prompted.append(p) or '1', output=lambda _s: None,
        )
    assert exc.value.error_code == 'CONTRACT_DRAFT_ALREADY_APPROVED'
    assert prompted == []

    # 4) Already-prepared scan plan must fail BEFORE opening TTY prompt in load_linked_plan.
    prep_res = maker.call('vasp_draft_contract', _draft_args(job_name='si-prep-pre'))
    prep_draft_id = prep_res['result']['draft_id']
    prep_plan_file = parameter_plans.path(maker.box, prep_res['result']['plan_id'])
    prep_plan = json.loads(prep_plan_file.read_text(encoding='utf-8'))
    prep_plan['status'] = 'prepared'
    prep_plan_file.write_text(json.dumps(prep_plan, ensure_ascii=False, indent=2) + '\n', encoding='utf-8', newline='\n')
    prep_draft = task_contract.load_draft(ws, prep_draft_id)
    prep_draft['workflow']['plan_sha256'] = hashlib.sha256(prep_plan_file.read_bytes()).hexdigest()
    task_contract.save_draft(ws, prep_draft)
    with pytest.raises(task_contract.ContractError) as exc:
        contract_ui.approve_contract_interactive(
            ws, prep_draft_id, reader=lambda p: prompted.append(p) or '1', output=lambda _s: None,
        )
    assert exc.value.error_code == 'CONTRACT_PLAN_ALREADY_PREPARED'
    assert prompted == []


def test_json_cli_keeps_stdout_pure_json_when_input_writes_prompt_to_stdout(tmp_path, monkeypatch, capsys):
    import sys

    ws = _setup_workspace(tmp_path)
    maker = ControlService(ws, role='maker')
    draft_id = maker.call('vasp_draft_contract', _draft_args(job_name='si-json-tty'))['result']['draft_id']

    def cpython_like_input(prompt: str = '') -> str:
        if prompt:
            sys.stdout.write(prompt)
            sys.stdout.flush()
        return '1'

    monkeypatch.setattr(task_contract, 'has_real_tty', lambda stream=None: True)
    monkeypatch.setattr('builtins.input', cpython_like_input)

    rc = control_cli.main(['--workspace', str(ws), 'contract', 'approve', draft_id, '--json'])
    assert rc == 0
    captured = capsys.readouterr()
    parsed = json.loads(captured.out)
    assert parsed['ok'] is True
    cid = parsed['result']['contract']['contract_id']
    assert captured.err

    task_contract.halt_contract(ws, cid, 'check json resume')
    rc_res = control_cli.main(['--workspace', str(ws), 'contract', 'resume', cid, '--json'])
    assert rc_res == 0
    assert json.loads(capsys.readouterr().out)['ok'] is True

    rc_rev = control_cli.main(['--workspace', str(ws), 'contract', 'revoke', cid, '--json'])
    assert rc_rev == 0
    assert json.loads(capsys.readouterr().out)['ok'] is True


def test_precision_bounds_enforced_on_defaults_toggle_and_orphan_plan_cleaned_and_blank_circuit_break(tmp_path, monkeypatch):
    ws = _setup_workspace(tmp_path)
    maker = ControlService(ws, role='maker')

    # 1) tool_default exceeding tight precision_bounds must fail AND leave ZERO orphan scan plans on disk.
    before_plans = set(agent_paths.store_items(ws, 'scan_plans', '*.json'))
    tight_default = maker.call(
        'vasp_draft_contract',
        _draft_args(
            job_name='si-tight-def',
            encuts=None,
            meshes=None,
            rationale='',
            precision_bounds={'max_encut': 450, 'max_kmesh_axis': 16, 'max_kmesh_points': 4096},
        ),
    )
    assert tight_default['ok'] is False
    assert tight_default['error_code'] == 'CONTRACT_PRECISION_BOUND_EXCEEDED'
    assert set(agent_paths.store_items(ws, 'scan_plans', '*.json')) == before_plans

    # 2) AI suggestion fits inside precision_bounds (<= 460), while tool defaults reach 500 (> 460):
    # toggling `p` back to defaults during TTY approval must be rejected with CONTRACT_PRECISION_BOUND_EXCEEDED!
    ai_low = maker.call(
        'vasp_draft_contract',
        _draft_args(
            job_name='si-low-ai',
            encut=400,
            encuts=[350, 400, 450],
            meshes=[[4, 4, 4], [6, 6, 6], [8, 8, 8]],
            precision_bounds={'max_encut': 460, 'max_kmesh_axis': 16, 'max_kmesh_points': 4096},
            rationale='保守低截断测试；验证上限校验；待核验。',
        ),
    )
    assert ai_low['ok'] is True, ai_low
    ai_draft_id = ai_low['result']['draft_id']
    ai_plan_file = parameter_plans.path(maker.box, ai_low['result']['plan_id'])
    orig_ai_plan = ai_plan_file.read_bytes()
    assert max(json.loads(orig_ai_plan.decode('utf-8'))['defaults']['encuts']) > 460

    monkeypatch.setattr(task_contract, 'has_real_tty', lambda stream=None: True)
    answers = iter(['p', '1'])
    with pytest.raises(task_contract.ContractError) as exc:
        contract_ui.approve_contract_interactive(
            ws, ai_draft_id, reader=lambda _p: next(answers), output=lambda _s: None,
        )
    assert exc.value.error_code == 'CONTRACT_PRECISION_BOUND_EXCEEDED'
    assert ai_plan_file.read_bytes() == orig_ai_plan

    # 3) Blank reason or blank code in vasp_circuit_break must be rejected.
    approved = contract_ui.approve_contract_interactive(
        ws, ai_draft_id, reader=lambda _p: '1', output=lambda _s: None,
    )
    cid = approved['contract']['contract_id']
    blank_reason = maker.call('vasp_circuit_break', {'contract_id': cid, 'reason': '   '})
    assert blank_reason['ok'] is False
    blank_code = maker.call('vasp_circuit_break', {'contract_id': cid, 'reason': 'valid', 'code': '   '})
    assert blank_code['ok'] is False


def test_remote_non_implicit_env_profile_revalidation_and_stage_param_s_toggle(tmp_path, monkeypatch):
    ws = _setup_workspace(tmp_path)
    # Register a fake remote server with two non-implicit profiles (one conflicts with cell relax).
    servers_dir = ws / 'servers'
    env_dir = servers_dir / 'environments'
    env_dir.mkdir(parents=True)
    (servers_dir / 'hpc.json').write_text(json.dumps({'host': 'hpc.example', 'user': 'u'}), encoding='utf-8')
    (servers_dir / 'hpc.compute.json').write_text(json.dumps({'vasp_env': '/opt/default.sh'}), encoding='utf-8')
    profiles_doc = {
        'schema': 1,
        'server': 'hpc',
        'profiles': [
            {
                'name': 'std6',
                'env_script': '/opt/std6.sh',
                'environment_sha256': 'a' * 64,
                'executable_sha256': 'b' * 64,
                'capabilities': ['vasp6'],
                'version': '6.4.3',
            },
            {
                'name': 'alt6',
                'env_script': '/opt/alt6.sh',
                'environment_sha256': 'c' * 64,
                'executable_sha256': 'd' * 64,
                'capabilities': ['vasp6'],
                'version': '6.4.3',
            },
        ],
    }
    (env_dir / 'hpc.profiles.json').write_text(json.dumps(profiles_doc), encoding='utf-8', newline='\n')

    # Mock remote preview_only prepare so no real SSH runs.
    from vasp_agent.chat import release_check
    from vasp_agent.remote import remote_compute

    monkeypatch.setattr(release_check, 'ensure_current', lambda _box, _server: None)

    class _FakeRemote:
        def __init__(self, _workspace, _server):
            pass

        def prepare(self, *_a, **_kw):
            from vasp_agent.workflow import parameter_specs

            return {
                'signature': 'remote-sig-1',
                'potential_floor': 300.0,
                'defaults': {'encuts': [400, 450, 500], 'meshes': [[4, 4, 4], [6, 6, 6], [8, 8, 8]]},
                'base_config': {'dimensionality': {'dimension': '3D'}},
                'stage_specs': {'dos': parameter_specs.describe(parameter_specs.for_stage('dos'), {'dimension': '3D'})},
            }

    monkeypatch.setattr(remote_compute, 'RemoteCompute', _FakeRemote)

    maker = ControlService(ws, role='maker')
    remote_env = _local_envelope(['convergence', 'relaxation', 'scf', 'bands', 'dos'])
    draft_res = maker.call(
        'vasp_draft_contract',
        _draft_args(
            server='hpc',
            job_name='si-rem-env',
            optional_stages=['dos'],
            stage_parameters=[{'stage': 'dos', 'field': 'sampling_interval_eV', 'value': 0.005}],
            resource_envelope=remote_env,
            rationale='远端 DOS 采样间隔加密测试；待人工确认。',
        ),
    )
    assert draft_res['ok'] is True, draft_res
    draft_id = draft_res['result']['draft_id']
    plan_id = draft_res['result']['plan_id']

    # Toggle `s` (back to spec default 0.01) and pick `e` -> `2` (`std6`, index 2 after implicit default) -> `1`.
    monkeypatch.setattr(task_contract, 'has_real_tty', lambda stream=None: True)
    answers = iter(['s', 'e', '2', '1'])
    approved = contract_ui.approve_contract_interactive(
        ws, draft_id, reader=lambda _p: next(answers), output=lambda _s: None,
    )
    assert approved['contract']['scope']['environment_profile'] == {
        'name': 'std6',
        'environment_sha256': 'a' * 64,
        'executable_sha256': 'b' * 64,
    }
    settled_plan = json.loads(parameter_plans.path(maker.box, plan_id).read_text(encoding='utf-8'))
    assert settled_plan['stage_parameters']['dos']['choice'] == 'operator_spec_default'
    assert settled_plan['stage_parameters']['dos']['selected']['sampling_interval_eV'] == 0.01

    # Now draft another remote job, then mutate std6 on disk to conflict with relax_mode=cell -> approve must refuse!
    draft2_res = maker.call(
        'vasp_draft_contract',
        _draft_args(server='hpc', job_name='si-rem-cap', encuts=None, meshes=None, rationale='', resource_envelope=_local_envelope()),
    )
    assert draft2_res['ok'] is True, draft2_res
    draft2_id = draft2_res['result']['draft_id']
    profiles_doc['profiles'][0]['capabilities'] = ['patch:fixz']
    (env_dir / 'hpc.profiles.json').write_text(json.dumps(profiles_doc), encoding='utf-8', newline='\n')
    answers2 = iter(['e', '2', '1'])
    with pytest.raises(task_contract.ContractError) as exc:
        contract_ui.approve_contract_interactive(
            ws, draft2_id, reader=lambda _p: next(answers2), output=lambda _s: None,
        )
    assert exc.value.error_code == 'ENVIRONMENT_CAPABILITY_MISSING'

    # Calling vasp_draft_contract against an already-prepared identical plan is rejected immediately.
    prep_plan_file = parameter_plans.path(maker.box, draft2_res['result']['plan_id'])
    prep_doc = json.loads(prep_plan_file.read_text(encoding='utf-8'))
    prep_doc['status'] = 'prepared'
    prep_plan_file.write_text(json.dumps(prep_doc, ensure_ascii=False, indent=2) + '\n', encoding='utf-8', newline='\n')
    already_prep = maker.call(
        'vasp_draft_contract',
        _draft_args(server='hpc', job_name='si-rem-cap', encuts=None, meshes=None, rationale='', resource_envelope=_local_envelope()),
    )
    assert already_prep['ok'] is False
    assert already_prep['error_code'] == 'CONTRACT_PLAN_ALREADY_PREPARED'


# ------------------------------- 5. Layer 3 AST zero-upward-import guard

def test_layer3_contract_ui_has_zero_upward_imports():
    mod_path = Path(contract_ui.__file__)
    tree = ast.parse(mod_path.read_text(encoding='utf-8'))
    violations = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name.startswith(('vasp_agent.chat', 'vasp_agent.control')):
                    violations.append(alias.name)
        elif isinstance(node, ast.ImportFrom):
            mod = node.module or ''
            if mod.startswith(('vasp_agent.chat', 'vasp_agent.control')):
                violations.append(mod)
            elif node.level >= 2 and (mod in ('chat', 'control') or mod.startswith(('chat.', 'control.'))):
                violations.append('.' * node.level + mod)
    assert violations == []
