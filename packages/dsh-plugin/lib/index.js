/**
 * JEV Model Router — DeepSeek Harness adapter (P0 probe).
 *
 * Stage P0 is deliberately observation-only: it classifies a task through the
 * real JEV (TypeSafe System One) API and reports the judgment. It never
 * intercepts, reroutes, or delays a model request, so mounting this plugin
 * cannot change what the agent does or what a run costs.
 *
 * The module intentionally imports nothing from `@deepseek-ai/*`: the command
 * registry contract is used directly, which keeps the package installable from
 * a plain local path with no peer-resolution step.
 *
 * @module dsh-plugin-jev-router
 */

import { appendFile, mkdir } from 'node:fs/promises'
import { homedir } from 'node:os'
import { dirname, join } from 'node:path'

import { JevError, classifyJev } from './jev/classifier.js'

/** Cordis plugin name. */
export const name = 'jev-router'

/** Services this plugin consumes. */
export const inject = ['commands']

/**
 * Frozen stage-1 model layering, mirroring `config/models.real-pilot.yaml`.
 *
 * P0 reports this as a *reference* suggestion only; the real Policy Engine
 * (task-type filtering, price ordering, budget guard, fallback chain) is not
 * part of this stage.
 */
export const DEFAULT_MODEL_LAYERS = Object.freeze({
  low: 'glm-5.3-flash',
  medium: 'qwen3.8-flash',
  high: 'gpt-5.6-luna',
})

/** Default decision-log location, outside any frozen benchmark results path. */
export const DEFAULT_DECISION_LOG = join(
  homedir(),
  '.dsh',
  'jev-router',
  'decisions.jsonl',
)

const USAGE =
  '用法：/jev <任务描述>；也可直接 /jev 让 JEV 判断你最近一条消息。'

/**
 * Register the `/jev` command.
 *
 * @param ctx - the Cordis context carrying the command registry.
 * @param config - raw plugin config supplied by the profile patch.
 */
export function apply(ctx, config = {}) {
  const settings = resolveSettings(config)

  ctx.commands.register({
    definitionId: 'dsh-plugin-jev-router',
    name: 'jev',
    description: '用 JEV 判断当前任务类型、难度和所需模型能力（不改变本次请求）',
    input: { hint: '[任务描述]' },
    recordInput: false,
    handler: (invocation) => runJevCommand(invocation, settings),
  })

  ctx.logger?.info(
    'jev-router (P0 probe) ready: /jev registered; key file %s',
    settings.apiKeyFile ? 'configured' : 'NOT configured',
  )
}

/**
 * Run one `/jev` invocation.
 *
 * @param invocation - the command invocation handed over by the registry.
 * @param settings - resolved plugin settings.
 * @returns a `CommandResult` carrying either the judgment or a usage error.
 */
async function runJevCommand(invocation, settings) {
  const rawInput = typeof invocation?.rawInput === 'string' ? invocation.rawInput.trim() : ''
  const session = invocation?.agent?.session
  const task = rawInput.length > 0 ? rawInput : latestUserText(session)

  if (task.length === 0) {
    return { kind: 'error', text: `没有可判断的任务文本。${USAGE}` }
  }
  if (!settings.apiKeyFile) {
    return {
      kind: 'error',
      text:
        '未配置 JEV 密钥文件。请在 profile 的 cordis.patch.yml 里给 jev-router 行设置 config.apiKeyFile，或设置 JEV_API_KEY_FILE 环境变量。',
    }
  }

  let outcome
  try {
    outcome = await classifyJev({
      prompt: task,
      apiKeyFile: settings.apiKeyFile,
      timeoutMs: settings.timeoutMs,
    })
  } catch (error) {
    if (error instanceof JevError) {
      return { kind: 'error', text: `JEV 判断失败：${error.code} — ${error.message}` }
    }
    // A command handler must never throw at the user: an unexpected failure is
    // reported as an ordinary error result.
    return {
      kind: 'error',
      text: `JEV 判断失败：${
        error instanceof Error ? error.message : String(error)
      }`,
    }
  }

  const { classifier, metrics } = outcome
  const inputSource = rawInput.length > 0 ? '命令参数' : '最近一条消息'
  const reference = settings.modelLayers[classifier.required_capability] ?? '（未配置）'

  // Awaited on purpose: a decision that is reported to the user must also be
  // on disk, so the evidence log can never silently lag the answer.
  await recordDecision(settings.decisionLog, {
    at: new Date().toISOString(),
    source: inputSource,
    task_chars: task.length,
    classifier,
    metrics,
    reference_model: reference,
  })

  const lines = [
    `JEV 判定（${metrics.returned_model}｜${metrics.latency_ms}ms｜$${metrics.exact_cost.toFixed(6)}｜输入：${inputSource}）`,
    '',
    `任务类型：${classifier.task_type}`,
    `难度：${classifier.difficulty_score} / 10（${classifier.difficulty_bucket}）`,
    `所需能力：${classifier.required_capability}`,
    `风险级别：${classifier.risk_level}`,
    `置信度：${classifier.confidence}`,
    '',
    `参考模型（按已冻结的三层配置，未经完整 Policy）：${reference}`,
    '',
    '说明：本命令只做判断，不会改变本次请求使用的模型。',
  ]

  return { kind: 'success', text: lines.join('\n') }
}

/**
 * Resolve plugin settings from raw config, environment, and defaults.
 *
 * @param config - raw config object from the profile patch.
 * @returns the normalized settings.
 */
function resolveSettings(config) {
  const raw = typeof config === 'object' && config !== null ? config : {}
  const envKeyFile =
    process.env.JEV_API_KEY_FILE ?? process.env.TYPESAFE_API_KEY_FILE ?? ''

  const modelLayers = { ...DEFAULT_MODEL_LAYERS }
  if (typeof raw.modelLayers === 'object' && raw.modelLayers !== null) {
    for (const [key, value] of Object.entries(raw.modelLayers)) {
      if (typeof value === 'string' && value.length > 0) modelLayers[key] = value
    }
  }

  const timeoutMs =
    typeof raw.timeoutMs === 'number' && Number.isFinite(raw.timeoutMs) && raw.timeoutMs > 0
      ? raw.timeoutMs
      : 30_000

  return {
    apiKeyFile:
      typeof raw.apiKeyFile === 'string' && raw.apiKeyFile.length > 0
        ? raw.apiKeyFile
        : envKeyFile || null,
    decisionLog:
      typeof raw.decisionLog === 'string' && raw.decisionLog.length > 0
        ? raw.decisionLog
        : DEFAULT_DECISION_LOG,
    modelLayers,
    timeoutMs,
  }
}

/**
 * Extract the most recent user-authored text from a session.
 *
 * @param session - the invoking agent's session, when it has one.
 * @returns the text, or an empty string when none is available.
 */
function latestUserText(session) {
  if (session === undefined || typeof session.deriveMessages !== 'function') return ''
  let messages
  try {
    messages = session.deriveMessages()
  } catch {
    return ''
  }
  if (!Array.isArray(messages)) return ''

  for (let index = messages.length - 1; index >= 0; index -= 1) {
    const message = messages[index]
    if (message?.role !== 'user' || !Array.isArray(message.content)) continue
    const parts = []
    for (const block of message.content) {
      if (block?.type === 'text' && typeof block.text === 'string') parts.push(block.text)
    }
    const text = parts.join('\n').trim()
    if (text.length > 0) return text
  }
  return ''
}

/**
 * Append one decision record to the JSONL log.
 *
 * Logging is best-effort: a logging failure must never fail the command, and
 * no secret is ever written — only the classifier result and safe metrics.
 *
 * @param path - target JSONL path.
 * @param record - the decision record.
 */
async function recordDecision(path, record) {
  try {
    await mkdir(dirname(path), { recursive: true })
    await appendFile(path, `${JSON.stringify(record)}\n`, 'utf8')
  } catch (error) {
    // Intentionally silent: observability must not break the command.
    void error
  }
}
