/**
 * Budget Guard tests.
 *
 * Deterministic and local only: no real model is ever called to prove a
 * spending ceiling. The Python `check_budget` reference is exercised for
 * parity so the two implementations cannot drift.
 */

import assert from 'node:assert/strict'
import { execFileSync } from 'node:child_process'
import { existsSync } from 'node:fs'
import { dirname, join } from 'node:path'
import { fileURLToPath } from 'node:url'
import test from 'node:test'

import {
  BUDGET_LIMIT_REACHED,
  BudgetLedger,
  checkBudget,
  estimateMaxCost,
  turnKey,
} from '../lib/llm/budget.js'

const HERE = dirname(fileURLToPath(import.meta.url))
const PROJECT_ROOT = join(HERE, '..', '..', '..')
const PYTHON = join(PROJECT_ROOT, '.venv', 'bin', 'python')

test('current + next below the limit is allowed', () => {
  const decision = checkBudget(0.5, 0.25, 1.5)
  assert.equal(decision.allowed, true)
  assert.equal(decision.error_code, null)
})

test('current + next exactly at the limit is allowed', () => {
  // The frozen Python semantics are `current + next <= limit`, so equality
  // must pass rather than block.
  const decision = checkBudget(1.25, 0.25, 1.5)
  assert.equal(decision.allowed, true)
  assert.equal(decision.error_code, null)
})

test('current + next above the limit is blocked', () => {
  const decision = checkBudget(1.26, 0.25, 1.5)
  assert.equal(decision.allowed, false)
  assert.equal(decision.error_code, BUDGET_LIMIT_REACHED)
})

test('a null limit disables the guard entirely', () => {
  const decision = checkBudget(999, 999, null)
  assert.equal(decision.allowed, true)
  assert.equal(decision.budget_limit, null)
})

test('the equality boundary is exact despite binary floating point', () => {
  // 0.15 + 0.47 is not exactly 0.62 in binary floating point; the guard scales
  // to integers so the boundary cannot drift.
  const decision = checkBudget(0.15, 0.47, 0.62)
  assert.equal(decision.allowed, true)
  assert.equal(decision.current_accumulated_cost, 0.15)
  assert.equal(decision.estimated_next_max_cost, 0.47)
})

test('invalid arguments are rejected rather than guessed', () => {
  assert.throws(() => checkBudget(-1, 0.1, 1), TypeError)
  assert.throws(() => checkBudget(0, Number.NaN, 1), TypeError)
  assert.throws(() => checkBudget(0, 0.1, 'lots'), TypeError)
  assert.throws(() => checkBudget(0, 0.1, -5), TypeError)
})

test('the frozen tier estimates are reused, not reinvented', () => {
  assert.equal(estimateMaxCost('low'), 0.1)
  assert.equal(estimateMaxCost('medium'), 0.25)
  assert.equal(estimateMaxCost('high'), 1.0)
  assert.throws(() => estimateMaxCost('unknown'), TypeError)
})

test('turn keys separate sessions and keep one turn addressable', () => {
  assert.notEqual(turnKey('session-a', 'hi'), turnKey('session-b', 'hi'))
  assert.notEqual(turnKey('session-a', 'hi'), turnKey('session-a', 'bye'))
  assert.equal(turnKey('session-a', 'hi'), turnKey('session-a', 'hi'))
})

test('a ledger accumulates within one turn', () => {
  const ledger = new BudgetLedger()
  const key = turnKey('s1', 'task')
  ledger.openTurn(key)
  ledger.charge(key, { execution: 0.25 })
  ledger.charge(key, { execution: 0.25 })
  assert.equal(ledger.peek(key).spent, 0.5)
})

test('separate turns, sessions and agents never share an account', () => {
  const ledger = new BudgetLedger()
  const parent = turnKey('parent-session', 'task')
  const child = turnKey('child-session', 'sub task')
  const nextTurn = turnKey('parent-session', 'another task')

  ledger.charge(parent, { execution: 0.25 })
  ledger.charge(child, { execution: 0.25 })
  ledger.charge(nextTurn, { execution: 0.25 })

  assert.equal(ledger.peek(parent).spent, 0.25, 'child spend must not touch the parent')
  assert.equal(ledger.peek(child).spent, 0.25)
  assert.equal(ledger.peek(nextTurn).spent, 0.25, 'a new turn starts fresh')
})

test('reopening a turn resets its account', () => {
  const ledger = new BudgetLedger()
  const key = turnKey('s1', 'task')
  ledger.charge(key, { execution: 0.9 })
  ledger.openTurn(key)
  assert.equal(ledger.peek(key).spent, 0)
})

test('the ledger is bounded', () => {
  const ledger = new BudgetLedger({ maxTurns: 2 })
  ledger.charge(turnKey('a', 'x'), { execution: 0.1 })
  ledger.charge(turnKey('b', 'x'), { execution: 0.1 })
  ledger.charge(turnKey('c', 'x'), { execution: 0.1 })
  assert.equal(ledger.peek(turnKey('a', 'x')), undefined, 'the oldest turn is evicted')
  assert.ok(ledger.peek(turnKey('c', 'x')))
})

test('the JS guard matches the Python reference on the boundary grid', (t) => {
  if (!existsSync(PYTHON)) {
    t.skip('project virtualenv is unavailable')
    return
  }

  const source = `
import json, sys
sys.path.insert(0, ${JSON.stringify(join(PROJECT_ROOT, 'src'))})
from jev_router.fallback import check_budget
out = []
for case in json.loads(sys.argv[1]):
    decision = check_budget(case[0], case[1], case[2])
    out.append({
        "allowed": decision.allowed,
        "current_accumulated_cost": decision.current_accumulated_cost,
        "estimated_next_max_cost": decision.estimated_next_max_cost,
        "budget_limit": decision.budget_limit,
        "error_code": decision.error_code,
    })
print(json.dumps(out))
`

  const cases = []
  for (const current of [0, 0.15, 0.5, 1.0, 1.24, 1.25, 1.26, 1.5, 2.0]) {
    for (const next of [0, 0.1, 0.25, 1.0]) {
      for (const limit of [null, 0, 0.62, 1.25, 1.5]) cases.push([current, next, limit])
    }
  }

  const expected = JSON.parse(
    execFileSync(PYTHON, ['-c', source, JSON.stringify(cases)], {
      cwd: PROJECT_ROOT,
      encoding: 'utf8',
    }),
  )

  const actual = cases.map(([current, next, limit]) => {
    const decision = checkBudget(current, next, limit)
    return {
      allowed: decision.allowed,
      current_accumulated_cost: decision.current_accumulated_cost,
      estimated_next_max_cost: decision.estimated_next_max_cost,
      budget_limit: decision.budget_limit,
      error_code: decision.error_code,
    }
  })

  assert.equal(actual.length, cases.length)
  for (let index = 0; index < cases.length; index += 1) {
    assert.deepEqual(
      actual[index],
      expected[index],
      `boundary case ${JSON.stringify(cases[index])}`,
    )
  }
})
