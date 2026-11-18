import json
from types import SimpleNamespace as NS

import pytest
from pymatgen.core import Lattice, Structure
from pymatgen.io.vasp.inputs import Poscar
from vasp_agent.chat.ai_agent import Toolbox
from vasp_agent.workflow import potential_library as lib
from vasp_agent.planning import parameter_plans as plans
from test_parameter_plans import request


@pytest.fixture
def setup(tmp_path, monkeypatch):
    library = tmp_path / 'library'
    for variant in ('Si', 'Si_pv'):
        folder = library / variant
        folder.mkdir(parents=True)
        (folder / 'POTCAR').write_text(variant + '\n')
    def parse(text):
        return [NS(element='Si', functional='PBE', TITEL=text.strip(),
                   enmax=400., ZVAL=12 if 'pv' in text else 4)]
    monkeypatch.setattr(lib.Potcar, 'from_str', parse)
    monkeypatch.setattr(lib.Potcar, 'from_file', lambda p: parse(p.read_text()))
    Poscar(Structure(Lattice.cubic(5.43), ['Si'], [[0,0,0]])).write_file(tmp_path / 'POSCAR')
    box = Toolbox(tmp_path, potcar_library=library)
    return box, library


def test_explicit_selection_prepared_and_metadata_bound(setup):
    box, library = setup
    req = {**request(), 'potential_variants': [{'element':'Si','variant':'Si_pv'}]}
    preview = box.call('preview_scan_defaults', req)
    assert preview['ok'], preview
    assert preview['result']['potential_selection'][0]['zval'] == 12
    assert preview['result']['band_capacity']['minimum_occupied_bands'] == 6
    draft = plans.propose(box, req, None, None, 'tool_default', '')
    plans.prepare(box, draft['id'])
    target = box.job(req['job_name'])
    assert (target / 'POTCAR').read_bytes() == (library / 'Si_pv/POTCAR').read_bytes()
    record = json.loads((target / 'potential-selection.json').read_text())[0]
    assert record['variant'] == 'Si_pv' and record['zval'] == 12
    assert 'potential-selection.json' in json.loads((target / 'manifest.json').read_text())['hashes']
    assert not (library / 'potentials.toml').exists()


def test_bands_insufficient_and_changed_potential_conflict(setup):
    box, library = setup
    req = {**request(), 'potential_variants':[{'element':'Si','variant':'Si_pv'}]}
    with pytest.raises(ValueError, match='NBANDS'):
        plans.preview_defaults(box, {**req, 'nbands': 2})
    draft = plans.propose(box, req, None, None, 'tool_default', '')
    (library / 'Si_pv/POTCAR').write_text('Si_pv changed\n')
    with pytest.raises(ValueError, match='已变化'):
        plans.prepare(box, draft['id'])
    assert not box.job(req['job_name']).exists()


@pytest.mark.parametrize('variants', [
    [{'element':'Cu','variant':'Cu_pv'}],
    [{'element':'Si','variant':'../Si'}],
    [{'element':'Si','variant':'Cu_pv'}],
    [{'element':'Si','variant':'Si'}, {'element':'Si','variant':'Si_pv'}],
    [{'element':'Si','variant':'Si_missing'}],
])
def test_invalid_variants_rejected(setup, variants):
    _, library = setup
    with pytest.raises(ValueError):
        lib.select_potentials(library, ['Si'], variants)


def test_remote_plan_forwards_variants(setup, monkeypatch):
    from vasp_agent.remote.remote_compute import RemoteCompute
    box, _ = setup
    monkeypatch.setattr(RemoteCompute, '__init__', lambda *a: None)
    calls = []
    monkeypatch.setattr(RemoteCompute, 'prepare', lambda self, *a, **kw: calls.append(kw))
    req = {**request(), 'server':'test', 'potential_variants':[{'element':'Si','variant':'Si_pv'}]}
    plans.invoke(box, req, preview=True)
    assert calls[0]['potential_variants'] == req['potential_variants']
    assert calls[0]['preview_only']
