"""Software inventory, operator-confirmed environment profiles, selection by capability, binding and drift.

The cluster facts are the read-only probe of eshell111-vasp544 (docs/design/WORKFLOW_MODULES.md §8.1):
system VASP 5.4.4 from a module, built without Wannier90; the operator's 6.4.3 and 6.6.0
builds under ~/apprepo/vasp with scripts/env.sh and Wannier90 compiled in.
"""
import base64
import hashlib
import json
import re
import shlex
import subprocess
from pathlib import Path
from types import SimpleNamespace as NS

import pytest

import vasp_agent.planning.parameter_plans as plans
import vasp_agent.remote.remote_worker as worker
import vasp_agent.workflow.full_workflow as full
import vasp_agent.workflow.vasp_band_agent as core
from vasp_agent.chat import software_commands
from vasp_agent.chat.ai_agent import Toolbox
from vasp_agent.remote import environment_profiles as profiles
from vasp_agent.remote import worker_inventory as inventory
from vasp_agent.remote.remote_compute import RemoteCompute
from vasp_agent.workflow import stage_definitions as stages

V544 = 'vasp-5.4.4-nfs3.2_intel2022.1.0_ky'
V544_DIR = V544 + '-1716577535591288833'
V643 = '6.4.3-ioptcell_intelmpi2017_hdf5_libxc'
V660 = '6.6.0-fixz_ext_oneapi2024.0.1'

BINARIES = {
    V544: b'\x7fELF..vasp.5.4.4.18Apr17-6-g9f103f2a35..-DMPI -DHOST=\\"LinuxIFC\\" -DCACHE_SIZE=4000 -Davoidalloc'
          b'..VASP was compiled without wannier90 library..',
    V643: b'\x7fELF..vasp.6.4.3 19Mar24 (build Jan 10 2025)..-DHOST=\\"LinuxIFC\\" -DMPI -DMPI_BLOCK=8000 '
          b'-DVASP2WANNIER90 -DVASP_HDF5 -DUSELIBXC -DscaLAPACK -Dsol_compat..w90_comms_bcast..wannier_setup..',
    V660: b'\x7fELF..vasp.6.6.0 01Jun25..-DMPI -DVASP2WANNIER90 -DVASP2WANNIER90v2 -D_OPENMP -DVASP_HDF5x'
          b' -Dsol_compat..w90_comms_bcast..',
}


def write_exe(path, data=b'#!/bin/sh\n'):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    path.chmod(0o755)
    return path


@pytest.fixture
def cluster(tmp_path):
    """The probed layout under tmp_path, and a runner that answers like the cluster's shell."""
    public = tmp_path / 'public/software/apprepo/vasp' / V544_DIR
    home = tmp_path / 'work/home/researcher'
    agent_root = home / 'vasp-agent-remote'
    apprepo = home / 'apprepo'
    b544 = write_exe(public / 'bin/vasp_std', BINARIES[V544])
    write_exe(public / 'bin/vasp_gam')
    (public / 'modules').mkdir()
    b643 = write_exe(apprepo / 'vasp' / V643 / 'app/bin/vasp_std', BINARIES[V643])
    for name in ('vasp_gam', 'vasp_ncl'):
        write_exe(apprepo / 'vasp' / V643 / 'app/bin' / name)
    b660 = write_exe(apprepo / 'vasp' / V660 / 'app/bin/vasp_std', BINARIES[V660])
    (apprepo / 'vasp' / V660 / 'package_summary.json').write_text(
        json.dumps({'features': ['Wannier90/v2', 'VASPsol', 'VTST', 'FixZ']}))
    scripts = {}
    for version, binary in ((V643, b643), (V660, b660)):
        script = apprepo / 'vasp' / version / 'scripts/env.sh'
        script.parent.mkdir(parents=True)
        script.write_text(f'module purge\nexport PATH={binary.parent}:$PATH\n')
        scripts[script] = binary
    env544 = agent_root / 'environments/vasp544-public.sh'
    env544.parent.mkdir(parents=True)
    env544.write_text(f'module load {V544}\n')
    scripts[env544] = b544
    write_exe(apprepo / 'phonopy/bin/phonopy')
    write_exe(apprepo / 'vaspkit/1.5.1/bin/vaspkit')
    write_exe(apprepo / 'bader/bin/bader')
    calls = []

    def run(argv, timeout):
        calls.append((list(argv), timeout))
        out, err = '', ''
        if argv[:2] == ['bash', '-c'] and 'command -v vasp_std' in argv[2]:
            binary = scripts.get(Path(argv[4]))
            out = f'{binary}\n' if binary else ''
        elif argv[:2] == ['bash', '-c'] and 'module -t avail' in argv[2]:
            err = (f'{public / "modules"}:\n{V544}\n/public/software/modules:\nvasp-6.4.3\nvasp-6.6.0\n'
                   'vaspkit/1.5.1\nintel/2022.1.0\n')
        elif argv[:2] == ['bash', '-c'] and 'module show' in argv[2]:
            err = {V544: f'prepend-path PATH {public / "bin"}\n',
                   'vasp-6.4.3': f'prepend_path("PATH","{b643.parent}")\n',
                   'vasp-6.6.0': 'prepend-path PATH /nonexistent/bin\n'}.get(argv[4], '')
        elif argv[:2] == ['bash', '-c'] and 'for n in' in argv[2]:
            out = ''.join(f'{name}\t\n' for name in argv[4:])  # nothing on PATH
        elif argv[1:] == ['--version']:
            out = '2.20.0\n'
        return subprocess.CompletedProcess(argv, 0, out, err)

    return NS(tmp=tmp_path, home=home, apprepo=apprepo, agent_root=agent_root, env544=env544, run=run, calls=calls,
              binaries={V544: b544, V643: b643, V660: b660},
              scripts={V643: apprepo / 'vasp' / V643 / 'scripts/env.sh', V660: apprepo / 'vasp' / V660 / 'scripts/env.sh'})


def probe(cluster):
    return inventory.software_inventory(str(cluster.env544), cluster.agent_root, [str(cluster.apprepo)], run=cluster.run)


def by_version(result):
    return {build['version']: build for build in result['vasp_builds']}


# --- 1. The probe ---------------------------------------------------------------

def test_probe_parses_the_three_builds_and_their_capabilities(cluster):
    result = probe(cluster)
    builds = by_version(result)
    assert set(builds) == {'5.4.4', '6.4.3', '6.6.0'}

    old = builds['5.4.4']
    assert old['wannier90_absent_marker'] is True and old['wannier90_symbols'] is False
    assert old['capabilities'] == ['vasp:5']
    assert old['executable_sha256'] == hashlib.sha256(BINARIES[V544]).hexdigest()
    assert old['env_scripts'][0] == {'kind': 'configured', 'path': str(cluster.env544),
                                     'sha256': hashlib.sha256(cluster.env544.read_bytes()).hexdigest(),
                                     'passes': True, 'reason': '已在使用的环境脚本，加载后解析到本构建的 vasp_std'}
    assert old['modules'] == [V544]   # the module and the configured script resolve to one build
    assert old['executables'] == {'vasp_std': str(cluster.binaries[V544].resolve()),
                                  'vasp_gam': str(cluster.binaries[V544].resolve().with_name('vasp_gam')),
                                  'vasp_ncl': None}

    b643 = builds['6.4.3']
    assert {'VASP2WANNIER90', 'VASP_HDF5', 'USELIBXC'} <= set(b643['flags'])
    assert b643['capabilities'] == ['patch:ioptcell', 'vasp:6', 'vasp:hdf5', 'vasp:libxc', 'vasp:wannier90']
    assert b643['wannier90_symbols'] and not b643['wannier90_absent_marker']
    assert all(b643['executables'].values())
    script = b643['env_scripts'][0]
    assert script['kind'] == 'build_script' and script['passes'] and script['path'] == str(cluster.scripts[V643])
    assert b643['modules'] == ['vasp-6.4.3']
    assert 'vasp:vaspsol' not in b643['capabilities']   # stock -Dsol_compat is not the VASPsol patch

    b660 = builds['6.6.0']
    assert b660['capabilities'] == ['patch:fixz', 'vasp:6', 'vasp:openmp', 'vasp:vaspsol', 'vasp:vtst',
                                    'vasp:wannier90', 'vasp:wannier90v2']
    assert b660['metadata_files'] == ['package_summary.json']
    assert b660['env_scripts'][0]['passes']
    assert 'vasp:hdf5' not in b660['capabilities']   # -DVASP_HDF5x is not the flag

    tools = {tool['tool']: tool for tool in result['tools']}
    assert set(tools) == {'phonopy', 'vaspkit', 'bader'}   # no wannier90.x/postw90.x anywhere
    assert tools['phonopy']['version'] == '2.20.0' and tools['vaspkit']['version'] == '1.5.1'
    assert result['modules']['tools'] == ['vaspkit/1.5.1']
    assert result['probe_version'] == inventory.INVENTORY_VERSION and result['content_is_untrusted']


def test_probe_runs_only_bounded_commands_and_never_find(cluster):
    result = probe(cluster)
    assert cluster.calls
    for argv, timeout in cluster.calls:
        assert 0 < timeout <= inventory.MODULE_TIMEOUT
        program = ' '.join(argv[:3])   # the executable, or bash -c and its script; later items are data
        assert not re.search(r'\b(find|locate|du|ls -R|tree)\b', program)
        assert '/public' not in program   # nothing lists /public; module names are passed as data
    assert all(entry['timeout'] and entry['status'] == 'ok' for entry in result['commands'])
    assert result['bounds']['recursive_search'] is False and result['bounds']['depth'] == 2


def test_probe_honours_its_budget_and_reports_timeouts(cluster):
    def slow(argv, timeout):
        raise subprocess.TimeoutExpired(argv, timeout)
    result = inventory.software_inventory(str(cluster.env544), cluster.agent_root, [str(cluster.apprepo)], run=slow)
    assert all(entry['status'] in ('timeout', 'skipped_budget') for entry in result['commands'])
    # Builds found by listing the roots are still fingerprinted; the env scripts could not be checked.
    builds = by_version(result)
    assert set(builds) == {'6.4.3', '6.6.0'}
    assert not any(row['passes'] for build in builds.values() for row in build['env_scripts'])


def test_build_script_that_launches_vasp_is_not_an_environment_script(cluster):
    cluster.scripts[V643].write_text('export PATH=/x:$PATH\nmpirun vasp_std\n')
    script = by_version(probe(cluster))['6.4.3']['env_scripts'][0]
    assert script['passes'] is False and '启动命令' in script['reason']


@pytest.mark.parametrize('root', ['/', '/public', '~/../etc', 'relative/apprepo'])
def test_broad_or_relative_roots_are_refused(tmp_path, root):
    with pytest.raises(ValueError):
        worker.dispatch({'config': {'root': str(tmp_path / 'a/b/c'), 'vasp_env': '/env.sh'},
                         'action': 'software_inventory', 'roots': [root]})


def test_inventory_action_is_read_only(tmp_path, monkeypatch):
    seen = []
    monkeypatch.setattr(worker, 'software_inventory', lambda env, root, roots: seen.append((env, root, roots)) or {'ok': 1})
    root = tmp_path / 'x/y/agent'
    assert worker.dispatch({'config': {'root': str(root), 'vasp_env': '/env.sh'}, 'action': 'software_inventory'}) == {'ok': 1}
    assert seen == [('/env.sh', root.resolve(), ['~/apprepo'])]
    assert not root.exists()   # the probe never creates the remote root


# --- 2. Registration through the operator menu ------------------------------------

@pytest.fixture
def workspace(tmp_path, cluster):
    servers = tmp_path / 'ws/servers'
    servers.mkdir(parents=True)
    connection = '{"host": "eshell111", "user": "researcher", "port": 22}'
    (servers / 'hpc.json').write_text(connection)
    compute = {'root': '/work/home/researcher/vasp-agent-remote', 'python': '/opt/py/bin/python',
               'potcar_library': '/public/PBE', 'vasp_env': str(cluster.env544), 'partition': 'compute',
               'ntasks': 8, 'walltime': '04:00:00'}
    (servers / 'hpc.compute.json').write_text(json.dumps(compute))
    box = Toolbox(tmp_path / 'ws')
    box.plan_output = lambda text: None
    return NS(box=box, servers=servers, connection=connection, compute=json.dumps(compute))


@pytest.fixture
def remote_calls(monkeypatch, cluster):
    """RemoteCompute.call answered from the fixture cluster (no SSH)."""
    calls = []
    import vasp_agent.remote.remote_compute as remote_compute
    # The fixture's paths are local Windows/POSIX temp paths, not the cluster's absolute ones.
    monkeypatch.setattr(remote_compute, 'validate_paths', lambda config: config)

    def call(self, action, job_name=None, **params):
        calls.append((action, params))
        if action == 'software_inventory':
            return probe(cluster)
        if action == 'environment_profile_check':
            script = Path(params['env_script'])
            binary = {cluster.env544: cluster.binaries[V544], cluster.scripts[V643]: cluster.binaries[V643],
                      cluster.scripts[V660]: cluster.binaries[V660]}[script]
            return {'environment_script': str(script), 'environment_sha256': hashlib.sha256(script.read_bytes()).hexdigest(),
                    'executable': str(binary.resolve()), 'executable_sha256': hashlib.sha256(binary.read_bytes()).hexdigest(),
                    'constraint_markers': []}
        raise AssertionError(action)
    monkeypatch.setattr(RemoteCompute, 'call', call)
    return calls


def answers(*values):
    queue = list(values)
    return lambda prompt: queue.pop(0)


def test_scan_saves_an_untrusted_record_outside_the_connection_files(workspace, remote_calls):
    result = workspace.box.call('software_environments', {'server': 'hpc', 'action': 'scan'})
    assert result['ok']
    view = result['result']
    assert view['content_is_untrusted'] and len(view['inventory']['vasp_builds']) == 3
    assert [row['name'] for row in view['profiles']] == ['default']   # nothing registered by a scan
    saved = json.loads((workspace.servers / 'environments/hpc.inventory.json').read_text(encoding='utf-8'))
    assert saved['probe_version'] == inventory.INVENTORY_VERSION and saved['saved_at']
    assert (workspace.servers / 'hpc.json').read_text() == workspace.connection
    assert (workspace.servers / 'hpc.compute.json').read_text() == workspace.compute
    assert sorted(p.name for p in workspace.servers.glob('*.json')) == ['hpc.compute.json', 'hpc.json']


def test_registration_needs_the_operator_menu(workspace, remote_calls):
    workspace.box.call('software_environments', {'server': 'hpc', 'action': 'scan'})
    result = workspace.box.call('software_environments', {'server': 'hpc', 'action': 'register'})['result']
    assert result['status'] == 'operator_confirmation_required' and result['registered'] == []
    assert len(result['candidates']) == 3
    assert not (workspace.servers / 'environments/hpc.profiles.json').exists()


def test_menu_registers_confirmed_builds_and_skips_the_rest(workspace, remote_calls, cluster):
    box = workspace.box
    software_commands.scan(box, 'hpc')
    shown = []
    result = software_commands.register(box, 'hpc', answers('n', 'y', '', 'vasp660', 'FixZ/VASPsol/VTST 补丁'),
                                        shown.append)
    assert result['registered'] == ['vasp643', 'vasp660']
    assert result['skipped'][0]['reason'] == 'operator_skipped'
    text = '\n'.join(shown)
    assert 'VASP was compiled without wannier90 library' in text and '-DVASP2WANNIER90v2' in text
    saved = {row['name']: row for row in profiles.load_profiles(box.workspace, 'hpc')}
    assert saved['vasp643']['env_script'] == str(cluster.scripts[V643])
    assert saved['vasp643']['executable_sha256'] == hashlib.sha256(BINARIES[V643]).hexdigest()
    assert saved['vasp643']['script_source'] == 'build_script' and saved['vasp643']['confirmed_by'] == 'operator_menu'
    assert saved['vasp660']['capabilities'][-1] == 'vasp:wannier90v2' and saved['vasp660']['notes'].startswith('FixZ')
    # The implicit default stays a candidate beside the registered ones.
    assert [row['name'] for row in profiles.candidates(box.workspace, 'hpc')] == ['default', 'vasp643', 'vasp660']
    assert (workspace.servers / 'hpc.compute.json').read_text() == workspace.compute


def test_module_only_build_gets_a_generated_script_shown_before_writing(workspace, remote_calls, cluster, monkeypatch):
    box = workspace.box
    record = software_commands.scan(box, 'hpc')
    build = by_version(record['result'])['5.4.4']
    build['env_scripts'] = []   # as if no script loaded it: only the module remains
    record['result']['vasp_builds'] = [build]
    profiles.save_inventory(box.workspace, 'hpc', record['result'], record['requested_roots'])
    written = []
    import vasp_agent.remote.server_setup as server_setup
    monkeypatch.setattr(server_setup, 'install_env_script', lambda ws, server, path, content: written.append((path, content)))
    shown = []
    result = software_commands.register(box, 'hpc', answers('y', '取消'), shown.append)
    assert result['registered'] == [] and written == []
    assert any('module load ' + V544 in line for line in shown)
    assert any('/work/home/researcher/vasp-agent-remote/environments/vasp544.sh' in line for line in shown)


def test_a_changed_executable_is_not_registered(workspace, remote_calls, cluster):
    software_commands.scan(workspace.box, 'hpc')
    cluster.binaries[V643].write_bytes(b'rebuilt')   # changed between probe and confirmation
    result = software_commands.register(workspace.box, 'hpc', answers('n', 'y', '', 'q'), lambda _: None)
    assert result['registered'] == [] and result['skipped'][-1]['reason'] == 'executable_changed'


# --- 3. Selection by capability ------------------------------------------------------

def register(box, name, script, binary, capabilities):
    profiles.save_profile(box.workspace, 'hpc', {
        'name': name, 'env_script': str(script), 'environment_sha256': hashlib.sha256(script.read_bytes()).hexdigest(),
        'executable': str(binary), 'executable_sha256': hashlib.sha256(binary.read_bytes()).hexdigest(),
        'capabilities': capabilities, 'version': name[-3:]})


@pytest.fixture
def registered(workspace, cluster):
    register(workspace.box, 'vasp643', cluster.scripts[V643], cluster.binaries[V643],
             ['patch:ioptcell', 'vasp:6', 'vasp:hdf5', 'vasp:libxc', 'vasp:wannier90'])
    register(workspace.box, 'vasp660', cluster.scripts[V660], cluster.binaries[V660],
             ['patch:fixz', 'vasp:6', 'vasp:openmp', 'vasp:wannier90', 'vasp:wannier90v2'])
    return workspace


@pytest.fixture
def wannier_stage(monkeypatch):
    """A synthetic stage requiring a Wannier-capable VASP 6 (the §8 declaration)."""
    definition = stages._definition(
        'wannier', depends_on=('scf',), inputs={'rule': 'test', 'enforced_by': 'test', 'note': 'test'},
        checks={'physics': stages._PHYSICS}, execution={'mode': 'allocation', 'runner': 'wannier'},
        phase_outputs=('wannier.json',), completion={'rule': 'test', 'enforced_by': 'test'},
        required_capabilities=('vasp>=6', 'vasp:wannier90'))
    monkeypatch.setitem(stages.DEFINITIONS, 'wannier', {1: definition})
    return stages.record((*stages.LEGACY_STAGES, 'wannier'))


def test_without_profiles_the_default_is_the_only_match(workspace):
    choice = profiles.choose(workspace.box.workspace, 'hpc')
    assert choice['status'] == 'one' and choice['matches'][0]['name'] == 'default'
    assert profiles.payload(choice['matches'][0]) is None   # nothing new is sent
    assert profiles.requirements() == ()


def test_several_matches_and_cell_conflicts(registered):
    workspace = registered.box.workspace
    assert [row['name'] for row in profiles.choose(workspace, 'hpc', 'ions')['matches']] == ['default', 'vasp643', 'vasp660']
    cell = profiles.choose(workspace, 'hpc', 'cell')
    assert cell['status'] == 'one' and cell['matches'][0]['name'] == 'default'
    assert {row['name'] for row in cell['excluded']} == {'vasp643', 'vasp660'}


def test_stage_requirements_select_the_capable_profiles(registered, wannier_stage):
    workspace = registered.box.workspace
    required = profiles.requirements(wannier_stage)
    assert required == ('vasp>=6', 'vasp:wannier90')
    choice = profiles.choose(workspace, 'hpc', 'ions', required)
    assert choice['status'] == 'several' and [row['name'] for row in choice['matches']] == ['vasp643', 'vasp660']
    assert choice['excluded'] == [{'name': 'default', 'reason': '缺少 vasp>=6、vasp:wannier90'}]
    one = profiles.choose(workspace, 'hpc', 'ions', ('vasp:wannier90v2',))
    assert one['status'] == 'one' and one['matches'][0]['name'] == 'vasp660'


def test_no_capable_profile_is_refused_with_what_is_missing(workspace, wannier_stage):
    choice = profiles.choose(workspace.box.workspace, 'hpc', 'ions', ('vasp:wannier90',))
    assert choice['status'] == 'none'
    assert choice['message'] == '需要 vasp:wannier90，已登记环境均不满足'


def test_version_requirements():
    assert stages.satisfies(['vasp:6'], 'vasp>=6') and not stages.satisfies(['vasp:5'], 'vasp>=6')
    assert stages.unmet(('vasp>=6', 'vasp:wannier90'), ['vasp:6']) == ('vasp:wannier90',)
    assert stages.required_capabilities(stages.record()) == ()


# --- 3b. The plan menu ----------------------------------------------------------------

def plan_request():
    return dict(server='hpc', job_name='wan-task', structure='POSCAR', encut=520, mesh=[8, 8, 8], nbands=16,
                relax_mode='ions', parallel_tests=1, cores_per_test=8, scan_preset='standard')


@pytest.fixture
def fake_invoke(monkeypatch):
    sent = []
    defaults = {'encuts': [520., 598., 676.], 'meshes': [[8, 8, 8], [10, 10, 10], [12, 12, 12]]}

    def invoke(box, request, preview=False, choice=None, environment=None, solver_overrides=None):
        sent.append({'preview': preview, 'environment': environment})
        return {'defaults': defaults, 'potential_floor': 0., 'signature': 'sig'} if preview else {'prepared': True}
    monkeypatch.setattr(plans, 'invoke', invoke)
    return sent


def test_plan_without_profiles_is_unchanged(workspace, fake_invoke):
    summary = plans.propose(workspace.box, plan_request(), None, None, 'tool_default', '')
    assert summary['status'] == 'selected' and 'environment' not in summary
    plans.prepare(workspace.box, summary['id'])
    assert [call['environment'] for call in fake_invoke] == [None, None]


def test_plan_menu_chooses_among_several_profiles(registered, fake_invoke):
    box = registered.box
    shown = []
    box.plan_output = shown.append
    box.plan_reader = answers('3')
    summary = plans.propose(box, plan_request(), None, None, 'tool_default', '')
    assert summary['status'] == 'selected'
    assert summary['environment']['selected'] == 'vasp660' and summary['environment']['source'] == 'operator_menu'
    assert any('vasp643' in line and 'vasp660' in line for line in shown)
    plans.prepare(box, summary['id'])
    sent = fake_invoke[-1]['environment']
    assert sent['name'] == 'vasp660' and sent['executable_sha256'] == hashlib.sha256(BINARIES[V660]).hexdigest()
    assert set(sent) == set(profiles.PROFILE_FIELDS)


def test_plan_stays_pending_until_the_environment_is_chosen(registered, fake_invoke):
    box = registered.box
    box.plan_reader = answers('q')
    summary = plans.propose(box, plan_request(), None, None, 'tool_default', '')
    assert summary['status'] == 'pending' and summary['next'] == '/plan'
    assert summary['selection']['requires_environment_choice'] is True
    assert summary['selection']['requires_parameter_choice'] is False
    with pytest.raises(ValueError, match='尚未选择'):
        plans.prepare(box, summary['id'])


def test_plan_with_one_match_names_it(registered, fake_invoke, wannier_stage, monkeypatch):
    monkeypatch.setattr(profiles, 'requirements', lambda entries=None: ('vasp:wannier90v2',))
    summary = plans.propose(registered.box, plan_request(), None, None, 'tool_default', '')
    assert summary['status'] == 'selected'
    assert summary['environment']['selected'] == 'vasp660' and summary['environment']['source'] == 'only_match'
    assert '唯一' in summary['environment']['message']


def test_plan_with_no_match_is_refused_before_any_remote_call(registered, fake_invoke, monkeypatch):
    monkeypatch.setattr(profiles, 'requirements', lambda entries=None: ('tool:phonopy',))
    with pytest.raises(ValueError, match='需要 tool:phonopy，已登记环境均不满足'):
        plans.propose(registered.box, plan_request(), None, None, 'tool_default', '')
    assert fake_invoke == []


def test_direct_remote_prepare_refuses_an_ambiguous_choice(registered, monkeypatch):
    box = registered.box
    assert box.direct_environment('hpc', 'cell') is None   # only the default can relax the cell
    with pytest.raises(ValueError, match='propose_scan_plan'):
        box.direct_environment('hpc', 'ions')


# --- 4. The worker: task binding, submit script, drift -------------------------------------

def profile_for(script, binary, caps=('vasp:6', 'vasp:wannier90')):
    return {'name': 'vasp643', 'env_script': str(script),
            'environment_sha256': hashlib.sha256(script.read_bytes()).hexdigest(),
            'executable_sha256': hashlib.sha256(binary.read_bytes()).hexdigest(), 'capabilities': list(caps)}


@pytest.fixture
def remote_root(tmp_path, monkeypatch, cluster):
    release = tmp_path / 'release'
    release.mkdir()
    root = tmp_path / 'remote/agent/root'
    root.mkdir(parents=True)
    (root / 'deployment.json').write_text(json.dumps({'release': str(release)}))
    binaries = {cluster.env544: cluster.binaries[V544], cluster.scripts[V643]: cluster.binaries[V643]}

    def environment(cfg, mode):
        script = Path(cfg['vasp_env'])
        binary = binaries[script]
        return {'environment_script': str(script), 'environment_sha256': hashlib.sha256(script.read_bytes()).hexdigest(),
                'executable': str(binary), 'executable_sha256': hashlib.sha256(binary.read_bytes()).hexdigest(),
                'constraint_markers': [], 'provenance': 'unverified'}
    monkeypatch.setattr(worker, 'inspect_environment', environment)
    seen = []

    def fake_prepare(args):
        seen.append(args)
        folder = Path(args.workdir)
        folder.mkdir(parents=True)
        (folder / 'manifest.json').write_text('{"ready": true}')
    monkeypatch.setattr(full, 'prepare_full', fake_prepare)
    monkeypatch.setattr(worker.subprocess, 'run', lambda *args, **kwargs: NS(returncode=0))
    cfg = dict(root=str(root), python='/opt/python', potcar_library='/data/PBE', vasp_env=str(cluster.env544),
               partition='compute', ntasks=8, walltime='04:00:00')
    return NS(root=root, release=release, cfg=cfg, seen=seen)


def prepare(remote, **extra):
    return worker.dispatch({'config': remote.cfg, 'action': 'prepare', 'job_name': 'si',
                            'structure': base64.b64encode(b'structure fixture').decode(),
                            'parameters': {'encut': 520, 'mesh': [8, 8, 8], 'nbands': 16, 'relax_mode': 'ions',
                                           'parallel_tests': 1, 'cores_per_test': 8}, **extra})


def legacy_script(remote, source):
    return ('#!/bin/bash\n#SBATCH --job-name=va-si\n#SBATCH --partition=compute\n#SBATCH --nodes=1\n'
            '#SBATCH --ntasks=8\n#SBATCH --cpus-per-task=1\n#SBATCH --time=04:00:00\n#SBATCH --output=slurm-%j.log\n'
            f'set -eo pipefail\nsource {shlex.quote(source)}\nexport OMP_NUM_THREADS=1\n'
            + shlex.join(['/opt/python', str(remote.release / 'vasp_band_agent.py'), 'run',
                          str(remote.root.resolve() / 'jobs/si'), '--', 'srun', 'vasp_std']) + '\n')


def test_without_a_profile_the_prepared_task_is_byte_identical(remote_root, cluster):
    result = prepare(remote_root)
    folder = remote_root.root / 'jobs/si'
    assert (folder / 'submit.slurm').read_text() == legacy_script(remote_root, str(cluster.env544))
    assert 'profile' not in json.loads((folder / 'environment-review.json').read_text())
    assert not hasattr(remote_root.seen[0], 'environment_profile')
    assert 'profile' not in result['environment']


def test_profile_task_sources_its_script_and_records_the_binding(remote_root, cluster):
    profile = profile_for(cluster.scripts[V643], cluster.binaries[V643])
    result = prepare(remote_root, environment_profile=profile)
    folder = remote_root.root / 'jobs/si'
    # The controlled template, with only the sourced script changed.
    assert (folder / 'submit.slurm').read_text() == legacy_script(remote_root, str(cluster.scripts[V643]))
    review = json.loads((folder / 'environment-review.json').read_text())
    assert review['environment_script'] == str(cluster.scripts[V643])
    assert review['executable_sha256'] == profile['executable_sha256']
    assert review['profile'] == {'name': 'vasp643', 'capabilities': ['vasp:6', 'vasp:wannier90'], 'version': 1}
    assert remote_root.seen[0].environment_profile == {**profile, 'capabilities': ['vasp:6', 'vasp:wannier90']}
    assert result['environment']['profile']['name'] == 'vasp643'


def test_a_drifted_profile_is_refused_at_preparation(remote_root, cluster):
    profile = profile_for(cluster.scripts[V643], cluster.binaries[V643])
    cluster.binaries[V643].write_bytes(b'rebuilt in place')
    with pytest.raises(ValueError, match='重新确认') as error:
        prepare(remote_root, environment_profile=profile)
    assert error.value.error_code == 'ENVIRONMENT_PROFILE_DRIFT'
    assert not (remote_root.root / 'jobs/si').exists()


def test_an_old_engine_refuses_a_profile(remote_root, cluster, monkeypatch):
    monkeypatch.delattr(full, 'ENVIRONMENT_PROFILES_VERSION')
    with pytest.raises(ValueError, match='部署新版'):
        prepare(remote_root, environment_profile=profile_for(cluster.scripts[V643], cluster.binaries[V643]))
    prepare(remote_root)   # a legacy preparation still works on it


@pytest.mark.parametrize('field, value', [('name', 'default'), ('env_script', 'relative/env.sh'),
                                          ('executable_sha256', 'x'), ('capabilities', ['Bad Tag'])])
def test_malformed_profile_records_are_refused(remote_root, cluster, field, value):
    profile = {**profile_for(cluster.scripts[V643], cluster.binaries[V643]), field: value}
    with pytest.raises(ValueError):
        prepare(remote_root, environment_profile=profile)


# --- 4b. The engine records the profile; submission re-checks it --------------------

@pytest.fixture
def engine_task(tmp_path, cluster):
    from legacy_stage_walk import build
    (tmp_path / 'engine').mkdir()
    return build(tmp_path / 'engine', legacy=False)


def test_engine_records_the_profile_and_refuses_missing_capabilities(tmp_path, cluster, wannier_stage, monkeypatch):
    from pymatgen.core import Lattice, Structure
    from pymatgen.io.vasp.inputs import Poscar
    source = tmp_path / 'POSCAR'
    Poscar(Structure(Lattice.cubic(5.43), ['Si'], [[0, 0, 0]])).write_file(source)

    def args(workdir, profile):
        return NS(structure=str(source), workdir=str(tmp_path / workdir), potcar=None, encut=520, mesh=[4, 4, 4],
                  nbands=16, relax_mode='ions', ediff=1e-7, points=40, symprec=.01, force_tol=.01, stress_tol=1.,
                  energy_tol=.001, nsw=200, environment_profile=profile)
    profile = profiles.payload({**profile_for(cluster.scripts[V643], cluster.binaries[V643]), 'version': '6.4.3'})
    full.prepare_full(args('bound', profile))
    config = json.loads((tmp_path / 'bound/config.json').read_text())
    assert config['environment_profile'] == profile
    manifest = json.loads((tmp_path / 'bound/manifest.json').read_text())
    assert manifest['hashes']['config.json'] == core.digest(tmp_path / 'bound/config.json')
    # Stages needing Wannier90: the default (no profile) and a profile without it are refused.
    monkeypatch.setattr(stages, 'record', lambda names=None: wannier_stage)
    with pytest.raises(ValueError, match='需要 vasp>=6、vasp:wannier90，默认环境未登记该能力'):
        full.prepare_full(args('legacy', None))
    with pytest.raises(ValueError, match='需要 vasp:wannier90，环境配置 vasp643 不满足'):
        full.prepare_full(args('weak', {**profile, 'capabilities': ['vasp:6']}))
    assert not (tmp_path / 'legacy').exists() and not (tmp_path / 'weak').exists()


def submit_ready(root, cluster, profile):
    """Bind the engine-built task to a profile as a profile preparation would, ready for preflight."""
    config = json.loads((root / 'config.json').read_text())
    if profile is not None:
        config['environment_profile'] = profile
    core.save(root / 'config.json', config)
    manifest = json.loads((root / 'manifest.json').read_text())
    manifest['hashes']['config.json'] = core.digest(root / 'config.json')
    core.save(root / 'manifest.json', manifest)
    (root / 'submit.slurm').write_text('#!/bin/bash\n')
    core.save(root / 'remote-job.json', {'release': str(root.parent), 'script_sha256': worker.sha(root / 'submit.slurm')})


def test_submission_refuses_a_drifted_profile_executable(engine_task, cluster, monkeypatch):
    monkeypatch.setattr(worker, 'inspect_environment', lambda cfg, mode: {
        'environment_sha256': hashlib.sha256(Path(cfg['vasp_env']).read_bytes()).hexdigest(),
        'executable_sha256': hashlib.sha256(cluster.binaries[V643].read_bytes()).hexdigest(), 'executable': 'x'})
    profile = profile_for(cluster.scripts[V643], cluster.binaries[V643])
    submit_ready(engine_task, cluster, profile)
    cfg = {'vasp_env': str(cluster.env544)}
    cluster.binaries[V643].write_bytes(b'rebuilt in place')
    with pytest.raises(ValueError, match='重新确认该配置') as error:
        worker._submit_checks({'phase': 'convergence'}, cfg, engine_task, True)
    assert error.value.error_code == 'ENVIRONMENT_PROFILE_DRIFT'


def test_submission_checks_the_profile_script_not_vasp_env(engine_task, cluster, monkeypatch):
    seen = []

    def environment(cfg, mode):
        seen.append(cfg['vasp_env'])
        raise ValueError('stop after the environment check')
    monkeypatch.setattr(worker, 'inspect_environment', environment)
    submit_ready(engine_task, cluster, profile_for(cluster.scripts[V643], cluster.binaries[V643]))
    with pytest.raises(ValueError, match='stop after'):
        worker._submit_checks({'phase': 'convergence'}, {'vasp_env': str(cluster.env544)}, engine_task, True)
    assert seen == [str(cluster.scripts[V643])]


def test_legacy_task_submission_keeps_using_vasp_env(engine_task, cluster, monkeypatch):
    seen = []

    def environment(cfg, mode):
        seen.append(cfg['vasp_env'])
        raise ValueError('stop after the environment check')
    monkeypatch.setattr(worker, 'inspect_environment', environment)
    submit_ready(engine_task, cluster, None)
    with pytest.raises(ValueError, match='stop after'):
        worker._submit_checks({'phase': 'convergence'}, {'vasp_env': str(cluster.env544)}, engine_task, True)
    assert seen == [str(cluster.env544)]


def test_fork_of_a_profile_task_needs_the_same_environment(remote_root, cluster, monkeypatch):
    source_config = {'relax_mode': 'ions', 'environment_profile': profile_for(cluster.scripts[V643], cluster.binaries[V643])}
    worker.activate_release(remote_root.release)
    cfg, environment = worker.fork_environment(remote_root.cfg, source_config)
    assert cfg['vasp_env'] == str(cluster.scripts[V643]) and environment['profile']['name'] == 'vasp643'
    cfg, environment = worker.fork_environment(remote_root.cfg, {'relax_mode': 'ions'})
    assert cfg['vasp_env'] == str(cluster.env544) and 'profile' not in environment
    cluster.binaries[V643].write_bytes(b'rebuilt in place')
    with pytest.raises(ValueError, match='重新确认'):
        worker.fork_environment(remote_root.cfg, source_config)


def test_software_command_shows_scans_and_registers(workspace, remote_calls):
    from vasp_agent.chat.extra_commands import execute
    shown = []
    c = NS(agent=NS(tools=workspace.box), output=shown.append, reader=answers('n', 'n', 'n'))
    assert execute(c, '/software', 'show')
    assert any('尚无探测记录' in line for line in shown)
    execute(c, '/software', 'scan hpc')
    assert remote_calls[-1] == ('software_inventory', {'roots': None})
    assert any('vasp:wannier90v2' in line for line in shown)
    execute(c, '/software', 'register')
    assert profiles.load_profiles(workspace.box.workspace, 'hpc') == []


def test_bare_software_command_guides_scan_then_register(workspace, remote_calls):
    from vasp_agent.chat.extra_commands import execute
    shown = []
    # 1 = scan now, 1 = register now, then skip each of the three probed builds.
    c = NS(agent=NS(tools=workspace.box), output=shown.append, reader=answers('1', '1', 'n', 'n', 'n'))
    execute(c, '/software', 'hpc')
    assert remote_calls[-1] == ('software_inventory', {'roots': None})
    assert any('vasp:wannier90v2' in line for line in shown)
    assert any(line.startswith('环境配置登记') for line in shown)
    assert profiles.load_profiles(workspace.box.workspace, 'hpc') == []


def test_bare_software_command_can_just_look(workspace, remote_calls):
    from vasp_agent.chat.extra_commands import execute
    c = NS(agent=NS(tools=workspace.box), output=lambda _: None, reader=answers('q'))
    execute(c, '/software', 'hpc')
    assert remote_calls == []


def test_real_660_binary_is_not_misread_as_vasp5(tmp_path):
    """The operator's 6.6.0 build has no run banner; help text names vasp.5.2.2 and
    prose contains -Dxxx-looking fragments (probe v1 reported 5.2.2 and junk flags)."""
    from vasp_agent.remote import worker_inventory as inv
    folder = tmp_path / '6.6.0-fixz_ext_oneapi2024.0.1'
    binary = folder / 'app' / 'bin' / 'vasp_std'
    binary.parent.mkdir(parents=True)
    binary.write_bytes(
        b'\x7fELF..| L  is equivalent to ENCUTFOCK=0 in vasp.5.2.2      |..prior to vasp.4.4..'
        b'Directed graph -Directed -Done -DFT..Kpoint -DVASP_HDF5Kpoint..'
        b'-DHOST="LinuxIFC" -DMPI -DMPI_BLOCK=8000 -Duse_collective -DscaLAPACK -Dvasp6 '
        b'-DVASP2WANNIER90 -DVASP2WANNIER90v2 -D_OPENMP -Dsol_compat ..w90_comms_bcast..')
    (folder / 'package_summary.json').write_text('{"version": "6.6.0", "features": "Wannier90/v2"}')
    scanned = inv.scan_binary(binary)
    assert {'Directed', 'Done', 'FT', 'VASP_HDF5Kpoint'}.isdisjoint(scanned['flags'])
    assert {'vasp6', 'VASP2WANNIER90v2', '_OPENMP', 'MPI_BLOCK', 'HOST'} <= scanned['flags']
    assert scanned['versions'] == set()   # help text is not a banner
    _, text = inv.read_metadata(folder)
    assert inv.choose_version(scanned['versions'], text, folder, [], scanned['flags']) == ('6.6.0', 'metadata')
    # Without metadata the path wins; a vasp5-looking path is vetoed by -Dvasp6.
    assert inv.choose_version(set(), '', folder, [], scanned['flags']) == ('6.6.0', 'path')
    assert inv.choose_version({'5.2.2'}, '', tmp_path / 'x', [], {'vasp6'}) == ('6', 'flag')
    tags = inv.capabilities('6.6.0', scanned['flags'], 'fixz', False)
    assert 'vasp:6' in tags and 'vasp:5' not in tags and 'vasp:wannier90v2' in tags


def test_544_banner_still_gives_vasp5(tmp_path):
    from vasp_agent.remote import worker_inventory as inv
    scanned_versions = {'5.4.4'}
    assert inv.choose_version(scanned_versions, '', tmp_path / 'bin', [], {'MPI'}) == ('5.4.4', 'binary')
