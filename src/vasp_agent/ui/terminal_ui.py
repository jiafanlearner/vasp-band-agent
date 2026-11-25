"""Small terminal presentation layer; saved conversations remain plain text."""
import os
import sys
import json
import re
import time

PALETTE_DEFAULTS = {'user': 'bright_blue', 'success': 'bright_blue',
                    'running': 'cyan', 'error': 'bright_red', 'emphasis': 'bright_blue'}
COLOR_CODES = dict(zip(('black', 'red', 'green', 'yellow', 'blue', 'magenta', 'cyan', 'white'), range(30, 38)))
COLOR_CODES.update({'bright_' + name: number + 60 for name, number in list(COLOR_CODES.items())})
COLOR_CODES['default'] = 39


def code_theme():
    """Blue/violet syntax without the yellow/green Monokai accents."""
    from pygments.token import Token
    from rich.style import Style
    from rich.syntax import ANSISyntaxTheme
    return ANSISyntaxTheme({
        Token.Text: Style(color='default'),
        Token.Keyword: Style(color='bright_magenta', bold=True),
        Token.Name: Style(color='bright_blue'),
        Token.Literal: Style(color='bright_magenta'),
        Token.Operator: Style(color='bright_white'),
        Token.Punctuation: Style(color='bright_white'),
        Token.Comment: Style(color='default', dim=True),
        Token.Error: Style(color='bright_red', underline=True),
    })


def ambiguous_wide():
    """Whether this console draws East Asian 'ambiguous' symbols two cells wide.

    CJK code pages and locales (and mintty with a CJK font) draw Greek letters,
    math operators and the multiplication sign double-width, while Rich counts
    them as one cell, so the next character is drawn over their right half.
    VASP_AGENT_AMBIGUOUS_WIDE=1/0 overrides the guess.
    """
    forced = os.environ.get('VASP_AGENT_AMBIGUOUS_WIDE')
    if forced in ('0', '1'):
        return forced == '1'
    import locale
    language = (os.environ.get('LC_ALL') or os.environ.get('LC_CTYPE') or os.environ.get('LANG') or '').lower()
    encoding = (locale.getpreferredencoding(False) or '').lower()
    return language.startswith(('zh', 'ja', 'ko')) or encoding in ('cp936', 'gbk', 'gb2312', 'gb18030', 'cp950', 'big5', 'cp932', 'shift_jis', 'cp949', 'euc-kr')


def _overdrawn(c):
    import unicodedata
    code = ord(c)
    return (unicodedata.east_asian_width(c) == 'A'
            and (0x0370 <= code <= 0x04FF or 0x2200 <= code <= 0x22FF or c in '×÷±'))


def pad_ambiguous(text, wide=None):
    """Display copy only: a space after symbols the console draws two cells wide."""
    if not (ambiguous_wide() if wide is None else wide):
        return text
    return ''.join(c + ' ' if _overdrawn(c) else c for c in text)


def display_safe(value):
    """Redact credentials and terminal control sequences in display copies only."""
    from vasp_agent.chat.ai_agent import scrub
    if isinstance(value, dict):
        return {str(k): '[已隐藏]' if any(word in str(k).lower() for word in
                ('api_key', 'password', 'secret', 'token', 'private_key', 'encrypted_content', 'potcar'))
                else display_safe(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [display_safe(v) for v in value]
    if isinstance(value, str):
        value = scrub(value)
        value = re.sub(r'\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)', '', value)
        value = re.sub(r'\x1b\[[0-?]*[ -/]*[@-~]', '', value)
        return ''.join(c for c in value if c in '\n\t' or (c.isprintable() and c != '\x7f'))
    return value


class TerminalUI:
    RESET = '\033[0m'
    COLORS = {'user': '\033[94m', 'assistant': '\033[96m',
              'success': '\033[94m', 'error': '\033[91m'}

    def __init__(self, color='auto', workspace=None):
        self.color = color
        self._prompt_session = None
        self.status_provider = None  # callable -> one-line background status ('' when idle)
        self._last_status = None
        self._prompt_color = None
        self._basic_input = False
        self.trace = []
        self._trace_id = 0
        self._menu_wait = 0.0
        self._ticket_wait = {}
        self.palette = dict(PALETTE_DEFAULTS)
        from vasp_agent.common import agent_paths
        self.palette_file = agent_paths.file_path(workspace, 'terminal_colors') if workspace is not None else None
        if self.palette_file is not None and self.palette_file.exists():
            try:
                data = json.loads(self.palette_file.read_text(encoding='utf-8'))
                self.validate_palette(data)
                self.palette.update(data)
            except (OSError, ValueError, TypeError):
                print('颜色配置无法读取或无效，使用默认配色；可用 /settings colors reset 恢复。', file=sys.stderr)
        self.apply_palette()

    @staticmethod
    def validate_palette(data):
        if not isinstance(data, dict) or any(k not in PALETTE_DEFAULTS or not isinstance(v, str)
                                             or v not in COLOR_CODES for k, v in data.items()):
            raise ValueError('无效颜色配置；用 /settings colors 查看角色和可用颜色。')

    def apply_palette(self):
        self.COLORS = {**type(self).COLORS, **{role: f'\033[{COLOR_CODES[color]}m'
                                            for role, color in self.palette.items()}}
        # Rebuild style on the next main prompt; menus keep their scoped settings.
        self._prompt_color = None

    def color_settings(self, argument):
        parts = argument.split()
        if parts == ['reset']:
            candidate = dict(PALETTE_DEFAULTS)
        elif len(parts) == 2:
            self.validate_palette({parts[0]: parts[1]})
            candidate = {**self.palette, parts[0]: parts[1]}
        elif not parts:
            return {'colors': dict(self.palette), 'available': list(COLOR_CODES),
                    'usage': '/settings colors user bright_blue | success cyan | running yellow | error red | emphasis bright_blue | reset',
                    'saved_to': str(self.palette_file) if self.palette_file else None}
        else:
            raise ValueError('用法：/settings colors [角色 颜色] 或 /settings colors reset')
        if self.palette_file is not None:
            from vasp_agent.workflow.vasp_band_agent import save
            self.palette_file.parent.mkdir(parents=True, exist_ok=True)
            save(self.palette_file, candidate)
        self.palette = candidate
        self.apply_palette()
        return {'colors': dict(self.palette), 'saved': self.palette_file is not None,
                'note': '新输出立即生效，输入配色在下一次提示生效；color never 时不显示颜色。'}

    def console(self, stream=None):
        from rich.console import Console
        from rich.theme import Theme
        stream = stream or sys.stdout
        # Redirected Windows stdout may use GBK. Unsupported decorative glyphs
        # must not crash the workflow; native UTF-8 terminals retain them.
        if hasattr(stream, 'reconfigure'):
            stream.reconfigure(errors='replace')
        colored = self.enabled(stream)
        theme = Theme({
            'markdown.strong': 'bold ' + self.palette['emphasis'],
            'markdown.code': 'bold bright_cyan on default',
            'markdown.block_quote': 'bold ' + self.palette['emphasis'],
            'markdown.h1': 'bold bright_cyan',
            'markdown.h1.border': 'bright_cyan',
            'markdown.h2': 'bold bright_cyan',
            'markdown.h3': 'bold bright_cyan',
            'markdown.h4': 'bold bright_cyan',
            'markdown.table.header': 'bold bright_cyan',
            'markdown.item.number': 'bold bright_cyan',
        })
        return Console(file=stream, force_terminal=colored, color_system='standard' if colored else None,
                       no_color=not colored, highlight=False, markup=False, legacy_windows=False, theme=theme)

    def enabled(self, stream):
        if self.color == 'never':
            return False
        if self.color == 'always':
            return True
        if 'NO_COLOR' in os.environ or os.environ.get('TERM') == 'dumb' or not stream.isatty():
            return False
        if os.name == 'nt' and not os.environ.get('TERM'):
            # Native Windows consoles need VT processing; Git Bash already supports ANSI.
            try:
                import ctypes
                from ctypes import wintypes
                kernel = ctypes.windll.kernel32
                kernel.GetStdHandle.restype = wintypes.HANDLE
                kernel.GetConsoleMode.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
                kernel.SetConsoleMode.argtypes = [wintypes.HANDLE, wintypes.DWORD]
                handle = kernel.GetStdHandle(-12 if stream is sys.stderr else -11)
                mode = wintypes.DWORD()
                return bool(kernel.GetConsoleMode(handle, ctypes.byref(mode)) and
                            kernel.SetConsoleMode(handle, mode.value | 4))
            except (AttributeError, OSError):
                return False
        return True

    def write(self, text, role, stream=None):
        stream = stream or sys.stdout
        if self.enabled(stream):
            print(self.COLORS[role] + text + self.RESET, file=stream)
        else:
            print(text, file=stream)

    def prompt(self):
        colored = self.enabled(sys.stdout)
        if not self._basic_input and sys.stdin.isatty() and sys.stdout.isatty() and os.environ.get('TERM') != 'dumb':
            try:
                from vasp_agent.ui.interactive_prompt import build_prompt
                if self._prompt_session is None or self._prompt_color != colored:
                    self._prompt_session = build_prompt(color=colored, on_trace=self.show_trace,
                                                        user_color=self.palette['user'], status=self.status_provider)
                    self._prompt_color = colored
            except (ImportError, OSError):
                self._basic_input = True
                print('实时菜单不可用，使用普通输入；输入 /help 查看命令。', file=sys.stderr)
            except Exception as exc:
                if type(exc).__name__ != 'NoConsoleScreenBufferError':
                    raise
                self._basic_input = True
                print('当前 Git Bash 未提供控制台接口；请用 winpty python ai_agent.py 启动实时菜单，或使用 Windows Terminal。', file=sys.stderr)
            else:
                return self._prompt_session.prompt()
        if self.status_provider is not None:
            # Plain input has no status bar: print the line when it changes.
            try:
                line = self.status_provider() or ''
            except Exception:
                line = ''
            if line and line != self._last_status:
                self.activity(line)
            self._last_status = line
        if colored:
            # Set color outside input's prompt so readline counts prompt width correctly.
            sys.stdout.write(self.COLORS['user'])
            sys.stdout.flush()
        try:
            return input('你 > ')
        finally:
            if colored:
                sys.stdout.write(self.RESET)
                sys.stdout.flush()

    AUTO_PROMPT = '\x00auto-followup'

    def interrupt_prompt(self):
        """End an idle, empty main prompt from another thread; returns whether it did.

        Never discards typing: a non-empty input line is left alone and the queued
        event waits until that message has been handled.
        """
        session = self._prompt_session
        app = getattr(session, 'app', None)
        if session is None or app is None or not getattr(app, 'is_running', False):
            return False
        if session.default_buffer.text.strip():
            return False
        loop = getattr(app, 'loop', None)
        if loop is None:
            return False
        loop.call_soon_threadsafe(lambda: app.exit(result=self.AUTO_PROMPT) if app.is_running else None)
        return True

    def reply(self, text):
        from vasp_agent.ui.markdown_layout import TerminalMarkdown
        console = self.console()
        console.print('\n● 助理', style='bold')
        console.print(TerminalMarkdown(pad_ambiguous(display_safe(text)), code_theme=code_theme(), hyperlinks=False))
        console.print()

    def activity(self, text):
        self.console().print('• ' + display_safe(text), style='dim')

    def job_notification(self, event):
        """Display a background observation in at most two lines, outside tool traces."""
        from rich.text import Text

        def brief(value, limit=180):
            if not isinstance(value, (str, int, float)):
                return ''
            return ' '.join(str(display_safe(str(value))).split())[:limit]

        server = brief(event.get('server') or '本机', 40)
        job_id = brief(event.get('job_id'), 40) or '—'
        name = brief(event.get('job_name'), 48)
        state = brief(event.get('state') or event.get('scheduler_state') or 'unknown', 40)
        workflow = brief(event.get('workflow_status'), 40)
        phase = brief(event.get('phase'), 40)
        failed = (event.get('kind') == 'error' or bool(event.get('error'))
                  or workflow in ('failed', 'blocked') or event.get('outcome') in ('failed', 'cancelled')
                  or state.upper().split('+', 1)[0] in
                  {'FAILED', 'CANCELLED', 'TIMEOUT', 'OUT_OF_MEMORY', 'NODE_FAIL', 'BOOT_FAIL', 'DEADLINE'})
        context = f'{server} · JobID {job_id}' + (f' · {name}' if name else '')
        if event.get('kind') == 'error' or event.get('error'):
            header = f'• 监控 {context} · 读取失败'
            detail = brief(event.get('message') or event.get('error')) or '稍后重试；可用 /watch check 只读核对。'
        else:
            scheduler = 'Slurm 已结束（COMPLETED）' if state.upper() == 'COMPLETED' else f'Slurm {state}'
            if event.get('kind') == 'recovered':
                scheduler = '查询已恢复 · ' + scheduler
            header = f'• 监控 {context} · {scheduler}'
            if event.get('outcome') in ('failed', 'cancelled') or workflow in ('failed', 'blocked'):
                detail = '本次作业失败、取消或工作流受阻；请核查状态和日志。不会自动重交。'
            elif workflow == 'complete':
                detail = '工作流已完成（complete）。'
            elif workflow == 'awaiting_review':
                next_phase = brief(event.get('next_phase'), 40)
                detail = '待审阅（awaiting_review）' + (f' · 下一阶段：{next_phase}' if next_phase else '')
                detail += '；需审阅后提交。'
            elif state.upper() == 'COMPLETED':
                detail = 'Slurm 作业已结束；工作流完成状态仍需核对。'
            else:
                detail = (f'阶段：{phase} · ' if phase else '') + (f'工作流：{workflow}' if workflow else '')
                detail = detail.strip(' ·') or brief(event.get('message'))
            progress = event.get('array_progress')
            if isinstance(progress, dict) and all(type(progress.get(k)) is int for k in ('terminal', 'total')):
                detail = f'数组已结束 {progress["terminal"]}/{progress["total"]} 项 · ' + detail
        console = self.console()
        role = 'error' if failed else 'success' if event.get('kind') == 'terminal' else 'running'
        console.print(Text(header, style=self.palette[role]), no_wrap=True, overflow='ellipsis')
        if detail:
            console.print(Text('  ' + detail, style='dim'), no_wrap=True, overflow='ellipsis')

    def commentary(self, text):
        from vasp_agent.ui.markdown_layout import TerminalMarkdown
        self.console().print(TerminalMarkdown(pad_ambiguous(display_safe(text)), code_theme=code_theme(), hyperlinks=False))

    def tool_started(self, name, arguments):
        from rich.text import Text
        from vasp_agent.editing.workspace_editor import EDIT_TOOLS, safe_value
        if name in EDIT_TOOLS:
            arguments = safe_value(arguments)
        self._trace_id += 1
        entry = {'id': self._trace_id, 'tool': display_safe(name),
                 'arguments': display_safe(arguments), 'state': 'running'}
        self.trace.append(entry)
        self.trace = self.trace[-100:]
        context = ' '.join(f'{k}={entry["arguments"][k]}' for k in
                           ('server', 'job_name', 'action', 'path', 'plan_id', 'phase')
                           if isinstance(entry['arguments'], dict) and entry['arguments'].get(k) is not None)
        self.console().print(Text(f'• 执行 #{entry["id"]} {entry["tool"]}  {context[:240]}', style=self.palette['running']))
        self._ticket_wait[entry['id']] = self._menu_wait
        if len(self._ticket_wait) > 200:
            self._ticket_wait = {k: v for k, v in self._ticket_wait.items() if k > self._trace_id - 100}
        return entry, time.monotonic()

    def tool_finished(self, ticket, output):
        from rich.text import Text
        entry, started = ticket
        total = max(0.0, time.monotonic() - started)
        wait_start = self._ticket_wait.pop(entry['id'], None)
        wait = max(0.0, min(total, self._menu_wait - wait_start)) if wait_start is not None else 0.0
        exec_seconds = max(0.0, total - wait)
        entry.update(state='success' if output.get('ok') else 'failed',
                     seconds=round(total, 2),
                     exec_seconds=round(exec_seconds, 2),
                     wait_seconds=round(wait, 2))
        # Details are bounded and separate from canonical model/audit history.
        detail = json.dumps(display_safe(output), ensure_ascii=False, indent=2, default=str)
        entry['detail'] = detail[:16000]
        entry['truncated'] = len(detail) > 16000
        console = self.console()
        good = entry['state'] == 'success'
        timing = (f'执行 {entry["exec_seconds"]:.2f}s（含菜单等待 {entry["wait_seconds"]:.2f}s）'
                  if entry['wait_seconds'] >= 0.01 else f'{entry["seconds"]:.2f}s')
        console.print(Text(f'{"✓" if good else "✗"} {"完成" if good else "失败"} #{entry["id"]} '
                           f'{entry["tool"]} · {timing}', style=self.palette['success' if good else 'error']))
        result = output.get('result')
        if good and isinstance(result, dict):
            keys = ('change_id', 'path', 'validation', 'added_lines', 'removed_lines', 'changed', 'connection_verified',
                    'prepared', 'ready', 'preview_only', 'plan_saved', 'status', 'phase', 'allowed',
                    'choice', 'job_id', 'remote_directory', 'defaults', 'scans', 'resources', 'selection')
            brief = {k: result[k] for k in keys if k in result}
            if not brief:
                brief = {'返回字段': list(result)[:12]}
        elif good:
            brief = result
        else:
            brief = {k: output[k] for k in ('error_code', 'error', 'next_step') if k in output}
        brief = display_safe(brief)
        if isinstance(brief, dict):
            lines = [f'{key}: ' + (value if isinstance(value, str) else
                     json.dumps(value, ensure_ascii=False, default=str)) for key, value in brief.items()]
            lines = [line for item in lines for line in item.splitlines()]
        else:
            lines = str(brief).splitlines()
        for line in lines[:5]:
            console.print(Text('  │ ' + line[:220], style='dim'))
        if len(lines) > 5:
            console.print(Text(f'  │ … 摘要另有 {len(lines) - 5} 行', style='dim'))
        console.print(Text(f'  └ /trace {entry["id"]} 查看详情 · Ctrl+T 查看最近过程', style='dim'))
        if good and isinstance(result, dict) and result.get('change_id') and 'diff' in result:
            from rich.syntax import Syntax
            console.print(Syntax(display_safe(result['diff'])[:12000], 'diff', word_wrap=True))
            console.print(Text(f'  /changes {result["change_id"]} 查看记录；/undo {result["change_id"]} 恢复', style='dim'))

    def show_trace(self, argument=''):
        from rich.text import Text
        console = self.console()
        if not self.trace:
            console.print('本次启动尚无工具执行记录。')
            return
        argument = str(argument).strip()
        if argument:
            if not argument.isascii() or not argument.isdigit():
                console.print('用法：/trace 或 /trace 编号')
                return
            entries = [e for e in self.trace if e['id'] == int(argument)]
            if not entries:
                console.print('该记录不在本次启动保留的最近 100 条中。')
                return
        else:
            entries = self.trace[-20:]
        for entry in entries:
            console.print(Text(f'#{entry["id"]} {entry["tool"]} · {entry["state"]}', style='bold'))
            if argument:
                console.print(Text(json.dumps(entry['arguments'], ensure_ascii=False, indent=2, default=str)))
                console.print(Text(entry.get('detail', '正在执行，尚无结果。')))
                if entry.get('truncated'):
                    console.print('显示已截断；完整工具记录见本地审计日志。')
        if not argument:
            console.print('输入 /trace 编号查看参数和结果。记录来自真实工具调用，不含模型内部推理。')

    def read_menu(self, message):
        """Keep menus on the same input backend as the conversation."""
        started = time.monotonic()
        try:
            if self._prompt_session is not None and not self._basic_input:
                from prompt_toolkit.key_binding import KeyBindings
                session = self._prompt_session
                # prompt() options persist on PromptSession; None arguments mean
                # "keep the old value", not "disable". Scope all menu overrides,
                # including on Ctrl-C/EOF, so the next chat prompt is really chat.
                previous = {name: getattr(session, name) for name in
                            ('message', 'completer', 'bottom_toolbar', 'key_bindings')}
                try:
                    session.message = message
                    session.completer = None
                    session.bottom_toolbar = ''
                    session.key_bindings = KeyBindings()
                    return session.prompt(default='')
                finally:
                    for name, value in previous.items():
                        setattr(session, name, value)
            return input(message)
        finally:
            self._menu_wait += max(0.0, time.monotonic() - started)

    def tool(self, name, success):
        self.write(f'[工具] {name}: {"成功" if success else "失败"}',
                   'success' if success else 'error', sys.stderr)
