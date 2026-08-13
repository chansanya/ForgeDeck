/* 验证通用展示格式化函数的边界行为。 */

import { describe, expect, it } from 'vitest'

import { formatBytes, formatDuration, maskHost, percent, shortSha } from './format'

describe('format helpers', () => {
  it('formats byte values without inventing precision for bytes', () => {
    expect(formatBytes(0)).toBe('0 B')
    expect(formatBytes(1024)).toBe('1.0 KiB')
    expect(formatBytes(null)).toBe('—')
  })

  it('formats bounded durations and percentages', () => {
    expect(formatDuration(-2)).toBe('0s')
    expect(formatDuration(65)).toBe('1m 5s')
    expect(formatDuration(3665)).toBe('1h 1m')
    expect(percent(150, 100)).toBe(100)
    expect(percent(25, 100)).toBe(25)
  })

  it('shortens identifiers and masks mail-like hosts', () => {
    expect(shortSha('0123456789abcdef')).toBe('01234567')
    expect(maskHost('deployer@example.com')).toBe('de***@example.com')
    expect(maskHost('10.0.0.8')).toBe('10.0.0.8')
  })
})
