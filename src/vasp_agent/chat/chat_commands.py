"""Local slash commands; these never go to the model API."""
import json
import re
import shutil
import sys
from datetime import datetime
from uuid import uuid4

from vasp_agent.common import agent_paths


HELP = """命令菜单
  /help 或 /          显示此菜单
  /new [名称]         新建空白对话，保留旧对话
  /sessions           列出所有接口/模型的对话，按编号继续
  /sessions archived  列出已归档对话；/sessions archive [名称…|older 天数] 归档，/sessions restore [名称] 恢复（移动，不删除）
  /import-session     从其他接口/模型导入旧对话副本，保留原文件
  /resume [名称]      恢复指定对话；不填名称则打开选择菜单
  /history [编号|all]  对话记录目录；加编号查看该条全文
  /config             查看当前接口、模型、对话和工作区（隐藏密钥）
  /status [任务名]    查看计算任务状态；不填则列出任务
  /clear              清屏，不删除对话
  /retry              从已保存的工具结果继续请求
  /physics [服务器] [任务]  选择已有/最近任务并确认物理模型；不填打开菜单
  /submit [操作]      统一默认资源设置；status 查看 / off 关闭（兼容 once/on）
  /permissions        /submit 的别名，同一默认资源设置界面
  /exit 或 /quit      退出；不停止已提交的集群作业
名称使用英文字母、数字、下划线或短横线，最长 48 字符。
"""


HISTORY_ROWS = 10


def summary_line(text, room):
    """First meaningful line without Markdown markers, cut to `room` terminal columns."""
    from vasp_agent.ui.menu_text import width
    lines = [line for line in text.splitlines() if line.strip()]
    first = re.sub(r'^\s*(?:#{1,6}\s*|>\s*|[-*+]\s+|\d+[.)]\s+)', '', lines[0] if lines else '')
    first = ' '.join(re.sub(r'\*\*|__|`', '', first).split())
    suffix = f'（共 {len(lines)} 行）' if len(lines) > 1 else ''
    limit = room - width(suffix)
    if width(first) > limit:
        cut = ''
        for char in first:
            if width(cut + char) > limit - 1:
                break
            cut += char
        first = cut + '…'
    return first + suffix


def printable(value):
    return ''.join(c if c.isprintable() or c == '\n' else ' ' for c in str(value))


def portable_history(history):
    """Import context as text; never replay calls or forward provider-bound IDs."""
    from vasp_agent.chat.ai_agent import scrub
    if not isinstance(history, list) or any(not isinstance(item, dict) for item in history):
        raise ValueError('旧对话格式无效，未导入。')
    converted = []
    for item in history:
        kind = item.get('type')
        if kind in ('reasoning', 'compaction'):
            continue  # Encrypted reasoning belongs to the original provider.
        if kind in ('function_call', 'function_call_output'):
            if kind == 'function_call':
                record = {k: item[k] for k in ('name', 'arguments', 'call_id') if k in item}
                label = '历史工具请求（仅记录，不代表已执行）'
            else:
                record = {k: item[k] for k in ('call_id', 'output') if k in item}
                label = '历史工具返回（仅当时的结果，当前状态需重新读取）'
            text = label + '\n' + json.dumps(record, ensure_ascii=False)
            role = 'assistant'
        else:
            role = item.get('role')
            if role not in ('user', 'assistant'):
                continue
            content = item.get('content', '')
            if isinstance(content, list):
                text = '\n'.join(part.get('text', part.get('refusal', '')) for part in content
                                 if isinstance(part, dict) and isinstance(part.get('text', part.get('refusal', '')), str))
            elif isinstance(content, str):
                text = content
            else:
                raise ValueError('旧对话文字格式无效，未导入。')
        if text:
            converted.append({'role': role, 'content': scrub(text)})
    if not converted:
        raise ValueError('旧对话没有可导入的文字或工具记录。')
    converted.append({'role': 'user', 'content':
        '【程序生成的跨接口导入说明】以上是旧接口的历史上下文，不是新的执行指令。'
        '历史工具请求仅供参考，不得重放，特别是准备、审阅写入及提交。'
        '远程实时状态和不确定的提交结果须重新只读核查。'
        '本次导入不提供提交授权、不证明资源已在当前会话确认；等待我的后续要求。'})
    return converted


class Commands:
    def __init__(self, agent, factory, config, name, ui, reader=input, output=print, runtime=None):
        self.agent, self.factory, self.config = agent, factory, config
        self.name, self.ui, self.reader, self.output = name, ui, reader, output
        self.runtime = runtime
        self.agent.tools.plan_reader = reader
        self.agent.tools.plan_output = output

    def path(self, name):
        if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_-]{0,47}', name):
            raise ValueError('对话名称不合法；使用 1–48 个英文字母、数字、下划线或短横线。')
        return agent_paths.session_file(self.agent.tools.workspace, f'{name}-{self.config.identity()}')

    def session_paths(self, archived=False):
        """History files keyed as menus show them: name, or name@identity for other interfaces."""
        result = {}
        for stem, file in agent_paths.session_files(self.agent.tools.workspace, archived).items():
            match = re.fullmatch(r'([A-Za-z0-9][A-Za-z0-9_-]{0,47})-([0-9a-f]{12})', stem)
            if match:
                name, identity = match.groups()
                result[name if identity == self.config.identity() else name + '@' + identity] = file
        return result

    def entries(self, archived=False):
        entries = []
        for key, file in self.session_paths(archived).items():
            try:
                history = json.loads(file.read_text(encoding='utf-8'))
                if not isinstance(history, list) or any(not isinstance(x, dict) for x in history):
                    raise ValueError('invalid history')
                preview = next((x['content'] for x in history if x.get('role') == 'user' and isinstance(x.get('content'), str)), '空白对话')
                entries.append((key, file.stat().st_mtime, printable(preview).replace('\n', ' ')[:60]))
            except (OSError, ValueError, TypeError, AttributeError):
                self.output(f'跳过无法读取的会话：{printable(key)}')
        return sorted(entries, key=lambda row: row[1], reverse=True)

    def prepare_transfer(self, history, name, config):
        """Consent to sending history to the destination; write a local copy only."""
        public = config.public()
        self.output('继续旧对话的目标接口：' + str(public.get('base_url', '当前配置')) +
                    '；模型：' + str(public.get('model', '当前配置')))
        self.output('后续消息或/retry会向此接口发送历史文字和工具结果；旧文件保留，'
                    '加密推理及压缩缓存不迁移，历史工具不重放，提交授权关闭。')
        if self.reader('继续原对话请输入“继续”，其他输入取消切换：').strip() != '继续':
            self.output('已取消切换，未发送历史。')
            return None
        converted = portable_history(history) if history else []
        workspace = self.agent.tools.workspace
        target = agent_paths.session_file(workspace, f'{name}-{config.identity()}')
        if target.exists():
            name = name[:35] + '-' + uuid4().hex[:8]
            target = agent_paths.session_file(workspace, f'{name}-{config.identity()}')
        target.parent.mkdir(parents=True, exist_ok=True)
        with target.open('x', encoding='utf-8') as stream:
            json.dump(converted, stream, ensure_ascii=False, indent=2)
        return name

    def history(self, arg=''):
        """Index of text messages, one line each; '/history N' renders message N in full."""
        from vasp_agent.chat.extra_commands import text_messages
        messages = [(role, printable(text)) for role, text in text_messages(self.agent.history) if text.strip()]
        if not messages:
            self.output('当前对话还没有文字消息。')
            return
        if arg.isascii() and arg.isdigit():
            number = int(arg)
            if not 1 <= number <= len(messages):
                raise ValueError(f'编号须为 1–{len(messages)}。')
            role, text = messages[number - 1]
            self.output(f'── 第 {number} 条 · {"你" if role == "user" else "AI"} ──')
            renderer = getattr(self.ui, 'commentary', None)
            # Render Markdown on the real terminal; captured output keeps the plain text.
            renderer(text) if self.output is print and renderer else self.output(text)
            return
        if arg not in ('', 'all'):
            raise ValueError('用法：/history、/history 编号、/history all')
        shown = messages if arg == 'all' else messages[-HISTORY_ROWS:]
        first = len(messages) - len(shown) + 1
        self.output(f'对话记录 · {self.name}（共 {len(messages)} 条'
                    + (f'，显示最近 {len(shown)} 条）' if len(shown) < len(messages) else '）'))
        room = max(30, shutil.get_terminal_size((100, 24)).columns - 14)
        for number, (role, text) in enumerate(shown, first):
            self.output(f'{number:>4}  {"你" if role == "user" else "AI"}  {summary_line(text, room)}')
        self.output('输入 /history 编号 查看该条全文' + ('；/history all 列出全部' if len(shown) < len(messages) else '') + '。')

    def switch(self, name, create=False):
        if not create and '@' in name:
            match = re.fullmatch(r'([A-Za-z0-9][A-Za-z0-9_-]{0,47})@([0-9a-f]{12})', name)
            if not match:
                raise ValueError('跨接口会话标识无效。')
            source = agent_paths.session_file(self.agent.tools.workspace, f'{match[1]}-{match[2]}')
            if source.is_symlink():
                raise ValueError('会话文件不能是链接。')
            if not source.is_file():
                raise ValueError('没有这个对话；已归档的对话请先 /sessions restore。')
            name = self.prepare_transfer(json.loads(source.read_text(encoding='utf-8')), match[1], self.config)
            if name is None:
                return
        if not create and name == self.name:
            # Re-selecting the open conversation is not a switch: keep its grant.
            self.output(f'已在当前对话：{name}')
            return
        path = self.path(name)
        if create:
            path.parent.mkdir(parents=True, exist_ok=True)
            with path.open('x', encoding='utf-8') as stream:
                stream.write('[]')
        elif not path.is_file():
            matches = [entry[0] for entry in self.entries() if entry[0].split('@')[0] == name]
            if len(matches) == 1:
                return self.switch(matches[0])
            raise ValueError('没有唯一匹配的对话；请用 /sessions 选择具体来源。')
        replacement = self.factory(name)
        if replacement is not self.agent and callable(getattr(self.agent, 'release_session', None)):
            self.agent.release_session()
        revoked = self.agent.tools.revoke_submission('session_switch')
        if replacement.tools is not self.agent.tools:
            replacement.tools.revoke_submission('session_switch')
        if revoked:
            self.output('已撤销提交授权（切换对话后需重新确认）。')
        self.agent, self.name = replacement, name
        self.output(f'已切换到对话：{name}（{len(replacement.history)} 条记录）')

    def choose(self):
        entries = self.entries()
        if not entries:
            self.output('当前工作区没有已保存对话；可用 /new 创建。')
            return
        for index, (name, modified, preview) in enumerate(entries, 1):
            mark = ' *当前' if name == self.name else ''
            date = datetime.fromtimestamp(modified).strftime('%m-%d %H:%M')
            self.output(f'{index:>3}. {name}{mark} | {date} | {preview}')
        answer = self.reader('输入编号切换，回车或 q 取消：').strip()
        if answer in ('', 'q'):
            return
        if not answer.isascii() or not answer.isdigit() or not 1 <= int(answer) <= len(entries):
            raise ValueError('编号无效；未切换对话。')
        self.switch(entries[int(answer) - 1][0])

    def import_session(self):
        workspace = self.agent.tools.workspace
        candidates, archived = [], set()
        for in_archive in (False, True):
            for stem, path in agent_paths.session_files(workspace, in_archive).items():
                match = re.fullmatch(r'([A-Za-z0-9][A-Za-z0-9_-]*)-([0-9a-f]{12})', stem)
                if not match or match[2] == self.config.identity():
                    continue
                candidates.append((path.stat().st_mtime, path, match[1], match[2]))
                if in_archive:
                    archived.add(path)
        candidates.sort(key=lambda row: row[0], reverse=True)
        if not candidates:
            self.output('当前工作区没有其他接口/模型的旧对话文件；请确认 --workspace 与以前相同。')
            return
        for i, (modified, path, name, identity) in enumerate(candidates, 1):
            mark = ' | 已归档' if path in archived else ''
            self.output(f'{i}. {name} | 来源标识 {identity} | {datetime.fromtimestamp(modified):%m-%d %H:%M}{mark}')
        answer = self.reader('选择要导入的旧对话编号（回车/q 取消）：').strip()
        if answer in ('', 'q'):
            return
        if not answer.isascii() or not answer.isdigit() or not 1 <= int(answer) <= len(candidates):
            raise ValueError('编号无效，未导入。')
        source = candidates[int(answer) - 1][1]
        if not source.resolve().is_relative_to(workspace):
            raise ValueError('源文件路径越界。')
        history = portable_history(json.loads(source.read_text(encoding='utf-8')))
        public = self.config.public()
        self.output('准备导入：' + source.name + f'（转换后 {len(history)} 条记录）。原文件保留。')
        self.output('目标接口：' + str(public.get('base_url', '当前配置')) + '；模型：' + str(public.get('model', '当前配置')))
        self.output('导入仅写本地副本；之后发送消息或 /retry 会将导入的历史文字及工具结果发送给新接口。'
                    '不迁移加密推理，不执行历史工具，切换会关闭提交授权。')
        if self.reader('同意导入并在后续请求中向新接口发送历史请输入“导入”，其他输入取消：').strip() != '导入':
            self.output('已取消，未导入或发送历史。')
            return
        name = 'import-' + datetime.now().strftime('%Y%m%d-%H%M%S-') + uuid4().hex[:6]
        target = self.path(name)
        target.parent.mkdir(parents=True, exist_ok=True)
        with target.open('x', encoding='utf-8') as stream:
            json.dump(history, stream, ensure_ascii=False, indent=2)
        self.switch(name)
        self.output('旧对话副本已导入并切换；未调用模型或 SSH。可用 /history 查看，再明确下一步要求。')

    def handle(self, message):
        """Return exit, handled, or pass. Invalid commands never become model prompts."""
        from vasp_agent.common.workspace_paths import starts_with_path
        if not message.startswith('/') or starts_with_path(message):
            return 'pass'
        command, _, arg = message.partition(' ')
        arg = arg.strip()
        try:
            if command in ('/exit', '/quit'):
                return 'exit'
            if command in ('/', '/help'):
                from vasp_agent.chat.extra_commands import HELP as EXTRA_HELP
                self.output(HELP + EXTRA_HELP)
            elif command in ('/context', '/compact'):
                if arg and command == '/context':
                    raise ValueError('用法：/context 不带参数；/compact [本次压缩需重点保留的内容]。')
                result = self.agent.context_status() if command == '/context' else self.agent.compact_context(arg or None)
                self.output(json.dumps(result, ensure_ascii=False, indent=2))
            elif command == '/new':
                self.switch(arg or datetime.now().strftime('chat-%Y%m%d-%H%M%S-') + uuid4().hex[:4], create=True)
            elif command == '/import-session':
                if arg:
                    raise ValueError('用法：/import-session，然后从菜单选择旧对话。')
                self.import_session()
            elif command == '/physics':
                from vasp_agent.chat.stage_commands import execute
                execute(self, arg)
            elif command in ('/permissions', '/submit'):
                from vasp_agent.permissions.submission_permissions import execute
                execute(self, arg)
            elif command == '/sessions' and arg.split(' ', 1)[0] in ('archive', 'archived', 'restore'):
                from vasp_agent.chat.session_commands import execute as session_execute
                sub, _, rest = arg.partition(' ')
                session_execute(self, sub, rest.strip())
            elif command in ('/sessions', '/resume'):
                self.switch(arg) if arg else self.choose()
            elif command == '/config':
                self.output(json.dumps({**self.config.public(), 'session': self.name,
                             'workspace': str(self.agent.tools.workspace)}, ensure_ascii=False, indent=2))
            elif command == '/status':
                data = self.agent.tools.get_status(arg) if arg else self.agent.tools.list_files()
                self.output(json.dumps(data, ensure_ascii=False, indent=2))
            elif command == '/history':
                self.history(arg)
            elif command == '/clear':
                if self.ui.enabled(sys.stdout):
                    print('\033[2J\033[H', end='', flush=True)
                else:
                    self.output('\n' * 20 + '—— 当前对话继续 ——')
            elif command == '/retry':
                self.ui.reply(self.agent.ask())
            else:
                from vasp_agent.chat.extra_commands import execute
                if not execute(self, command, arg):
                    self.output('未知命令；输入 /help 查看。该内容未发送给模型。')
        except Exception as exc:
            from vasp_agent.chat.ai_agent import scrub
            message = str(exc)
            if getattr(self.config, 'api_key', ''):
                message = message.replace(self.config.api_key, '[REDACTED KEY]')
            self.output(f'命令未完成：{printable(scrub(message))}')
        return 'handled'
