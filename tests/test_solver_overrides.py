"""P3a tests: Tier 3 solver_overrides, CONFIRMATION_VERSION=2, P3-C fixes, and remote version gates."""
import ast
import base64
import json
from pathlib import Path
from types import SimpleNamespace as NS

import numpy as np
import pytest
from pymatgen.core import Lattice, Structure
from pymatgen.io.vasp.inputs import Incar, Poscar

import vasp_agent.workflow.band_path as band_path
import vasp_agent.workflow.convergence_reuse as convergence_reuse
import vasp_agent.workflow.dos_stage as dos_stage
import vasp_agent.workflow.full_workflow as full
import vasp_agent.workflow.scan_config as scan_config
import vasp_agent.workflow.scf_reuse as scf_reuse
import vasp_agent.workflow.solver_overrides as solver_ov
import vasp_agent.workflow.stage_gate as gate
import vasp_agent.workflow.vasp_band_agent as core
from vasp_agent.remote import bundle, remote_worker, worker_prepare

ROOT = Path(__file__).resolve().parents[1]
VALID_CONTRACT_SHA = 'a' * 64


def _si_structure():
    return Structure(Lattice.from_parameters(3.84, 3.84, 3.84, 60, 60, 60), ['Si', 'Si'], [[0, 0, 0], [0.25, 0.25, 0.25]])


def _prepare_si_task(tmp_path, name='si', *, solver_overrides=None):
    poscar = tmp_path / 'Si.POSCAR'
    if not poscar.exists():
        Poscar(_si_structure()).write_file(poscar)
    workdir = tmp_path / 'runs' / name
    full.prepare_full(NS(
        structure=str(poscar), workdir=str(workdir), potcar=None,
        encut=520, mesh=[4, 4, 4], nbands=16, relax_mode='cell', ediff=1e-7,
        points=40, symprec=0.01, force_tol=0.01, stress_tol=1.0, energy_tol=0.001, nsw=200,
        solver_overrides=solver_overrides,
    ))
    (workdir / 'POTCAR').write_text('fake PBE POTCAR\n', encoding='utf-8')
    core.save(workdir / 'potential-selection.json', [{'element': 'Si', 'titel': 'PAW_PBE Si'}])
    manifest = json.loads((workdir / 'manifest.json').read_text(encoding='utf-8'))
    manifest['ready'] = True
    manifest['hashes']['POTCAR'] = core.digest(workdir / 'POTCAR')
    manifest['hashes']['potential-selection.json'] = core.digest(workdir / 'potential-selection.json')
    core.save(workdir / 'manifest.json', manifest)
    return workdir


def _fake_result(folder):
    structure = Poscar.from_file(folder / 'POSCAR').structure
    incar = Incar.from_file(folder / 'INCAR')
    return NS(
        final_structure=structure, final_energy=-10.0 * len(structure), converged_ionic=True,
        parameters={'NELM': int(incar.get('NELM', 150)), 'NBANDS': 16, 'NELECT': 8.0},
        efermi=0.0,
        ionic_steps=[{'forces': np.zeros((len(structure), 3)).tolist(),
                      'stress': np.zeros((3, 3)).tolist(), 'electronic_steps': [{}]}],
    )


def _fake_execute(root, name, command):
    folder = root / name
    stamp = folder / 'complete.json'
    if not stamp.exists():
        (folder / 'vasprun.xml').write_text(f'mock output {name}\n', encoding='utf-8')
        data = {'xml_sha256': core.digest(folder / 'vasprun.xml')}
        if name == 'scf':
            (folder / 'CHGCAR').write_text('mock charge density\n', encoding='utf-8')
            data['charge_sha256'] = core.digest(folder / 'CHGCAR')
        core.save(stamp, data)
    return _fake_result(folder)


def _checks_for(required):
    return [{'item': item, 'judgment': 'pending' if item == 'validation_plan' else 'pass',
             'blocking': False, 'evidence': 'ok', 'reason': 'ok', 'next_action': '无需'} for item in required]


def _run_to_phase(workdir, stop_after, monkeypatch):
    monkeypatch.setattr(
        full.Potcar, 'from_file',
        lambda p: [NS(element='Si', functional='PBE', enmax=245.345, ZVAL=4.0, TITEL='PAW_PBE Si')],
    )
    monkeypatch.setattr(core, 'execute', _fake_execute)
    monkeypatch.setattr(full, 'analyze_full', lambda root: core.save(
        root / 'workflow-state.json', {**json.loads((root / 'workflow-state.json').read_text(encoding='utf-8')), 'status': 'complete'}))
    while gate.current(workdir) != 'complete':
        ctx = gate.context(workdir)
        phase = ctx['phase']
        gate.record(workdir, ctx['snapshot'], phase, _checks_for(ctx['required_checks']), 'checker:test')
        full.run_full(workdir, ['mock'])
        if phase == stop_after:
            return ctx
    return None


# --- 1. Whitelist, strict types, bounds, stage constraints ---------------------

def test_all_nine_tier3_tags_validate_and_apply():
    raw = {
        'version': 1,
        'stages': {
            'convergence': {
                'tags': {'algo': 'Fast', 'amix': 0.2, 'bmix': 0.0001, 'nelm': 200, 'nelmin': 4, 'maxmix': 60, 'symprec': 1e-6},
                'reason': '电子步慢收敛，测试全部 7 个电子步求解器参数。',
            },
            'relaxation': {'tags': {'IBRION': 1, 'POTIM': 0.2, 'ALGO': 'All', 'NELM': 240}, 'reason': '弛豫切换 RMM-DIIS。'},
            'scf': {'tags': {'ALGO': 'Damped', 'BMIX': -0.0}, 'reason': '表面电荷震荡。'},
            'bands': {'tags': {'ALGO': 'VeryFast', 'MAXMIX': 40}, 'reason': '非自洽加速。'},
            'dos': {'tags': {'ALGO': 'All', 'SYMPREC': 1e-5}, 'reason': '对称性容差收紧。'},
        },
    }
    norm = solver_ov.validate(raw, allowed_phases=solver_ov.VALID_PHASES)
    assert norm['version'] == 1 and list(norm['stages']) == list(solver_ov.VALID_PHASES)
    assert str(norm['stages']['scf']['tags']['BMIX']) == '0.0'  # -0.0 normalized to +0.0

    cfg = {'solver_overrides': norm}
    r1 = solver_ov.apply_to_incar({'ENCUT': 520, 'IBRION': 2, 'NSW': 200, 'POTIM': 0.5, 'ALGO': 'Normal', 'NELM': 150},
                                  cfg, 'relaxation', 'relax-1')
    val_ref = solver_ov.apply_to_incar({'ENCUT': 520, 'IBRION': -1, 'NSW': 0, 'ALGO': 'Normal', 'NELM': 150},
                                       cfg, 'relaxation', 'validation/reference')
    assert r1['IBRION'] == 1 and r1['POTIM'] == 0.2 and r1['ALGO'] == 'All' and r1['NELM'] == 240
    assert val_ref['IBRION'] == -1 and val_ref['NSW'] == 0 and 'POTIM' not in val_ref and val_ref['ALGO'] == 'All'
    # Empty dict in config['solver_overrides'] must be rejected, not silently ignored.
    with pytest.raises(ValueError, match='stages'):
        solver_ov.apply_to_incar({'ENCUT': 520}, {'solver_overrides': {}}, 'scf', 'scf')
    with pytest.raises(ValueError, match='stages'):
        solver_ov.diff_for_phase({'solver_overrides': {}}, 'scf')


@pytest.mark.parametrize('tags, match', [
    ({'NELM': True}, 'NELM'), ({'NELM': False}, 'NELM'), ({'NELM': 301}, 'NELM'),
    ({'NELM': 39}, 'NELM'), ({'NELM': 150.0}, 'NELM'), ({'NELMIN': True}, 'NELMIN'),
    ({'NELMIN': 1}, 'NELMIN'), ({'NELMIN': 13}, 'NELMIN'), ({'MAXMIX': True}, 'MAXMIX'),
    ({'MAXMIX': 19}, 'MAXMIX'), ({'MAXMIX': 81}, 'MAXMIX'), ({'AMIX': True}, 'AMIX'),
    ({'AMIX': 0}, 'AMIX'), ({'AMIX': -0.1}, 'AMIX'), ({'AMIX': 1.5}, 'AMIX'),
    ({'AMIX': float('nan')}, 'AMIX'), ({'AMIX': float('inf')}, 'AMIX'), ({'BMIX': -0.01}, 'BMIX'),
    ({'BMIX': 10.1}, 'BMIX'), ({'BMIX': float('nan')}, 'BMIX'), ({'SYMPREC': 1e-2}, 'SYMPREC'),
    ({'SYMPREC': 1e-9}, 'SYMPREC'), ({'SYMPREC': False}, 'SYMPREC'), ({'ALGO': 'Exact'}, 'ALGO'),
    ({'ALGO': 1}, 'ALGO'), ({'ALGO': 'Normal'}, '默认基线'), ({'NELM': 150}, '默认基线'),
])
def test_invalid_electronic_types_bounds_and_noops_rejected(tags, match):
    with pytest.raises(ValueError, match=match):
        solver_ov.validate({'version': 1, 'stages': {'scf': {'tags': tags, 'reason': '测试非法输入'}}})


@pytest.mark.parametrize('tags, match', [
    ({'IBRION': True}, 'IBRION'), ({'IBRION': 0}, 'IBRION'), ({'IBRION': 4}, 'IBRION'),
    ({'IBRION': -1}, 'IBRION'), ({'IBRION': 2}, '默认基线'), ({'POTIM': True}, 'POTIM'),
    ({'POTIM': 0}, 'POTIM'), ({'POTIM': -0.5}, 'POTIM'), ({'POTIM': 2.5}, 'POTIM'),
    ({'POTIM': float('inf')}, 'POTIM'), ({'POTIM': 0.5}, '默认基线'),
])
def test_invalid_ionic_types_bounds_and_noops_rejected(tags, match):
    with pytest.raises(ValueError, match=match):
        solver_ov.validate({'version': 1, 'stages': {'relaxation': {'tags': tags, 'reason': '测试非法离子步参数'}}})


def test_nelmin_gt_effective_nelm_explicit_guard(monkeypatch):
    monkeypatch.setattr(solver_ov, 'MIN_NELM', 4)
    with pytest.raises(ValueError, match='NELMIN.*NELM'):
        solver_ov.validate({'version': 1, 'stages': {'scf': {'tags': {'NELM': 6, 'NELMIN': 8}, 'reason': '交叉下限'}}})


@pytest.mark.parametrize('tag, val', [
    ('ISPIN', 2), ('ISMEAR', -5), ('SIGMA', 0.2), ('NSW', 500), ('EDIFF', 1e-3),
    ('EDIFFG', -0.05), ('LHFCALC', True), ('LDAU', True), ('KPAR', 4), ('NCORE', 2),
    ('ICHARG', 2), ('LCHARG', False), ('ENCUT', 400), ('LORBIT', 11), ('NEDOS', 3001),
    ('GGA', 'PS'), ('PREC', 'Normal'),
])
def test_forbidden_physics_and_execution_tags_hard_rejected(tag, val):
    with pytest.raises(ValueError, match='不允许通过 solver_overrides'):
        solver_ov.validate({'version': 1, 'stages': {'scf': {'tags': {tag: val}, 'reason': '越权修改物理参数'}}})


@pytest.mark.parametrize('phase', ['convergence', 'scf', 'bands', 'dos'])
@pytest.mark.parametrize('tags', [{'IBRION': 1}, {'POTIM': 0.2}])
def test_ionic_tags_rejected_outside_relaxation(phase, tags):
    with pytest.raises(ValueError, match='仅允许用于 relaxation'):
        solver_ov.validate({'version': 1, 'stages': {phase: {'tags': tags, 'reason': '非法阶段离子标签'}}},
                           allowed_phases=solver_ov.VALID_PHASES)


def test_reason_and_schema_shape_validation():
    for bad_reason in ('', '   ', 'a' * 501, 'line1\nline2', 'tab\there', 123):
        with pytest.raises(ValueError, match='reason'):
            solver_ov.validate({'version': 1, 'stages': {'scf': {'tags': {'ALGO': 'All'}, 'reason': bad_reason}}})
    with pytest.raises(ValueError, match='未在当前任务启用'):
        solver_ov.validate({'version': 1, 'stages': {'dos': {'tags': {'ALGO': 'All'}, 'reason': 'ok'}}},
                           allowed_phases=('convergence', 'relaxation', 'scf', 'bands'))
    with pytest.raises(ValueError, match='未允许字段'):
        solver_ov.validate({'version': 1, 'stages': {'scf': {'tags': {'ALGO': 'All'}, 'reason': 'ok'}}, 'extra': 1})
    with pytest.raises(ValueError, match='version'):
        solver_ov.validate({'version': 2, 'stages': {'scf': {'tags': {'ALGO': 'All'}, 'reason': 'ok'}}})
    with pytest.raises(ValueError, match='max_nelm|NELM'):
        solver_ov.validate({'version': 1, 'stages': {'scf': {'tags': {'NELM': 220}, 'reason': 'ok'}}}, max_nelm=200)


# --- 2. End-to-end preparation, INCAR/symprec isolation, scf_reuse, forks -----

def test_prepare_run_incar_solver_diff_and_symprec_isolation(tmp_path, monkeypatch):
    overrides = {
        'version': 1,
        'stages': {
            'convergence': {'tags': {'ALGO': 'Fast', 'SYMPREC': 1e-6, 'AMIX': 0.2}, 'reason': '收敛阶段加速并微调对称容差。'},
            'relaxation': {'tags': {'IBRION': 1, 'POTIM': 0.2, 'ALGO': 'All', 'NELM': 200}, 'reason': '弛豫切换 RMM-DIIS。'},
            'scf': {'tags': {'ALGO': 'All', 'AMIX': 0.2, 'BMIX': 0.0001, 'NELMIN': 4, 'MAXMIX': 60}, 'reason': 'SCF 线性混合稳态。'},
            'bands': {'tags': {'ALGO': 'Damped'}, 'reason': '能带稳健求解。'},
        },
    }
    workdir = _prepare_si_task(tmp_path, 'si-ov', solver_overrides=overrides)
    cfg = json.loads((workdir / 'config.json').read_text(encoding='utf-8'))
    plan = json.loads((workdir / 'input-plan.json').read_text(encoding='utf-8'))
    assert cfg['symprec'] == 0.01 and plan['solver_overrides'] == cfg['solver_overrides']

    conv_incar = Incar.from_file(workdir / 'convergence/encut-0/INCAR')
    assert conv_incar['ALGO'] == 'Fast' and conv_incar['SYMPREC'] == pytest.approx(1e-6) and conv_incar['AMIX'] == pytest.approx(0.2)
    conv_text = (workdir / 'convergence/encut-0/INCAR').read_text(encoding='utf-8')
    assert '收敛阶段加速' not in conv_text and 'solver override:' in conv_text

    assert plan['incars']['relax-1']['IBRION'] == 1 and plan['incars']['relax-1']['POTIM'] == 0.2
    for vname in ('validation/reference', 'validation/higher-cutoff', 'validation/denser-mesh'):
        assert plan['incars'][vname]['IBRION'] == -1 and plan['incars'][vname]['NSW'] == 0
        assert 'POTIM' not in plan['incars'][vname] and plan['incars'][vname]['ALGO'] == 'All'

    gate.confirm_physics(
        workdir, 'Si 3D nonmagnetic PBE with Tier 3 overrides',
        channel='operator_task_contract', contract_id='ctr-20261127-abcd1234', contract_sha256=VALID_CONTRACT_SHA,
    )
    conv_ctx = gate.context(workdir)
    assert conv_ctx['incar_solver_diff']['phase'] == 'convergence'
    assert {c['tag'] for c in conv_ctx['incar_solver_diff']['changes']} == {'ALGO', 'AMIX', 'SYMPREC'}

    _run_to_phase(workdir, 'scf', monkeypatch)
    assert scf_reuse.verified_source(workdir)['config']['solver_overrides'] == cfg['solver_overrides']
    assert gate.context(workdir)['incar_solver_diff']['changes'] == [
        {'tag': 'ALGO', 'baseline': 'Normal', 'override': 'Damped', 'applies_to': ['bands']}]

    bad_fork = tmp_path / 'runs/si-ov-bad-bands'
    with pytest.raises(ValueError, match='禁止修改已验收上游阶段 scf'):
        band_path.prepare_bands_path(
            workdir, bad_fork, 'GAMMA-X-L-GAMMA',
            solver_overrides={'version': 1, 'stages': {'scf': {'tags': {'ALGO': 'Fast'}, 'reason': '非法改上游'}}},
        )
    assert not bad_fork.exists()

    ok_fork = tmp_path / 'runs/si-ov-ok-bands'
    band_path.prepare_bands_path(
        workdir, ok_fork, 'GAMMA-X-L-GAMMA',
        solver_overrides={'version': 1, 'stages': {'bands': {'tags': {'ALGO': 'All', 'NELM': 220}, 'reason': '新路径放宽电子步'}}},
    )
    fork_cfg = json.loads((ok_fork / 'config.json').read_text(encoding='utf-8'))
    assert fork_cfg['solver_overrides']['stages']['scf'] == cfg['solver_overrides']['stages']['scf']
    assert fork_cfg['solver_overrides']['stages']['bands']['tags'] == {'ALGO': 'All', 'NELM': 220}
    _run_to_phase(ok_fork, 'bands', monkeypatch)
    assert gate.current(ok_fork) == 'complete'

    # Cross-fork regression: A(bands override) -> DOS fork -> bands_path_fork(None) must run cleanly.
    monkeypatch.setattr(
        dos_stage, 'scf_bands',
        lambda root: {'band_minima_eV': [-12.0, -8.0, -4.0, -1.0, 1.0, 5.0, 10.0, 15.0] * 2,
                      'lowest_eigenvalue_eV': -12.0, 'kpoints': 8},
    )
    dos_fork = tmp_path / 'runs/si-ov-dos-mid'
    dos_stage.prepare_dos_fork(workdir, dos_fork, dos_stage.fork_preview(workdir)['defaults'])
    assert 'bands' not in json.loads((dos_fork / 'input-plan.json').read_text(encoding='utf-8'))['incars']
    bands_after_dos = tmp_path / 'runs/si-ov-bands-after-dos'
    band_path.prepare_bands_path(dos_fork, bands_after_dos, 'GAMMA-X-L-GAMMA')
    assert json.loads((bands_after_dos / 'input-plan.json').read_text(encoding='utf-8'))['incars']['bands']['ALGO'] == 'Normal'
    _run_to_phase(bands_after_dos, 'bands', monkeypatch)
    assert gate.current(bands_after_dos) == 'complete'


def test_prepare_retry_production_and_dos_fork_override_boundaries(tmp_path, monkeypatch):
    monkeypatch.setattr(core, 'read_result', _fake_result)
    initial_ov = {
        'version': 1,
        'stages': {
            'convergence': {'tags': {'ALGO': 'Fast'}, 'reason': '快速收敛扫描'},
            'bands': {'tags': {'ALGO': 'Damped'}, 'reason': '预设能带求解器'},
        },
    }
    workdir = _prepare_si_task(tmp_path, 'si-prod-src', solver_overrides=initial_ov)
    gate.confirm_physics(workdir, 'Si 3D nonmagnetic PBE')
    _run_to_phase(workdir, 'convergence', monkeypatch)
    rel_ctx = gate.context(workdir)
    assert 'incar_solver_diff' not in rel_ctx

    # prepare_retry(mode='both') merges new convergence override while preserving downstream bands override.
    retry_both = tmp_path / 'runs/si-retry-both'
    convergence_reuse.prepare_retry(
        workdir, retry_both, 'both', encuts=[560.0, 620.0, 700.0], meshes=[[5, 5, 5], [6, 6, 6], [7, 7, 7]],
        solver_overrides={'version': 1, 'stages': {'convergence': {'tags': {'ALGO': 'All', 'AMIX': 0.2}, 'reason': '重试加稳电子步'}}},
    )
    retry_cfg = json.loads((retry_both / 'config.json').read_text(encoding='utf-8'))
    assert retry_cfg['solver_overrides']['stages']['convergence']['tags'] == {'ALGO': 'All', 'AMIX': 0.2}
    assert retry_cfg['solver_overrides']['stages']['bands']['tags'] == {'ALGO': 'Damped'}
    assert Incar.from_file(retry_both / 'convergence/encut-0/INCAR')['AMIX'] == pytest.approx(0.2)

    # prepare_retry(mode='mesh') reuses accepted encut scan and must refuse mutating convergence overrides.
    retry_partial_bad = tmp_path / 'runs/si-retry-partial-bad'
    with pytest.raises(ValueError, match='禁止修改已验收上游阶段 convergence'):
        convergence_reuse.prepare_retry(
            workdir, retry_partial_bad, 'mesh', meshes=[[5, 5, 5], [6, 6, 6], [7, 7, 7]],
            solver_overrides={'version': 1, 'stages': {'convergence': {'tags': {'ALGO': 'All'}, 'reason': '非法改复用阶段'}}},
        )
    assert not retry_partial_bad.exists()

    # prepare_production refuses modifying accepted convergence before mkdir.
    bad_prod = tmp_path / 'runs/si-prod-bad'
    with pytest.raises(ValueError, match='禁止修改已验收上游阶段 convergence'):
        convergence_reuse.prepare_production(
            workdir, bad_prod, [4, 4, 4], rel_ctx['snapshot'],
            solver_overrides={'version': 1, 'stages': {'convergence': {'tags': {'ALGO': 'All'}, 'reason': '非法改上游'}}},
        )
    assert not bad_prod.exists()

    ok_prod = tmp_path / 'runs/si-prod-ok'
    convergence_reuse.prepare_production(
        workdir, ok_prod, [4, 4, 4], rel_ctx['snapshot'],
        solver_overrides={'version': 1, 'stages': {'relaxation': {'tags': {'IBRION': 3, 'POTIM': 0.1}, 'reason': '阻尼弛豫'}}},
    )
    prod_ctx = gate.context(ok_prod)
    assert all(c['applies_to'] == ['relax-1', 'relax-2'] for c in prod_ctx['incar_solver_diff']['changes'])

    _run_to_phase(ok_prod, 'scf', monkeypatch)
    monkeypatch.setattr(
        dos_stage, 'scf_bands',
        lambda root: {'band_minima_eV': [-12.0, -8.0, -4.0, -1.0, 1.0, 5.0, 10.0, 15.0] * 2,
                      'lowest_eigenvalue_eV': -12.0, 'kpoints': 8},
    )
    dos_values = dos_stage.fork_preview(ok_prod)['defaults']
    bad_dos = tmp_path / 'runs/si-dos-bad'
    with pytest.raises(ValueError, match='禁止修改已验收上游阶段 scf'):
        dos_stage.prepare_dos_fork(
            ok_prod, bad_dos, dos_values,
            solver_overrides={'version': 1, 'stages': {'scf': {'tags': {'ALGO': 'All'}, 'reason': '非法改上游'}}},
        )
    assert not bad_dos.exists()

    ok_dos = tmp_path / 'runs/si-dos-ok'
    dos_stage.prepare_dos_fork(
        ok_prod, ok_dos, dos_values,
        solver_overrides={'version': 1, 'stages': {'dos': {'tags': {'ALGO': 'All', 'NELM': 210}, 'reason': 'DOS 收敛稳健'}}},
    )
    dos_ctx = gate.context(ok_dos)
    assert dos_ctx['dos_plan']['incar']['ALGO'] == 'All' and dos_ctx['dos_plan']['incar']['NELM'] == 210
    assert dos_ctx['dos_plan']['incar']['LORBIT'] == 11 and dos_ctx['incar_solver_diff']['phase'] == 'dos'


def test_no_overrides_keeps_context_and_notes_clean(tmp_path):
    workdir = _prepare_si_task(tmp_path, 'si-plain')
    gate.confirm_physics(workdir, 'Si 3D plain')
    ctx = gate.context(workdir)
    assert 'incar_solver_diff' not in ctx and 'solver_overrides' not in ctx['inputs']['config']
    assert 'solver_overrides' not in ctx['inputs']['plan']
    assert 'solver_overrides' not in (workdir / 'PARAMETERS.md').read_text(encoding='utf-8')
    assert 'contract_id' not in ctx['physics'] and 'contract_sha256' not in ctx['physics']


# --- 3. P3-B: CONFIRMATION_VERSION = 2 & operator_task_contract ----------------

def test_confirmation_version_2_contract_channel_and_read_write_symmetry(tmp_path):
    assert gate.CONFIRMATION_VERSION == 2 and 'operator_task_contract' in gate.CONFIRMATION_CHANNELS
    workdir = _prepare_si_task(tmp_path, 'si-ctr')
    with pytest.raises(ValueError, match='contract_id'):
        gate.confirm_physics(workdir, 'goal', 'operator_task_contract', contract_id='bad\nid', contract_sha256=VALID_CONTRACT_SHA)
    with pytest.raises(ValueError, match='contract_sha256'):
        gate.confirm_physics(workdir, 'goal', 'operator_task_contract', contract_id='ctr-1', contract_sha256='NOTHEX')
    with pytest.raises(ValueError, match='仅 operator_task_contract'):
        gate.confirm_physics(workdir, 'goal', 'operator_slash_command', contract_id='ctr-1', contract_sha256=VALID_CONTRACT_SHA)

    saved = gate.confirm_physics(
        workdir, 'Si 3D contract goal', 'operator_task_contract',
        contract_id='ctr-20261127-12345678', contract_sha256=VALID_CONTRACT_SHA,
    )
    assert saved['confirmed_by'] == 'operator_task_contract' and gate.physics(workdir)['contract_sha256'] == VALID_CONTRACT_SHA

    # Read-side symmetry: control chars, >128 chars, bad sha256, or forged contract fields on slash channel all rejected.
    raw = json.loads((workdir / 'physics-model.json').read_text(encoding='utf-8'))
    for mutated, match in [
        ({**raw, 'contract_sha256': 'bad'}, 'contract_sha256'),
        ({**raw, 'contract_id': 'x' * 129}, 'contract_id'),
        ({**raw, 'contract_id': 'bad\nid'}, 'contract_id'),
        ({**raw, 'confirmed_by': 'operator_slash_command'}, '仅 operator_task_contract'),
    ]:
        core.save(workdir / 'physics-model.json', mutated)
        with pytest.raises(ValueError, match=match):
            gate.physics(workdir)


# --- 4. P3-C micro-fixes & Layer 2 remote version gates -----------------------

def test_p3c_scan_config_posix_and_band_path_runtime_error(tmp_path, monkeypatch):
    workdir = _prepare_si_task(tmp_path, 'si-p3c')
    verified = scan_config.verify_scan_inputs(workdir)
    assert verified['matched'] is True and all('\\' not in item for item in verified['checked'])
    assert 'convergence/encut-0' in verified['checked']

    import pymatgen.symmetry.bandstructure as bs_mod
    real_hsk = bs_mod.HighSymmKpath

    def fake_hsk(struct, symprec=0.01, path_type='setyawan_curtarolo', **kwargs):
        if path_type == 'hinuma':
            raise RuntimeError('seekpath is required for hinuma')
        return real_hsk(struct, symprec=symprec, path_type=path_type, **kwargs)

    monkeypatch.setattr(bs_mod, 'HighSymmKpath', fake_hsk)
    s = _si_structure()
    with pytest.raises(ValueError, match='seekpath'):
        band_path.tables('hinuma', s, s, 0.01)


def test_remote_worker_version_gates_and_stdlib_purity(tmp_path, monkeypatch):
    assert 'solver_overrides' in remote_worker.ENGINE_MODULES and full.SOLVER_OVERRIDES_VERSION == 1
    assert {'vasp_agent/workflow/solver_overrides.py', 'solver_overrides.py'} <= set(bundle.engine_sources())

    old_full = NS(SCAN_PLAN_VERSION=1, SOLVER_OVERRIDES_VERSION=0, run_stages=True)
    with pytest.raises(ValueError, match='remote_deploy'):
        worker_prepare._require_solver_overrides_engine(
            old_full, {'version': 1, 'stages': {'scf': {'tags': {'ALGO': 'All'}, 'reason': 'x'}}})
    worker_prepare._require_solver_overrides_engine(old_full, None)

    env_script = tmp_path / 'vasp.env'
    env_script.write_text('export PATH=$PATH\n', encoding='utf-8')
    rel_dir = tmp_path / 'releases/old'
    rel_dir.mkdir(parents=True)
    (rel_dir / 'convergence_reuse.py').write_text('# stub\n', encoding='utf-8')
    (tmp_path / 'deployment.json').write_text(json.dumps({'release': str(rel_dir), 'version': 'old'}), encoding='utf-8')
    monkeypatch.setattr(worker_prepare, 'task_environment', lambda cfg, prof, rm: (cfg, {'executable_sha256': '0' * 64}))
    monkeypatch.setattr(worker_prepare, 'activate_release', lambda r: r)
    monkeypatch.setattr(worker_prepare, 'validate_scheduler', lambda cfg: None)
    monkeypatch.setattr(worker_prepare, 'engine_module', lambda name: old_full if name == 'full_workflow' else NS())

    cfg = {'vasp_env': str(env_script), 'potcar_library': str(tmp_path), 'ntasks': 8, 'partition': 'p', 'walltime': '01:00:00', 'python': 'py'}
    payload = {
        'structure': base64.b64encode(str(Poscar(_si_structure())).encode('utf-8')).decode('ascii'),
        'parameters': {'encut': 520, 'mesh': [4, 4, 4], 'nbands': 16, 'relax_mode': 'cell',
                       'solver_overrides': {'version': 1, 'stages': {'scf': {'tags': {'ALGO': 'All'}, 'reason': 't'}}}},
    }
    with pytest.raises(ValueError, match='remote_deploy'):
        worker_prepare._prepare_task(payload, cfg, tmp_path, 'job1', tmp_path / 'jobs/job1', preview_only=False)

    # Read-only _inspect_convergence refuses old engine on a task that has solver_overrides.
    src_job = tmp_path / 'jobs/ov-job'
    src_job.mkdir(parents=True)
    (src_job / 'manifest.json').write_text('{}', encoding='utf-8')
    (src_job / 'config.json').write_text(json.dumps({'solver_overrides': {'version': 1, 'stages': {'scf': {'tags': {'ALGO': 'All'}, 'reason': 't'}}}}), encoding='utf-8')
    with pytest.raises(ValueError, match='remote_deploy'):
        worker_prepare._inspect_convergence({}, cfg, tmp_path, 'ov-job', src_job)

    # _confirm_physics refuses old CONFIRMATION_VERSION < 2 AND refuses contract metadata on legacy channels.
    old_gate = NS(CONFIRMATION_VERSION=1, CONFIRMATION_CHANNELS=('operator_slash_command', 'operator_chat_menu'))
    monkeypatch.setattr(remote_worker, '_stage_gate', lambda folder: old_gate)
    with pytest.raises(ValueError, match='remote_deploy'):
        remote_worker._confirm_physics(
            {'goal': 'g', 'channel': 'operator_task_contract', 'contract_id': 'c1', 'contract_sha256': VALID_CONTRACT_SHA},
            cfg, src_job,
        )
    with pytest.raises(ValueError, match='仅 operator_task_contract'):
        remote_worker._confirm_physics(
            {'goal': 'g', 'channel': 'operator_slash_command', 'contract_id': 'c1', 'contract_sha256': VALID_CONTRACT_SHA},
            cfg, src_job,
        )

    tree = ast.parse((ROOT / 'src/vasp_agent/workflow/solver_overrides.py').read_text(encoding='utf-8'))
    imports = [a.name for n in ast.walk(tree) if isinstance(n, ast.Import) for a in n.names] + [
        n.module for n in ast.walk(tree) if isinstance(n, ast.ImportFrom) and n.module]
    assert all(not name.startswith('vasp_agent') for name in imports), imports
