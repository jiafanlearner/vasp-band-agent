"""Publication-style band-structure plot, shared by export_bands and local replots.

export_bands draws from a pymatgen BandStructureSymmLine on the cluster;
replot_folder redraws a finished result from bands.csv + scf-summary.json
without VASP or pymatgen band objects. Both feed the same plot_bands, so a
replot looks like what a new run would deliver.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
from pathlib import Path

import numpy as np

DEFAULT_YLIM = (-8.0, 8.0)
LINE = '#1f4e79'
SPIN_COLORS = ('#1f4e79', '#c55a11')
REFERENCE = '#c0392b'
# Greek names that appear in pymatgen / Setyawan-Curtarolo and 2D path labels.
GREEK = {'gamma': 'Γ', 'sigma': 'Σ', 'delta': 'Δ', 'lambda': 'Λ', 'pi': 'Π', 'xi': 'Ξ', 'mu': 'Μ'}
REPLOT_STEM = 'bands.replot'
EDGE_TOL = 1e-4


class ReceiptMismatch(ValueError):
    """A replot input is missing from, or differs from, the download receipt."""


def format_label(label):
    """'\\Gamma' -> 'Γ', '\\Sigma_1' -> 'Σ$_{1}$', 'Y_1' -> 'Y$_{1}$', "S'" unchanged."""
    if not label:
        return ''
    name, _, sub = str(label).strip().lstrip('\\').partition('_')
    name = GREEK.get(name.lower(), name)
    return name + (f'$_{{{sub}}}$' if sub else '')


def format_formula(formula):
    """'La2CuO4' -> 'La$_2$CuO$_4$' (digits after an element become subscripts)."""
    return re.sub(r'(?<=[A-Za-z)\]])(\d+(?:\.\d+)?)', r'$_{\1}$', formula or '')


def plot_title(formula, functional='PBE', spin_polarized=False, magnetism=None):
    spin = 'spin-polarized' if spin_polarized else 'non-spin-polarized'
    if magnetism and not spin_polarized and magnetism != 'nonmagnetic':
        spin += f' ({magnetism})'
    parts = [format_formula(formula)] if formula else []
    return ' · '.join(parts + [', '.join(x for x in (functional, spin) if x)])


def path_ticks(distances, labels):
    """Ticks and segment breaks from per-k labels (None for unlabeled points).

    Line mode repeats each vertex at the start of the next branch with the same
    distance. Equal labels there are a joint (one tick); different labels are a
    discontinuity: one merged tick "Z|X" and a break so no line spans the jump.
    Returns ([(x, label)], [k indices where a new segment starts]).
    """
    ticks, breaks = [], []
    for k, (x, label) in enumerate(zip(distances, labels)):
        if label is None:
            continue
        text = format_label(label)
        if ticks and abs(ticks[-1][0] - x) < 1e-8:
            previous = ticks[-1][1]
            if text != previous.rsplit('|', 1)[-1]:
                ticks[-1] = (ticks[-1][0], previous + '|' + text)
                if k > 0 and labels[k - 1] is not None:
                    breaks.append(k)
        else:
            ticks.append((float(x), text))
    return ticks, breaks


def branch_ticks(bs):
    """Ticks and breaks from a BandStructureSymmLine's branches and their end labels."""
    labels = [None] * len(bs.kpoints)
    for branch in bs.branches:
        for index in (branch['start_index'], branch['end_index']):
            labels[index] = bs.kpoints[index].label or ''
    ticks, breaks = path_ticks(bs.distance, labels)
    # A branch starting at the same distance as the previous one ended is a jump
    # even when both ends were given the same label text (e.g. unlabeled points).
    for previous, branch in zip(bs.branches, bs.branches[1:]):
        start = branch['start_index']
        if start == previous['end_index'] + 1 and abs(bs.distance[start] - bs.distance[start - 1]) < 1e-8:
            same = np.allclose(bs.kpoints[start].frac_coords, bs.kpoints[start - 1].frac_coords, atol=1e-6)
            if not same and start not in breaks:
                breaks.append(start)
    return ticks, sorted(breaks)


def _gap_edges(energies):
    """(vbm, cbm) relative to the plot reference, or None when no gap is visible."""
    valence, conduction = [], []
    for values in energies.values():
        for band in np.asarray(values, dtype=float):
            (valence if band.max() <= EDGE_TOL else conduction).append(band)
    if not valence or not conduction:
        return None
    vbm = max(band.max() for band in valence)
    cbm = min(band.min() for band in conduction)
    return (vbm, cbm) if cbm - vbm > EDGE_TOL else None


def plot_bands(distances, energies, ticks, breaks=(), reference_kind='scf_fermi', title=None, ylim=DEFAULT_YLIM):
    """Draw a band structure and return the matplotlib Figure.

    distances: path coordinate per k point (1/Å).
    energies: {spin name: array (nbands, nk)} relative to the plot reference.
    ticks: [(x, label)] with labels already formatted (see path_ticks).
    breaks: k indices that start a new, disconnected path segment.
    reference_kind: 'scf_fermi' (metal, zero at the SCF E_F) or 'vbm'.
    """
    import matplotlib
    if matplotlib.get_backend().lower() != 'agg':
        matplotlib.use('Agg')
    import matplotlib.pyplot as plt
    x = np.asarray(distances, dtype=float)
    spins = list(energies)
    polarized = len(spins) > 1
    fig, ax = plt.subplots(figsize=(7.0, 4.6))
    segments = [s for s in np.split(np.arange(len(x)), sorted(set(breaks))) if len(s)]
    names = {0: 'spin ↑', 1: 'spin ↓'}
    for index, spin in enumerate(spins):
        color = SPIN_COLORS[index % 2] if polarized else LINE
        for row, band in enumerate(np.asarray(energies[spin], dtype=float)):
            for number, segment in enumerate(segments):
                ax.plot(x[segment], band[segment], color=color, lw=1.0, solid_capstyle='round',
                        label=names.get(index) if polarized and row == 0 and number == 0 else None)
    ax.axhline(0, color=REFERENCE, lw=0.8, ls='--', zorder=0)
    if reference_kind == 'vbm':
        note = 'VBM'
        edges = _gap_edges(energies)
        if edges:
            ax.axhspan(edges[0], edges[1], color='0.92', zorder=-1, lw=0)
            ax.axhline(edges[1], color='0.55', lw=0.6, ls=':', zorder=0)
        ylabel = '$E - E_\\mathrm{VBM}$ (eV)'
    else:
        note = '$E_\\mathrm{F}$ from SCF'
        ylabel = '$E - E_\\mathrm{F}$ (eV)'
    for xt, _ in ticks:
        if x[0] + 1e-9 < xt < x[-1] - 1e-9:
            ax.axvline(xt, color='0.75', lw=0.6, zorder=0)
    ax.set_xticks([t[0] for t in ticks], [t[1] for t in ticks], fontsize=10)
    ax.set_xlim(x[0], x[-1])
    ax.set_ylim(*ylim)
    ax.set_ylabel(ylabel, fontsize=11)
    ax.tick_params(axis='y', labelsize=10, direction='in')
    ax.tick_params(axis='x', length=0, pad=6)
    ax.text(1.0, 1.01, note, transform=ax.transAxes, ha='right', va='bottom', fontsize=8, color=REFERENCE)
    if polarized:
        ax.legend(loc='lower right', fontsize=8, frameon=False, handlelength=1.5)
    if title:
        ax.set_title(title, fontsize=11, loc='left')
    fig.tight_layout()
    return fig


def save_figure(fig, png, pdf):
    import matplotlib.pyplot as plt
    try:
        fig.savefig(png, dpi=200, bbox_inches='tight')
        fig.savefig(pdf, bbox_inches='tight')
    finally:
        plt.close(fig)


# --- Local replot of finished results ---------------------------------------

def _kpath_points(structure, dimension):
    if dimension == '2D':
        from vasp_agent.workflow.dimensionality import band_path_2d
        return band_path_2d(structure).kpath['kpoints']
    from pymatgen.symmetry.bandstructure import HighSymmKpath
    return HighSymmKpath(structure, symprec=0.01).kpath['kpoints']


def csv_labels(kpoints, poscar, dimension):
    """Per-k labels for path vertices, recovered by matching fractional coordinates.

    bands.csv carries no labels; the band path was generated from the same
    primitive cell (scf-summary 'poscar'), so its vertices match exactly. A
    vertex that matches nothing keeps an empty label rather than a guess.
    """
    vertices = {0, len(kpoints) - 1}
    for k in range(1, len(kpoints)):
        if abs(kpoints[k][0] - kpoints[k - 1][0]) < 1e-8:
            vertices.update((k - 1, k))
    named = []
    if poscar:
        try:
            from pymatgen.core import Structure
            named = list(_kpath_points(Structure.from_str(poscar, fmt='poscar'), dimension).items())
        except Exception:  # an unlabeled plot beats no plot
            named = []
    labels = [None] * len(kpoints)
    for k in vertices:
        frac = np.asarray(kpoints[k][1])
        labels[k] = next((name for name, point in named if np.allclose(frac, point, atol=1e-4)), '')
    return labels


def csv_breaks(kpoints):
    """Repeated distance with a different coordinate: a path discontinuity."""
    return [k for k in range(1, len(kpoints)) if abs(kpoints[k][0] - kpoints[k - 1][0]) < 1e-8
            and not np.allclose(kpoints[k][1], kpoints[k - 1][1], atol=1e-6)]


def _read_inputs(folder):
    """Read replot inputs; under a download receipt every file used must match its hash."""
    receipt_path = folder / 'download-receipt.json'
    receipt = json.loads(receipt_path.read_text(encoding='utf-8')).get('files', {}) if receipt_path.is_file() else None
    data, hashes = {}, {}
    for name in ('bands.csv', 'scf-summary.json', 'report.json', 'physics-model.json'):
        path = folder / name
        if not path.is_file():
            continue
        raw = path.read_bytes()
        digest = hashlib.sha256(raw).hexdigest()
        if receipt is not None:
            entry = receipt.get(name)
            if not isinstance(entry, dict) or entry.get('sha256') != digest:
                raise ReceiptMismatch(f'{name} 与下载收据不符，拒绝重绘。')
        data[name], hashes[name] = raw, digest
    if 'bands.csv' not in data:
        raise FileNotFoundError('没有 bands.csv，无法重绘。')
    return data, hashes, receipt is not None


def replot_folder(folder, ylim=DEFAULT_YLIM, title=None):
    """Redraw bands.replot.png/.pdf next to a finished result's bands.csv.

    The delivered bands.png/bands.pdf (hashed in download-receipt.json) are
    never touched; the replot and a small bands.replot.json with the input
    hashes are new files the receipt does not list. The folder's mtime is
    restored because latest_download picks the newest download by mtime.
    """
    from vasp_agent.workflow.band_analysis import parse_bands
    folder = Path(folder)
    data, hashes, receipt_verified = _read_inputs(folder)
    parsed = parse_bands(data['bands.csv'].decode('utf-8-sig'))
    documents = {name: json.loads(raw.decode('utf-8')) for name, raw in data.items() if name.endswith('.json')}
    scf, report = documents.get('scf-summary.json', {}), documents.get('report.json', {})
    physics = documents.get('physics-model.json', {})
    reference = report.get('plot_energy_reference') or {'kind': 'scf_fermi'}
    kind = reference.get('kind', 'scf_fermi')
    efermi = report.get('scf_fermi_energy_eV', scf.get('efermi'))
    offset = float(reference['energy_eV']) - float(efermi) if 'energy_eV' in reference and efermi is not None else 0.0
    kpoints = parsed['kpoints']
    # Exports with a k_label column carry the path's own labels (operator paths
    # included); older files are matched against the automatic path.
    labels = parsed.get('labels') or csv_labels(kpoints, scf.get('poscar'), physics.get('dimension'))
    ticks, _ = path_ticks([k[0] for k in kpoints], labels)
    breaks = csv_breaks(kpoints)
    energies = {spin: np.asarray(values) - offset for spin, values in parsed['energies'].items()}
    if title is None:
        title = plot_title(report.get('formula', ''), physics.get('functional', 'PBE'),
                           len(energies) > 1, physics.get('magnetism'))
    stat = folder.stat()
    png, pdf = folder / f'{REPLOT_STEM}.png', folder / f'{REPLOT_STEM}.pdf'
    try:
        save_figure(plot_bands([k[0] for k in kpoints], energies, ticks, breaks, kind, title, tuple(ylim)), png, pdf)
        record = {'inputs_sha256': hashes, 'receipt_verified': receipt_verified, 'reference_kind': kind,
                  'ylim': list(ylim), 'ticks': [label for _, label in ticks],
                  'labels_recovered': any(labels),
                  'outputs_sha256': {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in (png, pdf)},
                  'note': '本地重绘，仅改变绘图样式；原始 bands.png/bands.pdf 及下载收据未改动。'}
        (folder / f'{REPLOT_STEM}.json').write_text(json.dumps(record, ensure_ascii=False, indent=2), encoding='utf-8')
    finally:
        os.utime(folder, ns=(stat.st_atime_ns, stat.st_mtime_ns))
    return {'png': str(png), 'pdf': str(pdf), 'record': str(folder / f'{REPLOT_STEM}.json'), **record}
