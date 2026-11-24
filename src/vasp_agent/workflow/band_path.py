"""Operator-chosen band-structure paths: syntax, resolution on the bands cell, record, fork.

Which cell the coordinates refer to
-----------------------------------
3D tasks run SCF and bands in one cell: the Setyawan–Curtarolo standardized
primitive cell of the relaxed structure (HighSymmKpath(relaxed).prim, sites
regrouped to the POTCAR order, lattice unchanged). That cell is written to
scf/POSCAR and bands/POSCAR. Every fractional coordinate here -- the labels of
either convention and any explicit point -- is in the basis of that cell's
reciprocal lattice. A convention never changes the SCF cell: Hinuma labels are
computed on this same cell and transformed into its reciprocal basis by
pymatgen, not by re-standardizing.

2D slabs keep the relaxed, non-standardized slab cell, so only the automatic
in-plane path is supported there; an operator path is refused at preparation.

Path text (what the model passes): segments separated by '|' (a break), points
by '-', each segment optionally ending in '@N' (points per line, overriding
config['points']), an optional 'hinuma:' prefix, and explicit points written
LABEL(a,b,c). Example: 'Γ-X-M-Γ|Z-R@60', 'hinuma:GAMMA-X-P', 'Γ-M(0.5,0.5,0)'.
The stored form (config['band_path']) is the canonical dict validate() returns.
"""
import math
import re
import warnings

import numpy as np

from vasp_agent.workflow.band_path_candidates import (
    IN_PLANE,
    IN_PLANE_NAMES,
    LAYERED_RATIO,
    LAYERED_SYSTEMS,
    MAX_EXTREMA_POINTS,
    _extrema,
    _layered,
    _route,
    _same_route,
    _text,
    candidates,
    candidates_for_download,
    candidates_for_task,
)
from vasp_agent.workflow.band_path_fork import (
    _check_reproducible,
    _source_files,
    prepare_bands_path,
)

__all__ = [
    'ALIASES',
    'CELL_NOTE',
    'CONVENTIONS',
    'GAMMA_ALIASES',
    'GREEK',
    'IN_PLANE',
    'IN_PLANE_NAMES',
    'LABEL',
    'LAYERED_RATIO',
    'LAYERED_SYSTEMS',
    'MAX_DIVISIONS',
    'MAX_EXTREMA_POINTS',
    'MAX_POINTS',
    'MAX_SEGMENTS',
    'MAX_TOTAL_KPOINTS',
    'MIN_DIVISIONS',
    'RECORD',
    'SEPARATORS',
    'SPEC_VERSION',
    '_check_reproducible',
    '_extrema',
    '_layered',
    '_route',
    '_same_route',
    '_source_files',
    '_split_outside',
    '_text',
    'ascii_label',
    'candidates',
    'candidates_for_download',
    'candidates_for_task',
    'files_record',
    'kpoints',
    'lookup',
    'norm',
    'parse',
    'prepare_bands_path',
    'pretty',
    'provisional',
    'record_for',
    'render',
    'resolve',
    'review_context',
    'same',
    'tables',
    'validate',
]

# Format of the stored spec and of bands-path.json. The engine capability marker
# the worker checks is full_workflow.BAND_PATH_VERSION.
SPEC_VERSION = 1
CONVENTIONS = ('setyawan_curtarolo', 'hinuma')
ALIASES = {'sc': 'setyawan_curtarolo', 'setyawan_curtarolo': 'setyawan_curtarolo', 'setyawan-curtarolo': 'setyawan_curtarolo',
           'hinuma': 'hinuma', 'seekpath': 'hinuma'}
# The existing bound on config['points'] is >= 2; the upper bounds keep one
# operator path within what a single bands job is meant to be.
MIN_DIVISIONS, MAX_DIVISIONS = 2, 200
MAX_SEGMENTS, MAX_POINTS, MAX_TOTAL_KPOINTS = 20, 30, 5000
GREEK = {'Γ': 'Gamma', 'Σ': 'Sigma', 'Δ': 'Delta', 'Λ': 'Lambda', 'Π': 'Pi', 'Ξ': 'Xi', 'Ω': 'Omega',
         'Θ': 'Theta', 'Φ': 'Phi', 'Ψ': 'Psi'}
GAMMA_ALIASES = ('G', 'GAMMA')
LABEL = re.compile(r"^[A-Za-z0-9_'\\{}" + ''.join(GREEK) + r"]{1,16}$")
SEPARATORS = re.compile(r'[\s\-–—→>]+')
RECORD = 'bands-path.json'
CELL_NOTE = ('分数坐标基于能带/SCF 所用晶胞（scf/POSCAR = bands/POSCAR，即优化结构的 Setyawan–Curtarolo '
             '标准原胞）的倒格子基矢；笛卡尔坐标单位 1/Å（含 2π）。')


def norm(label):
    """Matching key: 'Σ1', 'SIGMA_1', '\\Sigma_1' -> 'SIGMA1'."""
    text = str(label).strip()
    for char, name in GREEK.items():
        text = text.replace(char, name)
    return re.sub(r'[\\{}_$\s]', '', text).upper()


def pretty(label):
    """'\\Gamma' -> 'Γ', '\\Sigma_1' -> 'Σ_1', 'GAMMA' -> 'Γ' (display only)."""
    text = str(label).lstrip('\\')
    for char, name in GREEK.items():
        if text.upper() == name.upper():
            return char
        if text.startswith(name):
            text = char + text[len(name):]
    return text


def ascii_label(label):
    """KPOINTS label for an explicit point: Greek letters in pymatgen's '\\Name' form."""
    text = str(label)
    for char, name in GREEK.items():
        text = text.replace(char, '\\' + name)
    match = re.fullmatch(r'(\\?[A-Za-z]+)(\d+)', text)
    return f'{match[1]}_{match[2]}' if match else text


def _split_outside(text, pattern):
    """Split on a regex, never inside parentheses (explicit coordinates may be negative)."""
    parts, depth, start, i = [], 0, 0, 0
    while i < len(text):
        char = text[i]
        if char == '(':
            depth += 1
        elif char == ')':
            depth -= 1
            if depth < 0:
                raise ValueError('能带路径括号不匹配。')
        elif depth == 0:
            match = pattern.match(text, i)
            if match:
                parts.append(text[start:i])
                start = i = match.end()
                continue
        i += 1
    if depth:
        raise ValueError('能带路径括号不匹配。')
    return parts + [text[start:]]


def parse(text):
    """Path text -> canonical-shaped dict (validated afterwards by validate)."""
    if not isinstance(text, str) or not text.strip() or len(text) > 2000:
        raise ValueError('能带路径须为非空文本，例如 Γ-X-M-Γ|Z-R。')
    text = text.strip()
    convention = 'setyawan_curtarolo'
    head = re.match(r'^([A-Za-z_\-]+)\s*:', text)
    if head:
        convention, text = head[1].lower(), text[head.end():]
    segments = []
    for raw in _split_outside(text, re.compile(r'\|')):
        raw = raw.strip()
        divisions = None
        tail = re.search(r'@\s*(\d+)\s*$', raw)
        if tail:
            divisions, raw = int(tail[1]), raw[:tail.start()]
        points = []
        for token in _split_outside(raw.strip(), SEPARATORS):
            match = re.fullmatch(r'\s*([^()\s]+)\s*(?:\(([^()]*)\))?\s*', token)
            if not match:
                raise ValueError(f'无法解析能带路径点 “{token}”；写法如 Γ-X-M 或 K(0.5,0,0)。')
            point = {'label': match[1]}
            if match[2] is not None:
                try:
                    point['frac'] = [float(x) for x in match[2].split(',')]
                except ValueError:
                    raise ValueError(f'能带路径点 {match[1]} 的坐标无法解析。') from None
            points.append(point)
        segments.append({'path': points, 'divisions': divisions})
    return {'convention': convention, 'segments': segments}


def validate(value, default_divisions):
    """Syntax only: the canonical stored form, or a ValueError naming what is wrong.

    Accepts path text, a list of segments, or a dict with 'segments' (and
    optionally 'convention'). A segment is a list of points or a dict with
    'path' (alias 'labels'/'kpoints') and 'divisions' (alias 'points', an int).
    A point is a label or {'label', 'frac'}. Nothing here knows the lattice.
    """
    if isinstance(value, str):
        value = parse(value)
    if isinstance(value, list):
        value = {'segments': value}
    if not isinstance(value, dict) or not set(value) <= {'version', 'convention', 'segments'}:
        raise ValueError('能带路径格式无效：需要路径文本或 {convention, segments}。')
    convention = str(value.get('convention') or 'setyawan_curtarolo').strip().lower()
    if convention in ('latimer_munro', 'latimer-munro', 'lm'):
        raise ValueError('latimer_munro 约定的标签是程序生成的字母，不对应已发表的标签表，不支持标签路径；'
                         '请用 setyawan_curtarolo（默认）或 hinuma，或用显式坐标点。')
    if convention not in ALIASES:
        raise ValueError(f'未知能带路径约定 {convention}；支持 ' + ' / '.join(CONVENTIONS) + '。')
    segments = value.get('segments')
    if not isinstance(segments, list) or not 1 <= len(segments) <= MAX_SEGMENTS:
        raise ValueError(f'能带路径须有 1–{MAX_SEGMENTS} 段。')
    if type(default_divisions) is not int or not MIN_DIVISIONS <= default_divisions <= MAX_DIVISIONS:
        raise ValueError('默认路径点数无效。')
    canonical, total = [], 0
    for index, segment in enumerate(segments, start=1):
        divisions = None
        if isinstance(segment, dict):
            keys = set(segment)
            path_keys = keys & {'path', 'labels', 'kpoints'}
            count_keys = keys & {'divisions', 'points'}
            if len(path_keys) != 1 or len(count_keys) > 1 or keys - path_keys - count_keys:
                raise ValueError(f'第 {index} 段格式无效：需要 path 列表和可选 divisions。')
            points = segment[path_keys.pop()]
            divisions = segment[count_keys.pop()] if count_keys else None
        else:
            points = segment
        if divisions is not None and (type(divisions) is not int or not MIN_DIVISIONS <= divisions <= MAX_DIVISIONS):
            raise ValueError(f'第 {index} 段每条线的点数须为 {MIN_DIVISIONS}–{MAX_DIVISIONS} 的整数。')
        if not isinstance(points, list) or not 2 <= len(points) <= MAX_POINTS:
            raise ValueError(f'第 {index} 段须有 2–{MAX_POINTS} 个点；单独一点不构成路径。')
        cleaned = []
        for point in points:
            if isinstance(point, str):
                point = {'label': point}
            if not isinstance(point, dict) or not set(point) <= {'label', 'frac'} or 'label' not in point:
                raise ValueError(f'第 {index} 段的点须为标签或 {{label, frac}}。')
            label = point['label'].strip() if isinstance(point['label'], str) else None
            if not label or not LABEL.fullmatch(label):
                raise ValueError(f'能带路径标签 “{point["label"]}” 无效：只允许字母、数字、_、\'、\\ 和希腊字母，最长 16 字符。')
            # Stored ASCII ('Γ' -> '\Gamma'): config.json is read back without an
            # explicit encoding in places, and VASP reads the KPOINTS labels.
            entry = {'label': ascii_label(label)}
            if 'frac' in point:
                frac = point['frac']
                if (not isinstance(frac, list) or len(frac) != 3
                        or any(type(x) not in (int, float) or not math.isfinite(x) or abs(x) > 1 for x in frac)):
                    raise ValueError(f'点 {label} 的坐标须为 3 个 |x| ≤ 1 的有限数（标准原胞倒格子分数坐标）。')
                entry['frac'] = [float(x) for x in frac]
            if cleaned and cleaned[-1] == entry:
                raise ValueError(f'第 {index} 段相邻两点 {label} 相同，线段长度为零。')
            cleaned.append(entry)
        total += (len(cleaned) - 1) * (divisions or default_divisions)
        canonical.append({'path': cleaned, 'divisions': divisions})
    if total > MAX_TOTAL_KPOINTS:
        raise ValueError(f'能带路径共 {total} 个 k 点，超过 {MAX_TOTAL_KPOINTS}；请减少段数或每段点数。')
    return {'version': SPEC_VERSION, 'convention': ALIASES[convention], 'segments': canonical}


def render(spec):
    """Canonical dict -> the path text it came from (for menus and notes)."""
    prefix = '' if spec['convention'] == 'setyawan_curtarolo' else spec['convention'] + ':'
    parts = []
    for segment in spec['segments']:
        text = '-'.join(pretty(p['label']) + ('(' + ','.join(f'{x:g}' for x in p['frac']) + ')' if 'frac' in p else '')
                        for p in segment['path'])
        parts.append(text + (f"@{segment['divisions']}" if segment['divisions'] else ''))
    return prefix + '|'.join(parts)


def tables(convention, structure, primitive, symprec, sc_path=None):
    """(label table, lattice type, SC path) for the bands cell.

    The table maps labels to fractional coordinates in primitive's reciprocal
    basis. The SC path is the HighSymmKpath run_full takes the bands cell from
    (path.prim), so its coordinates are in the cell SCF runs in; Hinuma labels
    are computed on that same cell and transformed into its basis by pymatgen.
    """
    from pymatgen.symmetry.bandstructure import HighSymmKpath
    from pymatgen.symmetry.kpath import KPathSetyawanCurtarolo
    with warnings.catch_warnings():
        warnings.simplefilter('ignore')
        sc = sc_path or HighSymmKpath(structure, symprec=symprec)
        lattice_type = KPathSetyawanCurtarolo(structure, symprec=symprec).name
        if convention == 'setyawan_curtarolo':
            table = sc.kpath['kpoints']
        else:
            try:
                table = HighSymmKpath(primitive, symprec=symprec, path_type='hinuma').kpath['kpoints']
            except ImportError:
                raise ValueError('服务器 Python 缺少 seekpath，不能使用 hinuma 约定；请改用默认约定或显式坐标。') from None
            lattice_type += '（Hinuma 标签）'
    return {k: [float(x) for x in v] for k, v in table.items()}, lattice_type, sc


def lookup(label, table, lattice_type, convention):
    """A label's table key, or a ValueError listing the valid labels for this lattice."""
    keys = {norm(k): k for k in table}
    key = norm(label)
    if key in keys:
        return keys[key]
    if key in GAMMA_ALIASES and 'GAMMA' in keys:
        return keys['GAMMA']
    raise ValueError(f'未知高对称点标签 {label}：{lattice_type}（{convention}）可用标签为 '
                     + '、'.join(pretty(k) for k in table) + '；其他点请写显式坐标 名称(a,b,c)（能带原胞倒格子分数坐标）。')


def resolve(spec, table, lattice_type, reciprocal, default_divisions):
    """Resolved segments: every point with its label, KPOINTS label and coordinates."""
    seen = {}
    segments = []
    for segment in spec['segments']:
        points = []
        for point in segment['path']:
            if 'frac' in point:
                entry = {'label': pretty(point['label']), 'kpoints_label': point['label'],
                         'frac': point['frac'], 'source': 'explicit'}
            else:
                key = lookup(point['label'], table, lattice_type, spec['convention'])
                entry = {'label': pretty(key), 'kpoints_label': key, 'frac': table[key], 'source': 'convention'}
            entry['cartesian_inv_A'] = (np.asarray(entry['frac']) @ reciprocal).tolist()
            previous = seen.setdefault(entry['kpoints_label'], entry['frac'])
            if not np.allclose(previous, entry['frac'], atol=1e-6):
                raise ValueError(f"标签 {entry['label']} 对应了两个不同坐标；同一标签只能表示一个点。")
            points.append(entry)
        for a, b in zip(points, points[1:]):
            if np.allclose(a['frac'], b['frac'], atol=1e-6):
                raise ValueError(f"{a['label']}-{b['label']} 两点坐标相同，线段长度为零。")
        segments.append({'divisions': segment['divisions'] or default_divisions, 'points': points})
    for label, frac in seen.items():
        for other, coordinate in seen.items():
            if label < other and np.allclose(frac, coordinate, atol=1e-6):
                raise ValueError(f'{pretty(label)} 与 {pretty(other)} 坐标相同；同一点请用同一标签。')
    return segments


def record_for(config, structure, primitive, sc_path=None):
    """What the bands stage runs: source, convention, cell and resolved segments.

    structure is the relaxed structure the SC path is derived from, primitive the
    bands cell. Shared by the run (in-memory objects) and the bands review
    (relaxed-2.POSCAR and scf/POSCAR), and compared between the two.
    """
    from vasp_agent.workflow.dimensionality import band_path_2d, is_2d
    spec = config.get('band_path')
    reciprocal = np.asarray(primitive.lattice.reciprocal_lattice.matrix)
    cell = {'source': 'scf/POSCAR', 'abc_A': list(primitive.lattice.abc), 'angles_deg': list(primitive.lattice.angles),
            'reciprocal_lattice_inv_A': reciprocal.tolist(), 'note': CELL_NOTE}
    if is_2d(config):
        if spec is not None:
            raise ValueError('二维 slab 的能带晶胞未标准化，只支持自动面内路径。')
        path = band_path_2d(structure)
        table, lattice_type, convention = path.kpath['kpoints'], path.lattice_type, 'in_plane_2d'
        cell['note'] = '二维 slab：分数坐标基于优化后未标准化的 slab 晶胞倒格子基矢（c 为真空方向）。'
    else:
        convention = spec['convention'] if spec else 'setyawan_curtarolo'
        table, lattice_type, path = tables(convention, structure, primitive, config['symprec'], sc_path)
    segments = spec['segments'] if spec else [{'path': [{'label': k} for k in labels], 'divisions': None}
                                              for labels in path.kpath['path']]
    segments = resolve({'convention': convention, 'segments': segments}, table, lattice_type, reciprocal, config['points'])
    return {'version': SPEC_VERSION, 'source': 'operator' if spec else 'automatic',
            'convention': convention, 'lattice_type': lattice_type, 'requested': spec,
            'requested_text': render(spec) if spec else None, 'cell': cell,
            'available_labels': {pretty(k): [float(x) for x in v] for k, v in table.items()},
            'segments': segments, 'total_kpoints': sum((len(s['points']) - 1) * s['divisions'] for s in segments),
            'kpoints_style': 'Line_mode' if len({s['divisions'] for s in segments}) == 1 else 'Reciprocal'}


def kpoints(record):
    """KPOINTS for an operator path: line mode when every segment has the same density."""
    from pymatgen.io.vasp.inputs import Kpoints
    segments = record['segments']
    if record['kpoints_style'] == 'Line_mode':
        kpts, labels = [], []
        for segment in segments:
            for a, b in zip(segment['points'], segment['points'][1:]):
                kpts += [a['frac'], b['frac']]
                labels += [a['kpoints_label'], b['kpoints_label']]
        return Kpoints('Operator band path (' + record['convention'] + ')', style=Kpoints.supported_modes.Line_mode,
                       coord_type='Reciprocal', kpts=kpts, labels=labels, num_kpts=segments[0]['divisions'])
    # Different densities per segment: an explicit list that repeats each line's
    # end points exactly as VASP line mode does, so pymatgen reads the same branches.
    kpts, labels = [], []
    for segment in segments:
        for a, b in zip(segment['points'], segment['points'][1:]):
            for i, t in enumerate(np.linspace(0.0, 1.0, segment['divisions'])):
                kpts.append((np.asarray(a['frac']) + t * (np.asarray(b['frac']) - np.asarray(a['frac']))).tolist())
                labels.append(a['kpoints_label'] if i == 0 else b['kpoints_label'] if i == segment['divisions'] - 1 else '')
    return Kpoints('Operator band path (' + record['convention'] + ', explicit)', style=Kpoints.supported_modes.Reciprocal,
                   num_kpts=len(kpts), kpts=kpts, kpts_weights=[1] * len(kpts), labels=labels)


def same(a, b):
    """Two records describe the same path (labels, divisions, coordinates to 1e-6)."""
    if (a['source'], a['convention'], a['kpoints_style']) != (b['source'], b['convention'], b['kpoints_style']):
        return False
    if len(a['segments']) != len(b['segments']):
        return False
    for x, y in zip(a['segments'], b['segments']):
        if x['divisions'] != y['divisions'] or len(x['points']) != len(y['points']):
            return False
        for p, q in zip(x['points'], y['points']):
            if p['kpoints_label'] != q['kpoints_label'] or not np.allclose(p['frac'], q['frac'], atol=1e-6):
                return False
    return True


def files_record(root, config):
    """The record the bands review shows, derived from the verified cell files."""
    from pymatgen.io.vasp.inputs import Poscar
    structure = Poscar.from_file(root / 'relaxed-2.POSCAR').structure
    primitive = Poscar.from_file(root / 'scf/POSCAR').structure
    return record_for(config, structure, primitive)


def review_context(root, config):
    """For the bands stage review: the requested path and exactly what will run, or why it cannot."""
    requested = config.get('band_path')
    try:
        record = files_record(root, config)
    except ValueError as exc:
        return {'source': 'operator' if requested else 'automatic', 'requested': requested,
                'requested_text': render(requested) if requested else None, 'resolved': None, 'error': str(exc),
                'next': '该路径不能在最终晶胞上解析；bands 阶段会拒绝运行。请审阅为 blocked，并用 prepare_bands_path 在新目录换路径。'}
    return {'source': record['source'], 'requested': requested, 'requested_text': record['requested_text'],
            'resolved': record,
            'note': ('操作者指定路径，按上述坐标运行。' if requested else '自动路径（Setyawan–Curtarolo，最终标准原胞）。')
                    + '标签、断点和每段点数与运行时生成的 KPOINTS 一致，运行前会再次比对。'}


def provisional(spec, structure, symprec):
    """Preparation-time check of labels on the input cell; the final cell decides at bands."""
    from pymatgen.symmetry.bandstructure import HighSymmKpath
    with warnings.catch_warnings():
        warnings.simplefilter('ignore')
        sc = HighSymmKpath(structure, symprec=symprec)
    table, lattice_type, _ = tables(spec['convention'], structure, sc.prim, symprec, sc)
    reciprocal = np.asarray(sc.prim.lattice.reciprocal_lattice.matrix)
    segments = resolve(spec, table, lattice_type, reciprocal, MIN_DIVISIONS)
    return {'text': render(spec), 'convention': spec['convention'], 'lattice_type_of_input': lattice_type,
            'labels_on_input_cell': {p['label']: p['frac'] for s in segments for p in s['points']},
            'note': '仅按输入结构预检标签；最终坐标在 SCF 完成后按优化结构的标准原胞解析并在 bands 审阅中显示，'
                    '晶格类型变化（如 cell 优化后 BCT1/BCT2 互换）导致标签不存在时 bands 拒绝运行。'}
