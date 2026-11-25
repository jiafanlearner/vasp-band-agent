"""Annotated deterministic inputs and hash-bound AI reviews; no model API on cluster."""
import hashlib
import json
from datetime import datetime, timezone
from uuid import uuid4

from pymatgen.io.vasp.inputs import Incar, Kpoints, Poscar, Potcar
import numpy as np
import vasp_agent.workflow.vasp_band_agent as core

NOTES = {
    'ENCUT': ('scan policy', 'Plane-wave cutoff to be validated by convergence scans'),
    'NBANDS': ('preparation argument', 'Requested band count - verify electron occupancy and empty bands'),
    'EDIFF': ('workflow default', 'Electronic convergence threshold'),
    'PREC': ('workflow default', 'Accurate numerical precision preset'),
    'GGA': ('supported scope', 'PBE functional - requires matching PBE potentials'),
    'ISPIN': ('supported scope', 'Nonmagnetic assumption - not inferred from structure'),
    'ISMEAR': ('workflow default', 'Gaussian occupations - suitability requires review'),
    'SIGMA': ('workflow default', 'Occupation width in eV - not convergence tested yet'),
    'LREAL': ('workflow default', 'Use reciprocal-space projectors'),
    'LASPH': ('workflow default', 'Include nonspherical PAW contributions'),
    'LMAXMIX': ('workflow default', 'Conservative mixing angular momentum limit'),
    'NELM': ('workflow default', 'Electronic iteration cap - does not guarantee convergence'),
    'NSW': ('stage policy', 'Maximum ionic steps - zero for static calculations'),
    'IBRION': ('stage policy', 'Ionic update method - disabled for static calculations'),
    'LWAVE': ('workflow default', 'Do not write wavefunctions'),
    'ALGO': ('workflow default', 'Normal electronic minimization algorithm'),
    'ISTART': ('stage policy', 'Start without a wavefunction restart'),
    'ICHARG': ('stage policy', 'Initial charge policy - 11 uses fixed SCF charge for bands'),
    'LCHARG': ('stage policy', 'Write SCF charge for downstream band calculation'),
    'ISIF': ('stage policy', 'Cell and ionic relaxation policy or static stress evaluation'),
    'EDIFFG': ('acceptance policy', 'Negative value selects force stopping threshold'),
    'POTIM': ('workflow default', 'Ionic step parameter - review if relaxation is unstable'),
}
# Not part of the reviewed plan: derived at launch from the confirmed MPI layout
# (full_workflow.execution_tags) and recorded in each stage's execution.json.
EXECUTION_NOTES = {
    'KPAR': ('execution', 'k-point parallel groups from MPI ranks - performance only, not physics'),
    'NCORE': ('execution', 'Ranks per band/FFT group from MPI ranks - performance only, not physics'),
}
# Keys only stages generated at start (dos) write; kept apart from NOTES so the
# plans and PARAMETERS.md of every other task stay byte for byte as before.
STAGE_NOTES = {
    'LORBIT': ('parameter spec', 'Site- and lm-projected DOS on PAW spheres (RWIGS from POTCAR)'),
    'EMIN': ('parameter spec', 'Absolute lower DOS energy: SCF E_F + min(window, lowest band - margin)'),
    'EMAX': ('parameter spec', 'Absolute upper DOS energy: SCF E_F + window maximum'),
    'NEDOS': ('parameter spec', 'Grid points from the sampling interval - not a resolution'),
}


def annotated(incar):
    lines = []
    for line in str(Incar(incar)).splitlines():
        key = line.split('=', 1)[0].strip()
        source, note = NOTES.get(key) or EXECUTION_NOTES.get(key) or STAGE_NOTES.get(key, ('workflow', 'See parameter notes'))
        lines.append(f'{line} # {source}: {note}')
    return '\n'.join(lines) + '\n'


def production_incars(config):
    """Relaxation onward: the reviewed INCARs at the production ENCUT."""
    from vasp_agent.workflow.full_workflow import base_incar
    from vasp_agent.workflow.convergence_reuse import production_cutoff
    cut, stages = production_cutoff(config), {}
    for i in (1, 2):
        stages[f'relax-{i}'] = {**base_incar(config, cut), 'IBRION': 2, 'NSW': config['nsw'],
            'ISIF': 3 if config['relax_mode'] == 'cell' else 2, 'EDIFFG': -config['force_tol'], 'POTIM': .5}
    for name, encut in [('reference', cut), ('higher-cutoff', float(np.ceil(cut * 1.15))), ('denser-mesh', cut)]:
        stages[f'validation/{name}'] = base_incar(config, encut)
    stages['scf'] = {**base_incar(config, cut), 'LCHARG': True}
    stages['bands'] = {**base_incar(config, cut), 'ICHARG': 11}
    return stages


def production_cutoff_of(config):
    from vasp_agent.workflow.convergence_reuse import production_cutoff
    return production_cutoff(config)


def prepare_plan(root, config, structure, requested):
    from vasp_agent.workflow.full_workflow import base_incar, plan_kpoints
    from vasp_agent.workflow.convergence_reuse import fixed_cutoff
    cut, mesh = fixed_cutoff(config), config['meshes'][-1]
    stages = {}
    files = []
    for kind, values in [('encut', config['encuts']), ('mesh', config['meshes'])]:
        if kind not in config.get('active_scans', ['encut', 'mesh']):
            continue
        for i, value in enumerate(values):
            name = f'convergence/{kind}-{i}'
            incar = base_incar(config, value if kind == 'encut' else cut)
            grid = value if kind == 'mesh' else mesh
            stages[name] = incar
            folder = root / name
            folder.mkdir(parents=True)
            for filename, text in {'INCAR': annotated(incar), 'POSCAR': str(Poscar(structure)),
                                   'KPOINTS': str(Kpoints.gamma_automatic(grid))}.items():
                (folder / filename).write_text(text, encoding='utf-8')
                files.append(f'{name}/{filename}')
    if config.get('fixed_encut') is not None:
        name = 'convergence/cross-cutoff'
        stages[name] = base_incar(config, config['encuts'][-1])
        folder = root / name
        folder.mkdir(parents=True)
        for filename, text in {'INCAR': annotated(stages[name]), 'POSCAR': str(Poscar(structure)),
                               'KPOINTS': str(Kpoints.gamma_automatic(mesh))}.items():
            (folder / filename).write_text(text, encoding='utf-8')
            files.append(f'{name}/{filename}')
    stages.update(production_incars(config))
    # The sampling that the prepared config already determines is registered here so
    # the run can be held to what was reviewed; the deviation check in
    # full_workflow.stage compares against this. SCF and bands have no entry because
    # their grid follows from the optimized cell, which does not exist yet.
    plan = {'schema': 2, 'incars': stages, 'kpoints': plan_kpoints(config), 'requested': requested,
            'source_note': '准备入口参数来源尚未区分用户指定或 AI 建议，由 AI 审阅结合对话记录补充。默认值不是收敛结论。',
            'future_geometry': '优化后复核使用优化结构；SCF 和能带使用同一标准原胞，SCF 网格按倒空间间距换算，能带路径由最终原胞生成。优化与复核网格已在 kpoints 中登记；优化后几何、SCF 网格和能带路径尚未产生，只能审阅其生成规则。',
            'pending_validation': ['ENCUT 和 k 网格收敛', f"非磁性 {config.get('dimensionality', {}).get('dimension', '3D')} PBE 假设",
                                   'NBANDS 占据和空带范围', '展宽及精度适用性'],
            'parameter_notes': {k: {'source': v[0], 'explanation': v[1]} for k, v in NOTES.items()}}
    if config.get('dimensionality', {}).get('dimension') == '2D':
        plan['future_geometry'] = ('二维 slab：优化只移动原子（ISIF=2），面内晶格常数沿用输入；SCF 和能带使用优化后的同一晶胞，'
                                   '不做标准化，网格与优化网格相同（c 方向 1 个点），能带走面内高对称路径。')
        plan['pending_validation'] += ['真空层厚度收敛', '非对称 slab 偶极修正（未自动启用 LDIPOL）', '面内晶格常数来源']
    if config.get('band_path') is not None:
        from vasp_agent.workflow.band_path import render
        # Validated now, resolved on the final cell at bands; the bands review shows the coordinates.
        plan['band_path'] = {'source': 'operator', 'requested': config['band_path'], 'text': render(config['band_path']),
                             'note': '操作者指定能带路径；坐标基于最终标准原胞倒格子基矢，SCF 完成后解析并在 bands 审阅中显示。'}
    if config.get('stage_parameters'):
        from vasp_agent.workflow import parameter_specs
        # Operator-chosen values of optional stages, with what they generate now
        # (or the rules applied when the stage starts, after its inputs exist).
        evidence = {'config': config, 'dimension': config.get('dimensionality', {}).get('dimension', '3D'),
                    'encut': production_cutoff_of(config)}
        plan['stage_parameters'] = {
            name: parameter_specs.plan_entry(parameter_specs.spec(entry['spec']['id'], entry['spec']['version']),
                                             entry['values'], evidence)
            for name, entry in config['stage_parameters'].items()}
    if len(config.get('active_scans', ['encut', 'mesh'])) == 1:
        receipt = json.loads((root / 'convergence-reuse.json').read_text(encoding='utf-8'))
        plan['execution_plan'] = {'compute': config['active_scans'], 'reuse': receipt['kind'],
                                  'conditions': receipt['result']['conditions'], 'scope': receipt['scope']}
        plan['source_note'] = '操作者选择性扫描方案；复用项来自已校验实际输出，不是尚未运行的默认值。'
        plan['pending_validation'][0] = '本次仅执行 ' + ', '.join(config['active_scans']) + ' 扫描；复用参数在原条件下已通过，新条件下交叉精度及优化后精度仍待复核。'
    if config.get('fixed_encut') is not None:
        plan['execution_plan'].update(fixed_encut=cut, cross_cutoff=config['encuts'][-1], tests=4)
        plan['pending_validation'][0] = '三组网格均采用指定ENCUT；最高网格额外计算来源最高ENCUT，能量/力/应力交叉复核通过后才可优化；优化后仍复核。'
    core.save(root / 'input-plan.json', plan)
    notes = ['# 参数说明（代码生成，尚非 AI 审阅结论）', '', plan['source_note'], plan['future_geometry'],
             '', '输入起点：' + json.dumps(requested, ensure_ascii=False),
             '待验证：' + '、'.join(plan['pending_validation']), '', '| 参数 | 来源 | 说明 |', '|---|---|---|']
    notes += [f'| {k} | {v[0]} | {v[1]} |' for k, v in NOTES.items()]
    notes += ['', '## 选择依据与边界', '',
              '- ENCUT：以准备入口值及赝势 ENMAX 规则确定扫描起点，扫描三级。默认后续取最高测试值；显式fixed_encut采用指定已测试值，须新最密网格交叉复核通过才继续。',
              '- NBANDS：保留准备入口值，不能仅凭该值断言空带足够；需结合赝势电子数及计算结果审阅。',
              '- ISPIN/GGA：当前仅支持非磁性 PBE，属于使用前提，不是从结构自动证明的性质。',
              '- EDIFF/PREC：采用工作流精度默认值，仍须用输出检查实际电子收敛。',
              '- ISMEAR/SIGMA：默认高斯展宽，是否适合目标材料及精度由审阅和后续验证判断。',
              '- IBRION/ISIF/NSW/EDIFFG：按优化方式选择离子与晶胞自由度，以力阈值停止；达到步数上限不等于收敛。',
              '- ICHARG/LCHARG：SCF 输出电荷供能带阶段读取；能带使用固定电荷，代码核验上下游一致。',
              '', '## 各阶段参数（后续阶段为方案，尚未生成最终几何）']
    for name, values in stages.items():
        notes += ['', '### ' + name, '```ini', annotated(values).rstrip(), '```']
    if 'band_path' in plan:
        notes += ['', '能带路径（操作者指定）：' + plan['band_path']['text'] + '。' + plan['band_path']['note']]
    for name, entry in plan.get('stage_parameters', {}).items():
        notes += ['', f"### {name} 阶段参数（规范 {entry['spec']['id']} v{entry['spec']['version']}，操作者确认）",
                  '```json', json.dumps(entry['values'], ensure_ascii=False, indent=1), '```',
                  '输入生成：' + ('准备时已生成，见 input-plan.json' if 'generated' in entry else '阶段启动时按规范从已验收证据生成并与本方案比对')]
    if 'execution_plan' in plan:
        notes += ['', '实际执行清单（以上通用参数说明不代表全部重跑）：' + json.dumps(plan['execution_plan'], ensure_ascii=False)]
    (root / 'PARAMETERS.md').write_text('\n'.join(notes) + '\n', encoding='utf-8')
    return files + ['input-plan.json', 'PARAMETERS.md']


def context(root):
    manifest = json.loads((root / 'manifest.json').read_text(encoding='utf-8'))
    config = json.loads((root / 'config.json').read_text())
    for name, digest in manifest['hashes'].items():
        if not core.prepared_matches(root, name, digest, config):
            raise core.ToolError('INPUT_HASH_MISMATCH', '审阅输入已改变：' + name)
    if not (root / 'input-plan.json').exists():
        raise ValueError('旧任务没有审阅方案，请用最新版在新目录准备。')
    from vasp_agent.workflow.scan_config import verify_scan_inputs
    verified = verify_scan_inputs(root)
    # Prepared stage inputs as reviewed: a launch appending KPAR/NCORE (shown in
    # the stage review's execution_tags) must not change this snapshot mid-phase.
    initial = {name: (root / name).read_text(encoding='utf-8') for name in manifest['hashes'] if name.endswith(('/INCAR', '/KPOINTS'))}
    if config.get('execution_tags') is not None:
        initial = {name: core.without_execution_lines(text) if name.endswith('/INCAR') else text for name, text in initial.items()}
    data = {'scan_verification': verified, 'manifest': manifest, 'config': config,
            'plan': json.loads((root / 'input-plan.json').read_text(encoding='utf-8')),
            'initial_poscar': (root / 'initial.POSCAR').read_text(encoding='utf-8'),
            'initial_inputs': initial}
    if (root / 'production-choice.json').exists():
        from vasp_agent.workflow.convergence_reuse import production_mesh, production_cutoff
        data['production_sampling'] = {'encut': production_cutoff(data['config']),
            'relaxation_mesh': production_mesh(data['config']),
            'choice': json.loads((root / 'production-choice.json').read_text(encoding='utf-8'))}
    if (root / 'convergence-reuse.json').exists():
        from vasp_agent.workflow.convergence_reuse import reused_result
        kind, result = reused_result(root)
        data['convergence_reuse'] = {kind: result}
    choice = root / 'parameter-choice.json'
    if choice.exists():
        data['parameter_choice'] = json.loads(choice.read_text(encoding='utf-8'))
    selection = root / 'potential-selection.json'
    if selection.exists():
        if 'potential-selection.json' not in manifest['hashes']:
            raise core.ToolError('UNVERIFIED_METADATA', '赝势摘要未绑定 manifest 哈希。')
        metadata = json.loads(selection.read_text(encoding='utf-8'))
        if not isinstance(metadata, list) or not metadata or any(not isinstance(row, dict) or not row.get('element') for row in metadata):
            raise core.ToolError('METADATA_INVALID', '赝势摘要缺失或格式无效。')
        data['potential_metadata'] = metadata
    elif (root / 'POTCAR').exists():
        if 'POTCAR' not in manifest['hashes']:
            raise core.ToolError('UNVERIFIED_METADATA', 'POTCAR 未绑定 manifest 哈希。')
        data['potential_metadata'] = [{'element': p.element, 'enmax': float(p.enmax),
             'zval': float(p.ZVAL), 'title': str(p.TITEL)} for p in Potcar.from_file(root / 'POTCAR')]
    else:
        if manifest.get('ready'):
            raise core.ToolError('METADATA_MISSING', '就绪任务缺少 POTCAR 和赝势元数据。')
        data['potential_metadata'] = {'note': '无 POTCAR，仅为不可提交的预览'}
    environment = root / 'environment-review.json'
    if environment.exists():
        data['environment'] = json.loads(environment.read_text(encoding='utf-8'))
    script = root / 'submit.slurm'
    if script.exists():
        data['submit_script'] = script.read_text(encoding='utf-8')
    data['snapshot'] = hashlib.sha256(json.dumps(data, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
    return data


def record(root, snapshot, verdict, explanation, reviewer):
    if json.loads((root / 'manifest.json').read_text()).get('stage_gates'):
        raise ValueError('新版本任务必须使用结构化 record_stage_review。')
    data = context(root)
    if snapshot != data['snapshot']:
        raise ValueError('输入快照不匹配，请重新读取并审阅。')
    if verdict not in ('pass', 'needs_validation', 'blocked') or not explanation.strip() or len(explanation) > 20000:
        raise ValueError('无效审阅结论或说明。')
    result = {'snapshot': snapshot, 'verdict': verdict, 'explanation': explanation,
              'reviewer': reviewer, 'time': datetime.now(timezone.utc).isoformat()}
    archive = root / 'reviews'
    archive.mkdir(exist_ok=True)
    core.save(archive / (uuid4().hex + '.json'), result)
    core.save(root / 'ai-review.json', result)
    (root / 'AI_REVIEW.md').write_text('# AI 提交前审阅\n\n' + verdict + '\n\n' + explanation + '\n', encoding='utf-8')
    return result


def require_review(root):
    manifest = json.loads((root / 'manifest.json').read_text(encoding='utf-8'))
    if manifest.get('stage_gates'):
        from vasp_agent.workflow.stage_gate import require_review as require_stage, authorized_phase
        return require_stage(root, authorized_phase(root))
    if not manifest.get('review_required'):
        return
    path = root / 'ai-review.json'
    if not path.exists():
        raise ValueError('尚未完成 AI 输入审阅，请先 get_input_review 再 record_input_review。')
    review = json.loads(path.read_text(encoding='utf-8'))
    if review['snapshot'] != context(root)['snapshot'] or review['verdict'] not in ('pass', 'needs_validation'):
        raise ValueError('AI 审阅阻断或已失效，请重新审阅。')


def preparation_summary(root, resources=None):
    """Read generated files, verify hashes and return metadata only, never POTCAR."""
    from vasp_agent.workflow.full_workflow import execution_preview
    data = context(root)
    ready = bool(data['manifest'].get('ready'))
    source = 'potential-selection.json' if (root / 'potential-selection.json').exists() else ('POTCAR' if (root / 'POTCAR').exists() else None)
    config = data['config']
    metadata = data['potential_metadata']
    missing = []
    if not source:
        missing.append('potential_metadata')
    elif isinstance(metadata, list):
        for i, row in enumerate(metadata):
            for key in ('element', 'variant', 'source', 'sha256', 'titel', 'enmax_eV'):
                if key not in row:
                    missing.append(f'potential_metadata[{i}].{key}')
    return {'ready': ready, 'preview_only': not ready, 'source': source,
            'execution_plan': {'compute': config.get('active_scans', ['encut', 'mesh']),
                               'reuse': data.get('convergence_reuse', {}),
                               'fixed_encut': config.get('fixed_encut'),
                               'tests': 3 * len(config.get('active_scans', ['encut', 'mesh'])) + (1 if config.get('fixed_encut') is not None else 0),
                               'cross_cutoff': config['encuts'][-1] if config.get('fixed_encut') is not None else None, 'post_relax_checks': ['higher-cutoff', 'denser-mesh']},
            'hashes_verified': True, 'potential_metadata': metadata, 'missing_fields': missing,
            'scans': {**{k: config[k] for k in ('encuts', 'meshes')}, 'fixed_encut': config.get('fixed_encut')},
            'scan_verification': data['scan_verification'],
            'resources': resources if resources is not None else {k: config[k] for k in ('parallel_tests', 'cores_per_test') if k in config},
            **({'execution_tags': execution_preview(config, 'convergence')} if config.get('execution_tags') is not None else {}),
            'note': '文件一致性已核对；尚非物理确认、收敛结论或提交放行。'}
