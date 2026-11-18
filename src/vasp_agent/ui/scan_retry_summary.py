"""Compact operator preview; never change the calculation evidence or request."""
import json

from vasp_agent.ui.terminal_ui import display_safe


def _text(value, maximum=180):
    text = display_safe(str(value)).replace('\n', ' ').replace('\r', ' ')
    return text if len(text) <= maximum else text[:maximum - 1] + '…'


def _sequence(values, mesh=False):
    if not isinstance(values, list) or not values:
        return '沿用来源序列（待核验）'
    shown = []
    for value in values[:3]:
        if mesh and isinstance(value, list) and len(value) == 3:
            shown.append('×'.join(_text(item, 12) for item in value))
        else:
            shown.append(_text(json.dumps(value, ensure_ascii=False), 45))
    return ' → '.join(shown) + (' → …' if len(values) > 3 else '')


ACTIVE_SCANS = {'both': ['encut', 'mesh'], 'encut': ['encut'], 'mesh': ['mesh']}


def planned_tests(mode, fixed_encut=None):
    """Same rule as full_workflow.convergence_tests: 3 per active scan, +1 cross-cutoff."""
    return 3 * len(ACTIVE_SCANS.get(mode, [])) + (1 if fixed_encut is not None and mode == 'mesh' else 0)


# Defaults used by the agent's prepare paths: energy_tol, force_tol/2, stress_tol/2.
DEFAULT_SCAN_THRESHOLDS = {'energy_eV_per_atom': 0.001, 'force_eV_per_A': 0.005, 'stress_kbar': 0.5}
THRESHOLD_LABELS = {'energy_eV_per_atom': ('能量差', 'eV/atom'), 'force_eV_per_A': ('力差', 'eV/Å'),
                    'stress_kbar': ('应力差', 'kbar')}


PRESET_NAMES = {'standard': '标准', 'strict': '严格'}


def preset_thresholds(preset):
    """Effective scan thresholds of a preset with the agent's default tolerances."""
    from vasp_agent.workflow.full_workflow import SCAN_PRESETS
    return {**DEFAULT_SCAN_THRESHOLDS, **SCAN_PRESETS.get(preset or 'strict', {})}


def preset_line(preset):
    values = preset_thresholds(preset)
    use = '能带、Wannier、态密度等电子结构' if preset == 'standard' else '声子、弹性、精确结构参数'
    return (f"判据档位：{PRESET_NAMES.get(preset or 'strict')}（能量差 {values['energy_eV_per_atom']:g} eV/atom、"
            f"力差 {values['force_eV_per_A']:g} eV/Å、应力差 {values['stress_kbar']:g} kbar 仅 cell 模式），适合{use}")


def threshold_lines(scan_thresholds, preset=None):
    """Operator-facing description of changed scan thresholds and what they mean."""
    changed = {k: v for k, v in (scan_thresholds or {}).items() if v is not None}
    if not changed:
        return []
    base = preset_thresholds(preset)
    parts = [f'{THRESHOLD_LABELS[k][0]} {base[k]:g} → {v:g} {THRESHOLD_LABELS[k][1]}'
             for k, v in changed.items() if k in THRESHOLD_LABELS]
    lines = ['扫描阈值（操作者放宽，记入任务配置与报告）：' + '；'.join(parts) + '；其余维持默认。']
    if 'stress_kbar' in changed:
        lines.append('影响估算：应力差 1 kbar 约对应体积误差 δV/V≈δP/B，对体弹模量 100–200 GPa 的固体约 0.05–0.1%。')
    if 'energy_eV_per_atom' in changed or 'force_eV_per_A' in changed:
        lines.append('能量/力阈值放宽会直接降低总能与几何精度；请确认与研究目标相符。')
    lines.append('弛豫完成标准（EDIFFG、应力上限）不变；复用的来源证据按新阈值重新判定。')
    return lines


def scan_retry_summary(server, source_job, job_name, mode, evidence, encuts, meshes, fixed_encut=None, scan_thresholds=None,
                       scan_preset=None):
    parameters = evidence.get('parameters', {})
    scans = {kind: parameters.get(kind, {}) for kind in ('encut', 'mesh')}
    statuses = {'passed': '已通过', 'failed': '未通过', 'unavailable': '无法核验'}
    lines = [f'选择性重扫：{_text(server or "local", 64)} / {_text(source_job, 64)} → {_text(job_name, 64)}',
             '来源收敛：' + '；'.join(f'{label} {statuses.get(scans[kind].get("status"), "状态未知")}'
                                  for kind, label in (('encut', 'ENCUT'), ('mesh', 'k 网格')))]
    if mode in ('both', 'encut'):
        values = encuts if encuts is not None else scans['encut'].get('values')
        lines.append('计算 ENCUT：' + _sequence(values) + ' eV')
    if mode in ('both', 'mesh'):
        values = meshes if meshes is not None else scans['mesh'].get('values')
        lines.append('计算 k 网格：' + _sequence(values, mesh=True))
    if mode == 'mesh':
        source_values = scans['encut'].get('values')
        highest = source_values[-1] if isinstance(source_values, list) and source_values else None
        fixed = fixed_encut if fixed_encut is not None else highest
        lines.append('复用 ENCUT：固定 ' + (_text(fixed, 30) + ' eV' if fixed is not None else '来源最高已测试值（待核验）'))
        lines.append(f'执行 {planned_tests(mode, fixed_encut)} 项测试：3 项网格扫描 + 1 项最高 ENCUT 交叉复核；复核不通过禁止优化。'
                     if fixed_encut is not None else f'执行 {planned_tests(mode)} 项网格测试；复用证据仅适用于来源条件。')
    elif mode == 'encut':
        source_values = scans['mesh'].get('values')
        fixed = [source_values[-1]] if isinstance(source_values, list) and source_values else None
        lines.append('复用 k 网格：固定 ' + _sequence(fixed, mesh=True))
        lines.append(f'执行 {planned_tests(mode)} 项 ENCUT 测试；复用证据仅适用于来源条件。')
    elif mode == 'both':
        lines.append(f'执行 {planned_tests(mode)} 项测试：ENCUT 与 k 网格各 3 项。')
    if fixed_encut is not None and mode != 'mesh':
        lines.append('固定 ENCUT 仅适用于 mesh 模式，当前组合须由程序核验。')
    for kind, label in (('encut', 'ENCUT'), ('mesh', 'k 网格')):
        if scans[kind].get('status') == 'unavailable' and scans[kind].get('reason'):
            lines.append(f'{label} 核验说明：' + _text(scans[kind]['reason'], 100))
    if scan_preset:
        lines.append(preset_line(scan_preset) + '；复用的来源证据按此档重新判定。')
    else:
        lines.append('判据档位：沿用来源任务的档位（未设档位的旧任务为严格档）。')
    lines.extend(threshold_lines(scan_thresholds, scan_preset))
    lines.append('确认后仅准备新目录，不提交；新任务仍需物理确认和阶段审阅。')
    lines.append('详细收敛证据可用 inspect_convergence 查看，工具执行详情见 /trace。')
    return '\n'.join(lines)
