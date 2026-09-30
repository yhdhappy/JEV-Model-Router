# JEV 置信度口径 —— Adjust 闸门裁决材料 v1.1

> 文档性质：**闸门裁决输入材料**（不是结论，不是授权，不修改任何冻结内容）
> 提交对象：唯一总顾问（ChatGPT Web / GPT-5.6 Sol High）
> 生成方式：DeepSeek Harness 侧 P0 探针 + 对已有冻结证据的重新统计
> 关联闸门：`PILOT_ADJUST_GATE`（已通过）/ `PILOT_ADJUST_EXECUTION_GATE`（已通过，执行未完成）
> 关联证据：`benchmark/results/pilot_runs.jsonl`、`orchestration/workflow_state.json`
> 配套技术记录：`docs/dsh-integration/JEV_置信度聚合口径_发现记录_v0.1.md`
>
> **v1.1 修订（2026-10-01）**：由 DSH 子 Agent 复核后修正统计口径——§2.1 曾把 `23`、
> `0.371`、`87%` 三个不同口径的数字并列，已改为逐行自洽；§0 "9 个里 7 个"更正为
> "8 个调用 JEV 的真实任务里 7 个"；§2.4-1 归因从"只登记主要触发原因"更正为代码级
> "repeat trigger 自动校验覆盖不足"；§3 补充"探针数字需实时调用 API、无法用历史记录复核"。
> **核心结论与所有事实性统计不变**，本次仅统一口径与归因表述。

---

## 0. 给项目负责人的一段大白话

JEV 这个"给任务定难度"的裁判，**判得其实很稳、也很准**——同一个任务跑三次，它三次都给出
一模一样的类型、难度和能力档。但是项目里有一条规则：**只要裁判说自己"没把握"，这个任务
就必须再跑两遍**。问题出在"没把握"这个数字是怎么算出来的：它取的是五个问题里**最没把握的那一个**
的分数。而这五项里有一项（难度打分）用的打分方式跟其它四项不一样，它**经常直接报 0**，
哪怕它心里其实很有数。一个 0，就把整条"没把握"的信号拉爆了。

结果是：**9 个真实任务里有 8 个实际调用了 JEV，其中 7 个被判成"没把握"，全部被迫跑了三遍，
把允许的重复次数用光了。**（第 9 个 task_001 走的是轻量规则、根本没问 JEV。）

这件事没有让项目出错，但它说明：**"没把握就重跑"这道保险，现在实际上是常开的**。
按现在的规则继续往下走，你以后每接一个新任务，大概率还是会被要求重跑。

**这不是我发现的 bug，是规则本身的一个脆弱点；改不改、怎么改，必须由总顾问裁决。**

---

## 1. 为什么必须由总顾问裁决，而不是执行 Agent 自行处理

本项**不在已冻结的 Adjust 范围内**：

| 项 | 冻结值 | 出处 |
|---|---|---|
| Adjust 范围 | `timeout_fallback_budget_only` | `workflow_state.json → adjust_gate.scope` |
| 冻结阈值 | `jev_confidence_threshold: 0.7` | `workflow_state.json → pilot_config_gate` |
| 聚合口径 | `min(五项置信度)` | `src/jev_router/real_jev.py::_parse_response` |

因此：

- **调整阈值** = 修改冻结的 Pilot 参数 → 超出 Adjust 范围；
- **调整聚合口径** = 修改已冻结并已用于历史结果的分类器行为 → 会使历史 confidence 失去可比性；
- 两者都**不属于**执行 Agent 的职权，也不属于本材料的作者。

按 `AUTO_WORKFLOW.md` §6 第 3 条（「出现文档无法裁决的产品选择」），本项**可能构成停止条件**。
是否升级为 BLOCKER、是否开设新闸门，由总顾问裁定。

---

## 2. 事实（可复核）

数据源：`benchmark/results/pilot_runs.jsonl`（48 条记录）。

### 2.1 置信度统计

三种口径，每一行的样本数、均值、百分比都按**同一口径**计算（不再混用）：

| 口径 | 样本数 | 均值 | 范围 | `< 0.70` |
|---|---|---|---|---|
| 真实任务运行（`route_source=jev`，排除受控 Mock） | **21** | 0.3467 | 0.17 – 0.94 | **20（95.2%）** |
| 仅 `route_source=jev`（含 Mock 的 router 那一条） | 22 | 0.3709 | 0.17 – 0.94 | 20（90.9%） |
| 所有带 confidence 的记录（含 Mock 的 baseline + router 两条） | 23 | 0.3930 | 0.17 – 0.94 | 20（87.0%） |

> **勘误说明**：本材料初稿曾在"含受控 Mock"一行里把 `23`、`0.371`、`87%` 三个数字并列，
> 但它们分属不同口径，不能同时成立。已改为上表逐行自洽。差异根因是
> `task_010_fallback` 一次运行产生了 **baseline（`route_source=fallback`, conf 0.88）**
> 和 **router（`route_source=jev`, conf 0.88）** 两条都带置信度的记录。
> 本材料的核心结论只依赖第一行（真实任务），不受该口径差异影响。

`task_001` 走 `light_rule`（未调用 JEV，无置信度）；`task_005` 的 attempt 2 因
`jev_network_error` 走 `safe_default`（无有效分类器）；其余记录为 `manual_override`，无置信度。

### 2.2 任务级结果

| 任务 | 各 attempt 置信度 | 实际触发的 repeat 条件 | 重复次数是否耗尽 |
|---|---|---|---|
| task_001 | 未调用 JEV（light_rule） | — | 否 |
| task_002 | 0.94 | — | 否 |
| task_003 | 0.48 / 0.45 / 0.44 | `jev_confidence_below`, `difficulty_score_near_bucket_boundary` | **是** |
| task_004 | 0.23 / 0.30 / 0.25 | `jev_confidence_below`, `difficulty_score_near_bucket_boundary` | **是** |
| task_005 | 0.46 / — / 0.49 | `unexpected_fallback`（**见 §2.4**） | **是** |
| task_006 | 0.26 / 0.27 / 0.28 | `jev_confidence_below`, `unexpected_fallback` | **是** |
| task_007 | 0.36 / 0.28 / 0.31 | `jev_confidence_below` | **是** |
| task_008 | 0.26 / 0.31 / 0.36 | `jev_confidence_below`, `difficulty_score_near_bucket_boundary`, `unexpected_fallback` | **是** |
| task_009 | 0.17 / 0.19 / 0.19 | `jev_confidence_below`, `difficulty_score_near_bucket_boundary` | **是** |

**置信度低于 0.70 的真实任务：7 / 8**（`task_002` 为 0.94；`task_001` 未调用 JEV）。
**重复次数耗尽的真实任务：7 个**——即所有置信度不达标的任务都用光了允许的 2 次额外运行。

### 2.3 与"分类质量"的对照（关键）

**同一任务的三次运行，JEV 的核心判定完全一致**：

| 任务 | 三次一致的类型 / 难度 / 能力 |
|---|---|
| task_003 | coding / 4 / medium |
| task_004 | debugging / 4 / medium |
| task_006 | debugging / 5 / medium |
| task_007 | review / 6 / high |
| task_008 | coding / 4 / medium |
| task_009 | architecture / 8 / high |

人工合理性抽查的历史结论也全部是 `reasonable`。

> **判得稳、判得对，置信度却系统性偏低。** 问题指向聚合口径，而非分类质量。

### 2.4 需要总顾问注意的两处记录不一致（不影响主结论）

1. `task_005` 两次运行置信度为 0.46 与 0.49，**均在阈值以下**，但
   `workflow_state.json` 的 `repeat_triggers` 只记录了 `unexpected_fallback`，
   未记录 `jev_confidence_below`。**代码级根因已定位**：
   `benchmark/official_pilot.py` 的 `_validate_repeat_triggers()` 只对 `task_003`
   强制校验"必须包含两个客观冻结触发条件"（`if task_id != "task_003": return`），
   其余任务的 `repeat_triggers` 集合是否完整**未经同等自动复核**。因此这不是
   "当时只登记主要触发原因"，而是 **repeat trigger 的自动校验覆盖不足，存在人工漏登风险**。
   该缺陷不影响本材料的统计数字（统计直接从 `pilot_runs.jsonl` 的原始置信度重算），
   但说明历史 `workflow_state.json` 里的 `repeat_triggers` 字段不能作为完整性证据。
2. `workflow_state.json` 中 `adjust_execution_gate.real_adjust_started = false`、
   `next_task = task_003`，但磁盘上已存在 `benchmark/results/adjust_runs.jsonl` 与
   `benchmark/results/adjust_artifacts/task_003/attempt_1/`。**状态文件落后于实际产物。**
   本材料不对此做任何处置，仅报告。

---

## 3. 机制（已实测复现）

> **可核验性说明**：本节所有数字来自**实时调用 System One API**，不是从 `pilot_runs.jsonl`
> 反查得到。历史 Pilot 记录只保存了聚合后的单个 `confidence`，**没有保留五项分解和原始概率分布**，
> 因此本节数字无法用仓库内既有数据独立复核，只能用第 8 节的命令重新实时调用一次来复现。
> 也正因如此，"Pilot 当年的偏低是否同样由 score 项归零主导"仍属推断（见第 4 节）。

对任务文本 `测试一下JEV` 直接读取 System One 原始响应（`jev-1.13.0`）：

| 问题项 | 判定 | 自报置信度 |
|---|---|---|
| task_type | testing | 0.99 |
| **difficulty_score** | **3.45** | **0** ← 归零项 |
| difficulty_bucket | low | 0.91 |
| required_capability | low | 0.78 |
| risk_level | low | 0.95 |

`difficulty_score` 的原始概率分布：等级 0 = 0.33，等级 7 = 0.38，其余八档合计 0.29。
这是明显的**双峰分布**，信息量充足，**并非"十档均匀、毫无判断"**，但该项自报 `confidence = 0`。

控制组：`帮我修复登录接口偶发 500，并补一个回归测试` → 五项 0.99 / 0.74 / 0.80 / 0.68 / 0.59，
整体 0.59（正常）。

**机制结论**：TypeSafe 的 `choice` 与 `score` 是两种 primitive，其 `confidence` 语义并不一致。
`min()` 把语义差异直接放大为**整体信号归零**。

> 注意：`score` 的 `0` 是否等同于"无判断"，**尚未取得 TypeSafe 官方口径确认**，
> 目前是基于概率分布的经验判断。这一点在大规模采用前需要补证。

---

## 4. 推断（尚未证实，不得作为结论）

1. 「Pilot 中偏低的置信度也是由 `score` 项主导」——**属于推断**。
   当时的原始响应未留存五项分解，`pilot_runs.jsonl` 只存了聚合后的 `confidence`，
   **无法回溯验证**。
2. 「换一种聚合口径后，Pilot 的 Go/Adjust/Stop 结论会改变」——**未验证**，
   且不应在缺少证据时假定。

---

## 5. 候选方案（**总顾问已裁决**）

| # | 方案 | 影响 | 风险 | 裁决状态（2026-10-01） |
|---|---|---|---|---|
| **A** | **不改**，仅补测量。先让 DSH 适配器持续记录五项分解，积累真实任务样本后再议 | 零风险；历史 results 完全可比 | 已知的常开门继续存在 | **已批准（Phase 1：仅取证）** |
| **B** | **新增** `confidence_choice_only` 字段（只用**四个** `choice` 项聚合：`task_type`、`difficulty_bucket`、`required_capability`、`risk_level`），**保留原 `confidence` 不变** | 历史结果完全可比；新增字段仅供新样本使用 | 需再次授权才能参与任何判定 | **预注册候选**；当前只允许记录/展示/统计/比较，**不得**参与选模、repeat、fallback、Go/Adjust/Stop |
| **C** | **按维度拆分** repeat 条件：只有「能力档不确定」才触发重复，「难度原始分不确定」不触发 | 直击真实痛点 | 修改冻结 repeat 规则 | **暂不批准** |
| **D** | 调整 `jev_confidence_threshold` 数值 | 改动最小 | 治标不治本 | **不批准** |

> **勘误（2026-10-01）**：方案 B 初稿写作"三个 `choice` 项"，**该定义错误**。
> 实际 primitive 分布为——`task_type`(choice)、`difficulty_score`(**score**)、
> `difficulty_bucket`(choice)、`required_capability`(choice)、`risk_level`(choice)，
> 即 **4 个 choice + 1 个 score**。方案 B 的候选聚合必须是上述四项。

**总顾问裁决**：先执行 **A**（仅取证，Phase 1），达到 20 个独立真实任务后重新提交审核。
本 Gate 已登记为 `JEV_CONFIDENCE_SEMANTICS_GATE`。

---

## 6. 预注册的测量方案（若采纳 A）

在任何口径或阈值改动之前，必须先完成以下测量。**测量本身不改动任何冻结内容**：

| 项 | 内容 |
|---|---|
| 目的 | 取得真实任务上**五项置信度的完整分解**，验证 §4 的推断 |
| 工具 | DeepSeek Harness 适配器 `packages/dsh-plugin`（已上线，`/jev` 命令） |
| 证据路径 | `~/.dsh/jev-router/decisions.jsonl` |
| 每次记录 | `classifier`（聚合值，口径不变）、`answer_confidences`（五项分解）、`metrics`（token/成本/延迟）、`reference_model` |
| 最小样本 | 待总顾问确认（建议 ≥ 20 次真实任务判定） |
| 成功判据 | 能明确回答：偏低是否由 `score` 项主导；`choice` 三项单独聚合后的分布如何 |
| 明确不做 | 不修改 `pilot_config.yaml` / `pilot_adjust_config.yaml`；不写任何 `benchmark/results/` 下的既有文件；不重跑官方 slot |

**补充说明**：`/jev` 命令是**只读判断**，不接管、不改写任何模型请求，
因此该测量过程不会产生新的路由行为，也不会污染任何路由结论。

---

## 7. 本材料明确不做的事

- 不修改 `benchmark/pilot_config.yaml`、`pilot_adjust_config.yaml` 及其冻结值
- 不修改 `src/jev_router/` 的既有行为（`confidence = min(...)` 保持原样）
- 不覆盖、不重写 `pilot_runs.jsonl`、`pilot_summary.*`、原始 `artifacts/`
- 不替总顾问作 Go / Adjust / Stop 判断
- 不宣布任何闸门通过

---

## 8. 复现步骤

```bash
cd /Users/yhd/Documents/AI_Workspace/project_0010_JEV_Model_Router

# 2.1 / 2.2 节的统计
.venv/bin/python - <<'PY'
import json, collections
runs=[json.loads(l) for l in open('benchmark/results/pilot_runs.jsonl') if l.strip()]
per=collections.defaultdict(list)
for r in runs:
    c=r.get('classifier')
    if isinstance(c,dict) and c.get('confidence') is not None:
        per[r.get('task_id') or r.get('slot')].append(c['confidence'])
for t in sorted(per): print(t, per[t])
real=[c for t,v in per.items() if t!='task_010_fallback' for c in v]
print('真实任务运行数', len(real), '均值', round(sum(real)/len(real),3),
      '低于0.70', sum(1 for c in real if c<0.70))
PY

# 第 3 节的原始响应复现
cd packages/dsh-plugin
node scripts/smoke.mjs "测试一下JEV" "/path/to/jev-key.rtf"
```

---

*本文档为裁决输入材料，不构成结论、授权或闸门判定。是否开设新闸门、是否调整冻结参数，由唯一总顾问决定。*
