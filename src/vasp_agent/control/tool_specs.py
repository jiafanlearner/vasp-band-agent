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
    PHASES,
    POTENTIAL_VARIANTS,
    RELAX_MODE,
    SCAN_ENCUTS,
    SCAN_MESHES,
    SCAN_PRESET,
    SERVER,
    STAGE_PARAMETERS,
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
_NON_EMPTY_STR = {'type': 'string', 'minLength': 1}
_GUIDE_ENUM = {'type': ['string', 'null'], 'enum': [*guides.ids(), None]}
_CONTRACT_ID = {'type': 'string', 'pattern': '^[A-Za-z0-9][A-Za-z0-9_-]{3,63}$'}
_PHASE_RESOURCE = {
    'type': 'object',
    'properties': {
        'submission_mode': {'type': 'string', 'enum': ['allocation', 'array']},
        'parallel_tests': {'type': 'integer', 'minimum': 1, 'maximum': 6},
        'cores_per_test': {'type': 'integer', 'minimum': 1, 'maximum': 65536},
        'nodes': {'type': 'integer', 'minimum': 1, 'maximum': 256},
        'partition': STR,
        'walltime': STR,
    },
    'required': ['parallel_tests', 'cores_per_test', 'nodes', 'partition', 'walltime'],
    'additionalProperties': False,
}
_RESOURCE_ENVELOPE = {
    'type': 'object',
    'properties': {
        'allowed_modes': {'type': 'array', 'minItems': 1, 'items': {'type': 'string', 'enum': ['allocation', 'array']}},
        'allowed_partitions': {'type': 'array', 'minItems': 1, 'items': STR},
        'max_parallel_tests': {'type': 'integer', 'minimum': 1, 'maximum': 6},
        'max_cores_per_test': {'type': 'integer', 'minimum': 1, 'maximum': 65536},
        'max_nodes': {'type': 'integer', 'minimum': 1, 'maximum': 256},
        'max_total_cores': {'type': 'integer', 'minimum': 1, 'maximum': 65536},
        'max_walltime': STR,
        'phase_defaults': {'type': 'object', 'minProperties': 1, 'additionalProperties': _PHASE_RESOURCE},
    },
    'required': [
        'allowed_modes',
        'allowed_partitions',
        'max_parallel_tests',
        'max_cores_per_test',
        'max_nodes',
        'max_total_cores',
        'max_walltime',
        'phase_defaults',
    ],
    'additionalProperties': False,
}
_PRECISION_BOUNDS = {
    'type': ['object', 'null'],
    'properties': {
        'max_encut': {'type': 'number', 'minimum': 200, 'maximum': 3000},
        'max_kmesh_axis': {'type': 'integer', 'minimum': 1, 'maximum': 128},
        'max_kmesh_points': {'type': 'integer', 'minimum': 1, 'maximum': 128 ** 3},
    },
    'additionalProperties': False,
}
_BUDGET = {
    'type': ['object', 'null'],
    'properties': {
        'max_submissions': {'type': 'integer', 'minimum': 1, 'maximum': 200},
        'max_resume_retries': {'type': 'integer', 'minimum': 0, 'maximum': 20},
        'max_repair_retries': {'type': 'integer', 'minimum': 0, 'maximum': 20},
        'max_scan_retries': {'type': 'integer', 'minimum': 0, 'maximum': 20},
        'max_consecutive_failures': {'type': 'integer', 'minimum': 1, 'maximum': 20},
    },
    'additionalProperties': False,
}
_PHYSICS_INPUT = {
    'type': ['object', 'null'],
    'properties': {
        'magnetism': STR,
        'charge': {'type': ['integer', 'boolean']},
        'functional': STR,
        'soc': _BOOL,
        'hubbard_u': _BOOL,
        'hybrid': _BOOL,
        'confirmed_by': STR,
        'assumptions_source': STR,
    },
    'additionalProperties': False,
}
_NULL_PHASE_RESOURCE = {**_PHASE_RESOURCE, 'type': ['object', 'null']}
_PHASE_REQUIRED = {'type': 'string', 'enum': PHASES}
_NULL_PHASES = {'type': ['array', 'null'], 'minItems': 1, 'items': _PHASE_REQUIRED}
_NULL_DETAILS = {'type': ['object', 'null']}
_EVENT_ID = {'type': 'string', 'pattern': '^[A-Za-z0-9][A-Za-z0-9_-]{3,63}$'}
_NULL_EVENT_IDS = {'type': ['array', 'null'], 'minItems': 1, 'maxItems': 200, 'items': _EVENT_ID}


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
        '只读查看任务自治合同摘要、账本已用额度、允许任务列表、熔断原因与待签草案列表。',
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
        'vasp_draft_contract',
        '调用扫描方案提案并保存未签发的任务自治合同草案，返回操作者本地真实终端签发命令（仅 maker 可用，不授权提交）。',
        ('maker',),
        _schema(
            {
                'server': SERVER,
                'job_name': JOB,
                'structure': STR,
                'encut': {'type': 'number', 'minimum': 200, 'maximum': 1500},
                'mesh': MESH,
                'nbands': NBANDS,
                'relax_mode': RELAX_MODE,
                'goal': _NON_EMPTY_STR,
                'resource_envelope': _RESOURCE_ENVELOPE,
                'parallel_tests': NULL_PARALLEL_TESTS,
                'cores_per_test': NULL_CORES_PER_TEST,
                'encuts': SCAN_ENCUTS,
                'meshes': SCAN_MESHES,
                'scan_preset': SCAN_PRESET,
                'band_path': NULL_STR,
                'optional_stages': OPTIONAL_STAGES,
                'stage_parameters': STAGE_PARAMETERS,
                'potential_variants': POTENTIAL_VARIANTS,
                'rationale': STR,
                'allowed_phases': _NULL_PHASES,
                'allow_forks': _BOOL,
                'expires_in_hours': {'type': 'integer', 'minimum': 1, 'maximum': 720},
                'precision_bounds': _PRECISION_BOUNDS,
                'budget': _BUDGET,
                'review_mode': {'type': 'string', 'enum': ['maker_checker', 'maker_direct']},
                'auto_advance_on_green': _BOOL,
                'physics_assertion': _PHYSICS_INPUT,
            },
            ('job_name', 'structure', 'encut', 'mesh', 'nbands', 'relax_mode', 'goal', 'resource_envelope'),
        ),
    ),
    _spec(
        'vasp_prepare_task',
        '核验已签发任务自治合同后执行无头准备（parameter_plans.prepare）、沿用合同物理确认（operator_slash_command）并写入首阶段合同包络内资源；不提交作业、不消耗提交额度（仅 maker 可用）。',
        ('maker',),
        _schema(
            {
                'contract_id': _CONTRACT_ID,
                'resources': _NULL_PHASE_RESOURCE,
            },
            ('contract_id',),
        ),
    ),
    _spec(
        'vasp_submit_stage',
        '合同门控有界阶段提交：核验已签合同、范围、预算、资源包络与阶段审阅门禁，按需应用资源（变动时要求先重审不提交），原子写账本 in_flight 后提交并登记 JobMonitor；断连仅只读对账，模糊即硬熔断 halted_uncertain 绝不双交（仅 maker 可用）。',
        ('maker',),
        _schema(
            {
                'contract_id': _CONTRACT_ID,
                'server': SERVER,
                'job_name': JOB,
                'phase': _PHASE_REQUIRED,
                'resources': _NULL_PHASE_RESOURCE,
            },
            ('contract_id', 'job_name', 'phase'),
        ),
    ),
    _spec(
        'vasp_watch_jobs',
        '读取持久监控事件队列与活跃作业状态（server=null 或省略查询全部服务器，server="local" 仅查本机）；maker 可传 poll=true 触发只读轮询（守护进程活跃时自动跳过重复 SSH）及 ack_ids 确认事件，checker 仅可只读查看。',
        ROLES,
        _schema(
            {
                'poll': _BOOL,
                'ack_ids': _NULL_EVENT_IDS,
                'include_acked': _BOOL,
                'server': {
                    **SERVER,
                    'description': '可选服务器过滤：null 或省略表示全部服务器；"local" 仅过滤本机作业；服务器名仅过滤该集群。',
                },
                'job_name': _NULL_JOB,
                'phase': NULL_PHASE,
                'limit': {'type': 'integer', 'minimum': 1, 'maximum': 200},
            },
        ),
    ),
    _spec(
        'vasp_circuit_break',
        '单向熔断：将已签合同账本不可逆置为 halted/halted_uncertain 并写入结构化诊断，任何代理均不可自解（maker/checker 均可调用）。',
        ROLES,
        _schema(
            {
                'contract_id': _CONTRACT_ID,
                'reason': _NON_EMPTY_STR,
                'code': _NON_EMPTY_STR,
                'uncertain': _BOOL,
                'details': _NULL_DETAILS,
            },
            ('contract_id', 'reason'),
        ),
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
