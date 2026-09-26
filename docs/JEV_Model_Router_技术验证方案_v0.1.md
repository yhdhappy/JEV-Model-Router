# JEV Model Router 技术验证方案 v0.1

> 文档性质：技术验证方案  
> 版本：v0.1  
> 上游依据：`JEV_Model_Router_产品方案_v0.3_冻结版.md`  
> 当前阶段：阶段 1 — Router Core + CLI / Benchmark Harness 技术验证  
> 目标：为 10 个真实任务 Pilot 准备一个可重复、可记录、可验收的最小技术实现  
> 明确不包含：正式 UI、Codex Adapter、DeepSeek Harness Adapter、PI Agent Adapter、多 Agent 集成

---

# 一、这份技术验证方案解决什么问题

产品定义已经冻结。

这一阶段不再讨论：

- 产品要不要做；
- UI 长什么样；
- 最终接 Codex 还是 DeepSeek Harness；
- 是否商业化；
- 是否做复杂学习系统。

本阶段只回答一个问题：

> **能否用一个最小、可测试的 Router Core，把“任务 → 轻量规则 → 必要时 JEV → Router → 模型 → 结果 → 成本记录”这条链稳定跑通，并为 10 个真实任务 Pilot 提供可信数据。**

阶段 1 通过，不代表产品已经成功。

阶段 1 通过只代表：

> **Router Core 的技术链路可以进入 10 个任务 Pilot。**

---

# 二、阶段 1 的边界

## 2.1 必须实现

1. Router Core。
2. Lightweight Rule Engine。
3. JEV Classifier。
4. 结构化 JEV 输出校验。
5. Model Registry。
6. Policy Engine。
7. 主模型 / 备用模型。
8. 单任务成本上限。
9. JEV 失败降级。
10. Lightweight Rule 命中留痕。
11. possible_rule_misclassification 标记。
12. 自动模型模式。
13. 手动模型覆盖。
14. 生产运行成本记录。
15. 基础成功 / 失败记录。
16. CLI。
17. Benchmark Harness。
18. Task Fixture。
19. 相同初始状态恢复。
20. Pilot 日志格式。
21. 故障注入能力。

## 2.2 明确不实现

阶段 1 不实现：

- 正式图形 UI；
- Codex Plugin；
- DeepSeek Harness Plugin；
- PI Agent Adapter；
- Claude Code Adapter；
- 多轮 Agent 路由；
- 完整分层路由；
- 自动学习策略；
- 自动修改生产策略；
- SaaS；
- 多租户；
- Dashboard；
- 用户系统；
- 自动社区 Benchmark。

如果开发过程中出现“顺手把这些也做了”的情况，应停止扩展。

---

# 三、技术栈建议

这一节属于**技术验证建议**，不是产品永久绑定。

## 3.1 推荐语言

阶段 1 推荐：

**Python 3.12+**

原因：

- CLI 和 Benchmark Harness 开发快；
- JSON / YAML / HTTP 调用简单；
- 测试工具成熟；
- 适合快速验证 JEV、Provider、日志和成本计算；
- 后续 Router Core 如果需要迁移到 TypeScript / Rust，并不影响产品定义。

如果实际开发环境只有更新版本 Python，应先跑依赖兼容测试，不要为了技术验证强行升级大量依赖。

## 3.2 推荐依赖原则

尽量少依赖。

建议优先：

- 标准库；
- `pydantic`：Schema 校验；
- `httpx`：HTTP Provider；
- `PyYAML`：配置；
- `pytest`：测试。

CLI 第一版可以直接使用 `argparse`，不必为了界面漂亮引入大型 CLI 框架。

## 3.3 不允许的技术复杂化

阶段 1 不需要：

- 数据库服务器；
- Redis；
- Kafka；
- Kubernetes；
- 微服务；
- 前端框架；
- Docker 编排。

默认本地单进程即可。

---

# 四、建议目录结构

建议项目目录：

```text
project_0010_JEV_Model_Router/
├── docs/
│   ├── JEV_Model_Router_产品方案_v0.3_冻结版.md
│   └── JEV_Model_Router_技术验证方案_v0.1.md
│
├── src/
│   └── jev_router/
│       ├── __init__.py
│       ├── cli.py
│       ├── router.py
│       ├── rules.py
│       ├── classifier.py
│       ├── policy.py
│       ├── registry.py
│       ├── providers.py
│       ├── cost.py
│       ├── fallback.py
│       ├── logging_store.py
│       ├── schemas.py
│       └── errors.py
│
├── config/
│   ├── models.yaml
│   ├── router.yaml
│   └── providers.example.yaml
│
├── benchmark/
│   ├── fixtures/
│   │   ├── task_001/
│   │   ├── task_002/
│   │   ├── ...
│   │   └── task_010_fallback/
│   ├── results/
│   ├── schemas/
│   └── runner.py
│
├── tests/
│   ├── test_schemas.py
│   ├── test_rules.py
│   ├── test_classifier.py
│   ├── test_policy.py
│   ├── test_cost.py
│   ├── test_fallback.py
│   ├── test_router.py
│   └── test_benchmark.py
│
├── pyproject.toml
├── README.md
└── .gitignore
```

阶段 1 不创建：

```text
frontend/
web/
desktop/
plugins/
adapters/
```

除非后续进入真实 Agent 集成阶段。

---

# 五、核心模块职责

## 5.1 Router Core — `router.py`

Router Core 是总协调器。

职责：

```text
接收任务
↓
检查 manual_override
↓
调用 Lightweight Rule Engine
↓
必要时调用 JEV Classifier
↓
调用 Policy Engine
↓
得到候选模型和主/备用顺序
↓
检查成本上限
↓
调用 Provider
↓
失败时 fallback
↓
记录成本和结果
↓
返回统一 RouteResult
```

Router Core 不应该：

- 自己理解每家 Provider 的 API；
- 自己保存明文 API Key；
- 自己写死模型名称；
- 自己写死任务类型规则。

---

# 六、统一输入 Schema

建议 `RouteRequest`：

```json
{
  "task_id": "task_001",
  "prompt": "修复登录失败问题并运行测试",
  "context": [],
  "manual_model": null,
  "budget_limit": 0.50,
  "metadata": {
    "source": "cli",
    "fixture_id": "task_001"
  }
}
```

字段最低要求：

| 字段 | 必需 | 说明 |
|---|---:|---|
| task_id | 是 | 当前任务唯一 ID |
| prompt | 是 | 用户任务 |
| context | 否 | 附加上下文 |
| manual_model | 否 | 用户手动指定模型 |
| budget_limit | 否 | 单任务成本上限 |
| metadata | 否 | 测试或来源信息 |

阶段 1 的 `context` 可以先支持字符串或文件引用列表，不需要做复杂多模态。

---

# 七、JEV Classifier Schema

JEV 不输出具体模型名。

建议统一输出：

```json
{
  "schema_version": "0.1",
  "task_type": "debugging",
  "difficulty_score": 6,
  "difficulty_bucket": "medium",
  "required_capability": "medium",
  "confidence": 0.86,
  "risk_level": "medium",
  "notes": "涉及多文件定位和回归测试"
}
```

## 7.1 必需字段

### `schema_version`

固定：

```text
0.1
```

便于未来升级。

### `task_type`

阶段 1 先限制为固定枚举：

```text
file_operation
coding
debugging
testing
review
architecture
research
reasoning
other
```

不要第一版就支持几十个类型。

### `difficulty_score`

整数：

```text
1 - 10
```

保留原始值，只用于日志和后续分析。

### `difficulty_bucket`

阶段 1 路由主要使用：

```text
low
medium
high
```

### `required_capability`

阶段 1同样只用：

```text
low
medium
high
```

### `confidence`

范围：

```text
0.0 - 1.0
```

### `risk_level`

阶段 1：

```text
low
medium
high
```

## 7.2 Schema 校验失败

如果 JEV：

- 返回非 JSON；
- 缺字段；
- difficulty 超范围；
- task_type 不在枚举；
- confidence 无效；

不能让系统崩溃。

记录：

```text
fallback_reason: jev_invalid_response
```

然后进入 JEV fallback 流程。

---

# 八、JEV Prompt 设计要求

技术验证阶段 Prompt 不追求“完美”，但必须可版本化。

建议保存：

```text
config/prompts/jev_classifier_v0.1.txt
```

Prompt 至少包含：

1. 明确告诉 JEV 不要选模型。
2. 只分析任务类型、难度、能力需求和置信度。
3. 强制 JSON。
4. 提供枚举范围。
5. 要求只基于当前输入判断。
6. 不让 JEV因为模型价格主动决定任务难度。
7. 禁止添加 Schema 之外字段，或允许但忽略额外字段，二选一后固定。

每次 Pilot 必须记录：

```text
classifier_prompt_version
```

否则未来 Prompt 改了以后无法比较结果。

---

# 九、Lightweight Rule Engine

## 9.1 目标

轻量规则的目的不是替代 JEV。

它只负责极明确、低风险、无需语义推理的任务。

## 9.2 第一版规则必须是白名单

示例候选：

```text
simple_file_read
simple_file_list
simple_exact_text_replace
simple_rename
simple_format_conversion
```

是否真正启用某条规则，应在技术实现时逐条写测试。

## 9.3 禁止规则

第一版禁止通过简单关键词把以下任务直接降级：

- Debug；
- 多文件修改；
- 架构；
- 安全；
- 数据迁移；
- 未知错误；
- 长上下文；
- 需要外部搜索；
- 需要大量工具调用；
- 任何目标含糊任务。

## 9.4 输出 Schema

```json
{
  "matched": true,
  "rule_id": "simple_file_read",
  "confidence": 0.99,
  "capability": "low",
  "reason": "只读单个已知文件，无修改"
}
```

未命中：

```json
{
  "matched": false
}
```

## 9.5 小跨度降级限制

技术上建议加一个硬守卫：

```text
maximum_downgrade_steps = 1
```

例如：

```text
medium → low   允许
high → medium  允许
high → low     禁止
```

阶段 1 即使某规则“觉得很简单”，也不能跨两级。

---

# 十、Model Registry

模型信息不能写死在 Router 代码里。

建议 `models.yaml`：

```yaml
models:
  low_model:
    provider: provider_a
    model_id: actual-model-id
    capability: low
    task_types:
      - file_operation
      - coding
    input_cost_per_million: 0
    output_cost_per_million: 0
    enabled: true
    credential_ref: provider-a-main

  medium_model:
    provider: provider_b
    model_id: actual-model-id
    capability: medium
    task_types:
      - coding
      - debugging
      - testing
      - review
    input_cost_per_million: 0
    output_cost_per_million: 0
    enabled: true
    credential_ref: provider-b-main

  high_model:
    provider: provider_c
    model_id: actual-model-id
    capability: high
    task_types:
      - coding
      - debugging
      - testing
      - review
      - architecture
      - reasoning
    input_cost_per_million: 0
    output_cost_per_million: 0
    enabled: true
    credential_ref: provider-c-main
```

## 10.1 重要原则

技术验证阶段可以把：

```text
low_model
medium_model
high_model
```

映射到实际使用的三个模型。

但 Router Core 只认识能力和配置，不认识“Luna / Sol / Astra 必须是什么”。

---

# 十一、Provider Layer

统一接口建议：

```python
class ModelProvider(Protocol):
    def invoke(self, request: ModelRequest) -> ModelResponse:
        ...
```

统一 `ModelResponse` 至少包含：

```json
{
  "text": "...",
  "input_tokens": 1234,
  "output_tokens": 567,
  "provider_request_id": "optional",
  "latency_ms": 1200,
  "raw_finish_reason": "stop"
}
```

## 11.1 阶段 1 只做最少 Provider

目标不是一次接所有厂商。

建议：

- JEV Provider：1 个；
- 执行模型 Provider：只实现 Pilot 真正需要的 Provider。

如果三个模型来自同一兼容 API，可只做一个通用 OpenAI-compatible Provider。

---

# 十二、凭证

阶段 1 仍然不把真实 API Key 写进仓库。

最低要求：

- 配置只保存 `credential_ref`；
- 本地测试可先从环境变量或 macOS Keychain 读取；
- 日志永远不打印完整 Key；
- 异常信息中必须脱敏。

技术验证阶段不用先实现完整 Secret Manager 抽象，但接口不能阻止以后扩展。

额外约束：

> **“日志不打印 Key”不等于“Agent 无法接触 Key”。**

阶段 1 的独立 CLI 测试可以临时使用环境变量；但进入 Codex、DeepSeek Harness、PI Agent 等真实 Agent 集成阶段前，必须重新评估凭证隔离方式。真实 Agent 可能执行终端命令或继承进程环境，因此不应默认把所有 Provider Key 作为全局环境变量暴露给 Agent 进程。

真实 Agent 集成阶段优先考虑：

- macOS Keychain；
- 最小权限 credential helper；
- 按 Provider / 进程范围注入凭证；
- 不让无关 Agent 继承不需要的密钥。

该问题不阻塞阶段 1，但必须列为进入真实 Agent 集成前的安全检查项。

---

# 十三、Policy Engine

Policy Engine 只做规则决策。

输入：

- JEV / Lightweight Rule 判断；
- 模型注册表；
- 预算；
- 启用状态；
- 任务类型。

输出：

```json
{
  "primary_model": "medium_model",
  "fallback_models": [
    "high_model"
  ],
  "reason": [
    "task_type=debugging",
    "required_capability=medium",
    "low_model capability insufficient",
    "high_model cost higher than medium_model"
  ]
}
```

## 13.1 第一版决策顺序

建议固定：

```text
1. 手动模型覆盖？
   是 → 跳过 JEV / Lightweight Rule / 模型选择策略，直接指定该模型
        但仍必须经过 Budget Guard
        若预计会突破 budget_limit，则停止，不自动改选其他模型

2. Lightweight Rule 命中？
   是 → 得到最低能力要求
   否 → 调 JEV

3. 根据 task_type 排除不支持模型

4. 根据 required_capability 排除能力不足模型

5. 排除 disabled / unavailable 模型

6. 排除预计会突破预算的模型

7. 在剩余候选中按成本升序

8. 第一名作为 primary

9. 其余满足条件的模型组成 fallback
```

阶段 1 不引入复杂权重打分。

---

# 十四、成本计算

建议 `cost.py` 提供统一函数：

```text
input_cost
output_cost
classifier_cost
fallback_cost
total_production_cost
```

单次模型费用：

```text
input_tokens / 1,000,000 × input_price
+
output_tokens / 1,000,000 × output_price
```

## 14.1 生产运行成本日志

至少记录：

```json
{
  "classifier_cost": 0.001,
  "execution_cost": 0.020,
  "fallback_cost": 0.000,
  "total_production_cost": 0.021,
  "cost_estimated": false
}
```

如果 Provider 能返回可信的 input/output token usage，则：

```text
cost_estimated: false
```

如果 Provider 不返回 Token 用量、返回字段不完整，或只能通过字符数、本地 tokenizer 或其他近似方式估算，则：

```text
cost_estimated: true
```

此时仍可记录成本估算值，但 Pilot 汇总必须明确标出它不是精确账单数据。

建议内部同时保留可选字段：

```text
cost_estimation_source: provider_usage | local_tokenizer | heuristic
```

其中 `cost_estimated` 是阶段 1 必需字段。

## 14.2 实验验证成本

不要混入上述字段。

单独记录：

```json
{
  "fixture_setup_minutes": 8,
  "human_review_minutes": 2,
  "repeat_run_cost": 0.00
}
```

---

# 十五、Fallback

## 15.1 JEV Fallback

场景：

- timeout；
- network_error；
- invalid_json；
- schema_error；
- provider_error。

记录标准化原因：

```text
jev_timeout
jev_network_error
jev_invalid_response
jev_schema_error
jev_provider_error
```

JEV 失败后建议：

```text
保守规则
↓
无法判断
↓
default_safe_model
```

不要在 JEV 失败时默认选择最便宜模型。

## 15.2 执行模型 Fallback

场景：

- model_unavailable；
- auth_error；
- timeout；
- rate_limit；
- provider_error。

执行：

```text
primary
↓
fallback_1
↓
检查累计预算
↓
fallback_2
↓
检查累计预算
↓
结束
```

每次 fallback 前必须先检查：

```text
当前累计成本 + 预计下一模型最大允许成本
<= budget_limit
```

如果不满足：

```text
fallback_reason: budget_limit_reached
```

并停止升级。

---

# 十六、手动覆盖

CLI 必须支持：

```bash
jev-router run --task benchmark/fixtures/task_001
```

自动路由。

也支持：

```bash
jev-router run --task benchmark/fixtures/task_001 --model medium_model
```

手动覆盖。

日志：

```text
route_source: manual_override
```

手动覆盖不修改全局配置。

## 16.1 手动覆盖与预算的关系

**手动覆盖只覆盖“选哪个模型”，不覆盖成本政策。**

```text
manual_model
↓
跳过 JEV / Rule / 自动选模
↓
Budget Guard
↓
允许执行 / 拦截
```

如果手动指定模型预计会突破当前 `budget_limit`，阶段 1 默认：

```text
停止执行
error_code: manual_model_budget_exceeded
```

系统不得偷偷改成其他模型，也不得因为“用户手动选了”就忽略预算。

如果用户确实希望使用该模型，应显式提高本次 `budget_limit` 后重新运行。

这样既保留用户最终选择权，也避免手动模型成为绕过预算保护的后门。

---

# 十七、统一路由结果 Schema

建议 `RouteResult`：

```json
{
  "task_id": "task_001",
  "status": "success",
  "route_source": "jev",
  "rule_id": null,
  "classifier": {
    "task_type": "debugging",
    "difficulty_score": 6,
    "difficulty_bucket": "medium",
    "required_capability": "medium",
    "confidence": 0.86
  },
  "selected_model": "medium_model",
  "fallback_history": [],
  "cost": {
    "classifier_cost": 0.001,
    "execution_cost": 0.020,
    "fallback_cost": 0,
    "total_production_cost": 0.021,
    "cost_estimated": false
  },
  "acceptance": null,
  "errors": []
}
```

失败时：

```json
{
  "status": "failed",
  "errors": [
    {
      "code": "budget_limit_reached",
      "message": "..."
    }
  ]
}
```

---

# 十八、日志设计

阶段 1 推荐：

**JSONL**

例如：

```text
benchmark/results/pilot_runs.jsonl
```

每行一个完整任务运行记录。

优点：

- 简单；
- 可追加；
- 容易被 Python / jq / Agent 分析；
- 不需要数据库。

## 18.1 禁止日志内容

默认日志不得包含：

- 完整 API Key；
- Authorization Header；
- 未脱敏凭证；
- 不必要的完整用户私密内容。

Pilot 为调试需要保存 Prompt 时，应明确放在 fixture 内，而不是在错误日志里重复泄露。

---

# 十九、Task Fixture 规范

每个任务目录：

```text
benchmark/fixtures/task_001/
├── task.yaml
├── prompt.md
├── acceptance.md
├── initial_state/
└── expected_constraints.md
```

## 19.1 `task.yaml`

建议：

```yaml
id: task_001
name: example_task
difficulty_expected_bucket: low
baseline_model: medium_model
budget_limit: 0.50

fixture:
  reset_before_run: true

acceptance:
  mode: automated
  command: "pytest -q"
  allowed_paths:
    - src/
    - tests/
```

`difficulty_expected_bucket` 不是 JEV 的“标准答案”。

它只是测试准备阶段的人工预期，供后续合理性抽查参考。

---

# 二十、相同初始状态恢复

这是 Pilot 可信度的硬要求。

每次运行：

```text
restore fixture
↓
确认 hash / commit
↓
执行任务
↓
保存结果
↓
丢弃本轮修改
↓
下一方案再次 restore
```

实现方式可以选择：

- 临时目录复制；
- Git worktree；
- Git reset 到固定 commit；
- fixture snapshot。

阶段 1 推荐优先简单、安全、不会污染真实项目的方式。

如果 Pilot 使用真实项目代码：

> **必须在隔离副本或独立 worktree 中执行，不要直接把 Benchmark 当成生产项目修改流程。**

---

# 二十一、自动验收

## 21.1 Coding / Debug / Testing

优先自动化：

- 测试；
- 构建；
- lint；
- typecheck；
- 文件修改范围；
- 预期文件存在；
- 禁止文件未改变。

AcceptanceResult 示例：

```json
{
  "passed": true,
  "checks": [
    {
      "name": "pytest",
      "passed": true
    },
    {
      "name": "allowed_paths",
      "passed": true
    }
  ]
}
```

---

# 二十二、人工评分任务

适用于：

- architecture；
- review；
- research；
- 方案分析。

每个任务在运行前就写进 `acceptance.md`。

建议 3～5 项：

```text
[ ] 覆盖全部主要约束
[ ] 无明显事实或逻辑错误
[ ] 未遗漏会改变结论的重要风险
[ ] 输出足够支持下一步行动
[ ] 无无依据的重大结论
```

每项标注：

```text
required / optional
```

运行完成后只按预定义项勾选。

---

# 二十三、10 个 Pilot 任务框架

阶段 1 技术实现完成后再从真实工作中填写具体任务内容。

不要现在为了凑数编造 10 个虚拟题。

固定槽位：

| ID | 类型 | 难度档 | 目的 |
|---|---|---|---|
| task_001 | 机械/小修改 | Low | 验证轻量规则 |
| task_002 | 机械/小修改 | Low | 验证低成本模型 |
| task_003 | Coding | Medium | 普通开发 |
| task_004 | Debug | Medium | 错误定位 |
| task_005 | Testing | Medium | 测试任务 |
| task_006 | Coding | Medium | 多步骤实现 |
| task_007 | Review/Debug | Medium~High | 复杂分析 |
| task_008 | 多文件任务 | Medium~High | 上下文与能力边界 |
| task_009 | Architecture/Reasoning | High | 高能力模型必要性 |
| task_010 | Failure Injection | N/A | fallback + budget |

这 10 个槽位可以用真实任务替换，但测试目的不要随意改变。

---

# 二十四、故障注入设计

`task_010` 不通过破坏正式凭证来实现。

优先级：

1. Mock Provider 主动返回 `model_unavailable`；
2. 测试专用模型 ID；
3. 测试专用 Provider 配置。

需要至少验证两种情况：

## Case A：Fallback 成功

```text
primary → 故障
fallback_1 → 成功
```

要求：

- 最终 status=success；
- fallback_history 有 primary 失败记录；
- fallback_reason 正确；
- fallback_cost 被计入。

## Case B：预算阻断

```text
primary → 故障
fallback_1 → 预计超预算
停止
```

要求：

```text
status=failed
error=budget_limit_reached
```

不能继续偷偷调用更贵模型。

---

# 二十五、Baseline 定义

Pilot 的 Baseline 不是“最便宜模型”。

Baseline 应是：

> **用户当前现实中如果没有 JEV Router，会采用的默认模型策略。**

例如当前日常 Agent 默认使用某个中档模型，则 Baseline 就使用这个模型。

Pilot 主比较：

```text
Baseline
vs
JEV Router
```

不要求每个任务再完整跑 low / medium / high 三个模型。

只有符合重复/边界条件才补跑相邻能力模型。

---

# 二十六、重复运行触发条件

Pilot 开始前固定。

满足任一条件才允许额外重复最多 2 次：

1. Router 模型失败，但相邻更强模型补跑成功；
2. JEV confidence 低于预设阈值；
3. difficulty_score 落在分档边界附近；
4. 人工评分为 borderline / questionable；
5. 非预期 fallback；
6. 结果直接影响 Go / Adjust / Stop 结论。

禁止：

> 因为“这次结果我不喜欢”而临时重跑。

阈值具体数值应在 Pilot 开始前写入 `benchmark/pilot_config.yaml`。

---

# 二十七、JEV 合理性人工抽查

10 个任务全部看一眼。

只记录：

```text
reasonable
questionable
clearly_unreasonable
```

抽查字段：

- task_type；
- difficulty_bucket；
- required_capability；
- confidence。

不要求人工判断：

```text
difficulty_score=6 到底是不是绝对比 7 更正确
```

重点看：

- 有没有明显离谱；
- Low / Medium / High 是否具有基本排序价值；
- 高难度是否确实更需要高能力模型。

---

# 二十八、Pilot 输出数据

建议最终产生：

```text
benchmark/results/
├── pilot_runs.jsonl
├── pilot_summary.json
├── pilot_summary.md
└── artifacts/
```

`pilot_summary.json`：

```json
{
  "total_tasks": 10,
  "baseline_total_cost": 0,
  "router_total_cost": 0,
  "baseline_passed": 0,
  "router_passed": 0,
  "fallback_tests_passed": false,
  "cost_estimated_runs": 0,
  "jev_reasonable": 0,
  "jev_questionable": 0,
  "jev_clearly_unreasonable": 0,
  "decision_effectiveness": "supports|mixed|insufficient|contradicts",
  "decision_effectiveness_notes": "",
  "decision": "go|adjust|stop"
}
```

10 个样本不输出：

```text
“成功率 93.7%”
“统计显著提升 12.4%”
```

这种伪精确结论。

---

# 二十九、Go / Adjust / Stop 闸门

## 29.1 Go

可以进入真实 Agent 集成验证，前提是：

- Core 链路稳定；
- fallback 测试通过；
- 没有预算失控；
- JEV 没有大量明显离谱判断；
- Router 没有明显造成质量恶化；
- 有初步成本收益迹象，或至少成本差异值得继续验证。

## 29.2 Adjust

出现：

- JEV 判断有明显偏差，但可通过 Prompt / Schema 调整；
- 轻量规则误判；
- 模型能力标注不合理；
- fallback 或预算逻辑存在可修复问题；
- Router 有潜力，但当前参数不稳。

处理：

> 只修受影响模块，然后重跑受影响任务。

不要重新开发 UI。

## 29.3 Stop

出现：

- Router 无法稳定工作；
- JEV 判断基本没有区分价值；
- 路由成本明显吃掉节省；
- 质量下降明显；
- 需要大量人工干预才勉强工作。

处理：

> 暂停扩展，不进入 Adapter/UI。

---

# 三十、单元测试最低要求

阶段 1 不能只靠 10 个 Pilot。

至少要有以下单元测试：

## Schema

- 合法 JEV JSON；
- 缺字段；
- 非法 difficulty；
- 非法 confidence；
- 未知 task_type；
- 非 JSON。

## Rule Engine

- 白名单命中；
- 含复杂迹象时禁止命中；
- high → low 被守卫拒绝；
- rule_id 被记录。

## Policy

- low 任务选 low；
- medium 任务不选能力不足模型；
- high 任务只保留 high；
- disabled 模型被排除；
- 不支持 task_type 模型被排除；
- 预算不足时停止。

## Fallback

- primary fail → fallback success；
- primary fail → fallback fail；
- JEV fail → safe default；
- budget limit reached。

## Cost

- input/output 价格计算；
- classifier_cost；
- fallback_cost；
- total_production_cost。

## Manual Override

- 指定模型直接绕过 JEV/规则；
- route_source 正确；
- 不修改全局配置。

## Fixture

- 每次运行前能恢复相同初始状态；
- 两轮执行不会互相污染。

---

# 三十一、阶段 1 验收命令

最终至少提供以下命令。

## 运行测试

```bash
pytest -q
```

## 校验配置

```bash
python -m jev_router.cli validate-config
```

## 单任务自动路由

```bash
python -m jev_router.cli run --fixture benchmark/fixtures/task_001
```

## 手动模型

```bash
python -m jev_router.cli run --fixture benchmark/fixtures/task_001 --model medium_model
```

## Pilot

```bash
python -m jev_router.cli pilot --config benchmark/pilot_config.yaml
```

命令名称可以在开发中微调，但能力不能缺失。

---

# 三十二、阶段 1 开发任务拆解

以下任务可以直接交给 Codex/开发 Agent。

原则：

> 每个任务尽量独立验收，不把 20 个模块一次性塞给 Agent。

---

## T-01 项目骨架与 Schema

目标：

- 建目录；
- 建 Python package；
- 定义 Pydantic Schema；
- 建基本测试。

交付：

- `schemas.py`
- `errors.py`
- `test_schemas.py`

验收：

- 合法 RouteRequest / ClassifierResult / RouteResult 可解析；
- 非法字段被拒绝；
- `pytest -q` 通过。

---

## T-02 Model Registry

目标：

- 读取 `models.yaml`；
- 校验模型能力、任务类型、价格、enabled。

交付：

- `registry.py`
- `config/models.yaml`
- `test_registry.py`

验收：

- 三层模型可加载；
- 非法价格/能力报错；
- disabled 模型可识别。

---

## T-03 Lightweight Rule Engine

目标：

- 白名单规则；
- 高置信度守卫；
- 最大降级跨度 1；
- rule_id 留痕。

交付：

- `rules.py`
- `test_rules.py`

验收：

- 简单只读任务命中；
- Debug/多文件等复杂任务不命中；
- high→low 不允许。

---

## T-04 JEV Classifier

目标：

- 调用 JEV；
- 强制结构化输出；
- Schema 校验；
- Prompt 版本记录。

交付：

- `classifier.py`
- `config/prompts/jev_classifier_v0.1.txt`
- `test_classifier.py`

验收：

- 正常 JSON 成功；
- 非 JSON / 缺字段进入标准错误；
- 不泄露凭证。

---

## T-05 Provider 抽象与 Mock Provider

目标：

- 统一 invoke 接口；
- 先实现 Mock Provider；
- 支持 success / timeout / unavailable / error。

交付：

- `providers.py`
- Mock 测试。

验收：

- 可稳定模拟 fallback 场景。

---

## T-06 Policy Engine

目标：

- task_type 过滤；
- capability 过滤；
- enabled 过滤；
- 成本排序；
- 生成 primary + fallback。

交付：

- `policy.py`
- `test_policy.py`

验收：

- 不选能力不足模型；
- 不选 disabled；
- 候选模型顺序可解释。

---

## T-07 成本计算

目标：

- Token 成本；
- classifier / execution / fallback；
- production 总成本。

交付：

- `cost.py`
- `test_cost.py`

验收：

- 给定 Token 与价格，计算值确定；
- Provider 返回可信 usage 时，`cost_estimated=false`；
- Provider 不返回 Token usage 时，允许使用约定估算方式，但必须 `cost_estimated=true`；
- 估算成本不能在 Pilot Summary 中被默认为精确账单成本。

---

## T-08 Fallback 与 Budget Guard

目标：

- JEV fallback；
- 模型 fallback；
- 单任务预算守卫。

交付：

- `fallback.py`
- `test_fallback.py`

验收：

- primary fail → fallback success；
- 预算不足 → 停止；
- 标准化 fallback_reason；
- 手动指定模型仍受 Budget Guard 约束；
- 手动模型超预算时返回 `manual_model_budget_exceeded`，不得自动换模，也不得自动忽略预算。

---

## T-09 Router Core

目标：

把前面模块串起来。

交付：

- `router.py`
- `test_router.py`

验收：

- manual override；
- light rule；
- JEV；
- policy；
- provider；
- fallback；
- cost；
- logging 全链通过。

---

## T-10 JSONL 日志

目标：

- 一次运行一条完整记录；
- 脱敏；
- 可追加。

交付：

- `logging_store.py`
- 日志测试。

验收：

- 不含 API Key；
- RouteResult、fallback、成本可追踪。

---

## T-11 CLI

目标：

实现：

```text
validate-config
run
pilot
```

交付：

- `cli.py`

验收：

- 命令退出码稳定；
- 人类可读输出 + 结构化结果文件。

---

## T-12 Benchmark Fixture / Harness

目标：

- fixture reset；
- 自动 acceptance；
- baseline / router 两种运行；
- 结果汇总。

交付：

- `benchmark/runner.py`
- `test_benchmark.py`

验收：

- 连跑两次同一个 fixture 不互相污染。

---

## T-13 Failure Injection

目标：

专门实现 task_010 所需受控故障。

验收：

### Case A

```text
primary fail
→ fallback success
```

### Case B

```text
primary fail
→ fallback 预计超预算
→ budget_limit_reached
```

两者都必须自动测试。

---

## T-14 Pilot 模板

目标：

建立 task_001 ~ task_010 空模板，不编造真实业务题。

交付：

```text
benchmark/fixtures/task_001/
...
benchmark/fixtures/task_010_fallback/
```

每个模板包含：

- task.yaml；
- prompt.md；
- acceptance.md；
- initial_state；
- expected_constraints.md。

task_010 可以预置故障注入结构。

---

## T-15 阶段 1 总验收

要求：

1. 全部单元测试通过。
2. 配置校验通过。
3. 3 个 mock smoke task 通过：
   - light rule；
   - JEV route；
   - fallback。
4. 相同 fixture 可重复运行。
5. 日志不含敏感 Key。
6. 预算守卫通过。
7. README 给出完整运行步骤。

通过后才允许填写真实 10 个 Pilot 任务。

---

# 三十三、开发 Agent 执行规则

由于项目负责人本身不负责手写代码，技术任务必须适合交给 Agent。

每个 T-xx 应采用：

```text
读取当前状态
↓
只做当前任务
↓
先写/更新测试
↓
实现
↓
运行相关测试
↓
运行完整回归
↓
输出修改清单
↓
输出实际测试结果
↓
停止
```

不要一次同时执行 T-01 到 T-15。

每完成一个任务，必须明确：

- 修改了哪些文件；
- 测试命令；
- 通过多少；
- 是否有失败；
- 是否引入额外依赖；
- 下一任务前是否存在阻塞。

---

# 三十四、禁止开发 Agent 自行做的事情

阶段 1 Agent 不得自行：

- 开发 UI；
- 接 DeepSeek Harness；
- 接 Codex；
- 增加数据库；
- 增加 Web Server；
- 引入复杂框架；
- 改产品 v0.3 冻结决策；
- 把 10 个真实 Pilot 扩成 100 个；
- 自动修改正式 Router 策略；
- 为了测试破坏真实 API Key；
- 将密钥写入仓库。

如果发现技术验证确实需要改变产品定义，应先形成：

```text
BLOCKER / 产品假设冲突报告
```

而不是直接改产品方案。

---

# 三十五、阶段 1 完成定义 Definition of Done

阶段 1 只有同时满足以下条件才算完成：

- [ ] Router Core 可独立运行。
- [ ] Lightweight Rule Engine 有白名单和跨度守卫。
- [ ] JEV 输出通过 Schema 校验。
- [ ] JEV 故障可 fallback。
- [ ] Model Registry 不写死模型。
- [ ] Policy 能按类型 + 能力 + 成本选择。
- [ ] 主模型和备用模型可工作。
- [ ] Budget Guard 可阻止超预算 fallback。
- [ ] Manual Override 可工作，且手动模型不能绕过 Budget Guard。
- [ ] 成本统计可工作。
- [ ] 无精确 usage 时会标记 `cost_estimated=true`。
- [ ] JSONL 日志可工作且不泄露密钥。
- [ ] CLI 可运行。
- [ ] Fixture 可恢复相同起点。
- [ ] Mock Failure Injection 可运行。
- [ ] 自动测试全部通过。
- [ ] Pilot 10 个任务模板已准备。
- [ ] README 能让另一个 Agent 从零运行测试。
- [ ] 没有 UI。
- [ ] 没有真实 Agent Adapter。

只要最后两项被违反，就说明阶段边界失控。

---

# 三十六、进入 10 个 Pilot 前的闸门

在花真实模型费用之前，先做免费/低成本准备检查。

必须确认：

1. Mock 模式全通过。
2. JEV Provider 能正常返回 Schema。
3. 三层模型配置可读取。
4. 价格配置已核对。
5. budget_limit 已设置。
6. 日志路径正确。
7. 凭证不会被日志输出。
8. task_010 fallback 用 Mock 已通过。
9. Baseline 模型已经确定。
10. 10 个任务验收标准在运行前写好。

只有全部满足，才开始真实 Pilot。

---

# 三十七、10 个 Pilot 的执行顺序

建议不要一次全跑。

顺序：

```text
task_001
↓
检查日志和费用
↓
task_002
↓
检查
↓
...
task_009
↓
task_010 fallback
```

每个任务完成后立刻确认：

- 初始状态是否正确；
- JEV 输出是否记录；
- Router 选了什么；
- 费用有没有异常；
- 验收结果；
- 是否触发 repeat condition。

如果前两个任务就发现成本计算或日志错误：

> 立即停，不要继续把剩下 8 个费用烧掉。

---

# 三十八、Pilot Summary 模板

最终 `pilot_summary.md` 建议：

```markdown
# JEV Model Router Pilot Summary

## 结论
Go / Adjust / Stop

## 任务
10

## 成本
Baseline:
Router:
差异:

## 任务完成情况
Baseline:
Router:

## JEV 合理性
reasonable:
questionable:
clearly_unreasonable:

## 决策有效性
结论: supports / mixed / insufficient / contradicts
观察:
- Low 判断是否通常可由低能力模型完成:
- High 判断是否更常需要高能力模型:
- 是否存在“分类看似合理但选模无帮助”的案例:
- 当前证据是否足够:

## 成本可信度
精确成本任务数:
估算成本任务数:
是否存在会影响结论的成本估算:

## Fallback
Case A:
Case B:

## 主要发现
1.
2.
3.

## 需要调整
1.
2.

## 是否进入真实 Agent 集成
是 / 否
```

---

# 三十九、阶段 1 的技术风险

## 风险 1：JEV 输出不稳定

处理：

- Schema；
- Prompt 版本化；
- retry 只允许有限次数；
- invalid response fallback。

## 风险 2：Provider Token 用量字段不一致

处理：

- Provider Layer 统一归一化；
- 无法获得真实 Token 时必须标记 `cost_estimated=true`；
- `RouteResult.cost` 必须携带该字段；
- Pilot Summary 必须汇总 `cost_estimated_runs`；
- 不允许默默当成精确成本。

## 风险 3：模型价格配置错误

处理：

- 价格版本化；
- Pilot 前人工核对；
- 日志记录 price_source / config_version。

## 风险 4：Fixture 污染

处理：

- 每次 reset；
- hash / commit 校验；
- 隔离目录。

## 风险 5：Fallback 无限升级

处理：

- 最大 fallback 次数；
- budget_limit；
- visited_models 防循环。

## 风险 6：Light Rule 误判

处理：

- 白名单；
- 小跨度；
- possible_rule_misclassification；
- 抽样审计。

## 风险 7：Pilot 太小

处理：

- 不输出伪精确统计；
- 只做 Go / Adjust / Stop；
- 后续真实使用继续积累。

---

# 四十、技术验证阶段的最终原则

阶段 1 的目标不是写一个漂亮产品。

目标也不是证明 JEV Router 已经成功。

目标是：

> **用最少代码、最少费用和最少变量，验证 Router Core 是否值得进入真实 Agent 集成。**

因此：

- 能用 Mock 验证的，不先烧真实模型费用；
- 能用 CLI 验证的，不先做 UI；
- 能单独测 Core 的，不先接 Agent；
- 能预先写验收标准的，不事后凭感觉；