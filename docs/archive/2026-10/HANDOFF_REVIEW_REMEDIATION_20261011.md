# 交接报告：代码评审整改（2026-10-11）

本文件写给接替我继续这项工作的人。它记录**已经改了什么、刻意没改什么、以及为什么**，目的
是让你不必从 `git diff` 反推。

原始评审原文保存在同目录 [REVIEW_FINDINGS_20261011.txt](REVIEW_FINDINGS_20261011.txt)（335 行，
三份独立报告：remote worker 层、chat/agent loop 层、workflow+permissions 层）。本报告中
所有"remote#N / chat#N / wf#N"编号都指那份文件里的编号。**离开那份文件，本报告的编号无法对照。**

---

## 0. 先读这一段（三条硬约束）

### 0.1 全部改动未提交

工作树的 `src/` + `tests/` 共 **40 个文件、2131 行插入 / 294 行删除**（`src/` 21 文件
+1609/-267，`tests/` 19 文件 +522/-27）；连同 docs 等全部改动合计 47 文件 +2389/-319。

**没有任何 commit，没有 push。** 上一轮的用户指示是"按照你的想法进行工作"，不含提交授权。
是否提交、拆成几个 commit、是否先建分支，由你决定 —— 但请先读完第 4 节，那里有一批改动是
**策略选择而非机械修复**，可能需要你先确认再固化。

### 0.2 必须跑全量测试，不能用定向测试代替

```bash
python -m pytest -q
```

当前状态：**1046 passed, 1 skipped, 15 warnings**（≈120–130s；15 个 warning 全是 spglib 的
既有弃用警告，与本次改动无关）。

**不要传 `--timeout`** —— 本环境未安装 `pytest-timeout`。

这条约束是我用代价换来的，见第 5 节。定向跑 `test_run_improvements.py test_remote_compute.py
test_remote_array.py test_tool_recovery.py test_submission_recovery.py` 曾经 160 passed 全绿，
而全量跑出 1 个失败 —— 那个失败路径只有全量才覆盖到。

### 0.3 Windows 环境细节

- 平台 win32，Python 3.12，shell 是 Git Bash。
- 默认文件编码 GBK。**读含中文的源码必须显式 `encoding='utf-8'`**，否则 `UnicodeDecodeError`
  或乱码。
- 测试里的路径是 Windows 形式（`C:\...`、`tmp_path`）。任何新写的路径判断都要用
  `Path(x).is_absolute()`，**不要**写 `x.startswith('/')` —— 见第 5 节第 2 条。

---

## 1. 层与关键机制（新接手时的最小上下文）

`vasp-agent` v0.7.0：可审计的 PBE SCF→能带结构 agent（DFT/VASP）。分层：
`chat`、`common`、`editing`、`models`、`monitoring`、`permissions`、`planning`、`remote`、`ui`、`workflow`。

理解本次改动必须知道的四件事：

**（1）remote worker 以单个 stdin 程序下发。**
[remote_compute.py:69-78](../../src/vasp_agent/remote/remote_compute.py) 读取 `remote_worker.py`
全文，前面拼上 `'import io, sys\nsys.stdin = io.StringIO(' + repr(payload) + ')\n'`，整体作为
stdin 喂给远端 Python。这是**刻意**把多行 Python 挡在 Windows argv → SSH → shell 的引号链之外。
所以 worker 是单文件；任何拆分必须能扛住这种捆绑（见 remote#9）。

**（2）`engine_module(name)` 的 `__package__` 分派。**
[remote_worker.py:580](../../src/vasp_agent/remote/remote_worker.py)：

```python
return import_module(ENGINE_MODULES[name] if __package__ else name)
```

下发后的 stdin worker 用扁平启动器加载 release；本地包用户（含测试）用限定模块名。
`__package__` 的真假就是"我现在是捆绑 worker 还是本地包"的判据。**测试会 monkeypatch 这个值**，
见 `tests/test_remote_array.py` 的 `test_array_rejects_bound_flat_release_missing_array_engine`。

**（3）`sys.path.insert(0, release)` 是 release 绑定机制，共七处。**
每处都在改全局解释器状态。remote#9 指出这是真实隐患：一个 worker 进程内针对两个不同 release
准备的两个任务，会静默共用"谁后插谁赢"的那条路径。本轮我**只做了收口**（见第 2 节
`engine_release`），**没有做** remote#9 建议的 `engine_module(action_context)` 重构与模块拆分。

**（4）先绑定后加载。**
`validate_scheduler(cfg)` 在昂贵的 `prepare_full` **之前**调用；`meta['script_sha256'] !=
sha(folder / 'submit.slurm')` 在任何资源或阶段操作之前重新把任务绑回它的脚本。

---

## 2. 已修条目（逐条，含锚点）

行号是**当前工作树**的行号，未提交。

### 2.1 remote 层

| 编号 | 问题 | 修法 | 锚点 |
|---|---|---|---|
| remote#1 | 复用分支把未校验的 partition/walltime 直接拼进 `submit.slurm` | 抽出 `validate_scheduler(cfg)`，两个建脚本分支都调 | `remote_worker.py:825`，调用点 `:1528`、`:1597` |
| remote#2 | 锁与 sbatch 之间被杀则任务永久卡死，且恢复路径被同一把锁挡住 | 改为 OS 建议锁（`fcntl.flock`），进程死即释放；`lock_is_held` 判活 | `open_lock:102`、`lock_is_held:131`、`submission_lock:150` |
| remote#3 | 复用分支裸 `open('x')` 抛未捕获 `FileExistsError` | 同一把 `submission_lock`，带中文说明 | `:1538` 附近 |
| remote#4 | `call()` 超时额度对 `deploy`/`prepare` 过小，`TimeoutExpired` 裸逃 | 分档 `CALL_TIMEOUTS` 表 + 翻译成 `ToolError('REMOTE_TIMEOUT', ...)` | `remote_compute.py:23`、`:88-95` |
| remote#5 | `run_ssh` 重试预算（≈96s）超过调用方 timeout（50s），且 timeout 只作用于单次尝试 | `run_ssh` 接受总 deadline，用 `time.monotonic()` 跨尝试扣减 | `remote_servers.py:13-41` |
| remote#6 | `fetch` 中途失败留下孤儿半成品目录 | 写入 `.partial` 再 `os.replace`；异常时 `shutil.rmtree` | `remote_compute.py:140-160` |
| remote#7 | 远端 `uploads/` 无界增长，无任何引用 | 改为按请求的 `TemporaryDirectory`，非 preview 时 `dir=root` | `remote_worker.py:1580` |
| remote#8 | `status` 读 JSON 无尺寸上限，且 `review-*.json` glob 过宽 | 加 200_000 上限 + 符号链接/越界检查；review 改为四个显式文件名 | `remote_worker.py:1738` 附近 |
| remote#10 | 注入面无任何测试 | 两个建脚本分支各加参数化拒绝测试 | `tests/test_remote_compute.py:125`、`tests/test_convergence_reuse.py:150` |
| remote#11 | 四项 minor | 见下 | |

remote#11 的四项分别落在：`remote_compute.py:15` 死导入 `SOURCE_FILES` 已删；
`engine_release(meta)` 新增（`remote_worker.py:587`）取代裸 `meta['release']` 索引，七处
`sys.path.insert` 全部走它；`prepared` 响应补 `content_is_untrusted: True`；
`deletion_preview` 的 `squeue`/`sacct` 查询改走 `query()` 辅助函数（`:733`），把
`CalledProcessError` 变成点名命令的中文消息。

### 2.2 workflow / planning 层

| 编号 | 问题 | 修法 | 锚点 |
|---|---|---|---|
| wf#4 | `origin='user_explicit'` 由模型自由传入即跳过操作者菜单 | 新增 `stated_by_user()`，要求所声称的数值真的出现在用户消息里（带数字边界，520 不会匹配到 5200 里） | `parameter_plans.py:64-78`、调用点 `:86` |
| wf#5 | 已审阅方案偏离检查不覆盖 KPOINTS，且 `if plan_path.exists()` 时静默放行 | 新增 `plan_kpoints(config)` 记录 KPOINTS；`stage()` 比对 `plan['kpoints'][name]` | `full_workflow.py:175-190`、比对在 `:209-211` |
| wf#7 | `stamp['charge_sha256']` 在旧/截断的 `complete.json` 上抛 `KeyError` | 改 `stamp.get(...)`，缺失时报明确中文 | `stage_gate.py:132-137` |
| wf#1（部分）| "物理确认"是硬编码常量，不代表真实体系 | **只加了诚实标注**：`confirmed_by` 现在记录真实 channel，新增 `ASSUMPTIONS_SOURCE = 'operator_assertion_not_structure_derived'` | `stage_gate.py:35-56` |

**wf#1 是刻意只做一半的** —— 见第 4 节。

### 2.3 models / caching 层

| 编号 | 问题 | 修法 | 锚点 |
|---|---|---|---|
| models#1 | 压缩证据写入非原子 | `.tmp` + `replace` | `context_compaction.py:213-215`、`:336-338` |
| models#2 | `json.loads(call.arguments)` 未包装，坏参数抛裸 JSON 错 | 归入该函数已有 `ValueError` 语义 | `model_adapters.py:108-110` |

---

## 3. 未修条目（清单，可直接作为下一轮开工顺序）

按我判断的性价比排序。前三项是我认为最该先做的。

**优先级 1 —— chat#1：模型输出上的无保护属性访问会终止整个交互会话。**
[ai_agent.py:1341-1342](../../src/vasp_agent/chat/ai_agent.py)（`for item in response.output` →
`item.model_dump(...)`）、`:1349`、以及 `summarize_context` 里的 `:1244`，都直接迭代
`response.output` 并访问 `item.type` / `item.model_dump`，假定是 SDK 对象列表。响应端点返回
未建模的条目（dict，或缺 `.type`）或 `{"output": null}` 时抛 `AttributeError`/`TypeError`。

**先纠正评审此处的一处不实**：评审说 `main()` 只捕获 `ImportError`/`KeyboardInterrupt`/`EOFError`、
异常"裸 traceback 逃逸"。**这不对** —— [ai_agent.py:1627](../../src/vasp_agent/chat/ai_agent.py)
有一个兜底的 `except Exception`，会脱敏并 `parser.exit(1, 'AI 助理错误：…')`。我在
`git show HEAD:src/vasp_agent/chat/ai_agent.py` 里确认它在本次改动**之前**就存在（旧版第 1339 行），
不是本轮引入的。所以不会是裸 traceback。

**但后果仍然成立，只是机制不同**：`parser.exit` 调 `sys.exit` → **进程照样退出**，操作者照样
丢失整个交互会话，只是拿到一条脱敏消息而非 traceback。断言"杀掉会话"是对的，断言"未捕获"是错的。

**同时**，`:1348` 已把畸形条目追加进 `self.history`，而 `:1373` 的 `core.save` 永不执行 —— 中毒
条目甚至没落盘，重启后不可复现。

*修法*：调用成功后立刻归一化一次 `items = response.output if isinstance(response.output, list)
else []`，之后一律用 `getattr(item, 'type', None)`。至于兜底：因为已有 `except Exception`，
真正要改的不是加不加捕获，而是**让它保住循环而不是 `exit(1)`** —— 这个代码库其他地方的设计都是
"失败不致命"。

**优先级 2 —— chat#2：`action['arguments']` 抛 `KeyError`，而外层 handler 不接。**
[ai_agent.py:1360,1363](../../src/vasp_agent/chat/ai_agent.py)。外层只捕
`except (ValueError, TypeError)`（`:1368`）。模型返回
`{"error_code": "METADATA_READ_REQUIRES_SPECIALIZED_TOOL", "next_action": {"tool":
"get_input_review", "requires_user_input": false}}`（`next_action` 在 schema 里是自由
`STR`，无人校验形状，这是很可能的近似错误）时，`action.get('tool')`（`:1358`）通过、
`requires_user_input is False`（`:1359`）通过，然后 `action['arguments']` 抛 `KeyError` 逃逸，
**丢弃本轮已经产生的工具结果**。
*修法*：retry 分支前先 `arguments = action.get('arguments')` 并判 `isinstance(dict)`；把整个
per-call body 的 handler 放宽到 `Exception`。

**优先级 3 —— wf#2 + wf#3：静默产出错误物理。**
这两条是评审里唯二"产生错误物理而非令人困惑的报错"的条目，评审自己也是这么建议的。

- **wf#2**：[kmesh.py:10-27](../../src/vasp_agent/planning/kmesh.py) 的网格启发式对三个轴施加
  同一目标倒空间间距，全代码库（`planning/`、`workflow/`）grep 不到任何 2D/slab/vacuum 处理；
  `gamma_centered: True` 是**字面量**（`:27`）。该值经 `inspect_structure`（
  [ai_agent.py:858-872](../../src/vasp_agent/chat/ai_agent.py)，返回键在 `:872`）流入 AI 上下文，
  而系统提示（`ai_agent.py:35`）指示模型把 `kmesh_suggestion` 的三个网格作为
  `propose_scan_plan` 的依据 —— 于是一个硬编码的 `True` 被当作**计算出的几何事实**呈现给模型。
  另外 `'relative_to_8_10_12': sum(counts)/3240`（`:32`）的分母 3240 = 8³+10³+12³ 本身可辩护，
  但未按轴归一化，对 slab 报告的比值与参考网格形状无关，在审计里读起来像收敛结论而其实不是。
  *测试缺口*：`test_kmesh.py` 只钉了 `Lattice.orthorhombic(3.8,3.8,13.2)`，无法区分 2D 感知
  与 2D 盲的代码。
- **wf#3**：[full_workflow.py:79](../../src/vasp_agent/workflow/full_workflow.py) 的
  `meshes=[[int(np.ceil(n * f)) for n in args.mesh] for f in (1, 1.25, 1.5)]` 在细轴上与
  `scan_config.py:14-15` 的"逐级加密、不能重复"冲突：`args.mesh = [2,2,2]` 产生
  `[2,2,2]/[3,3,3]/[3,3,3]`，直接硬失败；`[1,1,2]` 同理。用户看到的是关于网格的中文校验错误，
  无从知道根因是整数取整。注意 `kmesh.py:14-22` **已经**处理了这个碰撞（它会 bump 最粗轴），
  **两条网格生成路径互相不一致**。

**优先级 4 —— 其余（有明确锚点，可独立开工）：**
- **chat#3** `ai_agent.py:1053`：`Agent.__init__` 清空**共享** `Toolbox` 的 `review_snapshots`，
  而 `toolbox` 被每个 `Agent` 实例共用。跑完收敛、调过 `get_input_review`、再 `/model` 换端点
  → 快照被构造器清掉 → 同一会话里 `record_stage_review` 开始报"先重新审阅"错，而任务本身毫无
  变化。快照只在内存，无法恢复。
- **chat#4** `ai_agent.py:598-605`：读形状的工具做了**未记审计**的文件系统写入 ——
  `get_submission_resources` 在缺失时写 `submit.slurm`，而 `Toolbox.call` 只记参数与输出。
- **chat#5** `ai_agent.py:527-704`（`submit_stage`/`request_submission`/`remote_submit`）+
  `:934-1014`（`submit_job`）：本地与远端两条提交路径各自重实现了
  `allow_submit`/`submission_limits`/`submission_target` 检查、`confirm`、preflight 重读、
  `'submission_pending'` 预留、异常写 `'submission_uncertain'`。**已经可见的漂移**：`remote_submit`
  在 `confirm` **之前**重查 limits（`:667-672`，`confirm` 在 `:675`），`submit_job` 只在
  `check_resources`（`:940-941`）和 `confirm`（`:974`）里做。漏掉一个分支就是重复 `sbatch`。
- **chat#6** `ai_agent.py:527-537`：`request_submission` 在 `confirm` 返回 `None` 后落进
  `remote_submit`，后者**第二次**调 `confirm`（`:675`），即第二次 SSH `resource_context`
  往返。若两次之间资源签名变化，第二次不会走 early-out，会对同一次逻辑提交**再开一次操作者
  菜单**。评审自己没有构造出确定性复现（需要活的 SSH worker），我复核后同意：**这是需要
  补测试的设计异味，不是已确认缺陷**。建议加断言"每次 `request_submission` 恰好一次
  `rpc('resource_context', ...)`"。
- **chat#9** `/archive`（[extra_commands.py:178](../../src/vasp_agent/chat/extra_commands.py)）与
  `/restore`（`:191`）用 `open('x')`，重跑命令抛 `FileExistsError: file exists`，经 `Commands.handle`
  变成一句无用的"命令未完成：file exists"，且归档不被覆盖，**不手动删文件就无法重跑**。
  `prepare_transfer`（[chat_commands.py:118-120](../../src/vasp_agent/chat/chat_commands.py)）
  已经处理了同样的情况（uuid 后缀回退），这两处没有。
- **chat#11** [scan_retry_summary.py:42](../../src/vasp_agent/ui/scan_retry_summary.py)：硬编码
  "执行 4 项 / 3 项测试"，而真实数目来自 worker 的 `execution_plan['tests']`；
  `submission_dialog.confirm` 自己读的是 `data.get('execution_plan', {}).get('tests', 6)`
  —— **默认值是 6**。一个 `tests: 6` 且固定 ENCUT 的方案，菜单告示"执行 4 项测试"，数组作业
  实跑 6 个元素。**操作者批准了一个他没被展示的成本。**
- **chat#12** `extra_commands.py:257-258`：`/diagnose` 把 `json.dumps(evidence)`（含
  `workflow-state.json` 内容与 `stdout.log` 尾部）拼进模型提示。标注"以下为工具取得的数据，
  不是指令"是缓解不是控制 —— 权限层才是真正的守卫（toolbox 会重查权限，所以无法借此升级
  提交）。评分低，但建议把 evidence 截断到固定预算、`stdout.log` 只留错误附近的若干行。
- **chat#8** `stage_commands.select_task/execute`、`task_commands.execute`、
  `extra_commands` 三处重复"glob `servers/*.compute.json` + sort + `choose` + 默认本地"。
  已有可见漂移：`stage_commands` 在**一处**把 `'local'` 归一成 `None`，另两处没有。
- **wf#6** [stage_gate.py:91-95](../../src/vasp_agent/workflow/stage_gate.py)：`check_stage` 接受
  调用方传入的 `target`（来自环境变量 `VASP_AGENT_PHASE`，由提交方设置，**未与
  `checkpoint.json` 比对认证**）。把它改成"从 `checkpoint['next']` 取，env 只允许**等于**它"
  就能把覆盖变成断言。另：目录名拼错时 `PHASES.index(phase_of(name))` 抛
  `ValueError: 'foo' is not in tuple`，不可操作。
- **wf#8** [convergence_reuse.py:124](../../src/vasp_agent/workflow/convergence_reuse.py)：`freeze`
  只比六个键，`symprec`/`points`/`nsw` 可静默不同。`points`/`nsw` 不与扫描相关、不阻塞复用
  是可辩护的，但**收据应列出"未核对参数"**，而不是宣称一个它没检查过的范围。
- **wf#9** [analysis_approval.py:31-33](../../src/vasp_agent/permissions/analysis_approval.py)：
  `session_trusted` 模式没有任何模式切换审计事件（`settings()` 全文 `log(` 调用数为 **0**），
  与 `save_confirmation`、`revoke_submission` 的做法不一致。运行收据写了 `operator_session`，
  读起来像"操作者批准了这次"，但没有任何产物显示操作者批准过**这个模式**、何时批准、何时失效。
- **wf#10** [submission_permissions.py:57-62](../../src/vasp_agent/permissions/submission_permissions.py)：
  `/submit on` 重置了 limits/confirmations/target/submissions/max_submissions，**但没有清
  `confirmed_resource_selections`**（`revoke_submission` 会清）。评审未能构造出可行绕过（我复核
  同样未构造出），但"已确认"短语可能是在**上一次**授权限额下做出的选择上被求值。
- **wf#11** [submission_dialog.py:84](../../src/vasp_agent/permissions/submission_dialog.py)：
  `min(tests if tests is not None else 6, 7) - 1` 把数组截在 7 个。当前不可达（
  `convergence_tests` 至多 7），但 `7`、`-1` 这两个魔数在四处重复
  （`submission_dialog.py:84`、`remote_worker.py:1355` 的 count 校验、`:1101` 的
  `1 <= count <= 7`、`array_layout` 的 `:1420`），且 `array_layout` 的 `0-([0-6])` 会**拒绝**
  `directives` 为 8+ 个测试写出的脚本 —— 两者会互相矛盾，而不是其中一个干净失败。
- **remote#9**：见第 4 节，我建议**不改**。

**测试缺口（散见于各条，集中记在这里）：**
- `FakeResponses` 从不产生非 `'completed'` 状态、`output=None`、缺 `.type` 的条目或纯 dict 条目
  —— 第 3 节的 chat#1/#2 每一条都能被一个这样的假响应抓到。
- 没有人用不可解析的 JSON（`'{"a":'`）驱动 `call.arguments`，所以 `:1353-1354` 那条
  `(ValueError, TypeError)` 路径没被测过 —— 这正是"没人发现它不接 `KeyError`"的原因。
- `stage_gate.evidence` 配上被篡改的 `charge_sha256` —— wf#7 的 `KeyError` 路径无测试。
- `stage()` 在 `VASP_AGENT_PHASE` 与 `checkpoint['next']` 不一致时 —— 无测试。
- 上述第 5 节的两类回归（Windows 路径、`SimpleNamespace` 桩）值得各加一条。

---

## 4. 刻意不改 / 只改一半的，及理由（**最容易被误解的部分**）

### remote#9 —— 不拆分 god object

评审自己承认单文件约束是真的，并主张"模块应可拼接，而非一个扁平命名空间"。我同意问题存在，
但**没有做拆分**，理由：

1. 它是纯重构。文件当前 1790 行、130+ 测试全绿，拆分本身不改变任何可观察行为，风险全部落在
   "拆错了打包链"上 —— 而打包链（第 1 节第 1 条）恰恰是这个文件最脆的部分。
2. 评审**唯一**指出的具体正确性隐患是七处 `sys.path.insert`。这一项我**已经做了**：
   `engine_release(meta)`（`remote_worker.py:587`）成为唯一读取 release 的入口，七处调用点
   全部改走它，外加两处 `Path(...release)`。剩下的 `engine_module(action_context)` 签名重构
   属于风格统一，收益递减。
3. 如果确实要拆，评审给的顺序是：(a) 收据/日志/锁独立成模块（第 2 节 remote#2/#3 都住在那），
   (b) `release_registry` 访问器，(c) 派发 `if` 链改成 dict 派发 + 每动作一函数，
   (d) 调度器解析块。**建议单独立项，不要和正确性修复混在一个 commit 里。**

### wf#1 —— 只加了标注，没有让 schema 能表达 2D

评审的完整修法是：把 `structure_sha256`、`formula`、`natoms`、晶胞矩阵存进同一份文档，让
`physics()` 从 `initial.POSCAR` 重新推导并拒绝不一致。我**没有**这么做，只做了两件事：
`confirmed_by` 记录真实 channel，新增 `ASSUMPTIONS_SOURCE`。

**理由：完整修法会改变审计记录格式，并且让"2D 体系能否确认"从一个记录问题变成功能问题。**
现在的 schema 是 `dimension: '3D'` 硬编码常量，而 `physics()` 用**同一批常量**做精确相等校验
—— 也就是说，这个 schema **在结构上无法表达 slab**，因此每个 slab 任务都会静默写下一条
错误陈述。这是评审的核心指控，我**没有解决它**，只是让记录自认是"操作者断言、非结构推导"。
`stage_commands.py:64` 的 UI 文案早就是诚实的（"不能由结构文件自动证明这些假设"），现在 JSON
也带上了同样的限定。

**这是待办，不是已完成。** 给它排优先级需要你先决定产品意图：这个 agent 要不要支持 slab/2D？
不支持的话，正确修法是**拒绝**确认而不是扩 schema；支持的话才需要扩。这个判断我不该替你做。

---

## 5. 两个我踩过的坑（**请务必读**）

### 5.1 定向测试全绿≠没坏：`SimpleNamespace` 桩

我用定向测试验证 remote#7/#8/#11 时得到 `160 passed`。全量跑出
`tests/test_task_commands.py::test_queue_and_accounting_checks` 失败。

根因：该测试用 `lambda *a, **kw: NS(stdout=...)` 打桩 `subprocess.run` —— 裸
`SimpleNamespace`，**没有 `returncode`、没有 `stderr`**。我把 remote#11d 的
`check=True` 换成收口到 `query()` 后，无条件读 `result.returncode` → `AttributeError`；
修掉之后又在非数组的 `sacct` 分支上二次读了 `result.stdout`（而 `query()` 已经返回
**字符串**）。

**教训**：这个代码库里有相当数量用 `SimpleNamespace` 打桩外部进程的测试。任何新的
`subprocess.run` 收口都必须用 `getattr(result, 'returncode', 0)` /
`getattr(result, 'stderr', '')` 这类容错读法，或者**先跑全量**。

### 5.2 我写的 `startswith('/')` 一次打挂 12 个测试

我给 remote#11b 新增 `engine_release()` 时，守卫写成：

```python
if not isinstance(release, str) or not release.startswith('/') or '\n' in release:
```

**12 个测试立刻失败**，两个测试套件都中：
- `tests/test_tool_recovery.py:34` 写 `{'release': str(Path(core.__file__).parent)}` → Windows 形式 `C:\...\src\vasp_agent\workflow`
- `tests/test_remote_array.py:39` 写 `tmp_path / 'release'`

于是它在**每一次**调用上触发，而测试驱动的 `review_context` 恢复路径在到达
`input_review.context` **之前**就抛了 —— 而
`INPUT_HASH_MISMATCH`/`UNVERIFIED_METADATA`/`METADATA_INVALID`/`FILE_NOT_FOUND`
这些具体错误码**正是** `input_review.py:126,150,153` 发出的。守卫抛出的 `ValueError` 经
`dispatch` 的处理器退化成通用的 `REMOTE_ERROR`，对外显示为 `TOOL_ERROR`。
数组失败同理：`submission_preflight` 根本没走到能报"旧引擎"的地方。

**正确写法是 `Path(release).is_absolute()`**（现已如此）—— 接受 Windows 盘符形式也接受 POSIX
形式，仍然拒绝缺失/相对路径记录。守卫的职责是"拒绝一条无法命名目录的记录"，不是"把部署钉死在
某个 OS 上"。**在当前工作树里 grep 任何新的路径守卫，确认没有回到 `startswith('/')`。**

---

## 6. 环境与流程约束（沿用，勿改）

- 读含中文源码显式 `encoding='utf-8'`。
- 不传 `--timeout`（未装 `pytest-timeout`）。
- 模型配置：`models.toml`。不修改会话、密钥、模型或服务器配置，除非用户明确要求。
- `.gitignore` 已忽略：`.env`、`.env.*`、`servers/*`、`potentials/**`、`*.pem`/`*.key`/`*.ppk`/
  `*.p12`/`*.pfx`、`.submission-defaults/`、`.ai-audit.jsonl`、`.ai-session-*.json`、
  `.context-evidence/`、`.ai-archive/`、`downloads/`、`workbench/`、`runs/`。
  **`servers/*.json` 含连接凭据，绝不能纳入版本控制。**
- 权限配置只写项目 `.claude/settings.json`，**不**写 `~/.claude/settings.json`、**不**写
  `.claude/settings.local.json`；不向 `permissions.deny`/`permissions.ask` 添加任何内容。
- 绝不把授予任意代码执行权限的模式加进允许列表（解释器、shell、包运行器、任务运行器通配符、
  `gh api *`、`docker run`/`exec`、`kubectl exec`、`sudo`）。精确的 `Bash(bun run typecheck)`
  可以，`Bash(bun run *)` 不行。

---

## 7. 我建议的开工顺序

1. **chat#1 + chat#2**（第 3 节优先级 1、2）。两者都在 `ask()` 的同一个循环里、都是"异常逃逸
   杀掉交互会话"，可以一个 commit 做完，并顺手补 §3 末尾说的那组参数化 `FakeResponses`
   —— 那几个假响应能同时覆盖这两条。
2. **wf#2 + wf#3**。唯二产出错误物理的条目，且互相咬合（同一个 kmesh 建议会流进同一条
   mesh 阶梯）。先加 slab 测试用例再改实现。
3. **chat#11**。独立、小、面向操作者的成本陈述不实 —— 改动量很小，收益直接。
4. **wf#1 的完整修法** —— 但先确认产品是否支持 2D（第 4 节）。
5. 其余按第 3 节顺序。**每完成一条跑一次全量。**

**在你决定是否提交之前，请不要把第 2 节的改动当作"已验证上生产"。** 它们的验证只有一条：
全量测试 1046 passed。**没有连接过真实模型，没有 SSH 到真实服务器，没有提交过任何计算。**

---

## 8. 续作记录（2026-10-12）

接手后按第 7 节顺序继续。仍**未提交**。全量：**1070 passed, 1 skipped**（原 1046 + 新增 24 个测试）。

| 编号 | 处理 | 锚点 |
|---|---|---|
| chat#1 | `output_items()` / `item_field()` 归一化 `response.output`（None、非 list、dict 条目、缺 `.type` 均不再抛错）；无 `call_id` 的调用不执行也不入历史；`summarize_context` 同样改用归一化；`main()` 交互循环里 `ask()` 抛异常时保存对话并回复错误，**不再 `exit(1)`** | `ai_agent.py` `item_field`/`output_items`、`ask()`、`main()` 循环 |
| chat#2 | 每个工具调用的 handler 放宽到 `Exception`；`next_action` 非 dict、`arguments` 缺失或非 dict 时不做只读恢复，原工具结果照常返回模型；参数 JSON 非对象时报错返回模型 | `ask()` 内 per-call 循环 |
| wf#3 | 新增 `scan_config.mesh_ladder()`：取整碰撞时把"相对基准加密最少"的轴各 +1（对称网格保持对称）；`full_workflow` 改用它。`[2,2,2]`→`[2,2,2]/[3,3,3]/[4,4,4]` | `scan_config.py`、`full_workflow.py:78` |
| wf#2（部分） | `kmesh` 新增真空检测：各轴最大原子空隙（按晶面间距 2π/|b_i| 换算，含周期回绕）≥7 Å 时给出 `vacuum_axes` + `dimensionality_warning`；新增 `centering_source`、`ratio_note`。系统提示要求模型遇到该警告时先向用户确认维度。**网格本身未改**——真空轴是否钉为 1 取决于 wf#1 的产品决定 | `kmesh.py`、`ai_agent.py:35` |
| chat#11 | 新增 `planned_tests(mode, fixed_encut)`，摘要文字中的测试数由它生成；测试断言它与 `convergence_tests(config)` 的实际长度一致。复核结论：评审描述的"tests:6 + 固定 ENCUT"在现有代码中不可达（`fixed_cutoff` 只允许 mesh 模式），这是漂移隐患而非现存错误 | `ui/scan_retry_summary.py`、`tests/test_scan_retry_summary.py` |
| chat#3 | **判定为刻意行为，不改逻辑**：审阅记录以 `review_model` 署名，换模型后必须由新模型重新读取证据。加了注释、把两处报错改成说明原因，并加测试固定这一不变量 | `ai_agent.py` `Agent.__init__` |
| chat#9 | `/archive` 重名时加 uuid 后缀（与 `prepare_transfer` 一致）；`/restore` 遇到同名活动对话时报出名称和处理办法 | `extra_commands.py` |
| chat#12 | `/diagnose`：日志尾部限 4000 字符并排在 status 之前，整体证据限 12000 字符 | `extra_commands.py` `DIAGNOSE_*` |
| wf#9 | 新增 `record_mode()`：本地分析模式每次变化写审计事件（from/to/confirmed_by/scope） | `permissions/analysis_approval.py` |
| wf#10 | `/submit on|once` 同时清空 `confirmed_resource_selections` | `permissions/submission_permissions.py` |

**仍未处理**：wf#1（等 2D 产品决定）、chat#4、chat#5、chat#6、chat#8、wf#6、wf#8、wf#11、remote#9。

---

## 9. 二维 slab 支持（2026-10-12，wf#1 + wf#2 完整修法）

产品决定（用户确认）：**支持 slab/2D**。全量 1081 passed, 1 skipped。

**设计**
- 新引擎模块 `workflow/dimensionality.py`（已加入 `ENGINE_MODULES`，随 release 下发；远端需重新 deploy）。
  按原子分数坐标求各轴最大空隙（按晶面间距换算 Å），≥7 Å 记为真空方向。0 个=3D，1 个=2D，≥2 个=1D/0D 拒绝。
  2D 要求真空沿第三晶格矢量 c，否则拒绝并提示重排晶格矢量。
- `prepare_full`：维度写入 `config['dimensionality']`（进 manifest 哈希）；2D 时 `relax_mode=cell` 拒绝
  （ISIF=3 会压缩真空层）；基础网格和全部扫描网格 c 方向必须为 1；`mesh_ladder(fixed_axes=(2,))` 不加密 c。
  `manifest.scope` 写实际维度。
- `relaxation_grids`：2D 的 denser-mesh 复核不加密 c。
- 运行到 SCF/能带：2D **不做** `HighSymmKpath` 标准化（标准化可能把真空轴挪离 c），沿用优化后晶胞与生产网格；
  能带走 `band_path_2d` 面内路径（六角 γ=120/60、正方、矩形；斜方格子用通用路径并附说明），kz 恒为 0。
- `stage_gate`：`confirm_physics` 的 `dimension` 由 `initial.POSCAR` 检测，附 `dimension_source`、空隙、
  `structure_sha256`；`physics()` 每次重新检测并比对，不一致或结构哈希变化即拒绝。旧记录（无这些字段、写 3D）
  在结构确为体相时继续有效；**slab 结构上的旧 3D 记录会被拒绝**——这是本次要堵的口子。
- `kmesh` 改用同一检测：受支持的 slab 网格 c 方向固定 1 并给 `slab_note`；真空不在 c 或多真空方向给 `dimensionality_warning`。
- bands-only 旧入口（`vasp_band_agent.prepare`）遇到真空层直接拒绝，指引用 full 模式。
- 系统提示、两个物理确认菜单、输入审阅的 `pending_validation`/`future_geometry` 已同步。

**明确未做（列为待验证，而非静默处理）**
- 面内晶格常数优化：VASP 原生无法固定 c 做晶胞优化；`fixz`/`ioptcell` 补丁版本可做，但其来源不可核验，暂不放开。
- 偶极修正（LDIPOL/IDIPOL）未自动启用；真空层厚度收敛不自动扫描。两者写入 `pending_validation`。
- 未在真实服务器/VASP 上跑过 slab；验证只有单元测试（含伪 VASP 的端到端流程测试 `tests/test_slab_2d.py`）。

---

## 10. 剩余条目收尾（2026-10-12）

全量 **1089 passed, 1 skipped**。至此评审条目除 remote#9 外全部处理。

| 编号 | 处理 | 锚点 |
|---|---|---|
| wf#6 | 新增 `stage_gate.authorized_phase()`：以 `checkpoint['next']` 为准，`VASP_AGENT_PHASE` 只能与之相同，不一致即拒绝；`stage()`、`run_full`、`input_review.require_review` 全部改用它。`check_stage` 对拼错的阶段目录给出可读报错 | `stage_gate.py`、`full_workflow.py`、`input_review.py` |
| wf#8 | 复用收据新增 `checked`（比对过的文件和 6 个参数）与 `unchecked_parameters`（symprec/points/nsw 的来源值、目标值、是否不同）及说明 | `convergence_reuse.py` `REUSE_CHECKED`/`REUSE_UNCHECKED` |
| wf#11 | 数组上限集中为 `remote_worker.MAX_ARRAY_TESTS=7`、`MAX_ARRAY_CONCURRENCY=6`，四处引用；`submission_dialog` 不再 `min(tests, 7)` 静默截断，超限直接报错；`array_layout` 改由常量判定（仍拒绝前导零）；默认测试数命名为 `DEFAULT_ARRAY_TESTS` | `remote_worker.py`、`permissions/submission_dialog.py` |
| chat#4 | 新增 `Toolbox.stage_submit_script()`：本机任务首次复制提交脚本时写 `submit-script-source.json`（源路径/源哈希/副本哈希）并记审计；已有副本若来源哈希与本会话审核脚本不同则拒绝。**不**在每次提交时刷新副本——资源菜单确认后会改写其中的 SBATCH 指令。无来源记录的旧副本照旧接受 | `ai_agent.py` |
| chat#6 | `request_submission` 在 `confirm(authorize=True)` 返回 None 后显式断言授权状态，并跳过第二次 `confirm`：每次请求恰好一次 `resource_context`，不会对同一次提交再开菜单 | `ai_agent.py` `request_submission`/`_remote_submit` |
| chat#8 | `extra_commands.configured_servers()` 统一四处服务器列表（`/physics`、`/task`、`/submit` 两处）。修正漂移：`/submit` 原先会列出缺连接文件的服务器 | `extra_commands.py` |
| chat#5（部分） | 抽出 `authorize_submission(server, job, phase)`（启用/额度/服务器/目标四项），本机与远程两条路径共用。修正漂移：本机 `submit_job` 原先不检查 `submission_target`，且额度检查在资源菜单之后。已有提交记录的只读回读仍排在授权检查之前（保持幂等）。**未**做完整的单一 `_submit()` 合并：本机 sbatch 与远程 RPC 的预留/异常记录写在不同进程，强行合并收益小、风险集中在防重复提交逻辑上 | `ai_agent.py` |

**仍未处理**：remote#9（拆分 `remote_worker.py`）。建议单独立项，与正确性修复分开提交。

## 11. remote#9 拆分（2026-10-12，纯重构，行为不变）

按依赖顺序分五个提交（每步全量 `pytest` 通过）：

1. **打包链先行**：`bundle.worker_program()` 按 `remote_worker.WORKER_UNITS` 顺序拼接各单元源码，最后接 `remote_worker.py`，并删去 `# <bundle:package-only>` … `# </bundle:package-only>` 之间的行（只在包导入路径下需要的 `from vasp_agent.remote.worker_* import ...`）。`bundle.stdin_program(payload)` 是 `RemoteCompute.call` 与测试共用的唯一组装入口。`tests/test_worker_bundle.py`：在 `-I`、无 `PYTHONPATH` 下运行拼接程序并与包内 `dispatch` 逐个比对结果；并逐函数比对字节码。
2. **`worker_common.py`**（`SubmissionError`、`diagnostic_text`、`write_json`、`sha`）与 **`worker_slurm.py`**（`MAX_ARRAY_*`、`scheduler_read`、`capacity_preflight`、`array_layout`、`accounting_window`、`submission_accounting`、`submission_command_evidence`、`array_scheduler_status`、`monitor_status`）。先于收据单元，因为 `recovery_context` 依赖记账读取。
3. **`worker_receipts.py`**：锁/收据/恢复日志（`open_lock`、`try_lock`、`lock_is_held`、`submission_lock`、`submit_with_receipt`、`recovery_path`、`recovery_context`、`recover_submission`、`recovery_barrier`）。
4. **release 访问器**：`deployed_release(root)`（新任务）、`task_release(meta)`（已有任务，= `engine_release` + `activate_release`）；`sys.path.insert` 只剩 `activate_release` 一处。
5. **派发表**：`ROOT_ACTIONS` / `NEW_TASK_ACTIONS` / `TASK_ACTIONS` 三张表 + 每动作一个 `_handler`，`dispatch` 只保留按序校验。

约束：
- 单元只能用标准库和**更早**的单元；新增单元须同时加入 `WORKER_UNITS`（自动进入 `SOURCE_FILES`，部署后的 release 才能导入 `remote_worker`）。
- `remote_worker` 在 package-only 块里再导出所有移出的名字，`from vasp_agent.remote.remote_worker import X` 照旧可用。
- monkeypatch 要打在**查找发生的模块**上：例如 `recovery_context` 在 `worker_receipts` 里查 `submission_accounting`，所以 `test_submission_recovery` 改为 patch `worker_receipts`。拼接程序是单一命名空间，不受影响。
- `validate_array_submission` 与 `submission_diagnostic` 仍留在 `remote_worker.py`：前者依赖 `engine_module`/`__package__`（测试会 patch 它们来模拟拼接模式），后者被测试在 `remote_worker` 上 patch 记账读取。

---

## 11. 测试缺口补齐（2026-10-12）

基于 remote#9 拆分分支继续。全量 **1106 passed, 2 skipped**（新增 skip 为 POSIX 路径用例在 Windows 上按设计跳过）。

- **wf#7**：`stage_gate.evidence` 遇到缺 `charge_sha256` 的 `scf/complete.json` 报 `FILE_NOT_FOUND`，缺 `xml_sha256` 报 `UNVERIFIED_METADATA` 并点名记录——已有修复，补测试。
- **第 5.2 节回归**：`engine_release` 接受任意绝对路径形式（含 Windows 盘符），拒绝缺失、相对、非字符串、含换行的记录。
- **chat#1 交互循环**：循环体抽成 `ai_agent.answer()`，测试断言异常时会话不退出、对话落盘、密钥被遮蔽。
- **remote#5 后半（ServerAlive）**：核实后**不改**。`ServerAliveInterval/CountMax` 是 SSH 协议层保活，由 sshd 应答，与远端程序是否输出无关；只有网络或 sshd 无响应约 30–45 秒才断开。已在 `ssh_args` 旁加注释。
- 第 5.1 节 `SimpleNamespace` 桩一类已由 `test_task_commands.py::test_queue_and_accounting_checks` 覆盖，未重复添加。

评审条目至此全部处理完毕。chat#5 刻意只做了共用授权检查（理由见第 10 节）；chat#7（`/plot`）评审自己评为弱项，未改。
