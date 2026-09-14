import { ElMessageBox } from 'element-plus'
import { getCurrentInstance, onBeforeUnmount, onMounted } from 'vue'
import { onBeforeRouteLeave } from 'vue-router'
import { useI18n } from 'vue-i18n'

export function useUnsavedChanges(isDirty: () => boolean) {
  const { t } = useI18n()
  async function confirmDiscard() {
    if (!isDirty()) return true
    try { await ElMessageBox.confirm(t('workspace.unsaved'), t('common.confirm'), { type: 'warning', confirmButtonText: t('common.confirm'), cancelButtonText: t('common.cancel') }); return true } catch { return false }
  }
  function beforeUnload(event: BeforeUnloadEvent) { if (isDirty()) { event.preventDefault(); event.returnValue = '' } }
  if (getCurrentInstance()) {
    onMounted(() => window.addEventListener('beforeunload', beforeUnload))
    onBeforeUnmount(() => window.removeEventListener('beforeunload', beforeUnload))
    onBeforeRouteLeave(confirmDiscard)
  }
  return { confirmDiscard }
}
