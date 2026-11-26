"""Stage definitions: legacy tasks unchanged, completion and delivery follow the enabled stages."""
import base64
import hashlib
import json
import os
from pathlib import Path
from types import SimpleNamespace as NS

import numpy as np
import pytest

import vasp_agent.workflow.full_workflow as full
import vasp_agent.workflow.stage_gate as gate
import vasp_agent.workflow.vasp_band_agent as core
from vasp_agent.remote import remote_worker, worker_slurm
from vasp_agent.remote.remote_compute import delivery_summary
from vasp_agent.workflow import stage_definitions as stages

from legacy_stage_walk import build, checks_for, walk

GOLDEN = Path(__file__).with_name('data') / 'legacy_stage_walk.json'
# The review item names as they were before stage definitions; never renamed.
LEGACY_CHECKS = {'convergence': ('physics', 'inputs', 'validation_plan'),
                 'relaxation': ('physics', 'convergence'),
                 'scf': ('physics', 'geometry', 'post_relax_convergence'),
                 'bands': ('physics', 'scf', 'electronic')}


def rehash_config(root, config):
    core.save(root / 'config.json', config)
    manifest = json.loads((root / 'manifest.json').read_text())
    manifest['hashes']['config.json'] = core.digest(root / 'config.json')
    core.save(root / 'manifest.json', manifest)


# --- Legacy tasks -------------------------------------------------------------

def test_legacy_task_reviews_and_checkpoints_match_the_pre_change_engine(tmp_path, monkeypatch):
    if os.name != 'nt':
        # Golden legacy_stage_walk.json was recorded on Windows (CRLF); normalize text newlines
        # before build() so Linux CI checks every snapshot/checkpoint hash byte-for-byte
        # without altering frozen ENGINE_MODULES or tests/data/legacy_stage_walk.json.
        def crlf_digest(path):
            raw = Path(path).read_bytes().replace(b'\r\n', b'\n').replace(b'\n', b'\r\n')
            return hashlib.sha256(raw).hexdigest()
        monkeypatch.setattr(core, 'digest', crlf_digest)
    root = build(tmp_path)
    assert 'stages' not in json.loads((root / 'config.json').read_text())
    steps = walk(root, monkeypatch)
    golden = json.loads(GOLDEN.read_text(encoding='utf-8'))
    assert json.loads(json.dumps(steps, sort_keys=True)) == golden
    # Nothing was written into the old task's config along the way.
    assert 'stages' not in json.loads((root / 'config.json').read_text())
    # The fork source evidence (everything through SCF) is what the bands review bound.
    assert gate.evidence_through(root, 'scf')['hashes'] == golden[3]['evidence_hashes']


def test_required_checks_keep_their_names_and_gain_details(tmp_path):
    assert gate.CHECKS == LEGACY_CHECKS
    root = build(tmp_path)
    data = gate.context(root)
    assert tuple(data['required_checks']) == LEGACY_CHECKS['convergence']
    assert list(data['check_details']) == list(LEGACY_CHECKS['convergence'])
    assert all(item['description'] for item in data['check_details'].values())
    assert data['check_details']['validation_plan']['accepts'] == ['pass', 'pending']
    assert data['check_details']['inputs']['accepts'] == ['pass']
    assert data['rules_version'] == 1
    for phase, names in LEGACY_CHECKS.items():
        rules = stages.review_rules({}, phase)
        assert stages.definition({}, phase)['review']['required_checks'] == names
        assert set(rules['check_details']) == set(names)


def test_review_validation_stays_name_based(tmp_path):
    root = build(tmp_path)
    data = gate.context(root)
    checks = checks_for(data['required_checks'])
    with pytest.raises(ValueError, match='缺项'):
        gate.record(root, data['snapshot'], 'convergence', checks[:-1], 'test')
    renamed = [dict(checks[0], item='physics_model'), *checks[1:]]
    with pytest.raises(ValueError, match='缺项'):
        gate.record(root, data['snapshot'], 'convergence', renamed, 'test')
    blocked = [dict(checks[0], judgment='pending'), *checks[1:]]
    assert not gate.record(root, data['snapshot'], 'convergence', blocked, 'test')['allowed']


# --- New tasks record their stages --------------------------------------------

def test_new_task_records_enabled_stages_under_the_config_hash(tmp_path):
    root = build(tmp_path, legacy=False)
    config = json.loads((root / 'config.json').read_text())
    assert config['stages'] == [{'name': name, 'version': 1} for name in stages.LEGACY_STAGES]
    manifest = json.loads((root / 'manifest.json').read_text())
    assert manifest['hashes']['config.json'] == core.digest(root / 'config.json')
    data = gate.context(root)
    assert data['inputs']['config']['stages'] == config['stages']
    # Changing the record changes the reviewed snapshot (and fails verification).
    rehash_config(root, {**config, 'stages': config['stages'][:3] + [{'name': 'bands', 'version': 1}]})
    assert gate.context(root)['snapshot'] == data['snapshot']
    rehash_config(root, {**config, 'stages': config['stages'][:3]})
    assert gate.context(root)['snapshot'] != data['snapshot']


@pytest.mark.parametrize('record, message', [
    ([{'name': 'convergence', 'version': 9}], '定义版本'),
    ([{'name': 'scf', 'version': 1}], '前置'),
    ([{'name': n, 'version': 1} for n in ('convergence', 'relaxation', 'scf', 'scf')], '重复'),
    ([{'name': n, 'version': 1} for n in ('convergence', 'relaxation', 'bands', 'scf')], '前置'),
    ([{'name': 'Bands!', 'version': 1}], '格式'),
    ([], '无效'),
])
def test_stage_record_is_validated(record, message):
    with pytest.raises(ValueError, match=message):
        stages.validate(record)


def test_legacy_tables_are_the_definitions():
    legacy = {}
    assert set(stages.result_files(legacy)) == set(remote_worker.RESULT_FILES)
    assert {phase: stages.phase_outputs(legacy, phase) for phase in stages.LEGACY_STAGES} == remote_worker.PHASE_OUTPUTS
    assert stages.delivery_files(legacy) == ('workflow-state.json', 'checkpoint.json', 'report.json',
                                             'bands.csv', 'bands.png', 'bands.pdf')
    assert stages.summary_files(legacy) == ('convergence.json', 'relaxation.json', 'post-relax-validation.json',
                                            'geometry-summary.json', 'scf-summary.json')
    assert stages.following(legacy, 'bands') == 'complete' and stages.is_legacy(legacy)
    # Declared now, matched against environment profiles later; the legacy stages need nothing extra.
    assert all(stages.definition(legacy, phase)['required_capabilities'] == () for phase in stages.LEGACY_STAGES)


# --- A synthetic fifth stage ----------------------------------------------------

@pytest.fixture
def fifth_stage(monkeypatch):
    """A test-only SCF-dependent stage after bands, with its own runner and outputs."""
    definition = stages._definition(
        'extra', depends_on=('scf',),
        inputs={'rule': 'test_only', 'enforced_by': 'test runner', 'note': 'test'},
        checks={'physics': stages._PHYSICS, 'extra_ready': {'description': '测试项', 'accepts': ('pass',)}},
        execution={'mode': 'allocation', 'runner': 'extra'},
        phase_outputs=('extra.json',), stage_results=('extra.json',), delivery=('extra.json',),
        completion={'rule': 'test', 'enforced_by': 'test runner'})
    monkeypatch.setitem(stages.DEFINITIONS, 'extra', {1: definition})
    runs = []

    def runner(root, config, products):
        runs.append(sorted(products))
        core.save(root / 'extra.json', {'ran_on_scf_mesh': products['scf_mesh']})
    monkeypatch.setitem(full.STAGE_RUNNERS, 'extra', runner)
    return runs


def five_stage_task(tmp_path):
    root = build(tmp_path, legacy=False)
    config = json.loads((root / 'config.json').read_text())
    rehash_config(root, {**config, 'stages': stages.record((*stages.LEGACY_STAGES, 'extra'))})
    return root


def test_completion_follows_enabled_stages_not_bands_last(tmp_path, monkeypatch, fifth_stage):
    root = five_stage_task(tmp_path)
    steps = walk(root, monkeypatch)
    assert [s['phase'] for s in steps] == ['convergence', 'relaxation', 'scf', 'bands', 'extra']
    after_bands = steps[3]
    assert after_bands['checkpoint']['next'] == 'extra'
    assert after_bands['checkpoint']['completed'] == ['convergence', 'relaxation', 'scf', 'bands']
    # Bands finished, but the task is not complete: the next stage awaits its review.
    assert after_bands['workflow_state']['status'] == 'awaiting_review'
    assert steps[4]['required_checks'] == ['physics', 'extra_ready']
    # The later sibling's review binds the bands stage it ran after.
    assert 'bands/vasprun.xml' in steps[4]['evidence_hashes']
    assert steps[4]['checkpoint'] == {'completed': [*stages.LEGACY_STAGES, 'extra'], 'next': 'complete'}
    assert steps[4]['workflow_state']['status'] == 'complete'
    assert fifth_stage == [['command', 'encut', 'primitive', 'relax_mesh', 'scf_mesh', 'structure']]
    assert gate.current(root) == 'complete'
    with pytest.raises(ValueError, match='流程已完成'):
        gate.context(root)


def test_stage_without_a_runner_is_refused_not_skipped(tmp_path, monkeypatch, fifth_stage):
    root = five_stage_task(tmp_path)
    monkeypatch.delitem(full.STAGE_RUNNERS, 'extra')
    with pytest.raises(ValueError, match='没有阶段 extra 的执行实现'):
        walk(root, monkeypatch)
    assert gate.current(root) == 'extra'


def test_unreviewed_task_cannot_enable_extra_stages(tmp_path, monkeypatch, fifth_stage):
    root = five_stage_task(tmp_path)
    config = json.loads((root / 'config.json').read_text())
    rehash_config(root, {**config, 'stage_gates': False})
    with pytest.raises(ValueError, match='逐阶段审阅'):
        full.run_full(root, ['mock'])


def bound_release(root, tmp_path):
    (root / 'remote-job.json').write_text(json.dumps({'release': str(tmp_path / 'release'), 'script_sha256': ''}))


def fake_band_results(root):
    core.save(root / 'report.json', {'band_validation': {'passed': True}, 'sampled_path_gap': {'energy': 1.1}})
    for name in ('bands.csv', 'bands.png', 'bands.pdf'):
        (root / name).write_text('x')


def download(root, tmp_path, name='dl'):
    """What fetch would deliver, via the worker's own allowlist and the local check."""
    files = remote_worker._fetch({}, {}, root)['files']
    target = tmp_path / name
    target.mkdir()
    for filename, item in files.items():
        assert filename in remote_worker.RESULT_FILES or filename in stages.fetchable_files()
        (target / filename).write_bytes(base64.b64decode(item['data']))
    return target, files


def test_delivery_follows_enabled_stages(tmp_path, monkeypatch, fifth_stage):
    root = five_stage_task(tmp_path)
    bound_release(root, tmp_path)
    walk(root, monkeypatch)
    fake_band_results(root)
    target, files = download(root, tmp_path)
    assert {'extra.json', 'config.json', 'review-extra.json', 'REVIEW-extra.md'} <= set(files)
    assert delivery_summary(target)['status'] == 'complete_package'
    (target / 'extra.json').unlink()
    summary = delivery_summary(target)
    assert summary['status'] == 'partial_or_unverified' and summary['missing_files'] == ['extra.json']
    # A four-stage completion record is not a complete five-stage task.
    core.save(target / 'extra.json', {})
    core.save(target / 'checkpoint.json', {'completed': list(stages.LEGACY_STAGES), 'next': 'complete'})
    assert any('5 个启用阶段' in issue for issue in delivery_summary(target)['issues'])


def test_legacy_delivery_is_unchanged(tmp_path, monkeypatch):
    root = build(tmp_path)
    walk(root, monkeypatch)
    fake_band_results(root)
    target, files = download(root, tmp_path)
    assert set(files) <= set(remote_worker.RESULT_FILES) and 'config.json' not in files
    assert delivery_summary(target) == {
        'status': 'complete_package', 'missing_files': [], 'issues': [], 'sampled_path_gap': {'energy': 1.1},
        'note': '这是交付文件一致性检查；不替代原始输出验收，也不等同全布里渊区带隙证明。'}
    core.save(target / 'checkpoint.json', {'completed': ['convergence', 'relaxation', 'scf'], 'next': 'complete'})
    assert '四阶段完成记录缺失或不一致' in delivery_summary(target)['issues']


def scheduler(monkeypatch, sacct):
    def run(command, **kwargs):
        stdout = '' if command[0] == 'squeue' else sacct
        return NS(returncode=0, stdout=stdout, stderr='')
    monkeypatch.setattr(worker_slurm.subprocess, 'run', run)
    monkeypatch.setattr(remote_worker.subprocess, 'run', run)


def test_monitor_and_previous_job_follow_enabled_stages(tmp_path, monkeypatch, fifth_stage):
    root = five_stage_task(tmp_path)
    walk(root, monkeypatch)
    core.save(root / 'report.json', {'band_validation': {'passed': True}})
    (root / 'submissions').mkdir()
    for phase, job in (('bands', '101'), ('extra', '102')):
        core.save(root / 'submissions' / (phase + '.json'), {'status': 'submitted', 'job_id': job, 'phase': phase})
    scheduler(monkeypatch, '101|COMPLETED|0:0\n102|COMPLETED|0:0\n')
    status = worker_slurm.monitor_status(root, '102', 'extra')
    assert status['outcome'] == 'completed' and status['workflow_status'] == 'complete'
    # The previous job of 'extra' is the bands receipt, found from the task's order.
    queried = []
    monkeypatch.setattr(remote_worker, 'scheduler_query',
                        lambda command, consequence: queried.append(command) or ('' if command[0] == 'squeue' else '101|COMPLETED|0:0'))
    remote_worker.check_previous_job(root, 'extra')
    assert ['sacct', '-n', '-j', '101', '--format=JobIDRaw,State,ExitCode', '-P'] in queried
    # The same files under a four-stage record do not read as a verified completion.
    config = json.loads((root / 'config.json').read_text())
    core.save(root / 'config.json', {**config, 'stages': config['stages'][:4]})
    with pytest.raises(ValueError, match='监控'):
        worker_slurm.monitor_status(root, '102', 'extra')
    status = worker_slurm.monitor_status(root, '101', 'bands')
    assert status['workflow_status'] == 'completion_unverified'


def test_resume_and_release_use_the_definition_outputs(tmp_path, monkeypatch, fifth_stage):
    root = five_stage_task(tmp_path)
    assert remote_worker.phase_outputs(root, 'extra') == ('extra.json',)
    assert remote_worker.kept_stage_inputs(root, 'bands') == ('CHGCAR',)
    assert remote_worker.kept_stage_inputs(root, 'extra') == ()
    core.save(root / 'extra.json', {})
    assert 'extra.json' in remote_worker.started_outputs(root, 'extra', gate)


# --- Tasks bound to releases without the stage module ----------------------------

def test_legacy_task_never_needs_the_stage_module_in_the_worker(tmp_path, monkeypatch):
    root = build(tmp_path)
    walk(root, monkeypatch)
    real = remote_worker.engine_module

    def old_release(name):
        if name == 'stage_definitions':
            raise ImportError('release predates stage definitions')
        return real(name)
    monkeypatch.setattr(remote_worker, 'engine_module', old_release)
    assert remote_worker.phase_outputs(root, 'bands') == remote_worker.PHASE_OUTPUTS['bands']
    assert remote_worker.kept_stage_inputs(root, 'bands') == ('CHGCAR',)
    assert remote_worker.result_files(root) == remote_worker.RESULT_FILES
    fake_band_results(root)
    target, _ = download(root, tmp_path)
    assert delivery_summary(target)['status'] == 'complete_package'
    status = remote_worker._status({}, {}, root)
    assert set(status['stage_reviews']) == set(stages.LEGACY_STAGES) and 'stage_record_error' not in status
    assert remote_worker.task_phases(root) == stages.LEGACY_STAGES
    # A task that records stages on an engine without the module is refused, not guessed.
    config = json.loads((root / 'config.json').read_text())
    core.save(root / 'config.json', {**config, 'stages': stages.record()})
    with pytest.raises(ValueError, match='不按旧流程猜测'):
        remote_worker.phase_outputs(root, 'bands')


def test_bundled_worker_fetches_a_legacy_task_without_any_engine(tmp_path, monkeypatch):
    from test_worker_bundle import run_bundled, run_package
    remote = tmp_path / 'remote-root'
    (tmp_path / 'build').mkdir()
    root = build(tmp_path / 'build')
    walk(root, monkeypatch)
    (remote / 'jobs').mkdir(parents=True)
    root.rename(remote / 'jobs' / 'si')
    payload = {'config': {'root': str(remote)}, 'action': 'fetch', 'job_name': 'si'}
    code, bundled = run_bundled(payload, tmp_path)
    assert code == 0 and (code, bundled) == run_package(payload)
    assert set(bundled['result']['files']) <= set(remote_worker.RESULT_FILES)


# --- Analysis keeps bands' report but completes only the last stage -----------------

@pytest.mark.parametrize('names, final', [(stages.LEGACY_STAGES, True), ((*stages.LEGACY_STAGES, 'extra'), False)])
def test_analyze_full_marks_complete_only_when_bands_is_last(tmp_path, monkeypatch, fifth_stage, names, final):
    root = tmp_path
    core.save(root / 'config.json', {'stages': stages.record(names)})
    core.save(root / 'workflow-state.json', {'status': 'calculated'})
    for name in ('convergence.json', 'relaxation.json', 'post-relax-validation.json'):
        core.save(root / name, {})
    for name in ('scf', 'bands'):
        (root / name).mkdir()
        (root / name / 'vasprun.xml').write_text(name)
        core.save(root / name / 'inputs.json', {})
        core.save(root / name / 'complete.json', {'xml_sha256': core.digest(root / name / 'vasprun.xml')})
    bands = NS(vasp_version='test', get_band_structure=lambda **kw: NS(bands={'up': np.array([[1.0]])}, efermi=0.0))
    monkeypatch.setattr(core, 'verify', lambda root: {'formula': 'Si'})
    monkeypatch.setattr(core, 'read_result', lambda folder: bands if folder.name == 'bands' else NS(efermi=0.0))
    monkeypatch.setattr(core, 'validate_band_data', lambda result: {'passed': True})
    monkeypatch.setattr(core, 'export_bands', lambda bs, root, metadata: {'limitations': ['x']})
    full.analyze_full(root)
    assert (root / 'report.json').is_file()
    assert json.loads((root / 'workflow-state.json').read_text())['status'] == ('complete' if final else 'calculated')
    events = [json.loads(line)['action'] for line in (root / 'events.jsonl').read_text(encoding='utf-8').splitlines()]
    assert events == (['full_workflow_completed'] if final else ['bands_analyzed'])
