/**
 * JEV (TypeSafe System One) request schema.
 *
 * This module is a field-for-field mirror of the Python reference
 * implementation in `src/jev_router/real_jev.py`. Any change here must be
 * mirrored there, and vice versa; `tests/parity/` proves the two produce the
 * same request body for the same task input.
 *
 * @module dsh-plugin-jev-router/jev/schema
 */

/** System One endpoint used by the frozen Python reference implementation. */
export const SYSTEM_ONE_URL = 'https://api.typesafe.ai/v1/systemone'

/** Model alias requested from System One. */
export const JEV_MODEL = 'jev-latest'

/**
 * JEV input price per million tokens, in USD.
 *
 * The Python reference charges input tokens only; output tokens are recorded
 * but never priced, and this module keeps that behavior exactly.
 */
export const JEV_INPUT_PRICE_PER_MILLION = 0.042

/** Task types accepted by the stage-1 classifier schema. */
export const TASK_TYPES = Object.freeze([
  'file_operation',
  'coding',
  'debugging',
  'testing',
  'review',
  'architecture',
  'research',
  'reasoning',
  'other',
])

/** Coarse capability / difficulty buckets. */
export const LEVELS = Object.freeze(['low', 'medium', 'high'])

/** Ordered difficulty criteria; index 0 is the raw score 0, index 9 is raw 9. */
export const DIFFICULTY_LEVELS = Object.freeze([
  'trivial, one obvious local action',
  'simple, one small change with little ambiguity',
  'straightforward, limited reasoning or verification',
  'moderate, a few related steps or files',
  'moderate, meaningful debugging or coordination',
  'substantial, several interacting decisions',
  'difficult, broad reasoning and validation',
  'very difficult, high ambiguity or system interaction',
  'complex, multiple high-impact dependencies',
  'exceptionally difficult, the hardest class of task',
])

/**
 * Build the System One `questions` object.
 *
 * @returns the exact question schema the Python reference sends.
 */
export function questionSchema() {
  return {
    task_type: {
      type: 'choice',
      instructions: 'What kind of task is this?',
      criteria: Object.fromEntries(
        TASK_TYPES.map((value) => [value, value.replace(/_/g, ' ')]),
      ),
    },
    difficulty_score: {
      type: 'score',
      instructions: 'How difficult is this task?',
      criteria: [...DIFFICULTY_LEVELS],
    },
    difficulty_bucket: {
      type: 'choice',
      instructions: 'Which difficulty bucket best fits this task?',
      criteria: Object.fromEntries(LEVELS.map((value) => [value, value])),
    },
    required_capability: {
      type: 'choice',
      instructions: 'What minimum capability level is required?',
      criteria: Object.fromEntries(LEVELS.map((value) => [value, value])),
    },
    risk_level: {
      type: 'choice',
      instructions: 'What is the task risk level?',
      criteria: Object.fromEntries(LEVELS.map((value) => [value, value])),
    },
  }
}

/** Names the classifier reads back, in the order the Python parser checks them. */
export const ANSWER_NAMES = Object.freeze([
  'task_type',
  'difficulty_score',
  'difficulty_bucket',
  'required_capability',
  'risk_level',
])
