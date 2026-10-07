<script setup lang="ts">
/** 翻译文本导出对话框（对话/UI/人名/术语四页共用）：
 *  选数据类型 + 格式(txt/json/xlsx/docx) + 导出列 + 可选文件名；
 *  TextsPage 传入 filterScope 时支持「仅导出当前筛选结果」。
 *  产物存 exports/{项目名}/，成功后可直接下载或在文件管理器中定位。 */
import { computed, reactive, ref, watch } from 'vue'
import {
  NButton, NCheckbox, NCheckboxGroup, NInput, NModal, NRadio, NRadioGroup,
  NSpace, NText, useMessage,
} from 'naive-ui'
import { api, toastError } from '../api/client'
import { useSessionStore } from '../stores/session'

type ExportType = 'dialogue' | 'ui' | 'names' | 'glossary'

interface FilterScope {
  content_type: 'dialogue' | 'ui'
  filter_mode: string
  search: string
  character: string
}

const props = defineProps<{
  show: boolean
  presetTypes: ExportType[]
  filterScope?: FilterScope | null
}>()
const emit = defineEmits<{ 'update:show': [boolean] }>()

const message = useMessage()
const session = useSessionStore()

// 与后端 services/text_export.py COLUMN_DEFS 对齐（列 key + 中文表头）
const TYPE_LABELS: Record<ExportType, string> = {
  dialogue: '对话翻译', ui: 'UI 字符串', names: '人名表', glossary: '术语表',
}
const COLUMN_OPTIONS: Record<ExportType, Array<{ label: string; value: string }>> = {
  dialogue: [
    { label: '原文', value: 'original' }, { label: '译文', value: 'translated' },
    { label: '说话人', value: 'character' }, { label: '来源文件', value: 'file' },
    { label: '行号', value: 'line' }, { label: '标签', value: 'label' },
    { label: '已翻译', value: 'status' },
  ],
  ui: [
    { label: '原文', value: 'original' }, { label: '译文', value: 'translated' },
    { label: '来源文件', value: 'file' }, { label: '行号', value: 'line' },
    { label: '标签', value: 'label' }, { label: '上下文提示', value: 'context' },
    { label: '已翻译', value: 'status' },
  ],
  names: [
    { label: '原名', value: 'original' }, { label: '译名', value: 'translated' },
    { label: '变量名', value: 'variable' }, { label: '台词数', value: 'lines' },
  ],
  glossary: [
    { label: '英文术语', value: 'en' }, { label: '中文术语', value: 'cn' },
    { label: '类型', value: 'type' }, { label: '来源', value: 'source' },
  ],
}
const DEFAULT_COLUMNS: Record<ExportType, string[]> = {
  dialogue: ['original', 'translated'],
  ui: ['original', 'translated'],
  names: ['original', 'translated'],
  glossary: ['en', 'cn'],
}

const selectedTypes = ref<ExportType[]>([])
const format = ref<'txt' | 'json' | 'xlsx' | 'docx' | 'tword'>('xlsx')
const selectedColumns = reactive<Record<ExportType, string[]>>({
  dialogue: [], ui: [], names: [], glossary: [],
})
const scope = ref<'all' | 'filter'>('all')
const filename = ref('')
const exporting = ref(false)
const result = ref<{ file: string; counts: Record<string, number> } | null>(null)

// 打开时重置：预勾选当前页类型 + 默认列，清掉上次结果
watch(() => props.show, (v) => {
  if (!v) return
  selectedTypes.value = [...props.presetTypes]
  for (const t of Object.keys(DEFAULT_COLUMNS) as ExportType[]) {
    selectedColumns[t] = [...DEFAULT_COLUMNS[t]]
  }
  scope.value = 'all'
  filename.value = ''
  result.value = null
})

const filterUsable = computed(() =>
  !!props.filterScope && selectedTypes.value.includes(props.filterScope.content_type))

// 翻译 Word：仅原文每条一行（供译者翻译后从「导入」导回），无需选列
const isTword = computed(() => format.value === 'tword')

const filterActive = computed(() => {
  const f = props.filterScope
  return !!f && (f.filter_mode !== 'all' || !!f.search || !!f.character)
})

const filterSummary = computed(() => {
  const f = props.filterScope
  if (!f) return ''
  const parts: string[] = []
  if (f.filter_mode !== 'all') parts.push(f.filter_mode === 'translated' ? '已翻译' : '未翻译')
  if (f.character) parts.push(`角色「${f.character}」`)
  if (f.search) parts.push(`搜索「${f.search}」`)
  return parts.length ? parts.join('，') : '当前无筛选条件（等同全部）'
})

function close() {
  if (!exporting.value) emit('update:show', false)
}

async function submit() {
  if (!selectedTypes.value.length) {
    message.warning('请至少选择一项导出内容')
    return
  }
  if (!isTword.value) {
    for (const t of selectedTypes.value) {
      if (!selectedColumns[t].length) {
        message.warning(`「${TYPE_LABELS[t]}」请至少选择一列`)
        return
      }
    }
  }
  exporting.value = true
  try {
    const useFilter = scope.value === 'filter' && filterUsable.value && props.filterScope
    const data = await api.post<{ file: string; counts: Record<string, number> }>(
      '/api/current/export/texts', {
        types: selectedTypes.value,
        format: format.value,
        columns: Object.fromEntries(selectedTypes.value.map((t) => [t, selectedColumns[t]])),
        filename: filename.value.trim(),
        filter: useFilter ? { ...props.filterScope } : null,
      })
    result.value = data
  } catch (e) {
    toastError(message, e)
  } finally {
    exporting.value = false
  }
}

const downloadUrl = computed(() => {
  if (!result.value) return ''
  return `/api/projects/${encodeURIComponent(session.currentProject)}`
    + `/packages/${encodeURIComponent(result.value.file)}`
})

async function revealFile() {
  if (!result.value) return
  try {
    await api.post(
      `/api/projects/${encodeURIComponent(session.currentProject)}/packages/reveal`
      + `?file=${encodeURIComponent(result.value.file)}`)
  } catch (e) {
    toastError(message, e)
  }
}
</script>

<template>
  <n-modal :show="show" preset="card" title="导出翻译文本" style="width: 640px"
           :mask-closable="!exporting" :closable="!exporting" :close-on-esc="!exporting"
           @update:show="emit('update:show', $event)">
    <!-- 结果态 -->
    <n-space v-if="result" vertical>
      <n-text>已导出：{{ result.file }}</n-text>
      <n-text depth="3" style="font-size: 12px">
        <template v-for="(n, t) in result.counts" :key="t">
          {{ TYPE_LABELS[t as ExportType] }} {{ n }} 条&nbsp;&nbsp;
        </template>
      </n-text>
      <n-space>
        <n-button size="small" @click="revealFile">打开所在目录</n-button>
        <a :href="downloadUrl" :download="result.file" style="text-decoration: none">
          <n-button size="small" type="primary">下载文件</n-button>
        </a>
      </n-space>
    </n-space>

    <!-- 配置态 -->
    <n-space v-else vertical size="medium">
      <div>
        <n-text depth="3" style="font-size: 12px">导出内容</n-text>
        <n-checkbox-group v-model:value="selectedTypes">
          <n-space>
            <n-checkbox v-for="(label, t) in TYPE_LABELS" :key="t" :value="t" :label="label" />
          </n-space>
        </n-checkbox-group>
      </div>

      <div>
        <n-text depth="3" style="font-size: 12px">格式</n-text>
        <n-radio-group v-model:value="format">
          <n-space>
            <n-radio value="xlsx">Excel (.xlsx)</n-radio>
            <n-radio value="docx">Word (.docx)</n-radio>
            <n-radio value="tword">翻译 Word (.docx)</n-radio>
            <n-radio value="txt">文本 (.txt)</n-radio>
            <n-radio value="json">JSON (.json)</n-radio>
          </n-space>
        </n-radio-group>
        <n-text v-if="isTword" depth="3" style="display: block; font-size: 12px">
          仅导出原文、每条一行，除此之外什么都没有；译者翻好后用各页面的「导入」按行对应导回。
        </n-text>
      </div>

      <template v-if="!isTword">
        <div v-for="t in selectedTypes" :key="t">
          <n-text depth="3" style="font-size: 12px">{{ TYPE_LABELS[t] }} · 导出列</n-text>
          <n-checkbox-group v-model:value="selectedColumns[t]">
            <n-space>
              <n-checkbox v-for="opt in COLUMN_OPTIONS[t]" :key="opt.value"
                          :value="opt.value" :label="opt.label" />
            </n-space>
          </n-checkbox-group>
        </div>
      </template>

      <div v-if="filterUsable">
        <n-text depth="3" style="font-size: 12px">范围（仅对{{ TYPE_LABELS[filterScope!.content_type] }}生效）</n-text>
        <n-radio-group v-model:value="scope">
          <n-space>
            <n-radio value="all">全部</n-radio>
            <n-radio value="filter" :disabled="!filterActive">仅当前筛选结果</n-radio>
          </n-space>
        </n-radio-group>
        <n-text v-if="scope === 'filter'" depth="3" style="display: block; font-size: 12px">
          {{ filterSummary }}
        </n-text>
      </div>

      <n-input v-model:value="filename" placeholder="文件名（可选，留空自动命名，无需扩展名）" />
    </n-space>

    <template #footer>
      <n-space justify="end">
        <n-button :disabled="exporting" @click="close">{{ result ? '完成' : '取消' }}</n-button>
        <n-button v-if="!result" type="primary" :loading="exporting" @click="submit">导出</n-button>
      </n-space>
    </template>
  </n-modal>
</template>
