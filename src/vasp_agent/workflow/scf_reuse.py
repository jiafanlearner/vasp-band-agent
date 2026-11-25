"""Reuse a passed SCF in a new task: the checks shared by every SCF-dependent fork.

A fork (new bands path, DOS only) copies a source task's evidence through SCF
into a new directory and runs one later stage there. Before anything is
written, the source must be idle, its prepared inputs (structure, POTCAR,
config, plans) must still match its manifest, its physics confirmation and SCF
acceptance must stand, every stage through SCF must verify against its
completion record (incl. scf/CHGCAR against charge_sha256), and every reused
INCAR must be what this engine would write. When the caller knows the
environment the fork will run in, the source's VASP executable fingerprint must
match it (charge densities are not reused across builds).

The source is never modified. The fork gets its own review and its own
submission grant; nothing of the source's reviews or grants is copied.
"""
import json
import shutil
from pathlib import Path

import vasp_agent.workflow.vasp_band_agent as core


def check_idle(source):
    if (source / '.agent.lock').exists() or (source / '.task-archived.json').exists():
        raise ValueError('来源仍在运行或已归档。')


def source_files(source, manifest, data):
    """Every file the reused stages read or verify upstream, with its expected hash."""
    from vasp_agent.workflow.stage_gate import phase_of, task_config
    from vasp_agent.workflow import stage_definitions as stages
    files = dict(manifest['hashes'])
    files.update(data['hashes'])
    files['physics-model.json'] = core.digest(source / 'physics-model.json')
    plan = json.loads((source / 'input-plan.json').read_text(encoding='utf-8'))
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


def check_reproducible(source, plan):
    """Every reused stage's INCAR is what this engine would write for it.

    The fork's job walks all earlier stages again (as cached results) and refuses
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


def check_environment(source, environment):
    """The fork runs on the executable that computed the source's charge density."""
    if environment is None:
        return None
    path = source / 'environment-review.json'
    if not path.is_file():
        raise ValueError('来源任务缺少环境指纹，不能证明其 SCF 电荷密度可在当前环境复用。')
    previous = json.loads(path.read_text(encoding='utf-8'))
    if previous.get('executable_sha256') != environment.get('executable_sha256'):
        raise ValueError('来源任务VASP可执行文件不同，不能复用其 SCF 电荷密度。')
    return previous.get('executable_sha256')


def verified_source(source, environment=None):
    """Everything a fork may reuse from source, verified; refuses before anything is written.

    Returns {'manifest', 'config', 'checkpoint', 'evidence', 'plan', 'files',
    'executable_sha256'}; 'evidence' is stage_gate.evidence_through(source, 'scf').
    """
    from vasp_agent.workflow import stage_gate
    from vasp_agent.workflow import stage_definitions as stages
    source = Path(source)
    check_idle(source)
    manifest = core.verify(source)  # initial.POSCAR, POTCAR, config.json, input plan as prepared
    config = json.loads((source / 'config.json').read_text(encoding='utf-8'))
    if not manifest.get('stage_gates') or manifest.get('workflow') != 'full':
        raise ValueError('只支持逐阶段审阅的完整流程任务。')
    stage_gate.physics(source)
    checkpoint = json.loads((source / 'checkpoint.json').read_text(encoding='utf-8')) if (source / 'checkpoint.json').is_file() else {}
    if 'scf' not in checkpoint.get('completed', []):
        raise ValueError('来源 SCF 尚未通过验收，不能复用电荷密度。')
    # Everything through SCF (incl. scf/CHGCAR vs charge_sha256): the source's
    # stage after SCF may be bands or another SCF-dependent stage.
    data = stage_gate.evidence_through(source, 'scf')
    if checkpoint.get('next') == stages.following(config, 'scf') and checkpoint.get('evidence_hashes') != data['hashes']:
        raise ValueError('来源 SCF 完成后证据文件发生变化，请人工检查。')
    plan = json.loads((source / 'input-plan.json').read_text(encoding='utf-8'))
    check_reproducible(source, plan)
    executable = check_environment(source, environment)
    return {'manifest': manifest, 'config': config, 'checkpoint': checkpoint, 'evidence': data, 'plan': plan,
            'files': source_files(source, manifest, data), 'executable_sha256': executable}


def copy_files(source, target, files):
    """Copy the reused evidence into the new directory (not the manifest), each hash checked after the copy."""
    for name, digest in files.items():
        if name == 'manifest.json':
            continue
        destination = target / name
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source / name, destination)
        if core.digest(destination) != digest:
            raise ValueError('复制期间来源证据发生变化；新目录未完成，不能提交。')


def fork_config(config, stage_entries):
    """The fork's config: the source's, with only the reused stages plus the new one, and fresh execution tags.

    The new stage runs in this task, so it gets the KPAR/NCORE a new task would;
    the copied stages keep running as they ran (no execution.json, no tags).
    """
    from vasp_agent.workflow import stage_definitions as stages
    from vasp_agent.workflow.full_workflow import EXECUTION_TAGS_VERSION
    config = dict(config)
    if stage_entries is not None:
        stages.validate(stage_entries)
        config['stages'] = stage_entries
    if config.get('submission_mode') == 'array':
        config['submission_mode'] = 'allocation'  # convergence is done; only the new stage remains
    config.update(parallel_tests=1, cores_per_test=8, nodes=1, execution_tags=EXECUTION_TAGS_VERSION)
    return config


def recheck_source(source, data):
    """After the new directory is written: the source is still idle and its evidence unchanged."""
    from vasp_agent.workflow import stage_gate
    check_idle(source)
    if stage_gate.evidence_through(source, 'scf')['hashes'] != data['hashes']:
        raise ValueError('准备期间来源证据发生变化；新目录未完成，不能提交。')
