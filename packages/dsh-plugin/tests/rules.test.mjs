/**
 * Lightweight rule engine tests, including a parity check against Python.
 *
 * The parity check runs `src/jev_router/rules.py` over the same corpus and
 * compares every field of the rule result. It skips cleanly when the project
 * virtualenv is unavailable.
 */

import assert from 'node:assert/strict'
import { execFileSync } from 'node:child_process'
import { existsSync } from 'node:fs'
import { dirname, join } from 'node:path'
import { fileURLToPath } from 'node:url'
import test from 'node:test'

import {
  classifierFromRule,
  downgradeSteps,
  evaluateLightweightRules,
  isDowngradeAllowed,
  isExplicitFilePath,
  normalizeText,
} from '../lib/jev/rules.js'

const HERE = dirname(fileURLToPath(import.meta.url))
const PROJECT_ROOT = join(HERE, '..', '..', '..')
const PYTHON = join(PROJECT_ROOT, '.venv', 'bin', 'python')

/** Corpus shared by the unit tests and the Python parity check. */
const CASES = [
  ['read README.md', []],
  ['read README', []],
  ['please show the file src/main.py', []],
  ['view Makefile', []],
  ['open docs/guide.md contents', []],
  ['读取 README.md', []],
  ['请查看 src/main.py 的内容', []],
  ['列出目录文件', []],
  ['list files', []],
  ['read the file', ['README.md']],
  ['read README.md', ['README.md']],
  ['read README.md', ['other.md']],
  ['read the file', []],
  ['list files', ['README.md']],
  ['fix the bug in main.py', []],
  ['read README.md and fix it', []],
  ['调试登录问题', []],
  ['帮我写一个登录系统', []],
  ['review the architecture', []],
  ['read src/app.py', ['src/app.py', 'extra']],
  ['read .hidden', []],
  ['read ../secrets.md', []],
  ['read a/b/c/d/e.md', []],
  ['read file.', []],
  ['read 123', []],
  ['show Dockerfile', []],
  ['read a very long prompt '.repeat(20).trim(), []],
  ['read README.md', ['x'.repeat(300)]],
]

test('downgrade guard allows one level and refuses two', () => {
  assert.equal(downgradeSteps('high', 'medium'), 1)
  assert.equal(downgradeSteps('low', 'high'), 0, 'an upgrade is not a downgrade')
  assert.equal(isDowngradeAllowed('high', 'medium'), true)
  assert.equal(isDowngradeAllowed('high', 'low'), false)
  assert.equal(isDowngradeAllowed('medium', 'low'), true)
})

test('normalizeText collapses whitespace', () => {
  assert.equal(normalizeText('  read\n\t README.md  '), 'read README.md')
})

test('path safety rejects traversal and dotfiles', () => {
  assert.equal(isExplicitFilePath('src/main.py'), true)
  assert.equal(isExplicitFilePath('README.md'), true)
  assert.equal(isExplicitFilePath('README'), true)
  assert.equal(isExplicitFilePath('Makefile'), true)
  assert.equal(isExplicitFilePath('../secrets.md'), false)
  assert.equal(isExplicitFilePath('.hidden'), false)
  assert.equal(isExplicitFilePath('notes'), false, 'no extension and not a known name')
})

test('a plain file read is allowlisted at low capability', () => {
  const result = evaluateLightweightRules('read README.md')
  assert.equal(result.matched, true)
  assert.equal(result.rule_id, 'simple_file_read')
  assert.equal(result.capability, 'low')
  assert.equal(result.confidence, 0.99)
})

test('a directory listing is allowlisted', () => {
  const result = evaluateLightweightRules('列出目录文件')
  assert.equal(result.matched, true)
  assert.equal(result.rule_id, 'simple_file_list')
})

test('any complexity marker defers to JEV', () => {
  for (const prompt of ['fix the bug', '调试登录问题', 'review the architecture']) {
    const result = evaluateLightweightRules(prompt)
    assert.equal(result.matched, false, prompt)
    assert.equal(result.reason.includes('Complexity marker'), true, prompt)
  }
})

test('the synthetic classifier matches the frozen Router shape', () => {
  const rule = evaluateLightweightRules('read README.md')
  assert.deepEqual(classifierFromRule(rule), {
    schema_version: '0.1',
    task_type: 'file_operation',
    difficulty_score: 1,
    difficulty_bucket: 'low',
    required_capability: 'low',
    confidence: 0.99,
    risk_level: 'low',
    notes: null,
  })
})

test('the JS rule engine matches the Python reference on the whole corpus', (t) => {
  if (!existsSync(PYTHON)) {
    t.skip('project virtualenv is unavailable')
    return
  }

  const source = `
import json, sys
sys.path.insert(0, ${JSON.stringify(join(PROJECT_ROOT, 'src'))})
from jev_router.rules import LightweightRuleEngine
from jev_router.schemas import RouteRequest
engine = LightweightRuleEngine()
out = []
for case in json.loads(sys.argv[1]):
    request = RouteRequest(task_id="parity", prompt=case["prompt"], context=case["context"])
    result = engine.evaluate(request)
    out.append({
        "matched": result.matched,
        "rule_id": result.rule_id,
        "confidence": result.confidence,
        "capability": result.capability.value if result.capability is not None else None,
        "reason": result.reason,
    })
print(json.dumps(out, ensure_ascii=False))
`

  const payload = CASES.map(([prompt, context]) => ({ prompt, context }))
  const expected = JSON.parse(
    execFileSync(PYTHON, ['-c', source, JSON.stringify(payload)], {
      cwd: PROJECT_ROOT,
      encoding: 'utf8',
    }),
  )

  const actual = payload.map(({ prompt, context }) => {
    const result = evaluateLightweightRules(prompt, { context })
    return {
      matched: result.matched,
      rule_id: result.rule_id,
      confidence: result.confidence,
      capability: result.capability,
      reason: result.reason,
    }
  })

  assert.equal(actual.length, CASES.length)
  for (let index = 0; index < payload.length; index += 1) {
    assert.deepEqual(
      actual[index],
      expected[index],
      `corpus case ${index}: ${JSON.stringify(payload[index])}`,
    )
  }
})
