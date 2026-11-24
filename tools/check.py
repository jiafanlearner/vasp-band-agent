"""Unified local engineering check: ruff + pytest + source line-count gate.

Keeps default stdout compact (<= 4 KB) for both humans and AI agents; pass
`--output .agent/cache/check-report.json` to persist full diagnostic details.
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
WARN_LINES = 500
MAX_LINES = 800
STDOUT_BYTE_LIMIT = 4096

# All legacy oversized files were retired across Stage 2 and Stage 3.
# Files in (WARN_LINES, MAX_LINES] emit warnings without failing; any file
# exceeding MAX_LINES fails the gate.
GRANDFATHERED_LINE_CAPS: dict[str, int] = {}

RUFF_TARGETS = ('src', 'tests', 'tools', '.agents/hooks')
LOC_ROOTS = ('src/vasp_agent', 'tools', '.agents/hooks')


def run_step(command: list[str], timeout: int = 600) -> dict:
    started = time.monotonic()
    try:
        proc = subprocess.run(
            command,
            cwd=ROOT,
            capture_output=True,
            text=True,
            encoding='utf-8',
            errors='replace',
            timeout=timeout,
        )
        return {
            'ok': proc.returncode == 0,
            'returncode': proc.returncode,
            'elapsed_s': round(time.monotonic() - started, 2),
            'stdout': proc.stdout,
            'stderr': proc.stderr,
        }
    except subprocess.TimeoutExpired:
        return {
            'ok': False,
            'returncode': 124,
            'elapsed_s': round(time.monotonic() - started, 2),
            'stdout': '',
            'stderr': f'Command timed out after {timeout}s',
        }
    except OSError as exc:
        return {
            'ok': False,
            'returncode': 127,
            'elapsed_s': round(time.monotonic() - started, 2),
            'stdout': '',
            'stderr': str(exc),
        }


def check_line_counts(root: Path = ROOT) -> dict:
    violations: list[dict] = []
    warnings: list[dict] = []
    grandfathered: list[dict] = []
    scanned = 0
    for rel_root in LOC_ROOTS:
        base = root / rel_root
        if not base.exists():
            continue
        for path in sorted(base.rglob('*.py')):
            if '__pycache__' in path.parts:
                continue
            scanned += 1
            rel = path.relative_to(root).as_posix()
            lines = len(path.read_text(encoding='utf-8').splitlines())
            if lines <= WARN_LINES:
                continue
            if lines <= MAX_LINES:
                warnings.append({
                    'path': rel,
                    'lines': lines,
                    'warn_limit': WARN_LINES,
                    'hard_limit': MAX_LINES,
                })
                continue
            cap = GRANDFATHERED_LINE_CAPS.get(rel)
            if cap is not None and lines <= cap:
                grandfathered.append({'path': rel, 'lines': lines, 'cap': cap})
            elif cap is not None:
                violations.append({'path': rel, 'lines': lines, 'limit': cap, 'kind': 'debt_grew'})
            else:
                violations.append({'path': rel, 'lines': lines, 'limit': MAX_LINES, 'kind': 'new_oversized'})
    return {
        'ok': not violations,
        'warn_limit': WARN_LINES,
        'limit': MAX_LINES,
        'scanned': scanned,
        'warnings': warnings,
        'violations': violations,
        'grandfathered': grandfathered,
    }


def engine_release_version() -> str:
    src = str(ROOT / 'src')
    if src not in sys.path:
        sys.path.insert(0, src)
    from vasp_agent.remote.bundle import local_version
    return local_version()


def _tail_lines(text: str, max_lines: int = 8) -> list[str]:
    lines = [line.rstrip() for line in text.splitlines() if line.strip()]
    return lines[-max_lines:]


def format_summary(report: dict) -> str:
    lines: list[str] = []
    overall = 'PASS' if report['ok'] else 'FAIL'
    lines.append(f"[check] {overall} | release={report['release_version']}")

    ruff = report['ruff']
    if ruff['ok']:
        lines.append(f"  ruff:   OK ({ruff['elapsed_s']}s)")
    else:
        lines.append(f"  ruff:   FAIL rc={ruff['returncode']} ({ruff['elapsed_s']}s)")
        for item in _tail_lines((ruff['stdout'] + '\n' + ruff['stderr']).strip(), 6):
            lines.append(f"    {item[:180]}")

    tests = report['pytest']
    if tests.get('skipped'):
        lines.append('  pytest: SKIPPED (--skip-pytest)')
    else:
        summary_line = next(iter(_tail_lines(tests['stdout'], 1)), '')
        status = 'OK' if tests['ok'] else f"FAIL rc={tests['returncode']}"
        lines.append(f"  pytest: {status} ({tests['elapsed_s']}s) {summary_line[:160]}".rstrip())
        if not tests['ok']:
            for item in _tail_lines(tests['stdout'] + '\n' + tests['stderr'], 8):
                lines.append(f"    {item[:180]}")

    loc = report['line_counts']
    warn_limit = loc.get('warn_limit', WARN_LINES)
    warnings = loc.get('warnings', [])
    if loc['ok']:
        lines.append(
            f"  loc:    OK ({loc['scanned']} files, warn={warn_limit}, limit={loc['limit']}, "
            f"warnings={len(warnings)}, grandfathered={len(loc['grandfathered'])})"
        )
        for item in warnings[:8]:
            lines.append(
                f"    WARN {item['path']}: {item['lines']}L > {item['warn_limit']}L "
                f"(hard={item['hard_limit']}L)"
            )
    else:
        lines.append(
            f"  loc:    FAIL ({len(loc['violations'])} violations, "
            f"warn={warn_limit}, limit={loc['limit']})"
        )
        for item in loc['violations'][:8]:
            lines.append(f"    {item['path']}: {item['lines']}L > {item['limit']}L ({item['kind']})")
        for item in warnings[:4]:
            lines.append(
                f"    WARN {item['path']}: {item['lines']}L > {item['warn_limit']}L "
                f"(hard={item['hard_limit']}L)"
            )

    if report.get('release_check_failed'):
        lines.append(
            f"  release: FAIL expected={report['expected_release']} actual={report['release_version']}"
        )

    if report.get('output_path'):
        lines.append(f"  report: {report['output_path']}")

    text = '\n'.join(lines) + '\n'
    raw = text.encode('utf-8')
    if len(raw) <= STDOUT_BYTE_LIMIT:
        return text
    suffix = '\n... (truncated to 4 KB; use --output .agent/cache/check-report.json)\n'
    budget = STDOUT_BYTE_LIMIT - len(suffix.encode('utf-8'))
    return raw[:budget].decode('utf-8', errors='ignore').rstrip() + suffix


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description='Run ruff, pytest and source line-count checks.')
    parser.add_argument('--output', help='Write full JSON report to this path.')
    parser.add_argument('--skip-pytest', action='store_true', help='Skip pytest (lint + loc only).')
    parser.add_argument('--expect-release', help='Fail if bundle.local_version() differs from this hash.')
    args = parser.parse_args(argv)

    existing_targets = [t for t in RUFF_TARGETS if (ROOT / t).exists()]
    ruff = run_step([sys.executable, '-m', 'ruff', 'check', '--output-format', 'concise', *existing_targets])
    if args.skip_pytest:
        pytest_res = {'ok': True, 'skipped': True, 'returncode': 0, 'elapsed_s': 0.0, 'stdout': '', 'stderr': ''}
    else:
        pytest_res = run_step([sys.executable, '-m', 'pytest', '-q', '--tb=short'], timeout=900)
    loc = check_line_counts()
    release = engine_release_version()
    release_mismatch = bool(args.expect_release and release != args.expect_release)

    report = {
        'ok': bool(ruff['ok'] and pytest_res['ok'] and loc['ok'] and not release_mismatch),
        'release_version': release,
        'expected_release': args.expect_release,
        'release_check_failed': release_mismatch,
        'ruff': ruff,
        'pytest': pytest_res,
        'line_counts': loc,
        'output_path': args.output,
    }

    if args.output:
        out_path = Path(args.output)
        if not out_path.is_absolute():
            out_path = ROOT / out_path
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')

    sys.stdout.write(format_summary(report))
    return 0 if report['ok'] else 1


if __name__ == '__main__':
    raise SystemExit(main())
