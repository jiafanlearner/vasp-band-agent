import json
from types import SimpleNamespace as NS

import pytest
from pymatgen.core import Structure, Lattice
from pymatgen.io.vasp.inputs import Poscar, Kpoints, Incar

from vasp_agent.chat.ai_agent import Toolbox
import vasp_agent.planning.parameter_plans as plans
from vasp_agent.workflow.scan_config import validate_scans, verify_scan_inputs
from vasp_agent.common import agent_paths


@pytest.fixture
def box(tmp_path):
    Poscar(Structure(Lattice.cubic(5.43), ['Si'], [[0,0,0]])).write_file(tmp_path / 'POSCAR')
    return Toolbox(tmp_path)


def request():
    return dict(server=None, job_name='scan-task', structure='POSCAR', encut=520, mesh=[12,12,12],
                nbands=16, relax_mode='cell', parallel_tests=1, cores_per_test=8)


SUGGESTED = [[12,12,12],[14,14,14],[16,16,16]]


def test_pending_cancel_no_task_and_no_silent_default_replacement(box):
    box.plan_reader = lambda _: 'q'
    box.plan_output = lambda _: None
    draft = plans.propose(box, request(), None, SUGGESTED, 'ai_suggestion', 'Reduce grid size, convergence still needs testing')
    assert draft['status'] == 'pending'
    assert not draft['selection']['operator_confirmed']
    assert draft['selection']['requires_parameter_choice']
    assert draft['defaults']['meshes'] == [[12,12,12],[15,15,15],[18,18,18]]
    assert not box.job('scan-task').exists()
    with pytest.raises(ValueError, match='尚未选择'):
        plans.prepare(box, draft['id'])
    before = plans.path(box, draft['id']).read_bytes()
    with pytest.raises(ValueError, match='同名草案') as error:
        plans.propose(box, request(), None, None, 'tool_default', '')
    assert error.value.error_code == 'SCAN_PLAN_CONFLICT'
    assert plans.path(box, draft['id']).read_bytes() == before


def test_menu_choice_is_reported_as_user_confirmation_and_survives_reload(box):
    box.plan_reader = lambda _: '2'
    box.plan_output = lambda _: None
    draft = plans.propose(box, request(), None, SUGGESTED, 'ai_suggestion', 'test reason')
    assert draft['status'] == 'selected'
    assert draft['choice'] == 'operator_accept_ai'
    assert draft['selection']['operator_confirmed']
    assert draft['selection']['source'] == 'operator_menu'
    assert draft['selection']['menu_option'] == '2'
    assert not draft['selection']['requires_parameter_choice']
    assert not draft['selection']['submission_authorized']
    fresh = Toolbox(box.workspace)
    saved = plans.list_plans(fresh)['plans'][0]
    assert saved['selection'] == draft['selection']
    # Reusing the selected plan must not prompt again.
    fresh.plan_reader = lambda _: pytest.fail('already confirmed')
    assert plans.propose(fresh, request(), None, SUGGESTED, 'ai_suggestion', 'reason')['selection'] == draft['selection']


def test_default_agreement_is_not_misreported_as_operator_menu(box):
    draft = plans.propose(box, request(), None, None, 'tool_default', '')
    assert not draft['selection']['operator_confirmed']
    assert not draft['selection']['requires_parameter_choice']


@pytest.mark.parametrize('answer,meshes', [('1', [[12,12,12],[15,15,15],[18,18,18]]), ('2', SUGGESTED)])
def test_selection_generates_exact_files_and_remembers_choice(box, answer, meshes):
    draft = plans.propose(box, request(), None, SUGGESTED, 'ai_suggestion', 'test reason')
    plans.select(box, draft['id'], lambda _: answer, lambda _: None)
    plans.prepare(box, draft['id'])
    folder = box.job('scan-task')
    assert verify_scan_inputs(folder)['matched']
    for i, expected in enumerate(meshes):
        assert list(Kpoints.from_file(folder / f'convergence/mesh-{i}/KPOINTS').kpts[0]) == expected
    assert plans.prepare(box, draft['id'])['already_prepared']
    choice = json.loads((folder / 'parameter-choice.json').read_text())
    assert choice['choice'] == ('operator_default' if answer == '1' else 'operator_accept_ai')


def test_custom_and_explicit_sequences(box):
    draft = plans.propose(box, request(), None, SUGGESTED, 'ai_suggestion', 'reason')
    custom = {'encuts':[530,610,700], 'meshes':[[10,12,14],[12,14,16],[14,16,18]]}
    answers = iter(['3',json.dumps(custom)])
    plans.select(box, draft['id'], lambda _: next(answers), lambda _: None)
    plans.prepare(box, draft['id'])
    assert Incar.from_file(box.job('scan-task') / 'convergence/encut-0/INCAR')['ENCUT'] == 530
    # `user_explicit` is only accepted when the operator's own message carries the
    # numbers being claimed, so the record reflects something the user typed.
    box.latest_user_message = '请用 530/610/700 eV，网格 12 14 16 做收敛测试'
    explicit = plans.propose(box, {**request(), 'job_name':'explicit'}, [530,610,700], SUGGESTED, 'user_explicit', 'user instruction')
    assert explicit['status'] == 'selected'
    plans.prepare(box, explicit['id'])


def test_user_explicit_requires_the_operator_message_to_state_the_sequence(box):
    draft = plans.propose(box, request(), None, SUGGESTED, 'ai_suggestion', 'test reason')
    plans.select(box, draft['id'], lambda _: '2', lambda _: None)
    before = plans.path(box, draft['id']).read_bytes()
    box.plan_reader = lambda _: pytest.fail('rejected claim must not open a menu')
    # The model asserts the user chose the sequence; the recorded message never
    # mentions it, so the label is unsupported and the plan must not be written.
    box.latest_user_message = '帮我做一套收敛测试，参数你定'
    with pytest.raises(ValueError) as error:
        plans.propose(box, {**request(), 'job_name': 'claimed'}, [530,610,700], SUGGESTED, 'user_explicit', 'reason')
    assert error.value.error_code == 'ORIGIN_NOT_OPERATOR_STATED'
    assert plans.path(box, draft['id']).read_bytes() == before
    assert not box.job('claimed').exists()
    assert box.call('propose_scan_plan', {**request(), 'job_name': 'claimed', 'encuts': [530,610,700],
        'meshes': SUGGESTED, 'origin': 'user_explicit', 'rationale': 'reason'})['error_code'] == 'ORIGIN_NOT_OPERATOR_STATED'


def test_stated_numbers_are_matched_with_a_digit_boundary(box):
    # 520 must not be found inside 5200, or an unrelated resource number would
    # silently satisfy the check for a sequence the user never gave.
    box.latest_user_message = '用 5200 个核时跑一下'
    assert not plans.stated_by_user(box, [520], None)
    box.latest_user_message = 'ENCUT 用 520 eV'
    assert plans.stated_by_user(box, [520], None)
    assert not plans.stated_by_user(box, [520.5], None)


def test_changed_structure_invalidates_selection(box):
    draft = plans.propose(box, request(), None, None, 'tool_default', '')
    Poscar(Structure(Lattice.cubic(6), ['Si'], [[0,0,0]])).write_file(box.workspace / 'POSCAR')
    with pytest.raises(ValueError, match='变化'):
        plans.prepare(box, draft['id'])
    assert not box.job('scan-task').exists()


def test_actual_file_mismatch_is_detected_even_if_manifest_is_bypassed(box):
    draft = plans.propose(box, request(), None, None, 'tool_default', '')
    plans.prepare(box, draft['id'])
    folder = box.job('scan-task')
    Kpoints.gamma_automatic([11,11,11]).write_file(folder / 'convergence/mesh-0/KPOINTS')
    with pytest.raises(ValueError, match='不一致'):
        verify_scan_inputs(folder)


@pytest.mark.parametrize('encuts,meshes', [([520,500,676],SUGGESTED), ([520,598,float('nan')],SUGGESTED),
    ([520,598,676],[[12,12,12]]*3), ([520,598,676],[[12,12,12],[11,14,14],[16,16,16]])])
def test_invalid_sequences_rejected(encuts, meshes):
    with pytest.raises(ValueError):
        validate_scans(encuts, meshes)


def test_runtime_hides_old_preparation_tools(box):
    box.require_scan_plans = True
    names = [t['name'] for t in box.schemas()]
    assert 'remote_prepare' not in names and 'prepare_calculation' not in names
    assert 'propose_scan_plan' in names and 'prepare_scan_plan' in names


def test_preview_does_not_save_or_select_a_draft(box):
    box.plan_reader = lambda _: pytest.fail('preview must not prompt')
    result = box.call('preview_scan_defaults', request())
    assert result['ok']
    assert result['result']['defaults']['meshes'] == [[12,12,12],[15,15,15],[18,18,18]]
    assert result['result']['plan_saved'] is False
    assert not box.job('scan-task').exists()
    assert not list(agent_paths.store_dir(box.workspace, 'scan_plans').glob('*.json'))


@pytest.mark.parametrize('state', ['pending', 'operator_default', 'operator_accept_ai', 'user_explicit', 'prepared'])
def test_different_proposal_never_overwrites_protected_state(box, state):
    origin = 'user_explicit' if state == 'user_explicit' else 'ai_suggestion'
    if origin == 'user_explicit':
        box.latest_user_message = '网格用 12 14 16'
    draft = plans.propose(box, request(), None, SUGGESTED, origin, 'test reason')
    if state in ('operator_default', 'operator_accept_ai', 'prepared'):
        plans.select(box, draft['id'], lambda _: '1' if state == 'operator_default' else '2', lambda _: None)
    if state == 'prepared':
        plans.prepare(box, draft['id'])
    target = plans.path(box, draft['id'])
    before = target.read_bytes()
    box.plan_reader = lambda _: pytest.fail('conflict must not prompt or replace')
    result = box.call('propose_scan_plan', {**request(), 'encuts': None,
        'meshes': [[14,14,14],[16,16,16],[18,18,18]], 'origin': 'ai_suggestion', 'rationale': 'different'})
    assert not result['ok'] and result['error_code'] == 'SCAN_PLAN_CONFLICT'
    assert target.read_bytes() == before


@pytest.mark.parametrize('change', [{'nbands': 20}, {'mesh': [8,8,8]}, {'cores_per_test': 16}])
def test_default_draft_does_not_hide_changed_request(box, change):
    draft = plans.propose(box, request(), None, None, 'tool_default', '')
    before = plans.path(box, draft['id']).read_bytes()
    with pytest.raises(ValueError) as error:
        plans.propose(box, {**request(), **change}, None, SUGGESTED, 'ai_suggestion', 'reason')
    assert error.value.error_code == 'SCAN_PLAN_CONFLICT'
    assert plans.path(box, draft['id']).read_bytes() == before


def test_legacy_default_upgrade_cancel_blocks_prepare_and_keeps_history(box):
    draft = plans.propose(box, request(), None, None, 'tool_default', '')
    box.plan_reader = lambda _: 'q'
    box.plan_output = lambda _: None
    updated = plans.propose(box, request(), None, SUGGESTED, 'ai_suggestion', 'reason')
    assert updated['id'] == draft['id'] and updated['status'] == 'pending'
    assert updated['selected'] is None and updated['choice'] is None
    assert updated['revision_reason']
    saved = json.loads(plans.path(box, draft['id']).read_text(encoding='utf-8'))
    assert saved['previous_proposal']['choice'] == 'no_difference'
    with pytest.raises(ValueError, match='尚未选择'):
        plans.prepare(box, draft['id'])
    assert not box.job('scan-task').exists()


def test_prepared_default_is_not_upgraded(box):
    draft = plans.propose(box, request(), None, None, 'tool_default', '')
    plans.prepare(box, draft['id'])
    before = plans.path(box, draft['id']).read_bytes()
    with pytest.raises(ValueError) as error:
        plans.propose(box, request(), None, SUGGESTED, 'ai_suggestion', 'reason')
    assert error.value.error_code == 'SCAN_PLAN_CONFLICT'
    assert plans.path(box, draft['id']).read_bytes() == before


def test_preview_does_not_modify_existing_selection(box):
    draft = plans.propose(box, request(), None, SUGGESTED, 'ai_suggestion', 'reason')
    plans.select(box, draft['id'], lambda _: '2', lambda _: None)
    before = plans.path(box, draft['id']).read_bytes()
    box.plan_reader = lambda _: pytest.fail('read-only preview must not prompt')
    assert box.call('preview_scan_defaults', request())['ok']
    assert plans.path(box, draft['id']).read_bytes() == before


@pytest.mark.parametrize('final,choice', [('2', 'operator_accept_ai'), ('4', None), ('q', None)])
def test_blank_menu_input_waits_for_explicit_choice(box, final, choice):
    draft = plans.propose(box, request(), None, SUGGESTED, 'ai_suggestion', 'reason')
    answers = iter(['', '\r\n', '  ', final])
    messages = []
    def reader(prompt):
        saved = json.loads(plans.path(box, draft['id']).read_text(encoding='utf-8'))
        assert saved['status'] == 'pending'
        assert not box.job('scan-task').exists()
        return next(answers)
    plans.select(box, draft['id'], reader, messages.append)
    result = plans.list_plans(box)['plans'][0]
    assert result['choice'] == choice
    assert result['status'] == ('selected' if choice else 'pending')
    assert sum('未收到选项' in text for text in messages) == 3


def test_menu_eof_after_blank_preserves_pending_plan(box):
    draft = plans.propose(box, request(), None, SUGGESTED, 'ai_suggestion', 'reason')
    answers = iter(['', EOFError()])
    def reader(prompt):
        answer = next(answers)
        if isinstance(answer, Exception):
            raise answer
        return answer
    with pytest.raises(EOFError):
        plans.select(box, draft['id'], reader, lambda _: None)
    assert plans.list_plans(box)['plans'][0]['status'] == 'pending'


def test_menu_shows_side_by_side_table_and_folds_long_rationale(box):
    reason = '改了网格；原因是倒空间间距偏大；点数增加约三成；Si 非磁性假设待确认；优化后需复核'
    draft = plans.propose(box, request(), None, SUGGESTED, 'ai_suggestion', reason)
    answers, messages = iter(['d', '2']), []
    plans.select(box, draft['id'], lambda _: next(answers), messages.append)
    text = '\n'.join(messages)
    assert 'ENCUT (eV)' in text and 'k 网格' in text and '（相同）' in text and '（不同）' in text
    assert '差异：k 网格 不同' in text and '{"encuts"' not in text
    assert '另有 2 条' in text and '完整理由' in text and '优化后需复核' in text
    assert plans.list_plans(box)['plans'][0]['choice'] == 'operator_accept_ai'


def test_ai_threshold_suggestion_applies_only_when_operator_accepts(box):
    draft = plans.propose(box, request(), None, SUGGESTED, 'ai_suggestion', '放宽应力差阈值；理由见菜单',
                          {'stress_kbar': 2.0})
    shown = []
    plans.select(box, draft['id'], lambda _: '2', shown.append)
    text = '\n'.join(shown)
    assert '应力差阈值' in text and '（放宽）' in text and '0.5 → 2 kbar' in text
    saved = plans.list_plans(box)['plans'][0]
    assert saved['selected']['scan_thresholds'] == {'stress_kbar': 2.0}
    with pytest.raises(ValueError, match='默认模式'):
        plans.propose(box, {**request(), 'job_name': 'other'}, None, None, 'tool_default', '', {'stress_kbar': 2.0})


def test_accepted_thresholds_reach_the_prepared_task(box):
    draft = plans.propose(box, request(), None, SUGGESTED, 'ai_suggestion', '放宽应力差阈值', {'stress_kbar': 2.0})
    plans.select(box, draft['id'], lambda _: '2', lambda _: None)
    plans.prepare(box, draft['id'])
    config = json.loads((box.job('scan-task') / 'config.json').read_text(encoding='utf-8'))
    assert config['scan_thresholds'] == {'stress_kbar': 2.0}


def test_menu_states_whether_the_cell_is_optimized(box):
    draft = plans.propose(box, request(), None, SUGGESTED, 'ai_suggestion', 'reason')
    shown = []
    plans.select(box, draft['id'], lambda _: '4', shown.append)
    mode = request()['relax_mode']
    assert any(('ISIF=2' if mode == 'ions' else 'ISIF=3') in line for line in shown)


def test_new_plans_default_to_standard_preset_and_s_switches(box):
    # Through the model's tool entry, where an omitted preset defaults to standard.
    draft = box.propose_scan_plan(**request(), encuts=None, meshes=SUGGESTED, origin='ai_suggestion', rationale='reason')
    assert draft['request']['scan_preset'] == 'standard'
    shown, answers = [], iter(['s', '2'])
    plans.select(box, draft['id'], lambda _: next(answers), shown.append)
    text = '\n'.join(shown)
    assert '判据档位：标准' in text and '已切换：判据档位：严格' in text
    saved = plans.list_plans(box)['plans'][0]
    assert saved['request']['scan_preset'] == 'strict'
    plans.prepare(box, draft['id'])
    config = json.loads((box.job('scan-task') / 'config.json').read_text(encoding='utf-8'))
    assert config['scan_preset'] == 'strict'


def test_standard_preset_thresholds_and_sources():
    from vasp_agent.workflow import full_workflow as full
    from test_full_workflow import config as base_config, result
    cfg = {**base_config(), 'scan_preset': 'standard'}
    check = full.compare(full.metrics(result(force=0.)), full.metrics(result(force=0.008)), cfg)
    assert check['passed'] and check['comparison_policy'] == 'scan_preset_standard_v1'
    assert check['effective_thresholds'] == {'energy_eV_per_atom': 0.001, 'force_eV_per_A': 0.01, 'stress_kbar': 2.0}
    assert check['threshold_sources']['force_eV_per_A'] == 'preset:standard'
    assert check['threshold_sources']['energy_eV_per_atom'] == 'default'
    strict = full.compare(full.metrics(result(force=0.)), full.metrics(result(force=0.008)), base_config())
    assert not strict['passed'] and 'threshold_sources' not in strict
    override = full.compare(full.metrics(result(force=0.)), full.metrics(result(force=0.008)),
                            {**cfg, 'scan_thresholds': {'force_eV_per_A': 0.006}})
    assert not override['passed'] and override['threshold_sources']['force_eV_per_A'] == 'operator'
