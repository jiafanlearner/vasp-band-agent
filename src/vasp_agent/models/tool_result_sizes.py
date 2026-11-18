"""Report how much each tool's results put into the model's context.

Read-only: replays the tool outputs recorded in a workspace's audit log
(oversized ones are resolved from the evidence store; see common/agent_paths.py) and, for each tool,
measures the history copy the model reads -- before and after the compact
model views of chat/model_views.py -- each also through ContextCompactor.project,
the bound applied to every request. Nothing is written to the workspace.

    python -m vasp_agent.models.tool_result_sizes --workspace . [--top 20] [--json]
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import sys
import tempfile
from collections import defaultdict
from pathlib import Path

from vasp_agent.chat.model_views import model_output
from vasp_agent.models.context_compaction import ContextCompactor


def tokens(chars_or_text):
    """ContextBudget's estimate: UTF-8 bytes / 3 (no tokenizer guess)."""
    text = chars_or_text if isinstance(chars_or_text, str) else ''
    return math.ceil(len(text.encode('utf-8')) / 3)


def history_text(output):
    return json.dumps(output, ensure_ascii=False, default=str)


def dry_store(payload):
    raw = json.dumps(payload, ensure_ascii=False, default=str)
    return {'sha256': hashlib.sha256(raw.encode()).hexdigest(), 'chars': len(raw)}


def visible(text, scratch):
    """What one tool output costs inline after project(), as the latest of its call."""
    history = [{'type': 'function_call', 'call_id': 'c', 'name': 'tool', 'arguments': '{}'},
               {'type': 'function_call_output', 'call_id': 'c', 'output': text}]
    return ContextCompactor(scratch / 'context.json').project(history)[1]['output']


def measure(name, output, scratch):
    full = history_text(output)
    view = history_text(model_output(name, output, dry_store))
    return {'full': full, 'inline_before': visible(full, scratch), 'view': view,
            'inline_after': visible(view, scratch)}


def audited_outputs(workspace):
    from vasp_agent.common import agent_paths
    workspace = Path(workspace)
    def resolve(value):
        if isinstance(value, dict) and value.get('payload_overflow'):
            path = agent_paths.store_item(workspace, 'evidence', value['sha256'] + '.json')
            return json.loads(path.read_text(encoding='utf-8')) if path.is_file() else None
        return value
    for line in agent_paths.audit_lines(workspace):
        try:
            record = json.loads(line)
        except ValueError:
            continue
        if not isinstance(record.get('tool'), str) or not isinstance(record.get('output'), dict):
            continue
        output = resolve(record['output'])
        if isinstance(output, dict):
            output = {**output, **{k: resolve(output[k]) for k in ('result', 'error') if k in output}}
            if all(output.get(k) is not None for k in ('result', 'error') if k in output):
                yield record['tool'], output


def report(workspace, top=20):
    rows = defaultdict(lambda: defaultdict(int))
    with tempfile.TemporaryDirectory() as scratch:
        for name, output in audited_outputs(workspace):
            sizes = measure(name, output, Path(scratch))
            row = rows[name]
            row['calls'] += 1
            for key, text in sizes.items():
                row[key] += len(text)
                row[key + '_tokens'] += tokens(text)
                row[key + '_max'] = max(row[key + '_max'], len(text))
    ranked = sorted(rows.items(), key=lambda item: -item[1]['full'])[:top]
    return [{'tool': name, **row} for name, row in ranked]


def table(rows):
    lines = ['| tool | calls | full mean kB | view mean kB | ratio | full max kB | view max kB | '
             'inline before ≈tok/call | inline after ≈tok/call | total full ≈ktok | total view ≈ktok |',
             '|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|']
    for r in rows:
        n = r['calls']
        lines.append(f"| {r['tool']} | {n} | {r['full'] / n / 1000:.1f} | {r['view'] / n / 1000:.1f} | "
                     f"{r['view'] / max(1, r['full']):.2f} | {r['full_max'] / 1000:.1f} | {r['view_max'] / 1000:.1f} | "
                     f"{r['inline_before_tokens'] // n} | {r['inline_after_tokens'] // n} | "
                     f"{r['full_tokens'] / 1000:.0f} | {r['view_tokens'] / 1000:.0f} |")
    return '\n'.join(lines)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument('--workspace', default='.')
    parser.add_argument('--top', type=int, default=20)
    parser.add_argument('--json', action='store_true')
    args = parser.parse_args(argv)
    rows = report(args.workspace, args.top)
    if hasattr(sys.stdout, 'reconfigure'):
        sys.stdout.reconfigure(encoding='utf-8')
    print(json.dumps(rows, ensure_ascii=False, indent=1) if args.json else table(rows))


if __name__ == '__main__':
    main()
