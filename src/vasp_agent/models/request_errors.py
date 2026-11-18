"""Safe, local recovery advice for model requests; never retry or echo error bodies."""
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
import math
import socket
import ssl
from collections.abc import Mapping

_UNKNOWN = object()


def _seconds(value):
    if type(value) not in (int, float):
        return None
    try:
        return float(value) if math.isfinite(value) and value >= 0 else None
    except OverflowError:
        return None


def _timeout_text(value):
    if value is None:
        return '未设置超时限制'
    number = _seconds(value)
    if number is not None:
        return f'{number:g} 秒（连接/读取等阶段限制，并非总等待时长）'
    if value is _UNKNOWN:
        return '未知'
    parts = []
    for field, label in [('connect', '连接'), ('read', '读取'), ('write', '写入'), ('pool', '连接池')]:
        item = getattr(value, field, _UNKNOWN)
        number = _seconds(item)
        shown = '无限制' if item is None else f'{number:g} 秒' if number is not None else '未知'
        parts.append(f'{label} {shown}')
    return '；'.join(parts) + '（分阶段限制，并非总等待时长）'


def _cause_text(exc):
    """Classify bounded exception chains without exposing messages, URLs or keys."""
    descriptions = {
        'ConnectTimeout': '建立连接超时', 'ReadTimeout': '等待响应数据超时',
        'WriteTimeout': '发送请求数据超时', 'PoolTimeout': '等待连接池超时',
        'ConnectError': '连接建立失败', 'ReadError': '读取连接数据失败',
        'WriteError': '发送连接数据失败', 'CloseError': '关闭连接失败',
        'RemoteProtocolError': '远端 HTTP 响应不完整或协议异常',
        'LocalProtocolError': '本地 HTTP 协议异常', 'ProxyError': '代理连接异常',
    }
    seen, parts = set(), []
    current = exc
    for _ in range(8):
        if current is None or id(current) in seen:
            break
        seen.add(id(current))
        name = type(current).__name__
        if isinstance(current, ssl.SSLCertVerificationError):
            detail = 'TLS 证书验证失败'
        elif isinstance(current, ssl.SSLEOFError):
            detail = 'TLS 握手或传输被提前关闭（SSLEOFError）'
        elif isinstance(current, ssl.SSLError):
            detail = 'TLS 连接异常'
        elif isinstance(current, socket.gaierror):
            detail = 'DNS 地址解析失败'
        elif isinstance(current, ConnectionResetError):
            detail = '连接被重置'
        elif isinstance(current, ConnectionRefusedError):
            detail = '连接被拒绝'
        elif isinstance(current, BrokenPipeError):
            detail = '连接写入通道已关闭'
        elif isinstance(current, TimeoutError):
            detail = '底层网络操作超时（阶段未知）'
        elif isinstance(current, TypeError):
            detail = '底层调用类型异常（TypeError）'
        else:
            detail = descriptions.get(name)
        if detail and detail not in parts:
            parts.append(detail)
        cause = current.__cause__
        context = current.__context__
        # httpcore maps low-level network exceptions with ``raise ... from None``.
        # Recover only known transport categories from that boundary, not arbitrary
        # suppressed application exceptions or their potentially sensitive prose.
        mapped_transport = (
            type(current).__module__.split('.')[0] in ('httpcore', 'httpcore2')
            and isinstance(context, (ssl.SSLError, socket.gaierror, ConnectionError, TimeoutError))
        )
        current = cause or (context if not current.__suppress_context__ or mapped_transport else None)
    return ' → '.join(parts) if parts else '未提供可识别的底层原因，无法进一步定位'


def request_failure_summary(exc):
    """Safe short reason for retry progress; never includes raw provider text."""
    names = {cls.__name__ for cls in type(exc).__mro__}
    if isinstance(exc, TimeoutError) or 'APITimeoutError' in names:
        return '请求超时 · ' + _cause_text(exc)
    if isinstance(exc, ConnectionError) or 'APIConnectionError' in names:
        return '连接异常 · ' + _cause_text(exc)
    response = getattr(exc, 'response', None)
    status = getattr(exc, 'status_code', None)
    if status is None:
        status = getattr(response, 'status_code', None)
    if type(status) is int and 400 <= status <= 599:
        return f'HTTP {status}'
    return None


def _transport_message(exc, *, elapsed_seconds, timeout):
    names = {cls.__name__ for cls in type(exc).__mro__}
    timed_out = isinstance(exc, TimeoutError) or 'APITimeoutError' in names
    if not timed_out and not (isinstance(exc, ConnectionError) or 'APIConnectionError' in names):
        return None
    kind = ('APITimeoutError' if 'APITimeoutError' in names else 'TimeoutError') if timed_out else (
        'APIConnectionError' if 'APIConnectionError' in names else 'ConnectionError')
    elapsed = _seconds(elapsed_seconds)
    elapsed_text = f'{elapsed:.1f} 秒' if elapsed is not None else '未知'
    title = '模型接口请求超时' if timed_out else '模型接口连接异常'
    advice = ('请结合底层原因检查网络与接口状态；若读取响应确实较慢，可调整 /settings timeout 600。'
              if timed_out else
              '连接异常可能早于配置超时发生；延长超时不能修复已断开的连接。请检查网络、代理或中转站状态。')
    return (f'{title}\n实际等待：{elapsed_text}（本次模型请求）\n'
            f'配置超时：{_timeout_text(timeout)}\n异常类型：{kind}\n'
            f'底层原因：{_cause_text(exc)}\n\n'
            '已保存对话和已完成的工具结果。输入 /retry 继续当前请求，不重放已完成工具。'
            '程序没有自动重试。' + advice)


def _delay(value, now):
    if isinstance(value, bool) or not isinstance(value, (str, int, float)):
        return None
    if isinstance(value, str) and len(value) > 128:
        return None
    try:
        seconds = float(value)
    except (TypeError, ValueError, OverflowError):
        if not isinstance(value, str):
            return None
        try:
            date = parsedate_to_datetime(value)
            if date.tzinfo is None:
                return None
            seconds = (date - now).total_seconds()
        except (TypeError, ValueError, OverflowError):
            return None
    if not math.isfinite(seconds) or not 0 <= seconds <= 604800:
        return None
    return math.ceil(seconds)


def request_failure_message(exc, *, now=None, elapsed_seconds=None, timeout=_UNKNOWN):
    """Return recovery text for transport/HTTP failures, None for programming errors.

    Only status, timing, safe cause categories and bounded retry metadata are used. Provider prose, URLs, and
    arbitrary exception text must not leak credentials or become instructions.
    """
    saved = '已保存对话和已完成的工具结果。'
    transport = _transport_message(exc, elapsed_seconds=elapsed_seconds, timeout=timeout)
    if transport is not None:
        return transport
    response = getattr(exc, 'response', None)
    status = getattr(exc, 'status_code', None)
    if status is None:
        status = getattr(response, 'status_code', None)
    if type(status) is not int or not 400 <= status <= 599:
        return None
    prefix = f'模型接口返回 HTTP {status}；' + saved
    if status in (408, 429) or status >= 500:
        now = now or datetime.now(timezone.utc)
        headers = getattr(response, 'headers', None)
        values = []
        if isinstance(headers, Mapping):
            values.extend(v for k, v in headers.items() if str(k).lower() == 'retry-after')
        body = getattr(exc, 'body', None)
        if isinstance(body, Mapping):
            values.append(body.get('retry_after'))
            if isinstance(body.get('error'), Mapping):
                values.append(body['error'].get('retry_after'))
        delays = [delay for v in values if (delay := _delay(v, now)) is not None]
        wait = f'至少等待 {max(delays)} 秒后' if delays else '稍后'
        reason = '接口限流或配额限制。' if status == 429 else '服务端或网关暂时未能完成请求。'
        return (prefix + reason + wait + '输入 /retry 继续当前请求，不重放已完成工具。'
                '程序没有自动重试。若持续发生，请联系接口服务商核查。')
    if status in (401, 403):
        return (prefix + '请核对接口地址、令牌权限及模型访问权限；'
                '这类错误不能仅靠等待解决。修正配置后输入 /retry。')
    return (prefix + '请核对接口协议、模型与请求配置；修正后输入 /retry。'
            '程序没有自动重试。')
