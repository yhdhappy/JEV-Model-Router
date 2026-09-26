# JEV Model Router 产品方案 v0.1

## 一、产品定位

JEV Model Router 是一个独立的、通用的 AI 模型智能路由系统。

它位于用户/Agent 与大模型之间，在真正调用大模型之前判断任务，根据：

**任务难度 + 成本 + 任务类型**

决定应该使用哪个模型。

例如当前可以配置：

- GPT 6 Luna
- GPT 6 Sol
- GPT 6 Astra
- DeepSeek
- Claude
- Qwen
- 本地模型

这些模型名称都不写死在程序中，以后增加或删除模型只需要修改配置。

它的目标不是“永远选择最强模型”，而是：

> 在能够可靠完成任务的前提下，尽量使用成本更低的模型。

---

## 二、核心工作流程

完整链路：

```text
用户提交任务
      ↓
是否手动指定模型？
   ↓          ↓
  是          否
   ↓          ↓
指定模型    轻量规则判断
               ↓
        是否能够明确判断？
          ↓          ↓
         是          否
          ↓          ↓
      Router      调用 JEV
                     ↓
              输出结构化判断
                     ↓
                  Router
                     ↓
     任务类型 + 难度 + 成本策略
                     ↓
                候选模型
                     ↓
          主模型 → 备用模型
                     ↓
                执行任务
                     ↓
              收集执行结果
                     ↓
              路由效果统计
                     ↓
            生成策略优化建议
```

---

## 三、JEV 的职责

一个重要原则：

**JEV 不负责直接决定模型。**

JEV 只负责理解任务。

例如：

```json
{
  "task_type": "coding",
  "difficulty": 7,
  "required_capability": "high",
  "cost_sensitivity": "medium",
  "context_size": "medium",
  "confidence": 0.91
}
```

真正选择哪个模型由 Router 决定。

这样以后即使：

GPT 6 Sol 被替换、
价格发生变化、
新增 DeepSeek、
新增本地模型，

都不需要修改 JEV 的判断逻辑。

---

## 四、两级判断机制

不是每个任务都调用 JEV。

第一层是非常轻量的规则判断。

比如：

```text
改文件名
读取文件
把一句英文翻译成中文
修改一个简单变量
```

如果能够非常确定，就直接路由。

判断不明确，例如：

```text
分析整个项目架构
修复未知 Bug
进行代码重构
设计数据库
分析复杂技术方案
```

才调用 JEV。

原则：

> 规则只处理“明显任务”，复杂判断交给 JEV。

避免重新开发一个比 JEV 还复杂的规则系统。

---

## 五、模型选择逻辑

三个主要因素：

### 1. 任务类型

例如：

```text
coding
debugging
architecture
research
writing
translation
review
testing
reasoning
data-analysis
```

首先排除不适合某类任务的模型。

### 2. 难度

例如使用 1～10：

```text
1–3  简单
4–6  中等
7–8  困难
9–10 极难
```

但难度不直接绑定具体模型。

模型能力由配置决定。

### 3. 成本

在满足任务能力要求的候选模型中选择成本较低者。

因此不是：

> 最便宜模型优先。

而是：

> 能完成任务的模型里面，成本更低者优先。

---

## 六、模型策略

每一种任务可以配置：

```text
主模型
备用模型 1
备用模型 2
成本上限
```

例如：

```text
任务：
普通代码开发

主模型：
GPT 6 Sol

备用：
DeepSeek
GPT 6 Astra

单任务成本上限：
$0.50
```

主模型不可用、超时、额度不足后，才进入备用链。

达到成本上限以后停止继续升级。

---

## 七、JEV 故障降级

JEV 不能成为整个系统的单点故障。

因此：

```text
JEV 正常
→ 正常路由

JEV 超时
→ 本地规则判断

仍无法判断
→ 默认保底模型

保底模型不可用
→ 备用模型链
```

同时记录：

```text
fallback_reason: jev_timeout
```

以后可以统计 JEV 本身的稳定性。

---

## 八、分层路由

Router 不应该只在一项大任务开始时判断一次。

同时也不能每一步都调用 JEV。

采用：

**分层路由。**

例如：

```text
开发登录系统
→ Sol

架构设计阶段
→ Astra

修改按钮文字
→ Luna

安全审查
→ Astra

普通测试
→ Sol
```

只有在：

任务类型明显变化、
任务难度明显变化、
上下文明显扩大，

才重新进行路由判断。

---

## 九、用户手动选择模型

这是 UI 的重要功能。

任务输入区域旁边提供：

```text
模型

● 自动选择（默认）
○ GPT 6 Luna
○ GPT 6 Sol
○ GPT 6 Astra
○ DeepSeek
○ Claude
○ 其他模型……
```

默认：

**自动选择**

代表：

```text
规则 → JEV → Router
```

如果用户手动选择：

```text
GPT 6 Astra
```

则此次任务直接使用 Astra。

记录：

```text
route_source: manual_override
```

但不会修改全局路由规则。

下一次任务仍恢复：

**自动选择。**

---

## 十、UI 与配置文件

产品必须同时支持两类用户。

### 普通用户

完全通过 UI 使用。

不要求懂：

```text
YAML
JSON
API
终端
环境变量
```

可以直接在界面完成：

模型添加、
API 配置、
成本设置、
模型能力设置、
主模型设置、
备用模型设置、
自动路由、
手动选模型、
路由日志查看。

### 开发者

可以直接编辑：

```text
models.yaml
router.yaml
providers.yaml
privacy.yaml
```

UI 与配置文件双向同步。

---

## 十一、UI 首页建议

首页不要做得复杂。

顶部：

```text
JEV Model Router

状态：运行中
JEV：正常
运行模式：Local
```

主要区域：

```text
【模型】

GPT 6 Luna     可用
GPT 6 Sol      可用
GPT 6 Astra    可用
DeepSeek       可用

【路由策略】

自动路由：开启

判断因素：
✓ 任务类型
✓ 难度
✓ 成本

【最近一次路由】

任务：代码修改
JEV 难度：6
任务类型：Coding
选择模型：GPT 6 Sol
原因：Luna 能力不足，Astra 成本过高
```

普通用户一眼就应该能看明白：

**为什么选择了这个模型。**

---

## 十二、凭证管理

Router 不直接在配置文件保存明文 API Key。

配置只保存：

```text
credential_ref: openai-main
```

真正凭证根据运行环境放在：

```text
macOS Keychain
环境变量
Docker Secret
服务器 Secret Manager
```

这使本地版和服务器版可以共用同一个 Router Core。

---

## 十三、本地与服务器

支持两种运行模式：

```text
Local
Server
```

Local：

```text
Codex
   ↓
localhost
   ↓
JEV Router
```

Server：

```text
Codex
   ↓
HTTPS
   ↓
JEV Router Server
```

核心代码完全相同。

只改变部署方式。

---

## 十四、日志与隐私

提供三种模式。

### 模式 1：最小记录【默认】

保存：

```text
任务类型
难度
选择模型
成本
成功/失败
是否发生 fallback
```

不保存完整任务内容。

### 模式 2：分析模式

增加：

```text
脱敏后的任务摘要
是否返工
为什么返工
```

### 模式 3：开发调试

允许记录完整请求与响应。

必须由用户主动开启。

---

## 十五、Router 学习机制

系统持续记录：

```text
任务类型
难度
选用模型
成本
成功率
返工率
失败原因
```

例如以后可能得到：

```text
代码修复

Luna
成功率：73%

Sol
成功率：94%

Astra
成功率：96%
```

Router 可以据此发现：

> 很多原本交给 Astra 的任务，其实 Sol 已经足够。

于是生成建议：

```text
建议：

Coding
难度 6–7

主模型从 Astra 调整为 Sol

预计：
成本下降 38%
成功率变化约 -1.4%
```

但：

**Router 永远不能自己修改生产路由策略。**

必须：

```text
生成建议
↓
用户批准
↓
生效
```

---

## 十六、总体技术架构

正式产品建议分成六部分：

```text
JEV Model Router
│
├── Router Core
│
├── JEV Classifier
│
├── Policy Engine
│
├── Provider Layer
│
├── UI
│
└── Platform Adapters
      │
      ├── Codex Adapter
      ├── DeepSeek Harness Adapter
      ├── Claude Code Adapter
      ├── PASEO Adapter
      └── 其他 Agent Adapter
```

真正需要长期维护的核心只有：

**Router Core。**

---

## 十七、Codex 接入方式

这里不建议只做一个 SKILL。

Codex 当前官方插件架构可以包含：

```text
Plugin
├── Skill
├── MCP Server
├── Hooks
└── UI
```

因此 Codex 版本建议打包成：

**JEV Router Codex Plugin**

其中：

```text
Skill
→ 告诉 Codex 什么时候使用 Router

MCP
→ 与 JEV Router Core 通信

UI
→ 模型选择、路由解释、设置

Hooks
→ 在适合的生命周期节点触发 Router
```

当前公开的 Codex Hooks 包括 `UserPromptSubmit`、`SessionStart`、`SubagentStart`、`PreToolUse` 等，但目前没有看到一个官方公开的“任意时刻直接修改当前主模型”的 ModelBeforeRequest Hook。

因此第一版不要建立在“Codex 每个回合都能偷偷更换自身主模型”这个假设上。

Codex Adapter 应采用：

**任务入口路由 + Router 工具委派 + 必要时新建/派发模型任务**

这种更稳定的方式。

---

## 十八、DeepSeek Harness 接入方式

DeepSeek Harness 更适合深度接入。

它当前已经有统一的：

```text
@deepseek-ai/dsh-llm
```

以及 provider adapter 注册机制，模型请求通过统一的 provider/model 路由。

同时已有：

```text
UI Model Selection
Default Model Selection
Provider Adapter
Credential 管理
```

因此可以开发：

**dsh-jev-router**

让 DeepSeek Harness 原来的模型选择区域出现：

```text
自动路由
GPT 6 Luna
GPT 6 Sol
GPT 6 Astra
……
```

选择：

**自动路由**

以后，请求进入 JEV Router。

所以 DeepSeek Harness 很可能会成为最适合做第一版完整验证的平台。

---

## 十九、Skill、插件、Router 三者关系

最终不要再纠结“到底是 Skill 还是插件”。

正确关系是：

```text
              JEV Router Core
                    ↑
          ┌─────────┼──────────┐
          │         │          │
       Codex       DSH      Claude Code
          │         │          │
       Plugin     Plugin      Adapter
          │
       Skill
```

因此：

**Router 是产品。**

**Plugin 是安装方式。**

**Skill 是插件内部的一部分。**

---

## 二十、第一版开发范围

v0.1 不要一下子支持十个平台。

第一版只做：

```text
1. JEV Router Core
2. JEV 判断
3. Luna / Sol / Astra 三模型测试
4. 自动路由
5. 手动选择模型
6. 成本策略
7. 备用模型
8. 基础日志
9. UI
10. DeepSeek Harness Adapter
```

先把这条链跑通：

```text
用户任务
↓
自动模式
↓
JEV
↓
Router
↓
模型
↓
任务完成
↓
记录结果
```

稳定以后再做：

```text
Codex
Claude Code
PASEO
其他 Agent
```

---

## 二十一、产品名称

工作名称：

**JEV Model Router**

中文：

**JEV 智能模型路由器**

如果将来希望脱离 JEV，成为真正独立产品，可以把 JEV 仅作为一个 classifier provider：

```text
Classifier:
● JEV
○ Local Rules
○ Other Classifier
```

这样未来即使 JEV 不存在，整个 Router 仍然可以继续工作。

---

## 二十二、v0.1 最终原则

这个项目最重要的不是“让 JEV 判断哪个模型最好”。

而是建立一个稳定的模型调度层：

> **任务理解交给 JEV，模型决策交给 Router，最终控制权留给用户。**

默认自动化。

用户需要时可以随时接管。

模型、JEV、Agent 平台都应该可以替换。

这才是真正意义上的“通用模型路由器”。
