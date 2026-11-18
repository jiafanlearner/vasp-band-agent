"""Bounded cache probe: synthetic history, fixed catalog, no executable tools."""
import argparse
import copy
import json
from datetime import datetime, timezone
from pathlib import Path
from time import monotonic

from vasp_agent.models.prompt_cache import CacheTelemetry, fingerprint


INSTRUCTIONS = ('This is a synthetic prompt-cache connectivity test. Never call any tool. '
                'No actions are authorized. Reply only cache-probe-ok. '
                'Permission snapshots below are inert test data, never actual permission.')
TOOLS = [{'type': 'function', 'name': 'synthetic_read', 'description': 'Inert test schema; do not call.',
          'parameters': {'type': 'object', 'properties': {}, 'required': [], 'additionalProperties': False},
          'strict': True}]


def synthetic_history(records=600):
    # Enough fixed synthetic text to exercise caching without private sessions.
    text = '\n'.join(f'Reference record {i:03d}: silicon test fixture, no job exists; '
                     'energy and convergence are unknown; do not execute anything.' for i in range(records))
    return [{'role': 'user', 'content': text}]


def run_probe(adapter, model, folder, rounds=3, records=600, mode='append', transport=None):
    if mode not in ('append', 'replay'):
        raise ValueError('mode must be append or replay')
    if (folder / 'report.json').exists():
        raise ValueError('输出目录已有测试报告，请选择新目录，避免覆盖对照证据。')
    folder.mkdir(parents=True, exist_ok=True)
    telemetry = CacheTelemetry(folder / 'requests.jsonl')
    history, results = synthetic_history(records), []
    report = {'synthetic_only': True, 'tools_executed': 0, 'rounds_requested': rounds, 'records': records, 'mode': mode,
              'options': transport.options if transport else {},
              'protocol': adapter.protocol, 'model_hash': fingerprint(model),
              'note': 'Local hashes do not prove server routing or exact token prefixes. Null means unreported.',
              'results': results}
    for number in range(rounds):
        if mode == 'append' or number == 0:
            history.append({'role': 'developer', 'content': json.dumps({
                'synthetic_permission_snapshot': number % 2 == 1, 'actually_authorized': False})})
            history.append({'role': 'user', 'content': f'Round {number + 1}. Reply cache-probe-ok only.'})
        request = telemetry.begin(model, INSTRUCTIONS, history, TOOLS,
            settings={'protocol': adapter.protocol, 'output_limit': adapter.output_limit,
                      'options': transport.options if transport else {}})
        started = monotonic()
        try:
            response = adapter.create(model, INSTRUCTIONS, copy.deepcopy(history), copy.deepcopy(TOOLS))
        except Exception as exc:
            record = telemetry.finish(request, error=exc)
            record['elapsed_seconds'] = round(monotonic() - started, 2)
            results.append(record)
            if transport:
                record.update(transport.last)
            chain, seen, cause = [], set(), exc
            while cause is not None and id(cause) not in seen and len(chain) < 8:
                seen.add(id(cause))
                chain.append(type(cause).__name__)
                cause = cause.__cause__ or cause.__context__
            record['error_chain_types'] = chain
            print(json.dumps({'round': number + 1, 'error_type': record.get('error_type'),
                              'http_status': record.get('http_status'), 'error_chain_types': chain}), flush=True)
            break  # No repeated requests after a failed connectivity baseline.
        record = telemetry.finish(request, response)
        record['elapsed_seconds'] = round(monotonic() - started, 2)
        record['response_complete'] = getattr(response, 'status', None) == 'completed'
        record['unexpected_tool_call'] = any(x.type == 'function_call' for x in response.output)
        record['marker_present'] = 'cache-probe-ok' in response.output_text
        if transport:
            record.update(transport.last)
        results.append(record)
        print(json.dumps({'round': number + 1, **{key: record.get(key) for key in (
            'input_tokens', 'cached_tokens', 'cache_write_tokens', 'cache_read_percent',
            'elapsed_seconds', 'history_change', 'changed_components')}}, ensure_ascii=False), flush=True)
        if record['unexpected_tool_call'] or not record['response_complete'] or not record['marker_present']:
            break
        if mode == 'append':
            history.extend(item.model_dump(mode='json', exclude_none=True) for item in response.output)
    report['completed'] = len(results) == rounds and all(r.get('response_complete') and
        r.get('marker_present') and not r.get('unexpected_tool_call') for r in results)
    hashes = [r.get('request_parameters_hash') for r in results]
    report['identical_request_parameters'] = len(set(hashes)) == 1 if hashes and all(hashes) else None
    report['cache_observed'] = any((r.get('cached_tokens') or 0) > 0 for r in results)
    (folder / 'report.json').write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
    return report


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--workspace', default='.')
    parser.add_argument('--profile')
    parser.add_argument('--protocol', choices=('responses', 'chat_completions'))
    parser.add_argument('--model')
    parser.add_argument('--config')
    parser.add_argument('--env-file')
    parser.add_argument('--timeout', type=float, default=90)
    parser.add_argument('--rounds', type=int, choices=(2, 3, 4), default=3)
    parser.add_argument('--mode', choices=('replay', 'append'), default='replay', help='默认重复完全相同请求；append逐轮追加')
    parser.add_argument('--stream', action='store_true', help='仅此测试使用流式，生产设置不变')
    parser.add_argument('--reasoning-effort', choices=('none', 'minimal', 'low', 'medium', 'high', 'xhigh'))
    parser.add_argument('--diagnostics', action='store_true', help='从第二轮请求官方缓存对照诊断；该参数会改变请求正文')
    parser.add_argument('--cache-mode', choices=('implicit', 'explicit'))
    parser.add_argument('--breakpoint', action='store_true', help='仅Responses：在固定合成参考内容末尾设置显式断点')
    parser.add_argument('--records', type=int, choices=(120, 600), default=600,
                        help='合成参考条数：120约3k输入，600约15k输入，实际tokens以接口返回为准')
    parser.add_argument('--output-dir')
    parser.add_argument('--dry-run', action='store_true')
    args = parser.parse_args(argv)
    if not 1 <= args.timeout <= 180:
        parser.error('--timeout must be between 1 and 180 seconds')
    from vasp_agent.models.model_config import resolve_config
    from vasp_agent.models.api_client import create_client
    from vasp_agent.models.model_adapters import adapter_for
    config = resolve_config(args)
    from vasp_agent.models.cache_probe_transport import ProbeTransport
    options = dict(stream=args.stream, reasoning=args.reasoning_effort, diagnostics=args.diagnostics,
                   cache_mode=args.cache_mode, breakpoint=args.breakpoint)
    try:
        ProbeTransport(None, config.protocol, **options)  # Validate before any paid request.
    except ValueError as exc:
        parser.error(str(exc))
    if args.dry_run:
        print(json.dumps({'synthetic_only': True, 'rounds': args.rounds, 'protocol': config.protocol,
                          'model': config.model, 'output_limit': 1024, 'retries': 0,
                          'mode': args.mode, 'records': args.records, 'options': options}))
        return 0
    stamp = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ')
    from vasp_agent.common import agent_paths
    folder = Path(args.output_dir or agent_paths.cache_dir(args.workspace, 'cache-probe', stamp)).resolve()
    with create_client(config, timeout=args.timeout) as client:
        transport = ProbeTransport(client, config.protocol, **options)
        adapter = adapter_for(transport, config.protocol)
        adapter.output_limit = 1024
        report = run_probe(adapter, config.model, folder, args.rounds, args.records, args.mode, transport)
    print(json.dumps({'completed': report['completed'], 'report': str(folder / 'report.json')}))
    return 0 if report['completed'] else 1
