/**
 * Auto-route: make "自动选择" a selectable model and route each user turn.
 *
 * Design decisions worth stating explicitly:
 *
 * 1. **The `llm/stream` waterfall does the rerouting, not the adapter.**
 *    `LlmRuntime.adapterStream()` projects the request (file handles, image
 *    placeholders, tool-update declarations) against the *target adapter's*
 *    model metadata before dispatch. Reaching the real model through the
 *    synthetic adapter would therefore project twice — the first pass using
 *    the synthetic model's metadata, which is not the model that will actually
 *    run. A waterfall listener sees the untouched request, so the inner call
 *    projects exactly once, against the real model. It also leaves history the
 *    user produced by selecting the real provider directly eligible for replay.
 *
 *    Caveat, verified in `dsh-agent-loop`: history written by a routed turn
 *    records `source.provider = "jev-router"`, and `forAdapter()` strips replay
 *    state whose `source.provider` belongs to another adapter. So replay state
 *    for routed history is dropped on the inner dispatch under *either*
 *    design; only rewriting each message's `source` could preserve it, which
 *    P1 deliberately does not do. The cost is provider-native fidelity, not
 *    correctness.
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
 * 6. **The decision log is the only record of the real model.** A rewrite at
 *    the `llm/stream` layer never reaches the session log, so `model/selection`
 *    and `assistant/message.source` keep saying `jev-router/auto`. This log is
 *    therefore load-bearing, not a convenience.
 *
 * @module dsh-plugin-jev-router/llm/auto-route
 */

import { JevError, classifyJev } from '../jev/classifier.js'
import { NoEligibleModelError, decideRouteForSettings } from '../jev/policy.js'
import { classifierFromRule, evaluateLightweightRules } from '../jev/rules.js'
import {
  BUDGET_LIMIT_REACHED,
  BudgetLedger,
  checkBudget,
  estimateMaxCost,
  turnKey,
} from './budget.js'
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
    budget: new BudgetLedger({ maxTurns: settings.budgetMaxTurns }),
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

  // Budget Guard sits on the last boundary before dispatch. Returning here
  // without calling `next()` means the provider request is never made, which is
  // what makes this a spending-prevention mechanism rather than an accounting
  // one. The classifier's own cost is charged to the same turn account.
  const guard = applyBudgetGuard(options, settings, state, decision)

  const forwarded = { ...options, provider: decision.provider, model: decision.model }
  if (forwarded.reasoningEffort === undefined) delete forwarded.reasoningEffort

  await recordDecision(settings.decisionLog, {
    at: new Date().toISOString(),
    kind: 'route',
    origin,
    // Whether this request carried a session id decides whether the per-turn
    // hold can work at all; recorded so a field run can prove it either way.
    session_scoped: typeof options?.sessionId === 'string',
    provider: decision.provider,
    model: decision.model,
    route_source: decision.source,
    reused: decision.reused === true,
    relaxed_task_type: decision.relaxedTaskType === true,
    rule_id: decision.ruleId ?? null,
    // Whether JEV was actually consulted. A light-rule turn costs nothing to
    // classify, and the log must not imply otherwise.
    jev_called: decision.jevCalled !== false,
    classifier: decision.classifier ?? null,
    answer_confidences: decision.answerConfidences ?? null,
    jev_cost: decision.jevCost ?? null,
    jev_latency_ms: decision.jevLatencyMs ?? null,
    reason: decision.reason ?? [],
    error: decision.error ?? null,
    ...guard.record,
  })

  if (!guard.allowed) {
    // The provider must not be called. Emit one terminal error chunk, the same
    // shape the runtime itself uses for a refused call, and stop.
    yield {
      type: 'finish',
      reason: {
        kind: 'error',
        failure: {
          code: BUDGET_LIMIT_REACHED,
          message: `预算上限已达到：本次已累计 $${guard.record.budget_exposure_before}，` +
            `下一模型预计最高 $${guard.record.estimated_next_max_cost}，` +
            `上限 $${guard.record.budget_limit}`,
        },
      },
    }
    return
  }

  yield* ctx.llm.stream(forwarded)
}

/**
 * Evaluate the per-turn budget for the model about to be called.
 *
 * @param options - the original call options.
 * @param settings - resolved plugin settings.
 * @param state - per-fiber turn state.
 * @param decision - the resolved route.
 * @returns `{ allowed, record, turnKey }`.
 */
function applyBudgetGuard(options, settings, state, decision) {
  const limit = settings.budgetLimit
  const capability = capabilityOf(settings.models, decision.provider, decision.model)
  const estimated = capability === null
    ? 0
    : estimateMaxCost(capability, settings.estimatedMaxCosts)

  const record = {
    budget_limit: limit,
    budget_exposure_before: null,
    estimated_next_max_cost: estimated,
    budget_allowed: true,
    budget_error: null,
    // Estimated by construction: a provider's real cost is only known after it
    // answers, so the guard must reason from the configured ceiling.
    cost_estimated: true,
    cost_estimation_source: 'estimated_max',
    budget_turn_key: null,
  }

  if (limit === null || limit === undefined) {
    record.cost_estimated = false
    record.cost_estimation_source = null
    return { allowed: true, record, turnKey: null }
  }

  const key = turnKey(options?.sessionId, decision.turnText ?? '')
  const turn = state.budget.peek(key) ?? state.budget.openTurn(key)

  // The classifier is paid for once per turn, on the step that actually
  // classified. Later steps of the same turn reuse the held route, so charging
  // `jevCost` again would invent spend that never happened.
  const classifierCharge = decision.reused === true ? 0 : (decision.jevCost ?? 0)
  const exposure = turn.spent + classifierCharge

  const verdict = checkBudget(exposure, estimated, limit)
  record.budget_exposure_before = verdict.current_accumulated_cost
  record.budget_allowed = verdict.allowed
  record.budget_error = verdict.error_code
  record.budget_turn_key = key

  if (!verdict.allowed) return { allowed: false, record, turnKey: key }

  // Commit this call's ceiling into the turn account before dispatch, so a
  // later step of the same turn sees it. The provider's real cost replaces the
  // estimate after a successful call.
  state.budget.charge(key, {
    classifier: classifierCharge,
    execution: estimated,
    source: 'estimated_max',
  })
  record.budget_exposure_after = turn.spent
  record.budget_classifier_charge = classifierCharge
  return { allowed: true, record, turnKey: key }
}

/**
 * Resolve the capability tier of one configured route.
 *
 * @param models - configured model entries.
 * @param provider - the route provider.
 * @param model - the route model id.
 * @returns the tier, or null when the route is not configured.
 */
function capabilityOf(models, provider, model) {
  const entry = models.find(
    (candidate) => candidate.provider === provider && candidate.model === model,
  )
  return entry === undefined ? null : entry.capability
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
    return hold(state, sessionId, {
      source: 'safe_default',
      provider: settings.safeDefault.provider,
      model: settings.safeDefault.model,
      reason: ['no user text in the request; nothing to classify'],
    })
  }

  // The lightweight layer answers allowlisted trivial tasks without calling
  // JEV at all. The frozen design forbids paying the classifier tax on a task
  // that needs no semantic judgment, and this is also the only path that
  // removes the classifier's latency rather than just its cost.
  const rule = settings.lightRules
    ? evaluateLightweightRules(task)
    : { matched: false, rule_id: null }

  const classified = rule.matched
    ? null
    : await classifyCached(task, settings, state, deps.classify ?? classifyJev)

  const carried = rule.matched
    ? {
        ruleId: rule.rule_id,
        classifier: classifierFromRule(rule),
        answerConfidences: null,
        jevCost: null,
        jevLatencyMs: null,
        jevCalled: false,
      }
    : {
        ruleId: null,
        classifier: classified.classifier,
        answerConfidences: classified.answer_confidences,
        jevCost: classified.metrics?.exact_cost ?? null,
        jevLatencyMs: classified.metrics?.latency_ms ?? null,
        jevCalled: true,
      }

  // A safe default is a decision like any other: it must be held for the rest
  // of the turn too. Not holding it made every later step of the turn re-decide
  // (observed in the field as repeated `reused: false` records).
  const safeDefault = (extra) =>
    hold(
      state,
      sessionId,
      {
        source: 'safe_default',
        provider: settings.safeDefault.provider,
        model: settings.safeDefault.model,
        ...carried,
        ...extra,
      },
      task,
    )

  let policy
  try {
    // The configured tolerance for an undeclared task type, shared with the
    // `/jev` command so both always report the same model.
    policy = decideRouteForSettings(carried.classifier, settings.models, {
      unmatchedTaskType: settings.unmatchedTaskType,
    })
  } catch (error) {
    if (!(error instanceof NoEligibleModelError)) throw error
    deps.log.warn('jev-router: %s', error.message)
    return safeDefault({ reason: [error.message], error: 'no_eligible_model' })
  }

  return hold(
    state,
    sessionId,
    {
      source: rule.matched ? 'light_rule' : 'jev',
      provider: policy.primary.provider,
      model: policy.primary.model,
      ...carried,
      reason: rule.matched ? [rule.reason, ...policy.reason] : policy.reason,
      relaxedTaskType: policy.relaxed === true,
      fallback: policy.fallbacks.map((entry) => `${entry.provider}/${entry.model}`),
    },
    task,
  )
}

/**
 * Remember one resolved route for the rest of its turn.
 *
 * Holding the route is what keeps a turn on a single model and stops every
 * tool round-trip from paying the classifier again; it applies to fallback
 * decisions exactly as it does to a normal policy decision.
 *
 * @param state - per-fiber turn state.
 * @param sessionId - the owning session, when the caller supplied one.
 * @param decision - the decision to remember.
 * @returns the same decision, for convenient returning.
 */
function hold(state, sessionId, decision, turnText = '') {
  const held = turnText.length > 0 ? { ...decision, turnText } : decision
  if (sessionId !== undefined) state.heldRoutes.set(sessionId, held)
  return held
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
