"""Headless ControlService: role-gated control tools, model_views compaction, and 12 KB response cap."""
from __future__ import annotations

import json
import subprocess
import threading
from pathlib import Path
from typing import Any

from jsonschema import validate

from vasp_agent.chat import model_views
from vasp_agent.chat.ai_agent import Toolbox
from vasp_agent.chat.toolbox_core import scrub, scrub_payload
from vasp_agent.common.workspace_paths import local_path
from vasp_agent.control import service_contracts
from vasp_agent.control.tool_specs import COMPACT_ALIAS, ROLES, SPEC_BY_NAME, tool_definitions
import vasp_agent.workflow.vasp_band_agent as core

MAX_RESPONSE_BYTES = 12 * 1024
_SUMMARY_KEYS = (
    'snapshot',
    'phase',
    'status',
    'job_id',
    'error_code',
    'blocked',
    'operator_confirmed',
    'physics_confirmed',
    'required_checks',
    'rules_version',
)


def _encode_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(',', ':'), default=str)


def _normalize_server(server: str | None) -> str | None:
    return None if server in (None, 'local') else server


def _extract_summary(result: dict[str, Any]) -> dict[str, Any]:
    summary: dict[str, Any] = {}
    used = 0
    for key in _SUMMARY_KEYS:
        if key not in result:
            continue
        size = len(_encode_json(result[key]).encode('utf-8'))
        if size <= 512 and used + size <= 2048:
            summary[key] = result[key]
            used += size
    return summary


class ControlService:
    """Headless control plane wrapping non-interactive Toolbox without automatic submission grants."""

    def __init__(self, workspace: str | Path, *, role: str = 'maker', actor_id: str | None = None):
        if role not in ROLES:
            raise ValueError(f'无效 role={role!r}，必须为 {" / ".join(ROLES)}。')
        self.workspace = Path(workspace).resolve(strict=True)
        if not self.workspace.is_dir():
            raise NotADirectoryError(f'工作区路径不是目录：{self.workspace.name}')
        self.role = role
        self.actor_id = (actor_id or f'{role}-headless').strip() or f'{role}-headless'
        self._call_lock = threading.RLock()
        self.box = Toolbox(self.workspace, allow_submit=False, max_submissions=0)
        self.box.plan_reader = None
        self.box.plan_output = lambda *_args, **_kwargs: None
        self.box.require_scan_plans = True
        self.box.allow_submit = False
        self.box.max_submissions = 0

    def tools(self) -> list[dict[str, Any]]:
        return tool_definitions(self.role)

    def tool_names(self) -> list[str]:
        return [item['name'] for item in self.tools()]

    def _trim_log_tail(self, full_result: dict[str, Any], raw_bytes: int) -> dict[str, Any]:
        stored = self.box.store_evidence(full_result)
        tail = str(full_result.get('tail', ''))
        meta = {
            **{k: v for k, v in full_result.items() if k != 'tail'},
            'truncated_to_response_limit': True,
            'original_bytes': raw_bytes,
            'max_response_bytes': MAX_RESPONSE_BYTES,
            'read_tool': {
                'name': 'vasp_read_evidence',
                'arguments': {'kind': 'evidence', 'sha256': stored['sha256'], 'offset': 0},
            },
        }
        low, high = 0, len(tail)
        best = ''
        while low <= high:
            mid = (low + high) // 2
            candidate = tail[-mid:] if mid else ''
            envelope = {'ok': True, 'result': {**meta, 'tail': candidate}}
            if len(_encode_json(envelope).encode('utf-8')) <= MAX_RESPONSE_BYTES:
                best = candidate
                low = mid + 1
            else:
                high = mid - 1
        return {'ok': True, 'result': {**meta, 'tail': best}}

    def _compact_and_cap(self, name: str, args: dict[str, Any], output: dict[str, Any]) -> dict[str, Any]:
        if output.get('ok') is not True:
            return output
        full_result = output.get('result')
        is_read_evidence = name == 'vasp_read_evidence'
        is_evidence_page = is_read_evidence and args.get('kind') == 'evidence'
        is_log_tail = is_read_evidence and args.get('kind') == 'log'
        if is_read_evidence:
            compacted = output
        else:
            mapped = COMPACT_ALIAS.get(name, name)
            if mapped in model_views.COMPACT_TOOLS:
                compacted = model_views.model_output(mapped, output, self.box.store_evidence)
            elif isinstance(full_result, dict) and len(model_views.encoded(full_result)) >= model_views.MIN_CHARS:
                compacted = {**output, 'result': model_views.compact_result(full_result, self.box.store_evidence, name)}
            else:
                compacted = output

        raw_bytes = len(_encode_json(compacted).encode('utf-8'))
        if raw_bytes <= MAX_RESPONSE_BYTES or is_evidence_page:
            return compacted
        if is_log_tail and isinstance(full_result, dict) and isinstance(full_result.get('tail'), str):
            return self._trim_log_tail(full_result, raw_bytes)

        stored = self.box.store_evidence(full_result)
        full_text = _encode_json(full_result)
        summary = _extract_summary(full_result) if isinstance(full_result, dict) else {}
        preview_limit = 1200
        while True:
            overflow_output = {
                'ok': True,
                'result': {
                    'payload_overflow': True,
                    'sha256': stored['sha256'],
                    'chars': stored['chars'],
                    'original_bytes': raw_bytes,
                    'max_response_bytes': MAX_RESPONSE_BYTES,
                    'summary': summary,
                    'preview': full_text[:preview_limit],
                    'read_tool': {
                        'name': 'vasp_read_evidence',
                        'arguments': {'kind': 'evidence', 'sha256': stored['sha256'], 'offset': 0},
                    },
                    'note': '响应超过 12 KB 上限，完整原文已按内容寻址转存至 .agent/evidence/，请使用 vasp_read_evidence 分页读取。',
                },
            }
            if len(_encode_json(overflow_output).encode('utf-8')) <= MAX_RESPONSE_BYTES or preview_limit == 0:
                return overflow_output
            preview_limit = max(0, preview_limit // 2)

    def call(self, name: str, arguments: dict[str, Any] | None = None) -> dict[str, Any]:
        args = {} if arguments is None else arguments
        with self._call_lock:
            try:
                spec = SPEC_BY_NAME.get(name)
                if spec is None:
                    raise core.ToolError('UNKNOWN_TOOL', f'未知或未启用的控制面工具：{name}。')
                if self.role not in spec['roles']:
                    raise core.ToolError(
                        'TOOL_NOT_ALLOWED_FOR_ROLE',
                        f'角色 {self.role} 无权调用工具 {name}。',
                    )
                if not isinstance(args, dict):
                    raise core.ToolError('INVALID_ARGUMENTS', '工具参数必须是 JSON 对象。')
                validate(args, spec['inputSchema'])
                handler = getattr(self, name)
                raw_result = handler(**args)
                scrubbed_result = scrub_payload(raw_result)
                output = self._compact_and_cap(name, args, {'ok': True, 'result': scrubbed_result})
            except Exception as exc:
                error_code = getattr(
                    exc,
                    'error_code',
                    'FILE_NOT_FOUND'
                    if isinstance(exc, FileNotFoundError)
                    else 'PROCESS_TIMEOUT'
                    if isinstance(exc, subprocess.TimeoutExpired)
                    else 'TOOL_ERROR',
                )
                output = {
                    'ok': False,
                    'error': scrub(str(exc))[:2000],
                    'type': type(exc).__name__,
                    'error_code': error_code,
                    'recoverable': bool(getattr(exc, 'next_action', None)),
                }
                if getattr(exc, 'next_action', None):
                    output['next_action'] = scrub_payload(exc.next_action)
            self.box.log({
                'control_plane': True,
                'role': self.role,
                'actor_id': self.actor_id,
                'tool': name,
                'arguments': args,
                'output': output,
            })
            return output

    # ----------------------------------------------------------------- P0 tools

    def vasp_workspace_info(self) -> dict[str, Any]:
        files_info = self.box.list_files()
        return {
            'workspace': self.workspace.name,
            'role': self.role,
            'actor_id': self.actor_id,
            'autonomy_phase': service_contracts.AUTONOMY_PHASE,
            'submission_enabled': False,
            'files': files_info['files'],
            'jobs': files_info['jobs'],
            'potcar_configured': files_info['potcar_configured'],
            'potcar_library_configured': files_info['potcar_library_configured'],
            'servers': self.box.list_servers(),
            'contract': self.vasp_contract_status(),
        }

    def _sanitize_potential_record(self, item: Any) -> Any:
        if not isinstance(item, dict) or not isinstance(item.get('source'), str):
            return item
        source = Path(item['source'])
        try:
            rel = source.resolve().relative_to(self.workspace).as_posix()
        except (OSError, ValueError):
            rel = source.name
        return {**item, 'source': rel}

    def vasp_inspect_structure(self, path: str, include_potentials: bool = True) -> dict[str, Any]:
        candidate = Path(local_path(path))
        if self.role == 'checker' and candidate.is_absolute():
            if not candidate.resolve().is_relative_to(self.workspace):
                raise core.ToolError(
                    'TOOL_NOT_ALLOWED_FOR_ROLE',
                    '只读审查角色禁止导入工作区外的结构文件。',
                )
        structure = self.box.inspect_structure(path)
        rel_path = Path(structure['path']).as_posix()
        result = {**structure, 'path': rel_path, 'content_is_untrusted': True}
        if include_potentials:
            potentials = self.box.inspect_potentials(rel_path)
            if isinstance(potentials, dict) and isinstance(potentials.get('selection'), list):
                potentials = {
                    **potentials,
                    'selection': [self._sanitize_potential_record(item) for item in potentials['selection']],
                }
            result['potentials'] = potentials
        return result

    def vasp_preview_defaults(
        self,
        job_name: str,
        structure: str,
        encut: float,
        mesh: list[int],
        nbands: int,
        relax_mode: str,
        server: str | None = None,
        parallel_tests: int | None = None,
        cores_per_test: int | None = None,
        potential_variants: list[dict[str, str]] | None = None,
        optional_stages: list[str] | None = None,
    ) -> dict[str, Any]:
        return self.box.preview_scan_defaults(
            _normalize_server(server),
            job_name,
            structure,
            encut,
            mesh,
            nbands,
            relax_mode,
            parallel_tests,
            cores_per_test,
            potential_variants=potential_variants,
            optional_stages=optional_stages,
        )

    def vasp_task_status(
        self,
        job_name: str,
        server: str | None = None,
        include_convergence: bool = False,
    ) -> dict[str, Any]:
        target = _normalize_server(server)
        status = self.box.get_status(job_name) if target is None else self.box.remote_status(target, job_name)
        if isinstance(status, dict) and isinstance(status.get('completed_stage_records'), list):
            status = {
                **status,
                'completed_stage_records': [Path(p).as_posix() for p in status['completed_stage_records']],
            }
        if include_convergence:
            try:
                status = {**status, 'convergence_inspection': self.box.inspect_convergence(target, job_name)}
            except (FileNotFoundError, ValueError, core.ToolError) as exc:
                status = {**status, 'convergence_inspection_error': scrub(str(exc))}
        return status

    def vasp_read_evidence(
        self,
        kind: str,
        sha256: str | None = None,
        offset: int = 0,
        guide: str | None = None,
        page: int = 1,
        job_name: str | None = None,
        file: str | None = None,
    ) -> dict[str, Any]:
        if kind == 'evidence':
            if not sha256:
                raise ValueError('kind=evidence 需要提供 sha256。')
            return self.box.read_context_evidence(sha256, offset)
        if kind == 'guide':
            if not guide:
                raise ValueError('kind=guide 需要提供 guide。')
            return self.box.read_stage_guide(guide, page)
        if kind == 'log':
            if not job_name or not file:
                raise ValueError('kind=log 需要提供 job_name 与 file。')
            return self.box.read_log(job_name, file)
        raise ValueError(f'不支持的证据读取类型：{kind}。')

    def vasp_diagnose_submission(
        self,
        server: str,
        job_name: str,
        phase: str | None = None,
        job_id: str | None = None,
        start_time: str | None = None,
        end_time: str | None = None,
        operation_id: str | None = None,
        include_resources: bool = True,
    ) -> dict[str, Any]:
        result = dict(
            self.box.get_submission_diagnostic(
                server,
                job_name,
                phase,
                job_id=job_id,
                start_time=start_time,
                end_time=end_time,
            )
        )
        if operation_id:
            result['recovery'] = self.box.get_submission_recovery(server, job_name, operation_id)
        if include_resources:
            try:
                result['resources_context'] = self.box.get_submission_resources(server, job_name, phase)
            except Exception as exc:
                result['resources_context_error'] = scrub(str(exc))
        return result

    def vasp_contract_status(self, contract_id: str | None = None) -> dict[str, Any]:
        return service_contracts.contract_status(self, contract_id)

    def vasp_draft_contract(self, **kwargs: Any) -> dict[str, Any]:
        return service_contracts.draft_contract(self, **kwargs)

    def vasp_circuit_break(
        self,
        contract_id: str,
        reason: str,
        code: str = 'CIRCUIT_BREAKER',
        uncertain: bool = False,
        details: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        return service_contracts.circuit_break(
            self,
            contract_id=contract_id,
            reason=reason,
            code=code,
            uncertain=uncertain,
            details=details,
        )

    def vasp_get_stage_review(self, job_name: str, server: str | None = None) -> dict[str, Any]:
        return self.box.get_stage_review(_normalize_server(server), job_name)

    def vasp_fetch_results(
        self,
        server: str,
        job_name: str,
        analyze_bands: bool = False,
        table_offset: int = 0,
        table_limit: int = 0,
        replot: bool = False,
        y_min: float | None = None,
        y_max: float | None = None,
    ) -> dict[str, Any]:
        result: dict[str, Any] = {'fetch': self.box.remote_fetch(server, job_name)}
        if analyze_bands:
            result['band_analysis'] = self.box.analyze_band_results(
                server,
                job_name,
                table_offset=table_offset,
                table_limit=table_limit,
            )
        if replot:
            result['replot'] = self.box.replot_bands(server, job_name, y_min=y_min, y_max=y_max)
        return result

    def vasp_sandbox_analysis(
        self,
        action: str,
        server: str | None = None,
        job_name: str | None = None,
        files: list[str] | None = None,
        script: str | None = None,
        expected_sha256: str | None = None,
        inputs: list[dict[str, str]] | None = None,
        timeout_seconds: int | None = None,
        image_profile: str | None = None,
        export_outputs: bool | None = None,
        run_id: str | None = None,
    ) -> dict[str, Any]:
        if action == 'runtime':
            return self.box.get_analysis_runtime()
        if action == 'stage':
            if not server or not job_name or not files:
                raise ValueError('action=stage 需要提供 server、job_name 与 files。')
            return self.box.stage_download_for_analysis(server, job_name, files)
        if action == 'validate':
            if not script or not expected_sha256 or inputs is None:
                raise ValueError('action=validate 需要提供 script、expected_sha256 与 inputs。')
            return self.box.validate_analysis_script(script, expected_sha256, inputs)
        if action == 'run':
            if not script or not expected_sha256 or inputs is None or timeout_seconds is None:
                raise ValueError('action=run 需要提供 script、expected_sha256、inputs 与 timeout_seconds。')
            return self.box.run_analysis_script(
                script,
                expected_sha256,
                inputs,
                timeout_seconds,
                image_profile=image_profile,
                export_outputs=export_outputs,
            )
        if action == 'get':
            if not run_id:
                raise ValueError('action=get 需要提供 run_id。')
            return self.box.get_analysis_run(run_id)
        raise ValueError(f'不支持的沙箱分析动作：{action}。')
