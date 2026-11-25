"""Remote engine release binding, digest-memo sharing, and VASP environment verification.

Part of the stdin worker program (see remote_worker.WORKER_UNITS): standard library only.
"""
import contextlib
import hashlib
import json
from pathlib import Path
import re
import subprocess
import sys

# <bundle:package-only>
from vasp_agent.remote.worker_common import SubmissionError, digest_memo, sha
# </bundle:package-only>


ENGINE_MODULES = {
    'vasp_band_agent': 'vasp_agent.workflow.vasp_band_agent',
    'band_export': 'vasp_agent.workflow.band_export',
    'full_workflow': 'vasp_agent.workflow.full_workflow',
    'workflow_policy': 'vasp_agent.workflow.workflow_policy',
    'workflow_prepare': 'vasp_agent.workflow.workflow_prepare',
    'potential_library': 'vasp_agent.workflow.potential_library',
    'input_review': 'vasp_agent.workflow.input_review',
    'stage_gate': 'vasp_agent.workflow.stage_gate',
    'scan_config': 'vasp_agent.workflow.scan_config',
    'convergence_reuse': 'vasp_agent.workflow.convergence_reuse',
    'convergence_array': 'vasp_agent.workflow.convergence_array',
    'convergence_array_checks': 'vasp_agent.workflow.convergence_array_checks',
    'dimensionality': 'vasp_agent.workflow.dimensionality',
    'band_plot': 'vasp_agent.workflow.band_plot',
    'band_path': 'vasp_agent.workflow.band_path',
    'band_path_fork': 'vasp_agent.workflow.band_path_fork',
    'band_path_candidates': 'vasp_agent.workflow.band_path_candidates',
    'band_analysis': 'vasp_agent.workflow.band_analysis',
    'stage_definitions': 'vasp_agent.workflow.stage_definitions',
    'parameter_specs': 'vasp_agent.workflow.parameter_specs',
    'scf_reuse': 'vasp_agent.workflow.scf_reuse',
    'dos_stage': 'vasp_agent.workflow.dos_stage',
    'dos_plot': 'vasp_agent.workflow.dos_plot',
    'remote_worker': 'vasp_agent.remote.remote_worker',
}


def engine_module(name):
    from importlib import import_module
    # A transmitted stdin worker loads the task's release through its flat
    # launchers. Local package users (including tests) use qualified modules.
    module = import_module(ENGINE_MODULES[name] if __package__ else name)
    share_digest_memo()
    return module


# Set by memoized_action for one dispatched action: (memo, exit stack, engine cores sharing it).
_MEMO_SCOPE = None


@contextlib.contextmanager
def memoized_action():
    """One digest memo for one worker action, shared with the task's engine once loaded."""
    global _MEMO_SCOPE
    with digest_memo() as memo, contextlib.ExitStack() as engines:
        previous, _MEMO_SCOPE = _MEMO_SCOPE, (memo, engines, set())
        try:
            yield memo
        finally:
            _MEMO_SCOPE = previous


def share_digest_memo():
    """Let a loaded engine core hash through the active action's memo."""
    scope = _MEMO_SCOPE
    core = sys.modules.get(ENGINE_MODULES['vasp_band_agent'])
    if scope is None or core is None or not hasattr(core, 'digest_memo') or id(core) in scope[2]:
        return
    scope[2].add(id(core))
    scope[1].enter_context(core.digest_memo(scope[0]))


def execution_tag_status(config, phase=None):
    """Whether the task's bound engine writes KPAR/NCORE, and the values it derives."""
    wanted = config.get('execution_tags')
    full_workflow = engine_module('full_workflow')
    supported = getattr(full_workflow, 'EXECUTION_TAGS_VERSION', 0)
    if wanted is None:
        return {'enabled': False, 'engine_version': supported,
                'note': '该任务准备时未启用并行标签，按原审阅方案运行（KPAR/NCORE 为 VASP 默认 1）；'
                        '需要时部署新版并准备新任务，不改写已有任务。'}
    if type(wanted) is not int or supported < wanted:
        raise ValueError(f'任务要求并行标签版本 {wanted}，绑定的计算引擎不支持；请部署新版并准备新任务。')
    return full_workflow.execution_preview(config, phase)


def engine_release(meta):
    """Read the release a task is bound to, and say so when the record predates it."""
    release = meta.get('release')
    if not isinstance(release, str) or '\n' in release or not Path(release).is_absolute():
        raise ValueError('该任务未记录绑定的引擎版本，无法安全加载；请重新部署并重新准备该任务（不会改用当前版本）。')
    return release


def deployed_release(root):
    """The release new tasks are prepared against: the latest deploy, never a task's own."""
    return Path(json.loads((root / 'deployment.json').read_text())['release'])


def activate_release(release):
    """The one place this worker points engine imports at a release."""
    sys.path.insert(0, str(release))
    return Path(release)


def task_release(meta):
    """Activate the release an existing task is bound to (see engine_release)."""
    return activate_release(engine_release(meta))


def task_config(folder):
    path = folder / 'config.json'
    return json.loads(path.read_text(encoding='utf-8')) if path.is_file() else {}


def stage_definitions(config):
    """The bound engine's stage definitions for a task that records its stages, else None."""
    if config.get('stages') is None:
        return None
    try:
        module = engine_module('stage_definitions')
    except ImportError as exc:
        raise ValueError('任务记录了阶段定义，但绑定的引擎没有阶段定义模块；不按旧流程猜测，请人工检查。') from exc
    if getattr(module, 'STAGES_VERSION', 0) < 1:
        raise ValueError('任务记录了阶段定义，但绑定的引擎不支持；不按旧流程猜测，请人工检查。')
    return module


# The legacy tasks' download allowlist, unchanged; a task recording its stages
# adds what its stage definitions name, plus config.json that names the stages.
RESULT_FILES = (
    'environment-review.json', 'relaxation-diagnostic.json', 'convergence-reuse.json', 'parameter-choice.json',
    'scan-verification.json', 'physics-model.json', 'checkpoint.json', 'geometry-summary.json', 'scf-summary.json',
    'review-convergence.json', 'review-relaxation.json', 'review-scf.json', 'review-bands.json',
    'REVIEW-convergence.md', 'REVIEW-relaxation.md', 'REVIEW-scf.md', 'REVIEW-bands.md', 'input-plan.json',
    'PARAMETERS.md', 'ai-review.json', 'AI_REVIEW.md', 'manifest.json', 'workflow-state.json', 'convergence.json',
    'relaxation.json', 'post-relax-validation.json', 'potential-selection.json', 'report.json',
    'convergence-array.json', 'relaxed-2.POSCAR', 'bands.png', 'bands.pdf', 'bands.csv', 'bands-path.json',
    'bands-path-choice.json',
)


def inspect_environment(cfg, relax_mode='cell'):
    """Resolve, never launch VASP on a login node. Unknown provenance is explicit."""
    evidence = environment_evidence(cfg['vasp_env'])
    markers = evidence['constraint_markers']
    if markers and relax_mode == 'cell':
        raise ValueError('运行环境包含晶胞约束标记 ' + ', '.join(markers) +
                         '，与三维完整晶胞优化存在冲突；请配置经核验的适用环境。未提交。')
    return evidence


def environment_evidence(env_script):
    """The fingerprint of one environment script and the vasp_std it resolves to."""
    env_path = Path(env_script)
    if not env_path.is_file():
        raise ValueError('VASP 环境脚本不存在。')
    source = env_path.read_text(encoding='utf-8', errors='replace')
    probe = subprocess.run(['bash', '-c', 'source "$1" >/dev/null && command -v vasp_std',
                            'vasp-environment-check', str(env_path)],
                           capture_output=True, text=True, timeout=30)
    binary = Path(probe.stdout.strip().splitlines()[-1]) if probe.stdout.strip() else None
    if probe.returncode or binary is None or not binary.is_absolute() or not binary.is_file():
        raise ValueError('VASP 环境加载或可执行文件解析失败；未启动计算。')
    markers = sorted(set(re.findall(r'fixz|ioptcell', (str(env_path) + source + str(binary.resolve())).lower())))
    return {'environment_script': str(env_path), 'environment_sha256': sha(env_path),
            'executable': str(binary.resolve()), 'executable_sha256': sha(binary),
            'constraint_markers': markers, 'provenance': 'unverified',
            'note': '路径/脚本标记只是环境证据；未发现标记不能证明二进制未经修改。'}


# Worker marker: this worker binds tasks to operator-confirmed environment
# profiles (config['environment_profile']).
ENVIRONMENT_PROFILES_VERSION = 1
PROFILE_FIELDS = ('name', 'env_script', 'environment_sha256', 'executable_sha256', 'capabilities')


def profile_record(profile):
    """The binding a profile-bound task records: these fields only, validated."""
    if not isinstance(profile, dict) or not set(PROFILE_FIELDS) <= set(profile):
        raise ValueError('环境配置记录不完整。')
    name, script, tags = profile['name'], profile['env_script'], profile['capabilities']
    if not isinstance(name, str) or not re.fullmatch(r'[a-z0-9][a-z0-9._-]{0,31}', name) or name == 'default':
        raise ValueError('环境配置名称无效。')
    if not isinstance(script, str) or not Path(script).is_absolute() or any(c in script for c in '\n\x00'):
        raise ValueError('环境配置的脚本路径须为远程绝对路径。')
    if any(not isinstance(profile[k], str) or not re.fullmatch(r'[0-9a-f]{64}', profile[k])
           for k in ('environment_sha256', 'executable_sha256')):
        raise ValueError('环境配置的哈希无效。')
    if (not isinstance(tags, list) or len(tags) > 32
            or any(not isinstance(t, str) or not re.fullmatch(r'[a-z0-9][a-z0-9:._>=+-]{0,47}', t) for t in tags)):
        raise ValueError('环境配置的能力标签无效。')
    return {'name': name, 'env_script': script, 'environment_sha256': profile['environment_sha256'],
            'executable_sha256': profile['executable_sha256'], 'capabilities': sorted(set(tags))}


def task_environment(cfg, profile, relax_mode):
    """The compute config a task runs with, and its environment evidence."""
    if profile is None:
        return cfg, inspect_environment(cfg, relax_mode)
    record = profile_record(profile)
    bound = {**cfg, 'vasp_env': record['env_script']}
    if not Path(record['env_script']).is_file():
        raise SubmissionError('ENVIRONMENT_PROFILE_DRIFT', f'环境配置 {record["name"]} 的脚本 {record["env_script"]} 不存在；'
                              '请用 /software register 重新确认该配置后准备新任务。未继续。')
    environment = inspect_environment(bound, relax_mode)
    changed = [label for key, label in (('environment_sha256', '环境脚本'), ('executable_sha256', 'VASP 可执行文件'))
               if environment.get(key) != record[key]]
    if changed:
        raise SubmissionError('ENVIRONMENT_PROFILE_DRIFT',
            f'环境配置 {record["name"]} 的{"和".join(changed)}与操作者确认时不同'
            f'（登记 {record["executable_sha256"][:12]}，现为 {str(environment.get("executable_sha256"))[:12]}）；'
            '请用 /software register 重新确认该配置，然后准备新任务。未继续，不改用其他环境。')
    return bound, {**environment, 'profile': {'name': record['name'], 'capabilities': record['capabilities'],
                                              'version': ENVIRONMENT_PROFILES_VERSION}}


def require_profile_engine(full_workflow, profile):
    """A profile binding needs an engine that records and honours it."""
    if profile is not None and getattr(full_workflow, 'ENVIRONMENT_PROFILES_VERSION', 0) < 1:
        raise ValueError('任务使用环境配置，但计算引擎不支持；请先部署新版并准备新任务，不会改用默认环境。')


def fork_environment(cfg, source_config):
    """A fork runs where its source ran: the source's environment profile, or vasp_env for a legacy source."""
    profile = source_config.get('environment_profile')
    require_profile_engine(engine_module('full_workflow'), profile)
    return task_environment(cfg, profile, source_config.get('relax_mode', 'cell'))


def release_version(sources):
    """The release name a deploy of these sources gets; the local side computes it too."""
    return hashlib.sha256(json.dumps(sources, sort_keys=True).encode()).hexdigest()[:16]
