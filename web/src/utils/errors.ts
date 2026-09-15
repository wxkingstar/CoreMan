import { ApiError, type FieldError } from '@/api/client'
import { i18n } from '@/i18n'

/** FastAPI 的 loc 以请求位置开头（body/query/path…），它不是字段名。 */
const LOCATIONS = new Set(['body', 'query', 'path', 'header', 'cookie'])
/** pydantic 在联合类型与校验器外层插入的类型标签，例如 `function-after[...]`、`str`、`list[str]`。 */
const TYPE_TAG = /[[\](),]|^(str|int|float|bool|bytes|dict|list|tuple|set|none|decimal|uuid|date|datetime|literal|url|json)$/i
/** 后端自定义校验器抛出的 ValueError / AssertionError 会被 pydantic 加上这层英文前缀。 */
const MSG_PREFIX = /^(Value error|Assertion failed),\s*/
/** 一条提示里最多列几项，其余折叠成「等 N 项」。 */
const MAX_LISTED = 3
/**
 * 乐观锁冲突的后端文案：`coreman/api/versioning.py::require_if_match`、StaleDataError 处理器
 * （`coreman/api/errors.py`）与换机领域服务（`coreman/core/bots/switch_relay.py`）。
 * 其它 409（工作目录被占用、飞书应用已分配、记录已存在）必须把后端原话给用户，不能一律当成并发冲突。
 */
const VERSION_CONFLICT = /已被(他人|其他操作)修改/

/** 字段名 → 界面上的表单标签。 */
export type FieldLabels = Record<string, string>

function meaningfulPath(loc: FieldError['loc']): string[] {
  return loc.filter((seg): seg is string => typeof seg === 'string' && !LOCATIONS.has(seg) && !TYPE_TAG.test(seg))
}

/** loc 里最后一个有意义的段；给了标签表时，优先取表里有的那一段（`env_vars.FOO` 落到 `env_vars`）。 */
export function fieldKey(loc: FieldError['loc'], labels?: FieldLabels): string | null {
  const path = meaningfulPath(loc)
  if (labels) {
    for (let i = path.length - 1; i >= 0; i--) if (path[i] in labels) return path[i]
  }
  return path[path.length - 1] ?? null
}

function cleanMsg(msg: string): string {
  return msg.replace(MSG_PREFIX, '')
}

/** 422 明细；不是后端校验错误时为空数组。 */
export function validationErrors(e: unknown): FieldError[] {
  return e instanceof ApiError ? e.errors : []
}

/** 表单回填用：字段名 → 原因（同一字段只保留第一条）。 */
export function fieldErrorMap(e: unknown, labels?: FieldLabels): Record<string, string> {
  const out: Record<string, string> = {}
  for (const err of validationErrors(e)) {
    const key = fieldKey(err.loc, labels)
    if (key && !(key in out)) out[key] = cleanMsg(err.msg)
  }
  return out
}

/** 统一的错误提示文本：422 带明细时拼成「字段：原因」，否则用后端 message。 */
export function errorMessage(e: unknown, labels?: FieldLabels): string {
  const errors = validationErrors(e)
  if (!errors.length) return e instanceof Error ? e.message : String(e)
  const t = i18n.global.t
  const lines = [...new Set(errors.map((err) => {
    const key = fieldKey(err.loc, labels)
    const detail = cleanMsg(err.msg)
    return key ? t('common.fieldError', { field: labels?.[key] ?? key, detail }) : detail
  }))]
  const text = lines.slice(0, MAX_LISTED).join(t('common.errorSeparator'))
  return lines.length > MAX_LISTED ? `${text} ${t('common.moreErrors', { count: lines.length })}` : text
}

/** 只有乐观锁版本冲突才该提示「已被他人修改，请刷新」。 */
export function isVersionConflict(e: unknown): boolean {
  return e instanceof ApiError && e.status === 409 && VERSION_CONFLICT.test(e.message)
}
