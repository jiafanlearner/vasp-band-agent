"""Role-gated JSON Schemas and tool catalog definitions for the Layer 4 control plane."""
from __future__ import annotations

from typing import Any

from vasp_agent import guides
from vasp_agent.chat.tool_schemas import (
    ANALYSIS_EXPORT_OUTPUTS,
    ANALYSIS_IMAGE_PROFILE,
    JOB,
    MESH,
    NBANDS,
    NULL_CORES_PER_TEST,
    NULL_PARALLEL_TESTS,
    NULL_PHASE,
    NULL_STR,
    OFFSET,
    OPTIONAL_STAGES,
    POTENTIAL_VARIANTS,
    RELAX_MODE,
    SERVER,
    STR,
)

ROLES = ('maker', 'checker')
COMPACT_ALIAS = {
    'vasp_get_stage_review': 'get_stage_review',
    'vasp_task_status': 'remote_status',
    'vasp_diagnose_submission': 'get_submission_diagnostic',
    'vasp_fetch_results': 'analyze_band_results',
}

_NULL_JOB = {'type': ['string', 'null'], 'pattern': JOB['pattern']}
_NULL_JOB_ID = {'type': ['string', 'null'], 'pattern': '^[0-9]+$'}
_NULL_OP_ID = {'type': ['string', 'null'], 'pattern': '^[a-f0-9]{32}$'}
_NULL_FILES = {'type': ['array', 'null'], 'minItems': 1, 'maxItems': 10, 'items': STR}
_NULL_INPUTS = {
    'type': ['array', 'null'],
    'maxItems': 10,
    'items': {
        'type': 'object',
        'properties': {'path': STR, 'expected_sha256': STR},
        'required': ['path', 'expected_sha256'],
        'additionalProperties': False,
    },
}
_NULL_TIMEOUT = {'type': ['integer', 'null'], 'minimum': 1, 'maximum': 60}
_NULL_NUMBER = {'type': ['number', 'null']}
_BOOL = {'type': 'boolean'}
_GUIDE_ENUM = {'type': ['string', 'null'], 'enum': [*guides.ids(), None]}


def _schema(properties: dict[str, Any], required: tuple[str, ...] = ()) -> dict[str, Any]:
    return {
        'type': 'object',
        'properties': properties,
        'required': list(required),
        'additionalProperties': False,
    }


def _spec(name: str, description: str, roles: tuple[str, ...], input_schema: dict[str, Any]) -> dict[str, Any]:
    return {'name': name, 'description': description, 'roles': roles, 'inputSchema': input_schema}


TOOL_SPECS: tuple[dict[str, Any], ...] = (
    _spec(
        'vasp_workspace_info',
        '只读查看工作区文件列表、任务目录、已登记服务器及活动合同摘要（不泄露宿主机绝对路径）。',
        ROLES,
        _schema({}),
    ),
    _spec(
        'vasp_inspect_structure',
        '只读检查结构文件与可选 PBE 赝势库信息；返回带 content_is_untrusted=true 标记的不可信数据。',
        ROLES,
        _schema({'path': STR, 'include_potentials': _BOOL}, ('path',)),
    ),
    _spec(
        'vasp_preview_defaults',
        '只读计算默认收敛扫描参数、赝势下限、可选阶段参数规范与按需指南指针；不写入草案或任务。',
        ROLES,
        _schema(
            {
                'server': SERVER,
                'job_name': JOB,
                'structure': STR,
                'encut': {'type': 'number', 'minimum': 200, 'maximum': 1500},
                'mesh': MESH,
                'nbands': NBANDS,
                'relax_mode': RELAX_MODE,
                'parallel_tests': NULL_PARALLEL_TESTS,
                'cores_per_test': NULL_CORES_PER_TEST,
                'potential_variants': POTENTIAL_VARIANTS,
                'optional_stages': OPTIONAL_STAGES,
            },
            ('job_name', 'structure', 'encut', 'mesh', 'nbands', 'relax_mode'),
        ),
    ),
    _spec(
        'vasp_task_status',
        '只读查询本地或远端任务状态（manifest/checkpoint/提交回执/调度状态），可选附带收敛核验结果。',
        ROLES,
        _schema({'server': SERVER, 'job_name': JOB, 'include_convergence': _BOOL}, ('job_name',)),
    ),
    _spec(
        'vasp_read_evidence',
        '只读分页读取 .agent/evidence/ 历史原文（kind=evidence）、阶段指南（kind=guide）或任务白名单日志尾部（kind=log）。',
        ROLES,
        _schema(
            {
                'kind': {'type': 'string', 'enum': ['evidence', 'guide', 'log']},
                'sha256': NULL_STR,
                'offset': OFFSET,
                'guide': _GUIDE_ENUM,
                'page': {'type': 'integer', 'minimum': 1, 'maximum': 50},
                'job_name': _NULL_JOB,
                'file': NULL_STR,
            },
            ('kind',),
        ),
    ),
    _spec(
        'vasp_diagnose_submission',
        '只读诊断远端提交状态、Slurm 记账/分区容量冲突、候选布局、可选恢复回执及资源上下文；绝不修改或重交。',
        ROLES,
        _schema(
            {
                'server': JOB,
                'job_name': JOB,
                'phase': NULL_PHASE,
                'job_id': _NULL_JOB_ID,
                'start_time': NULL_STR,
                'end_time': NULL_STR,
                'operation_id': _NULL_OP_ID,
                'include_resources': _BOOL,
            },
            ('server', 'job_name'),
        ),
    ),
    _spec(
        'vasp_contract_status',
        '只读查看任务自治合同状态、账本额度与熔断原因（P0 未启用自动提交时返回空状态）。',
        ROLES,
        _schema({'contract_id': NULL_STR}),
    ),
    _spec(
        'vasp_get_stage_review',
        '只读读取当前待放行阶段、物理模型、真实证据摘要、哈希快照与按需阶段指南指针。',
        ROLES,
        _schema({'server': SERVER, 'job_name': JOB}, ('job_name',)),
    ),
    _spec(
        'vasp_fetch_results',
        '下载并哈希校验远端计算结果到 downloads/，可选执行确定性只读能带分析或本地重绘（仅 maker 角色可用）。',
        ('maker',),
        _schema(
            {
                'server': JOB,
                'job_name': JOB,
                'analyze_bands': _BOOL,
                'table_offset': OFFSET,
                'table_limit': {'type': 'integer', 'minimum': 0, 'maximum': 200},
                'replot': _BOOL,
                'y_min': _NULL_NUMBER,
                'y_max': _NULL_NUMBER,
            },
            ('server', 'job_name'),
        ),
    ),
    _spec(
        'vasp_sandbox_analysis',
        '仅限 Docker 隔离沙箱的后处理分析工具（runtime/stage/validate/run/get），绝不降级宿主机执行（仅 maker 角色可用）。',
        ('maker',),
        _schema(
            {
                'action': {'type': 'string', 'enum': ['runtime', 'stage', 'validate', 'run', 'get']},
                'server': _NULL_JOB,
                'job_name': _NULL_JOB,
                'files': _NULL_FILES,
                'script': NULL_STR,
                'expected_sha256': NULL_STR,
                'inputs': _NULL_INPUTS,
                'timeout_seconds': _NULL_TIMEOUT,
                'image_profile': ANALYSIS_IMAGE_PROFILE,
                'export_outputs': ANALYSIS_EXPORT_OUTPUTS,
                'run_id': NULL_STR,
            },
            ('action',),
        ),
    ),
)

SPEC_BY_NAME: dict[str, dict[str, Any]] = {item['name']: item for item in TOOL_SPECS}


def tool_definitions(role: str = 'maker') -> list[dict[str, Any]]:
    """Return MCP/CLI tool definitions exposed to `role`."""
    if role not in ROLES:
        raise ValueError(f'无效 role={role!r}，必须为 {" / ".join(ROLES)}。')
    return [
        {'name': item['name'], 'description': item['description'], 'inputSchema': item['inputSchema']}
        for item in TOOL_SPECS
        if role in item['roles']
    ]
