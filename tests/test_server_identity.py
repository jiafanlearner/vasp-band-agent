import json
import subprocess
from types import SimpleNamespace as NS

import pytest

from vasp_agent.remote.remote_servers import Servers, ssh_error
from vasp_agent.workflow.vasp_band_agent import ToolError


def setup(tmp_path):
    folder = tmp_path / 'servers'
    folder.mkdir()
    config = folder / 'hpc.json'
    config.write_text(json.dumps(dict(host='host.example', user='u', port=65082,
        identity_file=str(tmp_path / 'old'), source_file='original.bat', extra='preserve')), encoding='utf-8')
    compute = folder / 'hpc.compute.json'
    compute.write_bytes(b'{"root":"/work/jobs", "unknown": 12}\n')
    new = tmp_path / 'new key.txt'
    new.write_text('FAKE KEY FOR TESTS ONLY')
    return Servers(tmp_path), config, compute, new


def test_identity_updates_only_path_and_can_restore(tmp_path, monkeypatch):
    servers, config, compute, key = setup(tmp_path)
    original, computation = config.read_bytes(), compute.read_bytes()
    before = servers.list()[0]
    # The private file must never be opened to update its reference.
    from pathlib import Path
    original_open = Path.open
    def no_key_open(path, *args, **kwargs):
        assert path != key
        return original_open(path, *args, **kwargs)
    monkeypatch.setattr(Path, 'open', no_key_open)
    record = servers.update_identity('hpc', str(key), before['config_sha256'])
    expected = json.loads(original)
    expected['identity_file'] = str(key)
    assert json.loads(config.read_text()) == expected
    assert compute.read_bytes() == computation
    assert not record['connection_verified'] and record['compute_unchanged']
    assert not servers.update_identity('hpc', str(key), 'old hash')['changed']
    restored = servers.undo_identity(record['change_id'])
    assert restored['identity_file'] == json.loads(original)['identity_file']
    assert restored['config_sha256'] == before['config_sha256']
    servers.undo_identity(record['change_id'])
    assert config.read_bytes() == original


def test_conflicts_and_missing_key_leave_profile_unchanged(tmp_path):
    servers, config, _, key = setup(tmp_path)
    old = config.read_bytes()
    with pytest.raises(ToolError):
        servers.update_identity('hpc', str(key), 'wrong')
    with pytest.raises(ToolError):
        servers.update_identity('hpc', str(tmp_path / 'missing'), servers.config_hash('hpc'))
    assert config.read_bytes() == old
    result = servers.update_identity('hpc', str(key), servers.config_hash('hpc'))
    config.write_text(config.read_text() + '\n')
    with pytest.raises(ToolError, match='文件已改变'):
        servers.undo_identity(result['change_id'])


def test_connect_failure_does_not_rollback_key(tmp_path, monkeypatch):
    servers, config, _, key = setup(tmp_path)
    result = servers.update_identity('hpc', str(key), servers.config_hash('hpc'), 'call-key')
    monkeypatch.setattr('vasp_agent.remote.remote_servers.run_ssh', lambda *a, **kw:
        NS(returncode=255, stdout='', stderr='** WARNING: post-quantum\nu@host: Permission denied (publickey,password).'))
    with pytest.raises(ToolError) as exc:
        servers.operate('hpc', 'info')
    assert exc.value.error_code == 'SSH_AUTH_FAILED'
    assert exc.value.warnings and exc.value.connection['port'] == 65082
    assert '过期' in str(exc.value) and '不能区分' in str(exc.value)
    assert json.loads(config.read_text())['identity_file'] == str(key)
    assert servers.update_identity('hpc', str(key), result['before_sha256'], 'call-key')['change_id'] == result['change_id']


@pytest.mark.parametrize('message,code', [
    ('ssh: connect to host x port 22: Connection timed out', 'SSH_CONNECTION_FAILED'),
    ('Host key verification failed.', 'SSH_HOST_KEY_FAILED'),
    ('Load key "x": invalid format', 'SSH_KEY_FILE_INVALID'),
    ('Permission denied (publickey).', 'SSH_AUTH_FAILED'),
    ('unexpected disconnect', 'SSH_FAILED'),
])
def test_ssh_classification(message, code):
    assert ssh_error(message).error_code == code


def test_connect_timeout_and_success_metadata(tmp_path, monkeypatch):
    servers, _, _, _ = setup(tmp_path)
    def timeout(*args, **kwargs):
        raise subprocess.TimeoutExpired('ssh', 50)
    monkeypatch.setattr('vasp_agent.remote.remote_servers.run_ssh', timeout)
    with pytest.raises(ToolError) as exc:
        servers.operate('hpc', 'info')
    assert exc.value.error_code == 'SSH_CONNECTION_FAILED'
    seen = []
    def run(args, **kwargs):
        seen.extend(args)
        return NS(returncode=0, stdout='login\nu\n', stderr='** WARNING: post-quantum')
    monkeypatch.setattr('vasp_agent.remote.remote_servers.run_ssh', run)
    result = servers.operate('hpc', 'info')
    assert result['connection_verified'] and result['warnings']
    assert 'StrictHostKeyChecking=yes' in seen and 'BatchMode=yes' in seen
    assert not any('accept-new' in a for a in seen)


def test_identity_crash_receipt_recovery(tmp_path, monkeypatch):
    from vasp_agent.editing.edit_store import EditStore
    servers, config, _, key = setup(tmp_path)
    original = servers.config_hash('hpc')
    save = EditStore.save
    def broken(store, record):
        if record['status'] == 'applied':
            raise OSError('after replace')
        return save(store, record)
    monkeypatch.setattr(EditStore, 'save', broken)
    with pytest.raises(OSError):
        servers.update_identity('hpc', str(key), original, 'recover')
    monkeypatch.setattr(EditStore, 'save', save)
    result = servers.update_identity('hpc', str(key), original, 'recover')
    assert result['already_applied']
    assert json.loads(config.read_text())['identity_file'] == str(key)


def test_list_ignores_backup_metadata(tmp_path):
    servers, config, _, _ = setup(tmp_path)
    (config.parent / 'hpc.json.backup-verification.json').write_text('{"backup":true}')
    assert [row['name'] for row in servers.list()] == ['hpc']
