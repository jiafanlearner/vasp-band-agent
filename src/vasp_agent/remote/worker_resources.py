"""Slurm resource validation, context, fast-layout planning, rewriting, and array checks.

Part of the stdin worker program (see remote_worker.WORKER_UNITS): standard library only.
"""
import hashlib
import json
from pathlib import Path
import re
import shlex

# <bundle:package-only>
from vasp_agent.remote.worker_common import SubmissionError, is_phase_name, sha, task_phases, write_json
from vasp_agent.remote.worker_slurm import (
    MAX_ARRAY_CONCURRENCY, MAX_ARRAY_TESTS, array_layout, capacity_preflight, scheduler_read)
from vasp_agent.remote.worker_receipts import recovery_barrier
from vasp_agent.remote.worker_env import engine_module, engine_release, execution_tag_status
from vasp_agent.remote.worker_tasks import check_previous_job
# </bundle:package-only>


def wall_seconds(value):
    if not re.fullmatch(r'\d{1,3}:[0-5]\d:[0-5]\d', value):
        raise ValueError('运行时间必须为 HH:MM:SS。')
    h, m, s = map(int, value.split(':'))
    seconds = h * 3600 + m * 60 + s
    if seconds <= 0:
        raise ValueError('运行时间必须大于零。')
    return seconds


def validate_scheduler(cfg):
    """Check the two profile values that get interpolated into submit.slurm."""
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
    config = json.loads((folder / 'config.json').read_text(encoding='utf-8'))
    script = (folder / 'submit.slurm').read_text(encoding='utf-8')
    values = dict(re.findall(r'^#SBATCH --([\w-]+)=([^\n]+)$', script, re.M))
    resources = dict(parallel_tests=config.get('parallel_tests', 1), cores_per_test=config.get('cores_per_test', 8),
                     nodes=int(values.get('nodes', 1)), partition=values['partition'], walltime=values['time'])
    if 'submission_mode' in config:
        resources['submission_mode'] = config['submission_mode']
    if phase not in (None, 'convergence') and resources.get('submission_mode') == 'array':
        resources.update(submission_mode='allocation', parallel_tests=1)
    receipt_path = folder / 'resource-change.json'
    receipt = json.loads(receipt_path.read_text()) if receipt_path.exists() else None
    return {'resource_change': receipt, 'resources': resources, 'phase': phase,
            'execution_tags': execution_tag_status(config, phase), 'execution_plan': {'submission_mode': resources.get('submission_mode', 'allocation'),
            'array_elements': 3 * len(config.get('active_scans', ['encut', 'mesh'])) + (1 if config.get('fixed_encut') is not None else 0) if resources.get('submission_mode') == 'array' else 0,
            'compute': config.get('active_scans', ['encut', 'mesh']),
            'reuse': [k for k in ('encut', 'mesh') if k not in config.get('active_scans', ['encut', 'mesh'])],
            'tests': 3 * len(config.get('active_scans', ['encut', 'mesh'])) + (1 if config.get('fixed_encut') is not None else 0), 'cross_cutoff': config.get('fixed_encut') is not None, 'post_relax_checks': ['higher-cutoff', 'denser-mesh']}, 'signature': hashlib.sha256(
        ((folder / 'config.json').read_text(encoding='utf-8') + script).encode('utf-8')).hexdigest()}


def plan_fast_resources(folder, phase, limits=None, read=None):
    """Read-only throughput heuristic; no timing claims, mutations or authorization."""
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
    bounds = dict(limits) if limits is not None else dict(
        nodes=min(256, cores * min(6, tests) if mode == 'array' else cores),
        cores=min(65536, cores * min(6, tests)),
        partition=resources['partition'], walltime=resources['walltime'])
    if bounds['partition'] != resources['partition']:
        raise ValueError('当前任务分区不在资源授权范围内。')
    if wall_seconds(resources['walltime']) > wall_seconds(bounds['walltime']):
        raise ValueError('当前时限超过授权；不会自动缩短时限导致运行中断。')
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
    script = (folder / 'submit.slurm').read_text(encoding='utf-8')
    if 'srun vasp_std' not in script:
        raise ValueError('此脚本不使用受控 srun vasp_std 命令，不能自动配置并行。')
    config = json.loads((folder / 'config.json').read_text(encoding='utf-8'))
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
    changed = script != (folder / 'submit.slurm').read_text(encoding='utf-8') or config.get('submission_mode', 'allocation') != mode or any(config.get(k, 1) != resources[k] for k in ('parallel_tests', 'cores_per_test', 'nodes'))
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
