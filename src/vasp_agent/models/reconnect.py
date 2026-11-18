"""Bounded model-request retries only; never wraps tool or SSH execution."""
from collections.abc import Mapping
from datetime import datetime, timezone
import ssl
import time

from vasp_agent.models.request_errors import _delay


def retry_delay(exc, attempt, remaining_wait):
    """None means stop; honor server delays without exceeding the wait budget."""
    current, seen = exc, set()
    for _ in range(8):
        if current is None or id(current) in seen:
            break
        seen.add(id(current))
        if isinstance(current, ssl.SSLCertVerificationError):
            return None
        current = current.__cause__ or (None if current.__suppress_context__ else current.__context__)
    names = {cls.__name__ for cls in type(exc).__mro__}
    response = getattr(exc, 'response', None)
    status = getattr(exc, 'status_code', getattr(response, 'status_code', None))
    transport = isinstance(exc, (TimeoutError, ConnectionError)) or bool(names & {'APITimeoutError', 'APIConnectionError'})
    if not transport and status not in (408, 429, 500, 502, 503, 504, 524):
        return None
    body = getattr(exc, 'body', None)
    values = []
    if isinstance(body, Mapping):
        error = body.get('error', body)
        if isinstance(error, Mapping) and error.get('code') in ('insufficient_quota', 'billing_hard_limit_reached'):
            return None
        values.append(body.get('retry_after'))
        if isinstance(error, Mapping):
            values.append(error.get('retry_after'))
    headers = getattr(response, 'headers', None)
    if isinstance(headers, Mapping):
        values.extend(v for k, v in headers.items() if str(k).lower() == 'retry-after')
    now = datetime.now(timezone.utc)
    delays = [v for raw in values if (v := _delay(raw, now)) is not None]
    delay = max([min(5 * 2 ** attempt, 60), *delays])
    return delay if delay <= remaining_wait else None


def wait_for_retry(seconds, event):
    """Short interruptible sleeps, with periodic visible progress, no network probe."""
    deadline = time.monotonic() + seconds
    next_notice = 10
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return
        elapsed = seconds - remaining
        if elapsed >= next_notice:
            event(f'等待重新连接 · 剩余约 {remaining:.0f} 秒 · Ctrl+C 取消本次等待')
            next_notice += 10
        time.sleep(min(1, remaining))
