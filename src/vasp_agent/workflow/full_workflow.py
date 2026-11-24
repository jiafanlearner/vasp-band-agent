"""Preflight, bounded convergence studies, two-pass relaxation and bands."""
import json
import shutil
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed

import numpy as np
from pymatgen.io.vasp.inputs import Incar, Kpoints, Poscar, Potcar
from pymatgen.symmetry.bandstructure import HighSymmKpath

import vasp_agent.workflow.vasp_band_agent as core
from vasp_agent.workflow import stage_definitions as stages
from vasp_agent.workflow.workflow_policy import (
    EXECUTION_TAGS,
    EXECUTION_TAGS_RULE,
    EXECUTION_TAGS_VERSION,
    MAX_NCORE,
    MAX_POINT_GROUP_ORDER,
    MIN_RANKS_PER_KPOINT_GROUP,
    RESOURCE_LAYOUT_VERSION,
    SCAN_PRESETS,
    SCAN_THRESHOLD_LIMITS,
    SCAN_THRESHOLDS_VERSION,
    accept_convergence,
    base_incar,
    check_relaxation,
    compare,
    convergence_tasks,
    convergence_tests,
    execution_command,
    execution_preview,
    execution_tags,
    incar_matches,
    kpoint_lower_bound,
    metrics,
    plan_kpoints,
    relaxation_grids,
    save_partial_convergence,
    scan_thresholds,
    split_execution,
    stage_execution,
    validate_scan_preset,
    validate_scan_thresholds,
)
from vasp_agent.workflow.workflow_prepare import (
    prepare_full,
    recorded_stage_parameters,
    stage_selection,
)

__all__ = [
    'ARRAY_EXECUTION_VERSION',
    'BAND_PATH_VERSION',
    'ENVIRONMENT_PROFILES_VERSION',
    'EXECUTION_TAGS',
    'EXECUTION_TAGS_RULE',
    'EXECUTION_TAGS_VERSION',
    'MAX_NCORE',
    'MAX_POINT_GROUP_ORDER',
    'MIN_RANKS_PER_KPOINT_GROUP',
    'PARAMETER_SPECS_VERSION',
    'POTENTIAL_VARIANTS_VERSION',
    'RESOURCE_LAYOUT_VERSION',
    'SCAN_PLAN_VERSION',
    'SCAN_PRESETS',
    'SCAN_THRESHOLD_LIMITS',
    'SCAN_THRESHOLDS_VERSION',
    'STAGE_RUNNERS',
    '_stage_context',
    'accept_convergence',
    'analyze_full',
    'bands_accepted',
    'base_incar',
    'check_relaxation',
    'compare',
    'convergence_tasks',
    'convergence_tests',
    'execution_command',
    'execution_preview',
    'execution_tags',
    'incar_matches',
    'kpoint_lower_bound',
    'metrics',
    'plan_kpoints',
    'prepare_full',
    'recorded_stage_parameters',
    'relaxation_grids',
    'run_full',
    'run_stage',
    'run_stages',
    'save_partial_convergence',
    'scan_thresholds',
    'split_execution',
    'stage',
    'stage_execution',
    'stage_selection',
    'validate_scan_preset',
    'validate_scan_thresholds',
]

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
