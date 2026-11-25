"""Execution tags, scan thresholds, relaxation checks, and convergence acceptance policies."""
import json
import os

import numpy as np
from pymatgen.io.vasp.inputs import Incar, Kpoints

import vasp_agent.workflow.vasp_band_agent as core


def base_incar(config, encut):
    data = dict(ENCUT=encut, EDIFF=config['ediff'], PREC='Accurate', GGA='PE',
                ISPIN=1, ISMEAR=0, SIGMA=0.05, LREAL=False, LASPH=True,
                LMAXMIX=6, NELM=150, NSW=0, IBRION=-1, LWAVE=False,
                ALGO='Normal', ISTART=0, ICHARG=2, LCHARG=False, ISIF=2)
    if config['nbands'] is not None:
        data['NBANDS'] = config['nbands']
    return data


# Parallelization-only INCAR tags. They change how VASP distributes work, not the
# physical model, so they are kept out of the reviewed-plan comparison -- but only
# these keys, only with the values execution_tags() derives, and only for tasks
# prepared with config['execution_tags'] (older tasks run exactly as reviewed).
EXECUTION_TAGS_VERSION = 1
EXECUTION_TAGS = core.EXECUTION_TAGS
# Largest point group (Oh, with inversion, so time reversal adds nothing): no
# Gamma-centred mesh of N points has fewer than ceil(N / 48) irreducible points.
MAX_POINT_GROUP_ORDER = 48
MIN_RANKS_PER_KPOINT_GROUP = 4
MAX_NCORE = 4
EXECUTION_TAGS_RULE = ('nk = ceil(网格点数/48)（不可约 k 点数下界；能带线模式为路径点总数）；'
                       'KPAR = 每组 MPI 数的最大因子且 ≤ min(nk, MPI/4)；'
                       'NCORE = MPI/KPAR 与每节点 MPI 的公因子中最大且 ≤ min(4, √(MPI/KPAR)) 者。'
                       '仅影响并行效率；VASP 会把 NBANDS 向上取整到 MPI/(KPAR·NCORE) 的倍数。')


def kpoint_lower_bound(kpoints):
    """Fewest k-points VASP can end up with for this KPOINTS, without spglib.

    A symmetry count would depend on SYMPREC and the spglib build; a lower bound
    only ever makes KPAR smaller, never larger than the real k-point count.
    """
    style = kpoints.style.name
    if style == 'Line_mode':
        return int(kpoints.num_kpts) * (len(kpoints.kpts) // 2)
    if style in ('Gamma', 'Monkhorst') and len(kpoints.kpts) == 1:
        return max(1, -(-int(np.prod(kpoints.kpts[0])) // MAX_POINT_GROUP_ORDER))
    if style == 'Reciprocal':  # explicit band path list, used as given
        return len(kpoints.kpts)
    raise ValueError('无法从该 KPOINTS 推导并行标签。')


def execution_tags(ranks, nodes, kpoints=None):
    """KPAR/NCORE for one stage from the MPI ranks it runs on.

    KPAR divides ranks and keeps at least four ranks per k-point group; NCORE
    divides the ranks of a group and of a node, at most 4 and about the square
    root of the group size. With kpoints=None the bound on k-points is dropped,
    giving the largest values any stage of the task can receive.
    """
    if type(ranks) is not int or type(nodes) is not int or ranks < 1 or nodes < 1 or ranks % nodes:
        raise ValueError('MPI 进程数或节点布局无效，无法推导并行标签。')
    limit = max(1, ranks // MIN_RANKS_PER_KPOINT_GROUP)
    if kpoints is not None:
        limit = min(limit, kpoint_lower_bound(kpoints))
    kpar = max(d for d in range(1, limit + 1) if ranks % d == 0)
    group, per_node = ranks // kpar, ranks // nodes
    ncore = max(d for d in range(1, min(MAX_NCORE, int(np.sqrt(group))) + 1)
                if group % d == 0 and per_node % d == 0)
    return {'KPAR': kpar, 'NCORE': ncore}


def stage_execution(config, kpoints):
    """The execution record for one stage, or None for tasks without the flag."""
    if config.get('execution_tags') != EXECUTION_TAGS_VERSION:
        return None
    ranks, nodes = config.get('cores_per_test', 8), config.get('nodes', 1)
    tags = execution_tags(ranks, nodes, kpoints)
    return {'version': EXECUTION_TAGS_VERSION, 'tags': tags, 'mpi_ranks_per_test': ranks,
            'nodes': nodes, 'kpoint_lower_bound': kpoint_lower_bound(kpoints),
            'band_groups': ranks // (tags['KPAR'] * tags['NCORE']), 'rule': EXECUTION_TAGS_RULE}


def execution_preview(config, phase):
    """What the review and resource summaries show; values follow the current config."""
    if config.get('execution_tags') != EXECUTION_TAGS_VERSION:
        return {'enabled': False, 'note': '旧任务按原审阅方案运行，不写入 KPAR/NCORE（VASP 默认 1）。'}
    ranks, nodes = config.get('cores_per_test', 8), config.get('nodes', 1)
    stage_tags = {}
    if phase == 'convergence':
        stage_tags = {test['stage']: execution_tags(ranks, nodes, Kpoints.gamma_automatic(test['mesh']))
                      for test in convergence_tests(config)}
    elif phase == 'relaxation':
        stage_tags = {name: execution_tags(ranks, nodes, Kpoints.gamma_automatic(grid))
                      for name, grid in relaxation_grids(config).items()}
    return {'enabled': True, 'allowlist': list(EXECUTION_TAGS), 'mpi_ranks_per_test': ranks, 'nodes': nodes,
            'stages': stage_tags, 'maximum': execution_tags(ranks, nodes), 'rule': EXECUTION_TAGS_RULE,
            'note': ('SCF 与能带的网格/路径在启动时才确定，标签按同一规则在启动时推导；' if phase in ('scf', 'bands')
                     else 'DOS 网格由 SCF 证据在启动时生成，标签按同一规则在启动时推导；' if phase == 'dos' else '')
                    + '实际值写入各阶段 INCAR 和 execution.json；其余 INCAR 参数仍须与审阅方案逐项一致。'}


def split_execution(incar):
    """(physics, execution tags) of an INCAR mapping."""
    data = dict(incar)
    return {k: v for k, v in data.items() if k not in EXECUTION_TAGS}, {k: data[k] for k in EXECUTION_TAGS if k in data}


def incar_matches(folder, config, physics):
    """A stage INCAR on disk is the given physics plus exactly its recorded tags."""
    actual, tags = split_execution(Incar.from_file(folder / 'INCAR'))
    if actual != dict(Incar(physics)):
        return False
    if config.get('execution_tags') != EXECUTION_TAGS_VERSION or not (folder / 'execution.json').is_file():
        return not tags
    return tags == json.loads((folder / 'execution.json').read_text(encoding='utf-8'))['tags']


def relaxation_grids(config):
    """The KPOINTS grid each relaxation stage will use.

    Shared between the reviewed plan and the run so the two cannot drift. Only
    the stages whose grid follows from the prepared config appear here: the SCF
    and bands grids follow from the optimized cell, which does not exist yet, so
    no value can be pre-registered for them -- see plan_kpoints.
    """
    from vasp_agent.workflow.convergence_reuse import production_mesh
    from vasp_agent.workflow.dimensionality import VACUUM_AXIS, is_2d
    mesh = production_mesh(config)
    fixed = (VACUUM_AXIS,) if is_2d(config) else ()
    return {'relax-1': mesh, 'relax-2': mesh, 'validation/reference': mesh,
            'validation/higher-cutoff': mesh,
            'validation/denser-mesh': [n if axis in fixed else int(np.ceil(n * 1.25))
                                       for axis, n in enumerate(mesh)]}


def plan_kpoints(config):
    """KPOINTS text for the stages that are determined before any output exists.

    Returns nothing for scf and bands on purpose. Their grids are derived at run
    time from the standardized primitive cell of the optimized structure, which
    is not knowable while the operator is reviewing the plan, so recording a
    value here would be inventing one. Those two are covered instead by their own
    evidence: the SCF mesh is written to scf-summary.json and read back by the
    SCF and bands stage reviews, and bands/KPOINTS is hashed into the evidence
    the bands review verifies. The convergence KPOINTS is verified against the
    confirmed scan plan by scan_config.verify_scan_inputs.
    """
    return {name: str(Kpoints.gamma_automatic(grid)) for name, grid in relaxation_grids(config).items()}


def metrics(result):
    step = result.ionic_steps[-1]
    forces = np.asarray(step['forces'], dtype=float)
    stress = np.asarray(step['stress'], dtype=float)
    energy = float(result.final_energy) / len(result.final_structure)
    if forces.shape != (len(result.final_structure), 3) or stress.shape != (3, 3):
        raise ValueError('输出缺少完整力/应力数据。')
    if not np.isfinite(energy) or not np.all(np.isfinite(forces)) or not np.all(np.isfinite(stress)):
        raise ValueError('输出含非有限能量、力或应力。')
    return {'energy_per_atom': energy, 'forces': forces.tolist(), 'stress_kbar': stress.tolist(),
            'max_force': float(np.linalg.norm(forces, axis=1).max()),
            'max_stress_kbar': float(np.abs(stress).max())}


# Upper bounds for operator-set scan thresholds; beyond these a "converged" scan
# would no longer mean anything for production settings.
SCAN_THRESHOLD_LIMITS = {'energy_eV_per_atom': 0.01, 'force_eV_per_A': 0.05, 'stress_kbar': 10.0}
SCAN_THRESHOLDS_VERSION = 2
# Named threshold sets chosen by purpose. 'strict' is the historical default
# (energy_tol, force_tol/2, stress_tol/2) and what tasks without a preset use.
# 'standard' suits electronic-structure goals (bands, DOS, Wannier): forces and
# stress only position atoms there, and Pulay stress converges very slowly.
SCAN_PRESETS = {'standard': {'force_eV_per_A': 0.01, 'stress_kbar': 2.0}, 'strict': {}}


def validate_scan_preset(value):
    if value is None:
        return None
    if value not in SCAN_PRESETS:
        raise ValueError('扫描判据档位须为 standard（标准）或 strict（严格）。')
    return value


def validate_scan_thresholds(value):
    """Operator-confirmed overrides for scan-difference thresholds, or None."""
    if value is None:
        return None
    if not isinstance(value, dict):
        raise ValueError('扫描阈值必须是对象。')
    cleaned = {}
    for key, item in value.items():
        if item is None:
            continue
        if key not in SCAN_THRESHOLD_LIMITS:
            raise ValueError(f'未知扫描阈值 {key}；只支持 ' + ' / '.join(SCAN_THRESHOLD_LIMITS) + '。')
        if type(item) not in (int, float) or not np.isfinite(item) or not 0 < item <= SCAN_THRESHOLD_LIMITS[key]:
            raise ValueError(f'{key} 须为 0–{SCAN_THRESHOLD_LIMITS[key]} 之间的正数。')
        cleaned[key] = float(item)
    return cleaned or None


def scan_thresholds(config):
    """Effective scan-difference thresholds and, per item, where each one comes from.

    Defaults are energy_tol and half the relaxation force/stress tolerances; an
    operator override replaces single items and never touches the relaxation
    tolerances themselves.
    """
    thresholds = {'energy_eV_per_atom': config['energy_tol'],
                  'force_eV_per_A': config['force_tol'] / 2,
                  'stress_kbar': config['stress_tol'] / 2}
    sources = {name: 'default' for name in thresholds}
    preset = config.get('scan_preset')
    for name, value in SCAN_PRESETS.get(preset, {}).items():
        thresholds[name] = value
        sources[name] = f'preset:{preset}'
    for name, value in (config.get('scan_thresholds') or {}).items():
        thresholds[name] = value
        sources[name] = 'operator'
    return thresholds, sources


def compare(a, b, config):
    delta = dict(energy_eV_per_atom=abs(a['energy_per_atom'] - b['energy_per_atom']),
                 force_eV_per_A=float(np.linalg.norm(np.array(a['forces']) - b['forces'], axis=1).max()),
                 stress_kbar=float(np.abs(np.array(a['stress_kbar']) - b['stress_kbar']).max()))
    # Scan differences use half the relaxation force/stress tolerances unless the
    # operator set a scan threshold; the legacy delta fields stay unchanged.
    thresholds, sources = scan_thresholds(config)
    units = {'energy_eV_per_atom': 'eV/atom', 'force_eV_per_A': 'eV/Å',
             'stress_kbar': 'kbar'}
    checks = {}
    for name, value in delta.items():
        enabled = name != 'stress_kbar' or config['relax_mode'] == 'cell'
        checks[name] = {'value': value, 'threshold': thresholds[name],
                        'unit': units[name], 'enabled': enabled,
                        'passed': bool(value <= thresholds[name]) if enabled else None}
    failed = [name for name, check in checks.items()
              if check['enabled'] and not check['passed']]
    operator = 'operator' in sources.values()
    preset = config.get('scan_preset')
    policy = ('operator_scan_thresholds_v1' if operator else
              f'scan_preset_{preset}_v1' if preset == 'standard' else 'scan_difference_half_force_stress_v1')
    return {'passed': not failed, **delta,
            'comparison_policy': policy,
            'effective_thresholds': thresholds, 'checks': checks,
            **({'threshold_sources': sources} if operator or preset == 'standard' else {}),
            'failed_checks': failed,
            'failure_reasons': [f'{name}: {delta[name]:.8g} > {thresholds[name]:.8g} {units[name]}'
                                for name in failed]}


def check_relaxation(result, config):
    data = metrics(result)
    if not result.converged_ionic:
        raise ValueError('结构优化未收敛；禁止进入 SCF/能带计算。')
    # Every ionic step must have converged its electronic problem.
    if any(len(step['electronic_steps']) >= int(result.parameters['NELM']) for step in result.ionic_steps):
        raise ValueError('优化过程中存在未可靠收敛的电子步骤。')
    if data['max_force'] > config['force_tol']:
        raise ValueError('优化后最大原子力超标。')
    if config['relax_mode'] == 'cell' and data['max_stress_kbar'] > config['stress_tol']:
        raise ValueError('优化后残余应力超标。')
    return data


RESOURCE_LAYOUT_VERSION = 2


def execution_command(config, command):
    parallel = config.get('parallel_tests', 1)
    nodes = config.get('nodes', 1)
    cores = config.get('cores_per_test', 8)
    if parallel > 1 or nodes > 1:
        if type(nodes) is not int or not 1 <= nodes <= 256 or type(cores) is not int or cores < nodes or cores % nodes:
            raise ValueError('多节点 MPI 布局无效，每组进程数必须为节点数的整数倍。')
        if command != ['srun', 'vasp_std']:
            raise ValueError('并行测试只支持受控启动命令 srun vasp_std。')
        if not os.environ.get('SLURM_JOB_ID') or int(os.environ.get('SLURM_NTASKS', '0')) < parallel * cores:
            raise ValueError('Slurm 已分配总核数不足，禁止并行超额运行。')
        if nodes > 1 and int(os.environ.get('SLURM_JOB_NUM_NODES', os.environ.get('SLURM_NNODES', '0'))) != nodes:
            raise ValueError('Slurm 实际分配节点数与配置不一致。')
        command = ['srun', '--exclusive', '--exact', f'--nodes={nodes}', f'--ntasks={cores}', '--cpus-per-task=1']
        if nodes > 1:
            command += [f'--ntasks-per-node={cores // nodes}']
        command += ['vasp_std']
    return command


def convergence_tests(config):
    """Frozen scan order, including distinct stages with identical highest inputs."""
    from vasp_agent.workflow.scan_config import validate_scans
    from vasp_agent.workflow.convergence_reuse import fixed_cutoff
    validate_scans(config['encuts'], config['meshes'])
    active = config.get('active_scans', ['encut', 'mesh'])
    if active not in (['encut'], ['mesh'], ['encut', 'mesh']):
        raise ValueError('扫描执行清单无效。')
    encut, mesh = fixed_cutoff(config), config['meshes'][-1]
    tests = []
    for kind, values in [('encut', config['encuts']), ('mesh', config['meshes'])]:
        if kind not in active:
            continue
        for index, value in enumerate(values):
            tests.append({'task_id': len(tests), 'stage': f'convergence/{kind}-{index}',
                'kind': kind, 'index': index, 'encut': value if kind == 'encut' else encut,
                'mesh': list(value if kind == 'mesh' else mesh)})
    if config.get('fixed_encut') is not None:
        tests.append({'task_id': len(tests), 'stage': 'convergence/cross-cutoff',
            'kind': 'cross_cutoff', 'index': None, 'encut': config['encuts'][-1], 'mesh': list(mesh)})
    return tests


def convergence_tasks(root, config, initial, command):
    return [(root, test['stage'], initial, base_incar(config, test['encut']),
             Kpoints.gamma_automatic(test['mesh']), command) for test in convergence_tests(config)]


def save_partial_convergence(root, config):
    from vasp_agent.workflow.convergence_reuse import scan_result, reused_result
    partial = {}
    for kind in ('encut', 'mesh'):
        try:
            if kind not in config.get('active_scans', ['encut', 'mesh']):
                _, partial[kind] = reused_result(root)
            else:
                partial[kind] = scan_result(root, kind)
        except Exception as exc:
            partial[kind] = {'status': 'unavailable', 'reason': str(exc)}
    core.save(root / 'convergence.json', partial)
    return partial


def accept_convergence(root, config, samples_all):
    """Shared allocation/array scientific acceptance; callers supply validated outputs."""
    tests = convergence_tests(config)
    if len(samples_all) != len(tests):
        raise ValueError('收敛测试输出数量与冻结方案不一致。')
    by_stage = {test['stage']: sample for test, sample in zip(tests, samples_all)}
    convergence = {}
    if (root / 'convergence-reuse.json').exists():
        from vasp_agent.workflow.convergence_reuse import reused_result
        kind, result = reused_result(root)
        convergence[kind] = result
    for kind in config.get('active_scans', ['encut', 'mesh']):
        samples = [by_stage[f'convergence/{kind}-{index}'] for index in range(3)]
        comparisons = [compare(samples[i], samples[i + 1], config) for i in (0, 1)]
        convergence[kind] = {'values': config['encuts' if kind == 'encut' else 'meshes'],
            'samples': samples, 'comparisons': comparisons,
            'status': 'passed' if all(x['passed'] for x in comparisons) else 'failed', 'origin': 'computed'}
    if config.get('fixed_encut') is not None:
        from vasp_agent.workflow.convergence_reuse import fixed_cutoff
        samples = [by_stage['convergence/mesh-2'], by_stage['convergence/cross-cutoff']]
        comparison = compare(*samples, config)
        convergence['cross_cutoff'] = {'values': [fixed_cutoff(config), config['encuts'][-1]],
            'mesh': config['meshes'][-1], 'samples': samples, 'comparisons': [comparison],
            'status': 'passed' if comparison['passed'] else 'failed', 'origin': 'computed'}
    core.save(root / 'convergence.json', convergence)
    for kind, scan in convergence.items():
        if not all(x['passed'] for x in scan['comparisons']):
            raise ValueError('固定ENCUT在新网格的交叉精度复核失败；停止优化，请在新任务调整参数。' if kind == 'cross_cutoff' else f'{kind} 扫描未达到连续两级收敛；请扩大扫描起点后新建任务。')
    return convergence
