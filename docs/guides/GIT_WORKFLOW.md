# 本地 Git 版本管理

本仓库于2026-09-29建立，首次提交是当前代码基线。此前更新的说明见 docs/archive/2026-10/HANDOFF_NEW_FEATURES.md；早期逐次代码差异无法由首次提交还原。

Git 在本机即可使用。公开仓库为 https://github.com/jiafanlearner/vasp-band-agent ，远程名称为 origin。提交保存在本机，只有执行 push 才会上传。

## 日常更新

在vasp-band-agent目录内执行：

```sh
git status --short
git diff
python -m pytest -q
git add <本次改动的文件或目录>
git diff --cached --stat
git diff --cached
git commit -m "说明本次修改的目的"
```

每次完成一组功能或修复，更新交接文档和测试结果，再保存一个提交。Git不会在每次编辑时自动提交。避免把未完成的多个功能混在同一提交。

## 查看与恢复

```sh
git log --oneline --decorate -10
git show <提交ID> --stat
git diff <旧提交ID> <新提交ID>
```

需要撤销已提交更新时，先处理当前未提交修改，再使用 `git revert <提交ID>` 追加反向提交，保留历史。`git restore --source=<提交ID> -- <文件>` 会覆盖该文件当前内容，执行前先保存需要保留的改动。不要用 `git reset --hard` 或 `git clean` 作为常规恢复手段。

## 不进入版本历史的内容

.env、密钥、实际服务器配置及备份、对话和审计日志、资源偏好、计算任务及下载结果、工作草稿、编辑/运行回执、赝势数据和缓存均忽略。源代码、测试、文档、样例和不含密钥的模型配置纳入版本管理。

首次公开发布从清理后的当前版本建立独立历史。此前本地历史保存在 `codex/local-history-before-github` 分支；该分支包含未经清理的历史环境信息，只保留在本机，不要执行 `git push --all` 或 `git push --mirror`。

后续发布先检查差异和忽略文件，再运行 `git push origin main`。`.gitignore` 不能从已提交的历史中移除秘密；不要使用 `git add -f` 强制加入敏感文件。
