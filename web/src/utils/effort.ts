import type { CatalogOut, EffortLevel } from '@/api/types'

/** 思考档位由低到高；与后端 `coreman/core/db/models/bots.py::EFFORT_LEVELS` 同序，改动要同步。 */
export const EFFORT_LEVELS: readonly EffortLevel[] = ['low', 'medium', 'high', 'xhigh', 'max']

/**
 * 模型支持的档位：xhigh / max 看目录标记，其余档位都支持。
 * 与后端 `supports_effort()` 同一口径：同一 model 可挂在多个 provider 下，目录里任一行支持即可。
 */
export function supportedEfforts(model: string, rows: readonly CatalogOut[]): EffortLevel[] {
  const mine = rows.filter((r) => r.model === model)
  return EFFORT_LEVELS.filter((lv) =>
    lv === 'xhigh' ? mine.some((r) => r.supports_xhigh)
      : lv === 'max' ? mine.some((r) => r.supports_max)
        : true)
}

/** 不超过 level 的最高已支持档位（与后端 `fit_effort()`、Claude Code 的降档方式一致）。 */
export function fitEffort(level: EffortLevel, supported: readonly EffortLevel[]): EffortLevel {
  const below = EFFORT_LEVELS.slice(0, EFFORT_LEVELS.indexOf(level) + 1)
  return [...below].reverse().find((lv) => supported.includes(lv)) ?? level
}
