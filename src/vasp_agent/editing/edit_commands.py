"""Local maintenance commands; remain usable when the model API is unavailable."""
import json

from vasp_agent.remote.remote_servers import Servers
from vasp_agent.editing.workspace_editor import WorkspaceEditor, safe_value


def execute(c, command, argument):
    from vasp_agent.chat.extra_commands import choose
    box = c.agent.tools
    editor = WorkspaceEditor(box.workspace)

    def show(value):
        c.output(json.dumps(safe_value(value), ensure_ascii=False, indent=2))

    if command == '/changes':
        fields = argument.split()
        if not fields:
            show(editor.changes())
        elif len(fields) <= 2:
            show(editor.get(fields[0], int(fields[1]) if len(fields) == 2 else 0))
        else:
            raise ValueError('用法：/changes [修改ID [字符偏移]]')
    elif command == '/undo':
        if not argument or len(argument.split()) != 1:
            raise ValueError('用法：/undo 修改ID')
        show(box.call('revert_workspace_edit', {'change_id': argument}))
    elif command == '/server-key':
        servers = Servers(box.workspace)
        if argument.startswith('--undo '):
            result = servers.undo_identity(argument[len('--undo '):].strip())
            box.log({'command': '/server-key --undo', 'result': result})
            show(result)
            return
        if argument:
            fields = argument.split(maxsplit=1)
            if len(fields) != 2:
                raise ValueError('用法：/server-key 服务器名 "新密钥路径"')
            server, path = fields
        else:
            entries = servers.list()
            if not entries:
                c.output('尚无服务器配置，请先导入 SSH 登录脚本。')
                return
            server = choose(c, [x['name'] for x in entries], '选择更新密钥的服务器')
            if not server:
                return
            path = c.reader('新私钥文件路径（回车/q 取消；不输入私钥正文）：').strip()
            if path in ('', 'q'):
                return
        show(box.call('update_server_identity', {'server': server, 'identity_file': path,
                                                'expected_config_sha256': servers.config_hash(server)}))

