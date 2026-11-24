"""Task listing, archiving, deletion previews, and previous-job scheduler checks.

Part of the stdin worker program (see remote_worker.WORKER_UNITS): standard library only.
"""
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess

# <bundle:package-only>
from vasp_agent.remote.worker_common import task_phases, write_json
from vasp_agent.remote.worker_slurm import array_scheduler_status
# </bundle:package-only>


def list_workflow_jobs(base, include_archived=False):
    """List only task metadata under the configured workspace, never VASP outputs."""
    rows = []
    base = Path(base).resolve()
    for folder in base.iterdir() if base.is_dir() else []:
        if not folder.is_dir() or not folder.resolve().is_relative_to(base) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_-]{0,63}', folder.name):
            continue
        archived = (folder / '.task-archived.json').exists()
        if archived and not include_archived:
            continue
        modified = folder.stat().st_mtime
        def read(name):
            nonlocal modified
            path = folder / name
            if not path.exists():
                return {}
            if not path.resolve().is_relative_to(base) or path.stat().st_size > 1_000_000:
                raise ValueError('元数据文件越界或过大')
            modified = max(modified, path.stat().st_mtime)
            data = json.loads(path.read_text(encoding='utf-8'))
            if not isinstance(data, dict):
                raise ValueError('元数据格式无效')
            return data
        try:
            manifest, state = read('manifest.json'), read('workflow-state.json')
            if not manifest:
                continue
            checkpoint, physics = read('checkpoint.json'), read('physics-model.json')
            phase = checkpoint.get('next', 'convergence')
            reason = ''
            if not manifest.get('stage_gates'):
                reason = '旧版任务不支持 /physics'
            elif phase != 'convergence' or (folder / 'submissions').exists() or (folder / '.agent.lock').exists():
                reason = '任务已进入执行流程，物理模型已锁定'
            rows.append({'name': folder.name, 'status': state.get('status', 'unknown'),
                'archived': archived,
                'phase': phase, 'physics_confirmed': bool(physics), 'goal': physics.get('goal', ''),
                'selectable': not reason, 'reason': reason, 'modified': modified})
        except (ValueError, OSError) as exc:
            rows.append({'name': folder.name, 'status': 'unreadable', 'phase': '-',
                'archived': archived,
                'physics_confirmed': False, 'selectable': False, 'reason': str(exc), 'modified': modified})
    rows.sort(key=lambda row: (row['modified'], row['name']), reverse=True)
    return {'jobs': rows[:100], 'total': len(rows), 'limit': 100}


def task_target(base, name):
    base = Path(base)
    def linked(path):
        return path.is_symlink() or getattr(path, 'is_junction', lambda: False)()
    if linked(base) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_-]{0,63}', name):
        raise ValueError('任务路径无效。')
    folder = base / name
    if linked(folder) or not folder.is_dir() or folder.resolve().parent != base.resolve():
        raise ValueError('任务不存在或路径越界。')
    manifest = folder / 'manifest.json'
    if linked(manifest) or not manifest.is_file():
        raise ValueError('目录不是可识别的 Agent 任务。')
    return folder.resolve()


def deletion_preview(base, name, require_scheduler=True):
    folder = task_target(base, name)
    if (folder / '.agent.lock').exists():
        raise ValueError('任务有运行锁，不能删除；请先核实运行状态。')
    entries = []
    def walk_error(error):
        raise error
    for parent, dirs, files in os.walk(folder, followlinks=False, onerror=walk_error):
        for child in sorted(dirs + files):
            path = Path(parent) / child
            if path.is_symlink() or getattr(path, 'is_junction', lambda: False)():
                raise ValueError('任务包含符号链接或目录联接，请人工检查，未删除。')
            if not path.resolve().is_relative_to(folder):
                raise ValueError('任务内路径越界。')
            st = path.stat()
            entries.append((str(path.relative_to(folder)), st.st_size, st.st_mtime_ns))
    job_ids = set()
    array_records = {}
    records = [folder / 'submission.json', folder / 'remote-submission.json', *sorted((folder / 'submissions').glob('*.json'))]
    for path in records:
        if path.exists():
            data = json.loads(path.read_text(encoding='utf-8'))
            job_id = str(data.get('job_id', ''))
            if not re.fullmatch(r'\d+', job_id):
                raise ValueError('存在结果不明确的提交记录，须先人工核实，不能删除。')
            job_ids.add(job_id)
            if data.get('submission_mode') == 'array':
                if job_id in array_records and array_records[job_id] != data:
                    raise ValueError('同一数组存在不一致提交记录，不能删除。')
                array_records[job_id] = data
    def query(command):
        try:
            result = subprocess.run(command, capture_output=True, text=True, timeout=20)
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise ValueError(f'{command[0]} 查询失败（{type(exc).__name__}）；无法确认作业状态，不能删除。') from exc
        returncode = getattr(result, 'returncode', 0)
        if returncode:
            detail = (getattr(result, 'stderr', '') or result.stdout or '').strip()[-1200:]
            raise ValueError(f'{command[0]} 查询失败（退出码 {returncode}）：{detail or "未返回诊断信息"}；无法确认作业状态，不能删除。')
        return result.stdout
    states = {}
    if require_scheduler or job_ids or shutil.which('squeue'):
        queued = query(['squeue', '--me', '-h', '-o', '%i|%Z'])
        for line in queued.splitlines():
            if not line.strip():
                continue
            fields = line.strip().split('|', 1)
            if len(fields) != 2 or not Path(fields[1]).is_absolute():
                raise ValueError('无法确认排队作业的工作目录，不能删除。')
            cwd = Path(fields[1]).resolve()
            if (fields[0] in job_ids or any(fields[0].startswith(key + '_') for key in array_records)
                    or cwd == folder or cwd.is_relative_to(folder)):
                raise ValueError('任务仍在排队或运行，不能删除。')
        terminal = {'COMPLETED', 'FAILED', 'CANCELLED', 'TIMEOUT', 'OUT_OF_MEMORY', 'NODE_FAIL'}
        for job_id in sorted(job_ids):
            if job_id in array_records:
                result = query(['sacct', '-n', '-j', job_id, '--format=JobID,State,ExitCode', '-P'])
                summary = array_scheduler_status(array_records[job_id], queued, result)
                if not summary['all_terminal']:
                    raise ValueError('无法确认全部数组元素已终止，不能删除。')
                states[job_id] = {item['job_id']: item['state'] for item in summary['tasks']}
                continue
            result = query(['sacct', '-n', '-j', job_id, '--format=JobIDRaw,State', '-P'])
            rows = [line.strip().split('|') for line in result.splitlines()]
            row = next((r for r in rows if len(r) >= 2 and r[0] == job_id), None)
            if row is None or not row[1].split() or row[1].split()[0] not in terminal:
                raise ValueError('无法确认作业已经终止，不能删除。')
            states[job_id] = row[1]
    state = json.loads((folder / 'workflow-state.json').read_text(encoding='utf-8')) if (folder / 'workflow-state.json').exists() else {}
    if state.get('status') in ('running', 'submitted', 'submission_uncertain') and not states:
        raise ValueError('工作流显示正在执行或提交状态不明确，不能删除。')
    fingerprint = hashlib.sha256(json.dumps(sorted(entries), ensure_ascii=True).encode()).hexdigest()
    return {'name': name, 'path': str(folder), 'workflow_status': state.get('status', 'unknown'),
            'job_states': states, 'queue_checked': bool(require_scheduler or job_ids or shutil.which('squeue')),
            'archived': (folder / '.task-archived.json').exists(), 'fingerprint': fingerprint}


def manage_task(base, name, action, *, confirmation=None, fingerprint=None, require_scheduler=True):
    folder = task_target(base, name)
    marker = folder / '.task-archived.json'
    if marker.is_symlink():
        raise ValueError('归档标记是链接，拒绝操作。')
    if action == 'archive':
        if marker.with_suffix('.tmp').exists() or marker.with_suffix('.tmp').is_symlink():
            raise ValueError('归档临时文件已存在，请先检查。')
        write_json(marker, {'archived': True})
        return {'archived': True, 'path': str(folder), 'note': '文件保留；不会取消已有作业。'}
    if action == 'restore':
        marker.unlink(missing_ok=True)
        return {'archived': False, 'path': str(folder)}
    if action == 'delete_preview':
        return deletion_preview(base, name, require_scheduler)
    if action == 'delete':
        if confirmation != name or not fingerprint:
            raise ValueError('必须输入完整任务名确认删除。')
        current = deletion_preview(base, name, require_scheduler)
        if current['fingerprint'] != fingerprint:
            raise ValueError('确认期间任务文件已变化，请重新查看后再决定。')
        target = task_target(base, name)
        if target != folder or target.parent != Path(base).resolve():
            raise ValueError('删除目标路径变化，未删除。')
        shutil.rmtree(target)
        return {'deleted': True, 'path': str(target)}
    raise ValueError('未知任务管理操作。')


def scheduler_query(command, consequence):
    """One strict squeue/sacct read: any failure refuses, naming what it blocks."""
    try:
        return subprocess.run(command, capture_output=True, text=True, timeout=20, check=True).stdout
    except subprocess.CalledProcessError as exc:
        detail = (exc.stderr or exc.stdout or '').strip()[-1200:]
        raise ValueError(f'{command[0]} 查询失败（退出码 {exc.returncode}）：{detail or "未返回诊断信息"}；{consequence}。') from exc
    except subprocess.TimeoutExpired as exc:
        raise ValueError(f'{command[0]} 查询超时；{consequence}。') from exc


def check_previous_job(folder, phase):
    """Check live queue independently of whether Slurm still knows the old ID."""
    phases = task_phases(folder)
    if phase not in phases:
        raise ValueError('无效阶段。')
    if phase == phases[0]:
        return
    previous = folder / 'submissions' / (phases[phases.index(phase) - 1] + '.json')
    if not previous.exists():
        return  # Direct execution in an allocation is checked by stage evidence.
    receipt = json.loads(previous.read_text(encoding='utf-8'))
    job_id = str(receipt.get('job_id', ''))
    if not re.fullmatch(r'[0-9]+', job_id):
        raise ValueError('上阶段提交结果不明确，请人工核对。')
    def query(command):
        return scheduler_query(command, '未确认上阶段退出，禁止提交')
    queued = query(['squeue', '--me', '-h', '-o', '%i'])
    if any(line.strip() == job_id or line.strip().startswith(job_id + '_') for line in queued.splitlines()):
        raise ValueError('上阶段作业尚未退出，请稍后再提交。')
    accounting = query(['sacct', '-n', '-j', job_id,
                        '--format=JobID,State,ExitCode' if receipt.get('submission_mode') == 'array' else '--format=JobIDRaw,State,ExitCode', '-P'])
    if receipt.get('submission_mode') == 'array':
        summary = array_scheduler_status(receipt, queued, accounting)
        if not summary['all_successful']:
            raise ValueError('尚不能确认全部数组测试正常退出；存在未完成、失败或缺失记账，禁止继续提交。')
        return
    rows = [[field.strip() for field in line.split('|')] for line in accounting.splitlines()]
    if not any(len(row) >= 3 and row[:3] == [job_id, 'COMPLETED', '0:0'] for row in rows):
        raise ValueError('尚不能确认上阶段正常退出；请检查队列记录，禁止继续提交。')
