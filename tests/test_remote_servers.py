import json
from types import SimpleNamespace as NS

import pytest

import vasp_agent.remote.remote_servers as remote


def test_import_persist_and_reuse(tmp_path):
    script = tmp_path / 'login.bat'
    script.write_text('@echo off\nssh -i %USERPROFILE%\\.ssh\\key -p 65082 user@host.example\n')
    servers = remote.Servers(tmp_path / 'workspace')
    record = servers.register('hpc', script)
    assert record['port'] == 65082
    assert record['host'] == 'host.example'
    assert remote.Servers(tmp_path / 'workspace').load('hpc')['user'] == 'user'
    assert servers.register('hpc', script)['already_registered']


@pytest.mark.parametrize('command', [
    'ssh user@host; rm -rf /', 'ssh -o ProxyCommand=evil host', 'ssh user@host uname',
    'sshpass -p password ssh user@host', '-----BEGIN OPENSSH PRIVATE KEY-----'])
def test_reject_dangerous_login_file(tmp_path, command):
    script = tmp_path / 'login.sh'
    script.write_text(command)
    with pytest.raises(ValueError):
        remote.parse_login(script)


def test_remote_path_is_shell_quoted(tmp_path, monkeypatch):
    script = tmp_path / 'login.sh'
    script.write_text('ssh user@host')
    servers = remote.Servers(tmp_path)
    servers.register('hpc', script)
    seen = []
    def run(args, **kwargs):
        seen.append(args)
        return NS(returncode=0, stdout='ok', stderr='')
    monkeypatch.setattr(remote.subprocess, 'run', run)
    servers.operate('hpc', 'list', '/tmp/a;touch BAD')
    assert seen[0][-1] == "ls -la -- '/tmp/a;touch BAD'"
    assert 'StrictHostKeyChecking=yes' in seen[0]
    with pytest.raises(ValueError):
        servers.operate('hpc', 'read', '/home/user/.ssh/id_rsa')


def test_config_name_cannot_escape(tmp_path):
    with pytest.raises(ValueError):
        remote.Servers(tmp_path).file('../escape')
