"""Offline tests for P1d contract-gated vasp_prepare_task and vasp_submit_stage with disconnect reconciliation."""
from __future__ import annotations

import contextlib
from datetime import datetime, timedelta, timezone
import hashlib
import io
import json
import os
from pathlib import Path
import subprocess
from types import SimpleNamespace

import pytest
from pymatgen.io.vasp.inputs import Potcar

from vasp_agent.chat import release_check
from vasp_agent.common import agent_paths
from vasp_agent.control import ControlService
from vasp_agent.permissions import contract_ui, task_contract
from vasp_agent.planning import parameter_plans
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


def _remote_envelope(phases: list[str] | None = None) -> dict:
    allowed = phases or ['convergence', 'relaxation', 'scf', 'bands', 'dos']
    defaults = {}
    for phase in allowed:
        if phase == 'convergence':
            defaults[phase] = {
                'submission_mode': 'array',
                'parallel_tests': 2,
                'cores_per_test': 8,
                'nodes': 1,
                'partition': 'compute',
                'walltime': '02:00:00',
            }
        elif phase == 'relaxation':
            defaults[phase] = {
                'submission_mode': 'allocation',
                'parallel_tests': 1,
                'cores_per_test': 16,
                'nodes': 1,
                'partition': 'compute',
                'walltime': '04:00:00',
            }
        else:
            defaults[phase] = {
                'submission_mode': 'allocation',
                'parallel_tests': 1,
                'cores_per_test': 16,
                'nodes': 1,
                'partition': 'compute',
                'walltime': '02:00:00',
            }
    return {
        'allowed_modes': ['allocation', 'array'],
        'allowed_partitions': ['compute'],
        'max_parallel_tests': 3,
        'max_cores_per_test': 16,
        'max_nodes': 2,
        'max_total_cores': 32,
        'max_walltime': '04:00:00',
        'phase_defaults': defaults,
    }


def _local_envelope(phases: list[str] | None = None) -> dict:
    allowed = phases or ['convergence', 'relaxation', 'scf', 'bands']
    base = {
        'submission_mode': 'allocation',
        'parallel_tests': 1,
        'cores_per_test': 8,
        'nodes': 1,
        'partition': 'compute',
        'walltime': '02:00:00',
    }
    return {
        'allowed_modes': ['allocation'],
        'allowed_partitions': ['compute'],
        'max_parallel_tests': 1,
        'max_cores_per_test': 16,
        'max_nodes': 1,
        'max_total_cores': 16,
        'max_walltime': '04:00:00',
        'phase_defaults': {p: dict(base) for p in allowed},
    }


def _record_green_review(folder: Path, phase: str) -> dict:
    if folder.parent.name == 'runs':
        ws, server = folder.parents[1], None
    else:
        ws, server = folder.parents[2] / 'ws', 'hpc'
    checker = ControlService(ws, role='checker', actor_id='chk-exec-01', host='test', model='pro')
    got = checker.call('vasp_get_stage_review', {'server': server, 'job_name': folder.name})
    assert got['ok'] is True, got
    ctx = got['result']['summary'] if got['result'].get('payload_overflow') else got['result']
    assert ctx['phase'] == phase
    assert ctx['review_lease']['lease_id'].startswith('lse-')
    checks = [
        {
            'item': item,
            'judgment': 'pending' if (phase == 'convergence' and item == 'validation_plan') else 'pass',
            'blocking': False,
            'evidence': f'verified {phase}/{item}',
            'reason': 'offline test review pass',
            'next_action': '无需',
        }
        for item in ctx['required_checks']
    ]
    rec_res = checker.call(
        'vasp_record_stage_review',
        {
            'server': server,
            'job_name': folder.name,
            'snapshot': ctx['snapshot'],
            'phase': phase,
            'checks': checks,
        },
    )
    assert rec_res['ok'] is True, rec_res
    return rec_res['result']


def _complete_phase_stub(folder: Path, phase: str) -> None:
    """Create minimal hash-valid upstream stage completion artifacts so stage_gate can advance."""
    from pymatgen.io.vasp.inputs import Kpoints

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
        stamp = {'xml_sha256': core.digest(xml)}
        if rel == 'scf':
            chg = stage_dir / 'CHGCAR'
            chg.write_text('Si CHGCAR\n', encoding='utf-8', newline='\n')
            stamp['charge_sha256'] = core.digest(chg)
            core.save(folder / 'scf-summary.json', {'efermi': 5.0, 'nelect': 8.0, 'nbands': 16, 'mesh': [4, 4, 4]})
        core.save(stage_dir / 'complete.json', stamp)
    if phase == 'convergence':
        core.save(folder / 'convergence.json', {'passed': True, 'encut': 420, 'mesh': [4, 4, 4]})
    elif phase == 'relaxation':
        (folder / 'relaxed-2.POSCAR').write_bytes(EXAMPLE_POSCAR.read_bytes())
        (folder / 'relax-final').mkdir(exist_ok=True)
        (folder / 'relax-final/CONTCAR').write_bytes(EXAMPLE_POSCAR.read_bytes())
        core.save(folder / 'relaxation.json', {'passed': True})
        core.save(folder / 'post-relax-validation.json', {'passed': True})
    stage_gate.complete(folder, phase)


class _FakeRemoteCluster:
    """In-memory fake remote cluster backed by real full_workflow, stage_gate, and remote_worker helpers."""

    def __init__(self, ws: Path, remote_root: Path):
        self.ws = ws
        self.remote_root = remote_root
        self.jobs_dir = remote_root / 'jobs'
        self.jobs_dir.mkdir(parents=True, exist_ok=True)
        self.next_job_id = 1000
        self.calls: list[tuple[str, str, dict]] = []
        self.submit_hook = None
        self.resource_context_hook = None
        self.diagnostic_hook = None
        self.configure_hook = None

    def job_folder(self, job_name: str) -> Path:
        return self.jobs_dir / job_name

    def prepare(
        self,
        job_name: str,
        structure: str,
        *,
        encut: float,
        mesh: list[int],
        nbands: int,
        relax_mode: str,
        parallel_tests: int | None = None,
        cores_per_test: int | None = None,
        scan_preset: str | None = None,
        potential_variants=None,
        band_path=None,
        optional_stages=None,
        encuts=None,
        meshes=None,
        scan_thresholds=None,
        parameter_choice=None,
        stage_parameters=None,
        solver_overrides=None,
        preview_only: bool = False,
        environment_profile=None,
    ) -> dict:
        folder = self.job_folder(job_name)
        args = SimpleNamespace(
            structure=str(self.ws / structure),
            workdir=str(folder),
            potcar=None,
            potcar_library=str(self.ws / 'potentials/PBE'),
            potential_variants=potential_variants,
            encut=encut,
            mesh=mesh,
            nbands=nbands,
            relax_mode=relax_mode,
            parallel_tests=parallel_tests or 1,
            cores_per_test=cores_per_test or 8,
            scan_preset=scan_preset,
            band_path=band_path,
            optional_stages=optional_stages,
            stage_parameters=stage_parameters,
            solver_overrides=solver_overrides,
            encuts=encuts,
            meshes=meshes,
            scan_thresholds=scan_thresholds,
            parameter_choice=parameter_choice,
            preview_only=preview_only,
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
        if preview_only:
            return res
        script = folder / 'submit.slurm'
        script.write_text(SLURM_TEMPLATE.replace('va-demo', f'va-{job_name}'), encoding='utf-8', newline='\n')
        meta = {
            'job_name': job_name,
            'script_sha256': core.digest(script),
            'release': 'local-package',
            'version': 'test',
        }
        core.save(folder / 'remote-job.json', meta)
        if environment_profile is not None:
            cfg = json.loads((folder / 'config.json').read_text(encoding='utf-8'))
            cfg['environment_profile'] = environment_profile
            core.save(folder / 'config.json', cfg)
            manifest = json.loads((folder / 'manifest.json').read_text(encoding='utf-8'))
            manifest['hashes']['config.json'] = core.digest(folder / 'config.json')
            core.save(folder / 'manifest.json', manifest)
        return {'job_name': job_name, 'prepared': True}

    def call(self, action: str, job_name: str, **kwargs) -> dict:
        self.calls.append((action, job_name, dict(kwargs)))
        folder = self.job_folder(job_name)
        if action == 'confirm_physics':
            return stage_gate.confirm_physics(
                folder, kwargs['goal'], kwargs.get('channel', 'operator_slash_command'),
                contract_id=kwargs.get('contract_id'), contract_sha256=kwargs.get('contract_sha256'),
            )
        if action == 'stage_context':
            return stage_gate.context(folder)
        if action == 'stage_review':
            return stage_gate.record(
                folder, kwargs['snapshot'], kwargs['phase'], kwargs['checks'], kwargs['reviewer'],
            )
        if action == 'resource_context':
            if self.resource_context_hook is not None:
                return self.resource_context_hook(folder, kwargs)
            return remote_worker.resource_context(folder, kwargs.get('phase'))
        if action == 'configure_resources':
            if self.configure_hook is not None:
                return self.configure_hook(folder, kwargs)
            return remote_worker.configure_resources(
                folder, kwargs.get('phase'), kwargs['resources'], kwargs['signature'],
                kwargs['limits'], kwargs.get('operation_id'),
            )
        if action == 'submission_diagnostic':
            if self.diagnostic_hook is not None:
                return self.diagnostic_hook(folder, kwargs)
            phase = kwargs.get('phase')
            rec = folder / 'submissions' / f'{phase}.json'
            return {
                'receipt': json.loads(rec.read_text(encoding='utf-8')) if rec.is_file() else None,
                'query_error': None,
                'lookup_status': 'no_match',
                'candidate_count': 0,
                'malformed_rows': 0,
                'truncated': False,
                'accounting': [],
                'diagnosis': {'evidence': {'task_files': [{'path': f'submissions/{phase}.json', 'exists': rec.is_file()}]}},
            }
        if action == 'submit_checked':
            if self.submit_hook is not None:
                return self.submit_hook(folder, kwargs)
            return self._default_submit_checked(folder, kwargs['phase'], kwargs['limits'])
        raise AssertionError(f'Unexpected remote action: {action}')

    def _default_submit_checked(self, folder: Path, phase: str, limits: dict) -> dict:
        rec = folder / 'submissions' / f'{phase}.json'
        if rec.exists():
            return {**json.loads(rec.read_text(encoding='utf-8')), 'submission_not_attempted': True, 'existing_record': True}
        try:
            core.verify(folder)
            remote_worker.check_resources((folder / 'submit.slurm').read_text(encoding='utf-8'), limits)
            stage_gate.require_review(folder, phase)
        except Exception as exc:
            return {
                'ready': False,
                'submission_not_attempted': True,
                'error_code': getattr(exc, 'error_code', 'PREFLIGHT_REFUSED'),
                'error': str(exc),
            }
        self.next_job_id += 1
        jid = str(self.next_job_id)
        data = {'status': 'submitted', 'job_id': jid, 'phase': phase}
        rec.parent.mkdir(exist_ok=True)
        core.save(rec, data)
        core.save(folder / 'remote-submission.json', data)
        return data


def _install_fake_remote(monkeypatch, ws: Path, remote_root: Path) -> _FakeRemoteCluster:
    from vasp_agent.workflow import dos_stage

    cluster = _FakeRemoteCluster(ws, remote_root)
    monkeypatch.setattr(release_check, 'ensure_current', lambda _box, _server: None)
    monkeypatch.setattr(remote_worker, 'check_previous_job', lambda _folder, _phase: None)
    monkeypatch.setattr(
        dos_stage,
        'scf_bands',
        lambda _root: {
            'band_minima_eV': [-7.0 + idx * 1.5 for idx in range(16)],
            'lowest_eigenvalue_eV': -7.0,
            'kpoints': 8,
        },
    )

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
    job_name: str = 'si-p1d-01',
    optional_stages: list[str] | None = None,
    allowed_phases: list[str] | None = None,
    allow_forks: bool = False,
    budget: dict | None = None,
    resource_envelope: dict | None = None,
    expires_in_hours: int = 168,
) -> tuple[ControlService, dict, dict]:
    maker = ControlService(ws, role='maker', actor_id='mk-p1d')
    phases = allowed_phases or (
        ['convergence', 'relaxation', 'scf', 'bands', 'dos'] if optional_stages == ['dos']
        else ['convergence', 'relaxation', 'scf', 'bands']
    )
    env = resource_envelope or (_remote_envelope(phases) if server else _local_envelope(phases))
    args = {
        'server': server,
        'job_name': job_name,
        'structure': 'structures/Si.POSCAR',
        'encut': 400,
        'mesh': [4, 4, 4],
        'nbands': 16,
        'relax_mode': 'cell',
        'goal': 'Si 3D 非磁性 PBE 端到端能带与 DOS 自治计算',
        'resource_envelope': env,
        'allowed_phases': phases,
        'allow_forks': allow_forks,
        'expires_in_hours': expires_in_hours,
        **({'optional_stages': optional_stages} if optional_stages else {}),
        **({'budget': budget} if budget else {}),
    }
    draft_res = maker.call('vasp_draft_contract', args)
    assert draft_res['ok'] is True, draft_res
    draft_id = draft_res['result']['draft_id']
    monkeypatch.setattr(task_contract, 'has_real_tty', lambda stream=None: True)
    approved = contract_ui.approve_contract_interactive(ws, draft_id, reader=lambda _p: '1', output=lambda _s: None)
    return maker, approved['contract'], approved['ledger']


# ----------------------------- 1. Happy path: full multi-stage remote workflow


def test_prepare_and_multistage_remote_submission_with_resource_transition_and_monitor(tmp_path, monkeypatch):
    ws = _setup_workspace(tmp_path / 'ws')
    cluster = _install_fake_remote(monkeypatch, ws, tmp_path / 'remote')
    maker, contract, _ = _draft_and_approve(ws, monkeypatch, server='hpc', job_name='si-flow', optional_stages=['dos'])
    checker = ControlService(ws, role='checker', actor_id='chk-p1d')
    cid = contract['contract_id']

    # submission_enabled reflects role + active contract; ambient Toolbox remains zero-submit.
    assert maker.call('vasp_workspace_info', {})['result']['submission_enabled'] is True
    assert checker.call('vasp_workspace_info', {})['result']['submission_enabled'] is False
    assert maker.box.allow_submit is False and maker.box.max_submissions == 0

    # Checker cannot call vasp_prepare_task or vasp_submit_stage.
    assert checker.call('vasp_prepare_task', {'contract_id': cid})['error_code'] == 'TOOL_NOT_ALLOWED_FOR_ROLE'
    assert checker.call(
        'vasp_submit_stage', {'contract_id': cid, 'server': 'hpc', 'job_name': 'si-flow', 'phase': 'convergence'},
    )['error_code'] == 'TOOL_NOT_ALLOWED_FOR_ROLE'

    # 1) vasp_prepare_task prepares directory, confirms physics via operator_task_contract, configures array convergence.
    prep = maker.call('vasp_prepare_task', {'contract_id': cid})
    assert prep['ok'] is True, prep
    p_res = prep['result']
    assert p_res['prepared'] is True and p_res['submitted'] is False and p_res['already_prepared'] is False
    assert p_res['physics_confirmed'] is True
    assert p_res['physics']['confirmed_by'] == 'operator_task_contract'
    assert p_res['physics']['contract_id'] == cid
    assert p_res['physics']['contract_sha256'] == contract['approval']['canonical_sha256']
    assert p_res['resources_applied'] is True
    assert p_res['resources']['submission_mode'] == 'array'
    assert p_res['ledger']['submissions_used'] == 0

    folder = cluster.job_folder('si-flow')
    phys_disk = json.loads((folder / 'physics-model.json').read_text(encoding='utf-8'))
    assert phys_disk['confirmed_by'] == 'operator_task_contract'
    assert phys_disk['contract_id'] == cid
    assert phys_disk['contract_sha256'] == contract['approval']['canonical_sha256']
    assert '#SBATCH --array=' in (folder / 'submit.slurm').read_text(encoding='utf-8')

    # Idempotent re-call before submission passes reconstructed plan_sha256 check.
    prep2 = maker.call('vasp_prepare_task', {'contract_id': cid})
    assert prep2['ok'] is True, prep2
    assert prep2['result']['already_prepared'] is True

    # 2) Submitting BEFORE stage review fails local attestation gate BEFORE touching submit_checked or budget.
    no_rev = maker.call(
        'vasp_submit_stage', {'contract_id': cid, 'server': 'hpc', 'job_name': 'si-flow', 'phase': 'convergence'},
    )
    assert no_rev['ok'] is False
    assert no_rev['error_code'] == 'REVIEW_ATTESTATION_MISSING'
    ledger = task_contract.read_ledger(ws, cid)
    assert ledger['submissions_used'] == 0 and ledger['in_flight'] is None and ledger['status'] == 'active'

    # Even if local attestation is bypassed via hook, remote stage_gate.require_review() still rejects & refunds!
    maker._review_attestation_verifier = lambda *_a, **_kw: {'enforced': True, 'mode': 'maker_checker'}
    no_remote_rev = maker.call(
        'vasp_submit_stage', {'contract_id': cid, 'server': 'hpc', 'job_name': 'si-flow', 'phase': 'convergence'},
    )
    maker._review_attestation_verifier = None
    assert no_remote_rev['ok'] is False and no_remote_rev['error_code'] == 'PREFLIGHT_REFUSED'
    ledger = task_contract.read_ledger(ws, cid)
    assert ledger['submissions_used'] == 0 and ledger['in_flight'] is None and ledger['status'] == 'active'

    # 3) Record review & submit convergence -> succeeds, increments ledger, registers JobMonitor.
    _record_green_review(folder, 'convergence')
    sub_conv = maker.call(
        'vasp_submit_stage', {'contract_id': cid, 'server': 'hpc', 'job_name': 'si-flow', 'phase': 'convergence'},
    )
    assert sub_conv['ok'] is True, sub_conv
    assert sub_conv['result']['status'] == 'submitted'
    assert sub_conv['result']['job_id'] == '1001'
    assert sub_conv['result']['monitoring']['registered'] is True
    assert sub_conv['result']['review_attestation']['enforced'] is True
    assert sub_conv['result']['review_attestation']['stage_gate_remote_enforced'] is True
    assert sub_conv['result']['ledger']['submissions_used'] == 1
    assert maker.box.allow_submit is False

    # Re-submitting already submitted convergence returns already_submitted without consuming budget.
    dup_conv = maker.call(
        'vasp_submit_stage', {'contract_id': cid, 'server': 'hpc', 'job_name': 'si-flow', 'phase': 'convergence'},
    )
    assert dup_conv['ok'] is True
    assert dup_conv['result']['status'] == 'already_submitted'
    assert dup_conv['result']['submitted'] is False
    assert task_contract.read_ledger(ws, cid)['submissions_used'] == 1

    # 4) Complete convergence -> advance to relaxation: array -> allocation + walltime change requires re-review first!
    _complete_phase_stub(folder, 'convergence')
    rel_cfg = maker.call(
        'vasp_submit_stage', {'contract_id': cid, 'server': 'hpc', 'job_name': 'si-flow', 'phase': 'relaxation'},
    )
    assert rel_cfg['ok'] is True, rel_cfg
    assert rel_cfg['result']['status'] == 'awaiting_resource_review'
    assert rel_cfg['result']['submitted'] is False
    assert rel_cfg['result']['resources_changed'] is True
    assert task_contract.read_ledger(ws, cid)['submissions_used'] == 1
    assert '#SBATCH --array=' not in (folder / 'submit.slurm').read_text(encoding='utf-8')

    # Record relaxation review against post-resource snapshot -> submit succeeds!
    _record_green_review(folder, 'relaxation')
    sub_rel = maker.call(
        'vasp_submit_stage', {'contract_id': cid, 'server': 'hpc', 'job_name': 'si-flow', 'phase': 'relaxation'},
    )
    assert sub_rel['ok'] is True, sub_rel
    assert sub_rel['result']['status'] == 'submitted' and sub_rel['result']['job_id'] == '1002'
    assert sub_rel['result']['ledger']['submissions_used'] == 2

    # 5) Advance scf -> bands -> dos; scf walltime changes (4h -> 2h) then bands & dos submit directly.
    _complete_phase_stub(folder, 'relaxation')
    scf_cfg = maker.call('vasp_submit_stage', {'contract_id': cid, 'server': 'hpc', 'job_name': 'si-flow', 'phase': 'scf'})
    assert scf_cfg['ok'] is True and scf_cfg['result']['status'] == 'awaiting_resource_review'
    _record_green_review(folder, 'scf')
    sub_scf = maker.call('vasp_submit_stage', {'contract_id': cid, 'server': 'hpc', 'job_name': 'si-flow', 'phase': 'scf'})
    assert sub_scf['ok'] is True and sub_scf['result']['job_id'] == '1003'

    _complete_phase_stub(folder, 'scf')
    _record_green_review(folder, 'bands')
    sub_bands = maker.call('vasp_submit_stage', {'contract_id': cid, 'server': 'hpc', 'job_name': 'si-flow', 'phase': 'bands'})
    assert sub_bands['ok'] is True and sub_bands['result']['job_id'] == '1004'

    _complete_phase_stub(folder, 'bands')
    _record_green_review(folder, 'dos')
    sub_dos = maker.call('vasp_submit_stage', {'contract_id': cid, 'server': 'hpc', 'job_name': 'si-flow', 'phase': 'dos'})
    assert sub_dos['ok'] is True and sub_dos['result']['job_id'] == '1005'
    assert task_contract.read_ledger(ws, cid)['submissions_used'] == 5
    mon_jobs = {(t['phase'], t['job_id']) for t in maker.box.monitor.status()['tasks']}
    assert mon_jobs == {('convergence', '1001'), ('relaxation', '1002'), ('scf', '1003'), ('bands', '1004'), ('dos', '1005')}


# ------------------------------ 2. Local contract preparation and submission


def test_local_contract_prepare_and_submit_with_and_without_submit_script(tmp_path, monkeypatch):
    ws = _setup_workspace(tmp_path / 'ws')
    maker, contract, _ = _draft_and_approve(ws, monkeypatch, server=None, job_name='si-local-01')
    cid = contract['contract_id']

    # Without submit_script: prepare succeeds with resources_applied=False, submit refuses cleanly without consuming budget.
    prep = maker.call('vasp_prepare_task', {'contract_id': cid})
    assert prep['ok'] is True, prep
    assert prep['result']['resources_applied'] is False and prep['result']['physics_confirmed'] is True

    no_script = maker.call('vasp_submit_stage', {'contract_id': cid, 'server': None, 'job_name': 'si-local-01', 'phase': 'convergence'})
    assert no_script['ok'] is False and no_script['error_code'] == 'LOCAL_SUBMIT_SCRIPT_MISSING'
    assert task_contract.read_ledger(ws, cid)['submissions_used'] == 0

    # Configure submit_script on box -> configure_resources transitions -> review -> mocked sbatch succeeds.
    script_file = ws / 'submit-template.slurm'
    script_file.write_text(SLURM_TEMPLATE, encoding='utf-8', newline='\n')
    maker.box.submit_script = script_file.resolve()
    maker.box.script_hash = core.digest(maker.box.submit_script)

    cfg = maker.call('vasp_submit_stage', {'contract_id': cid, 'server': None, 'job_name': 'si-local-01', 'phase': 'convergence'})
    assert cfg['ok'] is True and cfg['result']['status'] == 'awaiting_resource_review'
    _record_green_review(ws / 'runs/si-local-01', 'convergence')

    monkeypatch.setattr(
        subprocess, 'run',
        lambda cmd, **_kw: SimpleNamespace(returncode=0, stdout='54321;cluster\n', stderr=''),
    )
    sub = maker.call('vasp_submit_stage', {'contract_id': cid, 'server': 'local', 'job_name': 'si-local-01', 'phase': 'convergence'})
    assert sub['ok'] is True, sub
    assert sub['result']['job_id'] == '54321' and sub['result']['submitted'] is True
    assert task_contract.read_ledger(ws, cid)['submissions_used'] == 1
    assert maker.box.monitor.status()['tasks'][0]['job_id'] == '54321'


# ----------- 3. Unsigned, tampered, symlink, drift, scope, envelope & budget


def test_unsigned_tampered_plan_structure_symlink_and_env_drift_are_blocked(tmp_path, monkeypatch):
    ws = _setup_workspace(tmp_path / 'ws')
    _install_fake_remote(monkeypatch, ws, tmp_path / 'remote')
    maker, contract, _ = _draft_and_approve(ws, monkeypatch, server='hpc', job_name='si-sec-01')
    cid = contract['contract_id']

    # 1) Non-existent / unapproved draft ID refused.
    assert maker.call('vasp_prepare_task', {'contract_id': 'drf-20261127-deadbeef'})['error_code'] == 'CONTRACT_NOT_FOUND'

    # 2) Spoofed plan (setting status='prepared' and changing selected encuts) is caught by reconstructed sha256!
    plan_file = parameter_plans.path(maker.box, contract['workflow']['plan_id'])
    orig_plan = plan_file.read_bytes()
    spoofed = json.loads(orig_plan.decode('utf-8'))
    spoofed['status'] = 'prepared'
    spoofed['result'] = {'fake': True}
    spoofed['selected']['encuts'] = [380, 440, 500]
    plan_file.write_text(json.dumps(spoofed, ensure_ascii=False, indent=2) + '\n', encoding='utf-8', newline='\n')
    res_spoof = maker.call('vasp_prepare_task', {'contract_id': cid})
    assert res_spoof['ok'] is False and res_spoof['error_code'] == 'CONTRACT_TAMPERED'
    plan_file.write_bytes(orig_plan)

    # 3) Symlinked structure file (mocked is_symlink + resolve to simulate Linux dereference) refused.
    struct_path = ws / 'structures/Si.POSCAR'
    outside = tmp_path / 'outside.POSCAR'
    outside.write_bytes(EXAMPLE_POSCAR.read_bytes())
    real_is_symlink, real_resolve = Path.is_symlink, Path.resolve
    monkeypatch.setattr(Path, 'is_symlink', lambda p: True if p == struct_path else real_is_symlink(p))
    monkeypatch.setattr(Path, 'resolve', lambda p, strict=False: outside if p == struct_path else real_resolve(p, strict=strict))
    sym_res = maker.call('vasp_prepare_task', {'contract_id': cid})
    assert sym_res['ok'] is False and sym_res['error_code'] == 'CONTRACT_SYMLINK_REFUSED'
    monkeypatch.setattr(Path, 'is_symlink', real_is_symlink)
    monkeypatch.setattr(Path, 'resolve', real_resolve)

    # 4) Modifying structure bytes after approval triggers INPUT_HASH_MISMATCH AND auto-halts contract!
    orig_poscar = struct_path.read_bytes()
    struct_path.write_bytes(orig_poscar + b'\n')
    drift_res = maker.call('vasp_prepare_task', {'contract_id': cid})
    assert drift_res['ok'] is False and drift_res['error_code'] == 'INPUT_HASH_MISMATCH'
    assert task_contract.read_ledger(ws, cid)['status'] == 'halted'
    struct_path.write_bytes(orig_poscar)

    # 5) Tampering contract JSON directly fails CONTRACT_TAMPERED.
    active_file = agent_paths.store_dir(ws, 'contracts') / 'active' / f'{cid}.json'
    bad_contract = json.loads(active_file.read_text(encoding='utf-8'))
    bad_contract['budget']['max_submissions'] = 99
    active_file.write_text(json.dumps(bad_contract, ensure_ascii=False, indent=2) + '\n', encoding='utf-8', newline='\n')
    assert maker.call('vasp_prepare_task', {'contract_id': cid})['error_code'] == 'CONTRACT_TAMPERED'


def test_real_symlink_structure_refused_when_os_permits(tmp_path, monkeypatch):
    ws = _setup_workspace(tmp_path / 'ws')
    _install_fake_remote(monkeypatch, ws, tmp_path / 'remote')
    maker, contract, _ = _draft_and_approve(ws, monkeypatch, server='hpc', job_name='si-realsym')
    struct_path = ws / 'structures/Si.POSCAR'
    target = ws / 'structures/Si-real.POSCAR'
    target.write_bytes(struct_path.read_bytes())
    struct_path.unlink()
    try:
        os.symlink(target, struct_path)
    except OSError:
        pytest.skip('OS does not permit creating symlinks without elevated privileges')
    res = maker.call('vasp_prepare_task', {'contract_id': contract['contract_id']})
    assert res['ok'] is False and res['error_code'] == 'CONTRACT_SYMLINK_REFUSED'


def test_scope_envelope_budget_expiry_drift_and_p2_attestation_hook(tmp_path, monkeypatch):
    ws = _setup_workspace(tmp_path / 'ws')
    cluster = _install_fake_remote(monkeypatch, ws, tmp_path / 'remote')
    maker, contract, _ = _draft_and_approve(
        ws, monkeypatch, server='hpc', job_name='si-bounds',
        allowed_phases=['convergence', 'relaxation'],
        budget={'max_submissions': 1, 'max_resume_retries': 0, 'max_repair_retries': 0, 'max_scan_retries': 0, 'max_consecutive_failures': 2},
    )
    cid = contract['contract_id']
    assert maker.call('vasp_prepare_task', {'contract_id': cid})['ok'] is True
    folder = cluster.job_folder('si-bounds')
    _record_green_review(folder, 'convergence')
    calls_before = len(cluster.calls)

    # Scope violations make zero remote calls.
    assert maker.call('vasp_submit_stage', {'contract_id': cid, 'server': 'other', 'job_name': 'si-bounds', 'phase': 'convergence'})['error_code'] == 'CONTRACT_SCOPE_VIOLATION'
    assert maker.call('vasp_submit_stage', {'contract_id': cid, 'server': 'hpc', 'job_name': 'other-job', 'phase': 'convergence'})['error_code'] == 'CONTRACT_SCOPE_VIOLATION'
    assert maker.call('vasp_submit_stage', {'contract_id': cid, 'server': 'hpc', 'job_name': 'si-bounds', 'phase': 'scf'})['error_code'] == 'CONTRACT_SCOPE_VIOLATION'

    # Out-of-envelope resources make zero remote calls.
    over_cores = {'submission_mode': 'allocation', 'parallel_tests': 1, 'cores_per_test': 32, 'nodes': 1, 'partition': 'compute', 'walltime': '01:00:00'}
    assert maker.call('vasp_submit_stage', {'contract_id': cid, 'server': 'hpc', 'job_name': 'si-bounds', 'phase': 'convergence', 'resources': over_cores})['error_code'] == 'CONTRACT_RESOURCE_ENVELOPE_EXCEEDED'
    bad_part = {**over_cores, 'cores_per_test': 8, 'partition': 'gpu'}
    assert maker.call('vasp_submit_stage', {'contract_id': cid, 'server': 'hpc', 'job_name': 'si-bounds', 'phase': 'convergence', 'resources': bad_part})['error_code'] == 'CONTRACT_RESOURCE_ENVELOPE_EXCEEDED'
    assert len(cluster.calls) == calls_before

    # Pluggable P2 review attestation hook blocks BEFORE budget reservation or submit_checked when resources match.
    maker._review_attestation_verifier = lambda *_a, **_kw: (_ for _ in ()).throw(
        task_contract.ContractError('REVIEW_ATTESTATION_MISSING', '缺少独立 Checker 本地背书。')
    )
    hook_res = maker.call('vasp_submit_stage', {'contract_id': cid, 'server': 'hpc', 'job_name': 'si-bounds', 'phase': 'convergence'})
    assert hook_res['ok'] is False and hook_res['error_code'] == 'REVIEW_ATTESTATION_MISSING'
    assert not any(action == 'submit_checked' for action, _, _ in cluster.calls[calls_before:])
    assert task_contract.read_ledger(ws, cid)['submissions_used'] == 0
    maker._review_attestation_verifier = None

    # First valid submission consumes the 1 allowed credit.
    first = maker.call('vasp_submit_stage', {'contract_id': cid, 'server': 'hpc', 'job_name': 'si-bounds', 'phase': 'convergence'})
    assert first['ok'] is True and first['result']['job_id'] == '1001'

    # Second submission (relaxation) exceeds max_submissions=1 -> auto-halts BEFORE touching remote!
    _complete_phase_stub(folder, 'convergence')
    calls_before_budget = len(cluster.calls)
    over_budget = maker.call('vasp_submit_stage', {'contract_id': cid, 'server': 'hpc', 'job_name': 'si-bounds', 'phase': 'relaxation'})
    assert over_budget['ok'] is False and over_budget['error_code'] == 'CONTRACT_BUDGET_EXHAUSTED'
    assert len(cluster.calls) == calls_before_budget
    assert task_contract.read_ledger(ws, cid)['status'] == 'halted'

    # Preflight remote drift error (VASP环境已改变) auto-halts contract and releases in_flight.
    maker2, c2, _ = _draft_and_approve(ws, monkeypatch, server='hpc', job_name='si-drift')
    cid2 = c2['contract_id']
    assert maker2.call('vasp_prepare_task', {'contract_id': cid2})['ok'] is True
    _record_green_review(cluster.job_folder('si-drift'), 'convergence')
    cluster.submit_hook = lambda _f, _kw: {
        'ready': False, 'submission_not_attempted': True,
        'error_code': 'PREFLIGHT_REFUSED', 'error': 'VASP环境已改变，请新建任务重新验证，不复用旧环境证据。',
    }
    drift_sub = maker2.call('vasp_submit_stage', {'contract_id': cid2, 'server': 'hpc', 'job_name': 'si-drift', 'phase': 'convergence'})
    assert drift_sub['ok'] is False and drift_sub['error_code'] == 'ENVIRONMENT_PROFILE_DRIFT'
    led2 = task_contract.read_ledger(ws, cid2)
    assert led2['status'] == 'halted' and led2['in_flight'] is None and led2['submissions_used'] == 0

    # Expired contract is rejected before any action.
    maker3, c3, _ = _draft_and_approve(ws, monkeypatch, server='hpc', job_name='si-exp')
    future = datetime.now(timezone.utc) + timedelta(days=30)
    monkeypatch.setattr(task_contract._ledger, 'utc_now', lambda now=None: future if now is None else now)
    exp_res = maker3.call('vasp_prepare_task', {'contract_id': c3['contract_id']})
    assert exp_res['ok'] is False and exp_res['error_code'] == 'CONTRACT_EXPIRED'


# --------------- 4. SSH disconnect reconciliation & halted_uncertain hard halt


def test_disconnect_auto_recovers_confirmed_remote_job_id_and_registers_monitor(tmp_path, monkeypatch):
    ws = _setup_workspace(tmp_path / 'ws')
    cluster = _install_fake_remote(monkeypatch, ws, tmp_path / 'remote')
    maker, contract, _ = _draft_and_approve(ws, monkeypatch, server='hpc', job_name='si-rec-ok')
    cid = contract['contract_id']
    assert maker.call('vasp_prepare_task', {'contract_id': cid})['ok'] is True
    folder = cluster.job_folder('si-rec-ok')
    _record_green_review(folder, 'convergence')

    def submit_then_drop(f: Path, kw: dict) -> dict:
        rec = f / 'submissions' / f'{kw["phase"]}.json'
        rec.parent.mkdir(exist_ok=True)
        core.save(rec, {'status': 'submitted', 'job_id': '887766', 'phase': kw['phase']})
        raise subprocess.TimeoutExpired(cmd=['ssh'], timeout=30)

    cluster.submit_hook = submit_then_drop
    res = maker.call('vasp_submit_stage', {'contract_id': cid, 'server': 'hpc', 'job_name': 'si-rec-ok', 'phase': 'convergence'})
    assert res['ok'] is True, res
    body = res['result']
    assert body['status'] == 'submitted' and body['recovered'] is True and body['job_id'] == '887766'
    assert body['monitoring']['registered'] is True
    ledger = task_contract.read_ledger(ws, cid)
    assert ledger['status'] == 'active' and ledger['in_flight'] is None and ledger['submissions_used'] == 1
    assert any(t['job_id'] == '887766' for t in maker.box.monitor.status()['tasks'])


def test_disconnect_no_receipt_and_clean_diagnostic_releases_in_flight_for_safe_retry(tmp_path, monkeypatch):
    ws = _setup_workspace(tmp_path / 'ws')
    cluster = _install_fake_remote(monkeypatch, ws, tmp_path / 'remote')
    maker, contract, _ = _draft_and_approve(
        ws, monkeypatch, server='hpc', job_name='si-rec-retry',
        budget={'max_submissions': 3, 'max_resume_retries': 0, 'max_repair_retries': 0, 'max_scan_retries': 0, 'max_consecutive_failures': 2},
    )
    cid = contract['contract_id']
    assert maker.call('vasp_prepare_task', {'contract_id': cid})['ok'] is True
    _record_green_review(cluster.job_folder('si-rec-retry'), 'convergence')

    # Drop SSH before writing any remote receipt -> diagnostic proves no_match -> recoverable safe retry.
    cluster.submit_hook = lambda _f, _kw: (_ for _ in ()).throw(ConnectionError('SSH reset by peer'))
    dropped = maker.call('vasp_submit_stage', {'contract_id': cid, 'server': 'hpc', 'job_name': 'si-rec-retry', 'phase': 'convergence'})
    assert dropped['ok'] is False
    assert dropped['error_code'] == 'SUBMISSION_DISCONNECTED_NO_RECEIPT'
    assert dropped['recoverable'] is True
    assert dropped['next_action']['action'] == 'retry_vasp_submit_stage'
    assert dropped['next_action']['remaining_submissions'] == 2
    ledger = task_contract.read_ledger(ws, cid)
    assert ledger['status'] == 'active' and ledger['in_flight'] is None and ledger['submissions_used'] == 1

    # Safe retry now succeeds cleanly!
    cluster.submit_hook = None
    retried = maker.call('vasp_submit_stage', {'contract_id': cid, 'server': 'hpc', 'job_name': 'si-rec-retry', 'phase': 'convergence'})
    assert retried['ok'] is True and retried['result']['job_id'] == '1001'
    assert task_contract.read_ledger(ws, cid)['submissions_used'] == 2

    # When max_submissions=1 and the sole attempt disconnects cleanly with no receipt, auto-halt CONTRACT_BUDGET_EXHAUSTED.
    maker_one, c_one, _ = _draft_and_approve(
        ws, monkeypatch, server='hpc', job_name='si-rec-zero',
        budget={'max_submissions': 1, 'max_resume_retries': 0, 'max_repair_retries': 0, 'max_scan_retries': 0, 'max_consecutive_failures': 2},
    )
    cid_one = c_one['contract_id']
    assert maker_one.call('vasp_prepare_task', {'contract_id': cid_one})['ok'] is True
    _record_green_review(cluster.job_folder('si-rec-zero'), 'convergence')
    cluster.submit_hook = lambda _f, _kw: (_ for _ in ()).throw(ConnectionError('SSH dropped on last credit'))
    last_drop = maker_one.call('vasp_submit_stage', {'contract_id': cid_one, 'server': 'hpc', 'job_name': 'si-rec-zero', 'phase': 'convergence'})
    assert last_drop['ok'] is False and last_drop['error_code'] == 'CONTRACT_BUDGET_EXHAUSTED'
    assert task_contract.read_ledger(ws, cid_one)['status'] == 'halted'


@pytest.mark.parametrize('failure_mode', ['uncertain_receipt', 'ambiguous_diag', 'diag_query_error', 'reconcile_disconnect'])
def test_disconnect_ambiguity_or_uncertain_receipt_hard_halts_contract(tmp_path, monkeypatch, failure_mode):
    ws = _setup_workspace(tmp_path / 'ws')
    cluster = _install_fake_remote(monkeypatch, ws, tmp_path / 'remote')
    job_name = f'si-halt-{failure_mode[:8]}'
    maker, contract, _ = _draft_and_approve(ws, monkeypatch, server='hpc', job_name=job_name)
    cid = contract['contract_id']
    assert maker.call('vasp_prepare_task', {'contract_id': cid})['ok'] is True
    _record_green_review(cluster.job_folder(job_name), 'convergence')

    if failure_mode == 'uncertain_receipt':
        def write_uncertain(f: Path, kw: dict):
            rec = f / 'submissions' / f'{kw["phase"]}.json'
            rec.parent.mkdir(exist_ok=True)
            core.save(rec, {'status': 'submission_uncertain', 'job_id': None, 'phase': kw['phase']})
            raise ConnectionError('lost after sbatch reservation')
        cluster.submit_hook = write_uncertain
    else:
        cluster.submit_hook = lambda _f, _kw: (_ for _ in ()).throw(ConnectionError('SSH dropped'))

    if failure_mode == 'ambiguous_diag':
        cluster.diagnostic_hook = lambda _f, _kw: {
            'receipt': None, 'query_error': None, 'lookup_status': 'ambiguous',
            'candidate_count': 2, 'malformed_rows': 0, 'truncated': False,
            'accounting': [{'job_id': '1'}, {'job_id': '2'}],
        }
    elif failure_mode == 'diag_query_error':
        cluster.diagnostic_hook = lambda _f, _kw: {
            'receipt': None, 'query_error': 'sacct timeout', 'lookup_status': 'no_match',
            'candidate_count': 0, 'malformed_rows': 0, 'truncated': False, 'accounting': None,
        }
    elif failure_mode == 'reconcile_disconnect':
        first_ctx = [True]

        def flaky_ctx(f: Path, kw: dict):
            if first_ctx[0]:
                first_ctx[0] = False
                return remote_worker.resource_context(f, kw.get('phase'))
            raise ConnectionError('SSH still down during reconcile')

        cluster.resource_context_hook = flaky_ctx

    res = maker.call('vasp_submit_stage', {'contract_id': cid, 'server': 'hpc', 'job_name': job_name, 'phase': 'convergence'})
    assert res['ok'] is False
    assert res['error_code'] == 'SUBMISSION_UNCERTAIN'
    assert res['recoverable'] is False
    ledger = task_contract.read_ledger(ws, cid)
    assert ledger['status'] == 'halted_uncertain'
    assert ledger['halt_code'] == 'SUBMISSION_UNCERTAIN'

    # Subsequent submission is strictly blocked by halted_uncertain (never double-submits).
    cluster.submit_hook = None
    cluster.resource_context_hook = None
    blocked = maker.call('vasp_submit_stage', {'contract_id': cid, 'server': 'hpc', 'job_name': job_name, 'phase': 'convergence'})
    assert blocked['ok'] is False and blocked['error_code'] == 'CONTRACT_HALTED'


def test_in_flight_guard_local_sbatch_uncertain_and_env_profile_drift_halt(tmp_path, monkeypatch):
    ws = _setup_workspace(tmp_path / 'ws')
    cluster = _install_fake_remote(monkeypatch, ws, tmp_path / 'remote')

    # 1) Pre-existing in_flight on ledger blocks both prepare and submit immediately.
    maker, c1, _ = _draft_and_approve(ws, monkeypatch, server='hpc', job_name='si-inflight')
    cid1 = c1['contract_id']
    task_contract.reserve_submission(
        ws, cid1, server='hpc', job_name='si-inflight', phase='convergence',
        resources=c1['resource_envelope']['phase_defaults']['convergence'],
    )
    assert maker.call('vasp_prepare_task', {'contract_id': cid1})['error_code'] == 'SUBMISSION_IN_FLIGHT'
    assert maker.call(
        'vasp_submit_stage', {'contract_id': cid1, 'server': 'hpc', 'job_name': 'si-inflight', 'phase': 'convergence'},
    )['error_code'] == 'SUBMISSION_IN_FLIGHT'

    # 2) Local sbatch failure writes submission_uncertain and hard-halts ledger to halted_uncertain.
    maker_l, cl, _ = _draft_and_approve(ws, monkeypatch, server=None, job_name='si-loc-fail')
    cidl = cl['contract_id']
    script_file = ws / 'local-submit.slurm'
    script_file.write_text(SLURM_TEMPLATE, encoding='utf-8', newline='\n')
    maker_l.box.submit_script = script_file.resolve()
    maker_l.box.script_hash = core.digest(maker_l.box.submit_script)
    assert maker_l.call('vasp_prepare_task', {'contract_id': cidl})['ok'] is True
    _record_green_review(ws / 'runs/si-loc-fail', 'convergence')
    monkeypatch.setattr(
        subprocess, 'run',
        lambda *_a, **_kw: (_ for _ in ()).throw(subprocess.TimeoutExpired(cmd=['sbatch'], timeout=30)),
    )
    fail_loc = maker_l.call('vasp_submit_stage', {'contract_id': cidl, 'server': None, 'job_name': 'si-loc-fail', 'phase': 'convergence'})
    assert fail_loc['ok'] is False and fail_loc['error_code'] == 'SUBMISSION_UNCERTAIN'
    assert task_contract.read_ledger(ws, cidl)['status'] == 'halted_uncertain'

    # 3) Local environment_profile hash drift halts contract before prepare/submit.
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
        'server': 'hpc', 'job_name': 'si-env-drift', 'structure': 'structures/Si.POSCAR',
        'encut': 400, 'mesh': [4, 4, 4], 'nbands': 16, 'relax_mode': 'cell',
        'goal': 'Si env drift test', 'resource_envelope': _remote_envelope(['convergence']),
        'allowed_phases': ['convergence'],
    })
    assert draft_e['ok'] is True, draft_e
    monkeypatch.setattr(task_contract, 'has_real_tty', lambda stream=None: True)
    answers = iter(['e', '2', '1'])
    approved_e = contract_ui.approve_contract_interactive(ws, draft_e['result']['draft_id'], reader=lambda _p: next(answers), output=lambda _s: None)
    cide = approved_e['contract']['contract_id']
    assert approved_e['contract']['scope']['environment_profile']['name'] == 'std6'

    # Mutate registered profile hash on disk -> prepare must detect ENVIRONMENT_PROFILE_DRIFT and auto-halt!
    prof_doc['profiles'][0]['executable_sha256'] = 'f' * 64
    (env_dir / 'hpc.profiles.json').write_text(json.dumps(prof_doc), encoding='utf-8', newline='\n')
    drift_prep = maker_e.call('vasp_prepare_task', {'contract_id': cide})
    assert drift_prep['ok'] is False and drift_prep['error_code'] == 'ENVIRONMENT_PROFILE_DRIFT'
    assert task_contract.read_ledger(ws, cide)['status'] == 'halted'
    assert not cluster.job_folder('si-env-drift').exists()


# ---------- 5. Review hardening: post-submit expiry, P2 ordering, fail-closed


def test_confirmed_job_id_preserved_and_monitored_when_contract_expires_mid_submit(tmp_path, monkeypatch):
    ws = _setup_workspace(tmp_path / 'ws')
    cluster = _install_fake_remote(monkeypatch, ws, tmp_path / 'remote')
    maker, contract, _ = _draft_and_approve(ws, monkeypatch, server='hpc', job_name='si-mid-exp')
    cid = contract['contract_id']
    assert maker.call('vasp_prepare_task', {'contract_id': cid})['ok'] is True
    folder = cluster.job_folder('si-mid-exp')
    _record_green_review(folder, 'convergence')

    future = datetime.now(timezone.utc) + timedelta(days=30)
    real_utc_now = task_contract._ledger.utc_now
    expired_flag = [False]
    monkeypatch.setattr(task_contract._ledger, 'utc_now', lambda now=None: future if (now is None and expired_flag[0]) else real_utc_now(now))

    def submit_then_expire(f: Path, kw: dict) -> dict:
        res = cluster._default_submit_checked(f, kw['phase'], kw['limits'])
        expired_flag[0] = True
        return res

    cluster.submit_hook = submit_then_expire
    sub = maker.call('vasp_submit_stage', {'contract_id': cid, 'server': 'hpc', 'job_name': 'si-mid-exp', 'phase': 'convergence'})
    assert sub['ok'] is True, sub
    body = sub['result']
    assert body['status'] == 'submitted' and body['submitted'] is True and body['job_id'] == '1001'
    assert body['monitoring']['registered'] is True
    assert body['ledger_warning']['error_code'] == 'CONTRACT_EXPIRED'
    assert body['ledger']['status'] == 'expired'
    assert any(t['job_id'] == '1001' for t in maker.box.monitor.status()['tasks'])
    audit_lines = [json.loads(line) for line in agent_paths.audit_file(ws).read_text(encoding='utf-8').splitlines() if line.strip()]
    assert any(item.get('control_submit_stage', {}).get('job_id') == '1001' for item in audit_lines)

    # Next stage is still blocked by expired contract.
    _complete_phase_stub(folder, 'convergence')
    nxt = maker.call('vasp_submit_stage', {'contract_id': cid, 'server': 'hpc', 'job_name': 'si-mid-exp', 'phase': 'relaxation'})
    assert nxt['ok'] is False and nxt['error_code'] == 'CONTRACT_EXPIRED'

    # Same guarantee on disconnect recovery when contract expires before confirm_submission.
    expired_flag[0] = False
    maker2, c2, _ = _draft_and_approve(ws, monkeypatch, server='hpc', job_name='si-mid-rec')
    cid2 = c2['contract_id']
    assert maker2.call('vasp_prepare_task', {'contract_id': cid2})['ok'] is True
    _record_green_review(cluster.job_folder('si-mid-rec'), 'convergence')

    def drop_after_submit_and_expire(f: Path, kw: dict) -> dict:
        rec_p = f / 'submissions' / f'{kw["phase"]}.json'
        rec_p.parent.mkdir(exist_ok=True)
        core.save(rec_p, {'status': 'submitted', 'job_id': '776655', 'phase': kw['phase']})
        expired_flag[0] = True
        raise subprocess.TimeoutExpired(cmd=['ssh'], timeout=30)

    cluster.submit_hook = drop_after_submit_and_expire
    rec_res = maker2.call('vasp_submit_stage', {'contract_id': cid2, 'server': 'hpc', 'job_name': 'si-mid-rec', 'phase': 'convergence'})
    assert rec_res['ok'] is True, rec_res
    assert rec_res['result']['job_id'] == '776655' and rec_res['result']['recovered'] is True
    assert rec_res['result']['monitoring']['registered'] is True
    assert rec_res['result']['ledger_warning']['error_code'] == 'CONTRACT_EXPIRED'


def test_p2_attestation_hook_allows_resource_transition_then_blocks_submit_before_reserve(tmp_path, monkeypatch):
    ws = _setup_workspace(tmp_path / 'ws')
    cluster = _install_fake_remote(monkeypatch, ws, tmp_path / 'remote')
    maker, contract, _ = _draft_and_approve(ws, monkeypatch, server='hpc', job_name='si-p2-order')
    cid = contract['contract_id']
    assert maker.call('vasp_prepare_task', {'contract_id': cid})['ok'] is True
    folder = cluster.job_folder('si-p2-order')
    _record_green_review(folder, 'convergence')
    assert maker.call('vasp_submit_stage', {'contract_id': cid, 'server': 'hpc', 'job_name': 'si-p2-order', 'phase': 'convergence'})['ok'] is True
    _complete_phase_stub(folder, 'convergence')

    hook_calls: list[str] = []
    allow_attestation = [False]

    def p2_hook(_c, _l, *, server, job_name, phase):
        hook_calls.append(phase)
        if not allow_attestation[0]:
            raise task_contract.ContractError('REVIEW_ATTESTATION_MISSING', f'缺少 {phase} 本地背书。')
        return {'enforced': True, 'mode': 'maker_checker', 'phase': phase}

    maker._review_attestation_verifier = p2_hook

    # 1) Transitioning convergence (array/2h) -> relaxation (allocation/4h) MUST succeed with awaiting_resource_review!
    cfg = maker.call('vasp_submit_stage', {'contract_id': cid, 'server': 'hpc', 'job_name': 'si-p2-order', 'phase': 'relaxation'})
    assert cfg['ok'] is True, cfg
    assert cfg['result']['status'] == 'awaiting_resource_review'
    assert cfg['result']['resources_changed'] is True
    assert cfg['result']['review_attestation']['pending_resource_review'] is True
    assert hook_calls == []
    assert task_contract.read_ledger(ws, cid)['submissions_used'] == 1

    # 2) After resource update, second call has resources_changed=False -> hook blocks BEFORE reserve_submission/submit_checked!
    _record_green_review(folder, 'relaxation')
    calls_before = len(cluster.calls)
    blocked = maker.call('vasp_submit_stage', {'contract_id': cid, 'server': 'hpc', 'job_name': 'si-p2-order', 'phase': 'relaxation'})
    assert blocked['ok'] is False and blocked['error_code'] == 'REVIEW_ATTESTATION_MISSING'
    assert hook_calls == ['relaxation']
    assert not any(action == 'submit_checked' for action, _, _ in cluster.calls[calls_before:])
    assert task_contract.read_ledger(ws, cid)['submissions_used'] == 1

    # 3) Once Checker attestation is present, submission proceeds cleanly.
    allow_attestation[0] = True
    ok = maker.call('vasp_submit_stage', {'contract_id': cid, 'server': 'hpc', 'job_name': 'si-p2-order', 'phase': 'relaxation'})
    assert ok['ok'] is True and ok['result']['job_id'] == '1002'
    assert ok['result']['review_attestation']['enforced'] is True


@pytest.mark.parametrize('diag_variant', ['missing_diagnosis', 'missing_task_files', 'missing_phase_entry', 'read_error'])
def test_disconnect_diagnostic_fail_closed_when_task_files_incomplete(tmp_path, monkeypatch, diag_variant):
    ws = _setup_workspace(tmp_path / 'ws')
    cluster = _install_fake_remote(monkeypatch, ws, tmp_path / 'remote')
    job_name = f'si-fc-{diag_variant[:8]}'
    maker, contract, _ = _draft_and_approve(ws, monkeypatch, server='hpc', job_name=job_name)
    cid = contract['contract_id']
    assert maker.call('vasp_prepare_task', {'contract_id': cid})['ok'] is True
    _record_green_review(cluster.job_folder(job_name), 'convergence')
    cluster.submit_hook = lambda _f, _kw: (_ for _ in ()).throw(ConnectionError('SSH dropped'))

    base_diag = {
        'receipt': None, 'query_error': None, 'lookup_status': 'no_match',
        'candidate_count': 0, 'malformed_rows': 0, 'truncated': False, 'accounting': [],
    }
    if diag_variant == 'missing_diagnosis':
        payload = dict(base_diag)
    elif diag_variant == 'missing_task_files':
        payload = {**base_diag, 'diagnosis': {'evidence': {}}}
    elif diag_variant == 'missing_phase_entry':
        payload = {**base_diag, 'diagnosis': {'evidence': {'task_files': [{'path': 'events.jsonl', 'exists': False}]}}}
    else:
        payload = {**base_diag, 'diagnosis': {'evidence': {'task_files': [
            {'path': 'submissions/convergence.json', 'exists': False, 'read_error': 'permission denied'},
        ]}}}
    cluster.diagnostic_hook = lambda _f, _kw: payload

    res = maker.call('vasp_submit_stage', {'contract_id': cid, 'server': 'hpc', 'job_name': job_name, 'phase': 'convergence'})
    assert res['ok'] is False and res['error_code'] == 'SUBMISSION_UNCERTAIN'
    assert task_contract.read_ledger(ws, cid)['status'] == 'halted_uncertain'


@pytest.mark.parametrize('corrupt_text', ['{not valid json', '["not-a-dict"]'])
def test_local_reconcile_corrupt_receipt_hard_halts_uncertain(tmp_path, monkeypatch, corrupt_text):
    ws = _setup_workspace(tmp_path / 'ws')
    maker, contract, _ = _draft_and_approve(ws, monkeypatch, server=None, job_name='si-loc-corrupt')
    cid = contract['contract_id']
    script_file = ws / 'local-submit.slurm'
    script_file.write_text(SLURM_TEMPLATE, encoding='utf-8', newline='\n')
    maker.box.submit_script = script_file.resolve()
    maker.box.script_hash = core.digest(maker.box.submit_script)
    assert maker.call('vasp_prepare_task', {'contract_id': cid})['ok'] is True
    folder = ws / 'runs/si-loc-corrupt'
    _record_green_review(folder, 'convergence')

    def corrupt_and_raise(_service, f: Path, phase: str, _limits: dict):
        rec_p = stage_gate.submission_record(f, phase)
        rec_p.parent.mkdir(exist_ok=True)
        rec_p.write_text(corrupt_text, encoding='utf-8', newline='\n')
        raise RuntimeError('crash mid-write')

    from vasp_agent.control import execution_reconcile

    monkeypatch.setattr(execution_reconcile, 'local_submit_checked', corrupt_and_raise)
    res = maker.call('vasp_submit_stage', {'contract_id': cid, 'server': None, 'job_name': 'si-loc-corrupt', 'phase': 'convergence'})
    assert res['ok'] is False and res['error_code'] == 'SUBMISSION_UNCERTAIN'
    assert task_contract.read_ledger(ws, cid)['status'] == 'halted_uncertain'


def test_local_submit_refuses_unapproved_stray_script_and_symlinked_submission_dir(tmp_path, monkeypatch):
    ws = _setup_workspace(tmp_path / 'ws')
    maker, contract, _ = _draft_and_approve(ws, monkeypatch, server=None, job_name='si-stray')
    cid = contract['contract_id']
    assert maker.call('vasp_prepare_task', {'contract_id': cid})['ok'] is True
    folder = ws / 'runs/si-stray'
    stray_script = folder / 'submit.slurm'
    stray_script.write_text(SLURM_TEMPLATE, encoding='utf-8', newline='\n')
    before_script = stray_script.read_bytes()
    before_manifest = (folder / 'manifest.json').read_bytes()

    # Without maker.box.submit_script configured, submit_stage MUST refuse BEFORE mutating stray submit.slurm/manifest!
    refused = maker.call('vasp_submit_stage', {'contract_id': cid, 'server': None, 'job_name': 'si-stray', 'phase': 'convergence'})
    assert refused['ok'] is False and refused['error_code'] == 'LOCAL_SUBMIT_SCRIPT_MISSING'
    assert stray_script.read_bytes() == before_script
    assert (folder / 'manifest.json').read_bytes() == before_manifest
    assert task_contract.read_ledger(ws, cid)['submissions_used'] == 0

    # With submit_script configured, symlinked submissions/ parent dir is refused and refunds in_flight.
    script_file = ws / 'approved.slurm'
    script_file.write_text(SLURM_TEMPLATE, encoding='utf-8', newline='\n')
    maker.box.submit_script = script_file.resolve()
    maker.box.script_hash = core.digest(maker.box.submit_script)
    assert maker.call('vasp_submit_stage', {'contract_id': cid, 'server': None, 'job_name': 'si-stray', 'phase': 'convergence'})['result']['status'] == 'awaiting_resource_review'
    _record_green_review(folder, 'convergence')

    subs_dir = folder / 'submissions'
    subs_dir.mkdir(exist_ok=True)
    real_is_symlink = Path.is_symlink
    monkeypatch.setattr(Path, 'is_symlink', lambda p: True if p == subs_dir else real_is_symlink(p))
    sym_sub = maker.call('vasp_submit_stage', {'contract_id': cid, 'server': None, 'job_name': 'si-stray', 'phase': 'convergence'})
    monkeypatch.setattr(Path, 'is_symlink', real_is_symlink)
    assert sym_sub['ok'] is False and sym_sub['error_code'] == 'CONTRACT_SYMLINK_REFUSED'
    led = task_contract.read_ledger(ws, cid)
    assert led['submissions_used'] == 0 and led['in_flight'] is None


def test_prepare_blocks_non_convergence_fork_contract_and_halts_on_script_drift(tmp_path, monkeypatch):
    ws = _setup_workspace(tmp_path / 'ws')
    cluster = _install_fake_remote(monkeypatch, ws, tmp_path / 'remote')

    # 1) Fork contract starting at relaxation is refused BEFORE creating folder or marking plan prepared.
    maker, contract, _ = _draft_and_approve(
        ws, monkeypatch, server='hpc', job_name='si-fork-only',
        allowed_phases=['relaxation', 'scf'], allow_forks=True,
    )
    cid = contract['contract_id']
    plan_file = parameter_plans.path(maker.box, contract['workflow']['plan_id'])
    res = maker.call('vasp_prepare_task', {'contract_id': cid})
    assert res['ok'] is False and res['error_code'] == 'CONTRACT_SCOPE_VIOLATION'
    assert not cluster.job_folder('si-fork-only').exists()
    assert json.loads(plan_file.read_text(encoding='utf-8'))['status'] == 'selected'

    # 2) Script hash drift during prepare_task's _configure_phase_resources auto-halts contract.
    maker_l, cl, _ = _draft_and_approve(ws, monkeypatch, server=None, job_name='si-prep-drift')
    cidl = cl['contract_id']
    script_file = ws / 'prep-submit.slurm'
    script_file.write_text(SLURM_TEMPLATE, encoding='utf-8', newline='\n')
    maker_l.box.submit_script = script_file.resolve()
    maker_l.box.script_hash = core.digest(maker_l.box.submit_script)
    script_file.write_text(SLURM_TEMPLATE + '# tampered\n', encoding='utf-8', newline='\n')
    drift = maker_l.call('vasp_prepare_task', {'contract_id': cidl})
    assert drift['ok'] is False and drift['error_code'] == 'SCRIPT_HASH_DRIFT'
    assert task_contract.read_ledger(ws, cidl)['status'] == 'halted'


def test_existing_record_race_mismatched_phase_revoked_consecutive_failures_and_ssh_sig(tmp_path, monkeypatch):
    ws = _setup_workspace(tmp_path / 'ws')
    cluster = _install_fake_remote(monkeypatch, ws, tmp_path / 'remote')

    # 1) submit_checked returning existing_record=True (submitted) refunds budget and registers monitor.
    maker, c1, _ = _draft_and_approve(ws, monkeypatch, server='hpc', job_name='si-race-ok')
    cid1 = c1['contract_id']
    assert maker.call('vasp_prepare_task', {'contract_id': cid1})['ok'] is True
    _record_green_review(cluster.job_folder('si-race-ok'), 'convergence')
    cluster.submit_hook = lambda _f, kw: {
        'status': 'submitted', 'job_id': '314159', 'phase': kw['phase'],
        'submission_not_attempted': True, 'existing_record': True,
    }
    race_ok = maker.call('vasp_submit_stage', {'contract_id': cid1, 'server': 'hpc', 'job_name': 'si-race-ok', 'phase': 'convergence'})
    assert race_ok['ok'] is True and race_ok['result']['status'] == 'already_submitted' and race_ok['result']['job_id'] == '314159'
    assert race_ok['result']['monitoring']['registered'] is True
    assert task_contract.read_ledger(ws, cid1)['submissions_used'] == 0

    # 2) submit_checked existing_record with submission_uncertain OR wrong phase hard-halts halted_uncertain.
    cluster.submit_hook = lambda _f, _kw: {
        'status': 'submitted', 'job_id': '314159', 'phase': 'relaxation',
        'submission_not_attempted': True, 'existing_record': True,
    }
    wrong_phase = maker.call('vasp_submit_stage', {'contract_id': cid1, 'server': 'hpc', 'job_name': 'si-race-ok', 'phase': 'convergence'})
    assert wrong_phase['ok'] is False and wrong_phase['error_code'] == 'SUBMISSION_UNCERTAIN'
    assert task_contract.read_ledger(ws, cid1)['status'] == 'halted_uncertain'

    maker_u, cu, _ = _draft_and_approve(ws, monkeypatch, server='hpc', job_name='si-race-unc')
    cidu = cu['contract_id']
    assert maker_u.call('vasp_prepare_task', {'contract_id': cidu})['ok'] is True
    _record_green_review(cluster.job_folder('si-race-unc'), 'convergence')
    cluster.submit_hook = lambda _f, kw: {
        'status': 'submission_uncertain', 'job_id': None, 'phase': kw['phase'],
        'submission_not_attempted': True, 'existing_record': True,
    }
    race_unc = maker_u.call('vasp_submit_stage', {'contract_id': cidu, 'server': 'hpc', 'job_name': 'si-race-unc', 'phase': 'convergence'})
    assert race_unc['ok'] is False and race_unc['error_code'] == 'SUBMISSION_UNCERTAIN'
    assert task_contract.read_ledger(ws, cidu)['status'] == 'halted_uncertain'
    cluster.submit_hook = None

    # 3) Revoked contract is refused on prepare and submit.
    maker_r, cr, _ = _draft_and_approve(ws, monkeypatch, server='hpc', job_name='si-revoked')
    cidr = cr['contract_id']
    task_contract.revoke_contract(
        ws, cidr, provider=task_contract.LocalTTYApprovalProvider(reader=lambda _p: '1', output=lambda _s: None),
    )
    assert maker_r.call('vasp_prepare_task', {'contract_id': cidr})['error_code'] == 'CONTRACT_REVOKED'
    assert maker_r.call('vasp_submit_stage', {'contract_id': cidr, 'server': 'hpc', 'job_name': 'si-revoked', 'phase': 'convergence'})['error_code'] == 'CONTRACT_REVOKED'

    # 4) consecutive_failures reaching max_consecutive_failures auto-halts BEFORE touching remote.
    maker_cf, ccf, _ = _draft_and_approve(
        ws, monkeypatch, server='hpc', job_name='si-consec',
        budget={'max_submissions': 5, 'max_resume_retries': 0, 'max_repair_retries': 0, 'max_scan_retries': 0, 'max_consecutive_failures': 2},
    )
    cidcf = ccf['contract_id']
    assert maker_cf.call('vasp_prepare_task', {'contract_id': cidcf})['ok'] is True
    # Directly set consecutive_failures == max while keeping active to verify submit_stage pre-remote auto-halt path.
    with task_contract._ledger.ledger_transaction(ws, cidcf, write=True) as led:
        led['consecutive_failures'] = 2
    calls_before = len(cluster.calls)
    cf_res = maker_cf.call('vasp_submit_stage', {'contract_id': cidcf, 'server': 'hpc', 'job_name': 'si-consec', 'phase': 'convergence'})
    assert cf_res['ok'] is False and cf_res['error_code'] == 'CONTRACT_CONSECUTIVE_FAILURES_EXCEEDED'
    assert len(cluster.calls) == calls_before
    assert task_contract.read_ledger(ws, cidcf)['status'] == 'halted'

    # 5) require_ssh_signature=True refuses missing or tampered .sig BEFORE touching remote.
    maker_s, cs, _ = _draft_and_approve(ws, monkeypatch, server='hpc', job_name='si-ssh-sig')
    cids = cs['contract_id']
    signers_file = ws / 'allowed_signers'
    signers_file.write_text('op@local ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAITestKey\n', encoding='utf-8', newline='\n')
    pol_path = agent_paths.file_path(ws, 'autonomy_policy')
    pol_path.parent.mkdir(parents=True, exist_ok=True)
    pol_path.write_text(
        json.dumps({'require_ssh_signature': True, 'allowed_signers': 'allowed_signers', 'principal': 'op@local'}) + '\n',
        encoding='utf-8', newline='\n',
    )
    calls_before_sig = len(cluster.calls)
    miss_sig = maker_s.call('vasp_submit_stage', {'contract_id': cids, 'server': 'hpc', 'job_name': 'si-ssh-sig', 'phase': 'convergence'})
    assert miss_sig['ok'] is False and miss_sig['error_code'] == 'CONTRACT_SIGNATURE_INVALID'
    assert len(cluster.calls) == calls_before_sig

    # Add signature metadata + tampered .sig file -> ssh-keygen -Y verify failure blocks BEFORE remote.
    active_file = agent_paths.store_dir(ws, 'contracts') / 'active' / f'{cids}.json'
    doc = json.loads(active_file.read_text(encoding='utf-8'))
    doc['approval']['ssh_signature'] = {'namespace': 'vasp-agent-contract', 'sig_file': f'{cids}.json.sig', 'principal': 'op@local'}
    active_file.write_text(json.dumps(doc, ensure_ascii=False, indent=2) + '\n', encoding='utf-8', newline='\n')
    (active_file.parent / f'{cids}.json.sig').write_text('-----BEGIN SSH SIGNATURE-----\nTAMPERED\n-----END SSH SIGNATURE-----\n', encoding='utf-8', newline='\n')
    monkeypatch.setattr(task_contract, '_resolve_ssh_keygen', lambda: 'ssh-keygen')
    monkeypatch.setattr(subprocess, 'run', lambda *_a, **_kw: SimpleNamespace(returncode=1, stdout=b'', stderr=b'bad sig'))
    bad_sig = maker_s.call('vasp_submit_stage', {'contract_id': cids, 'server': 'hpc', 'job_name': 'si-ssh-sig', 'phase': 'convergence'})
    assert bad_sig['ok'] is False and bad_sig['error_code'] == 'CONTRACT_SIGNATURE_INVALID'
    assert len(cluster.calls) == calls_before_sig


# ------------------ P3b: Contract preset solver_overrides, policy & confirm v2

def test_p3b_contract_preset_solver_overrides_policy_and_tty_approval(tmp_path, monkeypatch):
    ws = _setup_workspace(tmp_path / 'ws')
    cluster = _install_fake_remote(monkeypatch, ws, tmp_path / 'remote')
    maker = ControlService(ws, role='maker', actor_id='mk-p3b')
    phases = ['convergence', 'relaxation', 'scf', 'bands']
    env = _remote_envelope(phases)

    base_args = {
        'server': 'hpc', 'job_name': 'si-p3-preset', 'structure': 'structures/Si.POSCAR',
        'encut': 400, 'mesh': [4, 4, 4], 'nbands': 16, 'relax_mode': 'cell',
        'goal': 'Si 3D 非磁性 PBE 预设 Tier 3 求解器覆盖测试', 'resource_envelope': env,
        'allowed_phases': phases,
    }
    # 1) Policy forbidding overrides rejects initial solver_overrides BEFORE writing any scan plan.
    before_plans = set(agent_paths.store_items(ws, 'scan_plans', '*.json'))
    denied_pol = maker.call('vasp_draft_contract', {
        **base_args,
        'solver_policy': {'allow_solver_overrides': False, 'max_nelm': 300},
        'solver_overrides': {'stages': {'convergence': {'tags': {'ALGO': 'All'}, 'reason': '收敛稳健性测试'}}},
    })
    assert denied_pol['ok'] is False and denied_pol['error_code'] == 'SOLVER_OVERRIDE_FORBIDDEN'
    assert set(agent_paths.store_items(ws, 'scan_plans', '*.json')) == before_plans

    # 2) NELM exceeding contract solver_policy.max_nelm is rejected with SOLVER_OVERRIDE_FORBIDDEN.
    over_nelm = maker.call('vasp_draft_contract', {
        **base_args,
        'solver_policy': {'allow_solver_overrides': True, 'max_nelm': 200},
        'solver_overrides': {'stages': {'scf': {'tags': {'NELM': 240}, 'reason': '超上限测试'}}},
    })
    assert over_nelm['ok'] is False and over_nelm['error_code'] == 'SOLVER_OVERRIDE_FORBIDDEN'
    assert set(agent_paths.store_items(ws, 'scan_plans', '*.json')) == before_plans

    # 3) Valid solver_policy + initial solver_overrides appear in TTY summary and apply via vasp_prepare_task.
    preset_ov = {
        'stages': {
            'convergence': {'tags': {'ALGO': 'All', 'NELM': 200}, 'reason': '收敛测试采用稳健 All 求解器与 200 步'},
            'relaxation': {'tags': {'IBRION': 1, 'POTIM': 0.25}, 'reason': '初始采用 RMM-DIIS 与较小步长'},
        },
    }
    draft_res = maker.call('vasp_draft_contract', {
        **base_args,
        'solver_policy': {'allow_solver_overrides': True, 'max_nelm': 200},
        'solver_overrides': preset_ov,
    })
    assert draft_res['ok'] is True, draft_res
    assert draft_res['result']['solver_policy'] == {'allow_solver_overrides': True, 'max_nelm': 200}
    draft_id = draft_res['result']['draft_id']

    rendered_pages: list[str] = []
    monkeypatch.setattr(task_contract, 'has_real_tty', lambda stream=None: True)
    approved = contract_ui.approve_contract_interactive(
        ws, draft_id, reader=lambda _p: '1', output=rendered_pages.append,
    )
    tty_text = '\n'.join(rendered_pages)
    assert 'max_nelm <= 200' in tty_text
    assert 'ALGO=All, NELM=200' in tty_text
    assert 'IBRION=1, POTIM=0.25' in tty_text

    contract = approved['contract']
    cid = contract['contract_id']
    prep = maker.call('vasp_prepare_task', {'contract_id': cid})
    assert prep['ok'] is True, prep
    assert prep['result']['physics']['confirmed_by'] == 'operator_task_contract'
    assert prep['result']['physics']['contract_id'] == cid
    assert prep['result']['physics']['contract_sha256'] == contract['approval']['canonical_sha256']
    assert prep['result']['solver_overrides']['stages']['convergence']['tags'] == {'ALGO': 'All', 'NELM': 200}

    folder = cluster.job_folder('si-p3-preset')
    cfg = json.loads((folder / 'config.json').read_text(encoding='utf-8'))
    assert cfg['solver_overrides']['version'] == 1
    assert cfg['solver_overrides']['stages']['relaxation']['tags'] == {'IBRION': 1, 'POTIM': 0.25}
    plan = json.loads((folder / 'input-plan.json').read_text(encoding='utf-8'))
    assert plan['incars']['convergence/encut-0']['ALGO'] == 'All'
    assert plan['incars']['convergence/encut-0']['NELM'] == 200
    assert plan['incars']['relax-1']['IBRION'] == 1 and plan['incars']['relax-1']['POTIM'] == 0.25
    assert plan['incars']['validation/reference']['IBRION'] == -1

    # Stage review immediately exposes incar_solver_diff for convergence.
    checker = ControlService(ws, role='checker', actor_id='chk-p3b')
    rev = checker.call('vasp_get_stage_review', {'server': 'hpc', 'job_name': 'si-p3-preset'})
    assert rev['ok'] is True, rev
    diff = rev['result']['summary']['incar_solver_diff'] if rev['result'].get('payload_overflow') else rev['result']['incar_solver_diff']
    assert diff['phase'] == 'convergence'
    assert {c['tag'] for c in diff['changes']} == {'ALGO', 'NELM'}


def test_p3b_real_remote_compute_prepare_forwards_solver_overrides(tmp_path, monkeypatch):
    ws = _setup_workspace(tmp_path / 'ws')
    captured: list[tuple[str, str, dict]] = []
    real_rc = object.__new__(remote_compute.RemoteCompute)
    real_rc.workspace = ws
    real_rc.server = 'hpc'
    monkeypatch.setattr(real_rc, 'call', lambda action, name, **kw: captured.append((action, name, kw)) or {'prepared': True})
    preset_ov = {'version': 1, 'stages': {'scf': {'tags': {'ALGO': 'All', 'NELM': 200}, 'reason': 'real prepare payload check'}}}
    real_rc.prepare('si-real-ov', 'structures/Si.POSCAR', solver_overrides=preset_ov)
    real_rc.prepare('si-real-none', 'structures/Si.POSCAR')
    assert captured[0][0] == 'prepare' and captured[0][2]['parameters']['solver_overrides'] == preset_ov
    assert 'solver_overrides' not in captured[1][2]['parameters']



