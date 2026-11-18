import argparse
import json
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pytest
from pymatgen.core import Lattice
from pymatgen.electronic_structure.bandstructure import BandStructureSymmLine
from pymatgen.electronic_structure.core import Spin
from pymatgen.io.vasp.inputs import Incar

import vasp_agent.workflow.vasp_band_agent as agent


def settings(tmp_path):
    return argparse.Namespace(structure=str(Path(__file__).parents[1] / 'examples/Si.POSCAR'),
                              workdir=str(tmp_path / 'job'), encut=520, ediff=1e-7,
                              mesh=[8, 8, 8], points=10, symprec=0.01, potcar=None)


def test_prepare_consistent_basis_and_charge(tmp_path):
    args = settings(tmp_path)
    agent.prepare(args)
    root = Path(args.workdir)
    assert (root / 'scf/POSCAR').read_bytes() == (root / 'bands/POSCAR').read_bytes()
    assert Incar.from_file(root / 'bands/INCAR')['ICHARG'] == 11
    assert Incar.from_file(root / 'scf/INCAR')['LCHARG']
    with pytest.raises(ValueError, match='POTCAR'):
        agent.verify(root)
    with pytest.raises(ValueError, match='非空'):
        agent.prepare(args)


def test_unconverged_blocks_result(monkeypatch, tmp_path):
    monkeypatch.setattr(agent, 'Vasprun', lambda *a, **k: SimpleNamespace(converged_electronic=False))
    with pytest.raises(ValueError, match='未收敛'):
        agent.read_result(tmp_path)


def test_reader_enables_fermi_energy_parsing(monkeypatch, tmp_path):
    def parse(*args, **kwargs):
        assert kwargs['parse_dos'] is True
        return SimpleNamespace(converged_electronic=False)
    monkeypatch.setattr(agent, 'Vasprun', parse)
    with pytest.raises(ValueError, match='未收敛'):
        agent.read_result(tmp_path)


def test_changed_input_rejected(tmp_path):
    args = settings(tmp_path)
    agent.prepare(args)
    root = Path(args.workdir)
    manifest = json.loads((root / 'manifest.json').read_text())
    manifest['ready'] = True  # fixture only; no real POTCAR is distributed
    agent.save(root / 'manifest.json', manifest)
    (root / 'bands/INCAR').write_text('ICHARG=2')
    with pytest.raises(ValueError, match='已改变'):
        agent.verify(root)


def test_synthetic_plot_and_csv(tmp_path):
    # Pure synthetic bands test rendering and reference shift, not VASP physics.
    bs = BandStructureSymmLine([[0, 0, 0], [.25, 0, 0], [.5, 0, 0]],
         {Spin.up: np.array([[-1., -.5, -1.], [1., 1.5, 2.]])},
         Lattice.cubic(1), 0.2, labels_dict={'G': [0, 0, 0], 'X': [.5, 0, 0]})
    report = agent.export_bands(bs, tmp_path, {'formula': 'SYNTHETIC TEST'})
    assert report['sampled_path_gap']['energy'] == pytest.approx(1.5)
    assert report['plot_energy_reference'] == {'kind': 'vbm', 'energy_eV': -.5}
    assert 'energy_minus_plot_reference_eV' in (tmp_path / 'bands.csv').read_text()
    assert '-1.2' in (tmp_path / 'bands.csv').read_text()
    assert (tmp_path / 'bands.png').stat().st_size > 1000


def test_scf_failure_never_launches_bands(monkeypatch, tmp_path):
    stages = []
    monkeypatch.setattr(agent, 'verify', lambda root: {})
    monkeypatch.setattr(agent.shutil, 'which', lambda command: command)
    def fail(root, stage, command):
        stages.append(stage)
        raise ValueError('SCF failed')
    monkeypatch.setattr(agent, 'execute', fail)
    with pytest.raises(ValueError, match='SCF failed'):
        agent.run(SimpleNamespace(workdir=str(tmp_path), command=['vasp_std']))
    assert stages == ['scf']
    assert not (tmp_path / '.agent.lock').exists()


def test_successful_workflow_and_resume(monkeypatch, tmp_path):
    (tmp_path / 'scf').mkdir()
    (tmp_path / 'bands').mkdir()
    launches = []
    monkeypatch.setattr(agent, 'verify', lambda root: {})
    monkeypatch.setattr(agent.shutil, 'which', lambda command: command)
    monkeypatch.setattr(agent, 'read_result', lambda folder: object())
    monkeypatch.setattr(agent, 'analyze', lambda root: None)
    def launch(command, cwd, **kwargs):
        launches.append(cwd.name)
        (cwd / 'vasprun.xml').write_text('synthetic fixture')
        if cwd.name == 'scf':
            (cwd / 'CHGCAR').write_text('synthetic charge')
        else:
            assert (cwd / 'CHGCAR').read_text() == 'synthetic charge'
    monkeypatch.setattr(agent.subprocess, 'run', launch)
    args = SimpleNamespace(workdir=str(tmp_path), command=['vasp_std'])
    agent.run(args)
    agent.run(args)
    assert launches == ['scf', 'bands']
