"""Tool schemas, model-facing terse catalog view, and tool gating sets."""
from __future__ import annotations

from vasp_agent import guides
from vasp_agent.chat import tool_groups
from vasp_agent.workflow import parameter_specs, stage_definitions


def tool(name, description, properties):
    return dict(type='function', name=name, description=description, strict=True,
                parameters=dict(type='object', properties=properties,
                                required=list(properties), additionalProperties=False))


STR = {'type': 'string'}
NULL_STR = {'type': ['string', 'null']}
OPTIONAL_PATH = NULL_STR
SERVER = NULL_STR  # null is the local workspace
JOB = {'type': 'string', 'pattern': '^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$'}
# Every stage this program defines; a task's own enabled stages are checked by the engine.
PHASES = list(stage_definitions.known_names())
PHASE = {'type': 'string', 'enum': PHASES}
NULL_PHASE = {'type': ['string', 'null'], 'enum': [*PHASES, None]}
OFFSET = {'type': 'integer', 'minimum': 0}
RELAX_MODE = {'type': 'string', 'enum': ['cell', 'ions']}
CHECKLIST = {'type': 'array', 'minItems': 2, 'maxItems': 3, 'items': {'type': 'object',
    'properties': {'item': STR, 'evidence': STR, 'reason': STR, 'next_action': STR,
                   'judgment': {'type': 'string', 'enum': ['pass', 'pending', 'blocked']}, 'blocking': {'type': 'boolean'}},
    'required': ['item', 'evidence', 'reason', 'next_action', 'judgment', 'blocking'], 'additionalProperties': False}}
SCAN_ENCUTS = {'type': ['array', 'null'], 'items': {'type': 'number', 'minimum': 200, 'maximum': 3000}, 'minItems': 3, 'maxItems': 3}
SCAN_THRESHOLDS = {'type': ['object', 'null'], 'description': '未放宽项为 null。',
                   'properties': {'energy_eV_per_atom': {'type': ['number', 'null']},
                                  'force_eV_per_A': {'type': ['number', 'null']},
                                  'stress_kbar': {'type': ['number', 'null']}},
                   'required': ['energy_eV_per_atom', 'force_eV_per_A', 'stress_kbar'], 'additionalProperties': False}
SCAN_PRESET = {'type': ['string', 'null'], 'enum': ['standard', 'strict', None]}
MESH = {'type': 'array', 'items': {'type': 'integer', 'minimum': 1, 'maximum': 32}, 'minItems': 3, 'maxItems': 3}
SCAN_MESHES = {'type': ['array', 'null'], 'items': {'type': 'array', 'items': {'type': 'integer', 'minimum': 1, 'maximum': 128}, 'minItems': 3, 'maxItems': 3}, 'minItems': 3, 'maxItems': 3}
NBANDS = {'type': 'integer', 'minimum': 1, 'maximum': 4096}
PARALLEL_TESTS = {'type': 'integer', 'minimum': 1, 'maximum': 6}
NULL_PARALLEL_TESTS = {'type': ['integer', 'null'], 'minimum': 1, 'maximum': 6}
NULL_CORES_PER_TEST = {'type': ['integer', 'null'], 'minimum': 1, 'maximum': 256}
# Fields shared by preview_scan_defaults, propose_scan_plan and (without the variants) remote_prepare.
SCAN_INPUTS = {'server': SERVER, 'job_name': JOB, 'structure': STR,
    'encut': {'type': 'number', 'minimum': 200, 'maximum': 1500}, 'mesh': MESH, 'nbands': NBANDS,
    'relax_mode': RELAX_MODE, 'parallel_tests': NULL_PARALLEL_TESTS, 'cores_per_test': NULL_CORES_PER_TEST}
# Optional stages a new task can enable; each brings a parameter spec whose
# values the operator chooses in the plan menu (parameter_specs).
OPTIONAL_STAGES = {'type': ['array', 'null'], 'items': {'type': 'string', 'enum': list(parameter_specs.optional_stages())},
                   'maxItems': 4}
STAGE_PARAMETERS = {'type': ['array', 'null'], 'maxItems': 32, 'items': {'type': 'object',
    'properties': {'stage': STR, 'field': STR, 'value': {'type': ['number', 'string']}},
    'required': ['stage', 'field', 'value'], 'additionalProperties': False}}
POTENTIAL_VARIANTS = {'type': ['array', 'null'], 'items': {'type': 'object', 'properties': {'element': STR, 'variant': STR},
                      'required': ['element', 'variant'], 'additionalProperties': False}}
ANALYSIS_SCRIPT = {'script': STR, 'expected_sha256': STR, 'inputs': {'type': 'array', 'maxItems': 10, 'items': {
    'type': 'object', 'properties': {'path': STR, 'expected_sha256': STR},
    'required': ['path', 'expected_sha256'], 'additionalProperties': False}}}
ANALYSIS_TIMEOUT = {'type': 'integer', 'minimum': 1, 'maximum': 60}
RESOURCE_PROPERTIES = {'nodes': {'type': 'integer', 'minimum': 1, 'maximum': 256},
    'parallel_tests': PARALLEL_TESTS,
    'cores_per_test': {'type': 'integer', 'minimum': 1, 'maximum': 65536},
    'partition': STR, 'walltime': STR}
RESOURCE_SELECTION = {'anyOf': [
    {'type': 'object', 'properties': RESOURCE_PROPERTIES,
     'required': list(RESOURCE_PROPERTIES), 'additionalProperties': False},
    {'type': 'object', 'properties': {**RESOURCE_PROPERTIES,
        'submission_mode': {'type': 'string', 'enum': ['allocation', 'array']}},
     'required': [*RESOURCE_PROPERTIES, 'submission_mode'], 'additionalProperties': False}]}
NULL_RESOURCE_SELECTION = {'anyOf': [*RESOURCE_SELECTION['anyOf'], {'type': 'null'}]}

TOOL_GROUPS = {'type': 'array', 'items': {'type': 'string', 'enum': list(tool_groups.ids())},
               'minItems': 1, 'maxItems': 3}
TOOLS = [
    tool('enable_tool_group', '按需展开低频工具组的完整参数定义；只改变可见工具目录，不授予任何执行或提交权限。', {
        'groups': TOOL_GROUPS}),
    tool('get_job_monitor', '只读查看已保存的监控目标、间隔、最近状态和检查时间；不是实时查询。', {}),
    tool('recommend_job_check_interval', '提交前建议后台检查间隔（1–60分钟）；只是检查频率，不是耗时预测。', {
        'server': SERVER, 'job_name': JOB,
        'phase': NULL_PHASE,
        'minutes': {'type': 'integer', 'minimum': 1, 'maximum': 60}, 'reason': STR}),
    tool('request_analysis_approval_settings', '打开菜单让操作者调整或撤销本会话本机脚本批准；模型不能代选。', {}),
    tool('get_analysis_runtime', '只读检查本地 Docker 隔离环境和 Python 镜像；不安装、不拉取。', {}),
    tool('stage_download_for_analysis', '把 remote_fetch 已校验的文本结果复制到 workbench/data/服务器/任务名/，返回可作 inputs 的路径和哈希。', {'server': JOB, 'job_name': JOB, 'files': {'type': 'array', 'minItems': 1, 'maxItems': 10, 'items': STR}}),
    tool('analyze_band_results', '只读确定性分析已完成任务的 bands.csv（server 为 null/local 读 runs/，否则读已校验下载）：占据带、VBM/CBM、直接/间接带隙、费米面交叉、空带、Γ简并、路径分段；table_limit>0 分页返回逐k表。', {
        'server': SERVER, 'job_name': JOB,
        'table_offset': OFFSET, 'table_limit': {'type': 'integer', 'minimum': 0, 'maximum': 200}}),
    tool('replot_bands', '用已校验 bands.csv 本地重绘 bands.replot.png/.pdf，不改原图和收据。', {
        'server': SERVER, 'job_name': JOB,
        'y_min': {'type': ['number', 'null']}, 'y_max': {'type': ['number', 'null']}}),
    tool('validate_analysis_script', '静态校验 workbench 内脚本及显式输入哈希，返回容器输入映射；不执行。输入只能是选定草稿，不含凭据、POTCAR 或正式任务文件。', ANALYSIS_SCRIPT),
    tool('run_analysis_script', '在无网络、无远程工具、无 Agent 源码和提交权限的 Docker 容器运行草稿快照，只挂载选定输入；无容器不执行。', {**ANALYSIS_SCRIPT, 'timeout_seconds': ANALYSIS_TIMEOUT}),
    tool('run_local_analysis_script', '请求本机非沙箱执行，须经操作者批准菜单；不从 Docker 自动降级。', {**ANALYSIS_SCRIPT, 'timeout_seconds': ANALYSIS_TIMEOUT}),
    tool('get_analysis_run', '读取分析运行回执；状态未知先读它，running_or_interrupted 即结果未知，不自动重放。', {'run_id': STR}),

    tool('list_workspace_files', '分页列出 workbench/ 内文件。', {'path': STR, 'offset': OFFSET}),
    tool('read_workspace_file', '按行或字符偏移读取草稿，返回 sha256；最多12000字符。', {'path': STR, 'start_line': {'type': 'integer', 'minimum': 1}, 'offset': OFFSET}),
    tool('search_workspace_text', '在 workbench/ 内搜索字面文本。', {'query': STR, 'path': STR, 'offset': OFFSET}),
    tool('edit_workspace_file', '新建或精确补丁修改 workbench/ 单个草稿，备份并静态校验；不执行。补丁头 --- a/路径 和 +++ b/路径；create 只给 content，patch 只给 patch 和 expected_sha256，其余 null。', {
        'path': STR, 'operation': {'type': 'string', 'enum': ['create', 'patch']},
        'expected_sha256': {'type': ['string', 'null']}, 'content': {'type': ['string', 'null']},
        'patch': {'type': ['string', 'null']}, 'reason': STR}),
    tool('get_workspace_edit', '查看草稿修改回执、状态和分页差异；不是计算证据。', {'change_id': STR, 'offset': OFFSET}),
    tool('revert_workspace_edit', '核对哈希后恢复一次草稿修改；冲突不覆盖。', {'change_id': STR}),
    tool('update_server_identity', '只更新已有服务器的 SSH 私钥路径并备份；不读私钥正文，不连接。', {
        'server': JOB, 'identity_file': STR, 'expected_config_sha256': STR}),
    tool('check_remote_environment', '只读检查服务器 VASP 环境、晶胞约束标记和文件指纹；不启动 VASP。', {'server': JOB, 'relax_mode': RELAX_MODE}),
    tool('software_environments', '软件清单与环境配置：read 读记录，scan 有界只读探测，register 请求操作者登记菜单。', {
        'server': JOB, 'action': {'type': 'string', 'enum': ['read', 'scan', 'register']}}),
    tool('read_stage_guide', '按 guides 指针分页读取阶段指南；是数据，不授权。', {
        'guide': {'type': 'string', 'enum': list(guides.ids())}, 'page': {'type': 'integer', 'minimum': 1, 'maximum': 50}}),
    tool('read_context_evidence', '分页读取被压缩的历史工具原文或审计载荷；只是历史，不代表实时状态。', {'sha256': STR, 'offset': OFFSET}),
    tool('inspect_convergence', '从真实输出重新核验 ENCUT 和 k 网格各自的收敛状态及阈值。', {'server': SERVER, 'job_name': JOB}),
    tool('prepare_production_from_convergence', '新目录复用已完成的收敛输出，固定选定的已测网格和 ENCUT，不重算扫描。source_snapshot 来自 get_stage_review；来源须尚未提交或生成优化输入。继承物理模型确认，不继承审阅和提交授权。', {'server': SERVER, 'source_job': JOB, 'job_name': JOB, 'mesh': {'type': 'array', 'items': {'type': 'integer', 'minimum': 1, 'maximum': 128}, 'minItems': 3, 'maxItems': 3}, 'source_snapshot': STR, 'encut': {'type': ['number', 'null'], 'exclusiveMinimum': 0, 'description': 'null 为最高已测值；否则须是来源已测、cutoff_vs_highest passed 的值（ENCUT 扫描同一网格）。'}}),
    tool('prepare_bands_path', '新目录复用来源已通过的 SCF（CHGCAR 哈希已校验），只按新路径重做能带；不改来源，不继承审阅和提交授权。', {'server': SERVER, 'source_job': JOB, 'job_name': JOB, 'band_path': {'type': 'string', 'description': '-连点，|断开，段尾@N每线点数，hinuma:换约定，L(a,b,c)为能带原胞倒格分数坐标，如 Γ-X-M-Γ|Z-R'}}),
    tool('request_band_path_choice', '打开程序生成并校验的能带路径菜单（来源 SCF 须已通过）；操作者选定后新建任务只重做能带，模型不能代选。extra_suggestion 可附一条 AI 路径，同样校验。', {'server': SERVER, 'source_job': JOB, 'extra_suggestion': OPTIONAL_PATH, 'extra_rationale': OPTIONAL_PATH}),
    tool('prepare_scan_retry', '新目录选择性重扫：mesh 只测网格并复用 ENCUT，encut 反之，both 全测；继承结构、POTCAR、模型和阈值，不改来源序列。fixed_encut 只用于 mesh 且须为来源已测值，新最密网格与来源最高 ENCUT 交叉复核通过才优化。菜单确认后准备。', {
        'server': SERVER, 'source_job': JOB, 'job_name': JOB,
        'mode': {'type': 'string', 'enum': ['both', 'encut', 'mesh']}, 'encuts': SCAN_ENCUTS, 'meshes': SCAN_MESHES, 'fixed_encut': {'type': ['number', 'null'], 'minimum': 200, 'maximum': 3000},
        'scan_thresholds': SCAN_THRESHOLDS, 'scan_preset': SCAN_PRESET}),
    tool('plan_submission_resources', '自动规划尽快完成的阶段资源：按独立测试数最大化并发，保留每组 MPI 数，核查容量和授权范围；只保存建议，无性能实测，不保证最优。', {'server': JOB, 'job_name': JOB, 'phase': PHASE}),
    tool('apply_submission_resources', '仅用于改资源而不提交：应用操作者菜单确认的资源，不授予提交权。submission_mode 缺省 allocation。', {'server': JOB, 'job_name': JOB, 'phase': PHASE, 'resources': RESOURCE_SELECTION}),
    tool('get_submission_resources', '读取实际资源、配置签名和授权上限。', {'server': SERVER, 'job_name': JOB, 'phase': NULL_PHASE}),
    tool('recommend_submission_resources', '已有授权上限时保存 AI 资源建议和理由；不是操作者确认，提交菜单会展示。', {
        'server': SERVER, 'job_name': JOB, 'phase': NULL_PHASE,
        'signature': STR, 'parallel_tests': PARALLEL_TESTS,
        'cores_per_test': {'type': 'integer', 'minimum': 1, 'maximum': 65536}, 'nodes': {'type': ['integer', 'null'], 'minimum': 1, 'maximum': 256},
        'submission_mode': {'type': ['string', 'null'], 'enum': ['allocation', 'array', None]}, 'rationale': STR}),
    tool('preview_scan_defaults', '只读计算默认扫描参数和赝势下限（optional_stages 附参数规范）；不保存草案。', {
        'potential_variants': POTENTIAL_VARIANTS, **SCAN_INPUTS, 'optional_stages': OPTIONAL_STAGES}),
    tool('propose_scan_plan', '保存正式参数草案并与默认比较，有差异时由菜单询问用户；同名不同建议明确冲突，不覆盖用户选择。', {
        'potential_variants': POTENTIAL_VARIANTS, **SCAN_INPUTS,
        'encuts': SCAN_ENCUTS, 'meshes': SCAN_MESHES, 'scan_thresholds': SCAN_THRESHOLDS, 'scan_preset': SCAN_PRESET,
        'band_path': OPTIONAL_PATH, 'optional_stages': OPTIONAL_STAGES,
        'stage_parameters': STAGE_PARAMETERS,
        'origin': {'type': 'string', 'enum': ['ai_suggestion', 'user_explicit', 'tool_default']}, 'rationale': {'type': 'string', 'description': '菜单显示的理由：3–6 条短句，用“；”分隔，依次写改了什么、为什么、计算量变化、待验证项；不重复菜单已对比的数值。'}}),
    tool('list_scan_plans', '列出最近参数草案及选择状态。', {}),
    tool('prepare_scan_plan', '按已选方案生成新任务并核对实际文件；pending 禁止准备。', {'plan_id': STR}),
    tool('request_physics_confirmation', '当回合打开物理模型确认菜单，操作者选择或编辑研究目标（proposed_goal 只是建议）。', {'server': SERVER, 'job_name': JOB, 'proposed_goal': STR}),
    tool('get_stage_review', '读取当前待放行阶段、物理模型、真实结果摘要和证据快照。bands 阶段附 scf_occupation_audit（SCF 全 k 点最高带占据），electronic 检查以它为证据。', {'server': SERVER, 'job_name': JOB}),
    tool('record_stage_review', '记录结构化审阅，只放行当前阶段。', {
        'server': SERVER, 'job_name': JOB, 'snapshot': STR, 'phase': PHASE, 'checks': CHECKLIST}),
    tool('submit_stage', '提交已审阅的指定当前阶段，须有用户提交要求和该目标授权；重复阶段或结果不明确都不重交；phase=null 仅用于无阶段的旧版任务。', {
        'server': SERVER, 'job_name': JOB, 'phase': NULL_PHASE}),
    tool('request_submission', '打开统一提交菜单：操作者一次确认资源并授权指定任务当前阶段（优先展示已保存默认值；resources 预填用户所说资源，如32核）。之后仍须审阅和预检。', {
        'server': JOB, 'job_name': JOB, 'phase': PHASE,
        'resources': NULL_RESOURCE_SELECTION}),
    tool('request_job_cancellation', '打开取消菜单，操作者选择才执行。cancel_and_release：scancel 后等记账 CANCELLED 并释放该阶段回执（改资源重交用此项）；cancel：只取消；release：已取消时只释放；resume：作业超时/取消/节点故障/抢占结束后复用已验收阶段、归档中断输出并释放回执。释放或续算后该阶段授权失效。', {'server': JOB, 'job_name': JOB, 'phase': NULL_PHASE, 'action': {'type': 'string', 'enum': ['cancel_and_release', 'cancel', 'release', 'resume']}}),
    tool('get_submission_recovery', '只读查询恢复操作回执，用于 SSH 响应丢失后核对。', {'server': JOB, 'job_name': JOB, 'operation_id': {'type': 'string', 'pattern': '^[a-f0-9]{32}$'}}),
    tool('request_submission_recovery', '核查初始 convergence 失败作业及队列并打开操作者对账菜单；保留原回执，不改资源、不授权提交。', {'server': JOB, 'job_name': JOB, 'phase': {'type': 'string', 'enum': ['convergence']}, 'job_id': {'type': 'string', 'pattern': '^[0-9]+$'}}),
    tool('get_submission_diagnostic', '只读诊断不确定提交：回执、Slurm 历史与 SubmitLine、当前脚本与配置、分区容量、事件和日志，返回 diagnosis（配置冲突与候选布局）。job_id=null 先用回执编号，否则按 SSH 用户、任务名和时间窗查找（默认回执前后一天，无记录查7天；start_time/end_time 为 ISO，最多31天，无时区按服务器时间）。多个候选须列出，不自动选择；phase_verified 是旧兼容字段，以新字段的阶段证据为准。', {
        'server': JOB, 'job_name': JOB, 'phase': NULL_PHASE,
        'job_id': {'type': ['string', 'null'], 'pattern': '^[0-9]+$'},
        'start_time': {'type': ['string', 'null']}, 'end_time': {'type': ['string', 'null']}}),
    tool('get_input_review', '只读查看新旧任务实际输入、赝势元数据和参数方案，无需 /physics。', {'server': SERVER, 'job_name': JOB}),
    tool('record_input_review', '仅旧版任务：读取快照后记录 AI 审阅（目标适配、参数来源、待验证事项、阻断理由）；不是提交授权。', {
        'server': SERVER, 'job_name': JOB, 'snapshot': STR,
        'verdict': {'type': 'string', 'enum': ['pass', 'needs_validation', 'blocked']}, 'explanation': STR}),
    tool('remote_deploy', '上传计算程序并验证服务器 Python 依赖；不上传密钥或模型配置。', {'server': JOB}),
    tool('remote_prepare', '上传结构并用服务器赝势库准备完整计算；不提交。', {
        **SCAN_INPUTS, 'server': JOB}),
    tool('remote_submit', '仅旧版远程任务（新版用 submit_stage）：提交已准备任务；重复调用返回已登记状态，结果不明确不重交。', {'server': JOB, 'job_name': JOB}),
    tool('remote_status', '查询远程状态、队列和日志尾部；数组任务的 array_tasks 给出每项状态，全部终态后核验真实输出并汇总到 convergence-array.json。', {'server': JOB, 'job_name': JOB}),
    tool('remote_fetch', '下载报告和能带图到本地独立目录并校验哈希；不下载 POTCAR。', {'server': JOB, 'job_name': JOB}),
    tool('register_server', '从用户明确提供的本地 SSH 登录脚本导入连接配置；不执行脚本、不读私钥正文。', {'name': JOB, 'login_file': STR}),
    tool('list_servers', '列出已保存的服务器配置及哈希。', {}),
    tool('remote_operation', '用已保存配置执行远程只读操作；路径为远程绝对路径。', {
        'server': JOB, 'action': {'type': 'string', 'enum': ['info', 'list', 'read', 'jobs']},
        'path': {'type': ['string', 'null']}}),
    tool('list_files', '列出工作区顶层文件和 runs 中的任务。', {}),
    tool('import_structure_file', '导入用户当前消息明确给出的工作区外 POSCAR，校验后复制到 structures/，返回相对路径和来源哈希。', {'path': STR}),
    tool('inspect_structure', '读取 POSCAR/CIF，返回晶胞、成分和 kmesh_suggestion；不判断磁性。', {'path': STR}),
    tool('inspect_potentials', '检查结构所需的本地 PBE 赝势及变体映射。', {'structure': STR}),
    tool('prepare_calculation', '从原始结构准备完整 PBE 流程；不提交、不覆盖。空值用固定默认值。', {
        'structure': STR, 'job_name': JOB,
        'encut': {'type': ['number', 'null'], 'minimum': 200, 'maximum': 1500},
        'mesh': {**MESH, 'type': ['array', 'null']},
        'relax_mode': RELAX_MODE,
        'nbands': {**NBANDS, 'type': ['integer', 'null']},
        'rationale': STR}),
    tool('get_status', '获取任务输入清单、配置、工作流状态、报告摘要和提交记录。', {'job_name': JOB}),
    tool('read_log', '读取任务内白名单日志/结果文件尾部（最多12000字符）。', {'job_name': JOB, 'file': STR}),
    tool('analyze_results', '对已完成的完整计算重新校验并生成能带报告。', {'job_name': JOB}),
    tool('query_job', '用固定 squeue/sacct 查询该任务已登记的 Slurm 作业。', {'job_name': JOB}),
    tool('submit_job', '仅旧版本机任务（新版用 submit_stage）：按操作者预配置脚本提交一次 Slurm 作业。', {'job_name': JOB}),
]

# Bounds and patterns the model gets back from Toolbox.call as a validation
# error. Advertised on every field they cost ~650 tokens per request, the name
# pattern alone ~840; INSTRUCTIONS states that pattern once.
EXECUTION_ONLY_KEYWORDS = frozenset({'minimum', 'maximum', 'exclusiveMinimum', 'minItems', 'maxItems', 'pattern'})
# One object with a nullable submission_mode instead of the two anyOf branches.
# Toolbox.call drops submission_mode=null before validating (null_resource_mode),
# so null means "omitted", i.e. allocation, exactly as the full schema reads.
TERSE_RESOURCES = {'type': 'object', 'properties': {**RESOURCE_PROPERTIES,
                       'submission_mode': {'type': ['string', 'null'], 'enum': ['allocation', 'array', None]}},
                   'required': [*RESOURCE_PROPERTIES, 'submission_mode'], 'additionalProperties': False}
TERSE_FORMS = ((RESOURCE_SELECTION, TERSE_RESOURCES),
               (NULL_RESOURCE_SELECTION, {**TERSE_RESOURCES, 'type': ['object', 'null']}))


def advertised(value, _properties=False):
    """Model-facing copy of a schema: execution-only keywords dropped.

    What is kept is what strict function calling needs (types, enums,
    required, additionalProperties). Toolbox.call still validates every call
    against the full TOOLS schema, so the dropped keywords are enforced there;
    tests/test_prompt_budget.py checks that both views judge arguments alike.
    """
    if isinstance(value, dict):
        if not _properties:
            value = next((terse for full, terse in TERSE_FORMS if value == full), value)
        # Keys directly under 'properties' are argument names, not keywords.
        return {key: advertised(item, key == 'properties' and not _properties) for key, item in value.items()
                if _properties or key not in EXECUTION_ONLY_KEYWORDS}
    if isinstance(value, list):
        return [advertised(item) for item in value]
    return value


def null_resource_mode(arguments):
    """submission_mode=null from the terse view means omitted (allocation)."""
    resources = arguments.get('resources') if isinstance(arguments, dict) else None
    if isinstance(resources, dict) and 'submission_mode' in resources and resources['submission_mode'] is None:
        arguments = {**arguments, 'resources': {k: v for k, v in resources.items() if k != 'submission_mode'}}
    return arguments


MODEL_TOOLS = advertised(TOOLS)
# Gated per turn by operator grants (allow_submit); always advertised.
SUBMIT_TOOLS = ('submit_job', 'remote_submit', 'submit_stage')
# Old entry points others now cover: submit_stage (phase=null for tasks without
# stage gates) and inspect_structure (imports a path outside the workspace and
# returns import_receipt). Still executable so old conversations and audit logs keep working.
LEGACY_ALIASES = ('submit_job', 'remote_submit', 'import_structure_file')
# Preparation without a selected scan plan. The chat session requires plans, so
# these are neither callable nor advertised there; CLI and tests still use them.
PLAN_BYPASS_TOOLS = ('prepare_calculation', 'remote_prepare')
