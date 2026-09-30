/**
 * Unit tests for the JEV schema, key-file reader, and response parser.
 *
 * These exercise the parity-critical parts without any network access.
 */

import assert from 'node:assert/strict'
import { mkdtemp, writeFile } from 'node:fs/promises'
import { tmpdir } from 'node:os'
import { join } from 'node:path'
import test from 'node:test'

import {
  JevError,
  parseResponse,
  scoreToInteger,
} from '../lib/jev/classifier.js'
import { readApiKey, rtfToText } from '../lib/jev/keyfile.js'
import {
  DIFFICULTY_LEVELS,
  LEVELS,
  TASK_TYPES,
  questionSchema,
} from '../lib/jev/schema.js'

/** Build a complete, valid System One response payload. */
function payload(overrides = {}) {
  return {
    model: 'jev-1.13.0',
    request_id: 'req_test',
    answers: {
      task_type: { choice: 'debugging', confidence: 0.86, probabilities: {} },
      difficulty_score: { score: 5.15, confidence: 0.6, probabilities: {} },
      difficulty_bucket: { choice: 'medium', confidence: 0.7, probabilities: {} },
      required_capability: { choice: 'medium', confidence: 0.9, probabilities: {} },
      risk_level: { choice: 'low', confidence: 0.5, probabilities: {} },
    },
    usage: { input_tokens: 769, output_tokens: 212 },
    ...overrides,
  }
}

test('scoreToInteger maps the raw 0..9 scale onto 1..10 half-up', () => {
  assert.equal(scoreToInteger(0), 1)
  assert.equal(scoreToInteger(1.15), 2)
  assert.equal(scoreToInteger(2.15), 3)
  assert.equal(scoreToInteger(4.5), 6, 'a true half rounds up')
  assert.equal(scoreToInteger(8.9), 10)
  assert.equal(scoreToInteger(9), 10, 'clamped at the ceiling')
})

test('scoreToInteger rejects out-of-range and non-numeric input', () => {
  for (const bad of [-1, 10, Number.NaN, '3', true, null]) {
    assert.throws(() => scoreToInteger(bad), (error) => error instanceof JevError)
  }
})

test('parseResponse normalizes a valid payload', () => {
  const { classifier, metrics } = parseResponse(payload(), 731)
  assert.deepEqual(classifier, {
    schema_version: '0.1',
    task_type: 'debugging',
    difficulty_score: 6,
    difficulty_bucket: 'medium',
    required_capability: 'medium',
    confidence: 0.5,
    risk_level: 'low',
    notes: null,
  })
  assert.equal(metrics.returned_model, 'jev-1.13.0')
  assert.equal(metrics.latency_ms, 731)
  // 769 input tokens at $0.042/M — output tokens are recorded, never priced.
  assert.ok(Math.abs(metrics.exact_cost - 0.000032298) < 1e-12)
})

test('parseResponse takes the minimum reported confidence', () => {
  const { classifier } = parseResponse(payload(), 1)
  assert.equal(classifier.confidence, 0.5, 'the lowest confidence wins')
})

test('parseResponse reports per-answer confidence for diagnosis', () => {
  const { answer_confidences: perAnswer } = parseResponse(payload(), 1)
  assert.deepEqual(perAnswer, {
    task_type: 0.86,
    difficulty_score: 0.6,
    difficulty_bucket: 0.7,
    required_capability: 0.9,
    risk_level: 0.5,
  })
})

test('a zero-confidence answer zeroes the frozen minimum', () => {
  const zeroed = payload()
  zeroed.answers.difficulty_score.confidence = 0
  const { classifier, answer_confidences: perAnswer } = parseResponse(zeroed, 1)
  assert.equal(classifier.confidence, 0, 'min() collapses to 0 — the known weak point')
  assert.equal(perAnswer.difficulty_score, 0)
  assert.equal(perAnswer.task_type, 0.86, 'the other answers keep their signal')
})

test('parseResponse rejects malformed payloads', () => {
  assert.throws(() => parseResponse(null, 1), JevError)
  assert.throws(() => parseResponse(payload({ model: '' }), 1), JevError)
  assert.throws(() => parseResponse(payload({ usage: null }), 1), JevError)

  const missingAnswer = payload()
  delete missingAnswer.answers.risk_level
  assert.throws(() => parseResponse(missingAnswer, 1), JevError)

  const badConfidence = payload()
  badConfidence.answers.task_type.confidence = 1.4
  assert.throws(() => parseResponse(badConfidence, 1), JevError)

  const noConfidence = payload()
  for (const answer of Object.values(noConfidence.answers)) delete answer.confidence
  assert.throws(() => parseResponse(noConfidence, 1), JevError)

  const badUsage = payload()
  badUsage.usage.input_tokens = -1
  assert.throws(() => parseResponse(badUsage, 1), JevError)
})

test('questionSchema declares every answered question exactly once', () => {
  const schema = questionSchema()
  assert.deepEqual(Object.keys(schema), [
    'task_type',
    'difficulty_score',
    'difficulty_bucket',
    'required_capability',
    'risk_level',
  ])
  assert.deepEqual(Object.keys(schema.task_type.criteria), [...TASK_TYPES])
  assert.equal(schema.task_type.criteria.file_operation, 'file operation')
  assert.deepEqual(schema.difficulty_score.criteria, [...DIFFICULTY_LEVELS])
  assert.deepEqual(Object.keys(schema.required_capability.criteria), [...LEVELS])
})

test('rtfToText extracts visible text and skips metadata groups', () => {
  const rtf =
    '{\\rtf1\\ansi{\\fonttbl{\\f0 Helvetica;}}\\f0 API_KEY: abc123\\par}'
  assert.equal(rtfToText(rtf), 'API_KEY: abc123')
})

test('readApiKey resolves a labelled plain-text key file', async () => {
  const dir = await mkdtemp(join(tmpdir(), 'jev-key-'))
  const path = join(dir, 'key.txt')
  await writeFile(path, 'JEV_API_KEY: sk-test-value\n', 'utf8')
  assert.equal(readApiKey(path), 'sk-test-value')
})

test('readApiKey resolves a bare key file holding only the key', async () => {
  const dir = await mkdtemp(join(tmpdir(), 'jev-key-'))
  const path = join(dir, 'key.txt')
  await writeFile(path, 'sk-bare-key\n', 'utf8')
  assert.equal(readApiKey(path), 'sk-bare-key')
})

test('readApiKey fails loudly instead of returning a wrong key', async () => {
  const dir = await mkdtemp(join(tmpdir(), 'jev-key-'))
  const path = join(dir, 'key.txt')
  await writeFile(path, 'this file has several words and no label\n', 'utf8')
  assert.throws(() => readApiKey(path), /does not contain a key/)
  assert.throws(() => readApiKey(join(dir, 'missing.txt')), /could not be read/)
})
