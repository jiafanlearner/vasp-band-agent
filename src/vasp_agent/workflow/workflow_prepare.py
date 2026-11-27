"""Full-workflow preparation, preview signature generation, and stage-parameter binding."""
import hashlib
import json
import shutil
from pathlib import Path

import numpy as np
from pymatgen.core import Structure
from pymatgen.io.vasp.inputs import Poscar, Potcar

import vasp_agent.workflow.vasp_band_agent as core
from vasp_agent.workflow import stage_definitions as stages
from vasp_agent.workflow.workflow_policy import (
    EXECUTION_TAGS_VERSION,
    validate_scan_preset,
    validate_scan_thresholds,
)


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
    raw_overrides = getattr(args, 'solver_overrides', None)
    if getattr(args, 'preview_only', False):
        if raw_overrides is not None:
            from vasp_agent.workflow import solver_overrides as solver_ov
            preview['solver_overrides'] = solver_ov.validate(raw_overrides, allowed_phases=stage_selection(optional))
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
    if raw_overrides is not None:
        from vasp_agent.workflow import solver_overrides as solver_ov
        config['solver_overrides'] = solver_ov.validate(raw_overrides, allowed_phases=stages.order(config))
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
