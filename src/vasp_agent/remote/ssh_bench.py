"""Time read-only deployment_info calls over both SSH transports.

    python -m vasp_agent.remote.ssh_bench --server NAME [--workspace .] [-n 5]

Only deployment_info runs: it reads the deployment record and touches no task.
The pooled column's first call includes the handshake; the rest reuse it.
"""
import argparse
import json
import os
import statistics
import time

from vasp_agent.remote import ssh_pool
from vasp_agent.remote.remote_compute import RemoteCompute


def measure(remote, transport, count):
    os.environ[ssh_pool.ENV] = transport
    ssh_pool.POOL.reset()
    times, errors = [], []
    for _ in range(count):
        start = time.monotonic()
        try:
            remote.call('deployment_info')
        except Exception as exc:
            errors.append(f'{getattr(exc, "error_code", type(exc).__name__)}: {str(exc)[:200]}')
        times.append(round(time.monotonic() - start, 2))
    key = (str(remote.servers.root), remote.server)
    entry = ssh_pool.POOL.entries.get(key)
    pooled = transport == 'pooled' and entry is not None and entry.client is not None
    ssh_pool.POOL.close_all()
    result = {'transport': transport, 'seconds': times, 'errors': errors}
    if times:
        result['first'] = times[0]
        if len(times) > 1:
            result['median_after_first'] = statistics.median(times[1:])
    if transport == 'pooled':
        result['connection_reused'] = pooled
        if not pooled:
            result['note'] = '连接复用未生效，已退回 ssh 子进程（原因见上方 stderr）。'
    return result


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--server', required=True)
    parser.add_argument('--workspace', default='.')
    parser.add_argument('-n', '--count', type=int, default=5)
    args = parser.parse_args(argv)
    if not 1 <= args.count <= 50:
        parser.error('-n 须为 1–50')
    previous = os.environ.get(ssh_pool.ENV)
    remote = RemoteCompute(args.workspace, args.server)
    try:
        results = [measure(remote, transport, args.count) for transport in ('subprocess', 'pooled')]
    finally:
        if previous is None:
            os.environ.pop(ssh_pool.ENV, None)
        else:
            os.environ[ssh_pool.ENV] = previous
    print(json.dumps({'server': args.server, 'action': 'deployment_info', 'calls': args.count,
                      'results': results}, ensure_ascii=False, indent=2))
    return results


if __name__ == '__main__':
    main()
