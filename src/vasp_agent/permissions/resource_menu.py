"""One operator menu for resource defaults and task-bound submission consent."""
import json
import os
import re
import tempfile

FIELDS = ('parallel_tests', 'cores_per_test', 'nodes', 'partition', 'walltime')
LABELS = ('同时运行的测试数', '每组 MPI 进程数', '申请的物理节点数（不是总核数）', '分区', '运行时限 HH:MM:SS')
ALIASES = dict(zip(('并发', '每组', '节点', '分区', '时限'), FIELDS)) | {k: k for k in FIELDS} | {'模式': 'submission_mode', 'submission_mode': 'submission_mode'}


def validate_server_mode(server, resources):
    if server == 'local' and resources.get('submission_mode') == 'array':
        raise ValueError('本机暂不支持 array；数组模式请使用已保存的远程服务器配置。')


def selection(resources, phase):
    """Retain the five-field form for old callers and normalize phase defaults."""
    result = {k: resources[k] for k in FIELDS}
    if 'submission_mode' in resources:
        result['submission_mode'] = resources['submission_mode']
        if phase != 'convergence' and result['submission_mode'] == 'array':
            result['submission_mode'] = 'allocation'
    return result


def ceiling(resources):
    # Structural bounds, not a grant and not evidence of cluster capacity.
    return dict(nodes=256, cores=65536, partition=resources['partition'], walltime='999:59:59')


def preference_path(workspace, server, phase):
    if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_-]{0,63}', server):
        raise ValueError('服务器名称无效。')
    from vasp_agent.common import agent_paths
    bucket = 'scan' if phase in (None, 'convergence') else 'sequential'
    path = agent_paths.store_item(workspace, 'submission_defaults', f'{server}-{bucket}.json')
    folder = path.parent
    for candidate in (folder, path):
        if candidate.is_symlink() or (candidate.exists() and getattr(candidate.stat(), 'st_file_attributes', 0) & 0x400):
            raise ValueError('默认资源路径不允许链接或重解析点。')
    if path.exists() and path.stat().st_nlink > 1:
        raise ValueError('默认资源文件不允许硬链接。')
    return path


def load_defaults(workspace, server, phase):
    from vasp_agent.permissions.submission_dialog import validate_resources
    path = preference_path(workspace, server, phase)
    if not path.exists():
        return None
    if path.stat().st_size > 4096:
        raise ValueError('默认资源文件过大。')
    data = json.loads(path.read_text(encoding='utf-8'))
    if not isinstance(data, dict) or set(data) not in (set(FIELDS), set(FIELDS) | {'submission_mode'}):
        raise ValueError('默认资源字段无效；不读取其中的授权状态。')
    validate_resources(data, phase, ceiling(data))
    validate_server_mode(server, data)
    return data


def save_defaults(workspace, server, phase, resources):
    from vasp_agent.permissions.submission_dialog import validate_resources
    data = {k: resources[k] for k in FIELDS}
    if 'submission_mode' in resources:
        data['submission_mode'] = resources['submission_mode']
    validate_resources(data, phase, ceiling(data))
    validate_server_mode(server, data)
    path = preference_path(workspace, server, phase)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix='.resources-', dir=path.parent)
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as stream:
            json.dump(data, stream, ensure_ascii=False, indent=2)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def parse_custom(text, resources):
    import unicodedata
    # A Chinese IME in full-width mode types ＝, ：, ，, full-width digits and
    # U+3000 spaces; NFKC folds them to ASCII, and commas separate like spaces.
    text = unicodedata.normalize('NFKC', text).replace(',', ' ').replace('、', ' ')
    tokens = text.split()
    if not tokens:
        raise ValueError('请填写参数，例如：模式=array 并发=6 每组=16 节点=1。')
    candidate, seen = resources.copy(), set()
    for token in tokens:
        key, separator, value = token.partition('=')
        field = ALIASES.get(key)
        if not separator or not field or not value or field in seen:
            raise ValueError('只接受不重复的 参数=值；可用：模式、并发、每组、节点、分区、时限。')
        seen.add(field)
        if field in FIELDS[:3]:
            if not re.fullmatch(r'[0-9]+', value):
                raise ValueError('组数、进程数和节点数必须为正整数。')
            value = int(value)
        candidate[field] = value
    return candidate


def edit_fields(resources, phase, tests, limits, reader, output):
    from vasp_agent.permissions.submission_dialog import validate_field
    result = resources.copy()
    for field, label in zip(FIELDS, LABELS):
        if field == 'parallel_tests' and phase not in (None, 'convergence'):
            result[field] = 1
            continue
        if field == 'nodes':
            total = result['parallel_tests'] * result['cores_per_test']
            array = result.get('submission_mode', 'allocation') == 'array'
            if array:
                label = '每个数组元素申请的物理节点数（不是 CPU 核数）'
                output(f'CPU 并发峰值自动计算：{result["parallel_tests"]} 组 × {result["cores_per_test"]} 进程 = {total} 核；节点峰值为并发数 × 每项节点数。')
            else:
                output(f'总核数自动计算：{result["parallel_tests"]} 组 × {result["cores_per_test"]} 进程 = {total} 核；不是节点数。')
            authorized_nodes = (limits or ceiling(result))['nodes'] // (result['parallel_tests'] if array else 1)
            maximum = min(256, result['cores_per_test'], authorized_nodes)
            output('满足均分和节点授权的节点数：' + ', '.join(str(n) for n in range(1, maximum + 1) if result['cores_per_test'] % n == 0))
        while True:
            answer = reader(f'{label} [{result[field]}]（q 取消）：').strip()
            if answer.lower() == 'q':
                raise ValueError('已取消，未修改资源或授权。')
            try:
                value = answer or str(result[field])
                if field in FIELDS[:3]:
                    try:
                        value = int(value)
                    except ValueError:
                        raise ValueError('请输入整数，或 q 取消。') from None
                candidate = {**result, field: value}
                validate_field(field, candidate, phase, limits or ceiling(candidate), tests)
            except ValueError as exc:
                output(str(exc) + ' 只需重新填写此项；前面的有效输入已保留。')
                continue
            result = candidate
            break
    return result


def execution_row(resources, execution_tags):
    """Menu row for the KPAR/NCORE the task's engine derives from these resources.

    execution_tags is the task's status from resource_context (None when the
    server did not report one). The values follow the edited MPI layout, so they
    are recomputed here with the same function the engine uses at launch; the
    k-point bound of each stage can only lower KPAR.
    """
    if not execution_tags:
        return None
    if not execution_tags.get('enabled'):
        return ['并行标签', '不写入（旧任务）', 'KPAR/NCORE 为 VASP 默认 1；按原审阅方案运行']
    from vasp_agent.workflow.full_workflow import execution_tags as derive
    try:
        tags = derive(resources['cores_per_test'], max(1, resources['nodes']))
    except ValueError:
        return ['并行标签', '无法推导', '每组 MPI 须为节点数的整数倍']
    return ['并行标签', f'KPAR ≤ {tags["KPAR"]}，NCORE {tags["NCORE"]}',
            '按每组 MPI 与各阶段 k 点数在启动时推导；只影响速度，不改物理参数']


def resource_menu(workspace, server, phase, current, reader, output, *, tests=6, limits=None, job=None, source='当前任务/服务器配置', apply_only=False, prefer_saved=True, execution_tags=None):
    from vasp_agent.permissions.submission_dialog import validate_resources
    resources = selection(current, phase)
    if workspace is not None and prefer_saved:
        try:
            saved = load_defaults(workspace, server, phase)
        except (ValueError, OSError, TypeError, KeyError):
            saved = None
            output('已保存默认资源无效或不可读，改为展示当前配置；不会自动授权。')
        if saved:
            resources, source = saved, '用户已保存的默认资源'
            output('下表是你保存的默认资源，不是上面的建议；可直接确认，或修改后再确认。')
    maximum = min(6, tests) if phase in (None, 'convergence') else 1
    if resources['parallel_tests'] > maximum:
        output(f'本阶段最多 {maximum} 组，本次展示的并发数从 {resources["parallel_tests"]} 调整为 {maximum}；仍需确认，原默认文件不变。')
        resources = {**resources, 'parallel_tests': maximum}
    while True:
        from vasp_agent.ui.menu_text import options, table
        mode = resources.get('submission_mode', 'allocation')
        r, nodes = resources, max(1, resources['nodes'])
        total = r['parallel_tests'] * r['cores_per_test']
        if phase == 'convergence' and server != 'local':
            mode_note = '输入 array / allocation 可切换'
        else:
            mode_note = ('本机只用 allocation' if server == 'local' else
                         '数组仅用于 convergence' if phase is not None else '完整流程只用 allocation')
        rows = [['提交模式', mode, mode_note]]
        if mode == 'array':
            rows += [['每个数组元素', f'{r["cores_per_test"]} MPI × {r["nodes"]} 节点', f'每节点 {r["cores_per_test"] // nodes} MPI'],
                     ['最多同时运行', f'{r["parallel_tests"]} 个', f'上限 {maximum}；共 {tests} 项测试分别排队'],
                     ['并发峰值', f'{total} 核 / {r["parallel_tests"] * r["nodes"]} 节点', ''],
                     ['分区', r['partition'], ''],
                     ['每项时限', r['walltime'], '']]
            note = '每项 MPI 跨本项节点均分，OMP_NUM_THREADS=1；可能部分先开始运行，用 squeue -r 查看各元素；节点容量未核实。'
        else:
            rows += [['并发', f'{r["parallel_tests"]} 组 × 每组 {r["cores_per_test"]} MPI', f'上限 {maximum} 组'],
                     ['节点', r['nodes'], f'每节点 {total // nodes} MPI，每组每节点 {r["cores_per_test"] // nodes}'],
                     ['分区', r['partition'], ''],
                     ['时限', r['walltime'], ''],
                     ['CPU 总数', f'{total} 核', '']]
            note = '每组 MPI 跨全部节点均分，OMP_NUM_THREADS=1；节点容量未核实。'
        tag_row = execution_row(r, execution_tags)
        if tag_row:
            rows.append(tag_row)
        # With a job, the caller has already named server/job/phase; only the source changes per round.
        header = f'资源（{source}）' if job else f'默认资源 · {server} / {phase or "完整流程"}（{source}）'
        output(header + '\n' + table(rows) + '\n' + note)
        try:
            validate_server_mode(server, resources)
            validate_resources(resources, phase, limits or ceiling(resources), tests)
            valid = True
        except ValueError as exc:
            output('当前方案不可确认：' + str(exc))
            valid = False
        custom = '直接填写参数（也可在下方输入行填写）'
        if job:
            if apply_only:
                choices = [('1', '仅应用以上资源（不授权、不提交）'), ('2', '保存为默认并应用（不提交）')]
            else:
                choices = [('1', '确认并授权本阶段提交'), ('2', '保存为默认，并授权本阶段提交')]
            output('\n' + options(choices + [('3', '逐项修改'), ('4', '取消'), ('5', custom)]))
            output('仅应用配置；随后重新审阅，提交须另行授权。' if apply_only else
                   '只授权上方任务的这一阶段；通过审阅和提交预检后才会启动，不授权后续阶段。')
        else:
            output('\n' + options([('1', '确认保存为默认（不授权、不提交）'), ('2', '逐项修改'), ('3', '取消'), ('4', custom)]))
        answer = reader('输入编号，或直接填参数如 并发=6 每组=16 节点=2 时限=04:00:00（q 取消）：').strip()
        if answer in ('array', 'allocation'):
            from vasp_agent.permissions.submission_dialog import validate_mode
            try:
                validate_server_mode(server, {**resources, 'submission_mode': answer})
                validate_mode({**resources, 'submission_mode': answer}, phase)
            except ValueError as exc:
                output(str(exc) + ' 模式未切换。')
            else:
                resources, source = {**resources, 'submission_mode': answer}, '操作者切换模式，待确认'
            continue
        if not job:
            answer = {'2': '3', '3': '4', '4': '5'}.get(answer, answer)
        if answer.lower() in ('', 'q', '4'):
            raise ValueError('已取消，未修改资源或授权。')
        if answer in ('1', '2'):
            if not valid:
                continue
            if (answer == '2' or not job) and workspace is not None:
                save_defaults(workspace, server, phase, resources)
                output('默认资源已保存；保存内容不包含提交授权。')
            return resources
        if answer == '3':
            resources = edit_fields(resources, phase, tests, limits, reader, output)
            source = '操作者修改，待确认'
            continue
        if answer == '5':
            answer = reader('自定义参数（模式=array 并发=6 每组=16 节点=1 分区=compute 时限=04:00:00；q 取消）：').strip()
            if answer.lower() in ('', 'q'):
                raise ValueError('已取消，未修改资源或授权。')
        try:
            candidate = parse_custom(answer, resources)
            validate_server_mode(server, candidate)
            validate_resources(candidate, phase, limits or ceiling(candidate), tests)
        except ValueError as exc:
            output(str(exc) + ' 参数未应用。')
            continue
        resources, source = candidate, '操作者自定义，待确认'
        output('已识别：' + ' '.join(f'{name}={candidate[field]}' for name, field in
                                    zip(('并发', '每组', '节点', '分区', '时限'), FIELDS))
               + '。确认无误请输入 1。')
