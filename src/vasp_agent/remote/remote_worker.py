"""Trusted remote RPC worker. Payload travels on stdin, never in shell interpolation."""
import base64
import contextlib
import hashlib
import io
import json
import os
from pathlib import Path
import re
import shlex
import subprocess
import sys
import shutil
from datetime import datetime, timezone

# Names from the earlier worker units, re-exported so `remote_worker.X` stays the
# one import path for callers. The stdin program defines them by concatenation.
# <bundle:package-only>
from vasp_agent.remote.worker_common import (
    LEGACY_PHASES, SubmissionError, diagnostic_text, digest_memo, file_sha, is_phase_name, sha, task_phases, write_json)
from vasp_agent.remote.worker_slurm import (
    MAX_ARRAY_CONCURRENCY, MAX_ARRAY_TESTS, accounting_window, array_layout, array_scheduler_status,
    capacity_preflight, monitor_status, scheduler_read, submission_accounting, submission_command_evidence)
from vasp_agent.remote.worker_receipts import (
    lock_is_held, open_lock, recover_submission, recovery_barrier, recovery_context, recovery_path,
    submission_lock, submit_with_receipt, try_lock)
from vasp_agent.remote.worker_inventory import DEFAULT_ROOTS, MAX_ROOTS, expand_root, software_inventory
# </bundle:package-only>


def diagnostic_file(folder, name, tail=False):
    """Only fixed caller-selected task files; never follow links or execute content."""
    path = folder / name
    if path.is_symlink() or not path.resolve().is_relative_to(folder.resolve()):
        raise ValueError('诊断文件路径越界或为链接。')
    if not path.exists():
        return None
    if not path.is_file():
        raise ValueError('诊断目标不是普通文件。')
    if not tail and path.stat().st_size > 1_000_000:
        raise ValueError('诊断文件过大。')
    with path.open('rb') as stream:
        if tail:
            stream.seek(max(0, path.stat().st_size - 12000))
        return stream.read(12000 if tail else 1_000_001).decode('utf-8', errors='replace' if tail else 'strict').replace('\r\n', '\n')


def task_evidence_files(folder, phase):
    """Fixed allowlist only; hash exactly the bounded bytes that are returned."""
    names = ['events.jsonl', 'remote-submission.json']
    if phase in task_phases(folder):
        names.append('submissions/' + phase + '.json')
    results = []
    for name in names:
        path = folder / name
        item = {'path': name, 'source': 'task_file', 'content_is_untrusted': True}
        try:
            cursor = path
            while cursor != folder:
                if cursor.is_symlink():
                    raise ValueError('日志路径不允许链接。')
                cursor = cursor.parent
            if not path.resolve().is_relative_to(folder.resolve()):
                raise ValueError('日志路径越界。')
            if not path.exists():
                results.append({**item, 'exists': False})
                continue
            if not path.is_file() or path.stat().st_nlink != 1:
                raise ValueError('日志须为非硬链接普通文件。')
            with path.open('rb') as stream:
                size = os.fstat(stream.fileno()).st_size
                offset = max(0, size - 12000)
                stream.seek(offset)
                data = stream.read(12000)
            text = data.decode('utf-8', errors='replace')
            # Handles common JSON credential values as well as ordinary key=value.
            text = re.sub(r'(?i)("(?:password|passwd|api[_-]?key|secret|access[_-]?token|token)"\s*:\s*)"(?:\\.|[^"\\])*"',
                          r'\1"[REDACTED]"', text)
            results.append({**item, 'exists': True, 'file_size': size, 'offset': offset,
                            'bytes_read': len(data), 'truncated': offset > 0 or offset + len(data) < size,
                            'sha256_scope': 'raw_returned_slice', 'sha256': hashlib.sha256(data).hexdigest(),
                            'text': diagnostic_text(text),
                            'note': '哈希对应脱敏前读取片段；截断片段可能从记录中间开始，文件可能仍在增长。'})
        except (OSError, ValueError) as exc:
            results.append({**item, 'read_error': diagnostic_text(str(exc))})
    return results


def submission_diagnostic(folder, phase, job_id=None, start_time=None, end_time=None):
    result = submission_accounting(folder, phase, job_id, start_time, end_time)
    evidence = {'checked_at': datetime.now(timezone.utc).isoformat(), 'current_script': None,
                'capacity': None, 'logs': [], 'errors': []}
    evidence['task_files'] = task_evidence_files(folder, phase)
    if result.get('target_matched'):
        evidence['submission_command'] = submission_command_evidence(folder, result['accounting'][0], phase)
    else:
        evidence['submission_command'] = {'status': 'requires_unique_job', 'submission_phase_verified': False}
    result['submission_phase_verified'] = evidence['submission_command'].get('submission_phase_verified', False)
    result['execution_phase_verified'] = False
    findings, suggestions = [], []
    cache, failures = {}, {}
    def read(command):
        key = tuple(command)
        if key in failures:
            raise failures[key]
        if key not in cache:
            try:
                cache[key] = scheduler_read(command)
            except SubmissionError as exc:
                failures[key] = exc
                raise
        return cache[key]
    def assess(script):
        try:
            return {'status': 'compatible', 'details': capacity_preflight(script, read=read)}
        except SubmissionError as exc:
            return {'status': 'incompatible' if exc.error_code == 'SCHEDULER_CAPACITY_EXCEEDED' else 'unknown',
                    'code': exc.error_code, 'message': diagnostic_text(str(exc))}
    try:
        script = diagnostic_file(folder, 'submit.slurm')
        if script is not None:
            directives = dict(re.findall(r'^#SBATCH --([\w-]+)=([^\n]+)$', script, re.M))
            evidence['current_script'] = {'sha256': sha(folder / 'submit.slurm'),
                'resources': {k: diagnostic_text(v) for k, v in directives.items()
                              if k in ('partition', 'nodes', 'ntasks', 'cpus-per-task', 'time')},
                'historical_snapshot': False}
            evidence['capacity'] = assess(script)
            if evidence['capacity']['status'] == 'incompatible':
                findings.append({'code': 'CURRENT_RESOURCE_CAPACITY_CONFLICT', 'evidence': 'current_script + current_partition',
                                 'message': evidence['capacity']['message']})
            config_text = diagnostic_file(folder, 'config.json')
            config = json.loads(config_text) if config_text else {}
            if not isinstance(config, dict):
                raise ValueError('计算配置不是对象。')
            # Suggestions must preserve the application's per-test divisibility rule.
            parallel, cores = config.get('parallel_tests'), config.get('cores_per_test')
            evidence['current_config_resources'] = {'parallel_tests': parallel, 'cores_per_test': cores}
            nodes = int(directives.get('nodes', '0'))
            if type(cores) is int and cores > 0 and nodes > 0 and cores % nodes:
                findings.append({'code': 'PER_TEST_NODE_DIVISIBILITY', 'evidence': 'current_config + current_script',
                                 'message': f'每组 {cores} MPI 进程不能在 {nodes} 节点均分。'})
            if (type(parallel) is int and type(cores) is int and parallel > 0 and 0 < cores <= 65536
                    and parallel * cores == int(directives.get('ntasks', '0'))
                    and (evidence['capacity']['status'] != 'compatible' or (nodes > 0 and cores % nodes))):
                for candidate in range(1, min(cores, 256) + 1):
                    if cores % candidate or candidate == nodes:
                        continue
                    candidate_script = re.sub(r'^#SBATCH --nodes=.*$', f'#SBATCH --nodes={candidate}', script, flags=re.M)
                    check = assess(candidate_script)
                    if check['status'] == 'compatible':
                        suggestions.append({'nodes': candidate, 'parallel_tests': parallel, 'cores_per_test': cores,
                                            'cpus_per_node': check['details']['cpus_per_node'],
                                            'default_memory_mb_per_node': check['details']['default_memory_mb_per_node']})
                        if len(suggestions) == 3:
                            break
    except (OSError, ValueError, TypeError, UnicodeError) as exc:
        evidence['errors'].append({'source': 'current_task_files', 'message': diagnostic_text(str(exc))})
    # Historical requests are never inferred from today's script.
    for row in (result.get('accounting') or [])[:3]:
        try:
            cpus, nodes = int(row.get('requested_cpus', '')), int(row.get('requested_nodes', ''))
            partition = row.get('partition', '')
            if cpus <= 0 or nodes <= 0 or cpus % nodes or not re.fullmatch(r'[A-Za-z0-9_-]+', partition):
                continue
            historical_script = f'#SBATCH --partition={partition}\n#SBATCH --nodes={nodes}\n#SBATCH --ntasks={cpus}\n'
            check = assess(historical_script)
            row['comparison_with_current_partition'] = check
            if check['status'] == 'incompatible':
                findings.append({'code': 'HISTORICAL_REQUEST_CURRENT_CAPACITY_CONFLICT', 'job_id': row['job_id'],
                    'evidence': 'sacct_requested_resources + current_partition', 'message': check['message'],
                    'historical_cause_proven': False})
        except (ValueError, TypeError):
            pass
    # Only a uniquely matched job permits reading its fixed log filename.
    if result.get('target_matched'):
        row = result['accounting'][0]
        name = 'slurm-' + row['job_id'] + '.log'
        try:
            log = diagnostic_file(folder, name, tail=True)
            evidence['logs'].append({'path': name, 'exists': log is not None,
                                     'tail': diagnostic_text(log) if log is not None else None})
        except (OSError, ValueError, UnicodeError) as exc:
            evidence['errors'].append({'source': name, 'message': diagnostic_text(str(exc))})
    evidence['errors'].extend({'source': list(command), 'message': diagnostic_text(str(exc))}
                              for command, exc in failures.items())
    evidence['partition_queries'] = [{'command': list(command), 'output': diagnostic_text(output)}
                                     for command, output in cache.items()]
    return {**result, 'diagnosis': {'evidence': evidence, 'confirmed_configuration_findings': findings,
        'resource_layout_candidates': suggestions, 'historical_root_cause_confirmed': False,
        'limitations': ['当前分区规格不是历史配置快照；当前脚本不是历史提交脚本证据。',
                       '容量对比默认采用当前分区默认内存，历史ReqMem/ReqTRES单独列出，不能视为相同。',
                       '失败码和缺少日志不能单独证明VASP错误；查询失败应保留已取得的其他证据。'],
        'next_steps': ['先说明已有配置冲突，再说明历史根因的证据缺口，不因缺少控制器日志而忽略配置问题。',
                       '候选布局只验证当前CPU/默认内存和均分规则，不保证配额、可用性或性能；需操作者确认及重新审阅。',
                       '本工具不执行脚本、不修改资源或回执、不重交；历史精确错误可由管理员补查。']}}


ENGINE_MODULES = {
    'vasp_band_agent': 'vasp_agent.workflow.vasp_band_agent',
    'full_workflow': 'vasp_agent.workflow.full_workflow',
    'potential_library': 'vasp_agent.workflow.potential_library',
    'input_review': 'vasp_agent.workflow.input_review',
    'stage_gate': 'vasp_agent.workflow.stage_gate',
    'scan_config': 'vasp_agent.workflow.scan_config',
    'convergence_reuse': 'vasp_agent.workflow.convergence_reuse',
    'convergence_array': 'vasp_agent.workflow.convergence_array',
    'dimensionality': 'vasp_agent.workflow.dimensionality',
    'band_plot': 'vasp_agent.workflow.band_plot',
    'band_path': 'vasp_agent.workflow.band_path',
    'band_analysis': 'vasp_agent.workflow.band_analysis',
    'stage_definitions': 'vasp_agent.workflow.stage_definitions',
    'parameter_specs': 'vasp_agent.workflow.parameter_specs',
    'remote_worker': 'vasp_agent.remote.remote_worker',
}
# Sibling modules this worker is assembled from, in dependency order. The
# package imports them; the stdin program concatenates them ahead of this file
# (bundle.worker_program), so each may use only the stdlib and earlier units.
WORKER_UNITS = ('worker_common', 'worker_slurm', 'worker_receipts', 'worker_inventory')
# Explicit package allowlist plus legacy launchers. Existing release layouts stay usable.
SOURCE_FILES = tuple(name + '.py' for name in ENGINE_MODULES) + (
    'vasp_agent/__init__.py', 'vasp_agent/workflow/__init__.py', 'vasp_agent/remote/__init__.py',
) + tuple(module.replace('.', '/') + '.py' for module in ENGINE_MODULES.values()) + tuple(
    'vasp_agent/remote/' + unit + '.py' for unit in WORKER_UNITS)


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
    """One digest memo for one worker action, shared with the task's engine once loaded.

    Only dispatch enters this, around a single read/verify action (MEMO_ACTIONS);
    the memo dies with the action. See worker_common.digest_memo for its rules.
    """
    global _MEMO_SCOPE
    with digest_memo() as memo, contextlib.ExitStack() as engines:
        previous, _MEMO_SCOPE = _MEMO_SCOPE, (memo, engines, set())
        try:
            yield memo
        finally:
            _MEMO_SCOPE = previous


def share_digest_memo():
    """Let a loaded engine core hash through the active action's memo.

    A release older than the memo has no core.digest_memo and simply hashes
    every time, exactly as before.
    """
    scope = _MEMO_SCOPE
    core = sys.modules.get(ENGINE_MODULES['vasp_band_agent'])
    if scope is None or core is None or not hasattr(core, 'digest_memo') or id(core) in scope[2]:
        return
    scope[2].add(id(core))
    scope[1].enter_context(core.digest_memo(scope[0]))


def execution_tag_status(config, phase=None):
    """Whether the task's bound engine writes KPAR/NCORE, and the values it derives.

    The flag is set by the engine that prepared the task, so an old task keeps
    running exactly as reviewed; a flagged task on an engine without the marker
    is refused rather than run without the tags it was reviewed with.
    """
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
    """Read the release a task is bound to, and say so when the record predates it.

    `meta['release']` raised a bare KeyError for a job folder whose remote-job.json
    predates the key, which reached the operator as a traceback naming neither the
    task nor what to do. The binding is deliberate and is never re-resolved from the
    current deployment, so the only honest answer is to redeploy and re-prepare.
    """
    release = meta.get('release')
    # Path.is_absolute() rather than a leading '/' -- the guard rejects a record
    # that cannot name a directory, not a deployment on one particular OS, and
    # the Windows drive form is legitimate for a Windows-hosted deployment.
    if not isinstance(release, str) or '\n' in release or not Path(release).is_absolute():
        raise ValueError('该任务未记录绑定的引擎版本，无法安全加载；请重新部署并重新准备该任务（不会改用当前版本）。')
    return release


def deployed_release(root):
    """The release new tasks are prepared against: the latest deploy, never a task's own."""
    return Path(json.loads((root / 'deployment.json').read_text())['release'])


def activate_release(release):
    """The one place this worker points engine imports at a release.

    engine_module resolves flat launcher names on sys.path in the stdin program,
    so whichever release is first there is the engine an action runs. Each action
    calls this once, before its first engine import, with either deployed_release
    (new tasks) or task_release (existing tasks) -- one worker process serves one
    action, so the path never names two releases at once.
    """
    sys.path.insert(0, str(release))
    return Path(release)


def task_release(meta):
    """Activate the release an existing task is bound to (see engine_release)."""
    return activate_release(engine_release(meta))
def task_config(folder):
    path = folder / 'config.json'
    return json.loads(path.read_text(encoding='utf-8')) if path.is_file() else {}


def stage_definitions(config):
    """The bound engine's stage definitions for a task that records its stages, else None.

    A task records config['stages'] only when its engine has stage_definitions,
    so the module is there for every such task; its absence means a damaged or
    hand-edited task and is refused rather than read as the legacy chain. Tasks
    without the record never load it -- older releases keep working unchanged.
    The task's release must already be active.
    """
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
RESULT_FILES = ('environment-review.json', 'relaxation-diagnostic.json', 'convergence-reuse.json', 'parameter-choice.json', 'scan-verification.json', 'physics-model.json', 'checkpoint.json', 'geometry-summary.json', 'scf-summary.json',
                'review-convergence.json', 'review-relaxation.json', 'review-scf.json', 'review-bands.json',
                'REVIEW-convergence.md', 'REVIEW-relaxation.md', 'REVIEW-scf.md', 'REVIEW-bands.md', 'input-plan.json', 'PARAMETERS.md', 'ai-review.json', 'AI_REVIEW.md', 'manifest.json', 'workflow-state.json', 'convergence.json', 'relaxation.json',
                'post-relax-validation.json', 'potential-selection.json', 'report.json', 'convergence-array.json',
                'relaxed-2.POSCAR', 'bands.png', 'bands.pdf', 'bands.csv', 'bands-path.json', 'bands-path-choice.json')


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
    evidence = {'environment_script': str(env_path), 'environment_sha256': sha(env_path),
                'executable': str(binary.resolve()), 'executable_sha256': sha(binary),
                'constraint_markers': markers, 'provenance': 'unverified',
                'note': '路径/脚本标记只是环境证据；未发现标记不能证明二进制未经修改。'}
    return evidence


# Worker marker: this worker binds tasks to operator-confirmed environment
# profiles (config['environment_profile']). The engine's marker is
# full_workflow.ENVIRONMENT_PROFILES_VERSION; a profile-bound task on an engine
# without it is refused, never run with the compute profile's vasp_env.
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
    """The compute config a task runs with, and its environment evidence.

    Without a profile this is exactly the legacy path: the server's vasp_env.
    With one, the profile's script replaces vasp_env, and the script and the
    executable it resolves to must still hash as the operator confirmed them;
    a change is refused and the operator re-confirms the profile, then prepares
    a new task. The evidence records the profile, so environment-review.json
    (hashed into every review) names it.
    """
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


def list_workflow_jobs(base, include_archived=False):
    """List only task metadata under the configured workspace, never VASP outputs."""
    rows = []
    base = Path(base).resolve()
    for folder in base.iterdir() if base.is_dir() else []:
        if not folder.is_dir() or not folder.resolve().is_relative_to(base) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_-]{0,63}', folder.name):
            continue
        archived = (folder / '.task-archived.json').exists()
        if archived and not include_archived:
            continue
        modified = folder.stat().st_mtime
        def read(name):
            nonlocal modified
            path = folder / name
            if not path.exists():
                return {}
            if not path.resolve().is_relative_to(base) or path.stat().st_size > 1_000_000:
                raise ValueError('元数据文件越界或过大')
            modified = max(modified, path.stat().st_mtime)
            data = json.loads(path.read_text(encoding='utf-8'))
            if not isinstance(data, dict):
                raise ValueError('元数据格式无效')
            return data
        try:
            manifest, state = read('manifest.json'), read('workflow-state.json')
            if not manifest:
                continue
            checkpoint, physics = read('checkpoint.json'), read('physics-model.json')
            phase = checkpoint.get('next', 'convergence')
            reason = ''
            if not manifest.get('stage_gates'):
                reason = '旧版任务不支持 /physics'
            elif phase != 'convergence' or (folder / 'submissions').exists() or (folder / '.agent.lock').exists():
                reason = '任务已进入执行流程，物理模型已锁定'
            rows.append({'name': folder.name, 'status': state.get('status', 'unknown'),
                'archived': archived,
                'phase': phase, 'physics_confirmed': bool(physics), 'goal': physics.get('goal', ''),
                'selectable': not reason, 'reason': reason, 'modified': modified})
        except (ValueError, OSError) as exc:
            rows.append({'name': folder.name, 'status': 'unreadable', 'phase': '-',
                'archived': archived,
                'physics_confirmed': False, 'selectable': False, 'reason': str(exc), 'modified': modified})
    rows.sort(key=lambda row: (row['modified'], row['name']), reverse=True)
    return {'jobs': rows[:100], 'total': len(rows), 'limit': 100}


def task_target(base, name):
    base = Path(base)
    def linked(path):
        return path.is_symlink() or getattr(path, 'is_junction', lambda: False)()
    if linked(base) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_-]{0,63}', name):
        raise ValueError('任务路径无效。')
    folder = base / name
    if linked(folder) or not folder.is_dir() or folder.resolve().parent != base.resolve():
        raise ValueError('任务不存在或路径越界。')
    manifest = folder / 'manifest.json'
    if linked(manifest) or not manifest.is_file():
        raise ValueError('目录不是可识别的 Agent 任务。')
    return folder.resolve()


def deletion_preview(base, name, require_scheduler=True):
    folder = task_target(base, name)
    if (folder / '.agent.lock').exists():
        raise ValueError('任务有运行锁，不能删除；请先核实运行状态。')
    entries = []
    def walk_error(error):
        raise error
    for parent, dirs, files in os.walk(folder, followlinks=False, onerror=walk_error):
        for child in sorted(dirs + files):
            path = Path(parent) / child
            if path.is_symlink() or getattr(path, 'is_junction', lambda: False)():
                raise ValueError('任务包含符号链接或目录联接，请人工检查，未删除。')
            if not path.resolve().is_relative_to(folder):
                raise ValueError('任务内路径越界。')
            st = path.stat()
            entries.append((str(path.relative_to(folder)), st.st_size, st.st_mtime_ns))
    job_ids = set()
    array_records = {}
    records = [folder / 'submission.json', folder / 'remote-submission.json', *sorted((folder / 'submissions').glob('*.json'))]
    for path in records:
        if path.exists():
            data = json.loads(path.read_text(encoding='utf-8'))
            job_id = str(data.get('job_id', ''))
            if not re.fullmatch(r'\d+', job_id):
                raise ValueError('存在结果不明确的提交记录，须先人工核实，不能删除。')
            job_ids.add(job_id)
            if data.get('submission_mode') == 'array':
                if job_id in array_records and array_records[job_id] != data:
                    raise ValueError('同一数组存在不一致提交记录，不能删除。')
                array_records[job_id] = data
    def query(command):
        # The conservative reading is right -- refuse to delete when the queue cannot be
        # read -- but `check=True` reported it as a raw CalledProcessError traceback,
        # while check_previous_job converts the same class into a message naming the
        # command. Same failure, same refusal, useful text.
        try:
            result = subprocess.run(command, capture_output=True, text=True, timeout=20)
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise ValueError(f'{command[0]} 查询失败（{type(exc).__name__}）；无法确认作业状态，不能删除。') from exc
        returncode = getattr(result, 'returncode', 0)
        if returncode:
            detail = (getattr(result, 'stderr', '') or result.stdout or '').strip()[-1200:]
            raise ValueError(f'{command[0]} 查询失败（退出码 {returncode}）：{detail or "未返回诊断信息"}；无法确认作业状态，不能删除。')
        return result.stdout
    states = {}
    if require_scheduler or job_ids or shutil.which('squeue'):
        # Check all own queued jobs, including manually submitted jobs with no agent record.
        queued = query(['squeue', '--me', '-h', '-o', '%i|%Z'])
        for line in queued.splitlines():
            if not line.strip():
                continue
            fields = line.strip().split('|', 1)
            if len(fields) != 2 or not Path(fields[1]).is_absolute():
                raise ValueError('无法确认排队作业的工作目录，不能删除。')
            cwd = Path(fields[1]).resolve()
            if (fields[0] in job_ids or any(fields[0].startswith(key + '_') for key in array_records)
                    or cwd == folder or cwd.is_relative_to(folder)):
                raise ValueError('任务仍在排队或运行，不能删除。')
        terminal = {'COMPLETED', 'FAILED', 'CANCELLED', 'TIMEOUT', 'OUT_OF_MEMORY', 'NODE_FAIL'}
        for job_id in sorted(job_ids):
            if job_id in array_records:
                result = query(['sacct', '-n', '-j', job_id, '--format=JobID,State,ExitCode', '-P'])
                summary = array_scheduler_status(array_records[job_id], queued, result)
                if not summary['all_terminal']:
                    raise ValueError('无法确认全部数组元素已终止，不能删除。')
                states[job_id] = {item['job_id']: item['state'] for item in summary['tasks']}
                continue
            result = query(['sacct', '-n', '-j', job_id, '--format=JobIDRaw,State', '-P'])
            rows = [line.strip().split('|') for line in result.splitlines()]
            row = next((r for r in rows if len(r) >= 2 and r[0] == job_id), None)
            if row is None or not row[1].split() or row[1].split()[0] not in terminal:
                raise ValueError('无法确认作业已经终止，不能删除。')
            states[job_id] = row[1]
    state = json.loads((folder / 'workflow-state.json').read_text(encoding='utf-8')) if (folder / 'workflow-state.json').exists() else {}
    if state.get('status') in ('running', 'submitted', 'submission_uncertain') and not states:
        raise ValueError('工作流显示正在执行或提交状态不明确，不能删除。')
    fingerprint = hashlib.sha256(json.dumps(sorted(entries), ensure_ascii=True).encode()).hexdigest()
    return {'name': name, 'path': str(folder), 'workflow_status': state.get('status', 'unknown'),
            'job_states': states, 'queue_checked': bool(require_scheduler or job_ids or shutil.which('squeue')),
            'archived': (folder / '.task-archived.json').exists(), 'fingerprint': fingerprint}


def manage_task(base, name, action, *, confirmation=None, fingerprint=None, require_scheduler=True):
    folder = task_target(base, name)
    marker = folder / '.task-archived.json'
    if marker.is_symlink():
        raise ValueError('归档标记是链接，拒绝操作。')
    if action == 'archive':
        if marker.with_suffix('.tmp').exists() or marker.with_suffix('.tmp').is_symlink():
            raise ValueError('归档临时文件已存在，请先检查。')
        write_json(marker, {'archived': True})
        return {'archived': True, 'path': str(folder), 'note': '文件保留；不会取消已有作业。'}
    if action == 'restore':
        marker.unlink(missing_ok=True)
        return {'archived': False, 'path': str(folder)}
    if action == 'delete_preview':
        return deletion_preview(base, name, require_scheduler)
    if action == 'delete':
        if confirmation != name or not fingerprint:
            raise ValueError('必须输入完整任务名确认删除。')
        current = deletion_preview(base, name, require_scheduler)
        if current['fingerprint'] != fingerprint:
            raise ValueError('确认期间任务文件已变化，请重新查看后再决定。')
        # Recheck the exact absolute target immediately before recursive deletion.
        target = task_target(base, name)
        if target != folder or target.parent != Path(base).resolve():
            raise ValueError('删除目标路径变化，未删除。')
        shutil.rmtree(target)
        return {'deleted': True, 'path': str(target)}
    raise ValueError('未知任务管理操作。')


def wall_seconds(value):
    if not re.fullmatch(r'\d{1,3}:[0-5]\d:[0-5]\d', value):
        raise ValueError('运行时间必须为 HH:MM:SS。')
    h, m, s = map(int, value.split(':'))
    seconds = h * 3600 + m * 60 + s
    if seconds <= 0:
        raise ValueError('运行时间必须大于零。')
    return seconds


def validate_scheduler(cfg):
    """Check the two profile values that get interpolated into submit.slurm.

    Both script builders below paste partition and walltime directly into the
    batch script, and the profile they come from is an ordinary local file.
    The newline is the whole point: capacity_preflight parses the finished
    script line by line, so an injected '#SBATCH' line ends the capture and it
    reads back the truncated value that still passes validation, and 'bash -n'
    only checks syntax, which injected whole lines satisfy. This lives in one
    place because the two builders had drifted -- one checked, one did not.
    """
    if not re.fullmatch(r'[A-Za-z0-9_-]+', cfg.get('partition', '')):
        raise ValueError('调度分区无效。')
    wall_seconds(cfg.get('walltime', ''))


def resource_context(folder, phase=None):
    recovery_barrier(folder, phase)
    if phase is not None and phase not in task_phases(folder):
        raise ValueError('无效阶段。')
    record = folder / 'submissions' / (phase + '.json') if phase else folder / 'remote-submission.json'
    if record.exists():
        return {'already_registered': True, **json.loads(record.read_text())}
    if (folder / '.agent.lock').exists() or (folder / '.task-archived.json').exists():
        raise ValueError('运行中或归档任务不能调整提交资源。')
    config = json.loads((folder / 'config.json').read_text())
    script = (folder / 'submit.slurm').read_text()
    values = dict(re.findall(r'^#SBATCH --([\w-]+)=([^\n]+)$', script, re.M))
    resources = dict(parallel_tests=config.get('parallel_tests', 1), cores_per_test=config.get('cores_per_test', 8),
                     nodes=int(values.get('nodes', 1)), partition=values['partition'], walltime=values['time'])
    if 'submission_mode' in config:
        resources['submission_mode'] = config['submission_mode']
    if phase not in (None, 'convergence') and resources.get('submission_mode') == 'array':
        # The existing script still belongs to convergence. The next phase must
        # explicitly regenerate it and be reviewed before it can be submitted.
        resources.update(submission_mode='allocation', parallel_tests=1)
    receipt_path = folder / 'resource-change.json'
    receipt = json.loads(receipt_path.read_text()) if receipt_path.exists() else None
    return {'resource_change': receipt, 'resources': resources, 'phase': phase,
            'execution_tags': execution_tag_status(config, phase), 'execution_plan': {'submission_mode': resources.get('submission_mode', 'allocation'),
            'array_elements': 3 * len(config.get('active_scans', ['encut', 'mesh'])) + (1 if config.get('fixed_encut') is not None else 0) if resources.get('submission_mode') == 'array' else 0,
            'compute': config.get('active_scans', ['encut', 'mesh']),
            'reuse': [k for k in ('encut', 'mesh') if k not in config.get('active_scans', ['encut', 'mesh'])],
            'tests': 3 * len(config.get('active_scans', ['encut', 'mesh'])) + (1 if config.get('fixed_encut') is not None else 0), 'cross_cutoff': config.get('fixed_encut') is not None, 'post_relax_checks': ['higher-cutoff', 'denser-mesh']}, 'signature': hashlib.sha256(
        ((folder / 'config.json').read_text() + script).encode()).hexdigest()}


def plan_fast_resources(folder, phase, limits=None, read=None):
    """Read-only throughput heuristic; no timing claims, mutations or authorization."""
    # Membership in the task's own stages is checked by resource_context below.
    if not is_phase_name(phase):
        raise ValueError('无效阶段。')
    current = resource_context(folder, phase)
    if current.get('already_registered'):
        return {**current, 'planned': False}
    resources = current['resources']
    mode = resources.get('submission_mode', 'allocation')
    if mode not in ('allocation', 'array') or mode == 'array' and phase != 'convergence':
        raise ValueError('数组规划只支持 convergence，提交模式必须为 allocation 或 array。')
    cores = resources['cores_per_test']
    tests = current['execution_plan']['tests'] if phase == 'convergence' else 1
    if (type(cores) is not int or not 1 <= cores <= 65536 or type(tests) is not int
            or tests < 1 or mode == 'array' and tests > MAX_ARRAY_TESTS):
        raise ValueError('任务进程数或测试数量无效。')
    # Without an existing grant, propose at most the current per-test MPI count
    # times the actual independent tests. These bounds never grant permission.
    bounds = dict(limits) if limits is not None else dict(
        nodes=min(256, cores * min(6, tests) if mode == 'array' else cores),
        cores=min(65536, cores * min(6, tests)),
        partition=resources['partition'], walltime=resources['walltime'])
    if bounds['partition'] != resources['partition']:
        raise ValueError('当前任务分区不在资源授权范围内。')
    if wall_seconds(resources['walltime']) > wall_seconds(bounds['walltime']):
        raise ValueError('当前时限超过授权；不会自动缩短时限导致运行中断。')
    # Cache each fixed scheduler query for a coherent and bounded enumeration.
    cache = {}
    reader = read or scheduler_read
    def cached(command):
        key = tuple(command)
        if key not in cache:
            cache[key] = reader(command)
        return cache[key]
    attempts = 0
    max_parallel = min(6, tests, bounds['cores'] // cores)
    if mode == 'array':
        max_parallel = min(max_parallel, bounds['nodes'])
    for parallel in range(max_parallel, 0, -1):
        max_nodes = bounds['nodes'] // parallel if mode == 'array' else bounds['nodes']
        for nodes in range(1, min(256, cores, max_nodes) + 1):
            if cores % nodes:
                continue
            candidate = {**resources, 'parallel_tests': parallel, 'nodes': nodes}
            script = '\n'.join('#SBATCH --' + k + '=' + str(v) for k, v in dict(
                partition=candidate['partition'], nodes=nodes,
                ntasks=cores if mode == 'array' else parallel * cores,
                time=candidate['walltime'], **{'cpus-per-task': 1},
                **({'array': f'0-{tests - 1}%{parallel}'} if mode == 'array' else {})).items())
            check_resources(script, bounds)
            attempts += 1
            try:
                capacity = capacity_preflight(script, read=cached)
            except SubmissionError as exc:
                if exc.error_code == 'SCHEDULER_CAPACITY_EXCEEDED':
                    continue
                raise
            # Ensure the proposal is still tied to the same input/resources.
            if resource_context(folder, phase).get('signature') != current['signature']:
                raise ValueError('规划期间配置发生变化，请重新读取。')
            return {'planned': True, 'objective': 'fastest', 'policy': 'maximize_independent_concurrency',
                'signature': current['signature'], 'resources': candidate, 'capacity': capacity,
                'tests': tests, 'estimated_waves': (tests + parallel - 1) // parallel,
                'candidates_checked': attempts, 'limits': limits,
                'operator_confirmed': False, 'submitted': False, 'optimality_verified': False,
                'rationale': '优先增加独立测试并发，在相同并发下选最少节点；保留当前每组MPI进程数。'
                    '已检查CPU及默认内存容量；没有伸缩实测，不预测排队时间或承诺最快。'
                    '波次数按等长测试估算，实际各网格耗时不同。',
                'next': '展示方案并通过现有统一菜单确认及授权；应用后重新审阅，再提交本阶段。'}
    raise SubmissionError('NO_FEASIBLE_RESOURCE_PLAN',
        '当前每组MPI进程数在授权及容量范围内没有可行布局；未修改、未提交。')


def validate_array_submission(folder, phase, manifest, meta):
    """Bind bounded scheduler elements and their launcher to the verified scan plan."""
    script = (folder / 'submit.slurm').read_text(encoding='utf-8')
    layout = array_layout(script)
    config_path = folder / 'config.json'
    config = json.loads(config_path.read_text(encoding='utf-8')) if config_path.exists() else {}
    mode = config.get('submission_mode', 'allocation')
    if mode not in ('allocation', 'array'):
        raise ValueError('计算配置中的提交模式无效。')
    if mode != 'array':
        if layout is not None or 'run-array-test' in script:
            raise ValueError('数组脚本与计算配置的提交模式不一致，不能提交。')
        return
    if phase != 'convergence' or not manifest.get('stage_gates') or layout is None:
        raise ValueError('数组仅支持有阶段审阅的 convergence，且须有受控数组脚本。')
    if not __package__ and not (Path(engine_release(meta)) / 'convergence_array.py').is_file():
        raise ValueError('任务绑定的引擎不支持数组，请部署最新版并准备新任务。')
    engine = engine_module('convergence_array')
    if getattr(engine, 'ARRAY_EXECUTION_VERSION', 0) < 1:
        raise ValueError('任务绑定的引擎不支持数组，请部署最新版并准备新任务。')
    tests = engine.tests_from_config(config)
    directives = dict(re.findall(r'^#SBATCH --([\w-]+)=([^\n]+)$', script, re.M))
    if (layout['task_count'] != len(tests) or layout['concurrency'] != config.get('parallel_tests')
            or int(directives.get('ntasks', 0)) != config.get('cores_per_test')
            or int(directives.get('nodes', 0)) != config.get('nodes')
            or int(directives.get('cpus-per-task', 0)) != 1):
        raise ValueError('数组范围、并发数或每项资源与计算配置不一致，不能提交。')
    launchers = []
    for line in script.splitlines():
        if line.strip() and not line.lstrip().startswith('#') and 'vasp_band_agent.py' in line:
            tokens = shlex.split(line)
            positions = [i for i, token in enumerate(tokens) if Path(token).name == 'vasp_band_agent.py']
            if len(positions) != 1:
                raise ValueError('数组启动命令无效。')
            position = positions[0]
            if (position + 5 != len(tokens) - 1 or tokens[position + 1] != 'run-array-test'
                    or Path(tokens[position + 2]).resolve() != folder.resolve()
                    or tokens[position + 3:] != ['--', 'srun', 'vasp_std']):
                raise ValueError('数组必须使用受控 run-array-test 启动当前任务。')
            launchers.append(tokens)
    if len(launchers) != 1 or 'export OMP_NUM_THREADS=1' not in script:
        raise ValueError('数组需要唯一受控启动行和 OMP_NUM_THREADS=1。')


def scheduler_query(command, consequence):
    """One strict squeue/sacct read: any failure refuses, naming what it blocks."""
    try:
        return subprocess.run(command, capture_output=True, text=True, timeout=20, check=True).stdout
    except subprocess.CalledProcessError as exc:
        detail = (exc.stderr or exc.stdout or '').strip()[-1200:]
        raise ValueError(f'{command[0]} 查询失败（退出码 {exc.returncode}）：{detail or "未返回诊断信息"}；{consequence}。') from exc
    except subprocess.TimeoutExpired as exc:
        raise ValueError(f'{command[0]} 查询超时；{consequence}。') from exc


def check_previous_job(folder, phase):
    """Check live queue independently of whether Slurm still knows the old ID.

    The previous phase is the one before it in the task's own stage order.
    """
    phases = task_phases(folder)
    if phase not in phases:
        raise ValueError('无效阶段。')
    if phase == phases[0]:
        return
    previous = folder / 'submissions' / (phases[phases.index(phase) - 1] + '.json')
    if not previous.exists():
        return  # Direct execution in an allocation is checked by stage evidence.
    receipt = json.loads(previous.read_text(encoding='utf-8'))
    job_id = str(receipt.get('job_id', ''))
    if not re.fullmatch(r'[0-9]+', job_id):
        raise ValueError('上阶段提交结果不明确，请人工核对。')
    def query(command):
        return scheduler_query(command, '未确认上阶段退出，禁止提交')
    # A completed job ID may make `squeue -j ID` exit 1. Query this user's
    # live jobs instead, then require the exact parent accounting row below.
    queued = query(['squeue', '--me', '-h', '-o', '%i'])
    if any(line.strip() == job_id or line.strip().startswith(job_id + '_') for line in queued.splitlines()):
        raise ValueError('上阶段作业尚未退出，请稍后再提交。')
    accounting = query(['sacct', '-n', '-j', job_id,
                        '--format=JobID,State,ExitCode' if receipt.get('submission_mode') == 'array' else '--format=JobIDRaw,State,ExitCode', '-P'])
    if receipt.get('submission_mode') == 'array':
        summary = array_scheduler_status(receipt, queued, accounting)
        if not summary['all_successful']:
            raise ValueError('尚不能确认全部数组测试正常退出；存在未完成、失败或缺失记账，禁止继续提交。')
        return
    rows = [[field.strip() for field in line.split('|')] for line in accounting.splitlines()]
    if not any(len(row) >= 3 and row[:3] == [job_id, 'COMPLETED', '0:0'] for row in rows):
        raise ValueError('尚不能确认上阶段正常退出；请检查队列记录，禁止继续提交。')


PHASES = LEGACY_PHASES
# Files a phase writes at the task root once its run has started. Their presence
# means the cancelled job did real work, which must never be silently reused.
# Legacy tasks; a task recording its stages takes them from its definitions.
PHASE_OUTPUTS = {'convergence': ('convergence.json', 'convergence-array.json'),
                 'relaxation': ('relaxation.json', 'relaxation-diagnostic.json', 'relaxed-1.POSCAR', 'relaxed-2.POSCAR',
                                'geometry-summary.json', 'post-relax-validation.json'),
                 'scf': ('scf-summary.json',),
                 'bands': ('report.json', 'bands.csv', 'bands.png', 'bands.pdf')}


def phase_outputs(folder, phase):
    config = task_config(folder)
    definitions = stage_definitions(config)
    return PHASE_OUTPUTS[phase] if definitions is None else definitions.phase_outputs(config, phase)


def kept_stage_inputs(folder, phase):
    """Inputs a stage folder of phase receives besides STAGE_INPUTS (bands: the SCF charge density)."""
    config = task_config(folder)
    definitions = stage_definitions(config)
    if definitions is None:
        return ('CHGCAR',) if phase == 'bands' else ()
    return definitions.kept_inputs(config, phase)


def submitted_receipt(folder, phase):
    """The phase's registered receipt and its strict JobID, or a refusal.

    phase None means the checkpoint's current phase for a staged task, or the
    single legacy remote-submission.json for an unstaged one.
    """
    manifest = folder / 'manifest.json'
    staged = json.loads(manifest.read_text()).get('stage_gates', False) if manifest.is_file() else False
    if phase is None and staged:
        checkpoint = folder / 'checkpoint.json'
        phase = json.loads(checkpoint.read_text())['next'] if checkpoint.is_file() else task_phases(folder)[0]
    if phase is not None and phase not in task_phases(folder):
        raise ValueError('无效阶段。')
    if staged != (phase is not None):
        raise ValueError('阶段式任务需指定阶段；旧任务不带阶段。')
    record = folder / 'submissions' / (phase + '.json') if phase else folder / 'remote-submission.json'
    if record.is_symlink() or not record.is_file():
        raise ValueError(f'阶段 {phase or "完整流程"} 没有提交回执，没有可取消的作业。')
    receipt = json.loads(record.read_text(encoding='utf-8'))
    job_id = str(receipt.get('job_id') or '')
    if not re.fullmatch(r'[0-9]+', job_id):
        raise ValueError('提交回执没有明确的 JobID；不取消、不释放，请人工核对调度记录。')
    return phase, record, receipt, job_id


def record_event(folder, action, **details):
    task_release(json.loads((folder / 'remote-job.json').read_text()))
    engine_module('vasp_band_agent').event(folder, action, **details)


def cancel_job(folder, phase=None, confirm_job_id=None):
    """scancel the JobID a receipt names. Without the typed JobID it only previews.

    The receipt stays in place: a cancel request is not proof the job ended, and
    the phase stays locked until release_cancelled_submission confirms it did.
    """
    phase, _, receipt, job_id = submitted_receipt(folder, phase)
    preview = {'phase': phase, 'job_id': job_id, 'receipt_status': receipt.get('status'),
               'submission_mode': receipt.get('submission_mode', 'allocation')}
    if confirm_job_id is None:
        return {**preview, 'preview': True, 'cancel_requested': False}
    if str(confirm_job_id) != job_id:
        raise ValueError('输入的作业编号与提交回执不一致，未取消。')
    try:
        result = subprocess.run(['scancel', job_id], capture_output=True, text=True, timeout=30)
    except subprocess.TimeoutExpired as exc:
        raise SubmissionError('CANCEL_UNCERTAIN', 'scancel 超时；取消是否生效未知，请用 /jobs 或任务状态核对，不要据此重新提交。') from exc
    except OSError as exc:
        raise ValueError(f'无法执行 scancel：{exc}') from exc
    if result.returncode:
        detail = diagnostic_text(result.stderr or result.stdout).strip()
        raise ValueError(f'scancel 失败（退出码 {result.returncode}）：{detail or "未返回诊断信息"}')
    record_event(folder, 'cancellation_requested', job_id=job_id, phase=phase)
    return {**preview, 'cancel_requested': True, 'stderr': diagnostic_text(result.stderr).strip(),
            'next': '已发送取消请求；作业退出且记账为 CANCELLED 后，可由操作者释放该阶段提交记录并重新确认资源与授权。'}


def started_outputs(folder, phase, stage_gate):
    """Paths showing the phase already ran: anything beyond its prepared inputs."""
    prepared = set(json.loads((folder / 'manifest.json').read_text()).get('hashes', {}))
    plan = json.loads((folder / 'input-plan.json').read_text(encoding='utf-8'))
    found = [name for name in phase_outputs(folder, phase) if (folder / name).exists()]
    for name in plan['incars']:
        if stage_gate.phase_of(name) != phase:
            continue
        stage = folder / name
        if stage.is_symlink():
            found.append(name)
        elif stage.is_dir():
            for path in sorted(stage.rglob('*')):
                relative = path.relative_to(folder).as_posix()
                if not path.is_dir() and relative not in prepared:
                    found.append(relative)
    state = folder / 'workflow-state.json'
    if state.is_file() and json.loads(state.read_text(encoding='utf-8')).get('status') in ('running', 'failed'):
        found.append('workflow-state.json')
    return found


def release_cancelled_submission(folder, phase, confirm_job_id):
    """Archive a phase receipt whose job Slurm records as CANCELLED, unlocking the phase.

    Only for a job that never produced output: what a cancelled run left behind
    is unverified and the workflow would pick it up, so any trace of a started
    run refuses and the operator forks a new task instead. Nothing is resubmitted
    here; the phase goes back through resource review, stage review and a fresh
    operator grant like any first submission.
    """
    if phase not in task_phases(folder):
        raise ValueError('释放提交记录需要明确的阶段。')
    phase, record, receipt, job_id = submitted_receipt(folder, phase)
    if str(confirm_job_id) != job_id:
        raise ValueError('输入的作业编号与提交回执不一致，未释放。')
    if receipt.get('status') != 'submitted' or receipt.get('submission_mode') == 'array':
        raise ValueError('仅支持已确认 JobID 的普通（非数组）提交；其他记录请新建任务。')
    if (folder / '.task-archived.json').exists():
        raise ValueError('任务已归档，请先恢复。')
    task_release(json.loads((folder / 'remote-job.json').read_text()))
    stage_gate = engine_module('stage_gate')
    if stage_gate.current(folder) != phase:
        raise ValueError('该阶段已不是当前待执行阶段（可能已完成），不释放提交记录。')
    archived = folder / 'submissions' / f'{phase}.cancelled-{job_id}.json'
    lock = submission_lock(folder, '提交或恢复正在登记，禁止并行操作。')
    try:
        if archived.exists():
            raise ValueError('已有同名取消归档，不覆盖。')
        consequence = '未确认作业已取消退出，不释放提交记录'
        queued = scheduler_query(['squeue', '--me', '-h', '-o', '%i'], consequence)
        if any(line.strip() == job_id or line.strip().startswith(job_id + '_') for line in queued.splitlines()):
            raise ValueError('作业仍在队列中（可能仍在排队、运行或正在退出），不释放；请稍后再试。')
        accounting = scheduler_query(['sacct', '-n', '-j', job_id, '--format=JobIDRaw,State,ExitCode', '-P'], consequence)
        states = [row[1].strip() for row in (line.split('|') for line in accounting.splitlines())
                  if len(row) >= 3 and row[0].strip() == job_id]
        if len(states) != 1 or states[0].split()[:1] != ['CANCELLED']:
            raise ValueError(f'调度记账未显示作业 {job_id} 为 CANCELLED（记录：{", ".join(states) or "无"}）；'
                             '只有确认取消的作业才能释放，完成、失败或运行中的作业不释放。')
        if (folder / '.agent.lock').exists():
            raise ValueError('任务目录留有运行锁，说明被取消的作业已开始计算；部分输出未经验证，'
                             '不能在本任务中重算。请基于已通过的阶段新建任务（fork）后按新资源提交。')
        partial = started_outputs(folder, phase, stage_gate)
        if partial:
            raise ValueError('被取消的作业已留下本阶段输出（' + ', '.join(partial[:8]) +
                             (' 等' if len(partial) > 8 else '') + '）；部分输出未经验证，不能静默复用或覆盖。'
                             '请基于已通过的阶段新建任务（fork）后按新资源提交。')
        os.replace(record, archived)
    finally:
        lock.close()
    relative = archived.relative_to(folder).as_posix()
    record_event(folder, 'cancelled_submission_released', job_id=job_id, phase=phase,
                 archived_receipt=relative, scheduler_state=states[0])
    return {'released': True, 'phase': phase, 'job_id': job_id, 'scheduler_state': states[0],
            'archived_receipt': relative, 'submission_authorized': False,
            'next': '提交记录已归档；该阶段需重新确认资源、按新快照重新审阅，并通过操作者菜单重新授权后才能提交。'}


# Slurm end states after which a phase may continue from its completed stages.
# COMPLETED never needs it and FAILED needs a diagnosis, not a rerun.
RESUMABLE_STATES = ('TIMEOUT', 'CANCELLED', 'NODE_FAIL', 'PREEMPTED')
# The resume protocol. Recorded with each resume, and checked locally so a client
# that expects it refuses a worker without it; bump when the record changes.
PHASE_RESUME_VERSION = 1
# Stage files written before launch (full_workflow.stage); everything else in a
# stage folder is run output. bands also receives the SCF charge density as input.
STAGE_INPUTS = ('POSCAR', 'INCAR', 'KPOINTS', 'POTCAR', 'inputs.json', 'execution.json')


def verified_stage(folder, name, plan, config):
    """Hashes of a completed stage, after checking it against its record and the reviewed plan."""
    from pymatgen.io.vasp.inputs import Incar
    stage = folder / name
    stamp = json.loads((stage / 'complete.json').read_text(encoding='utf-8'))
    if not stamp.get('xml_sha256') or not (stage / 'vasprun.xml').is_file() or file_sha(stage / 'vasprun.xml') != stamp['xml_sha256']:
        raise ValueError(f'{name} 的输出与完成记录不一致，不能复用；请人工检查，不续算。')
    if name == 'scf' and (not stamp.get('charge_sha256') or not (stage / 'CHGCAR').is_file()
                          or file_sha(stage / 'CHGCAR') != stamp['charge_sha256']):
        raise ValueError('scf 电荷文件与完成记录不一致，不能复用。')
    if not (stage / 'inputs.json').is_file():
        raise ValueError(f'{name} 缺少 inputs.json，无法核对输入，不能复用。')
    inputs = json.loads((stage / 'inputs.json').read_text(encoding='utf-8'))
    for filename, expected in inputs.items():
        if not (stage / filename).is_file() or file_sha(stage / filename) != expected:
            raise ValueError(f'{name}/{filename} 与 inputs.json 不一致，不能复用。')
    if file_sha(stage / 'POTCAR') != file_sha(folder / 'POTCAR'):
        raise ValueError(f'{name}/POTCAR 与任务 POTCAR 不一致，不能复用。')
    # The reviewed plan: the physics INCAR exactly, plus only the recorded execution tags.
    data = dict(Incar.from_file(stage / 'INCAR'))
    tags = {key: data.pop(key) for key in ('KPAR', 'NCORE') if key in data}
    if name not in plan['incars'] or data != dict(Incar(plan['incars'][name])):
        raise ValueError(f'{name} 的 INCAR 与审阅方案不一致，不能复用。')
    if tags and (config.get('execution_tags') is None or not (stage / 'execution.json').is_file()
                 or json.loads((stage / 'execution.json').read_text(encoding='utf-8')).get('tags') != tags):
        raise ValueError(f'{name} 的并行标签与执行记录不一致，不能复用。')
    recorded = plan.get('kpoints', {}).get(name)
    if recorded is not None and (stage / 'KPOINTS').read_text(encoding='utf-8') != recorded:
        raise ValueError(f'{name} 的 KPOINTS 与审阅方案不一致，不能复用。')
    names = {'complete.json', 'vasprun.xml', 'inputs.json', *inputs}
    names |= {'execution.json'} if (stage / 'execution.json').is_file() else set()
    names |= {'CHGCAR'} if name == 'scf' else set()
    return {filename: file_sha(stage / filename) for filename in sorted(names)}


def check_resume(folder):
    """A resumed phase's reused stages still hash to what the operator confirmed.

    Run by the worker before each stage review and submission, so a task bound
    to a release older than the resume record gets the same check as a new one.
    The record lives in checkpoint.json, which every review snapshot includes,
    so the record itself cannot change without invalidating the review.
    """
    path = folder / 'checkpoint.json'
    if not path.is_file():
        return None
    checkpoint = json.loads(path.read_text(encoding='utf-8'))
    resume = checkpoint.get('resume')
    if not resume or resume.get('phase') != checkpoint.get('next'):
        return None
    for name, hashes in resume['reused'].items():
        for filename, expected in hashes.items():
            target = folder / name / filename
            if target.is_symlink() or not target.is_file() or file_sha(target) != expected:
                raise ValueError(f'续算复用的 {name}/{filename} 在续算确认后发生变化；请人工检查，不能审阅或提交。')
    return resume


def prepare_phase_resume(folder, phase, confirm_job_id=None):
    """Resume a phase whose job Slurm ended early, reusing every verified stage.

    Without confirm_job_id this only reports what would happen. With it -- the
    operator's confirmation -- interrupted stages' outputs are moved (never
    deleted) to interrupted/<jobid>/, the receipt is archived and the phase goes
    back to awaiting_review. Nothing is submitted: the phase needs a new stage
    review, resource confirmation and operator grant like any submission.
    """
    if phase not in task_phases(folder):
        raise ValueError('续算需要明确的阶段。')
    phase, record, receipt, job_id = submitted_receipt(folder, phase)
    if confirm_job_id is not None and str(confirm_job_id) != job_id:
        raise ValueError('输入的作业编号与提交回执不一致，未续算。')
    if receipt.get('status') != 'submitted' or receipt.get('submission_mode') == 'array':
        raise ValueError('仅支持已确认 JobID 的普通（非数组）提交；其他记录请人工核对。')
    if (folder / '.task-archived.json').exists():
        raise ValueError('任务已归档，请先恢复。')
    task_release(json.loads((folder / 'remote-job.json').read_text()))
    stage_gate = engine_module('stage_gate')
    if stage_gate.current(folder) != phase:
        raise ValueError('该阶段已不是当前待执行阶段（可能已完成），不续算。')
    lock = submission_lock(folder, '提交或恢复正在登记，禁止并行操作。')
    try:
        consequence = '未确认作业已结束，不续算'
        queued = scheduler_query(['squeue', '--me', '-h', '-o', '%i'], consequence)
        live = {line.strip().split('_')[0] for line in queued.splitlines() if line.strip()}
        if job_id in live:
            raise ValueError('作业仍在队列中（可能仍在排队、运行或正在退出），不续算；请稍后再试。')
        for other in sorted((folder / 'submissions').glob('*.json')):
            other_id = str(json.loads(other.read_text(encoding='utf-8')).get('job_id') or '')
            if other_id in live:
                raise ValueError(f'该任务的另一个作业 {other_id} 仍在队列中，不续算。')
        accounting = scheduler_query(['sacct', '-n', '-j', job_id, '--format=JobIDRaw,State,ExitCode', '-P'], consequence)
        states = [row[1].strip() for row in (line.split('|') for line in accounting.splitlines())
                  if len(row) >= 3 and row[0].strip() == job_id]
        state = states[0].split()[0] if len(states) == 1 and states[0].split() else None
        if state not in RESUMABLE_STATES:
            raise ValueError(f'调度记账未显示作业 {job_id} 为 {"/".join(RESUMABLE_STATES)} 之一（记录：{", ".join(states) or "无"}）；'
                             '只有被时限、取消、节点故障或抢占中断的作业才能续算。COMPLETED 无需续算，FAILED 需先诊断。')
        state_path = folder / 'workflow-state.json'
        workflow = json.loads(state_path.read_text(encoding='utf-8')) if state_path.is_file() else {}
        if workflow.get('status') == 'failed':
            raise ValueError('工作流记录为 failed（' + str(workflow.get('error', ''))[:300] + '）；失败需先诊断，不续算。')
        archive = folder / 'interrupted' / job_id
        archived_receipt = folder / 'submissions' / f'{phase}.{state.lower()}-{job_id}.json'
        if archive.exists() or archive.is_symlink() or archived_receipt.exists():
            raise ValueError(f'已有该作业的中断归档（{archive.relative_to(folder).as_posix()}），不覆盖；请人工检查。')
        checkpoint_path = folder / 'checkpoint.json'
        checkpoint = json.loads(checkpoint_path.read_text(encoding='utf-8')) if checkpoint_path.is_file() else {'next': phase}
        config = json.loads((folder / 'config.json').read_text(encoding='utf-8'))
        plan = json.loads((folder / 'input-plan.json').read_text(encoding='utf-8'))
        outputs, kept_extra = phase_outputs(folder, phase), kept_stage_inputs(folder, phase)
        reused, rerun, moves = {}, [], []
        for name in (name for name in plan['incars'] if stage_gate.phase_of(name) == phase):
            stage = folder / name
            if stage.is_symlink():
                raise ValueError(f'{name} 是链接，不续算。')
            if (stage / 'complete.json').is_file():
                reused[name] = verified_stage(folder, name, plan, config)
                continue
            rerun.append(name)
            if not stage.is_dir():
                continue
            if (stage / 'inputs.json').is_file():
                # The stage reruns from the inputs it was launched with; they must be intact.
                for filename, expected in json.loads((stage / 'inputs.json').read_text(encoding='utf-8')).items():
                    if not (stage / filename).is_file() or file_sha(stage / filename) != expected:
                        raise ValueError(f'{name}/{filename} 与 inputs.json 不一致；中断阶段的输入已改变，不续算。')
            kept = STAGE_INPUTS + kept_extra
            moves += [(path, archive / name / path.name) for path in sorted(stage.iterdir()) if path.name not in kept]
        # The phase's root summaries come from its stages' validated output and are
        # rewritten from the reused stages when the phase runs again.
        moves += [(folder / path, archive / 'root' / path) for path in outputs if (folder / path).exists()]
        # Upstream evidence must still be exactly what the phase was reviewed on.
        upstream = {key: value for key, value in stage_gate.evidence(folder, phase)['hashes'].items()
                    if key not in outputs}
        if checkpoint.get('evidence_hashes', {}) != upstream:
            raise ValueError('上游阶段证据与检查点记录不一致；请人工检查，不续算。')
        lock_file = folder / '.agent.lock'
        stale_lock = lock_file.exists() or lock_file.is_symlink()
        summary = {'phase': phase, 'job_id': job_id, 'scheduler_state': state, 'reused': sorted(reused),
                   'rerun': rerun, 'archive': archive.relative_to(folder).as_posix(),
                   'archived': sorted(target.relative_to(archive).as_posix() for _, target in moves),
                   'stale_lock': stale_lock, 'resume_version': PHASE_RESUME_VERSION,
                   # Who re-verifies the reused stages: the worker always (check_resume);
                   # an engine with the marker also at review and when the new job starts.
                   'reuse_checked_by': ['worker', 'engine'] if getattr(stage_gate, 'RESUME_VERSION', 0) >= 1 else ['worker'],
                   'submission_authorized': False}
        if confirm_job_id is None:
            return {**summary, 'preview': True, 'prepared': False}
        archive.mkdir(parents=True)
        for source, target in moves:
            target.parent.mkdir(parents=True, exist_ok=True)
            os.replace(source, target)
        if stale_lock:
            # The job is confirmed ended and no other job of this task is queued,
            # so no process can still hold the lock; it is kept as evidence.
            os.replace(lock_file, archive / '.agent.lock')
        if state_path.is_file():
            shutil.copy2(state_path, archive / 'workflow-state.json')
        resume = {'version': PHASE_RESUME_VERSION, 'phase': phase, 'job_id': job_id, 'scheduler_state': state,
                  'archive': summary['archive'], 'reused': reused, 'rerun': rerun,
                  'stale_lock_archived': stale_lock, 'time': datetime.now(timezone.utc).isoformat()}
        write_json(archive / 'resume.json', resume)
        write_json(checkpoint_path, {**checkpoint, 'evidence_hashes': checkpoint.get('evidence_hashes', {}), 'resume': resume})
        write_json(state_path, {'status': 'awaiting_review', 'completed': checkpoint.get('completed', []),
                                'next': phase, 'resumed_from_job': job_id})
        os.replace(record, archived_receipt)
    finally:
        lock.close()
    relative = archived_receipt.relative_to(folder).as_posix()
    record_event(folder, 'phase_resume_prepared', job_id=job_id, phase=phase, scheduler_state=state,
                 reused=sorted(reused), rerun=rerun, archive=summary['archive'],
                 archived_receipt=relative, stale_lock_archived=stale_lock)
    return {**summary, 'prepared': True, 'archived_receipt': relative,
            'next': '提交记录已归档；本阶段需重新审阅（快照包含复用阶段的哈希），重新确认资源（可改时限）并经操作者重新授权后才能提交。'}


def configure_resources(folder, phase, resources, signature, limits, operation_id=None):
    if operation_id is not None and not re.fullmatch(r'[a-f0-9]{32}', operation_id):
        raise ValueError('资源操作编号无效。')
    current = resource_context(folder, phase)
    if current.get('already_registered') or current['signature'] != signature:
        raise ValueError('任务已提交或资源配置已变化，请重新检查。')
    core = engine_module('vasp_band_agent')
    core.verify(folder)
    manifest = json.loads((folder / 'manifest.json').read_text())
    if manifest.get('stage_gates'):
        stage_gate = engine_module('stage_gate')
        if stage_gate.current(folder) != phase:
            raise ValueError('只能配置当前待提交阶段。')
        check_previous_job(folder, phase)
    elif (folder / 'submission.json').exists() or (folder / 'remote-submission.json').exists():
        raise ValueError('旧任务已有提交记录，不能修改。')
    parallel, cores = resources['parallel_tests'], resources['cores_per_test']
    mode = resources.get('submission_mode', 'allocation')
    if mode not in ('allocation', 'array'):
        raise ValueError('提交模式必须为 allocation 或 array。')
    if mode == 'array' and (phase != 'convergence' or not manifest.get('stage_gates')):
        raise ValueError('数组只支持阶段式 convergence 收敛任务。')
    if type(parallel) is not int or type(cores) is not int or not 1 <= parallel <= (6 if phase in (None, 'convergence') else 1) or not 1 <= cores <= 65536 or parallel * cores > 65536:
        raise ValueError('并发数或核数不在当前引擎支持范围内。')
    if type(resources['nodes']) is not int or not 1 <= resources['nodes'] <= 256 or cores % resources['nodes'] or not re.fullmatch(r'[A-Za-z0-9_-]+', resources['partition']):
        raise ValueError('节点数须为 1–256，每组进程数必须为节点数的整数倍，分区名称须有效。')
    if resources['nodes'] > 1:
        full_workflow = engine_module('full_workflow')
        if getattr(full_workflow, 'RESOURCE_LAYOUT_VERSION', 0) < 2:
            raise ValueError('该任务绑定的旧计算引擎不支持多节点，请部署新版并准备新任务。')
    script = (folder / 'submit.slurm').read_text()
    if 'srun vasp_std' not in script:
        raise ValueError('此脚本不使用受控 srun vasp_std 命令，不能自动配置并行。')
    config = json.loads((folder / 'config.json').read_text())
    count = None
    if mode == 'array':
        meta_path = folder / 'remote-job.json'
        if not __package__ and meta_path.exists() and not (Path(engine_release(json.loads(meta_path.read_text()))) / 'convergence_array.py').is_file():
            raise ValueError('该任务绑定的旧引擎不支持作业数组，请部署新版并准备新任务；不更换已有任务的引擎。')
        try:
            array_engine = engine_module('convergence_array')
        except ImportError as exc:
            raise ValueError('请部署支持作业数组的最新版并准备新任务。') from exc
        if getattr(array_engine, 'ARRAY_EXECUTION_VERSION', 0) < 1:
            raise ValueError('请部署支持作业数组的最新版并准备新任务。')
        count = len(array_engine.tests_from_config(config))
        if not 1 <= count <= MAX_ARRAY_TESTS or parallel > min(MAX_ARRAY_CONCURRENCY, count):
            raise ValueError('数组测试数量或并发数无效。')
    changes = {'nodes': resources['nodes'], 'ntasks': cores if mode == 'array' else parallel * cores, 'cpus-per-task': 1,
               'partition': resources['partition'], 'time': resources['walltime']}
    if mode == 'array' or resources['nodes'] > 1 or '--ntasks-per-node=' in script:
        changes['ntasks-per-node'] = (cores if mode == 'array' else parallel * cores) // resources['nodes']
    if mode == 'array':
        changes['array'] = f'0-{count - 1}%{parallel}'
        changes['output'] = 'slurm-%A_%a.log'
    elif re.search(r'^#SBATCH --array=', script, re.M):
        script = re.sub(r'^#SBATCH --array=.*\n?', '', script, flags=re.M)
        changes['output'] = 'slurm-%j.log'
    if mode == 'array' or config.get('submission_mode') == 'array':
        lines, rewritten = [], 0
        for line in script.splitlines():
            if 'vasp_band_agent.py' in line and 'srun vasp_std' in line:
                tokens = shlex.split(line)
                positions = [i for i, token in enumerate(tokens) if Path(token).name == 'vasp_band_agent.py']
                if len(positions) != 1 or tokens[positions[0] + 1] not in ('run', 'run-array-test'):
                    raise ValueError('计算启动行无法安全转换为数组模式。')
                tokens[positions[0] + 1] = 'run-array-test' if mode == 'array' else 'run'
                line, rewritten = shlex.join(tokens), rewritten + 1
            lines.append(line)
        if rewritten != 1:
            raise ValueError('数组需要唯一受控 vasp_band_agent.py 启动行。')
        script = '\n'.join(lines) + '\n'
    for key, value in changes.items():
        pattern = r'^#SBATCH --' + key + r'=[^\n]+$'
        if re.search(pattern, script, re.M):
            script = re.sub(pattern, lambda m: '#SBATCH --' + key + '=' + str(value), script, flags=re.M)
        else:
            script = script.replace('\n', '\n#SBATCH --' + key + '=' + str(value) + '\n', 1)
    if 'export OMP_NUM_THREADS=1' not in script:
        raise ValueError('提交脚本必须显式设置 OMP_NUM_THREADS=1。')
    check_resources(script, limits)
    changed = script != (folder / 'submit.slurm').read_text() or config.get('submission_mode', 'allocation') != mode or any(config.get(k, 1) != resources[k] for k in ('parallel_tests', 'cores_per_test', 'nodes'))
    if changed:
        config.update(parallel_tests=parallel, cores_per_test=cores, nodes=resources['nodes'])
        if mode == 'array' or 'submission_mode' in config or 'submission_mode' in resources:
            config['submission_mode'] = mode
        write_json(folder / 'config.json', config)
        (folder / 'submit.slurm').write_text(script)
        manifest['hashes']['config.json'] = sha(folder / 'config.json')
        write_json(folder / 'manifest.json', manifest)
        meta_path = folder / 'remote-job.json'
        if meta_path.exists():
            meta = json.loads(meta_path.read_text())
            meta.update(script_sha256=sha(folder / 'submit.slurm'), resources={**resources,
                'ntasks': cores if mode == 'array' else parallel * cores,
                **({'array_task_count': count, 'peak_cpus': parallel * cores, 'peak_nodes': parallel * resources['nodes']} if mode == 'array' else {})})
            write_json(meta_path, meta)
    if operation_id:
        write_json(folder / 'resource-change.json', {'operation_id': operation_id, 'phase': phase,
            'signature': resource_context(folder, phase)['signature'], 'changed': changed})
    return {**resource_context(folder, phase), 'changed': changed,
            'note': '资源已保存；提交前必须通过当前快照的审阅。'}


def check_resources(script, limits):
    """Fail closed on ambiguous Slurm directives; inspect actual submitted script."""
    values = {}
    allowed = {'partition', 'nodes', 'ntasks', 'ntasks-per-node', 'cpus-per-task', 'time', 'job-name', 'output', 'error', 'array'}
    for line in script.splitlines():
        if not line.strip().startswith('#SBATCH'):
            continue
        match = re.fullmatch(r'\s*#SBATCH\s+--([a-z-]+)(?:=|\s+)([^\s]+)\s*', line)
        if not match or match[1] not in allowed or match[1] in values:
            raise ValueError('提交脚本包含无法审核的或重复的 Slurm 参数。')
        values[match[1]] = match[2]
    if not {'partition', 'nodes', 'ntasks', 'time'} <= values.keys():
        raise ValueError('提交脚本必须显式声明分区、节点数、任务数和时长。')
    nodes, tasks, cpus = (int(values[k]) for k in ('nodes', 'ntasks', 'cpus-per-task')) if 'cpus-per-task' in values else (int(values['nodes']), int(values['ntasks']), 1)
    if 'ntasks-per-node' in values and int(values['ntasks-per-node']) * nodes != tasks:
        raise ValueError('每节点进程数与总进程数不一致。')
    layout = array_layout(script)
    concurrency = layout['concurrency'] if layout else 1
    if (values['partition'] != limits['partition'] or min(nodes, tasks, cpus) < 1
            or nodes * concurrency > limits['nodes'] or tasks * cpus * concurrency > limits['cores']
            or wall_seconds(values['time']) > wall_seconds(limits['walltime'])):
        raise ValueError('任务超出授权分区或资源上限，请使用 /permissions 调整。')


def _environment_check(payload, cfg, root):
    return inspect_environment(cfg, payload.get('relax_mode', 'cell'))


def _software_inventory(payload, cfg, root):
    """Read-only, bounded probe of VASP builds and tools (worker_inventory); writes nothing."""
    roots = payload.get('roots') or list(DEFAULT_ROOTS)
    if not isinstance(roots, list) or len(roots) > MAX_ROOTS or any(not isinstance(r, str) for r in roots):
        raise ValueError('探测根目录须为字符串列表，最多 8 个。')
    for value in roots:
        expand_root(value)
    return software_inventory(cfg.get('vasp_env'), root, roots)


def _environment_profile_check(payload, cfg, root):
    """Read-only fingerprint of one environment script, for the registration menu."""
    script = payload.get('env_script')
    if not isinstance(script, str) or not Path(script).is_absolute() or any(c in script for c in '\n\x00'):
        raise ValueError('环境脚本须为远程绝对路径。')
    return {**environment_evidence(script), 'profiles_version': ENVIRONMENT_PROFILES_VERSION}


def _list_jobs(payload, cfg, root):
    return list_workflow_jobs(root / 'jobs', payload.get('include_archived', False))


def _manage_task(payload, cfg, root):
    return manage_task(root / 'jobs', payload['job_name'], payload['operation'],
        confirmation=payload.get('confirmation'), fingerprint=payload.get('fingerprint'))


def release_version(sources):
    """The release name a deploy of these sources gets; the local side computes it too."""
    return hashlib.sha256(json.dumps(sources, sort_keys=True).encode()).hexdigest()[:16]


def _deployment_info(payload, cfg, root):
    """Read-only: which release new tasks would be prepared against."""
    path = root / 'deployment.json'
    if not path.is_file():
        return {'deployed': False, 'version': None, 'release': None}
    data = json.loads(path.read_text())
    return {'deployed': True, 'version': data.get('version'), 'release': data.get('release')}


def _deploy(payload, cfg, root):
    sources = payload['sources']
    if set(sources) != set(SOURCE_FILES):
        raise ValueError('部署文件不在白名单。')
    version = release_version(sources)
    release = root / 'releases' / version
    release.mkdir(parents=True, exist_ok=True)
    for name, encoded in sources.items():
        data = base64.b64decode(encoded, validate=True)
        target = release / name
        target.parent.mkdir(parents=True, exist_ok=True)
        if target.exists() and target.read_bytes() != data:
            raise ValueError('远程版本内容冲突。')
        target.write_bytes(data)
    # Reuse a configured scientific Python environment; never install into it.
    check = subprocess.run([cfg['python'], '-c', 'import pymatgen.io.vasp, matplotlib, numpy'],
                           capture_output=True, text=True, timeout=180)
    if check.returncode:
        raise ValueError('所配置 Python 缺少计算依赖。请提供独立已安装 pymatgen/matplotlib/numpy 的 Python 路径。' + check.stderr[-2000:])
    write_json(root / 'deployment.json', {'release': str(release), 'version': version})
    return {'deployed': True, 'release': str(release), 'python': cfg['python']}


def _convergence_reuse(root):
    """Activate the current deployment and load its convergence-reuse engine."""
    release = deployed_release(root)
    if not (release / 'convergence_reuse.py').is_file():
        raise ValueError('请先部署支持收敛复用的最新版。')
    activate_release(release)
    convergence_reuse = engine_module('convergence_reuse')
    return release, convergence_reuse


def _inspect_convergence(payload, cfg, root, name, folder):
    _, convergence_reuse = _convergence_reuse(root)
    return convergence_reuse.inspect(task_target(root / 'jobs', name))


def _prepare_from_convergence(payload, cfg, root, name, folder, production):
    release, convergence_reuse = _convergence_reuse(root)
    source = task_target(root / 'jobs', payload['source_job'])
    if folder.exists():
        raise ValueError('重试必须使用不存在的新任务目录。')
    if not Path(cfg['vasp_env']).is_file():
        raise ValueError('VASP 环境脚本不存在。')
    if production and getattr(convergence_reuse, 'PRODUCTION_MESH_VERSION', 0) < 2:
        raise ValueError('当前引擎不支持指定后续网格，请先部署新版；不会静默使用最高网格。')
    if (production and payload.get('encut') is not None
            and getattr(convergence_reuse, 'PRODUCTION_ENCUT_VERSION', 0) < 1):
        raise ValueError('当前引擎不支持选择后续ENCUT，请先部署新版；不会静默使用最高ENCUT。')
    source_config = json.loads((source / 'config.json').read_text())
    if (source_config.get('execution_tags') is not None
            and getattr(engine_module('full_workflow'), 'EXECUTION_TAGS_VERSION', 0) < source_config['execution_tags']):
        raise ValueError('来源任务使用并行标签，当前部署引擎不支持；请先部署新版，不会去掉标签继续。')
    cfg, environment = fork_environment(cfg, source_config)
    previous_path = source / 'environment-review.json'
    if not previous_path.exists():
        raise ValueError('来源任务缺少环境指纹，不能证明跨环境收敛复用有效；请重新扫描。')
    previous = json.loads(previous_path.read_text())
    if previous.get('executable_sha256') != environment['executable_sha256']:
        raise ValueError('来源任务VASP可执行文件不同，不能复用收敛证据。')
    # Before the expensive part: a profile that cannot produce a valid batch
    # script should not spend minutes building a job directory first.
    validate_scheduler(cfg)
    with contextlib.redirect_stdout(io.StringIO()):
        if payload.get('fixed_encut') is not None and getattr(convergence_reuse, 'FIXED_ENCUT_VERSION', 0) < 1:
            raise ValueError('当前部署引擎不支持指定固定ENCUT及交叉复核，请先部署新版；不会退回最高值。')
        kwargs = {'fixed_encut': payload['fixed_encut']} if payload.get('fixed_encut') is not None else {}
        if payload.get('scan_thresholds'):
            if getattr(engine_module('full_workflow'), 'SCAN_THRESHOLDS_VERSION', 0) < 1:
                raise ValueError('当前部署引擎不支持操作者扫描阈值，请先部署新版；不会按默认阈值静默执行。')
            kwargs['scan_thresholds'] = payload['scan_thresholds']
        if payload.get('scan_preset'):
            if getattr(engine_module('full_workflow'), 'SCAN_THRESHOLDS_VERSION', 0) < 2:
                raise ValueError('当前部署引擎不支持扫描判据档位，请先部署新版；不会按严格档静默执行。')
            kwargs['scan_preset'] = payload['scan_preset']
        if production:
            # Hold the source job's lock across the whole read: this reads the
            # source's evidence and writes into the new folder, and a submit or
            # recovery on the source concurrently would invalidate both. The
            # bare O_EXCL 'x' this replaces raised a raw FileExistsError with no
            # error_code or explanation, and leaked a permanent block if the
            # process died anywhere in the prepare_production call below.
            reservation = submission_lock(source, '来源任务正在提交或恢复；不能同时读取其收敛证据。')
            try:
                check_previous_job(source, 'relaxation')
                production_kwargs = {'encut': payload['encut']} if payload.get('encut') is not None else {}
                result = convergence_reuse.prepare_production(source, folder, payload['mesh'], payload['source_snapshot'],
                                                              **production_kwargs)
            finally:
                reservation.close()
        else:
            result = convergence_reuse.prepare_retry(source, folder, payload['mode'], payload.get('encuts'), payload.get('meshes'), **kwargs)
    return _fork_job_files(cfg, release, name, folder, environment, result)


def fork_environment(cfg, source_config):
    """A fork runs where its source ran: the source's environment profile, or vasp_env for a legacy source.

    The bound profile comes from the source's own config (it is what the reused
    evidence was computed with), so a re-registered profile with a new
    executable cannot quietly serve an old source.
    """
    profile = source_config.get('environment_profile')
    require_profile_engine(engine_module('full_workflow'), profile)
    return task_environment(cfg, profile, source_config.get('relax_mode', 'cell'))


def _fork_job_files(cfg, release, name, folder, environment, result):
    """Batch script, environment fingerprint and release binding of a task forked from another."""
    script = ('#!/bin/bash\n#SBATCH --job-name=va-' + name + '\n#SBATCH --partition=' + cfg['partition'] +
        '\n#SBATCH --nodes=1\n#SBATCH --ntasks=8\n#SBATCH --cpus-per-task=1\n#SBATCH --time=' + cfg['walltime'] +
        '\n#SBATCH --output=slurm-%j.log\nset -eo pipefail\nsource ' + shlex.quote(cfg['vasp_env']) +
        '\nexport OMP_NUM_THREADS=1\n' + shlex.join([cfg['python'], str(release / 'vasp_band_agent.py'), 'run', str(folder), '--', 'srun', 'vasp_std']) + '\n')
    (folder / 'submit.slurm').write_text(script)
    write_json(folder / 'environment-review.json', environment)
    subprocess.run(['bash', '-n', str(folder / 'submit.slurm')], check=True, capture_output=True, timeout=10)
    resources = dict(partition=cfg['partition'], walltime=cfg['walltime'], ntasks=8, nodes=1, parallel_tests=1, cores_per_test=8)
    write_json(folder / 'remote-job.json', {'release': str(release), 'script_sha256': sha(folder / 'submit.slurm'), 'resources': resources})
    result['resources'] = resources
    return result


def _prepare_bands_path(payload, cfg, root, name, folder):
    """New task that redoes only bands on an operator k-path, reusing a verified SCF."""
    release = activate_release(deployed_release(root))
    # An engine without the marker would ignore config['band_path'] and quietly
    # run the automatic path; refuse instead.
    if getattr(engine_module('full_workflow'), 'BAND_PATH_VERSION', 0) < 1:
        raise ValueError('当前部署引擎不支持自定义能带路径，请先部署新版；不会改用自动路径。')
    band_path = engine_module('band_path')
    source = task_target(root / 'jobs', payload['source_job'])
    if folder.exists():
        raise ValueError('换路径必须使用不存在的新任务目录。')
    if not Path(cfg['vasp_env']).is_file():
        raise ValueError('VASP 环境脚本不存在。')
    source_config = json.loads((source / 'config.json').read_text())
    cfg, environment = fork_environment(cfg, source_config)
    previous_path = source / 'environment-review.json'
    if not previous_path.exists():
        raise ValueError('来源任务缺少环境指纹，不能证明其 SCF 电荷密度可在当前环境复用。')
    if json.loads(previous_path.read_text()).get('executable_sha256') != environment['executable_sha256']:
        raise ValueError('来源任务VASP可执行文件不同，不能复用其 SCF 电荷密度。')
    validate_scheduler(cfg)
    reservation = submission_lock(source, '来源任务正在提交或恢复；不能同时读取其 SCF 证据。')
    try:
        with contextlib.redirect_stdout(io.StringIO()):
            result = band_path.prepare_bands_path(source, folder, payload['band_path'])
    finally:
        reservation.close()
    return _fork_job_files(cfg, release, name, folder, environment, result)


def _band_path_candidates(payload, cfg, root, name, folder):
    """Read-only: k-path candidates for an existing task (name), computed by the deployed engine."""
    activate_release(deployed_release(root))
    # The source task may be bound to an older release; the candidates come from
    # the deployed engine, the same one prepare_bands_path will use.
    if getattr(engine_module('full_workflow'), 'BAND_PATH_VERSION', 0) < 2:
        raise ValueError('当前部署引擎不支持能带路径候选，请先部署新版；不会凭空给出路径。')
    source = task_target(root / 'jobs', name)
    with contextlib.redirect_stdout(io.StringIO()):
        data = engine_module('band_path').candidates_for_task(source, payload.get('extra_suggestion'),
                                                             payload.get('extra_rationale'))
    return {**data, 'content_is_untrusted': True}


def _prepare_scan_retry(payload, cfg, root, name, folder):
    return _prepare_from_convergence(payload, cfg, root, name, folder, production=False)


def _prepare_production_from_convergence(payload, cfg, root, name, folder):
    return _prepare_from_convergence(payload, cfg, root, name, folder, production=True)


def _prepare_task(payload, cfg, root, name, folder, preview_only):
    profile = payload.get('environment_profile')
    if profile is None and not Path(cfg['vasp_env']).is_file():
        raise ValueError('VASP 环境脚本不存在，请修正服务器计算配置。')
    cfg, environment = task_environment(cfg, profile, payload['parameters'].get('relax_mode', 'cell'))
    release = activate_release(deployed_release(root))
    full_workflow = engine_module('full_workflow')
    require_profile_engine(full_workflow, profile)
    if (preview_only or any(payload['parameters'].get(k) is not None for k in ('encuts', 'meshes', 'parameter_choice'))) and not getattr(full_workflow, 'SCAN_PLAN_VERSION', 0):
        raise ValueError('请先部署支持参数方案预览的最新版，未创建任务。')
    from types import SimpleNamespace
    import tempfile
    # The uploaded structure is an input to prepare_full, which reads it once and
    # writes the parsed cell into the job folder as initial.POSCAR; the digest it
    # records in the manifest is over that POSCAR, not over this file. So the copy
    # is dead the moment the call returns, and a per-request directory under root
    # says that. It replaces a shared root/uploads keyed by content hash: that
    # deduplicated the file but was outside jobs/, so manage_task's deletion and
    # the archive sweep never saw it and one entry per distinct structure
    # accumulated on the server forever. The hash key is kept only so the name is
    # stable within the request and never derived from caller input.
    with tempfile.TemporaryDirectory(prefix='vasp-input-', dir=None if preview_only else root) as scratch:
        uploads = Path(scratch)
        data = base64.b64decode(payload['structure'], validate=True)
        source = uploads / (hashlib.sha256(data).hexdigest() + '.vasp')
        source.write_bytes(data)
        params = payload['parameters']
        parallel = params.get('parallel_tests')
        cores = params.get('cores_per_test')
        if parallel is None:
            parallel = cfg.get('parallel_tests', 1)
        if cores is None:
            cores = cfg.get('cores_per_test', cfg['ntasks'])
        if type(parallel) is not int or not 1 <= parallel <= 6 or type(cores) is not int or not 1 <= cores <= 256 or parallel * cores > 256:
            raise ValueError('并发数须为 1–6，每测试核数须为 1–256，总核数不超过 256。')
        total_cores = parallel * cores
        if parallel > 1 and not hasattr(full_workflow, 'run_stages'):
            raise ValueError('服务器计算源码版本不支持并行，请先 remote_deploy 更新，再准备任务。')
        validate_scheduler(cfg)
        if params.get('potential_variants') and not getattr(full_workflow, 'POTENTIAL_VARIANTS_VERSION', 0):
            raise ValueError('当前 release 不支持显式赝势变体，请先部署新版；不会回退默认赝势。')
        args = SimpleNamespace(structure=str(source), workdir=str(folder), potcar=None,
            potential_variants=params.get('potential_variants'),
            potcar_library=cfg['potcar_library'], encut=params['encut'], mesh=params['mesh'],
            nbands=params['nbands'], relax_mode=params['relax_mode'], ediff=1e-7, points=40,
            symprec=.01, force_tol=.01, stress_tol=1., energy_tol=.001, nsw=200,
            parallel_tests=parallel, cores_per_test=cores, preview_only=preview_only,
            encuts=params.get('encuts'), meshes=params.get('meshes'), parameter_choice=params.get('parameter_choice'),
            scan_thresholds=params.get('scan_thresholds'), scan_preset=params.get('scan_preset'))
        if params.get('scan_thresholds') and getattr(full_workflow, 'SCAN_THRESHOLDS_VERSION', 0) < 1:
            raise ValueError('当前部署引擎不支持操作者扫描阈值，请先部署新版；不会按默认阈值静默执行。')
        if params.get('scan_preset') and getattr(full_workflow, 'SCAN_THRESHOLDS_VERSION', 0) < 2:
            raise ValueError('当前部署引擎不支持扫描判据档位，请先部署新版；不会按严格档静默执行。')
        if params.get('band_path') is not None:
            if getattr(full_workflow, 'BAND_PATH_VERSION', 0) < 1:
                raise ValueError('当前部署引擎不支持自定义能带路径，请先部署新版；不会改用自动路径。未创建任务。')
            args.band_path = params['band_path']
        if profile is not None:
            args.environment_profile = profile_record(profile)
        if params.get('optional_stages') or params.get('stage_parameters'):
            # An engine without the marker would drop the stages and their values.
            if getattr(full_workflow, 'PARAMETER_SPECS_VERSION', 0) < 1:
                raise ValueError('当前部署引擎不支持可选阶段与阶段参数，请先部署新版；不会去掉所请求的阶段。未创建任务。')
            args.optional_stages = params.get('optional_stages')
            args.stage_parameters = params.get('stage_parameters')
        with contextlib.redirect_stdout(io.StringIO()):
            preview = full_workflow.prepare_full(args)
    if preview_only:
        if not isinstance(preview, dict) or 'signature' not in preview:
            raise ValueError('请先部署支持参数预览的最新版。')
        return preview
    script = ('#!/bin/bash\n#SBATCH --job-name=va-' + name + '\n#SBATCH --partition=' + cfg['partition'] +
        '\n#SBATCH --nodes=1\n#SBATCH --ntasks=' + str(total_cores) + '\n#SBATCH --cpus-per-task=1\n#SBATCH --time=' + cfg['walltime'] +
        '\n#SBATCH --output=slurm-%j.log\nset -eo pipefail\nsource ' + shlex.quote(cfg['vasp_env']) +
        '\nexport OMP_NUM_THREADS=1\n' + shlex.join([cfg['python'], str(release / 'vasp_band_agent.py'), 'run', str(folder), '--', 'srun', 'vasp_std']) + '\n')
    (folder / 'submit.slurm').write_text(script)
    write_json(folder / 'environment-review.json', environment)
    subprocess.run(['bash', '-n', str(folder / 'submit.slurm')], check=True, capture_output=True, timeout=10)
    resources = {**{k: cfg[k] for k in ('partition', 'walltime')}, 'ntasks': total_cores,
                 'parallel_tests': parallel, 'cores_per_test': cores, 'nodes': 1}
    write_json(folder / 'remote-job.json', {'release': str(release), 'script_sha256': sha(folder / 'submit.slurm'), 'resources': resources})
    input_review = engine_module('input_review')
    manifest = json.loads((folder / 'manifest.json').read_text())
    if hasattr(input_review, 'preparation_summary') and 'hashes' in manifest:
        summary = input_review.preparation_summary(folder, resources)
    else:
        summary = {'available': False, 'missing_fields': ['preparation_summary'],
                   'note': '绑定的 release 不支持已校验准备摘要；可用 get_input_review 读取旧版上下文，不自动更换 release。'}
    # Files the remote engine wrote; the sibling read paths all carry this flag.
    return {'prepared': True, 'content_is_untrusted': True, 'environment': environment, 'preparation_summary': summary, 'remote_directory': str(folder), 'manifest': manifest,
            'resources': resources,
            'scan_verification': json.loads((folder / 'scan-verification.json').read_text()) if (folder / 'scan-verification.json').exists() else None}


def _prepare(payload, cfg, root, name, folder):
    return _prepare_task(payload, cfg, root, name, folder, preview_only=False)


def _preview(payload, cfg, root, name, folder):
    return _prepare_task(payload, cfg, root, name, folder, preview_only=True)


def _resource_task(folder):
    """Activate the task's own release once its script still matches the record."""
    meta = json.loads((folder / 'remote-job.json').read_text())
    if meta['script_sha256'] != sha(folder / 'submit.slurm'):
        raise ValueError('提交脚本哈希已变化。')
    task_release(meta)


def _plan_submission_resources(payload, cfg, folder):
    _resource_task(folder)
    return plan_fast_resources(folder, payload['phase'], payload.get('limits'))


def _resource_context(payload, cfg, folder):
    _resource_task(folder)
    return resource_context(folder, payload.get('phase'))


def _configure_resources(payload, cfg, folder):
    _resource_task(folder)
    return configure_resources(folder, payload.get('phase'), payload['resources'], payload['signature'], payload['limits'], payload.get('operation_id'))


def _stage_gate(folder):
    meta = json.loads((folder / 'remote-job.json').read_text())
    task_release(meta)
    stage_gate = engine_module('stage_gate')
    return stage_gate


def _confirm_physics(payload, cfg, folder):
    stage_gate = _stage_gate(folder)
    # A release that predates the channel parameter takes only
    # (folder, goal); calling it with the extra argument would surface
    # as a bare TypeError. Refuse with what to do about it instead.
    if getattr(stage_gate, 'CONFIRMATION_VERSION', 0) < 1:
        raise ValueError('该任务绑定的引擎未记录确认来源，请部署最新版并准备新任务。')
    # Defaulted rather than required so an older local caller still
    # works; the value is validated by confirm_physics itself, and it
    # always names an operator surface the local menu actually ran.
    channel = payload.get('channel', 'operator_slash_command')
    if channel not in stage_gate.CONFIRMATION_CHANNELS:
        raise ValueError('确认来源无效。')
    return stage_gate.confirm_physics(folder, payload['goal'], channel)


OCCUPIED = 1e-6


def scf_occupation_audit(folder):
    """Highest-band occupation at every SCF k-point, read from the hash-verified vasprun.xml.

    Evidence for the bands review's electronic check: NBANDS large enough to hold
    NELECT does not show the top band is empty. Computed by the worker (fresh on
    every call) so tasks on older engines get it too; the file it reads is in the
    review snapshot's evidence hashes, and its hash is checked here again.
    """
    import numpy as np
    from pymatgen.io.vasp.outputs import Vasprun
    folder = Path(folder)
    scf = folder / 'scf'
    stamp = json.loads((scf / 'complete.json').read_text(encoding='utf-8'))
    if not stamp.get('xml_sha256') or engine_module('vasp_band_agent').digest(scf / 'vasprun.xml') != stamp['xml_sha256']:
        raise ValueError('scf/vasprun.xml 与完成记录不一致，不能作为占据证据。')
    summary = json.loads((folder / 'scf-summary.json').read_text(encoding='utf-8'))
    efermi = summary.get('efermi')
    run = Vasprun(scf / 'vasprun.xml', parse_dos=False, parse_projected_eigen=False, parse_potcar_file=False)
    channels = {}
    for spin, values in run.eigenvalues.items():
        values = np.asarray(values)  # (k-points, bands, [energy, occupation])
        occupation = values[:, :, 1]
        occupied = np.flatnonzero((occupation > OCCUPIED).any(axis=0))
        highest = int(occupied.max()) + 1 if occupied.size else 0
        channels[getattr(spin, 'name', str(spin))] = {
            'kpoints': int(values.shape[0]), 'nbands': int(values.shape[1]),
            'highest_band_max_occupation': float(occupation[:, -1].max()),
            'highest_occupied_band': highest, 'empty_bands_above': int(values.shape[1]) - highest,
            'top_band_min_minus_efermi_eV': None if efermi is None else round(float(values[:, -1, 0].min() - efermi), 4)}
    if not channels:
        raise ValueError('scf/vasprun.xml 缺少本征值及占据数据。')
    return {'passed': all(c['highest_band_max_occupation'] <= OCCUPIED for c in channels.values()),
            'threshold': OCCUPIED, 'channels': channels, 'efermi': efermi, 'nelect': summary.get('nelect'),
            'source': 'scf/vasprun.xml', 'xml_sha256': stamp['xml_sha256'],
            'scope': '覆盖 SCF 网格全部 k 点；能带路径的顶部空带在能带计算完成后另行校验。'}


def with_occupation_audit(data, folder):
    if data.get('phase') == 'bands':
        try:
            data['scf_occupation_audit'] = scf_occupation_audit(folder)
        except (ValueError, OSError, KeyError) as exc:
            data['scf_occupation_audit'] = {'passed': False, 'unavailable': str(exc)}
    return data


def _stage_context(payload, cfg, folder):
    stage_gate = _stage_gate(folder)
    check_resume(folder)
    return with_occupation_audit(stage_gate.context(folder), folder)


def _stage_review(payload, cfg, folder):
    stage_gate = _stage_gate(folder)
    check_resume(folder)
    return stage_gate.record(folder, payload['snapshot'], payload['phase'], payload['checks'], payload['reviewer'])


def _input_review(folder):
    meta = json.loads((folder / 'remote-job.json').read_text())
    task_release(meta)
    input_review = engine_module('input_review')
    return input_review


def _review_context(payload, cfg, folder):
    input_review = _input_review(folder)
    return input_review.context(folder)


def _record_review(payload, cfg, folder):
    input_review = _input_review(folder)
    return input_review.record(folder, payload['snapshot'], payload['verdict'], payload['explanation'], payload['reviewer'])


def _submission_diagnostic(payload, cfg, folder):
    return submission_diagnostic(folder, payload.get('phase'), payload.get('job_id'), payload.get('start_time'), payload.get('end_time'))


def _recovery_status(payload, cfg, folder):
    operation = payload['operation_id']
    if not re.fullmatch(r'[a-f0-9]{32}', operation):
        raise ValueError('恢复编号无效。')
    path = recovery_path(folder, 'submission-recoveries/' + operation + '.json')
    return json.loads(path.read_text(encoding='utf-8')) if path.exists() else {'status': 'not_recorded'}


def _recovery_context(payload, cfg, folder):
    return recovery_context(folder, payload['phase'], payload['job_id'])


def _recover_submission(payload, cfg, folder):
    return recover_submission(folder, payload['phase'], payload['job_id'], payload['snapshot'],
                              payload['operation_id'], payload.get('operator_confirmed', False))


def _submit_task(payload, cfg, folder, preflight_only, report_refusal=False):
    """Every pre-sbatch check, then sbatch unless preflight_only.

    With report_refusal, a check that refuses returns an explicit
    submission_not_attempted result instead of raising, so the caller can tell
    "refused before sbatch" from a lost response in one round trip. Nothing
    after the checks is wrapped: an error from submit_with_receipt stays an
    error, because sbatch may already have run.
    """
    if not report_refusal:
        return _submit_checks(payload, cfg, folder, preflight_only)
    try:
        outcome = _submit_checks(payload, cfg, folder, True)
    except Exception as exc:
        return {'ready': False, 'submission_not_attempted': True,
                'error_code': getattr(exc, 'error_code', 'PREFLIGHT_REFUSED'),
                'error': diagnostic_text(str(exc))[:2000]}
    if outcome.get('ready') is not True:
        # An existing receipt: reported, never a second sbatch.
        return {**outcome, 'submission_not_attempted': True, 'existing_record': True}
    return submit_with_receipt(*outcome.pop('_submit'), outcome['capacity'])


def _submit_checks(payload, cfg, folder, preflight_only):
    recovery_barrier(folder, payload.get('phase'), require_new_review=True)
    if (folder / '.task-archived.json').exists():
        raise ValueError('任务已归档，请先恢复再提交。')
    phase = payload.get('phase')
    if phase is not None and phase not in task_phases(folder):
        raise ValueError('无效阶段。')
    record = folder / 'remote-submission.json' if phase is None else folder / 'submissions' / (phase + '.json')
    if record.exists():
        return json.loads(record.read_text())
    if (folder / '.agent.lock').exists():
        raise ValueError('任务仍有运行锁，不能提交。')
    meta = json.loads((folder / 'remote-job.json').read_text())
    if meta['script_sha256'] != sha(folder / 'submit.slurm'):
        raise ValueError('提交脚本已改变。')
    if payload.get('limits') is not None:
        check_resources((folder / 'submit.slurm').read_text(), payload['limits'])
    staged = json.loads((folder / 'manifest.json').read_text()).get('stage_gates', False)
    if staged and phase is None or not staged and phase is not None:
        raise ValueError('阶段式任务请用 submit_stage 指定阶段；旧任务使用原提交工具。')
    task_release(meta)
    if staged:
        stage_gate = engine_module('stage_gate')
        check_resume(folder)
    core = engine_module('vasp_band_agent')
    manifest = core.verify(folder)
    validate_array_submission(folder, phase, manifest, meta)
    config = json.loads((folder / 'config.json').read_text()) if (folder / 'config.json').exists() else {}
    profile = config.get('environment_profile')
    if profile is None:
        environment = inspect_environment(cfg, config.get('relax_mode', 'cell'))
    else:
        # A profile-bound task runs with the profile it was prepared with, never vasp_env.
        require_profile_engine(engine_module('full_workflow'), profile)
        _, environment = task_environment(cfg, profile, config.get('relax_mode', 'cell'))
    saved_environment = folder / 'environment-review.json'
    if saved_environment.exists():
        previous = json.loads(saved_environment.read_text())
        if any(previous.get(k) != environment[k] for k in ('environment_sha256', 'executable_sha256', 'executable')):
            raise ValueError('VASP环境已改变，请新建任务重新验证，不复用旧环境证据。')
    else:
        write_json(saved_environment, environment)
    if staged:
        stage_gate.require_review(folder, phase)
        check_previous_job(folder, phase)
    elif json.loads((folder / 'manifest.json').read_text()).get('review_required'):
        require_review = engine_module('input_review').require_review
        require_review(folder)
    capacity = capacity_preflight((folder / 'submit.slurm').read_text())
    if preflight_only:
        return {'ready': True, 'capacity': capacity, '_submit': (folder, record, phase, staged)}
    return submit_with_receipt(folder, record, phase, staged, capacity)


def _submit(payload, cfg, folder):
    return _submit_task(payload, cfg, folder, preflight_only=False)


def _submit_checked(payload, cfg, folder):
    """Preflight and submit in one call: one SSH connection and one verification pass."""
    return _submit_task(payload, cfg, folder, preflight_only=False, report_refusal=True)


def _configure_and_submit(payload, cfg, folder):
    """Write the operator's resources and, when that changed nothing reviewed, submit.

    One SSH connection, one Python start and one evidence pass instead of two
    calls. A write that changed the script or config needs a new stage review,
    so it returns without submitting, exactly as configure_resources alone; so
    does a write whose result does not match the request. Submission runs the
    same checks as submit_checked.
    """
    _resource_task(folder)
    phase, requested = payload.get('phase'), payload['resources']
    configured = configure_resources(folder, phase, requested, payload['signature'], payload['limits'],
                                     payload.get('operation_id'))
    def same(left, right):
        return (isinstance(left, dict) and isinstance(right, dict)
                and {**left, 'submission_mode': left.get('submission_mode', 'allocation')}
                == {**right, 'submission_mode': right.get('submission_mode', 'allocation')})
    if configured.get('changed'):
        return {**configured, 'submission': None, 'not_submitted': 'needs_review'}
    if configured.get('already_registered') or not same(configured.get('resources'), requested):
        return {**configured, 'submission': None, 'not_submitted': 'resource_mismatch'}
    return {**configured, 'submission': _submit_task({'phase': phase, 'limits': payload['submit_limits']}, cfg, folder,
                                                     preflight_only=False, report_refusal=True)}


def _submission_preflight(payload, cfg, folder):
    result = _submit_task(payload, cfg, folder, preflight_only=True)
    result.pop('_submit', None)
    return result


def _monitor_status(payload, cfg, folder):
    return monitor_status(folder, payload['job_id'], payload.get('phase'))


def _status(payload, cfg, folder):
    info = {}
    # Same bound as monitor_status's reader. This action is a poll target and it
    # concatenates every file it reads into one response, so an overgrown or
    # corrupt entry here is the one that can stall a poll; the sibling readers all
    # cap the file first and this one did not.
    def read_json(path):
        if path.is_symlink() or not path.resolve().is_relative_to(folder):
            raise ValueError('任务元数据路径越界或为符号链接。')
        if path.stat().st_size > 200_000:
            raise ValueError('任务元数据过大。')
        return json.loads(path.read_text(encoding='utf-8'))
    for filename in ('manifest.json', 'workflow-state.json', 'remote-submission.json', 'remote-job.json', 'ai-review.json', 'checkpoint.json', 'physics-model.json', 'report.json', 'convergence-array.json'):
        if (folder / filename).is_file():
            info[filename] = read_json(folder / filename)
    try:
        phases = task_phases(folder)
    except (ValueError, OSError) as exc:
        # A status read still reports what it can; the bad record is named.
        phases, info['stage_record_error'] = LEGACY_PHASES, diagnostic_text(str(exc))
    info['stage_submissions'] = {name: read_json(folder / 'submissions' / (name + '.json'))
                                 for name in phases
                                 if (folder / 'submissions' / (name + '.json')).is_file()}
    # Named explicitly rather than globbed: stage_gate writes exactly
    # review-<phase>.json, so the prefix form additionally pulled in any other
    # review-*.json that happened to sit in the folder.
    info['stage_reviews'] = {name: read_json(folder / ('review-' + name + '.json'))
                             for name in phases
                             if (folder / ('review-' + name + '.json')).is_file()}
    job_id = str(info.get('remote-submission.json', {}).get('job_id', ''))
    if re.fullmatch(r'\d+', job_id):
        array_receipt = info.get('remote-submission.json', {})
        is_array = array_receipt.get('submission_mode') == 'array'
        queue_command = ['squeue', '-h', '-j', job_id, '-o', '%i|%T|%M|%R'] + (['--array'] if is_array else [])
        queue = subprocess.run(queue_command, capture_output=True, text=True, timeout=15)
        info['scheduler_errors'] = []
        if queue.returncode:
            info['scheduler_errors'].append({'command': 'squeue', 'returncode': queue.returncode, 'stderr': queue.stderr[-2000:]})
        info['queue'] = queue.stdout.strip() if queue.returncode == 0 else None
        if not info['queue'] or is_array:
            account = subprocess.run(['sacct', '-n', '-j', job_id, '--format=JobID,State,ExitCode', '-P'], capture_output=True, text=True, timeout=15)
            if account.returncode:
                info['scheduler_errors'].append({'command': 'sacct', 'returncode': account.returncode, 'stderr': account.stderr[-2000:]})
            info['accounting'] = account.stdout[-4000:] if account.returncode == 0 else None
        info['scheduler_status_known'] = bool(info.get('queue')) or any(
            len(parts) >= 3 and parts[0] == job_id and bool(parts[1].strip())
            for parts in (line.split('|') for line in (info.get('accounting') or '').splitlines()))
        if is_array:
            summary = array_scheduler_status(array_receipt, info.get('queue'), info.get('accounting'))
            info['array_tasks'] = summary
            info['scheduler_status_known'] = summary['known']
            info['array_note'] = '数组元素分别排队；squeue -r 可展开。全部真实输出验收后仅放行下一阶段审阅，不自动提交。'
            config_path = folder / 'config.json'
            checkpoint = info.get('checkpoint.json') or {}
            array_config = json.loads(config_path.read_text(encoding='utf-8')) if config_path.exists() else {}
            if (summary['all_terminal'] and queue.returncode == 0
                    and array_config.get('submission_mode') == 'array'
                    and checkpoint.get('next', 'convergence') in ('convergence', 'relaxation')):
                try:
                    meta = info.get('remote-job.json', {})
                    task_release(meta)
                    result = engine_module('convergence_array').finalize_array(folder, job_id, scheduler_terminal=True, scheduler_evidence=summary)
                    info['array_collection'] = result
                    for filename in ('convergence-array.json', 'workflow-state.json', 'checkpoint.json'):
                        if (folder / filename).exists():
                            info[filename] = json.loads((folder / filename).read_text(encoding='utf-8'))
                except (ValueError, OSError, ImportError, KeyError) as exc:
                    info['array_collection_error'] = diagnostic_text(str(exc))
        if not info['scheduler_status_known']:
            info['scheduler_note'] = '调度状态未知：查询失败或没有记录，不代表作业完成；不要据此重复提交。'
        log = folder / f'slurm-{job_id}.log'
        if log.exists():
            with log.open('rb') as stream:
                stream.seek(max(0, log.stat().st_size - 12000))
                info['log_tail'] = stream.read().decode('utf-8', errors='replace')
        if is_array:
            info['array_log_tails'] = {}
            for item in info['array_tasks']['tasks']:
                log = folder / f'slurm-{item["job_id"]}.log'
                if log.is_file() and not log.is_symlink():
                    with log.open('rb') as stream:
                        stream.seek(max(0, log.stat().st_size - 2000))
                        info['array_log_tails'][item['job_id']] = diagnostic_text(stream.read().decode('utf-8', errors='replace'))
    return info


def _cancel_job(payload, cfg, folder):
    return cancel_job(folder, payload.get('phase'), payload.get('confirm_job_id'))


def _release_cancelled_submission(payload, cfg, folder):
    return release_cancelled_submission(folder, payload.get('phase'), payload.get('confirm_job_id'))


def _prepare_phase_resume(payload, cfg, folder):
    return prepare_phase_resume(folder, payload.get('phase'), payload.get('confirm_job_id'))


def result_files(folder):
    """The download allowlist for this task: legacy RESULT_FILES, plus its stages' files."""
    config = task_config(folder)
    if config.get('stages') is None:
        return RESULT_FILES
    task_release(json.loads((folder / 'remote-job.json').read_text()))
    extra = [name for name in stage_definitions(config).result_files(config) if name not in RESULT_FILES]
    return RESULT_FILES + tuple(extra) + ('config.json',)


def _fetch(payload, cfg, folder):
    files = {}
    for name in result_files(folder):
        path = (folder / name).resolve()
        if not path.is_relative_to(folder):
            raise ValueError('结果路径越界。')
        if path.is_file():
            if path.stat().st_size > 30_000_000:
                raise ValueError('结果文件超过 30 MB，请手动传输。')
            # One read: the hash is of exactly the bytes returned.
            data = path.read_bytes()
            files[name] = {'data': base64.b64encode(data).decode(), 'sha256': hashlib.sha256(data).hexdigest()}
    return {'files': files}


# One table per stage of request validation; the stage an action sits in says
# how much dispatch has checked before its handler runs.
# Need only the validated remote root: handler(payload, cfg, root).
ROOT_ACTIONS = {
    'environment_check': _environment_check,
    'software_inventory': _software_inventory,
    'environment_profile_check': _environment_profile_check,
    'list_jobs': _list_jobs,
    'manage_task': _manage_task,
    'deploy': _deploy,
    'deployment_info': _deployment_info,
}
# Create a task folder from a validated name: handler(payload, cfg, root, name, folder).
NEW_TASK_ACTIONS = {
    'inspect_convergence': _inspect_convergence,
    'prepare_scan_retry': _prepare_scan_retry,
    'prepare_production_from_convergence': _prepare_production_from_convergence,
    'prepare_bands_path': _prepare_bands_path,
    'band_path_candidates': _band_path_candidates,
    'prepare': _prepare,
    'preview': _preview,
}
# Act on an existing task folder: handler(payload, cfg, folder).
TASK_ACTIONS = {
    'resource_context': _resource_context,
    'configure_resources': _configure_resources,
    'plan_submission_resources': _plan_submission_resources,
    'confirm_physics': _confirm_physics,
    'stage_context': _stage_context,
    'stage_review': _stage_review,
    'review_context': _review_context,
    'record_review': _record_review,
    'submission_diagnostic': _submission_diagnostic,
    'recovery_status': _recovery_status,
    'recovery_context': _recovery_context,
    'recover_submission': _recover_submission,
    'submit': _submit,
    'submit_checked': _submit_checked,
    'configure_and_submit': _configure_and_submit,
    'submission_preflight': _submission_preflight,
    'monitor_status': _monitor_status,
    'status': _status,
    'fetch': _fetch,
    'cancel_job': _cancel_job,
    'release_cancelled_submission': _release_cancelled_submission,
    'prepare_phase_resume': _prepare_phase_resume,
}

# Read/verify actions whose checks hash the same files several times (manifest,
# stage evidence, resume record, review snapshot). Within one of these a file is
# hashed once unless it changes; nothing is cached across actions.
MEMO_ACTIONS = frozenset({'stage_context', 'stage_review', 'review_context', 'record_review',
                          'submission_preflight', 'submit', 'submit_checked',
                          'configure_and_submit'})


def handler_for(table, action):
    # A non-string action from a malformed payload falls through to the unknown
    # action error after the task checks, as the if-chain did, not a TypeError.
    return table.get(action) if isinstance(action, str) else None


def dispatch(payload):
    cfg = payload['config']
    root = Path(cfg['root']).resolve()
    if not root.is_absolute() or root == Path('/') or len(root.parts) < 4:
        raise ValueError('远程根目录不合法。')
    # Adopt the caller's session for the whole request, so that anything this
    # worker writes -- and anything it hands to sbatch below -- is labelled with
    # the session that asked for it. It arrives from the local process via the
    # payload; the name is validated here because it reaches an sbatch
    # --export argument and, from that, a remote environment.
    session = payload.get('session')
    if session is not None:
        if not isinstance(session, str) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_-]{0,63}', session):
            raise ValueError('远程请求的会话名称无效。')
        os.environ['VASP_AGENT_SESSION'] = session
    action = payload['action']
    # environment_check and the read-only actions never create the root.
    if action not in ('environment_check', 'preview', 'monitor_status', 'deployment_info',
                      'software_inventory', 'environment_profile_check'):
        root.mkdir(parents=True, exist_ok=True)
    handler = handler_for(ROOT_ACTIONS, action)
    if handler:
        return handler(payload, cfg, root)
    name = payload['job_name']
    if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_-]{0,63}', name):
        raise ValueError('非法任务名。')
    folder = (root / 'jobs' / name).resolve()
    if not folder.is_relative_to(root):
        raise ValueError('任务路径越界。')
    handler = handler_for(NEW_TASK_ACTIONS, action)
    if handler:
        return handler(payload, cfg, root, name, folder)
    if not folder.is_dir():
        raise FileNotFoundError('远程任务不存在。')
    handler = handler_for(TASK_ACTIONS, action)
    if handler is None:
        raise ValueError('未知远程动作。')
    if action not in MEMO_ACTIONS:
        return handler(payload, cfg, folder)
    with memoized_action():
        return handler(payload, cfg, folder)


if __name__ == '__main__':
    try:
        print(json.dumps({'ok': True, 'result': dispatch(json.load(sys.stdin))}, ensure_ascii=False))
    except Exception as exc:
        print(json.dumps({'ok': False, 'error': str(exc), 'type': type(exc).__name__,
                          'error_code': getattr(exc, 'error_code', 'FILE_NOT_FOUND' if isinstance(exc, FileNotFoundError) else 'REMOTE_ERROR')}, ensure_ascii=False))
        sys.exit(1)
