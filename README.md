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

## Pilot 模板：只检查，不填写

T-14 只准备了结构模板。`benchmark/fixtures/task_001` 到 `task_009` 以及 `benchmark/fixtures/task_010_fallback` 都是模板，不是真实任务结果。

T-15 已通过，但这些目录当前仍是 `FILL_BEFORE_PILOT` 模板。只有在明确开始真实 10-task Pilot 准备时，才应逐项填入经过确认的 Prompt、模型、预算和验收条件；在真实 Pilot 运行前不得预填验收结果或 Go / Adjust / Stop 判断。`task_010_fallback` 继续复用已经定义的 T-13 受控故障入口。

只读查看模板并验证结构：

```bash
find benchmark/fixtures -mindepth 1 -maxdepth 1 -type d -print | sort
rg -n 'FILL_BEFORE_PILOT' benchmark/fixtures
.venv/bin/python -m pytest -q tests/test_pilot_templates.py
```

不要把 `pilot` CLI 的委托命令误当作真实 Pilot 已经执行。当前仓库只完成了 Pilot 模板和技术闸门；下一步需要先明确并填充 10 个真实任务，再运行正式 Pilot。模板检查的目标仍是确认结构完整、未伪造结果、未写入未经确认的真实数据，并且 fixture 初始状态没有被污染。

## 安全与阶段边界

- 不需要真实 API Key；任何凭证都不得进入 repo、fixture、JSONL、终端输出或提交。
- 当前验证只使用本地代码和 Mock Provider；没有 UI、真实 Agent Adapter 或真实网络 Provider 集成。
- CLI help 或配置校验不能证明真实模型调用；本仓库当前验收信号限定在 Router Core、Mock、fixture 和静态/本地测试证据。
- 阶段 1 的成功信号是：配置校验通过、T-15 smoke 通过、指定回归通过、完整 pytest 通过、compileall 和 `git diff --check` 无错误。完整测试数量会随后续合法测试变更而变化，因此不在此处硬编码固定总数。

## 当前交接边界

T-15 已由开发、独立审核和总顾问验收通过。阶段 1 到此结束，真实 Pilot 尚未开始。下一道闸门是：为 `task_001`..`task_010_fallback` 填写经过确认的真实任务内容并运行 10-task Pilot；在获得真实 Pilot 数据前，不作 Go / Adjust / Stop 判断。
