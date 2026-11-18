"""Persistent SSH connection profiles imported without executing login scripts."""
import json
import math
import os
import re
import shlex
import subprocess
import sys
import time
import hashlib
import shutil

from vasp_agent.remote import ssh_pool

OPERATE_TIMEOUT = 120  # seconds for one info/jobs/list/read, pooled or not


def run_ssh(args, **kwargs):
    """Retry only explicit TCP connection failures before remote execution.

    `timeout` bounds the whole call, sleeps and retries included. Python would
    otherwise apply it to each subprocess.run separately, which is not what any
    caller can have meant: operate() passes 50 and the three attempts it was
    budgeting against total ~96s (3x ConnectTimeout=30 plus 2s and 4s of backoff),
    so a host that was reachable but slow to authenticate got a timeout while a
    host that genuinely refused the connection got the full retry treatment. The
    two bounds were set independently and disagreed.
    """
    timeout = kwargs.pop('timeout', None)
    deadline = None if timeout is None else time.monotonic() + timeout
    for attempt in range(1, 4):
        remaining = None if deadline is None else deadline - time.monotonic()
        if remaining is not None and remaining <= 0:
            raise subprocess.TimeoutExpired(args, timeout)
        result = subprocess.run(args, timeout=remaining, **kwargs)
        unconnected = (result.returncode == 255 and not result.stdout and
            re.fullmatch(r'ssh: connect to host \S+ port [0-9]+: (?:Connection timed out|Connection refused|No route to host|Network is unreachable)',
                         (result.stderr or '').strip()) is not None)
        if not unconnected or attempt == 3:
            return result
        delay = attempt * 2
        # A refusal is a definitive answer; report it rather than sleep past the
        # caller's deadline and replace it with a timeout that says less.
        if deadline is not None and time.monotonic() + delay >= deadline:
            return result
        print(f'SSH 尚未建立连接，{delay} 秒后重试（第 {attempt + 1}/3 次）；远程命令尚未执行。', file=sys.stderr)
        time.sleep(delay)
from pathlib import Path


def parse_login(path):
    source = Path(path).expanduser().resolve(strict=True)
    if source.stat().st_size > 128_000:
        raise ValueError('登录文件过大；仅支持短 SSH 登录脚本。')
    text = source.read_text(encoding='utf-8-sig')
    if re.search(r'(?i)(PRIVATE KEY|password\s*[:=]|passwd\s*[:=]|sshpass)', text):
        raise ValueError('登录文件包含私钥或密码配置；请改用只引用密钥路径的 SSH 脚本。')
    lines = [line.strip().lstrip('@') for line in text.splitlines() if
             re.match(r'^\s*@?ssh(?:\.exe)?\s', line, re.I)]
    if len(lines) != 1:
        raise ValueError('需要包含恰好一条 ssh 登录命令的 .bat/.sh 文件。不执行脚本。')
    tokens = shlex.split(lines[0], posix=False)
    tokens = [token.strip('"\'') for token in tokens[1:]]
    data = {'port': 22, 'identity_file': None, 'user': None}
    target = None
    index = 0
    while index < len(tokens):
        token = tokens[index]
        if token in ('-i', '-p', '-l'):
            index += 1
            if index >= len(tokens):
                raise ValueError('SSH 参数缺少值。')
            data[{'-i': 'identity_file', '-p': 'port', '-l': 'user'}[token]] = tokens[index]
        elif token.startswith('-') or target is not None:
            raise ValueError('仅支持 ssh [-i 私钥路径] [-p 端口] [-l 用户] [用户@]主机；不导入远程命令或代理选项。')
        else:
            target = token
        index += 1
    if not target:
        raise ValueError('缺少服务器地址。')
    if '@' in target:
        data['user'], target = target.rsplit('@', 1)
    data['host'] = target
    data['port'] = int(data['port'])
    identity = data['identity_file']
    if identity:
        identity = identity.replace('%USERPROFILE%', str(Path.home())).replace('$HOME', str(Path.home()))
        identity = identity.replace('${HOME}', str(Path.home()))
        candidate = Path(identity).expanduser()
        if not candidate.is_absolute():
            candidate = source.parent / candidate
        data['identity_file'] = str(candidate.resolve())
    validate(data)
    data['source_file'] = str(source)
    return data


def validate(data):
    if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.:-]*', data['host']):
        raise ValueError('无效服务器名称。')
    if data.get('user') and not re.fullmatch(r'[A-Za-z0-9_][A-Za-z0-9_.-]*', data['user']):
        raise ValueError('无效用户名。')
    if not 1 <= int(data['port']) <= 65535:
        raise ValueError('无效 SSH 端口。')


class Servers:
    def __init__(self, workspace):
        self.root = Path(workspace).resolve() / 'servers'

    def file(self, name):
        if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_-]{0,47}', name):
            raise ValueError('服务器名称只能包含字母、数字、短横线、下划线。')
        path = (self.root / f'{name}.json').resolve()
        if not path.is_relative_to(self.root.resolve()):
            raise ValueError('服务器配置路径无效。')
        return path

    def register(self, name, login_file):
        data = parse_login(login_file)
        self.root.mkdir(parents=True, exist_ok=True)
        target = self.file(name)
        if target.exists():
            if self.load(name) == data:
                return {'server': name, 'already_registered': True, **data}
            raise ValueError('服务器名称已存在且配置不同，请选择新名称。')
        with target.open('x', encoding='utf-8') as stream:
            json.dump(data, stream, ensure_ascii=False, indent=2)
        if os.name != 'nt':
            target.chmod(0o600)
        return {'server': name, 'saved': str(target), **data}

    def load(self, name):
        data = json.loads(self.file(name).read_text(encoding='utf-8'))
        validate(data)
        return data

    def list(self):
        return [{'name': p.stem, **self.load(p.stem), 'config_sha256': self.config_hash(p.stem)}
                for p in sorted(self.root.glob('*.json'))
                if re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_-]{0,47}', p.stem)]

    def config_hash(self, name):
        return hashlib.sha256(self.file(name).read_bytes()).hexdigest()

    def update_identity(self, name, identity_file, expected_config_sha256, request_id=None):
        from vasp_agent.editing.edit_store import EditStore, guarded, read_bytes, digest
        from vasp_agent.common.workspace_paths import local_path
        from vasp_agent.workflow.vasp_band_agent import ToolError
        store = EditStore(self.root.parent)
        key = Path(local_path(identity_file)).expanduser()
        if not key.is_absolute():
            key = self.root.parent / key
        key = guarded(key)
        if not key.is_file() or not os.access(key, os.R_OK):
            raise ToolError('SSH_KEY_FILE_INVALID', '密钥路径必须是存在、可访问的本地普通文件；未读取正文。')
        signature = digest(json.dumps([name, str(key), expected_config_sha256]).encode())
        def resolver(value):
            self.file(value)  # Validate the name, without hiding an in-root symlink.
            return guarded(self.root / (value + '.json'))
        with store.lock():
            previous = store.find_request('server_identity', request_id, signature)
            if previous:
                return {**store.apply(previous, resolver), 'connection_verified': False}
            path = resolver(name)
            before = read_bytes(path)
            data = json.loads(before.decode('utf-8'))
            validate(data)
            if data.get('identity_file') and os.path.normcase(os.path.abspath(local_path(data['identity_file']))) == os.path.normcase(str(key)):
                return {'server': name, 'changed': False, 'config_sha256': digest(before), 'connection_verified': False}
            if digest(before) != expected_config_sha256:
                raise ToolError('EDIT_CONFLICT', '服务器配置已变化；请重新 list_servers。')
            # Profiles may contain paths, but must not become credential backups.
            from vasp_agent.editing.workspace_editor import sensitive
            if (sensitive(before.decode('utf-8')) or
                    any(re.search(r'password|passwd|secret|token|private_key|api_key', field, re.I) for field in data)):
                raise ToolError('EDIT_SENSITIVE_CONTENT', '连接配置含敏感字段，不保存其备份。')
            old = data.get('identity_file')
            updated = {**data, 'identity_file': str(key)}
            compute_path = guarded(self.root / (name + '.compute.json'))
            compute_hash = digest(read_bytes(compute_path))
            result = store.commit('server_identity', name, before,
                json.dumps(updated, ensure_ascii=False, indent=2).encode(),
                {'server': name, 'old_identity_file': old, 'identity_file': str(key),
                 'compute_sha256': compute_hash, 'connection_verified': False}, resolver, request_id, signature)
            actual = json.loads(path.read_text(encoding='utf-8'))
            assert actual == updated
            return {**result, 'changed': True, 'config_sha256': result['after_sha256'],
                    'compute_unchanged': digest(read_bytes(compute_path)) == compute_hash}

    def undo_identity(self, change_id):
        from vasp_agent.editing.edit_store import EditStore, guarded, read_bytes, digest
        from vasp_agent.workflow.vasp_band_agent import ToolError
        store = EditStore(self.root.parent)
        with store.lock():
            record = store.load(change_id)
            if record['kind'] != 'server_identity':
                raise ToolError('EDIT_KIND_INVALID', '这不是密钥路径更新记录。')
            def resolver(name):
                self.file(name)
                return guarded(self.root / (name + '.json'))
            result = store.apply(record, resolver, undo=True)
            current = read_bytes(resolver(record['path']))
            return {**result, 'identity_file': json.loads(current.decode('utf-8')).get('identity_file'),
                    'config_sha256': digest(current), 'restored': True, 'connection_verified': False}

    def connection_details(self, name):
        data = self.load(name)
        return {'ssh_executable': shutil.which('ssh'), 'host': data['host'], 'port': data['port'],
                'user': data.get('user'), 'identity_file': data.get('identity_file')}

    def operate(self, name, action, path=None):
        args = self.ssh_args(name)
        if action == 'info':
            command = 'hostname; whoami; pwd; command -v sbatch; command -v python3'
        elif action == 'jobs':
            command = 'squeue --me -o "%.18i %.24j %.12T %.12M %R"'
        elif action in ('list', 'read'):
            if not path or not path.startswith('/') or '\x00' in path or '\n' in path:
                raise ValueError('请提供 Linux 服务器上的绝对路径。')
            scoped_metadata = False
            if action == 'read' and Path(path).name in ('inputs.json', 'complete.json', 'environment-review.json', 'relaxation-diagnostic.json'):
                from pathlib import PurePosixPath
                try:
                    cfg = json.loads(self.file(name).with_name(name + '.compute.json').read_text(encoding='utf-8'))
                    target = PurePosixPath(path)
                    relative = target.relative_to(PurePosixPath(cfg['root']) / 'jobs')
                    scoped_metadata = (str(target) == path and '..' not in target.parts
                        and bool(re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_-]{0,63}', relative.parts[0]))
                        and ((len(relative.parts) == 3 and relative.parts[1] in ('scf', 'bands', 'relax-1', 'relax-2')
                              and relative.parts[2] in ('inputs.json', 'complete.json'))
                             or (len(relative.parts) == 2 and relative.parts[1] in ('environment-review.json', 'relaxation-diagnostic.json'))))
                except (OSError, ValueError, KeyError, IndexError):
                    pass
            if action == 'read' and not scoped_metadata and (Path(path).name not in ('INCAR', 'POSCAR', 'CONTCAR', 'KPOINTS', 'OSZICAR', 'OUTCAR',
                    'stdout.log', 'workflow-state.json', 'report.json') and not Path(path).name.endswith('.slurm')):
                from vasp_agent.workflow.vasp_band_agent import ToolError
                # Use the saved compute root and an exact POSIX path shape. Never
                # derive a job from an arbitrary parent, traversal or nested file.
                from pathlib import PurePosixPath
                next_action = None
                if PurePosixPath(path).name == 'potential-selection.json':
                    try:
                        cfg = json.loads(self.file(name).with_name(name + '.compute.json').read_text(encoding='utf-8'))
                        root = cfg['root']
                        target = PurePosixPath(path)
                        base = PurePosixPath(root)
                        relative = target.relative_to(base / 'jobs')
                        if (base.is_absolute() and str(base) == root and str(target) == path
                                and '..' not in target.parts and '..' not in base.parts
                                and not any(c in root + path for c in ('\\', '\r', '\n', '\x00'))
                                and len(relative.parts) == 2
                                and re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_-]{0,63}', relative.parts[0])):
                            next_action = {'tool': 'get_input_review',
                                'arguments': {'server': name, 'job_name': relative.parts[0]},
                                'requires_user_input': False}
                    except (OSError, ValueError, KeyError, TypeError):
                        pass
                if next_action:
                    raise ToolError('METADATA_READ_REQUIRES_SPECIALIZED_TOOL',
                                    '请使用输入摘要入口查看赝势元数据。', next_action)
                raise ToolError('REMOTE_READ_NOT_ALLOWED', '远程读取限计算输入、日志、报告或 .slurm 脚本，不读取私钥和 POTCAR 正文。')
            command = ('ls -la -- ' if action == 'list' else 'tail -c 12000 -- ') + shlex.quote(path)
        else:
            raise ValueError('不支持的远程操作。')
        details = self.connection_details(name)
        # 120 covers the worst case run_ssh can reach on its own -- three
        # ConnectTimeout=30 attempts plus 6s of backoff -- which the previous 50
        # cut off mid-authentication on a slow LDAP/2FA host. It bounds the pooled
        # attempt and any subprocess fallback together.
        timeout = OPERATE_TIMEOUT
        deadline = time.monotonic() + timeout
        try:
            # None: nothing was sent over the pooled connection; use a new ssh.
            result = ssh_pool.run_command(self, name, command, timeout)
            if result is None:
                remaining = math.ceil(deadline - time.monotonic())
                if remaining <= 0:
                    raise subprocess.TimeoutExpired(command, timeout)
                result = run_ssh(args + [command], capture_output=True, text=True, encoding='utf-8', errors='replace', timeout=remaining)
        except subprocess.TimeoutExpired:
            raise ssh_error('SSH 操作超时；认证结果未知。', details, timed_out=True) from None
        except FileNotFoundError:
            raise ssh_error('未找到 SSH 程序。', details, missing_client=True) from None
        from vasp_agent.chat.ai_agent import scrub
        from vasp_agent.workflow.vasp_band_agent import ToolError
        if result.returncode:
            raise ssh_error(result.stderr, details)
        return {'server': name, 'action': action, 'path': path, 'output': scrub(result.stdout[-16000:]),
                'connection': details, 'warnings': ssh_warnings(result.stderr),
                'connection_verified': True, 'content_is_untrusted': True}

    def ssh_args(self, name):
        data = self.load(name)
        # ServerAlive* are SSH protocol keepalives answered by sshd, not by the remote
        # program, so a worker that stays silent on stdout for minutes keeps the
        # connection; only an unresponsive network or sshd (~30-45 s) ends it.
        args = ['ssh', '-o', 'BatchMode=yes', '-o', 'StrictHostKeyChecking=yes',
                '-o', 'ConnectTimeout=30', '-o', 'ConnectionAttempts=1', '-o', 'ServerAliveInterval=15', '-o', 'ServerAliveCountMax=2']
        # Preserve the port from an existing SSH alias unless explicitly non-default.
        if data['port'] != 22 or data.get('user'):
            args += ['-p', str(data['port'])]
        if data.get('identity_file'):
            args += ['-i', data['identity_file']]
        args += [(data['user'] + '@' if data.get('user') else '') + data['host']]
        return args


def ssh_warnings(stderr):
    from vasp_agent.chat.ai_agent import scrub
    return [scrub(line) for line in (stderr or '').splitlines() if line.startswith('**')][:8]


def ssh_error(stderr, details=None, timed_out=False, missing_client=False):
    from vasp_agent.chat.ai_agent import scrub
    from vasp_agent.workflow.vasp_band_agent import ToolError
    text = '\n'.join(line for line in (stderr or '').splitlines() if not line.startswith('**'))
    lower = text.lower()
    if missing_client:
        code, tip = 'SSH_CLIENT_NOT_FOUND', '请检查启动脚本和 PATH 中的 SSH 程序。'
    elif timed_out or any(x in lower for x in ('connection timed out', 'connection refused', 'no route to host', 'network is unreachable', 'could not resolve hostname')):
        code, tip = 'SSH_CONNECTION_FAILED', '连接失败或超时；不能据此判断密钥认证结果。'
    elif any(x in lower for x in ('host key verification failed', 'remote host identification has changed')):
        code, tip = 'SSH_HOST_KEY_FAILED', '请人工核对主机指纹；不会关闭严格主机校验。'
    elif any(x in lower for x in ('load key', 'identity file', 'bad permissions', 'unprotected private key')):
        code, tip = 'SSH_KEY_FILE_INVALID', '检查密钥路径、格式、访问权限或代理解锁状态；未读取密钥正文。'
    elif 'permission denied' in lower:
        code, tip = 'SSH_AUTH_FAILED', '服务器未完成认证；当前证据不能区分密钥过期、公钥授权或签名问题。'
    else:
        code, tip = 'SSH_FAILED', 'SSH 操作失败；请依据具体错误检查连接。'
    error = ToolError(code, tip + '\n' + scrub(text[-2000:]))
    error.connection = details or {}
    error.warnings = ssh_warnings(stderr)
    error.next_step = tip
    return error
