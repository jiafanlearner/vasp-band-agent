"""Synthetic VASP and Slurm only; no remote scheduler or licensed potential."""
import json
import threading
import time
from types import SimpleNamespace as NS

import pytest
from pymatgen.io.vasp.inputs import Incar, Poscar

import vasp_agent.workflow.convergence_array as array
import vasp_agent.workflow.convergence_reuse as reuse
import vasp_agent.workflow.full_workflow as full
import vasp_agent.workflow.stage_gate as gate
import vasp_agent.workflow.vasp_band_agent as core
from test_full_workflow import result
from test_stage_gate import staged, approve


def ready(root, monkeypatch):
    config = json.loads((root / 'config.json').read_text(encoding='utf-8'))
    config.update(submission_mode='array', nodes=2, cores_per_test=8, parallel_tests=3)
    core.save(root / 'config.json', config)
    manifest = json.loads((root / 'manifest.json').read_text(encoding='utf-8'))
    manifest['hashes']['config.json'] = core.digest(root / 'config.json')
    core.save(root / 'manifest.json', manifest)
    executable = root.parent / 'mock-vasp'
    executable.write_text('synthetic executable')
    script = root.parent / 'mock-env.sh'
    script.write_text('synthetic environment')
    core.save(root / 'environment-review.json', {
        'executable_sha256': core.digest(executable), 'executable': str(executable),
        'environment_script': str(script), 'environment_sha256': core.digest(script)})
    monkeypatch.setattr(array.shutil, 'which', lambda name: str(executable) if name == 'vasp_std' else '/mock/srun')
    gate.confirm_physics(root, 'synthetic silicon PBE')
    approve(root)
    return root


@pytest.fixture
def simulation(staged, monkeypatch):
    state = NS(launches=[], changes={}, crashes=set())
    monkeypatch.setattr(full.Potcar, 'from_file', lambda p: [NS(element='Si', functional='PBE', enmax=245., ZVAL=4, TITEL='synthetic')])
    root = ready(staged, monkeypatch)
    for key, value in {'SLURM_ARRAY_JOB_ID': '4500', 'SLURM_ARRAY_TASK_ID': '0',
                       'SLURM_JOB_ID': '4501', 'SLURM_NTASKS': '8', 'SLURM_CPUS_PER_TASK': '1',
                       'SLURM_JOB_NUM_NODES': '2', 'SLURM_JOB_CPUS_PER_NODE': '4(x2)'}.items():
        monkeypatch.setenv(key, value)
    monkeypatch.delenv('VASP_AGENT_PHASE', raising=False)
    monkeypatch.delenv('SLURM_ARRAY_TASK_COUNT', raising=False)

    def launch(command, *, cwd, stdout, stderr, check, shell):
        name = cwd.relative_to(root).as_posix() if cwd.is_relative_to(root) else 'convergence/' + cwd.name
        state.launches.append((name, command))
        if name in state.crashes:
            raise core.subprocess.CalledProcessError(1, command)
        (cwd / 'vasprun.xml').write_text(json.dumps({'energy': -10., **state.changes.get(name, {})}))

    def read_xml(path, **kwargs):
        data = json.loads(path.read_text(encoding='utf-8'))
        structure = Poscar.from_file(path.parent / 'POSCAR').structure
        value = result(structure, energy=data.get('energy', -10.), force=data.get('force', 0.), stress=data.get('stress', 0.))
        value.initial_structure = structure
        value.incar = Incar.from_file(path.parent / 'INCAR')
        value.efermi = data.get('efermi', 0.)
        value.converged_electronic = data.get('electronic', True)
        return value

    monkeypatch.setattr(core.subprocess, 'run', launch)
    monkeypatch.setattr(core, 'Vasprun', read_xml)
    state.root = root
    return state


def run_id(simulation, monkeypatch, index, root=None):
    monkeypatch.setenv('SLURM_ARRAY_TASK_ID', str(index))
    return array.run_array_test(root or simulation.root, ['srun', 'vasp_std'])


def run_all(simulation, monkeypatch, root=None):
    count = len(array.tests_from_config(json.loads(((root or simulation.root) / 'config.json').read_text(encoding='utf-8'))))
    for index in range(count):
        report = run_id(simulation, monkeypatch, index, root)
    return report


def scheduler_evidence(parent='4500', count=6, changes=None):
    tasks = [{'task_id': index, 'job_id': f'{parent}_{index}', 'state': 'COMPLETED',
              'exit_code': '0:0', 'terminal': True, 'successful': True,
              **(changes or {}).get(index, {})} for index in range(count)]
    return {'job_id': parent, 'task_count': count, 'tasks': tasks, 'active': False, 'known': True,
            'all_terminal': all(row['terminal'] for row in tasks),
            'all_successful': all(row['successful'] for row in tasks)}


def test_all_six_independent_allocations_preserve_highest_duplicate_and_gate(simulation, monkeypatch):
    root = simulation.root
    initial_state = (root / 'workflow-state.json').read_bytes()
    tests = array.tests_from_config(json.loads((root / 'config.json').read_text(encoding='utf-8')))
    assert len(tests) == 6
    assert tests[2]['encut'] == tests[5]['encut'] and tests[2]['mesh'] == tests[5]['mesh']
    # An unrelated root lock must never serialize or block the array elements.
    (root / '.agent.lock').write_text('unrelated ordinary-run lock')
    for index in [3, 0, 5, 1, 4]:
        report = run_id(simulation, monkeypatch, index)
        assert report['status'] == 'running'
        assert (root / 'workflow-state.json').read_bytes() == initial_state
        assert not (root / 'convergence.json').exists()
    report = run_id(simulation, monkeypatch, 2)
    assert report['status'] == 'awaiting_review' and report['next'] == 'relaxation'
    assert gate.current(root) == 'relaxation'
    assert len(simulation.launches) == 6
    assert not (root / 'relax-1').exists()
    assert (root / '.agent.lock').exists()
    for test in tests:
        folder = root / test['stage']
        receipt = json.loads((folder / array.RECEIPT_NAME).read_text(encoding='utf-8'))
        assert receipt['parent_job_id'] == '4500' and receipt['task_id'] == test['task_id']
        assert receipt['status'] == 'complete'
        assert (folder / 'events.jsonl').exists() and not (folder / '.array-test.lock').exists()
    assert all('--ntasks=8' in command and '--nodes=2' in command and '--ntasks-per-node=4' in command
               for _, command in simulation.launches)
    assert array.collect_array(root, '4500')['status'] == 'awaiting_review'
    before = len(simulation.launches)
    assert run_id(simulation, monkeypatch, 2)['status'] == 'awaiting_review'
    assert len(simulation.launches) == before


@pytest.mark.parametrize('kind', ['energy', 'force', 'stress', 'electronic'])
def test_failed_physics_or_electronic_output_never_releases(simulation, monkeypatch, kind):
    simulation.changes['convergence/mesh-1'] = {kind: False if kind == 'electronic' else 2.}
    for index in range(6):
        if index == 4 and kind == 'electronic' or index == 5:
            with pytest.raises(ValueError):
                run_id(simulation, monkeypatch, index)
        else:
            run_id(simulation, monkeypatch, index)
    root = simulation.root
    assert gate.current(root) == 'convergence'
    assert json.loads((root / 'workflow-state.json').read_text(encoding='utf-8'))['status'] == 'failed'
    assert json.loads((root / array.SUMMARY_NAME).read_text(encoding='utf-8'))['status'] == 'failed'
    assert not (root / 'relax-1').exists()
    convergence = json.loads((root / 'convergence.json').read_text(encoding='utf-8'))
    assert convergence['encut']['status'] == 'passed'
    assert convergence['mesh']['status'] == ('unavailable' if kind == 'electronic' else 'failed')


def test_failed_and_missing_receipts_only_finalize_missing_when_scheduler_terminal(simulation, monkeypatch):
    simulation.crashes.add('convergence/encut-1')
    run_id(simulation, monkeypatch, 0)
    with pytest.raises(core.subprocess.CalledProcessError):
        run_id(simulation, monkeypatch, 1)
    root = simulation.root
    report = array.finalize_array(root, '4500')
    assert report['missing_tasks'] == [2, 3, 4, 5]
    assert report['failed_tasks'] == [1]
    assert not (root / array.SUMMARY_NAME).exists()
    before = (root / 'workflow-state.json').read_bytes()
    assert array.collect_array(root, '4500', scheduler_terminal=True)['status'] == 'failed'
    assert (root / 'workflow-state.json').read_bytes() == before
    report = array.finalize_array(root, '4500', scheduler_terminal=True)
    assert report['status'] == 'failed' and report['missing_tasks'] == [2, 3, 4, 5]
    assert json.loads((root / 'workflow-state.json').read_text(encoding='utf-8'))['status'] == 'failed'
    assert gate.current(root) == 'convergence'
    with pytest.raises(ValueError):
        run_id(simulation, monkeypatch, 1)
    assert len(simulation.launches) == 2


@pytest.mark.parametrize('change', ['input', 'output', 'receipt', 'config', 'review', 'environment'])
def test_tampering_is_rejected_from_real_evidence(simulation, monkeypatch, change):
    run_id(simulation, monkeypatch, 0)
    root = simulation.root
    folder = root / 'convergence/encut-0'
    paths = {'input': folder / 'INCAR', 'output': folder / 'vasprun.xml',
             'receipt': folder / array.RECEIPT_NAME, 'config': root / 'config.json',
             'review': root / 'review-convergence.json', 'environment': root / 'environment-review.json'}
    if change == 'input':
        paths[change].write_text('ENCUT=521\n')
    else:
        value = json.loads(paths[change].read_text(encoding='utf-8'))
        value.update({'parent_job_id': '9999'} if change == 'receipt' else {'tampered': True})
        core.save(paths[change], value)
    assert array.collect_array(root, '4500')['status'] == 'failed'
    assert array.finalize_array(root, '4500', scheduler_terminal=True)['status'] == 'failed'
    assert gate.current(root) == 'convergence'
    assert len(simulation.launches) == 1


@pytest.mark.parametrize('environment', [
    {'SLURM_ARRAY_TASK_ID': '-1'}, {'SLURM_ARRAY_TASK_ID': '6'},
    {'SLURM_ARRAY_JOB_ID': '4500_1'}, {'SLURM_NTASKS': '24'},
    {'SLURM_JOB_NUM_NODES': '1'}, {'SLURM_JOB_CPUS_PER_NODE': '2(x2)'},
    {'SLURM_CPUS_PER_TASK': '2'}, {'SLURM_ARRAY_TASK_COUNT': '5'},
])
def test_wrong_array_identity_or_actual_allocation_never_launches(simulation, monkeypatch, environment):
    for name, value in environment.items():
        monkeypatch.setenv(name, value)
    with pytest.raises(ValueError):
        array.run_array_test(simulation.root, ['srun', 'vasp_std'])
    assert not simulation.launches


def test_test_lock_blocks_duplicates_without_root_state_write(simulation, monkeypatch):
    root = simulation.root
    lock = root / 'convergence/encut-0/.array-test.lock'
    lock.write_text('another element owns this stage')
    before = (root / 'workflow-state.json').read_bytes()
    with pytest.raises(FileExistsError):
        run_id(simulation, monkeypatch, 0)
    assert lock.exists() and not simulation.launches
    assert before == (root / 'workflow-state.json').read_bytes()


def test_process_summary_lock_serializes_collectors(simulation):
    entered = threading.Event()
    released = threading.Event()
    order = []
    def first():
        with array._finalize_lock(simulation.root):
            order.append('first')
            entered.set()
            released.wait(3)
    def second():
        entered.wait(3)
        with array._finalize_lock(simulation.root):
            order.append('second')
    a, b = threading.Thread(target=first), threading.Thread(target=second)
    a.start()
    b.start()
    assert entered.wait(3)
    time.sleep(.05)
    assert order == ['first']
    released.set()
    a.join(3)
    b.join(3)
    assert order == ['first', 'second']


@pytest.mark.parametrize('fixed', [None, 598])
def test_reused_encut_runs_only_real_mesh_tasks_and_optional_cross_point(simulation, monkeypatch, fixed):
    run_all(simulation, monkeypatch)
    root = simulation.root
    target = root.parent / 'mesh-array-retry'
    reuse.prepare_retry(root, target, 'mesh', meshes=[[6]*3, [8]*3, [10]*3], fixed_encut=fixed)
    ready(target, monkeypatch)
    simulation.launches.clear()
    report = run_all(simulation, monkeypatch, target)
    assert report['status'] == 'awaiting_review'
    assert len(simulation.launches) == (4 if fixed else 3)
    assert not (target / 'convergence/encut-0').exists()
    data = json.loads((target / 'convergence.json').read_text(encoding='utf-8'))
    assert data['encut']['origin'] == 'reused' and data['mesh']['origin'] == 'computed'
    assert ('cross_cutoff' in data) == (fixed is not None)
    if fixed:
        assert data['cross_cutoff']['status'] == 'passed'
        assert Incar.from_file(target / 'convergence/cross-cutoff/INCAR')['ENCUT'] == 676


def test_ordinary_run_cannot_bypass_array_or_create_root_lock(simulation):
    with pytest.raises(ValueError, match='run-array-test'):
        core.run(NS(workdir=str(simulation.root), command=['srun', 'vasp_std']))
    with pytest.raises(ValueError, match='run-array-test'):
        full.run_full(simulation.root, ['srun', 'vasp_std'])
    assert not (simulation.root / '.agent.lock').exists()
    assert not simulation.launches


def test_killed_finalizer_recovers_only_with_all_real_checkpoint_evidence(simulation, monkeypatch):
    run_all(simulation, monkeypatch)
    root = simulation.root
    (root / array.SUMMARY_NAME).unlink()
    report = array.finalize_array(root, '4500', scheduler_terminal=True, scheduler_evidence=scheduler_evidence())
    assert report['status'] == 'awaiting_review'
    assert len(simulation.launches) == 6 and gate.current(root) == 'relaxation'
    # A disappeared receipt defeats the checkpoint too, even though Slurm
    # might report all six processes COMPLETED.
    (root / array.SUMMARY_NAME).unlink()
    (root / 'convergence/mesh-2' / array.RECEIPT_NAME).unlink()
    report = array.finalize_array(root, '4500', scheduler_terminal=True)
    assert report['status'] == 'failed' and gate.current(root) == 'convergence'
    with pytest.raises(ValueError):
        gate.require_review(root, 'relaxation')


def test_success_receipt_changed_after_gate_revokes_previous_array_completion(simulation, monkeypatch):
    run_all(simulation, monkeypatch)
    root = simulation.root
    path = root / 'convergence/encut-0' / array.RECEIPT_NAME
    receipt = json.loads(path.read_text(encoding='utf-8'))
    receipt['parent_job_id'] = '9999'
    core.save(path, receipt)
    report = array.finalize_array(root, '4500', scheduler_terminal=True)
    assert report['status'] == 'failed' and gate.current(root) == 'convergence'
    assert json.loads((root / 'workflow-state.json').read_text(encoding='utf-8'))['status'] == 'failed'


def test_large_per_element_mpi_layout_uses_server_runtime_limits(monkeypatch):
    monkeypatch.setenv('SLURM_NTASKS', '1024')
    monkeypatch.setenv('SLURM_JOB_NUM_NODES', '16')
    monkeypatch.setenv('SLURM_JOB_CPUS_PER_NODE', '64(x16)')
    monkeypatch.setenv('SLURM_CPUS_PER_TASK', '1')
    command = array.execution_command({'cores_per_test': 1024, 'nodes': 16, 'parallel_tests': 6}, ['srun', 'vasp_std'])
    assert '--ntasks=1024' in command and '--ntasks-per-node=64' in command
    with pytest.raises(ValueError):
        array.execution_command({'cores_per_test': 65536, 'nodes': 16, 'parallel_tests': 6}, ['srun', 'vasp_std'])


def test_reused_mesh_executes_only_three_encut_elements(simulation, monkeypatch):
    run_all(simulation, monkeypatch)
    target = simulation.root.parent / 'encut-array-retry'
    reuse.prepare_retry(simulation.root, target, 'encut', encuts=[676, 780, 900])
    ready(target, monkeypatch)
    simulation.launches.clear()
    report = run_all(simulation, monkeypatch, target)
    assert report['status'] == 'awaiting_review'
    assert [stage for stage, _ in simulation.launches] == [f'convergence/encut-{i}' for i in range(3)]
    data = json.loads((target / 'convergence.json').read_text(encoding='utf-8'))
    assert data['mesh']['origin'] == 'reused' and data['encut']['origin'] == 'computed'


def test_cli_dispatches_array_without_ordinary_run_lock(tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr(array, 'run_array_test', lambda root, command: calls.append((root, command)))
    monkeypatch.setattr(core.sys, 'argv', ['vasp-band-agent', 'run-array-test', str(tmp_path), '--', 'srun', 'vasp_std'])
    core.main()
    assert calls == [(tmp_path.resolve(), ['srun', 'vasp_std'])]
    assert not (tmp_path / '.agent.lock').exists()


def complete_receipts_without_summary(simulation, monkeypatch):
    with monkeypatch.context() as scoped:
        scoped.setattr(array, 'finalize_array', lambda *args, **kwargs: {'status': 'running'})
        run_all(simulation, monkeypatch)


@pytest.mark.parametrize('state,code', [('TIMEOUT', '0:15'), ('FAILED', '1:0'), ('CANCELLED', '0:0'), ('COMPLETED', '1:0')])
def test_scheduler_failure_blocks_complete_real_outputs(simulation, monkeypatch, state, code):
    complete_receipts_without_summary(simulation, monkeypatch)
    root = simulation.root
    evidence = scheduler_evidence(changes={2: {'state': state, 'exit_code': code, 'successful': False}})
    assert array.collect_array(root, '4500', True, evidence)['status'] == 'failed'
    assert not (root / array.SUMMARY_NAME).exists()
    report = array.finalize_array(root, '4500', True, evidence)
    assert report['status'] == 'failed' and report['scheduler_failed_tasks'] == [2]
    assert gate.current(root) == 'convergence'
    assert len(simulation.launches) == 6
    # A later worker call cannot turn the persisted scheduler failure into
    # success using the same complete XML and receipts.
    assert array.finalize_array(root, '4500')['status'] == 'failed'


def test_late_timeout_revokes_worker_completed_stage(simulation, monkeypatch):
    run_all(simulation, monkeypatch)
    root = simulation.root
    assert gate.current(root) == 'relaxation'
    evidence = scheduler_evidence(changes={5: {'state': 'TIMEOUT', 'exit_code': '0:15', 'successful': False}})
    report = array.finalize_array(root, '4500', True, evidence)
    assert report['status'] == 'failed' and report['scheduler_failed_tasks'] == [5]
    assert gate.current(root) == 'convergence'
    assert json.loads((root / 'workflow-state.json').read_text(encoding='utf-8'))['status'] == 'failed'


@pytest.mark.parametrize('change', ['missing', 'parent', 'duplicate', 'count', 'job_id', 'nonterminal', 'missing_exit'])
def test_terminal_evidence_is_exact_and_cannot_use_only_completed_aggregate(simulation, monkeypatch, change):
    complete_receipts_without_summary(simulation, monkeypatch)
    evidence = scheduler_evidence()
    if change == 'missing':
        evidence = None
    elif change == 'parent':
        evidence['job_id'] = '9999'
    elif change == 'duplicate':
        evidence['tasks'][2] = dict(evidence['tasks'][1])
    elif change == 'count':
        evidence['task_count'] = 5
    elif change == 'job_id':
        evidence['tasks'][2]['job_id'] = '9999_2'
    elif change == 'nonterminal':
        evidence['tasks'][2]['terminal'] = False
    elif change == 'missing_exit':
        evidence['tasks'][2].pop('exit_code')
    report = array.finalize_array(simulation.root, '4500', True, evidence)
    assert report['status'] == 'failed' and gate.current(simulation.root) == 'convergence'


def test_terminal_good_scheduler_rows_still_require_real_xml(simulation, monkeypatch):
    complete_receipts_without_summary(simulation, monkeypatch)
    report = array.finalize_array(simulation.root, '4500', True, scheduler_evidence())
    assert report['status'] == 'awaiting_review'
    assert report['scheduler_evidence']['all_successful'] is True
    (simulation.root / 'convergence/encut-0/vasprun.xml').write_text('{"energy": -11.}')
    assert array.finalize_array(simulation.root, '4500', True, scheduler_evidence())['status'] == 'failed'
    assert gate.current(simulation.root) == 'convergence'


def test_killed_after_convergence_json_before_checkpoint_recovers_original_review(simulation, monkeypatch):
    complete_receipts_without_summary(simulation, monkeypatch)
    root = simulation.root
    def interrupted(*args, **kwargs):
        raise KeyboardInterrupt('synthetic SIGTERM before checkpoint')
    with monkeypatch.context() as scoped:
        scoped.setattr(gate, 'complete', interrupted)
        with pytest.raises(KeyboardInterrupt):
            array.finalize_array(root, '4500')
    assert (root / 'convergence.json').exists() and (root / array.JOURNAL_NAME).exists()
    assert not (root / 'checkpoint.json').exists() and not (root / array.SUMMARY_NAME).exists()
    report = array.finalize_array(root, '4500', True, scheduler_evidence())
    assert report['status'] == 'awaiting_review' and gate.current(root) == 'relaxation'
    assert len(simulation.launches) == 6


@pytest.mark.parametrize('change', ['receipt', 'script', 'review', 'journal'])
def test_interrupted_summary_recovery_cannot_ignore_changed_authorization(simulation, monkeypatch, change):
    complete_receipts_without_summary(simulation, monkeypatch)
    root = simulation.root
    with monkeypatch.context() as scoped:
        scoped.setattr(gate, 'complete', lambda *args: (_ for _ in ()).throw(KeyboardInterrupt()))
        with pytest.raises(KeyboardInterrupt):
            array.finalize_array(root, '4500')
    if change == 'script':
        (root / 'submit.slurm').write_text('changed submit script')
    elif change == 'review':
        path = root / 'review-convergence.json'
        data = json.loads(path.read_text(encoding='utf-8'))
        data['reviewer'] = 'changed reviewer'
        core.save(path, data)
    elif change == 'journal':
        path = root / array.JOURNAL_NAME
        data = json.loads(path.read_text(encoding='utf-8'))
        data['parent_job_id'] = '9999'
        core.save(path, data)
    else:
        path = root / 'convergence/encut-0' / array.RECEIPT_NAME
        data = json.loads(path.read_text(encoding='utf-8'))
        data['extra'] = 'changed receipt'
        core.save(path, data)
    assert array.finalize_array(root, '4500', True, scheduler_evidence())['status'] == 'failed'
    assert gate.current(root) == 'convergence'


def test_saved_submission_binds_parent_and_pending_reservation_is_safe(simulation, monkeypatch):
    root = simulation.root
    records = root / 'submissions'
    records.mkdir()
    record = records / 'convergence.json'
    reservation = {'status': 'submission_uncertain', 'job_id': None, 'phase': 'convergence',
                   'submission_mode': 'array', 'array_task_count': 6, 'array_task_ids': list(range(6))}
    core.save(record, reservation)
    assert run_id(simulation, monkeypatch, 0)['status'] == 'running'
    # sbatch may start an element before the submitting process saves its ID.
    # The atomic parent claim prevents a different array from claiming task 1.
    monkeypatch.setenv('SLURM_ARRAY_JOB_ID', '9999')
    with pytest.raises(ValueError, match='另一父数组'):
        run_id(simulation, monkeypatch, 1)
    core.save(record, {**reservation, 'status': 'submitted', 'job_id': '4500'})
    with pytest.raises(ValueError, match='提交记录'):
        run_id(simulation, monkeypatch, 1)
    monkeypatch.setenv('SLURM_ARRAY_JOB_ID', '4500')
    assert run_id(simulation, monkeypatch, 1)['status'] == 'running'
    assert len(simulation.launches) == 2


def test_saved_other_parent_blocks_before_first_vasp_launch(simulation, monkeypatch):
    root = simulation.root
    (root / 'submissions').mkdir()
    core.save(root / 'submissions/convergence.json', {'status': 'submitted', 'job_id': '9999', 'phase': 'convergence', 'submission_mode': 'array'})
    with pytest.raises(ValueError, match='提交记录'):
        run_id(simulation, monkeypatch, 0)
    assert not simulation.launches and not (root / array.PARENT_NAME).exists()


def test_killed_between_checkpoint_and_workflow_state_recovers_matching_state(simulation, monkeypatch):
    complete_receipts_without_summary(simulation, monkeypatch)
    root = simulation.root
    def checkpoint_only(root, phase):
        core.save(root / 'checkpoint.json', {'completed': ['convergence'], 'next': 'relaxation',
                                            'evidence_hashes': gate.evidence(root, 'relaxation')['hashes']})
        raise KeyboardInterrupt('synthetic SIGTERM before workflow-state write')
    with monkeypatch.context() as scoped:
        scoped.setattr(gate, 'complete', checkpoint_only)
        with pytest.raises(KeyboardInterrupt):
            array.finalize_array(root, '4500')
    assert json.loads((root / 'workflow-state.json').read_text(encoding='utf-8'))['status'] == 'prepared'
    report = array.finalize_array(root, '4500', True, scheduler_evidence())
    assert report['status'] == 'awaiting_review'
    state = json.loads((root / 'workflow-state.json').read_text(encoding='utf-8'))
    assert state['status'] == 'awaiting_review' and state['next'] == 'relaxation'


def test_scheduler_confirmation_preserves_original_success_provenance(simulation, monkeypatch):
    run_all(simulation, monkeypatch)
    root = simulation.root
    report = array.finalize_array(root, '4500', True, scheduler_evidence())
    assert report['status'] == 'awaiting_review' and report['scheduler_confirmed_at']
    saved = json.loads((root / array.SUMMARY_NAME).read_text(encoding='utf-8'))
    assert saved['scheduler_evidence']['all_successful'] is True
    assert saved['convergence_sha256'] == core.digest(root / 'convergence.json')
    assert array.collect_array(root, '4500')['status'] == 'awaiting_review'
