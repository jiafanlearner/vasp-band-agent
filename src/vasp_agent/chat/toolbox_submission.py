"""Toolbox mixin for submission authorization, resource planning, recovery, and Slurm submit/query."""
from __future__ import annotations

import json
import os
import re
import subprocess
from datetime import datetime, timezone
from uuid import uuid4

import vasp_agent.workflow.vasp_band_agent as core
from vasp_agent.chat.toolbox_core import scrub


class _SubmissionSettled(Exception):
    """Unwinds confirm() once a merged write-and-submit call already has its answer."""


class ToolboxSubmissionMixin:
    def submit_stage(self, server, job_name, phase):
        if server is None:
            return self.submit_job(job_name, phase=phase)
        return self.remote_submit(server, job_name, phase=phase)

    def request_submission(self, server, job_name, phase, resources=None):
        if self.plan_reader is None:
            raise core.ToolError('SUBMISSION_NOT_AUTHORIZED', '需要操作者在交互菜单中确认；未授权或提交。')
        from vasp_agent.remote.remote_compute import RemoteCompute
        from vasp_agent.permissions.submission_dialog import confirm
        remote = RemoteCompute(self.workspace, server)
        self._merged_submission = None
        self.configure_then_submit = lambda **kw: self._configure_then_submit(remote, server, job_name, **kw)
        try:
            result = confirm(self, server, job_name, phase,
                             lambda action, **kw: remote.call(action, job_name, **kw), authorize=True,
                             expected_resources=resources)
        except _SubmissionSettled:
            result = None
        finally:
            self.configure_then_submit = None
        merged, self._merged_submission = self._merged_submission, None
        if merged is not None:
            kind, value = merged
            if kind == 'error':
                raise value
            return value
        if result is not None:
            return self.track_submission(server, job_name, phase, result)
        if not self.allow_submit or self.submission_target != (server, job_name, phase):
            raise core.ToolError('SUBMISSION_NOT_AUTHORIZED', '操作者确认未完成，未提交。')
        return self._remote_submit(server, job_name, phase, confirmed=True)

    def request_job_cancellation(self, server, job_name, phase, action):
        from vasp_agent.chat.cancel_commands import request
        return request(self, server, job_name, phase, action)

    def get_submission_recovery(self, server, job_name, operation_id):
        from vasp_agent.remote.remote_compute import RemoteCompute
        return RemoteCompute(self.workspace, server).call('recovery_status', job_name, operation_id=operation_id)

    def request_submission_recovery(self, server, job_name, phase, job_id):
        from vasp_agent.remote.remote_compute import RemoteCompute
        remote = RemoteCompute(self.workspace, server)
        context = remote.call('recovery_context', job_name, phase=phase, job_id=job_id)
        if self.plan_reader is None:
            return {'status': 'operator_confirmation_required', 'context': context}
        output = getattr(self, 'plan_output', print)
        output(f'不确定回执人工对账：{server} / {job_name} / {phase} / JobID={job_id}')
        output(json.dumps(context, ensure_ascii=False, indent=2))
        output('历史作业已失败且未分配节点，但旧回执没有唯一尝试标识。此次由你确认以该历史作业作为恢复依据；不是程序证明唯一关联。原回执将归档，恢复后需重新审阅和单独提交授权；本次不修改资源、不重交。')
        if self.plan_reader('接受关联缺口并恢复请输入“确认对账”；其他输入取消：').strip() != '确认对账':
            return {'status': 'cancelled', 'record_modified': False}
        operation_id = uuid4().hex
        self.allow_submit = False
        self.submission_target = None
        try:
            result = remote.call('recover_submission', job_name, phase=phase, job_id=job_id,
                                 snapshot=context['snapshot'], operation_id=operation_id, operator_confirmed=True)
        except Exception:
            # Never replay a remote mutation merely because its response was lost.
            return {'status': 'recovery_result_uncertain', 'operation_id': operation_id,
                    'next': '先只读核查恢复记录，不重新提交或重复归档。'}
        self.allow_submit = False
        self.submission_target = None
        return result

    def get_submission_diagnostic(self, server, job_name, phase, job_id=None, start_time=None, end_time=None):
        from vasp_agent.remote.remote_compute import RemoteCompute
        return RemoteCompute(self.workspace, server).call('submission_diagnostic', job_name, phase=phase, job_id=job_id, start_time=start_time, end_time=end_time)

    def plan_submission_resources(self, server, job_name, phase):
        from vasp_agent.remote.remote_compute import RemoteCompute
        limits = self.submission_limits
        if limits is not None and limits.get('server') != server:
            raise ValueError('服务器不在当前资源授权范围内。')
        result = RemoteCompute(self.workspace, server).call(
            'plan_submission_resources', job_name, phase=phase, limits=limits)
        if result.get('planned'):
            suggestion = {key: result[key] for key in ('signature', 'resources', 'rationale')}
            suggestion['automatic'] = True
            self.resource_suggestions[(server, job_name, phase)] = suggestion
            self.log({'automatic_resource_suggestion': suggestion, 'server': server,
                      'job_name': job_name, 'phase': phase})
        return result

    def apply_submission_resources(self, server, job_name, phase, resources):
        from vasp_agent.remote.remote_compute import RemoteCompute
        from vasp_agent.permissions.submission_dialog import confirm
        remote = RemoteCompute(self.workspace, server)
        result = confirm(self, server, job_name, phase,
                         lambda action, **kw: remote.call(action, job_name, **kw), apply_only=True, expected_resources=resources)
        return result if result is not None else {'status': 'resources_already_confirmed',
            'submitted': False, 'next': '资源已应用或确认；读取当前阶段快照重新审阅，不代表已提交。'}

    def stage_submit_script(self, folder):
        """Copy the session's verified script into a local task, once, with provenance."""
        task_script, provenance = folder / 'submit.slurm', folder / 'submit-script-source.json'
        if task_script.exists():
            if provenance.is_file():
                source = json.loads(provenance.read_text(encoding='utf-8'))
                if source.get('source_sha256') != self.script_hash:
                    raise ValueError('任务中的 submit.slurm 来自另一份提交脚本（'
                                     f"{source.get('source_path')}），与本会话审核的脚本不同；"
                                     '请新建任务，或用原脚本重新启动会话。')
            return task_script
        if self.submit_script is None:
            raise ValueError('请先配置本机提交脚本。')
        if core.digest(self.submit_script) != self.script_hash:
            raise ValueError('Slurm 脚本已改变，请重新启动会话审核配置。')
        task_script.write_text(self.submit_script.read_text(encoding='utf-8'), encoding='utf-8')
        record = {'source_path': str(self.submit_script), 'source_sha256': self.script_hash,
                  'staged_sha256': core.digest(task_script), 'time': datetime.now(timezone.utc).isoformat()}
        core.save(provenance, record)
        self.log({'submit_script_staged': {'job': folder.name, **record}})
        return task_script

    def get_submission_resources(self, server, job_name, phase):
        if server is None:
            from vasp_agent.remote.remote_worker import resource_context
            folder = self.job(job_name)
            self.stage_submit_script(folder)
            result = resource_context(folder, phase)
        else:
            from vasp_agent.remote.remote_compute import RemoteCompute
            result = RemoteCompute(self.workspace, server).call('resource_context', job_name, phase=phase)
        return {**result, 'limits': self.submission_limits}

    def recommend_submission_resources(self, server, job_name, phase, signature, parallel_tests, cores_per_test, rationale, nodes=None, submission_mode=None):
        from vasp_agent.permissions.submission_dialog import validate_resources
        data = self.get_submission_resources(server, job_name, phase)
        if data.get('already_registered') or signature != data['signature']:
            raise ValueError('任务已提交或配置已变化，请重新读取。')
        resources = {**data['resources'], 'parallel_tests': parallel_tests, 'cores_per_test': cores_per_test}
        if nodes is not None:
            resources['nodes'] = nodes
        if submission_mode is not None:
            resources['submission_mode'] = submission_mode
        if server is None and resources.get('submission_mode') == 'array':
            raise ValueError('本机提交暂不支持 array；请使用已保存的远程服务器配置。')
        validate_resources(resources, phase, self.submission_limits, data.get('execution_plan', {}).get('tests'))
        if not rationale.strip():
            raise ValueError('资源建议必须附带理由。')
        suggestion = {'signature': signature, 'resources': resources, 'rationale': scrub(rationale)}
        self.resource_suggestions[(server or 'local', job_name, phase)] = suggestion
        self.log({'ai_resource_suggestion': suggestion, 'job_name': job_name, 'server': server, 'phase': phase})
        return {'saved': True, 'operator_confirmed': False, 'next': '提出提交请求时调用提交工具，程序会让操作者选择。', **suggestion}

    def authorize_submission(self, server, job_name, phase):
        """The grant checks both submission paths must apply, in one place."""
        if not self.allow_submit or self.submissions >= self.max_submissions:
            raise core.ToolError('SUBMISSION_NOT_AUTHORIZED', '未启用提交或额度已用完；请用 request_submission 打开本次任务的操作者确认菜单。')
        if self.submission_limits is not None and server != self.submission_limits['server']:
            raise core.ToolError('SUBMISSION_NOT_AUTHORIZED', '服务器不在当前提交授权范围内。')
        if self.submission_target is not None and self.submission_target != (server, job_name, phase):
            raise core.ToolError('SUBMISSION_NOT_AUTHORIZED', '本次授权仅限已确认的任务和阶段，请为新目标打开提交确认菜单。')

    def remote_submit(self, server, job_name, phase=None):
        return self._remote_submit(server, job_name, phase)

    def _configure_then_submit(self, remote, server, job_name, phase, **arguments):
        """Resource write plus submission in one remote call (see confirm/finish)."""
        self.authorize_submission(server, job_name, phase)
        holder = {}
        def call():
            reply = remote.call('configure_and_submit', job_name, phase=phase, submit_limits=self.submission_limits, **arguments)
            holder['configured'] = {k: v for k, v in reply.items() if k not in ('submission', 'not_submitted')}
            if reply.get('submission') is None:
                return {'submission_not_attempted': True, 'not_submitted': reply.get('not_submitted')}
            return reply['submission']
        try:
            outcome = self._submit_call(remote, server, job_name, phase, call)
        except Exception as exc:
            if 'configured' not in holder:
                raise  # the write itself is uncertain: finish keeps its pending record
            self._merged_submission = ('error', exc)  # written; refused before sbatch
            return holder['configured']
        if 'configured' not in holder:
            # Response lost, existing receipt recovered read-only: that is the answer.
            self._merged_submission = ('result', outcome)
            raise _SubmissionSettled()
        if not outcome.get('not_submitted'):
            self._merged_submission = ('result', outcome)
        return holder['configured']

    def _remote_submit(self, server, job_name, phase=None, confirmed=False):
        from vasp_agent.remote.remote_compute import RemoteCompute
        self.authorize_submission(server, job_name, phase)
        remote = RemoteCompute(self.workspace, server)
        if not confirmed:
            from vasp_agent.permissions.submission_dialog import confirm
            result = confirm(self, server, job_name, phase, lambda action, **kw: remote.call(action, job_name, **kw))
            if result is not None:
                return self.track_submission(server, job_name, phase, result)
        return self._submit_call(remote, server, job_name, phase,
            lambda: remote.call('submit_checked', job_name, limits=self.submission_limits, phase=phase))

    def _submit_call(self, remote, server, job_name, phase, call):
        """Spend the attempt, run the remote call that may sbatch, settle the outcome."""
        before = (self.submissions, self.allow_submit, getattr(self, 'revocation', None))
        self.consume_submission()
        try:
            result = call()
        except Exception as exc:
            # Read only: a lost response must never trigger another sbatch.
            try:
                registered = remote.call('resource_context', job_name, phase=phase)
            except Exception:
                registered = {}
            if registered.get('already_registered'):
                from vasp_agent.remote.remote_worker import diagnostic_text
                confirmed = registered.get('status') == 'submitted' and bool(re.fullmatch(r'[0-9]+', str(registered.get('job_id', ''))))
                return self.track_submission(server, job_name, phase, {**registered, 'record_recovered_after_error': True,
                        'submission_confirmed': confirmed,
                        'original_error': {'type': type(exc).__name__,
                            'code': getattr(exc, 'error_code', None), 'message': diagnostic_text(str(exc))},
                        'next': '只读找到了既有记录，不代表发生过SSH断线；无有效JobID时仍不明确，须核查记账与诊断，禁止重复提交。'})
            raise
        if result.get('submission_not_attempted'):
            self.submissions, self.allow_submit, self.revocation = before
            self.log({'permission': 'attempt_returned', 'reason': 'refused_before_sbatch',
                      'server': server, 'job_name': job_name, 'phase': phase})
            if result.get('error'):
                raise core.ToolError(result.get('error_code') or 'PREFLIGHT_REFUSED', result['error'])
            if result.get('not_submitted'):
                return result  # resources written; review or a separate submit still needed
            return self.track_submission(server, job_name, phase,
                {**result, 'next': '预检未放行；核查已有提交记录，不自动重复提交。'})
        return self.track_submission(server, job_name, phase, result)

    def submit_job(self, job_name, phase=None):
        if not self.allow_submit or self.submit_script is None:
            raise core.ToolError('SUBMISSION_NOT_AUTHORIZED', '未启用提交或未配置 Slurm 脚本。')
        folder = self.job(job_name)
        if (folder / '.task-archived.json').exists():
            raise ValueError('任务已归档，请先恢复再提交。')
        manifest = core.verify(folder)
        if manifest.get('workflow') != 'full':
            raise ValueError('AI 提交只支持完整流程。')
        staged = manifest.get('stage_gates', False)
        if staged:
            import vasp_agent.workflow.stage_gate as stage_gate
            record = stage_gate.submission_record(folder, phase)
        else:
            if phase is not None:
                raise ValueError('旧任务用 submit_stage 且 phase=null，不支持分阶段提交。')
            record = folder / 'submission.json'
        if record.exists():
            return self.track_submission(None, job_name, phase,
                {'already_registered': True, **json.loads(record.read_text(encoding='utf-8'))})
        self.authorize_submission('local', job_name, phase)
        if self.submission_limits is not None:
            from vasp_agent.remote.remote_worker import check_resources
            check_resources(self.submit_script.read_text(encoding='utf-8'), self.submission_limits)
        if core.digest(self.submit_script) != self.script_hash:
            raise ValueError('Slurm 脚本已改变，请重新启动会话审核配置。')
        task_script = self.stage_submit_script(folder)
        from vasp_agent.remote.remote_worker import array_layout
        if (json.loads((folder / 'config.json').read_text(encoding='utf-8')).get('submission_mode') == 'array'
                or array_layout(task_script.read_text(encoding='utf-8')) is not None):
            raise ValueError('本机提交暂不支持 array；请使用远程服务器的 convergence 阶段提交。')
        from vasp_agent.permissions.submission_dialog import confirm
        from vasp_agent.remote.remote_worker import resource_context, configure_resources
        def resource_rpc(action, **kwargs):
            if action == 'resource_context':
                return resource_context(folder, kwargs['phase'])
            return configure_resources(folder, kwargs['phase'], kwargs['resources'], kwargs['signature'], kwargs['limits'], kwargs.get('operation_id'))
        resource_result = confirm(self, 'local', job_name, phase, resource_rpc)
        if resource_result is not None:
            return self.track_submission(None, job_name, phase, resource_result)
        if staged:
            stage_gate.require_review(folder, phase)
            stage_gate.check_previous_job(folder, phase)
        elif manifest.get('review_required'):
            from vasp_agent.workflow.input_review import require_review
            require_review(folder)
        if self.submissions >= self.max_submissions:
            raise core.ToolError('SUBMISSION_NOT_AUTHORIZED', '本次启动的提交次数已达上限。')
        if core.digest(self.submit_script) != self.script_hash:
            raise ValueError('Slurm 脚本已改变，请重新启动会话审核配置。')
        if (folder / '.agent.lock').exists():
            raise ValueError('该计算目录正在被使用。')
        # A durable reservation prevents duplicates even after an uncertain sbatch timeout.
        record.parent.mkdir(exist_ok=True)
        with record.open('x', encoding='utf-8') as out:
            json.dump({'status': 'submission_pending', 'job_id': None}, out)
        self.consume_submission()
        env = dict({k: v for k, v in os.environ.items() if not k.startswith('SBATCH_')}, AGENT_DIR=str(self.agent_dir), WORK_DIR=str(folder))
        if staged:
            env['VASP_AGENT_PHASE'] = phase
        if getattr(self, 'session_id', None):
            env['VASP_AGENT_SESSION'] = self.session_id
        try:
            result = subprocess.run(['sbatch', '--parsable', str(task_script)],
                cwd=folder, env=env, capture_output=True, text=True, timeout=30, check=True)
            job_id = result.stdout.strip().split(';')[0]
            if not re.fullmatch(r'\d+', job_id):
                raise ValueError('sbatch 返回无法识别，必须人工核对队列后再处理。')
            data = {'status': 'submitted', 'job_id': job_id, 'script_sha256': core.digest(task_script)}
            if staged:
                data['phase'] = phase
                core.save(folder / 'submission.json', data)
            core.save(record, data)
        except Exception:
            core.save(record, {'status': 'submission_uncertain', 'job_id': None,
                              'next': '人工核对 squeue/sacct；禁止自动重新提交。'})
            raise
        return self.track_submission(None, job_name, phase, data)

    def query_job(self, job_name):
        record = json.loads((self.job(job_name) / 'submission.json').read_text(encoding='utf-8'))
        job_id = str(record.get('job_id', ''))
        if not re.fullmatch(r'\d+', job_id):
            return record
        queue = subprocess.run(['squeue', '-h', '-j', job_id, '-o', '%i|%T|%M|%R'],
                               capture_output=True, text=True, timeout=20, check=True)
        if queue.stdout.strip():
            return {'source': 'squeue', 'job_id': job_id, 'status': scrub(queue.stdout[:4000])}
        accounting = subprocess.run(['sacct', '-n', '-j', job_id, '--format=JobID,State,ExitCode', '-P'],
                                     capture_output=True, text=True, timeout=20, check=True)
        return {'source': 'sacct', 'job_id': job_id, 'status': scrub(accounting.stdout[:4000]),
                'note': '队列退出或调度器 COMPLETED 不等于计算已通过科学验收。'}
