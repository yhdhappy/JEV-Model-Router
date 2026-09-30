/**
 * Policy tests, including a parity check against the Python reference.
 *
 * The parity check runs `src/jev_router/policy.py` over the same 27
 * classification combinations and compares the resulting primary/fallback
 * order. It skips cleanly when the project virtualenv is unavailable, so the
 * suite stays runnable from a bare checkout.
 */

import assert from 'node:assert/strict'
import { execFileSync } from 'node:child_process'
import { existsSync } from 'node:fs'
import { dirname, join } from 'node:path'
import { fileURLToPath } from 'node:url'
import test from 'node:test'

import { DEFAULT_MODELS } from '../lib/jev/models.js'
import { NoEligibleModelError, decideRoute, priceProxy } from '../lib/jev/policy.js'

const HERE = dirname(fileURLToPath(import.meta.url))
const PROJECT_ROOT = join(HERE, '..', '..', '..')
const PYTHON = join(PROJECT_ROOT, '.venv', 'bin', 'python')
const REGISTRY = join(PROJECT_ROOT, 'config', 'models.real-pilot.yaml')

/** The frozen registry as the JS policy sees it. */
const MODELS = DEFAULT_MODELS.map((entry) => ({ ...entry, taskTypes: [...entry.taskTypes] }))

const classify = (task_type, required_capability) => ({ task_type, required_capability })

test('the phase-one price proxy is input plus output', () => {
  assert.equal(priceProxy({ input: 0.15, output: 0.47 }), 0.62)
  assert.equal(priceProxy({ input: 0.2, output: 1.2 }), 1.4)
})

test('a lower tier is excluded below the capability floor', () => {
  const decision = decideRoute(classify('debugging', 'high'), MODELS)
  assert.equal(decision.primary.name, 'high_model')
  assert.deepEqual(decision.fallbacks, [])
  assert.ok(decision.reason.some((line) => line.includes('low_model excluded')))
  assert.ok(decision.reason.some((line) => line.includes('medium_model excluded')))
})

test('an unsupported task type excludes every tier', () => {
  assert.throws(
    () => decideRoute(classify('other', 'low'), MODELS),
    (error) => error instanceof NoEligibleModelError,
  )
})

test('disabled models never win', () => {
  const models = MODELS.map((entry) =>
    entry.name === 'medium_model' ? { ...entry, enabled: false } : entry,
  )
  // coding/low without medium_model: low (0.65) now beats high (1.40).
  const decision = decideRoute(classify('coding', 'low'), models)
  assert.equal(decision.primary.name, 'low_model')
  assert.ok(decision.reason.some((line) => line.includes('medium_model excluded: enabled=false')))
})

test('ordering reproduces the frozen known quirk', () => {
  // low_model's proxy (0.65) is above medium_model's (0.62), so a `low`
  // coding request selects the *medium* tier. This is frozen stage-1
  // behaviour, observed in the gate smoke, and is reproduced deliberately.
  const decision = decideRoute(classify('coding', 'low'), MODELS)
  assert.equal(decision.primary.name, 'medium_model')
  assert.deepEqual(
    decision.fallbacks.map((entry) => entry.name),
    ['low_model', 'high_model'],
  )
})

test('the JS policy matches the Python reference on all 27 combinations', (t) => {
  if (!existsSync(PYTHON) || !existsSync(REGISTRY)) {
    t.skip('project virtualenv or real-pilot registry is unavailable')
    return
  }

  const taskTypes = [
    'file_operation',
    'coding',
    'debugging',
    'testing',
    'review',
    'architecture',
    'research',
    'reasoning',
    'other',
  ]
  const keys = []
  for (const capability of ['low', 'medium', 'high']) {
    for (const taskType of taskTypes) keys.push(`${taskType}|${capability}`)
  }

  const pythonSource = `
import json, sys
sys.path.insert(0, ${JSON.stringify(join(PROJECT_ROOT, 'src'))})
from jev_router.registry import ModelRegistry
from jev_router.policy import PolicyEngine
from jev_router.schemas import (ClassifierResult, TaskType, DifficultyBucket,
                                CapabilityLevel, RiskLevel)
registry = ModelRegistry.from_yaml(${JSON.stringify(REGISTRY)})
engine = PolicyEngine(registry)
out = {}
for key in json.loads(sys.argv[1]):
    task_type, capability = key.split("|")
    classification = ClassifierResult(
        schema_version="0.1", task_type=TaskType(task_type), difficulty_score=4,
        difficulty_bucket=DifficultyBucket.MEDIUM,
        required_capability=CapabilityLevel(capability), confidence=0.5,
        risk_level=RiskLevel.LOW)
    try:
        decision = engine.decide(classification)
        out[key] = {"primary": decision.primary_model,
                    "fallbacks": list(decision.fallback_models)}
    except Exception as error:
        out[key] = {"error": type(error).__name__}
print(json.dumps(out))
`

  const expected = JSON.parse(
    execFileSync(PYTHON, ['-c', pythonSource, JSON.stringify(keys)], {
      cwd: PROJECT_ROOT,
      encoding: 'utf8',
    }),
  )

  const actual = {}
  for (const key of keys) {
    const [task_type, required_capability] = key.split('|')
    try {
      const decision = decideRoute(classify(task_type, required_capability), MODELS)
      actual[key] = {
        primary: decision.primary.name,
        fallbacks: decision.fallbacks.map((entry) => entry.name),
      }
    } catch (error) {
      actual[key] = { error: error.constructor.name }
    }
  }

  assert.deepEqual(actual, expected, 'the JS policy must mirror the Python policy')
  assert.equal(keys.length, 27)
})
