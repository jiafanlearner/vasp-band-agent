import json
from types import SimpleNamespace as NS

import pytest
import vasp_agent.remote.remote_worker as worker
from test_extra_commands import make


def task(base, name='si'):
    folder = base / name
    folder.mkdir(parents=True)
    (folder / 'manifest.json').write_text('{"workflow":"full"}')
    (folder / 'workflow-state.json').write_text('{"status":"prepared"}')
    return folder


@pytest.fixture(autouse=True)
def no_scheduler(monkeypatch):
    monkeypatch.setattr(worker.shutil, 'which', lambda name: None)


def test_archive_restore_preserves_files(tmp_path):
    folder = task(tmp_path)
    original = (folder / 'manifest.json').read_bytes()
    worker.manage_task(tmp_path, 'si', 'archive')
    assert worker.list_workflow_jobs(tmp_path)['jobs'] == []
    assert worker.list_workflow_jobs(tmp_path, True)['jobs'][0]['archived']
    worker.manage_task(tmp_path, 'si', 'restore')
    assert len(worker.list_workflow_jobs(tmp_path)['jobs']) == 1
    assert (folder / 'manifest.json').read_bytes() == original


def test_delete_requires_matching_confirmation_and_unchanged_preview(tmp_path):
    folder = task(tmp_path)
    sibling = task(tmp_path, 'keep')
    preview = worker.deletion_preview(tmp_path, 'si', False)
    with pytest.raises(ValueError):
        worker.manage_task(tmp_path, 'si', 'delete', confirmation='yes', fingerprint=preview['fingerprint'], require_scheduler=False)
    (folder / 'new.txt').write_text('changed')
    with pytest.raises(ValueError, match='变化'):
        worker.manage_task(tmp_path, 'si', 'delete', confirmation='si', fingerprint=preview['fingerprint'], require_scheduler=False)
    preview = worker.deletion_preview(tmp_path, 'si', False)
    worker.manage_task(tmp_path, 'si', 'delete', confirmation='si', fingerprint=preview['fingerprint'], require_scheduler=False)
    assert not folder.exists() and sibling.exists()


def test_lock_and_uncertain_submission_block(tmp_path):
    folder = task(tmp_path)
    (folder / '.agent.lock').touch()
    with pytest.raises(ValueError, match='运行锁'):
        worker.deletion_preview(tmp_path, 'si', False)
    (folder / '.agent.lock').unlink()
    (folder / 'submission.json').write_text('{"job_id":null}')
    with pytest.raises(ValueError, match='不明确'):
        worker.deletion_preview(tmp_path, 'si', False)
    with pytest.raises(ValueError):
        worker.task_target(tmp_path, '../si')


def test_queue_and_accounting_checks(tmp_path, monkeypatch):
    folder = task(tmp_path)
    (folder / 'submission.json').write_text('{"job_id":"123"}')
    monkeypatch.setattr(worker.subprocess, 'run', lambda *a, **kw: NS(stdout=f'123|{folder}\n'))
    with pytest.raises(ValueError, match='排队或运行'):
        worker.deletion_preview(tmp_path, 'si')
    def run(args, **kwargs):
        return NS(stdout='' if args[0] == 'squeue' else '123|RUNNING|\n')
    monkeypatch.setattr(worker.subprocess, 'run', run)
    with pytest.raises(ValueError, match='已经终止'):
        worker.deletion_preview(tmp_path, 'si')
    monkeypatch.setattr(worker.subprocess, 'run', lambda args, **kw: NS(stdout='' if args[0] == 'squeue' else '123|COMPLETED|\n'))
    assert worker.deletion_preview(tmp_path, 'si')['job_states'] == {'123': 'COMPLETED'}


def test_menu_archive_restore_and_delete_cancel(tmp_path):
    c, output = make(tmp_path)
    folder = task(tmp_path / 'runs')
    answers = iter(['1', '1', '1'])
    c.reader = lambda prompt: next(answers)
    c.handle('/archive')
    assert (folder / '.task-archived.json').exists()
    assert c.agent.tools.list_files()['jobs'] == []
    c.handle('/restore task local si')
    assert not (folder / '.task-archived.json').exists()
    c.reader = lambda prompt: 'yes'
    c.handle('/delete local si')
    assert folder.exists()
    assert any(str(folder) in line.replace('\\\\', '\\') for line in output)
    c.reader = lambda prompt: 'si'
    c.handle('/delete local si')
    assert not folder.exists()


def test_remote_delete_carries_preview_and_operator_confirmation(tmp_path, monkeypatch):
    import vasp_agent.remote.remote_compute as remote_compute
    c, output = make(tmp_path)
    calls = []
    class Remote:
        def __init__(self, *args):
            pass
        def call(self, action, name, **kwargs):
            calls.append((action, name, kwargs))
            return {'path': '/jobs/si', 'fingerprint': 'snapshot'}
    monkeypatch.setattr(remote_compute, 'RemoteCompute', Remote)
    c.reader = lambda prompt: 'si'
    c.handle('/delete server si')
    assert calls[-1] == ('manage_task', 'si', {'operation': 'delete', 'confirmation': 'si', 'fingerprint': 'snapshot'})
