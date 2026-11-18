"""Evidence checked independently for each parameter; no fabricated completion stamps."""
import math
import json
import shutil
from pathlib import Path
from types import SimpleNamespace

import vasp_agent.workflow.vasp_band_agent as core
from vasp_agent.workflow import stage_definitions as stages


FIXED_ENCUT_VERSION = 1
# 2 records the continuation's KPOINTS in its reviewed plan. An older engine copies
# the source plan verbatim, so the copied artifact would name the source's grid while
# production_sampling names the newly chosen one -- a reviewed plan contradicting
# itself. Refuse the mixed pair rather than produce that record.
PRODUCTION_MESH_VERSION = 2
# 1: prepare_production accepts an operator-chosen tested ENCUT for production.
PRODUCTION_ENCUT_VERSION = 1


def fixed_cutoff(config):
    value = config.get('fixed_encut')
    if value is None:
        return config['encuts'][-1]
    if (type(value) not in (int, float) or not math.isfinite(value)
            or value not in config['encuts'][:-1] or config.get('active_scans') != ['mesh']):
        raise ValueError('固定ENCUT必须是来源扫描中低于最高值的已测试点，且仅用于网格重试。')
    return value


def production_cutoff(config):
    """ENCUT for relaxation onward; the convergence scan itself keeps fixed_cutoff."""
    value = config.get('production_encut')
    if value is None:
        return fixed_cutoff(config)
    if (type(value) not in (int, float) or not math.isfinite(value) or value not in config['encuts']
            or config.get('fixed_encut') is not None):
        raise ValueError('后续ENCUT必须是本任务扫描中已测试的值，且不能与固定ENCUT网格重试同时使用。')
    return value


def check_production_cutoff(result, encut, mesh, config):
    """A lower production ENCUT must match the highest tested one on the same grid."""
    values = result['values']
    if encut not in values:
        raise ValueError(f'ENCUT {encut} 不是来源扫描的已测试值 {values}。')
    if encut == values[-1]:
        return None
    if config.get('fixed_encut') is not None:
        raise ValueError('来源是固定ENCUT网格重试，后续只能沿用该固定值。')
    fixed_mesh = result['conditions']['fixed_mesh']
    if list(fixed_mesh) != list(mesh):
        raise ValueError(f'ENCUT 扫描在网格 {fixed_mesh} 上进行，与后续网格 {mesh} 不同；'
                         '较低ENCUT与最高值的等价性只在扫描网格上得到验证，请使用该网格或最高ENCUT。')
    rows = cutoff_eligibility(result, config)
    row = next(r for r in rows if r['encut'] == encut)
    if not row['passed']:
        eligible = [r['encut'] for r in rows if r['passed']] + [values[-1]]
        raise ValueError(f"来源扫描数据显示 ENCUT {encut} 与最高已测 {values[-1]} eV 不满足当前阈值"
                         f"（{', '.join(row['failed_checks']) or '比较未通过'}）；可选：{eligible}。")
    return row


def scan_result(root, kind, config=None):
    from vasp_agent.workflow.full_workflow import metrics, compare, base_incar, incar_matches
    from pymatgen.io.vasp.inputs import Incar, Kpoints
    root = Path(root)
    if (root / 'convergence-reuse.json').exists():
        receipt = json.loads((root / 'convergence-reuse.json').read_text(encoding='utf-8'))
        if receipt['kind'] == kind:
            _, result = reused_result(root)
            return result
    source_config = json.loads((root / 'config.json').read_text())
    config = config or source_config
    samples, hashes = [], {}
    for index in range(3):
        folder = root / f'convergence/{kind}-{index}'
        stamp = json.loads((folder / 'complete.json').read_text())
        if core.digest(folder / 'vasprun.xml') != stamp['xml_sha256']:
            raise ValueError('收敛输出哈希不一致。')
        recorded = json.loads((folder / 'inputs.json').read_text())
        if set(recorded) != {'INCAR', 'KPOINTS', 'POSCAR', 'POTCAR'}:
            raise ValueError('收敛输入来源记录不完整。')
        for name, digest in recorded.items():
            if core.digest(folder / name) != digest:
                raise ValueError('收敛输入哈希不一致：' + name)
        for name in ('initial.POSCAR', 'POTCAR'):
            if core.digest(root / name) != core.digest(folder / ('POSCAR' if name == 'initial.POSCAR' else name)):
                raise ValueError('收敛测试结构或赝势与任务不一致。')
        cut = source_config['encuts'][index] if kind == 'encut' else fixed_cutoff(source_config)
        mesh = source_config['meshes'][index] if kind == 'mesh' else source_config['meshes'][-1]
        if not incar_matches(folder, source_config, base_incar(source_config, cut)):
            raise ValueError('实际 INCAR 与声明的收敛模型不一致。')
        points = Kpoints.from_file(folder / 'KPOINTS')
        if points.style.name != 'Gamma' or list(points.kpts[0]) != mesh or tuple(points.kpts_shift) != (0, 0, 0):
            raise ValueError('实际网格与收敛条件不一致。')
        samples.append(metrics(core.read_result(folder)))
        extra = ('execution.json',) if (folder / 'execution.json').is_file() else ()
        for name in (*recorded, 'inputs.json', 'complete.json', 'vasprun.xml', *extra):
            hashes[(folder / name).relative_to(root).as_posix()] = core.digest(folder / name)
    comparisons = [compare(samples[i], samples[i + 1], config) for i in (0, 1)]
    return {'status': 'passed' if all(c['passed'] for c in comparisons) else 'failed',
            'values': source_config['encuts' if kind == 'encut' else 'meshes'],
            'samples': samples, 'comparisons': comparisons, 'hashes': hashes,
            'conditions': {'fixed_mesh': source_config['meshes'][-1]} if kind == 'encut' else {'fixed_encut': fixed_cutoff(source_config)}}


def inspect(root):
    root = Path(root)
    core.verify(root)
    if (root / '.agent.lock').exists():
        raise ValueError('来源任务仍有运行锁，不能冻结复用证据。')
    results = {}
    for kind in ('encut', 'mesh'):
        try:
            results[kind] = scan_result(root, kind)
        except (ValueError, OSError, KeyError) as exc:
            results[kind] = {'status': 'unavailable', 'reason': str(exc)}
    config = json.loads((root / 'config.json').read_text())
    if results['encut'].get('samples'):
        # Which lower cutoffs could stand in for the highest: the cross check's question.
        results['encut']['cutoff_vs_highest'] = cutoff_eligibility(results['encut'], config)
    if config.get('fixed_encut') is not None:
        try:
            results['cross_cutoff'] = cross_result(root, config, results['mesh'])
        except (ValueError, OSError, KeyError) as exc:
            results['cross_cutoff'] = {'status': 'unavailable', 'reason': str(exc)}
    return {'source': str(root), 'parameters': results}


def cross_result(root, config, mesh_result):
    """Recompute the fixed-ENCUT cross check from outputs, as prepare_production does."""
    from pymatgen.io.vasp.inputs import Incar
    from vasp_agent.workflow import full_workflow
    if not mesh_result.get('samples'):
        raise ValueError('网格扫描结果不可用，无法核验交叉复核。')
    folder = root / 'convergence/cross-cutoff'
    if not full_workflow.incar_matches(folder, config, full_workflow.base_incar(config, config['encuts'][-1])):
        raise ValueError('交叉复核输入与声明不一致。')
    comparison = full_workflow.compare(mesh_result['samples'][-1], full_workflow.metrics(core.read_result(folder)), config)
    return {'values': [fixed_cutoff(config), config['encuts'][-1]], 'mesh': config['meshes'][-1],
            'comparison': comparison, 'status': 'passed' if comparison['passed'] else 'failed',
            'note': '比较新最密网格上固定 ENCUT 与最高已测 ENCUT 的结果；按当前核验程序由原始输出重新计算。'}


def cutoff_eligibility(result, config):
    """Compare every tested ENCUT with the highest one on the source scan's own data.

    A passed scan only bounds adjacent differences; the differences add up, so a
    lower cutoff can differ from the highest by more than energy_tol while every
    adjacent step passes. The cross-cutoff check compares exactly those ends.
    """
    from vasp_agent.workflow.full_workflow import compare
    samples, values = result['samples'], result['values']
    rows = []
    for value, sample in zip(values[:-1], samples[:-1]):
        comparison = compare(sample, samples[-1], config)
        rows.append({'encut': value, 'passed': comparison['passed'],
                     'energy_diff_meV_per_atom': round(abs(sample['energy_per_atom'] - samples[-1]['energy_per_atom']) * 1000, 4),
                     'failed_checks': comparison.get('failed_checks', [])})
    return rows


def check_fixed_cutoff(result, fixed_encut, config):
    """Refuse a fixed ENCUT that the source data already shows will fail the cross check."""
    rows = cutoff_eligibility(result, config)
    row = next((r for r in rows if r['encut'] == fixed_encut), None)
    if row is not None and not row['passed']:
        eligible = [r['encut'] for r in rows if r['passed']]
        raise ValueError(
            f"来源扫描数据已显示 ENCUT {fixed_encut} 与最高已测 {result['values'][-1]} eV 相差 "
            f"{row['energy_diff_meV_per_atom']} meV/atom（未通过：{', '.join(row['failed_checks']) or '比较'}），"
            f"交叉复核必然失败，未准备任务。相邻两级都通过不代表两端差值在阈值内。"
            + (f"可选的较低已测值：{', '.join(str(v) for v in eligible)} eV；" if eligible else '没有可替代的较低已测值；')
            + '也可不传 fixed_encut，固定采用最高已测值。')


def prepare_retry(source, target, mode, encuts=None, meshes=None, fixed_encut=None, scan_thresholds=None, scan_preset=None):
    """New task only, same structure/potentials/model; selected scan may change.

    scan_thresholds (operator-confirmed) replace scan-difference thresholds for the
    new task, and reused source evidence is judged again with them.
    """
    from vasp_agent.workflow.full_workflow import prepare_full, validate_scan_preset, validate_scan_thresholds
    source, target = Path(source), Path(target)
    scan_thresholds = validate_scan_thresholds(scan_thresholds)
    scan_preset = validate_scan_preset(scan_preset)
    if mode not in ('both', 'encut', 'mesh'):
        raise ValueError('扫描模式必须为 both、encut 或 mesh。')
    core.verify(source)
    if (source / '.agent.lock').exists():
        raise ValueError('来源任务仍在运行。')
    config = json.loads((source / 'config.json').read_text())
    selected_encuts = config['encuts'] if encuts is None else encuts
    selected_meshes = config['meshes'] if meshes is None else meshes
    if fixed_encut is not None:
        fixed_cutoff({'encuts': config['encuts'], 'fixed_encut': fixed_encut,
                      'active_scans': ['mesh'] if mode == 'mesh' else []})
    skip = None if mode == 'both' else ('encut' if mode == 'mesh' else 'mesh')
    if skip:
        key = 'encuts' if skip == 'encut' else 'meshes'
        if (selected_encuts if key == 'encuts' else selected_meshes) != config[key]:
            raise ValueError('被复用参数的原扫描序列不能改变；固定采用最高已测试值。')
        # Judge reused evidence with the new task's thresholds (preset and overrides).
        judged = {**config, **({'scan_thresholds': scan_thresholds} if scan_thresholds else {}),
                  **({'scan_preset': scan_preset or config.get('scan_preset')} if (scan_preset or config.get('scan_preset')) else {})}
        result = scan_result(source, skip, judged)
        if result['status'] != 'passed':
            raise ValueError('来源参数未通过连续两级收敛，不能复用' + ('（已按新扫描阈值重新判定）。' if scan_thresholds or scan_preset else '。'))
        if fixed_encut is not None:
            check_fixed_cutoff(result, fixed_encut, judged)
    args = SimpleNamespace(**{k: config[k] for k in ('ediff', 'points', 'symprec', 'nbands', 'relax_mode', 'force_tol', 'stress_tol', 'energy_tol', 'nsw')},
        structure=str(source / 'initial.POSCAR'), workdir=str(target), potcar=str(source / 'POTCAR'),
        encut=selected_encuts[0], mesh=selected_meshes[0], encuts=selected_encuts, meshes=selected_meshes,
        parallel_tests=1, cores_per_test=8, stage_gates=True, reuse_source=str(source) if skip else None, reuse_kind=skip, fixed_encut=fixed_encut,
        scan_thresholds=scan_thresholds, scan_preset=scan_preset or config.get('scan_preset'),
        environment_profile=config.get('environment_profile'),
        # The source's stage selection and operator-chosen stage parameters carry
        # over (validated again by prepare_full); a legacy source has neither.
        optional_stages=[name for name in stages.order(config) if name not in stages.LEGACY_STAGES],
        stage_parameters=config.get('stage_parameters'))
    prepare_full(args)
    from vasp_agent.workflow.input_review import preparation_summary
    return {'prepared': True, 'directory': str(target), 'preparation_summary': preparation_summary(target)}


# Parameters that enter the scan calculations and must match for reuse.
REUSE_CHECKED = ('ediff', 'nbands', 'relax_mode', 'force_tol', 'stress_tol', 'energy_tol')
# Recorded in config but not used by the scans; listed in the receipt, not compared.
REUSE_UNCHECKED = ('symprec', 'points', 'nsw')


def scan_thresholds_of(config):
    from vasp_agent.workflow.full_workflow import scan_thresholds
    return scan_thresholds(config)[0]


def freeze(source, target, kind, config):
    source, target = Path(source), Path(target)
    old = json.loads((source / 'config.json').read_text())
    for name in ('initial.POSCAR', 'POTCAR'):
        if core.digest(source / name) != core.digest(target / name):
            raise ValueError('复用要求结构与赝势完全相同。')
    for key in REUSE_CHECKED:
        if old[key] != config[key]:
            raise ValueError('复用模型或阈值不一致：' + key)
    fixed_cutoff(config)
    result = scan_result(source, kind, config)
    if result['status'] != 'passed':
        raise ValueError('来源参数未收敛。')
    evidence = target / 'reused-evidence'
    origin = source / 'reused-evidence' if not (source / f'convergence/{kind}-0').exists() else source
    files = {**result['hashes'], **{name: core.digest(origin / name) for name in ('config.json', 'initial.POSCAR', 'POTCAR')}}
    for name, digest in files.items():
        destination = evidence / name
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(origin / name, destination)
        if core.digest(destination) != digest:
            raise ValueError('复制期间来源发生变化。')
    receipt = {'kind': kind, 'source': str(source), 'result': result,
               'scope': '仅原结构、赝势和模型及记录的固定参数条件下通过；其他参数改变后的交叉精度尚未验证。优化后仍执行高截断能和密网格复核。',
               'files': {(evidence / name).relative_to(target).as_posix(): digest for name, digest in files.items()},
               # State what was compared, and what was not, instead of a blanket scope claim.
               'checked': {'files': ['initial.POSCAR', 'POTCAR'], 'parameters': list(REUSE_CHECKED)},
               'unchecked_parameters': {key: {'source': old.get(key), 'target': config.get(key),
                                              'differs': old.get(key) != config.get(key)}
                                        for key in REUSE_UNCHECKED},
               'unchecked_note': 'symprec 只影响能带路径的对称性判断，points 只影响能带路径点数，nsw 只影响优化步数上限；'
                                 '三者不进入收敛扫描计算，故不阻断复用，但此处如实列出是否不同。',
               'judged_with_thresholds': scan_thresholds_of(config),
               'source_thresholds': scan_thresholds_of(old),
               'rejudged_with_operator_thresholds': scan_thresholds_of(config) != scan_thresholds_of(old)}
    core.save(target / 'convergence-reuse.json', receipt)
    return ['convergence-reuse.json', *receipt['files']]


def reused_result(root):
    receipt = json.loads((root / 'convergence-reuse.json').read_text(encoding='utf-8'))
    for name, digest in receipt['files'].items():
        path = (root / name).resolve()
        if not path.is_relative_to((root / 'reused-evidence').resolve()) or core.digest(path) != digest:
            raise ValueError('复用证据已改变。')
    config = json.loads((root / 'config.json').read_text())
    fixed_cutoff(config)
    if config.get('fixed_encut') is not None and receipt['kind'] != 'encut':
        raise ValueError('固定ENCUT复用证据类型不匹配。')
    result = scan_result(root / 'reused-evidence', receipt['kind'], config)
    if config.get('fixed_encut') is not None and config['encuts'] != result['values']:
        raise ValueError('固定ENCUT的来源扫描序列与冻结证据不同。')
    if result['status'] != 'passed':
        raise ValueError('复用证据未通过当前验收。')
    return receipt['kind'], {**result, 'origin': 'reused', 'source': receipt['source'], 'scope': receipt['scope']}


def production_mesh(config):
    """Production sampling is independent of the preserved convergence scan."""
    mesh = config.get('production_mesh', config['meshes'][-1])
    if (not isinstance(mesh, list) or len(mesh) != 3
            or any(type(n) is not int or not 1 <= n <= 128 for n in mesh)
            or mesh not in config['meshes']):
        raise ValueError('后续网格必须是本任务扫描中已测试的三维整数网格。')
    return mesh


def prepare_production(source, target, mesh, expected_snapshot, encut=None):
    """Fork verified convergence into a new reviewed continuation, never mutate source."""
    from pymatgen.io.vasp.inputs import Incar
    from vasp_agent.workflow import stage_gate, full_workflow
    source, target = Path(source), Path(target)
    # Every enabled stage after convergence, and the folders its runs would write.
    later = stage_gate.task_stages(source)[1:]
    folders = ('relax-1', 'relax-2', 'validation', *later[1:])

    def check_idle():
        if (source / '.agent.lock').exists() or (source / '.task-archived.json').exists():
            raise ValueError('来源仍在运行或已归档。')
        if any((source / name).exists() for name in folders):
            raise ValueError('来源已生成后续输入或输出，不能复用为优化起点。')
        if any((source / 'submissions' / (phase + '.json')).exists()
               for phase in later):
            raise ValueError('来源已有后续阶段提交记录，先核查提交状态。')
    if target.exists() or source.resolve() == target.resolve():
        raise ValueError('后续准备必须使用不存在的新目录。')
    check_idle()
    manifest = core.verify(source)
    data = stage_gate.context(source)
    if data['phase'] != 'relaxation' or data['snapshot'] != expected_snapshot:
        raise ValueError('来源必须完成收敛且快照一致，请重新读取阶段审阅。')
    if data['checkpoint']['completed'] != ['convergence']:
        raise ValueError('来源检查点不是仅完成收敛。')
    config = dict(data['inputs']['config'])
    config['production_mesh'] = mesh
    production_mesh(config)
    results = {kind: scan_result(source, kind) for kind in ('encut', 'mesh')}
    if any(result['status'] != 'passed' for result in results.values()):
        raise ValueError('原始ENCUT及网格输出必须通过连续两级收敛验收。')
    cutoff_check = None
    if encut is not None:
        cutoff_check = check_production_cutoff(results['encut'], encut, mesh, config)
        if encut != fixed_cutoff(config):
            config['production_encut'] = encut
        production_cutoff(config)
    cross = None
    if config.get('fixed_encut') is not None:
        folder = source / 'convergence/cross-cutoff'
        if not full_workflow.incar_matches(folder, config, full_workflow.base_incar(config, config['encuts'][-1])):
            raise ValueError('交叉复核模型与声明不一致。')
        cross = full_workflow.compare(results['mesh']['samples'][-1],
            full_workflow.metrics(core.read_result(folder)), config)
        if not cross['passed']:
            raise ValueError('固定ENCUT交叉复核未通过，不能准备优化。')
    files = dict(manifest['hashes'])
    files.update(data['evidence']['hashes'])
    for name in ('manifest.json', 'physics-model.json', 'checkpoint.json'):
        files[name] = core.digest(source / name)
    # Include POTCAR and all exact input/output provenance for cached stages.
    for name in data['inputs']['plan']['incars']:
        if name.startswith('convergence/'):
            for leaf in ('INCAR', 'KPOINTS', 'POSCAR', 'POTCAR', 'inputs.json', 'complete.json', 'vasprun.xml', 'execution.json'):
                if leaf != 'execution.json' or (source / name / leaf).is_file():
                    files[name + '/' + leaf] = core.digest(source / name / leaf)
    for name in files:
        path = source / name
        if (Path(name).is_absolute() or '..' in Path(name).parts or not path.resolve().is_relative_to(source.resolve())
                or any(part.is_symlink() for part in (path, *path.parents) if part != source.parent)):
            raise ValueError('来源证据路径无效。')
    target.mkdir(parents=True, exist_ok=False)
    # Publish manifest last: an interrupted preparation cannot pass core.verify.
    for name, digest in files.items():
        if name == 'manifest.json':
            continue
        destination = target / name
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source / name, destination)
        if core.digest(destination) != digest:
            raise ValueError('复制期间来源证据发生变化；新目录未完成，不能提交。')
    shutil.copyfile(source / 'manifest.json', target / 'source-manifest.json')
    # The stages from relaxation on run in this task and get the KPAR/NCORE a new
    # task would; the copied convergence stages stay as they ran.
    config.update(parallel_tests=1, cores_per_test=8, nodes=1, execution_tags=full_workflow.EXECUTION_TAGS_VERSION)
    core.save(target / 'config.json', config)
    plan = data['inputs']['plan']
    plan['schema'] = 2
    # Production INCARs follow the chosen ENCUT; the convergence entries stay as run.
    from vasp_agent.workflow.input_review import production_incars
    plan['incars'].update(production_incars(config))
    plan['production_sampling'] = {'encut': production_cutoff(config), 'relaxation_mesh': mesh,
        'source': 'operator_selected_tested_mesh',
        'encut_source': ('operator_selected_tested_encut' if 'production_encut' in config
                         else 'highest_tested_encut'),
        'scf_mesh': '按优化后标准原胞倒空间间距换算，不保证整数网格不变',
        'post_relax_validation': '保留更高截断能及更密网格复核'}
    # The source's plan recorded the source's grid; this run was just given a
    # different production_mesh above. Carrying the copied map over unchanged would
    # hold the continuation to the source's sampling and full_workflow.stage would
    # refuse every relaxation stage as a deviation. Re-derive it from this config.
    plan['kpoints'] = full_workflow.plan_kpoints(config)
    core.save(target / 'input-plan.json', plan)
    with (target / 'PARAMETERS.md').open('a', encoding='utf-8') as stream:
        stream.write('\n\n后续优化网格（操作者选择的已测试值）：' + str(mesh) +
            '；后续ENCUT：' + str(production_cutoff(config)) + ' eV' +
            ('（操作者选择的已测试值，与最高已测值比较通过）' if 'production_encut' in config else '') +
            '；原扫描序列不变。SCF按标准原胞倒空间间距换算；优化后继续精度复核。\n')
    receipt = {'source': str(source), 'source_snapshot': expected_snapshot,
        'source_files': files, 'mesh': mesh, 'encut': production_cutoff(config),
        'encut_vs_highest': cutoff_check,
        'mesh_comparisons': results['mesh']['comparisons'], 'cross_cutoff': cross,
        'physics': '同结构、赝势和模型，沿用来源操作者确认；不复制审阅或提交授权',
        'scope': '原结构收敛证据复用；优化后仍须精度复核', 'submitted': False}
    core.save(target / 'production-choice.json', receipt)
    check_idle()
    if stage_gate.context(source)['snapshot'] != expected_snapshot:
        raise ValueError('准备期间来源快照发生变化；新目录未完成，不能提交。')
    updated = dict(manifest)
    updated['hashes'] = {name: core.digest(target / name) for name in manifest['hashes']}
    for name in ('source-manifest.json', 'production-choice.json'):
        updated['hashes'][name] = core.digest(target / name)
    core.save(target / 'workflow-state.json', {'status': 'awaiting_review', 'next': 'relaxation',
        'completed': ['convergence'], 'source': str(source)})
    core.save(target / 'manifest.json', updated)
    core.verify(target)
    context = stage_gate.context(target)
    core.event(target, 'prepared_production_continuation', source=str(source), mesh=mesh,
               encut=production_cutoff(config), source_snapshot=expected_snapshot)
    return {'prepared': True, 'directory': str(target), 'phase': 'relaxation',
        'encut': production_cutoff(config), 'encut_vs_highest': cutoff_check,
        'post_relax_higher_cutoff': math.ceil(production_cutoff(config) * 1.15),
        'relaxation_mesh': mesh, 'scan_recomputed': False,
        'snapshot': context['snapshot'], 'submitted': False,
        'next': '新目录已复用并验证收敛输出；需重新审阅及单独提交授权。'}
