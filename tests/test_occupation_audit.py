"""SCF occupation evidence for the bands review; fake vasprun, no VASP or SSH."""
import json
from types import SimpleNamespace as NS

import numpy as np
import pytest

import vasp_agent.remote.remote_worker as worker
from vasp_agent.workflow import vasp_band_agent as core


def task(tmp_path, monkeypatch, top_occupation=0.0):
    folder = tmp_path / 'la'
    (folder / 'scf').mkdir(parents=True)
    (folder / 'scf/vasprun.xml').write_text('<fake/>')
    core.save(folder / 'scf/complete.json', {'xml_sha256': core.digest(folder / 'scf/vasprun.xml')})
    core.save(folder / 'scf-summary.json', {'efermi': 5.0, 'nelect': 63.0, 'nbands': 64})
    energies = np.linspace(-10, 20, 64)
    values = np.zeros((10, 64, 2))
    values[:, :, 0] = energies
    values[:, :32, 1] = 1.0
    values[:, 31, 1] = 0.5
    values[3, -1, 1] = top_occupation
    import pymatgen.io.vasp.outputs as outputs
    monkeypatch.setattr(outputs, 'Vasprun', lambda *a, **k: NS(eigenvalues={__import__('pymatgen.electronic_structure.core', fromlist=['Spin']).Spin.up: values}))
    return folder


def test_audit_reports_empty_top_band_and_margin(tmp_path, monkeypatch):
    audit = worker.scf_occupation_audit(task(tmp_path, monkeypatch))
    up = audit['channels']['up']
    assert audit['passed'] and up['highest_occupied_band'] == 32 and up['empty_bands_above'] == 32
    assert up['highest_band_max_occupation'] == 0 and up['top_band_min_minus_efermi_eV'] == 15.0
    assert audit['nelect'] == 63.0 and audit['xml_sha256']


def test_occupied_top_band_fails(tmp_path, monkeypatch):
    audit = worker.scf_occupation_audit(task(tmp_path, monkeypatch, top_occupation=1e-3))
    assert not audit['passed'] and audit['channels']['up']['empty_bands_above'] == 0


def test_changed_output_is_not_used(tmp_path, monkeypatch):
    folder = task(tmp_path, monkeypatch)
    (folder / 'scf/vasprun.xml').write_text('<tampered/>')
    with pytest.raises(ValueError, match='不一致'):
        worker.scf_occupation_audit(folder)
    data = worker.with_occupation_audit({'phase': 'bands'}, folder)
    assert data['scf_occupation_audit']['passed'] is False and 'unavailable' in data['scf_occupation_audit']


def test_only_bands_phase_gets_the_audit(tmp_path, monkeypatch):
    folder = task(tmp_path, monkeypatch)
    assert 'scf_occupation_audit' not in worker.with_occupation_audit({'phase': 'scf'}, folder)
