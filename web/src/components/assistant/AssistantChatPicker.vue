<script setup lang="ts">
import { computed, onMounted, reactive, ref } from 'vue'
import { useI18n } from 'vue-i18n'
import { myAssistant, type ChatChoice } from '@/api/myAssistant'
import { errorMessage } from '@/utils/errors'

/** 按会话名挑会话：先列最近活跃的会话，输入时按名字或成员搜；值是 chat_id 的列表。 */
const model = defineModel<string[]>({ required: true })
const props = defineProps<{
  disabled?: boolean
  /** 只列群（重点群用）：单聊不出现在下拉里。 */
  groupsOnly?: boolean
  /** 最多选几个；0 或不给表示不限（超出交给表单校验）。 */
  limit?: number
}>()
const { t } = useI18n()

const options = ref<ChatChoice[]>([])
// 见过的会话名：名单里有、但这次清单里没列出来的会话，也尽量显示名字。
const known = reactive<Record<string, ChatChoice>>({})
const loading = ref(false), error = ref(''), loaded = ref(false)
let latest = 0

async function search(query: string) {
  const ticket = ++latest
  loading.value = true; error.value = ''
  try {
    const { items } = await myAssistant.chatChoices(query.trim())
    if (ticket !== latest) return
    for (const item of items) known[item.chat_id] = item
    options.value = props.groupsOnly ? items.filter((item) => item.kind === 'group') : items
    loaded.value = true
  } catch (e) { if (ticket === latest) error.value = errorMessage(e) }
  finally { if (ticket === latest) loading.value = false }
}
function label(choice: ChatChoice) {
  const kind = t(choice.kind === 'p2p' ? 'assistant.settings.chatP2p' : 'assistant.settings.chatGroup')
  return `${kind} · ${choice.name || choice.chat_id}`
}
/** 下拉里的选项：这次清单里的，加上已选而清单里没有的（没名字时显示 chat_id）。 */
const shown = computed(() => {
  const listed = new Set(options.value.map((item) => item.chat_id))
  const extra = model.value.filter((id) => !listed.has(id)).map((id) => known[id] ?? { chat_id: id, name: '', kind: 'group' as const })
  return [...extra, ...options.value]
})
function open(visible: boolean) { if (visible && !loaded.value && !loading.value) void search('') }
// 已有选中的会话时先取一次清单：已选的会话一打开页面就显示名字，而不是 chat_id。
onMounted(() => { if (model.value.length) void search('') })
defineExpose({ search, shown })
</script>
<template>
  <div class="chat-picker">
    <el-select
      v-model="model"
      multiple
      filterable
      remote
      :remote-method="search"
      :loading="loading"
      :disabled="disabled"
      :multiple-limit="limit ?? 0"
      :placeholder="t('assistant.settings.chatPickerPlaceholder')"
      :no-data-text="error || t('assistant.settings.chatPickerEmpty')"
      style="width: 100%"
      data-test="chat-picker"
      @visible-change="open"
    >
      <el-option
        v-for="choice in shown"
        :key="choice.chat_id"
        :label="label(choice)"
        :value="choice.chat_id"
      />
    </el-select>
  </div>
</template>
<style scoped>
.chat-picker { flex-basis: 100%; }
</style>
