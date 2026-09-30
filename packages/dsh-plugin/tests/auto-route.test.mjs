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
        exact_cost: 0.0000042,
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
  assert.equal(records[0].jev_cost, 0.0000042, 'the classifier ran, so its cost must be recorded')
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
