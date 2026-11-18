# 本地 PBE 赝势库

将已有的同一套 PBE 赝势按以下目录放置（不附带或下载赝势正文）：

```
potentials/PBE/
  Si/POTCAR
  O/POTCAR
  Fe/POTCAR
  Fe_pv/POTCAR
  potentials.toml
```

AI 启动时自动发现本工作区的 `potentials/PBE`；也可用 `--potcar-library /已有/PBE/目录`，无需复制整个库。目录在 AI 运行的机器上，本地进程不能直接读取集群路径。

默认使用与元素同名的目录，例如 Si → Si/POTCAR。变体需在本目录 `potentials.toml` 明确映射：

```toml
[potentials]
Fe = "Fe_pv"
Ti = "Ti_sv"
```

没有默认赝势时，程序列出可见变体并停止，不自动猜测。映射只用于选择，不修改任何 POTCAR 内容。生成时按 POSCAR 的元素块顺序拼接，验证 PBE/元素，记录 TITEL、ENMAX、来源及 SHA-256 到任务的 `potential-selection.json`。AI 只读取元数据，不接收赝势正文。
