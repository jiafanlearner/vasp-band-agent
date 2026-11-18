"""KPAR/NCORE derived from the confirmed MPI layout; fake VASP, no SSH or scheduler."""
import json
from types import SimpleNamespace as NS

import numpy as np
import pytest
from pymatgen.io.vasp.inputs import Incar, Kpoints, Poscar

import vasp_agent.workflow.full_workflow as full
import vasp_agent.workflow.stage_gate as gate
import vasp_agent.workflow.vasp_band_agent as core
from test_stage_gate import staged, approve


def set_config(root, **changes):
    """Stand-in for configure_resources: rewrite config.json and its manifest hash."""
    config = json.loads((root / 'config.json').read_text())
    for key, value in changes.items():
        if value is None:
            config.pop(key, None)
        else:
            config[key] = value
    core.save(root / 'config.json', config)
    manifest = json.loads((root / 'manifest.json').read_text())
    manifest['hashes']['config.json'] = core.digest(root / 'config.json')
    core.save(root / 'manifest.json', manifest)


@pytest.fixture
def fake_vasp(monkeypatch):
    launches = []
    def execute(root, name, command):
        folder = root / name
        structure = Poscar.from_file(folder / 'POSCAR').structure
        if not (folder / 'complete.json').exists():
            launches.append(name)
            (folder / 'vasprun.xml').write_text('mock output ' + name)
            data = {'xml_sha256': core.digest(folder / 'vasprun.xml')}
            if name == 'scf':
                (folder / 'CHGCAR').write_text('mock charge')
                data['charge_sha256'] = core.digest(folder / 'CHGCAR')
            core.save(folder / 'complete.json', data)
        return NS(final_structure=structure, final_energy=-10. * len(structure), converged_ionic=True,
            parameters={'NELM': 150, 'NBANDS': 16, 'NELECT': 4}, efermi=0.,
            ionic_steps=[{'forces': np.zeros((len(structure), 3)).tolist(),
                          'stress': np.zeros((3, 3)).tolist(), 'electronic_steps': [{}]}])
    monkeypatch.setattr(core, 'execute', execute)
    monkeypatch.setattr(full, 'analyze_full', lambda root: core.save(root / 'workflow-state.json', {'status': 'complete'}))
    return launches


@pytest.mark.parametrize('ranks, nodes, mesh, expected', [
    (32, 1, [11, 11, 15], {'KPAR': 8, 'NCORE': 2}),   # La2CuO4: nk >= 38, KPAR capped at ranks/4
    (8, 1, [11, 11, 15], {'KPAR': 2, 'NCORE': 2}),
    (16, 1, [11, 11, 15], {'KPAR': 4, 'NCORE': 2}),
    (24, 1, [11, 11, 15], {'KPAR': 6, 'NCORE': 2}),
    (64, 2, [11, 11, 15], {'KPAR': 16, 'NCORE': 2}),
    (7, 1, [11, 11, 15], {'KPAR': 1, 'NCORE': 1}),    # prime rank count: no safe split
    (32, 1, [4, 4, 4], {'KPAR': 2, 'NCORE': 4}),      # 64 points -> at least 2 irreducible
    (32, 1, [2, 2, 2], {'KPAR': 1, 'NCORE': 4}),
    (12, 2, [2, 2, 2], {'KPAR': 1, 'NCORE': 3}),      # NCORE must divide ranks per node (6)
])
def test_formula(ranks, nodes, mesh, expected):
    tags = full.execution_tags(ranks, nodes, Kpoints.gamma_automatic(mesh))
    assert tags == expected
    assert ranks % tags['KPAR'] == 0 and (ranks // tags['KPAR']) % tags['NCORE'] == 0
    assert tags['KPAR'] <= full.kpoint_lower_bound(Kpoints.gamma_automatic(mesh))


def test_line_mode_counts_every_path_point():
    points = Kpoints(style=Kpoints.supported_modes.Line_mode, num_kpts=40, coord_type='Reciprocal',
                     kpts=[[0, 0, 0], [.5, 0, 0], [.5, 0, 0], [.5, .5, 0]], labels=['G', 'X', 'X', 'M'])
    assert full.kpoint_lower_bound(points) == 80
    assert full.execution_tags(32, 1, points) == {'KPAR': 8, 'NCORE': 2}


def test_invalid_layout_is_refused():
    with pytest.raises(ValueError, match='布局'):
        full.execution_tags(30, 4)


def test_new_task_writes_derived_tags_and_keeps_manifest_and_review_valid(staged, fake_vasp):
    assert json.loads((staged / 'config.json').read_text())['execution_tags'] == full.EXECUTION_TAGS_VERSION
    prepared = (staged / 'convergence/encut-0/INCAR').read_text()
    assert 'KPAR' not in prepared  # the reviewed file is physics only
    set_config(staged, cores_per_test=32)
    gate.confirm_physics(staged, 'nonmagnetic bulk silicon PBE bands')
    review = gate.context(staged)
    preview = review['execution_tags']
    assert preview['enabled'] and preview['allowlist'] == ['KPAR', 'NCORE'] and preview['mpi_ranks_per_test'] == 32
    mesh = json.loads((staged / 'config.json').read_text())['meshes'][0]
    assert preview['stages']['convergence/mesh-0'] == full.execution_tags(32, 1, Kpoints.gamma_automatic(mesh))
    approve(staged)
    full.run_full(staged, ['mock'])
    assert gate.current(staged) == 'relaxation'
    for test in full.convergence_tests(json.loads((staged / 'config.json').read_text())):
        folder = staged / test['stage']
        incar = Incar.from_file(folder / 'INCAR')
        record = json.loads((folder / 'execution.json').read_text(encoding='utf-8'))
        assert {k: incar[k] for k in ('KPAR', 'NCORE')} == record['tags'] == \
            full.execution_tags(32, 1, Kpoints.gamma_automatic(test['mesh']))
        assert record['mpi_ranks_per_test'] == 32 and record['band_groups'] == 32 // (record['tags']['KPAR'] * record['tags']['NCORE'])
    # The prepared lines are untouched; only the tag lines were appended.
    assert core.without_execution_lines((staged / 'convergence/encut-0/INCAR').read_text()) == prepared
    core.verify(staged)
    context = gate.context(staged)
    assert 'convergence/encut-0/execution.json' in context['evidence']['hashes']
    assert context['execution_tags']['stages']['relax-1']['KPAR'] >= 1
    # Review-time snapshot of the prepared inputs is unaffected by the appended tags.
    assert 'KPAR' not in context['inputs']['initial_inputs']['convergence/encut-0/INCAR']


def test_completed_stages_keep_their_tags_when_later_phases_change_resources(staged, fake_vasp):
    set_config(staged, cores_per_test=32)
    gate.confirm_physics(staged, 'silicon PBE bands')
    approve(staged)
    full.run_full(staged, ['mock'])
    ran_with = (staged / 'convergence/mesh-2/INCAR').read_bytes()
    set_config(staged, cores_per_test=8, parallel_tests=1)
    for phase in ('relaxation', 'scf', 'bands'):
        assert gate.current(staged) == phase
        approve(staged)
        full.run_full(staged, ['mock'])
    assert gate.current(staged) == 'complete'
    assert (staged / 'convergence/mesh-2/INCAR').read_bytes() == ran_with
    for name in ('relax-1', 'scf', 'bands'):
        record = json.loads((staged / name / 'execution.json').read_text(encoding='utf-8'))
        assert record['mpi_ranks_per_test'] == 8
        assert Incar.from_file(staged / name / 'INCAR')['KPAR'] == record['tags']['KPAR']
    assert Incar.from_file(staged / 'bands/INCAR')['ICHARG'] == 11
    assert len(fake_vasp) == 13


def test_never_launched_stage_takes_the_current_tags(staged, fake_vasp, monkeypatch):
    set_config(staged, cores_per_test=8)
    gate.confirm_physics(staged, 'silicon PBE bands')
    approve(staged)
    # Inputs written, VASP never started (e.g. the job was cancelled in the queue).
    real = core.execute
    monkeypatch.setattr(core, 'execute', lambda *a, **k: (_ for _ in ()).throw(RuntimeError('cancelled')))
    with pytest.raises(RuntimeError):
        full.run_full(staged, ['mock'])
    grid = Kpoints.gamma_automatic(json.loads((staged / 'config.json').read_text())['meshes'][-1])
    assert Incar.from_file(staged / 'convergence/encut-0/INCAR')['KPAR'] == full.execution_tags(8, 1, grid)['KPAR'] == 2
    monkeypatch.setattr(core, 'execute', real)
    set_config(staged, cores_per_test=32)
    approve(staged)
    full.run_full(staged, ['mock'])
    # [6,6,6]: at least ceil(216/48) = 5 irreducible points, so KPAR = 4 rather than 32/4.
    assert Incar.from_file(staged / 'convergence/encut-0/INCAR')['KPAR'] == full.execution_tags(32, 1, grid)['KPAR'] == 4
    assert json.loads((staged / 'convergence/encut-0/execution.json').read_text(encoding='utf-8'))['mpi_ranks_per_test'] == 32
    core.verify(staged)


def test_old_task_runs_exactly_as_reviewed(staged, fake_vasp):
    set_config(staged, execution_tags=None, cores_per_test=32)
    prepared = (staged / 'convergence/encut-0/INCAR').read_bytes()
    gate.confirm_physics(staged, 'silicon PBE bands')
    assert 'execution_tags' not in gate.context(staged)
    approve(staged)
    full.run_full(staged, ['mock'])
    assert (staged / 'convergence/encut-0/INCAR').read_bytes() == prepared
    assert not (staged / 'convergence/encut-0/execution.json').exists()
    assert 'KPAR' not in Incar.from_file(staged / 'convergence/mesh-2/INCAR')
    approve(staged)
    full.run_full(staged, ['mock'])
    assert 'KPAR' not in Incar.from_file(staged / 'relax-1/INCAR')
    # An old task does not accept appended tags as "only parallelization".
    path = staged / 'convergence/encut-0/INCAR'
    path.write_bytes(prepared + b'KPAR = 2\n')
    with pytest.raises(ValueError, match='输入文件已改变'):
        core.verify(staged)


@pytest.mark.parametrize('extra', [{'NPAR': 2}, {'KPAR': 4}, {'NCORE': 2}, {'ISYM': 0}])
def test_any_change_to_the_physics_incar_is_still_refused(staged, extra):
    config = json.loads((staged / 'config.json').read_text())
    structure = Poscar.from_file(staged / 'initial.POSCAR').structure
    incar = {**full.base_incar(config, config['encuts'][0]), **extra}
    with pytest.raises(ValueError, match='偏离'):
        full.stage(staged, 'convergence/encut-0', structure, incar,
                   Kpoints.gamma_automatic(config['meshes'][-1]), ['mock'])


@pytest.mark.parametrize('line', ['ISYM = 0\n', 'KPAR = 3\n', 'NCORE = 8\n'])
def test_hand_edited_prepared_incar_is_refused(staged, fake_vasp, line):
    gate.confirm_physics(staged, 'silicon PBE bands')
    path = staged / 'convergence/encut-0/INCAR'
    with path.open('a', encoding='utf-8') as stream:
        stream.write(line)
    if line.startswith('ISYM'):
        with pytest.raises(ValueError):
            core.verify(staged)
        return
    # Tag lines leave the prepared physics hash intact, but the launch only
    # accepts the values it derives itself and rewrites the line.
    approve(staged)
    full.run_full(staged, ['mock'])
    tags = full.execution_tags(8, 1, Kpoints.gamma_automatic(json.loads((staged / 'config.json').read_text())['meshes'][-1]))
    assert {k: Incar.from_file(path)[k] for k in ('KPAR', 'NCORE')} == tags


def test_incar_matches_requires_the_recorded_tags(staged, fake_vasp):
    set_config(staged, cores_per_test=16)
    gate.confirm_physics(staged, 'silicon PBE bands')
    approve(staged)
    full.run_full(staged, ['mock'])
    config = json.loads((staged / 'config.json').read_text())
    folder = staged / 'convergence/encut-1'
    physics = full.base_incar(config, config['encuts'][1])
    assert full.incar_matches(folder, config, physics)
    record = json.loads((folder / 'execution.json').read_text(encoding='utf-8'))
    core.save(folder / 'execution.json', {**record, 'tags': {'KPAR': 1, 'NCORE': 1}})
    assert not full.incar_matches(folder, config, physics)
    assert not full.incar_matches(folder, {k: v for k, v in config.items() if k != 'execution_tags'}, physics)


def test_resource_menu_shows_the_tags_for_the_edited_layout(tmp_path):
    from vasp_agent.permissions.resource_menu import resource_menu
    current = dict(parallel_tests=1, cores_per_test=32, nodes=1, partition='compute', walltime='04:00:00')
    output = []
    resource_menu(None, 'test', 'relaxation', current, lambda prompt: '1', output.append, job='la2cuo4',
                  execution_tags={'enabled': True})
    assert any('KPAR ≤ 8，NCORE 2' in text for text in output)
    output.clear()
    resource_menu(None, 'test', 'relaxation', current, lambda prompt: '1', output.append, job='old',
                  execution_tags={'enabled': False})
    assert any('不写入（旧任务）' in text for text in output)


def test_remote_resource_context_reports_tags_and_detects_old_engine(staged, monkeypatch):
    from vasp_agent.remote.remote_worker import resource_context
    script = ('#!/bin/bash\n#SBATCH --partition=compute\n#SBATCH --nodes=1\n#SBATCH --ntasks=32\n'
              '#SBATCH --cpus-per-task=1\n#SBATCH --time=04:00:00\nexport OMP_NUM_THREADS=1\nsrun vasp_std\n')
    (staged / 'submit.slurm').write_text(script)
    set_config(staged, cores_per_test=32)
    data = resource_context(staged, 'convergence')
    assert data['execution_tags']['enabled'] and data['execution_tags']['maximum'] == {'KPAR': 8, 'NCORE': 2}
    monkeypatch.setattr(full, 'EXECUTION_TAGS_VERSION', 0)
    with pytest.raises(ValueError, match='部署新版'):
        resource_context(staged, 'convergence')
    set_config(staged, execution_tags=None)
    assert not resource_context(staged, 'convergence')['execution_tags']['enabled']
