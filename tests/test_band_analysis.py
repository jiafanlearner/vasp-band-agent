import hashlib
import json

import pytest

import vasp_agent.chat.ai_agent as ai
from vasp_agent.workflow.band_analysis import COLUMNS, analyze_bands

GAMMA, X, K = (0, 0, 0), (0.5, 0, 0.5), (0.375, 0.375, 0.75)


def path_points(steps=10, length=1.0):
    """Γ→X, then a discontinuity K→Γ; each entry is (distance, frac, t from Γ)."""
    points = []
    for i in range(steps + 1):
        t = i / steps
        points.append((t * length, tuple(g + (x - g) * t for g, x in zip(GAMMA, X)), t))
    for i in range(steps + 1):
        s = i / steps
        points.append((length + s * length, tuple(k + (g - k) * s for k, g in zip(K, GAMMA)), 1 - s))
    return points


def bands_csv(band_functions, points=None, drop=0):
    points = points or path_points()
    lines = [','.join(COLUMNS)]
    for band, energy in enumerate(band_functions, start=1):
        for k, (distance, frac, t) in enumerate(points):
            value = energy(t)
            lines.append(','.join(map(str, ['1', band, k, distance, *frac, value, value])))
    return '\n'.join(lines[:len(lines) - drop]) + '\n'


SEMICONDUCTOR = [lambda t: -10.0, lambda t: -3 * t, lambda t: -2 * t, lambda t: -t,
                 lambda t: 0.6 + 4 * (t - 0.85) ** 2, lambda t: 4.0, lambda t: 5.0, lambda t: 6.0]
METAL = [lambda t: -1 + 2 * t, lambda t: 0.5 + t, lambda t: 3.0]


def test_si_like_semiconductor_has_indirect_gap_and_gamma_degeneracy():
    result = analyze_bands(bands_csv(SEMICONDUCTOR), {'nelect': 8.0, 'nbands': 8, 'efermi': 5.1})
    assert result['data_shape']['rows_match'] and result['data_shape']['k_points'] == 22
    assert result['occupation']['occupied_bands'] == 4
    edges = result['band_edges']
    assert edges['vbm']['label'] == 'Γ' and edges['vbm']['energy_eV'] == 0.0
    assert edges['cbm']['label'] is None and edges['cbm']['energy_eV'] == pytest.approx(0.61)
    assert edges['gap_type'] == 'indirect' and edges['gap_eV'] == pytest.approx(0.61)
    assert edges['smallest_direct_gap']['gap_eV'] == pytest.approx(1.39)
    assert edges['smallest_direct_gap']['frac'] == pytest.approx([0.35, 0, 0.35])
    assert not result['metallic_check']['metallic_on_sampled_path']
    assert result['metallic_check']['fermi_crossing_count'] == 0
    assert result['highest_band_check']['fully_above_ef']
    gamma = result['gamma_degeneracy']['gamma_points']
    assert [g['k_index'] for g in gamma] == [0, 21]
    assert {'bands': [2, 3, 4], 'degeneracy': 3, 'energy_eV': 0.0} in gamma[0]['groups']
    path = result['path']
    assert path['segment_count'] == 2 and path['breaks'] == [{'k_index': 11, 'kind': 'discontinuity'}]
    assert path['segments'][0]['start_label'] == 'Γ' and path['segments'][1]['end_label'] == 'Γ'
    assert 'k_table' not in result and result['flags'] == []
    assert any('PBE' in line for line in result['limitations'])


def test_metal_counts_fermi_crossings():
    result = analyze_bands(bands_csv(METAL), {'nelect': 2.0, 'nbands': 3})
    metal = result['metallic_check']
    assert metal['metallic_on_sampled_path'] and metal['metallic_by_occupation']
    assert metal['fermi_crossing_count'] == 2 and metal['bands_crossing_scf_ef'] == {'1': [1]}
    assert result['band_edges']['gap_eV'] == 0.0 and result['band_edges']['band_overlap_eV'] > 0


def test_odd_electron_count_is_flagged_not_guessed():
    result = analyze_bands(bands_csv(SEMICONDUCTOR), {'nelect': 7.0, 'nbands': 8})
    assert result['occupation']['occupied_bands'] is None and 'band_edges' not in result
    assert any('NELECT' in flag for flag in result['flags'])


def test_k_table_is_paged_and_bounded():
    result = analyze_bands(bands_csv(SEMICONDUCTOR), {'nelect': 8.0}, table_offset=20, table_limit=200)
    table = result['k_table']
    assert [row['k_index'] for row in table['rows']] == [20, 21] and table['next_offset'] is None
    first = analyze_bands(bands_csv(SEMICONDUCTOR), {'nelect': 8.0}, table_limit=5)['k_table']
    assert len(first['rows']) == 5 and first['next_offset'] == 5
    assert first['rows'][0]['direct_gap_eV'] == pytest.approx(3.49)


def write_download(workspace, text, scf=None):
    folder = workspace / 'downloads/eshell/si-scan-01-8d6e0213'
    folder.mkdir(parents=True)
    files = {'bands.csv': text.encode(), 'scf-summary.json': json.dumps(scf or {'nelect': 8.0, 'nbands': 8, 'efermi': 5.1}).encode()}
    for name, data in files.items():
        (folder / name).write_bytes(data)
    (folder / 'download-receipt.json').write_text(json.dumps({'files': {
        name: {'sha256': hashlib.sha256(data).hexdigest(), 'verified': True} for name, data in files.items()}}))
    return folder


def test_tool_reads_receipt_verified_download(tmp_path):
    write_download(tmp_path, bands_csv(SEMICONDUCTOR))
    box = ai.Toolbox(tmp_path)
    result = box.call('analyze_band_results', {'server': 'eshell', 'job_name': 'si-scan-01',
                                               'table_offset': 0, 'table_limit': 0})
    assert result['ok'], result
    assert result['result']['source']['receipt_verified']
    assert set(result['result']['source']['files_sha256']) == {'bands.csv', 'scf-summary.json'}
    assert result['result']['band_edges']['gap_type'] == 'indirect'
    assert not (tmp_path / 'workbench').exists()


def test_tool_refuses_tampered_download(tmp_path):
    folder = write_download(tmp_path, bands_csv(SEMICONDUCTOR))
    (folder / 'bands.csv').write_text(bands_csv(METAL))
    result = ai.Toolbox(tmp_path).call('analyze_band_results', {'server': 'eshell', 'job_name': 'si-scan-01',
                                                                 'table_offset': 0, 'table_limit': 0})
    assert not result['ok'] and result['error_code'] == 'INPUT_HASH_MISMATCH'


def test_tool_refuses_wrong_row_count(tmp_path):
    write_download(tmp_path, bands_csv(SEMICONDUCTOR, drop=1))
    result = ai.Toolbox(tmp_path).call('analyze_band_results', {'server': 'eshell', 'job_name': 'si-scan-01',
                                                                 'table_offset': 0, 'table_limit': 0})
    assert not result['ok'] and result['error_code'] == 'BAND_DATA_INVALID'
    assert '行数' in result['error']


def test_tool_reads_local_job(tmp_path):
    folder = tmp_path / 'runs/si-local'
    folder.mkdir(parents=True)
    (folder / 'bands.csv').write_text(bands_csv(METAL), encoding='utf-8')
    result = ai.Toolbox(tmp_path).call('analyze_band_results', {'server': None, 'job_name': 'si-local',
                                                                 'table_offset': 0, 'table_limit': 3})
    assert result['ok'], result
    data = result['result']
    assert data['source']['kind'] == 'local' and not data['source']['receipt_verified']
    assert data['metallic_check']['metallic_on_sampled_path'] and data['occupation']['occupied_bands'] is None
    assert len(data['k_table']['rows']) == 3


def test_tool_is_registered_and_prompted():
    schema = next(t for t in ai.TOOLS if t['name'] == 'analyze_band_results')
    assert schema['parameters']['properties']['table_limit']['maximum'] == 200
    assert 'analyze_band_results' in ai.INSTRUCTIONS
