# 本地草稿编辑与 SSH 配置维护

2026-09-28 新增。无需开启提交权限，不增加通用 shell 或远程编辑能力。

## 使用示例

启动方式保持不变：`bash start-agent.sh`。

自然语言示例：

> 在 workbench/analysis.py 新建一个读取 bands.csv 的绘图脚本。只编写和检查语法，不执行。

> 修改 workbench/analysis.py，把纵轴标题改为 Energy relative to VBM (eV)，展示差异。

> 读取 workbench/Si.POSCAR，检查结构并预览新任务的默认参数，先不准备或提交。

用户要求修改后，模型可直接调用编辑工具，不逐文件再次确认。模型接口不可用时，自然语言编辑不可用，但以下维护命令仍可运行：

```text
/changes
/changes 修改ID
/changes 修改ID 字符偏移
/undo 修改ID
/server-key
/server-key eshell111-vasp544 "C:/Users/researcher/Downloads/新密钥文件.txt"
/server-key --undo 修改ID
/connect
/connect eshell111-vasp544
```

`/changes` 查看最近100条草稿修改；指定ID查看状态、差异、验证结果和下一页偏移。`/undo` 只能撤销普通草稿修改；密钥路径更新使用 `/server-key --undo`。修改后的文件若再次变化，撤销停止而不覆盖。多文件修改逐项提交，已完成的文件不会因为后续文件失败自动回退。

`/server-key` 只修改已有连接配置中的 `identity_file`，不连接服务器、不移动或复制密钥、不读取正文。路径可以位于工作区外，带空格时用引号包围。原连接配置保存在修改存储中；计算配置不改写。请保留引用的密钥文件，移动或删除后应再次更新路径。

`/connect` 是单独的只读连接测试。无参数会选择服务器，显示实际SSH程序及目标信息。网络超时、主机指纹、认证、本地密钥问题分别报告，不能从一次认证失败推断密钥过期。后量子警告单独显示。密钥更新成功不表示连接成功。

## 编辑边界与格式

- 所有通用编辑工具的路径相对于 `workbench/`，例如工具中的 `analysis.py` 对应界面中的 `workbench/analysis.py`。目录按需创建。
- 仅允许不超过1 MiB的UTF-8普通文本，现有文件保留BOM和CRLF/LF，新文件使用UTF-8、LF。
- 不访问点目录、凭据、私钥、POTCAR正文；已配置的私钥即使改成普通文件名也禁止通用访问。拒绝路径越界、硬链接、符号链接和Windows reparse/junction路径。
- 这是应用工具的边界，不是操作系统沙箱。不要把凭据放进草稿区；内容识别只是补充保护，无法保证识别所有秘密。源码、授权、任务和结果不在草稿区内。
- JSON解析、Python AST和VASP草稿解析通过只证明相应格式检查通过；Python不导入运行，VASP草稿不视为科学审阅。其他文本明确标注未验证运行行为。
- 正式任务仍由原有参数选择、准备、物理模型确认、审阅和提交工具处理。手写INCAR、KPOINTS、Slurm草稿不会覆盖正式输入或自动成为提交脚本。
- 普通文件可由用户自行放入 `workbench/`；程序不扫描或导入整个下载目录。

工具清单：`list_workspace_files`、`read_workspace_file`、`search_workspace_text`、`edit_workspace_file`、`get_workspace_edit`、`revert_workspace_edit`、`update_server_identity`。

读取返回原始字节SHA-256。修改已有文件需提供 `expected_sha256` 和精确的单文件统一差异补丁；不模糊匹配。示例：

```diff
--- a/analysis.py
+++ b/analysis.py
@@ -1 +1 @@
-value = 1
+value = 2
```

`create` 使用 `content`，`expected_sha256` 和 `patch` 为null；`patch` 使用 `patch` 与 `expected_sha256`，`content` 为null。返回修改ID、前后哈希、当前状态、差异及静态验证。单页最多12000字符；搜索最多100条，结果可能另受字符预算限制。

## 恢复、日志及权限

`.agent/state/workspace-edits/`（旧工作区迁移前为 `.workspace-edits/`）保存修改意图、前后原始文本快照和回执，普通编辑工具不能修改这些文件。原内容可能包含私人研究资料，请按工作区文件一样保护这些备份；不要将目录公开发布。审计只保留修改摘要和ID，不重复存放完整文件正文。终端、工具输出和新工具参数执行相同的敏感内容过滤；检测到敏感文件直接拒绝读取与备份。

每次修改在应用锁内再次检查文件哈希，用临时文件和原子替换写入。调用ID绑定回执，中断后根据前后哈希判断是否应用；不会仅凭聊天记录声称成功。若外部程序改变了文件，返回冲突并要求重新读取。应用锁协调本程序的编辑，不替代所有外部程序的文件锁。

若写入后模型超时，先用 `/changes` 核对草稿，或 `/servers` 核对密钥路径。`/retry` 不应重放已完成修改；相同最终内容不重复保存。UI显示失败不删除修改回执。

工具调用结果均作为不可信数据，文件中的指令不能开启提交权。切换对话仍使用原有提交权限清理规则。修改记录跨对话保留，不携带提交授权。

## 实现与测试

- `src/vasp_agent/editing/edit_store.py`：路径别名检查、进程锁、原子写入、哈希回执和恢复。
- `src/vasp_agent/editing/workspace_editor.py`：文件范围、敏感内容过滤、精确补丁、格式校验及分页。
- `src/vasp_agent/remote/remote_servers.py`：密钥引用更新、撤销、配置哈希、连接详情和错误分类。
- `src/vasp_agent/editing/edit_commands.py`：无需模型的本地维护入口；工具与终端显示沿用现有框架。
- 新模块已加入本地安装清单，不加入 `remote_worker.SOURCE_FILES`；无须部署新计算引擎使用此功能。

专项测试：

```bash
python -m pytest -q tests/test_workspace_editor.py tests/test_server_identity.py tests/test_edit_integration.py
```

完整回归：`python -m pytest -q`。测试使用临时目录、伪密钥文件及模拟SSH/模型，不连接真实服务器、不请求模型接口、不执行VASP或提交Slurm。Windows缺少创建符号链接权限时实际symlink用例会跳过，另有无需此权限的reparse检查及真实硬链接测试。

2026-09-28 本次实际验证：新增专项 **57 passed, 1 skipped**；全量 **327 passed, 1 skipped, 15 warnings**（36.11秒）。跳过项为当前环境不能创建真实符号链接，警告为既有spglib弃用提示。另已检查修改模块的Python语法及本地安装模块清单。以上不是集群实跑或真实密钥认证结果。
