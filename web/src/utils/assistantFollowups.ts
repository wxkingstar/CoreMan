import { ElMessageBox } from 'element-plus'

/** 日期串是 YYYY-MM-DD，按字典序比就是按日期比；「今天」以后端给的所有者时区的今天为准。 */
export const laterThan = (value: string, today: string) => /^\d{4}-\d{2}-\d{2}$/.test(value ?? '') && value > today

/** 问推后到哪一天：总表与待跟进共用同一个对话框。取消时抛出 'cancel' / 'close'。 */
export async function askSnoozeDate(t: (key: string) => string, today: string): Promise<string> {
  const { value } = await ElMessageBox.prompt(t('assistant.followups.snoozePrompt'), t('assistant.followups.snooze'), {
    inputType: 'date',
    inputValidator: (input: string) => laterThan(input, today) || t('assistant.followups.snoozeInvalid'),
  })
  return value
}
