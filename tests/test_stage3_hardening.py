"""Stage 3 hardening regressions: thread-local digest memo, zero workflow reverse imports, and facades."""
import ast
import threading
from pathlib import Path

import pytest

from vasp_agent.models import context_compaction
from vasp_agent.planning import parameter_plans
from vasp_agent.workflow import band_path, convergence_array, full_workflow, vasp_band_agent as core

ROOT = Path(__file__).resolve().parents[1]


def test_digest_memo_is_thread_local_and_isolated(tmp_path):
    """Concurrent threads entering digest_memo() never see or overwrite each other's memo."""
    file_a = tmp_path / 'a.json'
    file_b = tmp_path / 'b.json'
    file_a.write_text('{"a": 1}', encoding='utf-8')
    file_b.write_text('{"b": 2}', encoding='utf-8')

    barrier = threading.Barrier(2)
    observed = {}
    errors = []

    def worker_a():
        try:
            with core.digest_memo() as memo_a:
                core.digest(file_a)
                observed['a_inside'] = memo_a
                observed['a_attr'] = core._digests
                barrier.wait(timeout=5)
                # Thread B saves a file (calling forget_digests()) while Thread A holds memo_a.
                barrier.wait(timeout=5)
                observed['a_after_b_save'] = len(memo_a)
                observed['a_still_same'] = core._digests is memo_a
            observed['a_outside'] = core._digests
        except Exception as exc:
            errors.append(exc)

    def worker_b():
        try:
            with core.digest_memo() as memo_b:
                core.digest(file_b)
                observed['b_inside'] = memo_b
                observed['b_attr'] = core._digests
                barrier.wait(timeout=5)
                core.save(tmp_path / 'written.json', {'ok': True})
                observed['b_after_own_save'] = len(memo_b)
                barrier.wait(timeout=5)
            observed['b_outside'] = core._digests
        except Exception as exc:
            errors.append(exc)

    t1 = threading.Thread(target=worker_a)
    t2 = threading.Thread(target=worker_b)
    t1.start()
    t2.start()
    t1.join(timeout=10)
    t2.join(timeout=10)

    assert not errors
    assert observed['a_inside'] is observed['a_attr']
    assert observed['b_inside'] is observed['b_attr']
    assert observed['a_inside'] is not observed['b_inside']
    assert observed['b_after_own_save'] == 0
    assert observed['a_after_b_save'] == 1
    assert observed['a_still_same'] is True
    assert observed['a_outside'] is None
    assert observed['b_outside'] is None
    assert core._digests is None


def test_workflow_package_has_zero_upward_imports():
    """Layer 1 (workflow/) must never import Layer 2+ packages (remote, chat, models, etc.)."""
    forbidden = ('remote', 'chat', 'models', 'permissions', 'planning', 'editing', 'monitoring', 'ui', 'guides')
    violations = []
    for path in sorted((ROOT / 'src/vasp_agent/workflow').rglob('*.py')):
        tree = ast.parse(path.read_text(encoding='utf-8'))
        for node in ast.walk(tree):
            modules = []
            if isinstance(node, ast.Import):
                modules = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom) and node.module:
                modules = [node.module]
            for mod in modules:
                if any(mod == f'vasp_agent.{pkg}' or mod.startswith(f'vasp_agent.{pkg}.') for pkg in forbidden):
                    violations.append(f'{path.name}:{node.lineno} -> {mod}')
    assert violations == []


def test_vasp_band_agent_chat_subcommand_uses_injected_handler(monkeypatch):
    called = []
    monkeypatch.setattr(core.sys, 'argv', ['vasp-band-agent', 'chat', '--help'])
    core.main(chat_handler=lambda: called.append(list(core.sys.argv)))
    assert called == [['vasp-band-agent', '--help']]

    monkeypatch.setattr(core.sys, 'argv', ['vasp-band-agent', 'chat'])
    with pytest.raises(SystemExit):
        core.main(chat_handler=None)


def test_stage3_split_facades_preserve_public_exports():
    for name in ('NOTICE', 'SUMMARY_FIELDS', 'CompactionError', 'SummaryTruncated',
                 'normalize_summary', 'mechanical_summary', 'validate_summary', 'ContextCompactor'):
        assert hasattr(context_compaction, name), name
    for name in ('propose', 'prepare', 'preview_defaults', 'select', 'select_stage_parameters',
                 'select_environment', 'comparison', 'menu', 'list_plans'):
        assert hasattr(parameter_plans, name), name
    for name in ('ToolError', 'save', 'digest', 'digest_memo', 'verify', 'execute',
                 'validate_band_data', 'export_bands', 'main'):
        assert hasattr(core, name), name
    for name in ('ARRAY_EXECUTION_VERSION', 'collect_array', 'finalize_array', 'run_array_test',
                 'execution_command', '_validate_receipt', '_scheduler_check'):
        assert hasattr(convergence_array, name), name
    for name in ('parse', 'validate', 'render', 'record_for', 'kpoints', 'review_context',
                 'prepare_bands_path', 'candidates', 'candidates_for_task', 'candidates_for_download'):
        assert hasattr(band_path, name), name
    for name in ('prepare_full', 'run_full', 'run_stage', 'analyze_full', 'accept_convergence',
                 'execution_tags', 'execution_preview', 'incar_matches', 'STAGE_RUNNERS', 'RESOURCE_LAYOUT_VERSION'):
        assert hasattr(full_workflow, name), name
