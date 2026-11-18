import os
from pathlib import Path

import pytest
from vasp_agent.common.workspace_paths import local_path, workspace_path
from test_chat_commands import setup
from vasp_agent.chat.ai_agent import Toolbox


def test_path_prose_not_command(tmp_path):
    commands, output = setup(tmp_path)
    for message in ['/c/Users/js/examples/Si.POSCAR 这是结构，计算体相Si', '/work/Si.POSCAR 检查', '"/c/My Files/Si.POSCAR" 检查']:
        assert commands.handle(message) == 'pass'
    assert not output
    assert commands.handle('/typo') == 'handled'
    assert '未知命令' in output[-1]
    assert commands.handle('/help') == 'handled'


def test_normalize_only_local_windows_paths():
    assert local_path('/c/Users/js/a b/Si.POSCAR', windows=True) == 'C:/Users/js/a b/Si.POSCAR'
    assert local_path('/c/Users/js/Si.POSCAR', windows=False) == '/c/Users/js/Si.POSCAR'
    assert local_path('/work/home/Si.POSCAR', windows=True) == '/work/home/Si.POSCAR'
    assert local_path('"C:/My Files/Si.POSCAR"', windows=True) == 'C:/My Files/Si.POSCAR'


@pytest.mark.skipif(os.name != 'nt', reason='Windows Git Bash integration')
def test_gitbash_structure_and_boundary(tmp_path):
    source = tmp_path / 'Si.POSCAR'
    source.write_bytes((Path(__file__).parents[1] / 'examples/Si.POSCAR').read_bytes())
    native = source.as_posix()
    bash = '/' + native[0].lower() + native[2:]
    assert Toolbox(tmp_path).inspect_structure(bash)['formula'] == 'Si'
    with pytest.raises(ValueError, match='工作区'):
        workspace_path(tmp_path, '/' + native[0].lower() + native[2:] + '/../../outside')
