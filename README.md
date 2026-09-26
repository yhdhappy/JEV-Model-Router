# JEV Model Router

当前处于 **阶段 1：Core + CLI 技术验证**。

已完成：
- T-01：项目骨架、核心 Schema、错误类型和 Schema 测试
- T-02：Model Registry（models.yaml 加载、能力/任务类型/价格/enabled 校验）
- T-03：Lightweight Rule Engine（白名单规则、高置信守卫、单步降级、rule_id 留痕）
- T-04：JEV Classifier（版本化 Prompt、JSON 解析、Schema 校验、可注入调用边界）
- T-05：Provider 抽象与 Mock Provider（统一 invoke 契约、确定性 success/timeout/unavailable/error 故障注入）

当前下一任务：**T-06 Policy Engine**。

阶段 1 暂不开发 UI，不接真实 Agent Adapter。

## 本地开发

\`\`\`bash
python3 -m venv .venv
.venv/bin/python -m pip install -e '.[dev]'
.venv/bin/python -m pytest -q
\`\`\`

当前项目保持 Python 3.9+ 兼容；技术方案中的 Python 3.12+ 是阶段 1 的推荐环境，不是当前系统升级的前置条件。
