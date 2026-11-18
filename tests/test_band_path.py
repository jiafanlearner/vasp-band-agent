"""Operator-chosen band k-paths and the bands-only fork; fake VASP only, no SSH or model traffic."""
import base64
import csv
import json
from types import SimpleNamespace as NS

import numpy as np
import pytest
from pymatgen.core import Lattice, Structure
from pymatgen.electronic_structure.bandstructure import BandStructureSymmLine
from pymatgen.electronic_structure.core import Spin
from pymatgen.io.vasp.inputs import Kpoints, Poscar

import vasp_agent.remote.remote_worker as worker
from vasp_agent.workflow import band_path as paths, full_workflow as full, stage_gate as gate, vasp_band_agent as core
from vasp_agent.workflow.band_plot import branch_ticks
from test_stage_gate import approve

SI = Structure(Lattice.cubic(5.43), ['Si'], [[0, 0, 0]])  # simple cubic: Γ, X, M, R
# La2CuO4 HTT (I4/mmm), the user's first band structure; c > a, so BCT2 in Setyawan–Curtarolo.
LCO = Structure(Lattice.tetragonal(3.80996, 13.2), ['La', 'La', 'La', 'La', 'Cu', 'Cu', 'O', 'O', 'O', 'O', 'O', 'O', 'O', 'O'],
                [[0, 0, .3613], [0, 0, .6387], [.5, .5, .8613], [.5, .5, .1387], [0, 0, 0], [.5, .5, .5],
                 [.5, 0, 0], [0, .5, 0], [0, .5, .5], [.5, 0, .5], [0, 0, .1828], [0, 0, .8172], [.5, .5, .6828], [.5, .5, .3172]])


def prepare(tmp_path, monkeypatch, name='si', band_path=None, structure=SI):
    source = tmp_path / (name + '.POSCAR')
    Poscar(structure).write_file(source)
    root = tmp_path / 'runs' / name
    full.prepare_full(NS(structure=str(source), workdir=str(root), potcar=None, encut=520, mesh=[4, 4, 4], nbands=16,
                         relax_mode='cell', ediff=1e-7, points=40, symprec=.01, force_tol=.01, stress_tol=1.,
                         energy_tol=.001, nsw=200, band_path=band_path))
    (root / 'POTCAR').write_text('test fixture')
    core.save(root / 'potential-selection.json', [{'element': 'Si', 'titel': 'test-only metadata'}])
    manifest = json.loads((root / 'manifest.json').read_text())
    manifest.update(ready=True)
    for name in ('POTCAR', 'potential-selection.json'):
        manifest['hashes'][name] = core.digest(root / name)
    core.save(root / 'manifest.json', manifest)
    monkeypatch.setattr(full.Potcar, 'from_file', lambda p: [NS(element='Si')])
    gate.confirm_physics(root, 'nonmagnetic bulk PBE bands')
    return root


@pytest.fixture
def vasp(monkeypatch):
    """Fake VASP: reuses accepted stages, writes a charge density for scf, records launches."""
    launches = []
    def execute(root, name, command, event_root=None):
        folder = root / name
        structure = Poscar.from_file(folder / 'POSCAR').structure
        if not (folder / 'complete.json').exists():
            launches.append(name)
            (folder / 'vasprun.xml').write_text('mock output ' + name)
            data = {'xml_sha256': core.digest(folder / 'vasprun.xml')}
            if name == 'scf':
                (folder / 'CHGCAR').write_text('mock charge')
                data['charge_sha256'] = core.digest(folder / 'CHGCAR')
            core.save(folder / 'complete.json', data)
        elif name == 'scf' and json.loads((folder / 'complete.json').read_text())['charge_sha256'] != core.digest(folder / 'CHGCAR'):
            raise ValueError('SCF 电荷文件已改变。')
        return NS(final_structure=structure, final_energy=-10. * len(structure), converged_ionic=True,
                  parameters={'NELM': 150, 'NBANDS': 16, 'NELECT': 4}, efermi=0.,
                  ionic_steps=[{'forces': np.zeros((len(structure), 3)).tolist(), 'stress': np.zeros((3, 3)).tolist(),
                                'electronic_steps': [{}]}])
    monkeypatch.setattr(core, 'execute', execute)
    monkeypatch.setattr(full, 'analyze_full', lambda root: core.save(root / 'workflow-state.json', {'status': 'complete'}))
    return launches


def run_phases(root, until='bands'):
    for phase in gate.PHASES[:gate.PHASES.index(until) + 1]:
        assert gate.current(root) == phase
        approve(root)
        full.run_full(root, ['mock'])


def symmline(kpoints, lattice):
    """What Vasprun.get_band_structure builds from this KPOINTS (VASP's line-mode expansion)."""
    if kpoints.style == Kpoints.supported_modes.Line_mode:
        points, labels = [], {}
        for i in range(0, len(kpoints.kpts), 2):
            a, b = np.asarray(kpoints.kpts[i]), np.asarray(kpoints.kpts[i + 1])
            points += [a + (b - a) * t for t in np.linspace(0, 1, kpoints.num_kpts)]
            labels.update({kpoints.labels[i]: a, kpoints.labels[i + 1]: b})
    else:
        points = [np.asarray(k) for k in kpoints.kpts]
        labels = {label: k for label, k in zip(kpoints.labels, kpoints.kpts) if label}
    return BandStructureSymmLine(points, {Spin.up: np.zeros((2, len(points)))}, lattice.reciprocal_lattice, 0., labels)


def test_labelled_path_resolves_records_and_runs(tmp_path, monkeypatch, vasp):
    root = prepare(tmp_path, monkeypatch, band_path='Γ-X-M-Γ|R-X')
    config = json.loads((root / 'config.json').read_text())
    assert config['band_path']['segments'][1] == {'path': [{'label': 'R'}, {'label': 'X'}], 'divisions': None}
    assert json.loads((root / 'input-plan.json').read_text(encoding='utf-8'))['band_path']['text'] == 'Γ-X-M-Γ|R-X'
    run_phases(root, until='scf')
    review = gate.context(root)['band_path']
    assert review['source'] == 'operator' and review['requested_text'] == 'Γ-X-M-Γ|R-X'
    segments = review['resolved']['segments']
    assert [[p['label'] for p in s['points']] for s in segments] == [['Γ', 'X', 'M', 'Γ'], ['R', 'X']]
    assert segments[1]['points'][0]['frac'] == [.5, .5, .5] and segments[0]['divisions'] == 40
    assert review['resolved']['lattice_type'] == 'CUB' and 'scf/POSCAR' in review['resolved']['cell']['note']
    approve(root)
    full.run_full(root, ['mock'])
    kpoints = Kpoints.from_file(root / 'bands/KPOINTS')
    assert kpoints.style == Kpoints.supported_modes.Line_mode and kpoints.num_kpts == 40
    assert kpoints.labels == ['\\Gamma', 'X', 'X', 'M', 'M', '\\Gamma', 'R', 'X']
    record = json.loads((root / 'bands-path.json').read_text(encoding='utf-8'))
    assert record['source'] == 'operator' and record['total_kpoints'] == 160
    assert json.loads((root / 'workflow-state.json').read_text())['status'] == 'complete'


def test_explicit_points_and_per_segment_density_write_an_explicit_list(tmp_path, monkeypatch, vasp):
    root = prepare(tmp_path, monkeypatch, band_path='Γ-X@10|M(0.5,0.5,0)-R(0.5,0.5,0.5)@5')
    run_phases(root)
    kpoints = Kpoints.from_file(root / 'bands/KPOINTS')
    assert kpoints.style == Kpoints.supported_modes.Reciprocal and len(kpoints.kpts) == 15
    assert [label for label in kpoints.labels if label] == ['\\Gamma', 'X', 'M', 'R']
    assert np.allclose(kpoints.kpts[11], [.5, .5, .125])
    # The plot reads the operator's labels and breaks the line between X and M.
    bs = symmline(kpoints, Poscar.from_file(root / 'bands/POSCAR').structure.lattice)
    ticks, breaks = branch_ticks(bs)
    assert [label for _, label in ticks] == ['Γ', 'X|M', 'R'] and breaks == [10]
    assert full.kpoint_lower_bound(kpoints) == 15


def test_break_starts_a_separate_plot_segment():
    lattice = SI.lattice
    record = paths.record_for({'symprec': .01, 'points': 6, 'band_path': paths.validate('Γ-X|M-R', 6)}, SI, SI)
    bs = symmline(paths.kpoints(record), lattice)
    ticks, breaks = branch_ticks(bs)
    assert [label for _, label in ticks] == ['Γ', 'X|M', 'R'] and breaks == [6]
    joined = paths.record_for({'symprec': .01, 'points': 6, 'band_path': paths.validate('Γ-X-M', 6)}, SI, SI)
    assert branch_ticks(symmline(paths.kpoints(joined), lattice))[1] == []


@pytest.mark.parametrize('text, message', [
    ('Γ-Q-X', '未知高对称点标签 Q'),           # unknown for this lattice: lists the valid ones
    ('Γ-X((0', '括号'),
    ('Γ', '2–30 个点'),
    ('Γ-X@1', '2–200'),
    ('Γ-X(2,0,0)', '|x| ≤ 1'),
    ('latimer_munro:Γ-X', 'latimer_munro'),
    ('Γ-X-X', '线段长度为零'),
])
def test_bad_path_is_refused_before_anything_is_written(tmp_path, monkeypatch, text, message):
    with pytest.raises(ValueError, match=message.replace('|', r'\|').replace('(', r'\(')):
        prepare(tmp_path, monkeypatch, band_path=text)
    assert not (tmp_path / 'runs').exists()


def test_unknown_label_lists_the_lattice_labels():
    with pytest.raises(ValueError) as caught:
        paths.provisional(paths.validate('Γ-X-M-Γ', 40), LCO, .01)
    assert 'BCT2' in str(caught.value) and all(label in str(caught.value) for label in ('Σ_1', 'Y_1', 'N', 'P', 'Z'))


def test_bct_labels_accept_aliases_and_hinuma(tmp_path):
    spec = paths.validate('G-X-P-N-GAMMA|SIGMA_1-Z@20', 40)
    labels = paths.provisional(spec, LCO, .01)['labels_on_input_cell']
    assert list(labels) == ['Γ', 'X', 'P', 'N', 'Σ_1', 'Z'] and labels['Z'] == [.5, .5, -.5]
    pytest.importorskip('seekpath')
    hinuma = paths.provisional(paths.validate('hinuma:GAMMA-X-M-GAMMA', 40), LCO, .01)['labels_on_input_cell']
    assert hinuma['M'] == pytest.approx([.5, .5, -.5])


def test_label_reused_for_another_point_is_refused():
    with pytest.raises(ValueError, match='两个不同坐标'):
        paths.record_for({'symprec': .01, 'points': 40, 'band_path': paths.validate('Γ-X-M|X(0.25,0,0)-R', 40)}, SI, SI)


@pytest.fixture
def finished(tmp_path, monkeypatch, vasp):
    root = prepare(tmp_path, monkeypatch)
    run_phases(root)
    assert gate.current(root) == 'complete'
    vasp.clear()
    return root


def snapshot(folder):
    return {str(p.relative_to(folder)): core.digest(p) for p in sorted(folder.rglob('*')) if p.is_file()}


def test_bands_fork_reuses_scf_and_needs_its_own_review(finished, vasp):
    before = snapshot(finished)
    target = finished.parent / 'si-new-path'
    answer = paths.prepare_bands_path(finished, target, 'Γ-R@12|X-M@12')
    assert snapshot(finished) == before  # the source is never touched
    assert answer['phase'] == 'bands' and not answer['scf_recomputed'] and not answer['submitted']
    assert answer['resolved']['segments'] == [[('Γ', [0, 0, 0]), ('R', [.5, .5, .5])], [('X', [0, .5, 0]), ('M', [.5, .5, 0])]]
    assert core.digest(target / 'scf/CHGCAR') == json.loads((finished / 'scf/complete.json').read_text())['charge_sha256']
    assert not (target / 'bands').exists() and not (target / 'submissions').exists()
    assert not list(target.glob('review-*.json'))
    with pytest.raises(ValueError, match='未审阅'):
        gate.require_review(target)
    review = gate.context(target)
    assert review['phase'] == 'bands' and review['band_path']['requested_text'] == 'Γ-R@12|X-M@12'
    approve(target)
    full.run_full(target, ['mock'])
    assert vasp == ['bands']  # SCF and everything before it are reused, not rerun
    kpoints = Kpoints.from_file(target / 'bands/KPOINTS')
    assert kpoints.labels == ['\\Gamma', 'R', 'X', 'M'] and kpoints.num_kpts == 12
    assert json.loads((target / 'workflow-state.json').read_text())['status'] == 'complete'


def test_bands_fork_refuses_changed_charge_and_unknown_labels(finished):
    target = finished.parent / 'fork'
    with pytest.raises(ValueError, match='未知高对称点标签 Q'):
        paths.prepare_bands_path(finished, target, 'Γ-Q')
    assert not target.exists()
    (finished / 'scf/CHGCAR').write_text('tampered charge')
    with pytest.raises(ValueError, match='电荷'):
        paths.prepare_bands_path(finished, target, 'Γ-R')
    assert not target.exists()


def test_bands_fork_requires_a_passed_scf(tmp_path, monkeypatch, vasp):
    root = prepare(tmp_path, monkeypatch)
    run_phases(root, until='relaxation')
    with pytest.raises(ValueError, match='SCF 尚未通过'):
        paths.prepare_bands_path(root, tmp_path / 'fork', 'Γ-X')


def test_review_and_run_must_agree(tmp_path, monkeypatch, vasp):
    root = prepare(tmp_path, monkeypatch, band_path='Γ-X')
    run_phases(root, until='scf')
    approve(root)
    real = paths.record_for
    def drifted(config, structure, primitive, sc_path=None):
        record = real(config, structure, primitive, sc_path)
        # Only the run passes its in-memory SC path; the review reads the cell files.
        return {**record, 'kpoints_style': 'Reciprocal'} if sc_path is not None else record
    monkeypatch.setattr(paths, 'record_for', drifted)
    with pytest.raises(ValueError, match='审阅时显示的不一致'):
        full.run_full(root, ['mock'])
    assert not (root / 'bands/vasprun.xml').exists()


def test_old_engine_refuses_a_path_instead_of_running_the_automatic_one(tmp_path, monkeypatch):
    old = NS(SCAN_PLAN_VERSION=1, run_stages=True, prepare_full=lambda args: pytest.fail('old engine must not prepare'))
    monkeypatch.setattr(worker, 'deployed_release', lambda root: tmp_path)
    monkeypatch.setattr(worker, 'activate_release', lambda release: tmp_path)
    monkeypatch.setattr(worker, 'engine_module', lambda name: old)
    monkeypatch.setattr(worker, 'inspect_environment', lambda cfg, mode: {'executable_sha256': 'x'})
    monkeypatch.setattr(worker, 'validate_scheduler', lambda cfg: None)
    cfg = {'vasp_env': __file__, 'potcar_library': str(tmp_path), 'ntasks': 8, 'partition': 'p', 'walltime': '01:00:00'}
    spec = paths.validate('Γ-X', 40)
    payload = {'structure': base64.b64encode(str(Poscar(SI)).encode()).decode(),
               'parameters': {'encut': 520, 'mesh': [4, 4, 4], 'nbands': 16, 'relax_mode': 'cell', 'band_path': spec}}
    with pytest.raises(ValueError, match='部署新版'):
        worker._prepare_task(payload, cfg, tmp_path, 'new', tmp_path / 'jobs/new', False)
    with pytest.raises(ValueError, match='部署新版'):
        worker._prepare_bands_path({'source_job': 'src', 'band_path': spec}, cfg, tmp_path, 'new', tmp_path / 'jobs/new')
    assert not (tmp_path / 'jobs').exists()


def test_slab_keeps_the_automatic_path_and_refuses_an_operator_path(tmp_path, monkeypatch):
    from test_slab_2d import mos2
    with pytest.raises(ValueError, match='二维 slab'):
        prepare(tmp_path, monkeypatch, band_path='Γ-M-K-Γ', structure=mos2())
    assert not (tmp_path / 'runs').exists()
    from vasp_agent.workflow import dimensionality
    config = {'symprec': .01, 'points': 40, 'dimensionality': dimensionality.classify(mos2())}
    record = paths.record_for(config, mos2(), mos2())
    assert record['source'] == 'automatic' and record['convention'] == 'in_plane_2d'
    assert [p['kpoints_label'] for p in record['segments'][0]['points']] == ['\\Gamma', 'M', 'K', '\\Gamma']


def test_csv_and_report_record_the_path_source(tmp_path):
    from test_band_plot import cubic_bs
    from vasp_agent.workflow.band_analysis import COLUMNS, EXTRA_COLUMNS, parse_bands
    from vasp_agent.workflow.band_plot import replot_folder
    report = core.export_bands(cubic_bs(), tmp_path, {'formula': 'Po', 'band_path': {'source': 'operator'}})
    assert report['band_path_source'] == 'operator' and report['band_path'] == {'source': 'operator'}
    rows = list(csv.reader((tmp_path / 'bands.csv').open(encoding='utf-8')))
    assert tuple(rows[0]) == COLUMNS + EXTRA_COLUMNS and {row[-1] for row in rows[1:]} == {'operator'}
    parsed = parse_bands((tmp_path / 'bands.csv').read_text(encoding='utf-8'))
    assert parsed['labels'][0] == '\\Gamma' and parsed['labels'][1] is None
    assert replot_folder(tmp_path)['ticks'] == ['Γ', 'X', 'M', 'Γ', 'R', 'X|M', 'R']


def test_tools_validate_and_forward_the_path(tmp_path, monkeypatch):
    from vasp_agent.chat import ai_agent as ai
    from vasp_agent.remote.remote_compute import RemoteCompute
    box = ai.Toolbox(tmp_path)
    calls = []
    monkeypatch.setattr(RemoteCompute, '__init__', lambda *a: None)
    monkeypatch.setattr(RemoteCompute, 'call', lambda self, action, job, **kw: calls.append((action, job, kw)) or {})
    monkeypatch.setattr(box, 'ensure_current_release', lambda server: None)
    box.prepare_bands_path('hpc', 'La2CuO4_HTT_Cu_pv_780_ions_32c', 'lco-gxpng', 'Γ-X-P-N-Γ')
    assert calls[0][0] == 'prepare_bands_path' and calls[0][2]['band_path'] == paths.validate('Γ-X-P-N-Γ', 40)
    output = box.call('prepare_bands_path', {'server': 'hpc', 'source_job': 'a', 'job_name': 'b', 'band_path': 'Γ-X(('})
    assert not output['ok'] and '括号' in output['error'] and len(calls) == 1
    schema = next(t for t in ai.TOOLS if t['name'] == 'propose_scan_plan')
    assert 'band_path' in schema['parameters']['required']


# --- Candidate paths and the operator menu -------------------------------------

def sc_cell(structure):
    from pymatgen.symmetry.bandstructure import HighSymmKpath
    return HighSymmKpath(structure, symprec=.01).prim


def resolves(candidate, structure, primitive, config):
    paths.record_for({**config, 'band_path': paths.validate(candidate['text'], 40)}, structure, primitive)
    return True


def test_bct2_cuprate_candidates_include_the_layered_in_plane_path():
    config = {'symprec': .01, 'points': 40}
    data = paths.candidates(config, LCO, sc_cell(LCO))
    found = {c['key']: c for c in data['candidates']}
    assert data['lattice_type'] == 'BCT2' and 3 <= len(found) <= 5
    assert found['automatic']['used_by_first_run'] and found['automatic']['text'] == 'Γ-X-Y-Σ-Γ-Z-Σ_1-N-P-Y_1-Z|X-P'
    assert found['layered']['text'] == 'Γ-X(-0.25,0.25,0.25)-M(0,0,0.5)-Γ-Z'
    assert 'c/max(a,b) = 3.46' in found['layered']['rationale'] and not found['layered']['used_by_first_run']
    if 'hinuma' not in found:
        assert any(o['key'] == 'hinuma' and 'seekpath' in o['reason'] for o in data['omitted'])
    assert all(resolves(c, LCO, sc_cell(LCO), config) for c in data['candidates'])
    assert paths.candidates(config, LCO, sc_cell(LCO)) == data  # deterministic


def test_cubic_cell_is_not_layered_and_extrema_come_from_the_first_run(tmp_path):
    from test_band_plot import CUBIC
    from pymatgen.symmetry.bandstructure import HighSymmKpath
    kpath = HighSymmKpath(CUBIC, symprec=.01).kpath
    points = []
    for branch in kpath['path']:
        for start, end in zip(branch, branch[1:]):
            a, b = np.array(kpath['kpoints'][start]), np.array(kpath['kpoints'][end])
            points += [a + (b - a) * t for t in np.linspace(0, 1, 11)]
    reciprocal = CUBIC.lattice.reciprocal_lattice.matrix
    def pocket(center, radius):  # occupied outside a small sphere around a vertex: E_F crossings near it
        return np.array([radius - np.linalg.norm((k - np.array(center)) @ reciprocal) for k in points])
    bands = np.array([pocket([.5, .5, .5], .3), pocket([0, .5, 0], .2), np.full(len(points), 3.)])
    bs = BandStructureSymmLine(points, {Spin.up: bands}, CUBIC.lattice.reciprocal_lattice, 0., kpath['kpoints'])
    core.export_bands(bs, tmp_path, {'formula': 'Po'})
    config = {'symprec': .01, 'points': 40}
    data = paths.candidates(config, CUBIC, CUBIC, (tmp_path / 'bands.csv').read_text(encoding='utf-8'))
    found = {c['key']: c for c in data['candidates']}
    assert 'layered' not in found
    assert any(o['key'] == 'layered' and '非层状' in o['reason'] for o in data['omitted'])
    assert found['extrema']['text'] == 'Γ-X-R' and '不是全布里渊区搜索' in found['extrema']['rationale']
    assert all(resolves(c, CUBIC, CUBIC, config) for c in data['candidates'])


def test_slab_offers_only_the_automatic_path():
    from test_slab_2d import mos2
    from vasp_agent.workflow import dimensionality
    config = {'symprec': .01, 'points': 40, 'dimensionality': dimensionality.classify(mos2())}
    data = paths.candidates(config, mos2(), mos2())
    assert [(c['key'], c['selectable']) for c in data['candidates']] == [('automatic', False)]
    assert paths.record_for(config, mos2(), mos2())['total_kpoints'] == data['candidates'][0]['total_kpoints']


def test_ai_suggestion_is_validated_and_labelled(finished):
    data = paths.candidates_for_task(finished, 'Γ-M-R', '对角线方向')
    (ai,) = [c for c in data['candidates'] if c['origin'] == 'ai_suggestion']
    assert ai['text'] == 'Γ-M-R' and ai['rationale'] == '对角线方向' and ai['selectable']
    data = paths.candidates_for_task(finished, 'Γ-Q')
    assert not any(c['origin'] == 'ai_suggestion' for c in data['candidates'])
    assert any(o['key'] == 'ai_suggestion' and '未知高对称点标签 Q' in o['reason'] for o in data['omitted'])


def menu_box(tmp_path, answers):
    from vasp_agent.chat import ai_agent as ai
    box = ai.Toolbox(tmp_path)
    shown = []
    box.plan_output = shown.append
    box.plan_reader = (lambda prompt: answers.pop(0)) if answers is not None else None
    return box, shown


def test_menu_needs_an_operator_pick(finished):
    box, _ = menu_box(finished.parent.parent, None)
    result = box.request_band_path_choice(None, 'si')
    assert result['status'] == 'operator_confirmation_required' and not result['prepared']
    assert len(result['candidates']) >= 2 and sorted(p.name for p in (finished.parent).iterdir()) == ['si']


def test_menu_pick_forks_with_the_chosen_path(finished, vasp):
    box, shown = menu_box(finished.parent.parent, ['x', '2'])
    data = paths.candidates_for_task(finished)
    second = [c for c in data['candidates'] if c['selectable']][1]
    result = box.request_band_path_choice(None, 'si')
    assert '能带路径候选' in shown[0] and '无效选择' in shown[1]
    assert result['status'] == 'prepared' and result['job_name'] == 'si_path2'
    assert result['selection'] == {'source': 'operator_menu', 'operator_confirmed': True, 'choice': '2',
                                   'key': second['key'], 'text': second['text']}
    record = json.loads((finished.parent / 'si_path2/config.json').read_text())['band_path']
    assert paths.render(record) == second['text'] and gate.current(finished.parent / 'si_path2') == 'bands'


def test_menu_reasks_an_invalid_custom_path_without_writing(finished):
    box, shown = menu_box(finished.parent.parent, ['c', 'Γ-Q', 'q'])
    result = box.request_band_path_choice(None, 'si')
    assert result['status'] == 'cancelled' and any('未知高对称点标签 Q' in line for line in shown)
    assert sorted(p.name for p in finished.parent.iterdir()) == ['si']
    box, _ = menu_box(finished.parent.parent, ['c', 'Γ-R@10'])
    result = box.request_band_path_choice(None, 'si')
    assert result['job_name'] == 'si_pathc' and result['band_path'] == 'Γ-R@10'


def test_menu_job_names_fit_the_task_name_limit():
    from vasp_agent.chat.band_path_menu import job_name
    assert job_name('La2CuO4_HTT_Cu_pv_780_ions_32c', 3) == 'La2CuO4_HTT_Cu_pv_780_ions_32c_path3'
    assert len(job_name('a' * 64, 12)) == 64


def test_old_engine_refuses_candidates(tmp_path, monkeypatch):
    monkeypatch.setattr(worker, 'deployed_release', lambda root: tmp_path)
    monkeypatch.setattr(worker, 'activate_release', lambda release: tmp_path)
    monkeypatch.setattr(worker, 'engine_module', lambda name: NS(BAND_PATH_VERSION=1))
    with pytest.raises(ValueError, match='部署新版'):
        worker._band_path_candidates({}, {}, tmp_path, 'src', tmp_path / 'jobs/src')
