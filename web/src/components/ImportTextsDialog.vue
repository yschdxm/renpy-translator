<script setup lang="ts">
/** 翻译文本导入对话框（对话/UI/人名/术语四页共用）：
 *  支持 .json（本工具导出格式，按原文匹配）与翻译 .docx（仅原文每条一行，
 *  按段落顺序对齐——类型与范围需与导出时一致）。选择文件后直接导入。 */
import { computed, ref, watch } from 'vue'
import {
  NButton, NCheckbox, NCheckboxGroup, NModal, NRadio, NRadioGroup, NSpace,
  NText, useMessage,
} from 'naive-ui'
import { api, toastError, toastOk } from '../api/client'
import { useSessionStore } from '../stores/session'
import FileDropSelect from './FileDropSelect.vue'

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
const emit = defineEmits<{ 'update:show': [boolean]; imported: [] }>()

const message = useMessage()
const session = useSessionStore()

const TYPE_LABELS: Record<ExportType, string> = {
  dialogue: '对话翻译', ui: 'UI 字符串', names: '人名表', glossary: '术语表',
}

const file = ref<File | null>(null)
const selectedTypes = ref<ExportType[]>([])
const scope = ref<'all' | 'filter'>('all')
const importing = ref(false)

watch(() => props.show, (v) => {
  if (!v) return
  file.value = null
  selectedTypes.value = [...props.presetTypes]
  scope.value = 'all'
})

const kind = computed<'json' | 'tword' | null>(() => {
  const ext = file.value?.name.toLowerCase().split('.').pop()
  return ext === 'json' ? 'json' : ext === 'docx' ? 'tword' : null
})

// accept 只约束系统文件选择器，拖入可绕过，选中后立即校验
watch(file, (f) => {
  if (f && !kind.value) {
    message.warning('仅支持 .json 或翻译 .docx 文件')
    file.value = null
  }
})

const filterUsable = computed(() =>
  !!props.filterScope && selectedTypes.value.includes(props.filterScope.content_type))

function close() {
  if (!importing.value) emit('update:show', false)
}

async function submit() {
  if (!file.value) {
    message.warning('请先选择文件')
    return
  }
  if (kind.value === 'tword' && !selectedTypes.value.length) {
    message.warning('翻译 Word 按行对应导回，请选择与导出时一致的内容类型')
    return
  }
  importing.value = true
  try {
    const form = new FormData()
    form.append('file', file.value)
    if (kind.value === 'tword') {
      form.append('types', JSON.stringify(selectedTypes.value))
      const useFilter = scope.value === 'filter' && filterUsable.value && props.filterScope
      if (useFilter) form.append('filter', JSON.stringify(props.filterScope))
    }
    const data = await api.postForm<{
      counts: Record<string, number>
      total: number
      stats: Record<string, Record<string, number>>
    }>('/api/current/import/texts', form)
    const parts = Object.entries(data.counts)
      .map(([t, n]) => `${TYPE_LABELS[t as ExportType]} ${n} 条`)
    const unmatched = Object.values(data.stats)
      .reduce((s, st) => s + (st.unmatched || 0), 0)
    toastOk(message,
      `导入完成：${parts.join('，') || '无变更'}`
      + (unmatched ? `（未匹配 ${unmatched} 条）` : ''))
    emit('update:show', false)
    emit('imported')
    await session.refresh()
  } catch (e) {
    toastError(message, e)
  } finally {
    importing.value = false
  }
}
</script>

<template>
  <n-modal :show="show" preset="card" title="导入翻译文本" style="width: 640px"
           :mask-closable="!importing" :closable="!importing" :close-on-esc="!importing"
           @update:show="emit('update:show', $event)">
    <n-space vertical size="medium">
      <file-drop-select v-model="file" accept=".json,.docx"
                        placeholder="点击或拖拽选择文件（.json / 翻译 .docx）" />
      <n-text v-if="file && kind" depth="3" style="font-size: 12px">
        {{ kind === 'json' ? 'JSON：按原文匹配写回译文' : '翻译 Word：按行顺序对应写回译文' }}
      </n-text>

      <!-- 翻译 Word：类型与范围需与导出时一致 -->
      <template v-if="kind === 'tword'">
        <div>
          <n-text depth="3" style="font-size: 12px">
            导入内容（翻译 Word 只含译文行，按顺序与库中条目一一对应，需与导出时一致）
          </n-text>
          <n-checkbox-group v-model:value="selectedTypes">
            <n-space>
              <n-checkbox v-for="(label, t) in TYPE_LABELS" :key="t" :value="t" :label="label" />
            </n-space>
          </n-checkbox-group>
        </div>
        <div v-if="filterUsable">
          <n-text depth="3" style="font-size: 12px">范围</n-text>
          <n-radio-group v-model:value="scope">
            <n-space>
              <n-radio value="all">全部</n-radio>
              <n-radio value="filter">仅当前筛选结果（需与导出时相同筛选）</n-radio>
            </n-space>
          </n-radio-group>
        </div>
      </template>
    </n-space>

    <template #footer>
      <n-space justify="end">
        <n-button :disabled="importing" @click="close">取消</n-button>
        <n-button type="primary" :loading="importing" :disabled="!file" @click="submit">
          导入
        </n-button>
      </n-space>
    </template>
  </n-modal>
</template>
