"""Versioned parameter specifications for stages whose inputs the operator chooses.

A stage definition names a spec ({'id', 'version'}); the spec says which fields
the operator decides, their types, units and allowed values, how a starting
suggestion is derived (with its basis), which fields depend on each other, and
how the chosen values become the stage's actual inputs. The flow is the same
for every workflow (DOS, Wannier, phonons):

    read the spec -> show the suggestions and their basis -> operator choice
    in the plan menu -> strict validation -> saved in config['stage_parameters']
    (hashed into the manifest, shown in every review snapshot) -> inputs generated.

Validation here is the authority: the engine runs it again in prepare_full
whatever the local plan said. Unknown fields are refused, and the only strings
accepted are listed choices, so no path, command or free-text configuration can
reach a task. A spec marked not executable can be read and previewed, never
saved into a plan or prepared.

Standard library only (the worker bundles the engine; see stage_definitions).
See docs/design/WORKFLOW_MODULES.md §4.
"""
import math

# Engine marker: engines with it accept optional stages with parameter specs.
PARAMETER_SPECS_VERSION = 1
# Bounds that keep a single DOS run reasonable; not physics thresholds.
MAX_NEDOS = 20001
MAX_POINT_GROUP_ORDER = 48
TETRAHEDRON_MIN_KPOINTS = 4


def _field(kind, label, *, unit=None, minimum=None, maximum=None, choices=None, nullable=False, description=''):
    if kind not in ('integer', 'number', 'choice'):
        raise ValueError('参数类型无效。')
    item = {'kind': kind, 'label': label, 'unit': unit, 'nullable': nullable, 'description': description}
    if kind == 'choice':
        item['choices'] = list(choices)
    else:
        item.update(minimum=minimum, maximum=maximum)
    return item


# ---------------------------------------------------------------- DOS, v1 (3D)

DOS_PURPOSE = ('绘图与投影 DOS 的能量窗口（相对参考能量）；不是电子数核验的积分范围——'
               '核验从最低能带之下积分，由引擎另行设定（步骤 C）。')


def _dos_suggest(context):
    three_d = context.get('dimension', '3D') == '3D'
    return {
        'kmesh_factor': (2, 'SCF 网格（标准原胞，按倒空间间距换算）每个周期方向乘 2 并向上取整。'
                            '加密倍数是初始策略，不证明 DOS 已收敛；报告须写明。'),
        'integration': ('tetrahedron', '三维规则网格用 Blöchl 修正四面体法（ISMEAR=-5）。前置条件：自动生成的 Γ/MP 规则网格，'
                                       '不可约 k 点数 ≥ 4（运行前按下界估计，运行后读实际数复核）。不满足即阻断，'
                                       '不静默改为 Gaussian；Gaussian 只能由操作者明确选择并确认 SIGMA。')
                        if three_d else
                        ('tetrahedron', '二维 DOS 尚未开放（真空方向网格与展宽规则待补齐）；此任务不能启用 DOS。'),
        'sigma_eV': (None, '四面体法不使用 SIGMA；仅当操作者选择 gaussian 时必须给出。'),
        'energy_reference': ('scf_fermi', '零点取 SCF 的 E_F（固定电荷密度的唯一自洽参考，与 bands.csv 一致）；'
                                          'DOS 计算自身的 E_F 及与 SCF 的差值另行记录，不要求相等。'),
        'window_min_eV': (-10.0, '相对 E_F 的窗口下限，覆盖常见价带；有半芯态或深能级时按需加宽。'),
        'window_max_eV': (5.0, '相对 E_F 的窗口上限；空带须覆盖它（见 empty_band_margin_eV）。'),
        'sampling_interval_eV': (0.01, '能量采样间隔：NEDOS = ceil((上限−下限)/间隔) + 1。'
                                       '这是采样间隔，不是分辨率；实际分辨率受 k 网格与积分方法限制。'),
        'empty_band_margin_eV': (1.0, '各 k 点最高能带须高于 E_F + 窗口上限 + 该余量；不足时增加 NBANDS，不照抄 bands 的值。'),
    }


def _dos_check(values, context):
    errors = []
    if context.get('dimension', '3D') != '3D':
        errors.append('二维 DOS 尚未开放；首版只支持三维体相。')
    if values['integration'] == 'gaussian' and values['sigma_eV'] is None:
        errors.append('integration=gaussian 必须由操作者给出 sigma_eV。')
    if values['integration'] == 'tetrahedron' and values['sigma_eV'] is not None:
        errors.append('integration=tetrahedron 不使用 sigma_eV，请设为 null。')
    if nedos(values) > MAX_NEDOS:
        errors.append(f'NEDOS={nedos(values)} 超过上限 {MAX_NEDOS}；请增大采样间隔或缩小窗口。')
    return errors


def nedos(values):
    """Grid points for the window at the chosen sampling interval (not a resolution)."""
    span = values['window_max_eV'] - values['window_min_eV']
    return math.ceil(round(span / values['sampling_interval_eV'], 9)) + 1


def kpoint_lower_bound(mesh):
    """Fewest irreducible k-points a regular mesh can reduce to (any point group)."""
    return max(1, -(-math.prod(mesh) // MAX_POINT_GROUP_ORDER))


def nbands_check(values, highest_band_minimum_eV):
    """Empty bands must cover the window: the lowest value of the highest band, relative to E_F."""
    required = values['window_max_eV'] + values['empty_band_margin_eV']
    covered = highest_band_minimum_eV is not None and highest_band_minimum_eV >= required
    return {'covered': covered, 'highest_band_minimum_eV': highest_band_minimum_eV, 'required_eV': required,
            'reference': 'scf_fermi',
            'action': None if covered else '增加 NBANDS（非自洽计算可多于 SCF）后重新生成；不能照抄 bands 的值。'}


def _dos_generate(values, evidence):
    """INCAR tags and KPOINTS for the DOS run from accepted SCF evidence (used by step C).

    evidence: dimension, scf_mesh, scf_kpoints_style ('Gamma'/'Monkhorst'),
    scf_fermi_eV, scf_nbands, highest_band_minimum_eV (relative to the SCF E_F).
    Returns {'incar', 'kpoints', 'derived', 'blocked'}; a non-empty 'blocked'
    means the run must not start.
    """
    blocked = list(_dos_check(values, evidence))
    mesh = [math.ceil(n * values['kmesh_factor']) for n in evidence['scf_mesh']]
    lower = kpoint_lower_bound(mesh)
    if values['integration'] == 'tetrahedron':
        if evidence.get('scf_kpoints_style') not in ('Gamma', 'Monkhorst'):
            blocked.append('四面体法需要自动生成的 Γ/MP 规则网格。')
        if lower < TETRAHEDRON_MIN_KPOINTS:
            blocked.append(f'网格 {mesh} 的不可约 k 点下界 {lower} < {TETRAHEDRON_MIN_KPOINTS}；'
                           '请增大 kmesh_factor，或由操作者明确选择 gaussian 并确认 sigma_eV。')
    fermi = evidence['scf_fermi_eV']
    bands = nbands_check(values, evidence.get('highest_band_minimum_eV'))
    if not bands['covered']:
        blocked.append('空带不足以覆盖窗口上限：' + bands['action'])
    incar = {'ICHARG': 11, 'LORBIT': 11, 'LWAVE': False, 'NSW': 0, 'IBRION': -1,
             'ISMEAR': -5 if values['integration'] == 'tetrahedron' else 0,
             'EMIN': round(fermi + values['window_min_eV'], 6), 'EMAX': round(fermi + values['window_max_eV'], 6),
             'NEDOS': nedos(values), 'NBANDS': evidence['scf_nbands']}
    if values['integration'] == 'gaussian':
        incar['SIGMA'] = values['sigma_eV']
    return {'incar': incar, 'kpoints': {'style': 'Gamma', 'mesh': mesh},
            'derived': {'energy_conversion': {'reference': 'scf_fermi', 'scf_fermi_eV': fermi,
                                              'note': 'VASP 5.4.4 的 EMIN/EMAX 是绝对能量，按 SCF E_F 换算。'},
                        'irreducible_kpoints_lower_bound': lower, 'nbands': bands,
                        'sampling_interval_eV': values['sampling_interval_eV'],
                        'sampling_note': '采样间隔，不是分辨率。'},
            'blocked': blocked}


DOS_V1 = {
    'id': 'dos', 'version': 1, 'stage': 'dos', 'guide': 'dos',
    'title': '三维态密度（固定 SCF 电荷的非自洽计算）',
    # Step C registers the dos stage and flips this; until then nothing can save or prepare it.
    'executable': False,
    'not_executable_reason': 'DOS 阶段在步骤 C 实现；当前只能查看参数规范，不能保存方案或准备任务。',
    'generate_at': 'stage_start',
    'fields': {
        'kmesh_factor': _field('integer', 'k 网格加密倍数', minimum=1, maximum=4,
                               description='SCF 网格每个周期方向的倍数（向上取整）。'),
        'integration': _field('choice', '积分方法', choices=('tetrahedron', 'gaussian'),
                              description='tetrahedron: ISMEAR=-5；gaussian: ISMEAR=0，须给 sigma_eV。'),
        'sigma_eV': _field('number', 'Gaussian 展宽', unit='eV', minimum=0.01, maximum=0.5, nullable=True),
        'energy_reference': _field('choice', '能量参考', choices=('scf_fermi',)),
        'window_min_eV': _field('number', '窗口下限（相对参考）', unit='eV', minimum=-50.0, maximum=-0.5),
        'window_max_eV': _field('number', '窗口上限（相对参考）', unit='eV', minimum=0.5, maximum=30.0),
        'sampling_interval_eV': _field('number', '能量采样间隔', unit='eV', minimum=0.001, maximum=0.1,
                                       description='决定 NEDOS；不是分辨率。'),
        'empty_band_margin_eV': _field('number', '空带余量', unit='eV', minimum=0.1, maximum=5.0),
    },
    'fixed': {'ICHARG': 11, 'LORBIT': 11, 'LWAVE': False},
    'derived': {'ISMEAR': 'tetrahedron → -5；gaussian → 0 且 SIGMA=sigma_eV',
                'EMIN/EMAX': 'SCF E_F + 窗口上下限（绝对能量），换算记录写入输入方案',
                'NEDOS': 'ceil((window_max − window_min)/sampling_interval) + 1',
                'NBANDS': '运行前按 SCF 占据审计检查空带覆盖窗口上限 + 余量；运行后按 DOS 计算复核',
                'KPOINTS': 'Γ 中心规则网格 = SCF 网格 × kmesh_factor'},
    'dependencies': ['integration=gaussian ⇒ sigma_eV 必填；tetrahedron ⇒ sigma_eV 为 null',
                     'NEDOS = ceil((window_max_eV − window_min_eV)/sampling_interval_eV) + 1 ≤ %d' % MAX_NEDOS,
                     '仅三维体相（二维 DOS 尚未开放）'],
    'purpose': DOS_PURPOSE,
    'records': ['SCF E_F（零点）', 'DOS 计算的 E_F 及差值（不要求相等）', '实际 k 点数与 ISMEAR'],
    'suggest': _dos_suggest, 'check': _dos_check, 'generate': _dos_generate,
}

SPECS = {'dos': {1: DOS_V1}}


# ------------------------------------------------------------- generic flow

def spec(spec_id, version=None):
    versions = SPECS.get(spec_id)
    if not versions or (version is not None and version not in versions):
        raise ValueError(f'本引擎没有参数规范 {spec_id}' + (f' v{version}' if version is not None else '') + '。')
    return versions[max(versions) if version is None else version]


def for_stage(stage):
    """The current spec for an optional stage, or ValueError when none exists."""
    for versions in SPECS.values():
        item = versions[max(versions)]
        if item['stage'] == stage:
            return item
    raise ValueError(f'阶段 {stage} 没有参数规范，不能作为可选阶段启用。')


def optional_stages():
    """Stages a new task can enable through a parameter spec (tool enum)."""
    return tuple(dict.fromkeys(versions[max(versions)]['stage'] for versions in SPECS.values()))


def reference(item):
    return {'id': item['id'], 'version': item['version']}


def suggestions(item, context):
    return {name: {'value': value, 'basis': basis} for name, (value, basis) in item['suggest'](context).items()}


def describe(item, context=None):
    """The full spec as data for tool results: fields, rules, suggestions and their basis."""
    data = {key: item[key] for key in ('id', 'version', 'stage', 'title', 'executable', 'generate_at', 'fields',
                                       'fixed', 'derived', 'dependencies', 'purpose', 'records', 'guide')}
    if not item['executable']:
        data['not_executable_reason'] = item['not_executable_reason']
    data['suggestions'] = suggestions(item, context or {})
    data['note'] = '建议是起点，不是收敛证明；操作者在 /plan 菜单中逐项确认或修改，执行端按本规范严格校验。'
    return data


def _value_error(name, field, value):
    if value is None:
        return None if field['nullable'] else f'{name} 不能为空。'
    if field['kind'] == 'choice':
        return None if value in field['choices'] and type(value) is type(field['choices'][0]) else (
            f"{name} 只能取 {'、'.join(map(str, field['choices']))}。")
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return f'{name} 必须是数值（不接受文本、路径或命令）。'
    if field['kind'] == 'integer' and not isinstance(value, int):
        return f'{name} 必须是整数。'
    if not math.isfinite(value) or not field['minimum'] <= value <= field['maximum']:
        unit = f" {field['unit']}" if field['unit'] else ''
        return f"{name} 须在 [{field['minimum']}, {field['maximum']}]{unit} 内。"
    return None


def validate(item, values, context=None, partial=False):
    """Strictly check values against the spec; returns them normalized, raises ValueError listing every problem.

    partial=True checks only the fields given (an AI suggestion before merging
    with the suggested values); the dependency rules need the full set.
    """
    if not isinstance(values, dict):
        raise ValueError('阶段参数必须是字段到值的映射。')
    unknown = sorted(set(values) - set(item['fields']), key=str)
    errors = [f"未知参数：{'、'.join(map(str, unknown))}（规范 {item['id']} v{item['version']} 只接受 "
              f"{'、'.join(item['fields'])}）。"] if unknown else []
    missing = [] if partial else [name for name in item['fields'] if name not in values]
    if missing:
        errors.append('缺少参数：' + '、'.join(missing) + '。')
    for name, value in values.items():
        if name in item['fields']:
            problem = _value_error(name, item['fields'][name], value)
            if problem:
                errors.append(problem)
    if not errors and not partial:
        errors += item['check'](values, context or {})
    if errors:
        raise ValueError(f"阶段参数（{item['id']} v{item['version']}）无效：" + '；'.join(errors))
    return {name: (float(value) if item['fields'][name]['kind'] == 'number' and value is not None else value)
            for name, value in values.items()}


def merged(item, context, overrides):
    """Suggested values with the given overrides applied (validated, full set)."""
    base = {name: entry['value'] for name, entry in suggestions(item, context).items()}
    return validate(item, {**base, **validate(item, overrides or {}, context, partial=True)}, context)


def record_entry(item, values, context):
    """config['stage_parameters'][stage]: the spec reference and the validated values."""
    return {'spec': reference(item), 'values': validate(item, values, context)}


def check_record(stage, entry, context):
    """Execution-side check of a recorded entry: known spec for this stage, executable, valid values."""
    if not isinstance(entry, dict) or set(entry) != {'spec', 'values'} or not isinstance(entry['spec'], dict) \
            or set(entry['spec']) != {'id', 'version'}:
        raise ValueError(f'阶段 {stage} 的参数记录格式无效。')
    item = spec(entry['spec']['id'], entry['spec']['version'])
    if item['stage'] != stage:
        raise ValueError(f"参数规范 {item['id']} 不属于阶段 {stage}。")
    if not item['executable']:
        raise ValueError(f"阶段 {stage} 尚不可执行：{item['not_executable_reason']}")
    return item, validate(item, entry['values'], context)


def plan_entry(item, values, evidence):
    """What the reviewed input plan records for the stage now.

    A spec generated at preparation gives its actual inputs; one generated at
    stage start (DOS: it needs the SCF E_F and grid) records the validated
    values and the rules, and the stage compares its generated inputs against them.
    """
    entry = {'spec': reference(item), 'values': values, 'generate_at': item['generate_at'],
             'fixed': item['fixed'], 'derived': item['derived']}
    if item['generate_at'] == 'prepare':
        generated = item['generate'](values, evidence)
        if generated.get('blocked'):
            raise ValueError('阶段输入生成被阻断：' + '；'.join(generated['blocked']))
        entry['generated'] = generated
    return entry
