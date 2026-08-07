<script setup lang="ts">
/** 内嵌文本复核（嵌在 JobProgressDialog 的 question 插槽中）
 *
 * 行：勾选 + 原文 + 出处 + AI 判定标记（理由 tooltip）+ 单句精判 + 源码查看
 * 底：全选/全不选/仅选AI建议 | 取消 / 重判筛出 / 精判筛出 / 标记并重新生成模板
 */
import { computed, ref, watch } from 'vue'
import { NButton, NCheckbox, NInput, NScrollbar, NSelect, NSpace, NTag, useMessage } from 'naive-ui'
import { CodeOutline, SparklesOutline } from '@vicons/ionicons5'
import { api, errorText } from '../api/client'
import { useJobsStore, type JobView } from '../stores/jobs'
import { renderIcon } from './icons'
import CodeSnippet from './CodeSnippet.vue'

export interface EmbeddedRow {
  id: number
  text: string
  kind: string
  hint: string
  file: string
  line: number
  confidence: string
  raw: string
  ai_keep: number   // 1 / 0 / -1
  ai_reason: string
  ai_danger: number // 1 = 静态分析发现比较/键名/资源等非显示用途
  source: string    // 'rule' = 静态规则判定, 'ai' = 模型判定
  status: string
}

const props = defineProps<{ job: JobView; question: { question_id: string; payload: unknown } }>()

const message = useMessage()
const jobsStore = useJobsStore()

const rows = ref<EmbeddedRow[]>([])
const chosen = ref<Set<number>>(new Set())
const refining = ref<Set<number>>(new Set())
const snippetFor = ref<number | null>(null)
const reasonFor = ref<number | null>(null)
const answering = ref(false)

function defaultChecked(r: EmbeddedRow): boolean {
  if (r.ai_keep !== -1) return !!r.ai_keep
  return r.confidence === 'high'
}

// 新问题（重判后再问）→ 重置本地状态
watch(() => props.question.question_id, () => {
  rows.value = (props.question.payload as { rows: EmbeddedRow[] }).rows
    .map((r) => ({ ...r }))
  chosen.value = new Set(rows.value.filter(defaultChecked).map((r) => r.id))
  snippetFor.value = null
  reasonFor.value = null
}, { immediate: true })

const chosenCount = computed(() => chosen.value.size)

// ---- 筛选 ----
const search = ref('')
const verdictFilter = ref('all')   // all | keep | drop | undecided
const sourceFilter = ref('all')    // all | rule | ai
const kindFilter = ref('all')      // all | screen | python
const dangerFilter = ref('all')    // all | danger | safe

const filteredRows = computed(() => rows.value.filter((r) => {
  if (verdictFilter.value === 'undecided' && r.ai_keep !== -1) return false
  if (verdictFilter.value === 'keep' && r.ai_keep !== 1) return false
  if (verdictFilter.value === 'drop' && r.ai_keep !== 0) return false
  if (sourceFilter.value !== 'all' && r.source !== sourceFilter.value) return false
  if (kindFilter.value !== 'all' && r.kind !== kindFilter.value) return false
  if (dangerFilter.value === 'danger' && !r.ai_danger) return false
  if (dangerFilter.value === 'safe' && r.ai_danger) return false
  if (search.value) {
    const q = search.value.toLowerCase()
    return r.text.toLowerCase().includes(q)
      || r.file.toLowerCase().includes(q)
      || r.hint.toLowerCase().includes(q)
  }
  return true
}))

const verdictOptions = [
  { label: '全部判定', value: 'all' },
  { label: '建议保留', value: 'keep' },
  { label: '建议丢弃', value: 'drop' },
  { label: '未判定', value: 'undecided' },
]
const sourceOptions = [
  { label: '全部来源', value: 'all' },
  { label: '规则判定', value: 'rule' },
  { label: 'AI 判定', value: 'ai' },
]
const kindOptions = [
  { label: '全部类型', value: 'all' },
  { label: '屏幕语言', value: 'screen' },
  { label: '脚本数据', value: 'python' },
]
const dangerOptions = [
  { label: '全部风险', value: 'all' },
  { label: '仅 ⚠ 风险', value: 'danger' },
  { label: '无 ⚠ 风险', value: 'safe' },
]

function toggle(id: number, v: boolean) {
  if (v) chosen.value.add(id)
  else chosen.value.delete(id)
}

// 全选/全不选作用于当前筛选出的行（所见即所得）
function setAll(v: boolean) {
  const next = new Set(chosen.value)
  for (const r of filteredRows.value) {
    if (v) next.add(r.id)
    else next.delete(r.id)
  }
  chosen.value = next
}

function setAi() {
  chosen.value = new Set(filteredRows.value.filter(defaultChecked).map((r) => r.id))
}

// 精判并发上限：一次精判的 agentic 循环要挂几十秒连接，
// Chromium 对单域名限 6 个连接（SSE 已占 2 个），
// 不加限制地点多个精判会把心跳 /api/health 挤在队列里 → 误判服务断联
const MAX_CONCURRENT_REFINE = 2
let refineActive = 0
const refineWaiters: Array<() => void> = []

async function acquireRefineSlot(): Promise<void> {
  if (refineActive < MAX_CONCURRENT_REFINE) {
    refineActive++
    return
  }
  await new Promise<void>((resolve) => refineWaiters.push(resolve))
  refineActive++
}

function releaseRefineSlot(): void {
  refineActive--
  const next = refineWaiters.shift()
  if (next) next()
}

async function refineOne(row: EmbeddedRow) {
  refining.value.add(row.id)
  await acquireRefineSlot()
  try {
    const data = await api.post<{ ai_keep: number; ai_reason: string; ai_danger: number }>(
      `/api/current/embedded/refine/${row.id}`)
    row.ai_keep = data.ai_keep
    row.ai_reason = data.ai_reason
    row.ai_danger = data.ai_danger
    row.source = 'ai'   // 单句精判是模型判定
    toggle(row.id, !!data.ai_keep)
  } catch (e) {
    message.error(errorText(e), { duration: 10000, closable: true })
  } finally {
    releaseRefineSlot()
    refining.value.delete(row.id)
  }
}

async function answer(payload: Record<string, unknown>) {
  answering.value = true
  try {
    await jobsStore.answer(props.job.id, props.question.question_id, payload)
  } catch (e) {
    message.error(errorText(e), { duration: 8000 })
  } finally {
    answering.value = false
  }
}

function confirm() {
  answer({ action: 'confirm', chosen_ids: [...chosen.value] })
}

function truncate(s: string, n: number): string {
  return s.length > n ? s.slice(0, n - 3) + '...' : s
}
</script>

<template>
  <div style="border-top: 1px solid #333; padding-top: 10px">
    <n-space align="center" style="margin-bottom: 8px" wrap>
      <span style="font-weight: 600">内嵌文本复核（{{ rows.length }} 条候选）</span>
      <n-tag size="small">已选 {{ chosenCount }}/{{ rows.length }}</n-tag>
      <n-button size="tiny" quaternary @click="setAll(true)">全选</n-button>
      <n-button size="tiny" quaternary @click="setAll(false)">全不选</n-button>
      <n-button size="tiny" quaternary @click="setAi">仅选 AI 建议</n-button>
    </n-space>
    <n-space align="center" style="margin-bottom: 8px" wrap size="small">
      <n-input
        v-model:value="search" size="tiny" clearable placeholder="搜索文本/出处"
        style="width: 180px"
      />
      <n-select v-model:value="verdictFilter" size="tiny" :options="verdictOptions" style="width: 110px" />
      <n-select v-model:value="sourceFilter" size="tiny" :options="sourceOptions" style="width: 110px" />
      <n-select v-model:value="kindFilter" size="tiny" :options="kindOptions" style="width: 110px" />
      <n-select v-model:value="dangerFilter" size="tiny" :options="dangerOptions" style="width: 110px" />
      <span v-if="filteredRows.length !== rows.length" style="font-size: 12px; color: #888">
        筛出 {{ filteredRows.length }} 条
      </span>
    </n-space>
    <div style="font-size: 12px; color: #e8a33d; margin-bottom: 8px">
      ✓/✗ 为预筛建议（规则=静态分析确定，AI=模型判定），⚠ 表示该字符串另有比较/键名/资源用途，
      包 _() 后这些用途可能失效，请点「理由」查看 AI 核实结论。最终以你的勾选为准。<br>
      define 期求值的数据在游戏中途切换语言不会更新（开局选中文或重启后正常）。
    </div>

    <n-scrollbar style="max-height: 46vh">
      <div v-for="r in filteredRows" :key="r.id" style="padding: 3px 0; border-bottom: 1px solid #262626">
        <n-space align="center" :wrap="false" size="small">
          <n-checkbox
            :checked="chosen.has(r.id)" size="small"
            @update:checked="(v: boolean) => toggle(r.id, v)"
          />
          <span style="flex: 1; font-size: 13px; word-break: break-all">
            {{ truncate(r.text, 80) }}
            <span style="color: #888; font-size: 12px">〔{{ r.hint }} · {{ r.file }}:{{ r.line }}〕</span>
          </span>
          <n-tag
            v-if="r.ai_danger" size="tiny" type="warning" :bordered="false"
          >⚠</n-tag>
          <n-tag
            v-if="r.ai_keep !== -1" size="tiny"
            :type="r.ai_keep ? 'success' : 'error'" :bordered="false"
          >
            {{ (r.ai_keep ? '✓' : '✗') + (r.source === 'rule' ? '规则' : 'AI') }}
          </n-tag>
          <n-button
            v-if="r.ai_reason || r.ai_danger" size="tiny" quaternary
            @click="reasonFor = reasonFor === r.id ? null : r.id"
          >理由</n-button>
          <n-button
            size="tiny" quaternary :loading="refining.has(r.id)"
            :render-icon="renderIcon(SparklesOutline)" @click="refineOne(r)"
          >精判</n-button>
          <n-button
            size="tiny" quaternary :render-icon="renderIcon(CodeOutline)"
            @click="snippetFor = snippetFor === r.id ? null : r.id"
          >源码</n-button>
        </n-space>
        <div
          v-if="reasonFor === r.id && (r.ai_reason || r.ai_danger)"
          style="font-size: 12px; color: #999; margin: 2px 0 2px 24px; word-break: break-all; max-height: 120px; overflow-y: auto"
        >
          <span v-if="r.ai_danger" style="color: #e8a33d">
            ⚠ 静态分析发现该字符串另有比较/键名/资源等非显示用途，翻译后这些用途可能失效。
          </span>
          {{ r.ai_reason }}
        </div>
        <code-snippet
          v-if="snippetFor === r.id"
          :file="r.file" :line="r.line" :literal="r.raw"
        />
      </div>
    </n-scrollbar>

    <n-space justify="end" style="margin-top: 10px">
      <n-button size="small" :disabled="answering" @click="answer({ action: 'cancel' })">取消</n-button>
      <n-button
        size="small" type="warning" :disabled="answering || !filteredRows.length"
        @click="answer({ action: 'rescreen', row_ids: filteredRows.map((r) => r.id) })"
      >
        重判筛出的 {{ filteredRows.length }} 条
      </n-button>
      <n-button
        size="small" type="warning" :disabled="answering || !filteredRows.length"
        @click="answer({ action: 'refine_all', row_ids: filteredRows.map((r) => r.id) })"
      >
        精判筛出的 {{ filteredRows.length }} 条
      </n-button>
      <n-button size="small" type="primary" :disabled="answering" @click="confirm">
        标记并重新生成模板
      </n-button>
    </n-space>
  </div>
</template>
