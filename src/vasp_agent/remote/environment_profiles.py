"""Software inventory records and operator-confirmed VASP environment profiles, per server.

Both live in servers/environments/, beside and never inside the connection files
(servers/<name>.json holds credentials and is only read, as is the compute
profile servers/<name>.compute.json):

  <server>.inventory.json   the latest read-only probe (worker_inventory); data, not a grant
  <server>.profiles.json    profiles the operator confirmed in the registration menu

A profile is an environment script on the cluster, the vasp_std it resolves to
(sha256), the script's sha256, capability tags and notes. Only the menu in
chat/software_commands.py writes one. The compute profile's vasp_env is the
implicit 'default' profile with no confirmed capabilities: while it is the
choice, a preparation sends nothing new and the task is exactly a legacy one.
"""
import json
import os
import re
from datetime import datetime, timezone
from pathlib import Path

from vasp_agent.remote.remote_servers import Servers
from vasp_agent.workflow import stage_definitions as stages

SCHEMA = 1
DEFAULT = 'default'
NAME = re.compile(r'[a-z0-9][a-z0-9._-]{0,31}')
# Path/flag markers of patched builds that inspect_environment refuses for
# full-cell relaxation; such a profile cannot serve relax_mode=cell.
CELL_CONFLICTS = ('patch:fixz', 'patch:ioptcell')
PROFILE_FIELDS = ('name', 'env_script', 'environment_sha256', 'executable_sha256', 'capabilities')


def folder(workspace):
    return Path(workspace).resolve() / 'servers' / 'environments'


def _path(workspace, server, kind):
    Servers(workspace).file(server)  # validates the server name
    return folder(workspace) / f'{server}.{kind}.json'


def _write(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + '.tmp')
    temporary.write_text(json.dumps(data, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    os.replace(temporary, path)


def _read(path):
    if not path.is_file():
        return None
    data = json.loads(path.read_text(encoding='utf-8'))
    if not isinstance(data, dict) or data.get('schema') != SCHEMA:
        raise ValueError(f'{path.name} 格式无效或版本未知；未使用。')
    return data


def load_inventory(workspace, server):
    return _read(_path(workspace, server, 'inventory'))


def save_inventory(workspace, server, result, roots):
    record = {'schema': SCHEMA, 'server': server, 'saved_at': datetime.now(timezone.utc).isoformat(timespec='seconds'),
              'requested_roots': list(roots), 'probe_version': result.get('probe_version'),
              'content_is_untrusted': True, 'result': result}
    _write(_path(workspace, server, 'inventory'), record)
    return record


def load_profiles(workspace, server):
    data = _read(_path(workspace, server, 'profiles'))
    return list(data['profiles']) if data else []


def save_profile(workspace, server, profile):
    """Add or replace one confirmed profile (same name); the menu is the only caller."""
    if not NAME.fullmatch(profile['name']) or profile['name'] == DEFAULT:
        raise ValueError('配置名称须为小写字母、数字、._-，最长 32 字符，且不能是 default。')
    rows = [row for row in load_profiles(workspace, server) if row['name'] != profile['name']]
    rows.append(profile)
    _write(_path(workspace, server, 'profiles'), {'schema': SCHEMA, 'server': server,
                                                  'profiles': sorted(rows, key=lambda row: row['name'])})


def compute_config(workspace, server):
    path = Servers(workspace).file(server).with_name(server + '.compute.json')
    return json.loads(path.read_text(encoding='utf-8'))


def candidates(workspace, server):
    """Registered profiles plus the implicit default (unless a registered one uses the same script)."""
    rows = load_profiles(workspace, server)
    env = compute_config(workspace, server).get('vasp_env')
    if env and all(row['env_script'] != env for row in rows):
        rows = [{'name': DEFAULT, 'env_script': env, 'capabilities': [], 'implicit': True,
                 'note': '服务器计算配置的 vasp_env；未经登记，没有已确认的能力标签'}] + rows
    return rows


def requirements(stage_entries=None):
    """Capability tags the enabled stages need (step A: the legacy four need none)."""
    return stages.required_capabilities(stage_entries if stage_entries is not None else stages.record())


def missing_message(required, rows):
    lacking = [tag for tag in required if not any(stages.satisfies(row['capabilities'], tag) for row in rows)]
    return f"需要 {'、'.join(lacking or required)}，已登记环境均不满足"


def choose(workspace, server, relax_mode='cell', required=None):
    """Profiles able to run the enabled stages: none, exactly one, or several for the operator."""
    required = tuple(requirements() if required is None else required)
    matches, excluded = [], []
    for row in candidates(workspace, server):
        lacking = stages.unmet(required, row['capabilities'])
        conflicts = [tag for tag in CELL_CONFLICTS if tag in row['capabilities']] if relax_mode == 'cell' else []
        if lacking:
            excluded.append({'name': row['name'], 'reason': '缺少 ' + '、'.join(lacking)})
        elif conflicts:
            excluded.append({'name': row['name'], 'reason': '、'.join(conflicts) + ' 与晶胞优化（relax_mode=cell）冲突'})
        else:
            matches.append(row)
    status = 'none' if not matches else 'one' if len(matches) == 1 else 'several'
    result = {'status': status, 'required': list(required), 'matches': matches, 'excluded': excluded}
    if status == 'none':
        result['message'] = missing_message(required, candidates(workspace, server)) if required else \
            '没有可用于 relax_mode=cell 的环境配置（' + '；'.join(f"{e['name']}：{e['reason']}" for e in excluded) + '）'
    return result


def registered(workspace, server):
    """True when the operator has confirmed at least one profile; otherwise selection is the legacy path."""
    return bool(load_profiles(workspace, server))


def by_name(workspace, server, name):
    for row in candidates(workspace, server):
        if row['name'] == name:
            return row
    raise ValueError(f'环境配置 {name} 已不存在；请重新提出方案或用 /software register 登记。')


def payload(row):
    """What a preparation sends: nothing for the implicit default, else the binding record."""
    if row.get('implicit'):
        return None
    return {key: row[key] for key in PROFILE_FIELDS}


def brief(row):
    """One profile as shown to the model and in menus."""
    return {'name': row['name'], 'env_script': row['env_script'], 'version': row.get('version'),
            'capabilities': row['capabilities'], 'executable_sha256': row.get('executable_sha256'),
            'implicit': bool(row.get('implicit')), 'notes': row.get('notes') or row.get('note')}


def unregistered_error(server, choice):
    from vasp_agent.workflow.vasp_band_agent import ToolError
    return ToolError('ENVIRONMENT_CAPABILITY_MISSING', choice['message'] + '；请操作者用 /software scan 与 /software register 登记满足能力的环境。未创建任务。')
