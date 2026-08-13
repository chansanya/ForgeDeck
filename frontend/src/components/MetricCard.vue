<!-- 指标卡片组件：展示带语义色调的核心数值与变化说明。 -->
<template>
  <article class="metric-card panel" :data-tone="tone">
    <div class="metric-icon">
      <NIcon :component="icon" :size="19" />
    </div>
    <div>
      <p class="metric-label">{{ label }}</p>
      <strong>{{ value }}</strong>
      <p v-if="caption" class="metric-caption">{{ caption }}</p>
    </div>
  </article>
</template>

<script setup lang="ts">
import type { Component } from 'vue'
import { NIcon } from 'naive-ui'

withDefaults(
  defineProps<{
    label: string
    value: string | number
    caption?: string
    tone?: 'lime' | 'blue' | 'red' | 'amber'
    icon: Component
  }>(),
  { tone: 'lime', caption: '' },
)
</script>

<style scoped>
.metric-card {
  display: grid;
  grid-template-columns: auto 1fr;
  gap: 14px;
  min-height: 126px;
  padding: 20px;
}

.metric-icon {
  display: grid;
  width: 39px;
  height: 39px;
  border: 1px solid color-mix(in srgb, var(--tone) 35%, transparent);
  border-radius: 10px;
  color: var(--tone);
  background: color-mix(in srgb, var(--tone) 10%, transparent);
  place-items: center;
}

.metric-card[data-tone='lime'] {
  --tone: #c7ff4a;
}
.metric-card[data-tone='blue'] {
  --tone: #5ea1ff;
}
.metric-card[data-tone='red'] {
  --tone: #ff637d;
}
.metric-card[data-tone='amber'] {
  --tone: #f5b942;
}

.metric-label,
.metric-caption {
  margin: 0;
}

.metric-label {
  color: #8995a4;
  font-size: 12px;
  letter-spacing: 0.03em;
}

strong {
  display: block;
  margin-top: 6px;
  font-family: 'JetBrains Mono', Consolas, monospace;
  font-size: 28px;
  font-weight: 650;
  letter-spacing: -0.045em;
}

.metric-caption {
  margin-top: 7px;
  color: #687483;
  font-size: 11px;
}
</style>
