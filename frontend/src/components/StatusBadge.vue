<!-- 状态徽标组件：将后端状态映射为统一中文标签与视觉语义。 -->
<template>
  <span class="status" :data-tone="tone">
    <i aria-hidden="true" />
    {{ label }}
  </span>
</template>

<script setup lang="ts">
import { computed } from 'vue'

const props = defineProps<{ status?: string | null }>()

const normalized = computed(() => (props.status || 'unknown').toLowerCase())
const label = computed(
  () =>
    ({
      queued: '排队中',
      pending: '待处理',
      running: '运行中',
      success: '成功',
      succeeded: '成功',
      failed: '失败',
      cancelled: '已取消',
      canceling: '取消中',
      rolling_back: '回滚中',
      rolled_back: '已回滚',
      deploying: '部署中',
      executing: '执行中',
      online: '在线',
      offline: '离线',
      degraded: '异常',
      unknown: '未知',
      healthy: '健康',
      unhealthy: '不健康',
      approved: '已批准',
      rejected: '已拒绝',
      expired: '已过期',
      active: '有效',
      revoked: '已吊销',
      enabled: '已启用',
      disabled: '已停用',
      stopped: '已停止',
      exited: '已退出',
      created: '已创建',
    })[normalized.value] ||
    props.status ||
    '未知',
)

const tone = computed(() => {
  if (
    [
      'success',
      'succeeded',
      'online',
      'healthy',
      'approved',
      'active',
      'enabled',
      'running',
    ].includes(normalized.value)
  )
    return 'success'
  if (['failed', 'offline', 'unhealthy', 'rejected', 'revoked'].includes(normalized.value))
    return 'danger'
  if (
    [
      'pending',
      'queued',
      'canceling',
      'rolling_back',
      'deploying',
      'executing',
      'degraded',
    ].includes(normalized.value)
  )
    return 'warning'
  return 'neutral'
})
</script>

<style scoped>
.status {
  display: inline-flex;
  align-items: center;
  gap: 7px;
  min-width: max-content;
  padding: 4px 8px;
  border: 1px solid color-mix(in srgb, var(--status-color) 24%, transparent);
  border-radius: 999px;
  color: var(--status-color);
  background: color-mix(in srgb, var(--status-color) 8%, transparent);
  font-size: 11px;
  font-weight: 650;
}

.status i {
  width: 5px;
  height: 5px;
  border-radius: 50%;
  background: currentColor;
  box-shadow: 0 0 8px currentColor;
}

.status[data-tone='success'] {
  --status-color: #50d890;
}
.status[data-tone='danger'] {
  --status-color: #ff637d;
}
.status[data-tone='warning'] {
  --status-color: #f5b942;
}
.status[data-tone='neutral'] {
  --status-color: #8d99a8;
}
</style>
