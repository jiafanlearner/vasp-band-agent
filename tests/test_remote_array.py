"""Array submissions use mocked engines and schedulers only."""
import json
import shlex
import subprocess
from copy import deepcopy
from types import SimpleNamespace as NS

import pytest

from vasp_agent.remote import remote_worker as worker


ARRAY_RESOURCES = dict(submission_mode='array', parallel_tests=3,
                       cores_per_test=16, nodes=2, partition='compute',
                       walltime='04:00:00')
ARRAY_LIMITS = dict(nodes=6, cores=48, partition='compute', walltime='04:00:00')
PARENT_ID = '69800234'


def slurm_script(array='0-5%3', *, nodes=2, tasks=16, cpus=1):
    directives = ['#!/bin/bash', '#SBATCH --partition=compute',
                  f'#SBATCH --nodes={nodes}', f'#SBATCH --ntasks={tasks}',
                  f'#SBATCH --ntasks-per-node={tasks // nodes}',
                  f'#SBATCH --cpus-per-task={cpus}', '#SBATCH --time=04:00:00']
    if array is not None:
        directives.append(f'#SBATCH --array={array}')
    return '\n'.join(directives) + '\nexport OMP_NUM_THREADS=1\nsrun vasp_std\n'


def write_json(path, value):
    path.write_text(json.dumps(value), encoding='utf-8')


@pytest.fixture
def task(tmp_path, monkeypatch):
    """A prepared allocation task that can be converted without real engines."""
    folder = tmp_path / 'jobs' / 'silicon'
    folder.mkdir(parents=True)
    release = tmp_path / 'release'
    release.mkdir()
    write_json(folder / 'config.json', dict(parallel_tests=3, cores_per_test=16,
        nodes=1, encuts=[400, 500, 600], meshes=[[2, 2, 2], [3, 3, 3], [4, 4, 4]],
        active_scans=['encut', 'mesh'], nbands=16))
    write_json(folder / 'manifest.json', {'stage_gates': True, 'hashes': {}})
    script = slurm_script(None, nodes=1, tasks=48)
    script = script.replace('srun vasp_std\n', shlex.join([
        '/opt/python', str(release / 'vasp_band_agent.py'), 'run', str(folder),
        '--', 'srun', 'vasp_std']) + '\n')
    (folder / 'submit.slurm').write_text(script, encoding='utf-8')
    write_json(folder / 'remote-job.json', {'release': str(release),
        'script_sha256': worker.sha(folder / 'submit.slurm')})
    modules = dict(
        vasp_band_agent=NS(verify=lambda _: {'stage_gates': True}),
        stage_gate=NS(current=lambda _: 'convergence', require_review=lambda *args: None),
        full_workflow=NS(RESOURCE_LAYOUT_VERSION=2),
        convergence_array=NS(ARRAY_EXECUTION_VERSION=1,
                             tests_from_config=lambda _: [{'task_id': n} for n in range(6)]))
    monkeypatch.setattr(worker, 'engine_module', lambda name: modules[name])
    monkeypatch.setattr(worker.subprocess, 'run',
                        lambda *args, **kwargs: pytest.fail('unexpected scheduler command'))
    return folder, modules


@pytest.mark.parametrize('array', ['0-0%1', '0-2%3', '0-5%3', '0-6%6'])
def test_strict_array_range_accepts_bounded_count_and_concurrency(array):
    worker.check_resources(slurm_script(array), {**ARRAY_LIMITS, 'nodes': 12, 'cores': 96})


@pytest.mark.parametrize('array', ['0-5', '1-5%3', '0-7%3', '0-5%0', '0-5%7',
                                 '0-0%2', '0-2%4', '0,1,2%2', '0-5:2%2',
                                 '[0-5]%2', '0-5%03'])
def test_array_range_and_concurrency_fail_closed(array):
    with pytest.raises(ValueError):
        worker.check_resources(slurm_script(array), {**ARRAY_LIMITS, 'nodes': 256, 'cores': 65536})


@pytest.mark.parametrize('limit_change', [{'cores': 47}, {'nodes': 5}])
def test_array_authorization_checks_concurrent_peak_not_per_element(limit_change):
    # Three elements, each 8 tasks * 2 CPUs and two nodes, peak at 48 CPUs/6 nodes.
    script = slurm_script(nodes=2, tasks=8, cpus=2)
    worker.check_resources(script, ARRAY_LIMITS)
    with pytest.raises(ValueError, match='上限'):
        worker.check_resources(script, {**ARRAY_LIMITS, **limit_change})


def test_array_checks_retain_duplicate_and_per_node_layout_guards():
    script = slurm_script()
    with pytest.raises(ValueError):
        worker.check_resources(script + '#SBATCH --array=0-5%3\n', ARRAY_LIMITS)
    with pytest.raises(ValueError, match='不一致'):
        worker.check_resources(script.replace('--ntasks-per-node=8', '--ntasks-per-node=16'), ARRAY_LIMITS)


def test_existing_allocation_resource_bounds_remain_total_allocation():
    worker.check_resources(slurm_script(None, nodes=2, tasks=48),
                           {**ARRAY_LIMITS, 'nodes': 2})


def test_configure_array_uses_one_element_resources_and_actual_test_count(task):
    folder, modules = task
    config = json.loads((folder / 'config.json').read_text())
    config.update(active_scans=['mesh'], fixed_encut=500)
    write_json(folder / 'config.json', config)
    modules['convergence_array'].tests_from_config = lambda _: [{'task_id': n} for n in range(4)]
    before = worker.resource_context(folder, 'convergence')
    result = worker.configure_resources(folder, 'convergence', ARRAY_RESOURCES,
                                        before['signature'], ARRAY_LIMITS)
    script = (folder / 'submit.slurm').read_text()
    assert result['changed']
    assert '#SBATCH --nodes=2\n' in script
    assert '#SBATCH --ntasks=16\n' in script
    assert '#SBATCH --ntasks-per-node=8\n' in script
    assert '#SBATCH --array=0-3%3\n' in script
    assert 'run-array-test' in script
    assert shlex.split(script.splitlines()[-1])[-3:] == ['--', 'srun', 'vasp_std']
    saved = json.loads((folder / 'config.json').read_text())
    assert saved['submission_mode'] == 'array' and saved['nbands'] == 16
    assert result['resources']['submission_mode'] == 'array'
    assert result['resources']['nodes'] == 2 and result['resources']['cores_per_test'] == 16
    assert result['execution_plan']['tests'] == 4
    assert result['execution_plan']['array_elements'] == 4
    worker.check_resources(script, ARRAY_LIMITS)
    meta = json.loads((folder / 'remote-job.json').read_text())
    assert meta['script_sha256'] == worker.sha(folder / 'submit.slurm')


@pytest.mark.parametrize('version', [None, 0])
def test_array_rejects_old_engine_before_mutating_task(task, version):
    folder, modules = task
    if version is None:
        del modules['convergence_array'].ARRAY_EXECUTION_VERSION
    else:
        modules['convergence_array'].ARRAY_EXECUTION_VERSION = version
    files = ['config.json', 'manifest.json', 'submit.slurm', 'remote-job.json']
    original = {name: (folder / name).read_bytes() for name in files}
    before = worker.resource_context(folder, 'convergence')
    with pytest.raises(ValueError):
        worker.configure_resources(folder, 'convergence', ARRAY_RESOURCES,
                                   before['signature'], ARRAY_LIMITS)
    assert {name: (folder / name).read_bytes() for name in files} == original


def test_array_rejects_bound_flat_release_missing_array_engine(task, monkeypatch):
    folder, _ = task
    before = worker.resource_context(folder, 'convergence')
    original = (folder / 'submit.slurm').read_bytes()
    monkeypatch.setattr(worker, '__package__', '')
    with pytest.raises(ValueError, match='旧引擎'):
        worker.configure_resources(folder, 'convergence', ARRAY_RESOURCES,
                                   before['signature'], ARRAY_LIMITS)
    assert (folder / 'submit.slurm').read_bytes() == original


@pytest.mark.parametrize('phase,stage_gates', [('convergence', False), ('scf', True), (None, False)])
def test_array_configuration_requires_convergence_stage_gates(task, phase, stage_gates):
    folder, modules = task
    modules['stage_gate'].current = lambda _: phase
    write_json(folder / 'manifest.json', {'stage_gates': stage_gates, 'hashes': {}})
    before = worker.resource_context(folder, phase)
    resources = {**ARRAY_RESOURCES, 'parallel_tests': 1}
    with pytest.raises(ValueError):
        worker.configure_resources(folder, phase, resources, before['signature'], ARRAY_LIMITS)


def test_configure_allocation_preserves_total_tasks_and_plain_run(task):
    folder, _ = task
    before = worker.resource_context(folder, 'convergence')
    result = worker.configure_resources(folder, 'convergence',
        {**ARRAY_RESOURCES, 'submission_mode': 'allocation'}, before['signature'],
        {**ARRAY_LIMITS, 'nodes': 2})
    script = (folder / 'submit.slurm').read_text()
    assert result['changed']
    assert '#SBATCH --ntasks=48\n' in script and '#SBATCH --ntasks-per-node=24\n' in script
    assert '#SBATCH --array=' not in script and 'run-array-test' not in script


def test_next_phase_replaces_array_directives_and_launcher_with_allocation(task, monkeypatch):
    folder, modules = task
    before = worker.resource_context(folder, 'convergence')
    worker.configure_resources(folder, 'convergence', ARRAY_RESOURCES,
                               before['signature'], ARRAY_LIMITS)
    (folder / 'submissions').mkdir()
    write_json(folder / 'submissions' / 'convergence.json', dict(job_id=PARENT_ID,
        submission_mode='array', array_task_count=6, array_concurrency=3,
        array_task_ids=list(range(6))))
    modules['stage_gate'].current = lambda _: 'relaxation'
    monkeypatch.setattr(worker.subprocess, 'run', lambda command, **kwargs:
        NS(returncode=0, stdout='' if command[0] == 'squeue' else '\n'.join(
            f'{PARENT_ID}_{index}|COMPLETED|0:0|' for index in range(6)), stderr=''))
    context = worker.resource_context(folder, 'relaxation')
    assert context['resources']['submission_mode'] == 'allocation'
    assert context['resources']['parallel_tests'] == 1
    result = worker.configure_resources(folder, 'relaxation', context['resources'],
                                        context['signature'], ARRAY_LIMITS)
    script = (folder / 'submit.slurm').read_text()
    assert result['changed']
    assert '#SBATCH --array=' not in script and 'run-array-test' not in script
    assert '#SBATCH --ntasks=16\n' in script and '#SBATCH --ntasks-per-node=8\n' in script
    assert json.loads((folder / 'config.json').read_text())['submission_mode'] == 'allocation'


def test_array_receipt_records_parent_and_elements_from_one_sbatch(task, monkeypatch):
    folder, _ = task
    (folder / 'submit.slurm').write_text(slurm_script('0-3%3'), encoding='utf-8')
    calls = []

    def run(command, **kwargs):
        calls.append((command, kwargs))
        assert command[0] == 'sbatch'
        return NS(returncode=0, stdout=PARENT_ID + ';compute\n', stderr='')

    monkeypatch.setattr(worker.subprocess, 'run', run)
    record = folder / 'submissions' / 'convergence.json'
    receipt = worker.submit_with_receipt(folder, record, 'convergence', True,
                                         {'capacity_checked': True})
    assert len(calls) == 1
    # The session rides the same --export as the phase, so assert on the
    # comma-separated items rather than on the whole argv element.
    export = [token for token in calls[0][0] if token.startswith('--export=')].pop()
    assert 'ALL' in export.split('=', 1)[1].split(',')
    assert 'VASP_AGENT_PHASE=convergence' in export.split(',')
    assert receipt['job_id'] == PARENT_ID and receipt['status'] == 'submitted'
    assert receipt['submission_mode'] == 'array'
    assert receipt['array_task_count'] == 4 and receipt['array_concurrency'] == 3
    assert receipt['array_task_ids'] == list(range(4))
    assert json.loads(record.read_text()) == receipt
    assert json.loads((folder / 'remote-submission.json').read_text()) == receipt
    replay = worker.dispatch(dict(config={'root': str(folder.parents[1])},
        action='submit', job_name=folder.name, phase='convergence', limits=ARRAY_LIMITS))
    assert replay == receipt and len(calls) == 1


@pytest.mark.parametrize('phase,staged', [('scf', True), ('convergence', False), (None, False)])
def test_array_receipt_refuses_other_phase_or_nonstaged_submit(task, phase, staged):
    folder, _ = task
    (folder / 'submit.slurm').write_text(slurm_script(), encoding='utf-8')
    record = folder / 'remote-submission.json'
    with pytest.raises(ValueError):
        worker.submit_with_receipt(folder, record, phase, staged, {})
    # The lock file now stays behind on purpose, matching job_monitor.FileLock:
    # unlinking on release would let two processes lock two different inodes of
    # the same path and both think they were exclusive. What must hold is that
    # nobody is holding it once we return.
    assert not record.exists() and not worker.lock_is_held(folder / '.submission-recovery.lock')


def test_array_uncertain_submit_keeps_array_metadata_without_second_attempt(task, monkeypatch):
    folder, _ = task
    (folder / 'submit.slurm').write_text(slurm_script('0-3%2'), encoding='utf-8')
    calls = []

    def run(command, **kwargs):
        calls.append(command)
        raise subprocess.TimeoutExpired(command, 30)

    monkeypatch.setattr(worker.subprocess, 'run', run)
    record = folder / 'submissions' / 'convergence.json'
    with pytest.raises(worker.SubmissionError) as error:
        worker.submit_with_receipt(folder, record, 'convergence', True, {})
    assert error.value.error_code == 'SUBMISSION_UNCERTAIN'
    saved = json.loads(record.read_text())
    assert saved['status'] == 'submission_uncertain' and saved['job_id'] is None
    assert saved['submission_mode'] == 'array' and saved['array_task_count'] == 4
    assert saved['array_concurrency'] == 2 and saved['array_task_ids'] == [0, 1, 2, 3]
    replay = worker.dispatch(dict(config={'root': str(folder.parents[1])},
        action='submit', job_name=folder.name, phase='convergence'))
    assert replay == saved and len(calls) == 1


@pytest.fixture
def previous_array(tmp_path):
    (tmp_path / 'submissions').mkdir()
    write_json(tmp_path / 'submissions' / 'convergence.json', dict(job_id=PARENT_ID,
        submission_mode='array', array_task_count=3, array_concurrency=2,
        array_task_ids=[0, 1, 2]))
    return tmp_path


@pytest.mark.parametrize('queue', [PARENT_ID, f'{PARENT_ID}_1',
                                 f'{PARENT_ID}_[0-2]', f'{PARENT_ID}_[0-2%2]'])
def test_previous_array_live_parent_element_or_compressed_range_blocks(previous_array, monkeypatch, queue):
    calls = []

    def run(command, **kwargs):
        calls.append(command)
        assert command[0] == 'squeue', 'a live array must block before accounting'
        return NS(returncode=0, stdout=queue + '\n', stderr='')

    monkeypatch.setattr(worker.subprocess, 'run', run)
    with pytest.raises(ValueError, match='尚未退出'):
        worker.check_previous_job(previous_array, 'relaxation')
    assert len(calls) == 1


@pytest.mark.parametrize('accounting', [
    f'{PARENT_ID}|COMPLETED|0:0|\n',
    f'{PARENT_ID}_0|COMPLETED|0:0|\n{PARENT_ID}_1|COMPLETED|0:0|\n',
    f'{PARENT_ID}|COMPLETED|0:0|\n{PARENT_ID}_0|COMPLETED|0:0|\n'
    f'{PARENT_ID}_1|FAILED|1:0|\n{PARENT_ID}_2|COMPLETED|0:0|\n',
    f'{PARENT_ID}_0|COMPLETED|0:0|\n{PARENT_ID}_1|COMPLETED|0:1|\n{PARENT_ID}_2|COMPLETED|0:0|\n',
    f'{PARENT_ID}_0.batch|COMPLETED|0:0|\n{PARENT_ID}_1.batch|COMPLETED|0:0|\n'
    f'{PARENT_ID}_2.batch|COMPLETED|0:0|\n',
])
def test_previous_array_needs_successful_accounting_for_every_element(previous_array, monkeypatch, accounting):
    monkeypatch.setattr(worker.subprocess, 'run', lambda command, **kwargs:
        NS(returncode=0, stdout='' if command[0] == 'squeue' else accounting, stderr=''))
    with pytest.raises(ValueError):
        worker.check_previous_job(previous_array, 'relaxation')


def test_previous_array_all_completed_elements_allow_next_stage_without_parent_row(previous_array, monkeypatch):
    accounting = '\n'.join(f'{PARENT_ID}_{task_id}|COMPLETED|0:0|' for task_id in range(3))
    calls = []

    def run(command, **kwargs):
        calls.append(command)
        return NS(returncode=0, stdout='999999_0\n' if command[0] == 'squeue' else accounting, stderr='')

    monkeypatch.setattr(worker.subprocess, 'run', run)
    worker.check_previous_job(previous_array, 'relaxation')
    assert [command[0] for command in calls] == ['squeue', 'sacct']
    assert not (previous_array / 'submissions' / 'relaxation.json').exists()


def array_status(task, monkeypatch, *, queue='', accounting='', queue_returncode=0):
    folder, modules = task
    config = json.loads((folder / 'config.json').read_text())
    write_json(folder / 'config.json', {**config, 'submission_mode': 'array'})
    receipt = dict(job_id=PARENT_ID, submission_mode='array', array_task_count=3,
                   array_concurrency=2, array_task_ids=[0, 1, 2])
    write_json(folder / 'remote-submission.json', receipt)
    commands = []

    def run(command, **kwargs):
        commands.append(command)
        if command[0] == 'squeue':
            return NS(returncode=queue_returncode, stdout=queue, stderr='controller unavailable' if queue_returncode else '')
        assert command[0] == 'sacct'
        return NS(returncode=0, stdout=accounting, stderr='')

    monkeypatch.setattr(worker.subprocess, 'run', run)
    info = worker.dispatch(dict(config={'root': str(folder.parents[1])},
                                action='status', job_name=folder.name))
    return info, commands


def test_status_expands_live_elements_and_parent_absence_does_not_mean_completed(task, monkeypatch):
    folder, modules = task
    modules['convergence_array'].finalize_array = lambda *args, **kwargs: pytest.fail('active array must not finalize')
    queue = f'{PARENT_ID}_0|RUNNING|00:01|node01\n{PARENT_ID}_1|PENDING|00:00|Resources\n'
    info, commands = array_status(task, monkeypatch, queue=queue)
    assert any(flag in commands[0] for flag in ('--array', '-r'))
    assert info['scheduler_status_known']
    assert info['array_tasks']['active'] and not info['array_tasks']['all_terminal']
    assert [item['state'] for item in info['array_tasks']['tasks']] == ['RUNNING', 'PENDING', 'UNKNOWN']
    assert 'array_collection' not in info


def test_status_collects_terminal_array_through_engine_once(task, monkeypatch):
    folder, modules = task
    collections = []

    def finalize(*args, **kwargs):
        collections.append((args, kwargs))
        return {'status': 'completed', 'next': 'relaxation'}

    modules['convergence_array'].finalize_array = finalize
    accounting = '\n'.join(f'{PARENT_ID}_{index}|COMPLETED|0:0|' for index in range(3))
    info, _ = array_status(task, monkeypatch, accounting=accounting)
    assert info['scheduler_status_known']
    assert info['array_tasks']['all_terminal'] and info['array_tasks']['all_successful']
    assert collections == [((folder, PARENT_ID), {'scheduler_terminal': True, 'scheduler_evidence': info['array_tasks']})]
    assert info['array_collection'] == {'status': 'completed', 'next': 'relaxation'}


@pytest.mark.parametrize('accounting', ['', f'{PARENT_ID}|COMPLETED|0:0|',
                                     f'{PARENT_ID}_0|COMPLETED|0:0|'])
def test_status_never_collects_array_with_missing_element_accounting(task, monkeypatch, accounting):
    _, modules = task
    modules['convergence_array'].finalize_array = lambda *args, **kwargs: pytest.fail('incomplete accounting must not finalize')
    info, _ = array_status(task, monkeypatch, accounting=accounting)
    assert not info['array_tasks']['all_terminal']
    assert 'array_collection' not in info


def test_status_queue_query_failure_prevents_collection_even_with_complete_accounting(task, monkeypatch):
    _, modules = task
    modules['convergence_array'].finalize_array = lambda *args, **kwargs: pytest.fail('failed queue query must not finalize')
    accounting = '\n'.join(f'{PARENT_ID}_{index}|COMPLETED|0:0|' for index in range(3))
    info, _ = array_status(task, monkeypatch, accounting=accounting, queue_returncode=1)
    assert info['scheduler_errors'][0]['command'] == 'squeue'
    assert 'array_collection' not in info


def test_status_failed_terminal_array_surfaces_engine_collection_error(task, monkeypatch):
    _, modules = task
    collections = []

    def finalize(*args, **kwargs):
        collections.append((args, kwargs))
        raise ValueError('VASP output verification failed')

    modules['convergence_array'].finalize_array = finalize
    accounting = (f'{PARENT_ID}_0|COMPLETED|0:0|\n{PARENT_ID}_1|FAILED|1:0|\n'
                  f'{PARENT_ID}_2|COMPLETED|0:0|\n')
    info, _ = array_status(task, monkeypatch, accounting=accounting)
    assert info['array_tasks']['all_terminal'] and not info['array_tasks']['all_successful']
    assert len(collections) == 1
    assert info['array_collection_error'] == 'VASP output verification failed'


@pytest.fixture
def array_planner(monkeypatch):
    context = dict(signature='array-snapshot', resources={**ARRAY_RESOURCES,
        'parallel_tests': 1, 'nodes': 1}, execution_plan={'tests': 6, 'array_elements': 6})
    monkeypatch.setattr(worker, 'resource_context', lambda *args: deepcopy(context))
    monkeypatch.setattr(worker.subprocess, 'run',
                        lambda *args, **kwargs: pytest.fail('planner must use its mock reader'))
    commands = []

    def read(command):
        commands.append(command)
        return '64|257667|20' if command[0] == 'sinfo' else 'DefMemPerCPU=3570'

    return context, read, commands


def test_array_planner_six_tests_use_one_node_per_element_with_six_node_grant(array_planner):
    context, read, commands = array_planner
    result = worker.plan_fast_resources(None, 'convergence',
        {**ARRAY_LIMITS, 'cores': 96}, read=read)
    assert result['resources'] == {**context['resources'], 'parallel_tests': 6}
    assert result['capacity']['nodes'] == 1 and result['capacity']['cpus_per_node'] == 16
    assert result['estimated_waves'] == 1
    assert len(commands) == 2
    assert not result['submitted'] and not result['operator_confirmed']


@pytest.mark.parametrize('limits,parallel', [({'nodes': 2, 'cores': 96}, 2),
                                           ({'nodes': 6, 'cores': 48}, 3)])
def test_array_planner_limits_both_peak_nodes_and_peak_cpus(array_planner, limits, parallel):
    _, read, _ = array_planner
    result = worker.plan_fast_resources(None, 'convergence', {**ARRAY_LIMITS, **limits}, read=read)
    resources = result['resources']
    assert resources['parallel_tests'] == parallel and resources['nodes'] == 1
    assert resources['cores_per_test'] == 16
    assert resources['parallel_tests'] * resources['nodes'] <= limits['nodes']
    assert resources['parallel_tests'] * resources['cores_per_test'] <= limits['cores']
    assert result['estimated_waves'] == (6 + parallel - 1) // parallel


def test_array_planner_multinode_element_reduces_parallelism_to_node_grant(array_planner):
    context, read, commands = array_planner
    context['resources']['cores_per_test'] = 128
    result = worker.plan_fast_resources(None, 'convergence',
        {**ARRAY_LIMITS, 'cores': 768}, read=read)
    assert result['resources']['parallel_tests'] == 3
    assert result['resources']['nodes'] == 2 and result['resources']['cores_per_test'] == 128
    assert result['capacity']['cpus_per_node'] == 64
    assert result['estimated_waves'] == 2 and len(commands) == 2


def test_array_planner_memory_can_require_more_nodes_per_element(array_planner):
    _, _, commands = array_planner

    def read(command):
        commands.append(command)
        return '64|50000|20' if command[0] == 'sinfo' else 'DefMemPerCPU=3570'

    result = worker.plan_fast_resources(None, 'convergence',
        {**ARRAY_LIMITS, 'cores': 96}, read=read)
    assert result['resources']['parallel_tests'] == 3 and result['resources']['nodes'] == 2
    assert result['capacity']['default_memory_mb_per_node'] == 28560
    assert len(commands) == 2


@pytest.mark.parametrize('tests,parallel', [(4, 4), (7, 6)])
def test_array_planner_script_uses_actual_count_and_per_element_tasks(array_planner, monkeypatch, tests, parallel):
    context, read, _ = array_planner
    context['execution_plan'].update(tests=tests, array_elements=tests)
    scripts = []
    original = worker.capacity_preflight

    def capacity(script, **kwargs):
        scripts.append(script)
        return original(script, **kwargs)

    monkeypatch.setattr(worker, 'capacity_preflight', capacity)
    result = worker.plan_fast_resources(None, 'convergence',
        {**ARRAY_LIMITS, 'cores': 96}, read=read)
    assert result['resources']['parallel_tests'] == parallel
    assert '#SBATCH --ntasks=16\n' in scripts[-1] + '\n'
    assert f'#SBATCH --array=0-{tests - 1}%{parallel}' in scripts[-1]
    assert len(scripts) == 1


def test_array_planner_without_grant_can_propose_one_cpu_elements_in_parallel(array_planner):
    context, read, _ = array_planner
    context['resources']['cores_per_test'] = 1
    result = worker.plan_fast_resources(None, 'convergence', read=read)
    assert result['resources']['parallel_tests'] == 6
    assert result['resources']['nodes'] == result['resources']['cores_per_test'] == 1
    assert result['limits'] is None and not result['operator_confirmed']


def test_array_planner_insufficient_cpu_grant_never_queries_capacity(array_planner):
    _, read, commands = array_planner
    with pytest.raises(worker.SubmissionError, match='没有可行'):
        worker.plan_fast_resources(None, 'convergence', {**ARRAY_LIMITS, 'cores': 15}, read=read)
    assert not commands


@pytest.fixture
def array_preflight(task):
    folder, modules = task
    context = worker.resource_context(folder, 'convergence')
    worker.configure_resources(folder, 'convergence', ARRAY_RESOURCES,
                               context['signature'], ARRAY_LIMITS)
    return folder, modules


@pytest.mark.parametrize('case', ['mode', 'invalid_mode', 'no_array', 'range', 'count',
    'concurrency', 'ntasks', 'nodes', 'cpus', 'launcher', 'target', 'command',
    'missing_launcher', 'duplicate_launcher', 'omp'])
def test_dispatch_preflight_rejects_array_mismatches_before_environment_or_sbatch(array_preflight, monkeypatch, case):
    folder, _ = array_preflight
    config = json.loads((folder / 'config.json').read_text())
    script = (folder / 'submit.slurm').read_text()
    if case == 'mode':
        config['submission_mode'] = 'allocation'
    elif case == 'invalid_mode':
        config['submission_mode'] = 'unknown'
    elif case == 'no_array':
        script = '\n'.join(line for line in script.splitlines() if '--array=' not in line) + '\n'
    elif case == 'range':
        script = script.replace('--array=0-5%3', '--array=0-7%3')
    elif case == 'count':
        script = script.replace('--array=0-5%3', '--array=0-4%3')
    elif case == 'concurrency':
        script = script.replace('--array=0-5%3', '--array=0-5%2')
    elif case == 'ntasks':
        script = script.replace('--ntasks=16', '--ntasks=32')
    elif case == 'nodes':
        script = script.replace('--nodes=2', '--nodes=1')
    elif case == 'cpus':
        script = script.replace('--cpus-per-task=1', '--cpus-per-task=2')
    elif case == 'launcher':
        script = script.replace('run-array-test', 'run')
    elif case == 'target':
        tokens = shlex.split(script.splitlines()[-1])
        position = tokens.index('run-array-test')
        tokens[position + 1] = str(folder.parent / 'other-task')
        script = '\n'.join(script.splitlines()[:-1] + [shlex.join(tokens)]) + '\n'
    elif case == 'command':
        script = script.replace('srun vasp_std', 'srun vasp_gam')
    elif case == 'missing_launcher':
        script = '\n'.join(script.splitlines()[:-1]) + '\n'
    elif case == 'duplicate_launcher':
        script += script.splitlines()[-1] + '\n'
    elif case == 'omp':
        script = script.replace('export OMP_NUM_THREADS=1', 'export OMP_NUM_THREADS=2')
    write_json(folder / 'config.json', config)
    (folder / 'submit.slurm').write_text(script, encoding='utf-8')
    meta = json.loads((folder / 'remote-job.json').read_text())
    write_json(folder / 'remote-job.json', {**meta, 'script_sha256': worker.sha(folder / 'submit.slurm')})
    monkeypatch.setattr(worker, 'inspect_environment',
        lambda *args, **kwargs: pytest.fail('invalid array must fail before environment inspection'))
    monkeypatch.setattr(worker, 'capacity_preflight',
        lambda *args, **kwargs: pytest.fail('invalid array must fail before capacity queries'))
    with pytest.raises(ValueError):
        worker.dispatch(dict(config={'root': str(folder.parents[1])}, action='submit',
                             job_name=folder.name, phase='convergence'))
    assert not (folder / 'submissions' / 'convergence.json').exists()
    assert not (folder / 'remote-submission.json').exists()


def test_valid_array_preflight_checks_without_submitting(array_preflight, monkeypatch):
    folder, _ = array_preflight
    monkeypatch.setattr(worker, 'inspect_environment', lambda *args, **kwargs:
        {'environment_sha256': 'synthetic-env', 'executable_sha256': 'synthetic-vasp',
         'executable': '/synthetic/vasp_std'})
    capacity = {'capacity_checked': True}
    monkeypatch.setattr(worker, 'capacity_preflight', lambda _: capacity)
    result = worker.dispatch(dict(config={'root': str(folder.parents[1])},
        action='submission_preflight', job_name=folder.name, phase='convergence', limits=ARRAY_LIMITS))
    assert result == {'ready': True, 'capacity': capacity}
    assert not (folder / 'submissions' / 'convergence.json').exists()


@pytest.mark.parametrize('phase,stage_gates', [('scf', True), ('convergence', False)])
def test_array_submission_validator_requires_convergence_review_context(array_preflight, phase, stage_gates):
    folder, _ = array_preflight
    meta = json.loads((folder / 'remote-job.json').read_text())
    with pytest.raises(ValueError):
        worker.validate_array_submission(folder, phase, {'stage_gates': stage_gates}, meta)


@pytest.mark.parametrize('mode', [None, 'allocation'])
def test_submission_validator_preserves_legacy_allocation_without_array_engine(task, monkeypatch, mode):
    folder, _ = task
    config = json.loads((folder / 'config.json').read_text())
    if mode is not None:
        config['submission_mode'] = mode
    write_json(folder / 'config.json', config)
    monkeypatch.setattr(worker, 'engine_module',
        lambda *args: pytest.fail('allocation must not load the array engine'))
    worker.validate_array_submission(folder, None, {'stage_gates': False}, {})


@pytest.mark.parametrize('where', ['queue', 'accounting'])
def test_array_summary_blocks_unexpected_element_even_if_expected_elements_completed(where):
    receipt = dict(job_id=PARENT_ID, array_task_count=3, array_task_ids=[0, 1, 2])
    accounting = '\n'.join(f'{PARENT_ID}_{index}|COMPLETED|0:0|' for index in range(3))
    queue = ''
    if where == 'queue':
        queue = f'{PARENT_ID}_3|RUNNING|'
    else:
        accounting += f'\n{PARENT_ID}_3|COMPLETED|0:0|'
    summary = worker.array_scheduler_status(receipt, queue, accounting)
    assert summary['unexpected_task_ids'] == [3]
    assert not summary['all_terminal'] and not summary['all_successful']


def test_array_summary_blank_state_stays_incomplete_instead_of_crashing():
    receipt = dict(job_id=PARENT_ID, array_task_count=3, array_task_ids=[0, 1, 2])
    accounting = (f'{PARENT_ID}_0|COMPLETED|0:0|\n{PARENT_ID}_1||0:0|\n'
                  f'{PARENT_ID}_2|COMPLETED|0:0|\n')
    summary = worker.array_scheduler_status(receipt, '', accounting)
    assert not summary['all_terminal'] and not summary['all_successful']
    assert not summary['tasks'][1]['terminal']


def array_deletion_preview(task, monkeypatch, *, accounting, queue=''):
    folder, _ = task
    write_json(folder / 'remote-submission.json', dict(job_id=PARENT_ID,
        submission_mode='array', array_task_count=3, array_concurrency=2, array_task_ids=[0, 1, 2]))
    commands = []

    def run(command, **kwargs):
        commands.append(command)
        assert command[0] in ('squeue', 'sacct')
        return NS(returncode=0, stdout=queue if command[0] == 'squeue' else accounting, stderr='')

    monkeypatch.setattr(worker.subprocess, 'run', run)
    result = worker.deletion_preview(folder.parent, folder.name)
    return result, commands


@pytest.mark.parametrize('accounting', [f'{PARENT_ID}|COMPLETED|0:0|',
    f'{PARENT_ID}_0|COMPLETED|0:0|\n{PARENT_ID}_1|FAILED|1:0|'])
def test_array_deletion_preview_parent_only_or_partial_accounting_blocks(task, monkeypatch, accounting):
    folder, _ = task
    with pytest.raises(ValueError, match='全部数组元素'):
        array_deletion_preview(task, monkeypatch, accounting=accounting)
    assert folder.is_dir() and (folder / 'manifest.json').is_file()


def test_array_deletion_preview_all_terminal_children_allows_preview_without_deletion(task, monkeypatch):
    folder, _ = task
    accounting = (f'{PARENT_ID}_0|COMPLETED|0:0|\n{PARENT_ID}_1|FAILED|1:0|\n'
                  f'{PARENT_ID}_2|TIMEOUT|0:0|\n')
    preview, commands = array_deletion_preview(task, monkeypatch, accounting=accounting)
    assert preview['job_states'][PARENT_ID] == {f'{PARENT_ID}_0': 'COMPLETED',
        f'{PARENT_ID}_1': 'FAILED', f'{PARENT_ID}_2': 'TIMEOUT'}
    assert [command[0] for command in commands] == ['squeue', 'sacct']
    assert preview['queue_checked'] and preview['fingerprint']
    assert folder.is_dir() and (folder / 'manifest.json').is_file()


@pytest.mark.parametrize('job', [f'{PARENT_ID}_1', f'{PARENT_ID}_[0-2]'])
def test_array_deletion_preview_active_child_or_compressed_range_blocks(task, monkeypatch, job):
    folder, _ = task
    queue = f'{job}|{folder.parent / "unrelated-directory"}\n'
    accounting = '\n'.join(f'{PARENT_ID}_{index}|COMPLETED|0:0|' for index in range(3))
    with pytest.raises(ValueError, match='排队或运行'):
        array_deletion_preview(task, monkeypatch, accounting=accounting, queue=queue)
    assert folder.is_dir()


def test_array_status_child_log_tails_are_bounded_redacted_and_expected_only(task, monkeypatch):
    folder, modules = task
    modules['convergence_array'].finalize_array = lambda *args, **kwargs: pytest.fail('live array must not collect')
    fake_key = 'synthetic_private_value_do_not_use'
    (folder / f'slurm-{PARENT_ID}_0.log').write_text('BEGINNING_SHOULD_NOT_BE_READ\n'
        + 'x' * 3000 + f'\napi_key={fake_key}\nworker zero tail\n', encoding='utf-8')
    (folder / f'slurm-{PARENT_ID}_1.log').write_text(
        '\x1b[31mworker one\x1b[0m\nBearer synthetic_not_a_real_token\n', encoding='utf-8')
    (folder / f'slurm-{PARENT_ID}_99.log').write_text('unexpected log must not be read', encoding='utf-8')
    info, _ = array_status(task, monkeypatch, queue=f'{PARENT_ID}_0|RUNNING|00:01|node01')
    tails = info['array_log_tails']
    assert set(tails) == {f'{PARENT_ID}_0', f'{PARENT_ID}_1'}
    assert all(len(tail) <= 2000 for tail in tails.values())
    assert fake_key not in tails[f'{PARENT_ID}_0'] and '[REDACTED]' in tails[f'{PARENT_ID}_0']
    assert 'BEGINNING_SHOULD_NOT_BE_READ' not in tails[f'{PARENT_ID}_0']
    assert 'synthetic_not_a_real_token' not in tails[f'{PARENT_ID}_1']
    assert '\x1b' not in tails[f'{PARENT_ID}_1']


def test_late_array_timeout_revalidates_accepted_convergence_with_full_scheduler_evidence(task, monkeypatch):
    folder, modules = task
    write_json(folder / 'checkpoint.json', {'next': 'relaxation', 'completed': ['convergence']})
    write_json(folder / 'workflow-state.json', {'status': 'awaiting_review', 'next': 'relaxation'})
    collections = []
    report = {'status': 'failed', 'failed_tasks': [1], 'error': 'late scheduler timeout'}

    def finalize(root, parent, **kwargs):
        collections.append((root, parent, kwargs))
        write_json(root / 'convergence-array.json', report)
        write_json(root / 'workflow-state.json', {'status': 'failed', 'stage': 'convergence'})
        write_json(root / 'checkpoint.json', {'next': 'convergence', 'completed': [], 'array_failed': True})
        return report

    modules['convergence_array'].finalize_array = finalize
    accounting = (f'{PARENT_ID}_0|COMPLETED|0:0|\n{PARENT_ID}_1|TIMEOUT|0:0|\n'
                  f'{PARENT_ID}_2|COMPLETED|0:0|\n')
    info, _ = array_status(task, monkeypatch, accounting=accounting)
    assert len(collections) == 1
    root, parent, kwargs = collections[0]
    assert root == folder and parent == PARENT_ID and kwargs['scheduler_terminal']
    assert kwargs['scheduler_evidence'] is info['array_tasks']
    assert kwargs['scheduler_evidence']['tasks'][1]['state'] == 'TIMEOUT'
    assert kwargs['scheduler_evidence']['tasks'][1]['exit_code'] == '0:0'
    assert kwargs['scheduler_evidence']['all_terminal'] and not kwargs['scheduler_evidence']['all_successful']
    assert info['array_collection'] == report and info['convergence-array.json'] == report
    assert info['checkpoint.json']['next'] == 'convergence' and info['workflow-state.json']['status'] == 'failed'


@pytest.mark.parametrize('old_engine', ['version', 'flat_release'])
def test_submission_preflight_refuses_old_bound_array_engine(array_preflight, monkeypatch, old_engine):
    folder, modules = array_preflight
    if old_engine == 'version':
        modules['convergence_array'].ARRAY_EXECUTION_VERSION = 0
    else:
        monkeypatch.setattr(worker, '__package__', '')
    monkeypatch.setattr(worker, 'inspect_environment',
        lambda *args, **kwargs: pytest.fail('old array engine must fail before environment inspection'))
    with pytest.raises(ValueError, match='引擎不支持数组'):
        worker.dispatch(dict(config={'root': str(folder.parents[1])}, action='submission_preflight',
                             job_name=folder.name, phase='convergence', limits=ARRAY_LIMITS))
    assert not (folder / 'submissions' / 'convergence.json').exists()


@pytest.mark.parametrize('case', ['blank_state', 'extra_element'])
def test_status_inconsistent_array_accounting_never_reaches_collector(task, monkeypatch, case):
    _, modules = task
    modules['convergence_array'].finalize_array = lambda *args, **kwargs: pytest.fail('inconsistent array accounting must not collect')
    rows = [f'{PARENT_ID}_{index}|COMPLETED|0:0|' for index in range(3)]
    if case == 'blank_state':
        rows[1] = f'{PARENT_ID}_1||0:0|'
    else:
        rows.append(f'{PARENT_ID}_3|COMPLETED|0:0|')
    info, _ = array_status(task, monkeypatch, accounting='\n'.join(rows))
    assert not info['array_tasks']['all_terminal'] and not info['array_tasks']['all_successful']
    assert 'array_collection' not in info
