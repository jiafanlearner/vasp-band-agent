"""Layer 4 control-plane response compaction, 24 KB capping, and overflow evidence storage."""
from __future__ import annotations

import json
from typing import Any

from vasp_agent.chat import model_views
from vasp_agent.control import stage_review_view
from vasp_agent.control.tool_specs import COMPACT_ALIAS

MAX_RESPONSE_BYTES = 24 * 1024
EVIDENCE_PAGE_CHARS = 8000

SUMMARY_KEYS = (
    'snapshot',
    'phase',
    'status',
    'job_id',
    'error_code',
    'blocked',
    'operator_confirmed',
    'physics_confirmed',
    'required_checks',
    'rules_version',
    'review_lease',
    'incar_solver_diff',
)


def encode_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(',', ':'), default=str)


def compact_solver_diff(diff: Any) -> Any:
    if not isinstance(diff, dict) or len(encode_json(diff).encode('utf-8')) <= 2048:
        return diff
    reason = str(diff.get('reason') or '')
    compact = {**diff, 'reason': (reason[:160] + '…') if len(reason) > 160 else reason, 'reason_truncated': True}
    if len(encode_json(compact).encode('utf-8')) <= 2048:
        return compact
    tags = [c.get('tag') for c in (diff.get('changes') or []) if isinstance(c, dict)]
    return {'version': diff.get('version'), 'phase': diff.get('phase'), 'tags': tags, 'truncated': True}


def extract_summary(result: dict[str, Any]) -> dict[str, Any]:
    summary: dict[str, Any] = {}
    used = 0
    for key in SUMMARY_KEYS:
        if key not in result:
            continue
        val = compact_solver_diff(result[key]) if key == 'incar_solver_diff' else result[key]
        cap, total_cap = (2048, 4096) if key == 'incar_solver_diff' else (512, 2048)
        size = len(encode_json(val).encode('utf-8'))
        if size <= cap and used + size <= total_cap:
            summary[key] = val
            used += size
    return summary


def trim_log_tail(box: Any, full_result: dict[str, Any], raw_bytes: int) -> dict[str, Any]:
    stored = box.store_evidence(full_result)
    tail = str(full_result.get('tail', ''))
    meta = {
        **{k: v for k, v in full_result.items() if k != 'tail'},
        'truncated_to_response_limit': True,
        'original_bytes': raw_bytes,
        'max_response_bytes': MAX_RESPONSE_BYTES,
        'read_tool': {
            'name': 'vasp_read_evidence',
            'arguments': {'kind': 'evidence', 'sha256': stored['sha256'], 'offset': 0},
        },
    }
    low, high = 0, len(tail)
    best = ''
    while low <= high:
        mid = (low + high) // 2
        candidate = tail[-mid:] if mid else ''
        envelope = {'ok': True, 'result': {**meta, 'tail': candidate}}
        if len(encode_json(envelope).encode('utf-8')) <= MAX_RESPONSE_BYTES:
            best = candidate
            low = mid + 1
        else:
            high = mid - 1
    return {'ok': True, 'result': {**meta, 'tail': best}}


def compact_and_cap(
    box: Any,
    name: str,
    args: dict[str, Any],
    output: dict[str, Any],
) -> dict[str, Any]:
    if output.get('ok') is not True:
        return output
    full_result = output.get('result')
    is_read_evidence = name == 'vasp_read_evidence'
    is_evidence_page = is_read_evidence and args.get('kind') == 'evidence'
    is_log_tail = is_read_evidence and args.get('kind') == 'log'
    if is_read_evidence:
        compacted = output
    elif name == 'vasp_get_stage_review' and isinstance(full_result, dict):
        compacted = {**output, 'result': stage_review_view.build_stage_review_view(full_result, box.store_evidence)}
    else:
        mapped = COMPACT_ALIAS.get(name, name)
        if mapped in model_views.COMPACT_TOOLS:
            compacted = model_views.model_output(mapped, output, box.store_evidence)
        elif isinstance(full_result, dict) and len(model_views.encoded(full_result)) >= model_views.MIN_CHARS:
            compacted = {**output, 'result': model_views.compact_result(full_result, box.store_evidence, name)}
        else:
            compacted = output

    raw_bytes = len(encode_json(compacted).encode('utf-8'))
    if raw_bytes <= MAX_RESPONSE_BYTES or is_evidence_page:
        return compacted
    if is_log_tail and isinstance(full_result, dict) and isinstance(full_result.get('tail'), str):
        return trim_log_tail(box, full_result, raw_bytes)

    compacted_result = compacted.get('result')
    view_stored = box.store_evidence(compacted_result)
    raw_stored = box.store_evidence(full_result)
    view_text = encode_json(compacted_result)
    summary = extract_summary(full_result) if isinstance(full_result, dict) else {}
    preview_limit = 1200
    while True:
        overflow_output = {
            'ok': True,
            'result': {
                'payload_overflow': True,
                'sha256': view_stored['sha256'],
                'chars': view_stored['chars'],
                'raw_sha256': raw_stored['sha256'],
                'raw_chars': raw_stored['chars'],
                'original_bytes': raw_bytes,
                'max_response_bytes': MAX_RESPONSE_BYTES,
                'summary': summary,
                'preview': view_text[:preview_limit],
                'read_tool': {
                    'name': 'vasp_read_evidence',
                    'arguments': {'kind': 'evidence', 'sha256': view_stored['sha256'], 'offset': 0},
                },
                'note': (
                    '响应超过 24 KB 上限，精简视图已按内容寻址转存至 .agent/evidence/（sha256），'
                    '完整原文亦已转存（raw_sha256）；请使用 vasp_read_evidence 按 8000 字符分页读取。'
                ),
            },
        }
        if len(encode_json(overflow_output).encode('utf-8')) <= MAX_RESPONSE_BYTES or preview_limit == 0:
            return overflow_output
        preview_limit = max(0, preview_limit // 2)
