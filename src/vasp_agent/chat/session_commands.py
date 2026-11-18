"""/sessions archive | archived | restore: move conversations, never delete them.

A conversation is its history file plus sidecars (compaction cache, parts,
errors, cache metrics); they always move together, into .agent/sessions/archive/
and back. Nothing is overwritten: a name already taken at the destination gets a
suffix (archive) or the move is refused (restore). A conversation open in this
window is archived with /archive chat; one open in another agent window is
skipped (its session lock is held).
"""
import time
from datetime import datetime
from uuid import uuid4

from vasp_agent.common import agent_paths

USAGE = ('用法：/sessions 列出当前对话；/sessions archived 列出已归档；'
         '/sessions archive [名称…|older 天数] 归档；/sessions restore [名称] 恢复。')


def show(c, archived):
    rows = c.entries(archived=archived)
    if not rows:
        c.output('没有已归档的对话。' if archived else '当前工作区没有已保存对话；可用 /new 创建。')
        return rows
    for index, (key, modified, preview) in enumerate(rows, 1):
        mark = ' *当前' if key == c.name and not archived else ''
        c.output(f'{index:>3}. {key}{mark} | {datetime.fromtimestamp(modified):%m-%d %H:%M} | {preview}')
    return rows


def pick(c, rows, prompt):
    answer = c.reader(prompt + '（编号，回车或 q 取消）：').strip()
    if answer in ('', 'q'):
        return None
    if not answer.isascii() or not answer.isdigit() or not 1 <= int(answer) <= len(rows):
        raise ValueError('编号无效；未移动任何对话。')
    return rows[int(answer) - 1][0]


def archive_file(c, path, stem):
    """Move one conversation into the archive; a taken name gets a suffix. Returns the archived stem."""
    workspace = c.agent.tools.workspace
    taken = set(agent_paths.session_files(workspace, archived=True))
    target = stem
    for _ in range(20):
        if target not in taken:
            try:
                agent_paths.move_session(workspace, path, archived=True, stem=target)
                return target
            except FileExistsError:
                taken.add(target)
        name, _, identity = stem.rpartition('-')
        target = f'{name[:35]}-{uuid4().hex[:8]}-{identity}' if name else stem + '-' + uuid4().hex[:8]
    raise ValueError('无法为归档对话选择不冲突的名称；未移动。')


def archive(c, arg):
    workspace = c.agent.tools.workspace
    paths = c.session_paths()
    if not arg:
        rows = [row for row in c.entries() if row[0] != c.name]
        if not rows:
            c.output('没有可归档的其他对话；当前对话请用 /archive chat。')
            return
        for index, (key, modified, preview) in enumerate(rows, 1):
            c.output(f'{index:>3}. {key} | {datetime.fromtimestamp(modified):%m-%d %H:%M} | {preview}')
        chosen = pick(c, rows, '选择要归档的对话')
        keys = [chosen] if chosen else []
    elif arg.split()[0] == 'older':
        words = arg.split()
        if len(words) != 2 or not words[1].isascii() or not words[1].isdigit() or int(words[1]) < 1:
            raise ValueError('用法：/sessions archive older 天数（正整数）')
        cutoff = time.time() - int(words[1]) * 86400
        keys = [key for key, modified, _ in c.entries() if modified < cutoff and key != c.name]
        if not keys:
            c.output(f'没有超过 {words[1]} 天未修改的对话。')
            return
        c.output(f'将归档 {len(keys)} 个超过 {words[1]} 天未修改的对话（连同其上下文缓存文件，移动不删除）：')
        for key in keys:
            c.output('  ' + key)
        if c.reader('确认归档请输入“归档”，其他输入取消：').strip() != '归档':
            c.output('已取消，未移动任何对话。')
            return
    else:
        keys = arg.split()
    for key in keys:
        if key == c.name:
            c.output(f'{key}：当前对话请用 /archive chat 归档（会切换到新对话）。')
            continue
        path = paths.get(key)
        if path is None:
            c.output(f'{key}：没有这个对话，跳过。')
            continue
        stem = agent_paths.session_stem(path)
        if agent_paths.session_in_use(workspace, stem):
            c.output(f'{key}：正在另一个 Agent 窗口中打开，跳过。')
            continue
        archived = archive_file(c, path, stem)
        c.output(f'已归档：{key}' + (f'（归档名 {archived}）' if archived != stem else ''))


def restore(c, arg, switch=False):
    """Move an archived conversation back; refuses when the active name is taken."""
    workspace = c.agent.tools.workspace
    archived = c.session_paths(archived=True)
    if switch:  # /restore chat: conversations of the current interface only
        archived = {key: path for key, path in archived.items() if '@' not in key}
    key = arg
    if not key:
        rows = [row for row in c.entries(archived=True) if row[0] in archived]
        if not rows:
            c.output('没有已归档的对话。')
            return None
        for index, (name, modified, preview) in enumerate(rows, 1):
            c.output(f'{index:>3}. {name} | {datetime.fromtimestamp(modified):%m-%d %H:%M} | {preview}')
        key = pick(c, rows, '选择要恢复的归档对话')
        if not key:
            return None
    path = archived.get(key)
    if path is None:
        raise ValueError(f'没有名为 {key} 的归档对话。')
    stem = agent_paths.session_stem(path)
    name = key.partition('@')[0]
    if agent_paths.session_file(workspace, stem).exists():
        raise ValueError(f'当前已有同名对话 {name}，未覆盖。请先 /rename 或 /archive 该对话后再恢复。')
    agent_paths.move_session(workspace, path, archived=False)
    if switch:
        c.switch(name)
    else:
        c.output(f'已恢复：{key}。用 /resume {key} 继续该对话。')
    return key


def archive_current(c):
    """/archive chat: save, move the open conversation into the archive, start a new one."""
    from vasp_agent.workflow import vasp_band_agent as core
    core.save(c.agent.file, c.agent.history)
    archive_file(c, c.agent.file, agent_paths.session_stem(c.agent.file))
    c.switch('chat-' + datetime.now().strftime('%Y%m%d-%H%M%S-') + uuid4().hex[:4], create=True)


def execute(c, sub, arg):
    if sub == 'archived':
        if arg:
            raise ValueError(USAGE)
        show(c, archived=True)
    elif sub == 'archive':
        archive(c, arg)
    elif sub == 'restore':
        restore(c, arg)
    else:
        raise ValueError(USAGE)
