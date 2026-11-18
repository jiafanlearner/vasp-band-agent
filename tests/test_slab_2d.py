import json
from types import SimpleNamespace

import numpy as np
import pytest
from pymatgen.core import Lattice, Structure
from pymatgen.io.vasp.inputs import Poscar

import vasp_agent.workflow.full_workflow as full
import vasp_agent.workflow.vasp_band_agent as core
from vasp_agent.workflow import dimensionality, stage_gate
from test_full_workflow import config, result


def mos2(c=20.0):
    return Structure(Lattice.hexagonal(3.16, c), ['Mo', 'S', 'S'],
                     [[1 / 3, 2 / 3, 0.5], [2 / 3, 1 / 3, 0.578], [2 / 3, 1 / 3, 0.422]])


def slab_args(tmp_path, **changes):
    source = tmp_path / 'POSCAR'
    Poscar(mos2()).write_file(source)
    values = {**{k: v for k, v in config().items() if k not in ('encuts', 'meshes')},
              'relax_mode': 'ions', 'encut': 520, 'mesh': [6, 6, 1], 'potcar': None,
              'structure': str(source), 'workdir': str(tmp_path / 'job')}
    values.update(changes)
    return SimpleNamespace(**values)


def test_classify_bulk_slab_and_wire():
    assert dimensionality.classify(Structure(Lattice.cubic(5.43), ['Si', 'Si'],
                                             [[0, 0, 0], [.25, .25, .25]]))['dimension'] == '3D'
    info = dimensionality.classify(mos2())
    assert info['dimension'] == '2D' and info['vacuum_axis'] == 2
    wire = Structure(Lattice.tetragonal(15, 2.5), ['C'], [[0, 0, 0]])
    with pytest.raises(ValueError, match='1D'):
        dimensionality.require_supported(dimensionality.classify(wire))


def test_vacuum_must_be_along_c():
    flipped = Structure(Lattice.orthorhombic(20, 3.2, 3.2), ['Mo'], [[0.5, 0, 0]])
    with pytest.raises(ValueError, match='第三晶格矢量'):
        dimensionality.require_supported(dimensionality.classify(flipped))


@pytest.mark.parametrize('gamma', [120, 60])
def test_hexagonal_path_points_are_zone_corner_and_edge(gamma):
    structure = Structure(Lattice.from_parameters(3.16, 3.16, 20, 90, 90, gamma), ['Mo'], [[0, 0, .5]])
    path = dimensionality.band_path_2d(structure)
    reciprocal = structure.lattice.reciprocal_lattice
    b1 = np.linalg.norm(reciprocal.matrix[0])
    cart = {k: reciprocal.get_cartesian_coords(v) for k, v in path.kpath['kpoints'].items()}
    # Hexagonal zone: edge midpoint at |b|/2, corner at |b|/sqrt(3).
    assert np.linalg.norm(cart['M']) == pytest.approx(b1 / 2)
    assert np.linalg.norm(cart['K']) == pytest.approx(b1 / np.sqrt(3))
    assert all(v[2] == 0 for v in path.kpath['kpoints'].values())


def test_slab_rejects_cell_relaxation_and_out_of_plane_mesh(tmp_path):
    with pytest.raises(ValueError, match='relax_mode=ions'):
        full.prepare_full(slab_args(tmp_path, relax_mode='cell'))
    with pytest.raises(ValueError, match=r'\[k1, k2, 1\]'):
        full.prepare_full(slab_args(tmp_path, mesh=[6, 6, 6]))
    with pytest.raises(ValueError, match=r'\[k1, k2, 1\]'):
        full.prepare_full(slab_args(tmp_path, meshes=[[6, 6, 1], [8, 8, 1], [9, 9, 2]]))


def test_slab_prepare_records_dimension_and_in_plane_meshes(tmp_path):
    full.prepare_full(slab_args(tmp_path))
    job = tmp_path / 'job'
    cfg = json.loads((job / 'config.json').read_text(encoding='utf-8'))
    assert cfg['dimensionality']['dimension'] == '2D'
    assert cfg['meshes'] == [[6, 6, 1], [8, 8, 1], [9, 9, 1]]
    assert full.relaxation_grids(cfg)['validation/denser-mesh'] == [12, 12, 1]
    assert json.loads((job / 'manifest.json').read_text())['scope'].startswith('2D')
    plan = json.loads((job / 'input-plan.json').read_text(encoding='utf-8'))
    assert '真空层厚度收敛' in plan['pending_validation']


def test_physics_records_detected_dimension_and_refuses_mismatch(tmp_path):
    full.prepare_full(slab_args(tmp_path))
    job = tmp_path / 'job'
    data = stage_gate.confirm_physics(job, '单层 MoS2 能带')
    assert data['dimension'] == '2D' and data['dimension_source'] == dimensionality.SOURCE
    assert stage_gate.physics(job)['dimension'] == '2D'
    core.save(job / 'physics-model.json', {**data, 'dimension': '3D'})
    with pytest.raises(ValueError, match='不一致'):
        stage_gate.physics(job)


def test_slab_run_keeps_cell_and_samples_plane(tmp_path, monkeypatch):
    cfg = {**config(), 'relax_mode': 'ions', 'meshes': [[6, 6, 1], [8, 8, 1], [9, 9, 1]],
           'dimensionality': dimensionality.classify(mos2()), 'parallel_tests': 1, 'cores_per_test': 8}
    core.save(tmp_path / 'config.json', cfg)
    initial = mos2()
    Poscar(initial).write_file(tmp_path / 'initial.POSCAR')
    from vasp_agent.workflow.input_review import prepare_plan
    prepare_plan(tmp_path, cfg, initial, {'nbands': cfg['nbands']})
    monkeypatch.setattr(core, 'verify', lambda root: {})
    seen = {}
    def fake_stage(root, name, structure, incar, kpoints, command, charge=None):
        seen[name] = (structure, incar, kpoints)
        return SimpleNamespace(**{**vars(result(structure)), 'efermi': 0.,
                                  'parameters': {'NELM': 150, 'NBANDS': 16, 'NELECT': 18.}})
    monkeypatch.setattr(full, 'stage', fake_stage)
    monkeypatch.setattr(full, 'analyze_full', lambda root: None)
    full.run_full(tmp_path, ['fake-vasp'])
    assert seen['relax-1'][1]['ISIF'] == 2
    assert list(seen['validation/denser-mesh'][2].kpts[0]) == [12, 12, 1]
    scf_structure, _, scf_kpoints = seen['scf']
    assert np.allclose(scf_structure.lattice.matrix, initial.lattice.matrix)
    assert list(scf_kpoints.kpts[0]) == [9, 9, 1]
    bands = seen['bands'][2]
    assert set(bands.labels) == {'\\Gamma', 'M', 'K'}
    assert all(k[2] == 0 for k in bands.kpts)


def test_bands_only_mode_refuses_slab(tmp_path):
    source = tmp_path / 'POSCAR'
    Poscar(mos2()).write_file(source)
    args = SimpleNamespace(structure=str(source), workdir=str(tmp_path / 'job'), encut=520, ediff=1e-6,
                           points=20, mesh=[6, 6, 1], symprec=0.01, potcar=None, potcar_library=None)
    with pytest.raises(ValueError, match='full 模式'):
        core.prepare(args)


def test_model_tools_accept_slab_meshes():
    from vasp_agent.chat.ai_agent import TOOLS
    for tool in TOOLS:
        mesh = tool.get('parameters', {}).get('properties', {}).get('mesh')
        if mesh:
            assert mesh['items']['minimum'] == 1, tool['name']
