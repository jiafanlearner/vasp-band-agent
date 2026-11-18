"""Deterministic, read-only analysis of an exported band structure.

The model used to write a fresh Python script for every routine question about
a finished band calculation (gap, VBM/CBM, metallicity, degeneracy). Such
scripts need the Docker sandbox or an operator approval to run, so the standard
case stalled on machines without Docker. This module answers the standard
questions from bands.csv (written by core.export_bands) plus the SCF summary,
with fixed code that executes no model-written text and writes no files.

All energies are relative to the SCF Fermi level, as stored in the
energy_minus_scf_ef_eV column.
"""
from __future__ import annotations

import csv
import io
import math

COLUMNS = ('spin', 'band', 'k_index', 'distance_inv_angstrom', 'kx_fractional', 'ky_fractional',
           'kz_fractional', 'energy_minus_scf_ef_eV', 'energy_minus_plot_reference_eV')
EXTRA_COLUMNS = ('k_label', 'path_source')
DEGENERACY_TOL = 1e-3
# Same tolerance as pymatgen BandStructure.is_metal: an insulator's E_F often
# sits exactly at the VBM, which must not count as a crossing.
FERMI_TOL = 1e-4
TABLE_MAX_ROWS = 200
MAX_BANDS_CSV_BYTES = 32 * 1024 * 1024
LIMITATIONS = [
    '极值只来自采样的高对称路径，真实带边可能不在路径上。',
    'PBE Kohn-Sham 带隙不是实验或准粒子带隙，通常低估。',
    '所有能量相对 SCF 费米能级 E_F（energy_minus_scf_ef_eV 列）。',
    '高对称点标签仅按分数坐标识别 Γ；其他点只给出坐标。',
]


class BandDataError(ValueError):
    """bands.csv cannot be analysed as a complete band structure."""

    def __init__(self, message, shape=None):
        super().__init__(message)
        self.shape = shape


def _is_gamma(frac):
    return all(abs(value - round(value)) < 1e-6 for value in frac)


def _label(frac):
    return 'Γ' if _is_gamma(frac) else None


def parse_bands(text):
    """Parse bands.csv into a dense grid and check its shape.

    Returns spins, nk, nbands, per-k (distance, frac) and energies[spin][band][k].
    Any missing, duplicated or non-finite value refuses the file, because a gap
    computed from a partial grid would look plausible while being wrong.
    """
    reader = csv.reader(io.StringIO(text))
    header = next(reader, None)
    header = tuple(column.strip() for column in header) if header is not None else None
    # Newer exports append k_label/path_source; the original columns are unchanged.
    if header not in (COLUMNS, COLUMNS + EXTRA_COLUMNS):
        raise BandDataError(f'bands.csv 表头不符，应为 {",".join(COLUMNS)}。')
    width = len(header)
    labels = {}
    values = {}
    kpoints = {}
    spins, bands = [], set()
    rows = 0
    for line, row in enumerate(reader, start=2):
        if not row:
            continue
        if len(row) != width:
            raise BandDataError(f'bands.csv 第 {line} 行列数不是 {width}。')
        try:
            spin = row[0].strip()
            band, k = int(row[1]), int(row[2])
            distance, kx, ky, kz, energy = (float(row[i]) for i in (3, 4, 5, 6, 7))
        except ValueError:
            raise BandDataError(f'bands.csv 第 {line} 行含无法解析的数值。') from None
        if not all(math.isfinite(v) for v in (distance, kx, ky, kz, energy)) or band < 1 or k < 0:
            raise BandDataError(f'bands.csv 第 {line} 行数值无效（非有限值或索引越界）。')
        if (spin, band, k) in values:
            raise BandDataError(f'bands.csv 第 {line} 行重复 spin={spin} band={band} k={k}。')
        point = (distance, (kx, ky, kz))
        if k in kpoints and any(abs(a - b) > 1e-6 for a, b in zip((point[0], *point[1]), (kpoints[k][0], *kpoints[k][1]))):
            raise BandDataError(f'k_index={k} 在不同能带中的路径距离或坐标不一致。')
        kpoints.setdefault(k, point)
        if width > len(COLUMNS):
            labels.setdefault(k, row[len(COLUMNS)].strip() or None)
        if spin not in spins:
            spins.append(spin)
        bands.add(band)
        values[spin, band, k] = energy
        rows += 1
    if not rows:
        raise BandDataError('bands.csv 没有数据行。')
    nk, nbands = len(kpoints), len(bands)
    expected = nk * nbands * len(spins)
    shape = {'rows': rows, 'spins': spins, 'k_points': nk, 'bands': nbands,
             'expected_rows': expected, 'rows_match': rows == expected}
    if sorted(kpoints) != list(range(nk)) or sorted(bands) != list(range(1, nbands + 1)):
        raise BandDataError('k_index 或 band 编号不连续。', shape)
    if rows != expected:
        raise BandDataError(f'行数 {rows} ≠ k 点 {nk} × 能带 {nbands} × 自旋 {len(spins)} = {expected}，数据不完整。', shape)
    energies = {spin: [[values[spin, band, k] for k in range(nk)] for band in range(1, nbands + 1)]
                for spin in spins}
    return {'shape': shape, 'kpoints': [kpoints[k] for k in range(nk)], 'energies': energies,
            'labels': [labels.get(k) for k in range(nk)] if labels else None}


def path_segments(kpoints):
    """Split the path where the distance stops increasing.

    pymatgen line mode repeats a vertex at the start of each branch with the
    same distance: an identical coordinate is a joint of a continuous path, a
    different coordinate is a discontinuity ("X|U"). A direction change between
    consecutive steps without a repeated point is reported as a kink.
    """
    segments, start = [], 0
    breaks = []
    for k in range(1, len(kpoints)):
        if abs(kpoints[k][0] - kpoints[k - 1][0]) < 1e-8:
            same = all(abs(a - b) < 1e-6 for a, b in zip(kpoints[k][1], kpoints[k - 1][1]))
            breaks.append({'k_index': k, 'kind': 'joint' if same else 'discontinuity'})
            segments.append((start, k - 1))
            start = k
    segments.append((start, len(kpoints) - 1))
    kinks = []
    for first, last in segments:
        for k in range(first + 1, last):
            a = [x - y for x, y in zip(kpoints[k][1], kpoints[k - 1][1])]
            b = [x - y for x, y in zip(kpoints[k + 1][1], kpoints[k][1])]
            na, nb = math.sqrt(sum(x * x for x in a)), math.sqrt(sum(x * x for x in b))
            if na > 1e-9 and nb > 1e-9 and sum(x * y for x, y in zip(a, b)) / (na * nb) < 1 - 1e-6:
                kinks.append(k)
    steps = [kpoints[k][0] - kpoints[k - 1][0] for k in range(1, len(kpoints))]
    positive = sorted(step for step in steps if step > 1e-8)
    median = positive[len(positive) // 2] if positive else 0.0
    jumps = [k for k, step in enumerate(steps, start=1) if median and step > 5 * median]
    if any(step < -1e-8 for step in steps):
        raise BandDataError('路径距离出现递减，bands.csv 不是按路径顺序导出的。')
    described = [{'start_k': a, 'end_k': b, 'start_frac': _round(kpoints[a][1]), 'end_frac': _round(kpoints[b][1]),
                  'start_label': _label(kpoints[a][1]), 'end_label': _label(kpoints[b][1]),
                  'length_inv_angstrom': round(kpoints[b][0] - kpoints[a][0], 6)} for a, b in segments]
    return {'ranges': segments, 'segment_count': len(segments), 'segments': described[:50], 'segments_truncated': len(segments) > 50,
            'breaks': breaks[:50], 'kinks_k_index': kinks[:50], 'distance_jumps_k_index': jumps[:50],
            'median_step_inv_angstrom': round(median, 8)}


def _round(frac):
    return [round(value, 6) for value in frac]


def _point(kpoints, k, energy):
    return {'energy_eV': round(energy, 6), 'k_index': k, 'frac': _round(kpoints[k][1]),
            'label': _label(kpoints[k][1]), 'distance_inv_angstrom': round(kpoints[k][0], 6)}


def fermi_crossings(bands, segments):
    """Count sign changes of E-E_F along each band inside each continuous segment.

    Points within FERMI_TOL of E_F carry no sign, so a band that only touches
    E_F is not a crossing and passing through E_F at a grid point counts once.
    """
    counts = {}
    for index, energies in enumerate(bands, start=1):
        total = 0
        for first, last in segments:
            previous = 0
            for k in range(first, last + 1):
                sign = 1 if energies[k] > FERMI_TOL else -1 if energies[k] < -FERMI_TOL else 0
                if sign and previous and sign != previous:
                    total += 1
                previous = sign or previous
        if total:
            counts[index] = total
    return counts


def gamma_degeneracy(kpoints, bands, window):
    """Group band energies at each Γ occurrence within DEGENERACY_TOL."""
    first, last = window
    result = []
    for k, (_, frac) in enumerate(kpoints):
        if not _is_gamma(frac):
            continue
        ordered = sorted((bands[b][k], b + 1) for b in range(first - 1, last))
        groups, current = [], [ordered[0]] if ordered else []
        for item in ordered[1:]:
            if item[0] - current[-1][0] <= DEGENERACY_TOL:
                current.append(item)
            else:
                groups.append(current)
                current = [item]
        if current:
            groups.append(current)
        result.append({'k_index': k, 'groups': [
            {'bands': [b for _, b in group], 'degeneracy': len(group),
             'energy_eV': round(sum(e for e, _ in group) / len(group), 6)} for group in groups]})
    return result[:6]


def analyze_bands(text, scf_summary=None, report=None, table_offset=0, table_limit=0):
    """Return a bounded JSON-able analysis of one bands.csv text."""
    parsed = parse_bands(text)
    shape, kpoints, energies = parsed['shape'], parsed['kpoints'], parsed['energies']
    flags = []
    path = path_segments(kpoints)
    segments = path.pop('ranges')
    nbands = shape['bands']
    scf = scf_summary or {}
    if scf.get('nbands') is not None and int(scf['nbands']) != nbands:
        flags.append(f'scf-summary nbands={scf["nbands"]} 与 bands.csv 能带数 {nbands} 不一致。')
    spin_polarized = len(shape['spins']) > 1
    occupation = {'nelect': scf.get('nelect'), 'occupied_bands': None, 'method': None}
    if spin_polarized:
        flags.append('自旋极化数据：仅由 NELECT 无法确定每个自旋通道的占据数，未计算带隙。')
    elif scf.get('nelect') is None:
        flags.append('缺少 scf-summary.json 的 NELECT，无法按电子数确定占据带；只给出相对 E_F 的判断。')
    else:
        half = float(scf['nelect']) / 2
        if abs(half - round(half)) > 1e-6:
            flags.append(f'NELECT={scf["nelect"]} 为奇数或非整数，非自旋极化下 NELECT/2 不是整数，拒绝按电子数确定占据带。')
        else:
            occupation.update(occupied_bands=int(round(half)), method='nelect/2')
    spin = shape['spins'][0]
    bands = energies[spin]

    crossings = {s: fermi_crossings(energies[s], segments) for s in shape['spins']}
    crossing_bands = {s: sorted(c) for s, c in crossings.items()}
    metallic_by_fermi = any(crossing_bands.values()) or any(
        min(band) < -FERMI_TOL and max(band) > FERMI_TOL for s in shape['spins'] for band in energies[s])
    highest_band_min = min(min(energies[s][-1]) for s in shape['spins'])
    result = {
        'data_shape': shape,
        'scf_fermi_reference': {'efermi_eV': scf.get('efermi'), 'energies_relative_to': 'SCF E_F'},
        'occupation': occupation,
        'metallic_check': {
            'bands_crossing_scf_ef': {s: c for s, c in crossing_bands.items() if c},
            'fermi_crossing_count': sum(sum(c.values()) for c in crossings.values()),
            'crossings_per_band': {s: c for s, c in crossings.items() if c},
            'metallic_on_sampled_path': bool(metallic_by_fermi),
        },
        'highest_band_check': {
            'highest_band': nbands, 'min_energy_eV': round(highest_band_min, 6),
            'fully_above_ef': highest_band_min > 0,
            'note': '最高能带完全位于 E_F 之上才说明已包含空带；否则需增加 NBANDS。'},
        'path': path,
    }
    if highest_band_min <= 0:
        flags.append('最高能带并非全部高于 E_F，能带数可能不足。')

    n_occ = occupation['occupied_bands']
    if n_occ is not None:
        if n_occ >= nbands:
            flags.append(f'占据带数 {n_occ} ≥ 能带数 {nbands}，没有导带，无法计算带隙。')
        elif n_occ < 1:
            flags.append('占据带数小于 1，无法计算带隙。')
        else:
            valence, conduction = bands[n_occ - 1], bands[n_occ]
            kv = max(range(len(kpoints)), key=valence.__getitem__)
            kc = min(range(len(kpoints)), key=conduction.__getitem__)
            vbm, cbm = valence[kv], conduction[kc]
            direct = [conduction[k] - valence[k] for k in range(len(kpoints))]
            kd = min(range(len(kpoints)), key=direct.__getitem__)
            gap = cbm - vbm
            overlap = gap <= 0
            result['band_edges'] = {
                'valence_band': n_occ, 'conduction_band': n_occ + 1,
                'vbm': _point(kpoints, kv, vbm), 'cbm': _point(kpoints, kc, cbm),
                'gap_eV': round(gap, 6) if not overlap else 0.0,
                'band_overlap_eV': round(-gap, 6) if overlap else None,
                'gap_type': None if overlap else ('direct' if _same_k(kpoints, kv, kc) else 'indirect'),
                'smallest_direct_gap': {**_point(kpoints, kd, direct[kd]), 'gap_eV': round(direct[kd], 6)},
            }
            del result['band_edges']['smallest_direct_gap']['energy_eV']
            result['metallic_check']['metallic_by_occupation'] = overlap
            if overlap != metallic_by_fermi:
                flags.append('按电子数占据与按 E_F 交叉的金属性判断不一致，需检查 E_F 或 NELECT。')
            if not overlap and not (vbm <= 1e-3 and cbm >= -1e-3):
                flags.append('VBM/CBM 未包围 SCF E_F（容差 1 meV），E_F 可能不在带隙内，需核对。')
    window = (max(1, n_occ - 3), min(nbands, n_occ + 4)) if n_occ and n_occ < nbands else (1, min(nbands, 40))
    result['gamma_degeneracy'] = {'tolerance_eV': DEGENERACY_TOL, 'spin': spin, 'band_window': list(window),
                                  'gamma_points': gamma_degeneracy(kpoints, bands, window)}
    if not result['gamma_degeneracy']['gamma_points']:
        flags.append('路径中没有 Γ 点（分数坐标全为整数），未做 Γ 简并分析。')
    if report and isinstance(report.get('sampled_path_gap'), dict):
        result['report_sampled_path_gap'] = {key: report['sampled_path_gap'].get(key)
                                             for key in ('energy', 'direct', 'transition')}
    if table_limit:
        rows = []
        stop = min(len(kpoints), table_offset + min(table_limit, TABLE_MAX_ROWS))
        edge = (n_occ - 1, n_occ) if n_occ and n_occ < nbands else None
        for k in range(table_offset, stop):
            row = {'k_index': k, 'distance': round(kpoints[k][0], 6), 'frac': _round(kpoints[k][1]),
                   'label': _label(kpoints[k][1])}
            if edge:
                row.update(vb_eV=round(bands[edge[0]][k], 6), cb_eV=round(bands[edge[1]][k], 6),
                           direct_gap_eV=round(bands[edge[1]][k] - bands[edge[0]][k], 6))
            else:
                below = [b[k] for b in bands if b[k] <= 0]
                above = [b[k] for b in bands if b[k] > 0]
                row.update(highest_below_ef_eV=round(max(below), 6) if below else None,
                           lowest_above_ef_eV=round(min(above), 6) if above else None)
            rows.append(row)
        result['k_table'] = {'spin': spin, 'offset': table_offset, 'rows': rows,
                             'next_offset': stop if stop < len(kpoints) else None, 'total_k_points': len(kpoints)}
    result['flags'] = flags
    result['limitations'] = LIMITATIONS
    return result


def _same_k(kpoints, a, b):
    return a == b or all(abs(x - y) < 1e-6 for x, y in zip(kpoints[a][1], kpoints[b][1]))
