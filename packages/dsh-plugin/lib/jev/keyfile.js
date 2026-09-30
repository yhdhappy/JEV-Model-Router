/**
 * JEV API key extraction from a local key file.
 *
 * A line-by-line port of `_rtf_to_text` / `_read_api_key` in the Python
 * reference implementation (`src/jev_router/real_jev.py`), so the same macOS
 * TextEdit `.rtf` key file keeps working unchanged. `tests/parity/` proves the
 * two implementations return the same key for the same file.
 *
 * SECURITY: the resolved key is returned to the caller only. It is never
 * logged, never written to disk, and never echoed by this module.
 *
 * @module dsh-plugin-jev-router/jev/keyfile
 */

import { readFileSync } from 'node:fs'

/** Standard RTF metadata destinations whose contents are not visible text. */
const SKIP_GROUPS = new Set([
  'fonttbl',
  'colortbl',
  'stylesheet',
  'info',
  'pict',
  'object',
])

const LETTER = /\p{L}/u
const DIGIT = /\p{Nd}/u

/**
 * Extract visible text from an RTF document.
 *
 * @param value - the raw RTF source.
 * @returns the concatenated, whitespace-collapsed visible text.
 */
export function rtfToText(value) {
  const output = []
  const skipped = [false]
  let skipFallback = false
  let pendingIgnoredDestination = false
  let index = 0

  while (index < value.length) {
    const character = value[index]

    if (character === '{') {
      skipped.push(skipped[skipped.length - 1])
      index += 1
      continue
    }
    if (character === '}') {
      if (skipped.length > 1) skipped.pop()
      index += 1
      continue
    }
    if (skipFallback) {
      if (character === '\\' && index + 1 < value.length) {
        index += 2
        if (index < value.length && value[index - 1] === "'") index += 2
      } else {
        index += 1
      }
      skipFallback = false
      continue
    }
    if (character !== '\\') {
      if (!skipped[skipped.length - 1]) output.push(character)
      index += 1
      continue
    }

    index += 1
    if (index >= value.length) break
    const symbol = value[index]

    if (symbol === '\\' || symbol === '{' || symbol === '}') {
      if (!skipped[skipped.length - 1]) output.push(symbol)
      index += 1
      continue
    }
    if (symbol === "'" && index + 2 < value.length) {
      const hex = value.slice(index + 1, index + 3)
      const decoded = /^[0-9a-fA-F]{2}$/.test(hex)
        ? String.fromCharCode(parseInt(hex, 16))
        : ''
      if (!skipped[skipped.length - 1]) output.push(decoded)
      index += 3
      continue
    }
    if (symbol === '*') {
      pendingIgnoredDestination = true
      index += 1
      continue
    }
    if (symbol === '~' || symbol === '_' || symbol === '-') {
      if (!skipped[skipped.length - 1] && (symbol === '~' || symbol === '-')) {
        output.push(symbol === '~' ? ' ' : '-')
      }
      index += 1
      continue
    }

    const wordStart = index
    while (index < value.length && LETTER.test(value[index])) index += 1
    const word = value.slice(wordStart, index)
    if (index < value.length && (value[index] === '+' || value[index] === '-')) {
      index += 1
    }
    while (index < value.length && DIGIT.test(value[index])) index += 1
    if (index < value.length && value[index] === ' ') index += 1

    if (pendingIgnoredDestination || SKIP_GROUPS.has(word)) {
      skipped[skipped.length - 1] = true
      pendingIgnoredDestination = false
      continue
    }

    if (word === 'u') {
      // RTF Unicode escapes carry one fallback character, which is not part of
      // the visible text when the code point is used.
      const raw = value.slice(wordStart + 1, index).trim()
      if (/^[+-]?\d+$/.test(raw)) {
        let codePoint = Number.parseInt(raw, 10)
        if (codePoint < 0) codePoint += 65536
        const isSurrogate = codePoint >= 0xd800 && codePoint <= 0xdfff
        if (codePoint <= 0x10ffff && !isSurrogate && !skipped[skipped.length - 1]) {
          output.push(String.fromCodePoint(codePoint))
        }
      }
      skipFallback = true
    } else if (!skipped[skipped.length - 1] && (word === 'par' || word === 'line')) {
      output.push('\n')
    } else if (!skipped[skipped.length - 1] && word === 'tab') {
      output.push('\t')
    }
  }

  return output.join('').replace(/\s+/g, ' ').trim()
}

/**
 * Resolve the API key from a key-file path.
 *
 * Accepts a plain text file or an RTF file, holding either a labelled key
 * (`JEV_API_KEY: xxx`, `TYPESAFE_API_KEY=xxx`, ...) or nothing but the key.
 *
 * @param path - absolute path to the key file.
 * @returns the resolved key.
 * @throws if the file cannot be read or holds no key.
 */
export function readApiKey(path) {
  let raw
  try {
    raw = readFileSync(path)
  } catch {
    throw new Error(`JEV API key file could not be read: ${path}`)
  }

  let text
  try {
    text = new TextDecoder('utf-8', { fatal: true }).decode(raw)
  } catch {
    throw new Error('JEV API key file is not valid text')
  }

  const plain = text.slice(0, 32).toLowerCase().includes('{\\rtf')
    ? rtfToText(text)
    : text

  const labelled =
    /(?:^|\b)(?:JEV_API_KEY|TYPESAFE_API_KEY|API_KEY)\b\s*[:=]\s*["']?([^\s"';}]+)/im.exec(
      plain,
    )
  let key = labelled ? labelled[1].trim() : ''

  if (!key) {
    const candidates = plain.trim().split(/\s+/).filter(Boolean)
    key = candidates.length === 1 ? candidates[0] : ''
  }

  if (!key) throw new Error('JEV API key file does not contain a key')
  return key
}
