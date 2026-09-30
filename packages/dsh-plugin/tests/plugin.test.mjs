/**
 * Plugin-level tests: command registration and handler behavior.
 *
 * The success path performs one real JEV call and is skipped unless
 * `JEV_TEST_KEY_FILE` points at a key file, so the suite stays offline by
 * default and still verifies the live integration when asked to.
 */

import assert from 'node:assert/strict'
import { mkdtemp, readFile } from 'node:fs/promises'
import { tmpdir } from 'node:os'
import { join } from 'node:path'
import test from 'node:test'

import { apply, inject, name, resolveSettings } from '../lib/index.js'

/** Minimal Cordis context capturing the command registration. */
function fakeContext() {
  const registered = []
  const logs = []
  const warnings = []
  const injected = []
  const ctx = {
    registered,
    logs,
    warnings,
    injected,
    commands: {
      register(definition) {
        registered.push(definition)
      },
    },
    llm: {
      registerAdapter() {},
      stream() {
        return (async function* () {})()
      },
    },
    on() {},
    inject(deps, callback) {
      injected.push({ deps, callback })
      callback(ctx)
    },
    logger: {
      info(...args) {
        logs.push(args)
      },
      warn(...args) {
        warnings.push(args)
      },
    },
  }
  return ctx
}

/** Minimal invocation carrying one session with the given last user text. */
function invocation(rawInput, sessionText) {
  return {
    commandId: 'cmd-test-1',
    rawInput,
    attachments: [],
    signal: undefined,
    agent: {
      session: {
        deriveMessages: () =>
          sessionText === undefined
            ? []
            : [{ role: 'user', content: [{ type: 'text', text: sessionText }] }],
      },
    },
  }
}

test('apply registers exactly one /jev command and logs readiness', () => {
  const ctx = fakeContext()
  apply(ctx, {})

  assert.equal(ctx.registered.length, 1)
  const definition = ctx.registered[0]
  assert.equal(definition.name, 'jev')
  assert.equal(typeof definition.handler, 'function')
  assert.match(definition.name, /^[a-z][a-z0-9_-]*$/)
  assert.ok(definition.description.trim().length > 0)
  assert.ok(ctx.logs.length >= 1, 'readiness is logged')
})

test('plugin exports the documented Cordis shape', () => {
  assert.equal(name, 'jev-router')
  assert.deepEqual(inject, ['commands'])
})

test('settings defaults are safe and self-consistent', () => {
  const resolved = resolveSettings({})
  assert.equal(resolved.autoRoute, true)
  assert.equal(resolved.timeoutMs, 30000)
  assert.equal(resolved.cacheSize, 32)
  assert.equal(resolved.models.length, 3)
  assert.equal(resolved.safeDefault.provider, 'opencode-go')
})

test('a model routed back into this plugin is refused', () => {
  // Without this guard, JEV deciding to use `jev-router` would recurse forever.
  const resolved = resolveSettings({
    safeDefault: { provider: 'jev-router', model: 'auto' },
    models: [
      {
        name: 'loop_model',
        provider: 'jev-router',
        model: 'auto',
        capability: 'low',
        taskTypes: ['coding'],
        input: 0,
        output: 0,
      },
      {
        name: 'real_model',
        provider: 'opencode-go',
        model: 'qwen3.8-flash',
        capability: 'medium',
        taskTypes: ['coding'],
        input: 0.15,
        output: 0.47,
      },
    ],
  })

  assert.equal(resolved.safeDefault.provider, 'opencode-go')
  assert.deepEqual(
    resolved.models.map((entry) => entry.name),
    ['real_model'],
  )
})

test('autoRoute false is honoured', () => {
  assert.equal(resolveSettings({ autoRoute: false }).autoRoute, false)
})

test('the auto route waits for the llm service instead of racing it', () => {
  const ctx = fakeContext()
  apply(ctx, {})

  assert.equal(ctx.injected.length, 1, 'registration must go through ctx.inject')
  assert.deepEqual(ctx.injected[0].deps, ['llm'])

  const withoutLlm = fakeContext()
  apply(withoutLlm, { autoRoute: false })
  assert.equal(withoutLlm.injected.length, 0, 'a disabled route injects nothing')
  assert.equal(withoutLlm.registered.length, 1, '/jev still registers')
})

test('one malformed model entry falls back to the frozen defaults', () => {
  const resolved = resolveSettings({
    models: [{ name: 'broken', provider: 'opencode-go', capability: 'medium' }],
  })
  assert.equal(resolved.models.length, 3, 'an unusable table falls back wholesale')
})

test('a missing key file is reported as an error, never as a success', async () => {
  const ctx = fakeContext()
  apply(ctx, {})
  const result = await ctx.registered[0].handler(invocation('do something'))
  assert.equal(result.kind, 'error')
  assert.match(result.text, /未配置 JEV 密钥文件/)
})

test('an empty task falls back to the latest user message', async () => {
  const ctx = fakeContext()
  apply(ctx, { apiKeyFile: '/nonexistent/key.rtf' })
  // The fallback resolved a task, so the failure is the unreadable key file,
  // not the "no task text" usage error.
  const result = await ctx.registered[0].handler(invocation('', '帮我修复登录 bug'))
  assert.equal(result.kind, 'error')
  assert.doesNotMatch(result.text, /没有可判断的任务文本/)
})

test('no task text and no session yields a usage error', async () => {
  const ctx = fakeContext()
  apply(ctx, { apiKeyFile: '/nonexistent/key.rtf' })
  const result = await ctx.registered[0].handler(invocation('', undefined))
  assert.equal(result.kind, 'error')
  assert.match(result.text, /没有可判断的任务文本/)
})

test('live JEV call returns a judgment and writes one decision record', async (t) => {
  const keyFile = process.env.JEV_TEST_KEY_FILE
  if (!keyFile) {
    t.skip('set JEV_TEST_KEY_FILE to run the live JEV integration test')
    return
  }

  const dir = await mkdtemp(join(tmpdir(), 'jev-log-'))
  const decisionLog = join(dir, 'decisions.jsonl')

  const ctx = fakeContext()
  apply(ctx, { apiKeyFile: keyFile, decisionLog })
  const result = await ctx.registered[0].handler(
    invocation('帮我修复登录接口偶发 500，并补一个回归测试'),
  )

  assert.equal(result.kind, 'success', result.text)
  for (const field of ['任务类型', '难度', '所需能力', '风险级别', '置信度', '各项置信度']) {
    assert.match(result.text, new RegExp(field))
  }
  assert.doesNotMatch(result.text, /sk-/, 'no key material may reach the output')

  const lines = (await readFile(decisionLog, 'utf8')).trim().split('\n')
  assert.equal(lines.length, 1)
  const record = JSON.parse(lines[0])
  assert.equal(record.source, '命令参数')
  assert.equal(record.classifier.schema_version, '0.1')
  assert.ok(['low', 'medium', 'high'].includes(record.classifier.required_capability))
  assert.ok(record.classifier.difficulty_score >= 1)
  assert.ok(record.classifier.difficulty_score <= 10)
  assert.ok(record.metrics.exact_cost >= 0)
  assert.equal(
    typeof record.answer_confidences,
    'object',
    'per-answer confidence is recorded as evidence for the adjust gate',
  )
  assert.ok(
    record.classifier.confidence ===
      Math.min(...Object.values(record.answer_confidences)),
    'the frozen minimum rule still governs the classifier result',
  )
  assert.ok(!JSON.stringify(record).includes('Bearer'))
})
