"""Operator-chosen production ENCUT from a passed scan; no model, SSH or VASP traffic."""
import json
import math
from types import SimpleNamespace as NS

import pytest
from pymatgen.io.vasp.inputs import Incar, Kpoints, Poscar

from vasp_agent.workflow import convergence_reuse as reuse, full_workflow as full, stage_gate as gate, vasp_band_agent as core
from test_stage_gate import staged, approve
from test_full_workflow import result

# Per-atom energy offsets (eV): adjacent steps 0.8 meV, both pass the 1 meV tolerance,
# yet encut-0 sits 1.6 meV from the highest -- the case the cross comparison catches.
OFFSETS = {'convergence/encut-0': .0016, 'convergence/encut-1': .0008}


@pytest.fixture
def converged(staged, monkeypatch):
    launches = []
    def read(folder):
        return result(Poscar.from_file(folder / 'POSCAR').structure,
                      energy=float((folder / 'vasprun.xml').read_text()))
    def execute(root, name, command):
        folder = root / name
        if not (folder / 'complete.json').exists():
            launches.append(name)
            (folder / 'vasprun.xml').write_text(str(-10. + OFFSETS.get(name, 0.)))
            core.save(folder / 'complete.json', {'xml_sha256': core.digest(folder / 'vasprun.xml')})
        return read(folder)
    monkeypatch.setattr(core, 'execute', execute)
    monkeypatch.setattr(core, 'read_result', read)
    gate.confirm_physics(staged, 'silicon PBE')
    approve(staged)
    full.run_full(staged, ['mock'])
    assert gate.current(staged) == 'relaxation'
    launches.clear()
    return staged, launches


def test_lower_passing_cutoff_drives_every_production_stage(converged):
    source, launches = converged
    cfg = json.loads((source / 'config.json').read_text())
    lower, highest, mesh = cfg['encuts'][1], cfg['encuts'][-1], cfg['meshes'][-1]
    rows = reuse.inspect(source)['parameters']['encut']['cutoff_vs_highest']
    assert [r['passed'] for r in rows] == [False, True]
    target = source.parent / 'lower'
    answer = reuse.prepare_production(source, target, mesh, gate.context(source)['snapshot'], encut=lower)
    assert answer['encut'] == lower and answer['encut_vs_highest']['passed']
    assert answer['post_relax_higher_cutoff'] == math.ceil(lower * 1.15)
    assert not answer['scan_recomputed'] and not answer['submitted']
    saved = json.loads((target / 'config.json').read_text())
    assert saved['production_encut'] == lower and saved['encuts'] == cfg['encuts']
    sampling = gate.context(target)['inputs']['production_sampling']
    assert sampling['encut'] == lower
    assert json.loads((target / 'production-choice.json').read_text(encoding='utf-8'))['encut'] == lower
    plan = json.loads((target / 'input-plan.json').read_text(encoding='utf-8'))
    assert plan['incars']['relax-1']['ENCUT'] == lower
    assert plan['incars']['validation/higher-cutoff']['ENCUT'] == math.ceil(lower * 1.15)
    assert plan['incars']['convergence/encut-2']['ENCUT'] == highest  # the scan record is untouched
    approve(target)
    full.run_full(target, ['mock'])
    assert launches == ['relax-1', 'relax-2', 'validation/reference', 'validation/higher-cutoff', 'validation/denser-mesh']
    for name in ('relax-1', 'relax-2', 'validation/reference', 'validation/denser-mesh'):
        assert Incar.from_file(target / name / 'INCAR')['ENCUT'] == lower
    assert Incar.from_file(target / 'validation/higher-cutoff/INCAR')['ENCUT'] == math.ceil(lower * 1.15)
    assert Kpoints.from_file(target / 'relax-1/KPOINTS').kpts[0] == tuple(mesh)


def test_cutoff_that_fails_against_highest_is_refused(converged):
    source, _ = converged
    cfg = json.loads((source / 'config.json').read_text())
    target = source.parent / 'too-low'
    with pytest.raises(ValueError, match=str(cfg['encuts'][1])):
        reuse.prepare_production(source, target, cfg['meshes'][-1], gate.context(source)['snapshot'],
                                 encut=cfg['encuts'][0])
    assert not target.exists()


@pytest.mark.parametrize('encut', [123., 'x'])
def test_untested_cutoff_refused(converged, encut):
    source, _ = converged
    cfg = json.loads((source / 'config.json').read_text())
    with pytest.raises(ValueError, match='已测试'):
        reuse.prepare_production(source, source.parent / 'bad', cfg['meshes'][-1],
                                 gate.context(source)['snapshot'], encut=encut)
    assert not (source.parent / 'bad').exists()


def test_lower_cutoff_needs_the_scan_grid(converged):
    source, _ = converged
    cfg = json.loads((source / 'config.json').read_text())
    with pytest.raises(ValueError, match='扫描在网格'):
        reuse.prepare_production(source, source.parent / 'coarse', cfg['meshes'][1],
                                 gate.context(source)['snapshot'], encut=cfg['encuts'][1])
    # The highest value needs no equivalence claim, on any tested grid.
    answer = reuse.prepare_production(source, source.parent / 'coarse-high', cfg['meshes'][1],
                                      gate.context(source)['snapshot'], encut=cfg['encuts'][-1])
    assert answer['encut'] == cfg['encuts'][-1] and answer['encut_vs_highest'] is None
    assert 'production_encut' not in json.loads((source.parent / 'coarse-high/config.json').read_text())


def test_production_cutoff_validation():
    cfg = {'encuts': [520, 598, 676]}
    assert reuse.production_cutoff(cfg) == 676
    assert reuse.production_cutoff({**cfg, 'production_encut': 598}) == 598
    with pytest.raises(ValueError):
        reuse.production_cutoff({**cfg, 'production_encut': 600})


def test_tool_and_remote_pass_encut(tmp_path, monkeypatch):
    from vasp_agent.chat import ai_agent as ai
    from vasp_agent.remote.remote_compute import RemoteCompute
    box = ai.Toolbox(tmp_path)
    box.review_snapshots[('stage', 'hpc', 'src')] = 'snap'
    calls = []
    monkeypatch.setattr(RemoteCompute, '__init__', lambda *a: None)
    monkeypatch.setattr(RemoteCompute, 'call', lambda self, action, job, **kw: calls.append((action, job, kw)) or {})
    monkeypatch.setattr(box, 'ensure_current_release', lambda server: None)
    box.prepare_production_from_convergence('hpc', 'src', 'new', [4, 4, 4], 'snap', 780)
    box.prepare_production_from_convergence('hpc', 'src', 'new2', [4, 4, 4], 'snap')
    assert calls[0][2]['encut'] == 780 and 'encut' not in calls[1][2]
    schema = next(t for t in ai.TOOLS if t['name'] == 'prepare_production_from_convergence')
    assert 'encut' in schema['parameters']['properties']


def test_old_engine_refuses_encut_instead_of_using_highest(monkeypatch, tmp_path):
    from vasp_agent.remote import remote_worker as worker
    monkeypatch.setattr(worker, '_convergence_reuse', lambda root: (tmp_path, NS(PRODUCTION_MESH_VERSION=2)))
    monkeypatch.setattr(worker, 'task_target', lambda jobs, name: tmp_path / name)
    cfg = {'vasp_env': __file__}
    with pytest.raises(ValueError, match='部署新版'):
        worker._prepare_from_convergence({'source_job': 'src', 'encut': 780, 'mesh': [4] * 3},
                                         cfg, tmp_path, 'new', tmp_path / 'new', production=True)
