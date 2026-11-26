"""Offline tests for P1a: autonomy agent_paths keys, contract kernel, ledger lock, SSH verify, and AST guard."""
from __future__ import annotations

import ast
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
import io
import json
from pathlib import Path
import shutil
import subprocess
from types import SimpleNamespace

import pytest

from vasp_agent.common import agent_paths, workspace_migration
from vasp_agent.permissions import contract_ledger, contract_rules, task_contract
from vasp_agent.permissions.task_contract import (
    ContractError,
    InWindowApprovalProvider,
    LocalTTYApprovalProvider,
)

ROOT = Path(__file__).resolve().parents[1]
SHA64_A = 'a' * 64
SHA64_B = 'b' * 64
SHA64_C = 'c' * 64
PLAN32 = '1' * 32


def _now() -> datetime:
    return datetime(2026, 11, 26, 9, 0, 0, tzinfo=timezone.utc)


def _sample_draft(
    *,
    draft_id: str = 'drf-20261126-9f8e7d6c',
    server: str | None = 'hpc',
    job_name: str = 'si-auto-01',
    dimension: str = '3D',
    review_mode: str = 'maker_checker',
    allow_forks: bool = False,
) -> dict:
    created = _now()
    expires = created + timedelta(days=7)
    conv_res = {
        'submission_mode': 'array' if server is not None else 'allocation',
        'parallel_tests': 3 if server is not None else 1,
        'cores_per_test': 16,
        'nodes': 1,
        'partition': 'compute',
        'walltime': '02:00:00',
    }
    alloc_res = {
        'submission_mode': 'allocation',
        'parallel_tests': 1,
        'cores_per_test': 16,
        'nodes': 1,
        'partition': 'compute',
        'walltime': '02:00:00',
    }
    return {
        'contract_version': 1,
        'draft_id': draft_id,
        'created_at': contract_rules.format_utc(created),
        'expires_at': contract_rules.format_utc(expires),
        'scope': {
            'server': server,
            'job_name': job_name,
            'structure': 'structures/Si.POSCAR',
            'structure_sha256': SHA64_A,
            'dimension': dimension,
            'environment_profile': {
                'name': 'default',
                'environment_sha256': SHA64_B,
                'executable_sha256': SHA64_C,
            } if server is not None else None,
            'allow_forks': allow_forks,
        },
        'physics_assertion': {
            'goal': 'Si 3D 体相非磁性 PBE 收敛测试、晶胞优化、SCF、能带与 DOS 端到端计算',
            'magnetism': 'nonmagnetic',
            'charge': 0,
            'functional': 'PBE',
            'soc': False,
            'hubbard_u': False,
            'hybrid': False,
            'confirmed_by': 'operator_slash_command',
            'assumptions_source': 'operator_assertion_not_structure_derived',
        },
        'workflow': {
            'plan_id': PLAN32,
            'plan_sha256': SHA64_A,
            'allowed_phases': ['convergence', 'relaxation', 'scf', 'bands', 'dos'],
            'scan_preset': 'standard',
            'allow_threshold_relaxation': False,
        },
        'precision_bounds': {
            'max_encut': 700,
            'max_kmesh_axis': 16,
            'max_kmesh_points': 4096,
        },
        'resource_envelope': {
            'allowed_modes': ['allocation', 'array'] if server is not None else ['allocation'],
            'allowed_partitions': ['compute'],
            'max_parallel_tests': 3,
            'max_cores_per_test': 16,
            'max_nodes': 1,
            'max_total_cores': 48,
            'max_walltime': '04:00:00',
            'phase_defaults': {
                'convergence': conv_res,
                'relaxation': {**alloc_res, 'walltime': '04:00:00'},
                'scf': alloc_res,
                'bands': alloc_res,
                'dos': alloc_res,
            },
        },
        'budget': {
            'max_submissions': 10,
            'max_resume_retries': 2,
            'max_repair_retries': 3,
            'max_scan_retries': 1,
            'max_consecutive_failures': 3,
        },
        'review_policy': {
            'mode': review_mode,
            'auto_advance_on_green': True,
        },
    }


def _tty_provider(monkeypatch, answer: str = '1') -> LocalTTYApprovalProvider:
    monkeypatch.setattr(task_contract, 'has_real_tty', lambda stream=None: True)
    shown: list[str] = []
    return LocalTTYApprovalProvider(reader=lambda _prompt: answer, output=shown.append)


def _approved_contract(tmp_path: Path, monkeypatch, **draft_kwargs) -> tuple[dict, dict]:
    draft = _sample_draft(**draft_kwargs)
    task_contract.save_draft(tmp_path, draft)
    provider = _tty_provider(monkeypatch, '1')
    return task_contract.approve_draft(tmp_path, draft['draft_id'], provider=provider, now=_now())


# ------------------------------------------------------------ 1. agent_paths

def test_agent_paths_autonomy_keys_never_fallback_and_preserve_migration(tmp_path):
    assert agent_paths.store_dir(tmp_path, 'contracts') == agent_paths.agent_path(tmp_path, 'state/contracts')
    assert agent_paths.store_dir(tmp_path, 'review_attestations') == agent_paths.agent_path(
        tmp_path, 'state/review-attestations'
    )
    assert agent_paths.file_path(tmp_path, 'monitor_events') == agent_paths.agent_path(
        tmp_path, 'state/monitor-events.jsonl'
    )
    assert agent_paths.file_path(tmp_path, 'autonomy_policy') == agent_paths.agent_path(
        tmp_path, 'state/autonomy-policy.json'
    )
    assert agent_paths.legacy_store_dir(tmp_path, 'contracts') is None
    assert agent_paths.legacy_store_dir(tmp_path, 'review_attestations') is None

    # Root spoofed files/dirs must never be picked up or crash workspace_migration.
    (tmp_path / '.contracts').mkdir()
    (tmp_path / '.contracts' / 'spoof.json').write_text('{}', encoding='utf-8')
    (tmp_path / '.autonomy-policy.json').write_text('{"require_ssh_signature": false}', encoding='utf-8')
    (tmp_path / '.agent-settings.json').write_text('{"max_steps": 12}', encoding='utf-8')

    assert agent_paths.store_items(tmp_path, 'contracts', '*.json') == []
    assert agent_paths.store_item(tmp_path, 'contracts', 'x.json') == (
        agent_paths.agent_path(tmp_path, 'state/contracts') / 'x.json'
    )
    plan = workspace_migration.plan(tmp_path)
    moved_sources = {m['source'].name for m in plan['moves']}
    assert moved_sources == {'.agent-settings.json'}


# ---------------------------------------------- 2. Schema, physics, envelope

def test_canonical_hash_is_stable_and_sensitive_to_every_policy_field(tmp_path, monkeypatch):
    contract, _ = _approved_contract(tmp_path, monkeypatch)
    expected = contract['approval']['canonical_sha256']
    assert len(expected) == 64
    assert contract_rules.canonical_sha256(contract) == expected

    # Reordered keys & volatile signature metadata do not change canonical hash.
    reordered = json.loads(json.dumps(contract, sort_keys=False))
    reordered['approval']['ssh_signature'] = {'sig_file': 'x.sig', 'principal': 'op'}
    assert contract_rules.canonical_sha256(reordered) == expected

    # Changing any governed field changes canonical hash.
    for mutator in (
        lambda d: d['budget'].__setitem__('max_submissions', 11),
        lambda d: d['precision_bounds'].__setitem__('max_encut', 750),
        lambda d: d.__setitem__('expires_at', contract_rules.format_utc(_now() + timedelta(days=30))),
        lambda d: d['review_policy'].__setitem__('mode', 'maker_direct'),
    ):
        mutated = json.loads(json.dumps(contract))
        mutator(mutated)
        assert contract_rules.canonical_sha256(mutated) != expected


@pytest.mark.parametrize(
    ('field', 'bad_value'),
    [
        ('magnetism', 'collinear'),
        ('charge', 1),
        ('charge', False),
        ('functional', 'HSE06'),
        ('soc', True),
        ('hubbard_u', True),
        ('hybrid', True),
        ('confirmed_by', 'ai_model'),
        ('assumptions_source', 'structure_derived'),
    ],
)
def test_unsupported_physics_assertion_is_hard_rejected(field, bad_value):
    draft = _sample_draft()
    draft['physics_assertion'][field] = bad_value
    with pytest.raises(ContractError) as exc:
        contract_rules.validate_contract_schema(draft, require_approval=False)
    assert exc.value.error_code == 'CONTRACT_UNSUPPORTED_PHYSICS'


def test_unsupported_dimension_and_threshold_relaxation_and_bad_paths_rejected():
    for bad_dim in ('1D', '0D', '4D'):
        draft = _sample_draft(dimension=bad_dim)
        with pytest.raises(ContractError) as exc:
            contract_rules.validate_contract_schema(draft, require_approval=False)
        assert exc.value.error_code == 'CONTRACT_UNSUPPORTED_PHYSICS'

    draft_relax = _sample_draft()
    draft_relax['workflow']['allow_threshold_relaxation'] = True
    with pytest.raises(ContractError) as exc:
        contract_rules.validate_contract_schema(draft_relax, require_approval=False)
    assert exc.value.error_code == 'CONTRACT_THRESHOLD_RELAXATION_FORBIDDEN'

    for bad_struct in ('/etc/passwd', '../Si.POSCAR', 'C:\\Si.POSCAR', 'structures\\Si.POSCAR'):
        draft_path = _sample_draft()
        draft_path['scope']['structure'] = bad_struct
        with pytest.raises(ContractError) as exc:
            contract_rules.validate_contract_schema(draft_path, require_approval=False)
        assert exc.value.error_code == 'CONTRACT_SCHEMA_INVALID'


def test_resource_envelope_validation_catches_all_out_of_bounds_requests():
    draft = _sample_draft()
    env = draft['resource_envelope']
    base = env['phase_defaults']['convergence']
    contract_rules.validate_resources_in_envelope(base, 'convergence', env, server='hpc', tests=6)

    cases = [
        ({**base, 'partition': 'gpu'}, 'convergence', 'hpc'),
        ({**base, 'parallel_tests': 4}, 'convergence', 'hpc'),
        ({**base, 'cores_per_test': 32, 'parallel_tests': 1}, 'convergence', 'hpc'),
        ({**base, 'nodes': 2}, 'convergence', 'hpc'),
        ({**base, 'cores_per_test': 15, 'nodes': 2}, 'convergence', 'hpc'),
        ({**base, 'walltime': '04:00:01'}, 'convergence', 'hpc'),
        ({**base, 'submission_mode': 'array'}, 'relaxation', 'hpc'),
        ({**base, 'submission_mode': 'array', 'parallel_tests': 1}, 'convergence', None),
        ({**base, 'submission_mode': 'allocation', 'parallel_tests': 2}, 'scf', 'hpc'),
    ]
    for bad_res, phase, server in cases:
        with pytest.raises(ContractError) as exc:
            contract_rules.validate_resources_in_envelope(bad_res, phase, env, server=server, tests=6)
        assert exc.value.error_code == 'CONTRACT_RESOURCE_ENVELOPE_EXCEEDED'

    # Tighter max_total_cores than max_parallel_tests * max_cores_per_test must still enforce total cores.
    tight_env = {**env, 'max_total_cores': 32}
    with pytest.raises(ContractError) as exc:
        contract_rules.validate_resources_in_envelope(base, 'convergence', tight_env, server='hpc', tests=6)
    assert exc.value.error_code == 'CONTRACT_RESOURCE_ENVELOPE_EXCEEDED'


# -------------------------------------------- 3. Strict monotonic scan retry

def test_validate_stricter_retry_enforces_monotonicity_bounds_2d_and_no_relaxation(tmp_path, monkeypatch):
    contract, _ = _approved_contract(tmp_path, monkeypatch)
    src_encuts = [450, 520, 600]
    src_meshes = [[4, 4, 4], [6, 6, 6], [8, 8, 8]]

    ok = contract_rules.validate_stricter_retry(
        contract,
        source_encuts=src_encuts,
        source_meshes=src_meshes,
        new_encuts=[520, 600, 680],
        new_meshes=[[6, 6, 6], [8, 8, 8], [10, 10, 10]],
        mode='both',
        potential_floor=400,
    )
    assert ok['encuts'] == [520, 600, 680]
    assert ok['meshes'][-1] == [10, 10, 10]

    # Moving standard -> strict is allowed; strict -> standard is relaxation and forbidden.
    contract_rules.validate_stricter_retry(
        contract,
        source_encuts=src_encuts,
        source_meshes=src_meshes,
        new_encuts=[520, 600, 680],
        mode='encut',
        source_scan_preset='standard',
        scan_preset='strict',
    )
    with pytest.raises(ContractError) as exc:
        contract_rules.validate_stricter_retry(
            contract,
            source_encuts=src_encuts,
            source_meshes=src_meshes,
            new_encuts=[520, 600, 680],
            mode='encut',
            source_scan_preset='strict',
            scan_preset='standard',
        )
    assert exc.value.error_code == 'CONTRACT_THRESHOLD_RELAXATION_FORBIDDEN'

    # Passing looser scan_thresholds is forbidden.
    with pytest.raises(ContractError) as exc:
        contract_rules.validate_stricter_retry(
            contract,
            source_encuts=src_encuts,
            source_meshes=src_meshes,
            new_encuts=[520, 600, 680],
            mode='encut',
            scan_thresholds={'energy_eV_per_atom': 0.005},
        )
    assert exc.value.error_code == 'CONTRACT_THRESHOLD_RELAXATION_FORBIDDEN'

    # ENCUT not strictly higher or exceeding max_encut or below potential_floor rejected.
    with pytest.raises(ContractError) as exc:
        contract_rules.validate_stricter_retry(
            contract, source_encuts=src_encuts, source_meshes=src_meshes,
            new_encuts=[450, 520, 600], mode='encut',
        )
    assert exc.value.error_code == 'CONTRACT_RETRY_NOT_STRICTER'

    with pytest.raises(ContractError) as exc:
        contract_rules.validate_stricter_retry(
            contract, source_encuts=src_encuts, source_meshes=src_meshes,
            new_encuts=[560, 640, 720], mode='encut',
        )
    assert exc.value.error_code == 'CONTRACT_PRECISION_BOUND_EXCEEDED'

    with pytest.raises(ContractError) as exc:
        contract_rules.validate_stricter_retry(
            contract, source_encuts=src_encuts, source_meshes=src_meshes,
            new_encuts=[380, 500, 650], mode='encut', potential_floor=400,
        )
    assert exc.value.error_code == 'CONTRACT_RETRY_NOT_STRICTER'

    # Mesh shrinking an axis, exceeding max_kmesh_axis, or exceeding max_kmesh_points rejected.
    with pytest.raises(ContractError) as exc:
        contract_rules.validate_stricter_retry(
            contract, source_encuts=src_encuts, source_meshes=src_meshes,
            new_meshes=[[4, 4, 4], [5, 8, 8], [10, 10, 10]], mode='mesh',
        )
    assert exc.value.error_code == 'CONTRACT_RETRY_NOT_STRICTER'

    with pytest.raises(ContractError) as exc:
        contract_rules.validate_stricter_retry(
            contract, source_encuts=src_encuts, source_meshes=src_meshes,
            new_meshes=[[6, 6, 6], [10, 10, 10], [18, 10, 10]], mode='mesh',
        )
    assert exc.value.error_code == 'CONTRACT_PRECISION_BOUND_EXCEEDED'

    # Boundary 16x16x16 = 4096 == max_kmesh_points is permitted.
    boundary = contract_rules.validate_stricter_retry(
        contract, source_encuts=src_encuts, source_meshes=src_meshes,
        new_meshes=[[8, 8, 8], [12, 12, 12], [16, 16, 16]], mode='mesh',
    )
    assert boundary['meshes'][-1] == [16, 16, 16]


def test_validate_stricter_retry_mesh_points_bound_and_2d_vacuum_axis(tmp_path, monkeypatch):
    contract, _ = _approved_contract(tmp_path, monkeypatch, draft_id='drf-20261126-2d000001', dimension='2D')
    tight = json.loads(json.dumps(contract))
    tight['precision_bounds']['max_kmesh_points'] = 100

    # Valid 2D refinement with kz == 1 within 100 points.
    res = contract_rules.validate_stricter_retry(
        tight,
        source_encuts=[450, 520, 600],
        source_meshes=[[4, 4, 1], [6, 6, 1], [8, 8, 1]],
        new_meshes=[[6, 6, 1], [8, 8, 1], [10, 10, 1]],
        mode='mesh',
    )
    assert res['meshes'][-1] == [10, 10, 1]

    # 2D task with kz != 1 rejected.
    with pytest.raises(ContractError) as exc:
        contract_rules.validate_stricter_retry(
            tight,
            source_encuts=[450, 520, 600],
            source_meshes=[[4, 4, 1], [6, 6, 1], [8, 8, 1]],
            new_meshes=[[6, 6, 2], [8, 8, 2], [10, 10, 2]],
            mode='mesh',
        )
    assert exc.value.error_code == 'CONTRACT_RETRY_NOT_STRICTER'

    # Exceeding max_kmesh_points (12 * 12 * 1 = 144 > 100) rejected.
    with pytest.raises(ContractError) as exc:
        contract_rules.validate_stricter_retry(
            tight,
            source_encuts=[450, 520, 600],
            source_meshes=[[4, 4, 1], [6, 6, 1], [8, 8, 1]],
            new_meshes=[[6, 6, 1], [10, 10, 1], [12, 12, 1]],
            mode='mesh',
        )
    assert exc.value.error_code == 'CONTRACT_PRECISION_BOUND_EXCEEDED'


# ------------------------------------ 4. Real TTY refusal & ApprovalProvider

def test_non_tty_refuses_approval_revoke_resume_and_in_window_reserved(tmp_path, monkeypatch):
    draft = _sample_draft()
    task_contract.save_draft(tmp_path, draft)

    # Non-TTY StringIO stream MUST fail real TTY check without mocking has_real_tty.
    pipe_provider = LocalTTYApprovalProvider(
        reader=lambda _prompt: '1',
        output=lambda _text: None,
        stream=io.StringIO('1\n'),
    )
    with pytest.raises(ContractError) as exc:
        task_contract.approve_draft(tmp_path, draft['draft_id'], provider=pipe_provider, now=_now())
    assert exc.value.error_code == 'CONTRACT_TTY_REQUIRED'

    # Reserved in_window_approval provider is refused in v1.
    with pytest.raises(ContractError) as exc:
        task_contract.approve_draft(tmp_path, draft['draft_id'], provider=InWindowApprovalProvider(), now=_now())
    assert exc.value.error_code == 'APPROVAL_MODE_UNSUPPORTED'

    # Operator cancel leaves no active contract or ledger.
    cancel_provider = _tty_provider(monkeypatch, 'q')
    with pytest.raises(ContractError) as exc:
        task_contract.approve_draft(tmp_path, draft['draft_id'], provider=cancel_provider, now=_now())
    assert exc.value.error_code == 'CONTRACT_APPROVAL_CANCELLED'
    assert task_contract.contract_status(tmp_path)['contracts'] == []

    # Approve properly, then verify non-TTY cannot revoke or resume.
    contract, _ = task_contract.approve_draft(
        tmp_path, draft['draft_id'], provider=_tty_provider(monkeypatch, '1'), now=_now()
    )
    cid = contract['contract_id']
    task_contract.halt_contract(tmp_path, cid, 'manual halt', now=_now())

    monkeypatch.setattr(task_contract, 'has_real_tty', lambda stream=None: False)
    with pytest.raises(ContractError) as exc:
        task_contract.resume_contract(tmp_path, cid, now=_now())
    assert exc.value.error_code == 'CONTRACT_TTY_REQUIRED'
    with pytest.raises(ContractError) as exc:
        task_contract.revoke_contract(tmp_path, cid, now=_now())
    assert exc.value.error_code == 'CONTRACT_TTY_REQUIRED'


# ------------------------------------ 5. Tamper, expiry, halt, revoke, LF

def test_tamper_expiry_halt_resume_revoke_and_lf_storage(tmp_path, monkeypatch):
    contract, ledger = _approved_contract(tmp_path, monkeypatch)
    cid = contract['contract_id']
    assert ledger['status'] == 'active'
    assert ledger['allowed_jobs'] == ['si-auto-01']

    # Active contract and ledger files on disk must be strict LF (0 CRLF).
    active_file = agent_paths.store_dir(tmp_path, 'contracts') / 'active' / f'{cid}.json'
    ledger_file = agent_paths.store_dir(tmp_path, 'contracts') / 'ledger' / f'{cid}.json'
    assert b'\r\n' not in active_file.read_bytes()
    assert b'\r\n' not in ledger_file.read_bytes()

    # Halt (uncertain) blocks verify; only human TTY resume restores active.
    halted = task_contract.halt_contract(
        tmp_path, cid, 'submission status uncertain', uncertain=True, now=_now() + timedelta(minutes=5)
    )
    assert halted['status'] == 'halted_uncertain'
    with pytest.raises(ContractError) as exc:
        task_contract.verify_contract(tmp_path, cid, now=_now() + timedelta(minutes=6))
    assert exc.value.error_code == 'CONTRACT_HALTED'

    resumed = task_contract.resume_contract(
        tmp_path, cid, provider=_tty_provider(monkeypatch, '1'), now=_now() + timedelta(minutes=10)
    )
    assert resumed['status'] == 'active' and resumed['in_flight'] is None

    # Expiry transitions ledger to expired and cannot be resumed.
    expired_time = _now() + timedelta(days=8)
    with pytest.raises(ContractError) as exc:
        task_contract.verify_contract(tmp_path, cid, now=expired_time)
    assert exc.value.error_code == 'CONTRACT_EXPIRED'
    assert task_contract.read_ledger(tmp_path, cid)['status'] == 'expired'
    with pytest.raises(ContractError) as exc:
        task_contract.resume_contract(tmp_path, cid, provider=_tty_provider(monkeypatch, '1'), now=expired_time)
    assert exc.value.error_code == 'CONTRACT_EXPIRED'

    # Revoke is terminal and blocks verify/resume.
    c2, _ = _approved_contract(tmp_path, monkeypatch, draft_id='drf-20261126-rev00001')
    cid2 = c2['contract_id']
    revoked = task_contract.revoke_contract(
        tmp_path, cid2, provider=_tty_provider(monkeypatch, '1'), now=_now() + timedelta(minutes=1)
    )
    assert revoked['status'] == 'revoked'
    with pytest.raises(ContractError) as exc:
        task_contract.verify_contract(tmp_path, cid2, now=_now() + timedelta(minutes=2))
    assert exc.value.error_code == 'CONTRACT_REVOKED'
    with pytest.raises(ContractError) as exc:
        task_contract.resume_contract(tmp_path, cid2, provider=_tty_provider(monkeypatch, '1'), now=_now())
    assert exc.value.error_code == 'CONTRACT_REVOKED'

    # Tampering with immutable active contract breaks canonical_sha256 -> CONTRACT_TAMPERED.
    c3, _ = _approved_contract(tmp_path, monkeypatch, draft_id='drf-20261126-tamper01')
    cid3 = c3['contract_id']
    c3_file = agent_paths.store_dir(tmp_path, 'contracts') / 'active' / f'{cid3}.json'
    tampered = json.loads(c3_file.read_text(encoding='utf-8'))
    tampered['budget']['max_submissions'] = 999
    c3_file.write_text(json.dumps(tampered, indent=2), encoding='utf-8', newline='\n')
    with pytest.raises(ContractError) as exc:
        task_contract.verify_contract(tmp_path, cid3, now=_now())
    assert exc.value.error_code == 'CONTRACT_TAMPERED'


    # Complete transitions active -> completed and blocks further active verification.
    c4, _ = _approved_contract(tmp_path, monkeypatch, draft_id='drf-20261126-done0001')
    cid4 = c4['contract_id']
    done = task_contract.complete_contract(tmp_path, cid4, summary={'phases': 5}, now=_now())
    assert done['status'] == 'completed'
    with pytest.raises(ContractError) as exc:
        task_contract.verify_contract(tmp_path, cid4, now=_now())
    assert exc.value.error_code == 'CONTRACT_COMPLETED'

    # Symlink contract file must be refused (real symlink if host allows, plus mocked is_symlink edge).
    c5, _ = _approved_contract(tmp_path, monkeypatch, draft_id='drf-20261126-sym00001')
    cid5 = c5['contract_id']
    c5_file = agent_paths.store_dir(tmp_path, 'contracts') / 'active' / f'{cid5}.json'
    orig_is_symlink = Path.is_symlink
    monkeypatch.setattr(Path, 'is_symlink', lambda self: True if self == c5_file else orig_is_symlink(self))
    with pytest.raises(ContractError) as exc:
        task_contract.load_contract(tmp_path, cid5)
    assert exc.value.error_code == 'CONTRACT_SYMLINK_REFUSED'
    monkeypatch.setattr(Path, 'is_symlink', orig_is_symlink)


# ------------------------------- 6. Optional OpenSSH signing & verification

def test_ssh_signature_real_keygen_roundtrip_and_tamper_detection(tmp_path, monkeypatch):
    ssh_keygen = shutil.which('ssh-keygen')
    if not ssh_keygen:
        pytest.skip('ssh-keygen not installed on test host')

    key_dir = tmp_path / 'keys'
    key_dir.mkdir()
    key_path = key_dir / 'operator_ed25519'
    other_key = key_dir / 'attacker_ed25519'
    subprocess.run([ssh_keygen, '-t', 'ed25519', '-N', '', '-f', str(key_path), '-q'], check=True)
    subprocess.run([ssh_keygen, '-t', 'ed25519', '-N', '', '-f', str(other_key), '-q'], check=True)

    pub_line = (key_dir / 'operator_ed25519.pub').read_text(encoding='utf-8').strip()
    allowed_signers = key_dir / 'allowed_signers'
    allowed_signers.write_text(f'operator@local {pub_line}\n', encoding='utf-8', newline='\n')

    policy_file = agent_paths.file_path(tmp_path, 'autonomy_policy')
    policy_file.parent.mkdir(parents=True, exist_ok=True)
    policy_file.write_text(
        json.dumps({
            'require_ssh_signature': True,
            'allowed_signers': str(allowed_signers),
            'principal': 'operator@local',
        }),
        encoding='utf-8',
        newline='\n',
    )

    draft = _sample_draft(draft_id='drf-20261126-ssh00001')
    task_contract.save_draft(tmp_path, draft)

    # Refuses unsigned approval when policy requires SSH signature.
    with pytest.raises(ContractError) as exc:
        task_contract.approve_draft(tmp_path, draft['draft_id'], provider=_tty_provider(monkeypatch, '1'), now=_now())
    assert exc.value.error_code == 'CONTRACT_SIGNATURE_REQUIRED'

    # Signing with authorized key succeeds and verifies cleanly.
    contract, _ = task_contract.approve_draft(
        tmp_path,
        draft['draft_id'],
        provider=_tty_provider(monkeypatch, '1'),
        now=_now(),
        ssh_key=key_path,
        ssh_principal='operator@local',
    )
    cid = contract['contract_id']
    assert contract['approval']['ssh_signature'] is not None
    verified_c, verified_l = task_contract.verify_contract(tmp_path, cid, now=_now())
    assert verified_c['contract_id'] == cid and verified_l['status'] == 'active'

    # Tampering signature file fails verification with CONTRACT_SIGNATURE_INVALID.
    sig_file = agent_paths.store_dir(tmp_path, 'contracts') / 'active' / f'{cid}.json.sig'
    original_sig = sig_file.read_bytes()
    sig_file.write_text('-----BEGIN SSH SIGNATURE-----\ncorrupted\n-----END SSH SIGNATURE-----\n', encoding='utf-8')
    with pytest.raises(ContractError) as exc:
        task_contract.verify_contract(tmp_path, cid, now=_now())
    assert exc.value.error_code == 'CONTRACT_SIGNATURE_INVALID'

    # Re-signing canonical bytes with unauthorized attacker key still fails verification.
    canon = contract_rules.canonical_bytes(contract)
    task_contract.sign_contract_ssh(canon, sig_file, key_path=other_key)
    with pytest.raises(ContractError) as exc:
        task_contract.verify_contract(tmp_path, cid, now=_now())
    assert exc.value.error_code == 'CONTRACT_SIGNATURE_INVALID'

    # Restoring original signature verifies again.
    sig_file.write_bytes(original_sig)
    assert task_contract.verify_contract(tmp_path, cid, now=_now())[0]['contract_id'] == cid

    # Spoofing ssh_signature namespace or sig_file in active contract JSON is hard-rejected.
    active_file = agent_paths.store_dir(tmp_path, 'contracts') / 'active' / f'{cid}.json'
    original_json = active_file.read_bytes()
    for bad_sig in (
        {'namespace': 'git', 'sig_file': f'{cid}.json.sig', 'principal': 'operator@local'},
        {'namespace': 'vasp-agent-contract', 'sig_file': 'other.sig', 'principal': 'operator@local'},
        {'namespace': 'vasp-agent-contract', 'sig_file': f'{cid}.json.sig', 'principal': 'other@local'},
    ):
        spoofed = json.loads(original_json.decode('utf-8'))
        spoofed['approval']['ssh_signature'] = bad_sig
        active_file.write_text(json.dumps(spoofed, indent=2), encoding='utf-8', newline='\n')
        with pytest.raises(ContractError) as exc:
            task_contract.load_contract(tmp_path, cid)
        assert exc.value.error_code == 'CONTRACT_SIGNATURE_INVALID'
    active_file.write_bytes(original_json)

    # Mismatched ledger canonical_sha256 is rejected on direct reserve_submission.
    ledger_file = agent_paths.store_dir(tmp_path, 'contracts') / 'ledger' / f'{cid}.json'
    bad_ledger = json.loads(ledger_file.read_text(encoding='utf-8'))
    bad_ledger['canonical_sha256'] = '0' * 64
    ledger_file.write_text(json.dumps(bad_ledger, indent=2), encoding='utf-8', newline='\n')
    with pytest.raises(ContractError) as exc:
        task_contract.reserve_submission(
            tmp_path, cid, server='hpc', job_name='si-auto-01', phase='convergence',
            resources=contract['resource_envelope']['phase_defaults']['convergence'], now=_now(),
        )
    assert exc.value.error_code == 'CONTRACT_TAMPERED'


def test_ssh_signature_mocked_failure_and_missing_binary(tmp_path, monkeypatch):
    allowed = tmp_path / 'allowed_signers'
    allowed.write_text('op@local ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIMockKey\n', encoding='utf-8')
    policy_file = agent_paths.file_path(tmp_path, 'autonomy_policy')
    policy_file.parent.mkdir(parents=True, exist_ok=True)
    policy_file.write_text(
        json.dumps({'require_ssh_signature': True, 'allowed_signers': str(allowed), 'principal': 'op@local'}),
        encoding='utf-8',
    )

    draft = _sample_draft(draft_id='drf-20261126-ssh00002')
    task_contract.save_draft(tmp_path, draft)
    monkeypatch.setattr(task_contract, '_resolve_ssh_keygen', lambda: None)
    with pytest.raises(ContractError) as exc:
        task_contract.approve_draft(
            tmp_path,
            draft['draft_id'],
            provider=_tty_provider(monkeypatch, '1'),
            now=_now(),
            ssh_key=tmp_path / 'fake_key',
            ssh_principal='op@local',
        )
    assert exc.value.error_code == 'CONTRACT_SIGNATURE_INVALID'

    # Simulate mocked ssh-keygen returning non-zero on verify.
    monkeypatch.setattr(task_contract, '_resolve_ssh_keygen', lambda: 'ssh-keygen')
    monkeypatch.setattr(
        task_contract.subprocess,
        'run',
        lambda *a, **kw: SimpleNamespace(returncode=1, stdout=b'', stderr=b'bad signature'),
    )
    sig_file = tmp_path / 'x.sig'
    sig_file.write_text('fake', encoding='utf-8')
    with pytest.raises(ContractError) as exc:
        task_contract.verify_contract_ssh(
            b'{}', sig_file, allowed_signers=allowed, principal='op@local'
        )
    assert exc.value.error_code == 'CONTRACT_SIGNATURE_INVALID'


# --------------- 7. Ledger budget, in_flight, retries, failures, concurrency

def test_ledger_budget_in_flight_retries_failures_and_auto_halt(tmp_path, monkeypatch):
    draft = _sample_draft(draft_id='drf-20261126-led00001')
    draft['budget'] = {
        'max_submissions': 2,
        'max_resume_retries': 1,
        'max_repair_retries': 1,
        'max_scan_retries': 1,
        'max_consecutive_failures': 2,
    }
    task_contract.save_draft(tmp_path, draft)
    contract, _ = task_contract.approve_draft(
        tmp_path, draft['draft_id'], provider=_tty_provider(monkeypatch, '1'), now=_now()
    )
    cid = contract['contract_id']
    conv_res = contract['resource_envelope']['phase_defaults']['convergence']
    relax_res = contract['resource_envelope']['phase_defaults']['relaxation']

    # Scope violations rejected before deducting budget.
    with pytest.raises(ContractError) as exc:
        task_contract.reserve_submission(
            tmp_path, cid, server='other', job_name='si-auto-01', phase='convergence', resources=conv_res, now=_now()
        )
    assert exc.value.error_code == 'CONTRACT_SCOPE_VIOLATION'
    with pytest.raises(ContractError) as exc:
        task_contract.reserve_submission(
            tmp_path, cid, server='hpc', job_name='rogue-job', phase='convergence', resources=conv_res, now=_now()
        )
    assert exc.value.error_code == 'CONTRACT_SCOPE_VIOLATION'
    assert task_contract.read_ledger(tmp_path, cid)['submissions_used'] == 0

    # Reserve #1 -> in_flight blocks duplicate concurrent reserve -> release with refund restores count.
    l1 = task_contract.reserve_submission(
        tmp_path, cid, server='hpc', job_name='si-auto-01', phase='convergence',
        resources=conv_res, operation_id='op1', now=_now(),
    )
    assert l1['submissions_used'] == 1 and l1['in_flight']['operation_id'] == 'op1'
    with pytest.raises(ContractError) as exc:
        task_contract.reserve_submission(
            tmp_path, cid, server='hpc', job_name='si-auto-01', phase='convergence', resources=conv_res, now=_now()
        )
    assert exc.value.error_code == 'SUBMISSION_IN_FLIGHT'
    released = task_contract.release_in_flight(tmp_path, cid, refund=True, reason='confirmed_not_submitted', now=_now())
    assert released['submissions_used'] == 0 and released['in_flight'] is None

    # Reserve #1 and confirm, then register repair retry adding new job to allowed_jobs.
    task_contract.reserve_submission(
        tmp_path, cid, server='hpc', job_name='si-auto-01', phase='convergence', resources=conv_res, now=_now()
    )
    confirmed = task_contract.confirm_submission(tmp_path, cid, job_id='9001', now=_now())
    assert confirmed['in_flight'] is None and confirmed['submissions_used'] == 1

    # One failure + one pass resets consecutive_failures.
    f1 = task_contract.record_phase_outcome(
        tmp_path, cid, job_name='si-auto-01', phase='convergence', passed=False,
        failure_info={'category': 'OOM'}, now=_now(),
    )
    assert f1['consecutive_failures'] == 1 and f1['status'] == 'active'

    retried = task_contract.register_retry(
        tmp_path, cid, kind='repair', new_job_name='si-auto-01_fix1',
        diagnostic={'failure_category': 'OOM', 'evidence_sha256': SHA64_A}, now=_now(),
    )
    assert retried['repair_retries_used'] == 1
    assert 'si-auto-01_fix1' in retried['allowed_jobs']
    assert len(retried['diagnostics']) == 1

    # Second repair retry exceeds max_repair_retries=1 -> auto-halts!
    with pytest.raises(ContractError) as exc:
        task_contract.register_retry(tmp_path, cid, kind='repair', new_job_name='si-auto-01_fix2', now=_now())
    assert exc.value.error_code == 'CONTRACT_BUDGET_EXHAUSTED'
    assert task_contract.read_ledger(tmp_path, cid)['status'] == 'halted'

    # Resume via TTY, pass outcome resets consecutive_failures, then exhaust max_submissions=2 -> auto-halt.
    task_contract.resume_contract(tmp_path, cid, provider=_tty_provider(monkeypatch, '1'), now=_now())
    p1 = task_contract.record_phase_outcome(
        tmp_path, cid, job_name='si-auto-01_fix1', phase='convergence', passed=True, now=_now()
    )
    assert p1['consecutive_failures'] == 0

    task_contract.reserve_submission(
        tmp_path, cid, server='hpc', job_name='si-auto-01_fix1', phase='relaxation', resources=relax_res, now=_now()
    )
    task_contract.confirm_submission(tmp_path, cid, job_id='9002', now=_now())
    with pytest.raises(ContractError) as exc:
        task_contract.reserve_submission(
            tmp_path, cid, server='hpc', job_name='si-auto-01_fix1', phase='scf',
            resources=contract['resource_envelope']['phase_defaults']['scf'], now=_now(),
        )
    assert exc.value.error_code == 'CONTRACT_BUDGET_EXHAUSTED'
    assert task_contract.read_ledger(tmp_path, cid)['status'] == 'halted'


def test_ledger_file_lock_serializes_concurrent_outcome_updates(tmp_path, monkeypatch):
    draft = _sample_draft(draft_id='drf-20261126-conc0001')
    draft['budget']['max_consecutive_failures'] = 20
    task_contract.save_draft(tmp_path, draft)
    contract, _ = task_contract.approve_draft(
        tmp_path, draft['draft_id'], provider=_tty_provider(monkeypatch, '1'), now=_now()
    )
    cid = contract['contract_id']

    def worker(idx: int):
        return task_contract.record_phase_outcome(
            tmp_path, cid, job_name='si-auto-01', phase='convergence', passed=False,
            failure_info={'idx': idx}, now=_now(),
        )

    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(worker, range(8)))
    assert len(results) == 8
    final = task_contract.read_ledger(tmp_path, cid)
    assert final['consecutive_failures'] == 8
    assert final['status'] == 'active'


# ------------------------------------ 8. Layer 3 AST zero-upward-import guard

def test_layer3_task_contract_modules_have_zero_upward_imports():
    """Layer 3 contract modules must never import Layer 4 chat/ or control/."""
    forbidden = ('chat', 'control')
    violations = []
    for mod in (contract_rules, contract_ledger, task_contract):
        mod_path = Path(mod.__file__)
        tree = ast.parse(mod_path.read_text(encoding='utf-8'))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    for pkg in forbidden:
                        if alias.name == f'vasp_agent.{pkg}' or alias.name.startswith(f'vasp_agent.{pkg}.'):
                            violations.append(f'{mod_path.name}:{node.lineno} -> {alias.name}')
            elif isinstance(node, ast.ImportFrom):
                mod_name = node.module or ''
                for pkg in forbidden:
                    if mod_name == f'vasp_agent.{pkg}' or mod_name.startswith(f'vasp_agent.{pkg}.'):
                        violations.append(f'{mod_path.name}:{node.lineno} -> {mod_name}')
                    elif node.level >= 2 and (mod_name == pkg or mod_name.startswith(f'{pkg}.')):
                        violations.append(f'{mod_path.name}:{node.lineno} -> {"." * node.level}{mod_name}')
                    elif node.level >= 2 and not mod_name:
                        for alias in node.names:
                            if alias.name == pkg:
                                violations.append(f'{mod_path.name}:{node.lineno} -> {"." * node.level}{alias.name}')
    assert violations == []


# ---------------------- 9. P1a review hardening regressions (state machine & edge cases)

def test_record_phase_outcome_refuses_terminal_and_halted_contracts(tmp_path, monkeypatch):
    # Revoked contract must NEVER be overwritten back to halted (would reopen resume_contract!).
    draft = _sample_draft(draft_id='drf-20261126-revout01')
    draft['budget']['max_consecutive_failures'] = 1
    task_contract.save_draft(tmp_path, draft)
    c_rev, _ = task_contract.approve_draft(
        tmp_path, draft['draft_id'], provider=_tty_provider(monkeypatch, '1'), now=_now()
    )
    cid_rev = c_rev['contract_id']
    task_contract.revoke_contract(tmp_path, cid_rev, provider=_tty_provider(monkeypatch, '1'), now=_now())
    with pytest.raises(ContractError) as exc:
        task_contract.record_phase_outcome(
            tmp_path, cid_rev, job_name='si-auto-01', phase='convergence', passed=False, now=_now()
        )
    assert exc.value.error_code == 'CONTRACT_REVOKED'
    assert task_contract.read_ledger(tmp_path, cid_rev)['status'] == 'revoked'

    # Completed contract refuses outcome updates.
    c_done, _ = _approved_contract(tmp_path, monkeypatch, draft_id='drf-20261126-doneout1')
    task_contract.complete_contract(tmp_path, c_done['contract_id'], now=_now())
    with pytest.raises(ContractError) as exc:
        task_contract.record_phase_outcome(
            tmp_path, c_done['contract_id'], job_name='si-auto-01', phase='convergence', passed=False, now=_now()
        )
    assert exc.value.error_code == 'CONTRACT_COMPLETED'
    assert task_contract.read_ledger(tmp_path, c_done['contract_id'])['status'] == 'completed'

    # Expired contract transitions to expired and refuses outcome updates.
    c_exp, _ = _approved_contract(tmp_path, monkeypatch, draft_id='drf-20261126-expout01')
    with pytest.raises(ContractError) as exc:
        task_contract.record_phase_outcome(
            tmp_path, c_exp['contract_id'], job_name='si-auto-01', phase='convergence',
            passed=False, now=_now() + timedelta(days=10),
        )
    assert exc.value.error_code == 'CONTRACT_EXPIRED'
    assert task_contract.read_ledger(tmp_path, c_exp['contract_id'])['status'] == 'expired'

    # Halted contract refuses passed=True so consecutive_failures is never silently cleared.
    c_halt, _ = _approved_contract(tmp_path, monkeypatch, draft_id='drf-20261126-hltout01')
    cid_h = c_halt['contract_id']
    task_contract.record_phase_outcome(tmp_path, cid_h, job_name='si-auto-01', phase='convergence', passed=False, now=_now())
    task_contract.halt_contract(tmp_path, cid_h, 'manual halt', now=_now())
    with pytest.raises(ContractError) as exc:
        task_contract.record_phase_outcome(tmp_path, cid_h, job_name='si-auto-01', phase='convergence', passed=True, now=_now())
    assert exc.value.error_code == 'CONTRACT_HALTED'
    assert task_contract.read_ledger(tmp_path, cid_h)['consecutive_failures'] == 1


def test_confirm_release_and_halt_enforce_status_binding_operation_id_and_monotonic_uncertain(tmp_path, monkeypatch):
    contract, _ = _approved_contract(tmp_path, monkeypatch, draft_id='drf-20261126-gate0001')
    cid = contract['contract_id']
    conv_res = contract['resource_envelope']['phase_defaults']['convergence']

    # confirm_submission refuses invalid job_id and refuses missing in_flight (non-recovered) or bogus op_id.
    with pytest.raises(ContractError) as exc:
        task_contract.confirm_submission(tmp_path, cid, job_id='not-numeric', now=_now())
    assert exc.value.error_code == 'CONTRACT_SCHEMA_INVALID'

    with pytest.raises(ContractError) as exc:
        task_contract.confirm_submission(tmp_path, cid, job_id='12345', now=_now())
    assert exc.value.error_code == 'SUBMISSION_NOT_IN_FLIGHT'

    with pytest.raises(ContractError) as exc:
        task_contract.confirm_submission(tmp_path, cid, job_id='12345', operation_id='wrong-op', recovered=True, now=_now())
    assert exc.value.error_code == 'SUBMISSION_IN_FLIGHT'

    # Reserve -> wrong operation_id rejected on both confirm and release -> correct operation_id succeeds.
    task_contract.reserve_submission(
        tmp_path, cid, server='hpc', job_name='si-auto-01', phase='convergence',
        resources=conv_res, operation_id='op-good', now=_now(),
    )
    with pytest.raises(ContractError) as exc:
        task_contract.release_in_flight(tmp_path, cid, operation_id='op-wrong', refund=True, now=_now())
    assert exc.value.error_code == 'SUBMISSION_IN_FLIGHT'
    assert task_contract.read_ledger(tmp_path, cid)['submissions_used'] == 1

    # Once halted_uncertain, release_in_flight(refund=True) MUST be refused, and halt(uncertain=False) must NOT downgrade it!
    task_contract.halt_contract(tmp_path, cid, 'uncertain sbatch', uncertain=True, now=_now())
    with pytest.raises(ContractError) as exc:
        task_contract.release_in_flight(tmp_path, cid, operation_id='op-good', refund=True, now=_now())
    assert exc.value.error_code == 'CONTRACT_HALTED'
    assert task_contract.read_ledger(tmp_path, cid)['submissions_used'] == 1
    assert task_contract.read_ledger(tmp_path, cid)['in_flight'] is not None

    still_uncertain = task_contract.halt_contract(tmp_path, cid, 'extra note', uncertain=False, now=_now())
    assert still_uncertain['status'] == 'halted_uncertain'

    # confirm_submission on halted contract is refused.
    with pytest.raises(ContractError) as exc:
        task_contract.confirm_submission(tmp_path, cid, job_id='9001', operation_id='op-good', now=_now())
    assert exc.value.error_code == 'CONTRACT_HALTED'

    # Expired contract is terminal for halt_contract.
    c_exp, _ = _approved_contract(tmp_path, monkeypatch, draft_id='drf-20261126-exphlt01')
    with pytest.raises(ContractError):
        task_contract.verify_contract(tmp_path, c_exp['contract_id'], now=_now() + timedelta(days=10))
    with pytest.raises(ContractError) as exc:
        task_contract.halt_contract(tmp_path, c_exp['contract_id'], 'late halt', now=_now() + timedelta(days=10))
    assert exc.value.error_code == 'CONTRACT_TERMINAL'


def test_contract_status_list_view_catches_tampered_ledger_binding(tmp_path, monkeypatch):
    contract, _ = _approved_contract(tmp_path, monkeypatch, draft_id='drf-20261126-lst00001')
    cid = contract['contract_id']
    ledger_file = agent_paths.store_dir(tmp_path, 'contracts') / 'ledger' / f'{cid}.json'
    bad = json.loads(ledger_file.read_text(encoding='utf-8'))
    bad['canonical_sha256'] = 'f' * 64
    ledger_file.write_text(json.dumps(bad, indent=2), encoding='utf-8', newline='\n')

    summary = task_contract.contract_status(tmp_path, now=_now())
    assert summary['active'] is False
    entry = next(item for item in summary['contracts'] if item['contract_id'] == cid)
    assert entry['status'] == 'invalid'
    assert entry['error_code'] == 'CONTRACT_TAMPERED'


def test_relative_allowed_signers_resolves_against_workspace_independent_of_cwd(tmp_path, monkeypatch):
    ws = tmp_path / 'ws'
    other_cwd = tmp_path / 'elsewhere'
    ws.mkdir()
    other_cwd.mkdir()
    signers_rel = 'servers/allowed_signers'
    (ws / 'servers').mkdir()
    (ws / signers_rel).write_text('op@local ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIMockKey\n', encoding='utf-8')
    policy_file = agent_paths.file_path(ws, 'autonomy_policy')
    policy_file.parent.mkdir(parents=True, exist_ok=True)
    policy_file.write_text(
        json.dumps({'require_ssh_signature': True, 'allowed_signers': signers_rel, 'principal': 'op@local'}),
        encoding='utf-8',
        newline='\n',
    )
    monkeypatch.chdir(other_cwd)
    policy = task_contract.load_autonomy_policy(ws)
    assert Path(policy['allowed_signers']) == (ws / signers_rel).resolve()


def test_prefix_phase_contracts_allowed_and_dependency_gaps_rejected():
    for prefix in (['convergence'], ['convergence', 'relaxation'], ['convergence', 'relaxation', 'scf']):
        draft = _sample_draft()
        draft['workflow']['allowed_phases'] = prefix
        draft['resource_envelope']['phase_defaults'] = {
            p: draft['resource_envelope']['phase_defaults'][p] for p in prefix
        }
        contract_rules.validate_contract_schema(draft, require_approval=False)

    for bad_chain in (['relaxation'], ['convergence', 'scf'], ['convergence', 'relaxation', 'bands']):
        draft = _sample_draft()
        draft['workflow']['allowed_phases'] = bad_chain
        draft['resource_envelope']['phase_defaults'] = {
            p: draft['resource_envelope']['phase_defaults'][p] for p in bad_chain
        }
        with pytest.raises(ContractError) as exc:
            contract_rules.validate_contract_schema(draft, require_approval=False)
        assert exc.value.error_code == 'CONTRACT_SCHEMA_INVALID'


def test_approve_draft_prevalidates_before_prompt_and_cleans_up_on_ledger_failure(tmp_path, monkeypatch):
    draft = _sample_draft(draft_id='drf-20261126-preval01')
    task_contract.save_draft(tmp_path, draft)
    prompted = []
    monkeypatch.setattr(task_contract, 'has_real_tty', lambda stream=None: True)
    provider = LocalTTYApprovalProvider(
        reader=lambda p: prompted.append(p) or '1',
        output=lambda _s: None,
    )

    with pytest.raises(ContractError) as exc:
        task_contract.approve_draft(tmp_path, draft['draft_id'], provider=provider, review_mode='bogus', now=_now())
    assert exc.value.error_code == 'CONTRACT_SCHEMA_INVALID'
    assert prompted == []

    with pytest.raises(ContractError) as exc:
        task_contract.approve_draft(
            tmp_path, draft['draft_id'], provider=provider, ssh_key=tmp_path / 'missing.key', now=_now()
        )
    assert exc.value.error_code == 'CONTRACT_SIGNATURE_INVALID'
    assert prompted == []

    # Simulate initial ledger creation failure -> active contract must be rolled back (no orphan).
    cid = 'ctr-20261126-rollback'
    monkeypatch.setattr(
        contract_ledger,
        'create_initial_ledger',
        lambda *_a, **_kw: (_ for _ in ()).throw(OSError('disk full')),
    )
    with pytest.raises(OSError, match='disk full'):
        task_contract.approve_draft(tmp_path, draft['draft_id'], provider=provider, contract_id=cid, now=_now())
    active_file = agent_paths.store_dir(tmp_path, 'contracts') / 'active' / f'{cid}.json'
    assert not active_file.exists()


def test_real_tty_rejects_regular_file_and_pipe(tmp_path):
    import os

    regular = tmp_path / 'input.txt'
    regular.write_text('1\n', encoding='utf-8')
    with regular.open('r', encoding='utf-8') as fh:
        assert task_contract.has_real_tty(fh) is False

    r_fd, w_fd = os.pipe()
    try:
        with os.fdopen(r_fd, 'r', encoding='utf-8') as r_stream:
            assert task_contract.has_real_tty(r_stream) is False
    finally:
        os.close(w_fd)


def test_real_symlink_refused_when_os_permits(tmp_path):
    import os

    active_dir = agent_paths.store_dir(tmp_path, 'contracts') / 'active'
    active_dir.mkdir(parents=True, exist_ok=True)
    target = tmp_path / 'outside.json'
    target.write_text('{}', encoding='utf-8')
    link = active_dir / 'ctr-20261126-realsym1.json'
    try:
        os.symlink(target, link)
    except OSError:
        pytest.skip('OS does not permit creating symlinks in this test environment')
    with pytest.raises(ContractError) as exc:
        task_contract.load_contract(tmp_path, 'ctr-20261126-realsym1')
    assert exc.value.error_code == 'CONTRACT_SYMLINK_REFUSED'


def test_relative_allowed_signers_symlink_refused_in_policy(tmp_path, monkeypatch):
    ws = tmp_path / 'ws'
    (ws / 'servers').mkdir(parents=True)
    signers = (ws / 'servers' / 'allowed_signers').resolve()
    signers.write_text('op@local ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIMockKey\n', encoding='utf-8')
    policy_file = agent_paths.file_path(ws, 'autonomy_policy')
    policy_file.parent.mkdir(parents=True, exist_ok=True)
    policy_file.write_text(
        json.dumps({'require_ssh_signature': True, 'allowed_signers': 'servers/allowed_signers', 'principal': 'op@local'}),
        encoding='utf-8',
        newline='\n',
    )
    orig_is_symlink = Path.is_symlink
    monkeypatch.setattr(Path, 'is_symlink', lambda self: True if self == signers else orig_is_symlink(self))
    with pytest.raises(ContractError) as exc:
        task_contract.load_autonomy_policy(ws)
    assert exc.value.error_code == 'CONTRACT_SYMLINK_REFUSED'


def test_relative_allowed_signers_real_symlink_refused_when_os_permits(tmp_path):
    import os

    ws = tmp_path / 'ws'
    (ws / 'servers').mkdir(parents=True)
    outside = tmp_path / 'outside_signers'
    outside.write_text('op@local ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIMockKey\n', encoding='utf-8')
    link = ws / 'servers' / 'allowed_signers'
    try:
        os.symlink(outside, link)
    except OSError:
        pytest.skip('OS does not permit creating symlinks in this test environment')
    policy_file = agent_paths.file_path(ws, 'autonomy_policy')
    policy_file.parent.mkdir(parents=True, exist_ok=True)
    policy_file.write_text(
        json.dumps({'require_ssh_signature': True, 'allowed_signers': 'servers/allowed_signers', 'principal': 'op@local'}),
        encoding='utf-8',
        newline='\n',
    )
    with pytest.raises(ContractError) as exc:
        task_contract.load_autonomy_policy(ws)
    assert exc.value.error_code == 'CONTRACT_SYMLINK_REFUSED'
