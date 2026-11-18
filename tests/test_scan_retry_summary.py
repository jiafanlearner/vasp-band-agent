import pytest

from vasp_agent.ui.scan_retry_summary import ACTIVE_SCANS, planned_tests, scan_retry_summary
from vasp_agent.workflow.full_workflow import convergence_tests


@pytest.mark.parametrize('mode,fixed_encut', [('both', None), ('encut', None), ('mesh', None), ('mesh', 598.0)])
def test_summary_count_matches_frozen_execution_order(mode, fixed_encut):
    config = {'encuts': [520.0, 598.0, 676.0], 'meshes': [[4, 4, 4], [5, 5, 5], [6, 6, 6]],
              'active_scans': ACTIVE_SCANS[mode], 'fixed_encut': fixed_encut}
    assert planned_tests(mode, fixed_encut) == len(convergence_tests(config))
    evidence = {'parameters': {'encut': {'status': 'passed', 'values': config['encuts']},
                               'mesh': {'status': 'passed', 'values': config['meshes']}}}
    text = scan_retry_summary(None, 'src', 'dst', mode, evidence, None, None, fixed_encut)
    assert f'执行 {len(convergence_tests(config))} 项' in text
