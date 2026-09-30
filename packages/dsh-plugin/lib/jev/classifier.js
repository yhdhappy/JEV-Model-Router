/**
 * JEV (TypeSafe System One) classifier client.
 *
 * A behavioral mirror of `RealJEVClassifier` in the Python reference
 * implementation (`src/jev_router/real_jev.py`): same request body, same
 * response validation, same difficulty mapping, same cost arithmetic. The
 * only intentional difference is transport — this module uses the runtime's
 * global `fetch` instead of `urllib`.
 *
 * SECURITY: the API key is read from a local file at call time, sent only in
 * the `Authorization` header, and never logged or persisted.
 *
 * @module dsh-plugin-jev-router/jev/classifier
 */

import {
  ANSWER_NAMES,
  JEV_INPUT_PRICE_PER_MILLION,
  JEV_MODEL,
  SYSTEM_ONE_URL,
  questionSchema,
} from './schema.js'
import { readApiKey } from './keyfile.js'

/** Stable failure codes shared with the Python reference vocabulary. */
export const JEV_ERROR_CODES = Object.freeze({
  TIMEOUT: 'jev_timeout',
  NETWORK: 'jev_network_error',
  INVALID: 'jev_invalid_response',
  SCHEMA: 'jev_schema_error',
  PROVIDER: 'jev_provider_error',
  CONFIG: 'jev_config_error',
})

/** A classifier failure carrying one stable, machine-readable code. */
export class JevError extends Error {
  /**
   * @param code - one of {@link JEV_ERROR_CODES}.
   * @param message - operator-facing explanation.
   */
  constructor(code, message) {
    super(message)
    this.name = 'JevError'
    this.code = code
  }
}

/**
 * Map JEV's ordered raw 0..9 score onto the 1..10 integer the router uses.
 *
 * Rule: round-half-up(raw + 1), then clamp into 1..10 — identical to
 * `_score_to_integer` in the Python reference.
 *
 * @param raw - the raw `score` value from System One.
 * @returns the integer difficulty score.
 */
export function scoreToInteger(raw) {
  if (typeof raw !== 'number' || !Number.isFinite(raw) || raw < 0 || raw > 9) {
    throw new JevError(
      JEV_ERROR_CODES.SCHEMA,
      'JEV classifier response failed schema validation',
    )
  }
  // Nudge away from binary floating-point representation error so an exact
  // half never rounds down by accident (e.g. 2.15 + 1 stored as 3.1499...).
  const shifted = Number(`${raw}`) + 1
  const rounded = Math.floor(shifted * 1e9 + 0.5 + 1e-6) / 1e9
  const integer = Math.floor(rounded + 0.5)
  return Math.max(1, Math.min(10, integer))
}

/**
 * Validate and normalize one System One response payload.
 *
 * @param payload - the decoded JSON response body.
 * @param latencyMs - measured round-trip time, recorded as-is.
 * @returns immutable classifier result plus safe call metrics.
 */
export function parseResponse(payload, latencyMs) {
  const schemaError = () =>
    new JevError(
      JEV_ERROR_CODES.SCHEMA,
      'JEV classifier response failed schema validation',
    )

  if (typeof payload !== 'object' || payload === null) throw schemaError()
  const { answers, usage, model: returnedModel } = payload
  if (typeof answers !== 'object' || answers === null) throw schemaError()
  if (typeof usage !== 'object' || usage === null) throw schemaError()
  if (typeof returnedModel !== 'string' || returnedModel.length === 0) {
    throw schemaError()
  }

  const values = {}
  const confidences = []
  const answerConfidences = {}
  for (const name of ANSWER_NAMES) {
    const answer = answers[name]
    if (typeof answer !== 'object' || answer === null) throw schemaError()
    const valueKey = name === 'difficulty_score' ? 'score' : 'choice'
    if (!(valueKey in answer)) throw schemaError()
    values[name] = answer[valueKey]

    const confidence = answer.confidence
    if (confidence !== undefined && confidence !== null) {
      if (
        typeof confidence !== 'number' ||
        !Number.isFinite(confidence) ||
        confidence < 0 ||
        confidence > 1
      ) {
        throw schemaError()
      }
      confidences.push(confidence)
      answerConfidences[name] = confidence
    }
  }

  if (confidences.length === 0) throw schemaError()

  const inputTokens = usageInt(usage, 'input_tokens', schemaError)
  const outputTokens = usageInt(usage, 'output_tokens', schemaError)
  const requestId = payload.request_id ?? payload.id ?? null
  if (requestId !== null && typeof requestId !== 'string') throw schemaError()

  values.difficulty_score = scoreToInteger(values.difficulty_score)
  values.schema_version = '0.1'
  values.confidence = Math.min(...confidences)
  values.notes = null

  const exactCost =
    (inputTokens * JEV_INPUT_PRICE_PER_MILLION) / 1_000_000

  return {
    classifier: {
      schema_version: values.schema_version,
      task_type: values.task_type,
      difficulty_score: values.difficulty_score,
      difficulty_bucket: values.difficulty_bucket,
      required_capability: values.required_capability,
      confidence: values.confidence,
      risk_level: values.risk_level,
      notes: values.notes,
    },
    metrics: {
      returned_model: returnedModel,
      request_id: requestId,
      input_tokens: inputTokens,
      output_tokens: outputTokens,
      latency_ms: latencyMs,
      exact_cost: exactCost,
    },
    // Per-answer confidence is diagnostic only. The frozen router contract
    // still reads `classifier.confidence` (the minimum), so this addition
    // makes a deflated minimum explainable without changing any decision.
    answer_confidences: answerConfidences,
  }
}

/**
 * Classify one task through the real JEV API.
 *
 * @param options - classification request.
 * @param options.prompt - the task text to classify.
 * @param options.context - optional supporting lines.
 * @param options.apiKeyFile - absolute path to the JEV key file.
 * @param options.endpoint - override the System One endpoint.
 * @param options.timeoutMs - request timeout in milliseconds.
 * @param options.fetchImpl - transport override, for tests.
 * @returns the classifier result and safe call metrics.
 */
export async function classifyJev({
  prompt,
  context = [],
  apiKeyFile,
  endpoint = SYSTEM_ONE_URL,
  timeoutMs = 30_000,
  fetchImpl = globalThis.fetch,
} = {}) {
  if (typeof prompt !== 'string' || prompt.trim().length === 0) {
    throw new JevError(JEV_ERROR_CODES.INVALID, 'JEV task prompt is required')
  }
  if (!endpoint.startsWith('https://')) {
    throw new JevError(JEV_ERROR_CODES.PROVIDER, 'JEV endpoint must use HTTPS')
  }
  if (!(timeoutMs > 0) || !Number.isFinite(timeoutMs)) {
    throw new JevError(JEV_ERROR_CODES.PROVIDER, 'JEV timeout must be positive')
  }
  if (typeof fetchImpl !== 'function') {
    throw new JevError(JEV_ERROR_CODES.NETWORK, 'no fetch implementation available')
  }

  // A key-file problem is a configuration failure, not an API failure; it is
  // normalized here so every caller sees one error type.
  let apiKey
  try {
    apiKey = readApiKey(apiKeyFile)
  } catch (error) {
    throw new JevError(
      JEV_ERROR_CODES.CONFIG,
      error instanceof Error ? error.message : 'JEV API key is unavailable',
    )
  }

  const body = JSON.stringify({
    model: JEV_MODEL,
    state: { prompt, context: [...context] },
    questions: questionSchema(),
  })

  const controller = new AbortController()
  const timer = setTimeout(() => controller.abort(), timeoutMs)
  const started = Date.now()

  let response
  try {
    response = await fetchImpl(endpoint, {
      method: 'POST',
      headers: {
        Authorization: `Bearer ${apiKey}`,
        'Content-Type': 'application/json',
        Accept: 'application/json',
      },
      body,
      signal: controller.signal,
    })
  } catch (error) {
    if (error && error.name === 'AbortError') {
      throw new JevError(JEV_ERROR_CODES.TIMEOUT, 'JEV classifier timed out')
    }
    throw new JevError(
      JEV_ERROR_CODES.NETWORK,
      'JEV classifier network request failed',
    )
  } finally {
    clearTimeout(timer)
  }

  const latencyMs = Date.now() - started

  if (!response.ok) {
    if (response.status === 408 || response.status === 504) {
      throw new JevError(JEV_ERROR_CODES.TIMEOUT, 'JEV classifier timed out')
    }
    throw new JevError(
      JEV_ERROR_CODES.PROVIDER,
      `JEV classifier returned HTTP ${response.status}`,
    )
  }

  let payload
  try {
    payload = JSON.parse(await response.text())
  } catch {
    throw new JevError(
      JEV_ERROR_CODES.INVALID,
      'JEV classifier returned invalid JSON',
    )
  }

  return parseResponse(payload, latencyMs)
}

/**
 * Read one non-negative integer usage field.
 *
 * @param usage - the `usage` object.
 * @param name - the field name.
 * @param schemaError - factory producing the validation error.
 * @returns the validated value.
 */
function usageInt(usage, name, schemaError) {
  const value = usage[name]
  if (typeof value !== 'number' || !Number.isInteger(value) || value < 0) {
    throw schemaError()
  }
  return value
}
