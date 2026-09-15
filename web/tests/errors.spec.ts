import { describe, expect, it } from 'vitest'
import { ApiError, type FieldError } from '@/api/client'
import { errorMessage, fieldErrorMap, fieldKey, isVersionConflict } from '@/utils/errors'

const invalid = (errors: FieldError[]) => new ApiError(422, 422, '参数校验失败', errors)

describe('errorMessage', () => {
  it('uses the backend message when there are no field errors', () => {
    expect(errorMessage(new ApiError(409, 409, '该飞书应用已分配给另一机器人'))).toBe('该飞书应用已分配给另一机器人')
    expect(errorMessage(new Error('network down'))).toBe('network down')
    expect(errorMessage('cancel')).toBe('cancel')
  })

  // 以前 422 只显示「参数校验失败」，字段名与原因全被吞掉。
  it('joins 422 details as "field: reason" using the last meaningful loc segment', () => {
    const e = invalid([
      { loc: ['body', 'working_dir'], msg: 'String should have at least 1 character', type: 'string_too_short' },
      { loc: ['body', 'rules', 0, 'dept_path_contains'], msg: 'Field required', type: 'missing' },
      { loc: ['body', 'env_vars', 'function-after[_check(), dict[str,str]]'], msg: 'Value error, 环境变量名不合法', type: 'value_error' },
    ])
    expect(errorMessage(e)).toBe('working_dir：String should have at least 1 character；dept_path_contains：Field required；env_vars：环境变量名不合法')
  })

  it('maps field names to form labels, preferring a labelled segment', () => {
    const e = invalid([{ loc: ['body', 'env_vars', 'FOO-BAR'], msg: 'Value error, 变量名不合法', type: 'value_error' }])
    expect(fieldKey(e.errors[0].loc)).toBe('FOO-BAR')
    expect(errorMessage(e, { env_vars: '环境变量' })).toBe('环境变量：变量名不合法')
    expect(fieldErrorMap(e, { env_vars: '环境变量' })).toEqual({ env_vars: '变量名不合法' })
  })

  it('shows model-level errors without a field and folds long lists', () => {
    const e = invalid([
      { loc: ['body'], msg: 'Value error, 结束时间不能早于开始时间', type: 'value_error' },
      ...['a', 'b', 'c'].map((k) => ({ loc: ['body', k], msg: 'Field required', type: 'missing' })),
    ])
    expect(errorMessage(e)).toBe('结束时间不能早于开始时间；a：Field required；b：Field required 等 4 项')
    expect(fieldErrorMap(e)).toEqual({ a: 'Field required', b: 'Field required', c: 'Field required' })
  })
})

describe('isVersionConflict', () => {
  it('only matches optimistic-lock conflicts', () => {
    expect(isVersionConflict(new ApiError(409, 409, '记录已被他人修改，请刷新后重试'))).toBe(true)
    expect(isVersionConflict(new ApiError(409, 409, '机器人已被其他操作修改，请刷新后重试'))).toBe(true)
    expect(isVersionConflict(new ApiError(409, 409, '该实例工作目录已属于另一个机器人'))).toBe(false)
    expect(isVersionConflict(new ApiError(409, 409, '记录已存在、被其它记录引用或不满足约束；被引用的配置可先停用'))).toBe(false)
    expect(isVersionConflict(new ApiError(422, 422, '记录已被他人修改'))).toBe(false)
    expect(isVersionConflict(new Error('记录已被他人修改'))).toBe(false)
  })
})
