"""Offline unit tests for tools/check.py dual-threshold line-count gate."""
from __future__ import annotations

import importlib.util
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CHECK_PATH = ROOT / 'tools/check.py'


def _load_check():
    spec = importlib.util.spec_from_file_location('repo_check_tool', CHECK_PATH)
    module = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    spec.loader.exec_module(module)
    return module


def _write_lines(path: Path, count: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(''.join(f'x = {i}\n' for i in range(count)), encoding='utf-8')


def test_dual_threshold_constants():
    check = _load_check()
    assert check.WARN_LINES == 500
    assert check.MAX_LINES == 800
    assert check.GRANDFATHERED_LINE_CAPS == {}


def test_line_count_gate_warns_above_500_without_failing(tmp_path):
    check = _load_check()
    _write_lines(tmp_path / 'src/vasp_agent/ok_mod.py', 500)
    _write_lines(tmp_path / 'src/vasp_agent/warn_mod.py', 501)
    _write_lines(tmp_path / 'tools/edge_mod.py', 800)

    loc = check.check_line_counts(tmp_path)
    assert loc['ok'] is True
    assert loc['scanned'] == 3
    assert loc['violations'] == []
    assert [item['path'] for item in loc['warnings']] == [
        'src/vasp_agent/warn_mod.py',
        'tools/edge_mod.py',
    ]

    summary = check.format_summary({
        'ok': True,
        'release_version': 'deadbeef00000000',
        'ruff': {'ok': True, 'elapsed_s': 0.01},
        'pytest': {'ok': True, 'skipped': True, 'elapsed_s': 0.0, 'stdout': '', 'stderr': ''},
        'line_counts': loc,
    })
    assert 'loc:    OK (3 files, warn=500, limit=800, warnings=2, grandfathered=0)' in summary
    assert 'WARN src/vasp_agent/warn_mod.py: 501L > 500L (hard=800L)' in summary
    assert 'WARN tools/edge_mod.py: 800L > 500L (hard=800L)' in summary


def test_line_count_gate_fails_above_800(tmp_path):
    check = _load_check()
    _write_lines(tmp_path / 'src/vasp_agent/warn_mod.py', 600)
    _write_lines(tmp_path / 'src/vasp_agent/too_big.py', 801)

    loc = check.check_line_counts(tmp_path)
    assert loc['ok'] is False
    assert len(loc['warnings']) == 1
    assert loc['violations'] == [
        {'path': 'src/vasp_agent/too_big.py', 'lines': 801, 'limit': 800, 'kind': 'new_oversized'}
    ]

    summary = check.format_summary({
        'ok': False,
        'release_version': 'deadbeef00000000',
        'ruff': {'ok': True, 'elapsed_s': 0.01},
        'pytest': {'ok': True, 'skipped': True, 'elapsed_s': 0.0, 'stdout': '', 'stderr': ''},
        'line_counts': loc,
    })
    assert 'loc:    FAIL (1 violations, warn=500, limit=800)' in summary
    assert 'src/vasp_agent/too_big.py: 801L > 800L (new_oversized)' in summary
    assert 'WARN src/vasp_agent/warn_mod.py: 600L > 500L (hard=800L)' in summary


def test_build_pytest_command_xdist_and_anti_recursion():
    check = _load_check()
    # Default stays serial for backward compatibility.
    default_cmd = check.build_pytest_command(None, env={}, has_xdist=True)
    assert '-n' not in default_cmd and '--dist' not in default_cmd

    # Explicit workers enables xdist with loadfile distribution when installed.
    parallel_cmd = check.build_pytest_command('auto', env={}, has_xdist=True)
    assert parallel_cmd[-4:] == ['-n', 'auto', '--dist', 'loadfile']

    # Inside an active xdist worker, never spawn nested parallel workers.
    nested_cmd = check.build_pytest_command('auto', env={'PYTEST_XDIST_WORKER': 'gw0'}, has_xdist=True)
    assert '-n' not in nested_cmd

    # Missing xdist or explicit off/0 gracefully falls back to serial.
    no_xdist_cmd = check.build_pytest_command('auto', env={}, has_xdist=False)
    assert '-n' not in no_xdist_cmd
    for disabled in ('0', 'off', 'none'):
        off_cmd = check.build_pytest_command(disabled, env={}, has_xdist=True)
        assert '-n' not in off_cmd
