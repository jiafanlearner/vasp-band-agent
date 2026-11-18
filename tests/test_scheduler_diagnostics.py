"""Offline scheduler fixtures: never connect or launch a real job."""
import json
import subprocess
from types import SimpleNamespace as NS

import pytest
from vasp_agent.remote import remote_worker as worker


def script(nodes=1, tasks=96):
    return f"#SBATCH --partition=xahcnormal\n#SBATCH --nodes={nodes}\n#SBATCH --ntasks={tasks}\n"


def capacity_mock(monkeypatch, groups="32|126536|3218\n64|255551|2285\n", memory="DefMemPerCPU=3570"):
    calls = []
    def run(command, **kwargs):
        calls.append(command)
        assert command[0] in ("sinfo", "scontrol")
        return NS(returncode=0, stdout=groups if command[0] == "sinfo" else memory, stderr="")
    monkeypatch.setattr(worker.subprocess, "run", run)
    return calls


def test_actual_96_core_layout_requires_more_than_one_node(monkeypatch):
    calls = capacity_mock(monkeypatch)
    with pytest.raises(worker.SubmissionError) as caught:
        worker.capacity_preflight(script())
    assert caught.value.error_code == "SCHEDULER_CAPACITY_EXCEEDED"
    assert len(calls) == 1
    result = worker.capacity_preflight(script(nodes=2))
    assert result["cpus_per_node"] == 48
    assert result["default_memory_mb_per_node"] == 171360


@pytest.mark.parametrize("groups,memory", [
    ("64|100000|10\n32|500000|10\n", "DefMemPerCPU=3570"),
    ("64|255551|1\n", "DefMemPerCPU=3570"),
    ("64|255551|10\n", "DefMemPerNode=300G"),
])
def test_capacity_requires_enough_nodes_matching_both_cpu_and_memory(monkeypatch, groups, memory):
    capacity_mock(monkeypatch, groups, memory)
    with pytest.raises(worker.SubmissionError) as caught:
        worker.capacity_preflight(script(nodes=2))
    assert caught.value.error_code == "SCHEDULER_CAPACITY_EXCEEDED"


@pytest.mark.parametrize("failure", ["empty", "format", "returncode", "timeout", "memory"])
def test_unknown_capacity_fails_closed(monkeypatch, failure):
    def run(command, **kwargs):
        if failure == "timeout":
            raise subprocess.TimeoutExpired(command, 20)
        return NS(returncode=1 if failure == "returncode" else 0,
                  stdout="" if failure == "empty" else ("64|255551|10" if failure == "memory" and command[0] == "sinfo" else "unknown"), stderr="token=hidden")
    monkeypatch.setattr(worker.subprocess, "run", run)
    with pytest.raises(worker.SubmissionError) as caught:
        worker.capacity_preflight(script(nodes=2))
    assert caught.value.error_code == "SCHEDULER_CAPACITY_UNKNOWN"
    assert "hidden" not in str(caught.value)


@pytest.mark.parametrize("text", [script(nodes=0), script(nodes=5), script()+"#SBATCH --mem=10G\n"])
def test_invalid_or_unsupported_layout_never_queries(monkeypatch, text):
    monkeypatch.setattr(worker.subprocess, "run", lambda *a, **k: pytest.fail("unexpected command"))
    with pytest.raises(worker.SubmissionError):
        worker.capacity_preflight(text)


@pytest.mark.parametrize("outcome", ["timeout", "nonzero", "malformed", "success"])
def test_submission_persists_outcome_and_never_replays(tmp_path, monkeypatch, outcome):
    record = tmp_path / "submissions/convergence.json"
    (tmp_path / "submit.slurm").write_text("#!/bin/bash\n#SBATCH --nodes=1\n#SBATCH --ntasks=8\n", encoding="utf-8")
    calls = []
    def run(command, **kwargs):
        calls.append(command)
        assert json.loads(record.read_text())["status"] == "submission_uncertain"
        if outcome == "timeout":
            raise subprocess.TimeoutExpired(command, 30, output=b"partial", stderr=b"token=secret-value")
        return NS(returncode=1 if outcome == "nonzero" else 0,
                  stdout="70292806;cluster\n" if outcome == "success" else "unrecognized response",
                  stderr="token=secret-value")
    monkeypatch.setattr(worker.subprocess, "run", run)
    if outcome == "success":
        assert worker.submit_with_receipt(tmp_path, record, "convergence", True, {})["job_id"] == "70292806"
    else:
        with pytest.raises(worker.SubmissionError):
            worker.submit_with_receipt(tmp_path, record, "convergence", True, {})
    saved = json.loads(record.read_text())
    diagnostic = saved["submission_diagnostic"]
    assert "secret-value" not in record.read_text()
    assert diagnostic["stdout"]
    assert diagnostic["finished_at"]
    assert diagnostic["returncode"] == (None if outcome == "timeout" else 1 if outcome == "nonzero" else 0)
    with pytest.raises(FileExistsError):
        worker.submit_with_receipt(tmp_path, record, "convergence", True, {})
    assert len(calls) == 1


@pytest.mark.parametrize("case", ["match", "name", "path", "empty", "error", "timeout", "missing", "invalid"])
def test_history_is_readonly_and_requires_exact_target(tmp_path, monkeypatch, case):
    record = tmp_path / "submissions/convergence.json"
    record.parent.mkdir()
    record.write_text('{"status":"submission_uncertain","job_id":null}')
    before = record.read_bytes()
    calls = []
    monkeypatch.setattr(worker.os, "getuid", lambda: 123, raising=False)
    def run(command, **kwargs):
        calls.append(command)
        assert command[0] == "sacct" and "123" in command
        if case == "timeout":
            raise subprocess.TimeoutExpired(command, 20)
        name = "other" if case == "name" else "va-" + tmp_path.name
        path = "/other" if case == "path" else str(tmp_path)
        line = f"70292806|{name}|FAILED|1:0|submit|start|end|None assigned|{path}|\n"
        return NS(returncode=1 if case == "error" else 0, stdout="" if case == "empty" else line, stderr="failure")
    monkeypatch.setattr(worker.subprocess, "run", run)
    job_id = None if case == "missing" else "bad;id" if case == "invalid" else "70292806"
    if case in ("name", "path", "invalid"):
        with pytest.raises(ValueError):
            worker.submission_accounting(tmp_path, "convergence", job_id)
    else:
        result = worker.submission_accounting(tmp_path, "convergence", job_id)
        assert result["record_modified"] is False
        if case == "match":
            assert result["target_matched"] and result["accounting"][0]["state"] == "FAILED"
        if case == "empty":
            assert not result["target_matched"]
    assert len(calls) == (0 if case == "invalid" else 1)
    assert record.read_bytes() == before


@pytest.mark.parametrize('case', ['unique', 'multiple', 'wrong_directory', 'wrong_name', 'empty', 'malformed'])
def test_discover_job_without_id_never_changes_receipt(tmp_path, monkeypatch, case):
    record = tmp_path / 'submissions/convergence.json'
    record.parent.mkdir()
    record.write_text(json.dumps({'status': 'submission_uncertain', 'job_id': None,
        'submission_diagnostic': {'started_at': '2026-09-28T10:01:31+00:00'}}))
    before = record.read_bytes()
    monkeypatch.setattr(worker.os, 'getuid', lambda: 456, raising=False)
    def row(number, name=None, directory=None):
        return f"{number}|{name or 'va-'+tmp_path.name}|FAILED|1:0|submit|start|end|None assigned|{directory or tmp_path}|\n"
    outputs = {'unique': row('70292806'), 'multiple': row('70292806') + row('70292807'),
               'wrong_directory': row('70292806', directory='/elsewhere'),
               'wrong_name': row('70292806', name='va-other'), 'empty': '',
               'malformed': row('70292806') + 'invalid row\n'}
    calls = []
    def run(command, **kwargs):
        calls.append(command)
        assert command[0] == 'sacct' and '-j' not in command
        assert command[command.index('-u')+1] == '456'
        assert '--name=va-'+tmp_path.name in command
        assert '-S' in command and '-E' in command
        return NS(returncode=0, stdout=outputs[case], stderr='')
    monkeypatch.setattr(worker.subprocess, 'run', run)
    result = worker.submission_accounting(tmp_path, 'convergence')
    assert result['lookup_mode'] == 'discover'
    assert result['search_window']['source'] == 'receipt_started_at'
    assert result['discovered_job_id'] == ('70292806' if case == 'unique' else None)
    assert result['lookup_status'] == {'unique': 'unique_match', 'multiple': 'ambiguous',
        'wrong_directory': 'no_match', 'wrong_name': 'no_match', 'empty': 'no_match', 'malformed': 'incomplete'}[case]
    assert not result['phase_verified'] and not result['record_modified']
    assert record.read_bytes() == before and len(calls) == 1


def test_search_windows_support_legacy_receipts_and_explicit_range(tmp_path):
    from datetime import datetime
    path = tmp_path / 'receipt.json'
    assert worker.accounting_window(path, None)['source'] == 'last_seven_days'
    path.write_text('{}')
    assert worker.accounting_window(path, {})['source'] == 'receipt_mtime_estimate'
    result = worker.accounting_window(path, {}, '2026-09-28T17:40:00', '2026-09-28T18:30:00')
    assert result['source'] == 'operator_window'
    assert result['start'] == '2026-09-28T17:40:00'
    assert (datetime.fromisoformat(result['end']) - datetime.fromisoformat(result['start'])).total_seconds() == 3000


@pytest.mark.parametrize('start,end', [('bad', 'bad'), ('2026-01-01', None),
    ('2026-01-01', '2026-03-01'), ('2026-01-02', '2026-01-01')])
def test_invalid_search_window_does_not_query(tmp_path, monkeypatch, start, end):
    monkeypatch.setattr(worker.os, 'getuid', lambda: 456, raising=False)
    monkeypatch.setattr(worker.subprocess, 'run', lambda *a, **k: pytest.fail('no query'))
    with pytest.raises(ValueError):
        worker.submission_accounting(tmp_path, 'convergence', None, start, end)


def test_discovery_errors_do_not_report_no_matches(tmp_path, monkeypatch):
    monkeypatch.setattr(worker.os, 'getuid', lambda: 456, raising=False)
    monkeypatch.setattr(worker.subprocess, 'run', lambda *a, **k: NS(returncode=1, stdout='', stderr='accounting unavailable'))
    result = worker.submission_accounting(tmp_path, 'convergence')
    assert result['accounting'] is None and result['query_error']
    assert not result.get('discovered_job_id')


def test_diagnostic_tool_forwards_search_parameters(tmp_path, monkeypatch):
    from vasp_agent.chat.ai_agent import Toolbox
    from vasp_agent.remote.remote_compute import RemoteCompute
    monkeypatch.setattr(RemoteCompute, '__init__', lambda *a: None)
    calls = []
    def call(self, action, job, **params):
        calls.append((action, job, params))
        return {'record_modified': False}
    monkeypatch.setattr(RemoteCompute, 'call', call)
    box = Toolbox(tmp_path)
    assert 'get_submission_diagnostic' in [s['name'] for s in box.schemas()]
    box.get_submission_diagnostic('test', 'si', 'convergence', None, '2026-09-28T00:00:00', '2026-09-29T00:00:00')
    assert calls == [('submission_diagnostic', 'si', {'phase': 'convergence', 'job_id': None,
        'start_time': '2026-09-28T00:00:00', 'end_time': '2026-09-29T00:00:00'})]


@pytest.mark.parametrize('scenario', ['original', 'three_nodes', 'changed_script', 'query_failure', 'accounting_failure', 'ambiguous', 'secret_log', 'missing_script'])
def test_integrated_diagnostic_preserves_evidence_and_never_submits(tmp_path, monkeypatch, scenario):
    record = tmp_path / 'submissions/convergence.json'
    record.parent.mkdir()
    record.write_text('{"status":"submission_uncertain","job_id":null}')
    nodes = 3 if scenario == 'three_nodes' else 2 if scenario == 'changed_script' else 1
    if scenario != 'missing_script':
        (tmp_path / 'submit.slurm').write_text(script(nodes=nodes))
    (tmp_path / 'config.json').write_text('{"parallel_tests":6,"cores_per_test":16}')
    if scenario == 'secret_log':
        (tmp_path / 'slurm-70292806.log').write_text('startup failed token=private-value')
    before = {str(p.relative_to(tmp_path)): p.read_bytes() for p in tmp_path.rglob('*') if p.is_file()}
    monkeypatch.setattr(worker.os, 'getuid', lambda: 456, raising=False)
    calls = []
    def row(number):
        return f'{number}|va-{tmp_path.name}|FAILED|1:0|submit|start|end|None assigned|{tmp_path}|None|xahcnormal|96|1|3570Mc|cpu=96,mem=342720M,node=1|0|0|00:00:00\n'
    def run(command, **kwargs):
        calls.append(command)
        assert command[0] in ('sacct', 'sinfo', 'scontrol')
        if command[0] == 'sacct':
            if scenario == 'accounting_failure':
                return NS(returncode=1, stdout='', stderr='accounting unavailable')
            return NS(returncode=0, stdout=row('70292806') + (row('70292807') if scenario == 'ambiguous' else ''), stderr='')
        if scenario == 'query_failure':
            raise subprocess.TimeoutExpired(command, 20)
        return NS(returncode=0, stdout='32|126536|3218\n64|255551|2285\n' if command[0] == 'sinfo' else 'DefMemPerCPU=3570', stderr='')
    monkeypatch.setattr(worker.subprocess, 'run', run)
    result = worker.submission_diagnostic(tmp_path, 'convergence')
    diag = result['diagnosis']
    codes = {f['code'] for f in diag['confirmed_configuration_findings']}
    if scenario in ('original', 'accounting_failure', 'secret_log', 'ambiguous'):
        assert 'CURRENT_RESOURCE_CAPACITY_CONFLICT' in codes
        assert diag['resource_layout_candidates'][0]['nodes'] == 2
        assert all(16 % c['nodes'] == 0 for c in diag['resource_layout_candidates'])
    if scenario == 'three_nodes':
        assert 'PER_TEST_NODE_DIVISIBILITY' in codes
        assert all(c['nodes'] != 3 for c in diag['resource_layout_candidates'])
    if scenario == 'changed_script':
        assert 'CURRENT_RESOURCE_CAPACITY_CONFLICT' not in codes
        assert 'HISTORICAL_REQUEST_CURRENT_CAPACITY_CONFLICT' in codes
        assert result['accounting'][0]['requested_nodes'] == '1'
        assert diag['evidence']['current_script']['resources']['nodes'] == '2'
    if scenario == 'query_failure':
        assert not codes and diag['evidence']['capacity']['status'] == 'unknown'
        assert len([c for c in calls if c[0] == 'sinfo']) == 1
    if scenario == 'accounting_failure':
        assert result['query_error'] and result['accounting'] is None
    if scenario == 'ambiguous':
        assert not diag['evidence']['logs']
    if scenario == 'secret_log':
        assert 'private-value' not in json.dumps(result)
        assert 'startup failed' in diag['evidence']['logs'][0]['tail']
    if scenario == 'missing_script':
        assert diag['evidence']['current_script'] is None
        assert 'HISTORICAL_REQUEST_CURRENT_CAPACITY_CONFLICT' in codes
    assert not diag['historical_root_cause_confirmed']
    assert not result['record_modified']
    assert before == {str(p.relative_to(tmp_path)): p.read_bytes() for p in tmp_path.rglob('*') if p.is_file()}


def test_diagnostic_file_rejects_outside_directory(tmp_path):
    with pytest.raises(ValueError):
        worker.diagnostic_file(tmp_path, '../outside')


def test_diagnostic_log_tail_is_bounded(tmp_path):
    p = tmp_path / 'slurm-1.log'
    p.write_text('x' * 20000 + 'last line')
    result = worker.diagnostic_file(tmp_path, p.name, tail=True)
    assert len(result) == 12000 and result.endswith('last line')


@pytest.mark.parametrize('case', ['match', 'different_phase', 'uid', 'directory', 'time', 'unsupported', 'no_phase', 'duplicate', 'substring'])
def test_submitline_requires_identity_and_explicit_export(tmp_path, monkeypatch, case):
    monkeypatch.setattr(worker.os, 'getuid', lambda: 456, raising=False)
    line = 'sbatch --parsable --export=ALL,VASP_AGENT_PHASE=convergence submit.slurm'
    if case == 'different_phase': line = line.replace('convergence', 'scf')
    if case == 'no_phase': line = 'sbatch submit.slurm'
    if case == 'duplicate': line += ' --export=VASP_AGENT_PHASE=scf'
    if case == 'substring': line = 'sbatch --comment=VASP_AGENT_PHASE=convergence submit.slurm'
    def run(command, **kw):
        assert command[0] == 'sacct' and 'SubmitLine' in command[-1]
        uid = '999' if case == 'uid' else '456'
        directory = '/wrong' if case == 'directory' else str(tmp_path)
        stamp = 'other' if case == 'time' else 'stamp'
        return NS(returncode=1 if case == 'unsupported' else 0,
            stdout=f'70292806|va-{tmp_path.name}|{directory}|{uid}|jiafan|{stamp}|{line}\n', stderr='unsupported field' if case == 'unsupported' else '')
    monkeypatch.setattr(worker.subprocess, 'run', run)
    result = worker.submission_command_evidence(tmp_path, {'job_id': '70292806', 'submit': 'stamp'}, 'convergence')
    assert result['submission_phase_verified'] is (case == 'match')
    assert not result['execution_phase_verified'] and not result['attempt_binding_verified']
    if case == 'unsupported': assert result['query_error'] == 'unsupported field'


def test_fixed_evidence_is_bounded_redacted_and_hashed(tmp_path):
    import hashlib
    raw = b'x' * 16000 + b'\n{"password": "not-public"}\n'
    (tmp_path / 'events.jsonl').write_bytes(raw)
    (tmp_path / 'unrelated.key').write_text('do not read')
    result = worker.task_evidence_files(tmp_path, 'convergence')
    event = result[0]
    assert event['bytes_read'] == 12000 and event['truncated']
    assert event['sha256'] == hashlib.sha256(raw[-12000:]).hexdigest()
    assert 'not-public' not in event['text']
    assert {x['path'] for x in result} == {'events.jsonl', 'remote-submission.json', 'submissions/convergence.json'}
    assert (tmp_path / 'events.jsonl').read_bytes() == raw


def test_log_read_failure_does_not_drop_submitline(tmp_path, monkeypatch):
    monkeypatch.setattr(worker, 'submission_accounting', lambda *a: {'target_matched': True,
        'accounting': [{'job_id': '70292806', 'submit': 'stamp'}], 'record_modified': False})
    monkeypatch.setattr(worker, 'submission_command_evidence', lambda *a: {'submission_phase_verified': True})
    (tmp_path / 'events.jsonl').mkdir()
    result = worker.submission_diagnostic(tmp_path, 'convergence')
    assert result['submission_phase_verified'] and not result['execution_phase_verified']
    assert result['diagnosis']['evidence']['task_files'][0]['read_error']
