import hashlib
import json
import subprocess
from types import SimpleNamespace as NS
import pytest
from vasp_agent.editing import analysis_runner as runner
from vasp_agent.workflow.vasp_band_agent import ToolError
from vasp_agent.common import agent_paths


def draft(tmp_path, text='print(1 + 1)'):
    root = tmp_path / 'workbench'
    root.mkdir(exist_ok=True)
    (root / 'calc.py').write_text(text, encoding='utf-8')
    return hashlib.sha256((root / 'calc.py').read_bytes()).hexdigest()


def test_validate_never_executes(tmp_path, monkeypatch):
    sha = draft(tmp_path, 'raise RuntimeError("not executed")')
    monkeypatch.setattr(runner, 'probe', lambda: pytest.fail('no runtime check'))
    result = runner.AnalysisRunner(tmp_path).validate('calc.py', sha, [])
    assert result['syntax_valid'] and not result['executed']


@pytest.mark.parametrize('case', ['syntax', 'hash', 'traversal', 'secret'])
def test_invalid_scripts_rejected_before_execution(tmp_path, monkeypatch, case):
    sha = draft(tmp_path, 'if :' if case == 'syntax' else 'password="private"' if case == 'secret' else 'print(1)')
    monkeypatch.setattr(runner, 'probe', lambda: pytest.fail('no runtime'))
    with pytest.raises(ToolError):
        runner.AnalysisRunner(tmp_path).run('../calc.py' if case == 'traversal' else 'calc.py', 'bad' if case == 'hash' else sha, [])


def test_no_sandbox_no_host_fallback(tmp_path, monkeypatch):
    sha = draft(tmp_path)
    monkeypatch.setattr(runner.shutil, 'which', lambda _: None)
    monkeypatch.setattr(runner.subprocess, 'Popen', lambda *a, **k: pytest.fail('host execution'))
    result = runner.AnalysisRunner(tmp_path).run('calc.py', sha, [])
    assert result['status'] == 'sandbox_unavailable' and not result['executed']
    assert not agent_paths.store_dir(tmp_path, 'analysis_runs').exists()


def test_snapshot_only_explicit_files_and_idempotent_receipt(tmp_path, monkeypatch):
    sha = draft(tmp_path)
    (tmp_path / 'workbench/data.json').write_text('[1,2,3]')
    (tmp_path / 'workbench/unselected.txt').write_text('not selected')
    data_sha = hashlib.sha256(b'[1,2,3]').hexdigest()
    monkeypatch.setattr(runner, 'probe', lambda: {'available': True, 'image_id': 'sha256:'+'a'*64})
    calls = []
    def execute(runtime, snapshot, seconds):
        calls.append(1)
        assert sorted(str(p.relative_to(snapshot)).replace('\\','/') for p in snapshot.rglob('*') if p.is_file()) == ['inputs/0/data.json', 'main.py']
        return {'status': 'completed', 'output': '2', 'exit_code': 0}
    monkeypatch.setattr(runner, 'execute', execute)
    service = runner.AnalysisRunner(tmp_path)
    args = ('calc.py', sha, [{'path': 'data.json', 'expected_sha256': data_sha}])
    first = service.run(*args, request_id='call1')
    assert service.run(*args, request_id='call1')['replayed'] is False
    assert service.get(first['run_id'])['status'] == 'completed'
    assert len(calls) == 1
    assert (tmp_path / 'workbench/calc.py').read_text() == 'print(1 + 1)'
    with pytest.raises(ToolError):
        service.run(*args, timeout_seconds=10, request_id='call1')


def test_input_hash_conflict(tmp_path):
    sha = draft(tmp_path)
    with pytest.raises(ToolError):
        runner.AnalysisRunner(tmp_path).validate('calc.py', sha, [{'path': 'calc.py', 'expected_sha256': 'changed'}])


def test_docker_command_has_no_host_credentials_or_network(tmp_path):
    command = runner.container_command({'docker': 'docker', 'image_id': 'sha256:'+'a'*64}, 'test', tmp_path)
    for flag in ('--network=none', '--read-only', '--cap-drop=ALL', '--security-opt=no-new-privileges',
                 '--memory=256m', '--pids-limit=32', '--pull=never', '--user=65534:65534'):
        assert flag in command
    assert command.count('--mount') == 1
    assert 'readonly' in command[command.index('--mount')+1]
    assert command[-4:] == ['python', '-I', '-B', '/analysis/main.py']


@pytest.mark.parametrize('remote', [True, False])
def test_probe_rejects_remote_and_accepts_local_linux(monkeypatch, remote):
    for key in ('DOCKER_HOST', 'DOCKER_CONTEXT', 'DOCKER_TLS_VERIFY'):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setattr(runner.shutil, 'which', lambda _: 'docker')
    def run(command, **kw):
        if command[1] == 'context':
            out = json.dumps([{'Endpoints': {'docker': {'Host': 'ssh://remote' if remote else 'unix:///var/run/docker.sock'}}}])
        else:
            out = 'linux' if command[1] == 'info' else 'sha256:' + 'a'*64
        return NS(returncode=0, stdout=out)
    monkeypatch.setattr(runner.subprocess, 'run', run)
    assert runner.probe()['available'] is not remote


def test_timeout_cleans_only_own_container(tmp_path, monkeypatch):
    class Process:
        returncode = None
        def poll(self): return self.returncode
        def kill(self): self.returncode = -9
        def wait(self, **kw): return self.returncode
    monkeypatch.setattr(runner.subprocess, 'Popen', lambda *a, **k: Process())
    clock = iter([0, 2, 2, 2])
    monkeypatch.setattr(runner.time, 'monotonic', lambda: next(clock))
    calls = []
    def run(command, **kwargs):
        calls.append(command)
        return NS(returncode=0)
    monkeypatch.setattr(runner.subprocess, 'run', run)
    result = runner.execute({'docker': 'docker', 'image_id': 'sha256:'+'a'*64}, tmp_path, 1)
    assert result['status'] == 'timeout'
    assert calls == [['docker', 'rm', '-f', result['container_name']]]


def test_interruption_receipt_prevents_replay(tmp_path, monkeypatch):
    sha = draft(tmp_path)
    monkeypatch.setattr(runner, 'probe', lambda: {'available': True, 'image_id': 'sha256:'+'a'*64})
    def interrupt(*a): raise KeyboardInterrupt()
    monkeypatch.setattr(runner, 'execute', interrupt)
    service = runner.AnalysisRunner(tmp_path)
    with pytest.raises(KeyboardInterrupt):
        service.run('calc.py', sha, [], request_id='interrupted')
    assert service.run('calc.py', sha, [], request_id='interrupted')['status'] == 'running_or_interrupted'


def test_tools_integrated_without_submission_permission(tmp_path, monkeypatch):
    from vasp_agent.chat.ai_agent import Toolbox
    from vasp_agent.remote.remote_worker import SOURCE_FILES
    sha = draft(tmp_path)
    box = Toolbox(tmp_path)
    assert not box.allow_submit
    result = box.call('validate_analysis_script', {'script': 'calc.py', 'expected_sha256': sha, 'inputs': []})
    assert result['ok'] and result['result']['syntax_valid']
    monkeypatch.setattr(runner, 'probe', lambda: {'available': False, 'reason': 'fixture'})
    result = box.call('run_analysis_script', {'script': 'calc.py', 'expected_sha256': sha, 'inputs': [], 'timeout_seconds': 5})
    assert result['ok'] and result['result']['status'] == 'sandbox_unavailable'
    assert not box.allow_submit
    assert not any('analysis_runner' in name for name in SOURCE_FILES)
