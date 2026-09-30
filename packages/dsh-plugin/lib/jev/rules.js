/**
 * Conservative, whitelist-only lightweight routing rules.
 *
 * A behavior mirror of `src/jev_router/rules.py`: the same two allowlisted
 * task shapes (read one explicit file, list the current directory), the same
 * high-confidence guard, the same one-step downgrade limit, and the same
 * complexity vetoes. A match lets the adapter answer without calling JEV at
 * all, which is the entire point of the layer: the frozen design forbids
 * paying the classifier tax on tasks that need no semantic judgment.
 *
 * Everything unmatched defers to JEV; nothing here is allowed to guess.
 *
 * @module dsh-plugin-jev-router/jev/rules
 */

/** A downgrade may remove at most this many capability levels. */
export const MAXIMUM_DOWNGRADE_STEPS = 1

/** A rule candidate must be at least this confident to be trusted. */
export const HIGH_CONFIDENCE_THRESHOLD = 0.95

/** Every lightweight rule routes to the lowest capability tier. */
const LIGHTWEIGHT_CAPABILITY = 'low'

/** Capability ordering shared with the policy. */
const CAPABILITY_RANK = { low: 0, medium: 1, high: 2 }

const SIMPLE_PATH = '(?:[A-Za-z0-9_.-]+/)*[A-Za-z0-9_.-]+'

const EXTENSIONLESS_FILE_NAMES = new Set([
  'README',
  'LICENSE',
  'Makefile',
  'Dockerfile',
])

const READ_PATTERNS = [
  new RegExp(
    `^(?:please\\s+)?(?:read|view|show|open|display|print)\\s+` +
      `(?:the\\s+)?(?:file\\s+)?(?<path>${SIMPLE_PATH})` +
      `(?:\\s+contents?)?\\.?$`,
    'id',
  ),
  new RegExp(
    `^(?:请)?(?:读取|查看|打开|显示|打印)(?:一下)?` +
      `(?:文件)?\\s*(?<path>${SIMPLE_PATH})\\s*(?:的内容)?[。.]?$`,
    'd',
  ),
]

const GENERIC_READ_PATTERNS = [
  /^(?:please\s+)?(?:read|view|show|open|display|print)\s+(?:(?:the|a|an)\s+)?file(?:\s+contents?)?\.?$/i,
  /^(?:请)?(?:读取|查看|打开)(?:一下)?(?:文件)?(?:内容)?[。.]?$/,
]

const LIST_PATTERNS = [
  /^(?:please\s+)?list\s+(?:the\s+)?files?\.?$/i,
  /^(?:请)?列出(?:当前)?目录文件[。.]?$/,
]

/** Substrings that disqualify a task from the lightweight path. */
const COMPLEX_MARKERS = [
  'debug',
  'debugging',
  'fix',
  'error',
  'failure',
  'bug',
  'test',
  'modify',
  'change',
  'edit',
  'write',
  'create',
  'delete',
  'remove',
  'rename',
  'migrat',
  'architect',
  'security',
  'research',
  'search',
  'external',
  'tool',
  'multiple',
  'multi-file',
  'long context',
  '调试',
  '修复',
  '错误',
  '失败',
  '故障',
  '测试',
  '修改',
  '更改',
  '编辑',
  '写入',
  '创建',
  '删除',
  '移除',
  '重命名',
  '迁移',
  '架构',
  '安全',
  '研究',
  '搜索',
  '外部',
  '工具',
  '多个',
  '多文件',
  '长上下文',
  '分析',
  '比较',
  '设计',
]

/**
 * Full-match a pattern the way Python's `re.fullmatch` does.
 *
 * @param pattern - a `d`-flagged regular expression.
 * @param text - the string to match.
 * @returns the match when the pattern consumes the whole string, else null.
 */
function fullMatch(pattern, text) {
  const match = pattern.exec(text)
  if (match === null) return null
  return match.index === 0 && match[0].length === text.length ? match : null
}

/**
 * Collapse whitespace the way the Python reference does.
 *
 * @param value - the raw text.
 * @returns the trimmed, single-spaced text.
 */
export function normalizeText(value) {
  return value.trim().replace(/\s+/g, ' ')
}

/**
 * Count the capability levels a route removes.
 *
 * @param from - the current capability.
 * @param to - the target capability.
 * @returns the number of levels removed, never negative.
 */
export function downgradeSteps(from, to) {
  return Math.max((CAPABILITY_RANK[from] ?? 0) - (CAPABILITY_RANK[to] ?? 0), 0)
}

/**
 * Apply the fixed one-level downgrade guard.
 *
 * @param from - the current capability.
 * @param to - the target capability.
 * @returns whether the downgrade is allowed.
 */
export function isDowngradeAllowed(from, to) {
  return downgradeSteps(from, to) <= MAXIMUM_DOWNGRADE_STEPS
}

/**
 * Build an unmatched result.
 *
 * @param reason - why the rule did not apply.
 * @param ruleId - the rule that declined, when one was reached.
 * @returns the rule result.
 */
function unmatched(reason, ruleId = null) {
  return { matched: false, rule_id: ruleId, confidence: 0.0, capability: null, reason }
}

/**
 * Apply the confidence and downgrade guards to one rule candidate.
 *
 * @param candidate - `{ ruleId, confidence, capability, reason, currentCapability }`.
 * @returns the guarded rule result.
 */
export function evaluateRuleCandidate({
  ruleId,
  confidence,
  capability,
  reason,
  currentCapability = null,
}) {
  if (confidence < HIGH_CONFIDENCE_THRESHOLD) {
    return unmatched(
      `${ruleId} confidence=${confidence.toFixed(2)} is below the ` +
        'high-confidence guard; defer to JEV.',
      ruleId,
    )
  }
  if (currentCapability !== null && !isDowngradeAllowed(currentCapability, capability)) {
    return unmatched(
      `${ruleId} rejected: maximum_downgrade_steps=1 forbids ` +
        `${currentCapability}->${capability}; defer to JEV.`,
      ruleId,
    )
  }
  return {
    matched: true,
    rule_id: ruleId,
    confidence,
    capability,
    reason,
  }
}

/**
 * Evaluate the allowlisted rules for one task.
 *
 * @param prompt - the task text.
 * @param options - `{ context, currentCapability }`.
 * @returns the rule result; unmatched explicitly defers to JEV.
 */
export function evaluateLightweightRules(prompt, options = {}) {
  const { context = [], currentCapability = null } = options
  const normalized = normalizeText(prompt)

  const complexityReason = complexityGuard(context, semanticPrompt(normalized))
  if (complexityReason !== null) return unmatched(complexityReason)

  const candidate =
    matchReadRule(context, normalized) ?? matchListRule(context, normalized)
  if (candidate === null) {
    return unmatched('No whitelisted lightweight rule matched; defer to JEV.')
  }

  return evaluateRuleCandidate({
    ruleId: candidate.ruleId,
    confidence: candidate.confidence,
    capability: LIGHTWEIGHT_CAPABILITY,
    reason: candidate.reason,
    currentCapability,
  })
}

/**
 * Remove an identified path before checking semantic complexity markers.
 *
 * @param prompt - the normalized prompt.
 * @returns the prompt with any matched file path blanked out.
 */
function semanticPrompt(prompt) {
  for (const pattern of READ_PATTERNS) {
    const match = fullMatch(pattern, prompt)
    if (match === null) continue
    const span = match.indices?.groups?.path
    if (span === undefined) continue
    return `${prompt.slice(0, span[0])} ${prompt.slice(span[1])}`
  }
  return prompt
}

/**
 * Veto anything that looks like more than a trivial read.
 *
 * @param context - the request context lines.
 * @param prompt - the semantic prompt.
 * @returns a veto reason, or null when the task may proceed.
 */
function complexityGuard(context, prompt) {
  if (prompt.length > 240) {
    return 'Task prompt is too long for a lightweight rule; defer to JEV.'
  }
  if (context.length > 1) {
    return 'Task context has multiple items; defer to JEV.'
  }
  if (context.length > 0 && String(context[0]).length > 240) {
    return 'Task context is too long for a lightweight rule; defer to JEV.'
  }
  const lowered = prompt.toLowerCase()
  const marker = COMPLEX_MARKERS.find((item) => lowered.includes(item))
  if (marker !== undefined) {
    return `Complexity marker '${marker}' detected; defer to JEV.`
  }
  return null
}

/**
 * Match the single-file read rule.
 *
 * @param context - the request context lines.
 * @param prompt - the normalized prompt.
 * @returns the candidate, or null.
 */
function matchReadRule(context, prompt) {
  if (GENERIC_READ_PATTERNS.some((pattern) => fullMatch(pattern, prompt) !== null)) {
    const contextPath = contextPathOf(context)
    if (contextPath === null) return null
    return {
      ruleId: 'simple_file_read',
      confidence: 0.99,
      reason: `明确读取单个文件 ${contextPath}，只读、无修改`,
    }
  }

  const path = extractPath(READ_PATTERNS, prompt)
  if (path === null) return null
  if (!isExplicitFilePath(path)) return null
  const fromContext = contextPathOf(context)
  if (context.length > 0 && fromContext !== path) return null
  return {
    ruleId: 'simple_file_read',
    confidence: 0.99,
    reason: `明确读取单个文件 ${path}，只读、无修改`,
  }
}

/**
 * Match the directory-listing rule.
 *
 * @param context - the request context lines.
 * @param prompt - the normalized prompt.
 * @returns the candidate, or null.
 */
function matchListRule(context, prompt) {
  if (context.length > 0) return null
  if (!LIST_PATTERNS.some((pattern) => fullMatch(pattern, prompt) !== null)) return null
  return {
    ruleId: 'simple_file_list',
    confidence: 0.99,
    reason: '明确列出当前目录文件，只读、无修改',
  }
}

/**
 * Read the named `path` group from the first pattern that fully matches.
 *
 * @param patterns - the candidate patterns.
 * @param prompt - the normalized prompt.
 * @returns the captured path, or null.
 */
function extractPath(patterns, prompt) {
  for (const pattern of patterns) {
    const match = fullMatch(pattern, prompt)
    if (match !== null) return match.groups.path
  }
  return null
}

/**
 * Read the single context entry when it is an explicit file path.
 *
 * @param context - the request context lines.
 * @returns the path, or null.
 */
function contextPathOf(context) {
  if (context.length !== 1) return null
  const candidate = String(context[0]).trim()
  return isExplicitFilePath(candidate) ? candidate : null
}

/**
 * Whether a path is both safe and recognizably a file path.
 *
 * @param path - the candidate path.
 * @returns whether the path may be routed by the read rule.
 */
export function isExplicitFilePath(path) {
  if (typeof path !== 'string') return false
  if (fullMatch(new RegExp(`^(?:${SIMPLE_PATH})$`), path) === null) return false

  const segments = path.split('/')
  if (segments.some((segment) => segment === '.' || segment === '..')) return false
  if (segments.some((segment) => segment.startsWith('.'))) return false

  const basename = segments[segments.length - 1]
  if (path.includes('/')) return true
  if (EXTENSIONLESS_FILE_NAMES.has(basename)) return true
  if (basename.includes('.') && !basename.endsWith('.')) return true
  return false
}

/**
 * The classifier result the frozen Router synthesizes from a matched rule.
 *
 * Mirrors `_classifier_from_rule` in `src/jev_router/router.py`, so a
 * light-rule route produces exactly the same classifier shape the Python
 * Router would, and the same policy then chooses the model.
 *
 * @param rule - a matched rule result.
 * @returns the synthetic classifier result.
 */
export function classifierFromRule(rule) {
  if (!rule.matched || rule.capability === null) {
    throw new Error('a matched lightweight rule must provide capability')
  }
  return {
    schema_version: '0.1',
    task_type: 'file_operation',
    difficulty_score: 1,
    difficulty_bucket: 'low',
    required_capability: rule.capability,
    confidence: rule.confidence,
    risk_level: 'low',
    notes: null,
  }
}
