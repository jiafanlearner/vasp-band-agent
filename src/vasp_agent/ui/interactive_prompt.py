"""Live slash-command discovery with descriptions and keyboard completion."""
from prompt_toolkit import PromptSession
from prompt_toolkit.completion import Completer, Completion
from prompt_toolkit.filters import Condition
from prompt_toolkit.key_binding import KeyBindings
from prompt_toolkit.styles import Style


def command_catalog():
    import re
    from vasp_agent.chat.chat_commands import HELP
    from vasp_agent.chat.extra_commands import HELP as MORE_HELP
    catalog = {}
    for line in (HELP + MORE_HELP).splitlines():
        fields = re.split(r'\s{2,}', line.strip(), maxsplit=1)
        if len(fields) == 2 and fields[0].startswith('/'):
            command = fields[0].split()[0]
            catalog[command] = (fields[0], fields[1])
    return catalog


class SlashCompleter(Completer):
    def get_completions(self, document, complete_event):
        text = document.text_before_cursor
        if text.startswith('/watch ') and not document.text_after_cursor:
            prefix = text[len('/watch '):]
            for action, description in [('status', '查看监控目标、间隔及下次检查时间'),
                                        ('on', '开启周期只读检查'),
                                        ('off', '暂停周期检查，保留监控记录'),
                                        ('auto', '启用自动建议间隔，保持开启/暂停状态'),
                                        ('interval', '固定全局间隔：1–1440 整数分钟'),
                                        ('check', '立即执行一次只读检查'),
                                        ('remove', '移除 JobID [服务器]，作业继续运行')]:
                if action.startswith(prefix):
                    yield Completion(action, start_position=-len(prefix), display=action, display_meta=description)
            return
        if text.startswith('/submit ') and not document.text_after_cursor:
            prefix = text[len('/submit '):]
            for action, description in [('once', '授权一次提交尝试，用完自动关闭'),
                                        ('on', '选择服务器、资源上限及提交次数'),
                                        ('off', '关闭后续提交，不停止已有作业'),
                                        ('status', '查看当前权限与剩余额度')]:
                if action.startswith(prefix):
                    yield Completion(action, start_position=-len(prefix), display=action, display_meta=description)
            return
        if not text.startswith('/') or any(c.isspace() for c in text) or document.text_after_cursor:
            return
        for command, (usage, description) in command_catalog().items():
            if command.startswith(text):
                yield Completion(command, start_position=-len(text),
                                 display=command, display_meta=description)


def build_prompt(color=True, on_trace=None, user_color='bright_blue', status=None, **kwargs):
    bindings = KeyBindings()
    session = None

    @bindings.add('enter', filter=Condition(lambda: bool(session and session.default_buffer.complete_state and
                    session.default_buffer.complete_state.current_completion)))
    def select(event):
        buffer = event.current_buffer
        completion = buffer.complete_state.current_completion
        buffer.apply_completion(completion)
        usage = command_catalog().get(buffer.text, (buffer.text, ''))[0]
        if ' ' in usage and not buffer.text.endswith(' '):
            buffer.insert_text(' ')
        # Selecting a command does not execute it: inspect its hint, then press Enter.

    @bindings.add('escape', eager=True)
    def dismiss(event):
        event.current_buffer.cancel_completion()

    @bindings.add('c-t')
    def transcript(event):
        if on_trace is not None:
            from prompt_toolkit.application import run_in_terminal
            run_in_terminal(on_trace)

    def toolbar():
        text = session.default_buffer.text
        command = text.split(' ', 1)[0]
        details = command_catalog().get(command)
        if details:
            hint = [('class:hint', f'用法：{details[0]}  ·  {details[1]}  | Enter 执行 · Esc 收起')]
        else:
            hint = [('class:hint', '输入 / 查看命令 · Tab 补全 · Enter 确认 · Ctrl+T 工作过程')]
        line = ''
        if status is not None:
            try:
                line = status() or ''
            except Exception:
                line = ''
        # Background monitoring shows here, above the key hints, without filling the scrollback.
        return ([('class:monitor', line), ('', '\n')] if line else []) + hint

    foreground = '' if user_color == 'default' else 'ansi' + user_color.replace('_', '')
    foreground = {'white': 'ansigray', 'bright_white': 'ansiwhite'}.get(user_color, foreground)
    style = Style.from_dict({
        '': foreground, 'prompt': (foreground + ' bold').strip(),
        'completion-menu.completion': 'bg:#20252b #eeeeee',
        'completion-menu.completion.current': 'bg:#174760 #66d9ff bold',
        'completion-menu.meta.completion': 'bg:#20252b #aaaaaa',
        'completion-menu.meta.completion.current': 'bg:#174760 #eeeeee',
        'hint': '#aaaaaa', 'monitor': '#66d9ff',
    }) if color else Style.from_dict({'completion-menu.completion.current': 'reverse',
                                      'completion-menu.meta.completion.current': 'reverse'})
    session = PromptSession([('class:prompt', '你 > ')], completer=SlashCompleter(),
                            complete_while_typing=True, reserve_space_for_menu=10,
                            key_bindings=bindings, bottom_toolbar=toolbar,
                            # Redraw periodically so the monitoring line stays current while idle.
                            refresh_interval=30 if status is not None else 0,
                            style=style, **kwargs)
    return session
