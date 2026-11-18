"""Preflight, bounded convergence studies, two-pass relaxation and bands."""
import json
import hashlib
import os
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
import shutil
from pathlib import Path

import numpy as np
from pymatgen.core import Structure
from pymatgen.io.vasp.inputs import Incar, Kpoints, Poscar, Potcar
from pymatgen.symmetry.bandstructure import HighSymmKpath

import vasp_agent.workflow.vasp_band_agent as core
from vasp_agent.workflow import stage_definitions as stages


SCAN_PLAN_VERSION = 1
POTENTIAL_VARIANTS_VERSION = 1
ARRAY_EXECUTION_VERSION = 1
# 1: config['band_path'] (operator k-path, band_path.py) is honoured at the bands
# stage. Engines without it would silently run the automatic path, so the worker
# refuses a band_path for them. 2: band_path.candidates_for_task (path menu).
BAND_PATH_VERSION = 2
# 1: config['environment_profile'] records the operator-confirmed environment
# profile (name, script and executable hashes, capability tags) a task runs with;
# prepare_full refuses one that lacks a capability the enabled stages need. The
# worker refuses a profile for engines without this marker.
ENVIRONMENT_PROFILES_VERSION = 1
# 1: optional stages with operator-chosen parameters (parameter_specs): the
# preview describes each requested stage's spec, prepare_full validates the
# chosen values again and records them in config['stage_parameters']. The worker
# refuses optional stages for engines without this marker.
PARAMETER_SPECS_VERSION = 1

_stage_context = threading.local()


def prepare_full(args):
    root = Path(args.workdir).resolve()
    if root.exists() and any(root.iterdir()):
        raise ValueError('工作目录非空，请使用新目录。')
    for name in ('encut', 'ediff', 'symprec', 'force_tol', 'stress_tol', 'energy_tol', 'nsw'):
        if not np.isfinite(getattr(args, name)) or getattr(args, name) <= 0:
            raise ValueError(f'{name} 必须是正数。')
    if min(args.mesh) < 1 or args.points < 2 or (args.nbands is not None and args.nbands < 1):
        raise ValueError('网格、路径点数或 NBANDS 无效。')
    parallel_tests = getattr(args, 'parallel_tests', 1)
    cores_per_test = getattr(args, 'cores_per_test', 8)
    submission_mode = getattr(args, 'submission_mode', 'allocation')
    if submission_mode not in ('allocation', 'array'):
        raise ValueError('submission_mode 必须为 allocation 或 array。')
    if submission_mode == 'array' and not getattr(args, 'stage_gates', True):
        raise ValueError('作业数组仅支持逐阶段审阅的收敛测试。')
    if type(parallel_tests) is not int or not 1 <= parallel_tests <= 6 or type(cores_per_test) is not int or not 1 <= cores_per_test <= 256 or parallel_tests * cores_per_test > 256:
        raise ValueError('并发数须为 1–6，每个测试核数须为 1–256，总核数不超过 256。')
    structure = Structure.from_file(args.structure)
    if not structure.is_ordered or structure.site_properties:
        raise ValueError('仅支持有序、无站点属性的非磁性结构；请先审核占位、磁矩和约束。')
    if structure.volume <= 0 or not np.all(np.isfinite(structure.lattice.matrix)):
        raise ValueError('无效晶胞。')
    if any(structure.get_neighbors(site, 0.5) for site in structure):
        raise ValueError('存在小于 0.5 Å 的周期原子间距。')
    from vasp_agent.workflow import dimensionality
    dimension = dimensionality.require_supported(dimensionality.classify(structure))
    slab = dimension['dimension'] == '2D'
    if slab and args.relax_mode == 'cell':
        raise ValueError('二维 slab 不支持 relax_mode=cell：ISIF=3 会同时优化 c 轴并压缩真空层。'
                         '请使用 relax_mode=ions，并说明面内晶格常数的来源。')
    if slab:
        dimensionality.check_meshes([args.mesh])
    # Retain the supplied cell for relaxation; standardize only after it converges.
    poscar = Poscar(structure)
    encut = args.encut
    potential_floor = 0.
    library = getattr(args, 'potcar_library', None)
    variants = getattr(args, 'potential_variants', None)
    if variants and not library:
        raise ValueError('显式赝势变体要求配置赝势库，不支持覆盖合并 POTCAR。')
    if library and args.potcar:
        raise ValueError('--potcar 与 --potcar-library 不能同时使用。')
    assembled, selection = None, None
    if library:
        from vasp_agent.workflow.potential_library import assemble_potentials
        assembled, selection = assemble_potentials(library, poscar.site_symbols, variants)
    if args.potcar or assembled:
        pots = Potcar.from_str(assembled.decode('utf-8')) if assembled else Potcar.from_file(args.potcar)
        if [p.element for p in pots] != poscar.site_symbols:
            raise ValueError(f'POTCAR 必须按输入结构顺序 {poscar.site_symbols} 提供。')
        if any(p.functional != 'PBE' for p in pots):
            raise ValueError('仅支持 PBE POTCAR。')
        factor = 1.3 if args.relax_mode == 'cell' else 1.0
        potential_floor = float(np.ceil(factor * max(float(p.enmax) for p in pots)))
        encut = max(encut, potential_floor)
    config = {name: getattr(args, name) for name in
              ('ediff', 'points', 'symprec', 'nbands', 'relax_mode', 'force_tol',
               'stress_tol', 'energy_tol', 'nsw')}
    config.update(dimensionality=dimension)
    band_path = getattr(args, 'band_path', None)
    path_check = None
    if band_path is not None:
        from vasp_agent.workflow import band_path as band_paths
        if slab:
            raise ValueError('二维 slab 的能带晶胞未标准化，只支持自动面内路径；未创建任务。')
        config['band_path'] = band_paths.validate(band_path, args.points)
        # Labels must exist for this lattice now; the relaxed cell decides finally.
        path_check = band_paths.provisional(config['band_path'], structure, args.symprec)
    thresholds = validate_scan_thresholds(getattr(args, 'scan_thresholds', None))
    preset = validate_scan_preset(getattr(args, 'scan_preset', None))
    config.update(parallel_tests=parallel_tests, cores_per_test=cores_per_test, annotated_inputs=True,
                  stage_gates=getattr(args, "stage_gates", True), submission_mode=submission_mode,
                  execution_tags=EXECUTION_TAGS_VERSION)
    from vasp_agent.workflow.scan_config import mesh_ladder, validate_scans, verify_scan_inputs
    config.update(encuts=[float(np.ceil(encut * f)) for f in (1, 1.15, 1.3)],
                  meshes=mesh_ladder([int(n) for n in args.mesh],
                                     fixed_axes=(dimensionality.VACUUM_AXIS,) if slab else ()))
    defaults = validate_scans(config['encuts'], config['meshes'])
    potential_bytes = assembled if assembled else Path(args.potcar).read_bytes() if args.potcar else b''
    preview = {'defaults': defaults, 'potential_floor': potential_floor,
        'base_config': config.copy(), 'structure_sha256': hashlib.sha256(str(poscar).encode()).hexdigest(),
        'potential_sha256': hashlib.sha256(potential_bytes).hexdigest(),
        'engine_sha256': {name: core.digest(Path(__file__).with_name(name)) for name in ('full_workflow.py', 'scan_config.py', 'input_review.py')}}
    if path_check:
        preview['band_path'] = path_check
    if variants:
        preview['potential_variants'] = variants
        preview['potential_selection'] = selection
        electrons = sum(float(p.ZVAL) * n for p, n in zip(pots, poscar.natoms))
        minimum_bands = int(np.ceil(electrons / 2))
        preview['band_capacity'] = {'valence_electrons': electrons,
            'minimum_occupied_bands': minimum_bands, 'requested_nbands': args.nbands,
            'note': '非磁性中性体系最低占据容量；仍须预留空带并检查最高能带占据。'}
        if args.nbands is not None and args.nbands < minimum_bands:
            raise ValueError(f'NBANDS={args.nbands} 不足以容纳新赝势的价电子，至少需要 {minimum_bands}，且应预留空带。')
    preview['signature'] = hashlib.sha256(json.dumps(preview, sort_keys=True).encode()).hexdigest()
    optional = tuple(getattr(args, 'optional_stages', None) or ())
    spec_context = {'dimension': dimension['dimension']}
    if optional:
        from vasp_agent.workflow import parameter_specs
        # After the signature, which covers only the defaults: the chosen values
        # are bound by the plan's choice and validated again below.
        preview['stage_specs'] = {name: parameter_specs.describe(parameter_specs.for_stage(name), spec_context)
                                  for name in optional}
    if getattr(args, 'preview_only', False):
        return preview
    selected = validate_scans(defaults['encuts'] if getattr(args, 'encuts', None) is None else args.encuts,
                              defaults['meshes'] if getattr(args, 'meshes', None) is None else args.meshes)
    if slab:
        dimensionality.check_meshes(selected['meshes'])
    if selected['encuts'][0] < potential_floor:
        raise ValueError(f'指定 ENCUT 起点低于当前赝势/优化策略要求 {potential_floor} eV；不会静默改值。')
    choice = getattr(args, 'parameter_choice', None)
    if choice is not None and (choice['preview_signature'] != preview['signature']
                               or {k: choice['selected'][k] for k in ('encuts', 'meshes')} != selected
                               or validate_scan_thresholds(choice['selected'].get('scan_thresholds')) != thresholds):
        raise ValueError('结构、赝势、默认配置或最终方案已变化，请重新提出并确认参数方案。')
    config.update(selected)
    if thresholds:
        # Operator-confirmed scan thresholds are part of the chosen plan, not of
        # the defaults the preview signature covers; relaxation tolerances unchanged.
        config['scan_thresholds'] = thresholds
    if preset:
        config['scan_preset'] = preset
    reuse_kind = getattr(args, 'reuse_kind', None)
    if reuse_kind:
        config['active_scans'] = ['mesh' if reuse_kind == 'encut' else 'encut']
    if getattr(args, 'fixed_encut', None) is not None:
        from vasp_agent.workflow.convergence_reuse import fixed_cutoff
        config['fixed_encut'] = args.fixed_encut
        if reuse_kind != 'encut' or fixed_cutoff(config) < potential_floor:
            raise ValueError('固定ENCUT要求ENCUT复用证据且不得低于赝势策略下限。')
    # The enabled stages and their definition versions, recorded after the
    # preview signature (which covers only the defaults) and before the config
    # is hashed into the manifest: the legacy chain plus any optional stages.
    config['stages'] = stages.record(stage_selection(optional))
    stage_parameters = recorded_stage_parameters(config, getattr(args, 'stage_parameters', None), spec_context)
    if choice is not None and (choice.get('stage_parameters') or {}) != stage_parameters:
        raise ValueError('阶段参数与已确认的方案不一致，请重新提出并确认参数方案。')
    if stage_parameters:
        config['stage_parameters'] = stage_parameters
    profile = getattr(args, 'environment_profile', None)
    if profile is not None:
        # Recorded like the stages: after the preview signature, before the
        # manifest hash, so every review snapshot covers the binding.
        config['environment_profile'] = profile
    missing = stages.unmet(stages.required_capabilities(config['stages']),
                           profile['capabilities'] if profile is not None else ())
    if missing:
        raise ValueError(f"需要 {'、'.join(missing)}，"
                         + (f"环境配置 {profile['name']} 不满足" if profile is not None else '默认环境未登记该能力')
                         + '；未创建任务。')
    root.mkdir(parents=True, exist_ok=True)
    poscar.write_file(root / 'initial.POSCAR')
    core.save(root / 'config.json', config)
    files = ['initial.POSCAR', 'config.json']
    if choice is not None:
        core.save(root / 'parameter-choice.json', choice)
        files.append('parameter-choice.json')
    if args.potcar:
        shutil.copyfile(args.potcar, root / 'POTCAR')
        files.append('POTCAR')
    elif assembled:
        (root / 'POTCAR').write_bytes(assembled)
        core.save(root / 'potential-selection.json', selection)
        files.extend(['POTCAR', 'potential-selection.json'])
    from vasp_agent.workflow.input_review import prepare_plan
    if reuse_kind:
        from vasp_agent.workflow.convergence_reuse import freeze
        files.extend(freeze(args.reuse_source, root, reuse_kind, config))
    files.extend(prepare_plan(root, config, structure, {'encut': args.encut, 'mesh': args.mesh, 'nbands': args.nbands, 'relax_mode': args.relax_mode,
        'final_encuts': config['encuts'], 'final_meshes': config['meshes'],
        'selection_source': choice['choice'] if choice else 'code_default_or_explicit_cli'}))
    core.save(root / 'scan-verification.json', verify_scan_inputs(root))
    files.append('scan-verification.json')
    manifest = dict(stage_gates=config["stage_gates"], review_required=True, schema=2, workflow='full', ready=bool(args.potcar or assembled),
                    formula=structure.composition.reduced_formula,
                    potential_order=poscar.site_symbols,
                    scope=dimension['dimension'] + ' nonmagnetic PBE; convergence, relaxation, SCF, bands',
                    hashes={name: core.digest(root / name) for name in files})
    core.save(root / 'manifest.json', manifest)
    core.save(root / 'workflow-state.json', {'status': 'prepared', 'next': 'convergence'})
    core.event(root, 'prepared_full', config=config)
    print(json.dumps({'manifest': manifest, 'config': config}, indent=2, ensure_ascii=False))


def stage_selection(optional):
    """Stage names for a new task; a spec that is not executable yet is refused first, by name."""
    if optional:
        from vasp_agent.workflow import parameter_specs
        for name in optional:
            item = parameter_specs.for_stage(name)
            if not item['executable']:
                raise ValueError(f"可选阶段 {name} 尚不可执行：{item['not_executable_reason']}未创建任务。")
    return stages.with_optional(optional)


def recorded_stage_parameters(config, chosen, context):
    """Validate the chosen values of every enabled stage with a parameter spec (execution side)."""
    from vasp_agent.workflow import parameter_specs
    chosen = chosen or {}
    needed = stages.parameter_stages(config)
    if not isinstance(chosen, dict) or set(chosen) != set(needed):
        raise ValueError('阶段参数须且只须覆盖启用的参数化阶段：' + ('、'.join(needed) or '无') + '。')
    recorded = {}
    for name in needed:
        item, values = parameter_specs.check_record(name, chosen[name], context)
        if stages.definition(config, name)['parameter_spec'] != parameter_specs.reference(item):
            raise ValueError(f'阶段 {name} 的定义引用的参数规范版本与所选不同，请重新提出方案。')
        recorded[name] = {'spec': parameter_specs.reference(item), 'values': values}
    return recorded


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
    stages = {}
    if phase == 'convergence':
        stages = {test['stage']: execution_tags(ranks, nodes, Kpoints.gamma_automatic(test['mesh']))
                  for test in convergence_tests(config)}
    elif phase == 'relaxation':
        stages = {name: execution_tags(ranks, nodes, Kpoints.gamma_automatic(grid))
                  for name, grid in relaxation_grids(config).items()}
    return {'enabled': True, 'allowlist': list(EXECUTION_TAGS), 'mpi_ranks_per_test': ranks, 'nodes': nodes,
            'stages': stages, 'maximum': execution_tags(ranks, nodes), 'rule': EXECUTION_TAGS_RULE,
            'note': ('SCF 与能带的网格/路径在启动时才确定，标签按同一规则在启动时推导；' if phase in ('scf', 'bands') else '')
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


def stage(root, name, structure, incar, kpoints, command, charge=None, event_root=None):
    if (root / 'manifest.json').exists():
        from vasp_agent.workflow.stage_gate import enabled, authorized_phase, check_stage
        if enabled(root):
            check_stage(root, name, authorized_phase(root))
    folder = root / name
    folder.mkdir(parents=True, exist_ok=True)
    incar_text = str(Incar(incar))
    plan_path = root / 'input-plan.json'
    if plan_path.exists():
        from vasp_agent.workflow.input_review import annotated
        plan = json.loads(plan_path.read_text(encoding='utf-8'))
        if name not in plan['incars'] or dict(Incar(plan['incars'][name])) != dict(Incar(incar)):
            raise ValueError(f'{name} 参数偏离已审阅的方案，禁止启动。')
        # The INCAR check above covered half the reviewed plan; the sampling was
        # never compared, so a relaxation stage could run on a grid the operator
        # never saw. Only the stages whose grid the plan records are checked --
        # an absent entry means the grid is legitimately derived later, not that
        # it was reviewed and matches.
        recorded = plan.get('kpoints', {}).get(name)
        if recorded is not None and str(kpoints) != recorded:
            raise ValueError(f'{name} 的 KPOINTS 偏离已审阅的方案，禁止启动。')
        incar_text = annotated(incar)
    # Execution tags are appended after the reviewed keys, never merged into them:
    # the comparison above stays on the physics alone, and any key outside the
    # allowlist -- or an allowlisted key already in the physics -- is refused.
    config_path = root / 'config.json'
    execution = stage_execution(json.loads(config_path.read_text(encoding='utf-8')), kpoints) if config_path.exists() else None
    launched = any((folder / f).exists() for f in ('complete.json', 'vasprun.xml', 'OUTCAR', 'stdout.log'))
    record = folder / 'execution.json'
    if execution is not None:
        if any(key in dict(Incar(incar)) for key in EXECUTION_TAGS):
            raise ValueError(f'{name} 的物理参数不得包含并行标签 {"/".join(EXECUTION_TAGS)}。')
        if launched and not record.is_file() and (folder / 'complete.json').exists():
            # A completed stage copied into a fork from a source without tags ran
            # without them; it is checked byte for byte as it ran, tag lines refused.
            execution = None
        elif launched:
            # A stage that already ran keeps the tags it ran with; later phases may
            # use other resources without rewriting completed inputs.
            if not record.is_file():
                raise ValueError(f'{name} 已启动但缺少执行标签记录，不能核对输入。')
            execution = json.loads(record.read_text(encoding='utf-8'))
            if set(execution.get('tags', {})) != set(EXECUTION_TAGS):
                raise ValueError(f'{name} 执行标签记录无效。')
    if execution is not None:
        incar_text = annotated({**incar, **execution['tags']}) if plan_path.exists() else str(Incar({**incar, **execution['tags']}))
    expected = {'POSCAR': str(Poscar(structure)), 'INCAR': incar_text, 'KPOINTS': str(kpoints)}
    for filename, content in expected.items():
        target = folder / filename
        if target.exists() and target.read_text(encoding='utf-8') != content:
            # A prepared or never-launched INCAR may only gain or change the tag
            # lines derived for the current resources; every other line must
            # already match byte for byte.
            if not (execution is not None and filename == 'INCAR' and not launched
                    and core.without_execution_lines(target.read_text(encoding='utf-8'))
                    == core.without_execution_lines(content)):
                raise ValueError(f'{name}/{filename} 与当前工作流不一致。')
            target.write_text(content, encoding='utf-8')
        if not target.exists():
            target.write_text(content, encoding='utf-8')
    if execution is not None and not launched:
        core.save(record, execution)
    for filename, source in [('POTCAR', root / 'POTCAR')] + ([('CHGCAR', charge)] if charge else []):
        target = folder / filename
        if not target.exists():
            shutil.copyfile(source, target)
        if core.digest(source) != core.digest(target):
            raise ValueError(f'{name}/{filename} 与上游不一致。')
    hashes = {f: core.digest(folder / f) for f in (*expected, 'POTCAR')}
    provenance = folder / 'inputs.json'
    # Only a never-launched stage of a tagged task may re-record its inputs (new tags).
    if provenance.exists() and (launched or execution is None) and json.loads(provenance.read_text()) != hashes:
        raise ValueError(f'{name} 输入校验失败。')
    core.save(provenance, hashes)
    if not getattr(_stage_context, 'parallel', False):
        core.save(root / 'workflow-state.json', {'status': 'running', 'stage': name})
    if event_root is not None:
        return core.execute(root, name, command, event_root=event_root)
    return core.execute(root, name, command)


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


def run_stages(root, group, tasks, parallel):
    """Independent stages; one coordinator writes shared state, joins all running work."""
    if parallel == 1:
        return [metrics(stage(*task)) for task in tasks]
    states = {task[1]: 'pending' for task in tasks}
    lock = threading.Lock()
    def record(name, status):
        with lock:
            states[name] = status
            core.save(root / 'workflow-state.json', {
                'status': 'running', 'stage': group, 'parallel_tests': parallel, 'stages': dict(states)})
    def work(task):
        _stage_context.parallel = True
        record(task[1], 'running')
        try:
            value = metrics(stage(*task))
            record(task[1], 'complete')
            return value
        except Exception:
            record(task[1], 'failed')
            raise
        finally:
            _stage_context.parallel = False
    results = [None] * len(tasks)
    failure = None
    with ThreadPoolExecutor(max_workers=parallel) as pool:
        futures = {pool.submit(work, task): i for i, task in enumerate(tasks)}
        for future in as_completed(futures):
            index = futures[future]
            if future.cancelled():
                record(tasks[index][1], 'cancelled')
                continue
            try:
                results[index] = future.result()
            except Exception as exc:
                if failure is None:
                    failure = exc
                    for pending in futures:
                        pending.cancel()
    if failure is not None:
        raise failure
    return results


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


# Runners for stages after SCF other than bands, by a definition's
# execution['runner']. Empty in this engine: a task enabling such a stage is
# refused at its run, never silently skipped. Each runner receives the verified
# products of the base stages and must run, check and record its own stage.
STAGE_RUNNERS = {}


def run_full(root, command):
    core.verify(root)
    config = json.loads((root / 'config.json').read_text())
    if config.get('submission_mode', 'allocation') == 'array':
        raise ValueError('数组模式必须使用 run-array-test；普通 run 禁止启动数组测试。')
    gated = config.get('stage_gates', False)
    stages.require_runnable(config, gated)
    target = None
    if gated:
        import vasp_agent.workflow.stage_gate as stage_gate
        target = stage_gate.require_review(root, stage_gate.authorized_phase(root))
    initial = Poscar.from_file(root / 'initial.POSCAR').structure
    # Production ENCUT: the highest tested value unless the operator chose a tested
    # value the scan showed equivalent (prepare_production); the scan keeps its own.
    from vasp_agent.workflow.convergence_reuse import production_cutoff
    encut, mesh = production_cutoff(config), config['meshes'][-1]
    parallel = config.get('parallel_tests', 1)
    command = execution_command(config, command)
    tasks = convergence_tasks(root, config, initial, command)
    try:
        samples_all = run_stages(root, 'convergence', tasks, parallel)
    except Exception:
        save_partial_convergence(root, config)
        raise
    accept_convergence(root, config, samples_all)
    if gated and target == 'convergence':
        stage_gate.complete(root, target)
        return
    # The scan grid above stays unchanged; production may use an explicitly tested grid.
    from vasp_agent.workflow.convergence_reuse import production_mesh
    mesh = production_mesh(config)
    relax_grids = relaxation_grids(config)
    relaxation = []
    structure = initial
    for index in (1, 2):
        incar = {**base_incar(config, encut), 'IBRION': 2, 'NSW': config['nsw'],
                 'ISIF': 3 if config['relax_mode'] == 'cell' else 2,
                 'EDIFFG': -config['force_tol'], 'POTIM': 0.5}
        result = stage(root, f'relax-{index}', structure, incar,
                       Kpoints.gamma_automatic(relax_grids[f'relax-{index}']), command)
        try:
            relaxation.append(check_relaxation(result, config))
        except ValueError as exc:
            core.save(root / 'relaxation-diagnostic.json', {
                'stage': f'relax-{index}', 'error': str(exc), 'metrics': metrics(result),
                'initial_lattice': structure.lattice.matrix.tolist(),
                'final_lattice': result.final_structure.lattice.matrix.tolist(),
                'ionic_steps': len(result.ionic_steps), 'converged_ionic': bool(result.converged_ionic),
                'next_step': '比较各方向晶格变化及运行环境补丁；不能仅增加NSW或放宽阈值。'})
            raise
        structure = result.final_structure
        # Archive geometry from validated XML, never accept an unchecked CONTCAR.
        Poscar(structure).write_file(root / f'relaxed-{index}.POSCAR')
    core.save(root / 'relaxation.json', relaxation)
    if gated:
        core.save(root / 'geometry-summary.json', {'initial_volume': initial.volume, 'optimized_volume': structure.volume,
            'relative_volume_change': structure.volume / initial.volume - 1,
            'initial_lattice': initial.lattice.matrix.tolist(), 'optimized_lattice': structure.lattice.matrix.tolist(),
            'optimized_poscar': str(Poscar(structure))})
    # Verify convergence again at the optimized geometry against stricter settings.
    tasks = []
    # Grids come from relaxation_grids so the run uses exactly the values the
    # reviewed plan recorded, rather than a second copy of the same formula.
    grids = relaxation_grids(config)
    for name, cut, grid in [('reference', encut, grids['validation/reference']),
                            ('higher-cutoff', float(np.ceil(encut * 1.15)), grids['validation/higher-cutoff']),
                            ('denser-mesh', encut, grids['validation/denser-mesh'])]:
        tasks.append((root, f'validation/{name}', structure, base_incar(config, cut),
                      Kpoints.gamma_automatic(grid), command))
    checks = run_stages(root, 'validation', tasks, parallel)
    post = [compare(checks[0], checks[i], config) for i in (1, 2)]
    core.save(root / 'post-relax-validation.json', post)
    if not all(x['passed'] for x in post):
        raise ValueError('优化后更严格参数复核失败，请提高参数并重新优化。')
    if checks[0]['max_force'] > config['force_tol'] or (config['relax_mode'] == 'cell' and
                                                       checks[0]['max_stress_kbar'] > config['stress_tol']):
        raise ValueError('重建平面波基组后的静态力/应力超标，请继续优化。')
    if gated and target == 'relaxation':
        stage_gate.complete(root, target)
        return
    from vasp_agent.workflow.dimensionality import band_path_2d, is_2d
    if is_2d(config):
        # Keep the relaxed slab cell: standardization could move the vacuum off
        # c, and the relaxed grid (vacuum axis 1) already fits this cell.
        path = band_path_2d(structure)
        primitive, final_mesh = structure, list(mesh)
        sc_path = None
    else:
        path = sc_path = HighSymmKpath(structure, symprec=config['symprec'])
        primitive = path.prim
        # Standardization may reorder species; keep the original POTCAR grouping.
        original_pots = Potcar.from_file(root / 'POTCAR')
        by_element = {p.element: p for p in original_pots}
        if len(by_element) != len(original_pots):
            raise ValueError('同元素多种赝势目前不支持自动重排。')
        # Avoid changing the root's hashed POTCAR: final stages keep original species grouping.
        order = {symbol: i for i, symbol in enumerate(Poscar(initial).site_symbols)}
        primitive = primitive.get_sorted_structure(key=lambda site: order[site.specie.symbol])
        if Poscar(primitive).site_symbols != Poscar(initial).site_symbols:
            raise ValueError('标准原胞元素顺序与 POTCAR 不兼容。')
        # Preserve reciprocal-space sampling density if primitive-cell size changes.
        old_lengths = np.array(structure.lattice.reciprocal_lattice.abc)
        new_lengths = np.array(primitive.lattice.reciprocal_lattice.abc)
        spacing = float(np.min(old_lengths / np.array(mesh)))
        final_mesh = np.maximum(1, np.ceil(new_lengths / spacing).astype(int)).tolist()
    scf = stage(root, 'scf', primitive, {**base_incar(config, encut), 'LCHARG': True},
                Kpoints.gamma_automatic(final_mesh), command)
    scf_metrics = metrics(scf)
    if scf_metrics['max_force'] > config['force_tol'] or (config['relax_mode'] == 'cell' and
                scf_metrics['max_stress_kbar'] > config['stress_tol']):
        raise ValueError('标准原胞静态计算的力或应力超标。')
    if gated:
        core.save(root / 'scf-summary.json', {'metrics': scf_metrics, 'efermi': float(scf.efermi),
            'nbands': int(scf.parameters['NBANDS']), 'nelect': float(scf.parameters['NELECT']),
            'poscar': str(Poscar(primitive)), 'mesh': final_mesh})
        if target == 'scf':
            stage_gate.complete(root, target)
            return
    if gated and target != 'bands':
        # A later stage than SCF that is not bands: bands (if enabled) already
        # ran and is covered by this stage's review evidence; never redo it here.
        return run_stage(root, config, target, {'structure': structure, 'primitive': primitive,
                         'encut': encut, 'relax_mesh': mesh, 'scf_mesh': final_mesh, 'command': command})
    from vasp_agent.workflow import band_path as band_paths
    # The same record the bands review showed, resolved on the cell SCF ran in.
    record = band_paths.record_for(config, structure, primitive, sc_path)
    if gated and not band_paths.same(record, band_paths.files_record(root, config)):
        raise ValueError('能带路径与 bands 审阅时显示的不一致，禁止启动。')
    core.save(root / band_paths.RECORD, record)
    kpoints = (Kpoints.automatic_linemode(config['points'], path) if record['source'] == 'automatic'
               else band_paths.kpoints(record))
    stage(root, 'bands', primitive, {**base_incar(config, encut), 'ICHARG': 11},
          kpoints, command, root / 'scf/CHGCAR')
    core.save(root / 'workflow-state.json', {'status': 'calculated', 'selected_encut': encut,
              'relax_mesh': mesh, 'scf_mesh': final_mesh, 'band_path_source': record['source'],
              'kpath': [[p['kpoints_label'] for p in s['points']] for s in record['segments']]})
    analyze_full(root)
    if gated:
        stage_gate.complete(root, 'bands')


def run_stage(root, config, phase, products):
    """Run a reviewed stage after SCF through its definition's runner, then record it complete."""
    import vasp_agent.workflow.stage_gate as stage_gate
    runner = STAGE_RUNNERS.get(stages.definition(config, phase)['execution']['runner'])
    if runner is None:
        raise ValueError(f'本引擎没有阶段 {phase} 的执行实现；未启动计算，请部署支持该阶段的版本并新建任务。')
    runner(root, config, products)
    if stages.is_final(config, phase):
        state = json.loads((root / 'workflow-state.json').read_text(encoding='utf-8'))
        core.save(root / 'workflow-state.json', {**state, 'status': 'complete'})
        core.event(root, 'full_workflow_completed', final_stage=phase)
    stage_gate.complete(root, phase)


def bands_accepted(root):
    path = root / 'checkpoint.json'
    return path.is_file() and 'bands' in json.loads(path.read_text(encoding='utf-8')).get('completed', [])


def analyze_full(root):
    manifest = core.verify(root)
    state = json.loads((root / 'workflow-state.json').read_text())
    config = json.loads((root / 'config.json').read_text(encoding='utf-8'))
    # Bands is the end of the workflow only when it is the task's last stage;
    # with a later stage, the bands report is written but the task is not complete.
    final = stages.is_final(config, 'bands')
    if state['status'] not in ('calculated', 'complete') and (final or not bands_accepted(root)):
        raise ValueError('完整前置流程尚未验收，不能生成最终报告。')
    for name in ('scf', 'bands'):
        folder = root / name
        for filename, expected in json.loads((folder / 'inputs.json').read_text()).items():
            if core.digest(folder / filename) != expected:
                raise ValueError(f'{name}/{filename} 校验失败。')
        recorded = json.loads((folder / 'complete.json').read_text())
        if core.digest(folder / 'vasprun.xml') != recorded['xml_sha256']:
            raise ValueError('结果文件已改变。')
    scf = core.read_result(root / 'scf')
    bands = core.read_result(root / 'bands')
    validation = core.validate_band_data(bands)
    bs = bands.get_band_structure(kpoints_filename=str(root / 'bands/KPOINTS'),
                                  line_mode=True, efermi=float(scf.efermi))
    if not any(np.any(values > bs.efermi) for values in bs.bands.values()):
        raise ValueError('空带不足，请增加 --nbands。')
    path_record = root / 'bands-path.json'
    band_path = None
    if path_record.is_file():
        record = json.loads(path_record.read_text(encoding='utf-8'))
        band_path = {key: record[key] for key in ('source', 'convention', 'lattice_type', 'requested_text', 'total_kpoints')}
        band_path['segments'] = [[{'label': p['label'], 'frac': p['frac']} for p in s['points']] for s in record['segments']]
    report = core.export_bands(bs, root, {'formula': manifest['formula'], 'workflow': 'full',
                 **({'band_path': band_path} if band_path else {}),
                 'vasp_version': bands.vasp_version, 'band_validation': validation,
                 'convergence': json.loads((root / 'convergence.json').read_text(encoding='utf-8')),
                 'relaxation': json.loads((root / 'relaxation.json').read_text()),
                 'post_relax_validation': json.loads((root / 'post-relax-validation.json').read_text())})
    report['limitations'][-1] = 'Convergence is finite-grid validation, not a proof of global stability; magnetism/SOC are excluded.'
    core.save(root / 'report.json', report)
    if not final:
        core.event(root, 'bands_analyzed')
        print(f'能带报告已生成：{root / "report.json"}；后续阶段尚未完成。')
        return
    state = json.loads((root / 'workflow-state.json').read_text())
    core.save(root / 'workflow-state.json', {**state, 'status': 'complete'})
    core.event(root, 'full_workflow_completed')
    print(f'完整流程完成：{root / "report.json"}')
