"""Evidence-bound stage reviews and operator-confirmed physical scope."""
import json
import hashlib
import os
from datetime import datetime, timezone
from uuid import uuid4

import vasp_agent.workflow.vasp_band_agent as core
from vasp_agent.workflow import stage_definitions as stages

# The legacy chain: what every task without config['stages'] runs, unchanged.
# A task's own order comes from task_stages(); nothing below assumes this tuple
# or that bands is its last stage.
PHASES = stages.LEGACY_STAGES
CHECKS = {name: stages.DEFINITIONS[name][1]['review']['required_checks'] for name in PHASES}


def enabled(root):
    return json.loads((root / 'manifest.json').read_text(encoding='utf-8')).get('stage_gates', False)


def task_config(root):
    path = root / 'config.json'
    return json.loads(path.read_text(encoding='utf-8')) if path.is_file() else {}


def task_stages(root):
    """The task's enabled stages in run order (the legacy four when it records none)."""
    return stages.order(task_config(root))


def current(root):
    path = root / 'checkpoint.json'
    return json.loads(path.read_text(encoding='utf-8'))['next'] if path.exists() else task_stages(root)[0]


# Every operator surface that can confirm a physical model. The value is written
# by the local code path that ran the menu, never taken from model tool
# arguments, so a record cannot claim a channel that the operator did not use.
# Bumped when the confirmation signature changes, so a task bound to an older
# release is refused with an explanation instead of a TypeError.
CONFIRMATION_VERSION = 1
CONFIRMATION_CHANNELS = ('operator_slash_command', 'operator_chat_menu')
# The physical assumptions are written from the operator's confirmation, not
# derived from the structure. Recorded as a fixed string so a reader can tell
# an operator assertion from a property the program would have verified.
# Dimension is the exception: it is detected from initial.POSCAR and recorded
# with its own source, and physics() re-derives it on every read.
ASSUMPTIONS_SOURCE = 'operator_assertion_not_structure_derived'


def structure_dimension(root):
    from pymatgen.core import Structure
    from vasp_agent.workflow import dimensionality
    path = root / 'initial.POSCAR'
    if not path.is_file():
        raise core.ToolError('FILE_NOT_FOUND', 'initial.POSCAR 缺失，无法核对体系维度；不自动重建。')
    return dimensionality.require_supported(dimensionality.classify(Structure.from_file(path)))


def confirm_physics(root, goal, channel='operator_slash_command'):
    if not enabled(root):
        raise ValueError('旧任务不支持阶段放行，请准备新任务。')
    if not goal.strip() or len(goal) > 2000:
        raise ValueError('请填写研究目标。')
    if channel not in CONFIRMATION_CHANNELS:
        raise ValueError('确认来源无效。')
    if current(root) != 'convergence' or (root / 'submissions').exists() or (root / '.agent.lock').exists():
        raise ValueError('任务已进入提交流程，不允许更改物理模型，请新建任务。')
    # Magnetism, charge and functional remain the operator's assertion -- the
    # structure cannot prove them, and the menu says so. Dimension is detected
    # from the structure and bound to it by hash, so a slab can no longer be
    # recorded as 3D bulk.
    dimension = structure_dimension(root)
    data = {'goal': goal, 'dimension': dimension['dimension'], 'magnetism': 'nonmagnetic', 'charge': 0,
            'functional': 'PBE', 'soc': False, 'hubbard_u': False, 'hybrid': False,
            'confirmed_by': channel, 'assumptions_source': ASSUMPTIONS_SOURCE,
            'dimension_source': dimension['source'], 'vacuum_axes': dimension['vacuum_axes'],
            'largest_empty_gap_A': dimension['largest_empty_gap_A'],
            'structure_sha256': core.digest(root / 'initial.POSCAR'),
            'time': datetime.now(timezone.utc).isoformat()}
    core.save(root / 'physics-model.json', data)
    return data


def physics(root):
    path = root / 'physics-model.json'
    if not path.exists():
        raise core.ToolError('PHYSICS_CONFIRMATION_REQUIRED', '缺少物理模型确认，请操作者使用 /physics 服务器 任务名。')
    data = json.loads(path.read_text(encoding='utf-8'))
    expected = dict(magnetism='nonmagnetic', charge=0, functional='PBE',
                    soc=False, hubbard_u=False, hybrid=False)
    if any(data.get(k) != v for k, v in expected.items()) or not data.get('goal', '').strip():
        raise ValueError('物理模型未确认或超出当前支持范围。')
    # Records written before dimension detection all say '3D'; they stay valid
    # exactly when the structure really is bulk.
    derived = structure_dimension(root)['dimension']
    if data.get('dimension') != derived:
        raise ValueError(f"物理模型记录的维度 {data.get('dimension')} 与结构检测结果 {derived} 不一致；"
                         '请新建任务并重新确认物理模型。')
    if data.get('structure_sha256', core.digest(root / 'initial.POSCAR')) != core.digest(root / 'initial.POSCAR'):
        raise ValueError('物理模型确认后 initial.POSCAR 已改变。')
    # Both provenance fields default for records written before they existed:
    # those confirmations were also the operator's own assertion made on an
    # operator surface, so the absent value is read as what it must have been,
    # and existing tasks keep working. A field that is present and says
    # something else is a different claim, and is refused rather than
    # quietly reinterpreted.
    if data.get('assumptions_source', ASSUMPTIONS_SOURCE) != ASSUMPTIONS_SOURCE:
        raise ValueError('物理模型假设来源无效。')
    if data.get('confirmed_by', 'operator_slash_command') not in CONFIRMATION_CHANNELS:
        raise ValueError('物理模型确认来源无效。')
    return data


def authorized_phase(root):
    """The checkpoint decides which phase may run.

    VASP_AGENT_PHASE is set by whoever submitted the job and is not
    authenticated, so it may only restate checkpoint['next']; a mismatch is
    refused instead of overriding the checkpoint.
    """
    phase = current(root)
    claimed = os.environ.get('VASP_AGENT_PHASE')
    if claimed and claimed != phase:
        raise ValueError(f'提交声明的阶段 {claimed} 与任务检查点 {phase} 不一致，拒绝运行；请核对提交记录。')
    return phase


def phase_of(name):
    if name.startswith('convergence/'):
        return 'convergence'
    if name.startswith(('relax-', 'validation/')):
        return 'relaxation'
    return name


def check_stage(root, name, target):
    order = task_stages(root)
    if phase_of(name) not in order or target not in order:
        raise ValueError(f'无法识别阶段目录 {name} 或目标阶段 {target}；请检查任务目录。')
    if order.index(phase_of(name)) > order.index(target):
        raise ValueError('禁止越过当前获准阶段。')
    if order.index(phase_of(name)) < order.index(target) and not (root / name / 'complete.json').is_file():
        raise ValueError('已完成阶段缺少验收记录，禁止重算，请检查任务。')


# Engines with this marker verify a resumed phase's reused stages themselves
# (evidence 'resumed'), at every review and again when the resumed job starts.
RESUME_VERSION = 1


def resumed(root, phase):
    """Hashes of the stages a resume of this phase reuses, verified against its record.

    The worker writes the record into checkpoint.json when the operator confirms a
    resume (remote_worker.prepare_phase_resume); a stage that changed since is
    refused, never re-hashed into the new evidence.
    """
    path = root / 'checkpoint.json'
    record = json.loads(path.read_text(encoding='utf-8')).get('resume') if path.exists() else None
    if not record or record.get('phase') != phase:
        return None
    hashes = {}
    for name, files in record['reused'].items():
        for filename, expected in files.items():
            target = root / name / filename
            if not target.is_file() or core.digest(target) != expected:
                raise ValueError(f'续算复用的 {name}/{filename} 在续算确认后发生变化，请人工检查。')
            hashes[name + '/' + filename] = expected
    return hashes


def evidence(root, phase):
    """Validate completed-stage provenance before presenting or accepting a review.

    Upstream is every enabled stage before phase in the task's order. For
    SCF-dependent siblings run one after another (bands, then a later stage)
    that includes the earlier sibling, so it cannot change under the later one.
    """
    config = task_config(root)
    data = verified_upstream(root, config, stages.before(config, phase))
    reused = resumed(root, phase)
    if reused is not None:
        # Kept apart from 'hashes', which must equal what the previous phase recorded.
        data['resumed'] = reused
    return data


def evidence_through(root, last):
    """The evidence of every enabled stage up to and including last (fork sources)."""
    config = task_config(root)
    return verified_upstream(root, config, stages.through(config, last))


def verified_upstream(root, config, upstream):
    summaries = {}
    hashes = {}
    for name in stages.summary_files(config):
        path = root / name
        if path.exists():
            summaries[name] = json.loads(path.read_text(encoding='utf-8'))
            hashes[name] = core.digest(path)
    plan = json.loads((root / 'input-plan.json').read_text(encoding='utf-8'))
    for name in plan['incars']:
        if phase_of(name) not in upstream:
            continue
        folder = root / name
        stamp = json.loads((folder / 'complete.json').read_text(encoding='utf-8'))
        # A completion record that cannot be verified is a broken upstream, not a
        # hash mismatch. Say which record and which field, so the operator can
        # tell a damaged or hand-edited job from a genuinely changed output --
        # indexing these directly reported a bare KeyError naming only the field.
        if not stamp.get('xml_sha256'):
            raise core.ToolError('UNVERIFIED_METADATA',
                f'{name}/complete.json 缺少输出哈希，无法验证上游证据；请检查任务是否完整。')
        if core.digest(folder / 'vasprun.xml') != stamp['xml_sha256']:
            raise ValueError('上游输出哈希不匹配：' + name)
        inputs = json.loads((folder / 'inputs.json').read_text(encoding='utf-8'))
        for filename, expected in inputs.items():
            if not (folder / filename).is_file():
                raise core.ToolError('FILE_NOT_FOUND',
                    f'{name}/{filename} 缺失，无法验证上游输入；不自动重建。')
            if core.digest(folder / filename) != expected:
                raise ValueError('上游输入已改变：' + name + '/' + filename)
        for filename in ('complete.json', 'inputs.json', 'vasprun.xml', 'POSCAR', 'INCAR', 'KPOINTS'):
            hashes[name + '/' + filename] = core.digest(folder / filename)
        # The record of the parallelization tags the stage ran with (tagged tasks only).
        if (folder / 'execution.json').is_file():
            hashes[name + '/execution.json'] = core.digest(folder / 'execution.json')
        if name == 'scf':
            if not stamp.get('charge_sha256') or not (folder / 'CHGCAR').is_file():
                raise core.ToolError('FILE_NOT_FOUND',
                    'scf 缺少 CHGCAR 或其哈希，无法验证电荷文件；不自动重建。')
            if core.digest(folder / 'CHGCAR') != stamp['charge_sha256']:
                raise ValueError('SCF 电荷文件已改变。')
            hashes['scf/CHGCAR'] = stamp['charge_sha256']
    for name in stages.root_evidence_files(config):
        if (root / name).exists():
            hashes[name] = core.digest(root / name)
    return {'summaries': summaries, 'hashes': hashes}


def context(root):
    if not enabled(root):
        raise ValueError('旧任务请使用原输入审阅工具。')
    phase = current(root)
    config = task_config(root)
    if phase not in stages.order(config):
        raise ValueError('流程已完成，没有待放行阶段。')
    from vasp_agent.workflow.input_review import context as input_context
    data = {'phase': phase, 'required_checks': stages.definition(config, phase)['review']['required_checks'],
            'physics': physics(root),
            'inputs': input_context(root), 'evidence': evidence(root, phase)}
    if data['inputs']['config'].get('execution_tags') is not None:
        # Only tasks prepared with execution tags; older snapshots stay as reviewed.
        from vasp_agent.workflow import full_workflow
        data['execution_tags'] = full_workflow.execution_preview(data['inputs']['config'], phase)
    if phase == 'bands':
        # Requested and resolved k-path, from the verified relaxed and SCF cells:
        # what the bands stage will run (run_full compares again before launch).
        from vasp_agent.workflow.band_path import review_context
        data['band_path'] = review_context(root, data['inputs']['config'])
    chosen = data['inputs']['plan'].get('stage_parameters', {}).get(phase)
    if chosen is not None:
        # The operator-chosen values of this stage and what the plan derived from
        # them; inside the snapshot (only tasks with a parameterized stage have it).
        data['stage_parameters'] = chosen
    path = root / 'checkpoint.json'
    data['checkpoint'] = json.loads(path.read_text()) if path.exists() else None
    if data['checkpoint'] and data['checkpoint'].get('evidence_hashes') != data['evidence']['hashes']:
        # Typically a job of this phase ran and was cut off, leaving the phase's
        # own summaries behind; only an operator-confirmed resume archives them.
        raise ValueError('阶段完成后证据文件发生变化，请人工检查；若本阶段作业因超时、取消、节点故障或抢占中断，'
                         '可请求阶段续算（复用已验收阶段）。')
    data['snapshot'] = hashlib.sha256(json.dumps(data, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
    # What each required check means and which judgments it accepts. Outside the
    # hash on purpose: it follows from the stage definition version, which the
    # snapshot already binds (config['stages'], or the legacy default), and old
    # tasks keep the snapshot their reviews were recorded against.
    data.update(stages.review_rules(config, phase))
    return data


def record(root, snapshot, phase, checks, reviewer):
    if (root / '.agent.lock').exists():
        raise ValueError('计算仍在运行，不允许更新审阅。')
    data = context(root)
    if phase != data['phase'] or snapshot != data['snapshot']:
        raise ValueError('阶段或证据快照已改变，请重新读取。')
    required = data['required_checks']
    if len(checks) != len(required) or {x['item'] for x in checks} != set(required):
        raise ValueError('检查表缺项或重复。')
    for check in checks:
        if check['judgment'] not in ('pass', 'pending', 'blocked') or type(check['blocking']) is not bool:
            raise ValueError('检查结论无效。')
        if any(not isinstance(check.get(k), str) or not check[k].strip() or len(check[k]) > 10000
               for k in ('evidence', 'reason', 'next_action')):
            raise ValueError('每项必须填写证据、理由和后续动作（无动作请填写无需）。')
    # Which judgments let an item through comes from the stage definition (only
    # convergence's validation_plan accepts pending); the names stay the gate.
    config = task_config(root)
    allowed = all(not x['blocking'] and stages.check_allowed(config, phase, x['item'], x['judgment']) for x in checks)
    result = dict(phase=phase, snapshot=snapshot, checks=checks, allowed=allowed,
                  reviewer=reviewer, time=datetime.now(timezone.utc).isoformat())
    existing = root / f'review-{phase}.json'
    if existing.exists():
        previous = json.loads(existing.read_text(encoding='utf-8'))
        if all(previous.get(k) == result[k] for k in ('phase', 'snapshot', 'checks', 'allowed', 'reviewer')):
            return {**previous, 'already_recorded': True}
    history = root / 'reviews'
    history.mkdir(exist_ok=True)
    core.save(history / (uuid4().hex + '.json'), result)
    core.save(root / f'review-{phase}.json', result)
    (root / f'REVIEW-{phase}.md').write_text('# ' + phase + ' 阶段审阅\n\n' +
        ('允许本阶段' if allowed else '阻断') + '\n\n' + '\n\n'.join(
        f"## {x['item']} — {x['judgment']}\n证据：{x['evidence']}\n理由：{x['reason']}\n后续：{x['next_action']}" for x in checks), encoding='utf-8')
    return result


def require_review(root, phase=None):
    phase = phase or current(root)
    if phase != current(root) or phase not in task_stages(root):
        raise ValueError('请求阶段不是当前待执行阶段。')
    path = root / f'review-{phase}.json'
    if not path.exists():
        raise ValueError('本阶段未审阅，请先 get_stage_review 和 record_stage_review。')
    review = json.loads(path.read_text(encoding='utf-8'))
    if not review['allowed'] or review['snapshot'] != context(root)['snapshot']:
        raise ValueError('阶段审阅阻断或证据已变化。')
    return phase


def complete(root, phase):
    """Record phase accepted; next is the task's next enabled stage, or 'complete' after its last."""
    config = task_config(root)
    next_phase = stages.following(config, phase)
    checkpoint = {'completed': list(stages.through(config, phase)), 'next': next_phase}
    if next_phase != 'complete':
        checkpoint['evidence_hashes'] = evidence(root, next_phase)['hashes']
    core.save(root / 'checkpoint.json', checkpoint)
    if next_phase != 'complete':
        core.save(root / 'workflow-state.json', {'status': 'awaiting_review', **checkpoint})
    core.event(root, 'phase_completed', phase=phase, next=next_phase)


def submission_record(root, phase):
    if phase not in task_stages(root):
        raise ValueError('阶段式任务必须明确指定 phase，使用 submit_stage。')
    return root / 'submissions' / (phase + '.json')
