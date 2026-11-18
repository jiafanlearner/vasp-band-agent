from types import SimpleNamespace as NS
import hashlib
import pytest
from vasp_agent.permissions.analysis_approval import approve, settings
from vasp_agent.editing import analysis_runner as runner
from vasp_agent.chat.ai_agent import Toolbox, Agent


def box(answers):
    values = iter(answers)
    return NS(plan_reader=lambda _: next(values), plan_output=lambda _: None, allow_submit=False)


@pytest.mark.parametrize('answer', ['', 'q', '3', 'yes'])
def test_default_denies(answer):
    b = box([answer])
    assert approve(b, {'main.py': b'print(1)'}, [], 5) is None
    assert not b.allow_submit


def test_once_does_not_grant_session():
    b = box(['1', '3'])
    assert approve(b, {'main.py': b'print(1)'}, [], 5)['mode'] == 'once'
    assert approve(b, {'main.py': b'print(1)'}, [], 5) is None


def test_session_requires_literal_and_can_revoke():
    b = box(['2', 'no', '2', 'y', '1'])
    assert approve(b, {'main.py': b'print(1)'}, [], 5) is None
    assert approve(b, {'main.py': b'print(1)'}, [], 5)['mode'] == 'session_trusted'
    assert approve(b, {'main.py': b'print(2)'}, [], 5)['mode'] == 'session_trusted'
    assert settings(b)['mode'] == 'ask'
    assert not b.allow_submit


def test_settings_container_only_denies_host():
    b = box(['2'])
    assert settings(b)['mode'] == 'container_only'
    assert approve(b, {'main.py': b'print(1)'}, [], 5) is None


def test_no_reader_no_execution(tmp_path, monkeypatch):
    root = tmp_path / 'workbench'
    root.mkdir()
    (root / 'a.py').write_text('print(1)')
    sha = hashlib.sha256(b'print(1)').hexdigest()
    monkeypatch.setattr(runner, 'execute_local', lambda *a: pytest.fail('not approved'))
    b = Toolbox(tmp_path)
    assert b.run_local_analysis_script('a.py', sha, [], 5)['status'] == 'approval_denied'


def test_approved_snapshot_is_used_and_receipt_not_replayed(tmp_path, monkeypatch):
    root = tmp_path / 'workbench'
    root.mkdir()
    (root / 'a.py').write_text('print(1)')
    sha = hashlib.sha256(b'print(1)').hexdigest()
    b = Toolbox(tmp_path)
    b.plan_output = lambda _: None
    b._edit_request_id = 'same-call'
    def reader(_):
        (root / 'a.py').write_text('raise RuntimeError()')
        return '1'
    b.plan_reader = reader
    calls = []
    def run(snapshot, seconds):
        calls.append(1)
        assert (snapshot / 'main.py').read_text() == 'print(1)'
        return {'status': 'completed', 'output': '1', 'isolation': 'none'}
    monkeypatch.setattr(runner, 'execute_local', run)
    first = b.run_local_analysis_script('a.py', sha, [], 5)
    assert first['approval']['source'] == 'operator_once' and first['backend'] == 'local'
    assert b.run_local_analysis_script('a.py', sha, [], 5)['replayed'] is False
    assert len(calls) == 1 and not b.allow_submit


def test_session_change_resets_host_approval(tmp_path):
    b = Toolbox(tmp_path)
    b.local_analysis_mode = 'session_trusted'
    Agent(None, 'test', b, adapter=NS())
    assert b.local_analysis_mode == 'ask'


def test_real_local_smoke(tmp_path):
    (tmp_path / 'main.py').write_text('print(2 + 3)')
    result = runner.execute_local(tmp_path, 5)
    assert result['status'] == 'completed' and result['output'].strip() == '5'
    assert result['isolation'] == 'none'


def test_real_local_timeout(tmp_path):
    (tmp_path / 'main.py').write_text('import time; time.sleep(30)')
    assert runner.execute_local(tmp_path, 1)['status'] == 'timeout'


def test_mode_changes_are_audited():
    events = []
    b = box(['2', 'y', '1'])
    b.log = events.append
    assert approve(b, {'main.py': b'print(1)'}, [], 5)['mode'] == 'session_trusted'
    settings(b)
    assert [(e['from'], e['to'], e['confirmed_by']) for e in events] == [
        ('ask', 'session_trusted', 'operator_approval_prompt'),
        ('session_trusted', 'ask', 'operator_settings_menu')]


def test_summary_replaces_full_code_and_v_shows_it():
    shown = []
    answers = iter(['v', '1'])
    b = NS(plan_reader=lambda _: next(answers), plan_output=shown.append, allow_submit=False)
    code = b'import csv\n' + b'x = 1\n' * 400
    assert approve(b, {'main.py': code}, [], 5)['mode'] == 'once'
    assert '401 行' in shown[0] and '导入：csv' in shown[0]
    assert sum('x = 1' in text for text in shown) == 1  # only after 'v'


def test_trusted_session_still_asks_for_risky_scripts():
    b = box(['2', 'y', '3'])
    assert approve(b, {'main.py': b'print(1)'}, [], 5)['mode'] == 'session_trusted'
    assert approve(b, {'main.py': b'import csv\nprint(2)'}, [], 5)['mode'] == 'session_trusted'
    assert approve(b, {'main.py': b'import subprocess\nsubprocess.run(["ls"])'}, [], 5) is None


@pytest.mark.parametrize('code,expected', [
    ('import csv\nopen("result.csv", "w").write("x")\ns = "a".replace("a", "b")\n[1].remove(1)\nimport re\nre.compile("x")\n', []),
    ('import os\nos.remove("x")\n', ['删除文件']),
    ('import requests\n', ['联网']),
    ('from pathlib import Path\nPath("../x").write_text("y")\n', ['写文件', '临时目录以外']),
    ('eval("1")\n', ['动态执行']),
])
def test_script_risk_scan(code, expected):
    from vasp_agent.permissions.analysis_approval import script_risks
    flags = ' '.join(script_risks(code)['flags'])
    assert all(word in flags for word in expected) and (expected or not flags)
