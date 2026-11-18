import json
import sys
from pathlib import Path
from types import SimpleNamespace as NS

import pytest

from vasp_agent.chat import release_check
from vasp_agent.remote import remote_worker
from vasp_agent.remote.bundle import engine_sources, local_version
from vasp_agent.remote.remote_compute import RemoteCompute
from vasp_agent.workflow.vasp_band_agent import ToolError


def test_deployment_info_is_read_only(tmp_path):
    root = tmp_path / 'cluster' / 'agent-root'
    payload = {'config': {'root': str(root)}, 'action': 'deployment_info'}
    assert remote_worker.dispatch(payload) == {'deployed': False, 'version': None, 'release': None}
    assert not root.exists()
    root.mkdir(parents=True)
    (root / 'deployment.json').write_text(json.dumps({'release': str(root / 'releases' / 'abc'), 'version': 'abc'}))
    assert remote_worker.dispatch(payload) == {'deployed': True, 'version': 'abc', 'release': str(root / 'releases' / 'abc')}


def test_local_version_matches_what_deploy_records(tmp_path):
    root = tmp_path / 'cluster' / 'agent-root'
    result = remote_worker.dispatch({'config': {'root': str(root), 'python': sys.executable},
                                     'action': 'deploy', 'sources': engine_sources()})
    assert result['release'].endswith(local_version())
    assert json.loads((root / 'deployment.json').read_text())['version'] == local_version()


def test_local_version_ignores_checkout_line_endings(monkeypatch):
    # The same commit checked out with core.autocrlf=true must not look like a new release.
    real = Path.read_bytes
    def read(self, ending):
        return real(self).replace(b'\r\n', b'\n').replace(b'\n', ending)
    monkeypatch.setattr(Path, 'read_bytes', lambda self: read(self, b'\n'))
    lf_sources, lf_version = engine_sources(), local_version()
    monkeypatch.setattr(Path, 'read_bytes', lambda self: read(self, b'\r\n'))
    assert b'\r\n' in Path(remote_worker.__file__).read_bytes()
    assert engine_sources() == lf_sources
    assert local_version() == lf_version


def test_freshness_compares_versions(monkeypatch):
    monkeypatch.setattr(RemoteCompute, '__init__', lambda self, *a: setattr(self, 'server', 'hpc'))
    seen = []
    def call(self, action, job_name=None, **params):
        seen.append(action)
        return {'deployed': True, 'version': 'old0000000000000', 'release': '/r/releases/old'}
    monkeypatch.setattr(RemoteCompute, 'call', call)
    state = RemoteCompute('ws', 'hpc').freshness()
    assert seen == ['deployment_info']
    assert state['local_version'] == local_version() and not state['current'] and state['deployed']
    monkeypatch.setattr(RemoteCompute, 'call', lambda self, action, **kw: {'deployed': True, 'version': local_version()})
    assert RemoteCompute('ws', 'hpc').freshness()['current']


def stale(deployed=True):
    return {'server': 'hpc', 'local_version': 'new0000000000000', 'deployed': deployed,
            'deployed_version': 'old0000000000000' if deployed else None, 'current': False, 'release': None}


def make_box(answers, state):
    out, deploys = [], []
    answers = iter(answers)
    box = NS(workspace='ws', plan_output=out.append, plan_reader=lambda prompt: next(answers),
             remote_deploy=lambda server: deploys.append(server) or {'release': '/r/releases/new0000000000000'})
    return box, out, deploys


def test_current_release_asks_nothing(monkeypatch):
    monkeypatch.setattr(release_check, 'check', lambda box, server: {**stale(), 'current': True})
    box, out, deploys = make_box([], None)
    assert release_check.ensure_current(box, 'hpc')['current'] and out == [] and deploys == []


def test_stale_release_offers_deploy(monkeypatch):
    monkeypatch.setattr(release_check, 'check', lambda box, server: stale())
    box, out, deploys = make_box(['1'], None)
    assert release_check.ensure_current(box, 'hpc')['current']
    assert deploys == ['hpc']
    text = '\n'.join(out)
    assert 'new000000000' in text and 'old000000000' in text and '已准备的任务仍绑定各自准备时的 release' in text


def test_keeping_old_release_is_remembered_per_version(monkeypatch):
    state = stale()
    monkeypatch.setattr(release_check, 'check', lambda box, server: state)
    box, out, deploys = make_box(['2'], None)
    release_check.ensure_current(box, 'hpc')
    release_check.ensure_current(box, 'hpc')  # no second prompt: the iterator would raise
    assert deploys == []
    state = {**stale(), 'deployed_version': 'other00000000000'}
    with pytest.raises(StopIteration):
        release_check.ensure_current(box, 'hpc')


def test_cancel_and_missing_deploy(monkeypatch):
    monkeypatch.setattr(release_check, 'check', lambda box, server: stale())
    box, out, deploys = make_box(['q'], None)
    with pytest.raises(ToolError) as error:
        release_check.ensure_current(box, 'hpc')
    assert error.value.error_code == 'ENGINE_UPDATE_DECLINED' and deploys == []
    monkeypatch.setattr(release_check, 'check', lambda box, server: stale(deployed=False))
    box, out, deploys = make_box(['2'], None)  # no "keep" option when nothing is deployed
    with pytest.raises(ToolError):
        release_check.ensure_current(box, 'hpc')
    box.plan_reader = None
    with pytest.raises(ToolError) as error:
        release_check.ensure_current(box, 'hpc')
    assert error.value.error_code == 'ENGINE_NOT_DEPLOYED'


def test_non_interactive_stale_continues_with_notice(monkeypatch):
    monkeypatch.setattr(release_check, 'check', lambda box, server: stale())
    box, out, deploys = make_box([], None)
    box.plan_reader = None
    assert not release_check.ensure_current(box, 'hpc')['current']
    assert '非交互模式' in out[-1] and deploys == []


def test_unreadable_profile_does_not_mask_prepare_error(monkeypatch):
    def broken(box, server):
        raise KeyError('root')
    monkeypatch.setattr(release_check, 'check', broken)
    box, out, deploys = make_box([], None)
    assert release_check.ensure_current(box, 'hpc') is None and '未能核对' in out[0]


def test_remote_prepare_checks_release_first(tmp_path, monkeypatch):
    from vasp_agent.chat.ai_agent import Toolbox
    order = []
    monkeypatch.setattr(release_check, 'check', lambda box, server: order.append('check') or stale())
    monkeypatch.setattr(RemoteCompute, '__init__', lambda self, *a: None)
    monkeypatch.setattr(RemoteCompute, 'prepare', lambda self, *a, **kw: order.append('prepare') or {'prepared': True})
    box = Toolbox(tmp_path)
    box.plan_reader = lambda prompt: 'q'
    box.plan_output = lambda text: None
    with pytest.raises(ToolError):
        box.remote_prepare('hpc', 'si', 'Si.vasp', 520, [8, 8, 8], 16, 'cell')
    assert order == ['check']
    box.plan_reader = lambda prompt: '2'
    assert box.remote_prepare('hpc', 'si', 'Si.vasp', 520, [8, 8, 8], 16, 'cell') == {'prepared': True}
    assert order == ['check', 'check', 'prepare']


def test_servers_listing_shows_versions(tmp_path, monkeypatch):
    from vasp_agent.chat.ai_agent import Toolbox
    (tmp_path / 'servers').mkdir()
    for name in ('a', 'b', 'c'):
        (tmp_path / 'servers' / f'{name}.json').write_text(json.dumps({'host': name, 'port': 22}))
    for name in ('a', 'b'):
        (tmp_path / 'servers' / f'{name}.compute.json').write_text('{}')
    def check(box, server):
        if server == 'b':
            raise ToolError('SSH_CONNECTION_FAILED', 'timeout')
        return stale()
    monkeypatch.setattr(release_check, 'check', check)
    box = Toolbox(tmp_path)
    rows = {row['name']: row['engine'] for row in release_check.server_versions(box, box.list_servers())}
    assert rows['a'] == {'local_version': 'new0000000000000', 'deployed_version': 'old0000000000000', 'status': '需重新部署'}
    assert rows['b']['status'] == '无法查询' and 'SSH_CONNECTION_FAILED' not in rows['b']['status']
    assert '/setup-server c' in rows['c']['status']
