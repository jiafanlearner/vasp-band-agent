"""Offline tests for P1e-1 contract-gated in-place interrupted-phase resume (`vasp_prepare_resume`)."""
from __future__ import annotations

import contextlib
from datetime import datetime, timedelta, timezone
import io
import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from pymatgen.io.vasp.inputs import Kpoints, Potcar

from vasp_agent.chat import release_check
from vasp_agent.common import agent_paths
from vasp_agent.control import ControlService
from vasp_agent.permissions import contract_ui, task_contract
from vasp_agent.remote import remote_compute, remote_worker
from vasp_agent.workflow import full_workflow, stage_gate
import vasp_agent.workflow.vasp_band_agent as core

ROOT = Path(__file__).resolve().parents[1]
EXAMPLE_POSCAR = ROOT / 'examples/Si.POSCAR'
FAKE_POTCAR = 'PAW_PBE Si 05Jan2001\n'

SLURM_TEMPLATE = """#!/bin/bash
#SBATCH --job-name=va-demo
#SBATCH --partition=compute
#SBATCH --nodes=1
#SBATCH --ntasks=16
#SBATCH --cpus-per-task=1
#SBATCH --time=02:00:00
#SBATCH --output=slurm-%j.log
export OMP_NUM_THREADS=1
python3 vasp_band_agent.py run . -- srun vasp_std
"""


@pytest.fixture(autouse=True)
def fake_pbe_potcar(monkeypatch):
    fake = [SimpleNamespace(element='Si', functional='PBE', TITEL='PAW_PBE Si 05Jan2001', ZVAL=4.0, enmax=245.345)]
    monkeypatch.setattr(Potcar, 'from_str', classmethod(lambda _cls, _text: fake))
    monkeypatch.setattr(Potcar, 'from_file', classmethod(lambda _cls, _path: fake))


def _setup_workspace(tmp_path: Path) -> Path:
    structures = tmp_path / 'structures'
    structures.mkdir(parents=True, exist_ok=True)
    (structures / 'Si.POSCAR').write_bytes(EXAMPLE_POSCAR.read_bytes())
    potcar = tmp_path / 'potentials/PBE/Si/POTCAR'
    potcar.parent.mkdir(parents=True, exist_ok=True)
    potcar.write_text(FAKE_POTCAR, encoding='utf-8', newline='\n')
    return tmp_path


def _alloc_envelope(phases: list[str] | None = None, *, allow_array: bool = False) -> dict:
    allowed = phases or ['convergence', 'relaxation', 'scf', 'bands']
    defaults = {}
    for phase in allowed:
        defaults[phase] = {
            'submission_mode': 'array' if (allow_array and phase == 'convergence') else 'allocation',
            'parallel_tests': 2 if (allow_array and phase == 'convergence') else 1,
            'cores_per_test': 8,
            'nodes': 1,
            'partition': 'compute',
            'walltime': '02:00:00',
        }
    return {
        'allowed_modes': ['allocation', 'array'] if allow_array else ['allocation'],
        'allowed_partitions': ['compute'],
        'max_parallel_tests': 2 if allow_array else 1,
        'max_cores_per_test': 16,
        'max_nodes': 2,
        'max_total_cores': 32,
        'max_walltime': '04:00:00',
        'phase_defaults': defaults,
    }


def _record_green_review(folder: Path, phase: str) -> dict:
    remote_worker.check_resume(folder)
    ctx = stage_gate.context(folder)
    assert ctx['phase'] == phase
    checks = [
        {
            'item': item,
            'judgment': 'pending' if (phase == 'convergence' and item == 'validation_plan') else 'pass',
            'blocking': False,
            'evidence': f'verified {phase}/{item}',
            'reason': 'offline test pass',
            'next_action': '无需',
        }
        for item in ctx['required_checks']
    ]
    return stage_gate.record(folder, ctx['snapshot'], phase, checks, 'checker:test:pro:chk001')


def _complete_convergence_substage(folder: Path, rel: str) -> None:
    """Create one real hash-valid completed convergence sub-stage keeping manifest-hashed inputs intact."""
    stage = folder / rel
    assert (stage / 'INCAR').is_file() and (stage / 'KPOINTS').is_file()
    if not (stage / 'POSCAR').is_file():
        (stage / 'POSCAR').write_bytes((folder / 'initial.POSCAR').read_bytes())
    if not (stage / 'POTCAR').is_file():
        (stage / 'POTCAR').write_bytes((folder / 'POTCAR').read_bytes())
    inputs = {name: core.digest(stage / name) for name in ('POSCAR', 'POTCAR', 'INCAR', 'KPOINTS')}
    core.save(stage / 'inputs.json', inputs)
    xml = stage / 'vasprun.xml'
    xml.write_text('<modeling/>\n', encoding='utf-8', newline='\n')
    core.save(stage / 'complete.json', {'xml_sha256': core.digest(xml)})


def _complete_phase_stub(folder: Path, phase: str) -> None:
    plan = json.loads((folder / 'input-plan.json').read_text(encoding='utf-8'))
    for rel in plan['incars']:
        if stage_gate.phase_of(rel) != phase:
            continue
        stage_dir = folder / rel
        stage_dir.mkdir(parents=True, exist_ok=True)
        for name in ('POSCAR', 'INCAR', 'KPOINTS'):
            if not (stage_dir / name).is_file():
                if rel == 'scf' and name == 'KPOINTS':
                    Kpoints.gamma_automatic([4, 4, 4]).write_file(stage_dir / name)
                elif rel == 'scf' and name == 'POSCAR':
                    (stage_dir / name).write_bytes(EXAMPLE_POSCAR.read_bytes())
                else:
                    (stage_dir / name).write_text(f'{rel}:{name}\n', encoding='utf-8', newline='\n')
        xml = stage_dir / 'vasprun.xml'
        xml.write_text('<modeling/>\n', encoding='utf-8', newline='\n')
        inputs = {n: core.digest(stage_dir / n) for n in ('POSCAR', 'INCAR', 'KPOINTS')}
        core.save(stage_dir / 'inputs.json', inputs)
        core.save(stage_dir / 'complete.json', {'xml_sha256': core.digest(xml)})
    if phase == 'convergence':
        core.save(folder / 'convergence.json', {'passed': True, 'encut': 420, 'mesh': [4, 4, 4]})
    stage_gate.complete(folder, phase)


class _FakeRemoteResumeCluster:
    """Offline fake remote cluster wired to real worker_lifecycle.prepare_phase_resume and worker_resources."""

    def __init__(self, ws: Path, remote_root: Path):
        self.ws = ws
        self.jobs_dir = remote_root / 'jobs'
        self.jobs_dir.mkdir(parents=True, exist_ok=True)
        self.next_job_id = 2000
        self.queued_ids: list[str] = []
        self.sacct_states: dict[str, str] = {}
        self.calls: list[tuple[str, str, dict]] = []
        self.resume_hook = None
        self.status_hook = None
        self.configure_hook = None

    def job_folder(self, job_name: str) -> Path:
        return self.jobs_dir / job_name

    def scheduler_query(self, command: list[str], _consequence: str) -> str:
        if command[0] == 'squeue':
            return '\n'.join(self.queued_ids) + ('\n' if self.queued_ids else '')
        if command[0] == 'sacct':
            jid = command[command.index('-j') + 1]
            state = self.sacct_states.get(jid)
            return f'{jid}|{state}|0:0\n' if state else ''
        raise AssertionError(f'Unexpected scheduler command: {command}')

    def prepare(self, job_name: str, structure: str, **kwargs) -> dict:
        folder = self.job_folder(job_name)
        args = SimpleNamespace(
            structure=str(self.ws / structure),
            workdir=str(folder),
            potcar=None,
            potcar_library=str(self.ws / 'potentials/PBE'),
            potential_variants=kwargs.get('potential_variants'),
            encut=kwargs['encut'],
            mesh=kwargs['mesh'],
            nbands=kwargs['nbands'],
            relax_mode=kwargs['relax_mode'],
            parallel_tests=kwargs.get('parallel_tests') or 1,
            cores_per_test=kwargs.get('cores_per_test') or 8,
            scan_preset=kwargs.get('scan_preset'),
            band_path=kwargs.get('band_path'),
            optional_stages=kwargs.get('optional_stages'),
            stage_parameters=kwargs.get('stage_parameters'),
            encuts=kwargs.get('encuts'),
            meshes=kwargs.get('meshes'),
            scan_thresholds=kwargs.get('scan_thresholds'),
            parameter_choice=kwargs.get('parameter_choice'),
            preview_only=bool(kwargs.get('preview_only', False)),
            ediff=1e-7,
            points=20,
            symprec=0.01,
            force_tol=0.01,
            stress_tol=1.0,
            energy_tol=0.001,
            nsw=100,
        )
        with contextlib.redirect_stdout(io.StringIO()):
            res = full_workflow.prepare_full(args)
        if args.preview_only:
            return res
        script = folder / 'submit.slurm'
        script.write_text(SLURM_TEMPLATE.replace('va-demo', f'va-{job_name}'), encoding='utf-8', newline='\n')
        core.save(
            folder / 'remote-job.json',
            {'job_name': job_name, 'script_sha256': core.digest(script), 'release': 'local-package', 'version': 'test'},
        )
        return {'job_name': job_name, 'prepared': True}

    def call(self, action: str, job_name: str, **kwargs) -> dict:
        self.calls.append((action, job_name, dict(kwargs)))
        folder = self.job_folder(job_name)
        if action == 'confirm_physics':
            return stage_gate.confirm_physics(folder, kwargs['goal'], kwargs.get('channel', 'operator_slash_command'))
        if action == 'resource_context':
            return remote_worker.resource_context(folder, kwargs.get('phase'))
        if action == 'configure_resources':
            if self.configure_hook is not None:
                return self.configure_hook(folder, kwargs)
            return remote_worker.configure_resources(
                folder, kwargs.get('phase'), kwargs['resources'], kwargs['signature'],
                kwargs['limits'], kwargs.get('operation_id'),
            )
        if action == 'submit_checked':
            rec = folder / 'submissions' / f'{kwargs["phase"]}.json'
            if rec.exists():
                return {**json.loads(rec.read_text(encoding='utf-8')), 'submission_not_attempted': True, 'existing_record': True}
            try:
                core.verify(folder)
                remote_worker.check_resume(folder)
                remote_worker.check_resources((folder / 'submit.slurm').read_text(encoding='utf-8'), kwargs['limits'])
                stage_gate.require_review(folder, kwargs['phase'])
            except Exception as exc:
                return {'ready': False, 'submission_not_attempted': True, 'error_code': 'PREFLIGHT_REFUSED', 'error': str(exc)}
            self.next_job_id += 1
            jid = str(self.next_job_id)
            cfg = json.loads((folder / 'config.json').read_text(encoding='utf-8'))
            data = {
                'status': 'submitted', 'job_id': jid, 'phase': kwargs['phase'],
                'submission_mode': cfg.get('submission_mode', 'allocation'),
            }
            rec.parent.mkdir(exist_ok=True)
            core.save(rec, data)
            core.save(folder / 'remote-submission.json', data)
            return data
        if action == 'prepare_phase_resume':
            if self.resume_hook is not None:
                return self.resume_hook(folder, kwargs)
            return remote_worker.prepare_phase_resume(folder, kwargs.get('phase'), kwargs.get('confirm_job_id'))
        if action == 'status':
            if self.status_hook is not None:
                return self.status_hook(folder, kwargs)
            info: dict = {}
            for name in ('checkpoint.json', 'workflow-state.json'):
                if (folder / name).is_file():
                    info[name] = json.loads((folder / name).read_text(encoding='utf-8'))
            info['stage_submissions'] = {
                p.stem: json.loads(p.read_text(encoding='utf-8'))
                for p in (folder / 'submissions').glob('*.json')
                if p.stem in ('convergence', 'relaxation', 'scf', 'bands', 'dos')
            } if (folder / 'submissions').is_dir() else {}
            return info
        raise AssertionError(f'Unexpected remote action: {action}')


def _install_fake_remote(monkeypatch, ws: Path, remote_root: Path) -> _FakeRemoteResumeCluster:
    cluster = _FakeRemoteResumeCluster(ws, remote_root)
    monkeypatch.setattr(release_check, 'ensure_current', lambda _box, _server: None)
    monkeypatch.setattr(remote_worker, 'check_previous_job', lambda _folder, _phase: None)
    monkeypatch.setattr(remote_worker, 'scheduler_query', cluster.scheduler_query)
    monkeypatch.setattr(remote_worker, 'task_release', lambda _meta: None)

    class _Adapter:
        def __init__(self, _workspace, _server):
            pass

        def prepare(self, *args, **kwargs):
            return cluster.prepare(*args, **kwargs)

        def call(self, action, job_name, **kwargs):
            return cluster.call(action, job_name, **kwargs)

    monkeypatch.setattr(remote_compute, 'RemoteCompute', _Adapter)
    return cluster


def _draft_and_approve(
    ws: Path,
    monkeypatch,
    *,
    server: str | None = 'hpc',
    job_name: str = 'si-res-01',
    allow_array: bool = False,
    budget: dict | None = None,
) -> tuple[ControlService, dict, dict]:
    maker = ControlService(ws, role='maker', actor_id='mk-res')
    args = {
        'server': server,
        'job_name': job_name,
        'structure': 'structures/Si.POSCAR',
        'encut': 400,
        'mesh': [4, 4, 4],
        'nbands': 16,
        'relax_mode': 'cell',
        'goal': 'Si 3D 非磁性 PBE 中断续算测试',
        'resource_envelope': _alloc_envelope(allow_array=allow_array),
        'allowed_phases': ['convergence', 'relaxation', 'scf', 'bands'],
        'expires_in_hours': 168,
        **({'budget': budget} if budget else {}),
    }
    draft_res = maker.call('vasp_draft_contract', args)
    assert draft_res['ok'] is True, draft_res
    monkeypatch.setattr(task_contract, 'has_real_tty', lambda stream=None: True)
    approved = contract_ui.approve_contract_interactive(
        ws, draft_res['result']['draft_id'], reader=lambda _p: '1', output=lambda _s: None,
    )
    return maker, approved['contract'], approved['ledger']


# ------------------- 1. Happy path: TIMEOUT resume, walltime bump, re-submit, cross-phase failure reset


def test_prepare_resume_timeout_archives_outputs_deactivates_monitor_updates_walltime_and_resets_next_phase(
    tmp_path, monkeypatch,
):
    ws = _setup_workspace(tmp_path / 'ws')
    cluster = _install_fake_remote(monkeypatch, ws, tmp_path / 'remote')
    maker, contract, _ = _draft_and_approve(ws, monkeypatch, server='hpc', job_name='si-timeout')
    cid = contract['contract_id']

    assert maker.call('vasp_prepare_task', {'contract_id': cid})['ok'] is True
    folder = cluster.job_folder('si-timeout')
    _record_green_review(folder, 'convergence')
    sub1 = maker.call('vasp_submit_stage', {'contract_id': cid, 'server': 'hpc', 'job_name': 'si-timeout', 'phase': 'convergence'})
    assert sub1['ok'] is True and sub1['result']['job_id'] == '2001'

    # Simulate TIMEOUT after encut-0 finished and encut-1 left partial OUTCAR + stale .agent.lock.
    _complete_convergence_substage(folder, 'convergence/encut-0')
    partial_dir = folder / 'convergence/encut-1'
    partial_dir.mkdir(parents=True, exist_ok=True)
    (partial_dir / 'OUTCAR').write_text('interrupted partial outcar\n', encoding='utf-8', newline='\n')
    (folder / '.agent.lock').write_text('pid=999\n', encoding='utf-8', newline='\n')
    core.save(folder / 'workflow-state.json', {'status': 'running', 'next': 'convergence'})
    cluster.sacct_states['2001'] = 'TIMEOUT'

    longer_res = {
        'submission_mode': 'allocation', 'parallel_tests': 1, 'cores_per_test': 8,
        'nodes': 1, 'partition': 'compute', 'walltime': '04:00:00',
    }
    resumed = maker.call(
        'vasp_prepare_resume',
        {
            'contract_id': cid, 'server': 'hpc', 'job_name': 'si-timeout', 'phase': 'convergence',
            'job_id': '2001', 'resources': longer_res, 'reason': 'Slurm TIMEOUT on encut-1, extending walltime to 4h',
        },
    )
    assert resumed['ok'] is True, resumed
    body = resumed['result']
    assert body['prepared'] is True and body['submitted'] is False and body['recovered'] is False
    assert body['job_id'] == '2001' and body['scheduler_state'] == 'TIMEOUT'
    assert body['reused'] == ['convergence/encut-0']
    assert 'convergence/encut-1' in body['rerun']
    assert body['stale_lock'] is True
    assert body['monitor_deactivated'] is True
    assert body['resources_applied'] is True and body['resources_changed'] is True
    assert body['resources']['walltime'] == '04:00:00'
    assert '#SBATCH --time=04:00:00' in (folder / 'submit.slurm').read_text(encoding='utf-8')
    assert not (folder / '.agent.lock').exists()
    assert not (folder / 'submissions/convergence.json').exists()
    assert (folder / 'interrupted/2001/convergence/encut-1/OUTCAR').is_file()
    assert body['ledger']['resume_retries_used'] == 1
    assert body['ledger']['resume_retries_remaining'] == 1
    assert body['ledger']['consecutive_failures'] == 1

    # Old JobMonitor task 2001 is deactivated with TIMEOUT state, and audit log recorded the resume.
    mon_tasks = maker.box.monitor.status()['tasks']
    old_mon = next(t for t in mon_tasks if t['job_id'] == '2001')
    assert old_mon['active'] is False and old_mon['terminal'] is True and old_mon['state'] == 'TIMEOUT'
    audit_rows = [json.loads(line) for line in agent_paths.audit_file(ws).read_text(encoding='utf-8').splitlines() if line.strip()]
    assert any(row.get('control_prepare_resume', {}).get('job_id') == '2001' for row in audit_rows)

    # Re-review against updated checkpoint['resume'] + walltime snapshot -> submit succeeds as 2002.
    _record_green_review(folder, 'convergence')
    sub2 = maker.call(
        'vasp_submit_stage',
        {'contract_id': cid, 'server': 'hpc', 'job_name': 'si-timeout', 'phase': 'convergence', 'resources': longer_res},
    )
    assert sub2['ok'] is True and sub2['result']['job_id'] == '2002'
    assert task_contract.read_ledger(ws, cid)['submissions_used'] == 2

    # Completing convergence and advancing to relaxation automatically resets consecutive_failures to 0!
    _complete_phase_stub(folder, 'convergence')
    rel_cfg = maker.call('vasp_submit_stage', {'contract_id': cid, 'server': 'hpc', 'job_name': 'si-timeout', 'phase': 'relaxation'})
    assert rel_cfg['ok'] is True and rel_cfg['result']['status'] == 'awaiting_resource_review'
    assert rel_cfg['result']['ledger']['consecutive_failures'] == 0
    assert task_contract.read_ledger(ws, cid)['consecutive_failures'] == 0


# ------------------- 2. Resumable vs ineligible states (CANCELLED/NODE_FAIL/PREEMPTED vs FAILED/array/active)


@pytest.mark.parametrize('state', ['CANCELLED', 'NODE_FAIL', 'PREEMPTED'])
def test_prepare_resume_accepts_all_resumable_states(tmp_path, monkeypatch, state):
    ws = _setup_workspace(tmp_path / 'ws')
    cluster = _install_fake_remote(monkeypatch, ws, tmp_path / 'remote')
    job = f'si-{state.lower()[:6]}'
    maker, contract, _ = _draft_and_approve(ws, monkeypatch, server='hpc', job_name=job)
    cid = contract['contract_id']
    assert maker.call('vasp_prepare_task', {'contract_id': cid})['ok'] is True
    folder = cluster.job_folder(job)
    _record_green_review(folder, 'convergence')
    sub = maker.call('vasp_submit_stage', {'contract_id': cid, 'server': 'hpc', 'job_name': job, 'phase': 'convergence'})
    jid = sub['result']['job_id']
    cluster.sacct_states[jid] = state

    res = maker.call('vasp_prepare_resume', {'contract_id': cid, 'server': 'hpc', 'job_name': job, 'phase': 'convergence'})
    assert res['ok'] is True, res
    assert res['result']['scheduler_state'] == state
    assert res['result']['resources_applied'] is False
    assert task_contract.read_ledger(ws, cid)['resume_retries_used'] == 1


@pytest.mark.parametrize('ineligible_case', ['FAILED', 'OUT_OF_MEMORY', 'COMPLETED', 'workflow_failed', 'array_mode', 'still_queued'])
def test_prepare_resume_rejects_ineligible_jobs_without_mutating_remote_or_budget(tmp_path, monkeypatch, ineligible_case):
    ws = _setup_workspace(tmp_path / 'ws')
    cluster = _install_fake_remote(monkeypatch, ws, tmp_path / 'remote')
    job = f'si-inel-{ineligible_case[:6]}'
    maker, contract, _ = _draft_and_approve(
        ws, monkeypatch, server='hpc', job_name=job, allow_array=(ineligible_case == 'array_mode'),
    )
    cid = contract['contract_id']
    assert maker.call('vasp_prepare_task', {'contract_id': cid})['ok'] is True
    folder = cluster.job_folder(job)
    _record_green_review(folder, 'convergence')
    sub = maker.call('vasp_submit_stage', {'contract_id': cid, 'server': 'hpc', 'job_name': job, 'phase': 'convergence'})
    jid = sub['result']['job_id']

    if ineligible_case == 'still_queued':
        cluster.queued_ids = [jid]
        cluster.sacct_states[jid] = 'TIMEOUT'
        expected_code = 'RESUME_JOB_STILL_ACTIVE'
    elif ineligible_case == 'workflow_failed':
        cluster.sacct_states[jid] = 'TIMEOUT'
        core.save(folder / 'workflow-state.json', {'status': 'failed', 'error': 'VASP EDDDAV crash'})
        expected_code = 'RESUME_NOT_ELIGIBLE'
    elif ineligible_case == 'array_mode':
        cluster.sacct_states[jid] = 'TIMEOUT'
        expected_code = 'RESUME_NOT_ELIGIBLE'
    else:
        cluster.sacct_states[jid] = ineligible_case
        expected_code = 'RESUME_NOT_ELIGIBLE'

    res = maker.call('vasp_prepare_resume', {'contract_id': cid, 'server': 'hpc', 'job_name': job, 'phase': 'convergence'})
    assert res['ok'] is False and res['error_code'] == expected_code
    assert (folder / 'submissions/convergence.json').is_file()
    led = task_contract.read_ledger(ws, cid)
    assert led['resume_retries_used'] == 0 and led['consecutive_failures'] == 0 and led['status'] == 'active'


# ------------------- 3. Role, local, scope, in_flight, job_id mismatch, envelope, and budget auto-halts


def test_prepare_resume_gates_local_scope_inflight_job_id_envelope_and_budget_halts(tmp_path, monkeypatch):
    ws = _setup_workspace(tmp_path / 'ws')
    cluster = _install_fake_remote(monkeypatch, ws, tmp_path / 'remote')

    # 1) Local contract refused cleanly without mutating ledger.
    maker_l, cl, _ = _draft_and_approve(ws, monkeypatch, server=None, job_name='si-loc-res')
    loc_res = maker_l.call('vasp_prepare_resume', {'contract_id': cl['contract_id'], 'server': None, 'job_name': 'si-loc-res', 'phase': 'convergence'})
    assert loc_res['ok'] is False and loc_res['error_code'] == 'RESUME_UNSUPPORTED_LOCAL'

    # 2) Scope, in_flight, array resources, over-walltime, and job_id mismatch block before remote mutation.
    maker, c1, _ = _draft_and_approve(
        ws, monkeypatch, server='hpc', job_name='si-gates', allow_array=False,
        budget={'max_submissions': 6, 'max_resume_retries': 1, 'max_repair_retries': 0, 'max_scan_retries': 0, 'max_consecutive_failures': 3},
    )
    cid = c1['contract_id']
    assert maker.call('vasp_prepare_task', {'contract_id': cid})['ok'] is True
    folder = cluster.job_folder('si-gates')
    _record_green_review(folder, 'convergence')
    jid = maker.call('vasp_submit_stage', {'contract_id': cid, 'server': 'hpc', 'job_name': 'si-gates', 'phase': 'convergence'})['result']['job_id']
    cluster.sacct_states[jid] = 'TIMEOUT'

    calls_before = len(cluster.calls)
    assert maker.call('vasp_prepare_resume', {'contract_id': cid, 'server': 'other', 'job_name': 'si-gates', 'phase': 'convergence'})['error_code'] == 'CONTRACT_SCOPE_VIOLATION'
    assert maker.call('vasp_prepare_resume', {'contract_id': cid, 'server': 'hpc', 'job_name': 'other-job', 'phase': 'convergence'})['error_code'] == 'CONTRACT_SCOPE_VIOLATION'
    assert maker.call('vasp_prepare_resume', {'contract_id': cid, 'server': 'hpc', 'job_name': 'si-gates', 'phase': 'dos'})['error_code'] == 'CONTRACT_SCOPE_VIOLATION'
    over_wall = {'submission_mode': 'allocation', 'parallel_tests': 1, 'cores_per_test': 8, 'nodes': 1, 'partition': 'compute', 'walltime': '08:00:00'}
    assert maker.call('vasp_prepare_resume', {'contract_id': cid, 'server': 'hpc', 'job_name': 'si-gates', 'phase': 'convergence', 'resources': over_wall})['error_code'] == 'CONTRACT_RESOURCE_ENVELOPE_EXCEEDED'
    arr_res = {'submission_mode': 'array', 'parallel_tests': 1, 'cores_per_test': 8, 'nodes': 1, 'partition': 'compute', 'walltime': '02:00:00'}
    assert maker.call('vasp_prepare_resume', {'contract_id': cid, 'server': 'hpc', 'job_name': 'si-gates', 'phase': 'convergence', 'resources': arr_res})['error_code'] == 'CONTRACT_RESOURCE_ENVELOPE_EXCEEDED'
    # Active in_flight on ledger blocks resume with zero remote calls.
    task_contract.reserve_submission(
        ws, cid, server='hpc', job_name='si-gates', phase='convergence',
        resources=c1['resource_envelope']['phase_defaults']['convergence'], operation_id='a' * 32,
    )
    assert maker.call('vasp_prepare_resume', {'contract_id': cid, 'server': 'hpc', 'job_name': 'si-gates', 'phase': 'convergence'})['error_code'] == 'SUBMISSION_IN_FLIGHT'
    task_contract.release_in_flight(ws, cid, operation_id='a' * 32, refund=True, reason='test_cleanup')
    assert len(cluster.calls) == calls_before

    # Wrong explicit job_id refused after read-only preview (receipt still intact).
    bad_jid = maker.call('vasp_prepare_resume', {'contract_id': cid, 'server': 'hpc', 'job_name': 'si-gates', 'phase': 'convergence', 'job_id': '999999'})
    assert bad_jid['ok'] is False and bad_jid['error_code'] == 'RESUME_JOB_ID_MISMATCH'
    assert (folder / 'submissions/convergence.json').is_file()

    # 3) First resume uses max_resume_retries=1; second attempt auto-halts BEFORE touching remote!
    ok1 = maker.call('vasp_prepare_resume', {'contract_id': cid, 'server': 'hpc', 'job_name': 'si-gates', 'phase': 'convergence'})
    assert ok1['ok'] is True
    _record_green_review(folder, 'convergence')
    jid2 = maker.call('vasp_submit_stage', {'contract_id': cid, 'server': 'hpc', 'job_name': 'si-gates', 'phase': 'convergence'})['result']['job_id']
    cluster.sacct_states[jid2] = 'TIMEOUT'
    calls_before_cap = len(cluster.calls)
    exhausted = maker.call('vasp_prepare_resume', {'contract_id': cid, 'server': 'hpc', 'job_name': 'si-gates', 'phase': 'convergence'})
    assert exhausted['ok'] is False and exhausted['error_code'] == 'CONTRACT_BUDGET_EXHAUSTED'
    assert len(cluster.calls) == calls_before_cap
    assert task_contract.read_ledger(ws, cid)['status'] == 'halted'

    # 4) Prospective max_consecutive_failures=2 halts on 2nd failure WITHOUT committing remote resume!
    maker_cf, ccf, _ = _draft_and_approve(
        ws, monkeypatch, server='hpc', job_name='si-cf-halt',
        budget={'max_submissions': 6, 'max_resume_retries': 5, 'max_repair_retries': 0, 'max_scan_retries': 0, 'max_consecutive_failures': 2},
    )
    cidcf = ccf['contract_id']
    assert maker_cf.call('vasp_prepare_task', {'contract_id': cidcf})['ok'] is True
    f_cf = cluster.job_folder('si-cf-halt')
    _record_green_review(f_cf, 'convergence')
    j1 = maker_cf.call('vasp_submit_stage', {'contract_id': cidcf, 'server': 'hpc', 'job_name': 'si-cf-halt', 'phase': 'convergence'})['result']['job_id']
    cluster.sacct_states[j1] = 'TIMEOUT'
    assert maker_cf.call('vasp_prepare_resume', {'contract_id': cidcf, 'server': 'hpc', 'job_name': 'si-cf-halt', 'phase': 'convergence'})['ok'] is True
    assert task_contract.read_ledger(ws, cidcf)['consecutive_failures'] == 1

    _record_green_review(f_cf, 'convergence')
    j2 = maker_cf.call('vasp_submit_stage', {'contract_id': cidcf, 'server': 'hpc', 'job_name': 'si-cf-halt', 'phase': 'convergence'})['result']['job_id']
    cluster.sacct_states[j2] = 'NODE_FAIL'
    cf_halt = maker_cf.call('vasp_prepare_resume', {'contract_id': cidcf, 'server': 'hpc', 'job_name': 'si-cf-halt', 'phase': 'convergence'})
    assert cf_halt['ok'] is False and cf_halt['error_code'] == 'CONTRACT_CONSECUTIVE_FAILURES_EXCEEDED'
    assert (f_cf / 'submissions/convergence.json').is_file()
    led_cf = task_contract.read_ledger(ws, cidcf)
    assert led_cf['status'] == 'halted' and led_cf['consecutive_failures'] == 2 and led_cf['resume_retries_used'] == 1

    # 5) max_submissions already exhausted auto-halts BEFORE touching remote!
    maker_sub, csub, _ = _draft_and_approve(
        ws, monkeypatch, server='hpc', job_name='si-sub-cap',
        budget={'max_submissions': 1, 'max_resume_retries': 2, 'max_repair_retries': 0, 'max_scan_retries': 0, 'max_consecutive_failures': 3},
    )
    cidsub = csub['contract_id']
    assert maker_sub.call('vasp_prepare_task', {'contract_id': cidsub})['ok'] is True
    _record_green_review(cluster.job_folder('si-sub-cap'), 'convergence')
    jsub = maker_sub.call('vasp_submit_stage', {'contract_id': cidsub, 'server': 'hpc', 'job_name': 'si-sub-cap', 'phase': 'convergence'})['result']['job_id']
    cluster.sacct_states[jsub] = 'TIMEOUT'
    calls_before_sub = len(cluster.calls)
    sub_cap = maker_sub.call('vasp_prepare_resume', {'contract_id': cidsub, 'server': 'hpc', 'job_name': 'si-sub-cap', 'phase': 'convergence'})
    assert sub_cap['ok'] is False and sub_cap['error_code'] == 'CONTRACT_BUDGET_EXHAUSTED'
    assert len(cluster.calls) == calls_before_sub
    assert task_contract.read_ledger(ws, cidsub)['status'] == 'halted'


# ------------------- 4. Disconnect recovery, mid-call expiry, and monitor failure resilience


def test_prepare_resume_disconnect_reconciliation_and_mid_call_expiry_resilience(tmp_path, monkeypatch):
    ws = _setup_workspace(tmp_path / 'ws')
    cluster = _install_fake_remote(monkeypatch, ws, tmp_path / 'remote')

    # 1) SSH drops AFTER remote commit -> read-only status detects committed resume, deactivates monitor, settles ledger!
    maker, c1, _ = _draft_and_approve(ws, monkeypatch, server='hpc', job_name='si-disc-ok')
    cid1 = c1['contract_id']
    assert maker.call('vasp_prepare_task', {'contract_id': cid1})['ok'] is True
    f1 = cluster.job_folder('si-disc-ok')
    _record_green_review(f1, 'convergence')
    j1 = maker.call('vasp_submit_stage', {'contract_id': cid1, 'server': 'hpc', 'job_name': 'si-disc-ok', 'phase': 'convergence'})['result']['job_id']
    cluster.sacct_states[j1] = 'TIMEOUT'

    def commit_then_drop(folder: Path, kw: dict) -> dict:
        res = remote_worker.prepare_phase_resume(folder, kw.get('phase'), kw.get('confirm_job_id'))
        if kw.get('confirm_job_id') is not None:
            raise ConnectionError('SSH dropped right after commit')
        return res

    cluster.resume_hook = commit_then_drop
    rec_ok = maker.call('vasp_prepare_resume', {'contract_id': cid1, 'server': 'hpc', 'job_name': 'si-disc-ok', 'phase': 'convergence'})
    assert rec_ok['ok'] is True, rec_ok
    assert rec_ok['result']['prepared'] is True and rec_ok['result']['recovered'] is True
    assert rec_ok['result']['monitor_deactivated'] is True
    assert task_contract.read_ledger(ws, cid1)['resume_retries_used'] == 1

    # 2) SSH drops BEFORE remote commit -> receipt still intact -> recoverable RESUME_DISCONNECTED_NOT_APPLIED, zero budget spent.
    maker2, c2, _ = _draft_and_approve(ws, monkeypatch, server='hpc', job_name='si-disc-no')
    cid2 = c2['contract_id']
    assert maker2.call('vasp_prepare_task', {'contract_id': cid2})['ok'] is True
    f2 = cluster.job_folder('si-disc-no')
    _record_green_review(f2, 'convergence')
    j2 = maker2.call('vasp_submit_stage', {'contract_id': cid2, 'server': 'hpc', 'job_name': 'si-disc-no', 'phase': 'convergence'})['result']['job_id']
    cluster.sacct_states[j2] = 'PREEMPTED'

    def drop_before_commit(folder: Path, kw: dict) -> dict:
        if kw.get('confirm_job_id') is not None:
            raise ConnectionError('SSH reset before commit')
        return remote_worker.prepare_phase_resume(folder, kw.get('phase'), None)

    cluster.resume_hook = drop_before_commit
    rec_no = maker2.call('vasp_prepare_resume', {'contract_id': cid2, 'server': 'hpc', 'job_name': 'si-disc-no', 'phase': 'convergence'})
    assert rec_no['ok'] is False and rec_no['error_code'] == 'RESUME_DISCONNECTED_NOT_APPLIED' and rec_no['recoverable'] is True
    assert task_contract.read_ledger(ws, cid2)['resume_retries_used'] == 0

    # When status check also fails -> non-recoverable RESUME_RESULT_UNCERTAIN.
    cluster.status_hook = lambda _f, _kw: (_ for _ in ()).throw(ConnectionError('status down'))
    unc = maker2.call('vasp_prepare_resume', {'contract_id': cid2, 'server': 'hpc', 'job_name': 'si-disc-no', 'phase': 'convergence'})
    assert unc['ok'] is False and unc['error_code'] == 'RESUME_RESULT_UNCERTAIN' and unc['recoverable'] is False
    cluster.status_hook = None
    cluster.resume_hook = None

    # 3) Contract expires mid-resume -> completed resume & monitor deactivation still returned with ledger_warning!
    future = datetime.now(timezone.utc) + timedelta(days=30)
    real_utc_now = task_contract._ledger.utc_now
    expired_flag = [False]
    monkeypatch.setattr(task_contract._ledger, 'utc_now', lambda now=None: future if (now is None and expired_flag[0]) else real_utc_now(now))

    def commit_then_expire(folder: Path, kw: dict) -> dict:
        res = remote_worker.prepare_phase_resume(folder, kw.get('phase'), kw.get('confirm_job_id'))
        if kw.get('confirm_job_id') is not None:
            expired_flag[0] = True
        return res

    cluster.resume_hook = commit_then_expire
    exp_res = maker2.call('vasp_prepare_resume', {'contract_id': cid2, 'server': 'hpc', 'job_name': 'si-disc-no', 'phase': 'convergence'})
    assert exp_res['ok'] is True, exp_res
    assert exp_res['result']['prepared'] is True and exp_res['result']['monitor_deactivated'] is True
    assert exp_res['result']['ledger_warning']['error_code'] == 'CONTRACT_EXPIRED'
    expired_flag[0] = False
    cluster.resume_hook = None

    # 4) Monitor.deactivate error emits monitor_warning without hiding completed resume.
    maker3, c3, _ = _draft_and_approve(ws, monkeypatch, server='hpc', job_name='si-mon-warn')
    cid3 = c3['contract_id']
    assert maker3.call('vasp_prepare_task', {'contract_id': cid3})['ok'] is True
    f3 = cluster.job_folder('si-mon-warn')
    _record_green_review(f3, 'convergence')
    j3 = maker3.call('vasp_submit_stage', {'contract_id': cid3, 'server': 'hpc', 'job_name': 'si-mon-warn', 'phase': 'convergence'})['result']['job_id']
    cluster.sacct_states[j3] = 'TIMEOUT'
    monkeypatch.setattr(maker3.box.monitor, 'deactivate', lambda *_a, **_kw: (_ for _ in ()).throw(OSError('lock fail')))
    warn_res = maker3.call('vasp_prepare_resume', {'contract_id': cid3, 'server': 'hpc', 'job_name': 'si-mon-warn', 'phase': 'convergence'})
    assert warn_res['ok'] is True and warn_res['result']['prepared'] is True
    assert warn_res['result']['monitor_deactivated'] is False and 'monitor_warning' in warn_res['result']
    assert task_contract.read_ledger(ws, cid3)['resume_retries_used'] == 1

    # 5) Tampering manifest-hashed input triggers INPUT_HASH_MISMATCH auto-halt during post-resume resource config!
    maker_t, ct, _ = _draft_and_approve(ws, monkeypatch, server='hpc', job_name='si-res-tamper')
    cidt = ct['contract_id']
    assert maker_t.call('vasp_prepare_task', {'contract_id': cidt})['ok'] is True
    ft = cluster.job_folder('si-res-tamper')
    _record_green_review(ft, 'convergence')
    jt = maker_t.call('vasp_submit_stage', {'contract_id': cidt, 'server': 'hpc', 'job_name': 'si-res-tamper', 'phase': 'convergence'})['result']['job_id']
    cluster.sacct_states[jt] = 'TIMEOUT'
    (ft / 'convergence/encut-0/INCAR').write_text('ENCUT = 999\n', encoding='utf-8', newline='\n')
    bump = {'submission_mode': 'allocation', 'parallel_tests': 1, 'cores_per_test': 8, 'nodes': 1, 'partition': 'compute', 'walltime': '03:00:00'}
    tamper_res = maker_t.call('vasp_prepare_resume', {'contract_id': cidt, 'server': 'hpc', 'job_name': 'si-res-tamper', 'phase': 'convergence', 'resources': bump})
    assert tamper_res['ok'] is False and tamper_res['error_code'] == 'INPUT_HASH_MISMATCH'
    assert task_contract.read_ledger(ws, cidt)['status'] == 'halted'
    audit_rows = [json.loads(line) for line in agent_paths.audit_file(ws).read_text(encoding='utf-8').splitlines() if line.strip()]
    assert any(
        row.get('control_prepare_resume', {}).get('job_id') == jt
        and row['control_prepare_resume'].get('drift_code') == 'INPUT_HASH_MISMATCH'
        and row['control_prepare_resume'].get('archived_receipt')
        for row in audit_rows
    )

    # 6) Local environment_profile drift halts contract BEFORE remote resume preview!
    servers_dir = ws / 'servers'
    env_dir = servers_dir / 'environments'
    env_dir.mkdir(parents=True, exist_ok=True)
    (servers_dir / 'hpc.json').write_text(json.dumps({'host': 'hpc.example', 'user': 'u'}), encoding='utf-8')
    (servers_dir / 'hpc.compute.json').write_text(json.dumps({'vasp_env': '/opt/default.sh'}), encoding='utf-8')
    prof_doc = {
        'schema': 1, 'server': 'hpc',
        'profiles': [{
            'name': 'std6', 'env_script': '/opt/std6.sh', 'environment_sha256': 'a' * 64,
            'executable_sha256': 'b' * 64, 'capabilities': ['vasp6'], 'version': '6.4.3',
        }],
    }
    (env_dir / 'hpc.profiles.json').write_text(json.dumps(prof_doc), encoding='utf-8', newline='\n')
    maker_e = ControlService(ws, role='maker')
    draft_e = maker_e.call('vasp_draft_contract', {
        'server': 'hpc', 'job_name': 'si-res-drift', 'structure': 'structures/Si.POSCAR',
        'encut': 400, 'mesh': [4, 4, 4], 'nbands': 16, 'relax_mode': 'cell',
        'goal': 'Si resume env drift test', 'resource_envelope': _alloc_envelope(['convergence']),
        'allowed_phases': ['convergence'],
    })
    assert draft_e['ok'] is True, draft_e
    monkeypatch.setattr(task_contract, 'has_real_tty', lambda stream=None: True)
    answers = iter(['e', '2', '1'])
    approved_e = contract_ui.approve_contract_interactive(
        ws, draft_e['result']['draft_id'], reader=lambda _p: next(answers), output=lambda _s: None,
    )
    cide = approved_e['contract']['contract_id']
    prof_doc['profiles'][0]['executable_sha256'] = 'f' * 64
    (env_dir / 'hpc.profiles.json').write_text(json.dumps(prof_doc), encoding='utf-8', newline='\n')
    calls_before = len(cluster.calls)
    drift_res = maker_e.call('vasp_prepare_resume', {'contract_id': cide, 'server': 'hpc', 'job_name': 'si-res-drift', 'phase': 'convergence'})
    assert drift_res['ok'] is False and drift_res['error_code'] == 'ENVIRONMENT_PROFILE_DRIFT'
    assert len(cluster.calls) == calls_before
    assert task_contract.read_ledger(ws, cide)['status'] == 'halted'


# ------------------- 5. Review hardening: REMOTE_ERROR, partial-commit, verified_stage tamper, boundaries


def test_prepare_resume_explicit_remote_error_partial_commit_and_verified_stage_tamper(tmp_path, monkeypatch):
    ws = _setup_workspace(tmp_path / 'ws')
    cluster = _install_fake_remote(monkeypatch, ws, tmp_path / 'remote')

    # 1) Explicit REMOTE_ERROR on commit is NOT misclassified as RESUME_DISCONNECTED_NOT_APPLIED and does not call status!
    maker, c1, _ = _draft_and_approve(ws, monkeypatch, server='hpc', job_name='si-rem-err')
    cid1 = c1['contract_id']
    assert maker.call('vasp_prepare_task', {'contract_id': cid1})['ok'] is True
    f1 = cluster.job_folder('si-rem-err')
    _record_green_review(f1, 'convergence')
    j1 = maker.call('vasp_submit_stage', {'contract_id': cid1, 'server': 'hpc', 'job_name': 'si-rem-err', 'phase': 'convergence'})['result']['job_id']
    cluster.sacct_states[j1] = 'TIMEOUT'

    def refuse_on_commit(folder: Path, kw: dict) -> dict:
        if kw.get('confirm_job_id') is not None:
            raise core.ToolError('REMOTE_ERROR', '提交或恢复正在登记，禁止并行操作。')
        return remote_worker.prepare_phase_resume(folder, kw.get('phase'), None)

    cluster.resume_hook = refuse_on_commit
    calls_before = len(cluster.calls)
    rem_err = maker.call('vasp_prepare_resume', {'contract_id': cid1, 'server': 'hpc', 'job_name': 'si-rem-err', 'phase': 'convergence'})
    assert rem_err['ok'] is False and rem_err['error_code'] == 'REMOTE_ERROR' and rem_err['recoverable'] is False
    assert '禁止并行操作' in rem_err['error']
    assert not any(action == 'status' for action, _, _ in cluster.calls[calls_before:])
    assert task_contract.read_ledger(ws, cid1)['resume_retries_used'] == 0

    # 2) Transport disconnect leaving partial-commit state (checkpoint['resume'] OR workflow['resumed_from_job'] written while receipt remains) -> RESUME_RESULT_UNCERTAIN!
    def drop_commit(folder: Path, kw: dict) -> dict:
        if kw.get('confirm_job_id') is not None:
            raise core.ToolError('REMOTE_TIMEOUT', '远程操作 prepare_phase_resume 超时')
        return remote_worker.prepare_phase_resume(folder, kw.get('phase'), None)

    cluster.resume_hook = drop_commit
    cluster.status_hook = lambda _f, _kw: {
        'checkpoint.json': {'next': 'convergence', 'resume': {'phase': 'convergence', 'job_id': j1}},
        'workflow-state.json': {'status': 'running'},
        'stage_submissions': {'convergence': {'status': 'submitted', 'job_id': j1, 'phase': 'convergence'}},
    }
    part1 = maker.call('vasp_prepare_resume', {'contract_id': cid1, 'server': 'hpc', 'job_name': 'si-rem-err', 'phase': 'convergence'})
    assert part1['ok'] is False and part1['error_code'] == 'RESUME_RESULT_UNCERTAIN' and part1['recoverable'] is False

    cluster.status_hook = lambda _f, _kw: {
        'checkpoint.json': {'next': 'convergence'},
        'workflow-state.json': {'status': 'awaiting_review', 'resumed_from_job': j1},
        'stage_submissions': {'convergence': {'status': 'submitted', 'job_id': j1, 'phase': 'convergence'}},
    }
    part2 = maker.call('vasp_prepare_resume', {'contract_id': cid1, 'server': 'hpc', 'job_name': 'si-rem-err', 'phase': 'convergence'})
    assert part2['ok'] is False and part2['error_code'] == 'RESUME_RESULT_UNCERTAIN' and part2['recoverable'] is False
    cluster.status_hook = None
    cluster.resume_hook = None

    # 3) worker_lifecycle.verified_stage detecting tampered vasprun.xml OR tampered inputs.json auto-halts INPUT_HASH_MISMATCH on preview!
    for tamper_kind in ('vasprun', 'inputs'):
        job = f'si-vt-{tamper_kind}'
        mk, ct, _ = _draft_and_approve(ws, monkeypatch, server='hpc', job_name=job)
        cidt = ct['contract_id']
        assert mk.call('vasp_prepare_task', {'contract_id': cidt})['ok'] is True
        ft = cluster.job_folder(job)
        _record_green_review(ft, 'convergence')
        jt = mk.call('vasp_submit_stage', {'contract_id': cidt, 'server': 'hpc', 'job_name': job, 'phase': 'convergence'})['result']['job_id']
        cluster.sacct_states[jt] = 'TIMEOUT'
        _complete_convergence_substage(ft, 'convergence/encut-0')
        if tamper_kind == 'vasprun':
            (ft / 'convergence/encut-0/vasprun.xml').write_text('<modeling>TAMPERED</modeling>\n', encoding='utf-8', newline='\n')
        else:
            (ft / 'convergence/encut-0/INCAR').write_text('ENCUT = 999\n', encoding='utf-8', newline='\n')
        t_res = mk.call('vasp_prepare_resume', {'contract_id': cidt, 'server': 'hpc', 'job_name': job, 'phase': 'convergence'})
        assert t_res['ok'] is False and t_res['error_code'] == 'INPUT_HASH_MISMATCH'
        assert task_contract.read_ledger(ws, cidt)['status'] == 'halted'


def test_prepare_resume_already_counted_same_phase_no_reset_and_post_resume_resource_warnings(tmp_path, monkeypatch):
    ws = _setup_workspace(tmp_path / 'ws')
    cluster = _install_fake_remote(monkeypatch, ws, tmp_path / 'remote')
    maker, c1, _ = _draft_and_approve(ws, monkeypatch, server='hpc', job_name='si-edge')
    cid = c1['contract_id']
    assert maker.call('vasp_prepare_task', {'contract_id': cid})['ok'] is True
    folder = cluster.job_folder('si-edge')
    _record_green_review(folder, 'convergence')
    j1 = maker.call('vasp_submit_stage', {'contract_id': cid, 'server': 'hpc', 'job_name': 'si-edge', 'phase': 'convergence'})['result']['job_id']
    cluster.sacct_states[j1] = 'TIMEOUT'

    # 1) Pre-recorded phase_failed for j1 is NOT double-counted in consecutive_failures.
    task_contract.record_phase_outcome(
        ws, cid, job_name='si-edge', phase='convergence', passed=False,
        failure_info={'job_id': j1, 'scheduler_state': 'TIMEOUT'},
    )
    assert task_contract.read_ledger(ws, cid)['consecutive_failures'] == 1

    # 2) Post-resume _configure_phase_resources non-drift error emits resource_warning while keeping prepared=True.
    cluster.configure_hook = lambda _f, _kw: (_ for _ in ()).throw(RuntimeError('transient sbatch template error'))
    bump = {'submission_mode': 'allocation', 'parallel_tests': 1, 'cores_per_test': 8, 'nodes': 1, 'partition': 'compute', 'walltime': '03:00:00'}
    res1 = maker.call('vasp_prepare_resume', {'contract_id': cid, 'server': 'hpc', 'job_name': 'si-edge', 'phase': 'convergence', 'resources': bump})
    cluster.configure_hook = None
    assert res1['ok'] is True and res1['result']['prepared'] is True
    assert res1['result']['resources_applied'] is False and 'resource_warning' in res1['result']
    assert task_contract.read_ledger(ws, cid)['consecutive_failures'] == 1

    # 3) Re-submitting the SAME phase (convergence) does NOT prematurely clear consecutive_failures!
    _record_green_review(folder, 'convergence')
    j2 = maker.call('vasp_submit_stage', {'contract_id': cid, 'server': 'hpc', 'job_name': 'si-edge', 'phase': 'convergence'})['result']['job_id']
    assert task_contract.read_ledger(ws, cid)['consecutive_failures'] == 1

    # 4) Post-resume _configure_phase_resources returning already_registered=True emits resource_warning!
    cluster.sacct_states[j2] = 'NODE_FAIL'
    from vasp_agent.control import service_execution

    real_cfg = service_execution._configure_phase_resources
    monkeypatch.setattr(service_execution, '_configure_phase_resources', lambda *_a, **_kw: {'already_registered': True, 'receipt': {'status': 'submitted', 'job_id': '9999'}})
    res2 = maker.call('vasp_prepare_resume', {'contract_id': cid, 'server': 'hpc', 'job_name': 'si-edge', 'phase': 'convergence', 'resources': bump})
    monkeypatch.setattr(service_execution, '_configure_phase_resources', real_cfg)
    assert res2['ok'] is True and res2['result']['prepared'] is True
    assert res2['result']['resources_applied'] is False and '并发已登记提交回执' in res2['result']['resource_warning']

    # 5) Advancing to relaxation on a forked allowed_job records phase_passed under CURRENT job_name (not old job)!
    _complete_phase_stub(folder, 'convergence')
    _record_green_review(folder, 'relaxation')
    with task_contract._ledger.ledger_transaction(ws, cid, write=True) as led:
        led['history'].append({'event': 'phase_failed', 'time': '2026-11-27T00:00:00Z', 'job_name': 'si-old-failed', 'phase': 'convergence'})
    rel = maker.call('vasp_submit_stage', {'contract_id': cid, 'server': 'hpc', 'job_name': 'si-edge', 'phase': 'relaxation'})
    assert rel['ok'] is True, rel
    assert rel['result']['ledger']['consecutive_failures'] == 0
    passed_events = [e for e in task_contract.read_ledger(ws, cid)['history'] if e.get('event') == 'phase_passed']
    assert passed_events and passed_events[-1]['job_name'] == 'si-edge' and passed_events[-1]['phase'] == 'convergence'

