"""Geometry-based Gamma mesh proposals, not convergence certification."""
import math

from vasp_agent.workflow import dimensionality


def suggest_meshes(structure):
    lengths = [float(x) for x in structure.lattice.reciprocal_lattice.abc]
    if len(lengths) != 3 or any(not math.isfinite(x) or x <= 0 for x in lengths):
        raise ValueError('倒格矢长度无效，无法建议网格。')
    dimension = dimensionality.classify(structure)
    # A supported slab samples the vacuum direction with one point at every level.
    pinned = (dimensionality.VACUUM_AXIS,) if dimension.get('vacuum_axis') == dimensionality.VACUUM_AXIS \
        and dimension['dimension'] == '2D' else ()
    free = [axis for axis in range(3) if axis not in pinned]
    # pymatgen reciprocal_lattice includes 2*pi. Units: inverse Angstrom.
    spacings = [0.25, 0.20, 0.16]
    meshes = []
    for spacing in spacings:
        mesh = [1 if axis in pinned else max(1, math.ceil(length / spacing)) for axis, length in enumerate(lengths)]
        if meshes and mesh == meshes[-1]:
            # Discrete rounding can collapse successive densities for large cells.
            axis = max(free, key=lambda i: lengths[i] / mesh[i])
            mesh[axis] += 1
        if meshes:
            mesh = [max(n, old) for n, old in zip(mesh, meshes[-1])]
            if mesh == meshes[-1]:
                axis = max(free, key=lambda i: lengths[i] / mesh[i])
                mesh[axis] += 1
        if max(mesh) > 128:
            raise ValueError('建议网格超过每轴128的支持范围；需要人工检查晶胞。')
        meshes.append(mesh)
    counts = [math.prod(m) for m in meshes]
    result = {'origin': 'geometry_spacing_heuristic', 'gamma_centered': True,
        'reciprocal_lengths_inv_A': lengths, 'includes_2pi': True,
        'target_spacings_inv_A': spacings, 'meshes': meshes,
        'actual_spacings_inv_A': [[b/n for b,n in zip(lengths,m)] for m in meshes],
        'full_grid_points': counts, 'total_full_grid_points': sum(counts),
        'relative_to_8_10_12': sum(counts)/3240,
        'convergence_verified': False,
        'centering_source': 'workflow_writes_gamma_automatic',
        'ratio_note': 'relative_to_8_10_12 仅为总点数之比（8³+10³+12³=3240），不按轴归一化，不是收敛结论。',
        'dimensionality': dimension,
        'largest_empty_gap_A': dimension['largest_empty_gap_A'], 'vacuum_axes': dimension['vacuum_axes'],
        'note': '按含2π的倒格矢长度/目标间距向上取整；0.25/0.20/0.16 Å⁻¹为启发式默认，非材料精度标准。'
                '取整重复时加密当前最粗方向。未做对称性约化；点数不是耗时预测。'
                '用户明确网格优先，必须验收能量/力/应力及优化后精度；磁性和金属性另行审阅。'}
    if pinned:
        result['slab_note'] = ('检测为二维 slab（真空沿 c）：c 方向固定 1 个 k 点，只加密面内；'
                               '优化只能用 relax_mode=ions；真空层厚度与偶极修正需另行评估。')
    elif dimension['vacuum_axes']:
        try:
            dimensionality.require_supported(dimension)
        except ValueError as exc:
            result['dimensionality_warning'] = str(exc) + '以上网格未按低维体系处理，不能直接采用。'
    return result
