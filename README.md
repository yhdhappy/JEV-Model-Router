# JEV Model Router

**阶段 1：Router Core + CLI / Benchmark Harness 技术验证已通过 T-15 总验收。**

这表示 Router Core 的技术链路已经满足进入 10 个真实任务 Pilot 的前置条件；不代表产品已经成功，也不代表真实 Agent 集成或生产能力已经验证完成。

## 阶段 1 是什么，不是什么

阶段 1 要验证一条最小、可测试、可重复的链路：

```text
任务 → Lightweight Rule → 必要时 JEV → Policy → Mock/Provider 边界 → 结果与成本记录
```

阶段 1 包含：Router Core、模型配置校验、Lightweight Rule、JEV Schema 边界、Policy、主模型与 fallback、Budget Guard、成本合同、JSONL 安全日志、CLI、T-12 fixture/harness，以及空的 Pilot 模板。

阶段 1 不包含：正式 UI、真实 Agent Adapter（包括 Codex、DeepSeek Harness、PI Agent）、真实网络 Provider 集成、多 Agent 集成，也不包含真实 10-task Pilot 的执行或 Go / Adjust / Stop 判断。CLI 或 Mock 通过只证明 Router Core 的验证链可运行，不等于真实 Agent 集成或生产能力已经证明。

## 从零开始

项目根目录示例：

```bash
cd /Users/yhd/Documents/AI_Workspace/project_0010_JEV_Model_Router
```

以下命令均假定当前目录已经是仓库根目录；不要在子目录中运行相对路径命令。

### 前置条件

- Python 3.9+ 受当前项目支持。
- 技术方案推荐 Python 3.12+，但当前阶段验证不要求先升级到 3.12。
- 阶段 1 不需要真实 API Key、网络 Provider 或付费额度。不要把 Key 写入仓库、fixture、日志、测试输出或 shell profile。

创建并激活虚拟环境，然后以 editable 模式安装开发依赖：

```bash
python3 -m venv .venv
source .venv/bin/activate
.venv/bin/python -m pip install -e '.[dev]'
```

### 配置校验

直接使用项目 CLI 校验当前模型注册表：

```bash
.venv/bin/python -m jev_router.cli validate-config
```

成功信号是输出 `configuration valid`，并显示当前 3 个模型、3 个 enabled。也可以查看 CLI 帮助：

```bash
.venv/bin/python -m jev_router.cli --help
.venv/bin/python -m jev_router.cli validate-config --help
```

### T-15 专项 smoke

规范命令是：

```bash
.venv/bin/python -m pytest -q tests/test_stage1_acceptance.py
```

专项套件覆盖配置加载、light rule、JEV route、fallback、Budget Guard、fixture 隔离重复运行、JSONL 脱敏、估算成本合同和 manual override 预算保护。测试使用本地确定性 Mock，不需要 API Key。

### 指定回归与完整回归

T-15 相关回归：

```bash
.venv/bin/python -m pytest -q \
  tests/test_router.py \
  tests/test_benchmark.py \
  tests/test_logging_store.py \
  tests/test_failure_injection.py
```

完整回归的规范命令是：

```bash
.venv/bin/python -m pytest -q
```

重要：请使用 `.venv/bin/python -m pytest -q`，不要直接运行 `.venv/bin/pytest`。项目顶层 benchmark 的导入在 console-script 路径下有已知 caveat，使用 Python module 入口才能保持仓库根目录下的规范导入路径。

其他本地完整性检查：

```bash
PYTHONPYCACHEPREFIX=/private/tmp/jev-router-pycache \
  .venv/bin/python -m compileall -q src benchmark tests
git diff --check
```

## Pilot 任务准备状态

`benchmark/fixtures/task_001` 到 `task_009` 以及 `benchmark/fixtures/task_010_fallback` 已经全部从空模板升级为可审查的 Pilot 任务定义。`task_001`～`task_009` 都来自当前仓库中的真实问题或真实下一步工作；`task_010_fallback` 是受控 Mock 故障注入任务。

这不表示真实 10-task Pilot 已经运行。当前状态仍是 `real_pilot_started=false`：

- 真实 Provider Gate 已通过：JEV System One 与本机 OpenCode Go 已完成真实端到端单请求验证，Router 能取得 JEV 分类、真实模型执行结果、usage 和 provider-reported cost。
- `task_003`～`task_009` fixture 内的 `FILL_BEFORE_REAL_PILOT` 仍保留为冻结快照中的执行前占位，不代表真实 Provider 未接通；正式 Pilot runner 会在运行时注入真实模型层级和预算。
- `task_010_fallback` 继续使用 T-13 受控 Mock 故障入口，用来验证 fallback / Budget Guard，不冒充真实 Provider 成功。
- 在获得真实 Pilot 数据前，不预填结果、分数或 Go / Adjust / Stop 判断。

PILOT_RUNNER_WIRING 已通过：离线接线、两轮真实 task_002 gate smoke、总顾问验收和独立 review 均通过。Baseline / Router 使用独立 workspace 与独立 provider 状态；真实 Router 路径已实际调用 JEV，验收通过，且 gate 结果只写 `pilot_runner_gate_smoke.jsonl`。当前仍保持 `real_pilot_started=false`；下一步是 `PILOT_CONFIG_FREEZE`，冻结正式 Pilot 的 Baseline、预算、重复运行阈值和官方结果写入规则。

阶段一 Policy 中，required_capability 表示可接受模型能力的下限；在满足 task type、enabled 和能力下限的 eligible 模型中，系统仍按当前静态 price proxy 排序。因此，更高能力 tier 如果 proxy 更低，也可能被选中。gate smoke 曾观察到 low file_operation 选择 medium_model；这只是当前路由规则行为，不是官方 Pilot 的成本结论。

仅在明确授权进行一次 gate smoke 时使用以下命令；它需要本机已有的 JEV_API_KEY_FILE，只写 benchmark/results/pilot_runner_gate_smoke.jsonl，不会写官方 pilot_runs.jsonl：

~~~bash
JEV_API_KEY_FILE=/path/to/jev-key.rtf \
  .venv/bin/python scripts/real_pilot_gate_smoke.py benchmark/fixtures/task_002 \
  --baseline-model low_model \
  --budget-limit 1.00 \
  --max-cost low_model=0.10 \
  --max-cost medium_model=0.25 \
  --max-cost high_model=1.00 \
  --output benchmark/results/pilot_runner_gate_smoke.jsonl
~~~

检查 10 个已准备任务的结构与状态：

```bash
find benchmark/fixtures -mindepth 1 -maxdepth 1 -type d -print | sort
rg -n 'FILL_BEFORE_REAL_PILOT|controlled_mock_only_not_real_provider' benchmark/fixtures/*/task.yaml
.venv/bin/python -m pytest -q tests/test_pilot_templates.py
```

不要把 `pilot` CLI 的委托命令误当作真实 Pilot 已经执行。真实 Provider Gate 与 `PILOT_RUNNER_WIRING` 均已通过；当前正式闸门是 `PILOT_CONFIG_FREEZE`。配置冻结并通过审查后，才允许创建官方 `benchmark/results/pilot_runs.jsonl` 并运行正式 10-task Pilot。

## 安全与阶段边界

- 真实 Provider smoke 需要运行时凭证，但凭证只能由本机安全路径/认证环境提供；任何 Key 都不得进入 repo、fixture、JSONL、终端输出或提交。
- 当前已经具备真实 JEV HTTP adapter 与本机 OpenCode Go execution provider；仍没有 UI，也没有面向外部 Agent 产品的正式 Adapter。
- 真实 Provider Gate 的验收证据包含真实 JEV + Router + OpenCode Go 单请求 smoke；这仍不等于 10-task Pilot 已经执行。
- 阶段 1 的成功信号是：配置校验通过、T-15 smoke 通过、指定回归通过、完整 pytest 通过、compileall 和 `git diff --check` 无错误。完整测试数量会随后续合法测试变更而变化，因此不在此处硬编码固定总数。

## 当前交接边界

T-15、10 个 Pilot 任务准备、真实 Provider Gate 与 `PILOT_RUNNER_WIRING` 均已完成。真实 Pilot 尚未开始。当前执行闸门是 `PILOT_CONFIG_FREEZE`；在冻结 Baseline、预算、重复运行阈值和官方结果写入策略并通过审查前，不运行正式 10-task Pilot；在获得真实 Pilot 数据前不作 Go / Adjust / Stop 判断。
