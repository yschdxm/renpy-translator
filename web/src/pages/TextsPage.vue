<script setup lang="ts">
/** 文本翻译页（UI 字符串 / 对话共用，contentType 区分） */
import { computed, h, onMounted, reactive, ref, watch } from 'vue'
import {
  NButton, NDataTable, NEmpty, NInput, NModal, NPagination, NSelect, NSpace,
  NText, NInputGroup, useMessage,
} from 'naive-ui'
import type { DataTableColumns, DataTableSortState } from 'naive-ui'
import { BookOutline, CodeSlashOutline, LanguageOutline, LocateOutline, RefreshOutline, SparklesOutline, WarningOutline } from '@vicons/ionicons5'
import { api, toastError, toastOk } from '../api/client'
import { renderIcon } from '../components/icons'
import FailedBatchesDialog from '../components/FailedBatchesDialog.vue'
import { useInlineEdit } from '../composables/useInlineEdit'
import { useJobTask } from '../composables/useJobTask'
import { useSessionStore } from '../stores/session'

const props = defineProps<{ contentType: 'ui' | 'dialogue' }>()
const message = useMessage()
const session = useSessionStore()
const { runJob } = useJobTask()

interface Row {
  id: number
  character?: string
  original_text: string
  translated_text: string
  label?: string
  file_path?: string
  line_number?: number
  context_hint?: string
}

const rows = ref<Row[]>([])
const total = ref(0)
const loading = ref(false)
const query = reactive({
  page: 0, size: 50, filter_mode: 'all', search: '', character: '',
  sort_by: '', sort_order: 'asc' as 'asc' | 'desc',
})
const characters = ref<Array<{ label: string; value: string }>>([])
const translatingIds = ref<Set<number>>(new Set())
const failedBatchCount = ref(0)
const failedDialogVisible = ref(false)

const isDialogue = computed(() => props.contentType === 'dialogue')
const title = computed(() => (isDialogue.value ? '对话翻译' : '字符串翻译'))

async function load() {
  loading.value = true
  try {
    const params = new URLSearchParams({
      page: String(query.page), size: String(query.size),
      filter_mode: query.filter_mode, search: query.search,
      character: query.character,
      sort_by: query.sort_by, sort_order: query.sort_order,
    })
    const data = await api.get<{ rows: Row[]; total: number }>(
      `/api/current/texts/${props.contentType}?${params}`)
    rows.value = data.rows
    total.value = data.total
    await loadFailedCount()
  } catch (e) {
    toastError(message, e)
  } finally {
    loading.value = false
  }
}

async function loadFailedCount() {
  try {
    const data = await api.get<{ count: number }>(
      `/api/current/texts/${props.contentType}/failed-batches`)
    failedBatchCount.value = data.count
  } catch { /* 计数失败不挡主流程 */ }
}

async function loadCharacters() {
  if (!isDialogue.value) return
  const data = await api.get<{ characters: string[]; variable_map: Record<string, string> }>(
    `/api/current/texts/${props.contentType}/characters`)
  characters.value = [
    { label: '全部角色', value: '' },
    ...data.characters.map((v) => ({
      label: data.variable_map[v] ? `${data.variable_map[v]} (${v})` : v,
      value: v,
    })),
  ]
}

// ---- 行内编辑 ----
const { editingText, isEditing, startEdit, commitEdit } = useInlineEdit<Row>(
  (row) => row.id,
  (row) => row.translated_text || '',
  async (row, value) => {
    const old = row.translated_text
    row.translated_text = value
    try {
      await api.patch(`/api/current/texts/${props.contentType}/${row.id}`, {
        translated_text: value,
      })
    } catch (e) {
      row.translated_text = old  // 响亮回滚
      throw e
    }
  })

async function onCommit(row: Row) {
  try {
    await commitEdit(row)
  } catch (e) {
    toastError(message, e)
  }
}

// ---- 服务端排序（单列受控） ----
const sortableKeys = computed(() =>
  isDialogue.value ? ['original_text', 'character'] : ['original_text'])

function onSorterChange(sorter: DataTableSortState | null) {
  if (sorter && sorter.order && sortableKeys.value.includes(String(sorter.columnKey))) {
    query.sort_by = String(sorter.columnKey)
    query.sort_order = sorter.order === 'descend' ? 'desc' : 'asc'
  } else {
    query.sort_by = ''
    query.sort_order = 'asc'
  }
  query.page = 0
  load()
}

/** 列的受控排序态 */
function sortOrderOf(key: string): 'ascend' | 'descend' | false {
  if (query.sort_by !== key) return false
  return query.sort_order === 'desc' ? 'descend' : 'ascend'
}

// ---- 单条翻译 ----
async function translateOne(row: Row) {
  translatingIds.value.add(row.id)
  try {
    const data = await api.post<{ translated_text: string }>(
      `/api/current/texts/${props.contentType}/${row.id}/translate`)
    row.translated_text = data.translated_text
    await session.refresh()
  } catch (e) {
    toastError(message, e)
  } finally {
    translatingIds.value.delete(row.id)
  }
}

// ---- 批量翻译（任务）：终态后刷新表格与统计；有失败条目时自动打开核验对话框 ----
async function onTranslateJobDone() {
  await load()
  if (failedBatchCount.value > 0) failedDialogVisible.value = true
}

async function translateAll() {
  await runJob(
    () => api.post(`/api/current/texts/${props.contentType}/translate-all`),
    onTranslateJobDone,
    { emptyMessage: '没有待翻译的内容' })
}

async function translatePage() {
  await runJob(
    () => api.post(`/api/current/texts/${props.contentType}/translate-page`, { ...query }),
    onTranslateJobDone,
    { emptyMessage: '本页没有待翻译的内容' })
}

// ---- 上下文对话框 ----
const contextVisible = ref(false)
const contextRows = ref<{ before: Row[]; after: Row[] }>({ before: [], after: [] })
const contextTarget = ref<Row | null>(null)

async function showContext(row: Row) {
  try {
    contextRows.value = await api.get(
      `/api/current/texts/${props.contentType}/${row.id}/context?n=5`)
    contextTarget.value = row
    contextVisible.value = true
  } catch (e) {
    toastError(message, e)
  }
}

// ---- 重建上下文（UI 字符串） ----
async function rebuildHints() {
  await runJob(
    () => api.post(`/api/current/texts/ui/hints/rebuild`),
    () => load())
}

// ---- 提取内嵌文本（UI 字符串） ----
async function extractEmbedded() {
  await runJob(
    () => api.post('/api/current/embedded/scan'),
    () => load())
}

// ---- 风格指南（对话） ----
const guideVisible = ref(false)
const guideText = ref('')
const guideGenerating = ref(false)

async function openStyleGuide() {
  const data = await api.get<{ style_guide: string }>('/api/current/style-guide')
  guideText.value = data.style_guide
  guideVisible.value = true
}

async function generateGuide() {
  guideGenerating.value = true
  try {
    const data = await api.post<{ style_guide: string }>(
      '/api/current/style-guide/generate')
    guideText.value = data.style_guide
    toastOk(message, '风格指南已生成，可编辑后保存')
  } catch (e) {
    toastError(message, e)
  } finally {
    guideGenerating.value = false
  }
}

async function saveGuide() {
  await api.put('/api/current/style-guide', { style_guide: guideText.value })
  toastOk(message, '风格指南已保存，后续翻译将按此风格执行')
  guideVisible.value = false
}

// ---- 表格列 ----
const columns = computed<DataTableColumns<Row>>(() => {
  const cols: DataTableColumns<Row> = [
    { title: '#', key: 'id', width: 60 },
  ]
  if (isDialogue.value) {
    cols.push({ title: '角色', key: 'character', width: 90,
                sorter: true, sortOrder: sortOrderOf('character'),
                render: (r) => r.character || '旁白' })
  }
  cols.push({
    title: '原文', key: 'original_text', ellipsis: { tooltip: true },
    sorter: true, sortOrder: sortOrderOf('original_text'),
    render: (r) => h('span', { style: 'white-space: pre-wrap' }, r.original_text),
  })
  cols.push({
    title: '译文（点击编辑）', key: 'translated_text', ellipsis: { tooltip: true },
    render: (r) => {
      if (isEditing(r)) {
        return h(NInput, {
          value: editingText.value,
          'onUpdate:value': (v: string) => { editingText.value = v },
          onBlur: () => onCommit(r),
          onKeydown: (e: KeyboardEvent) => { if (e.key === 'Enter') onCommit(r) },
          autofocus: true,
          size: 'small',
        })
      }
      return h('span', {
        style: `cursor: text; display: block; min-height: 20px; white-space: pre-wrap; ${r.translated_text ? '' : 'color: #666'}`,
        onClick: () => startEdit(r),
      }, r.translated_text || '（点击输入译文）')
    },
  })
  cols.push({
    title: '操作', key: 'actions', width: 150,
    render: (r) => h(NSpace, { size: 4 }, () => [
      h(NButton, {
        size: 'tiny', type: 'primary', quaternary: true,
        loading: translatingIds.value.has(r.id),
        onClick: () => translateOne(r),
      }, () => 'AI翻译'),
      h(NButton, {
        size: 'tiny', quaternary: true, onClick: () => showContext(r),
      }, () => '上下文'),
    ]),
  })
  return cols
})

onMounted(() => {
  load()
  loadCharacters()
})

watch(() => [query.filter_mode, query.character], () => { query.page = 0; load() })
</script>

<template>
  <div>
    <n-space align="center" style="margin-bottom: 12px" wrap>
      <h2 style="margin: 0">{{ title }}</h2>
      <n-button size="small" type="primary" :render-icon="renderIcon(LanguageOutline)" @click="translateAll">全部翻译</n-button>
      <n-button size="small" @click="translatePage">翻译本页</n-button>
      <n-button
        v-if="failedBatchCount > 0" size="small" type="warning"
        :render-icon="renderIcon(WarningOutline)" @click="failedDialogVisible = true"
      >失败条目 ({{ failedBatchCount }})</n-button>
      <n-button v-if="!isDialogue" size="small" :render-icon="renderIcon(LocateOutline)" @click="rebuildHints">重建上下文</n-button>
      <n-button v-if="!isDialogue" size="small" type="warning" :render-icon="renderIcon(CodeSlashOutline)" @click="extractEmbedded">提取内嵌文本</n-button>
      <n-button v-if="isDialogue" size="small" :render-icon="renderIcon(BookOutline)" @click="openStyleGuide">风格指南</n-button>
      <n-button size="small" quaternary :render-icon="renderIcon(RefreshOutline)" @click="load">刷新</n-button>
    </n-space>

    <n-space align="center" style="margin-bottom: 10px" wrap>
      <n-select
        v-model:value="query.filter_mode" size="small" style="width: 130px"
        :options="[
          { label: '全部', value: 'all' },
          { label: '未翻译', value: 'untranslated' },
          { label: '已翻译', value: 'translated' },
        ]"
      />
      <n-select
        v-if="isDialogue"
        v-model:value="query.character" size="small" style="width: 200px"
        :options="characters" placeholder="角色筛选"
      />
      <n-input-group style="width: 280px">
        <n-input
          v-model:value="query.search" size="small" placeholder="搜索原文/译文"
          @keydown.enter="query.page = 0; load()"
        />
        <n-button size="small" @click="query.page = 0; load()">搜索</n-button>
      </n-input-group>
      <n-text depth="3" style="font-size: 12px">共 {{ total }} 条</n-text>
    </n-space>

    <n-data-table
      :columns="columns" :data="rows" :loading="loading"
      :row-key="(r: Row) => r.id" size="small" :scroll-x="900"
      :render-empty="() => h(NEmpty, { description: '暂无文本' })"
      @update:sorter="onSorterChange"
    />

    <n-space justify="end" style="margin-top: 10px">
      <n-pagination
        :page="query.page + 1"
        :item-count="total"
        :page-size="query.size"
        @update:page="(p: number) => { query.page = p - 1; load() }"
      />
    </n-space>

    <!-- 上下文对话框 -->
    <n-modal v-model:show="contextVisible" preset="card" title="上下文" style="width: 640px">
      <div v-if="contextTarget" style="font-size: 13px">
        <div v-for="r in contextRows.before" :key="'b' + r.id" style="color: #888; padding: 2px 0">
          {{ r.character || '旁白' }}: {{ r.original_text }}
          <span v-if="r.translated_text" style="color: #6a6">→ {{ r.translated_text }}</span>
        </div>
        <div style="background: #4a4420; padding: 4px 6px; border-radius: 4px; margin: 4px 0">
          <b>{{ contextTarget.character || '旁白' }}: {{ contextTarget.original_text }}</b>
        </div>
        <div v-for="r in contextRows.after" :key="'a' + r.id" style="color: #888; padding: 2px 0">
          {{ r.character || '旁白' }}: {{ r.original_text }}
          <span v-if="r.translated_text" style="color: #6a6">→ {{ r.translated_text }}</span>
        </div>
      </div>
    </n-modal>

    <!-- 失败批次核验对话框 -->
    <failed-batches-dialog
      v-model:show="failedDialogVisible" :content-type="contentType"
      @changed="load"
    />

    <!-- 风格指南对话框 -->
    <n-modal v-model:show="guideVisible" preset="card" title="翻译风格指南" style="width: 720px">
      <n-input
        v-model:value="guideText" type="textarea" :autosize="{ minRows: 12, maxRows: 24 }"
        placeholder="描述译文的整体风格要求，AI 翻译时会遵循..."
      />
      <template #footer>
        <n-space justify="end">
          <n-button :loading="guideGenerating" :render-icon="renderIcon(SparklesOutline)" @click="generateGuide">AI 生成</n-button>
          <n-button @click="guideVisible = false">取消</n-button>
          <n-button type="primary" @click="saveGuide">保存</n-button>
        </n-space>
      </template>
    </n-modal>
  </div>
</template>
