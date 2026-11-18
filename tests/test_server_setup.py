import json
from types import SimpleNamespace as NS

import pytest

import vasp_agent.remote.remote_servers as remote_servers
from vasp_agent.chat import setup_commands
from vasp_agent.remote import server_setup


PROBE_OUTPUT = '\n'.join([
    'HOME\t/home/alice',
    'SBATCH\t/usr/bin/sbatch',
    'PY\t/usr/bin/python3\t3.6.8\t-\t1.19.5\t-\tx86_64\tglibc-2.17',
    'PY\t/home/alice/miniconda3/envs/vasp/bin/python\t3.11.9\t2026.5.4\t2.2.3\t3.9.2\tx86_64\tglibc-2.28',
    'PY\t/opt/broken/python\t',
    'PYMOD\tanaconda3/2023.09',
    'VASPMOD\tvasp/6.4.2-intel',
    'PART\tcpu*|64|1-00:00:00|up',
    'PART\tcpu*|48|1-00:00:00|up',
    'PART\tgpu|32|infinite|up',
    'PART\tbad;name|8|1:00:00|up',
    'PCONF\tPartitionName=cpu AllowGroups=ALL Default=YES DefaultTime=04:00:00 MaxTime=1-00:00:00',
    'PCONF\tPartitionName=gpu Default=NO DefaultTime=NONE MaxTime=UNLIMITED',
    'VASPBIN\t',
    'VASPENV\t/opt/vasp/env-vasp6.sh\t/opt/vasp/6.4/bin/vasp_std',
    'VASPENV\t/home/alice/old-vasp-env.sh\t',
    'POTSI\t/public/software/potpaw_PBE.64/Si/POTCAR',
    'POTSI\t/home/alice/potpaw_LDA/Si/POTCAR',
    'POTSI\t/public/software/potpaw_PBE.64/Si/POTCAR',
    'NET\tno',
    'END',
]) + '\n'


@pytest.fixture(autouse=True)
def deployed_engine_is_current(monkeypatch):
    """The post-setup deploy offer queries the server; these tests answer for it."""
    from vasp_agent.chat import release_check
    monkeypatch.setattr(release_check, 'check', lambda box, server: {
        'current': True, 'deployed': True, 'local_version': 'v1', 'deployed_version': 'v1'})


BOOTSTRAP_OUTPUT = """@@CREATED
@@BEFORE
@@INSTALL
Successfully installed matplotlib-3.9.2 numpy-2.2.3 pymatgen-2026.5.4 ...
@@AFTER
matplotlib==3.9.2
numpy==2.2.3
pymatgen==2026.5.4
ruamel.yaml==0.18.6
@@IMPORT
ok
@@END
"""


def test_slurm_time_formats():
    assert server_setup.slurm_time('1-00:00:00') == '24:00:00'
    assert server_setup.slurm_time('2-12') == '60:00:00'
    assert server_setup.slurm_time('04:00:00') == '04:00:00'
    assert server_setup.slurm_time('90') == '01:30:00'
    assert server_setup.slurm_time('30:15') == '00:30:15'
    for unlimited in ('infinite', 'UNLIMITED', 'NONE', '', '50-00:00:00'):
        assert server_setup.slurm_time(unlimited) is None


def test_parse_probe_collects_candidates():
    found = server_setup.parse_probe(PROBE_OUTPUT)
    assert found['complete'] and found['network'] is False and found['home'] == '/home/alice'
    assert [p['path'] for p in found['pythons']] == ['/home/alice/miniconda3/envs/vasp/bin/python', '/usr/bin/python3']
    ready, old = found['pythons']
    assert ready['ready'] and ready['pymatgen'] == '2026.5.4' and ready['libc'] == 'glibc-2.28'
    assert not old['ready'] and old['numpy'] == '1.19.5' and old['pymatgen'] is None
    cpu, gpu = found['partitions']
    # Mixed node types keep the smallest so nodes=1 jobs fit on any node.
    assert cpu == {'name': 'cpu', 'default': True, 'cores': 48, 'max_time': '24:00:00', 'state': 'up', 'default_time': '04:00:00'}
    assert gpu['max_time'] is None and gpu['default_time'] is None and not gpu['default']
    assert [e['path'] for e in found['vasp_envs']] == ['/opt/vasp/env-vasp6.sh']
    assert found['vasp_modules'] == ['vasp/6.4.2-intel'] and found['python_modules'] == ['anaconda3/2023.09']
    assert [lib['path'] for lib in found['potcar_libraries']] == ['/public/software/potpaw_PBE.64', '/home/alice/potpaw_LDA']


def test_parse_probe_marks_truncated_output():
    assert not server_setup.parse_probe('HOME\t/home/alice\n')['complete']


def test_probe_program_carries_current_values_quoted():
    program = server_setup.probe_program({'root': "/home/a/it's", 'python': 'relative/python'})
    assert program.startswith("ROOT='/home/a/it'\"'\"'s'\nCURRENT_PYTHON=''\n")
    assert 'cat ' not in program.split('POTSI')[0].split('VASPENV')[-1]  # POTCARs are only tested for existence


def valid_cfg(**changes):
    cfg = {'root': '/home/alice/vasp-agent', 'python': '/home/alice/miniconda3/envs/vasp/bin/python',
           'potcar_library': '/public/software/potpaw_PBE.64', 'vasp_env': '/opt/vasp/env-vasp6.sh',
           'partition': 'cpu', 'ntasks': 48, 'walltime': '04:00:00'}
    cfg.update(changes)
    return cfg


@pytest.mark.parametrize('changes', [
    {'python': 'python3'}, {'root': '/home/alice'}, {'root': '/home/alice/../../etc'},
    {'partition': 'cpu\n#SBATCH --qos=evil'}, {'walltime': '4h'}, {'walltime': '00:00:00'},
    {'ntasks': 0}, {'ntasks': '8'}, {'vasp_env': ''}])
def test_validate_compute_rejects(changes):
    with pytest.raises(ValueError):
        server_setup.validate_compute(valid_cfg(**changes))


def test_write_compute_backs_up_existing(tmp_path):
    (tmp_path / 'servers').mkdir()
    (tmp_path / 'servers' / 'hpc.json').write_text('{}')
    first = server_setup.write_compute(tmp_path, 'hpc', valid_cfg())
    assert first['backup'] is None
    second = server_setup.write_compute(tmp_path, 'hpc', valid_cfg(ntasks=32))
    third = server_setup.write_compute(tmp_path, 'hpc', valid_cfg(ntasks=16))
    assert json.loads(open(second['backup'], encoding='utf-8').read())['ntasks'] == 48
    assert json.loads(open(third['backup'], encoding='utf-8').read())['ntasks'] == 32
    assert second['backup'] != third['backup']
    assert server_setup.load_compute(tmp_path, 'hpc')['ntasks'] == 16
    with pytest.raises(ValueError):
        server_setup.write_compute(tmp_path, 'hpc', valid_cfg(partition='a b'))
    assert server_setup.load_compute(tmp_path, 'hpc')['ntasks'] == 16


class FakeSSH:
    """Stands in for run_ssh: the probe, then any remote writes."""
    def __init__(self, probe_output=PROBE_OUTPUT, env_output='/opt/vasp/6.4/bin/vasp_std\n'):
        self.calls, self.probe_output, self.env_output = [], probe_output, env_output

    def __call__(self, args, **kwargs):
        self.calls.append((args, kwargs.get('input')))
        assert 'BatchMode=yes' in args
        if isinstance(kwargs.get('input'), bytes):
            assert args[-1].startswith('mkdir -p ') and ' && tar -xzf - -C ' in args[-1]
            return NS(returncode=0, stdout=b'', stderr=b'')
        stdin = kwargs.get('input') or ''
        assert args[-1] == 'bash -s'
        assert 'sudo' not in stdin
        if 'VASP_AGENT_ENV' in stdin:
            return NS(returncode=0, stdout=self.env_output, stderr='')
        if '@@BEFORE' in stdin:
            return NS(returncode=0, stdout=BOOTSTRAP_OUTPUT, stderr='')
        return NS(returncode=0, stdout=self.probe_output, stderr='')


def make_console(tmp_path, answers):
    script = tmp_path / 'login.sh'
    script.write_text('ssh alice@hpc.example')
    remote_servers.Servers(tmp_path).register('hpc', script)
    out = []
    answers = iter(answers)
    return NS(agent=NS(tools=NS(workspace=tmp_path)), reader=lambda prompt: next(answers), output=out.append), out


def test_wizard_defaults_write_profile(tmp_path, monkeypatch):
    ssh = FakeSSH()
    monkeypatch.setattr(server_setup, 'run_ssh', ssh)
    c, out = make_console(tmp_path, [''] * 7 + ['写入'])
    setup_commands.execute(c, 'hpc')
    assert server_setup.load_compute(tmp_path, 'hpc') == valid_cfg()
    assert len(ssh.calls) == 1  # only the read-only probe
    text = '\n'.join(out)
    assert '未读取 POTCAR 内容' in text and '← 新增' in text


def test_wizard_cancel_writes_nothing(tmp_path, monkeypatch):
    ssh = FakeSSH()
    monkeypatch.setattr(server_setup, 'run_ssh', ssh)
    c, out = make_console(tmp_path, [''] * 7 + ['不'])
    setup_commands.execute(c, 'hpc')
    assert server_setup.load_compute(tmp_path, 'hpc') is None
    c, out = make_console(tmp_path, ['', 'q'])
    setup_commands.execute(c, 'hpc')
    assert server_setup.load_compute(tmp_path, 'hpc') is None and out[-1] == '已取消；未写入任何文件。'
    assert len(ssh.calls) == 2


def test_wizard_manual_entries_and_module_script(tmp_path, monkeypatch):
    ssh = FakeSSH()
    monkeypatch.setattr(server_setup, 'run_ssh', ssh)
    c, out = make_console(tmp_path, [])
    old = valid_cfg(root='/work/alice/agent', partition='gpu', ntasks=32, walltime='08:00:00', parallel_tests=2)
    (tmp_path / 'servers' / 'hpc.compute.json').write_text(json.dumps(old))
    answers = iter(['',                              # root: keep current
                    'e', 'python3', 'e', '/opt/py/bin/python3',  # bad then good manual entry
                    '2',                             # VASP module -> generated script
                    '2',                             # second POTCAR candidate
                    '1',                             # partition cpu
                    'e', '300', 'e', '24',           # cores: out of range, then 24
                    '',                              # walltime: current 08:00:00
                    '写入'])
    c.reader = lambda prompt: next(answers)
    setup_commands.execute(c, 'hpc')
    cfg = server_setup.load_compute(tmp_path, 'hpc')
    assert cfg == {**old, 'python': '/opt/py/bin/python3', 'vasp_env': '/work/alice/agent/vasp-env.sh',
                   'potcar_library': '/home/alice/potpaw_LDA', 'partition': 'cpu', 'ntasks': 24}
    backups = list((tmp_path / 'servers').glob('hpc.compute.json.bak-*'))
    assert len(backups) == 1 and json.loads(backups[0].read_text()) == old
    text = '\n'.join(out)
    assert '需要 Linux 绝对路径' in text and '核数须为 1–256' in text
    env_call = ssh.calls[-1][1]
    assert "cat > /work/alice/agent/vasp-env.sh <<'VASP_AGENT_ENV'" in env_call
    assert 'module load vasp/6.4.2-intel' in env_call
    assert '解析到 vasp_std：/opt/vasp/6.4/bin/vasp_std' in text


def test_wizard_requires_connection(tmp_path):
    out = []
    c = NS(agent=NS(tools=NS(workspace=tmp_path)), reader=lambda p: '', output=out.append)
    setup_commands.execute(c, '')
    assert '尚无服务器连接' in out[0]
    with pytest.raises(ValueError, match='没有服务器连接'):
        setup_commands.execute(c, 'nope')


def test_engine_requirements_reads_pins(tmp_path):
    assert server_setup.engine_requirements() == ['pymatgen==2026.5.4', 'numpy==2.2.3', 'matplotlib==3.9.2']
    pins = tmp_path / 'req.txt'
    pins.write_text('# note\nrich==15.0.0\nnumpy==2.2.3\npymatgen==2026.5.4\n')
    with pytest.raises(ValueError, match='matplotlib'):
        server_setup.engine_requirements(pins)


def test_wheel_platforms_follow_glibc():
    tags = server_setup.wheel_platforms('x86_64', 'glibc-2.28')
    assert tags[0] == 'manylinux_2_28_x86_64' and 'manylinux_2_17_x86_64' in tags and tags[-1] == 'manylinux2014_x86_64'
    assert server_setup.wheel_platforms('aarch64', '-') == ['manylinux_2_17_aarch64', 'manylinux2014_aarch64']
    with pytest.raises(ValueError):
        server_setup.wheel_platforms('ppc64le', 'glibc-2.28')
    with pytest.raises(ValueError):
        server_setup.wheel_platforms('x86_64', 'glibc-2.12')


def test_download_wheels_targets_cluster_interpreter(tmp_path):
    seen = []
    def runner(command, **kwargs):
        seen.append(command)
        (tmp_path / 'numpy-2.2.3-cp311-cp311-manylinux_2_17_x86_64.whl').write_bytes(b'w')
        return NS(returncode=0, stdout='', stderr='')
    files = server_setup.download_wheels(tmp_path, ['numpy==2.2.3'], '3.11.9', 'x86_64', 'glibc-2.17', runner)
    command = seen[0]
    assert files == ['numpy-2.2.3-cp311-cp311-manylinux_2_17_x86_64.whl']
    assert command[command.index('--python-version') + 1] == '3.11' and command[command.index('--abi') + 1] == 'cp311'
    assert '--only-binary=:all:' in command and command[-1] == 'numpy==2.2.3'


def test_bootstrap_program_and_report():
    program = server_setup.bootstrap_program('/home/a b/va', '/usr/bin/python3', ['numpy==2.2.3'], '/home/a b/va/wheels/cp311-x86_64')
    assert "VENV='/home/a b/va/venv'" in program and "--find-links '/home/a b/va/wheels/cp311-x86_64'" in program
    assert '--require-virtualenv' in program and 'sudo' not in program
    with pytest.raises(ValueError):
        server_setup.bootstrap_program('relative', '/usr/bin/python3', [])
    report = server_setup.parse_bootstrap(BOOTSTRAP_OUTPUT.replace('@@BEFORE\n', '@@BEFORE\nnumpy==1.26.4\n'))
    assert report['created'] and report['import_ok'] and report['complete']
    assert report['installed'] == {'matplotlib': '3.9.2', 'numpy': '2.2.3', 'pymatgen': '2026.5.4', 'ruamel.yaml': '0.18.6'}
    assert report['previous'] == {'numpy': '1.26.4'}


def test_bootstrap_failure_is_reported(tmp_path, monkeypatch):
    make_console(tmp_path, [])
    monkeypatch.setattr(server_setup, 'run_ssh', lambda args, **kw: NS(
        returncode=1, stdout='@@BEFORE\n@@INSTALL\nERROR: No matching distribution found for pymatgen==2026.5.4\n', stderr=''))
    with pytest.raises(ValueError, match='No matching distribution'):
        server_setup.bootstrap(tmp_path, 'hpc', '/home/alice/vasp-agent', '/usr/bin/python3', ['pymatgen==2026.5.4'])


def test_wizard_online_bootstrap_sets_venv_python(tmp_path, monkeypatch):
    ssh = FakeSSH(PROBE_OUTPUT.replace('NET\tno', 'NET\tyes'))
    monkeypatch.setattr(server_setup, 'run_ssh', ssh)
    c, out = make_console(tmp_path, ['', 'v', '', '', '', '', '', '', '', '写入'])
    setup_commands.execute(c, 'hpc')
    assert server_setup.load_compute(tmp_path, 'hpc')['python'] == '/home/alice/vasp-agent/venv/bin/python'
    program = ssh.calls[1][1]
    assert '/home/alice/miniconda3/envs/vasp/bin/python -m venv' in program and '--no-index' not in program
    text = '\n'.join(out)
    assert '本次安装或变更 4 个包' in text and 'ruamel.yaml' in text
    # /usr/bin/python3 is 3.6: never offered as a venv base.
    assert '3.6.8' not in text.split('选择基础 Python')[1].split('将在')[0]


def test_wizard_offline_bootstrap_uploads_wheels(tmp_path, monkeypatch):
    import io
    import tarfile
    ssh = FakeSSH()
    monkeypatch.setattr(server_setup, 'run_ssh', ssh)
    names = ['numpy-2.2.3-cp311-cp311-manylinux_2_17_x86_64.whl', 'pymatgen-2026.5.4-cp311-cp311-manylinux_2_28_x86_64.whl']
    def download(dest, requirements, version, machine, libc):
        assert (version, machine, libc) == ('3.11.9', 'x86_64', 'glibc-2.28')
        for name in names:
            (tmp_path.joinpath(dest) / name).write_bytes(b'wheel')
        return names
    c, out = make_console(tmp_path, ['', 'v', '', '', '', '', '', '', '', '写入'])
    setup_commands.execute(c, 'hpc', download=download)
    args, data = ssh.calls[1]
    assert args[-1] == ('mkdir -p /home/alice/vasp-agent/wheels/cp311-x86_64 && '
                        'tar -xzf - -C /home/alice/vasp-agent/wheels/cp311-x86_64')
    with tarfile.open(fileobj=io.BytesIO(data)) as archive:
        assert sorted(archive.getnames()) == names
    assert '--no-index --find-links /home/alice/vasp-agent/wheels/cp311-x86_64' in ssh.calls[2][1]
    assert server_setup.load_compute(tmp_path, 'hpc')['python'] == '/home/alice/vasp-agent/venv/bin/python'
    assert '已上传 2 个 wheel' in '\n'.join(out)


def test_wizard_offers_deploy_when_engine_is_stale(tmp_path, monkeypatch):
    from vasp_agent.chat import release_check
    monkeypatch.setattr(server_setup, 'run_ssh', FakeSSH())
    monkeypatch.setattr(release_check, 'check', lambda box, server: {
        'current': False, 'deployed': False, 'local_version': 'abcdef1234567890', 'deployed_version': None})
    c, out = make_console(tmp_path, [''] * 7 + ['写入', '1'])
    deploys = []
    c.agent.tools.remote_deploy = lambda server: deploys.append(server) or {'release': '/r/releases/abcdef1234567890'}
    setup_commands.execute(c, 'hpc')
    assert deploys == ['hpc'] and '尚未部署' in '\n'.join(out) and out[-1] == '已部署 /r/releases/abcdef1234567890。'
