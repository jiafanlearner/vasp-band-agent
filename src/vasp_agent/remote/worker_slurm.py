"""Slurm queries and output parsing: capacity, accounting, array state, job monitoring.

Part of the stdin worker program (see remote_worker.WORKER_UNITS): standard library
and earlier units only. Nothing here submits, cancels or rewrites a receipt.
"""
from datetime import datetime, timedelta
import json
import os
from pathlib import Path
import re
import shlex
import subprocess

# <bundle:package-only>
from vasp_agent.remote.worker_common import SubmissionError, diagnostic_text, task_phases
# </bundle:package-only>


def scheduler_read(command):
    try:
        result = subprocess.run(command, capture_output=True, text=True, timeout=20)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise SubmissionError('SCHEDULER_CAPACITY_UNKNOWN',
            f'{command[0]} 容量查询失败（{type(exc).__name__}）；未提交，不把未知容量视为足够。') from exc
    if result.returncode or not result.stdout.strip() or len(result.stdout) > 2_000_000:
        raise SubmissionError('SCHEDULER_CAPACITY_UNKNOWN',
            f'{command[0]} 容量查询无有效结果；未提交。' + diagnostic_text(result.stderr))
    return result.stdout


def capacity_preflight(script, read=None):
    """Check hardware bounds only. This does not predict queue time or grant access."""
    read = read or scheduler_read
    values = dict(re.findall(r'^#SBATCH --([\w-]+)=([^\n]+)$', script, re.M))
    if re.search(r'^\s*#SBATCH\s+--mem(?:ory)?(?:-per-[a-z]+)?(?:=|\s)', script, re.M):
        raise SubmissionError('SCHEDULER_CAPACITY_UNKNOWN', '当前容量检查只支持受控脚本的分区默认内存，显式内存参数需专门核验。')
    partition = values.get('partition', '')
    if not re.fullmatch(r'[A-Za-z0-9_-]+', partition):
        raise SubmissionError('SCHEDULER_CAPACITY_UNKNOWN', '无法解析分区，未提交。')
    try:
        nodes, tasks, cpus = int(values['nodes']), int(values['ntasks']), int(values.get('cpus-per-task', 1))
        if min(nodes, tasks, cpus) < 1 or tasks % nodes:
            raise ValueError()
    except (KeyError, ValueError):
        raise SubmissionError('SCHEDULER_CAPACITY_UNKNOWN', '无法解析均分节点布局，未提交。') from None
    # Query exact CPU/memory groups; unavailable/down nodes are still hardware
    # capacity, not a promise they can currently be allocated.
    raw = read(['sinfo', '-h', '-e', '-p', partition, '-o', '%c|%m|%D'])
    groups = []
    for line in raw.splitlines():
        match = re.fullmatch(r'\s*(\d+)\|(\d+)\|(\d+)\s*', line)
        if not match or min(map(int, match.groups())) <= 0:
            raise SubmissionError('SCHEDULER_CAPACITY_UNKNOWN', 'sinfo 节点规格格式不明确，未提交。')
        cpu, memory, count = map(int, match.groups())
        groups.append({'cpus': cpu, 'memory_mb': memory, 'nodes': count})
    per_node = tasks // nodes * cpus
    max_cpu = max(g['cpus'] for g in groups)
    if sum(g['nodes'] for g in groups if g['cpus'] >= per_node) < nodes:
        raise SubmissionError('SCHEDULER_CAPACITY_EXCEEDED',
            f'分区 {partition} 最大 {max_cpu} 核/节点；本次 {nodes} 节点、{tasks * cpus} 总核，'
            f'要求每节点 {per_node} 核，满足条件的节点数量不足。未提交；请重新选择资源并审阅。')
    info = read(['scontrol', 'show', 'partition', partition, '-o'])
    setting = re.findall(r'\bDefMemPer(CPU|Node)=([0-9]+(?:[KMGT])?|UNLIMITED)\b', info)
    if len(setting) != 1:
        raise SubmissionError('SCHEDULER_CAPACITY_UNKNOWN', '分区默认内存配置不明确，未提交。')
    kind, amount = setting[0]
    required_memory = 0
    if amount not in ('0', 'UNLIMITED'):
        units = re.fullmatch(r'(\d+)([KMGT]?)', amount)
        memory_mb = int(units[1]) * {'': 1, 'K': 1 / 1024, 'M': 1, 'G': 1024, 'T': 1024 * 1024}[units[2]]
        required_memory = memory_mb * per_node if kind == 'CPU' else memory_mb
    eligible = sum(g['nodes'] for g in groups if g['cpus'] >= per_node and g['memory_mb'] >= required_memory)
    if eligible < nodes:
        raise SubmissionError('SCHEDULER_CAPACITY_EXCEEDED',
            f'分区 {partition} 无足够节点同时满足每节点 {per_node} 核及默认内存 {required_memory:g} MB；未提交。')
    return {'partition': partition, 'nodes': nodes, 'cpus_per_node': per_node,
            'default_memory_mb_per_node': required_memory, 'node_types': groups,
            'capacity_checked': True, 'note': '只验证CPU及默认内存容量，不保证配额、节点可用性或调度成功。'}


def accounting_window(path, receipt, start_time=None, end_time=None):
    """Bound searches; format dates in the remote server's local time for Slurm."""
    if (start_time is None) != (end_time is None):
        raise ValueError('start_time 和 end_time 必须同时提供。')
    if start_time is not None:
        try:
            start = datetime.fromisoformat(start_time).astimezone()
            end = datetime.fromisoformat(end_time).astimezone()
        except (ValueError, TypeError):
            raise ValueError('查询时间须为 ISO 日期时间。') from None
        source = 'operator_window'
    else:
        stamp = (receipt or {}).get('submission_diagnostic', {}).get('started_at')
        try:
            anchor = datetime.fromisoformat(stamp).astimezone() if stamp else None
        except (ValueError, TypeError):
            anchor = None
        source = 'receipt_started_at'
        if anchor is None and path.exists():
            anchor = datetime.fromtimestamp(path.stat().st_mtime).astimezone()
            source = 'receipt_mtime_estimate'
        if anchor is not None:
            start, end = anchor - timedelta(days=1), anchor + timedelta(days=1)
        else:
            end = datetime.now().astimezone()
            start = end - timedelta(days=7)
            source = 'last_seven_days'
    if not timedelta(0) < end - start <= timedelta(days=31):
        raise ValueError('历史查询时间范围须大于0且不超过31天。')
    return {'start': start.strftime('%Y-%m-%dT%H:%M:%S'),
            'end': end.strftime('%Y-%m-%dT%H:%M:%S'), 'source': source,
            'timezone': 'remote_server_local',
            'note': '默认时间窗仅用于查找，文件修改时间不是提交成功证据；可指定时间窗继续查询。'}


def submission_accounting(folder, phase, job_id=None, start_time=None, end_time=None):
    """Read receipts/accounting for an explicit target, never reconcile or resubmit."""
    if phase is not None and phase not in task_phases(folder):
        raise ValueError('阶段无效。')
    path = folder / 'submissions' / (phase + '.json') if phase else folder / 'remote-submission.json'
    if path.is_symlink():
        raise ValueError('提交记录不能是链接。')
    if path.exists() and path.stat().st_size > 100_000:
        raise ValueError('提交记录过大，停止诊断。')
    receipt = json.loads(path.read_text(encoding='utf-8')) if path.exists() else None
    def clean(value):
        if isinstance(value, str):
            return diagnostic_text(value)
        if isinstance(value, dict):
            return {key: clean(item) for key, item in value.items()}
        if isinstance(value, list):
            return [clean(item) for item in value]
        return value
    receipt = clean(receipt)
    job_id = job_id or (receipt or {}).get('job_id')
    result = {'receipt': receipt, 'record_modified': False, 'content_is_untrusted': True,
              'note': '只读诊断不授予提交权限，不自动更正回执或重新提交。'}
    discovery = not job_id
    command = ['sacct', '-X', '-n', '-P', '-u', str(os.getuid())]
    if discovery:
        window = accounting_window(path, receipt, start_time, end_time)
        result['search_window'] = window
        command += ['--name=va-' + folder.name, '-S', window['start'], '-E', window['end']]
    else:
        job_id = str(job_id)
        if not re.fullmatch(r'[0-9]+', job_id):
            raise ValueError('JobID必须为数字。')
        command += ['-j', job_id]
    result['lookup_mode'] = 'discover' if discovery else 'job_id'
    command += ['--format=JobIDRaw,JobName%128,State,ExitCode,Submit,Start,End,NodeList%256,WorkDir%1024,Reason%256,Partition,ReqCPUS,ReqNodes,ReqMem,ReqTRES%512,AllocCPUS,AllocNodes,Elapsed']
    try:
        account = subprocess.run(command, capture_output=True, text=True, timeout=20)
    except (OSError, subprocess.TimeoutExpired) as exc:
        return {**result, 'accounting': None, 'query_error': type(exc).__name__}
    if len(account.stdout) > 2_000_000:
        return {**result, 'accounting': None, 'query_error': '记账结果过大。'}
    if account.returncode:
        return {**result, 'accounting': None, 'query_error': diagnostic_text(account.stderr)}
    matches = []
    unmatched = 0
    malformed = 0
    for line in account.stdout.splitlines():
        # Empty Reason is a valid last field; do not lose it to rstrip.
        fields = line.split('|')
        if not line.strip():
            continue
        if len(fields) < 10 or not re.fullmatch(r'[0-9]+', fields[0].strip()):
            malformed += 1
            continue
        if not discovery and fields[0].strip() != job_id:
            continue
        extra = [diagnostic_text(field.strip()) for field in fields[10:18]]
        fields = [field.strip() for field in fields[:10]]
        if fields[1] != 'va-' + folder.name or fields[8] != str(folder):
            if not discovery:
                raise ValueError('历史JobID的作业名称或工作目录与目标不一致；不能关联此任务。')
            unmatched += 1
            continue
        fields = [diagnostic_text(field) for field in fields]
        matches.append(dict(zip(('job_id', 'job_name', 'state', 'exit_code', 'submit', 'start', 'end',
                                 'node_list', 'work_dir', 'reason'), fields)))
        matches[-1].update(dict(zip(('partition', 'requested_cpus', 'requested_nodes', 'requested_memory',
                                    'requested_tres', 'allocated_cpus', 'allocated_nodes', 'elapsed'), extra)))
    unique = len(matches) == 1 and malformed == 0
    return {**result, 'accounting': matches[:100], 'candidate_count': len(matches),
            'truncated': len(matches) > 100, 'unmatched_count': unmatched, 'malformed_rows': malformed,
            'target_matched': unique, 'discovered_job_id': matches[0]['job_id'] if discovery and unique else None,
            'lookup_status': 'incomplete' if malformed else 'unique_match' if unique else 'ambiguous' if matches else 'no_match',
            'phase_verified': False,
            'next': '多个候选不能自动选择；唯一匹配只核实任务名称及目录，不能单凭记账确认阶段。查询为空不代表未提交；不得自动写回回执或重交。'}


def submission_command_evidence(folder, row, phase):
    """Supplementary query: unsupported SubmitLine cannot erase basic accounting."""
    command = ['sacct', '-X', '-n', '-P', '-u', str(os.getuid()), '-j', row['job_id'],
               '--format=JobIDRaw,JobName%128,WorkDir%1024,UID,User%128,Submit,SubmitLine%4096']
    result = {'source': 'sacct.SubmitLine', 'command': command, 'status': 'unknown',
              'submission_phase_verified': False, 'execution_phase_verified': False,
              'attempt_binding_verified': False}
    try:
        reply = subprocess.run(command, capture_output=True, text=True, timeout=20)
    except (OSError, subprocess.TimeoutExpired) as exc:
        return {**result, 'query_error': type(exc).__name__}
    if reply.returncode or len(reply.stdout) > 100000:
        return {**result, 'query_error': diagnostic_text(reply.stderr) or '历史命令查询失败或结果过大。',
                'returncode': reply.returncode}
    rows = [line.split('|', 6) for line in reply.stdout.splitlines() if line.strip()]
    rows = [fields for fields in rows if len(fields) == 7 and fields[0].strip() == row['job_id']]
    if len(rows) != 1:
        return {**result, 'status': 'missing_or_ambiguous'}
    job, name, directory, uid, user, submitted, line = [field.strip() for field in rows[0]]
    if (name != 'va-' + folder.name or directory != str(folder) or uid != str(os.getuid())
            or submitted != row.get('submit')):
        return {**result, 'status': 'identity_mismatch'}
    result.update(user=diagnostic_text(user), uid=uid, submit=submitted,
                  submit_line=diagnostic_text(line), status='available')
    # Parse only direct sbatch --export arguments; no shell execution or substring inference.
    try:
        tokens = shlex.split(line)
    except ValueError:
        return {**result, 'status': 'unparseable'}
    exports = []
    if not tokens or Path(tokens[0]).name != 'sbatch':
        return {**result, 'status': 'unsupported_command'}
    for index, token in enumerate(tokens[1:], 1):
        if token.startswith('--export='):
            exports.append(token.split('=', 1)[1])
        elif token == '--export' and index + 1 < len(tokens):
            exports.append(tokens[index + 1])
    phases = [item.split('=', 1)[1] for export in exports for item in export.split(',')
              if item.startswith('VASP_AGENT_PHASE=')]
    declared = phases[0] if len(exports) == 1 and len(phases) == 1 and phases[0] in task_phases(folder) else None
    result.update(declared_phase=declared, submission_phase_verified=bool(phase and declared == phase),
                  status='phase_match' if phase and declared == phase else 'phase_mismatch' if declared else 'phase_unknown',
                  note='只证明历史提交命令指定阶段；不证明阶段启动、不确定回执与该尝试的唯一绑定或允许重交。')
    return result


# Array bounds, shared by the planner, the menu, the script parser and the
# accounting check: 3 ENCUT + 3 mesh + 1 cross-cutoff test, at most 6 at once.
MAX_ARRAY_TESTS = 7
MAX_ARRAY_CONCURRENCY = 6


def array_scheduler_status(receipt, queue, accounting):
    """Require all array elements, never a parent's completion row alone."""
    parent = str(receipt.get('job_id', ''))
    count = receipt.get('array_task_count')
    if not re.fullmatch(r'\d+', parent) or type(count) is not int or not 1 <= count <= MAX_ARRAY_TESTS:
        raise ValueError('数组提交记录缺少合法父编号或测试数量，不能确认完成。')
    expected = list(range(count))
    if receipt.get('array_task_ids', expected) != expected:
        raise ValueError('数组元素清单与提交记录不一致。')
    live = {}
    parent_live = False
    for line in (queue or '').splitlines():
        fields = line.strip().split('|')
        key = fields[0]
        if key == parent or key.startswith(parent + '_['):
            parent_live = True
        match = re.fullmatch(re.escape(parent) + r'_(\d+)', key)
        if match:
            live[int(match[1])] = fields[1] if len(fields) > 1 else 'ACTIVE'
    rows = {}
    for line in (accounting or '').splitlines():
        fields = [part.strip() for part in line.split('|')]
        match = re.fullmatch(re.escape(parent) + r'_(\d+)', fields[0]) if fields else None
        if match and len(fields) >= 3:
            rows.setdefault(int(match[1]), []).append((fields[1], fields[2]))
    terminal_states = {'COMPLETED', 'FAILED', 'CANCELLED', 'TIMEOUT', 'OUT_OF_MEMORY',
                       'NODE_FAIL', 'PREEMPTED', 'BOOT_FAIL', 'DEADLINE', 'REVOKED'}
    tasks = []
    unexpected = sorted((set(live) | set(rows)) - set(expected))
    for index in expected:
        values = rows.get(index, [])
        terminal = bool(values) and all(bool(state.split()) and state.split()[0].rstrip('+') in terminal_states for state, _ in values)
        success = bool(values) and all(state == 'COMPLETED' and code == '0:0' for state, code in values)
        tasks.append({'task_id': index, 'job_id': f'{parent}_{index}',
                      'state': live.get(index) or (values[-1][0] if values else 'UNKNOWN'),
                      'exit_code': values[-1][1] if values else None,
                      'terminal': terminal and index not in live and not parent_live,
                      'successful': success and index not in live and not parent_live})
    return {'job_id': parent, 'tasks': tasks, 'task_count': count,
            'active': parent_live or bool(live), 'known': bool(live) or parent_live or any(rows.values()),
            'unexpected_task_ids': unexpected,
            'all_terminal': not parent_live and not live and not unexpected and all(item['terminal'] for item in tasks),
            'all_successful': not parent_live and not live and not unexpected and all(item['successful'] for item in tasks)}


def monitor_status(folder, job_id, phase=None, local=False):
    """Read exact confirmed receipt and scheduler state; never collect or submit."""
    folder = Path(folder).resolve(strict=True)
    job_id = str(job_id)
    if not re.fullmatch(r'[0-9]+', job_id) or phase is not None and phase not in task_phases(folder):
        raise ValueError('监控需要有效 JobID 和阶段。')

    def read(relative, required=False):
        path = folder / relative
        if not path.resolve().is_relative_to(folder) or path.is_symlink():
            raise ValueError('监控记录路径越界或为符号链接。')
        if not path.exists() and not required:
            return {}
        if path.stat().st_size > 200_000:
            raise ValueError('监控元数据过大。')
        data = json.loads(path.read_text(encoding='utf-8'))
        if not isinstance(data, dict):
            raise ValueError('监控元数据格式无效。')
        return data

    receipt_name = f'submissions/{phase}.json' if phase else ('submission.json' if local else 'remote-submission.json')
    receipt = read(receipt_name, required=True)
    if (receipt.get('status') != 'submitted' or str(receipt.get('job_id', '')) != job_id
            or (phase is not None and receipt.get('phase', phase) != phase)):
        raise ValueError('监控 JobID 与该阶段实际提交回执不符；不追踪其他作业。')

    def query(command):
        try:
            result = subprocess.run(command, capture_output=True, text=True, timeout=20)
            if result.returncode or len(result.stdout) > 100_000:
                return None
            return result.stdout.strip()
        except (OSError, subprocess.TimeoutExpired):
            return None

    is_array = receipt.get('submission_mode') == 'array'
    queue = query(['squeue', '-h', '-j', job_id, '-o', '%i|%T|%M|%R'] + (['--array'] if is_array else []))
    accounting = query(['sacct', '-n', '-j', job_id, '--format=JobID,State,ExitCode', '-P']) if not queue or is_array else None
    if queue is None and accounting is None:
        raise SubmissionError('MONITOR_QUERY_FAILED', '调度查询失败；状态未知，稍后重试，不提交或取消。')
    state = read('workflow-state.json')
    checkpoint = read('checkpoint.json')
    result = {'state': 'UNKNOWN', 'terminal': False, 'outcome': None,
              'workflow_status': state.get('status'), 'next_phase': checkpoint.get('next') or state.get('next'),
              'message': '调度状态未知；查询为空不代表作业完成。'}
    # The UI cannot turn a scheduler completion into an unverified full workflow completion.
    if result['workflow_status'] == 'complete' and not (
            checkpoint.get('next') == 'complete'
            and checkpoint.get('completed') == list(task_phases(folder))
            and read('report.json')):
        result['workflow_status'] = 'completion_unverified'
    if is_array:
        summary = array_scheduler_status(receipt, queue, accounting)
        result['array_progress'] = {'total': summary['task_count'],
            'terminal': sum(item['terminal'] for item in summary['tasks']),
            'successful': sum(item['successful'] for item in summary['tasks'])}
        if summary['all_terminal']:
            result.update(state='COMPLETED' if summary['all_successful'] else 'FAILED', terminal=True,
                          outcome='completed' if summary['all_successful'] else 'failed')
        elif summary['known']:
            result['state'] = 'RUNNING' if any(item['state'] in ('RUNNING', 'COMPLETING') for item in summary['tasks']) else 'ARRAY_PENDING_OR_PARTIAL'
        result['message'] = '数组各项独立检查；所有元素结束后仍需真实输出验收和阶段审阅。'
        return result
    live = [line.split('|') for line in (queue or '').splitlines() if line.split('|')[0] == job_id]
    if live and all(len(row) >= 2 and row[1].strip() for row in live):
        if len({row[1].strip() for row in live}) == 1:
            result.update(state=live[0][1].strip(), message='作业仍在调度队列中。')
        return result
    rows = [[part.strip() for part in line.split('|')] for line in (accounting or '').splitlines()]
    rows = [row for row in rows if len(row) >= 3 and row[0] == job_id]
    if not rows or len({(row[1], row[2]) for row in rows}) != 1:
        return result
    current, code = rows[0][1].split()[0] if rows[0][1].split() else 'UNKNOWN', rows[0][2]
    current = current.rstrip('+')
    terminal = current in {'COMPLETED', 'FAILED', 'CANCELLED', 'TIMEOUT', 'OUT_OF_MEMORY', 'NODE_FAIL', 'PREEMPTED', 'BOOT_FAIL', 'DEADLINE', 'REVOKED'}
    if current == 'COMPLETED' and not re.fullmatch(r'[0-9]+:[0-9]+', code):
        return result
    outcome = ('completed' if code == '0:0' else 'failed') if current == 'COMPLETED' else 'cancelled' if current == 'CANCELLED' else 'failed' if terminal else None
    result.update(state='FAILED' if current == 'COMPLETED' and outcome == 'failed' else current,
                  terminal=terminal, outcome=outcome,
                  message='调度阶段已结束；仍需检查真实输出及下一阶段审阅。' if terminal else '调度状态已读取。')
    return result


def array_layout(script):
    """Only a bounded contiguous array with an explicit concurrency cap is supported."""
    values = re.findall(r'^\s*#SBATCH\s+--array(?:=|\s+)([^\s]+)\s*$', script, re.M)
    if not values:
        return None
    if len(values) != 1:
        raise ValueError('数组参数重复，不能确认资源上限。')
    match = re.fullmatch(r'0-(0|[1-9]\d{0,2})%([1-9]\d{0,2})', values[0])
    if (not match or not 1 <= int(match[1]) + 1 <= MAX_ARRAY_TESTS
            or not 1 <= int(match[2]) <= min(MAX_ARRAY_CONCURRENCY, int(match[1]) + 1)):
        raise ValueError(f'数组仅支持 0-N%K；1–{MAX_ARRAY_TESTS} 项测试、最多 {MAX_ARRAY_CONCURRENCY} 项并发，'
                         '不接受无并发上限的数组。')
    return {'task_count': int(match[1]) + 1, 'concurrency': int(match[2]),
            'task_ids': list(range(int(match[1]) + 1))}
