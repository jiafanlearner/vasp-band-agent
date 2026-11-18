import json
from types import SimpleNamespace as NS
import pytest
from vasp_agent.remote import remote_worker as w
# recovery_context looks the accounting readers up in its own unit.
from vasp_agent.remote import worker_receipts as receipts


@pytest.fixture
def target(tmp_path, monkeypatch):
    (tmp_path / 'submissions').mkdir()
    (tmp_path / 'submissions/convergence.json').write_text('{"status":"submission_uncertain","job_id":null}')
    (tmp_path / 'workflow-state.json').write_text('{"status":"prepared","next":"convergence"}')
    (tmp_path / 'review-convergence.json').write_text('{"old":true}')
    row = {'job_id': '123', 'state': 'FAILED', 'allocated_nodes': '0', 'elapsed': '00:00:00'}
    def account(folder, phase, job_id=None):
        return {'target_matched': True, 'accounting': [row], 'discovered_job_id': '123', 'search_window': {}}
    monkeypatch.setattr(receipts, 'submission_accounting', account)
    monkeypatch.setattr(receipts, 'submission_command_evidence', lambda *a: {'submission_phase_verified': True})
    monkeypatch.setattr(w.subprocess, 'run', lambda *a, **k: NS(returncode=0, stdout=''))
    return tmp_path, row


def recover(folder):
    context = w.recovery_context(folder, 'convergence', '123')
    return w.recover_submission(folder, 'convergence', '123', context['snapshot'], 'a'*32, True)


def test_archive_original_and_barrier_requires_new_review(target):
    folder, _ = target
    original = (folder / 'submissions/convergence.json').read_bytes()
    result = recover(folder)
    assert result['status'] == 'recovered' and not result['submission_authorized']
    assert not result['attempt_binding_verified']
    assert not (folder / 'submissions/convergence.json').exists()
    assert (folder / ('submission-recoveries/'+'a'*32+'.original.json')).read_bytes() == original
    w.recovery_barrier(folder, 'convergence')
    with pytest.raises(ValueError, match='重新审阅'):
        w.recovery_barrier(folder, 'convergence', True)
    (folder / 'review-convergence.json').write_text('{"new":true}')
    w.recovery_barrier(folder, 'convergence', True)
    again = w.recover_submission(folder, 'convergence', '123', result['snapshot'], 'a'*32, True)
    assert again == result


@pytest.mark.parametrize('case', ['state', 'nodes', 'elapsed', 'queue', 'query_error', 'ambiguous', 'phase', 'other_receipt', 'running'])
def test_recovery_blocks_uncertain_or_active_evidence(target, monkeypatch, case):
    folder, row = target
    before = (folder / 'submissions/convergence.json').read_bytes()
    if case == 'state': row['state'] = 'RUNNING'
    if case == 'nodes': row['allocated_nodes'] = '1'
    if case == 'elapsed': row['elapsed'] = '00:01:00'
    if case in ('queue', 'query_error'):
        monkeypatch.setattr(w.subprocess, 'run', lambda *a, **k: NS(returncode=1 if case == 'query_error' else 0, stdout=f'123|va-{folder.name}|{folder}'))
    if case == 'ambiguous': monkeypatch.setattr(receipts, 'submission_accounting', lambda *a: {'target_matched': False})
    if case == 'phase': monkeypatch.setattr(receipts, 'submission_command_evidence', lambda *a: {'submission_phase_verified': False})
    if case == 'other_receipt': (folder / 'submissions/scf.json').write_text('{}')
    if case == 'running': (folder / '.agent.lock').write_text('locked')
    with pytest.raises(ValueError): recover(folder)
    assert (folder / 'submissions/convergence.json').read_bytes() == before


def test_conflict_and_missing_confirmation_preserve_original(target):
    folder, _ = target
    context = w.recovery_context(folder, 'convergence', '123')
    with pytest.raises(ValueError):
        w.recover_submission(folder, 'convergence', '123', context['snapshot'], 'a'*32, False)
    (folder / 'config.json').write_text('{}')
    with pytest.raises(ValueError, match='变化'):
        w.recover_submission(folder, 'convergence', '123', context['snapshot'], 'a'*32, True)
    assert (folder / 'submissions/convergence.json').exists()


def test_interrupted_archive_blocks_and_same_operation_resumes(target, monkeypatch):
    folder, _ = target
    context = w.recovery_context(folder, 'convergence', '123')
    original_replace = w.os.replace
    monkeypatch.setattr(w.os, 'replace', lambda *a: (_ for _ in ()).throw(OSError('interrupted')))
    with pytest.raises(OSError):
        w.recover_submission(folder, 'convergence', '123', context['snapshot'], 'a'*32, True)
    with pytest.raises(ValueError, match='未完成'):
        w.recovery_barrier(folder, 'convergence')
    monkeypatch.setattr(w.os, 'replace', original_replace)
    assert w.recover_submission(folder, 'convergence', '123', context['snapshot'], 'a'*32, True)['status'] == 'recovered'


def test_stale_lock_file_does_not_block_recovery(target):
    """A lock file with nobody holding it must be inert.

    The lock used to be a path created with O_EXCL and removed only on the way out,
    so a worker killed between claiming it and writing the receipt left a file that
    blocked every later submit -- and recover_submission took the same lock, so the
    one documented route out of a bad submission refused too. The job was wedged
    permanently, with no receipt and no way forward. Nothing in the operator-facing
    layers even mentioned the file's existence.
    """
    folder, _ = target
    lock = folder / '.submission-recovery.lock'
    lock.write_text('left behind by a killed worker')
    assert not w.lock_is_held(lock)
    w.recovery_barrier(folder, 'convergence')  # barrier must not treat it as active
    assert recover(folder)['status'] == 'recovered'
    assert not w.lock_is_held(lock)


def test_held_lock_blocks_recovery(target):
    """The lock must still do its job: an actually-held lock refuses, immediately."""
    folder, _ = target
    holder = w.submission_lock(folder, 'busy')
    try:
        assert w.lock_is_held(folder / '.submission-recovery.lock')
        context = w.recovery_context(folder, 'convergence', '123')
        with pytest.raises(ValueError, match='另一恢复正在进行'):
            w.recover_submission(folder, 'convergence', '123', context['snapshot'], 'a'*32, True)
        with pytest.raises(ValueError, match='恢复进行中'):
            w.recovery_barrier(folder, 'convergence')
    finally:
        holder.close()
    # Released by closing, not by unlinking the file -- two processes locking two
    # different inodes of the same path would both believe they were exclusive.
    assert not w.lock_is_held(folder / '.submission-recovery.lock')
    assert recover(folder)['status'] == 'recovered'


@pytest.mark.parametrize('answer', ['', 'yes', '确认对账'])
def test_operator_menu_only_source_of_confirmation(tmp_path, monkeypatch, answer):
    from vasp_agent.chat.ai_agent import Toolbox
    from vasp_agent.remote.remote_compute import RemoteCompute
    monkeypatch.setattr(RemoteCompute, '__init__', lambda *a: None)
    calls = []
    def call(self, action, job, **kw):
        calls.append((action, kw))
        return {'snapshot': 'hash'} if action == 'recovery_context' else {'status': 'recovered'}
    monkeypatch.setattr(RemoteCompute, 'call', call)
    box = Toolbox(tmp_path)
    box.plan_reader = lambda _: answer
    box.plan_output = lambda _: None
    result = box.request_submission_recovery('test', 'si', 'convergence', '123')
    assert len(calls) == (2 if answer == '确认对账' else 1)
    assert not box.allow_submit
    assert result['status'] == ('recovered' if answer == '确认对账' else 'cancelled')
