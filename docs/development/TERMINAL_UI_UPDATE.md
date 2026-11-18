# 终端回复与工作过程显示

## 2026-09-28 表格间距与窄窗口布局

后续补充正文：统一将回复和过程说明的显示宽度限制为最多112个字符单元。Rich原本按空格分词，会把连续中文当成长单词，造成任务名之后过早断行；新增中文边界换行处理，保留任务名、英文参数、强调样式和明确换行。超长标识仅在超过窗口宽度时折行，保存的原文不变。离线样例包含中英文混排的确认短语，可用 `--width 180` 验证宽窗口不会铺满整屏。

新增 `src/vasp_agent/ui/markdown_layout.py`，只替换本地 Markdown 渲染器中的表格元素，不修改 Rich 全局注册表或对话原文。回复和工作过程说明使用同一渲染规则：表格行之间留空行、列之间保留间距、表格最多占112个终端字符单元；空间不足时按原表头逐项显示字段，保留全部数值和行内代码样式。长说明自动折行，不用省略号隐藏内容。终端字体和字形仍由 Git Bash/Windows Terminal 设置决定。

离线查看宽窄两种样式，无需模型接口或服务器：

```bash
python -X utf8 examples/terminal_preview.py --width 100
python -X utf8 examples/terminal_preview.py --width 52
```

`tests/test_markdown_layout.py` 覆盖32至160列、中英文混排、长说明、完整数值、窄屏逐项布局、行间距、转义管道、空字段、多个表格及代码块不被误处理。宽屏HTML样例同步更新于 [终端预览](../previews/terminal-preview.html)。

## 初版实现（历史记录）

本次修改以截图中的 CLI 风格为参考，保留现有对话和菜单架构，不替换为全屏 TUI。

- `terminal_ui.py`：Rich 渲染回复中的标题、粗体、表格、列表和代码；正文使用默认色。真实工具执行显示开始、目标、成功/失败、耗时及摘要，详情缓存经过显示脱敏，最多 100 条、每条结果 16000 字符。
- `ai_agent.py`：在模型请求前提示等待，展示随工具调用返回的公开文字，在实际工具调用边界记录过程；恢复读取独立显示。渲染异常不丢弃完成的工具结果，不重放写操作。模型适配器和提交权限不变。
- `extra_commands.py`、`interactive_prompt.py`：增加本地 `/trace [编号]` 与主输入 Ctrl+T。显示最近过程时不提交或丢失当前输入；参数菜单仍隔离自己的提示和按键。
- `pyproject.toml`、`requirements-tested.txt`：声明 Rich 显示依赖，本机验证版本 15.0.0。没有新增远程核心模块，无需远程部署。
- `examples/terminal_preview.py`：固定演示数据的离线界面预览，可导出 HTML；不调用模型、SSH、VASP 或准备工具。
- `docs/guides/AI_USAGE.md`：更新显示方式、使用方法及限制。

升级本机依赖并重启：

```bash
python -m pip install -e ".[ai]"
python ai_agent.py --profile relay
```

只看界面样例：

```bash
python -X utf8 examples/terminal_preview.py
```

无颜色用 `--color never`。普通输入后端可用 `/trace`；Ctrl+T 仅在支持 prompt_toolkit 的主输入界面有效。该实现展示工具事件和已返回的公开说明，不展示内部推理，不包含逐 token 流式响应、全屏状态栏或菜单内的记录快捷键。工具耗时包括交互菜单等待时间。

验证：`python -m pytest -q` 为 **207 passed，15 个已有 spglib 弃用警告**。新增 9 项测试，覆盖 Markdown 排版、中文宽度、颜色开关、GBK 输出、显示脱敏、记录截断、真实事件顺序、Ctrl+T 输入保留及渲染故障不丢失工具结果等；原有菜单切换和恢复测试通过。运行离线样例并检查输出，生成 `docs/previews/terminal-preview.html`。未使用真实模型或连接集群，未部署或提交计算；Git Bash 的实际观感可用上述样例验证。
