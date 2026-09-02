<script setup lang="ts">
/** 失败条目核验对话框：批次翻译中未译出/存疑的条目，
 *  AI 曾返回但被拒的译文默认填进译文框（像翻译页一样行内直接修改），
 *  点「采用」或按 Enter 才落库——不自动保存，改错了关掉也不会误写。 */
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
  rejected: string  // AI 返回过但校验被拒的译文（预填进译文框）
  created_at: string
  draft: string     // 行内译文框内容（默认 = rejected）
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
const savingIds = ref<Set<number>>(new Set())

async function load() {
  loading.value = true
  try {
    const data = await api.get<{ items: FailedItem[]; count: number }>(
      `/api/current/texts/${props.contentType}/failed-batches`)
    items.value = data.items.map((it) => ({ ...it, draft: it.rejected || '' }))
  } catch (e) {
    toastError(message, e)
  } finally {
    loading.value = false
  }
}

watch(() => props.show, (v) => { if (v) load() })

/** 条目已译出（单翻/采用/重试成功）后从列表移除并通知父页刷新 */
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

// ---- 采用译文（显式确认才落库；Enter 同效） ----
async function commitEdit(it: FailedItem) {
  const value = it.draft.trim()
  if (!value || savingIds.value.has(it.id)) return
  savingIds.value.add(it.id)
  try {
    await api.patch(`/api/current/texts/${props.contentType}/${it.id}`,
                    { translated_text: value })
    toastOk(message, '已保存')
    onItemDone(it.id)
  } catch (e) {
    toastError(message, e)
  } finally {
    savingIds.value.delete(it.id)
  }
}

/** 可采用的条数（译文框非空），用于「全部采用」按钮 */
function adoptableCount(): number {
  return items.value.filter((it) => it.draft.trim()).length
}

// ---- 全部采用：把所有非空译文框的内容落库（逐条复用采用逻辑） ----
async function adoptAll() {
  for (const it of [...items.value]) {
    if (it.draft.trim()) await commitEdit(it)
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
      以下条目在批次翻译中未译出（模型未返回或译文存疑）。AI 曾返回的译文已填进译文框，
      可直接修改——点「采用」或按 Enter 才落库，不会自动保存。
      直接关闭则保持未翻译，下次「全部翻译」会重新拾起。
    </n-text>

    <n-space align="center" style="margin-bottom: 10px" wrap>
      <n-text depth="3" style="font-size: 12px">每批</n-text>
      <n-input-number v-model:value="chunkSize" size="small" :min="1" :max="50" style="width: 90px" />
      <n-button size="small" type="primary" :disabled="items.length === 0" @click="retryAll">
        全部重试（{{ items.length }} 条）
      </n-button>
      <n-button size="small" type="primary" secondary :disabled="adoptableCount() === 0" @click="adoptAll">
        全部采用（{{ adoptableCount() }} 条）
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
            <n-button
              size="tiny" type="success" :disabled="!it.draft.trim()"
              :loading="savingIds.has(it.id)" @click="commitEdit(it)"
            >采用</n-button>
          </n-space>
        </n-space>
        <div style="font-size: 13px; white-space: pre-wrap">{{ it.original_text }}</div>
        <n-input
          v-model:value="it.draft" type="textarea" size="small"
          :autosize="{ minRows: 1, maxRows: 4 }"
          :placeholder="it.rejected ? 'AI 译文（未通过校验），可直接修改' : '输入译文'"
          style="margin-top: 6px"
          @keydown.enter.prevent="commitEdit(it)"
        />
      </div>
    </div>
  </n-modal>
</template>
