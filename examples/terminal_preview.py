"""Offline terminal style preview. No model, SSH, task preparation or submission."""
import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from vasp_agent.ui.terminal_ui import TerminalUI


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--color', choices=['auto', 'always', 'never'], default='auto')
    parser.add_argument('--html', help='Optional HTML preview output path')
    parser.add_argument('--width', type=int, default=100, help='Preview width in terminal cells (at least 20)')
    args = parser.parse_args()
    if args.width < 20:
        parser.error('--width must be at least 20')
    ui = TerminalUI(args.color)
    console = ui.console()
    console.width = args.width
    ui.console = lambda stream=None: console
    if args.html:
        console.record = True
    ui.activity('离线界面演示 · 以下工具事件均为固定样例，未执行计算或远程操作')
    ui.commentary('先查看输入摘要，再检查当前阶段；这里仅展示终端样式。')
    first = ui.tool_started('get_input_review', {'server': 'demo-server', 'job_name': 'demo-si'})
    ui.tool_finished(first, {'ok': True, 'result': {'status': 'prepared', 'ready': True,
        'scans': {'encuts': [520, 598, 676], 'meshes': [[8,8,8], [10,10,10], [12,12,12]]}}})
    second = ui.tool_started('get_stage_review', {'server': 'demo-server', 'job_name': 'demo-si'})
    ui.tool_finished(second, {'ok': False, 'error_code': 'PHYSICS_CONFIRMATION_REQUIRED',
        'error': '样例任务未确认物理模型。', 'next_step': '先由操作者确认；只读输入摘要仍可查看。'})
    ui.reply('''### 输入摘要（演示数据）

**仅展示界面，没有启动计算。** 默认参数尚未通过收敛验证。

重点样式：**固定 ENCUT＝676 eV，只扫描 k 网格**。普通正文保持终端默认颜色。

正文换行样例：**“提交 demo-si 的收敛测试阶段”**。收到确认后先核对实际资源与授权上限、给出资源建议，再进入提交表单；本次只提交收敛测试，不会自动启动后续优化、SCF 或能带阶段。此处仅演示排版，没有执行提交。

> 适用条件：这里只展示提示样式，不代表已有收敛结果。

| 项目 | 当前值 | 说明 |
|---|---|---|
| ENCUT | 520 / 598 / 676 eV | 默认扫描序列 |
| k 网格 | 8³ / 10³ / 12³ | 需要实际收敛测试 |
| NBANDS | 16 | 仍需检查占据与空带 |
| 资源配置 | 1 节点、每组 8 核、1 组测试 | 沿用服务器配置，非提交确认 |

输入参数示例：

```ini
ENCUT = 520
NBANDS = 16
ISPIN = 1
GGA = PE
```

- 阶段放行仍需要物理模型确认。
- 输入 `/trace 1` 查看执行详情；主输入处按 **Ctrl+T** 查看最近过程。
''')
    if args.html:
        console.save_html(args.html)


if __name__ == '__main__':
    main()
