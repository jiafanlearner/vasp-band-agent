"""Agent mixin for context budget, compaction, capability learning, and usage/cache telemetry."""
from __future__ import annotations

import json
import sys
from time import monotonic as _default_monotonic
from uuid import uuid4

import vasp_agent.workflow.vasp_band_agent as core
from vasp_agent.chat.prompts import INSTRUCTIONS
from vasp_agent.chat.toolbox_core import item_field, output_items, scrub


def _monotonic():
    facade = sys.modules.get('vasp_agent.chat.ai_agent')
    fn = getattr(facade, 'monotonic', None) if facade is not None else None
    return fn() if callable(fn) else _default_monotonic()


class AgentCompactionMixin:
    def context_instructions(self):
        return INSTRUCTIONS

    def live_history(self):
        """History items still sent verbatim: those after the cached compaction cut."""
        from vasp_agent.models.context_compaction import digest
        cache = self.compactor.cache
        end = cache.get('end') if isinstance(cache, dict) else None
        if type(end) is int and 0 < end <= len(self.history) and cache.get('digest') == digest(self.history[:end]):
            return self.history[end:]
        return self.history

    def runtime_state(self):
        allowed = {t['name'] for t in self.tools.schemas()}
        state = {**self.tools.permission_status(),
                 'disabled_tools': sorted(t['name'] for t in self.tools.model_schemas() if t['name'] not in allowed)}
        return {'role': 'developer', 'content': '执行权限状态更新（程序生成，以最新一条为准）：\n' +
                json.dumps(state, ensure_ascii=False, sort_keys=True, separators=(',', ':'))}

    def sync_runtime_state(self):
        state = self.runtime_state()
        if state != self._last_runtime_state:
            # Compare with freshly computed authority, never restore permissions
            # from history. A restart with identical state needs no duplicate.
            latest = next((item for item in reversed(self.history) if item.get('role') == 'developer'
                           and str(item.get('content', '')).startswith('执行权限状态更新')), None)
            if latest != state:
                self.history.append(state)
                core.save(self.file, self.history)
            self._last_runtime_state = state

    def current_context(self, context, persist=False):
        # Compaction may have summarized an older state message. Restore the
        # authoritative snapshot at the tail, once, never by rewriting the prefix.
        state = self.runtime_state()
        latest = next((item for item in reversed(context) if item.get('role') == 'developer'
                       and str(item.get('content', '')).startswith('执行权限状态更新')), None)
        if latest != state:
            if persist:
                self.history.append(state)
                self._last_runtime_state = state
                core.save(self.file, self.history)
            return context + [state]
        return context

    def cache_request(self, instructions, history, schemas, kind='conversation'):
        adapter_client = getattr(self.adapter, 'client', self.client)
        settings = {'adapter': type(self.adapter).__name__, 'endpoint': str(getattr(adapter_client, 'base_url', '')),
                    'output_limit': getattr(self.adapter, 'output_limit', 4000),
                    'protocol': getattr(self.adapter, 'protocol', None)}
        return self.cache_telemetry.begin(self.model, instructions, history, schemas, kind, settings)

    def refresh_context_budget(self):
        if self.context_budget:
            self.context_budget.overhead = self.context_budget.estimate([self.context_instructions(), self.tools.model_schemas()])
            # Reserve room for the summary and the live instructions/tool schemas.
            self.compactor.recent_budget = min(self.recent_context_tokens, max(0,
                self.compactor.target - self.context_budget.overhead - 4000))

    def context_status(self):
        self.refresh_context_budget()
        context = self.compactor.context(self.history, self.summarize_context, lambda _: None, preview=True)
        context = self.current_context(context)
        if self.context_budget:
            return {**self.context_budget.status(context),
                    'compaction_mode': 'responses_native' if self.compactor.native else 'text_summary',
                    'summary_format': None if self.compactor.native else 'continuation_v1（失败时 vasp_handoff_v1 / 机械压缩）',
                    'recent_context_tokens': self.recent_context_tokens,
                    'effective_recent_budget': self.compactor.recent_budget,
                    'recent_note': '按预算保留完整用户轮次及工具配对；至少保留最新一轮，单轮过大时可能超预算。',
                    'native_state_note': '不透明压缩状态的字符串长度不等于实际token数，容量仍为估算。' if self.compactor.native else None}
        return {'mode': 'legacy_characters', 'compaction_mode': 'responses_native' if self.compactor.native else 'text_summary',
                'characters': len(json.dumps(context, ensure_ascii=False)),
                'threshold': self.compactor.limit, 'auto_compaction': bool(self.compactor.limit)}

    def compact_context(self, focus=None):
        self.refresh_context_budget()
        before = self.compactor.cache
        limit = self.compactor.limit
        try:
            if not limit:
                self.compactor.limit = 60000
            self.compactor.focus = scrub(focus)[:1000] if focus else None
            self.compactor.context(self.history, self.summarize_context,
                                   lambda text: self.display_event('activity', text), force=True)
        finally:
            self.compactor.limit = limit
            self.compactor.focus = None
        return {'compacted': self.compactor.cache is not before, 'context': self.context_status(),
                'skipped_reason': self.compactor.last_skip_reason,
                'last_compaction_error': None if self.compactor.last_skip_reason else self.compactor.last_error,
                'note': '仅在有足够旧内容时调用模型；原始记录保留。内容很少、没有合适旧轮次或摘要失败时不会强行删除内容。'}

    def compact_native_context(self, history):
        from vasp_agent.models.context_compaction import CompactionError, failure_detail, validate_native_output
        from vasp_agent.models.reconnect import retry_delay, wait_for_retry
        waited = 0
        for attempt in range(self.model_retries + 1):
            self.usage['requests'] += 1
            started = _monotonic()
            request = self.cache_request(self.context_instructions(), history, [], 'native_compaction')
            try:
                response = self.adapter.compact(self.model, self.context_instructions(), history)
            except Exception as exc:
                self.cache_telemetry.finish(request, error=exc)
                self.usage['failed_requests'] += 1
                delay = retry_delay(exc, attempt, 300 - waited) if attempt < self.model_retries else None
                if delay is None:
                    status = getattr(exc, 'status_code', None)
                    if status in (400, 404, 405, 422, 501):
                        raise CompactionError(f'原生压缩接口拒绝请求（HTTP {status}），请核查中转站/模型兼容性；未回退文字摘要') from exc
                    raise
                self.display_event('activity', f'原生上下文压缩：{failure_detail(exc, "native_request")}；'
                    f'本次请求 {_monotonic() - started:.1f} 秒；{delay} 秒后重试 {attempt + 1}/{self.model_retries}，Ctrl+C 取消')
                wait_for_retry(delay, lambda message: self.display_event('activity', '原生压缩 · ' + message))
                waited += delay
            else:
                break
        self.record_usage(response, request)
        if getattr(response, 'object', None) != 'response.compaction':
            raise CompactionError('接口没有返回response.compaction；未回退文字摘要')
        output = [item.model_dump(mode='json', exclude_none=True) if hasattr(item, 'model_dump') else item
                  for item in getattr(response, 'output', [])]
        return validate_native_output(output)

    def summarize_context(self, instructions, history):
        from vasp_agent.models.context_compaction import CompactionError
        if self.context_budget and self.context_budget.estimate([instructions, history]) > self.context_budget.available:
            raise CompactionError('待总结内容超出配置输入预算；请核实预算或新建对话')
        return self.summary_text(instructions, history)

    def summarize_conversation(self, items, focus=None):
        """Continuation summary: the live context plus one request."""
        from vasp_agent.models.context_compaction import CONTINUATION_PROMPT, CompactionError
        instructions, schemas = self.context_instructions(), self.tools.model_schemas()
        prompt = CONTINUATION_PROMPT + (f'\n用户对本次压缩的额外要求：{focus}' if focus else '')
        history = list(items) + [{'role': 'user', 'content': prompt}]
        if self.context_budget and self.context_budget.estimate([instructions, schemas, history]) > self.context_budget.available:
            raise CompactionError('当前上下文超过单次请求容量')
        tools = schemas if self.capability('tool_choice_none') != 'unsupported' else []
        return self.summary_text(instructions, history, tools)

    def summary_text(self, instructions, history, tools=None):
        """One summary reply as text, with the per-endpoint fallbacks applied."""
        from vasp_agent.models.context_compaction import CompactionError, SummaryTruncated
        effort = getattr(self, 'summary_reasoning', None)
        response = self.summary_attempt(instructions, history, effort, tools)
        usage = getattr(response, 'usage', None)
        reasoning = getattr(getattr(usage, 'output_tokens_details', None), 'reasoning_tokens', None)
        reason = getattr(getattr(response, 'incomplete_details', None), 'reason', None)
        text = (getattr(response, 'output_text', '') or '').strip()
        if (response.status != 'completed' and reason == 'max_output_tokens' and not text and effort != 'none'
                and self.capability('summary_reasoning') != 'unsupported'):
            self.display_event('activity', f'摘要请求的输出额度全部用于推理（{reasoning or "?"} tokens，没有摘要正文）；'
                                           '改为关闭推理重试一次')
            response = self.summary_attempt(instructions, history, 'none', tools)
            if self.summary_reasoning == 'none':
                self.remember_capability('summary_reasoning', 'none')
            usage = getattr(response, 'usage', None)
            reasoning = getattr(getattr(usage, 'output_tokens_details', None), 'reasoning_tokens', None)
            reason = getattr(getattr(response, 'incomplete_details', None), 'reason', None)
            text = (getattr(response, 'output_text', '') or '').strip()
        if response.status != 'completed':
            detail = (f'摘要响应未完成（{reason or response.status}；输出 '
                      f'{getattr(usage, "output_tokens", "?")} tokens，其中推理 {reasoning if reasoning is not None else "?"}）')
            raise (SummaryTruncated if reason == 'max_output_tokens' and text else CompactionError)(detail)
        if any(item_field(x, 'type') == 'function_call' for x in output_items(response)):
            raise CompactionError('摘要响应包含工具调用')
        return response.output_text

    def summary_attempt(self, instructions, history, effort, tools=None):
        """Send, stepping down on HTTP 400/422: first drop the tools, then the reasoning setting."""
        steps = [(tools or [], effort)]
        if tools:
            steps.append(([], effort))
        if effort:
            steps.append(([], None))
        for index, (step_tools, step_effort) in enumerate(steps):
            try:
                response = self.summary_request(instructions, history, step_effort, step_tools)
            except Exception as exc:
                if getattr(exc, 'status_code', None) not in (400, 422) or index == len(steps) - 1:
                    raise
                following = steps[index + 1]
                self.display_event('activity', '接口不接受' + ('工具定义/tool_choice' if step_tools and not following[0]
                                                               else '推理设置') + '参数，改为不带该参数重试')
                continue
            if tools and not step_tools:
                self.remember_capability('tool_choice_none', 'unsupported')
            if effort and not step_effort:
                self.summary_reasoning = None
                self.remember_capability('summary_reasoning', 'unsupported')
            elif step_effort:
                self.summary_reasoning = step_effort
            return response

    def capability(self, name):
        return self.capabilities.get(name)

    def remember_capability(self, name, value):
        """Persist what this endpoint accepts, so the next start skips failed attempts."""
        self.capabilities[name] = value
        if not self.capability_key:
            return
        from vasp_agent.common import agent_paths
        path = agent_paths.file_path(self.tools.workspace, 'model_capabilities')
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            data = json.loads(path.read_text(encoding='utf-8')) if path.is_file() else {}
            data = data if isinstance(data, dict) else {}
            data[self.capability_key] = {**data.get(self.capability_key, {}), name: value}
            temporary = path.with_name(path.name + '.' + uuid4().hex + '.tmp')
            try:
                temporary.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding='utf-8')
                temporary.replace(path)
            finally:
                temporary.unlink(missing_ok=True)
        except (OSError, ValueError):
            pass  # a cache only; the next process simply relearns it

    def summary_request(self, instructions, history, reasoning=None, tools=None):
        """One summary request, retried only for transient transport failures."""
        from vasp_agent.models.context_compaction import failure_detail
        from vasp_agent.models.reconnect import retry_delay, wait_for_retry
        tools = tools or []
        extra = {**({'reasoning': reasoning} if reasoning else {}), **({'tool_choice': 'none'} if tools else {})}
        waited = 0
        for attempt in range(self.model_retries + 1):
            self.usage['requests'] += 1
            started = _monotonic()
            request = self.cache_request(instructions, history, tools, 'summary')
            try:
                response = self.adapter.create(self.model, instructions, history, tools, **extra)
            except Exception as exc:
                self.cache_telemetry.finish(request, error=exc)
                self.usage['failed_requests'] += 1
                delay = retry_delay(exc, attempt, 300 - waited) if attempt < self.model_retries else None
                if delay is None:
                    raise
                self.display_event('activity', f'上下文压缩：{failure_detail(exc, "summary_request")}；'
                    f'本次请求 {_monotonic() - started:.1f} 秒；{delay} 秒后重试 {attempt + 1}/{self.model_retries}，Ctrl+C 取消')
                wait_for_retry(delay, lambda message: self.display_event('activity', '上下文压缩 · ' + message))
                waited += delay
            else:
                if attempt:
                    self.display_event('activity', '摘要接口连接已恢复，继续压缩上下文')
                break
        self.record_usage(response, request)
        return response

    def record_usage(self, response, request=None):
        reported = getattr(response, 'usage', None)
        if reported is not None:
            self.usage['responses_with_usage'] += 1
            self.usage['reported_input_tokens'] += getattr(reported, 'input_tokens', 0) or 0
            self.usage['reported_output_tokens'] += getattr(reported, 'output_tokens', 0) or 0
        from vasp_agent.models.prompt_cache import accumulate, cache_usage
        observed = cache_usage(response)
        accumulate(self.usage, observed, request['kind'] if request else 'other')
        if request:
            diagnostics = self.cache_telemetry.finish(request, response=response)
            self.usage['last_cache_request'] = diagnostics
            if request['kind'] == 'conversation':
                percent = observed['cache_read_percent']
                read = f"{observed['cached_tokens']:,}/{observed['input_tokens']:,} tokens · {percent}%" if percent is not None else '未知（接口未报告有效缓存读取比例）'
                write = str(observed['cache_write_tokens']) if observed['cache_write_tokens'] is not None else '未知'
                change = {'no_baseline': '首次记录', 'append_only': '仅追加',
                          'unchanged': '未变', 'prefix_rewritten': '已变化'}[diagnostics['history_change']]
                self.display_event('activity', f'缓存读取 {read}；缓存写入 {write}；历史前缀{change}')
