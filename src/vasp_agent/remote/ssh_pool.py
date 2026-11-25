"""One persistent SSH connection per server, reused across remote calls.

Each remote call used to start a new `ssh` process: a fresh handshake (6-16 s on
the cluster) before the worker even started. Windows OpenSSH has no
ControlMaster, so the connection is kept here instead, with Paramiko, the way a
terminal client keeps its session open. Every call still runs on its own
channel: `python -X utf8 -` with the same stdin program as the subprocess path.
run_command() does the same for plain shell commands that read no stdin (remote
info/jobs/ls/tail): their stdin is closed at once, like `ssh -n`.

Parity with Servers.ssh_args, or no pooling at all:
- host keys come from the same known_hosts files and an unknown or changed key is
  rejected (RejectPolicy, never AutoAdd);
- Port/User/HostName/IdentityFile resolve as OpenSSH would for this record
  (explicit -p/-i/user@ first, then ~/.ssh/config);
- no password or passphrase is ever passed, and the agent is never forwarded;
- keepalive every 15 s, connect timeout 30 s.
Anything Paramiko cannot reproduce faithfully (ProxyJump, ProxyCommand,
HostKeyAlias, Match/Include, @revoked or @cert-authority known_hosts lines), a
missing Paramiko, or a failed connect makes run() return None, and the caller
uses the subprocess path, which is reported once per server.

The connection is (re)established only before a request goes out. Once the exec
request for a call has been sent the call is never resent: a drop after that
point comes back as returncode 255, exactly like `ssh` losing its connection,
so the caller's read-only recovery runs as before.
"""
import atexit
import getpass
import os
from pathlib import Path
import re
import subprocess
import sys
import threading
import time

CONNECT_TIMEOUT = 30
KEEPALIVE = 15
IDLE_CLOSE = 4 * 3600  # seconds without a call before a connection is closed (operator choice: 4 h)
# A network failure is often transient; try pooling again after this long.
# Host-key, authentication and configuration refusals stay off for the process.
RETRY_AFTER = 600
MODES = ('pooled', 'subprocess')
ENV = 'VASP_AGENT_SSH_TRANSPORT'
UNSUPPORTED_OPTIONS = ('proxyjump', 'proxycommand', 'hostkeyalias')

_mode = 'pooled'


def load_paramiko():
    import logging
    import paramiko
    # Paramiko's Transport background thread logs raw tracebacks (e.g. transient
    # banner read errors) via logging.lastResort; ssh_pool catches the raised
    # exception and emits a concise fallback notice itself.
    logger = logging.getLogger('paramiko.transport')
    if not any(isinstance(h, logging.NullHandler) for h in logger.handlers):
        logger.addHandler(logging.NullHandler())
    logger.propagate = False
    logger.setLevel(logging.CRITICAL + 1)
    return paramiko


def configure(mode):
    """Select the transport for this process; switching resets fallbacks."""
    global _mode
    if mode not in MODES:
        raise ValueError('ssh_transport 须为 ' + ' / '.join(MODES) + '。')
    _mode = mode
    POOL.reset()


def mode():
    """The environment variable wins, so tests and the benchmark can pin a path."""
    value = os.environ.get(ENV)
    return value if value in MODES else _mode


class Unsupported(Exception):
    """The connection needs something Paramiko would not reproduce faithfully."""


class LostResponse(Exception):
    pass


def _ssh_dir():
    return Path.home() / '.ssh'


def _config_lookup(paramiko, host):
    path = _ssh_dir() / 'config'
    if not path.is_file():
        return {}
    text = path.read_text(encoding='utf-8-sig', errors='replace')
    # Paramiko's Match support can run `exec` commands and it does not follow
    # Include; either way it would not resolve what `ssh` resolves.
    if re.search(r'(?im)^\s*(match|include)\b', text):
        raise Unsupported('~/.ssh/config 使用 Match/Include')
    config = paramiko.SSHConfig()
    from io import StringIO
    config.parse(StringIO(text))
    return config.lookup(host)


def _known_hosts_files(options):
    configured = options.get('userknownhostsfile')
    if configured:
        names = configured if isinstance(configured, list) else str(configured).split()
        files = [Path(os.path.expanduser(name)) for name in names]
    else:
        files = [_ssh_dir() / 'known_hosts', _ssh_dir() / 'known_hosts2']
    if os.name == 'nt':
        files.append(Path(os.environ.get('PROGRAMDATA', r'C:\ProgramData')) / 'ssh' / 'ssh_known_hosts')
    else:
        files.append(Path('/etc/ssh/ssh_known_hosts'))
    existing = [path for path in files if path.is_file()]
    for path in existing:
        # Paramiko skips marker lines; a revocation it ignored would accept a key
        # that `ssh` refuses.
        if re.search(r'(?m)^\s*@(revoked|cert-authority)\b', path.read_text(encoding='utf-8', errors='replace')):
            raise Unsupported(f'{path.name} 含 @revoked/@cert-authority')
    return [str(path) for path in existing]


def resolve(paramiko, record):
    """Connection parameters for one server record, as `ssh <ssh_args>` would use them."""
    options = _config_lookup(paramiko, record['host'])
    for key in UNSUPPORTED_OPTIONS:
        if options.get(key) and str(options[key]).lower() != 'none':
            raise Unsupported(f'~/.ssh/config 使用 {key}')
    # ssh_args passes -p only when the port is non-default or a user is given;
    # otherwise an alias's Port applies.
    port = record['port'] if record['port'] != 22 or record.get('user') else int(options.get('port', 22))
    user = record.get('user') or options.get('user') or getpass.getuser()
    keys = []
    if record.get('identity_file'):
        keys.append(record['identity_file'])
    keys += [os.path.expanduser(name) for name in options.get('identityfile', [])]
    identities_only = str(options.get('identitiesonly', 'no')).lower() == 'yes'
    return {'hostname': options.get('hostname', record['host']), 'port': int(port), 'username': user,
            'key_filename': tuple(dict.fromkeys(keys)), 'look_for_keys': not keys,
            'allow_agent': not identities_only, 'known_hosts': tuple(_known_hosts_files(options))}


class Entry:
    def __init__(self, params):
        self.params = params
        self.client = None
        self.lock = threading.Lock()  # connect/reconnect; channels themselves are per call
        self.active = 0
        self.last_used = time.monotonic()

    def close(self):
        client, self.client = self.client, None
        if client is not None:
            try:
                client.close()
            except Exception:
                pass


class Pool:
    def __init__(self):
        self.lock = threading.Lock()
        self.entries = {}
        self.disabled = {}  # server key -> (until, reason); until None = for this process
        self.reported = set()
        self.reaper = None

    # -- lifecycle -----------------------------------------------------------
    def reset(self):
        self.close_all()
        with self.lock:
            self.disabled.clear()
            self.reported.clear()

    def close_all(self):
        with self.lock:
            entries, self.entries = list(self.entries.values()), {}
        for entry in entries:
            entry.close()

    def close_idle(self, now=None):
        now = time.monotonic() if now is None else now
        with self.lock:
            idle = [key for key, entry in self.entries.items()
                    if entry.active == 0 and now - entry.last_used >= IDLE_CLOSE]
            entries = [self.entries.pop(key) for key in idle]
        for entry in entries:
            entry.close()
        return len(entries)

    def _start_reaper(self):
        if self.reaper is not None:
            return
        def reap():
            while True:
                time.sleep(60)
                self.close_idle()
        self.reaper = threading.Thread(target=reap, name='ssh-pool-reaper', daemon=True)
        self.reaper.start()

    # -- fallback bookkeeping -----------------------------------------------
    def _fallback(self, server, reason, permanent):
        with self.lock:
            self.disabled[server] = (None if permanent else time.monotonic() + RETRY_AFTER, reason)
            first = server not in self.reported
            self.reported.add(server)
        if first:
            print(f'SSH 连接复用不可用（{server}：{reason}），改用每次新建 ssh 连接。', file=sys.stderr)

    def _enabled(self, server):
        with self.lock:
            state = self.disabled.get(server)
            if state is None:
                return True
            until, _ = state
            if until is not None and time.monotonic() >= until:
                del self.disabled[server]
                return True
            return False

    # -- calls ----------------------------------------------------------------
    def run(self, servers, server, command, program, timeout):
        """Run `command` with `program` on stdin over the pooled connection.

        Returns a CompletedProcess, or None when the caller must use the
        subprocess path instead (nothing has been sent in that case). Raises
        subprocess.TimeoutExpired once `timeout` seconds have passed.
        """
        return self._call(servers, server, command, program, timeout)

    def run_command(self, servers, server, command, timeout):
        """run() for a command that reads no stdin; same None/255/timeout contract."""
        return self._call(servers, server, command, None, timeout)

    def _call(self, servers, server, command, program, timeout):
        if mode() != 'pooled':
            return None
        key = (str(servers.root), server)
        if not self._enabled(key):
            return None
        try:
            paramiko = load_paramiko()
        except ImportError:
            self._fallback(key, '未安装 paramiko', permanent=True)
            return None
        deadline = time.monotonic() + timeout
        try:
            params = resolve(paramiko, servers.load(server))
        except Unsupported as exc:
            self._fallback(key, str(exc), permanent=True)
            return None
        except Exception:
            return None  # an unreadable record fails the same way on the ssh path
        entry = self._entry(key, params)
        with self.lock:
            entry.active += 1
        try:
            channel = self._open(paramiko, entry, key, deadline)
            if channel is None:
                return None
            return self._exchange(paramiko, entry, channel, command, program, deadline, timeout)
        finally:
            with self.lock:
                entry.active -= 1
                entry.last_used = time.monotonic()
                retired = entry.active == 0 and self.entries.get(key) is not entry
            if retired:
                entry.close()

    def _entry(self, key, params):
        with self.lock:
            entry = self.entries.get(key)
            if entry is None or entry.params != params:
                # Record or ~/.ssh/config changed: new connection. An old one still
                # carrying a call is closed when that call ends.
                if entry is not None and entry.active == 0:
                    entry.close()
                entry = self.entries[key] = Entry(params)
            self._start_reaper()
            return entry

    def _connect(self, paramiko, entry, deadline):
        params = entry.params
        client = paramiko.SSHClient()
        for path in params['known_hosts']:
            client.load_system_host_keys(path)
        client.set_missing_host_key_policy(paramiko.RejectPolicy())
        limit = max(1.0, min(CONNECT_TIMEOUT, deadline - time.monotonic()))
        try:
            client.connect(params['hostname'], port=params['port'], username=params['username'],
                           key_filename=list(params['key_filename']) or None,
                           look_for_keys=params['look_for_keys'], allow_agent=params['allow_agent'],
                           password=None, passphrase=None,
                           timeout=limit, banner_timeout=limit, auth_timeout=limit)
        except BaseException:
            try:
                client.close()
            except Exception:
                pass
            raise
        client.get_transport().set_keepalive(KEEPALIVE)
        entry.client = client

    def _open(self, paramiko, entry, key, deadline):
        """A new channel; reconnect (once) only here, before anything is sent."""
        with entry.lock:
            for attempt in (1, 2):
                if entry.client is None or not _active(entry.client):
                    entry.close()
                    try:
                        self._connect(paramiko, entry, deadline)
                    except paramiko.BadHostKeyException:
                        self._fallback(key, '主机密钥与 known_hosts 不符', permanent=True)
                        return None
                    except paramiko.AuthenticationException as exc:
                        self._fallback(key, f'认证未通过（{type(exc).__name__}）', permanent=True)
                        return None
                    except paramiko.SSHException as exc:
                        permanent = 'known_hosts' in str(exc)  # RejectPolicy: unknown host
                        self._fallback(key, '主机不在 known_hosts' if permanent else f'连接失败（{type(exc).__name__}）',
                                       permanent=permanent)
                        return None
                    except Exception as exc:  # network, timeout, unreadable key file
                        self._fallback(key, f'连接失败（{type(exc).__name__}）', permanent=False)
                        return None
                try:
                    return entry.client.get_transport().open_session(timeout=max(1.0, min(CONNECT_TIMEOUT, deadline - time.monotonic())))
                except (paramiko.SSHException, OSError, EOFError, AttributeError):
                    # The cached connection went away while idle; no request has
                    # been sent on it for this call, so reconnecting is safe.
                    entry.close()
            self._fallback(key, '无法在连接上打开会话', permanent=False)
            return None

    def _exchange(self, paramiko, entry, channel, command, program, deadline, timeout):
        stdout, stderr = bytearray(), bytearray()
        def remaining():
            left = deadline - time.monotonic()
            if left <= 0:
                raise subprocess.TimeoutExpired(command, timeout)
            return left
        writer = None
        write_error = []
        write_done = threading.Event()
        try:
            # From here on the request may have reached the server: never resend.
            channel.settimeout(remaining())
            channel.exec_command(command)
            if program is not None:
                payload = program.encode('utf-8')
                def feed():
                    try:
                        channel.sendall(payload)
                        channel.shutdown_write()
                    except BaseException as exc:
                        write_error.append(exc)
                    finally:
                        write_done.set()
                # Feed stdin concurrently while draining stdout/stderr so a large
                # upload (e.g. deploy) cannot deadlock if the remote writes early.
                writer = threading.Thread(target=feed, name='ssh-pool-stdin', daemon=True)
                writer.start()
            else:
                channel.shutdown_write()
                write_done.set()
            poll_sleep = 0.002
            while True:
                got_data = False
                if channel.recv_ready():
                    stdout += channel.recv(65536)
                    got_data = True
                if channel.recv_stderr_ready():
                    stderr += channel.recv_stderr(65536)
                    got_data = True
                if got_data:
                    poll_sleep = 0.002
                    continue
                if write_error and not channel.exit_status_ready():
                    raise write_error[0]
                if channel.exit_status_ready():
                    # exit-status follows all data on the same channel, so what
                    # is buffered now is the complete output.
                    while channel.recv_ready():
                        stdout += channel.recv(65536)
                    while channel.recv_stderr_ready():
                        stderr += channel.recv_stderr(65536)
                    code = channel.recv_exit_status()
                    break
                if channel.closed or not _active(entry.client):
                    if write_error:
                        raise write_error[0]
                    raise LostResponse('channel closed without exit status')
                left = remaining()
                time.sleep(min(poll_sleep, left))
                poll_sleep = min(0.05, poll_sleep * 1.5)
        except subprocess.TimeoutExpired:
            _close(channel)
            if writer is not None:
                writer.join(timeout=1.0)
            raise
        except (LostResponse, paramiko.SSHException, OSError, EOFError) as exc:
            _close(channel)
            if writer is not None:
                writer.join(timeout=1.0)
            if isinstance(exc, TimeoutError) or time.monotonic() >= deadline:
                raise subprocess.TimeoutExpired(command, timeout) from None
            with entry.lock:
                # A broken transport must not serve the next call; a healthy one
                # may be carrying another thread's call (a submission) right now.
                if entry.client is not None and not _active(entry.client):
                    entry.close()
            # 255 and an empty stdout are what `ssh` gives for a dropped connection.
            return subprocess.CompletedProcess(command, 255, '', (stderr.decode('utf-8', 'replace') +
                f'\nConnection lost after the request was sent ({type(exc).__name__}); '
                'remote execution state unknown, not resent.').lstrip())
        _close(channel)
        if writer is not None:
            writer.join(timeout=1.0)
        return subprocess.CompletedProcess(command, code, stdout.decode('utf-8', 'replace'),
                                           stderr.decode('utf-8', 'replace'))


def _active(client):
    try:
        transport = client.get_transport()
        return transport is not None and transport.is_active()
    except Exception:
        return False


def _close(channel):
    try:
        channel.close()
    except Exception:
        pass


POOL = Pool()
atexit.register(POOL.close_all)


def run(servers, server, command, program, timeout):
    return POOL.run(servers, server, command, program, timeout)


def run_command(servers, server, command, timeout):
    return POOL.run_command(servers, server, command, timeout)


def close_all():
    POOL.close_all()
