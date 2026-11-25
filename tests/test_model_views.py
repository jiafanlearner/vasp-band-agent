"""Compact model views: smaller history, same evidence, same snapshots."""
import json
import re
from types import SimpleNamespace as NS

import numpy as np
import pytest
from pymatgen.core import Lattice, Structure
from pymatgen.io.vasp.inputs import Poscar

import vasp_agent.chat.ai_agent as ai
import vasp_agent.workflow.full_workflow as full
import vasp_agent.workflow.stage_gate as gate
import vasp_agent.workflow.vasp_band_agent as core
from vasp_agent.chat.model_views import KEEP, MIN_CHARS, compact_result, model_output
from vasp_agent.models.context_compaction import MODEL_VIEW_MARK, ContextCompactor
from vasp_agent.remote.remote_compute import RemoteCompute
from test_ai_agent import Item, function, reply


def prompt_fields():
    """snake_case names the prompt or a tool description tells the model about."""
    text = ai.INSTRUCTIONS + ' '.join(t['description'] for t in ai.MODEL_TOOLS)
    return set(re.findall(r'\b[a-z][a-z0-9]*(?:_[a-z0-9]+)+\b', text)) - {t['name'] for t in ai.MODEL_TOOLS}


# Copied verbatim wherever they occur (value equality).
VERBATIM = KEEP | {'effective_thresholds', 'threshold_sources', 'checks', 'failed_checks', 'failure_reasons',
    'next', 'next_action', 'error_code', 'submission_confirmed', 'snapshot', 'required_checks'}
# Still present as fields (a matrix or path may be shown in a shorter form).
PROTECTED = VERBATIM | prompt_fields()


def pairs(value, out=None):
    """(key, encoded value) of every verbatim field, wherever it sits."""
    out = set() if out is None else out
    if isinstance(value, dict):
        for key, item in value.items():
            if key in VERBATIM:
                out.add((key, json.dumps(item, sort_keys=True, ensure_ascii=False)))
            pairs(item, out)
    elif isinstance(value, list):
        for item in value:
            pairs(item, out)
    return out


def keys(value, out=None):
    out = set() if out is None else out
    if isinstance(value, dict):
        out.update(value)
        for item in value.values():
            keys(item, out)
    elif isinstance(value, list):
        for item in value:
            keys(item, out)
    return out


def read_back(box, sha256):
    """Page a stored payload back through the model's own tool."""
    text, offset = '', 0
    while offset is not None:
        page = box.call('read_context_evidence', {'sha256': sha256, 'offset': offset})['result']
        text, offset = text + page['content'], page['next_offset']
    return json.loads(text)


def assert_faithful(box, result, view):
    """Protected fields unchanged, every stored part retrievable and equal."""
    meta = view['model_view']
    assert pairs(result) <= pairs(view), sorted(pairs(result) - pairs(view))[:3]
    assert {k for k in keys(result) if k in PROTECTED} <= keys(view)
    assert view['snapshot'] == result['snapshot']
    assert read_back(box, meta['full']['sha256']) == json.loads(json.dumps(result, default=str))
    for path, sha256 in meta['sections'].items():
        part = result
        for key in path.split('/'):
            part = part[key]
        assert read_back(box, sha256) == part


@pytest.fixture
def staged(tmp_path, monkeypatch):
    structure = Structure(Lattice.cubic(5.43), ['Si'], [[0, 0, 0]])
    source = tmp_path / 'POSCAR'
    Poscar(structure).write_file(source)
    root = tmp_path / 'runs/si'
    full.prepare_full(NS(structure=str(source), workdir=str(root), potcar=None,
        encut=520, mesh=[4, 4, 4], nbands=16, relax_mode='cell', ediff=1e-7,
        points=40, symprec=.01, force_tol=.01, stress_tol=1., energy_tol=.001, nsw=200))
    (root / 'POTCAR').write_text('test fixture')
    core.save(root / 'potential-selection.json', [{'element': 'Si', 'titel': 'test-only metadata'}])
    manifest = json.loads((root / 'manifest.json').read_text())
    manifest['ready'] = True
    manifest['hashes']['POTCAR'] = core.digest(root / 'POTCAR')
    manifest['hashes']['potential-selection.json'] = core.digest(root / 'potential-selection.json')
    core.save(root / 'manifest.json', manifest)
    monkeypatch.setattr(full.Potcar, 'from_file', lambda p: [NS(element='Si')])
    gate.confirm_physics(root, 'nonmagnetic bulk silicon PBE bands')

    def fake_execute(root, name, command):
        folder = root / name
        structure = Poscar.from_file(folder / 'POSCAR').structure
        if not (folder / 'complete.json').exists():
            (folder / 'vasprun.xml').write_text('mock output ' + name)
            data = {'xml_sha256': core.digest(folder / 'vasprun.xml')}
            if name == 'scf':
                (folder / 'CHGCAR').write_text('mock charge')
                data['charge_sha256'] = core.digest(folder / 'CHGCAR')
            core.save(folder / 'complete.json', data)
        n = len(structure)
        forces = (np.arange(3 * n).reshape(n, 3) * 1e-3).tolist()
        return NS(final_structure=structure, final_energy=-10. * n, converged_ionic=True,
                  parameters={'NELM': 150, 'NBANDS': 16, 'NELECT': 8}, efermi=0.,
                  ionic_steps=[{'forces': forces, 'stress': np.eye(3).tolist(), 'electronic_steps': [{}]}])
    monkeypatch.setattr(core, 'execute', fake_execute)
    # The mock vasprun.xml is not XML; the audit's own reading is tested elsewhere.
    import vasp_agent.remote.remote_worker as remote_worker
    monkeypatch.setattr(remote_worker, 'scf_occupation_audit', lambda folder: {
        'passed': True, 'threshold': 1e-6, 'channels': {'up': {'kpoints': 10, 'nbands': 16, 'highest_band_max_occupation': 0.0}},
        'source': 'scf/vasprun.xml', 'xml_sha256': core.digest(folder / 'scf/vasprun.xml')})
    monkeypatch.setattr(full, 'analyze_full', lambda root: core.save(root / 'workflow-state.json', {'status': 'complete'}))
    return root


class ReviewingModel:
    """Fake model: reads the stage review, then records it with the snapshot it saw."""

    def __init__(self, server, phase):
        self.server, self.phase, self.turn, self.seen = server, phase, 0, None

    def create(self, **kwargs):
        self.turn += 1
        if self.turn == 1:
            return reply(function('get_stage_review', {'server': self.server, 'job_name': 'si'}, 'c1'))
        if self.turn == 2:
            # The history copy -- what the model actually reads.
            self.seen = json.loads([x for x in kwargs['input'] if x.get('type') == 'function_call_output'][-1]['output'])
            checks = [dict(item=item, judgment='pending' if item == 'validation_plan' else 'pass', blocking=False,
                           evidence='test', reason='test', next_action='无需') for item in self.seen['result']['required_checks']]
            return reply(function('record_stage_review', {'server': self.server, 'job_name': 'si',
                'snapshot': self.seen['result']['snapshot'], 'phase': self.phase, 'checks': checks}, 'c2'))
        return reply(text='已审阅。')


def remote_fake(monkeypatch, tmp_path):
    """RemoteCompute.call served by the local engine: the remote branch, no SSH."""
    monkeypatch.setattr(RemoteCompute, '__init__', lambda self, workspace, server: None)
    root = tmp_path / 'runs/si'
    def call(self, action, job_name=None, **params):
        from vasp_agent.remote.remote_worker import with_occupation_audit
        if action == 'stage_context':
            return with_occupation_audit(gate.context(root), root)
        assert action == 'stage_review', action
        return gate.record(root, params['snapshot'], params['phase'], params['checks'], params['reviewer'])
    monkeypatch.setattr(RemoteCompute, 'call', call)


@pytest.mark.parametrize('server', [None, 'test'])
def test_stage_review_views_at_every_phase(staged, tmp_path, monkeypatch, server):
    if server:
        remote_fake(monkeypatch, tmp_path)
    box = ai.Toolbox(tmp_path)
    ratios = {}
    for phase in gate.PHASES:
        model = ReviewingModel(server, phase)
        agent = ai.Agent(NS(responses=model), 'offline-test', box, session='review-' + phase)
        agent.ask('审阅当前阶段。')
        result = box.get_stage_review(server, 'si')  # the full result, read again
        view = model.seen['result']
        assert view['phase'] == phase and view['model_view']['compact']
        assert_faithful(box, result, view)
        recorded = json.loads([x['output'] for x in agent.history if x.get('call_id') == 'c2'
                               and x.get('type') == 'function_call_output'][-1])
        assert recorded['ok'] and recorded['result']['allowed'], recorded
        # The snapshot was computed by the engine over the full data, unchanged.
        assert recorded['result']['snapshot'] == gate.context(staged)['snapshot']
        audit = [json.loads(line) for line in box.audit.read_text(encoding='utf-8').splitlines()]
        logged = [x for x in audit if x.get('tool') == 'get_stage_review'][-1]['output']
        assert logged['ok'] and 'model_view' not in json.dumps(logged)  # the audit keeps the full output
        ratios[phase] = len(json.dumps(view, ensure_ascii=False)) / len(json.dumps(result, ensure_ascii=False))
        full.run_full(staged, ['mock'])
    assert gate.current(staged) == 'complete'
    assert max(ratios.values()) < .5, ratios  # measured 0.22-0.32


def test_input_review_view_keeps_snapshot_and_detail(staged, tmp_path):
    box = ai.Toolbox(tmp_path)
    result = box.get_input_review(None, 'si')
    view = model_output('get_input_review', {'ok': True, 'result': result}, box.store_evidence)['result']
    assert_faithful(box, result, view)
    assert view['initial_inputs']['incar']['view'] == 'base_plus_differences'
    assert view['plan']['parameter_notes']['sources']['ENCUT']
    assert len(json.dumps(view, ensure_ascii=False)) < .5 * len(json.dumps(result, ensure_ascii=False))
    assert box.review_snapshots[(None, 'si')] == view['snapshot']


def test_threshold_fields_are_verbatim_and_hash_maps_are_counted(tmp_path):
    comparison = {'passed': False, 'energy_eV_per_atom': .002, 'stress_kbar': 3.1,
                  'effective_thresholds': {'energy_eV_per_atom': .001}, 'threshold_sources': {'energy_eV_per_atom': 'default'},
                  'checks': {'energy_eV_per_atom': {'value': .002, 'threshold': .001, 'enabled': True, 'passed': False}},
                  'failed_checks': ['energy_eV_per_atom'], 'failure_reasons': ['能量差超过阈值']}
    hashes = {f'stage-{i}/vasprun.xml': f'{i:064x}' for i in range(30)}
    result = {'parameters': {'encut': {'status': 'failed', 'comparisons': [comparison, dict(comparison)],
                                       'samples': [{'forces': [[0.1, 0, 0]] * 8, 'stress_kbar': [[1, 0, 0]] * 3, 'max_force': .1}],
                                       'hashes': hashes, 'cutoff_vs_highest': [{'encut': 520, 'passed': False}]}},
              'next_action': {'tool': 'prepare_scan_retry'}}
    box = ai.Toolbox(tmp_path)
    view = compact_result(result, box.store_evidence)
    encut = view['parameters']['encut']
    assert encut['comparisons'][0] == comparison
    assert encut['hashes']['count'] == 30 and len(encut['hashes']) == 3
    assert encut['samples'][0]['max_force'] == .1 and isinstance(encut['samples'][0]['forces'], str)
    assert encut['cutoff_vs_highest'] == result['parameters']['encut']['cutoff_vs_highest']
    assert view['next_action'] == result['next_action']
    assert read_back(box, view['model_view']['sections']['parameters/encut']) == result['parameters']['encut']


def test_small_failed_and_unlisted_outputs_are_unchanged(tmp_path):
    store = ai.Toolbox(tmp_path).store_evidence
    error = {'ok': False, 'error': 'x' * 9000, 'error_code': 'INPUT_HASH_MISMATCH'}
    assert model_output('get_stage_review', error, store) is error
    small = {'ok': True, 'result': {'hashes': {f'{i}': f'{i:064x}' for i in range(5)}}}
    assert len(json.dumps(small['result'])) < MIN_CHARS
    assert model_output('get_stage_review', small, store) == small
    other = {'ok': True, 'result': {'content': 'y' * 9000}}
    assert model_output('read_workspace_file', other, store) == other


def test_projection_keeps_latest_view_whole_and_shrinks_superseded(tmp_path):
    view = {'ok': True, 'result': {'phase': 'scf', 'snapshot': 'a' * 64, 'blob': ['z' * 150] * 60,
                                   'model_view': {'compact': True, 'full': {'sha256': 'b' * 64, 'chars': 1}, 'sections': {}}}}
    text = json.dumps(view, ensure_ascii=False)
    assert MODEL_VIEW_MARK in text and 6000 < len(text)
    call = lambda cid: {'type': 'function_call', 'call_id': cid, 'name': 'get_stage_review', 'arguments': '{"job_name":"si"}'}
    out = lambda cid: {'type': 'function_call_output', 'call_id': cid, 'output': text}
    projected = ContextCompactor(tmp_path / 'c.json').project([call('a'), out('a'), call('b'), out('b')])
    assert projected[3]['output'] == text
    old = json.loads(projected[1]['output'])
    assert old['summary']['superseded_by_later_call'] and old['summary']['full']['sha256'] == 'b' * 64
    assert len(projected[1]['output']) < 1500


def test_view_failure_falls_back_to_full_output(tmp_path, monkeypatch):
    agent = ai.Agent(None, 'offline-test', ai.Toolbox(tmp_path), adapter=NS(create=None))
    monkeypatch.setattr(agent.tools, 'store_evidence', lambda payload: 1 / 0)
    output = {'ok': True, 'result': {'hashes': {f'f{i}': f'{i:064x}' for i in range(80)}}}
    assert agent.model_output('get_stage_review', output) is output


def test_real_download_band_analysis_view(tmp_path):
    """The La2CuO4 bands.csv from a real download, when this checkout has it (read-only)."""
    from pathlib import Path
    name = 'downloads/eshell111-vasp544/La2CuO4_HTT_Cu_pv_780_ions_32c-15babfb7'
    # The project root, or the main checkout when this runs in .claude/worktrees/<name>.
    sample = next((p / name for p in Path(__file__).resolve().parents if (p / name / 'bands.csv').is_file()), None)
    if sample is None:
        pytest.skip('real download sample not present in this checkout')
    from vasp_agent.workflow.band_analysis import analyze_bands
    documents = {n: json.loads((sample / n).read_text(encoding='utf-8')) for n in ('scf-summary.json', 'report.json')}
    result = analyze_bands((sample / 'bands.csv').read_text(encoding='utf-8-sig'), documents['scf-summary.json'],
                           documents['report.json'], 0, 0)
    box = ai.Toolbox(tmp_path)
    view = compact_result(result, box.store_evidence, 'analyze_band_results')
    assert view['metallic_check'] == result['metallic_check']
    assert read_back(box, view['model_view']['full']['sha256']) == json.loads(json.dumps(result))


def test_list_scan_plans_view_keeps_open_plans_and_caps_prepared_history(tmp_path):
    def make_plan(i, status):
        return {
            'id': f'plan-{i:02d}',
            'status': status,
            'request': {'server': 'hpc', 'job_name': f'job-{i}', 'structure': 'Si.POSCAR',
                        'encut': 520, 'mesh': [6, 6, 6], 'nbands': 16, 'relax_mode': 'cell',
                        'optional_stages': ['dos'] if i == 0 else None},
            'defaults': {'encuts': [520, 598, 676], 'meshes': [[4, 4, 4], [5, 5, 5], [6, 6, 6]]},
            'suggestion': {'encuts': [520, 600, 680], 'meshes': [[6, 6, 6], [8, 8, 8], [10, 10, 10]]},
            'rationale': '详细参数理由' * 60,
            'selected': {'encuts': [520, 600, 680]} if status != 'pending' else None,
            'choice': None if status == 'pending' else 'operator_accept_ai',
            'selected_sources': {'encuts': 'ai_suggestion'} if status != 'pending' else None,
            'stage_parameters': {'dos': {'status': 'selected', 'selected': {'kmesh_factor': 2}}} if i == 0 else None,
            'selection': {'operator_confirmed': status != 'pending', 'requires_parameter_choice': status == 'pending',
                          'message': '很长的固定提示文本' * 20},
            'next': {'pending': '/plan', 'selected': 'prepare_scan_plan', 'prepared': 'get_stage_review'}[status],
        }
    plans_list = [make_plan(0, 'pending'), make_plan(1, 'selected')] + [make_plan(i, 'prepared') for i in range(2, 20)]
    for entry in plans_list:
        if entry['stage_parameters'] is None:
            entry.pop('stage_parameters')
    result = {'plans': plans_list, 'total': len(plans_list)}
    box = ai.Toolbox(tmp_path)
    view = compact_result(result, box.store_evidence, 'list_scan_plans')
    assert view['model_view']['compact'] is True
    assert view['total'] == 20 and view['omitted_prepared_plans'] == 13
    assert [p['id'] for p in view['plans']] == ['plan-00', 'plan-01', 'plan-02', 'plan-03', 'plan-04', 'plan-05', 'plan-06']
    assert view['plans'][0]['stage_parameters'] == plans_list[0]['stage_parameters']
    assert 'message' not in view['plans'][0]['selection']
    assert len(json.dumps(view, ensure_ascii=False)) < 0.25 * len(json.dumps(result, ensure_ascii=False))
    assert read_back(box, view['model_view']['full']['sha256']) == result
