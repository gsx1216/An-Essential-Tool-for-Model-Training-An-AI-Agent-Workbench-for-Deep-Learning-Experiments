<script setup lang="ts">
import {
  computed,
  onBeforeUnmount,
  onMounted,
  ref,
  watch,
  nextTick,
} from "vue";
import * as echarts from "echarts/core";
import { LineChart } from "echarts/charts";
import {
  GridComponent,
  TooltipComponent,
  LegendComponent,
  DataZoomComponent,
} from "echarts/components";
import { CanvasRenderer } from "echarts/renderers";
import type { MetricEvent } from "../types/domain";
echarts.use([
  LineChart,
  GridComponent,
  TooltipComponent,
  LegendComponent,
  DataZoomComponent,
  CanvasRenderer,
]);
const props = defineProps<{ events: MetricEvent[]; compact?: boolean }>();
const element = ref<HTMLElement>(),
  metric = ref("");
const names = computed(() => [
  ...new Set(props.events.flatMap((item) => Object.keys(item.values))),
]);
let chart: echarts.ECharts | undefined, observer: ResizeObserver | undefined;
function draw() {
  if (!element.value || !names.value.length) {
    chart?.clear();
    return;
  }
  chart ||= echarts.init(element.value);
  const groups = new Map<string, [number, number][]>();
  for (const event of props.events) {
    const value = event.values[metric.value];
    if (typeof value !== "number") continue;
    const key = `${event.dataset} / ${event.split}`;
    if (!groups.has(key)) groups.set(key, []);
    groups.get(key)!.push([event.step, value]);
  }
  chart.setOption(
    {
      animation: false,
      color: ["#15989a", "#7e98c1", "#d9a871", "#8ab794", "#a388bd"],
      tooltip: { trigger: "axis", renderMode: "richText", confine: true },
      grid: { left: 54, right: 20, top: 20, bottom: props.compact ? 32 : 86 },
      xAxis: {
        type: "value",
        name: "step",
        nameLocation: "middle",
        nameGap: 25,
        axisLabel: { fontSize: 10, color: "#90a0aa" },
        splitLine: { show: false },
      },
      yAxis: {
        type: "value",
        scale: true,
        axisLabel: { fontSize: 10, color: "#90a0aa" },
        splitLine: { lineStyle: { color: "#edf2f4", type: "dashed" } },
      },
      legend: props.compact
        ? { show: false }
        : { bottom: 0, type: "scroll", textStyle: { fontSize: 10 } },
      dataZoom: props.compact
        ? []
        : [{ type: "inside" }, { type: "slider", height: 14, bottom: 35 }],
      series: [...groups].map(([name, data]) => ({
        name,
        type: "line",
        data,
        showSymbol: data.length < 20,
        symbolSize: 4,
        smooth: false,
        lineStyle: { width: 2 },
        connectNulls: false,
      })),
    },
    { notMerge: true },
  );
}
watch(
  names,
  (value) => {
    if (!value.includes(metric.value))
      metric.value = value.includes("loss") ? "loss" : value[0] || "";
    void nextTick(draw);
  },
  { immediate: true },
);
watch(
  () => props.events,
  () => nextTick(draw),
  { deep: true },
);
watch(metric, draw);
onMounted(() => {
  draw();
  observer = new ResizeObserver(() => chart?.resize());
  if (element.value) observer.observe(element.value);
});
onBeforeUnmount(() => {
  observer?.disconnect();
  chart?.dispose();
});
</script>
<template>
  <div>
    <div v-if="names.length" class="chart-picker">
      <el-select v-model="metric" size="small" aria-label="选择图表指标"
        ><el-option
          v-for="name in names"
          :key="name"
          :label="name"
          :value="name"
      /></el-select>
    </div>
    <div
      ref="element"
      class="chart"
      role="img"
      :aria-label="`${metric || '训练'} 指标趋势`"
    />
    <p v-if="!names.length" class="hint chart-empty">
      尚无已采集指标，曲线会在数据就绪后显示。
    </p>
  </div>
</template>
<style scoped>
.chart-picker {
  width: 140px;
  margin: 0 0 8px auto;
}
.chart-empty {
  margin-top: -140px;
  height: 120px;
  text-align: center;
  padding: 10px 20px;
}
.chart-picker :deep(.el-select__wrapper) {
  min-height: 28px;
}
</style>
