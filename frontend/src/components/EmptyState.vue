<!-- 通用空状态组件：统一展示 Lucide 图标、说明和可选操作入口。 -->
<template>
  <div class="empty-state">
    <span class="icon-wrap"><NIcon :component="icon" :size="24" /></span>
    <h3>{{ title }}</h3>
    <p>{{ description }}</p>
    <NButton v-if="actionLabel" tertiary type="primary" @click="$emit('action')">{{
      actionLabel
    }}</NButton>
  </div>
</template>

<script setup lang="ts">
import type { Component } from 'vue'
import { NButton, NIcon } from 'naive-ui'

defineProps<{
  icon: Component
  title: string
  description: string
  actionLabel?: string
}>()

defineEmits<{ action: [] }>()
</script>

<style scoped>
.empty-state {
  display: grid;
  min-height: 260px;
  padding: 34px;
  text-align: center;
  place-items: center;
  align-content: center;
}

.icon-wrap {
  display: grid;
  width: 50px;
  height: 50px;
  border: 1px solid #293440;
  border-radius: 14px;
  color: #9ba8b7;
  background: #111923;
  place-items: center;
}

h3 {
  margin: 14px 0 4px;
  font-size: 15px;
}
p {
  max-width: 420px;
  margin: 0 0 16px;
  color: #758292;
  font-size: 13px;
  line-height: 1.6;
}
</style>
