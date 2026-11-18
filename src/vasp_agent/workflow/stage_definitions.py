"""Declarative, versioned definitions of the workflow stages a task runs.

One definition per (stage, version) says what a stage needs, how its inputs are
made, what its review must cover, how it runs, what it leaves behind and when it
is complete. A task records the stages it enables, with their versions, in
config['stages'] when it is prepared, so the record is covered by the config
hash in the manifest and by every review snapshot. A task without that record
was prepared before this module existed and runs exactly the four legacy stages.

Nothing here decides physics. Every program acceptance condition named below is
enforced by the code it names (full_workflow, stage_gate); the text only tells
the reviewer and the operator what that code checks. See
docs/design/WORKFLOW_MODULES.md.

Standard library only: the remote worker reads the same records (worker_common
keeps its own copy of the legacy order for tasks bound to releases without this
module).
"""
import re

# Engine marker: engines with it read config['stages']. The worker refuses a
# task that carries the record when its bound engine lacks this module.
STAGES_VERSION = 1
# Shape of one entry in config['stages'].
RECORD_SCHEMA = 1

LEGACY_STAGES = ('convergence', 'relaxation', 'scf', 'bands')
# Stages every full-workflow task runs first, in this order; the run order of
# full_workflow.run_full walks them before any later stage.
BASE_STAGES = ('convergence', 'relaxation', 'scf')
NAME = re.compile(r'[a-z][a-z0-9_]{0,31}')

# Task files that belong to the task, not to one stage (fetched for every task).
COMMON_RESULT_FILES = ('environment-review.json', 'convergence-reuse.json', 'parameter-choice.json',
                       'scan-verification.json', 'physics-model.json', 'checkpoint.json', 'input-plan.json',
                       'PARAMETERS.md', 'ai-review.json', 'AI_REVIEW.md', 'manifest.json',
                       'workflow-state.json', 'potential-selection.json')
# Files every delivered package needs whatever its stages.
COMMON_DELIVERY_FILES = ('workflow-state.json', 'checkpoint.json')

_PHYSICS = {'description': '操作者确认的物理模型（非磁性、PBE、维度）适合研究目标；不适合必须 blocked，即使用户已确认。',
            'accepts': ('pass',)}


def _definition(name, *, depends_on, inputs, checks, execution, summaries=(), root_evidence=(),
                phase_outputs, stage_results=(), delivery=(), kept_inputs=(), completion,
                parameter_spec=None, required_capabilities=(), guide=None, version=1, rules_version=1):
    return {
        'name': name, 'version': version, 'rules_version': rules_version,
        # Accepted stages this one reads; all must come earlier in the task's order.
        'depends_on': tuple(depends_on),
        # How the stage's inputs come about; the rule is enforced by the code named.
        'inputs': inputs,
        # Review items: names are the required_checks list record_stage_review
        # validates; details are shown beside it, never used to rename an item.
        'review': {'required_checks': tuple(checks), 'check_details': {k: dict(v) for k, v in checks.items()},
                   'rules_version': rules_version},
        'execution': execution,
        # Root JSON summaries read into the review evidence whenever they exist.
        'summaries': tuple(summaries),
        # Other root files hashed into the evidence whenever they exist.
        'root_evidence': tuple(root_evidence),
        # Root files a started run of this stage writes (phase resume / release refuse or archive them).
        'phase_outputs': tuple(phase_outputs),
        # Files fetched in a result download besides its review records and the common files.
        'result_files': tuple(stage_results),
        # Files a complete delivery must contain when this stage is enabled.
        'delivery': tuple(delivery),
        # Stage-folder inputs written before launch besides the usual five (kept on resume).
        'kept_inputs': tuple(kept_inputs),
        'completion': completion,
        # Reference to a parameter specification {'id', 'version'} in parameter_specs; the
        # operator chooses its values in the plan menu. None: parameters come from the scan plan.
        'parameter_spec': parameter_spec,
        # Id of the local workflow guide (src/vasp_agent/guides/<id>.md) whose pointer review
        # and preview results carry. Explanation only: acceptance stays in the code named above.
        'guide': guide,
        # Capability tags the task's environment profile must carry, e.g. ('vasp>=6', 'vasp:wannier90').
        # Empty for the legacy stages; matched against operator-confirmed profiles in a later step,
        # not enforced by this engine yet. See WORKFLOW_MODULES.md "软件清单与环境配置".
        'required_capabilities': tuple(required_capabilities),
    }


DEFINITIONS = {
    'convergence': {1: _definition(
        'convergence', depends_on=(),
        inputs={'rule': 'frozen_scan_plan', 'enforced_by': 'scan_config.verify_scan_inputs',
                'note': 'ENCUT 与 k 网格三级扫描，按操作者确认的扫描方案生成并逐项核对。'},
        checks={'physics': _PHYSICS,
                'inputs': {'description': '初始输入与审阅方案逐项一致：INCAR、KPOINTS、POTCAR 元素顺序及扫描序列。',
                           'accepts': ('pass',)},
                'validation_plan': {'description': '收敛判据与优化后复核方案；结果由程序在运行后判定。',
                                    'accepts': ('pass', 'pending')}},
        execution={'mode': 'allocation_or_array', 'runner': 'builtin'},
        summaries=('convergence.json',),
        phase_outputs=('convergence.json', 'convergence-array.json'),
        stage_results=('convergence.json', 'convergence-array.json'),
        completion={'rule': 'ENCUT 与网格扫描均连续两级差值达标',
                    'enforced_by': 'full_workflow.accept_convergence'})},
    'relaxation': {1: _definition(
        'relaxation', depends_on=('convergence',),
        inputs={'rule': 'reviewed_plan_production_parameters', 'enforced_by': 'full_workflow.stage',
                'note': '两轮优化及优化后复核，使用已测试的生产 ENCUT 与网格。'},
        checks={'physics': _PHYSICS,
                'convergence': {'description': '扫描连续两级差值达标（程序已判定），生产 ENCUT 与网格为已测试值。',
                                'accepts': ('pass',)}},
        execution={'mode': 'allocation', 'runner': 'builtin'},
        summaries=('relaxation.json', 'post-relax-validation.json', 'geometry-summary.json'),
        root_evidence=('relaxed-1.POSCAR', 'relaxed-2.POSCAR'),
        phase_outputs=('relaxation.json', 'relaxation-diagnostic.json', 'relaxed-1.POSCAR', 'relaxed-2.POSCAR',
                       'geometry-summary.json', 'post-relax-validation.json'),
        stage_results=('relaxation-diagnostic.json', 'geometry-summary.json', 'relaxation.json',
                       'post-relax-validation.json', 'relaxed-2.POSCAR'),
        completion={'rule': '离子收敛、力/应力达标，优化后更高截断能及更密网格复核通过',
                    'enforced_by': 'full_workflow.check_relaxation, full_workflow.compare'})},
    'scf': {1: _definition(
        'scf', depends_on=('relaxation',),
        inputs={'rule': 'standardized_primitive_same_density', 'enforced_by': 'full_workflow.run_full',
                'note': '优化结构的标准原胞，按倒空间间距换算网格；输出 CHGCAR。'},
        checks={'physics': _PHYSICS,
                'geometry': {'description': '两轮优化收敛，力/应力达标，体积与晶格变化合理。', 'accepts': ('pass',)},
                'post_relax_convergence': {'description': '优化后更高截断能与更密网格复核通过。', 'accepts': ('pass',)}},
        execution={'mode': 'allocation', 'runner': 'builtin'},
        summaries=('scf-summary.json',),
        phase_outputs=('scf-summary.json',),
        stage_results=('scf-summary.json',),
        completion={'rule': '电子收敛，标准原胞静态力/应力达标，CHGCAR 非空并记录哈希',
                    'enforced_by': 'core.execute, full_workflow.run_full'})},
    'bands': {1: _definition(
        'bands', depends_on=('scf',),
        inputs={'rule': 'fixed_scf_charge_line_mode', 'enforced_by': 'band_path.record_for',
                'note': 'ICHARG=11 读取已验收 SCF 的 CHGCAR，沿审阅显示的高对称路径。'},
        checks={'physics': _PHYSICS,
                'scf': {'description': 'SCF 电子收敛、力/应力达标，CHGCAR 哈希与完成记录一致。', 'accepts': ('pass',)},
                'electronic': {'description': 'NBANDS 留有空带（SCF 最高带占据审计），能带路径与审阅一致。',
                               'accepts': ('pass',)}},
        execution={'mode': 'allocation', 'runner': 'builtin'},
        phase_outputs=('report.json', 'bands.csv', 'bands.png', 'bands.pdf'),
        stage_results=('report.json', 'bands.png', 'bands.pdf', 'bands.csv', 'bands-path.json',
                       'bands-path-choice.json'),
        delivery=('report.json', 'bands.csv', 'bands.png', 'bands.pdf'),
        kept_inputs=('CHGCAR',),
        completion={'rule': '全路径本征值有限、最高能带在所有路径点为空，报告与图生成',
                    'enforced_by': 'core.validate_band_data, full_workflow.analyze_full'})},
}


def record(names=LEGACY_STAGES):
    """The config['stages'] entry for a new task enabling these stages, current versions."""
    entries = [{'name': name, 'version': max(DEFINITIONS[name])} for name in names]
    validate(entries)
    return entries


def with_optional(optional=()):
    """The stage names of a new task: the legacy chain followed by the requested optional stages."""
    optional = tuple(optional or ())
    if len(set(optional)) != len(optional) or any(name in LEGACY_STAGES for name in optional):
        raise ValueError('可选阶段重复或与基础阶段重复。')
    unknown = [name for name in optional if name not in DEFINITIONS]
    if unknown:
        raise ValueError(f"本引擎没有阶段 {'、'.join(unknown)} 的定义，不能启用；未创建任务。")
    return LEGACY_STAGES + optional


def parameter_stages(config):
    """Enabled stages whose definition names a parameter spec, in run order."""
    return tuple(entry['name'] for entry in entries(config)
                 if DEFINITIONS[entry['name']][entry['version']]['parameter_spec'] is not None)


def validate(entries):
    """Check a stage record: known definitions, unique names, base stages first, dependencies earlier."""
    if not isinstance(entries, list) or not entries:
        raise ValueError('任务阶段记录无效。')
    names = []
    for entry in entries:
        if (not isinstance(entry, dict) or set(entry) != {'name', 'version'} or not isinstance(entry['name'], str)
                or not NAME.fullmatch(entry['name']) or type(entry['version']) is not int):
            raise ValueError('任务阶段记录格式无效。')
        if entry['version'] not in DEFINITIONS.get(entry['name'], {}):
            raise ValueError(f"本引擎没有阶段 {entry['name']} 的定义版本 {entry['version']}；"
                             '请使用准备该任务的引擎版本，不改写任务。')
        if entry['name'] in names:
            raise ValueError(f"任务阶段 {entry['name']} 重复。")
        if any(dep not in names for dep in DEFINITIONS[entry['name']][entry['version']]['depends_on']):
            raise ValueError(f"阶段 {entry['name']} 的前置阶段未在其之前启用。")
        names.append(entry['name'])
    if tuple(names[:len(BASE_STAGES)]) != BASE_STAGES:
        raise ValueError('完整流程须依次以收敛、优化和 SCF 阶段开始。')
    return tuple(names)


def entries(config):
    """The task's stage record; tasks prepared before the record get the legacy stages at version 1."""
    stages = (config or {}).get('stages')
    if stages is None:
        return [{'name': name, 'version': 1} for name in LEGACY_STAGES]
    validate(stages)
    return stages


def order(config):
    """Enabled stage names in run order."""
    return tuple(entry['name'] for entry in entries(config))


def definition(config, phase):
    for entry in entries(config):
        if entry['name'] == phase:
            return DEFINITIONS[entry['name']][entry['version']]
    raise ValueError(f'阶段 {phase} 不在本任务启用的阶段中。')


def following(config, phase):
    """The stage after phase, or 'complete' when phase is the last enabled one."""
    names = order(config)
    index = names.index(phase)
    return names[index + 1] if index + 1 < len(names) else 'complete'


def before(config, phase):
    """Enabled stages that run before phase."""
    names = order(config)
    return names[:names.index(phase)]


def through(config, phase):
    names = order(config)
    return names[:names.index(phase) + 1]


def is_final(config, phase):
    return order(config)[-1] == phase


def is_legacy(config):
    return order(config) == LEGACY_STAGES


def review_rules(config, phase):
    """Per-check descriptions and the rules version, shown beside required_checks."""
    review = definition(config, phase)['review']
    return {'check_details': {name: {'description': item['description'], 'accepts': list(item['accepts'])}
                              for name, item in review['check_details'].items()},
            'rules_version': review['rules_version']}


def check_allowed(config, phase, item, judgment):
    return judgment in definition(config, phase)['review']['check_details'][item]['accepts']


def required_capabilities(stage_entries):
    """Union of the capability tags the given stage record needs, in first-seen order."""
    validate(stage_entries)
    tags = []
    for entry in stage_entries:
        for tag in DEFINITIONS[entry['name']][entry['version']]['required_capabilities']:
            if tag not in tags:
                tags.append(tag)
    return tuple(tags)


def satisfies(capabilities, requirement):
    """One requirement against an environment's tags.

    'vasp:wannier90' needs that exact tag; 'vasp>=6' needs a 'vasp:N' tag with
    N >= 6 (the probe tags each build with its major version).
    """
    tags = set(capabilities)
    match = re.fullmatch(r'([a-z][a-z0-9_]*)>=(\d+)', requirement)
    if not match:
        return requirement in tags
    prefix, minimum = match.group(1) + ':', int(match.group(2))
    return any(tag.startswith(prefix) and tag[len(prefix):].isdigit() and int(tag[len(prefix):]) >= minimum
               for tag in tags)


def unmet(required, capabilities):
    """Requirements the tags do not satisfy, in order."""
    return tuple(tag for tag in required if not satisfies(capabilities, tag))


def known_names():
    """Every stage this engine defines (tool schemas and operator commands validate against it)."""
    return tuple(DEFINITIONS)


def summary_files(config):
    """Root summaries read into review evidence: every known definition's, plus the task's own.

    'Whenever the file exists' is the rule, so the set does not shrink with the
    task's stages: a fork that drops a stage still hashes the summaries it copied.
    """
    names = []
    for versions in DEFINITIONS.values():
        for item in versions.values():
            names += [n for n in item['summaries'] if n not in names]
    for entry in entries(config):
        names += [n for n in DEFINITIONS[entry['name']][entry['version']]['summaries'] if n not in names]
    return tuple(names)


def root_evidence_files(config):
    names = []
    for versions in DEFINITIONS.values():
        for item in versions.values():
            names += [n for n in item['root_evidence'] if n not in names]
    return tuple(names)


def phase_outputs(config, phase):
    return definition(config, phase)['phase_outputs']


def kept_inputs(config, phase):
    return definition(config, phase)['kept_inputs']


def result_files(config):
    """Every file a result download of this task may contain, common files first."""
    names = list(COMMON_RESULT_FILES)
    for entry in entries(config):
        item = DEFINITIONS[entry['name']][entry['version']]
        names += [n for n in (*item['result_files'], f"review-{entry['name']}.json", f"REVIEW-{entry['name']}.md")
                  if n not in names]
    return tuple(names)


def fetchable_files():
    """Every file any known stage may deliver, plus the config that names the stages."""
    names = list(COMMON_RESULT_FILES) + ['config.json']
    for name, versions in DEFINITIONS.items():
        for item in versions.values():
            names += [n for n in (*item['result_files'], f'review-{name}.json', f'REVIEW-{name}.md') if n not in names]
    return frozenset(names)


def delivery_files(config):
    names = list(COMMON_DELIVERY_FILES)
    for entry in entries(config):
        names += [n for n in DEFINITIONS[entry['name']][entry['version']]['delivery'] if n not in names]
    return tuple(names)


def require_runnable(config, gated):
    """A task without per-stage reviews runs only the legacy chain end to end."""
    if not gated and not is_legacy(config):
        raise ValueError('启用额外阶段的任务必须逐阶段审阅；未启动计算。')
