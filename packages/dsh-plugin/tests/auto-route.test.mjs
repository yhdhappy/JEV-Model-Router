/**
 * Tests for the automatic model route.
 *
 * These drive the real listener and adapter against a fake Cordis context and
 * a stubbed classifier, so routing, turn scoping, and fail-safe behaviour are
 * all exercised without network access or a live harness.
 */

import assert from 'node:assert/strict'
import test from 'node:test'

import { DEFAULT_MODELS } from '../lib/jev/models.js'
import {
  isNewUserTurn,
  latestUserText,
  registerAutoRoute,
} from '../lib/llm/auto-route.js'

/** Minimal Cordis context recording what a plugin registers. */
function fakeContext() {
  const captured = { adapters: [], listeners: [], streams: [], warnings: [] }
  return {
    captured,
    llm: {
      registerAdapter(providers, adapter) {
        captured.adapters.push({ providers, adapter })
      },
      stream(options) {
        captured.streams.push(options)
        return innerStream()
      },
    },
    on(event, listener) {
      captured.listeners.push({ event, listener })
    },
    logger: {
      info() {},
      warn(...args) {
        captured.warnings.push(args)
      },
    },
  }
}

/** A stand-in for the inner provider stream. */
async function* innerStream() {
  yield { type: 'text-delta', text: 'hello' }
  yield { type: 'finish', reason: { kind: 'stop' } }
}

/** Collect every chunk of an async iterable. */
async function drain(stream) {
  const chunks = []
  for await (const chunk of stream) chunks.push(chunk)
  return chunks
}

/** Settings with everything a route needs. */
function settings(overrides = {}) {
  return {
    apiKeyFile: '/tmp/does-not-matter',
    decisionLog: '/tmp/jev-route-test.jsonl',
    models: DEFAULT_MODELS.map((entry) => ({ ...entry, taskTypes: [...entry.taskTypes] })),
    safeDefault: { provider: 'opencode-go', model: 'gpt-5.6-luna' },
    unmatchedTaskType: 'capability_only',
    lightRules: true,
    budgetLimit: null,
    estimatedMaxCosts: { low: 0.1, medium: 0.25, high: 1.0 },
    budgetMaxTurns: 64,
    cacheSize: 8,
    autoRoute: true,
    timeoutMs: 1000,
    ...overrides,
  }
}

/** A classifier stub returning one fixed judgment. */
function stubClassifier(calls, classifier = {}) {
  return async () => {
    calls.count += 1
    return {
      classifier: {
        schema_version: '0.1',
        task_type: 'debugging',
        difficulty_score: 5,
        difficulty_bucket: 'medium',
        required_capability: 'medium',
        confidence: 0.6,
        risk_level: 'medium',
        notes: null,
        ...classifier,
      },
      metrics: {
        returned_model: 'jev-1.13.0',
        request_id: 'req-test',
        input_tokens: 100,
        output_tokens: 50,
        latency_ms: 12,
        exact_cost: 0.000004,
      },
      answer_confidences: { task_type: 0.9, difficulty_score: 0.6 },
    }
  }
}

/** One user message. */
const userTurn = (text) => [{ role: 'user', content: [{ type: 'text', text }] }]

/** A continuation step inside the same turn. */
const toolStep = () => [
  { role: 'user', content: [{ type: 'text', text: 'do the thing' }] },
  { role: 'assistant', content: [{ type: 'text', text: 'ok' }] },
  { role: 'tool', content: [{ type: 'text', text: 'result' }] },
]

test('turn detection and task extraction', () => {
  assert.equal(isNewUserTurn(userTurn('hi')), true)
  assert.equal(isNewUserTurn(toolStep()), false)
  assert.equal(isNewUserTurn([]), false)
  assert.equal(latestUserText(userTurn('fix the bug')), 'fix the bug')
  assert.equal(latestUserText(toolStep()), 'do the thing')
  assert.equal(latestUserText([]), '')
})

test('the catalog entry satisfies the runtime validation rules', async () => {
  const ctx = fakeContext()
  registerAutoRoute(ctx, settings(), {
    log: ctx.logger,
    recordDecision: async () => {},
    classify: stubClassifier({ count: 0 }),
  })

  assert.equal(ctx.captured.adapters.length, 1)
  const { providers, adapter } = ctx.captured.adapters[0]
  assert.deepEqual(providers, ['jev-router'])

  const info = adapter.providerInfo('jev-router')
  assert.equal(info.id, 'jev-router')
  assert.ok(info.name.length > 0)

  const models = await adapter.listModels('jev-router')
  assert.equal(models.length, 1)
  assert.equal(models[0].provider, 'jev-router')
  assert.equal(models[0].id, 'auto')
  assert.ok(models[0].name.length > 0)
  assert.deepEqual(models[0].inputModalities, ['text', 'image'])

  const resolved = await adapter.resolveModel('jev-router', 'auto')
  assert.equal(resolved.provider, 'jev-router')
  assert.equal(resolved.id, 'auto')
  assert.ok(Number.isInteger(resolved.context.contextWindow))
  assert.ok(resolved.context.contextWindow > 0)
})

test('requests for other providers pass straight through', () => {
  const ctx = fakeContext()
  registerAutoRoute(ctx, settings(), {
    log: ctx.logger,
    recordDecision: async () => {},
    classify: stubClassifier({ count: 0 }),
  })

  const listener = ctx.captured.listeners.find((entry) => entry.event === 'llm/stream')
  assert.ok(listener, 'the llm/stream listener must be registered')

  let nextCalls = 0
  const passed = listener.listener(
    { provider: 'opencode-go', model: 'qwen3.8-flash', messages: [] },
    () => {
      nextCalls += 1
      return 'inner-stream'
    },
  )
  assert.equal(nextCalls, 1)
  assert.equal(passed, 'inner-stream')
  assert.equal(ctx.captured.streams.length, 0)
})

test('an auto request routes to the policy primary and forwards the chunks', async () => {
  const ctx = fakeContext()
  const calls = { count: 0 }
  const records = []
  registerAutoRoute(ctx, settings(), {
    log: ctx.logger,
    recordDecision: async (path, record) => records.push(record),
    classify: stubClassifier(calls),
  })

  const listener = ctx.captured.listeners.find((entry) => entry.event === 'llm/stream')
  const chunks = await drain(
    listener.listener(
      {
        provider: 'jev-router',
        model: 'auto',
        sessionId: 'session-a',
        messages: userTurn('修复登录接口的 bug'),
      },
      () => {
        throw new Error('the auto path must not fall through to the adapter')
      },
    ),
  )

  assert.equal(calls.count, 1)
  assert.deepEqual(chunks, [
    { type: 'text-delta', text: 'hello' },
    { type: 'finish', reason: { kind: 'stop' } },
  ])

  // debugging/medium: medium (proxy 0.62) beats high (proxy 1.40).
  assert.equal(ctx.captured.streams.length, 1)
  assert.equal(ctx.captured.streams[0].provider, 'opencode-go')
  assert.equal(ctx.captured.streams[0].model, 'qwen3.8-flash')

  assert.equal(records.length, 1)
  assert.equal(records[0].kind, 'route')
  assert.equal(records[0].route_source, 'jev')
  assert.equal(records[0].model, 'qwen3.8-flash')
  assert.equal(records[0].reused, false)
})

test('one classification covers the whole turn, not every step', async () => {
  const ctx = fakeContext()
  const calls = { count: 0 }
  registerAutoRoute(ctx, settings(), {
    log: ctx.logger,
    recordDecision: async () => {},
    classify: stubClassifier(calls),
  })
  const listener = ctx.captured.listeners.find((entry) => entry.event === 'llm/stream')

  await drain(
    listener.listener(
      { provider: 'jev-router', model: 'auto', sessionId: 's1', messages: userTurn('first') },
      () => {},
    ),
  )
  await drain(
    listener.listener(
      { provider: 'jev-router', model: 'auto', sessionId: 's1', messages: toolStep() },
      () => {},
    ),
  )
  await drain(
    listener.listener(
      { provider: 'jev-router', model: 'auto', sessionId: 's1', messages: toolStep() },
      () => {},
    ),
  )

  assert.equal(calls.count, 1, 'tool round-trips must not re-classify')
  // Every step still forwards to the same concrete model.
  assert.deepEqual(
    ctx.captured.streams.map((entry) => entry.model),
    ['qwen3.8-flash', 'qwen3.8-flash', 'qwen3.8-flash'],
  )
})

test('a new user turn re-decides', async () => {
  const ctx = fakeContext()
  const calls = { count: 0 }
  registerAutoRoute(ctx, settings(), {
    log: ctx.logger,
    recordDecision: async () => {},
    classify: stubClassifier(calls),
  })
  const listener = ctx.captured.listeners.find((entry) => entry.event === 'llm/stream')

  await drain(
    listener.listener(
      { provider: 'jev-router', model: 'auto', sessionId: 's2', messages: userTurn('one') },
      () => {},
    ),
  )
  await drain(
    listener.listener(
      { provider: 'jev-router', model: 'auto', sessionId: 's2', messages: userTurn('two') },
      () => {},
    ),
  )

  assert.equal(calls.count, 2)
})

test('an identical task text is classified once', async () => {
  const ctx = fakeContext()
  const calls = { count: 0 }
  registerAutoRoute(ctx, settings(), {
    log: ctx.logger,
    recordDecision: async () => {},
    classify: stubClassifier(calls),
  })
  const listener = ctx.captured.listeners.find((entry) => entry.event === 'llm/stream')

  for (const sessionId of ['a', 'b', 'c']) {
    await drain(
      listener.listener(
        {
          provider: 'jev-router',
          model: 'auto',
          sessionId,
          messages: userTurn('the very same task'),
        },
        () => {},
      ),
    )
  }

  assert.equal(calls.count, 1, 'the same task text is answered from cache')
  assert.equal(ctx.captured.streams.length, 3)
})

test('a JEV failure falls back to the safe default, never the cheapest tier', async () => {
  const ctx = fakeContext()
  const records = []
  registerAutoRoute(ctx, settings(), {
    log: ctx.logger,
    recordDecision: async (path, record) => records.push(record),
    classify: async () => {
      throw new Error('jev is down')
    },
  })
  const listener = ctx.captured.listeners.find((entry) => entry.event === 'llm/stream')

  const chunks = await drain(
    listener.listener(
      { provider: 'jev-router', model: 'auto', sessionId: 's3', messages: userTurn('anything') },
      () => {},
    ),
  )

  assert.equal(chunks.length, 2, 'the turn still completes')
  assert.equal(ctx.captured.streams[0].provider, 'opencode-go')
  assert.equal(ctx.captured.streams[0].model, 'gpt-5.6-luna')
  assert.equal(records[0].route_source, 'safe_default')
  assert.ok(ctx.captured.warnings.length >= 1)
})

test('an undeclared task type relaxes the filter instead of paying for the safe default', async () => {
  const ctx = fakeContext()
  const records = []
  registerAutoRoute(ctx, settings(), {
    log: ctx.logger,
    recordDecision: async (path, record) => records.push(record),
    classify: stubClassifier({ count: 0 }, { task_type: 'other', required_capability: 'low' }),
  })
  const listener = ctx.captured.listeners.find((entry) => entry.event === 'llm/stream')

  await drain(
    listener.listener(
      { provider: 'jev-router', model: 'auto', sessionId: 's4', messages: userTurn('read the docs') },
      () => {},
    ),
  )

  // No model declares `other`, so the task-type filter is dropped and the
  // capability floor decides: medium (0.62) still beats low (0.65).
  assert.equal(ctx.captured.streams[0].model, 'qwen3.8-flash')
  assert.equal(records[0].route_source, 'jev')
  assert.equal(records[0].relaxed_task_type, true)
  assert.ok(records[0].reason.some((line) => line.includes('relaxing the task-type filter')))
})

test('the undeclared-task-type tolerance is configurable', async () => {
  const ctx = fakeContext()
  const records = []
  registerAutoRoute(ctx, settings({ unmatchedTaskType: 'safe_default' }), {
    log: ctx.logger,
    recordDecision: async (path, record) => records.push(record),
    classify: stubClassifier({ count: 0 }, { task_type: 'other', required_capability: 'low' }),
  })
  const listener = ctx.captured.listeners.find((entry) => entry.event === 'llm/stream')

  await drain(
    listener.listener(
      { provider: 'jev-router', model: 'auto', sessionId: 's4b', messages: userTurn('something odd') },
      () => {},
    ),
  )

  assert.equal(ctx.captured.streams[0].model, 'gpt-5.6-luna')
  assert.equal(records[0].route_source, 'safe_default')
  assert.equal(records[0].error, 'no_eligible_model')
})

test('a capability shortfall still falls back even when relaxing is allowed', async () => {
  const ctx = fakeContext()
  const records = []
  const models = DEFAULT_MODELS.filter((entry) => entry.capability !== 'high').map(
    (entry) => ({ ...entry, taskTypes: [...entry.taskTypes] }),
  )
  registerAutoRoute(ctx, settings({ models }), {
    log: ctx.logger,
    recordDecision: async (path, record) => records.push(record),
    classify: stubClassifier({ count: 0 }, { task_type: 'other', required_capability: 'high' }),
  })
  const listener = ctx.captured.listeners.find((entry) => entry.event === 'llm/stream')

  await drain(
    listener.listener(
      { provider: 'jev-router', model: 'auto', sessionId: 's4c', messages: userTurn('hard meta task') },
      () => {},
    ),
  )

  assert.equal(records[0].route_source, 'safe_default')
  assert.equal(records[0].error, 'no_eligible_model')
})

test('a safe-default turn is held for the rest of that turn', async () => {
  // Field regression: the safe-default path returned early without holding the
  // decision, so every later step of the turn re-decided (`reused: false`).
  const ctx = fakeContext()
  const records = []
  const calls = { count: 0 }
  registerAutoRoute(ctx, settings({ unmatchedTaskType: 'safe_default' }), {
    log: ctx.logger,
    recordDecision: async (path, record) => records.push(record),
    classify: stubClassifier(calls, { task_type: 'other', required_capability: 'low' }),
  })
  const listener = ctx.captured.listeners.find((entry) => entry.event === 'llm/stream')

  await drain(
    listener.listener(
      { provider: 'jev-router', model: 'auto', sessionId: 's6', messages: userTurn('read the docs') },
      () => {},
    ),
  )
  await drain(
    listener.listener(
      { provider: 'jev-router', model: 'auto', sessionId: 's6', messages: toolStep() },
      () => {},
    ),
  )
  await drain(
    listener.listener(
      { provider: 'jev-router', model: 'auto', sessionId: 's6', messages: toolStep() },
      () => {},
    ),
  )

  assert.equal(calls.count, 1, 'later steps must not re-classify')
  assert.deepEqual(
    records.map((record) => record.reused),
    [false, true, true],
  )
  assert.deepEqual(
    records.map((record) => record.route_source),
    ['safe_default', 'safe_default', 'safe_default'],
  )
  assert.deepEqual(
    ctx.captured.streams.map((entry) => entry.model),
    ['gpt-5.6-luna', 'gpt-5.6-luna', 'gpt-5.6-luna'],
  )
})

test('the safe-default path still records what JEV cost', async () => {
  const ctx = fakeContext()
  const records = []
  registerAutoRoute(ctx, settings({ unmatchedTaskType: 'safe_default' }), {
    log: ctx.logger,
    recordDecision: async (path, record) => records.push(record),
    classify: stubClassifier({ count: 0 }, { task_type: 'other', required_capability: 'low' }),
  })
  const listener = ctx.captured.listeners.find((entry) => entry.event === 'llm/stream')

  await drain(
    listener.listener(
      { provider: 'jev-router', model: 'auto', sessionId: 's7', messages: userTurn('meta task') },
      () => {},
    ),
  )

  assert.equal(records[0].route_source, 'safe_default')
  assert.equal(records[0].jev_cost, 0.000004, 'the classifier ran, so its cost must be recorded')
  assert.equal(records[0].jev_latency_ms, 12)
  assert.ok(records[0].classifier)
  assert.ok(records[0].answer_confidences)
  assert.equal(records[0].session_scoped, true)
})

test('an allowlisted trivial task skips JEV entirely', async () => {
  const ctx = fakeContext()
  const calls = { count: 0 }
  const records = []
  registerAutoRoute(ctx, settings(), {
    log: ctx.logger,
    recordDecision: async (path, record) => records.push(record),
    classify: stubClassifier(calls),
  })
  const listener = ctx.captured.listeners.find((entry) => entry.event === 'llm/stream')

  await drain(
    listener.listener(
      { provider: 'jev-router', model: 'auto', sessionId: 'r1', messages: userTurn('read README.md') },
      () => {},
    ),
  )

  assert.equal(calls.count, 0, 'a light-rule match must not pay the classifier')
  assert.equal(records[0].route_source, 'light_rule')
  assert.equal(records[0].rule_id, 'simple_file_read')
  assert.equal(records[0].jev_called, false)
  assert.equal(records[0].jev_cost, null)
  // The synthetic classifier is file_operation/low, so the policy still runs
  // and picks a concrete model: medium (0.62) beats low (0.65).
  assert.equal(ctx.captured.streams[0].model, 'qwen3.8-flash')
  assert.ok(records[0].classifier, 'the log still records what the route assumed')
})

test('the lightweight layer can be switched off', async () => {
  const ctx = fakeContext()
  const calls = { count: 0 }
  const records = []
  registerAutoRoute(ctx, settings({ lightRules: false }), {
    log: ctx.logger,
    recordDecision: async (path, record) => records.push(record),
    classify: stubClassifier(calls),
  })
  const listener = ctx.captured.listeners.find((entry) => entry.event === 'llm/stream')

  await drain(
    listener.listener(
      { provider: 'jev-router', model: 'auto', sessionId: 'r2', messages: userTurn('read README.md') },
      () => {},
    ),
  )

  assert.equal(calls.count, 1, 'with the layer off the classifier is consulted')
  assert.equal(records[0].route_source, 'jev')
  assert.equal(records[0].jev_called, true)
})

test('a light-rule turn is held like any other decision', async () => {
  const ctx = fakeContext()
  const calls = { count: 0 }
  const records = []
  registerAutoRoute(ctx, settings(), {
    log: ctx.logger,
    recordDecision: async (path, record) => records.push(record),
    classify: stubClassifier(calls),
  })
  const listener = ctx.captured.listeners.find((entry) => entry.event === 'llm/stream')

  await drain(
    listener.listener(
      { provider: 'jev-router', model: 'auto', sessionId: 'r3', messages: userTurn('list files') },
      () => {},
    ),
  )
  await drain(
    listener.listener(
      { provider: 'jev-router', model: 'auto', sessionId: 'r3', messages: toolStep() },
      () => {},
    ),
  )

  assert.equal(calls.count, 0)
  assert.deepEqual(records.map((record) => record.reused), [false, true])
  assert.deepEqual(
    records.map((record) => record.route_source),
    ['light_rule', 'light_rule'],
  )
})

test('a request with no user text uses the safe default without classifying', async () => {
  const ctx = fakeContext()
  const calls = { count: 0 }
  registerAutoRoute(ctx, settings(), {
    log: ctx.logger,
    recordDecision: async () => {},
    classify: stubClassifier(calls),
  })
  const listener = ctx.captured.listeners.find((entry) => entry.event === 'llm/stream')

  await drain(
    listener.listener(
      {
        provider: 'jev-router',
        model: 'auto',
        sessionId: 's5',
        messages: [{ role: 'system', content: [{ type: 'text', text: 'sys' }] }],
      },
      () => {},
    ),
  )

  assert.equal(calls.count, 0)
  assert.equal(ctx.captured.streams[0].model, 'gpt-5.6-luna')
})

// ── Budget Guard integration ──────────────────────────────────────────────
// These prove spending is prevented, not merely recorded: the assertion that
// matters is that `ctx.captured.streams` stays empty when a call is blocked.

test('budget: a null limit leaves routing unaffected', async () => {
  const ctx = fakeContext()
  const records = []
  registerAutoRoute(ctx, settings({ budgetLimit: null }), {
    log: ctx.logger,
    recordDecision: async (path, record) => records.push(record),
    classify: stubClassifier({ count: 0 }),
  })
  const listener = ctx.captured.listeners.find((entry) => entry.event === 'llm/stream')

  await drain(
    listener.listener(
      { provider: 'jev-router', model: 'auto', sessionId: 'b1', messages: userTurn('fix a bug') },
      () => {},
    ),
  )

  assert.equal(ctx.captured.streams.length, 1)
  assert.equal(records[0].budget_limit, null)
  assert.equal(records[0].budget_allowed, true)
})

test('budget: JEV cost plus a medium estimate inside the limit is allowed', async () => {
  const ctx = fakeContext()
  const records = []
  registerAutoRoute(ctx, settings({ budgetLimit: 1.5 }), {
    log: ctx.logger,
    recordDecision: async (path, record) => records.push(record),
    classify: stubClassifier({ count: 0 }),
  })
  const listener = ctx.captured.listeners.find((entry) => entry.event === 'llm/stream')

  await drain(
    listener.listener(
      { provider: 'jev-router', model: 'auto', sessionId: 'b2', messages: userTurn('fix a bug') },
      () => {},
    ),
  )

  assert.equal(ctx.captured.streams.length, 1, 'the provider is called')
  assert.equal(records[0].budget_allowed, true)
  assert.equal(records[0].estimated_next_max_cost, 0.25)
  assert.equal(records[0].cost_estimated, true)
  assert.equal(records[0].cost_estimation_source, 'estimated_max')
})

test('budget: a high estimate that would exceed the limit blocks the call', async () => {
  const ctx = fakeContext()
  const records = []
  // capability high -> estimate 1.00; limit 0.5 cannot fit it.
  registerAutoRoute(ctx, settings({ budgetLimit: 0.5 }), {
    log: ctx.logger,
    recordDecision: async (path, record) => records.push(record),
    classify: stubClassifier({ count: 0 }, { required_capability: 'high' }),
  })
  const listener = ctx.captured.listeners.find((entry) => entry.event === 'llm/stream')

  const chunks = await drain(
    listener.listener(
      { provider: 'jev-router', model: 'auto', sessionId: 'b3', messages: userTurn('hard task') },
      () => {},
    ),
  )

  assert.equal(ctx.captured.streams.length, 0, 'THE PROVIDER MUST NOT BE CALLED')
  assert.equal(records[0].budget_allowed, false)
  assert.equal(records[0].budget_error, 'budget_limit_reached')
  assert.equal(chunks.length, 1)
  assert.equal(chunks[0].type, 'finish')
  assert.equal(chunks[0].reason.kind, 'error')
  assert.equal(chunks[0].reason.failure.code, 'budget_limit_reached')
})

test('budget: a blocked call never silently swaps to a cheaper model', async () => {
  const ctx = fakeContext()
  const records = []
  registerAutoRoute(ctx, settings({ budgetLimit: 0.5 }), {
    log: ctx.logger,
    recordDecision: async (path, record) => records.push(record),
    classify: stubClassifier({ count: 0 }, { required_capability: 'high' }),
  })
  const listener = ctx.captured.listeners.find((entry) => entry.event === 'llm/stream')

  await drain(
    listener.listener(
      { provider: 'jev-router', model: 'auto', sessionId: 'b4', messages: userTurn('hard task') },
      () => {},
    ),
  )

  // Nothing was called at all: no downgrade, no retry, no substitute route.
  assert.equal(ctx.captured.streams.length, 0)
  assert.equal(records.length, 1, 'exactly one decision is recorded')
  assert.equal(records[0].model, 'gpt-5.6-luna', 'the chosen model is still reported honestly')
})

test('budget: the light-rule path costs nothing to classify but is still guarded', async () => {
  const ctx = fakeContext()
  const records = []
  const calls = { count: 0 }
  registerAutoRoute(ctx, settings({ budgetLimit: 0.5 }), {
    log: ctx.logger,
    recordDecision: async (path, record) => records.push(record),
    classify: stubClassifier(calls),
  })
  const listener = ctx.captured.listeners.find((entry) => entry.event === 'llm/stream')

  await drain(
    listener.listener(
      { provider: 'jev-router', model: 'auto', sessionId: 'b5', messages: userTurn('read README.md') },
      () => {},
    ),
  )

  assert.equal(calls.count, 0, 'no classifier call on the light path')
  assert.equal(records[0].jev_cost, null)
  assert.equal(records[0].jev_called, false)
  assert.equal(records[0].budget_exposure_before, 0, 'classification added no exposure')
  // The synthetic classifier is file_operation/low, so the estimate is 0.10
  // and fits inside 0.50.
  assert.equal(records[0].budget_allowed, true)
  assert.equal(ctx.captured.streams.length, 1)
})

test('budget: the guard still applies on the safe-default path', async () => {
  const ctx = fakeContext()
  const records = []
  registerAutoRoute(
    ctx,
    settings({ budgetLimit: 0.5, safeDefault: { provider: 'opencode-go', model: 'gpt-5.6-luna' } }),
    {
      log: ctx.logger,
      recordDecision: async (path, record) => records.push(record),
      classify: async () => {
        throw new Error('jev is down')
      },
    },
  )
  const listener = ctx.captured.listeners.find((entry) => entry.event === 'llm/stream')

  await drain(
    listener.listener(
      { provider: 'jev-router', model: 'auto', sessionId: 'b6', messages: userTurn('anything') },
      () => {},
    ),
  )

  // safeDefault is gpt-5.6-luna (high), estimate 1.00, limit 0.50 -> blocked.
  assert.equal(records[0].route_source, 'safe_default')
  assert.equal(ctx.captured.streams.length, 0, 'a safe default may not bypass the budget')
  assert.equal(records[0].budget_error, 'budget_limit_reached')
})

test('budget: exposure accumulates across the tool round-trips of one turn', async () => {
  const ctx = fakeContext()
  const records = []
  // A medium route estimates 0.25 per call. With a 0.60 limit: step 1 starts at
  // 0 exposure (plus the one-off classifier cost), step 2 starts at 0.25, and
  // step 3 starts at 0.50 plus its own 0.25 == 0.75 > 0.60, so step 3 is blocked.
  registerAutoRoute(ctx, settings({ budgetLimit: 0.6 }), {
    log: ctx.logger,
    recordDecision: async (path, record) => records.push(record),
    classify: stubClassifier({ count: 0 }),
  })
  const listener = ctx.captured.listeners.find((entry) => entry.event === 'llm/stream')

  const call = (messages) =>
    drain(
      listener.listener(
        { provider: 'jev-router', model: 'auto', sessionId: 'b7', messages },
        () => {},
      ),
    )

  await call(userTurn('multi step task'))
  await call(toolStep())

  assert.equal(records[0].budget_exposure_before, 0.000004, 'only the classifier cost at first')
  assert.ok(records[0].budget_allowed)
  // Step 1 charged its classifier call plus the medium estimate.
  assert.equal(records[1].budget_exposure_before, 0.250004, 'the second step sees the first')
  assert.ok(records[1].budget_allowed)
  assert.equal(ctx.captured.streams.length, 2)

  // The third step cannot fit its own estimate, so the provider is never called.
  await call(toolStep())
  assert.equal(records[2].budget_exposure_before, 0.500004)
  assert.equal(records[2].budget_allowed, false)
  assert.equal(records[2].budget_error, 'budget_limit_reached')
  assert.equal(
    ctx.captured.streams.length,
    2,
    'the blocked step did not reach the provider',
  )
})

test('budget: a new user turn starts a fresh account', async () => {
  const ctx = fakeContext()
  const records = []
  registerAutoRoute(ctx, settings({ budgetLimit: 0.3 }), {
    log: ctx.logger,
    recordDecision: async (path, record) => records.push(record),
    classify: stubClassifier({ count: 0 }),
  })
  const listener = ctx.captured.listeners.find((entry) => entry.event === 'llm/stream')

  const call = (messages) =>
    drain(
      listener.listener(
        { provider: 'jev-router', model: 'auto', sessionId: 'b8', messages },
        () => {},
      ),
    )

  await call(userTurn('first task'))
  await call(toolStep())
  const blockedCount = ctx.captured.streams.length

  // A new user message opens a new turn with a fresh account.
  await call(userTurn('a completely different task'))

  assert.ok(blockedCount <= 2)
  assert.equal(records[records.length - 1].budget_exposure_before, 0.000004)
  assert.equal(
    ctx.captured.streams.length,
    blockedCount + 1,
    'the new turn is allowed again',
  )
})

test('budget: parent, child and sibling sessions keep separate accounts', async () => {
  const ctx = fakeContext()
  const records = []
  registerAutoRoute(ctx, settings({ budgetLimit: 0.3 }), {
    log: ctx.logger,
    recordDecision: async (path, record) => records.push(record),
    classify: stubClassifier({ count: 0 }),
  })
  const listener = ctx.captured.listeners.find((entry) => entry.event === 'llm/stream')

  const call = (sessionId, messages) =>
    drain(
      listener.listener(
        { provider: 'jev-router', model: 'auto', sessionId, messages },
        () => {},
      ),
    )

  await call('parent', userTurn('delegate some work'))
  await call('parent', toolStep())
  const afterParent = records.length

  // A child agent has its own session and must not inherit the parent's spend.
  await call('child-1', userTurn('a small subtask'))
  assert.equal(
    records[afterParent].budget_exposure_before,
    0.000004,
    'the child starts from zero',
  )
  assert.equal(records[afterParent].budget_allowed, true)

  await call('child-2', userTurn('another subtask'))
  assert.equal(
    records[afterParent + 1].budget_exposure_before,
    0.000004,
    'a sibling also starts from zero',
  )
})

test('budget: a repeated prompt in the same session still opens a new turn', async () => {
  // Field defect: keying the account on session + opening text alone made a
  // second turn that repeated the same wording inherit the first turn's spend.
  const ctx = fakeContext()
  const records = []
  const calls = { count: 0 }
  registerAutoRoute(ctx, settings({ budgetLimit: 1.5 }), {
    log: ctx.logger,
    recordDecision: async (path, record) => records.push(record),
    classify: stubClassifier(calls),
  })
  const listener = ctx.captured.listeners.find((entry) => entry.event === 'llm/stream')

  const call = (messages) =>
    drain(
      listener.listener(
        { provider: 'jev-router', model: 'auto', sessionId: 'same', messages },
        () => {},
      ),
    )

  await call(userTurn('same task'))
  // A genuinely new user turn, with byte-identical text.
  await call(userTurn('same task'))

  assert.equal(records.length, 2)
  assert.equal(records[0].budget_exposure_before, 0.000004, 'turn 1 paid for its classifier call')
  // Turn 2 is a fresh account AND a classifier cache hit, so it starts at a
  // true zero: no inherited execution exposure and no re-charged JEV cost.
  assert.equal(records[1].budget_exposure_before, 0)
  assert.equal(records[1].jev_cache_hit, true)
  assert.equal(records[1].budget_allowed, true)
  assert.equal(ctx.captured.streams.length, 2, 'both turns reached the provider')
})

test('budget: an unknown route is never treated as free', async () => {
  // Field defect: an unregistered safeDefault produced estimated_next_max_cost
  // of 0 and was dispatched straight through a $0.01 ceiling.
  const ctx = fakeContext()
  const records = []
  registerAutoRoute(
    ctx,
    settings({
      budgetLimit: 0.01,
      safeDefault: { provider: 'other-provider', model: 'expensive-model' },
    }),
    {
      log: ctx.logger,
      recordDecision: async (path, record) => records.push(record),
      classify: async () => {
        throw new Error('jev is down')
      },
    },
  )
  const listener = ctx.captured.listeners.find((entry) => entry.event === 'llm/stream')

  const chunks = await drain(
    listener.listener(
      { provider: 'jev-router', model: 'auto', sessionId: 'unknown', messages: userTurn('anything') },
      () => {},
    ),
  )

  assert.equal(records[0].route_source, 'safe_default')
  assert.equal(records[0].estimated_next_max_cost, null, 'no estimate is invented')
  assert.equal(records[0].budget_allowed, false)
  assert.equal(records[0].budget_error, 'budget_estimate_unavailable')
  assert.equal(ctx.captured.streams.length, 0, 'THE PROVIDER MUST NOT BE CALLED')
  assert.equal(chunks.length, 1)
  assert.equal(chunks[0].reason.failure.code, 'budget_estimate_unavailable')
})

test('budget: a known safeDefault is still estimated and still guarded', async () => {
  // The counterpart to the case above: a registered safeDefault keeps working
  // and is measured, so the fix does not simply refuse everything it cannot
  // recognise by accident.
  const ctx = fakeContext()
  const records = []
  registerAutoRoute(ctx, settings({ budgetLimit: 1.5 }), {
    log: ctx.logger,
    recordDecision: async (path, record) => records.push(record),
    classify: async () => {
      throw new Error('jev is down')
    },
  })
  const listener = ctx.captured.listeners.find((entry) => entry.event === 'llm/stream')

  await drain(
    listener.listener(
      { provider: 'jev-router', model: 'auto', sessionId: 'known', messages: userTurn('anything') },
      () => {},
    ),
  )

  assert.equal(records[0].route_source, 'safe_default')
  assert.equal(records[0].estimated_next_max_cost, 1.0, 'gpt-5.6-luna is a high tier')
  assert.equal(records[0].budget_allowed, true)
  assert.equal(ctx.captured.streams.length, 1)
})

test('budget: a classifier cache hit is not charged again and is labelled honestly', async () => {
  // Field defect: the classifier charge keyed on route reuse, so a new turn
  // that hit the classifier cache re-charged the previous turn's JEV cost and
  // logged jev_called=true for an API call that never happened. That would
  // corrupt the evidence collected for JEV_CONFIDENCE_SEMANTICS_GATE.
  const ctx = fakeContext()
  const records = []
  const calls = { count: 0 }
  registerAutoRoute(ctx, settings({ budgetLimit: 1.5 }), {
    log: ctx.logger,
    recordDecision: async (path, record) => records.push(record),
    classify: stubClassifier(calls),
  })
  const listener = ctx.captured.listeners.find((entry) => entry.event === 'llm/stream')

  const call = (messages) =>
    drain(
      listener.listener(
        { provider: 'jev-router', model: 'auto', sessionId: 'cache', messages },
        () => {},
      ),
    )

  // Turn 1 actually calls JEV.
  await call(userTurn('same task'))
  // Turn 2 is a genuinely new turn with identical text: a cache hit, no API call.
  await call(userTurn('same task'))

  assert.equal(calls.count, 1, 'JEV is called once; the second turn hits the cache')

  assert.deepEqual(
    records.map((record) => record.jev_called),
    [true, false],
    'only the first turn actually consulted JEV',
  )
  assert.deepEqual(
    records.map((record) => record.jev_cache_hit),
    [false, true],
    'the second turn is recorded as a cache hit',
  )
  assert.equal(records[0].budget_classifier_charge, 0.000004, 'the exact JEV cost, 6dp')
  assert.equal(records[1].budget_classifier_charge, 0, 'a cache hit costs nothing')

  // Budget reset and classifier cache reuse are independent facts: turn 2 opens
  // a fresh account AND still must not carry the cached JEV cost into it.
  assert.equal(records[1].budget_exposure_before, 0)
  assert.equal(records[1].budget_allowed, true)
  assert.equal(ctx.captured.streams.length, 2, 'both turns reached the provider')
})

test('budget: a cached classification still routes on the cached result', async () => {
  const ctx = fakeContext()
  const records = []
  registerAutoRoute(ctx, settings({ budgetLimit: 1.5 }), {
    log: ctx.logger,
    recordDecision: async (path, record) => records.push(record),
    classify: stubClassifier({ count: 0 }),
  })
  const listener = ctx.captured.listeners.find((entry) => entry.event === 'llm/stream')

  const call = (sessionId, messages) =>
    drain(
      listener.listener({ provider: 'jev-router', model: 'auto', sessionId, messages }, () => {}),
    )

  await call('cache-a', userTurn('repeated wording'))
  await call('cache-b', userTurn('repeated wording'))

  // A cache hit still yields a real classifier result, so the model choice is
  // unchanged; only the accounting and the JEV-call evidence differ.
  assert.deepEqual(
    ctx.captured.streams.map((entry) => entry.model),
    ['qwen3.8-flash', 'qwen3.8-flash'],
  )
  assert.ok(records[1].classifier, 'the cached classification is still reported')
  assert.equal(records[1].jev_cache_hit, true)
})

test('budget: the classifier is charged once per turn, never per step', async () => {
  // Second field defect: a reused held route carried the turn's jevCalled and
  // jevCost forward, so every tool round-trip of one turn re-reported a JEV call
  // that did not happen and charged the classifier again.
  const ctx = fakeContext()
  const records = []
  const calls = { count: 0 }
  registerAutoRoute(ctx, settings({ budgetLimit: 0.6 }), {
    log: ctx.logger,
    recordDecision: async (path, record) => records.push(record),
    classify: stubClassifier(calls),
  })
  const listener = ctx.captured.listeners.find((entry) => entry.event === 'llm/stream')

  const call = (messages) =>
    drain(
      listener.listener(
        { provider: 'jev-router', model: 'auto', sessionId: 'once', messages },
        () => {},
      ),
    )

  await call(userTurn('multi step task'))
  await call(toolStep())
  await call(toolStep())

  assert.equal(calls.count, 1, 'JEV is consulted once for the whole turn')
  assert.deepEqual(
    records.map((record) => record.jev_called),
    [true, false, false],
    'only the step that classified reports a JEV call',
  )
  assert.deepEqual(
    records.map((record) => record.budget_classifier_charge),
    [0.000004, 0, 0],
    'the classifier is charged exactly once',
  )
  assert.deepEqual(
    records.map((record) => record.reused),
    [false, true, true],
    'the later steps are genuine route reuse',
  )
  // Execution exposure still accumulates across the turn.
  assert.equal(records[1].budget_exposure_before, 0.250004)
  assert.equal(records[2].budget_exposure_before, 0.500004)
  assert.equal(ctx.captured.streams.length, 2, 'the third step was blocked by the ceiling')
})
