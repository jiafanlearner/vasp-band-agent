"""Deterministic candidate band paths (SC, Hinuma, layered in-plane, extrema)."""
import hashlib
import json
import warnings
from pathlib import Path

import numpy as np

import vasp_agent.workflow.vasp_band_agent as core


# A conventional cell whose unique axis is at least this many times its longest
# in-plane axis is offered an in-plane (2D sublattice) path. 2.5 is about 1.5x
# the ideal hcp ratio 1.633, so close-packed and tetragonally distorted cubic
# cells stay below it, while layered cuprates (La2CuO4 ~3.5, YBCO ~3.1), MoS2
# (~3.9) and Bi2Se3 (~6.9) are above it. A false positive costs one extra menu
# line; nothing is chosen without the operator.
LAYERED_RATIO = 2.5
MAX_EXTREMA_POINTS = 6
LAYERED_SYSTEMS = ('tetragonal', 'hexagonal', 'trigonal', 'orthorhombic')
# In-plane points in the conventional reciprocal basis (k3 = 0) and their path.
IN_PLANE = {'square': ({'X': (.5, 0), 'M': (.5, .5)}, ['Γ', 'X', 'M', 'Γ']),
            'hexagonal': ({'M': (.5, 0), 'K': (1 / 3, 1 / 3)}, ['Γ', 'M', 'K', 'Γ']),
            'rectangular': ({'X': (.5, 0), 'S': (.5, .5), 'Y': (0, .5)}, ['Γ', 'X', 'S', 'Y', 'Γ'])}
IN_PLANE_NAMES = {'square': ('正方', 'X=(π/a,0,0)、M=(π/a,π/a,0)'), 'hexagonal': ('六角', 'M、K 为面内六角记号'),
                  'rectangular': ('矩形', 'X=(π/a,0,0)、S=(π/a,π/b,0)、Y=(0,π/b,0)')}


def _text(record):
    """Path text of a resolved record, labels only (every point comes from a table)."""
    return '|'.join('-'.join(p['label'] for p in s['points']) for s in record['segments'])


def _route(record):
    return [[(p['label'], [round(x, 6) for x in p['frac']]) for p in s['points']] for s in record['segments']]


def _same_route(a, b):
    ra, rb = _route(a), _route(b)
    return len(ra) == len(rb) and all(
        len(x) == len(y) and all(np.allclose(p[1], q[1], atol=1e-5) for p, q in zip(x, y)) for x, y in zip(ra, rb))


def _layered(structure, primitive, symprec):
    """(path text, rationale) for the in-plane path of a layered cell, or (None, why not)."""
    from pymatgen.symmetry.analyzer import SpacegroupAnalyzer
    from pymatgen.symmetry.bandstructure import HighSymmKpath
    from vasp_agent.workflow import band_path as band_paths
    with warnings.catch_warnings():
        warnings.simplefilter('ignore')
        system = SpacegroupAnalyzer(structure, symprec=symprec).get_crystal_system()
        sc = HighSymmKpath(structure, symprec=symprec)
    conventional = sc.conventional.lattice
    a, b, c = conventional.abc
    ratio = c / max(a, b)
    if system not in LAYERED_SYSTEMS or ratio < LAYERED_RATIO:
        return None, f'非层状：{system}，惯用晶胞 c/max(a,b) = {ratio:.2f} < {LAYERED_RATIO}'
    # Same standardization, same Cartesian frame: the conventional cell's in-plane
    # points can be written in the bands cell's reciprocal basis.
    if not np.allclose(sc.prim.lattice.matrix, primitive.lattice.matrix, atol=1e-4):
        return None, '能带原胞与标准化惯用晶胞取向不一致，无法换算面内坐标'
    gamma = conventional.gamma
    kind = ('hexagonal' if abs(gamma - 120) < 1 and abs(a - b) < 1e-3 * a else
            'square' if abs(gamma - 90) < 1 and abs(a - b) < 1e-3 * a else
            'rectangular' if abs(gamma - 90) < 1 else None)
    if kind is None:
        return None, '面内晶格不是正方、六角或矩形'
    points, order = IN_PLANE[kind]
    to_cartesian = np.asarray(conventional.reciprocal_lattice.matrix)
    to_fraction = np.asarray(primitive.lattice.reciprocal_lattice.matrix)

    def explicit(name):
        if name == 'Γ':
            return 'Γ'
        u, v = points[name]
        frac = np.linalg.solve(to_fraction.T, np.array([u, v, 0.]) @ to_cartesian)
        return name + '(' + ','.join(f'{0.0 if abs(x) < 5e-7 else x:.6g}' for x in frac) + ')'
    text = '-'.join(explicit(name) for name in order)
    # A short kz segment to the convention's own point on the c* axis nearest Γ.
    cartesian = {k: np.asarray(v) @ to_fraction for k, v in sc.kpath['kpoints'].items()}
    axis = sorted((v[2], k) for k, v in cartesian.items() if abs(v[0]) < 1e-6 and abs(v[1]) < 1e-6 and v[2] > 1e-6)
    if axis:
        text += '-' + band_paths.pretty(axis[0][1])
    shape, names = IN_PLANE_NAMES[kind]
    rationale = (f'层状判据：惯用晶胞 c/max(a,b) = {ratio:.2f} ≥ {LAYERED_RATIO}。沿二维{shape}子晶格走面内路径'
                 f'（{names}；二维记号，与三维约定的同名标签不是同一点），便于对照 Wannier/紧束缚模型'
                 + (f'；末段 Γ–{band_paths.pretty(axis[0][1])} 看 kz 色散' if axis else '') + '。')
    return text, rationale


def _extrema(bands_text, table, primitive):
    """(path text, rationale) through table points nearest the first run's E_F crossings or band edges."""
    from vasp_agent.workflow import band_path as band_paths
    from vasp_agent.workflow.band_analysis import FERMI_TOL, parse_bands, path_segments
    parsed = parse_bands(bands_text)
    kpoints = parsed['kpoints']
    ranges = path_segments(kpoints)['ranges']
    hits = []
    for bands in parsed['energies'].values():
        for band in bands:
            for first, last in ranges:
                for k in range(first, last):
                    a, b = band[k], band[k + 1]
                    if (a < -FERMI_TOL and b > FERMI_TOL) or (a > FERMI_TOL and b < -FERMI_TOL):
                        hits.append(k if abs(a) <= abs(b) else k + 1)
    kind = 'E_F 交叉'
    if not hits:
        kind = '带边（VBM/CBM）'
        values = [(value, k) for bands in parsed['energies'].values() for band in bands for k, value in enumerate(band)]
        below = [item for item in values if item[0] <= FERMI_TOL]
        above = [item for item in values if item[0] > FERMI_TOL]
        hits = ([max(below)[1]] if below else []) + ([min(above)[1]] if above else [])
    reciprocal = np.asarray(primitive.lattice.reciprocal_lattice.matrix)
    names = list(table)
    vectors = np.asarray([table[k] for k in names]) @ reciprocal
    counts, first_seen = {}, {}
    for k in hits:
        distances = np.linalg.norm(vectors - np.asarray(kpoints[k][1]) @ reciprocal, axis=1)
        name = names[int(np.argmin(distances))]
        counts[name] = counts.get(name, 0) + 1
        first_seen.setdefault(name, k)
    chosen = sorted(sorted(counts, key=lambda n: (-counts[n], first_seen[n]))[:MAX_EXTREMA_POINTS], key=lambda n: first_seen[n])
    gamma = next((n for n in table if band_paths.norm(n) == 'GAMMA'), None)
    route = ([gamma] + [n for n in chosen if n != gamma]) if gamma else chosen
    if len(route) < 2:
        return None, f'第一次结果的{kind}都最靠近 Γ，无法组成不同的路径'
    rationale = (f'第一次结果有 {len(hits)} 处 {kind}，最靠近 ' + '、'.join(band_paths.pretty(n) for n in chosen) +
                 '；只依据已采样路径，不是全布里渊区搜索，路径外可能另有交叉或极值。')
    return '-'.join(band_paths.pretty(n) for n in route), rationale


def candidates(config, structure, primitive, bands_text=None, first_run=None, extra=None, extra_rationale=None):
    """Reasonable paths for the bands cell, each validated and resolved like an operator path.

    Deterministic for the same inputs. A candidate that fails validate() or
    record_for() is never offered; it is listed under 'omitted' with the reason.
    """
    from vasp_agent.workflow import band_path as band_paths
    from vasp_agent.workflow.dimensionality import is_2d
    base = {k: v for k, v in config.items() if k != 'band_path'}
    automatic = band_paths.record_for(base, structure, primitive)
    if first_run is None and config.get('band_path') is None:
        first_run = automatic
    offered, omitted = [], []

    def offer(key, title, text, rationale, origin='program'):
        if text is None:
            omitted.append({'key': key, 'title': title, 'reason': rationale})
            return
        try:
            record = band_paths.record_for({**base, 'band_path': band_paths.validate(text, base['points'])}, structure, primitive)
        except (ValueError, ImportError) as exc:
            omitted.append({'key': key, 'title': title, 'text': text, 'reason': str(exc)})
            return
        duplicate = next((c['title'] for c, r in offered if _same_route(r, record)), None)
        if duplicate:
            omitted.append({'key': key, 'title': title, 'text': text, 'reason': f'与“{duplicate}”路径相同'})
            return
        offered.append(({'key': key, 'title': title, 'origin': origin, 'text': band_paths.render(record['requested']),
                         'resolved': _route(record), 'total_kpoints': record['total_kpoints'], 'rationale': rationale,
                         'used_by_first_run': first_run is not None and _same_route(record, first_run),
                         'selectable': True}, record))

    if is_2d(config):
        entry = {'key': 'automatic', 'title': '自动（面内）', 'origin': 'program', 'text': _text(automatic),
                 'resolved': _route(automatic), 'total_kpoints': automatic['total_kpoints'],
                 'rationale': '二维 slab 的能带晶胞未标准化，只支持自动面内路径。', 'used_by_first_run': True, 'selectable': False}
        return {'version': band_paths.SPEC_VERSION, 'candidates': [entry], 'cell': automatic['cell'], 'lattice_type': automatic['lattice_type'],
                'omitted': [{'key': k, 'reason': '二维 slab 只支持自动面内路径'} for k in ('hinuma', 'layered', 'extrema')],
                'note': '二维 slab 没有可换的路径。'}
    offer('automatic', '自动（Setyawan–Curtarolo）', _text(automatic),
          f"{automatic['lattice_type']} 的标准路径，经过该约定的全部高对称点。")
    try:
        from pymatgen.symmetry.bandstructure import HighSymmKpath
        band_paths.tables('hinuma', structure, primitive, base['symprec'])  # refuses clearly without seekpath
        with warnings.catch_warnings():
            warnings.simplefilter('ignore')
            route = HighSymmKpath(primitive, symprec=base['symprec'], path_type='hinuma').kpath['path']
        offer('hinuma', 'Hinuma（seekpath）', 'hinuma:' + '|'.join('-'.join(band_paths.pretty(n) for n in seg) for seg in route),
              'Hinuma 等人的约定（seekpath），标签和分段不同，便于对照使用 seekpath 的文献或软件。')
    except (ValueError, ImportError) as exc:
        offer('hinuma', 'Hinuma（seekpath）', None, str(exc))
    offer('layered', '层状面内', *_layered(structure, primitive, base['symprec']))
    if bands_text:
        table, _, _ = band_paths.tables('setyawan_curtarolo', structure, primitive, base['symprec'])
        try:
            offer('extrema', '靠近交叉/带边', *_extrema(bands_text, table, primitive))
        except ValueError as exc:
            offer('extrema', '靠近交叉/带边', None, f'bands.csv 无法分析：{exc}')
    else:
        offer('extrema', '靠近交叉/带边', None, '没有第一次计算的 bands.csv')
    if extra:
        offer('ai_suggestion', 'AI 建议', extra, (extra_rationale or '').strip()[:300] or '（未附理由）', origin='ai_suggestion')
    return {'version': band_paths.SPEC_VERSION, 'candidates': [c for c, _ in offered], 'omitted': omitted,
            'cell': automatic['cell'], 'lattice_type': automatic['lattice_type'],
            'note': '候选均已在能带原胞上解析；选中后新建任务复用来源 SCF，只重算能带，需重新审阅和单独提交授权。'}


def candidates_for_task(root, extra=None, extra_rationale=None):
    """Candidates for a task whose SCF passed, from its verified cell files."""
    from pymatgen.io.vasp.inputs import Poscar
    from vasp_agent.workflow import band_path as band_paths
    from vasp_agent.workflow import stage_gate
    root = Path(root)
    core.verify(root)
    checkpoint = json.loads((root / 'checkpoint.json').read_text(encoding='utf-8')) if (root / 'checkpoint.json').is_file() else {}
    if 'scf' not in checkpoint.get('completed', []):
        raise ValueError('来源 SCF 尚未通过验收，不能给出换路径候选。')
    stage_gate.evidence(root, 'bands')  # scf/POSCAR, relaxed-2.POSCAR and CHGCAR as accepted
    config = json.loads((root / 'config.json').read_text(encoding='utf-8'))
    first, bands = root / band_paths.RECORD, root / 'bands.csv'
    data = candidates(config, Poscar.from_file(root / 'relaxed-2.POSCAR').structure,
                      Poscar.from_file(root / 'scf/POSCAR').structure,
                      bands.read_text(encoding='utf-8') if bands.is_file() else None,
                      json.loads(first.read_text(encoding='utf-8')) if first.is_file() else None, extra, extra_rationale)
    data['inputs'] = {'cell': 'scf/POSCAR（证据已校验）', 'bands_csv_sha256': core.digest(bands) if bands.is_file() else None}
    return data


def candidates_for_download(folder, extra=None, extra_rationale=None):
    """Candidates from a verified download (scf-summary poscar, relaxed-2.POSCAR, bands.csv).

    Downloads carry no config.json; symprec 0.01 and 40 points per line are what
    every remote preparation uses.
    """
    from pymatgen.core import Structure
    folder = Path(folder)
    receipt = json.loads((folder / 'download-receipt.json').read_text(encoding='utf-8'))['files']
    raw = {}
    for name in ('scf-summary.json', 'relaxed-2.POSCAR', 'bands.csv', 'physics-model.json'):
        data = (folder / name).read_bytes()
        if receipt.get(name, {}).get('sha256') != hashlib.sha256(data).hexdigest():
            raise ValueError(f'{name} 与下载收据不符，不能给出候选。')
        raw[name] = data.decode('utf-8')
    physics = json.loads(raw['physics-model.json'])
    config = {'symprec': .01, 'points': 40, 'dimensionality': {'dimension': physics.get('dimension', '3D')}}
    primitive = Structure.from_str(json.loads(raw['scf-summary.json'])['poscar'], fmt='poscar')
    structure = Structure.from_str(raw['relaxed-2.POSCAR'], fmt='poscar')
    return candidates(config, structure, primitive, raw['bands.csv'], None, extra, extra_rationale)
