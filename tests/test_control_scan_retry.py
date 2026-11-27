"""Offline tests for P1e-2 contract-gated strictly tighter convergence scan retry (`vasp_prepare_scan_retry`)."""
from __future__ import annotations

import contextlib
import io
import json
import os
from pathlib import Path
from types import SimpleNamespace

import pytest
from pymatgen.core import Lattice, Structure
from pymatgen.io.vasp.inputs import Poscar, Potcar

from vasp_agent.chat import release_check
from vasp_agent.common import agent_paths
from vasp_agent.control import ControlService
from vasp_agent.permissions import contract_ui, task_contract
from vasp_agent.remote import environment_profiles, remote_compute, remote_worker
from vasp_agent.workflow import convergence_reuse, full_workflow, input_review, stage_gate
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
def fake_pbe_potcar_and_vasprun(monkeypatch):
    fake_pot = [SimpleNamespace(element='Si', functional='PBE', TITEL='PAW_PBE Si 05Jan2001', ZVAL=4.0, enmax=245.345)]
    monkeypatch.setattr(Potcar, 'from_str', classmethod(lambda _cls, _text: fake_pot))
    monkeypatch.setattr(Potcar, 'from_file', classmethod(lambda _cls, _path: fake_pot))

    def _read_fake_result(folder: Path):
        structure = Poscar.from_file(folder / 'POSCAR').structure
        payload = json.loads((folder / 'vasprun.xml').read_text(encoding='utf-8'))
        n = len(structure)
        energy = float(payload['energy_per_atom']) * n
        forces = payload.get('forces') or [[0.0, 0.0, 0.0] for _ in range(n)]
        stress = payload.get('stress') or [[0.0, 0.0, 0.0], [0.0, 0.0, 0.0], [0.0, 0.0, 0.0]]
        return SimpleNamespace(
            final_energy=energy,
            final_structure=structure,
            ionic_steps=[{'forces': forces, 'stress': stress}],
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


def _envelope(*, allow_array: bool = False, conv_parallel: int = 1, max_parallel: int = 4) -> dict:
    phases = ['convergence', 'relaxation', 'scf', 'bands']
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
        'allowed_partitions': ['compute'],
        'max_parallel_tests': max_parallel,
        'max_cores_per_test': 16,
        'max_nodes': 2,
        'max_total_cores': 64,
        'max_walltime': '04:00:00',
        'phase_defaults': defaults,
    }


def _populate_convergence_outputs(
    folder: Path,
    *,
    encut_energies: tuple[float, float, float] = (-5.0, -5.0002, -5.0003),
    mesh_energies: tuple[float, float, float] = (-5.0, -5.0200, -5.0400),
    skip_stages: set[str] | None = None,
    cross_energy: float | None = None,
) -> None:
    """Write real hash-verified convergence outputs (energy_per_atom in eV/atom)."""
    skip = skip_stages or set()
    for kind, energies in (('encut', encut_energies), ('mesh', mesh_energies)):
        for idx, energy in enumerate(energies):
            rel = f'convergence/{kind}-{idx}'
            stage = folder / rel
            if not stage.is_dir() or rel in skip:
                continue
            if not (stage / 'POSCAR').is_file():
                (stage / 'POSCAR').write_bytes((folder / 'initial.POSCAR').read_bytes())
            if not (stage / 'POTCAR').is_file():
                (stage / 'POTCAR').write_bytes((folder / 'POTCAR').read_bytes())
            inputs = {name: core.digest(stage / name) for name in ('POSCAR', 'POTCAR', 'INCAR', 'KPOINTS')}
            core.save(stage / 'inputs.json', inputs)
            xml = stage / 'vasprun.xml'
            xml.write_text(json.dumps({'energy_per_atom': energy}), encoding='utf-8', newline='\n')
            core.save(stage / 'complete.json', {'xml_sha256': core.digest(xml)})
    cross = folder / 'convergence/cross-cutoff'
    if cross.is_dir() and cross_energy is not None:
        if not (cross / 'POSCAR').is_file():
            (cross / 'POSCAR').write_bytes((folder / 'initial.POSCAR').read_bytes())
        if not (cross / 'POTCAR').is_file():
            (cross / 'POTCAR').write_bytes((folder / 'POTCAR').read_bytes())
        inputs = {name: core.digest(cross / name) for name in ('POSCAR', 'POTCAR', 'INCAR', 'KPOINTS')}
        core.save(cross / 'inputs.json', inputs)
        xml = cross / 'vasprun.xml'
        xml.write_text(json.dumps({'energy_per_atom': cross_energy}), encoding='utf-8', newline='\n')
        core.save(cross / 'complete.json', {'xml_sha256': core.digest(xml)})


def _record_green_review(folder: Path, phase: str) -> dict:
    if folder.parent.name == 'runs':
        ws, server = folder.parents[1], None
    else:
        ws, server = folder.parents[2] / 'ws', 'hpc'
    checker = ControlService(ws, role='checker', actor_id='chk-scan-01', host='test', model='pro')
    got = checker.call('vasp_get_stage_review', {'server': server, 'job_name': folder.name})
    assert got['ok'] is True, got
    ctx = got['result']['summary'] if got['result'].get('payload_overflow') else got['result']
    assert ctx['phase'] == phase
    checks = [
        {
            'item': item,
            'judgment': 'pending' if (phase == 'convergence' and item == 'validation_plan') else 'pass',
            'blocking': False,
            'evidence': f'verified {phase}/{item}',
            'reason': 'offline scan retry test pass',
            'next_action': '无需',
        }
        for item in ctx['required_checks']
    ]
    rec = checker.call(
        'vasp_record_stage_review',
        {'server': server, 'job_name': folder.name, 'snapshot': ctx['snapshot'], 'phase': phase, 'checks': checks},
    )
    assert rec['ok'] is True, rec
    return rec['result']


class _FakeRemoteScanCluster:
    """Offline fake remote cluster wired to real convergence_reuse, input_review, and worker_resources."""

    def __init__(self, ws: Path, remote_root: Path):
        self.ws = ws
        self.jobs_dir = remote_root / 'jobs'
        self.jobs_dir.mkdir(parents=True, exist_ok=True)
        self.next_job_id = 3000
        self.queue_by_job: dict[str, str] = {}
        self.calls: list[tuple[str, str | None, dict]] = []
        self.prepare_retry_hook = None
        self.review_context_hook = None
        self.status_hook = None
        self.confirm_physics_hook = None
        self.configure_resources_hook = None
        self.list_jobs_override: list[str] | None = None

    def job_folder(self, job_name: str) -> Path:
        return self.jobs_dir / job_name

    def _write_remote_meta(self, folder: Path, job_name: str) -> None:
        script = folder / 'submit.slurm'
        script.write_text(SLURM_TEMPLATE.replace('va-demo', f'va-{job_name}'), encoding='utf-8', newline='\n')
        core.save(
            folder / 'remote-job.json',
            {'job_name': job_name, 'script_sha256': core.digest(script), 'release': 'local-package', 'version': 'test'},
        )
        core.save(folder / 'environment-review.json', {'executable_sha256': 'e' * 64, 'environment_sha256': 'f' * 64})

    def prepare(self, job_name: str, structure: str, **kwargs) -> dict:
        folder = self.job_folder(job_name)
        args = SimpleNamespace(
            structure=str(self.ws / structure), workdir=str(folder), potcar=None,
            potcar_library=str(self.ws / 'potentials/PBE'), potential_variants=kwargs.get('potential_variants'),
            encut=kwargs['encut'], mesh=kwargs['mesh'], nbands=kwargs['nbands'], relax_mode=kwargs['relax_mode'],
            parallel_tests=kwargs.get('parallel_tests') or 1, cores_per_test=kwargs.get('cores_per_test') or 8,
            scan_preset=kwargs.get('scan_preset'), band_path=kwargs.get('band_path'),
            optional_stages=kwargs.get('optional_stages'), stage_parameters=kwargs.get('stage_parameters'),
            encuts=kwargs.get('encuts'), meshes=kwargs.get('meshes'), scan_thresholds=kwargs.get('scan_thresholds'),
            parameter_choice=kwargs.get('parameter_choice'), preview_only=bool(kwargs.get('preview_only', False)),
            ediff=1e-7, points=20, symprec=0.01, force_tol=0.01, stress_tol=1.0, energy_tol=0.001, nsw=100,
        )
        with contextlib.redirect_stdout(io.StringIO()):
            res = full_workflow.prepare_full(args)
        if args.preview_only:
            return res
        self._write_remote_meta(folder, job_name)
        return {'job_name': job_name, 'prepared': True}

    def call(self, action: str, job_name: str | None = None, **kwargs) -> dict:
        self.calls.append((action, job_name, dict(kwargs)))
        if action == 'list_jobs':
            names = self.list_jobs_override if self.list_jobs_override is not None else sorted(p.name for p in self.jobs_dir.iterdir() if p.is_dir())
            return {'jobs': [{'name': n} for n in names]}
        assert job_name is not None
        folder = self.job_folder(job_name)
        if action == 'stage_context':
            return stage_gate.context(folder)
        if action == 'stage_review':
            return stage_gate.record(
                folder, kwargs['snapshot'], kwargs['phase'], kwargs['checks'], kwargs['reviewer'],
            )
        if action == 'status':
            if self.status_hook is not None:
                return self.status_hook(job_name, folder, kwargs)
            if not folder.is_dir():
                raise core.ToolError('REMOTE_ERROR', f'任务 {job_name} 不存在。')
            info: dict = {}
            for name in ('manifest.json', 'checkpoint.json', 'workflow-state.json', 'physics-model.json'):
                if (folder / name).is_file():
                    info[name] = json.loads((folder / name).read_text(encoding='utf-8'))
            info['stage_submissions'] = {
                p.stem: json.loads(p.read_text(encoding='utf-8'))
                for p in (folder / 'submissions').glob('*.json')
                if p.stem in ('convergence', 'relaxation', 'scf', 'bands', 'dos')
            } if (folder / 'submissions').is_dir() else {}
            if self.queue_by_job.get(job_name):
                info['queue'] = self.queue_by_job[job_name]
            return info
        if action == 'inspect_convergence':
            return convergence_reuse.inspect(folder)
        if action == 'review_context':
            if self.review_context_hook is not None:
                return self.review_context_hook(job_name, folder, kwargs)
            if not folder.is_dir() or not (folder / 'manifest.json').is_file():
                raise FileNotFoundError(f'No such file or directory: {folder / "manifest.json"}')
            return input_review.context(folder)
        if action == 'prepare_scan_retry':
            if self.prepare_retry_hook is not None:
                return self.prepare_retry_hook(job_name, folder, kwargs)
            if folder.exists():
                raise core.ToolError('REMOTE_ERROR', '重试必须使用不存在的新任务目录。')
            source = self.job_folder(kwargs['source_job'])
            with contextlib.redirect_stdout(io.StringIO()):
                result = convergence_reuse.prepare_retry(
                    source, folder, kwargs['mode'], kwargs.get('encuts'), kwargs.get('meshes'),
                    fixed_encut=kwargs.get('fixed_encut'), scan_thresholds=kwargs.get('scan_thresholds'),
                    scan_preset=kwargs.get('scan_preset'),
                )
            self._write_remote_meta(folder, job_name)
            return result
        if action == 'confirm_physics':
            if self.confirm_physics_hook is not None:
                return self.confirm_physics_hook(job_name, folder, kwargs)
            return stage_gate.confirm_physics(folder, kwargs['goal'], kwargs.get('channel', 'operator_slash_command'))
        if action == 'resource_context':
            return remote_worker.resource_context(folder, kwargs.get('phase'))
        if action == 'configure_resources':
            if self.configure_resources_hook is not None:
                return self.configure_resources_hook(job_name, folder, kwargs)
            return remote_worker.configure_resources(
                folder, kwargs.get('phase'), kwargs['resources'], kwargs['signature'], kwargs['limits'], kwargs.get('operation_id'),
            )
        if action == 'submit_checked':
            rec_path = folder / 'submissions' / f'{kwargs["phase"]}.json'
            if rec_path.exists():
                return {**json.loads(rec_path.read_text(encoding='utf-8')), 'submission_not_attempted': True, 'existing_record': True}
            core.verify(folder)
            remote_worker.check_resources((folder / 'submit.slurm').read_text(encoding='utf-8'), kwargs['limits'])
            stage_gate.require_review(folder, kwargs['phase'])
            self.next_job_id += 1
            data = {'status': 'submitted', 'job_id': str(self.next_job_id), 'phase': kwargs['phase']}
            rec_path.parent.mkdir(exist_ok=True)
            core.save(rec_path, data)
            core.save(folder / 'remote-submission.json', data)
            return data
        raise AssertionError(f'Unexpected remote action: {action}')


def _install_fake_remote(monkeypatch, ws: Path, remote_root: Path) -> _FakeRemoteScanCluster:
    cluster = _FakeRemoteScanCluster(ws, remote_root)
    monkeypatch.setattr(release_check, 'ensure_current', lambda _box, _server: None)
    monkeypatch.setattr(remote_worker, 'check_previous_job', lambda _folder, _phase: None)
    monkeypatch.setattr(remote_worker, 'task_release', lambda _meta: None)

    class _Adapter:
        def __init__(self, _workspace, _server):
            pass

        def prepare(self, *args, **kwargs):
            return cluster.prepare(*args, **kwargs)

        def call(self, action, job_name=None, **kwargs):
            return cluster.call(action, job_name, **kwargs)

    monkeypatch.setattr(remote_compute, 'RemoteCompute', _Adapter)
    return cluster


def _draft_and_approve(
    ws: Path, monkeypatch, *, server: str | None = 'hpc', job_name: str = 'si-scan-01',
    structure: str = 'structures/Si.POSCAR', relax_mode: str = 'cell', mesh: list[int] | None = None,
    encuts: list[float] | None = None, meshes: list[list[int]] | None = None, scan_preset: str = 'standard',
    precision_bounds: dict | None = None, budget: dict | None = None, allow_array: bool = False, conv_parallel: int = 1,
) -> tuple[ControlService, dict, dict]:
    maker = ControlService(ws, role='maker', actor_id='mk-scan')
    args = {
        'server': server, 'job_name': job_name, 'structure': structure,
        'encut': 400, 'mesh': mesh or [4, 4, 4], 'nbands': 16, 'relax_mode': relax_mode,
        'goal': 'Si 非磁性 PBE 收敛重扫测试',
        'resource_envelope': _envelope(allow_array=allow_array, conv_parallel=conv_parallel),
        'allowed_phases': ['convergence', 'relaxation', 'scf', 'bands'],
        'scan_preset': scan_preset, 'expires_in_hours': 168,
        'rationale': '离线扫描测试采用显式三级序列；资源受合同包络限制；待收敛验收后推进。',
        **({'encuts': encuts} if encuts is not None else {}),
        **({'meshes': meshes} if meshes is not None else {}),
        **({'precision_bounds': precision_bounds} if precision_bounds is not None else {}),
        **({'budget': budget} if budget is not None else {}),
    }
    draft_res = maker.call('vasp_draft_contract', args)
    assert draft_res['ok'] is True, draft_res
    monkeypatch.setattr(task_contract, 'has_real_tty', lambda stream=None: True)
    approved = contract_ui.approve_contract_interactive(
        ws, draft_res['result']['draft_id'], reader=lambda _p: '1', output=lambda _s: None,
    )
    return maker, approved['contract'], approved['ledger']


# ------------------- 1. Remote happy path: mesh retry, default parallel clamp, ledger allowed_jobs & submit


def test_prepare_scan_retry_remote_mesh_reuses_encut_clamps_default_parallel_and_allows_submit(tmp_path, monkeypatch):
    ws = _setup_workspace(tmp_path / 'ws')
    cluster = _install_fake_remote(monkeypatch, ws, tmp_path / 'remote')
    maker, contract, _ = _draft_and_approve(
        ws, monkeypatch, server='hpc', job_name='si-mesh-01',
        encuts=[400, 460, 520], meshes=[[4, 4, 4], [6, 6, 6], [8, 8, 8]],
        allow_array=True, conv_parallel=4,
    )
    cid = contract['contract_id']
    assert maker.call('vasp_prepare_task', {'contract_id': cid})['ok'] is True
    src_folder = cluster.job_folder('si-mesh-01')
    _populate_convergence_outputs(src_folder, encut_energies=(-5.0, -5.0002, -5.0003), mesh_energies=(-5.0, -5.015, -5.030))

    retry = maker.call(
        'vasp_prepare_scan_retry',
        {
            'contract_id': cid, 'server': 'hpc', 'source_job': 'si-mesh-01', 'mode': 'mesh',
            'meshes': [[8, 8, 8], [10, 10, 10], [12, 12, 12]],
            'reason': 'k-mesh energy delta 15 meV/atom > 1 meV/atom; densifying k-mesh while reusing passed ENCUT',
        },
    )
    assert retry['ok'] is True, retry
    body = retry['result']
    assert body['job_name'] == 'si-mesh-01_r1'
    assert body['name_conflicted'] is False
    assert body['prepared'] is True and body['submitted'] is False and body['recovered'] is False
    assert body['planned_tests'] == 3
    assert body['encuts'] == [400.0, 460.0, 520.0]
    assert body['meshes'] == [[8, 8, 8], [10, 10, 10], [12, 12, 12]]
    assert body['physics_confirmed'] is True
    assert body['physics']['confirmed_by'] == 'operator_slash_command'
    assert body['resources_applied'] is True
    # Contract default parallel_tests=4 is safely clamped to planned_tests=3 for mesh-only retry.
    assert body['resources']['parallel_tests'] == 3
    assert body['ledger']['scan_retries_used'] == 1
    assert body['ledger']['scan_retries_remaining'] == 0
    assert 'si-mesh-01_r1' in body['ledger']['allowed_jobs']

    new_folder = cluster.job_folder('si-mesh-01_r1')
    assert (new_folder / 'convergence-reuse.json').is_file()
    assert (new_folder / 'reused-evidence/convergence/encut-0/vasprun.xml').is_file()
    assert not (new_folder / 'convergence/encut-0').exists()
    assert (new_folder / 'physics-model.json').is_file()
    audit_rows = [json.loads(line) for line in agent_paths.audit_file(ws).read_text(encoding='utf-8').splitlines() if line.strip()]
    assert any(row.get('control_prepare_scan_retry', {}).get('job_name') == 'si-mesh-01_r1' for row in audit_rows)

    # New directory is immediately eligible for stage review + default-resource bounded submission (no explicit resources!).
    _record_green_review(new_folder, 'convergence')
    # Explicit over-parallel resources (4 > 3 tests) is still rejected!
    over_par = maker.call(
        'vasp_submit_stage',
        {
            'contract_id': cid, 'server': 'hpc', 'job_name': 'si-mesh-01_r1', 'phase': 'convergence',
            'resources': contract['resource_envelope']['phase_defaults']['convergence'],
        },
    )
    assert over_par['ok'] is False and over_par['error_code'] == 'CONTRACT_RESOURCE_ENVELOPE_EXCEEDED'
    # Omitting resources uses contract default clamped to planned_tests=3 and submits smoothly without re-review loop.
    sub = maker.call(
        'vasp_submit_stage',
        {'contract_id': cid, 'server': 'hpc', 'job_name': 'si-mesh-01_r1', 'phase': 'convergence'},
    )
    assert sub['ok'] is True, sub
    assert sub['result']['job_id'] == '3001'


# ------------------- 2. Encut retry, both retry, and fixed_encut validation


def test_prepare_scan_retry_encut_both_and_fixed_encut_rules(tmp_path, monkeypatch):
    ws = _setup_workspace(tmp_path / 'ws')
    cluster = _install_fake_remote(monkeypatch, ws, tmp_path / 'remote')
    maker, contract, _ = _draft_and_approve(
        ws, monkeypatch, server='hpc', job_name='si-mix',
        encuts=[400, 460, 520], meshes=[[4, 4, 4], [6, 6, 6], [8, 8, 8]],
        budget={'max_scan_retries': 3},
    )
    cid = contract['contract_id']
    assert maker.call('vasp_prepare_task', {'contract_id': cid})['ok'] is True
    src = cluster.job_folder('si-mix')

    # Case A: ENCUT passed with 460 agreeing with 520 (0.3 meV), 400 disagreeing (1.2 meV); mesh failed.
    _populate_convergence_outputs(src, encut_energies=(-5.0, -5.0009, -5.0012), mesh_energies=(-5.0, -5.015, -5.030))
    bad_fixed = maker.call(
        'vasp_prepare_scan_retry',
        {'contract_id': cid, 'server': 'hpc', 'source_job': 'si-mix', 'mode': 'mesh',
         'meshes': [[8, 8, 8], [10, 10, 10], [12, 12, 12]], 'fixed_encut': 400},
    )
    assert bad_fixed['ok'] is False and bad_fixed['error_code'] == 'SCAN_RETRY_FIXED_ENCUT_INELIGIBLE'

    ok_fixed = maker.call(
        'vasp_prepare_scan_retry',
        {'contract_id': cid, 'server': 'hpc', 'source_job': 'si-mix', 'mode': 'mesh',
         'meshes': [[8, 8, 8], [10, 10, 10], [12, 12, 12]], 'fixed_encut': 460},
    )
    assert ok_fixed['ok'] is True, ok_fixed
    assert ok_fixed['result']['planned_tests'] == 4 and ok_fixed['result']['fixed_encut'] == 460
    assert (cluster.job_folder('si-mix_r1') / 'convergence/cross-cutoff/INCAR').is_file()

    # Case B: ENCUT failed, mesh passed -> encut-only retry succeeds; fixed_encut rejected on encut mode.
    _populate_convergence_outputs(src, encut_energies=(-5.0, -5.020, -5.040), mesh_energies=(-5.0, -5.0002, -5.0003))
    bad_fixed_mode = maker.call(
        'vasp_prepare_scan_retry',
        {'contract_id': cid, 'server': 'hpc', 'source_job': 'si-mix', 'mode': 'encut',
         'encuts': [520, 580, 640], 'fixed_encut': 460},
    )
    assert bad_fixed_mode['ok'] is False and bad_fixed_mode['error_code'] == 'CONTRACT_SCHEMA_INVALID'

    ok_encut = maker.call(
        'vasp_prepare_scan_retry',
        {'contract_id': cid, 'server': 'hpc', 'source_job': 'si-mix', 'mode': 'encut', 'encuts': [520, 580, 640]},
    )
    assert ok_encut['ok'] is True, ok_encut
    assert ok_encut['result']['job_name'] == 'si-mix_r1_2' and ok_encut['result']['name_conflicted'] is True
    assert (cluster.job_folder('si-mix_r1_2') / 'reused-evidence/convergence/mesh-0/vasprun.xml').is_file()

    # Case C: Both failed -> mode='both' succeeds; encut/mesh-only refused with SCAN_RETRY_MODE_MISMATCH.
    _populate_convergence_outputs(src, encut_energies=(-5.0, -5.020, -5.040), mesh_energies=(-5.0, -5.020, -5.040))
    bad_mode = maker.call(
        'vasp_prepare_scan_retry',
        {'contract_id': cid, 'server': 'hpc', 'source_job': 'si-mix', 'mode': 'encut', 'encuts': [520, 580, 640]},
    )
    assert bad_mode['ok'] is False and bad_mode['error_code'] == 'SCAN_RETRY_MODE_MISMATCH'
    ok_both = maker.call(
        'vasp_prepare_scan_retry',
        {'contract_id': cid, 'server': 'hpc', 'source_job': 'si-mix', 'mode': 'both',
         'encuts': [520, 580, 640], 'meshes': [[8, 8, 8], [10, 10, 10], [12, 12, 12]]},
    )
    assert ok_both['ok'] is True, ok_both
    assert ok_both['result']['planned_tests'] == 6 and ok_both['result']['job_name'] == 'si-mix_r1_3'


# ------------------- 3. Local workspace execution, non-overwrite collision, and symlink guard


def test_prepare_scan_retry_local_execution_collision_and_live_guards(tmp_path, monkeypatch):
    ws = _setup_workspace(tmp_path / 'ws')
    maker, contract, _ = _draft_and_approve(
        ws, monkeypatch, server=None, job_name='si-loc',
        encuts=[400, 460, 520], meshes=[[4, 4, 4], [6, 6, 6], [8, 8, 8]],
        budget={'max_scan_retries': 2},
    )
    cid = contract['contract_id']
    assert maker.call('vasp_prepare_task', {'contract_id': cid})['ok'] is True
    src = ws / 'runs/si-loc'
    _populate_convergence_outputs(src, encut_energies=(-5.0, -5.0001, -5.0002), mesh_energies=(-5.0, -5.020, -5.040))

    # Local live guards: .agent.lock, .task-archived.json, corrupt checkpoint.json, completed checkpoint.
    (src / '.agent.lock').write_text('pid=1\n', encoding='utf-8', newline='\n')
    assert maker.call(
        'vasp_prepare_scan_retry',
        {'contract_id': cid, 'server': None, 'source_job': 'si-loc', 'mode': 'mesh',
         'meshes': [[8, 8, 8], [10, 10, 10], [12, 12, 12]]},
    )['error_code'] == 'SCAN_RETRY_SOURCE_RUNNING'
    (src / '.agent.lock').unlink()

    (src / '.task-archived.json').write_text('{}\n', encoding='utf-8', newline='\n')
    assert maker.call(
        'vasp_prepare_scan_retry',
        {'contract_id': cid, 'server': None, 'source_job': 'si-loc', 'mode': 'mesh',
         'meshes': [[8, 8, 8], [10, 10, 10], [12, 12, 12]]},
    )['error_code'] == 'SCAN_RETRY_NOT_ELIGIBLE'
    (src / '.task-archived.json').unlink()

    (src / 'checkpoint.json').write_text('{corrupt', encoding='utf-8', newline='\n')
    assert maker.call(
        'vasp_prepare_scan_retry',
        {'contract_id': cid, 'server': None, 'source_job': 'si-loc', 'mode': 'mesh',
         'meshes': [[8, 8, 8], [10, 10, 10], [12, 12, 12]]},
    )['error_code'] == 'SCAN_RETRY_SOURCE_UNCERTAIN'
    (src / 'checkpoint.json').write_text('{"completed":["convergence"]}\n', encoding='utf-8', newline='\n')
    assert maker.call(
        'vasp_prepare_scan_retry',
        {'contract_id': cid, 'server': None, 'source_job': 'si-loc', 'mode': 'mesh',
         'meshes': [[8, 8, 8], [10, 10, 10], [12, 12, 12]]},
    )['error_code'] == 'SCAN_RETRY_ALREADY_PASSED'
    (src / 'checkpoint.json').unlink()

    # Pre-create runs/si-loc_r1 with sentinel file: local retry must NEVER overwrite it!
    sentinel_dir = ws / 'runs/si-loc_r1'
    sentinel_dir.mkdir(parents=True)
    (sentinel_dir / 'sentinel.txt').write_text('keep-me\n', encoding='utf-8', newline='\n')

    retry = maker.call(
        'vasp_prepare_scan_retry',
        {'contract_id': cid, 'server': None, 'source_job': 'si-loc', 'mode': 'mesh',
         'meshes': [[8, 8, 8], [10, 10, 10], [12, 12, 12]]},
    )
    assert retry['ok'] is True, retry
    assert retry['result']['job_name'] == 'si-loc_r1_2'
    assert retry['result']['name_conflicted'] is True
    assert retry['result']['resources_applied'] is False
    assert (sentinel_dir / 'sentinel.txt').read_text(encoding='utf-8') == 'keep-me\n'
    assert (ws / 'runs/si-loc_r1_2/physics-model.json').is_file()

    # Local with configured submit_script applies resources cleanly, and SCRIPT_HASH_DRIFT halts contract.
    script_file = ws / 'submit.slurm'
    script_file.write_text(SLURM_TEMPLATE, encoding='utf-8', newline='\n')
    maker.box.submit_script = script_file
    maker.box.script_hash = core.digest(script_file)
    retry2 = maker.call(
        'vasp_prepare_scan_retry',
        {'contract_id': cid, 'server': None, 'source_job': 'si-loc', 'mode': 'mesh',
         'meshes': [[8, 8, 8], [10, 10, 10], [12, 12, 12]]},
    )
    assert retry2['ok'] is True and retry2['result']['resources_applied'] is True


def test_prepare_scan_retry_real_symlink_refused(tmp_path, monkeypatch):
    outside = tmp_path / 'outside-dir'
    outside.mkdir()
    probe = tmp_path / 'sym-probe'
    try:
        os.symlink(outside, probe, target_is_directory=True)
    except OSError:
        pytest.skip('Windows environment lacks symlink creation privilege')
    probe.unlink()

    ws = _setup_workspace(tmp_path / 'ws')
    maker, contract, _ = _draft_and_approve(ws, monkeypatch, server=None, job_name='si-sym')
    (ws / 'runs').mkdir(parents=True, exist_ok=True)
    os.symlink(outside, ws / 'runs/si-sym', target_is_directory=True)
    res_sym = maker.call(
        'vasp_prepare_scan_retry',
        {'contract_id': contract['contract_id'], 'server': None, 'source_job': 'si-sym', 'mode': 'mesh',
         'meshes': [[8, 8, 8], [10, 10, 10], [12, 12, 12]]},
    )
    assert res_sym['ok'] is False and res_sym['error_code'] == 'CONTRACT_SYMLINK_REFUSED'


@pytest.mark.parametrize(
    'symlink_suffix',
    [
        'runs/si-msym',
        'runs/si-msym/submissions/convergence.json',
        'runs/si-msym/checkpoint.json',
        'runs/si-msym_r1',
        'scan_plans',
    ],
)
def test_prepare_scan_retry_mock_symlink_guards(tmp_path, monkeypatch, symlink_suffix):
    ws = _setup_workspace(tmp_path / 'ws')
    maker, contract, _ = _draft_and_approve(
        ws, monkeypatch, server=None, job_name='si-msym',
        encuts=[400, 460, 520], meshes=[[4, 4, 4], [6, 6, 6], [8, 8, 8]],
    )
    cid = contract['contract_id']
    assert maker.call('vasp_prepare_task', {'contract_id': cid})['ok'] is True
    _populate_convergence_outputs(ws / 'runs/si-msym', encut_energies=(-5.0, -5.0001, -5.0002), mesh_energies=(-5.0, -5.020, -5.040))

    orig_is_symlink = Path.is_symlink

    def fake_is_symlink(self: Path) -> bool:
        posix = self.as_posix()
        if symlink_suffix == 'scan_plans':
            if posix.endswith(f'/{contract["workflow"]["plan_id"]}.json'):
                return True
        elif posix.endswith(symlink_suffix):
            return True
        return orig_is_symlink(self)

    monkeypatch.setattr(Path, 'is_symlink', fake_is_symlink)
    res = maker.call(
        'vasp_prepare_scan_retry',
        {'contract_id': cid, 'server': None, 'source_job': 'si-msym', 'mode': 'mesh',
         'meshes': [[8, 8, 8], [10, 10, 10], [12, 12, 12]]},
    )
    assert res['ok'] is False and res['error_code'] == 'CONTRACT_SYMLINK_REFUSED'


# ------------------- 4. Eligibility gating: unavailable/crash, tampered output, already passed, running


def test_prepare_scan_retry_rejects_crash_already_passed_running_and_halts_on_tampered_output(tmp_path, monkeypatch):
    ws = _setup_workspace(tmp_path / 'ws')
    cluster = _install_fake_remote(monkeypatch, ws, tmp_path / 'remote')
    maker, contract, _ = _draft_and_approve(
        ws, monkeypatch, server='hpc', job_name='si-gate',
        encuts=[400, 460, 520], meshes=[[4, 4, 4], [6, 6, 6], [8, 8, 8]],
    )
    cid = contract['contract_id']
    assert maker.call('vasp_prepare_task', {'contract_id': cid})['ok'] is True
    src = cluster.job_folder('si-gate')

    # 1. Missing output (hardware crash / incomplete scan) -> SCAN_RETRY_NOT_ELIGIBLE (contract stays active).
    _populate_convergence_outputs(src, skip_stages={'convergence/mesh-2'})
    crash = maker.call(
        'vasp_prepare_scan_retry',
        {'contract_id': cid, 'server': 'hpc', 'source_job': 'si-gate', 'mode': 'mesh',
         'meshes': [[8, 8, 8], [10, 10, 10], [12, 12, 12]]},
    )
    assert crash['ok'] is False and crash['error_code'] == 'SCAN_RETRY_NOT_ELIGIBLE'
    assert task_contract.read_ledger(ws, cid)['status'] == 'active'

    # 2. All converged -> SCAN_RETRY_ALREADY_PASSED.
    _populate_convergence_outputs(src, encut_energies=(-5.0, -5.0001, -5.0002), mesh_energies=(-5.0, -5.0001, -5.0002))
    passed = maker.call(
        'vasp_prepare_scan_retry',
        {'contract_id': cid, 'server': 'hpc', 'source_job': 'si-gate', 'mode': 'mesh',
         'meshes': [[8, 8, 8], [10, 10, 10], [12, 12, 12]]},
    )
    assert passed['ok'] is False and passed['error_code'] == 'SCAN_RETRY_ALREADY_PASSED'

    # 3. Still in Slurm queue -> SCAN_RETRY_SOURCE_RUNNING.
    _populate_convergence_outputs(src, encut_energies=(-5.0, -5.0001, -5.0002), mesh_energies=(-5.0, -5.020, -5.040))
    cluster.queue_by_job['si-gate'] = '3001|RUNNING|00:10:00|node01'
    running = maker.call(
        'vasp_prepare_scan_retry',
        {'contract_id': cid, 'server': 'hpc', 'source_job': 'si-gate', 'mode': 'mesh',
         'meshes': [[8, 8, 8], [10, 10, 10], [12, 12, 12]]},
    )
    assert running['ok'] is False and running['error_code'] == 'SCAN_RETRY_SOURCE_RUNNING'
    cluster.queue_by_job.clear()

    # 4. Tampered convergence output hash -> auto-halts contract with INPUT_HASH_MISMATCH!
    (src / 'convergence/encut-1/vasprun.xml').write_text('{"energy_per_atom": -999.0}', encoding='utf-8', newline='\n')
    tampered = maker.call(
        'vasp_prepare_scan_retry',
        {'contract_id': cid, 'server': 'hpc', 'source_job': 'si-gate', 'mode': 'mesh',
         'meshes': [[8, 8, 8], [10, 10, 10], [12, 12, 12]]},
    )
    assert tampered['ok'] is False and tampered['error_code'] == 'INPUT_HASH_MISMATCH'
    ledger = task_contract.read_ledger(ws, cid)
    assert ledger['status'] == 'halted' and ledger['halt_code'] == 'INPUT_HASH_MISMATCH'


# ------------------- 5. Monotonic stricter rules, threshold non-relaxation, 2D kz==1 preservation


def test_prepare_scan_retry_enforces_strict_monotonic_no_relaxation_and_2d_kz(tmp_path, monkeypatch):
    ws = _setup_workspace(tmp_path / 'ws')
    cluster = _install_fake_remote(monkeypatch, ws, tmp_path / 'remote')
    maker, contract, _ = _draft_and_approve(
        ws, monkeypatch, server='hpc', job_name='si-strict',
        encuts=[400, 460, 520], meshes=[[4, 4, 4], [6, 6, 6], [8, 8, 8]],
        scan_preset='strict', budget={'max_scan_retries': 2},
    )
    cid = contract['contract_id']
    assert maker.call('vasp_prepare_task', {'contract_id': cid})['ok'] is True
    src = cluster.job_folder('si-strict')
    _populate_convergence_outputs(src, encut_energies=(-5.0, -5.020, -5.040), mesh_energies=(-5.0, -5.0001, -5.0002))

    # Relaxing strict -> standard is forbidden.
    rel_preset = maker.call(
        'vasp_prepare_scan_retry',
        {'contract_id': cid, 'server': 'hpc', 'source_job': 'si-strict', 'mode': 'encut',
         'encuts': [520, 580, 640], 'scan_preset': 'standard'},
    )
    assert rel_preset['ok'] is False and rel_preset['error_code'] == 'CONTRACT_THRESHOLD_RELAXATION_FORBIDDEN'

    # Relaxing scan_thresholds is forbidden.
    rel_th = maker.call(
        'vasp_prepare_scan_retry',
        {'contract_id': cid, 'server': 'hpc', 'source_job': 'si-strict', 'mode': 'encut',
         'encuts': [520, 580, 640], 'scan_thresholds': {'energy_eV_per_atom': 0.005}},
    )
    assert rel_th['ok'] is False and rel_th['error_code'] == 'CONTRACT_THRESHOLD_RELAXATION_FORBIDDEN'

    # Non-stricter ENCUT (max not strictly > 520) or below potential_floor (319 for cell Si) is forbidden.
    not_strict = maker.call(
        'vasp_prepare_scan_retry',
        {'contract_id': cid, 'server': 'hpc', 'source_job': 'si-strict', 'mode': 'encut', 'encuts': [400, 460, 520]},
    )
    assert not_strict['ok'] is False and not_strict['error_code'] == 'CONTRACT_RETRY_NOT_STRICTER'

    below_floor = maker.call(
        'vasp_prepare_scan_retry',
        {'contract_id': cid, 'server': 'hpc', 'source_job': 'si-strict', 'mode': 'encut', 'encuts': [300, 460, 580]},
    )
    assert below_floor['ok'] is False and below_floor['error_code'] == 'CONTRACT_RETRY_NOT_STRICTER'

    # Attempting to modify reused meshes in encut mode is rejected.
    mod_reused = maker.call(
        'vasp_prepare_scan_retry',
        {'contract_id': cid, 'server': 'hpc', 'source_job': 'si-strict', 'mode': 'encut',
         'encuts': [520, 580, 640], 'meshes': [[6, 6, 6], [8, 8, 8], [10, 10, 10]]},
    )
    assert mod_reused['ok'] is False and mod_reused['error_code'] == 'CONTRACT_RETRY_NOT_STRICTER'

    # Tightening scan_thresholds IS permitted and preserved into the retry's config.json!
    tight = maker.call(
        'vasp_prepare_scan_retry',
        {'contract_id': cid, 'server': 'hpc', 'source_job': 'si-strict', 'mode': 'encut',
         'encuts': [520, 580, 640], 'scan_thresholds': {'energy_eV_per_atom': 0.0005}},
    )
    assert tight['ok'] is True, tight
    r1_cfg = json.loads((cluster.job_folder('si-strict_r1') / 'config.json').read_text(encoding='utf-8'))
    assert r1_cfg['scan_thresholds'] == {'energy_eV_per_atom': 0.0005}

    # 2D slab: kz != 1 is forbidden, kz == 1 succeeds.
    ws2d = _setup_workspace(tmp_path / 'ws2d', slab_2d=True)
    cluster2d = _install_fake_remote(monkeypatch, ws2d, tmp_path / 'remote2d')
    maker2d, ctr2d, _ = _draft_and_approve(
        ws2d, monkeypatch, server='hpc', job_name='si2d', structure='structures/Si2D.POSCAR',
        relax_mode='ions', mesh=[4, 4, 1], encuts=[400, 460, 520], meshes=[[4, 4, 1], [6, 6, 1], [8, 8, 1]],
    )
    cid2d = ctr2d['contract_id']
    assert maker2d.call('vasp_prepare_task', {'contract_id': cid2d})['ok'] is True
    _populate_convergence_outputs(
        cluster2d.job_folder('si2d'), encut_energies=(-5.0, -5.0001, -5.0002), mesh_energies=(-5.0, -5.020, -5.040),
    )
    bad_kz = maker2d.call(
        'vasp_prepare_scan_retry',
        {'contract_id': cid2d, 'server': 'hpc', 'source_job': 'si2d', 'mode': 'mesh',
         'meshes': [[8, 8, 1], [10, 10, 1], [12, 12, 2]]},
    )
    assert bad_kz['ok'] is False and bad_kz['error_code'] == 'CONTRACT_RETRY_NOT_STRICTER'

    ok_2d = maker2d.call(
        'vasp_prepare_scan_retry',
        {'contract_id': cid2d, 'server': 'hpc', 'source_job': 'si2d', 'mode': 'mesh',
         'meshes': [[8, 8, 1], [10, 10, 1], [12, 12, 1]]},
    )
    assert ok_2d['ok'] is True and ok_2d['result']['physics']['dimension'] == '2D'


# ------------------- 6. Precision bounds auto-halt & max_scan_retries budget auto-halt


def test_prepare_scan_retry_precision_bounds_and_budget_auto_halt(tmp_path, monkeypatch):
    ws = _setup_workspace(tmp_path / 'ws')
    cluster = _install_fake_remote(monkeypatch, ws, tmp_path / 'remote')
    maker, contract, _ = _draft_and_approve(
        ws, monkeypatch, server='hpc', job_name='si-bounds',
        encuts=[400, 460, 520], meshes=[[4, 4, 4], [6, 6, 6], [8, 8, 8]],
        precision_bounds={'max_encut': 600, 'max_kmesh_axis': 12, 'max_kmesh_points': 1728},
        budget={'max_scan_retries': 2},
    )
    cid = contract['contract_id']
    assert maker.call('vasp_prepare_task', {'contract_id': cid})['ok'] is True
    src = cluster.job_folder('si-bounds')
    _populate_convergence_outputs(src, encut_energies=(-5.0, -5.020, -5.040), mesh_energies=(-5.0, -5.0001, -5.0002))

    # Exceeding max_encut when source is still below ceiling rejects cleanly WITHOUT halting contract.
    too_high = maker.call(
        'vasp_prepare_scan_retry',
        {'contract_id': cid, 'server': 'hpc', 'source_job': 'si-bounds', 'mode': 'encut', 'encuts': [520, 580, 650]},
    )
    assert too_high['ok'] is False and too_high['error_code'] == 'CONTRACT_PRECISION_BOUND_EXCEEDED'
    assert task_contract.read_ledger(ws, cid)['status'] == 'active'

    # Valid retry right up to ceiling 600 eV succeeds.
    at_cap = maker.call(
        'vasp_prepare_scan_retry',
        {'contract_id': cid, 'server': 'hpc', 'source_job': 'si-bounds', 'mode': 'encut', 'encuts': [520, 560, 600]},
    )
    assert at_cap['ok'] is True and at_cap['result']['job_name'] == 'si-bounds_r1'

    # Now si-bounds_r1 ALSO fails ENCUT right at max_encut=600 -> MUST auto-halt contract!
    _populate_convergence_outputs(
        cluster.job_folder('si-bounds_r1'), encut_energies=(-5.0, -5.020, -5.040), mesh_energies=(-5.0, -5.0001, -5.0002),
    )
    ceiling_hit = maker.call(
        'vasp_prepare_scan_retry',
        {'contract_id': cid, 'server': 'hpc', 'source_job': 'si-bounds_r1', 'mode': 'encut', 'encuts': [600, 650, 700]},
    )
    assert ceiling_hit['ok'] is False and ceiling_hit['error_code'] == 'CONTRACT_PRECISION_BOUND_EXCEEDED'
    halted = task_contract.read_ledger(ws, cid)
    assert halted['status'] == 'halted' and halted['halt_code'] == 'CONTRACT_PRECISION_BOUND_EXCEEDED'

    # Separate contract with max_scan_retries=1 -> 2nd retry auto-halts with CONTRACT_BUDGET_EXHAUSTED before remote mutation.
    maker2, ctr2, _ = _draft_and_approve(
        ws, monkeypatch, server='hpc', job_name='si-bud',
        encuts=[400, 460, 520], meshes=[[4, 4, 4], [6, 6, 6], [8, 8, 8]],
        budget={'max_scan_retries': 1},
    )
    cid2 = ctr2['contract_id']
    assert maker2.call('vasp_prepare_task', {'contract_id': cid2})['ok'] is True
    _populate_convergence_outputs(cluster.job_folder('si-bud'), encut_energies=(-5.0, -5.0001, -5.0002), mesh_energies=(-5.0, -5.020, -5.040))
    first = maker2.call(
        'vasp_prepare_scan_retry',
        {'contract_id': cid2, 'server': 'hpc', 'source_job': 'si-bud', 'mode': 'mesh',
         'meshes': [[8, 8, 8], [10, 10, 10], [12, 12, 12]]},
    )
    assert first['ok'] is True
    calls_before = len(cluster.calls)
    second = maker2.call(
        'vasp_prepare_scan_retry',
        {'contract_id': cid2, 'server': 'hpc', 'source_job': 'si-bud', 'mode': 'mesh',
         'meshes': [[8, 8, 8], [10, 10, 10], [12, 12, 12]]},
    )
    assert second['ok'] is False and second['error_code'] == 'CONTRACT_BUDGET_EXHAUSTED'
    assert len(cluster.calls) == calls_before
    assert task_contract.read_ledger(ws, cid2)['status'] == 'halted'


# ------------------- 7. Reactive collision naming, 64-char truncation, and disconnect reconciliation


def test_prepare_scan_retry_reactive_collision_64char_limit_and_disconnect_reconciliation(tmp_path, monkeypatch):
    ws = _setup_workspace(tmp_path / 'ws')
    cluster = _install_fake_remote(monkeypatch, ws, tmp_path / 'remote')
    long_job = 's' * 64
    maker, contract, _ = _draft_and_approve(
        ws, monkeypatch, server='hpc', job_name=long_job,
        encuts=[400, 460, 520], meshes=[[4, 4, 4], [6, 6, 6], [8, 8, 8]],
        budget={'max_scan_retries': 3},
    )
    cid = contract['contract_id']
    assert maker.call('vasp_prepare_task', {'contract_id': cid})['ok'] is True
    _populate_convergence_outputs(cluster.job_folder(long_job), encut_energies=(-5.0, -5.0001, -5.0002), mesh_energies=(-5.0, -5.020, -5.040))

    # Simulate stale list_jobs hiding a concurrent remote collision -> reactive increment succeeds <= 64 chars.
    cluster.list_jobs_override = []
    first_candidate = 's' * 61 + '_r1'
    cluster.job_folder(first_candidate).mkdir(parents=True)

    r_ok = maker.call(
        'vasp_prepare_scan_retry',
        {'contract_id': cid, 'server': 'hpc', 'source_job': long_job, 'mode': 'mesh',
         'meshes': [[8, 8, 8], [10, 10, 10], [12, 12, 12]]},
    )
    assert r_ok['ok'] is True, r_ok
    assert r_ok['result']['name_conflicted'] is True
    assert len(r_ok['result']['job_name']) <= 64
    assert r_ok['result']['job_name'].endswith('_2')
    cluster.list_jobs_override = None

    # Disconnect Case 1: SSH dropped BEFORE remote created target dir -> SCAN_RETRY_DISCONNECTED_NOT_CREATED (0 retry spent).
    cluster.prepare_retry_hook = lambda _jn, _f, _kw: (_ for _ in ()).throw(core.ToolError('SSH_FAILED', 'connection reset'))
    not_created = maker.call(
        'vasp_prepare_scan_retry',
        {'contract_id': cid, 'server': 'hpc', 'source_job': long_job, 'mode': 'mesh',
         'job_name': 'si-disc-none', 'meshes': [[8, 8, 8], [10, 10, 10], [12, 12, 12]]},
    )
    assert not_created['ok'] is False and not_created['error_code'] == 'SCAN_RETRY_DISCONNECTED_NOT_CREATED'
    assert task_contract.read_ledger(ws, cid)['scan_retries_used'] == 1

    # Disconnect Case 2: SSH dropped leaving a PARTIAL directory (no manifest.json) -> SCAN_RETRY_RESULT_UNCERTAIN.
    def _partial_drop(_jn, folder, _kw):
        folder.mkdir(parents=True)
        raise core.ToolError('REMOTE_TIMEOUT', 'ssh timed out mid-prepare')

    cluster.prepare_retry_hook = _partial_drop
    partial = maker.call(
        'vasp_prepare_scan_retry',
        {'contract_id': cid, 'server': 'hpc', 'source_job': long_job, 'mode': 'mesh',
         'job_name': 'si-disc-partial', 'meshes': [[8, 8, 8], [10, 10, 10], [12, 12, 12]]},
    )
    assert partial['ok'] is False and partial['error_code'] == 'SCAN_RETRY_RESULT_UNCERTAIN'
    assert task_contract.read_ledger(ws, cid)['scan_retries_used'] == 1

    # Disconnect Case 3: Remote finished creating full directory, then SSH dropped -> recovered=True cleanly!
    def _created_then_drop(jn, folder, kw):
        with contextlib.redirect_stdout(io.StringIO()):
            convergence_reuse.prepare_retry(
                cluster.job_folder(kw['source_job']), folder, kw['mode'], kw.get('encuts'), kw.get('meshes'),
                fixed_encut=kw.get('fixed_encut'), scan_thresholds=kw.get('scan_thresholds'), scan_preset=kw.get('scan_preset'),
            )
        cluster._write_remote_meta(folder, jn)
        raise core.ToolError('REMOTE_RESPONSE_INVALID', 'truncated stdout after commit')

    cluster.prepare_retry_hook = _created_then_drop
    recovered = maker.call(
        'vasp_prepare_scan_retry',
        {'contract_id': cid, 'server': 'hpc', 'source_job': long_job, 'mode': 'mesh',
         'job_name': 'si-disc-ok', 'meshes': [[8, 8, 8], [10, 10, 10], [12, 12, 12]]},
    )
    assert recovered['ok'] is True, recovered
    assert recovered['result']['recovered'] is True and recovered['result']['job_name'] == 'si-disc-ok'
    assert 'si-disc-ok' in task_contract.read_ledger(ws, cid)['allowed_jobs']
    assert (cluster.job_folder('si-disc-ok') / 'physics-model.json').is_file()


# ------------------- 8. Scope, in_flight, env profile drift, and executable drift guards


def test_prepare_scan_retry_enforces_scope_in_flight_and_env_executable_drift(tmp_path, monkeypatch):
    ws = _setup_workspace(tmp_path / 'ws')
    cluster = _install_fake_remote(monkeypatch, ws, tmp_path / 'remote')
    maker, contract, _ = _draft_and_approve(
        ws, monkeypatch, server='hpc', job_name='si-guard',
        encuts=[400, 460, 520], meshes=[[4, 4, 4], [6, 6, 6], [8, 8, 8]],
    )
    cid = contract['contract_id']
    assert maker.call('vasp_prepare_task', {'contract_id': cid})['ok'] is True
    _populate_convergence_outputs(cluster.job_folder('si-guard'), encut_energies=(-5.0, -5.0001, -5.0002), mesh_energies=(-5.0, -5.020, -5.040))

    # Unlisted source_job rejected with CONTRACT_SCOPE_VIOLATION.
    bad_scope = maker.call(
        'vasp_prepare_scan_retry',
        {'contract_id': cid, 'server': 'hpc', 'source_job': 'other-job', 'mode': 'mesh',
         'meshes': [[8, 8, 8], [10, 10, 10], [12, 12, 12]]},
    )
    assert bad_scope['ok'] is False and bad_scope['error_code'] == 'CONTRACT_SCOPE_VIOLATION'

    # Active in_flight submission blocks scan retry.
    task_contract.reserve_submission(
        ws, cid, server='hpc', job_name='si-guard', phase='convergence',
        resources=contract['resource_envelope']['phase_defaults']['convergence'], operation_id='a' * 32,
    )
    in_flight = maker.call(
        'vasp_prepare_scan_retry',
        {'contract_id': cid, 'server': 'hpc', 'source_job': 'si-guard', 'mode': 'mesh',
         'meshes': [[8, 8, 8], [10, 10, 10], [12, 12, 12]]},
    )
    assert in_flight['ok'] is False and in_flight['error_code'] == 'SUBMISSION_IN_FLIGHT'
    task_contract.release_in_flight(ws, cid, operation_id='a' * 32, refund=True, reason='test')

    # Remote executable drift during prepare_scan_retry auto-halts contract with ENVIRONMENT_PROFILE_DRIFT.
    cluster.prepare_retry_hook = lambda _jn, _f, _kw: (_ for _ in ()).throw(
        core.ToolError('REMOTE_ERROR', '来源任务VASP可执行文件不同，不能复用收敛证据。')
    )
    drift = maker.call(
        'vasp_prepare_scan_retry',
        {'contract_id': cid, 'server': 'hpc', 'source_job': 'si-guard', 'mode': 'mesh',
         'meshes': [[8, 8, 8], [10, 10, 10], [12, 12, 12]]},
    )
    assert drift['ok'] is False and drift['error_code'] == 'ENVIRONMENT_PROFILE_DRIFT'
    ledger = task_contract.read_ledger(ws, cid)
    assert ledger['status'] == 'halted' and ledger['halt_code'] == 'ENVIRONMENT_PROFILE_DRIFT'

    # Bound environment_profile hash drift auto-halts before remote calls.
    monkeypatch.setattr(
        environment_profiles, 'by_name',
        lambda _ws, _srv, name: {'name': name, 'environment_sha256': '1' * 64, 'executable_sha256': '2' * 64},
    )
    maker2, ctr2, _ = _draft_and_approve(ws, monkeypatch, server='hpc', job_name='si-env')
    # Inject bound environment_profile directly into contract + re-sign canonical sha for test coverage.
    cid2 = ctr2['contract_id']
    ctr_path = ws / f'.agent/state/contracts/active/{cid2}.json'
    doc = json.loads(ctr_path.read_text(encoding='utf-8'))
    doc['scope']['environment_profile'] = {'name': 'vasp544', 'environment_sha256': '9' * 64, 'executable_sha256': '8' * 64}
    import hashlib
    new_sha = hashlib.sha256(task_contract.canonical_bytes(doc)).hexdigest()
    doc['approval']['canonical_sha256'] = new_sha
    ctr_path.write_text(json.dumps(doc, ensure_ascii=False, indent=2) + '\n', encoding='utf-8', newline='\n')
    led_path = ws / f'.agent/state/contracts/ledger/{cid2}.json'
    led = json.loads(led_path.read_text(encoding='utf-8'))
    led['canonical_sha256'] = new_sha
    led_path.write_text(json.dumps(led, ensure_ascii=False, indent=2) + '\n', encoding='utf-8', newline='\n')

    env_drift = maker2.call(
        'vasp_prepare_scan_retry',
        {'contract_id': cid2, 'server': 'hpc', 'source_job': 'si-env', 'mode': 'mesh',
         'meshes': [[8, 8, 8], [10, 10, 10], [12, 12, 12]]},
    )
    assert env_drift['ok'] is False and env_drift['error_code'] == 'ENVIRONMENT_PROFILE_DRIFT'
    assert task_contract.read_ledger(ws, cid2)['status'] == 'halted'


# ------------------- 9. Failed cross_cutoff guards, ceiling halt, enmax_eV floor, and remote live/contract guards


def test_prepare_scan_retry_cross_cutoff_failure_blocks_encut_lower_fixed_and_halts_at_mesh_ceiling(tmp_path, monkeypatch):
    ws = _setup_workspace(tmp_path / 'ws')
    cluster = _install_fake_remote(monkeypatch, ws, tmp_path / 'remote')
    maker, contract, _ = _draft_and_approve(
        ws, monkeypatch, server='hpc', job_name='si-cross',
        encuts=[400, 460, 520], meshes=[[4, 4, 4], [6, 6, 6], [8, 8, 8]],
        precision_bounds={'max_encut': 700, 'max_kmesh_axis': 12, 'max_kmesh_points': 1728},
        budget={'max_scan_retries': 3},
    )
    cid = contract['contract_id']
    assert maker.call('vasp_prepare_task', {'contract_id': cid})['ok'] is True
    _populate_convergence_outputs(
        cluster.job_folder('si-cross'), encut_energies=(-5.0, -5.0002, -5.0003), mesh_energies=(-5.0, -5.020, -5.040),
    )

    # First retry with fixed_encut=460 right up to mesh ceiling [12,12,12].
    r1 = maker.call(
        'vasp_prepare_scan_retry',
        {'contract_id': cid, 'server': 'hpc', 'source_job': 'si-cross', 'mode': 'mesh',
         'meshes': [[8, 8, 8], [10, 10, 10], [12, 12, 12]], 'fixed_encut': 460},
    )
    assert r1['ok'] is True and r1['result']['job_name'] == 'si-cross_r1'

    # Populate si-cross_r1: mesh passed, cross-cutoff FAILED (diff 20 meV/atom)!
    _populate_convergence_outputs(
        cluster.job_folder('si-cross_r1'), mesh_energies=(-5.0, -5.0002, -5.0003), cross_energy=-5.0203,
    )
    # 1. mode='encut' MUST be blocked (cannot reuse unconverged fixed_encut mesh evidence).
    bad_encut = maker.call(
        'vasp_prepare_scan_retry',
        {'contract_id': cid, 'server': 'hpc', 'source_job': 'si-cross_r1', 'mode': 'encut', 'encuts': [520, 580, 640]},
    )
    assert bad_encut['ok'] is False and bad_encut['error_code'] == 'SCAN_RETRY_MODE_MISMATCH'

    # 2. Lowering or repeating fixed_encut (400 <= 460) when cross_cutoff failed MUST be rejected.
    # (Using a non-ceiling contract first to test fixed_encut <= source_fixed_encut without ceiling halt.)
    maker2, ctr2, _ = _draft_and_approve(
        ws, monkeypatch, server='hpc', job_name='si-cross2',
        encuts=[400, 460, 520], meshes=[[4, 4, 4], [6, 6, 6], [8, 8, 8]],
        precision_bounds={'max_encut': 700, 'max_kmesh_axis': 16, 'max_kmesh_points': 4096},
        budget={'max_scan_retries': 3},
    )
    cid2 = ctr2['contract_id']
    assert maker2.call('vasp_prepare_task', {'contract_id': cid2})['ok'] is True
    _populate_convergence_outputs(
        cluster.job_folder('si-cross2'), encut_energies=(-5.0, -5.0002, -5.0003), mesh_energies=(-5.0, -5.020, -5.040),
    )
    assert maker2.call(
        'vasp_prepare_scan_retry',
        {'contract_id': cid2, 'server': 'hpc', 'source_job': 'si-cross2', 'mode': 'mesh',
         'meshes': [[8, 8, 8], [10, 10, 10], [12, 12, 12]], 'fixed_encut': 460},
    )['ok'] is True
    _populate_convergence_outputs(
        cluster.job_folder('si-cross2_r1'), mesh_energies=(-5.0, -5.0002, -5.0003), cross_energy=-5.0203,
    )
    for bad_fe in (400, 460):
        res_fe = maker2.call(
            'vasp_prepare_scan_retry',
            {'contract_id': cid2, 'server': 'hpc', 'source_job': 'si-cross2_r1', 'mode': 'mesh',
             'meshes': [[12, 12, 12], [14, 14, 14], [16, 16, 16]], 'fixed_encut': bad_fe},
        )
        assert res_fe['ok'] is False and res_fe['error_code'] == 'CONTRACT_RETRY_NOT_STRICTER'

    # 3. On si-cross_r1 where mesh is ALREADY at ceiling [12,12,12], failed cross_cutoff MUST auto-halt contract!
    halted_cross = maker.call(
        'vasp_prepare_scan_retry',
        {'contract_id': cid, 'server': 'hpc', 'source_job': 'si-cross_r1', 'mode': 'mesh',
         'meshes': [[10, 10, 10], [12, 12, 12], [14, 14, 14]]},
    )
    assert halted_cross['ok'] is False and halted_cross['error_code'] == 'CONTRACT_PRECISION_BOUND_EXCEEDED'
    assert task_contract.read_ledger(ws, cid)['status'] == 'halted'


def test_prepare_scan_retry_enmax_ev_floor_mesh_ceilings_and_chained_r2_thresholds(tmp_path, monkeypatch):
    ws = _setup_workspace(tmp_path / 'ws')
    cluster = _install_fake_remote(monkeypatch, ws, tmp_path / 'remote')
    maker, contract, _ = _draft_and_approve(
        ws, monkeypatch, server='hpc', job_name='si-floor',
        encuts=[400, 460, 520], meshes=[[4, 4, 4], [6, 6, 6], [8, 8, 8]],
        precision_bounds={'max_encut': 700, 'max_kmesh_axis': 12, 'max_kmesh_points': 1728},
        budget={'max_scan_retries': 3},
    )
    cid = contract['contract_id']
    assert maker.call('vasp_prepare_task', {'contract_id': cid})['ok'] is True
    src = cluster.job_folder('si-floor')
    _populate_convergence_outputs(src, encut_energies=(-5.0, -5.020, -5.040), mesh_energies=(-5.0, -5.0001, -5.0002))

    # Delete local scan plan file: potential_floor MUST still be computed as 319 eV from potential-selection.json (`enmax_eV`)!
    plan_file = agent_paths.store_dir(ws, 'scan_plans') / f'{contract["workflow"]["plan_id"]}.json'
    plan_file.unlink()
    below_meta = maker.call(
        'vasp_prepare_scan_retry',
        {'contract_id': cid, 'server': 'hpc', 'source_job': 'si-floor', 'mode': 'encut', 'encuts': [300, 460, 580]},
    )
    assert below_meta['ok'] is False and below_meta['error_code'] == 'CONTRACT_RETRY_NOT_STRICTER'

    # Chained retry _r1 -> _r2 preserves tightened scan_thresholds automatically!
    r1 = maker.call(
        'vasp_prepare_scan_retry',
        {'contract_id': cid, 'server': 'hpc', 'source_job': 'si-floor', 'mode': 'encut',
         'encuts': [520, 560, 600], 'scan_thresholds': {'energy_eV_per_atom': 0.0005}},
    )
    assert r1['ok'] is True and r1['result']['job_name'] == 'si-floor_r1' and r1['result']['potential_floor'] == 319.0
    _populate_convergence_outputs(cluster.job_folder('si-floor_r1'), encut_energies=(-5.0, -5.020, -5.040))
    r2 = maker.call(
        'vasp_prepare_scan_retry',
        {'contract_id': cid, 'server': 'hpc', 'source_job': 'si-floor_r1', 'mode': 'encut', 'encuts': [600, 650, 700]},
    )
    assert r2['ok'] is True, r2
    assert r2['result']['job_name'] == 'si-floor_r2'
    assert r2['result']['scan_thresholds'] == {'energy_eV_per_atom': 0.0005}
    r2_cfg = json.loads((cluster.job_folder('si-floor_r2') / 'config.json').read_text(encoding='utf-8'))
    assert r2_cfg['scan_thresholds'] == {'energy_eV_per_atom': 0.0005}

    # 3D mesh ceiling auto-halt (max_kmesh_axis=8, top mesh=[8,8,8] failed).
    maker3, ctr3, _ = _draft_and_approve(
        ws, monkeypatch, server='hpc', job_name='si-mceil',
        encuts=[400, 460, 520], meshes=[[4, 4, 4], [6, 6, 6], [8, 8, 8]],
        precision_bounds={'max_encut': 700, 'max_kmesh_axis': 8, 'max_kmesh_points': 512},
    )
    cid3 = ctr3['contract_id']
    assert maker3.call('vasp_prepare_task', {'contract_id': cid3})['ok'] is True
    _populate_convergence_outputs(cluster.job_folder('si-mceil'), encut_energies=(-5.0, -5.0001, -5.0002), mesh_energies=(-5.0, -5.020, -5.040))
    m_halt = maker3.call(
        'vasp_prepare_scan_retry',
        {'contract_id': cid3, 'server': 'hpc', 'source_job': 'si-mceil', 'mode': 'mesh',
         'meshes': [[8, 8, 8], [10, 10, 10], [12, 12, 12]]},
    )
    assert m_halt['ok'] is False and m_halt['error_code'] == 'CONTRACT_PRECISION_BOUND_EXCEEDED'
    assert task_contract.read_ledger(ws, cid3)['status'] == 'halted'

    # 2D slab mesh ceiling auto-halt (max_kmesh_axis=8, top=[8,8,1] cannot tighten without breaking kz==1 or axis limit).
    ws2d = _setup_workspace(tmp_path / 'ws2d-ceil', slab_2d=True)
    cluster2d = _install_fake_remote(monkeypatch, ws2d, tmp_path / 'remote2d-ceil')
    maker2d, ctr2d, _ = _draft_and_approve(
        ws2d, monkeypatch, server='hpc', job_name='si2d-ceil', structure='structures/Si2D.POSCAR',
        relax_mode='ions', mesh=[4, 4, 1], encuts=[400, 460, 520], meshes=[[4, 4, 1], [6, 6, 1], [8, 8, 1]],
        precision_bounds={'max_encut': 700, 'max_kmesh_axis': 8, 'max_kmesh_points': 100},
    )
    cid2d = ctr2d['contract_id']
    assert maker2d.call('vasp_prepare_task', {'contract_id': cid2d})['ok'] is True
    _populate_convergence_outputs(cluster2d.job_folder('si2d-ceil'), encut_energies=(-5.0, -5.0001, -5.0002), mesh_energies=(-5.0, -5.020, -5.040))
    m2d_halt = maker2d.call(
        'vasp_prepare_scan_retry',
        {'contract_id': cid2d, 'server': 'hpc', 'source_job': 'si2d-ceil', 'mode': 'mesh',
         'meshes': [[6, 6, 1], [8, 8, 1], [10, 10, 1]]},
    )
    assert m2d_halt['ok'] is False and m2d_halt['error_code'] == 'CONTRACT_PRECISION_BOUND_EXCEEDED'
    assert task_contract.read_ledger(ws2d, cid2d)['status'] == 'halted'


def test_prepare_scan_retry_remote_live_guards_disconnect_hardening_and_post_prepare_paths(tmp_path, monkeypatch):
    ws = _setup_workspace(tmp_path / 'ws')
    cluster = _install_fake_remote(monkeypatch, ws, tmp_path / 'remote')
    maker, contract, _ = _draft_and_approve(
        ws, monkeypatch, server='hpc', job_name='si-hard',
        encuts=[400, 460, 520], meshes=[[4, 4, 4], [6, 6, 6], [8, 8, 8]],
        budget={'max_scan_retries': 4},
    )
    cid = contract['contract_id']
    assert maker.call('vasp_prepare_task', {'contract_id': cid})['ok'] is True
    src = cluster.job_folder('si-hard')
    _populate_convergence_outputs(src, encut_energies=(-5.0, -5.0001, -5.0002), mesh_energies=(-5.0, -5.020, -5.040))

    # Remote live guards: non-terminal array, unknown scheduler, non-submitted receipt.
    cluster.status_hook = lambda _jn, _f, _kw: {'array_tasks': {'all_terminal': False}, 'scheduler_status_known': True}
    assert maker.call(
        'vasp_prepare_scan_retry',
        {'contract_id': cid, 'server': 'hpc', 'source_job': 'si-hard', 'mode': 'mesh', 'meshes': [[8, 8, 8], [10, 10, 10], [12, 12, 12]]},
    )['error_code'] == 'SCAN_RETRY_SOURCE_RUNNING'

    cluster.status_hook = lambda _jn, _f, _kw: {'scheduler_status_known': False}
    assert maker.call(
        'vasp_prepare_scan_retry',
        {'contract_id': cid, 'server': 'hpc', 'source_job': 'si-hard', 'mode': 'mesh', 'meshes': [[8, 8, 8], [10, 10, 10], [12, 12, 12]]},
    )['error_code'] == 'SCAN_RETRY_SOURCE_UNCERTAIN'

    cluster.status_hook = lambda _jn, _f, _kw: {'stage_submissions': {'convergence': {'status': 'corrupt_receipt'}}}
    assert maker.call(
        'vasp_prepare_scan_retry',
        {'contract_id': cid, 'server': 'hpc', 'source_job': 'si-hard', 'mode': 'mesh', 'meshes': [[8, 8, 8], [10, 10, 10], [12, 12, 12]]},
    )['error_code'] == 'SCAN_RETRY_SOURCE_UNCERTAIN'
    cluster.status_hook = None

    # Disconnect hardening 1: status probe fails with SSH_FAILED containing '不存在' -> MUST be UNCERTAIN, NOT NOT_CREATED!
    cluster.prepare_retry_hook = lambda _jn, _f, _kw: (_ for _ in ()).throw(core.ToolError('SSH_FAILED', 'drop'))
    cluster.status_hook = lambda jn, _f, _kw: (
        (_ for _ in ()).throw(core.ToolError('SSH_FAILED', 'ssh key 不存在')) if jn == 'si-ssh-ambig' else {}
    )
    ssh_ambig = maker.call(
        'vasp_prepare_scan_retry',
        {'contract_id': cid, 'server': 'hpc', 'source_job': 'si-hard', 'mode': 'mesh',
         'job_name': 'si-ssh-ambig', 'meshes': [[8, 8, 8], [10, 10, 10], [12, 12, 12]]},
    )
    assert ssh_ambig['ok'] is False and ssh_ambig['error_code'] == 'SCAN_RETRY_RESULT_UNCERTAIN'
    cluster.status_hook = None

    # Disconnect hardening 2: pre-existing foreign/submitted directory with matching encuts/meshes -> MUST be UNCERTAIN!
    foreign = cluster.job_folder('si-foreign')
    with contextlib.redirect_stdout(io.StringIO()):
        convergence_reuse.prepare_retry(src, foreign, 'mesh', None, [[8, 8, 8], [10, 10, 10], [12, 12, 12]], scan_preset='standard')
    cluster._write_remote_meta(foreign, 'si-foreign')
    (foreign / 'submissions').mkdir(exist_ok=True)
    core.save(foreign / 'submissions/convergence.json', {'status': 'submitted', 'job_id': '9999', 'phase': 'convergence'})
    cluster.list_jobs_override = []
    foreign_res = maker.call(
        'vasp_prepare_scan_retry',
        {'contract_id': cid, 'server': 'hpc', 'source_job': 'si-hard', 'mode': 'mesh',
         'job_name': 'si-foreign', 'meshes': [[8, 8, 8], [10, 10, 10], [12, 12, 12]]},
    )
    assert foreign_res['ok'] is False and foreign_res['error_code'] == 'SCAN_RETRY_RESULT_UNCERTAIN'
    assert 'si-foreign' not in task_contract.read_ledger(ws, cid)['allowed_jobs']
    cluster.list_jobs_override = None

    # Exhausting remote name collisions reaches SCAN_RETRY_NAME_CONFLICT cleanly.
    cluster.prepare_retry_hook = lambda _jn, _f, _kw: (_ for _ in ()).throw(core.ToolError('REMOTE_ERROR', '重试必须使用不存在的新任务目录。'))
    exhausted = maker.call(
        'vasp_prepare_scan_retry',
        {'contract_id': cid, 'server': 'hpc', 'source_job': 'si-hard', 'mode': 'mesh', 'meshes': [[8, 8, 8], [10, 10, 10], [12, 12, 12]]},
    )
    assert exhausted['ok'] is False and exhausted['error_code'] == 'SCAN_RETRY_NAME_CONFLICT'
    cluster.prepare_retry_hook = None

    # Post-prepare failure 1: confirm_physics failure -> SCAN_RETRY_POST_PREPARE_FAILED, NOT added to allowed_jobs.
    cluster.confirm_physics_hook = lambda _jn, _f, _kw: (_ for _ in ()).throw(core.ToolError('SSH_FAILED', 'confirm drop'))
    cp_fail = maker.call(
        'vasp_prepare_scan_retry',
        {'contract_id': cid, 'server': 'hpc', 'source_job': 'si-hard', 'mode': 'mesh',
         'job_name': 'si-cp-fail', 'meshes': [[8, 8, 8], [10, 10, 10], [12, 12, 12]]},
    )
    assert cp_fail['ok'] is False and cp_fail['error_code'] == 'SCAN_RETRY_POST_PREPARE_FAILED'
    assert 'si-cp-fail' not in task_contract.read_ledger(ws, cid)['allowed_jobs']
    cluster.confirm_physics_hook = None

    # Post-prepare failure 2: non-drift configure_resources failure -> succeeds with resource_warning (no lost job_name).
    cluster.configure_resources_hook = lambda _jn, _f, _kw: (_ for _ in ()).throw(core.ToolError('REMOTE_TIMEOUT', 'cfg timeout'))
    res_warn = maker.call(
        'vasp_prepare_scan_retry',
        {'contract_id': cid, 'server': 'hpc', 'source_job': 'si-hard', 'mode': 'mesh',
         'job_name': 'si-res-warn', 'meshes': [[8, 8, 8], [10, 10, 10], [12, 12, 12]]},
    )
    assert res_warn['ok'] is True and res_warn['result']['resources_applied'] is False
    assert 'resource_warning' in res_warn['result']
    assert 'si-res-warn' in task_contract.read_ledger(ws, cid)['allowed_jobs']

    # Post-prepare failure 3: drift during configure_resources -> auto-halts contract!
    cluster.configure_resources_hook = lambda _jn, _f, _kw: (_ for _ in ()).throw(core.ToolError('SCRIPT_HASH_DRIFT', '提交脚本已改变'))
    res_drift = maker.call(
        'vasp_prepare_scan_retry',
        {'contract_id': cid, 'server': 'hpc', 'source_job': 'si-hard', 'mode': 'mesh',
         'job_name': 'si-res-drift', 'meshes': [[8, 8, 8], [10, 10, 10], [12, 12, 12]]},
    )
    assert res_drift['ok'] is False and res_drift['error_code'] == 'SCRIPT_HASH_DRIFT'
    assert task_contract.read_ledger(ws, cid)['status'] == 'halted'
    cluster.configure_resources_hook = None


def test_prepare_scan_retry_source_contract_binding_mismatches_auto_halt(tmp_path, monkeypatch):
    ws = _setup_workspace(tmp_path / 'ws')
    cluster = _install_fake_remote(monkeypatch, ws, tmp_path / 'remote')
    maker, contract, _ = _draft_and_approve(
        ws, monkeypatch, server='hpc', job_name='si-bind',
        encuts=[400, 460, 520], meshes=[[4, 4, 4], [6, 6, 6], [8, 8, 8]], scan_preset='strict',
    )
    cid = contract['contract_id']
    assert maker.call('vasp_prepare_task', {'contract_id': cid})['ok'] is True
    src = cluster.job_folder('si-bind')
    _populate_convergence_outputs(src, encut_energies=(-5.0, -5.0001, -5.0002), mesh_energies=(-5.0, -5.020, -5.040))

    # Source config tampered to looser scan_preset='standard' under a 'strict' contract -> auto-halt CONTRACT_TAMPERED!
    orig_ctx = input_review.context(src)
    looser_ctx = {**orig_ctx, 'config': {**orig_ctx['config'], 'scan_preset': 'standard'}}
    cluster.review_context_hook = lambda _jn, _f, _kw: looser_ctx
    res = maker.call(
        'vasp_prepare_scan_retry',
        {'contract_id': cid, 'server': 'hpc', 'source_job': 'si-bind', 'mode': 'mesh',
         'meshes': [[8, 8, 8], [10, 10, 10], [12, 12, 12]]},
    )
    assert res['ok'] is False and res['error_code'] == 'CONTRACT_TAMPERED'
    assert task_contract.read_ledger(ws, cid)['status'] == 'halted'

