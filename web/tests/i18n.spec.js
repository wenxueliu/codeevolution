import { afterEach, describe, expect, it } from 'vitest'

import { locale, setLocale, t } from '../src/i18n.js'

afterEach(() => {
  setLocale('zh')
  window.localStorage.clear()
})

describe('web internationalization', () => {
  it('uses Chinese by default and supports interpolated messages', () => {
    setLocale('zh')
    expect(t('Graph View')).toBe('图谱视图')
    expect(t('选择 {name}', { name: 'mall' })).toBe('选择 mall')
  })

  it('switches the core navigation and page vocabulary to English', () => {
    setLocale('en')
    expect(locale.value).toBe('en')
    expect(t('知识中心')).toBe('Knowledge Center')
    expect(t('创建当前 View')).toBe('Create current view')
    expect(t('共 {count} 条', { count: 3 })).toBe('3 total')
  })

  it('persists the selected locale for the next browser session', () => {
    setLocale('en')
    expect(window.localStorage.getItem('codeevolution:locale')).toBe('en')
  })
})
