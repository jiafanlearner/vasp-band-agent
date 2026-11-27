"""Offline tests for P2c L5 PreToolUse autonomy guard and hooks.json wiring."""
from __future__ import annotations

import base64
import importlib.util
import json
import os
import shlex
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
REPO_HOOKS_JSON = ROOT / '.agents/hooks.json'
PLUGIN_HOOKS_JSON = ROOT / 'integrations/vasp-autonomy/hooks/hooks.json'
GUARD_PATH = ROOT / 'integrations/vasp-autonomy/hooks/autonomy_guard.py'


def _load_guard():
    spec = importlib.util.spec_from_file_location('autonomy_guard', GUARD_PATH)
    module = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    spec.loader.exec_module(module)
    return module


def _try_dir_link_or_junction(link: Path, target: Path) -> bool:
    try:
        os.symlink(target, link, target_is_directory=True)
        return True
    except (OSError, NotImplementedError):
        pass
    if os.name == 'nt':
        proc = subprocess.run(
            ['cmd', '/c', 'mklink', '/J', str(link), str(target)],
            capture_output=True, check=False,
        )
        return proc.returncode == 0
    return False


def test_repo_and_plugin_hooks_json_wire_autonomy_guard_across_cwds(tmp_path):
    assert GUARD_PATH.is_file()
    for hooks_file, cwds in (
        (REPO_HOOKS_JSON, (ROOT, ROOT / '.agents', ROOT / 'src/vasp_agent')),
        (PLUGIN_HOOKS_JSON, (ROOT, ROOT / 'integrations/vasp-autonomy', ROOT / 'integrations/vasp-autonomy/hooks')),
    ):
        data = json.loads(hooks_file.read_text(encoding='utf-8'))
        pre = data['hooks']['PreToolUse']
        assert isinstance(pre, list) and pre
        cmds = [h['command'] for entry in pre for h in entry['hooks'] if 'autonomy_guard.py' in h['command']]
        assert len(cmds) == 1, f'{hooks_file} must register autonomy_guard.py exactly once'
        argv = shlex.split(cmds[0])
        argv[0] = sys.executable
        for workdir in cwds:
            blocked = subprocess.run(
                argv,
                input=json.dumps({'tool_name': 'Write', 'tool_input': {'file_path': '.agent/state/contracts/active/c.json'}}),
                cwd=workdir, capture_output=True, text=True, encoding='utf-8', timeout=15,
            )
            assert blocked.returncode == 2, f'{hooks_file} cwd={workdir}: {blocked.stderr}'
            assert json.loads(blocked.stdout)['decision'] == 'block'

            allowed = subprocess.run(
                argv,
                input=json.dumps({'tool_name': 'Write', 'tool_input': {'file_path': 'src/vasp_agent/control/service.py'}}),
                cwd=workdir, capture_output=True, text=True, encoding='utf-8', timeout=15,
            )
            assert allowed.returncode == 0, f'{hooks_file} cwd={workdir}: {allowed.stderr}'

        # External CWD with PLUGIN_ROOT finds the script; without PLUGIN_ROOT fails open cleanly (no StopIteration).
        env_with = {**os.environ, 'PLUGIN_ROOT': str(ROOT / 'integrations/vasp-autonomy')}
        ext_block = subprocess.run(
            argv,
            input=json.dumps({'tool_name': 'Bash', 'tool_input': {'command': 'sbatch job.slurm'}}),
            cwd=tmp_path, env=env_with, capture_output=True, text=True, encoding='utf-8', timeout=15,
        )
        assert ext_block.returncode == 2
        env_without = {k: v for k, v in os.environ.items() if k not in ('PLUGIN_ROOT', 'CLAUDE_PLUGIN_ROOT')}
        ext_missing = subprocess.run(
            argv,
            input=json.dumps({'tool_name': 'Bash', 'tool_input': {'command': 'git status'}}),
            cwd=tmp_path, env=env_without, capture_output=True, text=True, encoding='utf-8', timeout=15,
        )
        assert ext_missing.returncode == 0, ext_missing.stderr


@pytest.mark.parametrize('protected_path', [
    '.agent/state/contracts/drafts/drf-1.json',
    '.agent/state/contracts/active/ctr-1.json',
    '.agent/state/contracts/active/ctr-1.json.sig',
    '.agent/state/contracts/ledger/ctr-1.json',
    '.agent/state/review-attestations/leases/lse-1.json',
    '.agent/state/review-attestations/scopes/local/si-scf-abcdef123456.json',
    '.agent/state/autonomy-policy.json',
    'servers/eshell111.json',
    'servers/id_ed25519',
    'runs/si-auto-01/INCAR',
    'runs/si-auto-01/review-convergence.json',
    'potentials/PBE/Si/POTCAR',
    r'.\RUNS\si-auto-01\submit.slurm',
    r'C:runs\new_job\INCAR',
    'src/../runs/si-auto-01/manifest.json',
    r'C:\repo\.agent\state\contracts\active\ctr-1.json',
    '/workspace/runs/si-auto-01/INCAR',
    '/home/dev/src/vasp-band-agent/runs/si-auto-01/INCAR',
])
def test_write_guard_blocks_all_protected_paths(protected_path):
    guard = _load_guard()
    for payload in (
        {'tool_name': 'Write', 'tool_input': {'file_path': protected_path}, 'cwd': r'C:\repo'},
        {'tool_name': 'edit_file', 'tool_input': {'Path': protected_path}, 'cwd': r'C:\repo'},
        {'toolCall': {'name': 'write_to_file', 'args': {'TargetFile': protected_path}}, 'cwd': r'C:\repo'},
        {'tool_name': 'mcp_agy-dart_fast_apply_edits', 'tool_input': {'cwd': r'C:\repo', 'edits': [{'path': protected_path}]}},
    ):
        allowed, reason = guard.evaluate(payload)
        assert not allowed, f'Expected block for {protected_path} via {payload}'
        assert reason and '禁止' in reason


def test_write_guard_blocks_apply_patch_timestamps_and_allows_dev_files():
    guard = _load_guard()
    bad_patch = '*** Begin Patch\n*** Update File: runs/si-01/INCAR\n@@\n-ENCUT = 500\n+ENCUT = 200\n*** End Patch\n'
    allowed, reason = guard.evaluate({'tool_name': 'apply_patch', 'tool_input': {'input': bad_patch}})
    assert not allowed and 'runs' in reason

    udiff_ts = '--- a/.agent/state/autonomy-policy.json\t2026-11-27 10:00:00\n+++ b/.agent/state/autonomy-policy.json\t2026-11-27 10:01:00\n'
    allowed_u, reason_u = guard.evaluate({'tool_name': 'apply_patch', 'tool_input': {'patch': udiff_ts}})
    assert not allowed_u and 'autonomy-policy.json' in reason_u

    good_patch = '*** Begin Patch\n*** Update File: src/vasp_agent/control/service.py\n@@\n*** End Patch\n'
    assert guard.evaluate({'tool_name': 'apply_patch', 'tool_input': {'input': good_patch}}) == (True, None)

    for safe_path in (
        'src/vasp_agent/control/service.py',
        'src/vasp_agent/permissions/review_attestation.py',
        'tests/test_autonomy_guard.py',
        'docs/ARCHITECTURE.md',
        'integrations/vasp-autonomy/SKILL.md',
        'workbench/plot_bands.py',
        '.agents/hooks.json',
        r'C:\repo\tests\fixtures\runs\sample.json',
        '/workspace/src/vasp_agent/runs_helper.py',
        '/workspace/workbench/runs/plot.py',
    ):
        assert guard.evaluate({
            'tool_name': 'write_file',
            'tool_input': {'Path': safe_path},
            'cwd': r'C:\repo',
            'workspacePaths': [r'C:\repo'],
        }) == (True, None), safe_path


def test_symlink_and_ancestor_named_runs_boundary(tmp_path, monkeypatch):
    guard = _load_guard()

    # 1) Checkout located inside an ancestor named `runs/` must NOT false-positive on safe relative writes.
    checkout = tmp_path / 'runs' / 'vasp-band-agent'
    (checkout / 'src').mkdir(parents=True)
    (checkout / 'pyproject.toml').write_text('[project]\nname = "demo"\n', encoding='utf-8')
    (checkout / 'src/foo.py').write_text('x = 1\n', encoding='utf-8')
    monkeypatch.chdir(checkout)
    assert guard.evaluate({'tool_name': 'Write', 'tool_input': {'file_path': 'src/foo.py'}}) == (True, None)
    assert guard.evaluate({'tool_name': 'Write', 'tool_input': {'file_path': 'src/new_file.py'}}) == (True, None)
    assert guard.evaluate({'tool_name': 'Write', 'cwd': str(checkout), 'tool_input': {'file_path': 'src/foo.py'}}) == (True, None)

    # 2) Mocked symlink/junction redirect to nested non-existent child under runs/ -> BLOCKED.
    ws = tmp_path / 'ws'
    (ws / 'runs').mkdir(parents=True)
    monkeypatch.setattr(
        guard, '_resolve_symlink_redirect',
        lambda raw_path, exec_cwd, roots: f'{ws.as_posix()}/runs/new_job/INCAR' if 'alias_link' in raw_path else None,
    )
    allowed_m, reason_m = guard.evaluate({
        'tool_name': 'Write',
        'tool_input': {'file_path': 'alias_link/new_job/INCAR'},
        'workspacePaths': [str(ws)],
        'cwd': str(ws),
    })
    assert not allowed_m and 'runs' in reason_m
    monkeypatch.undo()

    # 3) Real OS symlink / Windows NTFS junction pointing to runs/ with non-existent deep child -> BLOCKED.
    link = ws / 'real_link'
    if not _try_dir_link_or_junction(link, ws / 'runs'):
        pytest.skip('OS does not permit creating directory symlinks or NTFS junctions')
    allowed_r, reason_r = guard.evaluate({
        'tool_name': 'Write',
        'tool_input': {'file_path': 'real_link/new_job/INCAR'},
        'workspacePaths': [str(ws)],
        'cwd': str(ws),
    })
    assert not allowed_r and 'runs' in reason_r


@pytest.mark.parametrize('blocked_cmd', [
    'ssh user@cluster "ls"',
    'scp POSCAR user@cluster:~/job/',
    'sftp user@cluster',
    'sbatch runs/si-01/submit.slurm',
    'scancel 12345',
    r'C:\Windows\System32\OpenSSH\ssh.exe user@cluster',
    r'"C:\Windows\System32\OpenSSH\ssh.exe" user@hpc',
    '/usr/bin/sbatch submit.slurm',
    r's\sh user@hpc ls',
    's`sh user@hpc ls',
    'git status; ssh user@cluster id',
    'echo ok | sbatch job.slurm',
    'bash -c "ssh user@cluster ls"',
    'bash -ec "sbatch job.slurm"',
    'powershell -Command "sbatch submit.slurm"',
    'powershell -Command vasp-ctl contract approve drf-1',
    'powershell -EncodedCommand ' + base64.b64encode('sbatch submit.slurm'.encode('utf-16-le')).decode('ascii'),
    'cmd /c "scancel 9999"',
    'cmd /k "ssh user@hpc"',
    'Start-Process ssh -ArgumentList "user@cluster"',
    'Start-Process -FilePath ssh -ArgumentList user@hpc',
    'sudo -u root ssh user@hpc',
    'env -i sbatch job.slurm',
    '$(sbatch submit.slurm)',
    'echo "don\'t" "$(sbatch job.slurm)"',
    'echo "$( (ssh user@hpc) )"',
    'python -c "import subprocess; subprocess.run([\'ssh\', \'cluster\', \'ls\'])"',
    'python3.12 -c "import subprocess; subprocess.run([\'ssh\', \'hpc\'])"',
    'python3.12 -m vasp_agent.control.cli contract approve drf-1',
    'vasp-ctl contract approve drf-20261127-12345678',
    'vasp-ctl --workspace . contract revoke ctr-20261127-12345678',
    'python -m vasp_agent.control.cli contract resume ctr-20261127-12345678',
    'echo "{}" > .agent/state/contracts/active/ctr-1.json',
    'echo hi>runs/si-01/INCAR',
    'echo "hi">runs/si-01/INCAR',
    "echo '{}'>.agent/state/contracts/active/ctr-1.json",
    r'echo hi > "runs\si-01\INCAR"',
    'echo hi>>.agent/state/contracts/active/ctr-1.json',
    'Set-Content -Path runs\\si-01\\INCAR -Value "ENCUT=200"',
    r'Set-Content -Path "runs\si-01\INCAR" -Value x',
    'Set-Content -Path:runs/si-01/INCAR -Value x',
    'rm -rf .agent/state/review-attestations/leases',
    'cd runs; rm -rf si-01',
    'sed -i "s/500/200/" runs/si-01/INCAR',
    'sed --in-place=.bak s/a/b/ runs/si-01/INCAR',
    'cp fake.json servers/eshell111.json',
    'dd if=/dev/zero of=runs/si-01/INCAR count=1',
    'Remove-Item potentials/PBE/Si/POTCAR',
    'python -c "from pathlib import Path; Path(\'.agent/state/autonomy-policy.json\').write_text(\'{}\')"',
    "@'\nfrom pathlib import Path\nPath('runs/si-01/INCAR').write_text('x')\n'@ | python -",
    "python3 - <<'EOF'\nfrom pathlib import Path\nPath('runs/si-01/INCAR').write_text('x')\nEOF\n",
    "bash <<'EOF'\nssh user@hpc ls\nEOF\n",
])
def test_shell_guard_blocks_remote_bypass_tty_bypass_and_protected_writes(blocked_cmd):
    guard = _load_guard()
    allowed, reason = guard.evaluate({'tool_name': 'run_command', 'tool_input': {'CommandLine': blocked_cmd}})
    assert not allowed, f'Expected shell block for: {blocked_cmd}'
    assert reason and '禁止' in reason


def test_shell_guard_respects_cwd_inside_protected_dir_and_batch_entries():
    guard = _load_guard()
    # cwd inside runs/ while workspacePaths is /repo -> relative rm targets runs/si-01 -> BLOCKED.
    allowed, reason = guard.evaluate({
        'tool_name': 'run_command',
        'workspacePaths': ['/repo'],
        'tool_input': {'cwd': '/repo/runs', 'CommandLine': 'rm -rf si-01'},
    })
    assert not allowed and 'runs' in reason

    # Per-entry cwd in mcp_agy-dart_fast_run_checks -> BLOCKED.
    allowed_b, reason_b = guard.evaluate({
        'tool_name': 'mcp_agy-dart_fast_run_checks',
        'workspacePaths': ['/repo'],
        'tool_input': {'commands': [{'name': 'bad', 'cwd': '/repo/runs', 'command': 'rm -rf si-01'}]},
    })
    assert not allowed_b and 'runs' in reason_b

    # mcp_agy-dart_context_ctx_execute writing protected path or invoking shell ssh/sbatch -> BLOCKED.
    allowed_c, reason_c = guard.evaluate({
        'tool_name': 'mcp_agy-dart_context_ctx_execute',
        'tool_input': {'code': "from pathlib import Path; Path('runs/si-01/INCAR').write_text('x')"},
    })
    assert not allowed_c and '禁止' in reason_c

    allowed_cs, reason_cs = guard.evaluate({
        'tool_name': 'mcp_agy-dart_context_ctx_execute',
        'tool_input': {'language': 'shell', 'code': 'sbatch job.slurm'},
    })
    assert not allowed_cs and 'sbatch' in reason_cs


@pytest.mark.parametrize('safe_cmd', [
    'python -m pytest tests/test_autonomy_guard.py -q --tb=short',
    'python -m pytest tests/test_ssh_pool.py -k ssh -q',
    'python tools/check.py --skip-pytest --expect-release c9c11663accd1fce',
    'git status --short -uall',
    'git diff --stat',
    'git commit -m "feat(guard): block ssh/scp/sbatch/scancel and runs/ writes"',
    "git commit -m 'guard blocks $(sbatch) and `ssh`'",
    'ruff check src tests tools',
    'vasp-ctl contract show --json',
    'vasp-ctl call vasp_workspace_info --json',
    'vasp-ctl watch once --no-poll --json',
    'ssh-keygen -Y verify -f allowed_signers -I op -n vasp-agent-contract -s c.sig',
    'python -c "import sys; sys.path.insert(0,\'src\'); from vasp_agent.remote.bundle import local_version; print(local_version())"',
    'python -c "print(open(\'runs/si-01/INCAR\', encoding=\'ascii\').read())"',
    'git diff > patch.diff',
])
def test_shell_guard_allows_normal_dev_git_pytest_and_vasp_ctl(safe_cmd):
    guard = _load_guard()
    assert guard.evaluate({'tool_name': 'run_command', 'tool_input': {'CommandLine': safe_cmd}}) == (True, None), safe_cmd


def test_cli_protocols_support_both_claude_codex_and_antigravity_envelopes():
    # 1) Claude Code / Codex snake_case envelope -> exit 2 + decision="block"
    claude_block = subprocess.run(
        [sys.executable, str(GUARD_PATH)],
        input=json.dumps({'tool_name': 'Bash', 'tool_input': {'command': 'sbatch submit.slurm'}}),
        capture_output=True, text=True, encoding='utf-8', timeout=15,
    )
    assert claude_block.returncode == 2
    assert json.loads(claude_block.stdout)['decision'] == 'block'
    assert '[autonomy_guard]' in claude_block.stderr

    # 2) Antigravity camelCase toolCall envelope -> exit 0 + decision="deny"
    agy_block = subprocess.run(
        [sys.executable, str(GUARD_PATH)],
        input=json.dumps({'toolCall': {'name': 'run_command', 'args': {'CommandLine': 'ssh user@hpc'}}}),
        capture_output=True, text=True, encoding='utf-8', timeout=15,
    )
    assert agy_block.returncode == 0
    assert json.loads(agy_block.stdout)['decision'] == 'deny'
    assert '[autonomy_guard]' in agy_block.stderr

    # 3) Allowed call & invalid/empty stdin -> exit 0 with empty stdout
    for raw_in in (
        json.dumps({'toolCall': {'name': 'run_command', 'args': {'CommandLine': 'git status'}}}),
        '',
        '{not-json',
    ):
        res = subprocess.run(
            [sys.executable, str(GUARD_PATH)],
            input=raw_in, capture_output=True, text=True, encoding='utf-8', timeout=15,
        )
        assert res.returncode == 0
        assert res.stdout.strip() == ''
