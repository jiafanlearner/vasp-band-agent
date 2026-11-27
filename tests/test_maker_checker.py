"""Offline regression tests for P2 Maker-Checker review leases, attestations, and AST boundaries."""
from __future__ import annotations

import ast
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path

import pytest

from vasp_agent.permissions import review_attestation as att
from vasp_agent.permissions.contract_rules import ContractError

SNAP_A = 'a' * 64
SNAP_B = 'b' * 64


def _now(offset_seconds: int = 0) -> datetime:
    return datetime(2026, 11, 27, 8, 0, 0, tzinfo=timezone.utc) + timedelta(seconds=offset_seconds)


def _pass_checks() -> list[dict]:
    return [
        {
            'item': 'physics_model',
            'evidence': 'nonmagnetic PBE 3D Si confirmed',
            'reason': 'matches scope',
            'next_action': '无需',
            'judgment': 'pass',
            'blocking': False,
        },
        {
            'item': 'validation_plan',
            'evidence': '3x3 ENCUT/kmesh scan ready',
            'reason': 'bounded',
            'next_action': '无需',
            'judgment': 'pending',
            'blocking': False,
        },
    ]


def _blocked_checks() -> list[dict]:
    return [
        {
            'item': 'dos_inputs',
            'evidence': 'program_checks failed',
            'reason': 'empty bands insufficient',
            'next_action': '增加 nbands 后重跑',
            'judgment': 'blocked',
            'blocking': True,
        },
        {
            'item': 'physics_model',
            'evidence': 'ok',
            'reason': 'ok',
            'next_action': '无需',
            'judgment': 'pass',
            'blocking': False,
        },
    ]


def _sample_contract(mode: str = 'maker_checker', contract_id: str = 'ctr-20261127-a1b2c3d4') -> dict:
    return {
        'contract_id': contract_id,
        'review_policy': {'mode': mode, 'auto_advance_on_green': True},
    }


def _try_symlink(link: Path, target: Path, *, target_is_directory: bool = False) -> bool:
    try:
        link.symlink_to(target, target_is_directory=target_is_directory)
        return True
    except OSError:
        return False


# ------------------------------------ 1. Reviewer identity formatting

def test_format_and_parse_reviewer_identity():
    tag = att.format_reviewer(role='checker', host='antigravity', model='pro', actor_id='sub_9a4c12ff9999')
    assert tag == 'checker:antigravity:pro:sub_9a4c12ff'
    parsed = att.parse_reviewer(tag)
    assert parsed == {'role': 'checker', 'host': 'antigravity', 'model': 'pro', 'actor_prefix': 'sub_9a4c12ff'}

    sanitized = att.format_reviewer(role='maker', host='My Host:1', model='gpt/5.4 ', actor_id='maker 01:abc')
    assert sanitized == 'maker:My_Host_1:gpt_5.4:maker_01_abc'
    assert att.parse_reviewer(sanitized) is not None
    assert att.parse_reviewer('invalid:host:model:actor') is None

    with pytest.raises(ContractError) as exc:
        att.format_reviewer(role='admin', actor_id='sub_1')
    assert exc.value.error_code == 'REVIEW_INVALID_INPUT'


# ------------------------------------ 2. Lease lifecycle & tamper guards

def test_lease_issue_verify_expiry_mismatch_and_pruning(tmp_path):
    # Read-only verify on empty workspace fails cleanly without creating dirs.
    with pytest.raises(ContractError) as exc:
        att.verify_lease(
            tmp_path, actor_id='checker-1', role='checker', server='cluster-a',
            job_name='si-auto-01', phase='convergence', snapshot=SNAP_A, now=_now(),
        )
    assert exc.value.error_code == 'REVIEW_LEASE_REQUIRED'
    assert not (tmp_path / '.agent').exists()

    lease = att.issue_lease(
        tmp_path, actor_id='checker-1', role='checker', server='cluster-a',
        job_name='si-auto-01', phase='convergence', snapshot=SNAP_A,
        host='antigravity', model='pro', ttl_seconds=900, now=_now(),
    )
    path = att.lease_path(tmp_path, lease['lease_id'])
    assert path.is_file()
    assert b'\r\n' not in path.read_bytes()

    verified = att.verify_lease(
        tmp_path, actor_id='checker-1', role='checker', server='cluster-a',
        job_name='si-auto-01', phase='convergence', snapshot=SNAP_A,
        host='antigravity', model='pro', now=_now(899),
    )
    assert verified['lease_id'] == lease['lease_id']

    # Different model or host cannot hijack the lease even with the same actor_id.
    with pytest.raises(ContractError) as exc_model:
        att.verify_lease(
            tmp_path, actor_id='checker-1', role='checker', server='cluster-a',
            job_name='si-auto-01', phase='convergence', snapshot=SNAP_A,
            host='antigravity', model='flash', now=_now(10),
        )
    assert exc_model.value.error_code == 'REVIEW_LEASE_REQUIRED'

    with pytest.raises(ContractError) as exc_model_id:
        att.verify_lease(
            tmp_path, actor_id='checker-1', role='checker', server='cluster-a',
            job_name='si-auto-01', phase='convergence', snapshot=SNAP_A,
            lease_id=lease['lease_id'], host='antigravity', model='flash', now=_now(10),
        )
    assert exc_model_id.value.error_code == 'REVIEW_LEASE_INVALID'

    with pytest.raises(ContractError) as exc_host_id:
        att.verify_lease(
            tmp_path, actor_id='checker-1', role='checker', server='cluster-a',
            job_name='si-auto-01', phase='convergence', snapshot=SNAP_A,
            lease_id=lease['lease_id'], host='codex', model='pro', now=_now(10),
        )
    assert exc_host_id.value.error_code == 'REVIEW_LEASE_INVALID'

    # Different actor or role cannot hijack the lease.
    with pytest.raises(ContractError) as exc_actor:
        att.verify_lease(
            tmp_path, actor_id='checker-2', role='checker', server='cluster-a',
            job_name='si-auto-01', phase='convergence', snapshot=SNAP_A, now=_now(10),
        )
    assert exc_actor.value.error_code == 'REVIEW_LEASE_REQUIRED'

    with pytest.raises(ContractError) as exc_role:
        att.verify_lease(
            tmp_path, actor_id='checker-1', role='maker', server='cluster-a',
            job_name='si-auto-01', phase='convergence', snapshot=SNAP_A, lease_id=lease['lease_id'], now=_now(10),
        )
    assert exc_role.value.error_code == 'REVIEW_LEASE_INVALID'

    # Different snapshot raises REVIEW_SNAPSHOT_MISMATCH.
    with pytest.raises(ContractError) as exc_snap:
        att.verify_lease(
            tmp_path, actor_id='checker-1', role='checker', server='cluster-a',
            job_name='si-auto-01', phase='convergence', snapshot=SNAP_B, now=_now(10),
        )
    assert exc_snap.value.error_code == 'REVIEW_SNAPSHOT_MISMATCH'

    # Different server is isolated.
    with pytest.raises(ContractError) as exc_srv:
        att.verify_lease(
            tmp_path, actor_id='checker-1', role='checker', server=None,
            job_name='si-auto-01', phase='convergence', snapshot=SNAP_A, now=_now(10),
        )
    assert exc_srv.value.error_code == 'REVIEW_LEASE_REQUIRED'

    # After TTL (901s) -> REVIEW_LEASE_EXPIRED.
    with pytest.raises(ContractError) as exc_exp:
        att.verify_lease(
            tmp_path, actor_id='checker-1', role='checker', server='cluster-a',
            job_name='si-auto-01', phase='convergence', snapshot=SNAP_A, now=_now(901),
        )
    assert exc_exp.value.error_code == 'REVIEW_LEASE_EXPIRED'

    # Pruning removes expired leases and preserves active ones.
    fresh = att.issue_lease(
        tmp_path, actor_id='checker-1', role='checker', server='cluster-a',
        job_name='si-auto-01', phase='convergence', snapshot=SNAP_B, now=_now(902),
    )
    assert not path.exists()
    assert [item['lease_id'] for item in att.list_leases(tmp_path, now=_now(910))] == [fresh['lease_id']]


def test_tampered_lease_is_rejected(tmp_path):
    lease = att.issue_lease(
        tmp_path, actor_id='checker-1', role='checker', server=None,
        job_name='si-01', phase='scf', snapshot=SNAP_A, now=_now(),
    )
    path = att.lease_path(tmp_path, lease['lease_id'])
    tampered = json.loads(path.read_text(encoding='utf-8'))
    tampered['snapshot'] = SNAP_B
    path.write_text(json.dumps(tampered) + '\n', encoding='utf-8', newline='\n')

    with pytest.raises(ContractError) as exc:
        att.verify_lease(
            tmp_path, actor_id='checker-1', role='checker', server=None,
            job_name='si-01', phase='scf', snapshot=SNAP_B, lease_id=lease['lease_id'], now=_now(5),
        )
    assert exc.value.error_code == 'REVIEW_LEASE_INVALID'


# ------------------------------------ 3. Policy enforcement & attestations

def test_maker_checker_policy_and_submit_attestation_gate(tmp_path):
    contract = _sample_contract('maker_checker')
    lease = att.issue_lease(
        tmp_path, actor_id='sub_checker_01', role='checker', server='cluster-a',
        job_name='si-auto-01', phase='convergence', snapshot=SNAP_A, now=_now(),
    )
    reviewer = att.format_reviewer(role='checker', host='antigravity', model='pro', actor_id='sub_checker_01')

    saved = att.save_attestation(
        tmp_path, server='cluster-a', job_name='si-auto-01', phase='convergence', snapshot=SNAP_A,
        role='checker', checker_actor_id='sub_checker_01', reviewer=reviewer, lease_id=lease['lease_id'],
        allowed=True, checks=_pass_checks(), contract_id=contract['contract_id'],
        maker_actor_id='maker_main_01', review_mode='maker_checker',
        host='antigravity', model='pro', now=_now(5),
    )
    file_path = att.attestation_path(tmp_path, 'si-auto-01', 'convergence', SNAP_A, server='cluster-a')
    assert file_path.name == f'si-auto-01-convergence-{SNAP_A[:12]}.json'
    assert file_path.parent.name == 'cluster-a'
    assert b'\r\n' not in file_path.read_bytes()
    assert saved['blocking_items'] == []

    # Maker with a distinct actor_id passes submit gate.
    verified = att.verify_attestation_for_submit(
        tmp_path, contract, server='cluster-a', job_name='si-auto-01', phase='convergence',
        snapshot=SNAP_A, submitting_actor_id='maker_main_01',
    )
    assert verified['enforced'] is True
    assert verified['mode'] == 'maker_checker'
    assert verified['checker_actor_id'] == 'sub_checker_01'
    assert verified['stage_gate_remote_enforced'] is True

    # Self-approval refusal: submitter actor_id == checker actor_id.
    with pytest.raises(ContractError) as exc_self:
        att.verify_attestation_for_submit(
            tmp_path, contract, server='cluster-a', job_name='si-auto-01', phase='convergence',
            snapshot=SNAP_A, submitting_actor_id='sub_checker_01',
        )
    assert exc_self.value.error_code == 'REVIEW_SELF_APPROVAL_FORBIDDEN'

    # Replay across a different contract is rejected.
    other_contract = _sample_contract('maker_checker', contract_id='ctr-20261127-deadbeef')
    with pytest.raises(ContractError) as exc_cid:
        att.verify_attestation_for_submit(
            tmp_path, other_contract, server='cluster-a', job_name='si-auto-01', phase='convergence',
            snapshot=SNAP_A, submitting_actor_id='maker_main_01',
        )
    assert exc_cid.value.error_code == 'REVIEW_ATTESTATION_MISMATCH'

    # Snapshot drift is rejected.
    with pytest.raises(ContractError) as exc_snap:
        att.verify_attestation_for_submit(
            tmp_path, contract, server='cluster-a', job_name='si-auto-01', phase='convergence',
            snapshot=SNAP_B, submitting_actor_id='maker_main_01',
        )
    assert exc_snap.value.error_code == 'REVIEW_ATTESTATION_MISSING'

    # Cross-server lookup is isolated (does not see cluster-a's attestation) and saving cluster-b never clobbers cluster-a.
    with pytest.raises(ContractError) as exc_srv:
        att.verify_attestation_for_submit(
            tmp_path, contract, server='cluster-b', job_name='si-auto-01', phase='convergence',
            snapshot=SNAP_A, submitting_actor_id='maker_main_01',
        )
    assert exc_srv.value.error_code == 'REVIEW_ATTESTATION_MISSING'

    att.save_attestation(
        tmp_path, server='cluster-b', job_name='si-auto-01', phase='convergence', snapshot=SNAP_A,
        role='checker', checker_actor_id='sub_checker_01', reviewer=reviewer, lease_id=lease['lease_id'],
        allowed=True, checks=_pass_checks(), contract_id=contract['contract_id'],
        maker_actor_id='maker_main_01', review_mode='maker_checker',
        host='antigravity', model='pro', now=_now(6),
    )
    assert att.load_attestation(tmp_path, server='cluster-a', job_name='si-auto-01', phase='convergence', snapshot=SNAP_A)['server'] == 'cluster-a'
    assert att.load_attestation(tmp_path, server='cluster-b', job_name='si-auto-01', phase='convergence', snapshot=SNAP_A)['server'] == 'cluster-b'


def test_maker_direct_allowed_and_maker_attestation_blocked_under_maker_checker(tmp_path):
    direct_contract = _sample_contract('maker_direct', contract_id='ctr-20261127-direct01')
    strict_contract = _sample_contract('maker_checker', contract_id='ctr-20261127-direct01')
    lease = att.issue_lease(
        tmp_path, actor_id='maker_01', role='maker', server=None,
        job_name='si-direct', phase='bands', snapshot=SNAP_A, now=_now(),
    )
    reviewer = att.format_reviewer(role='maker', host='codex', model='o3', actor_id='maker_01')
    att.save_attestation(
        tmp_path, server=None, job_name='si-direct', phase='bands', snapshot=SNAP_A,
        role='maker', checker_actor_id='maker_01', reviewer=reviewer, lease_id=lease['lease_id'],
        allowed=True, checks=_pass_checks(), contract_id=direct_contract['contract_id'],
        maker_actor_id='maker_01', review_mode='maker_direct', host='codex', model='o3', now=_now(5),
    )

    # Explicitly authorized maker_direct passes.
    ok = att.verify_attestation_for_submit(
        tmp_path, direct_contract, server=None, job_name='si-direct', phase='bands',
        snapshot=SNAP_A, submitting_actor_id='maker_01',
    )
    assert ok['enforced'] is True and ok['mode'] == 'maker_direct'

    # Same maker-signed attestation is rejected if contract requires maker_checker.
    with pytest.raises(ContractError) as exc:
        att.verify_attestation_for_submit(
            tmp_path, strict_contract, server=None, job_name='si-direct', phase='bands',
            snapshot=SNAP_A, submitting_actor_id='maker_02',
        )
    assert exc.value.error_code == 'REVIEW_CHECKER_ROLE_REQUIRED'


def test_blocked_or_tampered_attestation_cannot_pass_submit_gate(tmp_path):
    contract = _sample_contract('maker_checker')
    lease = att.issue_lease(
        tmp_path, actor_id='chk_1', role='checker', server=None,
        job_name='si-dos', phase='dos', snapshot=SNAP_A, now=_now(),
    )
    reviewer = att.format_reviewer(role='checker', actor_id='chk_1')

    # Inconsistent allowed=True with blocking item or program_failures is refused at save time.
    with pytest.raises(ContractError) as exc_inc:
        att.save_attestation(
            tmp_path, server=None, job_name='si-dos', phase='dos', snapshot=SNAP_A,
            role='checker', checker_actor_id='chk_1', reviewer=reviewer, lease_id=lease['lease_id'],
            allowed=True, checks=_blocked_checks(), contract_id=contract['contract_id'], now=_now(),
        )
    assert exc_inc.value.error_code == 'REVIEW_BLOCKED_INCONSISTENT'

    with pytest.raises(ContractError) as exc_pf:
        att.save_attestation(
            tmp_path, server=None, job_name='si-dos', phase='dos', snapshot=SNAP_A,
            role='checker', checker_actor_id='chk_1', reviewer=reviewer, lease_id=lease['lease_id'],
            allowed=True, checks=_pass_checks(), contract_id=contract['contract_id'],
            program_failures=['nbands_insufficient'], now=_now(),
        )
    assert exc_pf.value.error_code == 'REVIEW_BLOCKED_INCONSISTENT'

    # Properly saved blocked attestation is refused by submit gate.
    saved = att.save_attestation(
        tmp_path, server=None, job_name='si-dos', phase='dos', snapshot=SNAP_A,
        role='checker', checker_actor_id='chk_1', reviewer=reviewer, lease_id=lease['lease_id'],
        allowed=False, checks=_blocked_checks(), contract_id=contract['contract_id'], now=_now(),
    )
    assert saved['blocking_items'] == ['dos_inputs']
    with pytest.raises(ContractError) as exc_blk:
        att.verify_attestation_for_submit(
            tmp_path, contract, server=None, job_name='si-dos', phase='dos',
            snapshot=SNAP_A, submitting_actor_id='maker_1',
        )
    assert exc_blk.value.error_code == 'REVIEW_ATTESTATION_BLOCKED'

    # Tampering allowed -> True on disk without re-hashing is rejected.
    path = att.attestation_path(tmp_path, 'si-dos', 'dos', SNAP_A)
    raw = json.loads(path.read_text(encoding='utf-8'))
    raw['allowed'] = True
    raw['blocking_items'] = []
    path.write_text(json.dumps(raw) + '\n', encoding='utf-8', newline='\n')
    with pytest.raises(ContractError) as exc_tamp:
        att.verify_attestation_for_submit(
            tmp_path, contract, server=None, job_name='si-dos', phase='dos',
            snapshot=SNAP_A, submitting_actor_id='maker_1',
        )
    assert exc_tamp.value.error_code == 'REVIEW_ATTESTATION_INVALID'


# ------------------------------------ 4. Symlink defenses (mock + real OS)

def test_symlink_store_and_file_refused_via_mock(tmp_path, monkeypatch):
    ws = tmp_path.resolve()
    target = ws / '.agent' / 'state' / 'review-attestations'
    orig_is_symlink = Path.is_symlink

    def fake_is_symlink(self: Path) -> bool:
        if self == target:
            return True
        return orig_is_symlink(self)

    monkeypatch.setattr(Path, 'is_symlink', fake_is_symlink)
    with pytest.raises(ContractError) as exc:
        att.issue_lease(
            ws, actor_id='chk', role='checker', server=None,
            job_name='si-01', phase='convergence', snapshot=SNAP_A, now=_now(),
        )
    assert exc.value.error_code == 'REVIEW_SYMLINK_REFUSED'


def test_real_symlink_attestation_dir_refused(tmp_path):
    outside = tmp_path / 'outside'
    outside.mkdir()
    ws = tmp_path / 'ws'
    state = ws / '.agent' / 'state'
    state.mkdir(parents=True)
    link = state / 'review-attestations'
    if not _try_symlink(link, outside, target_is_directory=True):
        pytest.skip('OS does not permit creating directory symlinks without privilege')
    with pytest.raises(ContractError) as exc:
        att.issue_lease(
            ws, actor_id='chk', role='checker', server=None,
            job_name='si-01', phase='convergence', snapshot=SNAP_A, now=_now(),
        )
    assert exc.value.error_code == 'REVIEW_SYMLINK_REFUSED'


# ------------------------------------ 5. Layer 3 AST zero-upward-import guard

def test_layer3_review_attestation_has_zero_upward_imports():
    """Layer 3 review_rules and review_attestation must never import Layer 4 chat/ or control/."""
    from vasp_agent.permissions import review_rules

    forbidden = ('chat', 'control')
    violations = []
    for mod in (review_rules, att):
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


# ------------------------------------ 6. P2b ControlService end-to-end tests

def _build_checks_for(ctx: dict, *, block_first: bool = False) -> list[dict]:
    meta = ctx['summary'] if ctx.get('payload_overflow') else ctx
    phase = meta['phase']
    out = []
    for idx, item in enumerate(meta['required_checks']):
        blocked = block_first and idx == 0
        out.append({
            'item': item,
            'judgment': 'blocked' if blocked else ('pending' if phase == 'convergence' and item == 'validation_plan' else 'pass'),
            'blocking': blocked,
            'evidence': f'verified {phase}/{item}',
            'reason': 'program precheck failed' if blocked else 'independent check passed',
            'next_action': 'fix inputs' if blocked else '无需',
        })
    return out


def _setup_p2b_local_contract(
    tmp_path: Path, monkeypatch, *, job_name: str = 'si-mc-01', review_mode: str = 'maker_checker',
    optional_stages: list[str] | None = None, maker_actor_id: str = 'maker-main-01',
):
    import subprocess
    from types import SimpleNamespace
    from pymatgen.io.vasp.inputs import Potcar
    from vasp_agent.control import ControlService
    from vasp_agent.permissions import contract_ui, task_contract
    import vasp_agent.workflow.vasp_band_agent as core

    root = Path(__file__).resolve().parents[1]
    fake_pot = [SimpleNamespace(element='Si', functional='PBE', TITEL='PAW_PBE Si 05Jan2001', ZVAL=4.0, enmax=245.345)]
    monkeypatch.setattr(Potcar, 'from_str', classmethod(lambda _cls, _text: fake_pot))
    monkeypatch.setattr(Potcar, 'from_file', classmethod(lambda _cls, _path: fake_pot))
    monkeypatch.setattr(task_contract, 'has_real_tty', lambda stream=None: True)
    monkeypatch.setattr(
        subprocess, 'run',
        lambda cmd, **_kw: SimpleNamespace(returncode=0, stdout='99001;local\n', stderr=''),
    )

    ws = tmp_path / 'ws'
    (ws / 'structures').mkdir(parents=True, exist_ok=True)
    (ws / 'structures/Si.POSCAR').write_bytes((root / 'examples/Si.POSCAR').read_bytes())
    (ws / 'potentials/PBE/Si').mkdir(parents=True, exist_ok=True)
    (ws / 'potentials/PBE/Si/POTCAR').write_text('PAW_PBE Si 05Jan2001\n', encoding='utf-8', newline='\n')
    script_file = ws / 'submit-template.slurm'
    script_file.write_text(
        '#!/bin/bash\n#SBATCH --job-name=va-demo\n#SBATCH --partition=compute\n'
        '#SBATCH --nodes=1\n#SBATCH --ntasks=16\n#SBATCH --cpus-per-task=1\n'
        '#SBATCH --time=02:00:00\n#SBATCH --output=slurm-%j.log\n'
        'export OMP_NUM_THREADS=1\npython3 vasp_band_agent.py run . -- srun vasp_std\n',
        encoding='utf-8', newline='\n',
    )

    phases = ['convergence', 'relaxation', 'scf', 'bands'] + (list(optional_stages) if optional_stages else [])
    base = {'submission_mode': 'allocation', 'parallel_tests': 1, 'cores_per_test': 16, 'nodes': 1, 'partition': 'compute', 'walltime': '02:00:00'}
    envelope = {
        'allowed_modes': ['allocation'], 'allowed_partitions': ['compute'],
        'max_parallel_tests': 1, 'max_cores_per_test': 16, 'max_nodes': 1, 'max_total_cores': 16,
        'max_walltime': '04:00:00', 'phase_defaults': {p: dict(base) for p in phases},
    }

    maker = ControlService(ws, role='maker', actor_id=maker_actor_id, host='antigravity', model='pro')
    maker.box.submit_script = script_file.resolve()
    maker.box.script_hash = core.digest(maker.box.submit_script)

    draft_args = {
        'server': None, 'job_name': job_name, 'structure': 'structures/Si.POSCAR',
        'encut': 400, 'mesh': [4, 4, 4], 'nbands': 16, 'relax_mode': 'cell',
        'goal': 'Si P2b Maker-Checker test', 'resource_envelope': envelope,
        'allowed_phases': phases, 'review_mode': review_mode,
    }
    if optional_stages:
        draft_args['optional_stages'] = optional_stages
    draft_res = maker.call('vasp_draft_contract', draft_args)
    assert draft_res['ok'] is True, draft_res
    approved = contract_ui.approve_contract_interactive(
        ws, draft_res['result']['draft_id'], reader=lambda _p: '1', output=lambda _s: None,
    )
    cid = approved['contract']['contract_id']
    prep = maker.call('vasp_prepare_task', {'contract_id': cid})
    assert prep['ok'] is True, prep
    return ws, maker, approved['contract']


def test_p2b_maker_checker_end_to_end_self_review_expiry_and_submit(tmp_path, monkeypatch):
    from vasp_agent.control import ControlService
    from vasp_agent.permissions import task_contract

    ws, maker, contract = _setup_p2b_local_contract(tmp_path, monkeypatch, job_name='si-e2e-01')
    cid = contract['contract_id']
    assert contract['maker_actor_id'] == 'maker-main-01'

    # 1) Submit before review is blocked locally with REVIEW_ATTESTATION_MISSING and zero budget consumed.
    pre = maker.call('vasp_submit_stage', {'contract_id': cid, 'server': None, 'job_name': 'si-e2e-01', 'phase': 'convergence'})
    assert pre['ok'] is False and pre['error_code'] == 'REVIEW_ATTESTATION_MISSING'
    assert task_contract.read_ledger(ws, cid)['submissions_used'] == 0

    # 2) Maker cannot self-review under maker_checker.
    m_got = maker.call('vasp_get_stage_review', {'server': None, 'job_name': 'si-e2e-01'})
    assert m_got['ok'] is True and m_got['result']['review_lease']['role'] == 'maker'
    m_snap = m_got['result']['snapshot']
    m_rec = maker.call('vasp_record_stage_review', {
        'server': None, 'job_name': 'si-e2e-01', 'snapshot': m_snap, 'phase': 'convergence',
        'checks': _build_checks_for(m_got['result']),
    })
    assert m_rec['ok'] is False and m_rec['error_code'] == 'REVIEW_CHECKER_ROLE_REQUIRED'

    # 3) Checker sharing the same actor_id as Maker is refused with REVIEW_SELF_APPROVAL_FORBIDDEN.
    same_id_checker = ControlService(ws, role='checker', actor_id='maker-main-01', host='antigravity', model='pro')
    s_got = same_id_checker.call('vasp_get_stage_review', {'server': None, 'job_name': 'si-e2e-01'})
    assert s_got['ok'] is True
    s_rec = same_id_checker.call('vasp_record_stage_review', {
        'server': None, 'job_name': 'si-e2e-01', 'snapshot': m_snap, 'phase': 'convergence',
        'checks': _build_checks_for(s_got['result']),
    })
    assert s_rec['ok'] is False and s_rec['error_code'] == 'REVIEW_SELF_APPROVAL_FORBIDDEN'

    # 4) Independent Checker without lease -> REVIEW_LEASE_REQUIRED.
    checker = ControlService(ws, role='checker', actor_id='sub_9a4c12ff88', host='antigravity', model='pro')
    no_lease = checker.call('vasp_record_stage_review', {
        'server': None, 'job_name': 'si-e2e-01', 'snapshot': m_snap, 'phase': 'convergence',
        'checks': _build_checks_for(m_got['result']),
    })
    assert no_lease['ok'] is False and no_lease['error_code'] == 'REVIEW_LEASE_REQUIRED'

    # 5) Checker gets lease -> wrong snapshot fails with REVIEW_SNAPSHOT_MISMATCH.
    c_got = checker.call('vasp_get_stage_review', {'server': None, 'job_name': 'si-e2e-01'})
    assert c_got['ok'] is True and c_got['result']['review_lease']['role'] == 'checker'
    bad_snap = checker.call('vasp_record_stage_review', {
        'server': None, 'job_name': 'si-e2e-01', 'snapshot': SNAP_B, 'phase': 'convergence',
        'checks': _build_checks_for(c_got['result']),
    })
    assert bad_snap['ok'] is False and bad_snap['error_code'] == 'REVIEW_SNAPSHOT_MISMATCH'

    # 5b) Same actor_id on a different model cannot reuse another model's read lease.
    other_model_checker = ControlService(ws, role='checker', actor_id='sub_9a4c12ff88', host='antigravity', model='flash')
    cross_model_rec = other_model_checker.call('vasp_record_stage_review', {
        'server': None, 'job_name': 'si-e2e-01', 'snapshot': m_snap, 'phase': 'convergence',
        'checks': _build_checks_for(c_got['result']),
    })
    assert cross_model_rec['ok'] is False and cross_model_rec['error_code'] == 'REVIEW_LEASE_REQUIRED'
    cross_model_explicit = other_model_checker.call('vasp_record_stage_review', {
        'server': None, 'job_name': 'si-e2e-01', 'snapshot': m_snap, 'phase': 'convergence',
        'lease_id': c_got['result']['review_lease']['lease_id'],
        'checks': _build_checks_for(c_got['result']),
    })
    assert cross_model_explicit['ok'] is False and cross_model_explicit['error_code'] == 'REVIEW_LEASE_INVALID'

    # 6) Expired lease is rejected with REVIEW_LEASE_EXPIRED.
    real_now = att._ledger.utc_now
    future = real_now() + timedelta(minutes=20)
    monkeypatch.setattr(att._ledger, 'utc_now', lambda now=None: future if now is None else real_now(now))
    exp_rec = checker.call('vasp_record_stage_review', {
        'server': None, 'job_name': 'si-e2e-01', 'snapshot': m_snap, 'phase': 'convergence',
        'checks': _build_checks_for(c_got['result']),
    })
    monkeypatch.setattr(att._ledger, 'utc_now', real_now)
    assert exp_rec['ok'] is False and exp_rec['error_code'] == 'REVIEW_LEASE_EXPIRED'

    # 7) Valid Checker review records structured reviewer & local attestation -> Maker submits cleanly.
    ok_rec = checker.call('vasp_record_stage_review', {
        'server': None, 'job_name': 'si-e2e-01', 'snapshot': m_snap, 'phase': 'convergence',
        'checks': _build_checks_for(c_got['result']),
    })
    assert ok_rec['ok'] is True, ok_rec
    assert ok_rec['result']['reviewer'] == 'checker:antigravity:pro:sub_9a4c12ff'
    assert ok_rec['result']['attestation']['contract_id'] == cid
    assert ok_rec['result']['attestation']['allowed'] is True

    # Remote/local engine review file also holds the structured reviewer tag.
    review_file = json.loads((ws / 'runs/si-e2e-01/review-convergence.json').read_text(encoding='utf-8'))
    assert review_file['reviewer'] == 'checker:antigravity:pro:sub_9a4c12ff'

    # Checker cannot call submit or write tools.
    assert checker.call('vasp_submit_stage', {
        'contract_id': cid, 'server': None, 'job_name': 'si-e2e-01', 'phase': 'convergence',
    })['error_code'] == 'TOOL_NOT_ALLOWED_FOR_ROLE'

    # Maker with SAME actor_id as checker cannot submit using that attestation!
    spoof_maker = ControlService(ws, role='maker', actor_id='sub_9a4c12ff88', host='antigravity', model='pro')
    spoof_maker.box.submit_script = maker.box.submit_script
    spoof_maker.box.script_hash = maker.box.script_hash
    spoof_sub = spoof_maker.call('vasp_submit_stage', {
        'contract_id': cid, 'server': None, 'job_name': 'si-e2e-01', 'phase': 'convergence',
    })
    assert spoof_sub['ok'] is False and spoof_sub['error_code'] == 'REVIEW_SELF_APPROVAL_FORBIDDEN'

    # Legitimate Maker submits cleanly and receives enforced attestation metadata.
    sub = maker.call('vasp_submit_stage', {
        'contract_id': cid, 'server': None, 'job_name': 'si-e2e-01', 'phase': 'convergence',
    })
    assert sub['ok'] is True, sub
    assert sub['result']['job_id'] == '99001'
    assert sub['result']['review_attestation']['enforced'] is True
    assert sub['result']['review_attestation']['reviewer'] == 'checker:antigravity:pro:sub_9a4c12ff'


def test_p2b_maker_direct_explicit_authorization_and_standalone_refusal(tmp_path, monkeypatch):
    from vasp_agent.control import ControlService

    ws, maker, contract = _setup_p2b_local_contract(
        tmp_path, monkeypatch, job_name='si-direct-01', review_mode='maker_direct', maker_actor_id='mk-direct-1',
    )
    cid = contract['contract_id']

    got = maker.call('vasp_get_stage_review', {'server': None, 'job_name': 'si-direct-01'})
    assert got['ok'] is True
    rec = maker.call('vasp_record_stage_review', {
        'server': None, 'job_name': 'si-direct-01', 'snapshot': got['result']['snapshot'],
        'phase': 'convergence', 'checks': _build_checks_for(got['result']),
    })
    assert rec['ok'] is True, rec
    assert rec['result']['reviewer'] == 'maker:antigravity:pro:mk-direct-1'
    assert rec['result']['review_mode'] == 'maker_direct'

    sub = maker.call('vasp_submit_stage', {
        'contract_id': cid, 'server': None, 'job_name': 'si-direct-01', 'phase': 'convergence',
    })
    assert sub['ok'] is True, sub
    assert sub['result']['review_attestation']['enforced'] is True
    assert sub['result']['review_attestation']['mode'] == 'maker_direct'

    # Without an active maker_direct contract covering a job, maker is refused on vasp_record_stage_review.
    other_ws = tmp_path / 'standalone'
    other_ws.mkdir()
    standalone_maker = ControlService(other_ws, role='maker', actor_id='mk-solo')
    att.issue_lease(
        other_ws, actor_id='mk-solo', role='maker', server=None,
        job_name='uncontracted', phase='convergence', snapshot=SNAP_A,
    )
    denied = standalone_maker.call('vasp_record_stage_review', {
        'server': None, 'job_name': 'uncontracted', 'snapshot': SNAP_A,
        'phase': 'convergence', 'checks': _pass_checks(),
    })
    assert denied['ok'] is False and denied['error_code'] == 'REVIEW_CHECKER_ROLE_REQUIRED'


def test_p2b_dos_program_failures_veto_cannot_be_overridden(tmp_path, monkeypatch):
    from vasp_agent.control import ControlService
    from vasp_agent.workflow import dos_stage, stage_gate
    import vasp_agent.workflow.vasp_band_agent as core
    from pymatgen.io.vasp.inputs import Kpoints

    ws, maker, contract = _setup_p2b_local_contract(
        tmp_path, monkeypatch, job_name='si-dos-veto', optional_stages=['dos'],
    )
    cid = contract['contract_id']
    folder = ws / 'runs/si-dos-veto'
    root = Path(__file__).resolve().parents[1]
    plan = json.loads((folder / 'input-plan.json').read_text(encoding='utf-8'))

    # Advance convergence -> relaxation -> scf -> bands so next stage is dos.
    for phase in ('convergence', 'relaxation', 'scf', 'bands'):
        for rel in plan['incars']:
            if stage_gate.phase_of(rel) != phase:
                continue
            sdir = folder / rel
            sdir.mkdir(parents=True, exist_ok=True)
            for name in ('POSCAR', 'INCAR', 'KPOINTS'):
                if not (sdir / name).is_file():
                    if rel == 'scf' and name == 'KPOINTS':
                        Kpoints.gamma_automatic([4, 4, 4]).write_file(sdir / name)
                    elif rel == 'scf' and name == 'POSCAR':
                        (sdir / name).write_bytes((root / 'examples/Si.POSCAR').read_bytes())
                    else:
                        (sdir / name).write_text(f'{rel}:{name}\n', encoding='utf-8', newline='\n')
            xml = sdir / 'vasprun.xml'
            xml.write_text('<modeling/>\n', encoding='utf-8', newline='\n')
            core.save(sdir / 'inputs.json', {n: core.digest(sdir / n) for n in ('POSCAR', 'INCAR', 'KPOINTS')})
            stamp = {'xml_sha256': core.digest(xml)}
            if rel == 'scf':
                chg = sdir / 'CHGCAR'
                chg.write_text('Si CHGCAR\n', encoding='utf-8', newline='\n')
                stamp['charge_sha256'] = core.digest(chg)
                core.save(folder / 'scf-summary.json', {'efermi': 5.0, 'nelect': 8.0, 'nbands': 16, 'mesh': [4, 4, 4]})
            core.save(sdir / 'complete.json', stamp)
        if phase == 'convergence':
            core.save(folder / 'convergence.json', {'passed': True, 'encut': 420, 'mesh': [4, 4, 4]})
        elif phase == 'relaxation':
            (folder / 'relaxed-2.POSCAR').write_bytes((root / 'examples/Si.POSCAR').read_bytes())
            (folder / 'relax-final').mkdir(exist_ok=True)
            (folder / 'relax-final/CONTCAR').write_bytes((root / 'examples/Si.POSCAR').read_bytes())
            core.save(folder / 'relaxation.json', {'passed': True})
            core.save(folder / 'post-relax-validation.json', {'passed': True})
        stage_gate.complete(folder, phase)

    # Force a real DOS program pre-check failure (16 bands matching scf-summary, all below E_F + window upper edge).
    monkeypatch.setattr(
        dos_stage, 'scf_bands',
        lambda _root: {
            'band_minima_eV': [-5.0 + 0.2 * idx for idx in range(16)],
            'lowest_eigenvalue_eV': -5.0,
            'kpoints': 8,
        },
    )

    checker = ControlService(ws, role='checker', actor_id='chk-dos-1', host='antigravity', model='pro')
    got = checker.call('vasp_get_stage_review', {'server': None, 'job_name': 'si-dos-veto'})
    assert got['ok'] is True, got
    meta = got['result']['summary'] if got['result'].get('payload_overflow') else got['result']
    assert meta['phase'] == 'dos'
    snap = meta['snapshot']

    # Attempting to pass when program_failures exist is vetoed by stage_gate.record!
    vetoed = checker.call('vasp_record_stage_review', {
        'server': None, 'job_name': 'si-dos-veto', 'snapshot': snap, 'phase': 'dos',
        'checks': _build_checks_for(got['result'], block_first=False),
    })
    assert vetoed['ok'] is False
    assert '程序预检未通过' in vetoed['error']
    assert not att.attestation_path(ws, 'si-dos-veto', 'dos', snap, server=None).exists()

    # Recording blocked succeeds with allowed=False, records program_failures, and submit is blocked.
    blocked_rec = checker.call('vasp_record_stage_review', {
        'server': None, 'job_name': 'si-dos-veto', 'snapshot': snap, 'phase': 'dos',
        'checks': _build_checks_for(got['result'], block_first=True),
    })
    assert blocked_rec['ok'] is True, blocked_rec
    assert blocked_rec['result']['allowed'] is False
    assert blocked_rec['result']['attestation']['blocking_items']
    assert blocked_rec['result']['attestation']['program_failures'] == ['window']

    sub = maker.call('vasp_submit_stage', {
        'contract_id': cid, 'server': None, 'job_name': 'si-dos-veto', 'phase': 'dos',
    })
    assert sub['ok'] is False
    assert sub['error_code'] in ('REVIEW_PROGRAM_PRECHECK_BLOCKED', 'REVIEW_ATTESTATION_BLOCKED')


# ------------------------------------ 7. P2ab security hardening regressions

def _try_dir_link_or_junction(link: Path, target: Path) -> bool:
    import subprocess

    if _try_symlink(link, target, target_is_directory=True):
        return True
    if os.name == 'nt':
        proc = subprocess.run(
            ['cmd', '/c', 'mklink', '/J', str(link), str(target)],
            capture_output=True, check=False,
        )
        return proc.returncode == 0
    return False


def test_inside_workspace_link_or_junction_and_external_link_raise_contract_error(tmp_path):
    ws = tmp_path / 'ws'
    inside = ws / 'inside_target'
    outside = tmp_path / 'outside_target'
    inside.mkdir(parents=True)
    outside.mkdir(parents=True)
    state = ws / '.agent' / 'state'
    state.mkdir(parents=True)

    # 1) Inside-workspace symlink / Windows NTFS junction MUST raise REVIEW_SYMLINK_REFUSED (never silently resolve!).
    link = state / 'review-attestations'
    if not _try_dir_link_or_junction(link, inside):
        pytest.skip('OS does not permit creating directory symlinks or NTFS junctions')
    with pytest.raises(ContractError) as exc_in:
        att.issue_lease(
            ws, actor_id='chk', role='checker', server=None,
            job_name='si-01', phase='convergence', snapshot=SNAP_A, now=_now(),
        )
    assert exc_in.value.error_code == 'REVIEW_SYMLINK_REFUSED'
    assert not (inside / 'leases').exists()

    # 2) Outside-workspace symlink / junction MUST raise ContractError('REVIEW_SYMLINK_REFUSED'), NOT bare ValueError!
    link.rmdir() if (os.name == 'nt' and link.is_dir()) else link.unlink()
    assert _try_dir_link_or_junction(link, outside)
    with pytest.raises(ContractError) as exc_out:
        att.issue_lease(
            ws, actor_id='chk', role='checker', server=None,
            job_name='si-01', phase='convergence', snapshot=SNAP_A, now=_now(),
        )
    assert exc_out.value.error_code == 'REVIEW_SYMLINK_REFUSED'
    assert not (outside / 'leases').exists()


def test_corrupt_lease_json_and_malformed_record_schemas_fail_safely(tmp_path):
    # Read-only calls never create .agent/ on clean workspace.
    assert att.list_leases(tmp_path, now=_now()) == []
    assert att.prune_expired_leases(tmp_path, now=_now()) == 0
    with pytest.raises(ContractError) as exc_miss:
        att.load_attestation(tmp_path, server=None, job_name='si-01', phase='convergence', snapshot=SNAP_A)
    assert exc_miss.value.error_code == 'REVIEW_ATTESTATION_MISSING'
    assert not (tmp_path / '.agent').exists()

    # Non-dict JSON files in leases/ must never crash prune_expired_leases or issue_lease with AttributeError!
    leases_dir = tmp_path / '.agent' / 'state' / 'review-attestations' / 'leases'
    leases_dir.mkdir(parents=True)
    (leases_dir / 'lse-20261127-badlist00001.json').write_text('[]\n', encoding='utf-8', newline='\n')
    (leases_dir / 'lse-20261127-badnull00002.json').write_text('null\n', encoding='utf-8', newline='\n')
    assert att.prune_expired_leases(tmp_path, now=_now()) == 0
    lease = att.issue_lease(
        tmp_path, actor_id='chk-safe', role='checker', server=None,
        job_name='si-01', phase='convergence', snapshot=SNAP_A, now=_now(),
    )
    assert att.verify_lease(
        tmp_path, actor_id='chk-safe', role='checker', server=None,
        job_name='si-01', phase='convergence', snapshot=SNAP_A, now=_now(5),
    )['lease_id'] == lease['lease_id']

    # Malformed attestation with bool version (`True == 1`) or missing required fields -> REVIEW_ATTESTATION_INVALID.
    bad_path = att.attestation_path(tmp_path, 'si-01', 'convergence', SNAP_A, server=None)
    bad_path.parent.mkdir(parents=True, exist_ok=True)
    bad_bool_ver = {
        'attestation_version': True, 'contract_id': 'ctr-20261127-a1b2c3d4', 'server': None,
        'job_name': 'si-01', 'phase': 'convergence', 'snapshot': SNAP_A, 'role': 'checker',
        'reviewer': 'checker:external:unknown:chk', 'allowed': True,
    }
    bad_bool_ver['canonical_sha256'] = att.canonical_record_sha256(bad_bool_ver)
    bad_path.write_text(json.dumps(bad_bool_ver) + '\n', encoding='utf-8', newline='\n')
    with pytest.raises(ContractError) as exc_ver:
        att.load_attestation(tmp_path, server=None, job_name='si-01', phase='convergence', snapshot=SNAP_A)
    assert exc_ver.value.error_code == 'REVIEW_ATTESTATION_INVALID'

    bad_missing_fields = {
        'attestation_version': 1, 'contract_id': 'ctr-20261127-a1b2c3d4', 'server': None,
        'job_name': 'si-01', 'phase': 'convergence', 'snapshot': SNAP_A, 'role': 'checker',
        'reviewer': 'checker:external:unknown:chk', 'allowed': True,
    }
    bad_missing_fields['canonical_sha256'] = att.canonical_record_sha256(bad_missing_fields)
    bad_path.write_text(json.dumps(bad_missing_fields) + '\n', encoding='utf-8', newline='\n')
    with pytest.raises(ContractError) as exc_fields:
        att.verify_attestation_for_submit(
            tmp_path, _sample_contract('maker_checker'), server=None, job_name='si-01',
            phase='convergence', snapshot=SNAP_A, submitting_actor_id='maker-1',
        )
    assert exc_fields.value.error_code == 'REVIEW_ATTESTATION_INVALID'


def test_p2b_multi_contract_ambiguity_explicit_ids_phase_mismatch_and_pending_block(tmp_path, monkeypatch):
    from vasp_agent.control import ControlService
    from vasp_agent.permissions import contract_ui, task_contract

    ws, maker, c1 = _setup_p2b_local_contract(tmp_path, monkeypatch, job_name='si-ambig', review_mode='maker_checker')
    cid1 = c1['contract_id']

    # Save & approve a second active contract targeting the same job_name in maker_direct mode.
    draft1 = task_contract.load_draft(ws, c1['draft_id'])
    draft2 = {
        **draft1,
        'draft_id': task_contract.new_draft_id(),
        'review_policy': {'mode': 'maker_direct', 'auto_advance_on_green': True},
    }
    task_contract.save_draft(ws, draft2)
    c2, _ = task_contract.approve_draft(
        ws,
        draft2['draft_id'],
        provider=task_contract.LocalTTYApprovalProvider(reader=lambda _p: '1', output=lambda _s: None),
    )
    cid2 = c2['contract_id']

    checker = ControlService(ws, role='checker', actor_id='chk-ambig-1', host='antigravity', model='pro')
    got = checker.call('vasp_get_stage_review', {'server': 'local', 'job_name': 'si-ambig'})
    assert got['ok'] is True
    snap = got['result']['snapshot']
    lid = got['result']['review_lease']['lease_id']

    # 1) Omitting contract_id when two active contracts match raises REVIEW_CONTRACT_AMBIGUOUS.
    ambig = checker.call('vasp_record_stage_review', {
        'server': None, 'job_name': 'si-ambig', 'snapshot': snap, 'phase': 'convergence',
        'checks': _build_checks_for(got['result']),
    })
    assert ambig['ok'] is False and ambig['error_code'] == 'REVIEW_CONTRACT_AMBIGUOUS'

    # 2) Passing explicit contract_id + lease_id binds cleanly to c2 (maker_direct).
    ok_c2 = checker.call('vasp_record_stage_review', {
        'server': 'local', 'job_name': 'si-ambig', 'snapshot': snap, 'phase': 'convergence',
        'checks': _build_checks_for(got['result']), 'contract_id': cid2, 'lease_id': lid,
    })
    assert ok_c2['ok'] is True and ok_c2['result']['contract_id'] == cid2

    # 3) Replaying c2's attestation to submit under c1 (maker_checker) is rejected with REVIEW_ATTESTATION_MISMATCH!
    replay = maker.call('vasp_submit_stage', {
        'contract_id': cid1, 'server': None, 'job_name': 'si-ambig', 'phase': 'convergence',
    })
    assert replay['ok'] is False and replay['error_code'] == 'REVIEW_ATTESTATION_MISMATCH'

    # 4) Submitting a wrong phase is refused by resource_context pre-check, and verify_submit_attestation enforces REVIEW_PHASE_MISMATCH.
    from vasp_agent.control import service_review

    wrong_phase = maker.call('vasp_submit_stage', {
        'contract_id': cid2, 'server': None, 'job_name': 'si-ambig', 'phase': 'relaxation',
    })
    assert wrong_phase['ok'] is False
    with pytest.raises(ContractError) as exc_phase:
        service_review.verify_submit_attestation(
            maker, c2, task_contract.read_ledger(ws, cid2), server=None, job_name='si-ambig', phase='relaxation',
        )
    assert exc_phase.value.error_code == 'REVIEW_PHASE_MISMATCH'

    # 5) Recording `judgment='pending'` on a non-validation_plan item yields allowed=False and blocks submit.
    pending_checks = _build_checks_for(got['result'])
    target_item = next(item for item in pending_checks if item['item'] != 'validation_plan')
    target_item['judgment'] = 'pending'
    pend_rec = checker.call('vasp_record_stage_review', {
        'server': None, 'job_name': 'si-ambig', 'snapshot': snap, 'phase': 'convergence',
        'checks': pending_checks, 'contract_id': cid2,
    })
    assert pend_rec['ok'] is True and pend_rec['result']['allowed'] is False
    blocked_sub = maker.call('vasp_submit_stage', {
        'contract_id': cid2, 'server': None, 'job_name': 'si-ambig', 'phase': 'convergence',
    })
    assert blocked_sub['ok'] is False and blocked_sub['error_code'] == 'REVIEW_ATTESTATION_BLOCKED'
    assert task_contract.read_ledger(ws, cid2)['submissions_used'] == 0


def test_p2b_actor_id_validation_and_host_model_env_propagation(tmp_path, monkeypatch, capsys):
    from vasp_agent.control import ControlService, cli as control_cli

    with pytest.raises(ValueError, match='actor_id'):
        ControlService(tmp_path, role='checker', actor_id='bad\nactor')
    with pytest.raises(ValueError, match='actor_id'):
        ControlService(tmp_path, role='checker', actor_id='a' * 129)

    monkeypatch.setenv('VASP_AGENT_HOST', 'antigravity-env')
    monkeypatch.setenv('VASP_AGENT_MODEL', 'gemini-pro')
    svc = ControlService(tmp_path, role='checker', actor_id='chk-env-1')
    info = svc.call('vasp_workspace_info', {})
    assert info['ok'] is True
    assert info['result']['host'] == 'antigravity-env'
    assert info['result']['model'] == 'gemini-pro'

    rc = control_cli.main([
        '--workspace', str(tmp_path), '--role', 'checker', '--actor-id', 'chk-cli-1',
        '--host', 'codex-cli', '--model', 'o3-pro', 'call', 'vasp_workspace_info', '--json',
    ])
    assert rc == 0
    out = json.loads(capsys.readouterr().out)
    assert out['ok'] is True
    assert out['result']['host'] == 'codex-cli'
    assert out['result']['model'] == 'o3-pro'
