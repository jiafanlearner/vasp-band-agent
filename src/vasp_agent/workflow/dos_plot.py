"""DOS figures in the band_plot style, and the bands+DOS figure with one energy reference.

Every figure here is drawn from dos.csv (and bands.csv for the combined one),
so the cluster export and a local replot of a download look the same. All
energies are relative to the SCF E_F: dos.csv's energy_minus_scf_ef_eV and
bands.csv's energy_minus_scf_ef_eV share that zero, which is why the two panels
of the combined figure can share an axis (bands.png alone may use the VBM for
an insulator; the combined figure never does).
"""
from __future__ import annotations

import csv
import hashlib
import io
import json
import math
import os
from pathlib import Path

import numpy as np

from vasp_agent.workflow.band_plot import LINE, REFERENCE, SPIN_COLORS, path_ticks, plot_title, save_figure

REPLOT_STEM = 'dos.replot'
COMBINED_STEM = 'bands-dos.replot'
COMBINED_NOTE = ('两图同一零点：SCF 的 E_F（bands.csv 与 dos.csv 的 energy_minus_scf_ef_eV）。'
                 '单独的 bands.png 对绝缘体可能以 VBM 为零点，合图不用 VBM。')
ELEMENT_COLORS = ('#c55a11', '#2e7d32', '#6a1b9a', '#00838f', '#ad1457', '#5d4037', '#283593', '#9e9d24')
FIXED_COLUMNS = ('energy_minus_scf_ef_eV', 'energy_eV', 'total_states_per_eV', 'integrated_states')
TAIL_COLUMNS = ('scf_fermi_eV', 'dos_fermi_eV')
DEFAULT_WINDOW = (-10.0, 5.0)


class DosDataError(ValueError):
    """dos.csv is missing columns or values."""


def parse_dos(text):
    """dos.csv -> {'energy', 'total', 'integrated', 'projections': {'La_d': array}, 'scf_fermi_eV', 'dos_fermi_eV'}."""
    reader = csv.reader(io.StringIO(text))
    header = [column.strip() for column in next(reader, [])]
    if tuple(header[:4]) != FIXED_COLUMNS or tuple(header[-2:]) != TAIL_COLUMNS:
        raise DosDataError('dos.csv 表头不符，应以 ' + ','.join(FIXED_COLUMNS) + ' 开始、以 ' + ','.join(TAIL_COLUMNS) + ' 结束。')
    rows = []
    for line, row in enumerate(reader, start=2):
        if not row:
            continue
        if len(row) != len(header):
            raise DosDataError(f'dos.csv 第 {line} 行列数不是 {len(header)}。')
        try:
            values = [float(x) for x in row]
        except ValueError:
            raise DosDataError(f'dos.csv 第 {line} 行含无法解析的数值。') from None
        if not all(math.isfinite(v) for v in values):
            raise DosDataError(f'dos.csv 第 {line} 行含非有限值。')
        rows.append(values)
    if len(rows) < 2:
        raise DosDataError('dos.csv 数据行不足。')
    data = np.asarray(rows)
    if not np.all(np.diff(data[:, 0]) > 0):
        raise DosDataError('dos.csv 能量不是严格递增。')
    names = header[4:-2]
    if any(not name.endswith('_states_per_eV') for name in names):
        raise DosDataError('dos.csv 投影列名无效。')
    return {'energy': data[:, 0], 'total': data[:, 2], 'integrated': data[:, 3],
            'projections': {name.removesuffix('_states_per_eV'): data[:, 4 + i] for i, name in enumerate(names)},
            'scf_fermi_eV': float(data[0, -2]), 'dos_fermi_eV': float(data[0, -1])}


def by_element(parsed):
    totals = {}
    for name, values in parsed['projections'].items():
        element = name.rsplit('_', 1)[0]
        totals[element] = totals.get(element, 0) + values
    return totals


def _axes_dos(ax, parsed, window, vertical=False):
    """Total DOS and element-projected DOS; energy on x (standalone) or y (beside bands)."""
    energy = parsed['energy']
    mask = (energy >= window[0]) & (energy <= window[1])
    series = [('total', parsed['total'], LINE, 1.2)] + [
        (element, values, ELEMENT_COLORS[i % len(ELEMENT_COLORS)], 0.9)
        for i, (element, values) in enumerate(by_element(parsed).items())]
    for label, values, color, width in series:
        x, y = (values[mask], energy[mask]) if vertical else (energy[mask], values[mask])
        ax.plot(x, y, color=color, lw=width, label=label)
    shift = parsed['dos_fermi_eV'] - parsed['scf_fermi_eV']
    line = ax.axhline if vertical else ax.axvline
    line(0, color=REFERENCE, lw=0.8, ls='--', zorder=0)
    if abs(shift) > 1e-4:
        line(shift, color='0.55', lw=0.6, ls=':', zorder=0)
    peak = max(float(values[mask].max()) for _, values, _, _ in series) if mask.any() else 1.0
    if vertical:
        ax.set_xlim(0, 1.05 * peak)
        ax.set_ylim(*window)
        ax.set_xlabel('DOS (states/eV/cell)', fontsize=10)
    else:
        ax.set_xlim(*window)
        ax.set_ylim(0, 1.05 * peak)
        ax.set_xlabel('$E - E_\\mathrm{F}$ (eV)', fontsize=11)
        ax.set_ylabel('DOS (states/eV/cell)', fontsize=11)
    ax.tick_params(labelsize=10, direction='in')
    ax.legend(loc='upper right', fontsize=8, frameon=False, handlelength=1.5)


def _agg():
    import matplotlib
    if matplotlib.get_backend().lower() != 'agg':
        matplotlib.use('Agg')
    import matplotlib.pyplot as plt
    return plt


def plot_dos(parsed, window=DEFAULT_WINDOW, title=None):
    plt = _agg()
    fig, ax = plt.subplots(figsize=(7.0, 4.2))
    _axes_dos(ax, parsed, window)
    ax.text(1.0, 1.01, '$E_\\mathrm{F}$ from SCF (dotted: DOS-run $E_\\mathrm{F}$)', transform=ax.transAxes,
            ha='right', va='bottom', fontsize=8, color=REFERENCE)
    if title:
        ax.set_title(title, fontsize=11, loc='left')
    fig.tight_layout()
    return fig


def plot_combined(bands, parsed, window=DEFAULT_WINDOW, title=None):
    """Bands (left) and DOS (right) on one energy axis: E - E_F(SCF)."""
    plt = _agg()
    fig, (left, right) = plt.subplots(1, 2, figsize=(9.0, 4.6), sharey=True, gridspec_kw={'width_ratios': [2.6, 1]})
    distances = np.asarray([k[0] for k in bands['kpoints']], dtype=float)
    labels = bands.get('labels') or [None] * len(distances)
    ticks, breaks = path_ticks(distances, labels)
    segments = [s for s in np.split(np.arange(len(distances)), sorted(set(breaks))) if len(s)]
    polarized = len(bands['energies']) > 1
    for index, values in enumerate(bands['energies'].values()):
        for band in np.asarray(values, dtype=float):
            for segment in segments:
                left.plot(distances[segment], band[segment], color=SPIN_COLORS[index % 2] if polarized else LINE, lw=1.0)
    left.axhline(0, color=REFERENCE, lw=0.8, ls='--', zorder=0)
    for xt, _ in ticks:
        if distances[0] + 1e-9 < xt < distances[-1] - 1e-9:
            left.axvline(xt, color='0.75', lw=0.6, zorder=0)
    left.set_xticks([t[0] for t in ticks], [t[1] for t in ticks], fontsize=10)
    left.set_xlim(distances[0], distances[-1])
    left.set_ylim(*window)
    left.set_ylabel('$E - E_\\mathrm{F}$ (eV)', fontsize=11)
    left.tick_params(axis='y', labelsize=10, direction='in')
    left.tick_params(axis='x', length=0, pad=6)
    _axes_dos(right, parsed, window, vertical=True)
    right.text(1.0, 1.01, '$E_\\mathrm{F}$ from SCF (both panels)', transform=right.transAxes, ha='right',
               va='bottom', fontsize=8, color=REFERENCE)
    if title:
        left.set_title(title, fontsize=11, loc='left')
    fig.tight_layout()
    return fig


def save(fig, png, pdf):
    save_figure(fig, png, pdf)


def title_for(root):
    root = Path(root)
    formula = ''
    for name in ('manifest.json', 'report.json'):
        path = root / name
        if path.is_file():
            formula = json.loads(path.read_text(encoding='utf-8')).get('formula') or formula
            if formula:
                break
    return plot_title(formula, 'PBE', False) + ' · DOS'


# --- Local replot of a downloaded DOS ------------------------------------------------

def _read(folder, names):
    receipt_path = folder / 'download-receipt.json'
    receipt = json.loads(receipt_path.read_text(encoding='utf-8')).get('files', {}) if receipt_path.is_file() else None
    data, hashes = {}, {}
    for name in names:
        path = folder / name
        if not path.is_file():
            continue
        raw = path.read_bytes()
        digest = hashlib.sha256(raw).hexdigest()
        if receipt is not None:
            entry = receipt.get(name)
            if not isinstance(entry, dict) or entry.get('sha256') != digest:
                from vasp_agent.workflow.band_plot import ReceiptMismatch
                raise ReceiptMismatch(f'{name} 与下载收据不符，拒绝重绘。')
        data[name], hashes[name] = raw, digest
    return data, hashes, receipt is not None


def replot_folder(folder, window=None):
    """Redraw dos.replot.png/.pdf (and bands-dos.replot.* when bands.csv exists) from a verified dos.csv.

    The delivered figures and the download receipt are never touched; the
    folder's mtime is restored (latest_download picks the newest by mtime).
    """
    folder = Path(folder)
    data, hashes, verified = _read(folder, ('dos.csv', 'bands.csv', 'dos-report.json', 'report.json'))
    if 'dos.csv' not in data:
        raise FileNotFoundError('没有 dos.csv，无法重绘 DOS。')
    parsed = parse_dos(data['dos.csv'].decode('utf-8-sig'))
    report = json.loads(data['dos-report.json'].decode('utf-8')) if 'dos-report.json' in data else {}
    if window is None:
        values = report.get('values') or {}
        window = (values.get('window_min_eV', DEFAULT_WINDOW[0]), values.get('window_max_eV', DEFAULT_WINDOW[1]))
    window = (float(window[0]), float(window[1]))
    if not window[0] < window[1]:
        raise ValueError('能量下限必须小于上限。')
    formula = json.loads(data['report.json'].decode('utf-8')).get('formula', '') if 'report.json' in data else ''
    title = plot_title(formula, 'PBE', False) + ' · DOS'
    stat = folder.stat()
    outputs = [folder / f'{REPLOT_STEM}.png', folder / f'{REPLOT_STEM}.pdf']
    try:
        save(plot_dos(parsed, window, title), *outputs)
        if 'bands.csv' in data:
            from vasp_agent.workflow.band_analysis import parse_bands
            bands = parse_bands(data['bands.csv'].decode('utf-8-sig'))
            combined = [folder / f'{COMBINED_STEM}.png', folder / f'{COMBINED_STEM}.pdf']
            save(plot_combined(bands, parsed, window, title), *combined)
            outputs += combined
        record = {'inputs_sha256': hashes, 'receipt_verified': verified, 'energy_reference': 'scf_fermi',
                  'window_eV': list(window), 'combined_note': COMBINED_NOTE if 'bands.csv' in data else None,
                  'outputs_sha256': {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in outputs},
                  'note': '本地重绘，仅改变绘图范围与样式；原始 dos.png/dos.pdf 及下载收据未改动。'}
        (folder / f'{REPLOT_STEM}.json').write_text(json.dumps(record, ensure_ascii=False, indent=2), encoding='utf-8')
    finally:
        os.utime(folder, ns=(stat.st_atime_ns, stat.st_mtime_ns))
    return {'files': [p.name for p in outputs], 'record': str(folder / f'{REPLOT_STEM}.json'), **record}
