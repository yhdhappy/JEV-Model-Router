# JEV Model Router

当前处于 **阶段 1：Core + CLI 技术验证**。

已完成：
- T-01：项目骨架、核心 Schema、错误类型和 Schema 测试
- T-02：Model Registry（models.yaml 加载、能力/任务类型/价格/enabled 校验）
- T-03：Lightweight Rule Engine（白名单规则、高置信守卫、单步降级、rule_id 留痕）
- T-04：JEV Classifier（版本化 Prompt、JSON 解析、Schema 校验、可注入调用边界）
- T-05：Provider 抽象与 Mock Provider（统一 invoke 契约、确定性 success/timeout/unavailable/error 故障注入）
- T-06：Policy Engine（task_type/capability/enabled 过滤、确定性成本代理排序、primary + fallback 与可解释 reason）
- T-07：成本计算（Token 费用、classifier/execution/fallback 分项、production 总成本、精确/估算成本标记）
- T-08：Fallback 与 Budget Guard（JEV safe-default 信号、模型 fallback、单任务预算守卫、manual model 预算约束）
- T-09：Router Core（manual / light rule / JEV / policy / provider / fallback / cost 全链串联，终态 result_sink 接口）
- T-10：JSONL 日志（默认最小安全投影、追加写入、脱敏、成本/fallback/错误码可追踪）
- T-11：CLI（validate-config / run / pilot 稳定命令边界、退出码、结构化结果文件协议与 T-12 可插拔执行接口）
- T-12：Benchmark Fixture / Harness（隔离初始状态、baseline/router 双运行、自动/人工验收、路径约束、稳定结果与 acceptance timeout）
- T-13：Failure Injection（task_010 可复用受控故障入口；primary→fallback 成功与预算阻断两种 Router 终态自动验证）
- T-14：Pilot 模板（task_001~task_010_fallback 空模板、预运行占位、T-13 故障场景引用与防伪结构校验）

当前下一任务：**T-15 阶段 1 总验收与收尾**。

阶段 1 暂不开发 UI，不接真实 Agent Adapter。

## 本地开发

\`\`\`bash
python3 -m venv .venv
.venv/bin/python -m pip install -e '.[dev]'
.venv/bin/python -m pytest -q
\`\`\`

当前项目保持 Python 3.9+ 兼容；技术方案中的 Python 3.12+ 是阶段 1 的推荐环境，不是当前系统升级的前置条件。
