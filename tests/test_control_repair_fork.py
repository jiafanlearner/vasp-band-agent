"""Offline tests for P1e-3 contract-gated bounded repair fork (`vasp_prepare_repair_fork`)."""
from __future__ import annotations

import contextlib
import hashlib
import io
import json
import os
from pathlib import Path
import shutil
import subprocess
from types import SimpleNamespace

import numpy as np
import pytest
from pymatgen.core import Lattice, Structure
from pymatgen.io.vasp.inputs import Poscar, Potcar

from vasp_agent.chat import release_check
from vasp_agent.control import ControlService
from vasp_agent.permissions import contract_ui, task_contract
from vasp_agent.remote import environment_profiles, remote_compute, remote_worker
from vasp_agent.workflow import (
    band_path as band_path_mod,
    convergence_reuse,
    dos_stage,
    full_workflow,
    input_review,
    stage_gate,
)
import vasp_agent.workflow.vasp_band_agent as core

ROOT = Path(__file__).resolve().parents[1]
EXAMPLE_POSCAR = ROOT / 'examples/Si.POSCAR'
FAKE_POTCAR = 'PAW_PBE Si 05Jan2001\n'
EVIDENCE_SHA = 'e' * 64
SCF_NBANDS = 16

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
def fake_pbe_potcar_and_vasp(monkeypatch):
    fake_pot = [SimpleNamespace(element='Si', functional='PBE', TITEL='PAW_PBE Si 05Jan2001', ZVAL=4.0, enmax=245.345)]
    monkeypatch.setattr(Potcar, 'from_str', classmethod(lambda _cls, _text: fake_pot))
    monkeypatch.setattr(Potcar, 'from_file', classmethod(lambda _cls, _path: fake_pot))

    def _read_fake_result(folder: Path):
        structure = Poscar.from_file(folder / 'POSCAR').structure
        raw = (folder / 'vasprun.xml').read_text(encoding='utf-8')
        try:
            payload = json.loads(raw)
            energy = float(payload['energy_per_atom']) * len(structure)
        except ValueError:
            energy = -5.0 * len(structure)
        n = len(structure)
        return SimpleNamespace(
            final_energy=energy,
            final_structure=structure,
            converged_ionic=True,
            efermi=0.0,
            parameters={'NELM': 150, 'NBANDS': SCF_NBANDS, 'NELECT': 4.0 * n},
            ionic_steps=[{
                'forces': [[0.0, 0.0, 0.0] for _ in range(n)],
                'stress': [[0.0, 0.0, 0.0], [0.0, 0.0, 0.0], [0.0, 0.0, 0.0]],
                'electronic_steps': [{}],
            }],
        )

    monkeypatch.setattr(core, 'read_result', _read_fake_result)


def _setup_workspace(tmp_path: Path, *, slab_2d: bool = False) -> Path:
    structures = tmp_path / 'structures'
    structures.mkdir(parents=True, exist_ok=True)
    if slab_2d:
        slab = Structure(Lattice.tetragonal(3.84, 18.0), ['Si', 'Si'], [[0.0, 0.0, 0.45], [0.5, 0.5, 0.55]])
        Poscar(slab).write_file(structures / 'Si2D.POSCAR')
    else:
        (structures / 'Si.POSCAR').write_bytes(EXAMPLE_POSCAR.read_bytes())
    potcar = tmp_path / 'potentials/PBE/Si/POTCAR'
    potcar.parent.mkdir(parents=True, exist_ok=True)
    potcar.write_text(FAKE_POTCAR, encoding='utf-8', newline='\n')
    return tmp_path


def _envelope(*, allow_array: bool = False, include_dos: bool = False, conv_parallel: int = 1) -> dict:
    phases = ['convergence', 'relaxation', 'scf', 'bands', *(['dos'] if include_dos else [])]
    defaults = {
        phase: {
            'submission_mode': 'array' if (allow_array and phase == 'convergence') else 'allocation',
            'parallel_tests': conv_parallel if phase == 'convergence' else 1,
            'cores_per_test': 8,
            'nodes': 1,
            'partition': 'compute',
            'walltime': '02:00:00',
        }
        for phase in phases
    }
    return {
        'allowed_modes': ['allocation', 'array'] if allow_array else ['allocation'],
        'allowed_partitions': ['compute', 'highmem'],
        'max_parallel_tests': 4,
        'max_cores_per_test': 16,
        'max_nodes': 2,
        'max_total_cores': 64,
        'max_walltime': '04:00:00',
        'phase_defaults': defaults,
    }


def _configure_local_submit_script(service: ControlService, ws: Path) -> Path:
    script = ws / 'local.slurm'
    script.write_text(SLURM_TEMPLATE, encoding='utf-8', newline='\n')
    service.box.submit_script = script
    service.box.script_hash = core.digest(script)
    return script


def _draft_and_approve(
    ws: Path,
    monkeypatch,
    *,
    server: str | None = None,
    job_name: str = 'si-rep',
    slab_2d: bool = False,
    allow_array: bool = False,
    include_dos: bool = False,
    band_path: str | None = None,
    budget: dict | None = None,
    precision_bounds: dict | None = None,
    nbands: int = SCF_NBANDS,
) -> tuple[ControlService, dict]:
    maker = ControlService(ws, role='maker', actor_id='mk-repair')
    if server is None:
        _configure_local_submit_script(maker, ws)
    args = {
        'server': server,
        'job_name': job_name,
        'structure': 'structures/Si2D.POSCAR' if slab_2d else 'structures/Si.POSCAR',
        'encut': 400,
        'mesh': [4, 4, 1] if slab_2d else [4, 4, 4],
        'nbands': nbands,
        'relax_mode': 'ions' if slab_2d else 'cell',
        'goal': 'Si 2D nonmagnetic PBE repair test' if slab_2d else 'Si 3D nonmagnetic PBE repair test',
        'resource_envelope': _envelope(allow_array=allow_array, include_dos=include_dos, conv_parallel=3 if allow_array else 1),
        'budget': budget or {
            'max_submissions': 10,
            'max_resume_retries': 2,
            'max_repair_retries': 3,
            'max_scan_retries': 2,
            'max_consecutive_failures': 3,
        },
        **({'optional_stages': ['dos']} if include_dos else {}),
        **({'band_path': band_path} if band_path is not None else {}),
        **({'precision_bounds': precision_bounds} if precision_bounds is not None else {}),
    }
    draft = maker.call('vasp_draft_contract', args)
    assert draft['ok'] is True, draft
    monkeypatch.setattr(task_contract, 'require_real_tty', lambda _stream=None: None)
    approved = contract_ui.approve_contract_interactive(
        ws, draft['result']['draft_id'], reader=lambda _p: '1', output=lambda _t: None,
    )
    return maker, approved['contract']


def _record_green_review(folder: Path, phase: str) -> dict:
    ctx = stage_gate.context(folder)
    assert ctx['phase'] == phase
    checks = [
        {
            'item': item,
            'judgment': 'pending' if (phase == 'convergence' and item == 'validation_plan') else 'pass',
            'blocking': False,
            'evidence': f'verified {phase}/{item}',
            'reason': 'offline repair test green',
            'next_action': '无需',
        }
        for item in ctx['required_checks']
    ]
    return stage_gate.record(folder, ctx['snapshot'], phase, checks, 'checker:test:pro:chk001')


def _complete_local_through(folder: Path, target_completed: str, monkeypatch, *, scf_top_ev: float = 8.0) -> None:
    """Run real `full_workflow.run_stage` with mocked `core.execute` up through `target_completed`."""
    def fake_execute(root: Path, name: str, command: list[str], event_root: Path | None = None):
        stage_dir = root / name
        structure = Poscar.from_file(stage_dir / 'POSCAR').structure
        n = len(structure)
        if not (stage_dir / 'complete.json').exists():
            (stage_dir / 'stdout.log').write_text('ok\n', encoding='utf-8', newline='\n')
            xml = stage_dir / 'vasprun.xml'
            xml.write_text(json.dumps({'energy_per_atom': -5.0}), encoding='utf-8', newline='\n')
            stamp = {'xml_sha256': core.digest(xml)}
            if name == 'scf':
                chg = stage_dir / 'CHGCAR'
                chg.write_text('mock scf charge density\n', encoding='utf-8', newline='\n')
                stamp['charge_sha256'] = core.digest(chg)
            core.save(stage_dir / 'complete.json', stamp)
        return SimpleNamespace(
            final_structure=structure,
            final_energy=-5.0 * n,
            converged_ionic=True,
            efermi=0.0,
            parameters={'NELM': 150, 'NBANDS': SCF_NBANDS, 'NELECT': 4.0 * n},
            ionic_steps=[{
                'forces': [[0.0, 0.0, 0.0] for _ in range(n)],
                'stress': [[0.0, 0.0, 0.0], [0.0, 0.0, 0.0], [0.0, 0.0, 0.0]],
                'electronic_steps': [{}],
            }],
        )

    monkeypatch.setattr(core, 'execute', fake_execute)
    monkeypatch.setattr(
        dos_stage,
        'scf_bands',
        lambda _root: {
            'band_minima_eV': [-12.0, -4.0] + list(np.linspace(1.0, scf_top_ev, SCF_NBANDS - 2)),
            'lowest_eigenvalue_eV': -12.0,
            'kpoints': 8,
        },
    )
    order = ['convergence', 'relaxation', 'scf']
    stop_idx = order.index(target_completed)
    for ph in order[:stop_idx + 1]:
        _record_green_review(folder, ph)
        with contextlib.redirect_stdout(io.StringIO()):
            full_workflow.run_full(folder, ['fake-vasp'])


class _FakeRemoteRepairCluster:
    """Offline fake remote cluster wired to real frozen workflow & worker_resources."""

    def __init__(self, ws: Path, remote_root: Path):
        self.ws = ws
        self.remote_root = remote_root
        self.jobs_dir = remote_root / 'jobs'
        self.jobs_dir.mkdir(parents=True, exist_ok=True)
        self.env = {
            'environment_sha256': '1' * 64,
            'executable_sha256': '2' * 64,
            'executable': '/opt/vasp/vasp_std',
        }
        self.status_overrides: dict[str, dict] = {}
        self.disconnect_on: dict[str, tuple[Exception, bool]] = {}
        self.calls: list[tuple[str, str | None]] = []

    def _write_remote_meta(self, folder: Path, job_name: str, *, parallel: int = 1, cores: int = 8) -> None:
        script = (
            f'#!/bin/bash\n#SBATCH --job-name=va-{job_name}\n#SBATCH --partition=compute\n'
            f'#SBATCH --nodes=1\n#SBATCH --ntasks={parallel * cores}\n#SBATCH --cpus-per-task=1\n'
            f'#SBATCH --time=02:00:00\n#SBATCH --output=slurm-%j.log\nset -eo pipefail\n'
            f'export OMP_NUM_THREADS=1\npython3 vasp_band_agent.py run {folder} -- srun vasp_std\n'
        )
        (folder / 'submit.slurm').write_text(script, encoding='utf-8', newline='\n')
        core.save(folder / 'environment-review.json', self.env)
        core.save(folder / 'remote-job.json', {
            'release': str(self.remote_root / 'releases/test'),
            'script_sha256': core.digest(folder / 'submit.slurm'),
            'resources': {'partition': 'compute', 'walltime': '02:00:00', 'ntasks': parallel * cores, 'nodes': 1,
                          'parallel_tests': parallel, 'cores_per_test': cores},
        })

    def prepare(self, name: str, structure: str, **kwargs):
        self.calls.append(('prepare', name))
        preview_only = bool(kwargs.get('preview_only', False))
        folder = self.jobs_dir / name
        if not preview_only and 'prepare' in self.disconnect_on:
            exc, create_first = self.disconnect_on.pop('prepare')
            if not create_first:
                raise exc
            self._prepare_folder(folder, name, structure, **kwargs)
            raise exc
        return self._prepare_folder(folder, name, structure, **kwargs)

    def _prepare_folder(self, folder: Path, name: str, structure: str, **kwargs):
        preview_only = bool(kwargs.get('preview_only', False))
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
            encuts=kwargs.get('encuts'),
            meshes=kwargs.get('meshes'),
            parameter_choice=kwargs.get('parameter_choice'),
            scan_thresholds=kwargs.get('scan_thresholds'),
            scan_preset=kwargs.get('scan_preset'),
            band_path=kwargs.get('band_path'),
            optional_stages=kwargs.get('optional_stages'),
            stage_parameters=kwargs.get('stage_parameters'),
            environment_profile=kwargs.get('environment_profile'),
            preview_only=preview_only,
            ediff=1e-7, points=40, symprec=0.01, force_tol=0.01, stress_tol=1.0, energy_tol=0.001, nsw=200,
        )
        with contextlib.redirect_stdout(io.StringIO()):
            result = full_workflow.prepare_full(args)
        if preview_only:
            return result
        self._write_remote_meta(folder, name, parallel=args.parallel_tests, cores=args.cores_per_test)
        return {'prepared': True, 'directory': str(folder)}

    def call(self, action: str, job_name: str | None = None, **params):
        self.calls.append((action, job_name))
        if action == 'list_jobs':
            return {'jobs': [{'name': p.name} for p in sorted(self.jobs_dir.iterdir()) if p.is_dir()]}
        folder = self.jobs_dir / str(job_name)
        if action == 'confirm_physics':
            return stage_gate.confirm_physics(folder, params['goal'], params.get('channel', 'operator_slash_command'))
        if action == 'status':
            if not folder.is_dir():
                raise core.ToolError('FILE_NOT_FOUND', f'远程任务 {job_name} 不存在。')
            data = {}
            for fn in ('manifest.json', 'workflow-state.json', 'checkpoint.json', 'physics-model.json',
                       'remote-job.json', 'convergence-array.json'):
                if (folder / fn).is_file():
                    data[fn] = json.loads((folder / fn).read_text(encoding='utf-8'))
            subs = {}
            if (folder / 'submissions').is_dir():
                for p in (folder / 'submissions').glob('*.json'):
                    subs[p.stem] = json.loads(p.read_text(encoding='utf-8'))
            data['stage_submissions'] = subs
            data['queue'] = None
            data['scheduler_status_known'] = True
            data.update(self.status_overrides.get(str(job_name), {}))
            return data
        if action == 'review_context':
            if not folder.is_dir():
                raise core.ToolError('FILE_NOT_FOUND', f'远程任务 {job_name} 不存在。')
            return input_review.context(folder)
        if action == 'stage_context':
            return stage_gate.context(folder)
        if action == 'resource_context':
            return remote_worker.resource_context(folder, params.get('phase'))
        if action == 'configure_resources':
            return remote_worker.configure_resources(
                folder, params.get('phase'), params['resources'], params['signature'], params['limits'], params.get('operation_id'),
            )
        if action == 'submit_checked':
            phase = params['phase']
            stage_gate.require_review(folder, phase)
            receipt = {'status': 'submitted', 'job_id': '91001', 'phase': phase, 'script_sha256': core.digest(folder / 'submit.slurm')}
            (folder / 'submissions').mkdir(exist_ok=True)
            core.save(folder / 'submissions' / f'{phase}.json', receipt)
            return receipt
        if action in ('prepare_production_from_convergence', 'prepare_bands_path', 'prepare_dos_fork'):
            if action in self.disconnect_on:
                exc, create_first = self.disconnect_on.pop(action)
                if not create_first:
                    raise exc
                self._run_fork_action(action, folder, str(job_name), params)
                raise exc
            return self._run_fork_action(action, folder, str(job_name), params)
        raise AssertionError(f'unexpected remote action: {action}')

    def _run_fork_action(self, action: str, folder: Path, name: str, params: dict) -> dict:
        source = self.jobs_dir / params['source_job']
        if folder.exists():
            raise ValueError('派生必须使用不存在的新任务目录。')
        with contextlib.redirect_stdout(io.StringIO()):
            if action == 'prepare_production_from_convergence':
                kwargs = {'encut': params['encut']} if params.get('encut') is not None else {}
                res = convergence_reuse.prepare_production(source, folder, params['mesh'], params['source_snapshot'], **kwargs)
            elif action == 'prepare_bands_path':
                res = band_path_mod.prepare_bands_path(source, folder, params['band_path'], environment=self.env)
            elif action == 'prepare_dos_fork':
                res = dos_stage.prepare_dos_fork(source, folder, params['values'], environment=self.env)
            else:
                raise AssertionError(action)
        self._write_remote_meta(folder, name, parallel=1, cores=8)
        return res


def _install_remote(monkeypatch, ws: Path, cluster: _FakeRemoteRepairCluster, *, server: str = 'hpc') -> None:
    monkeypatch.setattr(release_check, 'ensure_current', lambda _box, _srv: None)
    monkeypatch.setattr(remote_worker, 'task_release', lambda _meta: None)

    class _Proxy:
        def __init__(self, _workspace, _server):
            assert _server == server

        def prepare(self, *a, **kw):
            return cluster.prepare(*a, **kw)

        def call(self, *a, **kw):
            return cluster.call(*a, **kw)

    monkeypatch.setattr(remote_compute, 'RemoteCompute', _Proxy)


def _diag(**overrides) -> dict:
    base = {
        'failure_category': 'oom',
        'evidence_sha256': EVIDENCE_SHA,
        'repair_hypothesis': '单节点内存不足触发 OOM，改为 2 节点 allocation 降低单节点 MPI 密度',
        'applied_change': '切换为 allocation，nodes=2, parallel_tests=1, cores_per_test=8, walltime=03:00:00',
    }
    base.update(overrides)
    return base


# ---------------------------------------------------------------- Tests


def test_remote_convergence_array_oom_fresh_restart_and_submit_loop(tmp_path, monkeypatch):
    ws = _setup_workspace(tmp_path)
    cluster = _FakeRemoteRepairCluster(ws, tmp_path / 'remote')
    _install_remote(monkeypatch, ws, cluster)
    maker, contract = _draft_and_approve(ws, monkeypatch, server='hpc', job_name='si-arr', allow_array=True)
    cid = contract['contract_id']

    prep = maker.call('vasp_prepare_task', {'contract_id': cid})
    assert prep['ok'] is True, prep
    src_dir = cluster.jobs_dir / 'si-arr'
    (src_dir / 'submissions').mkdir(exist_ok=True)
    core.save(src_dir / 'submissions/convergence.json', {'status': 'submitted', 'job_id': '7001', 'phase': 'convergence', 'submission_mode': 'array'})
    core.save(src_dir / 'workflow-state.json', {'status': 'failed', 'stage': 'convergence', 'next': 'convergence'})
    cluster.status_overrides['si-arr'] = {
        'array_tasks': {'all_terminal': True, 'known': True, 'tasks': [{'job_id': '7001_0', 'state': 'OUT_OF_MEMORY'}]},
    }
    maker.box.monitor.register('hpc', 'si-arr', 'convergence', '7001')

    repaired_res = {
        'submission_mode': 'allocation', 'parallel_tests': 1, 'cores_per_test': 8,
        'nodes': 2, 'partition': 'highmem', 'walltime': '03:00:00',
    }
    res = maker.call('vasp_prepare_repair_fork', {
        'contract_id': cid, 'server': 'hpc', 'source_job': 'si-arr', 'phase': 'convergence',
        'strategy': 'fresh_restart', 'resources': repaired_res, **_diag(failure_category='array_failure'),
    })
    assert res['ok'] is True, res
    out = res['result']
    assert out['job_name'] == 'si-arr_fix1'
    assert out['target_phase'] == 'convergence'
    assert out['prepared'] is True and out['submitted'] is False
    assert out['physics_confirmed'] is True
    assert out['physics']['confirmed_by'] == 'operator_slash_command'
    assert out['monitor_deactivated'] is True
    assert out['resources_applied'] is True
    assert out['resources']['nodes'] == 2 and out['resources']['partition'] == 'highmem'
    assert out['ledger']['repair_retries_used'] == 1
    assert out['ledger']['consecutive_failures'] == 1
    assert 'si-arr_fix1' in out['ledger']['allowed_jobs']

    ledger = task_contract.read_ledger(ws, cid)
    assert len(ledger['diagnostics']) == 1
    d = ledger['diagnostics'][0]
    assert d['kind'] == 'repair' and d['failure_category'] == 'array_failure' and d['evidence_sha256'] == EVIDENCE_SHA
    assert d['repair_hypothesis'] and d['applied_change']

    # New directory requires fresh review before vasp_submit_stage succeeds.
    _record_green_review(cluster.jobs_dir / 'si-arr_fix1', 'convergence')
    sub = maker.call('vasp_submit_stage', {
        'contract_id': cid, 'server': 'hpc', 'job_name': 'si-arr_fix1', 'phase': 'convergence', 'resources': repaired_res,
    })
    assert sub['ok'] is True, sub
    assert sub['result']['job_id'] == '91001'


def test_fresh_restart_nbands_increase_and_preserves_tighter_scan_retry(tmp_path, monkeypatch):
    ws = _setup_workspace(tmp_path)
    maker, contract = _draft_and_approve(ws, monkeypatch, server=None, job_name='si-nb')
    cid = contract['contract_id']
    assert maker.call('vasp_prepare_task', {'contract_id': cid})['ok'] is True

    # Create a real tighter scan-retry child directory via convergence_reuse.prepare_retry.
    src_dir = ws / 'runs/si-nb'
    retry_dir = ws / 'runs/si-nb_r1'
    with contextlib.redirect_stdout(io.StringIO()):
        convergence_reuse.prepare_retry(src_dir, retry_dir, 'both', [460.0, 520.0, 580.0], [[4, 4, 4], [6, 6, 6], [8, 8, 8]], scan_preset='standard')
    stage_gate.confirm_physics(retry_dir, contract['physics_assertion']['goal'], 'operator_slash_command')
    task_contract.register_retry(ws, cid, kind='scan', new_job_name='si-nb_r1', diagnostic={'seed': True})
    core.save(retry_dir / 'workflow-state.json', {'status': 'failed', 'stage': 'scf'})
    core.save(retry_dir / 'checkpoint.json', {'completed': ['convergence', 'relaxation'], 'next': 'scf'})

    # Empty bands insufficient requires strictly larger nbands.
    bad = maker.call('vasp_prepare_repair_fork', {
        'contract_id': cid, 'source_job': 'si-nb_r1', 'phase': 'scf', 'strategy': 'fresh_restart',
        'nbands': SCF_NBANDS, **_diag(failure_category='empty_bands_insufficient'),
    })
    assert bad['ok'] is False and bad['error_code'] == 'CONTRACT_SCHEMA_INVALID'

    # Downgrading nbands below source is always rejected.
    lower = maker.call('vasp_prepare_repair_fork', {
        'contract_id': cid, 'source_job': 'si-nb_r1', 'phase': 'scf', 'strategy': 'fresh_restart',
        'nbands': SCF_NBANDS - 2, **_diag(failure_category='vasp_crash'),
    })
    assert lower['ok'] is False and lower['error_code'] == 'CONTRACT_SCHEMA_INVALID'

    ok = maker.call('vasp_prepare_repair_fork', {
        'contract_id': cid, 'source_job': 'si-nb_r1', 'phase': 'scf', 'strategy': 'fresh_restart',
        'nbands': 28, **_diag(failure_category='empty_bands_insufficient', applied_change='将 nbands 从 16 提升至 28'),
    })
    assert ok['ok'] is True, ok
    new_cfg = json.loads((ws / 'runs/si-nb_r1_fix1/config.json').read_text(encoding='utf-8'))
    assert new_cfg['nbands'] == 28
    assert new_cfg['encuts'] == [460.0, 520.0, 580.0]


def test_production_from_convergence_clean_source_and_check_idle_refusal(tmp_path, monkeypatch):
    ws = _setup_workspace(tmp_path)
    maker, contract = _draft_and_approve(ws, monkeypatch, server=None, job_name='si-conv')
    cid = contract['contract_id']
    assert maker.call('vasp_prepare_task', {'contract_id': cid})['ok'] is True
    conv_dir = ws / 'runs/si-conv'
    _complete_local_through(conv_dir, 'convergence', monkeypatch)

    # First fork a relaxation job from clean convergence (simulate failed relaxation fork).
    snap = stage_gate.context(conv_dir)['snapshot']
    with contextlib.redirect_stdout(io.StringIO()):
        convergence_reuse.prepare_production(conv_dir, ws / 'runs/si-conv_fix1', [6, 6, 6], snap)
    task_contract.register_retry(ws, cid, kind='repair', new_job_name='si-conv_fix1', diagnostic={'seed': True})
    failed_dir = ws / 'runs/si-conv_fix1'
    (failed_dir / 'relax-1').mkdir()
    (failed_dir / 'submissions').mkdir()
    core.save(failed_dir / 'submissions/relaxation.json', {'status': 'submitted', 'job_id': '8100', 'phase': 'relaxation'})
    core.save(failed_dir / 'workflow-state.json', {'status': 'failed', 'stage': 'relax-1'})

    # Attempting production_from_convergence with dirty source (`si-conv_fix1` has relax-1 + receipt) is refused cleanly.
    dirty = maker.call('vasp_prepare_repair_fork', {
        'contract_id': cid, 'source_job': 'si-conv_fix1', 'phase': 'relaxation',
        'strategy': 'production_from_convergence', **_diag(),
    })
    assert dirty['ok'] is False and dirty['error_code'] == 'REPAIR_FORK_NOT_ELIGIBLE'
    assert 'prepare_production' in dirty['error'] or 'check_idle' in dirty['error']

    # Forking from clean convergence source (`si-conv`) while attributing failure to `failed_job='si-conv_fix1'` succeeds!
    res = maker.call('vasp_prepare_repair_fork', {
        'contract_id': cid, 'source_job': 'si-conv', 'failed_job': 'si-conv_fix1', 'phase': 'relaxation',
        'strategy': 'production_from_convergence', 'production_mesh': [6, 6, 6],
        'resources': {'submission_mode': 'allocation', 'parallel_tests': 1, 'cores_per_test': 8,
                      'nodes': 2, 'partition': 'highmem', 'walltime': '04:00:00'},
        **_diag(),
    })
    assert res['ok'] is True, res
    out = res['result']
    assert out['job_name'] == 'si-conv_fix2'
    assert out['target_phase'] == 'relaxation'
    assert out['physics_confirmed'] is True
    assert out['applied_parameters']['production_mesh'] == [6, 6, 6]
    assert (ws / 'runs/si-conv_fix2/production-choice.json').is_file()
    assert json.loads((ws / 'runs/si-conv_fix2/checkpoint.json').read_text(encoding='utf-8'))['completed'] == ['convergence']


def test_bands_from_scf_local_and_2d_refusal(tmp_path, monkeypatch):
    ws = _setup_workspace(tmp_path)
    maker, contract = _draft_and_approve(ws, monkeypatch, server=None, job_name='si-bnd', band_path='Γ-X-W-K-Γ')
    cid = contract['contract_id']
    assert maker.call('vasp_prepare_task', {'contract_id': cid})['ok'] is True
    folder = ws / 'runs/si-bnd'
    _complete_local_through(folder, 'scf', monkeypatch)
    (folder / 'submissions').mkdir(exist_ok=True)
    core.save(folder / 'submissions/bands.json', {'status': 'submitted', 'job_id': '8200', 'phase': 'bands'})
    core.save(folder / 'workflow-state.json', {'status': 'failed', 'stage': 'bands'})

    res = maker.call('vasp_prepare_repair_fork', {
        'contract_id': cid, 'source_job': 'si-bnd', 'phase': 'bands', 'strategy': 'bands_from_scf',
        'resources': {'submission_mode': 'allocation', 'parallel_tests': 1, 'cores_per_test': 8,
                      'nodes': 2, 'partition': 'highmem', 'walltime': '03:00:00'},
        **_diag(),
    })
    assert res['ok'] is True, res
    out = res['result']
    assert out['job_name'] == 'si-bnd_fix1'
    assert out['target_phase'] == 'bands'
    assert out['physics_confirmed'] is True
    assert (ws / 'runs/si-bnd_fix1/bands-path-choice.json').is_file()
    ckpt = json.loads((ws / 'runs/si-bnd_fix1/checkpoint.json').read_text(encoding='utf-8'))
    assert 'scf' in ckpt['completed'] and ckpt['next'] == 'bands'

    # 2D slab must be refused for bands_from_scf.
    ws2 = _setup_workspace(tmp_path / 'slab2d', slab_2d=True)
    maker2, contract2 = _draft_and_approve(ws2, monkeypatch, server=None, job_name='si2d', slab_2d=True)
    assert maker2.call('vasp_prepare_task', {'contract_id': contract2['contract_id']})['ok'] is True
    bad2d = maker2.call('vasp_prepare_repair_fork', {
        'contract_id': contract2['contract_id'], 'source_job': 'si2d', 'phase': 'bands',
        'strategy': 'bands_from_scf', 'band_path': 'Γ-X', **_diag(),
    })
    assert bad2d['ok'] is False and bad2d['error_code'] == 'REPAIR_FORK_NOT_ELIGIBLE'


def test_dos_from_scf_empty_bands_repair_and_blocked_guard(tmp_path, monkeypatch):
    ws = _setup_workspace(tmp_path)
    maker, contract = _draft_and_approve(ws, monkeypatch, server=None, job_name='si-dos', include_dos=True)
    cid = contract['contract_id']
    assert maker.call('vasp_prepare_task', {'contract_id': cid})['ok'] is True
    folder = ws / 'runs/si-dos'
    # SCF top band only reaches 4.0 eV (< default window_max 5.0 + margin 1.0 = 6.0 eV).
    _complete_local_through(folder, 'scf', monkeypatch, scf_top_ev=4.0)
    (folder / 'submissions').mkdir(exist_ok=True)
    core.save(folder / 'submissions/dos.json', {'status': 'submitted', 'job_id': '8300', 'phase': 'dos'})
    core.save(folder / 'workflow-state.json', {'status': 'failed', 'stage': 'dos'})

    # Keeping default nbands=None (SCF 16 bands) triggers deterministic engine block without consuming budget.
    blocked = maker.call('vasp_prepare_repair_fork', {
        'contract_id': cid, 'source_job': 'si-dos', 'phase': 'dos', 'strategy': 'dos_from_scf',
        **_diag(failure_category='empty_bands_insufficient'),
    })
    assert blocked['ok'] is False and blocked['error_code'] == 'REPAIR_FORK_BLOCKED'
    assert task_contract.read_ledger(ws, cid)['repair_retries_used'] == 0
    assert not (ws / 'runs/si-dos_fix1').exists()

    # Raising dos_values.nbands to 32 clears the pre-run block and creates si-dos_fix1.
    ok = maker.call('vasp_prepare_repair_fork', {
        'contract_id': cid, 'source_job': 'si-dos', 'phase': 'dos', 'strategy': 'dos_from_scf',
        'dos_values': {'nbands': 32},
        **_diag(failure_category='empty_bands_insufficient', applied_change='在 DOS 派生中将 nbands 提升至 32'),
    })
    assert ok['ok'] is True, ok
    out = ok['result']
    assert out['job_name'] == 'si-dos_fix1' and out['target_phase'] == 'dos'
    assert out['applied_parameters']['dos_values']['nbands'] == 32
    assert (ws / 'runs/si-dos_fix1/dos-fork.json').is_file()


def test_incar_numerical_unsupported_auto_halts_without_creating_dir(tmp_path, monkeypatch):
    ws = _setup_workspace(tmp_path)
    maker, contract = _draft_and_approve(ws, monkeypatch, server=None, job_name='si-alg')
    cid = contract['contract_id']
    assert maker.call('vasp_prepare_task', {'contract_id': cid})['ok'] is True

    res = maker.call('vasp_prepare_repair_fork', {
        'contract_id': cid, 'source_job': 'si-alg', 'phase': 'scf', 'strategy': 'fresh_restart',
        **_diag(
            failure_category='incar_numerical_unsupported',
            repair_hypothesis='SCF 电子步振荡，需要修改 ALGO=All 与 AMIX=0.1',
            applied_change='冻结引擎未暴露 ALGO/AMIX 恢复档位，触发硬熔断交人工',
        ),
    })
    assert res['ok'] is False
    assert res['error_code'] == 'REPAIR_UNSUPPORTED_INCAR_RECOVERY'
    assert res['recoverable'] is False
    assert not (ws / 'runs/si-alg_fix1').exists()
    ledger = task_contract.read_ledger(ws, cid)
    assert ledger['status'] == 'halted'
    assert ledger['halt_code'] == 'REPAIR_UNSUPPORTED_INCAR_RECOVERY'


def test_multi_round_consecutive_failures_and_repair_budget_auto_halt(tmp_path, monkeypatch):
    # Part 1: consecutive_failures ceiling halts BEFORE creating orphan directory, deduplicating same JobID.
    ws = _setup_workspace(tmp_path / 'cf')
    maker, contract = _draft_and_approve(
        ws, monkeypatch, server=None, job_name='si-cf',
        budget={'max_submissions': 10, 'max_resume_retries': 2, 'max_repair_retries': 5, 'max_scan_retries': 1, 'max_consecutive_failures': 3},
    )
    cid = contract['contract_id']
    assert maker.call('vasp_prepare_task', {'contract_id': cid})['ok'] is True

    def _fail(job: str, job_id: str) -> None:
        folder = ws / 'runs' / job
        (folder / 'submissions').mkdir(exist_ok=True)
        core.save(folder / 'submissions/convergence.json', {'status': 'submitted', 'job_id': job_id, 'phase': 'convergence'})
        core.save(folder / 'workflow-state.json', {'status': 'failed', 'stage': 'convergence'})

    _fail('si-cf', '9001')
    # Pre-recorded external phase_failed (no repair fork yet) must not be double-counted.
    task_contract.record_phase_outcome(ws, cid, job_name='si-cf', phase='convergence', passed=False, failure_info={'job_id': '9001'})
    r1 = maker.call('vasp_prepare_repair_fork', {'contract_id': cid, 'source_job': 'si-cf', 'phase': 'convergence', 'strategy': 'fresh_restart', **_diag()})
    assert r1['ok'] is True and r1['result']['job_name'] == 'si-cf_fix1'
    assert r1['result']['ledger']['consecutive_failures'] == 1

    # Replaying the exact same already-repaired `(si-cf, convergence, 9001)` MUST be rejected (prevents consecutive_failures bypass!).
    r1_replay = maker.call('vasp_prepare_repair_fork', {'contract_id': cid, 'source_job': 'si-cf', 'phase': 'convergence', 'strategy': 'fresh_restart', **_diag()})
    assert r1_replay['ok'] is False and r1_replay['error_code'] == 'REPAIR_FORK_ALREADY_HANDLED'
    assert not (ws / 'runs/si-cf_fix1_2').exists()

    _fail('si-cf_fix1', '9002')
    r2 = maker.call('vasp_prepare_repair_fork', {'contract_id': cid, 'source_job': 'si-cf_fix1', 'phase': 'convergence', 'strategy': 'fresh_restart', **_diag()})
    assert r2['ok'] is True and r2['result']['job_name'] == 'si-cf_fix2'
    assert r2['result']['ledger']['consecutive_failures'] == 2

    _fail('si-cf_fix2', '9003')
    r3 = maker.call('vasp_prepare_repair_fork', {'contract_id': cid, 'source_job': 'si-cf_fix2', 'phase': 'convergence', 'strategy': 'fresh_restart', **_diag()})
    assert r3['ok'] is False and r3['error_code'] == 'CONTRACT_CONSECUTIVE_FAILURES_EXCEEDED'
    assert not (ws / 'runs/si-cf_fix3').exists()
    ledger = task_contract.read_ledger(ws, cid)
    assert ledger['status'] == 'halted' and ledger['consecutive_failures'] == 3
    assert any(d.get('halted') is True and d.get('failed_job') == 'si-cf_fix2' for d in ledger['diagnostics'])

    # Part 2: max_repair_retries ceiling halts before creating directory and retains halted diagnostic.
    ws2 = _setup_workspace(tmp_path / 'rb')
    maker2, contract2 = _draft_and_approve(
        ws2, monkeypatch, server=None, job_name='si-rb',
        budget={'max_submissions': 10, 'max_resume_retries': 2, 'max_repair_retries': 1, 'max_scan_retries': 1, 'max_consecutive_failures': 5},
    )
    cid2 = contract2['contract_id']
    assert maker2.call('vasp_prepare_task', {'contract_id': cid2})['ok'] is True
    (ws2 / 'runs/si-rb/submissions').mkdir(exist_ok=True)
    core.save(ws2 / 'runs/si-rb/submissions/convergence.json', {'status': 'submitted', 'job_id': '9101', 'phase': 'convergence'})
    assert maker2.call('vasp_prepare_repair_fork', {'contract_id': cid2, 'source_job': 'si-rb', 'phase': 'convergence', 'strategy': 'fresh_restart', **_diag()})['ok'] is True
    (ws2 / 'runs/si-rb_fix1/submissions').mkdir(exist_ok=True)
    core.save(ws2 / 'runs/si-rb_fix1/submissions/convergence.json', {'status': 'submitted', 'job_id': '9102', 'phase': 'convergence'})
    exhausted = maker2.call('vasp_prepare_repair_fork', {'contract_id': cid2, 'source_job': 'si-rb_fix1', 'phase': 'convergence', 'strategy': 'fresh_restart', **_diag()})
    assert exhausted['ok'] is False and exhausted['error_code'] == 'CONTRACT_BUDGET_EXHAUSTED'
    assert not (ws2 / 'runs/si-rb_fix2').exists()
    l2 = task_contract.read_ledger(ws2, cid2)
    assert l2['status'] == 'halted' and any(d.get('halted') is True for d in l2['diagnostics'])


def test_preflight_capacity_conflict_and_unsubmitted_eligibility_guards(tmp_path, monkeypatch):
    ws = _setup_workspace(tmp_path)
    maker, contract = _draft_and_approve(ws, monkeypatch, server=None, job_name='si-cap')
    cid = contract['contract_id']
    assert maker.call('vasp_prepare_task', {'contract_id': cid})['ok'] is True

    # Unsubmitted non-failed task with oom is refused.
    no_fail = maker.call('vasp_prepare_repair_fork', {
        'contract_id': cid, 'source_job': 'si-cap', 'phase': 'convergence', 'strategy': 'fresh_restart', **_diag(failure_category='oom'),
    })
    assert no_fail['ok'] is False and no_fail['error_code'] == 'REPAIR_FORK_NOT_ELIGIBLE'

    # capacity_conflict without explicit resources is refused.
    no_res = maker.call('vasp_prepare_repair_fork', {
        'contract_id': cid, 'source_job': 'si-cap', 'phase': 'convergence', 'strategy': 'fresh_restart', **_diag(failure_category='capacity_conflict'),
    })
    assert no_res['ok'] is False and no_res['error_code'] == 'REPAIR_FORK_NOT_ELIGIBLE'

    # capacity_conflict WITH explicit repaired resources succeeds even before submission.
    ok = maker.call('vasp_prepare_repair_fork', {
        'contract_id': cid, 'source_job': 'si-cap', 'phase': 'convergence', 'strategy': 'fresh_restart',
        'resources': {'submission_mode': 'allocation', 'parallel_tests': 1, 'cores_per_test': 8,
                      'nodes': 2, 'partition': 'highmem', 'walltime': '02:00:00'},
        **_diag(failure_category='capacity_conflict'),
    })
    assert ok['ok'] is True, ok
    assert ok['result']['job_name'] == 'si-cap_fix1'


def test_security_guards_running_uncertain_passed_scope_envelope_and_drift(tmp_path, monkeypatch):
    ws = _setup_workspace(tmp_path)
    cluster = _FakeRemoteRepairCluster(ws, tmp_path / 'remote')
    _install_remote(monkeypatch, ws, cluster)
    maker, contract = _draft_and_approve(ws, monkeypatch, server='hpc', job_name='si-sec')
    cid = contract['contract_id']
    assert maker.call('vasp_prepare_task', {'contract_id': cid})['ok'] is True
    src = cluster.jobs_dir / 'si-sec'

    # 1. Extra forbidden field (`scan_thresholds` or `incar`) rejected by strict schema.
    extra = maker.call('vasp_prepare_repair_fork', {
        'contract_id': cid, 'server': 'hpc', 'source_job': 'si-sec', 'phase': 'convergence',
        'strategy': 'fresh_restart', 'incar': {'ALGO': 'All'}, **_diag(),
    })
    assert extra['ok'] is False

    # 2. Scope violation (unregistered failed_job / wrong server).
    bad_scope = maker.call('vasp_prepare_repair_fork', {
        'contract_id': cid, 'server': 'hpc', 'source_job': 'si-sec', 'failed_job': 'other-job',
        'phase': 'convergence', 'strategy': 'fresh_restart', **_diag(),
    })
    assert bad_scope['ok'] is False and bad_scope['error_code'] == 'CONTRACT_SCOPE_VIOLATION'

    # 3. Queued job -> REPAIR_FORK_SOURCE_RUNNING.
    cluster.status_overrides['si-sec'] = {'queue': '7100|RUNNING|00:05:00|node01'}
    running = maker.call('vasp_prepare_repair_fork', {
        'contract_id': cid, 'server': 'hpc', 'source_job': 'si-sec', 'phase': 'convergence', 'strategy': 'fresh_restart', **_diag(),
    })
    assert running['ok'] is False and running['error_code'] == 'REPAIR_FORK_SOURCE_RUNNING'

    # 4. Unknown scheduler status -> REPAIR_FORK_SOURCE_UNCERTAIN.
    cluster.status_overrides['si-sec'] = {'queue': None, 'scheduler_status_known': False}
    unk = maker.call('vasp_prepare_repair_fork', {
        'contract_id': cid, 'server': 'hpc', 'source_job': 'si-sec', 'phase': 'convergence', 'strategy': 'fresh_restart', **_diag(),
    })
    assert unk['ok'] is False and unk['error_code'] == 'REPAIR_FORK_SOURCE_UNCERTAIN'

    # 5. submission_uncertain receipt -> REPAIR_FORK_SOURCE_UNCERTAIN.
    cluster.status_overrides['si-sec'] = {}
    (src / 'submissions').mkdir(exist_ok=True)
    core.save(src / 'submissions/convergence.json', {'status': 'submission_uncertain', 'job_id': None, 'phase': 'convergence'})
    unc = maker.call('vasp_prepare_repair_fork', {
        'contract_id': cid, 'server': 'hpc', 'source_job': 'si-sec', 'phase': 'convergence', 'strategy': 'fresh_restart', **_diag(),
    })
    assert unc['ok'] is False and unc['error_code'] == 'REPAIR_FORK_SOURCE_UNCERTAIN'

    # 6. Already passed phase -> REPAIR_FORK_ALREADY_PASSED.
    core.save(src / 'submissions/convergence.json', {'status': 'submitted', 'job_id': '7100', 'phase': 'convergence'})
    core.save(src / 'checkpoint.json', {'completed': ['convergence'], 'next': 'relaxation'})
    passed = maker.call('vasp_prepare_repair_fork', {
        'contract_id': cid, 'server': 'hpc', 'source_job': 'si-sec', 'phase': 'convergence', 'strategy': 'fresh_restart', **_diag(),
    })
    assert passed['ok'] is False and passed['error_code'] == 'REPAIR_FORK_ALREADY_PASSED'

    # 7. Resource envelope violation (partition outside allowed_partitions).
    (src / 'checkpoint.json').unlink()
    env_bad = maker.call('vasp_prepare_repair_fork', {
        'contract_id': cid, 'server': 'hpc', 'source_job': 'si-sec', 'phase': 'convergence', 'strategy': 'fresh_restart',
        'resources': {'submission_mode': 'allocation', 'parallel_tests': 1, 'cores_per_test': 8,
                      'nodes': 1, 'partition': 'gpu', 'walltime': '02:00:00'},
        **_diag(),
    })
    assert env_bad['ok'] is False and env_bad['error_code'] == 'CONTRACT_RESOURCE_ENVELOPE_EXCEEDED'

    # 8. Tampered physics-model.json triggers hard CONTRACT_TAMPERED halt!
    phys = json.loads((src / 'physics-model.json').read_text(encoding='utf-8'))
    phys['goal'] = 'tampered magnetic goal'
    core.save(src / 'physics-model.json', phys)
    tampered = maker.call('vasp_prepare_repair_fork', {
        'contract_id': cid, 'server': 'hpc', 'source_job': 'si-sec', 'phase': 'convergence', 'strategy': 'fresh_restart', **_diag(),
    })
    assert tampered['ok'] is False and tampered['error_code'] == 'CONTRACT_TAMPERED'
    assert task_contract.read_ledger(ws, cid)['status'] == 'halted'


def test_remote_disconnect_reconciliation_missing_recovered_and_uncertain(tmp_path, monkeypatch):
    ws = _setup_workspace(tmp_path)
    cluster = _FakeRemoteRepairCluster(ws, tmp_path / 'remote')
    _install_remote(monkeypatch, ws, cluster)
    maker, contract = _draft_and_approve(ws, monkeypatch, server='hpc', job_name='si-dc')
    cid = contract['contract_id']
    assert maker.call('vasp_prepare_task', {'contract_id': cid})['ok'] is True
    src = cluster.jobs_dir / 'si-dc'
    (src / 'submissions').mkdir(exist_ok=True)
    core.save(src / 'submissions/convergence.json', {'status': 'submitted', 'job_id': '7200', 'phase': 'convergence'})

    # Case 1: Disconnect before creation -> REPAIR_FORK_DISCONNECTED_NOT_CREATED, budget untouched.
    cluster.disconnect_on['prepare'] = (core.ToolError('REMOTE_TIMEOUT', 'ssh dropped'), False)
    r_miss = maker.call('vasp_prepare_repair_fork', {
        'contract_id': cid, 'server': 'hpc', 'source_job': 'si-dc', 'phase': 'convergence', 'strategy': 'fresh_restart', **_diag(),
    })
    assert r_miss['ok'] is False and r_miss['error_code'] == 'REPAIR_FORK_DISCONNECTED_NOT_CREATED'
    assert task_contract.read_ledger(ws, cid)['repair_retries_used'] == 0

    # Case 2: Disconnect AFTER creation -> read-only reconciliation recovers cleanly!
    cluster.disconnect_on['prepare'] = (core.ToolError('SSH_FAILED', 'connection reset'), True)
    r_rec = maker.call('vasp_prepare_repair_fork', {
        'contract_id': cid, 'server': 'hpc', 'source_job': 'si-dc', 'phase': 'convergence', 'strategy': 'fresh_restart', **_diag(),
    })
    assert r_rec['ok'] is True, r_rec
    assert r_rec['result']['recovered'] is True
    assert r_rec['result']['job_name'] == 'si-dc_fix1'
    assert task_contract.read_ledger(ws, cid)['repair_retries_used'] == 1

    # Case 3: Disconnect where candidate dir exists with mismatched nbands -> REPAIR_FORK_RESULT_UNCERTAIN.
    (cluster.jobs_dir / 'si-dc_fix1/submissions').mkdir(exist_ok=True)
    core.save(cluster.jobs_dir / 'si-dc_fix1/submissions/convergence.json', {'status': 'submitted', 'job_id': '7201', 'phase': 'convergence'})
    cluster._prepare_folder(
        cluster.jobs_dir / 'si-dc_fix2', 'si-dc_fix2', 'structures/Si.POSCAR',
        encut=400, mesh=[4, 4, 4], nbands=99, relax_mode='cell', scan_preset='standard',
    )
    # Remove from proactive listing so candidate_job resolves to si-dc_fix2 and hits disconnect reconcile.
    orig_call = cluster.call

    def _hide_fix2(action, job_name=None, **params):
        if action == 'list_jobs':
            return {'jobs': [{'name': 'si-dc'}, {'name': 'si-dc_fix1'}]}
        return orig_call(action, job_name, **params)

    monkeypatch.setattr(cluster, 'call', _hide_fix2)
    cluster.disconnect_on['prepare'] = (core.ToolError('REMOTE_RESPONSE_INVALID', 'truncated'), False)
    r_unc = maker.call('vasp_prepare_repair_fork', {
        'contract_id': cid, 'server': 'hpc', 'source_job': 'si-dc_fix1', 'phase': 'convergence', 'strategy': 'fresh_restart', **_diag(),
    })
    assert r_unc['ok'] is False and r_unc['error_code'] == 'REPAIR_FORK_RESULT_UNCERTAIN'
    assert 'si-dc_fix2' not in task_contract.read_ledger(ws, cid)['allowed_jobs']


def test_environment_profile_drift_halts_repair_fork(tmp_path, monkeypatch):
    ws = _setup_workspace(tmp_path)
    cluster = _FakeRemoteRepairCluster(ws, tmp_path / 'remote')
    _install_remote(monkeypatch, ws, cluster)
    prof = {
        'name': 'vasp544', 'env_script': '/opt/vasp/env.sh', 'executable': '/opt/vasp/vasp_std',
        'environment_sha256': cluster.env['environment_sha256'], 'executable_sha256': cluster.env['executable_sha256'],
        'capabilities': ['3d', 'cell', 'ions', 'dos'], 'relax_modes': ['cell', 'ions'],
    }
    monkeypatch.setattr(environment_profiles, 'registered', lambda _ws, _srv: True)
    monkeypatch.setattr(environment_profiles, 'choose', lambda _ws, _srv, _rm, _req=None: {'status': 'one', 'matches': [prof], 'required': [], 'excluded': []})
    monkeypatch.setattr(environment_profiles, 'by_name', lambda _ws, _srv, _name: dict(prof))
    monkeypatch.setattr(environment_profiles, 'payload', lambda row: dict(row))

    maker, contract = _draft_and_approve(ws, monkeypatch, server='hpc', job_name='si-env')
    cid = contract['contract_id']
    assert maker.call('vasp_prepare_task', {'contract_id': cid})['ok'] is True

    # Mutate environment profile hash -> verify_healing_contract halts with ENVIRONMENT_PROFILE_DRIFT.
    drifted = {**prof, 'executable_sha256': '9' * 64}
    monkeypatch.setattr(environment_profiles, 'by_name', lambda _ws, _srv, _name: drifted)
    res = maker.call('vasp_prepare_repair_fork', {
        'contract_id': cid, 'server': 'hpc', 'source_job': 'si-env', 'phase': 'convergence', 'strategy': 'fresh_restart', **_diag(),
    })
    assert res['ok'] is False and res['error_code'] == 'ENVIRONMENT_PROFILE_DRIFT'
    assert task_contract.read_ledger(ws, cid)['status'] == 'halted'


def test_repair_fork_naming_conflict_and_64_char_limit(tmp_path, monkeypatch):
    ws = _setup_workspace(tmp_path)
    long_name = 's' * 61
    maker, contract = _draft_and_approve(ws, monkeypatch, server=None, job_name=long_name)
    cid = contract['contract_id']
    assert maker.call('vasp_prepare_task', {'contract_id': cid})['ok'] is True
    src = ws / 'runs' / long_name
    (src / 'submissions').mkdir(exist_ok=True)
    core.save(src / 'submissions/convergence.json', {'status': 'submitted', 'job_id': '9500', 'phase': 'convergence'})

    expected_base = 's' * 59 + '_fix1'
    assert len(expected_base) == 64
    (ws / 'runs' / expected_base).mkdir()

    res = maker.call('vasp_prepare_repair_fork', {
        'contract_id': cid, 'source_job': long_name, 'phase': 'convergence', 'strategy': 'fresh_restart', **_diag(),
    })
    assert res['ok'] is True, res
    new_name = res['result']['job_name']
    assert len(new_name) <= 64 and new_name.endswith('_2')
    assert res['result']['name_conflicted'] is True


def test_repair_fork_real_symlink_refused(tmp_path, monkeypatch):
    probe_target = tmp_path / 'probe_target'
    probe_target.mkdir()
    probe_link = tmp_path / 'probe_link'
    try:
        os.symlink(probe_target, probe_link, target_is_directory=True)
    except (OSError, NotImplementedError):
        pytest.skip('OS/privilege does not allow creating real symlinks')
    probe_link.unlink()

    ws = _setup_workspace(tmp_path / 'ws')
    maker, contract = _draft_and_approve(ws, monkeypatch, server=None, job_name='si-sym')
    cid = contract['contract_id']
    outside = tmp_path / 'outside_job'
    outside.mkdir()
    (ws / 'runs').mkdir(exist_ok=True)
    os.symlink(outside, ws / 'runs/si-sym', target_is_directory=True)

    res = maker.call('vasp_prepare_repair_fork', {
        'contract_id': cid, 'source_job': 'si-sym', 'phase': 'convergence', 'strategy': 'fresh_restart', **_diag(),
    })
    assert res['ok'] is False and res['error_code'] == 'CONTRACT_SYMLINK_REFUSED'


@pytest.mark.parametrize('symlink_suffix', [
    'runs/si-msym',
    'runs/si-msym/config.json',
    'runs/si-msym/submissions/convergence.json',
    'runs/si-msym/manifest.json',
    'runs/si-msym_fix1',
])
def test_repair_fork_mock_symlinks_refused(tmp_path, monkeypatch, symlink_suffix):
    ws = _setup_workspace(tmp_path)
    maker, contract = _draft_and_approve(ws, monkeypatch, server=None, job_name='si-msym')
    cid = contract['contract_id']
    assert maker.call('vasp_prepare_task', {'contract_id': cid})['ok'] is True
    src = ws / 'runs/si-msym'
    (src / 'submissions').mkdir(exist_ok=True)
    core.save(src / 'submissions/convergence.json', {'status': 'submitted', 'job_id': '9600', 'phase': 'convergence'})

    target_rel = Path(symlink_suffix)
    ws_resolved = ws.resolve()
    real_is_symlink = Path.is_symlink

    def fake_is_symlink(self: Path) -> bool:
        try:
            if self.relative_to(ws_resolved) == target_rel:
                return True
        except ValueError:
            pass
        return real_is_symlink(self)

    monkeypatch.setattr(Path, 'is_symlink', fake_is_symlink)
    res = maker.call('vasp_prepare_repair_fork', {
        'contract_id': cid, 'source_job': 'si-msym', 'phase': 'convergence', 'strategy': 'fresh_restart', **_diag(),
    })
    assert res['ok'] is False and res['error_code'] == 'CONTRACT_SYMLINK_REFUSED'
    assert hashlib.sha256((ws / 'structures/Si.POSCAR').read_bytes()).hexdigest() == contract['scope']['structure_sha256']


def test_dos_values_monotonic_allowlist_and_precision_bounds(tmp_path, monkeypatch):
    ws = _setup_workspace(tmp_path)
    maker, contract = _draft_and_approve(
        ws, monkeypatch, server=None, job_name='si-dosg', include_dos=True,
        precision_bounds={'max_encut': 700, 'max_kmesh_axis': 16, 'max_kmesh_points': 4096},
    )
    cid = contract['contract_id']
    assert maker.call('vasp_prepare_task', {'contract_id': cid})['ok'] is True
    folder = ws / 'runs/si-dosg'
    _complete_local_through(folder, 'scf', monkeypatch, scf_top_ev=8.0)
    (folder / 'submissions').mkdir(exist_ok=True)
    core.save(folder / 'submissions/dos.json', {'status': 'submitted', 'job_id': '8310', 'phase': 'dos'})
    core.save(folder / 'workflow-state.json', {'status': 'failed', 'stage': 'dos'})

    for bad_dos in (
        {'integration': 'gaussian', 'sigma_eV': 0.05},
        {'window_max_eV': 1.0},
        {'empty_band_margin_eV': 0.1},
        {'sampling_interval_eV': 0.05},
        {'kmesh_factor': 1},
        {'nbands': 12},
    ):
        r = maker.call('vasp_prepare_repair_fork', {
            'contract_id': cid, 'source_job': 'si-dosg', 'phase': 'dos', 'strategy': 'dos_from_scf',
            'dos_values': bad_dos, **_diag(),
        })
        assert r['ok'] is False and r['error_code'] == 'CONTRACT_DOS_RELAXATION_FORBIDDEN', (bad_dos, r)

    # Raising kmesh_factor to 4 on [6,6,6] -> [24,24,24] > max_kmesh_axis=16 must hit precision_bounds.
    over_bounds = maker.call('vasp_prepare_repair_fork', {
        'contract_id': cid, 'source_job': 'si-dosg', 'phase': 'dos', 'strategy': 'dos_from_scf',
        'dos_values': {'kmesh_factor': 4}, **_diag(),
    })
    assert over_bounds['ok'] is False and over_bounds['error_code'] == 'CONTRACT_PRECISION_BOUND_EXCEEDED'


def test_cross_job_provenance_and_fresh_restart_failed_job_guards(tmp_path, monkeypatch):
    ws = _setup_workspace(tmp_path)
    maker, contract = _draft_and_approve(ws, monkeypatch, server=None, job_name='si-anc')
    cid = contract['contract_id']
    assert maker.call('vasp_prepare_task', {'contract_id': cid})['ok'] is True
    conv_dir = ws / 'runs/si-anc'
    _complete_local_through(conv_dir, 'convergence', monkeypatch)

    snap = stage_gate.context(conv_dir)['snapshot']
    with contextlib.redirect_stdout(io.StringIO()):
        convergence_reuse.prepare_production(conv_dir, ws / 'runs/si-anc_fix1', [6, 6, 6], snap)
    task_contract.register_retry(ws, cid, kind='repair', new_job_name='si-anc_fix1', diagnostic={'seed': True})
    failed_dir = ws / 'runs/si-anc_fix1'
    (failed_dir / 'submissions').mkdir()
    core.save(failed_dir / 'submissions/relaxation.json', {'status': 'submitted', 'job_id': '8400', 'phase': 'relaxation'})

    # 1. fresh_restart rejects failed_job != source_job.
    bad_fresh = maker.call('vasp_prepare_repair_fork', {
        'contract_id': cid, 'source_job': 'si-anc', 'failed_job': 'si-anc_fix1',
        'phase': 'relaxation', 'strategy': 'fresh_restart', **_diag(),
    })
    assert bad_fresh['ok'] is False and bad_fresh['error_code'] == 'CONTRACT_SCHEMA_INVALID'

    # 2. Tampered physics-model.json on failed_job triggers CONTRACT_TAMPERED halt!
    phys = json.loads((failed_dir / 'physics-model.json').read_text(encoding='utf-8'))
    phys['goal'] = 'tampered failed_job goal'
    core.save(failed_dir / 'physics-model.json', phys)
    tampered = maker.call('vasp_prepare_repair_fork', {
        'contract_id': cid, 'source_job': 'si-anc', 'failed_job': 'si-anc_fix1',
        'phase': 'relaxation', 'strategy': 'production_from_convergence', **_diag(),
    })
    assert tampered['ok'] is False and tampered['error_code'] == 'CONTRACT_TAMPERED'
    assert task_contract.read_ledger(ws, cid)['status'] == 'halted'


def test_eligibility_phase_mismatch_local_squeue_and_remote_unknown_scheduler(tmp_path, monkeypatch):
    # Part 1: capacity_conflict phase mismatch + local squeue PENDING / error guards.
    ws = _setup_workspace(tmp_path / 'loc')
    maker, contract = _draft_and_approve(ws, monkeypatch, server=None, job_name='si-elg')
    cid = contract['contract_id']
    assert maker.call('vasp_prepare_task', {'contract_id': cid})['ok'] is True

    wrong_phase = maker.call('vasp_prepare_repair_fork', {
        'contract_id': cid, 'source_job': 'si-elg', 'phase': 'bands', 'strategy': 'fresh_restart',
        'resources': {'submission_mode': 'allocation', 'parallel_tests': 1, 'cores_per_test': 8,
                      'nodes': 1, 'partition': 'highmem', 'walltime': '02:00:00'},
        **_diag(failure_category='capacity_conflict'),
    })
    assert wrong_phase['ok'] is False and wrong_phase['error_code'] == 'REPAIR_FORK_NOT_ELIGIBLE'

    (ws / 'runs/si-elg/submissions').mkdir(exist_ok=True)
    core.save(ws / 'runs/si-elg/submissions/convergence.json', {'status': 'submitted', 'job_id': '8501', 'phase': 'convergence'})
    monkeypatch.setattr(shutil, 'which', lambda name: '/usr/bin/squeue' if name == 'squeue' else None)
    monkeypatch.setattr(subprocess, 'run', lambda *a, **kw: SimpleNamespace(returncode=0, stdout='8501|PENDING\n', stderr=''))
    pending = maker.call('vasp_prepare_repair_fork', {
        'contract_id': cid, 'source_job': 'si-elg', 'phase': 'convergence', 'strategy': 'fresh_restart', **_diag(),
    })
    assert pending['ok'] is False and pending['error_code'] == 'REPAIR_FORK_SOURCE_RUNNING'

    monkeypatch.setattr(subprocess, 'run', lambda *a, **kw: SimpleNamespace(returncode=1, stdout='', stderr='slurm down'))
    sq_err = maker.call('vasp_prepare_repair_fork', {
        'contract_id': cid, 'source_job': 'si-elg', 'phase': 'convergence', 'strategy': 'fresh_restart', **_diag(),
    })
    assert sq_err['ok'] is False and sq_err['error_code'] == 'REPAIR_FORK_SOURCE_UNCERTAIN'

    # Part 2: Remote scheduler_status_known=None & mismatched remote-submission.json rejected.
    ws2 = _setup_workspace(tmp_path / 'rem')
    cluster = _FakeRemoteRepairCluster(ws2, tmp_path / 'remote')
    _install_remote(monkeypatch, ws2, cluster)
    maker2, contract2 = _draft_and_approve(ws2, monkeypatch, server='hpc', job_name='si-rem')
    cid2 = contract2['contract_id']
    assert maker2.call('vasp_prepare_task', {'contract_id': cid2})['ok'] is True
    (cluster.jobs_dir / 'si-rem/submissions').mkdir(exist_ok=True)
    core.save(cluster.jobs_dir / 'si-rem/submissions/convergence.json', {'status': 'submitted', 'job_id': '8601', 'phase': 'convergence'})

    cluster.status_overrides['si-rem'] = {'scheduler_status_known': None}
    none_sched = maker2.call('vasp_prepare_repair_fork', {
        'contract_id': cid2, 'server': 'hpc', 'source_job': 'si-rem', 'phase': 'convergence', 'strategy': 'fresh_restart', **_diag(),
    })
    assert none_sched['ok'] is False and none_sched['error_code'] == 'REPAIR_FORK_SOURCE_UNCERTAIN'

    cluster.status_overrides['si-rem'] = {'scheduler_status_known': True, 'remote-submission.json': {'job_id': '9999'}}
    mis_sub = maker2.call('vasp_prepare_repair_fork', {
        'contract_id': cid2, 'server': 'hpc', 'source_job': 'si-rem', 'phase': 'convergence', 'strategy': 'fresh_restart', **_diag(),
    })
    assert mis_sub['ok'] is False and mis_sub['error_code'] == 'REPAIR_FORK_SOURCE_UNCERTAIN'


def test_remote_disconnect_false_positives_nonempty_collision_potcar_drift_and_in_flight(tmp_path, monkeypatch):
    ws = _setup_workspace(tmp_path)
    cluster = _FakeRemoteRepairCluster(ws, tmp_path / 'remote')
    _install_remote(monkeypatch, ws, cluster)
    maker, contract = _draft_and_approve(
        ws, monkeypatch, server='hpc', job_name='si-adv', include_dos=True, band_path='Γ-X-W-K-Γ',
        budget={'max_submissions': 1, 'max_resume_retries': 2, 'max_repair_retries': 5, 'max_scan_retries': 1, 'max_consecutive_failures': 5},
    )
    cid = contract['contract_id']
    assert maker.call('vasp_prepare_task', {'contract_id': cid})['ok'] is True
    src = cluster.jobs_dir / 'si-adv'
    (src / 'submissions').mkdir(exist_ok=True)
    core.save(src / 'submissions/convergence.json', {'status': 'submitted', 'job_id': '8700', 'phase': 'convergence'})

    # 1. in_flight non-null is strictly refused.
    task_contract.reserve_submission(
        ws, cid, server='hpc', job_name='si-adv', phase='convergence',
        resources=contract['resource_envelope']['phase_defaults']['convergence'], operation_id='a' * 32,
    )
    in_fl = maker.call('vasp_prepare_repair_fork', {
        'contract_id': cid, 'server': 'hpc', 'source_job': 'si-adv', 'phase': 'convergence', 'strategy': 'fresh_restart', **_diag(),
    })
    assert in_fl['ok'] is False and in_fl['error_code'] == 'SUBMISSION_IN_FLIGHT'
    # Refund and release in_flight so we can test disconnect & collision.
    task_contract.release_in_flight(ws, cid, operation_id='a' * 32, refund=True, reason='test')

    # 2. Remote '工作目录非空' reactive collision increments cleanly to `_2`.
    (cluster.jobs_dir / 'si-adv_fix1').mkdir()
    (cluster.jobs_dir / 'si-adv_fix1/partial.txt').write_text('abandoned\n', encoding='utf-8', newline='\n')
    r_col = maker.call('vasp_prepare_repair_fork', {
        'contract_id': cid, 'server': 'hpc', 'source_job': 'si-adv', 'phase': 'convergence', 'strategy': 'fresh_restart', **_diag(),
    })
    assert r_col['ok'] is True, r_col
    assert r_col['result']['job_name'] == 'si-adv_fix1_2' and r_col['result']['name_conflicted'] is True

    # 3. Disconnect false-positive guard: existing dir that ALREADY has physics-model.json MUST be rejected as UNCERTAIN!
    f2_src = cluster.jobs_dir / 'si-adv_fix1_2'
    (f2_src / 'submissions').mkdir(exist_ok=True)
    core.save(f2_src / 'submissions/convergence.json', {'status': 'submitted', 'job_id': '8701', 'phase': 'convergence'})
    existing_dir = cluster.jobs_dir / 'si-adv_fix1_2_fix1'
    cluster._prepare_folder(
        existing_dir, 'si-adv_fix1_2_fix1', 'structures/Si.POSCAR',
        encut=400, mesh=[4, 4, 4], nbands=SCF_NBANDS, relax_mode='cell', scan_preset='standard',
        band_path='Γ-X-W-K-Γ', optional_stages=['dos'],
        stage_parameters=json.loads((f2_src / 'config.json').read_text(encoding='utf-8'))['stage_parameters'],
    )
    stage_gate.confirm_physics(existing_dir, contract['physics_assertion']['goal'], 'operator_slash_command')
    orig_call = cluster.call
    monkeypatch.setattr(cluster, 'call', lambda a, j=None, **kw: {'jobs': [{'name': 'si-adv'}, {'name': 'si-adv_fix1_2'}]} if a == 'list_jobs' else orig_call(a, j, **kw))
    cluster.disconnect_on['prepare'] = (core.ToolError('REMOTE_TIMEOUT', 'dropped'), False)
    fp = maker.call('vasp_prepare_repair_fork', {
        'contract_id': cid, 'server': 'hpc', 'source_job': 'si-adv_fix1_2', 'phase': 'convergence', 'strategy': 'fresh_restart', **_diag(),
    })
    assert fp['ok'] is False and fp['error_code'] == 'REPAIR_FORK_RESULT_UNCERTAIN'

    # 4. Remote dos_from_scf disconnect recovery succeeds when matching fork exists.
    _complete_local_through(f2_src, 'scf', monkeypatch, scf_top_ev=8.0)
    core.save(f2_src / 'submissions/dos.json', {'status': 'submitted', 'job_id': '8702', 'phase': 'dos'})
    cluster.disconnect_on['prepare_dos_fork'] = (core.ToolError('SSH_FAILED', 'reset'), True)
    dos_rec = maker.call('vasp_prepare_repair_fork', {
        'contract_id': cid, 'server': 'hpc', 'source_job': 'si-adv_fix1_2', 'phase': 'dos', 'strategy': 'dos_from_scf',
        'job_name': 'si-dos-rec', 'dos_values': {'nbands': 32}, **_diag(failure_category='empty_bands_insufficient'),
    })
    assert dos_rec['ok'] is True, dos_rec
    assert dos_rec['result']['recovered'] is True and dos_rec['result']['job_name'] == 'si-dos-rec'

    # 5. Exhausting max_submissions (=1) auto-halts with CONTRACT_BUDGET_EXHAUSTED and saves diagnostic.
    task_contract.reserve_submission(
        ws, cid, server='hpc', job_name='si-adv', phase='convergence',
        resources=contract['resource_envelope']['phase_defaults']['convergence'], operation_id='b' * 32,
    )
    task_contract.confirm_submission(ws, cid, job_id='8703', operation_id='b' * 32)
    sub_ex = maker.call('vasp_prepare_repair_fork', {
        'contract_id': cid, 'server': 'hpc', 'source_job': 'si-dos-rec', 'phase': 'dos', 'strategy': 'dos_from_scf', **_diag(),
    })
    assert sub_ex['ok'] is False and sub_ex['error_code'] == 'CONTRACT_BUDGET_EXHAUSTED'
    assert task_contract.read_ledger(ws, cid)['status'] == 'halted'


def test_fresh_restart_potcar_drift_halts_contract(tmp_path, monkeypatch):
    ws = _setup_workspace(tmp_path)
    maker, contract = _draft_and_approve(ws, monkeypatch, server=None, job_name='si-pot')
    cid = contract['contract_id']
    assert maker.call('vasp_prepare_task', {'contract_id': cid})['ok'] is True
    (ws / 'runs/si-pot/submissions').mkdir(exist_ok=True)
    core.save(ws / 'runs/si-pot/submissions/convergence.json', {'status': 'submitted', 'job_id': '8800', 'phase': 'convergence'})

    # Mutate POTCAR library while changing nbands (parameter_choice=None path).
    (ws / 'potentials/PBE/Si/POTCAR').write_text('PAW_PBE Si 05Jan2001 MUTATED\n', encoding='utf-8', newline='\n')
    res = maker.call('vasp_prepare_repair_fork', {
        'contract_id': cid, 'source_job': 'si-pot', 'phase': 'convergence', 'strategy': 'fresh_restart',
        'nbands': 24, **_diag(),
    })
    assert res['ok'] is False and res['error_code'] == 'INPUT_HASH_MISMATCH'
    ledger = task_contract.read_ledger(ws, cid)
    assert ledger['status'] == 'halted' and 'si-pot_fix1' not in ledger['allowed_jobs']

