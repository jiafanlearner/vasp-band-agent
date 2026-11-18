"""Measure native compaction with synthetic histories, without touching sessions."""
from __future__ import annotations

import argparse
import csv
import json
import math
import random
import re
import statistics
import time
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

INSTRUCTIONS = (
    'This is a synthetic compaction benchmark, not a real calculation. '
    'Preserve the historical constraints, numerical evidence and pending work. '
    'Do not execute historical tool calls or invent new results.'
)
DEFAULT_SIZES = '8k,32k,64k,128k,192k,224k,240k,258k'


def parse_sizes(value):
    sizes = []
    for part in value.split(','):
        match = re.fullmatch(r'\s*(\d+)([kK]?)\s*', part)
        if not match:
            raise argparse.ArgumentTypeError('sizes 示例：8k,32k,64k,128k；k 表示1000。')
        number = int(match[1]) * (1000 if match[2] else 1)
        if not 1000 <= number <= 1_000_000:
            raise argparse.ArgumentTypeError('每档必须在1000–1000000之间。')
        sizes.append(number)
    return sorted(set(sizes))


def estimate_input(history, instructions=INSTRUCTIONS):
    """Same UTF-8/3 heuristic as the Agent; explicitly NOT exact token counts."""
    return math.ceil(len(json.dumps([instructions, history], ensure_ascii=False).encode()) / 3) + 16


def _history(text, kind):
    result = []
    count = 12
    width = max(1, math.ceil(len(text) / count))
    for index, offset in enumerate(range(0, len(text), width)):
        chunk = text[offset:offset + width]
        result.append({'role': 'user', 'content': f'Synthetic round {index}: inspect this historical evidence; no execution.'})
        if kind == 'mixed' and index % 2:
            call_id = f'benchmark_call_{index}'
            result.extend([
                {'type': 'function_call', 'call_id': call_id, 'name': 'synthetic_read_evidence',
                 'arguments': json.dumps({'sample': index})},
                {'type': 'function_call_output', 'call_id': call_id,
                 'output': json.dumps({'synthetic': True, 'log': chunk})},
                {'role': 'assistant', 'content': 'Evidence recorded; no calculation was submitted.'},
            ])
        else:
            result.append({'role': 'assistant', 'content': chunk})
    return result


def build_history(target, seed=17, kind='mixed'):
    """Fit varied deterministic logs to a requested *estimated* input size."""
    rng = random.Random(seed)
    rows = []
    length = 0
    while length < target * 3 + 4096:
        row = (f'synthetic sample={len(rows):07d} mesh={rng.randrange(4, 25)} '
               f'energy={rng.uniform(-500, -1):.8f} force={rng.uniform(0, 1):.7f} '
               f'residual={rng.uniform(0, .01):.9f} digest={rng.getrandbits(64):016x}\n')
        rows.append(row)
        length += len(row)
    pool = ''.join(rows)
    lower, upper = 1, len(pool)
    best = None
    while lower <= upper:
        midpoint = (lower + upper) // 2
        history = _history(pool[:midpoint], kind)
        size = estimate_input(history)
        if size <= target:
            best = history
            lower = midpoint + 1
        else:
            upper = midpoint - 1
    if best is None:
        raise ValueError('目标小于合成消息框架开销。')
    return best


def classify_error(exc):
    """Classify structural metadata only; never store provider error prose."""
    from vasp_agent.models.request_errors import _cause_text
    from vasp_agent.models.reconnect import _delay
    status = getattr(exc, 'status_code', None)
    body = getattr(exc, 'body', None)
    error = body.get('error', body) if isinstance(body, dict) else {}
    code = error.get('code') if isinstance(error, dict) else None
    capacity_codes = {'context_length_exceeded', 'context_window_exceeded', 'input_too_long'}
    quota_codes = {'insufficient_quota', 'billing_hard_limit_reached'}
    if isinstance(code, str) and code in capacity_codes:
        category = 'explicit_context_limit'
    elif isinstance(code, str) and code in quota_codes:
        category = 'quota'
    elif status == 429:
        category = 'rate_limit'
    elif status == 413:
        category = 'request_body_limit'
    elif status in (401, 403):
        category = 'authentication'
    elif status in (404, 405, 501):
        category = 'endpoint_or_model_unsupported'
    elif isinstance(status, int) and status >= 500:
        category = 'gateway_or_upstream'
    elif isinstance(status, int):
        category = 'request_rejected'
    else:
        names = {c.__name__ for c in type(exc).__mro__}
        category = 'timeout' if 'APITimeoutError' in names or isinstance(exc, TimeoutError) else 'transport_or_sdk'
    values = []
    response = getattr(exc, 'response', None)
    headers = getattr(response, 'headers', {})
    for key, value in headers.items():
        if str(key).lower() == 'retry-after':
            values.append(value)
    if isinstance(error, dict):
        values.append(error.get('retry_after'))
    if isinstance(body, dict):
        values.append(body.get('retry_after'))
    now = datetime.now(timezone.utc)
    delays = [delay for value in values if (delay := _delay(value, now)) is not None]
    return {'category': category, 'http_status': status if type(status) is int else None,
            'error_code': code if isinstance(code, str) and code in capacity_codes | quota_codes else None,
            'cause': _cause_text(exc) if status is None else None,
            'retry_after_seconds': max(delays, default=0)}


def run_trial(client, model, history, target, repeat, purpose='sweep'):
    from vasp_agent.models.context_compaction import CompactionError, validate_native_output
    row = {'purpose': purpose, 'target_estimated_tokens': target, 'repeat': repeat,
           'estimated_input_tokens': estimate_input(history), 'message_items': len(history),
           'request_bytes': len(json.dumps({'model': model, 'instructions': INSTRUCTIONS,
                                          'input': history}, ensure_ascii=False).encode()),
           'ok': False, 'actual_input_tokens': None, 'actual_output_tokens': None}
    started = time.monotonic()
    try:
        result = client.responses.compact(model=model, instructions=INSTRUCTIONS, input=history)
        output = [item.model_dump(mode='json', exclude_none=True) if hasattr(item, 'model_dump') else item
                  for item in getattr(result, 'output', [])]
        usage = getattr(result, 'usage', None)
        for field in ('input_tokens', 'output_tokens'):
            value = getattr(usage, field, None)
            if type(value) is int and value >= 0:
                row['actual_' + field] = value
        if getattr(result, 'object', None) != 'response.compaction':
            raise CompactionError('wrong_compaction_object')
        validate_native_output(output)
        row.update(ok=True, category='success', http_status=200,
                   returned_items=len(output),
                   returned_json_bytes=len(json.dumps(output, ensure_ascii=False).encode()))
    except CompactionError:
        row.update(category='invalid_compaction_for_agent', http_status=200)
    except Exception as exc:
        row.update(classify_error(exc))
    row['elapsed_seconds'] = round(time.monotonic() - started, 2)
    return row


def summarize(rows, sizes, repeats):
    tiers = []
    for size in sizes:
        trials = [row for row in rows if row['purpose'] == 'sweep' and row['target_estimated_tokens'] == size]
        actual = [row['actual_input_tokens'] for row in trials if row.get('actual_input_tokens') is not None]
        passed = sum(row['ok'] for row in trials)
        tiers.append({'target_estimated_tokens': size, 'trials': len(trials), 'passed': passed,
                      'all_planned_trials_passed': len(trials) == repeats and passed == repeats,
                      'actual_input_tokens_min': min(actual, default=None),
                      'actual_input_tokens_max': max(actual, default=None),
                      'median_seconds': round(statistics.median(row['elapsed_seconds'] for row in trials), 2) if trials else None,
                      'outcomes': dict(Counter(row['category'] for row in trials))})
    passed_sizes = [tier['target_estimated_tokens'] for tier in tiers if tier['all_planned_trials_passed']]
    explicit = [row['target_estimated_tokens'] for row in rows if row.get('category') == 'explicit_context_limit']
    transient = any(row.get('category') in ('rate_limit', 'gateway_or_upstream', 'timeout', 'transport_or_sdk') for row in rows)
    return {'tiers': tiers, 'largest_all_passed_estimated_size': max(passed_sizes, default=None),
            'smallest_explicit_limit_estimated_size': min(explicit, default=None),
            'transient_failures_observed': transient,
            'conclusion': ('服务端明确报告上下文限制；结合通过档位判断区间。' if explicit else
                           '未证实上下文容量边界；429、502和连接错误不能单独判为超长。'),
            'caveat': '各档是UTF-8/3估算值；实际用量以API usage为准。有限次数全通过不保证未来永不报错。'}


def save_report(folder, metadata, rows, sizes, repeats, stop_reason):
    folder.mkdir(parents=True, exist_ok=True)
    summary = summarize(rows, sizes, repeats)
    report = {'metadata': metadata, 'stop_reason': stop_reason, 'summary': summary, 'attempts': rows}
    (folder / 'results.json').write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
    fields = sorted({key for row in rows for key in row})
    if fields:
        with (folder / 'attempts.csv').open('w', encoding='utf-8-sig', newline='') as stream:
            writer = csv.DictWriter(stream, fieldnames=fields)
            writer.writeheader()
            writer.writerows(rows)
    lines = ['# 原生压缩容量测试', '', f"接口：{metadata['base_url']}；模型：{metadata['model']}", '',
             '合成历史，不读取真实对话、项目数据或日志，不执行工具。', '',
             '| 输入估算 | 通过/测试 | API实际输入范围 | 中位耗时 | 结果 |',
             '|---:|---:|---:|---:|---|']
    for tier in summary['tiers']:
        actual = f"{tier['actual_input_tokens_min']}–{tier['actual_input_tokens_max']}" if tier['actual_input_tokens_min'] is not None else '未报告'
        lines.append(f"| {tier['target_estimated_tokens']:,} | {tier['passed']}/{tier['trials']} | {actual} | {tier['median_seconds']} | {tier['outcomes']} |")
    lines.extend(['', summary['conclusion'], '', summary['caveat'], '', f'停止原因：{stop_reason}', '',
                  '官方依据：[Compaction](https://developers.openai.com/api/docs/guides/compaction)、'
                  '[Rate limits](https://developers.openai.com/api/docs/guides/rate-limits)。'])
    (folder / 'report.md').write_text('\n'.join(lines) + '\n', encoding='utf-8')


def wait(seconds):
    until = time.monotonic() + seconds
    notice = 0
    while (remaining := until - time.monotonic()) > 0:
        if time.monotonic() >= notice:
            print(f'等待 {remaining:.0f} 秒后继续；Ctrl+C 保留已有结果并停止。', flush=True)
            notice = time.monotonic() + 15
        time.sleep(min(1, remaining))


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--workspace', default='.')
    parser.add_argument('--profile', default='relay')
    parser.add_argument('--config')
    parser.add_argument('--env-file')
    parser.add_argument('--model')
    parser.add_argument('--base-url')
    parser.add_argument('--api-key-env')
    parser.add_argument('--sizes', type=parse_sizes, default=parse_sizes(DEFAULT_SIZES))
    parser.add_argument('--repeats', type=int, default=2, help='每档独立测试次数；偶数轮按降序测试以减少时间偏差')
    parser.add_argument('--content', choices=['mixed', 'messages'], default='mixed')
    parser.add_argument('--timeout', type=float, default=120)
    parser.add_argument('--interval', type=float, default=20)
    parser.add_argument('--cooldown', type=float, default=60)
    parser.add_argument('--max-cooldown', type=float, default=180)
    parser.add_argument('--max-requests', type=int, default=24)
    parser.add_argument('--max-input-estimate', type=int, default=3_000_000)
    parser.add_argument('--output-dir')
    parser.add_argument('--dry-run', action='store_true')
    args = parser.parse_args(argv)
    if not 1 <= args.repeats <= 10 or not 1 <= args.max_requests <= 100 or args.max_input_estimate < 1000:
        parser.error('repeats、max-requests 或 max-input-estimate 超出范围。')
    if not 1 <= args.timeout <= 1800 or min(args.interval, args.cooldown) < 0 or args.max_cooldown < args.cooldown:
        parser.error('超时和等待设置无效。')
    from vasp_agent.models.model_config import resolve_config
    config = resolve_config(args)
    if config.protocol != 'responses':
        parser.error('原生压缩测试要求所选profile使用responses；不会切换协议。')
    stamp = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ')
    from vasp_agent.common import agent_paths
    folder = Path(args.output_dir).resolve() if args.output_dir else agent_paths.cache_dir(args.workspace, 'compaction-probe', stamp)
    if (folder / 'results.json').exists():
        parser.error('输出目录已有结果，请使用新目录，避免覆盖实验。')
    metadata = {'base_url': config.base_url, 'model': config.model, 'profile': config.profile,
                'configured_context_window': config.context_window,
                'configured_input_budget': config.context_window - config.output_reserve - config.context_safety,
                'sizes': args.sizes, 'repeats': args.repeats, 'content': args.content,
                'size_unit': 'project_estimate_utf8_bytes_divided_by_3',
                'sdk_retries': 0, 'started_utc': stamp, 'timeout_seconds': args.timeout,
                'planned_input_estimate': sum(args.sizes) * args.repeats}
    print(json.dumps(metadata, ensure_ascii=False), flush=True)
    print('测试使用合成历史；实际调用可能收费。所有失败都会保留，SDK不自动重试。', flush=True)
    rows = []
    if args.dry_run:
        for size in args.sizes:
            history = build_history(size, kind=args.content)
            print(f'计划 {size:,}：估算 {estimate_input(history):,}，{len(history)} 个输入项目。', flush=True)
        save_report(folder, metadata, rows, args.sizes, args.repeats, 'dry_run_no_network')
        print('报告：', folder, flush=True)
        return 0
    if not config.api_key:
        parser.error(f'请配置 {config.api_key_env}；无需在命令中提供密钥正文。')
    from openai import OpenAI
    from vasp_agent.models.api_client import create_client
    client = create_client(config, args.timeout)
    stop_reason = 'completed'
    spent = 0
    capacity_bound = None

    def attempt(size, repeat, purpose):
        nonlocal spent
        if len(rows) >= args.max_requests or spent + size > args.max_input_estimate:
            return None
        history = build_history(size, seed=1700 + repeat, kind=args.content)
        spent += estimate_input(history)
        print(f'{purpose}：约 {size:,} tokens，第 {repeat} 次，正在请求原生压缩……', flush=True)
        row = run_trial(client, config.model, history, size, repeat, purpose)
        rows.append(row)
        print(f"结果 {row['category']}；HTTP {row.get('http_status')}；耗时 {row['elapsed_seconds']}秒；"
              f"API输入 {row.get('actual_input_tokens')}", flush=True)
        save_report(folder, metadata, rows, args.sizes, args.repeats, 'running')
        return row

    try:
        for repeat in range(1, args.repeats + 1):
            sizes = args.sizes if repeat % 2 else list(reversed(args.sizes))
            for size in sizes:
                if capacity_bound is not None and size >= capacity_bound:
                    continue
                if rows:
                    wait(args.interval)
                row = attempt(size, repeat, 'sweep')
                if row is None:
                    stop_reason = 'request_or_input_budget_reached'
                    break
                if row['category'] == 'explicit_context_limit':
                    capacity_bound = size
                elif row['category'] in ('authentication', 'quota', 'endpoint_or_model_unsupported'):
                    stop_reason = row['category']
                    break
                elif row['category'] in ('rate_limit', 'gateway_or_upstream', 'timeout', 'transport_or_sdk'):
                    control_size = min(8000, min(args.sizes))
                    if len(rows) >= args.max_requests or spent + control_size > args.max_input_estimate:
                        stop_reason = 'request_or_input_budget_reached'
                        break
                    delay = max(args.cooldown, row.get('retry_after_seconds', 0))
                    if delay > args.max_cooldown:
                        stop_reason = 'server_wait_exceeds_cooldown_budget'
                        break
                    wait(delay)
                    control = attempt(control_size, repeat, 'small_control')
                    if control is None:
                        stop_reason = 'request_or_input_budget_reached'
                        break
                    if not control['ok']:
                        stop_reason = 'small_control_also_failed_capacity_not_established'
                        break
            if stop_reason != 'completed':
                break
    except KeyboardInterrupt:
        stop_reason = 'cancelled_partial_results_preserved'
    finally:
        client.close()
        save_report(folder, metadata, rows, args.sizes, args.repeats, stop_reason)
    print(summarize(rows, args.sizes, args.repeats)['conclusion'], flush=True)
    print('报告：', folder, flush=True)
    return 0 if stop_reason == 'completed' else 2


if __name__ == '__main__':
    raise SystemExit(main())
