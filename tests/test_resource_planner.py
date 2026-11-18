from copy import deepcopy
import pytest
from vasp_agent.remote import remote_worker as worker
from vasp_agent.chat.ai_agent import Toolbox


@pytest.fixture
def setup(monkeypatch):
    context = dict(signature='snapshot', resources=dict(parallel_tests=1, cores_per_test=16,
        nodes=1, partition='compute', walltime='04:00:00'), execution_plan={'tests': 4})
    monkeypatch.setattr(worker, 'resource_context', lambda *a: deepcopy(context))
    calls = []
    def read(command):
        calls.append(command)
        return '64|257667|20' if command[0] == 'sinfo' else 'DefMemPerCPU=3570'
    return context, read, calls


def test_four_tests_not_six_and_queries_cached(setup):
    context, read, calls = setup
    result = worker.plan_fast_resources(None, 'convergence', read=read)
    assert result['resources'] == {**context['resources'], 'parallel_tests': 4}
    assert result['estimated_waves'] == 1
    assert len(calls) == 2
    assert not result['submitted'] and not result['operator_confirmed']
    assert not result['optimality_verified']


def test_six_tests_require_two_nodes(setup):
    context, read, calls = setup
    context['execution_plan']['tests'] = 6
    result = worker.plan_fast_resources(None, 'convergence', read=read)
    assert result['resources']['nodes'] == 2
    assert result['resources']['parallel_tests'] == 6
    assert len(calls) == 2


def test_memory_capacity_not_cpu_alone(setup):
    _, _, calls = setup
    def read(cmd):
        calls.append(cmd)
        return '64|100000|20' if cmd[0] == 'sinfo' else 'DefMemPerCPU=3570'
    result = worker.plan_fast_resources(None, 'convergence', read=read)
    assert result['resources']['nodes'] == 4
    assert len(calls) == 2


def test_limits_reduce_concurrency_without_changing_mpi(setup):
    _, read, _ = setup
    result = worker.plan_fast_resources(None, 'convergence', dict(
        nodes=1, cores=32, partition='compute', walltime='04:00:00'), read=read)
    assert result['resources']['parallel_tests'] == 2
    assert result['resources']['cores_per_test'] == 16
    assert result['estimated_waves'] == 2


@pytest.mark.parametrize('phase', ['relaxation', 'scf', 'bands'])
def test_sequential_stage_single_group(setup, phase):
    _, read, _ = setup
    result = worker.plan_fast_resources(None, phase, read=read)
    assert result['tests'] == result['resources']['parallel_tests'] == 1


def test_unknown_capacity_stops(setup):
    with pytest.raises(worker.SubmissionError, match='格式'):
        worker.plan_fast_resources(None, 'convergence', read=lambda _: 'unknown')


def test_no_feasible_plan(setup):
    _, read, _ = setup
    with pytest.raises(worker.SubmissionError, match='没有可行'):
        worker.plan_fast_resources(None, 'convergence', dict(
            nodes=1, cores=8, partition='compute', walltime='04:00:00'), read=read)


def test_registered_never_queries_or_replans(setup):
    context, read, calls = setup
    context.update(already_registered=True, status='submission_uncertain')
    assert not worker.plan_fast_resources(None, 'convergence', read=read)['planned']
    assert not calls


def test_changed_snapshot_rejected(setup, monkeypatch):
    context, read, _ = setup
    sequence = iter([context, {**context, 'signature': 'changed'}])
    monkeypatch.setattr(worker, 'resource_context', lambda *a: next(sequence))
    with pytest.raises(ValueError, match='变化'):
        worker.plan_fast_resources(None, 'convergence', read=read)


def test_tool_saves_suggestion_not_authorization(tmp_path, monkeypatch):
    from vasp_agent.remote.remote_compute import RemoteCompute
    monkeypatch.setattr(RemoteCompute, '__init__', lambda *a: None)
    calls = []
    def rpc(self, action, job, **kwargs):
        calls.append(action)
        return dict(planned=True, signature='snapshot', resources={}, rationale='capacity checked')
    monkeypatch.setattr(RemoteCompute, 'call', rpc)
    box = Toolbox(tmp_path)
    result = box.call('plan_submission_resources', dict(server='test', job_name='si', phase='convergence'))
    assert result['ok']
    assert box.resource_suggestions[('test', 'si', 'convergence')]['automatic']
    assert not box.allow_submit
    assert calls == ['plan_submission_resources']


def test_wrong_server_never_queries(tmp_path, monkeypatch):
    box = Toolbox(tmp_path)
    box.submission_limits = {'server': 'other'}
    with pytest.raises(ValueError, match='服务器'):
        box.plan_submission_resources('test', 'si', 'convergence')


def test_automatic_proposal_not_overwritten_by_saved_defaults(tmp_path, monkeypatch):
    from vasp_agent.permissions import resource_menu
    from vasp_agent.permissions.submission_dialog import confirm
    box = Toolbox(tmp_path)
    box.plan_reader = lambda _: '1'
    box.plan_output = lambda _: None
    resources = dict(nodes=1, parallel_tests=4, cores_per_test=8,
                     partition='compute', walltime='04:00:00')
    box.resource_suggestions[('test', 'si', 'convergence')] = dict(
        signature='old', resources=resources, rationale='capacity', automatic=True)
    choices = []
    def menu(*args, **kwargs):
        choices.append(kwargs['prefer_saved'])
        assert args[3] == resources
        return resources
    monkeypatch.setattr(resource_menu, 'resource_menu', menu)
    def rpc(action, **kwargs):
        if action == 'resource_context':
            return dict(signature='old', resources={**resources, 'parallel_tests': 1},
                        execution_plan={'tests': 4})
        assert action == 'configure_resources'
        assert kwargs['resources'] == resources
        return dict(signature='new', changed=True)
    result = confirm(box, 'test', 'si', 'convergence', rpc, authorize=True)
    assert choices == [False]
    assert result['status'] == 'awaiting_resource_review'
    assert box.submission_target == ('test', 'si', 'convergence')
