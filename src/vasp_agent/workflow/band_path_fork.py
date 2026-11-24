"""Fork a task whose SCF passed into a new directory that only redoes bands on a new path."""
import json
import shutil
from pathlib import Path

import vasp_agent.workflow.vasp_band_agent as core


def _source_files(source, manifest, data):
    """Every file the bands phase reads or verifies upstream, with its expected hash."""
    files = dict(manifest['hashes'])
    files.update(data['hashes'])
    files['physics-model.json'] = core.digest(source / 'physics-model.json')
    plan = json.loads((source / 'input-plan.json').read_text(encoding='utf-8'))
    from vasp_agent.workflow.stage_gate import phase_of, task_config
    from vasp_agent.workflow import stage_definitions as stages
    reused = stages.through(task_config(source), 'scf')
    for name in plan['incars']:
        if phase_of(name) not in reused:
            continue
        inputs = json.loads((source / name / 'inputs.json').read_text(encoding='utf-8'))
        for filename, digest in inputs.items():
            files.setdefault(name + '/' + filename, digest)
    for name in files:
        path = source / name
        if (Path(name).is_absolute() or '..' in Path(name).parts or not path.resolve().is_relative_to(source.resolve())
                or any(part.is_symlink() for part in (path, *path.parents) if part != source.parent)):
            raise ValueError('来源证据路径无效。')
    return files


def _check_reproducible(source, plan):
    """Every reused stage's INCAR is what this engine would write for it.

    The bands job walks all earlier stages again (as cached results) and refuses
    any input it would write differently. A source prepared by an engine whose
    INCAR text differs is refused here, before anything is copied.
    """
    from vasp_agent.workflow.input_review import annotated
    from vasp_agent.workflow.stage_gate import phase_of, task_config
    from vasp_agent.workflow import stage_definitions as stages
    reused = stages.through(task_config(source), 'scf')
    for name, incar in plan['incars'].items():
        if phase_of(name) not in reused:
            continue
        folder = source / name
        tags = json.loads((folder / 'execution.json').read_text(encoding='utf-8'))['tags'] if (folder / 'execution.json').is_file() else {}
        if (folder / 'INCAR').read_text(encoding='utf-8') != annotated({**incar, **tags}):
            raise ValueError(f'来源 {name}/INCAR 与当前引擎生成的输入不一致（来源由不同版本准备）；'
                             '不能在新引擎下逐项复核，未创建新目录。')


def prepare_bands_path(source, target, band_path):
    """Fork a task whose SCF passed into a new directory that only redoes bands on a new path.

    Copies and verifies the evidence through SCF (incl. scf/CHGCAR against
    complete.json charge_sha256), never touches the source, never reruns SCF;
    the new task waits at the bands review and needs its own submission grant.
    """
    from vasp_agent.workflow import band_path as band_paths
    from vasp_agent.workflow import stage_gate
    source, target = Path(source), Path(target)
    if target.exists() or source.resolve() == target.resolve():
        raise ValueError('换路径必须使用不存在的新目录。')

    def check_idle():
        if (source / '.agent.lock').exists() or (source / '.task-archived.json').exists():
            raise ValueError('来源仍在运行或已归档。')
    check_idle()
    manifest = core.verify(source)
    config = json.loads((source / 'config.json').read_text(encoding='utf-8'))
    if not manifest.get('stage_gates') or manifest.get('workflow') != 'full':
        raise ValueError('只支持逐阶段审阅的完整流程任务。')
    stage_gate.physics(source)
    checkpoint = json.loads((source / 'checkpoint.json').read_text(encoding='utf-8')) if (source / 'checkpoint.json').is_file() else {}
    if 'scf' not in checkpoint.get('completed', []):
        raise ValueError('来源 SCF 尚未通过验收，不能复用电荷密度。')
    from vasp_agent.workflow.dimensionality import is_2d
    if is_2d(config):
        raise ValueError('二维 slab 的能带晶胞未标准化，只支持自动面内路径。')
    spec = band_paths.validate(band_path, config['points'])
    from vasp_agent.workflow import stage_definitions as stages
    # Everything through SCF (incl. scf/CHGCAR vs charge_sha256): the source's
    # stage after SCF may be bands or another SCF-dependent stage.
    data = stage_gate.evidence_through(source, 'scf')
    if checkpoint.get('next') == stages.following(config, 'scf') and checkpoint.get('evidence_hashes') != data['hashes']:
        raise ValueError('来源 SCF 完成后证据文件发生变化，请人工检查。')
    plan = json.loads((source / 'input-plan.json').read_text(encoding='utf-8'))
    _check_reproducible(source, plan)
    config = {**config, 'band_path': spec}
    if 'stages' in config:
        # The fork runs only bands after the reused stages; a source's other
        # SCF-dependent stages are not part of it. Sources without the record
        # stay without it (legacy chain, bands last).
        kept = [entry for entry in config['stages'] if entry['name'] in stages.BASE_STAGES]
        bands = ([entry for entry in config['stages'] if entry['name'] == 'bands']
                 or [{'name': 'bands', 'version': max(stages.DEFINITIONS['bands'])}])
        config['stages'] = kept + bands
        stages.validate(config['stages'])
        # Parameters of the dropped stages do not belong to the fork.
        config.pop('stage_parameters', None)
    # Resolved on the source's final cell now: an unknown label is refused before any write.
    record = band_paths.files_record(source, config)
    files = _source_files(source, manifest, data)
    target.mkdir(parents=True, exist_ok=False)
    for name, digest in files.items():
        if name == 'manifest.json':
            continue
        destination = target / name
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source / name, destination)
        if core.digest(destination) != digest:
            raise ValueError('复制期间来源证据发生变化；新目录未完成，不能提交。')
    shutil.copyfile(source / 'manifest.json', target / 'bands-source-manifest.json')
    if config.get('submission_mode') == 'array':
        config['submission_mode'] = 'allocation'  # convergence is done; only bands remains
    # Bands runs in this task, so it gets the KPAR/NCORE a new task would; the
    # copied stages keep running as they ran (no execution.json, no tags).
    from vasp_agent.workflow.full_workflow import EXECUTION_TAGS_VERSION
    config.update(parallel_tests=1, cores_per_test=8, nodes=1, execution_tags=EXECUTION_TAGS_VERSION)
    core.save(target / 'config.json', config)
    plan['band_path'] = {'source': 'operator', 'requested': spec, 'text': band_paths.render(spec),
                         'note': '换路径续算：复用来源已验收的 SCF 电荷密度，只运行 bands。'}
    core.save(target / 'input-plan.json', plan)
    with (target / 'PARAMETERS.md').open('a', encoding='utf-8') as stream:
        stream.write('\n\n能带路径（操作者指定，换路径续算）：' + band_paths.render(spec) +
                     '；复用来源 SCF 电荷密度（ICHARG=11），不重算 SCF。\n')
    receipt = {'source': str(source), 'source_files': files, 'band_path': spec, 'text': band_paths.render(spec),
               'resolved_on_source_cell': record, 'scf_reused': True, 'scf_recomputed': False,
               'charge_sha256': data['hashes']['scf/CHGCAR'],
               'physics': '同结构、赝势和模型，沿用来源操作者确认；不复制审阅或提交授权', 'submitted': False}
    core.save(target / 'bands-path-choice.json', receipt)
    updated = dict(manifest)
    updated['hashes'] = dict(manifest['hashes'])
    for name in ('config.json', 'input-plan.json', 'PARAMETERS.md', 'bands-source-manifest.json', 'bands-path-choice.json'):
        updated['hashes'][name] = core.digest(target / name)
    checkpoint = {'completed': list(stages.through(config, 'scf')), 'next': 'bands',
                  'evidence_hashes': stage_gate.evidence(target, 'bands')['hashes'], 'bands_path_fork': str(source)}
    if checkpoint['evidence_hashes'] != data['hashes']:
        raise ValueError('新目录证据与来源不一致；新目录未完成，不能提交。')
    core.save(target / 'checkpoint.json', checkpoint)
    core.save(target / 'workflow-state.json', {'status': 'awaiting_review', **checkpoint})
    check_idle()
    if stage_gate.evidence_through(source, 'scf')['hashes'] != data['hashes']:
        raise ValueError('准备期间来源证据发生变化；新目录未完成，不能提交。')
    # Published last: an interrupted preparation cannot pass core.verify.
    core.save(target / 'manifest.json', updated)
    core.verify(target)
    context = stage_gate.context(target)
    core.event(target, 'prepared_bands_path_fork', source=str(source), band_path=band_paths.render(spec),
               charge_sha256=data['hashes']['scf/CHGCAR'])
    return {'prepared': True, 'directory': str(target), 'phase': 'bands', 'band_path': band_paths.render(spec),
            'resolved': {'lattice_type': record['lattice_type'], 'convention': record['convention'],
                         'segments': [[(p['label'], [round(x, 6) for x in p['frac']]) for p in s['points']] for s in record['segments']],
                         'divisions': [s['divisions'] for s in record['segments']], 'total_kpoints': record['total_kpoints']},
            'scf_recomputed': False, 'charge_sha256': data['hashes']['scf/CHGCAR'],
            'snapshot': context['snapshot'], 'submitted': False,
            'next': '新目录已复用并验证 SCF；需 get_stage_review 审阅 bands 并单独提交授权。'}
