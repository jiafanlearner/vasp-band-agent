from types import SimpleNamespace as NS

from vasp_agent.chat.ai_agent import Agent, Toolbox
from vasp_agent.chat.chat_commands import Commands
from vasp_agent.ui.terminal_ui import TerminalUI


def setup(tmp_path):
    toolbox = Toolbox(tmp_path)
    config = NS(identity=lambda: '123456abcdef', public=lambda: {'api_key': '[configured]'})
    def factory(name):
        return Agent(None, 'test', toolbox, f'{name}-{config.identity()}')
    output = []
    return Commands(factory('default'), factory, config, 'default', TerminalUI('never'),
                    reader=lambda _: '1', output=output.append), output


def test_new_resume_preserves_history_and_rejects_overwrite(tmp_path):
    commands, output = setup(tmp_path)
    commands.handle('/new first')
    first = commands.agent.file
    first.write_text('[{"role":"user","content":"old task"}]')
    commands.handle('/new second')
    commands.handle('/resume first')
    assert commands.agent.history[0]['content'] == 'old task'
    commands.handle('/new first')
    assert 'old task' in first.read_text()
    assert commands.name == 'first'


def test_sessions_filter_and_menu(tmp_path):
    commands, output = setup(tmp_path)
    commands.handle('/new first')
    (tmp_path / '.ai-session-other-000000000000.json').write_text('[]')
    assert {x[0] for x in commands.entries()} == {'first', 'other@000000000000'}
    commands.reader = lambda _: str(next(i for i, e in enumerate(commands.entries(), 1) if e[0] == 'first'))
    commands.handle('/sessions')
    assert commands.name == 'first'


def test_commands_never_call_model(tmp_path):
    commands, output = setup(tmp_path)
    for message in ('/', '/config', '/history', '/status', '/typo', '/new ../outside'):
        assert commands.handle(message) == 'handled'
    assert commands.handle('normal message') == 'pass'
    assert commands.handle('/exit') == 'exit'


def test_history_is_an_index_and_numbers_open_full_text(tmp_path):
    from vasp_agent.chat.chat_commands import summary_line
    long_reply = '## 远程实际状态\n\n**任务已准备**，未提交。\n- 第一项\n- 第二项'
    assert summary_line(long_reply, 60) == '远程实际状态（共 4 行）'
    assert summary_line('x' * 100, 20).endswith('…')
    c, output = setup(tmp_path)
    c.agent.history = [{'role': 'user', 'content': '继续'},
                       {'role': 'assistant', 'content': [{'type': 'output_text', 'text': long_reply}]},
                       {'type': 'function_call', 'name': 'get_status'}]
    c.handle('/history')
    assert any(line.strip().startswith('1  你  继续') for line in output)
    assert any('2  AI  远程实际状态（共 4 行）' in line for line in output)
    assert not any('**' in line or '##' in line for line in output)
    output.clear()
    c.handle('/history 2')
    assert '第 2 条 · AI' in output[0] and output[1] == long_reply
