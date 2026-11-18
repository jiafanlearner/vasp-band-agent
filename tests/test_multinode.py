import json
import pytest
from vasp_agent.workflow.full_workflow import execution_command
from vasp_agent.permissions.submission_dialog import validate_resources
from vasp_agent.remote.remote_worker import configure_resources, resource_context, check_resources

RES = dict(parallel_tests=3, cores_per_test=16, nodes=2, partition='compute', walltime='04:00:00')
LIMITS = dict(nodes=2, cores=48, partition='compute', walltime='04:00:00')


def test_multinode_step_layout(monkeypatch):
    monkeypatch.setenv('SLURM_JOB_ID', 'test')
    monkeypatch.setenv('SLURM_NTASKS', '48')
    monkeypatch.setenv('SLURM_JOB_NUM_NODES', '2')
    command = execution_command(RES, ['srun', 'vasp_std'])
    assert '--ntasks=16' in command and '--nodes=2' in command and '--ntasks-per-node=8' in command
    assert '--exact' in command and '--exclusive' in command
    assert '--nodes=2' in execution_command({**RES, 'parallel_tests': 1}, ['srun', 'vasp_std'])
    monkeypatch.setenv('SLURM_NTASKS', '32')
    with pytest.raises(ValueError, match='总核数不足'):
        execution_command(RES, ['srun', 'vasp_std'])
    monkeypatch.setenv('SLURM_NTASKS', '48')
    monkeypatch.setenv('SLURM_JOB_NUM_NODES', '1')
    with pytest.raises(ValueError, match='节点数'):
        execution_command(RES, ['srun', 'vasp_std'])


@pytest.mark.parametrize('change', [{'nodes': 0}, {'nodes': 3}, {'cores_per_test': 15}, {'nodes': 4}])
def test_invalid_or_unauthorized_layout(change):
    with pytest.raises(ValueError):
        validate_resources({**RES, **change}, 'convergence', LIMITS)


def test_multinode_script_and_old_engine_guard(tmp_path, monkeypatch):
    import vasp_agent.workflow.vasp_band_agent as core
    import vasp_agent.workflow.full_workflow as full_workflow
    monkeypatch.setattr(core, 'verify', lambda _: None)
    (tmp_path / 'config.json').write_text(json.dumps({**RES, 'nodes': 1}))
    (tmp_path / 'manifest.json').write_text('{"hashes":{}}')
    (tmp_path / 'submit.slurm').write_text('#!/bin/bash\n#SBATCH --partition=compute\n#SBATCH --nodes=1\n#SBATCH --ntasks=48\n#SBATCH --time=04:00:00\nexport OMP_NUM_THREADS=1\nsrun vasp_std\n')
    before = resource_context(tmp_path, 'convergence')
    monkeypatch.setattr(full_workflow, 'RESOURCE_LAYOUT_VERSION', 1)
    with pytest.raises(ValueError, match='旧计算引擎'):
        configure_resources(tmp_path, 'convergence', RES, before['signature'], LIMITS)
    monkeypatch.setattr(full_workflow, 'RESOURCE_LAYOUT_VERSION', 2)
    configure_resources(tmp_path, 'convergence', RES, before['signature'], LIMITS)
    script = (tmp_path / 'submit.slurm').read_text()
    assert '--nodes=2' in script and '--ntasks-per-node=24' in script
    assert json.loads((tmp_path / 'config.json').read_text())['nodes'] == 2
    check_resources(script, LIMITS)
    with pytest.raises(ValueError, match='不一致'):
        check_resources(script.replace('--ntasks-per-node=24', '--ntasks-per-node=32'), LIMITS)
