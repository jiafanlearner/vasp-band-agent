"""Operator-confirmed resume of a phase whose job ended early; fake Slurm and fake VASP only."""
import json
from types import SimpleNamespace as NS

import pytest
from pymatgen.io.vasp.inputs import Poscar

import vasp_agent.remote.remote_worker as worker
from vasp_agent.workflow import full_workflow as full, stage_gate as gate, vasp_band_agent as core
from test_stage_gate import staged, approve
from test_full_workflow import result
from test_remote_cancel import menu_box, wire
from test_submission_permissions import commands

JOB = '71442506'
SLURM = '#SBATCH --partition=compute\n#SBATCH --time=04:00:00\n#SBATCH --nodes=1\n#SBATCH --ntasks=8\nexport OMP_NUM_THREADS=1\nsrun vasp_std\n'
REUSED = ['relax-1', 'relax-2', 'validation/higher-cutoff', 'validation/reference']
CUT = 'validation/denser-mesh'


class Walltime(Exception):
    """Slurm killing the job at its time limit."""


@pytest.fixture
def vasp(monkeypatch):
    """Fake VASP with the real execute()'s rules: reuse a completion record, refuse unaccepted output."""
    state = NS(launches=[], interrupt=set())
    def read(folder):
        return result(Poscar.from_file(folder / 'POSCAR').structure, energy=float((folder / 'vasprun.xml').read_text()))
    def execute(root, name, command, event_root=None):
        folder = root / name
        if (folder / 'complete.json').exists():
            if json.loads((folder / 'complete.json').read_text())['xml_sha256'] != core.digest(folder / 'vasprun.xml'):
                raise ValueError(f'{name} 输出与完成记录不一致。')
            return read(folder)
        if any((folder / f).exists() for f in ('vasprun.xml', 'OUTCAR', 'stdout.log')):
            raise ValueError(f'{name} 存在未验收输出，请检查日志并在新目录重建任务。')
        state.launches.append(name)
        (folder / 'stdout.log').write_text('running\n')
        if name in state.interrupt:
            (folder / 'OSZICAR').write_text('   1 F= -.10E+02\n   2 F= -.10E+02\n')
            (folder / 'OUTCAR').write_text('partial')
            (folder / 'WAVECAR').write_bytes(b'\0' * 64)
            raise Walltime(name)
        (folder / 'vasprun.xml').write_text('-10.0')
        core.save(folder / 'complete.json', {'xml_sha256': core.digest(folder / 'vasprun.xml')})
        return read(folder)
    monkeypatch.setattr(core, 'execute', execute)
    monkeypatch.setattr(core, 'read_result', read)
    return state


@pytest.fixture
def timed_out(staged, vasp, tmp_path):
    """The La2CuO4 state: relaxation job hit its walltime inside validation/denser-mesh."""
    gate.confirm_physics(staged, 'nonmagnetic bulk PBE bands')
    approve(staged)
    full.run_full(staged, ['mock'])
    (staged / 'submit.slurm').write_text(SLURM)
    (staged / 'remote-job.json').write_text(json.dumps({'release': str(tmp_path / 'release'),
                                                        'script_sha256': worker.sha(staged / 'submit.slurm')}))
    (staged / 'submissions').mkdir()
    (staged / 'submissions/convergence.json').write_text(json.dumps({'status': 'submitted', 'job_id': '71000001'}))
    (staged / 'submissions/relaxation.json').write_text(json.dumps({'status': 'submitted', 'job_id': JOB, 'phase': 'relaxation'}))
    approve(staged)
    vasp.launches.clear()
    vasp.interrupt.add(CUT)
    (staged / '.agent.lock').write_text('')  # run() never reached its finally
    with pytest.raises(Walltime):
        full.run_full(staged, ['mock'])
    vasp.interrupt.clear()
    vasp.launches.clear()
    return staged


def scheduler(monkeypatch, state='TIMEOUT', queue=''):
    calls = []
    def run(args, **kwargs):
        calls.append(args)
        if args[0] == 'squeue':
            return NS(returncode=0, stdout=queue, stderr='')
        assert args[0] == 'sacct'
        return NS(returncode=0, stdout=f'{JOB}|{state}|0:0\n{JOB}.batch|CANCELLED|0:15\n{JOB}.extern|COMPLETED|0:0\n', stderr='')
    monkeypatch.setattr(worker.subprocess, 'run', run)
    return calls


def snapshot(folder):
    """Every file and its bytes: proof that a refusal changed nothing."""
    return {p.relative_to(folder).as_posix(): p.read_bytes() for p in sorted(folder.rglob('*'))
            if p.is_file() and p.name != '.submission-recovery.lock'}


def test_timeout_state_explains_the_evidence_error(timed_out):
    # The phase's own summaries (relaxed-*.POSCAR, relaxation.json, ...) were written
    # by the interrupted run, so the evidence no longer matches the checkpoint.
    assert (timed_out / 'relaxed-2.POSCAR').exists()
    assert json.loads((timed_out / 'workflow-state.json').read_text())['status'] == 'running'
    with pytest.raises(ValueError, match='证据文件发生变化.*续算'):
        gate.context(timed_out)


def test_resume_after_timeout_reuses_completed_stages_and_reruns_only_the_interrupted_one(timed_out, vasp, monkeypatch):
    calls = scheduler(monkeypatch)
    before = snapshot(timed_out)
    preview = worker.prepare_phase_resume(timed_out, 'relaxation')
    assert preview['preview'] and not preview['prepared'] and snapshot(timed_out) == before
    assert preview['reused'] == REUSED and preview['rerun'] == [CUT] and preview['stale_lock']
    assert preview['scheduler_state'] == 'TIMEOUT' and preview['reuse_checked_by'] == ['worker', 'engine']
    with pytest.raises(ValueError, match='不一致'):
        worker.prepare_phase_resume(timed_out, 'relaxation', '1')
    assert snapshot(timed_out) == before

    done = worker.prepare_phase_resume(timed_out, 'relaxation', JOB)
    assert done['prepared'] and not done['submission_authorized']
    assert calls[0] == ['squeue', '--me', '-h', '-o', '%i'] and calls[1][:4] == ['sacct', '-n', '-j', JOB]
    archive = timed_out / 'interrupted' / JOB
    # Moved, byte for byte, never deleted.
    for name in ('OSZICAR', 'OUTCAR', 'WAVECAR', 'stdout.log'):
        assert not (timed_out / CUT / name).exists()
        assert (archive / CUT / name).read_bytes() == before[f'{CUT}/{name}']
    for name in ('relaxed-1.POSCAR', 'relaxed-2.POSCAR', 'relaxation.json', 'geometry-summary.json'):
        assert (archive / 'root' / name).read_bytes() == before[name] and not (timed_out / name).exists()
    # The reviewed inputs stay, so the stage reruns from them.
    for name in ('INCAR', 'KPOINTS', 'POSCAR', 'POTCAR', 'inputs.json'):
        assert (timed_out / CUT / name).read_bytes() == before[f'{CUT}/{name}']
    assert not (timed_out / '.agent.lock').exists() and (archive / '.agent.lock').exists()
    assert (timed_out / f'submissions/relaxation.timeout-{JOB}.json').read_bytes() == before['submissions/relaxation.json']
    assert not (timed_out / 'submissions/relaxation.json').exists()
    assert json.loads((timed_out / 'workflow-state.json').read_text())['status'] == 'awaiting_review'
    assert json.loads((archive / 'workflow-state.json').read_text())['status'] == 'running'
    event = [json.loads(line) for line in (timed_out / 'events.jsonl').read_text().splitlines()][-1]
    assert event['action'] == 'phase_resume_prepared' and event['stale_lock_archived'] and event['rerun'] == [CUT]
    # Completed stages untouched.
    for name in REUSED:
        assert (timed_out / name / 'vasprun.xml').read_bytes() == before[f'{name}/vasprun.xml']

    # The review works again, its snapshot covers the reused stages, and the old review no longer counts.
    context = gate.context(timed_out)
    assert context['evidence']['resumed']['relax-1/vasprun.xml'] == core.digest(timed_out / 'relax-1/vasprun.xml')
    assert set(context['evidence']['resumed']) >= {f'{n}/complete.json' for n in REUSED}
    with pytest.raises(ValueError, match='证据已变化'):
        gate.require_review(timed_out, 'relaxation')
    approve(timed_out)
    full.run_full(timed_out, ['mock'])
    assert vasp.launches == [CUT]
    assert gate.current(timed_out) == 'scf'
    assert (timed_out / 'relaxed-2.POSCAR').read_bytes() == before['relaxed-2.POSCAR']
    assert 'resume' not in json.loads((timed_out / 'checkpoint.json').read_text())


@pytest.mark.parametrize('state', ['COMPLETED', 'FAILED', 'RUNNING', 'PENDING', 'OUT_OF_MEMORY'])
def test_refused_unless_the_job_was_cut_off(timed_out, monkeypatch, state):
    scheduler(monkeypatch, state=state)
    before = snapshot(timed_out)
    with pytest.raises(ValueError, match='TIMEOUT/CANCELLED/NODE_FAIL/PREEMPTED'):
        worker.prepare_phase_resume(timed_out, 'relaxation', JOB)
    assert snapshot(timed_out) == before


@pytest.mark.parametrize('state', ['CANCELLED by 1234', 'NODE_FAIL', 'PREEMPTED'])
def test_other_interruptions_resume(timed_out, monkeypatch, state):
    scheduler(monkeypatch, state=state)
    done = worker.prepare_phase_resume(timed_out, 'relaxation', JOB)
    word = state.split()[0]
    assert done['scheduler_state'] == word
    assert (timed_out / f'submissions/relaxation.{word.lower()}-{JOB}.json').exists()


def test_refused_while_still_queued(timed_out, monkeypatch):
    calls = scheduler(monkeypatch, queue=f'69999999\n{JOB}\n')
    before = snapshot(timed_out)
    with pytest.raises(ValueError, match='仍在队列'):
        worker.prepare_phase_resume(timed_out, 'relaxation', JOB)
    assert [c[0] for c in calls] == ['squeue'] and snapshot(timed_out) == before


def test_refused_when_a_completed_stage_does_not_verify(timed_out, monkeypatch):
    scheduler(monkeypatch)
    (timed_out / 'relax-2/vasprun.xml').write_text('-11.0')
    before = snapshot(timed_out)
    with pytest.raises(ValueError, match='relax-2 的输出与完成记录不一致'):
        worker.prepare_phase_resume(timed_out, 'relaxation', JOB)
    assert snapshot(timed_out) == before


def test_refused_when_a_completed_stage_input_left_the_reviewed_plan(timed_out, monkeypatch):
    scheduler(monkeypatch)
    plan = json.loads((timed_out / 'input-plan.json').read_text(encoding='utf-8'))
    plan['incars']['validation/reference']['EDIFF'] = 1e-5
    (timed_out / 'input-plan.json').write_text(json.dumps(plan), encoding='utf-8')
    with pytest.raises(ValueError, match='validation/reference 的 INCAR 与审阅方案不一致'):
        worker.prepare_phase_resume(timed_out, 'relaxation', JOB)
    assert (timed_out / 'submissions/relaxation.json').exists()


def test_refused_when_interrupted_inputs_changed_or_run_failed(timed_out, monkeypatch):
    scheduler(monkeypatch)
    incar = (timed_out / CUT / 'INCAR').read_text()
    (timed_out / CUT / 'INCAR').write_text(incar + 'NELM = 999\n')
    with pytest.raises(ValueError, match='中断阶段的输入已改变'):
        worker.prepare_phase_resume(timed_out, 'relaxation', JOB)
    (timed_out / CUT / 'INCAR').write_text(incar)
    (timed_out / 'workflow-state.json').write_text('{"status": "failed", "error": "relax-2 未收敛"}', encoding='utf-8')
    with pytest.raises(ValueError, match='先诊断'):
        worker.prepare_phase_resume(timed_out, 'relaxation', JOB)
    assert (timed_out / 'submissions/relaxation.json').exists() and (timed_out / CUT / 'OSZICAR').exists()


def test_an_existing_archive_is_never_overwritten(timed_out, monkeypatch):
    scheduler(monkeypatch)
    (timed_out / 'interrupted' / JOB).mkdir(parents=True)
    with pytest.raises(ValueError, match='不覆盖'):
        worker.prepare_phase_resume(timed_out, 'relaxation', JOB)


def test_reused_stage_changed_after_resume_blocks_review_and_submit(timed_out, monkeypatch):
    scheduler(monkeypatch)
    worker.prepare_phase_resume(timed_out, 'relaxation', JOB)
    # A consistent swap of output and completion record still differs from the confirmed hashes.
    (timed_out / 'relax-1/vasprun.xml').write_text('-12.0')
    core.save(timed_out / 'relax-1/complete.json', {'xml_sha256': core.digest(timed_out / 'relax-1/vasprun.xml')})
    with pytest.raises(ValueError, match='续算复用的 relax-1'):
        gate.context(timed_out)
    with pytest.raises(ValueError, match='续算复用的 relax-1'):
        worker.TASK_ACTIONS['stage_context']({}, {}, timed_out)
    with pytest.raises(ValueError, match='续算复用的 relax-1'):
        worker.TASK_ACTIONS['submission_preflight']({'phase': 'relaxation'}, {}, timed_out)


def test_worker_checks_reuse_for_an_engine_without_the_marker(timed_out, monkeypatch):
    """A task bound to a release older than the resume record is still checked by the worker."""
    scheduler(monkeypatch)
    monkeypatch.delattr(gate, 'RESUME_VERSION')
    monkeypatch.setattr(gate, 'resumed', lambda root, phase: None)
    done = worker.prepare_phase_resume(timed_out, 'relaxation', JOB)
    assert done['reuse_checked_by'] == ['worker']
    (timed_out / 'relax-2/inputs.json').write_text('{}')
    with pytest.raises(ValueError, match='续算复用的 relax-2/inputs.json'):
        worker.TASK_ACTIONS['stage_review']({}, {}, timed_out)


def test_action_is_dispatched():
    assert worker.TASK_ACTIONS['prepare_phase_resume'] is worker._prepare_phase_resume


def test_menu_resume_needs_operator_choice_and_a_fresh_grant(tmp_path, timed_out, monkeypatch):
    scheduler(monkeypatch)
    box, prompts, shown = menu_box(tmp_path, timed_out, monkeypatch, 'q')
    key = ('test', 'La2CuO4', 'relaxation')
    box.submission_target, box.allow_submit = key, True
    box.confirmed_resource_selections[key] = {'signature': 'old', 'resources': {'cores_per_test': 32}}
    box.monitor.register('test', 'La2CuO4', 'relaxation', JOB)
    declined = box.request_job_cancellation('test', 'La2CuO4', 'relaxation', 'resume')
    assert declined['status'] == 'cancelled_by_operator' and box.allow_submit
    assert (timed_out / 'submissions/relaxation.json').exists()
    assert any('复用' in line and 'relax-1' in line for line in shown)
    assert any('重算' in line and CUT in line for line in shown)

    box.plan_reader = lambda prompt: prompts.append(prompt) or '1'
    data = box.call('request_job_cancellation', {'server': 'test', 'job_name': 'La2CuO4',
                                                 'phase': 'relaxation', 'action': 'resume'})
    data = json.loads(data) if isinstance(data, str) else data
    assert data['ok'], data
    assert data['result']['prepared'] and data['result']['rerun'] == [CUT]
    assert not box.allow_submit and box.submission_target is None
    assert key not in box.confirmed_resource_selections
    row = box.monitor.status()['tasks'][0]
    assert not row['active'] and row['state'] == 'TIMEOUT'
    with pytest.raises(Exception, match='request_submission'):
        box.remote_submit('test', 'La2CuO4', 'relaxation')
    assert not (timed_out / 'submissions/relaxation.json').exists()


def test_menu_resume_without_operator_does_nothing(tmp_path, timed_out, monkeypatch):
    scheduler(monkeypatch)
    box, _, _ = menu_box(tmp_path, timed_out, monkeypatch, '1')
    box.plan_reader = None
    result = box.request_job_cancellation('test', 'La2CuO4', 'relaxation', 'resume')
    assert result['status'] == 'operator_confirmation_required' and result['preview']
    assert (timed_out / 'submissions/relaxation.json').exists()


def test_slash_resume_requires_typed_job_id(tmp_path, timed_out, monkeypatch):
    scheduler(monkeypatch)
    actions = wire(monkeypatch, timed_out)
    commands(tmp_path, ['no']).handle('/cancel resume test La2CuO4 relaxation')
    assert (timed_out / 'submissions/relaxation.json').exists()
    commands(tmp_path, [JOB]).handle('/cancel resume test La2CuO4 relaxation')
    assert actions[-1] == ('prepare_phase_resume', {'phase': 'relaxation', 'confirm_job_id': JOB})
    assert (timed_out / f'submissions/relaxation.timeout-{JOB}.json').exists()
