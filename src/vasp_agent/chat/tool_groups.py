"""On-demand tool group registry for lazy catalog disclosure in chat sessions.

High-frequency core tools stay in the model catalog on every turn; specialized
groups expand only after the model calls ``enable_tool_group``. Activation is
derived purely from ``Agent.live_history()`` (matching ``guides``), so session
switching, restarts, and ``/compact`` reclamation remain stateless and
consistent. Expanding a group changes only catalog visibility; execution and
submission permissions still come solely from operator menus and Toolbox gates.
"""
from __future__ import annotations

import json

CORE_TOOLS = (
    # Structure and workspace discovery
    'list_files',
    'list_servers',
    'inspect_structure',
    'inspect_potentials',
    # Scan plans and preparation
    'preview_scan_defaults',
    'propose_scan_plan',
    'list_scan_plans',
    'prepare_scan_plan',
    # Physics confirmation and stage review
    'request_physics_confirmation',
    'get_stage_review',
    'record_stage_review',
    'get_input_review',
    # Resources and submission authorization
    'get_submission_resources',
    'plan_submission_resources',
    'request_submission',
    'submit_stage',
    # Status and result delivery
    'remote_status',
    'remote_fetch',
    'get_status',
    # Progressive disclosure meta-tools
    'read_stage_guide',
    'read_context_evidence',
    'enable_tool_group',
)

GROUPS = {
    'convergence_fork': {
        'summary': '收敛复核、选择性重扫、收敛后派生生产任务、自定义或菜单选能带路径',
        'tools': (
            'inspect_convergence',
            'prepare_scan_retry',
            'prepare_production_from_convergence',
            'prepare_bands_path',
            'request_band_path_choice',
        ),
    },
    'recovery_cancel': {
        'summary': '取消/释放/超时续算作业、诊断不确定提交、对账恢复、仅改资源不提交或保存资源建议',
        'tools': (
            'request_job_cancellation',
            'get_submission_diagnostic',
            'request_submission_recovery',
            'get_submission_recovery',
            'apply_submission_resources',
            'recommend_submission_resources',
        ),
    },
    'server_ops': {
        'summary': '登记服务器、换 SSH 私钥、远程只读操作、部署引擎、检查环境与软件清单登记',
        'tools': (
            'register_server',
            'update_server_identity',
            'remote_operation',
            'remote_deploy',
            'check_remote_environment',
            'software_environments',
        ),
    },
    'workbench_edit': {
        'summary': '列出、读取、搜索、补丁修改、查看或撤销 workbench/ 草稿文件',
        'tools': (
            'list_workspace_files',
            'read_workspace_file',
            'search_workspace_text',
            'edit_workspace_file',
            'get_workspace_edit',
            'revert_workspace_edit',
        ),
    },
    'sandbox_analysis': {
        'summary': '检查 Docker 沙箱、暂存下载数据、校验并运行容器或本机分析脚本、调整本机批准设置',
        'tools': (
            'get_analysis_runtime',
            'stage_download_for_analysis',
            'validate_analysis_script',
            'run_analysis_script',
            'run_local_analysis_script',
            'get_analysis_run',
            'request_analysis_approval_settings',
        ),
    },
    'band_postproc': {
        'summary': '确定性分析已完成任务的 bands.csv（带隙/VBM/CBM/费米交叉/简并）及本地重绘能带图',
        'tools': (
            'analyze_band_results',
            'replot_bands',
        ),
    },
    'local_and_monitor': {
        'summary': '查看/建议后台作业监控间隔、本机任务日志与 Slurm 查询、重跑本机能带报告、旧版输入审阅',
        'tools': (
            'get_job_monitor',
            'recommend_job_check_interval',
            'read_log',
            'query_job',
            'analyze_results',
            'record_input_review',
        ),
    },
}

TOOL_TO_GROUP = {
    tool_name: group_id
    for group_id, spec in GROUPS.items()
    for tool_name in spec['tools']
}


def ids():
    """Valid lazy tool group ids in canonical declaration order."""
    return tuple(GROUPS)


def group_of(tool_name):
    """Return the lazy group id for tool_name, or None for core/legacy/bypass tools."""
    return TOOL_TO_GROUP.get(tool_name)


def active(items):
    """Tool group ids enabled in the given live history items, in first-activation order."""
    call_names = {
        item.get('call_id'): item.get('name')
        for item in (items or ())
        if isinstance(item, dict) and item.get('type') == 'function_call' and isinstance(item.get('call_id'), str)
    }
    seen = []
    for item in items or ():
        if not isinstance(item, dict) or item.get('type') != 'function_call_output':
            continue
        call_id = item.get('call_id')
        if call_id in call_names and call_names[call_id] != 'enable_tool_group':
            continue
        output = item.get('output')
        if not isinstance(output, str) or '"tool_groups_enabled"' not in output:
            continue
        try:
            payload = json.loads(output)
        except (ValueError, TypeError):
            continue
        if not isinstance(payload, dict) or payload.get('ok') is not True:
            continue
        result = payload.get('result')
        if not isinstance(result, dict) or result.get('authority') != 'none':
            continue
        for group_id in result.get('tool_groups_enabled') or ():
            if isinstance(group_id, str) and group_id in GROUPS and group_id not in seen:
                seen.append(group_id)
    return tuple(seen)


def enable(requested, currently_active=()):
    """Validate requested group ids and build the enable_tool_group tool result."""
    if not isinstance(requested, list) or not requested:
        raise ValueError('groups 必须是非空工具组名称列表。')
    ordered = []
    for group_id in requested:
        if not isinstance(group_id, str) or group_id not in GROUPS:
            raise ValueError(f'未知工具组 {group_id}；可用：' + '、'.join(GROUPS))
        if group_id not in ordered:
            ordered.append(group_id)
    active_list = [g for g in currently_active if g in GROUPS]
    newly, already = [], []
    for group_id in ordered:
        if group_id in active_list:
            already.append(group_id)
        else:
            newly.append(group_id)
            active_list.append(group_id)
    return {
        'tool_groups_enabled': ordered,
        'newly_enabled': newly,
        'already_enabled': already,
        'active_tool_groups': active_list,
        'tools': {group_id: list(GROUPS[group_id]['tools']) for group_id in ordered},
        'authority': 'none',
        'note': (
            '已展开所选工具组，下一轮可直接调用其工具；展开目录不授予任何执行或提交权限，'
            '仍须遵守审阅与操作者菜单授权规则。'
            if newly
            else '所选工具组已在当前上下文中展开，无需重复加载；展开目录不授予任何执行或提交权限。'
        ),
    }


def references(items):
    """Program-written reference to tool groups enabled in compacted items ('' when none)."""
    groups = active(items)
    if not groups:
        return ''
    lines = [f"- {group_id}：{GROUPS[group_id]['summary']}" for group_id in groups]
    return (
        '\n\n压缩前已展开的按需工具组（程序生成的引用；压缩后已回收以节省上下文，'
        '仍需使用时调用 enable_tool_group 重新展开，不代表任何执行或提交授权）：\n'
        + '\n'.join(lines)
    )
