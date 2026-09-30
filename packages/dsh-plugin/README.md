# dsh-plugin-jev-router

JEV Model Router 的 DeepSeek Harness 适配器 —— **P0 探针版本**。

## 这一版是什么，不是什么

**是**：一个只做判断、不做接管的插件。注册 `/jev` 命令，把一段任务文本送进真实的 JEV
（TypeSafe System One）API，把判定结果和成本报给你，并写入决策日志。

**不是**：它**不会**拦截、改写或延迟任何一次模型请求。装上它之后，Agent 用什么模型、
一次运行花多少钱，和没装之前完全一样。这是刻意的设计：P0 的唯一目的是在真实 DSH 任务上
验证「JEV 判得准不准」，把风险压到零。

真正的「自动选择模型」属于 P1，尚未实现。

## 它判什么

| 字段 | 含义 |
|---|---|
| `task_type` | 任务类型（coding / debugging / review / …） |
| `difficulty_score` | 1–10 原始难度分 |
| `difficulty_bucket` | low / medium / high |
| `required_capability` | 所需的最低模型能力档 |
| `risk_level` | 风险级别 |
| `confidence` | 置信度（取各问题置信度的最小值，与 Python 版口径一致） |

命令还会附一行**参考模型**，取自已冻结的三层配置（low=`glm-5.3-flash`、
medium=`qwen3.8-flash`、high=`gpt-5.6-luna`）。这一行只按能力下限映射，**没有**经过完整的
Policy Engine（任务类型过滤、价格排序、预算守卫、fallback 链），所以它只是参考。

## 用法

在 Harness Web 里：

```
/jev 帮我修复登录接口偶发 500，并补一个回归测试
```

也可以只打 `/jev`，插件会取你**最近一条消息**作为任务文本。

每次调用都会往 `~/.dsh/jev-router/decisions.jsonl` 追一条记录（路径可配置），内容只有判定
结果和安全指标，永远不含密钥。

## 安装

用 `plugin_manager` 工具的 `install_bundle`，`target` 指向本目录的绝对路径。不要手写
profile 的 `package.json` / `cordis.patch.yml`，也不要手工跑 pnpm —— `install_bundle`
会完成这些步骤。

## 配置

配置写在本包的 `cordis.patch.yml` 里：

| 字段 | 默认 | 说明 |
|---|---|---|
| `apiKeyFile` | 无（回退到 `JEV_API_KEY_FILE` 环境变量） | JEV 密钥文件路径，支持 RTF 与纯文本 |
| `decisionLog` | `~/.dsh/jev-router/decisions.jsonl` | 决策日志路径 |
| `timeoutMs` | `30000` | 单次 JEV 调用超时（毫秒） |
| `modelLayers` | 见上表 | 能力档 → 模型名的参考映射 |

`apiKeyFile` 是本包中**唯一一处机器相关配置**。对外分享前必须替换掉，或删除它改用
`JEV_API_KEY_FILE`。

## 与 Python 参考实现的一致性

`lib/jev/` 是 `src/jev_router/real_jev.py` 的逐字段镜像，并已实测确认：

- **请求体逐字节一致** —— 同一个任务文本生成的 JSON 完全相同（1430 字节）；
- **密钥解析结果一致** —— 对同一个 RTF 文件，两边提取出的密钥 SHA-256 完全相同；
- **难度映射一致** —— `round-half-up(raw + 1)` 再截断到 1–10；
- **成本口径一致** —— 只对 input token 计价（$0.042/M），output token 只记录不计价；
- **置信度口径一致** —— 取所有问题置信度的最小值。

## 开发

```bash
# 离线测试（不联网）
npm test

# 含一次真实 JEV 调用的集成测试
JEV_TEST_KEY_FILE="/path/to/jev-key.rtf" npm test

# 只做一次分类，用于人工核对
node scripts/smoke.mjs "任务文本" "/path/to/jev-key.rtf"
```

## P0 之后

- **P0.5**：加 `jev_classify` 工具，让 Agent 自己也能调用 JEV。前置条件是先验证
  profile 安装的 bundle 能否 import `@deepseek-ai/*` 包（本版刻意零 import 以规避该风险）。
- **P1**：注册 `jev-router` provider，让 `/model` 出现「自动选择」，并按**用户轮次**
  （而不是按请求）路由，整轮持有同一个模型。
- **P2**：界面展示「为什么选它、花了多少钱」；JEV 密钥改走 DSH 凭证层。
