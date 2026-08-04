<script setup lang="ts">
/** 应用主体（在 message/dialog provider 内，可用 useMessage） */
import { computed, onMounted } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import {
  NLayout, NLayoutHeader, NLayoutSider, NLayoutContent, NMenu, NSelect,
  NTag, NButton, NSpace, useMessage,
} from 'naive-ui'
import type { MenuOption } from 'naive-ui'
import { useSessionStore } from './stores/session'
import { useProjectsStore } from './stores/projects'
import { useJobsStore, type JobView } from './stores/jobs'
import JobProgressDialog from './components/JobProgressDialog.vue'
import EmbeddedReviewDialog from './components/EmbeddedReviewDialog.vue'
import { errorText } from './api/client'

const message = useMessage()
const session = useSessionStore()
const projectsStore = useProjectsStore()
const jobsStore = useJobsStore()
const route = useRoute()
const router = useRouter()

const menuOptions: MenuOption[] = [
  { label: '项目管理', key: '/projects' },
  { label: '人名翻译', key: '/names' },
  { label: '字符串翻译', key: '/strings' },
  { label: '对话翻译', key: '/dialogue' },
  { label: '导出游戏', key: '/export' },
  { label: '模型配置', key: '/settings' },
]

const activeKey = computed(() => route.path)

const projectOptions = computed(() =>
  projectsStore.list.map((p) => ({ label: p.name, value: p.name })))

/** 打开对话框的任务（含刷新后恢复的） */
const dialogJobs = computed(() =>
  [...jobsStore.jobs.values()].filter((j) => j.dialogOpen))

async function onProjectChange(name: string | null) {
  if (!name) return
  await session.open(name)
  await projectsStore.refresh()
}

function onMenu(key: string) {
  router.push(key)
}

/** 通用 confirm 问题回答 */
async function answerConfirm(job: JobView, ok: boolean) {
  const q = job.question
  if (!q) return
  try {
    await jobsStore.answer(job.id, q.question_id, { ok })
  } catch (e) {
    message.error(errorText(e), { duration: 8000 })
  }
}

onMounted(async () => {
  await session.refresh()
  await projectsStore.refresh()
  await jobsStore.restore()
  // 中断任务只给一条汇总提示（不逐条弹窗；翻译类重发即自动跳过已完成部分）
  if (session.interruptedJobs > 0) {
    message.warning(
      `上次服务退出时有 ${session.interruptedJobs} 个任务未跑完，已标记为中断。` +
      '重新发起即可（已翻译的部分会自动跳过）',
      { duration: 8000 })
  }
})
</script>

<template>
  <n-layout style="height: 100vh">
    <n-layout-header bordered style="padding: 0 16px; height: 52px; display: flex; align-items: center; gap: 16px">
      <span style="font-size: 16px; font-weight: 600">Ren'Py 翻译工具</span>
      <n-select
        style="width: 260px"
        size="small"
        placeholder="选择项目"
        :value="session.currentProject || null"
        :options="projectOptions"
        @update:value="onProjectChange"
      />
      <n-tag v-if="session.currentProject" size="small" type="success">
        {{ session.progressText }}
      </n-tag>
      <n-tag v-if="session.currentProject && !session.hasTranslator" size="small" type="warning">
        未配置模型
      </n-tag>
    </n-layout-header>
    <n-layout has-sider position="absolute" style="top: 52px">
      <n-layout-sider bordered :width="180">
        <n-menu
          :value="activeKey"
          :options="menuOptions"
          @update:value="onMenu"
        />
      </n-layout-sider>
      <n-layout-content content-style="padding: 16px">
        <router-view />
      </n-layout-content>
    </n-layout>
  </n-layout>

  <!-- 全局任务进度对话框（刷新/切路由后自动重开） -->
  <job-progress-dialog v-for="j in dialogJobs" :key="j.id" :job="j">
    <template #question="{ question }">
      <div v-if="question && question.type === 'confirm'" style="border-top: 1px solid #333; padding-top: 12px">
        <div style="font-weight: 600; margin-bottom: 6px">{{ question.payload.title }}</div>
        <div style="font-size: 13px; color: #ccc; white-space: pre-line">{{ question.payload.body }}</div>
        <n-space justify="end" style="margin-top: 10px">
          <n-button size="small" @click="answerConfirm(j, false)">取消</n-button>
          <n-button size="small" type="error" @click="answerConfirm(j, true)">确认</n-button>
        </n-space>
      </div>
      <embedded-review-dialog
        v-else-if="question && question.type === 'embedded_review'"
        :job="j" :question="question"
      />
    </template>
  </job-progress-dialog>
</template>
