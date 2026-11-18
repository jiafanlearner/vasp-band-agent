import json
from types import SimpleNamespace

import pytest
from pymatgen.core import Structure, Lattice
from pymatgen.io.vasp.inputs import Incar, Poscar, Kpoints

import vasp_agent.workflow.full_workflow as full
import vasp_agent.workflow.input_review as review
import vasp_agent.workflow.vasp_band_agent as core
from vasp_agent.chat.ai_agent import Toolbox


@pytest.fixture
def prepared(tmp_path):
    source = tmp_path / 'POSCAR'
    Poscar(Structure(Lattice.cubic(5.43), ['Si'], [[0, 0, 0]])).write_file(source)
    root = tmp_path / 'runs/si'
    full.prepare_full(SimpleNamespace(stage_gates=False, structure=str(source), workdir=str(root), potcar=None,
        encut=520, mesh=[4, 4, 4], nbands=16, relax_mode='cell', ediff=1e-7,
        points=40, symprec=.01, force_tol=.01, stress_tol=1., energy_tol=.001, nsw=200))
    return root


def test_annotations_roundtrip_and_actual_input_bundle(prepared):
    data = review.context(prepared)
    assert len(data['plan']['incars']) == 13
    assert len(data['initial_inputs']) == 12
    for name, values in data['plan']['incars'].items():
        assert dict(Incar.from_str(review.annotated(values))) == dict(Incar(values))
    assert '待验证' in (prepared / 'PARAMETERS.md').read_text(encoding='utf-8')
    assert not (prepared / 'bands/POSCAR').exists()


def test_review_gate_blocked_pending_and_stale(prepared):
    with pytest.raises(ValueError, match='尚未完成'):
        review.require_review(prepared)
    snapshot = review.context(prepared)['snapshot']
    review.record(prepared, snapshot, 'blocked', '物理假设未确认', 'test-model')
    with pytest.raises(ValueError, match='阻断'):
        review.require_review(prepared)
    review.record(prepared, snapshot, 'needs_validation', '仅允许先执行收敛验证', 'test-model')
    review.require_review(prepared)
    assert len(list((prepared / 'reviews').glob('*.json'))) == 2
    (prepared / 'submit.slurm').write_text('# new submission resource configuration')
    with pytest.raises(ValueError, match='失效'):
        review.require_review(prepared)
    with pytest.raises(ValueError, match='快照'):
        review.record(prepared, snapshot, 'pass', 'old review', 'test-model')


def test_changed_actual_incar_is_rejected(prepared):
    path = prepared / 'convergence/encut-0/INCAR'
    path.write_text(path.read_text() + '\nNBANDS = 99\n')
    with pytest.raises(ValueError, match='输入已改变'):
        review.context(prepared)


def test_model_must_read_before_record(prepared):
    box = Toolbox(prepared.parents[1])
    snapshot = review.context(prepared)['snapshot']
    with pytest.raises(ValueError, match='先调用'):
        box.record_input_review(None, 'si', snapshot, 'pass', 'review')
    box.get_input_review(None, 'si')
    box.review_model = 'actual-model'
    result = box.record_input_review(None, 'si', snapshot, 'needs_validation', '参数来源与验证说明')
    assert result['reviewer'] == 'actual-model'


def test_runtime_incar_cannot_deviate_from_reviewed_plan(prepared, monkeypatch):
    cfg = json.loads((prepared / 'config.json').read_text())
    incar = full.base_incar(cfg, cfg['encuts'][-1])
    incar['NBANDS'] = 99
    monkeypatch.setattr(core, 'execute', lambda *a: pytest.fail('must not run'))
    with pytest.raises(ValueError, match='偏离'):
        full.stage(prepared, 'validation/reference', Poscar.from_file(prepared / 'initial.POSCAR').structure,
                   incar, Kpoints.gamma_automatic([6, 6, 6]), ['fake'])
