import json
from types import SimpleNamespace

import numpy as np
import pytest
from pymatgen.core import Structure, Lattice
from pymatgen.io.vasp.inputs import Kpoints, Poscar

import vasp_agent.workflow.full_workflow as full
import vasp_agent.workflow.vasp_band_agent as core


def config():
    return dict(encuts=[520, 598, 676], meshes=[[4, 4, 4], [5, 5, 5], [6, 6, 6]],
                ediff=1e-7, points=10, symprec=0.01, nbands=16, relax_mode='cell',
                force_tol=0.01, stress_tol=1., energy_tol=0.001, nsw=200)


def result(structure=None, force=0., stress=0., energy=-10., converged=True):
    structure = structure or Structure(Lattice.cubic(5.43), ['Si'], [[0, 0, 0]])
    return SimpleNamespace(final_structure=structure, final_energy=energy * len(structure),
        converged_ionic=converged, parameters={'NELM': 150},
        ionic_steps=[{'forces': [[force, 0, 0]] * len(structure),
                      'stress': np.eye(3) * stress, 'electronic_steps': [{}] * 5}])


@pytest.mark.parametrize('kwargs', [{'force': .02}, {'stress': 2.}, {'converged': False}])
def test_relaxation_rejects_bad_results(kwargs):
    with pytest.raises(ValueError):
        full.check_relaxation(result(**kwargs), config())


def test_fixed_cell_does_not_require_zero_stress():
    cfg = {**config(), 'relax_mode': 'ions'}
    full.check_relaxation(result(stress=10.), cfg)


def test_energy_alone_cannot_pass_convergence():
    assert not full.compare(full.metrics(result()), full.metrics(result(force=.02)), config())['passed']


@pytest.mark.parametrize('parallel', [1, 3])
def test_full_order_and_relaxed_geometry(tmp_path, monkeypatch, parallel):
    cfg = config()
    cfg.update(parallel_tests=parallel, cores_per_test=8)
    monkeypatch.setenv('SLURM_JOB_ID', 'test')
    monkeypatch.setenv('SLURM_NTASKS', '24')
    core.save(tmp_path / 'config.json', cfg)
    initial = result().final_structure
    optimized = Structure(Lattice.cubic(5.5), ['Si'], [[0, 0, 0]])
    Poscar(initial).write_file(tmp_path / 'initial.POSCAR')
    from vasp_agent.workflow.input_review import prepare_plan
    prepare_plan(tmp_path, cfg, initial, {'nbands': cfg['nbands']})
    planned = json.loads((tmp_path / 'input-plan.json').read_text(encoding='utf-8'))['incars']
    monkeypatch.setattr(core, 'verify', lambda root: {})
    monkeypatch.setattr(full.Potcar, 'from_file', lambda path: [SimpleNamespace(element='Si')])
    seen = []
    def fake_stage(root, name, structure, incar, kpoints, command, charge=None):
        seen.append(name)
        assert incar == planned[name]
        if parallel > 1:
            assert command == ['srun', '--exclusive', '--exact', '--nodes=1', '--ntasks=8', '--cpus-per-task=1', 'vasp_std']
        if name == 'relax-2' or name.startswith('validation/') or name == 'scf':
            assert structure.volume == pytest.approx(optimized.volume)
        return result(optimized if name.startswith('relax') else structure)
    monkeypatch.setattr(full, 'stage', fake_stage)
    monkeypatch.setattr(full, 'analyze_full', lambda root: None)
    full.run_full(tmp_path, ['srun', 'vasp_std'] if parallel > 1 else ['fake-vasp'])
    assert sorted(seen[:6]) == ['convergence/encut-0', 'convergence/encut-1', 'convergence/encut-2',
                    'convergence/mesh-0', 'convergence/mesh-1', 'convergence/mesh-2']
    assert seen[6:8] == ['relax-1', 'relax-2']
    assert sorted(seen[8:11]) == ['validation/denser-mesh', 'validation/higher-cutoff', 'validation/reference']
    assert seen[11:] == ['scf', 'bands']


def prepared(tmp_path, monkeypatch):
    """A root with a real reviewed plan, no manifest, and a stub executor."""
    from vasp_agent.workflow.input_review import prepare_plan
    cfg = config()
    initial = result().final_structure
    core.save(tmp_path / 'config.json', cfg)
    Poscar(initial).write_file(tmp_path / 'initial.POSCAR')
    (tmp_path / 'POTCAR').write_text('Si\n')
    prepare_plan(tmp_path, cfg, initial, {'nbands': cfg['nbands']})
    monkeypatch.setattr(core, 'execute', lambda root, name, command: result())
    plan = json.loads((tmp_path / 'input-plan.json').read_text(encoding='utf-8'))
    return cfg, initial, plan


@pytest.mark.parametrize('stage_name', ['relax-1', 'relax-2', 'validation/reference',
                                        'validation/higher-cutoff', 'validation/denser-mesh'])
def test_reviewed_plan_registers_the_grid_each_stage_will_use(tmp_path, monkeypatch, stage_name):
    """The plan must record the sampling, not only the INCAR.

    Nothing compared KPOINTS against the reviewed plan before: stage() checked the
    plan's INCAR and then compared KPOINTS only against whatever file already sat in
    the stage folder. A relaxation or validation stage could therefore run on a grid
    the operator never saw, under a plan they had approved.
    """
    cfg, initial, plan = prepared(tmp_path, monkeypatch)
    assert set(plan['kpoints']) == {'relax-1', 'relax-2', 'validation/reference',
                                    'validation/higher-cutoff', 'validation/denser-mesh'}
    assert Kpoints.from_str(plan['kpoints'][stage_name]).kpts[0] == tuple(
        full.relaxation_grids(cfg)[stage_name])
    full.stage(tmp_path, stage_name, initial, plan['incars'][stage_name],
               Kpoints.from_str(plan['kpoints'][stage_name]), ['fake-vasp'])
    assert (tmp_path / stage_name / 'KPOINTS').exists()


@pytest.mark.parametrize('stage_name', ['relax-1', 'validation/denser-mesh'])
def test_kpoints_deviating_from_reviewed_plan_refused(tmp_path, monkeypatch, stage_name):
    cfg, initial, plan = prepared(tmp_path, monkeypatch)
    # Exactly the INCAR that was reviewed, so only the sampling is off -- an
    # unapproved grid must not ride in behind an approved INCAR.
    deviating = Kpoints.gamma_automatic([n + 1 for n in full.relaxation_grids(cfg)[stage_name]])
    with pytest.raises(ValueError, match='KPOINTS 偏离已审阅的方案'):
        full.stage(tmp_path, stage_name, initial, plan['incars'][stage_name], deviating, ['fake-vasp'])
    assert not (tmp_path / stage_name / 'INCAR').exists()


def test_plan_without_recorded_kpoints_stays_permissive(tmp_path, monkeypatch):
    """A plan predating the recording must not refuse a stage it never specified."""
    cfg, initial, plan = prepared(tmp_path, monkeypatch)
    plan.pop('kpoints')
    core.save(tmp_path / 'input-plan.json', plan)
    full.stage(tmp_path, 'relax-1', initial, plan['incars']['relax-1'],
               Kpoints.gamma_automatic([9, 9, 9]), ['fake-vasp'])


def test_derived_stages_are_not_pre_registered(tmp_path, monkeypatch):
    """scf and bands grids follow from the optimized cell, so no value is invented.

    Their sampling is instead covered by the phase evidence: scf-summary.json
    carries the mesh read back by both the SCF and bands reviews, and bands/KPOINTS
    is hashed into the evidence those reviews verify.
    """
    cfg, initial, plan = prepared(tmp_path, monkeypatch)
    assert 'scf' not in plan['kpoints'] and 'bands' not in plan['kpoints']
    assert {name for name in plan['incars']} - set(plan['kpoints']) == {
        'scf', 'bands', 'convergence/encut-0', 'convergence/encut-1', 'convergence/encut-2',
        'convergence/mesh-0', 'convergence/mesh-1', 'convergence/mesh-2'}


def test_failed_scan_blocks_relaxation(tmp_path, monkeypatch):
    core.save(tmp_path / 'config.json', config())
    Poscar(result().final_structure).write_file(tmp_path / 'initial.POSCAR')
    monkeypatch.setattr(core, 'verify', lambda root: {})
    seen = []
    def fake_stage(root, name, *args):
        seen.append(name)
        return result(energy=-10 - len(seen))
    monkeypatch.setattr(full, 'stage', fake_stage)
    with pytest.raises(ValueError, match='扫描未达到'):
        full.run_full(tmp_path, ['fake-vasp'])
    assert all(name.startswith('convergence/') for name in seen)


def test_prepare_full_has_no_premature_band_inputs(tmp_path):
    source = tmp_path / 'POSCAR'
    Poscar(result().final_structure).write_file(source)
    args = SimpleNamespace(**{k: v for k, v in config().items() if k not in ('encuts', 'meshes')},
                           encut=520, mesh=[4, 4, 4], potcar=None,
                           structure=str(source), workdir=str(tmp_path / 'job'))
    full.prepare_full(args)
    manifest = json.loads((tmp_path / 'job/manifest.json').read_text())
    assert manifest['workflow'] == 'full'
    assert not manifest['ready']
    assert not (tmp_path / 'job/bands/POSCAR').exists()


def test_parallel_stages_overlap_and_preserve_result_order(tmp_path, monkeypatch):
    import threading
    barrier = threading.Barrier(3, timeout=5)
    lock = threading.Lock()
    active = peak = 0
    def fake_stage(root, name):
        nonlocal active, peak
        with lock:
            active += 1
            peak = max(peak, active)
        barrier.wait()
        with lock:
            active -= 1
        return result(energy=-int(name))
    monkeypatch.setattr(full, 'stage', fake_stage)
    values = full.run_stages(tmp_path, 'test', [(tmp_path, str(i)) for i in range(6)], 3)
    assert peak == 3 and active == 0
    assert [v['energy_per_atom'] for v in values] == list(range(0, -6, -1))
    assert set(json.loads((tmp_path / 'workflow-state.json').read_text())['stages'].values()) == {'complete'}


def test_parallel_failure_blocks_downstream_and_joins_workers(tmp_path, monkeypatch):
    import threading
    cfg = dict(config(), parallel_tests=3, cores_per_test=8)
    core.save(tmp_path / 'config.json', cfg)
    Poscar(result().final_structure).write_file(tmp_path / 'initial.POSCAR')
    monkeypatch.setattr(core, 'verify', lambda root: {})
    monkeypatch.setenv('SLURM_JOB_ID', 'test')
    monkeypatch.setenv('SLURM_NTASKS', '24')
    barrier = threading.Barrier(3, timeout=5)
    active = set()
    lock = threading.Lock()
    def fake_stage(root, name, *args):
        assert name.startswith('convergence/')
        with lock:
            active.add(name)
        try:
            if name.startswith('convergence/encut'):
                barrier.wait()
            if name.endswith('encut-0'):
                raise ValueError('test stage failure')
            return result()
        finally:
            with lock:
                active.remove(name)
    monkeypatch.setattr(full, 'stage', fake_stage)
    with pytest.raises(ValueError, match='test stage failure'):
        full.run_full(tmp_path, ['srun', 'vasp_std'])
    assert not active


def test_parallel_rejects_insufficient_allocation(tmp_path, monkeypatch):
    core.save(tmp_path / 'config.json', dict(config(), parallel_tests=3, cores_per_test=8))
    Poscar(result().final_structure).write_file(tmp_path / 'initial.POSCAR')
    monkeypatch.setattr(core, 'verify', lambda root: {})
    monkeypatch.setenv('SLURM_JOB_ID', 'test')
    monkeypatch.setenv('SLURM_NTASKS', '8')
    monkeypatch.setattr(full, 'stage', lambda *a: pytest.fail('must not start'))
    with pytest.raises(ValueError, match='不足'):
        full.run_full(tmp_path, ['srun', 'vasp_std'])
