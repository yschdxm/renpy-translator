<script setup lang="ts">
/** 导出游戏页：统计 + 一键导出（任务进度走全局对话框） */
import { onMounted, ref } from 'vue'
import { NButton, NCard, NProgress, NSpace, NText, useMessage } from 'naive-ui'
import { CubeOutline, FolderOpenOutline, RefreshOutline } from '@vicons/ionicons5'
import { api, errorText } from '../api/client'
import { renderIcon } from '../components/icons'
import { nativeReady, openFolder } from '../api/native'
import { useJobsStore } from '../stores/jobs'

const message = useMessage()
const jobsStore = useJobsStore()
const guiMode = ref(false)

interface ExportInfo {
  dialogue: { total: number; translated: number }
  ui: { total: number; translated: number }
  names: { total: number; translated: number }
  total: number
  translated: number
  percent: number
  output_dir: string
}

const info = ref<ExportInfo | null>(null)

async function load() {
  try {
    info.value = await api.get<ExportInfo>('/api/current/export/info')
  } catch (e) {
    message.error(errorText(e), { duration: 8000 })
  }
}

async function startExport() {
  try {
    const data = await api.post<{ job_id: string }>('/api/current/export/game')
    jobsStore.track(data.job_id)
  } catch (e) {
    message.error(errorText(e), { duration: 10000, closable: true })
  }
}

onMounted(async () => {
  guiMode.value = await nativeReady()
  await load()
})
</script>

<template>
  <div style="max-width: 720px">
    <h2 style="margin-top: 0">导出翻译后的游戏</h2>
    <n-text depth="3">将翻译后的游戏导出为独立目录（项目 output/ 下），可直接运行</n-text>

    <n-card size="small" style="margin: 16px 0" v-if="info">
      <n-space vertical>
        <n-space align="center">
          <n-text strong>总体进度: {{ info.translated }}/{{ info.total }}</n-text>
          <n-text depth="3">({{ info.percent }}%)</n-text>
        </n-space>
        <n-progress type="line" :percentage="info.percent" :height="10" />
        <n-space size="large">
          <n-text>对话翻译: {{ info.dialogue.translated }}/{{ info.dialogue.total }}</n-text>
          <n-text>字符串翻译: {{ info.ui.translated }}/{{ info.ui.total }}</n-text>
          <n-text>人名翻译: {{ info.names.translated }}/{{ info.names.total }}</n-text>
        </n-space>
      </n-space>
    </n-card>

    <n-space>
      <n-button type="primary" size="large" :render-icon="renderIcon(CubeOutline)" @click="startExport">开始导出</n-button>
      <n-button :render-icon="renderIcon(RefreshOutline)" @click="load">刷新统计</n-button>
      <n-button v-if="guiMode && info" :render-icon="renderIcon(FolderOpenOutline)" @click="openFolder(info.output_dir)">
        打开输出目录
      </n-button>
    </n-space>
  </div>
</template>
