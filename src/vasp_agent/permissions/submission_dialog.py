"""Per-submission resource choices, entered by the operator, never by the model."""
import re
from uuid import uuid4


def validate_mode(resources, phase):
    mode = resources.get('submission_mode', 'allocation')
    if not isinstance(mode, str) or mode not in ('allocation', 'array'):
        raise ValueError('提交模式须为 allocation 或 array。')
    if mode == 'array' and phase != 'convergence':
        raise ValueError('array 数组提交模式仅支持 convergence 收敛扫描；本阶段必须使用 allocation。')
    return mode


def same_resources(left, right):
    """An omitted mode denotes allocation in old selections and remote receipts."""
    return (isinstance(left, dict) and isinstance(right, dict)
            and {**left, 'submission_mode': left.get('submission_mode', 'allocation')}
            == {**right, 'submission_mode': right.get('submission_mode', 'allocation')})


def validate_field(field, resources, phase, limits, tests=None):
    """Validate only the current field; earlier fields have already been checked."""
    from vasp_agent.remote.remote_worker import wall_seconds
    if not limits:
        raise ValueError('请先通过 /submit 设置资源授权上限。')
    mode = validate_mode(resources, phase)
    if field == 'submission_mode':
        return
    value = resources[field]
    if field == 'parallel_tests':
        maximum = 6 if phase in (None, 'convergence') else 1
        if tests is not None:
            maximum = min(maximum, tests)
        if type(value) is not int or not 1 <= value <= maximum:
            raise ValueError(f'同时运行的测试数须为 1–{maximum}；本阶段最多可并发 {maximum} 组，不能填 {value}。')
        if value > limits['cores']:
            raise ValueError(f'并发 {value} 组至少需要 {value} 核，超过授权上限 {limits["cores"]} 核。')
        if mode == 'array' and value > limits['nodes']:
            raise ValueError(f'并发 {value} 个数组元素至少需要 {value} 节点，超过节点并发峰值授权上限 {limits["nodes"]}。')
    elif field == 'cores_per_test':
        if type(value) is not int or not 1 <= value <= 65536:
            raise ValueError('每组 MPI 进程数须为 1–65536 的整数。')
        parallel = resources['parallel_tests']
        total = parallel * value
        maximum = min(65536, limits['cores'])
        if total > maximum:
            raise ValueError(f'{parallel} 组 × 每组 {value} 进程 = {total} 核，超过总核数上限 {maximum}；当前并发数下每组最多 {maximum // parallel} 进程。')
    elif field == 'nodes':
        cores = resources['cores_per_test']
        maximum = min(256, limits['nodes'] // (resources['parallel_tests'] if mode == 'array' else 1))
        choices = [n for n in range(1, min(maximum, cores) + 1) if cores % n == 0]
        if type(value) is not int or not 1 <= value <= maximum or cores % value:
            scope = '每个数组元素跨本项节点均分' if mode == 'array' else '每组跨全部节点均分'
            peak = f'数组授权覆盖 {resources["parallel_tests"]} 个并发元素，节点峰值为并发数 × 每项节点数。' if mode == 'array' else ''
            raise ValueError(
                f'节点数 {value} 不适用于每组 {cores} 个 MPI 进程：{scope}，节点数必须整除 {cores}，且不得超过节点授权/程序上限 {maximum}。'
                f'符合这些条件的节点数：{", ".join(map(str, choices))}。'
                + peak +
                '这里填写物理节点数，不是总核数；仍需核实每节点 CPU 容量。')
    elif field == 'partition':
        if not isinstance(value, str) or not re.fullmatch(r'[A-Za-z0-9_-]+', value):
            raise ValueError('分区名称无效。')
        if value != limits['partition']:
            raise ValueError(f'分区 {value} 不在当前授权内；已授权分区为 {limits["partition"]}。')
    elif field == 'walltime':
        if wall_seconds(value) > wall_seconds(limits['walltime']):
            raise ValueError(f'运行时限 {value} 超过授权上限 {limits["walltime"]}。')


# Older workers do not report execution_plan.tests; both scans, no cross-cutoff.
DEFAULT_ARRAY_TESTS = 6


def brief(resources):
    """One line of resources for the operator, instead of a JSON object."""
    r = resources
    return (f"{r.get('submission_mode', 'allocation')}，{r['parallel_tests']} 组 × 每组 {r['cores_per_test']} MPI，"
            f"{r['nodes']} 节点，{r['partition']}，{r['walltime']}")


def describe_plan(plan):
    """The convergence execution plan in words: what runs, what is reused, what follows."""
    names = {'encut': 'ENCUT', 'mesh': 'k 网格'}
    parts = [f'{names.get(kind, kind)} 3 项' for kind in plan.get('compute', ['encut', 'mesh'])]
    if plan.get('cross_cutoff'):
        parts.append('交叉复核 1 项')
    text = f"{plan.get('tests', DEFAULT_ARRAY_TESTS)} 项测试（{' + '.join(parts)}）"
    reuse = plan.get('reuse') or []
    reuse = [reuse] if isinstance(reuse, str) else reuse
    if reuse:
        text += '；复用 ' + '、'.join(names.get(kind, kind) for kind in reuse) + '（来源已通过）'
    checks = {'higher-cutoff': '更高截断能', 'denser-mesh': '更密网格'}
    if plan.get('post_relax_checks'):
        text += '；优化后另做复核：' + '、'.join(checks.get(x, x) for x in plan['post_relax_checks'])
    return text


def validate_resources(resources, phase, limits, tests=None):
    from vasp_agent.remote.remote_worker import MAX_ARRAY_TESTS, check_resources
    mode = validate_mode(resources, phase)
    for field in ('parallel_tests', 'cores_per_test', 'nodes', 'partition', 'walltime'):
        validate_field(field, resources, phase, limits, tests)
    parallel, cores, nodes = (resources[k] for k in ('parallel_tests', 'cores_per_test', 'nodes'))
    directives = dict(partition=resources['partition'], nodes=nodes,
                      ntasks=cores if mode == 'array' else parallel * cores,
                      time=resources['walltime'])
    if mode == 'array':
        if tests is not None and (type(tests) is not int or tests < 1):
            raise ValueError('当前执行清单没有可用的独立测试数量。')
        count = tests if tests is not None else DEFAULT_ARRAY_TESTS
        if count > MAX_ARRAY_TESTS:
            # Truncating would approve fewer elements than the plan runs.
            raise ValueError(f'执行清单有 {count} 项测试，超过作业数组上限 {MAX_ARRAY_TESTS}；请改用分配模式。')
        directives['array'] = f'0-{count - 1}%{parallel}'
    script = '\n'.join('#SBATCH --' + k + '=' + str(v) for k, v in directives.items())
    check_resources(script, limits)


def confirm(box, server, job, phase, rpc, *, authorize=False, apply_only=False, expected_resources=None):
    from vasp_agent.permissions.resource_menu import ceiling
    data = rpc('resource_context', phase=phase)
    if data.get('already_registered'):
        return data
    key = (server, job, phase)
    if expected_resources is not None:
        from vasp_agent.permissions.resource_menu import ceiling
        validate_resources(expected_resources, phase, ceiling(expected_resources), data.get('execution_plan', {}).get('tests', DEFAULT_ARRAY_TESTS))
    target_matches = expected_resources is None or same_resources(data['resources'], expected_resources)
    pending = box.resource_confirmations.get(key)
    if isinstance(pending, dict):
        receipt = data.get('resource_change') or {}
        if (receipt.get('operation_id') == pending['operation_id'] and receipt.get('phase') == phase
                and receipt.get('signature') == data['signature'] and same_resources(data['resources'], pending['resources'])):
            if not target_matches:
                return {'status': 'resource_target_mismatch', 'requested_resources': expected_resources,
                        'resources': data['resources'], 'next': '旧操作回执与本次目标不同；未应用本次方案，先核查旧操作。'}
            validate_resources(data['resources'], phase, box.submission_limits or ceiling(data['resources']))
            box.resource_confirmations[key] = data['signature']
            return {'status': 'awaiting_resource_review', 'operator_confirmed': True,
                    'recovered_after_disconnect': True, 'resources': data['resources'],
                    'next': '只读回执确认资源已应用；重新核验阶段审阅后继续同一提交，不重复询问资源。'}
        return {'status': 'resource_update_uncertain', 'operator_confirmed': True,
                'next': '资源操作缺少匹配的完成回执，停止提交；检查配置完整性，不自动重放写操作。'}
    if target_matches and box.resource_confirmations.get(key) == data['signature'] and (not authorize or (box.allow_submit and getattr(box, 'submission_target', None) == key)):
        return None
    selections = getattr(box, 'confirmed_resource_selections', {})
    selected = selections.get(key)
    reuse = bool(selected and selected['signature'] == data['signature']
                 and (expected_resources is None or same_resources(selected['resources'], expected_resources)))
    if box.plan_reader is None and not reuse:
        raise ValueError('资源未获得当前任务的操作者确认，请在交互模式重试；不会提交。')
    output = getattr(box, 'plan_output', print)
    limits = box.submission_limits
    if not limits and not authorize and not apply_only:
        raise ValueError('请通过统一提交确认菜单选择资源并授权。')
    from vasp_agent.permissions.resource_menu import selection
    current = selection(data['resources'], phase)
    if authorize or apply_only:
        from vasp_agent.permissions.resource_menu import ceiling
        limits = ceiling(current)
    tests = data.get('execution_plan', {}).get('tests', DEFAULT_ARRAY_TESTS) if phase in (None, 'convergence') else 1
    if type(tests) is not int or tests < 1:
        raise ValueError('当前执行清单没有可用的独立测试数量，请重新核查任务；未修改资源。')
    if reuse:
        # The operator already chose these in a menu for this exact snapshot; repeating
        # the header, suggestion and table only restates that choice.
        resources = selected['resources'].copy()
        validate_resources(resources, phase, limits, tests)
        output(f'提交授权 · {server} / {job} / {phase or "完整流程"}：沿用已确认资源 {brief(resources)}')
        return finish(box, server, job, phase, rpc, data, key, resources, limits,
                      authorize=authorize, apply_only=apply_only, expected_resources=expected_resources, reuse=True)
    output(f'提交前资源确认 · {server} / {job} / {phase or "完整流程"}'
           + ('（本轮仅应用资源，不授权、不提交）' if apply_only else ''))
    # Defaults must themselves fit the operator's limits, even after a larger old run.
    nodes = max(1, min(current['nodes'], limits['nodes'], limits['cores']))
    cores = max(nodes, min(current['cores_per_test'], limits['cores']) // nodes * nodes)
    suggested = min(3, tests, max(1, limits['cores'] // cores))
    if current.get('submission_mode', 'allocation') == 'array':
        suggested = min(suggested, max(1, limits['nodes'] // nodes))
    from vasp_agent.remote.remote_worker import wall_seconds as seconds
    walltime = current['walltime'] if seconds(current['walltime']) <= seconds(limits['walltime']) else limits['walltime']
    defaults = {**current, 'parallel_tests': suggested, 'cores_per_test': cores,
                'nodes': nodes, 'partition': limits['partition'], 'walltime': walltime}
    from vasp_agent.ui.menu_text import bullets, points
    if phase == 'convergence':
        plan = data.get('execution_plan', {'tests': DEFAULT_ARRAY_TESTS, 'compute': ['encut', 'mesh'], 'reuse': []})
        output('本次计算：' + describe_plan(plan))
    proposal = getattr(box, 'resource_suggestions', {}).get(key)
    if proposal and proposal['signature'] == data['signature']:
        try:
            validate_resources(proposal['resources'], phase, limits, tests)
            defaults = proposal['resources'].copy()
            output('AI 建议：' + brief(defaults) + '\n' + bullets(points(proposal['rationale'])))
        except ValueError as exc:
            output('AI 建议不符合当前授权，改用程序建议：' + str(exc))
    else:
        output(f'程序建议：同时 {suggested} 组 × 每组 {defaults["cores_per_test"]} MPI（无 AI 建议；未核实节点容量）')
    if expected_resources is not None:
        defaults = expected_resources.copy()
        output('本次目标资源：' + brief(defaults) + '（旧默认值不得覆盖）')
    from vasp_agent.permissions.resource_menu import resource_menu
    resources = resource_menu(getattr(box, 'workspace', None), server, phase, defaults,
                              box.plan_reader, output, tests=tests,
                              limits=None if authorize or apply_only else limits, job=job, source='本次目标' if expected_resources is not None else 'AI/程序建议',
                              apply_only=apply_only, prefer_saved=expected_resources is None and not (proposal and proposal.get('automatic') and proposal['signature'] == data['signature']),
                              execution_tags=data.get('execution_tags'))
    if not hasattr(box, 'confirmed_resource_selections'):
        box.confirmed_resource_selections = {}
    box.confirmed_resource_selections[key] = {'signature': data['signature'], 'resources': resources.copy()}
    return finish(box, server, job, phase, rpc, data, key, resources, limits,
                  authorize=authorize, apply_only=apply_only, expected_resources=expected_resources, reuse=False)


def finish(box, server, job, phase, rpc, data, key, resources, limits, *, authorize, apply_only, expected_resources, reuse):
    """Grant (only from this operator interaction) and write the chosen resources."""
    if expected_resources is not None and not same_resources(resources, expected_resources):
        return {'status': 'resource_selection_differs', 'requested_resources': expected_resources,
                'selected_resources': resources, 'applied': False,
                'next': '操作者选择与本次目标不同，未写入；请按明确的新目标继续。'}
    if authorize and reuse and not (getattr(box, 'allow_submit', False) and getattr(box, 'submission_target', None) == key):
        output = getattr(box, 'plan_output', print)
        output('  1  授权本阶段一次提交\n  q  取消')
        if box.plan_reader is None or box.plan_reader('选择：').strip().lower() != '1':
            raise ValueError('未获得提交授权；资源选择仍保留。')
    if authorize:
        # Only this operator interaction may grant; saved preferences never do.
        box.submission_limits = dict(server=server, partition=resources['partition'],
            nodes=resources['nodes'] * (resources['parallel_tests'] if resources.get('submission_mode', 'allocation') == 'array' else 1),
            cores=resources['parallel_tests'] * resources['cores_per_test'],
            walltime=resources['walltime'])
        box.submission_target = key
        box.max_submissions, box.submissions, box.allow_submit = 1, 0, True
        box.revocation = None
        limits = box.submission_limits
        box.log({'permission': 'grant_target_once', 'server': server, 'job_name': job,
                 'phase': phase, 'limits': limits})
    operation_id = uuid4().hex
    box.resource_confirmations[key] = {'operation_id': operation_id, 'resources': resources.copy()}
    # With a fresh grant the caller may write and submit in one remote call; the
    # reply still carries configure_resources' own fields, read below as usual.
    write = getattr(box, 'configure_then_submit', None) if authorize else None
    arguments = dict(phase=phase, resources=resources, signature=data['signature'], limits=limits, operation_id=operation_id)
    result = write(**arguments) if write else rpc('configure_resources', **arguments)
    if expected_resources is not None:
        # configure_resources returns the context read after its write; re-read
        # only when it came from a separate call.
        actual = result if write else rpc('resource_context', phase=phase)
        if (not same_resources(result.get('resources'), expected_resources) or not same_resources(actual.get('resources'), expected_resources)
                or result.get('signature') != actual.get('signature') or actual.get('already_registered')):
            return {'status': 'resource_application_mismatch', 'requested_resources': expected_resources,
                    'returned_resources': result.get('resources'), 'actual_resources': actual.get('resources'),
                    'operation_id': operation_id, 'target_verified': False,
                    'next': '写入结果未核实符合目标；保留待核对操作，停止，不自动重写或提交。'}
    box.resource_confirmations[key] = result['signature']
    box.confirmed_resource_selections[key] = {'signature': result['signature'], 'resources': resources.copy()}
    box.log({'operator_submission_resources': {'server': server, 'job_name': job, 'phase': phase, **resources}})
    if result['changed'] or apply_only:
        return {'status': 'awaiting_resource_review', 'operator_confirmed': True, 'resources': resources,
                'next': '资源已应用，尚未提交。读取当前阶段审阅上下文并重新审阅，通过后再次调用提交工具；同一配置不重复询问。'}
    return None
