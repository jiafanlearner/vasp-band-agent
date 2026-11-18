import json
from types import SimpleNamespace as NS

import pytest

from vasp_agent.remote import remote_worker as worker
from vasp_agent.monitoring.polling import poll_job


def task(tmp_path, phase='convergence', array=False):
    folder = tmp_path / 'runs' / 'si'
    (folder / 'submissions').mkdir(parents=True)
    receipt = dict(status='submitted', job_id='123', phase=phase)
    if array:
        receipt.update(submission_mode='array', array_task_count=3, array_task_ids=[0, 1, 2])
    (folder / 'submissions' / (phase + '.json')).write_text(json.dumps(receipt))
    (folder / 'workflow-state.json').write_text('{"status":"awaiting_review","next":"relaxation"}')
    (folder / 'checkpoint.json').write_text('{"next":"relaxation","completed":["convergence"]}')
    return folder


def scheduler(monkeypatch, queue='', accounting='', queue_code=0, account_code=0):
    calls = []
    def run(command, **kwargs):
        calls.append(command)
        assert command[0] in ('squeue', 'sacct')
        assert kwargs['timeout'] == 20
        return NS(stdout=queue if command[0] == 'squeue' else accounting,
                  returncode=queue_code if command[0] == 'squeue' else account_code,
                  stderr='private data not returned')
    monkeypatch.setattr(worker.subprocess, 'run', run)
    return calls


@pytest.mark.parametrize('state', ['PENDING', 'RUNNING', 'COMPLETING'])
def test_active_exact_id_and_no_mutations(tmp_path, monkeypatch, state):
    folder = task(tmp_path)
    calls = scheduler(monkeypatch, queue=f'123|{state}|00:02|Priority\n999|COMPLETED|00:01|None')
    before = {p: p.read_bytes() for p in folder.rglob('*') if p.is_file()}
    result = worker.monitor_status(folder, '123', 'convergence')
    assert result['state'] == state and not result['terminal']
    assert len(calls) == 1 and all(p.read_bytes() == data for p, data in before.items())


@pytest.mark.parametrize('state,code,outcome', [
    ('COMPLETED', '0:0', 'completed'), ('COMPLETED', '1:0', 'failed'),
    ('FAILED', '1:0', 'failed'), ('CANCELLED by 42', '0:15', 'cancelled'),
    ('TIMEOUT', '0:0', 'failed'), ('OUT_OF_MEMORY', '0:9', 'failed'),
])
def test_accounting_fallback_handles_old_id_and_exit_status(tmp_path, monkeypatch, state, code, outcome):
    folder = task(tmp_path)
    scheduler(monkeypatch, accounting=f'123|{state}|{code}|\n123.batch|FAILED|1:0|', queue_code=1)
    result = worker.monitor_status(folder, '123', 'convergence')
    assert result['terminal'] and result['outcome'] == outcome
    assert result['workflow_status'] == 'awaiting_review' and result['next_phase'] == 'relaxation'


@pytest.mark.parametrize('accounting', ['', '999|COMPLETED|0:0|', '123.batch|COMPLETED|0:0|',
                                        '123|COMPLETED||', '123|COMPLETED|0:0|\n123|RUNNING|0:0|'])
def test_missing_or_conflicting_evidence_not_completion(tmp_path, monkeypatch, accounting):
    folder = task(tmp_path)
    scheduler(monkeypatch, accounting=accounting)
    result = worker.monitor_status(folder, '123', 'convergence')
    assert result['state'] == 'UNKNOWN' and not result['terminal']


def test_query_failures_raise_for_backoff_and_id_mismatch_does_not_query(tmp_path, monkeypatch):
    folder = task(tmp_path)
    calls = scheduler(monkeypatch, queue_code=1, account_code=1)
    with pytest.raises(worker.SubmissionError) as exc:
        worker.monitor_status(folder, '123', 'convergence')
    assert exc.value.error_code == 'MONITOR_QUERY_FAILED'
    calls.clear()
    with pytest.raises(ValueError, match='回执不符'):
        worker.monitor_status(folder, '456', 'convergence')
    assert calls == []


@pytest.mark.parametrize('count,failed,expected', [(0, False, False), (2, False, False), (3, False, True), (3, True, True)])
def test_array_requires_all_children_not_parent(tmp_path, monkeypatch, count, failed, expected):
    folder = task(tmp_path, array=True)
    rows = ['123|COMPLETED|0:0|'] + [f'123_{i}|{"FAILED" if failed and i == 1 else "COMPLETED"}|{"1:0" if failed and i == 1 else "0:0"}|' for i in range(count)]
    scheduler(monkeypatch, accounting='\n'.join(rows))
    result = worker.monitor_status(folder, '123', 'convergence')
    assert result['terminal'] is expected
    if expected:
        assert result['outcome'] == ('failed' if failed else 'completed')
    assert result['array_progress']['total'] == 3


def test_local_and_remote_poll_pin_recorded_stage_not_latest(tmp_path, monkeypatch):
    task(tmp_path)
    target = dict(server='local', job_name='si', phase='convergence', job_id='123')
    scheduler(monkeypatch, accounting='123|COMPLETED|0:0|')
    assert poll_job(tmp_path, target)['terminal']
    from vasp_agent.remote.remote_compute import RemoteCompute
    monkeypatch.setattr(RemoteCompute, '__init__', lambda *a: None)
    calls = []
    monkeypatch.setattr(RemoteCompute, 'call', lambda self, action, job, **params:
                        calls.append((action, job, params)) or {'state': 'RUNNING', 'terminal': False})
    poll_job(tmp_path, {**target, 'server': 'hpc'})
    assert calls == [('monitor_status', 'si', {'job_id': '123', 'phase': 'convergence'})]


def test_stdin_rpc_dispatch_monitor_without_engine_or_science_changes(tmp_path, monkeypatch):
    root = tmp_path / 'remote'
    folder = root / 'jobs' / 'si'
    folder.mkdir(parents=True)
    (folder / 'remote-submission.json').write_text('{"status":"submitted","job_id":"123"}')
    scheduler(monkeypatch, accounting='123|COMPLETED|0:0|')
    before = sorted(p.relative_to(root) for p in root.rglob('*'))
    result = worker.dispatch(dict(config={'root': str(root)}, action='monitor_status', job_name='si', job_id='123', phase=None))
    assert result['terminal']
    assert sorted(p.relative_to(root) for p in root.rglob('*')) == before


def test_monitor_transport_is_quiet_bounded_and_does_not_submit(tmp_path, monkeypatch):
    from vasp_agent.remote import remote_compute as remote
    rpc = remote.RemoteCompute.__new__(remote.RemoteCompute)
    rpc.config = {'python': '/usr/bin/python3'}
    rpc.server = 'hpc'
    rpc.servers = NS(ssh_args=lambda _: ['ssh', 'hpc'])
    calls = []
    def run(command, **kwargs):
        calls.append((command, kwargs))
        return NS(returncode=0, stdout=json.dumps({'ok': True, 'result': {'state': 'PENDING', 'terminal': False}}), stderr='')
    monkeypatch.setattr(remote, 'run_ssh', lambda *a, **kw: pytest.fail('monitor must not print transport retries'))
    monkeypatch.setattr(remote.subprocess, 'run', run)
    result = rpc.call('monitor_status', 'si', job_id='123', phase='convergence')
    assert result['state'] == 'PENDING' and len(calls) == 1
    assert calls[0][1]['timeout'] == 60
    assert 'monitor_status' in calls[0][1]['input'] and "'123'" not in ' '.join(calls[0][0])
