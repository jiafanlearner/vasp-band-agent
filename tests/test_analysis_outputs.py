"""Offline unit tests for controlled Docker /output export validation and atomic publish (T5.2)."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import stat
from types import SimpleNamespace as NS

import pytest

from vasp_agent.common import agent_paths
from vasp_agent.editing import analysis_outputs as outputs
from vasp_agent.editing import analysis_runner as runner
from vasp_agent.workflow.vasp_band_agent import ToolError


RUN_ID = 'a' * 32


def _make_stage(tmp_path: Path) -> Path:
    stage = tmp_path / 'staging'
    stage.mkdir()
    return stage


def _draft(tmp_path: Path, text: str = 'print("ok")') -> str:
    wb = tmp_path / 'workbench'
    wb.mkdir(exist_ok=True)
    p = wb / 'calc.py'
    p.write_text(text, encoding='utf-8')
    return hashlib.sha256(p.read_bytes()).hexdigest()


def test_allowed_suffixes_exported_atomically_with_sha256(tmp_path):
    stage = _make_stage(tmp_path)
    samples = {
        'table.csv': b'x,y\n1,2\n',
        'summary.json': b'{"gap_eV": 1.12}\n',
        'notes.txt': b'plain text\n',
        'report.md': b'# Report\n',
        'figs/band.png': b'\x89PNG\r\n\x1a\nfake-png-bytes',
        'figs/dos.svg': b'<svg></svg>',
        'figs/paper.pdf': b'%PDF-1.4\n%%EOF\n',
    }
    for rel, payload in samples.items():
        p = stage / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(payload)

    res = outputs.collect_and_publish(tmp_path, RUN_ID, stage, 'completed')
    assert res['status'] == 'exported'
    assert res['content_is_untrusted'] is True
    assert res['output_dir'] == f'workbench/outputs/{RUN_ID}'
    assert res['file_count'] == len(samples)
    assert res['total_bytes'] == sum(len(v) for v in samples.values())

    by_name = {entry['name']: entry for entry in res['files']}
    assert set(by_name) == set(samples)
    for rel, payload in samples.items():
        disk_file = tmp_path / 'workbench' / 'outputs' / RUN_ID / rel
        assert disk_file.is_file()
        assert disk_file.read_bytes() == payload
        assert by_name[rel]['sha256'] == hashlib.sha256(payload).hexdigest()
        assert by_name[rel]['bytes'] == len(payload)
        assert by_name[rel]['path'] == f'workbench/outputs/{RUN_ID}/{rel}'
        assert by_name[rel]['workbench_path'] == f'outputs/{RUN_ID}/{rel}'

    # No leftover .tmp-* directories under workbench/outputs.
    leftovers = list((tmp_path / 'workbench' / 'outputs').glob('.tmp-*'))
    assert leftovers == []


def test_empty_output_creates_no_directory(tmp_path):
    stage = _make_stage(tmp_path)
    res = outputs.collect_and_publish(tmp_path, RUN_ID, stage, 'completed')
    assert res['status'] == 'empty' and res['file_count'] == 0 and res['output_dir'] is None
    assert res['content_is_untrusted'] is True
    assert not (tmp_path / 'workbench' / 'outputs').exists()


@pytest.mark.parametrize('exec_status', ['failed', 'timeout', 'output_limit', 'cleanup_failed'])
def test_non_completed_container_skips_export(tmp_path, exec_status):
    stage = _make_stage(tmp_path)
    (stage / 'ok.csv').write_text('a,b\n1,2\n', encoding='utf-8')
    res = outputs.collect_and_publish(tmp_path, RUN_ID, stage, exec_status)
    assert res['status'] == 'skipped' and res['files'] == [] and res['content_is_untrusted'] is True
    assert exec_status in res['reason']
    assert not (tmp_path / 'workbench' / 'outputs').exists()


@pytest.mark.parametrize('bad_name', ['evil.py', 'run.sh', 'bin.exe', 'page.html', 'noext', 'fake.csv.py'])
def test_disallowed_suffix_rejects_entire_batch(tmp_path, bad_name):
    stage = _make_stage(tmp_path)
    (stage / 'good.csv').write_text('x\n1\n', encoding='utf-8')
    (stage / bad_name).write_text('bad', encoding='utf-8')
    res = outputs.collect_and_publish(tmp_path, RUN_ID, stage, 'completed')
    assert res['status'] == 'rejected' and res['error_code'] == 'OUTPUT_SUFFIX_BLOCKED'
    assert res['files'] == [] and res['content_is_untrusted'] is True
    assert not (tmp_path / 'workbench' / 'outputs' / RUN_ID).exists()


def test_too_many_files_rejects_entire_batch(tmp_path):
    stage = _make_stage(tmp_path)
    for idx in range(outputs.MAX_OUTPUT_FILES + 1):
        (stage / f'f{idx}.txt').write_text('ok', encoding='utf-8')
    res = outputs.collect_and_publish(tmp_path, RUN_ID, stage, 'completed')
    assert res['status'] == 'rejected' and res['error_code'] == 'OUTPUT_TOO_MANY'
    assert not (tmp_path / 'workbench' / 'outputs' / RUN_ID).exists()


def test_single_file_over_2mib_rejects_entire_batch(tmp_path):
    stage = _make_stage(tmp_path)
    (stage / 'good.csv').write_text('a,b\n', encoding='utf-8')
    (stage / 'huge.txt').write_bytes(b'x' * (outputs.MAX_SINGLE_FILE_BYTES + 1))
    res = outputs.collect_and_publish(tmp_path, RUN_ID, stage, 'completed')
    assert res['status'] == 'rejected' and res['error_code'] == 'OUTPUT_FILE_TOO_LARGE'
    assert not (tmp_path / 'workbench' / 'outputs' / RUN_ID).exists()


def test_total_bytes_over_8mib_rejects_entire_batch(tmp_path):
    stage = _make_stage(tmp_path)
    chunk = b'a' * (1800 * 1024)  # 1.76 MiB each (< 2 MiB), 5 files = 8.8 MiB > 8 MiB
    for idx in range(5):
        (stage / f'part{idx}.txt').write_bytes(chunk)
    res = outputs.collect_and_publish(tmp_path, RUN_ID, stage, 'completed')
    assert res['status'] == 'rejected' and res['error_code'] == 'OUTPUT_TOTAL_TOO_LARGE'
    assert not (tmp_path / 'workbench' / 'outputs' / RUN_ID).exists()


def test_real_hardlink_rejects_entire_batch(tmp_path):
    stage = _make_stage(tmp_path)
    (stage / 'good.csv').write_text('1,2\n', encoding='utf-8')
    outside = tmp_path / 'outside.txt'
    outside.write_text('linked', encoding='utf-8')
    os.link(outside, stage / 'hardlinked.txt')
    res = outputs.collect_and_publish(tmp_path, RUN_ID, stage, 'completed')
    assert res['status'] == 'rejected' and res['error_code'] == 'OUTPUT_HARDLINK_BLOCKED'
    assert not (tmp_path / 'workbench' / 'outputs' / RUN_ID).exists()


@pytest.mark.parametrize('fake_kind,expected_code', [
    ('symlink', 'OUTPUT_LINK_BLOCKED'),
    ('reparse', 'OUTPUT_LINK_BLOCKED'),
    ('fifo', 'OUTPUT_NON_REGULAR'),
])
def test_symlink_reparse_and_non_regular_reject_entire_batch(tmp_path, monkeypatch, fake_kind, expected_code):
    stage = _make_stage(tmp_path)
    (stage / 'good.csv').write_text('1,2\n', encoding='utf-8')
    bad = stage / 'bad.txt'
    bad.write_text('placeholder', encoding='utf-8')

    real_lstat = outputs.os.lstat
    def fake_lstat(path):
        st = real_lstat(path)
        if Path(path) == bad:
            if fake_kind == 'symlink':
                return NS(st_mode=stat.S_IFLNK | 0o777, st_nlink=1, st_size=10, st_file_attributes=0)
            if fake_kind == 'reparse':
                return NS(st_mode=stat.S_IFREG | 0o644, st_nlink=1, st_size=10, st_file_attributes=0x400)
            if fake_kind == 'fifo':
                return NS(st_mode=stat.S_IFIFO | 0o644, st_nlink=1, st_size=0, st_file_attributes=0)
        return st

    monkeypatch.setattr(outputs.os, 'lstat', fake_lstat)
    res = outputs.collect_and_publish(tmp_path, RUN_ID, stage, 'completed')
    assert res['status'] == 'rejected' and res['error_code'] == expected_code
    assert not (tmp_path / 'workbench' / 'outputs' / RUN_ID).exists()


def test_real_symlink_if_os_allows(tmp_path):
    stage = _make_stage(tmp_path)
    (stage / 'good.csv').write_text('1,2\n', encoding='utf-8')
    target = tmp_path / 'secret.txt'
    target.write_text('host data', encoding='utf-8')
    try:
        os.symlink(target, stage / 'link.txt')
    except OSError:
        pytest.skip('OS disallows unprivileged symlinks')
    res = outputs.collect_and_publish(tmp_path, RUN_ID, stage, 'completed')
    assert res['status'] == 'rejected' and res['error_code'] == 'OUTPUT_LINK_BLOCKED'
    assert not (tmp_path / 'workbench' / 'outputs' / RUN_ID).exists()


@pytest.mark.parametrize('bad_segment', [
    '', '.', '..', '.hidden.csv', '.git', 'CON.txt', 'CON .csv', 'nul.csv', 'NUL .txt', 'COM1.json',
    'a/b.csv', 'a\\b.csv', 'bad:name.csv', 'trail.csv.', ' lead.csv', 'a' * 129 + '.csv',
])
def test_validate_segment_blocks_traversal_dotfiles_and_reserved_names(bad_segment):
    with pytest.raises(ToolError) as exc:
        outputs._validate_segment(bad_segment)
    assert exc.value.error_code == 'OUTPUT_PATH_BLOCKED'


def test_hidden_file_and_deep_nesting_and_dir_bomb_rejected(tmp_path):
    stage1 = tmp_path / 's1'
    stage1.mkdir()
    (stage1 / '.env.txt').write_text('x', encoding='utf-8')
    r1 = outputs.collect_and_publish(tmp_path, RUN_ID, stage1, 'completed')
    assert r1['status'] == 'rejected' and r1['error_code'] == 'OUTPUT_PATH_BLOCKED'

    stage2 = tmp_path / 's2'
    deep = stage2 / 'a' / 'b' / 'c' / 'd' / 'e'
    deep.mkdir(parents=True)
    (deep / 'x.csv').write_text('1\n', encoding='utf-8')
    r2 = outputs.collect_and_publish(tmp_path, RUN_ID, stage2, 'completed')
    assert r2['status'] == 'rejected' and r2['error_code'] == 'OUTPUT_PATH_BLOCKED'

    stage3 = tmp_path / 's3'
    stage3.mkdir()
    for i in range(outputs.MAX_SCANNED_ENTRIES + 2):
        (stage3 / f'd{i}').mkdir()
    r3 = outputs.collect_and_publish(tmp_path, RUN_ID, stage3, 'completed')
    assert r3['status'] == 'rejected' and r3['error_code'] == 'OUTPUT_TOO_MANY'


def test_case_insensitive_collision_rejected(tmp_path, monkeypatch):
    stage = _make_stage(tmp_path)
    p1 = stage / 'Result.csv'
    p1.write_text('1\n', encoding='utf-8')
    fake_entries = [NS(name='Result.csv', path=str(p1)), NS(name='result.csv', path=str(p1))]
    class FakeScan:
        def __init__(self, items): self._items = items
        def __enter__(self): return iter(self._items)
        def __exit__(self, *a): return False
    monkeypatch.setattr(outputs.os, 'scandir', lambda d: FakeScan(fake_entries))
    res = outputs.collect_and_publish(tmp_path, RUN_ID, stage, 'completed')
    assert res['status'] == 'rejected' and res['error_code'] == 'OUTPUT_NAME_COLLISION'

    # Directory-directory and directory-file case collisions must also be rejected as OUTPUT_NAME_COLLISION.
    d1 = stage / 'Figs'
    d1.mkdir()
    monkeypatch.setattr(outputs.os, 'scandir', lambda d: FakeScan(
        [NS(name='Figs', path=str(d1)), NS(name='figs', path=str(d1))] if Path(d) == stage else []))
    res_dd = outputs.collect_and_publish(tmp_path, RUN_ID, stage, 'completed')
    assert res_dd['status'] == 'rejected' and res_dd['error_code'] == 'OUTPUT_NAME_COLLISION'

    monkeypatch.setattr(outputs.os, 'scandir', lambda d: FakeScan(
        [NS(name='Plot.png', path=str(d1)), NS(name='plot.png', path=str(p1))] if Path(d) == stage else []))
    res_df = outputs.collect_and_publish(tmp_path, RUN_ID, stage, 'completed')
    assert res_df['status'] == 'rejected' and res_df['error_code'] == 'OUTPUT_NAME_COLLISION'


def test_destination_guarded_rejection_and_atomic_rollback(tmp_path, monkeypatch):
    stage = _make_stage(tmp_path)
    (stage / 'a.csv').write_text('1\n', encoding='utf-8')
    (stage / 'b.csv').write_text('2\n', encoding='utf-8')

    # Destination blocked by edit_store.guarded (e.g., symlinked workbench/outputs).
    monkeypatch.setattr(outputs, 'guarded', lambda p: (_ for _ in ()).throw(ToolError('EDIT_PATH_BLOCKED', '禁止链接目录')))
    blocked = outputs.collect_and_publish(tmp_path, RUN_ID, stage, 'completed')
    assert blocked['status'] == 'rejected' and blocked['error_code'] == 'EDIT_PATH_BLOCKED'
    assert not (tmp_path / 'workbench' / 'outputs' / RUN_ID).exists()
    monkeypatch.undo()

    # Existing output dir is never overwritten.
    existing = tmp_path / 'workbench' / 'outputs' / RUN_ID
    existing.mkdir(parents=True)
    (existing / 'old.txt').write_text('keep', encoding='utf-8')
    dup = outputs.collect_and_publish(tmp_path, RUN_ID, stage, 'completed')
    assert dup['status'] == 'rejected' and dup['error_code'] == 'OUTPUT_EXISTS'
    assert (existing / 'old.txt').read_text(encoding='utf-8') == 'keep'

    # Failure mid-write rolls back temp dir cleanly with zero partial files left behind.
    run_id2 = 'b' * 32
    real_write = outputs._write_file_synced
    calls = {'n': 0}
    def fail_second(path, data):
        calls['n'] += 1
        if calls['n'] == 2:
            raise OSError('disk full')
        return real_write(path, data)
    monkeypatch.setattr(outputs, '_write_file_synced', fail_second)
    rolled = outputs.collect_and_publish(tmp_path, run_id2, stage, 'completed')
    assert rolled['status'] == 'rejected' and rolled['error_code'] == 'OUTPUT_PUBLISH_FAILED'
    assert not (tmp_path / 'workbench' / 'outputs' / run_id2).exists()
    assert list((tmp_path / 'workbench' / 'outputs').glob('.tmp-*')) == []


def test_runner_container_mount_and_end_to_end_export_receipt(tmp_path, monkeypatch):
    sha = _draft(tmp_path)
    sci_id = 'sha256:' + '9' * 64
    rt = {'docker': 'docker', 'image_id': sci_id, 'image_profile': 'scientific'}

    # Without export_outputs=True, mounting output_dir must be refused.
    with pytest.raises(ToolError) as exc:
        runner.container_command(rt, 'c1', tmp_path, export_outputs=False, output_dir=tmp_path / 'out')
    assert exc.value.error_code == 'ANALYSIS_ARGUMENT'

    # Commas or double quotes in mount paths must be refused to prevent Docker CSV mount injection.
    for bad_path in ('/tmp/a,b', '/tmp/a"b'):
        with pytest.raises(ToolError) as exc_out:
            runner.container_command(rt, 'c_bad', tmp_path, export_outputs=True, output_dir=bad_path)
        assert exc_out.value.error_code == 'ANALYSIS_ARGUMENT'
        with pytest.raises(ToolError) as exc_snap:
            runner.container_command(rt, 'c_bad', bad_path)
        assert exc_snap.value.error_code == 'ANALYSIS_ARGUMENT'

    # With export_outputs=True + output_dir, /output bind mount and env are added while isolation stays intact.
    cmd = runner.container_command(rt, 'c2', tmp_path, export_outputs=True, output_dir=tmp_path / 'out')
    assert cmd.count('--mount') == 2
    assert 'target=/analysis,readonly' in cmd[cmd.index('--mount') + 1]
    second_mount = cmd[cmd.index('--mount', cmd.index('--mount') + 1) + 1]
    assert second_mount.endswith(',target=/output') and 'readonly' not in second_mount
    assert 'ANALYSIS_OUTPUT_DIR=/output' in cmd
    for flag in ('--pull=never', '--network=none', '--read-only', '--cap-drop=ALL',
                 '--security-opt=no-new-privileges', '--user=65534:65534', '--memory=512m', '--memory-swap=512m'):
        assert flag in cmd

    # End-to-end AnalysisRunner.run(..., export_outputs=True) writes files + SHA-256 into receipt.
    monkeypatch.setattr(runner, 'probe', lambda workspace=None: {
        'available': True, 'docker': 'docker', 'image_id': sci_id, 'network': False,
        'profiles': {
            'default': {'available': True, 'image': runner.IMAGE, 'image_id': sci_id},
            'scientific': {'available': True, 'image': runner.SCIENTIFIC_IMAGE, 'image_id': sci_id},
        },
    })
    staged_paths = []
    def fake_exec(runtime, snapshot, seconds):
        out_dir = Path(runtime['output_dir'])
        staged_paths.append(out_dir)
        (out_dir / 'dos_fit.csv').write_bytes(b'E,DOS\n0.0,1.5\n')
        (out_dir / 'dos_fit.png').write_bytes(b'\x89PNG\r\n\x1a\nplot')
        return {'status': 'completed', 'output': 'done', 'exit_code': 0}
    monkeypatch.setattr(runner, 'execute', fake_exec)

    service = runner.AnalysisRunner(tmp_path)
    result = service.run('calc.py', sha, [], image_profile='scientific', export_outputs=True, request_id='exp-1')
    assert result['status'] == 'completed' and result['content_is_untrusted'] is True
    assert result['exported_outputs']['status'] == 'exported'
    assert result['exported_outputs']['file_count'] == 2
    assert result['exported_outputs']['content_is_untrusted'] is True
    # Staging directory must be cleaned up after run completes.
    assert not staged_paths[0].exists()

    # Receipt on disk under agent_paths analysis_runs matches exactly.
    receipt_file = agent_paths.store_item(tmp_path, 'analysis_runs', result['run_id'] + '.json')
    persisted = json.loads(receipt_file.read_text(encoding='utf-8'))
    assert persisted['content_is_untrusted'] is True
    assert persisted['exported_outputs'] == result['exported_outputs']
    for item in persisted['exported_outputs']['files']:
        actual = (tmp_path / item['path']).read_bytes()
        assert hashlib.sha256(actual).hexdigest() == item['sha256']

    # Idempotent replay with same request_id returns persisted exported_outputs without re-executing.
    replayed = service.run('calc.py', sha, [], image_profile='scientific', export_outputs=True, request_id='exp-1')
    assert replayed['replayed'] is False and replayed['exported_outputs'] == result['exported_outputs']
    assert len(staged_paths) == 1

    # End-to-end violation inside container rejects batch cleanly and records reason in receipt.
    def fake_bad_exec(runtime, snapshot, seconds):
        out_dir = Path(runtime['output_dir'])
        (out_dir / 'ok.csv').write_text('1,2\n', encoding='utf-8')
        (out_dir / 'evil.py').write_text('import os', encoding='utf-8')
        return {'status': 'completed', 'output': 'done', 'exit_code': 0}
    monkeypatch.setattr(runner, 'execute', fake_bad_exec)
    bad_res = service.run('calc.py', sha, [], image_profile='scientific', export_outputs=True, request_id='exp-bad')
    assert bad_res['status'] == 'completed' and bad_res['content_is_untrusted'] is True
    assert bad_res['exported_outputs']['status'] == 'rejected'
    assert bad_res['exported_outputs']['error_code'] == 'OUTPUT_SUFFIX_BLOCKED'
    assert bad_res['exported_outputs']['files'] == []
    assert not (tmp_path / 'workbench' / 'outputs' / bad_res['run_id']).exists()

    # End-to-end timeout/failed container skips export even if files were written, and cleans staging dir.
    for fail_status in ('timeout', 'failed'):
        fail_stages = []
        def fake_fail_exec(runtime, snapshot, seconds, _st=fail_status, _seen=fail_stages):
            out_dir = Path(runtime['output_dir'])
            _seen.append(out_dir)
            (out_dir / 'partial.csv').write_text('1,2\n', encoding='utf-8')
            return {'status': _st, 'output': 'stopped', 'exit_code': 1}
        monkeypatch.setattr(runner, 'execute', fake_fail_exec)
        fail_res = service.run('calc.py', sha, [], image_profile='scientific', export_outputs=True,
                               request_id=f'exp-{fail_status}')
        assert fail_res['status'] == fail_status and fail_res['content_is_untrusted'] is True
        assert fail_res['exported_outputs']['status'] == 'skipped'
        assert fail_res['exported_outputs']['files'] == []
        assert not (tmp_path / 'workbench' / 'outputs' / fail_res['run_id']).exists()
        assert not fail_stages[0].exists()


def test_t5_3_toolbox_lazy_group_schema_compatibility_and_dockerfile(tmp_path, monkeypatch):
    from vasp_agent.chat import tool_groups
    from vasp_agent.chat.ai_agent import Toolbox
    from vasp_agent.remote.remote_worker import ENGINE_MODULES, SOURCE_FILES

    # New parameters live strictly inside sandbox_analysis lazy group, never in CORE_TOOLS.
    assert 'run_analysis_script' in tool_groups.GROUPS['sandbox_analysis']['tools']
    assert 'get_analysis_runtime' in tool_groups.GROUPS['sandbox_analysis']['tools']
    assert 'run_analysis_script' not in tool_groups.CORE_TOOLS
    assert 'get_analysis_runtime' not in tool_groups.CORE_TOOLS

    sha = _draft(tmp_path)
    box = Toolbox(tmp_path)
    box.require_scan_plans = True
    box.lazy_tool_groups = True

    # Calling run_analysis_script before expanding sandbox_analysis is blocked cleanly.
    gated = box.call('run_analysis_script', {'script': 'calc.py', 'expected_sha256': sha, 'inputs': [], 'timeout_seconds': 5})
    assert gated['ok'] is False and gated['error_code'] == 'TOOL_GROUP_NOT_ENABLED'

    # Expand sandbox_analysis lazy group.
    enabled = box.call('enable_tool_group', {'groups': ['sandbox_analysis']})
    assert enabled['ok'] is True

    # get_analysis_runtime forwards workspace so .agent/state/analysis-sandbox.json is respected.
    seen_ws = []
    sci_id = 'sha256:' + 'c' * 64
    def fake_probe(workspace=None):
        seen_ws.append(workspace)
        return {
            'available': True, 'docker': 'docker', 'image_id': sci_id, 'network': False,
            'profiles': {
                'default': {'available': True, 'image': runner.IMAGE, 'image_id': sci_id},
                'scientific': {'available': True, 'image': runner.SCIENTIFIC_IMAGE, 'image_id': sci_id},
            },
        }
    monkeypatch.setattr(runner, 'probe', fake_probe)
    rt_res = box.call('get_analysis_runtime', {})
    assert rt_res['ok'] is True and rt_res['result']['profiles']['scientific']['available'] is True
    assert seen_ws == [tmp_path.resolve()]

    # Arbitrary image name is rejected at Toolbox schema validation before reaching AnalysisRunner.
    bad_img = box.call('run_analysis_script', {
        'script': 'calc.py', 'expected_sha256': sha, 'inputs': [], 'timeout_seconds': 5,
        'image_profile': 'ubuntu:latest', 'export_outputs': False,
    })
    assert bad_img['ok'] is False

    # Legacy 4-argument call (omitting image_profile & export_outputs) stays compatible.
    def fake_exec(runtime, snapshot, seconds):
        if runtime.get('output_dir'):
            (Path(runtime['output_dir']) / 'summary.csv').write_text('x,y\n1,2\n', encoding='utf-8')
        return {'status': 'completed', 'output': 'ok', 'exit_code': 0}
    monkeypatch.setattr(runner, 'execute', fake_exec)
    legacy_ok = box.call('run_analysis_script', {'script': 'calc.py', 'expected_sha256': sha, 'inputs': [], 'timeout_seconds': 5})
    assert legacy_ok['ok'] is True and legacy_ok['result']['image_profile'] == 'default'
    assert legacy_ok['result']['export_outputs'] is False and 'exported_outputs' not in legacy_ok['result']

    # Full scientific + export_outputs=True call succeeds through Toolbox without submission permission.
    sci_ok = box.call('run_analysis_script', {
        'script': 'calc.py', 'expected_sha256': sha, 'inputs': [], 'timeout_seconds': 5,
        'image_profile': 'scientific', 'export_outputs': True,
    })
    assert sci_ok['ok'] is True and not box.allow_submit
    assert sci_ok['result']['image_profile'] == 'scientific'
    assert sci_ok['result']['exported_outputs']['status'] == 'exported'
    assert sci_ok['result']['exported_outputs']['file_count'] == 1
    assert sci_ok['result']['content_is_untrusted'] is True

    # Dockerfile.analysis template checks and zero bundle leakage.
    repo_root = Path(__file__).resolve().parents[1]
    dockerfile = repo_root / 'docker' / 'Dockerfile.analysis'
    text = dockerfile.read_text(encoding='utf-8')
    assert text.startswith('FROM python:3.12-slim')
    for pkg in ('numpy', 'scipy', 'matplotlib'):
        assert pkg in text
    assert 'USER 65534:65534' in text
    assert 'COPY ' not in text and 'ADD ' not in text
    assert not any('analysis_outputs' in x or 'Dockerfile' in x for x in SOURCE_FILES)
    assert not any('analysis_outputs' in x or 'analysis_runner' in x for x in ENGINE_MODULES.values())
