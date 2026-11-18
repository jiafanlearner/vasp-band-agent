"""Readable terminal Markdown without changing canonical conversation content."""
import re
from rich import box
from rich.cells import cell_len, chop_cells
from rich.markdown import Markdown, Paragraph, TableElement
from rich.table import Table
from rich.text import Text

TEXT_WIDTH = 112
_CJK = '\u3040-\u30ff\u3400-\u9fff\uac00-\ud7af\uf900-\ufaff\U00020000-\U0002fa1f'
_TOKENS = re.compile(
    rf'[（【《「『“‘]*[{_CJK}][，。！？；：、）】》」』”’…]*|[^\s{_CJK}]+|[^\S\n]+|\n')


def wrap_prose(text, width):
    """Add display-only breaks at CJK boundaries, retaining Rich style spans.

    Rich normally wraps at whitespace, which treats an entire Chinese sentence
    as one word. ASCII identifiers stay together unless wider than the window.
    """
    if width < 2 or not re.search(f'[{_CJK}]', text.plain):
        return text.copy()
    breaks, used = [], 0
    for match in _TOKENS.finditer(text.plain):
        token = match.group()
        if token == '\n':
            used = 0
            continue
        length = cell_len(token)
        if used and used + length > width:
            breaks.append(match.start())
            used = 0
        if length > width:
            offset = match.start()
            chunks = chop_cells(token, width)
            for chunk in chunks[:-1]:
                offset += len(chunk)
                breaks.append(offset)
            used = cell_len(chunks[-1])
        else:
            used += length
    return Text('\n').join(text.divide(breaks)) if breaks else text.copy()


class ReadableParagraph(Paragraph):
    def __rich_console__(self, console, options):
        text = wrap_prose(self.text, options.max_width)
        text.justify = self.justify
        # Explicit breaks already implement CJK wrapping; prevent Rich from
        # moving a Chinese phrase again using its whitespace-only algorithm.
        text.no_wrap = bool(re.search(f'[{_CJK}]', text.plain)) and options.max_width >= 2
        yield text


class ReadableTable(TableElement):
    def __rich_console__(self, console, options):
        headings = [cell.content for cell in self.header.row.cells] if (
            self.header is not None and self.header.row is not None) else []
        rows = [[cell.content for cell in row.cells] for row in self.body.rows] if self.body else []
        columns = max([len(headings), *(len(row) for row in rows)], default=0)
        if not columns:
            return
        width = min(options.max_width, TEXT_WIDTH)
        if width < max(60, columns * 26) and rows:
            # A compressed multi-column table is hard to scan in a narrow window.
            # Keep every field, including styled code, as a wrapped labelled line.
            for index, row in enumerate(rows):
                if index:
                    yield Text('')
                for column, cell in enumerate(row):
                    label = headings[column].plain if column < len(headings) else f'第 {column + 1} 列'
                    line = Text(label + '：', style='markdown.table.header')
                    # Do not let the label's style bleed into the field value.
                    line = Text.assemble(line, cell.copy())
                    line.overflow = 'fold'
                    yield line
            return
        table = Table(box=box.SIMPLE, pad_edge=False, padding=(0, 1),
                      collapse_padding=False, leading=1,
                      width=width,
                      style='markdown.table.border')
        for index in range(columns):
            heading = headings[index].copy() if index < len(headings) else Text('')
            heading.stylize('markdown.table.header')
            table.add_column(heading, overflow='fold', no_wrap=False)
        for row in rows:
            table.add_row(*(cell.copy() for cell in row))
        yield table


class TerminalMarkdown(Markdown):
    # Copy the registry: changing Markdown.elements globally affects other users.
    elements = {**Markdown.elements, 'table_open': ReadableTable,
                'paragraph_open': ReadableParagraph}

    def __rich_console__(self, console, options):
        yield from super().__rich_console__(console, options.update(width=min(options.max_width, TEXT_WIDTH)))
