"""Offline regression tests for Stage 2 hardening (2A + 2D + bundle cache)."""
from __future__ import annotations

import json
import threading
import time
from types import SimpleNamespace as NS

from vasp_agent.chat.ai_agent import Agent, Toolbox
from vasp_agent.common.file_lock import FileLock as CommonFileLock
from vasp_agent.models.model_adapters import ChatCompletionsAdapter
from vasp_agent.monitoring.job_monitor import JobMonitor, MonitorLockError, MonitorPersistenceError, FileLock as MonitorFileLock
from vasp_agent.remote import bundle, ssh_pool, worker_common


def test_file_lock_decoupled_and_reexported():
    assert issubclass(MonitorFileLock, CommonFileLock)
    assert issubclass(MonitorLockError, MonitorPersistenceError)


def test_job_monitor_dynamic_wait_and_transient_lock_backoff(tmp_path):
    now = [1000.0]
    monitor = JobMonitor(tmp_path, clock=lambda: now[0], max_idle_wait=25.0)
    monitor.register('hpc', 'si', 'scf', '42')
    assert monitor._check(False) == []
    assert monitor._next_wait == 25.0

    now[0] = 1890.0  # 10s until next_due_at=1900
    assert monitor._check(False) == []
    assert abs(monitor._next_wait - 10.0) < 0.01

    # Transient MonitorLockError must NOT terminate the background monitor loop.
    attempts = []
    recovered = threading.Event()
    notices = []
    original_check = monitor._check

    def flaky_check(force):
        attempts.append(force)
        if len(attempts) == 1:
            raise MonitorLockError('transient busy')
        recovered.set()
        return original_check(force)

    monitor._check = flaky_check
    assert monitor.start(lambda _: {'state': 'RUNNING', 'terminal': False}, notices.append)
    try:
        assert recovered.wait(timeout=3.0)
        assert monitor._thread.is_alive()
    finally:
        monitor.stop()
    assert not any(ev.get('kind') == 'error' for ev in notices)


def test_chat_completions_length_populates_incomplete_details_for_retry(tmp_path):
    calls = []

    class FakeCompletions:
        def create(self, **kwargs):
            calls.append(kwargs['max_tokens'])
            if len(calls) == 1:
                msg = NS(content='partial', tool_calls=None, refusal=None)
                choice = NS(message=msg, finish_reason='length')
            else:
                msg = NS(content='complete answer', tool_calls=None, refusal=None)
                choice = NS(message=msg, finish_reason='stop')
            usage = NS(prompt_tokens=100, completion_tokens=50, prompt_tokens_details=None)
            return NS(choices=[choice], usage=usage, model='m', service_tier=None)

    fake_client = NS(chat=NS(completions=FakeCompletions()))
    adapter = ChatCompletionsAdapter(fake_client)
    adapter.output_limit = 4000

    first = adapter.create('m', 'sys', [{'role': 'user', 'content': 'hi'}], [])
    assert first.status == 'incomplete'
    assert first.incomplete_details.reason == 'max_output_tokens'

    # Reset and verify Agent.retry_with_more_output actually triggers on ChatCompletionsAdapter.
    calls.clear()
    box = Toolbox(tmp_path)
    cfg = NS(context_window=64000, output_reserve=4000, context_safety=2048,
             compact_trigger=0.8, compact_target=0.55, compaction_mode='summary',
             recent_context_tokens=8000)
    agent = Agent(fake_client, 'm', box, adapter=adapter, context_config=cfg)
    reply = agent.ask('hello')
    assert reply == 'complete answer'
    assert len(calls) == 2 and calls[1] > calls[0]


def test_write_json_uses_unique_tmp_and_cleans_up_on_failure(tmp_path, monkeypatch):
    target = tmp_path / 'state.json'
    worker_common.write_json(target, {'ok': True})
    assert json.loads(target.read_text(encoding='utf-8')) == {'ok': True}
    assert list(tmp_path.glob('*.tmp')) == []

    # Simulate failure during replace: temporary file must be cleaned up.
    def boom(self, dst):
        raise OSError('disk full')

    monkeypatch.setattr(type(target), 'replace', boom)
    try:
        worker_common.write_json(target, {'ok': False})
    except OSError:
        pass
    assert list(tmp_path.glob('*.tmp')) == []


def test_ssh_pool_exchange_drains_stdout_concurrently_while_sending_stdin():
    """Verify _exchange does not deadlock when remote writes output before stdin finishes."""
    stdout_drained = threading.Event()

    class EarlyOutputChannel:
        def __init__(self):
            self.closed = False
            self.out = b'early-stdout-chunk\n'
            self.err = b''
            self.code = None
            self.stdin = bytearray()

        def settimeout(self, value):
            self.timeout = value

        def exec_command(self, command):
            self.command = command

        def sendall(self, data):
            # Block writer until main thread drains early stdout (would deadlock if synchronous!).
            assert stdout_drained.wait(timeout=2.0), 'deadlock: main thread did not drain stdout during sendall'
            self.stdin.extend(data)

        def shutdown_write(self):
            self.out += b'{"ok": true}\n'
            self.code = 0

        def recv_ready(self):
            return bool(self.out)

        def recv(self, size):
            chunk, self.out = self.out[:size], self.out[size:]
            if b'early-stdout-chunk' in chunk:
                stdout_drained.set()
            return chunk

        def recv_stderr_ready(self):
            return bool(self.err)

        def recv_stderr(self, size):
            chunk, self.err = self.err[:size], self.err[size:]
            return chunk

        def exit_status_ready(self):
            return self.code is not None

        def recv_exit_status(self):
            return self.code

        def close(self):
            self.closed = True

    pool = ssh_pool.Pool()
    entry = ssh_pool.Entry({})
    entry.client = NS(get_transport=lambda: NS(is_active=lambda: True))
    fake_paramiko = NS(SSHException=RuntimeError)
    chan = EarlyOutputChannel()
    proc = pool._exchange(fake_paramiko, entry, chan, 'python -', 'x' * 4096, time.monotonic() + 5.0, 5.0)
    assert proc.returncode == 0
    assert 'early-stdout-chunk' in proc.stdout and '{"ok": true}' in proc.stdout
    assert bytes(chan.stdin) == b'x' * 4096


def test_worker_program_cached_in_memory(monkeypatch):
    bundle.clear_worker_cache()
    reads = []
    real_unit_source = bundle.unit_source

    def counting_unit_source(unit):
        reads.append(unit)
        return real_unit_source(unit)

    monkeypatch.setattr(bundle, 'unit_source', counting_unit_source)
    first = bundle.worker_program()
    second = bundle.worker_program()
    assert first == second
    assert len(reads) == len(bundle.WORKER_UNITS) + 1
    bundle.clear_worker_cache()
