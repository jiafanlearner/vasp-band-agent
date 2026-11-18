"""Forked tasks get KPAR/NCORE for the stages they run; copied stages stay as they ran.

Fake VASP only, no SSH, scheduler or model traffic.
"""
import json
from types import SimpleNamespace as NS

import numpy as np
import pytest
from pymatgen.io.vasp.inputs import Incar, Poscar

import vasp_agent.remote.remote_worker as worker
from vasp_agent.workflow import band_path as paths, convergence_reuse as reuse, full_workflow as full, \
    stage_gate as gate, vasp_band_agent as core
from test_stage_gate import staged, approve
from test_execution_tags import set_config

TAGS = ('KPAR', 'NCORE')


def fake_result(folder):
    structure = Poscar.from_file(folder / 'POSCAR').structure
    return NS(final_structure=structure, final_energy=-10. * len(structure), converged_ionic=True,
              parameters={'NELM': 150, 'NBANDS': 16, 'NELECT': 4}, efermi=0.,
              ionic_steps=[{'forces': np.zeros((len(structure), 3)).tolist(),
                            'stress': np.zeros((3, 3)).tolist(), 'electronic_steps': [{}]}])


@pytest.fixture
def vasp(monkeypatch):
    launches = []
    def execute(root, name, command, event_root=None):
        folder = root / name
        if not (folder / 'complete.json').exists():
            launches.append(name)
            (folder / 'vasprun.xml').write_text('mock output ' + name)
            data = {'xml_sha256': core.digest(folder / 'vasprun.xml')}
            if name == 'scf':
                (folder / 'CHGCAR').write_text('mock charge')
                data['charge_sha256'] = core.digest(folder / 'CHGCAR')
            core.save(folder / 'complete.json', data)
        return fake_result(folder)
    monkeypatch.setattr(core, 'execute', execute)
    monkeypatch.setattr(core, 'read_result', fake_result)
    monkeypatch.setattr(full, 'analyze_full', lambda root: core.save(root / 'workflow-state.json', {'status': 'complete'}))
    return launches


def run(root, *phases):
    for phase in phases:
        assert gate.current(root) == phase
        approve(root)
        full.run_full(root, ['mock'])


def snapshot(folder):
    return {str(p.relative_to(folder)): core.digest(p) for p in sorted(folder.rglob('*')) if p.is_file()}


def stage_names(root):
    return list(json.loads((root / 'input-plan.json').read_text(encoding='utf-8'))['incars'])


def untagged_source(root):
    """A task prepared before the tags existed: no flag, so no stage gets KPAR/NCORE."""
    set_config(root, execution_tags=None, cores_per_test=8)
    gate.confirm_physics(root, 'nonmagnetic bulk silicon PBE bands')
    return root


def assert_untagged(root, name):
    assert not (root / name / 'execution.json').exists()
    assert not set(TAGS) & set(Incar.from_file(root / name / 'INCAR'))


def assert_tagged(root, name, ranks):
    record = json.loads((root / name / 'execution.json').read_text(encoding='utf-8'))
    incar = Incar.from_file(root / name / 'INCAR')
    assert {k: incar[k] for k in TAGS} == record['tags'] and record['mpi_ranks_per_test'] == ranks
    return record


def assert_copied_stages_verify(root, names):
    """Every check a copied, untagged stage meets in a task whose config enables tags."""
    config = json.loads((root / 'config.json').read_text())
    plan = json.loads((root / 'input-plan.json').read_text(encoding='utf-8'))
    assert config['execution_tags'] == full.EXECUTION_TAGS_VERSION
    core.verify(root)
    for name in names:
        assert_untagged(root, name)
        assert full.incar_matches(root / name, config, plan['incars'][name])
        hashes = worker.verified_stage(root, name, plan, config)  # phase resume's reuse check
        assert 'execution.json' not in hashes
    # A copied stage that gained tag lines is refused, flagged task or not.
    folder = root / names[0]
    original = (folder / 'INCAR').read_bytes()
    (folder / 'INCAR').write_bytes(original + b'KPAR = 2\n')
    try:
        assert not full.incar_matches(folder, config, plan['incars'][names[0]])
        with pytest.raises(ValueError, match='不能复用'):
            worker.verified_stage(root, names[0], plan, config)
    finally:
        (folder / 'INCAR').write_bytes(original)


def test_production_fork_of_an_untagged_task_tags_only_the_stages_it_runs(staged, vasp):
    source = untagged_source(staged)
    run(source, 'convergence')
    assert all(not (source / n / 'execution.json').exists() for n in stage_names(source) if n.startswith('convergence/'))
    before = snapshot(source)
    config = json.loads((source / 'config.json').read_text())
    target = source.parent / 'production'
    reuse.prepare_production(source, target, config['meshes'][-1], gate.context(source)['snapshot'])
    assert snapshot(source) == before  # the source, its config included, is untouched
    assert 'execution_tags' not in json.loads((source / 'config.json').read_text())
    forked = json.loads((target / 'config.json').read_text())
    assert forked['execution_tags'] == full.EXECUTION_TAGS_VERSION
    # The flag is part of the hashed config, like any other config field.
    assert json.loads((target / 'manifest.json').read_text())['hashes']['config.json'] == core.digest(target / 'config.json')
    copied = [n for n in stage_names(target) if n.startswith('convergence/')]
    assert_copied_stages_verify(target, copied)
    assert reuse.scan_result(target, 'mesh')['status'] == 'passed'
    set_config(target, cores_per_test=32)
    review = gate.context(target)
    assert review['phase'] == 'relaxation' and review['execution_tags']['enabled']
    assert review['execution_tags']['stages']['relax-1'] == full.execution_tags(32, 1, full.Kpoints.gamma_automatic(config['meshes'][-1]))
    assert not any(k.endswith('execution.json') for k in review['evidence']['hashes'])
    vasp.clear()
    run(target, 'relaxation', 'scf', 'bands')
    assert gate.current(target) == 'complete'
    assert vasp == ['relax-1', 'relax-2', 'validation/reference', 'validation/higher-cutoff',
                    'validation/denser-mesh', 'scf', 'bands']
    for name in vasp:
        assert_tagged(target, name, 32)
    for name in copied:  # walked again as cached stages by every later phase, still byte for byte
        assert (target / name / 'INCAR').read_bytes() == (source / name / 'INCAR').read_bytes()
    assert_copied_stages_verify(target, copied)


@pytest.fixture
def finished_untagged(staged, vasp):
    source = untagged_source(staged)
    run(source, *gate.PHASES)
    assert gate.current(source) == 'complete'
    vasp.clear()
    return source


def test_bands_path_fork_of_an_untagged_task_tags_only_bands(finished_untagged, vasp):
    source = finished_untagged
    before = snapshot(source)
    target = source.parent / 'new-path'
    answer = paths.prepare_bands_path(source, target, 'Γ-R@12|X-M@12')
    assert snapshot(source) == before
    forked = json.loads((target / 'config.json').read_text())
    assert forked['execution_tags'] == full.EXECUTION_TAGS_VERSION
    assert 'execution_tags' not in json.loads((source / 'config.json').read_text())
    copied = [n for n in stage_names(target) if n != 'bands']
    assert_copied_stages_verify(target, copied)
    # The fork's own evidence hashes are what the source's were: nothing copied gained a record.
    assert gate.evidence(target, 'bands')['hashes'] == gate.evidence(source, 'bands')['hashes']
    set_config(target, cores_per_test=32)
    review = gate.context(target)
    assert review['snapshot'] != answer['snapshot']  # changing resources asks for a fresh review
    preview = review['execution_tags']
    assert preview['enabled'] and preview['maximum'] == {'KPAR': 8, 'NCORE': 2} and '启动时' in preview['note']
    run(target, 'bands')
    assert vasp == ['bands']
    record = assert_tagged(target, 'bands', 32)
    total = full.Kpoints.from_file(target / 'bands/KPOINTS')
    assert record['tags'] == full.execution_tags(32, 1, total)
    assert_copied_stages_verify(target, copied)


def test_bands_path_fork_of_a_tagged_task_keeps_each_stage_as_it_ran(staged, vasp, monkeypatch):
    set_config(staged, cores_per_test=16)
    gate.confirm_physics(staged, 'nonmagnetic bulk silicon PBE bands')
    run(staged, *gate.PHASES)
    vasp.clear()
    target = staged.parent / 'new-path'
    paths.prepare_bands_path(staged, target, 'Γ-X')
    set_config(target, cores_per_test=64, nodes=2)
    monkeypatch.setattr(full, 'execution_command', lambda config, command: command)  # no Slurm allocation here
    run(target, 'bands')
    assert vasp == ['bands']
    assert assert_tagged(target, 'bands', 64)['nodes'] == 2
    for name in ('relax-1', 'scf'):  # copied with the tags they ran with
        assert assert_tagged(target, name, 16) == json.loads((staged / name / 'execution.json').read_text(encoding='utf-8'))
    core.verify(target)


def test_scan_retry_is_a_new_preparation_and_gets_the_tags(staged, vasp, monkeypatch):
    monkeypatch.setattr(full.Potcar, 'from_file', lambda _: [NS(element='Si', functional='PBE', enmax=245., ZVAL=4, TITEL='synthetic')])
    source = untagged_source(staged)
    run(source, 'convergence')
    target = source.parent / 'retry'
    reuse.prepare_retry(source, target, 'mesh')
    assert json.loads((target / 'config.json').read_text())['execution_tags'] == full.EXECUTION_TAGS_VERSION
    assert 'execution_tags' not in json.loads((source / 'config.json').read_text())


def test_launched_but_unfinished_stage_without_a_record_is_still_refused(staged, vasp, monkeypatch):
    gate.confirm_physics(staged, 'nonmagnetic bulk silicon PBE bands')
    approve(staged)
    (staged / 'convergence/encut-0/stdout.log').write_text('started')
    with pytest.raises(ValueError, match='缺少执行标签记录'):
        full.run_full(staged, ['mock'])


def test_bands_fork_is_bound_to_the_deployed_release_that_tags_it(finished_untagged, tmp_path, monkeypatch):
    release = tmp_path / 'releases/current'
    monkeypatch.setattr(worker, 'deployed_release', lambda root: release)
    monkeypatch.setattr(worker, 'activate_release', lambda path: path)
    monkeypatch.setattr(worker, 'task_target', lambda jobs, name: finished_untagged.parent / name)
    monkeypatch.setattr(worker, 'inspect_environment', lambda cfg, mode: {'executable_sha256': 'x'})
    monkeypatch.setattr(worker, 'validate_scheduler', lambda cfg: None)
    monkeypatch.setattr(worker, 'subprocess', NS(run=lambda *a, **k: None))
    core.save(finished_untagged / 'environment-review.json', {'executable_sha256': 'x'})
    cfg = {'vasp_env': __file__, 'partition': 'p', 'walltime': '01:00:00', 'python': 'python'}
    target = finished_untagged.parent / 'path2'
    worker._prepare_bands_path({'source_job': finished_untagged.name, 'band_path': 'Γ-X'}, cfg, tmp_path, 'path2', target)
    assert json.loads((target / 'remote-job.json').read_text())['release'] == str(release)
    config = json.loads((target / 'config.json').read_text())
    assert worker.execution_tag_status(config, 'bands')['enabled']
    assert not worker.execution_tag_status(json.loads((finished_untagged / 'config.json').read_text()), 'bands')['enabled']


@pytest.mark.parametrize('ranks, nodes, text', [
    (64, 2, 'KPAR ≤ 16，NCORE 2'),
    (32, 2, 'KPAR ≤ 8，NCORE 2'),
    (64, 4, 'KPAR ≤ 16，NCORE 2'),
    (30, 4, '无法推导'),  # 30 MPI cannot be split evenly over 4 nodes
])
def test_resource_menu_tag_row_for_multi_node_layouts(ranks, nodes, text):
    from vasp_agent.permissions.resource_menu import execution_row
    row = execution_row({'cores_per_test': ranks, 'nodes': nodes}, {'enabled': True})
    assert row[1] == text or row[1].startswith(text)
    if nodes and ranks % nodes == 0:
        assert row[1] == 'KPAR ≤ {KPAR}，NCORE {NCORE}'.format(**full.execution_tags(ranks, nodes))
