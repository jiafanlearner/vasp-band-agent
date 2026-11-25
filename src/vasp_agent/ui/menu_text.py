"""Plain-text layout for operator menus: aligned tables and short bullet lists.

Menus are read under time pressure before an irreversible choice, so values go
in aligned rows rather than JSON, and model prose is split into short lines.
"""
import re
import unicodedata


def width(text):
    """Terminal columns: CJK and full-width characters take two."""
    return sum(2 if unicodedata.east_asian_width(c) in 'WF' else 1 for c in str(text))


def pad(text, columns):
    text = str(text)
    return text + ' ' * max(0, columns - width(text))


def table(rows, indent='  '):
    """Rows of cells; every column except the last is padded to its widest cell."""
    rows = [[str(cell) for cell in row] for row in rows]
    count = max(len(row) for row in rows)
    widths = [max(width(row[i]) for row in rows if i < len(row)) + 2 for i in range(count - 1)]
    lines = []
    for row in rows:
        cells = [pad(cell, widths[i]) if i < len(row) - 1 else cell for i, cell in enumerate(row)]
        lines.append((indent + ''.join(cells)).rstrip())
    return '\n'.join(lines)


def points(text):
    """Split prose into short lines at sentence and clause breaks."""
    parts = re.split(r'(?<=[。；;])\s*|\n+', text or '')
    return [p.strip(' ；;。') for p in parts if p.strip(' ；;。')]


def bullets(lines, shown=None, more='输入 d 查看完整理由'):
    shown = len(lines) if shown is None else shown
    text = '\n'.join(f'  · {line}' for line in lines[:shown])
    if len(lines) > shown:
        text += f'\n  （另有 {len(lines) - shown} 条，{more}）'
    return text


def options(items):
    return '\n'.join(f'  {key}  {label}' for key, label in items)


def printable(value):
    """Replace non-printable control characters (except newline) with spaces."""
    return ''.join(c if c.isprintable() or c == '\n' else ' ' for c in str(value))
