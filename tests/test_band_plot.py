import csv
import hashlib
import json

import matplotlib
matplotlib.use('Agg')
import numpy as np
import pytest
from pymatgen.core import Lattice, Structure
from pymatgen.electronic_structure.bandstructure import BandStructureSymmLine
from pymatgen.electronic_structure.core import Spin
from pymatgen.io.vasp.inputs import Poscar
from pymatgen.symmetry.bandstructure import HighSymmKpath

import vasp_agent.chat.ai_agent as ai
import vasp_agent.workflow.band_plot as band_plot
import vasp_agent.workflow.vasp_band_agent as agent
from vasp_agent.workflow.band_analysis import COLUMNS, EXTRA_COLUMNS

CUBIC = Structure(Lattice.cubic(3.0), ['Po'], [[0, 0, 0]])
# Simple cubic path Γ-X-M-Γ-R-X | M-R: one discontinuity after the first branch.
TICKS = ['Γ', 'X', 'M', 'Γ', 'R', 'X|M', 'R']


def cubic_bs(kind='metal', spins=1):
    kpath = HighSymmKpath(CUBIC, symprec=0.01).kpath
    points = []
    for branch in kpath['path']:
        for start, end in zip(branch, branch[1:]):
            a, b = np.array(kpath['kpoints'][start]), np.array(kpath['kpoints'][end])
            points += [a + (b - a) * t for t in np.linspace(0, 1, 6)]
    s = np.linspace(0, 1, len(points))
    if kind == 'metal':
        bands = np.array([-4 + 0 * s, -2 + 4 * s, 3 + s])
    else:
        bands = np.array([-4 + 0 * s, -0.5 - np.sin(np.pi * s), 1.0 + s])
    eigen = {Spin.up: bands}
    if spins == 2:
        eigen[Spin.down] = bands + 0.3
    return BandStructureSymmLine(points, eigen, CUBIC.lattice.reciprocal_lattice, 0.0, kpath['kpoints'])


def test_labels_and_formula_are_typeset():
    assert band_plot.format_label('\\Gamma') == 'Γ'
    assert band_plot.format_label('GAMMA') == 'Γ'
    assert band_plot.format_label('\\Sigma_1') == 'Σ$_{1}$'
    assert band_plot.format_label('Y_1') == 'Y$_{1}$'
    assert band_plot.format_label("S'") == "S'"
    assert band_plot.format_formula('La2CuO4') == 'La$_{2}$CuO$_{4}$'


def test_export_takes_ticks_and_breaks_from_branches(tmp_path, monkeypatch):
    seen = {}
    real = band_plot.plot_bands
    def spy(distances, energies, ticks, breaks, kind, title, *args):
        seen.update(ticks=ticks, breaks=breaks, kind=kind, title=title)
        return real(distances, energies, ticks, breaks, kind, title, *args)
    monkeypatch.setattr(band_plot, 'plot_bands', spy)
    bs = cubic_bs()
    agent.export_bands(bs, tmp_path, {'formula': 'Po1'})
    assert [label for _, label in seen['ticks']] == TICKS
    (jump,) = seen['breaks']
    assert bs.kpoints[jump - 1].label == 'X' and bs.kpoints[jump].label == 'M'
    assert seen['kind'] == 'scf_fermi'
    assert seen['title'] == 'Po$_{1}$ · PBE, non-spin-polarized'
    assert (tmp_path / 'bands.png').stat().st_size > 1000 and (tmp_path / 'bands.pdf').stat().st_size > 1000


def test_export_keeps_csv_and_report_fields(tmp_path):
    report = agent.export_bands(cubic_bs('insulator'), tmp_path, {'formula': 'Po'})
    with (tmp_path / 'bands.csv').open(encoding='utf-8') as stream:
        rows = list(csv.reader(stream))
    # The original columns and report fields are unchanged; the path record is appended.
    assert tuple(rows[0]) == COLUMNS + EXTRA_COLUMNS
    assert len(rows) - 1 == 3 * len(cubic_bs().kpoints)
    assert set(report) == {'formula', 'scf_fermi_energy_eV', 'plot_energy_reference', 'metallic_on_sampled_path',
                           'sampled_path_gap', 'limitations', 'band_path_source'}
    assert report['band_path_source'] == 'automatic'
    assert report['plot_energy_reference'] == {'kind': 'vbm', 'energy_eV': pytest.approx(-0.5)}
    assert json.loads((tmp_path / 'report.json').read_text(encoding='utf-8'))['plot_energy_reference']['kind'] == 'vbm'


def test_plot_splits_lines_at_breaks_and_marks_reference():
    bs = cubic_bs()
    ticks, breaks = band_plot.branch_ticks(bs)
    energies = {'1': bs.bands[Spin.up]}
    fig = band_plot.plot_bands(bs.distance, energies, ticks, breaks, 'scf_fermi', 'title')
    ax = fig.axes[0]
    band_lines = [line for line in ax.get_lines() if len(line.get_xdata()) > 2]
    (jump,) = breaks
    # Each band is drawn as two pieces split at the X|M jump, never one line across it.
    assert sorted(len(line.get_xdata()) for line in band_lines) == sorted([jump, len(bs.kpoints) - jump] * 3)
    assert ax.get_ylim() == (-8, 8)
    assert ax.get_legend() is None
    assert any('from SCF' in t.get_text() for t in ax.texts)
    assert not ax.patches


def test_insulator_is_zeroed_at_vbm_with_gap_shaded():
    bs = cubic_bs('insulator')
    ticks, breaks = band_plot.branch_ticks(bs)
    energies = {'1': bs.bands[Spin.up] + 0.5}
    fig = band_plot.plot_bands(bs.distance, energies, ticks, breaks, 'vbm', None, ylim=(-3, 3))
    ax = fig.axes[0]
    assert ax.get_ylim() == (-3, 3)
    assert [t.get_text() for t in ax.texts] == ['VBM']
    (shade,) = ax.patches
    assert shade.get_y() == pytest.approx(0.0) and shade.get_height() == pytest.approx(1.5)
    assert 'VBM' in ax.get_ylabel()


def test_spin_polarized_gets_two_colors_and_a_legend(tmp_path):
    bs = cubic_bs(spins=2)
    ticks, breaks = band_plot.branch_ticks(bs)
    fig = band_plot.plot_bands(bs.distance, {str(s): v for s, v in bs.bands.items()}, ticks, breaks)
    ax = fig.axes[0]
    assert [t.get_text() for t in ax.get_legend().get_texts()] == ['spin ↑', 'spin ↓']
    assert {line.get_color() for line in ax.get_lines() if len(line.get_xdata()) > 2} == set(band_plot.SPIN_COLORS)
    agent.export_bands(bs, tmp_path, {'formula': 'Po'})
    assert {row['spin'] for row in csv.DictReader((tmp_path / 'bands.csv').open(encoding='utf-8'))} == {'1', '-1'}


def test_2d_path_labels_come_from_band_path_2d():
    slab = Structure(Lattice.from_parameters(3, 3, 20, 90, 90, 90), ['Mo'], [[0, 0, 0.5]])
    route = [(0, 0, 0), (0.5, 0, 0), (0.5, 0.5, 0), (0, 0, 0)]
    kpoints, distance = [], 0.0
    for a, b in zip(route, route[1:]):
        for t in np.linspace(0, 1, 4):
            frac = tuple(x + (y - x) * t for x, y in zip(a, b))
            if kpoints:
                distance += 0.0 if t == 0 else 0.1
            kpoints.append((distance, frac))
    labels = band_plot.csv_labels(kpoints, str(Poscar(slab)), '2D')
    ticks, _ = band_plot.path_ticks([k[0] for k in kpoints], labels)
    assert [label for _, label in ticks] == ['Γ', 'X', 'M', 'Γ']
    assert band_plot.csv_breaks(kpoints) == []


def write_download(workspace, bs, formula='Po'):
    """A delivered result folder with a receipt, the way RemoteCompute.fetch writes it."""
    folder = workspace / 'downloads/eshell/po-bands-01-8d6e0213'
    folder.mkdir(parents=True)
    agent.export_bands(bs, folder, {'formula': formula})
    (folder / 'scf-summary.json').write_text(json.dumps({'efermi': 0.0, 'nbands': 3, 'nelect': 2.0,
                                                         'poscar': str(Poscar(CUBIC))}), encoding='utf-8')
    (folder / 'physics-model.json').write_text(json.dumps({'dimension': '3D', 'magnetism': 'nonmagnetic',
                                                           'functional': 'PBE'}), encoding='utf-8')
    files = {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in folder.iterdir()}
    (folder / 'download-receipt.json').write_text(json.dumps({'files': {
        name: {'sha256': digest, 'verified': True} for name, digest in files.items()}}), encoding='utf-8')
    return folder, files


def receipt_holds(folder):
    receipt = json.loads((folder / 'download-receipt.json').read_text(encoding='utf-8'))['files']
    return all(hashlib.sha256((folder / name).read_bytes()).hexdigest() == entry['sha256']
               for name, entry in receipt.items())


def test_replot_from_csv_matches_export_and_keeps_receipt(tmp_path):
    folder, files = write_download(tmp_path, cubic_bs('insulator'))
    before = folder.stat().st_mtime_ns
    result = band_plot.replot_folder(folder)
    assert result['ticks'] == TICKS and result['labels_recovered']
    assert result['reference_kind'] == 'vbm' and result['receipt_verified']
    assert set(result['inputs_sha256']) == {'bands.csv', 'scf-summary.json', 'report.json', 'physics-model.json'}
    for name in ('bands.replot.png', 'bands.replot.pdf', 'bands.replot.json'):
        assert (folder / name).stat().st_size > 100
    assert receipt_holds(folder)
    assert hashlib.sha256((folder / 'bands.png').read_bytes()).hexdigest() == files['bands.png']
    assert folder.stat().st_mtime_ns == before  # latest_download orders downloads by mtime


def test_replot_refuses_inputs_that_differ_from_receipt(tmp_path):
    folder, _ = write_download(tmp_path, cubic_bs())
    (folder / 'report.json').write_text('{}', encoding='utf-8')
    with pytest.raises(band_plot.ReceiptMismatch):
        band_plot.replot_folder(folder)
    assert not (folder / 'bands.replot.png').exists()


def test_replot_tool_and_band_analysis_still_verify(tmp_path):
    folder, _ = write_download(tmp_path, cubic_bs())
    box = ai.Toolbox(tmp_path)
    result = box.call('replot_bands', {'server': 'eshell', 'job_name': 'po-bands-01', 'y_min': -5, 'y_max': 5})
    assert result['ok'], result
    assert result['result']['png'] == 'downloads/eshell/po-bands-01-8d6e0213/bands.replot.png'
    assert result['result']['ylim'] == [-5.0, 5.0]
    analysis = box.call('analyze_band_results', {'server': 'eshell', 'job_name': 'po-bands-01',
                                                 'table_offset': 0, 'table_limit': 0})
    assert analysis['ok'], analysis
    assert analysis['result']['source']['receipt_verified']
    bad = box.call('replot_bands', {'server': 'eshell', 'job_name': 'po-bands-01', 'y_min': 5, 'y_max': -5})
    assert not bad['ok']
