<script setup lang="ts">
import { ref, watch } from 'vue'
import {
  NButton, NCollapse, NCollapseItem, NEmpty, NModal, NSpace,
  NSpin, NStatistic, NTable, NTag, NText, useMessage,
} from 'naive-ui'
import { api, toastError, toastOk } from '../api/client'

interface UpdateReport {
  carried: number
  edited: number
  new: number
  still_untranslated: number
  obsolete: number
  review: number
  embedded_rewrapped: number
  embedded_lost: number
  updated_at: string
}

interface ReviewRow {
  id: number
  target_kind: string
  target_id: number
  new_original: string
  old_original: string
  old_translation: string
  ratio: number
  status: string
}

interface ObsoleteRow {
  id: number
  kind: string
  character: string
  original_text: string
  translated_text: string
}

const props = defineProps<{ projectName: string }>()
const show = defineModel<boolean>('show', { default: false })

const message = useMessage()
const loading = ref(false)
const report = ref<UpdateReport | null>(null)
const review = ref<ReviewRow[]>([])
const obsolete = ref<ObsoleteRow[]>([])
const acting = ref(0)  // 正在操作的复核行 id

async function load() {
  loading.value = true
  try {
    const data = await api.get<{
      report: UpdateReport | null
      obsolete: ObsoleteRow[]
      review: ReviewRow[]
    }>(`/api/projects/${encodeURIComponent(props.projectName)}/update-report`)
    report.value = data.report
    obsolete.value = data.obsolete
    review.value = data.review
  } catch (e) {
    toastError(message, e)
  } finally {
    loading.value = false
  }
}

watch(show, (v) => { if (v) load() })

async function reviewAction(row: ReviewRow, action: 'apply' | 'dismiss') {
  acting.value = row.id
  try {
    await api.post(
      `/api/projects/${encodeURIComponent(props.projectName)}/update-review/${row.id}`,
      { action })
    row.status = action === 'apply' ? 'applied' : 'dismissed'
    if (action === 'apply') toastOk(message, '已应用旧译文')
  } catch (e) {
    toastError(message, e)
  } finally {
    acting.value = 0
  }
}

function statusTag(status: string) {
  if (status === 'applied') return { type: 'success' as const, text: '已应用' }
  if (status === 'dismissed') return { type: 'default' as const, text: '已忽略' }
  return { type: 'warning' as const, text: '待复核' }
}

const pendingReview = () => review.value.filter((r) => r.status === 'pending')
</script>

<template>
  <n-modal v-model:show="show" preset="card" :title="`版本更新报告 — ${projectName}`"
           style="width: 860px; max-height: 85vh; overflow: auto">
    <n-spin :show="loading">
      <n-empty v-if="!loading && !report" description="该项目还没有执行过版本更新" />
      <n-space v-else-if="report" vertical size="large">
        <n-space>
          <n-statistic label="精确继承" :value="report.carried" />
          <n-statistic label="微改自动继承" :value="report.edited" />
          <n-statistic label="新增待翻译" :value="report.new" />
          <n-statistic label="原本待翻译" :value="report.still_untranslated" />
          <n-statistic label="失效旧译文" :value="report.obsolete" />
        </n-space>
        <n-text depth="3" style="font-size: 12px">
          更新时间 {{ report.updated_at?.replace('T', ' ').slice(0, 19) }}
          <template v-if="report.embedded_rewrapped || report.embedded_lost">
            · 内嵌文本重标记 {{ report.embedded_rewrapped }} 条
            <template v-if="report.embedded_lost">（{{ report.embedded_lost }} 条未找到已重置）</template>
          </template>
        </n-text>

        <!-- 微改复核：自动继承的留痕 + 待人工确认的近似句 -->
        <div v-if="review.length">
          <h3 style="margin: 0 0 8px">微改复核（{{ pendingReview().length }} 条待处理 / 共 {{ review.length }} 条）</h3>
          <n-table size="small" :bordered="false" single-line>
            <thead>
              <tr>
                <th style="width: 28%">新版原文</th>
                <th style="width: 28%">旧版原文</th>
                <th style="width: 24%">旧译文</th>
                <th style="width: 8%">相似度</th>
                <th style="width: 12%">操作</th>
              </tr>
            </thead>
            <tbody>
              <tr v-for="r in review" :key="r.id">
                <td>{{ r.new_original }}</td>
                <td>{{ r.old_original }}</td>
                <td>{{ r.old_translation }}</td>
                <td>{{ Math.round(r.ratio * 100) }}%</td>
                <td>
                  <n-space v-if="r.status === 'pending'" :size="4">
                    <n-button size="tiny" type="primary" :loading="acting === r.id"
                              @click="reviewAction(r, 'apply')">应用</n-button>
                    <n-button size="tiny" quaternary :disabled="acting === r.id"
                              @click="reviewAction(r, 'dismiss')">忽略</n-button>
                  </n-space>
                  <n-tag v-else size="small" :type="statusTag(r.status).type" :bordered="false">
                    {{ statusTag(r.status).text }}
                  </n-tag>
                </td>
              </tr>
            </tbody>
          </n-table>
        </div>

        <!-- 失效旧译文 -->
        <n-collapse v-if="obsolete.length">
          <n-collapse-item :title="`失效旧译文（${obsolete.length} 条，新版游戏中已不存在）`" name="obsolete">
            <n-table size="small" :bordered="false" single-line>
              <thead>
                <tr>
                  <th style="width: 10%">类型</th>
                  <th style="width: 12%">角色</th>
                  <th style="width: 39%">原文</th>
                  <th style="width: 39%">译文</th>
                </tr>
              </thead>
              <tbody>
                <tr v-for="o in obsolete" :key="o.id">
                  <td>{{ o.kind === 'dialogue' ? '对话' : '字符串' }}</td>
                  <td>{{ o.character }}</td>
                  <td>{{ o.original_text }}</td>
                  <td>{{ o.translated_text }}</td>
                </tr>
              </tbody>
            </n-table>
          </n-collapse-item>
        </n-collapse>
      </n-space>
    </n-spin>
    <template #footer>
      <n-space justify="end">
        <n-button @click="show = false">关闭</n-button>
      </n-space>
    </template>
  </n-modal>
</template>
