"""Stage parameter specs: strict validation, the operator menu, config hash and review snapshot.

The DOS spec v1 (never executable, superseded by v2 in step C) keeps its pure
functions tested here; the full flow (spec -> suggestion -> operator choice ->
validation -> saved -> generated inputs) also runs on a synthetic test-only
stage. The executable DOS stage itself is tested in test_dos_stage.py.
"""
import json
from types import SimpleNamespace as NS

import pytest
from pymatgen.core import Lattice, Structure
from pymatgen.io.vasp.inputs import Poscar

import vasp_agent.planning.parameter_plans as plans
import vasp_agent.workflow.full_workflow as full
import vasp_agent.workflow.stage_gate as gate
import vasp_agent.workflow.vasp_band_agent as core
from vasp_agent.chat.ai_agent import TOOLS, Toolbox
from vasp_agent.workflow import parameter_specs as specs
from vasp_agent.workflow import stage_definitions as stages
from vasp_agent.common import agent_paths

DOS = specs.spec('dos', 1)
CONTEXT = {'dimension': '3D'}


def dos_values(**changes):
    return {**{name: entry['value'] for name, entry in specs.suggestions(DOS, CONTEXT).items()}, **changes}


# --- The DOS spec: strict validation -------------------------------------------

def test_dos_suggestions_validate_and_state_their_basis():
    values = specs.validate(DOS, dos_values(), CONTEXT)
    assert values['integration'] == 'tetrahedron' and values['sigma_eV'] is None
    assert values['energy_reference'] == 'scf_fermi' and values['kmesh_factor'] == 2
    described = specs.describe(DOS, CONTEXT)
    assert described['executable'] is False and '步骤 C' in described['not_executable_reason']
    assert described['fixed']['LORBIT'] == 11
    basis = {name: entry['basis'] for name, entry in described['suggestions'].items()}
    assert '不证明 DOS 已收敛' in basis['kmesh_factor']
    assert '不静默改为 Gaussian' in basis['integration']
    assert '不是分辨率' in basis['sampling_interval_eV']
    assert 'DOS 计算自身的 E_F' in basis['energy_reference']
    assert specs.nedos(values) == 1501


@pytest.mark.parametrize('changes,message', [
    ({'smearing': 0.1}, '未知参数：smearing'),
    ({'kmesh_factor': 9}, 'kmesh_factor 须在 \\[1, 4\\]'),
    ({'kmesh_factor': 2.5}, 'kmesh_factor 必须是整数'),
    ({'kmesh_factor': True}, 'kmesh_factor 必须是数值'),
    ({'window_max_eV': '/etc/passwd'}, 'window_max_eV 必须是数值'),
    ({'window_min_eV': float('nan')}, 'window_min_eV 须在'),
    ({'integration': 'mp'}, 'integration 只能取 tetrahedron、gaussian'),
    ({'energy_reference': 'vbm'}, 'energy_reference 只能取 scf_fermi'),
    ({'integration': 'gaussian'}, 'gaussian 必须由操作者给出 sigma_eV'),
    ({'sigma_eV': 0.05}, 'tetrahedron 不使用 sigma_eV'),
    ({'window_min_eV': -50.0, 'window_max_eV': 30.0, 'sampling_interval_eV': 0.001}, 'NEDOS=80001 超过上限'),
])
def test_dos_values_are_validated_strictly(changes, message):
    with pytest.raises(ValueError, match=message):
        specs.validate(DOS, dos_values(**changes), CONTEXT)


def test_missing_fields_and_two_dimensional_tasks_are_refused():
    values = dos_values()
    del values['sampling_interval_eV']
    with pytest.raises(ValueError, match='缺少参数：sampling_interval_eV'):
        specs.validate(DOS, values, CONTEXT)
    with pytest.raises(ValueError, match='二维 DOS 尚未开放'):
        specs.validate(DOS, dos_values(), {'dimension': '2D'})
    assert specs.validate(DOS, dos_values(integration='gaussian', sigma_eV=0.05), CONTEXT)['sigma_eV'] == 0.05


def evidence(**changes):
    return {'dimension': '3D', 'scf_mesh': [8, 8, 8], 'scf_kpoints_style': 'Gamma', 'scf_fermi_eV': 5.25,
            'scf_nbands': 24, 'highest_band_minimum_eV': 9.0, **changes}


def test_dos_inputs_follow_the_rules_for_step_c():
    generated = DOS['generate'](specs.validate(DOS, dos_values(), CONTEXT), evidence())
    assert generated['blocked'] == []
    assert generated['kpoints'] == {'style': 'Gamma', 'mesh': [16, 16, 16]}
    incar = generated['incar']
    assert incar['ISMEAR'] == -5 and 'SIGMA' not in incar and incar['LORBIT'] == 11 and incar['ICHARG'] == 11
    # EMIN/EMAX are absolute in VASP 5.4.4: converted from the SCF E_F and recorded.
    assert (incar['EMIN'], incar['EMAX'], incar['NEDOS']) == (-4.75, 10.25, 1501)
    assert generated['derived']['energy_conversion']['scf_fermi_eV'] == 5.25
    assert generated['derived']['sampling_note'] == '采样间隔，不是分辨率。'


def test_tetrahedron_preconditions_block_instead_of_switching_to_gaussian():
    values = specs.validate(DOS, dos_values(kmesh_factor=1), CONTEXT)
    small = DOS['generate'](values, evidence(scf_mesh=[2, 2, 2]))
    assert small['incar']['ISMEAR'] == -5
    assert any('不可约 k 点下界 1 < 4' in reason and 'gaussian' in reason for reason in small['blocked'])
    explicit = DOS['generate'](values, evidence(scf_kpoints_style='Reciprocal'))
    assert any('Γ/MP 规则网格' in reason for reason in explicit['blocked'])
    # Only the operator's explicit choice gives Gaussian, with the SIGMA they confirmed.
    chosen = specs.validate(DOS, dos_values(kmesh_factor=1, integration='gaussian', sigma_eV=0.05), CONTEXT)
    gaussian = DOS['generate'](chosen, evidence(scf_mesh=[2, 2, 2]))
    assert gaussian['blocked'] == [] and (gaussian['incar']['ISMEAR'], gaussian['incar']['SIGMA']) == (0, 0.05)


def test_empty_bands_must_cover_the_window():
    values = specs.validate(DOS, dos_values(), CONTEXT)
    assert specs.nbands_check(values, 6.0)['covered']  # 5 eV window + 1 eV margin
    short = DOS['generate'](values, evidence(highest_band_minimum_eV=5.5))
    assert any('空带不足' in reason and '不能照抄 bands' in reason for reason in short['blocked'])
    assert short['derived']['nbands']['required_eV'] == 6.0


# --- DOS is offered and executable ----------------------------------------------------

@pytest.fixture
def box(tmp_path):
    Poscar(Structure(Lattice.cubic(5.43), ['Si'], [[0, 0, 0]])).write_file(tmp_path / 'POSCAR')
    return Toolbox(tmp_path)


def request(**extra):
    return dict(server=None, job_name='staged', structure='POSCAR', encut=520, mesh=[12, 12, 12],
                nbands=16, relax_mode='cell', parallel_tests=1, cores_per_test=8, **extra)


def test_dos_is_offered_executable_and_saved_as_a_pending_plan(box, tmp_path):
    schema = next(t for t in TOOLS if t['name'] == 'propose_scan_plan')['parameters']['properties']
    assert schema['optional_stages']['items']['enum'] == ['dos']
    assert 'dos' in stages.known_names()  # registered in step C
    arguments = {k: v for k, v in request().items()}
    proposed = box.call('propose_scan_plan', {**arguments, 'encuts': None, 'meshes': None, 'scan_thresholds': None,
                                              'scan_preset': None, 'band_path': None, 'optional_stages': ['dos'],
                                              'stage_parameters': None, 'origin': 'tool_default', 'rationale': '',
                                              'potential_variants': None})
    assert proposed['ok'], proposed
    entry = proposed['result']['stage_parameters']['dos']
    assert entry['status'] == 'operator_confirmation_required' and entry['spec'] == {'id': 'dos', 'version': 2}
    assert not box.job('staged').exists()  # the operator still has to choose in /plan
    preview = box.call('preview_scan_defaults', {**arguments, 'optional_stages': ['dos'], 'potential_variants': None})
    assert preview['ok'], preview
    spec = preview['result']['stage_specs']['dos']
    assert spec['executable'] is True and spec['version'] == 2 and spec['suggestions']['integration']['value'] == 'tetrahedron'
    assert '尚不可执行' not in preview['result']['note']
    assert preview['result']['guides'][0]['id'] == 'dos' and preview['result']['guides'][0]['implemented'] is True
    # A record of the superseded, never-executable v1 is refused by the engine itself.
    with pytest.raises(ValueError, match='由 v2 取代'):
        full.prepare_full(engine_args(tmp_path, optional_stages=['dos'], stage_parameters={'dos': {
            'spec': {'id': 'dos', 'version': 1}, 'values': dos_values()}}))
    assert stages.with_optional(['dos'])[-1] == 'dos'


def engine_args(tmp_path, **extra):
    source = tmp_path / 'POSCAR'
    return NS(structure=str(source), workdir=str(tmp_path / 'runs/direct'), potcar=None, encut=520, mesh=[4, 4, 4],
              nbands=16, relax_mode='cell', ediff=1e-7, points=40, symprec=.01, force_tol=.01, stress_tol=1.,
              energy_tol=.001, nsw=200, **extra)


# --- A synthetic stage exercises the whole flow -------------------------------------

def probe_generate(values, evidence):
    return {'incar': {'ISMEAR': 0, 'SIGMA': values['width_eV'], 'NEDOS': 100 * values['count'],
                      'ENCUT': evidence['encut']}, 'blocked': []}


@pytest.fixture
def probe(monkeypatch):
    spec = {'id': 'probe', 'version': 1, 'stage': 'probe', 'guide': 'dos', 'title': '测试阶段',
            'executable': True, 'generate_at': 'prepare',
            'fields': {'width_eV': specs._field('number', '展宽', unit='eV', minimum=0.01, maximum=0.5),
                       'mode': specs._field('choice', '模式', choices=('a', 'b')),
                       'count': specs._field('integer', '个数', minimum=1, maximum=8),
                       'extra_eV': specs._field('number', '附加', unit='eV', minimum=0.0, maximum=1.0, nullable=True)},
            'fixed': {'LORBIT': 11}, 'derived': {'NEDOS': '100 × count'}, 'dependencies': ['mode=b ⇒ extra_eV 必填'],
            'purpose': '测试', 'records': [],
            'suggest': lambda context: {'width_eV': (0.05, '依据甲'), 'mode': ('a', '依据乙'), 'count': (2, '依据丙'),
                                        'extra_eV': (None, '依据丁')},
            'check': lambda values, context: ['mode=b 必须给 extra_eV。'] if values['mode'] == 'b' and values['extra_eV'] is None else [],
            'generate': probe_generate}
    monkeypatch.setitem(specs.SPECS, 'probe', {1: spec})
    definition = stages._definition(
        'probe', depends_on=('scf',),
        inputs={'rule': 'stage_parameter_spec', 'enforced_by': 'parameter_specs', 'note': 'test'},
        checks={'physics': stages._PHYSICS, 'parameters': {'description': '测试项', 'accepts': ('pass',)}},
        execution={'mode': 'allocation', 'runner': 'probe'}, phase_outputs=('probe.json',),
        completion={'rule': 'test', 'enforced_by': 'test'}, parameter_spec={'id': 'probe', 'version': 1}, guide='dos')
    monkeypatch.setitem(stages.DEFINITIONS, 'probe', {1: definition})
    return spec


def test_non_interactive_proposal_requires_the_operator_menu(box, probe):
    draft = plans.propose(box, request(optional_stages=['probe']), None, None, 'tool_default', '')
    # No scan difference, but stage parameters always need the operator.
    assert draft['status'] == 'pending' and draft['next'] == '/plan'
    entry = draft['stage_parameters']['probe']
    assert entry['status'] == 'operator_confirmation_required' and entry['operator_confirmed'] is False
    assert draft['selection']['requires_stage_parameter_choice'] is True
    assert entry['suggestion'] == {'width_eV': 0.05, 'mode': 'a', 'count': 2, 'extra_eV': None}
    with pytest.raises(ValueError, match='尚未选择'):
        plans.prepare(box, draft['id'])
    assert not box.job('staged').exists()


def test_ai_overrides_are_checked_against_the_spec(box, probe):
    with pytest.raises(ValueError, match='未知参数：colour'):
        plans.propose(box, request(optional_stages=['probe']), None, None, 'ai_suggestion', '理由',
                      stage_parameters=[{'stage': 'probe', 'field': 'colour', 'value': 'red'}])
    with pytest.raises(ValueError, match='width_eV 须在'):
        plans.propose(box, request(optional_stages=['probe']), None, None, 'ai_suggestion', '理由',
                      stage_parameters=[{'stage': 'probe', 'field': 'width_eV', 'value': 9}])
    with pytest.raises(ValueError, match='mode=b 必须给 extra_eV'):
        plans.propose(box, request(optional_stages=['probe']), None, None, 'ai_suggestion', '理由',
                      stage_parameters=[{'stage': 'probe', 'field': 'mode', 'value': 'b'}])
    with pytest.raises(ValueError, match='未在 optional_stages 中启用'):
        plans.propose(box, request(), None, None, 'ai_suggestion', '理由',
                      stage_parameters=[{'stage': 'probe', 'field': 'mode', 'value': 'a'}])
    with pytest.raises(ValueError, match='必须说明理由'):
        plans.propose(box, request(optional_stages=['probe']), None, None, 'ai_suggestion', ' ',
                      stage_parameters=[{'stage': 'probe', 'field': 'width_eV', 'value': 0.1}])
    assert not list(agent_paths.store_dir(box.workspace, 'scan_plans').glob('*.json'))


def run_menu(box, plan_id, answers):
    answers, shown = iter(answers), []
    plans.select(box, plan_id, lambda _: next(answers), shown.append)
    return '\n'.join(shown)


def full_flow(box):
    draft = plans.propose(box, request(optional_stages=['probe']), None, None, 'ai_suggestion', '加宽以测试',
                          stage_parameters=[{'stage': 'probe', 'field': 'width_eV', 'value': 0.1}])
    assert draft['status'] == 'pending' and draft['stage_parameters']['probe']['proposed_fields'] == ['width_eV']
    shown = run_menu(box, draft['id'], ['2'])
    assert '规范建议' in shown and 'AI 建议' in shown and '（AI 修改）' in shown and '依据甲' in shown
    assert 'mode=b ⇒ extra_eV 必填' in shown
    chosen = plans.list_plans(box)['plans'][0]
    assert chosen['status'] == 'selected' and chosen['stage_parameters']['probe']['choice'] == 'operator_accept_ai'
    plans.prepare(box, draft['id'])
    root = box.job('staged')
    config = json.loads((root / 'config.json').read_text(encoding='utf-8'))
    assert [entry['name'] for entry in config['stages']] == [*stages.LEGACY_STAGES, 'probe']
    values = {'width_eV': 0.1, 'mode': 'a', 'count': 2, 'extra_eV': None}
    assert config['stage_parameters'] == {'probe': {'spec': {'id': 'probe', 'version': 1}, 'values': values}}
    # Hashed into the config the manifest binds, and recorded with the choice.
    manifest = json.loads((root / 'manifest.json').read_text(encoding='utf-8'))
    assert manifest['hashes']['config.json'] == core.digest(root / 'config.json')
    choice = json.loads((root / 'parameter-choice.json').read_text(encoding='utf-8'))
    assert choice['stage_parameters']['probe']['values'] == values
    assert choice['stage_parameter_choices'] == {'probe': 'operator_accept_ai'}
    # Inputs generated from the chosen values, in the reviewed plan.
    plan = json.loads((root / 'input-plan.json').read_text(encoding='utf-8'))
    generated = plan['stage_parameters']['probe']['generated']['incar']
    assert generated['SIGMA'] == 0.1 and generated['NEDOS'] == 200 and generated['ENCUT'] == plan['incars']['scf']['ENCUT']
    assert 'probe 阶段参数' in (root / 'PARAMETERS.md').read_text(encoding='utf-8')
    return root


def test_full_flow_on_a_synthetic_stage(box, probe):
    full_flow(box)


def ready(root):
    (root / 'POTCAR').write_text('test fixture')
    core.save(root / 'potential-selection.json', [{'element': 'Si', 'titel': 'test-only metadata'}])
    manifest = json.loads((root / 'manifest.json').read_text())
    manifest['ready'] = True
    for name in ('POTCAR', 'potential-selection.json'):
        manifest['hashes'][name] = core.digest(root / name)
    core.save(root / 'manifest.json', manifest)
    return root


def test_chosen_parameters_are_in_the_review_snapshot(box, probe, monkeypatch):
    root = ready(full_flow(box))
    gate.confirm_physics(root, '测试')
    first = gate.context(root)
    assert first['inputs']['config']['stage_parameters']['probe']['values']['width_eV'] == 0.1
    # At the parameterized stage the review shows the choice and what it generated.
    monkeypatch.setattr(gate, 'current', lambda _: 'probe')
    monkeypatch.setattr(gate, 'evidence', lambda *_: {'summaries': {}, 'hashes': {}})
    review = box.get_stage_review(None, 'staged')
    assert review['phase'] == 'probe' and review['stage_parameters']['values']['width_eV'] == 0.1
    assert review['stage_parameters']['generated']['incar']['SIGMA'] == 0.1
    assert review['guides'][0]['id'] == 'dos'  # the stage's guide pointer, outside the snapshot
    assert review['snapshot'] == gate.context(root)['snapshot']
    # Changing a chosen value changes the snapshot (and fails the manifest check).
    config = json.loads((root / 'config.json').read_text())
    config['stage_parameters']['probe']['values']['width_eV'] = 0.2
    core.save(root / 'config.json', config)
    with pytest.raises(core.ToolError, match='审阅输入已改变'):
        gate.context(root)


def test_custom_values_in_the_menu_are_validated(box, probe):
    draft = plans.propose(box, request(optional_stages=['probe']), None, None, 'tool_default', '')
    with pytest.raises(ValueError, match='mode=b 必须给 extra_eV'):
        run_menu(box, draft['id'], ['3', 'width_eV=0.2 mode=b'])
    with pytest.raises(ValueError, match='未知参数 path'):
        run_menu(box, draft['id'], ['3', 'path=/tmp/x'])
    with pytest.raises(ValueError, match='count 必须是数值'):
        run_menu(box, draft['id'], ['3', 'count=rm'])
    assert plans.list_plans(box)['plans'][0]['status'] == 'pending'
    shown = run_menu(box, draft['id'], ['4'])
    assert '尚未选择' in shown and plans.list_plans(box)['plans'][0]['status'] == 'pending'
    run_menu(box, draft['id'], ['3', 'width_eV=0.2 mode=b extra_eV=0.3'])
    chosen = plans.list_plans(box)['plans'][0]['stage_parameters']['probe']
    assert chosen['selected'] == {'width_eV': 0.2, 'mode': 'b', 'count': 2, 'extra_eV': 0.3}
    assert chosen['choice'] == 'operator_custom'


def test_execution_side_validation_is_authoritative(tmp_path, probe):
    Poscar(Structure(Lattice.cubic(5.43), ['Si'], [[0, 0, 0]])).write_file(tmp_path / 'POSCAR')
    entry = lambda **values: {'probe': {'spec': {'id': 'probe', 'version': 1},
                                        'values': {'width_eV': 0.05, 'mode': 'a', 'count': 2, 'extra_eV': None, **values}}}
    with pytest.raises(ValueError, match='须且只须覆盖启用的参数化阶段：probe'):
        full.prepare_full(engine_args(tmp_path, optional_stages=['probe']))
    with pytest.raises(ValueError, match='未知参数：shell'):
        full.prepare_full(engine_args(tmp_path, optional_stages=['probe'], stage_parameters=entry(shell='ls')))
    with pytest.raises(ValueError, match='count 须在'):
        full.prepare_full(engine_args(tmp_path, optional_stages=['probe'], stage_parameters=entry(count=99)))
    with pytest.raises(ValueError, match='须且只须覆盖'):
        full.prepare_full(engine_args(tmp_path, stage_parameters=entry()))
    with pytest.raises(ValueError, match='本引擎没有参数规范 probe v2'):
        full.prepare_full(engine_args(tmp_path, optional_stages=['probe'], stage_parameters={
            'probe': {'spec': {'id': 'probe', 'version': 2}, 'values': entry()['probe']['values']}}))
    assert not (tmp_path / 'runs/direct').exists()
    full.prepare_full(engine_args(tmp_path, optional_stages=['probe'], stage_parameters=entry()))
    config = json.loads((tmp_path / 'runs/direct/config.json').read_text())
    assert config['stage_parameters']['probe']['values']['width_eV'] == 0.05


def test_legacy_preparation_records_no_stage_parameters(tmp_path):
    Poscar(Structure(Lattice.cubic(5.43), ['Si'], [[0, 0, 0]])).write_file(tmp_path / 'POSCAR')
    full.prepare_full(engine_args(tmp_path))
    root = tmp_path / 'runs/direct'
    config = json.loads((root / 'config.json').read_text())
    assert 'stage_parameters' not in config and config['stages'] == stages.record()
    assert 'stage_parameters' not in json.loads((root / 'input-plan.json').read_text(encoding='utf-8'))


def test_remote_payload_carries_stages_only_when_requested(tmp_path, monkeypatch):
    from vasp_agent.remote.remote_compute import RemoteCompute
    Poscar(Structure(Lattice.cubic(5.43), ['Si'], [[0, 0, 0]])).write_file(tmp_path / 'POSCAR')
    sent = []
    monkeypatch.setattr(RemoteCompute, '__init__', lambda self, workspace, server: setattr(self, 'workspace', workspace))
    monkeypatch.setattr(RemoteCompute, 'call', lambda self, action, name, **payload: sent.append(payload) or {})
    remote = RemoteCompute(tmp_path, 'test')
    remote.prepare('a', 'POSCAR')
    remote.prepare('b', 'POSCAR', optional_stages=['probe'], stage_parameters={'probe': {'spec': {}, 'values': {}}})
    assert 'optional_stages' not in sent[0]['parameters'] and 'stage_parameters' not in sent[0]['parameters']
    assert sent[1]['parameters']['optional_stages'] == ['probe'] and 'probe' in sent[1]['parameters']['stage_parameters']


def test_worker_refuses_optional_stages_on_an_engine_without_the_marker():
    import inspect
    from vasp_agent.remote import remote_worker
    assert 'parameter_specs' in remote_worker.ENGINE_MODULES
    source = inspect.getsource(remote_worker._prepare_task)
    assert "getattr(full_workflow, 'PARAMETER_SPECS_VERSION', 0) < 1" in source
    assert full.PARAMETER_SPECS_VERSION == 1 and specs.PARAMETER_SPECS_VERSION == 1


def test_scan_retry_keeps_the_source_stages_and_parameters(box, probe, monkeypatch):
    from vasp_agent.workflow.convergence_reuse import prepare_retry
    monkeypatch.setattr(full.Potcar, 'from_file', lambda p: [NS(element='Si', functional='PBE', enmax=245., ZVAL=4., TITEL='PAW_PBE Si')])
    root = ready(full_flow(box))
    import contextlib, io
    with contextlib.redirect_stdout(io.StringIO()):
        prepare_retry(root, box.job('retry'), 'both', None, None)
    config = json.loads((box.job('retry') / 'config.json').read_text())
    source = json.loads((root / 'config.json').read_text())
    assert config['stages'] == source['stages'] and config['stage_parameters'] == source['stage_parameters']
