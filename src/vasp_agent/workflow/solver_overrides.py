"""Deterministic Tier 3 numerical solver INCAR overrides (Layer 1, stdlib only).

Only pure numerical convergence/solver parameters are allowlisted here. Physical
model, smearing, cutoff, k-mesh, DOS window, and MPI execution tags stay hard-locked
outside this module. Free-text reasons are stored in config/plan/review diffs only
and never interpolated into INCAR line comments so scf_reuse.check_reproducible()
remains byte-for-byte deterministic.
"""
import math

SOLVER_OVERRIDES_VERSION = 1
VALID_PHASES = ('convergence', 'relaxation', 'scf', 'bands', 'dos')
MAX_REASON_LEN = 500

ELECTRONIC_TAGS = frozenset({'ALGO', 'AMIX', 'BMIX', 'NELM', 'NELMIN', 'MAXMIX', 'SYMPREC'})
IONIC_TAGS = frozenset({'IBRION', 'POTIM'})
ALLOWED_TAGS = ELECTRONIC_TAGS | IONIC_TAGS
TAG_ORDER = ('ALGO', 'AMIX', 'BMIX', 'NELM', 'NELMIN', 'MAXMIX', 'SYMPREC', 'IBRION', 'POTIM')

ALLOWED_ALGO = ('Normal', 'Fast', 'VeryFast', 'All', 'Damped')
ALLOWED_IBRION = (1, 2, 3)

MIN_NELM = 40
MAX_NELM = 300
DEFAULT_NELM = 150
MIN_NELMIN = 2
MAX_NELMIN = 12
MIN_MAXMIX = 20
MAX_MAXMIX = 80
MAX_AMIX = 1.0
MAX_BMIX = 10.0
MIN_SYMPREC = 1e-8
MAX_SYMPREC = 1e-3
MAX_POTIM = 2.0

RELAX_IONIC_STAGES = ('relax-1', 'relax-2')
RELAX_ALL_STAGES = (
    'relax-1',
    'relax-2',
    'validation/reference',
    'validation/higher-cutoff',
    'validation/denser-mesh',
)

# Static deterministic INCAR comments for newly exposed Tier 3 tags only.
# Existing keys (ALGO, NELM, IBRION, POTIM) keep their historical input_review.NOTES text.
SOLVER_NOTES = {
    'AMIX': ('solver override', 'Linear mixing parameter for electronic convergence'),
    'BMIX': ('solver override', 'Kerker mixing cutoff wavevector for electronic convergence'),
    'NELMIN': ('solver override', 'Minimum electronic steps per ionic step'),
    'MAXMIX': ('solver override', 'Maximum steps stored in Broyden mixer'),
    'SYMPREC': ('solver override', 'VASP internal symmetry tolerance (INCAR only)'),
}

_BASELINES = {
    'ALGO': 'Normal',
    'AMIX': None,
    'BMIX': None,
    'NELM': DEFAULT_NELM,
    'NELMIN': None,
    'MAXMIX': None,
    'SYMPREC': None,
    'IBRION': 2,
    'POTIM': 0.5,
}


def baseline_for(phase, tag):
    """Default value of an allowlisted Tier 3 tag in phase (None = VASP default omitted)."""
    if tag not in ALLOWED_TAGS:
        raise ValueError(f'未知的 Tier ③ 求解器标签：{tag}。')
    if tag in IONIC_TAGS and phase != 'relaxation':
        return None
    return _BASELINES[tag]


def applies_to_for(phase, tag):
    """Stage folders a Tier 3 tag affects within phase."""
    if phase == 'relaxation':
        return list(RELAX_IONIC_STAGES if tag in IONIC_TAGS else RELAX_ALL_STAGES)
    return [phase]


def _check_finite_number(tag, value):
    if type(value) not in (int, float) or not math.isfinite(value):
        raise ValueError(f'{tag} 必须为有限数值（禁止布尔值或 NaN/Inf）。')
    num = float(value)
    return 0.0 if num == 0.0 else num


def _validate_tag_value(phase, tag, value, max_nelm):
    if tag in IONIC_TAGS and phase != 'relaxation':
        raise ValueError(f'{tag} 仅允许用于 relaxation 阶段（仅作用于 relax-1/relax-2），不允许用于 {phase} 阶段。')
    if tag == 'ALGO':
        if type(value) is not str or value not in ALLOWED_ALGO:
            raise ValueError(f'ALGO 必须为 {" / ".join(ALLOWED_ALGO)} 之一。')
        return value
    if tag == 'NELM':
        if type(value) is not int or not MIN_NELM <= value <= max_nelm:
            raise ValueError(f'NELM 必须为 {MIN_NELM}–{max_nelm} 之间的整数（禁止布尔值）。')
        return value
    if tag == 'NELMIN':
        if type(value) is not int or not MIN_NELMIN <= value <= MAX_NELMIN:
            raise ValueError(f'NELMIN 必须为 {MIN_NELMIN}–{MAX_NELMIN} 之间的整数（禁止布尔值）。')
        return value
    if tag == 'MAXMIX':
        if type(value) is not int or not MIN_MAXMIX <= value <= MAX_MAXMIX:
            raise ValueError(f'MAXMIX 必须为 {MIN_MAXMIX}–{MAX_MAXMIX} 之间的整数（禁止布尔值）。')
        return value
    if tag == 'IBRION':
        if type(value) is not int or value not in ALLOWED_IBRION:
            raise ValueError('IBRION 必须为 1、2 或 3（禁止布尔值）。')
        return value
    if tag == 'AMIX':
        num = _check_finite_number(tag, value)
        if not 0.0 < num <= MAX_AMIX:
            raise ValueError(f'AMIX 必须满足 0 < AMIX <= {MAX_AMIX}。')
        return num
    if tag == 'BMIX':
        num = _check_finite_number(tag, value)
        if not 0.0 <= num <= MAX_BMIX:
            raise ValueError(f'BMIX 必须满足 0 <= BMIX <= {MAX_BMIX}。')
        return num
    if tag == 'SYMPREC':
        num = _check_finite_number(tag, value)
        if not MIN_SYMPREC <= num <= MAX_SYMPREC:
            raise ValueError(f'SYMPREC 必须满足 {MIN_SYMPREC:g} <= SYMPREC <= {MAX_SYMPREC:g}。')
        return num
    if tag == 'POTIM':
        num = _check_finite_number(tag, value)
        if not 0.0 < num <= MAX_POTIM:
            raise ValueError(f'POTIM 必须满足 0 < POTIM <= {MAX_POTIM}。')
        return num
    raise ValueError(f'不允许通过 solver_overrides 修改 INCAR 标签 {tag}。')


def _validate_reason(phase, reason):
    if type(reason) is not str:
        raise ValueError(f'阶段 {phase} 的 solver_overrides.reason 必须为字符串。')
    if any(ord(ch) < 32 or ord(ch) == 127 for ch in reason):
        raise ValueError(f'阶段 {phase} 的 solver_overrides.reason 不得包含控制字符。')
    cleaned = reason.strip()
    if not 1 <= len(cleaned) <= MAX_REASON_LEN:
        raise ValueError(f'阶段 {phase} 的 solver_overrides.reason 长度必须为 1–{MAX_REASON_LEN} 字符。')
    return cleaned


def validate(raw, allowed_phases=None, *, max_nelm=MAX_NELM):
    """Validate and canonicalize a Tier 3 solver_overrides payload, or return None."""
    if raw is None:
        return None
    if type(max_nelm) is not int or not MIN_NELM <= max_nelm <= MAX_NELM:
        raise ValueError(f'max_nelm 上限必须为 {MIN_NELM}–{MAX_NELM} 之间的整数。')
    if not isinstance(raw, dict):
        raise ValueError('solver_overrides 必须是对象。')
    extra_top = set(raw) - {'version', 'stages'}
    if extra_top:
        raise ValueError(f'solver_overrides 包含未允许字段：{", ".join(sorted(extra_top))}。')
    version = raw.get('version', SOLVER_OVERRIDES_VERSION)
    if type(version) is not int or version != SOLVER_OVERRIDES_VERSION:
        raise ValueError(f'solver_overrides.version 必须为整数 {SOLVER_OVERRIDES_VERSION}。')
    raw_stages = raw.get('stages')
    if not isinstance(raw_stages, dict) or not raw_stages:
        raise ValueError('solver_overrides.stages 必须为非空对象。')
    allowed = tuple(VALID_PHASES) if allowed_phases is None else tuple(allowed_phases)
    allowed_set = set(allowed) & set(VALID_PHASES)
    normalized_stages = {}
    for phase, entry in raw_stages.items():
        if type(phase) is not str or phase not in VALID_PHASES:
            raise ValueError(f'solver_overrides 包含无效阶段名：{phase!r}。')
        if phase not in allowed_set:
            raise ValueError(f'阶段 {phase} 未在当前任务启用，不允许配置 solver_overrides。')
        if not isinstance(entry, dict):
            raise ValueError(f'阶段 {phase} 的 solver_overrides 条目必须为对象。')
        extra_entry = set(entry) - {'tags', 'reason'}
        if extra_entry or 'tags' not in entry or 'reason' not in entry:
            raise ValueError(f'阶段 {phase} 的 solver_overrides 仅允许且必须包含 tags 与 reason。')
        reason = _validate_reason(phase, entry['reason'])
        raw_tags = entry['tags']
        if not isinstance(raw_tags, dict) or not raw_tags:
            raise ValueError(f'阶段 {phase} 的 solver_overrides.tags 必须为非空对象。')
        parsed_tags = {}
        for raw_key, raw_val in raw_tags.items():
            if type(raw_key) is not str or not raw_key.strip():
                raise ValueError(f'阶段 {phase} 包含无效 INCAR 标签名：{raw_key!r}。')
            key = raw_key.strip().upper()
            if key in parsed_tags:
                raise ValueError(f'阶段 {phase} 存在重复的 INCAR 标签：{key}。')
            if key not in ALLOWED_TAGS:
                raise ValueError(
                    f'不允许通过 solver_overrides 修改 INCAR 标签 {key}；仅允许 Tier ③ 数值求解器白名单：'
                    + ', '.join(TAG_ORDER) + '。'
                )
            val = _validate_tag_value(phase, key, raw_val, max_nelm)
            base = baseline_for(phase, key)
            if base is not None and val == base:
                raise ValueError(f'阶段 {phase} 的 {key}={val} 与默认基线相同，请移除无实际变化的覆盖。')
            parsed_tags[key] = val
        effective_nelm = parsed_tags.get('NELM', DEFAULT_NELM)
        if 'NELMIN' in parsed_tags and parsed_tags['NELMIN'] > effective_nelm:
            raise ValueError(f'阶段 {phase} 的 NELMIN({parsed_tags["NELMIN"]}) 不能大于有效 NELM({effective_nelm})。')
        ordered_tags = {key: parsed_tags[key] for key in TAG_ORDER if key in parsed_tags}
        normalized_stages[phase] = {'tags': ordered_tags, 'reason': reason}
    ordered_stages = {phase: normalized_stages[phase] for phase in VALID_PHASES if phase in normalized_stages}
    return {'version': SOLVER_OVERRIDES_VERSION, 'stages': ordered_stages}


def apply_to_incar(incar, config, phase, stage_name=None):
    """Return a copy of incar with validated Tier 3 overrides for (phase, stage_name)."""
    out = dict(incar)
    raw = (config or {}).get('solver_overrides')
    if raw is None:
        return out
    normalized = validate(raw)
    entry = normalized['stages'].get(phase)
    if not entry:
        return out
    for tag, val in entry['tags'].items():
        if tag in IONIC_TAGS and not (phase == 'relaxation' and stage_name in RELAX_IONIC_STAGES):
            continue
        if tag in ALLOWED_TAGS:
            out[tag] = val
    return out


def diff_for_phase(config, phase):
    """Structured incar_solver_diff for stage_gate.context(), or None when absent."""
    raw = (config or {}).get('solver_overrides')
    if raw is None:
        return None
    normalized = validate(raw)
    entry = normalized['stages'].get(phase)
    if not entry:
        return None
    return {
        'version': SOLVER_OVERRIDES_VERSION,
        'phase': phase,
        'reason': entry['reason'],
        'changes': [
            {
                'tag': tag,
                'baseline': baseline_for(phase, tag),
                'override': val,
                'applies_to': applies_to_for(phase, tag),
            }
            for tag, val in entry['tags'].items()
        ],
    }


def set_overrides(mapping, overrides):
    """Attach canonical solver_overrides to mapping, or remove the key when None."""
    if overrides is not None:
        mapping['solver_overrides'] = overrides
    else:
        mapping.pop('solver_overrides', None)


def merge_fork_overrides(source_config, incoming, *, locked_phases, enabled_phases):
    """Merge downstream overrides for a fork while forbidding edits to accepted upstream phases."""
    locked = set(locked_phases)
    enabled = tuple(enabled_phases)
    source_raw = (source_config or {}).get('solver_overrides')
    source_stages = {}
    if source_raw is not None:
        validated_source = validate(source_raw)
        source_stages = {phase: entry for phase, entry in validated_source['stages'].items() if phase in enabled}
    if incoming is None:
        if not source_stages:
            return None
        return {'version': SOLVER_OVERRIDES_VERSION,
                'stages': {phase: source_stages[phase] for phase in VALID_PHASES if phase in source_stages}}
    cleaned = validate(incoming, allowed_phases=enabled)
    for phase in cleaned['stages']:
        if phase in locked:
            raise ValueError(f'派生任务仅允许为尚未执行的下游阶段指定 solver_overrides，禁止修改已验收上游阶段 {phase}。')
    merged = {**source_stages, **cleaned['stages']}
    return {'version': SOLVER_OVERRIDES_VERSION,
            'stages': {phase: merged[phase] for phase in VALID_PHASES if phase in merged}}


def apply_fork_overrides(config, plan, incoming, locked_phases, enabled_phases):
    """Validate, merge, and store fork solver_overrides into config (and optional plan)."""
    merged = merge_fork_overrides(config, incoming, locked_phases=locked_phases, enabled_phases=enabled_phases)
    set_overrides(config, merged)
    if plan is not None:
        set_overrides(plan, merged)
    return merged
