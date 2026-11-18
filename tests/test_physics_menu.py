import json
import os
from types import SimpleNamespace as NS

import pytest
from vasp_agent.chat.ai_agent import Toolbox
from vasp_agent.remote.remote_worker import list_workflow_jobs, dispatch
from vasp_agent.chat.stage_commands import execute


def job(root, name, *, staged=True, confirmed=False, submitted=False, mtime=100):
    folder = root / 'runs' / name
    folder.mkdir(parents=True)
    (folder / 'manifest.json').write_text(json.dumps({'stage_gates': staged}))
    (folder / 'workflow-state.json').write_text('{"status":"prepared"}')
    if confirmed:
        (folder / 'physics-model.json').write_text('{"goal":"existing goal"}')
    if submitted:
        (folder / 'submissions').mkdir()
    for path in folder.iterdir():
        os.utime(path, (mtime, mtime))
    os.utime(folder, (mtime, mtime))
    return folder


def controller(tmp_path, answers):
    responses = iter(answers)
    output = []
    return NS(agent=NS(tools=Toolbox(tmp_path)), reader=lambda _: next(responses), output=output.append), output


def test_recent_tasks_and_unavailable_reasons(tmp_path):
    job(tmp_path, 'old', staged=False, mtime=100)
    job(tmp_path, 'new', confirmed=True, mtime=300)
    job(tmp_path, 'running', submitted=True, mtime=200)
    rows = list_workflow_jobs(tmp_path / 'runs')['jobs']
    assert [x['name'] for x in rows] == ['new', 'running', 'old']
    assert rows[0]['physics_confirmed'] and rows[0]['selectable']
    assert not rows[1]['selectable'] and not rows[2]['selectable']


def test_empty_command_selects_local_and_task_without_model_call(tmp_path):
    folder = job(tmp_path, 'recent')
    c, output = controller(tmp_path, ['1', '1', ''])
    execute(c, '')
    assert any('/physics local recent' in x for x in output)
    assert not (folder / 'physics-model.json').exists()


def test_server_only_remote_listing_and_cancel(tmp_path, monkeypatch):
    from vasp_agent.remote.remote_compute import RemoteCompute
    calls = []
    monkeypatch.setattr(RemoteCompute, '__init__', lambda *a: None)
    def call(self, action):
        calls.append(action)
        return {'jobs':[{'name':'si-task', 'status':'prepared', 'phase':'convergence',
            'physics_confirmed':False, 'selectable':True, 'modified':100}], 'total':1}
    monkeypatch.setattr(RemoteCompute, 'call', call)
    c, output = controller(tmp_path, ['q'])
    execute(c, 'test-server')
    assert calls == ['list_jobs']
    assert any('si-task' in x for x in output)


def test_unavailable_selection_does_not_confirm(tmp_path):
    job(tmp_path, 'old', staged=False)
    c, _ = controller(tmp_path, ['1'])
    with pytest.raises(ValueError, match='旧版'):
        execute(c, 'local')


def test_listing_handles_bad_metadata_and_requires_no_job_argument(tmp_path):
    folder = tmp_path / 'jobs/broken'
    folder.mkdir(parents=True)
    (folder / 'manifest.json').write_text('{broken')
    result = dispatch({'config':{'root':str(tmp_path)}, 'action':'list_jobs'})
    assert result['jobs'][0]['status'] == 'unreadable'
    assert not result['jobs'][0]['selectable']
