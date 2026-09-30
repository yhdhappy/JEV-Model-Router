/**
 * Deterministic model eligibility and ordering policy.
 *
 * A behavior mirror of `src/jev_router/policy.py` at stage P1: the same
 * task-type filter, the same capability floor, the same phase-one price proxy
 * (`input + output` per million tokens) and the same tie-break order
 * (`proxy`, `input`, `output`, `name`).
 *
 * The mirror is deliberate: the stage-1 gate smoke observed a `low`
 * file_operation request selecting the *medium* tier because the proxy makes
 * medium cheaper than low. That is a real property of the frozen rule, not a
 * bug, and the adapter must reproduce it rather than invent a friendlier one.
 *
 * @module dsh-plugin-jev-router/jev/policy
 */

/** Capability ordering used by the capability floor. */
export const CAPABILITY_RANK = Object.freeze({ low: 0, medium: 1, high: 2 })

/** Raised when no configured model satisfies the classification. */
export class NoEligibleModelError extends Error {
  /**
   * @param message - operator-facing explanation.
   * @param reasons - one line per model explaining why it was excluded.
   */
  constructor(message, reasons = []) {
    super(message)
    this.name = 'NoEligibleModelError'
    this.reasons = reasons
  }
}

/**
 * Phase-one cost proxy: input plus output price per million tokens.
 *
 * @param model - one configured model entry.
 * @returns the proxy used only for ordering.
 */
export function priceProxy(model) {
  return Number(model.input) + Number(model.output)
}

/**
 * Choose the primary model and its fallback chain for one classification.
 *
 * @param classifier - the JEV classifier result.
 * @param models - configured model entries:
 *   `{ name, provider, model, capability, taskTypes, input, output, enabled }`.
 * @returns the ordered decision with an auditable reason list.
 * @throws {NoEligibleModelError} when nothing satisfies the classification.
 */
export function decideRoute(classifier, models) {
  const reasons = [
    `task_type=${classifier.task_type}`,
    `required_capability=${classifier.required_capability}`,
  ]
  const eligible = []
  const exclusions = []

  const byName = [...models].sort((a, b) => (a.name < b.name ? -1 : a.name > b.name ? 1 : 0))
  for (const model of byName) {
    const exclusion = exclusionReason(model, classifier)
    if (exclusion !== null) {
      reasons.push(exclusion)
      exclusions.push(exclusion)
      continue
    }
    eligible.push(model)
    reasons.push(
      `${model.name} eligible: capability=${model.capability}, ` +
        `phase-one cost proxy=${priceProxy(model)} ` +
        `(input=${model.input}, output=${model.output}; ` +
        'tie-break=input,output,model_name)',
    )
  }

  if (eligible.length === 0) {
    throw new NoEligibleModelError(
      `No eligible model for task_type=${classifier.task_type}, ` +
        `required_capability=${classifier.required_capability}`,
      exclusions,
    )
  }

  const ordered = [...eligible].sort(compareModels)
  const primary = ordered[0]
  const fallbacks = ordered.slice(1)

  reasons.push(
    `primary_model=${primary.name} selected by lowest phase-one cost proxy, ` +
      'then input/output price and model name',
  )
  reasons.push(
    fallbacks.length > 0
      ? `fallback_models=${fallbacks.map((m) => m.name).join(',')} ordered by ` +
          'ascending phase-one cost proxy'
      : 'fallback_models=[]: no additional eligible models',
  )

  return { primary, fallbacks, reason: reasons }
}

/**
 * Explain why one model is accepted or rejected.
 *
 * @param model - one configured model entry.
 * @param classifier - the JEV classifier result.
 * @returns an exclusion line, or `null` when the model is eligible.
 */
function exclusionReason(model, classifier) {
  if (model.enabled === false) return `${model.name} excluded: enabled=false`
  const supported = Array.isArray(model.taskTypes) ? model.taskTypes : []
  if (!supported.includes(classifier.task_type)) {
    return `${model.name} excluded: task_type=${classifier.task_type} unsupported`
  }
  const required = CAPABILITY_RANK[classifier.required_capability] ?? 0
  const actual = CAPABILITY_RANK[model.capability] ?? 0
  if (actual < required) {
    return (
      `${model.name} excluded: capability=${model.capability} below ` +
      `required_capability=${classifier.required_capability}`
    )
  }
  return null
}

/**
 * Order two eligible models by the frozen stage-1 tie-break chain.
 *
 * @param a - one eligible model.
 * @param b - the other eligible model.
 * @returns a negative, zero, or positive comparison result.
 */
function compareModels(a, b) {
  const keys = [
    [priceProxy(a), priceProxy(b)],
    [Number(a.input), Number(b.input)],
    [Number(a.output), Number(b.output)],
  ]
  for (const [left, right] of keys) {
    if (left !== right) return left - right
  }
  return a.name < b.name ? -1 : a.name > b.name ? 1 : 0
}
