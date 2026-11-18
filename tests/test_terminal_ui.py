import io
import json
from types import SimpleNamespace as NS

import pytest
from rich.console import Console
from rich.cells import cell_len

from vasp_agent.ui.terminal_ui import TerminalUI
from vasp_agent.chat.ai_agent import Agent, Toolbox
from test_ai_agent import FakeResponses, reply, function, Item


@pytest.fixture
def screen():
    stream = io.StringIO()
    console = Console(file=stream, width=64, color_system=None, force_terminal=False)
    ui = TerminalUI('never')
    ui.console = lambda stream=None: console
    return ui, stream


def test_reply_renders_markdown_table_and_code_without_changing_text(screen):
    ui, stream = screen
    text = '### 审阅结论\n\n**尚未提交**，使用 `NBANDS=16`。\n\n| 参数 | 值 |\n|---|---|\n| 网格 | 12×12×12 |\n\n```python\nnbands = 16\n```'
    ui.reply(text)
    rendered = stream.getvalue()
    for marker in ('###', '**', '```', '`NBANDS', '|---', '\x1b'):
        assert marker not in rendered
    for word in ('审阅结论', '尚未提交', 'NBANDS=16', '12×12×12', 'nbands = 16'):
        assert word in rendered
    assert max(cell_len(line) for line in rendered.splitlines()) <= 64
    assert '**尚未提交**' in text


def test_code_highlights_when_color_enabled(monkeypatch, capsys):
    ui = TerminalUI('always')
    ui.reply('```python\nanswer = 16\n```')
    assert '\x1b[' in capsys.readouterr().out
    ui.color = 'never'
    ui.reply('**结果**')
    assert '\x1b' not in capsys.readouterr().out


def test_gbk_redirect_does_not_crash_on_decorative_symbols():
    buffer = io.BytesIO()
    stream = io.TextIOWrapper(buffer, encoding='gbk')
    ui = TerminalUI('never')
    ui.console(stream).print('✓ 检查完成 • 尚未提交')
    stream.flush()
    assert '检查完成' in buffer.getvalue().decode('gbk')


def test_trace_reports_failure_details_and_redacts_controls(screen):
    ui, stream = screen
    ticket = ui.tool_started('get_input_review', {'server': 'test', 'api_key': 'hidden-value'})
    output = {'ok': False, 'error_code': 'FILE_NOT_FOUND', 'error': '缺少输入\x1b[2J',
              'secret': 'hidden-secret', 'POTCAR': 'hidden-body'}
    ui.tool_finished(ticket, output)
    ui.show_trace('1')
    result = stream.getvalue()
    assert '失败 #1' in result and 'FILE_NOT_FOUND' in result
    assert 'hidden-value' not in result and 'hidden-secret' not in result and 'hidden-body' not in result
    assert '\x1b' not in result
    assert output['POTCAR'] == 'hidden-body'  # Display redaction must not mutate evidence.


def test_trace_retention_and_detail_truncation(screen):
    ui, stream = screen
    for i in range(101):
        ticket = ui.tool_started('read_log', {'job_name': 'example'})
        ui.tool_finished(ticket, {'ok': True, 'result': {'tail': 'x' * 20000}})
    assert len(ui.trace) == 100 and ui.trace[0]['id'] == 2
    assert ui.trace[-1]['truncated'] and len(ui.trace[-1]['detail']) == 16000
    ui.show_trace('1')
    assert '最近 100 条' in stream.getvalue()
    ui.show_trace('101')
    assert '显示已截断' in stream.getvalue()


def test_agent_work_events_are_real_ordered_and_reasoning_is_not_displayed(tmp_path, screen, monkeypatch):
    ui, stream = screen
    responses = FakeResponses([
        reply(Item(type='reasoning', encrypted_content='not-for-display', summary=[]),
              function('list_files', {}), text='先查看工作目录。'),
        reply(text='目录检查完成，未提交。'),
    ])
    box = Toolbox(tmp_path)
    original = box.list_files
    def run():
        assert '执行 #1 list_files' in stream.getvalue()
        assert '完成 #1' not in stream.getvalue()
        return original()
    monkeypatch.setattr(box, 'list_files', run)
    agent = Agent(NS(responses=responses), 'test', box, ui=ui)
    final = agent.ask('仅检查工作目录')
    ui.reply(final)
    output = stream.getvalue()
    assert output.index('等待模型') < output.index('先查看') < output.index('执行 #1') < output.index('完成 #1')
    assert 'not-for-display' not in output
    assert len(ui.trace) == 1 and ui.trace[0]['state'] == 'success'
    history = json.loads(agent.file.read_text(encoding='utf-8'))
    assert next(x for x in history if x.get('type') == 'reasoning')['encrypted_content'] == 'not-for-display'
    assert not any('执行 #1' in json.dumps(item) for item in history)
    assert box.submissions == 0


def test_trace_command_is_local_and_does_not_call_model(tmp_path, screen):
    from test_chat_commands import setup
    commands, _ = setup(tmp_path)
    commands.ui = screen[0]
    assert commands.handle('/trace') == 'handled'
    assert '尚无工具执行记录' in screen[1].getvalue()


def test_render_failure_does_not_drop_completed_tool_output(tmp_path, screen, monkeypatch):
    ui, _ = screen
    def broken(*args):
        raise RuntimeError('renderer failure')
    monkeypatch.setattr(ui, 'tool_finished', broken)
    responses = FakeResponses([reply(function('list_files', {})), reply(text='完成')])
    agent = Agent(NS(responses=responses), 'test', Toolbox(tmp_path), ui=ui)
    assert agent.ask('查看目录') == '完成'
    outputs = [i for i in agent.history if i.get('type') == 'function_call_output']
    assert len(outputs) == 1 and json.loads(outputs[0]['output'])['ok']
    assert len(json.loads(agent.file.read_text(encoding='utf-8'))) == len(agent.history)


def test_color_settings_persist_and_reset(tmp_path):
    ui = TerminalUI(workspace=tmp_path)
    ui.color_settings('user magenta')
    ui.color_settings('success cyan')
    fresh = TerminalUI(workspace=tmp_path)
    assert fresh.palette['user'] == 'magenta' and fresh.palette['success'] == 'cyan'
    assert fresh.COLORS['user'] == '\033[35m'
    fresh.color_settings('reset')
    assert TerminalUI(workspace=tmp_path).palette['user'] == 'bright_blue'


@pytest.mark.parametrize('argument', ['user invalid', 'wrong blue', 'user \x1b[32m', 'user blue extra'])
def test_bad_color_does_not_change_saved_settings(tmp_path, argument):
    ui = TerminalUI(workspace=tmp_path)
    ui.color_settings('user cyan')
    previous = ui.palette_file.read_bytes()
    with pytest.raises(ValueError):
        ui.color_settings(argument)
    assert ui.palette_file.read_bytes() == previous
    assert ui.palette['user'] == 'cyan'


def test_corrupt_palette_uses_defaults_without_overwriting(tmp_path, capsys):
    path = tmp_path / '.terminal-colors.json'
    path.write_text('not json')
    ui = TerminalUI(workspace=tmp_path)
    assert ui.palette['user'] == 'bright_blue'
    assert path.read_text() == 'not json'
    assert '使用默认配色' in capsys.readouterr().err


def test_color_command_is_local_and_changes_rendering(tmp_path, monkeypatch):
    from test_chat_commands import setup
    commands, output = setup(tmp_path)
    commands.ui = TerminalUI(workspace=tmp_path)
    commands.agent.ask = lambda *a: pytest.fail('must not call model')
    assert commands.handle('/settings colors success magenta') == 'handled'
    assert commands.ui.palette['success'] == 'magenta'
    ui = commands.ui
    stream = io.StringIO()
    console = Console(file=stream, force_terminal=True, color_system='standard', no_color=False, legacy_windows=False)
    monkeypatch.setattr(ui, 'console', lambda: console)
    ticket = ui.tool_started('demo', {})
    ui.tool_finished(ticket, {'ok': True, 'result': {}})
    assert '\x1b[35m' in stream.getvalue()


def test_ambiguous_symbols_get_a_display_space_only_on_wide_consoles():
    from vasp_agent.ui.terminal_ui import pad_ambiguous
    text = 'ENCUT 520 eV≈2.12×ENMAX，α=β=γ=60°，9³，Å'
    assert pad_ambiguous(text, wide=True) == 'ENCUT 520 eV≈ 2.12× ENMAX，α =β =γ =60°，9³，Å'
    assert pad_ambiguous(text, wide=False) == text


def test_ambiguous_width_override(monkeypatch):
    from vasp_agent.ui.terminal_ui import ambiguous_wide
    monkeypatch.setenv('VASP_AGENT_AMBIGUOUS_WIDE', '0')
    assert not ambiguous_wide()
    monkeypatch.setenv('VASP_AGENT_AMBIGUOUS_WIDE', '1')
    assert ambiguous_wide()
