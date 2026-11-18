"""Fixed read-only scheduler queries; no conversational/model API traffic."""
from pathlib import Path

from vasp_agent.monitoring.job_monitor import JobMonitor


def poll_job(workspace, task):
    JobMonitor._identity(task['server'], task['job_name'], task.get('phase'), task['job_id'])
    if task['server'] == 'local':
        root = Path(workspace).resolve(strict=True)
        folder = (root / 'runs' / task['job_name']).resolve(strict=True)
        if not folder.is_relative_to(root):
            raise ValueError('监控任务目录越界。')
        from vasp_agent.remote.remote_worker import monitor_status
        return monitor_status(folder, task['job_id'], task.get('phase'), local=True)
    from vasp_agent.remote.remote_compute import RemoteCompute
    return RemoteCompute(workspace, task['server']).call(
        'monitor_status', task['job_name'], job_id=task['job_id'], phase=task.get('phase'))
