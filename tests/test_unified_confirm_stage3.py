"""Offline end-to-end regressions for Stage 3: /plan 3-in-1 startup confirmation window."""
from __future__ import annotations

import ast
import hashlib
import json
from pathlib import Path
from types import SimpleNamespace as NS

import pytest
from pymatgen.core import Lattice, Structure
from pymatgen.io.vasp.inputs import Kpoints, Poscar

from vasp_agent.chat import ai_agent as ai
from vasp_agent.permissions import resource_menu
from vasp_agent.planning import parameter_plans as plans
from vasp_agent.remote import environment_profiles as profiles, remote_compute, remote_worker as worker
from vasp_agent.workflow import vasp_band_agent as core

from test_dos_stage import SI, checks as passing_checks, vasp  # noqa: F401
from test_unified_confirm_stage2 import LOCAL_SLURM_SCRIPT, _setup_potentials, _setup_remote_harness


def _unified_box(workspace: Path, answers, *, submit_script: Path | None = None, potcar_library: Path | None = None):
    box = ai.Toolbox(workspace, submit_script=submit_script, potcar_library=potcar_library)
    box.require_scan_plans = True
    box.unified_plan_confirm = True
    box.ensure_current_release = lambda server: None
    shown = []
    box.plan_output = shown.append
    box.plan_reader = (lambda prompt: answers.pop(0)) if answers is not None else None
    return box, shown


def _setup_remote_new_task(tmp_path: Path, monkeypatch, vasp):
    """Reuse Stage 2 remote harness with current-release deployment and real remote_prepare."""
    from vasp_agent.remote.bundle import local_version

    workspace, remote_root, sbatch_calls = _setup_remote_harness(tmp_path, monkeypatch, vasp, run_to=None)
    engine_dir = Path(core.__file__).parent
    (remote_root / 'deployment.json').write_text(json.dumps({
        'release': str(engine_dir), 'version': local_version(),
    }), encoding='utf-8')
    monkeypatch.setattr(worker, 'deployed_release', lambda root: engine_dir)
    monkeypatch.setattr(worker, 'activate_release', lambda release: engine_dir)
    monkeypatch.setattr(worker, 'task_release', lambda meta: engine_dir)
    from vasp_agent.workflow import potential_library as potlib
    monkeypatch.setattr(
        potlib.Potcar, 'from_str',
        lambda text: [NS(element='Si', functional='PBE', enmax=245.0, ZVAL=4.0, TITEL='PAW_PBE Si')],
    )
    return workspace, remote_root, sbatch_calls


def _base_request(server='hpc', job_name='si-new', structure='Si.POSCAR', **extra):
    base = dict(server=server, job_name=job_name, structure=structure, encut=520, mesh=[4, 4, 4],
                nbands=16, relax_mode='cell', parallel_tests=None, cores_per_test=None)
    base.update(extra)
    return base


# --- 1. New task option 1: 3-in-1 startup -> prepare -> review -> submit convergence ---

def test_stage3_option1_remote_3in1_confirm_review_and_submit_once(tmp_path, monkeypatch, vasp):
    workspace, remote_root, sbatch_calls = _setup_remote_new_task(tmp_path, monkeypatch, vasp)
    # In ONE /plan menu: edit Block A goal (3), edit Block C to remote array 3x16 (5), then confirm+preauth (1).
    box, shown = _unified_box(workspace, [
        '3', 'Si 3D 体相非磁性 PBE 能带与收敛研究',
        '5', '模式=array 并发=3 每组=16 节点=1 时限=02:00:00',
        '1',
    ])
    draft = box.propose_scan_plan(
        **_base_request('hpc', 'si-new'),
        encuts=None, meshes=[[4, 4, 4], [6, 6, 6], [8, 8, 8]],
        origin='ai_suggestion', rationale='加密 k 网格；点数增加；收敛待验证',
    )
    text = '\n'.join(shown)
    assert '【区块 A · 物理模型与研究目标】' in text
    assert '【区块 B · 扫描参数、可选阶段与环境配置】' in text
    assert '【提交资源区】' in text and 'KPAR' in text
    assert draft['status'] == 'selected'
    assert draft['selection']['operator_confirmed'] is True
    assert draft['selection']['physics_confirmed'] is True
    assert draft['selection']['preauthorize_requested'] is True
    assert draft['selection']['submission_authorized'] is False
    assert box.allow_submit is False

    # Prepare writes directory, physics-model.json, submit.slurm (array), and activates 1-shot convergence grant.
    prepared = box.prepare_scan_plan(draft['id'])
    assert prepared['physics_confirmed'] is True
    assert prepared['resources_applied'] is True
    assert prepared['submission_preauthorized'] is True
    assert prepared['submission_target'] == {'server': 'hpc', 'job_name': 'si-new', 'phase': 'convergence'}
    assert box.allow_submit is True and box.submission_target == ('hpc', 'si-new', 'convergence')

    # Verify preparation_summary is synced with the applied resources (no stale 8-core summary or stale manifest).
    ps = prepared['preparation_summary']
    assert ps['resources']['cores_per_test'] == 16 and ps['resources']['parallel_tests'] == 3
    assert ps['resources']['submission_mode'] == 'array' and ps['resources']['ntasks'] == 16
    assert ps['execution_tags']['mpi_ranks_per_test'] == 16
    assert ps['execution_plan']['submission_mode'] == 'array' and ps['execution_plan']['array_elements'] == 6
    assert 'manifest' not in prepared and 'manifest_note' in prepared

    job_dir = remote_root / 'jobs' / 'si-new'
    physics_doc = json.loads((job_dir / 'physics-model.json').read_text(encoding='utf-8'))
    assert physics_doc['confirmed_by'] == 'operator_chat_menu'
    assert physics_doc['goal'] == 'Si 3D 体相非磁性 PBE 能带与收敛研究'
    slurm = (job_dir / 'submit.slurm').read_text(encoding='utf-8')
    assert '#SBATCH --array=0-5%3' in slurm and '#SBATCH --ntasks=16' in slurm and '#SBATCH --time=02:00:00' in slurm

    # Cannot use convergence pre-auth to submit relaxation or another job.
    with pytest.raises(core.ToolError) as err:
        box.submit_stage('hpc', 'si-new', 'relaxation')
    assert err.value.error_code == 'SUBMISSION_NOT_AUTHORIZED'
    with pytest.raises(core.ToolError) as err:
        box.submit_stage('hpc', 'other-job', 'convergence')
    assert err.value.error_code == 'SUBMISSION_NOT_AUTHORIZED'

    # No interactive menu needed anymore: get_stage_review -> record_stage_review -> submit_stage('convergence')!
    box.plan_reader = None
    review = box.get_stage_review('hpc', 'si-new')
    assert review['phase'] == 'convergence'
    assert review['physics']['goal'] == 'Si 3D 体相非磁性 PBE 能带与收敛研究'
    rec = box.record_stage_review('hpc', 'si-new', review['snapshot'], 'convergence', passing_checks(review))
    assert rec['allowed'] is True

    submitted = box.submit_stage('hpc', 'si-new', 'convergence')
    assert submitted['status'] == 'submitted' and submitted['job_id'] == '9001'
    assert len(sbatch_calls) == 1

    # --- 2. Strict single-stage isolation after convergence submission ---
    assert box.allow_submit is False
    with pytest.raises(core.ToolError) as err:
        box.submit_stage('hpc', 'si-new', 'relaxation')
    assert err.value.error_code == 'SUBMISSION_NOT_AUTHORIZED'
    again = box.prepare_scan_plan(draft['id'])
    assert again['already_prepared'] is True and box.allow_submit is False


# --- 3. Blocked review hard-blocks submission even when operator chose 1 (remote + local) ---

def test_stage3_option1_blocked_review_hard_blocks_remote_and_local(tmp_path, monkeypatch, vasp):
    workspace, _, sbatch_calls = _setup_remote_new_task(tmp_path / 'rem', monkeypatch, vasp)
    box, _ = _unified_box(workspace, ['1'])
    draft = box.propose_scan_plan(**_base_request('hpc', 'si-blk'), encuts=None, meshes=None,
                                  origin='tool_default', rationale='')
    prep = box.prepare_scan_plan(draft['id'])
    assert prep['physics_confirmed'] is True and prep['submission_preauthorized'] is True

    review = box.get_stage_review('hpc', 'si-blk')
    bad_checks = passing_checks(review)
    bad_checks[0]['judgment'] = 'blocked'
    bad_checks[0]['blocking'] = True
    assert box.record_stage_review('hpc', 'si-blk', review['snapshot'], 'convergence', bad_checks)['allowed'] is False
    with pytest.raises(core.ToolError):
        box.submit_stage('hpc', 'si-blk', 'convergence')
    assert sbatch_calls == []

    # Local case with submit_script
    local_ws = tmp_path / 'loc'
    local_ws.mkdir()
    Poscar(SI).write_file(local_ws / 'Si.POSCAR')
    pot_lib = _setup_potentials(local_ws)
    from vasp_agent.workflow import potential_library as potlib
    monkeypatch.setattr(
        potlib.Potcar, 'from_str',
        lambda text: [NS(element='Si', functional='PBE', enmax=245.0, ZVAL=4.0, TITEL='PAW_PBE Si')],
    )
    script = local_ws / 'template.slurm'
    script.write_text(LOCAL_SLURM_SCRIPT, encoding='utf-8')
    local_sbatch = []
    monkeypatch.setattr('vasp_agent.chat.toolbox_submission.subprocess.run',
                        lambda cmd, *a, **kw: local_sbatch.append(cmd) or NS(returncode=0, stdout='777\n', stderr=''))

    lbox, _ = _unified_box(local_ws, ['5', '每组=4 节点=1', '1'], submit_script=script, potcar_library=pot_lib)
    ldraft = lbox.propose_scan_plan(**_base_request(None, 'si-loc'), encuts=None, meshes=None,
                                    origin='tool_default', rationale='')
    lprep = lbox.prepare_scan_plan(ldraft['id'])
    assert lprep['physics_confirmed'] is True and lprep['submission_preauthorized'] is True
    assert lprep['resources']['cores_per_test'] == 4
    # Local manifest.json & preparation_summary are refreshed to match updated config.json hash.
    assert lprep['preparation_summary']['resources']['cores_per_test'] == 4
    assert lprep['manifest.json']['hashes']['config.json'] == core.digest(lbox.job('si-loc') / 'config.json')

    lrev = lbox.get_stage_review(None, 'si-loc')
    lbad = passing_checks(lrev)
    lbad[0]['judgment'] = 'blocked'
    lbad[0]['blocking'] = True
    assert lbox.record_stage_review(None, 'si-loc', lrev['snapshot'], 'convergence', lbad)['allowed'] is False
    with pytest.raises(ValueError, match='未通过|审阅|blocked|放行'):
        lbox.submit_stage(None, 'si-loc', 'convergence')
    assert local_sbatch == []

    lrev2 = lbox.get_stage_review(None, 'si-loc')
    assert lbox.record_stage_review(None, 'si-loc', lrev2['snapshot'], 'convergence', passing_checks(lrev2))['allowed'] is True
    lbox.plan_reader = None
    lsub = lbox.submit_stage(None, 'si-loc', 'convergence')
    assert lsub['status'] == 'submitted' and lsub['job_id'] == '777' and len(local_sbatch) == 1
    assert lbox.allow_submit is False


# --- 4. Option 2 (prepare-only) & revoke_submission clearing pending_plan_preauth ---

def test_stage3_option2_and_revoke_submission_prevent_grant_activation(tmp_path, monkeypatch, vasp):
    workspace, remote_root, sbatch_calls = _setup_remote_new_task(tmp_path, monkeypatch, vasp)
    box, _ = _unified_box(workspace, ['5', '每组=16 节点=2', '2'])
    box.allow_submit = True
    box.submission_target = None
    draft = box.propose_scan_plan(**_base_request('hpc', 'si-opt2'), encuts=None, meshes=None,
                                  origin='tool_default', rationale='')
    prep = box.prepare_scan_plan(draft['id'])
    assert prep['physics_confirmed'] is True
    assert prep['resources_applied'] is True and prep['resources']['cores_per_test'] == 16
    assert prep['submission_preauthorized'] is False and box.allow_submit is False
    assert (remote_root / 'jobs' / 'si-opt2' / 'physics-model.json').is_file()
    assert 'request_submission' in prep['next']
    with pytest.raises(core.ToolError) as err:
        box.submit_stage('hpc', 'si-opt2', 'convergence')
    assert err.value.error_code == 'SUBMISSION_NOT_AUTHORIZED'

    # Option 1 followed by revoke_submission (/submit off / session switch / model switch) MUST clear pending_plan_preauth!
    for reason, jname in (('operator_off', 'si-rev-off'), ('session_switch', 'si-rev-sess'), ('model_switch', 'si-rev-mod')):
        box_r, _ = _unified_box(workspace, ['1'])
        d = box_r.propose_scan_plan(**_base_request('hpc', jname), encuts=None, meshes=None,
                                    origin='tool_default', rationale='')
        assert d['id'] in box_r.pending_plan_preauth
        box_r.revoke_submission(reason)
        assert box_r.pending_plan_preauth == {}
        p = box_r.prepare_scan_plan(d['id'])
        assert p['physics_confirmed'] is True and p['resources_applied'] is True
        assert p['submission_preauthorized'] is False and box_r.allow_submit is False
    assert sbatch_calls == []


# --- 5. Block B edits, s-then-q disk immutability, 2D mesh check, blank/EOF, & conflict protection ---

def test_stage3_block_b_edits_preset_cancel_2d_and_conflict_protection(tmp_path, monkeypatch, vasp):
    Poscar(SI).write_file(tmp_path / 'Si.POSCAR')
    pot_lib = _setup_potentials(tmp_path)
    from vasp_agent.workflow import potential_library as potlib
    monkeypatch.setattr(
        potlib.Potcar, 'from_str',
        lambda text: [NS(element='Si', functional='PBE', enmax=245.0, ZVAL=4.0, TITEL='PAW_PBE Si')],
    )

    # Pressing 's' (toggle scan_preset) then 'q' (cancel) must NOT mutate saved request on disk,
    # so re-proposing with the original request does NOT trigger SCAN_PLAN_CONFLICT.
    box_sq, shown_sq = _unified_box(tmp_path, ['', 's', 'q'], potcar_library=pot_lib)
    cancelled = box_sq.propose_scan_plan(**_base_request(None, 'si-cancel'), encuts=None, meshes=None,
                                         origin='tool_default', rationale='')
    assert any('未收到选项' in line for line in shown_sq)
    assert cancelled['status'] == 'pending' and cancelled['request']['scan_preset'] == 'standard'
    assert not box_sq.job('si-cancel').exists()
    with pytest.raises(ValueError, match='尚未选择'):
        box_sq.prepare_scan_plan(cancelled['id'])
    # Re-proposing identical request with EOFError preserves pending state cleanly without conflict.
    def eof_reader(_prompt):
        raise EOFError()
    box_sq.plan_reader = eof_reader
    with pytest.raises(EOFError):
        box_sq.propose_scan_plan(**_base_request(None, 'si-cancel'), encuts=None, meshes=None,
                                 origin='tool_default', rationale='')
    assert plans.list_plans(box_sq)['plans'][0]['status'] == 'pending'

    # Edit Block B: switch AI suggestion back to tool default, edit DOS param, confirm (2).
    box_edit, _ = _unified_box(tmp_path, ['4', 'a', '1', '4', 'b', '3', 'kmesh_factor=3', '2'], potcar_library=pot_lib)
    draft = box_edit.propose_scan_plan(
        **_base_request(None, 'si-dos-edit', optional_stages=['dos']),
        encuts=None, meshes=[[4, 4, 4], [6, 6, 6], [8, 8, 8]],
        origin='ai_suggestion', rationale='测试切换默认与修改 DOS 参数',
    )
    assert draft['status'] == 'selected' and draft['choice'] == 'operator_default'
    assert draft['stage_parameters']['dos']['selected']['kmesh_factor'] == 3
    prep = box_edit.prepare_scan_plan(draft['id'])
    assert prep['physics_confirmed'] is True and prep['submission_preauthorized'] is False
    assert list(Kpoints.from_file(box_edit.job('si-dos-edit') / 'convergence/mesh-1/KPOINTS').kpts[0]) == [5, 5, 5]
    cfg = json.loads((box_edit.job('si-dos-edit') / 'config.json').read_text(encoding='utf-8'))
    assert cfg['stage_parameters']['dos']['values']['kmesh_factor'] == 3

    # 2D slab custom mesh with kz != 1 is rejected inside _edit_scan without breaking the menu.
    slab = Structure(Lattice.orthorhombic(3.84, 3.84, 20.0), ['Si', 'Si'], [[0, 0, 0.45], [0.5, 0.5, 0.55]])
    Poscar(slab).write_file(tmp_path / 'Slab.POSCAR')
    bad_custom = json.dumps({'encuts': [520, 600, 680], 'meshes': [[4, 4, 2], [6, 6, 2], [8, 8, 2]]})
    box_2d, shown_2d = _unified_box(tmp_path, ['4', '3', bad_custom, '2'], potcar_library=pot_lib)
    draft_2d = box_2d.propose_scan_plan(
        **_base_request(None, 'si-2d', structure='Slab.POSCAR', mesh=[4, 4, 1], relax_mode='ions'),
        encuts=None, meshes=None, origin='tool_default', rationale='',
    )
    assert any('扫描序列未修改' in line for line in shown_2d)
    assert draft_2d['status'] == 'selected' and draft_2d['selected']['meshes'][0][2] == 1

    # Operator-confirmed tool_default plan cannot be silently overwritten by later ai_suggestion.
    box_prot, _ = _unified_box(tmp_path, ['2'], potcar_library=pot_lib)
    box_prot.propose_scan_plan(**_base_request(None, 'si-prot'), encuts=None, meshes=None,
                               origin='tool_default', rationale='')
    with pytest.raises(core.ToolError) as err:
        box_prot.propose_scan_plan(**_base_request(None, 'si-prot'), encuts=None,
                                   meshes=[[4, 4, 4], [6, 6, 6], [8, 8, 8]],
                                   origin='ai_suggestion', rationale='试图覆盖已确认默认方案')
    assert err.value.error_code == 'SCAN_PLAN_CONFLICT'


# --- 6. Real environment_profiles (zero mock on profiles.*) + end-to-end prepare ---

def test_stage3_multi_env_real_profiles_flow_into_prepare(tmp_path, monkeypatch, vasp):
    workspace, remote_root, _ = _setup_remote_new_task(tmp_path, monkeypatch, vasp)
    env_b = remote_root / 'vasp643_env.sh'
    env_b.write_text('#!/bin/bash\n# vasp 6.4.3\n', encoding='utf-8')
    bin_a, bin_b = remote_root / 'vasp_544', remote_root / 'vasp_643'
    bin_a.write_bytes(b'fake-vasp-544')
    bin_b.write_bytes(b'fake-vasp-643')
    sha_b_env = hashlib.sha256(env_b.read_bytes()).hexdigest()
    sha_b_bin = hashlib.sha256(bin_b.read_bytes()).hexdigest()

    # Register real profile on disk via environment_profiles.save_profile (no monkeypatch on profiles.*).
    # The compute profile's vasp_env is automatically candidate #1 ('default'), vasp643 is candidate #2.
    profiles.save_profile(workspace, 'hpc', {
        'name': 'vasp643', 'version': '6.4.3', 'capabilities': ['vasp:6'],
        'env_script': str(env_b), 'environment_sha256': sha_b_env,
        'executable': str(bin_b), 'executable_sha256': sha_b_bin,
        'constraint_markers': [],
    })
    # Let inspect_environment return the exact hashes matching whichever env script worker receives.
    monkeypatch.setattr(worker, 'inspect_environment', lambda c, mode='cell': {
        'environment_script': c['vasp_env'],
        'environment_sha256': hashlib.sha256(Path(c['vasp_env']).read_bytes()).hexdigest(),
        'executable': str(bin_b if c['vasp_env'] == str(env_b) else bin_a),
        'executable_sha256': sha_b_bin if c['vasp_env'] == str(env_b) else hashlib.sha256(bin_a.read_bytes()).hexdigest(),
        'constraint_markers': [],
    })

    # Pressing '1' before choosing env is refused; '4 -> c -> 2' selects vasp643, then '2' confirms.
    box, shown = _unified_box(workspace, ['1', '4', 'c', '2', '2'])
    draft = box.propose_scan_plan(**_base_request('hpc', 'si-env'), encuts=None, meshes=None,
                                  origin='tool_default', rationale='')
    assert any('运行环境尚未选定' in line for line in shown)
    assert draft['status'] == 'selected' and draft['environment']['selected'] == 'vasp643'

    # Prepare must pass vasp643 through environment_payload into remote environment-review.json!
    prep = box.prepare_scan_plan(draft['id'])
    assert prep['physics_confirmed'] is True
    env_review = json.loads((remote_root / 'jobs' / 'si-env' / 'environment-review.json').read_text(encoding='utf-8'))
    assert env_review['environment_script'] == str(env_b) and env_review['environment_sha256'] == sha_b_env


# --- 7. Saved defaults priority, malformed disk fallback, reload, disconnects, non-interactive ---

def test_stage3_saved_defaults_priority_reload_disconnects_and_noninteractive(tmp_path, monkeypatch, vasp):
    workspace, remote_root, _ = _setup_remote_new_task(tmp_path, monkeypatch, vasp)

    # Saved user defaults (array 3x16) must NOT be clobbered by request parallel_tests=1, cores_per_test=8!
    resource_menu.save_defaults(workspace, 'hpc', 'convergence', {
        'parallel_tests': 3, 'cores_per_test': 16, 'nodes': 1,
        'partition': 'compute', 'walltime': '02:00:00', 'submission_mode': 'array',
    })
    box_def, _ = _unified_box(workspace, ['2'])
    draft_def = box_def.propose_scan_plan(
        **_base_request('hpc', 'si-saved-def', parallel_tests=1, cores_per_test=8),
        encuts=None, meshes=None, origin='tool_default', rationale='',
    )
    assert draft_def['startup_confirmation']['resources']['cores_per_test'] == 16
    assert draft_def['startup_confirmation']['resources']['submission_mode'] == 'array'

    # Malformed disk startup_confirmation.resources falls back gracefully without KeyError.
    plan_file = plans.path(box_def, draft_def['id'])
    corrupted = json.loads(plan_file.read_text(encoding='utf-8'))
    corrupted['startup_confirmation']['resources'] = {'cores_per_test': 'bad'}
    corrupted['status'] = 'pending'
    plan_file.write_text(json.dumps(corrupted), encoding='utf-8')
    box_def.plan_reader = lambda _: '2'
    plans.select_startup(box_def, draft_def['id'], lambda _: '2', lambda _: None, allow_reedit=True)
    repaired = json.loads(plan_file.read_text(encoding='utf-8'))
    assert repaired['startup_confirmation']['resources']['cores_per_test'] == 16

    # Cross-session reload after option 1 must NEVER grant allow_submit from disk alone.
    box1, _ = _unified_box(workspace, ['1'])
    draft = box1.propose_scan_plan(**_base_request('hpc', 'si-reload'), encuts=None, meshes=None,
                                   origin='tool_default', rationale='')
    box2, _ = _unified_box(workspace, None)
    prep = box2.prepare_scan_plan(draft['id'])
    assert prep['physics_confirmed'] is True and prep['resources_applied'] is True
    assert prep['submission_preauthorized'] is False and box2.allow_submit is False

    # Remote disconnect during confirm_physics preserves job_name, mentions resources not written, NEVER grants submit.
    orig_call = remote_compute.RemoteCompute.call

    def fail_physics(self, action, job_name=None, **params):
        if action == 'confirm_physics':
            raise core.ToolError('SSH_CONNECTION_FAILED', 'dropped during confirm_physics')
        return orig_call(self, action, job_name, **params)

    monkeypatch.setattr(remote_compute.RemoteCompute, 'call', fail_physics)
    box_drop, _ = _unified_box(workspace, ['1'])
    draft_drop = box_drop.propose_scan_plan(**_base_request('hpc', 'si-drop'), encuts=None, meshes=None,
                                            origin='tool_default', rationale='')
    prep_drop = box_drop.prepare_scan_plan(draft_drop['id'])
    assert (remote_root / 'jobs' / 'si-drop').is_dir() and prep_drop['job_name'] == 'si-drop'
    assert prep_drop['physics_confirmed'] is False and prep_drop['physics_uncertain'] is True
    assert prep_drop['submission_preauthorized'] is False and box_drop.allow_submit is False
    assert 'submit.slurm' in prep_drop['next'] and 'request_submission' in prep_drop['next']

    # Remote disconnect during configure_resources preserves job_name + physics_confirmed, records pending receipt, NEVER grants.
    def fail_configure(self, action, job_name=None, **params):
        if action == 'configure_resources':
            orig_call(self, action, job_name, **params)
            raise core.ToolError('SSH_CONNECTION_FAILED', 'dropped during configure_resources')
        return orig_call(self, action, job_name, **params)

    monkeypatch.setattr(remote_compute.RemoteCompute, 'call', fail_configure)
    box_rdrop, _ = _unified_box(workspace, ['1'])
    draft_rdrop = box_rdrop.propose_scan_plan(**_base_request('hpc', 'si-rdrop'), encuts=None, meshes=None,
                                              origin='tool_default', rationale='')
    prep_rdrop = box_rdrop.prepare_scan_plan(draft_rdrop['id'])
    assert prep_rdrop['physics_confirmed'] is True
    assert prep_rdrop['resources_applied'] is False and prep_rdrop['resources_uncertain'] is True
    assert prep_rdrop['submission_preauthorized'] is False and box_rdrop.allow_submit is False
    assert isinstance(box_rdrop.resource_confirmations[('hpc', 'si-rdrop', 'convergence')], dict)
    monkeypatch.setattr(remote_compute.RemoteCompute, 'call', orig_call)

    # Non-interactive mode NEVER auto-confirms physics or grants submission; legacy request_physics_confirmation still works.
    box_nonint, _ = _unified_box(workspace, None)
    draft_ni = box_nonint.propose_scan_plan(**_base_request('hpc', 'si-ni'), encuts=None, meshes=None,
                                            origin='tool_default', rationale='')
    assert draft_ni['status'] == 'selected' and 'startup_confirmation' not in draft_ni
    box_nonint.prepare_scan_plan(draft_ni['id'])
    assert not (remote_root / 'jobs' / 'si-ni' / 'physics-model.json').exists()
    assert box_nonint.allow_submit is False
    box_nonint.plan_reader = lambda _: '1'
    box_nonint.plan_output = lambda _: None
    phys = box_nonint.request_physics_confirmation('hpc', 'si-ni', '非磁性 Si 传统确认')
    assert phys['confirmed'] is True and phys['physics']['confirmed_by'] == 'operator_chat_menu'
    assert plans.startup_confirmed({'startup_confirmation': {'operator_confirmed': True}}) is False


# --- 8. Explicit /plan re-edit after revoke_submission + Layer 3 import purity ---

def test_stage3_slash_plan_reedit_and_layering_purity(tmp_path, monkeypatch, vasp):
    from test_chat_commands import setup as setup_commands

    Poscar(SI).write_file(tmp_path / 'Si.POSCAR')
    pot_lib = _setup_potentials(tmp_path)
    from vasp_agent.workflow import potential_library as potlib
    monkeypatch.setattr(
        potlib.Potcar, 'from_str',
        lambda text: [NS(element='Si', functional='PBE', enmax=245.0, ZVAL=4.0, TITEL='PAW_PBE Si')],
    )
    script = tmp_path / 'template.slurm'
    script.write_text(LOCAL_SLURM_SCRIPT, encoding='utf-8')
    commands, out = setup_commands(tmp_path)
    box = commands.agent.tools
    box.potcar_library = pot_lib
    box.submit_script = script
    box.script_hash = core.digest(script)
    box.require_scan_plans = True
    box.unified_plan_confirm = True
    box.plan_reader = None
    draft = box.propose_scan_plan(
        **_base_request(None, 'si-slash'),
        encuts=None, meshes=[[4, 4, 4], [6, 6, 6], [8, 8, 8]],
        origin='ai_suggestion', rationale='测试 /plan 命令打开与重编辑三合一窗口',
    )
    assert draft['status'] == 'pending'

    # First /plan: edit goal and pick 2 (prepare-only).
    answers1 = iter(['3', '初版 Si 目标', '2'])
    commands.reader = lambda _: next(answers1)
    assert commands.handle('/plan ' + draft['id']) == 'handled'
    assert '新任务启动核对（三合一）' in '\n'.join(out)

    # Repeated propose_scan_plan must NOT reopen menu on already startup-confirmed plan.
    box.plan_reader = lambda _: pytest.fail('already startup-confirmed must not re-prompt on propose')
    same = box.propose_scan_plan(
        **_base_request(None, 'si-slash'),
        encuts=None, meshes=[[4, 4, 4], [6, 6, 6], [8, 8, 8]],
        origin='ai_suggestion', rationale='测试 /plan 命令打开与重编辑三合一窗口',
    )
    assert same['status'] == 'selected' and same['startup_confirmation']['physics_goal'] == '初版 Si 目标'

    # Explicit /plan <id> DOES allow re-editing goal and upgrading to option 1 (pre-auth convergence)!
    answers2 = iter(['3', '重编辑后的 Si 研究目标', '1'])
    commands.reader = lambda _: next(answers2)
    assert commands.handle('/plan ' + draft['id']) == 'handled'
    assert draft['id'] in box.pending_plan_preauth

    prepared = box.prepare_scan_plan(draft['id'])
    assert prepared['physics_confirmed'] is True and prepared['submission_preauthorized'] is True
    phys = json.loads((box.job('si-slash') / 'physics-model.json').read_text(encoding='utf-8'))
    assert phys['goal'] == '重编辑后的 Si 研究目标' and phys['confirmed_by'] == 'operator_chat_menu'

    # Verify Layer 3 plan_startup.py has ZERO upward imports from Layer 4 vasp_agent.chat.
    startup_src = Path(plans.__file__).with_name('plan_startup.py').read_text(encoding='utf-8')
    tree = ast.parse(startup_src)
    chat_imports = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            chat_imports += [a.name for a in node.names if a.name == 'vasp_agent.chat' or a.name.startswith('vasp_agent.chat.')]
        elif isinstance(node, ast.ImportFrom) and node.module:
            if node.module == 'vasp_agent.chat' or node.module.startswith('vasp_agent.chat.'):
                chat_imports.append(node.module)
    assert chat_imports == []
