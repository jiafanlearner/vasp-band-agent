from types import SimpleNamespace as NS

import pytest

import vasp_agent.workflow.potential_library as lib


def put(root, variant, content):
    path = root / variant / 'POTCAR'
    path.parent.mkdir(parents=True)
    path.write_bytes(content.encode('utf-8'))


def test_order_variant_mapping_and_hash(tmp_path, monkeypatch):
    put(tmp_path, 'O', 'O\n')
    put(tmp_path, 'Fe_pv', 'Fe\n')
    (tmp_path / 'potentials.toml').write_text('[potentials]\nFe="Fe_pv"\n')
    monkeypatch.setattr(lib.Potcar, 'from_str', lambda text: [NS(element=text.strip(), functional='PBE',
                        TITEL='test fixture', enmax=400)])
    data, records = lib.assemble_potentials(tmp_path, ['Fe', 'O'])
    assert data == b'Fe\nO\n'
    assert [r['variant'] for r in records] == ['Fe_pv', 'O']
    assert all(len(r['sha256']) == 64 for r in records)


def test_no_silent_variant_fallback(tmp_path):
    put(tmp_path, 'Fe_pv', 'synthetic')
    with pytest.raises(ValueError, match='明确指定'):
        lib.select_potentials(tmp_path, ['Fe'])


def test_mapping_cannot_escape_library(tmp_path):
    (tmp_path / 'potentials.toml').write_text('[potentials]\nFe="../../outside"\n')
    with pytest.raises(ValueError, match='无效'):
        lib.select_potentials(tmp_path, ['Fe'])


def test_wrong_functional_rejected(tmp_path, monkeypatch):
    put(tmp_path, 'Si', 'synthetic')
    monkeypatch.setattr(lib.Potcar, 'from_str', lambda text: [NS(element='Si', functional='LDA')])
    with pytest.raises(ValueError, match='有效 PBE'):
        lib.assemble_potentials(tmp_path, ['Si'])
