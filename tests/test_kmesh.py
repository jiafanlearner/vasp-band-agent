import math
import pytest
from pymatgen.core import Structure, Lattice
from vasp_agent.planning.kmesh import suggest_meshes
from vasp_agent.workflow.scan_config import validate_scans


def test_layered_cell_uses_fewer_points_on_long_axis():
    # Atoms every quarter of c: a long bulk cell, not a slab.
    s = Structure(Lattice.orthorhombic(3.8,3.8,13.2), ['Cu'] * 4, [[0,0,z] for z in (0, .25, .5, .75)])
    r = suggest_meshes(s)
    assert r['meshes'] == [[7,7,2],[9,9,3],[11,11,3]]
    assert r['includes_2pi'] and not r['convergence_verified']
    assert r['reciprocal_lengths_inv_A'][2] == pytest.approx(2*math.pi/13.2)
    assert r['total_full_grid_points'] == 704
    validate_scans([520,598,676],r['meshes'])


def test_nonorthogonal_uses_reciprocal_vectors():
    s = Structure(Lattice.from_parameters(4,5,6,80,75,65), ['Si'], [[0,0,0]])
    r = suggest_meshes(s)
    for mesh, spacing in zip(r['meshes'],r['target_spacings_inv_A']):
        assert mesh == [math.ceil(b/spacing) for b in s.lattice.reciprocal_lattice.abc]


def test_large_cell_rounding_distinct_and_monotonic():
    r = suggest_meshes(Structure(Lattice.cubic(100), ['Si'], [[0,0,0]]))
    validate_scans([520,598,676],r['meshes'])
    assert len({tuple(m) for m in r['meshes']}) == 3


def test_inspect_exposes_proposal_without_changing_structure(tmp_path):
    from vasp_agent.chat.ai_agent import Toolbox
    from pymatgen.io.vasp.inputs import Poscar
    path = tmp_path/'POSCAR'
    Poscar(Structure(Lattice.orthorhombic(3.8,3.8,13.2), ['Cu'] * 4, [[0,0,z] for z in (0, .25, .5, .75)])).write_file(path)
    before = path.read_bytes()
    r = Toolbox(tmp_path).inspect_structure('POSCAR')
    assert r['kmesh_suggestion']['meshes'][0] == [7,7,2]
    assert path.read_bytes() == before


def test_mesh_above_supported_bound_rejected():
    with pytest.raises(ValueError,match='128'):
        suggest_meshes(Structure(Lattice.cubic(.1),['Si'],[[0,0,0]]))


def test_slab_pins_vacuum_axis_to_one_point():
    s = Structure(Lattice.orthorhombic(3.0, 3.0, 25.0), ['Mo', 'S', 'S'],
                  [[0, 0, 0.5], [0.5, 0.5, 0.44], [0.5, 0.5, 0.56]])
    r = suggest_meshes(s)
    assert r['vacuum_axes'] == [2] and r['dimensionality']['dimension'] == '2D'
    assert r['largest_empty_gap_A'][2] == pytest.approx(25.0 * 0.88)
    assert all(m[2] == 1 for m in r['meshes']) and 'slab_note' in r
    validate_scans([520, 598, 676], r['meshes'])


def test_vacuum_off_c_axis_is_warned_not_pinned():
    s = Structure(Lattice.orthorhombic(25.0, 3.0, 3.0), ['Mo'], [[0.5, 0, 0]])
    r = suggest_meshes(s)
    assert '第三晶格矢量' in r['dimensionality_warning']
    assert r['meshes'][0][0] > 1


def test_bulk_has_no_vacuum_warning():
    s = Structure(Lattice.cubic(5.43), ['Si', 'Si'], [[0, 0, 0], [0.25, 0.25, 0.25]])
    r = suggest_meshes(s)
    assert r['vacuum_axes'] == [] and 'dimensionality_warning' not in r and 'slab_note' not in r


@pytest.mark.parametrize('base,expected', [
    ([2, 2, 2], [[2, 2, 2], [3, 3, 3], [4, 4, 4]]),
    ([1, 1, 2], [[1, 1, 2], [2, 2, 3], [2, 2, 4]]),
    ([1, 1, 1], [[1, 1, 1], [2, 2, 2], [3, 3, 3]]),
    ([8, 8, 8], [[8, 8, 8], [10, 10, 10], [12, 12, 12]]),
])
def test_mesh_ladder_never_repeats_after_rounding(base, expected):
    from vasp_agent.workflow.scan_config import mesh_ladder
    assert mesh_ladder(base) == expected
    validate_scans([520, 598, 676], expected)


def test_mesh_ladder_keeps_fixed_axes():
    from vasp_agent.workflow.scan_config import mesh_ladder
    assert mesh_ladder([2, 2, 1], fixed_axes=(2,)) == [[2, 2, 1], [3, 3, 1], [4, 4, 1]]
    assert mesh_ladder([6, 6, 1], fixed_axes=(2,)) == [[6, 6, 1], [8, 8, 1], [9, 9, 1]]
