import json
from types import SimpleNamespace as NS
from pathlib import Path
import pytest
from pymatgen.io.vasp.inputs import Kpoints, Incar, Poscar
from vasp_agent.workflow import convergence_reuse as reuse, full_workflow as full, stage_gate as gate, vasp_band_agent as core
from test_stage_gate import staged, approve
from test_convergence_reuse import completed_source
from test_full_workflow import result


@pytest.fixture
def completed_fixed(completed_source, monkeypatch):
    source, _ = completed_source
    root = source.parent / 'mesh598'
    reuse.prepare_retry(source, root, 'mesh', meshes=[[10]*3, [12]*3, [14]*3], fixed_encut=598)
    launches = []
    def execute(root, name, command):
        folder = root / name
        if not (folder / 'complete.json').exists():
            launches.append(name)
            (folder / 'vasprun.xml').write_text('-10.')
            core.save(folder / 'complete.json', {'xml_sha256': core.digest(folder / 'vasprun.xml')})
        return result(Poscar.from_file(folder / 'POSCAR').structure)
    monkeypatch.setattr(core, 'execute', execute)
    gate.confirm_physics(root, 'silicon PBE')
    approve(root)
    full.run_full(root, ['mock'])
    launches.clear()
    return root, launches


def test_continuation_uses12_without_repeating_scan(completed_fixed):
    source, launches = completed_fixed
    before = {str(p.relative_to(source)): p.read_bytes() for p in source.rglob('*') if p.is_file()}
    target = source.parent / 'production12'
    answer = reuse.prepare_production(source, target, [12]*3, gate.context(source)['snapshot'])
    assert answer['encut'] == 598 and answer['relaxation_mesh'] == [12]*3
    assert not answer['submitted'] and not answer['scan_recomputed']
    assert gate.current(target) == 'relaxation'
    cfg = json.loads((target / 'config.json').read_text())
    assert cfg['meshes'] == [[10]*3, [12]*3, [14]*3]
    assert cfg['production_mesh'] == [12]*3
    assert gate.context(target)['inputs']['production_sampling']['relaxation_mesh'] == [12]*3
    assert not (target / 'review-relaxation.json').exists()
    assert not (target / 'submissions').exists()
    with pytest.raises(ValueError, match='未审阅'):
        full.run_full(target, ['mock'])
    approve(target)
    full.run_full(target, ['mock'])
    assert launches == ['relax-1', 'relax-2', 'validation/reference', 'validation/higher-cutoff', 'validation/denser-mesh']
    assert gate.current(target) == 'scf'
    for stage in ('relax-1', 'relax-2', 'validation/reference'):
        assert Kpoints.from_file(target / stage / 'KPOINTS').kpts[0] == (12,12,12)
        assert Incar.from_file(target / stage / 'INCAR')['ENCUT'] == 598
    assert Kpoints.from_file(target / 'validation/denser-mesh/KPOINTS').kpts[0] == (15,15,15)
    assert Kpoints.from_file(target / 'convergence/cross-cutoff/KPOINTS').kpts[0] == (14,14,14)
    assert before == {str(p.relative_to(source)): p.read_bytes() for p in source.rglob('*') if p.is_file()}


@pytest.mark.parametrize('mesh', [[11]*3, [True]*3, [12,12], [129]*3])
def test_unverified_mesh_rejected_without_target(completed_fixed, mesh):
    source, _ = completed_fixed
    target = source.parent / 'bad'
    with pytest.raises(ValueError, match='已测试'):
        reuse.prepare_production(source, target, mesh, gate.context(source)['snapshot'])
    assert not target.exists()


def test_stale_snapshot(completed_fixed):
    source, _ = completed_fixed
    with pytest.raises(ValueError, match='快照'):
        reuse.prepare_production(source, source.parent / 'bad', [12]*3, 'old')
    assert not (source.parent / 'bad').exists()


@pytest.mark.parametrize('block', ['.agent.lock', 'relax-1', 'submissions/relaxation.json'])
def test_started_or_submitted_source_rejected(completed_fixed, block):
    source, _ = completed_fixed
    snap = gate.context(source)['snapshot']
    path = source / block
    path.parent.mkdir(exist_ok=True)
    path.write_text('{}')
    with pytest.raises(ValueError):
        reuse.prepare_production(source, source.parent / 'bad', [12]*3, snap)
    assert not (source.parent / 'bad').exists()


def test_cross_validation_reparsed_not_trusted_summary(completed_fixed, monkeypatch):
    source, _ = completed_fixed
    original = core.read_result
    monkeypatch.setattr(core, 'read_result', lambda path: result(energy=-9.) if path.name == 'cross-cutoff' else original(path))
    with pytest.raises(ValueError, match='交叉复核未通过'):
        reuse.prepare_production(source, source.parent / 'bad', [12]*3, gate.context(source)['snapshot'])
    assert not (source.parent / 'bad').exists()


def test_output_tampering_rejected(completed_fixed):
    source, _ = completed_fixed
    snap = gate.context(source)['snapshot']
    (source / 'convergence/mesh-1/vasprun.xml').write_text('changed')
    with pytest.raises(ValueError, match='哈希'):
        reuse.prepare_production(source, source.parent / 'bad', [12]*3, snap)


def test_old_review_not_valid_for_new_mesh(completed_fixed):
    source, _ = completed_fixed
    approve(source)
    target = source.parent / 'different'
    reuse.prepare_production(source, target, [12]*3, gate.context(source)['snapshot'])
    (target / 'review-relaxation.json').write_bytes((source / 'review-relaxation.json').read_bytes())
    with pytest.raises(ValueError, match='已变化'):
        gate.require_review(target)


@pytest.mark.parametrize('old_release', [False, True])
def test_remote_preparation_uses_latest_release_and_no_submission(completed_fixed, monkeypatch, old_release):
    import shutil
    from vasp_agent.remote import remote_worker as worker
    source, _ = completed_fixed
    remote = source.parent / 'remote'
    jobs = remote / 'jobs'
    jobs.mkdir(parents=True)
    shutil.copytree(source, jobs / 'source')
    core.save(remote / 'deployment.json', {'release': str(Path(core.__file__).parent)})
    env = remote / 'env.sh'
    env.write_text('# fixture')
    cfg = dict(root=str(remote), python='python', vasp_env=str(env), partition='compute', walltime='04:00:00')
    environment = {'executable_sha256': 'fixture'}
    core.save(jobs / 'source/environment-review.json', environment)
    monkeypatch.setattr(worker, 'inspect_environment', lambda *a: environment)
    calls = []
    def run(command, **kwargs):
        calls.append(command)
        assert command[:2] == ['bash', '-n']
        return NS(returncode=0)
    monkeypatch.setattr(worker.subprocess, 'run', run)
    checked = []
    monkeypatch.setattr(worker, 'check_previous_job', lambda folder, phase: checked.append(phase))
    payload = dict(config=cfg, action='prepare_production_from_convergence', job_name='new',
        source_job='source', mesh=[12]*3, source_snapshot=gate.context(jobs / 'source')['snapshot'])
    if old_release:
        # One below the current marker: a release that supported mesh selection but
        # not the plan-recorded KPOINTS would build a self-contradicting plan.
        monkeypatch.setattr(reuse, 'PRODUCTION_MESH_VERSION', reuse.PRODUCTION_MESH_VERSION - 1)
        with pytest.raises(ValueError, match='部署新版'):
            worker.dispatch(payload)
        assert not (jobs / 'new').exists()
        assert not calls
        return
    answer = worker.dispatch(payload)
    assert answer['phase'] == 'relaxation' and not answer['submitted']
    assert checked == ['relaxation'] and len(calls) == 1
    # The source's lock is released, not deleted -- see submission_lock. Asserting
    # absence would now assert the bug.
    assert not worker.lock_is_held(jobs / 'source/.submission-recovery.lock')
    assert not (jobs / 'new/submissions').exists()
    meta = json.loads((jobs / 'new/remote-job.json').read_text())
    assert meta['script_sha256'] == core.digest(jobs / 'new/submit.slurm')


def test_tool_requires_fresh_context(tmp_path):
    from vasp_agent.chat.ai_agent import Toolbox
    box = Toolbox(tmp_path)
    with pytest.raises(ValueError, match='最新快照'):
        box.prepare_production_from_convergence(None, 'source', 'target', [12]*3, 'unread')
    assert not box.allow_submit


def test_existing_target_never_overwritten(completed_fixed):
    source, _ = completed_fixed
    target = source.parent / 'existing'
    target.mkdir()
    marker = target / 'keep'
    marker.write_text('user data')
    with pytest.raises(ValueError, match='新目录'):
        reuse.prepare_production(source, target, [12]*3, gate.context(source)['snapshot'])
    assert marker.read_text() == 'user data'


def test_interrupted_copy_cannot_pass_verification(completed_fixed, monkeypatch):
    source, _ = completed_fixed
    target = source.parent / 'incomplete'
    snapshot = gate.context(source)['snapshot']
    original = reuse.shutil.copyfile
    def fail(src, dest):
        if str(src).endswith('vasprun.xml'):
            raise OSError('simulated disk failure')
        return original(src, dest)
    monkeypatch.setattr(reuse.shutil, 'copyfile', fail)
    with pytest.raises(OSError, match='simulated'):
        reuse.prepare_production(source, target, [12]*3, snapshot)
    assert not (target / 'manifest.json').exists()
    with pytest.raises(FileNotFoundError):
        core.verify(target)
    assert gate.context(source)['snapshot'] == snapshot
