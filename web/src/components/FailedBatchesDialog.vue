<script setup lang="ts">
/** 失败条目核验对话框：批次翻译中未译出/存疑的条目，
 *  可全部重试（拆小批任务）、单句 AI 翻译、手动填写译文，或直接关闭。 */
import { ref, watch } from 'vue'
import {
  NButton, NEmpty, NInput, NInputNumber, NModal, NPopconfirm, NSpace, NTag,
  NText, useMessage,
} from 'naive-ui'
import { api, toastError, toastOk } from '../api/client'
import { useJobTask } from '../composables/useJobTask'

interface FailedItem {
  batch_id: number
  id: number
  character: string
  original_text: string
  reason: string
  created_at: string
}

const props = defineProps<{ show: boolean; contentType: 'ui' | 'dialogue' }>()
const emit = defineEmits<{
  'update:show': [boolean]
  changed: []
}>()

const message = useMessage()
const { runJob } = useJobTask()

const items = ref<FailedItem[]>([])
const loading = ref(false)
const chunkSize = ref(10)
const translatingIds = ref<Set<number>>(new Set())
const editingId = ref(0)
const editingText = ref('')

async function load() {
  loading.value = true
  try {
    const data = await api.get<{ items: FailedItem[]; count: number }>(
      `/api/current/texts/${props.contentType}/failed-batches`)
    items.value = data.items
  } catch (e) {
    toastError(message, e)
  } finally {
    loading.value = false
  }
}

watch(() => props.show, (v) => { if (v) load() })

/** 条目已译出（单翻/手动/重试成功）后从列表移除并通知父页刷新 */
function onItemDone(id: number) {
  items.value = items.value.filter((it) => it.id !== id)
  emit('changed')
}

// ---- 全部重试（拆小批任务） ----
async function retryAll() {
  await runJob(
    () => api.post(
      `/api/current/texts/${props.contentType}/failed-batches/retry`,
      { chunk_size: chunkSize.value }),
    async () => { await load(); emit('changed') })
}

// ---- 单句 AI 翻译 ----
async function translateOne(it: FailedItem) {
  translatingIds.value.add(it.id)
  try {
    await api.post(`/api/current/texts/${props.contentType}/${it.id}/translate`)
    onItemDone(it.id)
  } catch (e) {
    toastError(message, e)
  } finally {
    translatingIds.value.delete(it.id)
  }
}

// ---- 手动翻译 ----
function startEdit(it: FailedItem) {
  editingId.value = it.id
  editingText.value = ''
}

async function commitEdit(it: FailedItem) {
  const value = editingText.value.trim()
  editingId.value = 0
  if (!value) return
  try {
    await api.patch(`/api/current/texts/${props.contentType}/${it.id}`,
                    { translated_text: value })
    toastOk(message, '已保存')
    onItemDone(it.id)
  } catch (e) {
    toastError(message, e)
  }
}

// ---- 清空暂存 ----
async function clearAll() {
  try {
    await api.del(`/api/current/texts/${props.contentType}/failed-batches`)
    items.value = []
    emit('changed')
    toastOk(message, '暂存已清空，条目保持未翻译')
  } catch (e) {
    toastError(message, e)
  }
}
</script>

<template>
  <n-modal
    :show="show" preset="card" title="翻译失败的条目"
    style="width: 820px" @update:show="emit('update:show', $event)"
  >
    <n-text depth="3" style="font-size: 12px; display: block; margin-bottom: 10px">
      以下条目在批次翻译中未译出（模型未返回或译文存疑）。可全部重试（拆小批通常能恢复）、
      逐条 AI 翻译或手动填写；直接关闭则保持未翻译，下次「全部翻译」会重新拾起。
    </n-text>

    <n-space align="center" style="margin-bottom: 10px" wrap>
      <n-text depth="3" style="font-size: 12px">每批</n-text>
      <n-input-number v-model:value="chunkSize" size="small" :min="1" :max="50" style="width: 90px" />
      <n-button size="small" type="primary" :disabled="items.length === 0" @click="retryAll">
        全部重试（{{ items.length }} 条）
      </n-button>
      <n-popconfirm @positive-click="clearAll">
        <template #trigger>
          <n-button size="small" quaternary :disabled="items.length === 0">清空暂存</n-button>
        </template>
        清空后这些条目保持未翻译，可被下次「全部翻译」重新拾起。确定清空？
      </n-popconfirm>
      <n-button size="small" quaternary @click="load">刷新</n-button>
    </n-space>

    <n-empty v-if="!loading && items.length === 0" description="没有翻译失败的条目" />

    <div style="max-height: 480px; overflow-y: auto">
      <div v-for="it in items" :key="it.id"
           style="border: 1px solid #444; border-radius: 6px; padding: 8px 10px; margin-bottom: 8px">
        <n-space align="center" justify="space-between" wrap style="margin-bottom: 4px">
          <n-space align="center">
            <n-text depth="3" style="font-size: 12px">#{{ it.id }}</n-text>
            <n-tag v-if="it.character" size="small" type="info">{{ it.character }}</n-tag>
            <n-tag size="small" type="warning">{{ it.reason }}</n-tag>
          </n-space>
          <n-space align="center">
            <n-button
              size="tiny" type="primary" quaternary
              :loading="translatingIds.has(it.id)" @click="translateOne(it)"
            >AI翻译</n-button>
            <n-button size="tiny" quaternary @click="startEdit(it)">手动翻译</n-button>
          </n-space>
        </n-space>
        <div style="font-size: 13px; white-space: pre-wrap">{{ it.original_text }}</div>
        <div v-if="editingId === it.id" style="margin-top: 6px">
          <n-input
            v-model:value="editingText" type="textarea" size="small"
            :autosize="{ minRows: 1, maxRows: 4 }" placeholder="输入译文，Enter 保存 / Esc 取消"
            autofocus
            @keydown.enter.prevent="commitEdit(it)"
            @keydown.esc="editingId = 0"
            @blur="commitEdit(it)"
          />
        </div>
      </div>
    </div>
  </n-modal>
</template>
