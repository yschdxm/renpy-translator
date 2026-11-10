<script setup lang="ts">
/** 诊断页：API 日志开关 + LLM 轨迹面板（事件记录表 / 时间概览 / 局部检查器）
 *
 * 轨迹形态参照 deepseek-harness 的 ui-trajectory（Trajectory 视图）：
 * Overview 条按真实开始时间/耗时投影调用；选中行打开检查器看
 * token 用量、耗时、请求/响应；消息按 role 展示，JSON 可切原始视图。
 * 跟随尾部：跑批期间自动刷到最新并选中最新一条，手动选中旧记录后暂停跟随。
 */
import { computed, onActivated, onDeactivated, onMounted, onUnmounted, ref } from 'vue'
import {
  NButton, NEmpty, NSelect, NSpace, NSwitch, NTag, NText, NTooltip,
  useMessage,
} from 'naive-ui'
import { api, toastError, toastOk } from '../api/client'
import PageHelp from '../components/PageHelp.vue'
import { usePageScroll } from '../composables/usePageScroll'

interface ApiLogEntry {
  id: number
  ts: string
  duration_ms: number | null
  ok: boolean
  attempt: number
  error: string
  task_type: string
  model: string
  usage: Record<string, number> | null
  request: {
    model?: string
    messages?: Array<Record<string, unknown>>
    tools?: unknown
    [k: string]: unknown
  }
  response: Record<string, unknown>
}

const message = useMessage()
const entries = ref<ApiLogEntry[]>([])
const enabled = ref(false)
const envForced = ref(false)
const apiLogOn = ref(false)
const loading = ref(false)
const selectedId = ref(0)
const follow = ref(true)
const taskFilter = ref('')
const rawRequest = ref(false)
const rawResponse = ref(false)
const wrapLines = ref(true)
let timer: number | null = null

const TASK_LABELS: Record<string, string> = {
  name: '人名翻译', name_analysis: '人物分析', ui: '字符串翻译',
  dialogue: '对话翻译', analysis: '分析', screener: '内嵌预筛',
  graph: '关系图谱', scene: '场景摘要', export_fix: '导出修复',
  style_guide: '风格指南', test: '连接测试',
}

const taskOptions = computed(() => [
  { label: '全部类型', value: '' },
  ...Object.entries(TASK_LABELS).map(([value, label]) => ({ label, value })),
])

const selected = computed(() =>
  entries.value.find((e) => e.id === selectedId.value) || null)

const filtered = computed(() => taskFilter.value
  ? entries.value.filter((e) => e.task_type === taskFilter.value)
  : entries.value)

/** 列表倒序展示：最新在最上（后端按时间正序返回，翻页向更旧追加在尾部） */
const listRows = computed(() => [...filtered.value].reverse())

async function refresh(selectNewest = false) {
  loading.value = true
  try {
    const data = await api.get<{
      entries: ApiLogEntry[]; enabled: boolean; env_forced: boolean
    }>('/api/logs/api?limit=200', AbortSignal.timeout(15000))
    entries.value = data.entries
    enabled.value = data.enabled
    envForced.value = data.env_forced
    // 开关反映生效状态（环境变量强制时显示开且禁用）
    apiLogOn.value = data.enabled
    if (selectNewest && follow.value && data.entries.length) {
      selectedId.value = data.entries[data.entries.length - 1].id
    } else if (selectedId.value
        && !data.entries.some((e) => e.id === selectedId.value)) {
      selectedId.value = 0
    }
  } catch (e) {
    toastError(message, e)
  } finally {
    loading.value = false
  }
}

function select(e: ApiLogEntry) {
  selectedId.value = e.id
  // 手动翻旧记录：暂停自动跟随，免得新记录把选中顶走
  follow.value = false
}

async function toggleApiLog(v: boolean) {
  if (envForced.value) return
  try {
    await api.put('/api/settings/prefs', { api_log: v ? '1' : '0' })
    apiLogOn.value = v
    toastOk(message, v ? 'API 日志已开启，开始记录模型调用' : 'API 日志已关闭')
  } catch (e) {
    apiLogOn.value = !v
    toastError(message, e)
  }
}

function pickRow(id: number) {
  const e = entries.value.find((x) => x.id === id)
  if (e) select(e)
}

// ---- Overview 时间条 ----
const timeSpan = computed(() => {
  const list = filtered.value
  if (!list.length) return null
  const times = list.map((e) => Date.parse(e.ts.replace(' ', 'T')))
  const min = Math.min(...times)
  const max = Math.max(...times, min + 1000)
  return { min, span: Math.max(max - min, 1000) }
})

/** 时长按量级切换单位：<1s 用毫秒，<60s 用秒，否则分钟——避免长串毫秒 */
function fmtDur(ms: number | null): string {
  if (ms == null) return '—'
  if (ms < 1000) return `${ms}ms`
  if (ms < 60_000) return `${(ms / 1000).toFixed(1)}s`
  const min = ms / 60000
  return min < 10 ? `${min.toFixed(1)}min` : `${Math.round(min)}min`
}

function barStyle(e: ApiLogEntry) {
  const span = timeSpan.value
  if (!span) return {}
  const t = Date.parse(e.ts.replace(' ', 'T'))
  const left = ((t - span.min) / span.span) * 100
  const width = Math.max(0.6, ((e.duration_ms ?? 0) / span.span) * 100)
  return {
    left: `${Math.min(left, 99.4)}%`,
    width: `${width}%`,
    background: e.ok ? 'var(--rt-primary)' : 'var(--rt-warning)',
  }
}

function barTitle(e: ApiLogEntry) {
  return `${e.ts} · ${TASK_LABELS[e.task_type] || e.task_type}`
      + ` · ${fmtDur(e.duration_ms)}${e.ok ? '' : ' · 失败：' + e.error}`
}

// ---- 展示辅助 ----
function taskLabel(t: string) {
  return TASK_LABELS[t] || t || '其他'
}

function usageSummary(u: Record<string, number> | null | undefined) {
  if (!u || !Object.keys(u).length) return '—'
  const prompt = Number(u.prompt_tokens || 0)
  const completion = Number(u.completion_tokens || 0)
  // DeepSeek: prompt_cache_hit_tokens；OpenAI: prompt_tokens_details.cached_tokens
  const cached = Number(u.prompt_cache_hit_tokens
    || (u as Record<string, unknown>).prompt_tokens_details
      && ((u as Record<string, unknown>).prompt_tokens_details as Record<string, number>).cached_tokens
    || 0)
  return `↑${prompt} ↓${completion}` + (cached ? ` 缓存${cached}` : '')
}

function preview(e: ApiLogEntry) {
  const msgs = e.request?.messages || []
  const last = [...msgs].reverse().find((m) => m.role === 'user')
  const text = String(last?.content ?? '').replace(/\s+/g, ' ').trim()
  return text.slice(0, 60) || '（无文本）'
}

function pretty(obj: unknown) {
  try {
    return JSON.stringify(obj, null, 2)
  } catch {
    return String(obj)
  }
}

async function copy(text: string) {
  try {
    await navigator.clipboard.writeText(text)
  } catch { /* 剪贴板不可用时静默 */ }
}

function msgContent(m: Record<string, unknown>) {
  const c = m.content
  if (typeof c === 'string') return c
  return pretty(c)
}

const toolCalls = computed(() => {
  const resp = selected.value?.response as Record<string, unknown> | undefined
  const choices = (resp?.choices || []) as Array<Record<string, unknown>>
  const msg = (choices[0]?.message || {}) as Record<string, unknown>
  return (msg.tool_calls || []) as Array<Record<string, unknown>>
})

onMounted(() => {
  void refresh(true)
})
// keep-alive：轮询随激活启停（隐藏页不后台刷新）
onActivated(() => {
  timer = window.setInterval(() => { void refresh(true) }, 2500)
})
onDeactivated(() => {
  if (timer) window.clearInterval(timer)
})
onUnmounted(() => {
  if (timer) window.clearInterval(timer)
})
usePageScroll()  // 数据由轮询刷新，滚动位置仍按路由保留
</script>

<template>
  <div class="diag-page" style="max-width: 1200px">
    <n-space align="center" style="margin-bottom: 8px" wrap>
      <h2 style="margin: 0">诊断</h2>
      <n-space align="center" style="gap: 6px">
        <n-text depth="3" style="font-size: 12px">API 日志</n-text>
        <n-switch :value="apiLogOn" size="small" :disabled="envForced"
                  @update:value="toggleApiLog" />
      </n-space>
      <n-tag v-if="envForced" size="small" type="warning">API_LOG 环境变量强制开启</n-tag>
      <n-select v-model:value="taskFilter" size="small" style="width: 140px"
                :options="taskOptions" />
      <n-space align="center" style="gap: 6px">
        <n-switch v-model:value="follow" size="small" />
        <n-text depth="3" style="font-size: 12px">跟随最新</n-text>
      </n-space>
      <n-button size="small" :loading="loading" @click="refresh(false)">刷新</n-button>
      <page-help style="margin-left: auto" section="14" />
    </n-space>

    <n-text v-if="!apiLogOn && !envForced" depth="3"
            style="display: block; margin-bottom: 12px; font-size: 12px">
      未开启记录：打开上方开关后记录每次模型调用的完整请求/响应（含剧情文本，
      共享数据时注意隐私），下方轨迹表即刻可见提示词、返回内容、token 用量与缓存命中。
    </n-text>

    <!-- Overview：一根条 = 一次调用的真实开始时间与耗时 -->
    <div v-if="filtered.length" class="overview">
      <n-tooltip v-for="e in filtered" :key="e.id" trigger="hover" placement="top">
        <template #trigger>
          <div class="bar" :class="{ active: e.id === selectedId }"
               :style="barStyle(e)" @click="pickRow(e.id)" />
        </template>
        {{ barTitle(e) }}
      </n-tooltip>
    </div>

    <div class="main">
      <!-- 事件记录表 -->
      <div class="ledger">
        <div class="row head">
          <span class="c-time">时间</span>
          <span class="c-status">状态</span>
          <span class="c-task">类型</span>
          <span class="c-usage">用量</span>
          <span class="c-dur">耗时</span>
          <span class="c-preview">输入预览</span>
        </div>
        <div v-for="e in listRows" :key="e.id" class="row"
             :class="{ active: e.id === selectedId }" @click="pickRow(e.id)">
          <span class="c-time">{{ e.ts.slice(11) }}</span>
          <span class="c-status">
            <n-tag size="tiny" :type="e.ok ? 'success' : 'warning'"
                   style="width: 42px; justify-content: center">
              {{ e.ok ? '成功' : '失败' }}
            </n-tag>
          </span>
          <span class="c-task">{{ taskLabel(e.task_type) }}</span>
          <span class="c-usage">{{ usageSummary(e.usage) }}</span>
          <span class="c-dur">{{ fmtDur(e.duration_ms) }}</span>
          <span class="c-preview">{{ preview(e) }}</span>
        </div>
        <n-empty v-if="!filtered.length" description="暂无调用记录" style="margin: 24px 0" />
      </div>

      <!-- 局部检查器 -->
      <div v-if="selected" class="inspector">
        <n-space align="center" style="margin-bottom: 6px">
          <n-text strong>#{{ selected.id }}</n-text>
          <n-tag size="small">{{ taskLabel(selected.task_type) }}</n-tag>
          <n-tag size="small" :type="selected.ok ? 'success' : 'warning'">
            {{ selected.ok ? '成功' : '失败' }}
          </n-tag>
          <n-button size="tiny" quaternary @click="wrapLines = !wrapLines">
            {{ wrapLines ? '不换行' : '换行' }}
          </n-button>
        </n-space>
        <n-text depth="3" style="display: block; font-size: 12px; margin-bottom: 8px">
          {{ selected.ts }} · {{ selected.model || selected.request?.model || '—' }}
          · {{ fmtDur(selected.duration_ms) }} · 尝试 {{ selected.attempt }} 次
          · {{ usageSummary(selected.usage) }}
          <template v-if="!selected.ok"> · {{ selected.error }}</template>
        </n-text>

        <div class="section">
          <n-space align="center" style="margin-bottom: 4px">
            <n-text strong>请求</n-text>
            <n-button size="tiny" quaternary @click="rawRequest = !rawRequest">{}</n-button>
            <n-button size="tiny" quaternary @click="copy(pretty(selected.request))">复制</n-button>
          </n-space>
          <template v-if="rawRequest">
            <pre class="json" :class="{ wrap: wrapLines }">{{ pretty(selected.request) }}</pre>
          </template>
          <template v-else>
            <div v-for="(m, i) in selected.request?.messages || []" :key="i" class="msg">
              <n-tag size="tiny" style="margin-right: 6px">{{ m.role }}</n-tag>
              <pre class="json" :class="{ wrap: wrapLines }">{{ msgContent(m) }}</pre>
            </div>
            <pre v-if="selected.request?.tools" class="json" :class="{ wrap: wrapLines }">tools: {{ pretty(selected.request.tools) }}</pre>
          </template>
        </div>

        <div class="section">
          <n-space align="center" style="margin-bottom: 4px">
            <n-text strong>响应</n-text>
            <n-button size="tiny" quaternary @click="rawResponse = !rawResponse">{}</n-button>
            <n-button size="tiny" quaternary @click="copy(pretty(selected.response))">复制</n-button>
          </n-space>
          <template v-if="rawResponse">
            <pre class="json" :class="{ wrap: wrapLines }">{{ pretty(selected.response) }}</pre>
          </template>
          <template v-else>
            <pre class="json" :class="{ wrap: wrapLines }">{{ pretty(selected.response) }}</pre>
            <div v-for="(tc, i) in toolCalls" :key="i" class="msg">
              <n-tag size="tiny" type="info" style="margin-right: 6px">tool_call</n-tag>
              <pre class="json" :class="{ wrap: wrapLines }">{{ pretty(tc) }}</pre>
            </div>
          </template>
        </div>
      </div>
    </div>
  </div>
</template>

<style scoped>
.overview {
  position: relative;
  height: 34px;
  margin-bottom: 12px;
  border: 1px solid var(--n-border-color, var(--rt-border));
  border-radius: 6px;
  overflow: hidden;
}
.bar {
  position: absolute;
  top: 6px;
  bottom: 6px;
  min-width: 2px;
  border-radius: 2px;
  opacity: 0.75;
  cursor: pointer;
}
.bar.active {
  opacity: 1;
  outline: 2px solid var(--rt-text1);
}
/* 整页固定视口高度：列表与详情两栏各自独立滚动（不再整页滚动） */
.diag-page {
  display: flex;
  flex-direction: column;
  height: calc(100vh - 84px);   /* 减顶栏52 + 内容区上下内边距32 */
}
.main {
  display: flex;
  gap: 12px;
  align-items: stretch;
  flex: 1;
  min-height: 0;
}
.ledger {
  flex: 1.2;
  min-width: 0;
  height: 100%;
  border: 1px solid var(--n-border-color, var(--rt-border));
  border-radius: 6px;
  overflow-y: auto;
}
.row {
  display: flex;
  gap: 8px;
  align-items: center;
  padding: 4px 8px;
  font-size: 12px;
  cursor: pointer;
  border-bottom: 1px solid var(--rt-border);
}
.row.head {
  position: sticky;
  top: 0;
  z-index: 1;
  cursor: default;
  font-weight: 600;
  background: var(--rt-surface1);
}
.row.active {
  background: rgba(99, 226, 183, 0.14);
}
.row:hover:not(.head) {
  background: rgba(128, 128, 128, 0.1);
}
.c-time { width: 58px; flex: none; font-variant-numeric: tabular-nums; }
.c-status { width: 48px; flex: none; }
.c-task { width: 72px; flex: none; }
.c-usage { width: 110px; flex: none; font-variant-numeric: tabular-nums; }
.c-dur { width: 62px; flex: none; font-variant-numeric: tabular-nums; }
.c-preview {
  flex: 1;
  min-width: 0;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.inspector {
  flex: 1;
  min-width: 0;
  height: 100%;
  border: 1px solid var(--n-border-color, var(--rt-border));
  border-radius: 6px;
  padding: 10px;
  overflow-y: auto;
}
.section {
  margin-bottom: 12px;
}
.msg {
  margin-bottom: 6px;
}
.json {
  margin: 0;
  padding: 6px 8px;
  font-size: 12px;
  line-height: 1.5;
  font-family: Consolas, Menlo, monospace;
  background: rgba(128, 128, 128, 0.1);
  border-radius: 4px;
  overflow-x: auto;
  white-space: pre;
}
.json.wrap {
  white-space: pre-wrap;
  overflow-wrap: anywhere;
}
</style>
