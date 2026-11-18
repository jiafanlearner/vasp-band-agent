"""Walk a staged task through its phases with mocked VASP, recording what reviews see.

Shared by test_workflow_stages.py and by the one-off script that wrote
tests/data/legacy_stage_walk.json with the engine as it was before stage
definitions existed (commit 530835e). The walk is deterministic and free of
absolute paths, so the same task built by the current engine must produce the
same snapshots, checkpoints and workflow states byte for byte.
"""
import json
from types import SimpleNamespace as NS

import numpy as np
from pymatgen.core import Lattice, Structure
from pymatgen.io.vasp.inputs import Poscar

import vasp_agent.workflow.full_workflow as full
import vasp_agent.workflow.stage_gate as gate
import vasp_agent.workflow.vasp_band_agent as core

FIXED_TIME = '2026-01-01T00:00:00+00:00'


def build(tmp_path, legacy=True):
    """A ready staged Si task; legacy=True strips any stage record, as an old task has none."""
    structure = Structure(Lattice.cubic(5.43), ['Si'], [[0, 0, 0]])
    source = tmp_path / 'POSCAR'
    Poscar(structure).write_file(source)
    root = tmp_path / 'runs/si'
    full.prepare_full(NS(structure=str(source), workdir=str(root), potcar=None,
        encut=520, mesh=[4, 4, 4], nbands=16, relax_mode='cell', ediff=1e-7,
        points=40, symprec=.01, force_tol=.01, stress_tol=1., energy_tol=.001, nsw=200))
    (root / 'POTCAR').write_text('test fixture')
    core.save(root / 'potential-selection.json', [{'element': 'Si', 'titel': 'test-only metadata'}])
    manifest = json.loads((root / 'manifest.json').read_text())
    if legacy:
        config = json.loads((root / 'config.json').read_text())
        if config.pop('stages', None) is not None:
            core.save(root / 'config.json', config)
    manifest['ready'] = True
    for name in ('POTCAR', 'potential-selection.json', 'config.json'):
        manifest['hashes'][name] = core.digest(root / name)
    core.save(root / 'manifest.json', manifest)
    gate.confirm_physics(root, '非磁性三维硅的能带')
    physics = json.loads((root / 'physics-model.json').read_text(encoding='utf-8'))
    core.save(root / 'physics-model.json', {**physics, 'time': FIXED_TIME})
    return root


def fake_execute(root, name, command):
    folder = root / name
    structure = Poscar.from_file(folder / 'POSCAR').structure
    stamp = folder / 'complete.json'
    if not stamp.exists():
        (folder / 'vasprun.xml').write_text('mock output ' + name)
        data = {'xml_sha256': core.digest(folder / 'vasprun.xml')}
        if name == 'scf':
            (folder / 'CHGCAR').write_text('mock charge')
            data['charge_sha256'] = core.digest(folder / 'CHGCAR')
        core.save(stamp, data)
    return NS(final_structure=structure, final_energy=-10. * len(structure), converged_ionic=True,
              parameters={'NELM': 150, 'NBANDS': 16, 'NELECT': 4}, efermi=0.,
              ionic_steps=[{'forces': np.zeros((len(structure), 3)).tolist(),
                            'stress': np.zeros((3, 3)).tolist(), 'electronic_steps': [{}]}])


def fake_analyze(root):
    core.save(root / 'workflow-state.json', {**json.loads((root / 'workflow-state.json').read_text()),
                                             'status': 'complete'})


def checks_for(required):
    return [dict(item=item, judgment='pending' if item == 'validation_plan' else 'pass', blocking=False,
                 evidence='fixture', reason='fixture', next_action='无需') for item in required]


def walk(root, monkeypatch):
    """Review and run every phase; return what each review and completion recorded."""
    monkeypatch.setattr(full.Potcar, 'from_file', lambda p: [NS(element='Si')])
    monkeypatch.setattr(core, 'execute', fake_execute)
    monkeypatch.setattr(full, 'analyze_full', fake_analyze)
    steps = []
    while gate.current(root) != 'complete':
        data = gate.context(root)
        gate.record(root, data['snapshot'], data['phase'], checks_for(data['required_checks']), 'fixture')
        full.run_full(root, ['mock'])
        steps.append({'phase': data['phase'], 'snapshot': data['snapshot'],
                      'required_checks': list(data['required_checks']),
                      'evidence_hashes': data['evidence']['hashes'],
                      'checkpoint': json.loads((root / 'checkpoint.json').read_text(encoding='utf-8')),
                      'workflow_state': json.loads((root / 'workflow-state.json').read_text(encoding='utf-8'))})
    return steps
