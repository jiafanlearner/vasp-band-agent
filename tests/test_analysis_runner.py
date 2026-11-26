import hashlib
import json
import os
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


# ---- T5.1: dual image profiles, operator override, elevated caps, signature ----

def _fake_docker_run(installed_images, calls=None):
    def run(command, **kw):
        if calls is not None:
            calls.append(list(command))
        assert 'pull' not in command, 'probe must never invoke docker pull'
        if command[1] == 'context':
            return NS(returncode=0, stdout=json.dumps([{'Endpoints': {'docker': {'Host': 'unix:///var/run/docker.sock'}}}]))
        if command[1] == 'info':
            return NS(returncode=0, stdout='linux')
        if command[1] == 'image' and command[2] == 'inspect':
            ref = command[3]
            if ref in installed_images:
                return NS(returncode=0, stdout=installed_images[ref])
            return NS(returncode=1, stdout='', stderr='No such image')
        raise AssertionError(f'unexpected docker call: {command}')
    return run


@pytest.mark.parametrize('bad_ref', [
    '', ' ', '--privileged', 'vasp-agent-sci:latest --network=host',
    '../escape', 'UPPER/sci:latest', 'sci;rm', 'a' * 256, 123, None,
])
def test_validate_image_ref_rejects_invalid(bad_ref):
    with pytest.raises(ValueError):
        runner.validate_image_ref(bad_ref)


@pytest.mark.parametrize('good_ref', [
    'vasp-agent-sci:latest',
    'python:3.12-slim',
    'myorg/vasp-sci:v1.2_py312',
    'localhost:5000/vasp/sci-env:2026.11',
    'ghcr.io/my-org/vasp-sci:1.0@sha256:' + 'b' * 64,
])
def test_validate_image_ref_accepts_valid(good_ref):
    assert runner.validate_image_ref(good_ref) == good_ref


def test_probe_reports_both_profiles_without_auto_pull(tmp_path, monkeypatch):
    for key in ('DOCKER_HOST', 'DOCKER_CONTEXT', 'DOCKER_TLS_VERIFY', runner.SCIENTIFIC_IMAGE_ENV):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setattr(runner.shutil, 'which', lambda _: 'docker')
    default_id = 'sha256:' + '1' * 64
    sci_id = 'sha256:' + '2' * 64
    calls = []
    # Only default installed: scientific unavailable, never pulled.
    monkeypatch.setattr(runner.subprocess, 'run', _fake_docker_run({runner.IMAGE: default_id}, calls))
    only_default = runner.probe(tmp_path)
    assert only_default['available'] is True
    assert only_default['image_id'] == default_id
    assert only_default['profiles']['default'] == {'available': True, 'image': runner.IMAGE, 'image_id': default_id}
    assert only_default['profiles']['scientific']['available'] is False
    assert only_default['profiles']['scientific']['image'] == runner.SCIENTIFIC_IMAGE
    assert '不会自动拉取' in only_default['profiles']['scientific']['reason']
    assert not any('pull' in cmd for cmd in calls)

    # Both installed: both available with distinct verified sha256 IDs.
    monkeypatch.setattr(runner.subprocess, 'run', _fake_docker_run({
        runner.IMAGE: default_id, runner.SCIENTIFIC_IMAGE: sci_id,
    }))
    both = runner.probe(tmp_path)
    assert both['available'] is True
    assert both['profiles']['default']['image_id'] == default_id
    assert both['profiles']['scientific']['available'] is True
    assert both['profiles']['scientific']['image_id'] == sci_id


def test_scientific_image_operator_overrides_and_invalid_rejection(tmp_path, monkeypatch):
    for key in ('DOCKER_HOST', 'DOCKER_CONTEXT', 'DOCKER_TLS_VERIFY', runner.SCIENTIFIC_IMAGE_ENV):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setattr(runner.shutil, 'which', lambda _: 'docker')
    default_id = 'sha256:' + '1' * 64
    cfg_id = 'sha256:' + '3' * 64
    env_id = 'sha256:' + '4' * 64

    # Operator local controlled config under .agent/state/analysis-sandbox.json
    cfg_path = agent_paths.agent_path(tmp_path, runner.SANDBOX_CONFIG_REL)
    cfg_path.parent.mkdir(parents=True, exist_ok=True)
    cfg_path.write_text(json.dumps({'scientific_image': 'localhost:5000/vasp/sci:1.0'}), encoding='utf-8')
    monkeypatch.setattr(runner.subprocess, 'run', _fake_docker_run({
        runner.IMAGE: default_id, 'localhost:5000/vasp/sci:1.0': cfg_id,
    }))
    from_cfg = runner.probe(tmp_path)
    assert from_cfg['profiles']['scientific']['available'] is True
    assert from_cfg['profiles']['scientific']['image_id'] == cfg_id
    assert from_cfg['profiles']['scientific']['source'] == 'config'

    # Env var overrides local config.
    monkeypatch.setenv(runner.SCIENTIFIC_IMAGE_ENV, 'ghcr.io/lab/vasp-sci:2.0')
    monkeypatch.setattr(runner.subprocess, 'run', _fake_docker_run({
        runner.IMAGE: default_id, 'ghcr.io/lab/vasp-sci:2.0': env_id,
    }))
    from_env = runner.probe(tmp_path)
    assert from_env['profiles']['scientific']['available'] is True
    assert from_env['profiles']['scientific']['image_id'] == env_id
    assert from_env['profiles']['scientific']['source'] == 'env'

    # Invalid env override is rejected BEFORE calling docker image inspect, while default stays usable.
    calls = []
    monkeypatch.setenv(runner.SCIENTIFIC_IMAGE_ENV, '--privileged')
    monkeypatch.setattr(runner.subprocess, 'run', _fake_docker_run({runner.IMAGE: default_id}, calls))
    bad_env = runner.probe(tmp_path)
    assert bad_env['profiles']['default']['available'] is True
    assert bad_env['profiles']['scientific']['available'] is False
    assert '格式无效' in bad_env['profiles']['scientific']['reason']
    assert not any('--privileged' in cmd for cmd in calls)


def test_container_command_isolation_and_caps_by_profile_and_export(tmp_path):
    base_isolation = (
        '--pull=never', '--network=none', '--read-only', '--cap-drop=ALL',
        '--security-opt=no-new-privileges', '--user=65534:65534',
        '--cpus=1', '--pids-limit=32', '--log-driver=none',
    )
    rt = {'docker': 'docker', 'image_id': 'sha256:' + 'a' * 64}

    cmd_default = runner.container_command(rt, 'c1', tmp_path)
    for flag in base_isolation:
        assert flag in cmd_default
    assert '--memory=256m' in cmd_default and '--memory-swap=256m' in cmd_default
    assert '/tmp:rw,noexec,nosuid,size=16m' in cmd_default
    assert 'fsize=1048576:1048576' in cmd_default
    for env_entry in (
        'HOME=/tmp', 'XDG_CACHE_HOME=/tmp/.cache', 'MPLCONFIGDIR=/tmp/mpl', 'MPLBACKEND=Agg',
        'OPENBLAS_NUM_THREADS=1', 'OMP_NUM_THREADS=1', 'MKL_NUM_THREADS=1', 'NUMEXPR_NUM_THREADS=1',
    ):
        assert env_entry in cmd_default

    for kwargs in (
        {'image_profile': 'scientific', 'export_outputs': False},
        {'image_profile': 'default', 'export_outputs': True},
        {'image_profile': 'scientific', 'export_outputs': True},
    ):
        cmd = runner.container_command(rt, 'c2', tmp_path, **kwargs)
        for flag in base_isolation:
            assert flag in cmd
        assert '--memory=512m' in cmd and '--memory-swap=512m' in cmd
        assert '/tmp:rw,noexec,nosuid,size=32m' in cmd
        assert 'fsize=2097152:2097152' in cmd
        for env_entry in (
            'HOME=/tmp', 'XDG_CACHE_HOME=/tmp/.cache', 'MPLCONFIGDIR=/tmp/mpl', 'MPLBACKEND=Agg',
            'OPENBLAS_NUM_THREADS=1', 'OMP_NUM_THREADS=1', 'MKL_NUM_THREADS=1', 'NUMEXPR_NUM_THREADS=1',
        ):
            assert env_entry in cmd
        assert cmd[-4:] == ['python', '-I', '-B', '/analysis/main.py']

    # container_command must require a verified sha256 image digest, never a mutable tag.
    with pytest.raises(ToolError) as exc:
        runner.container_command({'docker': 'docker', 'image_id': 'vasp-agent-sci:latest'}, 'c3', tmp_path)
    assert exc.value.error_code == 'ANALYSIS_ARGUMENT'

    # Non-bool export_outputs must never be coerced to True.
    for bad_export in ('false', 'yes', 1, 0):
        with pytest.raises(ToolError) as exc_cap:
            runner.resource_caps('default', bad_export)
        assert exc_cap.value.error_code == 'ANALYSIS_ARGUMENT'
        with pytest.raises(ToolError) as exc_cmd:
            runner.container_command(rt, 'c4', tmp_path, export_outputs=bad_export)
        assert exc_cmd.value.error_code == 'ANALYSIS_ARGUMENT'


@pytest.mark.parametrize('bad_profile', ['python:3.12-slim', 'ubuntu:latest', 'vasp-agent-sci:latest', '', None, 1])
def test_run_rejects_non_enum_image_profile(tmp_path, monkeypatch, bad_profile):
    sha = draft(tmp_path)
    monkeypatch.setattr(runner, 'probe', lambda *a: pytest.fail('must reject before probe'))
    with pytest.raises(ToolError) as exc:
        runner.AnalysisRunner(tmp_path).run('calc.py', sha, [], image_profile=bad_profile)
    assert exc.value.error_code == 'ANALYSIS_ARGUMENT'


def test_run_scientific_profile_missing_does_not_fallback_and_idempotency_tracks_new_params(tmp_path, monkeypatch):
    sha = draft(tmp_path)
    default_id = 'sha256:' + '1' * 64
    sci_id = 'sha256:' + '2' * 64
    # Only default installed -> scientific returns sandbox_unavailable without fallback or receipt.
    monkeypatch.setattr(runner, 'probe', lambda workspace=None: {
        'available': True, 'docker': 'docker', 'image_id': default_id, 'network': False,
        'profiles': {
            'default': {'available': True, 'image': runner.IMAGE, 'image_id': default_id},
            'scientific': {'available': False, 'image': runner.SCIENTIFIC_IMAGE, 'image_id': None, 'reason': '未安装'},
        },
    })
    monkeypatch.setattr(runner, 'execute', lambda *a: pytest.fail('must not fallback to default'))
    service = runner.AnalysisRunner(tmp_path)
    missing = service.run('calc.py', sha, [], image_profile='scientific', request_id='req-missing')
    assert missing['executed'] is False and missing['status'] == 'sandbox_unavailable'
    assert missing['image_profile'] == 'scientific'
    assert 'docker' not in missing['runtime']
    assert not agent_paths.store_dir(tmp_path, 'analysis_runs').exists()

    # Both installed -> scientific runs with sci_id, records profile, and request_id signature covers profile & export.
    monkeypatch.setattr(runner, 'probe', lambda workspace=None: {
        'available': True, 'docker': 'docker', 'image_id': default_id, 'network': False,
        'profiles': {
            'default': {'available': True, 'image': runner.IMAGE, 'image_id': default_id},
            'scientific': {'available': True, 'image': runner.SCIENTIFIC_IMAGE, 'image_id': sci_id},
        },
    })
    seen = []
    def fake_exec(runtime, snapshot, seconds):
        seen.append((runtime['image_profile'], runtime['image_id'], runtime['export_outputs']))
        return {'status': 'completed', 'output': 'ok', 'exit_code': 0}
    monkeypatch.setattr(runner, 'execute', fake_exec)

    first = service.run('calc.py', sha, [], image_profile='scientific', request_id='req-sci')
    assert first['status'] == 'completed' and first['image_profile'] == 'scientific'
    assert first['image_id'] == sci_id and first['content_is_untrusted'] is True
    assert service.run('calc.py', sha, [], image_profile='scientific', request_id='req-sci')['replayed'] is False
    assert seen == [('scientific', sci_id, False)]

    # Same request_id with different image_profile or export_outputs must conflict.
    with pytest.raises(ToolError) as exc1:
        service.run('calc.py', sha, [], image_profile='default', request_id='req-sci')
    assert exc1.value.error_code == 'ANALYSIS_CONFLICT'
    with pytest.raises(ToolError) as exc2:
        service.run('calc.py', sha, [], image_profile='scientific', export_outputs=True, request_id='req-sci')
    assert exc2.value.error_code == 'ANALYSIS_CONFLICT'

    # Local backend rejects scientific image_profile and export_outputs.
    with pytest.raises(ToolError) as exc3:
        service.run('calc.py', sha, [], backend='local', image_profile='scientific')
    assert exc3.value.error_code == 'ANALYSIS_ARGUMENT'
    with pytest.raises(ToolError) as exc4:
        service.run('calc.py', sha, [], backend='local', export_outputs=True)
    assert exc4.value.error_code == 'ANALYSIS_ARGUMENT'


def test_only_scientific_installed_does_not_satisfy_default_profile(tmp_path, monkeypatch):
    sha = draft(tmp_path)
    sci_id = 'sha256:' + '5' * 64
    for key in ('DOCKER_HOST', 'DOCKER_CONTEXT', 'DOCKER_TLS_VERIFY', runner.SCIENTIFIC_IMAGE_ENV):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setattr(runner.shutil, 'which', lambda _: 'docker')
    monkeypatch.setattr(runner.subprocess, 'run', _fake_docker_run({runner.SCIENTIFIC_IMAGE: sci_id}))
    runtime = runner.probe(tmp_path)
    assert runtime['available'] is True and runtime['image_id'] is None
    assert runtime['profiles']['default']['available'] is False
    assert runtime['profiles']['scientific']['available'] is True

    executed = []
    monkeypatch.setattr(runner, 'execute', lambda rt, snap, sec: (executed.append(rt['image_id']), {'status': 'completed', 'exit_code': 0})[1])
    service = runner.AnalysisRunner(tmp_path)
    default_res = service.run('calc.py', sha, [], image_profile='default')
    assert default_res['executed'] is False and default_res['status'] == 'sandbox_unavailable'
    assert executed == []

    sci_res = service.run('calc.py', sha, [], image_profile='scientific')
    assert sci_res['status'] == 'completed' and sci_res['image_id'] == sci_id
    assert executed == [sci_id]


def test_config_guarded_rejection_blocks_scientific_image_only(tmp_path, monkeypatch):
    for key in ('DOCKER_HOST', 'DOCKER_CONTEXT', 'DOCKER_TLS_VERIFY', runner.SCIENTIFIC_IMAGE_ENV):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setattr(runner.shutil, 'which', lambda _: 'docker')
    default_id = 'sha256:' + '1' * 64
    monkeypatch.setattr(runner.subprocess, 'run', _fake_docker_run({runner.IMAGE: default_id}))
    monkeypatch.setattr(runner, 'guarded', lambda p: (_ for _ in ()).throw(ToolError('EDIT_PATH_BLOCKED', '链接路径禁止')))
    result = runner.probe(tmp_path)
    assert result['profiles']['default']['available'] is True
    assert result['profiles']['scientific']['available'] is False
    assert '链接路径禁止' in result['profiles']['scientific']['reason']


@pytest.mark.parametrize('case', ['bad_json', 'not_dict', 'unknown_key', 'oversized', 'directory', 'hardlink'])
def test_sandbox_config_malformed_and_hardlink_rejected(tmp_path, monkeypatch, case):
    for key in ('DOCKER_HOST', 'DOCKER_CONTEXT', 'DOCKER_TLS_VERIFY', runner.SCIENTIFIC_IMAGE_ENV):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setattr(runner.shutil, 'which', lambda _: 'docker')
    default_id = 'sha256:' + '1' * 64
    calls = []
    monkeypatch.setattr(runner.subprocess, 'run', _fake_docker_run({runner.IMAGE: default_id}, calls))
    cfg_path = agent_paths.agent_path(tmp_path, runner.SANDBOX_CONFIG_REL)
    cfg_path.parent.mkdir(parents=True, exist_ok=True)
    if case == 'bad_json':
        cfg_path.write_text('{broken', encoding='utf-8')
    elif case == 'not_dict':
        cfg_path.write_text('["vasp-agent-sci:latest"]', encoding='utf-8')
    elif case == 'unknown_key':
        cfg_path.write_text(json.dumps({'science_image': 'vasp-agent-sci:latest'}), encoding='utf-8')
    elif case == 'oversized':
        cfg_path.write_text(json.dumps({'scientific_image': 'vasp-agent-sci:latest'}) + ' ' * 5000, encoding='utf-8')
    elif case == 'directory':
        cfg_path.mkdir()
    elif case == 'hardlink':
        other = tmp_path / 'linked-config.json'
        other.write_text(json.dumps({'scientific_image': 'vasp-agent-sci:latest'}), encoding='utf-8')
        os.link(other, cfg_path)
    result = runner.probe(tmp_path)
    assert result['profiles']['default']['available'] is True
    assert result['profiles']['scientific']['available'] is False
    assert result['profiles']['scientific']['source'] == 'config'
    assert not any(runner.SCIENTIFIC_IMAGE in cmd for cmd in calls)


@pytest.mark.parametrize('failure', ['no_docker', 'daemon_fail'])
def test_probe_unavailable_populates_both_profiles(tmp_path, monkeypatch, failure):
    for key in ('DOCKER_HOST', 'DOCKER_CONTEXT', 'DOCKER_TLS_VERIFY', runner.SCIENTIFIC_IMAGE_ENV):
        monkeypatch.delenv(key, raising=False)
    if failure == 'no_docker':
        monkeypatch.setattr(runner.shutil, 'which', lambda _: None)
    else:
        monkeypatch.setattr(runner.shutil, 'which', lambda _: 'docker')
        monkeypatch.setattr(runner.subprocess, 'run', lambda *a, **k: NS(returncode=1, stdout='', stderr='boom'))
    res = runner.probe(tmp_path)
    assert res['available'] is False and res['network'] is False
    assert set(res['profiles']) == {'default', 'scientific'}
    assert res['profiles']['default']['available'] is False
    assert res['profiles']['scientific']['available'] is False


def test_select_profile_guards_top_level_available_legacy_flat_and_non_bool_export(tmp_path, monkeypatch):
    sha = draft(tmp_path)
    sci_id = 'sha256:' + '6' * 64
    service = runner.AnalysisRunner(tmp_path)

    # Non-bool export_outputs rejected in run() before probe.
    monkeypatch.setattr(runner, 'probe', lambda *a: pytest.fail('must reject before probe'))
    with pytest.raises(ToolError) as exc_bool:
        service.run('calc.py', sha, [], export_outputs='yes')
    assert exc_bool.value.error_code == 'ANALYSIS_ARGUMENT'

    # Top-level available=False must refuse even if nested profile claims available=True.
    monkeypatch.setattr(runner, 'probe', lambda workspace=None: {
        'available': False, 'docker': '/usr/bin/docker', 'reason': 'daemon down',
        'profiles': {'default': {'available': False, 'image_id': None},
                     'scientific': {'available': True, 'image': runner.SCIENTIFIC_IMAGE, 'image_id': sci_id}},
    })
    blocked = service.run('calc.py', sha, [], image_profile='scientific')
    assert blocked['status'] == 'sandbox_unavailable' and 'docker' not in blocked['runtime']

    # Legacy flat probe monkeypatch must refuse scientific profile and supply reason on invalid default id.
    monkeypatch.setattr(runner, 'probe', lambda: {'available': True, 'docker': '/usr/bin/docker', 'image_id': sci_id})
    legacy_sci = service.run('calc.py', sha, [], image_profile='scientific')
    assert legacy_sci['status'] == 'sandbox_unavailable' and 'docker' not in legacy_sci['runtime']
    monkeypatch.setattr(runner, 'probe', lambda: {'available': True, 'docker': '/usr/bin/docker', 'image_id': 'bad-id'})
    legacy_bad = service.run('calc.py', sha, [], image_profile='default')
    assert legacy_bad['status'] == 'sandbox_unavailable' and legacy_bad['runtime'].get('reason')
    assert 'docker' not in legacy_bad['runtime']


def test_concurrent_file_exists_still_checks_signature_conflict(tmp_path, monkeypatch):
    sha = draft(tmp_path)
    default_id = 'sha256:' + '1' * 64
    sci_id = 'sha256:' + '2' * 64
    monkeypatch.setattr(runner, 'probe', lambda workspace=None: {
        'available': True, 'docker': 'docker', 'image_id': default_id, 'network': False,
        'profiles': {
            'default': {'available': True, 'image': runner.IMAGE, 'image_id': default_id},
            'scientific': {'available': True, 'image': runner.SCIENTIFIC_IMAGE, 'image_id': sci_id},
        },
    })
    service = runner.AnalysisRunner(tmp_path)
    run_id = hashlib.sha256(b'race-id').hexdigest()[:32]
    receipt_path = agent_paths.store_item(tmp_path, 'analysis_runs', run_id + '.json')

    # Simulate another process creating the receipt with a different signature right before open('x').
    real_snapshot = service.snapshot
    def racing_snapshot(*a, **kw):
        res = real_snapshot(*a, **kw)
        receipt_path.parent.mkdir(parents=True, exist_ok=True)
        receipt_path.write_text(json.dumps({
            'run_id': run_id, 'signature': 'different-sig', 'status': 'completed', 'content_is_untrusted': True,
        }), encoding='utf-8')
        return res
    monkeypatch.setattr(service, 'snapshot', racing_snapshot)
    with pytest.raises(ToolError) as exc:
        service.run('calc.py', sha, [], image_profile='scientific', request_id='race-id')
    assert exc.value.error_code == 'ANALYSIS_CONFLICT'
