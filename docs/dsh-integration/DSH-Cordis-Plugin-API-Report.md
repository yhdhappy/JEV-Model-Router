# DSH / Cordis 插件开发 API 报告

> 证据全部来自 `/Applications/DeepSeek Harness.app/Contents/Resources/app.asar`
> （版本基线：`@deepseek-ai/dsh-*` 0.2.0-rc.2，`@deepseek-ai/cordis` ~4.0.4）。
> 归档内路径均省略 asar 前缀，例如 `dsh/node_modules/@deepseek-ai/dsh-commands/lib/index.js`。
> 归档里同时带有 **built `lib/*.js`** 和部分包的 **`src/*.ts`**；下面的引文原样抄录。

归档里还存在官方自带的权威文档，本报告的核心结论都与之交叉验证过：

- `dsh/node_modules/@deepseek-ai/dsh-agent-preset/skills/cordis-plugin-development/SKILL.md`
- `dsh/node_modules/@deepseek-ai/dsh-agent-preset/skills/cordis-plugin-development/references/host-plugin.md`
- `dsh/node_modules/@deepseek-ai/dsh-agent-preset/skills/cordis-composition-reference/SKILL.md`

---

## 1. 命令（command）注册：`ctx.commands`

### 结论

在 **Host 侧** 的插件 `apply(ctx)` 里调用 `ctx.commands.register({ name, description, input?, definitionId?, recordInput?, handler })`；
`handler(invocation)` 直接拿到 `{ commandId, agent, rawInput, attachments, signal }`，同步或异步返回 `{ kind:'success', text? }` 或 `{ kind:'error', text }`，
该文本由交互式 UI 适配器渲染，**不会变成 model message、不消耗 token**。纯文本命令不需要任何 UI 代码。

### 证据

**A. 官方用法（README）** — `dsh/node_modules/@deepseek-ai/dsh-commands/README.md`

```text
ctx.commands.register({
  name: 'plan',
  description: 'Enter plan mode',
  input: { hint: '<message>' },
  handler: ({ agent, rawInput }) => {
    // Runs directly against the agent; no model message is created.
    return { kind: 'success', text: 'plan mode selected' }
  },
})
```

同文件对该表面的描述：

```text
The handler returns `success` or `error` plus optional UI text that the adapter renders. `recordInput`
defaults to true; a command whose own authoritative domain event already carries the payload sets it to
false so the session log does not duplicate the input. Registering the same name twice in one scope throws.
```

```text
A command line starts with a slash at byte zero, a lowercase name containing letters, digits, `_` or `-`,
and then either end-of-input or whitespace. Everything after the name — including separator whitespace —
is the command's `rawInput`, and the command owns its own grammar for it.
```

**B. 注册实现与校验** — `dsh/node_modules/@deepseek-ai/dsh-commands/lib/index.js`

```js
const name = "commands";
const COMMAND_NAME = /^[a-z][a-z0-9_-]*$/u;
```

```js
	register(definition) {
		const registered = normalizeDefinition(definition);
		return this.layers.effect(this.ctx, (layer) => layer.commands.insert(registered.definition.name, registered), { label: "commands.register()" });
	}
```

`normalizeDefinition` 中的硬校验（决定哪些字段必填）：

```js
function normalizeDefinition(definition) {
	if (!COMMAND_NAME.test(definition.name)) throw new TypeError(`command name "${definition.name}" must match ${String(COMMAND_NAME)}`);
	if (typeof definition.description !== "string") throw new TypeError(`command "${definition.name}" description must be a string`);
	if (definition.description.trim().length === 0) throw new TypeError(`command "${definition.name}" description must not be empty`);
	if (typeof definition.handler !== "function") throw new TypeError(`command "${definition.name}" handler must be a function`);
	const rawInput = definition.input;
	let input;
	if (rawInput !== void 0) {
		if (typeof rawInput !== "object" || rawInput === null || !("hint" in rawInput) || typeof rawInput.hint !== "string") throw new TypeError(`command "${definition.name}" input hint must be a string`);
		if (rawInput.hint.trim().length === 0) throw new TypeError(`command "${definition.name}" input hint must not be empty`);
		if ("attachments" in rawInput && rawInput.attachments !== void 0 && typeof rawInput.attachments !== "boolean") throw new TypeError(`command "${definition.name}" input attachments flag must be a boolean`);
		...
	}
```

**C. handler 收到的参数** — 同文件 `execute()`：

```js
			const invocation = Object.freeze({
				commandId,
				agent,
				rawInput: parsed.rawInput,
				attachments,
				signal
			});
			let result;
			try {
				const output = command.definition.handler(invocation);
				result = normalizeResult(parsed.name, await withAbort(Promise.resolve(output), signal));
```

**D. 返回值格式（严格）** — 同文件 `normalizeResult()`：

```js
	if (result.kind === "success") {
		if (result.text !== void 0 && typeof result.text !== "string") throw new TypeError(`command "${command}" success text must be a string when supplied`);
		if (result.sourceEventSeq !== void 0 && (!Number.isSafeInteger(result.sourceEventSeq) || result.sourceEventSeq < 0 || Object.is(result.sourceEventSeq, -0))) throw new TypeError(`command "${command}" success sourceEventSeq must be a non-negative safe integer when supplied`);
		return Object.freeze({
			kind: "success",
			...result.text === void 0 ? {} : { text: result.text },
			...result.sourceEventSeq === void 0 ? {} : { sourceEventSeq: SessionSeq(result.sourceEventSeq) }
		});
	}
	if (result.kind === "error") {
		if (typeof result.text !== "string" || result.text.trim().length === 0) throw new TypeError(`command "${command}" error text must be a non-empty string`);
		return Object.freeze({
			kind: "error",
			text: result.text
		});
	}
	throw new TypeError(`command "${command}" returned unknown result kind "${String(result.kind)}"`);
```

**E. 真实使用者（最小纯文本命令）** — `dsh/node_modules/@deepseek-ai/dsh-command-compact/lib/index.js`（全文关键部分）

```js
const name = "command-compact";
const inject = ["commands", "compaction"];
const USAGE = "Usage: /compact (no arguments)";
```

```js
function apply(ctx) {
	const active = /* @__PURE__ */ new Set();
	const handler = (invocation) => {
		const operation = executeCompact(ctx, invocation);
		active.add(operation);
		const retire = () => {
			active.delete(operation);
		};
		operation.then(retire, retire);
		return operation;
	};
	ctx.effect(function* () {
		yield async () => {
			await Promise.allSettled(active);
		};
		yield ctx.commands.register({
			definitionId: CommandDefinitionId("@deepseek-ai/dsh-command-compact"),
			name: "compact",
			description: "Compact older conversation history",
			handler
		});
	}, "command-compact lifecycle");
}
```

并给出一个**更简单**的真实使用者 — `dsh/node_modules/@deepseek-ai/dsh-command-goal/lib/index.js`，它直接在 `apply` 里裸调用 `register`（没有 `ctx.effect` 包装）：

```js
function apply(ctx) {
	ctx.commands.register({
		definitionId: CommandDefinitionId("@deepseek-ai/dsh-command-goal"),
		name: "goal",
		description: "Set or view the goal for a long-running task",
		input: {
			hint: "[<objective>|clear|edit <objective>|pause|resume]",
			attachments: true
		},
		handler: (invocation) => executeGoalCommand(ctx, invocation)
	});
}
```

> 为什么裸调用就够：`register()` 内部走 `this.layers.effect(this.ctx, ...)`，而 `ScopedLayers.effect()` 用的是
> `ctx.effect(...)`（`dsh/node_modules/@deepseek-ai/dsh-scope/lib/index.js:190-193`），
> 因此注册**自动绑定到当前 fiber**，插件卸载时自动反注册。只有在需要**等待自己启动的异步任务结束**时才需要显式 `ctx.effect(function*(){...})`（如 compact）。

**F. Host 侧还是 Client 侧？→ Host 侧。**

`dsh/node_modules/@deepseek-ai/dsh-commands/lib/index.js` 开头：

```js
import { Remote, TypertRemoteService } from "@deepseek-ai/dsh-typert-protocol";
...
let CommandRuntime = (() => {
	let _classSuper = TypertRemoteService;
	...
			_list_decorators = [Remote];
			_execute_decorators = [Remote];
```

即 `CommandRuntime` 是一个 **Host 服务**，只有 `list` / `execute` 两个方法被标记为 `@Remote` 暴露给 Client；
`register` **不是** Remote —— Client 无法注册命令。Client 侧只有生成的远程代理：

`dsh/node_modules/@deepseek-ai/dsh-commands/lib/typert.remote-client.js`

```js
export const TYPERT_REMOTE = {
  package: '@deepseek-ai/dsh-commands',
  descriptors: [
    {
      id: '@deepseek-ai/dsh-commands#commands/execute',
      service: 'commands',
```

**G. agent 作用域命令**（同一 README）：

```text
A plain registration is global. A command-producing plugin mounted beneath an agent's own context declares
its `commands` injection and registers an exact agent-scoped command, which shadows the global definition of
the same name for that agent only.
```

### 最小示例

```js
// lib/index.js —— 纯文本命令，无 UI
export const name = 'my-commands'
export const inject = ['commands']

export function apply(ctx) {
  ctx.commands.register({
    definitionId: 'my-plugin/echo',
    name: 'echo',
    description: 'Echo the text back',
    input: { hint: '<text>' },
    handler: ({ rawInput }) => ({ kind: 'success', text: `echo: ${rawInput}` }),
  })
}
```

---

## 2. 工具（tool）注册：`ctx.tools`

### 结论

用 `defineTool({...})`（来自 `@deepseek-ai/dsh-tools`）构造定义，再 `ctx.tools.register(definition)`；
参数 schema 用的是 **DSH 自己的统一 JSON-Schema DSL**（隐式对象属性表，`{ path: { type, required, description } }`），
**不是** schemastery、也不是裸 JSON Schema；`execute(args, exec)` 返回 `output.schema` 声明的那个 JSON 值（可 sync 可 Promise）。

### 证据

**A. 官方用法** — `dsh/node_modules/@deepseek-ai/dsh-tools/README.md`

```ts
import { readFile } from 'node:fs/promises'
import type { Context } from '@deepseek-ai/cordis'
import { defineTool } from '@deepseek-ai/dsh-tools'

declare const ctx: Context

ctx.tools.register(defineTool({
  name: 'read_file',
  description: 'Read a file from disk.',
  parameters: {
    path: { type: 'string', required: true, description: 'Absolute file path' },
    offset: { type: 'number' },
    limit: { type: 'number' },
  },
  output: {
    schema: { type: 'string' },
    render: (_args, value) => [{ type: 'text', text: value }],
  },
  async execute(args, exec) {
    // args is typed: { path: string; offset?: number; limit?: number }
    return readFile(args.path, { encoding: 'utf8', signal: exec.signal })
  },
}))
```

```text
The unified schema DSL supports `string`, `number`, `integer`, `boolean`, `null`, `array`, `object`,
author-only `json`, and exact-one `oneOf`; `InferValue` preserves exact types through 16 container levels
before widening to `JsonValue`. A raw JSON Schema (`JsonSchemaNode`) is the wire-level counterpart shared
with subagents, workflows, and MCP.
```

**B. `register` 的真实签名与校验** — `dsh/node_modules/@deepseek-ai/dsh-tools/lib/index.js:2879`

```js
	register(definition) {
		const name = definition.name;
		const output = definition.output;
		if (output === void 0 || typeof output !== "object" || typeof output.render !== "function" || output.presentationMeta !== void 0 && typeof output.presentationMeta !== "function") throw new TypeError(`tool "${name}" must declare output { schema, render, presentationMeta? }`);
		assertSupportedJsonSchema(output.schema);
		const timeoutMs = definition.timeoutMs;
		if (timeoutMs !== void 0 && (!Number.isFinite(timeoutMs) || timeoutMs <= 0)) throw new TypeError(`tool "${name}" timeoutMs must be a positive finite number`);
		if (name === "run_code") throw new Error(`tool name "${RUN_CODE_NAME}" is reserved for the PTC mode presentation transport and cannot be registered or shadowed`);
		return this.layers.effect(this.ctx, (layer) => layer.tools.insert(name, definition), { label: "tools.register()" });
	}
```

**C. `defineTool` 的完整可选字段** — 同文件 `lib/index.js:839`

```js
function defineTool(options) {
	const userExecute = options.execute;
	const userFinalizeContent = options.finalizeContent;
	const userProjectContent = options.projectContent;
	const userRender = options.output.render;
	const userPresentationMeta = options.output.presentationMeta;
	const userPresentCall = options.presentCall;
	const userPresentResult = options.presentResult;
	const userIsConcurrencySafe = options.isConcurrencySafe;
	if (options.timeoutMs !== void 0 && (!Number.isFinite(options.timeoutMs) || options.timeoutMs <= 0)) throw new Error(`defineTool(${options.name}): timeoutMs must be a positive finite number`);
	const parameters = parameterSchemaSpecToJsonSchema(options.parameters);
	const outputSchema = valueSchemaSpecToJsonSchema(options.output.schema);
	const validate = (args) => validateJsonSchemaValue(parameters, args, "");
```

```js
	async execute(args, exec) {
			const violations = validate(args);
			if (violations.length > 0) throw new ToolArgsError(violations);
			return userExecute(args, exec);
		}
	};
```

`exec` 的字段（从 `lib/index.js` 里的实际使用可见）：`exec.agent`、`exec.callId`、`exec.signal`、`exec.token`、`exec.rootCallId`。

**D. 参数 DSL 的精确词汇表** — 同文件 `lib/index.js:538` 与 `:793`

```js
const ANNOTATION_KEYS = [
	"description",
	"title",
	"default",
	"examples"
];
```

```js
		default: authorError(`${path}.type must be string/number/integer/boolean/null/array/object/json, or use oneOf`);
```

```js
function parameterSchemaSpecToJsonSchema(spec) {
	const compiled = compilePropertyMap(spec, "parameters");
	const schema = {
		type: "object",
		properties: compiled.properties,
		...compiled.required === void 0 ? {} : { required: compiled.required }
	};
	assertSupportedJsonSchema(schema);
	return schema;
}
```

`object` 节点必须显式写 `additionalProperties`（`true`/`false`），`array` 用 `items`，`oneOf` 与 `type` 互斥：

```js
			case "object":
				assertAuthorKeys(input, path, [
					...authorKeys,
					"type",
					"properties",
					"additionalProperties"
				]);
				if (!Object.hasOwn(input, "additionalProperties") || typeof input.additionalProperties !== "boolean") authorError(`${path}.additionalProperties must be explicitly true or false`);
```

```js
		if (Object.hasOwn(input, "oneOf")) {
			assertAuthorKeys(input, path, [...authorKeys, "oneOf", "type"]);
			if (Object.hasOwn(input, "type")) authorError(`${path} cannot declare both type and oneOf`);
			if (!isPlainJsonArray(input.oneOf)) authorError(`${path}.oneOf must be an array of at least two value schemas`);
```

**E. 真实实现** — `dsh/node_modules/@deepseek-ai/dsh-tool-todo/lib/index.js`

```js
import z from "@deepseek-ai/schemastery";
import { z as z$1 } from "zod";
import { defineTool } from "@deepseek-ai/dsh-tools";
...
const name = "tool-todo";
const inject = ["tools", "sessionProjections"];
```

```js
	ctx.tools.register(defineTool({
		name: "todo_write",
		description: describe(allowParallel),
		parameters: { todos: {
			type: "array",
			required: true,
			description: "The COMPLETE task list, replacing any previous list.",
			items: {
				type: "object",
				additionalProperties: false,
				properties: {
					content: {
						type: "string",
						required: true,
						description: "What the task is — a short imperative line."
					},
					status: {
						type: "string",
						required: true,
						enum: [...STATUSES],
						description: "pending (not started) | in_progress (now) | completed (done)."
					}
				}
			}
		} },
		output: {
			schema: { /* ... */ },
			render: (_args, value) => [{
				type: "text",
				text: `Updated todo list: ${value.counts.pending} pending, ${value.counts.inProgress} in progress, ${value.counts.completed} completed.`
			}]
		},
		execute(args, exec) {
			const todos = toTodoList(args.todos, allowParallel);
			if (!exec.agent) throw new Error("todo_write requires an owning agent session");
			exec.agent.session.append("todo/write", { todos });
			...
			return Promise.resolve({ todos: ..., counts: { pending: ..., inProgress: ..., completed: ... } });
		},
		presentCall: (args) => ({
			card: "generic",
			title: "Update todo list",
			kind: "other",
			rawInput: args.todos
		})
	}));
```

**F. `dsh-tools` 自身的 Config（说明它确实是 schemastery，但那是服务配置、不是工具参数 schema）** — `dsh-tools/lib/index.js:2664`

```js
var ToolRuntime = class extends Service {
	static inject = ["systemPrompt"];
	static Config = z.object({
```

### 最小示例

```js
import { defineTool } from '@deepseek-ai/dsh-tools'

export const name = 'my-tools'
export const inject = ['tools']

export function apply(ctx) {
  ctx.tools.register(defineTool({
    name: 'greet',
    description: 'Greet someone by name.',
    parameters: {
      name: { type: 'string', required: true, description: 'Who to greet' },
    },
    output: {
      schema: { type: 'string' },
      render: (_args, value) => [{ type: 'text', text: value }],
    },
    execute(args) {
      return `Hello, ${args.name}!`
    },
  }))
}
```

---

## 3. 插件入口与 Config

### 结论

一个 DSH 插件模块支持**两种**导出形状（Loader 的 `unwrapExports` + `registry.resolve` 决定）：

1. **具名导出**（Koishi 风格）：`export const name` / `export const inject` / `export const Config` / `export function apply(ctx, config)`；
2. **默认导出一个函数或一个 Service 类**：`export default function (ctx, config) {}` 或 `export default class X extends Service {}`。

`inject` 是字符串数组（或 `{ service: interceptConfig }` 对象）；`Config` 是 **`@deepseek-ai/schemastery`** 的 `z.object({...})`（standard-schema，同步校验）。

### 证据

**A. Plugin 形状** — `dsh/node_modules/@deepseek-ai/cordis/src/registry.ts:121`

```ts
  /** Function plugin called with `(ctx, config)`. */
  export interface Function<T = any> extends Base<T> {
    (ctx: Context, config: T): any
  }

  /** Class plugin constructed with `(ctx, config)`. */
  export interface Constructor<T = any> extends Base<T> {
    new (ctx: Context, config: T): any
  }

  /** Object plugin with an `apply(ctx, config)` method. */
  export interface Object<T = any> extends Base<T> {
    apply(ctx: Context, config: T): any
  }
```

```ts
  export interface Base<T = any> {
    /** Display name used for fiber diagnostics and logger names. */
    name?: string
    /** Standard-schema validator applied to config before the plugin starts. */
    Config?: StandardSchemaV1<any, T>
    /** Services the plugin requires; it only loads while all are available. */
    inject?: Inject
    /** Service name(s) the plugin provides (read by `Service` and by loaders). */
    provide?: string | string[]
    /** Service names whose intercept config the plugin declares it consumes. */
    intercept?: Dict<boolean>
  }
```

**B. 注册时读取哪些字段** — 同文件 `registry.plugin()`

```ts
  plugin(plugin: Plugin, config?: any, getOuterStack = buildOuterStack()) {
    // check if it's a valid plugin
    const callback = this.resolve(plugin)
    if (!callback) throw new Error('invalid plugin, expect function or object with an "apply" method, received ' + typeof plugin)
    this.ctx.fiber.assertActive()

    let runtime = this._internal.get(callback)
    if (!runtime) {
      let name = plugin.name
      if (name === 'apply') name = undefined
      runtime = { name, callback, fibers: new DisposableList(), Config: plugin.Config }
      this._internal.set(callback, runtime)
    }

    const fiber = new Fiber(this.ctx, config, Inject.resolve(plugin.inject), runtime, getOuterStack)
```

```ts
function isApplicable(object: Plugin) {
  return object && typeof object === 'object' && typeof object.apply === 'function'
}
```

```ts
  resolve(plugin: Plugin): Function | undefined {
    // plugin.apply may throw
    try {
      if (typeof plugin === 'function') return plugin
      if (isApplicable(plugin)) return plugin.apply
    } catch {}
  }
```

`Inject.resolve` 接受数组或对象：

```ts
  export function resolve(inject: Inject | null | undefined, result: Dict = Object.create(null)) {
    if (!inject) return result
    if (Array.isArray(inject)) {
      for (const name of inject) {
        result[name] = null
      }
```

**C. ESM 具名导出如何被接受** — `dsh/node_modules/@deepseek-ai/cordis-plugin-loader/src/index.ts:202`

```ts
  /** Normalize ESM/CJS/default export shapes before applying a plugin. */
  unwrapExports(exports: any) {
    if (isNullable(exports)) return exports
    exports = exports.default ?? exports
    // https://github.com/evanw/esbuild/issues/2623
    // https://esbuild.github.io/content-types/#default-interop
    if (!exports.__esModule) return exports
    return exports.default ?? exports
  }
```

> 关键点：没有 `default` 导出时，整个 **module namespace 对象**就是 plugin，
> 于是 `export const name` / `inject` / `Config` / `export function apply` 全部被 `registry.plugin()` 读到。

**D. 官方 export 形式说明** — `.../cordis-plugin-development/references/host-plugin.md`

```markdown
## Host plugin export forms

`index.js` exports one of these forms; do not mix them:

- `export function apply(ctx, config) {}` with optional `export const inject = ['tools']` and `export const Config`.
- A service class as the default export.

Register every resource inside `apply` with `ctx.effect` or `ctx.on` and return its cleanup. A plugin that declares `Config` validates the row's `config` at activation; query `Config.listConfigs` for an installed plugin's schema before writing its `config`, and follow `$defs` references in the returned document.
```

**E. `Config` 用 schemastery（真实例子）** — `dsh/node_modules/@deepseek-ai/dsh-tool-todo/lib/index.js`

```js
import z from "@deepseek-ai/schemastery";
...
/** Schemastery configuration for the todo tool consumer. */
const Config = z.object({ allowParallelInProgress: z.boolean().required() });
...
export { Config, apply, inject, name }
```

`dsh/node_modules/@deepseek-ai/dsh-agent-default-model/lib/index.js`（**Service 类 + `static Config` + 默认导出**）：

```js
import { Service } from "@deepseek-ai/cordis";
import z from "@deepseek-ai/schemastery";
...
var AgentDefaultModelConfig = class extends Service {
	ownerContext;
	config;
	saves = Promise.resolve();
	static Config = z.object({
		provider: z.string().required().volatile(),
		model: z.string().required().volatile(),
		reasoningEffort: z.string().volatile()
	});
	constructor(ownerContext, config) {
		super(ownerContext, "agentDefaultModel");
		this.ownerContext = ownerContext;
		this.config = config;
```

```js
export { AgentDefaultModelConfig, AgentDefaultModelConfig as default };
```

**F. Config 校验发生在 fiber 启动前** — `dsh/node_modules/@deepseek-ai/cordis/src/fiber.ts:51`

```ts
export function resolveConfig(runtime: Plugin.Runtime, config: any) {
  if (!runtime.Config) return config
  // TODO: async validation
  const result = runtime.Config['~standard'].validate(config)
  if ('then' in result) {
    throw new TypeError('Async config validation is not supported')
  }
  if (result.issues) {
    throw new ValidationError(result.issues)
  } else {
    return result.value
  }
}
```

### 最小可运行插件骨架

```js
// lib/index.js
import z from '@deepseek-ai/schemastery'

export const name = 'my-plugin'           // fiber / logger 名
export const inject = ['tools', 'commands'] // 依赖的服务，全部就绪才 apply
export const Config = z.object({
  greeting: z.string().default('Hello'),
})

export function apply(ctx, config) {
  const greeting = config.greeting

  // 资源注册直接绑定当前 fiber，卸载自动清理
  const disposeHello = ctx.commands.register({
    name: 'hello',
    description: 'Say hello',
    handler: () => ({ kind: 'success', text: `${greeting}!` }),
  })

  // 需要跨 fiber 生命周期的资源用 ctx.effect 显式声明清理
  ctx.effect(() => {
    ctx.logger.info('%s: active', name)
    return () => ctx.logger.info('%s: disposed', name)
  }, `${name} lifecycle`)

  return disposeHello
}
```

---

## 4. Bundle 打包

### 结论

bundle 就是一个普通 npm 包，其 `package.json` 声明 **`dsh.bundle.patch`**（字符串或字符串数组，相对包目录），
指向一个 **YAML 顶层数组**的 loader patch 文件；loader 会把包内每个 patch 文件按 `dsh.profile.bundles` 的顺序
**叠加（applyEntryPatches）到一个空 entry list 上**，最后再叠加 profile 自己的 `cordis.patch.yml`。

### 证据

**A. bundle 判定与 patch 路径解析** — `dsh/node_modules/@deepseek-ai/dsh-app-boot/lib/index.js`

```js
function bundlePatchFiles(bundle) {
	const declared = typeof bundle.patch === "string" ? [bundle.patch] : bundle.patch;
	if (!Array.isArray(declared) || !declared.every((file) => typeof file === "string")) throw new Error("dsh.bundle.patch must be a file path or a list of file paths");
	return declared;
}
```

```js
function bundlePatchPaths(packageDir, bundle) {
	return bundlePatchFiles(bundle).map((file) => join(packageDir, file));
}
```

```js
		const packageDir = resolveBundleDir(binName, packageName, installAnchor, dir);
		const bundleManifest = readProfileManifest(binName, packageDir);
		const bundle = bundleManifest.dsh?.bundle;
		if (bundle === void 0) throw new Error(`${binName}: profile bundle ${JSON.stringify(packageName)} declares no dsh.bundle in its package.json`);
		...
		const patchPaths = bundlePatchPaths(packageDir, bundle);
		const patches = patchPaths.flatMap((patchPath) => loadOverlayPatches(binName, patchPath));
```

profile 的 `package.json` 用 `dsh.profile.bundles` 列 bundle：

```js
	const bundles = readProfileManifest(binName, dir).dsh?.profile?.bundles ?? [];
```

```js
	const manifest = {
		name: `dsh-profile-${basename(dir)}`,
		private: true,
		dependencies: {},
		dsh: { profile: { bundles: [...bundles] } }
	};
```

**B. patch 语义的**唯一**实现** — `dsh/node_modules/@deepseek-ai/cordis-plugin-include/src/index.ts`（`applyEntryPatches`）

```ts
export function applyEntryPatches(
  data: EntryOptions[],
  patches: PatchOptions[] | undefined,
  warn: (message: string, ...args: any[]) => void,
): EntryOptions[] {
  if (!patches?.length) return [...data]
  data = structuredClone(data)

  const entryMap = new Map<string, EntryOptions>()
  ...
  for (const patch of patches) {
    const { id, insert, name, ...overrides } = patch

    if (insert) {
      if (id) {
        const target = entryMap.get(id)
        if (!target) {
          warn('patch insert: entry %C not found', id)
          continue
        }
        if (!target.group) {
          warn('patch insert: entry %C is not a group', id)
          continue
        }
        if (!Array.isArray(target.config)) target.config = []
        target.config.push(...insert)
      } else {
        data.push(...insert)
      }
      ...
      buildMap(insert)
      continue
    }

    if (!id) {
      warn('patch: id is required for non-insert patches')
      continue
    }

    const target = entryMap.get(id)
    if (!target) {
      warn('patch: entry %C not found', id)
      continue
    }

    if (name && name !== target.name) {
      warn('patch: name mismatch for %C (expected %C, got %C), skipping', id, target.name, name)
      continue
    }

    for (const [key, value] of Object.entries(overrides)) {
      if (key === 'id') continue
      target[key] = value
    }
  }

  return data
}
```

行对象（row）的字段定义 — 同文件：

```ts
/** Serialized plugin entry options stored in loader config files. */
export interface EntryOptions {
  /** Stable id inside the containing entry tree. */
  id: string
  /** Module specifier imported by the entry tree. */
  name: string
  /** Config passed to the plugin. */
  config?: any
  /** Marks this entry as a nested group. */
  group?: boolean | null
  /** Prevents this entry and descendants from running. */
  disabled?: boolean | null
  /** Required services or service intercept config for this entry. */
  inject?: Inject | null
}
```

patch 对象：

```ts
/** Runtime patch applied to entries loaded from an included config file. */
export interface PatchOptions {
  id?: string
  insert?: EntryOptions[]
  name?: string
  config?: any
  group?: boolean | null
  disabled?: boolean | null
  inject?: any
  intercept?: any
  isolate?: any
  [key: string]: any
}
```

**C. Loader YAML 方言（权威说明）** — `.../cordis-composition-reference/SKILL.md`

```markdown
## Loader patch dialect

A profile composes an ordered list of patch layers over the bundle entry lists. Each patch is a mapping:

- `insert: [rows]` appends rows; with an `id` naming an existing `group: true` row, the rows are appended inside that group's `config` list.
- A patch with an `id` and no `insert` targets the existing row with that id. Supplied fields replace the row's fields; `config` is replaced wholesale, never deep-merged, so restate every field the row needs. A truthy `name` asserts the existing plugin name rather than renaming it.
- Non-insert patches without a nonempty `id`, and targets that match no row, are warned about and skipped.

A row has `id`, `name` (the plugin package specifier; inserted relative paths are anchored beside their patch file), optional `config`, and optional `disabled`, `inject`, `intercept`, and `isolate`.

- `group: true` with `name: cordis:group` makes `config` a nested entry list and allows patches to insert into it by id. `cordis:include` loads a literal YAML or JSON entry list from `config.path`.
- `disabled` accepts a boolean, null, or a `!!js` expression evaluated against the Loader context at every mount decision. A disabled row omits required `config` unless `group: true` forces activation.
- `!!js` scalars are Loader expressions, never `!js`. Inside `config` they are evaluated after the row's declared injections activate, against that plugin's context (`ctx.<service>`), so `!!js dshHomePath('sessions')` and `!!js "!ctx.get('profileContext')"` are valid. Other row metadata stays literal.
- `isolate` maps service names to `true` or a realm label; a preset plugin that provides a service isolates the provider and all consumers together.
```

**D. `!!js` 的真实解析** — `dsh/node_modules/@deepseek-ai/cordis-plugin-include/src/index.ts`

```ts
const JsExpr = new yaml.Type('tag:yaml.org,2002:js', {
  kind: 'scalar',
  resolve: (data) => typeof data === 'string',
  construct: (data) => ({ __jsExpr: data }),
  predicate: isJsExpr,
  represent: (data) => data['__jsExpr'],
})

export const entryListSchema = yaml.JSON_SCHEMA.extend(JsExpr)
```

求值上下文 — `dsh/node_modules/@deepseek-ai/cordis-plugin-loader/src/config/utils.ts`：

```ts
// eslint-disable-next-line no-new-func
/** Evaluate a JavaScript expression against a loader context scope. */
export const evaluate = new Function('ctx', 'expr', `
  with (ctx) {
    return eval(expr)
  }
`) as ((ctx: object, expr: string) => any)

/** Recursively replace YAML `!!js` expression nodes with evaluated values. */
export function interpolate(ctx: object, value: any) {
  if (isJsExpr(value)) {
    return evaluate(ctx, value.__jsExpr)
  }
```

挂载点 — `dsh/node_modules/@deepseek-ai/cordis-plugin-loader/src/index.ts`（`internal/config` waterfall）：

```ts
    ctx.on('internal/config', function (this: Fiber, _config, next) {
      const config = next()
      if (!this.entry || this.parent.fiber?.entry === this.entry) return config
      // Tree carriers (Group, Include) keep their configs literal: their
      // entry and patch lists hold other rows' configs, whose `!!js`
      // expressions belong to those rows' own fibers.
      const plugin = this.runtime?.callback as Record<PropertyKey, unknown> | undefined
      if (plugin?.[EntryGroup.key]) return config
      return interpolate(this.ctx, config)
    }, { global: true })
```

`disabled` 的 `!!js` 在每个 mount 决策时求值 — `.../src/config/entry.ts`：

```ts
  /**
   * Effective disabled state: a `!!js` expression evaluates against the loader
   * context. The raw node stays in the options, so write-back keeps the form.
   */
  private disabledOf(options: EntryOptions): boolean {
    return isJsExpr(options.disabled)
      ? Boolean(this.evaluate(options.disabled.__jsExpr))
      : Boolean(options.disabled)
  }
```

**E. `name` 是模块 specifier；相对路径也被支持，且锚定在 patch 文件旁边**

`dsh/node_modules/@deepseek-ai/dsh-app-boot/lib/index.js`：

```js
/** Convert inserted filesystem paths to file URLs, anchoring relative paths beside the patch; keep assertion names literal. */
function anchorInsertedPluginNames(patches, file) {
	const base = dirname(resolve(file));
	const visit = (entry) => {
		if (typeof entry.name === "string" && (isAbsolute(entry.name) || entry.name.startsWith("./") || entry.name.startsWith("../"))) entry.name = pathToFileURL(resolve(base, entry.name)).href;
		if (entry.group && Array.isArray(entry.config)) entry.config.forEach(visit);
	};
	for (const patch of patches) patch.insert?.forEach(visit);
	return patches;
}
```

对照 `cordis-plugin-include` 的 baseUrl 锚定（对 `cordis:include` 加载的 entry list）：

```ts
    this.ctx.baseUrl = new URL('.', pathToFileURL(this.filename)).href
```

文档也确认：

```markdown
A row has `id`, `name` (the plugin package specifier; inserted relative paths are anchored beside their patch file)
```

所以：`name` **通常是**包 specifier（如 `@local/my-plugin`），**也可以**是 `./lib/index.js` 这种相对路径；
如果本插件就是从**本地目录**安装的 bundle，**推荐直接用包名**（安装后 profile 的 `node_modules` 能解析到它），
相对路径只在特殊场合（不想走包解析）使用。

**F. 真实 `cordis.patch.yml` 片段**

`dsh/node_modules/@deepseek-ai/dsh-base/cordis.patch.yml`：

```yaml
- insert:
    - id: tool-plugin-manager
      name: '@deepseek-ai/dsh-plugin-manager/tools'
      disabled: true

    - id: plugin-manager
      name: '@deepseek-ai/dsh-plugin-manager'
      disabled: !!js "!ctx.get('profileContext')"

    - id: timer
      name: '@deepseek-ai/cordis-plugin-timer'

    # Profile configuration reloads by default; module roots are opt-in.
    - id: hmr
      name: '@deepseek-ai/dsh-hmr'
      disabled: !!js "!ctx.get('profileContext')"
      config:
        root: []

    - id: llm
      name: '@deepseek-ai/dsh-llm'
```

`dsh/node_modules/@deepseek-ai/dsh-web-app/cordis.patch.yml`（**非 insert 的 id 定向覆盖** + config 里用 `!!js`）：

```yaml
- id: tools
  config:
    # TEMPORARY workaround: DSH_TOOLS_MODE (native|ptc|both) opts a whole dsh
    # process into PTC mode ...
    mode: !!js process.env.DSH_TOOLS_MODE
```

```yaml
- insert:
    - id: desktop-product-telemetry
      name: '@deepseek-ai/dsh-host-product-telemetry-otel'
      disabled: !!js "ctx.get('profileContext')?.name !== 'desktop'"
      config:
        scheduledDelayMillis: 30000
        serviceName: deepseek-harness-desktop
        serviceVersion: !!js process.env.DSH_CLIENT_VERSION
        endpoint: !!js process.env.DSH_PRODUCT_ANALYTICS_OTLP_URL
```

**G. 最小 `cordis.patch.yml`（只挂一个插件行）** — 来自 `references/host-plugin.md`

```yaml
- insert:
    - id: my-plugin
      name: '@local/my-plugin'
      config: {}
```

**H. 最小 bundle 的 package.json** — 同文件

```json
{
  "name": "@local/my-plugin",
  "version": "1.0.0",
  "private": true,
  "type": "module",
  "exports": { ".": "./index.js" },
  "dsh": { "bundle": { "patch": "./cordis.patch.yml" } }
}
```

**I. 安装方式** — `.../cordis-plugin-development/SKILL.md`

```text
For implementation, use ordinary workspace files to author a bundle, then `plugin_manager` with
`action: install_bundle` and the absolute package directory as `target` to install it in the current profile.
Changes affect every session in that profile and survive restart.
```

```text
Do not write the profile's `package.json` or `cordis.patch.yml`, create packages under `$DSH_HOME`,
or run pnpm in the profile directory: `install_bundle` performs those steps
```

`dsh-plugin-manager` 对非 bundle 依赖的行为 — `dsh/node_modules/@deepseek-ai/dsh-plugin-manager/lib/types/operations.js`：

```js
        options.onOutput?.(`dsh: warning: ${name} declares no dsh.bundle — installed as a plain dependency, not a profile layer\n`, 'stderr');
```

判定「是不是 bundle」只看这一个字段 — 同文件：

```js
    return manifest.dsh?.bundle?.patch === undefined ? undefined : manifest;
```

**J. `id` 的语义**（有确凿证据）

- `id` 是**行在 entry tree 内的稳定标识**，用于被后续 patch 层按 id 覆盖/插入：
  `const target = entryMap.get(id)`（`applyEntryPatches`）；`EntryTree.resolve(id)` 也按 id 解析（支持 `:` 分隔嵌套）。
- 同一 patch 列表里，前一个 patch `insert` 的行**可以被后一个 patch 定向**：
  `// Index what this patch added so a LATER patch in the same list can target it.`
- `id` **可省略**，Loader 会随机生成：
  `ensureId(options) { if (!options.id) { do { options.id = Math.random().toString(16).slice(2, 10) } while (this.store[options.id]) } ... }`
- 非 `insert` 的 patch **必须**有 `id`，否则 `warn('patch: id is required for non-insert patches')`。

**K. 显示元数据（可选）** — `references/host-plugin.md`

```json
{ "meta": { "title": "My Decoration", "description": "Draws a badge under the composer." } }
```

```json
{
  "icon": "./icon.svg",
  "exports": { "./package.json": "./package.json", "./locale/*.json": "./locale/*.json" },
  "files": ["locale/*.json", "icon.svg"]
}
```

`locale/en.json` 里放 title/description；缺失时回退到 `package.json` 的 `name` / `description`。

### 最小示例

见第 7 节（完整三文件 bundle）。

---

## 5. 日志

### 结论

`ctx.logger` 是一个「可调用服务」：**`ctx.logger.info(...)` 用 fiber 名直接记**，
**`ctx.logger('my-name')` 返回一个命名 logger**。可用级别恰好 4 个：`error` / `info` / `warn` / `debug`。
参数是 printf 风格，支持 `%s %d %i %f %o %O %c %C`（`%C` 用该 logger 的名字着色）。

### 证据

`dsh/node_modules/@deepseek-ai/cordis/src/logger.ts`

```ts
/** Logger method name and severity category. */
export type LoggerType = 'error' | 'info' | 'warn' | 'debug'

/** Callable shape for one logger severity method. */
export type LoggerMethod = (format: any, ...param: any[]) => void
```

```ts
/** Built-in placeholder formatters used by `Logger.format()`. */
export const defaultFormatters: Record<string, Formatter> = {
  s: (value) => String(value),
  d: (value) => Math.trunc(Number(value)),
  i: (value) => Math.trunc(Number(value)),
  f: (value) => Number(value),
  o: (value) => JSON.stringify(value),
  O: (value) => JSON.stringify(value),
  c: () => '',
  C: (value, exporter, message) => {
    return Logger.color(exporter, Logger.code(message.name, exporter.colors), value)
  },
}
```

```ts
/** Callable `ctx.logger` service shape. */
export interface LoggerService extends Record<LoggerType, LoggerMethod> {
  (name?: string): Logger
}

/**
 * Built-in logging service.
 *
 * Call `ctx.logger()` to create a named logger, or call `ctx.logger.info()`
 * directly to log with the current fiber-derived name.
 */
```

```ts
  [symbols.invoke](name?: string): Logger {
    const config = this._resolveConfig()
    const fiber = ((this.ctx as any)[symbols.shadow] ?? this.ctx).fiber
    name ??= config.name
    name ??= hyphenate(fiber.name)
    return new Logger({
      name,
      level: config.level,
      meta: { fiber: new WeakRef(fiber) },
    }, this)
  }
```

`Error` 实例会被特殊处理（打印 stack，并透传 `cause` / `AggregateError`）：

```ts
  private _method(type: LoggerType, level: number): LoggerMethod {
    return (...args: any[]) => {
      if (args.length === 1 && args[0] instanceof Error) {
        if (args[0].cause) {
          this[type](args[0].cause)
        } else if (isAggregateError(args[0])) {
          args[0].errors.forEach(error => this[type](error))
          return
        }
      }
```

真实用法（带文件路径）：

```js
// dsh/node_modules/@deepseek-ai/cordis-plugin-loader/lib/index.js
	this.ctx.root.logger?.("loader").info("%s plugin %C", type, entry.options.name);
```

```js
// dsh/node_modules/@deepseek-ai/dsh-llm-pi-ai/lib/index.js
		onReplayDegrade: ({ provider, model, reason }) => {
			ctx.logger.warn(`llm-pi-ai: unusable replay state on assistant history for route "${provider}/${model}"; sending that message as provider-neutral content (${reason})`);
		}
```

```js
// dsh/node_modules/@deepseek-ai/dsh-commands/lib/index.js
			} catch (appendError) {
				this.ctx.logger.warn(`command "${command}": command/done append failed: ${renderThrown(appendError)}`);
			}
```

```js
// dsh/node_modules/@deepseek-ai/cordis-plugin-include/src/index.ts
      this.ctx.logger.warn('config update at %C failed', this.filename)
      this.ctx.logger.warn(error)
```

### 最小示例

```js
ctx.logger.info('started')                       // 用 fiber 名
ctx.logger('my-plugin').warn('slow path %d ms', ms)
ctx.logger.debug('%o', someObject)
ctx.logger.error(new Error('boom'))
```

---

## 6. 凭证读取

### 结论

两条路：

1. **只要一个固定环境变量** → 直接 `process.env.X`（或 `launchEnvironmentOf(ctx).get(name)` 读「启动时的快照」）。
2. **走 DSH 凭证存储** → `await ctx.credentials.resolve(credentialRef('MY_KEY'))` 拿 `{ value, source } | undefined`；
   配置里**只写引用名**（如 LLM 路由的 `apiKeyEnv: DEEPSEEK_API_KEY`），由服务在每次请求时解析，轮换密钥无需重启。

### 证据

**A. README 的 API 一览** — `dsh/node_modules/@deepseek-ai/dsh-credentials/README.md`

```ts
import type { Context } from '@deepseek-ai/cordis'
import { credentialRef } from '@deepseek-ai/dsh-credentials'

declare const ctx: Context

const ref = credentialRef('DEEPSEEK_API_KEY')          // POSIX shell identifier, branded
const hit = await ctx.credentials.resolve(ref)         // { value, source } | undefined
const info = await ctx.credentials.describe(ref)       // { configured, source?, writable } — never the value
await ctx.credentials.set(ref, 'sk-…')                 // rejects while a read-only source shadows the ref
await ctx.credentials.unset(ref)                       // no-op when absent; same shadowing rule
```

```ts
import { credentialKey } from '@deepseek-ai/dsh-credentials'

const key = credentialKey('llm-pi-ai', 'openai-codex')   // <owner>/<id>, branded
const hit = await ctx.credentials.readRecord(key)        // CredentialRecord | undefined
await ctx.credentials.describeRecord(key)                // { configured, kind?, writable } — never the value
await ctx.credentials.listRecords()                      // [{ key, kind }] — never values
await ctx.credentials.modifyRecord(key, async () => ({ kind: 'grant', payload: { token: '…' } }))
await ctx.credentials.deleteRecord(key)                  // no-op when absent
```

**B. 服务名与 brand 构造** — `dsh/node_modules/@deepseek-ai/dsh-credentials/lib/index.js`

```js
var CredentialProvider = class extends Service {
	constructor(ctx) {
		super(ctx, "credentials");
	}
```

```js
function credentialRef(value) {
	if (!isCredentialRefName(value)) throw new TypeError(`credential ref "${value}" must match ${String(REF_PATTERN)}`);
	return brandString(value);
}
```

```js
function credentialKey(scope, id) {
	for (const segment of [scope, id]) if (!KEY_SEGMENT_PATTERN.test(segment)) throw new TypeError(`credential key segment "${segment}" must match ${String(KEY_SEGMENT_PATTERN)}`);
	return brandString(`${scope}/${id}`);
}
```

**C. 本地存储与优先级** — `dsh/node_modules/@deepseek-ai/dsh-credentials-local/README.md`

```markdown
Credential lookup follows a fixed precedence: the launch environment wins, followed by the stored file,
the project's `.env`, and the harness-home `.env`
```

| Place | Writable? | Wins over |
|---|---|---|
| The environment you launched in (`DEEPSEEK_API_KEY=… dsh`) | no | everything |
| The stored file | yes (`set`/`unset`) | both `.env` files |
| Your project's `.env` (`<invocation cwd>/.env`) | not here | your home `.env` |
| Your home `.env` (`$DSH_HOME/.env`) | not here | nothing |

配置（默认 `<harness home>/.credentials.yaml`，由 `dsh-base` 组合加载）：

```yaml
- name: '@deepseek-ai/dsh-credentials-local'
  config:
    path: /absolute/path/to/.credentials.yaml
```

**D. `apiKeyEnv` 到底怎么解析** — `dsh/node_modules/@deepseek-ai/dsh-llm-pi-ai/lib/index.js:2564`

```js
	const resolveApiKey = async (provider, profile) => {
		const ref = profile.apiKeyEnv;
		if (ref === void 0) return void 0;
		const credentials = ctx.get("credentials");
		const hit = credentials !== void 0 ? (await credentials.resolve(ref))?.value : launchEnvironmentOf(ctx).get(ref)?.value;
		if (hit !== void 0 && hit.length > 0) return assertUsableApiKey(hit, "llm-pi-ai", ref);
		throw new LlmError(`llm-pi-ai: no credential for provider route "${provider}"; its profile resolves ${ref}, which is not set — store ${ref} through the credentials service (the web Models page writes it) or export it, and remove apiKeyEnv only if this provider should authenticate from pi-ai's own environment discovery`, "MISSING_CREDENTIAL");
	};
```

配置字段被 brand 化（`dsh-llm-pi-ai/lib/index.js:1019` / `:1140`）：

```js
const profile = z.object({
	apiKeyEnv: z.string().role("credential-ref"),
```

```js
			...apiKeyEnv === void 0 ? {} : { apiKeyEnv: credentialRef(apiKeyEnv) },
```

pi-ai 的全局 env 兜底（`lib/index.js:2088`）：

```js
		async env(name) {
			if (isCredentialRefName(name)) {
				const hit = await ctx.get("credentials")?.resolve(credentialRef(name));
				if (hit !== void 0) return hit.value;
			}
			return launchEnvironmentOf(ctx).get(name)?.value;
		},
```

**E. 「启动环境快照」是什么** — `dsh/node_modules/@deepseek-ai/dsh-launch-environment/lib/index.js:64`

```js
function launchEnvironmentOf(ctx) {
	return ctx.get("launchEnvironment") ?? createLaunchEnvironmentSnapshot([{
		source: "process",
		values: process.env
	}]);
}
```

> 注意：这是**启动时**的快照（或 `process.env` 兜底），启动后再 export 的变量不会被看到。

### 最小示例

```js
import { credentialRef } from '@deepseek-ai/dsh-credentials'

export const inject = ['credentials']
export const Config = z.object({ apiKeyEnv: z.string().role('credential-ref').default('MY_API_KEY') })

export function apply(ctx, config) {
  async function apiKey() {
    const credentials = ctx.get('credentials')
    const ref = credentialRef(config.apiKeyEnv)
    const hit = credentials !== void 0
      ? await credentials.resolve(ref)
      : undefined
    return hit?.value
  }
  // ...
}
```

---

## 7. 完整最小插件（bundle：`package.json` + `cordis.patch.yml` + `lib/index.js`）

已落到工作区：`dsh-cordis-plugin-report/hello-plugin/`（`node --check` 通过，JSON 合法）。

### `hello-plugin/package.json`

```json
{
  "name": "@local/dsh-hello-plugin",
  "version": "1.0.0",
  "private": true,
  "type": "module",
  "description": "Minimal DSH bundle registering a /hello slash command",
  "exports": {
    ".": "./lib/index.js",
    "./package.json": "./package.json"
  },
  "files": [
    "lib/index.js",
    "cordis.patch.yml"
  ],
  "dsh": {
    "bundle": {
      "patch": "./cordis.patch.yml"
    }
  }
}
```

### `hello-plugin/cordis.patch.yml`

```yaml
# Minimal bundle patch: one Host row pointing at this package's plugin entry.
- insert:
    - id: dsh-hello-plugin
      name: '@local/dsh-hello-plugin'
      config: {}
```

### `hello-plugin/lib/index.js`

```js
/**
 * Minimal DSH Host plugin: registers the human slash command `/hello`.
 *
 * No UI code, no Client half, no model message: the command runs directly
 * against the receiving agent and its `text` is rendered by whichever
 * interactive adapter (CLI / Web) is composed.
 *
 * Named exports (not a default export) are what the Cordis Loader reads:
 *   - `name`   display/logger name of the fiber
 *   - `inject` services that must exist before `apply` runs
 *   - `apply`  the plugin body, called as apply(ctx, config)
 */
export const name = 'dsh-hello-plugin'

/** Hold the plugin inactive until the human-command registry exists. */
export const inject = ['commands']

/**
 * @param {import('@deepseek-ai/cordis').Context} ctx
 */
export function apply(ctx) {
  // `ctx.commands.register()` returns a disposer and is already attached to
  // this fiber's effect scope, so it is unregistered automatically on unload.
  ctx.commands.register({
    // Stable, plugin-namespaced identity for adapters (optional but recommended).
    definitionId: '@local/dsh-hello-plugin/hello',
    name: 'hello',                       // -> /hello ; must match /^[a-z][a-z0-9_-]*$/
    description: 'Say hello',            // required, non-empty
    input: { hint: '[name]' },           // optional; enables "/hello <name>"
    handler: ({ rawInput }) => {
      const who = rawInput.trim() || 'world'
      // success -> optional `text`; error -> mandatory non-empty `text`.
      return { kind: 'success', text: `Hello, ${who}!` }
    },
  })

  ctx.logger.info('dsh-hello-plugin: registered /hello')
}
```

### 安装与验证

按 `cordis-plugin-development` 技能的官方流程（**不要**手写 profile 的 `package.json` / 跑 pnpm）：

1. 把 `hello-plugin/` 放在工作区；
2. 调 `plugin_manager`，`action: "install_bundle"`，`target` 为该目录的**绝对路径**；
3. 用 `cordis_inspect_query`（`Config.listConfigs` / `Service`）确认新行已挂载，再在 UI 里输入 `/hello`。

---

## 8. 未找到证据的点

- **`ctx.commands` 的 Client 侧注册 API**：不存在。`register` 未标 `@Remote`，Client 只有 `list` / `execute` 的远程代理。
- **命令的富 UI（表单 / 补全 / 富文本）**：README 明确说这不属于该注册表 ——
  `**Only unstructured text input** — forms, completion schemas, and typed arguments remain command-owned parsing concerns.`
- **`presentation.js` 里 `card` 的完整枚举**：归档内的 `dsh-tools/lib/types/presentation.js` 只是 `export {}`（纯类型，`.d.ts` 未打进 asar），
  因此 `presentCall` 的 `card` 取值集合**在 asar 内未找到确凿证据**；只在 `dsh-tool-todo` 里见到 `card: "generic"`。
- **`InferArgs` / `ValueSchemaSpec` 等 TS 类型名**：`dsh-tools` 的 `.d.ts` 未打包进 asar，只有 `lib/types/*.js`（运行时为空模块）。
  字段词汇表是从运行时编译代码 `ANNOTATION_KEYS` 与 `runSchemaCompiler` 反推的，已在上文给出。
- **bundle 的 `dsh.client` 字段语义**：本报告未展开（那是 Client/UI 插件范畴），
  仅证据到 `references/host-plugin.md` 与 `templates/decoration/package.json` 中出现的
  `"dsh": { "client": { "platform": "web", "immediately": true, "inject": [...] } }`。
