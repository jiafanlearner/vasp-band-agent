"""Compact model-facing views of bulky tool results.

Presentation only: a view is built from a result after the tool has returned,
for the conversation history the model reads. Nothing here touches what a tool
computes, hashes or verifies -- snapshots are computed by the engine over the
full data before the view exists, the audit log keeps the full output, and the
full result plus every reduced section are stored content-addressed in the
evidence store (.agent/evidence/) and named in the view's ``model_view`` field, so the model
can page any of them back with read_context_evidence (whose hash check makes the
round trip verifiable).

What is reduced, and why it is safe to reduce for the model:
- hash maps (four or more 64-hex values): a count and one combined digest; the
  engine compares every hash itself, the model never does;
- per-stage INCAR dicts and the generated INCAR/KPOINTS texts: a base plus the
  per-stage differences, which is the same information;
- parameter_notes: each parameter's source; the explanations are fixed text;
- force and stress matrices, lattice matrices, Cartesian k coordinates: the
  scalars beside them (max_force, max_stress_kbar, frac) stay;
- repeated subtrees: the second copy names the first (``same_as``);
- very long strings (scheduler dumps, task files): head and tail.
Fields the prompt or tool descriptions tell the model to read are in KEEP and
are copied verbatim wherever they occur.
"""
from __future__ import annotations

import hashlib
import json
import re
from collections import Counter

# Results the model reads at length, measured as the largest in the audit log.
COMPACT_TOOLS = frozenset({
    'get_stage_review', 'get_input_review', 'remote_status', 'inspect_convergence',
    'get_submission_diagnostic', 'analyze_band_results', 'list_scan_plans',
    'get_submission_resources', 'plan_submission_resources', 'get_status',
    'request_submission_recovery', 'prepare_scan_retry'})
# Below this a result is shown as it is; a view would save little.
MIN_CHARS = 2000
# Named in INSTRUCTIONS or tool descriptions, or what a review decision rests
# on: copied verbatim, never collapsed, deduplicated or clipped.
KEEP = frozenset({
    'effective_thresholds', 'threshold_sources', 'checks', 'failed_checks', 'failure_reasons',
    'next', 'next_step', 'next_steps', 'next_action', 'note', 'error_code', 'submission_confirmed',
    'snapshot', 'source_snapshot', 'phase', 'required_checks', 'check_details', 'rules_version',
    'cutoff_vs_highest', 'cross_cutoff',
    'scf_occupation_audit', 'execution_tags', 'array_tasks', 'preparation_summary',
    'kmesh_suggestion', 'dimensionality_warning', 'submission_phase_verified',
    'execution_phase_verified', 'attempt_binding_verified', 'phase_verified',
    'operator_confirmed', 'physics', 'limitations', 'status', 'job_id',
    # Guide pointers and pages (re-read by id/version/sha256) and the
    # operator-chosen stage parameters a review rests on.
    'guides', 'guide_page', 'guide_ref', 'stage_parameters',
    # The DOS plan a dos review rests on (generated inputs, program pre-checks).
    'dos_plan', 'program_checks', 'blocked',
    # On-demand tool group activation receipts (scanned from live_history).
    'tool_groups_enabled'})
# Matrices whose scalar summaries sit beside them.
MATRICES = frozenset({'forces', 'stress_kbar', 'initial_lattice', 'optimized_lattice', 'cartesian_inv_A'})
HEX64 = re.compile(r'[0-9a-f]{64}')
DEDUPE_CHARS = 200
LONG_TEXT = 2000
HEAD, TAIL = 400, 1200
# Per-tool string fields shown clipped: a plan list repeats each plan's whole
# rationale, which the menu and the selected plan already showed.
CLIPPED = {'list_scan_plans': ('rationale', 'message')}
CLIP_CHARS = 160
MAX_PREPARED_PLANS = 5


def encoded(value):
    return json.dumps(value, ensure_ascii=False, separators=(',', ':'), default=str)


def _hash_map(value):
    return (isinstance(value, dict) and len(value) >= 4
            and all(isinstance(v, str) and HEX64.fullmatch(v) for v in value.values()))


def _incar_table(stages):
    """Per-stage INCAR dicts as one base plus each stage's differences."""
    keys = {key for incar in stages.values() for key in incar}
    base = {}
    for key in sorted(keys):
        votes = Counter(encoded(incar.get(key)) for incar in stages.values())
        base[key] = json.loads(votes.most_common(1)[0][0])
    differences = {}
    for name, incar in stages.items():
        delta = {key: incar.get(key) for key in keys if incar.get(key) != base[key]}
        if delta:
            differences[name] = delta
    return {'base': {k: v for k, v in base.items() if v is not None}, 'differences': differences,
            'stages': list(stages), 'view': 'base_plus_differences'}


def _incar_text(text):
    pairs = {}
    for line in text.splitlines():
        line = line.split('#', 1)[0].split('!', 1)[0].strip()
        if '=' in line:
            key, value = line.split('=', 1)
            pairs[key.strip().upper()] = value.strip()
    return pairs


def _input_files(files):
    """Generated INCAR/KPOINTS texts: INCAR keys as base plus differences, KPOINTS grouped."""
    incars = {name: _incar_text(text) for name, text in files.items() if name.endswith('INCAR')}
    others = {}
    for name, text in files.items():
        if not name.endswith('INCAR'):
            others.setdefault(text, []).append(name)
    view = {'files': len(files)}
    if incars:
        view['incar'] = _incar_table(incars)
        view['incar']['comments'] = '生成文件的行内注释即 parameter_notes 的来源说明'
    if others:
        view['other_files'] = [{'text': text, 'files': names} for text, names in others.items()]
    return view


def _review_checklist(value):
    return (isinstance(value, list) and value
            and all(isinstance(x, dict) and 'item' in x and 'judgment' in x for x in value))


def _gamma_points(points):
    """Degenerate groups as 'bands@energy' strings; degeneracy is the band count."""
    view = []
    for point in points:
        if not isinstance(point, dict) or not isinstance(point.get('groups'), list):
            return points
        groups = []
        for group in point['groups']:
            bands = group.get('bands') or []
            label = f'{bands[0]}-{bands[-1]}' if len(bands) > 1 else str(bands[0] if bands else '?')
            groups.append(f"{label}@{group.get('energy_eV')}")
        view.append({**{k: v for k, v in point.items() if k != 'groups'}, 'groups': groups})
    return {'format': '能带序号区间@能量eV；区间长度即简并度', 'points': view}


def _resolved_path(value):
    return (isinstance(value, dict) and isinstance(value.get('available_labels'), dict)
            and isinstance(value.get('segments'), list)
            and all(isinstance(x, dict) and isinstance(x.get('points'), list) for x in value['segments']))


def _labelled_segment(segment, labels):
    """A resolved point whose coordinates are the convention's own, as its label alone."""
    def point(p):
        if (isinstance(p, dict) and p.get('source') == 'convention' and p.get('label') in labels
                and p.get('frac') == labels[p['label']]):
            return p['label']
        return {k: v for k, v in p.items() if k != 'cartesian_inv_A'} if isinstance(p, dict) else p
    return {**segment, 'points': [point(p) for p in segment['points']],
            'labels': 'available_labels 中的约定点只写标签'}


class View:
    def __init__(self, clip=()):
        self.seen = {}
        self.clip = frozenset(clip)

    def walk(self, value, path):
        key = path[-1] if path else None
        if key == 'checks' and _review_checklist(value):
            # An earlier recorded review (remote_status stage_reviews): its
            # outcome per item. 'checks' in KEEP is the per-metric threshold dict.
            return [{k: check.get(k) for k in ('item', 'judgment', 'blocking')} for check in value]
        if key in KEEP:
            return value
        if isinstance(key, str) and key in MATRICES and isinstance(value, list):
            return f'<{len(value)}×{len(value[0]) if value and isinstance(value[0], list) else 1} 省略>'
        if key in self.clip and isinstance(value, str) and len(value) > CLIP_CHARS:
            return value[:CLIP_CHARS] + '…'
        if _hash_map(value):
            return {'count': len(value), 'combined_sha256': hashlib.sha256(encoded(dict(sorted(value.items()))).encode()).hexdigest(),
                    'omitted': 'hash_map'}
        if key == 'incars' and isinstance(value, dict) and value and all(isinstance(v, dict) for v in value.values()):
            return _incar_table(value)
        if key == 'parameter_notes' and isinstance(value, dict) and all(isinstance(v, dict) for v in value.values()):
            return {'sources': {k: v.get('source') for k, v in value.items()}, 'omitted': 'explanations'}
        if key == 'initial_inputs' and isinstance(value, dict) and all(isinstance(v, str) for v in value.values()):
            return _input_files(value)
        if key == 'gamma_points' and isinstance(value, list):
            return _gamma_points(value)
        if _resolved_path(value):
            value = {**value, 'segments': [_labelled_segment(segment, value['available_labels'])
                                           for segment in value['segments']]}
            return {k: self.walk(v, path + [k]) for k, v in value.items()}
        if isinstance(value, (dict, list, str)):
            text = value if isinstance(value, str) else encoded(value)
            if len(text) >= DEDUPE_CHARS:
                if text in self.seen:
                    return {'same_as': self.seen[text]}
                self.seen[text] = '/'.join(map(str, path))
        if isinstance(value, dict):
            return {k: self.walk(v, path + [k]) for k, v in value.items()}
        if isinstance(value, list):
            return [self.walk(v, path + [i]) for i, v in enumerate(value)]
        if isinstance(value, str) and len(value) > LONG_TEXT:
            return value[:HEAD] + f'\n[…省略 {len(value) - HEAD - TAIL} 字符，原文见 model_view…]\n' + value[-TAIL:]
        return value


def _sections(full, compact, path=(), depth=0):
    """Paths (at most two levels deep) whose content the view changed."""
    if full == compact:
        return []
    if depth < 2 and isinstance(full, dict) and isinstance(compact, dict) and set(full) == set(compact):
        found = []
        for key in full:
            found += _sections(full[key], compact[key], path + (key,), depth + 1)
        return found
    return [path] if path else []


def _lookup(value, path):
    for key in path:
        value = value[key]
    return value


def _compact_plan_entry(plan):
    """Trim verbose fields from one scan plan entry while keeping actionable state."""
    if not isinstance(plan, dict):
        return plan
    req = plan.get('request') if isinstance(plan.get('request'), dict) else {}
    req_brief = {k: req[k] for k in ('server', 'job_name', 'structure', 'relax_mode', 'optional_stages')
                 if k in req and req[k] is not None}
    selection = plan.get('selection') if isinstance(plan.get('selection'), dict) else {}
    status = plan.get('status')
    if status in ('pending', 'selected'):
        entry = {k: plan[k] for k in ('id', 'status', 'selected', 'choice', 'next') if k in plan}
        if req_brief:
            entry['request'] = req_brief
        if plan.get('revision_reason'):
            entry['revision_reason'] = plan['revision_reason']
        if 'stage_parameters' in plan:
            entry['stage_parameters'] = plan['stage_parameters']
        if isinstance(plan.get('environment'), dict):
            entry['environment'] = {k: plan['environment'][k] for k in ('selected', 'required') if k in plan['environment']}
        if selection:
            entry['selection'] = {k: v for k, v in selection.items() if k != 'message'}
        if isinstance(plan.get('rationale'), str) and plan['rationale']:
            entry['rationale'] = plan['rationale'][:CLIP_CHARS] + ('…' if len(plan['rationale']) > CLIP_CHARS else '')
        return entry
    entry = {k: plan[k] for k in ('id', 'status', 'choice', 'next') if k in plan}
    entry.update(req_brief)
    if 'operator_confirmed' in selection:
        entry['operator_confirmed'] = selection['operator_confirmed']
    if 'stage_parameters' in plan:
        entry['stage_parameters'] = plan['stage_parameters']
    return entry


def _compact_plan_list(result):
    plans = result.get('plans')
    if not isinstance(plans, list):
        return None
    open_plans = [_compact_plan_entry(p) for p in plans if isinstance(p, dict) and p.get('status') in ('pending', 'selected')]
    prepared = [p for p in plans if not (isinstance(p, dict) and p.get('status') in ('pending', 'selected'))]
    shown_prepared = [_compact_plan_entry(p) for p in prepared[:MAX_PREPARED_PLANS]]
    omitted = max(0, len(prepared) - len(shown_prepared))
    view = {**result, 'plans': open_plans + shown_prepared}
    if omitted:
        view['omitted_prepared_plans'] = omitted
    return view


def compact_result(result, store, name=None):
    """The view of one tool result, or the result unchanged when no view applies.

    store(payload) saves a payload content-addressed and returns its sha256
    and size; the full result and each changed section are stored.
    """
    if not isinstance(result, dict) or len(encoded(result)) < MIN_CHARS:
        return result
    if name == 'list_scan_plans':
        view = _compact_plan_list(result)
    else:
        view = View(CLIPPED.get(name, ())).walk(result, [])
    if view is None or view == result:
        return result
    sections = {'/'.join(path): store(_lookup(result, path))['sha256'] for path in _sections(result, view)}
    view['model_view'] = {
        'compact': True, 'full': store(result), 'sections': sections,
        'note': '精简视图：哈希表只列数量和合并摘要（程序已逐一校验），重复内容标 same_as，矩阵只留标量。'
                '原文：read_context_evidence 读 sections 中各路径的 sha256 或 full.sha256。snapshot 按完整数据计算。'}
    return view


def model_output(name, output, store):
    """The copy of a tool output written to the model's history."""
    if not isinstance(output, dict):
        return output
    recovery = output.get('recovery')
    if isinstance(recovery, dict) and isinstance(recovery.get('output'), dict):
        output = {**output, 'recovery': {**recovery, 'output': model_output(recovery.get('tool'), recovery['output'], store)}}
    if name in COMPACT_TOOLS and output.get('ok') is True and isinstance(output.get('result'), dict):
        output = {**output, 'result': compact_result(output['result'], store, name)}
    return output
