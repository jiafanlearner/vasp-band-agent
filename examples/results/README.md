# Si PBE 能带示例 / Silicon band structure example

![Si PBE band structure](../../docs/assets/si-band-structure.png)

这份图和 [si-bands.csv](si-bands.csv) 来自本地保存的真实 VASP Si 完整工作流结果，不是模拟曲线。本次发布只整理已有结果，没有重新运行 VASP。

| 项目 / Item | 数值 / Value |
| --- | --- |
| 材料 / Material | Si |
| 方法 / Method | PBE，非磁性 / nonmagnetic |
| VASP 版本 / Version | 5.4.4.18apr17-6-g9f103f2a35 |
| 路径采样 / Path samples | 400 k points, 16 bands, 1 spin channel |
| 采样路径带隙 / Sampled-path gap | 0.6104 eV，间接 / indirect |
| SCF Fermi energy | 5.92828368 eV |
| 绘图零点 / Plot reference | VBM = 5.6164 eV |

保存报告中的能带检查通过：本征值全部有限，最高能带在所有采样点为空。CSV 同时保留相对 SCF Fermi 能和绘图参考能的列，单位为 eV；路径距离单位为 Å⁻¹。

图以 `bands.csv`、SCF 结构和报告为依据，使用本项目的 `band_plot.replot_folder` 重绘；保留原始数值，重绘前核验本地下载收据中的 SHA-256。重绘不触发 VASP 计算。曲线数据文件的 SHA-256：

```text
89a695486fbef70a193c9fb9a0ae44ce4574a2c04ad3148be0aa83a13c72184a
```

This is an existing VASP silicon calculation, replotted with this project's plotting module after validating the local download receipt. The numerical CSV is unchanged. No VASP calculation was performed for this publication. The plotted energy zero is the VBM. The sampled path can miss global band extrema, and a PBE Kohn–Sham gap is not an experimental quasiparticle gap.
