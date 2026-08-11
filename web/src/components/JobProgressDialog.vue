<script setup lang="ts">
/** 任务进度对话框：进度条 + 阶段 + 滚动日志 + 结果/traceback + 取消/继续 */
import { computed, nextTick, ref, watch } from 'vue'
import { NAlert, NButton, NModal, NScrollbar, NSpace, NTag, useMessage } from 'naive-ui'
import { useJobsStore, type JobView } from '../stores/jobs'
import { toastError, copyWithFeedback } from '../api/client'
import ProgressLine from './ProgressLine.vue'

const props = defineProps<{ job: JobView }>()
const jobsStore = useJobsStore()
const message = useMessage()

const TERMINAL = ['succeeded', 'failed', 'cancelled', 'interrupted']
const isTerminal = computed(() => TERMINAL.includes(props.job.status))

const statusType = computed(() => ({
  running: 'info', waiting_input: 'warning', succeeded: 'success',
  failed: 'error', cancelled: 'warning', interrupted: 'error',
}[props.job.status] ?? 'default') as 'info' | 'warning' | 'success' | 'error' | 'default')

const statusText = computed(() => ({
  running: '运行中', waiting_input: '等待确认', succeeded: '已完成',
  failed: '失败', cancelled: '已取消', interrupted: '被中断（服务重启）',
}[props.job.status] ?? props.job.status))

// 日志自动滚动到底
const logRef = ref<InstanceType<typeof NScrollbar>>()
watch(() => props.job.logs.length, async () => {
  await nextTick()
  logRef.value?.scrollTo({ top: 1e8 })
})

async function onCancel() {
  try {
    await jobsStore.cancel(props.job.id)
  } catch (e) {
    toastError(message, e)
  }
}

function onClose() {
  jobsStore.closeDialog(props.job.id)
  if (isTerminal.value) jobsStore.dismiss(props.job.id)
}

const emit = defineEmits<{ finished: [job: JobView] }>()
watch(isTerminal, (v) => { if (v) emit('finished', props.job) })
</script>

<template>
  <n-modal
    :show="job.dialogOpen"
    preset="card"
    :title="job.label || '任务'"
    :style="{ width: job.question?.type === 'embedded_review' ? '980px' : '640px' }"
    :mask-closable="false"
    @update:show="onClose"
  >
    <n-space vertical>
      <n-space align="center">
        <n-tag :type="statusType" size="small">{{ statusText }}</n-tag>
        <span style="font-size: 13px; color: #aaa">{{ job.stage }}</span>
      </n-space>

      <progress-line
        :value="job.progress"
        :status="job.status === 'failed' ? 'error' : job.status === 'succeeded' ? 'success' : 'default'"
        :processing="job.status === 'running'"
      />

      <n-alert v-if="job.sseFailed" type="error" title="进度推送连接中断">
        实时进度已断开（任务本身可能仍在运行）。
        <n-button size="tiny" style="margin-left: 8px" @click="jobsStore.retry(job.id)">重连</n-button>
      </n-alert>

      <n-alert v-if="job.cancelling && !isTerminal" type="warning" title="正在取消">
        取消请求已收到，任务将在当前条目完成后停止
        （在飞的 API 调用需等待返回，并非卡住）。
      </n-alert>

      <n-alert v-if="job.error" type="error" title="任务失败">
        <pre style="white-space: pre-wrap; font-size: 12px; max-height: 200px; overflow: auto; margin: 0">{{ job.error }}</pre>
        <n-button size="tiny" style="margin-top: 6px" @click="copyWithFeedback(message, job.error!)">复制错误信息</n-button>
      </n-alert>

      <n-alert v-if="job.status === 'interrupted'" type="warning" title="服务重启导致任务中断">
        可重新发起同一操作——翻译类任务会自动跳过已完成部分继续。
      </n-alert>

      <n-scrollbar v-if="job.logs.length" ref="logRef" style="max-height: 220px; background: #141414; border-radius: 6px; padding: 8px">
        <div v-for="(line, i) in job.logs" :key="i" style="font-size: 12px; font-family: monospace; white-space: pre-wrap">{{ line }}</div>
      </n-scrollbar>

      <!-- 交互问题插槽（官中确认 / 内嵌复核等由具体页面渲染） -->
      <slot name="question" :question="job.question" />
    </n-space>

    <template #footer>
      <n-space justify="end">
        <n-button
          v-if="!isTerminal && job.status !== 'waiting_input'"
          type="error" quaternary :disabled="job.cancelling" @click="onCancel"
        >
          {{ job.cancelling ? '取消中…' : '取消任务' }}
        </n-button>
        <n-button v-if="isTerminal" @click="onClose">关闭</n-button>
      </n-space>
    </template>
  </n-modal>
</template>
