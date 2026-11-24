"""Toolbox mixin for workbench editing, sandbox/local analysis, servers, and stage guides."""
from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path

import vasp_agent.workflow.vasp_band_agent as core
from vasp_agent import guides
from vasp_agent.workflow import stage_definitions


class ToolboxWorkspaceMixin:
    def register_server(self, name, login_file):
        from vasp_agent.remote.remote_servers import Servers
        return Servers(self.workspace).register(name, login_file)

    def update_server_identity(self, server, identity_file, expected_config_sha256):
        from vasp_agent.remote.remote_servers import Servers
        return Servers(self.workspace).update_identity(server, identity_file, expected_config_sha256,
                                                       getattr(self, '_edit_request_id', None))

    def list_servers(self):
        from vasp_agent.remote.remote_servers import Servers
        return Servers(self.workspace).list()

    def remote_operation(self, server, action, path):
        from vasp_agent.remote.remote_servers import Servers
        return Servers(self.workspace).operate(server, action, path)

    def analysis_runner(self):
        from vasp_agent.editing.analysis_runner import AnalysisRunner
        return AnalysisRunner(self.workspace)

    def get_analysis_runtime(self):
        from vasp_agent.editing.analysis_runner import probe
        return probe()

    def validate_analysis_script(self, script, expected_sha256, inputs):
        return self.analysis_runner().validate(script, expected_sha256, inputs)

    def run_analysis_script(self, script, expected_sha256, inputs, timeout_seconds):
        return self.analysis_runner().run(script, expected_sha256, inputs, timeout_seconds,
                                          getattr(self, '_edit_request_id', None))

    def request_analysis_approval_settings(self):
        from vasp_agent.permissions.analysis_approval import settings
        return settings(self)

    def run_local_analysis_script(self, script, expected_sha256, inputs, timeout_seconds):
        from vasp_agent.permissions.analysis_approval import approve
        return self.analysis_runner().run(script, expected_sha256, inputs, timeout_seconds,
            getattr(self, '_edit_request_id', None), backend='local',
            approve=lambda files, manifest, seconds: approve(self, files, manifest, seconds))

    def get_analysis_run(self, run_id):
        return self.analysis_runner().get(run_id)

    def stage_download_for_analysis(self, server, job_name, files):
        """Copy receipt-verified downloaded results into workbench/ for analysis scripts."""
        source, receipt = self.latest_download(server, job_name)
        editor = self.editor()
        target = editor.root / 'data' / server / job_name
        staged = []
        for name in files:
            entry = receipt.get(name)
            if not isinstance(entry, dict) or '/' in name or '\\' in name:
                raise core.ToolError('ANALYSIS_ARGUMENT', f'{name} 不在下载收据中，未复制。')
            if not (name.lower().endswith(('.csv', '.json', '.md', '.txt')) or name.endswith('POSCAR')):
                raise core.ToolError('ANALYSIS_ARGUMENT', f'{name} 不是文本结果文件；分析输入只支持 csv/json/md/txt/POSCAR。')
            data = (source / name).read_bytes()
            digest = hashlib.sha256(data).hexdigest()
            if digest != entry.get('sha256'):
                raise core.ToolError('INPUT_HASH_MISMATCH', f'{name} 与下载收据的哈希不符，已拒绝。')
            if len(data) > 1024 * 1024:
                raise core.ToolError('EDIT_TOO_LARGE', f'{name} 超过 1 MiB，无法作为分析输入。')
            target.mkdir(parents=True, exist_ok=True)
            destination = target / name
            if destination.exists() and destination.read_bytes() != data:
                destination = target / f'{Path(name).stem}-{digest[:8]}{Path(name).suffix}'
            if not destination.exists():
                destination.write_bytes(data)
            staged.append({'path': destination.relative_to(editor.root).as_posix(), 'expected_sha256': digest,
                           'source': (source / name).relative_to(self.workspace).as_posix()})
        return {'staged': staged, 'source_directory': source.relative_to(self.workspace).as_posix(),
                'note': '仅复制经下载收据哈希校验的文本文件；staged 项可直接作为 validate/run_analysis_script 的 inputs。'
                        '下载完整性不等于科学验收。'}

    def latest_download(self, server, job_name):
        """Newest downloads/<server>/<job>-<hex8>/ folder and its receipt's file table."""
        if not all(re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_-]{0,63}', value) for value in (server, job_name)):
            raise ValueError('服务器或任务名无效。')
        base = self.path('downloads') / server
        candidates = sorted((folder for folder in base.glob(job_name + '-*') if folder.is_dir()
                             and re.fullmatch(re.escape(job_name) + r'-[0-9a-f]{8}', folder.name)),
                            key=lambda folder: folder.stat().st_mtime)
        if not candidates:
            raise core.ToolError('FILE_NOT_FOUND', '本地没有该任务的已下载结果；请先用 remote_fetch 下载。')
        source = candidates[-1]
        receipt = json.loads((source / 'download-receipt.json').read_text(encoding='utf-8')).get('files', {})
        return source, receipt

    def replot_bands(self, server, job_name, y_min=None, y_max=None):
        """Redraw a finished band plot locally; the delivered bands.png/.pdf stay as hashed."""
        from vasp_agent.workflow.band_analysis import BandDataError
        from vasp_agent.workflow.band_plot import DEFAULT_YLIM, ReceiptMismatch, replot_folder
        folder = self.job(job_name) if server in (None, 'local') else self.latest_download(server, job_name)[0]
        ylim = (DEFAULT_YLIM[0] if y_min is None else float(y_min), DEFAULT_YLIM[1] if y_max is None else float(y_max))
        if not ylim[0] < ylim[1]:
            raise ValueError('y_min 必须小于 y_max。')
        try:
            result = replot_folder(folder, ylim)
        except FileNotFoundError as exc:
            raise core.ToolError('FILE_NOT_FOUND', str(exc)) from None
        except ReceiptMismatch as exc:
            raise core.ToolError('INPUT_HASH_MISMATCH', str(exc)) from None
        except BandDataError as exc:
            raise core.ToolError('BAND_DATA_INVALID', str(exc)) from None
        for key in ('png', 'pdf', 'record'):
            result[key] = Path(result[key]).relative_to(self.workspace).as_posix()
        return result

    def analyze_band_results(self, server, job_name, table_offset=0, table_limit=0):
        """Fixed, read-only band analysis so routine questions need no model-written script."""
        from vasp_agent.workflow.band_analysis import MAX_BANDS_CSV_BYTES, BandDataError, analyze_bands
        sources = {}
        if server in (None, 'local'):
            folder = self.job(job_name)
            def read(name):
                target = folder / name
                return target.read_bytes() if target.is_file() else None
            origin = {'kind': 'local', 'directory': folder.relative_to(self.workspace).as_posix(),
                      'receipt_verified': False}
        else:
            folder, receipt = self.latest_download(server, job_name)
            def read(name):
                target = folder / name
                if not target.is_file():
                    return None
                entry = receipt.get(name)
                if not isinstance(entry, dict):
                    raise core.ToolError('INPUT_HASH_MISMATCH', f'{name} 不在下载收据中，拒绝分析。')
                data = target.read_bytes()
                if hashlib.sha256(data).hexdigest() != entry.get('sha256'):
                    raise core.ToolError('INPUT_HASH_MISMATCH', f'{name} 与下载收据的哈希不符，拒绝分析。')
                return data
            origin = {'kind': 'download', 'directory': folder.relative_to(self.workspace).as_posix(),
                      'receipt_verified': True}
        if (folder / 'bands.csv').is_file() and (folder / 'bands.csv').stat().st_size > MAX_BANDS_CSV_BYTES:
            raise core.ToolError('EDIT_TOO_LARGE', 'bands.csv 过大，拒绝分析。')
        raw = read('bands.csv')
        if raw is None:
            raise core.ToolError('FILE_NOT_FOUND', '没有 bands.csv；能带阶段完成并下载后再分析。')
        sources['bands.csv'] = hashlib.sha256(raw).hexdigest()
        documents = {}
        for name in ('scf-summary.json', 'report.json'):
            data = read(name)
            if data is not None:
                sources[name] = hashlib.sha256(data).hexdigest()
                documents[name] = json.loads(data.decode('utf-8'))
        try:
            analysis = analyze_bands(raw.decode('utf-8-sig'), documents.get('scf-summary.json'),
                                     documents.get('report.json'), table_offset, table_limit)
        except BandDataError as exc:
            raise core.ToolError('BAND_DATA_INVALID', str(exc)) from None
        return {'source': {**origin, 'files_sha256': sources}, **analysis,
                'note': '确定性只读分析，未执行脚本、未写文件；下载完整性不等于科学验收。'}

    def editor(self):
        from vasp_agent.editing.workspace_editor import WorkspaceEditor
        return WorkspaceEditor(self.workspace)

    def list_workspace_files(self, path='.', offset=0):
        return self.editor().list(path, offset)

    def read_workspace_file(self, path, start_line=1, offset=0):
        return self.editor().read(path, start_line, offset)

    def search_workspace_text(self, query, path='.', offset=0):
        return self.editor().search(query, path, offset)

    def edit_workspace_file(self, path, operation, expected_sha256, content, patch, reason):
        return self.editor().edit(path, operation, expected_sha256, content, patch, reason,
                                  getattr(self, '_edit_request_id', None))

    def get_workspace_edit(self, change_id, offset=0):
        return self.editor().get(change_id, offset)

    def revert_workspace_edit(self, change_id):
        return self.editor().revert(change_id)

    def loaded_guides(self):
        """Guide text in this conversation's live context (the Agent supplies its history)."""
        source = getattr(self, 'guide_history', None)
        return guides.loaded(source() if source is not None else [])

    def read_stage_guide(self, guide, page):
        return guides.read(guide, page, self.loaded_guides())

    def stage_guides(self, data):
        """Pointers to the guide the current phase's definition declares (outside the snapshot)."""
        try:
            guide = stage_definitions.definition(data['inputs']['config'], data['phase'])['guide']
        except (KeyError, TypeError, ValueError):
            return data
        return guides.attach(data, [guide], self.loaded_guides())
