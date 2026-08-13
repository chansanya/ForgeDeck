<!-- 业务页面标题组件：统一标题层级、辅助说明和操作插槽。 -->
<template>
  <header class="page-header">
    <div>
      <p v-if="eyebrow" class="eyebrow">{{ eyebrow }}</p>
      <h1>{{ title }}</h1>
      <p v-if="description" class="description">{{ description }}</p>
    </div>
    <div v-if="$slots.actions" class="page-actions">
      <slot name="actions" />
    </div>
  </header>
</template>

<script setup lang="ts">
defineProps<{
  eyebrow?: string
  title: string
  description?: string
}>()
</script>

<style scoped>
.page-header {
  display: flex;
  align-items: flex-end;
  justify-content: space-between;
  gap: 24px;
  margin-bottom: 26px;
}

h1 {
  margin: 0;
  font-size: clamp(28px, 3vw, 42px);
  font-weight: 720;
  letter-spacing: -0.045em;
  line-height: 1.05;
}

.description {
  max-width: 720px;
  margin: 10px 0 0;
  color: #8995a4;
  font-size: 14px;
  line-height: 1.65;
}

.page-actions {
  display: flex;
  flex: 0 0 auto;
  flex-wrap: wrap;
  gap: 10px;
}

@media (max-width: 700px) {
  .page-header {
    align-items: flex-start;
    flex-direction: column;
  }
}
</style>
