# dsh-plugin-jev-router

JEV Model Router 的 DeepSeek Harness 适配器。包含两个能力：

| 能力 | 阶段 | 作用 | 是否改变模型行为 |
|---|---|---|---|
| `/jev` 命令 | P0 | 对任务文本做一次真实 JEV 判定并报告 | **不会** |
| `自动选择` | P1 | 在模型选择器里提供一个自动档，按用户轮次路由到具体模型 | 会（**只有你主动选中它时**） |

两个能力共用同一套 JEV 分类器与 Policy 镜像，都是 `src/jev_router/` 的逐字段镜像。

---

## 一、`/jev`：只做判断（P0）

```
/jev 帮我修复登录接口偶发 500，并补一个回归测试
```

只打 `/jev` 时，插件会取你**最近一条消息**作为任务文本。

输出示例：

```
JEV 判定（jev-1.13.0｜934ms｜$0.000032｜输入：命令参数）

任务类型：testing
难度：4 / 10（low）
所需能力：low
风险级别：low
置信度：0（取各项最小值，与已冻结口径一致）
各项置信度：类型 0.99｜难度 0｜分档 0.91｜能力 0.77｜风险 0.94

⚠ difficulty_score 自报置信度为 0，已把整体置信度拉到 0。…

参考模型（与"自动选择"同一套 Policy）：opencode-go / glm-5.3-flash
落选原因：备选 medium_model → high_model

说明：本命令只做判断，不会改变本次请求使用的模型。
```

### 关于「置信度」这一行的坑

整体的 `置信度` 严格等于五项置信度的**最小值**，这是与 Python 参考实现一致的冻结口径，
不是本插件新定的规则。

但**这个口径有已知脆弱点**：任意一项自报 0，整体就归零。实测中 `difficulty_score` 会在
概率分布明显有信息量时（例如双峰 0.33 / 0.38）依然返回 `confidence: 0`。所以本插件额外打印
`各项置信度`，并在有 0 值项时给出提示——**只是让这个数字可解释，不改变任何判定**。

证据与影响分析见：

- [发现记录 v0.1](../../docs/dsh-integration/JEV_置信度聚合口径_发现记录_v0.1.md)
- [Adjust 闸门裁决材料 v1.0](../../docs/dsh-integration/JEV_置信度口径_Adjust闸门材料_v1.0.md)

---

## 二、`自动选择`：真正省钱的那一步（P1）

### 怎么用

在 `/model`（或输入框的模型菜单）里选择：

```
JEV Router → 自动选择（JEV 智能路由）
```

选中之后，**每次你发一条新消息**，插件会：

1. 对这条消息跑一次 JEV 判定（约 750 ms，约 $0.00003）；
2. 用与 Python 版**完全一致**的 Policy 选出具体模型；
3. 把整轮请求（含所有工具往返）都交给这一个模型。

不选它，插件对模型行为**零影响**。

### 关键设计决定

| 决定 | 原因 |
|---|---|
| **按"用户轮次"路由，不是按请求** | 一轮对话包含很多次模型请求（工具往返）。若每次都判定，既给每一步加税，又会在轮次中途换模型，破坏缓存与上下文连续性。 |
| **走 `llm/stream` 瀑布监听器，而不是合成适配器内部转发** | `LlmRuntime.adapterStream()` 会在派发前调用 `forAdapter()`，把**不属于当前适配器**的历史 replay state 剥掉。合成适配器转发会丢掉它；瀑布监听器看到的是未被改写的原始请求，直接重派给真实适配器，**replay state 得以保留**。 |
| **合成适配器只负责让选择器显示 `auto`** | `listModels` / `resolveModel` 是目录驱动界面的硬要求；适配器自身的 `stream` 只是瀑布未生效时的兜底路径。 |
| **`inputModalities` 必须声明 image** | 真实路由支持图片。若声明为纯文本，运行时会**在插件看到请求之前**把所有图片替换成占位符。 |
| **判定失败 → 回退到 safe default，绝不回退到最便宜档** | 沿用已冻结的阶段 1 规则。插件永远不会因为自己出错而卡住你的对话。 |
| **相同任务文本只判定一次** | 有界缓存（默认 32 条），避免重复轮次重复付费。 |

### 这一版**还没有**的东西

- **没有模型 fallback 链**：一轮内换模型在流式输出开始后不是安全的本地决策，本版交给 DSH 已有的重试机制处理。
- **没有 Budget Guard**：单任务预算上限尚未接入。
- **没有轻量规则层**：每次都调 JEV。按当前价格这是 $0.00003/次，成本上可忽略；主要代价是每轮首次请求约 +750 ms 延迟。
- **没有界面展示**：路由原因目前只写进决策日志，尚未在界面显示。

以上都属于 P2。

---

## 三、安装

用 `plugin_manager` 工具的 `install_bundle`，`target` 指向本目录的绝对路径。不要手写
profile 的 `package.json` / `cordis.patch.yml`，也不要手工跑 pnpm —— `install_bundle`
会完成这些步骤。

修改本包 `lib/` 下的 JS 后，需要**重启 DSH** 才会加载新的模块代际（配置文件的改动可以热更新，模块不能）。

## 四、配置

配置写在本包的 `cordis.patch.yml` 里：

| 字段 | 默认 | 说明 |
|---|---|---|
| `apiKeyFile` | 无（回退到 `JEV_API_KEY_FILE` 环境变量） | JEV 密钥文件路径，支持 RTF 与纯文本 |
| `decisionLog` | `~/.dsh/jev-router/decisions.jsonl` | 决策日志路径 |
| `timeoutMs` | `30000` | 单次 JEV 调用超时（毫秒） |
| `autoRoute` | `true` | 设为 `false` 则**完全不注册**"自动选择" |
| `safeDefault` | `opencode-go / gpt-5.6-luna` | JEV 失败或无可选模型时使用的模型 |
| `cacheSize` | `32` | 任务文本判定结果的有界缓存条数 |
| `models` | 见 `lib/jev/models.js` | 可覆盖的模型表（名称/provider/模型 id/能力档/任务类型/价格/启用） |

`apiKeyFile` 是本包中**唯一一处机器相关配置**。对外分享前必须替换掉，或删除它改用
`JEV_API_KEY_FILE`。

### 关掉自动路由

```yaml
- id: jev-router
  config:
    autoRoute: false
```

---

## 五、与 Python 参考实现的一致性

`lib/jev/` 是 `src/jev_router/` 的逐字段镜像，并已实测确认：

| 项 | 验证方式 | 结果 |
|---|---|---|
| 请求体 | 与 `real_jev.py` 对同一任务生成的 JSON 比对 | **逐字节一致**（1430 字节） |
| 密钥解析 | 对同一个 RTF 文件比对 SHA-256 | **完全一致**（108 字符） |
| 难度映射 | `round-half-up(raw + 1)` 截断 1–10 | 一致 |
| 成本口径 | 只对 input token 计价（$0.042/M） | 一致 |
| 置信度 | 取五项最小值 | 一致 |
| **Policy** | 27 种「任务类型 × 能力档」组合逐一比对 | **27/27 完全一致**，含 `coding|low` 选中 `medium_model` 这一冻结特性 |

Policy 一致性的测试是自动化的：`tests/policy.test.mjs` 会调用 `.venv` 里的 Python 版逐条对拍，
`.venv` 不存在时自动跳过。

---

## 六、开发

```bash
# 离线测试（不联网）
npm test

# 含一次真实 JEV 调用的集成测试
JEV_TEST_KEY_FILE="/path/to/jev-key.rtf" npm test

# 只做一次分类，用于人工核对
node scripts/smoke.mjs "任务文本" "/path/to/jev-key.rtf"
```

## 七、后续

- **P2**：界面展示「为什么选它、花了多少钱」；Budget Guard；模型 fallback 链；轻量规则层；
  JEV 密钥改走 DSH 凭证层。
- **P0.5**：加 `jev_classify` 工具，让 Agent 自己也能调用 JEV。前置条件是先验证
  profile 安装的 bundle 能否 import `@deepseek-ai/*` 包（本版刻意零 import 以规避该风险）。
