"""The stdin worker program must behave exactly like the package import path."""
import importlib
import inspect
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import types

import pytest

from vasp_agent.remote import remote_worker
from vasp_agent.remote.bundle import PACKAGE_ONLY, stdin_program, unit_source, worker_program
from vasp_agent.remote.remote_worker import SOURCE_FILES, WORKER_UNITS


def worker_modules():
    return [importlib.import_module('vasp_agent.remote.' + unit) for unit in WORKER_UNITS] + [remote_worker]


def run_bundled(payload, cwd):
    # -I and no PYTHONPATH: the remote side has no vasp_agent package to fall back on,
    # so a stray package import left in the program fails here instead of on a server.
    env = {k: v for k, v in os.environ.items() if k != 'PYTHONPATH'}
    result = subprocess.run([sys.executable, '-I', '-X', 'utf8', '-'], input=stdin_program(json.dumps(payload)),
                            cwd=cwd, env=env, capture_output=True, text=True, encoding='utf-8', timeout=60)
    return result.returncode, json.loads(result.stdout.strip().splitlines()[-1])


def run_package(payload):
    try:
        return 0, {'ok': True, 'result': json.loads(json.dumps(remote_worker.dispatch(payload), ensure_ascii=False))}
    except Exception as exc:
        return 1, {'ok': False, 'error': str(exc), 'type': type(exc).__name__,
                   'error_code': getattr(exc, 'error_code', 'FILE_NOT_FOUND' if isinstance(exc, FileNotFoundError) else 'REMOTE_ERROR')}


def workspace(tmp_path):
    root = tmp_path / 'remote-root'
    job = root / 'jobs' / 'si'
    (job / 'submission-recoveries').mkdir(parents=True)
    (job / 'manifest.json').write_text(json.dumps({'stage_gates': True}), encoding='utf-8')
    (job / 'workflow-state.json').write_text(json.dumps({'status': 'prepared'}), encoding='utf-8')
    (job / 'checkpoint.json').write_text(json.dumps({'next': 'convergence'}), encoding='utf-8')
    (job / 'submission-recoveries' / ('a' * 32 + '.json')).write_text(
        json.dumps({'status': 'recovered', 'phase': 'convergence'}), encoding='utf-8')
    return root


@pytest.mark.parametrize('request_fields', [
    {'action': 'list_jobs'},
    {'action': 'status', 'job_name': 'si'},
    {'action': 'recovery_status', 'job_name': 'si', 'operation_id': 'a' * 32},
    {'action': 'recovery_status', 'job_name': 'si', 'operation_id': 'b' * 32},
    {'action': 'recovery_status', 'job_name': 'si', 'operation_id': 'not-hex'},
    {'action': 'status', 'job_name': 'missing'},
    {'action': 'status', 'job_name': '../escape'},
    {'action': 'no_such_action', 'job_name': 'si'},
])
def test_bundled_program_answers_like_the_package(tmp_path, request_fields):
    root = workspace(tmp_path)
    payload = {'config': {'root': str(root)}, **request_fields}
    bundled = run_bundled(payload, tmp_path)
    assert bundled == run_package(payload)


def test_bundled_program_defines_the_package_code(tmp_path):
    namespace = {'__name__': 'bundled_worker', '__package__': None}
    exec(compile(worker_program(), '<bundled worker>', 'exec'), namespace)

    def same_code(left, right):
        assert left.co_code == right.co_code and left.co_names == right.co_names, left.co_name
        assert left.co_varnames == right.co_varnames and left.co_freevars == right.co_freevars, left.co_name
        assert len(left.co_consts) == len(right.co_consts), left.co_name
        for a, b in zip(left.co_consts, right.co_consts):
            if isinstance(a, types.CodeType):
                same_code(a, b)
            else:
                assert a == b or (a != a and b != b), left.co_name

    checked = 0
    for module in worker_modules():
        for name, value in vars(module).items():
            if name.startswith('__') or isinstance(value, types.ModuleType):
                continue
            if inspect.isfunction(value) or inspect.isclass(value):
                if value.__module__ != module.__name__:
                    continue  # imported; checked where it is defined
                bundled = namespace[name]
                if inspect.isclass(value):
                    assert [base.__name__ for base in bundled.__mro__] == [base.__name__ for base in value.__mro__]
                    for attr, member in vars(value).items():
                        if inspect.isfunction(member):
                            same_code(member.__code__, vars(bundled)[attr].__code__)
                else:
                    same_code(value.__code__, bundled.__code__)
                checked += 1
            elif isinstance(value, dict) and any(inspect.isfunction(item) for item in value.values()):
                # Dispatch tables: the same action names bound to the same handlers.
                assert {key: item.__name__ for key, item in namespace[name].items()} == \
                    {key: item.__name__ for key, item in value.items()}, name
            elif not callable(value):
                assert namespace[name] == value, name
    assert checked >= 40
    # Every public name the package exposes from the worker is defined in the program.
    assert {name for name in vars(remote_worker) if not name.startswith('__')} <= namespace.keys()


def test_units_are_deployed_and_strip_to_stdlib_only():
    assert {'vasp_agent/remote/' + unit + '.py' for unit in WORKER_UNITS} <= set(SOURCE_FILES)
    for unit in (*WORKER_UNITS, 'remote_worker'):
        assert not re.search(r'^\s*(from|import)\s+vasp_agent\b', unit_source(unit), re.M), unit
    raw = Path(remote_worker.__file__).read_text(encoding='utf-8')
    assert raw.count(PACKAGE_ONLY[0]) == raw.count(PACKAGE_ONLY[1]) == (1 if WORKER_UNITS else 0)
