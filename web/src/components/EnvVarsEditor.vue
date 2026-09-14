<script setup lang="ts">
import { ref, watch } from 'vue'
import { useI18n } from 'vue-i18n'

type Mode = 'kv' | 'raw' | 'json'
interface Row { key: string; value: string }

const props = defineProps<{ modelValue: Record<string, string>; readonly?: boolean }>()
const emit = defineEmits<{ 'update:modelValue': [Record<string, string>]; invalid: [string] }>()
const { t } = useI18n()

const mode = ref<Mode>('kv')
const rows = ref<Row[]>([])
const rawText = ref('')
const jsonText = ref('')

function toRows(v: Record<string, string>): Row[] {
  return Object.entries(v).map(([key, value]) => ({ key, value }))
}

function toRaw(v: Record<string, string>): string {
  return Object.entries(v).map(([k, val]) => `${k}=${val}`).join('\n')
}

function fromRows(list: Row[]): Record<string, string> {
  return Object.fromEntries(list.filter((r) => r.key.trim()).map((r) => [r.key.trim(), r.value]))
}

/** 每行一个 `KEY=VALUE`；空行与 `#` 开头的注释跳过；只按第一个 `=` 切，值里可以再有 `=`。 */
function parseRaw(text: string): Record<string, string> {
  const out: Record<string, string> = {}
  for (const line of text.split('\n')) {
    const s = line.trim()
    if (!s || s.startsWith('#')) continue
    const i = s.indexOf('=')
    if (i <= 0) throw new Error(s)
    out[s.slice(0, i).trim()] = s.slice(i + 1)
  }
  return out
}

function parseJson(text: string): Record<string, string> {
  const parsed: unknown = JSON.parse(text)
  if (!parsed || typeof parsed !== 'object' || Array.isArray(parsed)) throw new Error('object')
  return Object.fromEntries(Object.entries(parsed as Record<string, unknown>).map(([k, val]) => [k, String(val)]))
}

function syncAll(v: Record<string, string>): void {
  rows.value = toRows(v)
  rawText.value = toRaw(v)
  jsonText.value = JSON.stringify(v, null, 2)
}

syncAll(props.modelValue)

/** 当前编辑区代表的值；解析不出来返回 null。 */
function currentValue(): Record<string, string> | null {
  try {
    if (mode.value === 'raw') return parseRaw(rawText.value)
    if (mode.value === 'json') return parseJson(jsonText.value)
    return fromRows(rows.value)
  } catch {
    return null
  }
}

// 父组件换掉整个值（重置表单、载入详情）时重建编辑区；自己 emit 回去又流回来的值不重建，
// 否则每提交一次，raw 模式里的注释和空行都会被抹掉。
watch(() => props.modelValue, (v) => {
  const cur = currentValue()
  if (cur && JSON.stringify(cur) === JSON.stringify(v)) return
  syncAll(v)
})

// 切换模式时按当前 model 重新生成目标模式的文本（未提交的编辑会被丢弃：
// 浏览器里点按钮会先让文本域失焦并提交，所以只有 jsdom 直接点按钮才碰得到）。
function switchMode(next: Mode): void {
  syncAll(props.modelValue)
  mode.value = next
}

function commitRows(): void {
  emit('update:modelValue', fromRows(rows.value))
}

function commitRaw(): void {
  try {
    emit('update:modelValue', parseRaw(rawText.value))
  } catch (e) {
    emit('invalid', t('bots.envInvalid') + (e instanceof Error ? `: ${e.message}` : ''))
  }
}

function commitJson(): void {
  try {
    emit('update:modelValue', parseJson(jsonText.value))
  } catch {
    emit('invalid', t('bots.envInvalid'))
  }
}

function addRow(): void {
  rows.value.push({ key: '', value: '' })
}

function removeRow(i: number): void {
  rows.value.splice(i, 1)
  commitRows()
}
</script>

<template>
  <div class="env-vars-editor">
    <!-- el-radio-button 的根是 <label>，jsdom 里点它不一定能转发到内部 radio；
         测试直接 trigger('click')，所以模式切换用按钮组而不是 radio 组。 -->
    <el-button-group
      v-if="!readonly"
      class="mode-switch"
    >
      <el-button
        size="small"
        data-test="mode-kv"
        :type="mode === 'kv' ? 'primary' : undefined"
        @click="switchMode('kv')"
      >
        {{ t('bots.envMode.kv') }}
      </el-button>
      <el-button
        size="small"
        data-test="mode-raw"
        :type="mode === 'raw' ? 'primary' : undefined"
        @click="switchMode('raw')"
      >
        {{ t('bots.envMode.raw') }}
      </el-button>
      <el-button
        size="small"
        data-test="mode-json"
        :type="mode === 'json' ? 'primary' : undefined"
        @click="switchMode('json')"
      >
        {{ t('bots.envMode.json') }}
      </el-button>
    </el-button-group>

    <template v-if="readonly || mode === 'kv'">
      <div
        v-for="(row, i) in rows"
        :key="i"
        class="env-row"
      >
        <!-- el-input 是 inheritAttrs: false，data-test 直接写在它身上会落到 <input> 上，
             `[data-test=x] input` 就选不到了；统一挂在外层容器上。 -->
        <div
          class="env-cell"
          :data-test="'row-key-' + i"
        >
          <el-input
            v-model="row.key"
            :disabled="readonly"
            :placeholder="t('bots.envKey')"
            @input="commitRows"
          />
        </div>
        <div
          class="env-cell"
          :data-test="'row-value-' + i"
        >
          <el-input
            v-model="row.value"
            :disabled="readonly"
            :placeholder="t('bots.envValue')"
            @input="commitRows"
          />
        </div>
        <el-button
          v-if="!readonly"
          type="danger"
          plain
          :data-test="'row-del-' + i"
          @click="removeRow(i)"
        >
          {{ t('common.delete') }}
        </el-button>
      </div>
      <span
        v-if="!rows.length"
        class="muted"
      >—</span>
      <el-button
        v-if="!readonly"
        size="small"
        data-test="add-row"
        @click="addRow"
      >
        {{ t('bots.envAddRow') }}
      </el-button>
    </template>

    <div
      v-if="!readonly && mode === 'raw'"
      data-test="raw-text"
    >
      <el-input
        v-model="rawText"
        type="textarea"
        :rows="8"
        placeholder="KEY=VALUE"
        @blur="commitRaw"
      />
    </div>

    <div
      v-if="!readonly && mode === 'json'"
      data-test="json-text"
    >
      <el-input
        v-model="jsonText"
        type="textarea"
        :rows="8"
        placeholder="{}"
        @blur="commitJson"
      />
    </div>
  </div>
</template>

<style scoped>
.env-vars-editor { width: 100%; }
.mode-switch { margin-bottom: 8px; }
.env-row { display: flex; gap: 8px; margin-bottom: 8px; }
.env-cell { flex: 1; min-width: 0; }
.muted { color: var(--el-text-color-secondary); font-size: 12px; margin-right: 8px; }
</style>
