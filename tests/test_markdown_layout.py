"""Mixed Chinese/ASCII tables must stay readable without losing model content."""
import io

import pytest
from rich.cells import cell_len
from rich.console import Console
from rich.markdown import Markdown, TableElement

from vasp_agent.ui.terminal_ui import TerminalUI


TABLE = '''| 参数 | 准备值 | 来源与状态 |
|---|---|---|
| ENCUT 扫描 | 520、598、676 eV | 工具默认，待验证收敛 |
| k 网格扫描 | 8×8×8、10×10×10、12×12×12 | 工具默认，待验证收敛 |
| NBANDS | `16` | AI 暂定，需核查目标空带范围 |
| 资源配置 | 1 节点、每组 8 核、1 组测试 | 沿用服务器配置，非提交确认 |
'''


def render(text, width, method='reply'):
    output = io.StringIO()
    ui = TerminalUI('never')
    console = Console(file=output, width=width, color_system=None, legacy_windows=False)
    ui.console = lambda: console
    getattr(ui, method)(text)
    return output.getvalue()


@pytest.mark.parametrize('width', [32, 52, 76, 80, 100, 160])
@pytest.mark.parametrize('method', ['reply', 'commentary'])
def test_tables_preserve_values_and_fit_window(width, method):
    result = render(TABLE, width, method)
    assert max(cell_len(line) for line in result.splitlines()) <= min(width, 112)
    compact = ''.join(result.split())
    for value in ('520、598、676eV', '8×8×8、10×10×10、12×12×12',
                  'AI暂定，需核查目标空带范围', '1节点、每组8核、1组测试',
                  '沿用服务器配置，非提交确认'):
        assert value in compact
    assert '`16`' not in result and '\x1b' not in result
    if width < 78:
        assert '参数：' in result and '准备值：' in result and '来源与状态：' in result
    else:
        lines = result.splitlines()
        first = next(i for i, line in enumerate(lines) if 'ENCUT 扫描' in line)
        second = next(i for i, line in enumerate(lines) if 'k 网格扫描' in line)
        assert any(not line.strip() for line in lines[first + 1:second])
    assert '`16`' in TABLE


def test_two_column_long_notes_and_inline_code():
    text = '''| 参数 | 说明 |
|---|---|
| 初始扫描 | `NSW=0、IBRION=-1、ICHARG=2`，在未优化结构上进行静态自洽测试 |
| EDIFF | 默认 `1×10⁻⁷ eV`，用于电子自洽收敛 |
'''
    compact = ''.join(render(text, 64).split())
    assert 'NSW=0、IBRION=-1、ICHARG=2' in compact
    assert '在未优化结构上进行静态自洽测试' in compact
    assert '1×10⁻⁷eV' in compact


def test_fenced_tables_remain_code_and_global_rich_registry_unchanged():
    result = render('```text\n| a | b |\n|---|---|\n```', 80)
    assert '| a | b |' in result and '|---|---|' in result
    assert Markdown.elements['table_open'] is TableElement


def test_escaped_pipe_empty_field_and_multiple_tables():
    text = '| 名称 | 内容 |\n|---|---|\n| A | x\\|y |\n| B | |\n\n分隔正文\n\n' + TABLE
    result = render(text, 52)
    assert 'x|y' in result and '名称：B' in result
    assert result.index('名称：B') < result.index('分隔正文') < result.index('参数：ENCUT')


CONFIRMATION = ('请明确确认：**“提交 Si_pbe_cell 的收敛测试阶段”**。'
                '收到后我会先核对实际资源与授权上限、给出资源建议，再进入提交表单；'
                '本次只提交收敛测试，不会自动启动后续优化、SCF 或能带阶段。')


@pytest.mark.parametrize('width', [32, 64, 112, 180])
@pytest.mark.parametrize('method', ['reply', 'commentary'])
def test_chinese_confirmation_wraps_without_stranding_job_name(width, method):
    result = render(CONFIRMATION, width, method)
    lines = [line for line in result.splitlines() if line.strip() and '● 助理' not in line]
    assert max(cell_len(line) for line in lines) <= min(width, 112)
    if width >= 64:
        assert '提交 Si_pbe_cell 的收敛测试阶段”' in lines[0]
    assert 'Si_pbe_cell' in result
    assert ''.join(''.join(lines).split()) == ''.join(CONFIRMATION.replace('**', '').split())


def test_prose_styles_and_explicit_line_breaks_are_preserved():
    from rich.text import Text
    from vasp_agent.ui.markdown_layout import wrap_prose
    original = Text('请确认 Si_pbe_cell 后继续核查输入。\n这一行保持换行。')
    original.stylize('bold cyan', 4, 15)
    wrapped = wrap_prose(original, 24)
    assert '\n这一行' in wrapped.plain
    assert wrapped.plain.replace('\n', '') == original.plain.replace('\n', '')
    start = wrapped.plain.index('Si_pbe_cell')
    assert any(span.start <= start < span.end and span.style == 'bold cyan' for span in wrapped.spans)
    assert original.plain == '请确认 Si_pbe_cell 后继续核查输入。\n这一行保持换行。'


def test_nested_list_and_long_ascii_token_are_not_clipped():
    long_path = 'workbench/' + 'analysis_' * 12 + '.py'
    text = '- 请核查 `Si_pbe_cell` 的收敛测试参数。\n  - 草稿文件：`' + long_path + '`，尚未执行。'
    result = render(text, 40)
    assert max(cell_len(line) for line in result.splitlines()) <= 40
    assert long_path in ''.join(result.split())
    assert 'Si_pbe_cell' in result and '尚未执行' in ''.join(result.split())
