import json
from pathlib import Path
from types import SimpleNamespace

import pytest

import vasp_agent.chat.ai_agent as ai
from vasp_agent.common import agent_paths


def prepare_args(name='si-ai'):
    return dict(structure='Si.POSCAR', job_name=name, encut=None, mesh=None,
                relax_mode='cell', nbands=16, rationale='用户要求硅的非磁性 PBE 完整流程。')


@pytest.fixture
def workspace(tmp_path):
    source = Path(__file__).parents[1] / 'examples/Si.POSCAR'
    (tmp_path / 'Si.POSCAR').write_bytes(source.read_bytes())
    return tmp_path


class Item:
    def __init__(self, **data):
        self.data = data
        self.__dict__.update(data)

    def model_dump(self, **kwargs):
        return self.data


class FakeResponses:
    def __init__(self, replies):
        self.replies = iter(replies)
        self.requests = []

    def create(self, **kwargs):
        # Snapshot mutable history before the next turn appends to it.
        self.requests.append(json.loads(json.dumps(kwargs)))
        return next(self.replies)


def reply(*items, text=''):
    return SimpleNamespace(output=list(items), output_text=text, status='completed')


def function(name, args, call_id='c1'):
    return Item(type='function_call', name=name, arguments=json.dumps(args), call_id=call_id)


def test_model_inspects_prepares_and_reads_back(workspace):
    responses = FakeResponses([
        reply(Item(type='reasoning', id='r1', summary=[]), function('inspect_structure', {'path': 'Si.POSCAR'})),
        reply(function('prepare_calculation', prepare_args(), 'c2')),
        reply(function('get_status', {'job_name': 'si-ai'}, 'c3')),
        reply(Item(type='message', role='assistant', content=[{'type': 'output_text', 'text': '已准备，未提交。', 'annotations': []}]), text='已准备，未提交。'),
    ])
    agent = ai.Agent(SimpleNamespace(responses=responses), 'test-model', ai.Toolbox(workspace))
    assert agent.ask('用硅准备非磁性 PBE 完整流程，先不提交') == '已准备，未提交。'
    assert (workspace / 'runs/si-ai/config.json').is_file()
    assert not (workspace / 'runs/si-ai/submission.json').exists()
    events = [x for x in responses.requests[1]['input'] if x.get('type')]
    assert events[0]['type'] == 'reasoning'
    outputs = [x for x in responses.requests[-1]['input'] if x.get('type') == 'function_call_output']
    assert len(outputs) == 3
    assert all(json.loads(x['output'])['ok'] for x in outputs)
    assert agent.file.is_file()


def test_tool_error_returns_to_model(workspace):
    responses = FakeResponses([reply(function('read_log', {'job_name': 'missing', 'file': 'POTCAR'})),
                               reply(text='该文件不允许读取。')])
    agent = ai.Agent(SimpleNamespace(responses=responses), 'test', ai.Toolbox(workspace))
    agent.ask('诊断任务')
    output = json.loads(responses.requests[1]['input'][-1]['output'])
    assert not output['ok']


@pytest.mark.parametrize('name,args', [
    ('inspect_structure', {'path': '../outside.POSCAR'}),
    ('prepare_calculation', {**prepare_args(), 'job_name': '../escape'}),
    ('prepare_calculation', {**prepare_args(), 'force_tol': 100}),
    ('prepare_calculation', {**prepare_args(), 'mesh': [0, 1, 2]}),
    ('submit_job', {'job_name': 'si-ai'}),
    ('shell', {'command': 'echo no'}),
])
def test_invalid_or_unauthorized_tools_rejected(workspace, name, args):
    assert not ai.Toolbox(workspace).call(name, args)['ok']


def test_submission_is_idempotent(workspace, monkeypatch):
    monkeypatch.setattr('vasp_agent.permissions.submission_dialog.confirm', lambda *a, **kw: None)
    box = ai.Toolbox(workspace)
    assert box.call('prepare_calculation', prepare_args())['ok']
    script = workspace / 'job.slurm'
    script.write_text('#!/bin/bash\n')
    box = ai.Toolbox(workspace, submit_script=script, allow_submit=True)
    monkeypatch.setattr(ai.core, 'verify', lambda folder: {'workflow': 'full'})
    launches = []
    def submit(command, **kwargs):
        launches.append(command)
        assert kwargs['env']['WORK_DIR'] == str(workspace / 'runs/si-ai')
        return SimpleNamespace(stdout='12345\n')
    monkeypatch.setattr(ai.subprocess, 'run', submit)
    assert box.call('submit_job', {'job_name': 'si-ai'})['result']['job_id'] == '12345'
    assert box.call('submit_job', {'job_name': 'si-ai'})['result']['already_registered']
    assert len(launches) == 1


def test_uncertain_submission_is_not_repeated(workspace, monkeypatch):
    monkeypatch.setattr('vasp_agent.permissions.submission_dialog.confirm', lambda *a, **kw: None)
    box = ai.Toolbox(workspace)
    box.call('prepare_calculation', prepare_args())
    script = workspace / 'job.slurm'
    script.write_text('#!/bin/bash\n')
    box = ai.Toolbox(workspace, submit_script=script, allow_submit=True)
    monkeypatch.setattr(ai.core, 'verify', lambda folder: {'workflow': 'full'})
    def timeout(*args, **kwargs):
        raise ai.subprocess.TimeoutExpired('sbatch', 30)
    monkeypatch.setattr(ai.subprocess, 'run', timeout)
    assert not box.call('submit_job', {'job_name': 'si-ai'})['ok']
    output = box.call('submit_job', {'job_name': 'si-ai'})
    assert output['result']['status'] == 'submission_uncertain'


def test_secret_redaction_and_log_whitelist(workspace):
    box = ai.Toolbox(workspace)
    box.call('prepare_calculation', prepare_args())
    folder = workspace / 'runs/si-ai'
    (folder / 'stdout.log').write_text('PASSWORD=very-secret\nSCF not converged\n')
    output = box.call('read_log', {'job_name': 'si-ai', 'file': 'stdout.log'})
    assert 'very-secret' not in json.dumps(output)
    assert 'SCF not converged' in output['result']['tail']


def test_loop_budget(workspace):
    responses = FakeResponses([reply(function('list_files', {}))])
    agent = ai.Agent(SimpleNamespace(responses=responses), 'test', ai.Toolbox(workspace), max_steps=1)
    assert '上限' in agent.ask('查看文件')
    assert len(responses.requests) == 1


def test_every_audit_entry_is_timestamped(workspace):
    """Audit records must be orderable; the writer supplies the time, not callers."""
    box = ai.Toolbox(workspace, allow_submit=True)
    box.call('list_files', {})
    box.revoke_submission()
    box.consume_submission()
    rows = [json.loads(line) for line in
            agent_paths.audit_file(workspace).read_text(encoding='utf-8').splitlines()]
    assert rows, 'expected audit records'
    assert all('time' in row for row in rows)
    times = [ai.datetime.fromisoformat(row['time']) for row in rows]
    assert all(moment.tzinfo is not None for moment in times)
    assert times == sorted(times), 'audit records must be in chronological order'


def test_audit_payload_cannot_forge_its_own_timestamp(workspace):
    box = ai.Toolbox(workspace)
    box.log({'time': '1999-01-01T00:00:00+00:00', 'tool': 'forged'})
    row = json.loads(agent_paths.audit_file(workspace).read_text(encoding='utf-8').strip())
    assert row['time'] != '1999-01-01T00:00:00+00:00'
    assert row['tool'] == 'forged'


def test_audit_record_survives_a_mention_of_a_secret(workspace):
    """Redaction must not be able to delete the record that mentions a secret.

    Scrubbing the serialized line applies a whole-line pattern to the single
    line that is the record, so one password-shaped substring used to erase the
    entire entry. Scrubbing the leaves keeps the record.
    """
    box = ai.Toolbox(workspace)
    box.call('prepare_calculation', prepare_args())
    folder = workspace / 'runs/si-ai'
    (folder / 'stdout.log').write_text('note: password: hunter2\nSCF not converged\n')
    box.call('read_log', {'job_name': 'si-ai', 'file': 'stdout.log'})
    rows = [json.loads(line) for line in
            agent_paths.audit_file(workspace).read_text(encoding='utf-8').splitlines()]
    assert rows, 'expected audit records'
    assert all(row.get('tool') for row in rows), 'every record must keep its identity'
    text = agent_paths.audit_file(workspace).read_text(encoding='utf-8')
    assert 'hunter2' not in text
    assert 'SCF not converged' in text


def test_secret_named_fields_are_redacted_not_dropped(workspace):
    box = ai.Toolbox(workspace)
    box.log({'tool': 'demo', 'arguments': {'api_key': 'sk-abcdefghijklmn', 'mesh': [8, 8, 8]}})
    row = json.loads(agent_paths.audit_file(workspace).read_text(encoding='utf-8').strip())
    assert row['arguments']['api_key'] == '[REDACTED SENSITIVE CONTENT]'
    assert row['arguments']['mesh'] == [8, 8, 8]


def test_audit_record_carries_the_session_join_key(workspace):
    """The session ties the audit trail to the job events and to remote calls."""
    box = ai.Toolbox(workspace)
    ai.Agent(SimpleNamespace(responses=None), 'test-model', box, 'si-session')
    assert box.session_id == 'si-session'
    box.call('list_files', {})
    row = json.loads(agent_paths.audit_file(workspace).read_text(encoding='utf-8').strip())
    assert row['session'] == 'si-session'


def test_audit_payload_cannot_forge_its_own_session(workspace):
    box = ai.Toolbox(workspace)
    box.log({'session': 'someone-else', 'tool': 'forged'})
    row = json.loads(agent_paths.audit_file(workspace).read_text(encoding='utf-8').strip())
    assert row['session'] is None and row['tool'] == 'forged'


def test_session_reaches_a_submitted_job_environment(workspace, monkeypatch):
    monkeypatch.setattr('vasp_agent.permissions.submission_dialog.confirm', lambda *a, **kw: None)
    box = ai.Toolbox(workspace)
    box.session_id = 'si-session'
    box.call('prepare_calculation', prepare_args())
    script = workspace / 'job.slurm'
    script.write_text('#!/bin/bash\n')
    box = ai.Toolbox(workspace, submit_script=script, allow_submit=True)
    box.session_id = 'si-session'
    monkeypatch.setattr(ai.core, 'verify', lambda folder: {'workflow': 'full'})
    seen = {}
    def submit(command, **kwargs):
        seen.update(kwargs['env'])
        return SimpleNamespace(stdout='12345\n')
    monkeypatch.setattr(ai.subprocess, 'run', submit)
    assert box.call('submit_job', {'job_name': 'si-ai'})['ok']
    assert seen['VASP_AGENT_SESSION'] == 'si-session'


def test_oversized_audit_payload_is_spilled_and_recoverable(workspace):
    """A big payload is stored whole and pointed at, not silently truncated."""
    box = ai.Toolbox(workspace)
    tail = 'SCF detail ' * 3000
    box.log({'tool': 'read_log', 'output': {'ok': True, 'result': {'tail': tail}}})
    row = json.loads(agent_paths.audit_file(workspace).read_text(encoding='utf-8').strip())
    # The queryable shape of a tool result survives: only the bulky field moves,
    # so a reader filtering on `ok` still sees this as a success.
    assert row['output']['ok'] is True
    stub = row['output']['result']
    assert stub['payload_overflow'] is True and stub['chars'] > ai.AUDIT_INLINE_LIMIT
    assert len(stub['preview']) == ai.AUDIT_PREVIEW_CHARS
    assert len(json.dumps(row)) < ai.AUDIT_INLINE_LIMIT + 2000, 'the stub must stay small'
    digest = stub['sha256']
    assert box.read_context_evidence(digest, 0)['content']
    raw, offset = '', 0
    while offset is not None:
        page = box.read_context_evidence(digest, offset)
        raw, offset = raw + page['content'], page['next_offset']
    assert json.loads(raw) == {'tail': tail}


def test_job_events_carry_the_session(workspace, monkeypatch):
    monkeypatch.setenv('VASP_AGENT_SESSION', 'si-session')
    ai.core.event(workspace, 'demo', detail=1)
    row = json.loads((workspace / 'events.jsonl').read_text(encoding='utf-8').strip())
    assert row['session'] == 'si-session' and row['action'] == 'demo' and row['detail'] == 1


@pytest.mark.parametrize('output', [
    None,
    [],
    [{'type': 'message', 'role': 'assistant', 'content': []}],
    [object()],
    [SimpleNamespace(role='assistant')],
])
def test_malformed_response_output_does_not_end_session(workspace, output):
    responses = FakeResponses([SimpleNamespace(output=output, output_text='完成', status='completed')])
    agent = ai.Agent(SimpleNamespace(responses=responses), 'test', ai.Toolbox(workspace))
    assert agent.ask('你好') == '完成'
    history = json.loads(agent.file.read_text(encoding='utf-8'))
    assert history[0]['role'] == 'user'


def test_dict_function_call_and_unparseable_arguments_are_returned_to_model(workspace):
    responses = FakeResponses([
        reply({'type': 'function_call', 'name': 'inspect_structure', 'arguments': '{"a":', 'call_id': 'c1'},
              {'type': 'function_call', 'name': 'inspect_structure', 'arguments': '[]', 'call_id': 'c2'},
              {'type': 'function_call', 'name': 'inspect_structure', 'arguments': '{}'}),
        reply(text='参数有误。'),
    ])
    agent = ai.Agent(SimpleNamespace(responses=responses), 'test', ai.Toolbox(workspace))
    assert agent.ask('检查结构') == '参数有误。'
    outputs = [x for x in responses.requests[1]['input'] if x.get('type') == 'function_call_output']
    assert [x['call_id'] for x in outputs] == ['c1', 'c2']
    assert not any(json.loads(x['output'])['ok'] for x in outputs)


@pytest.mark.parametrize('next_action', [
    {'tool': 'get_input_review', 'requires_user_input': False},
    {'tool': 'get_input_review', 'requires_user_input': False, 'arguments': 'x'},
    'get_input_review',
])
def test_malformed_recovery_action_keeps_tool_result(workspace, monkeypatch, next_action):
    def failing(name, arguments):
        return {'ok': False, 'error_code': 'METADATA_READ_REQUIRES_SPECIALIZED_TOOL', 'next_action': next_action}
    responses = FakeResponses([reply(function('read_log', {'job_name': 'x', 'file': 'POTCAR'})), reply(text='好的。')])
    agent = ai.Agent(SimpleNamespace(responses=responses), 'test', ai.Toolbox(workspace))
    monkeypatch.setattr(agent.tools, 'call', failing)
    assert agent.ask('读元数据') == '好的。'
    output = json.loads(responses.requests[1]['input'][-1]['output'])
    assert output['error_code'] == 'METADATA_READ_REQUIRES_SPECIALIZED_TOOL'
    assert 'recovery' not in output


def test_model_switch_requires_new_model_to_reread_review_evidence(workspace):
    box = ai.Toolbox(workspace)
    ai.Agent(SimpleNamespace(responses=None), 'model-a', box, 'si-a')
    box.review_snapshots[('stage', None, 'si')] = 'snap'
    ai.Agent(SimpleNamespace(responses=None), 'model-b', box, 'si-b')
    assert box.review_model == 'model-b'
    with pytest.raises(ValueError, match='当前模型重新读取'):
        box.record_stage_review(None, 'si', 'snap', 'relaxation', [])


def test_staged_submit_script_is_audited_and_bound_to_its_source(workspace):
    first = workspace / 'a.slurm'
    first.write_text('#!/bin/bash\n#SBATCH --partition=compute\necho a\n')
    folder = workspace / 'runs/si'
    folder.mkdir(parents=True)
    box = ai.Toolbox(workspace, submit_script=first)
    box.stage_submit_script(folder)
    provenance = json.loads((folder / 'submit-script-source.json').read_text(encoding='utf-8'))
    assert provenance['source_sha256'] == box.script_hash
    assert 'submit_script_staged' in box.audit.read_text(encoding='utf-8')
    second = workspace / 'b.slurm'
    second.write_text('#!/bin/bash\n#SBATCH --partition=compute\necho b\n')
    with pytest.raises(ValueError, match='另一份提交脚本'):
        ai.Toolbox(workspace, submit_script=second).stage_submit_script(folder)


def test_local_submission_honours_target_restricted_grant(workspace, monkeypatch):
    monkeypatch.setattr('vasp_agent.permissions.submission_dialog.confirm', lambda *a, **kw: None)
    box = ai.Toolbox(workspace)
    assert box.call('prepare_calculation', prepare_args())['ok']
    script = workspace / 'job.slurm'
    script.write_text('#!/bin/bash\n')
    box = ai.Toolbox(workspace, submit_script=script, allow_submit=True)
    box.submission_target = ('local', 'another-job', None)
    monkeypatch.setattr(ai.core, 'verify', lambda folder: {'workflow': 'full'})
    monkeypatch.setattr(ai.subprocess, 'run', lambda *a, **k: pytest.fail('sbatch must not run'))
    result = box.call('submit_job', {'job_name': 'si-ai'})
    assert not result['ok'] and result.get('error_code') == 'SUBMISSION_NOT_AUTHORIZED'


def test_interactive_turn_failure_keeps_session_and_redacts_key(workspace):
    def broken(message, **kw):
        agent.history.append({'role': 'user', 'content': message})
        raise RuntimeError('proxy rejected sk-test-secret-0123456789')
    agent = ai.Agent(SimpleNamespace(responses=None), 'test', ai.Toolbox(workspace))
    agent.ask = broken
    replies = []
    ai.answer(SimpleNamespace(reply=replies.append), agent, '你好',
              SimpleNamespace(api_key='sk-test-secret-0123456789'))
    assert '/retry' in replies[0] and 'sk-test-secret' not in replies[0]
    assert json.loads(agent.file.read_text(encoding='utf-8'))[0]['content'] == '你好'


def test_refused_remote_submission_points_to_the_grant_menu(workspace):
    box = ai.Toolbox(workspace)
    result = box.call('submit_stage', {'server': 'eshell', 'job_name': 'si', 'phase': 'convergence'})
    assert not result['ok'] and result['error_code'] == 'SUBMISSION_NOT_AUTHORIZED'
    assert result['next_action'] == {'tool': 'request_submission', 'requires_user_input': True,
        'arguments': {'server': 'eshell', 'job_name': 'si', 'phase': 'convergence'}}


def test_downloaded_results_can_be_staged_for_analysis(workspace):
    import hashlib
    folder = workspace / 'downloads/eshell/si-scan-01-8d6e0213'
    folder.mkdir(parents=True)
    files = {'bands.csv': b'spin,band,k\n1,1,0\n', 'report.json': b'{"ok": true}', 'bands.png': b'\x89PNG'}
    for name, data in files.items():
        (folder / name).write_bytes(data)
    (folder / 'download-receipt.json').write_text(json.dumps({'files': {
        name: {'sha256': hashlib.sha256(data).hexdigest(), 'verified': True} for name, data in files.items()}}))
    box = ai.Toolbox(workspace)
    result = box.call('stage_download_for_analysis', {'server': 'eshell', 'job_name': 'si-scan-01',
                                                       'files': ['bands.csv', 'report.json']})
    assert result['ok'], result
    staged = result['result']['staged']
    assert staged[0]['path'] == 'data/eshell/si-scan-01/bands.csv'
    assert box.editor().read_data(staged[0]['path']) == files['bands.csv']
    assert not box.call('stage_download_for_analysis', {'server': 'eshell', 'job_name': 'si-scan-01',
                                                        'files': ['bands.png']})['ok']
    (folder / 'bands.csv').write_bytes(b'tampered')
    refused = box.call('stage_download_for_analysis', {'server': 'eshell', 'job_name': 'si-scan-01', 'files': ['bands.csv']})
    assert refused['error_code'] == 'INPUT_HASH_MISMATCH'


def _turn(status='completed', text='', items=(), reason=None, out=100, reasoning=None):
    return SimpleNamespace(status=status, output=list(items), output_text=text,
                           incomplete_details=SimpleNamespace(reason=reason) if reason else None,
                           usage=SimpleNamespace(input_tokens=100, output_tokens=out,
                                                 output_tokens_details=SimpleNamespace(reasoning_tokens=reasoning)))


def test_turn_cut_off_by_output_limit_retries_once_with_more_room(workspace):
    limits = []
    replies = [_turn('incomplete', reason='max_output_tokens', out=8000, reasoning=3200), _turn(text='完成')]
    class Adapter:
        output_limit = 8000
        client = None
        def create(self, model, instructions, history, tools, **kw):
            limits.append(self.output_limit)
            return replies.pop(0)
    config = SimpleNamespace(identity=lambda: 'e', summary_reasoning_effort=None, recent_context_tokens=12000,
                             context_window=240000, output_reserve=8000, context_safety=16000,
                             compact_trigger=.8, compact_target=.55, compaction_mode='summary')
    adapter = Adapter()
    agent = ai.Agent(SimpleNamespace(responses=None), 'test', ai.Toolbox(workspace), adapter=adapter, context_config=config)
    adapter.output_limit = 8000
    assert agent.ask('写分析脚本') == '完成'
    assert limits == [8000, 32000] and adapter.output_limit == 8000


def test_incomplete_turn_message_names_the_reason(workspace):
    responses = FakeResponses([_turn('incomplete', reason='content_filter', out=10)])
    agent = ai.Agent(SimpleNamespace(responses=responses), 'test', ai.Toolbox(workspace))
    message = agent.ask('你好')
    assert 'content_filter' in message and '没有执行' in message


def test_followup_message_is_labelled_and_never_counts_as_operator_text(workspace):
    message = ai.followup_message([{'server': 'eshell111-vasp544', 'job_name': 'la2cuo4-nm-cupv-02',
        'job_id': '71397255', 'phase': 'convergence', 'state': 'FAILED', 'workflow_status': 'failed'}])
    assert '非操作者输入' in message and '不构成任何授权' in message and '不得自动重交' in message
    assert '71397255' in message and 'Slurm FAILED' in message and '工作流 failed' in message
    responses = FakeResponses([reply(text='已核验')])
    agent = ai.Agent(SimpleNamespace(responses=responses), 'test', ai.Toolbox(workspace))
    agent.tools.latest_user_message = 'ENCUT 520'
    assert agent.ask(message, operator=False) == '已核验'
    assert agent.tools.latest_user_message == 'ENCUT 520'
    assert agent.history[0]['content'] == message


def test_interrupt_prompt_only_ends_an_idle_empty_prompt():
    from vasp_agent.ui.terminal_ui import TerminalUI
    calls = []
    app = SimpleNamespace(is_running=True, loop=SimpleNamespace(call_soon_threadsafe=calls.append),
                          exit=lambda result: calls.append(result))
    ui = TerminalUI('never')
    ui._prompt_session = SimpleNamespace(app=app, default_buffer=SimpleNamespace(text=''))
    assert ui.interrupt_prompt() and len(calls) == 1
    calls[0]()
    assert calls[-1] == ui.AUTO_PROMPT
    ui._prompt_session.default_buffer.text = '正在输入'
    assert not ui.interrupt_prompt()
    ui._prompt_session = None
    assert not ui.interrupt_prompt()
