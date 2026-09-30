# DSH LlmAdapter 接口调研报告（可直接照写的实现级报告）

调查对象：`/Applications/DeepSeek Harness.app/Contents/Resources/app.asar` 内的
`@deepseek-ai/dsh-llm@0.2.0-rc.2`、`dsh-llm-pi-ai`、`dsh-llm-deepseek`、`dsh-agent-loop`、
`dsh-agent`、`dsh-api-session-controller`、`@deepseek-ai/cordis`。
所有结论均给出 asar 内路径 + 原样代码片段。凡无确凿证据的，明确写「未找到证据」。

---

## 0. 前置更正：`/tmp/asar_tool.py` 的 BASE 偏移有 bug（会污染取证）

现有的 `asar_tool.py` 用 `BASE = 16 + json_size` 定位数据区，**比真实数据区起点少 2 字节**。
后果：每个文件被读成「前一文件的最后 2 字节 + 自身的前 `size-2` 字节」，即首行被污染、
末 2 字节丢失。正确算法（asar pickle 布局：`[u32=4][u32 headerBuf 长度 H][4 字节字符串长度]
[JSON][padding]`，数据区起点 = `8 + H`）：

```python
v = struct.unpack('<4I', f.read(16))   # (4, H, jsonPayload, jsonSize)
BASE = 8 + v[1]                        # 真实数据区起点
raw  = f.read(v[2] - 4)                # JSON 字符串载荷（尾部有 NUL padding，需 raw_decode）
```

校验：`dsh-llm/README.md` 正确首行是 `---`，旧工具读成 `5`；`lib/types/error.js` 正确首行是
`/**`，旧工具读成 `ap/**`。**本报告所有行号/片段均来自修正后的工具**（差异仅 1 行左右，
内容一致，但引用首行时会被污染）。

---

## 1. LlmAdapter 契约的完整形状

### 结论

`registerAdapter(providers, adapter)` 是**全有或全无**的注册：注册期**同步**调用
`adapter.providerInfo(provider)` 与 `adapter.providerRetryPolicy(provider)`；`imageRequestPricing`
由 token meter 按需**同步**拉取；`listModels`/`resolveModel`/`prepareCall`/`stream` 在各自 API
被调用时异步调用。除 `stream` 外**全部有默认实现**（TS 里只有 `stream` 是 `abstract`）。

### 证据

**接口声明**（`dsh/node_modules/@deepseek-ai/dsh-llm/lib/typert.host.js:338`，官方 `.d.ts` 的
线上投影，与 `lib/index.js` 的 `LlmAdapter` 类逐字对应）：

```ts
export abstract class LlmAdapter {
    providerInfo(provider: string): LlmProviderInfo;
    providerRetryPolicy(_provider: string): ResolvedRetryPolicy | undefined;
    imageRequestPricing(_provider: string, _model: string): LlmImageRequestPricing | undefined;
    listModels(_provider: string): Promise<readonly LlmModelInfo[]>;
    resolveModel(provider: string, model: string, _signal?: AbortSignal): Promise<LlmResolvedModelInfo>;
    prepareCall(provider: string, model: string, signal?: AbortSignal): Promise<PreparedAdapterCall>;
    abstract stream(options: GenerateOptions): AsyncIterable<StreamChunk>;
}
```

**默认实现**（`dsh/node_modules/@deepseek-ai/dsh-llm/lib/index.js:1670-1741`，节选）：

```js
var LlmAdapter = class {
	providerInfo(provider) {
		return {
			id: provider,
			name: provider
		};
	}
	providerRetryPolicy(_provider) {}
	imageRequestPricing(_provider, _model) {}
	listModels(_provider) {
		return Promise.resolve([]);
	}
	resolveModel(provider, model, _signal) {
		return Promise.resolve({
			provider,
			id: model,
			name: model
		});
	}
	async prepareCall(provider, model, signal) {
		return {
			model: await this.resolveModel(provider, model, signal),
			stream: (options) => this.stream(options)
		};
	}
};
```

**注册与校验**（`.../lib/index.js:1833-1879`）：

```js
		registerAdapter(providers, adapter) {
			const owned = /* @__PURE__ */ new Set();
			let released = false;
			const dispose = this.ctx.effect(function* () {
				if (providers.length === 0) throw new LlmError("an adapter must register at least one provider", "INVALID_ADAPTER");
				this.commitRoutes(owned, this.prepareRoutes(providers, adapter, owned));
				yield () => {
					released = true;
					for (const provider of owned) this.adapters.delete(provider);
					owned.clear();
					this.emitAdaptersUpdated();
				};
			}.bind(this), "llm.registerAdapter()");
			const handle = (() => void dispose());
			handle.replace = (next) => {
				if (released) throw new LlmError("a disposed adapter registration cannot replace its routes", "REGISTRATION_DISPOSED");
				this.commitRoutes(owned, this.prepareRoutes(next, adapter, owned));
			};
			return handle;
		}
		prepareRoutes(providers, adapter, owned) {
			const unique = /* @__PURE__ */ new Set();
			const registrations = [];
			for (const provider of providers) {
				if (provider.length === 0) throw new LlmError("adapter provider names must be non-empty", "INVALID_ADAPTER");
				if (unique.has(provider) || this.adapters.has(provider) && !owned.has(provider)) throw new LlmError(`an adapter for provider "${provider}" is already registered`, "DUPLICATE_ADAPTER");
				const info = adapter.providerInfo(provider);
				if (typeof info.id !== "string" || info.id !== provider || typeof info.name !== "string" || info.name.length === 0) throw new LlmError(`adapter metadata for provider "${provider}" must preserve its id and have a non-empty name`, "INVALID_ADAPTER");
				unique.add(provider);
				const retryPolicy = adapter.providerRetryPolicy(provider) ?? resolveRetryPolicy(void 0, `llm: provider "${provider}" retryPolicy`);
				registrations.push({ adapter, provider: { id: info.id, name: info.name }, retryPolicy });
			}
			return registrations;
		}
```

**各方法调用时机与参数/返回值**：

| 方法 | 何时被调 | 入参 | 返回 | 是否必须 |
|---|---|---|---|---|
| `providerInfo(provider)` | `registerAdapter` 内**同步**（注册/`replace` 时）；每次 `replace` 重调 | provider 字符串 | `{id: string, name: string}`，**`id` 必须 === provider**，`name` 非空 | 有默认 `{id: provider, name: provider}` |
| `providerRetryPolicy(provider)` | 同上，注册时**一次性快照**进注册记录 | provider | `ResolvedRetryPolicy \| undefined`；`undefined` → `resolveRetryPolicy(undefined, …)` = normal/5 次/`[EMPTY_RESPONSE, RATE_LIMIT, SERVER, TIMEOUT, TRANSPORT]` | 有默认（返回 undefined） |
| `imageRequestPricing(provider, model)` | 同步、无 I/O，token meter 每次计量时经 `ctx.llm.imageRequestPricing()` 拉取 | provider, model | `{ priceImages(images: ImageBlock[]): {visualTokens:number,text:string}[] } \| undefined` | 有默认 |
| `listModels(provider)` | `ctx.llm.listModels(provider)`（GUI 目录、`modelAvailable()`） | provider | `Promise<LlmModelInfo[]>` | 有默认（`[]`，**GUI 将看不到任何模型**） |
| `resolveModel(provider, model, signal)` | `ctx.llm.resolveModelInfo()`、`resolveCallConfig()`，以及默认 `prepareCall()` 内部 | provider, model, AbortSignal? | `Promise<LlmResolvedModelInfo>` | 有默认 |
| `prepareCall(provider, model, signal)` | `ctx.llm.prepareCall(config, signal)`（agent loop 每步一次）；无 prepared 的 `adapterStream` 兜底路径 | provider, model, AbortSignal? | `Promise<{model, stream(options)}>` | 有默认 |
| `stream(options)` | `adapterStream` 经 waterfall 后 `dispatch(...)` 调用（`dispatch` 来自 `prepareCall().stream`） | `GenerateOptions`（已投影：file→文本、图片占位、tool 更新） | `AsyncIterable<StreamChunk>` | **唯一抽象方法，必须实现** |

关键运行时片段（`.../lib/index.js:2283-2324` 节选）：

```js
		async *adapterStream(options, prepared) {
			let iterator;
			try {
				const registration = prepared?.registration ?? this.registration(options.provider);
				const adapter = registration.adapter;
				...
				if (prepared === void 0) {
					const adapterCall = await adapter.prepareCall(options.provider, options.model, options.signal);
					modelInfo = this.normalizeModelInfo(registration, options.model, adapterCall.model);
					resolvedConfig = this.resolveCallWithInfo(options, modelInfo).config;
					dispatch = (options) => adapterCall.stream(options);
				} else { ... }
				...
				iterator = dispatch(this.forAdapter(projectedOptions, adapter))[Symbol.asyncIterator]();
			} catch (error) {
				yield adapterFailureChunk(error, options.signal);
				return;
			}
```

### 要点（坑）

- **`providerInfo().id` 必须严格等于 provider 字符串**，否则注册就抛 `INVALID_ADAPTER`（不是警告）。
- `providerRetryPolicy` 在**注册时**取值并冻结；事后改它无效，必须用 `handle.replace(routes)`。
- `stream()` 必须返回**异步**可迭代（`[Symbol.asyncIterator]`）。返回同步数组会在 `adapterStream`
  的 try 里抛 `TypeError`，被吞成一个 terminal `finish{kind:'error', code:'UNKNOWN'}` chunk —— 不会
  让插件加载失败，只会让每次请求静默失败。
- 注册被 fiber 管理：插件卸载即注销路由；不要自己 `delete`。
- `replace(providers)` 会重跑 `providerInfo`/`providerRetryPolicy`；被别的适配器占用的路由会让整批失败（原子）。

---

## 2. `listModels(provider)` 的返回结构（GUI `/model` 选择器）

### 结论

GUI 走的是 `ctx.llm.modelCatalog()` → `buildModelCatalog()`：对每个 provider 调 `listModels`，
再对**每个模型**调 `resolveModelInfo` 取推理档位。因此 `listModels` 的**必需字段只有
`provider` / `id` / `name`**；`description`、`inputModalities` 可选；**`contextWindow` / `maxTokens`
/ `reasoningEfforts` / `systemPromptUpdate` 不是 `listModels` 的字段**（不属于 `LlmModelInfo`），
写在那里会被运行时**静默丢弃**（不是报错），必须放到 `resolveModel` 的返回值里。

### 证据

**运行时归一化**（`dsh/node_modules/@deepseek-ai/dsh-llm/lib/index.js:2073-2089`）：只保留
5 个字段，其余全部丢弃：

```js
		async listModels(provider) {
			const models = await this.registration(provider).adapter.listModels(provider);
			const seen = /* @__PURE__ */ new Set();
			return models.map((model) => {
				if (typeof model.provider !== "string" || model.provider !== provider || typeof model.id !== "string" || model.id.length === 0 || typeof model.name !== "string" || model.name.length === 0 || model.description !== void 0 && typeof model.description !== "string" || seen.has(model.id)) throw new LlmError(`adapter returned invalid or duplicate model metadata for provider "${provider}"`, "INVALID_CATALOG");
				seen.add(model.id);
				const inputModalities = this.detachedModalities(model.inputModalities);
				return {
					provider: model.provider,
					id: model.id,
					name: model.name,
					...model.description === void 0 ? {} : { description: model.description },
					...inputModalities === void 0 ? {} : { inputModalities }
				};
			});
		}
```

**类型**（`.../lib/typert.host.js:378`）：

```ts
export interface LlmModelInfo {
    provider: string;
    id: string;
    name: string;
    description?: string;
    inputModalities?: readonly ModelModality[];   // 'text' | 'image'
}
```

**GUI 目录的真实消费者**（`dsh/node_modules/@deepseek-ai/dsh-api-session-controller/lib/index.js:500-529`）：

```js
async function buildModelCatalog(ctx, defaultSelection = ctx.agentDefaultModel.currentSelection()) {
	const providers = ctx.llm.listProviders();
	const catalog = await Promise.all(providers.map(async (provider) => {
		try {
			const models = await ctx.llm.listModels(provider.id);
			const entries = await Promise.all(models.map(async (model) => {
				const resolved = await ctx.llm.resolveModelInfo(provider.id, model.id);
				const reasoning = resolved.reasoning === void 0 ? void 0 : {
					efforts: resolved.reasoning.efforts.map((effort) => ({
						id: effort.id,
						name: effort.name,
						...effort.description === void 0 ? {} : { description: effort.description }
					})),
					...resolved.reasoning.defaultEffort === void 0 ? {} : { defaultEffort: resolved.reasoning.defaultEffort }
				};
				return {
					id: model.id,
					name: model.name,
					...model.description === void 0 ? {} : { description: model.description },
					...reasoning === void 0 ? {} : { reasoning }
				};
			}));
			return { kind: "group", group: { id: provider.id, name: provider.name, models: entries } };
		} catch (error) {
			return { kind: "failure", failure: { id: provider.id, name: provider.name, message: ... } };
		}
	}));
	const groups = catalog.flatMap((item) => item.kind === "group" ? [item.group] : []).filter((group) => group.models.length > 0);
```

注意：**只要 `listModels` 或某个 `resolveModelInfo` 抛错，整个 provider 组变成 `failure`**
（该组的模型一个都不显示）。所以 `resolveModel` 必须对目录里每个 id 都成功。

**非法字段的报错**（全部集中在上面这段）：

- `provider` 不是字符串 / 不等于请求的 provider → `LlmError('adapter returned invalid or duplicate model metadata for provider "…"', 'INVALID_CATALOG')`
- `id` 非字符串或空 → 同上 `INVALID_CATALOG`
- `name` 非字符串或空 → 同上 `INVALID_CATALOG`
- `description` 存在但非字符串 → 同上 `INVALID_CATALOG`
- `id` 重复 → 同上 `INVALID_CATALOG`
- `inputModalities` **不做值校验**（`detachedModalities` 只做 `[...modalities]` 拷贝）；非法模态串不会报错，但会影响下游判断（见 §6 要点）。

**最小合法返回示例**：

```js
async listModels(provider) {
  return [
    {
      provider,                       // 必须 === 注册时的 provider 字符串
      id: 'auto',                     // 非空
      name: '自动选择',                // 非空
      description: '由 JEV 判断任务类型后选择真实模型',   // 可选
      inputModalities: ['text', 'image'],               // 可选；'text'|'image'
    },
  ]
}
```

### 要点（坑）

- `listModels` 不做网络/凭证检查，可以纯静态返回（pi-ai 就是 `Promise.resolve().then(...)`）。
- 声明 `inputModalities: ['text']`（或不含 `'image'`）会让运行时在 `adapterStream` 里把历史图片
  **替换成占位文本**（`projectImagesForTextModel`），合成适配器就再也拿不到图片。要么声明
  `['text','image']`，要么**完全省略** `inputModalities`（省略时不做图片投影）。
- `contextWindow` / `maxTokens` / `reasoning` 必须写在 `resolveModel`，见 §3。
- 别写 `reasoningEfforts`（pi-ai 的**配置**字段名）到 `listModels`——运行时没有这个字段，会被丢弃。

### 另一个易混 API：`discoverModels`

`registerModelDiscovery(settingsNs, discover)` / `ctx.llm.discoverModels(...)` 用的是**另一套**
字段（`LlmDiscoveredModel = {id, name?, contextWindow?, maxTokens?, inputModalities?}`，
`.../lib/index.js:1996-2015`）。它与 `listModels` 无关；只有你想在 Models 设置页做「探测端点模型」
才需要它。

---

## 3. `resolveModel` 与 `prepareCall` 的返回结构

### 结论

`resolveModel(provider, model, signal) → Promise<LlmResolvedModelInfo>`；
`prepareCall(provider, model, signal) → Promise<{ model: LlmResolvedModelInfo, stream(options: GenerateOptions): AsyncIterable<StreamChunk> }>`。
两者的 `model` 都经过**同一套** `normalizeModelInfo` 校验。

### 证据

**类型**（`.../lib/typert.host.js:394, 438, 442`）：

```ts
export interface LlmResolvedModelInfo extends LlmModelInfo {
    context?: LlmModelContext;                 // { contextWindow: number }
    defaultMaxTokens?: number;
    reasoning?: LlmModelReasoningInfo;         // { efforts: LlmReasoningEffortInfo[]; defaultEffort? }
    systemPromptUpdate?: SystemPromptUpdate;   // 'in-history'
    toolUpdate?: ToolUpdate;                   // 'in-history' | 'addition-only'
}
export interface LlmModelReasoningInfo {
    efforts: readonly LlmReasoningEffortInfo[];
    defaultEffort?: ReasoningEffortId;
}
export interface LlmReasoningEffortInfo { id: ReasoningEffortId; name: string; description?: string; }
export interface PreparedAdapterCall {
    readonly model: LlmResolvedModelInfo;
    stream(options: GenerateOptions): AsyncIterable<StreamChunk>;
}
```

**校验器**（`.../lib/index.js:2106-2151`，全文见 §2 同源文件）：

```js
		normalizeModelInfo(registration, model, resolved) {
			const provider = registration.provider.id;
			if (typeof resolved.provider !== "string" || resolved.provider !== provider || typeof resolved.id !== "string" || resolved.id !== model || typeof resolved.name !== "string" || resolved.name.length === 0 || resolved.description !== void 0 && typeof resolved.description !== "string") throw new LlmError(`adapter returned invalid exact model metadata for provider "${provider}" model "${model}"`, "INVALID_MODEL_INFO");
			const context = resolved.context;
			if (context !== void 0 && (!Number.isInteger(context.contextWindow) || context.contextWindow <= 0)) throw new LlmError(..., "INVALID_MODEL_CONTEXT");
			...
			if (systemPromptUpdate !== void 0 && systemPromptUpdate !== "in-history") throw new LlmError(..., "INVALID_MODEL_INFO");
			if (toolUpdate !== void 0 && toolUpdate !== "in-history" && toolUpdate !== "addition-only") throw new LlmError(..., "INVALID_MODEL_INFO");
			if (defaultMaxTokens !== void 0 && (!Number.isSafeInteger(defaultMaxTokens) || defaultMaxTokens <= 0)) throw new LlmError(..., "INVALID_MODEL_MAX_TOKENS");
			...
			const reasoning = resolved.reasoning;
			if (reasoning === void 0) return info;
			if (reasoning.efforts.length === 0) throw new LlmError(..., "INVALID_MODEL_REASONING");
			const seen = /* @__PURE__ */ new Set();
			const efforts = reasoning.efforts.map((effort) => {
				if (typeof effort.id !== "string" || effort.id.length === 0 || typeof effort.name !== "string" || effort.name.length === 0 || effort.description !== void 0 && typeof effort.description !== "string" || seen.has(effort.id)) throw new LlmError(..., "INVALID_MODEL_REASONING");
				seen.add(effort.id);
				return { id: effort.id, name: effort.name, ...effort.description === void 0 ? {} : { description: effort.description } };
			});
			if (reasoning.defaultEffort !== void 0 && !seen.has(reasoning.defaultEffort)) throw new LlmError(..., "INVALID_MODEL_REASONING");
```

**`prepareCall` 的运行时包装**（`.../lib/index.js:2204-2236`）：

```js
		async prepareCall(config, signal) {
			const registration = this.registration(config.provider);
			const adapterCall = await registration.adapter.prepareCall(config.provider, config.model, signal);
			const modelInfo = this.normalizeModelInfo(registration, config.model, adapterCall.model);
			const resolved = this.resolveCallWithInfo(config, modelInfo);
			const resolvedConfig = deepFreeze(structuredClone(resolved.config));
			...
			let dispatched = false;
			return Object.freeze({
				config: resolvedConfig,
				retryPolicy: registration.retryPolicy,
				adapterDefaults,
				...context === void 0 ? {} : { context },
				...modelInfo.inputModalities === void 0 ? {} : { inputModalities: Object.freeze([...modelInfo.inputModalities]) },
				...modelInfo.systemPromptUpdate === void 0 ? {} : { systemPromptUpdate: modelInfo.systemPromptUpdate },
				...modelInfo.toolUpdate === void 0 ? {} : { toolUpdate: modelInfo.toolUpdate },
				stream: (options) => {
					if (dispatched) throw new LlmError("a prepared LLM call can only be dispatched once", "INVALID_PREPARED_CALL");
					if (!callConfigEquals(options, resolvedConfig)) throw new LlmError("prepared LLM call config changed before adapter dispatch", "INVALID_PREPARED_CALL");
					dispatched = true;
					return this.streamWithRegistration(options, { registration, config: resolvedConfig, modelInfo, dispatch: (options) => adapterCall.stream(options) });
				}
			});
		}
```

参考实现 `pi-ai`（`dsh/node_modules/@deepseek-ai/dsh-llm-pi-ai/lib/index.js:1799-1841`）：

```js
	listModels(provider) {
		return Promise.resolve().then(() => {
			const snapshot = this.current();
			this.profileOf(snapshot, provider);
			return snapshot.models.getModels(provider).map((model) => ({
				provider, id: model.id, name: model.name, inputModalities: [...model.input]
			}));
		});
	}
	resolveModel(provider, model, _signal) {
		return Promise.resolve().then(() => {
			const snapshot = this.current();
			return this.modelInfo(snapshot, provider, model);
		});
	}
	modelInfo(snapshot, provider, model) {
		const profile = this.profileOf(snapshot, provider);
		const resolvedModel = this.modelOf(snapshot, provider, model);
		const defaultLevel = describableReasoningLevel(resolvedModel, profile.reasoning);
		const configuredMaxTokens = profile.configuredMaxTokens.get(model);
		return {
			provider, id: model, name: resolvedModel.name,
			inputModalities: [...resolvedModel.input],
			context: { contextWindow: resolvedModel.contextWindow },
			...configuredMaxTokens === void 0 ? {} : { defaultMaxTokens: configuredMaxTokens },
			...reasoningInfo(resolvedModel, defaultLevel)
		};
	}
	prepareCall(provider, model, _signal) {
		const snapshot = this.current();
		return Promise.resolve({
			model: this.modelInfo(snapshot, provider, model),
			stream: (options) => this.streamWithSnapshot(options, snapshot)
		});
	}
```

`reasoningInfo`（`.../pi-ai/lib/index.js:1727`）：

```js
function reasoningInfo(model, defaultLevel) {
	if (!model.reasoning) return {};
	return { reasoning: {
		efforts: getSupportedThinkingLevels(model).map((level) => ({
			id: ReasoningEffortId(level),
			name: `${level.charAt(0).toUpperCase()}${level.slice(1)}`
		})),
		...defaultLevel === void 0 ? {} : { defaultEffort: ReasoningEffortId(defaultLevel) }
	} };
}
```

### 要点（坑）

- `resolved.provider` 必须 === 注册 provider，`resolved.id` 必须 **=== 请求的 model**，否则
  `INVALID_MODEL_INFO`。`prepareCall` 里不要"顺手"返回别的模型 id。
- **`reasoning` 只要给就必须给 `efforts` 数组**：`reasoning.efforts.length` 没有空值保护，
  `reasoning: {}` 会抛 `TypeError`（不是 `LlmError`），在 `adapterStream` 里会被
  `normalizeLlmFailure` 变成 `code:'UNKNOWN'` 的 error finish chunk。
- `reasoning` 一旦声明，`resolveCallWithInfo`（`lib/index.js:2170-2195`）会在**任何 provider I/O 之前**
  用请求里的 `reasoningEffort` 去比对：不匹配即 `UNSUPPORTED_REASONING_EFFORT`。所以合成模型声明的
  档位集合 = 你能接受的档位集合，多一个少一个都会在 `prepareCall`/`selectModel` 阶段炸。
- `defaultMaxTokens` 会被**物化进请求**（`resolveCallWithInfo` 的 `defaulted`），并写进 session 的
  `request/header`（`adapterDefaults.maxTokens: true`），随后**原样转发给内层真实模型**。合成适配器
  若不想替真实模型决定输出上限，就不要声明 `defaultMaxTokens`。
- `context.contextWindow` 只用于 `request/context` 与计量；合成模型应当声明一个**保守**值（或省略）。
- `prepareCall` 返回的 `stream` 是**一次性**的：第二次调用抛 `INVALID_PREPARED_CALL`。
- 合法最小返回：

```js
{ provider, id: model, name: '自动选择', inputModalities: ['text','image'], context: { contextWindow: 262144 } }
```

---

## 4. 适配器内部能否嵌套调用 `ctx.llm.stream()`？

### 结论

**能，但必须自己防递归**：内层 `ctx.llm.stream()` **会再次经过 `llm/stream` 瀑布**（不存在绕过
public API 的路径）。`forAdapter()` 在**每次 `adapterStream()` 分派时**、以**该次分派的目标
adapter 实例**为判据剥离 replayState，所以在"合成适配器 → 内层真实适配器"这条链上，
replayState 会在**内层**被剥掉；而历史里 provider 记的是**外层合成 provider**，因此**无论用
哪种做法（合成适配器 or 瀑布接管），只要不改写历史消息的 `source`，replayState 都会丢**。
两者的差异不在 replay，而在**投影层数与 route 记录**（见下）。

### 证据

**`adapterStream` 里 `prepared` / `dispatch` 的用法**（`.../lib/index.js:2284-2324`，全文见 §1）：

```js
		async *adapterStream(options, prepared) {
			let iterator;
			try {
				const registration = prepared?.registration ?? this.registration(options.provider);
				const adapter = registration.adapter;
				let modelInfo; let resolvedConfig; let dispatch;
				if (prepared === void 0) {
					const adapterCall = await adapter.prepareCall(options.provider, options.model, options.signal);
					modelInfo = this.normalizeModelInfo(registration, options.model, adapterCall.model);
					resolvedConfig = this.resolveCallWithInfo(options, modelInfo).config;
					dispatch = (options) => adapterCall.stream(options);
				} else {
					modelInfo = prepared.modelInfo;
					resolvedConfig = prepared.config;
					dispatch = prepared.dispatch;
				}
				if (prepared !== void 0 && !callConfigEquals(options, resolvedConfig)) throw new LlmError("prepared LLM call config changed before adapter dispatch", "INVALID_PREPARED_CALL");
				const resolvedOptions = callConfigEquals(options, resolvedConfig) ? options : Object.isFrozen(options) ? deepFreeze({ ...options, ...resolvedConfig }) : { ...options, ...resolvedConfig };
				let projectedMessages = resolvedOptions.messages;
				if (projectedMessages.some((message) => contentHasFile(message.content))) projectedMessages = projectFilesToText(projectedMessages, (ref) => this.fileReadPath(ref));
				if (modelInfo.inputModalities !== void 0 && !modelInfo.inputModalities.includes("image") && projectedMessages.some((message) => contentHasImage(message.content))) projectedMessages = projectImagesForTextModel(projectedMessages);
				const projectedTools = projectToolUpdates(projectedMessages, resolvedOptions.tools, modelInfo.toolUpdate, resolvedOptions.toolHistory);
				projectedMessages = projectedTools.messages;
				...
				iterator = dispatch(this.forAdapter(projectedOptions, adapter))[Symbol.asyncIterator]();
```

要点：**`prepared` 一旦存在，adapter 的选择就固定在 `prepared.registration`**（不再看
`options.provider`），并且 `options` 的 call-config 字段必须与 `resolvedConfig` 完全一致
（`callConfigEquals`），否则 `INVALID_PREPARED_CALL`。

**`forAdapter` 剥离逻辑**（`.../lib/index.js:2242-2264`）：

```js
		forAdapter(options, adapter) {
			const messages = options.messages.map((message) => {
				if (message.role !== "assistant") return message;
				const source = message.source;
				if (source.replayState === void 0) return message;
				if (this.adapters.get(source.provider)?.adapter === adapter) return message;
				return freezeMessage({
					...message,
					source: { kind: "model", provider: source.provider, model: source.model }
				});
			});
			...
		}
```

判据只有一条：`this.adapters.get(source.provider)?.adapter === adapter`（同一 adapter **实例**，
不看 model）。

**内层调用经过瀑布**（`.../lib/index.js:2368-2373`）：

```js
		stream(options) {
			return this.streamWithRegistration(options);
		}
		streamWithRegistration(options, prepared) {
			return this.ctx.waterfall(this, "llm/stream", options, () => this.adapterStream(options, prepared));
		}
```

**README 的官方不变式**（`.../dsh-llm/README.md:115`）：

```
- **Replay state travels only within one adapter** — assistant replay state rides along only when the same adapter instance owns the historical and target routes; otherwise it is dropped before dispatch.
```

**历史里的 provider 是谁**：loop 在被路由改写**之前**就把选中的 provider 写进了日志与消息源
（`dsh/node_modules/@deepseek-ai/dsh-agent-loop/lib/index.js:1137-1144`）：

```js
				const message = createAssistantMessage({
					content: live.blocks(),
					source: {
						provider: request.provider,
						model: request.model,
						...live.replayState !== void 0 ? { replayState: live.replayState } : {}
					}
				});
```

而 `request.provider` = 选中项（= `jev-router`），不是真实 provider。

**逐层推演**（合成 provider `jev-router`，真实 provider `opencode-go`）：

1. `adapterStream(request, prepared)`：`adapter` = 合成适配器，
   `forAdapter(messages, 合成适配器)` → 历史里 `source.provider==='jev-router'` → **保留** replayState
   （因为合成适配器正好拥有 `jev-router`）。
2. 合成适配器的 `stream(options)` 里调 `ctx.llm.stream({...options, provider:'opencode-go', model:'…'})`：
   → 新的（**非 frozen、未被 `markAgentLoopRequest` 标记**）options 进入瀑布 → `adapterStream(forwarded, undefined)`
   → `adapter` = pi-ai 适配器 → `forAdapter(messages, pi-ai 适配器)` 判据
   `adapters.get('jev-router').adapter === pi-ai 适配器` → **false** → **剥离 replayState**。
3. 于是真实适配器**永远拿不到**历史 replay state。

**换成瀑布监听器接管**：监听器拿到的是**未被改动**的 frozen options，它自己调
`ctx.llm.stream({...options, provider:'opencode-go', …})` → 同样进入 `adapterStream(forwarded, undefined)`
→ 同样 `forAdapter(..., pi-ai 适配器)` → `source.provider==='jev-router'` → **同样剥离**。

→ **两种做法的 replay 后果完全相同**（对"由本路由产生的历史"而言）。差别只在于：如果用户**先直接
用过 `opencode-go`**（历史 `source.provider==='opencode-go'`）再切到 auto，则：
瀑布方案内层 `forAdapter` 的判据为 `adapters.get('opencode-go').adapter === pi-ai 适配器` → **true → 保留**；
合成适配器方案在**外层**就先剥了（判据是合成适配器）→ **丢失且不可恢复**。

**想真正保住 replayState**：必须由转发方把历史消息副本的 `source.provider/model` 改写成**目标路由**
（`{...message, source:{...message.source, provider:'opencode-go', model:'…'}}`），因为真实适配器自己的
replay 校验器会核对这两个字段（pi-ai：`.../pi-ai/lib/index.js:183-187`）：

```js
function replayedAssistant(message, source, rawState) {
	const state = readReplayState(rawState);
	if (state.response.provider !== source.provider) return invalidReplay("provider does not match assistant source");
	if (state.response.model !== source.model) return invalidReplay("model does not match assistant source");
```

pi-ai 遇到 `INVALID_REPLAY_STATE` 会**降级**为 provider-neutral 消息（不失败，`onReplayDegrade` 打日志）；
deepseek 适配器则是抛 `LlmError('INVALID_REPLAY_STATE')`（`.../dsh-llm-deepseek/lib/index.js:1504-1508`）。
所以「改写 source」是可行的、但**只在该消息确实是目标路由产生时**才安全；不写就接受 replay 丢失。

### 要点（坑）

- **递归**：内层调用会重入瀑布。监听器/转发函数必须**先判 `options.provider`**
  （`if (options.provider !== 'jev-router') return next()`），否则无限递归。
- 内层 `ctx.llm.stream()` **不要传 `prepared`**（你也拿不到），它会重新 `prepareCall` + 重新归一化；
  这意味着**每个 loop step 会跑两次 `prepareCall`**（一次合成、一次真实）。
- 合成适配器路径会**多一层投影**：外层用合成模型的 `inputModalities` 决定图片是否被替换成占位、
  用合成模型的 `toolUpdate` 决定工具更新投影。**瀑布接管路径没有这一层**（监听器拿到原始 options，
  内层只按真实模型投影一次）。这是"瀑布接管"相对"合成适配器"的真正优势。
- 合成适配器路径还会把合成模型物化的 `maxTokens` / `reasoning` 校验结果带到内层；声明要克制。
- 内层 forwarded 是**新对象**，不在 `markAgentLoopRequest` 的 WeakSet 里（`.../lib/types/call-config.js:34-45`），
  所以 `dsh-agent-loop/invariant` 的"loop 请求重建"校验**对内层调用不生效**（它只校验外层原始对象，
  且只比 `model`/messages，不比 `provider`）。

---

## 5. `llm/stream` 瀑布监听器的精确签名

### 结论

签名（官方声明）：`'llm/stream'(this: LlmRuntime, options: GenerateOptions, next: () => AsyncIterable<StreamChunk>): AsyncIterable<StreamChunk>`。
**`next` 是零参闭包，传参会被完全忽略**——监听器**不能**通过 `next(modifiedOptions)` 改写请求。
**不调 `next()`、直接返回自己的 async iterable 是被接受的官方用法**（"short-circuit"）。

### 证据

**事件声明**（`.../lib/typert.host.js:591-611`）：

```
"The description": "Waterfall around every streaming model call (retry, replay, routing)...
   call `next()` to reach the resolved adapter's stream, or yield your own chunks to short-circuit."
"signature": "'llm/stream'(this: LlmRuntime, options: GenerateOptions, next: () => AsyncIterable<StreamChunk>): AsyncIterable<StreamChunk>"
```

**`next` 的实现**（`.../lib/index.js:2372`）：

```js
		streamWithRegistration(options, prepared) {
			return this.ctx.waterfall(this, "llm/stream", options, () => this.adapterStream(options, prepared));
		}
```

**Cordis 的 waterfall 本体**（`dsh/node_modules/@deepseek-ai/cordis/lib/index.js:318-325`）：

```js
	waterfall(...args) {
		const cbs = this.dispatch("waterfall", args);
		const inner = args.pop();
		const next = () => {
			return (cbs.shift() ?? inner)(...args);
		};
		args.push(next);
		return next();
	}
```

- `dispatch` 先 `shift` 掉 `thisArg` 与事件名，`args` 只剩 `[options]`，再 `pop` 出 `inner`。
- `next` 闭包写成 `() => …`，**形参表为空**：`next(x)` 里 `x` 被丢弃，链路下游永远拿到**同一个原始
  `options`**，参数在整条瀑布上不可变换。
- `waterfall` 的返回值就是最外层监听器的返回值，`ctx.llm.stream()` 的消费者用 `for await`，
  因此返回任意 async iterable 都合法。

**现有用法佐证**（`dsh/node_modules/@deepseek-ai/dsh-llm/lib/invariant.js:63-67`，
以及 `dsh-agent-loop/lib/invariant.js:15-29`）：

```js
const install = (ctx, fail) => {
	ctx.on("llm/stream", (_options, next) => validateStream(next(), fail), { global: true, prepend: true });
```

### 要点（坑）

- **不要试图用 `next(modified)` 改写**——静默无效，你会以为改了其实没改。
- 要在瀑布层改写，只有两条路：**(a) 自己调 `ctx.llm.stream(改写后的 options)`（注意递归守卫）；
  (b) 返回自己的 async iterable。**
- 不调 `next()` 就等于**短路**：`adapterStream`（含 `prepared` 一次性语义、投影、失败归一化）**全部跳过**。
  你自己返回的流若中途抛异常，**运行时不会**帮你包成 terminal finish chunk —— 必须自己保证"恰好一个
  terminal `finish`"，否则 loop 会把它当成正常 `stop`（见 §6/§9）。
- 监听器注册：`ctx.on('llm/stream', fn)`，随 fiber 自动注销；`prepend: true` 可插到最前。

---

## 6. StreamChunk 协议

### 结论

适配器 `stream` 必须 yield **异步**流式的 `StreamChunk`，并**恰好以一个 terminal `finish` 收尾**
（error/aborted 时允许有未闭合 block，正常结束时不允许）。**把内层 `ctx.llm.stream()` 的 chunk 原样
透传是合法的**——因为运行时保证内层流也一定以恰一个 finish 结束。

### 证据

**类型定义**（`.../lib/typert.host.js:506`）：

```ts
export type StreamChunk =
  | { type: 'block-start'; index: number; blockType: ContentBlockType; }
  | { type: 'text-delta'; index: number; text: string; }
  | { type: 'reasoning-delta'; index: number; text: string; }
  | { type: 'tool-call-delta'; index: number; id: ToolCallId; name?: string; argumentsDelta: string; }
  | { type: 'block-end'; index: number; block: ContentBlock; }
  | { type: 'usage'; usage: TokenUsage; }
  | { type: 'finish'; reason: FinishReason; replayState?: ReplayEnvelope; };
```

配套类型（同文件）：

```ts
export interface FinishReasonMap {
    stop: { kind: 'stop'; };
    'tool-calls': { kind: 'tool-calls'; };
    'max-tokens': { kind: 'max-tokens'; };
    aborted: { kind: 'aborted'; failure: LlmFailure; };
    error: { kind: 'error'; failure: LlmFailure; };
}
export interface TokenUsage {
    inputTokens: number; outputTokens: number; totalTokens?: number;
    cacheReadTokens?: number; cacheWriteTokens?: number; reasoningTokens?: number;
}
export interface ReplayEnvelope { response: unknown; blocks?: readonly unknown[]; }
export interface ContentBlockMap {
    text: TextBlock; reasoning: ReasoningBlock; image: ImageBlock; file: FileBlock;
    'tool-call': ToolCallBlock; 'tool-addition': ToolAdditionBlock; 'tool-removal': ToolRemovalBlock;
}
```

**终止要求（严格校验器，非默认挂载）**（`dsh/node_modules/@deepseek-ai/dsh-llm/lib/invariant.js:20-61`）：

```js
async function* validateStream(source, fail) {
	const open = /* @__PURE__ */ new Map();
	let usageSeen = false;
	let finished = false;
	for await (const chunk of source) {
		if (finished) fail(`LLM stream emitted ${chunk.type} after terminal finish`);
		switch (chunk.type) {
			case "block-start":
				validateIndex(chunk.index, fail);
				if (open.has(chunk.index)) fail(`LLM stream repeated block-start index ${chunk.index}`);
				open.set(chunk.index, chunk.blockType); break;
			case "text-delta": validateDelta(open, chunk.index, "text", fail); break;
			case "reasoning-delta": validateDelta(open, chunk.index, "reasoning", fail); break;
			case "tool-call-delta": validateDelta(open, chunk.index, "tool-call", fail); break;
			case "block-end": { ... }
			case "usage": if (usageSeen) fail("LLM stream emitted usage more than once"); usageSeen = true; break;
			case "finish":
				if (open.size > 0 && chunk.reason.kind !== "error" && chunk.reason.kind !== "aborted") fail(`LLM stream finished with ${open.size} open block(s)`);
				finished = true; break;
		}
		yield chunk;
	}
	if (!finished) fail("LLM stream ended without a terminal finish chunk");
}
```

注意：**这个 companion 在本机 desktop profile 里没有挂载**（`dsh-llm/invariant` 注入 `invariants`
服务，desktop 的 bundles 只有 `dsh-base`、`dsh-web-app`、`dsh-experimental-*`、`dsh-plugin-jev-router`，
没有任何 bundle 插入 `invariants`；对比 `@deepseek-ai/dsh-sdk-minimal/cordis.patch.yml:106-116` 才插入）。
所以本机**不会**因为协议错误直接 fail。

**宽松端的行为**（`.../lib/index.js:1101-1104` + `dsh-agent-loop/lib/index.js:466-468`）：

```js
	/** Finish reason from the `finish` chunk; `{kind: 'stop'}` when the stream ended without one. */
	get finish() {
		return this._finish ?? { kind: "stop" };
	}
```

即：**漏掉 finish 不会报错，会被当成正常 `stop`** —— 这是最危险的静默失败模式。

**README 的协议不变式**（`.../dsh-llm/README.md:119`）：

```
- **Protocol ordering** — `usage` precedes `finish`, tool arguments stay raw JSON strings, and nothing follows the terminal `finish`.
```

（`usage` 在 `finish` 之前是**文档要求**；校验器只强制"usage 至多一次"与"finish 之后不得再有 chunk"，
**没有**强制 usage 必须在 finish 之前。— 未找到更强的代码强制。）

**运行时保证内层流必有一个 finish**（`.../lib/index.js:2329-2356` + `2376-2389`）：

```js
			let completed = false;
			try {
				while (true) {
					let item;
					try {
						const next = await iterator.next();
						item = next.done ? { done: true } : { done: false, value: next.value };
					} catch (error) {
						completed = true;
						yield adapterFailureChunk(error, options.signal);
						return;
					}
					if (item.done) { completed = true; return; }
					yield item.value;
				}
			} finally {
				if (!completed) { const close = iterator.return?.bind(iterator); if (close) await close(); }
			}
```

```js
function adapterFailureChunk(error, signal) {
	const failure = normalizeLlmFailure(error);
	return {
		type: "finish",
		reason: signal?.aborted || failure.code === "ABORTED" ? { kind: "aborted", failure } : { kind: "error", failure }
	};
}
```

### 要点（坑）

- **透明透传是合法的**：`for await (const c of ctx.llm.stream(forwarded)) yield c`。
- **不要自己再补一个 finish**，否则会出现两个 finish（宽松端取最后一个，严格端会 fail）。
- 若内层流被**提前 break**，`adapterStream` 的 `finally` 会调 `iterator.return()`；你用
  `yield*` 委托即可自动把 `return()` 透传下去（`yield*` 会转发 return/throw）。
- `block-start` 的 `blockType` 可以是 `text|reasoning|image|file|tool-call|tool-addition|tool-removal`，
  但 `BlockAssembler` 只会为 `text/reasoning/tool-call` 从未闭合的 delta 组装（其它类型必须 `block-end`）。
- 用 `inputModalities` 决定图片占位的是**运行时**，不是 adapter；adapter 收到的是已投影的 options。

---

## 7. 失败 chunk 与 `LlmError` code

### 结论

适配器**抛任何异常**都会被 `adapterFailureChunk` → `normalizeLlmFailure` 变成**唯一**一个 terminal
`finish` chunk（`kind:'aborted'` 当且仅当 `signal.aborted` 或 `code==='ABORTED'`，否则 `'error'`）。
`LlmError(message, code, options?)` 的 `code` 是稳定路由键；非 `HarnessError` 一律降级成 `'UNKNOWN'`。

### 证据

**`LlmError`**（`.../lib/index.js:1616-1641`）：

```js
var LlmError = class extends HarnessError {
	constructor(message, code, options) {
		if (typeof message !== "string" || message.length === 0) throw new Error("LlmError message must be a non-empty string");
		if (typeof code !== "string" || code.length === 0) throw new Error("LlmError code must be a non-empty string");
		if (options?.status !== void 0 && (!Number.isInteger(options.status) || options.status < 100 || options.status > 599)) throw new Error("LlmError status must be an integer from 100 through 599");
		if (options?.providerRetryAfterMs !== void 0 && (!Number.isFinite(options.providerRetryAfterMs) || options.providerRetryAfterMs <= 0)) throw new Error("LlmError providerRetryAfterMs must be a positive finite number");
		if (options?.requestId !== void 0 && (typeof options.requestId !== "string" || options.requestId.length === 0)) throw new Error("LlmError requestId must be a non-empty string");
		super(message, code, options);
		this.name = "LlmError";
		this.failure = Object.freeze({ message, code, ...status, ...providerRetryAfterMs, ...requestId, ...offloadImages });
	}
};
```

**`normalizeLlmFailure`**（`.../lib/types/adapter-failure.js:14-27, 106-108`）：

```js
export function normalizeLlmFailure(value) {
    const error = value instanceof Error ? value : new HarnessError(thrownMessage(value), 'UNKNOWN', { cause: value });
    const carried = ownFailureSnapshot(error);
    if (carried !== undefined && carried.code === ownErrorCode(error)) return carried;
    return Object.freeze({ message: errorMessage(error), code: harnessErrorCode(error) });
}
...
function harnessErrorCode(error) {
    return error instanceof HarnessError ? error.code : 'UNKNOWN';
}
```

**终端失败事件的形状**（`.../lib/typert.host.js:358`）：

```ts
export interface LlmFailure {
    readonly message: string; readonly code: string; readonly status?: number;
    readonly providerRetryAfterMs?: number; readonly requestId?: ProviderRequestId;
    readonly offloadImages?: number;
}
```

**README 的措辞**（`.../dsh-llm/README.md:74`）：

```
Every stream ends in exactly one terminal `finish` chunk: `{ kind: 'error', failure }` on failure,
`{ kind: 'aborted', failure }` on cancellation. Failures carry stable codes such as `NO_ADAPTER`,
`MISSING_CREDENTIAL`, `AUTH`, `RATE_LIMIT`, and `CONTEXT_WINDOW_EXCEEDED`; consumers route on the code,
never on message text. `QUOTA` is provider-neutral exhaustion, while `ACCOUNT_QUOTA` is reserved for a
first-party account balance... `INVALID_CREDENTIAL`...
```

**`LlmError.code` 稳定值清单**（逐一取自 shipped 代码字面量，非推测）：

运行时/服务自身抛出（`dsh-llm/lib/index.js`、`lib/types/error.js`）：
`INVALID_ADAPTER`、`DUPLICATE_ADAPTER`、`REGISTRATION_DISPOSED`、`NO_ADAPTER`、`INVALID_CATALOG`、
`INVALID_MODEL_INFO`、`INVALID_MODEL_CONTEXT`、`INVALID_MODEL_MAX_TOKENS`、`INVALID_MODEL_REASONING`、
`UNSUPPORTED_REASONING_EFFORT`、`INVALID_PREPARED_CALL`、`INVALID_DIRECTORY`、`DUPLICATE_DIRECTORY`、
`INVALID_DISCOVERY`、`DUPLICATE_DISCOVERY`、`NO_DISCOVERY`；以及常量导出的
`CONTEXT_WINDOW_EXCEEDED`、`QUOTA`、`ACCOUNT_QUOTA`、`EMPTY_RESPONSE`、`INVALID_CREDENTIAL`、
`IMAGE_OFFLOAD_REQUIRED`（`.../lib/types/error.js:22,24,26,36,44,154`）。

适配器侧实际使用：
- `dsh-llm-deepseek`：`AUTH`、`RATE_LIMIT`、`QUOTA`、`CONTEXT_WINDOW_EXCEEDED`、`INVALID_REQUEST`、
  `SERVER`、`HTTP_<status>`、`TRANSPORT`、`FILES_API`、`INVALID_RESPONSE`、`MALFORMED_RESPONSE`、
  `REQUEST_EXTENSION`、`STREAM_CLOSED`、`ABORTED`、`TIMEOUT`、`EMPTY_RESPONSE`、
  `UNSUPPORTED_CONTENT`、`UNSUPPORTED_REASONING_EFFORT`、`INVALID_REPLAY_STATE`
  （分类函数见 `.../dsh-llm-deepseek/lib/index.js:1743-1762`）。
- `dsh-llm-pi-ai`：`MISSING_CREDENTIAL`、`NO_CREDENTIAL_STORE`、`UNKNOWN_MODEL`、`INVALID_CONFIG`、
  `UNSTORABLE_PROVIDER_ID`、`UNSUPPORTED_OPTION`、`UNSUPPORTED_CONTENT`、`DISCOVERY_FAILED`、
  `DISCOVERY_UNSUPPORTED`、`LLM_STREAM_IDLE_TIMEOUT`、`STREAM_CLOSED`、`PI_AI_ERROR`、`TIMEOUT`、
  `ABORTED`、`TRANSPORT`、`SERVER`、`RATE_LIMIT`、`INVALID_REQUEST`、`NO_ADAPTER`。
- api-key 插件：`MISSING_CREDENTIAL`、`INVALID_CREDENTIAL`（`assertUsableApiKey`）。

**默认重试集合**（`.../lib/types/retry-policy.js:16-22`）：

```js
const DEFAULT_RETRYABLE_CODES = Object.freeze([
    EMPTY_RESPONSE_CODE, 'RATE_LIMIT', 'SERVER', 'TIMEOUT', 'TRANSPORT',
]);
```

### 要点（坑）

- 抛**非 HarnessError**（如 `TypeError`、fetch 的 `TypeError`）→ `code:'UNKNOWN'`，**不在**默认重试集合里，
  不会重试。想被重试就要抛 `LlmError(msg, 'RATE_LIMIT'|'SERVER'|'TIMEOUT'|'TRANSPORT'|'EMPTY_RESPONSE')`。
- `AUTH` / `MISSING_CREDENTIAL` / `INVALID_CREDENTIAL` / `CONTEXT_WINDOW_EXCEEDED` / `QUOTA` 都是
  **不可重试**语义。
- **合成适配器不需要自己抓异常**：内层 `ctx.llm.stream()` 已经把内层失败转成了一个 terminal finish
  chunk，原样透传即可；只有你自己在转发途中抛的错才会被外层 `adapterStream` 再包一次
  （注意此时内层若已 yield 过 finish，就会**两个 finish**）。
- 取消：`options.signal` 中止时 `adapterFailureChunk` 产出 `kind:'aborted'`；转发时把 `signal` 原样
  交给内层（`{...options}` 已经带上）。

---

## 8. 模型选择现状：GUI 数据来源与选择结果

### 结论

GUI 数据来自 **Host 的 `buildModelCatalog(ctx)`**（`ctx.llm.listProviders()` + `listModels` +
`resolveModelInfo`），客户端由 `ModelDirectoryResolver` / `ModelDirectory`（`ctx.modelDirectories`）
持有，经 `remote.session.modelCatalog()` 拉取、经 `remote.session.selectModel()` 提交。
选中 `jev-router/auto` 后，`session.selectModel` **记录的就是 `{provider:'jev-router', model:'auto'}`
（外加 `reasoningEffort`——仅当 `auto` 的 `resolveModel` 声明了 `reasoning` 且存在显式或默认档位）**，
后续 `GenerateOptions.provider/model` **就是它**（在 `agent/request` 瀑布里被强制写成选中项）。

### 证据

**Host 目录**：见 §2 的 `buildModelCatalog`（`dsh-api-session-controller/lib/index.js:500`）。
**可用性校验**（同文件 `555-566`）——选中 `auto` 的前提是 `listModels` 真的返回它：

```js
async function modelAvailable(ctx, selection) {
	if (!ctx.llm.listProviders().some((provider) => provider.id === selection.provider)) return false;
	let models;
	try {
		models = await ctx.llm.listModels(selection.provider);
	} catch (error) { throw new RemoteError("session/model-unavailable", ...); }
	return models.some((model) => model.id === selection.model);
}
```

**`selectModel`**（同文件 `720-748`）：

```js
	async selectModel(request) {
		const agent = await this.resolveAgent(request.sessionId);
		return this.agents.serializeImageAdmission(agent, async () => {
			try {
				await this.requireModel(request);
				const resolved = await this.ctx.llm.resolveCallConfig({
					provider: request.provider,
					model: request.model,
					...request.reasoningEffort === void 0 ? {} : { reasoningEffort: ReasoningEffortId(request.reasoningEffort) }
				});
				const selected = {
					provider: resolved.provider,
					model: resolved.model,
					...resolved.reasoningEffort === void 0 ? {} : { reasoningEffort: resolved.reasoningEffort }
				};
				this.agents.selectForNextRequest(agent, selected);
				this.ctx.agentDefaultModel.saveSelection(selected).catch((error) => { ... });
				return { selected: { ...selected } };
			} catch (error) { ... throw new RemoteError("session/model-unavailable", ...); }
		});
	}
```

`resolveCallConfig` 只可能补出 `reasoningEffort`（默认档）与 `maxTokens`；`selected` **只取
provider/model/reasoningEffort**，`maxTokens` 被丢弃。

**写入会话日志 + 缓存**（同文件 `319-322`）：

```js
	selectForNextRequest(agent, selection) {
		agent.session.append("model/selection", selection);
		this.selectionFor(agent).current = selection;
	}
```

**选中项如何变成 `GenerateOptions`**（`dsh/node_modules/@deepseek-ai/dsh-agent/lib/index.js:166-192`）：

```js
function installModelSelection(agentCtx, selection) {
	const disposeAssembly = agentCtx.on("system-prompt/assemble", async (_assembly, _context, next) => {
		const selected = selection.current;
		const assembled = await next();
		selection.assembled = selected;
		if (selected === void 0) return assembled;
		return { ...assembled, variables: { ...assembled.variables, provider: selected.provider, model: selected.model } };
	});
	const disposeRequest = agentCtx.on("agent/request", async (_payload, next) => {
		const resolved = await next();
		const selected = selection.assembled;
		if (selected === void 0) return resolved;
		const { reasoningEffort: _inheritedEffort, ...withoutInheritedEffort } = resolved;
		return {
			...withoutInheritedEffort,
			provider: selected.provider,
			model: selected.model,
			...selected.reasoningEffort === void 0 ? {} : { reasoningEffort: selected.reasoningEffort }
		};
	});
	...
}
```

**loop 侧**（`dsh-agent-loop/lib/index.js:1163-1200`，节选）：

```js
	async prepareRequest(turn, step, signal) {
		...
		const proposedConfig = await this.dispatch.waterfall("agent/request", { turn, step, signal }, () => Promise.resolve(seedConfig));
		signal.throwIfAborted();
		if (!proposedConfig.provider || !proposedConfig.model) throw new Error(`agent "${this.id}" has no provider/model: ...`);
		let config;
		let preparedCall;
		try {
			preparedCall = await this.loopCtx.llm.prepareCall(proposedConfig, signal);
			config = preparedCall.config;
		} catch (error) {
			if (!(error instanceof LlmError) || error.code !== "NO_ADAPTER") throw error;
			config = proposedConfig;
		}
```

**客户端目录**（`dsh-client-ui-model-selection/lib/client.js:119` 拉 `modelCatalog`；`select()` 提交
`{sessionId, provider, model, reasoningEffort?}`）。`/model` 与 composer 两个入口共用同一
`ModelDirectory`，并在 `llm/adapters-updated`、`settings/document-updated`、凭证更新时**重新拉取**——
所以插件注册 provider 后目录会自动刷新（见 `dsh-client-ui-model-selection/README.md:69`）。

### 要点（坑）

- 选择器**只显示 `listModels` 返回的模型**，且该 provider 组不能有任何 `resolveModelInfo` 抛错。
- `selectModel` 会先 `requireModel` → 不调 `ctx.llm.listModels` 或返回空数组 → GUI 报
  `session/model-unavailable: Select an available model before sending a message.`
- 若 `auto` **不声明 `reasoning`**：`resolveCallConfig` 遇到请求里的 `reasoningEffort` 会抛
  `UNSUPPORTED_REASONING_EFFORT`；客户端在 `resolveModelInfo` 没有 reasoning 时不会自动带档位，
  但如果用户之前选过别的模型的档位并残留在默认配置里，可能命中。
- 若 `auto` **声明了 `reasoning`**：选择 `auto` 时会把 `defaultEffort` 写进 `model/selection` 与
  `agent-default-model` 配置；该档位会原样进 `GenerateOptions.reasoningEffort`，转发时若不删掉，
  真实模型可能不认（内层 `resolveCallWithInfo` → `UNSUPPORTED_REASONING_EFFORT` finish chunk）。
- `agent-default-model` 的 `saveSelection` 会**整体替换**该 entry 的 config（`{provider, model, reasoningEffort?}`），
  旧的 `reasoningEffort` 会被清掉（`dsh-agent-default-model/lib/index.js:54-67`）。

---

## 9. 会话日志一致性

### 结论

Loop 的日志快照发生在 **`agent/request` 瀑布之后、`llm.prepareCall` 之前**，且被 `deepFreeze`。
**`llm/stream` 瀑布上的改写不进日志**：选中/记录的是 `jev-router/auto`，实际用的是真实 provider，
assistant 消息的 `source` 也写成 `jev-router/auto`。也就是说——**会出现「日志说 A、实际用 B」，
而且是设计上无法从 session 日志反查真实模型**（除非插件自己另记）。README/JSDoc 里那句
"request waterfalls replace them and the loop logs changed snapshots" 指的是 **`agent/request`**
这一层，不是 `llm/stream`。

### 证据

**那句原文**（`dsh/node_modules/@deepseek-ai/dsh-llm/lib/types/call-config.js:1-6`）：

```js
/**
 * Conversation call configuration and freeze utilities. Provider routing,
 * model, reasoning effort, and sampling values are request-header state that
 * can affect cache reuse; request waterfalls replace them and the loop logs
 * changed snapshots instead of allowing silent per-call drift.
 * @module dsh-llm/call-config
 */
```

**日志写入点**（`dsh-agent-loop/lib/index.js:1202-1277`，节选）：

```js
	buildRequest(config, preparedCall, tools, position, startsRequestSeries, signal) {
		const { session } = this;
		const surfaceGeneration = session.surface.contentGeneration;
		const header = canonicalHeader({
			config,
			...preparedCall === void 0 ? {} : { adapterDefaults: preparedCall.adapterDefaults },
			...tools.length > 0 ? { tools } : {}
		});
		const baseline = this.session.requestHeader();
		...
		} else if (baseline === void 0 || !headerEquals(baseline, header)) headerSeq = this.session.append("request/header", {
			header, reason: "change", ...startsSeries ? { startsSeries: true } : {}
		}).seq;
		...
		const requestContext = {
			provider: config.provider, model: config.model,
			...contextWindow === void 0 ? {} : { contextWindow },
			...systemPromptUpdate === void 0 ? {} : { systemPromptUpdate }
		};
		...
		deepFreeze(header);
		const boundaryMessages = session.deriveMessages();
		for (const message of boundaryMessages) { if (this.frozenMessages.has(message)) continue; deepFreeze(message); this.frozenMessages.add(message); }
		Object.freeze(boundaryMessages);
		return markAgentLoopRequest(Object.freeze({
			...header.config,
			messages: boundaryMessages,
			toolHistory: session.toolHistory(),
			...header.tools !== void 0 ? { tools: header.tools } : {},
			sessionId: this.session.id,
			signal
		}));
	}
```

`config` 来自 `prepareRequest`：`preparedCall = await llm.prepareCall(proposedConfig)`，
`config = preparedCall.config`，而 `proposedConfig` 已经过 `agent/request` 瀑布（选中项在此生效）。
**因此日志记录的就是选中项 `jev-router/auto`**。

**派发点**（同文件 `1072`）：

```js
				const stream = preparedCall?.stream(request) ?? this.loopCtx.llm.stream(request);
```

`preparedCall.stream(request)` → `streamWithRegistration(request, prepared)` → `ctx.waterfall(...)`
→ 监听器可在此**完全改写去向**，而 `request`（已冻结、已标记）不变。

**assistant 消息源取自 request**（同文件 `1137-1144`，见 §4 引用）→ 记录 `jev-router/auto`。

**loop 请求重建不变式不检查 provider**（`dsh-agent-loop/lib/invariant.js:28`）：

```js
			if (!(options.model === header.config.model && options.system === void 0 && options.temperature === header.config.temperature && options.maxTokens === header.config.maxTokens && JSON.stringify(options.stop) === JSON.stringify(header.config.stop) && JSON.stringify(options.tools ?? []) === JSON.stringify(header.tools ?? []))) fail(`llm request for session "${String(session.id)}" diverges from the folded request header`);
```

（只比 `model`，**不比 `provider`**；而且内层 forwarded 是未标记的新对象，直接 `return next()` 跳过。）

### 要点（坑）

- **`llm/stream` 层改写 = 不可见**。不要指望日志能还原真实模型；插件必须**自己记录路由决策**
  （例如 `~/.dsh/jev-router/decisions.jsonl`，当前 P1 已这样做）。
- 反过来，**要"日志可见地换模型"，应该在 `agent/request` 瀑布里换**——那才是 loop 会在
  `request/header(reason:'change')` 里记快照的层，且 `installModelSelection` 正是这么做的。
  但 `agent/request` 是 agent-scoped 事件，注册点与生命周期要和 agent 绑定。
- `request` 对象被 `deepFreeze` + `WeakSet` 标记，**不能改写**；`forAdapter` 之所以新建 message
  对象，正是因为原对象已冻结。
- 合成 provider 的选择会同时写进 `agent-default-model`（跨会话默认），用户切走前所有新会话都用它。

---

## 10. 可直接复制的适配器骨架

目标：注册 provider `jev-router`，`listModels` 只返回一个 `auto`，`stream` 内部转发到 `opencode-go`
的具体模型并**原样透传 chunk**。

**实现前须知（本机实测）**：

- profile 插件目录**解析不到** `@deepseek-ai/dsh-llm` 包：
  `node -e "import('@deepseek-ai/dsh-llm')"` → `ERR_MODULE_NOT_FOUND`。
  DSH 启动时确实装了 `ResolutionRouter`（`dsh-app-boot/lib/index.js:1535+`），它能把
  `@deepseek-ai/*` 解析到 app 内置副本，但只对"linked root 且其 `package.json` 的
  **`peerDependencies` 声明了该包名**"的导入生效（`readPeerNames` 只读 `peerDependencies`，
  `routeLinked` 见 `dsh-app-boot/lib/index.js:1462-1477`）。**未找到证据**证明本机链路已生效，
  因此骨架**不 import dsh-llm**，用鸭子类型（父级 P0 已验证可行）。若要用 `LlmAdapter` 基类，
  请先在插件 `package.json` 里加 `"peerDependencies": {"@deepseek-ai/dsh-llm": "*"}` 并实测。
- 严格协议校验器（`dsh-llm/invariant`、`dsh-agent-loop/invariant`）在本机 desktop profile **未挂载**。

```js
// packages/dsh-plugin/lib/llm/router-adapter.js
/**
 * 注册合成 provider `jev-router`（唯一模型 `auto`），把请求转发到 opencode-go 的真实模型。
 *
 * 设计要点（全部有实现级依据，见 docs/dsh-integration/DSH-LlmAdapter-接口调研报告_v1.md）：
 *  1) `llm/stream` 监听器负责真正的转发：它拿到的是未被改写的请求，且**不会**在合成适配器那层
 *     被投影一次（图片占位/tool 更新只按真实模型投影一次）。
 *  2) 合成适配器的 `stream` 只是兜底（`stream()` 是抽象方法，必须实现）。
 *  3) 递归守卫：内层调用的 provider 是真实 provider，监听器只认 `jev-router`，天然不会重入。
 *  4) replayState：历史里记的是 `jev-router`，真实 adapter 的 `forAdapter()` 会剥掉它（见 §4）。
 *     不在这里做 `source` 改写——那需要逐条核对真实 adapter 的 replay 校验器，风险自负。
 */

export const ROUTER_PROVIDER = 'jev-router'
export const AUTO_MODEL = 'auto'
export const AUTO_MODEL_NAME = '自动选择'
export const TARGET_PROVIDER = 'opencode-go'
export const TARGET_MODEL = 'deepseek-v4.1-flash'

const AUTO_INFO = Object.freeze({
  provider: ROUTER_PROVIDER,
  id: AUTO_MODEL,
  name: AUTO_MODEL_NAME,
  description: '由 JEV 判断任务类型后自动选择真实模型',
  // 必须含 image，否则运行时在合成这一层就把图片换成占位文本；
  // 也可以整个省略 inputModalities，效果同样是"不投影"。
  inputModalities: ['text', 'image'],
})

const adapter = {
  // 注册期同步调用；id 必须 === provider，name 必须非空
  providerInfo(provider) {
    return { id: provider, name: 'JEV Model Router' }
  },
  // 返回 undefined = 使用 normal 默认（5 次重试：EMPTY_RESPONSE/RATE_LIMIT/SERVER/TIMEOUT/TRANSPORT）
  providerRetryPolicy() {
    return undefined
  },
  // 必须是**同步**、无 I/O 的；本路由不声明图片计价
  imageRequestPricing() {
    return undefined
  },
  // 只需 provider / id / name；description / inputModalities 可选。
  // contextWindow / maxTokens / reasoning 写在这里会被静默丢弃 —— 放 resolveModel。
  async listModels(provider) {
    return [{ ...AUTO_INFO, provider }]
  },
  // 每个目录里的 id 都必须能成功 resolveModel，否则整个 provider 组在 GUI 里变成 failure。
  // 不要声明 reasoning（否则会话被迫选档位）与 defaultMaxTokens（会被物化进请求再转发给真实模型）。
  async resolveModel(provider, model) {
    return {
      provider,
      id: model,          // 必须 === 请求的 model
      name: AUTO_MODEL_NAME,
      description: AUTO_INFO.description,
      inputModalities: ['text', 'image'],
      context: { contextWindow: 262144 },   // 保守值；只影响 request/context 与计量
    }
  },
  // 默认实现即可；显式写出是为了"绑定一次 prepare 的 dispatch"这一语义与 pi-ai 一致。
  async prepareCall(provider, model, signal) {
    return {
      model: await adapter.resolveModel(provider, model, signal),
      stream: (options) => forward(ctxRef, options),
    }
  },
  // 抽象方法，必须实现；正常情况下会被下面的 waterfall 监听器短路，不会走到这里。
  stream(options) {
    return forward(ctxRef, options)
  },
}

let ctxRef = null

export function registerRouterAdapter(ctx) {
  ctxRef = ctx
  ctx.llm.registerAdapter([ROUTER_PROVIDER], adapter)

  // 主路径：在进入 adapterStream 之前接管。
  // 注意 next 是零参闭包 —— next(modifiedOptions) 是无效的（§5）。
  ctx.on('llm/stream', (options, next) => {
    if (options?.provider !== ROUTER_PROVIDER) return next()
    return forward(ctx, options)
  })
}

/**
 * 解析真实路由，然后原样透传内层 chunk。
 * @param {object} ctx Cordis 上下文（含 llm 服务）
 * @param {object} options 原始 GenerateOptions（loop 请求是 deep-frozen 的，只能读）
 */
async function* forward(ctx, options) {
  // TODO: 这里换成真正的 JEV 决策（分类 → 策略 → 目标 provider/model）。
  const target = { provider: TARGET_PROVIDER, model: TARGET_MODEL }

  const forwarded = { ...options, provider: target.provider, model: target.model }

  // 合成模型不声明 reasoning，所以正常情况下 options.reasoningEffort 不存在；
  // 防御性删除，避免真实模型收到不认的档位（内层会抛 UNSUPPORTED_REASONING_EFFORT）。
  delete forwarded.reasoningEffort

  // `preparedCall.stream(request)` 传进来的 options 是 deep-frozen 的，
  // 但 spread 出来的 forwarded 是普通对象，可以再交给 ctx.llm.stream。
  // 内层会重新 prepareCall + 归一化（每个 step 两次 prepareCall，可接受）。
  yield* ctx.llm.stream(forwarded)
}
```

在插件 `apply` 里挂上（插件导出 `inject` 至少包含 `llm`，与 `dsh-llm-pi-ai` 一致）：

```js
// packages/dsh-plugin/lib/index.js
import { registerRouterAdapter } from './llm/router-adapter.js'

export const name = 'jev-router'
export const inject = ['llm']          // pi-ai 用 ["llm"]；ctx.llm 会在 apply 前就绪

export function apply(ctx, config = {}) {
  registerRouterAdapter(ctx)
}
```

**骨架的已知取舍**（逐条对应上面的证据）：

1. `listModels` 返回 `auto` 后，`/model` 选择器会立刻出现 `jev-router / 自动选择`（目录在
   `llm/adapters-updated` 上自动重拉）。
2. `session.selectModel` 会记录 `{provider:'jev-router', model:'auto'}`（无 `reasoningEffort`，
   因为 `auto` 未声明 reasoning）；`GenerateOptions.provider/model` 即此二者。
3. session 日志（`model/selection`、`request/header`、`request/context`、`assistant/message.source`）
   全部记 `jev-router/auto`，**不记真实模型**——决策必须自己落盘。
4. replayState 会因 `forAdapter()` 被剥（真实适配器降级为 provider-neutral 历史），代价是丢
   provider 原生 fidelity，不影响正确性。
5. `imageRequestPricing` 返回 `undefined` = 使用 token meter 的中性估算；若真实路由按视觉 token 计费，
   这里应返回 `{ priceImages(images){ return images.map(...) } }`（形状见 §1 表）。

---

## 附：明确「未找到证据」的项

1. **`lib/types/*.d.ts` 实体文件**：`package.json` 的 `files` 列了 `lib/types/**/*.d.ts`，但 asar 内
   **只有 `.js`**。本报告的 TS 类型均取自 `lib/typert.host.js` 里的 `"declaration"` 字段（官方
   `.d.ts` 的线上投影，与运行时类逐字对应）。未找到独立 `.d.ts`。
2. **`.agents/notes/*` 与 `docs/subsystems/llm-streaming.md`**：README 大量外链，但 asar 内
   `find ".agents/notes"`、`find "subsystems"`、`find "llm-streaming"` 均为空。任务描述里那句
   "…and the loop logs changed snapshots…" 的**完整出处**就是 §9 引用的 `call-config.js` JSDoc，
   没有更长的 Agent Note 可取证。
3. **`usage` 必须早于 `finish`**：只在 README 与 `BlockAssembler` 注释中作为不变式出现；
   `invariant.js` 只强制"至多一次 usage"和"finish 后无 chunk"。**未找到**强制顺序的代码。
4. **`next(modifiedOptions)` 是否可用**：Cordis `waterfall` 源码已证明 `next` 形参表为空 → 不可用。
   这是确凿结论，不是"未找到"。
5. **在 DSH 进程内从 linked 插件 import `@deepseek-ai/dsh-llm`**：解析器存在（app-boot 的
   ResolutionRouter），但触发条件是插件 `package.json` 的 `peerDependencies` 声明——**未实测**。
6. **`reasoningEffort` 的品牌类型**：`ReasoningEffortId = Branded<'ReasoningEffortId'>` 是纯编译期
   品牌，运行时就是字符串；插件不 import dsh-llm 也能安全地传普通字符串。
