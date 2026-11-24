import json
from types import SimpleNamespace as NS

import numpy as np
import pytest
from pymatgen.core import Structure, Lattice
from pymatgen.io.vasp.inputs import Poscar

import vasp_agent.workflow.full_workflow as full
import vasp_agent.workflow.stage_gate as gate
import vasp_agent.workflow.vasp_band_agent as core
from vasp_agent.chat.ai_agent import Toolbox


@pytest.fixture
def staged(tmp_path, monkeypatch):
    structure = Structure(Lattice.cubic(5.43), ['Si'], [[0, 0, 0]])
    source = tmp_path / 'POSCAR'
    Poscar(structure).write_file(source)
    root = tmp_path / 'runs/si'
    full.prepare_full(NS(structure=str(source), workdir=str(root), potcar=None,
        encut=520, mesh=[4,4,4], nbands=16, relax_mode='cell', ediff=1e-7,
        points=40, symprec=.01, force_tol=.01, stress_tol=1., energy_tol=.001, nsw=200))
    (root / 'POTCAR').write_text('test fixture')
    core.save(root / 'potential-selection.json', [{'element': 'Si', 'titel': 'test-only metadata'}])
    manifest = json.loads((root / 'manifest.json').read_text())
    manifest['ready'] = True
    manifest['hashes']['POTCAR'] = core.digest(root / 'POTCAR')
    manifest['hashes']['potential-selection.json'] = core.digest(root / 'potential-selection.json')
    core.save(root / 'manifest.json', manifest)
    monkeypatch.setattr(full.Potcar, 'from_file', lambda p: [NS(element='Si')])
    return root


def checks_for(phase):
    return [dict(item=item, judgment='pending' if item == 'validation_plan' else 'pass', blocking=False,
        evidence='test fixture evidence', reason='test rationale', next_action='执行对应阶段并验收') for item in gate.CHECKS[phase]]


def approve(root):
    data = gate.context(root)
    return gate.record(root, data['snapshot'], data['phase'], checks_for(data['phase']), 'test-model')


def test_resource_change_requires_new_review_without_changing_inputs(staged):
    from vasp_agent.remote.remote_worker import resource_context, configure_resources
    script = '#!/bin/bash\n#SBATCH --partition=compute\n#SBATCH --nodes=1\n#SBATCH --ntasks=8\n#SBATCH --cpus-per-task=1\n#SBATCH --time=04:00:00\nexport OMP_NUM_THREADS=1\nsrun vasp_std\n'
    (staged / 'submit.slurm').write_text(script)
    gate.confirm_physics(staged, 'nonmagnetic bulk silicon PBE bands')
    approve(staged)
    before = resource_context(staged, 'convergence')
    incar = (staged / 'convergence/mesh-0/INCAR').read_bytes()
    configure_resources(staged, 'convergence', {**before['resources'], 'parallel_tests': 3}, before['signature'],
                        dict(partition='compute', nodes=1, cores=24, walltime='04:00:00'))
    core.verify(staged)
    assert (staged / 'convergence/mesh-0/INCAR').read_bytes() == incar
    with pytest.raises(ValueError):
        gate.require_review(staged, 'convergence')
    approve(staged)
    assert gate.require_review(staged, 'convergence') == 'convergence'


def test_confirmation_and_structured_review_are_required(staged):
    with pytest.raises(ValueError, match='确认'):
        gate.context(staged)
    gate.confirm_physics(staged, '非磁性三维硅的 PBE 能带')
    with pytest.raises(ValueError, match='未审阅'):
        gate.require_review(staged)
    data = gate.context(staged)
    bad = checks_for('convergence')
    bad[0]['judgment'] = 'pending'
    assert not gate.record(staged, data['snapshot'], 'convergence', bad, 'test')['allowed']
    with pytest.raises(ValueError, match='阻断'):
        gate.require_review(staged)
    approve(staged)
    assert gate.require_review(staged) == 'convergence'
    with pytest.raises(ValueError, match='当前'):
        gate.require_review(staged, 'bands')
    with pytest.raises(ValueError, match='缺项'):
        gate.record(staged, data['snapshot'], 'convergence', bad[:1], 'test')
    from vasp_agent.workflow.input_review import record, context
    with pytest.raises(ValueError, match='结构化'):
        record(staged, context(staged)['snapshot'], 'pass', 'cannot bypass', 'test')


def test_physics_change_invalidates_review(staged):
    gate.confirm_physics(staged, 'goal one')
    approve(staged)
    gate.confirm_physics(staged, 'goal two')
    with pytest.raises(ValueError, match='变化'):
        gate.require_review(staged)


def test_four_runs_pause_and_do_not_relaunch_completed_calculations(staged, monkeypatch):
    gate.confirm_physics(staged, '非磁性三维硅的能带')
    launches = []
    def fake_execute(root, name, command):
        folder = root / name
        structure = Poscar.from_file(folder / 'POSCAR').structure
        stamp = folder / 'complete.json'
        if not stamp.exists():
            launches.append(name)
            (folder / 'vasprun.xml').write_text('mock output ' + name)
            data = {'xml_sha256': core.digest(folder / 'vasprun.xml')}
            if name == 'scf':
                (folder / 'CHGCAR').write_text('mock charge')
                data['charge_sha256'] = core.digest(folder / 'CHGCAR')
            core.save(stamp, data)
        return NS(final_structure=structure, final_energy=-10. * len(structure), converged_ionic=True,
            parameters={'NELM':150, 'NBANDS':16, 'NELECT':4}, efermi=0.,
            ionic_steps=[{'forces': np.zeros((len(structure),3)).tolist(), 'stress': np.zeros((3,3)).tolist(), 'electronic_steps':[{}]}])
    monkeypatch.setattr(core, 'execute', fake_execute)
    monkeypatch.setattr(full, 'analyze_full', lambda root: core.save(root / 'workflow-state.json', {'status':'complete'}))
    for i, phase in enumerate(gate.PHASES):
        assert gate.current(staged) == phase
        if i:
            saved = (staged / 'convergence.json').read_bytes()
            (staged / 'convergence.json').write_text('{}')
            with pytest.raises(ValueError, match='证据文件'):
                gate.context(staged)
            (staged / 'convergence.json').write_bytes(saved)
            with pytest.raises(ValueError, match='未审阅'):
                full.run_full(staged, ['mock'])
        approve(staged)
        full.run_full(staged, ['mock'])
        assert len(launches) == (6, 11, 12, 13)[i]
        state = json.loads((staged / 'workflow-state.json').read_text())
        assert state['status'] == ('complete' if phase == 'bands' else 'awaiting_review')
    assert len(set(launches)) == 13
    assert gate.current(staged) == 'complete'


def test_missing_upstream_completion_never_restarts(staged):
    with pytest.raises(ValueError, match='缺少'):
        gate.check_stage(staged, 'convergence/encut-0', 'relaxation')
    with pytest.raises(ValueError, match='越过'):
        gate.check_stage(staged, 'scf', 'convergence')


def test_confirmation_is_not_a_model_tool(staged):
    box = Toolbox(staged.parents[1])
    assert 'confirm_physics' not in [x['name'] for x in box.schemas()]
    assert 'submit_stage' not in [x['name'] for x in box.schemas()]
    assert not box.call('confirm_physics', {})['ok']


def test_confirmation_channel_must_name_an_operator_surface(staged):
    gate.confirm_physics(staged, 'goal', 'operator_chat_menu')
    assert gate.physics(staged)['confirmed_by'] == 'operator_chat_menu'
    # A value the operator surfaces never produce -- including one a model
    # might invent -- is refused rather than written into the record.
    with pytest.raises(ValueError, match='来源无效'):
        gate.confirm_physics(staged, 'goal', 'ai_agent')
    assert gate.physics(staged)['confirmed_by'] == 'operator_chat_menu'


def test_confirmation_records_the_operator_assertion_not_a_structure_finding(staged):
    data = gate.confirm_physics(staged, 'goal')
    assert data['assumptions_source'] == 'operator_assertion_not_structure_derived'
    saved = json.loads((staged / 'physics-model.json').read_text(encoding='utf-8'))
    assert saved['confirmed_by'] == 'operator_slash_command'
    # A record that names a different source is refused, rather than read back
    # as though the program had derived these properties from the structure.
    core.save(staged / 'physics-model.json', {**saved, 'assumptions_source': 'structure_derived'})
    with pytest.raises(ValueError, match='假设来源'):
        gate.physics(staged)
    # A record written before the field existed keeps working: those
    # confirmations were the same operator assertion, just unlabelled.
    core.save(staged / 'physics-model.json', {k: v for k, v in saved.items() if k != 'assumptions_source'})
    assert gate.physics(staged)['goal'] == 'goal'
    core.save(staged / 'physics-model.json', {k: v for k, v in saved.items() if k != 'confirmed_by'})
    assert gate.physics(staged)['goal'] == 'goal'


def test_release_without_channel_support_is_refused_not_crashed(staged, monkeypatch):
    import vasp_agent.remote.remote_worker as worker
    root = staged.parents[1]
    folder = root / 'jobs/si'
    folder.mkdir(parents=True)
    core.save(folder / 'remote-job.json', {'release': str(root)})
    # Stands in for a release whose stage_gate predates the channel argument:
    # calling it would raise a bare TypeError, so the guard must catch it first.
    old = NS(confirm_physics=lambda root, goal: pytest.fail('the old engine must not be called'))
    monkeypatch.setattr(worker, 'engine_module', lambda name: old)
    with pytest.raises(ValueError, match='确认来源'):
        worker.dispatch({'config': {'root': str(root)}, 'action': 'confirm_physics',
                         'job_name': 'si', 'goal': 'goal', 'channel': 'operator_slash_command'})


def test_prior_job_must_be_terminal(staged, monkeypatch):
    import subprocess
    import vasp_agent.remote.remote_worker as worker
    folder = staged / 'submissions'
    folder.mkdir()
    core.save(folder / 'convergence.json', {'job_id': '123'})
    monkeypatch.setattr(subprocess, 'run', lambda *a, **k: NS(stdout='123\n'))
    with pytest.raises(ValueError, match='尚未退出'):
        worker.check_previous_job(staged, 'relaxation')
    responses = iter(['', '123|FAILED|1:0\n'])
    monkeypatch.setattr(subprocess, 'run', lambda *a, **k: NS(stdout=next(responses)))
    with pytest.raises(ValueError, match='正常退出'):
        worker.check_previous_job(staged, 'relaxation')


def test_physics_slash_requires_literal_operator_confirmation(staged):
    from vasp_agent.chat.stage_commands import execute
    box = Toolbox(staged.parents[1])
    responses = iter(['goal', 'no'])
    c = NS(agent=NS(tools=box), reader=lambda _: next(responses), output=lambda _: None)
    execute(c, 'local si')
    assert not (staged / 'physics-model.json').exists()
    responses = iter(['goal', '确认'])
    execute(c, 'local si')
    assert gate.physics(staged)['confirmed_by'] == 'operator_slash_command'


@pytest.mark.parametrize('uncertain', [False, True])
def test_remote_phase_submission_is_explicit_and_idempotent(staged, monkeypatch, uncertain):
    import shutil
    import subprocess
    import vasp_agent.remote.remote_worker as worker
    root = staged.parents[1]
    folder = root / 'jobs/si'
    shutil.copytree(staged, folder)
    script = folder / 'submit.slurm'
    script.write_text('#!/bin/bash\n#SBATCH --partition=compute\n#SBATCH --nodes=1\n#SBATCH --ntasks=8\n#SBATCH --time=04:00:00\n')
    core.save(folder / 'remote-job.json', {'release': str(__import__('pathlib').Path(gate.__file__).parent),
                                         'script_sha256': core.digest(script)})
    environment = {'executable_sha256': 'fixture', 'environment_sha256': 'fixture', 'executable': '/fake/vasp_std'}
    monkeypatch.setattr(worker, 'inspect_environment', lambda *a: environment)
    core.save(folder / 'environment-review.json', environment)
    gate.confirm_physics(folder, 'goal')
    approve(folder)
    calls = []
    def launch(command, **kwargs):
        calls.append(command)
        # The phase and the session ride the same --export argument, so assert on
        # its comma-separated items rather than on the whole argv element.
        export = [token for token in command if token.startswith('--export=')].pop()
        assert 'VASP_AGENT_PHASE=convergence' in export.split('=', 1)[1].split(',')
        assert kwargs['env']['VASP_AGENT_PHASE'] == 'convergence'
        if uncertain:
            raise subprocess.TimeoutExpired('sbatch', 30)
        return NS(returncode=0, stdout='123', stderr='')
    monkeypatch.setattr(worker.subprocess, 'run', launch)
    monkeypatch.setattr(worker, 'capacity_preflight', lambda script: {'capacity_checked': True})
    payload = dict(config={'root': str(root)}, action='submit', job_name='si', phase='convergence')
    with pytest.raises(ValueError, match='阶段'):
        worker.dispatch({**payload, 'phase':'bands'})
    if uncertain:
        with pytest.raises(worker.SubmissionError, match='状态不明确'):
            worker.dispatch(payload)
    else:
        assert worker.dispatch(payload)['job_id'] == '123'
    again = worker.dispatch(payload)
    assert again['status'] == ('submission_uncertain' if uncertain else 'submitted')
    assert len(calls) == 1


def test_identical_review_retry_is_idempotent(staged):
    gate.confirm_physics(staged, 'same goal')
    first = approve(staged)
    second = approve(staged)
    assert second['already_recorded']
    assert second['time'] == first['time']
    assert len(list((staged / 'reviews').glob('*.json'))) == 1


@pytest.mark.parametrize('action', ['submission_preflight', 'submit'])
def test_capacity_blocks_before_reservation_or_sbatch(staged, monkeypatch, action):
    import shutil
    import vasp_agent.remote.remote_worker as worker
    root = staged.parents[1]
    folder = root / 'jobs/si'
    shutil.copytree(staged, folder)
    script = folder / 'submit.slurm'
    script.write_text('#!/bin/bash\n#SBATCH --partition=compute\n#SBATCH --nodes=1\n#SBATCH --ntasks=8\n#SBATCH --time=04:00:00\n')
    core.save(folder / 'remote-job.json', {'release': str(__import__('pathlib').Path(gate.__file__).parent),
                                         'script_sha256': core.digest(script)})
    environment = {'executable_sha256': 'fixture', 'environment_sha256': 'fixture', 'executable': '/fake/vasp_std'}
    monkeypatch.setattr(worker, 'inspect_environment', lambda *a: environment)
    core.save(folder / 'environment-review.json', environment)
    gate.confirm_physics(folder, 'goal')
    approve(folder)
    calls = []
    def reject(text):
        calls.append(text)
        raise worker.SubmissionError('SCHEDULER_CAPACITY_EXCEEDED', 'capacity')
    monkeypatch.setattr(worker, 'capacity_preflight', reject)
    monkeypatch.setattr(worker.subprocess, 'run', lambda *a, **k: pytest.fail('must not launch'))
    with pytest.raises(worker.SubmissionError):
        worker.dispatch(dict(config={'root': str(root)}, action=action, job_name='si', phase='convergence'))
    assert len(calls) == 1
    assert not (folder / 'submissions/convergence.json').exists()


def test_phase_env_may_only_restate_checkpoint(tmp_path, monkeypatch):
    from vasp_agent.workflow import stage_gate as gate
    gate.core.save(tmp_path / 'checkpoint.json', {'completed': ['convergence'], 'next': 'relaxation'})
    monkeypatch.setenv('VASP_AGENT_PHASE', 'relaxation')
    assert gate.authorized_phase(tmp_path) == 'relaxation'
    monkeypatch.setenv('VASP_AGENT_PHASE', 'bands')
    with pytest.raises(ValueError, match='不一致'):
        gate.authorized_phase(tmp_path)
    monkeypatch.delenv('VASP_AGENT_PHASE')
    assert gate.authorized_phase(tmp_path) == 'relaxation'


def test_unknown_stage_directory_is_actionable(tmp_path):
    from vasp_agent.workflow import stage_gate as gate
    with pytest.raises(ValueError, match='无法识别阶段目录 relx-1'):
        gate.check_stage(tmp_path, 'relx-1', 'relaxation')


def test_incomplete_completion_records_name_the_record_not_keyerror(staged, monkeypatch):
    gate.confirm_physics(staged, '非磁性三维硅的能带')
    def fake_execute(root, name, command):
        folder = root / name
        structure = Poscar.from_file(folder / 'POSCAR').structure
        if not (folder / 'complete.json').exists():
            (folder / 'vasprun.xml').write_text('mock output ' + name)
            data = {'xml_sha256': core.digest(folder / 'vasprun.xml')}
            if name == 'scf':
                (folder / 'CHGCAR').write_text('mock charge')
                data['charge_sha256'] = core.digest(folder / 'CHGCAR')
            core.save(folder / 'complete.json', data)
        return NS(final_structure=structure, final_energy=-10. * len(structure), converged_ionic=True,
            parameters={'NELM': 150, 'NBANDS': 16, 'NELECT': 4}, efermi=0.,
            ionic_steps=[{'forces': np.zeros((len(structure), 3)).tolist(), 'stress': np.zeros((3, 3)).tolist(), 'electronic_steps': [{}]}])
    monkeypatch.setattr(core, 'execute', fake_execute)
    for _ in ('convergence', 'relaxation', 'scf'):
        approve(staged)
        full.run_full(staged, ['mock'])
    stamp = staged / 'scf/complete.json'
    good = json.loads(stamp.read_text(encoding='utf-8'))
    core.save(stamp, {k: v for k, v in good.items() if k != 'charge_sha256'})
    with pytest.raises(core.ToolError) as missing_charge:
        gate.evidence(staged, 'bands')
    assert missing_charge.value.error_code == 'FILE_NOT_FOUND'
    core.save(stamp, good)
    first = staged / 'convergence/encut-0/complete.json'
    core.save(first, {})
    with pytest.raises(core.ToolError) as missing_xml:
        gate.evidence(staged, 'bands')
    assert missing_xml.value.error_code == 'UNVERIFIED_METADATA'
    assert 'convergence/encut-0' in str(missing_xml.value)
