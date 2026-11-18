"""Opt-in diagnostic transport. Production adapter request construction is reused."""
import copy
import re
from types import SimpleNamespace

from vasp_agent.models.prompt_cache import cache_usage, fingerprint


def diagnostic_summary(value):
    """Retain bounded diagnostic enums/counts, never arbitrary provider prose."""
    if not isinstance(value, dict):
        return None
    result = {}
    for key, item in value.items():
        if not re.fullmatch(r'[a-z_]{1,64}', key):
            continue
        if item is None or type(item) in (int, bool):
            result[key] = item
        elif isinstance(item, str) and re.fullmatch(r'[a-z_]{1,80}', item):
            result[key] = item
        elif isinstance(item, dict):
            result[key] = diagnostic_summary(item)
        else:
            result[key + '_hash'] = fingerprint(item)
    return result


class ProbeTransport:
    def __init__(self, client, protocol, *, stream=False, reasoning=None,
                 diagnostics=False, cache_mode=None, breakpoint=False):
        if protocol != 'responses' and (diagnostics or breakpoint):
            raise ValueError('诊断比较和显式断点测试目前仅支持 Responses；不自动切换协议。')
        if cache_mode == 'explicit' and not breakpoint:
            raise ValueError('显式缓存模式需要 --breakpoint，避免没有缓存写入位置。')
        self.client, self.protocol = client, protocol
        self.options = dict(stream=stream, reasoning=reasoning, diagnostics=diagnostics,
                            cache_mode=cache_mode, breakpoint=breakpoint)
        self.previous_response_id = None
        self.last = {}
        self.responses = SimpleNamespace(create=self.create)
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=self.create))

    def create(self, **original):
        kwargs = copy.deepcopy(original)
        options = self.options
        if options['reasoning']:
            if self.protocol == 'responses':
                kwargs['reasoning'] = {'effort': options['reasoning']}
            else:
                kwargs['reasoning_effort'] = options['reasoning']
        cache = {}
        if options['cache_mode']:
            cache['mode'] = options['cache_mode']
        if options['diagnostics'] and self.previous_response_id:
            cache['comparison_response_id'] = self.previous_response_id
        if cache:
            kwargs['prompt_cache_options'] = cache
        if options['breakpoint']:
            # Synthetic first reference message only. Never modify the caller's history.
            first = kwargs['input'][0]
            first['content'] = [{'type': 'input_text', 'text': first['content'],
                                 'prompt_cache_breakpoint': {'mode': 'explicit'}}]
        if options['stream']:
            kwargs['stream'] = True
            if self.protocol == 'chat_completions':
                kwargs['stream_options'] = {'include_usage': True}
        self.last = {'request_parameters_hash': fingerprint(kwargs),
                     'comparison_requested': bool(cache.get('comparison_response_id'))}
        endpoint = self.client.responses if self.protocol == 'responses' else self.client.chat.completions
        if options['stream']:
            # SDK aggregation preserves final usage/reasoning and closes the stream.
            # A broken stream is a failed probe; it is never silently retried.
            manager_kwargs = {k: v for k, v in kwargs.items() if k != 'stream'}
            with endpoint.stream(**manager_kwargs) as events:
                result = events.get_final_response() if self.protocol == 'responses' else events.get_final_completion()
            raw = result.model_dump(mode='json', exclude_unset=True)
            source = 'stream_final_fields_received_by_sdk'
        else:
            response = endpoint.with_raw_response.create(**kwargs)
            raw = response.http_response.json()
            result = response.parse()
            source = 'http_json'
        raw = raw if isinstance(raw, dict) else {}
        usage = raw.get('usage')
        details_name = 'input_tokens_details' if self.protocol == 'responses' else 'prompt_tokens_details'
        details = usage.get(details_name) if isinstance(usage, dict) else None
        self.last.update({'usage_source': source, 'wire_usage': cache_usage(raw),
            'usage_present': 'usage' in raw,
            'cached_tokens_present': isinstance(details, dict) and 'cached_tokens' in details,
            'cache_write_tokens_present': isinstance(details, dict) and 'cache_write_tokens' in details,
            'diagnostics_present': 'prompt_cache_diagnostics' in raw,
            'cache_diagnostics': diagnostic_summary(raw.get('prompt_cache_diagnostics'))})
        identifier = raw.get('id')
        self.last['response_id_hash'] = fingerprint(identifier) if isinstance(identifier, str) else None
        if isinstance(identifier, str) and re.fullmatch(r'resp_[A-Za-z0-9_-]{1,200}', identifier):
            self.previous_response_id = identifier
        else:
            self.previous_response_id = None
        return result
