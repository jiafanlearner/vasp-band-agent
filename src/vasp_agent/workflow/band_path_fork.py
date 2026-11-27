"""Fork a task whose SCF passed into a new directory that only redoes bands on a new path."""
import shutil
from pathlib import Path

import vasp_agent.workflow.vasp_band_agent as core


def _source_files(source, manifest, data):
    from vasp_agent.workflow.scf_reuse import source_files
    return source_files(source, manifest, data)


def _check_reproducible(source, plan):
    from vasp_agent.workflow.scf_reuse import check_reproducible
    return check_reproducible(source, plan)


def prepare_bands_path(source, target, band_path, environment=None, solver_overrides=None):
    """Fork a task whose SCF passed into a new directory that only redoes bands on a new path.

    Copies and verifies the evidence through SCF (scf_reuse.verified_source:
    structure, POTCAR, config, CHGCAR against complete.json charge_sha256 and,
    with environment, the executable fingerprint), never touches the source,
    never reruns SCF; the new task waits at the bands review and needs its own
    submission grant.
    """
    from vasp_agent.workflow import band_path as band_paths
    from vasp_agent.workflow import scf_reuse, solver_overrides as solver_ov, stage_gate
    from vasp_agent.workflow import stage_definitions as stages
    source, target = Path(source), Path(target)
    if target.exists() or source.resolve() == target.resolve():
        raise ValueError('换路径必须使用不存在的新目录。')
    reused = scf_reuse.verified_source(source, environment)
    manifest, config, data, plan, files = (reused[k] for k in ('manifest', 'config', 'evidence', 'plan', 'files'))
    from vasp_agent.workflow.dimensionality import is_2d
    if is_2d(config):
        raise ValueError('二维 slab 的能带晶胞未标准化，只支持自动面内路径。')
    spec = band_paths.validate(band_path, config['points'])
    config = {**config, 'band_path': spec}
    entries = None
    if 'stages' in config:
        # The fork runs only bands after the reused stages; a source's other
        # SCF-dependent stages are not part of it. Sources without the record
        # stay without it (legacy chain, bands last).
        kept = [entry for entry in config['stages'] if entry['name'] in stages.BASE_STAGES]
        bands = ([entry for entry in config['stages'] if entry['name'] == 'bands']
                 or [{'name': 'bands', 'version': max(stages.DEFINITIONS['bands'])}])
        entries = kept + bands
        # Parameters of the dropped stages do not belong to the fork.
        config.pop('stage_parameters', None)
    config = scf_reuse.fork_config(config, entries)
    merged_overrides = solver_ov.apply_fork_overrides(
        config, None, solver_overrides, ('convergence', 'relaxation', 'scf'), stages.order(config))
    # Resolved on the source's final cell now: an unknown label is refused before any write.
    record = band_paths.files_record(source, config)
    target.mkdir(parents=True, exist_ok=False)
    scf_reuse.copy_files(source, target, files)
    shutil.copyfile(source / 'manifest.json', target / 'bands-source-manifest.json')
    core.save(target / 'config.json', config)
    plan['band_path'] = {'source': 'operator', 'requested': spec, 'text': band_paths.render(spec),
                         'note': '换路径续算：复用来源已验收的 SCF 电荷密度，只运行 bands。'}
    solver_ov.set_overrides(plan, merged_overrides)
    from vasp_agent.workflow.input_review import production_incars
    plan['incars']['bands'] = production_incars(config)['bands']
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
    scf_reuse.recheck_source(source, data)
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
