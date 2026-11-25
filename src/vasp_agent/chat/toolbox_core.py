"""Core Toolbox infrastructure: init, permissions, audit/evidence store, dispatch, redaction."""
from __future__ import annotations

import json
import re
import subprocess
from datetime import datetime, timezone
from pathlib import Path

import vasp_agent.workflow.vasp_band_agent as core
from vasp_agent.chat import tool_groups
from vasp_agent.chat.tool_schemas import (
    JOB,
    LEGACY_ALIASES,
    MODEL_TOOLS,
    PLAN_BYPASS_TOOLS,
    SUBMIT_TOOLS,
    TOOLS,
    null_resource_mode,
)


def item_field(item, key):
    """Read a response output item whether the SDK modelled it or returned a dict."""
    return item.get(key) if isinstance(item, dict) else getattr(item, key, None)


def output_items(response):
    """Normalise response.output; proxies may return null or non-list values."""
    output = getattr(response, 'output', None)
    return [item for item in output if isinstance(item_field(item, 'type'), str)] if isinstance(output, list) else []


def scrub(text):
    """Best-effort redaction for logs; credentials are never intentionally read."""
    text = re.sub(r'(?is)-----BEGIN [^-]*PRIVATE KEY-----.*?-----END [^-]*PRIVATE KEY-----',
                  '[REDACTED PRIVATE KEY]', text)
    text = re.sub(r'sk-[A-Za-z0-9_-]{12,}', '[REDACTED KEY]', text)
    return re.sub(r'(?im)^.*(?:api[_-]?key|password|passwd|secret|access[_-]?token)\s*[:=].*$',
                  '[REDACTED SECRET LINE]', text)


_SECRET_NAME = re.compile(r'(?i)(?:api[_-]?key|password|passwd|secret|access[_-]?token)')
AUDIT_INLINE_LIMIT = 6000
AUDIT_PREVIEW_CHARS = 600


def scrub_payload(value, depth=0):
    """Redact leaf strings, so one secret cannot erase an entire audit record.

    The line-anchored secret pattern matches a whole line. Scrubbing the
    serialized record applies it to the single JSON line that is the record,
    replacing every field with a redaction marker; scrubbing the leaves first
    keeps the record while redacting the offending value. Keys whose name
    marks them as secret are dropped wholesale rather than pattern-matched.
    """
    if depth > 12:
        return '[REDACTED DEPTH LIMIT]'
    if isinstance(value, dict):
        return {key: ('[REDACTED SENSITIVE CONTENT]'
                      if isinstance(key, str) and _SECRET_NAME.search(key)
                      else scrub_payload(item, depth + 1))
                for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [scrub_payload(item, depth + 1) for item in value]
    if isinstance(value, str):
        return scrub(value)
    if value is None or isinstance(value, (bool, int, float)):
        return value
    return scrub(str(value))


def read_tail(path, limit=12000):
    with path.open('rb') as stream:
        stream.seek(0, 2)
        length = stream.tell()
        stream.seek(max(0, length - limit * 4))
        return scrub(stream.read().decode('utf-8', errors='replace')[-limit:])


class ToolboxCoreMixin:
    def __init__(self, workspace, potcar=None, submit_script=None, agent_dir=None,
                 allow_submit=False, max_submissions=1, potcar_library=None):
        self.workspace = Path(workspace).resolve(strict=True)
        self.potcar = Path(potcar).resolve(strict=True) if potcar else None
        default_library = self.workspace / 'potentials/PBE'
        self.potcar_library = Path(potcar_library).resolve(strict=True) if potcar_library else (
            default_library if not self.potcar and any(default_library.glob('*/POTCAR')) else None)
        if self.potcar and self.potcar_library:
            raise ValueError('--potcar 与 --potcar-library 不能同时使用。')
        self.submit_script = Path(submit_script).resolve(strict=True) if submit_script else None
        self.script_hash = core.digest(self.submit_script) if self.submit_script else None
        source_project = Path(__file__).resolve().parents[3]
        default_engine = source_project if (source_project / 'vasp_band_agent.py').is_file() else Path(core.__file__).parent
        self.agent_dir = Path(agent_dir or default_engine).resolve()
        self.allow_submit = allow_submit
        self.revocation = None
        self.max_submissions = max_submissions
        self.submissions = 0
        self.submission_limits = None
        self.submission_target = None
        self.review_snapshots = {}
        self.review_model = 'unidentified'
        self.require_scan_plans = False
        self.unified_plan_confirm = False
        self.pending_plan_preauth = {}
        self.lazy_tool_groups = False
        self.tool_group_history = None
        self._enabled_tool_groups = []
        self.plan_reader = None
        self.accepted_releases = {}
        self.resource_confirmations = {}
        self.confirmed_resource_selections = {}
        self.resource_suggestions = {}
        self.latest_user_message = ''
        from vasp_agent.common import agent_paths
        self.audit = agent_paths.audit_file(self.workspace)
        self._monitor = None
        self.monitor_suggestions = {}

    @property
    def monitor(self):
        if self._monitor is None:
            from vasp_agent.monitoring.job_monitor import JobMonitor
            self._monitor = JobMonitor(self.workspace)
        return self._monitor

    def get_job_monitor(self):
        return self.monitor.status()

    def recommend_job_check_interval(self, server, job_name, phase, minutes, reason):
        from vasp_agent.monitoring.job_monitor import JobMonitor
        JobMonitor._identity(server or 'local', job_name, phase, '0')
        if type(minutes) is not int or not 1 <= minutes <= 60 or not reason.strip():
            raise ValueError('检查间隔建议需要 1–60 整数分钟及理由。')
        suggestion = {'interval_seconds': minutes * 60, 'reason': scrub(reason)[:500]}
        self.monitor_suggestions[(server or 'local', job_name, phase)] = suggestion
        return {'saved': True, 'minutes': minutes, 'reason': suggestion['reason'],
                'submitted': False, 'next': '真实提交成功后采用建议；手动间隔优先，不开启已暂停的监控。'}

    def track_submission(self, server, job_name, phase, result):
        """Monitoring errors must never turn a successful sbatch into a retryable failure."""
        if (not isinstance(result, dict) or result.get('status') != 'submitted'
                or not re.fullmatch(r'[0-9]+', str(result.get('job_id', '')))
                or result.get('submission_confirmed') is False):
            return result
        try:
            phase = result.get('phase', phase)
            key = (server or 'local', job_name, phase)
            resources = self.confirmed_resource_selections.get(key, {}).get('resources')
            if resources is None and server is None:
                config = self.job(job_name) / 'config.json'
                resources = json.loads(config.read_text(encoding='utf-8')) if config.is_file() else {}
            recommendation = self.monitor_suggestions.get(key, {})
            task = self.monitor.register(server or 'local', job_name, phase, result['job_id'],
                resources=resources, conversation=getattr(self, 'session_id', None), **recommendation)
            settings = self.monitor.status()
            monitoring = {'registered': True, 'enabled': settings['enabled'],
                          'interval_seconds': task['interval_seconds'], 'active': task['active'],
                          'running': settings['running'], 'next_due_at': task['next_due_at'],
                          'note': 'Agent运行时周期只读检查；退出暂停，重启恢复，不自动提交下一阶段。'}
        except Exception:
            monitoring = {'registered': False, 'warning': '提交回执已确认，但监控登记失败；请用 /watch 检查本地记录。不要重复提交。'}
        return {**result, 'monitoring': monitoring}

    def permission_status(self):
        return {'read': True, 'prepare': True, 'submission_enabled': self.allow_submit,
                'draft_edit_root': 'workbench/', 'server_identity_update': True, 'general_execution': False,
                'submissions_remaining': max(0, self.max_submissions - self.submissions) if self.allow_submit else 0,
                'limits': self.submission_limits, 'target': self.submission_target,
                'scope': '仅当前运行的当前对话；有target时仅限该任务阶段；取消作业需另行确认'}

    def revoke_submission(self, reason='operator_off'):
        """Withdraw every submission grant. Returns True when a grant was active."""
        active = bool(self.allow_submit)
        if active:
            self.revocation = {'reason': reason, 'time': datetime.now(timezone.utc).isoformat()}
        self.resource_confirmations.clear()
        self.confirmed_resource_selections.clear()
        self.resource_suggestions.clear()
        self.allow_submit = False
        self.submissions = 0
        self.submission_limits = None
        self.submission_target = None
        self.log({'permission': 'off', 'reason': reason, 'grant_was_active': active})
        return active

    REVOCATION_REASONS = {'operator_off': '操作者执行 /submit off', 'session_switch': '切换了对话',
                          'model_switch': '切换了模型或接口配置', 'quota_used': '本次授权的提交次数已用完',
                          'submission_released': '操作者释放了已取消作业的提交记录',
                          'phase_resume': '操作者确认了中断阶段的续算'}

    def forget_submission_target(self, server, job_name, phase):
        """Drop every confirmation tied to a target whose receipt was released."""
        key = (server, job_name, phase)
        for store in (self.resource_confirmations, self.confirmed_resource_selections, self.resource_suggestions):
            store.pop(key, None)
        self.monitor_suggestions.pop(key, None)
        if self.submission_target == key:
            self.revoke_submission('submission_released')

    def disabled_submission_message(self):
        revocation = getattr(self, 'revocation', None)
        hint = '远程任务请用 request_submission 请操作者在菜单中重新确认，本机任务使用 /submit once。'
        if not revocation:
            return '提交未授权；' + hint
        when = revocation['time'][11:16]
        why = self.REVOCATION_REASONS.get(revocation['reason'], revocation['reason'])
        return f'之前的提交授权已在 {when}（UTC）因{why}而撤销，不能沿用；' + hint

    def consume_submission(self):
        self.submissions += 1
        if self.submission_limits is not None and self.submissions >= self.max_submissions:
            self.allow_submit = False
            self.revocation = {'reason': 'quota_used', 'time': datetime.now(timezone.utc).isoformat()}
        self.log({'permission': 'consume_attempt', **self.permission_status()})

    def path(self, relative):
        from vasp_agent.common.workspace_paths import workspace_path
        return workspace_path(self.workspace, relative)

    def job(self, name):
        if not isinstance(name, str) or not re.fullmatch(JOB['pattern'], name):
            raise ValueError('无效任务名称。')
        return self.path(f'runs/{name}')

    def active_tool_groups(self):
        """Currently expanded lazy tool groups (derived from live_history in Agent sessions)."""
        source = getattr(self, 'tool_group_history', None)
        if source is not None:
            return tool_groups.active(source())
        return tuple(self._enabled_tool_groups)

    def enable_tool_group(self, groups):
        result = tool_groups.enable(groups, self.active_tool_groups())
        if getattr(self, 'tool_group_history', None) is None:
            for group_id in result['newly_enabled']:
                if group_id not in self._enabled_tool_groups:
                    self._enabled_tool_groups.append(group_id)
        return result

    def schemas(self):
        active = set(self.active_tool_groups()) if self.lazy_tool_groups else None
        return [
            t for t in TOOLS
            if (self.allow_submit or t['name'] not in SUBMIT_TOOLS)
            and (not self.require_scan_plans or t['name'] not in PLAN_BYPASS_TOOLS)
            and (active is None or tool_groups.group_of(t['name']) in (None, *active))
        ]

    def model_schemas(self):
        """Advertised catalog; schemas() still gates and validates actual execution."""
        candidates = [
            t for t in MODEL_TOOLS
            if t['name'] not in LEGACY_ALIASES
            and not (self.require_scan_plans and t['name'] in PLAN_BYPASS_TOOLS)
        ]
        if not self.lazy_tool_groups:
            return candidates
        by_name = {t['name']: t for t in candidates}
        listed = [t for t in candidates if tool_groups.group_of(t['name']) is None]
        for group_id in self.active_tool_groups():
            for tool_name in tool_groups.GROUPS[group_id]['tools']:
                if tool_name in by_name:
                    listed.append(by_name[tool_name])
        return listed

    def read_context_evidence(self, sha256, offset):
        if not re.fullmatch(r'[a-f0-9]{64}', sha256) or type(offset) is not int or offset < 0:
            raise ValueError('证据哈希或分页偏移无效。')
        from vasp_agent.common import agent_paths
        target = agent_paths.store_item(self.workspace, 'evidence', sha256 + '.json')
        raw = target.read_text(encoding='utf-8')
        import hashlib
        if hashlib.sha256(raw.encode()).hexdigest() != sha256:
            raise ValueError('历史证据哈希不匹配。')
        return {'content': raw[offset:offset + 3500], 'offset': offset,
                'next_offset': offset + 3500 if offset + 3500 < len(raw) else None,
                'total_chars': len(raw), 'historical_only': True, 'content_is_untrusted': True}

    def store_evidence(self, payload):
        """Write one payload verbatim to the evidence store as <sha256>.json; return its address."""
        import hashlib
        raw = json.dumps(payload, ensure_ascii=False, default=str)
        digest = hashlib.sha256(raw.encode()).hexdigest()
        from vasp_agent.common import agent_paths
        target = agent_paths.store_item(self.workspace, 'evidence', digest + '.json')
        if not target.is_file():
            target.parent.mkdir(parents=True, exist_ok=True)
            temporary = target.parent / (digest + '.json.tmp')
            temporary.write_text(raw, encoding='utf-8')
            temporary.replace(target)
        return {'sha256': digest, 'chars': len(raw)}

    def audit_overflow(self, payload, session_id):
        """Store one oversized audit payload verbatim and return its inline stub."""
        raw = json.dumps(payload, ensure_ascii=False, default=str)
        digest = self.store_evidence(payload)['sha256']
        return {'payload_overflow': True, 'sha256': digest, 'chars': len(raw),
                'preview': raw[:AUDIT_PREVIEW_CHARS], 'session': session_id,
                'read_tool': {'name': 'read_context_evidence', 'sha256': digest, 'offset': 0},
                'note': '审计载荷超出内联上限，全文按内容寻址转存；这是原文，不是摘要。'}

    def audit_field(self, value, session_id):
        """Reduce one over-limit field, keeping the fields audit queries depend on."""
        if isinstance(value, dict) and ('result' in value or 'error' in value):
            bulky = [key for key in ('result', 'error') if key in value and
                     len(json.dumps(value[key], ensure_ascii=False, default=str)) > AUDIT_INLINE_LIMIT]
            if bulky:
                return {**value, **{key: self.audit_overflow(value[key], session_id) for key in bulky}}
        return self.audit_overflow(value, session_id)

    def log(self, data, session=None):
        """Append one audit record."""
        session = session if session is not None else getattr(self, 'session_id', None)
        payload = {key: value for key, value in data.items() if key not in ('time', 'session')}
        for key, value in list(payload.items()):
            if len(json.dumps(value, ensure_ascii=False, default=str)) > AUDIT_INLINE_LIMIT:
                payload[key] = self.audit_field(value, session)
        entry = {'time': datetime.now(timezone.utc).isoformat(), 'session': session, **payload}
        line = json.dumps(scrub_payload(entry), ensure_ascii=False, default=str)
        from vasp_agent.common import agent_paths
        agent_paths.append_audit(self.workspace, line + '\n')

    def call(self, name, arguments):
        from jsonschema import validate
        from vasp_agent.editing.workspace_editor import EDIT_TOOLS, audit_summary, safe_value
        try:
            schema = next((t for t in self.schemas() if t['name'] == name), None)
            if schema is None and name in SUBMIT_TOOLS:
                server = arguments.get('server') if isinstance(arguments, dict) else None
                action = ({'tool': 'request_submission', 'requires_user_input': True,
                           'arguments': {'server': server, 'job_name': arguments.get('job_name'),
                                         'phase': arguments.get('phase')}}
                          if server not in (None, 'local') else None)
                raise core.ToolError('SUBMISSION_NOT_AUTHORIZED', self.disabled_submission_message(), action)
            if schema is None and self.lazy_tool_groups:
                group = tool_groups.group_of(name)
                if group is not None and group not in self.active_tool_groups():
                    summary = tool_groups.GROUPS[group]['summary']
                    raise core.ToolError(
                        'TOOL_GROUP_NOT_ENABLED',
                        f'工具 {name} 属于按需加载组 {group}（{summary}），尚未展开；'
                        '请先调用 enable_tool_group 展开该组以查看完整参数定义，再调用该工具。',
                        {'tool': 'enable_tool_group', 'requires_user_input': False, 'arguments': {'groups': [group]}},
                    )
            if schema is None:
                raise ValueError('未知或未启用的工具。')
            if name in ('preview_scan_defaults', 'propose_scan_plan'):
                arguments = {'potential_variants': None, 'optional_stages': None, **arguments}
            if name in ('prepare_scan_retry', 'propose_scan_plan'):
                arguments = {'scan_thresholds': None, 'scan_preset': None, **arguments}
            if name == 'propose_scan_plan':
                arguments = {'band_path': None, 'stage_parameters': None, **arguments}
            if name == 'request_band_path_choice':
                arguments = {'extra_suggestion': None, 'extra_rationale': None, **arguments}
            if name == 'request_submission':
                arguments = {'resources': None, **arguments}
            if name in ('request_submission', 'apply_submission_resources'):
                arguments = null_resource_mode(arguments)
            if name == 'prepare_production_from_convergence':
                arguments = {'encut': None, **arguments}
            if name == 'recommend_submission_resources':
                arguments = {'submission_mode': None, **arguments}
            validate(arguments, schema['parameters'])
            result = getattr(self, name)(**arguments)
            output = {'ok': True, 'result': result}
        except Exception as exc:
            output = {'ok': False, 'error': scrub(str(exc))[:2000], 'type': type(exc).__name__,
                      'error_code': getattr(exc, 'error_code', 'FILE_NOT_FOUND' if isinstance(exc, FileNotFoundError) else 'PROCESS_TIMEOUT' if isinstance(exc, subprocess.TimeoutExpired) else 'TOOL_ERROR'),
                      'recoverable': False}
            next_steps = {
                'TOOL_GROUP_NOT_ENABLED': '先调用 enable_tool_group 展开对应工具组获取完整参数定义，再调用目标工具；展开目录不授予任何执行或提交权限。',
                'PHYSICS_CONFIRMATION_REQUIRED': '只读摘要可用 get_input_review；阶段放行需调用 request_physics_confirmation 弹出操作者确认菜单。',
                'SUBMISSION_NOT_AUTHORIZED': '远程任务：直接调用 request_submission 打开操作者授权菜单，这不算重复询问；不要让用户自己找命令，也不要声称部署不支持提交。之前的确认若已被撤销，必须重新确认。本机任务请操作者用 /submit once（与 /permissions 同一入口）。不会自动扩大授权。',
                'FILE_NOT_FOUND': '检查任务名及所需文件是否存在；不要重复准备或覆盖已有目录。',
                'INPUT_HASH_MISMATCH': '输入与 manifest 不一致；停止使用该证据，核对变更来源，不自动改写哈希。',
                'UNVERIFIED_METADATA': '摘要缺少哈希绑定；不能标记已核验，需核对原始任务来源。',
                'METADATA_INVALID': '摘要为空或格式无效；不能据此核验赝势。',
                'SSH_FAILED': '检查 SSH 连接、主机指纹和密钥代理；不会自动部署或提交。',
                'PROCESS_TIMEOUT': '操作超时；先核对状态，尤其不能重放可能已提交的写操作。',
            }
            if output['error_code'] in next_steps:
                output['next_step'] = next_steps[output['error_code']]
            if getattr(exc, 'next_action', None):
                output.update(recoverable=True, next_action=exc.next_action)
            if getattr(exc, 'connection', None) is not None:
                output.update(connection=exc.connection, warnings=exc.warnings, next_step=exc.next_step)
        if name in EDIT_TOOLS:
            output = safe_value(output)
        self.log({'tool': name, 'arguments': audit_summary(arguments) if name in EDIT_TOOLS else arguments,
                  'output': audit_summary(output) if name in EDIT_TOOLS else output})
        return output
