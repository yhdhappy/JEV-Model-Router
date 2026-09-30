/**
 * JEV Model Router — DeepSeek Harness adapter.
 *
 * Two capabilities live here:
 *
 * - **`/jev` (P0, observation-only)** — classifies a task through the real JEV
 *   (TypeSafe System One) API and reports the judgment. It never touches a
 *   model request.
 * - **`自动选择` (P1)** — registers a synthetic `jev-router/auto` catalog entry
 *   so the model picker offers an automatic choice, then routes each user turn
 *   to a concrete configured model. Routing is opt-in: nothing changes until
 *   the user selects it.
 *
 * The module intentionally imports nothing from `@deepseek-ai/*`: the command,
 * tool, and LLM service contracts are used directly, which keeps the package
 * installable from a plain local path with no peer-resolution step.
 *
 * @module dsh-plugin-jev-router
 */

import { appendFile, mkdir } from 'node:fs/promises'
import { homedir } from 'node:os'
import { dirname, join } from 'node:path'

import { JevError, classifyJev } from './jev/classifier.js'
import { AUTO_PROVIDER, DEFAULT_MODELS } from './jev/models.js'
import { NoEligibleModelError, decideRouteForSettings } from './jev/policy.js'
import { registerAutoRoute } from './llm/auto-route.js'

/** Cordis plugin name. */
export const name = 'jev-router'

/** Services this plugin consumes. */
export const inject = ['commands']

/** Default decision-log location, outside any frozen benchmark results path. */
export const DEFAULT_DECISION_LOG = join(
  homedir(),
  '.dsh',
  'jev-router',
  'decisions.jsonl',
)

/** Model used when JEV cannot decide: never the cheapest tier. */
export const DEFAULT_SAFE_DEFAULT = Object.freeze({
  provider: 'opencode-go',
  model: 'gpt-5.6-luna',
})

const USAGE =
  '用法：/jev <任务描述>；也可直接 /jev 让 JEV 判断你最近一条消息。'

/**
 * Register the `/jev` command and the automatic model route.
 *
 * @param ctx - the Cordis context carrying the command and LLM registries.
 * @param config - raw plugin config supplied by the profile patch.
 */
export function apply(ctx, config = {}) {
  const settings = resolveSettings(config)
  const logger = ctx.logger ?? { info() {}, warn() {} }

  ctx.commands.register({
    definitionId: 'dsh-plugin-jev-router',
    name: 'jev',
    description: '用 JEV 判断当前任务类型、难度和所需模型能力（不改变本次请求）',
    input: { hint: '[任务描述]' },
    recordInput: false,
    handler: (invocation) => runJevCommand(invocation, settings),
  })

  if (settings.autoRoute) {
    // Wait for the LLM service rather than racing plugin activation order:
    // `ctx.inject` starts a child fiber that stays pending until `llm` exists,
    // so `自动选择` is never silently skipped when the service mounts later.
    // `/jev` above keeps working either way.
    ctx.inject(['llm'], (llmCtx) => {
      registerAutoRoute(llmCtx, settings, {
        log: logger,
        recordDecision,
      })
    })
  }

  logger.info(
    'jev-router ready: /jev registered; auto route %s; key file %s',
    settings.autoRoute ? 'requested' : 'disabled',
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

  const { classifier, metrics, answer_confidences: perAnswer = {} } = outcome
  const inputSource = rawInput.length > 0 ? '命令参数' : '最近一条消息'

  // Run the same decider the auto route uses, so the command shows what would
  // actually be selected rather than a second, divergent guess.
  let reference = '（无可用模型）'
  let referenceDetail = null
  try {
    const decision = decideRouteForSettings(classifier, settings.models, {
      unmatchedTaskType: settings.unmatchedTaskType,
    })
    reference = `${decision.primary.provider} / ${decision.primary.model}`
    referenceDetail =
      (decision.relaxed === true
        ? '任务类型无匹配，已放宽任务类型过滤、保留能力下限｜'
        : '落选原因：') +
      (decision.fallbacks.length > 0
        ? `备选 ${decision.fallbacks.map((m) => m.name).join(' → ')}`
        : '无其它合格模型')
  } catch (error) {
    referenceDetail =
      error instanceof NoEligibleModelError ? error.message : String(error)
  }

  // Awaited on purpose: a decision that is reported to the user must also be
  // on disk, so the evidence log can never silently lag the answer.
  await recordDecision(settings.decisionLog, {
    at: new Date().toISOString(),
    source: inputSource,
    task_chars: task.length,
    classifier,
    answer_confidences: perAnswer,
    metrics,
    reference_model: reference,
  })

  const show = (value) => (value === undefined ? '—' : String(value))
  const breakdown = [
    `类型 ${show(perAnswer.task_type)}`,
    `难度 ${show(perAnswer.difficulty_score)}`,
    `分档 ${show(perAnswer.difficulty_bucket)}`,
    `能力 ${show(perAnswer.required_capability)}`,
    `风险 ${show(perAnswer.risk_level)}`,
  ].join('｜')

  const zeroConfidence = Object.entries(perAnswer)
    .filter(([, value]) => value === 0)
    .map(([key]) => key)

  const lines = [
    `JEV 判定（${metrics.returned_model}｜${metrics.latency_ms}ms｜$${metrics.exact_cost.toFixed(6)}｜输入：${inputSource}）`,
    '',
    `任务类型：${classifier.task_type}`,
    `难度：${classifier.difficulty_score} / 10（${classifier.difficulty_bucket}）`,
    `所需能力：${classifier.required_capability}`,
    `风险级别：${classifier.risk_level}`,
    `置信度：${classifier.confidence}（取各项最小值，与已冻结口径一致）`,
    `各项置信度：${breakdown}`,
  ]

  if (zeroConfidence.length > 0) {
    lines.push(
      '',
      `⚠ ${zeroConfidence.join('、')} 自报置信度为 0，已把整体置信度拉到 0。` +
        '该问题项的原始概率分布可能仍有信息量，这是当前聚合口径的已知脆弱点。',
    )
  }

  lines.push(
    '',
    `参考模型（与"自动选择"同一套 Policy）：${reference}`,
  )
  if (referenceDetail !== null) lines.push(referenceDetail)
  lines.push(
    '',
    '说明：本命令只做判断，不会改变本次请求使用的模型。',
  )

  return { kind: 'success', text: lines.join('\n') }
}

/**
 * Resolve plugin settings from raw config, environment, and defaults.
 *
 * Exported for tests; the loader only reads `name`, `inject`, and `apply`.
 *
 * @param config - raw config object from the profile patch.
 * @returns the normalized settings.
 */
export function resolveSettings(config) {
  const raw = typeof config === 'object' && config !== null ? config : {}
  const envKeyFile =
    process.env.JEV_API_KEY_FILE ?? process.env.TYPESAFE_API_KEY_FILE ?? ''

  const timeoutMs =
    typeof raw.timeoutMs === 'number' && Number.isFinite(raw.timeoutMs) && raw.timeoutMs > 0
      ? raw.timeoutMs
      : 30_000

  const cacheSize =
    Number.isInteger(raw.cacheSize) && raw.cacheSize > 0 ? raw.cacheSize : 32

  const safeDefault =
    typeof raw.safeDefault === 'object' &&
    raw.safeDefault !== null &&
    typeof raw.safeDefault.provider === 'string' &&
    typeof raw.safeDefault.model === 'string' &&
    raw.safeDefault.provider !== AUTO_PROVIDER
      ? { provider: raw.safeDefault.provider, model: raw.safeDefault.model }
      : { ...DEFAULT_SAFE_DEFAULT }

  // JEV returns `other` for meta and conversational work, and the frozen
  // registry declares no model for it. Dropping the task-type filter keeps such
  // turns on a capability-appropriate model instead of the safe default.
  const unmatchedTaskType =
    raw.unmatchedTaskType === 'safe_default' ? 'safe_default' : 'capability_only'

  return {
    apiKeyFile:
      typeof raw.apiKeyFile === 'string' && raw.apiKeyFile.length > 0
        ? raw.apiKeyFile
        : envKeyFile || null,
    decisionLog:
      typeof raw.decisionLog === 'string' && raw.decisionLog.length > 0
        ? raw.decisionLog
        : DEFAULT_DECISION_LOG,
    models: resolveModels(raw.models),
    safeDefault,
    unmatchedTaskType,
    // The lightweight layer answers allowlisted trivial tasks without JEV.
    lightRules: raw.lightRules !== false,
    cacheSize,
    autoRoute: raw.autoRoute !== false,
    timeoutMs,
  }
}

/**
 * Resolve the configured model catalog.
 *
 * Entries are validated per field with a fall back to the frozen defaults, so
 * one malformed entry cannot take the whole route table down.
 *
 * @param configured - the raw `models` config value.
 * @returns the model entries used by the Policy mirror.
 */
function resolveModels(configured) {
  if (!Array.isArray(configured) || configured.length === 0) {
    return DEFAULT_MODELS.map((entry) => ({ ...entry, taskTypes: [...entry.taskTypes] }))
  }
  const resolved = []
  for (const entry of configured) {
    if (typeof entry !== 'object' || entry === null) continue
    const {
      name,
      provider,
      model,
      capability,
      taskTypes,
      input,
      output,
      enabled,
    } = entry
    if (typeof name !== 'string' || name.length === 0) continue
    if (typeof provider !== 'string' || provider.length === 0) continue
    // A model routed back into this plugin would recurse without bound.
    if (provider === AUTO_PROVIDER) continue
    if (typeof model !== 'string' || model.length === 0) continue
    if (!['low', 'medium', 'high'].includes(capability)) continue
    if (!Array.isArray(taskTypes) || taskTypes.length === 0) continue
    if (typeof input !== 'number' || !Number.isFinite(input) || input < 0) continue
    if (typeof output !== 'number' || !Number.isFinite(output) || output < 0) continue
    resolved.push({
      name,
      provider,
      model,
      capability,
      taskTypes: [...taskTypes],
      input,
      output,
      enabled: enabled !== false,
    })
  }
  return resolved.length > 0
    ? resolved
    : DEFAULT_MODELS.map((entry) => ({ ...entry, taskTypes: [...entry.taskTypes] }))
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
