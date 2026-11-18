"""Responses and Chat Completions adapters sharing canonical tool events."""
import json
from types import SimpleNamespace


class Item(SimpleNamespace):
    def model_dump(self, **kwargs):
        return vars(self)


class ResponsesAdapter:
    protocol = 'responses'
    def __init__(self, client):
        self.client = client

    def compact(self, model, instructions, history):
        from vasp_agent.models.context_compaction import CompactionError
        method = getattr(self.client.responses, 'compact', None)
        if method is None:
            raise CompactionError('当前OpenAI SDK不支持responses.compact，请升级SDK；未回退文字摘要')
        return method(model=model, instructions=instructions, input=history)

    def create(self, model, instructions, history, tools, probe=False, reasoning=None, tool_choice=None):
        kwargs = dict(model=model, instructions=instructions, input=history, tools=tools,
                      parallel_tool_calls=False, store=False,
                      include=['reasoning.encrypted_content'], max_output_tokens=1024 if probe else getattr(self, "output_limit", 4000))
        if reasoning:
            kwargs['reasoning'] = {'effort': reasoning}
        if tool_choice and tools:
            kwargs['tool_choice'] = tool_choice
        if probe:
            kwargs['tool_choice'] = {'type': 'function', 'name': 'connection_probe'}
        return self.client.responses.create(**kwargs)


def chat_messages(history, instructions):
    messages = [{'role': 'system', 'content': instructions}]
    for item in history:
        kind = item.get('type')
        if kind == 'compaction':
            raise ValueError('原生压缩状态只能使用Responses协议，不能转换为Chat Completions。')
        if kind == 'reasoning':
            continue
        if kind == 'function_call':
            call = {'id': item['call_id'], 'type': 'function',
                    'function': {'name': item['name'], 'arguments': item['arguments']}}
            if messages[-1]['role'] == 'assistant' and messages[-1].get('tool_calls'):
                messages[-1]['tool_calls'].append(call)
            else:
                messages.append({'role': 'assistant', 'content': None, 'tool_calls': [call]})
        elif kind == 'function_call_output':
            messages.append({'role': 'tool', 'tool_call_id': item['call_id'], 'content': item['output']})
        else:
            content = item.get('content', '')
            if isinstance(content, list):
                content = '\n'.join(x.get('text', x.get('refusal', '')) for x in content)
            role = item.get('role', 'assistant')
            messages.append({'role': 'system' if role == 'developer' else role, 'content': content})
    return messages


class ChatCompletionsAdapter:
    protocol = 'chat_completions'
    def __init__(self, client):
        self.client = client

    def create(self, model, instructions, history, tools, probe=False, reasoning=None, tool_choice=None):
        converted = [{'type': 'function', 'function': {k: v for k, v in t.items() if k != 'type'}} for t in tools]
        kwargs = dict(model=model, messages=chat_messages(history, instructions), tools=converted,
                      max_tokens=1024 if probe else getattr(self, "output_limit", 4000))
        if reasoning:
            kwargs['reasoning_effort'] = reasoning
        if tool_choice and converted:
            kwargs['tool_choice'] = tool_choice
        if not converted:
            kwargs.pop('tools')
        if probe:
            kwargs['tool_choice'] = {'type': 'function', 'function': {'name': 'connection_probe'}}
        result = self.client.chat.completions.create(**kwargs)
        choice = result.choices[0]
        message = choice.message
        output = []
        if message.content:
            output.append(Item(type='message', role='assistant', content=[{'type': 'output_text', 'text': message.content}]))
        for call in message.tool_calls or []:
            if call.type != 'function':
                raise ValueError('接口返回不支持的工具类型。')
            output.append(Item(type='function_call', call_id=call.id, name=call.function.name, arguments=call.function.arguments))
        usage = getattr(result, 'usage', None)
        normalized_usage = SimpleNamespace(input_tokens=usage.prompt_tokens, output_tokens=usage.completion_tokens,
            input_tokens_details=getattr(usage, 'prompt_tokens_details', None)) if usage else None
        return SimpleNamespace(output=output, output_text=message.content or getattr(message, 'refusal', '') or '',
                               usage=normalized_usage,
                               model=getattr(result, 'model', None), service_tier=getattr(result, 'service_tier', None),
                               status='completed' if choice.finish_reason in ('stop', 'tool_calls') else 'incomplete')


def adapter_for(client, protocol):
    if protocol == 'responses':
        return ResponsesAdapter(client)
    if protocol == 'chat_completions':
        return ChatCompletionsAdapter(client)
    raise ValueError('未知协议；不会自动回退。')


def test_connection(adapter, model):
    """Probe tool calls and result roundtrip; never exposes VASP tools or project data."""
    probe = dict(type='function', name='connection_probe', description='Return the supplied ping string.', strict=True,
                 parameters={'type': 'object', 'properties': {'ping': {'type': 'string', 'enum': ['vasp-agent-probe']}},
                             'required': ['ping'], 'additionalProperties': False})
    history = [{'role': 'user', 'content': 'Call connection_probe with ping=vasp-agent-probe.'}]
    response = adapter.create(model, 'Connectivity test. Follow the requested tool call.', history, [probe], probe=True)
    calls = [item for item in response.output if item.type == 'function_call']
    if response.status != 'completed' or len(calls) != 1 or calls[0].name != 'connection_probe':
        raise ValueError('连接返回了响应，但未通过真正工具调用测试。')
    call = calls[0]
    if json.loads(call.arguments) != {'ping': 'vasp-agent-probe'}:
        raise ValueError('工具参数不符合预期。')
    history.extend(item.model_dump(mode='json', exclude_none=True) for item in response.output)
    history.append({'type': 'function_call_output', 'call_id': call.call_id,
                    'output': '{"result":"probe-ok"}'})
    result = adapter.create(model, 'Tool test finished. Reply probe-ok, do not call tools again.', history, [probe])
    if result.status != 'completed' or 'probe-ok' not in result.output_text or any(x.type == 'function_call' for x in result.output):
        raise ValueError('工具结果回传测试未通过。')
    return {'connection': 'ok', 'tool_call': 'ok', 'tool_result_roundtrip': 'ok', 'requests': 2}
