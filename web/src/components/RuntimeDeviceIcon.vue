<script setup lang="ts">
import { computed } from 'vue'
import { runtimeDevice } from '@/utils/runtimeDevice'

const props = defineProps<{ name: string | null | undefined }>()
const device = computed(() => runtimeDevice(props.name))
const TITLES = { 'mac-studio': 'Mac Studio', 'mac-mini': 'Mac mini', server: 'Server' } as const
</script>

<template>
  <svg
    class="runtime-device"
    :data-device="device"
    viewBox="0 0 24 24"
    width="18"
    height="18"
    fill="none"
    stroke="currentColor"
    stroke-width="1.6"
    stroke-linecap="round"
    stroke-linejoin="round"
    role="img"
    :aria-label="TITLES[device]"
  >
    <title>{{ TITLES[device] }}</title>
    <!-- Mac Studio：方正的矮立方体，正面左下两个口、右侧读卡槽，底座内缩。 -->
    <template v-if="device === 'mac-studio'">
      <rect
        x="3"
        y="5"
        width="18"
        height="13"
        rx="2.6"
      />
      <path d="M6.5 14.5v1.5M8.8 14.5v1.5M14.5 15.2h3" />
      <path d="M6 20h12" />
    </template>
    <!-- Mac mini：扁平的薄片，右侧一颗指示灯。 -->
    <template v-else-if="device === 'mac-mini'">
      <rect
        x="2.5"
        y="10"
        width="19"
        height="5.5"
        rx="2"
      />
      <circle
        cx="18"
        cy="12.75"
        r="0.6"
        fill="currentColor"
      />
      <path d="M6 18h12" />
    </template>
    <!-- 其他：两层机架服务器。 -->
    <template v-else>
      <rect
        x="3.5"
        y="4"
        width="17"
        height="7"
        rx="1.5"
      />
      <rect
        x="3.5"
        y="13"
        width="17"
        height="7"
        rx="1.5"
      />
      <path d="M7 7.5h.01M7 16.5h.01M11 7.5h6M11 16.5h6" />
    </template>
  </svg>
</template>

<style scoped>
.runtime-device { flex: none; color: var(--el-text-color-regular); }
</style>
