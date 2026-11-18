"""Per-call digest memo: each unchanged file hashed once per worker action, never reused across actions."""
import collections
import json
import os
from pathlib import Path

import pytest

import vasp_agent.remote.remote_worker as worker
import vasp_agent.remote.worker_common as common
from vasp_agent.workflow import stage_gate as gate, vasp_band_agent as core
from test_stage_gate import staged, approve  # noqa: F401
from test_phase_resume import vasp, timed_out, scheduler, JOB  # noqa: F401


@pytest.fixture
def hashes(monkeypatch):
    """Full-file SHA-256 reads per path, by the engine and by the worker."""
    counts = collections.Counter()
    for module in (core, common):
        original = module._sha256_file
        def counted(path, _original=original):
            counts[Path(path).resolve()] += 1
            return _original(path)
        monkeypatch.setattr(module, '_sha256_file', counted)
    return counts


@pytest.fixture
def resumed(timed_out, monkeypatch):
    """A resumed relaxation awaiting review: the worker and the engine both check the reused stages."""
    scheduler(monkeypatch)
    worker.prepare_phase_resume(timed_out, 'relaxation', JOB)
    worker.write_json(timed_out / 'environment-review.json', ENVIRONMENT)
    monkeypatch.setattr(worker, 'inspect_environment', lambda *a: ENVIRONMENT)
    monkeypatch.setattr(worker, 'capacity_preflight', lambda script: {'capacity_checked': True})
    monkeypatch.setattr(worker, 'check_previous_job', lambda folder, phase: None)
    approve(timed_out)
    return timed_out


ENVIRONMENT = {'executable_sha256': 'synthetic', 'environment_sha256': 'synthetic', 'executable': '/mock/vasp'}


def action(name, folder, **payload):
    """A task action as dispatch runs it."""
    handler = worker.TASK_ACTIONS[name]
    if name not in worker.MEMO_ACTIONS:
        return handler(payload, {}, folder)
    with worker.memoized_action():
        return handler(payload, {}, folder)


@pytest.mark.parametrize('name', ['stage_context', 'stage_review', 'submission_preflight'])
def test_each_file_is_hashed_once_per_action(resumed, hashes, name):
    payload = {}
    if name == 'stage_review':
        data = gate.context(resumed)
        payload = dict(snapshot=data['snapshot'], phase='relaxation', reviewer='test-model',
                       checks=json.loads((resumed / 'review-relaxation.json').read_text(encoding='utf-8'))['checks'])
    elif name == 'submission_preflight':
        payload = {'phase': 'relaxation'}
    hashes.clear()
    action(name, resumed, **payload)
    assert hashes and max(hashes.values()) == 1, hashes.most_common(3)
    # The reused stages are still checked -- by the worker and the engine, now with one read.
    assert hashes[(resumed / 'relax-1/vasprun.xml').resolve()] == 1
    assert hashes[(resumed / 'convergence/encut-0/vasprun.xml').resolve()] == 1
    hashes.clear()
    # The same work without the memo, as before this change, reads files repeatedly.
    worker.TASK_ACTIONS[name](payload, {}, resumed) if name != 'stage_review' else gate.context(resumed)
    assert max(hashes.values()) > 1


def test_memo_is_scoped_to_one_dispatched_action(tmp_path, monkeypatch):
    root = tmp_path / 'a' / 'b' / 'c'
    (root / 'jobs' / 'si').mkdir(parents=True)
    seen = {}
    def record(name):
        def handler(payload, cfg, folder):
            seen[name] = (common._DIGESTS is not None, core._digests)
            worker.engine_module('vasp_band_agent')
            seen[name + ':engine'] = core._digests is common._DIGESTS and core._digests is not None
            return {}
        return handler
    for name in ('stage_context', 'status'):
        monkeypatch.setitem(worker.TASK_ACTIONS, name, record(name))
        worker.dispatch({'config': {'root': str(root)}, 'action': name, 'job_name': 'si'})
    assert seen['stage_context'][0] and seen['stage_context:engine']
    assert seen['status'] == (False, None) and not seen['status:engine']
    assert common._DIGESTS is None and core._digests is None and worker._MEMO_SCOPE is None


def test_file_changed_between_two_actions_is_caught(resumed):
    action('stage_context', resumed)
    # Same size, so only the stat times (and the fresh memo of the next action) tell them apart.
    (resumed / 'relax-1/vasprun.xml').write_text('-12.0')
    with pytest.raises(ValueError, match='续算复用的 relax-1'):
        action('stage_context', resumed)


@pytest.mark.parametrize('change', ['size', 'same_size_new_mtime', 'atomic_replace'])
def test_file_changed_within_an_action_is_hashed_again(tmp_path, change):
    path = tmp_path / 'vasprun.xml'
    path.write_text('-10.0')
    with core.digest_memo():
        first = core.digest(path)
        assert core.digest(path) == first
        if change == 'size':
            path.write_text('-10.00')
        elif change == 'same_size_new_mtime':
            stat = path.stat()
            path.write_text('-12.0')
            os.utime(path, ns=(stat.st_atime_ns, stat.st_mtime_ns + 1_000_000))
        else:
            core.save(path, {'xml': 1})
        assert core.digest(path) == core._sha256_file(path)[0] != first


def test_worker_and_engine_share_one_read(tmp_path, hashes):
    path = tmp_path / 'vasprun.xml'
    path.write_bytes(b'x' * 4096)
    with worker.memoized_action():
        worker.engine_module('stage_gate')
        assert worker.file_sha(path) == core.digest(path) == worker.sha(path)
        worker.write_json(tmp_path / 'other.json', {})  # any write by the action forgets everything
        core.digest(path)
    assert hashes[path.resolve()] == 2


def test_a_file_written_during_its_read_is_not_memoized(tmp_path, monkeypatch, hashes):
    path = tmp_path / 'vasprun.xml'
    path.write_text('-10.0')
    real = core.file_identity
    calls = []
    def moving(p):
        calls.append(p)
        identity = real(p)
        return identity[:-1] + (identity[-1] + len(calls),)  # a different stat every time
    monkeypatch.setattr(core, 'file_identity', moving)
    with core.digest_memo() as memo:
        core.digest(path)
        core.digest(path)
        assert memo == {}
    assert hashes[path.resolve()] == 2


def test_old_engine_without_memo_still_verifies(resumed, hashes, monkeypatch):
    """A task bound to a release older than the memo hashes every time, exactly as before."""
    monkeypatch.delattr(core, 'digest_memo')
    action('stage_context', resumed)
    assert core._digests is None and max(hashes.values()) > 1


def test_memo_is_not_left_active(resumed):
    with pytest.raises(ValueError):
        with worker.memoized_action():
            (resumed / 'relax-1/vasprun.xml').write_text('-12.0')
            worker.TASK_ACTIONS['stage_context']({}, {}, resumed)
    assert common._DIGESTS is None and core._digests is None
