"""Offline end-to-end regressions for Stage 3: /plan 3-in-1 startup confirmation window."""
from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace as NS

import pytest
from pymatgen.io.vasp.inputs import Kpoints, Poscar

from vasp_agent.chat import ai_agent as ai
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
    # Ensure Potcar.from_str works with the lightweight synthetic POTCAR in _setup_potentials.
    from vasp_agent.workflow import potential_library as potlib
    monkeypatch.setattr(
        potlib.Potcar, 'from_str',
        lambda text: [NS(element='Si', functional='PBE', enmax=245.0, ZVAL=4.0, TITEL='PAW_PBE Si')],
    )
    return workspace, remote_root, sbatch_calls


def _base_request(server='hpc', job_name='si-new', structure='Si.POSCAR', **extra):
    return dict(server=server, job_name=job_name, structure=structure, encut=520, mesh=[4, 4, 4],
                nbands=16, relax_mode='cell', parallel_tests=None, cores_per_test=None, **extra)


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
    assert draft['selection']['submission_authorized'] is False  # not active until prepare_scan_plan
    assert box.allow_submit is False

    # Prepare writes directory, physics-model.json, submit.slurm (array), and activates 1-shot convergence grant.
    prepared = box.prepare_scan_plan(draft['id'])
    assert prepared['physics_confirmed'] is True
    assert prepared['resources_applied'] is True
    assert prepared['submission_preauthorized'] is True
    assert prepared['submission_target'] == {'server': 'hpc', 'job_name': 'si-new', 'phase': 'convergence'}
    assert box.allow_submit is True and box.submission_target == ('hpc', 'si-new', 'convergence')

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
    # Idempotent re-call to prepare_scan_plan must NOT re-grant submission.
    again = box.prepare_scan_plan(draft['id'])
    assert again['already_prepared'] is True and box.allow_submit is False


# --- 3. Blocked review hard-blocks submission even when operator chose 1 (remote + local) ---

def test_stage3_option1_blocked_review_hard_blocks_remote_and_local(tmp_path, monkeypatch, vasp):
    # Remote case
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

    lrev = lbox.get_stage_review(None, 'si-loc')
    lbad = passing_checks(lrev)
    lbad[0]['judgment'] = 'blocked'
    lbad[0]['blocking'] = True
    assert lbox.record_stage_review(None, 'si-loc', lrev['snapshot'], 'convergence', lbad)['allowed'] is False
    with pytest.raises(ValueError, match='未通过|审阅|blocked|放行'):
        lbox.submit_stage(None, 'si-loc', 'convergence')
    assert local_sbatch == []

    # Once review passes, local submit_stage succeeds with zero second menu and revokes grant.
    lrev2 = lbox.get_stage_review(None, 'si-loc')
    assert lbox.record_stage_review(None, 'si-loc', lrev2['snapshot'], 'convergence', passing_checks(lrev2))['allowed'] is True
    lbox.plan_reader = None
    lsub = lbox.submit_stage(None, 'si-loc', 'convergence')
    assert lsub['status'] == 'submitted' and lsub['job_id'] == '777' and len(local_sbatch) == 1
    assert lbox.allow_submit is False


# --- 4. Option 2 (prepare-only): writes physics + resources, NEVER grants submission ---

def test_stage3_option2_writes_physics_and_resources_without_submission_grant(tmp_path, monkeypatch, vasp):
    workspace, remote_root, sbatch_calls = _setup_remote_new_task(tmp_path, monkeypatch, vasp)
    box, _ = _unified_box(workspace, ['5', '每组=16 节点=2', '2'])
    # Seed a stale unbound grant to verify option 2 clears it.
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
    assert sbatch_calls == []


# --- 5. Block B edits (default switch, DOS stage params, multi-env), cancel, reload, disconnect, non-interactive ---

def test_stage3_block_b_edits_cancel_and_conflict_protection(tmp_path, monkeypatch, vasp):
    Poscar(SI).write_file(tmp_path / 'Si.POSCAR')
    pot_lib = _setup_potentials(tmp_path)
    from vasp_agent.workflow import potential_library as potlib
    monkeypatch.setattr(
        potlib.Potcar, 'from_str',
        lambda text: [NS(element='Si', functional='PBE', enmax=245.0, ZVAL=4.0, TITEL='PAW_PBE Si')],
    )

    # Cancel ('q') even on tool_default keeps plan pending and blocks prepare.
    box_cancel, _ = _unified_box(tmp_path, ['q'], potcar_library=pot_lib)
    cancelled = box_cancel.propose_scan_plan(**_base_request(None, 'si-cancel'), encuts=None, meshes=None,
                                             origin='tool_default', rationale='')
    assert cancelled['status'] == 'pending' and not box_cancel.job('si-cancel').exists()
    with pytest.raises(ValueError, match='尚未选择'):
        box_cancel.prepare_scan_plan(cancelled['id'])

    # Edit Block B: switch AI suggestion back to tool default (4 -> a -> 1), edit DOS param (4 -> b -> 3 -> kmesh_factor=3), confirm (2).
    box_edit, shown = _unified_box(tmp_path, [
        '4', 'a', '1',
        '4', 'b', '3', 'kmesh_factor=3',
        '2',
    ], potcar_library=pot_lib)
    draft = box_edit.propose_scan_plan(
        **_base_request(None, 'si-dos-edit', optional_stages=['dos']),
        encuts=None, meshes=[[4, 4, 4], [6, 6, 6], [8, 8, 8]],
        origin='ai_suggestion', rationale='测试切换默认与修改 DOS 参数',
    )
    assert draft['status'] == 'selected' and draft['choice'] == 'operator_default'
    assert draft['stage_parameters']['dos']['selected']['kmesh_factor'] == 3
    prep = box_edit.prepare_scan_plan(draft['id'])
    assert prep['physics_confirmed'] is True and prep['submission_preauthorized'] is False
    # Verify actual KPOINTS used tool default (4, 5, 6), not AI suggestion (4, 6, 8).
    assert list(Kpoints.from_file(box_edit.job('si-dos-edit') / 'convergence/mesh-1/KPOINTS').kpts[0]) == [5, 5, 5]
    cfg = json.loads((box_edit.job('si-dos-edit') / 'config.json').read_text(encoding='utf-8'))
    assert cfg['stage_parameters']['dos']['values']['kmesh_factor'] == 3

    # Operator-confirmed tool_default plan cannot be silently upgraded/overwritten by later ai_suggestion.
    box_prot, _ = _unified_box(tmp_path, ['2'], potcar_library=pot_lib)
    box_prot.propose_scan_plan(**_base_request(None, 'si-prot'), encuts=None, meshes=None,
                               origin='tool_default', rationale='')
    with pytest.raises(core.ToolError) as err:
        box_prot.propose_scan_plan(**_base_request(None, 'si-prot'), encuts=None,
                                   meshes=[[4, 4, 4], [6, 6, 6], [8, 8, 8]],
                                   origin='ai_suggestion', rationale='试图覆盖已确认默认方案')
    assert err.value.error_code == 'SCAN_PLAN_CONFLICT'


def test_stage3_multi_env_requires_selection_before_confirm(tmp_path, monkeypatch, vasp):
    workspace, _, _ = _setup_remote_new_task(tmp_path, monkeypatch, vasp)
    env_a, env_b = tmp_path / 'env_a.sh', tmp_path / 'env_b.sh'
    env_a.write_text('#!/bin/bash\n', encoding='utf-8')
    env_b.write_text('#!/bin/bash\n', encoding='utf-8')
    candidates = [
        {'name': 'vasp544', 'implicit': True, 'version': '5.4.4', 'capabilities': ['vasp:5'], 'env_script': str(env_a),
         'environment_sha256': 'a' * 64, 'executable_sha256': 'b' * 64},
        {'name': 'vasp643', 'implicit': False, 'version': '6.4.3', 'capabilities': ['vasp:6'], 'env_script': str(env_b),
         'environment_sha256': 'c' * 64, 'executable_sha256': 'd' * 64},
    ]
    monkeypatch.setattr(profiles, 'registered', lambda ws, srv: True)
    monkeypatch.setattr(profiles, 'choose', lambda ws, srv, mode, req=(): {
        'status': 'several', 'required': [], 'matches': candidates, 'excluded': [],
    })
    monkeypatch.setattr(profiles, 'brief', lambda row: {k: row[k] for k in ('name', 'implicit', 'version', 'capabilities', 'env_script')})
    monkeypatch.setattr(profiles, 'payload', lambda row: {
        'name': row['name'], 'env_script': row['env_script'], 'capabilities': row['capabilities'],
        'environment_sha256': row['environment_sha256'], 'executable_sha256': row['executable_sha256'],
    })

    # Pressing '1' before picking an environment must be refused; picking '4 -> c -> 2' then '2' succeeds.
    box, shown = _unified_box(workspace, ['1', '4', 'c', '2', '2'])
    draft = box.propose_scan_plan(**_base_request('hpc', 'si-env'), encuts=None, meshes=None,
                                  origin='tool_default', rationale='')
    assert any('运行环境尚未选定' in line for line in shown)
    assert draft['status'] == 'selected' and draft['environment']['selected'] == 'vasp643'


def test_stage3_reload_never_mints_grant_and_disconnect_and_noninteractive_stay_safe(tmp_path, monkeypatch, vasp):
    workspace, remote_root, _ = _setup_remote_new_task(tmp_path, monkeypatch, vasp)

    # 1. Cross-session reload after option 1 must NEVER grant allow_submit from disk alone.
    box1, _ = _unified_box(workspace, ['1'])
    draft = box1.propose_scan_plan(**_base_request('hpc', 'si-reload'), encuts=None, meshes=None,
                                   origin='tool_default', rationale='')
    box2, _ = _unified_box(workspace, None)  # fresh session without in-memory pending_plan_preauth
    prep = box2.prepare_scan_plan(draft['id'])
    assert prep['physics_confirmed'] is True and prep['resources_applied'] is True
    assert prep['submission_preauthorized'] is False and box2.allow_submit is False

    # 2. Remote disconnect during confirm_physics preserves job_name and NEVER grants submission.
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
    assert (remote_root / 'jobs' / 'si-drop').is_dir()
    assert prep_drop['job_name'] == 'si-drop'
    assert prep_drop['physics_confirmed'] is False and prep_drop['physics_uncertain'] is True
    assert prep_drop['submission_preauthorized'] is False and box_drop.allow_submit is False
    monkeypatch.setattr(remote_compute.RemoteCompute, 'call', orig_call)

    # 3. Non-interactive mode (plan_reader=None) NEVER auto-confirms physics or grants submission.
    box_nonint, _ = _unified_box(workspace, None)
    draft_ni = box_nonint.propose_scan_plan(**_base_request('hpc', 'si-ni'), encuts=None, meshes=None,
                                            origin='tool_default', rationale='')
    assert draft_ni['status'] == 'selected' and 'startup_confirmation' not in draft_ni
    box_nonint.prepare_scan_plan(draft_ni['id'])
    assert not (remote_root / 'jobs' / 'si-ni' / 'physics-model.json').exists()
    assert box_nonint.allow_submit is False
    # Legacy request_physics_confirmation still works on the non-interactive prepared task.
    box_nonint.plan_reader = lambda _: '1'
    box_nonint.plan_output = lambda _: None
    phys = box_nonint.request_physics_confirmation('hpc', 'si-ni', '非磁性 Si 传统确认')
    assert phys['confirmed'] is True and phys['physics']['confirmed_by'] == 'operator_chat_menu'
    assert plans.startup_confirmed({'startup_confirmation': {'operator_confirmed': True}}) is False


def test_stage3_slash_plan_command_opens_3in1_startup_window(tmp_path, monkeypatch, vasp):
    from test_chat_commands import setup as setup_commands

    Poscar(SI).write_file(tmp_path / 'Si.POSCAR')
    pot_lib = _setup_potentials(tmp_path)
    from vasp_agent.workflow import potential_library as potlib
    monkeypatch.setattr(
        potlib.Potcar, 'from_str',
        lambda text: [NS(element='Si', functional='PBE', enmax=245.0, ZVAL=4.0, TITEL='PAW_PBE Si')],
    )
    commands, out = setup_commands(tmp_path)
    box = commands.agent.tools
    box.potcar_library = pot_lib
    box.require_scan_plans = True
    box.unified_plan_confirm = True
    box.plan_reader = None  # first create a pending draft non-interactively
    draft = box.propose_scan_plan(
        **_base_request(None, 'si-slash'),
        encuts=None, meshes=[[4, 4, 4], [6, 6, 6], [8, 8, 8]],
        origin='ai_suggestion', rationale='测试 /plan 命令打开三合一窗口',
    )
    assert draft['status'] == 'pending'

    answers = iter(['3', '通过 /plan 命令确认的 Si 研究目标', '2'])
    commands.reader = lambda _: next(answers)
    assert commands.handle('/plan ' + draft['id']) == 'handled'
    text = '\n'.join(out)
    assert '新任务启动核对（三合一）' in text and '【区块 A · 物理模型与研究目标】' in text

    prepared = box.prepare_scan_plan(draft['id'])
    assert prepared['physics_confirmed'] is True and prepared['submission_preauthorized'] is False
    phys = json.loads((box.job('si-slash') / 'physics-model.json').read_text(encoding='utf-8'))
    assert phys['goal'] == '通过 /plan 命令确认的 Si 研究目标'
    assert phys['confirmed_by'] == 'operator_chat_menu'

