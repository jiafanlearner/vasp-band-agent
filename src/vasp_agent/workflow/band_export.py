"""Band-data validation and publication export (CSV, plot, JSON report)."""
import csv

import numpy as np

import vasp_agent.workflow.vasp_band_agent as core


def validate_band_data(result):
    """Validate every sampled eigenvalue/occupation, not a tail of OUTCAR."""
    arrays = list(result.eigenvalues.values()) if result.eigenvalues else []
    if not arrays:
        raise ValueError('缺少全路径本征值及占据数据。')
    shapes = set()
    for values in arrays:
        values = np.asarray(values)
        if values.ndim != 3 or values.shape[2] != 2 or not np.isfinite(values).all():
            raise ValueError('能带数据维度无效或包含非有限值。')
        shapes.add(values.shape[:2])
        if np.any(values[:, :, 1] < -1e-6) or np.any(values[:, :, 1] > 2 + 1e-6):
            raise ValueError('能带占据超出有效范围。')
        if np.any(values[:, -1, 1] > 1e-6):
            raise ValueError('部分路径点最高能带仍被占据，请增加 NBANDS。')
    if len(shapes) != 1:
        raise ValueError('自旋通道路径尺寸不一致。')
    nk, nb = shapes.pop()
    if nk == 0 or nb == 0:
        raise ValueError('能带数据为空。')
    return {'passed': True, 'kpoints': nk, 'bands': nb, 'spin_channels': len(arrays),
            'all_values_finite': True, 'highest_band_empty_at_all_kpoints': True,
            'note': '顶部空带检查不保证覆盖任意指定的高能量窗口。'}


def export_bands(bs, root, metadata):
    from vasp_agent.workflow.band_plot import branch_ticks, plot_bands, plot_title, save_figure
    metallic = bool(bs.is_metal())
    reference = float(bs.efermi if metallic else bs.get_vbm()['energy'])
    ticks, breaks = branch_ticks(bs)
    energies = {str(spin): values - reference for spin, values in bs.bands.items()}
    figure = plot_bands(bs.distance, energies, ticks, breaks, 'scf_fermi' if metallic else 'vbm',
                        plot_title(metadata.get('formula', ''), 'PBE', bool(bs.is_spin_polarized)))
    save_figure(figure, root / 'bands.png', root / 'bands.pdf')
    with (root / 'bands.csv').open('w', newline='', encoding='utf-8') as out:
        writer = csv.writer(out)
        # k_label and path_source are appended after the original columns: the
        # vertex label (empty between vertices) and automatic/operator path.
        writer.writerow(['spin', 'band', 'k_index', 'distance_inv_angstrom',
                         'kx_fractional', 'ky_fractional', 'kz_fractional', 'energy_minus_scf_ef_eV', 'energy_minus_plot_reference_eV',
                         'k_label', 'path_source'])
        vertex = [''] * len(bs.kpoints)
        for branch in bs.branches:
            for index in (branch['start_index'], branch['end_index']):
                vertex[index] = bs.kpoints[index].label or ''
        source = (metadata.get('band_path') or {}).get('source', 'automatic')
        for spin, values in bs.bands.items():
            for band, band_energies in enumerate(values):
                for k, energy in enumerate(band_energies):
                    writer.writerow([str(spin), band + 1, k, bs.distance[k],
                                     *bs.kpoints[k].frac_coords, float(energy - bs.efermi), float(energy - reference),
                                     vertex[k], source])
    report = dict(metadata, scf_fermi_energy_eV=float(bs.efermi), band_path_source=source,
                  plot_energy_reference={'kind': 'scf_fermi' if metallic else 'vbm', 'energy_eV': reference},
                  metallic_on_sampled_path=bool(bs.is_metal()),
                  sampled_path_gap=bs.get_band_gap(),
                  limitations=['Path extrema may miss the true global band edges.',
                               'PBE Kohn-Sham gap is not an experimental quasiparticle gap.',
                               'Geometry and parameter convergence require separate validation.'])
    core.save(root / 'report.json', report)
    return report
