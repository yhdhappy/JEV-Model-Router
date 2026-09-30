/**
 * Auto-route: make "自动选择" a selectable model and route each user turn.
 *
 * Design decisions worth stating explicitly:
 *
 * 1. **The `llm/stream` waterfall does the rerouting, not the adapter.**
 *    `LlmRuntime.adapterStream()` runs `forAdapter()` before dispatch, which
 *    strips `source.replayState` from history owned by another adapter. A
 *    synthetic adapter that re-dispatches therefore loses replay state; a
 *    waterfall listener sees the untouched request and re-dispatches straight
 *    to the real adapter, so replay state survives. The same listener is also
 *    the last point that can still see the original provider/model.
 *
 * 2. **The adapter exists only so the picker can show `auto`.** `listModels`
 *    and `resolveModel` are required for catalog-driven entry points; the
 *    adapter's own `stream` is a fallback path for dispatch that bypasses the
 *    waterfall.
 *
 * 3. **One classification per user turn, not per request.** An agent turn is
 *    many provider requests (tool round-trips included). Classifying every
 *    request would tax every step and switch models mid-turn, breaking cache
 *    reuse and replay continuity. The chosen route is held for the rest of the
 *    turn and re-decided only when a new user message arrives.
 *
 * 4. **Routing only; no fallback chain and no budget guard yet.** Model
 *    fallback and Budget Guard stay with the host's existing retry path in
 *    this stage, because switching models after a stream has emitted chunks is
 *    not a safe local decision.
 *
 * 5. **Fail safe, never fail closed.** A JEV failure falls back to a
 *    configured safe default — never to the cheapest tier — matching the
 *    frozen stage-1 rule. The user's turn is never blocked by this plugin.
 *
 * @module dsh-plugin-jev-router/llm/auto-route
 */

import { JevError, classifyJev } from '../jev/classifier.js'
import { NoEligibleModelError, decideRoute } from '../jev/policy.js'
import {
  AUTO_CONTEXT_WINDOW,
  AUTO_MODEL,
  AUTO_MODEL_NAME,
  AUTO_PROVIDER,
} from '../jev/models.js'

/** Text blocks of one message, concatenated. */
function messageText(message) {
  if (!Array.isArray(message?.content)) return ''
  const parts = []
  for (const block of message.content) {
    if (block?.type === 'text' && typeof block.text === 'string') parts.push(block.text)
  }
  return parts.join('\n').trim()
}

/** The most recent user-authored text in a request, if any. */
export function latestUserText(messages) {
  if (!Array.isArray(messages)) return ''
  for (let index = messages.length - 1; index >= 0; index -= 1) {
    if (messages[index]?.role !== 'user') continue
    const text = messageText(messages[index])
    if (text.length > 0) return text
  }
  return ''
}

/**
 * Whether this request opens a new user turn.
 *
 * A turn's first request ends with the user message; every later step of the
 * same turn ends with an assistant tool call or a tool result.
 *
 * @param messages - the request's message list.
 * @returns true when the last message is user-authored.
 */
export function isNewUserTurn(messages) {
  if (!Array.isArray(messages) || messages.length === 0) return false
  return messages[messages.length - 1]?.role === 'user'
}

/**
 * Register the `auto` catalog entry and the rerouting listener.
 *
 * @param ctx - the Cordis context.
 * @param settings - resolved plugin settings.
 * @param deps - injected collaborators: `{ log, recordDecision }`.
 * @returns nothing; registrations are owned by the plugin fiber.
 */
export function registerAutoRoute(ctx, settings, deps) {
  const { log, recordDecision, classify = classifyJev } = deps
  const state = {
    heldRoutes: new Map(),
    cache: new Map(),
  }

  const adapter = {
    providerInfo(provider) {
      return { id: provider, name: 'JEV Model Router' }
    },
    providerRetryPolicy() {
      return undefined
    },
    imageRequestPricing() {
      return undefined
    },
    async listModels(provider) {
      // `inputModalities` must include image: the real routes accept images,
      // and a text-only declaration would make the runtime replace every
      // image with a placeholder before this plugin ever sees the request.
      return [
        {
          provider,
          id: AUTO_MODEL,
          name: AUTO_MODEL_NAME,
          description: '由 JEV 判断任务类型与难度后自动选择模型',
          inputModalities: ['text', 'image'],
        },
      ]
    },
    async resolveModel(provider, model) {
      return {
        provider,
        id: model,
        name: AUTO_MODEL_NAME,
        inputModalities: ['text', 'image'],
        context: { contextWindow: AUTO_CONTEXT_WINDOW },
      }
    },
    async prepareCall(provider, model, signal) {
      return {
        model: await adapter.resolveModel(provider, model, signal),
        stream: (options) => routeStream(ctx, options, settings, state, deps, 'adapter'),
      }
    },
    stream(options) {
      return routeStream(ctx, options, settings, state, deps, 'adapter')
    },
  }

  ctx.llm.registerAdapter([AUTO_PROVIDER], adapter)

  // The primary path: intercept before adapter dispatch so history keeps its
  // replay state, and so the untouched request is still visible here.
  ctx.on('llm/stream', (options, next) => {
    if (options?.provider !== AUTO_PROVIDER) return next()
    return routeStream(ctx, options, settings, state, deps, 'waterfall')
  })

  log.info(
    'jev-router: "auto" (%s) registered on provider "%s"; safe default %s/%s',
    AUTO_MODEL,
    AUTO_PROVIDER,
    settings.safeDefault.provider,
    settings.safeDefault.model,
  )
}

/**
 * Resolve the route for one request, then forward the stream unchanged.
 *
 * @param ctx - the Cordis context.
 * @param options - the original call options.
 * @param settings - resolved plugin settings.
 * @param state - per-fiber turn state.
 * @param deps - injected collaborators.
 * @param origin - which dispatch path invoked this, for the record.
 * @returns an async iterable of the inner call's chunks.
 */
async function* routeStream(ctx, options, settings, state, deps, origin) {
  const { log, recordDecision } = deps
  let decision
  try {
    decision = await resolveRoute(options, settings, state, deps)
  } catch (error) {
    log.warn('jev-router: routing failed (%s); using the safe default', describe(error))
    decision = {
      source: 'safe_default',
      provider: settings.safeDefault.provider,
      model: settings.safeDefault.model,
      reason: [`routing failed: ${describe(error)}`],
      error: describe(error),
    }
  }

  const forwarded = { ...options, provider: decision.provider, model: decision.model }
  if (forwarded.reasoningEffort === undefined) delete forwarded.reasoningEffort

  await recordDecision(settings.decisionLog, {
    at: new Date().toISOString(),
    kind: 'route',
    origin,
    provider: decision.provider,
    model: decision.model,
    route_source: decision.source,
    reused: decision.reused === true,
    classifier: decision.classifier ?? null,
    answer_confidences: decision.answerConfidences ?? null,
    jev_cost: decision.jevCost ?? null,
    jev_latency_ms: decision.jevLatencyMs ?? null,
    reason: decision.reason ?? [],
    error: decision.error ?? null,
  })

  yield* ctx.llm.stream(forwarded)
}

/**
 * Decide which real route this request should use.
 *
 * @param options - the original call options.
 * @param settings - resolved plugin settings.
 * @param state - per-fiber turn state.
 * @param deps - injected collaborators.
 * @returns the decision, including whether a held route was reused.
 */
async function resolveRoute(options, settings, state, deps) {
  const messages = options?.messages ?? []
  const sessionId = typeof options?.sessionId === 'string' ? options.sessionId : undefined
  const opening = isNewUserTurn(messages)

  if (!opening && sessionId !== undefined) {
    const held = state.heldRoutes.get(sessionId)
    if (held !== undefined) return { ...held, reused: true }
  }

  const task = latestUserText(messages)
  if (task.length === 0) {
    const held = sessionId !== undefined ? state.heldRoutes.get(sessionId) : undefined
    if (held !== undefined) return { ...held, reused: true }
    return {
      source: 'safe_default',
      provider: settings.safeDefault.provider,
      model: settings.safeDefault.model,
      reason: ['no user text in the request; nothing to classify'],
    }
  }

  const classified = await classifyCached(task, settings, state, deps.classify ?? classifyJev)
  let policy
  try {
    policy = decideRoute(classified.classifier, settings.models)
  } catch (error) {
    if (error instanceof NoEligibleModelError) {
      deps.log.warn('jev-router: %s', error.message)
      return {
        source: 'safe_default',
        provider: settings.safeDefault.provider,
        model: settings.safeDefault.model,
        classifier: classified.classifier,
        answerConfidences: classified.answer_confidences,
        reason: [error.message],
        error: 'no_eligible_model',
      }
    }
    throw error
  }

  const decision = {
    source: 'jev',
    provider: policy.primary.provider,
    model: policy.primary.model,
    classifier: classified.classifier,
    answerConfidences: classified.answer_confidences,
    jevCost: classified.metrics.exact_cost,
    jevLatencyMs: classified.metrics.latency_ms,
    reason: policy.reason,
    fallback: policy.fallbacks.map((entry) => `${entry.provider}/${entry.model}`),
  }

  if (sessionId !== undefined) state.heldRoutes.set(sessionId, decision)
  return decision
}

/**
 * Classify one task, reusing a recent identical classification.
 *
 * @param task - the task text.
 * @param settings - resolved plugin settings.
 * @param state - per-fiber turn state.
 * @param classify - the classifier call to use.
 * @returns the classifier outcome.
 */
async function classifyCached(task, settings, state, classify) {
  const cached = state.cache.get(task)
  if (cached !== undefined) return cached

  const outcome = await classify({
    prompt: task,
    apiKeyFile: settings.apiKeyFile,
    timeoutMs: settings.timeoutMs,
  })

  state.cache.set(task, outcome)
  // Bounded: the cache exists to absorb repeated identical turns, not to grow.
  if (state.cache.size > settings.cacheSize) {
    const oldest = state.cache.keys().next().value
    state.cache.delete(oldest)
  }
  return outcome
}

/**
 * Render any thrown value as a short, secret-free description.
 *
 * @param error - the thrown value.
 * @returns a stable description.
 */
function describe(error) {
  if (error instanceof JevError) return `${error.code}: ${error.message}`
  if (error instanceof Error) return `${error.name}: ${error.message}`
  return String(error)
}
