/**
 * Budget Guard: the per-turn spend ceiling.
 *
 * Mirrors `check_budget` in `src/jev_router/fallback.py`: a call is allowed
 * only when `current + estimated <= limit`. `null` means no limit was
 * configured, and equality is allowed. Decimal semantics are reproduced by
 * scaling to integers, because `0.15 + 0.47` in binary floating point is not
 * exactly `0.62` and the equality boundary must stay deterministic.
 *
 * The guard is a spending-prevention mechanism, deliberately separate from
 * JEV failure handling: a JEV failure may fall back to the safe default, but
 * the safe default still has to pass this guard. It never silently swaps to a
 * cheaper model to fit the budget — a call that would exceed the limit is not
 * made at all.
 *
 * @module dsh-plugin-jev-router/llm/budget
 */

/** Estimated maximum cost per capability tier, from the frozen Pilot config. */
export const DEFAULT_ESTIMATED_MAX_COSTS = Object.freeze({
  low: 0.1,
  medium: 0.25,
  high: 1.0,
})

/** Stable error code shared with the frozen Router vocabulary. */
export const BUDGET_LIMIT_REACHED = 'budget_limit_reached'

/** Decimal places kept when normalizing money, matching the $0.000001 log scale. */
const SCALE = 1_000_000

/**
 * Convert a money value to an exact scaled integer.
 *
 * @param value - a finite, non-negative number.
 * @param field - field name used in the error.
 * @returns the value scaled by {@link SCALE}.
 * @throws {TypeError} when the value is not a finite non-negative number.
 */
function toScaled(value, field) {
  if (typeof value !== 'number' || !Number.isFinite(value) || value < 0) {
    throw new TypeError(`${field} must be a finite non-negative number`)
  }
  return Math.round(value * SCALE)
}

/**
 * Decide whether one more model call may be made.
 *
 * @param currentAccumulatedCost - spend already committed inside this turn.
 * @param estimatedNextMaxCost - conservative upper bound for the next call.
 * @param budgetLimit - the turn ceiling, or `null` for no limit.
 * @returns the decision, including the stable error code when blocked.
 * @throws {TypeError} when any argument is invalid.
 */
export function checkBudget(
  currentAccumulatedCost,
  estimatedNextMaxCost,
  budgetLimit,
) {
  const current = toScaled(currentAccumulatedCost, 'current_accumulated_cost')
  const estimated = toScaled(estimatedNextMaxCost, 'estimated_next_max_cost')
  const limit =
    budgetLimit === null || budgetLimit === undefined
      ? null
      : toScaled(budgetLimit, 'budget_limit')

  const allowed = limit === null || current + estimated <= limit

  return {
    allowed,
    current_accumulated_cost: current / SCALE,
    estimated_next_max_cost: estimated / SCALE,
    budget_limit: limit === null ? null : limit / SCALE,
    error_code: allowed ? null : BUDGET_LIMIT_REACHED,
  }
}

/**
 * Estimate a model's worst-case cost from its capability tier.
 *
 * @param capability - the tier recorded in the model table.
 * @param estimates - tier-to-cost table.
 * @returns the estimated maximum cost in USD.
 */
export function estimateMaxCost(capability, estimates = DEFAULT_ESTIMATED_MAX_COSTS) {
  const value = estimates[capability]
  if (typeof value !== 'number' || !Number.isFinite(value) || value < 0) {
    throw new TypeError(`no estimated max cost configured for capability "${capability}"`)
  }
  return value
}

/**
 * Per-turn accumulated spend, isolated by owning turn.
 *
 * DSH differs from the single-shot Python Router: one user task is many model
 * calls (LLM -> tool -> LLM -> ...), so the guard cannot restart from zero on
 * every request. State is keyed so that separate turns, separate sessions, and
 * parent vs child agents never share an account.
 */
export class BudgetLedger {
  /** @param options - `{ maxTurns }` bounds how many turns are retained. */
  constructor({ maxTurns = 64 } = {}) {
    this._maxTurns = maxTurns
    this._turns = new Map()
  }

  /**
   * Begin a turn, resetting its account.
   *
   * A turn is identified by its session plus its opening user message, so a
   * re-sent identical prompt starts a fresh account rather than inheriting a
   * spent one.
   *
   * @param key - the turn key.
   * @returns the fresh record.
   */
  openTurn(key) {
    const record = { spent: 0, classifier: 0, execution: 0, calls: 0, source: null }
    this._turns.set(key, record)
    if (this._turns.size > this._maxTurns) {
      const oldest = this._turns.keys().next().value
      this._turns.delete(oldest)
    }
    return record
  }

  /**
   * Read one turn's record without creating it.
   *
   * @param key - the turn key.
   * @returns the record, or undefined.
   */
  peek(key) {
    return this._turns.get(key)
  }

  /**
   * Record committed spend against a turn.
   *
   * @param key - the turn key.
   * @param component - `{ classifier, execution, source }` amounts in USD.
   * @returns the updated record.
   */
  charge(key, { classifier = 0, execution = 0, source = null } = {}) {
    const record = this._turns.get(key) ?? this.openTurn(key)
    record.classifier += classifier
    record.execution += execution
    record.spent = record.classifier + record.execution
    record.calls += 1
    if (source !== null) record.source = source
    return record
  }

  /** Drop every recorded turn. */
  clear() {
    this._turns.clear()
  }
}

/**
 * Build the stable turn key that scopes one account.
 *
 * The session keeps parent, child and sibling sessions apart; the opening user
 * text keeps consecutive turns in one session apart.
 *
 * @param sessionId - the owning session, when the caller supplied one.
 * @param openingText - the user text that opened the turn.
 * @returns the turn key.
 */
export function turnKey(sessionId, openingText) {
  const scope = typeof sessionId === 'string' && sessionId.length > 0 ? sessionId : 'no-session'
  return `${scope}\u0000${openingText}`
}
