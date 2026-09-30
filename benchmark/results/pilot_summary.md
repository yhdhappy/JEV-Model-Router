# JEV Model Router Pilot Summary

## 结论

adjust

## 任务

10（真实对比 9，受控失败 1）

## 成本

Baseline: 0.18408473
Router: 0.061786738
差异（Router - Baseline，首轮实际支出，含失败执行）: -0.122297992
成本可比任务（两种模式均 route_status=success）: task_001, task_002, task_007, task_009
Router 成本低于 Baseline 的可比任务: task_002, task_007, task_009

## 任务完成情况

Baseline: 6 / 9
Router: 4 / 9

## JEV 合理性

reasonable: 9
questionable: 0
clearly_unreasonable: 0
not_applicable: 0

## 决策有效性

结论: mixed
观察: Mixed evidence: JEV classifications were reasonable in all 9 real first attempts and controlled fallback/budget checks passed. On successful comparable runs, Router was cheaper in 3 of 4 tasks, but first-attempt acceptance was 4 of 9 versus 6 of 9 for Baseline, with repeated medium-model timeouts followed by high-model budget blocks. Adjust execution timeout, fallback, and budget parameters and rerun affected tasks before Agent integration.
- Low 判断是否通常可由低能力模型完成：由 advisor 根据结构化观察判断。
- High 判断是否更常需要高能力模型：由 advisor 根据结构化观察判断。
- 是否存在分类看似合理但选模无帮助的案例：由 advisor 判断。
- 当前证据是否足够：由 advisor 判断。

## 成本可信度

精确成本运行数: 18
估算成本运行数: 0
重复验证精确成本运行数: 27
重复验证估算成本运行数: 1
实验验证成本（record-level）: 3.1458e-05
估算成本未被当作精确账单成本；重复运行成本未混入生产对比。

## Fallback

Case A: passed; route=success; calls={'fallback_1': 1, 'primary': 1}
Case B: passed; route=failed; calls={'fallback_1': 0, 'fallback_2': 0, 'primary': 1}
fallback_tests_passed: True

## 主要发现

1. 首轮真实任务成本差异为 Router - Baseline = -0.122297992；这是首轮实际支出，包含失败执行；不含 task_010 与重复运行。
2. 首轮质量对照：Router-only=1，Baseline-only=3，both passed=3，both failed=2。
3. 重复验证任务：task_003, task_004, task_005, task_006, task_007, task_008, task_009；重复验证成本=0.43210483。
4. JEV 低/高能力观察：low observed=True, count=2；high observed=True, count=2。

## 需要调整

由 advisor 根据上述证据填写；summary builder 不自动生成调整方案。

## 是否进入真实 Agent 集成

否：先调整受影响模块并重跑受影响任务。
