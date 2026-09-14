<script setup lang="ts">
import type { DeptNode } from '@/api/types'

defineProps<{ nodes: DeptNode[] }>()
</script>

<template>
  <el-tree
    :data="nodes"
    :props="{ label: 'name', children: 'children' }"
    node-key="id"
    default-expand-all
  >
    <template #default="{ data }: { data: DeptNode }">
      <span>{{ data.name }} ({{ data.member_count }})</span>
    </template>
  </el-tree>
</template>
