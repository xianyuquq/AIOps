<script setup>
import { onMounted, ref, watch } from 'vue'
import * as echarts from 'echarts'
import api, { errMsg } from '../api'

const metrics = ref([])
const series = ref([])
const metricKey = ref('')
const hours = ref(6)
const limit = ref(300)
const loading = ref(false)
const chartRef = ref(null)
let chart = null

async function loadMetrics() {
  try {
    const { data } = await api.get('/metrics/available')
    metrics.value = data.metrics || []
    if (!metricKey.value && metrics.value.length) {
      metricKey.value = metrics.value.includes('cpu.usage_percent')
        ? 'cpu.usage_percent'
        : metrics.value[0]
    }
  } catch (e) {
    window.__toast?.(errMsg(e), 'err')
  }
}

const PALETTE = ['#3b82f6', '#22c55e', '#f59e0b', '#ef4444', '#a855f7', '#06b6d4', '#eab308', '#ec4899']

async function loadSeries() {
  if (!metricKey.value) return
  loading.value = true
  try {
    const { data } = await api.get('/metrics/series', {
      params: { metric_key: metricKey.value, hours: hours.value, limit: limit.value }
    })
    series.value = data
    render()
  } catch (e) {
    window.__toast?.(errMsg(e), 'err')
  } finally {
    loading.value = false
  }
}

function render() {
  if (!chartRef.value) return
  if (!chart) chart = echarts.init(chartRef.value, 'dark')

  const raw = series.value
  const unit = raw[0]?.unit || ''

  chart.setOption(
    {
      backgroundColor: 'transparent',
      tooltip: { trigger: 'axis' },
      legend: {
        type: 'scroll', top: 0, textStyle: { color: '#8b98a9', fontSize: 11 }
      },
      grid: { left: 55, right: 20, top: 38, bottom: 42 },
      xAxis: {
        type: 'time',
        axisLine: { lineStyle: { color: '#2a3444' } },
        axisLabel: { color: '#8b98a9', fontSize: 11 }
      },
      yAxis: {
        type: 'value',
        name: unit,
        nameTextStyle: { color: '#8b98a9' },
        splitLine: { lineStyle: { color: '#1e2733' } },
        axisLabel: { color: '#8b98a9', fontSize: 11 }
      },
      dataZoom: [
        { type: 'inside', start: 0, end: 100 },
        { type: 'slider', height: 18, bottom: 8, borderColor: '#2a3444', textStyle: { color: '#8b98a9' } }
      ],
      series: raw.map((s, i) => {
        const color = PALETTE[i % PALETTE.length]
        return {
          name: s.labels?.mount || s.server_name,
          type: 'line',
          showSymbol: false,
          smooth: true,
          lineStyle: { width: 2, color },
          itemStyle: { color },
          data: s.points.map((p) => [p.ts, p.value])
        }
      })
    },
    true
  )
  chart.resize()
}

watch([metricKey, hours], loadSeries)
watch(series, render)

onMounted(async () => {
  await loadMetrics()
  await loadSeries()
  window.addEventListener('resize', () => chart?.resize())
})
</script>

<template>
  <div class="col">
    <div class="row">
      <h2 style="margin:0;font-size:19px">监控图表</h2>
      <div class="spacer"></div>
      <select v-model="metricKey" style="width:250px">
        <option v-for="m in metrics" :key="m" :value="m">{{ m }}</option>
      </select>
      <select v-model.number="hours" style="width:120px">
        <option :value="1">近 1 小时</option>
        <option :value="6">近 6 小时</option>
        <option :value="24">近 24 小时</option>
        <option :value="72">近 3 天</option>
        <option :value="168">近 7 天</option>
      </select>
      <button @click="loadSeries" :disabled="loading">{{ loading ? '加载中…' : '刷新' }}</button>
    </div>

    <div v-if="!metrics.length" class="card empty">
      还没有采集到任何指标。<br />
      请先到「服务器管理」添加服务器，再到「监控脚本」启用采集脚本。
    </div>

    <div v-else class="card">
      <div ref="chartRef" class="chart"></div>
    </div>

    <div v-if="series.length" class="card">
      <h3 style="font-size:14px;margin:0 0 10px">当前值</h3>
      <table>
        <thead><tr><th>服务器</th><th>标签</th><th>最新值</th><th>采样点数</th></tr></thead>
        <tbody>
          <tr v-for="(s, i) in series" :key="i">
            <td>{{ s.server_name }}</td>
            <td class="small mono muted">
              {{ s.points.at(-1)?.labels ? JSON.stringify(s.points.at(-1).labels) : '-' }}
            </td>
            <td class="mono">{{ s.points.at(-1)?.value }}</td>
            <td class="muted">{{ s.points.length }}</td>
          </tr>
        </tbody>
      </table>
    </div>
  </div>
</template>

<style scoped>
.chart { height: 460px; width: 100%; }
</style>
