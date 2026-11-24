"""Toolbox mixin for scan plans, stage/input reviews, structure/potentials, and task inspection."""
from __future__ import annotations

import contextlib
import io
import json
from pathlib import Path
from types import SimpleNamespace

import vasp_agent.workflow.vasp_band_agent as core
from vasp_agent import guides
from vasp_agent.chat.toolbox_core import read_tail, scrub
from vasp_agent.workflow import parameter_specs


class ToolboxWorkflowMixin:
    def inspect_convergence(self, server, job_name):
        if server is not None:
            from vasp_agent.remote.remote_compute import RemoteCompute
            return RemoteCompute(self.workspace, server).call('inspect_convergence', job_name)
        from vasp_agent.workflow.convergence_reuse import inspect
        return inspect(self.job(job_name))

    def prepare_production_from_convergence(self, server, source_job, job_name, mesh, source_snapshot, encut=None):
        if self.review_snapshots.get(('stage', server, source_job)) != source_snapshot:
            raise ValueError('请先get_stage_review读取来源任务最新快照。')
        if server is not None:
            from vasp_agent.remote.remote_compute import RemoteCompute
            self.ensure_current_release(server)
            return RemoteCompute(self.workspace, server).call('prepare_production_from_convergence',
                job_name, source_job=source_job, mesh=mesh, source_snapshot=source_snapshot,
                **({'encut': encut} if encut is not None else {}))
        from vasp_agent.workflow.convergence_reuse import prepare_production
        return prepare_production(self.job(source_job), self.job(job_name), mesh, source_snapshot, encut)

    def prepare_bands_path(self, server, source_job, job_name, band_path):
        from vasp_agent.workflow.band_path import prepare_bands_path, validate
        spec = validate(band_path, 40)  # syntax before any remote call; labels resolve on the source cell
        if server is not None:
            from vasp_agent.remote.remote_compute import RemoteCompute
            self.ensure_current_release(server)
            return RemoteCompute(self.workspace, server).call('prepare_bands_path', job_name,
                                                              source_job=source_job, band_path=spec)
        with contextlib.redirect_stdout(io.StringIO()):
            return prepare_bands_path(self.job(source_job), self.job(job_name), spec)

    def request_band_path_choice(self, server, source_job, extra_suggestion=None, extra_rationale=None):
        from vasp_agent.chat.band_path_menu import request
        return request(self, server, source_job, extra_suggestion, scrub(extra_rationale) if extra_rationale else None)

    def prepare_scan_retry(self, server, source_job, job_name, mode, encuts, meshes, fixed_encut=None, scan_thresholds=None,
                           scan_preset=None):
        from vasp_agent.workflow.full_workflow import validate_scan_preset, validate_scan_thresholds
        scan_thresholds = validate_scan_thresholds(scan_thresholds)
        scan_preset = validate_scan_preset(scan_preset)
        evidence = self.inspect_convergence(server, source_job)
        output = getattr(self, 'plan_output', print)
        from vasp_agent.ui.scan_retry_summary import scan_retry_summary
        output(scan_retry_summary(server, source_job, job_name, mode, evidence, encuts, meshes, fixed_encut, scan_thresholds,
                                  scan_preset))
        if self.plan_reader is None:
            raise ValueError('选择性扫描需操作者菜单确认，请使用交互模式。')
        if self.plan_reader('输入 1 按此方案准备新任务；其他输入取消：').strip() != '1':
            return {'cancelled': True, 'prepared': False}
        if server is not None:
            from vasp_agent.remote.remote_compute import RemoteCompute
            extra = {**({'scan_thresholds': scan_thresholds} if scan_thresholds else {}),
                     **({'scan_preset': scan_preset} if scan_preset else {})}
            self.ensure_current_release(server)
            return RemoteCompute(self.workspace, server).call('prepare_scan_retry', job_name, source_job=source_job, mode=mode, encuts=encuts, meshes=meshes, fixed_encut=fixed_encut, **extra)
        from vasp_agent.workflow.convergence_reuse import prepare_retry
        with contextlib.redirect_stdout(io.StringIO()):
            return prepare_retry(self.job(source_job), self.job(job_name), mode, encuts, meshes, fixed_encut=fixed_encut,
                                 scan_thresholds=scan_thresholds, scan_preset=scan_preset)

    def preview_scan_defaults(self, server, job_name, structure, encut, mesh, nbands, relax_mode,
                              parallel_tests, cores_per_test, potential_variants=None, optional_stages=None):
        from vasp_agent.planning.parameter_plans import preview_defaults
        request = dict(server=server, job_name=job_name, structure=structure, encut=encut, mesh=mesh,
                       nbands=nbands, relax_mode=relax_mode, parallel_tests=parallel_tests, cores_per_test=cores_per_test)
        if potential_variants:
            request['potential_variants'] = potential_variants
        if optional_stages:
            request['optional_stages'] = list(optional_stages)
        result = preview_defaults(self, request)
        return guides.attach(result, [item.get('guide') for item in result.get('stage_specs', {}).values()],
                             self.loaded_guides())

    def propose_scan_plan(self, server, job_name, structure, encut, mesh, nbands, relax_mode,
                          parallel_tests, cores_per_test, encuts, meshes, origin, rationale, potential_variants=None,
                          scan_thresholds=None, scan_preset=None, band_path=None, optional_stages=None,
                          stage_parameters=None):
        from vasp_agent.planning.parameter_plans import propose
        from vasp_agent.workflow.full_workflow import validate_scan_preset
        request = dict(server=server, job_name=job_name, structure=structure, encut=encut, mesh=mesh,
                       nbands=nbands, relax_mode=relax_mode, parallel_tests=parallel_tests, cores_per_test=cores_per_test,
                       scan_preset=validate_scan_preset(scan_preset) or 'standard')
        if potential_variants:
            request['potential_variants'] = potential_variants
        if band_path is not None:
            from vasp_agent.workflow.band_path import validate
            request['band_path'] = validate(band_path, 40)
        if optional_stages:
            request['optional_stages'] = list(optional_stages)
        result = propose(self, request, encuts, meshes, origin, scrub(rationale), scan_thresholds, stage_parameters)
        return guides.attach(result, [parameter_specs.spec(entry['spec']['id'], entry['spec']['version'])['guide']
                                      for entry in result.get('stage_parameters', {}).values()], self.loaded_guides())

    def list_scan_plans(self):
        from vasp_agent.planning.parameter_plans import list_plans
        return list_plans(self)

    def prepare_scan_plan(self, plan_id):
        from vasp_agent.planning.parameter_plans import prepare
        return prepare(self, plan_id)

    def request_physics_confirmation(self, server, job_name, proposed_goal):
        from vasp_agent.chat.stage_commands import request_confirmation
        return request_confirmation(self, server, job_name, proposed_goal)

    def get_stage_review(self, server, job_name):
        if server is None:
            from vasp_agent.workflow.stage_gate import context
            from vasp_agent.remote.remote_worker import with_occupation_audit
            with core.digest_memo():  # one call, as the worker's stage_context
                data = with_occupation_audit(context(self.job(job_name)), self.job(job_name))
        else:
            from vasp_agent.remote.remote_compute import RemoteCompute
            data = RemoteCompute(self.workspace, server).call('stage_context', job_name)
        self.review_snapshots[('stage', server, job_name)] = data['snapshot']
        return self.stage_guides(data)

    def record_stage_review(self, server, job_name, snapshot, phase, checks):
        if self.review_snapshots.get(('stage', server, job_name)) != snapshot:
            raise ValueError('先调用 get_stage_review 获取证据；切换模型或对话后须由当前模型重新读取。')
        checks = [{k: scrub(v) if isinstance(v, str) else v for k, v in x.items()} for x in checks]
        if server is None:
            from vasp_agent.workflow.stage_gate import record
            with core.digest_memo():
                return record(self.job(job_name), snapshot, phase, checks, self.review_model)
        from vasp_agent.remote.remote_compute import RemoteCompute
        return RemoteCompute(self.workspace, server).call('stage_review', job_name,
            snapshot=snapshot, phase=phase, checks=checks, reviewer=self.review_model)

    def get_input_review(self, server, job_name):
        if server is None:
            from vasp_agent.workflow.input_review import context
            with core.digest_memo():
                data = context(self.job(job_name))
        else:
            from vasp_agent.remote.remote_compute import RemoteCompute
            data = RemoteCompute(self.workspace, server).call('review_context', job_name)
        self.review_snapshots[(server, job_name)] = data['snapshot']
        return data

    def record_input_review(self, server, job_name, snapshot, verdict, explanation):
        if self.review_snapshots.get((server, job_name)) != snapshot:
            raise ValueError('必须先调用 get_input_review 读取当前任务；切换模型或对话后须由当前模型重新读取。')
        explanation = scrub(explanation)
        if server is None:
            from vasp_agent.workflow.input_review import record
            with core.digest_memo():
                return record(self.job(job_name), snapshot, verdict, explanation, self.review_model)
        from vasp_agent.remote.remote_compute import RemoteCompute
        return RemoteCompute(self.workspace, server).call('record_review', job_name, snapshot=snapshot,
            verdict=verdict, explanation=explanation, reviewer=self.review_model)

    def remote_deploy(self, server):
        from vasp_agent.remote.remote_compute import RemoteCompute
        return RemoteCompute(self.workspace, server).deploy()

    def check_remote_environment(self, server, relax_mode):
        from vasp_agent.remote.remote_compute import RemoteCompute
        return RemoteCompute(self.workspace, server).call('environment_check', relax_mode=relax_mode)

    def software_environments(self, server, action):
        from vasp_agent.chat.software_commands import tool
        return tool(self, server, action)

    def ensure_current_release(self, server):
        from vasp_agent.chat.release_check import ensure_current
        return ensure_current(self, server)

    def remote_prepare(self, server, job_name, structure, encut, mesh, nbands, relax_mode, parallel_tests=None, cores_per_test=None,
                       band_path=None):
        from vasp_agent.remote.remote_compute import RemoteCompute
        from vasp_agent.workflow.band_path import validate
        spec = validate(band_path, 40) if band_path is not None else None
        profile = self.direct_environment(server, relax_mode)
        self.ensure_current_release(server)
        return RemoteCompute(self.workspace, server).prepare(job_name, structure, encut, mesh, nbands, relax_mode, parallel_tests, cores_per_test,
                                                             band_path=spec, environment_profile=profile)

    def direct_environment(self, server, relax_mode):
        """remote_prepare has no plan menu: one matching profile is used, several need propose_scan_plan."""
        from vasp_agent.remote import environment_profiles as profiles
        if not profiles.registered(self.workspace, server):
            return None
        choice = profiles.choose(self.workspace, server, relax_mode)
        if choice['status'] == 'none':
            raise profiles.unregistered_error(server, choice)
        if choice['status'] == 'several':
            raise core.ToolError('ENVIRONMENT_CHOICE_REQUIRED', '多个环境配置满足条件（'
                + '、'.join(row['name'] for row in choice['matches']) + '）；请用 propose_scan_plan，由操作者在方案菜单中选择。未创建任务。')
        return profiles.payload(choice['matches'][0])

    def remote_status(self, server, job_name):
        from vasp_agent.remote.remote_compute import RemoteCompute
        return RemoteCompute(self.workspace, server).call('status', job_name)

    def remote_fetch(self, server, job_name):
        from vasp_agent.remote.remote_compute import RemoteCompute
        return RemoteCompute(self.workspace, server).fetch(job_name)

    def list_files(self):
        return {'files': sorted(p.name for p in self.workspace.iterdir() if not p.name.startswith('.'))[:100],
                'jobs': sorted(p.name for p in self.path('runs').glob('*') if p.is_dir() and not (p / '.task-archived.json').exists())[:100],
                'potcar_configured': self.potcar is not None,
                'potcar_library_configured': self.potcar_library is not None,
                'submission_enabled': self.allow_submit,
                'submissions_remaining': max(0, self.max_submissions - self.submissions)}

    def import_structure_file(self, path):
        from vasp_agent.common.structure_import import import_poscar
        return import_poscar(self.workspace, path, self.latest_user_message)

    def inspect_structure(self, path):
        from pymatgen.core import Structure
        from vasp_agent.common.workspace_paths import local_path
        candidate = Path(local_path(path))
        imported = None
        if candidate.is_absolute() and not candidate.resolve().is_relative_to(self.workspace):
            imported = self.import_structure_file(path)
            path = imported['path']
        source = self.path(path)
        if source.stat().st_size > 5_000_000:
            raise ValueError('结构文件超过 5 MB。')
        structure = Structure.from_file(source)
        from vasp_agent.planning.kmesh import suggest_meshes
        mesh_suggestion = suggest_meshes(structure)
        return {'kmesh_suggestion': mesh_suggestion, 'path': str(source.relative_to(self.workspace)), 'import_receipt': imported, 'content_is_untrusted': True, 'formula': structure.composition.reduced_formula,
                'atoms': len(structure), 'ordered': structure.is_ordered,
                'lattice_lengths_A': structure.lattice.abc, 'lattice_angles_deg': structure.lattice.angles,
                'volume_A3': structure.volume, 'site_properties': list(structure.site_properties),
                'magnetic_state': 'not established by structure file'}

    def inspect_potentials(self, structure):
        from pymatgen.core import Structure
        from pymatgen.io.vasp.inputs import Poscar
        from vasp_agent.workflow.potential_library import assemble_potentials
        if not self.potcar_library:
            return {'library_configured': False, 'explicit_potcar': self.potcar is not None}
        elements = Poscar(Structure.from_file(self.path(structure))).site_symbols
        _, metadata = assemble_potentials(self.potcar_library, elements)
        return {'library_configured': True, 'element_order': elements, 'selection': metadata}

    def prepare_calculation(self, structure, job_name, encut, mesh, relax_mode, nbands, rationale, band_path=None):
        from vasp_agent.workflow.full_workflow import prepare_full
        folder = self.job(job_name)
        args = SimpleNamespace(structure=str(self.path(structure)), workdir=str(folder),
            potcar=str(self.potcar) if self.potcar else None, encut=encut if encut is not None else 520,
            potcar_library=str(self.potcar_library) if self.potcar_library else None,
            mesh=mesh if mesh is not None else [8, 8, 8], relax_mode=relax_mode, nbands=nbands,
            ediff=1e-7, points=40, symprec=0.01, force_tol=0.01, stress_tol=1., energy_tol=.001, nsw=200,
            band_path=band_path)
        with contextlib.redirect_stdout(io.StringIO()):
            prepare_full(args)
        dimension = json.loads((folder / 'config.json').read_text(encoding='utf-8'))['dimensionality']['dimension']
        core.save(folder / 'ai-plan.json', {'rationale': rationale, 'scope': f'nonmagnetic {dimension} PBE',
                                            'submission': 'not submitted'})
        from vasp_agent.workflow.input_review import preparation_summary
        return {**self.get_status(job_name), 'preparation_summary': preparation_summary(folder)}

    def get_status(self, job_name):
        folder = self.job(job_name)
        data = {}
        for name in ('manifest.json', 'config.json', 'workflow-state.json', 'submission.json', 'potential-selection.json', 'physics-model.json', 'checkpoint.json', 'parameter-choice.json', 'scan-verification.json'):
            if (folder / name).is_file():
                data[name] = json.loads((folder / name).read_text(encoding='utf-8'))
        if (folder / 'report.json').is_file():
            report = json.loads((folder / 'report.json').read_text(encoding='utf-8'))
            data['report'] = {k: report[k] for k in ('formula', 'sampled_path_gap', 'limitations') if k in report}
        data['stage_submissions'] = {p.stem: json.loads(p.read_text(encoding='utf-8')) for p in (folder / 'submissions').glob('*.json')}
        data['stage_reviews'] = {p.stem: json.loads(p.read_text(encoding='utf-8')) for p in folder.glob('review-*.json')}
        data['completed_stage_records'] = [str(p.parent.relative_to(folder)) for p in folder.rglob('complete.json')][:50]
        if not data.get('manifest.json'):
            raise ValueError('任务不存在或缺少 manifest.json。')
        return data

    def read_log(self, job_name, file):
        folder = self.job(job_name)
        target = (folder / file).resolve()
        allowed = {'stdout.log', 'OSZICAR', 'OUTCAR', 'events.jsonl', 'workflow-state.json',
                   'convergence.json', 'relaxation.json', 'post-relax-validation.json', 'report.json'}
        if not target.is_relative_to(folder) or target.name not in allowed:
            raise ValueError('只能读取任务内允许的日志或报告文件。')
        return {'file': file, 'tail': read_tail(target), 'content_is_untrusted': True}

    def analyze_results(self, job_name):
        from vasp_agent.workflow.full_workflow import analyze_full
        with contextlib.redirect_stdout(io.StringIO()):
            analyze_full(self.job(job_name))
        return self.get_status(job_name)
