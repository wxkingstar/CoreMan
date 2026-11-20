/** 日期串是 YYYY-MM-DD，按字典序比就是按日期比；「今天」以后端给的所有者时区的今天为准。 */
export const laterThan = (value: string, today: string) => /^\d{4}-\d{2}-\d{2}$/.test(value ?? '') && value > today
