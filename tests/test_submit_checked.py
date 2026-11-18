"""Preflight and sbatch in one worker call; fake checks and scheduler only."""
import pytest

import vasp_agent.remote.remote_worker as worker


def wire(monkeypatch, checks):
    sbatch = []
    monkeypatch.setattr(worker, '_submit_checks', checks)
    monkeypatch.setattr(worker, 'submit_with_receipt', lambda *a: sbatch.append(a) or {'status': 'submitted', 'job_id': '7'})
    return sbatch


def test_refusal_before_sbatch_is_explicit(monkeypatch, tmp_path):
    def refuse(*a):
        raise ValueError('阶段审阅未通过')
    sbatch = wire(monkeypatch, refuse)
    result = worker._submit_checked({'phase': 'scf'}, {}, tmp_path)
    assert result['submission_not_attempted'] and '审阅' in result['error'] and sbatch == []


def test_existing_receipt_is_reported_not_resubmitted(monkeypatch, tmp_path):
    sbatch = wire(monkeypatch, lambda *a: {'status': 'submitted', 'job_id': '5'})
    result = worker._submit_checked({'phase': 'scf'}, {}, tmp_path)
    assert result['existing_record'] and result['job_id'] == '5' and sbatch == []


def test_passing_checks_submit_once_and_sbatch_errors_stay_errors(monkeypatch, tmp_path):
    ready = lambda *a: {'ready': True, 'capacity': {'ok': True}, '_submit': (tmp_path, tmp_path / 'r.json', 'scf', True)}
    sbatch = wire(monkeypatch, ready)
    assert worker._submit_checked({'phase': 'scf'}, {}, tmp_path)['job_id'] == '7' and len(sbatch) == 1
    monkeypatch.setattr(worker, 'submit_with_receipt', lambda *a: (_ for _ in ()).throw(worker.SubmissionError('SUBMISSION_UNCERTAIN', 'x')))
    with pytest.raises(worker.SubmissionError):
        worker._submit_checked({'phase': 'scf'}, {}, tmp_path)


def test_preflight_action_keeps_its_old_shape(monkeypatch, tmp_path):
    wire(monkeypatch, lambda *a: {'ready': True, 'capacity': {}, '_submit': ()})
    assert worker._submission_preflight({'phase': 'scf'}, {}, tmp_path) == {'ready': True, 'capacity': {}}
    assert worker.TASK_ACTIONS['submit_checked'] is worker._submit_checked
    assert 'submit_checked' in worker.MEMO_ACTIONS


def test_attempt_returned_only_on_explicit_refusal(tmp_path, monkeypatch):
    from vasp_agent.chat import ai_agent as ai
    from vasp_agent.remote.remote_compute import RemoteCompute
    import vasp_agent.permissions.submission_dialog as dialog
    box = ai.Toolbox(tmp_path)
    box.allow_submit, box.submission_target = True, ('hpc', 'si', 'scf')
    box.submission_limits = dict(server='hpc', partition='p', nodes=1, cores=8, walltime='01:00:00')
    box.max_submissions, box.submissions = 1, 0
    monkeypatch.setattr(RemoteCompute, '__init__', lambda *a: None)
    monkeypatch.setattr(dialog, 'confirm', lambda *a, **kw: None)
    answers = iter([{'submission_not_attempted': True, 'error': '容量不足'}])
    monkeypatch.setattr(RemoteCompute, 'call', lambda self, action, job, **kw: next(answers))
    with pytest.raises(ValueError, match='容量不足'):
        box.remote_submit('hpc', 'si', 'scf')
    assert box.submissions == 0 and box.allow_submit
    def lost(self, action, job, **kw):
        if action == 'submit_checked':
            raise TimeoutError('lost')
        return {}
    monkeypatch.setattr(RemoteCompute, 'call', lost)
    with pytest.raises(TimeoutError):
        box.remote_submit('hpc', 'si', 'scf')
    assert box.submissions == 1 and not box.allow_submit


RES = dict(parallel_tests=1, cores_per_test=32, nodes=1, partition='p', walltime='01:00:00')


@pytest.mark.parametrize('configured, reason', [
    ({'changed': True, 'resources': RES}, 'needs_review'),
    ({'changed': False, 'resources': {**RES, 'cores_per_test': 8}}, 'resource_mismatch'),
    ({'changed': False, 'resources': RES, 'already_registered': True}, 'resource_mismatch'),
])
def test_worker_writes_but_does_not_submit_when_review_or_target_says_no(monkeypatch, tmp_path, configured, reason):
    monkeypatch.setattr(worker, '_resource_task', lambda folder: None)
    monkeypatch.setattr(worker, 'configure_resources', lambda *a: {'signature': 's', **configured})
    monkeypatch.setattr(worker, '_submit_task', lambda *a, **k: pytest.fail('must not submit'))
    reply = worker._configure_and_submit({'phase': 'scf', 'resources': RES, 'signature': 's', 'limits': {},
                                          'submit_limits': {}}, {}, tmp_path)
    assert reply['submission'] is None and reply['not_submitted'] == reason


def test_worker_submits_after_unchanged_write(monkeypatch, tmp_path):
    monkeypatch.setattr(worker, '_resource_task', lambda folder: None)
    monkeypatch.setattr(worker, 'configure_resources', lambda *a: {'signature': 's', 'changed': False, 'resources': RES})
    seen = []
    monkeypatch.setattr(worker, '_submit_task', lambda payload, cfg, folder, **k: seen.append((payload, k)) or {'job_id': '9', 'status': 'submitted'})
    reply = worker._configure_and_submit({'phase': 'scf', 'resources': RES, 'signature': 's', 'limits': {},
                                          'submit_limits': {'cores': 32}}, {}, tmp_path)
    assert reply['submission']['job_id'] == '9' and reply['signature'] == 's'
    assert seen == [({'phase': 'scf', 'limits': {'cores': 32}}, {'preflight_only': False, 'report_refusal': True})]


def menu_box(tmp_path, monkeypatch, reply):
    from vasp_agent.chat import ai_agent as ai
    from vasp_agent.remote.remote_compute import RemoteCompute
    from vasp_agent.permissions import resource_menu
    monkeypatch.setattr(RemoteCompute, '__init__', lambda *a: None)
    monkeypatch.setattr(resource_menu, 'resource_menu', lambda ws, server, phase, defaults, *a, **k: defaults.copy())
    calls = []
    def rpc(self, action, job, **kw):
        calls.append(action)
        if action == 'resource_context':
            return {'signature': 's', 'resources': RES, 'already_registered': len(calls) > 2 and reply == 'lost'}
        if action == 'configure_and_submit':
            if reply == 'lost':
                raise TimeoutError('response lost')
            return reply
        assert action == 'submit_checked'
        return {'status': 'submitted', 'job_id': '11'}
    monkeypatch.setattr(RemoteCompute, 'call', rpc)
    box = ai.Toolbox(tmp_path)
    box.plan_reader, box.plan_output = (lambda p: '1'), (lambda _: None)
    return box, calls


def test_refusal_after_write_keeps_bookkeeping_and_returns_attempt(tmp_path, monkeypatch):
    box, calls = menu_box(tmp_path, monkeypatch, {'signature': 's', 'changed': False, 'resources': RES,
        'submission': {'submission_not_attempted': True, 'error': '容量不足'}})
    with pytest.raises(ValueError, match='容量不足'):
        box.request_submission('hpc', 'la', 'scf', RES)
    key = ('hpc', 'la', 'scf')
    assert box.submissions == 0 and box.allow_submit and box.resource_confirmations[key] == 's'
    assert calls == ['resource_context', 'configure_and_submit']


def test_lost_response_recovers_existing_receipt_read_only(tmp_path, monkeypatch):
    box, calls = menu_box(tmp_path, monkeypatch, 'lost')
    result = box.request_submission('hpc', 'la', 'scf', RES)
    assert result['record_recovered_after_error'] and box.submissions == 1
    assert calls == ['resource_context', 'configure_and_submit', 'resource_context']


def test_mismatch_falls_back_to_a_separate_submit(tmp_path, monkeypatch):
    box, calls = menu_box(tmp_path, monkeypatch, {'signature': 's', 'changed': False, 'resources': RES,
        'submission': None, 'not_submitted': 'resource_mismatch'})
    assert box.request_submission('hpc', 'la', 'scf')['job_id'] == '11'
    assert calls == ['resource_context', 'configure_and_submit', 'submit_checked'] and box.submissions == 1
