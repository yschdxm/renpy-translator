<script setup lang="ts">
/** 内嵌文本复核（嵌在 JobProgressDialog 的 question 插槽中）
 *
 * 行：勾选 + 原文 + 出处 + AI 判定标记（理由 tooltip）+ 单句精判 + 源码查看
 * 底：全选/全不选/仅选AI建议 | 取消 / 全部重判 / 标记并重新生成模板
 */
import { computed, ref, watch } from 'vue'
import { NButton, NCheckbox, NScrollbar, NSpace, NTag, NTooltip, useMessage } from 'naive-ui'
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
  status: string
}

const props = defineProps<{ job: JobView; question: { question_id: string; payload: unknown } }>()

const message = useMessage()
const jobsStore = useJobsStore()

const rows = ref<EmbeddedRow[]>([])
const chosen = ref<Set<number>>(new Set())
const refining = ref<Set<number>>(new Set())
const snippetFor = ref<number | null>(null)
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
}, { immediate: true })

const chosenCount = computed(() => chosen.value.size)

function toggle(id: number, v: boolean) {
  if (v) chosen.value.add(id)
  else chosen.value.delete(id)
}

function setAll(v: boolean) {
  chosen.value = v ? new Set(rows.value.map((r) => r.id)) : new Set()
}

function setAi() {
  chosen.value = new Set(rows.value.filter(defaultChecked).map((r) => r.id))
}

async function refineOne(row: EmbeddedRow) {
  refining.value.add(row.id)
  try {
    const data = await api.post<{ ai_keep: number; ai_reason: string }>(
      `/api/current/embedded/refine/${row.id}`)
    row.ai_keep = data.ai_keep
    row.ai_reason = data.ai_reason
    toggle(row.id, !!data.ai_keep)
  } catch (e) {
    message.error(errorText(e), { duration: 10000, closable: true })
  } finally {
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
    <div style="font-size: 12px; color: #e8a33d; margin-bottom: 8px">
      ✓AI/✗AI 为 AI 判定（悬停看理由），最终以你的勾选为准。
      define 期求值的数据在游戏中途切换语言不会更新（开局选中文或重启后正常）。
    </div>

    <n-scrollbar style="max-height: 46vh">
      <div v-for="r in rows" :key="r.id" style="padding: 3px 0; border-bottom: 1px solid #262626">
        <n-space align="center" :wrap="false" size="small">
          <n-checkbox
            :checked="chosen.has(r.id)" size="small"
            @update:checked="(v: boolean) => toggle(r.id, v)"
          />
          <span style="flex: 1; font-size: 13px; word-break: break-all">
            {{ truncate(r.text, 80) }}
            <span style="color: #888; font-size: 12px">〔{{ r.hint }} · {{ r.file }}:{{ r.line }}〕</span>
          </span>
          <n-tooltip v-if="r.ai_keep !== -1" :disabled="!r.ai_reason">
            <template #trigger>
              <n-tag size="tiny" :type="r.ai_keep ? 'success' : 'error'" :bordered="false">
                {{ r.ai_keep ? '✓AI' : '✗AI' }}
              </n-tag>
            </template>
            {{ r.ai_reason }}
          </n-tooltip>
          <n-button
            size="tiny" quaternary :loading="refining.has(r.id)"
            :render-icon="renderIcon(SparklesOutline)" @click="refineOne(r)"
          >精判</n-button>
          <n-button
            size="tiny" quaternary :render-icon="renderIcon(CodeOutline)"
            @click="snippetFor = snippetFor === r.id ? null : r.id"
          >源码</n-button>
        </n-space>
        <code-snippet
          v-if="snippetFor === r.id"
          :file="r.file" :line="r.line" :literal="r.raw"
        />
      </div>
    </n-scrollbar>

    <n-space justify="end" style="margin-top: 10px">
      <n-button size="small" :disabled="answering" @click="answer({ action: 'cancel' })">取消</n-button>
      <n-button size="small" type="warning" :disabled="answering" @click="answer({ action: 'rescreen' })">
        全部重判
      </n-button>
      <n-button size="small" type="primary" :disabled="answering" @click="confirm">
        标记并重新生成模板
      </n-button>
    </n-space>
  </div>
</template>
