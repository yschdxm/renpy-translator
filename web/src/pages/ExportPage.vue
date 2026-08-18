<script setup lang="ts">
/** 导出游戏页：统计 + 一键导出（任务进度走全局对话框） */
import { computed, onMounted, ref } from 'vue'
import { NButton, NCard, NPopconfirm, NSpace, NText, useMessage } from 'naive-ui'
import { FolderOpenOutline, PlayOutline, RefreshOutline } from '@vicons/ionicons5'
import { api, toastError } from '../api/client'
import { renderIcon } from '../components/icons'
import ProgressLine from '../components/ProgressLine.vue'
import { useJobsStore } from '../stores/jobs'
import { useSessionStore } from '../stores/session'

const message = useMessage()
const jobsStore = useJobsStore()
const session = useSessionStore()

interface ExportInfo {
  dialogue: { total: number; translated: number }
  ui: { total: number; translated: number }
  names: { total: number; translated: number }
  total: number
  translated: number
  percent: number
  exports_dir: string
}

const info = ref<ExportInfo | null>(null)

/** 未翻译条数（>0 时导出前弹确认提醒） */
const untranslated = computed(() =>
  info.value ? info.value.total - info.value.translated : 0)

async function load() {
  try {
    info.value = await api.get<ExportInfo>('/api/current/export/info')
  } catch (e) {
    toastError(message, e)
  }
}

async function startExport() {
  try {
    const data = await api.post<{ job_id: string }>('/api/current/export/game')
    jobsStore.track(data.job_id)
  } catch (e) {
    toastError(message, e)
  }
}

async function revealExports() {
  try {
    await api.post(
      `/api/projects/${encodeURIComponent(session.currentProject)}/packages/reveal`)
  } catch (e) {
    toastError(message, e)
  }
}

onMounted(load)
</script>

<template>
  <div>
    <n-space align="center" style="margin-bottom: 12px">
      <h2 style="margin: 0">导出游戏</h2>
      <n-popconfirm v-if="untranslated > 0" @positive-click="startExport">
        <template #trigger>
          <n-button size="small" type="primary" :render-icon="renderIcon(PlayOutline)">开始导出</n-button>
        </template>
        还有 {{ untranslated }} 条未翻译，导出包中这些内容将保持原文。确定导出？
      </n-popconfirm>
      <n-button
        v-else size="small" type="primary"
        :render-icon="renderIcon(PlayOutline)" @click="startExport"
      >开始导出</n-button>
      <n-button size="small" quaternary :render-icon="renderIcon(RefreshOutline)" @click="load">刷新</n-button>
      <n-button v-if="info" size="small" :render-icon="renderIcon(FolderOpenOutline)" @click="revealExports">
        打开导出目录
      </n-button>
    </n-space>
    <n-text depth="3" style="font-size: 12px">
      导出为 {{ session.currentProject }}-translated.zip（与项目包同目录），解压即可运行
    </n-text>

    <n-card size="small" style="margin: 16px 0" v-if="info">
      <n-space vertical>
        <n-space align="center">
          <n-text strong>总体进度: {{ info.translated }}/{{ info.total }}</n-text>
          <n-text depth="3">({{ info.percent }}%)</n-text>
        </n-space>
        <progress-line :value="info.percent / 100" />
        <n-space size="large" align="center">
          <n-space align="center" size="small">
            <n-text>对话翻译: {{ info.dialogue.translated }}/{{ info.dialogue.total }}</n-text>
            <progress-line
              :value="info.dialogue.total ? info.dialogue.translated / info.dialogue.total : 0"
              :show-text="false" style="width: 120px"
            />
          </n-space>
          <n-space align="center" size="small">
            <n-text>字符串翻译: {{ info.ui.translated }}/{{ info.ui.total }}</n-text>
            <progress-line
              :value="info.ui.total ? info.ui.translated / info.ui.total : 0"
              :show-text="false" style="width: 120px"
            />
          </n-space>
          <n-space align="center" size="small">
            <n-text>人名翻译: {{ info.names.translated }}/{{ info.names.total }}</n-text>
            <progress-line
              :value="info.names.total ? info.names.translated / info.names.total : 0"
              :show-text="false" style="width: 120px"
            />
          </n-space>
        </n-space>
      </n-space>
    </n-card>
  </div>
</template>
