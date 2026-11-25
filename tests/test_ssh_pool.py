"""Pooled SSH transport against a fake Paramiko: no network, no real keys."""
import json
import threading
from types import SimpleNamespace as NS

import pytest

from vasp_agent.remote import remote_compute, ssh_pool
from vasp_agent.remote.remote_compute import RemoteCompute
from vasp_agent.remote.remote_servers import Servers
from vasp_agent.workflow.vasp_band_agent import ToolError


class SSHException(Exception):
    pass


class BadHostKeyException(SSHException):
    pass


class AuthenticationException(SSHException):
    pass


class RejectPolicy:
    pass


class SSHConfig:
    """Enough of ssh_config for Host blocks; keys lowercased like Paramiko's."""
    def __init__(self):
        self.blocks = []

    def parse(self, stream):
        for line in stream.read().splitlines():
            words = line.split()
            if not words or words[0].startswith('#'):
                continue
            key, value = words[0].lower(), ' '.join(words[1:])
            if key == 'host':
                self.blocks.append((value.split(), {}))
            elif key == 'identityfile':
                self.blocks[-1][1].setdefault(key, []).append(value)
            else:
                self.blocks[-1][1].setdefault(key, value)

    def lookup(self, host):
        result = {}
        for patterns, options in self.blocks:
            if host in patterns or '*' in patterns:
                for key, value in options.items():
                    result.setdefault(key, value)
        result.setdefault('hostname', host)
        return result


class Channel:
    def __init__(self, world):
        self.world, self.command, self.stdin = world, None, b''
        self.out, self.err, self.code = b'', b'', None
        self.closed = False
        self.eof_received = False

    def settimeout(self, value):
        self.timeout = value

    def exec_command(self, command):
        self.command = command
        self.world.execs.append(command)

    def sendall(self, data):
        self.stdin += data

    def shutdown_write(self):
        reply = self.world.handler(self.command, self.stdin.decode('utf-8'))
        if reply == 'drop':
            self.closed = True
        elif reply == 'hang':
            pass
        else:
            self.out, self.err, self.code = reply
            self.eof_received = True

    def recv_ready(self):
        return bool(self.out)

    def recv(self, size):
        data, self.out = self.out[:size], self.out[size:]
        return data

    def recv_stderr_ready(self):
        return bool(self.err)

    def recv_stderr(self, size):
        data, self.err = self.err[:size], self.err[size:]
        return data

    def exit_status_ready(self):
        return self.code is not None

    def recv_exit_status(self):
        return self.code

    def close(self):
        self.closed = True


class Transport:
    def __init__(self, world):
        self.world, self.active, self.keepalive = world, True, None

    def is_active(self):
        return self.active

    def set_keepalive(self, value):
        self.keepalive = value

    def open_session(self, timeout=None):
        if self.world.fail_open:
            self.world.fail_open -= 1
            raise EOFError('stale connection')
        channel = Channel(self.world)
        self.world.channels.append(channel)
        return channel


class Client:
    def __init__(self, world):
        self.world, self.transport = world, None
        self.known_hosts, self.policy = [], None

    def load_system_host_keys(self, path):
        self.known_hosts.append(path)

    def set_missing_host_key_policy(self, policy):
        self.policy = policy

    def connect(self, hostname, **kwargs):
        self.world.connects.append(dict(kwargs, hostname=hostname, policy=self.policy,
                                        known_hosts=list(self.known_hosts)))
        if self.world.host_key == 'unknown':
            raise SSHException(f"Server '{hostname}' not found in known_hosts")
        if self.world.host_key == 'changed':
            raise BadHostKeyException('host key changed')
        self.transport = Transport(self.world)
        self.world.transports.append(self.transport)

    def get_transport(self):
        return self.transport

    def close(self):
        if self.transport:
            self.transport.active = False
        self.world.closes += 1


def ok(result):
    return (json.dumps({'ok': True, 'result': result}).encode() + b'\n', b'', 0)


@pytest.fixture
def world(tmp_path, monkeypatch):
    home = tmp_path / 'home' / '.ssh'
    home.mkdir(parents=True)
    (home / 'known_hosts').write_text('hpc.example.org ssh-ed25519 AAAAfake\n')
    state = NS(connects=[], execs=[], channels=[], transports=[], closes=0, host_key='known',
               fail_open=0, handler=lambda command, stdin: ok({'deployed': True, 'version': 'v1'}), home=home)
    fake = NS(SSHClient=lambda: Client(state), RejectPolicy=RejectPolicy, SSHConfig=SSHConfig,
              SSHException=SSHException, BadHostKeyException=BadHostKeyException,
              AuthenticationException=AuthenticationException)
    monkeypatch.setattr(ssh_pool, 'load_paramiko', lambda: fake)
    monkeypatch.setattr(ssh_pool, '_ssh_dir', lambda: home)
    monkeypatch.setattr(ssh_pool, 'POOL', ssh_pool.Pool())
    monkeypatch.setattr(ssh_pool.Pool, '_start_reaper', lambda self: None)
    monkeypatch.setenv(ssh_pool.ENV, 'pooled')
    state.subprocess_calls = []
    def subprocess_path(args, **kwargs):
        state.subprocess_calls.append(args)
        return NS(returncode=0, stdout=json.dumps({'ok': True, 'result': {'via': 'ssh'}}), stderr='')
    monkeypatch.setattr(remote_compute, 'run_ssh', subprocess_path)
    monkeypatch.setattr(remote_compute.subprocess, 'run', subprocess_path)
    return state


@pytest.fixture
def remote(tmp_path):
    servers = tmp_path / 'servers'
    servers.mkdir()
    (servers / 'hpc.json').write_text(json.dumps({'host': 'hpc.example.org', 'user': 'alice', 'port': 2222,
                                                  'identity_file': 'C:/keys/id_ed25519'}))
    rpc = object.__new__(RemoteCompute)
    rpc.config, rpc.server, rpc.servers = {'python': '/opt/venv/bin/python'}, 'hpc', Servers(tmp_path)
    return rpc


def test_one_connection_is_reused_across_calls(world, remote):
    assert remote.call('deployment_info') == {'deployed': True, 'version': 'v1'}
    assert remote.call('deployment_info')['version'] == 'v1'
    assert len(world.connects) == 1 and len(world.channels) == 2
    assert world.execs == ['/opt/venv/bin/python -X utf8 -'] * 2
    connect = world.connects[0]
    assert connect['hostname'] == 'hpc.example.org' and connect['port'] == 2222 and connect['username'] == 'alice'
    assert connect['key_filename'] == ['C:/keys/id_ed25519'] and connect['look_for_keys'] is False
    assert isinstance(connect['policy'], RejectPolicy)
    assert connect['password'] is None and connect['passphrase'] is None
    assert connect['timeout'] == 30 and world.transports[0].keepalive == 15
    assert connect['known_hosts'] == [str(world.home / 'known_hosts')]
    # Same stdin program as the subprocess path.
    assert '"action": "deployment_info"' in world.channels[0].stdin.decode()
    assert world.subprocess_calls == []


def test_alias_from_ssh_config_resolves_like_ssh(world, remote, tmp_path):
    (world.home / 'config').write_text('Host hpc\n  HostName login.example.org\n  Port 2200\n'
                                       '  User bob\n  IdentityFile ~/.ssh/cluster\n')
    (tmp_path / 'servers' / 'hpc.json').write_text(json.dumps({'host': 'hpc', 'user': None, 'port': 22, 'identity_file': None}))
    remote.call('deployment_info')
    connect = world.connects[0]
    assert (connect['hostname'], connect['port'], connect['username']) == ('login.example.org', 2200, 'bob')
    assert connect['key_filename'][0].endswith('cluster')


@pytest.mark.parametrize('host_key', ['unknown', 'changed'])
def test_rejected_host_key_never_runs_and_falls_back_to_strict_ssh(world, remote, host_key, capsys):
    world.host_key = host_key
    assert remote.call('deployment_info') == {'via': 'ssh'}
    assert remote.call('deployment_info') == {'via': 'ssh'}
    assert world.execs == [] and world.channels == []
    assert len(world.connects) == 1  # not retried for this server in this process
    assert all('StrictHostKeyChecking=yes' in args for args in world.subprocess_calls)
    assert capsys.readouterr().err.count('SSH 连接复用不可用') == 1


def test_missing_paramiko_falls_back_once_quietly(world, remote, monkeypatch, capsys):
    def missing():
        raise ImportError('No module named paramiko')
    monkeypatch.setattr(ssh_pool, 'load_paramiko', missing)
    remote.call('deployment_info')
    remote.call('deployment_info')
    assert len(world.subprocess_calls) == 2 and world.connects == []
    err = capsys.readouterr().err
    assert err.count('SSH 连接复用不可用') == 1 and 'paramiko' in err


def test_load_paramiko_silences_transport_logger_noise(monkeypatch, capsys):
    import logging
    import types
    fake_mod = types.ModuleType('paramiko')
    monkeypatch.setitem(__import__('sys').modules, 'paramiko', fake_mod)
    logger = logging.getLogger('paramiko.transport')
    old_handlers, old_propagate, old_level = list(logger.handlers), logger.propagate, logger.level
    try:
        logger.handlers.clear()
        logger.propagate = True
        logger.setLevel(logging.NOTSET)
        assert ssh_pool.load_paramiko() is fake_mod
        assert logger.propagate is False and logger.level > logging.CRITICAL
        try:
            raise RuntimeError('Error reading SSH protocol banner')
        except RuntimeError:
            logger.error('Exception (client): Error reading SSH protocol banner', exc_info=True)
        captured = capsys.readouterr()
        assert captured.err == '' and captured.out == ''
    finally:
        logger.handlers[:] = old_handlers
        logger.propagate = old_propagate
        logger.setLevel(old_level)


@pytest.mark.parametrize('line', ['ProxyJump bastion', 'ProxyCommand ssh -W %h:%p bastion'])
def test_proxy_config_falls_back(world, remote, line):
    (world.home / 'config').write_text(f'Host hpc.example.org\n  {line}\n')
    assert remote.call('deployment_info') == {'via': 'ssh'}
    assert world.connects == []


def test_revoked_known_hosts_falls_back(world, remote):
    (world.home / 'known_hosts').write_text('@revoked hpc.example.org ssh-rsa AAAA\n')
    assert remote.call('deployment_info') == {'via': 'ssh'} and world.connects == []


def test_forced_subprocess_setting(world, remote, monkeypatch):
    monkeypatch.delenv(ssh_pool.ENV)
    monkeypatch.setattr(ssh_pool, '_mode', 'pooled')
    ssh_pool.configure('subprocess')
    try:
        assert remote.call('deployment_info') == {'via': 'ssh'} and world.connects == []
    finally:
        ssh_pool.configure('pooled')
    assert remote.call('deployment_info')['version'] == 'v1'
    with pytest.raises(ValueError):
        ssh_pool.configure('fast')


def test_drop_before_send_reconnects(world, remote):
    remote.call('deployment_info')
    world.transports[0].active = False  # idle connection died
    assert remote.call('deployment_info')['version'] == 'v1'
    assert len(world.connects) == 2 and len(world.execs) == 2
    # A transport that still claims to be up but cannot open a session.
    world.fail_open = 1
    assert remote.call('deployment_info')['version'] == 'v1'
    assert len(world.connects) == 3 and len(world.execs) == 3
    assert world.subprocess_calls == []


def test_drop_after_send_raises_without_resend(world, remote):
    remote.call('deployment_info')
    world.handler = lambda command, stdin: 'drop'
    with pytest.raises(ToolError) as caught:
        remote.call('submit_checked', 'si', limits={}, phase='scf')
    assert caught.value.error_code.startswith('SSH_')
    assert 'not resent' in str(caught.value)
    assert len(world.execs) == 2  # the one request, never again
    assert world.subprocess_calls == []  # and not replayed over the ssh path either


def test_drop_after_send_reaches_read_only_recovery(world, remote, tmp_path):
    from vasp_agent.chat.ai_agent import Toolbox
    box = Toolbox(tmp_path)
    actions = []
    def handler(command, stdin):
        action = json.loads(stdin.split('io.StringIO(', 1)[1].split(')\n', 1)[0].strip("'"))['action']
        actions.append(action)
        if action == 'submit_checked':
            return 'drop'
        return ok({'already_registered': True, 'status': 'submitted', 'job_id': '42'})
    world.handler = handler
    box.track_submission = lambda server, job, phase, result: result
    box.consume_submission = lambda: None
    result = box._submit_call(remote, 'hpc', 'si', 'scf',
                              lambda: remote.call('submit_checked', 'si', limits={}, phase='scf'))
    assert actions == ['submit_checked', 'resource_context']
    assert result['record_recovered_after_error'] and result['job_id'] == '42'


def test_timeout_maps_to_remote_timeout_and_keeps_connection(world, remote, monkeypatch):
    monkeypatch.setitem(remote_compute.CALL_TIMEOUTS, 'deployment_info', 0.2)
    world.handler = lambda command, stdin: 'hang'
    with pytest.raises(ToolError) as caught:
        remote.call('deployment_info')
    assert caught.value.error_code == 'REMOTE_TIMEOUT'
    assert world.channels[0].closed and world.subprocess_calls == []
    world.handler = lambda command, stdin: ok({'version': 'v2'})
    assert remote.call('deployment_info') == {'version': 'v2'}
    assert len(world.connects) == 1


def test_invalid_response_keeps_existing_code(world, remote):
    world.handler = lambda command, stdin: (b'not json\n', b'Traceback ...', 1)
    with pytest.raises(ToolError) as caught:
        remote.call('deployment_info')
    assert caught.value.error_code == 'REMOTE_RESPONSE_INVALID'
    assert 'Traceback ...' in str(caught.value)


def test_invalid_response_empty_stderr_includes_stdout_diagnostics(world, remote):
    truncated = '{"ok": true, "result": {"data": "' + ('A' * 300)
    world.handler = lambda command, stdin: (truncated.encode('utf-8'), b'', 0)
    with pytest.raises(ToolError) as caught:
        remote.call('fetch', 'si')
    assert caught.value.error_code == 'REMOTE_RESPONSE_INVALID'
    message = str(caught.value)
    assert 'rc=0' in message
    assert f'stdout_len={len(truncated)}' in message
    assert 'stdout_tail=' in message
    assert 'A' * 50 in message


def test_exchange_waits_for_eof_after_early_exit_status():
    """OpenSSH can send exit-status before finishing stdout/stderr packets and EOF."""
    import time

    expected_payload = json.dumps({'ok': True, 'result': {'blob': 'Z' * (400 * 1024)}}).encode('utf-8') + b'\n'
    chunk_size = 32768
    stdout_chunks = [expected_payload[i:i + chunk_size] for i in range(0, len(expected_payload), chunk_size)]
    stderr_chunks = [b'warn-1\n', b'warn-2\n']

    class DelayedEofChannel:
        def __init__(self):
            self.closed = False
            self.eof_received = False
            self.code = None
            self.out = b''
            self.err = b''
            self.poll_checks = 0
            self.remaining_out = list(stdout_chunks)
            self.remaining_err = list(stderr_chunks)

        def settimeout(self, value):
            self.timeout = value

        def exec_command(self, command):
            self.command = command

        def sendall(self, data):
            pass

        def shutdown_write(self):
            # Deliver exit-status and only the first stdout chunk right away;
            # remaining packets arrive over subsequent poll cycles before EOF.
            self.code = 0
            self.out += self.remaining_out.pop(0)

        def _advance(self):
            if self.code is None or self.eof_received:
                return
            self.poll_checks += 1
            if self.remaining_out:
                self.out += self.remaining_out.pop(0)
            if self.remaining_err and self.poll_checks % 3 == 0:
                self.err += self.remaining_err.pop(0)
            if not self.remaining_out and not self.remaining_err:
                # Set EOF alongside the final buffered chunk to verify post-EOF drain.
                self.eof_received = True

        def recv_ready(self):
            return bool(self.out)

        def recv(self, size):
            chunk, self.out = self.out[:size], self.out[size:]
            return chunk

        def recv_stderr_ready(self):
            return bool(self.err)

        def recv_stderr(self, size):
            chunk, self.err = self.err[:size], self.err[size:]
            return chunk

        def exit_status_ready(self):
            if self.code is not None and not self.out and not self.err and not self.eof_received:
                self._advance()
            return self.code is not None

        def recv_exit_status(self):
            return self.code

        def close(self):
            self.closed = True

    pool = ssh_pool.Pool()
    entry = ssh_pool.Entry({})
    entry.client = NS(get_transport=lambda: NS(is_active=lambda: True))
    fake_paramiko = NS(SSHException=SSHException)
    chan = DelayedEofChannel()
    proc = pool._exchange(fake_paramiko, entry, chan, 'python -', 'print(1)', time.monotonic() + 5.0, 5.0)
    assert proc.returncode == 0
    assert proc.stdout.encode('utf-8') == expected_payload
    assert proc.stderr == 'warn-1\nwarn-2\n'
    assert json.loads(proc.stdout)['result']['blob'] == 'Z' * (400 * 1024)


def test_exchange_times_out_when_eof_never_arrives_after_exit_status():
    """If exit-status arrives but remote keeps stdout open without EOF, deadline still bounds _exchange."""
    import subprocess
    import time

    class MissingEofChannel:
        def __init__(self):
            self.closed = False
            self.eof_received = False
            self.code = None
            self.out = b''

        def settimeout(self, value):
            self.timeout = value

        def exec_command(self, command):
            self.command = command

        def sendall(self, data):
            pass

        def shutdown_write(self):
            self.code = 0
            self.out = b'partial-output'

        def recv_ready(self):
            return bool(self.out)

        def recv(self, size):
            chunk, self.out = self.out[:size], self.out[size:]
            return chunk

        def recv_stderr_ready(self):
            return False

        def recv_stderr(self, size):
            return b''

        def exit_status_ready(self):
            return self.code is not None

        def recv_exit_status(self):
            return self.code

        def close(self):
            self.closed = True

    pool = ssh_pool.Pool()
    entry = ssh_pool.Entry({})
    entry.client = NS(get_transport=lambda: NS(is_active=lambda: True))
    fake_paramiko = NS(SSHException=SSHException)
    chan = MissingEofChannel()
    with pytest.raises(subprocess.TimeoutExpired):
        pool._exchange(fake_paramiko, entry, chan, 'python -', 'print(1)', time.monotonic() + 0.15, 0.15)
    assert chan.closed


def test_monitor_and_main_thread_share_the_connection_safely(world, remote):
    barrier = threading.Barrier(2, timeout=5)
    def handler(command, stdin):
        barrier.wait()  # both calls hold an open channel at the same time
        return ok({'action': 'monitor_status' if '"action": "monitor_status"' in stdin else 'deployment_info'})
    world.handler = handler
    results, errors = {}, []
    def run(action, *args, **kwargs):
        try:
            results[action] = remote.call(action, *args, **kwargs)
        except Exception as exc:
            errors.append(exc)
    threads = [threading.Thread(target=run, args=('monitor_status', 'si'), kwargs={'job_id': '1', 'phase': 'scf'}),
               threading.Thread(target=run, args=('deployment_info',))]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(10)
    assert errors == []
    assert results == {'monitor_status': {'action': 'monitor_status'}, 'deployment_info': {'action': 'deployment_info'}}
    # One connection established under the entry lock, two separate channels.
    assert len(world.connects) == 1 and len(world.channels) == 2
    assert world.channels[0] is not world.channels[1]


def test_idle_connections_close_and_exit_closes_all(world, remote):
    remote.call('deployment_info')
    pool = ssh_pool.POOL
    entry = next(iter(pool.entries.values()))
    assert pool.close_idle(now=entry.last_used + ssh_pool.IDLE_CLOSE - 1) == 0
    entry.active = 1
    assert pool.close_idle(now=entry.last_used + ssh_pool.IDLE_CLOSE) == 0  # busy
    entry.active = 0
    assert pool.close_idle(now=entry.last_used + ssh_pool.IDLE_CLOSE + 0.01) == 1  # float-safe boundary
    assert not world.transports[0].active and pool.entries == {}
    remote.call('deployment_info')
    ssh_pool.close_all()
    assert not world.transports[1].active and pool.entries == {}


def test_benchmark_times_both_transports_with_read_only_calls(world, remote, monkeypatch, capsys):
    from vasp_agent.remote import ssh_bench
    monkeypatch.setattr(ssh_bench, 'RemoteCompute', lambda workspace, server: remote)
    results = ssh_bench.main(['--server', 'hpc', '-n', '3'])
    subprocess_run, pooled = results
    assert subprocess_run['transport'] == 'subprocess' and len(subprocess_run['seconds']) == 3
    assert pooled['connection_reused'] and len(pooled['seconds']) == 3 and 'median_after_first' in pooled
    assert len(world.subprocess_calls) == 3 and len(world.connects) == 1
    assert world.execs and all('"action": "deployment_info"' in channel.stdin.decode() for channel in world.channels)
    assert json.loads(capsys.readouterr().out)['action'] == 'deployment_info'
    assert ssh_pool.os.environ[ssh_pool.ENV] == 'pooled'  # restored for the rest of the process


# -- Servers.operate and server_setup.run_remote ------------------------------

@pytest.fixture
def ops(world, monkeypatch):
    from vasp_agent.remote import remote_servers, server_setup
    world.ssh_calls = []
    world.ssh_reply = NS(returncode=0, stdout='via-ssh\n', stderr='')
    def run_ssh(args, **kwargs):
        world.ssh_calls.append((args, kwargs))
        return world.ssh_reply
    monkeypatch.setattr(remote_servers, 'run_ssh', run_ssh)
    monkeypatch.setattr(server_setup, 'run_ssh', run_ssh)
    world.handler = lambda command, stdin: (b'login01\nalice\n', b'', 0)
    return world


def test_operate_reuses_the_connection_without_stdin(ops, remote):
    servers = remote.servers
    first = servers.operate('hpc', 'info')
    second = servers.operate('hpc', 'list', '/home/alice/jobs')
    assert first['output'] == 'login01\nalice\n' and first['connection_verified']
    assert second['action'] == 'list'
    assert len(ops.connects) == 1 and len(ops.channels) == 2
    assert ops.execs[0].startswith('hostname; whoami') and ops.execs[1] == "ls -la -- /home/alice/jobs"
    assert all(channel.stdin == b'' for channel in ops.channels)  # nothing on stdin, just EOF
    assert ops.ssh_calls == []
    # The same connection serves RemoteCompute calls too.
    ops.handler = lambda command, stdin: ok({'version': 'v1'})
    assert remote.call('deployment_info') == {'version': 'v1'} and len(ops.connects) == 1


@pytest.mark.parametrize('host_key', ['unknown', 'changed'])
def test_operate_falls_back_to_strict_ssh_within_budget(ops, remote, host_key):
    ops.host_key = host_key
    result = remote.servers.operate('hpc', 'jobs')
    assert result['output'] == 'via-ssh\n' and ops.execs == []
    (args, kwargs), = ops.ssh_calls
    assert 'StrictHostKeyChecking=yes' in args and args[-1].startswith('squeue --me')
    assert 0 < kwargs['timeout'] <= 120 and 'input' not in kwargs


def test_operate_missing_paramiko_falls_back(ops, remote, monkeypatch):
    def missing():
        raise ImportError('No module named paramiko')
    monkeypatch.setattr(ssh_pool, 'load_paramiko', missing)
    assert remote.servers.operate('hpc', 'info')['output'] == 'via-ssh\n'
    assert len(ops.ssh_calls) == 1 and ops.connects == []


@pytest.mark.parametrize('stderr,code', [
    ('ls: cannot access /nope: No such file or directory', 'SSH_FAILED'),
    ('Permission denied (publickey).', 'SSH_AUTH_FAILED'),
    ('Host key verification failed.', 'SSH_HOST_KEY_FAILED'),
])
def test_operate_error_codes_match_the_ssh_path(ops, remote, monkeypatch, stderr, code):
    ops.handler = lambda command, stdin: (b'', stderr.encode(), 2)
    with pytest.raises(ToolError) as pooled:
        remote.servers.operate('hpc', 'list', '/nope')
    monkeypatch.setenv(ssh_pool.ENV, 'subprocess')
    ops.ssh_reply = NS(returncode=2, stdout='', stderr=stderr)
    with pytest.raises(ToolError) as subprocess_path:
        remote.servers.operate('hpc', 'list', '/nope')
    assert pooled.value.error_code == subprocess_path.value.error_code == code
    assert pooled.value.connection == subprocess_path.value.connection


def test_operate_timeout_and_drop_keep_their_codes(ops, remote, monkeypatch):
    from vasp_agent.remote import remote_servers
    monkeypatch.setattr(remote_servers, 'OPERATE_TIMEOUT', 0.2)
    ops.handler = lambda command, stdin: 'hang'
    with pytest.raises(ToolError) as caught:
        remote.servers.operate('hpc', 'info')
    assert caught.value.error_code == 'SSH_CONNECTION_FAILED' and ops.channels[0].closed
    ops.handler = lambda command, stdin: 'drop'
    with pytest.raises(ToolError) as caught:
        remote.servers.operate('hpc', 'jobs')
    assert caught.value.error_code == 'SSH_FAILED' and 'not resent' in str(caught.value)
    assert ops.ssh_calls == [] and len(ops.execs) == 2


def test_server_setup_text_programs_pool_binary_uploads_use_ssh(ops, remote, tmp_path):
    from vasp_agent.remote import server_setup
    seen = []
    ops.handler = lambda command, stdin: seen.append((command, stdin)) or (b'/opt/vasp/bin/vasp_std\n', b'', 0)
    result = server_setup.install_env_script(tmp_path, 'hpc', '/home/alice/vasp-agent/env.sh', 'module load vasp\n')
    assert result['vasp_std'] == '/opt/vasp/bin/vasp_std'
    assert seen[0][0] == 'bash -s' and "cat > /home/alice/vasp-agent/env.sh" in seen[0][1]
    assert ops.ssh_calls == []
    wheels = tmp_path / 'wheels'
    wheels.mkdir()
    (wheels / 'numpy-2.0-cp311-manylinux_x86_64.whl').write_bytes(b'wheel')
    ops.ssh_reply = NS(returncode=0, stdout=b'', stderr=b'')
    server_setup.upload_wheels(tmp_path, 'hpc', wheels, '/home/alice/vasp-agent/wheels')
    (args, kwargs), = ops.ssh_calls
    assert isinstance(kwargs['input'], bytes) and len(ops.connects) == 1
    # A connection lost after the program was sent is an SSH error, not resent.
    ops.handler = lambda command, stdin: 'drop'
    with pytest.raises(ToolError) as caught:
        server_setup.run_remote(tmp_path, 'hpc', 'echo hi\n', 60)
    assert caught.value.error_code == 'SSH_FAILED' and len(ops.ssh_calls) == 1
