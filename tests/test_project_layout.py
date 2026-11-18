"""Source layout and remote payload smoke checks; no SSH or scheduler calls."""
import base64
import importlib
import json
import os
import site
from pathlib import Path
import subprocess
import sys

import pytest

from vasp_agent.remote.bundle import engine_sources, stdin_program
from vasp_agent.remote.remote_worker import ENGINE_MODULES, SOURCE_FILES
from vasp_agent.remote import remote_worker

ROOT = Path(__file__).resolve().parents[1]


def unpack(directory):
    for name, body in engine_sources().items():
        path = directory / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(base64.b64decode(body))


def test_all_application_modules_import_from_package():
    for path in (ROOT / 'src/vasp_agent').rglob('*.py'):
        if path.name == '__main__.py':
            continue
        relative = path.relative_to(ROOT / 'src').with_suffix('')
        module = '.'.join(relative.parts).removesuffix('.__init__')
        imported = importlib.import_module(module)
        assert Path(imported.__file__).resolve().is_relative_to(ROOT / 'src')


@pytest.mark.parametrize('entry', ['ai_agent.py', 'vasp_band_agent.py'])
def test_original_launchers_work_from_outside_checkout(tmp_path, entry):
    result = subprocess.run([sys.executable, '-X', 'utf8', str(ROOT / entry), '--help'], cwd=tmp_path,
                            capture_output=True, text=True, encoding='utf-8', timeout=60)
    assert result.returncode == 0, result.stderr
    assert 'usage:' in result.stdout


def test_engine_bundle_is_closed_and_runs_without_chat_package(tmp_path):
    payload = engine_sources()
    assert set(payload) == set(SOURCE_FILES)
    assert not any('/editing/' in x or '/chat/' in x or '/models/' in x or '/permissions/' in x for x in payload)
    unpack(tmp_path)
    env = {k: v for k, v in os.environ.items() if k != 'PYTHONPATH'}
    modules = list(ENGINE_MODULES.values())
    code = ('import sys,importlib,json,site; site.addsitedir(' + repr(site.getusersitepackages()) + '); sys.path.insert(0, ' + repr(str(tmp_path)) + '); '
            'mods=[importlib.import_module(x) for x in ' + repr(modules) + ']; '
            'print(json.dumps([x.__file__ for x in mods]))')
    result = subprocess.run([sys.executable, '-I', '-c', code], cwd=tmp_path, env=env,
                            capture_output=True, text=True, timeout=60)
    assert result.returncode == 0, result.stderr
    assert all(Path(p).is_relative_to(tmp_path) for p in json.loads(result.stdout))
    run = subprocess.run([sys.executable, '-X', 'utf8', str(tmp_path / 'vasp_band_agent.py'), '--help'],
                         cwd=tmp_path, env=env, capture_output=True, text=True, encoding='utf-8', timeout=60)
    assert run.returncode == 0, run.stderr


def test_engine_bundle_draws_the_band_plot(tmp_path):
    # export_bands imports band_plot lazily; a release missing it fails only at the end of a run.
    unpack(tmp_path)
    env = {k: v for k, v in os.environ.items() if k != 'PYTHONPATH'}
    code = ('import sys,site; site.addsitedir(' + repr(site.getusersitepackages()) + '); sys.path.insert(0, ' + repr(str(tmp_path)) + '); '
            'from pymatgen.core import Lattice; from pymatgen.electronic_structure.core import Spin; '
            'from pymatgen.electronic_structure.bandstructure import BandStructureSymmLine; import numpy as np; '
            'import vasp_agent.workflow.vasp_band_agent as core; '
            "bs = BandStructureSymmLine([[0,0,0],[.25,0,0],[.5,0,0]], {Spin.up: np.array([[-1.,-.5,-1.],[1.,1.5,2.]])}, "
            "Lattice.cubic(1), 0.2, labels_dict={'G':[0,0,0],'X':[.5,0,0]}); "
            "out = __import__('pathlib').Path(" + repr(str(tmp_path / 'out')) + "); out.mkdir(); "
            "core.export_bands(bs, out, {'formula': 'T'}); "
            "import vasp_agent.workflow.band_plot as bp; assert bp.__file__.startswith(" + repr(str(tmp_path)) + ")")
    result = subprocess.run([sys.executable, '-I', '-c', code], cwd=tmp_path, env=env,
                            capture_output=True, text=True, timeout=120)
    assert result.returncode == 0, result.stderr
    assert (tmp_path / 'out/bands.png').stat().st_size > 1000


def test_stdin_worker_loads_legacy_release_instead_of_new_package(tmp_path):
    # A pre-refactor release has flat modules and no vasp_agent package.
    release = tmp_path / 'legacy-release'
    release.mkdir()
    (release / 'input_review.py').write_text('def context(root):\n    return {"legacy_release": True}\n')
    task = tmp_path / 'jobs/old'
    task.mkdir(parents=True)
    (task / 'remote-job.json').write_text(json.dumps({'release': str(release)}))
    payload = {'config': {'root': str(tmp_path)}, 'action': 'review_context', 'job_name': 'old'}
    program = stdin_program(json.dumps(payload))
    result = subprocess.run([sys.executable, '-I', '-X', 'utf8', '-'], input=program, cwd=tmp_path,
                            capture_output=True, text=True, encoding='utf-8', timeout=30)
    assert result.returncode == 0, result.stderr + result.stdout
    assert json.loads(result.stdout)['result'] == {'legacy_release': True}


def test_deploy_writes_package_allowlist_only(tmp_path, monkeypatch):
    monkeypatch.setattr(remote_worker.subprocess, 'run', lambda *args, **kwargs:
                        type('Result', (), {'returncode': 0})())
    result = remote_worker.dispatch({'action': 'deploy', 'config': {'root': str(tmp_path), 'python': 'fake'},
                                     'sources': engine_sources()})
    release = Path(result['release'])
    assert {p.relative_to(release).as_posix() for p in release.rglob('*') if p.is_file()} == set(SOURCE_FILES)
    payload = engine_sources()
    payload['../../unexpected.py'] = base64.b64encode(b'bad').decode()
    with pytest.raises(ValueError, match='白名单'):
        remote_worker.dispatch({'action': 'deploy', 'config': {'root': str(tmp_path), 'python': 'fake'}, 'sources': payload})


def test_new_release_prepares_preview_and_stdin_worker_reads_it(tmp_path):
    release = tmp_path / 'release'
    unpack(release)
    folder = tmp_path / 'jobs/si'
    prepared = subprocess.run([sys.executable, '-X', 'utf8', str(release / 'vasp_band_agent.py'),
        'prepare', str(ROOT / 'examples/Si.POSCAR'), str(folder)], cwd=tmp_path,
        capture_output=True, text=True, encoding='utf-8', timeout=60)
    assert prepared.returncode == 0, prepared.stderr
    assert json.loads((folder / 'manifest.json').read_text())['ready'] is False
    (folder / 'remote-job.json').write_text(json.dumps({'release': str(release)}))
    payload = {'config': {'root': str(tmp_path)}, 'action': 'review_context', 'job_name': 'si'}
    program = stdin_program(json.dumps(payload))
    result = subprocess.run([sys.executable, '-X', 'utf8', '-'], input=program, cwd=tmp_path,
                            capture_output=True, text=True, encoding='utf-8', timeout=60)
    assert result.returncode == 0, result.stderr + result.stdout
    output = json.loads(result.stdout)
    assert output['ok'] and output['result']['snapshot']
    assert not list(folder.rglob('*submission*.json'))
