"""Tests for Layer 4 stage-focused review projection (stage_review_view.py)."""
from __future__ import annotations

from copy import deepcopy
import hashlib
import json
from types import SimpleNamespace as NS

import numpy as np
import pytest
from pymatgen.core import Lattice, Structure
from pymatgen.io.vasp.inputs import Poscar

import vasp_agent.chat.ai_agent as ai
from vasp_agent.chat import model_views
import vasp_agent.workflow.full_workflow as full
import vasp_agent.workflow.stage_gate as gate
import vasp_agent.workflow.vasp_band_agent as core


class MemoryEvidenceStore:
    """Content-addressed store matching Toolbox.store_evidence semantics."""

    def __init__(self) -> None:
        self.items: dict[str, str] = {}

    def store(self, payload: object) -> dict[str, object]:
        raw = json.dumps(payload, ensure_ascii=False, default=str)
        digest = hashlib.sha256(raw.encode('utf-8')).hexdigest()
        self.items.setdefault(digest, raw)
        return {'sha256': digest, 'chars': len(raw)}

    def load(self, sha256: str) -> object:
        return json.loads(self.items[sha256])


def _find_dangling_same_as(root: object) -> list[str]:
    """Return any {'same_as': path} pointers whose target is missing or omitted."""
    dangling: list[str] = []

    def _resolve(path_str: str) -> bool:
        cur: object = root
        for part in path_str.split('/'):
            if isinstance(cur, dict) and part in cur:
                cur = cur[part]
            elif isinstance(cur, list) and part.isdigit() and int(part) < len(cur):
                cur = cur[int(part)]
            else:
                return False
        return not (isinstance(cur, dict) and 'omitted_for_phase' in cur)

    def _walk(node: object) -> None:
        if isinstance(node, dict):
            if set(node) == {'same_as'} and isinstance(node['same_as'], str):
                if not _resolve(node['same_as']):
                    dangling.append(node['same_as'])
                return
            for value in node.values():
                _walk(value)
        elif isinstance(node, list):
            for item in node:
                _walk(item)

    _walk(root)
    return dangling


def _synthetic_stage_context(phase: str, *, include_heavy_duplicates: bool = True) -> dict[str, object]:
    """Build a realistic stage_gate.context()-shaped payload with repeated subtrees."""
    repeated_block = {
        'formula': 'Si2',
        'spacegroup': 'Fd-3m',
        'details': 'x' * 240,
    }
    hashes = {f'file-{i:02d}/vasprun.xml': f'{i:064x}' for i in range(16)}
    incars = {
        'convergence/encut-0': {'ENCUT': 350, 'PREC': 'Accurate', 'EDIFF': 1e-6, 'ISMEAR': 0, 'SIGMA': 0.05},
        'convergence/encut-1': {'ENCUT': 400, 'PREC': 'Accurate', 'EDIFF': 1e-6, 'ISMEAR': 0, 'SIGMA': 0.05},
        'convergence/mesh-0': {'ENCUT': 400, 'PREC': 'Accurate', 'EDIFF': 1e-6, 'ISMEAR': 0, 'SIGMA': 0.05},
        'relax-1': {'ENCUT': 400, 'PREC': 'Accurate', 'EDIFF': 1e-6, 'IBRION': 2, 'ISIF': 3, 'NSW': 200},
        'relax-2': {'ENCUT': 400, 'PREC': 'Accurate', 'EDIFF': 1e-6, 'IBRION': 2, 'ISIF': 3, 'NSW': 200},
        'validation/reference': {'ENCUT': 400, 'PREC': 'Accurate', 'EDIFF': 1e-6, 'NSW': 0},
        'validation/higher-cutoff': {'ENCUT': 460, 'PREC': 'Accurate', 'EDIFF': 1e-6, 'NSW': 0},
        'validation/denser-mesh': {'ENCUT': 400, 'PREC': 'Accurate', 'EDIFF': 1e-6, 'NSW': 0},
        'scf': {'ENCUT': 400, 'PREC': 'Accurate', 'EDIFF': 1e-6, 'LCHARG': True, 'ISMEAR': 0, 'SIGMA': 0.05},
        'bands': {'ENCUT': 400, 'PREC': 'Accurate', 'EDIFF': 1e-6, 'ICHARG': 11, 'ISMEAR': 0, 'SIGMA': 0.05},
    }
    initial_inputs = {
        'convergence/encut-0/INCAR': 'ENCUT = 350\nPREC = Accurate\n' + ('# note\n' * 30),
        'convergence/encut-0/KPOINTS': 'Automatic\n0\nGamma\n8 8 8\n0 0 0\n',
        'convergence/mesh-0/INCAR': 'ENCUT = 400\nPREC = Accurate\n' + ('# note\n' * 30),
        'convergence/mesh-0/KPOINTS': 'Automatic\n0\nGamma\n6 6 6\n0 0 0\n',
    }
    summaries: dict[str, object] = {}
    if phase in ('relaxation', 'scf', 'bands', 'dos'):
        summaries['convergence.json'] = {'passed': True, 'encut': 400, 'mesh': [8, 8, 8], 'dup': deepcopy(repeated_block)}
    if phase in ('scf', 'bands', 'dos'):
        summaries['relaxation.json'] = {'passed': True, 'max_force': 0.002, 'forces': [[0.001, 0.0, 0.0]] * 6}
        summaries['geometry-summary.json'] = {'volume_change_pct': 0.4, 'dup': deepcopy(repeated_block)}
        summaries['post-relax-validation.json'] = {'passed': True, 'energy_diff_eV_per_atom': 0.0002}
    if phase in ('bands', 'dos'):
        summaries['scf-summary.json'] = {
            'converged_electronic': True,
            'max_force': 0.0005,
            'max_stress_kbar': 0.12,
            'efermi_eV': 5.82,
            'dup': deepcopy(repeated_block),
        }

    ctx: dict[str, object] = {
        'phase': phase,
        'snapshot': 'a' * 64,
        'required_checks': ['physics', 'scf'] if phase in ('bands', 'dos') else ['physics'],
        'check_details': {'physics': {'description': 'nonmagnetic PBE', 'accepts': ['pass']}},
        'rules_version': 1,
        'review_lease': {'lease_id': 'lse-demo', 'phase': phase, 'snapshot': 'a' * 64},
        'physics': {'dimension': '3D', 'magnetism': 'nonmagnetic', 'functional': 'PBE'},
        'execution_tags': {'KPAR': 2, 'NCORE': 4},
        'incar_solver_diff': {'version': 1, 'phase': phase, 'changes': [{'tag': 'NELM', 'baseline': 150, 'override': 160}]},
        'inputs': {
            'manifest': {'ready': True, 'stage_gates': True, 'hashes': dict(hashes)},
            'config': {'encuts': [350, 400, 450], 'meshes': [[6, 6, 6], [8, 8, 8], [10, 10, 10]], 'nbands': 16},
            'environment': {
                'selected': 'vasp544-public',
                'executable_sha256': 'b' * 64,
                'capabilities': ['vasp:5'],
                'matched': True,
            },
            'potential_metadata': [{'element': 'Si', 'enmax_eV': 245.3, 'zval': 4.0}],
            'initial_poscar': 'Si2\n1.0\n' + ('0.0 0.0 0.0\n' * 40),
            'submit_script': '#!/bin/bash\n#SBATCH -N 1\n' + ('echo line\n' * 40),
            'scan_verification': {'passed': True, 'dup': deepcopy(repeated_block)} if include_heavy_duplicates else {'passed': True},
            'initial_inputs': initial_inputs,
            'plan': {
                'schema': 2,
                'incars': incars,
                'kpoints': {
                    'relax-1': 'Gamma 8 8 8\n',
                    'relax-2': 'Gamma 8 8 8\n',
                    'validation/denser-mesh': 'Gamma 10 10 10\n',
                },
                'parameter_notes': {
                    f'TAG_{i}': {'source': 'workflow default', 'explanation': f'Long static note {i} ' + ('z' * 40)}
                    for i in range(12)
                },
                'pending_validation': ['ENCUT 和 k 网格收敛'],
            },
        },
        'evidence': {
            'summaries': summaries,
            'hashes': dict(hashes),
        },
        'checkpoint': {'completed': [], 'next': phase, 'evidence_hashes': dict(hashes)},
    }
    if phase == 'bands':
        ctx['band_path'] = {'convention': 'hinuma', 'segments': [{'points': ['G', 'X']}]}
        ctx['scf_occupation_audit'] = {'passed': True, 'highest_band_max_occupation': 0.0}
    if phase == 'dos':
        ctx['stage_parameters'] = {'spec': {'id': 'dos', 'version': 2}, 'values': {'kmesh_factor': 2}}
        ctx['dos_plan'] = {
            'incar': {'ENCUT': 400, 'ICHARG': 11, 'LORBIT': 11, 'ISMEAR': -5, 'NEDOS': 3001},
            'kpoints': {'mesh': [16, 16, 16]},
            'blocked': [],
            'program_checks': {'scf_source': {'passed': True}, 'inputs': {'passed': True}},
            'scf_evidence': {'scf_fermi_eV': 5.82, 'scf_nbands': 16, 'nelect': 8.0},
        }
    return ctx


@pytest.mark.parametrize('phase', ['convergence', 'relaxation', 'scf', 'bands', 'dos'])
def test_projection_matrix_across_all_five_phases(phase: str) -> None:
    from vasp_agent.control.stage_review_view import build_stage_review_view

    store = MemoryEvidenceStore()
    full_ctx = _synthetic_stage_context(phase)
    view = build_stage_review_view(full_ctx, store.store)

    assert view['phase'] == phase
    assert view['snapshot'] == full_ctx['snapshot']
    assert view['review_lease'] == full_ctx['review_lease']
    assert view['incar_solver_diff'] == full_ctx['incar_solver_diff']
    # Environment (~500 B) is preserved verbatim across all 5 phases.
    assert view['inputs']['environment'] == full_ctx['inputs']['environment']
    assert view['inputs']['potential_metadata'] == full_ctx['inputs']['potential_metadata']

    # No dangling same_as pointers anywhere in the returned view.
    assert _find_dangling_same_as(view) == []

    # stage_review_view metadata always carries full_sha256 & omitted index.
    meta = view['stage_review_view']
    assert meta['projected'] is True
    assert meta['phase'] == phase
    assert store.load(meta['full_sha256']) == full_ctx
    for digest in meta['omitted'].values():
        assert digest in store.items

    incars_node = view['inputs']['plan']['incars']
    incar_stages = set(incars_node['stages']) if isinstance(incars_node, dict) and 'stages' in incars_node else set(incars_node)

    if phase == 'convergence':
        assert 'omitted_for_phase' not in view['inputs']['initial_inputs']
        assert isinstance(view['inputs']['initial_poscar'], str)
        assert isinstance(view['inputs']['submit_script'], str)
        assert 'omitted_for_phase' not in view['inputs']['scan_verification']
        assert 'omitted_for_phase' not in view['inputs']['plan']['parameter_notes']
        assert 'omitted_for_phase' not in view['inputs']['plan']['kpoints']
        assert incar_stages == {'convergence/encut-0', 'convergence/encut-1', 'convergence/mesh-0'}
        assert 'incars_omitted' in view['inputs']['plan']
    else:
        assert view['inputs']['initial_inputs']['omitted_for_phase'] == phase
        assert view['inputs']['initial_poscar']['omitted_for_phase'] == phase
        assert view['inputs']['submit_script']['omitted_for_phase'] == phase
        assert view['inputs']['scan_verification']['omitted_for_phase'] == phase
        assert view['inputs']['plan']['parameter_notes']['omitted_for_phase'] == phase

    if phase == 'relaxation':
        assert 'omitted_for_phase' not in view['inputs']['plan']['kpoints']
        assert incar_stages == {
            'relax-1', 'relax-2', 'validation/reference', 'validation/higher-cutoff', 'validation/denser-mesh',
        }
        assert set(view['evidence']['summaries']) == {'convergence.json'}
    elif phase == 'scf':
        assert view['inputs']['plan']['kpoints']['omitted_for_phase'] == 'scf'
        assert incar_stages == {'scf'}
        assert set(view['evidence']['summaries']) == {
            'relaxation.json', 'geometry-summary.json', 'post-relax-validation.json',
        }
        assert view['evidence']['summaries_omitted']['omitted_keys'] == ['convergence.json']
    elif phase == 'bands':
        assert view['inputs']['plan']['kpoints']['omitted_for_phase'] == 'bands'
        assert incar_stages == {'bands', 'scf'}
        assert set(view['evidence']['summaries']) == {'scf-summary.json'}
        assert view['band_path'] == full_ctx['band_path']
        assert view['scf_occupation_audit'] == full_ctx['scf_occupation_audit']
    elif phase == 'dos':
        assert view['inputs']['plan']['kpoints']['omitted_for_phase'] == 'dos'
        assert incar_stages == {'scf'}
        # Critical: DOS keeps scf-summary.json for electronic/force/stress audit.
        assert set(view['evidence']['summaries']) == {'scf-summary.json'}
        assert view['dos_plan'] == full_ctx['dos_plan']
        assert view['stage_parameters'] == full_ctx['stage_parameters']


def test_small_projected_payload_below_min_chars_still_carries_full_sha256() -> None:
    from vasp_agent.control.stage_review_view import build_stage_review_view

    store = MemoryEvidenceStore()
    full_ctx = {
        'phase': 'scf',
        'snapshot': 'c' * 64,
        'required_checks': ['physics'],
        'inputs': {
            'environment': {'selected': 'vasp544'},
            'initial_inputs': {'convergence/encut-0/INCAR': 'ENCUT = 400\n' + ('# pad\n' * 400)},
            'plan': {
                'incars': {
                    'convergence/encut-0': {'ENCUT': 400},
                    'scf': {'ENCUT': 400, 'LCHARG': True},
                },
            },
        },
        'evidence': {'summaries': {'relaxation.json': {'passed': True}}},
    }
    assert len(model_views.encoded(full_ctx)) > model_views.MIN_CHARS

    view = build_stage_review_view(full_ctx, store.store)
    projected_without_meta = {k: v for k, v in view.items() if k != 'stage_review_view'}
    assert len(model_views.encoded(projected_without_meta)) < model_views.MIN_CHARS
    assert 'model_view' not in view
    assert view['stage_review_view']['projected'] is True
    assert view['stage_review_view']['phase'] == 'scf'
    assert store.load(view['stage_review_view']['full_sha256']) == full_ctx


def test_large_projected_payload_binds_model_view_full_to_raw_and_marks_sections_scope() -> None:
    from vasp_agent.control.stage_review_view import build_stage_review_view, project_stage_review

    store = MemoryEvidenceStore()
    full_ctx = _synthetic_stage_context('scf')
    projected, raw_stored, _meta = project_stage_review(full_ctx, store.store)
    assert len(model_views.encoded(projected)) >= model_views.MIN_CHARS

    view = build_stage_review_view(full_ctx, store.store)
    assert 'model_view' in view
    assert view['model_view']['full']['sha256'] == raw_stored['sha256']
    assert view['model_view']['full']['sha256'] == view['stage_review_view']['full_sha256']
    assert view['model_view']['sections_scope'] == 'projected_view'
    assert store.load(view['model_view']['full']['sha256']) == full_ctx

    # Every model_view section digest reads back the matching section of the projected view.
    for path_str, digest in view['model_view']['sections'].items():
        cur: object = projected
        for part in path_str.split('/'):
            cur = cur[part]  # type: ignore[index]
        assert store.load(digest) == cur


def test_projection_does_not_mutate_full_result_and_preserves_raw_digest() -> None:
    from vasp_agent.control.stage_review_view import build_stage_review_view

    store = MemoryEvidenceStore()
    full_ctx = _synthetic_stage_context('bands')
    snapshot_before = deepcopy(full_ctx)

    view = build_stage_review_view(full_ctx, store.store)
    assert full_ctx == snapshot_before
    assert store.load(view['stage_review_view']['full_sha256']) == snapshot_before


def test_unknown_or_future_phase_falls_back_safely(monkeypatch) -> None:
    from vasp_agent.control.stage_review_view import build_stage_review_view
    from vasp_agent.workflow import stage_definitions

    store = MemoryEvidenceStore()
    # Minimal dict without inputs/evidence passes through cleanly.
    minimal = {'phase': 'scf', 'snapshot': 'snap-min', 'required_checks': ['physics']}
    assert build_stage_review_view(minimal, store.store) == minimal

    # Unregistered phase passes through cleanly without dropping anything.
    unregistered = _synthetic_stage_context('scf')
    unregistered['phase'] = 'unregistered_stage'
    view = build_stage_review_view(unregistered, store.store)
    assert 'stage_review_view' not in view
    assert view['phase'] == 'unregistered_stage'

    # Registered future stage derives direct summaries and incars from depends_on safely.
    patched_defs = {
        **stage_definitions.DEFINITIONS,
        'phonon': {1: {'name': 'phonon', 'depends_on': ('scf',), 'summaries': ('phonon.json',)}},
    }
    monkeypatch.setattr(stage_definitions, 'DEFINITIONS', patched_defs)
    future = _synthetic_stage_context('dos')
    future['phase'] = 'phonon'
    future_view = build_stage_review_view(future, store.store)
    assert future_view['stage_review_view']['projected'] is True
    assert set(future_view['evidence']['summaries']) == {'scf-summary.json'}
    incars_node = future_view['inputs']['plan']['incars']
    incar_stages = set(incars_node['stages']) if isinstance(incars_node, dict) and 'stages' in incars_node else set(incars_node)
    assert incar_stages == {'scf'}


def test_realistic_five_phase_views_fit_well_under_24kb(tmp_path, monkeypatch) -> None:
    from vasp_agent.control.stage_review_view import build_stage_review_view

    structure = Structure(Lattice.cubic(5.43), ['Si'], [[0, 0, 0]])
    source = tmp_path / 'POSCAR'
    Poscar(structure).write_file(source)
    root = tmp_path / 'runs/si'
    full.prepare_full(NS(
        structure=str(source), workdir=str(root), potcar=None,
        encut=520, mesh=[4, 4, 4], nbands=16, relax_mode='cell', ediff=1e-7,
        points=40, symprec=.01, force_tol=.01, stress_tol=1., energy_tol=.001, nsw=200,
    ))
    (root / 'POTCAR').write_text('test fixture', encoding='utf-8')
    core.save(root / 'potential-selection.json', [{'element': 'Si', 'titel': 'PAW_PBE Si'}])
    manifest = json.loads((root / 'manifest.json').read_text(encoding='utf-8'))
    manifest['ready'] = True
    manifest['hashes']['POTCAR'] = core.digest(root / 'POTCAR')
    manifest['hashes']['potential-selection.json'] = core.digest(root / 'potential-selection.json')
    core.save(root / 'manifest.json', manifest)
    monkeypatch.setattr(full.Potcar, 'from_file', lambda p: [NS(element='Si')])
    gate.confirm_physics(root, 'nonmagnetic bulk silicon PBE bands')

    def fake_execute(job_root, name, command):
        folder = job_root / name
        st = Poscar.from_file(folder / 'POSCAR').structure
        if not (folder / 'complete.json').exists():
            (folder / 'vasprun.xml').write_text('mock output ' + name, encoding='utf-8')
            data = {'xml_sha256': core.digest(folder / 'vasprun.xml')}
            if name == 'scf':
                (folder / 'CHGCAR').write_text('mock charge', encoding='utf-8')
                data['charge_sha256'] = core.digest(folder / 'CHGCAR')
            core.save(folder / 'complete.json', data)
        n = len(st)
        forces = (np.arange(3 * n).reshape(n, 3) * 1e-3).tolist()
        return NS(
            final_structure=st, final_energy=-10. * n, converged_ionic=True,
            parameters={'NELM': 150, 'NBANDS': 16, 'NELECT': 8}, efermi=0.,
            ionic_steps=[{'forces': forces, 'stress': np.eye(3).tolist(), 'electronic_steps': [{}]}],
        )

    monkeypatch.setattr(core, 'execute', fake_execute)
    monkeypatch.setattr(full, 'analyze_full', lambda r: core.save(r / 'workflow-state.json', {'status': 'complete'}))

    box = ai.Toolbox(tmp_path)
    sizes: dict[str, int] = {}
    for phase in gate.PHASES:
        ctx = gate.context(root)
        view = build_stage_review_view(ctx, box.store_evidence)
        encoded = json.dumps(view, ensure_ascii=False, separators=(',', ':'), default=str).encode('utf-8')
        sizes[phase] = len(encoded)
        assert sizes[phase] < 12 * 1024, (phase, sizes[phase])
        assert _find_dangling_same_as(view) == []
        checks = [
            dict(item=item, judgment='pending' if item == 'validation_plan' else 'pass',
                 blocking=False, evidence='ok', reason='ok', next_action='无需')
            for item in ctx['required_checks']
        ]
        gate.record(root, ctx['snapshot'], phase, checks, 'checker-test')
        full.run_full(root, ['mock'])

    # Also verify a realistic DOS stage review view stays well under 12 KB.
    dos_ctx = _synthetic_stage_context('dos')
    dos_view = build_stage_review_view(dos_ctx, box.store_evidence)
    dos_bytes = len(json.dumps(dos_view, ensure_ascii=False, separators=(',', ':'), default=str).encode('utf-8'))
    assert dos_bytes < 12 * 1024, dos_bytes
    assert _find_dangling_same_as(dos_view) == []
