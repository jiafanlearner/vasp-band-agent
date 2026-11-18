"""Cache usage and local prefix diagnostics. Never persists prompt text or keys."""
import hashlib
import json
from collections.abc import Mapping
from datetime import datetime, timezone


def field(value, name, default=None):
    return value.get(name, default) if isinstance(value, Mapping) else getattr(value, name, default)


def count(value):
    return value if type(value) is int and value >= 0 else None


def fingerprint(value):
    raw = json.dumps(value, ensure_ascii=False, separators=(',', ':'), default=str)
    return hashlib.sha256(raw.encode()).hexdigest()


def cache_usage(response):
    usage = field(response, 'usage')
    details = field(usage, 'input_tokens_details')
    if details is None:
        details = field(usage, 'prompt_tokens_details')
    tokens = count(field(usage, 'input_tokens', field(usage, 'prompt_tokens')))
    cached = count(field(details, 'cached_tokens'))
    written = count(field(details, 'cache_write_tokens'))
    inconsistent = tokens is not None and cached is not None and cached > tokens
    return {'input_tokens': tokens, 'cached_tokens': cached, 'cache_write_tokens': written,
            'cache_read_percent': round(100 * cached / tokens, 2)
                if tokens and cached is not None and not inconsistent else None,
            'cache_read_reported': cached is not None, 'usage_inconsistent': inconsistent}


def accumulate(usage, observed, kind):
    groups = usage.setdefault('cache', {})
    for name in ('all', kind):
        group = groups.setdefault(name, {'responses': 0, 'responses_with_cache_read': 0,
            'responses_with_cache_write': 0, 'reported_cached_tokens': None,
            'reported_cache_write_tokens': None, 'input_tokens_with_cache_read': 0,
            'cache_read_percent': None, 'inconsistent_responses': 0})
        group['responses'] += 1
        if observed['usage_inconsistent']:
            group['inconsistent_responses'] += 1
            continue
        if observed['cached_tokens'] is not None and observed['input_tokens'] is not None:
            group['responses_with_cache_read'] += 1
            group['reported_cached_tokens'] = (group['reported_cached_tokens'] or 0) + observed['cached_tokens']
            group['input_tokens_with_cache_read'] += observed['input_tokens']
            denominator = group['input_tokens_with_cache_read']
            group['cache_read_percent'] = round(100 * group['reported_cached_tokens'] / denominator, 2) if denominator else None
        if observed['cache_write_tokens'] is not None:
            group['responses_with_cache_write'] += 1
            group['reported_cache_write_tokens'] = (group['reported_cache_write_tokens'] or 0) + observed['cache_write_tokens']
    usage['cache_note'] = ('缓存统计来自接口返回；null表示未知，不是0。累计命中率只使用同时报告输入和缓存读取量的响应；'
                           '不按官方价格推算中转站费用。all含摘要请求，conversation仅含对话请求。')


class CacheTelemetry:
    def __init__(self, path):
        self.path = path
        self.previous = {}  # Successful request baselines, separate from summaries.
        self.last = None

    def begin(self, model, instructions, history, tools, kind='conversation', settings=None):
        hashes = {'model_hash': fingerprint(model), 'instructions_hash': fingerprint(instructions),
                  'tools_hash': fingerprint(tools), 'settings_hash': fingerprint(settings or {})}
        items = [fingerprint(item) for item in history]
        old = self.previous.get(kind)
        common = 0
        if old:
            for left, right in zip(old['_items'], items):
                if left != right:
                    break
                common += 1
        changed = [name.removesuffix('_hash') for name in hashes if old and old[name] != hashes[name]]
        history_change = ('no_baseline' if old is None else
                          'append_only' if common == len(old['_items']) and len(items) > common else
                          'unchanged' if common == len(items) == len(old['_items']) else 'prefix_rewritten')
        return {'kind': kind, **hashes, 'stable_prefix_hash': fingerprint(hashes),
                'history_hash': fingerprint(items), 'history_items': len(items),
                'common_prefix_items': common if old else None, 'history_change': history_change,
                'changed_components': changed, 'request_hash': fingerprint([hashes, items]), '_items': items}

    def finish(self, request, response=None, error=None):
        record = {k: v for k, v in request.items() if k != '_items'}
        record['time'] = datetime.now(timezone.utc).isoformat()
        record['success'] = error is None
        if error is None:
            record.update(cache_usage(response))
            # Hash server-returned identifiers as well; no raw provider text.
            actual_model = field(response, 'model')
            tier = field(response, 'service_tier')
            record['returned_model_hash'] = fingerprint(actual_model) if isinstance(actual_model, str) else None
            record['returned_model_matches_requested'] = fingerprint(actual_model) == request['model_hash'] if isinstance(actual_model, str) else None
            record['service_tier_hash'] = fingerprint(tier) if isinstance(tier, str) else None
            old = self.previous.get(request['kind'])
            record['returned_model_changed'] = (record['returned_model_hash'] != old.get('returned_model_hash')) if old and record['returned_model_hash'] and old.get('returned_model_hash') else None
            record['service_tier_changed'] = (record['service_tier_hash'] != old.get('service_tier_hash')) if old and record['service_tier_hash'] and old.get('service_tier_hash') else None
            self.previous[request['kind']] = {**request, **record}
        else:
            record['error_type'] = type(error).__name__
            status = getattr(error, 'status_code', None)
            record['http_status'] = status if type(status) is int else None
        self.last = record
        try:
            with self.path.open('a', encoding='utf-8') as stream:
                stream.write(json.dumps(record, ensure_ascii=False) + '\n')
        except OSError:
            record['diagnostic_write_failed'] = True
        return record
