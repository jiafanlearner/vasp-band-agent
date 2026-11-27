"""Trusted remote RPC worker. Payload travels on stdin, never in shell interpolation."""
import base64
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys

# Names from the earlier worker units, re-exported so `remote_worker.X` stays the
# one import path for callers. The stdin program defines them by concatenation.
# <bundle:package-only>
import types as _types
from vasp_agent.remote.worker_common import (
    LEGACY_PHASES, SubmissionError, diagnostic_text, digest_memo, file_sha, is_phase_name, sha, task_phases, write_json)
from vasp_agent.remote.worker_slurm import (
    MAX_ARRAY_CONCURRENCY, MAX_ARRAY_TESTS, accounting_window, array_layout, array_scheduler_status,
    capacity_preflight, monitor_status, scheduler_read, submission_accounting, submission_command_evidence)
from vasp_agent.remote.worker_receipts import (
    lock_is_held, open_lock, recover_submission, recovery_barrier, recovery_context, recovery_path,
    submission_lock, submit_with_receipt, try_lock)
from vasp_agent.remote.worker_inventory import DEFAULT_ROOTS, MAX_ROOTS, expand_root, software_inventory
from vasp_agent.remote.worker_env import (
    ENGINE_MODULES, ENVIRONMENT_PROFILES_VERSION, PROFILE_FIELDS, RESULT_FILES, _MEMO_SCOPE,
    activate_release, deployed_release, engine_module, engine_release, environment_evidence,
    execution_tag_status, fork_environment, inspect_environment, memoized_action, profile_record,
    release_version, require_profile_engine, share_digest_memo, stage_definitions, task_config,
    task_environment, task_release)
from vasp_agent.remote.worker_tasks import (
    check_previous_job, deletion_preview, list_workflow_jobs, manage_task, scheduler_query, task_target)
from vasp_agent.remote.worker_lifecycle import (
    PHASES, PHASE_OUTPUTS, PHASE_RESUME_VERSION, RESUMABLE_STATES, STAGE_INPUTS,
    cancel_job, check_resume, generated_at_start, kept_stage_inputs, phase_outputs,
    phase_stage_folders, prepare_phase_resume, record_event, release_cancelled_submission,
    started_outputs, submitted_receipt, verified_stage)
from vasp_agent.remote.worker_resources import (
    check_resources, configure_resources, plan_fast_resources, resource_context,
    validate_array_submission, validate_scheduler, wall_seconds)
from vasp_agent.remote.worker_diagnostics import (
    OCCUPIED, _status, diagnostic_file, scf_occupation_audit, submission_diagnostic, task_evidence_files)
from vasp_agent.remote.worker_prepare import (
    _band_path_candidates, _convergence_reuse, _dos_fork_preview, _fork_job_files, _inspect_convergence,
    _prepare, _prepare_bands_path, _prepare_dos_fork, _prepare_from_convergence,
    _prepare_production_from_convergence, _prepare_scan_retry, _prepare_task, _preview, dos_engine)


class _WorkerFacade(_types.ModuleType):
    """Mirror monkeypatches made on remote_worker into any WORKER_UNITS module that binds the name."""

    def __setattr__(self, name, value):
        super().__setattr__(name, value)
        for unit in self.__dict__.get('WORKER_UNITS', ()):
            mod = sys.modules.get('vasp_agent.remote.' + unit)
            if mod is not None and (name in mod.__dict__ or name == '__package__'):
                if name == '__package__':
                    mod.__dict__['__package__'] = value
                else:
                    setattr(mod, name, value)

    def __delattr__(self, name):
        super().__delattr__(name)
        for unit in self.__dict__.get('WORKER_UNITS', ()):
            mod = sys.modules.get('vasp_agent.remote.' + unit)
            if mod is not None and name in mod.__dict__:
                delattr(mod, name)


sys.modules[__name__].__class__ = _WorkerFacade
del _WorkerFacade, _types
# </bundle:package-only>


# Sibling modules this worker is assembled from, in dependency order. The
# package imports them; the stdin program concatenates them ahead of this file
# (bundle.worker_program), so each may use only the stdlib and earlier units.
WORKER_UNITS = (
    'worker_common', 'worker_slurm', 'worker_receipts', 'worker_inventory',
    'worker_env', 'worker_tasks', 'worker_lifecycle', 'worker_resources',
    'worker_diagnostics', 'worker_prepare',
)
# Explicit package allowlist plus legacy launchers. Existing release layouts stay usable.
SOURCE_FILES = tuple(name + '.py' for name in ENGINE_MODULES) + (
    'vasp_agent/__init__.py', 'vasp_agent/workflow/__init__.py', 'vasp_agent/remote/__init__.py',
) + tuple(module.replace('.', '/') + '.py' for module in ENGINE_MODULES.values()) + tuple(
    'vasp_agent/remote/' + unit + '.py' for unit in WORKER_UNITS)


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
    check = subprocess.run([cfg['python'], '-c', 'import pymatgen.io.vasp, matplotlib, numpy'],
                           capture_output=True, text=True, timeout=180)
    if check.returncode:
        raise ValueError('所配置 Python 缺少计算依赖。请提供独立已安装 pymatgen/matplotlib/numpy 的 Python 路径。' + check.stderr[-2000:])
    write_json(root / 'deployment.json', {'release': str(release), 'version': version})
    return {'deployed': True, 'release': str(release), 'python': cfg['python']}


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
    return engine_module('stage_gate')


def _confirm_physics(payload, cfg, folder):
    stage_gate = _stage_gate(folder)
    if getattr(stage_gate, 'CONFIRMATION_VERSION', 0) < 1:
        raise ValueError('该任务绑定的引擎未记录确认来源，请部署最新版并准备新任务。')
    channel = payload.get('channel', 'operator_slash_command')
    if channel == 'operator_task_contract' and getattr(stage_gate, 'CONFIRMATION_VERSION', 0) < 2:
        raise ValueError('该任务绑定的引擎不支持 operator_task_contract 确认通道，请先 remote_deploy 部署最新版。')
    if channel not in stage_gate.CONFIRMATION_CHANNELS:
        raise ValueError('确认来源无效。')
    if channel == 'operator_task_contract':
        return stage_gate.confirm_physics(
            folder, payload['goal'], channel,
            contract_id=payload.get('contract_id'), contract_sha256=payload.get('contract_sha256'))
    if payload.get('contract_id') is not None or payload.get('contract_sha256') is not None:
        raise ValueError('仅 operator_task_contract 通道接受 contract_id 或 contract_sha256。')
    return stage_gate.confirm_physics(folder, payload['goal'], channel)


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
    return engine_module('input_review')


def _review_context(payload, cfg, folder):
    return _input_review(folder).context(folder)


def _record_review(payload, cfg, folder):
    return _input_review(folder).record(folder, payload['snapshot'], payload['verdict'], payload['explanation'], payload['reviewer'])


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
    """Every pre-sbatch check, then sbatch unless preflight_only."""
    if not report_refusal:
        return _submit_checks(payload, cfg, folder, preflight_only)
    try:
        outcome = _submit_checks(payload, cfg, folder, True)
    except Exception as exc:
        return {'ready': False, 'submission_not_attempted': True,
                'error_code': getattr(exc, 'error_code', 'PREFLIGHT_REFUSED'),
                'error': diagnostic_text(str(exc))[:2000]}
    if outcome.get('ready') is not True:
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
    config = json.loads((folder / 'config.json').read_text(encoding='utf-8')) if (folder / 'config.json').exists() else {}
    profile = config.get('environment_profile')
    if profile is None:
        environment = inspect_environment(cfg, config.get('relax_mode', 'cell'))
    else:
        require_profile_engine(engine_module('full_workflow'), profile)
        _, environment = task_environment(cfg, profile, config.get('relax_mode', 'cell'))
    saved_environment = folder / 'environment-review.json'
    if saved_environment.exists():
        previous = json.loads(saved_environment.read_text(encoding='utf-8'))
        if any(previous.get(k) != environment[k] for k in ('environment_sha256', 'executable_sha256', 'executable')):
            raise ValueError('VASP环境已改变，请新建任务重新验证，不复用旧环境证据。')
    else:
        write_json(saved_environment, environment)
    if staged:
        stage_gate.require_review(folder, phase)
        check_previous_job(folder, phase)
    elif json.loads((folder / 'manifest.json').read_text()).get('review_required'):
        engine_module('input_review').require_review(folder)
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
    """Write the operator's resources and, when that changed nothing reviewed, submit."""
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
            data = path.read_bytes()
            files[name] = {'data': base64.b64encode(data).decode(), 'sha256': hashlib.sha256(data).hexdigest()}
    return {'files': files}


ROOT_ACTIONS = {
    'environment_check': _environment_check,
    'software_inventory': _software_inventory,
    'environment_profile_check': _environment_profile_check,
    'list_jobs': _list_jobs,
    'manage_task': _manage_task,
    'deploy': _deploy,
    'deployment_info': _deployment_info,
}
NEW_TASK_ACTIONS = {
    'inspect_convergence': _inspect_convergence,
    'prepare_scan_retry': _prepare_scan_retry,
    'prepare_production_from_convergence': _prepare_production_from_convergence,
    'prepare_bands_path': _prepare_bands_path,
    'band_path_candidates': _band_path_candidates,
    'dos_fork_preview': _dos_fork_preview,
    'prepare_dos_fork': _prepare_dos_fork,
    'prepare': _prepare,
    'preview': _preview,
}
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
MEMO_ACTIONS = frozenset({'stage_context', 'stage_review', 'review_context', 'record_review',
                          'submission_preflight', 'submit', 'submit_checked',
                          'configure_and_submit'})


def handler_for(table, action):
    return table.get(action) if isinstance(action, str) else None


def dispatch(payload):
    cfg = payload['config']
    root = Path(cfg['root']).resolve()
    if not root.is_absolute() or root == Path('/') or len(root.parts) < 4:
        raise ValueError('远程根目录不合法。')
    session = payload.get('session')
    if session is not None:
        if not isinstance(session, str) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_-]{0,63}', session):
            raise ValueError('远程请求的会话名称无效。')
        os.environ['VASP_AGENT_SESSION'] = session
    action = payload['action']
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
