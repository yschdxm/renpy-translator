<script setup lang="ts">
/** 术语表：服务端分页/排序/筛选，译文行内编辑，手动新增/删除术语 */
import { computed, h, onMounted, reactive, ref, watch } from 'vue'
import {
  NButton, NDataTable, NEmpty, NInput, NInputGroup, NModal, NPagination,
  NPopconfirm, NSelect, NSpace, NTag, NText, useMessage,
} from 'naive-ui'
import type { DataTableColumns, DataTableSortState } from 'naive-ui'
import { AddOutline, RefreshOutline } from '@vicons/ionicons5'
import { api, toastError, toastOk } from '../api/client'
import { renderIcon } from '../components/icons'
import { useInlineEdit } from '../composables/useInlineEdit'

const message = useMessage()

interface Row {
  en_term: string
  cn_term: string
  term_type: string
  source: string
  created_at: string
}

const rows = ref<Row[]>([])
const total = ref(0)
const loading = ref(false)
const query = reactive({
  page: 0, size: 50, search: '', source: '',
  sort_by: 'en_term', sort_order: 'asc' as 'asc' | 'desc',
})

async function load() {
  loading.value = true
  try {
    const params = new URLSearchParams({
      page: String(query.page), size: String(query.size),
      search: query.search, source: query.source,
      sort_by: query.sort_by, sort_order: query.sort_order,
    })
    const data = await api.get<{ rows: Row[]; total: number }>(
      `/api/current/glossary?${params}`)
    rows.value = data.rows
    total.value = data.total
  } catch (e) {
    toastError(message, e)
  } finally {
    loading.value = false
  }
}

// ---- 行内编辑（译文） ----
const { editingText, isEditing, startEdit, commitEdit } = useInlineEdit<Row>(
  (row) => row.en_term,
  (row) => row.cn_term || '',
  async (row, value) => {
    await api.patch(`/api/current/glossary/${encodeURIComponent(row.en_term)}`, {
      cn_term: value, term_type: row.term_type,
    })
    row.cn_term = value
  })

async function onCommit(row: Row) {
  try {
    await commitEdit(row)
  } catch (e) {
    toastError(message, e)
  }
}

// ---- 删除 ----
async function deleteTerm(row: Row) {
  try {
    await api.del(`/api/current/glossary/${encodeURIComponent(row.en_term)}`)
    toastOk(message, `已删除术语「${row.en_term}」`)
    // 删的是本页最后一条且不在第一页时，回退一页避免空白页
    if (rows.value.length === 1 && query.page > 0) query.page--
    await load()
  } catch (e) {
    toastError(message, e)
  }
}

// ---- 新增术语 ----
const createVisible = ref(false)
const createForm = reactive({ en_term: '', cn_term: '', term_type: 'other' })
const creating = ref(false)

function openCreate() {
  createForm.en_term = ''
  createForm.cn_term = ''
  createForm.term_type = 'other'
  createVisible.value = true
}

async function submitCreate() {
  if (!createForm.en_term.trim()) {
    message.warning('请填写原文')
    return
  }
  creating.value = true
  try {
    await api.post('/api/current/glossary', {
      en_term: createForm.en_term.trim(),
      cn_term: createForm.cn_term.trim(),
      term_type: createForm.term_type,
    })
    toastOk(message, `已添加术语「${createForm.en_term.trim()}」`)
    createVisible.value = false
    await load()
  } catch (e) {
    toastError(message, e)
  } finally {
    creating.value = false
  }
}

// ---- 服务端排序（单列受控） ----
function onSorterChange(sorter: DataTableSortState | null) {
  if (sorter && sorter.order) {
    query.sort_by = String(sorter.columnKey)
    query.sort_order = sorter.order === 'descend' ? 'desc' : 'asc'
  } else {
    query.sort_by = 'en_term'
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

function fmtTime(s: string): string {
  return s ? s.slice(0, 16).replace('T', ' ') : ''
}

// ---- 表格列 ----
const columns = computed<DataTableColumns<Row>>(() => [
  {
    title: '原文', key: 'en_term', ellipsis: { tooltip: true },
    sorter: true, sortOrder: sortOrderOf('en_term'),
  },
  {
    title: '译文（点击编辑）', key: 'cn_term', ellipsis: { tooltip: true },
    sorter: true, sortOrder: sortOrderOf('cn_term'),
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
        style: `cursor: text; display: block; min-height: 20px; ${r.cn_term ? '' : 'color: #666'}`,
        onClick: () => startEdit(r),
      }, r.cn_term || '（点击输入译文）')
    },
  },
  {
    title: '来源', key: 'source', width: 90,
    sorter: true, sortOrder: sortOrderOf('source'),
    render: (r) => {
      if (r.source === 'ai') return h(NTag, { size: 'small' }, () => 'AI')
      if (r.source === 'manual') return h(NTag, { size: 'small', type: 'info' }, () => '手动')
      return r.source
    },
  },
  {
    title: '类型', key: 'term_type', width: 90,
    render: (r) => r.term_type === 'ui' ? 'UI' : '游戏',
  },
  {
    title: '创建时间', key: 'created_at', width: 140,
    sorter: true, sortOrder: sortOrderOf('created_at'),
    render: (r) => fmtTime(r.created_at),
  },
  {
    title: '操作', key: 'actions', width: 80,
    render: (r) => h(NPopconfirm, {
      onPositiveClick: () => deleteTerm(r),
    }, {
      trigger: () => h(NButton, {
        type: 'error', quaternary: true, size: 'tiny',
      }, () => '删除'),
      default: () => `确定删除术语「${r.en_term}」？`,
    }),
  },
])

onMounted(load)

watch(() => query.source, () => { query.page = 0; load() })
</script>

<template>
  <div>
    <n-space align="center" style="margin-bottom: 12px" wrap>
      <h2 style="margin: 0">术语表</h2>
      <n-button size="small" type="primary" :render-icon="renderIcon(AddOutline)" @click="openCreate">新增术语</n-button>
      <span style="flex: 1" />
      <n-button size="small" quaternary :render-icon="renderIcon(RefreshOutline)" @click="load">刷新</n-button>
    </n-space>

    <n-space align="center" style="margin-bottom: 10px" wrap>
      <n-select
        v-model:value="query.source" size="small" style="width: 130px"
        :options="[
          { label: '全部', value: '' },
          { label: 'AI 添加', value: 'ai' },
          { label: '手动添加', value: 'manual' },
        ]"
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
      :row-key="(r: Row) => r.en_term" size="small" :scroll-x="900"
      :render-empty="() => h(NEmpty, { description: '暂无术语' })"
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

    <!-- 新增术语 -->
    <n-modal v-model:show="createVisible" preset="card" title="新增术语" style="width: 480px">
      <n-space vertical>
        <n-input v-model:value="createForm.en_term" placeholder="原文（必填）" />
        <n-input v-model:value="createForm.cn_term" placeholder="译文" />
        <n-select
          v-model:value="createForm.term_type"
          :options="[
            { label: '游戏术语', value: 'other' },
            { label: 'UI 术语', value: 'ui' },
          ]"
        />
      </n-space>
      <template #footer>
        <n-space justify="end">
          <n-button :disabled="creating" @click="createVisible = false">取消</n-button>
          <n-button type="primary" :loading="creating" @click="submitCreate">保存</n-button>
        </n-space>
      </template>
    </n-modal>
  </div>
</template>
