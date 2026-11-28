"""Layer 4 stage-focused projection for `vasp_get_stage_review` (runs before `model_views`).

Reduces per-stage review payloads to the current stage's inputs and direct upstream
evidence summaries before `chat.model_views.compact_result` runs, preventing dangling
`same_as` references while keeping every omitted section and the full raw payload
content-addressed in `.agent/evidence/`.
"""
from __future__ import annotations

from typing import Any, Callable

from vasp_agent.chat import model_views
from vasp_agent.workflow import stage_definitions, stage_gate

StoreFn = Callable[[Any], dict[str, Any]]

_CONVERGENCE_ONLY_INPUT_KEYS = (
    'initial_inputs',
    'initial_poscar',
    'submit_script',
    'scan_verification',
)

_KPOINTS_PHASES = frozenset({'convergence', 'relaxation'})

_DIRECT_SUMMARIES: dict[str, frozenset[str]] = {
    'convergence': frozenset(),
    'relaxation': frozenset({'convergence.json'}),
    'scf': frozenset({'relaxation.json', 'geometry-summary.json', 'post-relax-validation.json'}),
    'bands': frozenset({'scf-summary.json'}),
    # DOS requires scf-summary.json for electronic convergence, force, and stress checks
    # not embedded inside dos_plan.scf_evidence.
    'dos': frozenset({'scf-summary.json'}),
}

_VIEW_NOTE = (
    '控制面已按当前阶段聚焦投影；历史输入与非直接上游摘要已按内容寻址转存，'
    '可按需用 vasp_read_evidence 点读，full_sha256 指向未投影完整原文。snapshot 按完整数据计算。'
)
_MODEL_VIEW_NOTE = (
    '精简视图（基于阶段聚焦投影）：哈希表只列数量和合并摘要（程序已逐一校验），重复内容标 same_as，'
    '矩阵只留标量。sections 指向投影视图内的压缩段；full 与 stage_review_view.full_sha256 指向未投影完整原文。'
    'snapshot 按完整数据计算。'
)


def _is_projectable(full_result: Any) -> bool:
    if not isinstance(full_result, dict):
        return False
    phase = full_result.get('phase')
    if not isinstance(phase, str) or phase not in stage_definitions.known_names():
        return False
    return isinstance(full_result.get('inputs'), dict) or isinstance(full_result.get('evidence'), dict)


def _fold_section(
    value: Any,
    *,
    phase: str,
    store: StoreFn,
    omitted_index: dict[str, str],
    path_key: str,
    omitted_keys: list[str] | None = None,
) -> dict[str, Any]:
    stored = store(value)
    omitted_index[path_key] = stored['sha256']
    marker: dict[str, Any] = {
        'omitted_for_phase': phase,
        'sha256': stored['sha256'],
        'chars': stored['chars'],
    }
    if omitted_keys is not None:
        marker['omitted_keys'] = list(omitted_keys)
    return marker


def _allowed_summary_names(phase: str, config: Any) -> set[str] | None:
    if phase in _DIRECT_SUMMARIES:
        return set(_DIRECT_SUMMARIES[phase])
    versions = stage_definitions.DEFINITIONS.get(phase)
    if not versions:
        return None
    ver = max(versions)
    if isinstance(config, dict) and isinstance(config.get('stages'), list):
        for entry in config['stages']:
            if isinstance(entry, dict) and entry.get('name') == phase and entry.get('version') in versions:
                ver = entry['version']
                break
    depends_on = versions[ver].get('depends_on') or ()
    names: set[str] = set()
    for dep in depends_on:
        dep_versions = stage_definitions.DEFINITIONS.get(dep) or {}
        for item in dep_versions.values():
            names.update(item.get('summaries') or ())
    return names


def _keep_incar_key(phase: str, key: str, depends_on: tuple[str, ...]) -> bool:
    if phase == 'convergence':
        return stage_gate.phase_of(key) == 'convergence'
    if phase == 'relaxation':
        return stage_gate.phase_of(key) == 'relaxation'
    if phase == 'scf':
        return key == 'scf'
    if phase == 'bands':
        return key in ('bands', 'scf')
    if phase == 'dos':
        return key == 'scf'
    stage_of_key = stage_gate.phase_of(key)
    return stage_of_key == phase or stage_of_key in depends_on


def _depends_on(phase: str) -> tuple[str, ...]:
    versions = stage_definitions.DEFINITIONS.get(phase)
    if not versions:
        return ()
    return tuple(versions[max(versions)].get('depends_on') or ())


def _project_plan(
    plan: dict[str, Any],
    *,
    phase: str,
    store: StoreFn,
    omitted_index: dict[str, str],
) -> dict[str, Any]:
    projected_plan = dict(plan)
    if phase != 'convergence' and 'parameter_notes' in projected_plan:
        projected_plan['parameter_notes'] = _fold_section(
            projected_plan['parameter_notes'],
            phase=phase,
            store=store,
            omitted_index=omitted_index,
            path_key='inputs/plan/parameter_notes',
        )
    if phase not in _KPOINTS_PHASES and 'kpoints' in projected_plan:
        projected_plan['kpoints'] = _fold_section(
            projected_plan['kpoints'],
            phase=phase,
            store=store,
            omitted_index=omitted_index,
            path_key='inputs/plan/kpoints',
        )
    incars = projected_plan.get('incars')
    if isinstance(incars, dict) and incars:
        deps = _depends_on(phase)
        kept = {k: v for k, v in incars.items() if _keep_incar_key(phase, k, deps)}
        # Forward-safe fallback: for unknown future stages with no matched keys, keep all incars.
        if not kept and phase not in _DIRECT_SUMMARIES:
            kept = dict(incars)
        dropped = {k: v for k, v in incars.items() if k not in kept}
        if dropped:
            projected_plan['incars'] = kept
            projected_plan['incars_omitted'] = _fold_section(
                dropped,
                phase=phase,
                store=store,
                omitted_index=omitted_index,
                path_key='inputs/plan/incars_omitted',
                omitted_keys=list(dropped),
            )
    return projected_plan


def _project_inputs(
    inputs: dict[str, Any],
    *,
    phase: str,
    store: StoreFn,
    omitted_index: dict[str, str],
) -> dict[str, Any]:
    projected_inputs = dict(inputs)
    if phase != 'convergence':
        for key in _CONVERGENCE_ONLY_INPUT_KEYS:
            if key in projected_inputs:
                projected_inputs[key] = _fold_section(
                    projected_inputs[key],
                    phase=phase,
                    store=store,
                    omitted_index=omitted_index,
                    path_key=f'inputs/{key}',
                )
    plan = projected_inputs.get('plan')
    if isinstance(plan, dict):
        projected_inputs['plan'] = _project_plan(
            plan, phase=phase, store=store, omitted_index=omitted_index,
        )
    return projected_inputs


def _project_evidence(
    evidence: dict[str, Any],
    *,
    phase: str,
    config: Any,
    store: StoreFn,
    omitted_index: dict[str, str],
) -> dict[str, Any]:
    projected_evidence = dict(evidence)
    summaries = projected_evidence.get('summaries')
    allowed = _allowed_summary_names(phase, config)
    if isinstance(summaries, dict) and summaries and allowed is not None:
        kept = {k: v for k, v in summaries.items() if k in allowed}
        dropped = {k: v for k, v in summaries.items() if k not in allowed}
        if dropped:
            projected_evidence['summaries'] = kept
            projected_evidence['summaries_omitted'] = _fold_section(
                dropped,
                phase=phase,
                store=store,
                omitted_index=omitted_index,
                path_key='evidence/summaries_omitted',
                omitted_keys=list(dropped),
            )
    return projected_evidence


def project_stage_review(
    full_result: dict[str, Any],
    store: StoreFn,
) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
    """Project a stage-review payload to current-stage inputs and direct upstream summaries.

    Never mutates `full_result`. Returns `(projected, raw_stored, view_meta)`.
    """
    phase = str(full_result['phase'])
    raw_stored = store(full_result)
    omitted_index: dict[str, str] = {}
    projected = dict(full_result)

    inputs = projected.get('inputs')
    config = inputs.get('config') if isinstance(inputs, dict) else None
    if isinstance(inputs, dict):
        projected['inputs'] = _project_inputs(
            inputs, phase=phase, store=store, omitted_index=omitted_index,
        )
    evidence = projected.get('evidence')
    if isinstance(evidence, dict):
        projected['evidence'] = _project_evidence(
            evidence, phase=phase, config=config, store=store, omitted_index=omitted_index,
        )

    view_meta: dict[str, Any] = {
        'projected': True,
        'phase': phase,
        'full_sha256': raw_stored['sha256'],
        'full_chars': raw_stored['chars'],
        'omitted': omitted_index,
        'note': _VIEW_NOTE,
    }
    return projected, raw_stored, view_meta


def build_stage_review_view(full_result: dict[str, Any], store: StoreFn) -> dict[str, Any]:
    """Run ① stage projection -> ② model_views compaction while preserving raw `full_sha256`."""
    if not _is_projectable(full_result):
        return model_views.compact_result(full_result, store, 'get_stage_review')

    projected, raw_stored, view_meta = project_stage_review(full_result, store)
    compacted = model_views.compact_result(projected, store, 'get_stage_review')
    result = dict(compacted) if isinstance(compacted, dict) else dict(projected)
    if isinstance(result.get('model_view'), dict):
        result['model_view'] = {
            **result['model_view'],
            'full': raw_stored,
            'sections_scope': 'projected_view',
            'note': _MODEL_VIEW_NOTE,
        }
    result['stage_review_view'] = view_meta
    return result
