import json
import subprocess
from types import SimpleNamespace as NS

import pytest
import vasp_agent.remote.remote_worker as worker


@pytest.fixture
def task(tmp_path):
    (tmp_path / 'submissions').mkdir()
    (tmp_path / 'submissions/convergence.json').write_text(json.dumps({'job_id': '69800234'}))
    return tmp_path


def test_expired_queue_id_reaches_accounting(task, monkeypatch):
    calls = []
    def run(args, **kwargs):
        calls.append(args)
        if args[0] == 'squeue':
            if '-j' in args:
                raise subprocess.CalledProcessError(1, args, stderr='Invalid job id specified')
            return NS(stdout='999999\n')
        return NS(stdout='69800234|COMPLETED|0:0|\n')
    monkeypatch.setattr(worker.subprocess, 'run', run)
    worker.check_previous_job(task, 'relaxation')
    assert calls[0] == ['squeue', '--me', '-h', '-o', '%i']
    assert calls[1][0] == 'sacct'


def test_still_queued_never_passes(task, monkeypatch):
    monkeypatch.setattr(worker.subprocess, 'run', lambda args, **kw: NS(stdout='69800234\n'))
    with pytest.raises(ValueError, match='尚未退出'):
        worker.check_previous_job(task, 'relaxation')


@pytest.mark.parametrize('rows', ['', '69800234|FAILED|1:0|', '69800234.batch|COMPLETED|0:0|', '69800234|COMPLETED|1:0|'])
def test_missing_or_unsuccessful_parent_blocks(task, monkeypatch, rows):
    monkeypatch.setattr(worker.subprocess, 'run', lambda args, **kw: NS(stdout='' if args[0] == 'squeue' else rows))
    with pytest.raises(ValueError, match='正常退出'):
        worker.check_previous_job(task, 'relaxation')


@pytest.mark.parametrize('fail_command', ['squeue', 'sacct'])
def test_scheduler_failure_keeps_diagnostics(task, monkeypatch, fail_command):
    def run(args, **kwargs):
        if args[0] == fail_command:
            raise subprocess.CalledProcessError(1, args, stderr='controller unavailable')
        return NS(stdout='')
    monkeypatch.setattr(worker.subprocess, 'run', run)
    with pytest.raises(ValueError, match='controller unavailable'):
        worker.check_previous_job(task, 'relaxation')
    assert not (task / 'submissions/relaxation.json').exists()
