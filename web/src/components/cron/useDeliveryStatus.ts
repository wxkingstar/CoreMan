import { useI18n } from 'vue-i18n'

/** 通知投递状态文案：没有对应翻译的状态原样显示，空值显示破折号。 */
export function useDeliveryStatus(): (status: string) => string {
  const { t } = useI18n()
  return (status: string) => {
    if (!status) return '—'
    const key = `notification.status.${status}`
    return t(key) === key ? status : t(key)
  }
}
