from pathlib import Path
import pytest
from vasp_agent.common.structure_import import import_poscar
from vasp_agent.chat.ai_agent import Toolbox

POSCAR = "Si\n1\n5 0 0\n0 5 0\n0 0 5\nSi\n1\nDirect\n0 0 0\n"

@pytest.fixture
def files(tmp_path):
    root = tmp_path / 'workspace'
    root.mkdir()
    source = tmp_path / 'with spaces' / 'POSCAR'
    source.parent.mkdir()
    source.write_text(POSCAR, encoding='utf-8')
    return root, source


def test_auto_import_and_original_unchanged(files):
    root, source = files
    box = Toolbox(root)
    box.latest_user_message = f'计算 "{source}" 的能带'
    answer = box.inspect_structure(str(source))
    assert answer['formula'] == 'Si' and answer['atoms'] == 1
    assert Path(answer['path']).parts[0] == 'structures'
    assert (root / answer['path']).read_bytes() == source.read_bytes()
    assert (root / answer['path']).with_suffix('.import.json').exists()
    assert answer['import_receipt']['submitted'] is False
    assert not box.allow_submit
    second = box.inspect_structure(str(source))
    assert second['path'] != answer['path']


def test_not_named_by_user_refused(files):
    root, source = files
    with pytest.raises(ValueError, match='明确'):
        import_poscar(root, str(source), '计算Si')
    with pytest.raises(ValueError, match='明确'):
        import_poscar(root, str(source), str(source)+'-another')
    assert not (root / 'structures').exists()


@pytest.mark.parametrize('content', ['garbage', '-----BEGIN PRIVATE KEY-----', 'Si\npassword=secret', 'a\x00b'])
def test_invalid_or_sensitive_not_copied(files, content):
    root, source = files
    source.write_text(content)
    with pytest.raises(ValueError, match='有效UTF-8 POSCAR'):
        import_poscar(root, str(source), str(source))
    assert not (root / 'structures').exists()


def test_large_file_not_copied(files):
    root, source = files
    source.write_bytes(b'x'*5000001)
    with pytest.raises(ValueError, match='5 MB'):
        import_poscar(root, str(source), str(source))


def test_hardlink_refused(files):
    root, source = files
    alias = source.with_name('alias')
    alias.hardlink_to(source)
    with pytest.raises(ValueError, match='硬链接'):
        import_poscar(root, str(alias), str(alias))


def test_git_bash_spelling(files):
    import os
    if os.name != 'nt':
        pytest.skip('Windows path normalization')
    root, source = files
    path = '/' + source.drive[0].lower() + source.as_posix()[2:]
    answer = import_poscar(root, path, f'检查 {path}')
    assert (root / answer['path']).is_file()


def test_workspace_file_needs_no_import(files):
    root, source = files
    (root / 'POSCAR').write_text(POSCAR)
    box = Toolbox(root)
    assert box.inspect_structure('POSCAR')['import_receipt'] is None
