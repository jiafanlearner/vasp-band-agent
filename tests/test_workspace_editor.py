import json
import os
from pathlib import Path

import pytest

from vasp_agent.editing.edit_store import EditStore, digest
from vasp_agent.editing.workspace_editor import WorkspaceEditor, MAX_BYTES, PAGE
from vasp_agent.workflow.vasp_band_agent import ToolError
from vasp_agent.common import agent_paths


def create(editor, path='hello.py', text='value = 1\n', request_id=None):
    return editor.edit(path, 'create', None, text, None, '用户要求', request_id)


def patch(path='hello.py', old='value = 1', new='value = 2'):
    return f'--- a/{path}\n+++ b/{path}\n@@ -1 +1 @@\n-{old}\n+{new}\n'


def test_create_patch_revert_and_records(tmp_path):
    editor = WorkspaceEditor(tmp_path)
    assert editor.list()['files'] == []
    assert not (tmp_path / 'workbench').exists()
    initial = create(editor)
    read = editor.read('hello.py')
    changed = editor.edit('hello.py', 'patch', read['sha256'], patch=patch())
    assert changed['added_lines'] == changed['removed_lines'] == 1
    assert 'Python' in changed['validation']
    assert editor.read('hello.py')['content'] == 'value = 2\n'
    assert editor.get(changed['change_id'])['current_state'] == 'applied'
    assert len(editor.changes()) == 2
    editor.revert(changed['change_id'])
    assert editor.read('hello.py')['content'] == 'value = 1\n'
    assert not editor.revert(changed['change_id'])['changed']
    editor.revert(initial['change_id'])
    assert not (tmp_path / 'workbench/hello.py').exists()


@pytest.mark.parametrize('path', ['../ai_agent.py', '/tmp/out', '/c/Users/js/out', 'C:/outside',
    'x/../../runs/si/INCAR', '.env', 'sub/.env.local', 'POTCAR', 'POTCAR.txt', 'id_rsa',
    'private.key', 'credentials.json', 'file:stream', 'NUL', 'folder./x', 'name '])
def test_path_boundaries(tmp_path, path):
    with pytest.raises(ToolError):
        create(WorkspaceEditor(tmp_path), path)
    assert not (tmp_path / 'runs').exists()


def test_links_and_configured_renamed_key(tmp_path):
    editor = WorkspaceEditor(tmp_path)
    root = tmp_path / 'workbench'
    root.mkdir()
    original = tmp_path / 'original.txt'
    original.write_text('secret-free file')
    try:
        os.link(original, root / 'alias.txt')
    except OSError:
        pytest.skip('hard links unavailable')
    with pytest.raises(ToolError):
        editor.read('alias.txt')
    (root / 'alias.txt').unlink()
    private = root / 'ordinary.txt'
    private.write_text('ordinary-looking key')
    (tmp_path / 'servers').mkdir()
    (tmp_path / 'servers/hpc.json').write_text(json.dumps({'identity_file': str(private)}))
    with pytest.raises(ToolError):
        editor.read('ordinary.txt')
    assert 'ordinary.txt' not in editor.list()['files']


def test_symlink_escape(tmp_path):
    editor = WorkspaceEditor(tmp_path)
    (tmp_path / 'workbench').mkdir()
    outside = tmp_path / 'outside'
    outside.mkdir()
    try:
        (tmp_path / 'workbench/alias').symlink_to(outside, target_is_directory=True)
    except OSError:
        pytest.skip('symlink privilege unavailable')
    with pytest.raises(ToolError):
        create(editor, 'alias/x.txt')
    assert editor.list()['files'] == []


@pytest.mark.parametrize('body', ['-----BEGIN RSA PRIVATE KEY-----\nnot-real\n-----END RSA PRIVATE KEY-----',
                                'api_key = "a-test-secret-value"', 'PAW_PBE Si\nTITEL = Si', 'a\x00b'])
def test_sensitive_and_binary_never_backed_up(tmp_path, body):
    editor = WorkspaceEditor(tmp_path)
    with pytest.raises(ToolError):
        create(editor, 'notes.txt', body)
    assert not list(agent_paths.store_dir(tmp_path, 'workspace_edits').glob('*.json'))
    folder = tmp_path / 'workbench'
    folder.mkdir(exist_ok=True)
    (folder / 'notes.txt').write_text(body, encoding='utf-8')
    with pytest.raises(ToolError):
        editor.read('notes.txt')
    assert editor.search('test')['matches'] == []
    assert not list(agent_paths.store_dir(tmp_path, 'workspace_edits').glob('*.json'))


def test_limits_and_read_search_pagination(tmp_path):
    editor = WorkspaceEditor(tmp_path)
    create(editor, 'long.txt', 'x' * (PAGE + 10))
    first = editor.read('long.txt')
    assert first['next_offset'] == PAGE
    assert editor.read('long.txt', offset=PAGE)['content'] == 'x' * 10
    create(editor, 'many.txt', 'hit\n' * 105)
    page = editor.search('hit')
    assert len(page['matches']) == 100 and page['next_offset'] == 100
    assert len(editor.search('hit', offset=100)['matches']) == 5
    with pytest.raises(ToolError):
        create(editor, 'huge.txt', 'a' * (MAX_BYTES + 1))
    (tmp_path / 'workbench/invalid.txt').write_bytes(b'\xff\xfe')
    with pytest.raises(ToolError):
        editor.read('invalid.txt')


@pytest.mark.parametrize('name,text', [('bad.py', 'x = '), ('bad.json', '{'), ('POSCAR', 'invalid'),
                                     ('KPOINTS', 'invalid'), ('INCAR', '')])
def test_validation_failure_does_not_write(tmp_path, name, text):
    with pytest.raises(ToolError, match='格式检查失败'):
        create(WorkspaceEditor(tmp_path), name, text)
    assert not (tmp_path / 'workbench' / name).exists()
    assert not list(agent_paths.store_dir(tmp_path, 'workspace_edits').glob('*.before'))


def test_vasp_drafts_and_no_code_execution(tmp_path):
    editor = WorkspaceEditor(tmp_path)
    poscar = (Path(__file__).parents[1] / 'examples/Si.POSCAR').read_text()
    assert '不代表科学审阅' in create(editor, 'Si.POSCAR', poscar)['validation']
    assert '草稿格式' in create(editor, 'INCAR', 'ENCUT = 520\nISPIN = 1\n')['validation']
    assert '草稿格式' in create(editor, 'KPOINTS', 'mesh\n0\nGamma\n8 8 8\n0 0 0\n')['validation']
    create(editor, 'script.py', "raise RuntimeError('must not run')\n")
    assert '未执行' in create(editor, 'job.slurm', '#!/bin/bash\nsbatch nope\n')['validation']


def test_conflicts_preserve_external_edit(tmp_path):
    editor = WorkspaceEditor(tmp_path)
    record = create(editor)
    p = tmp_path / 'workbench/hello.py'
    p.write_text('value = 3\n')
    with pytest.raises(ToolError, match='哈希已改变'):
        editor.edit('hello.py', 'patch', record['after_sha256'], patch=patch())
    with pytest.raises(ToolError, match='文件已改变'):
        editor.revert(record['change_id'])
    with pytest.raises(ToolError):
        editor.edit('hello.py', 'patch', digest(p.read_bytes()), patch=patch())
    assert p.read_text() == 'value = 3\n'


def test_bom_crlf_and_no_final_newline(tmp_path):
    editor = WorkspaceEditor(tmp_path)
    root = tmp_path / 'workbench'
    root.mkdir()
    p = root / 'hello.py'
    p.write_bytes(b'\xef\xbb\xbfvalue = 1\r\n')
    editor.edit('hello.py', 'patch', digest(p.read_bytes()), patch=patch())
    assert p.read_bytes() == b'\xef\xbb\xbfvalue = 2\r\n'
    p.write_bytes(b'value = 1')
    delta = '--- a/hello.py\n+++ b/hello.py\n@@ -1 +1 @@\n-value = 1\n\\ No newline at end of file\n+value = 2\n\\ No newline at end of file\n'
    editor.edit('hello.py', 'patch', digest(p.read_bytes()), patch=delta)
    assert p.read_bytes() == b'value = 2'


def test_idempotency_and_request_mismatch(tmp_path):
    editor = WorkspaceEditor(tmp_path)
    record = create(editor, request_id='call-1')
    repeat = create(editor, request_id='call-1')
    assert repeat['change_id'] == record['change_id']
    assert not create(editor, request_id='call-2')['changed']
    with pytest.raises(ToolError):
        create(editor, text='value = 3\n', request_id='call-1')
    read = editor.read('hello.py')
    first = editor.edit('hello.py', 'patch', read['sha256'], patch=patch(), request_id='call-3')
    repeat = editor.edit('hello.py', 'patch', read['sha256'], patch=patch(), request_id='call-4')
    assert not repeat['changed'] and repeat['change_id'] == first['change_id']


def test_recover_after_rename_before_receipt(tmp_path, monkeypatch):
    editor = WorkspaceEditor(tmp_path)
    save = EditStore.save
    def broken(store, record):
        if record['status'] == 'applied':
            raise OSError('simulated receipt failure')
        return save(store, record)
    monkeypatch.setattr(EditStore, 'save', broken)
    with pytest.raises(OSError):
        create(editor, request_id='crash')
    assert (tmp_path / 'workbench/hello.py').read_text() == 'value = 1\n'
    monkeypatch.setattr(EditStore, 'save', save)
    recovered = create(editor, request_id='crash')
    assert recovered['current_state'] == 'applied'
    assert len(editor.changes()) == 1


def test_recover_not_applied_and_tampered_backup(tmp_path, monkeypatch):
    editor = WorkspaceEditor(tmp_path)
    import vasp_agent.editing.edit_store as edit_store
    write = edit_store.atomic_write
    def broken(path, data):
        if Path(path).parent.name == 'workbench':
            raise OSError('simulated write failure')
        return write(path, data)
    monkeypatch.setattr(edit_store, 'atomic_write', broken)
    with pytest.raises(OSError):
        create(editor, request_id='before-write')
    record = editor.changes()[0]
    assert editor.get(record['change_id'])['current_state'] == 'not_applied'
    monkeypatch.setattr(edit_store, 'atomic_write', write)
    create(editor, request_id='before-write')
    (agent_paths.store_dir(tmp_path, 'workspace_edits') / (record['change_id'] + '.after')).write_text('tamper')
    with pytest.raises(ToolError, match='备份哈希'):
        editor.revert(record['change_id'])


def test_lock_busy_and_independent_files(tmp_path):
    editor = WorkspaceEditor(tmp_path)
    with editor.store.lock():
        with pytest.raises(ToolError, match='另一个编辑'):
            create(editor)
    create(editor, 'ok.txt', 'ok')
    with pytest.raises(ToolError):
        create(editor, 'bad.json', '{')
    assert editor.read('ok.txt')['content'] == 'ok'


def test_reparse_attribute_is_rejected_even_without_symlink_privilege(tmp_path, monkeypatch):
    from types import SimpleNamespace as NS
    (tmp_path / 'workbench').mkdir()
    alias = tmp_path / 'workbench/junction'
    alias.mkdir()
    lstat = Path.lstat
    def flagged(path):
        actual = lstat(path)
        if path == alias:
            return NS(st_mode=actual.st_mode, st_file_attributes=0x400, st_nlink=1)
        return actual
    monkeypatch.setattr(Path, 'lstat', flagged)
    with pytest.raises(ToolError, match='reparse'):
        create(WorkspaceEditor(tmp_path), 'junction/file.txt')


def test_multiple_hunks_and_empty_file_insertion(tmp_path):
    editor = WorkspaceEditor(tmp_path)
    first = create(editor, 'a.txt', 'a\nb\nc\nd\n')
    delta = '--- a/a.txt\n+++ b/a.txt\n@@ -1 +1 @@\n-a\n+A\n@@ -4 +4 @@\n-d\n+D\n'
    editor.edit('a.txt', 'patch', first['after_sha256'], patch=delta)
    assert editor.read('a.txt')['content'] == 'A\nb\nc\nD\n'
    empty = create(editor, 'empty.txt', '')
    editor.edit('empty.txt', 'patch', empty['after_sha256'], patch='--- a/empty.txt\n+++ b/empty.txt\n@@ -0,0 +1 @@\n+first\n')
    assert editor.read('empty.txt')['content'] == 'first\n'


def test_diff_pagination_and_undo_crash(tmp_path, monkeypatch):
    editor = WorkspaceEditor(tmp_path)
    result = create(editor, 'large.txt', 'a' * (PAGE + 20))
    first = editor.get(result['change_id'])
    assert first['next_offset'] == PAGE
    assert editor.get(result['change_id'], PAGE)['next_offset'] is None
    save = EditStore.save
    def broken(store, record):
        if record['status'] == 'undone':
            raise OSError('undo receipt interruption')
        return save(store, record)
    monkeypatch.setattr(EditStore, 'save', broken)
    with pytest.raises(OSError):
        editor.revert(result['change_id'])
    monkeypatch.setattr(EditStore, 'save', save)
    assert editor.revert(result['change_id'])['current_state'] == 'undone'


def test_protected_store_alias_and_changed_sensitive_current(tmp_path):
    editor = WorkspaceEditor(tmp_path)
    record = create(editor)
    target = tmp_path / 'workbench/hello.py'
    target.write_text('password = "now-sensitive"')
    with pytest.raises(ToolError, match='敏感内容'):
        editor.revert(record['change_id'])
    assert 'now-sensitive' in target.read_text()
    assert 'now-sensitive' not in ''.join(p.read_text(encoding='utf-8') for p in agent_paths.store_dir(tmp_path, 'workspace_edits').glob('*.before'))


def test_undo_does_not_certify_original_invalid_version(tmp_path):
    editor = WorkspaceEditor(tmp_path)
    (tmp_path / 'workbench').mkdir()
    path = tmp_path / 'workbench/demo.py'
    path.write_text('x =\n')
    record = editor.edit('demo.py', 'patch', digest(path.read_bytes()), patch=patch('demo.py', 'x =', 'x = 1'))
    assert '语法通过' in record['validation']
    restored = editor.revert(record['change_id'])
    assert path.read_text() == 'x =\n'
    assert '不适用于当前内容' in restored['validation']
