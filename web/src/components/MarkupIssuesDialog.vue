<script setup lang="ts">
/** 标记违规逐条修订对话框：导出预检扫出的破坏插值/标签的译文，
 *  行内直接修改（点「保存」或按 Enter 才落库），或单条/全部 AI 重译；
 *  每次落库后单条复核，通过即从清单移除。 */
import { computed, ref, watch } from 'vue'
import {
  NButton, NEmpty, NInput, NModal, NPagination, NSpace, NSpin, NTag, NText,
  useMessage,
} from 'naive-ui'
import { api, toastError, toastOk } from '../api/client'

interface IssueItem {
  kind: 'dialogue' | 'ui'
  id: number
  file: string
  line: number
  original: string
  translation: string
  reasons: string[]
  draft: string      // 行内译文框内容（默认 = 当前库内译文）
}

const props = defineProps<{ show: boolean }>()
const emit = defineEmits<{
  'update:show': [boolean]
  changed: []
}>()

const message = useMessage()

const items = ref<IssueItem[]>([])
const loading = ref(false)
const savingIds = ref<Set<number>>(new Set())
const translatingIds = ref<Set<number>>(new Set())
const bulkRunning = ref(false)
const bulkProgress = ref('')
let bulkCancel = false

// ---- 分页（违规可能数百条，全渲染会卡） ----
const page = ref(1)
const pageSize = 20
const pageItems = computed(() =>
  items.value.slice((page.value - 1) * pageSize, page.value * pageSize))
const pageCount = computed(() =>
  Math.max(1, Math.ceil(items.value.length / pageSize)))

async function load() {
  loading.value = true
  try {
    const data = await api.get<{ rows: IssueItem[] }>(
      '/api/current/export/markup-issues')
    items.value = data.rows.map((r) => ({ ...r, draft: r.translation }))
    page.value = 1
  } catch (e) {
    toastError(message, e)
  } finally {
    loading.value = false
  }
}

watch(() => props.show, (v) => { if (v) load() })

function key(it: IssueItem) { return `${it.kind}:${it.id}` }

/** 复核通过：从清单移除并通知父页刷新计数 */
function onResolved(it: IssueItem) {
  items.value = items.value.filter((x) => key(x) !== key(it))
  emit('changed')
}

/** 落库后单条复核：通过移除；仍违规则刷新该条的违规原因 */
async function recheck(it: IssueItem): Promise<boolean> {
  const data = await api.post<{
    problems: string[]; translated_text: string
  }>('/api/current/export/markup-check', { kind: it.kind, id: it.id })
  it.draft = data.translated_text
  if (data.problems.length === 0) {
    onResolved(it)
    return true
  }
  it.reasons = data.problems
  return false
}

// ---- 保存手动修订（显式确认才落库；Enter 同效） ----
async function commitEdit(it: IssueItem) {
  const value = it.draft.trim()
  if (!value || savingIds.value.has(it.id)) return
  savingIds.value.add(it.id)
  try {
    await api.patch(`/api/current/texts/${it.kind}/${it.id}`,
                    { translated_text: value })
    if (await recheck(it)) {
      toastOk(message, '已保存，校验通过')
    } else {
      message.warning('已保存，但仍未通过校验，请继续修订')
    }
  } catch (e) {
    toastError(message, e)
  } finally {
    savingIds.value.delete(it.id)
  }
}

// ---- 单条 AI 重译（翻译管线自带标记修复重试） ----
async function translateOne(it: IssueItem): Promise<boolean> {
  translatingIds.value.add(it.id)
  try {
    await api.post(`/api/current/texts/${it.kind}/${it.id}/translate`)
    return await recheck(it)
  } catch (e) {
    toastError(message, e)
    return false
  } finally {
    translatingIds.value.delete(it.id)
  }
}

// ---- 全部 AI 重译（串行，随时可停） ----
async function translateAll() {
  bulkRunning.value = true
  bulkCancel = false
  let fixed = 0
  const snapshot = [...items.value]
  try {
    for (let i = 0; i < snapshot.length; i++) {
      if (bulkCancel) break
      bulkProgress.value = `AI 重译中 (${i + 1}/${snapshot.length}，已修复 ${fixed}）`
      if (await translateOne(snapshot[i])) fixed++
    }
    bulkProgress.value = bulkCancel
      ? `已停止（修复 ${fixed} 条）`
      : `完成：修复 ${fixed}/${snapshot.length} 条，剩余请手动修订`
  } finally {
    bulkRunning.value = false
  }
}

function stopBulk() { bulkCancel = true }

function shortFile(f: string) {
  const parts = f.replace(/\\/g, '/').split('/')
  return parts[parts.length - 1] || f
}
</script>

<template>
  <n-modal
    :show="show" preset="card" style="width: 900px; max-width: 95vw"
    title="修订标记违规译文" :mask-closable="false"
    @update:show="(v: boolean) => emit('update:show', v)"
  >
    <n-space vertical size="medium">
      <n-text depth="3" style="font-size: 12px">
        这些译文破坏或丢失了原文的插值 [表达式] / 标签 {标签}，导出时会保留英文原文。
        行内修改后点「保存」（或按 Enter），或让 AI 重译——通过校验的条目会自动从清单移除。
      </n-text>

      <n-space align="center">
        <n-button
          size="small" type="primary" :disabled="bulkRunning || items.length === 0"
          @click="translateAll"
        >全部 AI 重译（{{ items.length }}）</n-button>
        <n-button v-if="bulkRunning" size="small" @click="stopBulk">停止</n-button>
        <n-text v-if="bulkProgress" depth="3" style="font-size: 12px">{{ bulkProgress }}</n-text>
      </n-space>

      <n-spin :show="loading">
        <n-empty
          v-if="!loading && items.length === 0"
          description="没有违规条目，可以直接导出"
        />
        <n-space v-else vertical size="medium">
          <div
            v-for="it in pageItems" :key="key(it)"
            style="border: 1px solid rgba(255,255,255,0.09); border-radius: 6px; padding: 10px"
          >
            <n-space align="center" size="small" style="margin-bottom: 6px">
              <n-tag size="small" :type="it.kind === 'dialogue' ? 'info' : 'default'">
                {{ it.kind === 'dialogue' ? '对话' : '字符串' }}
              </n-tag>
              <n-text depth="3" style="font-size: 12px">
                {{ shortFile(it.file) }}:{{ it.line }}
              </n-text>
            </n-space>
            <div style="font-size: 13px; margin-bottom: 4px; opacity: 0.85">
              原文：{{ it.original }}
            </div>
            <div style="font-size: 12px; margin-bottom: 6px; color: #e88080">
              {{ it.reasons.join('；') }}
            </div>
            <n-space align="center" size="small">
              <n-input
                v-model:value="it.draft" size="small" placeholder="修订译文"
                style="min-width: 480px" :disabled="translatingIds.has(it.id)"
                @keyup.enter="commitEdit(it)"
              />
              <n-button
                size="small" type="primary"
                :loading="savingIds.has(it.id)"
                :disabled="!it.draft.trim() || translatingIds.has(it.id)"
                @click="commitEdit(it)"
              >保存</n-button>
              <n-button
                size="small"
                :loading="translatingIds.has(it.id)"
                :disabled="savingIds.has(it.id)"
                @click="translateOne(it)"
              >AI 重译</n-button>
            </n-space>
          </div>
          <n-space justify="center">
            <n-pagination
              v-if="pageCount > 1" v-model:page="page" :page-count="pageCount"
              size="small"
            />
          </n-space>
        </n-space>
      </n-spin>
    </n-space>
  </n-modal>
</template>
