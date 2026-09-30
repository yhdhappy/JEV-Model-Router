/**
 * Standalone smoke test for the JEV classifier used by the DSH plugin.
 *
 * Usage:
 *   node scripts/smoke.mjs "<task text>" [key-file-path]
 *
 * The key file path may also come from `JEV_API_KEY_FILE`. The key itself is
 * never printed. This script is deliberately independent of DSH so the
 * classifier can be verified without loading the plugin.
 */

import { classifyJev, JevError } from '../lib/jev/classifier.js'

const prompt = process.argv[2] ?? '帮我修复登录接口偶发 500，并补一个回归测试'
const apiKeyFile =
  process.argv[3] ?? process.env.JEV_API_KEY_FILE ?? process.env.TYPESAFE_API_KEY_FILE

if (!apiKeyFile) {
  console.error('No key file: pass it as argv[2] or set JEV_API_KEY_FILE.')
  process.exit(2)
}

try {
  const { classifier, metrics } = await classifyJev({ prompt, apiKeyFile })
  console.log(
    JSON.stringify(
      {
        task_type: classifier.task_type,
        difficulty_score: classifier.difficulty_score,
        difficulty_bucket: classifier.difficulty_bucket,
        required_capability: classifier.required_capability,
        risk_level: classifier.risk_level,
        confidence: classifier.confidence,
        returned_model: metrics.returned_model,
        input_tokens: metrics.input_tokens,
        output_tokens: metrics.output_tokens,
        latency_ms: metrics.latency_ms,
        exact_cost_usd: metrics.exact_cost,
      },
      null,
      2,
    ),
  )
} catch (error) {
  if (error instanceof JevError) {
    console.error(`JEV failed: ${error.code} — ${error.message}`)
    process.exit(1)
  }
  throw error
}
