"""Remote cancel and release of a cancelled phase receipt, with a fake scheduler only."""
import json
from types import SimpleNamespace as NS

import pytest

import vasp_agent.remote.remote_worker as worker
from vasp_agent.remote.remote_compute import RemoteCompute
from test_submission_permissions import commands

JOB = '71440093'
SLURM = '#SBATCH --partition=compute\n#SBATCH --time=04:00:00\n#SBATCH --nodes=1\n#SBATCH --ntasks=8\nexport OMP_NUM_THREADS=1\nsrun vasp_std\n'


@pytest.fixture
def task(tmp_path):
    folder = tmp_path / 'remote' / 'La2CuO4'
    folder.mkdir(parents=True)
    (folder / 'manifest.json').write_text(json.dumps({'stage_gates': True, 'hashes': {
        'relax-1/INCAR': 'x', 'relax-1/KPOINTS': 'x', 'scf/INCAR': 'x'}}))
    (folder / 'input-plan.json').write_text(json.dumps({'incars': {
        'convergence/encut-1': {}, 'relax-1': {}, 'relax-2': {}, 'validation/reference': {}, 'scf': {}, 'bands': {}}}))
    (folder / 'checkpoint.json').write_text(json.dumps({'completed': ['convergence'], 'next': 'relaxation'}))
    (folder / 'workflow-state.json').write_text(json.dumps({'status': 'awaiting_review', 'next': 'relaxation'}))
    (folder / 'config.json').write_text(json.dumps({'parallel_tests': 1, 'cores_per_test': 8}))
    (folder / 'submit.slurm').write_text(SLURM)
    (folder / 'remote-job.json').write_text(json.dumps({'release': str(tmp_path / 'release'),
                                                        'script_sha256': worker.sha(folder / 'submit.slurm')}))
    (folder / 'relax-1').mkdir()
    (folder / 'relax-1/INCAR').write_text('prepared')
    (folder / 'relax-1/KPOINTS').write_text('prepared')
    (folder / 'submissions').mkdir()
    (folder / 'submissions/convergence.json').write_text(json.dumps({'status': 'submitted', 'job_id': '70000001'}))
    (folder / 'submissions/relaxation.json').write_text(json.dumps(
        {'status': 'submitted', 'job_id': JOB, 'phase': 'relaxation'}))
    return folder


def scheduler(monkeypatch, queue='', state='CANCELLED by 1234'):
    calls = []
    def run(args, **kwargs):
        calls.append(args)
        if args[0] == 'scancel':
            return NS(returncode=0, stdout='', stderr='')
        if args[0] == 'squeue':
            return NS(returncode=0, stdout=queue, stderr='')
        assert args[0] == 'sacct'
        return NS(returncode=0, stdout=f'{JOB}|{state}|0:0\n{JOB}.batch|CANCELLED|0:15\n', stderr='')
    monkeypatch.setattr(worker.subprocess, 'run', run)
    return calls


def events(folder):
    path = folder / 'events.jsonl'
    return [json.loads(line)['action'] for line in path.read_text().splitlines()] if path.exists() else []


def test_cancel_previews_without_scancel_and_requires_matching_id(task, monkeypatch):
    calls = scheduler(monkeypatch)
    preview = worker.cancel_job(task, 'relaxation')
    assert preview['job_id'] == JOB and preview['preview'] and calls == []
    with pytest.raises(ValueError, match='不一致'):
        worker.cancel_job(task, 'relaxation', '1')
    assert calls == []
    result = worker.cancel_job(task, None, JOB)
    assert result['cancel_requested'] and result['phase'] == 'relaxation'
    assert calls == [['scancel', JOB]] and events(task) == ['cancellation_requested']
    # A cancel request proves nothing about the job ending; the phase stays locked.
    assert worker.resource_context(task, 'relaxation')['already_registered']


def test_cancel_refuses_receipt_without_strict_job_id(task, monkeypatch):
    calls = scheduler(monkeypatch)
    (task / 'submissions/relaxation.json').write_text('{"status":"submission_uncertain","job_id":"7144;rm"}')
    with pytest.raises(ValueError, match='JobID'):
        worker.cancel_job(task, 'relaxation', '7144;rm')
    assert calls == []


def test_release_archives_cancelled_receipt_and_unlocks_resources(task, monkeypatch):
    calls = scheduler(monkeypatch)
    original = (task / 'submissions/relaxation.json').read_bytes()
    result = worker.release_cancelled_submission(task, 'relaxation', JOB)
    assert result['released'] and not result['submission_authorized']
    assert not (task / 'submissions/relaxation.json').exists()
    assert (task / f'submissions/relaxation.cancelled-{JOB}.json').read_bytes() == original
    assert calls[0] == ['squeue', '--me', '-h', '-o', '%i'] and calls[1][:4] == ['sacct', '-n', '-j', JOB]
    assert events(task) == ['cancelled_submission_released']
    context = worker.resource_context(task, 'relaxation')
    assert not context.get('already_registered') and context['resources']['cores_per_test'] == 8
    # The parent phase receipt is untouched.
    assert (task / 'submissions/convergence.json').exists()


@pytest.mark.parametrize('state', ['COMPLETED', 'FAILED', 'RUNNING', 'PENDING', 'TIMEOUT'])
def test_release_refuses_unless_accounting_says_cancelled(task, monkeypatch, state):
    scheduler(monkeypatch, state=state)
    with pytest.raises(ValueError, match='CANCELLED'):
        worker.release_cancelled_submission(task, 'relaxation', JOB)
    assert (task / 'submissions/relaxation.json').exists()


def test_release_refuses_while_still_queued(task, monkeypatch):
    calls = scheduler(monkeypatch, queue=f'69999999\n{JOB}\n')
    with pytest.raises(ValueError, match='仍在队列'):
        worker.release_cancelled_submission(task, 'relaxation', JOB)
    assert [c[0] for c in calls] == ['squeue'] and (task / 'submissions/relaxation.json').exists()


def test_release_refuses_on_scheduler_query_failure(task, monkeypatch):
    import subprocess
    def run(args, **kwargs):
        raise subprocess.CalledProcessError(1, args, stderr='slurm down')
    monkeypatch.setattr(worker.subprocess, 'run', run)
    with pytest.raises(ValueError, match='slurm down'):
        worker.release_cancelled_submission(task, 'relaxation', JOB)
    assert (task / 'submissions/relaxation.json').exists()


@pytest.mark.parametrize('trace', ['outcar', 'nested', 'root_output', 'lock', 'running_state'])
def test_release_refuses_when_cancelled_run_left_output(task, monkeypatch, trace):
    scheduler(monkeypatch)
    if trace == 'outcar':
        (task / 'relax-1/OUTCAR').write_text('partial')
    if trace == 'nested':
        (task / 'validation/reference').mkdir(parents=True)
        (task / 'validation/reference/OSZICAR').write_text('partial')
    if trace == 'root_output':
        (task / 'relaxation-diagnostic.json').write_text('{}')
    if trace == 'lock':
        (task / '.agent.lock').write_text('')
    if trace == 'running_state':
        (task / 'workflow-state.json').write_text('{"status":"running","stage":"relax-1"}')
    with pytest.raises(ValueError, match='新建任务'):
        worker.release_cancelled_submission(task, 'relaxation', JOB)
    assert (task / 'submissions/relaxation.json').exists()


def test_release_requires_typed_id_current_phase_and_plain_mode(task, monkeypatch):
    calls = scheduler(monkeypatch)
    with pytest.raises(ValueError, match='不一致'):
        worker.release_cancelled_submission(task, 'relaxation', None)
    with pytest.raises(ValueError, match='没有提交回执'):
        worker.release_cancelled_submission(task, 'scf', JOB)
    (task / 'checkpoint.json').write_text(json.dumps({'completed': ['convergence', 'relaxation'], 'next': 'scf'}))
    with pytest.raises(ValueError, match='当前待执行阶段'):
        worker.release_cancelled_submission(task, 'relaxation', JOB)
    assert calls == []


def test_actions_are_dispatched(task):
    assert worker.TASK_ACTIONS['cancel_job'] is worker._cancel_job
    assert worker.TASK_ACTIONS['release_cancelled_submission'] is worker._release_cancelled_submission


def wire(monkeypatch, folder):
    """RemoteCompute.call runs the real worker handler against a local folder."""
    actions = []
    def call(self, action, job, **kwargs):
        actions.append((action, kwargs))
        return worker.TASK_ACTIONS[action](kwargs, {}, folder)
    monkeypatch.setattr(RemoteCompute, 'call', call)
    return actions


def test_slash_cancel_remote_requires_typed_job_id(tmp_path, task, monkeypatch):
    calls = scheduler(monkeypatch)
    actions = wire(monkeypatch, task)
    c = commands(tmp_path, ['yes'])
    c.handle('/cancel test La2CuO4 relaxation')
    assert calls == [] and [a for a, _ in actions] == ['cancel_job']
    c = commands(tmp_path, [JOB])
    c.handle('/cancel test La2CuO4')
    assert calls == [['scancel', JOB]]
    assert actions[-1] == ('cancel_job', {'phase': 'relaxation', 'confirm_job_id': JOB})


def test_resubmit_after_release_needs_menu_and_fresh_grant(tmp_path, task, monkeypatch):
    scheduler(monkeypatch)
    wire(monkeypatch, task)
    c = commands(tmp_path, [JOB])
    box = c.agent.tools
    key = ('test', 'La2CuO4', 'relaxation')
    # State the earlier 8-core submission left behind: a spent grant and a saved selection.
    box.submission_target, box.allow_submit = key, False
    box.submission_limits = dict(server='test', partition='compute', nodes=1, cores=8, walltime='04:00:00')
    box.resource_confirmations[key] = 'old'
    box.confirmed_resource_selections[key] = {'signature': 'old', 'resources': {'cores_per_test': 8}}
    box.monitor.register('test', 'La2CuO4', 'relaxation', JOB)
    c.handle('/cancel release test La2CuO4 relaxation')
    assert (task / f'submissions/relaxation.cancelled-{JOB}.json').exists()
    assert key not in box.confirmed_resource_selections and key not in box.resource_confirmations
    assert box.submission_target is None and not box.allow_submit
    task_row = box.monitor.status()['tasks'][0]
    assert not task_row['active'] and task_row['state'] == 'CANCELLED'
    with pytest.raises(Exception, match='request_submission'):
        box.remote_submit('test', 'La2CuO4', 'relaxation')
    assert not (task / 'submissions/relaxation.json').exists()
    # The normal operator menu opens again (no reused selection); declining grants nothing.
    prompts = []
    box.plan_reader = lambda prompt: prompts.append(prompt) or 'q'
    with pytest.raises(ValueError, match='取消'):
        box.request_submission('test', 'La2CuO4', 'relaxation')
    assert prompts and not box.allow_submit
    assert not (task / 'submissions/relaxation.json').exists()


def test_release_slash_command_declined_keeps_receipt(tmp_path, task, monkeypatch):
    calls = scheduler(monkeypatch)
    wire(monkeypatch, task)
    commands(tmp_path, ['no']).handle('/cancel release test La2CuO4 relaxation')
    assert calls == [] and (task / 'submissions/relaxation.json').exists()


def test_local_cancel_form_unchanged(tmp_path, monkeypatch):
    import vasp_agent.chat.extra_commands as extra
    c = commands(tmp_path, ['12345'])
    folder = c.agent.tools.job('si')
    folder.mkdir(parents=True)
    (folder / 'submission.json').write_text('{"job_id":"12345"}')
    calls = []
    monkeypatch.setattr(extra, 'run_slurm', lambda args: calls.append(args))
    monkeypatch.setattr(RemoteCompute, 'call', lambda *a, **k: pytest.fail('local cancel must not use SSH'))
    c.handle('/cancel si')
    assert calls == [['scancel', '12345']]


def menu_box(tmp_path, task, monkeypatch, answer):
    import vasp_agent.chat.cancel_commands as cancel
    wire(monkeypatch, task)
    monkeypatch.setattr(cancel, 'sleep', lambda _: None)
    box = commands(tmp_path, []).agent.tools
    prompts, shown = [], []
    box.plan_reader = lambda prompt: prompts.append(prompt) or answer
    box.plan_output = shown.append
    return box, prompts, shown


def test_tool_cancels_and_releases_after_operator_picks_one(tmp_path, task, monkeypatch):
    calls = []
    def run(args, **kwargs):
        calls.append(args[0])
        # Still listed on the first poll, then gone with a CANCELLED accounting row.
        queue = JOB if calls.count('squeue') == 1 else ''
        out = {'scancel': '', 'squeue': queue, 'sacct': f'{JOB}|CANCELLED by 1|0:0\n'}[args[0]]
        return NS(returncode=0, stdout=out, stderr='')
    monkeypatch.setattr(worker.subprocess, 'run', run)
    box, prompts, shown = menu_box(tmp_path, task, monkeypatch, '1')
    box.monitor.register('test', 'La2CuO4', 'relaxation', JOB)
    result = box.call('request_job_cancellation', {'server': 'test', 'job_name': 'La2CuO4',
                                                   'phase': 'relaxation', 'action': 'cancel_and_release'})
    data = json.loads(result) if isinstance(result, str) else result
    assert data['ok'], data
    assert data['result']['cancel_requested'] and data['result']['release']['released']
    assert calls.count('scancel') == 1 and len(prompts) == 1
    assert any(JOB in line for line in shown)
    assert (task / f'submissions/relaxation.cancelled-{JOB}.json').exists()
    assert not box.allow_submit and not box.monitor.status()['tasks'][0]['active']


def test_tool_does_nothing_unless_operator_confirms(tmp_path, task, monkeypatch):
    calls = scheduler(monkeypatch)
    box, prompts, _ = menu_box(tmp_path, task, monkeypatch, 'q')
    result = box.request_job_cancellation('test', 'La2CuO4', 'relaxation', 'cancel_and_release')
    assert result['status'] == 'cancelled_by_operator' and calls == [] and prompts
    box.plan_reader = None
    assert box.request_job_cancellation('test', 'La2CuO4', 'relaxation', 'cancel')['status'] == 'operator_confirmation_required'
    assert calls == [] and (task / 'submissions/relaxation.json').exists()


def test_tool_release_refusal_for_started_run_is_reported_not_retried(tmp_path, task, monkeypatch):
    calls = scheduler(monkeypatch)
    (task / 'relax-1/OUTCAR').write_text('partial')
    box, _, _ = menu_box(tmp_path, task, monkeypatch, '1')
    result = box.request_job_cancellation('test', 'La2CuO4', 'relaxation', 'release')
    assert not result['release']['released'] and '新建任务' in result['release']['reason']
    assert calls.count(['scancel', JOB]) == 0
    assert len([c for c in calls if c[0] == 'sacct']) == 1
    assert (task / 'submissions/relaxation.json').exists()
