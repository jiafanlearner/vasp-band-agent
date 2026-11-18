"""Structure-derived dimensionality: 3D bulk or 2D slab with vacuum along c.

The classification is computed from atomic positions, not asserted by the
operator, so a physics confirmation can be checked against the structure it
claims to describe. Only one vacuum direction is supported, and it must be the
third lattice vector: KPOINTS, the band path and the relaxation mode all rely
on that axis being c.
"""
import math
from types import SimpleNamespace

VACUUM_GAP_A = 7.0
VACUUM_AXIS = 2
SUPPORTED = ('3D', '2D')
SOURCE = 'structure_vacuum_detection'


def vacuum_gaps(structure):
    """Largest empty slab between atomic planes along each lattice direction, in Angstrom.

    Measured perpendicular to the plane spanned by the other two vectors
    (interplanar spacing 2*pi/|b_i|), including the periodic wrap-around.
    """
    lengths = [float(x) for x in structure.lattice.reciprocal_lattice.abc]
    if len(lengths) != 3 or any(not math.isfinite(x) or x <= 0 for x in lengths):
        raise ValueError('倒格矢长度无效，无法判断体系维度。')
    gaps = []
    for axis, length in enumerate(lengths):
        coords = sorted(float(site.frac_coords[axis]) % 1.0 for site in structure)
        widest = max([b - a for a, b in zip(coords, coords[1:])] + [coords[0] + 1.0 - coords[-1]])
        gaps.append(widest * 2 * math.pi / length)
    return gaps


def classify(structure):
    gaps = vacuum_gaps(structure)
    axes = [axis for axis, gap in enumerate(gaps) if gap >= VACUUM_GAP_A]
    dimension = {0: '3D', 1: '2D', 2: '1D'}.get(len(axes), '0D')
    info = {'dimension': dimension, 'vacuum_axes': axes, 'largest_empty_gap_A': gaps,
            'threshold_A': VACUUM_GAP_A, 'source': SOURCE}
    if dimension == '2D':
        info['vacuum_axis'] = axes[0]
    return info


def require_supported(info):
    if info['dimension'] not in SUPPORTED:
        raise ValueError(f"检测到 {len(info['vacuum_axes'])} 个真空方向（{info['dimension']} 体系）；"
                         '当前仅支持三维体相和二维 slab。')
    if info['dimension'] == '2D' and info['vacuum_axis'] != VACUUM_AXIS:
        raise ValueError(f"二维体系须把真空方向设为第三晶格矢量 c；当前真空在轴 {info['vacuum_axis']}。"
                         '请重排晶格矢量后重新导入。')
    return info


def is_2d(config):
    """Tasks prepared before dimensionality was recorded are 3D bulk tasks."""
    return config.get('dimensionality', {}).get('dimension') == '2D'


def check_meshes(meshes):
    if any(mesh[VACUUM_AXIS] != 1 for mesh in meshes):
        raise ValueError('二维体系真空方向（c 轴）k 网格必须为 1，请使用 [k1, k2, 1] 形式。')


def lattice_2d(structure, length_tol=1e-3, angle_tol=0.5):
    a, b, _ = structure.lattice.abc
    gamma = structure.lattice.gamma
    same = abs(a - b) <= length_tol * max(a, b)
    if same and abs(gamma - 120) <= angle_tol:
        return 'hexagonal_120'
    if same and abs(gamma - 60) <= angle_tol:
        return 'hexagonal_60'
    if abs(gamma - 90) <= angle_tol:
        return 'square' if same else 'rectangular'
    return 'oblique'


# Fractional coordinates in the reciprocal basis of the supplied cell (k3 = 0).
# For gamma=120 the reciprocal vectors meet at 60 degrees and K is (1/3, 1/3);
# for gamma=60 they meet at 120 degrees and the equivalent corner is (1/3, 2/3).
PATHS_2D = {
    'hexagonal_120': ({'\\Gamma': (0, 0), 'M': (0.5, 0), 'K': (1 / 3, 1 / 3)}, ['\\Gamma', 'M', 'K', '\\Gamma']),
    'hexagonal_60': ({'\\Gamma': (0, 0), 'M': (0.5, 0), 'K': (1 / 3, 2 / 3)}, ['\\Gamma', 'M', 'K', '\\Gamma']),
    'square': ({'\\Gamma': (0, 0), 'X': (0.5, 0), 'M': (0.5, 0.5)}, ['\\Gamma', 'X', 'M', '\\Gamma']),
    'rectangular': ({'\\Gamma': (0, 0), 'X': (0.5, 0), 'S': (0.5, 0.5), 'Y': (0, 0.5)},
                    ['\\Gamma', 'X', 'S', 'Y', '\\Gamma']),
    'oblique': ({'\\Gamma': (0, 0), 'X': (0.5, 0), 'S': (0.5, 0.5), 'Y': (0, 0.5), "S'": (0.5, -0.5)},
                ['\\Gamma', 'X', 'S', 'Y', '\\Gamma', "S'"]),
}


def band_path_2d(structure):
    """In-plane path in the supplied cell, usable as Kpoints.automatic_linemode(n, path).

    The cell is not standardized: standardization may reorder axes and move the
    vacuum off c, and its kz != 0 points carry no dispersion for an isolated slab.
    """
    kind = lattice_2d(structure)
    points, labels = PATHS_2D[kind]
    kpath = {'kpoints': {name: [float(x), float(y), 0.0] for name, (x, y) in points.items()},
             'path': [labels]}
    note = ('低对称二维格子：使用通用路径，可能未经过全部能带极值点，须用均匀网格核查带边。'
            if kind == 'oblique' else '面内高对称路径；真空方向不取点。')
    return SimpleNamespace(kpath=kpath, lattice_type=kind, note=note)
