"""Deployment freshness: is the cluster running the engine this checkout would deploy?

Checked before every remote prepare/preview. A stale or missing deployment is
shown to the operator with an offer to deploy; tasks already prepared stay
bound to their own release either way.
"""
from vasp_agent.ui.menu_text import options, table


def short(version):
    return (version or '未部署')[:12]


def check(box, server):
    from vasp_agent.remote.remote_compute import RemoteCompute
    return RemoteCompute(box.workspace, server).freshness()


def ensure_current(box, server):
    """Return the freshness record, after deploying if the operator chose to.

    Raises ToolError when the operator cancels, or when nothing is deployed and
    no operator is present to approve a deploy.
    """
    import vasp_agent.workflow.vasp_band_agent as core
    output = getattr(box, 'plan_output', print)
    try:
        state = check(box, server)
    except core.ToolError:
        raise
    except Exception as exc:
        # The prepare call that follows reports a broken profile or connection
        # itself; this check must not mask that error with its own.
        output(f'未能核对 {server} 的引擎部署版本（{type(exc).__name__}）；继续。')
        return None
    if state['current']:
        return state
    accepted = getattr(box, 'accepted_releases', None)
    if accepted is None:
        accepted = box.accepted_releases = {}
    if state['deployed'] and accepted.get(server) == (state['local_version'], state['deployed_version']):
        return state
    output(f'\n{server} 上的计算引擎不是本机当前版本：')
    output(table([['本机版本', short(state['local_version'])], ['已部署版本', short(state['deployed_version'])]]))
    output('  · 已准备的任务仍绑定各自准备时的 release，重新部署不影响它们；新任务使用部署时的版本。')
    reader = getattr(box, 'plan_reader', None)
    if reader is None:
        if not state['deployed']:
            raise core.ToolError('ENGINE_NOT_DEPLOYED', f'{server} 尚未部署计算引擎；请先 remote_deploy（需操作者确认）。')
        output('  · 非交互模式：沿用已部署版本继续。')
        return state
    items = [('1', '部署本机版本后继续')]
    if state['deployed']:
        items.append(('2', '沿用已部署版本继续（本次会话对该版本不再提示）'))
    items.append(('q', '取消本次准备'))
    output(options(items))
    answer = reader('选择：').strip()
    if answer == '1':
        result = box.remote_deploy(server)
        output(f'已部署 {result.get("release")}。')
        return {**state, 'deployed': True, 'deployed_version': state['local_version'], 'current': True,
                'release': result.get('release')}
    if answer == '2' and state['deployed']:
        accepted[server] = (state['local_version'], state['deployed_version'])
        return state
    raise core.ToolError('ENGINE_UPDATE_DECLINED', '操作者取消：服务器引擎版本与本机不同，未准备任务。')


def server_versions(box, rows):
    """/servers: add deployed vs local engine version to each server with a compute profile."""
    from vasp_agent.chat.extra_commands import configured_servers
    configured = set(configured_servers(box))
    for row in rows:
        if row['name'] not in configured:
            row['engine'] = {'status': '无计算配置；可运行 /setup-server ' + row['name']}
            continue
        try:
            state = check(box, row['name'])
        except Exception as exc:
            row['engine'] = {'status': '无法查询', 'error': f'{type(exc).__name__}: {str(exc)[:200]}'}
            continue
        row['engine'] = {'local_version': state['local_version'], 'deployed_version': state['deployed_version'],
                         'status': '最新' if state['current'] else ('未部署' if not state['deployed'] else '需重新部署')}
    return rows
