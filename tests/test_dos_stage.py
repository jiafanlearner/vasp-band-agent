"""Three-dimensional DOS stage (step C): new tasks and DOS-only forks; fake VASP only, no SSH or real jobs."""
import csv
import json
from types import SimpleNamespace as NS

import numpy as np
import pytest
from pymatgen.core import Lattice, Structure
from pymatgen.electronic_structure.core import Orbital, Spin
from pymatgen.io.vasp.inputs import Incar, Poscar

import vasp_agent.remote.remote_worker as worker
from vasp_agent.remote.remote_compute import delivery_summary
from vasp_agent.workflow import (dos_plot, dos_stage, full_workflow as full, parameter_specs as specs,
                                 stage_definitions as stages, stage_gate as gate, vasp_band_agent as core)
from vasp_agent.workflow.band_analysis import COLUMNS

SI = Structure(Lattice.cubic(5.43), ['Si'], [[0, 0, 0]])  # one atom; SCF mesh = the last scan mesh ([6, 6, 6] from [4, 4, 4])
DOS = specs.spec('dos')
CONTEXT = {'dimension': '3D'}
NELECT = 4.0
SCF_NBANDS = 16


def dos_values(**changes):
    return {**{name: entry['value'] for name, entry in specs.suggestions(DOS, CONTEXT).items()}, **changes}


def chosen(**changes):
    return {'dos': {'spec': {'id': 'dos', 'version': DOS['version']}, 'values': specs.validate(DOS, dos_values(**changes), CONTEXT)}}


def args(tmp_path, name, mesh=(4, 4, 4), **extra):
    source = tmp_path / (name + '.POSCAR')
    Poscar(SI).write_file(source)
    return NS(structure=str(source), workdir=str(tmp_path / 'runs' / name), potcar=None, encut=520, mesh=list(mesh),
              nbands=SCF_NBANDS, relax_mode='cell', ediff=1e-7, points=40, symprec=.01, force_tol=.01, stress_tol=1.,
              energy_tol=.001, nsw=200, **extra)


def make_ready(root, monkeypatch):
    (root / 'POTCAR').write_text('test fixture')
    core.save(root / 'potential-selection.json', [{'element': 'Si', 'titel': 'test-only metadata'}])
    manifest = json.loads((root / 'manifest.json').read_text(encoding='utf-8'))
    manifest.update(ready=True)
    for name in ('POTCAR', 'potential-selection.json'):
        manifest['hashes'][name] = core.digest(root / name)
    core.save(root / 'manifest.json', manifest)
    monkeypatch.setattr(full.Potcar, 'from_file', lambda p: [NS(element='Si')])
    gate.confirm_physics(root, 'nonmagnetic bulk PBE DOS')
    return root


def prepare(tmp_path, monkeypatch, name='si', dos=True, mesh=(4, 4, 4), **changes):
    extra = {'optional_stages': ['dos'], 'stage_parameters': chosen(**changes)} if dos else {}
    full.prepare_full(args(tmp_path, name, mesh, **extra))
    return make_ready(tmp_path / 'runs' / name, monkeypatch)


class Walltime(Exception):
    """Slurm killing the job at its time limit."""


def gaussian(energies, centre, width, weight):
    return weight * np.exp(-0.5 * ((energies - centre) / width) ** 2) / (width * np.sqrt(2 * np.pi))


@pytest.fixture
def vasp(monkeypatch):
    """Fake VASP with execute()'s rules; DOS runs return a DOS shaped like a semiconductor with a semicore level.

    SCF: E_F = 0, 16 bands; band minima -12 (semicore), -4, then 1..8 eV. The
    DOS run integrates to 4 electrons at its E_F (0.02 eV) when EMIN lies below -12.
    """
    state = NS(launches=[], interrupt=set(), scale=1.0, nan=False, top=8.0, no_pdos=False,
               scf_minima=[-12.0, -4.0] + list(np.linspace(1.0, 8.0, SCF_NBANDS - 2)))

    def dos_result(folder):
        incar = Incar.from_file(folder / 'INCAR')
        energies = np.linspace(incar['EMIN'], incar['EMAX'], incar['NEDOS'])
        total = state.scale * (gaussian(energies, -12, 0.3, 2) + gaussian(energies, -4, 0.8, 2)
                               + gaussian(energies, 3, 1.0, 4))
        if state.nan:
            total[5] = np.nan
        integrated = np.concatenate([[0.], np.cumsum((total[1:] + total[:-1]) / 2 * np.diff(energies))])
        site = {} if state.no_pdos else {Orbital.s: {Spin.up: 0.4 * total}, **{o: {Spin.up: 0.4 * total / 3}
                                                                              for o in (Orbital.px, Orbital.py, Orbital.pz)}}
        nbands, nk = int(incar['NBANDS']), 11
        minima = [-12.0, -4.0] + list(np.linspace(1.0, state.top, nbands - 2))
        eigen = np.zeros((nk, nbands, 2))
        eigen[:, :, 0] = minima
        eigen[:, :2, 1] = 1.0
        return NS(tdos=NS(energies=energies, densities={Spin.up: total}),
                  idos=NS(energies=energies, densities={Spin.up: integrated}), pdos=[site], atomic_symbols=['Si'],
                  efermi=0.02, parameters={'NBANDS': nbands, 'NELECT': NELECT}, incar=dict(incar),
                  actual_kpoints=[[0, 0, 0]] * nk, actual_kpoints_weights=[1 / nk] * nk,
                  eigenvalues={Spin.up: eigen})

    def execute(root, name, command, event_root=None):
        folder = root / name
        structure = Poscar.from_file(folder / 'POSCAR').structure
        if (folder / 'complete.json').exists():
            stamp = json.loads((folder / 'complete.json').read_text(encoding='utf-8'))
            if stamp['xml_sha256'] != core.digest(folder / 'vasprun.xml'):
                raise ValueError(f'{name} 输出与完成记录不一致。')
            if name == 'scf' and stamp['charge_sha256'] != core.digest(folder / 'CHGCAR'):
                raise ValueError('SCF 电荷文件已改变。')
        else:
            if any((folder / f).exists() for f in ('vasprun.xml', 'OUTCAR', 'stdout.log')):
                raise ValueError(f'{name} 存在未验收输出，请检查日志并在新目录重建任务。')
            state.launches.append(name)
            (folder / 'stdout.log').write_text('running\n')
            if name in state.interrupt:
                (folder / 'OUTCAR').write_text('partial')
                raise Walltime(name)
            (folder / 'vasprun.xml').write_text('mock output ' + name)
            data = {'xml_sha256': core.digest(folder / 'vasprun.xml')}
            if name == 'scf':
                (folder / 'CHGCAR').write_text('mock charge')
                data['charge_sha256'] = core.digest(folder / 'CHGCAR')
            core.save(folder / 'complete.json', data)
        if name == 'dos':
            return dos_result(folder)
        return NS(final_structure=structure, final_energy=-10. * len(structure), converged_ionic=True,
                  parameters={'NELM': 150, 'NBANDS': SCF_NBANDS, 'NELECT': NELECT}, efermi=0.,
                  ionic_steps=[{'forces': np.zeros((len(structure), 3)).tolist(), 'stress': np.zeros((3, 3)).tolist(),
                                'electronic_steps': [{}]}])

    def analyze(root):
        # A bands.csv on the SCF E_F scale, so the combined figure has something to draw.
        with (root / 'bands.csv').open('w', newline='', encoding='utf-8') as out:
            writer = csv.writer(out)
            writer.writerow(COLUMNS)
            for band, offset in ((1, -4.0), (2, 2.0)):
                for k in range(3):
                    writer.writerow(['1', band, k, 0.1 * k, 0.25 * k, 0, 0, offset + 0.3 * k, offset + 0.3 * k])
        core.save(root / 'report.json', {'formula': 'Si', 'workflow': 'full', 'scf_fermi_energy_eV': 0.0,
                                         'band_validation': {'passed': True}, 'sampled_path_gap': {'energy': 0.5}})
        for name in ('bands.png', 'bands.pdf'):
            (root / name).write_bytes(b'figure')
        config = json.loads((root / 'config.json').read_text(encoding='utf-8'))
        if stages.is_final(config, 'bands'):
            core.save(root / 'workflow-state.json', {'status': 'complete'})

    monkeypatch.setattr(core, 'execute', execute)
    monkeypatch.setattr(full, 'analyze_full', analyze)
    monkeypatch.setattr(dos_stage, 'scf_bands', lambda root: {'band_minima_eV': list(state.scf_minima),
                                                             'lowest_eigenvalue_eV': min(state.scf_minima), 'kpoints': 10})
    return state


def checks(data, judgment='pass'):
    return [dict(item=item, judgment='pending' if item == 'validation_plan' else judgment, blocking=judgment == 'blocked',
                 evidence='fixture', reason='fixture', next_action='无需') for item in data['required_checks']]


def approve(root):
    data = gate.context(root)
    return gate.record(root, data['snapshot'], data['phase'], checks(data), 'test-model')


def run_until(root, last):
    while gate.current(root) != 'complete':
        phase = gate.current(root)
        approve(root)
        full.run_full(root, ['mock'])
        if phase == last:
            return


def snapshot(folder):
    return {p.relative_to(folder).as_posix(): p.read_bytes() for p in sorted(folder.rglob('*')) if p.is_file()}


# --- The spec is executable -------------------------------------------------------

def test_dos_spec_v2_is_executable_and_v1_stays_superseded():
    assert stages.DEFINITIONS['dos'][1]['parameter_spec'] == {'id': 'dos', 'version': 2}
    assert stages.DEFINITIONS['dos'][1]['depends_on'] == ('scf',) and stages.DEFINITIONS['dos'][1]['guide'] == 'dos'
    assert DOS['executable'] and 'not_executable_reason' not in specs.describe(DOS, CONTEXT)
    assert specs.spec('dos', 1)['executable'] is False and '由 v2 取代' in specs.spec('dos', 1)['not_executable_reason']
    assert stages.with_optional(['dos']) == ('convergence', 'relaxation', 'scf', 'bands', 'dos')
    assert full.DOS_VERSION == dos_stage.DOS_VERSION == 1 and 'dos' in full.STAGE_RUNNERS


def evidence(**changes):
    return {'dimension': '3D', 'scf_mesh': [8, 8, 8], 'scf_kpoints_style': 'Gamma', 'scf_fermi_eV': 5.25,
            'scf_nbands': 4, 'band_minima_eV': [-14.0, -3.0, 2.0, 7.0], 'lowest_eigenvalue_eV': -14.0, **changes}


def test_generated_inputs_cover_the_counting_range_and_keep_the_sampling_interval():
    generated = DOS['generate'](specs.validate(DOS, dos_values(), CONTEXT), evidence())
    incar = generated['incar']
    assert generated['blocked'] == [] and incar['ISMEAR'] == -5 and 'SIGMA' not in incar and incar['NBANDS'] == 4
    # EMIN: the lower of the window (-10) and the lowest band minus the margin (-16), absolute.
    assert incar['EMIN'] == 5.25 - 16.0 and incar['EMAX'] == 10.25 and incar['NEDOS'] == 2101
    assert generated['derived']['counting_range']['lower_eV'] == -16.0
    assert generated['derived']['grid_spacing_eV'] == 0.01 and generated['derived']['sampling_note'] == '采样间隔，不是分辨率。'


def test_too_few_bands_is_refused_with_the_alternatives():
    generated = DOS['generate'](specs.validate(DOS, dos_values(), CONTEXT), evidence(band_minima_eV=[-14, -3, 2, 4.5]))
    (reason,) = generated['blocked']
    assert '空带不足' in reason and '增大 nbands' in reason and '缩小 window_max_eV' in reason and '不能照抄 bands' in reason
    # More bands than SCF had: unknown before the run, the post-run check decides.
    more = DOS['generate'](specs.validate(DOS, dos_values(nbands=8), CONTEXT), evidence(band_minima_eV=[-14, -3, 2, 4.5]))
    assert more['blocked'] == [] and more['derived']['nbands']['status'] == 'unknown_until_run' and more['incar']['NBANDS'] == 8
    fewer = DOS['generate'](specs.validate(DOS, dos_values(nbands=3), CONTEXT), evidence())
    assert any('第 3 条能带最小值 2.0' in r for r in fewer['blocked'])


def test_nedos_over_the_full_range_is_bounded():
    generated = DOS['generate'](specs.validate(DOS, dos_values(sampling_interval_eV=0.001), CONTEXT), evidence())
    assert any('NEDOS=21001 > 20001' in r and 'sampling_interval_eV' in r for r in generated['blocked'])


# --- New task with the optional DOS stage --------------------------------------------

def test_dos_planned_reviewed_run_and_exported(tmp_path, monkeypatch, vasp):
    root = prepare(tmp_path, monkeypatch)
    config = json.loads((root / 'config.json').read_text(encoding='utf-8'))
    assert [e['name'] for e in config['stages']] == ['convergence', 'relaxation', 'scf', 'bands', 'dos']
    assert config['stage_parameters']['dos']['spec'] == {'id': 'dos', 'version': 2}
    plan = json.loads((root / 'input-plan.json').read_text(encoding='utf-8'))
    assert plan['stage_parameters']['dos']['generate_at'] == 'stage_start' and 'dos' not in plan['incars']
    run_until(root, 'bands')
    assert gate.current(root) == 'dos'
    data = gate.context(root)
    assert data['required_checks'] == ('physics', 'scf', 'inputs', 'integration', 'window')
    assert set(data['check_details']) == set(data['required_checks'])
    plan = data['dos_plan']
    assert plan['blocked'] == [] and all(check['passed'] for check in plan['program_checks'].values())
    assert plan['incar']['ICHARG'] == 11 and plan['incar']['LORBIT'] == 11 and plan['incar']['ISMEAR'] == -5
    assert plan['incar']['EMIN'] == -14.0 and plan['incar']['EMAX'] == 5.0 and plan['incar']['NEDOS'] == 1901
    assert plan['kpoints']['mesh'] == [12, 12, 12] and plan['program_checks']['scf_source']['charge_sha256']
    assert data['execution_tags']['enabled']
    approve(root)
    vasp.launches.clear()
    full.run_full(root, ['mock'])
    assert vasp.launches == ['dos']  # SCF and bands reused, not rerun
    assert gate.current(root) == 'complete'
    assert json.loads((root / 'workflow-state.json').read_text(encoding='utf-8'))['status'] == 'complete'
    report = json.loads((root / 'dos-report.json').read_text(encoding='utf-8'))
    assert report['status'] == 'passed' and report['failed_checks'] == []
    count = report['checks']['electron_count']
    assert abs(count['integrated_dos_at_dos_fermi'] - NELECT) <= dos_stage.ELECTRON_COUNT_TOLERANCE
    assert count['lower_bound_eV'] == -14.0 and count['lowest_eigenvalue_eV'] == -12.0 and count['clearance_eV'] == 2.0
    assert '四面体' in count['occupation_method'] and '不再乘 2' in count['normalization']
    assert report['checks']['fermi_shift']['shift_eV'] == 0.02 and not report['checks']['fermi_shift']['flagged']
    assert report['checks']['projections']['units'] == 'states/eV/cell'
    assert '不要求二者相等' in report['checks']['projections']['normalization']
    assert report['sampling_note'] == '采样间隔，不是分辨率。'
    # dos.csv: energies relative to the SCF E_F, total and element/orbital projections, both E_F recorded.
    rows = list(csv.reader((root / 'dos.csv').open(encoding='utf-8')))
    assert rows[0] == ['energy_minus_scf_ef_eV', 'energy_eV', 'total_states_per_eV', 'integrated_states',
                       'Si_s_states_per_eV', 'Si_p_states_per_eV', 'scf_fermi_eV', 'dos_fermi_eV']
    assert len(rows) == 1902 and float(rows[1][0]) == -14.0 and float(rows[1][-1]) == 0.02
    for name in ('dos.png', 'dos.pdf', 'bands-dos.png', 'bands-dos.pdf'):
        assert (root / name).stat().st_size > 0
    assert report['combined_figure']['energy_reference'] == 'scf_fermi' and 'VBM' in report['combined_figure']['note']
    # report.json keeps what bands wrote and gains a dos field.
    summary = json.loads((root / 'report.json').read_text(encoding='utf-8'))
    assert summary['scf_fermi_energy_eV'] == 0.0 and summary['dos']['status'] == 'passed'
    assert summary['dos']['electron_count']['nelect'] == NELECT
    # The DOS INCAR carries the reviewed physics plus only the derived execution tags.
    physics, tags = full.split_execution(Incar.from_file(root / 'dos/INCAR'))
    assert physics == dict(Incar(plan['incar'])) and set(tags) == {'KPAR', 'NCORE'}
    assert json.loads((root / 'dos/execution.json').read_text(encoding='utf-8'))['tags'] == tags


def test_delivery_requires_the_dos_outputs(tmp_path, monkeypatch, vasp):
    root = prepare(tmp_path, monkeypatch)
    run_until(root, 'dos')
    config = json.loads((root / 'config.json').read_text(encoding='utf-8'))
    assert {'dos-report.json', 'dos.csv', 'dos.png', 'dos.pdf'} <= set(stages.delivery_files(config))
    assert {'dos-plan.json', 'bands-dos.png', 'dos-fork.json'} <= set(stages.result_files(config))
    assert {'dos.csv', 'dos-report.json'} <= stages.fetchable_files()
    folder = tmp_path / 'download'
    folder.mkdir()
    for name in (*stages.delivery_files(config), 'config.json'):
        (folder / name).write_bytes((root / name).read_bytes())
    assert delivery_summary(folder)['status'] == 'complete_package'
    report = json.loads((folder / 'report.json').read_text(encoding='utf-8'))
    core.save(folder / 'report.json', {**report, 'dos': {**report['dos'], 'status': 'failed'}})
    assert any('DOS 程序验收' in issue for issue in delivery_summary(folder)['issues'])
    (folder / 'dos.csv').unlink()
    assert delivery_summary(folder)['missing_files'] == ['dos.csv']


def test_tetrahedron_preconditions_failing_block_the_review_and_the_run(tmp_path, monkeypatch, vasp):
    root = prepare(tmp_path, monkeypatch, mesh=(1, 1, 1), kmesh_factor=1)  # SCF [3,3,3]: lower bound 1 irreducible k-point
    run_until(root, 'bands')
    data = gate.context(root)
    plan = data['dos_plan']
    assert not plan['program_checks']['integration']['passed'] and plan['incar']['ISMEAR'] == -5
    assert any('不可约 k 点下界 1 < 4' in r and 'gaussian' in r and '不静默改为 Gaussian' in r for r in plan['blocked'])
    assert 'request_dos_fork' in plan['next']
    with pytest.raises(ValueError, match='程序预检未通过（integration）'):
        gate.record(root, data['snapshot'], 'dos', checks(data), 'test-model')
    blocked = gate.record(root, data['snapshot'], 'dos', checks(data, 'blocked'), 'test-model')
    assert blocked['allowed'] is False
    with pytest.raises(ValueError, match='阻断'):
        full.run_full(root, ['mock'])
    assert not (root / 'dos').exists()
    # The operator's explicit alternative: a fork with Gaussian and a confirmed SIGMA.
    result = dos_stage.prepare_dos_fork(root, tmp_path / 'runs/si_gauss',
                                        dos_values(kmesh_factor=1, integration='gaussian', sigma_eV=0.05))
    assert result['incar']['ISMEAR'] == 0 and result['phase'] == 'dos'
    assert gate.context(tmp_path / 'runs/si_gauss')['dos_plan']['incar']['SIGMA'] == 0.05


def test_the_runner_refuses_blocked_inputs_even_with_an_allowed_review(tmp_path, monkeypatch, vasp):
    root = prepare(tmp_path, monkeypatch)
    run_until(root, 'bands')
    approve(root)
    vasp.scf_minima[-1] = 4.0  # the evidence reader now reports too few empty bands
    with pytest.raises(ValueError, match='阻断|证据已变化'):
        full.run_full(root, ['mock'])
    assert not (root / 'dos').exists()


def test_too_few_bands_after_the_run_fails_acceptance(tmp_path, monkeypatch, vasp):
    vasp.scf_minima[-1] = 4.0
    root = prepare(tmp_path, monkeypatch)
    run_until(root, 'bands')
    plan = gate.context(root)['dos_plan']
    assert not plan['program_checks']['window']['passed'] and any('空带不足' in r for r in plan['blocked'])
    # A fork with more bands than SCF had is allowed; the run decides.
    fork = tmp_path / 'runs/si_more'
    dos_stage.prepare_dos_fork(root, fork, dos_values(nbands=24))
    assert gate.context(fork)['dos_plan']['program_checks']['window']['nbands']['status'] == 'unknown_until_run'
    vasp.top = 5.5  # the run's top band dips below window + margin (6 eV)
    approve(fork)
    with pytest.raises(ValueError, match='empty_bands'):
        full.run_full(fork, ['mock'])
    report = json.loads((fork / 'dos-report.json').read_text(encoding='utf-8'))
    assert report['status'] == 'failed' and report['failed_checks'] == ['empty_bands']
    assert '需要更多 NBANDS' in report['checks']['empty_bands']['reason']
    assert gate.current(fork) == 'dos' and not (fork / 'dos.csv').exists()


@pytest.mark.parametrize('change, failed, message', [
    ({'scale': 0.9}, 'electron_count', '超过容差'),
    ({'scf_minima': [-5.0, -4.0] + list(np.linspace(1.0, 8.0, SCF_NBANDS - 2))}, 'electron_count', '含半芯态'),
    ({'nan': True}, 'outputs', '非有限值'),
    ({'no_pdos': True}, 'outputs', '投影 DOS'),
])
def test_electron_count_and_output_checks_fail_the_stage(tmp_path, monkeypatch, vasp, change, failed, message):
    for key, value in change.items():
        setattr(vasp, key, value)
    root = prepare(tmp_path, monkeypatch)
    run_until(root, 'bands')
    approve(root)
    with pytest.raises(ValueError, match='DOS 程序验收未通过'):
        full.run_full(root, ['mock'])
    report = json.loads((root / 'dos-report.json').read_text(encoding='utf-8'))
    assert failed in report['failed_checks'] and message in report['checks'][failed]['reason']
    assert gate.current(root) == 'dos' and not (root / 'dos.csv').exists()


def test_semicore_lower_bound_is_what_makes_the_count_pass(tmp_path, monkeypatch, vasp):
    # The same DOS run counted from below the semicore level passes; from above it, misses 2 electrons.
    root = prepare(tmp_path, monkeypatch)
    run_until(root, 'dos')
    count = json.loads((root / 'dos-report.json').read_text(encoding='utf-8'))['checks']['electron_count']
    assert count['passed'] and count['lower_bound_eV'] < count['lowest_eigenvalue_eV'] - dos_stage.MIN_COUNT_CLEARANCE_EV
    energies, integrated = (np.array([float(r[i]) for r in list(csv.reader((root / 'dos.csv').open(encoding='utf-8')))[1:]]) for i in (0, 3))
    above = np.interp(0.02, energies, integrated) - np.interp(-8.0, energies, integrated)
    assert abs(above - (NELECT - 2)) < 0.05


def test_dos_failure_then_phase_resume(tmp_path, monkeypatch, vasp):
    from test_phase_resume import JOB, SLURM, scheduler
    root = prepare(tmp_path, monkeypatch)
    run_until(root, 'bands')
    (root / 'submit.slurm').write_text(SLURM)
    (root / 'remote-job.json').write_text(json.dumps({'release': str(tmp_path / 'release'),
                                                      'script_sha256': worker.sha(root / 'submit.slurm')}))
    (root / 'submissions').mkdir(exist_ok=True)
    (root / 'submissions/dos.json').write_text(json.dumps({'status': 'submitted', 'job_id': JOB, 'phase': 'dos'}))
    approve(root)
    vasp.interrupt.add('dos')
    with pytest.raises(Walltime):
        full.run_full(root, ['mock'])
    vasp.interrupt.clear()
    assert (root / 'dos/OUTCAR').exists() and (root / 'dos-plan.json').exists()
    # Without a resume the interrupted output is never silently reused or overwritten.
    approve(root)
    with pytest.raises(ValueError, match='未验收输出'):
        full.run_full(root, ['mock'])
    scheduler(monkeypatch)
    preview = worker.prepare_phase_resume(root, 'dos')
    assert preview['rerun'] == ['dos'] and preview['reused'] == []
    done = worker.prepare_phase_resume(root, 'dos', JOB)
    archive = root / 'interrupted' / JOB
    assert done['prepared'] and (archive / 'dos/OUTCAR').exists() and (archive / 'root/dos-plan.json').exists()
    assert (root / 'dos/CHGCAR').exists() and (root / 'dos/INCAR').exists() and not (root / 'dos/OUTCAR').exists()
    approve(root)
    vasp.launches.clear()
    full.run_full(root, ['mock'])
    assert vasp.launches == ['dos'] and gate.current(root) == 'complete'
    assert json.loads((root / 'dos-report.json').read_text(encoding='utf-8'))['status'] == 'passed'


def test_duplicate_runs_are_refused(tmp_path, monkeypatch, vasp):
    root = prepare(tmp_path, monkeypatch)
    run_until(root, 'bands')
    approve(root)
    (root / '.agent.lock').write_text('')  # another run of this task holds the lock
    with pytest.raises(ValueError, match='计算仍在运行'):
        gate.record(root, gate.context(root)['snapshot'], 'dos', checks(gate.context(root)), 'test-model')
    with pytest.raises(FileExistsError):
        core.run(NS(workdir=str(root), command=['--', 'python']))
    (root / '.agent.lock').unlink()
    full.run_full(root, ['mock'])
    assert gate.submission_record(root, 'dos').name == 'dos.json'
    # Completed: no phase left to run, and the accepted DOS is never redone.
    with pytest.raises(ValueError, match='不是当前待执行阶段'):
        full.run_full(root, ['mock'])


def test_two_dimensional_slabs_refuse_dos_as_a_later_extension(tmp_path, monkeypatch):
    from test_slab_2d import mos2
    base = vars(args(tmp_path, 'slab'))
    source = tmp_path / 'mos2.POSCAR'
    Poscar(mos2()).write_file(source)
    slab = NS(**{**base, 'structure': str(source), 'relax_mode': 'ions', 'mesh': [6, 6, 1],
                 'optional_stages': ['dos'], 'stage_parameters': chosen()})
    with pytest.raises(ValueError, match='二维 DOS 尚未开放'):
        full.prepare_full(slab)
    assert not (tmp_path / 'runs/slab').exists()
    with pytest.raises(ValueError, match='二维 slab 的 DOS 尚未开放（后续扩展'):
        dos_stage.refuse_2d({'dimensionality': {'dimension': '2D'}})


# --- DOS-only fork --------------------------------------------------------------------

@pytest.fixture
def finished(tmp_path, monkeypatch, vasp):
    """A task without the DOS stage (bands last), complete: what La2CuO4's existing task looks like."""
    root = prepare(tmp_path, monkeypatch, dos=False)
    run_until(root, 'bands')
    assert gate.current(root) == 'complete'
    vasp.launches.clear()
    return root


def test_fork_runs_only_dos_and_leaves_the_source_untouched(finished, vasp):
    before = snapshot(finished)
    preview = dos_stage.fork_preview(finished)
    assert preview['spec']['version'] == 2 and preview['preview']['blocked'] == []
    assert preview['spec']['suggestions']['nbands']['value'] is None  # SCF already covers the window
    assert snapshot(finished) == before
    target = finished.parent / 'si_dos'
    result = dos_stage.prepare_dos_fork(finished, target, preview['defaults'])
    assert snapshot(finished) == before  # the source is never touched
    assert result['phase'] == 'dos' and not result['scf_recomputed'] and not result['submitted']
    assert core.digest(target / 'scf/CHGCAR') == json.loads((finished / 'scf/complete.json').read_text(encoding='utf-8'))['charge_sha256']
    config = json.loads((target / 'config.json').read_text(encoding='utf-8'))
    assert [e['name'] for e in config['stages']] == ['convergence', 'relaxation', 'scf', 'dos']
    assert config['execution_tags'] == full.EXECUTION_TAGS_VERSION and config['stage_parameters']['dos']['values'] == preview['defaults']
    receipt = json.loads((target / 'dos-fork.json').read_text(encoding='utf-8'))
    assert receipt['scf_reused'] and receipt['charge_sha256'] == result['charge_sha256'] and not receipt['submitted']
    assert not (target / 'bands').exists() and not (target / 'submissions').exists() and not list(target.glob('review-*.json'))
    with pytest.raises(ValueError, match='未审阅'):
        gate.require_review(target)
    approve(target)
    full.run_full(target, ['mock'])
    assert vasp.launches == ['dos']
    assert json.loads((target / 'workflow-state.json').read_text(encoding='utf-8'))['status'] == 'complete'
    assert (target / 'dos.csv').exists() and not (target / 'bands-dos.png').exists()  # no bands in the fork
    assert json.loads((target / 'report.json').read_text(encoding='utf-8'))['dos']['status'] == 'passed'
    # The same choice again names the same directory: refused, nothing changes.
    with pytest.raises(ValueError, match='不存在的新目录'):
        dos_stage.prepare_dos_fork(finished, target, preview['defaults'])


def test_tampered_scf_charge_refuses_the_fork(finished):
    (finished / 'scf/CHGCAR').write_text('tampered charge')
    target = finished.parent / 'fork'
    with pytest.raises(ValueError, match='电荷'):
        dos_stage.prepare_dos_fork(finished, target, dos_values())
    with pytest.raises(ValueError, match='电荷'):
        dos_stage.fork_preview(finished)
    assert not target.exists()


def test_fork_checks_the_executable_fingerprint(finished):
    (finished / 'environment-review.json').write_text(json.dumps({'executable_sha256': 'a' * 64}))
    target = finished.parent / 'fork'
    with pytest.raises(ValueError, match='可执行文件不同'):
        dos_stage.prepare_dos_fork(finished, target, dos_values(), environment={'executable_sha256': 'b' * 64})
    assert not target.exists()
    dos_stage.prepare_dos_fork(finished, target, dos_values(), environment={'executable_sha256': 'a' * 64})
    assert json.loads((target / 'dos-fork.json').read_text(encoding='utf-8'))['executable_sha256'] == 'a' * 64


def test_fork_refuses_blocked_values_and_a_source_without_a_passed_scf(tmp_path, monkeypatch, vasp, finished):
    vasp.scf_minima[0] = -30.0  # a deep semicore level: the counting range needs NEDOS > 20001 at 0.001 eV
    with pytest.raises(ValueError, match='DOS 输入被程序阻断，未创建新目录.*NEDOS=.*sampling_interval_eV'):
        dos_stage.prepare_dos_fork(finished, finished.parent / 'fork', dos_values(sampling_interval_eV=0.001))
    with pytest.raises(ValueError, match='未知参数'):
        dos_stage.prepare_dos_fork(finished, finished.parent / 'fork', {**dos_values(), 'command': 'rm -rf /'})
    assert not (finished.parent / 'fork').exists()
    early = prepare(tmp_path, monkeypatch, name='early', dos=False)
    run_until(early, 'relaxation')
    with pytest.raises(ValueError, match='SCF 尚未通过'):
        dos_stage.prepare_dos_fork(early, tmp_path / 'runs/early_dos', dos_values())


def test_bands_fork_still_uses_the_shared_scf_checks(finished):
    from vasp_agent.workflow import band_path
    (finished / 'scf/CHGCAR').write_text('tampered charge')
    with pytest.raises(ValueError, match='电荷'):
        band_path.prepare_bands_path(finished, finished.parent / 'path', 'Γ-R')


# --- Operator menu, tools and the remote worker ----------------------------------------

def menu_box(workspace, answers):
    from vasp_agent.chat import ai_agent as ai
    box = ai.Toolbox(workspace)
    shown = []
    box.plan_output = shown.append
    box.plan_reader = (lambda prompt: answers.pop(0)) if answers is not None else None
    return box, shown


def test_menu_needs_the_operator(finished):
    box, _ = menu_box(finished.parent.parent, None)
    result = box.request_dos_fork(None, 'si')
    assert result['status'] == 'operator_confirmation_required' and not result['prepared']
    assert sorted(p.name for p in finished.parent.iterdir()) == ['si']


def test_menu_reasks_after_a_blocked_choice_and_forks_on_the_operator_pick(finished, vasp):
    vasp.scf_minima[0] = -30.0
    box, shown = menu_box(finished.parent.parent, ['3', 'sampling_interval_eV=0.001', '2', '3', 'sampling_interval_eV=0.01 integration=gaussian sigma_eV=0.05', '2'])
    result = box.request_dos_fork(None, 'si')
    assert 'DOS 派生' in shown[0] and any('未创建任务' in line and 'NEDOS=' in line for line in shown)
    assert result['status'] == 'prepared' and result['selection']['operator_confirmed']
    assert result['selection']['values']['integration'] == 'gaussian' and result['job_name'].startswith('si_dos')
    fork = finished.parent / result['job_name']
    assert gate.current(fork) == 'dos' and gate.context(fork)['dos_plan']['incar']['ISMEAR'] == 0


def test_tool_is_advertised_and_phase_enum_has_dos():
    from vasp_agent.chat import ai_agent as ai
    tool = next(t for t in ai.MODEL_TOOLS if t['name'] == 'request_dos_fork')
    assert '模型不能代选' in tool['description'] and '不继承审阅和提交授权' in tool['description']
    assert set(tool['parameters']['properties']) == {'server', 'source_job'}
    assert 'dos' in ai.PHASES and 'dos' in stages.known_names()


def test_remote_calls_go_through_the_worker_actions(tmp_path, monkeypatch):
    from vasp_agent.remote.remote_compute import RemoteCompute
    box, _ = menu_box(tmp_path, [])
    calls = []
    monkeypatch.setattr(RemoteCompute, '__init__', lambda *a: None)
    monkeypatch.setattr(RemoteCompute, 'call', lambda self, action, job, **kw: calls.append((action, job, kw)) or {'prepared': True})
    monkeypatch.setattr(box, 'ensure_current_release', lambda server: None)
    box.prepare_dos_fork('hpc', 'La2CuO4_HTT_Cu_pv_780_ions_32c', 'lco_dos', dos_values())
    assert calls == [('prepare_dos_fork', 'lco_dos', {'source_job': 'La2CuO4_HTT_Cu_pv_780_ions_32c', 'values': dos_values()})]
    assert {'dos_fork_preview', 'prepare_dos_fork'} <= set(worker.NEW_TASK_ACTIONS)


def test_old_engine_refuses_dos(tmp_path, monkeypatch):
    old = NS(SCAN_PLAN_VERSION=1, PARAMETER_SPECS_VERSION=1, run_stages=True, BAND_PATH_VERSION=2,
             prepare_full=lambda a: pytest.fail('old engine must not prepare'))
    monkeypatch.setattr(worker, 'deployed_release', lambda root: tmp_path)
    monkeypatch.setattr(worker, 'activate_release', lambda release: tmp_path)
    monkeypatch.setattr(worker, 'engine_module', lambda name: old)
    monkeypatch.setattr(worker, 'inspect_environment', lambda cfg, mode: {'executable_sha256': 'x'})
    monkeypatch.setattr(worker, 'validate_scheduler', lambda cfg: None)
    cfg = {'vasp_env': __file__, 'potcar_library': str(tmp_path), 'ntasks': 8, 'partition': 'p', 'walltime': '01:00:00'}
    import base64
    payload = {'structure': base64.b64encode(str(Poscar(SI)).encode()).decode(),
               'parameters': {'encut': 520, 'mesh': [4, 4, 4], 'nbands': 16, 'relax_mode': 'cell',
                              'optional_stages': ['dos'], 'stage_parameters': chosen()}}
    with pytest.raises(ValueError, match='不支持 DOS 阶段'):
        worker._prepare_task(payload, cfg, tmp_path, 'new', tmp_path / 'jobs/new', False)
    with pytest.raises(ValueError, match='不支持 DOS 阶段与 DOS 派生'):
        worker._prepare_dos_fork({'source_job': 'src', 'values': dos_values()}, cfg, tmp_path, 'new', tmp_path / 'jobs/new')
    with pytest.raises(ValueError, match='不支持 DOS 阶段与 DOS 派生'):
        worker._dos_fork_preview({}, cfg, tmp_path, 'src', tmp_path / 'jobs/src')
    assert not (tmp_path / 'jobs').exists()


# --- Plots and local replot -------------------------------------------------------------

def test_combined_plot_reference_and_local_replot(tmp_path, monkeypatch, vasp):
    root = prepare(tmp_path, monkeypatch)
    run_until(root, 'dos')
    parsed = dos_plot.parse_dos((root / 'dos.csv').read_text(encoding='utf-8'))
    assert parsed['scf_fermi_eV'] == 0.0 and parsed['dos_fermi_eV'] == 0.02 and set(parsed['projections']) == {'Si_s', 'Si_p'}
    assert 'SCF 的 E_F' in dos_plot.COMBINED_NOTE
    download = tmp_path / 'download'
    download.mkdir()
    files = {}
    for name in ('dos.csv', 'bands.csv', 'dos-report.json', 'report.json'):
        data = (root / name).read_bytes()
        (download / name).write_bytes(data)
        files[name] = {'sha256': core.digest(download / name)}
    (download / 'download-receipt.json').write_text(json.dumps({'files': files}))
    result = dos_plot.replot_folder(download)
    assert result['receipt_verified'] and result['energy_reference'] == 'scf_fermi' and result['window_eV'] == [-10.0, 5.0]
    assert result['files'] == ['dos.replot.png', 'dos.replot.pdf', 'bands-dos.replot.png', 'bands-dos.replot.pdf']
    assert dos_plot.replot_folder(download, (-3, 3))['window_eV'] == [-3.0, 3.0]
    (download / 'dos.csv').write_text('tampered')
    from vasp_agent.workflow.band_plot import ReceiptMismatch
    with pytest.raises(ReceiptMismatch):
        dos_plot.replot_folder(download)


def test_replot_tool_redraws_a_dos_only_download(tmp_path, monkeypatch, vasp, finished):
    target = finished.parent / 'si_dos'
    dos_stage.prepare_dos_fork(finished, target, dos_values())
    approve(target)
    full.run_full(target, ['mock'])
    box, _ = menu_box(tmp_path, [])
    monkeypatch.setattr(type(box), 'job', lambda self, name: target)
    result = box.replot_bands(None, 'si_dos')
    assert set(result) == {'dos'} and result['dos']['files'][0] == 'dos.replot.png'


def test_system_prompt_and_catalog_stay_stable_through_a_dos_review(tmp_path, monkeypatch, vasp):
    from types import SimpleNamespace
    from vasp_agent.chat.ai_agent import Agent, Toolbox
    from test_stage_guides import FakeResponses, call, reply
    root = prepare(tmp_path, monkeypatch)
    run_until(root, 'bands')
    box = Toolbox(tmp_path)
    box.require_scan_plans = True
    responses = FakeResponses([
        reply(call('get_stage_review', {'server': None, 'job_name': 'si'}, 'a')),
        reply(call('read_stage_guide', {'guide': 'dos', 'page': 2}, 'b')),
        reply(call('request_dos_fork', {'server': None, 'source_job': 'si'}, 'c')),
        reply(text='完成')])
    agent = Agent(SimpleNamespace(responses=responses), 'test', box)
    assert agent.ask('审阅 DOS') == '完成'
    instructions = {r['instructions'] for r in responses.requests}
    catalogs = {json.dumps(r['tools'], sort_keys=True) for r in responses.requests}
    assert len(responses.requests) == 4 and len(instructions) == 1 and len(catalogs) == 1
    outputs = {item['call_id']: json.loads(item['output']) for item in agent.history
               if item.get('type') == 'function_call_output'}
    review = outputs['a']['result']
    assert review['phase'] == 'dos' and review['guides'][0]['id'] == 'dos' and review['guides'][0]['implemented']
    assert review['dos_plan']['program_checks']['window']['passed']
    assert outputs['b']['result']['guide_page']['page'] == 2
    # Non-interactive: the fork menu needs the operator and creates nothing.
    assert outputs['c']['result']['status'] == 'operator_confirmation_required'
    assert sorted(p.name for p in root.parent.iterdir()) == ['si']


def test_toolbox_call_record_stage_review_accepts_all_five_dos_checks(tmp_path, monkeypatch, vasp):
    from vasp_agent.chat.ai_agent import CHECKLIST, Toolbox
    root = prepare(tmp_path, monkeypatch)
    run_until(root, 'bands')
    box = Toolbox(tmp_path)
    box.require_scan_plans = True
    box.lazy_tool_groups = True
    review = box.call('get_stage_review', {'server': None, 'job_name': 'si'})
    assert review['ok'] is True and review['result']['phase'] == 'dos'
    required = review['result']['required_checks']
    assert len(required) == 5 and CHECKLIST['maxItems'] >= len(required)
    submitted = checks(review['result'])
    recorded = box.call('record_stage_review', {
        'server': None, 'job_name': 'si',
        'snapshot': review['result']['snapshot'], 'phase': 'dos', 'checks': submitted,
    })
    assert recorded['ok'] is True and recorded['result']['allowed'] is True and recorded['result']['phase'] == 'dos'
    # Bounds still apply above the dynamic ceiling.
    oversized = box.call('record_stage_review', {
        'server': None, 'job_name': 'si',
        'snapshot': review['result']['snapshot'], 'phase': 'dos',
        'checks': submitted + [submitted[0]] * (CHECKLIST['maxItems'] - len(submitted) + 1),
    })
    assert oversized['ok'] is False and 'is too long' in oversized['error']


def test_stage_parameters_schema_and_planner_accept_null_values(tmp_path, monkeypatch):
    from jsonschema import Draft202012Validator
    from vasp_agent.chat.ai_agent import STAGE_PARAMETERS, Toolbox
    from vasp_agent.planning import parameter_plans as plans
    payload = [
        {'stage': 'dos', 'field': 'nbands', 'value': None},
        {'stage': 'dos', 'field': 'sigma_eV', 'value': None},
    ]
    Draft202012Validator(STAGE_PARAMETERS).validate(payload)
    overrides = plans.stage_overrides({'optional_stages': ['dos']}, payload)
    assert overrides == {'dos': {'nbands': None, 'sigma_eV': None}}
    assert specs.merged(DOS, CONTEXT, overrides['dos'])['nbands'] is None
    assert specs.merged(DOS, CONTEXT, overrides['dos'])['sigma_eV'] is None
    # End-to-end through Toolbox.call('propose_scan_plan', ...).
    source = tmp_path / 'Si.POSCAR'
    Poscar(SI).write_file(source)
    box = Toolbox(tmp_path)
    box.require_scan_plans = True
    box.lazy_tool_groups = True
    monkeypatch.setattr(plans, 'preview_defaults', lambda box, req: {
        'preview_signature': 'sig-null-test',
        'base_config': {'dimensionality': {'dimension': '3D'}},
        'defaults': {'encuts': [520, 598, 676], 'meshes': [[4, 4, 4], [5, 5, 5], [6, 6, 6]], 'scan_preset': 'strict'},
        'stage_specs': {'dos': specs.describe(DOS, CONTEXT)},
    })
    proposed = box.call('propose_scan_plan', {
        'server': None, 'job_name': 'si-dos-null', 'structure': 'Si.POSCAR',
        'encut': 520, 'mesh': [4, 4, 4], 'nbands': 16, 'relax_mode': 'cell',
        'parallel_tests': None, 'cores_per_test': None,
        'potential_variants': None, 'encuts': None, 'meshes': None,
        'scan_thresholds': None, 'scan_preset': None, 'band_path': None,
        'optional_stages': ['dos'], 'stage_parameters': payload,
        'origin': 'ai_suggestion', 'rationale': '沿用 SCF NBANDS 且使用四面体法（sigma_eV 为空）。',
    })
    assert proposed['ok'] is True
    assert proposed['result']['stage_parameters']['dos']['suggestion']['nbands'] is None
    assert proposed['result']['stage_parameters']['dos']['suggestion']['sigma_eV'] is None


def test_local_get_status_and_read_log_expose_dos_reports(tmp_path, monkeypatch, vasp):
    from vasp_agent.chat.ai_agent import Toolbox
    root = prepare(tmp_path, monkeypatch)
    run_until(root, 'dos')
    box = Toolbox(tmp_path)
    status = box.call('get_status', {'job_name': 'si'})
    assert status['ok'] is True
    assert status['result']['report']['dos']['status'] == 'passed'
    assert status['result']['report']['dos']['report'] == 'dos-report.json'
    plan_log = box.call('read_log', {'job_name': 'si', 'file': 'dos-plan.json'})
    report_log = box.call('read_log', {'job_name': 'si', 'file': 'dos-report.json'})
    assert plan_log['ok'] is True and '"ISMEAR": -5' in plan_log['result']['tail']
    assert report_log['ok'] is True and '"status": "passed"' in report_log['result']['tail']
