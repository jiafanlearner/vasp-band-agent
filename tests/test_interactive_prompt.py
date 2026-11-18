import asyncio

import pytest

from prompt_toolkit.document import Document
from prompt_toolkit.input import create_pipe_input
from prompt_toolkit.output import DummyOutput

from vasp_agent.ui.interactive_prompt import SlashCompleter, build_prompt, command_catalog


def test_slash_filters_and_explains():
    completer = SlashCompleter()
    choices = list(completer.get_completions(Document('/'), None))
    assert '/sessions' in [x.text for x in choices]
    assert all(x.display_meta for x in choices)
    assert [x.text for x in completer.get_completions(Document('/mod'), None)] == ['/model']
    assert list(completer.get_completions(Document('ordinary message /mod'), None)) == []
    assert list(completer.get_completions(Document('/logs si'), None)) == []
    assert '任务名' in command_catalog()['/logs'][0]


def test_keyboard_select_then_submit():
    async def scenario():
        with create_pipe_input() as pipe:
            session = build_prompt(input=pipe, output=DummyOutput())
            task = asyncio.create_task(session.prompt_async())
            pipe.send_text('/mod')
            await asyncio.sleep(.15)
            pipe.send_text('\t')
            await asyncio.sleep(.15)
            pipe.send_text('\r')
            await asyncio.sleep(.15)
            assert not task.done()  # First Enter selects; it must not execute /model.
            pipe.send_text('\r')
            result = await asyncio.wait_for(task, 2)
            assert result.strip() == '/model'
    asyncio.run(scenario())


def test_multiline_bracketed_paste_then_menu_uses_same_input(monkeypatch):
    from vasp_agent.ui.terminal_ui import TerminalUI
    from concurrent.futures import ThreadPoolExecutor
    from threading import Event
    monkeypatch.setattr('builtins.input', lambda _: (_ for _ in ()).throw(
        AssertionError('must not mix builtins.input with PromptSession')))
    with create_pipe_input() as pipe, ThreadPoolExecutor(max_workers=1) as pool:
        session = build_prompt(input=pipe, output=DummyOutput())
        ui = TerminalUI(color='never')
        ui._prompt_session = session
        # The trailing newline belongs to the paste, not the parameter menu.
        pipe.send_text('\x1b[200~first line\nsecond line\n\x1b[201~\r')
        assert session.prompt() == 'first line\nsecond line\n'
        started = Event()
        def menu():
            started.set()
            return ui.read_menu('选择：')
        future = pool.submit(menu)
        try:
            assert started.wait(2)
            assert not future.done()
            pipe.send_text('2\r')
            assert future.result(timeout=5) == '2'
        finally:
            if not future.done():
                pipe.send_text('\x04')


def test_basic_menu_input_fallback(monkeypatch):
    from vasp_agent.ui.terminal_ui import TerminalUI
    prompts = []
    monkeypatch.setattr('builtins.input', lambda message: prompts.append(message) or 'q')
    ui = TerminalUI(color='never')
    assert ui.read_menu('选择：') == 'q'
    assert prompts == ['选择：']


@pytest.mark.parametrize('keys,error', [('1\r', None), ('\x03', KeyboardInterrupt), ('\x04', EOFError)])
def test_menu_restores_chat_settings_on_return_or_interrupt(keys, error):
    from vasp_agent.ui.terminal_ui import TerminalUI
    with create_pipe_input() as pipe:
        session = build_prompt(input=pipe, output=DummyOutput())
        ui = TerminalUI(color='never')
        ui._prompt_session = session
        original = {name: getattr(session, name) for name in
                    ('message', 'completer', 'bottom_toolbar', 'key_bindings')}
        pipe.send_text(keys)
        if error:
            with pytest.raises(error):
                ui.read_menu('输入编号切换，回车或 q 取消：')
        else:
            assert ui.read_menu('输入编号切换，回车或 q 取消：') == '1'
        for name, value in original.items():
            assert getattr(session, name) is value, name


def test_sessions_then_chat_then_plan_keep_distinct_prompts(tmp_path, monkeypatch):
    from prompt_toolkit.formatted_text import to_formatted_text, fragment_list_to_text
    from test_chat_commands import setup
    from test_parameter_plans import request, SUGGESTED
    import vasp_agent.planning.parameter_plans as plans
    from pathlib import Path
    import vasp_agent.chat.ai_agent as ai_agent
    import sys

    commands, _ = setup(tmp_path)
    box = commands.agent.tools
    box.plan_reader = None
    (tmp_path / 'POSCAR').write_bytes((Path(__file__).parents[1] / 'examples/Si.POSCAR').read_bytes())
    draft = plans.propose(box, request(), None, SUGGESTED, 'ai_suggestion', 'test reason')
    commands.handle('/new saved')
    commands.handle('/new current')
    commands.entries = lambda: [('saved', 0, 'saved test conversation')]
    monkeypatch.setattr(sys.stdin, 'isatty', lambda: True)
    monkeypatch.setattr(sys.stdout, 'isatty', lambda: True)
    monkeypatch.setenv('TERM', 'xterm')
    monkeypatch.setattr(ai_agent.Agent, 'ask', lambda *a, **kw: pytest.fail('menus must not call model'))

    with create_pipe_input() as pipe:
        session = build_prompt(input=pipe, output=DummyOutput())
        ui = commands.ui
        ui._prompt_session, ui._prompt_color = session, False
        commands.reader = box.plan_reader = ui.read_menu
        observed = []
        def inspect_prompt():
            text = fragment_list_to_text(to_formatted_text(session.message))
            observed.append(text)
            if text != '你 > ':
                assert session.completer is None
                assert session.bottom_toolbar == ''
            else:
                assert isinstance(session.completer, SlashCompleter)
                assert callable(session.bottom_toolbar)
        # Actual PromptSession pre_run callback inspects what the user sees.
        session.app.pre_run_callables.append(inspect_prompt)
        pipe.send_text('1\r')
        assert commands.handle('/sessions') == 'handled'
        assert commands.name == 'saved'

        session.app.pre_run_callables.append(inspect_prompt)
        pipe.send_text('/plan ' + draft['id'] + '\r')
        message = ui.prompt()
        assert message == '/plan ' + draft['id']
        session.app.pre_run_callables.append(inspect_prompt)
        pipe.send_text('2\r')
        assert commands.handle(message) == 'handled'
        assert plans.list_plans(box)['plans'][0]['choice'] == 'operator_accept_ai'

        session.app.pre_run_callables.append(inspect_prompt)
        pipe.send_text('/history\r')
        assert commands.handle(ui.prompt()) == 'handled'
        assert observed[0].startswith('输入编号切换')
        assert observed[1] == observed[3] == '你 > '
        assert observed[2].startswith('输入编号（d 看完整理由；s 切换判据档位')
        assert not box.job('scan-task').exists()


def test_ctrl_t_shows_trace_without_submitting_or_losing_input():
    async def scenario():
        from threading import Event
        shown = Event()
        with create_pipe_input() as pipe:
            session = build_prompt(input=pipe, output=DummyOutput(), on_trace=shown.set)
            task = asyncio.create_task(session.prompt_async())
            pipe.send_text('unfinished\x14')
            for _ in range(40):
                if shown.is_set():
                    break
                await asyncio.sleep(.025)
            assert shown.is_set() and not task.done()
            pipe.send_text('\r')
            assert await asyncio.wait_for(task, 2) == 'unfinished'
    asyncio.run(scenario())


def test_supported_colors_build_prompt_styles():
    from vasp_agent.ui.terminal_ui import COLOR_CODES
    with create_pipe_input() as pipe:
        for name in COLOR_CODES:
            session = build_prompt(input=pipe, output=DummyOutput(), user_color=name)
            expected = '' if name == 'default' else 'ansi' + name.replace('_', '')
            expected = {'white': 'ansigray', 'bright_white': 'ansiwhite'}.get(name, expected)
            assert dict(session.style.style_rules)[''] == expected


def test_toolbar_shows_monitoring_line_above_hints():
    from prompt_toolkit.output import DummyOutput
    from prompt_toolkit.input import create_pipe_input
    from vasp_agent.ui.interactive_prompt import build_prompt
    with create_pipe_input() as pipe:
        session = build_prompt(input=pipe, output=DummyOutput(), status=lambda: '监控中 · job RUNNING')
        fragments = session.bottom_toolbar()
        assert fragments[0][1] == '监控中 · job RUNNING' and fragments[1][1] == '\n'
        assert session.refresh_interval == 30
        quiet = build_prompt(input=pipe, output=DummyOutput(), status=lambda: '')
        assert not any('监控' in text for _, text in quiet.bottom_toolbar())


def test_plain_input_prints_status_only_when_it_changes(monkeypatch):
    from vasp_agent.ui.terminal_ui import TerminalUI
    ui = TerminalUI('never')
    ui._basic_input = True
    shown, lines = [], iter(['监控中 · a', '监控中 · a', '监控中 · b'])
    ui.status_provider = lambda: next(lines)
    ui.activity = shown.append
    monkeypatch.setattr('builtins.input', lambda prompt: '')
    for _ in range(3):
        ui.prompt()
    assert shown == ['监控中 · a', '监控中 · b']
