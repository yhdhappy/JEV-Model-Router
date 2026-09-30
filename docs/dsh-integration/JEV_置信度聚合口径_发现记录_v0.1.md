# JEV 置信度聚合口径——发现记录 v0.1

> 文档性质：技术发现记录（**未修改任何冻结行为**）
> 发现日期：2026-09-30
> 发现来源：DeepSeek Harness 适配器 P0 探针（`packages/dsh-plugin/`）
> 关联闸门：`PILOT_ADJUST_GATE` / `PILOT_ADJUST_EXECUTION_GATE`

---

## 一、一句话结论

已冻结的 `confidence` 聚合口径是「取五个问题项置信度的**最小值**」。这个口径存在一个已实测到的
脆弱点：**任意一个问题项自报 0，整体置信度就归零**。而 Pilot 数据显示，真实任务上的 21 次
JEV 分类中有 **20 次（95%）低于冻结阈值 0.70**，与之对应的 **7 个真实任务全部耗尽了允许的
2 次额外运行**——**尽管 JEV 的分类结果本身稳定且合理**。

本记录只陈述发现，不主张修改冻结配置。裁决材料见
[`JEV_置信度口径_Adjust闸门材料_v1.0.md`](JEV_置信度口径_Adjust闸门材料_v1.0.md)。

---

## 二、聚合口径现状

`src/jev_router/real_jev.py` 的 `_parse_response()`：

```python
values["confidence"] = min(confidences)
```

`confidences` 收集五个问题项（`task_type`、`difficulty_score`、`difficulty_bucket`、
`required_capability`、`risk_level`）中每一个「存在且合法」的 `confidence` 字段。

TypeSafe System One 的 `choice` 与 `score` 是两种不同的 primitive。本发现的核心在于：
**两种 primitive 的 `confidence` 语义并不一致，用 `min()` 混在一起聚合会把差异放大成噪声。**

---

## 三、证据 A：Pilot 历史数据

数据来源：`benchmark/results/pilot_runs.jsonl`（48 条记录，其中 23 条含分类结果：
真实任务 21 条 + 受控 Mock `task_010_fallback` 2 条）。

| 指标 | 真实任务（21 条） | 含受控 Mock（23 条） |
|---|---|---|
| 置信度范围 | 0.17 ~ 0.94 | 0.17 ~ 0.94 |
| 均值 | **0.347** | 0.393 |
| 低于冻结阈值 0.70 | **20 / 21 = 95%** | 20 / 23 = 87% |
| 恰好为 0 | 0 | 0 |
| 重复次数耗尽的真实任务 | **7 / 8**（task_002 为 0.94；task_001 未调用 JEV） | — |

> 更正说明：本记录初稿曾写作「9/9 个任务全部触发」，该表述**不准确**。
> 准确情况是：`task_001` 走轻量规则、未调用 JEV；`task_002` 置信度 0.94 正常；
> 其余 **7 个任务**置信度全部低于阈值，并全部耗尽允许的重复次数。
> `workflow_state.json` 中显式登记 `jev_confidence_below` 的是 6 个任务
> （`task_005` 的 0.46 在阈值下却只登记了 `unexpected_fallback`）。

任务级明细（同一任务的多次 attempt）：

| 任务 | attempt 1 / 2 / 3 置信度 | task_type | difficulty | capability |
|---|---|---|---|---|
| task_002 | 0.94 | file_operation | 1 | low |
| task_003 | 0.48 / 0.45 / 0.44 | coding | 4 | medium |
| task_004 | 0.23 / 0.30 / 0.25 | debugging | 4 | medium |
| task_005 | 0.46 / — / 0.49 | testing | 5 | medium |
| task_006 | 0.26 / 0.27 / 0.28 | debugging | 5 | medium |
| task_007 | 0.36 / 0.28 / 0.31 | review | 6 | high |
| task_008 | 0.26 / 0.31 / 0.36 | coding | 4 | medium |
| task_009 | 0.17 / 0.19 / 0.19 | architecture | 8 | high |
| task_010_fallback | 0.88 | coding | 5 | medium |

**关键对比**：`task_type`、`difficulty_bucket`、`required_capability` 在同一任务的
三次运行中**完全一致且合理**，但置信度始终远低于 0.70。分类稳定、置信度却系统性偏低——
说明问题出在**聚合口径**，而不是分类质量。

两个 ≥0.70 的样本（task_002 的 0.94、task_010_fallback 的 0.88）恰好都是最简单的任务，
这使阈值门在效果上退化为「任何非平凡任务都触发重复」。

---

## 四、证据 B：今日探针实测（机制性解释）

对同一任务文本 `测试一下JEV`，直接读取 System One 原始响应：

```
task_type            value=testing        confidence=0.99
difficulty_score     value=3.45           confidence=0      ← 归零项
difficulty_bucket    value=low            confidence=0.91
required_capability  value=low            confidence=0.78
risk_level           value=low            confidence=0.95
```

`difficulty_score` 的原始概率分布：

```
等级0 trivial                 0.33
等级1 simple                  0.05
等级2 straightforward         0.14
等级3 moderate                0.04
等级4 moderate                0.03
等级5 substantial             0.01
等级6 difficult               0.02
等级7 very difficult          0.38
等级8 complex                 0.00
等级9 exceptionally difficult 0.00
```

这是一个明显的**双峰分布**（0.33 与 0.38），信息量充足，绝非「十档均匀、毫无把握」。
但该问题项自报 `confidence = 0`。

在同一控制组中，一个有意义的真实任务（`帮我修复登录接口偶发 500，并补一个回归测试`）
五项置信度为 0.99 / 0.74 / 0.80 / 0.68 / 0.59，整体 0.59——正常。

**结论**：`score` primitive 的 `confidence` 与 `choice` primitive 的 `confidence` 语义不同，
前者的 `0` 不能等同于「无判断」。`min()` 把这种语义差异直接转化为整体信号归零。

---

## 五、影响面

1. **去重闸门失效为常开**：7 个置信度不达标的真实任务**全部**触发了重复并耗尽允许的
   2 次额外运行，原本只应作用于「边界/存疑」任务的重复保护，实际变成了例行公事。
2. **决策有效性评估被放大**：Pilot 结论为 `mixed`，其中「Router 首轮验收 4/9」与
   `medium_model provider_timeout → high_model budget_limit_reached` 是能力/预算问题，
   但 confidence 门常开导致每个受影响任务都跑满 3 次，**放大了 timeout 的暴露面**
   （注：这是「放大」而非「造成」，不能据此推翻原结论）。
3. **后续真实自用会被反复要求重跑**：如果沿用该口径与阈值，第 11、12、13 个真实任务
   同样会频繁触发重复。

---

## 六、尚未验证的部分（不要据此下结论）

- 本记录**没有**证明「改用其他聚合方式后结论会改变」。这需要按冻结流程重跑受影响任务。
- 本记录**没有**定位 TypeSafe 对 score primitive 的 confidence 定义（未查官方文档）。
  「score 的 0 不等于无判断」是**基于概率分布的经验判断**，不是厂商口径确认。
- 证据 A 的 23 条记录只能看到聚合后的 `confidence`，看不到当时的五项分解；
  「Pilot 中的偏低也是由 score 项主导」属于**推断**，尚未被当时的原始响应证实。
  （新探针已开始记录 `answer_confidences`，用于后续证实或推翻。）

---

## 七、建议的 Adjust 动作（待授权，尚未执行）

以下只是候选方向，任何一条都需要先取得显式授权，并且**不得覆盖原始 Pilot 证据**：

1. **保留原 `confidence` 字段不变**（它是与历史可比的口径），新增一个
   `confidence_choice_only` 字段：只用三个 `choice` primitive 聚合，`score` 项单独记录。
2. **把 `jev_confidence_below` 的判定改为按维度**：区分「能力档不确定」与「难度分不确定」，
   只有前者才触发重复——因为真正影响选模的是能力档，不是难度原始分。
3. **先取证再改口径**：用适配器持续记录 `answer_confidences`，积累若干真实任务的五项分解，
   再决定是否调整，避免在少量样本上改冻结规则。

---

## 八、复现方式

```bash
# 1) 观察原始五项置信度与概率分布
cd packages/dsh-plugin
node -e "
import('./lib/jev/classifier.js').then(async (m) => {
  const r = await m.classifyJev({
    prompt: '测试一下JEV',
    apiKeyFile: '/path/to/jev-key.rtf',
  })
  console.log(r.classifier.confidence, r.answer_confidences)
})"

# 2) 复现 Pilot 统计
.venv/bin/python -m pytest -q tests/test_real_pilot.py

# 3) 适配器侧证据日志
cat ~/.dsh/jev-router/decisions.jsonl | .venv/bin/python -m json.tool --no-ensure-ascii
```

---

*本记录由 DeepSeek Harness 适配器 P0 探针产生，作为 Adjust 闸门的输入材料，不替代任何冻结结论。*
