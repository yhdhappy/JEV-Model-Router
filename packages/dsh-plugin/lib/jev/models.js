/**
 * Default model catalog for the JEV router.
 *
 * Mirrors `config/models.real-pilot.yaml`: the three frozen tiers, their
 * task-type coverage, and their per-million prices. The `provider`/`model`
 * values are the routes the desktop profile already exposes through
 * `@deepseek-ai/dsh-llm-pi-ai` (`opencode-go`), so the adapter re-dispatches
 * through the host's own credential and transport rather than shelling out.
 *
 * @module dsh-plugin-jev-router/jev/models
 */

/** Synthetic provider route this plugin owns. */
export const AUTO_PROVIDER = 'jev-router'

/** Synthetic model id the model picker shows. */
export const AUTO_MODEL = 'auto'

/** Display name shown in the `/model` picker. */
export const AUTO_MODEL_NAME = '自动选择（JEV 智能路由）'

/**
 * The three frozen tiers, in the Python registry's own shape.
 *
 * `name` is the registry key; `model` is the provider-owned model id.
 */
export const DEFAULT_MODELS = Object.freeze([
  Object.freeze({
    name: 'low_model',
    provider: 'opencode-go',
    model: 'glm-5.3-flash',
    capability: 'low',
    taskTypes: Object.freeze(['file_operation', 'coding', 'debugging']),
    input: 0.15,
    output: 0.5,
    enabled: true,
  }),
  Object.freeze({
    name: 'medium_model',
    provider: 'opencode-go',
    model: 'qwen3.8-flash',
    capability: 'medium',
    taskTypes: Object.freeze([
      'file_operation',
      'coding',
      'debugging',
      'testing',
      'review',
    ]),
    input: 0.15,
    output: 0.47,
    enabled: true,
  }),
  Object.freeze({
    name: 'high_model',
    provider: 'opencode-go',
    model: 'gpt-5.6-luna',
    capability: 'high',
    taskTypes: Object.freeze([
      'file_operation',
      'coding',
      'debugging',
      'testing',
      'review',
      'architecture',
      'research',
      'reasoning',
    ]),
    input: 0.2,
    output: 1.2,
    enabled: true,
  }),
])

/** Context window advertised for the synthetic `auto` model. */
export const AUTO_CONTEXT_WINDOW = 1_000_000
