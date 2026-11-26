"""Layer 3 sectioned contract UI rendering, interactive TTY approval, and scan-plan settle linkage.

Zero upward imports into chat/ or control/; reuses ui/menu_text.py and planning/plan_menu.py.
"""
from __future__ import annotations

from datetime import datetime
import hashlib
import hmac
import json
import os
from pathlib import Path
import re
from typing import Any, Callable
from uuid import uuid4

from vasp_agent.common import agent_paths
from vasp_agent.permissions import contract_ledger, task_contract
from vasp_agent.permissions.contract_rules import REVIEW_MODES, ContractError
from vasp_agent.planning import parameter_plans, plan_menu
from vasp_agent.ui.menu_text import bullets, options, points, printable, table
from vasp_agent.workflow import dimensionality, parameter_specs

MAX_APPROVAL_ROUNDS = 24
_PLAN_ID_RE = re.compile(r'^[0-9a-f]{32}$')
_APPROVE_TOKENS = {'1', 'yes', 'y', 'approve', '确认'}
_CANCEL_TOKENS = {'q', 'n', 'no', 'cancel', '取消'}
_SCAN_LABELS = {'defaults': '工具默认', 'suggestion': 'AI 建议'}
_REVIEW_LABELS = {
    'maker_checker': 'maker_checker（独立只读 Checker 审阅放行，推荐）',
    'maker_direct': 'maker_direct（允许主控代理直接记录审阅，无独立 Checker）',
}


def _atomic_write_bytes(path: Path, payload: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.parent / f'.{path.name}.{uuid4().hex}.tmp'
    try:
        with temp.open('xb') as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temp, path)
    finally:
        temp.unlink(missing_ok=True)


def load_linked_plan(workspace: str | Path, draft: dict[str, Any]) -> tuple[Path, dict[str, Any], bytes]:
    """Load and hash-verify the scan plan referenced by `draft`."""
    root = Path(workspace).resolve(strict=True)
    workflow = draft['workflow']
    plan_id = workflow['plan_id']
    if not isinstance(plan_id, str) or not _PLAN_ID_RE.fullmatch(plan_id):
        raise ContractError('CONTRACT_SCHEMA_INVALID', f'无效的关联扫描方案 ID：{plan_id!r}。')
    plan_path = agent_paths.store_item(root, 'scan_plans', f'{plan_id}.json')
    contract_ledger.safe_existing_file(plan_path, f'关联扫描方案 {plan_id}', workspace=root)
    raw = plan_path.read_bytes()
    actual_sha = hashlib.sha256(raw).hexdigest()
    if not hmac.compare_digest(actual_sha, workflow['plan_sha256']):
        raise ContractError(
            'CONTRACT_TAMPERED',
            f'关联扫描方案 {plan_id} 在草案生成后被修改（哈希不匹配），拒绝签发合同。',
        )
    try:
        plan = json.loads(raw.decode('utf-8'))
    except (UnicodeDecodeError, ValueError) as exc:
        raise ContractError('CONTRACT_TAMPERED', f'关联扫描方案 {plan_id} JSON 已损坏。') from exc
    req = plan.get('request') if isinstance(plan, dict) else None
    if (
        not isinstance(req, dict)
        or req.get('server') != draft['scope']['server']
        or req.get('job_name') != draft['scope']['job_name']
    ):
        raise ContractError('CONTRACT_TAMPERED', '关联扫描方案与合同草案的服务器或任务名不匹配。')
    if plan.get('status') == 'prepared':
        raise ContractError('CONTRACT_PLAN_ALREADY_PREPARED', f'扫描方案 {plan_id} 已准备，不可重复签发绑定。')
    return plan_path, plan, raw


def _phase_table(envelope: dict[str, Any], phases: list[str]) -> str:
    rows = [['阶段', '模式', '并发', '每组核', '节点', '总核', '分区', '时限']]
    for phase in phases:
        res = envelope['phase_defaults'][phase]
        total = int(res['parallel_tests']) * int(res['cores_per_test'])
        rows.append([
            phase, res['submission_mode'], res['parallel_tests'], res['cores_per_test'],
            res['nodes'], total, res['partition'], res['walltime'],
        ])
    return table(rows)


def render_contract_summary(
    doc: dict[str, Any],
    *,
    ledger: dict[str, Any] | None = None,
    plan: dict[str, Any] | None = None,
    review_mode: str | None = None,
    scan_choice: str | None = None,
    stage_choices: dict[str, str] | None = None,
    env_selected: str | None = None,
) -> str:
    """Render a contract draft or active contract into aligned sections via ui/menu_text.py."""
    scope = doc['scope']
    physics = doc['physics_assertion']
    workflow = doc['workflow']
    bounds = doc['precision_bounds']
    envelope = doc['resource_envelope']
    budget = doc['budget']
    effective_mode = review_mode or doc['review_policy']['mode']

    is_active = bool(doc.get('contract_id'))
    title = (
        f"任务自治合同 · {doc['contract_id']}（状态：{ledger['status'] if ledger else 'active'}）"
        if is_active
        else f"任务自治合同草案 · {doc['draft_id']}（待操作者真实 TTY 签发）"
    )
    env_prof = scope.get('environment_profile')
    env_label = (
        f"{env_prof['name']}（env={env_prof['environment_sha256'][:10]}…, exe={env_prof['executable_sha256'][:10]}…）"
        if isinstance(env_prof, dict) else '默认 / 未绑定特定环境配置'
    )
    block1 = '\n'.join([
        '【区块 1 · 任务范围与物理模型】',
        table([
            ['服务器 / 任务名', f"{scope['server'] or 'local'} / {scope['job_name']}"],
            ['结构文件', f"{scope['structure']}（sha256={scope['structure_sha256'][:12]}…，维度={scope['dimension']}）"],
            ['允许派生目录', '是' if scope['allow_forks'] else '否（仅限合同允许自愈/重扫登记）'],
            ['环境配置', env_label],
            ['物理模型边界', '电中性（charge=0）、非磁性（nonmagnetic）PBE；不含 SOC / DFT+U / 杂化泛函'],
            ['研究目标', printable(physics['goal'])],
        ]),
    ])

    block2_lines = [
        '【区块 2 · 阶段链与扫描方案】',
        table([
            ['允许阶段链', ' -> '.join(workflow['allowed_phases'])],
            ['收敛档位', f"{workflow['scan_preset']}（禁止放宽阈值：allow_threshold_relaxation=False）"],
            ['关联扫描方案', f"{workflow['plan_id']}（sha256={workflow['plan_sha256'][:12]}…）"],
        ]),
    ]
    if plan is not None:
        block2_lines.append(plan_menu.comparison(plan))
        has_diff = plan.get('defaults') != plan.get('suggestion')
        active_scan = scan_choice or ('suggestion' if has_diff else 'defaults')
        block2_lines.append(
            f"  当前拟采纳扫描序列：{_SCAN_LABELS.get(active_scan, active_scan)}"
            + ('（可按 p 切换工具默认 / AI 建议）' if has_diff else '（与工具默认一致）')
        )
        reasons = points(printable(plan.get('rationale', '')))
        if reasons:
            block2_lines.append('  方案理由：\n' + bullets(reasons, shown=4))
        for stage_name, entry in (plan.get('stage_parameters') or {}).items():
            item = parameter_specs.spec(entry['spec']['id'], entry['spec']['version'])
            chosen_key = (stage_choices or {}).get(stage_name) or (
                'suggestion' if entry.get('proposed_fields') else 'defaults'
            )
            label = 'AI 建议' if chosen_key == 'suggestion' and entry.get('proposed_fields') else '规范建议'
            block2_lines.append(f"  {stage_name} 可选阶段参数（拟采纳：{label}）：\n" + plan_menu.stage_table(entry, item))
        if 'environment' in plan:
            env = plan['environment']
            chosen_env = env_selected if env_selected is not None else env.get('selected')
            cands = '、'.join(row['name'] for row in env.get('candidates', []))
            block2_lines.append(
                f"  运行环境：{chosen_env or '尚未选定（签发前请按 e 选择）'}（候选：{cands}）"
            )
    block2 = '\n'.join(block2_lines)

    block3 = '\n'.join([
        '【区块 3 · 精度上限与资源包络】',
        table([
            ['精度上限', f"ENCUT <= {bounds['max_encut']} eV，k 网格单轴 <= {bounds['max_kmesh_axis']}，总 k 点 <= {bounds['max_kmesh_points']}"],
            ['允许模式 / 分区', f"{', '.join(envelope['allowed_modes'])} / {', '.join(envelope['allowed_partitions'])}"],
            ['资源硬上限', f"并发 <= {envelope['max_parallel_tests']}，每组核 <= {envelope['max_cores_per_test']}，节点 <= {envelope['max_nodes']}，总核 <= {envelope['max_total_cores']}，时限 <= {envelope['max_walltime']}"],
        ]),
        '  各阶段默认资源：',
        _phase_table(envelope, workflow['allowed_phases']),
    ])

    block4 = '\n'.join([
        '【区块 4 · 预算、自愈上限与审阅模式】',
        table([
            ['总提交上限', f"{budget['max_submissions']} 次"],
            ['自愈重试上限', f"续算 {budget['max_resume_retries']} / 派生修复 {budget['max_repair_retries']} / 加密重扫 {budget['max_scan_retries']} / 连续失败熔断 {budget['max_consecutive_failures']}"],
            ['审阅模式', _REVIEW_LABELS.get(effective_mode, effective_mode)],
            ['绿灯自动推进', '开启' if doc['review_policy']['auto_advance_on_green'] else '关闭'],
            ['有效期至（UTC）', doc['expires_at']],
        ]),
    ])

    sections = [title, block1, block2, block3, block4]
    if ledger is not None:
        in_flight = ledger.get('in_flight')
        in_flight_text = (
            f"{in_flight.get('job_name')}/{in_flight.get('phase')} (op={in_flight.get('operation_id')})"
            if isinstance(in_flight, dict) else '无'
        )
        block5 = '\n'.join([
            '【区块 5 · 账本执行状态】',
            table([
                ['当前状态', ledger['status']],
                ['已用提交', f"{ledger['submissions_used']} / {budget['max_submissions']}"],
                ['已用重试', f"续算 {ledger['resume_retries_used']} / 修复 {ledger['repair_retries_used']} / 重扫 {ledger['scan_retries_used']} / 连续失败 {ledger['consecutive_failures']}"],
                ['允许任务目录', ', '.join(ledger.get('allowed_jobs', []))],
                ['在途提交', in_flight_text],
                ['熔断信息', f"{ledger.get('halt_code') or '-'} | {printable(ledger.get('halt_reason') or '-')}"],
            ]),
        ])
        sections.append(block5)
    return '\n\n'.join(sections)


def render_contracts_overview(status_payload: dict[str, Any], drafts: list[dict[str, Any]]) -> str:
    """Render an overview table of active/historical contracts and pending drafts."""
    sections = ['任务自治合同总览']
    contracts = status_payload.get('contracts') or []
    if contracts:
        rows = [['合同 ID', '状态', '目标任务', '审阅模式', '提交进度', '有效期 / 异常']]
        for item in contracts:
            if item.get('status') == 'invalid':
                rows.append([item['contract_id'], 'invalid', '-', '-', '-', f"{item.get('error_code')}: {item.get('error')}"])
                continue
            scope = item.get('scope') or {}
            budget = item.get('budget') or {}
            rows.append([
                item['contract_id'], item['status'],
                f"{scope.get('server') or 'local'}/{scope.get('job_name')}",
                (item.get('review_policy') or {}).get('mode', '-'),
                f"{item.get('submissions_used', 0)}/{budget.get('max_submissions', '-')}",
                item.get('halt_reason') or item.get('expires_at', '-'),
            ])
        sections.append('【已签合同】\n' + table(rows))
    else:
        sections.append('【已签合同】\n  （暂无已签发合同）')
    if drafts:
        rows = [['草案 ID', '目标任务', '阶段数', '审阅模式', '有效期至（UTC）']]
        for draft in drafts:
            scope = draft.get('scope') or {}
            wf = draft.get('workflow') or {}
            rows.append([
                draft.get('draft_id', '-'),
                f"{scope.get('server') or 'local'}/{scope.get('job_name')}",
                len(wf.get('allowed_phases') or []),
                (draft.get('review_policy') or {}).get('mode', '-'),
                draft.get('expires_at', '-'),
            ])
        sections.append('【待签草案】\n' + table(rows))
    else:
        sections.append('【待签草案】\n  （暂无待签发草案）')
    return '\n\n'.join(sections)


class InteractiveContractApprovalProvider(task_contract.LocalTTYApprovalProvider):
    """Real-TTY interactive menu allowing review-mode/scan/stage/env toggles before signing."""

    def __init__(
        self,
        draft: dict[str, Any],
        *,
        plan: dict[str, Any] | None = None,
        review_mode: str | None = None,
        reader: Callable[[str], str] | None = None,
        output: Callable[[str], Any] | None = None,
        stream: Any = None,
    ):
        super().__init__(reader=reader, output=output, stream=stream)
        self.draft = draft
        self.plan = plan
        self.selected_review_mode = review_mode or draft['review_policy']['mode']
        has_diff = bool(plan and plan.get('defaults') != plan.get('suggestion'))
        self.scan_choice = 'suggestion' if has_diff else 'defaults'
        self.stage_choices: dict[str, str] = {
            name: ('suggestion' if entry.get('proposed_fields') else 'defaults')
            for name, entry in ((plan or {}).get('stage_parameters') or {}).items()
        }
        env = (plan or {}).get('environment')
        self.env_selected: str | None = env.get('selected') if isinstance(env, dict) else None

    def _menu_options(self) -> list[tuple[str, str]]:
        items = [
            ('1', '确认上述全部边界并签发不可变合同（同步锁定扫描方案）'),
            ('m', '切换审阅模式（maker_checker ↔ maker_direct）'),
        ]
        if self.plan and self.plan.get('defaults') != self.plan.get('suggestion'):
            items.append(('p', '切换扫描序列（工具默认 ↔ AI 建议）'))
        if self.plan and any(e.get('proposed_fields') for e in (self.plan.get('stage_parameters') or {}).values()):
            items.append(('s', '切换可选阶段参数（规范建议 ↔ AI 建议）'))
        if self.plan and isinstance(self.plan.get('environment'), dict) and len(self.plan['environment'].get('candidates', [])) > 1:
            items.append(('e', '选择运行环境配置'))
        items.append(('q', '取消签发（不落盘合同、不改动扫描方案）'))
        return items

    def _pick_environment(self) -> None:
        env = (self.plan or {}).get('environment')
        if not isinstance(env, dict):
            return
        candidates = env.get('candidates') or []
        rows = [['编号', '环境配置', 'VASP', '能力', '环境脚本']]
        for idx, row in enumerate(candidates, 1):
            rows.append([idx, row['name'], row.get('version') or '-', '、'.join(row.get('capabilities') or []) or '-', row.get('env_script', '-')])
        self.output(table(rows))
        raw = self.reader('输入环境编号（回车/q 返回）：').strip()
        if raw.isascii() and raw.isdigit() and 1 <= int(raw) <= len(candidates):
            self.env_selected = candidates[int(raw) - 1]['name']

    def confirm(self, prompt_title: str, summary_text: str) -> bool:
        task_contract.require_real_tty(self.stream)
        needs_env = bool(self.plan and isinstance(self.plan.get('environment'), dict))
        has_stage_ai = bool(self.plan and any(e.get('proposed_fields') for e in (self.plan.get('stage_parameters') or {}).values()))
        dirty = True
        for _ in range(MAX_APPROVAL_ROUNDS):
            if dirty:
                rendered = render_contract_summary(
                    self.draft, plan=self.plan, review_mode=self.selected_review_mode,
                    scan_choice=self.scan_choice, stage_choices=self.stage_choices, env_selected=self.env_selected,
                )
                self.output(rendered + '\n\n' + options(self._menu_options()))
                dirty = False
            try:
                answer = self.reader(f'{prompt_title}（输入选项，1 确认 / q 取消）：').strip().lower()
            except (EOFError, StopIteration):
                return False
            if answer in _CANCEL_TOKENS:
                return False
            if answer == 'm':
                self.selected_review_mode = 'maker_direct' if self.selected_review_mode == 'maker_checker' else 'maker_checker'
                dirty = True
            elif answer == 'p' and self.plan and self.plan.get('defaults') != self.plan.get('suggestion'):
                self.scan_choice = 'defaults' if self.scan_choice == 'suggestion' else 'suggestion'
                dirty = True
            elif answer == 's' and has_stage_ai:
                for name, entry in ((self.plan or {}).get('stage_parameters') or {}).items():
                    if entry.get('proposed_fields'):
                        self.stage_choices[name] = 'defaults' if self.stage_choices[name] == 'suggestion' else 'suggestion'
                dirty = True
            elif answer == 'e' and needs_env:
                self._pick_environment()
                dirty = True
            elif answer in _APPROVE_TOKENS:
                if needs_env and self.env_selected is None:
                    self.output('运行环境尚未选定，请先输入 e 选择运行环境后再确认签发。')
                    continue
                return True
            else:
                self.output('无效选项，请输入 1 确认、m/p/s/e 调整选项，或 q 取消。')
        return False


def settle_plan_for_contract(
    workspace: str | Path, plan_id: str, *, scan_choice: str = 'suggestion',
    stage_choices: dict[str, str] | None = None, env_selected: str | None = None,
    expected_sha256: str | None = None, precision_bounds: dict[str, Any] | None = None,
) -> tuple[dict[str, Any], str, dict[str, Any] | None]:
    """Settle the linked scan plan after real-TTY confirmation and return (plan, sha256, env_profile)."""
    root = Path(workspace).resolve(strict=True)
    plan_path = agent_paths.store_item(root, 'scan_plans', f'{plan_id}.json')
    contract_ledger.safe_existing_file(plan_path, f'关联扫描方案 {plan_id}', workspace=root)
    raw = plan_path.read_bytes()
    if expected_sha256 is not None and not hmac.compare_digest(hashlib.sha256(raw).hexdigest(), expected_sha256):
        raise ContractError('CONTRACT_TAMPERED', f'签发确认期间关联扫描方案 {plan_id} 已被修改，拒绝落盘合同。')
    plan = json.loads(raw.decode('utf-8'))
    if plan.get('status') == 'prepared':
        raise ContractError('CONTRACT_PLAN_ALREADY_PREPARED', f'扫描方案 {plan_id} 已准备，不可重复签发绑定。')

    has_diff = plan['defaults'] != plan['suggestion']
    if not has_diff:
        selected, choice = plan['defaults'], 'no_difference'
        sources = dict(plan.get('proposed_sources') or {'encuts': 'tool_default', 'meshes': 'tool_default'})
    elif scan_choice == 'defaults':
        selected, choice = plan['defaults'], 'operator_default'
        sources = {'encuts': 'operator_default', 'meshes': 'operator_default'}
    else:
        selected, choice = plan['suggestion'], 'operator_accept_ai'
        sources = dict(plan.get('proposed_sources') or {'encuts': 'ai_suggestion', 'meshes': 'ai_suggestion'})
    if selected['encuts'][0] < plan['potential_floor']:
        raise ContractError('CONTRACT_SCHEMA_INVALID', '选定 ENCUT 起点低于赝势下限，无法签发。')
    if plan.get('dimension') == '2D':
        dimensionality.check_meshes(selected['meshes'])
    if precision_bounds is not None:
        max_e = max(selected['encuts'])
        max_ax = max(a for m in selected['meshes'] for a in m)
        max_pts = max(m[0] * m[1] * m[2] for m in selected['meshes'])
        if max_e > precision_bounds['max_encut'] or max_ax > precision_bounds['max_kmesh_axis'] or max_pts > precision_bounds['max_kmesh_points']:
            raise ContractError('CONTRACT_PRECISION_BOUND_EXCEEDED', '选定扫描序列超出合同 precision_bounds 上限，拒绝签发。')
    plan.update(selected=selected, selected_sources=sources, choice=choice)

    for stage_name, entry in (plan.get('stage_parameters') or {}).items():
        item = parameter_specs.spec(entry['spec']['id'], entry['spec']['version'])
        pick = (stage_choices or {}).get(stage_name) or ('suggestion' if entry.get('proposed_fields') else 'defaults')
        use_ai = pick == 'suggestion' and bool(entry.get('proposed_fields'))
        raw_values = entry['suggestion'] if use_ai else entry['defaults']
        entry.update(
            selected=parameter_specs.validate(item, raw_values, entry['context']),
            choice='operator_accept_ai' if use_ai else 'operator_spec_default',
        )

    env_profile: dict[str, Any] | None = None
    if 'environment' in plan:
        env = plan['environment']
        target_env = env_selected if env_selected is not None else env.get('selected')
        valid_names = [row['name'] for row in env.get('candidates') or []]
        if target_env not in valid_names:
            raise ContractError('CONTRACT_ENVIRONMENT_REQUIRED', '签发前必须在真实终端选择有效的运行环境配置。')
        if env.get('selected') != target_env:
            env.update(selected=target_env, source='operator_menu')
        server = plan['request'].get('server')
        if server is not None:
            from vasp_agent.remote import environment_profiles as profiles

            choice_env = profiles.choose(root, server, plan['request']['relax_mode'], env.get('required'))
            row = next((r for r in choice_env['matches'] if r['name'] == target_env), None)
            if row is None:
                profiles.by_name(root, server, target_env)
                raise ContractError('ENVIRONMENT_CAPABILITY_MISSING', f'环境配置 {target_env} 已不再满足任务能力或优化模式要求。')
            if not row.get('implicit'):
                env_profile = {
                    'name': row['name'],
                    'environment_sha256': row['environment_sha256'],
                    'executable_sha256': row['executable_sha256'],
                }

    parameter_plans.settle(plan)
    if plan.get('status') != 'selected':
        raise ContractError('CONTRACT_PLAN_NOT_SETTLED', '关联扫描方案未能完成全部项确认（status != selected）。')
    contract_ledger.atomic_write_json(plan_path, plan)
    return plan, hashlib.sha256(plan_path.read_bytes()).hexdigest(), env_profile


def approve_contract_interactive(
    workspace: str | Path, draft_id: str, *, reader: Callable[[str], str] | None = None,
    output: Callable[[str], Any] | None = None, stream: Any = None, ssh_key: str | Path | None = None,
    ssh_principal: str | None = None, review_mode: str | None = None, contract_id: str | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Run real-TTY interactive approval, settle linked scan plan, and roll back plan on failure."""
    root = Path(workspace).resolve(strict=True)
    if review_mode is not None and review_mode not in REVIEW_MODES:
        raise ContractError('CONTRACT_SCHEMA_INVALID', f'无效审阅模式：{review_mode!r}。')
    existing_cid = task_contract.find_contract_for_draft(root, draft_id)
    if existing_cid is not None:
        raise ContractError('CONTRACT_DRAFT_ALREADY_APPROVED', f'草案 {draft_id} 已签发为合同 {existing_cid}，禁止重复签发。')
    draft = task_contract.load_draft(root, draft_id)
    plan_path, plan, original_plan_bytes = load_linked_plan(root, draft)
    provider = InteractiveContractApprovalProvider(
        draft, plan=plan, review_mode=review_mode, reader=reader, output=output, stream=stream,
    )
    state: dict[str, Any] = {'settled': False, 'plan': plan}

    def _on_confirmed(contract: dict[str, Any]) -> None:
        contract['review_policy']['mode'] = provider.selected_review_mode
        settled_plan, settled_sha, env_profile = settle_plan_for_contract(
            root, plan['id'], scan_choice=provider.scan_choice, stage_choices=provider.stage_choices,
            env_selected=provider.env_selected, expected_sha256=draft['workflow']['plan_sha256'],
            precision_bounds=draft['precision_bounds'],
        )
        state['settled'], state['plan'] = True, settled_plan
        contract['workflow']['plan_sha256'] = settled_sha
        if 'environment' in settled_plan:
            contract['scope']['environment_profile'] = env_profile

    try:
        contract, ledger = task_contract.approve_draft(
            root, draft_id, provider=provider, now=now, ssh_key=ssh_key, ssh_principal=ssh_principal,
            review_mode=review_mode, contract_id=contract_id, on_confirmed=_on_confirmed,
        )
    except Exception:
        if state['settled']:
            _atomic_write_bytes(plan_path, original_plan_bytes)
        raise
    return {
        'contract': contract, 'ledger': ledger, 'plan_id': plan['id'],
        'plan_status': state['plan']['status'], 'plan_sha256': contract['workflow']['plan_sha256'],
    }
