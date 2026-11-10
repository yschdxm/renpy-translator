<script setup lang="ts">
/** 人名翻译页：翻译+分析融合流程 */
import { computed, h, onMounted, ref } from 'vue'
import { useRouter } from 'vue-router'
import {
  NButton, NDataTable, NDropdown, NEmpty, NInput, NInputGroup, NModal,
  NSelect, NSpace, NTag, NText, useDialog, useMessage,
} from 'naive-ui'
import type { DropdownOption } from 'naive-ui'
import type { DataTableColumns } from 'naive-ui'
import { DownloadOutline, LanguageOutline, RefreshOutline, ShareOutline } from '@vicons/ionicons5'
import { api, toastError, toastOk } from '../api/client'
import { renderIcon } from '../components/icons'
import ExportTextsDialog from '../components/ExportTextsDialog.vue'
import ImportFromProjectDialog from '../components/ImportFromProjectDialog.vue'
import ImportTextsDialog from '../components/ImportTextsDialog.vue'
import PageHeader from '../components/PageHeader.vue'
import ContextHint from '../components/ContextHint.vue'
import { useInlineEdit } from '../composables/useInlineEdit'
import { useJobTask } from '../composables/useJobTask'
import { usePageScroll } from '../composables/usePageScroll'
import { useSessionStore } from '../stores/session'
import { hasActiveTextSelection } from '../lib/textSelection'

const message = useMessage()
const router = useRouter()
const session = useSessionStore()
const dialog = useDialog()
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
const exportVisible = ref(false)
const importVisible = ref(false)
const crossImportVisible = ref(false)
async function onImported() {
  await load()
  await session.refresh()
}

// 行的唯一键：多个无显示名角色共享空 original，必须带上 variable
const rowKey = (r: NameRow) => `${r.original}|${r.variable}`

async function load() {
  loading.value = true
  try {
    const data = await api.get<{
      rows: NameRow[]; total: number; translated: number; analyzed: number
    }>('/api/current/names')
    rows.value = data.rows
    stats.value = { total: data.total, translated: data.translated, analyzed: data.analyzed }
  } catch (e) {
    toastError(message, e)
  } finally {
    loading.value = false
  }
}

// ---- 筛选（客户端：数据量小） ----
const searchText = ref('')
const statusFilter = ref('')

const filteredRows = computed(() => {
  const kw = searchText.value.trim().toLowerCase()
  return rows.value.filter((r) => {
    if (kw) {
      const hit = [r.original, r.translated, r.variable]
        .some((s) => (s || '').toLowerCase().includes(kw))
      if (!hit) return false
    }
    if (statusFilter.value === 'untranslated') return !r.translated.trim()
    if (statusFilter.value === 'translated') return !!r.translated.trim()
    if (statusFilter.value === 'unanalyzed') return !r.analyzed
    return true
  })
})

// ---- 行内编辑（中文名） ----
const { editingText, isEditing, startEdit, commitEdit } = useInlineEdit<NameRow>(
  (row) => row.original + row.variable,
  (row) => row.translated || '',
  async (row, value) => {
    const old = row.translated
    row.translated = value
    try {
      await api.patch(`/api/current/names/${encodeURIComponent(row.original || row.variable)}`, {
        cn_name: value, variable: row.variable,
      })
      row.name_done = !!value.trim()
      await session.refresh()
    } catch (e) {
      row.translated = old
      throw e
    }
  })

async function onCommit(row: NameRow) {
  try {
    await commitEdit(row)
  } catch (e) {
    toastError(message, e)
  }
}

type RunMode = 'both' | 'name' | 'analyze'

async function runOne(row: NameRow, mode: RunMode) {
  if (!session.hasTranslator) {
    void router.push('/settings?new=1')
    return
  }
  processing.value.add(rowKey(row))
  try {
    const data = await api.post<{ cn_name: string; profile: object }>(
      `/api/current/names/${encodeURIComponent(row.original || row.variable)}/translate`,
      { variable: row.variable, mode })
    if (mode !== 'analyze') {
      row.translated = data.cn_name || row.translated
      row.name_done = !!row.translated.trim()
    }
    if (mode !== 'name') {
      row.analyzed = !!data.profile
    }
    if (mode === 'name') {
      toastOk(message, `${row.original} → ${data.cn_name}`)
    } else if (mode === 'analyze') {
      toastOk(message, `${row.original || row.variable} 分析完成`)
    } else if (data.cn_name) {
      toastOk(message, `${row.original} → ${data.cn_name}（已分析）`)
    } else {
      // 占位符/无显示名角色不译名，仅出画像
      toastOk(message, `${row.original || row.variable} 分析完成`)
    }
    await session.refresh()
  } catch (e) {
    toastError(message, e)
  } finally {
    processing.value.delete(rowKey(row))
  }
}

// 保留原名：中文名=原名，人名对照表指导对话 AI 正文也保留原名
async function keepOriginal(row: NameRow) {
  try {
    await api.patch(`/api/current/names/${encodeURIComponent(row.original || row.variable)}`, {
      cn_name: row.original, variable: row.variable,
    })
    row.translated = row.original
    row.name_done = true
    await session.refresh()
    toastOk(message, `${row.original} 已标记保留原名`)
  } catch (e) {
    toastError(message, e)
  }
}

async function translateAll(mode: RunMode) {
  // 模型门禁：没配模型不发请求，点击去配置
  if (!session.hasTranslator) {
    void router.push('/settings?new=1')
    return
  }
  await runJob(
    () => api.post('/api/current/names/translate-all', { mode }),
    () => load())
}

const keepAllLoading = ref(false)
async function keepOriginalAll() {
  keepAllLoading.value = true
  try {
    const data = await api.post<{ updated: number }>('/api/current/names/keep-original-all')
    toastOk(message, `已保留原名: ${data.updated} 个角色`)
    await session.refresh()
    await load()
  } catch (e) {
    toastError(message, e)
  } finally {
    keepAllLoading.value = false
  }
}

// ---- 工具栏收纳：L1 批量菜单 / L3 更多 / 行内 ⋯ ----
const batchMenuOptions: DropdownOption[] = [
  { key: 'both', label: '全部翻译+分析' },
  { key: 'analyze', label: '全部分析' },
]

function onBatchMenu(key: string) {
  void translateAll(key as RunMode)
}

const moreMenuOptions: DropdownOption[] = [
  { key: 'keepAll', label: '全部保留原名…' },
  { type: 'divider', key: 'd' },
  { key: 'rebuild', label: '重新提取角色' },
  { key: 'importFrom', label: '从其他项目导入…' },
]

function onMoreMenu(key: string) {
  if (key === 'keepAll') {
    dialog.warning({
      title: '全部保留原名',
      content: '将所有未翻译角色的中文名设为原名（正文提及也保留原名），可随时单个改回。确定？',
      positiveText: '保留原名',
      negativeText: '取消',
      onPositiveClick: () => keepOriginalAll(),
    })
  } else if (key === 'rebuild') void refreshCharacters()
  else if (key === 'importFrom') crossImportVisible.value = true
}

function rowMenu(r: NameRow): DropdownOption[] {
  return [
    { key: 'analyze', label: '仅分析', disabled: !session.hasTranslator },
    { key: 'rename', label: '重译名', disabled: !r.original || !session.hasTranslator },
    { key: 'keep', label: '保留原名', disabled: !r.original || r.translated === r.original },
    { key: 'view', label: '查看画像', disabled: !r.analyzed },
  ]
}

function onRowMenu(key: string, r: NameRow) {
  if (key === 'analyze') void runOne(r, 'analyze')
  else if (key === 'rename') void runOne(r, 'name')
  else if (key === 'keep') void keepOriginal(r)
  else if (key === 'view') void viewProfile(r)
}

// 重新提取角色：修复引号说话人（"Ricardo" "..." 匿名角色）历史上
// 被误判为泛指变量、display_name 缺失显示「无需翻译」的存量数据
const refreshLoading = ref(false)
async function refreshCharacters() {
  refreshLoading.value = true
  try {
    await api.post('/api/current/names/refresh-characters')
    toastOk(message, '角色已重新提取')
    await session.refresh()
    await load()
  } catch (e) {
    toastError(message, e)
  } finally {
    refreshLoading.value = false
  }
}

// ---- 画像对话框 ----
const profileVisible = ref(false)
const profileName = ref('')
const profileData = ref<Record<string, string>>({})

async function viewProfile(row: NameRow) {
  try {
    const data = await api.get<{ profile: Record<string, string> }>(
      `/api/current/names/${encodeURIComponent(row.original || row.variable)}/profile`)
    profileName.value = row.original
    profileData.value = data.profile
    profileVisible.value = true
  } catch (e) {
    toastError(message, e)
  }
}

const columns: DataTableColumns<NameRow> = [
  { title: '#', key: 'index', width: 50, render: (_, i) => i + 1 },
  { title: '变量名', key: 'variable', width: 110 },
  {
    title: '原文人名', key: 'original', width: 140,
    sorter: (a, b) => a.original.localeCompare(b.original),
  },
  {
    title: '中文名（点击编辑）', key: 'translated', width: 160,
    render: (r) => {
      if (isEditing(r)) {
        return h(NInput, {
          value: editingText.value,
          'onUpdate:value': (v: string) => { editingText.value = v },
          onBlur: () => onCommit(r),
          onKeydown: (e: KeyboardEvent) => {
            if (e.key === 'Enter' && !e.isComposing && e.keyCode !== 229) onCommit(r)
          },
          autofocus: true,
          size: 'small',
        })
      }
      // 无显示名角色没有可翻译的名字：不允许手动输入翻译
      if (!r.original) {
        return h('span', { class: 'rt-t3', style: 'display: block; min-height: 20px' },
          '（无需翻译）')
      }
      return h('span', {
        class: r.translated ? undefined : 'rt-t3',
        style: 'cursor: text; display: block; min-height: 20px',
        onClick: () => { if (!hasActiveTextSelection()) startEdit(r) },
      }, r.translated || '（点击输入中文名）')
    },
  },
  { title: '台词数', key: 'lines', width: 80, sorter: (a, b) => a.lines - b.lines },
  {
    title: '翻译', key: 'name_status', width: 90,
    render: (r) => h(NTag, {
      size: 'small',
      type: processing.value.has(rowKey(r)) ? 'warning' : r.name_done ? 'success' : 'default',
    }, () => {
      // 无显示名角色（泛指形参/玩家命名主角）没有可翻译的名字，不算待翻译
      if (!r.original) return '无需翻译'
      return processing.value.has(rowKey(r)) ? '处理中' : r.name_done ? '完成' : '待翻译'
    }),
  },
  {
    title: '分析', key: 'analysis_status', width: 90,
    render: (r) => h(NTag, {
      size: 'small',
      type: processing.value.has(rowKey(r)) ? 'warning' : r.analyzed ? 'success' : 'default',
    }, () => processing.value.has(rowKey(r)) ? '处理中' : r.analyzed ? '已完成' : '未分析'),
  },
  {
    title: '操作', key: 'actions', width: 190,
    render: (r) => h(NSpace, { size: 4 }, () => [
      // 主操作：译名+画像一次出（名字带台词上下文，质量高于单独盲译）
      h(NButton, {
        size: 'tiny', type: 'primary', quaternary: true,
        disabled: (!r.original && !r.variable) || !session.hasTranslator,
        loading: processing.value.has(rowKey(r)),
        onClick: () => runOne(r, 'both'),
      }, () => '翻译+分析'),
      // 其余四个动作收进「⋯」（频率低，且多数字对模型门禁敏感）
      h(NDropdown, { options: rowMenu(r), onSelect: (k: string) => onRowMenu(k, r) }, () =>
        h(NButton, { size: 'tiny', quaternary: true }, () => '更多 ▾')),
    ]),
  },
]

onMounted(load)
usePageScroll(load)  // keep-alive：返回保留分页/筛选并后台刷新
</script>

<template>
  <div>
    <n-space align="center" class="rt-page-toolbar" style="width: 100%; margin-bottom: 12px" wrap>
      <page-header title="人名与角色" help-section="4,8" />
      <!-- L1 主操作 -->
      <n-dropdown :options="batchMenuOptions" @select="onBatchMenu">
        <n-button size="small" type="primary" :render-icon="renderIcon(LanguageOutline)">全部翻译+分析 ▾</n-button>
      </n-dropdown>
      <!-- L2 次操作 -->
      <n-button size="small" :render-icon="renderIcon(ShareOutline)" @click="exportVisible = true">导出</n-button>
      <n-button size="small" :render-icon="renderIcon(DownloadOutline)" @click="importVisible = true">导入</n-button>
      <!-- 手动刷新（每页常驻可见） -->
      <n-button size="small" quaternary :render-icon="renderIcon(RefreshOutline)" @click="load">刷新</n-button>
      <!-- L3 更多 -->
      <n-dropdown :options="moreMenuOptions" @select="onMoreMenu">
        <n-button size="small" quaternary>⋯</n-button>
      </n-dropdown>
      <n-text depth="3" style="font-size: 12px">
        {{ stats.total }} 人名，翻译 {{ stats.translated }}，分析 {{ stats.analyzed }}
      </n-text>
    </n-space>

    <context-hint
      v-if="stats.total && stats.translated < stats.total"
      storage-key="rt.hint.names.first"
      text="先翻译主要角色即可开始对话翻译，不必等全部角色完成——人名会注入之后所有翻译。"
    />

    <n-space align="center" style="margin-bottom: 10px" wrap>
      <n-input-group style="width: 280px">
        <n-input
          v-model:value="searchText" size="small" clearable
          placeholder="搜索人名/变量名/中文名"
        />
      </n-input-group>
      <n-select
        v-model:value="statusFilter" size="small" style="width: 130px"
        :options="[
          { label: '全部', value: '' },
          { label: '未翻译', value: 'untranslated' },
          { label: '已翻译', value: 'translated' },
          { label: '未分析', value: 'unanalyzed' },
        ]"
      />
    </n-space>

    <n-data-table
      :columns="columns" :data="filteredRows" :loading="loading"
      :row-key="rowKey" size="small"
      :pagination="{ pageSize: 50 }"
      :render-empty="() => h(NEmpty, { description: '暂无人名' })"
    />

    <!-- 导出翻译文本对话框 -->
    <export-texts-dialog v-model:show="exportVisible" :preset-types="['names']" />

    <!-- 导入翻译文本对话框 -->
    <import-texts-dialog v-model:show="importVisible" :preset-types="['names']" @imported="onImported" />
    <import-from-project-dialog
      v-model:show="crossImportVisible" :preset-types="['names']" @imported="onImported"
    />

    <n-modal v-model:show="profileVisible" preset="card"
             :title="`${profileName} - 人物特征`" style="width: min(640px, 96vw)">
      <div class="modal-scroll">
        <div v-for="(value, key) in profileData" :key="key" style="margin-bottom: 10px">
          <n-text strong class="rt-info">【{{ key }}】</n-text>
          <div style="padding-left: 12px; white-space: pre-wrap; font-size: 13px">{{ value }}</div>
        </div>
      </div>
    </n-modal>
  </div>
</template>
