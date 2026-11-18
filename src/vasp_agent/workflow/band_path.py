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
import hashlib
import json
import math
import re
import shutil
import warnings
from pathlib import Path

import numpy as np

import vasp_agent.workflow.vasp_band_agent as core

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


# --- Fork: redo bands on a new path, reusing a verified SCF -------------------

def _source_files(source, manifest, data):
    """Every file the bands phase reads or verifies upstream, with its expected hash."""
    files = dict(manifest['hashes'])
    files.update(data['hashes'])
    files['physics-model.json'] = core.digest(source / 'physics-model.json')
    plan = json.loads((source / 'input-plan.json').read_text(encoding='utf-8'))
    from vasp_agent.workflow.stage_gate import phase_of, task_config
    from vasp_agent.workflow import stage_definitions as stages
    reused = stages.through(task_config(source), 'scf')
    for name in plan['incars']:
        if phase_of(name) not in reused:
            continue
        inputs = json.loads((source / name / 'inputs.json').read_text(encoding='utf-8'))
        for filename, digest in inputs.items():
            files.setdefault(name + '/' + filename, digest)
    for name in files:
        path = source / name
        if (Path(name).is_absolute() or '..' in Path(name).parts or not path.resolve().is_relative_to(source.resolve())
                or any(part.is_symlink() for part in (path, *path.parents) if part != source.parent)):
            raise ValueError('来源证据路径无效。')
    return files


def _check_reproducible(source, plan):
    """Every reused stage's INCAR is what this engine would write for it.

    The bands job walks all earlier stages again (as cached results) and refuses
    any input it would write differently. A source prepared by an engine whose
    INCAR text differs is refused here, before anything is copied.
    """
    from vasp_agent.workflow.input_review import annotated
    from vasp_agent.workflow.stage_gate import phase_of, task_config
    from vasp_agent.workflow import stage_definitions as stages
    reused = stages.through(task_config(source), 'scf')
    for name, incar in plan['incars'].items():
        if phase_of(name) not in reused:
            continue
        folder = source / name
        tags = json.loads((folder / 'execution.json').read_text(encoding='utf-8'))['tags'] if (folder / 'execution.json').is_file() else {}
        if (folder / 'INCAR').read_text(encoding='utf-8') != annotated({**incar, **tags}):
            raise ValueError(f'来源 {name}/INCAR 与当前引擎生成的输入不一致（来源由不同版本准备）；'
                             '不能在新引擎下逐项复核，未创建新目录。')


def prepare_bands_path(source, target, band_path):
    """Fork a task whose SCF passed into a new directory that only redoes bands on a new path.

    Copies and verifies the evidence through SCF (incl. scf/CHGCAR against
    complete.json charge_sha256), never touches the source, never reruns SCF;
    the new task waits at the bands review and needs its own submission grant.
    """
    from vasp_agent.workflow import stage_gate
    source, target = Path(source), Path(target)
    if target.exists() or source.resolve() == target.resolve():
        raise ValueError('换路径必须使用不存在的新目录。')

    def check_idle():
        if (source / '.agent.lock').exists() or (source / '.task-archived.json').exists():
            raise ValueError('来源仍在运行或已归档。')
    check_idle()
    manifest = core.verify(source)
    config = json.loads((source / 'config.json').read_text(encoding='utf-8'))
    if not manifest.get('stage_gates') or manifest.get('workflow') != 'full':
        raise ValueError('只支持逐阶段审阅的完整流程任务。')
    stage_gate.physics(source)
    checkpoint = json.loads((source / 'checkpoint.json').read_text(encoding='utf-8')) if (source / 'checkpoint.json').is_file() else {}
    if 'scf' not in checkpoint.get('completed', []):
        raise ValueError('来源 SCF 尚未通过验收，不能复用电荷密度。')
    from vasp_agent.workflow.dimensionality import is_2d
    if is_2d(config):
        raise ValueError('二维 slab 的能带晶胞未标准化，只支持自动面内路径。')
    spec = validate(band_path, config['points'])
    from vasp_agent.workflow import stage_definitions as stages
    # Everything through SCF (incl. scf/CHGCAR vs charge_sha256): the source's
    # stage after SCF may be bands or another SCF-dependent stage.
    data = stage_gate.evidence_through(source, 'scf')
    if checkpoint.get('next') == stages.following(config, 'scf') and checkpoint.get('evidence_hashes') != data['hashes']:
        raise ValueError('来源 SCF 完成后证据文件发生变化，请人工检查。')
    plan = json.loads((source / 'input-plan.json').read_text(encoding='utf-8'))
    _check_reproducible(source, plan)
    config = {**config, 'band_path': spec}
    if 'stages' in config:
        # The fork runs only bands after the reused stages; a source's other
        # SCF-dependent stages are not part of it. Sources without the record
        # stay without it (legacy chain, bands last).
        kept = [entry for entry in config['stages'] if entry['name'] in stages.BASE_STAGES]
        bands = ([entry for entry in config['stages'] if entry['name'] == 'bands']
                 or [{'name': 'bands', 'version': max(stages.DEFINITIONS['bands'])}])
        config['stages'] = kept + bands
        stages.validate(config['stages'])
        # Parameters of the dropped stages do not belong to the fork.
        config.pop('stage_parameters', None)
    # Resolved on the source's final cell now: an unknown label is refused before any write.
    record = files_record(source, config)
    files = _source_files(source, manifest, data)
    target.mkdir(parents=True, exist_ok=False)
    for name, digest in files.items():
        if name == 'manifest.json':
            continue
        destination = target / name
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source / name, destination)
        if core.digest(destination) != digest:
            raise ValueError('复制期间来源证据发生变化；新目录未完成，不能提交。')
    shutil.copyfile(source / 'manifest.json', target / 'bands-source-manifest.json')
    if config.get('submission_mode') == 'array':
        config['submission_mode'] = 'allocation'  # convergence is done; only bands remains
    # Bands runs in this task, so it gets the KPAR/NCORE a new task would; the
    # copied stages keep running as they ran (no execution.json, no tags).
    from vasp_agent.workflow.full_workflow import EXECUTION_TAGS_VERSION
    config.update(parallel_tests=1, cores_per_test=8, nodes=1, execution_tags=EXECUTION_TAGS_VERSION)
    core.save(target / 'config.json', config)
    plan['band_path'] = {'source': 'operator', 'requested': spec, 'text': render(spec),
                         'note': '换路径续算：复用来源已验收的 SCF 电荷密度，只运行 bands。'}
    core.save(target / 'input-plan.json', plan)
    with (target / 'PARAMETERS.md').open('a', encoding='utf-8') as stream:
        stream.write('\n\n能带路径（操作者指定，换路径续算）：' + render(spec) +
                     '；复用来源 SCF 电荷密度（ICHARG=11），不重算 SCF。\n')
    receipt = {'source': str(source), 'source_files': files, 'band_path': spec, 'text': render(spec),
               'resolved_on_source_cell': record, 'scf_reused': True, 'scf_recomputed': False,
               'charge_sha256': data['hashes']['scf/CHGCAR'],
               'physics': '同结构、赝势和模型，沿用来源操作者确认；不复制审阅或提交授权', 'submitted': False}
    core.save(target / 'bands-path-choice.json', receipt)
    updated = dict(manifest)
    updated['hashes'] = dict(manifest['hashes'])
    for name in ('config.json', 'input-plan.json', 'PARAMETERS.md', 'bands-source-manifest.json', 'bands-path-choice.json'):
        updated['hashes'][name] = core.digest(target / name)
    checkpoint = {'completed': list(stages.through(config, 'scf')), 'next': 'bands',
                  'evidence_hashes': stage_gate.evidence(target, 'bands')['hashes'], 'bands_path_fork': str(source)}
    if checkpoint['evidence_hashes'] != data['hashes']:
        raise ValueError('新目录证据与来源不一致；新目录未完成，不能提交。')
    core.save(target / 'checkpoint.json', checkpoint)
    core.save(target / 'workflow-state.json', {'status': 'awaiting_review', **checkpoint})
    check_idle()
    if stage_gate.evidence_through(source, 'scf')['hashes'] != data['hashes']:
        raise ValueError('准备期间来源证据发生变化；新目录未完成，不能提交。')
    # Published last: an interrupted preparation cannot pass core.verify.
    core.save(target / 'manifest.json', updated)
    core.verify(target)
    context = stage_gate.context(target)
    core.event(target, 'prepared_bands_path_fork', source=str(source), band_path=render(spec),
               charge_sha256=data['hashes']['scf/CHGCAR'])
    return {'prepared': True, 'directory': str(target), 'phase': 'bands', 'band_path': render(spec),
            'resolved': {'lattice_type': record['lattice_type'], 'convention': record['convention'],
                         'segments': [[(p['label'], [round(x, 6) for x in p['frac']]) for p in s['points']] for s in record['segments']],
                         'divisions': [s['divisions'] for s in record['segments']], 'total_kpoints': record['total_kpoints']},
            'scf_recomputed': False, 'charge_sha256': data['hashes']['scf/CHGCAR'],
            'snapshot': context['snapshot'], 'submitted': False,
            'next': '新目录已复用并验证 SCF；需 get_stage_review 审阅 bands 并单独提交授权。'}


# --- Candidate paths for the operator's choice ---------------------------------

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
        text += '-' + pretty(axis[0][1])
    shape, names = IN_PLANE_NAMES[kind]
    rationale = (f'层状判据：惯用晶胞 c/max(a,b) = {ratio:.2f} ≥ {LAYERED_RATIO}。沿二维{shape}子晶格走面内路径'
                 f'（{names}；二维记号，与三维约定的同名标签不是同一点），便于对照 Wannier/紧束缚模型'
                 + (f'；末段 Γ–{pretty(axis[0][1])} 看 kz 色散' if axis else '') + '。')
    return text, rationale


def _extrema(bands_text, table, primitive):
    """(path text, rationale) through table points nearest the first run's E_F crossings or band edges."""
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
    gamma = next((n for n in table if norm(n) == 'GAMMA'), None)
    route = ([gamma] + [n for n in chosen if n != gamma]) if gamma else chosen
    if len(route) < 2:
        return None, f'第一次结果的{kind}都最靠近 Γ，无法组成不同的路径'
    rationale = (f'第一次结果有 {len(hits)} 处 {kind}，最靠近 ' + '、'.join(pretty(n) for n in chosen) +
                 '；只依据已采样路径，不是全布里渊区搜索，路径外可能另有交叉或极值。')
    return '-'.join(pretty(n) for n in route), rationale


def candidates(config, structure, primitive, bands_text=None, first_run=None, extra=None, extra_rationale=None):
    """Reasonable paths for the bands cell, each validated and resolved like an operator path.

    Deterministic for the same inputs. A candidate that fails validate() or
    record_for() is never offered; it is listed under 'omitted' with the reason.
    """
    from vasp_agent.workflow.dimensionality import is_2d
    base = {k: v for k, v in config.items() if k != 'band_path'}
    automatic = record_for(base, structure, primitive)
    if first_run is None and config.get('band_path') is None:
        first_run = automatic
    offered, omitted = [], []

    def offer(key, title, text, rationale, origin='program'):
        if text is None:
            omitted.append({'key': key, 'title': title, 'reason': rationale})
            return
        try:
            record = record_for({**base, 'band_path': validate(text, base['points'])}, structure, primitive)
        except (ValueError, ImportError) as exc:
            omitted.append({'key': key, 'title': title, 'text': text, 'reason': str(exc)})
            return
        duplicate = next((c['title'] for c, r in offered if _same_route(r, record)), None)
        if duplicate:
            omitted.append({'key': key, 'title': title, 'text': text, 'reason': f'与“{duplicate}”路径相同'})
            return
        offered.append(({'key': key, 'title': title, 'origin': origin, 'text': render(record['requested']),
                         'resolved': _route(record), 'total_kpoints': record['total_kpoints'], 'rationale': rationale,
                         'used_by_first_run': first_run is not None and _same_route(record, first_run),
                         'selectable': True}, record))

    if is_2d(config):
        entry = {'key': 'automatic', 'title': '自动（面内）', 'origin': 'program', 'text': _text(automatic),
                 'resolved': _route(automatic), 'total_kpoints': automatic['total_kpoints'],
                 'rationale': '二维 slab 的能带晶胞未标准化，只支持自动面内路径。', 'used_by_first_run': True, 'selectable': False}
        return {'version': SPEC_VERSION, 'candidates': [entry], 'cell': automatic['cell'], 'lattice_type': automatic['lattice_type'],
                'omitted': [{'key': k, 'reason': '二维 slab 只支持自动面内路径'} for k in ('hinuma', 'layered', 'extrema')],
                'note': '二维 slab 没有可换的路径。'}
    offer('automatic', '自动（Setyawan–Curtarolo）', _text(automatic),
          f"{automatic['lattice_type']} 的标准路径，经过该约定的全部高对称点。")
    try:
        from pymatgen.symmetry.bandstructure import HighSymmKpath
        tables('hinuma', structure, primitive, base['symprec'])  # refuses clearly without seekpath
        with warnings.catch_warnings():
            warnings.simplefilter('ignore')
            route = HighSymmKpath(primitive, symprec=base['symprec'], path_type='hinuma').kpath['path']
        offer('hinuma', 'Hinuma（seekpath）', 'hinuma:' + '|'.join('-'.join(pretty(n) for n in seg) for seg in route),
              'Hinuma 等人的约定（seekpath），标签和分段不同，便于对照使用 seekpath 的文献或软件。')
    except (ValueError, ImportError) as exc:
        offer('hinuma', 'Hinuma（seekpath）', None, str(exc))
    offer('layered', '层状面内', *_layered(structure, primitive, base['symprec']))
    if bands_text:
        table, _, _ = tables('setyawan_curtarolo', structure, primitive, base['symprec'])
        try:
            offer('extrema', '靠近交叉/带边', *_extrema(bands_text, table, primitive))
        except ValueError as exc:
            offer('extrema', '靠近交叉/带边', None, f'bands.csv 无法分析：{exc}')
    else:
        offer('extrema', '靠近交叉/带边', None, '没有第一次计算的 bands.csv')
    if extra:
        offer('ai_suggestion', 'AI 建议', extra, (extra_rationale or '').strip()[:300] or '（未附理由）', origin='ai_suggestion')
    return {'version': SPEC_VERSION, 'candidates': [c for c, _ in offered], 'omitted': omitted,
            'cell': automatic['cell'], 'lattice_type': automatic['lattice_type'],
            'note': '候选均已在能带原胞上解析；选中后新建任务复用来源 SCF，只重算能带，需重新审阅和单独提交授权。'}


def candidates_for_task(root, extra=None, extra_rationale=None):
    """Candidates for a task whose SCF passed, from its verified cell files."""
    from pymatgen.io.vasp.inputs import Poscar
    from vasp_agent.workflow import stage_gate
    root = Path(root)
    core.verify(root)
    checkpoint = json.loads((root / 'checkpoint.json').read_text(encoding='utf-8')) if (root / 'checkpoint.json').is_file() else {}
    if 'scf' not in checkpoint.get('completed', []):
        raise ValueError('来源 SCF 尚未通过验收，不能给出换路径候选。')
    stage_gate.evidence(root, 'bands')  # scf/POSCAR, relaxed-2.POSCAR and CHGCAR as accepted
    config = json.loads((root / 'config.json').read_text(encoding='utf-8'))
    first, bands = root / RECORD, root / 'bands.csv'
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
