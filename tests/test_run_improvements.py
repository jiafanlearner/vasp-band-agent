import copy
import json
from types import SimpleNamespace as NS

import numpy as np
import pytest

from vasp_agent.models.context_compaction import ContextCompactor
from vasp_agent.workflow.vasp_band_agent import validate_band_data
import vasp_agent.remote.remote_worker as worker
from vasp_agent.common import agent_paths


def test_recent_large_tool_is_bounded_and_exact_evidence_saved(tmp_path):
    raw = json.dumps({'ok': False, 'result': {'status': 'submission_uncertain', 'rows': list(range(20000))}})
    history = [{'role': 'user', 'content': 'check'},
               {'type': 'function_call', 'call_id': '1', 'name': 'remote_status', 'arguments': '{}'},
               {'type': 'function_call_output', 'call_id': '1', 'output': raw}]
    before = copy.deepcopy(history)
    projected = ContextCompactor(tmp_path / 'ctx.json', 16000).context(history, lambda *a: pytest.fail('no summary needed'), print)
    assert history == before
    out = json.loads(projected[-1]['output'])
    from pathlib import Path
    assert Path(out['evidence_file']).read_text() == raw
    assert len(projected[-1]['output']) < 6000
    assert out['summary']['ok'] is False
    assert out['summary']['result']['status'] == 'submission_uncertain'
    assert projected[1]['call_id'] == projected[2]['call_id']


def test_band_validation_checks_every_kpoint():
    values = np.array([[[-1., 2.], [1., 0.]], [[-1., 2.], [1., 0.]]])
    assert validate_band_data(NS(eigenvalues={1: values}))['kpoints'] == 2
    values[0, -1, 1] = .1
    with pytest.raises(ValueError, match='最高能带'):
        validate_band_data(NS(eigenvalues={1: values}))
    values[0, -1, 1] = 0
    values[0, 0, 0] = np.nan
    with pytest.raises(ValueError, match='非有限'):
        validate_band_data(NS(eigenvalues={1: values}))


def test_environment_constraint_blocks_cell_and_records_binary(tmp_path, monkeypatch):
    binary = tmp_path / 'vasp_std'
    binary.write_bytes(b'fake executable')
    script = tmp_path / 'fixz-env.sh'
    script.write_text('# metadata FixZ')
    monkeypatch.setattr(worker.subprocess, 'run', lambda *a, **k: NS(returncode=0, stdout=str(binary)))
    with pytest.raises(ValueError, match='晶胞约束'):
        worker.inspect_environment({'vasp_env': str(script)}, 'cell')
    evidence = worker.inspect_environment({'vasp_env': str(script)}, 'ions')
    assert evidence['executable_sha256'] == worker.sha(binary)
    assert evidence['provenance'] == 'unverified'
    monkeypatch.setattr(worker.subprocess, 'run', lambda *a, **k: NS(returncode=1, stdout=''))
    with pytest.raises(ValueError, match='解析失败'):
        worker.inspect_environment({'vasp_env': str(script)})


def test_evidence_pagination_and_tampering(tmp_path):
    from vasp_agent.chat.ai_agent import Toolbox
    import hashlib
    folder = agent_paths.store_dir(tmp_path, 'evidence')
    folder.mkdir(parents=True)
    raw = 'evidence' * 1000
    digest = hashlib.sha256(raw.encode()).hexdigest()
    file = folder / (digest + '.json')
    file.write_text(raw)
    box = Toolbox(tmp_path)
    first = box.read_context_evidence(digest, 0)
    assert first['next_offset'] == 3500
    assert first['content'] + box.read_context_evidence(digest, 3500)['content'] + box.read_context_evidence(digest, 7000)['content'] == raw
    file.write_text('changed')
    with pytest.raises(ValueError, match='哈希'):
        box.read_context_evidence(digest, 0)
    with pytest.raises(ValueError):
        box.read_context_evidence('../.env', 0)


def test_scoped_metadata_does_not_open_arbitrary_json(tmp_path, monkeypatch):
    import vasp_agent.remote.remote_servers as remote
    script = tmp_path / 'login.sh'
    script.write_text('ssh user@host')
    servers = remote.Servers(tmp_path)
    servers.register('hpc', script)
    servers.file('hpc').with_name('hpc.compute.json').write_text(json.dumps({'root': '/work/agent'}))
    seen = []
    monkeypatch.setattr(remote, 'run_ssh', lambda args, **kw: seen.append(args) or NS(returncode=0, stdout='{}', stderr=''))
    servers.operate('hpc', 'read', '/work/agent/jobs/si/bands/inputs.json')
    assert len(seen) == 1
    for path in ['/etc/inputs.json', '/work/agent/jobs/si/../../bands/inputs.json', '/work/agent/jobs/si/bands/private.json']:
        with pytest.raises(ValueError):
            servers.operate('hpc', 'read', path)
    assert len(seen) == 1


def test_download_receipt_records_verified_hash(tmp_path, monkeypatch):
    from vasp_agent.remote.remote_compute import RemoteCompute
    import hashlib, base64
    data = b'{"status":"complete"}'
    digest = hashlib.sha256(data).hexdigest()
    (tmp_path / 'servers').mkdir()
    (tmp_path / 'servers/hpc.compute.json').write_text(json.dumps({'root': '/work/agent', 'python': '/usr/bin/python', 'potcar_library': '/potentials', 'vasp_env': '/env.sh', 'partition': 'compute', 'ntasks': 8, 'walltime': '04:00:00'}))
    remote = RemoteCompute(tmp_path, 'hpc')
    monkeypatch.setattr(remote, 'call', lambda *a: {'files': {'report.json': {'data': base64.b64encode(data).decode(), 'sha256': digest}}})
    result = remote.fetch('si')
    from pathlib import Path
    receipt = json.loads(Path(result['receipt']).read_text(encoding='utf8'))
    assert receipt['files']['report.json'] == {'sha256': digest, 'verified': True}


def test_failed_download_leaves_no_final_named_directory(tmp_path, monkeypatch):
    """A download that dies midway must not leave something that reads like a delivery.

    The directory used to be created under its final name before any validation, and
    the uuid suffix meant a retry got a fresh one -- so a hash mismatch on the third
    of nine files abandoned a directory with two valid results, no
    delivery-summary.json and no receipt. A reader could not tell that from a genuine
    truncated delivery whose summary write failed.
    """
    from vasp_agent.remote.remote_compute import RemoteCompute
    import hashlib, base64
    (tmp_path / 'servers').mkdir()
    (tmp_path / 'servers/hpc.compute.json').write_text(json.dumps({'root': '/work/agent', 'python': '/usr/bin/python', 'potcar_library': '/potentials', 'vasp_env': '/env.sh', 'partition': 'compute', 'ntasks': 8, 'walltime': '04:00:00'}))
    remote = RemoteCompute(tmp_path, 'hpc')
    good = b'{"status":"complete"}'
    files = {'workflow-state.json': {'data': base64.b64encode(good).decode(),
                                     'sha256': hashlib.sha256(good).hexdigest()},
             'report.json': {'data': base64.b64encode(b'tampered').decode(),
                             'sha256': hashlib.sha256(good).hexdigest()}}
    monkeypatch.setattr(remote, 'call', lambda *a: {'files': files})
    with pytest.raises(ValueError, match='下载校验失败'):
        remote.fetch('si')
    # Nothing at all survives -- not a final-named directory holding the one file
    # that did verify, and not the staging directory either.
    downloads = tmp_path / 'downloads' / 'hpc'
    assert not list(downloads.iterdir())


def test_delivery_distinguishes_old_missing_and_complete(tmp_path):
    from vasp_agent.remote.remote_compute import delivery_summary
    assert delivery_summary(tmp_path)['status'] == 'partial_or_unverified'
    for name in ('bands.csv', 'bands.png', 'bands.pdf'):
        (tmp_path / name).write_bytes(b'fixture')
    (tmp_path / 'workflow-state.json').write_text('{"status":"complete"}')
    (tmp_path / 'checkpoint.json').write_text(json.dumps({'next': 'complete', 'completed': ['convergence', 'relaxation', 'scf', 'bands']}))
    report = {'sampled_path_gap': {'energy': .6}, 'band_validation': {'passed': True}}
    (tmp_path / 'report.json').write_text(json.dumps(report))
    assert delivery_summary(tmp_path)['status'] == 'complete_package'
    report.pop('band_validation')
    (tmp_path / 'report.json').write_text(json.dumps(report))
    assert delivery_summary(tmp_path)['status'] == 'partial_or_unverified'
    (tmp_path / 'report.json').write_text('broken')
    assert delivery_summary(tmp_path)['issues']


@pytest.mark.parametrize('account_ok', [False, True])
def test_queue_failure_uses_accounting_without_fabricating_completion(tmp_path, monkeypatch, account_ok):
    folder = tmp_path / 'jobs/si'
    folder.mkdir(parents=True)
    (folder / 'remote-submission.json').write_text('{"job_id":"123"}')
    calls = []
    def run(args, **kwargs):
        calls.append(args[0])
        if args[0] == 'squeue':
            return NS(returncode=1, stdout='', stderr='invalid job id')
        assert args[0] == 'sacct'
        return NS(returncode=0 if account_ok else 1, stdout='123|COMPLETED|0:0' if account_ok else '', stderr='' if account_ok else 'accounting unavailable')
    monkeypatch.setattr(worker.subprocess, 'run', run)
    result = worker.dispatch({'config': {'root': str(tmp_path)}, 'action': 'status', 'job_name': 'si'})
    assert calls == ['squeue', 'sacct']
    assert result['queue'] is None
    assert result['scheduler_status_known'] is account_ok
    assert result['scheduler_errors'][0]['command'] == 'squeue'
    if not account_ok:
        assert '未知' in result['scheduler_note']
