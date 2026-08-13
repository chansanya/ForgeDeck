<!-- ECharts 趋势图容器：负责实例生命周期、尺寸监听和数据更新。 -->
<template>
  <div ref="root" role="img" aria-label="指标趋势图" :style="{ height: `${height}px` }" />
</template>

<script setup lang="ts">
import { onBeforeUnmount, onMounted, ref, watch } from 'vue'
import * as echarts from 'echarts/core'
import { LineChart, BarChart } from 'echarts/charts'
import { GridComponent, LegendComponent, TooltipComponent } from 'echarts/components'
import { CanvasRenderer } from 'echarts/renderers'
import type { EChartsCoreOption } from 'echarts/core'

echarts.use([LineChart, BarChart, GridComponent, LegendComponent, TooltipComponent, CanvasRenderer])

const props = withDefaults(
  defineProps<{
    option: EChartsCoreOption
    height?: number
  }>(),
  { height: 260 },
)

const root = ref<HTMLDivElement | null>(null)
let chart: echarts.ECharts | null = null
let observer: ResizeObserver | null = null

function render(): void {
  /** 根据最新指标配置 ECharts 实例，并在容器尺寸变化时刷新。 */
  if (!root.value) return
  chart ||= echarts.init(root.value, undefined, { renderer: 'canvas' })
  chart.setOption(props.option, { notMerge: true })
}

watch(() => props.option, render, { deep: true })

onMounted(() => {
  render()
  observer = new ResizeObserver(() => chart?.resize())
  if (root.value) observer.observe(root.value)
})

onBeforeUnmount(() => {
  observer?.disconnect()
  chart?.dispose()
})
</script>
