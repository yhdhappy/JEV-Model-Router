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

import { apply, inject, name } from '../lib/index.js'

/** Minimal Cordis context capturing the command registration. */
function fakeContext() {
  const registered = []
  const logs = []
  return {
    registered,
    logs,
    commands: {
      register(definition) {
        registered.push(definition)
      },
    },
    logger: {
      info(...args) {
        logs.push(args)
      },
    },
  }
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
  assert.equal(ctx.logs.length, 1)
})

test('plugin exports the documented Cordis shape', () => {
  assert.equal(name, 'jev-router')
  assert.deepEqual(inject, ['commands'])
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
  for (const field of ['任务类型', '难度', '所需能力', '风险级别', '置信度']) {
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
  assert.ok(!JSON.stringify(record).includes('Bearer'))
})
