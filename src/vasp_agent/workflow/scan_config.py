"""Exact three-point scans and deterministic input verification."""
import json
import math
from pymatgen.io.vasp.inputs import Incar, Kpoints


def mesh_ladder(base, factors=(1, 1.25, 1.5), fixed_axes=()):
    """Scale a base mesh into strictly denser levels.

    Integer rounding collapses levels on coarse axes ([2,2,2] -> [3,3,3] twice);
    then the axes least refined relative to the base gain one point each, which
    keeps symmetric meshes symmetric instead of failing validate_scans.
    fixed_axes (a slab's vacuum direction) keep their base value at every level.
    """
    meshes = []
    for factor in factors:
        mesh = [n if axis in fixed_axes else max(1, math.ceil(n * factor)) for axis, n in enumerate(base)]
        if meshes:
            mesh = [max(n, old) for n, old in zip(mesh, meshes[-1])]
            if mesh == meshes[-1]:
                free = [axis for axis in range(3) if axis not in fixed_axes]
                lowest = min(mesh[axis] / base[axis] for axis in free)
                mesh = [m + 1 if axis in free and m / n == lowest else m
                        for axis, (m, n) in enumerate(zip(mesh, base))]
        meshes.append(mesh)
    return meshes


def validate_scans(encuts, meshes):
    if not isinstance(encuts, list) or len(encuts) != 3 or any(type(x) not in (int, float) or not math.isfinite(x) or not 200 <= x <= 3000 for x in encuts):
        raise ValueError('ENCUT 必须为三个 200–3000 eV 的有限数值。')
    if not all(a < b for a, b in zip(encuts, encuts[1:])):
        raise ValueError('ENCUT 扫描必须严格递增。')
    if not isinstance(meshes, list) or len(meshes) != 3 or any(not isinstance(row, list) or len(row) != 3 or any(type(x) is not int or not 1 <= x <= 128 for x in row) for row in meshes):
        raise ValueError('meshes 必须为三组整数三维网格，每轴 1–128。')
    if any(any(x > y for x, y in zip(a, b)) or a == b for a, b in zip(meshes, meshes[1:])):
        raise ValueError('网格须逐级加密，各轴不能减小，不能重复。')
    return {'encuts': encuts, 'meshes': meshes}


def verify_scan_inputs(root):
    config = json.loads((root / 'config.json').read_text(encoding='utf-8'))
    active = config.get('active_scans', ['encut', 'mesh'])
    if active not in (['encut'], ['mesh'], ['encut', 'mesh']):
        raise ValueError('扫描执行清单无效。')
    if len(active) == 1:
        from vasp_agent.workflow.convergence_reuse import reused_result
        kind, _ = reused_result(root)
        if kind in active:
            raise ValueError('复用参数与执行清单冲突。')
    from vasp_agent.workflow.convergence_reuse import fixed_cutoff
    cut = fixed_cutoff(config)
    scans = validate_scans(config['encuts'], config['meshes'])
    choice = root / 'parameter-choice.json'
    if choice.exists():
        selected = json.loads(choice.read_text(encoding='utf-8'))['selected']
        if {k: selected[k] for k in ('encuts', 'meshes')} != scans:
            raise ValueError('实际配置与最终确认方案不一致。')
    checked = []
    for kind, values in [('encut', scans['encuts']), ('mesh', scans['meshes'])]:
        if kind not in config.get('active_scans', ['encut', 'mesh']):
            if not (root / 'convergence-reuse.json').exists():
                raise ValueError('跳过扫描必须有复用证据。')
            continue
        for index, value in enumerate(values):
            folder = root / f'convergence/{kind}-{index}'
            incar = Incar.from_file(folder / 'INCAR')
            points = Kpoints.from_file(folder / 'KPOINTS')
            encut = value if kind == 'encut' else cut
            mesh = scans['meshes'][-1] if kind == 'encut' else value
            if incar.get('ENCUT') != encut or [list(row) for row in points.kpts] != [mesh] or points.style.name != 'Gamma' or tuple(points.kpts_shift) != (0,0,0):
                raise ValueError(f'{kind}-{index} 实际 INCAR/KPOINTS 与确认方案不一致。')
            checked.append(folder.relative_to(root).as_posix())
    if config.get('fixed_encut') is not None:
        folder = root / 'convergence/cross-cutoff'
        points = Kpoints.from_file(folder / 'KPOINTS')
        if (Incar.from_file(folder / 'INCAR').get('ENCUT') != config['encuts'][-1]
                or [list(row) for row in points.kpts] != [config['meshes'][-1]]
                or points.style.name != 'Gamma' or tuple(points.kpts_shift) != (0, 0, 0)):
            raise ValueError('交叉复核输入与方案不一致。')
        checked.append('convergence/cross-cutoff')
    return {'fixed_encut': config.get('fixed_encut'), 'matched': True, 'checked': checked, **scans}
