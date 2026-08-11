<script setup lang="ts">
/** 人名翻译页：翻译+分析融合流程 */
import { h, onMounted, ref } from 'vue'
import {
  NButton, NDataTable, NInput, NModal, NSpace, NTag, NText, useMessage,
} from 'naive-ui'
import type { DataTableColumns } from 'naive-ui'
import { LanguageOutline, RefreshOutline } from '@vicons/ionicons5'
import { api, errorText } from '../api/client'
import { renderIcon } from '../components/icons'
import { useJobTask } from '../composables/useJobTask'
import { useSessionStore } from '../stores/session'

const message = useMessage()
const session = useSessionStore()
const { runJob } = useJobTask()

interface NameRow {
  variable: string
  original: string
  translated: string
  lines: number
  name_done: boolean
  analyzed: boolean
}

const rows = ref<NameRow[]>([])
const stats = ref({ total: 0, translated: 0, analyzed: 0 })
const loading = ref(false)
const processing = ref<Set<string>>(new Set())

async function load() {
  loading.value = true
  try {
    const data = await api.get<{
      rows: NameRow[]; total: number; translated: number; analyzed: number
    }>('/api/current/names')
    rows.value = data.rows
    stats.value = { total: data.total, translated: data.translated, analyzed: data.analyzed }
  } catch (e) {
    message.error(errorText(e), { duration: 8000 })
  } finally {
    loading.value = false
  }
}

// ---- 行内编辑 ----
// Enter 保存后焦点仍在输入框，随后的 blur 会再发一次相同 PATCH；
// 记录已保存值去重（与 TextsPage 的 editingId 守卫同思路）
const savedNames = new Map<string, string>()

async function saveName(row: NameRow, value: string) {
  const key = row.original + row.variable
  if (savedNames.get(key) === value) return
  const old = row.translated
  row.translated = value
  try {
    await api.patch(`/api/current/names/${encodeURIComponent(row.original)}`, {
      cn_name: value, variable: row.variable,
    })
    savedNames.set(key, value)
    row.name_done = !!value.trim()
    await session.refresh()
  } catch (e) {
    row.translated = old
    message.error(errorText(e), { duration: 8000, closable: true })
  }
}

async function translateOne(row: NameRow) {
  processing.value.add(row.original)
  try {
    const data = await api.post<{ cn_name: string; profile: object }>(
      `/api/current/names/${encodeURIComponent(row.original)}/translate`,
      { variable: row.variable })
    row.translated = data.cn_name || row.translated
    row.name_done = !!row.translated.trim()
    row.analyzed = !!data.profile
    await session.refresh()
    message.success(`${row.original} → ${data.cn_name}`)
  } catch (e) {
    message.error(errorText(e), { duration: 10000, closable: true })
  } finally {
    processing.value.delete(row.original)
  }
}

async function translateAll() {
  await runJob(
    () => api.post('/api/current/names/translate-all'),
    () => load())
}

// ---- 画像对话框 ----
const profileVisible = ref(false)
const profileName = ref('')
const profileData = ref<Record<string, string>>({})

async function viewProfile(row: NameRow) {
  try {
    const data = await api.get<{ profile: Record<string, string> }>(
      `/api/current/names/${encodeURIComponent(row.original)}/profile`)
    profileName.value = row.original
    profileData.value = data.profile
    profileVisible.value = true
  } catch (e) {
    message.error(errorText(e))
  }
}

const columns: DataTableColumns<NameRow> = [
  { title: '#', key: 'index', width: 50, render: (_, i) => i + 1 },
  { title: '变量名', key: 'variable', width: 110 },
  { title: '原文人名', key: 'original', width: 140 },
  {
    title: '中文名（点击编辑）', key: 'translated', width: 160,
    render: (r) => h(NInput, {
      value: r.translated,
      'onUpdate:value': (v: string) => { r.translated = v },
      onBlur: () => saveName(r, r.translated),
      onKeydown: (e: KeyboardEvent) => { if (e.key === 'Enter') saveName(r, r.translated) },
      size: 'small', placeholder: '（未翻译）',
    }),
  },
  { title: '台词数', key: 'lines', width: 80, sorter: (a, b) => a.lines - b.lines },
  {
    title: '翻译', key: 'name_status', width: 90,
    render: (r) => h(NTag, {
      size: 'small',
      type: processing.value.has(r.original) ? 'warning' : r.name_done ? 'success' : 'default',
    }, () => processing.value.has(r.original) ? '处理中' : r.name_done ? '完成' : '待翻译'),
  },
  {
    title: '分析', key: 'analysis_status', width: 90,
    render: (r) => h(NTag, {
      size: 'small',
      type: processing.value.has(r.original) ? 'warning' : r.analyzed ? 'success' : 'default',
    }, () => processing.value.has(r.original) ? '处理中' : r.analyzed ? '已完成' : '未分析'),
  },
  {
    title: '操作', key: 'actions', width: 190,
    render: (r) => h(NSpace, { size: 4 }, () => [
      h(NButton, {
        size: 'tiny', type: 'primary', quaternary: true,
        loading: processing.value.has(r.original),
        onClick: () => translateOne(r),
      }, () => '翻译+分析'),
      h(NButton, {
        size: 'tiny', quaternary: true, disabled: !r.analyzed,
        onClick: () => viewProfile(r),
      }, () => '查看'),
    ]),
  },
]

onMounted(load)
</script>

<template>
  <div>
    <n-space align="center" style="margin-bottom: 12px">
      <h2 style="margin: 0">人名翻译</h2>
      <n-button size="small" type="primary" :render-icon="renderIcon(LanguageOutline)" @click="translateAll">全部翻译+分析</n-button>
      <n-button size="small" quaternary :render-icon="renderIcon(RefreshOutline)" @click="load">刷新</n-button>
      <n-text depth="3" style="font-size: 12px">
        {{ stats.total }} 人名，翻译 {{ stats.translated }}，分析 {{ stats.analyzed }}
      </n-text>
    </n-space>

    <n-data-table
      :columns="columns" :data="rows" :loading="loading"
      :row-key="(r: NameRow) => r.original + r.variable" size="small"
      :pagination="{ pageSize: 50 }"
    />

    <n-modal v-model:show="profileVisible" preset="card"
             :title="`${profileName} - 人物特征`" style="width: 640px">
      <div v-for="(value, key) in profileData" :key="key" style="margin-bottom: 10px">
        <n-text strong style="color: #7eb8ff">【{{ key }}】</n-text>
        <div style="padding-left: 12px; white-space: pre-wrap; font-size: 13px">{{ value }}</div>
      </div>
    </n-modal>
  </div>
</template>
