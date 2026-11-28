<script setup lang="ts">
/** 一段给人看的文字：里面提到的任务编号（如「等 T-73 的结果」）变成可点的链接，旁边带状态。 */
import { computed } from 'vue'
import WorkTaskCode from './WorkTaskCode.vue'

const props = defineProps<{ text: string; linked?: Record<string, { title: string; status: string }> }>()
// 与后端 _CODE 一致：前后可以紧挨中文，不能是别的编号的一部分。
const CODE = /(?<![A-Za-z0-9_-])(T-\d+)(?!\d)/
const parts = computed(() =>
  (props.text || '').split(CODE).map((piece, i) => {
    const info = i % 2 === 1 ? props.linked?.[piece] : undefined
    return info ? { code: piece, ...info } : { text: piece }
  }),
)
</script>

<template>
  <span class="task-text">
    <template
      v-for="(part, i) in parts"
      :key="i"
    >
      <WorkTaskCode
        v-if="'code' in part"
        :code="part.code"
        :status="part.status"
        :title="part.title"
      />
      <template v-else>{{ part.text }}</template>
    </template>
  </span>
</template>
