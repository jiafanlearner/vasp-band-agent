"""Core conversational Agent loop: initialization, tool execution, retry and output expansion."""
from __future__ import annotations

import json
import os
import re
import sys

import vasp_agent.workflow.vasp_band_agent as core
from vasp_agent.chat.agent_compaction import _monotonic
from vasp_agent.chat.tool_schemas import JOB
from vasp_agent.chat.toolbox_core import item_field, output_items, scrub
from vasp_agent.models.request_errors import request_failure_message, request_failure_summary


class AgentLoopMixin:
    EXPANDED_OUTPUT_CAP = 32000

    def __init__(self, client, model, toolbox, session='default', max_steps=10, adapter=None, ui=None, context_chars=None, context_config=None, model_retries=0):
        from vasp_agent.models.model_adapters import ResponsesAdapter
        from vasp_agent.ui.terminal_ui import TerminalUI
        self.ui = ui or TerminalUI()
        if not re.fullmatch(JOB['pattern'], session):
            raise ValueError('无效会话名称。')
        if not 1 <= max_steps <= 30:
            raise ValueError('max_steps 必须在 1 到 30 之间。')
        self.client, self.model, self.tools = client, model, toolbox
        self.session = session
        self.tools.session_id = session
        os.environ['VASP_AGENT_SESSION'] = session
        self.tools.local_analysis_mode = 'ask'
        self.tools.review_model = model
        # Deliberate: reviews are recorded under review_model, so a model or
        # conversation switch must re-read evidence before signing a review.
        self.tools.review_snapshots.clear()
        self.adapter = adapter or ResponsesAdapter(client)
        self.max_steps = max_steps
        if type(model_retries) is not int or not 0 <= model_retries <= 5:
            raise ValueError('model_retries 必须为 0–5。')
        self.model_retries = model_retries
        self.usage = {'requests': 0, 'failed_requests': 0, 'responses_with_usage': 0,
                      'reported_input_tokens': 0, 'reported_output_tokens': 0,
                      'note': '本次启动累计；无 usage 返回的请求不计入 token 总数，不估算费用。连接测试不计入此处。'}
        from vasp_agent.common import agent_paths
        self.file = agent_paths.session_file(toolbox.workspace, session)
        if self.file.parent == agent_paths.session_dir(toolbox.workspace):
            self.file.parent.mkdir(parents=True, exist_ok=True)
        self.session_lock = None
        self.history = json.loads(self.file.read_text(encoding='utf-8')) if self.file.exists() else []
        from vasp_agent.models.prompt_cache import CacheTelemetry
        self.cache_telemetry = CacheTelemetry(self.file.with_suffix('.cache-metrics.jsonl'))
        self._last_runtime_state = None
        from vasp_agent.models.context_compaction import ContextCompactor
        self.context_budget = None
        if context_chars is None:
            from vasp_agent.models.context_budget import ContextBudget
            cfg = context_config
            self.context_budget = ContextBudget(
                getattr(cfg, 'context_window', 32768), getattr(cfg, 'output_reserve', 4000),
                getattr(cfg, 'context_safety', 2048), getattr(cfg, 'compact_trigger', .8), getattr(cfg, 'compact_target', .55))
            context_chars = int(self.context_budget.available * self.context_budget.trigger)
            if hasattr(self.adapter, 'client'):
                self.adapter.output_limit = self.context_budget.output
        self.compactor = ContextCompactor(self.file.with_suffix('.context.json'), context_chars)
        self.compactor.evidence_item = lambda name: agent_paths.store_item(toolbox.workspace, 'evidence', name)
        self.compactor.references = self.compaction_references
        self.tools.guide_history = self.live_history
        self.tools.tool_group_history = self.live_history
        mode = getattr(context_config, 'compaction_mode', 'summary')
        if mode not in ('summary', 'native'):
            raise ValueError('compaction_mode 必须为 summary 或 native。')
        if mode == 'summary':
            self.compactor.conversation = self.summarize_conversation
        if mode == 'native':
            if not callable(getattr(self.adapter, 'compact', None)):
                raise ValueError('当前适配器不支持 native 压缩；请显式选择 summary。')
            from vasp_agent.models.context_compaction import digest
            self.compactor.native = self.compact_native_context
            self.compactor.native_scope = digest([self.model, str(getattr(getattr(self.adapter, 'client', client), 'base_url', '')),
                                                  'responses-native-v1'])
        if self.context_budget:
            self.compactor.measure = self.context_budget.measure
            self.compactor.measure_content = self.context_budget.estimate
            self.compactor.minimum_source = 1024
            self.compactor.measure_request = self.context_budget.estimate
            self.compactor.request_limit = min(32000, self.context_budget.available)
            self.compactor.target = int(self.context_budget.available * self.context_budget.target)
            self.compactor.unit = '估算 tokens'
            self.recent_context_tokens = getattr(context_config, 'recent_context_tokens', 12000)
            self.compactor.recent_budget = self.recent_context_tokens
        identity = getattr(context_config, 'identity', None)
        self.capability_key = identity() if callable(identity) else None
        self.capabilities = {}
        try:
            stored = json.loads(agent_paths.file_path(self.tools.workspace, 'model_capabilities').read_text(encoding='utf-8'))
            if self.capability_key and isinstance(stored.get(self.capability_key), dict):
                self.capabilities = stored[self.capability_key]
        except (OSError, ValueError, AttributeError):
            pass
        learned = self.capabilities.get('summary_reasoning')
        self.summary_reasoning = (getattr(context_config, 'summary_reasoning_effort', None)
                                  or (learned if learned not in (None, 'unsupported') else None))

    def hold_session(self):
        """Mark this conversation open (released by release_session or process exit)."""
        from vasp_agent.common import agent_paths
        if self.session_lock is None:
            self.session_lock = agent_paths.hold_session(self.tools.workspace, self.session)
        return self.session_lock is not None

    def release_session(self):
        lock, self.session_lock = self.session_lock, None
        if lock is not None:
            lock.release()

    def ask(self, message=None, operator=True):
        if message is not None:
            if operator:
                # Only what the operator typed may later count as an operator statement.
                self.tools.latest_user_message = scrub(message)[:6000]
            self.history.append({'role': 'user', 'content': message})
        if not self.history:
            return '没有可继续的对话，请先输入任务。'
        core.save(self.file, self.history)
        recovery_attempted = set()
        for step in range(self.max_steps):
            self.sync_runtime_state()
            self.refresh_context_budget()
            context = self.compactor.context(self.history, self.summarize_context,
                                             lambda text: self.display_event('activity', text))
            context = self.current_context(context, persist=True)
            if self.context_budget:
                info = self.context_budget.status(context)
                self.display_event('activity', f"上下文约 {info['estimated_input_tokens']:,}/{info['input_budget']:,} tokens · {info['input_budget_percent']}% 输入预算")
                if info['estimated_input_tokens'] > info['input_budget']:
                    return '上下文估算超过输入预算，已停止本次请求。请用 /compact、/new，或核实模型容量后调整配置；完整记录保留。'
            self.usage['requests'] += 1
            self.display_event('activity', f'等待模型响应 · 第 {step + 1} 轮')
            instructions, schemas = self.context_instructions(), self.tools.model_schemas()
            request_client = getattr(self.adapter, 'client', self.client)
            diagnostics = {}
            if hasattr(request_client, 'timeout'):
                diagnostics['timeout'] = request_client.timeout
            from vasp_agent.models.reconnect import retry_delay, wait_for_retry
            waited = 0
            for attempt in range(self.model_retries + 1):
                started = _monotonic()
                request = self.cache_request(instructions, context, schemas)
                try:
                    response = self.adapter.create(self.model, instructions, context, schemas)
                except KeyboardInterrupt:
                    core.save(self.file, self.history)
                    return '已取消本次模型请求；对话及已完成工具结果已保存。可用 /retry 继续。'
                except Exception as exc:
                    self.cache_telemetry.finish(request, error=exc)
                    diagnostics['elapsed_seconds'] = _monotonic() - started
                    self.usage['failed_requests'] += 1
                    recovery = request_failure_message(exc, **diagnostics)
                    if recovery is None:
                        raise
                    core.save(self.file, self.history)
                    delay = retry_delay(exc, attempt, 300 - waited) if attempt < self.model_retries else None
                    if delay is None:
                        if attempt:
                            recovery = recovery.replace('程序没有自动重试。', f'已自动重试 {attempt} 次，现已停止。')
                        elif self.model_retries:
                            recovery += ' 本次错误不适合自动重试，或服务端要求的等待时间超过剩余等待预算。'
                        return recovery
                    reason = request_failure_summary(exc) or '模型接口异常'
                    self.display_event('activity', f'模型请求失败 · {reason} · 本次请求 {diagnostics["elapsed_seconds"]:.1f} 秒后失败；'
                        f'{delay} 秒后重新连接 {attempt + 1}/{self.model_retries} · Ctrl+C 取消等待')
                    try:
                        wait_for_retry(delay, lambda message: self.display_event('activity', message))
                    except KeyboardInterrupt:
                        return '已取消自动重连；对话及已完成工具结果已保存。可用 /retry 继续。'
                    waited += delay
                    self.usage['requests'] += 1
                    self.display_event('activity', f'正在重新连接 {attempt + 1}/{self.model_retries}，等待模型响应')
                else:
                    if attempt:
                        self.display_event('activity', '模型连接已恢复，继续当前请求')
                    break
            self.record_usage(response, request)
            if self.context_budget:
                self.context_budget.calibrate(context, [instructions, schemas],
                    getattr(getattr(response, 'usage', None), 'input_tokens', None))
            if response.status != 'completed':
                response = self.retry_with_more_output(response, instructions, context, schemas)
            if response.status != 'completed':
                return self.incomplete_message(response)
            from vasp_agent.editing.workspace_editor import EDIT_TOOLS, safe_value
            items = output_items(response)
            for item in items:
                dump = getattr(item, 'model_dump', None)
                saved = dump(mode='json', exclude_none=True) if callable(dump) else dict(item) if isinstance(item, dict) else None
                if not isinstance(saved, dict):
                    continue
                if saved.get('type') == 'function_call' and saved.get('name') in EDIT_TOOLS:
                    try:
                        saved['arguments'] = json.dumps(safe_value(json.loads(saved['arguments'])), ensure_ascii=False)
                    except (ValueError, TypeError):
                        saved['arguments'] = '{}'
                self.history.append(saved)
            calls = [item for item in items if item_field(item, 'type') == 'function_call'
                     and isinstance(item_field(item, 'call_id'), str)]
            if calls and response.output_text:
                self.display_event('commentary', response.output_text)
            for call in calls:
                name, call_id = item_field(call, 'name'), item_field(call, 'call_id')
                try:
                    arguments = json.loads(item_field(call, 'arguments') or '{}')
                    if not isinstance(arguments, dict):
                        raise ValueError('工具参数必须是 JSON 对象。')
                    output = self.execute_tool(name, arguments, call_id=call_id)
                    action = output.get('next_action')
                    retry = action.get('arguments') if isinstance(action, dict) else None
                    if (isinstance(retry, dict)
                            and output.get('error_code') == 'METADATA_READ_REQUIRES_SPECIALIZED_TOOL'
                            and action.get('tool') == 'get_input_review'
                            and action.get('requires_user_input') is False):
                        key = json.dumps(retry, sort_keys=True)
                        if key not in recovery_attempted and len(recovery_attempted) < 2:
                            recovery_attempted.add(key)
                            recovered = self.execute_tool('get_input_review', retry,
                                                          'get_input_review (只读恢复)')
                            output['recovery'] = {'tool': 'get_input_review', 'arguments': retry, 'output': recovered}
                        else:
                            output['recovery'] = {'skipped': True, 'reason': '本轮只读恢复次数已达上限；不得重复同一读取。'}
                except Exception as exc:
                    output = {'ok': False, 'error': scrub(str(exc)) or type(exc).__name__}
                    self.display_event('tool', str(name), False)
                self.history.append({'type': 'function_call_output', 'call_id': call_id,
                                     'output': json.dumps(self.model_output(name, output), ensure_ascii=False, default=str)})
            core.save(self.file, self.history)
            if not calls:
                if response.status != 'completed':
                    return '模型响应未完成，未确认任务成功；可继续提问或查看状态。'
                return response.output_text or '模型未返回文字，请查看工具记录后继续。'
        return '本轮已达到工具调用循环上限；执行记录已保存，请查看状态后继续。'

    def retry_with_more_output(self, response, instructions, context, schemas):
        """One retry with a larger output limit when a turn was cut off by it."""
        reason = getattr(getattr(response, 'incomplete_details', None), 'reason', None)
        current = getattr(self.adapter, 'output_limit', None)
        if reason != 'max_output_tokens' or not self.context_budget or not current:
            return response
        budget = self.context_budget
        room = budget.window - budget.estimate([instructions, schemas, context]) - budget.safety
        larger = min(self.EXPANDED_OUTPUT_CAP, room)
        if larger <= current * 1.25:
            return response
        usage = getattr(response, 'usage', None)
        reasoning = getattr(getattr(usage, 'output_tokens_details', None), 'reasoning_tokens', None)
        self.display_event('activity', f'输出达到 {current} tokens 上限（其中推理 {reasoning if reasoning is not None else "?"}），'
                                       f'未执行任何工具；以 {larger} tokens 上限重试一次')
        self.adapter.output_limit = larger
        self.usage['requests'] += 1
        request = self.cache_request(instructions, context, schemas)
        try:
            retried = self.adapter.create(self.model, instructions, context, schemas)
        except Exception as exc:
            self.cache_telemetry.finish(request, error=exc)
            self.usage['failed_requests'] += 1
            self.display_event('activity', f'放宽输出上限的重试未成功（{request_failure_summary(exc) or type(exc).__name__}）')
            return response
        finally:
            self.adapter.output_limit = current
        self.record_usage(retried, request)
        return retried

    def incomplete_message(self, response):
        reason = getattr(getattr(response, 'incomplete_details', None), 'reason', None) or response.status
        usage = getattr(response, 'usage', None)
        reasoning = getattr(getattr(usage, 'output_tokens_details', None), 'reasoning_tokens', None)
        tokens = (f'输出 {getattr(usage, "output_tokens", "?")} tokens，其中推理 {reasoning}'
                  if reasoning is not None else f'输出 {getattr(usage, "output_tokens", "?")} tokens')
        hint = ('可在 models.toml 中调大该 profile 的 output_reserve，或请模型把脚本拆成较小的几步；' if reason == 'max_output_tokens' else '')
        return f'模型响应未完成（{reason}；{tokens}），没有执行本次返回的工具；{hint}请重试或查看状态。'

    def model_output(self, name, output):
        """The history copy of a tool output: a compact view of bulky results."""
        from vasp_agent.chat.model_views import model_output
        try:
            return model_output(name, output, self.tools.store_evidence)
        except Exception:
            return output

    def execute_tool(self, name, arguments, label=None, call_id=None):
        """Report actual execution boundaries; never infer success from tool names."""
        label = label or name
        detailed = hasattr(self.ui, 'tool_started') and hasattr(self.ui, 'tool_finished')
        from vasp_agent.editing.workspace_editor import EDIT_TOOLS, safe_value
        shown = safe_value(arguments) if name in EDIT_TOOLS else arguments
        ticket = self.display_event('tool_started', label, shown) if detailed else None
        self.tools._edit_request_id = str(self.file) + ':' + call_id if call_id else None
        try:
            output = self.tools.call(name, arguments)
        except BaseException:
            if ticket is not None:
                self.display_event('tool_finished', ticket, {'ok': False, 'error': '执行中断；结果未知，请核对状态，不自动重放。'})
            raise
        finally:
            self.tools._edit_request_id = None
        if ticket is not None:
            self.display_event('tool_finished', ticket, output)
        else:
            self.display_event('tool', label, bool(output.get('ok')))
        return output

    def display_event(self, method, *args):
        handler = getattr(self.ui, method, None)
        if handler is not None:
            try:
                return handler(*args)
            except Exception:
                print('终端过程显示异常；工具结果仍按原流程保存。', file=sys.stderr)
