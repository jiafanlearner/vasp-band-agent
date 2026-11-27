"""Remote task preparation, preview, convergence forks, and band-path forks.

Part of the stdin worker program (see remote_worker.WORKER_UNITS): standard library only.
"""
import base64
import contextlib
import hashlib
import io
import json
from pathlib import Path
import shlex
import subprocess

# <bundle:package-only>
from vasp_agent.remote.worker_common import sha, write_json
from vasp_agent.remote.worker_receipts import submission_lock
from vasp_agent.remote.worker_env import (
    activate_release, deployed_release, engine_module, fork_environment, profile_record,
    require_profile_engine, task_environment)
from vasp_agent.remote.worker_tasks import check_previous_job, task_target
from vasp_agent.remote.worker_resources import validate_scheduler
# </bundle:package-only>


def _require_solver_overrides_engine(full_workflow, overrides):
    if overrides is not None and getattr(full_workflow, 'SOLVER_OVERRIDES_VERSION', 0) < 1:
        raise ValueError('当前部署引擎不支持 Tier ③ solver_overrides，请先 remote_deploy 部署最新版；不会静默丢弃。')


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
    source = task_target(root / 'jobs', name)
    if (source / 'config.json').is_file():
        source_config = json.loads((source / 'config.json').read_text(encoding='utf-8'))
        _require_solver_overrides_engine(engine_module('full_workflow'), source_config.get('solver_overrides'))
    return convergence_reuse.inspect(source)


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
    source_config = json.loads((source / 'config.json').read_text(encoding='utf-8'))
    if (source_config.get('execution_tags') is not None
            and getattr(engine_module('full_workflow'), 'EXECUTION_TAGS_VERSION', 0) < source_config['execution_tags']):
        raise ValueError('来源任务使用并行标签，当前部署引擎不支持；请先部署新版，不会去掉标签继续。')
    cfg, environment = fork_environment(cfg, source_config)
    previous_path = source / 'environment-review.json'
    if not previous_path.exists():
        raise ValueError('来源任务缺少环境指纹，不能证明跨环境收敛复用有效；请重新扫描。')
    previous = json.loads(previous_path.read_text(encoding='utf-8'))
    if previous.get('executable_sha256') != environment['executable_sha256']:
        raise ValueError('来源任务VASP可执行文件不同，不能复用收敛证据。')
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
        if payload.get('solver_overrides') is not None or source_config.get('solver_overrides') is not None:
            _require_solver_overrides_engine(
                engine_module('full_workflow'),
                payload.get('solver_overrides') if payload.get('solver_overrides') is not None else source_config.get('solver_overrides'))
        if payload.get('solver_overrides') is not None:
            kwargs['solver_overrides'] = payload['solver_overrides']
        if production:
            reservation = submission_lock(source, '来源任务正在提交或恢复；不能同时读取其收敛证据。')
            try:
                check_previous_job(source, 'relaxation')
                production_kwargs = {'encut': payload['encut']} if payload.get('encut') is not None else {}
                if payload.get('solver_overrides') is not None:
                    production_kwargs['solver_overrides'] = payload['solver_overrides']
                result = convergence_reuse.prepare_production(source, folder, payload['mesh'], payload['source_snapshot'],
                                                              **production_kwargs)
            finally:
                reservation.close()
        else:
            result = convergence_reuse.prepare_retry(source, folder, payload['mode'], payload.get('encuts'), payload.get('meshes'), **kwargs)
    return _fork_job_files(cfg, release, name, folder, environment, result)


def _prepare_bands_path(payload, cfg, root, name, folder):
    """New task that redoes only bands on an operator k-path, reusing a verified SCF."""
    release = activate_release(deployed_release(root))
    if getattr(engine_module('full_workflow'), 'BAND_PATH_VERSION', 0) < 1:
        raise ValueError('当前部署引擎不支持自定义能带路径，请先部署新版；不会改用自动路径。')
    band_path = engine_module('band_path')
    source = task_target(root / 'jobs', payload['source_job'])
    if folder.exists():
        raise ValueError('换路径必须使用不存在的新任务目录。')
    if not Path(cfg['vasp_env']).is_file():
        raise ValueError('VASP 环境脚本不存在。')
    source_config = json.loads((source / 'config.json').read_text(encoding='utf-8'))
    if payload.get('solver_overrides') is not None or source_config.get('solver_overrides') is not None:
        _require_solver_overrides_engine(
            engine_module('full_workflow'),
            payload.get('solver_overrides') if payload.get('solver_overrides') is not None else source_config.get('solver_overrides'))
    cfg, environment = fork_environment(cfg, source_config)
    previous_path = source / 'environment-review.json'
    if not previous_path.exists():
        raise ValueError('来源任务缺少环境指纹，不能证明其 SCF 电荷密度可在当前环境复用。')
    if json.loads(previous_path.read_text(encoding='utf-8')).get('executable_sha256') != environment['executable_sha256']:
        raise ValueError('来源任务VASP可执行文件不同，不能复用其 SCF 电荷密度。')
    validate_scheduler(cfg)
    reservation = submission_lock(source, '来源任务正在提交或恢复；不能同时读取其 SCF 证据。')
    try:
        with contextlib.redirect_stdout(io.StringIO()):
            fork_kwargs = {'solver_overrides': payload['solver_overrides']} if payload.get('solver_overrides') is not None else {}
            result = band_path.prepare_bands_path(source, folder, payload['band_path'], **fork_kwargs)
    finally:
        reservation.close()
    return _fork_job_files(cfg, release, name, folder, environment, result)


def dos_engine():
    """The deployed engine's DOS module; an engine without the marker refuses (never a silent bands-only fork)."""
    if getattr(engine_module('full_workflow'), 'DOS_VERSION', 0) < 1:
        raise ValueError('当前部署引擎不支持 DOS 阶段与 DOS 派生，请先部署新版；未创建任务。')
    return engine_module('dos_stage')


def _dos_fork_preview(payload, cfg, root, name, folder):
    """Read-only: the DOS spec and its suggestions on an existing task's (name) verified SCF."""
    activate_release(deployed_release(root))
    dos_stage = dos_engine()
    source = task_target(root / 'jobs', name)
    if (source / 'config.json').is_file():
        source_config = json.loads((source / 'config.json').read_text(encoding='utf-8'))
        _require_solver_overrides_engine(engine_module('full_workflow'), source_config.get('solver_overrides'))
    with contextlib.redirect_stdout(io.StringIO()):
        data = dos_stage.fork_preview(source)
    return {**data, 'content_is_untrusted': True}


def _prepare_dos_fork(payload, cfg, root, name, folder):
    """New task that runs only DOS, reusing a verified SCF of source_job on the same executable."""
    release = activate_release(deployed_release(root))
    dos_stage = dos_engine()
    source = task_target(root / 'jobs', payload['source_job'])
    if folder.exists():
        raise ValueError('DOS 派生必须使用不存在的新任务目录。')
    if not Path(cfg['vasp_env']).is_file():
        raise ValueError('VASP 环境脚本不存在。')
    source_config = json.loads((source / 'config.json').read_text(encoding='utf-8'))
    if (source_config.get('execution_tags') is not None
            and getattr(engine_module('full_workflow'), 'EXECUTION_TAGS_VERSION', 0) < source_config['execution_tags']):
        raise ValueError('来源任务使用并行标签，当前部署引擎不支持；请先部署新版。')
    if payload.get('solver_overrides') is not None or source_config.get('solver_overrides') is not None:
        _require_solver_overrides_engine(
            engine_module('full_workflow'),
            payload.get('solver_overrides') if payload.get('solver_overrides') is not None else source_config.get('solver_overrides'))
    cfg, environment = fork_environment(cfg, source_config)
    validate_scheduler(cfg)
    reservation = submission_lock(source, '来源任务正在提交或恢复；不能同时读取其 SCF 证据。')
    try:
        with contextlib.redirect_stdout(io.StringIO()):
            # The engine checks the source's executable fingerprint against this environment.
            fork_kwargs = {'solver_overrides': payload['solver_overrides']} if payload.get('solver_overrides') is not None else {}
            result = dos_stage.prepare_dos_fork(source, folder, payload['values'], environment=environment, **fork_kwargs)
    finally:
        reservation.close()
    return _fork_job_files(cfg, release, name, folder, environment, result)


def _band_path_candidates(payload, cfg, root, name, folder):
    """Read-only: k-path candidates for an existing task (name), computed by the deployed engine."""
    activate_release(deployed_release(root))
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
            if getattr(full_workflow, 'PARAMETER_SPECS_VERSION', 0) < 1:
                raise ValueError('当前部署引擎不支持可选阶段与阶段参数，请先部署新版；不会去掉所请求的阶段。未创建任务。')
            if 'dos' in (params.get('optional_stages') or []) and getattr(full_workflow, 'DOS_VERSION', 0) < 1:
                raise ValueError('当前部署引擎不支持 DOS 阶段，请先部署新版；不会去掉所请求的阶段。未创建任务。')
            args.optional_stages = params.get('optional_stages')
            args.stage_parameters = params.get('stage_parameters')
        if params.get('solver_overrides') is not None:
            _require_solver_overrides_engine(full_workflow, params['solver_overrides'])
            args.solver_overrides = params['solver_overrides']
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
    return {'prepared': True, 'content_is_untrusted': True, 'environment': environment, 'preparation_summary': summary, 'remote_directory': str(folder), 'manifest': manifest,
            'resources': resources,
            'scan_verification': json.loads((folder / 'scan-verification.json').read_text()) if (folder / 'scan-verification.json').exists() else None}


def _prepare(payload, cfg, root, name, folder):
    return _prepare_task(payload, cfg, root, name, folder, preview_only=False)


def _preview(payload, cfg, root, name, folder):
    return _prepare_task(payload, cfg, root, name, folder, preview_only=True)
